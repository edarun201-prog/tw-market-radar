"""只下載原始檔（不需資料庫）。"""
from datetime import date

from radar import raw_store
from radar.jobs.prefetch import prefetch

from .conftest import FakeAdapter, load_fixture, shift_date

WED, THU, FRI, SAT = date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 26)


def _adapter(**kw):
    mi, t86 = load_fixture("twse_mi_index_20260923.json"), load_fixture("twse_t86_20260923.json")
    return FakeAdapter(market={WED: shift_date(mi, WED)}, inst={WED: shift_date(t86, WED)}, **kw)


def test_prefetch_saves_raw_and_skips_t86_on_closed_days(settings):
    a = _adapter(fail_on={FRI})
    totals = prefetch(a, settings, WED, SAT)

    assert totals == {"fetched": 1, "closed": 1, "failed": 1}
    assert ("MI_INDEX", SAT) not in a.calls                  # 週末不發請求
    assert ("T86", THU) not in a.calls                       # 休市日不抓法人
    assert raw_store.load(settings.raw_dir, "TWSE", "T86", WED).has_data
    assert not raw_store.load(settings.raw_dir, "TWSE", "MI_INDEX", THU).has_data
    assert not raw_store.raw_path(settings.raw_dir, "TWSE", "MI_INDEX", FRI).exists()  # 失敗不存檔


def test_prefetch_rerun_uses_cache(settings):
    prefetch(_adapter(), settings, WED, THU)
    a = _adapter()
    totals = prefetch(a, settings, WED, THU)
    assert a.calls == []
    assert totals == {"cached": 1, "closed": 1}
