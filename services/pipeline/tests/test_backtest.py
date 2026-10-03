"""訊號回測（Phase 3）：後續報酬的計算、和全部股票比較、只用當時已知的結果、文字只描述過去。"""
import re

import pytest

from radar import explain
from radar.features import build_features
from radar.outcomes import ROUND_TRIP_COST, build_outcomes
from radar.web import queries as q

from .test_analytics import _seed, _weekdays
from .test_explain import ADVICE, _no_advice
from .test_web import client, market  # noqa: F401


def _signal(conn, sid, d, typ="vol_spike"):
    return conn.execute("""INSERT INTO market_signals (stock_id, trade_date, signal_type, value, threshold, evidence, engine_version)
                           VALUES (%s, %s, %s, 5, 4, '{}', 'v2') RETURNING id""", (sid, d, typ)).fetchone()[0]


def test_forward_returns_baseline_and_as_of(conn):
    days = _weekdays(30)
    ids, _ = _seed(conn, days, {"1101": [(round(100 * 1.01 ** i, 2), 1_000_000) for i in range(30)],   # 每天漲 1%
                                "1102": [(50, 1_000_000)] * 30})                                        # 不動
    build_features(conn, days[0], days[-1])
    sig = _signal(conn, ids["1101"], days[5])
    counts = build_outcomes(conn)
    assert counts["signal_outcomes"] == 1

    o = conn.execute("SELECT h5, h20, h60, d20, b5, e5, e20, ed20 FROM signal_outcomes WHERE signal_id = %s", (sig,)).fetchone()
    h5, h20, h60, d20, b5, e5, e20, ed20 = o
    assert float(h5) == pytest.approx(1.01 ** 5 - 1, abs=2e-4)               # 之後 5 個交易日，還原報酬連乘
    assert float(h20) == pytest.approx(1.01 ** 20 - 1, abs=5e-4)
    assert float(d20) == pytest.approx(1.01 ** 20 - 1, abs=5e-4)              # 隔天才買：往後錯開一天，一樣 20 天
    assert h60 is None                                                        # 還沒滿 60 天
    assert float(b5) == pytest.approx((1.01 ** 5 - 1) / 2, abs=2e-4)          # 同一天全部股票的平均（另一檔不動）
    assert (e5, e20, ed20) == (days[10], days[25], days[26])                  # 各段報酬結束的交易日

    n, up = conn.execute("SELECT n, up_share FROM market_forward_returns WHERE trade_date = %s AND horizon = 'h5'",
                         (days[5],)).fetchone()
    assert n == 2 and float(up) == 0.5

    # 快照只用「那天已經知道結果」的訊號：5 日報酬要到 days[10] 才知道
    assert not q.backtest_stats(conn, ROUND_TRIP_COST, as_of=days[9])
    known = {r["horizon"] for r in q.backtest_stats(conn, ROUND_TRIP_COST, as_of=days[10])}
    assert known == {"h5"}
    assert {r["horizon"] for r in q.backtest_stats(conn, ROUND_TRIP_COST)} == {"h5", "h20", "d20"}


def _row(t, h, n, win, median, base_win, base_median, mean=None):
    return {"signal_type": t, "horizon": h, "n": n, "win": win, "win_cost": win - 0.03, "mean": mean if mean is not None else median,
            "median": median, "p25": median - 0.06, "p75": median + 0.09, "excess_mean": median - base_median,
            "base_win": base_win, "base_median": base_median}


def test_backtest_view_compares_with_all_stocks_and_only_describes_the_past():
    rows = [_row("trust_big_buy", "h20", 800, 0.60, 0.02, 0.50, -0.001, mean=0.045),     # 比全部股票好一些
            _row("vol_spike", "h20", 1900, 0.42, -0.02, 0.49, -0.003),                 # 差一些
            _row("high_60", "h20", 1100, 0.51, 0.002, 0.48, -0.004),                   # 差不多
            _row("low_60", "h20", 12, 0.9, 0.2, 0.5, 0.0),                             # 筆數太少
            _row("foreign_buy_streak", "h20", 1800, 0.52, 0.004, 0.48, -0.005),
            _row("foreign_sell_streak", "h20", 1650, 0.55, 0.011, 0.50, -0.003),       # 連賣之後反而不差
            _row("surge_5d", "h20", 1700, 0.49, -0.007, 0.49, -0.004, mean=0.05),
            _row("surge_5d", "d20", 1690, 0.48, -0.012, 0.49, -0.004)]
    period = {"start": None, "end": None, "n": 9000, "index_from": 26000.0, "index_to": 47000.0, "index_change": 0.81}
    base = {"h20": {"up_share": 0.49, "median": -0.003, "mean": 0.01, "days": 200}}
    bt = explain.backtest_view(rows, period, base, ROUND_TRIP_COST)

    verdicts = {t["info"].type: t["verdict"]["key"] for t in bt["types"]}
    assert (verdicts["trust_big_buy"], verdicts["vol_spike"], verdicts["high_60"], verdicts["low_60"]) == ("better", "worse", "same", "few")
    assert verdicts["plunge_5d"] == "few"                                        # 沒有資料的類型
    assert [t["verdict"]["key"] for t in bt["types"]][0] == "better"            # 「好一些」排前面

    myths = {m["type"]: m for m in bt["myths"]}
    assert myths[None]["verdict"] == "資料不支持這個說法" and "+81.0%" in myths[None]["finding"]   # 大盤漲 ≠ 大部分股票漲
    assert any("連賣" in p for p in myths["foreign_buy_streak"]["points"])
    assert any("隔天收盤才買到" in p for p in myths["surge_5d"]["points"])
    assert any("少數大漲" in p for p in myths["surge_5d"]["points"])

    texts = [t["sentence"] for t in bt["types"]] + bt["caveats"] + [m["finding"] for m in bt["myths"]]
    texts += [p for m in bt["myths"] for p in m["points"]] + [m["verdict"] for m in bt["myths"]]
    for text in texts:
        _no_advice(text)
    assert any("不代表之後" in c for c in bt["caveats"])
    assert all("過去" in t["sentence"] for t in bt["types"] if t["h"])


def test_backtest_page_modes_and_api(conn, client, market):  # noqa: F811
    build_outcomes(conn)
    html = client.get("/backtest").text
    assert '<section class="block beg-only" aria-labelledby="bt-beg">' in html      # 新手：一句話
    assert '<section class="block std-only" aria-labelledby="bt-std">' in html      # 標準：一張表
    assert '<section class="block adv-only" aria-labelledby="bt-myth">' in html     # 進階：市面說法、完整數字、限制
    assert 'id="mode-advanced"' in html and "不是買賣建議" in html
    disclaimers_removed = re.sub(r"(不是|不提供任何|不提供|不構成投資)[^。<]{0,12}建議", "", html)   # 「不提供建議」是聲明，不算
    assert not [w for w in ADVICE if w in disclaimers_removed]
    api = client.get("/api/backtest").json()
    assert {"period", "types", "myths", "caveats", "round_trip_cost"} <= set(api) and len(api["types"]) == 8
    assert client.get("/radar").status_code == 200
    stock = client.get("/stock/2330").text
    assert '<th class="num adv-only">之後 20 日</th>' in stock
