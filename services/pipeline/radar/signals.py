"""異常訊號（market_signals）：以 daily_features 與法人資料判斷，只描述「發生了什麼」，不是買賣建議。

每筆訊號都存判斷依據（evidence）與引擎版本（engine_version）；改門檻就換版本號，新舊結果可以並存比較，
網站與 AI 解讀都只引用 evidence 裡的數字。

範圍：只看普通股（security_type = 'stock'），ETF、特別股、存託憑證不產生訊號。
門檻依 2025-09～2026-09 一年回測調整，目標是全市場每天 20～60 則（見 docs/signals.md）。
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

ENGINE_VERSION = "v2"   # v2：投信大買加金額門檻；還原價改用減除股利參考價（現金增資）

# 門檻（v1）
VOL_RATIO_MIN = 4.0             # 量比：今日成交量 ÷ 前 20 日均量
MIN_TURNOVER = 100_000_000      # 成交金額下限 1 億元：排除冷門股的假爆量
RET_5D_ABS = 0.20               # 5 日報酬 ±20%
STREAK_DAYS = 5                 # 外資連續同方向天數
STREAK_SHARE = 0.10             # 連續期間外資買（賣）超佔成交量
TRUST_SHARE = 0.10              # 投信當日買超佔成交量
TRUST_MIN_AMOUNT = 50_000_000   # 投信買超金額下限 5,000 萬元：只看佔量會把高價股的幾十張算成「大買」（v1 的倍利科 36 張）

TYPES = {
    "vol_spike": "量能爆增",
    "high_60": "創 60 日新高",
    "low_60": "創 60 日新低",
    "surge_5d": "5 日急漲",
    "plunge_5d": "5 日急跌",
    "foreign_buy_streak": "外資連買",
    "foreign_sell_streak": "外資連賣",
    "trust_big_buy": "投信大買",
}

_BASE = """
    FROM daily_features f
    JOIN daily_prices p ON p.stock_id = f.stock_id AND p.trade_date = f.trade_date
    JOIN stocks s ON s.id = f.stock_id AND s.security_type = 'stock'
   WHERE f.trade_date BETWEEN %(start)s AND %(end)s AND p.close IS NOT NULL AND p.volume > 0
"""

_RULES = {
    "vol_spike": f"""
    SELECT f.stock_id, f.trade_date, 'vol_spike', f.vol_ratio, %(vol_ratio)s,
           jsonb_build_object('volume', p.volume, 'vol_avg20', f.vol_avg20, 'turnover', p.turnover,
                              'close', p.close, 'ret_1d', f.ret_1d)
    {_BASE} AND f.vol_ratio >= %(vol_ratio)s AND p.turnover >= %(min_turnover)s""",

    "high_60": f"""
    SELECT f.stock_id, f.trade_date, 'high_60', p.close, f.high60,
           jsonb_build_object('close', p.close, 'prev_high60', f.high60, 'ret_20d', f.ret_20d, 'turnover', p.turnover)
    {_BASE} AND p.close > f.high60 AND p.turnover >= %(min_turnover)s""",

    "low_60": f"""
    SELECT f.stock_id, f.trade_date, 'low_60', p.close, f.low60,
           jsonb_build_object('close', p.close, 'prev_low60', f.low60, 'ret_20d', f.ret_20d, 'turnover', p.turnover)
    {_BASE} AND p.close < f.low60 AND p.turnover >= %(min_turnover)s""",

    "surge_5d": f"""
    SELECT f.stock_id, f.trade_date, 'surge_5d', f.ret_5d, %(ret5)s,
           jsonb_build_object('ret_5d', f.ret_5d, 'close', p.close, 'has_ex_right', f.has_ex_right)
    {_BASE} AND f.ret_5d >= %(ret5)s""",

    "plunge_5d": f"""
    SELECT f.stock_id, f.trade_date, 'plunge_5d', f.ret_5d, -%(ret5)s,
           jsonb_build_object('ret_5d', f.ret_5d, 'close', p.close, 'has_ex_right', f.has_ex_right)
    {_BASE} AND f.ret_5d <= -%(ret5)s""",

    "trust_big_buy": f"""
    SELECT f.stock_id, f.trade_date, 'trust_big_buy', i.trust_net::float8 / p.volume, %(trust_share)s,
           jsonb_build_object('trust_net', i.trust_net, 'amount', i.trust_net * p.close, 'volume', p.volume,
                              'turnover', p.turnover, 'close', p.close)
    {_BASE.replace("WHERE", "JOIN institutional_flows i ON i.stock_id = f.stock_id AND i.trade_date = f.trade_date WHERE")}
      AND i.trust_net > 0 AND i.trust_net >= %(trust_share)s * p.volume AND p.turnover >= %(min_turnover)s
      AND i.trust_net * p.close >= %(trust_min_amount)s""",
}

# 外資連續買／賣超：以行情表為基準（沒有法人資料的交易日算 0，會中斷連續），gaps-and-islands 算連續天數
_STREAK_SQL = """
WITH d AS (
    SELECT p.stock_id, p.trade_date, p.volume, p.turnover, p.close,
           coalesce(i.foreign_net, 0) AS net, sign(coalesce(i.foreign_net, 0)) AS dir
      FROM daily_prices p
      JOIN stocks s ON s.id = p.stock_id AND s.security_type = 'stock'
      LEFT JOIN institutional_flows i ON i.stock_id = p.stock_id AND i.trade_date = p.trade_date
     WHERE p.trade_date BETWEEN %(from)s AND %(end)s
),
g AS (
    SELECT d.*, row_number() OVER (PARTITION BY stock_id ORDER BY trade_date)
              - row_number() OVER (PARTITION BY stock_id, dir ORDER BY trade_date) AS grp
      FROM d
),
st AS (
    SELECT g.*,
           count(*) OVER run AS streak, sum(net) OVER run AS cum_net, sum(volume) OVER run AS cum_volume
      FROM g
    WINDOW run AS (PARTITION BY stock_id, dir, grp ORDER BY trade_date ROWS UNBOUNDED PRECEDING)
)
SELECT stock_id, trade_date,
       CASE WHEN dir > 0 THEN 'foreign_buy_streak' ELSE 'foreign_sell_streak' END,
       streak::numeric, %(streak_days)s,
       jsonb_build_object('streak_days', streak, 'cum_foreign_net', cum_net, 'cum_volume', cum_volume,
                          'share', cum_net::float8 / NULLIF(cum_volume, 0), 'close', close)
  FROM st
 WHERE trade_date BETWEEN %(start)s AND %(end)s AND dir <> 0 AND streak >= %(streak_days)s
   AND abs(cum_net) >= %(streak_share)s * cum_volume AND turnover >= %(min_turnover)s AND close IS NOT NULL
"""


# 冷卻期（交易日）：同一檔、同一種訊號在前 N 個交易日內已經成立過，就不再重複發出。
# 條件持續成立的情況（連買、強勢股天天創高）因此只會在「剛成立」那天出現一次。
COOLDOWN = {"vol_spike": 5, "high_60": 20, "low_60": 20, "surge_5d": 5, "plunge_5d": 5,
            "foreign_buy_streak": 5, "foreign_sell_streak": 5, "trust_big_buy": 5}
_CANDIDATE_LOOKBACK_DAYS = 45   # 曆日；足夠涵蓋 20 個交易日的冷卻期

_EMIT_SQL = """
WITH days AS (SELECT trade_date, row_number() OVER (ORDER BY trade_date) AS idx
                FROM trading_calendar WHERE is_open),
     cool(signal_type, n) AS (SELECT * FROM jsonb_each_text(%(cooldown)s::jsonb))
INSERT INTO market_signals (stock_id, trade_date, signal_type, value, threshold, evidence, engine_version)
SELECT c.stock_id, c.trade_date, c.signal_type, c.value, c.threshold, c.evidence, %(version)s
  FROM cand c
  JOIN days cd ON cd.trade_date = c.trade_date
  JOIN cool k ON k.signal_type = c.signal_type
 WHERE c.trade_date BETWEEN %(start)s AND %(end)s
   AND NOT EXISTS (
        SELECT 1 FROM cand e JOIN days ed ON ed.trade_date = e.trade_date
         WHERE e.stock_id = c.stock_id AND e.signal_type = c.signal_type
           AND ed.idx < cd.idx AND ed.idx >= cd.idx - k.n::int)
"""


def _params(start: date, end: date) -> dict:
    return {"start": start, "end": end, "from": start - timedelta(days=90),
            "vol_ratio": VOL_RATIO_MIN, "min_turnover": MIN_TURNOVER, "ret5": RET_5D_ABS,
            "streak_days": STREAK_DAYS, "streak_share": STREAK_SHARE, "trust_share": TRUST_SHARE,
            "trust_min_amount": TRUST_MIN_AMOUNT}


def generate_signals(conn, start: date, end: date, version: str = ENGINE_VERSION) -> Counter:
    """重算 start～end 的訊號（先刪同版本再寫入，重跑結果相同）。回傳各類型筆數。

    先算出 start 往前 45 天起的所有「候選」（條件成立），再依冷卻期決定哪些真的發出；
    每天只算當天也會得到和整段重算相同的結果。
    """
    import json

    cand_start = start - timedelta(days=_CANDIDATE_LOOKBACK_DAYS)
    cand_params = _params(cand_start, end)
    params = {"start": start, "end": end, "version": version, "cooldown": json.dumps(COOLDOWN)}
    with conn.transaction():
        conn.execute("CREATE TEMP TABLE cand (stock_id int, trade_date date, signal_type text, value numeric, "
                     "threshold numeric, evidence jsonb) ON COMMIT DROP")
        for sql in list(_RULES.values()) + [_STREAK_SQL]:
            conn.execute(f"INSERT INTO cand SELECT * FROM ({sql}) AS x", cand_params)
        conn.execute("DELETE FROM market_signals WHERE trade_date BETWEEN %(start)s AND %(end)s "
                     "AND engine_version = %(version)s", params)
        conn.execute(_EMIT_SQL, params)
    rows = conn.execute("SELECT signal_type, count(*) FROM market_signals WHERE trade_date BETWEEN %(start)s AND %(end)s "
                        "AND engine_version = %(version)s GROUP BY 1", params).fetchall()
    return Counter(dict(rows))


def daily_counts_report(conn, start: date, end: date, version: str = ENGINE_VERSION) -> list[tuple]:
    """回測用：每種訊號每天幾則（中位數、90 百分位、最多），以及全部加總。"""
    return conn.execute("""
        WITH days AS (SELECT trade_date FROM trading_calendar WHERE is_open AND trade_date BETWEEN %(start)s AND %(end)s),
             types AS (SELECT DISTINCT signal_type FROM market_signals WHERE engine_version = %(version)s),
             per AS (
                SELECT t.signal_type, d.trade_date, count(m.id) AS n
                  FROM days d CROSS JOIN types t
                  LEFT JOIN market_signals m ON m.trade_date = d.trade_date AND m.signal_type = t.signal_type
                                            AND m.engine_version = %(version)s
                 GROUP BY 1, 2
                UNION ALL
                SELECT '合計', d.trade_date, count(m.id)
                  FROM days d LEFT JOIN market_signals m ON m.trade_date = d.trade_date AND m.engine_version = %(version)s
                 GROUP BY 2)
        SELECT signal_type, percentile_cont(0.5) WITHIN GROUP (ORDER BY n), percentile_cont(0.9) WITHIN GROUP (ORDER BY n),
               max(n), sum(n)
          FROM per GROUP BY 1 ORDER BY 1
    """, {"start": start, "end": end, "version": version}).fetchall()
