#!/bin/bash
# Is the Yuanta egress tunnel up, and does UAT answer yet?
set -uo pipefail
NS=blave-agent
POD=$(kubectl -n "$NS" get pod -l app=blave-egress-wg -o jsonpath='{.items[0].metadata.name}')
echo "== tunnel ($POD)"
kubectl -n "$NS" exec "$POD" -- wg show wg0 latest-handshakes transfer
echo "== route to UAT from the node"
kubectl -n "$NS" exec "$POD" -- ip route get 220.130.122.92
echo "== VM tunnel address"
kubectl -n "$NS" exec "$POD" -- ping -c 2 -W 2 10.99.0.1 | tail -2
echo "== TCP 220.130.122.92:443 (answers only after Yuanta whitelists the VM IP)"
kubectl -n "$NS" exec "$POD" -- sh -c 'timeout 5 nc -w 4 220.130.122.92 443 </dev/null && echo OPEN || echo "no answer (not whitelisted yet?)"'
