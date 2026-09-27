#!/bin/bash
# Checks Alpaca keys against the real API from this machine, before they go into the pod.
# Keys are typed at hidden prompts once and kept in ~/.config/blave-agent/alpaca.env (mode 600);
# they never appear on a command line. Run from the repo root:
#   bash deploy/k0s/alpaca-probe.sh               read-only check
#   bash deploy/k0s/alpaca-probe.sh --paper-order places + cancels one far-away paper limit order
set -euo pipefail
F="$HOME/.config/blave-agent/alpaca.env"
if [ ! -s "$F" ]; then
    mkdir -p "$(dirname "$F")"; umask 077
    read -rsp "ALPACA_API_KEY (hidden): " k; echo
    read -rsp "ALPACA_SECRET_KEY (hidden): " s; echo
    printf 'ALPACA_API_KEY=%s\nALPACA_SECRET_KEY=%s\n' "$k" "$s" > "$F"
    unset k s
    echo "saved to $F"
fi
# scratch workspace so the guard can write state/audit.jsonl (the repo stays read-only)
docker run --rm --env-file "$F" -v "$PWD:/repo:ro" --entrypoint sh local/blave-agent:v1 -c \
    'mkdir -p /tmp/ws/state && cp -r /repo/lib /tmp/ws/ && cd /tmp/ws && python3 lib/alpaca_probe.py "$@"; rc=$?; [ -s state/audit.jsonl ] && { echo "--- audit"; cat state/audit.jsonl; }; exit $rc' \
    sh "$@"
