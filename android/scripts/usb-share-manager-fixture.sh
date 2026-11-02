#!/system/bin/sh
# Disposable emulator only. Independent client; the app's real managers own every sharing/Tesla rule.
set -eu
[ "$(getprop ro.kernel.qemu)" = 1 ] || exit 2
crash=${1:-false}
late=${2:-false}
base=/data/local/tmp/dashplay-share-manager
app=cache/usb-share-manager
mkdir -p "$base"
echo $$ > "$base/runner.pid"
client_pid=
cleanup() {
    [ -z "$client_pid" ] || kill "$client_pid" 2>/dev/null || true
    ndc network route remove 99 dp-ap0 192.0.2.0/24 >/dev/null 2>&1 || true
    ndc network interface remove 99 dp-ap0 >/dev/null 2>&1 || true
    ip link del dp-ap0 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 143' INT TERM
unshare -n sh -c 'echo $$ > /data/local/tmp/dashplay-share-manager/client.pid; sleep 420' >/dev/null 2>&1 &
for n in 1 2 3 4 5; do [ -s "$base/client.pid" ] && break; sleep 1; done
client_pid=$(cat "$base/client.pid")
client() { nsenter --net="/proc/$client_pid/ns/net" -- "$@"; }
ip link add dp-ap0 type veth peer name dp-client
ip link set dp-client netns "$client_pid"
ip addr add 192.0.2.1/24 dev dp-ap0
ip link set dp-ap0 up
# Model the local-network registration performed by Android IpServer for hotspot/P2P interfaces.
# A naked veth has no Android local-network reply route after sharing is removed.
ndc network interface add 99 dp-ap0 > "$base/netd-interface"
grep -q '^200 ' "$base/netd-interface"
ndc network route add 99 dp-ap0 192.0.2.0/24 > "$base/netd-route"
grep -q '^200 ' "$base/netd-route"
printf '0' > /proc/sys/net/ipv4/conf/dp-ap0/forwarding
client ip link set lo up
client ip addr add 192.0.2.2/24 dev dp-client
client ip link set dp-client up
client ip route add default via 192.0.2.1
stable_rules() {
    stable=0; attempts=0; previous=$(ip -4 rule show)
    while [ "$stable" -lt 3 ]; do
        sleep 2; current=$(ip -4 rule show)
        if [ "$current" = "$previous" ]; then stable=$((stable + 1)); else stable=0; fi
        previous=$current; attempts=$((attempts + 1)); [ "$attempts" -lt 20 ] || exit 1
    done
}
numeric_rules() {
    # Android's bundled ip has no -N. Resolve only lookup aliases using netd's
    # contemporaneous mapping, retaining raw output and the mapping as evidence.
    prefix=$1
    cat /data/misc/net/rt_tables > "$base/$prefix-table-names"
    ip -4 rule show > "$base/$prefix-rules-raw"
    cmp /data/misc/net/rt_tables "$base/$prefix-table-names"
    cp "$base/$prefix-rules-raw" "$base/$prefix-rules"
    while read -r number label rest; do
        case "$number" in ''|\#*) continue;; *[!0-9]*) exit 1;; esac
        case "$label" in ''|*[!A-Za-z0-9_-]*) exit 1;; esac
        sed "s/lookup $label /lookup $number /g" "$base/$prefix-rules" > "$base/$prefix-rules.tmp"
        mv "$base/$prefix-rules.tmp" "$base/$prefix-rules"
    done < "$base/$prefix-table-names"
    ! grep -E 'lookup [A-Za-z_]' "$base/$prefix-rules"
}
stable_rules
# Both normal close and process death must restore the pre-VPN machine state.
# The later 'before' snapshot includes system bandwidth rules for the live VPN;
# it remains the correct baseline only for sharing cleanup while that VPN lives.
numeric_rules pre-vpn
iptables -w 2 -t nat -S > "$base/pre-vpn-nat"
iptables -w 2 -t filter -S > "$base/pre-vpn-filter"
ip6tables -w 2 -t filter -S > "$base/pre-vpn-filter6"
cat /proc/sys/net/ipv4/conf/dp-ap0/forwarding > "$base/pre-vpn-ap-forwarding"
touch "$base/prepared"
wait_file() {
    count=0
    until run-as com.dashplay.android test -f "$app/$1"; do
        count=$((count + 1)); [ "$count" -lt 90 ] || { echo "MANAGER_WAIT_FAILED $1"; exit 1; }
        sleep 1
    done
}
wait_phase() {
    count=0
    until [ "$(run-as com.dashplay.android cat "$app/phase" 2>/dev/null)" = "$1" ]; do
        count=$((count + 1)); [ "$count" -lt 90 ] || { echo "MANAGER_PHASE_FAILED $1"; exit 1; }
        sleep 1
    done
}
done_phase() { run-as com.dashplay.android touch "$app/done-$1"; }
wait_file tunnel
tunnel=$(run-as com.dashplay.android cat "$app/tunnel")
case "$tunnel" in tun[0-9]*) ;; *) exit 1;; esac
stable_rules
snapshot() {
    iptables -w 2 -t nat -S > "$base/$1-nat"
    iptables -w 2 -t filter -S > "$base/$1-filter"
    ip6tables -w 2 -t filter -S > "$base/$1-filter6"
    ip -4 rule show > "$base/$1-rules"
    cat /proc/sys/net/ipv4/conf/dp-ap0/forwarding > "$base/$1-ap-forwarding"
    cat "/proc/sys/net/ipv4/conf/$tunnel/forwarding" > "$base/$1-tun-forwarding"
}
snapshot before
run-as com.dashplay.android touch "$app/baseline"
wait_phase shared
for name in forward check; do
    for suffix in query response tcp-query tcp-response; do
        run-as com.dashplay.android cat "$app/$name.$suffix" > "$base/$name.$suffix"
    done
done
echo_test() {
    printf 'manager-client-through-vpn\n' > "$base/echo-request"
    { cat "$base/echo-request"; sleep 1; } | client nc -q 1 -W 5 -w 5 198.51.100.77 49002 > "$base/echo-result"
    cmp "$base/echo-request" "$base/echo-result"
}
dns_test() {
    name=$1; proto=$2; flag=; suffix=tcp-
    [ "$proto" != udp ] || { flag=-u; suffix=; }
    { cat "$base/$name.${suffix}query"; sleep 1; } | client nc $flag -q 1 -W 5 -w 5 192.0.2.1 53 > "$base/dns-result"
    cmp "$base/$name.${suffix}response" "$base/dns-result"
}
tesla_test() {
    printf 'manager-alias\n' > "$base/alias-request"
    { cat "$base/alias-request"; sleep 1; } | client nc -q 1 -W 5 -w 5 100.96.0.1 443 > "$base/alias-result"
    cmp "$base/alias-request" "$base/alias-result"
    { printf 'GET /generate_204 HTTP/1.1\r\nHost: connectivitycheck.gstatic.com\r\nConnection: close\r\n\r\n'; sleep 1; } |
        client nc -q 1 -W 5 -w 5 100.96.0.2 80 > "$base/http-check-result"
    grep -q 'HTTP/1.1 204 No Content' "$base/http-check-result"
    dns_test check udp; dns_test check tcp
}
crash_test() {
    wait_phase crash-ready
    pid=$(run-as com.dashplay.android cat "$app/crash-pid")
    case "$pid" in ''|*[!0-9]*) exit 1;; esac
    [ "$(pidof com.dashplay.android)" = "$pid" ]
    echo "$pid" > "$base/old-pid"
    run-as com.dashplay.android cat shared_prefs/dashplay.xml > "$base/crash-before-prefs.xml"
    run-as com.dashplay.android cat "$app/table" > "$base/last-table"
    run-as com.dashplay.android cat "$app/owner" > "$base/crash-owner"
    if [ "$late" = true ]; then
        # Capture the exact command that the production manager sent to Root while alive.
        cp "$base/root-request.sh" "$base/late-tesla-install.sh"
        grep -q DASHPLAY_ROOT_ACTIVE "$base/late-tesla-install.sh"
        run-as com.dashplay.android cat "$app/late-tesla-remove.sh" > "$base/late-tesla-remove.sh"
    fi
    am force-stop com.dashplay.android
    count=0
    while kill -0 "$pid" 2>/dev/null || ip link show "$tunnel" >/dev/null 2>&1; do
        count=$((count + 1)); [ "$count" -lt 30 ] || exit 1; sleep 1
    done
    [ -z "$(pidof com.dashplay.android)" ]
    for name in usb-share-lease tesla-root-lease; do run-as com.dashplay.android test -f "files/$name"; done
    run-as com.dashplay.android cat shared_prefs/dashplay.xml > "$base/crash-dead-prefs.xml"
    cmp "$base/crash-before-prefs.xml" "$base/crash-dead-prefs.xml"
    for name in usb_share_journal tesla_root_journal; do grep -q "name=\"$name\"" "$base/crash-dead-prefs.xml"; done
    iptables -w 2 -t nat -S > "$base/crash-dead-nat"
    grep -q "$(cat "$base/crash-owner")" "$base/crash-dead-nat"
    grep -q 'DP_TESLA_' "$base/crash-dead-nat"
    [ "$(cat /proc/sys/net/ipv4/conf/dp-ap0/forwarding)" = 1 ]
    ip -4 route get 198.51.100.77 > "$base/crash-dead-other-uplink"
    grep -E 'dev (wlan0|eth0)' "$base/crash-dead-other-uplink"
    if ip -4 route get 198.51.100.77 from 192.0.2.2 iif dp-ap0 > "$base/crash-dead-route" 2>&1; then
        echo 'dead process leaked to another uplink'; exit 1
    fi
    { cat "$base/echo-request"; sleep 1; } | client nc -q 1 -W 2 -w 2 198.51.100.77 49002 > "$base/crash-dead-echo" || true
    [ ! -s "$base/crash-dead-echo" ]
    echo 'MANAGER_PROCESS_DEAD_TUN_GONE_GUARD_OK'
    if [ "$late" = true ]; then
        # Model a delayed second copy of the old request after its original rules were removed.
        # Keep the dead process's lease and journal intact: normal cancellation did not run.
        sh "$base/late-tesla-remove.sh" > "$base/late-remove-result"
        iptables -w 2 -t nat -S > "$base/late-before-nat"
        if sh "$base/late-tesla-install.sh" > "$base/late-install-result" 2>&1; then
            echo 'LATE_ROOT_ACCEPTED_DEAD_PROCESS'; exit 1
        fi
        iptables -w 2 -t nat -S > "$base/late-after-nat"
        cmp "$base/late-before-nat" "$base/late-after-nat"
        echo 'LATE_ROOT_DEAD_PROCESS_REJECTED_WITHOUT_MUTATION'
    fi
    touch "$base/crash-dead-ready"
    wait_phase crash-recovered
    new_pid=$(run-as com.dashplay.android cat "$app/recovered-pid")
    [ "$new_pid" != "$pid" ] && [ "$(pidof com.dashplay.android)" = "$new_pid" ]
    echo "$new_pid" > "$base/new-pid"
    stable_rules
    iptables -w 2 -t nat -S > "$base/crash-final-nat"
    iptables -w 2 -t filter -S > "$base/crash-final-filter"
    ip6tables -w 2 -t filter -S > "$base/crash-final-filter6"
    # Numeric table IDs avoid netd's table-name aliases changing after VPN teardown.
    numeric_rules crash-final
    cat /proc/sys/net/ipv4/conf/dp-ap0/forwarding > "$base/crash-final-ap-forwarding"
    for state in nat filter filter6 ap-forwarding; do cmp "$base/pre-vpn-$state" "$base/crash-final-$state"; done
    cmp "$base/pre-vpn-rules" "$base/crash-final-rules"
    table=$(cat "$base/last-table")
    ! ip -4 route show table all | grep -E " table $table( |$)"
    run-as com.dashplay.android cat shared_prefs/dashplay.xml > "$base/crash-final-prefs.xml"
    for name in usb_share_journal tesla_root_journal; do ! grep -q "name=\"$name\"" "$base/crash-final-prefs.xml"; done
    for name in usb-share-lease tesla-root-lease; do ! run-as com.dashplay.android test -f "files/$name"; done
    echo 'MANAGER_PROCESS_RESTART_EXACT_CLEANUP_OK'
    done_phase crash-recovered
}
for phase in shared tesla-last share-last recovered new-manager; do
    wait_phase "$phase"
    echo_test; dns_test forward udp; dns_test forward tcp
    [ "$phase" = shared ] || tesla_test
    echo "MANAGER_CLIENT_OK $phase"
    done_phase "$phase"
    if [ "$crash" = true ] && [ "$phase" = tesla-last ]; then crash_test; exit 0; fi
done
wait_phase tesla-only
tesla_test; dns_test forward udp; dns_test forward tcp
echo 'MANAGER_TESLA_SURVIVES_SHARE_CLOSE_OK'
done_phase tesla-only
wait_phase clean
snapshot after
for state in nat filter filter6 rules ap-forwarding tun-forwarding; do cmp "$base/before-$state" "$base/after-$state"; done
echo 'MANAGER_EXACT_CLEANUP_OK'
done_phase clean
wait_phase before-vpn-close
echo_test; dns_test forward tcp
run-as com.dashplay.android cat "$app/table" > "$base/last-table"
done_phase before-vpn-close
wait_phase vpn-closed
if ip -4 route get 198.51.100.77 from 192.0.2.2 iif dp-ap0 > "$base/closed-tun-route" 2>&1; then
    echo 'closed VPN leaked to another uplink'; exit 1
fi
{ cat "$base/echo-request"; sleep 1; } | client nc -q 1 -W 2 -w 2 198.51.100.77 49002 > "$base/closed-tun-echo" || true
[ ! -s "$base/closed-tun-echo" ]
echo 'MANAGER_ACTUAL_VPN_CLOSE_BLOCK_OK'
done_phase vpn-closed
wait_phase vpn-clean
stable_rules
iptables -w 2 -t nat -S > "$base/final-nat"
iptables -w 2 -t filter -S > "$base/final-filter"
ip6tables -w 2 -t filter -S > "$base/final-filter6"
numeric_rules final
for state in nat filter filter6 rules; do cmp "$base/pre-vpn-$state" "$base/final-$state"; done
table=$(cat "$base/last-table")
! ip -4 rule show | grep -E "lookup $table( |$)"
! ip -4 route show table all | grep -E " table $table( |$)"
cat /proc/sys/net/ipv4/conf/dp-ap0/forwarding > "$base/final-ap-forwarding"
cmp "$base/pre-vpn-ap-forwarding" "$base/final-ap-forwarding"
echo 'MANAGER_VPN_CLOSE_CLEANUP_OK'
done_phase vpn-clean
