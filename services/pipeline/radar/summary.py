"""AI 盤後摘要（Step 8）：把當天資料庫裡的數字整理成 facts，請 Claude 寫 3～5 句繁體中文描述。

原則
  - 模型只看得到 facts；寫入前檢查文字裡的每個數字都出自 facts、沒有買賣建議用語，不符就請它改寫一次，
    仍不符就不存（網站上不會出現沒有根據的數字）
  - 只描述發生了什麼：不預測、不給建議
  - 沒有 API 金鑰時改用「模擬摘要」：依固定規則從 facts 組句，一樣通過檢查，網站上標示為模擬
  - 一天呼叫一次、提示很短：低於可快取的最小長度，也不會在快取有效期內重複，所以不使用 prompt caching
  - 模型遇到安全分類器拒答時，由伺服器端 fallbacks 自動改用 Anthropic 建議的模型

費用（估計，尚未實測）：facts 約 2,700 字，輸入約 3～4 千 token；輸出數百 token 加上思考，一天約 0.03～0.08 美元。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from pathlib import Path

from psycopg.types.json import Jsonb

from radar.signals import TYPES
from radar.web import queries as q

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEEKDAYS = "一二三四五六日"

SYSTEM = """你是台股盤後資料的撰稿人。使用者會給你一份 JSON，是某個交易日臺灣證券交易所上市市場的統計數字。

請用繁體中文（台灣用語）寫 3 到 5 句話的盤後摘要：
- 只描述 JSON 裡的事實。每個數字都必須直接出自 JSON，不要自行計算新的數字（例如不要自己算比例、差額或倍數）。
- 不要預測走勢，不要給任何買進、賣出、持有的建議，也不要評論個股值不值得投資。
- 提到股票時用「名稱（代號）」。單位依欄位名稱（億元、張、%、倍）。
- 先講大盤與漲跌家數，再挑一兩類最明顯的訊號或類股，最後可以提成交金額與近 20 日平均的差別（兩個數字都寫出來）。
- 只輸出摘要本文，不要標題、條列或 Markdown。"""

# 出現就不存：買賣建議與預測用語
FORBIDDEN = ("建議", "買進", "賣出", "加碼", "減碼", "目標價", "看好", "看壞", "看多", "看空", "值得", "逢低", "布局",
             "預期", "可望", "有望", "將會")


def has_credentials() -> bool:
    """SDK 會依序找 API 金鑰、Auth token、`ant auth login` 的設定檔；三者都沒有就略過。"""
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("ANTHROPIC_PROFILE") or (Path.home() / ".config" / "anthropic").exists())


# ---- facts ----------------------------------------------------------------------------
def _r(v, nd=2):
    return None if v is None else round(float(v), nd)


def _yi(v):
    return None if v is None else round(float(v) / 1e8, 1)


def _stock(r) -> str:
    return f"{r['name']}（{r['symbol']}）"


def _signal_fact(r) -> dict:
    e, t = r["evidence"] or {}, r["signal_type"]
    base = {"股票": _stock(r), "產業": r["industry"] or "未分類"}
    if t == "vol_spike":
        return base | {"量比_倍": _r(r["value"], 1), "成交金額_億元": _yi(e.get("turnover"))}
    if t == "high_60":
        return base | {"收盤": _r(e.get("close")), "前60日最高": _r(e.get("prev_high60"))}
    if t == "low_60":
        return base | {"收盤": _r(e.get("close")), "前60日最低": _r(e.get("prev_low60"))}
    if t in ("surge_5d", "plunge_5d"):
        return base | {"5日漲跌幅_%": _r(float(e.get("ret_5d", 0)) * 100, 1)}
    if t in ("foreign_buy_streak", "foreign_sell_streak"):
        return base | {"連續天數": e.get("streak_days"), "累計買賣超_張": round(float(e.get("cum_foreign_net", 0)) / 1000)}
    if t == "trust_big_buy":
        return base | {"投信買超_張": round(float(e.get("trust_net", 0)) / 1000), "佔成交量_%": _r(float(r["value"]) * 100, 0)}
    return base


def build_facts(conn, d: date) -> dict:
    ov = q.market_overview(conn, d)
    rows = q.signals_on(conn, d)
    i, b = ov["index"] or {}, ov["breadth"] or {}
    avg20 = conn.execute("""SELECT avg(total_turnover) FROM (SELECT total_turnover FROM market_breadth
                            WHERE trade_date < %s ORDER BY trade_date DESC LIMIT 20) x""", (d,)).fetchone()[0]
    sectors = ov["sectors"]
    name = lambda s: s["name"].replace("類指數", "")
    groups = [g for g in q.group_signals(rows) if g["rows"]]
    return {
        "日期": f"{d:%Y-%m-%d}（星期{WEEKDAYS[d.weekday()]}）",
        "加權指數": {"收盤": _r(i.get("close")), "漲跌點數": _r(i.get("change")), "漲跌幅_%": _r(i.get("change_pct"))},
        "漲跌家數_股票": {"上漲": b.get("advancers"), "下跌": b.get("decliners"), "平盤": b.get("unchanged"),
                     "漲停": b.get("limit_up"), "跌停": b.get("limit_down")},
        "成交金額_億元": _yi(b.get("total_turnover")),
        "近20日平均成交金額_億元": _yi(avg20),
        "類股漲幅前五": [{"類股": name(s), "漲跌幅_%": _r(s["change_pct"])} for s in sectors[:5]],
        "類股跌幅前五": [{"類股": name(s), "漲跌幅_%": _r(s["change_pct"])} for s in sectors[::-1][:5]],
        "訊號總數": len(rows),
        "各類訊號數": {g["label"]: len(g["rows"]) for g in groups},
        "訊號重點_每類前3": {g["label"]: [_signal_fact(r) for r in g["rows"][:3]] for g in groups},
        "訊號最多的產業": [{"產業": x["industry"], "訊號數": x["total"]} for x in q.industry_counts(rows)[:3]],
    }


# ---- 檢查 ------------------------------------------------------------------------------
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _allowed_numbers(facts) -> set[str]:
    out: set[str] = set()

    def add(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            f = abs(float(v))
            for s in (f"{f:.0f}", f"{f:.1f}", f"{f:.2f}", f"{f:g}"):
                out.add(s)
                out.add(s.rstrip("0").rstrip(".") if "." in s else s)
        elif isinstance(v, str):                        # 日期、代號、欄位名稱裡的數字（例：09 → 也接受 9）
            for m in _NUM.findall(v):
                n = m.replace(",", "")
                out.add(n)
                if "." not in n:
                    out.add(str(int(n)))
        elif isinstance(v, dict):
            for k, x in v.items():
                add(k)
                add(x)
        elif isinstance(v, list):
            for x in v:
                add(x)

    add(facts)
    return out


def check_summary(text: str, facts: dict) -> list[str]:
    """回傳問題清單；空的代表可以存。"""
    problems = [f"用了不該出現的詞「{w}」" for w in FORBIDDEN if w in text]
    allowed = _allowed_numbers(facts)
    for m in _NUM.findall(text):
        n = m.replace(",", "")
        if n in allowed or (n.isdigit() and int(n) <= 10):   # 10 以下的小整數（「3 檔」「5 日」）不檢查
            continue
        problems.append(f"數字 {m} 不在資料裡")
    if not 2 <= len(re.findall(r"[。！？]", text)) <= 6:
        problems.append("句數不是 3～5 句")
    return problems


# ---- 產生與儲存 ---------------------------------------------------------------------------
def _ask(client, messages: list[dict]):
    return client.beta.messages.create(
        model=MODEL,
        max_tokens=8000,                       # 包含思考；摘要本身只有幾百字
        betas=[FALLBACK_BETA],
        fallbacks="default",                   # 拒答時伺服器端改用建議的模型
        output_config={"effort": "medium"},    # 簡單的事實整理，不需要 high
        system=SYSTEM,
        messages=messages,
    )


def _text(resp) -> str:
    return "".join(b.text for b in resp.content if b.type == "text").strip()


SIMULATED = "simulated"


def _store(conn, d: date, text: str, facts: dict, provider: str, model: str) -> None:
    conn.execute("""INSERT INTO market_summaries (trade_date, content, facts, provider, model)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (trade_date) DO UPDATE SET content = EXCLUDED.content, facts = EXCLUDED.facts,
                        provider = EXCLUDED.provider, model = EXCLUDED.model, created_at = now()""",
                 (d, text, Jsonb(facts), provider, model))


def _existing(conn, d: date) -> tuple[str, str] | None:
    return conn.execute("SELECT content, provider FROM market_summaries WHERE trade_date = %s", (d,)).fetchone()


def generate_summary(conn, d: date, client=None, *, force: bool = False) -> str | None:
    """用 Claude 產生並存入 market_summaries。已經有 AI 摘要就直接回傳（force 重寫）；模擬摘要會被取代。
    沒有通過檢查就回傳 None，原本的摘要（例如模擬摘要）保留不動。"""
    existing = _existing(conn, d)
    if existing and existing[1] != SIMULATED and not force:
        return existing[0]
    facts = build_facts(conn, d)
    if facts["加權指數"]["收盤"] is None:
        log.warning("%s 沒有加權指數資料，不產生摘要", d)
        return None
    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    messages = [{"role": "user", "content": json.dumps(facts, ensure_ascii=False, indent=1)}]
    for attempt in (1, 2):
        resp = _ask(client, messages)
        if resp.stop_reason == "refusal":
            log.warning("%s AI 摘要被拒絕（%s），不存", d, getattr(resp.stop_details, "category", None))
            return None
        text = _text(resp)
        problems = check_summary(text, facts)
        if not problems:
            _store(conn, d, text, facts, "anthropic", resp.model)
            log.info("%s AI 摘要已存（%s，第 %d 次）", d, resp.model, attempt)
            return text
        log.warning("%s AI 摘要第 %d 次沒通過檢查：%s", d, attempt, problems)
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": "這一版有問題：" + "；".join(problems) + "。請依規則重寫整段摘要。"}]
    return None


# ---- 模擬摘要：沒有 API 金鑰時使用 ------------------------------------------------------
def _n(v) -> str:
    """數字照 facts 的位數輸出，加千分位（檢查時會去掉千分位比對）。"""
    f = float(v)
    return f"{f:,.0f}" if f == int(f) else f"{f:,.2f}".rstrip("0").rstrip(".")


def simulate_summary(facts: dict) -> str:
    """依固定規則把 facts 組成 3～5 句話；只用 facts 裡的數字，所以一定通過 check_summary。"""
    i, b = facts["加權指數"], facts["漲跌家數_股票"]
    y, m, d = facts["日期"].split("（")[0].split("-")
    pts, pct = i["漲跌點數"], i["漲跌幅_%"]
    head = f"{int(y)} 年 {int(m)} 月 {int(d)} 日加權指數收在 {_n(i['收盤'])} 點"
    if pts is None:
        s = [head + "。"]
    elif pts == 0:
        s = [head + "，與前一日持平。"]
    else:
        s = [head + f"，{'上漲' if pts > 0 else '下跌'} {_n(abs(pts))} 點" + (f"（{_n(abs(pct))}%）" if pct is not None else "") + "。"]
    if b.get("上漲") is not None and b.get("下跌") is not None:
        s.append(f"上漲 {_n(b['上漲'])} 家、下跌 {_n(b['下跌'])} 家"
                 + (f"，漲停 {_n(b['漲停'])} 家、跌停 {_n(b['跌停'])} 家。"
                    if b.get("漲停") is not None and b.get("跌停") is not None else "。"))
    top, bottom = facts["類股漲幅前五"], facts["類股跌幅前五"]
    if top and bottom:
        sign = lambda v: ("+" if v > 0 else "−" if v < 0 else "") + _n(abs(v))
        s.append(f"類股中以{top[0]['類股']}（{sign(top[0]['漲跌幅_%'])}%）最強，"
                 f"{bottom[0]['類股']}（{sign(bottom[0]['漲跌幅_%'])}%）最弱。")
    counts = facts["各類訊號數"]
    if counts:
        label = max(counts, key=counts.get)
        names = "、".join(x["股票"] for x in facts["訊號重點_每類前3"].get(label, [])[:2])
        s.append(f"當天共有 {_n(facts['訊號總數'])} 則訊號，以「{label}」{_n(counts[label])} 則最多"
                 + (f"，包括{names}。" if names else "。"))
    if facts.get("成交金額_億元") is not None and facts.get("近20日平均成交金額_億元") is not None:
        s.append(f"成交金額 {_n(facts['成交金額_億元'])} 億元，近 20 日平均為 {_n(facts['近20日平均成交金額_億元'])} 億元。")
    return "".join(s)


def simulate(conn, d: date, *, force: bool = False) -> str | None:
    """產生並存入模擬摘要；已經有摘要（不論 AI 或模擬）就不動，除非 force 且原本就是模擬的。"""
    existing = _existing(conn, d)
    if existing and not (force and existing[1] == SIMULATED):
        return existing[0]
    facts = build_facts(conn, d)
    if facts["加權指數"]["收盤"] is None:
        return None
    text = simulate_summary(facts)
    problems = check_summary(text, facts)
    if problems:                                   # 規則寫錯才會發生；寧可不存
        log.error("%s 模擬摘要沒通過檢查：%s", d, problems)
        return None
    _store(conn, d, text, facts, SIMULATED, "rule-based")
    return text
