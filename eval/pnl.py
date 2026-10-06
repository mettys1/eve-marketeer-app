"""
Step 4b — Real (lot-accounted) P&L. Added 2026-10-06.

Why this exists: kpi.TRADING_PNL_DAILY_SQL's `net_cash_pnl` is a CASH FLOW
(sell revenue - buy spend - fees), not profit. Every day you build inventory
it shows a "loss", every day you liquidate it shows a "gain" — it says
nothing about whether a trade actually made money. This module computes
realized profit per SALE instead.

Two outputs, both over the last config.PNL_WINDOW_DAYS days (rolling, UTC):
  1. company_pnl  — P&L statement for the whole operation
  2. item_pnl     — the same per type_id, only for items that SOLD in the window
Plus daily_pnl — realized net profit per day (full history) for the chart.

Accounting rules (agreed with Matej 2026-10-06):

* Cost of goods sold = FIFO per type_id, across all locations (Perimeter buys
  and Jita sells are the same inventory). Built from the full
  wallet_transactions history, not just the window, so lots bought before the
  window are costed correctly.
* Opening inventory: anything sold that was never bought inside the logged
  history (pre-logging stock, loot, contracts) is an "unknown cost" virtual
  lot consumed FIRST (it is by definition the oldest stock). Units sold out of
  it are NOT counted in profit — their revenue is reported separately
  (`unknown_cost_*`), never with an assumed zero cost.
* Buy-side order fees (brokers_fee + market_provider_tax/SCC on buy orders,
  incl. reprices) are PRODUCT costs: capitalized into each unit bought as a
  per-type average (all attributed buy-side fees / all units bought) and
  realized only when that unit sells.
* Sell-side order fees and sales tax are PERIOD costs: expensed in the window
  they are charged. Sales tax is per sale (journal transaction_tax linked via
  context_id = transaction_id; config.SALES_TAX_RATE estimate if unlinked).
* Fee -> order attribution: the wallet journal does not say which order a
  brokers_fee belongs to. It is matched to my_orders by timestamp —
  ESI `issued` is set on placement AND reset on every modify, which is
  exactly when the fee is charged. Fees within config.PNL_FEE_MATCH_TOLERANCE_SEC
  of an order's issued time are attributed to it (split by order value if
  several orders share that second). Known gap: an order placed AND
  modified/filled between two my_orders polls is never seen, so its fee stays
  unmatched. Unmatched fees are never dropped — they are expensed in the
  company P&L as "nepřiřazené poplatky". Coverage is reported every run.

Invariant: sum(item_pnl.net_profit) + company-only lines == company net profit.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

import config
from eval import bq

ORDER_FEE_TYPES = ("brokers_fee", "market_provider_tax")
TAX_TYPE = "transaction_tax"

TX_SQL = f"""
select transaction_id, date, type_id, item_name, quantity, unit_price, is_buy, location_id
from `{config.TABLE_WALLET_TRANSACTIONS}`
"""

FEES_SQL = f"""
select journal_id, date, ref_type, -amount as fee, context_id, context_id_type
from `{config.TABLE_WALLET_JOURNAL}`
where ref_type in ('brokers_fee', 'market_provider_tax', 'transaction_tax')
"""

# One row per (order_id, issued) = one placement or modify event.
ORDER_EVENTS_SQL = f"""
select order_id, issued,
  any_value(type_id) as type_id,
  any_value(item_name) as item_name,
  logical_or(coalesce(is_buy_order, false)) as is_buy_order,
  max(price * volume_total) as order_value
from `{config.TABLE_MY_ORDERS}`
where issued is not null
group by order_id, issued
"""


# --------------------------------------------------------------------------
# Pure functions (no BigQuery) — unit-testable on synthetic frames.
# --------------------------------------------------------------------------

def fifo_sells(tx: pd.DataFrame) -> pd.DataFrame:
    """One row per SELL transaction with FIFO cost of its known-cost units.

    Returns columns: transaction_id, date, type_id, item_name, quantity,
    unit_price, revenue, known_qty, unknown_qty, cogs.
    """
    cols = ["transaction_id", "date", "type_id", "item_name", "quantity",
            "unit_price", "revenue", "known_qty", "unknown_qty", "cogs"]
    if tx.empty:
        return pd.DataFrame(columns=cols)

    # Buys before sells at the same timestamp (an instant flip must find the stock).
    t = tx.assign(_side=(~tx["is_buy"].astype(bool)).astype(int)) \
          .sort_values(["type_id", "date", "_side", "transaction_id"])

    out = []
    for type_id, g in t.groupby("type_id", sort=False):
        signed = g["quantity"].where(g["is_buy"].astype(bool), -g["quantity"])
        opening_unknown = max(0, -int(signed.cumsum().min()))

        lots: deque = deque()  # [qty_left, unit_cost or None]
        if opening_unknown:
            lots.append([opening_unknown, None])

        for r in g.itertuples(index=False):
            if r.is_buy:
                lots.append([int(r.quantity), float(r.unit_price)])
                continue
            need, known, unknown, cogs = int(r.quantity), 0, 0, 0.0
            while need > 0 and lots:
                lot = lots[0]
                take = min(need, lot[0])
                if lot[1] is None:
                    unknown += take
                else:
                    known += take
                    cogs += take * lot[1]
                lot[0] -= take
                need -= take
                if lot[0] == 0:
                    lots.popleft()
            unknown += need  # cannot happen given opening_unknown, kept as a guard
            out.append((r.transaction_id, r.date, type_id, r.item_name, int(r.quantity),
                        float(r.unit_price), r.quantity * float(r.unit_price),
                        known, unknown, cogs))
    return pd.DataFrame(out, columns=cols)


def attribute_order_fees(fees: pd.DataFrame, orders: pd.DataFrame, tol_sec: int) -> pd.DataFrame:
    """Split each brokers_fee / market_provider_tax journal entry onto the
    order event(s) issued within ±tol_sec of it, weighted by order value.

    Returns one row per (journal entry, matched order) — or one row with
    type_id = NA for unmatched entries. Columns: journal_id, date, ref_type,
    fee, type_id, is_buy_order, matched.
    """
    f = fees[fees["ref_type"].isin(ORDER_FEE_TYPES)].copy()
    cols = ["journal_id", "date", "ref_type", "fee", "type_id", "is_buy_order", "matched"]
    if f.empty:
        return pd.DataFrame(columns=cols).astype({"fee": float, "matched": bool})

    f["_sec"] = f["date"].dt.floor("s").astype("int64") // 10**9
    if orders.empty:
        cand = pd.DataFrame()
    else:
        o = orders.copy()
        o["_sec"] = o["issued"].dt.floor("s").astype("int64") // 10**9
        o["order_value"] = o["order_value"].fillna(0).clip(lower=0)
        expanded = pd.concat(
            [f[["journal_id", "_sec"]].assign(_sec=f["_sec"] + d) for d in range(-tol_sec, tol_sec + 1)]
        )
        cand = expanded.merge(o[["_sec", "order_id", "type_id", "is_buy_order", "order_value"]], on="_sec")
        cand = cand.drop_duplicates(["journal_id", "order_id"])

    rows = []
    cand_by_j = dict(tuple(cand.groupby("journal_id"))) if not cand.empty else {}
    for r in f.itertuples(index=False):
        c = cand_by_j.get(r.journal_id)
        if c is None or c.empty:
            rows.append((r.journal_id, r.date, r.ref_type, r.fee, pd.NA, pd.NA, False))
            continue
        w = c["order_value"]
        w = w / w.sum() if w.sum() > 0 else pd.Series(1.0 / len(c), index=c.index)
        for (_, o_row), share in zip(c.iterrows(), w):
            rows.append((r.journal_id, r.date, r.ref_type, r.fee * share,
                         o_row["type_id"], bool(o_row["is_buy_order"]), True))
    return pd.DataFrame(rows, columns=cols).astype({"matched": bool})


def sales_tax_per_sell(sells: pd.DataFrame, fees: pd.DataFrame, est_rate: float) -> pd.Series:
    """Actual transaction_tax linked by context_id = transaction_id; estimate otherwise."""
    tax = fees[fees["ref_type"] == TAX_TYPE].groupby("context_id")["fee"].sum()
    linked = sells["transaction_id"].map(tax)
    return linked.fillna(sells["revenue"] * est_rate), linked.notna()


@dataclass
class PnlResult:
    company: pd.DataFrame      # statement: line, isk
    items: pd.DataFrame        # per type_id, sold in window
    daily: pd.DataFrame        # day, net_profit (full history)
    coverage: dict             # diagnostics
    window_start: datetime
    window_end: datetime


def compute(tx: pd.DataFrame, fees: pd.DataFrame, orders: pd.DataFrame,
            window_days: int, tol_sec: int, sales_tax_rate: float,
            now: datetime | None = None) -> PnlResult:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=window_days)

    # ---- FIFO + per-sell costs -------------------------------------------
    sells = fifo_sells(tx)
    sells["sales_tax"], tax_linked = sales_tax_per_sell(sells, fees, sales_tax_rate)
    sells["known_share"] = (sells["known_qty"] / sells["quantity"]).fillna(0)

    attr = attribute_order_fees(fees, orders, tol_sec)
    matched = attr[attr["matched"]]
    buy_fees_by_type = matched[matched["is_buy_order"] == True].groupby("type_id")["fee"].sum()  # noqa: E712
    units_bought = tx[tx["is_buy"].astype(bool)].groupby("type_id")["quantity"].sum()
    buy_fee_per_unit = (buy_fees_by_type / units_bought).dropna()
    sells["buy_fees_cap"] = sells["type_id"].map(buy_fee_per_unit).fillna(0) * sells["known_qty"]

    # Sell-side order fees are period costs per type, assigned to that type's
    # sells in the same window pro rata by revenue; then split known/unknown.
    sell_fees = matched[matched["is_buy_order"] == False].copy()  # noqa: E712

    def window_frame(lo, hi):
        s = sells[(sells["date"] >= lo) & (sells["date"] < hi)].copy()
        sf = sell_fees[(sell_fees["date"] >= lo) & (sell_fees["date"] < hi)]
        sf_by_type = sf.groupby("type_id")["fee"].sum()
        rev_by_type = s.groupby("type_id")["revenue"].transform("sum")
        s["sell_fees"] = s["type_id"].map(sf_by_type).fillna(0) * (s["revenue"] / rev_by_type).fillna(0)
        sold_types = set(s["type_id"])
        sell_fees_no_sale = float(sf_by_type[~sf_by_type.index.isin(sold_types)].sum())
        return s, sell_fees_no_sale

    def statement(lo, hi):
        s, sell_fees_no_sale = window_frame(lo, hi)
        k = s["known_share"]
        revenue = (s["revenue"] * k).sum()
        cogs = s["cogs"].sum()
        buy_cap = s["buy_fees_cap"].sum()
        tax = (s["sales_tax"] * k).sum()
        sfees = (s["sell_fees"] * k).sum()

        f_win = fees[(fees["date"] >= lo) & (fees["date"] < hi)]
        a_win = attr[(attr["date"] >= lo) & (attr["date"] < hi)]
        unmatched_order_fees = a_win.loc[~a_win["matched"], "fee"].sum()
        # Buy fees on types never bought (cancelled/unfilled orders) can't be capitalized.
        a_buy = a_win[a_win["matched"] & (a_win["is_buy_order"] == True)]  # noqa: E712
        uncapitalizable = a_buy.loc[~a_buy["type_id"].isin(buy_fee_per_unit.index), "fee"].sum()
        tax_residual = f_win.loc[f_win["ref_type"] == TAX_TYPE, "fee"].sum() - s["sales_tax"].sum()

        unknown_rev = (s["revenue"] * (1 - k)).sum()
        unknown_costs = (s["sales_tax"] * (1 - k)).sum() + (s["sell_fees"] * (1 - k)).sum()

        net = (revenue - cogs - buy_cap - tax - sfees - sell_fees_no_sale
               - unmatched_order_fees - uncapitalizable - tax_residual)
        return s, {
            "revenue": revenue, "cogs": cogs, "buy_cap": buy_cap, "tax": tax,
            "sell_fees": sfees, "sell_fees_no_sale": sell_fees_no_sale,
            "unmatched_order_fees": unmatched_order_fees,
            "uncapitalizable": uncapitalizable, "tax_residual": tax_residual,
            "unknown_rev": unknown_rev, "unknown_costs": unknown_costs,
            "unknown_units": int(s["unknown_qty"].sum()), "net": net,
        }

    s, st = statement(start, now)

    # ---- company statement -----------------------------------------------
    gross = st["revenue"] - st["cogs"] - st["buy_cap"]
    tx_win = tx[(tx["date"] >= start) & (tx["date"] < now)]
    buy_spend = (tx_win.loc[tx_win["is_buy"].astype(bool), "quantity"]
                 * tx_win.loc[tx_win["is_buy"].astype(bool), "unit_price"]).sum()
    f_win = fees[(fees["date"] >= start) & (fees["date"] < now)]
    cash_flow = (s["revenue"].sum() - buy_spend - f_win["fee"].sum())
    company = pd.DataFrame([
        ("Tržby (prodeje se známým nákladem)", st["revenue"]),
        ("− Nákupní cena prodaného zboží (FIFO)", -st["cogs"]),
        ("− Nákupní poplatky v ceně prodaného zboží", -st["buy_cap"]),
        ("= Hrubý zisk", gross),
        ("− Daň z prodeje", -st["tax"]),
        ("− Poplatky za prodejní ordery (prodané položky)", -st["sell_fees"]),
        ("− Poplatky za prodejní ordery (bez prodeje v okně)", -st["sell_fees_no_sale"]),
        ("− Poplatky za nákupní ordery bez jediného nákupu", -st["uncapitalizable"]),
        ("− Nepřiřazené poplatky (order nenalezen)", -st["unmatched_order_fees"]),
        ("− Rozdíl daně (skutečnost vs. přiřazeno)", -st["tax_residual"]),
        ("= ČISTÝ REALIZOVANÝ ZISK", st["net"]),
        ("info: tržby bez známého nákladu (mimo zisk)", st["unknown_rev"]),
        ("info: z toho daň + poplatky (mimo zisk)", -st["unknown_costs"]),
        ("info: nákupy v okně (cash)", -buy_spend),
        ("info: peněžní tok z obchodování (stará metrika)", cash_flow),
    ], columns=["line", "isk"])

    # ---- item table --------------------------------------------------------
    if s.empty:
        items = pd.DataFrame(columns=["type_id", "item_name", "units_sold", "revenue", "cogs",
                                      "buy_fees", "sales_tax", "sell_fees", "net_profit",
                                      "net_margin_pct", "roi_pct", "unknown_cost_units",
                                      "unknown_cost_revenue"])
    else:
        k = s["known_share"]
        s = s.assign(
            k_units=s["known_qty"], k_rev=s["revenue"] * k, k_tax=s["sales_tax"] * k,
            k_sfees=s["sell_fees"] * k, u_rev=s["revenue"] * (1 - k),
        )
        items = s.groupby("type_id").agg(
            item_name=("item_name", "last"), units_sold=("k_units", "sum"),
            revenue=("k_rev", "sum"), cogs=("cogs", "sum"), buy_fees=("buy_fees_cap", "sum"),
            sales_tax=("k_tax", "sum"), sell_fees=("k_sfees", "sum"),
            unknown_cost_units=("unknown_qty", "sum"), unknown_cost_revenue=("u_rev", "sum"),
        ).reset_index()
        items["net_profit"] = (items["revenue"] - items["cogs"] - items["buy_fees"]
                               - items["sales_tax"] - items["sell_fees"])
        cost_basis = items["cogs"] + items["buy_fees"]
        items["net_margin_pct"] = (items["net_profit"] / items["revenue"] * 100).where(items["revenue"] > 0)
        items["roi_pct"] = (items["net_profit"] / cost_basis * 100).where(cost_basis > 0)
        items = items.sort_values("net_profit", ascending=False).reset_index(drop=True)

    # ---- daily series (full history) --------------------------------------
    daily_rows = []
    if not tx.empty:
        first_day = tx["date"].min().floor("D")
        day = first_day
        while day < now:
            _, d = statement(day, min(day + timedelta(days=1), now))
            daily_rows.append((day, d["net"], d["revenue"]))
            day += timedelta(days=1)
    daily = pd.DataFrame(daily_rows, columns=["day", "net_profit", "revenue"])

    # ---- coverage diagnostics ---------------------------------------------
    order_fee_total = attr["fee"].sum()
    coverage = {
        "order_fee_isk_matched_pct": (attr.loc[attr["matched"], "fee"].sum() / order_fee_total * 100)
                                     if order_fee_total else None,
        "order_fee_entries_unmatched": int(attr.loc[~attr["matched"], "journal_id"].nunique()),
        "sales_tax_linked_pct": (tax_linked.mean() * 100) if len(tax_linked) else None,
        "unknown_cost_units_window": st["unknown_units"],
    }

    return PnlResult(company, items, daily, coverage, start, now)


def net_profit(res: PnlResult) -> float:
    c = res.company
    return float(c.loc[c["line"].str.startswith("= ČISTÝ"), "isk"].iloc[0])


# --------------------------------------------------------------------------
# BigQuery entry point
# --------------------------------------------------------------------------

def _utc(df: pd.DataFrame, *cols: str) -> pd.DataFrame:
    for c in cols:
        if c in df:
            df[c] = pd.to_datetime(df[c], utc=True)
    return df


def build_pnl(client) -> PnlResult:
    tx = _utc(bq.query_df(client, TX_SQL), "date")
    fees = _utc(bq.query_df(client, FEES_SQL), "date")
    orders = _utc(bq.query_df(client, ORDER_EVENTS_SQL), "issued")
    if not tx.empty:
        tx["is_buy"] = tx["is_buy"].fillna(False).astype(bool)
    res = compute(tx, fees, orders,
                  window_days=config.PNL_WINDOW_DAYS,
                  tol_sec=config.PNL_FEE_MATCH_TOLERANCE_SEC,
                  sales_tax_rate=config.SALES_TAX_RATE)
    c = res.coverage
    print(f"[pnl] okno {res.window_start:%d.%m %H:%M} – {res.window_end:%d.%m %H:%M} UTC, "
          f"čistý zisk {net_profit(res):,.0f} ISK, {len(res.items)} prodaných položek")
    print(f"[pnl] coverage: order fee ISK přiřazeno {c['order_fee_isk_matched_pct'] or 0:.1f}% "
          f"({c['order_fee_entries_unmatched']} nepřiřazených záznamů), "
          f"daň linkovaná {c['sales_tax_linked_pct'] or 0:.1f}%, "
          f"kusy bez známého nákladu v okně: {c['unknown_cost_units_window']}")
    return res


if __name__ == "__main__":
    # Standalone: python -m eval.pnl  — prints both tables, no dashboard.
    pd.set_option("display.width", 200, "display.max_columns", 20, "display.float_format", "{:,.0f}".format)
    r = build_pnl(bq.get_client())
    print(r.company.to_string(index=False))
    print()
    print(r.items.to_string(index=False))
