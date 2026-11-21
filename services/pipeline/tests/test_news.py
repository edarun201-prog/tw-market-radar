"""新聞 RSS 與開盤快照：解析、入庫、今日市場與個股頁；開盤快照只存今天的報價、不對網站開放。"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from radar.config import TAIPEI
from radar.jobs.morning import snapshot_open
from radar.news import MARKET_FEEDS, NewsItem, collect_news, parse_rss

from .test_web import client, market  # noqa: F401  （共用網站測試的人工市場）

RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Yahoo股市</title>
<item><title><![CDATA[台股大崩 2,393 點  創史上第三大跌點]]></title><link>https://tw.stock.yahoo.com/news/a-1.html</link>
  <pubDate>Sat, 21 Nov 2026 03:21:47 GMT</pubDate><description><![CDATA[全文不存]]></description></item>
<item><title>沒有時間的新聞</title><link>https://example.com/b</link></item>
<item><title></title><link>https://example.com/c</link><pubDate>Sat, 21 Nov 2026 03:00:00 GMT</pubDate></item>
<item><title>中央社 財經</title><link>https://www.cna.com.tw/news/afe/1.aspx</link><pubDate>Sat, 21 Nov 2026 11:51:25 +0800</pubDate></item>
</channel></rss>""".encode()


def test_parse_rss():
    items = parse_rss(RSS)
    assert [i.title for i in items] == ["台股大崩 2,393 點 創史上第三大跌點", "中央社 財經"]   # 缺時間、缺標題的略過；空白合併
    assert items[0].published_at.astimezone(TAIPEI) == datetime(2026, 11, 21, 11, 21, 47, tzinfo=TAIPEI)
    assert items[1].url == "https://www.cna.com.tw/news/afe/1.aspx"


class FakeFetcher:
    def __init__(self, now):
        self.now, self.urls = now, []

    def get(self, url):
        self.urls.append(url)
        if url in MARKET_FEEDS.values():
            return [NewsItem(f"大盤新聞 {url[-10:]}", f"{url}#1", self.now - timedelta(hours=1))]
        sym = url.rsplit("=", 1)[1]
        return [NewsItem(f"{sym} 個股新聞", f"https://tw.stock.yahoo.com/news/{sym}.html", self.now - timedelta(hours=2))]


def test_news_saved_and_shown(conn, client, market):  # noqa: F811
    f = FakeFetcher(datetime.now(TAIPEI))
    first = collect_news(conn, f)
    assert collect_news(conn, f) == first                                   # 重跑不重複
    assert first["yahoo_market"] == first["cna_finance"] == 1 and first["yahoo_stock"] >= 1
    assert all("s=" not in u or u.split("=")[1] in ("2330", "2317") for u in f.urls)   # 只抓有訊號的股票
    n = conn.execute("SELECT count(*) FROM news_items").fetchone()[0]
    assert n == 2 + first["yahoo_stock"]

    home = client.get("/market").text
    assert 'id="news-title"' in home and "大盤新聞" in home and 'rel="noopener noreferrer"' in home
    sym = conn.execute("SELECT s.symbol FROM news_items n JOIN stocks s ON s.id = n.stock_id LIMIT 1").fetchone()[0]
    assert f"{sym} 個股新聞" in client.get(f"/stock/{sym}").text
    past = client.get("/market", params={"date": str(market[-2])}).text
    assert 'id="news-title"' not in past                                       # 過去的日子看不到之後的新聞


class FakeMis:
    def __init__(self, day):
        self.day, self.calls = day.strftime("%Y%m%d"), []

    def fetch(self, symbols, markets=None):
        self.calls.append(list(symbols))
        q = lambda s, d: {"symbol": s, "date": d, "time": None, "price": 101.234, "prev_close": 100.0, "open": 100.5,  # noqa: E731
                          "high": 102.0, "low": 100.0, "volume_lots": 1234}
        return {"quotes": [q("t00", self.day), q("o00", self.day)] + [q(s, self.day) for s in symbols[:-1]]
                          + [q(s, "20200101") for s in symbols[-1:]]}               # 最後一檔上游還沒更新


def test_open_snapshot_only_today_and_not_public(conn, client, market):  # noqa: F811
    day = market[-1]
    mis = FakeMis(day)
    assert snapshot_open(conn, mis, datetime.combine(day, time(8, 50), TAIPEI)) == 0 and not mis.calls  # 還沒開盤
    saturday = day + timedelta(days=(5 - day.weekday()) % 7 or 7)
    assert snapshot_open(conn, mis, datetime.combine(saturday, time(9, 35), TAIPEI)) == 0                # 休市
    n = snapshot_open(conn, mis, datetime.combine(day, time(9, 35), TAIPEI))
    symbols = mis.calls[-1]
    assert n == 2 + len(symbols) - 1                                            # 兩個指數＋今天有報價的股票
    assert conn.execute("SELECT price FROM open_snapshots WHERE symbol = 't00'").fetchone()[0] == Decimal("101.23")
    assert conn.execute("SELECT count(*) FROM open_snapshots WHERE symbol = %s", (symbols[-1],)).fetchone()[0] == 0
    granted = conn.execute("SELECT has_table_privilege('radar_readonly', 'open_snapshots', 'SELECT')").fetchone()[0]
    assert granted is False                                                    # 網站的唯讀帳號讀不到
