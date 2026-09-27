"""
Yuanta (元大證券) order execution — Taiwan stocks + TAIFEX futures via the
YuantaSparkAPI (.NET 8 assembly loaded through pythonnet; Linux/Windows/macOS).

Companion to references/yuanta-broker.md (API application, certificate, UAT).
Taiwan-broker shape (follows lib/order_sinopac.py, not the perp template):
every function takes `env` (dotenv dict) first.

Credentials in .env:
    yuanta_stock_account    S + branch(4) + account(7), e.g. S98875005091
    yuanta_futures_account  F + branch(7+3) + account(7), e.g. FF021000P001234567
    yuanta_password         電子交易密碼
    yuanta_pfx_path         absolute path of the .pfx certificate (Linux/macOS login needs it)
    yuanta_pfx_password     certificate password
    YUANTA_LIVE=true        PROD. Without it every call goes to the UAT test environment
                            (stock-only, needs a fixed IP registered with the broker).
    yuanta_units_verified=true   required for LIVE stock orders — set only after
                            lib/yuanta_probe.py --order-units passed in UAT (see below).
    yuanta_odd_collar_pct   odd-lot limit collar around last price, default 1.0 (%)
    yuanta_sdk_dir          SDK folder (default $YUANTA_SDK_DIR or /opt/yuanta-sdk)

Design rules (same as the other TW brokers):
1. FAIL LOUD. The SDK's plain calls only say "request sent"; this lib uses the
   *Sync variants (OnResponse<T>: Success/ErrorMessage/objValue) and then polls
   GetRealReportMerge until the order reaches a state the exchange reported.
   ReplyCode != 0, MsgCode != 0001, or order state 10/24/25/30 with no fill raise.
2. CONFIRMED. Returned fills are the broker's OkQty / AvgDealPrice, never the intent.
3. IDEMPOTENT-ISH. client_tag (≤8 alnum) is recorded per trading day in
   state/yuanta_tags.json; the same tag again that day is refused before sending.
4. HALTABLE + AUDITED. Every order passes _send(): guard.check_restart_stop first,
   then state/HALT for entries, then state/audit.jsonl. Closes/reduces pass HALT.

UNVERIFIED on a live account (documented UNCLEAR by Yuanta, see the reference):
- StockOrder.OrderQty unit for board lots (APCode 0). This lib sends 張 (lots): if
  the unit were shares, a 2-lot order becomes 2 shares and the exchange rejects it —
  the failure mode is a rejection, never a 1000x order. Odd lots (APCode 4) send shares.
- Futures commodity ids other than FITX (MXF→FIMTX, TMF→FITMF follow the FI+TAIFEX
  code pattern); override with yuanta_commodity_<SYMBOL> in .env.
- Cancel field requirements (no official example): cancel_* sends the original
  order's fields with TradeKind/FunctionCode 4.
"""

import datetime
import json
import logging
import math
import os
import sys
import threading
import time
from zoneinfo import ZoneInfo

from lib import guard

guard.mark_money_process()  # Stop in the chat never kills this process (lib/guard)

TZ = ZoneInfo("Asia/Taipei")
BOARD_LOT = 1000
MAX_ODD_LOT_SHARES = 999
MAX_LOTS_PER_ORDER = 499            # TWSE per-order cap for board lots
MAX_FUT_CONTRACTS_PER_ORDER = 100   # conservative; TAIFEX market-order cap for TX is 10 during some sessions

# RealReportMerge.OrderStatus (order state — NOT the RealReport event table)
ST_SENDING, ST_RESERVED, ST_FAILED, ST_ACCEPTED, ST_EXPIRED, ST_PRICE_STAB, ST_CANCELLED = 0, 5, 10, 20, 24, 25, 30
TERMINAL = {ST_FAILED, ST_EXPIRED, ST_PRICE_STAB, ST_CANCELLED}
STATUS_NAME = {0: "傳輸中", 5: "預約單", 10: "委託失敗", 20: "委託成功", 24: "委託失效",
               25: "價穩失效", 30: "取消"}

# SYMBOL (Blave's instrument name) → TAIFEX product code; order id = "FI" + code
FUTURES_PRODUCTS = {"TXF": "TX", "MXF": "MTX", "TMF": "TMF"}

# Rate limits (1.前言/3.使用限制說明): trade 10/s, query 3/s per function; 1 login attempt / 4 s
_MIN_INTERVAL = {"trade": 0.15, "query": 0.4}
LOGIN_RETRY_S = 4.5

TAGS_PATH = "state/yuanta_tags.json"

_api = None
_accounts = {}          # {"stock": "S…", "futures": "F…"} — logged in
_live = None
_sdk = None             # namespace of imported YuantaOneAPI names
_lock = threading.RLock()
_last_call = {}
_login_waiters = {}
_last_login_fail = 0.0


class YuantaError(Exception):
    """The broker rejected a request or reported an unexpected state."""


class OrderNotConfirmed(Exception):
    """Sent, but no exchange state within the timeout. It MAY still go through —
    query again (get_order) before resubmitting."""


class DuplicateOrder(Exception):
    """This client_tag was already used today — refused locally."""


# ── SDK loading / connection ─────────────────────────────────────────────────

def _load_sdk(env):
    """Import the .NET assembly once per process. Needs the .NET 8 (ASP.NET Core)
    runtime + pythonnet; the Docker image installs both (deploy/k0s/Dockerfile)."""
    global _sdk
    if _sdk is not None:
        return _sdk
    sdk_dir = env.get("yuanta_sdk_dir") or os.environ.get("YUANTA_SDK_DIR") or "/opt/yuanta-sdk"
    if not os.path.isfile(os.path.join(sdk_dir, "YuantaSparkAPI.dll")):
        raise YuantaError(f"YuantaSparkAPI.dll not found in {sdk_dir} — see references/yuanta-broker.md Step 3")
    from pythonnet import load
    try:
        load("coreclr")
    except RuntimeError:
        pass  # already loaded in this process
    import clr
    if sdk_dir not in sys.path:
        sys.path.append(sdk_dir)
    clr.AddReference("YuantaSparkAPI")
    from System.Collections.Generic import List
    import YuantaOneAPI as Y
    _sdk = type("YuantaSDK", (), {})()
    for name in ("YuantaSparkAPITrader", "enumEnvironmentMode", "enumLogType", "enumLangType",
                 "enumMarketType", "StockOrder", "FutureOrder", "Quote"):
        setattr(_sdk, name, getattr(Y, name))
    _sdk.List = List
    return _sdk


def _on_response(intMark, dwIndex, strIndex, objHandle, objValue):
    """Async channel: only Login is awaited here (it has no *Sync variant); the
    rest is logged. Pushes (intMark 2: RR_RealReport…) are not needed — order
    state is polled through GetRealReportMergeSync."""
    try:
        if strIndex == "Login":
            status = objValue.LoginStatus
            accts = [str(d.Account) for d in (objValue.LoginList or [])]
            for acct, waiter in list(_login_waiters.items()):
                if acct in accts or not accts:
                    waiter["result"] = (str(status.MsgCode), str(status.MsgContent), accts)
                    waiter["event"].set()
        elif intMark == 0 or not strIndex:
            logging.info(f"yuanta: {objValue}")
    except Exception as e:  # never raise into the .NET event loop
        logging.error(f"yuanta OnResponse handler: {e}")


def _login(api, env, account):
    global _last_login_fail
    wait = LOGIN_RETRY_S - (time.time() - _last_login_fail)
    if wait > 0:
        time.sleep(wait)  # Yuanta: after a failed login, at most one attempt per 4 s
    password = env.get("yuanta_password", "")
    pfx, pfx_pw = env.get("yuanta_pfx_path", ""), env.get("yuanta_pfx_password", "")
    if not password:
        raise YuantaError("yuanta_password missing from .env")
    waiter = {"event": threading.Event(), "result": None}
    _login_waiters[account] = waiter
    try:
        if sys.platform == "win32":
            sent = api.Login(account, password)
        else:
            if not pfx or not os.path.isabs(pfx) or not os.path.isfile(pfx):
                raise YuantaError(f"yuanta_pfx_path must be an absolute path to the .pfx (got {pfx!r})")
            sent = api.Login(pfx, pfx_pw, account, password)
        if not sent:
            raise YuantaError(f"Login request for {account} was not sent")
        if not waiter["event"].wait(30):
            raise YuantaError(f"no Login response for {account} within 30 s")
        code, msg, accts = waiter["result"]
        if code not in ("0001", "00001"):
            raise YuantaError(f"login {account} refused: {code} {msg}")
        logging.info(f"yuanta login OK: {account} ({msg})")
    except Exception:
        _last_login_fail = time.time()
        raise
    finally:
        _login_waiters.pop(account, None)


def _get_api(env):
    """Process-wide singleton: one connection, each configured account logged in
    exactly once (re-login of a logged-in account is forbidden by Yuanta)."""
    global _api, _live
    with _lock:
        if _api is not None:
            return _api, _accounts
        sdk = _load_sdk(env)
        _live = str(env.get("YUANTA_LIVE", "")).lower() == "true"
        wanted = {k: env.get(f"yuanta_{k}_account", "").strip() for k in ("stock", "futures")}
        wanted = {k: v for k, v in wanted.items() if v}
        if not wanted:
            raise YuantaError("neither yuanta_stock_account nor yuanta_futures_account is set in .env")
        log_dir = os.path.abspath(os.path.join("state", "yuanta_log"))
        os.makedirs(log_dir, exist_ok=True)
        api = sdk.YuantaSparkAPITrader(log_dir)
        api.SetLogType(sdk.enumLogType.System)
        api.OnResponse += _on_response
        api.Open(sdk.enumEnvironmentMode.PROD if _live else sdk.enumEnvironmentMode.UAT)
        time.sleep(2)  # the SDK has no connect event; its own samples wait 2 s
        try:
            for kind, acct in wanted.items():
                _login(api, env, acct)
                _accounts[kind] = acct
        except Exception:
            try:
                api.Close()
                api.Dispose()
            except Exception:
                pass
            _accounts.clear()
            raise
        _api = api
        return _api, _accounts


def reset_connection():
    """Drop the session (e.g. after changing YUANTA_LIVE or credentials in .env)."""
    global _api
    with _lock:
        if _api is not None:
            for step in ("LogOut", "Close", "Dispose"):
                try:
                    getattr(_api, step)()
                except Exception:
                    pass
        _api = None
        _accounts.clear()


def _account(env, kind):
    _, accts = _get_api(env)
    acct = accts.get(kind)
    if not acct:
        raise YuantaError(f"yuanta_{kind}_account is not configured in .env")
    return acct


def _call(kind, name, *args):
    """Throttled *Sync call → objValue, raising with the SDK's ErrorMessage."""
    with _lock:
        gap = _MIN_INTERVAL[kind] - (time.time() - _last_call.get(name, 0.0))
        if gap > 0:
            time.sleep(gap)
        _last_call[name] = time.time()
        r = getattr(_api, name)(*args)
    if r is None or not r.Success:
        raise YuantaError(f"{name} failed: {getattr(r, 'ErrorMessage', None) or 'no response'}")
    return r.objValue


def _lang():
    return _sdk.enumLangType.UTF8


def _today():
    return datetime.datetime.now(TZ).strftime("%Y/%m/%d")


# ── market data ──────────────────────────────────────────────────────────────

def _quotes(env, codes_by_market):
    """[(enumMarketType name, code)] → {code: QueryWatchList row}, first market with a name wins."""
    _get_api(env)
    acct = _accounts.get("stock") or _accounts.get("futures")
    lst = _sdk.List[_sdk.Quote]()
    for market, code in codes_by_market:
        q = _sdk.Quote()
        q.MarketType = getattr(_sdk.enumMarketType, market)
        q.StockCode = code
        lst.Add(q)
    res = _call("query", "GetWatchListAllSync", acct, lst, _lang(), False)
    out = {}
    for row in res.QueryWatchList or []:
        code = str(row.StkCode).strip()
        if code and code not in out and (str(row.StkName or "").strip() or float(row.DealPrice) > 0):
            out[code] = row
    return out


def get_stock_quote(env, symbol):
    """{'price', 'market', 'up_limit', 'down_limit', 'ref'} for a listed/OTC stock.
    price = last trade, else yesterday's close before the open. Raises if unknown."""
    row = _quotes(env, [("TWSE", symbol), ("TWOTC", symbol)]).get(symbol)
    if row is None:
        raise YuantaError(f"no quote for stock {symbol} on TWSE/TWOTC")
    price = float(row.DealPrice) or float(row.YstPrice) or float(row.OpenRefPrice)
    if price <= 0:
        raise YuantaError(f"no valid price for {symbol}")
    return {"price": price, "market": str(row.MarketNo), "ref": float(row.YstPrice),
            "up_limit": float(row.UpStopPrice), "down_limit": float(row.DownStopPrice)}


def tick_size(price):
    """TWSE/TPEx stock tick ladder."""
    for bound, tick in ((10, 0.01), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1.0)):
        if price < bound:
            return tick
    return 5.0


def marketable_limit(price, side, collar_pct, up_limit=0.0, down_limit=0.0):
    """Limit price collar_pct beyond `price`, on the tick grid, inside the day's limits."""
    raw = price * (1 + collar_pct / 100) if side == "buy" else price * (1 - collar_pct / 100)
    t = tick_size(raw)
    px = math.ceil(raw / t - 1e-9) * t if side == "buy" else math.floor(raw / t + 1e-9) * t
    if side == "buy" and up_limit > 0:
        px = min(px, up_limit)
    if side == "sell" and down_limit > 0:
        px = max(px, down_limit)
    return round(px, 2)


def near_month(now=None):
    """TAIFEX near-month settlement (yyyyMM): contracts expire the 3rd Wednesday at 13:30."""
    now = now or datetime.datetime.now(TZ)
    first = now.replace(day=1)
    third_wed = 1 + (2 - first.weekday()) % 7 + 14
    expired = now.day > third_wed or (now.day == third_wed and (now.hour, now.minute) >= (13, 30))
    y, m = (now.year, now.month + 1) if expired else (now.year, now.month)
    if m == 13:
        y, m = y + 1, 1
    return y * 100 + m


def futures_commodity(env, symbol):
    override = env.get(f"yuanta_commodity_{symbol}")
    if override:
        return override
    if symbol not in FUTURES_PRODUCTS:
        raise YuantaError(f"unknown futures SYMBOL {symbol} (known: {sorted(FUTURES_PRODUCTS)})")
    return "FI" + FUTURES_PRODUCTS[symbol]


# ── account ──────────────────────────────────────────────────────────────────

def get_stock_holdings(env):
    """{symbol: {'shares', 'sellable', 'avg_price', 'market_price', 'value'}} — 現股 only."""
    acct = _account(env, "stock")
    res = _call("query", "GetStoreSummarySync", acct, _lang())
    out = {}
    for s in res.StkStoreList or []:
        if int(s.TradeKind) != 0:  # 0 現股; 3 資買 / 4 券賣 / 6 借券 are not this lib's positions
            continue
        sym, qty = str(s.StkCode).strip(), int(s.StockQty)
        if not sym or qty <= 0:
            continue
        mp = float(s.MarketPrice)
        out[sym] = {"shares": qty, "sellable": int(s.TradingQty), "avg_price": float(s.Price),
                    "market_price": mp, "value": qty * mp}
    return out


def get_yuanta_positions(env):
    """{symbol: {'side': 'long', 'size': TWD}} — reconciler shape (same as sinopac)."""
    return {sym: {"side": "long", "size": h["value"]} for sym, h in get_stock_holdings(env).items()}


def get_futures_positions(env):
    """{SYMBOL: net contracts (+long / −short)} for the near and far months of TXF/MXF/TMF,
    keyed by Blave SYMBOL. Options and spreads are ignored."""
    acct = _account(env, "futures")
    res = _call("query", "GetFutStoreSummarySync", acct, _lang())
    by_commodity = {futures_commodity(env, s): s for s in FUTURES_PRODUCTS}
    out = {}
    for f in res.FutStoreList or []:
        if str(f.Kind).strip() != "F" or str(f.Commodity2 or "").strip():
            continue
        sym = by_commodity.get(str(f.Commodity1).strip())
        if sym is None:
            continue
        qty = int(f.Qty) * (1 if str(f.BS).strip() == "B" else -1)
        out[sym] = out.get(sym, 0) + qty
    return {k: v for k, v in out.items() if v}


def get_account_balance(env):
    """Stock settlement bank balance (TWD). Raises instead of returning 0."""
    acct = _account(env, "stock")
    res = _call("query", "GetBankBalanceSync", acct, _lang())
    rows = list(res.BankBalanceList or [])
    if not rows:
        raise YuantaError("GetBankBalance returned no rows")
    if str(rows[0].Message or "").strip():
        raise YuantaError(f"GetBankBalance: {rows[0].Message}")
    return float(rows[0].AvailableBalance)


def get_futures_equity(env):
    """Futures account equity (權益數, TWD)."""
    acct = _account(env, "futures")
    res = _call("query", "GetFutInterestStoreSync", acct, "1", "TWD", _lang())
    if int(res.ReplyCode) != 0:
        raise YuantaError(f"GetFutInterestStore: {res.ReplyCode} {res.Advisory}")
    return float(res.Equity)


# ── order state ──────────────────────────────────────────────────────────────

def _normalize(m):
    order_qty, ok = int(m.OrderQty), int(m.OkQty)
    code = int(m.OrderStatus)
    return {
        "order_no": str(m.OrderNo).strip(),
        "status_code": code,
        "status": STATUS_NAME.get(code, str(code)),
        "last_event": int(m.LastOrderStatus),
        "order_qty": order_qty,          # 股數/口數, includes filled
        "filled_qty": ok,
        "avg_fill_price": float(m.AvgDealPrice),
        "error": (str(m.StkErrorNo or "").strip("0 ") or str(m.OrderErrorNo or "").strip()),
        "live": bool(_live),
    }


def get_order(env, kind, order_no):
    """Today's merged state of one order (kind 'stock'|'futures'), or None if not reported yet."""
    acct = _account(env, kind)
    res = _call("query", "GetRealReportMergeSync", acct, _lang())
    for m in res.RealReportMergeList or []:
        if str(m.OrderNo).strip() == order_no.strip():
            return _normalize(m)
    return None


def confirm_order(env, kind, order_no, timeout=90):
    """Poll until filled, or a terminal state, or timeout.
    Filled → result. Terminal with a partial fill → result (status says why the rest stopped).
    Terminal with no fill → YuantaError. Accepted but resting at timeout → result (honest).
    Never reported → OrderNotConfirmed."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = get_order(env, kind, order_no) or last
        if last:
            code, filled = last["status_code"], last["filled_qty"]
            if last["order_qty"] > 0 and filled >= last["order_qty"]:
                return last
            if code in TERMINAL:
                if filled > 0:
                    return last
                raise YuantaError(f"order {order_no} {last['status']} "
                                  f"(error {last['error'] or '-'}), nothing filled")
            if code == ST_RESERVED:
                return last  # 預約單: accepted for the next session
        time.sleep(2)
    if last is None or last["status_code"] == ST_SENDING:
        raise OrderNotConfirmed(f"order {order_no} not acknowledged after {timeout}s — "
                                f"check get_order() before resubmitting")
    return last


# ── idempotency ledger ───────────────────────────────────────────────────────

def _tags_today():
    try:
        with open(TAGS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        data = {}
    return data, data.get(datetime.datetime.now(TZ).strftime("%Y-%m-%d"), {})


def _record_tag(tag, order_no):
    data, _ = _tags_today()
    day = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
    data = {day: {**data.get(day, {}), tag: order_no}}  # keep today only
    os.makedirs(os.path.dirname(TAGS_PATH), exist_ok=True)
    tmp = TAGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, TAGS_PATH)


# ── the one order path ───────────────────────────────────────────────────────

def _send(env, kind, fields, intent, client_tag=None, confirm_timeout=90):
    """Every order (stock or futures, new or cancel) goes through here.
    kind: 'stock'|'futures'; fields: StockOrder/FutureOrder attributes;
    intent: 'entry'|'reduce'|'cancel' (cancels pass the restart and HALT gates)."""
    audit = {"venue": "yuanta", "kind": kind, "intent": intent,
             **{k: v for k, v in fields.items() if k not in ("Account", "TradeDate")},
             **({"client_tag": client_tag} if client_tag else {})}
    guard.check_restart_stop(intent, {**audit, "symbol": fields.get("StkCode") or fields.get("CommodityID1")})
    if intent == "entry" and guard.halted():
        guard.audit("order_denied_halt", **audit)
        raise guard.Halted(f"state/HALT is set ({guard.halt_info()}) — yuanta entry order refused "
                           f"before reaching the broker. Closes still work; only the user clears the halt.")
    if client_tag:
        _, today = _tags_today()
        if client_tag in today:
            raise DuplicateOrder(f"client_tag {client_tag} already used today (order {today[client_tag]})")

    acct = _account(env, kind)
    if _live and kind == "stock" and fields.get("APCode") == 0 and intent != "cancel" \
            and str(env.get("yuanta_units_verified", "")).lower() != "true":
        raise YuantaError("live board-lot orders need yuanta_units_verified=true in .env — run "
                          "lib/yuanta_probe.py --order-units in UAT first (references/yuanta-broker.md)")
    cls = _sdk.StockOrder if kind == "stock" else _sdk.FutureOrder
    order = cls()
    for k, v in {**fields, "Account": acct, "TradeDate": _today(), "Identify": 1}.items():
        setattr(order, k, v)
    lst = _sdk.List[cls]()
    lst.Add(order)
    audit["live"] = bool(_live)
    guard.audit("order_attempt", **audit)
    try:
        res = _call("trade", "SendStockOrderSync" if kind == "stock" else "SendFutureOrderSync",
                    acct, lst, _lang())
        status, rows = res.ResultCount, list(res.ResultList or [])
        if str(status.MsgCode) not in ("0001", "00001") or not rows:
            raise YuantaError(f"order refused: {status.MsgCode} {status.MsgContent}")
        row = rows[0]
        if int(row.ReplyCode) != 0:
            raise YuantaError(f"order refused: {row.ErrType} {row.ErrNO} {row.Advisory}")
        order_no = str(row.OrderNO).strip()
        if client_tag:
            _record_tag(client_tag, order_no)
        if intent == "cancel":
            result = {"order_no": order_no, "status": "cancel sent"}
        else:
            result = confirm_order(env, kind, order_no, timeout=confirm_timeout)
    except Exception as e:
        guard.audit("order_error", error=str(e), **audit)
        raise
    guard.audit("order_ok", **{**audit, **{k: result.get(k) for k in
                                           ("order_no", "status", "filled_qty", "avg_fill_price")}})
    return result


# ── stocks ───────────────────────────────────────────────────────────────────

def place_stock_order(env, symbol, action, shares, client_tag=None, confirm_timeout=90):
    """One confirmed stock order for `shares` (現股, long-only).
    Multiples of 1000 → board-lot market IOC (APCode 0, OrderQty in 張, ≤499 張).
    1–999 → intraday odd-lot (APCode 4) marketable limit, ROD (odd lots take no market orders).
    Mixed sizes: use place_order_yuanta, which splits."""
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")
    shares = int(shares)
    intent = "entry" if action == "buy" else "reduce"
    guard.check_restart_stop(intent, {"venue": "yuanta", "symbol": symbol, "action": action, "shares": shares})
    if shares >= BOARD_LOT and shares % BOARD_LOT == 0:
        lots = shares // BOARD_LOT
        if lots > MAX_LOTS_PER_ORDER:
            raise ValueError(f"{lots} lots exceeds the per-order cap {MAX_LOTS_PER_ORDER}")
        fields = {"APCode": 0, "TradeKind": 0, "OrderType": "0", "StkCode": symbol,
                  "BuySell": "B" if action == "buy" else "S", "PriceFlag": "M", "Price": 0.0,
                  "OrderQty": lots, "Time_in_force": "3", "BasketNo": client_tag or "", "OrderNo": ""}
    elif 1 <= shares <= MAX_ODD_LOT_SHARES:
        q = get_stock_quote(env, symbol)
        collar = float(env.get("yuanta_odd_collar_pct") or 1.0)
        price = marketable_limit(q["price"], action, collar, q["up_limit"], q["down_limit"])
        fields = {"APCode": 4, "TradeKind": 0, "OrderType": "0", "StkCode": symbol,
                  "BuySell": "B" if action == "buy" else "S", "PriceFlag": " ", "Price": price,
                  "OrderQty": shares, "Time_in_force": "0", "BasketNo": client_tag or "", "OrderNo": ""}
    else:
        raise ValueError(f"shares must be 1-999 or a multiple of 1000, got {shares}")
    return _send(env, "stock", fields, intent, client_tag=client_tag, confirm_timeout=confirm_timeout)


def place_order_yuanta(env, symbol, signed_diff, client_tag=None, confirm_timeout=90):
    """Reconciler entry point for stocks. signed_diff in TWD (>0 buy, <0 sell).
    shares = floor(|diff| / last price); sells are capped at the sellable quantity.
    Board lots and the odd remainder go as separate orders. Returns
    {'orders', 'filled_qty', 'target_qty', …} or False when under one share."""
    action = "buy" if signed_diff > 0 else "sell"
    guard.check_restart_stop("entry" if action == "buy" else "reduce",
                             {"venue": "yuanta", "symbol": symbol, "signed_diff": signed_diff})
    price = get_stock_quote(env, symbol)["price"]
    target = int(abs(signed_diff) / price)
    if action == "sell":
        sellable = get_stock_holdings(env).get(symbol, {}).get("sellable", 0)
        if target > sellable:
            logging.warning(f"yuanta sell {symbol}: {target} shares capped at sellable {sellable}")
            target = sellable
    if target < 1:
        logging.info(f"yuanta skip {symbol}: diff {abs(signed_diff):.0f} TWD < 1 share @ {price}")
        return False
    chunks = []
    lots = target // BOARD_LOT
    while lots > 0:
        n = min(lots, MAX_LOTS_PER_ORDER)
        chunks.append(n * BOARD_LOT)
        lots -= n
    if target % BOARD_LOT:
        chunks.append(target % BOARD_LOT)
    results = []
    for i, shares in enumerate(chunks):
        tag = None if not client_tag else (client_tag if i == 0 else f"{client_tag[:7]}{chr(ord('a') + i - 1)}")
        results.append(place_stock_order(env, symbol, action, shares, client_tag=tag,
                                         confirm_timeout=confirm_timeout))
    return {"symbol": symbol, "action": action, "target_qty": target,
            "filled_qty": sum(r["filled_qty"] for r in results), "orders": results}


def cancel_stock_order(env, order_no):
    """Cancel a resting stock order (TradeKind 4). UNVERIFIED field set — confirm the
    result with get_order(env, 'stock', order_no)."""
    m = _find_merge_raw(env, "stock", order_no)
    fields = {"APCode": int(m.APCode), "TradeKind": 4, "OrderType": "0", "StkCode": str(m.CompanyNo).strip(),
              "BuySell": str(m.BS).strip(), "PriceFlag": " ", "Price": float(m.Price),
              "OrderQty": 0, "Time_in_force": "0", "BasketNo": "", "OrderNo": order_no}
    return _send(env, "stock", fields, "cancel")


def _find_merge_raw(env, kind, order_no):
    acct = _account(env, kind)
    for m in _call("query", "GetRealReportMergeSync", acct, _lang()).RealReportMergeList or []:
        if str(m.OrderNo).strip() == order_no.strip():
            return m
    raise YuantaError(f"{kind} order {order_no} not found today")


# ── futures ──────────────────────────────────────────────────────────────────

def place_futures_order(env, symbol, action, contracts, reduce_only, settlement_month=None,
                        client_tag=None, confirm_timeout=60):
    """One confirmed TAIFEX order: 範圍市價 (OrderType 3) IOC, explicit 新倉/平倉.
    symbol is Blave's SYMBOL ('TXF'|'MXF'|'TMF'); settlement_month yyyyMM, default near month."""
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")
    contracts = int(contracts)
    if not 1 <= contracts <= MAX_FUT_CONTRACTS_PER_ORDER:
        raise ValueError(f"contracts must be 1-{MAX_FUT_CONTRACTS_PER_ORDER}, got {contracts}")
    intent = "reduce" if reduce_only else "entry"
    guard.check_restart_stop(intent, {"venue": "yuanta", "symbol": symbol, "action": action,
                                      "contracts": contracts})
    fields = {"FunctionCode": 0, "CommodityID1": futures_commodity(env, symbol), "CallPut1": "",
              "SettlementMonth1": int(settlement_month or near_month()), "StrikePrice1": 0.0,
              "Price": 0.0, "OrderQty1": contracts, "BuySell1": "B" if action == "buy" else "S",
              "CommodityID2": "", "CallPut2": "", "SettlementMonth2": 0, "StrikePrice2": 0.0,
              "OrderQty2": 0, "BuySell2": "", "OpenOffsetKind": "1" if reduce_only else "0",
              "DayTradeID": " ", "OrderType": "3", "OrderCond": "2", "SellerNo": 0,
              "BasketNo": "", "Session": " ", "OrderNo": ""}
    return _send(env, "futures", fields, intent, client_tag=client_tag, confirm_timeout=confirm_timeout)


def place_futures_diff(env, symbol, signed_diff, client_tag=None, confirm_timeout=60):
    """Reconciler entry point for futures. signed_diff in CONTRACTS (>0 buy, <0 sell).
    The part that shrinks the current position is sent as 平倉 (passes HALT); the rest
    as 新倉 — so a long→short flip is a close then an open. Returns
    {'orders', 'filled_qty', 'target_qty'} or False when the diff is 0."""
    diff = int(signed_diff)
    if diff == 0:
        return False
    action = "buy" if diff > 0 else "sell"
    guard.check_restart_stop("reduce", {"venue": "yuanta", "symbol": symbol, "signed_diff": diff})
    pos = get_futures_positions(env).get(symbol, 0)
    closing = min(abs(diff), abs(pos)) if pos and (pos > 0) != (diff > 0) else 0
    legs = [(closing, True), (abs(diff) - closing, False)]
    results = []
    for i, (n, reduce_only) in enumerate([leg for leg in legs if leg[0] > 0]):
        while n > 0:
            k = min(n, MAX_FUT_CONTRACTS_PER_ORDER)
            tag = None if not client_tag else (client_tag if not results else f"{client_tag[:7]}{chr(ord('a') + len(results) - 1)}")
            results.append(place_futures_order(env, symbol, action, k, reduce_only,
                                               client_tag=tag, confirm_timeout=confirm_timeout))
            n -= k
    return {"symbol": symbol, "action": action, "target_qty": abs(diff),
            "filled_qty": sum(r["filled_qty"] for r in results), "orders": results}


def cancel_futures_order(env, order_no):
    """Cancel a resting futures order (FunctionCode 4). UNVERIFIED field set."""
    m = _find_merge_raw(env, "futures", order_no)
    trade_code = str(m.TradeCode).split()
    fields = {"FunctionCode": 4, "CommodityID1": trade_code[0] if trade_code else "", "CallPut1": "",
              "SettlementMonth1": int(trade_code[1]) if len(trade_code) > 1 else 0, "StrikePrice1": 0.0,
              "Price": float(m.Price), "OrderQty1": 0, "BuySell1": str(m.BS).strip(),
              "CommodityID2": "", "CallPut2": "", "SettlementMonth2": 0, "StrikePrice2": 0.0,
              "OrderQty2": 0, "BuySell2": "", "OpenOffsetKind": str(m.OpenOffsetKind).strip() or "2",
              "DayTradeID": " ", "OrderType": "2", "OrderCond": " ", "SellerNo": 0,
              "BasketNo": "", "Session": " ", "OrderNo": order_no}
    return _send(env, "futures", fields, "cancel")
