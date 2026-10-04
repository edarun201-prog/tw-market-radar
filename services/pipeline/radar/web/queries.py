"""網站的唯讀查詢。網頁與 JSON API 共用；只讀資料表，不寫入（連線帳號本身也沒有寫入權限）。

單位：資料庫存「股」與「元」，這裡原樣回傳，換算成張、億元在顯示層（templates／formatting）做。
"""
from __future__ import annotations

from datetime import date

from psycopg.rows import dict_row

from radar.signals import ENGINE_VERSION, MIN_TURNOVER, STREAK_DAYS, TYPES

TAIEX = "發行量加權股價指數"
OTC_INDEX = "櫃買指數"
MARKET_LABELS = {"TWSE": "上市", "TPEX": "上櫃"}
# 這幾個是多個類股的合併指數，和單一類股重複，類股排行不列入
AGGREGATE_SECTORS = ["塑膠化工類指數", "水泥窯製類指數", "機電類指數", "電子工業類指數", "化學生技醫療類指數"]


def _all(conn, sql: str, params=None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params or {}).fetchall()


def _one(conn, sql: str, params=None) -> dict | None:
    rows = _all(conn, sql, params)
    return rows[0] if rows else None


# ---- 日期 ------------------------------------------------------------------------
def latest_trade_date(conn) -> date | None:
    return conn.execute("SELECT max(trade_date) FROM daily_prices").fetchone()[0]


def is_trading_day(conn, d: date) -> bool:
    """今天有沒有開盤：週末不開；休市日曆（證交所 OpenAPI）標成休市的不開；其他平日當作有開。"""
    if d.weekday() >= 5:
        return False
    row = conn.execute("SELECT is_open FROM trading_calendar WHERE trade_date = %s", (d,)).fetchone()
    return True if row is None else bool(row[0])


def neighbor_dates(conn, d: date) -> tuple[date | None, date | None]:
    row = conn.execute(
        """SELECT (SELECT max(trade_date) FROM market_breadth WHERE trade_date < %(d)s),
                  (SELECT min(trade_date) FROM market_breadth WHERE trade_date > %(d)s)""", {"d": d}).fetchone()
    return row[0], row[1]


def resolve_date(conn, d: date | None) -> date | None:
    """沒指定或指定到休市日時，回傳該日（含）以前最近的交易日。"""
    latest = latest_trade_date(conn)
    if d is None or latest is None or d >= latest:
        return latest
    return conn.execute("SELECT max(trade_date) FROM market_breadth WHERE trade_date <= %s", (d,)).fetchone()[0]


# ---- 首頁 ------------------------------------------------------------------------
def market_overview(conn, d: date) -> dict:
    index = _one(conn, "SELECT close, change, change_pct FROM market_indices WHERE index_code = %(i)s AND trade_date = %(d)s",
                 {"i": TAIEX, "d": d})
    history = _all(conn, """SELECT trade_date, close FROM market_indices WHERE index_code = %(i)s AND trade_date <= %(d)s
                            ORDER BY trade_date DESC LIMIT 120""", {"i": TAIEX, "d": d})[::-1]
    breadth = _one(conn, "SELECT * FROM market_breadth WHERE trade_date = %(d)s AND market = 'TWSE'", {"d": d})
    # 上櫃：櫃買指數與上櫃漲跌家數（櫃買中心每天的資料；沒有時是 None）
    otc = _one(conn, "SELECT close, change, change_pct FROM market_indices WHERE index_code = %(i)s AND trade_date = %(d)s",
               {"i": OTC_INDEX, "d": d})
    otc_breadth = _one(conn, "SELECT * FROM market_breadth WHERE trade_date = %(d)s AND market = 'TPEX'", {"d": d})
    sectors = _all(conn, """SELECT index_code AS name, close, change_pct FROM market_indices
                            WHERE trade_date = %(d)s AND index_code LIKE '%%類指數' AND index_code <> ALL(%(agg)s)
                              AND change_pct IS NOT NULL ORDER BY change_pct DESC""", {"d": d, "agg": AGGREGATE_SECTORS})
    summary = _one(conn, "SELECT content, provider, model, created_at FROM market_summaries WHERE trade_date = %(d)s", {"d": d})
    return {"date": d, "index": index, "index_history": history, "breadth": breadth, "sectors": sectors,
            "summary": summary, "otc": otc, "otc_breadth": otc_breadth}


def signals_on(conn, d: date, signal_type: str | None = None) -> list[dict]:
    return _all(conn, """
        SELECT m.trade_date, m.signal_type, m.value, m.threshold, m.evidence, s.symbol, s.name, i.name AS industry,
               p.close, p.change, p.turnover, f.ret_1d, f.ret_5d, f.vol_ratio,
               p.change / NULLIF(p.close - p.change, 0) AS day_pct   -- 官方漲跌幅（除權息日相對參考價）
          FROM market_signals m
          JOIN stocks s ON s.id = m.stock_id
          LEFT JOIN industries i ON i.id = s.industry_id
          LEFT JOIN daily_prices p ON p.stock_id = m.stock_id AND p.trade_date = m.trade_date
          LEFT JOIN daily_features f ON f.stock_id = m.stock_id AND f.trade_date = m.trade_date
         WHERE m.trade_date = %(d)s AND m.engine_version = %(v)s
           AND (%(t)s::text IS NULL OR m.signal_type = %(t)s)
         ORDER BY m.signal_type, abs(m.value) DESC, s.symbol""", {"d": d, "v": ENGINE_VERSION, "t": signal_type})


def group_signals(rows: list[dict]) -> list[dict]:
    """依 signals.TYPES 的順序分組，方便首頁逐類顯示。"""
    groups = {t: [] for t in TYPES}
    for r in rows:
        groups.setdefault(r["signal_type"], []).append(r)
    return [{"type": t, "label": TYPES.get(t, t), "rows": rs} for t, rs in groups.items()]


def industry_counts(rows: list[dict]) -> list[dict]:
    counts: dict[str, dict] = {}
    for r in rows:
        name = r["industry"] or "未分類"
        c = counts.setdefault(name, {"industry": name, "total": 0, "types": {}})
        c["total"] += 1
        label = TYPES.get(r["signal_type"], r["signal_type"])
        c["types"][label] = c["types"].get(label, 0) + 1
    return sorted(counts.values(), key=lambda x: -x["total"])


# ---- 股票頁 ----------------------------------------------------------------------
def stock_profile(conn, symbol: str) -> dict | None:
    """上市與上櫃的代號不會重複；萬一重複，以上市為準。"""
    return _one(conn, """SELECT s.id, s.symbol, s.name, s.security_type, s.listed_date, s.market, i.name AS industry
                           FROM stocks s LEFT JOIN industries i ON i.id = s.industry_id
                          WHERE s.symbol = %(s)s ORDER BY (s.market = 'TWSE') DESC LIMIT 1""", {"s": symbol})


def symbol_markets(conn, symbols: list[str]) -> dict[str, str]:
    """代號 → 市場（TWSE／TPEX）；盤中即時要用它決定向證交所查上市還是上櫃。"""
    if not symbols:
        return {}
    return dict(conn.execute("SELECT symbol, market FROM stocks WHERE symbol = ANY(%s) ORDER BY (market = 'TWSE')",
                             (symbols,)).fetchall())


# 個股查詢的 until：只看這天（含）以前的資料。匯出過去的日子時用，網站本身不指定＝看到最新。
def stock_prices(conn, stock_id: int, days: int = 250, until: date | None = None) -> list[dict]:
    return _all(conn, """SELECT * FROM (
                           SELECT p.trade_date, p.open, p.high, p.low, p.close, p.change, p.volume, p.turnover,
                                  p.is_no_compare, f.vol_ratio, f.ret_5d, f.ret_20d, f.high60, f.low60, f.has_ex_right
                             FROM daily_prices p
                             LEFT JOIN daily_features f ON f.stock_id = p.stock_id AND f.trade_date = p.trade_date
                            WHERE p.stock_id = %(id)s AND (%(until)s::date IS NULL OR p.trade_date <= %(until)s)
                            ORDER BY p.trade_date DESC LIMIT %(n)s) x
                         ORDER BY trade_date""", {"id": stock_id, "n": days, "until": until})


def stock_flows(conn, stock_id: int, days: int = 60, until: date | None = None) -> list[dict]:
    return _all(conn, """SELECT * FROM (
                           SELECT trade_date, foreign_net, trust_net, dealer_net, total_net
                             FROM institutional_flows
                            WHERE stock_id = %(id)s AND (%(until)s::date IS NULL OR trade_date <= %(until)s)
                            ORDER BY trade_date DESC LIMIT %(n)s) x
                         ORDER BY trade_date""", {"id": stock_id, "n": days, "until": until})


def stock_signals(conn, stock_id: int, until: date | None = None) -> list[dict]:
    """個股的訊號紀錄，附上每一筆之後的報酬（h5、h20）與同一天全部股票的 20 日平均（b20）；
    until 指定時，只給那天之前已經知道的結果。"""
    return _all(conn, """SELECT m.trade_date, m.signal_type, m.value, m.threshold, m.evidence,
                                CASE WHEN %(until)s::date IS NULL OR o.e5 <= %(until)s THEN o.h5 END AS h5,
                                CASE WHEN %(until)s::date IS NULL OR o.e20 <= %(until)s THEN o.h20 END AS h20,
                                CASE WHEN %(until)s::date IS NULL OR o.e20 <= %(until)s THEN o.b20 END AS b20
                           FROM market_signals m LEFT JOIN signal_outcomes o ON o.signal_id = m.id
                          WHERE m.stock_id = %(id)s AND m.engine_version = %(v)s
                            AND (%(until)s::date IS NULL OR m.trade_date <= %(until)s)
                          ORDER BY m.trade_date DESC""",
                {"id": stock_id, "v": ENGINE_VERSION, "until": until})


def stock_actions(conn, stock_id: int, until: date | None = None) -> list[dict]:
    # eff_date：實際生效的交易日（颱風等休市時順延；早於交易日曆起點的無法判斷，留空）
    return _all(conn, """SELECT ca.ex_date, ca.action_type, ca.prev_close, ca.ref_price, ca.div_ref, ca.value,
                                CASE WHEN ca.ex_date >= (SELECT min(trade_date) FROM trading_calendar) THEN
                                     (SELECT min(t.trade_date) FROM trading_calendar t
                                       WHERE t.is_open AND t.trade_date >= ca.ex_date) END AS eff_date
                           FROM corporate_actions ca
                          WHERE ca.stock_id = %(id)s AND (%(until)s::date IS NULL OR ca.ex_date <= %(until)s)
                          ORDER BY ca.ex_date DESC""", {"id": stock_id, "until": until})


def industry_peers(conn, stock_id: int, d: date, limit: int = 8) -> list[dict]:
    return _all(conn, """SELECT s.symbol, s.name, p.close, f.ret_1d, f.ret_20d
                           FROM stocks s JOIN stocks me ON me.id = %(id)s AND s.industry_id = me.industry_id AND s.id <> me.id
                           JOIN daily_prices p ON p.stock_id = s.id AND p.trade_date = %(d)s
                           LEFT JOIN daily_features f ON f.stock_id = s.id AND f.trade_date = %(d)s
                          WHERE s.security_type = 'stock'
                          ORDER BY p.turnover DESC NULLS LAST LIMIT %(n)s""", {"id": stock_id, "d": d, "n": limit})


# 條件搜尋共用的欄位：量比、5 日報酬、是否高於前 60 日最高價、外資是否連續 STREAK_DAYS 個交易日買超
_SCREEN_FROM = """
    WITH lastn AS (SELECT trade_date FROM trading_calendar WHERE is_open AND trade_date <= %(d)s
                    ORDER BY trade_date DESC LIMIT %(streak)s),
         fb AS (SELECT stock_id FROM institutional_flows WHERE trade_date IN (SELECT trade_date FROM lastn)
                 GROUP BY stock_id HAVING count(*) = %(streak)s AND min(foreign_net) > 0)
    SELECT s.symbol, s.name, i.name AS industry, s.security_type, s.market, p.close, p.turnover, f.vol_ratio, f.ret_5d,
           CASE WHEN p.close IS NOT NULL AND p.change IS NOT NULL AND p.close <> p.change
                THEN p.change / (p.close - p.change) END AS day_pct,
           coalesce(p.close > f.high60, false) AS new_high, (fb.stock_id IS NOT NULL) AS foreign_buy
      FROM daily_prices p JOIN stocks s ON s.id = p.stock_id
      LEFT JOIN industries i ON i.id = s.industry_id
      LEFT JOIN daily_features f ON f.stock_id = p.stock_id AND f.trade_date = p.trade_date
      LEFT JOIN fb ON fb.stock_id = p.stock_id
     WHERE p.trade_date = %(d)s"""


def day_quotes(conn, d: date) -> list[dict]:
    """某個交易日所有有行情的證券（匯出檔的離線搜尋與條件搜尋用）；排序和搜尋結果一樣，普通股在前。"""
    return _all(conn, _SCREEN_FROM + " ORDER BY (s.security_type = 'stock') DESC, length(s.symbol), s.symbol",
                {"d": d, "streak": STREAK_DAYS})


# 條件搜尋：名稱、條件（SQL）、排序。只看普通股（和雷達一樣），條件都是當天的原始數字，不做評分。
SCREENS = {
    "vol3": ("量比 ≥ 3 倍", "f.vol_ratio >= 3", "f.vol_ratio DESC"),
    "up5": ("今日漲幅 ≥ 5%", "p.change / NULLIF(p.close - p.change, 0) >= 0.05", "day_pct DESC"),
    "high60": ("創 60 日新高", "p.close > f.high60", "p.turnover DESC"),
    "fbuy": (f"外資連買 {STREAK_DAYS} 日以上", "fb.stock_id IS NOT NULL", "p.turnover DESC"),
}
# 條件搜尋比雷達寬鬆（沒有成交金額門檻、量比 3 倍而不是 4 倍、外資連買不看佔成交量），檔數會和雷達不同，頁面上要講清楚
SCREEN_NOTE = (f"條件搜尋比雷達寬鬆：沒有成交金額 {MIN_TURNOVER // 100_000_000} 億元的門檻，外資連買也不看佔成交量，"
               "所以檔數通常比今日雷達多。")


def screen_stocks(conn, d: date, key: str, limit: int = 50) -> tuple[list[dict], int]:
    """條件搜尋：回傳（前 limit 筆, 符合的總數）。"""
    _, where, order = SCREENS[key]
    rows = _all(conn, _SCREEN_FROM + f" AND s.security_type = 'stock' AND {where} ORDER BY {order} NULLS LAST, s.symbol",
                {"d": d, "streak": STREAK_DAYS})
    return rows[:limit], len(rows)


def data_status(conn, d: date) -> list[dict]:
    """進階模式的資料品質：這一天各資料集的抓取狀態、筆數、完成時間（ingestion_runs）。"""
    return _all(conn, """SELECT ds.code AS source, r.dataset, r.status, r.row_count, r.warning_count, r.finished_at
                           FROM ingestion_runs r JOIN data_sources ds ON ds.id = r.source_id
                          WHERE r.target_date = %(d)s ORDER BY ds.code, r.dataset""", {"d": d})


# ---- 搜尋 ------------------------------------------------------------------------
def search_stocks(conn, q: str, limit: int = 20) -> list[dict]:
    q = q.strip()
    if not q:
        return []
    return _all(conn, """SELECT s.symbol, s.name, i.name AS industry, s.security_type, s.market
                           FROM stocks s LEFT JOIN industries i ON i.id = s.industry_id
                          WHERE s.is_active AND (s.symbol LIKE %(prefix)s OR s.name ILIKE %(contains)s)
                          ORDER BY (s.symbol = %(q)s) DESC, (s.name = %(q)s) DESC, (s.security_type = 'stock') DESC,
                                   length(s.symbol), s.symbol
                          LIMIT %(n)s""",
                {"q": q, "prefix": q.replace("%", "") + "%", "contains": "%" + q.replace("%", "") + "%", "n": limit})


# ---- 訊號回測（Phase 3）：資料由 radar/outcomes.py 預先算好 --------------------------------
# as_of：只用那天之前「已經知道結果」的訊號（匯出過去日期的快照時用，避免看到之後的資料）。
_OUTCOME_ROWS = """
    SELECT m.signal_type, m.trade_date, x.h, x.v, x.b, mf.up_share AS bu, mf.median AS bm
      FROM market_signals m
      JOIN signal_outcomes so ON so.signal_id = m.id
      CROSS JOIN LATERAL (VALUES ('h5', so.h5, so.b5, so.e5), ('h20', so.h20, so.b20, so.e20),
                                 ('h60', so.h60, so.b60, so.e60), ('d20', so.d20, so.bd20, so.ed20)) AS x(h, v, b, e)
      LEFT JOIN market_forward_returns mf ON mf.trade_date = m.trade_date AND mf.horizon = x.h
     WHERE m.engine_version = %(v)s AND x.v IS NOT NULL AND (%(as_of)s::date IS NULL OR x.e <= %(as_of)s)
"""


def backtest_version(conn) -> tuple:
    """回測資料的版本：最新交易日、訊號結果的筆數與最後計算時間。每日流程更新後就會變，網站用它判斷快取還能不能用。"""
    return conn.execute("""SELECT (SELECT max(trade_date) FROM daily_prices),
                                  (SELECT count(*) FROM signal_outcomes), (SELECT max(computed_at) FROM signal_outcomes),
                                  (SELECT count(*) FROM market_forward_returns)""").fetchone()


def backtest_stats(conn, cost: float, as_of: date | None = None) -> list[dict]:
    """每種訊號 × 每個持有天數：筆數、上漲比例（含扣成本後）、平均、中位數、四分位數、比等權平均多多少，以及同一天全部股票的對照。"""
    return _all(conn, f"""
        WITH o AS ({_OUTCOME_ROWS})
        SELECT signal_type, h AS horizon, count(*) AS n,
               avg((v > 0)::int) AS win, avg((v > %(cost)s)::int) AS win_cost,
               avg(v) AS mean, percentile_cont(0.5) WITHIN GROUP (ORDER BY v) AS median,
               percentile_cont(0.25) WITHIN GROUP (ORDER BY v) AS p25, percentile_cont(0.75) WITHIN GROUP (ORDER BY v) AS p75,
               avg(v - b) AS excess_mean, avg(bu) AS base_win, avg(bm) AS base_median,
               min(trade_date) AS first, max(trade_date) AS last
          FROM o GROUP BY signal_type, h""", {"v": ENGINE_VERSION, "cost": cost, "as_of": as_of})


def backtest_base(conn, as_of: date | None = None) -> dict:
    """任選一天買進全部普通股的結果（各持有天數平均）：上漲比例與中位數。"""
    rows = _all(conn, """
        SELECT mf.horizon, avg(mf.up_share) AS up_share, avg(mf.median) AS median, avg(mf.mean) AS mean, count(*) AS days
          FROM market_forward_returns mf
          JOIN (SELECT trade_date, row_number() OVER (ORDER BY trade_date) AS idx FROM trading_calendar WHERE is_open) c
            ON c.trade_date = mf.trade_date
         WHERE %(as_of)s::date IS NULL
            OR c.idx + CASE mf.horizon WHEN 'h5' THEN 5 WHEN 'h20' THEN 20 WHEN 'h60' THEN 60 ELSE 21 END
               <= (SELECT count(*) FROM trading_calendar WHERE is_open AND trade_date <= %(as_of)s)
         GROUP BY mf.horizon""", {"as_of": as_of})
    return {r["horizon"]: {k: (float(v) if k != "days" and v is not None else v) for k, v in r.items() if k != "horizon"}
            for r in rows}


def backtest_period(conn, as_of: date | None = None) -> dict:
    """回測涵蓋的期間：20 日報酬已知的訊號從哪天到哪天、幾筆，以及同期加權指數的變化。"""
    row = _one(conn, f"""
        WITH o AS ({_OUTCOME_ROWS})
        SELECT min(trade_date) AS start, max(trade_date) AS end, count(*) AS n FROM o WHERE h = 'h20'""",
               {"v": ENGINE_VERSION, "as_of": as_of}) or {}
    idx = _one(conn, """
        SELECT (SELECT close FROM market_indices WHERE index_code = %(i)s AND trade_date >= %(s)s ORDER BY trade_date LIMIT 1) AS index_from,
               (SELECT close FROM market_indices WHERE index_code = %(i)s AND (%(e)s::date IS NULL OR trade_date <= %(e)s)
                 ORDER BY trade_date DESC LIMIT 1) AS index_to""",
               {"i": TAIEX, "s": row.get("start") or date.min, "e": as_of}) or {}
    a, b = idx.get("index_from"), idx.get("index_to")
    return {"start": row.get("start"), "end": row.get("end"), "n": row.get("n") or 0,
            "index_from": float(a) if a else None, "index_to": float(b) if b else None,
            "index_change": float(b) / float(a) - 1 if a and b else None}


# ---- 估值與財報 -------------------------------------------------------------
# 財報「最晚什麼時候已經公開」：官方的出表日期只是開放資料產生的日子，不是公告日，所以和法定期限取較早的一個。
# 期限用各產業最晚的（第 1 季 5/31、第 2 季 8/31、第 3 季 11/30、年報隔年 3/31），寧可晚一點顯示，也不讓過去的快照看到之後的財報。
FIN_KNOWN_BY = """LEAST(published_on, make_date(fiscal_year + (quarter = 4)::int,
                                               (ARRAY[5, 8, 11, 3])[quarter], (ARRAY[31, 31, 30, 31])[quarter]))"""

def stock_valuation(conn, stock_id: int, until: date | None = None) -> dict | None:
    """最近一個交易日（不晚於 until）的本益比、殖利率、股價淨值比。"""
    return _one(conn, """SELECT trade_date, pe_ratio, dividend_yield, pb_ratio FROM valuations
                          WHERE stock_id = %(id)s AND (%(until)s::date IS NULL OR trade_date <= %(until)s)
                          ORDER BY trade_date DESC LIMIT 1""", {"id": stock_id, "until": until})


def stock_financials(conn, stock_id: int, until: date | None = None, limit: int = 8) -> list[dict]:
    """最近幾季的綜合損益（年度累計），新的在前；until：只取那天以前公布的（匯出過去的日子）。"""
    return _all(conn, """SELECT fiscal_year, quarter, report_type, revenue, net_income, eps, published_on
                           FROM financial_reports
                          WHERE stock_id = %(id)s AND (%(until)s::date IS NULL OR """ + FIN_KNOWN_BY + """ <= %(until)s)
                          ORDER BY fiscal_year DESC, quarter DESC LIMIT %(n)s""", {"id": stock_id, "until": until, "n": limit})
