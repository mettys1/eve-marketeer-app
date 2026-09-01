"""
Step 4 — Profitability and capital review.

Rewritten 2026-09-01: `net_worth_history` does not exist — confirmed against
LIVE BigQuery (`bq ls eve-jita-scanner-21359:eve_jita_scanner`), not just
schema.sql — and no Cloud Run Job writes one. Rather than build new GCP
infra for this, this reuses the exact same, already-tested SQL that
esi-oauth-service/server.js's `/report` endpoint already runs
(`wallet_capital`, `trading_pnl_daily`) — through the same BigQuery client
this app already uses everywhere else. No redeploy, no new table, no new
secrets.

Two outputs:
1. headline: the ~4-5 numbers the dashboard shows first.
2. trend_df: daily realized P&L + end-of-day wallet cash balance, full
   history (from wallet_transactions/wallet_journal) — the dashboard's
   Plotly rangeslider/rangeselector handles the date-range picker
   client-side.

Known limitation (inherited from wallet_capital's own comment, not new):
`balance`/`balance_eod` is wallet CASH only — it does not include ISK
currently locked in open buy-order escrow. headline's net_worth_today adds
today's live escrow back in for a same-day-accurate number; trend_df's daily
history does not have that adjustment applied retroactively (my_orders is a
live snapshot, not a clean daily history, so a fully accurate historical
net-worth series isn't reconstructable from what's collected today).
"""

import pandas as pd

import config
from eval import bq

WALLET_CAPITAL_SQL = f"""
with latest_balance as (
  select balance as current_balance, date as as_of
  from `{config.TABLE_WALLET_JOURNAL}`
  order by date desc limit 1
),
balance_24h_ago as (
  select balance
  from `{config.TABLE_WALLET_JOURNAL}`
  where date <= timestamp_sub(current_timestamp(), interval 24 hour)
  order by date desc limit 1
),
latest_orders as (
  select * except(rn) from (
    select *, row_number() over (partition by order_id order by scanned_at desc) as rn
    from `{config.TABLE_MY_ORDERS}`
  ) where rn = 1 and (is_open is null or is_open = true)
),
escrow as (
  select
    sum(price * volume_remain) as locked_in_buy_orders,
    countif(coalesce(is_buy_order, false)) as open_order_count
  from latest_orders
  where coalesce(is_buy_order, false)
)
select
  lb.current_balance, lb.as_of,
  b24.balance as balance_24h_ago,
  lb.current_balance - b24.balance as wallet_change_24h,
  esc.locked_in_buy_orders, esc.open_order_count
from latest_balance lb
left join balance_24h_ago b24 on true
left join escrow esc on true
"""

# CASH-ONLY (see module docstring) — used both for kpi.py's trend chart and
# by sizing.py (compute_available_capital) for the current cash figure.
CURRENT_CASH_SQL = f"""
select balance as cash
from `{config.TABLE_WALLET_JOURNAL}`
order by date desc
limit 1
"""

TRADING_PNL_DAILY_SQL = f"""
with daily_tx as (
  select date(date) as day,
    sum(if(is_buy, quantity * unit_price, 0)) as buy_spend,
    sum(if(not is_buy, quantity * unit_price, 0)) as sell_revenue
  from `{config.TABLE_WALLET_TRANSACTIONS}`
  group by day
),
daily_fees as (
  select date(date) as day,
    sum(if(ref_type = 'brokers_fee', -amount, 0)) as broker_fees,
    sum(if(ref_type = 'transaction_tax', -amount, 0)) as sales_tax,
    sum(if(ref_type = 'market_provider_tax', -amount, 0)) as scc_surcharge
  from `{config.TABLE_WALLET_JOURNAL}`
  group by day
),
daily_balance as (
  select day, balance from (
    select date(date) as day, balance,
      row_number() over (partition by date(date) order by date desc) as rn
    from `{config.TABLE_WALLET_JOURNAL}`
  ) where rn = 1
)
select
  t.day, t.buy_spend, t.sell_revenue,
  ifnull(f.broker_fees, 0) as broker_fees,
  ifnull(f.sales_tax, 0) as sales_tax,
  ifnull(f.scc_surcharge, 0) as scc_surcharge,
  (t.sell_revenue - t.buy_spend - ifnull(f.broker_fees, 0) - ifnull(f.sales_tax, 0) - ifnull(f.scc_surcharge, 0))
    as net_cash_pnl,
  b.balance as balance_eod
from daily_tx t
left join daily_fees f using (day)
left join daily_balance b using (day)
order by t.day
"""


def get_current_cash(client) -> float:
    row = bq.query_df(client, CURRENT_CASH_SQL)
    if row.empty:
        raise RuntimeError(
            "wallet_journal has no rows — run esi-wallet-poller "
            "(refresh_wallet.sh / refresh step) at least once first."
        )
    return float(row.iloc[0]["cash"])


def build_trend(client) -> pd.DataFrame:
    df = bq.query_df(client, TRADING_PNL_DAILY_SQL)
    if not df.empty:
        df["day"] = pd.to_datetime(df["day"])
    return df


def build_headline(client, trend_df: pd.DataFrame) -> dict:
    wc = bq.query_df(client, WALLET_CAPITAL_SQL)
    if wc.empty or pd.isna(wc.iloc[0]["current_balance"]):
        raise RuntimeError(
            "wallet_journal has no rows — run esi-wallet-poller at least once first."
        )
    w = wc.iloc[0]

    cash_today = float(w["current_balance"])
    locked_isk = float(w["locked_in_buy_orders"]) if pd.notna(w["locked_in_buy_orders"]) else 0.0
    net_worth_today = cash_today + locked_isk

    # capital_change_24h_approx from the /report version of this query: cash
    # delta over 24h, escrow held constant (we don't have a 24h-ago escrow
    # figure) — this IS the net worth delta approximation, not a separate one.
    wallet_change_24h = (
        float(w["wallet_change_24h"]) if pd.notna(w["wallet_change_24h"]) else None
    )
    net_worth_delta = wallet_change_24h if wallet_change_24h is not None else 0.0
    base_yesterday = net_worth_today - net_worth_delta
    net_worth_delta_pct = (net_worth_delta / base_yesterday * 100) if base_yesterday else 0.0

    realized_profit_yesterday = 0.0
    if not trend_df.empty:
        today = pd.Timestamp.now().normalize()
        past = trend_df[trend_df["day"] < today]
        target_row = past.iloc[-1] if not past.empty else trend_df.iloc[-1]
        realized_profit_yesterday = float(target_row["net_cash_pnl"])

    reserve_target = config.CAPITAL_RESERVE_PCT * net_worth_today
    reserve_ok = cash_today >= reserve_target

    return {
        "net_worth_today": net_worth_today,
        "net_worth_delta": net_worth_delta,
        "net_worth_delta_pct": net_worth_delta_pct,
        "open_order_count": int(w["open_order_count"]) if pd.notna(w["open_order_count"]) else 0,
        "locked_isk": locked_isk,
        "realized_profit_yesterday": realized_profit_yesterday,
        "cash_today": cash_today,
        "reserve_target": float(reserve_target),
        "reserve_ok": bool(reserve_ok),
    }
