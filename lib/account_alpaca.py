# Alpaca (US equities) account reads — lib/account_TEMPLATE.py contract.
# Keys: ALPACA_API_KEY / ALPACA_SECRET_KEY in .env; paper unless ALPACA_LIVE=true.
# Read-only: nothing here places an order (tests/check_restart_stop_order_gate.py).

from lib.order_alpaca import _send


def get_equity(env: dict) -> dict:
    """{'equity', 'currency', 'accounts': {'cash', 'buying_power', 'long_market_value',
    'short_market_value'}} — equity is the sizing base (cash + positions)."""
    a = _send("GET", "/v2/account", env)
    if a.get("trading_blocked") or a.get("account_blocked"):
        raise RuntimeError(f"Alpaca account blocked (status {a.get('status')})")
    return {
        "equity": float(a["equity"]),
        "currency": a.get("currency") or "USD",
        "accounts": {k: float(a.get(k) or 0) for k in
                     ("cash", "buying_power", "long_market_value", "short_market_value")},
        "pattern_day_trader": bool(a.get("pattern_day_trader")),
        "daytrade_count": int(a.get("daytrade_count") or 0),
    }


def get_account_id(env: dict) -> str:
    """Alpaca `account_number` from GET /v2/account — the same for every key of the account,
    different for another account (lib.portfolio.book_account_check). Raises when missing."""
    num = (_send("GET", "/v2/account", env) or {}).get("account_number")
    if not num:
        raise RuntimeError("Alpaca /v2/account returned no account_number")
    return str(num)


def get_positions(env: dict) -> list:
    """[{'symbol', 'side', 'size' (shares, abs), 'mark_price', 'avg_entry_price',
    'unrealized_pl'}], [] when flat. US tickers are already canonical (uppercase)."""
    out = []
    for p in _send("GET", "/v2/positions", env) or []:
        qty = float(p.get("qty") or 0)
        if qty == 0:
            continue
        out.append({
            "symbol": (p.get("symbol") or "").upper(),
            "side": "short" if qty < 0 else "long",
            "size": abs(qty),
            "mark_price": float(p.get("current_price") or 0),
            "avg_entry_price": float(p.get("avg_entry_price") or 0),
            "unrealized_pl": float(p.get("unrealized_pl") or 0),
        })
    return out


def get_todays_pl(env: dict) -> dict:
    """{'equity', 'last_equity', 'pl'} — today's P&L vs the previous close's equity
    (what a daily-loss / give-back rule needs)."""
    a = _send("GET", "/v2/account", env)
    eq, last = float(a["equity"]), float(a["last_equity"])
    return {"equity": eq, "last_equity": last, "pl": eq - last}
