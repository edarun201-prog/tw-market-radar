"""歷史回補：逐日呼叫 ingest_date，已成功的日期自動跳過，中斷後可直接重跑續傳。

1 年約 245 個交易日 × 2 個資料集 ≈ 500 次請求；以預設間隔 4 秒計，約 40–50 分鐘。
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta

from radar.adapters.base import MarketDataAdapter
from radar.config import Settings
from radar.jobs.ingest import ingest_date

log = logging.getLogger(__name__)


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def backfill(conn, adapter: MarketDataAdapter, settings: Settings, start: date, end: date, *,
             force: bool = False, from_raw: bool = False) -> Counter:
    if start > end:
        raise ValueError("start 不能晚於 end")
    totals: Counter = Counter()
    days = list(daterange(start, end))
    for i, d in enumerate(days, 1):
        r = ingest_date(conn, adapter, settings, d, final=True, from_raw=from_raw, skip_done=not force)
        for ds, st in r.status.items():
            totals[f"{ds}:{st}"] += 1
        if d.weekday() < 5:
            log.info("[%d/%d] %s %s", i, len(days), d, r.status)
    log.info("回補完成：%s", dict(totals))
    return totals
