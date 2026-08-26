---
name: eve-jita-pipeline-ops
description: Operational guide for Matej's EVE Online Jita station-trading GCP pipeline (project eve-jita-scanner-21359, repo eve-marketeer-app) — architecture, daily refresh workflow, report/dashboard regeneration, key identifiers, and hard-won methodology gotchas. Use when Matej asks to refresh Jita market data, regenerate the daily report or dashboard, debug the poller/BigQuery pipeline, or continue work on the eve-marketeer-app project. Distinct from the separate "eve-jita-scanner" skill, which is for one-off ad-hoc scans — this one is the GCP daily-pipeline documentation.
---

# EVE Jita station-trading pipeline — ops guide

## What this is

A GCP pipeline that scans EVE Online's Jita 4-4 station market once a day (manually
triggered — no auto Cloud Scheduler, by Matej's explicit choice), computes real
after-fee profitability for every liquid tradeable item in the region, and produces a
ranked report Matej reviews by hand each day and trades from selectively. There is
**no automated capital allocation** — Matej rejected that approach explicitly, since
his available ISK varies day to day. The report just ranks candidates; he picks.

## Architecture

- **Cloud Run Job** `eve-jita-poller` (region `europe-west1`, project
  `eve-jita-scanner-21359`) — scans ESI public market data, writes to BigQuery.
  Batch job, run-to-completion, ~2Gi memory. Triggered manually via
  `gcloud run jobs execute` (wrapped in `refresh.sh` — see below).
- **BigQuery** dataset `eve_jita_scanner`:
  - `market_snapshots` — one row per (scan, item) aggregate. **Can have >1 row per
    (type_id, scan_date)** if the job ran more than once that day — always dedupe by
    `scanned_at` when joining on it (see `bigquery/recompute_top_of_book.sql`).
  - `market_orders_raw` — full order book per scan (kept 90 days). This is what lets
    you recompute prices/margins without a new scan.
  - `market_history` — ESI daily volume/price history, full replace each run.
- **Repo**: `github.com/mettys1/eve-marketeer-app`, local clone at
  `C:\Users\Matej\Documents\GitHub\eve-marketeer-app` on Matej's machine (reachable via
  the device bridge if a desktop session is connected).

## Daily refresh workflow

1. Matej runs `bash refresh.sh` in Cloud Shell, from the repo root. This does two
   things: (a) executes the Cloud Run Job (new scan, few minutes), (b) runs
   `bigquery/recompute_top_of_book.sql` and saves the result to
   `recompute_top_of_book.csv`.
2. Matej uploads that CSV into the conversation.
3. Claude runs `python3 reports/generate_reports.py recompute_top_of_book.csv <YYYY-MM-DD>`
   — builds `jita_denni_report_<date>.xlsx` and `jita_dashboard.html` next to the CSV.
   Needs `openpyxl` (`pip install openpyxl --break-system-packages` if missing).
4. Claude runs the **xlsx skill's `scripts/recalc.py`** on the `.xlsx` output — required,
   since openpyxl writes formulas with no cached values (the "Kumulativní cena" running-
   total column will read blank otherwise).
5. Claude delivers the `.xlsx` via SendUserFile, and publishes/updates the dashboard via
   the **Artifact tool** — pass the existing artifact's URL (below) so it republishes to
   the same link instead of creating a new one.

Current dashboard: **https://claude.ai/code/artifact/516b21f0-b2f0-4872-bbc0-86d20bc3ae18**

## Key identifiers

| | |
|---|---|
| GCP project | `eve-jita-scanner-21359` |
| Region (GCP) | `europe-west1` |
| BigQuery dataset | `eve_jita_scanner` |
| EVE region | The Forge, `region_id = 10000002` |
| EVE station | Jita IV - Moon 4 - Caldari Navy Assembly Plant, `station_id = 60003760` |
| Broker fee | 1.382 % | 
| Sales tax | 3.375 % |

## Methodology — read this before touching pricing/sizing logic

**Prices are top-of-book**, i.e. the single best standing buy/sell order at the Jita
station — the same number the game client shows as "Market Buy/Sell". This was **not**
the original design and got fixed the hard way on 2026-08-25:

- The original method volume-weighted the top 5% of order-book volume on each side,
  meant to be more robust than a bare top price. In practice it was badly wrong for raw
  materials / moon goo (Promethium, Strontium Clathrates, compressed ore/gas): these
  markets regularly carry a few enormous "floor" buy orders from industrial players —
  sometimes 10-50x a day's actual traded volume in size — sitting far below the real
  price. Once one fell inside the 5%-of-volume window it swamped the average, showing a
  buy price far below what you'd actually have to pay to be competitive (confirmed
  against real numbers: Promethium showed 28.6k vs. an actual 51.0k; Strontium
  Clathrates showed 2.1k vs. an actual 3.95k).
- **Do not reintroduce a "minimum order count" filter reasoned from a queueing model.**
  EVE market matching is price-priority, not a queue: placing the best order captures
  ALL matching incoming instant-buy/sell flow immediately — there is no "waiting your
  turn." An earlier iteration this session added a min-8-orders filter on exactly that
  wrong assumption and had to be reverted.
- The real remaining open question for thin/illiquid books: whether volume is
  genuinely bidirectional. `avg_daily_volume_14d` is **region-wide**, not
  station-specific, and doesn't say how that volume splits between buy and sell side —
  a loot-type item's "volume" might be almost entirely farmers dumping into standing
  buy orders, with near-zero real demand on the sell side. No principled filter for
  this exists yet; `margin_pct` is capped at 100% in the recompute query as a blunt
  sanity ceiling, not a rigorous fix. If Matej reports another implausible number,
  this is the first place to look.
- `market_snapshots` residual rows (see Architecture above) will silently duplicate
  output rows in any query that LEFT JOINs on it without deduping by `scanned_at` —
  bit twice this session before the dedup went into `recompute_top_of_book.sql`.

**Position sizing**: `suggested_units = round(0.15 * avg_daily_volume_14d)` — 15% of
daily turnover, **no ISK/capital cap of any kind**. Ranking is by total achievable
position profit (`suggested_units * profit_per_unit`), not by `margin_pct` or
`profit_per_unit` alone — Matej wants the combination of margin, turnover, and base
price that maximizes deployable profit. He explicitly rejected a fixed-capital
allocation algorithm earlier in this project: his available ISK varies day to day, so
the report ranks candidates and he decides how far down the list to go, based on the
live running-total ("Kumulativní cena") column.

## Files in the repo

| File | Purpose |
|---|---|
| `deploy.sh` | One-time GCP bootstrap (project, BigQuery, Artifact Registry, Cloud Run Job, IAM). Idempotent — safe to re-run. `SETUP_SCHEDULER=false` keeps it manual-only (current default preference). |
| `poller/poller.js` | Cloud Run Job source. `ITEM_MODE=top_volume` (default) scans the ~750 highest-volume items in the region; `ITEM_MODE=watchlist` falls back to an old hand-picked list. |
| `bigquery/schema.sql` | Table definitions. |
| `bigquery/recompute_top_of_book.sql` | Recomputes prices/margins from already-collected raw order data — no new scan needed. Run this any time the pricing/filter logic needs a redo without waiting for tomorrow's scan. |
| `refresh.sh` | One-command daily refresh: new scan + recompute, in one call. |
| `reports/generate_reports.py` | Builds the `.xlsx` report + `.html` dashboard from a `recompute_top_of_book.csv`. |
| `docs/eve-jita-scanner-ops.md` | Mirror of this file, kept in the repo for anyone browsing it directly. |

## What not to do without Matej explicitly asking first

- Don't turn `SETUP_SCHEDULER` back on / add an automated trigger — he wants manual
  daily runs so he controls when capital gets committed.
- Don't add a fixed-capital allocation/auto-sizing algorithm back — see Methodology
  above.
- Don't change the pricing methodology without re-reading the history above; two
  different plausible-looking approaches already turned out to be wrong this project.

## Where things might go next (mentioned, not yet requested)

- Matej said he may start sharing snapshots of his own actual buys/sells from his EVE
  account, to compare "what the report recommended" vs. "what actually worked." No
  format or cadence has been discussed yet — ask him when it comes up rather than
  guessing a structure.
