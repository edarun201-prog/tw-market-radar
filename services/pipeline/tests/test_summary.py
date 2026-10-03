"""AI 盤後摘要：用假的 client，不呼叫 API。"""
from types import SimpleNamespace

from radar import summary
from radar.summary import check_summary

from .test_web import client, market  # noqa: F401  （共用人工行情與測試用網站）

GOOD = "加權指數收在20,440點，上漲10點（0.05%）。上漲600家、下跌300家。成交金額5,000億元。"


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        stop, text = self.replies.pop(0)
        return SimpleNamespace(stop_reason=stop, stop_details=SimpleNamespace(category="test"), model=kw["model"],
                               content=[SimpleNamespace(type="text", text=text)])


def test_checker_rejects_advice_and_made_up_numbers():
    facts = {"加權指數": {"收盤": 20440.0, "漲跌幅_%": 0.05}}
    assert check_summary("加權指數收在20,440點，上漲0.05%。今天成交清淡。量能普通。", facts) == []
    problems = check_summary("加權指數收在20,440點，比昨天多0.8%，建議逢低布局。明天可望續漲。", facts)
    assert any("0.8" in p for p in problems)
    assert any("建議" in p for p in problems) and any("可望" in p for p in problems)


def test_generate_retries_then_stores(conn, market):  # noqa: F811
    d = market[-1]
    client = FakeClient([("end_turn", "加權指數收在20,440點，較前一日多出0.8%。上漲600家。成交5,000億元。"),
                         ("end_turn", GOOD)])
    assert summary.generate_summary(conn, d, client) == GOOD
    assert len(client.calls) == 2
    assert "0.8" in client.calls[1]["messages"][-1]["content"]            # 改寫要求有指出哪個數字不對
    first = client.calls[0]
    assert first["model"] == "claude-opus-5" and first["fallbacks"] == "default"
    assert first["betas"] == [summary.FALLBACK_BETA]
    row = conn.execute("SELECT content, model, facts->'加權指數'->>'收盤' FROM market_summaries WHERE trade_date=%s",
                       (d,)).fetchone()
    assert row == (GOOD, "claude-opus-5", "20440.0")


def test_refusal_and_existing_are_not_rewritten(conn, market):  # noqa: F811
    d = market[-1]
    assert summary.generate_summary(conn, d, FakeClient([("refusal", "")])) is None
    assert conn.execute("SELECT count(*) FROM market_summaries").fetchone()[0] == 0
    summary.generate_summary(conn, d, FakeClient([("end_turn", GOOD)]))
    again = FakeClient([])
    assert summary.generate_summary(conn, d, again) == GOOD and again.calls == []   # 已有摘要，不再呼叫


def test_summary_shows_on_home_page(conn, market, client):  # noqa: F811
    summary.generate_summary(conn, market[-1], FakeClient([("end_turn", GOOD)]))
    r = client.get("/market")
    assert "AI 盤後摘要" in r.text and "20,440" in r.text


# ---- 模擬摘要 ------------------------------------------------------------------------
def test_simulated_summary_passes_checks_and_is_labelled(conn, market, client):  # noqa: F811
    d = market[-1]
    text = summary.simulate(conn, d)
    assert text and check_summary(text, summary.build_facts(conn, d)) == []
    assert "20,440" in text and "上漲 600 家" in text
    assert conn.execute("SELECT provider FROM market_summaries").fetchone()[0] == "simulated"
    assert "盤後摘要（模擬）" in client.get("/market").text


def test_ai_summary_replaces_simulated_but_not_the_reverse(conn, market):  # noqa: F811
    d = market[-1]
    summary.simulate(conn, d)
    assert summary.generate_summary(conn, d, FakeClient([("end_turn", GOOD)])) == GOOD
    assert summary.simulate(conn, d, force=True) == GOOD          # 已有 AI 摘要，模擬不會蓋掉
    assert conn.execute("SELECT provider FROM market_summaries").fetchone()[0] == "anthropic"


def test_simulated_summary_handles_missing_numbers():
    facts = {"日期": "2026-02-26（星期四）", "加權指數": {"收盤": 35414.49, "漲跌點數": 1.42, "漲跌幅_%": None},
             "漲跌家數_股票": {"上漲": None, "下跌": None}, "類股漲幅前五": [], "類股跌幅前五": [],
             "訊號總數": 0, "各類訊號數": {}, "訊號重點_每類前3": {}, "成交金額_億元": None, "近20日平均成交金額_億元": None}
    assert summary.simulate_summary(facts) == "2026 年 2 月 26 日加權指數收在 35,414.49 點，上漲 1.42 點。"
