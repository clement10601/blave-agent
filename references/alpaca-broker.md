# Alpaca (US equities) Broker — Agent Reference

Use this document when a user wants to trade US stocks through Alpaca. **Ships implemented:
`lib/order_alpaca.py` + `lib/account_alpaca.py` — never hand-write Alpaca order calls; import from
here.** Onboarding check: `lib/alpaca_probe.py`. API docs: https://docs.alpaca.markets/

Paper trading is the default: without `ALPACA_LIVE=true` every call goes to
`paper-api.alpaca.markets` (paper keys only work there, live keys only on `api.alpaca.markets`).

---

## Step 1 — Keys

The user creates a **paper** key pair in the Alpaca dashboard (Paper Trading → API Keys) and writes
them into the workspace `.env` themselves — never ask them to paste keys into chat:
```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
# ALPACA_LIVE=true          # live money — only on the user's explicit decision
# ALPACA_DATA_FEED=sip      # default iex (free); sip needs a paid market-data plan
```

## Step 2 — Probe

```
cd $BLAVE_AGENT_HOME/workspace && python3 lib/alpaca_probe.py
python3 lib/alpaca_probe.py --paper-order     # paper only: 1-share limit at 50% of last, then cancel
```
`account.status` must be `ACTIVE`, `trading_blocked` false. 401/403 = wrong key or a live key on paper.

---

## Using the lib

Units are **whole shares** (fractional orders cannot short or trade extended hours). All functions
take `env` first.

- Reconciler contract (lib/order_TEMPLATE.py): `get_contract_rules`, `format_qty`,
  `place_market_order(env, symbol, direction, qty, reduce_only=)`, `close_position_partial`.
  Market orders are **regular session only** — refused while the market is closed (Alpaca would
  otherwise queue them to the open).
- `place_limit_order(env, symbol, 'buy'|'sell', qty, limit_price, extended_hours=False)` —
  `extended_hours=True` for pre-market (04:00–09:30 ET) / after-hours (16:00–20:00 ET); limit + day only.
  Returns the order after `timeout` s, which may still be working (`status` new/partially_filled).
- `place_bracket_order(env, symbol, side, qty, stop_loss, take_profit, limit_price=None)` — entry with
  OCO stop/target children; **regular session only** (Alpaca does not run brackets in extended hours).
- `set_position(env, symbol, target_qty, extended_hours=False)` — signed target; shrinking part first
  (passes HALT), flips as close-then-open (Alpaca rejects orders that cross zero).
- `cancel_order`, `cancel_all_orders`, `close_position`, `get_order`, `confirm_order`,
  `get_position_qty`, `get_latest_quote` (bid/ask/last), `get_clock`, `get_asset`.
- Account: `get_equity`, `get_positions` (`side` + abs `size`), `get_todays_pl` (equity − last_equity,
  for daily-loss / give-back rules).

Shorts require the asset to be `shortable` and `easy_to_borrow` (checked before sending).
Results are Alpaca's `filled_qty` / `filled_avg_price`; exceptions: `AlpacaError` (status, code, message),
`OrderNotConfirmed`, `guard.Halted`.

**Gates:** Alpaca has no reduce-only flag, so the lib derives each order's intent from the live position
and a POST without that intent is treated as an entry. HALT blocks entries; reduces, stops and cancels pass.
A dropped POST is looked up by `client_order_id` before anything is resent.

## Limits worth knowing

- Market data on the free plan is **IEX only** (a slice of volume, not the consolidated tape); no Level 2
  and no full time & sales. A strategy that needs L2 / T&S needs its own data provider.
- Pattern day trader rule applies to live margin accounts under $25k equity (4+ day trades in 5 days).
- Rate limit 200 requests/min per key (429 → the lib backs off).
