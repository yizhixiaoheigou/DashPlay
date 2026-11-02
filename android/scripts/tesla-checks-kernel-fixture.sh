#!/system/bin/sh
set -eu
case "$(getprop ro.kernel.qemu)" in 1) ;; *) echo 'emulator required' >&2; exit 2;; esac
mode=${1:-kernel}
case "$mode" in kernel|rules) ;; *) exit 2;; esac
iface=dp-tesla0
[ "$mode" != rules ] || iface=eth0
base=/data/data/com.dashplay.android/cache/tesla-check-probe
nsfile=/data/local/tmp/tesla-check-ns.pid
namespace_pid=
probe_pid=
run_script() {
    case "$-" in *x*) sh -x "$@";; *) sh "$@";; esac
}
cleanup() {
    [ ! -f "$base/remove.sh" ] || sh "$base/remove.sh" >/dev/null 2>&1 || true
    [ ! -d "$base" ] || { : > "$base/done"; } 2>/dev/null || true
    [ -z "$probe_pid" ] || wait "$probe_pid" 2>/dev/null || true
    [ -z "$namespace_pid" ] || kill "$namespace_pid" 2>/dev/null || true
    if [ "$mode" = kernel ]; then
        ip link del dp-tesla0 2>/dev/null || true
        ip rule del pref 9000 from 192.0.2.1/32 to 192.0.2.0/24 lookup main 2>/dev/null || true
        ip route del blackhole 100.96.0.2/32 table local 2>/dev/null || true
    else
        ip addr del 192.0.2.1/24 dev "$iface" 2>/dev/null || true
    fi
    rm -f "$nsfile"
}
trap cleanup EXIT
# Snapshot operations do not wait indefinitely for the legacy xtables lock.
before=$(iptables -t nat -S)
rm -f "$nsfile"
if [ "$mode" = kernel ]; then
unshare -n sh -c 'echo $$ > /data/local/tmp/tesla-check-ns.pid; sleep 120' >/data/local/tmp/tesla-check-namespace.log 2>&1 &
for count in 1 2 3 4 5; do [ -s "$nsfile" ] && break; sleep 1; done
namespace_pid=$(cat "$nsfile")
ns() { nsenter --net="/proc/$namespace_pid/ns/net" -- "$@"; }
ip link add dp-tesla0 type veth peer name dp-peer
ip link set dp-peer netns "$namespace_pid"
fi
# Rules-only mode uses a temporary extra address on the disposable emulator's existing NIC.
# The legacy image lacks namespace tools and dummy-interface support; no client traffic is claimed.
ip addr add 192.0.2.1/24 dev "$iface"
if [ "$mode" = kernel ]; then
ip link set dp-tesla0 up
ip rule add pref 9000 from 192.0.2.1/32 to 192.0.2.0/24 lookup main
ip route add blackhole 100.96.0.2/32 table local
ns ip link set lo up
ns ip addr add 192.0.2.2/24 dev dp-peer
ns ip link set dp-peer up
ns ip route add default via 192.0.2.1
fi
am instrument -w -e probe tesla-checks -e checksInterface "$iface" com.dashplay.android.test/com.dashplay.android.CertificateNetworkProbe > /data/local/tmp/tesla-check-probe-result &
probe_pid=$!
count=0
while [ ! -f "$base/ready" ] && [ "$count" -lt 30 ]; do count=$((count + 1)); sleep 1; done
[ -f "$base/ready" ] || { cat /data/local/tmp/tesla-check-probe-result; exit 3; }
run_script "$base/install.sh"
run_script "$base/verify.sh"
if [ "$mode" = kernel ]; then
for label in check own forward; do
    # Requests originate in the isolated client and cross PREROUTING, not loopback.
    { cat "$base/$label.query"; sleep 2; } | ns timeout 4 nc -u -w 3 192.0.2.1 53 > "$base/$label.udp-actual" || true
    cmp "$base/$label.response" "$base/$label.udp-actual"
    { cat "$base/$label.tcp-query"; sleep 1; } | ns nc -w 3 192.0.2.1 53 > "$base/$label.tcp-actual"
    cmp "$base/$label.tcp-response" "$base/$label.tcp-actual"
    echo "KERNEL_DNS_OK $label UDP+TCP"
done
{ printf 'GET / HTTP/1.1\r\nHost: connman.vn.cloud.tesla.cn\r\n\r\n'; sleep 1; } | ns nc -w 3 100.96.0.2 80 > "$base/connman.response"
grep -q 'X-ConnMan-Status: online' "$base/connman.response"
{ printf 'GET /generate_204 HTTP/1.1\r\nHost: connectivitycheck.gstatic.com\r\n\r\n'; sleep 1; } | ns nc -w 3 100.96.0.2 80 > "$base/gstatic.response"
grep -q 'HTTP/1.1 204 No Content' "$base/gstatic.response"
{ printf 'GET / HTTP/1.1\r\nHost: unrelated.example.org\r\n\r\n'; sleep 1; } | ns nc -w 3 100.96.0.2 80 > "$base/other.response"
grep -q 'HTTP/1.1 404 Not Found' "$base/other.response"
echo 'KERNEL_CHECK_HTTP_OK connman/204; unrelated Host rejected'
fi
rm "$base/lease"
run_script "$base/remove.sh"
run_script "$base/remove.sh"
[ "$(iptables -t nat -S)" = "$before" ]
if run_script "$base/install.sh"; then echo 'revoked lease installed rules'; exit 1; fi
[ "$(iptables -t nat -S)" = "$before" ]
: > "$base/done"
wait "$probe_pid"
probe_pid=
cat /data/local/tmp/tesla-check-probe-result
grep -q 'success=true' /data/local/tmp/tesla-check-probe-result
if [ "$mode" = rules ]; then
    echo 'TESLA_CHECKS_RULES_OK production install/verify/revoke/cleanup; no independent client traffic tested'
else
    echo 'TESLA_CHECKS_KERNEL_OK production sockets and NAT; isolated upstream fixture; prior rules restored'
fi
