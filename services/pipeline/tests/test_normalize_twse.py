from datetime import date
from decimal import Decimal

import pytest

from radar.normalizers.common import NormalizeError
from radar.normalizers.twse import normalize_institutional, normalize_market_day

from .conftest import load_fixture, payload

D = date(2026, 9, 23)


def test_market_day_quotes():
    snap, warnings = normalize_market_day(payload("MI_INDEX", D, load_fixture("twse_mi_index_20260923.json")))
    assert warnings == []
    q = {x.symbol: x for x in snap.quotes}
    assert len(q) == 5

    tsmc = q["2330"]
    assert tsmc.name == "台積電" and tsmc.security_type == "stock"
    assert tsmc.close == Decimal("1010.00") and tsmc.change == Decimal("15.00")
    assert tsmc.volume == 45_210_000 and tsmc.turnover == 45_678_901_234

    assert q["2317"].change == Decimal("-2.50")
    assert q["2881"].is_no_compare and q["2881"].change is None      # X：不比價
    assert q["9999"].close is None and q["9999"].volume == 0          # 無成交
    assert q["0050"].security_type == "etf"


def test_market_day_index_and_breadth():
    snap, _ = normalize_market_day(payload("MI_INDEX", D, load_fixture("twse_mi_index_20260923.json")))
    idx = {i.index_code: i for i in snap.indices}
    assert idx["發行量加權股價指數"].close == Decimal("23456.78")
    assert idx["發行量加權股價指數"].change == Decimal("120.34")
    assert idx["臺灣50指數"].change == Decimal("-10.00")
    assert idx["臺灣50指數"].change_pct == Decimal("-0.05")

    b = snap.breadth
    assert (b.advancers, b.limit_up, b.decliners, b.limit_down) == (612, 20, 301, 3)
    assert (b.unchanged, b.no_trade) == (78, 5)
    assert b.total_turnover == 480_123_456_789 and b.total_volume == 8_765_432_100


def test_institutional():
    day, _ = normalize_institutional(payload("T86", D, load_fixture("twse_t86_20260923.json")))
    f = {x.symbol: x for x in day.flows}
    t = f["2330"]
    assert (t.foreign_buy, t.foreign_sell, t.foreign_net) == (10_000_000, 6_000_000, 4_000_000)
    assert (t.trust_net, t.dealer_buy, t.dealer_sell, t.dealer_net) == (300_000, 300_000, 400_000, -100_000)
    assert t.total_net == 4_200_000
    assert f["2317"].dealer_net == 50_000 and f["2317"].total_net == -2_050_000


def test_date_mismatch_is_rejected():
    body = load_fixture("twse_mi_index_20260923.json")
    with pytest.raises(NormalizeError, match="不符"):
        normalize_market_day(payload("MI_INDEX", date(2026, 9, 24), body))


def test_missing_table_error_lists_titles():
    body = load_fixture("twse_mi_index_20260923.json")
    body["tables"] = [t for t in body["tables"] if "每日收盤行情" not in t["title"]]
    with pytest.raises(NormalizeError, match="實際標題"):
        normalize_market_day(payload("MI_INDEX", D, body))
