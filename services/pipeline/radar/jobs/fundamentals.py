"""估值與財報：抓取 → 存原始檔 → 正規化 → 入庫，並記錄在 ingestion_runs。

證交所、櫃買中心的 OpenAPI 都只提供「最新一期」：估值是最新一個交易日，財報是最新一季（年度累計）。
所以每天抓一次，從 2026-10 起逐日、逐季累積；錯過的日子補不回來。每天共 14 次請求（間隔與其他抓取相同）。
失敗只記錄，不影響行情與其他步驟。
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from radar import raw_store, repository as repo
from radar.adapters.base import FetchError
from radar.config import TAIPEI, Settings
from radar.normalizers.common import NormalizeError
from radar.normalizers.fundamentals import normalize_financial_reports, normalize_valuations

log = logging.getLogger(__name__)

VALUATION, FIN_REPORT = "BWIBBU", "FIN_REPORT"
MIN_ROWS = 100      # 少於這個數字當成來源異常，不寫入


def _market(adapter) -> str:
    return "TPEX" if adapter.source_code == "TPEX" else "TWSE"


def _fail(conn, src: int, dataset: str, d: date, msg: str, **kw) -> str:
    repo.start_run(conn, src, dataset, d)
    repo.finish_run(conn, src, dataset, d, "failed", error=msg, **kw)
    log.error("%s %s %s", dataset, d, msg)
    return "failed"


def sync_valuations(conn, adapter, settings: Settings, as_of: date) -> str:
    """最新一個交易日的本益比、殖利率、股價淨值比。ingestion_runs 記在資料本身的日期（不是抓取日）。"""
    src = repo.source_id(conn, adapter.source_code)
    try:
        p = adapter.fetch_valuations(as_of)
        path = str(raw_store.save(settings.raw_dir, p))
        day, warnings = normalize_valuations(p)
    except (FetchError, NormalizeError) as e:
        return _fail(conn, src, VALUATION, as_of, str(e))
    if len(day.rows) < MIN_ROWS:
        return _fail(conn, src, VALUATION, day.trade_date, f"只有 {len(day.rows)} 筆，少於 {MIN_ROWS}", raw_path=path)
    repo.start_run(conn, src, VALUATION, day.trade_date)
    with conn.transaction():
        n, skipped = repo.write_valuations(conn, src, _market(adapter), day)
    if skipped:
        warnings.append(f"{skipped} 檔不在股票清單（例如特別股、尚未建立的新股），略過")
    repo.finish_run(conn, src, VALUATION, day.trade_date, "success", row_count=n, warnings=warnings, raw_path=path)
    log.info("%s 估值 %s：%d 筆", adapter.source_code, day.trade_date, n)
    return "success"


def sync_financial_reports(conn, adapter, settings: Settings, as_of: date) -> str:
    """最新一季的綜合損益表（各產業格式）。ingestion_runs 記在抓取日。"""
    src = repo.source_id(conn, adapter.source_code)
    try:
        p = adapter.fetch_financial_reports(as_of)
        path = str(raw_store.save(settings.raw_dir, p))
        reports, warnings = normalize_financial_reports(p)
    except (FetchError, NormalizeError) as e:
        return _fail(conn, src, FIN_REPORT, as_of, str(e))
    if len(reports) < MIN_ROWS:
        return _fail(conn, src, FIN_REPORT, as_of, f"只有 {len(reports)} 家，少於 {MIN_ROWS}", raw_path=path)
    repo.start_run(conn, src, FIN_REPORT, as_of)
    with conn.transaction():
        n, skipped = repo.write_financial_reports(conn, src, _market(adapter), reports)
    if skipped:
        warnings.append(f"{skipped} 家不在股票清單，略過")
    repo.finish_run(conn, src, FIN_REPORT, as_of, "success", row_count=n, warnings=warnings, raw_path=path)
    periods = sorted({(r.fiscal_year, r.quarter) for r in reports})
    log.info("%s 財報：%d 家，期間 %s", adapter.source_code, n, periods)
    return "success"


def update_fundamentals(settings: Settings, adapters=None) -> dict[str, str]:
    """上市、上櫃的估值與財報各抓一次；任何一項失敗都不影響其他項。"""
    if adapters is None:
        from radar.adapters.tpex import TpexAdapter
        from radar.adapters.twse import TwseAdapter
        adapters = (TwseAdapter(settings), TpexAdapter(settings))
    today = datetime.now(TAIPEI).date()
    results = {}
    with repo.connect(settings.database_url) as conn:
        for adapter in adapters:
            for step in (sync_valuations, sync_financial_reports):
                key = f"{adapter.source_code} {step.__name__}"
                try:
                    results[key] = step(conn, adapter, settings, today)
                except Exception:
                    log.exception("%s 失敗", key)
                    results[key] = "failed"
    return results
