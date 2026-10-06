"""
Renders one self-contained HTML file, no server needed. Layout (top to
bottom) matches what was agreed:

1. Headline cards      — net worth delta, open orders/locked ISK, profit
                          yesterday, reserve check
2. Action queue         — existing orders (REPRICE/CANCEL) + new candidates,
                          read-only tables, nothing to click/persist
3. Capital review       — net worth decomposition + realized/unrealized,
                          Plotly rangeselector (30d/90d/All) as the date
                          picker, entirely client-side
"""

import webbrowser
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import config
from eval import pnl

HEADLINE_CARD_TEMPLATE = """
<div class="card">
  <div class="card-label">{label}</div>
  <div class="card-value {value_class}">{value}</div>
</div>
"""

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>EVE Marketeer — Daily Eval ({run_date})</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, sans-serif; background: #14161a; color: #e8e8e8; margin: 0; padding: 24px 32px; }}
  h1 {{ font-size: 20px; font-weight: 600; margin-bottom: 4px; }}
  .subtitle {{ color: #8a8f98; font-size: 13px; margin-bottom: 24px; }}
  .headline {{ display: flex; gap: 16px; margin-bottom: 32px; flex-wrap: wrap; }}
  .card {{ background: #1c1f26; border-radius: 10px; padding: 16px 20px; min-width: 160px; flex: 1; }}
  .card-label {{ font-size: 12px; color: #8a8f98; margin-bottom: 6px; }}
  .card-value {{ font-size: 22px; font-weight: 600; }}
  .positive {{ color: #4ade80; }}
  .negative {{ color: #f87171; }}
  .warn {{ color: #fbbf24; }}
  h2 {{ font-size: 15px; font-weight: 600; margin: 28px 0 10px; border-bottom: 1px solid #2a2e36; padding-bottom: 6px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; font-size: 13px; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #2a2e36; }}
  th {{ color: #8a8f98; font-weight: 500; }}
  table.pnl td.num, table.pnl th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  table.pnl tr.total td {{ font-weight: 600; border-top: 1px solid #8a8f98; }}
  table.pnl tr.info td {{ color: #8a8f98; }}
  .empty-note {{ color: #8a8f98; font-size: 13px; padding: 8px 0 20px; }}
</style>
</head>
<body>
<h1>EVE Marketeer — Daily Eval</h1>
<div class="subtitle">Run: {run_date}</div>

<div class="headline">
  {headline_cards}
</div>

<h2>Existing orders — reprice / cancel</h2>
{orders_table}

<h2>New buy order candidates — levné (&lt; {tier_cheap_max})</h2>
{candidates_table_cheap}

<h2>New buy order candidates — střední ({tier_cheap_max} – {tier_mid_max})</h2>
{candidates_table_mid}

<h2>New buy order candidates — drahé (&gt;= {tier_mid_max})</h2>
{candidates_table_expensive}

<h2>P&amp;L firmy — posledních {pnl_days} dní</h2>
<div class="subtitle">{pnl_window}</div>
{pnl_company_table}
<div class="subtitle">{pnl_coverage}</div>

<h2>P&amp;L po položkách — prodáno za posledních {pnl_days} dní</h2>
{pnl_items_table}

<h2>Net worth &amp; capital decomposition</h2>
{net_worth_chart}

<h2>Čistý realizovaný zisk po dnech (FIFO)</h2>
{profit_chart}

</body>
</html>
"""


def _fmt_isk(v: float) -> str:
    v = round(v) or 0  # no "-0 ISK"
    return f"{v:,.0f} ISK".replace(",", " ")


def _headline_cards(headline: dict, pnl_result=None) -> str:
    delta_class = "positive" if headline["net_worth_delta"] >= 0 else "negative"
    reserve_class = "positive" if headline["reserve_ok"] else "warn"

    last_checkpoint_at = headline.get("last_checkpoint_at")
    if last_checkpoint_at is not None:
        # BigQuery TIMESTAMP comes back UTC — labelled explicitly so it's never
        # mistaken for a fixed "24h ago" window (that was the actual bug: the
        # old delta silently compared to exactly 24h ago, which is wrong
        # whenever this isn't run every single day — see kpi.py's docstring).
        net_worth_label = f"Net worth (Δ vs. {last_checkpoint_at.strftime('%d.%m %H:%M')} UTC)"
    else:
        net_worth_label = "Net worth (první report — zatím bez srovnání)"

    cards = [
        HEADLINE_CARD_TEMPLATE.format(
            label=net_worth_label,
            value=f"{_fmt_isk(headline['net_worth_today'])} "
                  f"({headline['net_worth_delta_pct']:+.1f}%)",
            value_class=delta_class,
        ),
        HEADLINE_CARD_TEMPLATE.format(
            label="Otevřené buy ordery",
            value=f"{headline['open_order_count']} ks / {_fmt_isk(headline['locked_isk'])}",
            value_class="",
        ),
        HEADLINE_CARD_TEMPLATE.format(
            # Added 2026-09-08 — sell orders are real net worth (cancel one and
            # you have the item back), just not cash, so they now count toward
            # net_worth_today too (see kpi.py). This card breaks that piece out
            # so it's visible, not silently folded into one big number.
            label="Vystaveno k prodeji",
            value=f"{headline['sell_order_count']} ks / {_fmt_isk(headline['listed_isk'])}",
            value_class="",
        ),
        _profit_card(headline, pnl_result),
        HEADLINE_CARD_TEMPLATE.format(
            label="Rezerva (cíl 1%)",
            value=f"{_fmt_isk(headline['cash_today'])} / {_fmt_isk(headline['reserve_target'])}",
            value_class=reserve_class,
        ),
    ]
    return "\n".join(cards)


def _profit_card(headline: dict, pnl_result) -> str:
    # 2026-10-06: the old card showed kpi's net_cash_pnl for yesterday — a cash
    # flow (buys count as losses), not profit. Real FIFO net profit replaces it.
    if pnl_result is None:
        v, label = headline["realized_profit_yesterday"], "Peněžní tok z obchodování (včera)"
    else:
        v, label = pnl.net_profit(pnl_result), f"Čistý zisk ({config.PNL_WINDOW_DAYS} dní, FIFO)"
    return HEADLINE_CARD_TEMPLATE.format(
        label=label, value=_fmt_isk(v), value_class="positive" if v >= 0 else "negative",
    )


def _pnl_company_table(res) -> str:
    rows = []
    for line, isk in zip(res.company["line"], res.company["isk"]):
        cls = "total" if line.startswith("=") else "info" if line.startswith("info:") else ""
        val_cls = "positive" if line.startswith("= ČISTÝ") and isk >= 0 else \
                  "negative" if line.startswith("= ČISTÝ") else ""
        rows.append(f'<tr class="{cls}"><td>{line.removeprefix("info: ")}</td>'
                    f'<td class="num {val_cls}">{_fmt_isk(isk)}</td></tr>')
    return '<table class="pnl" style="max-width:640px">' + "".join(rows) + "</table>"


def _pnl_items_table(res) -> str:
    if res.items.empty:
        return '<div class="empty-note">Za posledních %d dní nic neprodáno.</div>' % config.PNL_WINDOW_DAYS
    cols = [("item_name", "Položka", None), ("units_sold", "Ks", "int"), ("revenue", "Tržby", "isk"),
            ("cogs", "Nákup (FIFO)", "isk"), ("buy_fees", "Nák. poplatky", "isk"),
            ("sales_tax", "Daň", "isk"), ("sell_fees", "Prod. poplatky", "isk"),
            ("net_profit", "Čistý zisk", "isk"), ("net_margin_pct", "Marže %", "pct"),
            ("roi_pct", "ROI %", "pct"), ("unknown_cost_units", "Ks bez nákladu", "int")]
    head = "".join(f'<th class="{"num" if k else ""}">{h}</th>' for _, h, k in cols)
    body = []
    for r in res.items.itertuples(index=False):
        tds = []
        for c, _, kind in cols:
            v = getattr(r, c)
            if kind is None:
                tds.append(f"<td>{v}</td>")
                continue
            if pd.isna(v):
                txt = "–"
            elif kind == "pct":
                txt = f"{v:.1f}"
            elif kind == "int":
                txt = f"{int(v):,}".replace(",", " ")
            else:
                txt = f"{v:,.0f}".replace(",", " ")
            cls = ("positive" if v >= 0 else "negative") if c == "net_profit" else ""
            tds.append(f'<td class="num {cls}">{txt}</td>')
        body.append("<tr>" + "".join(tds) + "</tr>")
    return f'<table class="pnl"><tr>{head}</tr>{"".join(body)}</table>'


def _pnl_coverage(res) -> str:
    c = res.coverage
    parts = []
    if c["order_fee_isk_matched_pct"] is not None:
        parts.append(f"Broker/SCC poplatky na položky: {c['order_fee_isk_matched_pct']:.0f} % ISK "
                     f"(order {c['order_fee_by_order_pct']:.0f} % + dopočet z fillů {c['order_fee_by_fill_pct']:.0f} %; "
                     f"{c['order_fee_entries_unmatched']} záznamů zbývá → „Nepřiřazené poplatky“)")
    d = c.get("perimeter_scc_new_rate_derived")
    if d is not None:
        parts.append(f"SCC nový order Perimeter: z dat {d:.3%} vs. config {config.PERIMETER_SCC_NEW_RATE:.3%}")
    if c["sales_tax_linked_pct"] is not None:
        parts.append(f"Daň z prodeje napárovaná na transakci: {c['sales_tax_linked_pct']:.0f} % "
                     f"(zbytek odhad {config.SALES_TAX_RATE:.3%})")
    if c["unknown_cost_units_window"]:
        parts.append(f"{c['unknown_cost_units_window']} prodaných ks bez známého nákupu — mimo zisk")
    return " · ".join(parts)


def _table_or_empty(df: pd.DataFrame, empty_msg: str) -> str:
    if df is None or df.empty:
        return f'<div class="empty-note">{empty_msg}</div>'
    return df.to_html(index=False, border=0, classes="", justify="left")


def _net_worth_chart(trend_df: pd.DataFrame) -> str:
    if trend_df.empty:
        return '<div class="empty-note">Zatím žádná historie (wallet_transactions/wallet_journal jsou prázdné — spusť refresh alespoň jednou).</div>'
    # trend_df is daily wallet CASH (balance_eod) — there is no net_worth_history
    # table, so there's no clean historical series of locked (escrow) capital to
    # stack on top of it (my_orders is a live snapshot, not a daily history).
    # Today's locked ISK is shown separately in the headline cards instead.
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=trend_df["day"], y=trend_df["balance_eod"], name="Cash (EOD)",
        line=dict(color="#e8e8e8", width=2), fill="tozeroy",
    ))

    default_start = trend_df["day"].max() - pd.Timedelta(days=config.DASHBOARD_DEFAULT_WINDOW_DAYS)
    fig.update_layout(
        template="plotly_dark",
        height=380,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis=dict(
            rangeslider=dict(visible=True),
            rangeselector=dict(buttons=[
                dict(count=30, label="30d", step="day", stepmode="backward"),
                dict(count=90, label="90d", step="day", stepmode="backward"),
                dict(step="all", label="Vše"),
            ]),
            range=[default_start, trend_df["day"].max()],
        ),
        legend=dict(orientation="h"),
        annotations=[dict(
            text="Jen cash — neobsahuje ISK zamrzlé v otevřených buy orderech (viz karta nahoře)",
            xref="paper", yref="paper", x=0, y=1.08, showarrow=False,
            font=dict(size=11, color="#8a8f98"),
        )],
    )
    return fig.to_html(full_html=False, include_plotlyjs="cdn")


def _profit_chart(trend_df: pd.DataFrame, pnl_result=None) -> str:
    if pnl_result is not None and not pnl_result.daily.empty:
        df = pnl_result.daily.copy()
        df["cumulative"] = df["net_profit"].cumsum()
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(go.Bar(
            x=df["day"], y=df["net_profit"], name="Čistý zisk (denně)",
            marker_color=["#4ade80" if v >= 0 else "#f87171" for v in df["net_profit"]],
        ), secondary_y=False)
        fig.add_trace(go.Scatter(x=df["day"], y=df["cumulative"], name="Kumulativně",
                                 line=dict(color="#e8e8e8", width=2)), secondary_y=True)
        fig.update_layout(template="plotly_dark", height=340, margin=dict(l=10, r=10, t=10, b=10),
                          legend=dict(orientation="h"))
        # The net-worth chart above normally loads plotly.js; if it rendered its
        # empty note instead, this chart has to load it itself.
        return fig.to_html(full_html=False, include_plotlyjs=False if not trend_df.empty else "cdn")
    return _cash_flow_chart(trend_df)


def _cash_flow_chart(trend_df: pd.DataFrame) -> str:
    if trend_df.empty:
        return '<div class="empty-note">Zatím žádná historie realizovaného zisku.</div>'
    # Daily realized P&L (wallet_transactions netted against fees from
    # wallet_journal, same query as esi-oauth-service's trading_pnl_daily
    # report) — a cash-based approximation, not true lot-accounted P&L (see
    # kpi.py docstring). Cumulative line added for trend feel.
    df = trend_df.copy()
    df["cumulative_pnl"] = df["net_cash_pnl"].cumsum()

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=df["day"], y=df["net_cash_pnl"], name="Realizovaný P&L (denně)"), secondary_y=False)
    fig.add_trace(go.Scatter(
        x=df["day"], y=df["cumulative_pnl"], name="Kumulativně",
        line=dict(color="#4ade80", width=2),
    ), secondary_y=True)
    fig.update_layout(
        template="plotly_dark",
        height=340,
        margin=dict(l=10, r=10, t=10, b=10),
        barmode="group",
        legend=dict(orientation="h"),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def render(headline: dict, orders_eval: pd.DataFrame, candidates: pd.DataFrame, trend_df: pd.DataFrame, open_browser: bool = True,
           available_capital: float = None, pnl_result=None) -> str:
    config.DASHBOARD_OUTPUT_DIR.mkdir(exist_ok=True)
    run_date = datetime.now().strftime("%Y-%m-%d %H:%M")

    orders_display = orders_eval[["item_name", "placed_price", "new_price", "action", "reason", "reprice_cost_so_far"]] \
        if not orders_eval.empty else orders_eval

    # candidate_type added 2026-09-02 ("ranked" vs "first_mover" — step 3b,
    # no existing buy order, see eval/sizing.rank_first_mover_candidates)
    # so first-mover rows are visibly distinguishable, not silently mixed in.
    candidate_cols = ["item_name", "candidate_type", "suggested_qty", "suggested_price", "margin_pct", "risk_band", "est_cost"]
    if candidates.empty:
        cheap_display = mid_display = expensive_display = candidates
    else:
        cheap_display = candidates[candidates["price_tier"] == "levné"][candidate_cols]
        mid_display = candidates[candidates["price_tier"] == "střední"][candidate_cols]
        expensive_display = candidates[candidates["price_tier"] == "drahé"][candidate_cols]

    empty_candidates_msg = "Žádní noví kandidáti v tomhle cenovém pásmu (viz log — možná tenký watchlist při aktuálních prazích)."
    if available_capital is not None and available_capital <= 0:
        # Added 2026-09-28 — don't blame the watchlist when the real cause is budget.
        empty_candidates_msg = (
            f"Žádný volný kapitál pro nové ordery: available_capital = {_fmt_isk(available_capital)}. "
            "Cash po odečtení rezervy nestačí — viz log [sizing]."
        )

    html = PAGE_TEMPLATE.format(
        run_date=run_date,
        headline_cards=_headline_cards(headline, pnl_result),
        orders_table=_table_or_empty(orders_display, "Žádné otevřené buy ordery."),
        tier_cheap_max=_fmt_isk(config.PRICE_TIER_CHEAP_MAX),
        tier_mid_max=_fmt_isk(config.PRICE_TIER_MID_MAX),
        candidates_table_cheap=_table_or_empty(cheap_display, empty_candidates_msg),
        candidates_table_mid=_table_or_empty(mid_display, empty_candidates_msg),
        candidates_table_expensive=_table_or_empty(expensive_display, empty_candidates_msg),
        net_worth_chart=_net_worth_chart(trend_df),
        profit_chart=_profit_chart(trend_df, pnl_result),
        pnl_days=config.PNL_WINDOW_DAYS,
        pnl_window=(f"{pnl_result.window_start:%d.%m.%Y %H:%M} – {pnl_result.window_end:%d.%m.%Y %H:%M} UTC"
                    if pnl_result else ""),
        pnl_company_table=_pnl_company_table(pnl_result) if pnl_result else
            '<div class="empty-note">P&amp;L nespočítáno.</div>',
        pnl_coverage=_pnl_coverage(pnl_result) if pnl_result else "",
        pnl_items_table=_pnl_items_table(pnl_result) if pnl_result else "",
    )

    out_path = config.DASHBOARD_OUTPUT_DIR / f"eval_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    out_path.write_text(html, encoding="utf-8")

    if open_browser:
        webbrowser.open(out_path.as_uri())

    return str(out_path)
