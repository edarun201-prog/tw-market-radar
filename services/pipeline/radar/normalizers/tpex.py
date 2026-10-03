"""證券櫃檯買賣中心（上櫃）OpenAPI 的正規化：輸出和證交所相同的資料模型（market="TPEX"）。

欄位對照依 2026-10-02 的實際回應：
- 行情 tpex_mainboard_daily_close_quotes：Close／Change（"+1.27"、"-0.03 "）／Open／High／Low、TradingShares（股）、
  TransactionAmount（元）、TransactionNumber。只留普通股與 ETF（權證、債券等不收）。
- 三大法人 tpex_3insti_daily_trading（股）：外資用「外資及陸資合計」（含外資自營商，和證交所 T86 的算法一樣）、
  投信、自營商（合計）、三大法人合計。欄位名稱有的多空白，比對前先去掉空白。
- 除權息 tpex_exright_daily：權值＋息值、除權息前收盤、參考價、漲跌停、開盤競價基準、減除股利參考價。
- 公司基本資料 mopsfin_t187ap03_O：產業別只有代碼，名稱用證交所的同一套代碼對照（由 jobs/corporate.py 放進原始檔
  的 industry_names）。
"""
from __future__ import annotations

from datetime import date

from radar.adapters.base import RawPayload
from radar.adapters.tpex import roc_date
from radar.models import (CompanyProfile, CompanySnapshot, CorporateAction, CorporateActionPeriod, DailyQuote,
                          IndexQuote, InstitutionalDay, InstitutionalFlow, MarketBreadth, MarketDaySnapshot)
from radar.normalizers.common import NormalizeError, clean_str, parse_decimal, parse_int, security_type

OTC_INDEX = "櫃買指數"


def _key(k: str) -> str:
    return "".join(str(k).split())


def _get(row: dict, name: str):
    """欄位名稱忽略空白比對（OpenAPI 的欄位名稱有時多一個空白）。"""
    if name in row:
        return row[name]
    want = _key(name)
    for k, v in row.items():
        if _key(k) == want:
            return v
    return None


def _times(v, unit: int) -> int | None:
    n = parse_int(v)
    return n * unit if n is not None else None


def _on(rows: list[dict], d: date) -> list[dict]:
    return [r for r in rows if r.get("Date") and roc_date(r["Date"]) == d]


# ---- 行情、櫃買指數、市場現況 ----------------------------------------------------------
def normalize_market_day(p: RawPayload) -> tuple[MarketDaySnapshot, list[str]]:
    d, warnings = p.trade_date, []
    rows = _on(p.body.get("quotes") or [], d)
    if not rows:
        raise NormalizeError(f"上櫃行情裡沒有 {d} 的資料")
    quotes = []
    for r in rows:
        sym = clean_str(r.get("SecuritiesCompanyCode"))
        kind = security_type(sym)
        if kind not in ("stock", "etf"):
            continue
        quotes.append(DailyQuote(
            symbol=sym, name=clean_str(r.get("CompanyName")), security_type=kind,
            open=parse_decimal(r.get("Open")), high=parse_decimal(r.get("High")), low=parse_decimal(r.get("Low")),
            close=parse_decimal(r.get("Close")), change=parse_decimal(r.get("Change")), is_no_compare=False,
            volume=parse_int(r.get("TradingShares")) or 0, turnover=parse_int(r.get("TransactionAmount")) or 0,
            trade_count=parse_int(r.get("TransactionNumber"))))

    indices = []
    for r in _on(p.body.get("index") or [], d):
        close, change = parse_decimal(r.get("Close")), parse_decimal(r.get("Change"))
        pct = round(change / (close - change) * 100, 4) if close is not None and change is not None and close != change else None
        indices.append(IndexQuote(index_code=OTC_INDEX, close=close, change=change, change_pct=pct))
    if not indices:
        warnings.append(f"沒有 {d} 的櫃買指數")

    hl = _on(p.body.get("highlight") or [], d)
    if hl:
        h = hl[0]
        breadth = MarketBreadth(
            advancers=parse_int(h.get("PriceRiseCompanyNumbers")), decliners=parse_int(h.get("PriceDeclineCompanyNumbers")),
            unchanged=parse_int(h.get("PriceFlatCompanyNumbers")), limit_up=parse_int(h.get("LimitUpCompanyNumbers")),
            limit_down=parse_int(h.get("LimitDownCompanyNumbers")),
            no_trade=parse_int(h.get("UnmatchedCompanyNumbersSuspensionStocksIncluded")),
            # 市場現況的成交值單位是百萬元、成交量是千股（只算股票；2026-10-02 和逐筆加總對過）
            total_volume=_times(h.get("DailyTradingVolume"), 1_000), total_turnover=_times(h.get("DailyTradingValue"), 1_000_000),
            total_trades=sum(q.trade_count or 0 for q in quotes if q.security_type == "stock"))
    else:
        warnings.append(f"沒有 {d} 的上櫃市場現況（漲跌家數）")
        breadth = MarketBreadth(advancers=None, decliners=None, unchanged=None, limit_up=None, limit_down=None,
                                no_trade=None, total_volume=sum(q.volume for q in quotes),
                                total_turnover=sum(q.turnover for q in quotes), total_trades=None)
    return MarketDaySnapshot(market="TPEX", trade_date=d, quotes=quotes, indices=indices, breadth=breadth), warnings


# ---- 三大法人 -------------------------------------------------------------------------
_F = "ForeignInvestorsIncludeMainlandAreaInvestors"   # 外資及陸資合計（含外資自營商）
_T = "SecuritiesInvestmentTrustCompanies"


def normalize_institutional(p: RawPayload) -> tuple[InstitutionalDay, list[str]]:
    d = p.trade_date
    rows = _on(p.body.get("data") or [], d)
    if not rows:
        raise NormalizeError(f"上櫃三大法人裡沒有 {d} 的資料")
    if _get(rows[0], f"{_F}-Difference") is None or _get(rows[0], f"{_T}-Difference") is None:
        raise NormalizeError(f"上櫃三大法人找不到外資／投信欄位；實際欄位：{list(rows[0])}")
    flows = []
    for r in rows:
        sym = clean_str(r.get("SecuritiesCompanyCode"))
        if not sym:
            continue
        num = lambda name: parse_int(_get(r, name))  # noqa: E731
        f_net, t_net, d_net = num(f"{_F}-Difference"), num(f"{_T}-Difference"), num("Dealers-Difference")
        total = num("TotalDifference")
        if total is None and None not in (f_net, t_net, d_net):
            total = f_net + t_net + d_net
        flows.append(InstitutionalFlow(
            symbol=sym, name=clean_str(r.get("CompanyName")),
            foreign_buy=num(f"{_F}-TotalBuy"), foreign_sell=num(f"{_F}-TotalSell"), foreign_net=f_net,
            trust_buy=num(f"{_T}-TotalBuy"), trust_sell=num(f"{_T}-TotalSell"), trust_net=t_net,
            dealer_buy=num("Dealers-TotalBuy"), dealer_sell=num("Dealers-TotalSell"), dealer_net=d_net, total_net=total))
    return InstitutionalDay(market="TPEX", trade_date=d, flows=flows), []


# ---- 除權息 ---------------------------------------------------------------------------
_TYPES = {"除息": "dividend", "除權": "rights", "除權息": "both"}


def normalize_ex_rights(p: RawPayload, end: date) -> tuple[CorporateActionPeriod, list[str]]:
    start, warnings, actions = p.trade_date, [], []
    for r in p.body.get("data") or []:
        if not r.get("Date"):
            continue
        d = roc_date(r["Date"])
        if not (start <= d <= end):
            continue
        sym = clean_str(r.get("SecuritiesCompanyCode"))
        kind = _TYPES.get(clean_str(r.get("ExRightsDiviend")))
        prev, ref = parse_decimal(r.get("ClosePriceBeforeExRightsDiviend")), parse_decimal(r.get("ExRightsDiviendQuote"))
        value = parse_decimal(r.get("StockDividendPlusCashDividend"))
        if kind is None or prev is None or ref is None or value is None:
            warnings.append(f"{sym} {d} 除權息欄位不完整，略過")
            continue
        actions.append(CorporateAction(
            symbol=sym, name=clean_str(r.get("CompanyName")), ex_date=d, action_type=kind, prev_close=prev,
            ref_price=ref, value=value, limit_up=parse_decimal(r.get("LimitUp")), limit_down=parse_decimal(r.get("LimitDown")),
            open_ref=parse_decimal(r.get("OpeningReferencePrice")), div_ref=parse_decimal(r.get("DividendDeductedQuote"))))
    return CorporateActionPeriod(market="TPEX", start=start, end=end, actions=actions), warnings


# ---- 公司基本資料 ------------------------------------------------------------------------
def normalize_company_profiles(p: RawPayload) -> tuple[CompanySnapshot, list[str]]:
    names: dict[str, str] = p.body.get("industry_names") or {}
    warnings, companies = [], []
    for r in p.body.get("data") or []:
        sym = clean_str(r.get("SecuritiesCompanyCode"))
        if security_type(sym) != "stock":
            continue
        code = clean_str(r.get("SecuritiesIndustryCode")).zfill(2)
        listed = None
        raw_listed = clean_str(r.get("DateOfListing"))
        if raw_listed:
            try:
                listed = roc_date(raw_listed)
            except (ValueError, IndexError):
                warnings.append(f"{sym} 上櫃日期看不懂：{raw_listed}")
        companies.append(CompanyProfile(
            symbol=sym, name=clean_str(r.get("CompanyAbbreviation")) or clean_str(r.get("CompanyName")),
            industry_code=code, industry_name=names.get(code) or f"其他（{code}）", listed_date=listed))
    return CompanySnapshot(market="TPEX", as_of=p.trade_date, companies=companies), warnings
