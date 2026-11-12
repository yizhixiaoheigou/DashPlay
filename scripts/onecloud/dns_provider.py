"""DNSPod / dynv6 record operations, shared by DDNS and the bundled DNS-01 hooks.

Only the configured name's A record and the exact challenge TXT are managed. A
dedicated name is required; ambiguous A / AAAA / CNAME records are left untouched.
Credentials travel in HTTPS bodies/headers, never URLs, argv or error messages.
"""
import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class ProviderError(ValueError):
    def __init__(self, reason="failed", code=""):
        super().__init__(reason)
        self.reason, self.code = reason, code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward credentials to a redirect target


def identifier(value):
    value = str(value)
    if not value.isascii() or not value.isdigit():
        raise ProviderError()
    return value


class Client:
    def __init__(self, config, timeout=15):
        self.config, self.timeout = config, timeout
        self.deadline = time.monotonic() + 60
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, url, data=None, method=None, headers=None):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ProviderError()
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={"User-Agent": "DashPlay", **(headers or {})})
        try:
            with self.opener.open(request, timeout=min(left, self.timeout)) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ProviderError()
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raise ProviderError("denied" if error.code in (401, 403, 404) else
                                "invalid" if error.code in (400, 422) else "failed") from None
        except (OSError, ValueError):
            raise ProviderError() from None


class DNSPod(Client):
    def call(self, action, allowed=(), **fields):
        data = urllib.parse.urlencode(dict(login_token=self.config["id"] + "," + self.config["token"],
                                          format="json", lang="en", **fields)).encode()
        result = self.request("https://dnsapi.cn/" + action, data,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        code = str(result["status"]["code"])
        if code not in ("1", *allowed):
            raise ProviderError("denied" if code in ("-1", "-2", "-3", "-15", "7", "8") else
                                "failed" if code in ("-4", "-99") else "invalid", code)
        return result

    def zone(self, name):
        labels = name.split(".")
        # DNS-01's underscore label is a record, never a hosted zone. Some API
        # versions reject it as invalid rather than returning "domain not found".
        start = 1 if labels[0] == "_acme-challenge" else 0
        for index in range(start, len(labels) - 1):
            zone = ".".join(labels[index:])
            result = self.call("Domain.Info", allowed=("6", "7", "8", "13"), domain=zone)
            if str(result["status"]["code"]) == "1":
                info = result["domain"]
                if info["name"].lower().rstrip(".") != zone:
                    raise ProviderError()
                return identifier(info["id"]), ".".join(labels[:index]) or "@", max(600, int(info.get("ttl", 600)))
        raise ProviderError("denied")

    def records(self, zone, sub):
        found = []
        for offset in range(0, 3000, 100):
            result = self.call("Record.List", allowed=("10",), domain_id=zone,
                               sub_domain=sub, offset=offset, length=100)
            if str(result["status"]["code"]) == "10":
                return found
            records = result["records"]
            found.extend(record for record in records if record["name"] == sub)
            if len(records) < 100:
                return found
        raise ProviderError("invalid")  # do not mutate a truncated record set

    def txt(self, name, value, remove=False):
        zone, sub, ttl = self.zone(name)
        matches = [r for r in self.records(zone, sub) if r["type"] == "TXT" and r["value"] == value]
        if remove:
            for record in matches:
                self.call("Record.Remove", domain_id=zone, record_id=identifier(record["id"]))
        elif not matches:
            self.call("Record.Create", allowed=("104",), domain_id=zone, sub_domain=sub,
                      record_type="TXT", record_line="默认", value=value, ttl=ttl)

    def address(self, name, address):
        zone, sub, ttl = self.zone(name)
        records = self.records(zone, sub)
        candidates = [r for r in records if r["type"] == "A"]
        if (len(candidates) > 1 or any(r["type"] in ("AAAA", "CNAME") for r in records)
                or any(r.get("line") != "默认" for r in candidates)):
            raise ProviderError("conflict")
        fields = dict(domain_id=zone, sub_domain=sub, record_type="A", record_line="默认", value=address, ttl=ttl)
        if candidates:
            record = candidates[0]
            if record["value"] == address and record.get("enabled", "1") == "1":
                return
            self.call("Record.Modify", record_id=identifier(record["id"]), status="enable", **fields)
        else:
            self.call("Record.Create", **fields)


class Dynv6(Client):
    def call(self, path, data=None, method=None):
        return self.request("https://dynv6.com/api/v2" + path,
                            json.dumps(data).encode() if data is not None else None, method,
                            {"Authorization": "Bearer " + self.config["token"], "Content-Type": "application/json"})

    def zone(self, name):
        zones = self.call("/zones")
        matches = [zone for zone in zones if name == zone["name"] or name.endswith("." + zone["name"])]
        if not matches:
            raise ProviderError("denied")
        zone = max(matches, key=lambda zone: len(zone["name"]))
        path = "/zones/" + identifier(zone["id"])
        sub = name[:-(len(zone["name"]) + 1)] if name != zone["name"] else ""
        return path, sub, zone

    def records(self, path, sub):
        return [r for r in self.call(path + "/records") if r["name"] == sub]

    def txt(self, name, value, remove=False):
        path, sub, _ = self.zone(name)
        matches = [r for r in self.records(path, sub) if r["type"] == "TXT" and r["data"] == value]
        if remove:
            for record in matches:
                self.call(path + "/records/" + identifier(record["id"]), method="DELETE")
        elif not matches:
            self.call(path + "/records", dict(name=sub, type="TXT", data=value), "POST")

    def address(self, name, address):
        path, sub, zone = self.zone(name)
        records = self.records(path, sub)
        candidates = [r for r in records if r["type"] == "A"]
        if any(r["type"] in ("AAAA", "CNAME") for r in records) or len(candidates) > 1:
            raise ProviderError("conflict")
        if not sub:
            # The zone's primary address is separate from its records list.
            if candidates or zone.get("ipv6prefix") or zone.get("ipv6address"):
                raise ProviderError("conflict")
            if zone.get("ipv4address") != address:
                self.call(path, {"ipv4address": address}, "PATCH")
        elif candidates:
            if candidates[0]["data"] != address:
                self.call(path + "/records/" + identifier(candidates[0]["id"]), {"data": address}, "PATCH")
        else:
            self.call(path + "/records", dict(name=sub, type="A", data=address), "POST")


def client(config, timeout=15):
    return {"dnspod": DNSPod, "dynv6": Dynv6}[config["provider"]](config, timeout)


def update_address(config, address, timeout=15):
    try:
        client(config, timeout).address(config["domain"], str(ipaddress.IPv4Address(address)))
        return "ok"
    except ProviderError as error:
        return error.reason
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "failed"


def main():
    # Called only by our acme.sh hooks. Never print API responses or exception details.
    try:
        provider, action, name, value = sys.argv[1:]
        if action not in ("add", "rm") or name != "_acme-challenge." + os.environ["DASHPLAY_DNS_DOMAIN"]:
            raise ValueError()
        config = {"provider": provider, "token": os.environ["DP_Key" if provider == "dnspod" else "DYNV6_TOKEN"]}
        if provider == "dnspod":
            config["id"] = os.environ["DP_Id"]
        client(config).txt(name, value, remove=action == "rm")
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("DNS provider operation failed; check credentials, permissions and network.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
