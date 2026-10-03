"""跨資料集的一致性檢查：同一件事在兩份官方報表裡要對得上。

check_corporate_actions：除權息生效日（休市則順延到下一個交易日）的成交價，必須落在該筆除權息的漲跌停範圍內，
否則表示除權息紀錄（日期或參考價）和行情資料矛盾。
  - 有官方漲跌停價（TWT49U 的漲停／跌停價格）就用官方的：含現金增資的除權，漲跌停以「減除股利參考價」為基準，
    用除權息參考價 × (1 ± 10%) 推算會誤報（實測：天瀚、盟立、聯友金屬、同泰）。
  - 沒有官方值時，才用 開盤競價基準（再沒有就用參考價）× (1 ± 10%) ± 一個升降單位 推算。
  - 上下都檢查；只比對同一檔股票；當天沒有成交就跳過；除權息日早於交易日曆起點的不檢查（無法判斷生效日）。
只回報、不修改資料：矛盾通常代表來源有更正或解析錯位，需要人工看原始檔。
"""
from __future__ import annotations

from datetime import date

from psycopg.rows import dict_row

LIMIT = 0.10   # 一般股票漲跌幅限制

# 臺灣證券交易所股票升降單位（ETF：50 元以下 0.01、以上 0.05）
_TICK = """CASE WHEN s.security_type = 'etf' THEN CASE WHEN b.base < 50 THEN 0.01 ELSE 0.05 END
                 WHEN b.base < 10 THEN 0.01 WHEN b.base < 50 THEN 0.05 WHEN b.base < 100 THEN 0.1
                 WHEN b.base < 500 THEN 0.5 WHEN b.base < 1000 THEN 1 ELSE 5 END"""

_SQL = f"""
WITH actions AS (
    SELECT ca.*, (SELECT min(t.trade_date) FROM trading_calendar t
                   WHERE t.is_open AND t.trade_date >= ca.ex_date) AS eff_date
      FROM corporate_actions ca
     WHERE ca.ex_date >= (SELECT min(trade_date) FROM trading_calendar)
       AND ca.ex_date BETWEEN %(start)s AND %(end)s
),
bands AS (
    SELECT a.*, s.symbol, s.name, s.security_type, p.high, p.low,
           coalesce(a.open_ref, a.div_ref, a.ref_price) AS base
      FROM actions a
      JOIN stocks s ON s.id = a.stock_id
      JOIN daily_prices p ON p.stock_id = a.stock_id AND p.trade_date = a.eff_date
     WHERE p.high IS NOT NULL AND p.low IS NOT NULL
),
checked AS (
    SELECT b.*,
           coalesce(b.limit_up, b.base * (1 + %(limit)s) + {_TICK}) AS upper,
           coalesce(b.limit_down, b.base * (1 - %(limit)s) - {_TICK}) AS lower,
           (b.limit_up IS NOT NULL) AS official
      FROM bands b JOIN stocks s ON s.id = b.stock_id
)
SELECT symbol, name, ex_date, eff_date, ref_price, upper, lower, high, low, official,
       CASE WHEN high > upper THEN '最高價超過漲停' ELSE '最低價低於跌停' END AS problem
  FROM checked
 WHERE high > upper OR low < lower
 ORDER BY ex_date, symbol
"""


def check_corporate_actions(conn, start: date = date(1900, 1, 1), end: date = date(9999, 12, 31),
                            limit: float = LIMIT) -> list[dict]:
    """回傳矛盾清單；空清單代表除權息紀錄和成交價一致。"""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(_SQL, {"start": start, "end": end, "limit": limit}).fetchall()


# ---- 還原價：調整日的單日報酬不可能超過漲跌幅 ------------------------------------------------
MAX_ADJUSTED_MOVE = 0.105   # 10% 漲跌幅＋檔位進位的餘裕


def check_adjusted_returns(conn, start: date = date(1900, 1, 1), end: date = date(9999, 12, 31),
                           max_move: float = MAX_ADJUSTED_MOVE) -> list[dict]:
    """有價格調整（除權息、減資、分割）的日子，還原後的日報酬若超過漲跌幅，代表還原係數用錯了。
    只看普通股（新上市前 5 日、部分 ETF 沒有漲跌幅限制）。"""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("""
            SELECT s.symbol, s.name, f.trade_date, f.ret_1d, p.close
              FROM daily_features f
              JOIN stocks s ON s.id = f.stock_id AND s.security_type = 'stock'
              JOIN daily_prices p ON p.stock_id = f.stock_id AND p.trade_date = f.trade_date
             WHERE f.has_ex_right AND abs(f.ret_1d) > %(m)s AND f.trade_date BETWEEN %(s)s AND %(e)s
             ORDER BY f.trade_date, s.symbol""", {"m": max_move, "s": start, "e": end}).fetchall()
