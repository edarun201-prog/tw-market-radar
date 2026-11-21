"""新聞：新聞網站公開的 RSS → news_items。只存標題、連結、時間，不存全文（有著作權）；網頁上點標題連回原網站。

來源（2026-11 確認過 robots.txt 允許、RSS 不需要金鑰）
- Yahoo 股市「台股」（大盤）與「個股」（rss?s=代號，上市、上櫃都用同一種代號）
- 中央社財經（大盤）
經濟日報的使用條款禁止商業與 AI 用途，不用。

個股新聞只抓最近一個交易日有訊號的股票（最多 MAX_STOCKS 檔），請求間隔和其他抓取相同（REQUEST_INTERVAL_SEC）。
"""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx

from radar import repository as repo
from radar.config import TAIPEI, Settings

log = logging.getLogger(__name__)

MARKET_FEEDS = {
    "yahoo_market": "https://tw.stock.yahoo.com/rss?category=tw-market",
    "cna_finance": "https://feeds.feedburner.com/rsscna/finance",
}
STOCK_FEED = "https://tw.stock.yahoo.com/rss?s={symbol}"
MAX_STOCKS = 60
HEADERS = {"User-Agent": "tw-market-radar/0.1 (after-hours research project)",
           "Accept": "application/rss+xml, application/xml, text/xml"}


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    published_at: datetime


def parse_rss(xml: bytes) -> list[NewsItem]:
    """RSS 2.0 的 item：title、link、pubDate。缺標題、連結或時間的略過。"""
    out = []
    for it in ET.fromstring(xml).iter("item"):
        title, link, pub = (it.findtext(k) or "" for k in ("title", "link", "pubDate"))
        title, link = " ".join(title.split()), link.strip()
        try:
            when = parsedate_to_datetime(pub.strip())
        except (TypeError, ValueError):
            continue
        if title and link.startswith("http"):
            out.append(NewsItem(title, link, when if when.tzinfo else when.replace(tzinfo=TAIPEI)))
    return out


class Fetcher:
    """節流的 RSS 抓取：任兩次請求至少隔 interval 秒；失敗只記錄，回傳空清單。"""

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.interval = settings.request_interval_sec
        self.client = client or httpx.Client(headers=HEADERS, timeout=settings.request_timeout_sec, follow_redirects=True)
        self._last = -1e9

    def get(self, url: str) -> list[NewsItem]:
        wait = self.interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        try:
            r = self.client.get(url)
            r.raise_for_status()
            return parse_rss(r.content)
        except (httpx.HTTPError, ET.ParseError) as e:
            log.warning("新聞 RSS 抓取失敗 %s：%s", url, e)
            return []


def save(conn, feed: str, items: list[NewsItem], stock_id: int | None = None) -> int:
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO news_items (feed, stock_id, title, url, published_at) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (url, stock_id) DO UPDATE SET title = EXCLUDED.title""",
            [(feed, stock_id, n.title, n.url, n.published_at) for n in items])
    return len(items)


def radar_stocks(conn, limit: int = MAX_STOCKS) -> list[tuple[int, str]]:
    """最近一個交易日有訊號的股票（訊號越多越前面）。"""
    return conn.execute(
        """SELECT s.id, s.symbol FROM market_signals m JOIN stocks s ON s.id = m.stock_id
            WHERE m.trade_date = (SELECT max(trade_date) FROM market_signals)
            GROUP BY s.id, s.symbol ORDER BY count(*) DESC, s.symbol LIMIT %s""", (limit,)).fetchall()


def collect_news(conn, fetcher) -> dict[str, int]:
    counts: dict[str, int] = {}
    for feed, url in MARKET_FEEDS.items():
        counts[feed] = save(conn, feed, fetcher.get(url))
    stocks = radar_stocks(conn)
    counts["yahoo_stock"] = sum(save(conn, "yahoo_stock", fetcher.get(STOCK_FEED.format(symbol=sym)), sid)
                                for sid, sym in stocks)
    log.info("新聞：%s（個股 %d 檔）", counts, len(stocks))
    return counts


def update_news(settings: Settings) -> dict[str, int]:
    with repo.connect(settings.database_url) as conn:
        return collect_news(conn, Fetcher(settings))
