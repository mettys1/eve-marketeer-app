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

Fixed 2026-09-08 — "Net worth (Δ vs. last update)" used to be a hardcoded
trailing-24h wallet-cash delta (see WALLET_CAPITAL_SQL's old balance_24h_ago
CTE), which silently breaks whenever Matej doesn't run this every single
day: he came back after several days, net worth had genuinely dropped
~516M ISK (5.23B -> 4.71B, confirmed by diffing two actual report files),
and the badge still showed +4.8%, because it only ever compared to exactly
24h ago and never included escrow at all. Now uses net_worth_checkpoints
(see config.TABLE_NET_WORTH_CHECKPOINTS) instead: one row per rendered
report (net_worth, cash, locked_isk, recorded_at), so the delta is always
against the true previous report, however long ago that was.

Also fixed 2026-09-08 — net worth only ever counted ISK locked in BUY
orders (escrow), never the value of open SELL orders. Matej: "jinak mi to
nedava spravu o nasem kapitalu" (otherwise it doesn't give an accurate
picture of our capital) — a sell order isn't cash, but it IS real value
(cancel it and you have the item back, sellable near that listed price),
so leaving it out understated net worth by however much is currently
listed for sale. WALLET_CAPITAL_SQL's escrow CTE now also sums
`listed_for_sale` (price * volume_remain over sell orders), and
net_worth_today includes it alongside cash + buy-order escrow.
"""

from datetime import datetime, timezone

import pandas as pd
from google.cloud import bigquery

import config
from eval import bq

CHECKPOINT_SCHEMA = [
    bigquery.SchemaField("recorded_at", "TIMESTAMP"),
    bigquery.SchemaField("net_worth", "FLOAT"),
    bigquery.SchemaField("cash", "FLOAT"),
    bigquery.SchemaField("locked_isk", "FLOAT"),
    bigquery.SchemaField("listed_isk", "FLOAT"),  # added 2026-09-08 — value of open sell orders
]

LAST_CHECKPOINT_SQL = f"""
select recorded_at, net_worth, cash, locked_isk
from `{config.TABLE_NET_WORTH_CHECKPOINTS}`
order by recorded_at desc
limit 1
"""

WALLET_CAPITAL_SQL = f"""
with latest_balance as (
  select balance as current_balance, date as as_of
  from `{config.TABLE_WALLET_JOURNAL}`
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
),
listed as (
  select
    sum(price * volume_remain) as listed_for_sale,
    countif(not coalesce(is_buy_order, false)) as sell_order_count
  from latest_orders
  where not coalesce(is_buy_order, false)
)
select
  lb.current_balance, lb.as_of,
  esc.locked_in_buy_orders, esc.open_order_count,
  lst.listed_for_sale, lst.sell_order_count
from latest_balance lb
left join escrow esc on true
left join listed lst on true
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


def ensure_checkpoint_table(client) -> None:
    table_ref = bigquery.TableReference.from_string(config.TABLE_NET_WORTH_CHECKPOINTS)
    try:
        table = client.get_table(table_ref)
    except Exception:
        table = bigquery.Table(table_ref, schema=CHECKPOINT_SCHEMA)
        client.create_table(table)
        print(f"[kpi] created {config.TABLE_NET_WORTH_CHECKPOINTS}")
        return

    # Schema evolution safety net: if this table was created by an earlier
    # version of this code (before listed_isk existed), patch it in place
    # instead of erroring on the next insert_rows_json call.
    existing_cols = {f.name for f in table.schema}
    missing = [f for f in CHECKPOINT_SCHEMA if f.name not in existing_cols]
    if missing:
        table.schema = list(table.schema) + missing
        client.update_table(table, ["schema"])
        print(f"[kpi] added column(s) {[f.name for f in missing]} to {config.TABLE_NET_WORTH_CHECKPOINTS}")


def get_last_checkpoint(client):
    """Returns the previous run's (net_worth, recorded_at), or (None, None)
    if this is the very first time a report has ever been rendered."""
    ensure_checkpoint_table(client)
    last = bq.query_df(client, LAST_CHECKPOINT_SQL)
    if last.empty:
        return None, None
    row = last.iloc[0]
    return float(row["net_worth"]), row["recorded_at"]


def record_checkpoint(client, net_worth: float, cash: float, locked_isk: float, listed_isk: float) -> None:
    """Called once per rendered report (see run_eval.py) so the NEXT run's
    delta is against the true previous report, however long ago that was —
    not a fixed 24h window."""
    ensure_checkpoint_table(client)
    row = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "net_worth": float(net_worth),
        "cash": float(cash),
        "locked_isk": float(locked_isk),
        "listed_isk": float(listed_isk),
    }
    errors = client.insert_rows_json(config.TABLE_NET_WORTH_CHECKPOINTS, [row])
    if errors:
        print(f"[kpi] checkpoint insert errors (non-fatal): {errors}")


def build_headline(client, trend_df: pd.DataFrame) -> dict:
    wc = bq.query_df(client, WALLET_CAPITAL_SQL)
    if wc.empty or pd.isna(wc.iloc[0]["current_balance"]):
        raise RuntimeError(
            "wallet_journal has no rows — run esi-wallet-poller at least once first."
        )
    w = wc.iloc[0]

    cash_today = float(w["current_balance"])
    locked_isk = float(w["locked_in_buy_orders"]) if pd.notna(w["locked_in_buy_orders"]) else 0.0
    listed_isk = float(w["listed_for_sale"]) if pd.notna(w["listed_for_sale"]) else 0.0
    net_worth_today = cash_today + locked_isk + listed_isk

    prev_net_worth, last_checkpoint_at = get_last_checkpoint(client)
    if prev_net_worth is not None:
        net_worth_delta = net_worth_today - prev_net_worth
        net_worth_delta_pct = (net_worth_delta / prev_net_worth * 100) if prev_net_worth else 0.0
    else:
        # First report ever rendered — nothing to compare against yet.
        net_worth_delta = 0.0
        net_worth_delta_pct = 0.0

    realized_profit_yesterday = 0.0
    if not trend_df.empty:
        today = pd.Timestamp.now().normalize()
        past = trend_df[trend_df["day"] < today]
        target_row = past.iloc[-1] if not past.empty else trend_df.iloc[-1]
        realized_profit_yesterday = float(target_row["net_cash_pnl"])

    reserve_target = config.CAPITAL_RESERVE_PCT * net_worth_today
    reserve_ok = cash_today >= reserve_target

    # Record THIS run as the new checkpoint, now that it's been compared
    # against the previous one — the next run's delta will be against this.
    record_checkpoint(client, net_worth_today, cash_today, locked_isk, listed_isk)

    return {
        "net_worth_today": net_worth_today,
        "net_worth_delta": net_worth_delta,
        "net_worth_delta_pct": net_worth_delta_pct,
        "last_checkpoint_at": last_checkpoint_at,  # None on the very first report ever
        "open_order_count": int(w["open_order_count"]) if pd.notna(w["open_order_count"]) else 0,
        "locked_isk": locked_isk,
        "sell_order_count": int(w["sell_order_count"]) if pd.notna(w["sell_order_count"]) else 0,
        "listed_isk": listed_isk,
        "realized_profit_yesterday": realized_profit_yesterday,
        "cash_today": cash_today,
        "reserve_target": float(reserve_target),
        "reserve_ok": bool(reserve_ok),
    }
