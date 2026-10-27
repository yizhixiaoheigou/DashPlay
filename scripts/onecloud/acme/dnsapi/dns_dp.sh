#!/usr/bin/env sh
# DashPlay: DNSPod (Tencent Cloud DNS, https://dnsapi.cn) DNS-01 for acme.sh.
# Credentials stay in the environment: DP_Id and DP_Key. They are not saved here and not printed.

dns_dp_add() {
  fulldomain="$1"
  txtvalue="$2"
  _dp_init || return 1
  _dp_zone "$fulldomain" || return 1
  _dp_create "$_dp_domain_id" "$_dp_sub" "$txtvalue"
}

dns_dp_rm() {
  fulldomain="$1"
  txtvalue="$2"
  _dp_init || return 1
  _dp_zone "$fulldomain" || return 1
  _dp_remove "$_dp_domain_id" "$_dp_sub" "$txtvalue"
}

_dp_init() {
  if [ -z "${DP_Id}" ] || [ -z "${DP_Key}" ]; then
    _err "DP_Id and DP_Key are required."
    return 1
  fi
}

_dp_post() {
  action="$1"
  body="$2"
  export _H1="User-Agent: DashPlay/1.0"
  export _H2="Content-Type: application/x-www-form-urlencoded"
  response="$(_post "login_token=${DP_Id},${DP_Key}&format=json&lang=en&${body}" "https://dnsapi.cn/${action}" "" "POST")" || return 1
  _dp_code="$(printf '%s' "$response" | _dp_json code)" || return 1
  [ "$_dp_code" = "1" ]
}

_dp_json() {
  python3 -c '
import json, sys
kind = sys.argv[1]
want = sys.argv[2] if len(sys.argv) > 2 else ""
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(2)
if kind == "code":
    print((data.get("status") or {}).get("code") or "")
elif kind == "id":
    print((data.get("domain") or {}).get("id") or "")
elif kind == "records":
    for rec in data.get("records") or []:
        if str(rec.get("value") or "") == want and rec.get("id"):
            print(rec["id"])
else:
    sys.exit(2)
' "$1" "$2"
}

_dp_zone() {
  fulldomain="$1"
  name="$fulldomain"
  while :; do
    case "$name" in
      *.*) ;;
      *) _err "could not find the DNSPod zone"; return 1 ;;
    esac
    if _dp_post "Domain.Info" "domain=${name}"; then
      _dp_domain="$name"
      _dp_domain_id="$(printf '%s' "$response" | _dp_json id)" || return 1
      [ -n "$_dp_domain_id" ] || return 1
      if [ "$fulldomain" = "$_dp_domain" ]; then
        _dp_sub="@"
      else
        _dp_sub="${fulldomain%."$_dp_domain"}"
      fi
      return 0
    fi
    case "$name" in
      *.*.*) name="${name#*.}" ;;
      *) _err "could not find the DNSPod zone"; return 1 ;;
    esac
  done
}

_dp_create() {
  domain_id="$1"
  sub="$2"
  txtvalue="$3"
  # 默认 is the Chinese console's line. Some accounts only accept Default.
  if _dp_post "Record.Create" "domain_id=${domain_id}&sub_domain=${sub}&record_type=TXT&record_line=%E9%BB%98%E8%AE%A4&value=${txtvalue}"; then
    return 0
  fi
  _dp_post "Record.Create" "domain_id=${domain_id}&sub_domain=${sub}&record_type=TXT&record_line=Default&value=${txtvalue}"
}

_dp_remove() {
  domain_id="$1"
  sub="$2"
  txtvalue="$3"
  _dp_post "Record.List" "domain_id=${domain_id}&sub_domain=${sub}&record_type=TXT" || return 1
  ids="$(printf '%s' "$response" | _dp_json records "$txtvalue")" || return 1
  for record_id in $ids; do
    _dp_post "Record.Remove" "domain_id=${domain_id}&record_id=${record_id}" || return 1
  done
}
