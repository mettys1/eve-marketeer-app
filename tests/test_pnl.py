"""Synthetic-data tests for eval/pnl.py — run: python -m pytest tests/test_pnl.py -q"""
from datetime import datetime, timezone
import pandas as pd
import pytest
from eval import pnl

T = lambda s: pd.Timestamp(s, tz="UTC")
NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)

def tx(rows):
    return pd.DataFrame(rows, columns=["transaction_id","date","type_id","item_name","quantity","unit_price","is_buy","location_id"])

def fees(rows):
    return pd.DataFrame(rows, columns=["journal_id","date","ref_type","fee","context_id","context_id_type"])

import config
PER, JITA = config.PERIMETER_STRUCTURE_ID, config.JITA_STATION_ID

def orders(rows):
    """rows: (order_id, issued, type_id, name, is_buy, value[, location, price, volume_total, volume_remain])"""
    full = []
    for r in rows:
        if len(r) == 6:  # legacy shape: value -> price*1
            r = (*r[:5], 0, JITA, r[5], 1, 1)
        full.append(r[:5] + r[6:])
    return pd.DataFrame(full, columns=["order_id","issued","type_id","item_name","is_buy_order",
                                       "location_id","price","volume_total","volume_remain"])

def line(res, prefix):
    return res.company.loc[res.company["line"].str.startswith(prefix), "isk"].iloc[0]

def test_fifo_and_fees_end_to_end():
    t = tx([
        (1, T("2026-09-20 10:00"), 34, "Trit", 10, 100.0, True, 1),
        (2, T("2026-09-21 10:00"), 34, "Trit", 10, 120.0, True, 1),
        (3, T("2026-10-02 10:00"), 34, "Trit", 15, 200.0, False, 2),   # in window: 10@100 + 5@120
    ])
    f = fees([
        (100, T("2026-09-20 09:00:00"), "brokers_fee", 40.0, None, None),     # buy order A
        (101, T("2026-09-21 09:00:01"), "market_provider_tax", 20.0, None, None),  # buy order B (+1s)
        (102, T("2026-10-01 08:00:00"), "brokers_fee", 30.0, None, None),     # sell order S
        (103, T("2026-10-02 10:00"), "transaction_tax", 90.0, 3, "market_transaction_id"),
        (104, T("2026-10-03 08:00"), "brokers_fee", 7.0, None, None),         # unmatched
    ])
    o = orders([
        (1, T("2026-09-20 09:00:00"), 34, "Trit", True, 1000.0),
        (2, T("2026-09-21 09:00:00"), 34, "Trit", True, 1200.0),
        (3, T("2026-10-01 08:00:00"), 34, "Trit", False, 3000.0),
    ])
    r = pnl.compute(t, f, o, window_days=7, tol_sec=5, sales_tax_rate=0.03375, now=NOW)
    it = r.items.iloc[0]
    assert it.units_sold == 15 and it.revenue == 3000
    assert it.cogs == 10*100 + 5*120
    assert it.buy_fees == pytest.approx(60 / 20 * 15)   # 3 ISK/unit capitalized
    assert it.sales_tax == 90 and it.sell_fees == 30
    assert it.net_profit == pytest.approx(3000 - 1600 - 45 - 90 - 30)
    assert line(r, "− Nepřiřazené") == -7
    net = line(r, "= ČISTÝ")
    assert net == pytest.approx(r.items.net_profit.sum() - 7)
    assert r.coverage["sales_tax_linked_pct"] == 100

def test_unknown_opening_stock_is_consumed_first_and_excluded():
    t = tx([
        (1, T("2026-10-01 10:00"), 7, "X", 5, 50.0, False, 1),   # sold before any buy -> unknown
        (2, T("2026-10-02 10:00"), 7, "X", 5, 10.0, True, 1),
        (3, T("2026-10-03 10:00"), 7, "X", 5, 50.0, False, 1),
    ])
    r = pnl.compute(t, fees([]), orders([]), 7, 5, 0.0, now=NOW)
    it = r.items.iloc[0]
    assert it.unknown_cost_units == 5 and it.unknown_cost_revenue == 250
    assert it.units_sold == 5 and it.cogs == 50 and it.net_profit == 200

def test_unknown_lot_is_oldest_stock():
    # net position bottoms at -10 -> 10 unknown opening units, consumed before the bought lot
    t = tx([
        (1, T("2026-10-01"), 7, "X", 5, 10.0, True, 1),
        (2, T("2026-10-02"), 7, "X", 5, 50.0, False, 1),
        (3, T("2026-10-03"), 7, "X", 10, 50.0, False, 1),
    ])
    s = pnl.fifo_sells(t)
    assert list(s.unknown_qty) == [5, 5] and list(s.known_qty) == [0, 5]

def test_fee_split_between_orders_in_same_second():
    f = fees([(1, T("2026-10-01 08:00"), "brokers_fee", 100.0, None, None)])
    o = orders([(1, T("2026-10-01 08:00"), 1, "a", True, 300.0),
                (2, T("2026-10-01 08:00"), 2, "b", True, 100.0)])
    a = pnl.attribute_order_fees(f, o, 2)
    assert sorted(a.fee) == [25.0, 75.0]

def test_empty_inputs():
    r = pnl.compute(tx([]), fees([]), orders([]), 7, 5, 0.03, now=NOW)
    assert r.items.empty and line(r, "= ČISTÝ") == 0


def test_expected_amount_disambiguates_same_second_orders():
    # two new Perimeter buy orders placed in the same second; SCC amount identifies which
    o = orders([(1, T("2026-10-01 08:00"), 1, "a", True, 0, PER, 1000.0, 10, 10),
                (2, T("2026-10-01 08:00"), 2, "b", True, 0, PER, 50.0, 10, 10)])
    rate = config.PERIMETER_SCC_NEW_RATE
    f = fees([(1, T("2026-10-01 08:00"), "market_provider_tax", 10000 * rate, None, None),
              (2, T("2026-10-01 08:00"), "market_provider_tax", 500 * rate, None, None)])
    a = pnl.attribute_order_fees(f, o, 2)
    assert dict(zip(a.journal_id, a.type_id)) == {1: 1, 2: 2}
    assert (a.method == "order").all()

def test_perimeter_reprice_expected_fee():
    o = orders([(1, T("2026-10-01 08:00"), 1, "a", True, 0, PER, 100.0, 10, 10),
                (1, T("2026-10-02 08:00"), 1, "a", True, 0, PER, 110.0, 10, 8)])
    e = pnl.expected_order_fees(o).iloc[1]
    assert e.exp_brokers_fee == 100
    assert e.exp_market_provider_tax == pytest.approx(0.001 * 110 * 8 + 0.005 * 10 * 8)

def test_unseen_order_fee_allocated_to_following_fills():
    t = tx([(1, T("2026-10-01 09:00"), 5, "A", 10, 100.0, True, PER),   # 1000
            (2, T("2026-10-01 10:00"), 6, "B", 30, 100.0, True, PER),   # 3000
            (3, T("2026-10-01 11:00"), 7, "C", 1, 9999.0, True, JITA)]) # wrong location
    f = fees([(1, T("2026-10-01 08:00"), "market_provider_tax", 40.0, None, None)])
    a = pnl.attribute_order_fees(f, orders([]), 5, t)
    assert (a.method == "fill").all()
    assert dict(zip(a.type_id, a.fee)) == {5: 10.0, 6: 30.0}
    assert a.is_buy_order.all()

def test_fee_with_no_order_and_no_fill_stays_unallocated():
    f = fees([(1, T("2026-10-01 08:00"), "brokers_fee", 5000.0, None, None)])
    a = pnl.attribute_order_fees(f, orders([]), 5, tx([]))
    assert list(a.method) == ["none"]
