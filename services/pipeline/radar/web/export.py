"""把某一天的網站存成「一個」HTML 檔：今日市場、今日雷達、有訊號的個股段落、資料與計算方式；樣式與程式內嵌、圖表是 SVG，
不需要網路或伺服器，雙擊就能開，可以直接用 LINE、Email 傳給別人。

個股圖表在匯出檔裡縮短成近 120 個交易日，檔案通常 2～5 MB；崩盤日訊號多時最多收錄 MAX_STOCKS 檔。
"""
from __future__ import annotations

import base64
from datetime import date, datetime
from pathlib import Path

from radar.config import TAIPEI
from radar.formatting import fmt_price, fmt_ret, fmt_yi, tone
from radar.web import queries as q
from radar.web.app import HERE, about_context, backtest_context, home_context, make_templates, stock_context

MAX_STOCKS = 80
CHART_DAYS, FLOW_DAYS = 120, 40


SECURITY_TYPES = {"stock": "股票", "etf": "ETF", "other": "其他"}


def quote_index(conn, d: date, ctx: dict, included: set[str]) -> list[list]:
    """離線搜尋用的索引：當天每一檔上市證券一列，數字先照網站的格式排好，瀏覽器只負責比對與顯示。
    欄位：代號、名稱、產業、類型、收盤、漲跌幅、漲跌方向、成交金額、檔案裡有沒有個股段落、今天的訊號，
    以及條件搜尋用的原始數字：量比、創 60 日新高、外資連買、漲跌幅（小數）、是不是普通股、成交金額（元）。"""
    labels: dict[str, list[str]] = {}
    for sec in ctx["radar"]:
        for it in sec["types"]:
            for r in it["rows"]:
                labels.setdefault(r["symbol"], []).append(it["info"].label)
    return [[r["symbol"], r["name"], r["industry"] or "",
             ("上櫃" if r.get("market") == "TPEX" else "") + SECURITY_TYPES.get(r["security_type"], ""),
             fmt_price(r["close"]), fmt_ret(r["day_pct"]), tone(r["day_pct"]), fmt_yi(r["turnover"]),
             int(r["symbol"] in included), "、".join(labels.get(r["symbol"], [])),
             _round(r["vol_ratio"], 2), int(r["new_high"]), int(r["foreign_buy"]), _round(r["day_pct"], 4),
             int(r["security_type"] == "stock"), _round(r["turnover"], 0)]
            for r in q.day_quotes(conn, d)]


def _round(v, n: int):
    return None if v is None else round(float(v), n)


def render_day(conn, d: date) -> str:
    ctx = home_context(conn, d)
    symbols = list(dict.fromkeys(r["symbol"] for sec in ctx["radar"] for it in sec["types"] for r in it["rows"]))
    stocks = []
    for symbol in symbols[:MAX_STOCKS]:
        prof = q.stock_profile(conn, symbol)
        if prof:
            stocks.append(stock_context(conn, prof, CHART_DAYS, FLOW_DAYS, as_of=d))   # 個股段落停在匯出的那天
    included = {st["s"]["symbol"] for st in stocks}
    return make_templates().env.get_template("export.html").render(
        ctx | about_context(conn) | backtest_context(conn, d) | {
            "export": True,
            "stocks": stocks,
            "omitted": len(symbols) - len(stocks),
            "quotes": quote_index(conn, d, ctx, included),
            "screens": q.SCREENS,
            "stock_href": lambda symbol: f"#s-{symbol}" if symbol in included else "",   # 沒有收錄的只顯示文字
            "inline_css": (HERE / "static" / "style.css").read_text(encoding="utf-8"),
            "icon_uri": "data:image/svg+xml;base64," + base64.b64encode((HERE / "static" / "icon.svg").read_bytes()).decode(),
            "generated_at": datetime.now(TAIPEI).strftime("%Y/%m/%d %H:%M"),
        })


def export_day(conn, d: date, out_dir: Path) -> Path:
    """寫出 台股雷達_YYYY-MM-DD.html；d 是最新交易日時，也覆蓋 台股雷達_最新.html（方便固定傳同一個檔名）。
    匯出過去的日子不會動到「最新」。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    html = render_day(conn, d)
    path = out_dir / f"台股雷達_{d:%Y-%m-%d}.html"
    latest = conn.execute("SELECT max(trade_date) FROM market_breadth").fetchone()[0]
    targets = [path] + ([out_dir / "台股雷達_最新.html"] if d == latest else [])
    for target in targets:
        tmp = target.with_suffix(".tmp")
        tmp.write_text(html, encoding="utf-8")
        tmp.replace(target)
    return path
