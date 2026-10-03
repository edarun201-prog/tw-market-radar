"""爬蟲的驗收測試：用「真實回應」檢查解析結果。

怎麼用：
  1. 寫好 adapter 的 fetch_*，用 probe 抓一份真實回應存成測試資料：
       python -m radar probe --dataset TWT49U --date 2026-07-01 --save-fixture
       python -m radar probe --dataset COMPANY --date 2026-09-25 --save-fixture
  2. 跑 pytest tests/test_crawler_contract.py。還沒有測試資料的項目會略過；
     有資料但解析還沒寫好或寫錯，就會失敗。
  3. 到證交所網站查一兩筆，填進下面的 KNOWN_* 當作對照答案。
"""
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from radar.jobs.corporate import month_ranges
from radar.normalizers.twse import normalize_company_profiles, normalize_ex_rights
from radar.validators import validate_company_profiles, validate_ex_rights

from .conftest import FIXTURES, load_fixture, payload

EX_FIXTURES = sorted(FIXTURES.glob("twse_twt49u_*.json"))
COMPANY_FIXTURES = sorted(FIXTURES.glob("twse_company_*.json"))

# 從證交所以外的來源查到的正確答案：(代號, 除權息日) → 每股配發金額（元）
# 官方的權值+息值會有極小的計算尾數（例：6.000035），比對時容許 0.01 元誤差。
KNOWN_EX_RIGHTS: dict[tuple[str, date], Decimal] = {
    ("2330", date(2026, 6, 11)): Decimal("6.0"),   # 中央社 2026-06-10〈台積電現金股利6元11日除息〉
    ("2412", date(2026, 7, 9)): Decimal("5.2"),    # 財報狗：中華電 2025 年度現金股利 5.2 元
}


def _month(path: Path) -> tuple[date, date]:
    ym = path.stem.rsplit("_", 1)[1]                       # twse_twt49u_202607 → 202607
    first = date(int(ym[:4]), int(ym[4:]), 1)
    return month_ranges(first, first + timedelta(days=31))[0]


@pytest.mark.skipif(not EX_FIXTURES, reason="還沒有除權息測試資料：用 probe --dataset TWT49U --save-fixture 存一份")
@pytest.mark.parametrize("path", EX_FIXTURES, ids=lambda p: p.stem)
def test_ex_rights_contract(path):
    start, end = _month(path)
    period, warnings = normalize_ex_rights(payload("TWT49U", start, load_fixture(path.name)), end)

    assert period.actions, "解析結果是空的"
    check, kept = validate_ex_rights(period)
    assert check.ok, check.errors
    assert len(kept.actions) == len(period.actions)
    # 前收盤 − 參考價 ≈ 權值+息值：欄位對錯位置時這條一定不成立
    mismatch = [w for w in check.warnings if "≠" in w]
    assert len(mismatch) <= max(1, len(period.actions) // 50), mismatch[:5]
    assert all(a.symbol and a.name for a in period.actions)
    assert {a.action_type for a in period.actions} <= {"dividend", "rights", "both"}

    found = {(a.symbol, a.ex_date): a.value for a in period.actions}
    for key, value in KNOWN_EX_RIGHTS.items():
        if start <= key[1] <= end:
            assert key in found and abs(found[key] - value) < Decimal("0.01"), f"{key} 應為 {value}，解析出 {found.get(key)}"


def test_known_answers_are_covered_by_fixtures():
    months = [_month(p) for p in EX_FIXTURES]
    for symbol, d in KNOWN_EX_RIGHTS:
        assert any(s <= d <= e for s, e in months), f"{symbol} {d} 沒有對應月份的測試資料，對照答案不會被檢查"


@pytest.mark.skipif(not COMPANY_FIXTURES, reason="還沒有公司資料測試資料：用 probe --dataset COMPANY --save-fixture 存一份")
@pytest.mark.parametrize("path", COMPANY_FIXTURES, ids=lambda p: p.stem)
def test_company_profiles_contract(path):
    as_of = date.fromisoformat(f"{path.stem[-8:-4]}-{path.stem[-4:-2]}-{path.stem[-2:]}")
    snap, warnings = normalize_company_profiles(payload("COMPANY", as_of, load_fixture(path.name)))

    check, kept = validate_company_profiles(snap)
    assert check.ok, check.errors
    c = {x.symbol: x for x in kept.companies}
    assert "半導體" in c["2330"].industry_name        # 台積電
    assert "金融" in c["2881"].industry_name          # 富邦金
    assert c["2330"].name == "台積電"                  # 用簡稱，與行情表一致
    assert all(x.industry_code.isdigit() for x in kept.companies)
