"""證券櫃檯買賣中心（上櫃）adapter：官方 OpenAPI（https://www.tpex.org.tw/openapi/v1）。

- OpenAPI 只提供「最新一天」，沒有日期參數，所以每天抓最新的那一天；錯過的日子補不回來（網站的歷史查詢目前
  對程式沒有回應，等它能用再一次補齊）。要的日子不是最新一天時：比最新還舊 → FetchError（拿不到歷史）；
  比最新還新 → has_data=False（還沒更新，稍後再抓）。
- 不決定交易日曆（drives_calendar=False）：抓不到資料不會把那天記成休市，交易日曆以證交所為準。
- 每個資料集各 1 次請求，間隔與證交所相同（REQUEST_INTERVAL_SEC）。
"""
from __future__ import annotations

from datetime import date

import httpx

from radar.adapters.base import FetchError, PoliteHttpAdapter, RawPayload
from radar.config import Settings

OPENAPI = "https://www.tpex.org.tw/openapi/v1"
URLS = {
    "quotes": f"{OPENAPI}/tpex_mainboard_daily_close_quotes",   # 上櫃股票行情（含 ETF、權證等，normalizer 只留股票與 ETF）
    "index": f"{OPENAPI}/tpex_index",                            # 櫃買指數（最近幾天）
    "highlight": f"{OPENAPI}/tpex_mainborad_highlight",          # 上櫃股票市場現況：漲跌家數、成交量值
    "insti": f"{OPENAPI}/tpex_3insti_daily_trading",             # 三大法人買賣明細
    "exright": f"{OPENAPI}/tpex_exright_daily",                  # 除權除息計算結果表（當天）
    "company": f"{OPENAPI}/mopsfin_t187ap03_O",                  # 上櫃公司基本資料（產業別代碼）
    "peratio": f"{OPENAPI}/tpex_mainboard_peratio_analysis",     # 本益比、殖利率、股價淨值比（最新一天）
}
HEADERS = {"User-Agent": "tw-market-radar/0.1 (after-hours research project)", "Accept": "application/json"}


def roc_date(s: str) -> date:
    """OpenAPI 的日期：民國年 1151002 或西元 20261002。"""
    s = str(s).strip()
    if len(s) == 8:
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    return date(int(s[:-4]) + 1911, int(s[-4:-2]), int(s[-2:]))


class TpexAdapter(PoliteHttpAdapter):
    source_code = "TPEX"
    drives_calendar = False
    FIN_REPORT_URL = f"{OPENAPI}/mopsfin_t187ap06_O_{{kind}}"     # 上櫃公司綜合損益表（最新一季）

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        super().__init__(settings, client or httpx.Client(headers=HEADERS, timeout=settings.request_timeout_sec))

    def latest_date(self) -> date:
        """OpenAPI 目前提供的是哪一天（用最小的櫃買指數資料集判斷，1 次請求）。"""
        rows, _ = self._get_json(URLS["index"], {}, "TPEX 櫃買指數")
        if not isinstance(rows, list) or not rows:
            raise FetchError(f"櫃買指數回應不是資料清單：{str(rows)[:200]}")
        return max(roc_date(r["Date"]) for r in rows)

    def _rows(self, key: str, label: str) -> tuple[list[dict], str]:
        rows, url = self._get_json(URLS[key], {}, label)
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise FetchError(f"{label} 回應不是資料清單：{str(rows)[:200]}")
        return rows, url

    def _check(self, rows: list[dict], d: date, label: str) -> tuple[bool, date | None]:
        latest = max((roc_date(r["Date"]) for r in rows if r.get("Date")), default=None)
        if latest and latest > d:
            raise FetchError(f"{label}：櫃買中心 OpenAPI 只提供最新一天（{latest}），拿不到 {d} 的資料")
        return latest == d, latest

    def fetch_market_day(self, d: date) -> RawPayload:
        quotes, url = self._rows("quotes", f"TPEX 上櫃行情 {d}")
        has, latest = self._check(quotes, d, "上櫃行情")
        body = {"quotes": quotes, "index": [], "highlight": []}
        if has:
            body["index"], _ = self._rows("index", "TPEX 櫃買指數")
            body["highlight"], _ = self._rows("highlight", "TPEX 市場現況")
        return self._payload("MI_INDEX", d, url, body, has, {"latest": latest.isoformat() if latest else None})

    def fetch_institutional(self, d: date) -> RawPayload:
        rows, url = self._rows("insti", f"TPEX 三大法人 {d}")
        has, latest = self._check(rows, d, "上櫃三大法人")
        return self._payload("T86", d, url, {"data": rows}, has, {"latest": latest.isoformat() if latest else None})

    def fetch_ex_rights(self, start: date, end: date) -> RawPayload:
        """只有最新一天的除權息；normalizer 只取 start～end 之間的。"""
        rows, url = self._rows("exright", f"TPEX 除權息 {start}～{end}")
        return self._payload("TWT49U", start, url, {"data": rows}, True, {"end": end.isoformat()})

    def fetch_valuations(self, as_of: date) -> RawPayload:
        rows, url = self._rows("peratio", "TPEX 本益比")
        return self._payload("BWIBBU", as_of, url, {"data": rows}, bool(rows), {"rows": len(rows)})

    def fetch_company_profiles(self, as_of: date) -> RawPayload:
        rows, url = self._rows("company", "TPEX 上櫃公司基本資料")
        return self._payload("COMPANY", as_of, url, {"data": rows}, bool(rows), {"rows": len(rows)})
