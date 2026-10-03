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
