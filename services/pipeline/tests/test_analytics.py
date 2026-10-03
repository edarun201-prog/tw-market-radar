"""特徵（還原價）與訊號引擎：用人工行情驗證會踩雷的情況。"""
from datetime import date, timedelta

from radar.features import build_features
from radar.signals import generate_signals


def _weekdays(n, start=date(2026, 1, 5)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _seed(conn, days, series, closed=()):
    """series: symbol → list of (close, volume) 或 (close, volume, change)；change 省略時用前一天收盤計算。"""
    for d in days:
        conn.execute("INSERT INTO trading_calendar (trade_date, is_open) VALUES (%s, true)", (d,))
    for d in closed:
        conn.execute("INSERT INTO trading_calendar (trade_date, is_open) VALUES (%s, false)", (d,))
    src = conn.execute("SELECT id FROM data_sources WHERE code='TWSE'").fetchone()[0]
    ids = {}
    for sym, rows in series.items():
        ids[sym] = conn.execute("INSERT INTO stocks (market, symbol, name) VALUES ('TWSE', %s, %s) RETURNING id",
                                (sym, f"股{sym}")).fetchone()[0]
        prev = None
        for d, row in zip(days, rows):
            close, volume, *chg = row
            change = chg[0] if chg else (0 if prev is None else close - prev)
            conn.execute("""INSERT INTO daily_prices (stock_id, trade_date, open, high, low, close, change, volume,
                            turnover, source_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                         (ids[sym], d, close, close, close, close, change, volume, int(volume * close), src))
            prev = close
    return ids, src


def _feature(conn, sid, d, col):
    return conn.execute(f"SELECT {col} FROM daily_features WHERE stock_id=%s AND trade_date=%s", (sid, d)).fetchone()[0]


def _signals(conn, typ=None):
    sql = "SELECT s.symbol, m.trade_date, m.signal_type FROM market_signals m JOIN stocks s ON s.id = m.stock_id"
    rows = conn.execute(sql + " ORDER BY 2, 1").fetchall()
    return [r for r in rows if typ is None or r[2] == typ]


def test_ex_dividend_is_not_a_crash(conn):
    days = _weekdays(80)
    # 100 元走平，第 70 天除息 5 元後以 95 元走平：原始價看起來「創 60 日新低、急跌」，還原後什麼都沒發生
    ids, src = _seed(conn, days, {"1101": [(100, 2_000_000)] * 70 + [(95, 2_000_000, 0)] * 10})
    conn.execute("""INSERT INTO corporate_actions (stock_id, ex_date, action_type, prev_close, ref_price, value, source_id)
                    VALUES (%s, %s, 'dividend', 100, 95, 5, %s)""", (ids["1101"], days[70], src))
    build_features(conn, days[0], days[-1])
    generate_signals(conn, days[65], days[-1])

    sid = ids["1101"]
    assert _feature(conn, sid, days[70], "has_ex_right") is True
    assert abs(_feature(conn, sid, days[70], "ret_1d")) < 1e-6
    assert float(_feature(conn, sid, days[75], "low60")) == 95.0      # 前 60 日低點換算成當天價格水準
    assert _signals(conn) == []


def test_postponed_ex_date_takes_effect_next_trading_day(conn):
    days = _weekdays(30)
    typhoon = days[20] + timedelta(days=0)
    open_days = [d for d in days if d != typhoon]
    # 颱風休市那天是除息日：原始資料寫 typhoon，但生效在下一個交易日
    rows = [(100, 1_000_000)] * 20 + [(95, 1_000_000, 0)] * 9
    ids, src = _seed(conn, open_days, {"2891": rows}, closed=[typhoon])
    conn.execute("""INSERT INTO corporate_actions (stock_id, ex_date, action_type, prev_close, ref_price, value, source_id)
                    VALUES (%s, %s, 'dividend', 100, 95, 5, %s)""", (ids["2891"], typhoon, src))
    build_features(conn, open_days[0], open_days[-1])
    effective = open_days[20]
    assert effective > typhoon
    assert _feature(conn, ids["2891"], effective, "has_ex_right") is True
    assert abs(_feature(conn, ids["2891"], effective, "ret_1d")) < 1e-6


def test_split_without_twt49u_is_derived_from_official_change(conn):
    days = _weekdays(30)
    # 1 拆 20，TWT49U 不含分割；官方漲跌以調整後參考價（20）計算，所以 change = 0
    ids, _ = _seed(conn, days, {"0050": [(400, 1_000_000)] * 20 + [(20, 20_000_000, 0)] * 10})
    build_features(conn, days[0], days[-1])
    assert _feature(conn, ids["0050"], days[20], "has_ex_right") is True
    assert abs(_feature(conn, ids["0050"], days[20], "ret_1d")) < 1e-6
    assert abs(_feature(conn, ids["0050"], days[25], "ret_5d")) < 1e-6


def test_volume_spike_with_cooldown(conn):
    days = _weekdays(45)
    vols = [1_000_000] * 45
    vols[30] = vols[31] = 6_000_000        # 連兩天爆量：只算第一天
    vols[40] = 6_000_000                   # 9 個交易日後再爆量：冷卻期（5 日）已過，再發一次
    ids, _ = _seed(conn, days, {"2330": [(100, v) for v in vols]})
    build_features(conn, days[0], days[-1])
    generate_signals(conn, days[25], days[-1])
    assert [d for _, d, _ in _signals(conn, "vol_spike")] == [days[30], days[40]]


def test_foreign_buy_streak_emits_once(conn):
    days = _weekdays(40)
    ids, src = _seed(conn, days, {"2317": [(100, 2_000_000)] * 40})
    for i, d in enumerate(days):
        net = 300_000 if 25 <= i <= 33 else -1_000     # 連續 9 天買超，佔成交量 15%
        conn.execute("INSERT INTO institutional_flows (stock_id, trade_date, foreign_net, source_id) VALUES (%s, %s, %s, %s)",
                     (ids["2317"], d, net, src))
    build_features(conn, days[0], days[-1])
    generate_signals(conn, days[20], days[-1])
    assert [d for _, d, _ in _signals(conn, "foreign_buy_streak")] == [days[29]]   # 第 5 天成立，只發一次


def test_daily_run_matches_full_rerun(conn):
    days = _weekdays(45)
    vols = [1_000_000] * 45
    vols[30] = vols[31] = vols[40] = 6_000_000
    _seed(conn, days, {"2330": [(100, v) for v in vols]})
    build_features(conn, days[0], days[-1])
    full = generate_signals(conn, days[25], days[-1])
    again = generate_signals(conn, days[25], days[-1])
    assert full == again                                                  # 重跑結果相同
    conn.execute("DELETE FROM market_signals")
    for d in days[25:]:                                                    # 每天只算當天
        generate_signals(conn, d, d)
    assert [d for _, d, _ in _signals(conn, "vol_spike")] == [days[30], days[40]]


def _rights(conn, sid, src, ex, prev, ref, div_ref):
    conn.execute("""INSERT INTO corporate_actions (stock_id, ex_date, action_type, prev_close, ref_price, value,
                    div_ref, source_id) VALUES (%s, %s, 'rights', %s, %s, %s, %s, %s)""",
                 (sid, ex, prev, ref, prev - ref, div_ref, src))


def test_cash_capital_increase_uses_the_reference_the_market_traded_on(conn):
    days = _weekdays(70)
    # 天瀚型：參考價 30（含現金增資）但市場照 44.40 交易、當天漲停 48.80 → 不能還原成 +62%
    # 正達型：市場照除權後價格交易，當天 79.60 → 用減除股利參考價會變成 −14.9%，要用參考價（−8.2%）
    ids, src = _seed(conn, days, {
        "6225": [(44.40, 1_000_000)] * 65 + [(48.80, 1_000_000, 0)] + [(48.80, 1_000_000)] * 4,
        "3149": [(93.50, 1_000_000)] * 65 + [(79.60, 1_000_000, 0)] + [(79.60, 1_000_000)] * 4,
    })
    _rights(conn, ids["6225"], src, days[65], 44.40, 30.04, 44.40)
    _rights(conn, ids["3149"], src, days[65], 93.50, 86.68, 93.50)
    build_features(conn, days[0], days[-1])
    assert abs(float(_feature(conn, ids["6225"], days[65], "ret_1d")) - (48.80 / 44.40 - 1)) < 1e-6
    assert float(_feature(conn, ids["6225"], days[68], "low60")) == 44.40        # 前 60 日低點不被壓成 30 左右
    assert abs(float(_feature(conn, ids["3149"], days[65], "ret_1d")) - (79.60 / 86.68 - 1)) < 1e-6


def test_trust_big_buy_needs_real_money(conn):
    days = _weekdays(30)
    ids, src = _seed(conn, days, {"7822": [(920, 300_000)] * 30, "2882": [(60, 3_000_000)] * 30})
    for d in days:
        # 倍利科型：36 張、佔量 12%，但只有 3,312 萬元；國泰金型：900 張、5,400 萬元
        conn.execute("INSERT INTO institutional_flows (stock_id, trade_date, trust_net, source_id) VALUES (%s, %s, %s, %s)",
                     (ids["7822"], d, 36_000 if d == days[-1] else 0, src))
        conn.execute("INSERT INTO institutional_flows (stock_id, trade_date, trust_net, source_id) VALUES (%s, %s, %s, %s)",
                     (ids["2882"], d, 900_000 if d == days[-1] else 0, src))
    build_features(conn, days[0], days[-1])
    generate_signals(conn, days[-1], days[-1])
    assert [s for s, _, t in _signals(conn, "trust_big_buy")] == ["2882"]
