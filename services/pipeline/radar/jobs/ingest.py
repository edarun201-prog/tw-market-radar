"""單日抓取流程：抓取 → 存原始檔 → 正規化 → 驗證 → 入庫，並記錄在 ingestion_runs。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Callable

from radar import raw_store, repository as repo
from radar.adapters.base import FetchError, MarketDataAdapter, RawPayload
from radar.config import Settings
from radar.normalizers.common import NormalizeError
from radar.normalizers import tpex as tpex_norm, twse as twse_norm
from radar.validators import validate_institutional, validate_market_day

log = logging.getLogger(__name__)

MARKET_DAY, INSTITUTIONAL = "MI_INDEX", "T86"
DATASETS = (MARKET_DAY, INSTITUTIONAL)


@dataclass
class DayResult:
    trade_date: date
    status: dict[str, str]  # dataset → success / no_data / pending / failed / skipped

    @property
    def done(self) -> bool:
        return all(v in ("success", "no_data", "skipped") for v in self.status.values())


def _get_payload(adapter: MarketDataAdapter, settings: Settings, dataset: str, d: date,
                 from_raw: bool) -> tuple[RawPayload, str | None]:
    if from_raw:
        path = raw_store.raw_path(settings.raw_dir, adapter.source_code, dataset, d)
        return raw_store.load(settings.raw_dir, adapter.source_code, dataset, d), str(path)
    fetch: Callable[[date], RawPayload] = (
        adapter.fetch_market_day if dataset == MARKET_DAY else adapter.fetch_institutional
    )
    payload = fetch(d)
    return payload, str(raw_store.save(settings.raw_dir, payload))


def ingest_date(conn, adapter: MarketDataAdapter, settings: Settings, d: date, *,
                final: bool = True, from_raw: bool = False, skip_done: bool = False) -> DayResult:
    """final=False 時，「查無資料」視為尚未公布（pending），之後會重試；final=True 時視為休市。"""
    result = DayResult(d, {})
    if d.weekday() >= 5:
        repo.set_calendar(conn, d, False, "weekend")
        result.status = {ds: "skipped" for ds in DATASETS}
        return result

    src = repo.source_id(conn, adapter.source_code)

    # ---- 1) 全市場行情 ----------------------------------------------------
    if skip_done and repo.run_status(conn, src, MARKET_DAY, d) in repo.FINAL_STATUSES:
        result.status[MARKET_DAY] = "skipped"
    else:
        result.status[MARKET_DAY] = _ingest_market_day(conn, adapter, settings, src, d, final, from_raw)

    if repo.is_open_day(conn, d) is not True:
        result.status[INSTITUTIONAL] = "skipped"  # 休市或行情尚未入庫，法人資料不處理
        return result

    # ---- 2) 三大法人 ------------------------------------------------------
    if skip_done and repo.run_status(conn, src, INSTITUTIONAL, d) in repo.FINAL_STATUSES:
        result.status[INSTITUTIONAL] = "skipped"
    else:
        result.status[INSTITUTIONAL] = _ingest_institutional(conn, adapter, settings, src, d, final, from_raw)
    return result


def _norm(adapter):
    """依資料來源挑 normalizer：證交所（上市）或櫃買中心（上櫃）。"""
    return tpex_norm if adapter.source_code == "TPEX" else twse_norm


def _ingest_market_day(conn, adapter, settings, src, d, final, from_raw) -> str:
    repo.start_run(conn, src, MARKET_DAY, d)
    try:
        payload, path = _get_payload(adapter, settings, MARKET_DAY, d, from_raw)
    except (FetchError, FileNotFoundError) as e:
        repo.finish_run(conn, src, MARKET_DAY, d, "failed", error=str(e))
        log.error("%s MI_INDEX 抓取失敗：%s", d, e)
        return "failed"

    drives_calendar = getattr(adapter, "drives_calendar", True)   # 只有證交所能決定交易日曆
    if not payload.has_data:
        if final and drives_calendar:
            repo.set_calendar(conn, d, False, f"來源回覆：{payload.meta.get('stat', '')}"[:200])
            repo.finish_run(conn, src, MARKET_DAY, d, "no_data", raw_path=path)
            log.info("%s 休市（查無資料）", d)
            return "no_data"
        repo.finish_run(conn, src, MARKET_DAY, d, "pending", raw_path=path)
        log.info("%s 行情尚未公布，稍後重試", d)
        return "pending"

    try:
        snapshot, norm_warn = _norm(adapter).normalize_market_day(payload)
    except NormalizeError as e:
        repo.finish_run(conn, src, MARKET_DAY, d, "failed", error=f"格式錯誤：{e}", raw_path=path)
        log.error("%s MI_INDEX 格式錯誤：%s", d, e)
        return "failed"

    check, snapshot = validate_market_day(snapshot, settings, repo.prev_row_count(conn, "daily_prices", d, snapshot.market))
    warnings = norm_warn + check.warnings
    if not check.ok:
        repo.finish_run(conn, src, MARKET_DAY, d, "failed", error="驗證失敗：" + "；".join(check.errors),
                        warnings=warnings, raw_path=path)
        log.error("%s MI_INDEX 驗證失敗：%s", d, check.errors)
        return "failed"

    with conn.transaction():
        if drives_calendar:
            repo.set_calendar(conn, d, True)
        else:
            repo.ensure_calendar(conn, d)
        n = repo.write_market_day(conn, src, snapshot)
    repo.finish_run(conn, src, MARKET_DAY, d, "success", row_count=n, warnings=warnings, raw_path=path)
    log.info("%s MI_INDEX 入庫 %d 筆（警告 %d）", d, n, len(warnings))
    return "success"


def _ingest_institutional(conn, adapter, settings, src, d, final, from_raw) -> str:
    repo.start_run(conn, src, INSTITUTIONAL, d)
    try:
        payload, path = _get_payload(adapter, settings, INSTITUTIONAL, d, from_raw)
    except (FetchError, FileNotFoundError) as e:
        repo.finish_run(conn, src, INSTITUTIONAL, d, "failed", error=str(e))
        log.error("%s T86 抓取失敗：%s", d, e)
        return "failed"

    if not payload.has_data:
        status = "no_data" if final else "pending"
        repo.finish_run(conn, src, INSTITUTIONAL, d, status, raw_path=path,
                        error=None if not final else "交易日但查無法人資料")
        log.info("%s T86 %s", d, status)
        return status

    try:
        day, norm_warn = _norm(adapter).normalize_institutional(payload)
    except NormalizeError as e:
        repo.finish_run(conn, src, INSTITUTIONAL, d, "failed", error=f"格式錯誤：{e}", raw_path=path)
        log.error("%s T86 格式錯誤：%s", d, e)
        return "failed"

    check = validate_institutional(day, settings, repo.prev_row_count(conn, "institutional_flows", d, day.market))
    warnings = norm_warn + check.warnings
    if not check.ok:
        repo.finish_run(conn, src, INSTITUTIONAL, d, "failed", error="驗證失敗：" + "；".join(check.errors),
                        warnings=warnings, raw_path=path)
        log.error("%s T86 驗證失敗：%s", d, check.errors)
        return "failed"

    with conn.transaction():
        n = repo.write_institutional(conn, src, day)
    repo.finish_run(conn, src, INSTITUTIONAL, d, "success", row_count=n, warnings=warnings, raw_path=path)
    log.info("%s T86 入庫 %d 筆（警告 %d）", d, n, len(warnings))
    return "success"
