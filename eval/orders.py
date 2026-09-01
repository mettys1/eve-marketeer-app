"""
Step 2 — Evaluate open buy orders.

Design (agreed with Matej):
- Stateless, per order, every run. No cycle-counting, no "patience" — a
  reprice always jumps straight back to top of book, so there is no such
  thing as "still off the top after N refreshes".
- reference buy price = MAX(Jita region-wide buy.max, Perimeter buy.max) —
  this is the "reprice must pool Jita + Perimeter" rule from eve-trading.md,
  NOT Jita-only.
- new_price = reference_buy_max + REPRICE_TICK
- margin recomputed at new_price using current sell.min reference
    - margin >= MARGIN_FLOOR_PCT -> REPRICE
    - margin <  MARGIN_FLOOR_PCT -> CANCEL (chasing would erode profit
      below the floor — this is the actual "does it still pay to chase"
      check, done fresh every day, not carried over as state)
- reprice_cost_so_far = new_price - original placed price, logged for the
  KPI/capital review step and later as an ml_features input. This is NOT
  used to decide reprice/cancel — decision is margin-only, cost is just
  visibility.

TODO(Matej): column names below (region_buy_max, station_sell_min, price on
perimeter_orders_raw, is_buy_order) are my best guess from the schema
description in the skill notes, not verified against schema.sql. Please
check before relying on this query's output.
"""

import pandas as pd

import config
from eval import bq

REFERENCE_PRICE_SQL = f"""
with jita_region as (
    select type_id, item_name, region_buy_max, station_sell_min
    from `{config.TABLE_MARKET_SNAPSHOTS}`
    where scan_date = current_date()
),
perimeter_buy as (
    select type_id, max(price) as perimeter_buy_max
    from `{config.TABLE_PERIMETER_ORDERS_RAW}`
    where is_buy_order = true
    group by type_id
)
select
    j.type_id,
    j.item_name,
    greatest(j.region_buy_max, coalesce(p.perimeter_buy_max, 0)) as reference_buy_max,
    j.station_sell_min as reference_sell_min
from jita_region j
left join perimeter_buy p using (type_id)
"""

OPEN_ORDERS_SQL = f"""
select order_id, type_id, item_name, price as placed_price, volume_remain
from `{config.TABLE_MY_ORDERS}`
where is_buy_order = true
  and state = 'open'
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
