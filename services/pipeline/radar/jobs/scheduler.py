"""每日排程：週一至週五 18:30（台北時間）開始抓當日資料，未齊則每 20 分鐘重試到 21:30。

啟動時會先補抓「上次成功日」之後漏掉的交易日（例如電腦關機錯過排程）。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from radar import repository as repo
from radar.adapters.twse import TwseAdapter
from radar.config import TAIPEI, Settings
from radar.jobs.backfill import backfill
from radar.jobs.ingest import ingest_date

log = logging.getLogger(__name__)


def run_today(settings: Settings, today: date | None = None) -> None:
    now = datetime.now(TAIPEI)
    d = today or now.date()
    deadline = now.replace(hour=settings.retry_until_hour, minute=settings.retry_until_minute,
                           second=0, microsecond=0)
    step = timedelta(minutes=settings.retry_every_min)
    adapter = TwseAdapter(settings)

    with repo.connect(settings.database_url) as conn:
        if repo.is_open_day(conn, d) is False:
            # 休市日曆上的日子（或今天已確認休市）：只確認一次，日曆有誤時也能把資料抓進來
            r = ingest_date(conn, adapter, settings, d, final=True, skip_done=True)
            log.info("%s 休市日曆上的休市日，只確認一次：%s", d, r.status)
            return

    while True:
        final = datetime.now(TAIPEI) + step > deadline  # 最後一次嘗試：查無資料就視為休市
        with repo.connect(settings.database_url) as conn:
            r = ingest_date(conn, adapter, settings, d, final=final, skip_done=True)
        log.info("%s 狀態：%s（final=%s）", d, r.status, final)
        if r.done or final:
            return
        time.sleep(step.total_seconds())


def catch_up(settings: Settings) -> None:
    """補抓最後一個成功日（不含）到昨天之間的日期。"""
    with repo.connect(settings.database_url) as conn:
        row = conn.execute(
            """SELECT max(r.target_date) FROM ingestion_runs r JOIN data_sources s ON s.id = r.source_id
                WHERE s.code = 'TWSE' AND r.dataset = 'MI_INDEX' AND r.status IN ('success', 'no_data')"""
        ).fetchone()
        last = row[0]
        yesterday = datetime.now(TAIPEI).date() - timedelta(days=1)
        if last is None:
            log.info("資料庫還沒有任何資料，請先執行 backfill；排程只負責之後每天的更新")
            return
        if last >= yesterday:
            return
        log.info("補抓 %s ~ %s", last + timedelta(days=1), yesterday)
        backfill(conn, TwseAdapter(settings), settings, last + timedelta(days=1), yesterday)


def due_today(settings: Settings, now: datetime) -> bool:
    """已經到了當天的排程時間（平日 18:30 之後）才抓今天；太早抓只會一直 pending。"""
    start_at = now.replace(hour=settings.schedule_hour, minute=settings.schedule_minute, second=0, microsecond=0)
    return now.weekday() < 5 and now >= start_at


COMPANY_REFRESH_DAYS = 7


def refresh_reference_data(settings: Settings, today: date | None = None) -> None:
    """除權息（重抓當月；月初也補上個月）、公司基本資料與休市日曆（每 7 天）。失敗只記錄，不影響行情。"""
    from radar.jobs.corporate import COMPANY, HOLIDAY, backfill_ex_rights, sync_companies, sync_holidays

    today = today or datetime.now(TAIPEI).date()
    adapter = TwseAdapter(settings)
    with repo.connect(settings.database_url) as conn:
        backfill_ex_rights(conn, adapter, settings, (today - timedelta(days=7)).replace(day=1), today, today)
        from radar.audit import check_corporate_actions
        for b in check_corporate_actions(conn, start=today - timedelta(days=60)):
            log.warning("除權息與成交價矛盾：%s %s 除權息 %s（生效 %s）%s，最高 %s／最低 %s，範圍 %.2f～%.2f",
                        b["symbol"], b["name"], b["ex_date"], b["eff_date"], b["problem"], b["high"], b["low"],
                        b["lower"], b["upper"])
        for dataset, sync in ((COMPANY, sync_companies), (HOLIDAY, sync_holidays)):
            last = conn.execute(
                """SELECT max(r.target_date) FROM ingestion_runs r JOIN data_sources s ON s.id = r.source_id
                    WHERE s.code = 'TWSE' AND r.dataset = %s AND r.status = 'success'""", (dataset,)
            ).fetchone()[0]
            if last is None or (today - last).days >= COMPANY_REFRESH_DAYS:
                sync(conn, adapter, settings, today)


def update_tpex(settings: Settings) -> None:
    """上櫃（櫃買中心 OpenAPI）：只提供最新一天，所以每次抓它目前的那一天（行情、三大法人、當天除權息），
    公司基本資料每 7 天更新。交易日曆以證交所為準；錯過的日子補不回來。失敗只記錄，不影響上市的流程。"""
    from radar.adapters.tpex import TpexAdapter
    from radar.jobs.corporate import COMPANY, ingest_ex_rights, sync_companies

    adapter = TpexAdapter(settings)
    latest = adapter.latest_date()
    with repo.connect(settings.database_url) as conn:
        if repo.is_open_day(conn, latest) is False:
            log.info("上櫃：%s 在證交所交易日曆上是休市日，略過", latest)
            return
        src = repo.source_id(conn, "TPEX")
        last_company = conn.execute(
            "SELECT max(target_date) FROM ingestion_runs WHERE source_id = %s AND dataset = %s AND status = 'success'",
            (src, COMPANY)).fetchone()[0]
        today = datetime.now(TAIPEI).date()
        if last_company is None or (today - last_company).days >= COMPANY_REFRESH_DAYS:
            sync_companies(conn, adapter, settings, today)           # 產業別：先有公司資料，新股票才有產業
        r = ingest_date(conn, adapter, settings, latest, final=False, skip_done=True)
        ingest_ex_rights(conn, adapter, settings, latest, latest)
    log.info("上櫃 %s：%s", latest, dict(r.status))


def update_analytics(settings: Settings) -> None:
    """特徵與訊號：從上次算到的日子往前 7 天重算到最新的行情日（涵蓋補抓的日子；重算結果相同）。"""
    from radar.features import build_features
    from radar.signals import generate_signals

    with repo.connect(settings.database_url) as conn:
        last_price, first_price = conn.execute("SELECT max(trade_date), min(trade_date) FROM daily_prices").fetchone()
        last_feature = conn.execute("SELECT max(trade_date) FROM daily_features").fetchone()[0]
        if last_price is None:
            return
        start = max(first_price, (last_feature or first_price) - timedelta(days=7))
        n = build_features(conn, start, last_price)
        counts = generate_signals(conn, start, last_price)
        from radar.outcomes import build_outcomes
        outcomes = build_outcomes(conn)          # 訊號回測：每一筆訊號之後的報酬（整批重算）
        from radar.audit import check_adjusted_returns
        for j in check_adjusted_returns(conn, start=start):
            log.warning("還原價可能有錯：%s %s %s 還原日報酬 %+.2f%%，超過漲跌幅", j["symbol"], j["name"],
                        j["trade_date"], float(j["ret_1d"]) * 100)
    log.info("特徵 %s～%s 共 %d 列；訊號 %s；回測 %s", start, last_price, n, dict(counts), outcomes)


def update_summary(settings: Settings) -> None:
    """最新交易日的盤後摘要：有 Claude API 金鑰用 AI，沒有就用模擬摘要（依規則組句）。"""
    from radar import summary
    with repo.connect(settings.database_url) as conn:
        d = conn.execute("SELECT max(trade_date) FROM market_breadth").fetchone()[0]
        if d is None:
            return
        if summary.has_credentials() and summary.generate_summary(conn, d):
            return
        summary.simulate(conn, d)


def export_latest(settings: Settings) -> None:
    """把最新交易日的網站存成一個 HTML 檔（data/exports），可以直接傳給別人看。"""
    from radar.web.export import export_day
    with repo.connect(settings.web_database_url) as conn:
        d = conn.execute("SELECT max(trade_date) FROM market_breadth").fetchone()[0]
        if d is not None:
            path = export_day(conn, d, settings.export_dir)
            log.info("已匯出 %s", path)


def _safe(step, settings: Settings, label: str) -> None:
    """輔助資料或後續計算失敗只記 log，不影響行情；下一次排程會再試。"""
    try:
        step(settings)
    except Exception:
        log.exception("%s失敗", label)


def daily_job(settings: Settings) -> None:
    """常駐排程（Docker）每天 18:30 執行的內容。休市日曆要先更新，run_today 才知道今天是否休市。"""
    _safe(refresh_reference_data, settings, "更新除權息／公司資料／休市日曆")
    run_today(settings)
    _safe(update_tpex, settings, "上櫃（櫃買中心）")
    _safe(update_analytics, settings, "計算特徵與訊號")
    _safe(update_summary, settings, "盤後摘要")
    _safe(export_latest, settings, "匯出 HTML 檔")


def wait_for_database(settings: Settings, timeout_sec: float = 180, every_sec: float = 10) -> None:
    """剛開機登入時 PostgreSQL 服務可能還在啟動；先等它，逾時才放棄（例外往上拋、記進紀錄檔）。"""
    import psycopg

    deadline = time.monotonic() + timeout_sec
    while True:
        try:
            repo.connect(settings.database_url).close()
            return
        except psycopg.OperationalError as e:
            if time.monotonic() + every_sec > deadline:
                raise
            log.info("資料庫還沒好（%s），%d 秒後再試", str(e).splitlines()[0][:80], every_sec)
            time.sleep(every_sec)


def run_daily(settings: Settings) -> None:
    """跑一次就結束，給 Windows 工作排程器用（不常駐、不占記憶體）。

    開機或登入時觸發也安全：先等資料庫、補抓漏掉的日子；還沒到 18:30 就不抓今天。
    """
    log.info("每日流程開始")
    wait_for_database(settings)
    catch_up(settings)
    _safe(refresh_reference_data, settings, "更新除權息／公司資料／休市日曆")
    now = datetime.now(TAIPEI)
    if due_today(settings, now):
        run_today(settings)
    else:
        log.info("還沒到今天的排程時間（%02d:%02d）或是週末，只做補抓", settings.schedule_hour, settings.schedule_minute)
    _safe(update_tpex, settings, "上櫃（櫃買中心）")
    _safe(update_analytics, settings, "計算特徵與訊號")
    _safe(update_summary, settings, "盤後摘要")
    _safe(export_latest, settings, "匯出 HTML 檔")
    log.info("每日流程結束")


def start(settings: Settings, *, catch_up_on_start: bool = True) -> None:
    if catch_up_on_start:
        catch_up(settings)
    sched = BlockingScheduler(timezone=TAIPEI)
    sched.add_job(
        daily_job, CronTrigger(day_of_week="mon-fri", hour=settings.schedule_hour,
                               minute=settings.schedule_minute, timezone=TAIPEI),
        args=[settings], id="daily_ingest", max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    log.info("排程已啟動：週一至週五 %02d:%02d（台北時間）", settings.schedule_hour, settings.schedule_minute)
    sched.start()
