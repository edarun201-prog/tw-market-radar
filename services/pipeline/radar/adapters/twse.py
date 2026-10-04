"""臺灣證券交易所盤後報表 adapter。

端點與欄位依目前已知格式撰寫；第一次實跑請先執行
`python -m radar probe --date YYYY-MM-DD` 確認表格標題與欄位名稱。
"""
from __future__ import annotations

import logging
from datetime import date

import httpx

from radar.adapters.base import FetchError, PoliteHttpAdapter, RawPayload
from radar.config import Settings

log = logging.getLogger(__name__)

BASE = "https://www.twse.com.tw/rwd/zh"
OPENAPI = "https://openapi.twse.com.tw/v1"
ENDPOINTS = {
    # 每日收盤行情（全部，不含權證、牛熊證），同一份回應也含指數與漲跌家數
    "MI_INDEX": (f"{BASE}/afterTrading/MI_INDEX", {"type": "ALLBUT0999"}),
    # 三大法人買賣超日報（全部，不含權證、牛熊證）
    "T86": (f"{BASE}/fund/T86", {"selectType": "ALLBUT0999"}),
}
EX_RIGHTS_URL = f"{BASE}/exRight/TWT49U"                 # 除權除息計算結果表，可查任意區間
COMPANY_URL = f"{OPENAPI}/opendata/t187ap03_L"           # 上市公司基本資料：產業別只有代碼
INDUSTRY_NAME_URL = f"{OPENAPI}/opendata/t187ap14_L"     # 上市公司各產業 EPS 統計：產業別是名稱
HOLIDAY_URL = f"{OPENAPI}/holidaySchedule/holidaySchedule"  # 當年度集中市場開（休）市日期
VALUATION_URL = f"{OPENAPI}/exchangeReport/BWIBBU_ALL"   # 個股本益比、殖利率、股價淨值比（最新一天）
HEADERS = {
    "User-Agent": "tw-market-radar/0.1 (after-hours research project)",
    "Accept": "application/json",
}


class TwseAdapter(PoliteHttpAdapter):
    source_code = "TWSE"
    drives_calendar = True      # 交易日曆以證交所為準
    FIN_REPORT_URL = f"{OPENAPI}/opendata/t187ap06_L_{{kind}}"   # 上市公司綜合損益表（最新一季）

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        super().__init__(settings, client or httpx.Client(headers=HEADERS, timeout=settings.request_timeout_sec))

    # ---- public -------------------------------------------------------
    def fetch_market_day(self, d: date) -> RawPayload:
        return self._fetch("MI_INDEX", d)

    def fetch_institutional(self, d: date) -> RawPayload:
        return self._fetch("T86", d)

    def fetch_ex_rights(self, start: date, end: date) -> RawPayload:
        """除權除息計算結果表（TWT49U），查詢 start～end；原始檔以 start 命名。"""
        params = {"startDate": start.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d"), "response": "json"}
        body, url = self._get_json(EX_RIGHTS_URL, params, f"TWT49U {start}～{end}")
        stat = str(body.get("stat", ""))
        return self._payload("TWT49U", start, url, body, stat.upper() == "OK", {"stat": stat, "end": end.isoformat()})

    def fetch_company_profiles(self, as_of: date) -> RawPayload:
        """上市公司基本資料＋產業名稱（兩個 OpenAPI 資料集，2 次請求）。

        t187ap03_L 的產業別只有代碼（24），名稱要從 t187ap14_L（產業別直接寫「半導體業」）
        用公司代號對出來，兩份都放進原始檔，對照在 normalizer 做。
        OpenAPI 只提供最新一期，as_of 是抓取日。
        """
        companies, url = self._get_json(COMPANY_URL, {}, "COMPANY")
        industries, _ = self._get_json(INDUSTRY_NAME_URL, {}, "COMPANY 產業名稱")
        for label, rows in (("t187ap03_L", companies), ("t187ap14_L", industries)):
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise FetchError(f"{label} 回應不是資料清單：{str(rows)[:200]}")
        body = {"data": companies, "industry_names": industries}
        return self._payload("COMPANY", as_of, url, body, bool(companies), {"rows": len(companies)})

    def fetch_holiday_schedule(self, as_of: date) -> RawPayload:
        """當年度休市日曆（OpenAPI，1 次請求）；as_of 是抓取日。"""
        rows, url = self._get_json(HOLIDAY_URL, {}, "HOLIDAY")
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise FetchError(f"休市日曆回應不是資料清單：{str(rows)[:200]}")
        return self._payload("HOLIDAY", as_of, url, {"data": rows}, bool(rows), {"rows": len(rows)})

    def fetch_valuations(self, as_of: date) -> RawPayload:
        """本益比、殖利率、股價淨值比（OpenAPI 只有最新一天，1 次請求）；as_of 是抓取日，資料日期在每一列的 Date。"""
        rows, url = self._get_rows(VALUATION_URL, "BWIBBU 本益比")
        return self._payload("BWIBBU", as_of, url, {"data": rows}, bool(rows), {"rows": len(rows)})

    # ---- internals ----------------------------------------------------
    def _fetch(self, dataset: str, d: date) -> RawPayload:
        url, extra = ENDPOINTS[dataset]
        params = {"date": d.strftime("%Y%m%d"), "response": "json", **extra}
        body, real_url = self._get_json(url, params, f"{dataset} {d}")
        stat = str(body.get("stat", ""))
        return self._payload(dataset, d, real_url, body, stat.upper() == "OK", {"stat": stat})
