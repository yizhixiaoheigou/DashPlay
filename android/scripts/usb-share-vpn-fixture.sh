#!/system/bin/sh
# Disposable emulator: a real independent LAN client uses the app's real VPN and synthetic USB peer.
set -eu
[ "$(getprop ro.kernel.qemu)" = 1 ] || exit 2
base=/data/local/tmp/dashplay-shared-vpn
app=cache/usb-share-probe
mkdir -p "$base"
echo $$ > "$base/runner.pid"
client_pid=
ipv6_forwarding=$(cat /proc/sys/net/ipv6/conf/all/forwarding)
cleanup() {
    rm -f "$base/lease"
    [ ! -f "$base/remove.sh" ] || sh "$base/remove.sh" >/dev/null 2>&1 || true
    [ -z "$client_pid" ] || kill "$client_pid" 2>/dev/null || true
    ip -6 rule del pref 8998 from fd00:2::/64 iif dp-ap0 lookup 41888 2>/dev/null || true
    ip -6 route del 2001:db8::/64 dev dp-v6sink table 41888 2>/dev/null || true
    ip link del dp-v6sink 2>/dev/null || true
    ip link del dp-ap0 2>/dev/null || true
    printf '%s' "$ipv6_forwarding" > /proc/sys/net/ipv6/conf/all/forwarding
}
trap cleanup EXIT
trap 'exit 143' INT TERM
unshare -n sh -c 'echo $$ > /data/local/tmp/dashplay-shared-vpn/client.pid; sleep 240' >/dev/null 2>&1 &
for n in 1 2 3 4 5; do [ -s "$base/client.pid" ] && break; sleep 1; done
client_pid=$(cat "$base/client.pid")
client() { nsenter --net="/proc/$client_pid/ns/net" -- "$@"; }
ip link add dp-ap0 type veth peer name dp-client
ip link set dp-client netns "$client_pid"
ip addr add 192.0.2.1/24 dev dp-ap0
ip link set dp-ap0 up
printf '0' > /proc/sys/net/ipv4/conf/dp-ap0/forwarding
client ip link set lo up
client ip addr add 192.0.2.2/24 dev dp-client
client ip link set dp-client up
client ip route add default via 192.0.2.1
# A test-only IPv6 output path makes a FORWARD rejection observable independently of upstream reachability.
ip -6 addr add fd00:2::1/64 dev dp-ap0 nodad
client ip -6 addr add fd00:2::2/64 dev dp-client nodad
client ip -6 route add default via fd00:2::1
printf '1' > /proc/sys/net/ipv6/conf/dp-ap0/forwarding
# This disposable test router needs IPv6 forwarding enabled to reach the filter hook.
# Production sharing never toggles this global setting; cleanup restores the emulator's original value.
printf '1' > /proc/sys/net/ipv6/conf/all/forwarding
ip link add dp-v6sink type dummy
ip link set dp-v6sink up
ip -6 route add 2001:db8::/64 dev dp-v6sink table 41888
ip -6 rule add pref 8998 from fd00:2::/64 iif dp-ap0 lookup 41888
touch "$base/prepared"
wait_app() {
    count=0
    until run-as com.dashplay.android test -f "$app/$1"; do
        count=$((count + 1)); [ "$count" -lt 120 ] || { echo "APP_WAIT_FAILED $1"; exit 1; }
        sleep 1
    done
}
wait_phase() {
    count=0
    until [ "$(run-as com.dashplay.android cat "$app/phase" 2>/dev/null)" = "$1" ]; do
        count=$((count + 1)); [ "$count" -lt 30 ] || { echo "PHASE_WAIT_FAILED $1"; exit 1; }
        sleep 1
    done
}
wait_app tunnel
run-as com.dashplay.android cat "$app/tunnel" > "$base/tunnel"
tunnel=$(cat "$base/tunnel")
case "$tunnel" in tun[0-9]*) ;; *) echo 'unexpected VPN interface'; exit 1;; esac
printf 'DASHPLAY_SHARE_LINKS %s %s %s %s %s\n' "$(cat /sys/class/net/dp-ap0/ifindex)" "$(cat /sys/class/net/$tunnel/ifindex)" "$(cat /proc/sys/net/ipv4/conf/dp-ap0/forwarding)" "$(cat /proc/sys/net/ipv4/conf/$tunnel/forwarding)" "$(cat /proc/sys/kernel/random/boot_id)" | run-as com.dashplay.android sh -c "cat > $app/interfaces"
wait_app ready
for file in install.sh remove.sh verify.sh lose-route.sh restore-route.sh tunnel check.query check.response check.tcp-query check.tcp-response own.query own.response own.tcp-query own.tcp-response forward.query forward.response forward.tcp-query forward.tcp-response forward.offline-response; do
    run-as com.dashplay.android cat "$app/$file" > "$base/$file"
done
tunnel=$(cat "$base/tunnel")
case "$tunnel" in tun[0-9]*) ;; *) echo 'unexpected VPN interface'; exit 1;; esac
# Session installer must enable and later restore only these captured, test-owned interfaces.
cat /proc/sys/net/ipv4/conf/dp-ap0/forwarding > "$base/before-ap-forwarding"
cat "/proc/sys/net/ipv4/conf/$tunnel/forwarding" > "$base/before-tun-forwarding"
# Wait for netd's unrelated boot/validation work without weakening before/after equality checks.
stable=0; attempts=0; previous=$(ip -4 rule show)
while [ "$stable" -lt 3 ]; do
    sleep 2; current=$(ip -4 rule show)
    if [ "$current" = "$previous" ]; then stable=$((stable + 1)); else stable=0; fi
    previous=$current; attempts=$((attempts + 1)); [ "$attempts" -lt 20 ] || exit 1
done
iptables -w 2 -t nat -S > "$base/before-nat"
iptables -w 2 -t filter -S > "$base/before-filter"
ip6tables -w 2 -t filter -S > "$base/before-filter6"
ip -4 rule show > "$base/before-rules"
printf 'DP_USB_012345abcdef' > "$base/lease"
sh "$base/install.sh"
sh "$base/verify.sh"
echo_test() {
    flag=; port=49002
    [ "$1" != udp ] || { flag=-u; port=49001; }
    printf 'shared-client-through-real-vpn\n' > "$base/echo-request"
    { cat "$base/echo-request"; sleep 1; } | client nc $flag -q 1 -W 5 -w 5 198.51.100.77 "$port" > "$base/echo-$1"
    cmp "$base/echo-request" "$base/echo-$1"
    echo "SHARED_VPN_ECHO_OK $1"
}
dns_test() {
    label=$1; protocol=$2
    flag=; suffix=tcp-
    [ "$protocol" != udp ] || { flag=-u; suffix=; }
    { cat "$base/$label.${suffix}query"; sleep 1; } | client nc $flag -q 1 -W 5 -w 5 192.0.2.1 53 > "$base/$label-$protocol-actual"
    cmp "$base/$label.${suffix}response" "$base/$label-$protocol-actual"
    echo "SHARED_VPN_DNS_OK $label $protocol"
}
echo_test tcp; echo_test udp
for protocol in udp tcp; do for label in check own forward; do dns_test "$label" "$protocol"; done; done
run-as com.dashplay.android touch "$app/round1"
wait_phase offline
{ cat "$base/echo-request"; sleep 1; } | client nc -q 1 -W 2 -w 2 198.51.100.77 49002 > "$base/offline-echo" || true
[ ! -s "$base/offline-echo" ]
dns_test check udp
{ cat "$base/forward.query"; sleep 1; } | client nc -u -q 1 -W 5 -w 5 192.0.2.1 53 > "$base/offline-dns"
cmp "$base/forward.offline-response" "$base/offline-dns"
echo 'SHARED_VPN_OFFLINE_OK no echo; local check available; upstream DNS returns SERVFAIL'
run-as com.dashplay.android touch "$app/round2"
wait_phase recovered
echo_test tcp; echo_test udp; dns_test forward tcp
{ printf 'must-not-forward-ipv6'; sleep 1; } | client nc -6 -u -q 1 -W 2 -w 2 2001:db8::99 49001 >/dev/null || true
ip6tables -w 2 -t filter -L FORWARD -v -n -x > "$base/ipv6-counters"
grep DP_USB_012345abcdef "$base/ipv6-counters" | awk '$1 > 0 && $3 == "REJECT" && index($0, "dp-ap0") {found=1} END {exit !found}'
echo 'SHARED_VPN_IPV6_BLOCK_OK real independent client hit scoped FORWARD reject'
sh "$base/lose-route.sh"
if ip -4 route get 198.51.100.77 from 192.0.2.2 iif dp-ap0 > "$base/lost-route" 2>&1; then
    echo 'missing VPN route fell through to another uplink'; exit 1
fi
{ cat "$base/echo-request"; sleep 1; } | client nc -q 1 -W 2 -w 2 198.51.100.77 49002 > "$base/lost-route-echo" || true
[ ! -s "$base/lost-route-echo" ]
sh "$base/restore-route.sh"
sh "$base/verify.sh"
echo_test tcp
echo 'SHARED_VPN_ROUTE_LOSS_OK unreachable fallback prevented alternate uplink'
run-as com.dashplay.android touch "$app/round3"
rm "$base/lease"
sh "$base/remove.sh"; sh "$base/remove.sh"
iptables -w 2 -t nat -S > "$base/after-nat"
iptables -w 2 -t filter -S > "$base/after-filter"
ip6tables -w 2 -t filter -S > "$base/after-filter6"
cat /proc/sys/net/ipv4/conf/dp-ap0/forwarding > "$base/after-ap-forwarding"
cat "/proc/sys/net/ipv4/conf/$tunnel/forwarding" > "$base/after-tun-forwarding"
ip -4 rule show > "$base/after-rules"
for state in nat filter filter6 rules ap-forwarding tun-forwarding; do cmp "$base/before-$state" "$base/after-$state"; done
echo 'SHARED_VPN_CLEANUP_OK exact firewall and policy restoration with same live TUN'
run-as com.dashplay.android touch "$app/done"
