"""匯出檔：一個離線可看的 HTML。"""
import json
import re

from radar import summary
from radar.web.export import export_day

from .test_web import market  # noqa: F401


def test_export_is_self_contained(conn, market, tmp_path):  # noqa: F811
    d = market[40]                                                              # 這天有量能爆增訊號
    summary.simulate(conn, d)
    path = export_day(conn, d, tmp_path)
    html = path.read_text(encoding="utf-8")
    assert path.name == f"台股雷達_{d:%Y-%m-%d}.html"
    assert not (tmp_path / "台股雷達_最新.html").exists()                      # 不是最新交易日，不產生「最新」
    assert "<style>" in html and 'href="/static' not in html                  # 樣式內嵌
    assert not re.findall(r'(?:href|src)="/', html)                          # 不連回網站
    external = {re.match(r"https?://([^/]+)", u).group(1) for u in re.findall(r'(?:href|src)="(https?://[^"]+)"', html)}
    assert external <= {"fonts.googleapis.com", "fonts.gstatic.com", "db.onlinewebfonts.com", "cdnjs.cloudflare.com",
                        "d8j0ntlcm91z4.cloudfront.net"}                              # 只有入口頁的字型、圖示與背景影片（沒網路時退回）
    anchors = set(re.findall(r'id="([^"]+)"', html))
    views = set(re.findall(r'data-view="([^"]+)"', html))
    assert {"home", "radar", "search", "about", "stock"} <= views              # 一個檔案、多個頁面
    assert "s-2330" in anchors and 'data-view="stock"' in html and "<svg" in html
    assert "盤後摘要（模擬）" in html and 'action="/search"' not in html      # 搜尋不連伺服器
    assert 'class="tabbar"' in html and 'href="#v-search"' in html            # 手機版底部分頁列切換檔案裡的頁面
    for cls in ("links", "tabbar"):                                            # 桌面與手機同一組頁內項目
        start = html.index(f'<nav class="{cls}"')
        assert re.findall(r'href="([^"]+)"', html[start:html.index("</nav>", start)]) == ["#v-market", "#v-radar", "#v-backtest", "#v-search", "#v-about"]
    # 離線搜尋索引：當天每一檔上市證券一列，有個股段落的會標記
    index = json.loads(re.search(r'<script type="application/json" id="quote-index">(.*?)</script>', html, re.S).group(1))
    by_symbol = {r[0]: r for r in index}
    assert len(index) == conn.execute("SELECT count(*) FROM daily_prices WHERE trade_date = %s", (d,)).fetchone()[0]
    assert by_symbol["2330"][8] == 1 and by_symbol["2330"][9]                  # 有訊號、有段落
    assert all(r[8] == 0 for r in index if f'id="s-{r[0]}"' not in html)        # 沒有段落的不會連過去
    assert set(re.findall(r'href="#([^"]+)"', html)) <= anchors               # 每個頁內連結都有對應的頁面或段落
    assert "serviceWorker" not in html and "manifest" not in html              # 單一檔案不註冊 App 快取
    assert 'rel="icon" href="data:image/svg+xml;base64,' in html               # 圖示也內嵌


def test_exporting_a_past_day_keeps_latest(conn, market, tmp_path):  # noqa: F811
    export_day(conn, market[-1], tmp_path)
    latest = (tmp_path / "台股雷達_最新.html").read_text(encoding="utf-8")
    export_day(conn, market[40], tmp_path)                                       # 過去的日子
    assert (tmp_path / "台股雷達_最新.html").read_text(encoding="utf-8") == latest
    assert (tmp_path / f"台股雷達_{market[40]:%Y-%m-%d}.html").exists()


def test_past_day_stock_sections_stop_at_that_day(conn, market, tmp_path):  # noqa: F811
    d, latest = market[40], market[-1]
    html = export_day(conn, d, tmp_path).read_text(encoding="utf-8")
    start = html.index('id="s-2330"')
    section = html[start:html.index("</article>", start)]
    assert f"{d:%Y/%m/%d}" in section                                         # 收盤日是匯出的那天
    assert f"{latest:%Y/%m/%d}" not in section                                 # 看不到之後的資料
    assert "為什麼被雷達抓到" in section and "沒有被雷達標記" not in section    # 那天有訊號，說明也是那天的


def test_pages_switch_without_javascript(conn, market, tmp_path):  # noqa: F811
    # iPhone／iPad 從 LINE、「檔案」預覽 HTML 時不執行程式：換頁要只靠網址的 # 與 CSS :target，模式切換要只靠單選按鈕
    html = export_day(conn, market[40], tmp_path).read_text(encoding="utf-8")
    css = html[html.index("<style>"):html.index("</style>")]
    assert ".views:has(:target) > .view:not(:target):not(:has(:target))" in css
    assert ".views:not(:has(:target)) > .view:not(.default)" in css
    assert 'class="view landing default" id="v-landing"' in html              # 沒有 # 時顯示入口頁
    views = re.findall(r'<(?:div|article|section) class="view[^"]*" id="([^"]+)"', html)
    assert views[:5] == ["v-landing", "v-market", "v-radar", "v-backtest", "v-search"] and views[-1] == "v-about" and "s-2330" in views
    assert 'class="lp-cta" href="#v-market"' in html                           # 入口頁的按鈕進到今日市場
    assert all("/" not in v for v in views)                                    # 一般的 id，不是 #/radar 這種需要程式解析的網址
    assert 'type="radio" name="radar-mode" id="mode-beginner"' in html and "html:not(.js):has(#mode-standard:checked) :is(.beg-only, .adv-only)" in css
    assert '<div class="needs-js">' in html and "<noscript>" in html            # 搜尋需要程式，不能用時說明並改列個股頁
