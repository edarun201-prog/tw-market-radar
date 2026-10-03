"""盤中即時行情。資料來源可以替換（和 adapters/ 的分層一樣）：網頁、API 只看 LiveFeed，不知道資料從哪來。

資料來源（設定 LIVE_PROVIDER）
- mis（預設）：臺灣證券交易所「基本市況報導網站」。不需要金鑰，但**只適合自己看**。
- off：關閉即時行情。
- 將來上架讓別人看：即時行情受證交所（上櫃另有櫃買中心）資訊使用規範管制，對外提供需要先簽資訊使用契約，
  或改用已取得授權、條款允許對外顯示的資訊廠商。拿到授權後，新增一個符合 LiveProvider 的類別、登記在 PROVIDERS、
  把 LIVE_PROVIDER 改過去即可；網頁、API、快取都不用動。

LiveFeed（和資料來源無關）
- 伺服器端快取：開盤時間 15 秒、其他時間 10 分鐘。不論多少人同時在看，對上游的請求數都一樣。
- 上游失敗時回上一次的資料並標示「可能不是最新」。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, time as dtime
from typing import Protocol

import httpx

from radar.config import TAIPEI

log = logging.getLogger(__name__)

TTL_OPEN, TTL_CLOSED = 15, 600        # 快取秒數
MAX_SYMBOLS = 40
SYMBOL_RE = re.compile(r"^[0-9]{4,6}[A-Z]?$")

PHASES = {
    "pre": "尚未開盤",          # 08:30 前：顯示的是前一個交易日
    "auction": "開盤前試撮",     # 08:30–09:00
    "open": "盤中",             # 09:00–13:30
    "after": "已收盤",           # 13:30 之後：盤中最後一筆；官方盤後資料 18:30 後由每日流程更新
    "closed": "休市",
}


def market_phase(now: datetime, trading_day: bool) -> str:
    if not trading_day:
        return "closed"
    t = now.astimezone(TAIPEI).time()
    if t < dtime(8, 30):
        return "pre"
    if t < dtime(9, 0):
        return "auction"
    if t < dtime(13, 30):
        return "open"
    return "after"


def clean_symbols(raw: str | None) -> list[str]:
    """?symbols=2330,2317 → 只留合法的上市代號（不會把任意字串送到上游），最多 MAX_SYMBOLS 檔。"""
    out: list[str] = []
    for s in (raw or "").replace(" ", "").upper().split(","):
        if SYMBOL_RE.match(s) and s not in out:
            out.append(s)
    return out[:MAX_SYMBOLS]


def _num(s) -> float | None:
    if s in (None, "", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---- 資料來源 --------------------------------------------------------------------------------
class LiveProvider(Protocol):
    """即時資料來源。fetch 回傳 {"quotes": [quote...], "market": {...} 或 None}；quote 的欄位見 MisProvider.parse_quote。
    加權指數、櫃買指數也放在 quotes 裡，代號分別用 "t00"、"o00"。"""
    key: str
    name: str            # 網頁上顯示的資料來源
    usage: str           # 使用範圍
    public_ok: bool      # 條款是否允許對外提供（上架到公開平台）

    def fetch(self, symbols: list[str], markets: dict[str, str] | None = None) -> dict: ...


class MisProvider:
    """臺灣證券交易所 基本市況報導網站（MIS）。只給自己看，不能拿來對外提供。

    欄位（2026-10-02 用盤後官方資料驗證過）：z 成交價（這 5 秒沒有成交時是 "-"，改用 pz）、y 昨收、o/h/l 開高低、
    v 累計成交量（張）、u/w 漲停／跌停價、d/t 日期時間。統計的 tz 是一般交易的累計成交金額（不含零股、盤後定價、鉅額，
    比收盤後的官方總成交金額少一點）、tv 是累計成交量（張）。
    """
    key = "mis"
    name = "臺灣證券交易所 基本市況報導網站"
    usage = "個人使用（對外提供需先取得證交所授權）"
    public_ok = False
    QUOTE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
    STATS_URL = "https://mis.twse.com.tw/stock/api/getStatis.jsp"
    USER_AGENT = "Mozilla/5.0 (tw-market-radar; personal use)"
    MIN_INTERVAL = 3.0                     # 任兩次請求至少隔幾秒（MIS 自己建議 5 秒更新一次）
    INDEXES = {"t00": "tse_t00.tw", "o00": "otc_o00.tw"}   # 加權指數、櫃買指數

    STATS_TTL = TTL_OPEN                   # 盤中統計和要查哪些股票無關，共用 15 秒；換一組股票只需要查一次報價

    def __init__(self, timeout: float = 8):
        self.timeout = timeout
        self._last = -1e9
        self._stats: tuple[float, dict | None] = (-1e9, None)

    def _wait_turn(self):
        gap = time.monotonic() - self._last
        if gap < self.MIN_INTERVAL:
            time.sleep(self.MIN_INTERVAL - gap)
        self._last = time.monotonic()

    @staticmethod
    def parse_quote(m: dict) -> dict:
        price, prev = _num(m.get("z")) or _num(m.get("pz")), _num(m.get("y"))
        change = price - prev if price is not None and prev is not None else None
        up, down = _num(m.get("u")), _num(m.get("w"))
        stamp = None
        if m.get("d") and m.get("t"):
            try:
                stamp = datetime.strptime(m["d"] + m["t"], "%Y%m%d%H:%M:%S").replace(tzinfo=TAIPEI)
            except ValueError:
                stamp = None
        vol = _num(m.get("v"))
        return {
            "symbol": m.get("c"), "name": m.get("n"), "price": price, "prev_close": prev, "change": change,
            "change_pct": change / prev if change is not None and prev else None,
            "open": _num(m.get("o")), "high": _num(m.get("h")), "low": _num(m.get("l")),
            "volume_lots": int(vol) if vol is not None else None, "limit_up": up, "limit_down": down,
            "at_limit": ("up" if price is not None and up and price >= up
                         else "down" if price is not None and down and price <= down else None),
            "time": stamp.isoformat() if stamp else None, "date": m.get("d"),
        }

    @staticmethod
    def parse_stats(detail: dict) -> dict | None:
        tz, tv = _num(detail.get("tz")), _num(detail.get("tv"))
        return {"turnover": tz, "volume_lots": int(tv) if tv is not None else None, "time": detail.get("%")} if tz else None

    @classmethod
    def channels(cls, symbols: list[str], markets: dict[str, str] | None = None) -> str:
        """MIS 的查詢代碼：上市 tse_代號.tw、上櫃 otc_代號.tw，前面固定加上兩個指數。"""
        markets = markets or {}
        return "|".join(list(cls.INDEXES.values())
                        + [f"{'otc' if markets.get(s) == 'TPEX' else 'tse'}_{s}.tw" for s in symbols])

    def fetch(self, symbols: list[str], markets: dict[str, str] | None = None) -> dict:
        """markets：代號 → TWSE／TPEX；上櫃股票要用 otc_ 開頭查。"""
        ex_ch = self.channels(symbols, markets)
        stamp = str(int(time.time() * 1000))
        with httpx.Client(timeout=self.timeout, headers={"User-Agent": self.USER_AGENT}) as c:
            self._wait_turn()
            q = c.get(self.QUOTE_URL, params={"ex_ch": ex_ch, "json": "1", "delay": "0", "_": stamp})
            q.raise_for_status()
            if time.monotonic() - self._stats[0] > self.STATS_TTL:
                try:
                    self._wait_turn()
                    st = c.get(self.STATS_URL, params={"ex": "tse", "delay": "0", "_": stamp})
                    st.raise_for_status()
                    self._stats = (time.monotonic(), self.parse_stats(st.json().get("detail", {})))
                except (httpx.HTTPError, ValueError) as e:     # 統計拿不到不影響報價
                    log.warning("盤中統計取得失敗：%s", e)
        return {"quotes": [self.parse_quote(m) for m in q.json().get("msgArray", [])], "market": self._stats[1]}


# 已登記的資料來源。上架時加入有授權的來源，例如 {"mis": MisProvider, "vendor": VendorProvider}
PROVIDERS: dict[str, type] = {"mis": MisProvider}


def make_provider(key: str | None) -> LiveProvider | None:
    key = (key or "mis").strip().lower()
    if key in ("", "off", "none"):
        return None
    if key not in PROVIDERS:
        raise ValueError(f"不認得的即時資料來源 LIVE_PROVIDER={key}（可用：{', '.join(PROVIDERS)}、off）")
    return PROVIDERS[key]()


# ---- 快取（和資料來源無關） ------------------------------------------------------------------
class LiveFeed:
    def __init__(self, provider: LiveProvider | None, clock=time.monotonic):
        self.provider = provider
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[float, dict]] = {}

    def describe(self) -> dict:
        p = self.provider
        return ({"key": p.key, "name": p.name, "usage": p.usage, "public_ok": p.public_ok} if p
                else {"key": "off", "name": None, "usage": "即時行情沒有開啟", "public_ok": True})

    def snapshot(self, symbols: list[str], phase: str, markets: dict[str, str] | None = None) -> dict:
        if self.provider is None:
            return {"available": False, "index": None, "otc": None, "stocks": [], "market": None, "stale": False,
                    "error": "即時行情沒有開啟。"}
        key = tuple(sorted(symbols))
        ttl = TTL_OPEN if phase in ("open", "auction") else TTL_CLOSED
        with self._lock:                       # 同一時間只有一個上游請求；其他人等它拿到後直接用快取
            hit = self._cache.get(key)
            if hit and self._clock() - hit[0] < ttl:
                return hit[1] | {"cached": True}
            try:
                raw = self.provider.fetch(symbols, markets)
            except Exception as e:             # 上游沒回應、格式改了：給上一次的資料並標示「可能不是最新」
                log.warning("即時行情取得失敗：%s", e)
                if hit:
                    return hit[1] | {"cached": True, "stale": True, "error": "暫時取不到即時行情，顯示的是上一次的資料。"}
                return {"available": True, "index": None, "otc": None, "stocks": [], "market": None, "stale": True,
                        "error": "暫時取不到即時行情。"}
            by = {q["symbol"]: q for q in raw.get("quotes", [])}
            data = {"available": True, "index": by.pop("t00", None), "otc": by.pop("o00", None),
                    "stocks": [by[s] for s in symbols if s in by], "market": raw.get("market"),
                    "stale": False, "error": None}
            self._cache[key] = (self._clock(), data)
            return data | {"cached": False}
