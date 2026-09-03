"""
Step 2 — Evaluate open buy orders.

Design (agreed with Matej):
- Stateless, per order, every run. No cycle-counting, no "patience" — a
  reprice always jumps straight back to top of book, so there is no such
  thing as "still off the top after N refreshes".
- reference buy price = MAX(Jita system-wide buy.max, Perimeter buy.max) —
  confirmed 2026-09-01: NOT limited to a single station/structure, the
  whole Jita solar system (JITA_SYSTEM_ID) and the whole Perimeter citadel
  both count in full.
- new_price = reference_buy_max + REPRICE_TICK
- margin recomputed at new_price using current sell.min reference (Jita
  system only — that's where Matej actually lists sells), via
  reprice_margin_pct() — NOT margin_pct(). Added 2026-09-02: margin_pct()
  assumes both legs are brand-new orders (full BROKER_FEE_RATE on the buy),
  which overstates the real cost of repricing an order that already exists.
  reprice_margin_pct() uses the actual, exactly-computed reprice fee for
  THIS order's specific placed_price -> new_price delta (see config.py's
  REPRICE_FLAT_FEE / REPRICE_SCC_RATE_* — derived + verified against
  Matej's live wallet journal) on the buy leg, full fees on the (not yet
  placed) sell leg.
    - margin >= MARGIN_FLOOR_PCT -> REPRICE
    - margin <  MARGIN_FLOOR_PCT -> CANCEL (chasing would erode profit
      below the floor — this is the actual "does it still pay to chase"
      check, done fresh every day, not carried over as state)
- reprice_cost_so_far = new_price - original placed price, logged for the
  KPI/capital review step and later as an ml_features input. This is NOT
  used to decide reprice/cancel — decision is margin-only, cost is just
  visibility.

Verified 2026-09-01 against live `bq ls` + bigquery/schema.sql (not just the
skill's description, which was wrong on several points):
- `my_orders` has no `state` column — it has `is_open BOOL` (NULL = legacy
  row from before the is_open fix, treat as open, same as every other query
  in this repo, e.g. bigquery/stale_orders.sql).
- `my_orders` is an append-only history table (same order_id reappears every
  scan as price/volume_remain change) — needs a "latest row per order_id"
  dedupe, same pattern used everywhere else in this repo.
- `market_snapshots` has NO `region_buy_max` / `station_sell_min` columns —
  those never existed. True top-of-book price isn't a stored column at all;
  it's computed directly from the raw order book
  (market_orders_raw / perimeter_orders_raw), the same way
  bigquery/recompute_top_of_book.sql does it (best standing order per side),
  just scoped to the whole Jita system instead of one station.
"""

import pandas as pd

import config
from eval import bq

REFERENCE_PRICE_SQL = f"""
with jita_latest as (
  select max(scan_date) as d from `{config.TABLE_MARKET_ORDERS_RAW}`
),
jita_system as (
  select r.type_id, r.is_buy_order, r.price
  from `{config.TABLE_MARKET_ORDERS_RAW}` r, jita_latest l
  where r.scan_date = l.d and r.system_id = {config.JITA_SYSTEM_ID}
),
jita_buy as (
  select type_id, max(price) as jita_buy_max
  from jita_system where is_buy_order group by type_id
),
jita_sell as (
  select type_id, min(price) as jita_sell_min
  from jita_system where not is_buy_order group by type_id
),
perim_latest as (
  select max(scan_date) as d from `{config.TABLE_PERIMETER_ORDERS_RAW}`
),
perim_buy as (
  select r.type_id, max(r.price) as perim_buy_max
  from `{config.TABLE_PERIMETER_ORDERS_RAW}` r, perim_latest l
  where r.scan_date = l.d and r.is_buy_order
  group by r.type_id
)
select
  type_id,
  greatest(coalesce(jb.jita_buy_max, 0), coalesce(pb.perim_buy_max, 0)) as reference_buy_max,
  js.jita_sell_min as reference_sell_min
from jita_buy jb
full outer join perim_buy pb using (type_id)
left join jita_sell js using (type_id)
"""

OPEN_ORDERS_SQL = f"""
with latest_orders as (
  select * except(rn) from (
    select *, row_number() over (partition by order_id order by scanned_at desc) as rn
    from `{config.TABLE_MY_ORDERS}`
  )
  where rn = 1 and (is_open is null or is_open = true)
)
select order_id, type_id, item_name, price as placed_price, volume_remain
from latest_orders
where coalesce(is_buy_order, false)
"""


def margin_pct(buy_price: float, sell_price: float) -> float:
    """Round-trip margin assuming BOTH legs are brand-new orders (full
    BROKER_FEE_RATE on the buy, full BROKER_FEE_RATE+SALES_TAX_RATE on the
    sell). Correct for eval/sizing.py's new candidates — wrong for step 2's
    existing-order reprice decision, which uses reprice_margin_pct() below
    instead (added 2026-09-02, see config.py's REPRICE_* comment)."""
    buy_cost = buy_price * (1 + config.BROKER_FEE_RATE)
    sell_net = sell_price * (1 - config.BROKER_FEE_RATE - config.SALES_TAX_RATE)
    return (sell_net - buy_cost) / buy_cost * 100.0


def buy_reprice_fee(new_price: float, placed_price: float, qty: float) -> float:
    """Real ISK cost of repricing an EXISTING buy order upward at Perimeter
    (flat relist fee + SCC surcharge) — see config.py's REPRICE_FLAT_FEE /
    REPRICE_SCC_RATE_* comment for the derivation and verification against
    Matej's live wallet journal. `delta` is clamped at 0 because a buy
    reprice only ever moves the price up (chasing the top of book)."""
    delta = max(0.0, new_price - placed_price)
    return (
        config.REPRICE_FLAT_FEE
        + config.REPRICE_SCC_RATE_VALUE * (new_price * qty)
        + config.REPRICE_SCC_RATE_DELTA * (delta * qty)
    )


def reprice_margin_pct(new_price: float, placed_price: float, sell_price: float, qty: float) -> float:
    """Net margin %% for step 2's actual decision: REPRICE an existing buy
    order (real incremental reprice cost, computed exactly from this order's
    own placed_price -> new_price delta — no guessing/averaging) vs. a
    brand-new sell order on the other leg (full BROKER_FEE_RATE +
    SALES_TAX_RATE — that leg hasn't been placed yet, so it isn't a reprice
    at all). Compare against config.MARGIN_FLOOR_PCT, same as margin_pct()."""
    fee_per_unit = buy_reprice_fee(new_price, placed_price, qty) / qty
    buy_cost = new_price + fee_per_unit
    sell_net = sell_price * (1 - config.BROKER_FEE_RATE - config.SALES_TAX_RATE)
    return (sell_net - buy_cost) / buy_cost * 100.0


def evaluate_open_orders(client) -> pd.DataFrame:
    orders = bq.query_df(client, OPEN_ORDERS_SQL)
    if orders.empty:
        return orders.assign(action=[], new_price=[], reason=[], reprice_cost_so_far=[])

    ref = bq.query_df(client, REFERENCE_PRICE_SQL)
    df = orders.merge(ref, on="type_id", how="left", suffixes=("", "_ref"))

    actions, new_prices, reasons, costs = [], [], [], []
    for row in df.itertuples():
        if pd.isna(row.reference_buy_max) or pd.isna(row.reference_sell_min):
            actions.append("SKIP")
            new_prices.append(None)
            reasons.append("no fresh reference price for this item — check watchlist coverage")
            costs.append(None)
            continue

        new_price = round(row.reference_buy_max + config.REPRICE_TICK, 2)
        m = reprice_margin_pct(new_price, row.placed_price, row.reference_sell_min, row.volume_remain)
        cost = round(new_price - row.placed_price, 2)

        if m >= config.MARGIN_FLOOR_PCT:
            actions.append("REPRICE")
            new_prices.append(new_price)
            reasons.append(f"margin at new price {m:.1f}% >= {config.MARGIN_FLOOR_PCT}% floor")
        else:
            actions.append("CANCEL")
            new_prices.append(None)
            reasons.append(f"margin at new price {m:.1f}% < {config.MARGIN_FLOOR_PCT}% floor — chasing not worth it")
        costs.append(cost)

    df["action"] = actions
    df["new_price"] = new_prices
    df["reason"] = reasons
    df["reprice_cost_so_far"] = costs

    # Sorted alphabetically by item_name (added 2026-09-02, per Matej) — the
    # SQL/BigQuery result order is arbitrary/unstable, which made the
    # dashboard's "existing orders" table awkward to scan by hand against
    # the in-game orders list. Applies uniformly across REPRICE/CANCEL/SKIP
    # rows (not grouped by action) — easiest to eyeball against an
    # alphabetically-sorted in-game orders list.
    df = df.sort_values("item_name", kind="stable").reset_index(drop=True)
    return df
