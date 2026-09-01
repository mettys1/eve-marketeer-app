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
  system only — that's where Matej actually lists sells)
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
    buy_cost = buy_price * (1 + config.BROKER_FEE_RATE)
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
        m = margin_pct(new_price, row.reference_sell_min)
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
    return df
