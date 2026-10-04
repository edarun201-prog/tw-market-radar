"""技術指標：SMA、EMA、MACD。只描述價格的平均與變化，不是買賣訊號。

- 用「原始收盤價」計算，和 K 線圖、多數看盤軟體相同；除權息日的跳空會反映在均線上（K 線圖上也看得到）。
- SMA n：最近 n 個交易日收盤價的平均；資料不滿 n 天時是 None。
- EMA n：權重 2 ÷ (n + 1) 的指數移動平均，第一個值用前 n 天的 SMA 當起點；不滿 n 天時是 None。
- MACD（12, 26, 9）：DIF＝EMA12 − EMA26；MACD（訊號線）＝DIF 的 9 日 EMA；OSC（柱狀體）＝DIF − MACD。
- 沒有成交的日子（收盤價空白）沿用前一天的收盤價計算。
- EMA 的值和「從哪一天開始算」有關，所以網站、匯出檔、API、公開 JSON 都從同一段期間開始：最近 HISTORY ＋ WARMUP
  個交易日（資料不夠就從第一天），要顯示幾天都一樣，數字才會一致。
"""
from __future__ import annotations

SMA_PERIODS = (5, 20, 60)
EMA_PERIODS = (12, 26)
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
WARMUP = 60          # 計算時多抓的天數：最長的 SMA 要 60 天，畫出來的第一天就有值
HISTORY = 250        # 一律從最近 HISTORY + WARMUP 個交易日開始算（約 1 年 3 個月）


def lookback(days: int) -> int:
    """要顯示最近 days 天時，要從資料庫抓幾天來算（不論 days 多少，起算點都一樣）。"""
    return max(days, HISTORY) + WARMUP


def _filled(values: list) -> list[float | None]:
    out, last = [], None
    for v in values:
        last = float(v) if v is not None else last
        out.append(last)
    return out


def sma(values: list, n: int) -> list[float | None]:
    xs = _filled(values)
    out: list[float | None] = [None] * len(xs)
    window: list[float] = []
    for i, x in enumerate(xs):
        if x is None:
            continue
        window.append(x)
        if len(window) > n:
            window.pop(0)
        if len(window) == n:
            out[i] = sum(window) / n
    return out


def ema(values: list, n: int) -> list[float | None]:
    xs = _filled(values)
    out: list[float | None] = [None] * len(xs)
    alpha, seed, prev = 2 / (n + 1), [], None
    for i, x in enumerate(xs):
        if x is None:
            continue
        if prev is None:
            seed.append(x)
            if len(seed) == n:
                prev = sum(seed) / n
                out[i] = prev
            continue
        prev = alpha * x + (1 - alpha) * prev
        out[i] = prev
    return out


def macd(values: list, fast: int = MACD_FAST, slow: int = MACD_SLOW, signal: int = MACD_SIGNAL) -> dict[str, list]:
    f, s = ema(values, fast), ema(values, slow)
    dif = [a - b if a is not None and b is not None else None for a, b in zip(f, s)]
    sig = ema(dif, signal)
    # DIF 還沒有值的日子，訊號線也不給值
    sig = [v if d is not None else None for v, d in zip(sig, dif)]
    hist = [d - g if d is not None and g is not None else None for d, g in zip(dif, sig)]
    return {"dif": dif, "signal": sig, "hist": hist}


def compute(prices: list[dict]) -> dict[str, list]:
    """一檔股票的所有指標，和 prices 一樣長、同樣順序（舊 → 新）。"""
    closes = [p["close"] for p in prices]
    out: dict[str, list] = {f"sma{n}": sma(closes, n) for n in SMA_PERIODS}
    out |= {f"ema{n}": ema(closes, n) for n in EMA_PERIODS}
    out |= macd(closes)
    return out


def latest(ind: dict[str, list]) -> dict[str, float | None]:
    """最後一天的各項指標。"""
    return {k: (v[-1] if v else None) for k, v in ind.items()}


def tail(ind: dict[str, list], n: int) -> dict[str, list]:
    return {k: v[-n:] for k, v in ind.items()}
