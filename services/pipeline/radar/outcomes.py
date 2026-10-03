"""訊號回測（Phase 3）：每一筆訊號出現之後，股價實際怎麼走，並和同一天「全部普通股」比較。

- 報酬用還原後的日報酬（daily_features.ret_1d）連乘，除權息、減資、分割不會被當成漲跌。
- h5／h20／h60：從訊號當天收盤起算，之後 5／20／60 個「這檔有交易」的日子。
- d20：盤後才看得到訊號，最快隔天才買得到——從隔天收盤起算的 20 日報酬，用來看「晚一步」差多少。
- 比較基準：同一天所有普通股的等權平均、中位數與上漲比例（market_forward_returns）。
- 只描述過去發生了什麼，不代表之後，也不是買賣建議。每次整批重算，結果相同。
"""
from __future__ import annotations

HORIZONS = {"h5": (1, 5), "h20": (1, 20), "h60": (1, 60), "d20": (2, 21)}   # 視窗：之後第幾列到第幾列
# 一買一賣的成本：手續費 0.1425% × 2（未打折）＋ 證券交易稅 0.3%
ROUND_TRIP_COST = 0.001425 * 2 + 0.003


def _fwd_sql() -> str:
    cols, windows = [], []
    for name, (a, b) in HORIZONS.items():
        n = b - a + 1
        cols.append(f"CASE WHEN count(*) OVER {name} = {n} THEN exp(sum(lr) OVER {name}) - 1 END AS {name}")
        cols.append(f"CASE WHEN count(*) OVER {name} = {n} THEN max(trade_date) OVER {name} END AS e_{name}")
        windows.append(f"{name} AS (PARTITION BY stock_id ORDER BY trade_date ROWS BETWEEN {a} FOLLOWING AND {b} FOLLOWING)")
    return f"""
        CREATE TEMP TABLE fwd ON COMMIT DROP AS
        WITH r AS (
            SELECT f.stock_id, f.trade_date, ln(1 + coalesce(f.ret_1d, 0)::float8) AS lr
              FROM daily_features f JOIN stocks s ON s.id = f.stock_id
             WHERE s.security_type = 'stock')
        SELECT stock_id, trade_date, {", ".join(cols)}
          FROM r
        WINDOW {", ".join(windows)}"""


_BASELINE_SQL = """
    INSERT INTO market_forward_returns (trade_date, horizon, n, mean, median, up_share)
    SELECT f.trade_date, x.h, count(*), avg(x.v), percentile_cont(0.5) WITHIN GROUP (ORDER BY x.v), avg((x.v > 0)::int)
      FROM fwd f
      CROSS JOIN LATERAL (VALUES ('h5', f.h5), ('h20', f.h20), ('h60', f.h60), ('d20', f.d20)) AS x(h, v)
     WHERE x.v IS NOT NULL
     GROUP BY f.trade_date, x.h
"""

_SIGNALS_SQL = """
    INSERT INTO signal_outcomes (signal_id, h5, h20, h60, d20, b5, b20, b60, bd20, e5, e20, e60, ed20)
    SELECT m.id, f.h5, f.h20, f.h60, f.d20, b.b5, b.b20, b.b60, b.bd20, f.e_h5, f.e_h20, f.e_h60, f.e_d20
      FROM market_signals m
      JOIN fwd f ON f.stock_id = m.stock_id AND f.trade_date = m.trade_date
      LEFT JOIN LATERAL (
            SELECT max(mean) FILTER (WHERE horizon = 'h5')  AS b5,  max(mean) FILTER (WHERE horizon = 'h20') AS b20,
                   max(mean) FILTER (WHERE horizon = 'h60') AS b60, max(mean) FILTER (WHERE horizon = 'd20') AS bd20
              FROM market_forward_returns WHERE trade_date = m.trade_date) b ON true
"""


def build_outcomes(conn) -> dict[str, int]:
    """整批重算所有訊號的後續報酬與比較基準；回傳各表寫入的列數。"""
    with conn.transaction():
        conn.execute(_fwd_sql())
        conn.execute("DELETE FROM market_forward_returns")
        base = conn.execute(_BASELINE_SQL).rowcount
        conn.execute("DELETE FROM signal_outcomes")
        sig = conn.execute(_SIGNALS_SQL).rowcount
    return {"market_forward_returns": base, "signal_outcomes": sig}
