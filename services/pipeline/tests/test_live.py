"""盤中即時：解析證交所 MIS 的回應、開盤時段、代號檢查、快取與失敗時的處理、可替換的資料來源、網頁與 API。"""
import os
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from radar import live
from radar.config import TAIPEI
from radar.web.app import create_app
from radar.web.export import export_day

from .conftest import load_fixture
from .test_web import client, market  # noqa: F401


def test_parse_mis_quote():
    quotes = {m["c"]: live.MisProvider.parse_quote(m) for m in load_fixture("mis_quote.json")["msgArray"]}
    idx = quotes["t00"]
    assert idx["price"] == 48475.74 and idx["prev_close"] == 48353.49 and idx["change"] == pytest.approx(122.25)
    assert idx["volume_lots"] is None and idx["date"] == "20261002" and idx["time"].startswith("2026-10-02T13:33:00")
    tsmc = quotes["2330"]
    assert tsmc["price"] == 2500 and tsmc["change"] == -10 and tsmc["change_pct"] == pytest.approx(-10 / 2510)
    assert tsmc["volume_lots"] == 13869 and tsmc["at_limit"] is None
    hon = quotes["2317"]                                  # 這 5 秒沒有成交（z 是 "-"）：用最近一筆成交價，而且是漲停
    assert hon["price"] == 279 and hon["at_limit"] == "up"
    stats = live.MisProvider.parse_stats({"tz": "898174350920", "tv": "10892014", "%": "13:33:00"})
    assert stats == {"turnover": 898174350920.0, "volume_lots": 10892014, "time": "13:33:00"}


def test_market_phase():
    at = lambda h, m: datetime(2026, 10, 5, h, m, tzinfo=TAIPEI)  # noqa: E731
    assert [live.market_phase(at(h, m), True) for h, m in ((8, 0), (8, 45), (9, 0), (13, 29), (13, 30), (20, 0))] == \
        ["pre", "auction", "open", "open", "after", "after"]
    assert live.market_phase(at(10, 0), False) == "closed"


def test_symbols_are_checked_before_going_upstream():
    assert live.clean_symbols("2330, 2317,2330,00631l,abc,2330.tw|tse_t00,9999999,") == ["2330", "2317", "00631L"]
    assert len(live.clean_symbols(",".join(str(1000 + i) for i in range(100)))) == live.MAX_SYMBOLS


class FakeProvider:
    key, name, usage, public_ok = "fake", "測試資料來源", "測試", False

    def __init__(self):
        self.calls, self.fail = 0, False

    def fetch(self, symbols, markets=None):
        self.calls += 1
        self.markets = markets
        if self.fail:
            raise RuntimeError("上游沒回應")
        quotes = [live.MisProvider.parse_quote(m) for m in load_fixture("mis_quote.json")["msgArray"]]
        return {"quotes": quotes, "market": {"turnover": 8.9e11, "volume_lots": 10892014, "time": "13:33:00"}}


def test_feed_caches_shares_and_survives_failures():
    now = [0.0]
    prov = FakeProvider()
    feed = live.LiveFeed(prov, clock=lambda: now[0])
    a = feed.snapshot(["2317", "2330"], "open")
    assert prov.calls == 1 and not a["cached"] and a["index"]["symbol"] == "t00" and a["otc"]["symbol"] == "o00"
    assert [q["symbol"] for q in a["stocks"]] == ["2317", "2330"]                # 照要求的順序
    b = feed.snapshot(["2330", "2317"], "open")                                  # 同一組股票（順序不同）共用快取
    assert prov.calls == 1 and b["cached"]
    now[0] += live.TTL_OPEN + 1
    prov.fail = True
    c = feed.snapshot(["2330", "2317"], "open")                                  # 上游失敗：給上一次的資料並標示
    assert c["stale"] and c["index"]["price"] == 48475.74 and "上一次" in c["error"]
    empty = live.LiveFeed(prov).snapshot(["2330"], "open")
    assert empty["stale"] and empty["index"] is None
    assert live.LiveFeed(None).snapshot([], "open")["available"] is False         # LIVE_PROVIDER=off


def test_provider_is_replaceable():
    assert live.make_provider("off") is None and isinstance(live.make_provider("mis"), live.MisProvider)
    assert live.make_provider("mis").public_ok is False                          # 證交所 MIS 只給自己看
    with pytest.raises(ValueError):
        live.make_provider("nope")


def test_live_api_and_pages(conn, client, market, tmp_path):  # noqa: F811
    prov = FakeProvider()
    app = TestClient(create_app(os.environ["TEST_DATABASE_URL"], live_feed=live.LiveFeed(prov)))
    d = app.get("/api/live", params={"symbols": "2330,2317,bad|x"}).json()
    assert d["phase"] in live.PHASES and d["phase_label"] == live.PHASES[d["phase"]]
    assert [q["symbol"] for q in d["stocks"]] == ["2330", "2317"] and d["index"]["price"] == 48475.74
    assert d["provider"] == {"key": "fake", "name": "測試資料來源", "usage": "測試", "public_ok": False}
    assert d["market"]["turnover"] == 8.9e11
    off = TestClient(create_app(os.environ["TEST_DATABASE_URL"], live_feed=live.LiveFeed(None))).get("/api/live").json()
    assert off["available"] is False and off["provider"]["key"] == "off"

    page = client.get("/market", params={"date": str(market[40])}).text
    assert 'class="live-panel needs-js" data-live-panel' in page and "data-live-symbols=\"2330" in page
    assert 'data-live-panel data-live-symbols="2330"' in client.get("/stock/2330").text
    assert "lp-live needs-js" in client.get("/").text
    exported = export_day(conn, market[40], tmp_path).read_text(encoding="utf-8")
    assert "data-live-panel" not in exported                                     # 匯出檔沒有伺服器，不放即時
