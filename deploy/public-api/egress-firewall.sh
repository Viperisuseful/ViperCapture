#!/bin/sh
set -eu

CHAIN=VIPERCAPTURE_EGRESS
SOURCE_CIDR=${VIPERCAPTURE_DOCKER_CIDR:-172.30.0.0/24}

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
for destination in \
  0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 \
  169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16 \
  198.18.0.0/15 224.0.0.0/4 240.0.0.0/4
do
  iptables -A "$CHAIN" -d "$destination" -j REJECT
done
iptables -A "$CHAIN" -j RETURN
iptables -C DOCKER-USER -s "$SOURCE_CIDR" -j "$CHAIN" 2>/dev/null || \
  iptables -I DOCKER-USER 1 -s "$SOURCE_CIDR" -j "$CHAIN"

echo "installed $CHAIN for $SOURCE_CIDR"
iptables -S "$CHAIN"
