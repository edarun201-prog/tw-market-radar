"""資料庫整合測試：需要 TEST_DATABASE_URL（會清空該資料庫的 public schema）。"""
from datetime import date

from radar.jobs.backfill import backfill
from radar.jobs.ingest import ingest_date

from .conftest import FakeAdapter, load_fixture, shift_date

WED, THU, FRI, SAT = date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 26)


def _adapter(days=(WED,)):
    mi, t86 = load_fixture("twse_mi_index_20260923.json"), load_fixture("twse_t86_20260923.json")
    return FakeAdapter(market={d: shift_date(mi, d) for d in days}, inst={d: shift_date(t86, d) for d in days})


def _count(conn, table, d=None):
    if d:
        return conn.execute(f"SELECT count(*) FROM {table} WHERE trade_date=%s", (d,)).fetchone()[0]
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_ingest_writes_everything(conn, settings):
    r = ingest_date(conn, _adapter(), settings, WED)
    assert r.status == {"MI_INDEX": "success", "T86": "success"}
    assert _count(conn, "daily_prices", WED) == 5
    assert _count(conn, "institutional_flows", WED) == 3
    assert _count(conn, "market_indices", WED) == 2
    assert conn.execute("SELECT advancers, total_turnover FROM market_breadth WHERE trade_date=%s",
                        (WED,)).fetchone() == (612, 480_123_456_789)
    assert conn.execute("SELECT is_open FROM trading_calendar WHERE trade_date=%s", (WED,)).fetchone()[0] is True
    row = conn.execute("""SELECT p.close, p.volume, f.foreign_net FROM daily_prices p
                          JOIN stocks s ON s.id = p.stock_id
                          JOIN institutional_flows f ON f.stock_id = p.stock_id AND f.trade_date = p.trade_date
                          WHERE s.symbol='2330' AND p.trade_date=%s""", (WED,)).fetchone()
    assert (float(row[0]), row[1], row[2]) == (1010.0, 45_210_000, 4_000_000)
    assert (settings.raw_dir / "TWSE" / "MI_INDEX" / "2026-09-23.json.gz").exists()


def test_rerun_is_idempotent(conn, settings):
    a = _adapter()
    ingest_date(conn, a, settings, WED)
    ingest_date(conn, a, settings, WED)
    assert _count(conn, "daily_prices") == 5
    assert _count(conn, "institutional_flows") == 3
    assert _count(conn, "stocks") == 5
    assert conn.execute("SELECT attempts FROM ingestion_runs WHERE dataset='MI_INDEX'").fetchone()[0] == 2


def test_no_data_final_marks_holiday(conn, settings):
    r = ingest_date(conn, _adapter(days=()), settings, WED, final=True)
    assert r.status == {"MI_INDEX": "no_data", "T86": "skipped"}
    assert conn.execute("SELECT is_open FROM trading_calendar WHERE trade_date=%s", (WED,)).fetchone()[0] is False


def test_no_data_not_final_is_pending(conn, settings):
    r = ingest_date(conn, _adapter(days=()), settings, WED, final=False)
    assert r.status["MI_INDEX"] == "pending" and not r.done
    assert conn.execute("SELECT count(*) FROM trading_calendar WHERE trade_date=%s", (WED,)).fetchone()[0] == 0


def test_t86_pending_then_success(conn, settings):
    mi = load_fixture("twse_mi_index_20260923.json")
    a = FakeAdapter(market={WED: mi})                     # 法人資料還沒出來
    r1 = ingest_date(conn, a, settings, WED, final=False)
    assert r1.status == {"MI_INDEX": "success", "T86": "pending"}
    a.inst[WED] = load_fixture("twse_t86_20260923.json")  # 20 分鐘後公布了
    r2 = ingest_date(conn, a, settings, WED, final=False, skip_done=True)
    assert r2.status == {"MI_INDEX": "skipped", "T86": "success"}


def test_fetch_error_recorded(conn, settings):
    r = ingest_date(conn, FakeAdapter(fail_on={WED}), settings, WED)
    assert r.status["MI_INDEX"] == "failed"
    status, err = conn.execute("SELECT status, error FROM ingestion_runs WHERE dataset='MI_INDEX'").fetchone()
    assert status == "failed" and "模擬網路錯誤" in err


def test_validation_failure_writes_nothing(conn, settings):
    from dataclasses import replace
    strict = replace(settings, min_quote_rows=800)
    r = ingest_date(conn, _adapter(), strict, WED)
    assert r.status["MI_INDEX"] == "failed"
    assert _count(conn, "daily_prices") == 0
    assert "低於下限" in conn.execute("SELECT error FROM ingestion_runs WHERE dataset='MI_INDEX'").fetchone()[0]


def test_backfill_resumes_and_skips_weekend(conn, settings):
    a = _adapter(days=(WED, THU))                         # FRI 沒有資料 → 視為休市
    totals = backfill(conn, a, settings, WED, SAT)
    assert totals["MI_INDEX:success"] == 2 and totals["MI_INDEX:no_data"] == 1
    assert ("MI_INDEX", SAT) not in a.calls               # 週末不發請求
    assert _count(conn, "daily_prices") == 10

    a.calls.clear()
    backfill(conn, a, settings, WED, SAT)                 # 續跑：已完成的日期不再請求
    assert a.calls == []


def test_reprocess_from_raw(conn, settings):
    ingest_date(conn, _adapter(), settings, WED)
    conn.execute("DELETE FROM institutional_flows")
    r = ingest_date(conn, FakeAdapter(), settings, WED, from_raw=True)
    assert r.status == {"MI_INDEX": "success", "T86": "success"}
    assert _count(conn, "institutional_flows") == 3
