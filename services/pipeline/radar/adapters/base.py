"""Adapter 介面：只有 adapter 知道資料從哪裡來、長什麼樣子。

換資料來源時，新增一個實作這個 Protocol 的類別，並寫對應的 normalizer；
後面的驗證、入庫、特徵計算都不用改。
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

log = logging.getLogger(__name__)


# 綜合損益表的產業格式：一般業、銀行、證券期貨、金控、保險、異業
REPORT_TYPES = ("ci", "basi", "bd", "fh", "ins", "mim")


class FetchError(RuntimeError):
    """網路錯誤、被擋、回傳格式不是 JSON 等，可重試的錯誤。"""


@dataclass
class RawPayload:
    source: str             # TWSE / TPEX / FINMIND
    dataset: str            # MI_INDEX / T86 ...
    trade_date: date
    url: str
    fetched_at: datetime
    body: dict[str, Any]
    has_data: bool          # 來源明確表示「查無資料」時為 False
    meta: dict[str, Any] = field(default_factory=dict)


class MarketDataAdapter(Protocol):
    source_code: str

    def fetch_market_day(self, d: date) -> RawPayload:
        """全市場日行情＋指數＋漲跌家數。"""
        ...

    def fetch_institutional(self, d: date) -> RawPayload:
        """三大法人買賣超。"""
        ...


class CorporateActionAdapter(Protocol):
    source_code: str

    def fetch_ex_rights(self, start: date, end: date) -> RawPayload:
        """start～end 之間所有的除權息（含當天）。RawPayload.trade_date 請填 start，原始檔以它命名。"""
        ...


class CompanyProfileAdapter(Protocol):
    source_code: str

    def fetch_company_profiles(self, as_of: date) -> RawPayload:
        """目前所有上市公司的基本資料（含產業別）。RawPayload.trade_date 請填 as_of（抓取當天）。"""
        ...


class PoliteHttpAdapter:
    """共用的禮貌抓取：每次請求至少間隔 settings.request_interval_sec（加隨機抖動）、失敗重試、包成 RawPayload。

    子類別要設定 source_code，並在 __init__ 傳入 settings 與 httpx.Client。
    """
    source_code: str
    drives_calendar = True      # 抓不到資料時能不能把那天記成休市（只有證交所能決定交易日曆）

    def __init__(self, settings, client):
        self.s = settings
        self.client = client
        self._last_request = 0.0

    def _throttle(self) -> None:
        wait = self.s.request_interval_sec - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 1.0))
        self._last_request = time.monotonic()

    def _get_json(self, url: str, params: dict, label: str):
        """節流＋重試；回傳 (JSON, 實際網址)。被擋時常回 HTML，會以 ValueError 失敗並重試。"""
        import httpx
        last_err: Exception | None = None
        for attempt in range(1, self.s.max_retries + 1):
            self._throttle()
            try:
                resp = self.client.get(url, params=params)
                resp.raise_for_status()
                return resp.json(), str(resp.request.url)
            except (httpx.HTTPError, ValueError) as e:
                last_err = e
                backoff = 10 * attempt
                log.warning("%s 第 %d 次失敗：%s；%d 秒後重試", label, attempt, e, backoff)
                time.sleep(backoff)
        raise FetchError(f"{label} 抓取失敗：{last_err}")

    def _get_rows(self, url: str, label: str) -> tuple[list[dict], str]:
        """OpenAPI 的資料清單（list of dict）；格式不對就 FetchError。"""
        rows, real_url = self._get_json(url, {}, label)
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise FetchError(f"{label} 回應不是資料清單：{str(rows)[:200]}")
        return rows, real_url

    def fetch_financial_reports(self, as_of: date) -> RawPayload:
        """綜合損益表（最新一季、年度累計），各產業格式各一個資料集；子類別設定 FIN_REPORT_URL（含 {kind}）。"""
        body, url = {}, ""
        for kind in REPORT_TYPES:
            body[kind], url = self._get_rows(self.FIN_REPORT_URL.format(kind=kind), f"{self.source_code} 綜合損益表 {kind}")
        return self._payload("FIN_REPORT", as_of, url, body, any(body.values()), {k: len(v) for k, v in body.items()})

    def _payload(self, dataset: str, d: date, url: str, body, has_data: bool, meta: dict) -> RawPayload:
        from radar.config import TAIPEI
        return RawPayload(source=self.source_code, dataset=dataset, trade_date=d, url=url,
                          fetched_at=datetime.now(TAIPEI), body=body, has_data=has_data, meta=meta)
