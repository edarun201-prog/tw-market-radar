"""除權息與公司基本資料：抓取 → 存原始檔 → 正規化 → 驗證 → 入庫，並記錄在 ingestion_runs。

除權息以「月」為單位（一年 12 次請求）；已過完的月份成功後不再重抓，當月每次都重抓。
公司基本資料是當下快照，建議每週跑一次，產業別或上市日期有變動就會更新。
休市日曆（當年度）也每週更新，讓每日排程在休市日不必重試到 21:30。

爬蟲（adapter 的 fetch_*、normalizer 的 normalize_*）還沒寫好時，這裡會記錄為 failed，不影響每日行情。
"""
from __future__ import annotations

import calendar
import logging
from collections import Counter
from datetime import date, timedelta

from radar import raw_store, repository as repo
from radar.adapters.base import CompanyProfileAdapter, CorporateActionAdapter, FetchError, RawPayload
from radar.config import Settings
from radar.normalizers.common import NormalizeError
from radar.normalizers import tpex as tpex_norm, twse as normalizers
from radar.validators import validate_company_profiles, validate_ex_rights

log = logging.getLogger(__name__)

EX_RIGHTS, COMPANY, HOLIDAY = "TWT49U", "COMPANY", "HOLIDAY"


def month_ranges(start: date, end: date) -> list[tuple[date, date]]:
    """把 start～end 切成月份，頭尾月份以 start、end 為界。"""
    out = []
    d = start
    while d <= end:
        last = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
        out.append((d, min(last, end)))
        d = last + timedelta(days=1)
    return out


def _payload(adapter, settings: Settings, dataset: str, key: date, fetch, from_raw: bool) -> tuple[RawPayload, str]:
    if from_raw:
        return (raw_store.load(settings.raw_dir, adapter.source_code, dataset, key),
                str(raw_store.raw_path(settings.raw_dir, adapter.source_code, dataset, key)))
    p = fetch()
    return p, str(raw_store.save(settings.raw_dir, p))


def _norm(adapter):
    """依資料來源挑 normalizer：證交所（上市）或櫃買中心（上櫃）。"""
    return tpex_norm if adapter.source_code == "TPEX" else normalizers


def _fail(conn, src, dataset, key, msg, **kw) -> str:
    repo.finish_run(conn, src, dataset, key, "failed", error=msg, **kw)
    log.error("%s %s %s", dataset, key, msg)
    return "failed"


# ---- 除權息 ------------------------------------------------------------------
def ingest_ex_rights(conn, adapter: CorporateActionAdapter, settings: Settings, start: date, end: date, *,
                     from_raw: bool = False) -> str:
    src = repo.source_id(conn, adapter.source_code)
    repo.start_run(conn, src, EX_RIGHTS, start)
    try:
        p, path = _payload(adapter, settings, EX_RIGHTS, start, lambda: adapter.fetch_ex_rights(start, end), from_raw)
    except NotImplementedError as e:
        return _fail(conn, src, EX_RIGHTS, start, f"爬蟲尚未實作：{e}")
    except (FetchError, FileNotFoundError) as e:
        return _fail(conn, src, EX_RIGHTS, start, str(e))

    if not p.has_data:
        repo.finish_run(conn, src, EX_RIGHTS, start, "no_data", raw_path=path)
        return "no_data"
    try:
        period, warnings = _norm(adapter).normalize_ex_rights(p, end)
    except NotImplementedError as e:
        return _fail(conn, src, EX_RIGHTS, start, f"解析尚未實作：{e}", raw_path=path)
    except NormalizeError as e:
        return _fail(conn, src, EX_RIGHTS, start, f"格式錯誤：{e}", raw_path=path)

    check, period = validate_ex_rights(period)
    warnings = warnings + check.warnings
    if not check.ok:
        return _fail(conn, src, EX_RIGHTS, start, "驗證失敗：" + "；".join(check.errors), warnings=warnings, raw_path=path)
    with conn.transaction():
        n = repo.write_corporate_actions(conn, src, period)
    repo.finish_run(conn, src, EX_RIGHTS, start, "success", row_count=n, warnings=warnings, raw_path=path)
    log.info("除權息 %s～%s 入庫 %d 筆（警告 %d）", start, end, n, len(warnings))
    return "success"


def backfill_ex_rights(conn, adapter: CorporateActionAdapter, settings: Settings, start: date, end: date,
                       today: date, *, force: bool = False, from_raw: bool = False) -> Counter:
    src = repo.source_id(conn, adapter.source_code)
    totals: Counter = Counter()
    for m_start, m_end in month_ranges(start, end):
        month_over = m_end < today and m_end.day == calendar.monthrange(m_end.year, m_end.month)[1]
        if not force and month_over and repo.run_status(conn, src, EX_RIGHTS, m_start) in repo.FINAL_STATUSES:
            totals["skipped"] += 1
            continue
        totals[ingest_ex_rights(conn, adapter, settings, m_start, m_end, from_raw=from_raw)] += 1
    log.info("除權息回補完成：%s", dict(totals))
    return totals


# ---- 公司基本資料 -------------------------------------------------------------
def sync_companies(conn, adapter: CompanyProfileAdapter, settings: Settings, as_of: date, *,
                   from_raw: bool = False) -> str:
    src = repo.source_id(conn, adapter.source_code)
    repo.start_run(conn, src, COMPANY, as_of)
    try:
        p, path = _payload(adapter, settings, COMPANY, as_of, lambda: adapter.fetch_company_profiles(as_of), from_raw)
    except NotImplementedError as e:
        return _fail(conn, src, COMPANY, as_of, f"爬蟲尚未實作：{e}")
    except (FetchError, FileNotFoundError) as e:
        return _fail(conn, src, COMPANY, as_of, str(e))

    if not p.has_data:
        return _fail(conn, src, COMPANY, as_of, "來源沒有回傳任何公司資料", raw_path=path)
    try:
        if adapter.source_code == "TPEX":
            # 上櫃公司基本資料只有產業代碼；名稱用證交所的同一套代碼對照（存在資料庫裡）
            p.body["industry_names"] = dict(conn.execute("SELECT code, name FROM industries WHERE market = 'TWSE'").fetchall())
        snap, warnings = _norm(adapter).normalize_company_profiles(p)
    except NotImplementedError as e:
        return _fail(conn, src, COMPANY, as_of, f"解析尚未實作：{e}", raw_path=path)
    except NormalizeError as e:
        return _fail(conn, src, COMPANY, as_of, f"格式錯誤：{e}", raw_path=path)

    check, snap = validate_company_profiles(snap)
    warnings = warnings + check.warnings
    if not check.ok:
        return _fail(conn, src, COMPANY, as_of, "驗證失敗：" + "；".join(check.errors), warnings=warnings, raw_path=path)
    with conn.transaction():
        n = repo.write_company_profiles(conn, snap)
    repo.finish_run(conn, src, COMPANY, as_of, "success", row_count=n, warnings=warnings, raw_path=path)
    log.info("公司基本資料 %s 更新 %d 家（警告 %d）", as_of, n, len(warnings))
    return "success"


# ---- 休市日曆 ------------------------------------------------------------------
def sync_holidays(conn, adapter, settings: Settings, as_of: date, *, from_raw: bool = False) -> str:
    """把今天以後的休市日先寫進 trading_calendar；已由實際資料決定的日子不覆蓋。"""
    src = repo.source_id(conn, adapter.source_code)
    repo.start_run(conn, src, HOLIDAY, as_of)
    try:
        p, path = _payload(adapter, settings, HOLIDAY, as_of, lambda: adapter.fetch_holiday_schedule(as_of), from_raw)
        days, warnings = normalizers.normalize_holidays(p)
    except (FetchError, FileNotFoundError, NormalizeError) as e:
        return _fail(conn, src, HOLIDAY, as_of, str(e))
    n = repo.mark_known_holidays(conn, days, as_of)
    repo.finish_run(conn, src, HOLIDAY, as_of, "success", row_count=n, warnings=warnings, raw_path=path)
    log.info("休市日曆：今天以後 %d 個休市日", n)
    return "success"
