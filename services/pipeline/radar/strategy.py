"""自訂條件回測：使用者自己組選股條件，看過去符合條件之後股價實際怎麼走，並和同一天全部上市普通股比較。

- 條件分成幾個面向（成交量、今日漲跌、5 日報酬、均線、60 日高低、外資、投信、MACD、成交金額），每個面向選一個選項，
  最多同時選 MAX_PICKS 個面向。所有組合事先算好（約 2,200 組），網站與匯出檔（公開網頁）不需要伺服器就能查。
- 條件只用「當天收盤後」已經知道的資料；報酬從當天收盤起算（5／20／60 個交易日），另有「隔天收盤才買」的 20 日報酬。
  定義、還原方式與比較基準（market_forward_returns：同一天全部上市普通股）都和 radar/outcomes.py 的訊號回測相同。
- 均線、MACD 用原始收盤價（和網站上的技術指標一致）；報酬用還原後的日報酬。
- 只描述過去一段期間的統計，不代表之後，不是推薦，也不評分。
"""
from __future__ import annotations

import itertools
import json
from datetime import date
from pathlib import Path

import numpy as np

from radar import indicators
from radar.explain import MIN_SAMPLES, backtest_verdict
from radar.outcomes import HORIZONS, ROUND_TRIP_COST

MAX_PICKS = 3
VERDICT_CODE = {"better": "b", "same": "s", "worse": "w", "few": "f"}

# 面向 → 選項：(代號, 名稱, 判斷方式)。判斷方式用 _flags() 算出來的欄位
DIMENSIONS = [
    ("vol", "成交量", [("vr2", "量比 ≥ 2 倍", "今天成交量 ≥ 前 20 個交易日平均的 2 倍"),
                      ("vr4", "量比 ≥ 4 倍", "今天成交量 ≥ 前 20 個交易日平均的 4 倍"),
                      ("vr05", "量縮（量比 ≤ 0.5）", "今天成交量 ≤ 前 20 個交易日平均的一半")]),
    ("day", "今日漲跌", [("up5", "漲 5% 以上", "官方漲跌幅 ≥ +5%"), ("up0", "小漲（0～5%）", "官方漲跌幅介於 0 與 +5% 之間"),
                         ("dn0", "小跌（0～5%）", "官方漲跌幅介於 −5% 與 0 之間"), ("dn5", "跌 5% 以上", "官方漲跌幅 ≤ −5%")]),
    ("r5", "最近 5 日", [("r5u10", "5 日漲 10% 以上", "最近 5 個交易日累計漲幅 ≥ 10%（已還原除權息）"),
                        ("r5d10", "5 日跌 10% 以上", "最近 5 個交易日累計跌幅 ≥ 10%（已還原除權息）")]),
    ("ma", "均線", [("abv20", "收盤在 20 日均線之上", "收盤價 > SMA20"), ("blw20", "收盤在 20 日均線之下", "收盤價 < SMA20"),
                   ("bull", "多頭排列", "收盤價 > SMA20 > SMA60"), ("bear", "空頭排列", "收盤價 < SMA20 < SMA60")]),
    ("hl", "60 日高低", [("hi60", "創 60 日新高", "收盤價高於前 60 個交易日的最高價（已還原）"),
                        ("lo60", "創 60 日新低", "收盤價低於前 60 個交易日的最低價（已還原）")]),
    ("fi", "外資", [("fb1", "今天買超", "外資今天買進股數多於賣出"), ("fs1", "今天賣超", "外資今天賣出股數多於買進"),
                   ("fb3", "連買 3 日以上", "外資連續 3 個以上交易日買超"), ("fs3", "連賣 3 日以上", "外資連續 3 個以上交易日賣超")]),
    ("it", "投信", [("tb1", "今天買超", "投信今天買進股數多於賣出"), ("ts1", "今天賣超", "投信今天賣出股數多於買進")]),
    ("macd", "MACD", [("hpos", "DIF 在訊號線之上", "MACD 柱狀體 > 0"), ("hneg", "DIF 在訊號線之下", "MACD 柱狀體 < 0"),
                     ("hup", "柱狀體今天由負轉正", "昨天 ≤ 0、今天 > 0"), ("hdn", "柱狀體今天由正轉負", "昨天 ≥ 0、今天 < 0")]),
    ("liq", "成交金額", [("t1e8", "成交金額 ≥ 1 億", "排除成交冷清、實際上很難買賣的股票")]),
]
OPTIONS = [o[0] for _, _, opts in DIMENSIONS for o in opts]

_ROWS_SQL = """
    SELECT s.id, s.symbol, s.name, i.name AS industry, f.trade_date, p.close, p.turnover,
           p.change / NULLIF(p.close - p.change, 0) AS day_pct,
           f.ret_1d, f.ret_5d, f.vol_ratio, f.high60, f.low60, fl.foreign_net, fl.trust_net
      FROM daily_features f
      JOIN stocks s ON s.id = f.stock_id AND s.market = 'TWSE' AND s.security_type = 'stock'
      JOIN daily_prices p ON p.stock_id = f.stock_id AND p.trade_date = f.trade_date
      LEFT JOIN industries i ON i.id = s.industry_id
      LEFT JOIN institutional_flows fl ON fl.stock_id = f.stock_id AND fl.trade_date = f.trade_date
     WHERE f.trade_date <= %(d)s
     ORDER BY s.id, f.trade_date"""


def _f(v) -> float:
    return np.nan if v is None else float(v)


def _forward(ret_1d: np.ndarray, a: int, b: int) -> np.ndarray:
    """從第 i 列收盤起算，第 a～b 列的日報酬連乘（和 outcomes.py 一樣以這檔自己的列數計）；不滿就是 nan。"""
    lr = np.log1p(np.nan_to_num(ret_1d, nan=0.0))
    cs = np.concatenate([[0.0], np.cumsum(lr)])
    n = len(ret_1d)
    out = np.full(n, np.nan)
    i = np.arange(n)
    ok = i + b < n
    out[ok] = np.expm1(cs[i[ok] + b + 1] - cs[i[ok] + a])
    return out


def _streak(x: np.ndarray) -> np.ndarray:
    """外資連續買（正數）或賣（負數）超的天數；沒有資料或 0 就歸零。"""
    out = np.zeros(len(x), dtype=np.int32)
    run = 0
    for i, v in enumerate(x):
        if np.isnan(v) or v == 0:
            run = 0
        elif v > 0:
            run = run + 1 if run > 0 else 1
        else:
            run = run - 1 if run < 0 else -1
        out[i] = run
    return out


def _load(conn, d: date) -> dict:
    """每一列＝一檔上市普通股的一天。回傳欄位陣列（同長度）與每檔的起訖位置。"""
    rows = conn.execute(_ROWS_SQL, {"d": d}).fetchall()
    cols = {k: [] for k in ("sid", "date", "close", "turnover", "day", "ret1", "r5", "vr", "high60", "low60", "fnet", "tnet")}
    names = {}
    for r in rows:
        sid = r[0]
        names[sid] = (r[1], r[2], r[3])
        cols["sid"].append(sid)
        cols["date"].append(r[4])
        for k, v in zip(("close", "turnover", "day", "ret1", "r5", "vr", "high60", "low60", "fnet", "tnet"), r[5:]):
            cols[k].append(_f(v))
    data = {k: (np.array(v, dtype=float) if k not in ("sid", "date") else v) for k, v in cols.items()}
    data["sid"] = np.array(cols["sid"], dtype=np.int64)
    data["names"] = names
    # 每檔股票各自算均線、MACD、外資連續天數、後續報酬
    n = len(rows)
    for k in ("sma20", "sma60", "hist", "hist_prev", "fstreak", *HORIZONS):
        data[k] = np.full(n, np.nan)
    starts = np.flatnonzero(np.r_[True, data["sid"][1:] != data["sid"][:-1]]) if n else np.array([], dtype=int)
    ends = np.r_[starts[1:], n] if n else starts
    for a, b in zip(starts, ends):
        closes = [None if np.isnan(c) else c for c in data["close"][a:b]]
        sma20, sma60 = indicators.sma(closes, 20), indicators.sma(closes, 60)
        hist = indicators.macd(closes)["hist"]
        data["sma20"][a:b] = [np.nan if v is None else v for v in sma20]
        data["sma60"][a:b] = [np.nan if v is None else v for v in sma60]
        h = np.array([np.nan if v is None else v for v in hist])
        data["hist"][a:b] = h
        data["hist_prev"][a:b] = np.r_[np.nan, h[:-1]]
        data["fstreak"][a:b] = _streak(data["fnet"][a:b])
        for name, (lo, hi) in HORIZONS.items():
            data[name][a:b] = _forward(data["ret1"][a:b], lo, hi)
    data["is_last"] = np.zeros(n, dtype=bool)
    if n:
        data["is_last"][ends - 1] = True
    return data


def _flags(x: dict) -> dict[str, np.ndarray]:
    """每個選項在每一列成立與否（資料缺的就是不成立）。"""
    with np.errstate(invalid="ignore"):
        c, s20, s60, day, h, hp = x["close"], x["sma20"], x["sma60"], x["day"], x["hist"], x["hist_prev"]
        return {
            "vr2": x["vr"] >= 2, "vr4": x["vr"] >= 4, "vr05": x["vr"] <= 0.5,
            "up5": day >= 0.05, "up0": (day > 0) & (day < 0.05), "dn0": (day < 0) & (day > -0.05), "dn5": day <= -0.05,
            "r5u10": x["r5"] >= 0.10, "r5d10": x["r5"] <= -0.10,
            "abv20": c > s20, "blw20": c < s20, "bull": (c > s20) & (s20 > s60), "bear": (c < s20) & (s20 < s60),
            "hi60": c > x["high60"], "lo60": c < x["low60"],
            "fb1": x["fnet"] > 0, "fs1": x["fnet"] < 0, "fb3": x["fstreak"] >= 3, "fs3": x["fstreak"] <= -3,
            "tb1": x["tnet"] > 0, "ts1": x["tnet"] < 0,
            "hpos": h > 0, "hneg": h < 0, "hup": (hp <= 0) & (h > 0), "hdn": (hp >= 0) & (h < 0),
            "t1e8": x["turnover"] >= 1e8,
        }


def combos() -> list[tuple[str, ...]]:
    """所有「每個面向最多一個選項、最多 MAX_PICKS 個面向」的組合（依面向順序）。"""
    out = []
    for k in range(1, MAX_PICKS + 1):
        for dims in itertools.combinations(DIMENSIONS, k):
            out.extend(itertools.product(*[[o[0] for o in opts] for _, _, opts in dims]))
    return out


def _stats(vals: np.ndarray, bu: np.ndarray, bm: np.ndarray) -> list | None:
    if vals.size == 0:
        return None
    p25, med, p75 = np.percentile(vals, [25, 50, 75])
    r = lambda v: round(float(v), 4)
    return [int(vals.size), r((vals > 0).mean()), r((vals > ROUND_TRIP_COST).mean()), r(med), r(vals.mean()), r(p25), r(p75),
            r(np.nanmean(bu)), r(np.nanmean(bm))]


def build_lab(conn, d: date) -> dict:
    """自訂條件回測的完整結果（JSON 可序列化）：面向與選項、每個組合的統計、d 這天各股票符合哪些條件。"""
    x = _load(conn, d)
    flags = _flags(x)
    days = sorted(set(x["date"]))
    day_idx = {t: i for i, t in enumerate(days)}
    di = np.array([day_idx[t] for t in x["date"]], dtype=np.int64)
    base = {h: {} for h in HORIZONS}
    for t, h, up, med in conn.execute("SELECT trade_date, horizon, up_share, median FROM market_forward_returns WHERE trade_date <= %s", (d,)):
        base[h][t] = (float(up), float(med))
    bu = {h: np.array([base[h].get(t, (np.nan, np.nan))[0] for t in x["date"]]) for h in HORIZONS}
    bm = {h: np.array([base[h].get(t, (np.nan, np.nan))[1] for t in x["date"]]) for h in HORIZONS}
    valid = {h: ~np.isnan(x[h]) & ~np.isnan(bu[h]) for h in HORIZONS}
    sid_codes = np.unique(x["sid"], return_inverse=True)[1] if len(x["sid"]) else np.array([], dtype=int)

    def summarize(mask: np.ndarray) -> list:
        ev = int(mask.sum())
        stocks = int(np.count_nonzero(np.bincount(sid_codes[mask]))) if ev else 0
        n_days = int(np.count_nonzero(np.bincount(di[mask]))) if ev else 0
        hs = []
        for h in HORIZONS:
            m = mask & valid[h]
            hs.append(_stats(x[h][m], bu[h][m], bm[h][m]))
        s20 = hs[list(HORIZONS).index("h20")]
        v = backtest_verdict(None if not s20 else {"n": s20[0], "diff_win": s20[1] - s20[7], "diff_med": s20[3] - s20[8]})
        return [ev, stocks, n_days, VERDICT_CODE[v["key"]], *hs]

    results = {"+".join(c): summarize(np.logical_and.reduce([flags[o] for o in c])) for c in combos()}
    everything = summarize(np.ones(len(x["sid"]), dtype=bool))

    # d 這天（每檔股票的最後一列就是 d）各股票符合哪些條件：網頁上列出「今天符合這組條件的股票」
    today = []
    for i in np.flatnonzero(x["is_last"] & np.array([t == d for t in x["date"]], dtype=bool)):
        sym, name, ind = x["names"][int(x["sid"][i])]
        on = " ".join(o for o in OPTIONS if flags[o][i])
        today.append([sym, name, ind or "", round(float(x["close"][i]), 2), None if np.isnan(x["day"][i]) else round(float(x["day"][i]), 4), on])
    with_returns = [t for t, ok in zip(x["date"], valid["h20"]) if ok]
    return {
        "date": d, "cost": ROUND_TRIP_COST, "max_picks": MAX_PICKS, "min_samples": MIN_SAMPLES,
        "period": {"start": min(with_returns) if with_returns else None, "end": max(with_returns) if with_returns else None},
        "horizons": [{"key": "h5", "label": "5 個交易日後"}, {"key": "h20", "label": "20 個交易日後"},
                     {"key": "h60", "label": "60 個交易日後"}, {"key": "d20", "label": "隔天才買，20 個交易日後"}],
        "fields": ["筆數", "上漲比例", "扣成本後上漲比例", "中位數", "平均", "第 25 百分位", "第 75 百分位", "全部股票上漲比例", "全部股票中位數"],
        "dims": [{"key": k, "label": label, "options": [{"key": o, "label": ol, "rule": rule} for o, ol, rule in opts]}
                 for k, label, opts in DIMENSIONS],
        "all": everything, "combos": results, "today": today,
    }


def _default(v):
    if isinstance(v, date):
        return v.isoformat()
    raise TypeError(type(v))


def to_json(lab: dict) -> str:
    return json.dumps(lab, ensure_ascii=False, separators=(",", ":"), default=_default)


def load_or_build(conn, d: date, cache_dir: Path, version: str = "") -> str:
    """同一天、同一版資料只算一次（約 20～40 秒），結果存成 JSON 檔；回傳 JSON 字串。"""
    path = cache_dir / f"lab_{d.isoformat()}_{version}.json"
    if path.exists():
        return path.read_text(encoding="utf-8")
    text = to_json(build_lab(conn, d))
    cache_dir.mkdir(parents=True, exist_ok=True)
    for old in cache_dir.glob(f"lab_{d.isoformat()}_*.json"):
        old.unlink(missing_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return text


# ---- 頁面用：常見組合（不能執行程式時也看得到結果）---------------------------------------------
PRESETS = ["vr2+up5", "vr4", "hi60+vr2", "dn5+vr2", "abv20+fb3", "bull+t1e8", "lo60", "abv20+hup", "fs3", "tb1+t1e8"]
VERDICT_LABELS = {"b": ("過去比全部股票好一些", "up"), "s": ("和全部股票差不多", "flat"),
                  "w": ("過去比全部股票差一些", "down"), "f": ("樣本太少，看不出來", "flat")}


def option_labels(lab: dict) -> dict[str, str]:
    return {o["key"]: f"{dim['label']}：{o['label']}" for dim in lab["dims"] for o in dim["options"]}


def presets(lab: dict) -> list[dict]:
    """常見組合的 20 個交易日結果（伺服器端算好，直接放在頁面上）。"""
    names = option_labels(lab)
    out = []
    for key in PRESETS:
        r = lab["combos"].get(key)
        if not r:
            continue
        h20 = r[5]
        label, tone = VERDICT_LABELS[r[3]]
        out.append({"key": key, "label": "＋".join(names[k] for k in key.split("+")), "events": r[0], "stocks": r[1],
                    "verdict": label, "tone": tone, "h20": h20})
    return out
