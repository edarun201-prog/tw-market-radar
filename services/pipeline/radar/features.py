"""每日特徵（daily_features）：量能、報酬、20／60 日高低點，全部以「還原價」計算。

還原方式
  - 除權息生效日＝除權息日當天或之後的第一個交易日（颱風休市順延時，TWT49U 仍寫原日期，
    但實際在下一個交易日生效，見 docs/crawler-tasks.md）
  - 還原係數 f＝除權息參考價 ÷ 除權息前收盤價；日期 s 的價格乘上 g(s)＝Π(生效日 > s 的 f)
  - 含現金增資（減除股利參考價 ≠ 除權息參考價，約 4% 的除權息）：官方成交範圍是
    除權息參考價 × 0.9～減除股利參考價 × 1.1，市場照哪個基準交易因股而異，取生效日收盤較接近的那個（推估）
  - 減資、分割不在 TWT49U 裡，從行情表的官方漲跌反推（見 SQL 的 derived）；has_ex_right 兩種都算
  - 報酬＝還原收盤的比值；未來才發生的除權息在比值中互相抵銷，所以數值不會因之後的除權息而改變
  - high20／low20／high60／low60 換算成「當天 t 的價格水準」（除以 g(t)），可以直接和當天原始收盤價比較
  - 高低點與均量都只看「前 N 個交易日」，不含當天，方便判斷「今天創新高」「今天爆量」

限制：配股會改變股數，但成交量沒有還原（配股佔比很小，量比仍可用）。
"""
from __future__ import annotations

from datetime import date, timedelta

# 60 個交易日的高低點＋20 日報酬，往前多抓 150 天曆日一定夠
LOOKBACK_DAYS = 150

_SQL = """
WITH ev AS (
    SELECT ca.stock_id, e.eff_date,
           CASE
             -- 一般除權息：減除股利參考價＝除權息參考價，直接用
             WHEN ca.div_ref IS NULL OR ca.div_ref = ca.ref_price OR p.close IS NULL
               THEN ln(ca.ref_price::float8 / ca.prev_close::float8)
             -- 含現金增資：官方允許的成交範圍是「除權息參考價 × 0.9」到「減除股利參考價 × 1.1」，
             -- 市場實際照哪個基準交易因股而異（天瀚照 44.40 漲停；正達、嘉基照除權後價格），
             -- 所以取「生效日收盤比較接近」的那個基準還原（依實際成交推估，不是官方規則）
             WHEN abs(ln(p.close::float8 / ca.div_ref::float8)) < abs(ln(p.close::float8 / ca.ref_price::float8))
               THEN ln(ca.div_ref::float8 / ca.prev_close::float8)
             ELSE ln(ca.ref_price::float8 / ca.prev_close::float8)
           END AS ln_f
      FROM corporate_actions ca
      CROSS JOIN LATERAL (SELECT min(t.trade_date) AS eff_date FROM trading_calendar t
                           WHERE t.is_open AND t.trade_date >= ca.ex_date) e
      LEFT JOIN daily_prices p ON p.stock_id = ca.stock_id AND p.trade_date = e.eff_date
     WHERE ca.prev_close > 0 AND ca.ref_price > 0
       -- 交易日曆從資料起點才有；更早的除權息會被誤算成起點那天生效，直接排除
       AND ca.ex_date >= (SELECT min(trade_date) FROM trading_calendar)
),
twt49u AS (
    SELECT stock_id, eff_date, sum(ln_f) AS ln_f FROM ev
     WHERE eff_date BETWEEN %(from)s AND %(end)s
     GROUP BY 1, 2
),
-- TWT49U 不含減資、分割（官方附註），改從行情表反推：官方漲跌是相對「調整後參考價」計算，
-- 收盤 − 漲跌 ≠ 前一列收盤，就代表當天有價格調整，係數＝(收盤 − 漲跌) ÷ 前一列收盤。
prev AS (
    SELECT stock_id, trade_date, close, change, is_no_compare,
           lag(close) OVER (PARTITION BY stock_id ORDER BY trade_date) AS prev_close
      FROM daily_prices WHERE trade_date BETWEEN %(from)s AND %(end)s
),
derived AS (
    SELECT stock_id, trade_date AS eff_date, ln((close - change)::float8 / prev_close::float8) AS ln_f
      FROM prev
     WHERE NOT is_no_compare AND change IS NOT NULL AND close - change > 0 AND prev_close > 0
       AND abs((close - change) / prev_close - 1) > 0.005
),
events AS (
    SELECT stock_id, eff_date, ln_f FROM twt49u
    UNION ALL
    SELECT d.stock_id, d.eff_date, d.ln_f FROM derived d
     WHERE NOT EXISTS (SELECT 1 FROM twt49u t WHERE t.stock_id = d.stock_id AND t.eff_date = d.eff_date)
),
-- 價格列與除權息事件放在同一條時間軸，同一天事件排在價格前面：cum 就是「生效日 <= 當天」的累積
timeline AS (
    SELECT stock_id, trade_date AS d, 1 AS kind, 0::float8 AS ln_f
      FROM daily_prices WHERE trade_date BETWEEN %(from)s AND %(end)s
    UNION ALL
    SELECT stock_id, eff_date, 0, ln_f FROM events
),
adj AS (
    SELECT stock_id, d, kind,
           exp(sum(ln_f) OVER (PARTITION BY stock_id)
               - sum(ln_f) OVER (PARTITION BY stock_id ORDER BY d, kind ROWS UNBOUNDED PRECEDING)) AS g
      FROM timeline
),
base AS (
    SELECT p.stock_id, p.trade_date, p.volume, p.turnover,
           p.close::float8 * a.g AS ac, p.high::float8 * a.g AS ah, p.low::float8 * a.g AS al, a.g,
           (e.stock_id IS NOT NULL) AS has_ex
      FROM daily_prices p
      JOIN adj a ON a.stock_id = p.stock_id AND a.d = p.trade_date AND a.kind = 1
      LEFT JOIN events e ON e.stock_id = p.stock_id AND e.eff_date = p.trade_date
     WHERE p.trade_date BETWEEN %(from)s AND %(end)s
),
w AS (
    SELECT b.*,
           avg(volume) OVER prev20 AS vol_avg20, count(*) OVER prev20 AS n20,
           avg(turnover) OVER prev20 AS turnover_avg20,
           lag(ac, 1) OVER s AS ac1, lag(ac, 5) OVER s AS ac5, lag(ac, 20) OVER s AS ac20,
           max(ah) OVER prev20 AS h20, min(al) OVER prev20 AS l20, count(ah) OVER prev20 AS nh20,
           max(ah) OVER prev60 AS h60, min(al) OVER prev60 AS l60, count(ah) OVER prev60 AS nh60
      FROM base b
    WINDOW s AS (PARTITION BY stock_id ORDER BY trade_date),
           prev20 AS (s ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING),
           prev60 AS (s ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING)
)
INSERT INTO daily_features (stock_id, trade_date, vol_avg20, vol_ratio, turnover_avg20, ret_1d, ret_5d, ret_20d,
                            high20, low20, high60, low60, has_ex_right, computed_at)
SELECT stock_id, trade_date,
       CASE WHEN n20 = 20 THEN vol_avg20 END,
       CASE WHEN n20 = 20 AND vol_avg20 > 0 THEN volume / vol_avg20 END,
       CASE WHEN n20 = 20 THEN turnover_avg20 END,
       ac / NULLIF(ac1, 0) - 1, ac / NULLIF(ac5, 0) - 1, ac / NULLIF(ac20, 0) - 1,
       CASE WHEN nh20 = 20 THEN h20 / g END, CASE WHEN nh20 = 20 THEN l20 / g END,
       CASE WHEN nh60 = 60 THEN h60 / g END, CASE WHEN nh60 = 60 THEN l60 / g END,
       has_ex, now()
  FROM w
 WHERE trade_date BETWEEN %(start)s AND %(end)s
ON CONFLICT (stock_id, trade_date) DO UPDATE SET
    vol_avg20 = EXCLUDED.vol_avg20, vol_ratio = EXCLUDED.vol_ratio, turnover_avg20 = EXCLUDED.turnover_avg20,
    ret_1d = EXCLUDED.ret_1d, ret_5d = EXCLUDED.ret_5d, ret_20d = EXCLUDED.ret_20d,
    high20 = EXCLUDED.high20, low20 = EXCLUDED.low20, high60 = EXCLUDED.high60, low60 = EXCLUDED.low60,
    has_ex_right = EXCLUDED.has_ex_right, computed_at = now()
"""


def build_features(conn, start: date, end: date) -> int:
    """計算 start～end 每個交易日的特徵；重跑會覆蓋，結果相同。回傳寫入列數。"""
    cur = conn.execute(_SQL, {"from": start - timedelta(days=LOOKBACK_DAYS), "start": start, "end": end})
    return cur.rowcount
