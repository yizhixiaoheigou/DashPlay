#!/system/bin/sh
# Disposable emulator only. Isolated client namespace sends real TCP through the production NAT rules.
set -eu
case "$(getprop ro.kernel.qemu)" in 1) ;; *) echo 'emulator required' >&2; exit 2;; esac
base=/data/local/tmp/dashplay-tesla-root
namespace_pid=
server_pid=
cleanup() {
    rm -f "$base/lease"
    sh "$base/remove.sh" >/dev/null 2>&1 || true
    [ -z "$server_pid" ] || kill "$server_pid" 2>/dev/null || true
    [ -z "$namespace_pid" ] || kill "$namespace_pid" 2>/dev/null || true
    ip link del dp-tesla0 2>/dev/null || true
    ip rule del pref 9000 from 192.0.2.1/32 to 192.0.2.0/24 lookup main 2>/dev/null || true
    ip route del blackhole 100.96.0.1/32 table local 2>/dev/null || true
}
trap cleanup EXIT
before=$(iptables -w 2 -t nat -S)
unshare -n sh -c 'echo $$ > /data/local/tmp/dashplay-tesla-root/ns.pid; sleep 90' >/dev/null 2>&1 &
for count in 1 2 3 4 5; do [ -s "$base/ns.pid" ] && break; sleep 1; done
namespace_pid=$(cat "$base/ns.pid")
ns() { nsenter --net="/proc/$namespace_pid/ns/net" -- "$@"; }
ip link add dp-tesla0 type veth peer name dp-peer
ip link set dp-peer netns "$namespace_pid"
ip addr add 192.0.2.1/24 dev dp-tesla0
ip link set dp-tesla0 up
ip rule add pref 9000 from 192.0.2.1/32 to 192.0.2.0/24 lookup main
ip route add blackhole 100.96.0.1/32 table local
ns ip link set lo up
ns ip addr add 192.0.2.2/24 dev dp-peer
ns ip link set dp-peer up
ns ip route add default via 192.0.2.1
printf 'DP_TESLA_012345abcdef' > "$base/lease"
sh "$base/install.sh"
for port in 443 18080; do
    { printf 'dashplay-root-kernel-ok\n'; sleep 3; } | nc -l -p 18080 -s 192.0.2.1 -w 8 > "$base/request-$port" &
    server_pid=$!
    sleep 1
    { printf 'kernel-client\n'; sleep 2; } | ns nc -w 5 100.96.0.1 "$port" > "$base/response-$port"
    grep -q '^dashplay-root-kernel-ok' "$base/response-$port"
    wait "$server_pid"
    server_pid=
    echo "KERNEL_REDIRECT_OK port=$port"
done
rm "$base/lease"
sh "$base/remove.sh"
sh "$base/remove.sh"
[ "$(iptables -w 2 -t nat -S)" = "$before" ]
# A delayed installation without its still-current lease must not recreate the removed chain.
if sh "$base/install.sh"; then echo 'cancelled install unexpectedly succeeded' >&2; exit 1; fi
[ "$(iptables -w 2 -t nat -S)" = "$before" ]
echo 'KERNEL_CLEANUP_OK prior rules unchanged; repeat cleanup and revoked lease verified'
