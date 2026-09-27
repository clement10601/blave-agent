"""lib/order_yuanta.py without the broker: the .NET SDK is replaced by a fake that
records every order object and answers GetRealReportMergeSync from a script.

Covers: tick/collar math, TAIFEX near month, board-lot/odd-lot split (張 vs shares),
sell capped at sellable, futures close-then-open on a flip, confirm_order outcomes,
HALT (entry refused, reduce passes), duplicate client_tag, and the live units gate.

Run: cd blave-agent && python3 tests/check_yuanta_lib.py
"""
import datetime
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BLAVE_AGENT_HOME"] = os.environ["BLAVECLAW_HOME"] = tempfile.mkdtemp(prefix="notify-none-")
os.chdir(tempfile.mkdtemp(prefix="yuanta-ws-"))
os.makedirs("state", exist_ok=True)

from lib import guard  # noqa: E402
import lib.order_yuanta as y  # noqa: E402

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
    except Exception as e:  # wrong exception type
        print(f"     (got {type(e).__name__}: {e})")
        return False
    return False


# ── pure helpers ─────────────────────────────────────────────────────────────
check(y.tick_size(9.99) == 0.01 and y.tick_size(10) == 0.05 and y.tick_size(999) == 1.0
      and y.tick_size(1000) == 5.0, "tick ladder boundaries")
check(y.marketable_limit(100.0, "buy", 1.0) == 101.0, "buy collar 100 → 101.0 (0.5 tick)")
check(y.marketable_limit(49.9, "buy", 1.0) == 50.4, "buy collar 49.9 → 50.399 crosses into the 0.1-tick band, rounds up to 50.4")
check(y.marketable_limit(100.0, "sell", 1.0) == 99.0, "sell collar 100 → 99.0")
check(y.marketable_limit(100.0, "buy", 20, up_limit=110.0) == 110.0, "buy collar clamped at limit-up")
check(y.marketable_limit(100.0, "sell", 20, down_limit=90.0) == 90.0, "sell collar clamped at limit-down")

tz = y.TZ
check(y.near_month(datetime.datetime(2026, 9, 15, 10, 0, tzinfo=tz)) == 202609, "before 3rd Wed → this month")
check(y.near_month(datetime.datetime(2026, 9, 16, 13, 29, tzinfo=tz)) == 202609, "expiry day before 13:30 → this month")
check(y.near_month(datetime.datetime(2026, 9, 16, 13, 30, tzinfo=tz)) == 202610, "expiry day 13:30 → next month")
check(y.near_month(datetime.datetime(2026, 12, 28, 9, 0, tzinfo=tz)) == 202701, "December rolls into January")
check(y.futures_commodity({}, "TXF") == "FITX" and y.futures_commodity({"yuanta_commodity_TMF": "XX"}, "TMF") == "XX",
      "futures commodity id + .env override")


# ── fake SDK ─────────────────────────────────────────────────────────────────
class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeList(list):
    def Add(self, x):
        self.append(x)


class FakeListFactory:
    def __getitem__(self, cls):
        return FakeList


sent = []          # every order object handed to Send*OrderSync
merge = {}         # order_no → dict of RealReportMerge fields
reply = {"code": 0}
seq = [0]


def fake_call(kind, name, *args):
    if name in ("SendStockOrderSync", "SendFutureOrderSync"):
        order = args[1][0]
        sent.append((name, dict(order.__dict__)))
        if reply["code"]:
            return Obj(ResultCount=Obj(MsgCode="0001", MsgContent="ok", Count=1),
                       ResultList=[Obj(ReplyCode=reply["code"], ErrType="E", ErrNO="A999", Advisory="refused", OrderNO="")])
        seq[0] += 1
        no = f"f{seq[0]:03d}"
        qty = order.OrderQty * (1000 if getattr(order, "APCode", 4) == 0 else 1) if name == "SendStockOrderSync" \
            else order.OrderQty1
        merge[no] = dict(OrderNo=no, OrderQty=qty, OkQty=qty, AvgDealPrice=100.0, OrderStatus=20,
                         LastOrderStatus=8, StkErrorNo="00000", OrderErrorNo="")
        return Obj(ResultCount=Obj(MsgCode="0001", MsgContent="ok", Count=1),
                   ResultList=[Obj(ReplyCode=0, ErrType="", ErrNO="", Advisory="", OrderNO=no)])
    if name == "GetRealReportMergeSync":
        return Obj(RealReportMergeList=[Obj(**m) for m in merge.values()])
    raise AssertionError(f"unexpected call {name}")


y._sdk = Obj(StockOrder=Obj, FutureOrder=Obj, List=FakeListFactory(),
             enumLangType=Obj(UTF8=1))
y._get_api = lambda env: (object(), {"stock": "S98875005091", "futures": "FF021000P001234567"})
y._accounts.update({"stock": "S98875005091", "futures": "FF021000P001234567"})
y._call = fake_call
y._MIN_INTERVAL = {"trade": 0, "query": 0}
y.get_stock_quote = lambda env, s: {"price": 100.0, "market": "TWSE", "ref": 100.0,
                                    "up_limit": 110.0, "down_limit": 90.0}
holdings = {"2330": {"shares": 1500, "sellable": 1200, "avg_price": 90.0, "market_price": 100.0, "value": 150000}}
y.get_stock_holdings = lambda env: holdings
fut_pos = {"TXF": 2}
y.get_futures_positions = lambda env: fut_pos
real_sleep = y.time.sleep
y.time.sleep = lambda s: None

# ── stocks ───────────────────────────────────────────────────────────────────
r = y.place_order_yuanta({}, "2330", 250_000)   # 2500 shares @100 → 2 lots + 500 odd
orders = [o for _, o in sent]
check(len(orders) == 2 and orders[0]["APCode"] == 0 and orders[0]["OrderQty"] == 2
      and orders[0]["PriceFlag"] == "M" and orders[0]["Time_in_force"] == "3",
      "2500 shares → board-lot market IOC with OrderQty in 張 (2)")
check(orders[1]["APCode"] == 4 and orders[1]["OrderQty"] == 500 and orders[1]["Price"] == 101.0
      and orders[1]["Time_in_force"] == "0", "remainder → intraday odd-lot limit 500 shares @ collar price")
check(r["target_qty"] == 2500 and r["filled_qty"] == 2500, "result reports broker fills (2000 + 500)")

sent.clear()
r = y.place_order_yuanta({}, "2330", -500_000)   # wants 5000, sellable 1200
check([o["OrderQty"] for _, o in sent] == [1, 200] and all(o["BuySell"] == "S" for _, o in sent),
      "sell capped at sellable 1200 → 1 lot + 200 odd")
check(y.place_order_yuanta({}, "2330", 50) is False, "diff under one share → False")
check(raises(ValueError, lambda: y.place_stock_order({}, "2330", "buy", 1500)),
      "place_stock_order refuses a mixed size (1500) — split via place_order_yuanta")

# ── futures ──────────────────────────────────────────────────────────────────
sent.clear()
y.place_futures_diff({}, "TXF", -5)   # long 2 → short 3: close 2 then open 3
legs = [(o["BuySell1"], o["OrderQty1"], o["OpenOffsetKind"], o["OrderType"], o["OrderCond"]) for _, o in sent]
check(legs == [("S", 2, "1", "3", "2"), ("S", 3, "0", "3", "2")],
      "flip long 2 → short 3 = 平倉 2 then 新倉 3, range-market IOC")
check(all(o["CommodityID1"] == "FITX" and o["SettlementMonth1"] == y.near_month() for _, o in sent),
      "futures legs use FITX + near month")
check(y.place_futures_diff({}, "TXF", 0) is False, "zero futures diff → False")

# ── confirm_order outcomes ───────────────────────────────────────────────────
merge.clear()
merge["x1"] = dict(OrderNo="x1", OrderQty=1000, OkQty=0, AvgDealPrice=0.0, OrderStatus=10,
                   LastOrderStatus=1, StkErrorNo="12345", OrderErrorNo="")
check(raises(y.YuantaError, lambda: y.confirm_order({}, "stock", "x1", timeout=1)), "委託失敗 with no fill raises")
merge["x2"] = dict(OrderNo="x2", OrderQty=3000, OkQty=1000, AvgDealPrice=99.5, OrderStatus=25,
                   LastOrderStatus=25, StkErrorNo="00000", OrderErrorNo="")
res = y.confirm_order({}, "stock", "x2", timeout=1)
check(res["filled_qty"] == 1000 and res["status"] == "價穩失效", "價穩失效 after a partial fill returns the partial")
merge["x3"] = dict(OrderNo="x3", OrderQty=500, OkQty=0, AvgDealPrice=0.0, OrderStatus=20,
                   LastOrderStatus=0, StkErrorNo="00000", OrderErrorNo="")
res = y.confirm_order({}, "stock", "x3", timeout=0.05)
check(res["status_code"] == 20 and res["filled_qty"] == 0, "accepted but resting at timeout → returned honestly")
check(raises(y.OrderNotConfirmed, lambda: y.confirm_order({}, "stock", "nope", timeout=0.05)),
      "never reported → OrderNotConfirmed")

# ── HALT, duplicate tag, rejection, live gate ────────────────────────────────
guard.trip_halt("test", "check_yuanta_lib")
try:
    sent.clear()
    check(raises(guard.Halted, lambda: y.place_stock_order({}, "2330", "buy", 1000)) and not sent,
          "HALT: stock buy refused before sending")
    y.place_stock_order({}, "2330", "sell", 1000)
    check(len(sent) == 1, "HALT: stock sell still goes out")
    y.place_futures_order({}, "TXF", "sell", 1, reduce_only=True)
    check(len(sent) == 2, "HALT: futures 平倉 still goes out")
finally:
    guard.clear_halt("check_yuanta_lib")

sent.clear()
y.place_stock_order({}, "2330", "buy", 1000, client_tag="abc1")
check(raises(y.DuplicateOrder, lambda: y.place_stock_order({}, "2330", "buy", 1000, client_tag="abc1"))
      and len(sent) == 1, "same client_tag twice in a day → refused locally")

reply["code"] = 7
check(raises(y.YuantaError, lambda: y.place_stock_order({}, "2330", "buy", 1000)), "ReplyCode != 0 raises")
reply["code"] = 0

y._live = True
try:
    check(raises(y.YuantaError, lambda: y.place_stock_order({}, "2330", "buy", 1000)),
          "LIVE board lot without yuanta_units_verified=true is refused")
    sent.clear()
    y.place_stock_order({"yuanta_units_verified": "true"}, "2330", "buy", 1000)
    check(len(sent) == 1, "LIVE board lot passes once units are verified")
finally:
    y._live = False

audit = open("state/audit.jsonl").read()
check('"venue": "yuanta"' in audit and "order_attempt" in audit and "order_denied_halt" in audit,
      "orders are audited (attempt / denied_halt)")

y.time.sleep = real_sleep
print(f"\n{'FAILED' if fails else 'passed'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
