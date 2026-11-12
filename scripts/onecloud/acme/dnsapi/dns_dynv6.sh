#!/usr/bin/env sh
# DashPlay dynv6 HTTP hook. Credentials remain in DYNV6_TOKEN, never argv.
dns_dynv6_add() {
  python3 "${DASHPLAY_DNS_HELPER:?}" dynv6 add "$1" "$2"
}
dns_dynv6_rm() {
  python3 "${DASHPLAY_DNS_HELPER:?}" dynv6 rm "$1" "$2"
}
