"""Self-contained HTML report for the C-J import pipeline (same look as eval/dashboard.py)."""

from __future__ import annotations

import html
import webbrowser
from datetime import datetime

import pandas as pd

import config
from cj.analysis import CjResult

CSS = """
  body { font-family: -apple-system, Segoe UI, sans-serif; background: #14161a; color: #e8e8e8; margin: 0; padding: 24px 32px; }
  h1 { font-size: 20px; font-weight: 600; margin-bottom: 4px; }
  .subtitle { color: #8a8f98; font-size: 13px; margin-bottom: 20px; }
  .headline { display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
  .card { background: #1c1f26; border-radius: 10px; padding: 16px 20px; min-width: 150px; flex: 1; }
  .card-label { font-size: 12px; color: #8a8f98; margin-bottom: 6px; }
  .card-value { font-size: 22px; font-weight: 600; }
  .positive { color: #4ade80; } .negative { color: #f87171; } .muted { color: #8a8f98; }
  .warnbox { background: #3a2a0c; border: 1px solid #fbbf24; color: #fde68a; border-radius: 8px; padding: 10px 14px; margin-bottom: 8px; font-size: 13px; }
  h2 { font-size: 15px; font-weight: 600; margin: 28px 0 10px; border-bottom: 1px solid #2a2e36; padding-bottom: 6px; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 20px; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #2a2e36; white-space: nowrap; }
  th { color: #8a8f98; font-weight: 500; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  textarea { width: 100%; min-height: 140px; background: #1c1f26; color: #e8e8e8; border: 1px solid #2a2e36; border-radius: 6px; font-family: Consolas, monospace; font-size: 13px; padding: 8px; }
  .note { color: #8a8f98; font-size: 12px; margin: -10px 0 18px; }
"""


def _isk(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 1e3:
        return f"{v / 1e3:.1f}k"
    return f"{v:,.2f}"


def _num(v, fmt="{:,.0f}") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return fmt.format(v)


def _pct(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    cls = "positive" if v > 0 else "negative"
    return f'<span class="{cls}">{v:.1f}%</span>'


def _table(df: pd.DataFrame, cols: list[tuple[str, str, callable, bool]], empty: str) -> str:
    if df is None or df.empty:
        return f'<div class="muted">{html.escape(empty)}</div>'
    head = "".join(f'<th class="{"num" if n else ""}">{html.escape(h)}</th>' for _, h, _, n in cols)
    body = []
    for r in df.to_dict("records"):
        tds = "".join(f'<td class="{"num" if n else ""}">{f(r.get(k))}</td>' for k, _, f, n in cols)
        body.append(f"<tr>{tds}</tr>")
    return f"<table><tr>{head}</tr>{''.join(body)}</table>"


def _esc(v) -> str:
    return html.escape(str(v)) if v is not None else "—"


def render(res: CjResult, open_browser: bool = True):
    recs = res.recommendations
    buy = recs[recs.qty > 0] if not recs.empty else recs
    total_isk = float(buy.isk.sum()) if not buy.empty else 0.0
    total_m3 = float(buy.m3.sum()) if not buy.empty else 0.0
    total_ship = float((buy.ship_unit * buy.qty).sum()) if not buy.empty else 0.0
    total_profit = float(buy.profit_total.sum()) if not buy.empty else 0.0

    cards = [
        ("Nákup v Jitě (A)", _isk(total_isk), ""),
        ("Doprava", _isk(total_ship), ""),
        ("Objem", f"{total_m3:,.0f} m³", ""),
        ("Očekávaný zisk (po prodeji)", _isk(total_profit), "positive" if total_profit > 0 else "negative"),
        ("Pokrytí C-J snapshoty", f"{res.covered_hours:.0f} h / {res.snapshots} snímků", ""),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="card-label">{l}</div><div class="card-value {c}">{v}</div></div>'
        for l, v, c in cards)
    warn_html = "".join(f'<div class="warnbox">⚠ {html.escape(w)}</div>' for w in res.warnings)

    rec_cols = [
        ("name", "Modul", _esc, False),
        ("qty", "Ks", _num, True),
        ("cost_a_final", "Jita nákup A", _isk, True),
        ("cost_b", "Buy order B", _isk, True),
        ("ship_unit", "Doprava/ks", _isk, True),
        ("cj_sell_min", "C-J sell", _isk, True),
        ("spread_pct", "Rozdíl", _pct, True),
        ("profit_a", "Zisk/ks A", _isk, True),
        ("margin_a", "Marže A", _pct, True),
        ("margin_b", "Marže B", _pct, True),
        ("demand_day", "Prodej C-J/den", lambda v: _num(v, "{:,.1f}"), True),
        ("demand_src", "Zdroj", _esc, False),
        ("delve_day", "Delve/den", lambda v: _num(v, "{:,.1f}"), True),
        ("cj_sell_volume", "C-J nabídka ks", _num, True),
        ("competitor_days", "Nabídka dní", lambda v: _num(v, "{:,.1f}"), True),
        ("my_cj_stock", "Moje v C-J", _num, True),
        ("isk", "ISK", _isk, True),
        ("m3", "m³", _num, True),
        ("profit_total", "Zisk celkem", _isk, True),
        ("note", "", _esc, False),
    ]
    instant_cols = [
        ("name", "Modul", _esc, False), ("units", "Ks", _num, True),
        ("jita_avg", "Jita nákup", _isk, True), ("ship_unit", "Doprava/ks", _isk, True),
        ("cj_buy_max", "C-J buy order", _isk, True), ("margin_pct", "Marže", _pct, True),
        ("profit_total", "Zisk celkem", _isk, True), ("m3", "m³", _num, True),
    ]
    gap_cols = [
        ("name", "Modul", _esc, False), ("delve_daily", "Delve/den", lambda v: _num(v, "{:,.1f}"), True),
        ("jita_sell_min", "Jita sell min", _isk, True), ("ship_unit", "Doprava/ks", _isk, True),
        ("min_cj_price", "Min. cena v C-J", _isk, True), ("cj_buy_max", "C-J buy order", _isk, True),
    ]

    multibuy = "\n".join(f"{r['name']}\t{int(r['qty'])}" for r in buy.to_dict("records")) if not buy.empty else ""
    rejected = ", ".join(f"{k}: {v}" for k, v in res.rejected_counts.items() if v) or "—"
    run_at = res.run["run_at"]
    perim = res.perimeter_scan_at if res.perimeter_scan_at is not None else "—"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    page = f"""<!DOCTYPE html>
<html lang="cs"><head><meta charset="utf-8"><title>C-J import — {now}</title><style>{CSS}</style></head>
<body>
<h1>C-J6MT import — co zavézt z Jity</h1>
<div class="subtitle">Report {now} · C-J snapshot {run_at} · Jita scan {res.run.get('jita_reused_scan_at') or 'vlastní (cj job)'} · Perimeter {perim}</div>
{warn_html}
<div class="headline">{cards_html}</div>

<h2>Doporučení — top {config.CJ_TOP_N} podle očekávaného zisku/den</h2>
<div class="note">A = koupit hned ze sell orderů (Jita{' + Perimeter' if config.CJ_INCLUDE_PERIMETER_SELLS else ''}, průměr přes knihu pro dané množství) ·
B = buy order (max buy Jita/Perimeter + tick + SCC) · prodej za aktuální C-J sell min, po broker fee + sales tax ·
doprava = packaged m³ × {_num(config.CJ_SHIPPING_ISK_PER_M3 or 0)} ISK · množství = {config.CJ_STOCK_DAYS} dní poptávky − moje zásoba v C-J, strop {config.CJ_PER_POSITION_PCT:.0%} z rozpočtu {_isk(config.CJ_BUDGET_ISK)}</div>
{_table(recs, rec_cols, "Nic neprošlo filtry.")}
<div class="note">Vyřazeno: {rejected}</div>

<h2>Multibuy (Jita)</h2>
<textarea readonly onclick="this.select()">{html.escape(multibuy)}</textarea>

<h2>C-J buy ordery nad cenou z Jity (prodej rovnou do buy orderu, bez broker fee)</h2>
{_table(res.instant, instant_cols, "Nic.")}

<h2>Díry v C-J — žádný sell order, ale poptávka v Delve</h2>
<div class="note">Bez C-J dat o prodejích — jen tip na ruční ověření. Min. cena = cena, při které vychází cílová marže {config.CJ_MIN_MARGIN_PCT:g} %.</div>
{_table(res.gaps, gap_cols, "Nic.")}
</body></html>"""

    config.DASHBOARD_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = config.DASHBOARD_OUTPUT_DIR / f"cj_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    out.write_text(page, encoding="utf-8")
    if open_browser:
        webbrowser.open(out.as_uri())
    return out
