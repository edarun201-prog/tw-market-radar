"""技術指標：SMA、EMA、MACD 的數字與起算規則。"""
import pytest

from radar import indicators as ind


def test_sma_needs_n_days_and_averages():
    assert ind.sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]
    assert ind.sma([1, None, 3], 2) == [None, 1.0, 2.0]          # 沒成交的日子沿用前一天收盤


def test_ema_starts_from_sma_then_weights_recent_days():
    e = ind.ema([1, 2, 3, 4, 5], 3)
    assert e[:2] == [None, None] and e[2] == pytest.approx(2.0)   # 起點＝前 3 日 SMA
    assert e[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)             # 權重 2 ÷ (3 + 1)
    assert e[4] == pytest.approx(0.5 * 5 + 0.5 * 3.0)


def test_macd_definitions():
    closes = [100 + (i % 7) - (i % 3) * 0.5 + i * 0.2 for i in range(80)]
    m = ind.macd(closes)
    e12, e26 = ind.ema(closes, 12), ind.ema(closes, 26)
    assert m["dif"][24] is None and m["dif"][25] == pytest.approx(e12[25] - e26[25])
    assert m["signal"][32] is None and m["signal"][33] is not None   # 訊號線要 DIF 有 9 天
    sig9 = ind.ema(m["dif"], 9)
    assert m["signal"][-1] == pytest.approx(sig9[-1])
    assert m["hist"][-1] == pytest.approx(m["dif"][-1] - m["signal"][-1])


def test_same_start_gives_same_numbers_whatever_is_displayed():
    # 網站顯示 250 天、API 顯示 3 天：從同一個起算點算，最後一天的數字要一樣
    closes = [{"close": 50 + (i % 11) * 0.7 + i * 0.05} for i in range(400)]
    full = closes[-ind.lookback(250):]
    short = closes[-ind.lookback(3):]
    assert ind.compute(full)["dif"][-1] == pytest.approx(ind.compute(short)["dif"][-1])
    assert ind.lookback(3) == ind.lookback(250) == ind.HISTORY + ind.WARMUP
