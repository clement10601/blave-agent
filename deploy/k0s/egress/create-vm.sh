#!/bin/bash
# Fixed egress IP for Yuanta UAT: a GCP e2-micro in asia-east1 (Taiwan) running WireGuard,
# plus the k0s-side client that routes ONLY the Yuanta addresses through it.
# Idempotent — re-running skips what exists. Run from the repo root:
#   bash deploy/k0s/egress/create-vm.sh [gcp-project]
# Cost: e2-micro (~US$6–7/mo) + static IP in use (~US$3.65/mo) + egress (tiny).
# Remove everything: bash deploy/k0s/egress/create-vm.sh --delete [gcp-project]
set -euo pipefail

DELETE=0; [ "${1:-}" = "--delete" ] && { DELETE=1; shift; }
P="${1:-$(gcloud config get-value project 2>/dev/null)}"
R=asia-east1; Z=asia-east1-b
VM=blave-egress; ADDR=blave-egress-ip; FW=blave-egress-wg; NS=blave-agent
# Yuanta endpoints routed through the VM (UAT 220.130.122.92 observed from the SDK's Open(UAT))
DSTS="${YUANTA_DSTS:-220.130.122.92/32}"
HERE="$(cd "$(dirname "$0")" && pwd)"
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

if [ "$DELETE" = 1 ]; then
    kubectl -n "$NS" delete deploy/blave-egress-wg secret/blave-egress-wg --ignore-not-found
    gcloud compute instances delete "$VM" --project "$P" --zone "$Z" --quiet || true
    gcloud compute firewall-rules delete "$FW" --project "$P" --quiet || true
    gcloud compute addresses delete "$ADDR" --project "$P" --region "$R" --quiet || true
    exit 0
fi

echo "GCP project: $P   region: $R   routed via VM: $DSTS"
read -rp "Create billable resources (~US\$10/month) in this project? [y/N] " yn
[[ "$yn" =~ ^[Yy] ]] || exit 1

WORK="$(mktemp -d)"; chmod 700 "$WORK"; trap 'rm -rf "$WORK"' EXIT
step "1/5 WireGuard client key (reused from the cluster Secret when it exists)"
if kubectl -n "$NS" get secret blave-egress-wg >/dev/null 2>&1; then
    kubectl -n "$NS" get secret blave-egress-wg -o jsonpath='{.data.wg0\.conf}' | base64 -d \
        | awk -F' = ' '/^PrivateKey/{print $2}' > "$WORK/client.key"
fi
if [ ! -s "$WORK/client.key" ]; then
    docker run --rm --entrypoint python3 local/blave-agent:v1 -c '
import base64
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization as s
print(base64.b64encode(X25519PrivateKey.generate().private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode())' > "$WORK/client.key"
fi
CLIENT_PUB=$(docker run --rm -i --entrypoint python3 local/blave-agent:v1 -c '
import base64, sys
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization as s
k = X25519PrivateKey.from_private_bytes(base64.b64decode(sys.stdin.read().strip()))
print(base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode())' < "$WORK/client.key")

step "2/5 Static IP + firewall (UDP 51820 only)"
gcloud compute addresses describe "$ADDR" --project "$P" --region "$R" >/dev/null 2>&1 \
    || gcloud compute addresses create "$ADDR" --project "$P" --region "$R" --network-tier PREMIUM
IP=$(gcloud compute addresses describe "$ADDR" --project "$P" --region "$R" --format='value(address)')
gcloud compute firewall-rules describe "$FW" --project "$P" >/dev/null 2>&1 \
    || gcloud compute firewall-rules create "$FW" --project "$P" --network default --direction INGRESS \
        --action ALLOW --rules udp:51820 --source-ranges 0.0.0.0/0 --target-tags "$VM" \
        --description "WireGuard to the Yuanta egress VM"

step "3/5 VM $VM (e2-micro, Debian 12)"
META="enable-guest-attributes=TRUE,enable-oslogin=TRUE,wg-client-pubkey=$CLIENT_PUB,wg-allowed-dsts=$DSTS"
if gcloud compute instances describe "$VM" --project "$P" --zone "$Z" >/dev/null 2>&1; then
    gcloud compute instances add-metadata "$VM" --project "$P" --zone "$Z" --metadata "$META" \
        --metadata-from-file startup-script="$HERE/vm-startup.sh"
    gcloud compute instances reset "$VM" --project "$P" --zone "$Z" --quiet
else
    gcloud compute instances create "$VM" --project "$P" --zone "$Z" --machine-type e2-micro \
        --image-family debian-12 --image-project debian-cloud --boot-disk-size 10GB \
        --boot-disk-type pd-standard --address "$IP" --can-ip-forward --tags "$VM" \
        --metadata "$META" --metadata-from-file startup-script="$HERE/vm-startup.sh" \
        --labels purpose=blave-yuanta-egress
fi

step "4/5 Waiting for the VM's WireGuard public key"
SERVER_PUB=""
for _ in $(seq 60); do
    SERVER_PUB=$(gcloud compute instances get-guest-attributes "$VM" --project "$P" --zone "$Z" \
        --query-path=wg/server-pubkey --format='value(value)' 2>/dev/null || true)
    [ -n "$SERVER_PUB" ] && break
    sleep 5
done
[ -n "$SERVER_PUB" ] || { echo "VM did not publish its key — check: gcloud compute instances get-serial-port-output $VM --zone $Z" >&2; exit 1; }

step "5/5 k0s client (Secret + Deployment in $NS)"
{
    echo "[Interface]"
    echo "PrivateKey = $(cat "$WORK/client.key")"
    echo "Address = 10.99.0.2/32"
    echo "Table = auto"
    # pod traffic leaves the node with a pod source IP: masquerade it into the tunnel
    echo "PostUp = iptables -t nat -A POSTROUTING -o wg0 -j MASQUERADE"
    echo "PostDown = iptables -t nat -D POSTROUTING -o wg0 -j MASQUERADE"
    echo
    echo "[Peer]"
    echo "PublicKey = $SERVER_PUB"
    echo "Endpoint = $IP:51820"
    echo "AllowedIPs = ${DSTS// /, }, 10.99.0.1/32"
    echo "PersistentKeepalive = 25"
} > "$WORK/wg0.conf"
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"
kubectl -n "$NS" create secret generic blave-egress-wg --from-file=wg0.conf="$WORK/wg0.conf" \
    --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$HERE/wg-client.yaml"
kubectl -n "$NS" rollout restart deploy/blave-egress-wg >/dev/null
kubectl -n "$NS" rollout status deploy/blave-egress-wg --timeout=120s

echo
echo "Egress IP for Yuanta's UAT whitelist:  $IP"
echo "Give this IP to your 元大營業員. Check the tunnel: bash $HERE/check.sh"
