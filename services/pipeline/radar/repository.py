"""資料庫存取。所有寫入都是 upsert，重跑同一天不會產生重複資料。"""
from __future__ import annotations

from datetime import date
from typing import Iterable

import psycopg

from radar.models import (
    CompanySnapshot, CorporateActionPeriod, DailyQuote, InstitutionalDay, MarketDaySnapshot,
)
from radar.normalizers.common import security_type

FINAL_STATUSES = ("success", "no_data")


def connect(url: str) -> psycopg.Connection:
    # 資料庫沒開時要馬上失敗，不能讓排程卡住（Windows 上沒有逾時會一直等）
    return psycopg.connect(url, autocommit=True, connect_timeout=10)


def source_id(conn: psycopg.Connection, code: str) -> int:
    row = conn.execute("SELECT id FROM data_sources WHERE code = %s", (code,)).fetchone()
    if not row:
        raise RuntimeError(f"data_sources 缺少 {code}，請先執行 migration")
    return row[0]


# ---- ingestion_runs --------------------------------------------------------
def run_status(conn, src: int, dataset: str, d: date) -> str | None:
    row = conn.execute(
        "SELECT status FROM ingestion_runs WHERE source_id=%s AND dataset=%s AND target_date=%s",
        (src, dataset, d),
    ).fetchone()
    return row[0] if row else None


def start_run(conn, src: int, dataset: str, d: date) -> None:
    conn.execute(
        """
        INSERT INTO ingestion_runs (source_id, dataset, target_date, status)
        VALUES (%s, %s, %s, 'running')
        ON CONFLICT (source_id, dataset, target_date) DO UPDATE
           SET status='running', attempts = ingestion_runs.attempts + 1,
               started_at = now(), finished_at = NULL, error = NULL
        """,
        (src, dataset, d),
    )


def finish_run(conn, src: int, dataset: str, d: date, status: str, *, row_count: int | None = None,
               warnings: list[str] | None = None, error: str | None = None, raw_path: str | None = None) -> None:
    warn_text = None
    if warnings:
        head = "\n".join(warnings[:20])
        warn_text = head + (f"\n…另有 {len(warnings) - 20} 則" if len(warnings) > 20 else "")
    conn.execute(
        """
        UPDATE ingestion_runs
           SET status=%s, row_count=%s, warning_count=%s, warnings=%s,
               error=%s, raw_path=COALESCE(%s, raw_path), finished_at=now()
         WHERE source_id=%s AND dataset=%s AND target_date=%s
        """,
        (status, row_count, len(warnings or []), warn_text, error, raw_path, src, dataset, d),
    )


# ---- 交易日曆 --------------------------------------------------------------
def ensure_calendar(conn, d: date) -> None:
    """其他市場（上櫃）有成交的日子一定是交易日：日曆上沒有就補上，已有的（證交所決定的）不動。"""
    conn.execute("INSERT INTO trading_calendar (trade_date, is_open) VALUES (%s, true) ON CONFLICT (trade_date) DO NOTHING", (d,))


def set_calendar(conn, d: date, is_open: bool, note: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO trading_calendar (trade_date, is_open, note) VALUES (%s, %s, %s)
        ON CONFLICT (trade_date) DO UPDATE SET is_open=EXCLUDED.is_open, note=EXCLUDED.note, checked_at=now()
        """,
        (d, is_open, note),
    )


def mark_known_holidays(conn, days: list[tuple[date, str]], today: date) -> int:
    """休市日曆上「今天以後」的日子先記為休市；已經由實際資料決定的日子不覆蓋。"""
    rows = [(d, f"休市日曆：{name}"[:200]) for d, name in days if d >= today]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO trading_calendar (trade_date, is_open, note) VALUES (%s, false, %s) ON CONFLICT DO NOTHING",
            rows,
        )
    return len(rows)


def is_open_day(conn, d: date) -> bool | None:
    row = conn.execute("SELECT is_open FROM trading_calendar WHERE trade_date=%s", (d,)).fetchone()
    return row[0] if row else None


def prev_row_count(conn, table: str, d: date, market: str = "TWSE") -> int | None:
    """同一個市場（上市／上櫃）前一個有資料的交易日有幾筆，用來檢查今天的筆數是否異常。"""
    assert table in ("daily_prices", "institutional_flows")
    row = conn.execute(
        f"""
        WITH m AS (SELECT t.trade_date FROM {table} t JOIN stocks s ON s.id = t.stock_id
                    WHERE s.market = %(m)s AND t.trade_date < %(d)s)
        SELECT count(*) FROM {table} t JOIN stocks s ON s.id = t.stock_id
         WHERE s.market = %(m)s AND t.trade_date = (SELECT max(trade_date) FROM m)
        """,
        {"d": d, "m": market},
    ).fetchone()
    return row[0] or None


# ---- 證券主檔 --------------------------------------------------------------
def upsert_stocks(conn, market: str, d: date, items: Iterable[tuple[str, str]]) -> dict[str, int]:
    """items = (symbol, name)。回傳 symbol → stock_id。名稱只在資料日期較新時才覆蓋，避免回補舊資料改掉新名稱。"""
    rows = [(market, sym, name, security_type(sym), d, d) for sym, name in items]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO stocks (market, symbol, name, security_type, first_seen_date, last_seen_date)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (market, symbol) DO UPDATE SET
                name = CASE WHEN EXCLUDED.last_seen_date >= COALESCE(stocks.last_seen_date, EXCLUDED.last_seen_date)
                            THEN EXCLUDED.name ELSE stocks.name END,
                security_type   = EXCLUDED.security_type,
                first_seen_date = LEAST(stocks.first_seen_date, EXCLUDED.first_seen_date),
                last_seen_date  = GREATEST(stocks.last_seen_date, EXCLUDED.last_seen_date),
                is_active = true,
                updated_at = now()
            """,
            rows,
        )
    symbols = [r[1] for r in rows]
    found = conn.execute(
        "SELECT symbol, id FROM stocks WHERE market=%s AND symbol = ANY(%s)", (market, symbols)
    ).fetchall()
    return dict(found)


# ---- 日資料 ----------------------------------------------------------------
def write_market_day(conn, src: int, s: MarketDaySnapshot) -> int:
    d = s.trade_date
    ids = upsert_stocks(conn, s.market, d, ((q.symbol, q.name) for q in s.quotes))
    rows = [_price_row(ids[q.symbol], d, q, src) for q in s.quotes]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO daily_prices (stock_id, trade_date, open, high, low, close, change, is_no_compare,
                                      volume, turnover, trade_count, source_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stock_id, trade_date) DO UPDATE SET
                open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close,
                change=EXCLUDED.change, is_no_compare=EXCLUDED.is_no_compare, volume=EXCLUDED.volume,
                turnover=EXCLUDED.turnover, trade_count=EXCLUDED.trade_count,
                source_id=EXCLUDED.source_id, ingested_at=now()
            """,
            rows,
        )
        # 重跑時，這次沒有出現的列（例如被驗證剔除）要一併移除，保持與來源一致
        cur.execute(
            """
            DELETE FROM daily_prices p USING stocks s
             WHERE p.stock_id = s.id AND s.market = %s AND p.trade_date = %s
               AND p.source_id = %s AND NOT (p.stock_id = ANY(%s))
            """,
            (s.market, d, src, [r[0] for r in rows]),
        )
        cur.executemany(
            """
            INSERT INTO market_indices (index_code, trade_date, close, change, change_pct, source_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (index_code, trade_date) DO UPDATE SET
                close=EXCLUDED.close, change=EXCLUDED.change, change_pct=EXCLUDED.change_pct,
                source_id=EXCLUDED.source_id
            """,
            [(i.index_code, d, i.close, i.change, i.change_pct, src) for i in s.indices],
        )
        b = s.breadth
        cur.execute(
            """
            INSERT INTO market_breadth (trade_date, market, advancers, decliners, unchanged, limit_up,
                                        limit_down, no_trade, total_volume, total_turnover, total_trades, source_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (trade_date, market) DO UPDATE SET
                advancers=EXCLUDED.advancers, decliners=EXCLUDED.decliners, unchanged=EXCLUDED.unchanged,
                limit_up=EXCLUDED.limit_up, limit_down=EXCLUDED.limit_down, no_trade=EXCLUDED.no_trade,
                total_volume=EXCLUDED.total_volume, total_turnover=EXCLUDED.total_turnover,
                total_trades=EXCLUDED.total_trades, source_id=EXCLUDED.source_id
            """,
            (d, s.market, b.advancers, b.decliners, b.unchanged, b.limit_up, b.limit_down, b.no_trade,
             b.total_volume, b.total_turnover, b.total_trades, src),
        )
    return len(rows)


def _price_row(stock_id: int, d: date, q: DailyQuote, src: int) -> tuple:
    return (stock_id, d, q.open, q.high, q.low, q.close, q.change, q.is_no_compare,
            q.volume, q.turnover, q.trade_count, src)


def write_institutional(conn, src: int, day: InstitutionalDay) -> int:
    d = day.trade_date
    ids = upsert_stocks(conn, day.market, d, ((f.symbol, f.name) for f in day.flows))
    rows = [
        (ids[f.symbol], d, f.foreign_buy, f.foreign_sell, f.foreign_net, f.trust_buy, f.trust_sell,
         f.trust_net, f.dealer_buy, f.dealer_sell, f.dealer_net, f.total_net, src)
        for f in day.flows
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO institutional_flows (stock_id, trade_date, foreign_buy, foreign_sell, foreign_net,
                trust_buy, trust_sell, trust_net, dealer_buy, dealer_sell, dealer_net, total_net, source_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stock_id, trade_date) DO UPDATE SET
                foreign_buy=EXCLUDED.foreign_buy, foreign_sell=EXCLUDED.foreign_sell,
                foreign_net=EXCLUDED.foreign_net, trust_buy=EXCLUDED.trust_buy,
                trust_sell=EXCLUDED.trust_sell, trust_net=EXCLUDED.trust_net,
                dealer_buy=EXCLUDED.dealer_buy, dealer_sell=EXCLUDED.dealer_sell,
                dealer_net=EXCLUDED.dealer_net, total_net=EXCLUDED.total_net,
                source_id=EXCLUDED.source_id, ingested_at=now()
            """,
            rows,
        )
        cur.execute(
            """
            DELETE FROM institutional_flows f USING stocks s
             WHERE f.stock_id = s.id AND s.market = %s AND f.trade_date = %s
               AND f.source_id = %s AND NOT (f.stock_id = ANY(%s))
            """,
            (day.market, d, src, [r[0] for r in rows]),
        )
    return len(rows)


# ---- 除權息 ----------------------------------------------------------------
def write_corporate_actions(conn, src: int, p: CorporateActionPeriod) -> int:
    """寫入 p.start～p.end 的除權息；區間內這次沒有出現的列會刪除，保持與來源一致。"""
    ids: dict[str, int] = {}
    by_date: dict[date, list[tuple[str, str]]] = {}
    for a in p.actions:
        by_date.setdefault(a.ex_date, []).append((a.symbol, a.name))
    for d, items in by_date.items():
        ids.update(upsert_stocks(conn, p.market, d, items))
    rows = [(ids[a.symbol], a.ex_date, a.action_type, a.prev_close, a.ref_price, a.value,
             a.limit_up, a.limit_down, a.open_ref, a.div_ref, src) for a in p.actions]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO corporate_actions (stock_id, ex_date, action_type, prev_close, ref_price, value,
                                           limit_up, limit_down, open_ref, div_ref, source_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stock_id, ex_date) DO UPDATE SET
                action_type=EXCLUDED.action_type, prev_close=EXCLUDED.prev_close, ref_price=EXCLUDED.ref_price,
                value=EXCLUDED.value, limit_up=EXCLUDED.limit_up, limit_down=EXCLUDED.limit_down,
                open_ref=EXCLUDED.open_ref, div_ref=EXCLUDED.div_ref, source_id=EXCLUDED.source_id, ingested_at=now()
            """,
            rows,
        )
        cur.execute(
            """
            DELETE FROM corporate_actions c USING stocks s
             WHERE c.stock_id = s.id AND s.market = %s AND c.ex_date BETWEEN %s AND %s
               AND c.source_id = %s AND NOT ((c.stock_id, c.ex_date) IN (SELECT * FROM unnest(%s::int[], %s::date[])))
            """,
            (p.market, p.start, p.end, src, [r[0] for r in rows], [r[1] for r in rows]),
        )
    return len(rows)


# ---- 公司基本資料 ------------------------------------------------------------
def write_company_profiles(conn, s: CompanySnapshot) -> int:
    """更新產業別與上市日期；還沒出現在行情裡的公司也會建立（名稱之後以行情為準）。"""
    industries = sorted({(c.industry_code, c.industry_name) for c in s.companies})
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO industries (market, code, name) VALUES (%s, %s, %s)
            ON CONFLICT (market, code) DO UPDATE SET name = EXCLUDED.name
            """,
            [(s.market, code, name) for code, name in industries],
        )
    ind = dict(conn.execute("SELECT code, id FROM industries WHERE market = %s", (s.market,)).fetchall())
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO stocks (market, symbol, name, security_type, industry_id, listed_date)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (market, symbol) DO UPDATE SET
                industry_id = EXCLUDED.industry_id,
                listed_date = COALESCE(EXCLUDED.listed_date, stocks.listed_date),
                updated_at  = now()
            """,
            [(s.market, c.symbol, c.name, security_type(c.symbol), ind[c.industry_code], c.listed_date)
             for c in s.companies],
        )
    return len(s.companies)


# ---- 估值與財報 ---------------------------------------------------------------
def _stock_ids(conn, market: str) -> dict[str, int]:
    return dict(conn.execute("SELECT symbol, id FROM stocks WHERE market = %s", (market,)).fetchall())


def write_valuations(conn, src: int, market: str, day) -> tuple[int, int]:
    """寫入一天的估值（重跑會覆蓋）；回傳（寫入筆數, 不在股票清單而略過的筆數）。"""
    ids = _stock_ids(conn, market)
    rows = [(ids[v.symbol], day.trade_date, v.pe_ratio, v.dividend_yield, v.pb_ratio, src) for v in day.rows if v.symbol in ids]
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO valuations (stock_id, trade_date, pe_ratio, dividend_yield, pb_ratio, source_id)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (stock_id, trade_date) DO UPDATE
                  SET pe_ratio = EXCLUDED.pe_ratio, dividend_yield = EXCLUDED.dividend_yield,
                      pb_ratio = EXCLUDED.pb_ratio, source_id = EXCLUDED.source_id""", rows)
    return len(rows), len(day.rows) - len(rows)


def write_financial_reports(conn, src: int, market: str, reports) -> tuple[int, int]:
    """寫入財報（同一家、同一年、同一季重跑會覆蓋）；回傳（寫入筆數, 略過筆數）。"""
    ids = _stock_ids(conn, market)
    rows = [(ids[r.symbol], r.fiscal_year, r.quarter, r.report_type, r.revenue, r.net_income, r.eps, r.published_on, src)
            for r in reports if r.symbol in ids]
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO financial_reports (stock_id, fiscal_year, quarter, report_type, revenue, net_income, eps,
                                              published_on, source_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (stock_id, fiscal_year, quarter) DO UPDATE
                  SET report_type = EXCLUDED.report_type, revenue = EXCLUDED.revenue, net_income = EXCLUDED.net_income,
                      eps = EXCLUDED.eps, published_on = EXCLUDED.published_on, source_id = EXCLUDED.source_id,
                      updated_at = now()""", rows)
    return len(rows), len(reports) - len(rows)
