#!/bin/bash
# Interactive first-time deploy on a single-node k0s host.
# Secrets are typed at hidden prompts and passed to kubectl via temp files
# (mode 600, removed on exit) — never on the command line or in shell history.
# Run from the repo root:  bash deploy/k0s/setup.sh
set -euo pipefail

NS=blave-agent
IMAGE=local/blave-agent:v1
HERE="$(cd "$(dirname "$0")" && pwd)"

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "1/4 Import $IMAGE into k0s containerd (sudo)"
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -f "$HERE/Dockerfile" -t "$IMAGE" "$HERE/../.."
docker save "$IMAGE" | sudo k0s ctr --address /run/k0s/containerd.sock --namespace k8s.io images import -

step "2/4 Namespace"
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"

step "3/4 Secret blave-agent-secrets"
if kubectl -n "$NS" get secret blave-agent-secrets >/dev/null 2>&1; then
    read -rp "Secret already exists. Replace it? [y/N] " yn
    [[ "$yn" =~ ^[Yy] ]] || SKIP_SECRET=1
fi
if [ -z "${SKIP_SECRET:-}" ]; then
    read -rp "Generate a Claude subscription token now with 'claude setup-token'? [y/N] " yn
    if [[ "$yn" =~ ^[Yy] ]]; then claude setup-token; fi
    TMP="$(mktemp -d)"; chmod 700 "$TMP"; trap 'rm -rf "$TMP"' EXIT
    read -rsp "CLAUDE_CODE_OAUTH_TOKEN (hidden): " v; echo; printf '%s' "$v" > "$TMP/CLAUDE_CODE_OAUTH_TOKEN"
    read -rsp "TELEGRAM_BOT_TOKEN (hidden): " v; echo; printf '%s' "$v" > "$TMP/TELEGRAM_BOT_TOKEN"
    read -rp  "TELEGRAM_ALLOWED_CHAT_ID (your numeric chat id, blank = auto-pair first sender): " v
    [ -n "$v" ] && printf '%s' "$v" > "$TMP/TELEGRAM_ALLOWED_CHAT_ID"
    read -rsp "BLAVE_LLM_API_KEY for LiteLLM (hidden, blank to skip): " v; echo
    [ -n "$v" ] && printf '%s' "$v" > "$TMP/BLAVE_LLM_API_KEY"
    unset v
    for f in CLAUDE_CODE_OAUTH_TOKEN TELEGRAM_BOT_TOKEN; do
        [ -s "$TMP/$f" ] || { echo "$f is required" >&2; exit 1; }
    done
    # A token pasted without its "<bot id>:" prefix makes Telegram answer 404, and
    # entrypoint would then log the secret half as the "bot id".
    grep -Eqx '[0-9]+:[A-Za-z0-9_-]{30,}' "$TMP/TELEGRAM_BOT_TOKEN" \
        || { echo "TELEGRAM_BOT_TOKEN must be the full '<digits>:<secret>' line from BotFather" >&2; exit 1; }
    if [ -s "$TMP/TELEGRAM_ALLOWED_CHAT_ID" ]; then
        grep -Eqx -- '-?[0-9]+' "$TMP/TELEGRAM_ALLOWED_CHAT_ID" \
            || { echo "TELEGRAM_ALLOWED_CHAT_ID must be numeric" >&2; exit 1; }
        [ "$(cat "$TMP/TELEGRAM_ALLOWED_CHAT_ID")" != "$(cut -d: -f1 "$TMP/TELEGRAM_BOT_TOKEN")" ] \
            || { echo "TELEGRAM_ALLOWED_CHAT_ID is the bot's own id; use YOUR id from @userinfobot" >&2; exit 1; }
    fi
    kubectl -n "$NS" create secret generic blave-agent-secrets --from-file="$TMP" \
        --dry-run=client -o yaml | kubectl apply -f -
fi

step "4/4 Deploy"
kubectl apply -f "$HERE/k8s/blave-agent.yaml"
kubectl -n "$NS" rollout restart deploy/blave-agent >/dev/null 2>&1 || true
kubectl -n "$NS" rollout status deploy/blave-agent --timeout=180s
echo
echo "Done. Send your bot a message on Telegram. Logs: kubectl -n $NS logs -f deploy/blave-agent"
