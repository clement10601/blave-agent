"""Alpaca onboarding check — run from the workspace:

    python3 lib/alpaca_probe.py                 read-only: account, clock, positions, AAPL quote
    python3 lib/alpaca_probe.py --paper-order   PAPER ONLY: one 1-share AAPL limit buy at 50% of
                                                last (never fills), read back, cancel

Prints JSON; exit 0 ok / 2 failed. Keys come from .env (ALPACA_API_KEY / ALPACA_SECRET_KEY),
or from the environment when .env lacks them.
"""
import json
import time
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import dotenv_values  # noqa: E402

from lib import account_alpaca, order_alpaca as a  # noqa: E402


def probe(env):
    out = {"paper": a._base(env) == a.PAPER_URL}
    try:
        acct = a.get_account(env)
        out["account"] = {k: acct.get(k) for k in ("status", "currency", "equity", "cash", "buying_power",
                                                    "pattern_day_trader", "shorting_enabled", "trading_blocked")}
        out["clock"] = a.get_clock(env)
        out["positions"] = account_alpaca.get_positions(env)
        out["quote_AAPL"] = a.get_latest_quote(env, "AAPL")
        out["rules_AAPL"] = a.get_contract_rules(env, "AAPL")
        out["ok"] = True
    except Exception as e:
        out.update(ok=False, error=f"{type(e).__name__}: {e}")
    return out


def paper_order(env):
    if a._base(env) != a.PAPER_URL:
        return {"ok": False, "error": "refused: --paper-order runs on paper only (ALPACA_LIVE is true)"}
    out = {"paper": True}
    try:
        last = a.get_mark_price(env, "AAPL")
        px = round(last * 0.5, 2)
        o = a.place_limit_order(env, "AAPL", "buy", 1, px, extended_hours=not a.get_clock(env)["is_open"],
                                client_order_id=f"probe-{int(time.time())}", timeout=3)
        out["placed"] = o
        out["cancelled"] = a.cancel_order(env, o["order_id"])
        out["ok"] = out["cancelled"]["status"] in ("canceled", "pending_cancel")
    except Exception as e:
        out.update(ok=False, error=f"{type(e).__name__}: {e}")
    return out


if __name__ == "__main__":
    env = {**{k: v for k, v in os.environ.items() if k.startswith("ALPACA_")},
           **{k: v for k, v in dotenv_values(".env").items() if v}}
    result = paper_order(env) if "--paper-order" in sys.argv else probe(env)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    sys.exit(0 if result.get("ok") else 2)
