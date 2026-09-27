#!/bin/bash
# Prepares the PVC layout on every start, then supervises the two long-running
# pieces: supercronic (strategy / healthcheck schedules) and the Telegram bridge.
# The reconciler is NOT started here on purpose: upstream keeps it off after a
# restart until the user re-enables trading (references/manager.md); the agent
# starts it under tmux when asked.
set -euo pipefail

B=/opt/blave-agent
D="$B/data"
CRONTAB="$D/crontab"
PY="$B/venv/bin/python3"

umask 077
mkdir -p "$D"/{config,state,credentials,home,workspace}

# Seed the workspace once; after that it belongs to the user (strategies/, state/, .env).
if [ ! -f "$D/workspace/AGENTS.md" ]; then
    echo "[entrypoint] seeding workspace from image source"
    cp -a "$B/src/." "$D/workspace/"
fi
[ -f "$D/workspace/.env" ] || : > "$D/workspace/.env"
chmod 600 "$D/workspace/.env"

if [ ! -f "$CRONTAB" ]; then
    printf 'BLAVE_AGENT_HOME=%s\n' "$B" > "$CRONTAB"
fi

# First start only: pin the default model (later switches via set_model.py stick).
if [ ! -f "$D/state/model_prefs.json" ]; then
    printf '{"_last": "%s"}\n' "${BLAVE_DEFAULT_MODEL:-claude-sonnet-5}" > "$D/state/model_prefs.json"
fi

# Telegram pairing from the Secret. A new bot token drops the old pairing;
# TELEGRAM_ALLOWED_CHAT_ID pins the chat so auto-pair can never bind a stranger.
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    "$PY" - <<'EOF'
import json, os, sys
sys.path.insert(0, "/opt/blave-agent/current")
from telegram_pairing import write_json_600
path = "/opt/blave-agent/config/telegram.json"
try:
    with open(path) as f:
        cfg = json.load(f) or {}
except (FileNotFoundError, ValueError):
    cfg = {}
token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
if cfg.get("bot_token") != token:
    cfg = {"bot_token": token}
pinned = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
if pinned:
    cfg["allowed_chat_id"] = int(pinned)
write_json_600(path, cfg)
print(f"[entrypoint] telegram: bot {token.split(':', 1)[0]}, "
      f"chat {'pinned' if pinned else cfg.get('allowed_chat_id', 'auto-pair on first message')}")
EOF
    "$PY" "$B/sync_notify_compat.py"
else
    echo "[entrypoint] TELEGRAM_BOT_TOKEN not set — bridge will idle until it is"
fi

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ ! -f "$D/home/.claude/.credentials.json" ]; then
    echo "[entrypoint] WARNING: no Claude credentials (CLAUDE_CODE_OAUTH_TOKEN unset, no claude login)" >&2
fi

run_forever() {
    local name="$1"; shift
    while true; do
        "$@" || true
        echo "[entrypoint] $name exited; restarting in 5s" >&2
        sleep 5
    done
}

cd "$D/workspace"
run_forever cron supercronic -inotify "$CRONTAB" &
run_forever telegram "$PY" "$B/current/telegram_bridge.py" &
wait
