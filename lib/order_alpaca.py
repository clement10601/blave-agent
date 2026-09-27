"""
Alpaca (US equities) order execution — REST v2, paper by default.

Companion to references/alpaca-broker.md. Credentials in .env:
    ALPACA_API_KEY / ALPACA_SECRET_KEY   key pair from the Alpaca dashboard
    ALPACA_LIVE=true                     live trading. Without it every call goes to
                                         paper-api.alpaca.markets (paper keys only work there).
    ALPACA_DATA_FEED                     'iex' (free, default) or 'sip' (paid subscription)

Contract: the reconciler's four fixed names (get_contract_rules / format_qty /
place_market_order / close_position_partial, see lib/order_TEMPLATE.py) with
BASE units = whole shares, plus the US-equity helpers a signal engine needs:
place_limit_order (pre-market / after-hours via extended_hours), place_bracket_order,
cancel_order, get_order, get_latest_quote, get_clock.

Design rules (same as lib/order_bingx.py):
  FAIL LOUD      non-2xx → AlpacaError with Alpaca's own code/message.
  CONFIRMED      orders are polled to a terminal state; returns Alpaca's
                 filled_qty / filled_avg_price, never the intent.
  IDEMPOTENT     client_order_id unique per intent; a POST that dies in transit is
                 looked up by client_order_id before anything is resent.
  HALTABLE       every mutating request goes through _request → guard (restart stop,
                 account hold, HALT for entries) → audit. Alpaca has no reduce-only
                 flag, so the lib derives intent from the live position and passes it
                 as `_intent`; a POST without it is treated as an ENTRY (halted).
  NO FLIPS       Alpaca rejects an order that crosses zero; a long→short change is
                 sent as a close then an open.
"""

import logging
import time
import uuid

import requests

from lib import guard

guard.mark_money_process()  # Stop in the chat never kills this process (lib/guard)

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
CID_PREFIX = "blv-"
CID_MAX = 128

TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "stopped", "suspended"}
RETRY_STATUS = {429, 500, 502, 503, 504}


class AlpacaError(Exception):
    def __init__(self, status, code, message, path):
        self.status, self.code, self.message, self.path = status, code, message, path
        super().__init__(f"Alpaca {status} {code or ''} on {path}: {message}")


class OrderNotConfirmed(Exception):
    """Sent but not terminal within the timeout. Query get_order before resubmitting."""


def _base(env):
    return LIVE_URL if str(env.get("ALPACA_LIVE", "")).lower() == "true" else PAPER_URL


def _headers(env):
    key, secret = env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise ValueError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing from .env")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _canonical(symbol):
    return str(symbol).strip().upper()


# ── the one gate ─────────────────────────────────────────────────────────────

_AUDIT_KEYS = ("symbol", "side", "type", "qty", "limit_price", "stop_price", "time_in_force",
               "extended_hours", "order_class", "client_order_id")


def _order_intent(method, path, body):
    """'entry' | 'reduce' | 'protective' | 'cancel' | 'cancel_all' | None (read).
    POST /v2/orders carries the lib-computed `_intent`; missing → 'entry' (fail closed)."""
    if method == "GET":
        return None
    if method == "POST" and path == "/v2/orders":
        intent = (body or {}).get("_intent", "entry")
        return intent if intent in ("entry", "reduce", "protective") else "entry"
    if method == "PATCH" and path.startswith("/v2/orders/"):
        return "protective"          # replace: only used for stop/limit price moves
    if method == "DELETE" and path == "/v2/orders":
        return "cancel_all"
    if method == "DELETE" and path.startswith("/v2/orders/"):
        return "cancel"
    if method == "DELETE" and path.startswith("/v2/positions"):
        return "reduce"              # liquidation endpoints only ever close
    return "entry"                   # unknown mutating call: fail closed


def _request(method, path, env, body=None, params=None):
    """Gate + audit around _send. Reads pass straight through."""
    intent = _order_intent(method, path, body)
    if intent is None:
        return _send(method, path, env, body, params)
    fields = {k: (body or {})[k] for k in _AUDIT_KEYS if k in (body or {})}
    fields.update(intent=intent, venue="alpaca", path=path,
                  paper=_base(env) == PAPER_URL)
    if "symbol" not in fields and path.startswith("/v2/positions/"):
        fields["symbol"] = path.rsplit("/", 1)[-1]
    guard.check_restart_stop(intent, fields)
    guard.check_account_hold("alpaca", intent, fields)
    if intent == "entry" and guard.entry_blocked():
        guard.audit("order_denied_halt", **fields)
        raise guard.Halted(
            f"state/HALT is set ({guard.halt_info()}) — entry order for {fields.get('symbol')} "
            f"refused before reaching Alpaca. Closes, stops and cancels still work. "
            f"Only the user may clear the halt (guard.clear_halt).")
    guard.audit("order_attempt", **fields)
    try:
        data = _send(method, path, env, body, params)
    except Exception as e:
        guard.audit("order_error", error=str(e), **fields)
        raise
    guard.audit("order_ok", order_id=str((data or {}).get("id", "")) if isinstance(data, dict) else "",
                **fields)
    return data


def _send(method, path, env, body=None, params=None, retries=3, base=None):
    """HTTP transport. Keys starting with '_' are lib-internal and never sent.
    A POST /v2/orders that fails in transit is resolved by client_order_id instead of resent."""
    url = f"{base or _base(env)}{path}"
    payload = {k: v for k, v in (body or {}).items() if not k.startswith("_")} if body else None
    last = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, headers=_headers(env), json=payload,
                                 params=params, timeout=15)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last = e
            if method == "POST" and path == "/v2/orders" and payload.get("client_order_id"):
                found = _find_by_cid(env, payload["client_order_id"])
                if found:
                    return found
            if method != "GET" and not (method == "POST" and payload and payload.get("client_order_id")):
                raise
            time.sleep(1 + attempt)
            continue
        if r.status_code in RETRY_STATUS and attempt < retries - 1 and (
                method == "GET" or r.status_code == 429):
            time.sleep(float(r.headers.get("Retry-After") or (1 + attempt)))
            continue
        if r.status_code == 204 or not r.content:
            if r.ok:
                return None
        try:
            data = r.json()
        except ValueError:
            raise AlpacaError(r.status_code, None, f"non-JSON response ({r.text[:120]})", path)
        if not r.ok:
            if isinstance(data, dict):
                raise AlpacaError(r.status_code, data.get("code"), data.get("message"), path)
            raise AlpacaError(r.status_code, None, str(data)[:200], path)
        return data
    raise last


def _find_by_cid(env, cid):
    try:
        return _send("GET", "/v2/orders:by_client_order_id", env, params={"client_order_id": cid}, retries=2)
    except AlpacaError as e:
        if e.status == 404:
            return None
        raise


def _cid(client_order_id=None):
    cid = f"{CID_PREFIX}{client_order_id or uuid.uuid4().hex[:20]}"
    if len(cid) > CID_MAX:
        raise ValueError(f"client_order_id too long ({len(cid)} > {CID_MAX})")
    return cid


# ── reads ────────────────────────────────────────────────────────────────────

def get_account(env):
    return _send("GET", "/v2/account", env)


def get_clock(env):
    """{'is_open', 'next_open', 'next_close', 'timestamp'} — regular session only."""
    return _send("GET", "/v2/clock", env)


def get_asset(env, symbol):
    return _send("GET", f"/v2/assets/{_canonical(symbol)}", env)


def get_position_qty(env, symbol):
    """Signed whole-share position (+long / −short), 0 when flat."""
    try:
        p = _send("GET", f"/v2/positions/{_canonical(symbol)}", env)
    except AlpacaError as e:
        if e.status == 404:
            return 0.0
        raise
    return float(p["qty"])


def get_latest_quote(env, symbol):
    """{'bid', 'ask', 'bid_size', 'ask_size', 'last', 'ts'} from the data API
    (feed ALPACA_DATA_FEED, default iex — IEX is a slice of volume, not the NBBO)."""
    sym, feed = _canonical(symbol), env.get("ALPACA_DATA_FEED") or "iex"
    q = _send("GET", f"/v2/stocks/{sym}/quotes/latest", env, params={"feed": feed}, base=DATA_URL)["quote"]
    t = _send("GET", f"/v2/stocks/{sym}/trades/latest", env, params={"feed": feed}, base=DATA_URL)["trade"]
    return {"bid": float(q.get("bp") or 0), "ask": float(q.get("ap") or 0),
            "bid_size": q.get("bs"), "ask_size": q.get("as"), "last": float(t.get("p") or 0),
            "ts": t.get("t")}


def get_mark_price(env, symbol):
    return get_latest_quote(env, symbol)["last"]


def _norm(o):
    return {
        "order_id": o.get("id"),
        "client_order_id": o.get("client_order_id"),
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "type": o.get("type"),
        "status": o.get("status"),
        "qty": float(o.get("qty") or 0),
        "executed_qty": float(o.get("filled_qty") or 0),
        "avg_price": float(o.get("filled_avg_price") or 0),
        "commission": 0.0,  # Alpaca charges no equity commission; regulatory fees post later as activities
        "legs": [_norm(leg) for leg in (o.get("legs") or [])],
        "paper": None,
    }


def get_order(env, order_id):
    return _norm(_send("GET", f"/v2/orders/{order_id}", env, params={"nested": "true"}))


def confirm_order(env, order_id, timeout=30, rest_ok=False):
    """Poll to a terminal state. filled → result; canceled/expired/rejected with a partial
    fill → result; with no fill → AlpacaError. rest_ok=True returns a still-working order
    at timeout (limit orders); otherwise the timeout raises OrderNotConfirmed."""
    deadline = time.time() + timeout
    o = None
    while time.time() < deadline:
        o = get_order(env, order_id)
        if o["status"] == "filled":
            return o
        if o["status"] in TERMINAL:
            if o["executed_qty"] > 0:
                return o
            raise AlpacaError(None, o["status"], f"order {order_id} {o['status']} with nothing filled",
                              "/v2/orders")
        time.sleep(1)
    if rest_ok and o is not None and o["status"] not in ("pending_new",):
        return o
    raise OrderNotConfirmed(f"order {order_id} still {o['status'] if o else 'unknown'} after {timeout}s")


# ── reconciler contract (lib/order_TEMPLATE.py) ──────────────────────────────

_rules_cache = {}


def get_contract_rules(env, symbol):
    """Whole shares only (fractional orders cannot short or trade extended hours)."""
    sym = _canonical(symbol)
    if sym not in _rules_cache:
        a = get_asset(env, sym)
        if a.get("status") != "active" or not a.get("tradable"):
            raise AlpacaError(None, "not_tradable", f"{sym} is not tradable on Alpaca", "/v2/assets")
        _rules_cache[sym] = {"step": "1", "min_qty": 1.0, "min_notional": 1.0, "contract_value": 1.0,
                             "shortable": bool(a.get("shortable")),
                             "easy_to_borrow": bool(a.get("easy_to_borrow")),
                             "fractionable": bool(a.get("fractionable"))}
    return _rules_cache[sym]


def format_qty(env, symbol, qty, price=None):
    """Floor to whole shares; '' when below one share (the min-size gate)."""
    get_contract_rules(env, symbol)
    n = int(abs(float(qty)))
    return str(n) if n >= 1 else ""


def _side(direction, closing):
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    opening_side = "buy" if direction == "long" else "sell"
    return ("sell" if opening_side == "buy" else "buy") if closing else opening_side


def _check_short(env, symbol, direction):
    if direction == "short":
        r = get_contract_rules(env, symbol)
        if not (r["shortable"] and r["easy_to_borrow"]):
            raise AlpacaError(None, "not_shortable",
                              f"{_canonical(symbol)} is not shortable / easy-to-borrow on Alpaca", "/v2/assets")


def _intent_for(env, symbol, side, qty):
    """'reduce' only when the order shrinks the current position without crossing zero."""
    pos = get_position_qty(env, symbol)
    if (pos > 0 and side == "sell" and qty <= pos) or (pos < 0 and side == "buy" and qty <= -pos):
        return "reduce"
    if (pos > 0 and side == "sell") or (pos < 0 and side == "buy"):
        raise AlpacaError(None, "would_flip", f"{side} {qty} {symbol} crosses a {pos} position — "
                          f"close first (use set_position / close_position_partial)", "/v2/orders")
    return "entry"


def place_market_order(env, symbol, direction, qty, client_order_id=None, reduce_only=False,
                       timeout=30):
    """Confirmed market DAY order for whole shares; regular session only (Alpaca queues a
    market order sent while closed until the open — refused here instead).
    direction = the POSITION's direction. Returns fills, or False below one share."""
    n = format_qty(env, symbol, qty)
    if not n:
        return False
    side = _side(direction, closing=reduce_only)
    if not reduce_only:
        _check_short(env, symbol, direction)
    intent = _intent_for(env, symbol, side, int(n))
    if reduce_only and intent != "reduce":
        raise AlpacaError(None, "not_reduce", f"reduce_only {side} {n} {symbol} does not shrink the position",
                          "/v2/orders")
    if not get_clock(env).get("is_open"):
        raise AlpacaError(None, "market_closed", "market orders only in the regular session — "
                          "use place_limit_order(..., extended_hours=True) outside it", "/v2/orders")
    body = {"symbol": _canonical(symbol), "qty": n, "side": side, "type": "market",
            "time_in_force": "day", "client_order_id": _cid(client_order_id), "_intent": intent}
    o = _request("POST", "/v2/orders", env, body)
    return confirm_order(env, o["id"], timeout=timeout)


def close_position_partial(env, symbol, direction, qty, client_order_id=None):
    """Reduce the `direction` position by `qty` shares (market, regular session). Passes HALT."""
    return place_market_order(env, symbol, direction, qty, client_order_id=client_order_id,
                              reduce_only=True)


# ── US-equity helpers ────────────────────────────────────────────────────────

def place_limit_order(env, symbol, side, qty, limit_price, extended_hours=False,
                      time_in_force="day", client_order_id=None, timeout=10):
    """Whole-share limit order. extended_hours=True (pre-market 04:00–09:30 / after-hours
    16:00–20:00 ET) requires time_in_force 'day'. side: 'buy'|'sell'. Returns the order
    state after `timeout` s — filled, partially filled or still working (rest_ok)."""
    n = int(abs(float(qty)))
    if n < 1:
        return False
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
    if extended_hours and time_in_force != "day":
        raise ValueError("extended_hours orders must be time_in_force='day'")
    get_contract_rules(env, symbol)
    intent = _intent_for(env, symbol, side, n)
    if intent == "entry" and side == "sell":
        _check_short(env, symbol, "short")
    body = {"symbol": _canonical(symbol), "qty": str(n), "side": side, "type": "limit",
            "limit_price": f"{float(limit_price):.2f}" if limit_price >= 1 else f"{float(limit_price):.4f}",
            "time_in_force": time_in_force, "extended_hours": bool(extended_hours),
            "client_order_id": _cid(client_order_id), "_intent": intent}
    o = _request("POST", "/v2/orders", env, body)
    return confirm_order(env, o["id"], timeout=timeout, rest_ok=True)


def place_bracket_order(env, symbol, side, qty, stop_loss, take_profit, limit_price=None,
                        client_order_id=None, timeout=10):
    """Entry (market, or limit when limit_price) + OCO stop-loss / take-profit children,
    regular session only (Alpaca does not run brackets in extended hours). The children
    are protective: they only ever close what the parent opened."""
    n = int(abs(float(qty)))
    if n < 1:
        return False
    direction = "long" if side == "buy" else "short"
    _check_short(env, symbol, direction)
    if _intent_for(env, symbol, side, n) != "entry":
        raise AlpacaError(None, "bracket_on_reduce", "a bracket must open a position", "/v2/orders")
    ref = limit_price or get_mark_price(env, symbol)
    if side == "buy" and not stop_loss < ref < take_profit:
        raise ValueError(f"buy bracket needs stop_loss < entry ({ref}) < take_profit")
    if side == "sell" and not take_profit < ref < stop_loss:
        raise ValueError(f"sell bracket needs take_profit < entry ({ref}) < stop_loss")
    body = {"symbol": _canonical(symbol), "qty": str(n), "side": side,
            "type": "limit" if limit_price else "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit": {"limit_price": f"{take_profit:.2f}"},
            "stop_loss": {"stop_price": f"{stop_loss:.2f}"},
            "client_order_id": _cid(client_order_id), "_intent": "entry"}
    if limit_price:
        body["limit_price"] = f"{limit_price:.2f}"
    o = _request("POST", "/v2/orders", env, body)
    return confirm_order(env, o["id"], timeout=timeout, rest_ok=True)


def set_position(env, symbol, target_qty, client_tag=None, extended_hours=False, limit_offset_pct=0.5):
    """Move a symbol to a signed whole-share target (+long / −short). The shrinking part goes
    first as a reduce (passes HALT); a flip is close-then-open. Regular session → market
    orders; extended_hours=True → marketable limits (last ± limit_offset_pct %).
    Returns {'orders', 'from', 'to'} or False when already there."""
    target = int(target_qty)
    pos = start = int(get_position_qty(env, symbol))
    if target == pos:
        return False
    steps = []
    if pos and (target == 0 or (pos > 0) != (target > 0) or abs(target) < abs(pos)):
        close_n = abs(pos) if (target == 0 or (pos > 0) != (target > 0)) else abs(pos) - abs(target)
        steps.append(("sell" if pos > 0 else "buy", close_n))
        pos = pos - close_n if pos > 0 else pos + close_n
    if target != pos:
        steps.append(("buy" if target > pos else "sell", abs(target - pos)))
    results = []
    for i, (side, n) in enumerate(steps):
        cid = f"{client_tag}-{i}" if client_tag else None
        if extended_hours:
            last = get_mark_price(env, symbol)
            px = last * (1 + limit_offset_pct / 100) if side == "buy" else last * (1 - limit_offset_pct / 100)
            results.append(place_limit_order(env, symbol, side, n, px, extended_hours=True,
                                             client_order_id=cid, timeout=15))
        else:
            cur = get_position_qty(env, symbol)
            closing = (cur > 0 and side == "sell") or (cur < 0 and side == "buy")
            direction = ("long" if cur > 0 else "short") if closing else ("long" if side == "buy" else "short")
            results.append(place_market_order(env, symbol, direction, n, client_order_id=cid,
                                              reduce_only=closing))
    return {"symbol": _canonical(symbol), "from": start, "to": target,
            "now": int(get_position_qty(env, symbol)), "orders": results}


def cancel_order(env, order_id):
    """Cancel one working order (passes HALT and the restart stop)."""
    _request("DELETE", f"/v2/orders/{order_id}", env)
    return get_order(env, order_id)


def cancel_all_orders(env):
    return _request("DELETE", "/v2/orders", env)


def close_position(env, symbol):
    """Liquidate the whole position at market (Alpaca's own close endpoint). Passes HALT."""
    o = _request("DELETE", f"/v2/positions/{_canonical(symbol)}", env)
    return confirm_order(env, o["id"]) if isinstance(o, dict) and o.get("id") else o
