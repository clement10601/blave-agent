#!/bin/bash
# GCE startup script for the Yuanta egress VM (runs as root on every boot, idempotent).
# WireGuard server on UDP 51820; forwards ONLY to the destinations listed in the
# instance metadata `wg-allowed-dsts` (space-separated CIDRs) and masquerades them
# behind the VM's static IP. The server private key is generated here and never
# leaves the VM; its public key is published as guest attribute wg/server-pubkey.
set -euo pipefail
MD=http://metadata.google.internal/computeMetadata/v1
md() { curl -fsS -H Metadata-Flavor:Google "$MD/$1"; }

if ! command -v wg >/dev/null; then
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y wireguard-tools iptables
fi
echo net.ipv4.ip_forward=1 > /etc/sysctl.d/99-wg-forward.conf
sysctl -q -p /etc/sysctl.d/99-wg-forward.conf

umask 077
mkdir -p /etc/wireguard
[ -s /etc/wireguard/server.key ] || wg genkey > /etc/wireguard/server.key
wg pubkey < /etc/wireguard/server.key > /etc/wireguard/server.pub

CLIENT_PUB=$(md instance/attributes/wg-client-pubkey)
DSTS=$(md instance/attributes/wg-allowed-dsts)
NIC=$(ip route show default | awk '{print $5; exit}')

{
    echo "[Interface]"
    echo "Address = 10.99.0.1/30"
    echo "ListenPort = 51820"
    echo "PrivateKey = $(cat /etc/wireguard/server.key)"
    echo "PostUp = iptables -t nat -A POSTROUTING -s 10.99.0.0/30 -o $NIC -j MASQUERADE"
    echo "PostUp = iptables -A FORWARD -o wg0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
    for d in $DSTS; do echo "PostUp = iptables -A FORWARD -i wg0 -o $NIC -d $d -j ACCEPT"; done
    echo "PostUp = iptables -A FORWARD -i wg0 -j DROP"
    echo "PostDown = iptables -t nat -D POSTROUTING -s 10.99.0.0/30 -o $NIC -j MASQUERADE"
    echo "PostDown = iptables -D FORWARD -o wg0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
    for d in $DSTS; do echo "PostDown = iptables -D FORWARD -i wg0 -o $NIC -d $d -j ACCEPT"; done
    echo "PostDown = iptables -D FORWARD -i wg0 -j DROP"
    echo
    echo "[Peer]"
    echo "PublicKey = $CLIENT_PUB"
    echo "AllowedIPs = 10.99.0.2/32"
} > /etc/wireguard/wg0.conf.new

if ! cmp -s /etc/wireguard/wg0.conf.new /etc/wireguard/wg0.conf; then
    mv /etc/wireguard/wg0.conf.new /etc/wireguard/wg0.conf
    systemctl enable wg-quick@wg0
    systemctl restart wg-quick@wg0
else
    rm /etc/wireguard/wg0.conf.new
    systemctl enable --now wg-quick@wg0
fi

curl -fsS -X PUT --data "$(cat /etc/wireguard/server.pub)" -H Metadata-Flavor:Google \
    "$MD/instance/guest-attributes/wg/server-pubkey"
