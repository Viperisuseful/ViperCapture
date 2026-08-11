#!/bin/sh
set -eu

CHAIN=VIPERCAPTURE_EGRESS
RENDERER_CIDR=${VIPERCAPTURE_RENDERER_CIDR:-172.30.0.10/32}

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 1
fi

command -v iptables >/dev/null 2>&1 || {
  echo "iptables is required" >&2
  exit 1
}

iptables -N "$CHAIN" 2>/dev/null || true
iptables -F "$CHAIN"
iptables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
for destination in \
  0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 \
  169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16 \
  198.18.0.0/15 224.0.0.0/4 240.0.0.0/4
do
  iptables -A "$CHAIN" -d "$destination" -j REJECT
done
iptables -A "$CHAIN" -j RETURN
for source in 172.30.0.0/24 "$RENDERER_CIDR"; do
  while iptables -C DOCKER-USER -s "$source" -j "$CHAIN" 2>/dev/null; do
    iptables -D DOCKER-USER -s "$source" -j "$CHAIN"
  done
done
iptables -I DOCKER-USER 1 -s "$RENDERER_CIDR" -j "$CHAIN"

echo "installed $CHAIN for renderer $RENDERER_CIDR"
iptables -S "$CHAIN"
