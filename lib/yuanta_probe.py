"""Yuanta (元大) onboarding check — run from the workspace:

    python3 lib/yuanta_probe.py                 read-only: login, accounts, balance/equity,
                                                positions, one quote (2330, near-month TXF)
    python3 lib/yuanta_probe.py --order-units   UAT ONLY: places ONE 1-lot limit buy of 2885
                                                at limit-down (far from market, UAT money is
                                                fake), reads back the reported OrderQty and
                                                cancels it. Prints whether OrderQty=1 meant
                                                1 張 (reported 1000) or 1 share.

Prints one JSON object; exit 0 ok / 2 failed. Credentials come from .env
(see lib/order_yuanta.py). Never run --order-units with YUANTA_LIVE=true — it refuses.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import dotenv_values  # noqa: E402

from lib import order_yuanta as y  # noqa: E402


def probe(env):
    out = {"live": str(env.get("YUANTA_LIVE", "")).lower() == "true"}
    stage = "login"
    try:
        _, accts = y._get_api(env)
        out["accounts"] = dict(accts)
        if "stock" in accts:
            stage = "stock"
            out["stock_balance_twd"] = y.get_account_balance(env)
            out["stock_holdings"] = y.get_stock_holdings(env)
            out["quote_2330"] = y.get_stock_quote(env, "2330")
        if "futures" in accts:
            stage = "futures"
            out["futures_equity_twd"] = y.get_futures_equity(env)
            out["futures_positions"] = y.get_futures_positions(env)
            out["near_month"] = y.near_month()
        out["ok"] = True
    except Exception as e:
        out.update(ok=False, stage=stage, error=f"{type(e).__name__}: {e}")
    return out


def order_units(env):
    if str(env.get("YUANTA_LIVE", "")).lower() == "true":
        return {"ok": False, "error": "refused: --order-units runs in UAT only (YUANTA_LIVE is true)"}
    out = {"live": False}
    try:
        q = y.get_stock_quote(env, "2885")
        price = q["down_limit"] or round(q["price"] * 0.9, 2)
        fields = {"APCode": 0, "TradeKind": 0, "OrderType": "0", "StkCode": "2885", "BuySell": "B",
                  "PriceFlag": " ", "Price": price, "OrderQty": 1, "Time_in_force": "0",
                  "BasketNo": "", "OrderNo": ""}
        placed = y._send(env, "stock", fields, "entry", confirm_timeout=20)
        reported = placed.get("order_qty")
        out.update(order=placed, sent_order_qty=1, reported_order_qty=reported,
                   unit=("lots (張)" if reported == 1000 else "shares" if reported == 1 else "UNCLEAR"))
        if placed.get("filled_qty", 0) < (reported or 0):
            try:
                out["cancel"] = y.cancel_stock_order(env, placed["order_no"])
                out["after_cancel"] = y.get_order(env, "stock", placed["order_no"])
            except Exception as e:
                out["cancel_error"] = f"{type(e).__name__}: {e}"
        out["ok"] = out["unit"] == "lots (張)"
        out["next"] = ("set yuanta_units_verified=true in .env" if out["ok"] else
                       "do NOT enable live board-lot orders — report this output")
    except Exception as e:
        out.update(ok=False, error=f"{type(e).__name__}: {e}")
    return out


if __name__ == "__main__":
    env = dotenv_values(".env")
    result = order_units(env) if "--order-units" in sys.argv else probe(env)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    y.reset_connection()
    sys.exit(0 if result.get("ok") else 2)
