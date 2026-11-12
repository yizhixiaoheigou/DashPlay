#!/usr/bin/env sh
# DashPlay DNSPod hook. Credentials remain in DP_Id / DP_Key, never argv.
dns_dp_add() {
  python3 "${DASHPLAY_DNS_HELPER:?}" dnspod add "$1" "$2"
}
dns_dp_rm() {
  python3 "${DASHPLAY_DNS_HELPER:?}" dnspod rm "$1" "$2"
}
