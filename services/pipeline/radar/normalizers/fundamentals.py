"""估值與財報（證交所、櫃買中心 OpenAPI）→ 標準格式。

- 估值：上市 BWIBBU_ALL（Code、PEratio、DividendYield、PBratio）、上櫃 tpex_mainboard_peratio_analysis
  （SecuritiesCompanyCode、PriceEarningRatio、YieldRatio、PriceBookRatio）。本益比空白或 ≤ 0（虧損）存 None。
- 財報：綜合損益表（t187ap06），上市的代號欄位是中文（公司代號、年度、季別），上櫃是英文（SecuritiesCompanyCode、
  Year、Season），金額欄位兩邊都是中文。數字是「年度累計」，金額單位千元，這裡換成元。
  營業收入的欄位名稱依產業不同（一般業、保險：營業收入；證券：收益；異業：收入）。銀行沒有這一欄；金控的「淨收益」
  和其他收益欄位對不起來（例：國泰金上半年只有 40 億），意思不明確，所以銀行與金控的營業收入存 None，不猜。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from radar.adapters.base import RawPayload
from radar.adapters.tpex import roc_date
from radar.normalizers.common import NormalizeError, clean_str, parse_decimal

REVENUE_KEY = {"ci": "營業收入", "ins": "營業收入", "bd": "收益", "mim": "收入"}
PARENT_NET = re.compile(r"淨利（(淨)?損）歸屬於母公司業主")
THOUSAND = Decimal(1000)


@dataclass(frozen=True)
class Valuation:
    symbol: str
    pe_ratio: Decimal | None
    dividend_yield: Decimal | None
    pb_ratio: Decimal | None


@dataclass
class ValuationDay:
    trade_date: date
    rows: list[Valuation]


@dataclass(frozen=True)
class FinancialReport:
    symbol: str
    fiscal_year: int        # 西元
    quarter: int
    report_type: str
    revenue: Decimal | None     # 元，年度累計
    net_income: Decimal | None  # 元，年度累計（歸屬於母公司業主）
    eps: Decimal | None         # 元，年度累計
    published_on: date | None


def _first(r: dict, *keys: str) -> str:
    return next((clean_str(r[k]) for k in keys if clean_str(r.get(k))), "")


def normalize_valuations(p: RawPayload) -> tuple[ValuationDay, list[str]]:
    rows = p.body.get("data") or []
    out, warnings, dates = [], [], set()
    for r in rows:
        symbol = _first(r, "Code", "SecuritiesCompanyCode")
        if not symbol or not r.get("Date"):
            warnings.append(f"缺少代號或日期：{str(r)[:80]}")
            continue
        dates.add(roc_date(r["Date"]))
        pe = parse_decimal(r.get("PEratio", r.get("PriceEarningRatio")))
        out.append(Valuation(symbol, pe if pe is not None and pe > 0 else None,
                             parse_decimal(r.get("DividendYield", r.get("YieldRatio"))),
                             parse_decimal(r.get("PBratio", r.get("PriceBookRatio")))))
    if not out:
        raise NormalizeError("估值資料是空的")
    if len(dates) > 1:
        warnings.append(f"資料裡有多個日期 {sorted(d.isoformat() for d in dates)}，以最新的為準")
    return ValuationDay(max(dates), out), warnings


def normalize_financial_reports(p: RawPayload) -> tuple[list[FinancialReport], list[str]]:
    out: dict[str, FinancialReport] = {}
    warnings = []
    for kind, rows in p.body.items():
        for r in rows or []:
            symbol = _first(r, "公司代號", "SecuritiesCompanyCode")
            year, quarter = _first(r, "年度", "Year"), _first(r, "季別", "Season")
            if not symbol or not year.isdigit() or quarter not in ("1", "2", "3", "4"):
                warnings.append(f"{kind} 缺少代號、年度或季別：{str(r)[:80]}")
                continue
            if symbol in out:
                warnings.append(f"{symbol} 同時出現在兩種產業格式，保留第一筆")
                continue
            revenue = parse_decimal(r.get(REVENUE_KEY[kind])) if kind in REVENUE_KEY else None
            net = next((parse_decimal(v) for k, v in r.items() if PARENT_NET.fullmatch(k)), None)
            published = _first(r, "出表日期", "Date")
            out[symbol] = FinancialReport(
                symbol=symbol, fiscal_year=int(year) + (1911 if int(year) < 1911 else 0), quarter=int(quarter),
                report_type=kind, revenue=revenue * THOUSAND if revenue is not None else None,
                net_income=net * THOUSAND if net is not None else None,
                eps=parse_decimal(r.get("基本每股盈餘（元）")), published_on=roc_date(published) if published else None)
    if not out:
        raise NormalizeError("綜合損益表是空的")
    return list(out.values()), warnings
