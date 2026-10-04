"""公開 JSON：每天和公開網頁一起發布到 GitHub Pages，給需要的人用程式抓（免費、不用金鑰、不需要伺服器）。

    api/v1/index.json          資料日期、欄位說明、來源與聲明、所有股票清單
    api/v1/market.json         大盤、市場狀態、今日 3 大重點、當天的訊號
    api/v1/stocks/{代號}.json  近 SERIES_DAYS 個交易日的開高低收量、SMA5／20／60、EMA12／26、MACD，
                               以及估值（本益比、殖利率、淨值比）、財報（年度累計 EPS）、期間內的訊號

數字和網站同一套計算（技術指標從同一個起算點算，見 radar/indicators.py），只描述資料，不是買賣建議。
"""
from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from radar import explain, indicators
from radar.config import TAIPEI
from radar.signals import ENGINE_VERSION
from radar.web import queries as q

SERIES_DAYS = 120
VERSION = "v1"
MARKETS = {"TWSE": "上市", "TPEX": "上櫃"}
DISCLAIMER = ("台股市場雷達把證交所、櫃買中心公開的盤後資料整理成統計與說明，只描述發生了什麼，不預測股價，"
              "也不提供任何買賣建議；不是證券投資顧問事業。資料可能有延遲或錯誤，一切以官方公布為準。")
FIELDS = {
    "series.date": "交易日（舊 → 新）",
    "series.open/high/low/close": "開盤、最高、最低、收盤（元，原始價，未還原權息）",
    "series.volume_lots": "成交量（張）",
    "series.sma5/sma20/sma60": "簡單移動平均：最近 n 個交易日收盤價的平均",
    "series.ema12/ema26": "指數移動平均：權重 2 ÷ (n + 1)，以前 n 日 SMA 為起點",
    "series.dif": "EMA12 − EMA26",
    "series.macd": "DIF 的 9 日指數移動平均（訊號線）",
    "series.osc": "DIF − MACD（柱狀體）",
    "valuation": "官方本益比（倍，近四季虧損時為 null）、殖利率（%）、股價淨值比（倍）與資料日期",
    "financials": "綜合損益表：EPS、營業收入、稅後淨利（母公司）都是年度累計（元）；eps_quarter 是單季 EPS（有上一季資料時才有）",
    "signals": "期間內的雷達訊號（只代表資料和平常不一樣，不代表好壞）",
}


def _default(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    raise TypeError(type(v))


def _write(path: Path, doc) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), default=_default)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _r(v, n: int):
    return None if v is None else round(float(v), n)


def build_public_api(conn, d: date, out_dir: Path) -> dict:
    """把 d 這天的公開 JSON 寫到 out_dir/api/v1/（先清空）。回傳檔案數與總大小。conn 用唯讀帳號即可。"""
    from radar.web.app import home_context    # 避免循環匯入
    root = out_dir / "api" / VERSION
    if root.exists():
        shutil.rmtree(root)
    lookback = indicators.lookback(SERIES_DAYS)

    stocks = q._all(conn, """SELECT s.id, s.symbol, s.name, s.market, s.security_type, i.name AS industry
                               FROM daily_prices p JOIN stocks s ON s.id = p.stock_id
                               LEFT JOIN industries i ON i.id = s.industry_id
                              WHERE p.trade_date = %(d)s ORDER BY s.symbol, s.market""", {"d": d})
    # 每檔取最近 lookback 筆（和個股頁的 stock_prices 一樣以「筆」計），所以日曆上多抓一些
    prices: dict[int, list[tuple]] = defaultdict(list)
    for row in conn.execute("""WITH days AS (SELECT trade_date FROM trading_calendar WHERE is_open AND trade_date <= %(d)s
                                              ORDER BY trade_date DESC LIMIT %(n)s)
                               SELECT stock_id, trade_date, open, high, low, close, volume FROM daily_prices
                                WHERE trade_date BETWEEN (SELECT min(trade_date) FROM days) AND %(d)s
                                ORDER BY stock_id, trade_date""", {"d": d, "n": lookback + 60}):
        prices[row[0]].append(row[1:])
    valuations = {r["stock_id"]: r for r in q._all(conn, """
        SELECT DISTINCT ON (stock_id) stock_id, trade_date, pe_ratio, dividend_yield, pb_ratio FROM valuations
         WHERE trade_date <= %(d)s ORDER BY stock_id, trade_date DESC""", {"d": d})}
    financials: dict[int, list[dict]] = defaultdict(list)
    for r in q._all(conn, """SELECT stock_id, fiscal_year, quarter, report_type, revenue, net_income, eps, published_on
                               FROM financial_reports WHERE """ + q.FIN_KNOWN_BY + """ <= %(d)s
                              ORDER BY stock_id, fiscal_year DESC, quarter DESC""", {"d": d}):
        financials[r.pop("stock_id")].append(r)
    start = conn.execute("""SELECT min(trade_date) FROM (SELECT trade_date FROM trading_calendar WHERE is_open AND trade_date <= %s
                                                          ORDER BY trade_date DESC LIMIT %s) x""", (d, SERIES_DAYS)).fetchone()[0]
    signals: dict[int, list[dict]] = defaultdict(list)
    for g in q._all(conn, """SELECT stock_id, trade_date, signal_type, value, evidence FROM market_signals
                              WHERE engine_version = %(v)s AND trade_date BETWEEN %(start)s AND %(d)s
                              ORDER BY trade_date""", {"v": ENGINE_VERSION, "start": start or d, "d": d}):
        signals[g["stock_id"]].append(g)

    files, size = 0, 0
    for s in stocks:
        rows = prices.get(s["id"], [])[-lookback:]
        ind = indicators.tail(indicators.compute([{"close": r[4]} for r in rows]), SERIES_DAYS)
        rows = rows[-SERIES_DAYS:]
        doc = {
            "symbol": s["symbol"], "name": s["name"], "market": s["market"], "market_label": MARKETS.get(s["market"], ""),
            "industry": s["industry"], "type": s["security_type"], "date": d,
            "series": {
                "date": [r[0] for r in rows],
                "open": [_r(r[1], 2) for r in rows], "high": [_r(r[2], 2) for r in rows],
                "low": [_r(r[3], 2) for r in rows], "close": [_r(r[4], 2) for r in rows],
                "volume_lots": [None if r[5] is None else int(r[5]) // 1000 for r in rows],
                **{k: [_r(v, 3) for v in ind[k]] for k in ("sma5", "sma20", "sma60", "ema12", "ema26", "dif")},
                "macd": [_r(v, 3) for v in ind["signal"]], "osc": [_r(v, 3) for v in ind["hist"]],
            },
            "valuation": ({k: v for k, v in valuations[s["id"]].items() if k != "stock_id"} if s["id"] in valuations else None),
            "financials": [{"year": f["fiscal_year"], "quarter": f["quarter"], "eps": f["eps"],
                            "eps_quarter": None if f["eps_q"] is None else round(f["eps_q"], 2),
                            "revenue": f["revenue"], "net_income": f["net_income"], "published_on": f["published_on"]}
                           for f in explain.financial_rows(financials.get(s["id"], []))],
            "signals": [{"date": g["trade_date"], "type": g["signal_type"], "label": explain.info(g["signal_type"]).label,
                         "explanation": explain.signal_sentence(g)}
                        for g in signals.get(s["id"], []) if rows and g["trade_date"] >= rows[0][0]],
        }
        size += _write(root / "stocks" / f"{s['symbol']}.json", doc)
        files += 1

    ctx = home_context(conn, d)
    ov = ctx["ov"]
    size += _write(root / "market.json", {
        "date": d, "engine_version": ENGINE_VERSION, "index": ov["index"], "otc_index": ov["otc"],
        "breadth": ov["breadth"], "otc_breadth": ov["otc_breadth"],
        "state": {k: ctx["state"][k] for k in ("level", "label", "sentence")},
        "key_points": [{"title": p["title"], "text": p["text"]} for p in ctx["key_points"]],
        "signals": [explain.signal_object(r) for r in q.signals_on(conn, d)],
        "disclaimer": DISCLAIMER,
    })
    size += _write(root / "index.json", {
        "name": "台股市場雷達 公開資料", "version": VERSION, "date": d,
        "generated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"), "engine_version": ENGINE_VERSION,
        "sources": ["臺灣證券交易所（盤後報表、OpenAPI）", "證券櫃檯買賣中心（OpenAPI）"],
        "disclaimer": DISCLAIMER,
        "endpoints": {"market": "market.json", "stock": "stocks/{symbol}.json"},
        "fields": FIELDS,
        "stocks": [[s["symbol"], s["name"], s["market"], s["industry"], s["security_type"]] for s in stocks],
    })
    return {"files": files + 2, "bytes": size, "root": str(root)}
