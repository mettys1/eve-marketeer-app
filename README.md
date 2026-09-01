# eve-marketeer-app

Companion app for the daily EVE market routine, built on top of the existing
ESI → BigQuery `eve_jita_scanner` pipeline. Single entry point:

```
pip install -r requirements.txt
python run_eval.py
```

Opens one self-contained HTML dashboard in your browser. Nothing here
touches ESI directly or places/cancels orders in-game — output is a
read-only action list for you to execute manually in the client.

## Design decisions (confirmed)

- **Step order matters**: refresh → evaluate existing orders → size new
  orders → capital review. Sizing depends on capital freed/locked by the
  order-evaluation step, so it must run after it.
- **Reprice/cancel is stateless, per order, every run** — no cycle
  counting. A reprice always jumps to top of book, so "still off the top
  after N refreshes" doesn't apply here. Decision is margin-only: reprice
  if margin at the new top-of-book price is still ≥ 8%, else cancel.
- **New candidate filtering uses market density (risk-of-being-undercut),
  not our own order history** — our own fill/reprice history is
  necessarily empty for anything we've never traded, so it can't be used
  to screen candidates. Density is computed straight from the fresh scan
  and works for any item on the watchlist.
- **Capital reserve**: 1% of total capital, held aside, never spent on new
  buy orders.
- **ML is deferred** — `eval/features.py` logs a row per (scan_date,
  type_id) every run (density, action taken, reprice cost) so history
  accumulates, but nothing reads it back yet. `ml/` is an empty placeholder
  for when that's revisited.

## Before the first real run — TODO

Everything below was written from the `eve-jita-own-infra` skill's
*description* of the schema, not a live read of `schema.sql`. Check these
against the real tables before trusting any output:

- [x] ~~`config.py` — `OPS_DIR` path~~ — resolved 2026-09-01: there is no
      local `daily_ops.js`, it never existed. This repo's own root already
      *is* the ops pipeline (`refresh_all.sh` + `refresh_jita.sh` /
      `refresh_my_orders.sh` / `refresh_perimeter.sh` / `refresh_wallet.sh`,
      `esi-jobs/`, `poller/`) — 4 independent Cloud Run Jobs
      (`eve-jita-poller`, `esi-perimeter-poller`, `esi-my-orders-poller`,
      `esi-wallet-poller`, project `eve-jita-scanner-21359`, region
      `europe-west1`). `eval/refresh.py` now calls `gcloud run jobs execute`
      on all 4 in parallel instead of a subprocess to a nonexistent script —
      needs a real end-to-end run to confirm `gcloud` auth/ADC works the
      same from wherever `python run_eval.py` is actually run.
- [ ] `config.py` — `PROJECT_ID`/`DATASET` if they've since changed
- [ ] `eval/orders.py` — column names in `REFERENCE_PRICE_SQL` /
      `OPEN_ORDERS_SQL` (`region_buy_max`, `station_sell_min`, `price`,
      `is_buy_order`, `state`, `volume_remain`)
- [ ] `eval/sizing.py` — column names in `CANDIDATES_SQL` / `CASH_SQL`
      (`buy_volume`, `sell_volume`, `avg_daily_volume_14d`,
      `buy_order_count`, `sell_order_count`, `net_worth_history.cash`,
      `run_date`)
- [ ] `eval/kpi.py` — `net_worth_history` column names (`locked_buy`,
      `locked_sell`, `total`), and `wallet_journal.ref_type` values used
      for "realized profit yesterday" (currently a guess:
      `market_transaction` / `market_escrow` — needs checking against
      real journal entries)
- [ ] `eval/kpi.py` realized-profit query is a rough first pass — the
      profit chart in the dashboard currently falls back to a net-worth-
      delta proxy rather than a true realized series; worth revisiting
      once the journal query is confirmed
- [ ] Confirm BigQuery auth (ADC / service account) *and* `gcloud` CLI auth
      are set up wherever `python run_eval.py` actually runs — same
      requirement the existing `refresh_*.sh` scripts already have

None of the four core decisions above are affected by these TODOs — they're
schema plumbing, not logic changes.
