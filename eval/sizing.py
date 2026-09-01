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

TODO(Matej): column names (region_buy_max, station_sell_min, buy_volume,
sell_volume, avg_daily_volume_14d, buy_order_count, sell_order_count) are
assumed from the schema description, not verified against schema.sql.
"""

import pandas as pd

import config
from eval import bq
from eval.orders import margin_pct

CANDIDATES_SQL = f"""
select
    type_id,
    item_name,
    region_buy_max as buy_max,
    station_sell_min as sell_min,
    buy_volume,
    sell_volume,
    avg_daily_volume_14d,
    buy_order_count,
    sell_order_count
from `{config.TABLE_MARKET_SNAPSHOTS}`
where scan_date = current_date()
  and avg_daily_volume_14d > 0
"""

CASH_SQL = f"""
select cash
from `{config.TABLE_NET_WORTH_HISTORY}`
order by run_date desc
limit 1
"""


def density_band(density_per_1000: float) -> str:
    if density_per_1000 <= config.DENSITY_LOW_MAX:
        return "low"
    if density_per_1000 <= config.DENSITY_MEDIUM_MAX:
        return "medium"
    return "high"


def compute_available_capital(client, orders_eval: pd.DataFrame) -> float:
    cash_row = bq.query_df(client, CASH_SQL)
    if cash_row.empty:
        raise RuntimeError("net_worth_history has no rows — run step 4 / daily_ops.js at least once first.")
    cash = float(cash_row.iloc[0]["cash"])

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

    # Filter: margin floor + exclude high risk from NEW candidates
    # (existing positions are governed by step 2 only, not touched here).
    df = df[(df["margin_pct"] >= config.MARGIN_FLOOR_PCT) & (df["risk_band"] != "high")]

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
