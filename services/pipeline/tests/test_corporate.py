"""除權息與公司基本資料的流程、驗證、入庫。

爬蟲與解析（adapter.fetch_*、normalize_*）用假的代替，所以這裡在爬蟲寫好之前就能通過；
爬蟲本身的測試在 test_crawler_contract.py。
"""
from datetime import date
from decimal import Decimal

from radar.adapters.base import FetchError
from radar.jobs import corporate
from radar.models import CompanyProfile, CompanySnapshot, CorporateAction, CorporateActionPeriod
from radar.normalizers import twse as normalizers
from radar.validators import validate_company_profiles, validate_ex_rights

from .conftest import payload

JUL1, JUL31, AUG1, AUG31, SEP1, SEP25 = (date(2026, 7, 1), date(2026, 7, 31), date(2026, 8, 1),
                                         date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 25))


def _act(symbol, ex, prev="100", ref="95", value="5", kind="dividend", name=None):
    return CorporateAction(symbol=symbol, name=name or f"股{symbol}", ex_date=ex, action_type=kind,
                           prev_close=Decimal(prev), ref_price=Decimal(ref), value=Decimal(value))


class FakeCorpAdapter:
    source_code = "TWSE"

    def __init__(self, months=None, fail=False):
        self.months = months or {}      # 月初 → list[CorporateAction]
        self.fail = fail
        self.calls = []

    def fetch_ex_rights(self, start, end):
        self.calls.append(start)
        if self.fail:
            raise FetchError("模擬網路錯誤")
        body = {"stat": "OK" if self.months.get(start) else "很抱歉，沒有符合條件的資料!", "month": start.isoformat()}
        return payload("TWT49U", start, body)

    def fetch_company_profiles(self, as_of):
        return payload("COMPANY", as_of, {"stat": "OK"})


def _fake_normalize(adapter):
    def normalize(p, end):
        return CorporateActionPeriod(market="TWSE", start=p.trade_date, end=end,
                                     actions=adapter.months[p.trade_date]), []
    return normalize


def _rows(conn):
    return conn.execute("""SELECT s.symbol, c.ex_date, c.value FROM corporate_actions c
                           JOIN stocks s ON s.id = c.stock_id ORDER BY 1, 2""").fetchall()


# ---- 不需資料庫 -----------------------------------------------------------------
def test_month_ranges_clip_to_bounds():
    assert corporate.month_ranges(date(2026, 7, 15), date(2026, 9, 10)) == [
        (date(2026, 7, 15), JUL31), (AUG1, AUG31), (SEP1, date(2026, 9, 10))]


def test_validate_ex_rights_flags_mismatch_and_out_of_range():
    p = CorporateActionPeriod(market="TWSE", start=JUL1, end=JUL31, actions=[
        _act("2330", date(2026, 7, 16), "1000", "995", "5"),          # 正常
        _act("2317", date(2026, 7, 17), "200", "190", "3"),           # 200−190=10，與 3 差太多 → 警告
        _act("1101", date(2026, 7, 18), "0", "0", "1"),               # 價格不是正數 → 剔除
        _act("1312", date(2026, 7, 20), "13.20", "13.40", "-0.20", kind="rights"),  # 現金增資：負值合理
    ])
    r, kept = validate_ex_rights(p)
    assert [a.symbol for a in kept.actions] == ["2330", "2317", "1312"]
    assert not any("1312" in w for w in r.warnings)
    assert any("2317" in w and "≠" in w for w in r.warnings)
    assert not r.ok                                                   # 3 筆壞 1 筆，超過 1%

    r2, _ = validate_ex_rights(p.model_copy(update={"actions": [_act("2330", AUG1)]}))
    assert not r2.ok and "不在查詢區間" in r2.errors[0]


def test_validate_company_profiles_requires_enough_rows():
    few = CompanySnapshot(market="TWSE", as_of=SEP25, companies=[
        CompanyProfile(symbol="2330", name="台積電", industry_code="24", industry_name="半導體業",
                       listed_date=date(1994, 9, 5))])
    r, _ = validate_company_profiles(few)
    assert not r.ok and "低於下限" in r.errors[0]


# ---- 資料庫 ---------------------------------------------------------------------
def test_ex_rights_backfill_skips_finished_months_and_refreshes_current(conn, settings, monkeypatch):
    a = FakeCorpAdapter({JUL1: [_act("2330", date(2026, 7, 16), "1000", "995", "5")],
                         SEP1: [_act("2881", date(2026, 9, 3), "80", "78.5", "1.5")]})
    monkeypatch.setattr(normalizers, "normalize_ex_rights", _fake_normalize(a))

    totals = corporate.backfill_ex_rights(conn, a, settings, JUL1, SEP25, today=SEP25)
    assert totals == {"success": 2, "no_data": 1}                     # 8 月沒有除權息
    assert [(s, str(v)) for s, _, v in _rows(conn)] == [("2330", "5.0000"), ("2881", "1.5000")]
    assert (settings.raw_dir / "TWSE" / "TWT49U" / "2026-07-01.json.gz").exists()

    a.calls.clear()
    a.months[SEP1] = [_act("2882", date(2026, 9, 10), "60", "58", "2")]   # 9 月資料更新：2881 消失、2882 新增
    corporate.backfill_ex_rights(conn, a, settings, JUL1, SEP25, today=SEP25)
    assert a.calls == [SEP1]                                           # 7、8 月已過完且成功，不再請求
    assert [s for s, _, _ in _rows(conn)] == ["2330", "2882"]          # 當月重抓，消失的列會移除


def test_not_implemented_is_recorded_not_raised(conn, settings):
    class NotYet(FakeCorpAdapter):
        def fetch_ex_rights(self, start, end):
            raise NotImplementedError("TODO")

    assert corporate.ingest_ex_rights(conn, NotYet(), settings, JUL1, JUL31) == "failed"
    status, err = conn.execute("SELECT status, error FROM ingestion_runs WHERE dataset='TWT49U'").fetchone()
    assert status == "failed" and "尚未實作" in err


def test_fetch_error_writes_nothing(conn, settings):
    assert corporate.ingest_ex_rights(conn, FakeCorpAdapter(fail=True), settings, JUL1, JUL31) == "failed"
    assert _rows(conn) == []


def test_sync_companies_sets_industry(conn, settings, monkeypatch):
    companies = [CompanyProfile(symbol=f"{1000 + i}", name=f"公司{i}", industry_code="24", industry_name="半導體業",
                                listed_date=None) for i in range(599)]
    companies.append(CompanyProfile(symbol="2330", name="台積電", industry_code="24", industry_name="半導體業",
                                    listed_date=date(1994, 9, 5)))
    snap = CompanySnapshot(market="TWSE", as_of=SEP25, companies=companies)
    monkeypatch.setattr(normalizers, "normalize_company_profiles", lambda p: (snap, []))

    assert corporate.sync_companies(conn, FakeCorpAdapter(), settings, SEP25) == "success"
    assert corporate.sync_companies(conn, FakeCorpAdapter(), settings, SEP25) == "success"   # 重跑不重複
    row = conn.execute("""SELECT i.code, i.name, s.listed_date FROM stocks s JOIN industries i ON i.id = s.industry_id
                          WHERE s.symbol = '2330'""").fetchone()
    assert row == ("24", "半導體業", date(1994, 9, 5))
    assert conn.execute("SELECT count(*) FROM industries").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM stocks").fetchone()[0] == 600


# ---- 休市日曆 -------------------------------------------------------------------
def test_normalize_holidays_excludes_trading_days():
    from .conftest import load_fixture
    days, warnings = normalizers.normalize_holidays(payload("HOLIDAY", SEP25, load_fixture("twse_holiday_2026.json")))
    closed = dict(days)
    assert warnings == []
    assert date(2026, 1, 1) in closed and date(2026, 9, 25) in closed          # 元旦、中秋
    assert date(2026, 2, 12) in closed                                          # 市場無交易，僅辦理結算交割
    assert date(2026, 1, 2) not in closed and date(2026, 2, 11) not in closed   # 開始交易日、最後交易日
    assert date(2026, 2, 23) not in closed                                      # 春節後開始交易日


def test_mark_known_holidays_keeps_observed_days(conn):
    from radar import repository as repo
    repo.set_calendar(conn, date(2026, 9, 28), True, "實際有開市")               # 已由實際資料決定
    n = repo.mark_known_holidays(conn, [(date(2026, 9, 24), "過去"), (date(2026, 9, 28), "教師節"),
                                        (date(2026, 10, 9), "國慶日")], today=SEP25)
    assert n == 2                                                              # 過去的日子不寫
    assert repo.is_open_day(conn, date(2026, 9, 28)) is True                   # 不覆蓋實際資料
    assert repo.is_open_day(conn, date(2026, 10, 9)) is False
    assert repo.is_open_day(conn, date(2026, 9, 24)) is None
