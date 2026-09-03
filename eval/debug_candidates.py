"""
One-off diagnostic — NOT part of the run_eval.py pipeline, not imported
anywhere. Added 2026-09-02 to answer a specific question: after poller.js
started force-including TOP_N_EXPENSIVE=500 items by ISK volume, the
dashboard still showed zero candidates in the "střední"/"drahé" price
tiers. This prints, by price tier, how many rows from the raw
CANDIDATES_SQL pool survive each individual filter in
eval/sizing.rank_new_candidates() — so we can see exactly which filter is
killing expensive items, instead of guessing.

Usage:
    python -m eval.debug_candidates
"""

import pandas as pd

import config
from eval import bq, orders as orders_mod, sizing
from eval.orders import margin_pct
from eval.sizing import CANDIDATES_SQL, price_tier, compute_risk_band


def main():
    client = bq.get_client()
    bq.assert_fresh_or_raise(client)

    df = bq.query_df(client, CANDIDATES_SQL)
    print(f"Raw candidate pool (before ANY filter): {len(df)} rows\n")

    df["price_tier"] = df["buy_max"].apply(price_tier)
    df["density_per_1000"] = (df["buy_order_count"] + df["sell_order_count"]) / df["avg_daily_volume_14d"] * 1000
    # Bug fixed 2026-09-02: this used to call density_band(density_per_1000) directly,
    # which bypasses the DENSITY_MIN_VOLUME_FOR_RATIO fallback added the same day —
    # meaning this diagnostic was silently NOT reflecting the actual fix in
    # eval/sizing.rank_new_candidates() (both used the same buggy path before the fix,
    # but only rank_new_candidates() got updated). Use the real function so this
    # script's numbers match what the pipeline actually does.
    df["risk_band"] = df.apply(
        lambda r: compute_risk_band(r["buy_order_count"], r["sell_order_count"], r["avg_daily_volume_14d"]), axis=1
    )
    df["margin_pct"] = df.apply(lambda r: margin_pct(r["buy_max"], r["sell_min"]), axis=1)
    df["profit_per_unit"] = df["sell_min"] * (1 - config.BROKER_FEE_RATE - config.SALES_TAX_RATE) \
        - df["buy_max"] * (1 + config.BROKER_FEE_RATE)
    df["daily_profit_turnover"] = df["avg_daily_volume_14d"] * df["profit_per_unit"]

    pass_margin_floor = df["margin_pct"] >= config.MARGIN_FLOOR_PCT
    pass_margin_ceiling = df["margin_pct"] <= config.MARGIN_CEILING_PCT
    pass_turnover = df["daily_profit_turnover"] >= config.MIN_DAILY_PROFIT_TURNOVER
    pass_risk = df["risk_band"] != "high"
    pass_all = pass_margin_floor & pass_margin_ceiling & pass_turnover & pass_risk

    print("=== Funnel by price tier (raw pool -> each individual filter, independently) ===\n")
    for tier in ["levné", "střední", "drahé"]:
        sub = df[df["price_tier"] == tier]
        if sub.empty:
            print(f"{tier}: 0 rows in raw pool at all (never scanned / no buy+sell order pair)\n")
            continue
        print(f"{tier}: {len(sub)} rows in raw pool")
        print(f"  survive margin >= {config.MARGIN_FLOOR_PCT}%:      {pass_margin_floor[sub.index].sum()}")
        print(f"  survive margin <= {config.MARGIN_CEILING_PCT}%:     {pass_margin_ceiling[sub.index].sum()}")
        print(f"  survive turnover >= {config.MIN_DAILY_PROFIT_TURNOVER:,.0f} ISK/day: {pass_turnover[sub.index].sum()}")
        print(f"  survive risk_band != high:            {pass_risk[sub.index].sum()}")
        print(f"  survive ALL filters combined:          {pass_all[sub.index].sum()}")
        print(f"  risk_band breakdown: {sub['risk_band'].value_counts().to_dict()}")
        print(f"  median avg_daily_volume_14d: {sub['avg_daily_volume_14d'].median():.3f}")
        print(f"  median density_per_1000: {sub['density_per_1000'].median():.2f}")
        print(f"  median margin_pct: {sub['margin_pct'].median():.1f}")
        print()

    print("=== 15 most expensive rows in the ENTIRE raw pool (any tier), full detail ===")
    top_expensive = df.sort_values("buy_max", ascending=False).head(15)
    cols = ["item_name", "buy_max", "sell_min", "avg_daily_volume_14d", "buy_order_count",
            "sell_order_count", "density_per_1000", "risk_band", "margin_pct", "daily_profit_turnover"]
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(top_expensive[cols].to_string(index=False))

    # Also check: of the rows that pass every filter, how many would actually get
    # sized qty > 0 given TODAY's real available_capital? A row can pass every
    # margin/turnover/risk filter and still never appear on the dashboard if
    # per_position_cap (20% of budget / buy_max) or depth_cap rounds its qty down
    # to 0 — that's a real "can't afford even one unit" outcome, not a bug, but
    # worth telling apart from a filter actually rejecting the row.
    print("\n=== Of rows passing ALL filters, would today's real capital size qty>0? ===")
    orders_eval = orders_mod.evaluate_open_orders(client)
    available_capital = sizing.compute_available_capital(client, orders_eval)
    main_budget = available_capital - config.FIRST_MOVER_BUDGET_PCT * available_capital
    print(f"available_capital={available_capital:,.0f} ISK, main_budget={main_budget:,.0f} ISK\n")

    passing = df[pass_all].copy()
    for tier in ["levné", "střední", "drahé"]:
        sub = passing[passing["price_tier"] == tier]
        if sub.empty:
            print(f"{tier}: 0 rows pass all filters — nothing to size")
            continue
        per_position_cap = (config.PER_POSITION_PCT * main_budget) / sub["buy_max"]
        depth_cap = sub[["buy_volume", "sell_volume"]].min(axis=1) * config.DEPTH_CAP_FRACTION
        affordable_cap = main_budget / (sub["buy_max"] * (1 + config.BROKER_FEE_RATE))
        qty = pd.concat([per_position_cap, depth_cap, affordable_cap], axis=1).min(axis=1).clip(upper=config.HARD_UNIT_CAP)
        sizeable = (qty.astype(int) > 0).sum()
        position_cap_isk = config.PER_POSITION_PCT * main_budget
        below_position_cap = (sub["buy_max"] <= position_cap_isk).sum()
        print(f"{tier}: {len(sub)} pass all filters, {sizeable} would get qty>0 with today's capital")
        print(f"  buy_max distribution (ISK): min={sub['buy_max'].min():,.0f} p25={sub['buy_max'].quantile(.25):,.0f} "
              f"median={sub['buy_max'].median():,.0f} p75={sub['buy_max'].quantile(.75):,.0f} max={sub['buy_max'].max():,.0f}")
        print(f"  per-position cap = {position_cap_isk:,.0f} ISK (20% of main_budget) — "
              f"{below_position_cap}/{len(sub)} rows have buy_max under that cap")

    print("\n=== Cheapest 15 rows passing ALL filters in 'drahé' (if any) ===")
    drahe_passing = passing[passing["price_tier"] == "drahé"].sort_values("buy_max")
    if drahe_passing.empty:
        print("(none)")
    else:
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(drahe_passing[["item_name", "buy_max", "sell_min", "avg_daily_volume_14d",
                                  "margin_pct", "risk_band", "daily_profit_turnover"]].head(15).to_string(index=False))


if __name__ == "__main__":
    main()
