"""網站：首頁（當日盤後）、股票頁、搜尋、資料與計算方式，以及同樣內容的 JSON API（/api/…，文件在 /api/docs）。

頁面的閱讀順序：今日市場 → 今日雷達（哪些異常）→ 市場熱點 → 個股「為什麼被雷達注意」→ 詳細資料。
文字說明一律來自解釋層（radar/explain.py），這裡只負責查資料、組合頁面。
啟動：python -m radar web（預設只聽 127.0.0.1:8000）。資料庫用 WEB_DATABASE_URL 的唯讀帳號。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from radar import explain, live
from radar.config import TAIPEI, load_settings
from radar.formatting import (fmt_day, fmt_lots, fmt_pct, fmt_price, fmt_ret, fmt_signed, fmt_yi, tone)
from radar.outcomes import ROUND_TRIP_COST
from radar.signals import ENGINE_VERSION, TYPES
from radar.web import charts, queries as q

HERE = Path(__file__).parent
RECENT_DAYS = 10          # 個股今天沒有訊號時，往前找最近幾個交易日的訊號
# K 線的時間範圍（交易日數）：資料不夠長的範圍不顯示，不自己補
CHART_RANGES = [("1M", 21), ("3M", 63), ("6M", 120), ("1Y", 250)]


def _sector_ranking(sectors: list[dict], k: int = 5) -> dict:
    """漲幅最大、最小各 k 個（已依漲跌幅排序）；中間用 None 當分隔。"""
    rows = sectors if len(sectors) <= 2 * k else sectors[:k] + [None] + sectors[-k:]
    top = max((abs(float(s["change_pct"])) for s in sectors), default=0) or 1
    return {"rows": rows, "max": top, "count": len(sectors)}


# ---- 頁面內容（網站與匯出檔共用）------------------------------------------------------------
def radar_sections(rows: list[dict]) -> list[dict]:
    """今日雷達：依類別（成交量／價格／法人動向）分組，每種訊號都列出（0 則也列，讓新手知道雷達在看什麼）。"""
    by_type: dict[str, list[dict]] = {t: [] for t in explain.SIGNAL_INFO}
    labels: dict[str, list[str]] = {}
    for r in rows:
        labels.setdefault(r["symbol"], []).append(explain.info(r["signal_type"]).label)
    for r in rows:
        by_type.setdefault(r["signal_type"], []).append(r | {
            "sentence": explain.signal_sentence(r), "evidence_text": explain.evidence_text(r),
            "metrics": explain.signal_metrics(r),
            # 同一檔股票今天的其他訊號（卡片上「也出現」）
            "also": [x for x in labels[r["symbol"]] if x != explain.info(r["signal_type"]).label]})
    sections = []
    for cat, cat_label in explain.CATEGORIES.items():
        items = [{"info": i, "rows": by_type[i.type]} for i in explain.SIGNAL_INFO.values() if i.category == cat]
        sections.append({"category": cat, "label": cat_label, "types": items, "total": sum(len(x["rows"]) for x in items)})
    return sections


def hotspots(rows: list[dict], limit: int = 5) -> list[dict]:
    """市場熱點：哪些產業今天出現比較多異常訊號（只是計數，不評價產業好壞）。"""
    by_ind: dict[str, dict] = {}
    for r in rows:
        name = r["industry"] or "未分類"
        h = by_ind.setdefault(name, {"industry": name, "total": 0, "types": {}, "stocks": []})
        h["total"] += 1
        i = explain.info(r["signal_type"])
        h["types"].setdefault(i.type, {"label": i.label, "tone": i.tone, "count": 0})["count"] += 1
        if r["symbol"] not in [s["symbol"] for s in h["stocks"]]:
            h["stocks"].append({"symbol": r["symbol"], "name": r["name"]})
    ranked = sorted((h for h in by_ind.values() if h["total"] >= 2), key=lambda h: -h["total"])
    return [h | {"types": list(h["types"].values())} for h in ranked[:limit]]


def multi_signal_stocks(rows: list[dict], limit: int = 8) -> list[dict]:
    """同一天出現兩種以上訊號的個股。"""
    by_sym: dict[str, dict] = {}
    for r in rows:
        s = by_sym.setdefault(r["symbol"], {"symbol": r["symbol"], "name": r["name"], "industry": r["industry"],
                                            "day_pct": r["day_pct"], "signals": []})
        s["signals"].append({"type": r["signal_type"], "label": explain.info(r["signal_type"]).label,
                             "tone": explain.tone_of(r["signal_type"])})
    multi = [s for s in by_sym.values() if len(s["signals"]) >= 2]
    return sorted(multi, key=lambda s: -len(s["signals"]))[:limit]


def home_context(conn, day: date, requested: date | None = None) -> dict:
    ov = q.market_overview(conn, day)
    rows = q.signals_on(conn, day)
    prev_day, next_day = q.neighbor_dates(conn, day)
    state = explain.market_state(ov["index"], ov["breadth"])
    return {
        "day": day, "requested": requested, "ov": ov, "total": len(rows),
        # 盤中即時：雷達名單（前一個交易日盤後標記的股票）一次查即時報價，最多 live.MAX_SYMBOLS 檔
        "live_symbols": list(dict.fromkeys(r["symbol"] for r in rows))[:live.MAX_SYMBOLS],
        # 看過去的日子時，在「今日市場」與「今日雷達」之間切換要留在同一天
        "date_qs": "" if next_day is None else f"?date={day}",
        "state": state,
        "key_points": explain.key_points(state, ov["index"], ov["breadth"], rows, ov["sectors"]),
        "digest": explain.anomaly_digest(rows),
        "data_status": q.data_status(conn, day),
        "radar": radar_sections(rows), "hotspots": hotspots(rows), "multi": multi_signal_stocks(rows),
        "prev_day": prev_day, "next_day": next_day,
        "spark": charts.sparkline([(r["trade_date"], r["close"]) for r in ov["index_history"]]),
        "sectors": _sector_ranking(ov["sectors"]),
    }


def price_charts(prices: list[dict], sigs: list[dict], acts: list[dict]) -> list[dict]:
    """K 線的各個時間範圍（1M／3M／6M／1Y）；預設 3M，資料不夠時改成最長的那個。"""
    avail = [(label, n) for label, n in CHART_RANGES if len(prices) >= n]
    if not avail and prices:
        avail = [(f"{len(prices)} 日", len(prices))]
    default = "3M" if any(label == "3M" for label, _ in avail) else (avail[-1][0] if avail else None)
    return [{"label": label, "days": n, "default": label == default,
             "svg": charts.price_chart(prices[-n:], sigs, acts, TYPES)} for label, n in avail]


def stock_context(conn, prof: dict, days: int = 250, flow_days: int = 60, as_of: date | None = None) -> dict:
    """個股頁的資料。as_of：只用這天（含）以前的資料（匯出過去的日子）；不指定就是最新。"""
    prices = q.stock_prices(conn, prof["id"], days, as_of)
    sigs = q.stock_signals(conn, prof["id"], as_of)
    acts = q.stock_actions(conn, prof["id"], as_of)
    last = prices[-1] if prices else None
    today = [s for s in sigs if last and s["trade_date"] == last["trade_date"]]
    recent_from = prices[-RECENT_DAYS - 1]["trade_date"] if len(prices) > RECENT_DAYS else None
    recent = [s for s in sigs if last and s["trade_date"] < last["trade_date"]
              and (recent_from is None or s["trade_date"] >= recent_from)]
    return {
        "s": prof, "last": last, "signals": sigs, "actions": acts, "flow_days": flow_days,
        "story": explain.stock_story(last, today, recent),
        "chips": [explain.signal_chip(x) for x in today][:3],
        "kcharts": price_charts(prices, sigs, acts),
        "flows_svg": charts.flows_chart(q.stock_flows(conn, prof["id"], flow_days, as_of)),
        "peers": q.industry_peers(conn, prof["id"], last["trade_date"]) if last and prof["industry"] else [],
        "range": (prices[0]["trade_date"], prices[-1]["trade_date"]) if prices else None,
    }


def backtest_context(conn, as_of: date | None = None) -> dict:
    """訊號回測：as_of 指定時只用那天之前已經知道結果的訊號（今日雷達的過去日期、匯出快照）。"""
    rows = q.backtest_stats(conn, ROUND_TRIP_COST, as_of)
    return {"bt": explain.backtest_view(rows, q.backtest_period(conn, as_of), q.backtest_base(conn, as_of), ROUND_TRIP_COST)}


def backtest_json(bt: dict) -> dict:
    """回測結果給 API：訊號定義換成代號與名稱，日期換成字串。"""
    p = bt["period"]
    return {
        "engine_version": ENGINE_VERSION, "round_trip_cost": bt["cost"], "horizons": bt["horizons"],
        "period": {k: (v.isoformat() if isinstance(v, date) else v) for k, v in p.items()},
        "all_stocks": bt["base"],
        "types": [{"type": t["info"].type, "label": t["info"].label, "verdict": t["verdict"], "sentence": t["sentence"],
                   "horizons": t["h"]} for t in bt["types"]],
        "myths": bt["myths"], "caveats": bt["caveats"],
    }


def about_context(conn) -> dict:
    return {"signal_infos": list(explain.SIGNAL_INFO.values()), "methodology": explain.METHODOLOGY,
            "latest": q.latest_trade_date(conn)}


def asset_version() -> str:
    """樣式表的內容雜湊：改了 CSS，網址就會變，瀏覽器不會繼續用快取的舊檔。"""
    return hashlib.sha1((HERE / "static" / "style.css").read_bytes()).hexdigest()[:10]


def web_manifest() -> dict:
    """PWA 設定：讓瀏覽器可以把網站「安裝」成 App（獨立視窗、主畫面圖示）。"""
    icons = [{"src": f"/static/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png", "purpose": purpose}
             for n in (192, 512) for purpose in ("any", "maskable")]
    return {
        "name": "台股市場雷達", "short_name": "台股雷達", "lang": "zh-Hant", "dir": "ltr",
        "description": "每天盤後整理台股大盤與異常訊號，說明發生了什麼。不預測股價，也不提供買賣建議。",
        "id": "/", "start_url": "/", "scope": "/", "display": "standalone", "orientation": "portrait",
        "background_color": "#000000", "theme_color": "#000000",
        "icons": icons + [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
        "shortcuts": [
            {"name": "今日雷達", "url": "/radar", "icons": [{"src": "/static/icon-192.png", "sizes": "192x192"}]},
            {"name": "搜尋股票", "url": "/search", "icons": [{"src": "/static/icon-192.png", "sizes": "192x192"}]},
        ],
    }


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(lots=fmt_lots, yi=fmt_yi, price=fmt_price, ret=fmt_ret, pct=fmt_pct,
                                 signed=fmt_signed, tone=tone, day=fmt_day,
                                 share=lambda v: "–" if v is None else f"{float(v) * 100:.0f}%")
    templates.env.globals.update(
        describe=explain.evidence_text, sentence=explain.signal_sentence, tone_of=explain.tone_of,
        info=explain.info, SIGNAL_INFO=explain.SIGNAL_INFO, TYPES=TYPES, ENGINE_VERSION=ENGINE_VERSION,
        export=False, stock_href=lambda symbol: f"/stock/{symbol}", asset_v=asset_version())
    return templates


# ---- App ---------------------------------------------------------------------------
def create_app(db_url: str | None = None, live_feed: live.LiveFeed | None = None) -> FastAPI:
    settings = load_settings()
    url = db_url or settings.web_database_url
    # 盤中即時行情：整個網站共用一份快取，不論多少人在看，對上游的請求數都一樣；資料來源由 LIVE_PROVIDER 決定
    feed = live_feed or live.LiveFeed(live.make_provider(settings.live_provider))
    app = FastAPI(title="台股市場雷達 API", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = make_templates()

    def db():
        conn = psycopg.connect(url, autocommit=True, connect_timeout=10)
        try:
            yield conn
        finally:
            conn.close()

    def page(request: Request, name: str, ctx: dict, status: int = 200):
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    @app.exception_handler(HTTPException)
    async def not_found(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return page(request, "error.html", {"status": exc.status_code, "detail": exc.detail}, exc.status_code)

    # ---- 網頁 ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request, d: date | None = Query(None, alias="date"), conn=Depends(db)):
        """入口頁：全螢幕影片背景與當日重點數字，「看今天的盤後」進到今日市場。"""
        day = q.resolve_date(conn, d)
        if day is None:
            return page(request, "error.html", {"status": 503, "detail": "資料庫裡還沒有行情資料。"}, 503)
        return page(request, "landing.html", home_context(conn, day, d))

    @app.get("/market", response_class=HTMLResponse)
    def home(request: Request, d: date | None = Query(None, alias="date"), conn=Depends(db)):
        day = q.resolve_date(conn, d)
        if day is None:
            return page(request, "error.html", {"status": 503, "detail": "資料庫裡還沒有行情資料。"}, 503)
        return page(request, "home.html", home_context(conn, day, d))

    @app.get("/radar", response_class=HTMLResponse)
    def radar(request: Request, d: date | None = Query(None, alias="date"), conn=Depends(db)):
        day = q.resolve_date(conn, d)
        if day is None:
            return page(request, "error.html", {"status": 503, "detail": "資料庫裡還沒有行情資料。"}, 503)
        return page(request, "radar.html", home_context(conn, day, d) | backtest_context(conn, day))

    @app.get("/backtest", response_class=HTMLResponse)
    def backtest(request: Request, conn=Depends(db)):
        return page(request, "backtest.html", backtest_context(conn))

    @app.get("/stock/{symbol}", response_class=HTMLResponse)
    def stock(request: Request, symbol: str, conn=Depends(db)):
        prof = q.stock_profile(conn, symbol.upper())
        if prof is None:
            raise HTTPException(404, f"找不到證券代號 {symbol}")
        return page(request, "stock.html", stock_context(conn, prof) | backtest_context(conn))

    @app.get("/about", response_class=HTMLResponse)
    def about(request: Request, conn=Depends(db)):
        return page(request, "about.html", about_context(conn))

    # ---- App（PWA）：安裝設定、離線快取、離線頁 -----------------------------------------
    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return JSONResponse(web_manifest(), media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker(request: Request):
        # 放在根目錄，離線快取才能涵蓋整個網站；no-cache 讓瀏覽器每次都檢查有沒有新版
        return templates.TemplateResponse(request, "sw.js", {}, media_type="text/javascript",
                                          headers={"Cache-Control": "no-cache"})

    @app.get("/offline", response_class=HTMLResponse, include_in_schema=False)
    def offline(request: Request):
        return page(request, "offline.html", {})

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q_: str = Query("", alias="q"), f: str = Query("", description="條件搜尋：vol3／up5／high60／fbuy"),
               conn=Depends(db)):
        if f in q.SCREENS:
            day = q.resolve_date(conn, None)
            rows, total = q.screen_stocks(conn, day, f) if day else ([], 0)
            return page(request, "search.html", {"query": "", "results": [], "screens": q.SCREENS, "screen": f,
                                                 "screened": rows, "screen_total": total, "day": day})
        results = q.search_stocks(conn, q_)
        exact = [r for r in results if r["symbol"] == q_.strip().upper() or r["name"] == q_.strip()]
        if len(exact) == 1 or len(results) == 1:
            return RedirectResponse(f"/stock/{(exact or results)[0]['symbol']}", status_code=303)
        return page(request, "search.html", {"query": q_, "results": results, "screens": q.SCREENS, "screen": ""})

    # ---- JSON API ----------------------------------------------------------------
    @app.get("/api/market")
    def api_market(d: date | None = Query(None, alias="date"), conn=Depends(db)):
        """某個交易日（預設最新）的大盤、市場狀況、漲跌家數、類股漲跌與各類訊號數。"""
        day = q.resolve_date(conn, d)
        if day is None:
            raise HTTPException(404, "沒有行情資料")
        ov = q.market_overview(conn, day)
        rows = q.signals_on(conn, day)
        counts = {t: 0 for t in explain.SIGNAL_INFO}
        for r in rows:
            counts[r["signal_type"]] = counts.get(r["signal_type"], 0) + 1
        return {**ov, "state": explain.market_state(ov["index"], ov["breadth"]), "signal_counts": counts,
                "engine_version": ENGINE_VERSION}

    @app.get("/api/live")
    def api_live(symbols: str | None = Query(None, description="股票代號，逗號分隔，最多 40 檔"), conn=Depends(db)):
        """盤中即時：加權指數、櫃買指數、盤中累計成交金額，以及指定股票的即時報價。
        資料來自證交所基本市況報導網站，伺服器每 15 秒（開盤時間）向證交所更新一次；只供本機個人使用，不公開轉載。"""
        now = datetime.now(TAIPEI)
        phase = live.market_phase(now, q.is_trading_day(conn, now.date()))
        syms = live.clean_symbols(symbols)
        data = feed.snapshot(syms, phase, q.symbol_markets(conn, syms))
        return {"phase": phase, "phase_label": live.PHASES[phase], "server_time": now.isoformat(timespec="seconds"),
                "refresh_seconds": live.TTL_OPEN if phase in ("open", "auction") else 300,
                "provider": feed.describe()} | data

    @app.get("/api/backtest")
    def api_backtest(conn=Depends(db)):
        """訊號回測：每種訊號出現後 5／20／60 個交易日（與隔天才買的 20 日）的上漲比例、中位數、四分位數，
        和同一天全部普通股的對照；另附市面說法對照與限制說明。只描述過去，不代表之後。"""
        return backtest_json(backtest_context(conn)["bt"])

    @app.get("/api/signal-types")
    def api_signal_types():
        """每種訊號的定義：名稱、類別、方向、這是什麼／代表什麼／不代表什麼、判斷條件、冷卻期。"""
        return {"engine_version": ENGINE_VERSION, "types": [i.as_dict() for i in explain.SIGNAL_INFO.values()]}

    @app.get("/api/signals")
    def api_signals(d: date | None = Query(None, alias="date"), type: str | None = None, conn=Depends(db)):
        """某個交易日的訊號（標準訊號物件：type、symbol、date、value、threshold、category、explanation…）。"""
        if type is not None and type not in TYPES:
            raise HTTPException(422, f"type 必須是 {list(TYPES)} 之一")
        day = q.resolve_date(conn, d)
        rows = q.signals_on(conn, day, type) if day else []
        return {"date": day, "engine_version": ENGINE_VERSION, "signals": [explain.signal_object(r) for r in rows]}

    @app.get("/api/stocks/{symbol}")
    def api_stock(symbol: str, days: int = Query(250, ge=1, le=1000), conn=Depends(db)):
        """個股：基本資料、日行情與特徵、法人買賣超、訊號歷史、除權息。"""
        prof = q.stock_profile(conn, symbol.upper())
        if prof is None:
            raise HTTPException(404, f"找不到證券代號 {symbol}")
        sid = prof.pop("id")
        return {"profile": prof, "prices": q.stock_prices(conn, sid, days), "flows": q.stock_flows(conn, sid, days),
                "signals": q.stock_signals(conn, sid), "corporate_actions": q.stock_actions(conn, sid)}

    @app.get("/api/stocks/{symbol}/explain")
    def api_stock_explain(symbol: str, conn=Depends(db)):
        """個股「為什麼被雷達注意」：原因、價格事實與一段簡單理解（只描述資料，不預測）。"""
        prof = q.stock_profile(conn, symbol.upper())
        if prof is None:
            raise HTTPException(404, f"找不到證券代號 {symbol}")
        ctx = stock_context(conn, prof, days=RECENT_DAYS + 1, flow_days=1)
        return {"symbol": prof["symbol"], "name": prof["name"],
                "date": ctx["last"]["trade_date"] if ctx["last"] else None, **ctx["story"]}

    @app.get("/api/search")
    def api_search(q_: str = Query(..., alias="q", min_length=1), conn=Depends(db)):
        """用代號開頭或名稱搜尋證券。"""
        return {"results": q.search_stocks(conn, q_)}

    return app
