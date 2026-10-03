"""上櫃（櫃買中心 OpenAPI）：正規化、只有最新一天的處理、不動交易日曆、和上市分開驗證、網站與盤中即時。"""
import json
from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest

from radar import live, repository as repo
from radar.adapters.base import FetchError, RawPayload
from radar.adapters.tpex import URLS, TpexAdapter, roc_date
from radar.config import TAIPEI
from radar.jobs.ingest import ingest_date
from radar.normalizers import tpex as norm

from .conftest import FIXTURES
from .test_web import client, market  # noqa: F401

D = date(2026, 10, 2)


def fixture():
    return json.loads((FIXTURES / "tpex_openapi_day.json").read_text(encoding="utf-8"))


def payload(dataset, body, d=D):
    return RawPayload(source="TPEX", dataset=dataset, trade_date=d, url="fixture://tpex", fetched_at=datetime.now(TAIPEI),
                      body=body, has_data=True)


def test_dates():
    assert roc_date("1151002") == D and roc_date("20261002") == D


def test_market_day_keeps_stocks_and_etfs_only():
    f = fixture()
    snap, warn = norm.normalize_market_day(payload("MI_INDEX", {"quotes": f["quotes"], "index": f["index"], "highlight": f["highlight"]}))
    assert snap.market == "TPEX" and not warn
    q = {x.symbol: x for x in snap.quotes}
    assert set(q) == {"6488", "8299", "6171", "00679B", "5536"}                 # 權證不收
    assert q["8299"].change == Decimal("-15.00") and q["00679B"].security_type == "etf"
    assert q["6488"].volume == 16255922 and q["6488"].turnover == 18622066280
    assert q["5536"].close is None and q["5536"].volume == 0                    # 沒有成交
    idx = snap.indices[0]
    assert idx.index_code == "櫃買指數" and idx.close == Decimal("426.93") and idx.change_pct == pytest.approx(Decimal("1.9364"), abs=Decimal("0.0001"))
    b = snap.breadth
    assert (b.advancers, b.decliners, b.unchanged, b.limit_up, b.limit_down, b.no_trade) == (452, 331, 85, 31, 4, 24)
    assert b.total_turnover == 273_186_000_000 and b.total_volume == 1_238_583_000   # 市場現況：百萬元、千股


def test_institutional_uses_total_foreign_like_twse():
    day, _ = norm.normalize_institutional(payload("T86", {"data": fixture()["insti"]}))
    f = {x.symbol: x for x in day.flows}
    # 外資用「外資及陸資合計」（含外資自營商），欄位名稱多一個空白也找得到
    assert (f["8299"].foreign_buy, f["8299"].foreign_sell, f["8299"].foreign_net) == (110000, 400000, -290000)
    assert f["6488"].trust_net == 400000 and f["6488"].dealer_net == -100000 and f["6488"].total_net == 2300000


def test_ex_rights_and_company_profiles():
    f = fixture()
    period, _ = norm.normalize_ex_rights(payload("TWT49U", {"data": f["exright"]}), D)
    a = period.actions[0]
    assert (a.symbol, a.ex_date, a.action_type, a.prev_close, a.ref_price, a.value, a.div_ref) == \
        ("6171", D, "dividend", Decimal("27.35"), Decimal("25.35"), Decimal("2.000000"), Decimal("25.35"))
    assert not norm.normalize_ex_rights(payload("TWT49U", {"data": f["exright"]}, date(2026, 10, 3)), date(2026, 10, 5))[0].actions
    snap, _ = norm.normalize_company_profiles(payload("COMPANY", {"data": f["company"], "industry_names": {"24": "半導體業", "14": "建材營造"}}))
    c = {x.symbol: x for x in snap.companies}
    assert c["6488"].industry_name == "半導體業" and c["6488"].listed_date == date(2015, 9, 25)
    assert c["9999"].industry_name == "其他（91）" and c["9999"].listed_date == date(2026, 1, 1)


def _mock_adapter(settings, latest="1151002"):
    f = fixture()
    body = {URLS["quotes"]: f["quotes"], URLS["index"]: f["index"], URLS["highlight"]: f["highlight"],
            URLS["insti"]: f["insti"], URLS["exright"]: f["exright"], URLS["company"]: f["company"]}

    def handler(request):
        rows = body[str(request.url).split("?")[0]]
        rows = [dict(r, Date=latest) if "Date" in r and len(r["Date"]) == 7 else r for r in rows]
        return httpx.Response(200, json=rows)
    return TpexAdapter(settings, httpx.Client(transport=httpx.MockTransport(handler)))


def test_openapi_only_has_the_latest_day(settings):
    a = _mock_adapter(settings)
    assert a.latest_date() == D and a.drives_calendar is False
    assert a.fetch_market_day(D).has_data
    assert not a.fetch_market_day(date(2026, 10, 5)).has_data                  # 還沒更新：稍後再抓
    with pytest.raises(FetchError):
        a.fetch_market_day(date(2026, 9, 30))                                   # 拿不到歷史


def test_ingest_tpex_is_separate_from_twse(conn, settings):
    settings = settings.__class__(**{**settings.__dict__, "min_quote_rows": 1, "min_flow_rows": 1})
    a = _mock_adapter(settings)
    r = ingest_date(conn, a, settings, D, final=True)
    assert r.status == {"MI_INDEX": "success", "T86": "success"}
    assert conn.execute("SELECT trade_date, is_open FROM trading_calendar").fetchall() == [(D, True)]   # 有成交的日子補成交易日
    markets = dict(conn.execute("SELECT symbol, market FROM stocks").fetchall())
    assert markets["6488"] == "TPEX" and "7A0001" not in markets
    assert repo.prev_row_count(conn, "daily_prices", date(2026, 10, 5), "TPEX") == 5
    assert repo.prev_row_count(conn, "daily_prices", date(2026, 10, 5), "TWSE") is None
    b = conn.execute("SELECT advancers, decliners FROM market_breadth WHERE market = 'TPEX'").fetchone()
    assert b == (452, 331)
    # 還沒更新的日子：就算是最後一次嘗試，也不會把那天記成休市
    assert ingest_date(conn, a, settings, date(2026, 10, 5), final=True).status["MI_INDEX"] == "pending"   # 稍後再抓
    assert conn.execute("SELECT count(*) FROM trading_calendar WHERE NOT is_open").fetchone()[0] == 0


def test_otc_on_site_and_live_channels(conn, client, market, settings):  # noqa: F811
    settings = settings.__class__(**{**settings.__dict__, "min_quote_rows": 1, "min_flow_rows": 1})
    d = market[-1]                                                              # 上市測試資料的最後一天
    ingest_date(conn, _mock_adapter(settings, latest=f"{d.year - 1911}{d:%m%d}"), settings, d, final=True)
    stock = client.get("/stock/6488").text
    assert "環球晶" in stock and "上櫃股票" in stock and "上市 2015" not in stock
    assert "上櫃" in client.get("/search", params={"q": "群"}).text or client.get("/search", params={"q": "群"}).status_code == 303
    page = client.get("/market").text
    assert 'class="otc-line small"' in page and "上漲 452・下跌" in page        # 家數不上色，只有漲跌幅用紅綠
    assert live.MisProvider.channels(["2330", "6488"], {"2330": "TWSE", "6488": "TPEX"}) == \
        "tse_t00.tw|otc_o00.tw|tse_2330.tw|otc_6488.tw"
