"""入庫前的檢查。

- 系統性問題（筆數太少、與前一交易日差距過大）→ 整批失敗、不寫入。
- 個別列的小問題（高低價矛盾、買賣超對不上）→ 該列剔除或只記警告，超過 1% 才整批失敗。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from radar.config import Settings
from radar.models import (
    CompanySnapshot, CorporateAction, CorporateActionPeriod, DailyQuote, InstitutionalDay, MarketDaySnapshot,
)

PRICE_LIMIT = Decimal("0.101")  # 台股漲跌幅 10%（新上市前 5 日、部分境外 ETF 無限制，所以只記警告）
MAX_BAD_ROW_RATIO = 0.01
EX_VALUE_TOLERANCE = Decimal("0.01")  # 權值+息值 與 (前收盤 − 參考價) 的差距，超過前收盤的 1% 記警告
MIN_COMPANIES = 500                   # 上市公司約 1,000 家，少於此數視為抓取不完整


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _row_problem(q: DailyQuote) -> str | None:
    if q.volume < 0 or q.turnover < 0:
        return "成交量或金額為負"
    prices = [q.open, q.high, q.low, q.close]
    if all(p is None for p in prices):
        return None  # 當日無成交，合理
    if any(p is None for p in prices):
        return "開高低收部分缺漏"
    if q.high < q.low or not (q.low <= q.open <= q.high) or not (q.low <= q.close <= q.high):
        return "開高低收不一致"
    return None


def validate_market_day(s: MarketDaySnapshot, settings: Settings, prev_count: int | None) -> tuple[ValidationResult, MarketDaySnapshot]:
    r = ValidationResult(ok=True)
    n = len(s.quotes)

    if n < settings.min_quote_rows:
        r.errors.append(f"行情只有 {n} 筆，低於下限 {settings.min_quote_rows}")
    if prev_count and abs(n - prev_count) / prev_count > settings.max_row_change_ratio:
        r.errors.append(f"行情筆數 {n} 與前一交易日 {prev_count} 差距過大")
    if len({q.symbol for q in s.quotes}) != n:
        r.errors.append("行情中有重複的證券代號")

    good: list[DailyQuote] = []
    bad = 0
    for q in s.quotes:
        problem = _row_problem(q)
        if problem:
            bad += 1
            r.warnings.append(f"{q.symbol} 剔除：{problem}")
            continue
        if q.close is not None and q.change is not None:
            prev_close = q.close - q.change
            if prev_close > 0 and abs(q.change) / prev_close > PRICE_LIMIT:
                r.warnings.append(f"{q.symbol} 漲跌幅超過 10%（可能是新上市或無漲跌幅限制的 ETF）")
        good.append(q)

    if n and bad / n > MAX_BAD_ROW_RATIO:
        r.errors.append(f"有 {bad} 筆資料異常，超過 {MAX_BAD_ROW_RATIO:.0%}")
    if not s.indices:
        r.warnings.append("沒有指數資料")

    r.ok = not r.errors
    return r, s.model_copy(update={"quotes": good})


def validate_institutional(d: InstitutionalDay, settings: Settings, prev_count: int | None) -> ValidationResult:
    r = ValidationResult(ok=True)
    n = len(d.flows)
    if n < settings.min_flow_rows:
        r.errors.append(f"法人資料只有 {n} 筆，低於下限 {settings.min_flow_rows}")
    if prev_count and abs(n - prev_count) / prev_count > settings.max_row_change_ratio:
        r.errors.append(f"法人資料筆數 {n} 與前一交易日 {prev_count} 差距過大")
    if len({f.symbol for f in d.flows}) != n:
        r.errors.append("法人資料中有重複的證券代號")

    for f in d.flows:
        for label, buy, sell, net in (
            ("外資", f.foreign_buy, f.foreign_sell, f.foreign_net),
            ("投信", f.trust_buy, f.trust_sell, f.trust_net),
            ("自營商", f.dealer_buy, f.dealer_sell, f.dealer_net),
        ):
            if None not in (buy, sell, net) and buy - sell != net:
                r.warnings.append(f"{f.symbol} {label}買進－賣出 ≠ 買賣超")
    r.ok = not r.errors
    return r


# ---- 除權息 ------------------------------------------------------------------
def _action_problem(a: CorporateAction) -> str | None:
    # 權值+息值可以是負的：現金增資認購價高於市價時，參考價會高於前收盤（官方公式見 TWT49U 附註）
    if a.prev_close <= 0 or a.ref_price <= 0:
        return "前收盤或參考價不是正數"
    return None


def validate_ex_rights(p: CorporateActionPeriod) -> tuple[ValidationResult, CorporateActionPeriod]:
    r = ValidationResult(ok=True)
    n = len(p.actions)
    if len({(a.symbol, a.ex_date) for a in p.actions}) != n:
        r.errors.append("除權息資料中有重複的（證券代號, 日期）")
    outside = [a for a in p.actions if not (p.start <= a.ex_date <= p.end)]
    if outside:
        r.errors.append(f"有 {len(outside)} 筆日期不在查詢區間 {p.start}～{p.end}，例：{outside[0].symbol} {outside[0].ex_date}")

    good: list[CorporateAction] = []
    bad = 0
    for a in p.actions:
        problem = _action_problem(a)
        if problem:
            bad += 1
            r.warnings.append(f"{a.symbol} {a.ex_date} 剔除：{problem}")
            continue
        # 官方定義：權值+息值 = 除權息前收盤價 − 除權息參考價；欄位對錯位置時這條一定不成立
        if abs(a.prev_close - a.ref_price - a.value) > a.prev_close * EX_VALUE_TOLERANCE:
            r.warnings.append(f"{a.symbol} {a.ex_date} 權值+息值 {a.value} ≠ 前收盤 − 參考價 {a.prev_close - a.ref_price}")
        good.append(a)

    if n and bad / n > MAX_BAD_ROW_RATIO:
        r.errors.append(f"有 {bad} 筆除權息資料異常，超過 {MAX_BAD_ROW_RATIO:.0%}")
    r.ok = not r.errors
    return r, p.model_copy(update={"actions": good})


# ---- 公司基本資料 -------------------------------------------------------------
def validate_company_profiles(s: CompanySnapshot) -> tuple[ValidationResult, CompanySnapshot]:
    r = ValidationResult(ok=True)
    n = len(s.companies)
    if n < MIN_COMPANIES:
        r.errors.append(f"公司資料只有 {n} 筆，低於下限 {MIN_COMPANIES}")
    if len({c.symbol for c in s.companies}) != n:
        r.errors.append("公司資料中有重複的證券代號")
    good = []
    for c in s.companies:
        if not c.industry_code or not c.industry_name:
            r.warnings.append(f"{c.symbol} 剔除：缺少產業別")
            continue
        good.append(c)
    if n and (n - len(good)) / n > MAX_BAD_ROW_RATIO:
        r.errors.append(f"有 {n - len(good)} 家缺少產業別，超過 {MAX_BAD_ROW_RATIO:.0%}")
    r.ok = not r.errors
    return r, s.model_copy(update={"companies": good})
