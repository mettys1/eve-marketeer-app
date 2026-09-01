"""
Step 4 — Profitability and capital review.

Two outputs:
1. headline: dict of the ~4-5 numbers the dashboard shows first (net worth
   delta, open order count/locked ISK, yesterday's realized profit, reserve
   check).
2. trend_df: daily net_worth_history rows for the capital review charts
   (net worth decomposition, realized vs unrealized), full history returned
   — the dashboard's Plotly rangeslider/rangeselector handles the date-range
   picker client-side, no need to pre-filter by window here.

TODO(Matej): net_worth_history column names (cash, locked_buy, locked_sell,
total, run_date) assumed from the skill description, not verified.
"""

import pandas as pd

import config
from eval import bq

NET_WORTH_TREND_SQL = f"""
select run_date, cash, locked_buy, locked_sell, total
from `{config.TABLE_NET_WORTH_HISTORY}`
order by run_date
"""

YESTERDAY_REALIZED_PROFIT_SQL = f"""
select coalesce(sum(amount), 0) as realized_profit
from `{config.TABLE_WALLET_JOURNAL}`
where date(ref_date) = date_sub(current_date(), interval 1 day)
  and ref_type in ('market_transaction', 'market_escrow')
"""

OPEN_ORDERS_SUMMARY_SQL = f"""
select count(*) as open_order_count, sum(price * volume_remain) as locked_isk
from `{config.TABLE_MY_ORDERS}`
where is_buy_order = true and state = 'open'
"""


def build_trend(client) -> pd.DataFrame:
    return bq.query_df(client, NET_WORTH_TREND_SQL)


def build_headline(client, trend_df: pd.DataFrame) -> dict:
    if trend_df.empty or len(trend_df) < 2:
        raise RuntimeError("net_worth_history needs at least 2 rows to show a day-over-day delta.")

    today = trend_df.iloc[-1]
    yesterday = trend_df.iloc[-2]
    net_worth_delta = today["total"] - yesterday["total"]
    net_worth_delta_pct = (net_worth_delta / yesterday["total"] * 100) if yesterday["total"] else 0.0

    orders_summary = bq.query_df(client, OPEN_ORDERS_SUMMARY_SQL).iloc[0]
    realized = bq.query_df(client, YESTERDAY_REALIZED_PROFIT_SQL).iloc[0]["realized_profit"]

    reserve_target = config.CAPITAL_RESERVE_PCT * today["total"]
    reserve_ok = today["cash"] >= reserve_target

    return {
        "net_worth_today": float(today["total"]),
        "net_worth_delta": float(net_worth_delta),
        "net_worth_delta_pct": float(net_worth_delta_pct),
        "open_order_count": int(orders_summary["open_order_count"] or 0),
        "locked_isk": float(orders_summary["locked_isk"] or 0),
        "realized_profit_yesterday": float(realized),
        "cash_today": float(today["cash"]),
        "reserve_target": float(reserve_target),
        "reserve_ok": bool(reserve_ok),
    }
