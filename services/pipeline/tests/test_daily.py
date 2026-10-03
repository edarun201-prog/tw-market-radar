"""daily 指令的時間判斷（不需資料庫）：開機時觸發也不能一整天卡在 pending 重試。"""
from datetime import datetime

from radar.config import TAIPEI
from radar.jobs.scheduler import due_today


def _at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=TAIPEI)


def test_due_after_schedule_on_weekday(settings):
    assert due_today(settings, _at(2026, 9, 24, 18, 30))
    assert due_today(settings, _at(2026, 9, 24, 23, 0))


def test_not_due_before_schedule_or_on_weekend(settings):
    assert not due_today(settings, _at(2026, 9, 24, 8, 0))     # 早上開機：只補抓，不抓今天
    assert not due_today(settings, _at(2026, 9, 26, 19, 0))    # 週六
