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
docker run --rm --env-file "$F" -v "$PWD:/repo:ro" -w /repo --entrypoint python3 \
    local/blave-agent:v1 lib/alpaca_probe.py "$@"
