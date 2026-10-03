"""只下載原始檔、不碰資料庫：先把一年份回應存進 data/raw，之後用 `backfill --from-raw` 幾分鐘內入庫。

已存在的原始檔直接跳過，中斷後重跑會接著下載。休市日只存行情那份（查無資料），不抓法人。
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date

from radar import raw_store
from radar.adapters.base import FetchError, MarketDataAdapter, RawPayload
from radar.config import Settings
from radar.jobs.backfill import daterange
from radar.jobs.ingest import INSTITUTIONAL, MARKET_DAY

log = logging.getLogger(__name__)


def _cached_or_fetch(adapter: MarketDataAdapter, settings: Settings, dataset: str, d: date,
                     force: bool) -> tuple[RawPayload, bool]:
    path = raw_store.raw_path(settings.raw_dir, adapter.source_code, dataset, d)
    if path.exists() and not force:
        return raw_store.load(settings.raw_dir, adapter.source_code, dataset, d), True
    fetch = adapter.fetch_market_day if dataset == MARKET_DAY else adapter.fetch_institutional
    payload = fetch(d)
    raw_store.save(settings.raw_dir, payload)
    return payload, False


def prefetch(adapter: MarketDataAdapter, settings: Settings, start: date, end: date, *,
             force: bool = False) -> Counter:
    if start > end:
        raise ValueError("start 不能晚於 end")
    totals: Counter = Counter()
    days = [d for d in daterange(start, end) if d.weekday() < 5]
    for i, d in enumerate(days, 1):
        try:
            mi, cached = _cached_or_fetch(adapter, settings, MARKET_DAY, d, force)
            if not mi.has_data:
                totals["closed"] += 1
                log.info("[%d/%d] %s 休市（查無資料）", i, len(days), d)
                continue
            t86, cached_t86 = _cached_or_fetch(adapter, settings, INSTITUTIONAL, d, force)
        except FetchError as e:
            totals["failed"] += 1
            log.error("[%d/%d] %s 下載失敗：%s", i, len(days), d, e)
            continue
        totals["cached" if cached and cached_t86 else "fetched"] += 1
        if not t86.has_data:
            totals["t86_missing"] += 1
        log.info("[%d/%d] %s 行情%s、法人%s", i, len(days), d,
                 "已存在" if cached else "已下載", ("已存在" if cached_t86 else "已下載") if t86.has_data else "查無資料")
    log.info("下載完成：%s", dict(totals))
    return totals
