"""解釋層（Explanation Layer）：把資料與訊號翻成人話。網站、匯出檔、API、未來的 AI／通知都用這裡。

    Data（資料表）→ Signal Engine（signals.py）→ Explanation Layer（這裡）→ UI／API／AI

原則
  - 只描述資料發生了什麼：不預測、不評價、不給買賣建議（tests/test_explain.py 會檢查用語）
  - 句子裡的數字都來自資料庫或訊號的 evidence，不自行推估
  - 訊號的門檻文字直接由 signals.py 的常數組成，改門檻時說明會一起變，不會前後不一
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from radar import signals as sg
from radar.formatting import fmt_abs_ret, fmt_price, fmt_ret, fmt_yi, num

# ---- 訊號定義 --------------------------------------------------------------------------
CATEGORIES = {"volume": "成交量", "price": "價格", "flows": "法人動向"}


@dataclass(frozen=True)
class SignalInfo:
    type: str
    label: str
    category: str          # volume／price／flows
    tone: str              # up／down／neutral（台股慣例：up 紅、down 綠）
    what: str              # 這是什麼
    means: str             # 代表什麼資料現象
    not_mean: str          # 不代表什麼

    @property
    def category_label(self) -> str:
        return CATEGORIES[self.category]

    @property
    def rule(self) -> str:
        return rule_text(self.type)

    @property
    def cooldown(self) -> int:
        return sg.COOLDOWN[self.type]

    def as_dict(self) -> dict:
        return asdict(self) | {"category_label": self.category_label, "rule": self.rule,
                               "cooldown_days": self.cooldown}


def _yi(v: float) -> str:
    return f"{v / 1e8:g} 億元"


def rule_text(signal_type: str) -> str:
    """判斷條件的文字，直接由 signals.py 的門檻組成。"""
    turnover = f"成交金額 ≥ {_yi(sg.MIN_TURNOVER)}"
    return {
        "vol_spike": f"今天成交量 ≥ 前 20 個交易日平均的 {sg.VOL_RATIO_MIN:g} 倍，且{turnover}",
        "high_60": f"收盤價高於前 60 個交易日的最高價（已還原除權息），且{turnover}",
        "low_60": f"收盤價低於前 60 個交易日的最低價（已還原除權息），且{turnover}",
        "surge_5d": f"最近 5 個交易日累計上漲 ≥ {sg.RET_5D_ABS:.0%}（已還原除權息）",
        "plunge_5d": f"最近 5 個交易日累計下跌 ≥ {sg.RET_5D_ABS:.0%}（已還原除權息）",
        "foreign_buy_streak": f"外資連續 ≥ {sg.STREAK_DAYS} 個交易日買超，累計買超 ≥ 同期成交量的 {sg.STREAK_SHARE:.0%}，且{turnover}",
        "foreign_sell_streak": f"外資連續 ≥ {sg.STREAK_DAYS} 個交易日賣超，累計賣超 ≥ 同期成交量的 {sg.STREAK_SHARE:.0%}，且{turnover}",
        "trust_big_buy": f"投信當天買超 ≥ 成交量的 {sg.TRUST_SHARE:.0%}，買超金額 ≥ {_yi(sg.TRUST_MIN_AMOUNT)}，且{turnover}",
    }[signal_type]


SIGNAL_INFO: dict[str, SignalInfo] = {i.type: i for i in [
    SignalInfo("vol_spike", sg.TYPES["vol_spike"], "volume", "neutral",
               "今天的成交量明顯高於最近 20 個交易日的平均。",
               "這檔股票今天的交易比平常活躍很多，買進和賣出的人都變多。",
               "成交量增加不代表股價會上漲，大量賣出時成交量也會暴增。"),
    SignalInfo("high_60", sg.TYPES["high_60"], "price", "up",
               "今天的收盤價高於過去 60 個交易日（約 3 個月）的最高價。",
               "股價來到最近 3 個月都沒有到過的價位。",
               "創新高不代表之後還會繼續上漲。"),
    SignalInfo("low_60", sg.TYPES["low_60"], "price", "down",
               "今天的收盤價低於過去 60 個交易日（約 3 個月）的最低價。",
               "股價來到最近 3 個月都沒有到過的低點。",
               "創新低不代表之後還會繼續下跌，也不代表價格便宜。"),
    SignalInfo("surge_5d", sg.TYPES["surge_5d"], "price", "up",
               f"最近 5 個交易日，股價累計上漲 {sg.RET_5D_ABS:.0%} 以上。",
               "股價在很短的時間內上漲很多（已扣除除權息造成的價格變化）。",
               "短期漲幅大不代表之後還會繼續上漲。"),
    SignalInfo("plunge_5d", sg.TYPES["plunge_5d"], "price", "down",
               f"最近 5 個交易日，股價累計下跌 {sg.RET_5D_ABS:.0%} 以上。",
               "股價在很短的時間內下跌很多（已扣除除權息造成的價格變化）。",
               "短期跌幅大不代表之後還會繼續下跌，也不代表價格便宜。"),
    SignalInfo("foreign_buy_streak", sg.TYPES["foreign_buy_streak"], "flows", "up",
               f"外資（外國機構投資人）連續 {sg.STREAK_DAYS} 個以上交易日，買進的股數都多於賣出。",
               "外資在這段期間持續淨買進這檔股票，而且累計數量佔成交量的比例不小。",
               "外資買超不代表股價會上漲，外資也可能隨時改變方向。"),
    SignalInfo("foreign_sell_streak", sg.TYPES["foreign_sell_streak"], "flows", "down",
               f"外資（外國機構投資人）連續 {sg.STREAK_DAYS} 個以上交易日，賣出的股數都多於買進。",
               "外資在這段期間持續淨賣出這檔股票，而且累計數量佔成交量的比例不小。",
               "外資賣超不代表股價會下跌，外資也可能隨時改變方向。"),
    SignalInfo("trust_big_buy", sg.TYPES["trust_big_buy"], "flows", "up",
               "投信（國內的基金公司）今天買進的股數明顯多於賣出，而且佔當天成交量的比例高。",
               "國內基金今天集中淨買進這檔股票。",
               "投信買超不代表股價會上漲，也不代表之後會繼續買。"),
]}


def info(signal_type: str) -> SignalInfo:
    return SIGNAL_INFO[signal_type]


def tone_of(signal_type: str) -> str:
    return SIGNAL_INFO[signal_type].tone if signal_type in SIGNAL_INFO else "neutral"


# ---- 單一訊號的說明 -------------------------------------------------------------------------
def evidence_text(sig: dict) -> str:
    """專業版：一行依據（標準模式、表格用）。"""
    e, t, v = sig.get("evidence") or {}, sig["signal_type"], num(sig["value"])
    if t == "vol_spike":
        return f"量比 {v:.1f} 倍・成交 {fmt_yi(e.get('turnover'))}"
    if t == "high_60":
        return f"收 {fmt_price(e.get('close'))}，前 60 日最高 {fmt_price(e.get('prev_high60'))}"
    if t == "low_60":
        return f"收 {fmt_price(e.get('close'))}，前 60 日最低 {fmt_price(e.get('prev_low60'))}"
    if t in ("surge_5d", "plunge_5d"):
        return f"5 日 {fmt_ret(e.get('ret_5d'))}" + ("（期間有除權息，已還原）" if e.get("has_ex_right") else "")
    if t in ("foreign_buy_streak", "foreign_sell_streak"):
        return (f"連 {e.get('streak_days')} 日・累計 {float(e.get('cum_foreign_net', 0)) / 1000:+,.0f} 張"
                f"（佔量 {abs(float(e.get('share') or 0)):.0%}）")
    if t == "trust_big_buy":
        amount = f"約 {fmt_yi(e['amount'])}，" if e.get("amount") is not None else ""
        return f"投信買超 {float(e.get('trust_net', 0)) / 1000:,.0f} 張（{amount}佔量 {v:.0%}）"
    return ""


def signal_sentence(sig: dict) -> str:
    """新手版：一句完整的話，說明這檔股票為什麼觸發這個訊號。"""
    e, t, v = sig.get("evidence") or {}, sig["signal_type"], num(sig["value"])
    if t == "vol_spike":
        return f"今天成交量約為過去 20 個交易日平均的 {v:.1f} 倍，成交金額 {fmt_yi(e.get('turnover'))}元。"
    if t == "high_60":
        return f"今天收盤 {fmt_price(e.get('close'))} 元，高於過去 60 個交易日的最高價 {fmt_price(e.get('prev_high60'))} 元。"
    if t == "low_60":
        return f"今天收盤 {fmt_price(e.get('close'))} 元，低於過去 60 個交易日的最低價 {fmt_price(e.get('prev_low60'))} 元。"
    if t == "surge_5d":
        return f"最近 5 個交易日股價累計上漲 {fmt_abs_ret(e.get('ret_5d'))}。"
    if t == "plunge_5d":
        return f"最近 5 個交易日股價累計下跌 {fmt_abs_ret(e.get('ret_5d'))}。"
    if t in ("foreign_buy_streak", "foreign_sell_streak"):
        side = "買超" if t == "foreign_buy_streak" else "賣超"
        return (f"外資已連續 {e.get('streak_days')} 個交易日{side}，累計約 "
                f"{abs(float(e.get('cum_foreign_net', 0))) / 1000:,.0f} 張，"
                f"佔同期成交量 {abs(float(e.get('share') or 0)):.0%}。")
    if t == "trust_big_buy":
        amount = f"（約 {fmt_yi(e['amount'])}元）" if e.get("amount") is not None else ""
        return f"投信今天買超約 {float(e.get('trust_net', 0)) / 1000:,.0f} 張{amount}，佔今天成交量 {v:.0%}。"
    return ""


def signal_object(row: dict) -> dict:
    """訊號的標準物件：API、通知、個人化雷達、回測都用這個形狀，不必認得資料庫欄位。"""
    i = SIGNAL_INFO.get(row["signal_type"])
    return {
        "type": row["signal_type"], "label": i.label if i else row["signal_type"],
        "category": i.category if i else None, "category_label": i.category_label if i else None,
        "tone": tone_of(row["signal_type"]),
        "symbol": row.get("symbol"), "name": row.get("name"), "industry": row.get("industry"),
        "date": row.get("trade_date") or row.get("date"),
        "value": row.get("value"), "threshold": row.get("threshold"), "evidence": row.get("evidence"),
        "explanation": signal_sentence(row), "evidence_text": evidence_text(row),
    }


# ---- 市場狀況 ---------------------------------------------------------------------------
def market_state(index: dict | None, breadth: dict | None) -> dict:
    """依「加權指數漲跌」與「上漲／下跌家數」描述今天的市場：偏強、偏弱、分歧、持平。只是描述，不是預測。"""
    pct = num((index or {}).get("change_pct"))
    adv, dec = (breadth or {}).get("advancers"), (breadth or {}).get("decliners")
    if pct is None or adv is None or dec is None or adv + dec == 0:
        return {"level": "unknown", "label": "資料不足", "tone": "neutral", "sentence": "今天的大盤資料不完整。"}
    ratio = adv / (adv + dec)
    if abs(pct) < 0.1 and 0.45 <= ratio <= 0.55:
        return {"level": "flat", "label": "持平", "tone": "neutral",
                "sentence": f"加權指數幾乎沒變（{pct:+.2f}%），上漲 {adv} 家、下跌 {dec} 家，數量接近，市場整體持平。"}
    up, breadth_up = pct > 0, adv > dec
    if up and breadth_up:
        return {"level": "strong", "label": "偏強", "tone": "up",
                "sentence": f"上漲家數（{adv}）多於下跌家數（{dec}），加權指數也上漲 {abs(pct):.2f}%，市場整體偏強。"}
    if not up and not breadth_up:
        return {"level": "weak", "label": "偏弱", "tone": "down",
                "sentence": f"下跌家數（{dec}）多於上漲家數（{adv}），加權指數也下跌 {abs(pct):.2f}%，市場整體偏弱。"}
    return {"level": "mixed", "label": "分歧", "tone": "neutral",
            "sentence": (f"加權指數{'上漲' if up else '下跌'} {abs(pct):.2f}%，但{'下跌' if up else '上漲'}家數比較多"
                         f"（上漲 {adv}、下跌 {dec}）。加權指數依市值計算，少數大型股的漲跌影響較大，所以指數和多數個股的方向可能不同。")}


# ---- 個股：為什麼被雷達注意 ---------------------------------------------------------------
def _move_phrase(day_ret: float) -> str:
    if abs(day_ret) < 0.01:
        return f"股價變化不大（{fmt_ret(day_ret)}）"
    size = "大幅" if abs(day_ret) >= 0.05 else ""
    return f"股價{size}{'上漲' if day_ret > 0 else '下跌'} {fmt_abs_ret(day_ret)}"


def stock_story(last: dict | None, today_signals: list[dict], recent_signals: list[dict]) -> dict:
    """個股頁「為什麼被雷達注意？」：原因（每個訊號一句）＋價格事實＋一段簡單理解。只描述、不預測。

    last：最新一天的行情與特徵（stock_prices 的最後一列）；today_signals：最新一天的訊號；
    recent_signals：最近幾個交易日（不含今天）的訊號，今天沒有訊號時用來說明最近一次。
    """
    if not last or last.get("close") is None:
        return {"flagged": False, "reasons": [], "facts": [], "summary": "最近沒有成交資料。", "recent": None}

    reasons = [{"type": s["signal_type"], "label": info(s["signal_type"]).label, "tone": tone_of(s["signal_type"]),
                "text": signal_sentence(s)} for s in today_signals]

    change, close = num(last.get("change")), num(last.get("close"))
    day_ret = change / (close - change) if change is not None and close != change else None
    r5, r20 = num(last.get("ret_5d")), num(last.get("ret_20d"))
    vr = num(last.get("vol_ratio"))
    facts = []
    if day_ret is not None:
        facts.append({"kind": "today", "label": "今日價格變化", "tone": "up" if day_ret > 0 else "down" if day_ret < 0 else "flat",
                      "text": f"今天{_move_phrase(day_ret)}，收在 {fmt_price(close)} 元。"})
    if r5 is not None:
        extra = f"，過去 20 日 {fmt_ret(r20)}" if r20 is not None else ""
        facts.append({"kind": "short_term", "label": "短期表現", "tone": "up" if r5 > 0 else "down" if r5 < 0 else "flat",
                      "text": f"過去 5 個交易日累計 {fmt_ret(r5)}{extra}（已還原除權息）。"})

    # 簡單理解：把最明顯的幾件事接成一句
    parts = []
    if vr is not None and vr >= 2:
        parts.append(f"交易量明顯增加（約為平常的 {vr:.1f} 倍）")
    elif vr is not None and vr <= 0.5:
        parts.append("交易量比平常少")
    if day_ret is not None:
        parts.append(_move_phrase(day_ret))
    high60, low60 = num(last.get("high60")), num(last.get("low60"))
    if high60 is not None and close > high60:
        parts.append("收盤價高於過去 60 個交易日的最高價")
    elif low60 is not None and close < low60:
        parts.append("收盤價低於過去 60 個交易日的最低價")
    if r5 is not None and day_ret is not None and abs(r5) >= 0.01:
        opposite = (r5 > 0) != (day_ret > 0) and abs(day_ret) >= 0.01
        parts.append(f"{'但' if opposite else ''}過去 5 個交易日{'仍' if opposite else ''}累計 {fmt_ret(r5)}")
    for s in today_signals:
        if s["signal_type"] in ("foreign_buy_streak", "foreign_sell_streak"):
            e = s.get("evidence") or {}
            parts.append(f"外資已連續 {e.get('streak_days')} 個交易日{'買超' if s['signal_type'] == 'foreign_buy_streak' else '賣超'}")
        elif s["signal_type"] == "trust_big_buy":
            parts.append("投信今天集中買超")
    summary = ("今天這檔股票" + "，".join(parts) + "。") if parts else "今天這檔股票的價格與成交量變化都不大。"

    recent = None
    if not today_signals and recent_signals:
        s = recent_signals[0]
        recent = {"date": s["trade_date"], "label": info(s["signal_type"]).label, "text": signal_sentence(s)}
    return {"flagged": bool(today_signals), "reasons": reasons, "facts": facts, "summary": summary, "recent": recent}


# ---- 資料與計算方式（網站的說明頁、API、匯出檔共用）--------------------------------------------
METHODOLOGY = [
    ("資料來源", [
        "上市行情、指數、漲跌家數：臺灣證券交易所「每日收盤行情」（MI_INDEX）。",
        "上市三大法人買賣超：臺灣證券交易所「三大法人買賣超日報」（T86）。",
        "上市除權息：臺灣證券交易所「除權除息計算結果表」（TWT49U）。",
        "產業別與休市日曆：臺灣證券交易所 OpenAPI。",
        "上櫃行情、櫃買指數、三大法人、除權息、公司基本資料：證券櫃檯買賣中心 OpenAPI。櫃買中心只提供最新一天，所以上櫃資料從 2026/10/02 起逐日累積；需要較長歷史的訊號（例如 60 日新高）要等資料累積夠了才會出現在上櫃股票。",
        "資料的權利屬於原發布機關；本站只做整理與統計，可能有延遲或錯誤，一切以官方公布為準。盤後頁面不含盤中即時資料。",
    ]),
    ("更新時間", [
        "每個交易日 18:30 起自動抓當天盤後資料；資料還沒公布時每 20 分鐘重試，最晚到 21:30。",
        "電腦關機錯過的日子，下次開機後會自動補上。",
    ]),
    ("還原價", [
        "除權息、減資、分割會讓股價「跳空」，但那不是市場漲跌。計算報酬、高低點時，會把過去的價格依比例調整，讓前後可以比較。",
        "一般除權息用官方公布的「除權息參考價 ÷ 前一天收盤價」調整；含現金增資時，依當天實際成交判斷市場用的是哪個參考價。",
        "K 線圖畫的是原始價格，所以除權息日會看到跳空。",
    ]),
    ("報酬率", [
        "日報酬＝今天還原收盤價 ÷ 前一個交易日還原收盤價 − 1。5 日、20 日報酬同理，比較的是 5、20 個交易日前。",
        "首頁與列表上的「漲跌幅」是證交所公布的官方漲跌幅。",
    ]),
    ("量比", [
        "量比＝今天成交量 ÷ 前 20 個交易日的平均成交量（不含今天）。量比 4 表示今天的成交量是平常的 4 倍。",
    ]),
    ("冷卻機制", [
        "同一檔股票的同一種訊號，如果在冷卻期內已經出現過，就不會再重複出現。",
        "這樣條件持續成立時（例如連續幾天創新高），只會在剛成立的那天被標記一次。",
    ]),
    ("訊號回測", [
        "用過去的資料回頭看：每一筆訊號出現之後 5、20、60 個交易日，股價實際漲跌多少（已還原除權息）。",
        "比較對象是同一天全部上市普通股：上漲比例、中位數都和它比，而不是和加權指數比。",
        "另外算「隔天收盤才買到」的 20 日報酬，因為盤後才看得到訊號；也算扣掉手續費與證交稅之後的上漲比例。",
        "統計只描述過去一段期間，不代表之後會一樣。",
    ]),
    ("這個網站不做什麼", [
        "不預測股價，也不提供買進、賣出或持有的建議。",
        "只把公開市場資訊整理成統計與說明，不是證券投資顧問事業，不對個別股票提供推介。",
        "訊號只是「資料出現異常」的提示，不代表好或壞。",
    ]),
]


# ---- 訊號回測（Phase 3）-------------------------------------------------------------------
# 用過去的資料回頭看：每種訊號出現之後，股價實際怎麼走，並和同一天「全部普通股」比較。
# 句子只描述過去的統計；判斷「比全部股票好／差」的門檻寫在這裡，數字都來自 signal_outcomes。

HORIZON_LABELS = {"h5": "5 個交易日後", "h20": "20 個交易日後", "h60": "60 個交易日後", "d20": "隔天才買，20 個交易日後"}
MIN_SAMPLES = 30          # 筆數少於這個數，不下「好／差」的判斷
BETTER_WIN, BETTER_MED = 0.05, 0.01   # 上漲比例多 5 個百分點、而且中位數多 1 個百分點，才算「比全部股票好一些」

# 市面上常聽到的說法（引號裡是一般的說法，不是這個網站的判斷），逐一拿回測數字對照
MYTHS = [
    ("market", "大盤一直漲，買什麼都會漲。"),
    ("foreign_buy_streak", "外資連續買超，股價就會跟著漲。"),
    ("trust_big_buy", "投信大買的股票，接下來會漲。"),
    ("high_60", "創新高代表強勢，強者恆強。"),
    ("surge_5d", "短線急漲的強勢股，會繼續漲。"),
    ("vol_spike", "爆量代表主力進場，接下來會漲。"),
    ("plunge_5d", "跌深了就會反彈。"),
]


def _p(v: float | None) -> str:
    """報酬：+1.2%"""
    return "–" if v is None else f"{v * 100:+.1f}%"


def _share(v: float | None) -> str:
    """比例：56%"""
    return "–" if v is None else f"{v * 100:.0f}%"


def _stat(row: dict) -> dict:
    s = {k: (None if row.get(k) is None else float(row[k]))
         for k in ("win", "win_cost", "mean", "median", "p25", "p75", "excess_mean", "base_win", "base_median")}
    s["n"] = int(row["n"])
    s["diff_win"] = None if s["win"] is None or s["base_win"] is None else s["win"] - s["base_win"]
    s["diff_med"] = None if s["median"] is None or s["base_median"] is None else s["median"] - s["base_median"]
    return s


def backtest_verdict(s: dict | None) -> dict:
    """和同一天全部股票比：好一些／差不多／差一些。只比較過去，不代表之後。"""
    if not s or s["n"] < MIN_SAMPLES or s["diff_win"] is None or s["diff_med"] is None:
        return {"key": "few", "label": "筆數太少，看不出來", "tone": "flat"}
    if s["diff_win"] >= BETTER_WIN and s["diff_med"] >= BETTER_MED:
        return {"key": "better", "label": "之後比全部股票好一些", "tone": "up"}
    if s["diff_win"] <= -BETTER_WIN and s["diff_med"] <= -BETTER_MED:
        return {"key": "worse", "label": "之後比全部股票差一些", "tone": "down"}
    return {"key": "same", "label": "之後和全部股票差不多", "tone": "flat"}


def _myth(key: str, claim: str, by: dict, base: dict, period: dict, cost: float) -> dict | None:
    if key == "market":
        b20 = base.get("h20")
        if not b20 or period.get("index_change") is None:
            return None
        verdict = "資料不支持這個說法" if b20["up_share"] < 0.55 else "這段期間大致成立"
        return {"type": None, "label": "大盤與一般股票", "claim": claim, "verdict": verdict,
                "finding": f"這段期間加權指數從 {period['index_from']:,.0f} 點到 {period['index_to']:,.0f} 點（{_p(period['index_change'])}）。"
                           f"但任選一天買進全部上市股票，20 個交易日後平均只有 {_share(b20['up_share'])} 的股票上漲，"
                           f"中位數 {_p(b20['median'])}。",
                "points": ["加權指數依市值計算，台積電等少數大型股的影響很大；指數大漲，不代表大部分股票都在漲。",
                           "所以下面每種訊號都和「同一天全部股票」比，而不是和加權指數比。"]}
    s, d = by.get(key, {}).get("h20"), by.get(key, {}).get("d20")
    if not s or s["n"] < MIN_SAMPLES:
        return None
    if s["diff_win"] >= 0.08 and s["diff_med"] >= 0.02:
        verdict = "資料有支持，但沒有說的那麼穩"
    elif s["diff_win"] >= 0.03 or s["diff_med"] >= 0.005:
        verdict = "有一點，但差距不大"
    else:
        verdict = "資料不支持這個說法"
    points = [f"同樣的訊號，結果差很多：中間一半的情況落在 {_p(s['p25'])} 到 {_p(s['p75'])} 之間。"]
    if s["mean"] - s["median"] >= 0.01:
        points.append(f"平均 {_p(s['mean'])} 是被少數大漲的股票拉高的，一般情況（中位數）是 {_p(s['median'])}。")
    if key == "foreign_buy_streak":
        sell = by.get("foreign_sell_streak", {}).get("h20")
        if sell and sell["n"] >= MIN_SAMPLES and sell["median"] >= s["median"]:
            points.append(f"同一段期間，外資「連賣」的股票之後 20 個交易日的中位數是 {_p(sell['median'])}，"
                          f"上漲比例 {_share(sell['win'])}，並沒有比連買的差。")
    if d and d["n"] >= MIN_SAMPLES and s["median"] - d["median"] >= 0.003:
        points.append(f"盤後才看得到訊號，隔天收盤才買到的話，中位數從 {_p(s['median'])} 變成 {_p(d['median'])}。")
    if s["win"] - s["win_cost"] >= 0.015:
        points.append(f"扣掉手續費和證交稅（一買一賣約 {cost * 100:.2f}%），上漲比例從 {_share(s['win'])} 降到 {_share(s['win_cost'])}。")
    return {"type": key, "label": SIGNAL_INFO[key].label, "claim": claim, "verdict": verdict,
            "finding": f"出現後 20 個交易日，{_share(s['win'])} 的股票上漲（同一天全部股票 {_share(s['base_win'])}）；"
                       f"中位數 {_p(s['median'])}（全部股票 {_p(s['base_median'])}）。{s['n']:,} 筆。",
            "points": points}


def backtest_caveats(period: dict, cost: float) -> list[str]:
    start, end = period.get("start"), period.get("end")
    span = f"{start:%Y/%m/%d}～{end:%Y/%m/%d}" if start and end else "目前的資料期間"
    return [
        f"資料只有 {span} 出現的訊號（約一年），而且大多是同一種盤勢；換一段時間，結果可能完全不同。",
        "連續幾天出現的訊號，後面的報酬期間會重疊，所以筆數看起來多，實際上獨立的樣本比較少。",
        "報酬從訊號當天收盤價算起；實際上盤後才看到訊號，最快隔天才能買（表中「隔天才買」）。漲停、跌停時可能根本買不到或賣不掉。",
        f"交易成本用手續費 0.1425%（買、賣各一次，未打折）加證券交易稅 0.3% 估算，合計約 {cost * 100:.2f}%；沒有算滑價。",
        "比較基準是同一天全部上市普通股的等權平均與中位數，不是加權指數。",
        "報酬已還原除權息、減資與分割；只算上市普通股，期間內下市的股票，下市後就沒有資料。",
        "這些數字描述的是過去，不代表之後，也不能當成買賣的依據。",
    ]


def backtest_view(rows: list[dict], period: dict, base: dict, cost: float) -> dict:
    """回測頁與 API 用的結構：每種訊號 × 各持有天數的統計、給新手的一句話、市面說法對照、限制說明。"""
    by: dict[str, dict[str, dict]] = {}
    for r in rows:
        by.setdefault(r["signal_type"], {})[r["horizon"]] = _stat(r)
    order = {"better": 0, "same": 1, "worse": 2, "few": 3}
    types = []
    for t, inf in SIGNAL_INFO.items():
        h = by.get(t, {})
        s20 = h.get("h20")
        v = backtest_verdict(s20)
        sentence = (f"過去出現「{inf.label}」之後 20 個交易日，有 {_share(s20['win'])} 的股票上漲；"
                    f"同一天全部股票是 {_share(s20['base_win'])}。" if s20 else "還沒有滿 20 個交易日的訊號。")
        types.append({"info": inf, "h": h, "verdict": v, "sentence": sentence})
    types.sort(key=lambda x: (order[x["verdict"]["key"]], -((x["h"].get("h20") or {}).get("diff_med") or 0)))
    myths = [m for m in (_myth(k, c, by, base, period, cost) for k, c in MYTHS) if m]
    return {"period": period, "base": base, "types": types, "myths": myths, "cost": cost,
            "caveats": backtest_caveats(period, cost), "horizons": HORIZON_LABELS,
            "verdicts": {x["info"].type: x["verdict"] for x in types}}
