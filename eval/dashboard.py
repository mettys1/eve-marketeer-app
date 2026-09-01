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

<h2>New buy order candidates</h2>
{candidates_table}

<h2>Net worth &amp; capital decomposition</h2>
{net_worth_chart}

<h2>Realized vs. unrealized profit</h2>
{profit_chart}

</body>
</html>
"""


def _fmt_isk(v: float) -> str:
    return f"{v:,.0f} ISK".replace(",", " ")


def _headline_cards(headline: dict) -> str:
    delta_class = "positive" if headline["net_worth_delta"] >= 0 else "negative"
    reserve_class = "positive" if headline["reserve_ok"] else "warn"

    cards = [
        HEADLINE_CARD_TEMPLATE.format(
            label="Net worth (Δ vs. včera)",
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
            label="Realizovaný zisk (včera)",
            value=_fmt_isk(headline["realized_profit_yesterday"]),
            value_class="positive" if headline["realized_profit_yesterday"] >= 0 else "negative",
        ),
        HEADLINE_CARD_TEMPLATE.format(
            label="Rezerva (cíl 1%)",
            value=f"{_fmt_isk(headline['cash_today'])} / {_fmt_isk(headline['reserve_target'])}",
            value_class=reserve_class,
        ),
    ]
    return "\n".join(cards)


def _table_or_empty(df: pd.DataFrame, empty_msg: str) -> str:
    if df is None or df.empty:
        return f'<div class="empty-note">{empty_msg}</div>'
    return df.to_html(index=False, border=0, classes="", justify="left")


def _net_worth_chart(trend_df: pd.DataFrame) -> str:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=trend_df["run_date"], y=trend_df["cash"], name="Cash", stackgroup="one"))
    fig.add_trace(go.Scatter(x=trend_df["run_date"], y=trend_df["locked_buy"], name="Locked (buy)", stackgroup="one"))
    fig.add_trace(go.Scatter(x=trend_df["run_date"], y=trend_df["locked_sell"], name="Locked (sell)", stackgroup="one"))
    fig.add_trace(go.Scatter(x=trend_df["run_date"], y=trend_df["total"], name="Total", line=dict(color="#e8e8e8", width=2), stackgroup=None))

    default_start = trend_df["run_date"].max() - pd.Timedelta(days=config.DASHBOARD_DEFAULT_WINDOW_DAYS)
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
            range=[default_start, trend_df["run_date"].max()],
        ),
        legend=dict(orientation="h"),
    )
    return fig.to_html(full_html=False, include_plotlyjs="cdn")


def _profit_chart(trend_df: pd.DataFrame) -> str:
    # Weekly realized (from total delta as a proxy until wallet_journal-based
    # realized series is wired in — see kpi.py TODO) vs. unrealized (locked value).
    weekly = trend_df.set_index("run_date").resample("W").last().reset_index()
    weekly["realized_proxy"] = weekly["total"].diff()
    weekly["unrealized"] = weekly["locked_buy"] + weekly["locked_sell"]

    fig = make_subplots(specs=[[{"secondary_y": False}]])
    fig.add_trace(go.Bar(x=weekly["run_date"], y=weekly["realized_proxy"], name="Δ Net worth (weekly)"))
    fig.add_trace(go.Bar(x=weekly["run_date"], y=weekly["unrealized"], name="Locked (unrealized)"))
    fig.update_layout(
        template="plotly_dark",
        height=340,
        margin=dict(l=10, r=10, t=10, b=10),
        barmode="group",
        legend=dict(orientation="h"),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def render(headline: dict, orders_eval: pd.DataFrame, candidates: pd.DataFrame, trend_df: pd.DataFrame, open_browser: bool = True) -> str:
    config.DASHBOARD_OUTPUT_DIR.mkdir(exist_ok=True)
    run_date = datetime.now().strftime("%Y-%m-%d %H:%M")

    orders_display = orders_eval[["item_name", "placed_price", "new_price", "action", "reason", "reprice_cost_so_far"]] \
        if not orders_eval.empty else orders_eval
    candidates_display = candidates[["item_name", "suggested_qty", "suggested_price", "margin_pct", "risk_band", "est_cost"]] \
        if not candidates.empty else candidates

    html = PAGE_TEMPLATE.format(
        run_date=run_date,
        headline_cards=_headline_cards(headline),
        orders_table=_table_or_empty(orders_display, "Žádné otevřené buy ordery."),
        candidates_table=_table_or_empty(candidates_display, "Žádní noví kandidáti (viz log — možná tenký watchlist při aktuálních prazích)."),
        net_worth_chart=_net_worth_chart(trend_df),
        profit_chart=_profit_chart(trend_df),
    )

    out_path = config.DASHBOARD_OUTPUT_DIR / f"eval_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    out_path.write_text(html, encoding="utf-8")

    if open_browser:
        webbrowser.open(out_path.as_uri())

    return str(out_path)
