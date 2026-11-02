#!/system/bin/sh
# Two isolated namespaces: actual client -> Android kernel policy/SNAT -> synthetic Ethernet Internet peer.
# The upstream veth stands in for TUN only in this routing-layer test; no real USB, VPN or iPhone claim.
set -eu
[ "$(getprop ro.kernel.qemu)" = 1 ] || exit 2
base=/data/local/tmp/dashplay-usb-share
client_pid=
peer_pid=
server_pid=
cleanup() {
    rm -f "$base/lease"
    sh "$base/remove.sh" >/dev/null 2>&1 || true
    [ -z "$server_pid" ] || kill "$server_pid" 2>/dev/null || true
    [ -z "$client_pid" ] || kill "$client_pid" 2>/dev/null || true
    [ -z "$peer_pid" ] || kill "$peer_pid" 2>/dev/null || true
    ip link del dp-ap0 2>/dev/null || true
    ip link del dp-usb0 2>/dev/null || true
    ip rule del pref 8500 from 192.0.2.1/32 to 192.0.2.0/24 lookup main 2>/dev/null || true
}
trap cleanup EXIT
unshare -n sh -c 'echo $$ > /data/local/tmp/dashplay-usb-share/client.pid; sleep 180' >/dev/null 2>&1 &
unshare -n sh -c 'echo $$ > /data/local/tmp/dashplay-usb-share/peer.pid; sleep 180' >/dev/null 2>&1 &
for n in 1 2 3 4 5; do [ -s "$base/client.pid" ] && [ -s "$base/peer.pid" ] && break; sleep 1; done
client_pid=$(cat "$base/client.pid"); peer_pid=$(cat "$base/peer.pid")
client() { nsenter --net="/proc/$client_pid/ns/net" -- "$@"; }
peer() { nsenter --net="/proc/$peer_pid/ns/net" -- "$@"; }
ip link add dp-ap0 type veth peer name dp-client
ip link set dp-client netns "$client_pid"
ip addr add 192.0.2.1/24 dev dp-ap0
ip link set dp-ap0 up
client ip link set lo up
client ip addr add 192.0.2.2/24 dev dp-client
client ip link set dp-client up
client ip route add default via 192.0.2.1
ip link add dp-usb0 type veth peer name dp-peer
ip link set dp-peer netns "$peer_pid"
ip addr add 192.0.0.2/32 dev dp-usb0
ip link set dp-usb0 up
peer ip link set lo up
peer ip addr add 198.51.100.77/32 dev lo
peer ip addr add 192.0.0.1/32 dev dp-peer
peer ip link set dp-peer up
# Only the translated source can receive a reply; there is deliberately no route to the client's LAN.
peer ip route add 192.0.0.2/32 dev dp-peer
printf '1' > /proc/sys/net/ipv4/conf/dp-ap0/forwarding
printf '1' > /proc/sys/net/ipv4/conf/dp-usb0/forwarding
ip rule add pref 8500 from 192.0.2.1/32 to 192.0.2.0/24 lookup main
before_nat=$(iptables -w 2 -t nat -S)
before_filter=$(iptables -w 2 -t filter -S)
before_rules=$(ip -4 rule show)
before_global=$(cat /proc/sys/net/ipv4/ip_forward)
printf '%s\n' "$before_nat" > "$base/before-nat"
printf '%s\n' "$before_filter" > "$base/before-filter"
printf '%s\n' "$before_rules" > "$base/before-rules"
printf '%s\n' "$before_global" > "$base/before-global"
printf 'DP_USB_012345abcdef' > "$base/lease"
sh "$base/install.sh"
echo 'SHARE_ROUTES_INSTALLED'
ip -4 route get 198.51.100.77 from 192.0.2.2 iif dp-ap0
{ printf 'share-tcp-reply\n'; sleep 3; } | peer nc -q 1 -W 8 -l -p 19000 -s 198.51.100.77 -w 8 > "$base/tcp-request" &
server_pid=$!
sleep 1
{ printf 'share-tcp-request\n'; sleep 2; } | client nc -q 1 -W 5 -w 5 198.51.100.77 19000 > "$base/tcp-response"
grep -q '^share-tcp-reply' "$base/tcp-response"
wait "$server_pid"; server_pid=
grep -q '^share-tcp-request' "$base/tcp-request"
echo 'SHARE_TCP_SNAT_OK independent client and peer; peer has no LAN return route'
for protocol in udp tcp; do
    flag=
    [ "$protocol" != udp ] || flag=-u
    { printf "share-dns-$protocol-reply\n"; sleep 3; } | peer nc $flag -q 1 -W 8 -l -p 53 -s 192.0.0.1 -w 8 > "$base/dns-$protocol-request" &
    server_pid=$!
    sleep 1
    { printf "share-dns-$protocol-request\n"; sleep 2; } | client nc $flag -q 1 -W 5 -w 5 192.0.2.1 53 > "$base/dns-$protocol-response"
    grep -q "^share-dns-$protocol-reply" "$base/dns-$protocol-response"
    wait "$server_pid"; server_pid=
    grep -q "^share-dns-$protocol-request" "$base/dns-$protocol-request"
    echo "SHARE_DNS_REDIRECT_OK protocol=$protocol synthetic payload, actual port-53 DNAT/SNAT"
done
# Local management must remain local, not redirected through the Internet table.
{ printf 'share-local-page\n'; sleep 3; } | nc -q 1 -W 8 -l -p 18080 -s 192.0.2.1 -w 8 > "$base/local-request" &
server_pid=$!
sleep 1
{ printf 'share-local-client\n'; sleep 2; } | client nc -q 1 -W 5 -w 5 192.0.2.1 18080 > "$base/local-response"
grep -q '^share-local-page' "$base/local-response"
wait "$server_pid"; server_pid=
echo 'SHARE_LOCAL_PAGE_OK'
rm "$base/lease"
sh "$base/remove.sh"
sh "$base/remove.sh"
iptables -w 2 -t nat -S > "$base/after-nat"
iptables -w 2 -t filter -S > "$base/after-filter"
ip -4 rule show > "$base/after-rules"
cat /proc/sys/net/ipv4/ip_forward > "$base/after-global"
for state in nat filter rules global; do
    if ! cmp -s "$base/before-$state" "$base/after-$state"; then echo "SHARE_CLEANUP_MISMATCH $state"; exit 1; fi
done
if sh "$base/install.sh"; then echo 'revoked lease unexpectedly installed'; exit 1; fi
[ "$(iptables -w 2 -t nat -S)" = "$before_nat" ]
echo 'SHARE_CLEANUP_OK firewall and policies restored, global forwarding untouched, revoked lease rejected'
