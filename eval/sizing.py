"""
Step 3 — Size new buy order candidates.

Must run AFTER eval/orders.py (step 2), because the available budget depends
on what step 2 decided:

    available_capital = current_cash
                         + sum(escrow freed by CANCELled orders)
                         - sum(extra escrow needed by REPRICEd orders)
                         - CAPITAL_RESERVE_PCT * total_capital

This is the same-day, best-case snapshot described in the "first in line"
rules — recompute it fresh each run, don't carry state between days.

Ranking + sizing itself is unchanged from eve-jita-own-infra: rank by
profit-per-unit, walk top to bottom, sequential budget deduction, with one
addition — a density risk filter on top, using the SAME density formula as
the existing risk-score rule, applied to *candidates* only (never to items
we already hold an order on, which are handled entirely by step 2).

Rewritten 2026-09-01, verified against live BigQuery:
- `market_snapshots` has no `region_buy_max` / `station_sell_min` /
  `buy_volume` / `sell_volume` / `buy_order_count` / `sell_order_count`
  columns — those never existed. Top-of-book price, order counts and depth
  now come straight from the raw order book (market_orders_raw +
  perimeter_orders_raw), same approach as eval/orders.py.
- Confirmed 2026-09-01: pooling Jita system + Perimeter applies everywhere
  in this file, not just the buy price — buy_volume/sell_volume/
  buy_order_count/sell_order_count (depth cap + density) now sum BOTH
  markets too, no single-structure limit on either. Only sell_min stays
  Jita-only, per the separate, earlier-confirmed fact that Matej actually
  lists his sells at Jita, not Perimeter.
- `net_worth_history` doesn't exist either — current cash now comes from
  eval/kpi.get_current_cash (wallet_journal), same as the dashboard's
  headline numbers.
"""

import pandas as pd

import config
from eval import bq, kpi
from eval.orders import margin_pct

CANDIDATES_SQL = f"""
with jita_latest as (
  select max(scan_date) as d from `{config.TABLE_MARKET_ORDERS_RAW}`
),
jita_system as (
  select r.type_id, r.is_buy_order, r.price, r.volume_remain
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
jita_depth as (
  select type_id,
    sum(if(is_buy_order, volume_remain, 0)) as jita_buy_volume,
    sum(if(not is_buy_order, volume_remain, 0)) as jita_sell_volume,
    countif(is_buy_order) as jita_buy_orders,
    countif(not is_buy_order) as jita_sell_orders
  from jita_system group by type_id
),
perim_latest as (
  select max(scan_date) as d from `{config.TABLE_PERIMETER_ORDERS_RAW}`
),
perim_book as (
  select r.type_id, r.is_buy_order, r.price, r.volume_remain
  from `{config.TABLE_PERIMETER_ORDERS_RAW}` r, perim_latest l
  where r.scan_date = l.d
),
perim_buy as (
  select type_id, max(price) as perim_buy_max
  from perim_book where is_buy_order group by type_id
),
perim_depth as (
  select type_id,
    sum(if(is_buy_order, volume_remain, 0)) as perim_buy_volume,
    sum(if(not is_buy_order, volume_remain, 0)) as perim_sell_volume,
    countif(is_buy_order) as perim_buy_orders,
    countif(not is_buy_order) as perim_sell_orders
  from perim_book group by type_id
),
snapshot as (
  select type_id, item_name, avg_daily_volume_14d
  from `{config.TABLE_MARKET_SNAPSHOTS}`
  qualify row_number() over (partition by type_id order by scanned_at desc) = 1
)
select
  s.type_id, s.item_name,
  greatest(coalesce(jb.jita_buy_max, 0), coalesce(pb.perim_buy_max, 0)) as buy_max,
  js.jita_sell_min as sell_min,
  coalesce(d.jita_buy_volume, 0) + coalesce(pd.perim_buy_volume, 0) as buy_volume,
  coalesce(d.jita_sell_volume, 0) + coalesce(pd.perim_sell_volume, 0) as sell_volume,
  coalesce(d.jita_buy_orders, 0) + coalesce(pd.perim_buy_orders, 0) as buy_order_count,
  coalesce(d.jita_sell_orders, 0) + coalesce(pd.perim_sell_orders, 0) as sell_order_count,
  s.avg_daily_volume_14d
from snapshot s
left join jita_buy jb using (type_id)
left join jita_sell js using (type_id)
left join jita_depth d using (type_id)
left join perim_buy pb using (type_id)
left join perim_depth pd using (type_id)
where s.avg_daily_volume_14d > 0
  and js.jita_sell_min is not null
  and (jb.jita_buy_max is not null or pb.perim_buy_max is not null)
"""


def density_band(density_per_1000: float) -> str:
    if density_per_1000 <= config.DENSITY_LOW_MAX:
        return "low"
    if density_per_1000 <= config.DENSITY_MEDIUM_MAX:
        return "medium"
    return "high"


def compute_available_capital(client, orders_eval: pd.DataFrame) -> float:
    cash = kpi.get_current_cash(client)

    freed = 0.0
    extra_escrow = 0.0
    if not orders_eval.empty:
        cancelled = orders_eval[orders_eval["action"] == "CANCEL"]
        freed = float((cancelled["placed_price"] * cancelled["volume_remain"]).sum())

        repriced = orders_eval[orders_eval["action"] == "REPRICE"]
        if not repriced.empty:
            delta = repriced["new_price"] - repriced["placed_price"]
            extra_escrow = float((delta * repriced["volume_remain"]).sum())

    reserve = config.CAPITAL_RESERVE_PCT * cash
    return cash + freed - extra_escrow - reserve


def rank_new_candidates(client, available_capital: float, exclude_type_ids: set) -> pd.DataFrame:
    df = bq.query_df(client, CANDIDATES_SQL)
    if exclude_type_ids:
        df = df[~df["type_id"].isin(exclude_type_ids)]

    df["density_per_1000"] = (df["buy_order_count"] + df["sell_order_count"]) / df["avg_daily_volume_14d"] * 1000
    df["risk_band"] = df["density_per_1000"].apply(density_band)
    df["margin_pct"] = df.apply(lambda r: margin_pct(r["buy_max"], r["sell_min"]), axis=1)

    # Filter: margin floor + ceiling, min liquidity, exclude high risk from
    # NEW candidates (existing positions are governed by step 2 only, not
    # touched here). The floor/ceiling/order-count filters mirror the same
    # blunt sanity checks bigquery/recompute_top_of_book.sql already uses —
    # confirmed 2026-09-01 needed here too after a real run surfaced margins
    # like 46071%/9954%/9076% on near-zero-liquidity items (Armor
    # Reinforcement Charge, Shadow Tungsten Charge S, Arch Angel EMP M) —
    # classic reference-price artifacts on a thin book, not real
    # opportunities. MARGIN_CEILING_PCT / MIN_ORDERS_PER_SIDE live in
    # config.py, same as every other threshold.
    df = df[
        (df["margin_pct"] >= config.MARGIN_FLOOR_PCT)
        & (df["margin_pct"] <= config.MARGIN_CEILING_PCT)
        & (df["buy_order_count"] >= config.MIN_ORDERS_PER_SIDE)
        & (df["sell_order_count"] >= config.MIN_ORDERS_PER_SIDE)
        & (df["risk_band"] != "high")
    ]

    df["profit_per_unit"] = df["sell_min"] * (1 - config.BROKER_FEE_RATE - config.SALES_TAX_RATE) \
        - df["buy_max"] * (1 + config.BROKER_FEE_RATE)
    df = df.sort_values("profit_per_unit", ascending=False).reset_index(drop=True)

    remaining = available_capital
    rows = []
    total_budget = available_capital
    for row in df.itertuples():
        if remaining <= 0:
            break
        depth_cap = min(row.buy_volume, row.sell_volume) * config.DEPTH_CAP_FRACTION
        per_position_cap = (config.PER_POSITION_PCT * total_budget) / row.buy_max if row.buy_max else 0
        qty = min(
            remaining / (row.buy_max * (1 + config.BROKER_FEE_RATE)),
            depth_cap,
            config.HARD_UNIT_CAP,
            per_position_cap,
        )
        qty = int(qty)
        if qty <= 0:
            continue

        cost = qty * row.buy_max * (1 + config.BROKER_FEE_RATE)
        remaining -= cost

        rows.append({
            "type_id": row.type_id,
            "item_name": row.item_name,
            "suggested_qty": qty,
            "suggested_price": round(row.buy_max + config.REPRICE_TICK, 2),
            "margin_pct": round(row.margin_pct, 1),
            "risk_band": row.risk_band,
            "density_per_1000": round(row.density_per_1000, 3),
            "est_cost": round(cost, 0),
        })

    result = pd.DataFrame(rows)
    if result.empty:
        print("[sizing] no candidates sized — budget unallocated. See rule #4 in "
              "eve-jita-own-infra: may indicate thin watchlist, not a bug.")
    return result
