"""解釋層：每種訊號都有完整說明、文字只描述不建議、市場狀況判斷。"""
from datetime import date

from radar import explain
from radar import signals as sg

# 「不代表股價會上漲」這種否定句是說明的一部分，所以不檢查「會上漲」；檢查的是給建議或下判斷的用語
ADVICE = ("建議", "值得", "看好", "看壞", "應該買", "應該賣", "逢低", "布局", "目標價", "必漲", "必跌", "買點", "賣點")


def _no_advice(text: str):
    bad = [w for w in ADVICE if w in text]
    assert not bad, f"出現建議用語 {bad}：{text}"


def test_every_signal_type_has_three_part_explanation():
    assert set(explain.SIGNAL_INFO) == set(sg.TYPES)
    for i in explain.SIGNAL_INFO.values():
        assert i.label == sg.TYPES[i.type] and i.category in explain.CATEGORIES and i.tone in ("up", "down", "neutral")
        assert i.what and i.means and i.not_mean.startswith(("成交量增加不代表", "創新高不代表", "創新低不代表", "短期",
                                                               "外資", "投信"))
        assert "不代表" in i.not_mean
        for text in (i.what, i.means, i.not_mean, i.rule):
            _no_advice(text)


def test_rule_text_follows_engine_constants(monkeypatch):
    monkeypatch.setattr(sg, "VOL_RATIO_MIN", 5.0)
    assert "5 倍" in explain.rule_text("vol_spike")          # 改門檻，說明跟著變
    assert explain.SIGNAL_INFO["vol_spike"].cooldown == sg.COOLDOWN["vol_spike"]


def test_market_state_levels():
    b = lambda a, d: {"advancers": a, "decliners": d}
    assert explain.market_state({"change_pct": -0.28}, b(386, 546))["label"] == "偏弱"
    assert explain.market_state({"change_pct": 1.2}, b(700, 300))["label"] == "偏強"
    mixed = explain.market_state({"change_pct": 0.5}, b(300, 600))
    assert mixed["label"] == "分歧" and "大型股" in mixed["sentence"]
    assert explain.market_state({"change_pct": 0.02}, b(500, 490))["label"] == "持平"
    assert explain.market_state(None, None)["level"] == "unknown"
    for s in (mixed, explain.market_state({"change_pct": -0.28}, b(386, 546))):
        _no_advice(s["sentence"])


def test_stock_story_describes_without_predicting():
    last = {"trade_date": date(2026, 9, 24), "close": 50.4, "change": 4.2, "ret_5d": -0.1628, "ret_20d": -0.2364,
            "vol_ratio": 20.59, "high60": 80, "low60": 20.35}
    sig = {"trade_date": date(2026, 9, 24), "signal_type": "vol_spike", "value": 20.59,
           "evidence": {"turnover": 120_000_000}}
    story = explain.stock_story(last, [sig], [])
    assert story["flagged"] and story["reasons"][0]["label"] == "量能爆增"
    assert "20.6 倍" in story["summary"] and "上漲 9.09%" in story["summary"] and "-16.28%" in story["summary"]
    assert "但過去 5 個交易日仍累計" in story["summary"]                       # 今天漲、近期仍是跌：用「但」
    for text in [story["summary"]] + [r["text"] for r in story["reasons"]] + [f["text"] for f in story["facts"]]:
        _no_advice(text)
    quiet = explain.stock_story(last | {"vol_ratio": 1.0, "change": 0.1}, [], [sig])
    assert not quiet["flagged"] and quiet["recent"]["label"] == "量能爆增"


def test_methodology_has_required_sections():
    titles = [t for t, _ in explain.METHODOLOGY]
    for need in ("資料來源", "更新時間", "還原價", "報酬率", "量比", "冷卻機制"):
        assert need in titles
    for title, lines in explain.METHODOLOGY:
        if title == "這個網站不做什麼":        # 這一節本來就在說「不提供建議」
            continue
        for line in lines:
            _no_advice(line)


def test_key_points_digest_and_cards_only_describe():
    from radar.explain import anomaly_digest, key_points, market_state, past_performance, signal_chip, signal_metrics
    idx, br = {"change_pct": 0.25}, {"advancers": 483, "decliners": 506}
    rows = [
        {"signal_type": "vol_spike", "value": 8.9, "symbol": "9933", "name": "中鼎", "industry": "其他", "day_pct": 0.0996,
         "vol_ratio": 8.9, "ret_5d": 0.1144, "turnover": 9.7e8, "evidence": {"turnover": 9.7e8}},
        {"signal_type": "vol_spike", "value": 5.3, "symbol": "4722", "name": "國精化", "industry": "化學", "day_pct": 0.0979,
         "vol_ratio": 5.3, "ret_5d": 0.2169, "turnover": 1.02e9, "evidence": {"turnover": 1.02e9}},
        {"signal_type": "surge_5d", "value": 0.2169, "symbol": "4722", "name": "國精化", "industry": "化學", "day_pct": 0.0979,
         "vol_ratio": 5.3, "ret_5d": 0.2169, "turnover": 1.02e9, "evidence": {"ret_5d": 0.2169}},
    ]
    sectors = [{"name": "油電燃氣類指數", "change_pct": 5.51}, {"name": "食品類指數", "change_pct": -1.14}]
    pts = key_points(market_state(idx, br), idx, br, rows, sectors)
    assert [p["key"] for p in pts] == ["market", "radar", "sectors"]
    assert "分歧" in pts[0]["title"] and "483" in pts[0]["text"] and "506" in pts[0]["text"]
    assert "2 檔出現「量能爆增」" in pts[1]["text"] and "9933 中鼎" in pts[1]["text"]          # 最多的一種、最明顯的一檔
    assert "油電燃氣 +5.51%" in pts[2]["text"] and "食品 -1.14%" in pts[2]["text"]
    digest = anomaly_digest(rows)
    assert [d["symbol"] for d in digest] == ["4722", "9933"]                                   # 訊號數優先，再看量比
    assert digest[0]["metrics"][0] == {"label": "量比", "value": "5.3×", "tone": None}           # 量比不上色，只有漲跌上色
    assert signal_metrics(rows[0])[:2] == [{"label": "量比", "value": "8.9×", "tone": None},
                                           {"label": "今日", "value": "+9.96%", "tone": "up"}]
    assert signal_chip(rows[0])["value"] == "8.9×"
    for text in [p["text"] for p in pts] + [d["sentence"] for d in digest]:
        _no_advice(text)
    # 歷史訊號表現：樣本不夠就不顯示數字
    few = {"h20": {"n": 5, "median": 0.01, "base_median": 0.0, "diff_win": 0.1, "diff_med": 0.01}}
    assert past_performance(few) == {"enough": False, "n": 5, "rows": []}
    s = {"n": 400, "median": 0.023, "base_median": 0.011, "win": 0.6, "base_win": 0.52, "diff_win": 0.08, "diff_med": 0.012}
    p = past_performance({"h5": s, "h20": s})
    assert p["enough"] and [r["median"] for r in p["rows"]] == ["+2.3%", "+2.3%"] and p["rows"][0]["base"] == "+1.1%"
