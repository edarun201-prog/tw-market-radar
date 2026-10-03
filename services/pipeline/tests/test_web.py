"""網站與 JSON API：用人工資料跑一遍所有頁面。"""
import os
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from radar.features import build_features
from radar.signals import generate_signals
from radar.web.app import create_app

from .test_analytics import _seed, _weekdays


@pytest.fixture
def client(conn):
    return TestClient(create_app(os.environ["TEST_DATABASE_URL"]))


@pytest.fixture
def market(conn):
    days = _weekdays(45)
    vols = [1_000_000] * 45
    vols[40] = 6_000_000                                               # 最後幾天有一次爆量
    ids, src = _seed(conn, days, {"2330": [(100 + i, v) for i, v in enumerate(vols)], "2317": [(50, 2_000_000)] * 45})
    conn.execute("UPDATE stocks SET name = '台積電' WHERE symbol = '2330'")
    for i, d in enumerate(days):
        conn.execute("""INSERT INTO market_indices (index_code, trade_date, close, change, change_pct, source_id)
                        VALUES ('發行量加權股價指數', %s, %s, 10, 0.05, %s), ('半導體類指數', %s, 500, 5, 1.0, %s)""",
                     (d, 20000 + i * 10, src, d, src))
        conn.execute("""INSERT INTO market_breadth (trade_date, market, advancers, decliners, unchanged, total_turnover,
                        total_volume, total_trades, source_id) VALUES (%s, 'TWSE', 600, 300, 100, 500000000000, 9000000000, 4000000, %s)""",
                     (d, src))
    build_features(conn, days[0], days[-1])
    generate_signals(conn, days[25], days[-1])
    return days


def test_home_shows_latest_day(client, market):
    r = client.get("/market")
    assert r.status_code == 200
    assert "加權指數" in r.text and market[-1].strftime("%Y/%m/%d") in r.text
    assert "今日市場" in r.text and "今日雷達" in r.text


def test_home_on_holiday_falls_back(client, market):
    saturday = market[-1] + timedelta(days=(5 - market[-1].weekday()) % 7 or 7)
    r = client.get(f"/market?date={saturday}")
    assert r.status_code == 200 and "沒有開市" in r.text


def test_stock_page_and_404(client, market):
    r = client.get("/stock/2330")
    assert r.status_code == 200 and "<svg" in r.text and "台積電" in r.text
    missing = client.get("/stock/9999")
    assert missing.status_code == 404 and "找不到" in missing.text


def test_search_redirects_on_single_match(client, market):
    r = client.get("/search", params={"q": "台積"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/stock/2330"
    assert "找不到" in client.get("/search", params={"q": "不存在的公司"}).text


def test_json_api(client, market):
    m = client.get("/api/market").json()
    assert m["index"]["close"] and m["breadth"]["advancers"] == 600
    assert client.get("/api/signals", params={"type": "nope"}).status_code == 422
    s = client.get("/api/stocks/2330", params={"days": 10}).json()
    assert len(s["prices"]) == 10 and s["profile"]["symbol"] == "2330"
    assert client.get("/api/stocks/9999").status_code == 404


def test_empty_database(client):
    r = client.get("/")
    assert r.status_code == 503 and "還沒有行情資料" in r.text


def test_postponed_ex_date_and_neutral_volume_spike(conn, client):
    days = _weekdays(45)
    typhoon = days[42]
    open_days = [d for d in days if d != typhoon]
    vols = [1_000_000] * 44
    vols[43] = 6_000_000
    ids, src = _seed(conn, open_days, {"2891": [(70, v) for v in vols]}, closed=[typhoon])
    conn.execute("""INSERT INTO corporate_actions (stock_id, ex_date, action_type, prev_close, ref_price, value, source_id)
                    VALUES (%s, %s, 'dividend', 70, 67.5, 2.5, %s)""", (ids["2891"], typhoon, src))
    for d in open_days:
        conn.execute("""INSERT INTO market_breadth (trade_date, market, advancers, decliners, unchanged, source_id)
                        VALUES (%s, 'TWSE', 1, 1, 1, %s)""", (d, src))
    build_features(conn, open_days[0], open_days[-1])
    generate_signals(conn, open_days[30], open_days[-1])
    page = client.get("/stock/2891").text
    assert f"{open_days[42]:%Y/%m/%d}" in page and f"原訂 {typhoon:%m/%d}，休市順延" in page
    home = client.get("/market").text
    assert '<span class="dot neutral" aria-hidden="true"></span><span class="r-name">量能爆增' in home   # 爆量不分多空


def test_home_reading_order_and_modes(client, market):
    html = client.get("/market").text
    order = [html.index(s) for s in ("今日市場", "今日雷達", "類股指數漲跌")]
    assert order == sorted(order)                                              # 大盤 → 雷達總覽 → 類股
    radar = client.get("/radar", params={"date": str(market[40])}).text      # 這天有訊號
    assert "市場熱點" in radar and '<details class="radar-row"' in radar       # 雷達頁：完整清單與熱點
    assert radar.count('id="help-vol_spike"') == 1
    assert "偏強" in html or "偏弱" in html or "分歧" in html or "持平" in html  # 市場狀況標籤
    assert 'data-set-mode="beginner"' in html and 'class="std-only"' in html or "std-only" in html
    assert html.count('id="help-vol_spike"') == 1                             # 說明整頁只放一份


def test_stock_page_explains_why(client, market):
    html = client.get("/stock/2330").text
    assert "為什麼被雷達注意？" in html and "簡單理解" in html
    assert html.index("為什麼被雷達注意？") < html.index("查看詳細 K 線")        # 先說原因，再看 K 線
    assert "data-std-open" in html                                             # 新手模式收起 K 線


def test_about_page_is_generated_from_engine_constants(client, market):
    from radar import signals as sg
    from radar.explain import SIGNAL_INFO
    html = client.get("/about").text
    for i in SIGNAL_INFO.values():
        assert i.label in html and i.rule in html
    assert f"{sg.VOL_RATIO_MIN:g} 倍" in html and "冷卻機制" in html and "還原價" in html


def test_api_signal_objects_and_explain(client, market):
    types = client.get("/api/signal-types").json()["types"]
    assert {t["type"] for t in types} == set(__import__("radar.signals", fromlist=["TYPES"]).TYPES)
    assert all(t["what"] and t["means"] and t["not_mean"] and t["rule"] for t in types)
    day = market[40]
    sigs = client.get("/api/signals", params={"date": str(day)}).json()["signals"]
    assert sigs and set(sigs[0]) >= {"type", "symbol", "date", "value", "threshold", "explanation", "category"}
    story = client.get("/api/stocks/2330/explain").json()
    assert story["symbol"] == "2330" and story["summary"] and "facts" in story


def test_app_manifest_service_worker_and_offline_page(client, market):
    m = client.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    app = m.json()
    assert app["display"] == "standalone" and app["start_url"] == "/" and app["short_name"] == "台股雷達"
    sizes = {i["sizes"] for i in app["icons"]}
    assert {"192x192", "512x512"} <= sizes and any(i["purpose"] == "maskable" for i in app["icons"])
    for icon in app["icons"]:                                                    # 圖示都真的存在
        r = client.get(icon["src"])
        assert r.status_code == 200
        if icon["type"] == "image/png":
            assert r.content.startswith(b"\x89PNG")

    sw = client.get("/sw.js")
    assert "javascript" in sw.headers["content-type"] and sw.headers["cache-control"] == "no-cache"
    assert "{{" not in sw.text and "/offline" in sw.text and "radar-pages-v1" in sw.text
    off = client.get("/offline")
    assert off.status_code == 200 and "目前連不到雷達" in off.text and "radar-pages-v1" in off.text


def test_pages_are_app_shell(client, market):
    home = client.get("/market").text
    assert 'rel="manifest"' in home and "serviceWorker.register('/sw.js')" in home
    assert 'viewport-fit=cover' in home and 'apple-touch-icon' in home
    # 底部分頁列：首頁的「市場／雷達」是頁內錨點，其他頁連回首頁；目前所在的分頁有 aria-current
    assert 'href="/radar"' in home
    for path, current in (("/market", "市場"), ("/radar", "雷達"), ("/backtest", "回測"), ("/search", "搜尋"), ("/about", "說明")):
        html = client.get(path).text
        nav = html[html.index('class="tabbar"'):]
        nav = nav[:nav.index("</nav>")]
        assert nav.count("aria-current") == 1
        tail = nav[nav.index("aria-current"):]
        assert current in tail[:tail.index("</a>")]
    assert 'href="/radar"' in client.get("/about").text
    assert 'data-recent-symbol="2330"' in client.get("/stock/2330").text
    assert 'class="page-search"' in client.get("/search").text


def _nav_hrefs(html: str, cls: str) -> list[str]:
    start = html.index(f'<nav class="{cls}"')
    return re.findall(r'href="([^"]+)"', html[start:html.index("</nav>", start)])


def test_desktop_and_mobile_have_the_same_navigation(client, market):
    # 桌面版上方導覽列與手機版底部分頁列是同一組項目；搜尋頁兩邊都有同一個搜尋框
    for path in ("/", "/market", "/about", "/search", "/stock/2330"):
        html = client.get(path).text
        assert _nav_hrefs(html, "links") == _nav_hrefs(html, "tabbar")
        assert len(_nav_hrefs(html, "tabbar")) == 5
    for path in ("/market", "/radar", "/about"):
        assert _nav_hrefs(client.get(path).text, "links") == ["/market", "/radar", "/backtest", "/search", "/about"]
    search = client.get("/search").text
    assert 'class="page-search"' in search and 'class="search"' not in search   # 搜尋頁不重複放頁首搜尋框


def test_multi_page_market_and_radar(client, market):
    # 今日市場與今日雷達是兩個頁面：市場頁只放各類訊號數，點一類進到雷達頁的那一類
    day = market[40]                                                            # 這天有訊號
    home = client.get("/market", params={"date": str(day)}).text
    radar = client.get("/radar", params={"date": str(day)}).text
    assert '<details class="radar-row"' not in home and 'id="market-title"' not in radar and 'id="radar-title"' in radar
    types = re.findall(rf'<a class="radar-row link" href="/radar\?date={day}#([a-z_0-9]+)"', home)
    assert types and all(f'<details class="radar-row" id="{t}"' in radar for t in types)
    # 看過去的日子時，市場／雷達之間切換、前後一天都留在「同一種頁面」
    past = market[40]
    html = client.get(f"/radar?date={past}").text
    assert f"{past:%Y/%m/%d}" in html and f'href="/market?date={past}"' in html and 'href="/radar?date=' in html
    assert client.get(f"/market?date={past}").text.count(f'href="/radar?date={past}"') >= 2   # 上方導覽與底部分頁列
    assert client.get("/radar?date=2000-01-01").status_code == client.get("/market?date=2000-01-01").status_code  # 沒資料的日子兩頁一致


def test_landing_page(client, market):
    # 入口頁：全螢幕影片背景、點陣字標題、當日統計（數字跳動的目標值就是資料庫裡的數字），按鈕進到今日市場
    day = market[40]
    html = client.get("/", params={"date": str(day)}).text
    assert 'class="landing-open"' in html and '<section class="landing"' in html
    assert '<video class="lp-video" autoplay muted loop playsinline>' in html and "https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260809_012548_ef22562c-c0ae-4816-ad9d-f8922af4e6a7.mp4" in html
    assert "fonts.googleapis.com/css2?family=Inter:wght@400;500;600" in html and "BubbledotICG-FinePos" in html
    assert "font-awesome/6.5.2/css/all.min.css" in html
    assert "<span>Taiwan Stock</span>" in html and "<span>Intelligence Radar</span>" in html
    assert 'class="lp-cta" href="/market?date=' in html and '看今天的盤後' in html
    targets = [float(x) for x in re.findall(r'data-target="([^"]+)"', html)]
    assert len(targets) == 4 and targets[3] == len(client.get("/api/signals", params={"date": str(day)}).json()["signals"])
    m = client.get("/api/market", params={"date": str(day)}).json()
    assert targets[0] == float(m["index"]["close"]) and targets[1] == m["breadth"]["advancers"] and targets[2] == m["breadth"]["decliners"]
    assert "Trusted by" not in html and "Enterprises" not in html               # 不放不實的背書
    assert "臺灣證券交易所" in html
    assert client.get("/market").status_code == 200 and "今日市場" in client.get("/market").text
