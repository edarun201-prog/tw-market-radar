"""一致性檢查：除權息紀錄 vs 生效日成交價。"""
from datetime import timedelta

from radar.audit import check_corporate_actions
from radar.features import build_features

from .test_analytics import _seed, _weekdays


def _price(conn, sid, d, high, low):
    conn.execute("UPDATE daily_prices SET high=%s, low=%s WHERE stock_id=%s AND trade_date=%s", (high, low, sid, d))


def _action(conn, sid, src, ex, up=110, down=90, open_ref=None, ref=100):
    conn.execute("""INSERT INTO corporate_actions (stock_id, ex_date, action_type, prev_close, ref_price, value,
                    limit_up, limit_down, open_ref, source_id) VALUES (%s, %s, 'dividend', %s, %s, 1, %s, %s, %s, %s)""",
                 (sid, ex, ref + 1, ref, up, down, open_ref, src))


def test_flags_both_sides_but_not_exact_limit(conn):
    days = _weekdays(10)
    ids, src = _seed(conn, days, {"1101": [(100, 1_000_000)] * 10, "1102": [(100, 1_000_000)] * 10,
                                  "1103": [(100, 1_000_000)] * 10, "1104": [(100, 1_000_000)] * 10})
    for sym, (hi, lo) in {"1101": (110, 95), "1102": (115, 95), "1103": (105, 85), "1104": (105, 95)}.items():
        _action(conn, ids[sym], src, days[5])
        _price(conn, ids[sym], days[5], hi, lo)
    bad = {b["symbol"]: b["problem"] for b in check_corporate_actions(conn)}
    assert bad == {"1102": "最高價超過漲停", "1103": "最低價低於跌停"}   # 1101 剛好漲停、1104 在範圍內


def test_postponed_day_other_stock_no_trade_and_before_calendar(conn):
    days = _weekdays(12)
    typhoon = days[6]
    open_days = [d for d in days if d != typhoon]
    ids, src = _seed(conn, open_days, {"2891": [(100, 1_000_000)] * 11, "2330": [(100, 1_000_000)] * 11},
                     closed=[typhoon])
    _action(conn, ids["2891"], src, typhoon)                   # 颱風休市：檢查下一個交易日
    _price(conn, ids["2891"], days[7], 120, 95)
    _price(conn, ids["2330"], days[7], 150, 50)                # 別檔股票的價格不能拿來比
    _action(conn, ids["2330"], src, days[0] - timedelta(days=3))   # 早於交易日曆：無法判斷生效日，不檢查
    conn.execute("UPDATE daily_prices SET high=NULL, low=NULL, close=NULL WHERE stock_id=%s AND trade_date=%s",
                 (ids["2330"], days[0]))
    bad = check_corporate_actions(conn)
    assert [(b["symbol"], b["eff_date"]) for b in bad] == [("2891", days[7])]


def test_fallback_formula_uses_open_ref_when_no_official_limits(conn):
    days = _weekdays(6)
    ids, src = _seed(conn, days, {"2464": [(180, 1_000_000)] * 6})
    # 現金增資：參考價 150、開盤競價基準 184 → 推算上限 184 × 1.1 + 0.5 = 202.9
    _action(conn, ids["2464"], src, days[3], up=None, down=None, open_ref=184, ref=150)
    _price(conn, ids["2464"], days[3], 202, 180)
    assert check_corporate_actions(conn) == []
    _price(conn, ids["2464"], days[3], 204, 180)
    assert [b["problem"] for b in check_corporate_actions(conn)] == ["最高價超過漲停"]


def test_features_ignore_actions_before_calendar_start(conn):
    days = _weekdays(10)
    ids, src = _seed(conn, days, {"6446": [(100, 1_000_000)] * 10})
    _action(conn, ids["6446"], src, days[0] - timedelta(days=5))
    build_features(conn, days[0], days[-1])
    assert not any(r[0] for r in conn.execute("SELECT has_ex_right FROM daily_features").fetchall())


def test_adjusted_return_beyond_price_limit_is_flagged(conn):
    from radar.audit import check_adjusted_returns
    days = _weekdays(8)
    ids, src = _seed(conn, days, {"6225": [(44.40, 1_000_000)] * 5 + [(48.80, 1_000_000, 0)] * 3})
    conn.execute("""INSERT INTO corporate_actions (stock_id, ex_date, action_type, prev_close, ref_price, value, source_id)
                    VALUES (%s, %s, 'rights', 44.40, 30.04, 14.36, %s)""", (ids["6225"], days[5], src))   # 沒有 div_ref：只能用參考價
    build_features(conn, days[0], days[-1])
    bad = check_adjusted_returns(conn)
    assert [(b["symbol"], b["trade_date"]) for b in bad] == [("6225", days[5])]              # +62%：還原係數用錯
