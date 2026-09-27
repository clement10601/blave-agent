"""lib/order_alpaca.py + lib/account_alpaca.py against a fake Alpaca (requests.request
patched): intent derivation (no reduce-only flag), HALT, flips as close-then-open,
extended-hours limits, market orders refused while closed, bracket validation,
client_order_id recovery after a dropped POST, and the account readers.

Run: cd blave-agent && python3 tests/check_alpaca_lib.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BLAVE_AGENT_HOME"] = os.environ["BLAVECLAW_HOME"] = tempfile.mkdtemp(prefix="notify-none-")
os.chdir(tempfile.mkdtemp(prefix="alpaca-ws-"))
os.makedirs("state", exist_ok=True)

import requests  # noqa: E402

from lib import guard  # noqa: E402
import lib.order_alpaca as a  # noqa: E402
import lib.account_alpaca as acct  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += 0 if cond else 1


def raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception as e:
        print(f"     (got {type(e).__name__}: {e})")
    return False


ENV = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}
state = {"pos": {"AAPL": 0}, "open": True, "orders": {}, "posted": [], "drop_next_post": False,
         "asset": {"shortable": True, "easy_to_borrow": True}}


class Resp:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}
        self.content = b"" if data is None else json.dumps(data).encode()
        self.ok = 200 <= status < 300
        self.text = self.content.decode()

    def json(self):
        if self._data is None:
            raise ValueError("empty")
        return self._data


def fake_request(method, url, headers=None, json=None, params=None, timeout=None):
    assert headers["APCA-API-KEY-ID"] == "k"
    path = url.split(".markets", 1)[1]
    if method == "GET" and path == "/v2/clock":
        return Resp(200, {"is_open": state["open"]})
    if method == "GET" and path.startswith("/v2/assets/"):
        return Resp(200, {"status": "active", "tradable": True, "fractionable": True, **state["asset"]})
    if method == "GET" and path.startswith("/v2/positions/"):
        q = state["pos"].get(path.rsplit("/", 1)[1], 0)
        return Resp(200, {"qty": str(q)}) if q else Resp(404, {"code": 40410000, "message": "position does not exist"})
    if method == "GET" and path == "/v2/positions":
        return Resp(200, [{"symbol": s, "qty": str(q), "current_price": "10", "avg_entry_price": "9",
                           "unrealized_pl": str(q)} for s, q in state["pos"].items() if q])
    if method == "GET" and path == "/v2/account":
        return Resp(200, {"account_number": "PA3ABC", "equity": "100500", "last_equity": "100000", "cash": "50000", "buying_power": "200000",
                          "long_market_value": "50500", "short_market_value": "0", "currency": "USD"})
    if method == "GET" and path.startswith("/v2/stocks/"):
        return Resp(200, {"quote": {"bp": 9.99, "ap": 10.01}} if "quotes" in path else {"trade": {"p": 10.0}})
    if method == "GET" and path == "/v2/orders:by_client_order_id":
        for o in state["orders"].values():
            if o["client_order_id"] == params["client_order_id"]:
                return Resp(200, o)
        return Resp(404, {"code": 40410000, "message": "order not found"})
    if method == "GET" and path.startswith("/v2/orders/"):
        return Resp(200, state["orders"][path.rsplit("/", 1)[1]])
    if method == "POST" and path == "/v2/orders":
        assert "_intent" not in json, "lib-internal key leaked to Alpaca"
        state["posted"].append(json)
        oid = f"o{len(state['orders']) + 1}"
        qty = int(json["qty"])
        filled = json["type"] == "market"
        o = {"id": oid, "client_order_id": json["client_order_id"], "symbol": json["symbol"],
             "side": json["side"], "type": json["type"], "qty": str(qty),
             "status": "filled" if filled else "new", "filled_qty": str(qty if filled else 0),
             "filled_avg_price": "10.0" if filled else None, "legs": None}
        state["orders"][oid] = o
        if filled:
            state["pos"][json["symbol"]] = state["pos"].get(json["symbol"], 0) + (qty if json["side"] == "buy" else -qty)
        if state["drop_next_post"]:
            state["drop_next_post"] = False
            raise requests.exceptions.ConnectionError("dropped after the server accepted it")
        return Resp(200, o)
    if method == "DELETE" and path.startswith("/v2/orders/"):
        state["orders"][path.rsplit("/", 1)[1]]["status"] = "canceled"
        return Resp(204)
    raise AssertionError(f"unexpected {method} {path}")


a.requests.request = fake_request
a.time.sleep = lambda s: None

# ── reconciler contract ──────────────────────────────────────────────────────
check(a.format_qty(ENV, "aapl", 2.9) == "2" and a.format_qty(ENV, "AAPL", 0.4) == "", "format_qty floors to whole shares")
r = a.place_market_order(ENV, "AAPL", "long", 5)
check(r["executed_qty"] == 5 and r["avg_price"] == 10.0 and state["pos"]["AAPL"] == 5, "market long 5 → Alpaca fills")
check(state["posted"][-1]["side"] == "buy" and state["posted"][-1]["time_in_force"] == "day", "long entry = buy, day")
a.close_position_partial(ENV, "AAPL", "long", 2)
check(state["posted"][-1]["side"] == "sell" and state["pos"]["AAPL"] == 3, "close_position_partial long 2 = sell 2")
check(raises(a.AlpacaError, lambda: a.close_position_partial(ENV, "AAPL", "long", 10)) and state["pos"]["AAPL"] == 3,
      "closing more than held is refused (would flip)")
check(a.place_market_order(ENV, "AAPL", "long", 0.5) is False, "under one share → False")

state["open"] = False
check(raises(a.AlpacaError, lambda: a.place_market_order(ENV, "AAPL", "long", 1)), "market order refused while market closed")
state["open"] = True

# ── HALT: entries refused, reduces pass ──────────────────────────────────────
guard.trip_halt("test", "check_alpaca_lib")
try:
    n = len(state["posted"])
    check(raises(guard.Halted, lambda: a.place_market_order(ENV, "AAPL", "long", 1)) and len(state["posted"]) == n,
          "HALT: buy (entry) refused before reaching Alpaca")
    a.close_position_partial(ENV, "AAPL", "long", 1)
    check(len(state["posted"]) == n + 1, "HALT: sell that shrinks a long still goes out")
    n = len(state["posted"])
    check(raises(a.AlpacaError, lambda: a.place_limit_order(ENV, "AAPL", "sell", 50, 9.5)) and len(state["posted"]) == n,
          "HALT: a sell beyond the long is a flip, refused before sending (never passes as a reduce)")
finally:
    guard.clear_halt("check_alpaca_lib")
check(a._order_intent("POST", "/v2/orders", {}) == "entry", "POST without _intent fails closed as entry")

# ── flips and set_position ───────────────────────────────────────────────────
state["pos"]["AAPL"] = 3
res = a.set_position(ENV, "AAPL", -4)
check([(p["side"], p["qty"]) for p in state["posted"][-2:]] == [("sell", "3"), ("sell", "4")]
      and state["pos"]["AAPL"] == -4 and res["from"] == 3 and res["now"] == -4,
      "set_position long 3 → short 4 = close 3 then short 4")
state["asset"] = {"shortable": False, "easy_to_borrow": False}
a._rules_cache.clear()
state["pos"]["TSLA"] = 0
check(raises(a.AlpacaError, lambda: a.place_market_order(ENV, "TSLA", "short", 1)), "short refused when not easy-to-borrow")
state["asset"] = {"shortable": True, "easy_to_borrow": True}
a._rules_cache.clear()

# ── extended hours + bracket ─────────────────────────────────────────────────
state["open"] = False
o = a.place_limit_order(ENV, "AAPL", "buy", 2, 10.05, extended_hours=True, timeout=0.01)
p = state["posted"][-1]
check(p["type"] == "limit" and p["extended_hours"] is True and p["limit_price"] == "10.05" and o["status"] == "new",
      "pre-market limit with extended_hours, working order returned")
check(a.place_limit_order(ENV, "AAPL", "buy", 1, 0.5123, timeout=0.01) and state["posted"][-1]["limit_price"] == "0.5123",
      "sub-dollar limit keeps 4 decimals")
check(raises(ValueError, lambda: a.place_limit_order(ENV, "AAPL", "buy", 1, 10, extended_hours=True, time_in_force="gtc")),
      "extended hours needs time_in_force day")
state["open"] = True
state["pos"]["NVDA"] = 0
check(raises(ValueError, lambda: a.place_bracket_order(ENV, "NVDA", "buy", 10, stop_loss=11, take_profit=12)),
      "buy bracket with stop above entry is refused")
a.place_bracket_order(ENV, "NVDA", "buy", 10, stop_loss=9.5, take_profit=12, limit_price=10.02)
p = state["posted"][-1]
check(p["order_class"] == "bracket" and p["stop_loss"] == {"stop_price": "9.50"} and p["take_profit"] == {"limit_price": "12.00"},
      "bracket order carries stop_loss / take_profit legs")

# ── dropped POST resolved by client_order_id ─────────────────────────────────
n = len(state["posted"])
state["drop_next_post"] = True
state["pos"]["AMD"] = 0
r = a.place_market_order(ENV, "AMD", "long", 7, client_order_id="sig42")
check(len(state["posted"]) == n + 1 and r["executed_qty"] == 7 and r["client_order_id"] == "blv-sig42",
      "POST dropped in transit → found by client_order_id, not resent")

# ── cancel + account readers ─────────────────────────────────────────────────
wid = [k for k, v in state["orders"].items() if v["status"] == "new"][0]
check(a.cancel_order(ENV, wid)["status"] == "canceled", "cancel_order returns the canceled state")
eq = acct.get_equity(ENV)
check(eq["equity"] == 100500.0 and eq["currency"] == "USD", "get_equity")
pos = {p["symbol"]: p for p in acct.get_positions(ENV)}
check(pos["AAPL"]["side"] == "short" and pos["AAPL"]["size"] == 4.0, "get_positions: short as side + abs size")
check(acct.get_todays_pl(ENV)["pl"] == 500.0, "get_todays_pl = equity − last_equity")
check(acct.get_account_id(ENV) == "PA3ABC", "get_account_id = account_number")

log = open("state/audit.jsonl").read()
check('"venue": "alpaca"' in log and "order_denied_halt" in log and "order_ok" in log, "orders audited")
print(f"\n{'FAILED' if fails else 'passed'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
