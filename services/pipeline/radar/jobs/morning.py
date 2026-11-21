"""早上的流程（Windows 工作排程器平日 09:35 叫起來跑一次）：開盤快照 → 新聞 → 匯出 HTML 檔 → 發布公開網頁。

- 開盤快照：前一個交易日有訊號的股票＋加權、櫃買指數，開盤後的報價存進 open_snapshots，只給系統判斷用，不對外顯示
  （即時行情對外提供要先取得證交所授權）。來源是證交所基本市況報導網站（live.MisProvider），一次最多 40 檔。
- 公開網頁發布的仍然是前一天的盤後資料，只多了最新的新聞，沒有盤中股價。
"""
from __future__ import annotations

import logging
from datetime import datetime, time

from radar import repository as repo
from radar.config import TAIPEI, Settings
from radar.live import MAX_SYMBOLS, MisProvider

log = logging.getLogger(__name__)

MAX_STOCKS = 120


def _num(v):
    return v if v is None else round(v, 2)


def snapshot_open(conn, provider, now: datetime) -> int:
    """今天是交易日、而且已經開盤才抓；只存報價日期是今天的（上游還沒更新時不存舊的）。回傳存了幾筆。"""
    from radar.web.queries import is_trading_day
    if not is_trading_day(conn, now.date()) or now.time() < time(9, 0):
        log.info("開盤快照：今天休市或還沒開盤，略過")
        return 0
    stocks = conn.execute(
        """SELECT s.symbol, s.market FROM market_signals m JOIN stocks s ON s.id = m.stock_id
            WHERE m.trade_date = (SELECT max(trade_date) FROM market_signals)
            GROUP BY s.symbol, s.market ORDER BY count(*) DESC, s.symbol LIMIT %s""", (MAX_STOCKS,)).fetchall()
    markets = dict(stocks)
    symbols = list(markets)
    quotes = []
    for i in range(0, max(len(symbols), 1), MAX_SYMBOLS):     # 沒有訊號股票時也抓一次，至少有兩個指數
        quotes += provider.fetch(symbols[i:i + MAX_SYMBOLS], markets)["quotes"]
    today = now.strftime("%Y%m%d")
    rows = {q["symbol"]: q for q in quotes if q.get("symbol") and q.get("date") == today}
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO open_snapshots (trade_date, symbol, captured_at, quote_time, price, prev_close, open, high, low,
                                           volume_lots)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (trade_date, symbol) DO UPDATE
                  SET captured_at = EXCLUDED.captured_at, quote_time = EXCLUDED.quote_time, price = EXCLUDED.price,
                      prev_close = EXCLUDED.prev_close, open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                      volume_lots = EXCLUDED.volume_lots""",
            [(now.date(), s, now, q["time"], _num(q["price"]), _num(q["prev_close"]), _num(q["open"]), _num(q["high"]),
              _num(q["low"]), q["volume_lots"]) for s, q in rows.items()])
    log.info("開盤快照：%d 筆（股票 %d 檔＋指數）", len(rows), len(symbols))
    return len(rows)


def update_open_snapshot(settings: Settings) -> None:
    with repo.connect(settings.database_url) as conn:
        snapshot_open(conn, MisProvider(), datetime.now(TAIPEI))


def run_morning(settings: Settings) -> None:
    from radar.jobs.scheduler import _safe, export_latest, publish_latest_pages, wait_for_database
    from radar.news import update_news
    log.info("早上流程開始")
    wait_for_database(settings)
    _safe(update_open_snapshot, settings, "開盤快照")
    _safe(update_news, settings, "新聞")
    _safe(export_latest, settings, "匯出 HTML 檔")
    _safe(publish_latest_pages, settings, "發布公開網頁")
    log.info("早上流程結束")
