# C-J6MT import pipeline

Separate pipeline, run **after** the main daily eval:

```
python run_eval.py && python run_cj.py
```

For a small number of modules it checks the Jita price, the C-J Keepstar market, the price
difference and m3-based shipping, then suggests what to haul to C-J and how many units.
Output: `output/cj_YYYYMMDD_HHMM.html`, including a Jita multibuy list ready to paste.

## Parts

| Part | What it does |
|---|---|
| `esi-jobs/job_cj.js` → Cloud Run Job `esi-cj-poller` | C-J order book (authenticated), `type_info` cache (m3, category), Jita books for types the main scan didn't cover, Delve 14-day history → BigQuery |
| Cloud Run Job `esi-cj-snapshot` (optional, hourly) | C-J order book only. Makes the demand estimate more accurate |
| `bigquery/schema_cj.sql` | `cj_orders_raw`, `type_info`, `cj_jita_orders_raw`, `cj_delve_history`, `cj_runs` |
| `cj/analysis.py` | All the calculations (pure functions are tested in `tests/test_cj.py`) |
| `cj/dashboard.py`, `run_cj.py` | Report + entry point |
| `esi-oauth-service` `/login?target=cj` | Optional login for a different character → secret `esi-credentials-cj` |

## Setup (once)

1. **Structure ID of the C-J Keepstar.** In game: open Show Info on the structure (or find it in the
   Structure Browser) and drag it into a chat window. The link has the form
   `showinfo:35834//1035466617946`, and the trailing number is the `structure_id`. Put it in
   `config.CJ_STRUCTURE_ID` and in `CJ_STRUCTURE_ID` for `deploy_cj.sh`.
2. **Character.** The job uses `esi-credentials` (the trading character) by default.
   If it returns 403, that character has no market access in the Keepstar. In that case:
   `REDEPLOY_OAUTH=1 LOGIN_KEY=... bash deploy_cj.sh`, then log in at
   `/login?key=...&target=cj` with a character that has access. Finally run
   `CJ_CREDENTIALS_SECRET_NAME=esi-credentials-cj bash deploy_cj.sh`.
   This does not touch the trading character's token.
3. **Config** (`config.py`, C-J section): `CJ_SHIPPING_ISK_PER_M3` and `CJ_SELL_BROKER_FEE_RATE`.
   While these are unset, the report shows a warning banner and the profits are not reliable.
4. `bash deploy_cj.sh`, ideally with `ENABLE_HOURLY=1`.

## Calculation

- **Jita A**: buy immediately from sell orders (Jita system + Perimeter). This is the average
  price across the book for the suggested quantity, not just the sell min.
- **Jita B**: buy order = max(buy Jita, buy Perimeter) + tick + 0.5% SCC. Shown for reference
  only; it is not used for ranking.
- **Shipping** = `packaged_volume` × `CJ_SHIPPING_ISK_PER_M3` (no collateral).
- **Sale in C-J** = current C-J sell min × (1 − broker fee − sales tax).
- **Demand in C-J** (units/day): structure markets have no ESI history, so the pipeline compares
  consecutive snapshots of the C-J book:
  - a partial fill of an order counts in full;
  - an order that disappeared (and had not expired) counts at `CJ_DISAPPEARED_FILL_SHARE`,
    because there is no way to tell whether it sold out or was cancelled.

  If there are fewer than `CJ_MIN_COVERAGE_HOURS` of snapshots, the fallback is
  Delve 14d volume × `CJ_DELVE_SHARE`. Delve also includes 1DQ1-A, which is why the share is
  well below 1. The report shows both numbers side by side, so the share can be calibrated
  after about a week.
- **Quantity** = demand × `CJ_STOCK_DAYS` − my open sell stock in C-J, capped by:
  - Jita availability;
  - `CJ_PER_POSITION_PCT` of the budget;
  - the remaining budget;
  - `CJ_MAX_CARGO_M3`.
- **Filters**: margin A ≥ `CJ_MIN_MARGIN_PCT`, margin ≤ `CJ_MARGIN_CEILING_PCT` (artifacts),
  C-J supply ≤ `CJ_MAX_COMPETITOR_DAYS` days of demand.
- **Ranking**: expected profit/day = profit/unit A × demand/day, top `CJ_TOP_N`.

Extra sections in the report:

- C-J buy orders that already pay more than the landed Jita price (sell straight into the
  buy order, no broker fee);
- "gaps", i.e. modules with no sell order in C-J but with demand in Delve.

## Known limitations

- With a single snapshot a day, everything that sells out completely in between shows up only
  as "disappeared". An hourly `esi-cj-snapshot` helps a lot here.
- Demand only counts modules that already have at least one order in C-J. A module with no
  order there at all is not visible.
- The sale price is the current sell min. It ignores undercut wars and the time it takes to sell.
