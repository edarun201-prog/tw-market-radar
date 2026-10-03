"""TWSE 盤後報表 → 統一資料模型。

表格與欄位用「關鍵字」定位，不依賴固定順序；找不到時丟出 NormalizeError，
訊息會列出實際的標題與欄位，照著改關鍵字即可。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

from radar.adapters.base import RawPayload
from radar.models import (
    CompanyProfile, CompanySnapshot, CorporateAction, CorporateActionPeriod, DailyQuote, IndexQuote,
    InstitutionalDay, InstitutionalFlow, MarketBreadth, MarketDaySnapshot,
)
from radar.normalizers.common import (
    NormalizeError, clean_str, col, cols, find_tables, iter_tables, parse_decimal, parse_int,
    parse_roc_date, parse_sign, parse_ymd, require_table, security_type, sum_cols,
)

_COUNT_WITH_LIMIT = re.compile(r"([\d,]+)\s*\(\s*([\d,]+)\s*\)")


def _check_date(p: RawPayload) -> None:
    raw = p.body.get("date")
    if raw and parse_ymd(str(raw)) != p.trade_date:
        # 查詢日期與回應日期不同時，絕對不能入庫（否則會把別天的資料寫成今天）
        raise NormalizeError(f"回應日期 {raw} 與查詢日期 {p.trade_date} 不符")


def _signed(abs_value: Decimal | None, sign: int) -> Decimal | None:
    if abs_value is None:
        return None
    return abs(abs_value) * sign if sign else abs_value


# ---- MI_INDEX --------------------------------------------------------------
def normalize_market_day(p: RawPayload) -> tuple[MarketDaySnapshot, list[str]]:
    _check_date(p)
    tables = iter_tables(p.body)
    warnings: list[str] = []

    snapshot = MarketDaySnapshot(
        market="TWSE",
        trade_date=p.trade_date,
        quotes=_quotes(require_table(tables, "每日收盤行情")),
        indices=_indices(find_tables(tables, "價格指數"), warnings),
        breadth=_breadth(tables, warnings),
    )
    return snapshot, warnings


def _quotes(t) -> list[DailyQuote]:
    f = t.fields
    i_sym, i_name = col(f, "證券代號"), col(f, "證券名稱")
    i_vol, i_cnt, i_amt = col(f, "成交股數"), col(f, "成交筆數"), col(f, "成交金額")
    i_o, i_h, i_l, i_c = col(f, "開盤價"), col(f, "最高價"), col(f, "最低價"), col(f, "收盤價")
    i_sign, i_chg = col(f, "漲跌(+/-)"), col(f, "漲跌價差")

    out: list[DailyQuote] = []
    for row in t.data:
        symbol = clean_str(row[i_sym])
        if not symbol:
            continue
        sign, no_compare = parse_sign(row[i_sign])
        out.append(DailyQuote(
            symbol=symbol,
            name=clean_str(row[i_name]),
            security_type=security_type(symbol),
            open=parse_decimal(row[i_o]),
            high=parse_decimal(row[i_h]),
            low=parse_decimal(row[i_l]),
            close=parse_decimal(row[i_c]),
            change=None if no_compare else _signed(parse_decimal(row[i_chg]), sign),
            is_no_compare=no_compare,
            volume=parse_int(row[i_vol]) or 0,
            turnover=parse_int(row[i_amt]) or 0,
            trade_count=parse_int(row[i_cnt]),
        ))
    return out


def _indices(tables, warnings: list[str]) -> list[IndexQuote]:
    if not tables:
        warnings.append("找不到價格指數表格")
        return []
    seen: dict[str, IndexQuote] = {}
    for t in tables:
        f = t.fields
        i_name, i_close = col(f, lambda x: x == "指數"), col(f, "收盤指數")
        i_sign, i_pts = col(f, "漲跌(+/-)"), col(f, "漲跌點數")
        i_pct = col(f, "漲跌百分比", required=False)
        for row in t.data:
            code = clean_str(row[i_name])
            if not code or code in seen:
                continue
            sign, _ = parse_sign(row[i_sign])
            pct = parse_decimal(row[i_pct]) if i_pct is not None else None
            seen[code] = IndexQuote(
                index_code=code,
                close=parse_decimal(row[i_close]),
                change=_signed(parse_decimal(row[i_pts]), sign),
                change_pct=_signed(pct, sign),
            )
    return list(seen.values())


def _breadth(tables, warnings: list[str]) -> MarketBreadth:
    b = dict(advancers=None, decliners=None, unchanged=None, limit_up=None, limit_down=None,
             no_trade=None, total_volume=None, total_turnover=None, total_trades=None)

    updown = find_tables(tables, "漲跌證券數")
    if updown:
        t = updown[0]
        i_type = col(t.fields, "類型")
        i_val = col(t.fields, "股票", required=False)
        if i_val is None:
            i_val = col(t.fields, "整體市場")
        for row in t.data:
            kind, raw = clean_str(row[i_type]), clean_str(row[i_val])
            m = _COUNT_WITH_LIMIT.search(raw)
            main, limit = (parse_int(m.group(1)), parse_int(m.group(2))) if m else (parse_int(raw), None)
            if kind.startswith("上漲"):
                b["advancers"], b["limit_up"] = main, limit
            elif kind.startswith("下跌"):
                b["decliners"], b["limit_down"] = main, limit
            elif kind.startswith("持平"):
                b["unchanged"] = main
            elif kind.startswith("未成交"):
                b["no_trade"] = main
    else:
        warnings.append("找不到漲跌證券數表格")

    stats = find_tables(tables, "大盤統計")
    if stats:
        t = stats[0]
        i_kind = col(t.fields, "成交統計")
        i_amt, i_vol, i_cnt = col(t.fields, "成交金額"), col(t.fields, "成交股數"), col(t.fields, "成交筆數")
        for row in t.data:
            if clean_str(row[i_kind]).startswith("總計"):
                b["total_turnover"] = parse_int(row[i_amt])
                b["total_volume"] = parse_int(row[i_vol])
                b["total_trades"] = parse_int(row[i_cnt])
                break
        else:
            warnings.append("大盤統計表格中找不到「總計」列")
    else:
        warnings.append("找不到大盤統計表格")

    return MarketBreadth(**b)


# ---- T86 -------------------------------------------------------------------
def normalize_institutional(p: RawPayload) -> tuple[InstitutionalDay, list[str]]:
    _check_date(p)
    tables = iter_tables(p.body)
    if p.body.get("fields") and p.body.get("data") is not None:
        fields = [clean_str(x) for x in p.body["fields"]]
        data = p.body["data"]
    elif tables:
        fields, data = tables[0].fields, tables[0].data
    else:
        raise NormalizeError(f"T86 回應中找不到 fields/data；實際 keys：{list(p.body)}")

    i_sym, i_name = col(fields, "證券代號"), col(fields, "證券名稱")

    def foreign(kind: str):
        idx = cols(fields, lambda f: f.startswith(f"外陸資{kind}") or f.startswith(f"外資自營商{kind}"))
        return idx or cols(fields, lambda f: f.startswith(f"外資{kind}"))  # 舊版欄位名稱

    fb, fs, fn = foreign("買進股數"), foreign("賣出股數"), foreign("買賣超股數")
    tb, ts, tn = (cols(fields, lambda f, k=k: f.startswith(f"投信{k}")) for k in ("買進股數", "賣出股數", "買賣超股數"))
    db = cols(fields, lambda f: f.startswith("自營商買進股數"))
    ds = cols(fields, lambda f: f.startswith("自營商賣出股數"))
    dn = cols(fields, lambda f: f == "自營商買賣超股數") or cols(fields, lambda f: f.startswith("自營商買賣超股數("))
    total = cols(fields, lambda f: "三大法人買賣超股數" in f)

    if not (fn and tn and dn):
        raise NormalizeError(f"T86 找不到外資／投信／自營商買賣超欄位；實際欄位：{fields}")

    flows: list[InstitutionalFlow] = []
    for row in data:
        symbol = clean_str(row[i_sym])
        if not symbol:
            continue
        f_net, t_net, d_net = sum_cols(row, fn), sum_cols(row, tn), sum_cols(row, dn)
        total_net = sum_cols(row, total)
        if total_net is None and None not in (f_net, t_net, d_net):
            total_net = f_net + t_net + d_net
        flows.append(InstitutionalFlow(
            symbol=symbol, name=clean_str(row[i_name]),
            foreign_buy=sum_cols(row, fb), foreign_sell=sum_cols(row, fs), foreign_net=f_net,
            trust_buy=sum_cols(row, tb), trust_sell=sum_cols(row, ts), trust_net=t_net,
            dealer_buy=sum_cols(row, db), dealer_sell=sum_cols(row, ds), dealer_net=d_net,
            total_net=total_net,
        ))
    return InstitutionalDay(market="TWSE", trade_date=p.trade_date, flows=flows), []


# ---- TWT49U 除權除息計算結果表 --------------------------------------------------
def _action_type(raw) -> str | None:
    s = clean_str(raw)
    has_rights, has_dividend = "權" in s, "息" in s
    if has_rights and has_dividend:
        return "both"
    if has_dividend:
        return "dividend"
    if has_rights:
        return "rights"
    return None


def normalize_ex_rights(p: RawPayload, end: date) -> tuple[CorporateActionPeriod, list[str]]:
    """p 由 fetch_ex_rights(start, end) 取得（p.trade_date == start）。

    欄位：資料日期（115年07月01日）、股票代號、股票名稱、除權息前收盤價、除權息參考價、
    權值+息值、權/息（息／權／權息）。其餘欄位（漲跌停、申報淨值…）不需要。
    """
    body = p.body
    if body.get("fields") and body.get("data") is not None:
        fields, data = [clean_str(x) for x in body["fields"]], body["data"]
    else:
        tables = iter_tables(body)
        if not tables:
            raise NormalizeError(f"TWT49U 回應中找不到 fields/data；實際 keys：{list(body)}")
        fields, data = tables[0].fields, tables[0].data

    i_date, i_sym, i_name = col(fields, "資料日期"), col(fields, "股票代號"), col(fields, "股票名稱")
    i_prev, i_ref = col(fields, "除權息前收盤價"), col(fields, "除權息參考價")
    i_value, i_kind = col(fields, "權值+息值"), col(fields, lambda f: f == "權/息")
    i_up, i_down = col(fields, "漲停價格", required=False), col(fields, "跌停價格", required=False)
    i_open = col(fields, "開盤競價基準", required=False)
    i_div = col(fields, "減除股利參考價", required=False)

    warnings: list[str] = []
    actions: list[CorporateAction] = []
    for row in data:
        symbol = clean_str(row[i_sym])
        if not symbol:
            continue
        try:
            ex_date = parse_roc_date(row[i_date])
        except ValueError:
            warnings.append(f"{symbol} 剔除：無法解析日期 {clean_str(row[i_date])!r}")
            continue
        kind = _action_type(row[i_kind])
        prev, ref, value = parse_decimal(row[i_prev]), parse_decimal(row[i_ref]), parse_decimal(row[i_value])
        if kind is None or None in (prev, ref, value):
            warnings.append(f"{symbol} {ex_date} 剔除：欄位不完整（權/息={clean_str(row[i_kind])!r}）")
            continue
        opt = lambda i: parse_decimal(row[i]) if i is not None else None
        actions.append(CorporateAction(symbol=symbol, name=clean_str(row[i_name]), ex_date=ex_date,
                                       action_type=kind, prev_close=prev, ref_price=ref, value=value,
                                       limit_up=opt(i_up), limit_down=opt(i_down), open_ref=opt(i_open),
                                       div_ref=opt(i_div)))
    return CorporateActionPeriod(market="TWSE", start=p.trade_date, end=end, actions=actions), warnings


# ---- 上市公司基本資料（OpenAPI t187ap03_L ＋ t187ap14_L）------------------------
# t187ap03_L 的產業別只有代碼；名稱從 t187ap14_L（上市公司各產業 EPS 統計，產業別寫名稱）
# 以公司代號對出來，完全依官方資料，不手寫對照表。
# 例外：91（存託憑證 TDR）沒有 EPS 統計所以對不到，依證交所產業分類補上名稱。
_INDUSTRY_FALLBACK = {"91": "存託憑證"}


def _industry_names(companies: list[dict], eps_rows: list[dict], warnings: list[str]) -> dict[str, str]:
    name_by_symbol = {clean_str(r.get("公司代號")): clean_str(r.get("產業別")) for r in eps_rows}
    votes: dict[str, Counter] = defaultdict(Counter)
    for r in companies:
        name = name_by_symbol.get(clean_str(r.get("公司代號")))
        if name:
            votes[clean_str(r.get("產業別"))][name] += 1
    mapping = dict(_INDUSTRY_FALLBACK)
    for code, names in votes.items():
        (name, _), *others = names.most_common()
        if others:
            warnings.append(f"產業代碼 {code} 對到多個名稱 {dict(names)}，採用「{name}」")
        mapping[code] = name
    return mapping


def _listed_date(raw, symbol: str, warnings: list[str]) -> date | None:
    s = clean_str(raw)
    if not s:
        return None
    try:
        return parse_ymd(s) if re.fullmatch(r"(19|20)\d{6}", s) else parse_roc_date(s)
    except ValueError:
        warnings.append(f"{symbol} 上市日期無法解析：{s!r}")
        return None


def normalize_company_profiles(p: RawPayload) -> tuple[CompanySnapshot, list[str]]:
    """p 由 fetch_company_profiles(as_of) 取得，body = {"data": t187ap03_L, "industry_names": t187ap14_L}。"""
    rows = p.body.get("data")
    if not isinstance(rows, list) or not rows:
        raise NormalizeError(f"公司資料是空的或格式不對；實際 keys：{list(p.body)}")
    missing = [k for k in ("公司代號", "公司簡稱", "產業別", "上市日期") if k not in rows[0]]
    if missing:
        raise NormalizeError(f"t187ap03_L 缺少欄位 {missing}；實際欄位：{list(rows[0])}")

    warnings: list[str] = []
    mapping = _industry_names(rows, p.body.get("industry_names") or [], warnings)
    companies = []
    for r in rows:
        symbol = clean_str(r["公司代號"])
        if not symbol:
            continue
        code = clean_str(r["產業別"])
        companies.append(CompanyProfile(
            symbol=symbol,
            name=clean_str(r["公司簡稱"]) or clean_str(r.get("公司名稱")),
            industry_code=code,
            industry_name=mapping.get(code, ""),     # 對不到時留空，由 validator 剔除並記警告
            listed_date=_listed_date(r["上市日期"], symbol, warnings),
        ))
    return CompanySnapshot(market="TWSE", as_of=p.trade_date, companies=companies), warnings


# ---- 休市日曆（OpenAPI holidaySchedule）---------------------------------------------
# 清單裡也有「國曆新年開始交易日」「農曆春節前最後交易日」這種有開市的日子，名稱含這兩個詞的不算休市；
# 「市場無交易，僅辦理結算交割作業」是休市。
_OPEN_DAY_MARKERS = ("開始交易", "最後交易")


def normalize_holidays(p: RawPayload) -> tuple[list[tuple[date, str]], list[str]]:
    """回傳 [(休市日, 名稱)]。"""
    rows = p.body.get("data")
    if not isinstance(rows, list):
        raise NormalizeError(f"休市日曆格式不對；實際 keys：{list(p.body)}")
    warnings: list[str] = []
    closed: dict[date, str] = {}
    for r in rows:
        name = clean_str(r.get("Name"))
        if any(m in name for m in _OPEN_DAY_MARKERS):
            continue
        try:
            d = parse_roc_date(r.get("Date"))
        except ValueError:
            warnings.append(f"休市日曆日期無法解析：{r.get('Date')!r}（{name}）")
            continue
        closed.setdefault(d, name)
    return sorted(closed.items()), warnings
