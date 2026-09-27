# Yuanta Securities (元大證券) Broker — Agent Reference

Use this document when a user asks to connect a Yuanta (元大) account. The integration uses
Yuanta's official **SPARK API** (`YuantaSparkAPI.dll`, a .NET 8 assembly loaded through
`pythonnet`), which runs on Linux, Windows and macOS. One connection can log in a **stock
account (證券, `S…`)** and a **futures account (期貨, `F…`)** at the same time.

**Ships implemented: `lib/order_yuanta.py` — never hand-write SPARK API order calls; import from
here.** Read-only onboarding check: `lib/yuanta_probe.py`.

Official docs: https://www.yuanta.com.tw/file-repository/content/API/page/index.html ·
function reference: https://www.yuanta.com.tw/file-repository/content/sparkapi_docs/index.html

Status: implemented from the official docs and the SDK's own type metadata (2026-09); **not yet
run against a live account**. The items marked UNVERIFIED below must be closed in UAT before the
first live order.

---

## Supported Products

| Product | Blave symbol | Order path | Notes |
|---|---|---|---|
| 台股 上市/上櫃 現股 | `'2330'` | `place_order_yuanta(env, symbol, signed_diff_twd)` | Board lots = market IOC; odd remainder = intraday odd-lot (盤中零股) marketable limit |
| 台指期 大台 | `'TXF'` | `place_futures_diff(env, 'TXF', signed_contracts)` | Order id `FITX` + near month (yyyyMM) — FITX is the doc-confirmed id |
| 小台 / 微台 | `'MXF'` / `'TMF'` | same | `FIMTX` / `FITMF` by the FI+TAIFEX pattern — UNVERIFIED, override with `yuanta_commodity_MXF=` / `yuanta_commodity_TMF=` in `.env` |

Not covered: 融資/融券, 當沖 flags, options, spreads, 盤後零股, overseas, conditional orders (條件單).

---

## Step 0 — Scope

Ask the user in one message:
> 你要用元大交易台股、期貨，還是兩個都要？已經有元大的證券/期貨帳戶了嗎？

---

## Step 1 — Apply for API access (the user does this; allow 5–10 business days)

1. Contact their 營業員 and ask to open **元大 API 下單**.
2. **Sign the API 風險預告書 in person at a branch** (ID + seal). No online path.
3. Run Yuanta's **API 測試軟體** (Windows only: https://ys.yuanta.com.tw/Quartet/APITest/setup.exe),
   complete the connection test and upload the test record. This is part of the approval.
4. For the UAT test environment, give the 營業員 the **fixed public IP** the workspace connects from
   (the UAT firewall is IP-whitelisted). On the k0s deployment that is the home router's public IP —
   if the ISP assigns a dynamic IP, UAT breaks whenever it changes. PROD does not document an IP whitelist.

---

## Step 2 — Certificate (.pfx)

Linux/macOS login is `Login(pfx_path, pfx_password, account, password)` — **the certificate is
required on Linux**, unlike the Windows `Login(account, password)`.

- **UAT:** Yuanta publishes a test certificate: https://ys.yuanta.com.tw/quartet/api/B110000005_TWCA.zip
  (password `yuanta`) with the test account `S98875005091` / `1234` (stock only — futures cannot be tested in UAT).
- **PROD:** the user's own 元大 certificate, applied/renewed at https://www.yuanta.com.tw/eYuanta/Securities.
  Ask where their `.pfx` is; they upload it to the workspace (e.g. `credentials/yuanta.pfx`, chmod 600).
  Certificates expire yearly — renewal is a recurring step.

Never ask the user to paste passwords in chat; they write `.env` themselves.

---

## Step 3 — SDK

The k0s image (`deploy/k0s/Dockerfile`, `YUANTA_SDK=1` default) installs the .NET 8 ASP.NET Core
runtime, `pythonnet`, and the vendor's `YuantaSparkAPI_linux-x64_Python.zip` into `/opt/yuanta-sdk`
(sha256-pinned). Elsewhere: install `dotnet` 8 runtime + `pip install pythonnet`, unzip the SDK and set
`yuanta_sdk_dir=` in `.env`. The self-contained runtime bundled in the zip can NOT be used by pythonnet
("Initialization for self-contained components is not supported") — a system .NET 8 is required.

---

## Step 4 — `.env`

```
yuanta_stock_account=S98875005091        # S + branch(4) + account(7); omit if futures only
yuanta_futures_account=FF021000P001234567 # F + branch(7+3) + account(7); omit if stock only
yuanta_password=...                       # 電子交易密碼
yuanta_pfx_path=/opt/blave-agent/workspace/credentials/yuanta.pfx   # absolute
yuanta_pfx_password=...
# YUANTA_LIVE=true                        # PROD — only after Step 6
# yuanta_units_verified=true              # only after Step 6 prints unit "lots (張)"
```

Without `YUANTA_LIVE=true` everything connects to **UAT**.

---

## Step 5 — Read-only probe

```
cd $BLAVE_AGENT_HOME/workspace && python3 lib/yuanta_probe.py
```
Prints JSON: `accounts`, `stock_balance_twd`, `stock_holdings`, `quote_2330`, `futures_equity_twd`,
`futures_positions`, `near_month`. Exit 2 with `stage` = where it failed (`login` = certificate,
password, IP whitelist or API permission; read `error`). Login codes: `0102` 密碼凍結或未啟用,
`0112` 無此權限使用功能. **After a failed login wait ≥4 s before retrying** (Yuanta rule; the lib enforces it)
and never loop logins — 1000 logins/day per account.

---

## Step 6 — Close the UNVERIFIED items in UAT (before any live order)

1. **Board-lot quantity unit.** `StockOrder.OrderQty` is documented only as "委託單位". The lib sends 張
   (so a wrong guess is a rejected 2-share order, never a 1000× order) and **refuses live board-lot orders
   until `yuanta_units_verified=true`**. Run:
   ```
   python3 lib/yuanta_probe.py --order-units
   ```
   (UAT only — refuses when `YUANTA_LIVE=true`.) It places one 1-lot limit buy of 2885 at limit-down,
   reads the reported `OrderQty` and cancels. `unit: "lots (張)"` → tell the user to add
   `yuanta_units_verified=true`. Anything else → stop and report; do not work around it.
   UAT fills are simulated by the **last character of the order number** (broker-assigned), so the probe
   order may fill anyway — UAT money is fake.
2. **Cancel fields** (no official example) — the probe's cancel exercises `cancel_stock_order`; check
   `after_cancel.status == "取消"`.
3. **Futures (MXF/TMF ids, cancel)** cannot be tested in UAT. First live futures order: 1 contract of the
   user's choice with the user watching, then read `get_futures_positions`.

---

## Using the lib

All functions take `env` (dotenv dict) first.

- `place_order_yuanta(env, symbol, signed_diff, client_tag=)` — stock reconciler entry: TWD diff →
  shares at last price (sells capped at 可交易股數), split into ≤499-張 board-lot market IOC orders + one
  odd-lot limit (last ± `yuanta_odd_collar_pct`, default 1%, clamped to the day's limits, ROD). Returns
  `{'orders', 'filled_qty', 'target_qty'}` or `False` under one share.
- `place_stock_order(env, symbol, 'buy'|'sell', shares, client_tag=)` — one order: a multiple of 1000 or 1–999.
- `place_futures_diff(env, 'TXF', signed_contracts, client_tag=)` — futures reconciler entry: the part that
  shrinks the position goes as 平倉 (passes HALT), the rest as 新倉; a flip is close-then-open. 範圍市價 IOC.
- `place_futures_order(env, symbol, action, contracts, reduce_only, settlement_month=None)` — one order.
- `get_yuanta_positions(env)` → `{symbol: {'side': 'long', 'size': TWD}}` (sinopac shape);
  `get_stock_holdings(env)`; `get_futures_positions(env)` → `{'TXF': net contracts}`;
  `get_account_balance(env)` (bank balance TWD); `get_futures_equity(env)` (權益數).
- `get_order(env, 'stock'|'futures', order_no)`, `cancel_stock_order`, `cancel_futures_order`.

Results are the broker's numbers (`filled_qty` = OkQty, `avg_fill_price` = AvgDealPrice,
`status` = 委託成功/取消/價穩失效…) — report those, never the intent. Exceptions: `YuantaError`
(rejected — message has the broker's code), `OrderNotConfirmed` (sent, never reported — call `get_order`
before resubmitting), `DuplicateOrder` (client_tag reused today), `guard.Halted`.

Every order passes `_send()`: restart stop → HALT (entries only) → audit. Orders outside trading hours:
stocks rest or expire; `status_code 5` = 預約單.

---

## API facts worth knowing (from the SDK, 2026-09)

- Every plain call is async (result in the `OnResponse` event), but the DLL also has undocumented
  `*Sync` variants returning `OnResponse<T>` {`Success`, `ErrorMessage`, `objValue`} — the lib uses those.
  `Login` has no Sync variant (awaited via the event).
- Order ack (`ReplyCode 0`, `OrderNO`) is broker acceptance only; exchange state comes from
  `GetRealReportMerge`: `OrderStatus` 0 傳輸中 / 5 預約單 / 10 委託失敗 / 20 委託成功 / 24 委託失效 /
  25 價穩失效 / 30 取消; `OkQty`, `AvgDealPrice`, `OrderQty` (shares or contracts, includes filled).
  The per-event `RealReport.OrderStatus` table uses the SAME numbers for different meanings (20 = 改價成功).
- Rate limits: trade 10/s and ≤30 orders per call, query 3/s per function, GetKline 1/s. Exceeding a
  per-minute limit suspends service 1 min; **10 suspensions in an hour stop the account's API access**.
- Futures order code (`FITX` + `SettlementMonth1` yyyyMM) ≠ quote code (`TXF…`).
- `enumEnvironmentMode`: `UAT` = 1, `PROD` = 2 (the web docs list them reversed — use the names).
