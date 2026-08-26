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

## Coverage gap — `top_volume` mode doesn't include Matej's actual traded items

Found 2026-08-26: `ITEM_MODE=top_volume` ranks by raw region-wide volume, which surfaces
ammo/minerals/moon-goo almost exclusively. It does **not** include most of what Matej
actually runs buy orders on — T1 hulls (Rifter, Punisher, Cormorant, Catalyst, Vexor,
Rupture, Myrmidon), T2 modules (shield/armor reppers, autocannons, tracking disruptors,
ancillary current routers), skill injectors, or boosters. Of a 21-order sample only 2
items (Caldari Navy Antimatter Charge L, Photon Microprocessor) showed up in that day's
`recompute_top_of_book.csv`. Don't assume `top_volume` mode covers his real positions —
cross-check against `my_orders.csv` (see ESI section below) or fall back to
`ITEM_MODE=watchlist` when evaluating his actual open orders.

## ESI character orders — auto-pull instead of screenshots (added 2026-08-26)

`esi-auth/` pulls Matej's own live open orders (buy + sell, any station/structure)
directly from ESI via OAuth2 PKCE (public/native client, no secret stored anywhere) —
replacing manual in-game screenshots. One-time setup (register an app at
developers.eveonline.com, run `get_refresh_token.js` once) is entirely on Matej's side —
same "I can't do the interactive OAuth consent flow" reasoning as `deploy.sh`'s Cloud
Build connection step. See `esi-auth/README.md` for the exact steps. Once set up:

```
EVE_SSO_CLIENT_ID=<his client id> node esi-auth/fetch_my_orders.js
```

writes `my_orders.csv` (order_id, type_id, item_name, is_buy_order, price,
volume_remain, volume_total, location_id, location_name, region_id, range, min_volume,
duration, issued) — upload it into the conversation the same way as
`recompute_top_of_book.csv`. **Status as of 2026-08-26: code written, not yet run by
Matej** — no `.credentials.json` exists yet, so this can't be used until he completes
the one-time setup. Don't assume it's live; ask if he's done the setup before relying
on `my_orders.csv` existing.

Also added the same day, same scope group: `esi-auth/fetch_perimeter_market.js` pulls
the **full order book** of the Perimeter citadel (structure_id `1044752365771`, "0.0%
Neutral States Market HQ") — where Matej's own buy orders actually sit. This needs a
second scope, `esi-markets.structure_markets.v1`, requested explicitly by Matej
2026-08-26 because Jita's daily pipeline only covers the *public NPC station* — a
player-owned citadel's market is invisible to unauthenticated ESI/EVE
Tycoon/Fuzzwork entirely, authenticated per-structure access is the only way to see
it. Writes `perimeter_orders_raw.csv` + `perimeter_top_of_book.csv` (same shape as
`recompute_top_of_book.csv`, so it drops straight into `reports/generate_reports.py`
if a separate Perimeter dashboard/report is ever wanted — not built yet, ask if he
wants one). Same one-time-setup caveat applies: needs Matej's `.credentials.json` to
carry the structure_markets scope — if he authorized before this scope was added to
`get_refresh_token.js`, he needs to re-run the login script to pick it up.

## Files in the repo

| File | Purpose |
|---|---|
| `deploy.sh` | One-time GCP bootstrap (project, BigQuery, Artifact Registry, Cloud Run Job, IAM). Idempotent — safe to re-run. `SETUP_SCHEDULER=false` keeps it manual-only (current default preference). |
| `poller/poller.js` | Cloud Run Job source. `ITEM_MODE=top_volume` (default) scans the ~750 highest-volume items in the region; `ITEM_MODE=watchlist` falls back to an old hand-picked list. See coverage-gap note above. |
| `bigquery/schema.sql` | Table definitions. |
| `bigquery/recompute_top_of_book.sql` | Recomputes prices/margins from already-collected raw order data — no new scan needed. Run this any time the pricing/filter logic needs a redo without waiting for tomorrow's scan. |
| `refresh.sh` | One-command daily refresh: new scan + recompute, in one call. |
| `reports/generate_reports.py` | Builds the `.xlsx` report + `.html` dashboard from a `recompute_top_of_book.csv`. |
| `esi-auth/get_refresh_token.js` | One-time OAuth login (Matej runs, not Claude) — saves a refresh token. Scopes: `esi-markets.read_character_orders.v1`, `esi-markets.structure_markets.v1`. |
| `esi-auth/fetch_my_orders.js` | Pulls Matej's live open orders from ESI into `my_orders.csv`. |
| `esi-auth/fetch_perimeter_market.js` | Pulls the full Perimeter citadel order book (structure_id `1044752365771`) into `perimeter_orders_raw.csv` + a top-of-book aggregate `perimeter_top_of_book.csv` shaped like `recompute_top_of_book.csv`. |
| `docs/eve-jita-scanner-ops.md` | Mirror of this file, kept in the repo for anyone browsing it directly. |

## What not to do without Matej explicitly asking first

- Don't turn `SETUP_SCHEDULER` back on / add an automated trigger — he wants manual
  daily runs so he controls when capital gets committed.
- Don't add a fixed-capital allocation/auto-sizing algorithm back — see Methodology
  above.
- Don't change the pricing methodology without re-reading the history above; two
  different plausible-looking approaches already turned out to be wrong this project.
- Don't widen the ESI OAuth scope (e.g. add wallet) without asking first, even though
  the README mentions how — each added scope is a re-consent Matej has to click through.

## Where things might go next (mentioned, not yet requested)

- Matej said he may start sharing snapshots of his own actual buys/sells from his EVE
  account, to compare "what the report recommended" vs. "what actually worked." Superseded
  2026-08-26 by the ESI `my_orders.csv` pull above, which gets this from source instead of
  a manual snapshot — but he may still want a *history* of past buys/sells (not just
  currently-open orders) at some point; ESI's character orders endpoint only returns
  currently open orders, not history, so that would need a different endpoint
  (`/characters/{id}/orders/history/`) if he asks for it.
