"""Stand-in for the provision-owned control/sync_notify_compat.py (not in the public repo).

lib/notify.py (strategy alerts, healthcheck, reconciler) reads the bot token from
$BLAVE_AGENT_HOME/openclaw.json and the paired chat ids from
credentials/telegram-default-allowFrom.json. The bridge keeps the pairing in
config/telegram.json; this copies it across. Clearing is done by
runtime/telegram_pairing.clear_notify_compat, so this only ever writes.
"""
import json
import os
import sys

HOME = os.environ.get("BLAVE_AGENT_HOME", "/opt/blave-agent")
sys.path.insert(0, os.path.join(HOME, "current"))
from telegram_pairing import write_json_600  # noqa: E402

CONFIG = os.path.join(HOME, "config", "telegram.json")
OPENCLAW = os.path.join(HOME, "openclaw.json")
ALLOW_FROM = os.path.join(HOME, "credentials", "telegram-default-allowFrom.json")


def load(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def main():
    cfg = load(CONFIG)
    token, chat_id = cfg.get("bot_token"), cfg.get("allowed_chat_id")
    if not token:
        return
    data = load(OPENCLAW)
    data.setdefault("channels", {}).setdefault("telegram", {})["botToken"] = token
    write_json_600(OPENCLAW, data)
    if chat_id is not None:
        os.makedirs(os.path.dirname(ALLOW_FROM), exist_ok=True)
        write_json_600(ALLOW_FROM, {"allowFrom": [chat_id]})


if __name__ == "__main__":
    main()
