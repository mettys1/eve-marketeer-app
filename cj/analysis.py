"""
C-J6MT import analysis — what to haul from Jita to the C-J Keepstar.

Data comes from esi-cj-poller (esi-jobs/job_cj.js); this module only reads
BigQuery and computes. Per module type:

  Jita cost   A = buy now: walk the Jita-system (+ Perimeter) sell book for the
                  suggested quantity -> volume-weighted average price, no fee
              B = buy order: max(Jita buy, Perimeter buy) + tick, + Perimeter SCC
  Shipping      = packaged_volume (m3) x CJ_SHIPPING_ISK_PER_M3
  C-J sell      = current C-J sell min (we match it; tick-size rules make a
                  fixed undercut meaningless), net of broker fee + sales tax
  Demand        = units/day sold in C-J, from diffs of consecutive C-J order
                  book snapshots (partial fills + share of vanished orders);
                  fallback C-J's region (Insmother) history x CJ_REGION_SHARE
  Quantity      = demand x CJ_STOCK_DAYS - my open C-J sell stock, capped by
                  per-position / total budget and cargo m3

Ranking = expected profit per day (profit/unit A x demand/day). Variant B is
shown alongside, not ranked on — it depends on getting filled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd
from google.cloud import bigquery

import config

TYPE_INFO_DEDUP = f"""
  SELECT type_id, ANY_VALUE(name) AS name, ANY_VALUE(category_id) AS category_id,
         ANY_VALUE(packaged_volume) AS packaged_volume
  FROM `{config.TABLE_TYPE_INFO}` GROUP BY type_id
"""

# Latest C-J snapshot of ANY mode — the hourly orders_only snapshots are newer than the
# last full run; the C-J book, buy book and fills must use them (fix 2026-10-12: they were
# anchored to the full run, so hourly snapshots were ignored and coverage stayed 0 h).
LATEST_SNAPSHOT_SQL = f"""
SELECT MAX(scanned_at) AS snap FROM `{config.TABLE_CJ_ORDERS_RAW}`
WHERE scan_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY) AND structure_id = @sid
"""

LATEST_RUN_SQL = f"""
SELECT * FROM `{config.TABLE_CJ_RUNS}`
WHERE mode = 'full' AND structure_id = @sid
ORDER BY run_at DESC LIMIT 1
"""

CJ_BOOK_SQL = f"""
WITH ti AS ({TYPE_INFO_DEDUP})
SELECT o.type_id, ti.name, ti.packaged_volume,
       MIN(IF(NOT o.is_buy_order, o.price, NULL))          AS cj_sell_min,
       SUM(IF(NOT o.is_buy_order, o.volume_remain, 0))     AS cj_sell_volume,
       COUNTIF(NOT o.is_buy_order)                         AS cj_sell_orders,
       MAX(IF(o.is_buy_order, o.price, NULL))              AS cj_buy_max,
       SUM(IF(o.is_buy_order, o.volume_remain, 0))         AS cj_buy_volume
FROM `{config.TABLE_CJ_ORDERS_RAW}` o
JOIN ti USING (type_id)
WHERE o.scan_date = DATE(@run_at) AND o.scanned_at = @run_at AND o.structure_id = @sid
  AND ti.category_id IN UNNEST(@cats)
GROUP BY o.type_id, ti.name, ti.packaged_volume
"""

# Individual C-J buy orders (latest snapshot) — the instant-sell section must walk this
# book: only the TOP order pays cj_buy_max, the rest pay less (bug in the first version,
# which priced the whole buy-side volume at the max).
CJ_BUYS_SQL = f"""
WITH ti AS ({TYPE_INFO_DEDUP})
SELECT o.type_id, o.price, o.volume_remain, o.min_volume
FROM `{config.TABLE_CJ_ORDERS_RAW}` o JOIN ti USING (type_id)
WHERE o.scan_date = DATE(@run_at) AND o.scanned_at = @run_at AND o.structure_id = @sid
  AND o.is_buy_order AND ti.category_id IN UNNEST(@cats)
"""

# Consecutive snapshot pairs -> per-type sell-side fills. Expired orders that
# vanish are not counted; vanished non-expired orders are "disappeared" (sold
# out or cancelled — indistinguishable), weighted in Python.
FILLS_SQL = f"""
WITH ti AS ({TYPE_INFO_DEDUP}),
scans AS (
  SELECT DISTINCT scanned_at FROM `{config.TABLE_CJ_ORDERS_RAW}`
  WHERE scan_date >= DATE_SUB(DATE(@run_at), INTERVAL @window_days DAY)
    AND scanned_at <= @run_at AND structure_id = @sid
),
pairs AS (
  SELECT scanned_at AS t0, LEAD(scanned_at) OVER (ORDER BY scanned_at) AS t1 FROM scans
),
good AS (
  SELECT t0, t1 FROM pairs
  WHERE t1 IS NOT NULL AND TIMESTAMP_DIFF(t1, t0, MINUTE) <= @max_gap_h * 60
),
s AS (
  SELECT o.scanned_at, o.type_id, o.order_id, o.volume_remain, o.issued, o.duration
  FROM `{config.TABLE_CJ_ORDERS_RAW}` o JOIN ti USING (type_id)
  WHERE o.scan_date >= DATE_SUB(DATE(@run_at), INTERVAL @window_days DAY)
    AND o.scanned_at <= @run_at AND o.structure_id = @sid
    AND NOT o.is_buy_order AND ti.category_id IN UNNEST(@cats)
  -- ESI paginates ~50 pages; if the cache rolls mid-fetch, page boundaries shift and an
  -- order can appear twice (-> double-counted fills) or be missing (-> fake "sold out").
  QUALIFY ROW_NUMBER() OVER (PARTITION BY o.scanned_at, o.order_id ORDER BY o.volume_remain) = 1
),
last_seen AS (SELECT order_id, MAX(scanned_at) AS last_at FROM s GROUP BY order_id)
SELECT a.type_id,
  SUM(IF(b.order_id IS NOT NULL, GREATEST(a.volume_remain - b.volume_remain, 0), 0)) AS partial_units,
  -- vanished = gone at t1 AND never seen again in any later snapshot (a pagination glitch
  -- makes an order skip one snapshot and come back — that is not a sale) AND not expired
  SUM(IF(b.order_id IS NULL AND ls.last_at = a.scanned_at
         AND TIMESTAMP_ADD(a.issued, INTERVAL a.duration DAY) > g.t1, a.volume_remain, 0)) AS disappeared_units
FROM good g
JOIN s a ON a.scanned_at = g.t0
JOIN last_seen ls ON ls.order_id = a.order_id
LEFT JOIN s b ON b.scanned_at = g.t1 AND b.order_id = a.order_id
GROUP BY a.type_id
"""

COVERAGE_SQL = f"""
WITH scans AS (
  SELECT DISTINCT scanned_at FROM `{config.TABLE_CJ_ORDERS_RAW}`
  WHERE scan_date >= DATE_SUB(DATE(@run_at), INTERVAL @window_days DAY)
    AND scanned_at <= @run_at AND structure_id = @sid
),
pairs AS (SELECT scanned_at AS t0, LEAD(scanned_at) OVER (ORDER BY scanned_at) AS t1 FROM scans)
SELECT
  COALESCE(SUM(IF(t1 IS NOT NULL AND TIMESTAMP_DIFF(t1, t0, MINUTE) <= @max_gap_h * 60,
                  TIMESTAMP_DIFF(t1, t0, SECOND), 0)), 0) / 3600.0 AS covered_hours,
  COUNT(*) AS snapshots
FROM pairs
"""

JITA_BOOK_SQL = f"""
SELECT type_id, is_buy_order, price, volume_remain, system_id
FROM `{config.TABLE_MARKET_ORDERS_RAW}`
WHERE @reused IS NOT NULL AND scan_date = DATE(@reused) AND scanned_at = @reused
  AND type_id IN UNNEST(@ids) AND system_id = @jita_sys
UNION ALL
SELECT type_id, is_buy_order, price, volume_remain, system_id
FROM `{config.TABLE_CJ_JITA_ORDERS_RAW}`
WHERE scan_date = DATE(@run_at) AND scanned_at = @run_at
  AND type_id IN UNNEST(@ids) AND system_id = @jita_sys
"""

PERIMETER_BOOK_SQL = f"""
WITH latest AS (
  SELECT MAX(scanned_at) AS s FROM `{config.TABLE_PERIMETER_ORDERS_RAW}`
  WHERE scan_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
)
SELECT p.type_id, p.is_buy_order, p.price, p.volume_remain, l.s AS scanned_at
FROM `{config.TABLE_PERIMETER_ORDERS_RAW}` p, latest l
WHERE p.scan_date = DATE(l.s) AND p.scanned_at = l.s AND p.type_id IN UNNEST(@ids)
"""

DELVE_SQL = f"""
SELECT type_id, avg_daily_volume, avg_price, days_with_trades
FROM `{config.TABLE_CJ_DELVE_HISTORY}`
WHERE fetched_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY) AND type_id IN UNNEST(@ids)
  AND region_id = @rid
QUALIFY ROW_NUMBER() OVER (PARTITION BY type_id ORDER BY fetched_date DESC) = 1
"""

MY_CJ_STOCK_SQL = f"""
SELECT type_id, SUM(volume_remain) AS my_cj_stock FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY scanned_at DESC) AS rn
  FROM `{config.TABLE_MY_ORDERS}`
  WHERE scan_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 95 DAY)
)
WHERE rn = 1 AND (is_open IS NULL OR is_open) AND NOT is_buy_order AND location_id = @sid
GROUP BY type_id
"""


# --------------------------------------------------------------------------- pure helpers

def walk_book(sells: list[tuple[float, int]], qty: int) -> tuple[float | None, int]:
    """Volume-weighted avg price of buying `qty` units from a sell book.
    `sells` = [(price, volume)], any order. Returns (avg_price, units_available<=qty)."""
    if qty <= 0 or not sells:
        return None, 0
    remaining, cost = qty, 0.0
    for price, vol in sorted(sells):
        take = min(remaining, int(vol))
        cost += take * price
        remaining -= take
        if remaining == 0:
            break
    got = qty - remaining
    return (cost / got if got else None), got


def instant_fill(buys: list[tuple[float, int, int]], sells: list[tuple[float, int]], ship: float):
    """Sell Jita-bought units straight into C-J buy orders, best price first.
    `buys` = [(price, volume_remain, min_volume)]. An order is taken whole only if the
    MARGINAL Jita units it needs (book walk) still clear CJ_MIN_MARGIN_PCT — so the walk
    stops when either side of the spread gets too thin. Sales tax only (no broker fee:
    selling into a buy order). Returns dict or None."""
    tax = config.CJ_SALES_TAX_RATE
    jita_units = sum(int(v) for _, v in sells)
    cum, cost_cum, revenue, worst = 0, 0.0, 0.0, None
    for price, vol, min_vol in sorted(buys, key=lambda x: -x[0]):
        take = min(int(vol), jita_units - cum)
        if take <= 0 or take < (min_vol or 1):
            break
        avg_new, got = walk_book(sells, cum + take)
        cost_new = avg_new * got
        marginal_cost = (cost_new - cost_cum) / take + ship
        if price * (1 - tax) < marginal_cost * (1 + config.CJ_MIN_MARGIN_PCT / 100):
            break
        cum, cost_cum, worst = cum + take, cost_new, price
        revenue += take * price * (1 - tax)
    if cum == 0:
        return None
    total_cost = cost_cum + ship * cum
    return {"units": cum, "jita_avg": cost_cum / cum, "cj_buy_worst": worst,
            "cj_buy_avg": revenue / (1 - tax) / cum, "profit_total": revenue - total_cost,
            "margin_pct": (revenue - total_cost) / total_cost * 100}


def estimate_demand(partial: float, disappeared: float, covered_hours: float,
                    delve_daily: float | None) -> tuple[float, str]:
    """Units/day sold in C-J and where the number came from."""
    if covered_hours >= config.CJ_MIN_COVERAGE_HOURS:
        units = (partial or 0) + config.CJ_DISAPPEARED_FILL_SHARE * (disappeared or 0)
        return units / covered_hours * 24.0, "C-J snapshoty"
    if delve_daily and delve_daily > 0:
        return delve_daily * config.CJ_REGION_SHARE, f"{config.CJ_REGION_NAME} × {config.CJ_REGION_SHARE:g}"
    return 0.0, "žádná data"


def sell_net_factor() -> float:
    broker = config.CJ_SELL_BROKER_FEE_RATE if config.CJ_SELL_BROKER_FEE_RATE is not None else 0.02
    return 1.0 - broker - config.CJ_SALES_TAX_RATE


@dataclass
class CjResult:
    run: dict
    recommendations: pd.DataFrame
    instant: pd.DataFrame           # C-J buy orders above landed Jita cost
    gaps: pd.DataFrame              # C-J has no sell orders, region history shows demand
    rejected_counts: dict
    covered_hours: float
    snapshots: int
    perimeter_scan_at: object
    warnings: list = field(default_factory=list)
    snapshot_at: object = None


def build_recommendations(book: pd.DataFrame, jita: pd.DataFrame, perim: pd.DataFrame,
                          fills: pd.DataFrame, delve: pd.DataFrame, my_stock: pd.DataFrame,
                          covered_hours: float, cj_buys: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    """Pure: all inputs are DataFrames already loaded. Returns (recs, instant, gaps, rejected)."""
    ship_rate = config.CJ_SHIPPING_ISK_PER_M3 or 0.0
    net_f = sell_net_factor()
    tick = config.REPRICE_TICK

    sells_by_type: dict[int, list] = {}
    buy_max: dict[int, float] = {}
    for src in (jita, perim if config.CJ_INCLUDE_PERIMETER_SELLS else perim.iloc[0:0]):
        if src.empty:
            continue
        for r in src.itertuples(index=False):
            if r.is_buy_order:
                buy_max[r.type_id] = max(buy_max.get(r.type_id, 0.0), r.price)
            else:
                sells_by_type.setdefault(r.type_id, []).append((r.price, r.volume_remain))
    if not config.CJ_INCLUDE_PERIMETER_SELLS and not perim.empty:  # still use Perimeter buys for B
        for r in perim[perim.is_buy_order].itertuples(index=False):
            buy_max[r.type_id] = max(buy_max.get(r.type_id, 0.0), r.price)

    fills_i = fills.set_index("type_id") if not fills.empty else pd.DataFrame()
    delve_i = delve.set_index("type_id") if not delve.empty else pd.DataFrame()
    stock_i = my_stock.set_index("type_id")["my_cj_stock"].to_dict() if not my_stock.empty else {}

    buys_by_type: dict[int, list] = {}
    if cj_buys is not None and not cj_buys.empty:
        for r in cj_buys.itertuples(index=False):
            buys_by_type.setdefault(int(r.type_id), []).append(
                (r.price, int(r.volume_remain), int(r.min_volume or 1)))

    rows, instant, gaps = [], [], []
    rejected = {"bez Jita nabídky": 0, "nízká marže": 0, "podezřelá marže": 0,
                "trh přesycený": 0, "žádná poptávka": 0, "zásoba stačí": 0}

    for b in book.itertuples(index=False):
        t = int(b.type_id)
        pv = float(b.packaged_volume or 0)
        ship = pv * ship_rate
        sells = sells_by_type.get(t, [])
        partial = fills_i.loc[t, "partial_units"] if t in fills_i.index else 0
        disappeared = fills_i.loc[t, "disappeared_units"] if t in fills_i.index else 0
        delve_daily = float(delve_i.loc[t, "avg_daily_volume"]) if t in delve_i.index else None
        demand, source = estimate_demand(partial, disappeared, covered_hours, delve_daily)
        mine = int(stock_i.get(t, 0))

        # C-J buy orders that already pay more than landed cost -> sell straight into them
        if t in buys_by_type and sells:
            r = instant_fill(buys_by_type[t], sells, ship)
            if r:
                instant.append({"type_id": t, "name": b.name, "cj_buy_max": b.cj_buy_max,
                                "ship_unit": ship, "m3": pv * r["units"], **r})

        if pd.isna(b.cj_sell_min):
            if delve_daily and sells:
                avg, _ = walk_book(sells, 1)
                landed = avg + ship
                gaps.append({"type_id": t, "name": b.name, "delve_daily": delve_daily,
                             "jita_sell_min": avg, "ship_unit": ship,
                             "min_cj_price": landed * (1 + config.CJ_MIN_MARGIN_PCT / 100) / net_f,
                             "cj_buy_max": b.cj_buy_max})
            continue

        if not sells:
            rejected["bez Jita nabídky"] += 1
            continue
        if demand <= 0:
            rejected["žádná poptávka"] += 1
            continue

        target = math.ceil(demand * config.CJ_STOCK_DAYS) - mine
        if target <= 0:
            rejected["zásoba stačí"] += 1
            continue
        competitor_days = max(0.0, (b.cj_sell_volume or 0) - mine) / demand
        if competitor_days > config.CJ_MAX_COMPETITOR_DAYS:
            rejected["trh přesycený"] += 1
            continue

        cost_a, avail = walk_book(sells, target)
        sell_net = b.cj_sell_min * net_f
        profit_a = sell_net - cost_a - ship
        margin_a = profit_a / (cost_a + ship) * 100
        if margin_a > config.CJ_MARGIN_CEILING_PCT:
            rejected["podezřelá marže"] += 1
            continue
        if margin_a < config.CJ_MIN_MARGIN_PCT:
            rejected["nízká marže"] += 1
            continue

        bm = buy_max.get(t)
        cost_b = (bm + tick) * (1 + config.PERIMETER_SCC_NEW_RATE) if bm else None
        profit_b = sell_net - cost_b - ship if cost_b else None

        rows.append({
            "type_id": t, "name": b.name, "m3_unit": pv,
            "jita_sell_min": min(p for p, _ in sells), "cost_a": cost_a, "cost_b": cost_b,
            "ship_unit": ship, "cj_sell_min": b.cj_sell_min, "cj_buy_max": b.cj_buy_max,
            "spread_pct": (b.cj_sell_min / min(p for p, _ in sells) - 1) * 100,
            "profit_a": profit_a, "margin_a": margin_a, "profit_b": profit_b,
            "margin_b": profit_b / (cost_b + ship) * 100 if profit_b is not None else None,
            "demand_day": demand, "demand_src": source,
            "fills_partial": float(partial or 0), "fills_vanished": float(disappeared or 0),
            "delve_day": delve_daily, "cj_sell_volume": int(b.cj_sell_volume or 0),
            "cj_sell_orders": int(b.cj_sell_orders or 0), "competitor_days": competitor_days,
            "my_cj_stock": mine, "target_qty": target, "jita_avail": avail,
            "exp_profit_day": profit_a * demand,
            "_sells": sells,
        })

    recs = pd.DataFrame(rows)
    if not recs.empty:
        recs = recs.sort_values("exp_profit_day", ascending=False)
        recs = _apply_budget(recs)
        unaffordable = int((recs.qty == 0).sum())
        if unaffordable:
            rejected["1 ks nad strop pozice/rozpočet"] = unaffordable
        recs = recs[recs.qty > 0].head(config.CJ_TOP_N).reset_index(drop=True)
    instant_df = pd.DataFrame(instant)
    if not instant_df.empty:
        instant_df = instant_df.sort_values("profit_total", ascending=False).head(10)
    gaps_df = pd.DataFrame(gaps)
    if not gaps_df.empty:
        gaps_df["_score"] = gaps_df.delve_daily * gaps_df.jita_sell_min
        gaps_df = gaps_df.sort_values("_score", ascending=False).head(10).drop(columns="_score")
    return recs, instant_df, gaps_df, rejected


def _apply_budget(recs: pd.DataFrame) -> pd.DataFrame:
    """Walk ranked rows; cap qty by Jita availability, per-position %, remaining budget, cargo m3.
    Re-walks the Jita book for the final qty so cost reflects what is actually bought."""
    budget_left = float(config.CJ_BUDGET_ISK)
    per_pos = config.CJ_BUDGET_ISK * config.CJ_PER_POSITION_PCT
    m3_left = config.CJ_MAX_CARGO_M3 if config.CJ_MAX_CARGO_M3 else float("inf")
    out = []
    for r in recs.to_dict("records"):
        qty = min(r["target_qty"], r["jita_avail"])
        cap_isk = min(per_pos, budget_left)
        qty = min(qty, int(cap_isk // r["cost_a"]) if r["cost_a"] > 0 else qty)
        if r["m3_unit"] > 0 and math.isfinite(m3_left):
            qty = min(qty, int(m3_left // r["m3_unit"]))
        if qty <= 0:
            r.update(qty=0, cost_a_final=None, isk=0.0, m3=0.0, profit_total=0.0,
                     note="mimo rozpočet/cargo")
        else:
            avg, got = walk_book(r["_sells"], qty)
            r.update(qty=got, cost_a_final=avg, isk=avg * got, m3=r["m3_unit"] * got,
                     profit_total=(r["cj_sell_min"] * sell_net_factor() - avg - r["ship_unit"]) * got,
                     note="" if got >= r["target_qty"] else "zkráceno")
            budget_left -= avg * got
            m3_left -= r["m3_unit"] * got
        r.pop("_sells", None)
        out.append(r)
    return pd.DataFrame(out)


# --------------------------------------------------------------------------- BigQuery

def _q(client, sql, params):
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    return client.query(sql, job_config=job_config).to_dataframe()


def load_latest_run(client) -> dict | None:
    df = _q(client, LATEST_RUN_SQL, [bigquery.ScalarQueryParameter("sid", "INT64", config.CJ_STRUCTURE_ID)])
    return None if df.empty else df.iloc[0].to_dict()


def analyze(client, run: dict) -> CjResult:
    sid = bigquery.ScalarQueryParameter("sid", "INT64", config.CJ_STRUCTURE_ID)
    run_at = bigquery.ScalarQueryParameter("run_at", "TIMESTAMP", run["run_at"])   # Jita books
    snap_at = _q(client, LATEST_SNAPSHOT_SQL, [sid]).snap.iloc[0]
    if snap_at is None or pd.isna(snap_at):
        snap_at = run["run_at"]
    # C-J side queries take @run_at = latest C-J snapshot (same SQL param name)
    cj_at = bigquery.ScalarQueryParameter("run_at", "TIMESTAMP", snap_at)
    cats = bigquery.ArrayQueryParameter("cats", "INT64", config.CJ_CATEGORY_IDS)
    win = bigquery.ScalarQueryParameter("window_days", "INT64", config.CJ_FILL_WINDOW_DAYS)
    gap = bigquery.ScalarQueryParameter("max_gap_h", "INT64", config.CJ_MAX_PAIR_GAP_HOURS)

    book = _q(client, CJ_BOOK_SQL, [sid, cj_at, cats])
    ids = bigquery.ArrayQueryParameter("ids", "INT64", [int(x) for x in book.type_id]) if not book.empty \
        else bigquery.ArrayQueryParameter("ids", "INT64", [])
    reused = run.get("jita_reused_scan_at")
    reused = None if reused is None or pd.isna(reused) else reused
    jita = _q(client, JITA_BOOK_SQL, [ids, run_at,
                                      bigquery.ScalarQueryParameter("reused", "TIMESTAMP", reused),
                                      bigquery.ScalarQueryParameter("jita_sys", "INT64", config.JITA_SYSTEM_ID)])
    perim = _q(client, PERIMETER_BOOK_SQL, [ids])
    fills = _q(client, FILLS_SQL, [sid, cj_at, cats, win, gap])
    cov = _q(client, COVERAGE_SQL, [sid, cj_at, win, gap]).iloc[0]
    rid = run.get("region_id")
    rid = None if rid is None or pd.isna(rid) else int(rid)
    delve = _q(client, DELVE_SQL, [ids, bigquery.ScalarQueryParameter("rid", "INT64", rid)])
    mine = _q(client, MY_CJ_STOCK_SQL, [sid])
    cj_buys = _q(client, CJ_BUYS_SQL, [sid, cj_at, cats])

    warnings = []
    if rid is None:
        warnings.append("Poslední plný běh je ze staré verze jobu (historie z Delve, ne z regionu C-J) — "
                        "sloupec region/den je prázdný. Spusť run_cj.py bez --skip-job po redeployi.")
    if config.CJ_SHIPPING_ISK_PER_M3 is None:
        warnings.append("CJ_SHIPPING_ISK_PER_M3 není nastaveno — doprava počítána jako 0 ISK. Zisky jsou NADHODNOCENÉ.")
    if config.CJ_SELL_BROKER_FEE_RATE is None:
        warnings.append("CJ_SELL_BROKER_FEE_RATE není nastaveno — použit odhad 2,0 %. Ověř poplatek v Keepstaru.")
    perim_at = perim.scanned_at.iloc[0] if not perim.empty else None
    if perim.empty:
        warnings.append("Perimeter data nejsou z posledních 2 dní — varianta B a nákup v Perimeteru jen z Jity.")
    jita_age_h = (pd.Timestamp(snap_at) - pd.Timestamp(run["run_at"])).total_seconds() / 3600
    if jita_age_h > config.FRESHNESS_THRESHOLD_HOURS:
        warnings.append(f"Ceny z Jity jsou z plného běhu {jita_age_h:.0f} h starého než C-J snapshot — "
                        "pusť run_cj.py bez --skip-job.")
    covered = float(cov.covered_hours or 0)
    if covered < config.CJ_MIN_COVERAGE_HOURS:
        warnings.append(f"Jen {covered:.1f} h pokrytí C-J snapshoty (min {config.CJ_MIN_COVERAGE_HOURS} h) — "
                        f"poptávka je odhad z {config.CJ_REGION_NAME} × {config.CJ_REGION_SHARE:g}, ne skutečné prodeje v C-J.")

    perim_cols = perim.drop(columns=["scanned_at"]) if not perim.empty else perim
    recs, instant, gaps, rejected = build_recommendations(book, jita, perim_cols, fills, delve, mine, covered, cj_buys)
    return CjResult(run=run, recommendations=recs, instant=instant, gaps=gaps, rejected_counts=rejected,
                    covered_hours=covered, snapshots=int(cov.snapshots), perimeter_scan_at=perim_at, snapshot_at=snap_at,
                    warnings=warnings)
