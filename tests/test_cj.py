"""Unit tests for the pure parts of cj/analysis.py (no BigQuery)."""

import math

import pandas as pd
import pytest

import config
from cj import analysis
from cj.analysis import build_recommendations, estimate_demand, walk_book


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    monkeypatch.setattr(config, "CJ_SHIPPING_ISK_PER_M3", 1000.0)
    monkeypatch.setattr(config, "CJ_SELL_BROKER_FEE_RATE", 0.01)
    monkeypatch.setattr(config, "CJ_SALES_TAX_RATE", 0.03375)
    monkeypatch.setattr(config, "CJ_BUDGET_ISK", 1_000_000_000)
    monkeypatch.setattr(config, "CJ_PER_POSITION_PCT", 0.2)
    monkeypatch.setattr(config, "CJ_MAX_CARGO_M3", None)
    monkeypatch.setattr(config, "CJ_STOCK_DAYS", 7)
    monkeypatch.setattr(config, "CJ_MIN_MARGIN_PCT", 15.0)
    monkeypatch.setattr(config, "CJ_INCLUDE_PERIMETER_SELLS", True)


def test_sell_net_matches_ingame_window(monkeypatch):
    # 1st Taj Mahgoon sell window 2026-10-11: 52,230,000 -> 48,991,740 net
    monkeypatch.setattr(config, "CJ_SELL_BROKER_FEE_RATE", 0.005 + 0.015)
    monkeypatch.setattr(config, "CJ_SALES_TAX_RATE", 0.042)
    assert 52_230_000 * analysis.sell_net_factor() == pytest.approx(48_991_740, abs=0.01)


def test_walk_book_weighted_average():
    avg, got = walk_book([(110, 5), (100, 10)], 12)
    assert got == 12
    assert avg == pytest.approx((10 * 100 + 2 * 110) / 12)


def test_walk_book_short_book():
    avg, got = walk_book([(100, 3)], 10)
    assert got == 3 and avg == 100


def test_walk_book_empty():
    assert walk_book([], 5) == (None, 0)


def test_demand_snapshots_vs_fallback():
    d, src = estimate_demand(partial=10, disappeared=10, covered_hours=24, delve_daily=100)
    assert d == pytest.approx(10 + 0.7 * 10) and src == "C-J snapshoty"
    d, src = estimate_demand(partial=0, disappeared=0, covered_hours=2, delve_daily=100)
    assert d == pytest.approx(100 * config.CJ_DELVE_SHARE) and src.startswith("Delve")
    d, src = estimate_demand(0, 0, 2, None)
    assert d == 0 and src == "žádná data"


def _frames():
    book = pd.DataFrame([
        # good: cheap in Jita, expensive in C-J
        dict(type_id=1, name="Good Module", packaged_volume=5.0, cj_sell_min=2_000_000.0,
             cj_sell_volume=20, cj_sell_orders=3, cj_buy_max=None, cj_buy_volume=0),
        # thin margin
        dict(type_id=2, name="Thin Module", packaged_volume=5.0, cj_sell_min=1_050_000.0,
             cj_sell_volume=20, cj_sell_orders=3, cj_buy_max=None, cj_buy_volume=0),
        # saturated: 1000 units listed vs 2/day demand
        dict(type_id=3, name="Flooded Module", packaged_volume=5.0, cj_sell_min=3_000_000.0,
             cj_sell_volume=1000, cj_sell_orders=30, cj_buy_max=None, cj_buy_volume=0),
        # no C-J sells but a juicy C-J buy order + Delve demand
        dict(type_id=4, name="Gap Module", packaged_volume=5.0, cj_sell_min=None,
             cj_sell_volume=0, cj_sell_orders=0, cj_buy_max=3_000_000.0, cj_buy_volume=4),
    ])
    jita = pd.DataFrame([
        dict(type_id=1, is_buy_order=False, price=1_000_000.0, volume_remain=100, system_id=30000142),
        dict(type_id=1, is_buy_order=True, price=900_000.0, volume_remain=100, system_id=30000142),
        dict(type_id=2, is_buy_order=False, price=1_000_000.0, volume_remain=100, system_id=30000142),
        dict(type_id=3, is_buy_order=False, price=1_000_000.0, volume_remain=100, system_id=30000142),
        dict(type_id=4, is_buy_order=False, price=1_000_000.0, volume_remain=100, system_id=30000142),
    ])
    perim = pd.DataFrame([dict(type_id=1, is_buy_order=True, price=950_000.0, volume_remain=10)])
    fills = pd.DataFrame([
        dict(type_id=1, partial_units=48, disappeared_units=0),
        dict(type_id=2, partial_units=48, disappeared_units=0),
        dict(type_id=3, partial_units=4, disappeared_units=0),
    ])
    delve = pd.DataFrame([dict(type_id=4, avg_daily_volume=10.0, avg_price=2e6, days_with_trades=14)])
    mine = pd.DataFrame([dict(type_id=1, my_cj_stock=6)])
    return book, jita, perim, fills, delve, mine


def test_build_recommendations():
    recs, instant, gaps, rejected = build_recommendations(*_frames(), covered_hours=48)
    assert list(recs.name) == ["Good Module"]
    r = recs.iloc[0]
    # demand 48 units / 48 h = 24/day -> 7 days = 168 - my 6 = 162, but Jita has 100
    assert r.demand_day == pytest.approx(24)
    assert r.target_qty == 162
    # per-position cap 200M / 1M = 200 -> limited by Jita availability 100
    assert r.qty == 100 and r.note == "zkráceno"
    assert r.ship_unit == pytest.approx(5000)
    net = 2_000_000 * (1 - 0.01 - 0.03375)
    assert r.profit_a == pytest.approx(net - 1_000_000 - 5000)
    # B uses max(Jita 900k, Perimeter 950k) + tick + 0.5% SCC
    assert r.cost_b == pytest.approx((950_000 + 0.01) * 1.005)
    assert rejected["nízká marže"] == 1
    assert rejected["trh přesycený"] == 1
    assert list(gaps.name) == ["Gap Module"]
    assert list(instant.name) == ["Gap Module"]
    assert instant.iloc[0].units == 4


def test_budget_cap(monkeypatch):
    monkeypatch.setattr(config, "CJ_BUDGET_ISK", 100_000_000)  # per position 20M -> 20 units
    recs, *_ = build_recommendations(*_frames(), covered_hours=48)
    assert recs.iloc[0].qty == 20
    assert recs.iloc[0].isk == pytest.approx(20_000_000)


def test_cargo_cap(monkeypatch):
    monkeypatch.setattr(config, "CJ_MAX_CARGO_M3", 50)  # 5 m3/unit -> 10 units
    recs, *_ = build_recommendations(*_frames(), covered_hours=48)
    assert recs.iloc[0].qty == 10


def test_render(tmp_path, monkeypatch):
    from cj import dashboard
    monkeypatch.setattr(config, "DASHBOARD_OUTPUT_DIR", tmp_path)
    recs, instant, gaps, rejected = build_recommendations(*_frames(), covered_hours=48)
    res = analysis.CjResult(run={"run_at": "2026-10-11T12:00:00Z", "jita_reused_scan_at": None},
                            recommendations=recs, instant=instant, gaps=gaps, rejected_counts=rejected,
                            covered_hours=48, snapshots=3, perimeter_scan_at=None, warnings=["test warn"])
    out = dashboard.render(res, open_browser=False)
    text = out.read_text(encoding="utf-8")
    assert "Good Module\t100" in text and "test warn" in text
