"""
run_eval.py — single entry point for the daily EVE market evaluation.

    python run_eval.py

Steps (in order — the order matters, see eval/sizing.py docstring for why
step 3 must come after step 2):

  1. refresh    -> eval/refresh.py    (subprocess: daily_ops.js, freshness-checked)
  2. orders     -> eval/orders.py     (existing buy orders: REPRICE / CANCEL)
  3. sizing     -> eval/sizing.py     (new buy order candidates, first-in-line)
  4. kpi        -> eval/kpi.py        (headline + net worth trend)
  5. features   -> eval/features.py   (background-only logging, no read, no model)
  6. dashboard  -> eval/dashboard.py  (renders + opens the HTML file)
"""

import sys

import config
from eval import bq, refresh, orders, sizing, kpi, features, dashboard


def main() -> int:
    client = bq.get_client()

    print("[1/6] refresh...")
    try:
        refresh.run_refresh(client)
    except RuntimeError as e:
        print(f"REFRESH FAILED: {e}", file=sys.stderr)
        return 1

    bq.assert_fresh_or_raise(client)

    print("[2/6] evaluating open orders...")
    orders_eval = orders.evaluate_open_orders(client)

    print("[3/6] sizing new candidates...")
    available_capital = sizing.compute_available_capital(client, orders_eval)
    already_held = set(orders_eval["type_id"]) if not orders_eval.empty else set()
    candidates = sizing.rank_new_candidates(client, available_capital, already_held)

    print("[4/6] capital review...")
    trend_df = kpi.build_trend(client)
    headline = kpi.build_headline(client, trend_df)

    print("[5/6] logging ml features (background only)...")
    try:
        features.log_features(client, orders_eval, candidates)
    except Exception as e:
        # Logging must never block the day's actual output.
        print(f"[features] non-fatal error, continuing: {e}")

    print("[6/6] rendering dashboard...")
    out_path = dashboard.render(headline, orders_eval, candidates, trend_df)
    print(f"done -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
