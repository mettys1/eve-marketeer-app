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

# "First mover" variant (added 2026-09-02) — same CTEs, but the WHERE clause
# is flipped: NO existing buy order anywhere (neither Jita system nor
# Perimeter). CANDIDATES_SQL above silently drops these entirely, even
# though a real sell-side market can exist for them — see
# config.NO_COMPETITION_BUY_PRICE_PCT for why that's actually the ideal
# case for this app's "first in line" strategy, not a reason to skip them.
FIRST_MOVER_SQL = f"""
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
    sum(if(not is_buy_order, volume_remain, 0)) as jita_sell_volume,
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
snapshot as (
  select type_id, item_name, avg_daily_volume_14d
  from `{config.TABLE_MARKET_SNAPSHOTS}`
  qualify row_number() over (partition by type_id order by scanned_at desc) = 1
)
select
  s.type_id, s.item_name,
  js.jita_sell_min as sell_min,
  coalesce(d.jita_sell_volume, 0) as sell_volume,
  coalesce(d.jita_sell_orders, 0) as sell_order_count,
  s.avg_daily_volume_14d
from snapshot s
left join jita_buy jb using (type_id)
left join jita_sell js using (type_id)
left join jita_depth d using (type_id)
left join perim_buy pb using (type_id)
where s.avg_daily_volume_14d > 0
  and js.jita_sell_min is not null
  and jb.jita_buy_max is null
  and pb.perim_buy_max is null
"""


def price_tier(buy_price: float) -> str:
    """Display-only price bucket (added 2026-09-02) — see config.py's
    PRICE_TIER_* comment. Does not affect ranking, filtering, or sizing."""
    if buy_price < config.PRICE_TIER_CHEAP_MAX:
        return "levné"
    if buy_price < config.PRICE_TIER_MID_MAX:
        return "střední"
    return "drahé"


def density_band(density_per_1000: float) -> str:
    if density_per_1000 <= config.DENSITY_LOW_MAX:
        return "low"
    if density_per_1000 <= config.DENSITY_MEDIUM_MAX:
        return "medium"
    return "high"


def compute_risk_band(buy_order_count: int, sell_order_count: int, avg_daily_volume_14d: float,
                       require_buy_side: bool = True) -> str:
    """Replaces a bare density_band(density_per_1000) call at every call site
    — added 2026-09-02 after a real diagnostic run (eval/debug_candidates.py)
    showed the density ratio (order_count / avg_daily_volume_14d * 1000)
    structurally explodes for anything trading only a few units/day: a
    perfectly normal order count divided by a near-zero volume gives a
    density in the thousands, which is always > DENSITY_MEDIUM_MAX. Result:
    ALL 486 "střední" and ALL 641 "drahé" candidates in that run were
    risk_band="high", 100% — not because they were actually thin/risky, but
    because the ratio's denominator was too small to be meaningful for
    higher-priced, lower-unit-volume items (ships, faction/deadspace
    modules) by design — they're never going to trade in the hundreds/
    thousands of units per day the ratio implicitly assumes.

    Below DENSITY_MIN_VOLUME_FOR_RATIO, fall back to an absolute order-count
    floor (MIN_ORDERS_PER_SIDE) instead of the ratio — this only guards
    against a single stray/troll order defining the top-of-book price, not
    against genuinely low volume (capital sizing already caps position size
    for anything unaffordable, so an extremely expensive low-volume item
    doesn't need risk_band to also gate it out).

    require_buy_side=False is for the first-mover path (rank_first_mover_candidates)
    where buy_order_count is always 0 BY DESIGN (that's the whole point — no
    existing buy order) — the absolute-floor fallback there only checks the
    sell side, since checking buy_order_count would always trip and silently
    zero out every first-mover candidate below DENSITY_MIN_VOLUME_FOR_RATIO."""
    if avg_daily_volume_14d < config.DENSITY_MIN_VOLUME_FOR_RATIO:
        sell_thin = sell_order_count < config.MIN_ORDERS_PER_SIDE
        buy_thin = require_buy_side and buy_order_count < config.MIN_ORDERS_PER_SIDE
        return "high" if (sell_thin or buy_thin) else "low"
    density_per_1000 = (buy_order_count + sell_order_count) / avg_daily_volume_14d * 1000
    return density_band(density_per_1000)


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
    df["risk_band"] = df.apply(
        lambda r: compute_risk_band(r["buy_order_count"], r["sell_order_count"], r["avg_daily_volume_14d"]), axis=1
    )
    df["margin_pct"] = df.apply(lambda r: margin_pct(r["buy_max"], r["sell_min"]), axis=1)

    df["profit_per_unit"] = df["sell_min"] * (1 - config.BROKER_FEE_RATE - config.SALES_TAX_RATE) \
        - df["buy_max"] * (1 + config.BROKER_FEE_RATE)

    # Liquidity gate replaced 2026-09-02: raw standing-order counts
    # (MIN_ORDERS_PER_SIDE) let through 344/344 candidates that were cheap
    # niche items (compressed gases/ores) — orders on the book prove
    # someone parked a price, not that the item actually trades. Real
    # signal = realized daily volume weighted by how much profit each unit
    # is worth. See config.MIN_DAILY_PROFIT_TURNOVER (starter guess, not
    # yet calibrated against a real run).
    df["daily_profit_turnover"] = df["avg_daily_volume_14d"] * df["profit_per_unit"]

    # Filter: margin floor + ceiling, liquidity-weighted turnover. Floor/
    # ceiling mirror the same blunt sanity checks
    # bigquery/recompute_top_of_book.sql already uses — confirmed 2026-09-01
    # needed here too after a real run surfaced margins like
    # 46071%/9954%/9076% on near-zero-liquidity items — classic
    # reference-price artifacts on a thin book, not real opportunities.
    #
    # risk_band is NOT a hard filter here anymore — dropped 2026-09-02.
    # eval/debug_candidates.py showed density_per_1000's ratio check created
    # a dead zone between 10M and 1.6B ISK (nothing in "střední" and only
    # ultra-rare items in "drahé" could ever clear DENSITY_MEDIUM_MAX=2.0)
    # — the ratio was simply too strict for anything but very-high-volume
    # cheap items, and MIN_DAILY_PROFIT_TURNOVER above is a better liquidity
    # gate anyway (weighted by real profit, not raw order count). risk_band
    # is still computed and shown on the dashboard as an informational
    # column — Matej confirmed 2026-09-02: drop it as a filter, keep it as
    # a label.
    df = df[
        (df["margin_pct"] >= config.MARGIN_FLOOR_PCT)
        & (df["margin_pct"] <= config.MARGIN_CEILING_PCT)
        & (df["daily_profit_turnover"] >= config.MIN_DAILY_PROFIT_TURNOVER)
    ]
    df = df.sort_values("profit_per_unit", ascending=False).reset_index(drop=True)
    df["price_tier"] = df["buy_max"].apply(price_tier)

    # Reserve a slice of the budget for first-mover candidates (no existing
    # buy order) BEFORE the main ranked walk spends it — otherwise the main
    # walk (which runs first, sorted by profit_per_unit) could exhaust
    # available_capital and starve step 3b entirely. See
    # config.FIRST_MOVER_BUDGET_PCT.
    first_mover_budget = config.FIRST_MOVER_BUDGET_PCT * available_capital
    main_budget = available_capital - first_mover_budget

    # Two-phase walk added 2026-09-02, same day risk_band stopped filtering
    # (see above): once "střední"/"drahé" candidates could appear, a real
    # run showed "levné" collapse from ~250 candidates to 30 — sorting by
    # absolute profit_per_unit means a handful of expensive positions (each
    # near PER_POSITION_PCT's 20%-of-budget cap) ate almost the entire
    # main_budget before the walk ever got deep into the (individually
    # cheap) levné rows. Confirmed with Matej 2026-09-02: the tier split
    # already makes browsing fine, but 30 absolute candidates felt too thin.
    # Fix: guarantee "levné" a budget floor (LEVNE_RESERVED_BUDGET_PCT of
    # main_budget) via its own walk BEFORE the general walk spends the
    # rest — same pattern as the first-mover reservation above, just for
    # cheap/high-turnover items instead of no-buy-order items. Any of that
    # reserved slice the levné walk doesn't spend rolls into the general
    # walk (see `remaining_general` below), so it's never wasted.
    levne_reserved = config.LEVNE_RESERVED_BUDGET_PCT * main_budget
    general_budget = main_budget - levne_reserved

    df_levne = df[df["price_tier"] == "levné"]
    levne_rows, levne_spent, levne_bought_ids = _walk_budget(df_levne, levne_reserved, main_budget)

    remaining_general = general_budget + (levne_reserved - levne_spent)
    df_rest = df[~df["type_id"].isin(levne_bought_ids)]
    general_rows, _general_spent, _general_bought_ids = _walk_budget(df_rest, remaining_general, main_budget)

    result = pd.DataFrame(levne_rows + general_rows)

    first_mover = rank_first_mover_candidates(client, first_mover_budget, exclude_type_ids | set(df["type_id"]))
    if not first_mover.empty:
        result = pd.concat([result, first_mover], ignore_index=True) if not result.empty else first_mover

    if result.empty:
        print("[sizing] no candidates sized — budget unallocated. See rule #4 in "
              "eve-jita-own-infra: may indicate thin watchlist, not a bug.")
    return result


def _walk_budget(df: pd.DataFrame, budget: float, total_budget: float):
    """Shared sequential budget-deduction walk (profit_per_unit descending,
    already sorted by the caller) — extracted 2026-09-02 so
    rank_new_candidates() can run it twice (levné-reserved phase, then the
    general phase over everything else) without duplicating the loop.
    total_budget is always the FULL main_budget (not the smaller per-phase
    `budget`) so PER_POSITION_PCT's cap means the same thing regardless of
    which phase a row is sized in. Returns (rows, amount_spent, set_of_type_ids_bought)."""
    remaining = budget
    rows = []
    bought_ids = set()
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
        bought_ids.add(row.type_id)

        rows.append({
            "type_id": row.type_id,
            "item_name": row.item_name,
            "suggested_qty": qty,
            "suggested_price": round(row.buy_max + config.REPRICE_TICK, 2),
            "margin_pct": round(row.margin_pct, 1),
            "risk_band": row.risk_band,
            "density_per_1000": round(row.density_per_1000, 3),
            "est_cost": round(cost, 0),
            "price_tier": price_tier(row.buy_max),
            "candidate_type": "ranked",
        })

    return rows, budget - remaining, bought_ids


def rank_first_mover_candidates(client, budget: float, exclude_type_ids: set) -> pd.DataFrame:
    """Step 3b — items with NO existing buy order anywhere (Jita system or
    Perimeter) but real sell-side trading activity. Priced at
    sell_min * config.NO_COMPETITION_BUY_PRICE_PCT — deliberately a
    lowball, since there's no competing buy order to beat (confirmed with
    Matej 2026-09-02: "Malou, skoro nulovou. Spousta lidi nekontroluje buy
    ordery a jen to proda."). Uses its own reserved budget slice (see
    config.FIRST_MOVER_BUDGET_PCT) so these don't crowd out the main
    ranked walk despite their artificially huge profit_per_unit.

    No margin ceiling here (by design — a huge margin is the point of
    lowballing, not a reference-price artifact like in the main path), but
    still gated by the same turnover-based liquidity floor.
    """
    if budget <= 0:
        return pd.DataFrame()

    df = bq.query_df(client, FIRST_MOVER_SQL)
    if df.empty:
        return df
    if exclude_type_ids:
        df = df[~df["type_id"].isin(exclude_type_ids)]
    if df.empty:
        return df

    df["buy_max"] = (df["sell_min"] * config.NO_COMPETITION_BUY_PRICE_PCT).round(2)
    df["buy_order_count"] = 0
    df["buy_volume"] = 0

    df["density_per_1000"] = (df["buy_order_count"] + df["sell_order_count"]) / df["avg_daily_volume_14d"] * 1000
    df["risk_band"] = df.apply(
        lambda r: compute_risk_band(r["buy_order_count"], r["sell_order_count"], r["avg_daily_volume_14d"],
                                     require_buy_side=False),
        axis=1,
    )
    df["margin_pct"] = df.apply(lambda r: margin_pct(r["buy_max"], r["sell_min"]), axis=1)
    df["profit_per_unit"] = df["sell_min"] * (1 - config.BROKER_FEE_RATE - config.SALES_TAX_RATE) \
        - df["buy_max"] * (1 + config.BROKER_FEE_RATE)
    df["daily_profit_turnover"] = df["avg_daily_volume_14d"] * df["profit_per_unit"]

    # risk_band no longer a hard filter — see the matching comment in
    # rank_new_candidates() above (same 2026-09-02 decision, applies here too).
    df = df[df["daily_profit_turnover"] >= config.MIN_DAILY_PROFIT_TURNOVER]
    df = df.sort_values("profit_per_unit", ascending=False).reset_index(drop=True)

    remaining = budget
    rows = []
    for row in df.itertuples():
        if remaining <= 0:
            break
        # No buy-side depth exists (that's the whole point) — cap sizing by
        # sell-side liquidity only, so we don't size a position bigger than
        # the market can plausibly absorb on the way back out.
        depth_cap = row.sell_volume * config.DEPTH_CAP_FRACTION
        per_position_cap = (config.PER_POSITION_PCT * budget) / row.buy_max if row.buy_max else 0
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
            "suggested_price": row.buy_max,
            "margin_pct": round(row.margin_pct, 1),
            "risk_band": row.risk_band,
            "density_per_1000": round(row.density_per_1000, 3),
            "est_cost": round(cost, 0),
            "price_tier": price_tier(row.buy_max),
            "candidate_type": "first_mover",
        })

    return pd.DataFrame(rows)
