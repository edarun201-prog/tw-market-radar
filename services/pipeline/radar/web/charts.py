"""伺服器端產生的 SVG 圖表：不需要前端圖表庫，也不需要建置。

顏色一律用 CSS class（.up／.down／.axis…），由頁面的樣式表決定，淺色、深色主題都適用。
台股慣例：紅漲綠跌。
"""
from __future__ import annotations

import math
from datetime import date
from html import escape

from radar.explain import tone_of

def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if raw <= m * mag)
    first = math.ceil(lo / step) * step
    return [round(first + i * step, 10) for i in range(int((hi - first) / step) + 1)]


def _fmt_tick(v: float, step: float) -> str:
    if step >= 1:
        return f"{v:,.0f}"
    return f"{v:,.2f}" if step < 0.1 else f"{v:,.1f}"


def _f(v) -> float | None:
    return None if v is None else float(v)


def _month_labels(dates: list[date]) -> list[tuple[int, str]]:
    out, prev = [], None
    for i, d in enumerate(dates):
        if prev is None or d.month != prev.month:
            out.append((i, f"{d.year}" if d.month == 1 or prev is None else f"{d.month}月"))
        prev = d
    return out[1:] if len(out) > 1 and out[1][0] - out[0][0] < 12 else out


MA_LINES = (("sma5", "ma5", "SMA5"), ("sma20", "ma20", "SMA20"), ("sma60", "ma60", "SMA60"))


def _path(xs: list[float], ys: list[float | None]) -> str:
    """折線：遇到沒有值的日子就斷開。"""
    out, pen = [], False
    for x, y in zip(xs, ys):
        if y is None:
            pen = False
            continue
        out.append(f"{'L' if pen else 'M'}{x:.1f},{y:.1f}")
        pen = True
    return "".join(out)


def price_chart(prices: list[dict], signals: list[dict], actions: list[dict], labels: dict[str, str],
                width: int = 960, ind: dict[str, list] | None = None) -> str:
    """K 線＋成交量。signals、actions 會標在對應日期上（滑過顯示說明）。
    ind：技術指標（和 prices 同長度），有的話畫 SMA5／20／60（標準模式才顯示，class="std-only"）。"""
    rows = [r for r in prices if r["close"] is not None]
    if not rows:
        return '<p class="empty">這段期間沒有成交資料。</p>'
    left, right, top = 8, 64, 16
    price_h, gap, vol_h, bottom = 280, 14, 72, 24
    height = top + price_h + gap + vol_h + bottom
    n = len(prices)
    plot_w = width - left - right
    step = plot_w / max(n, 1)
    x = lambda i: left + step * (i + 0.5)

    lo = min(_f(r["low"]) for r in rows)
    hi = max(_f(r["high"]) for r in rows)
    ma_vals = [v for key, _, _ in MA_LINES for v in (ind or {}).get(key, []) if v is not None]
    if ma_vals:
        lo, hi = min(lo, min(ma_vals)), max(hi, max(ma_vals))
    pad = (hi - lo) * 0.06 or hi * 0.02 or 1
    lo, hi = lo - pad, hi + pad
    y = lambda v: top + (hi - v) / (hi - lo) * price_h
    vmax = max((r["volume"] or 0) for r in prices) or 1
    vy0 = top + price_h + gap + vol_h
    vy = lambda v: vy0 - v / vmax * vol_h

    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="K 線與成交量">']
    ticks = nice_ticks(lo, hi)
    tstep = ticks[1] - ticks[0] if len(ticks) > 1 else 1
    for t in ticks:
        out.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                   f'<text class="axis" x="{width - right + 6}" y="{y(t) + 4:.1f}">{_fmt_tick(t, tstep)}</text>')
    dates = [r["trade_date"] for r in prices]
    for i, label in _month_labels(dates):
        out.append(f'<line class="grid" x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{top}" y2="{vy0}"/>'
                   f'<text class="axis" x="{x(i):.1f}" y="{height - 6}" text-anchor="middle">{label}</text>')

    bw = max(1.0, step * 0.68)
    for i, r in enumerate(prices):
        if r["close"] is None:
            continue
        o, h, l, c = _f(r["open"]), _f(r["high"]), _f(r["low"]), _f(r["close"])
        cls = "up" if (r["change"] or 0) > 0 else "down" if (r["change"] or 0) < 0 else "flat"
        body_top, body_bot = y(max(o, c)), y(min(o, c))
        tip = (f'{r["trade_date"]:%Y/%m/%d}　開 {o:g}　高 {h:g}　低 {l:g}　收 {c:g}'
               f'　量 {r["volume"] / 1000:,.0f} 張')
        out.append(f'<g class="{cls}"><title>{tip}</title>'
                   f'<line class="wick" x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{y(h):.1f}" y2="{y(l):.1f}"/>'
                   f'<rect x="{x(i) - bw / 2:.1f}" y="{body_top:.1f}" width="{bw:.1f}" '
                   f'height="{max(1.0, body_bot - body_top):.1f}"/>'
                   f'<rect class="vol" x="{x(i) - bw / 2:.1f}" y="{vy(r["volume"] or 0):.1f}" width="{bw:.1f}" '
                   f'height="{vy0 - vy(r["volume"] or 0):.1f}"/></g>')

    if ind:
        xs = [x(i) for i in range(n)]
        out.append('<g class="ma std-only">')
        for key, cls, label in MA_LINES:
            ys = [y(v) if v is not None else None for v in ind.get(key, [None] * n)]
            if any(v is not None for v in ys):
                out.append(f'<path class="{cls}" d="{_path(xs, ys)}"><title>{label}</title></path>')
        out.append("</g>")

    index = {d: i for i, d in enumerate(dates)}
    for s in signals:
        i = index.get(s["trade_date"])
        if i is None or prices[i]["close"] is None:
            continue
        tone = tone_of(s["signal_type"])
        bear = tone == "down"
        cls = f"mark-{tone}"
        yy = y(_f(prices[i]["low"])) + 12 if bear else y(_f(prices[i]["high"])) - 12
        if tone == "neutral":                                   # 中性：菱形
            yy = y(_f(prices[i]["high"])) - 22
            tri = f"{x(i):.1f},{yy - 4:.1f} {x(i) + 4:.1f},{yy:.1f} {x(i):.1f},{yy + 4:.1f} {x(i) - 4:.1f},{yy:.1f}"
        else:
            tri = (f"{x(i) - 4:.1f},{yy - 4:.1f} {x(i) + 4:.1f},{yy - 4:.1f} {x(i):.1f},{yy + 3:.1f}" if bear else
                   f"{x(i) - 4:.1f},{yy + 4:.1f} {x(i) + 4:.1f},{yy + 4:.1f} {x(i):.1f},{yy - 3:.1f}")
        out.append(f'<polygon class="{cls}" points="{tri}"><title>{s["trade_date"]:%Y/%m/%d} '
                   f'{escape(labels.get(s["signal_type"], s["signal_type"]))}</title></polygon>')
    for a in actions:
        i = next((j for j, d in enumerate(dates) if d >= a["ex_date"]), None)
        if i is None or a["ex_date"] < dates[0]:
            continue
        kind = {"dividend": "除息", "rights": "除權", "both": "除權息"}[a["action_type"]]
        when = f'{dates[i]:%Y/%m/%d}' + (f'（原訂 {a["ex_date"]:%m/%d}，休市順延）' if dates[i] != a["ex_date"] else "")
        out.append(f'<g class="ex"><title>{when} {kind}：權值＋息值 {float(a["value"]):g} 元'
                   f'（參考價 {float(a["ref_price"]):g}）</title>'
                   f'<line x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{top}" y2="{top + price_h}"/>'
                   f'<text x="{x(i) + 3:.1f}" y="{top + price_h - 4}">{kind}</text></g>')

    last = rows[-1]
    ly = y(_f(last["close"]))
    lcls = "up" if (last["change"] or 0) > 0 else "down" if (last["change"] or 0) < 0 else "flat"
    out.append(f'<line class="last {lcls}" x1="{left}" x2="{width - right}" y1="{ly:.1f}" y2="{ly:.1f}"/>'
               f'<rect class="tag {lcls}" x="{width - right + 2}" y="{ly - 9:.1f}" width="{right - 4}" height="18" rx="3"/>'
               f'<text class="tag-text" x="{width - right + 6}" y="{ly + 4:.1f}">{_f(last["close"]):,g}</text>')
    out.append(f'<text class="axis" x="{left}" y="{top + price_h + gap + 10}">成交量（張）最高 {vmax / 1000:,.0f}</text>')
    out.append("</svg>")
    return "".join(out)


def flows_chart(flows: list[dict], width: int = 960) -> str:
    """外資、投信、自營商每日買賣超（張），各一列、以 0 為中線。"""
    if not flows:
        return '<p class="empty">沒有法人買賣超資料。</p>'
    series = [("外資", "foreign_net"), ("投信", "trust_net"), ("自營商", "dealer_net")]
    left, right, row_h, gap, top = 92, 64, 54, 12, 6
    height = top + len(series) * (row_h + gap)
    step = (width - left - right) / len(flows)
    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="三大法人買賣超">']
    for k, (label, key) in enumerate(series):
        vals = [(f[key] or 0) / 1000 for f in flows]
        m = max((abs(v) for v in vals), default=0) or 1
        y0 = top + k * (row_h + gap) + row_h / 2
        total = sum(vals[-20:])
        tcls = "up" if total > 0 else "down" if total < 0 else "flat"
        out.append(f'<text class="axis strong" x="0" y="{y0 - 6:.1f}">{label}</text>'
                   f'<text class="axis {tcls}-text" x="0" y="{y0 + 12:.1f}">近20日 {total:+,.0f}</text>'
                   f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y0:.1f}" y2="{y0:.1f}"/>'
                   f'<text class="axis" x="{width - right + 6}" y="{y0 - row_h / 2 + 10:.1f}">{m:,.0f}</text>')
        for i, (f, v) in enumerate(zip(flows, vals)):
            h = abs(v) / m * (row_h / 2)
            cls = "up" if v > 0 else "down"
            yy = y0 - h if v > 0 else y0
            out.append(f'<rect class="{cls} bar" x="{left + step * i + step * 0.15:.1f}" y="{yy:.1f}" '
                       f'width="{max(1.0, step * 0.7):.1f}" height="{max(0.5, h):.1f}">'
                       f'<title>{f["trade_date"]:%Y/%m/%d} {label} {v:+,.0f} 張</title></rect>')
    out.append("</svg>")
    return "".join(out)


def sparkline(points: list[tuple[date, float]], width: int = 320, height: int = 72) -> str:
    if len(points) < 2:
        return ""
    vals = [float(v) for _, v in points]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    xs = [i / (len(vals) - 1) * (width - 4) + 2 for i in range(len(vals))]
    ys = [height - 6 - (v - lo) / span * (height - 12) for v in vals]
    line = " ".join(f"{a:.1f},{b:.1f}" for a, b in zip(xs, ys))
    area = f"2,{height} " + line + f" {xs[-1]:.1f},{height}"
    cls = "up" if vals[-1] >= vals[0] else "down"
    return (f'<svg class="spark {cls}" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
            f'aria-label="加權指數近 {len(vals)} 個交易日走勢">'
            f'<polygon class="area" points="{area}"/><polyline points="{line}"/>'
            f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="3"/></svg>')


def macd_chart(prices: list[dict], ind: dict[str, list], width: int = 960) -> str:
    """MACD：DIF（白線）、MACD 訊號線（藍線）、OSC 柱狀體（正值紅、負值綠），以 0 為中線。"""
    dif, sig, hist = ind.get("dif", []), ind.get("signal", []), ind.get("hist", [])
    vals = [abs(v) for v in dif + sig + hist if v is not None]
    if not vals:
        return '<p class="empty small">資料不足 35 個交易日，還算不出 MACD。</p>'
    left, right, top, h, bottom = 8, 64, 14, 120, 22
    height = top + h + bottom
    n = len(prices)
    step = (width - left - right) / max(n, 1)
    x = lambda i: left + step * (i + 0.5)
    m = max(vals) * 1.1 or 1
    y = lambda v: top + (m - v) / (2 * m) * h
    out = [f'<svg class="chart macd" viewBox="0 0 {width} {height}" role="img" aria-label="MACD">',
           f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>',
           f'<text class="axis" x="{width - right + 6}" y="{y(0) + 4:.1f}">0</text>',
           f'<text class="axis" x="{width - right + 6}" y="{y(m * 0.8) + 4:.1f}">{m * 0.8:,.2f}</text>',
           f'<text class="axis" x="{width - right + 6}" y="{y(-m * 0.8) + 4:.1f}">{-m * 0.8:,.2f}</text>']
    dates = [r["trade_date"] for r in prices]
    for i, label in _month_labels(dates):
        out.append(f'<text class="axis" x="{x(i):.1f}" y="{height - 6}" text-anchor="middle">{label}</text>')
    bw = max(1.0, step * 0.6)
    for i, v in enumerate(hist):
        if v is None:
            continue
        y0, y1 = sorted((y(0), y(v)))
        cls = "up" if v > 0 else "down" if v < 0 else "flat"
        out.append(f'<rect class="{cls}" x="{x(i) - bw / 2:.1f}" y="{y0:.1f}" width="{bw:.1f}" height="{max(0.5, y1 - y0):.1f}">'
                   f'<title>{dates[i]:%Y/%m/%d}　DIF {dif[i]:.2f}　MACD {sig[i]:.2f}　OSC {v:+.2f}</title></rect>')
    xs = [x(i) for i in range(n)]
    out.append(f'<path class="dif" d="{_path(xs, [y(v) if v is not None else None for v in dif])}"><title>DIF</title></path>')
    out.append(f'<path class="sig" d="{_path(xs, [y(v) if v is not None else None for v in sig])}"><title>MACD 訊號線</title></path>')
    out.append(f'<text class="axis" x="{left}" y="{top - 2}">MACD（12, 26, 9）</text>')
    out.append("</svg>")
    return "".join(out)
