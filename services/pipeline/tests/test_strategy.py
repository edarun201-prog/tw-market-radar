"""自訂條件回測：組合的規則、後續報酬的定義（和訊號回測一致）、統計與比較基準、網頁／API／匯出檔。"""
import json
import math

import numpy as np
import pytest

from radar import strategy
from radar.outcomes import build_outcomes

from .test_web import client, market  # noqa: F401  （共用網站測試的人工市場）


def test_combos_pick_at_most_one_option_per_dimension():
    dim_of = {o[0]: k for k, _, opts in strategy.DIMENSIONS for o in opts}
    all_combos = strategy.combos()
    assert len(all_combos) == len(set(all_combos))
    assert all(1 <= len(c) <= strategy.MAX_PICKS and len({dim_of[o] for o in c}) == len(c) for c in all_combos)
    order = [o for o in strategy.OPTIONS]
    assert all(list(c) == sorted(c, key=order.index) for c in all_combos)        # 依面向順序，網頁用同樣的順序組代號


def test_forward_returns_and_foreign_streak():
    r = np.array([0.0, 0.1, -0.1, 0.05, 0.02])
    h2 = strategy._forward(r, 1, 2)                       # 從第 i 天收盤起，之後 2 天
    assert h2[0] == pytest.approx(1.1 * 0.9 - 1) and h2[2] == pytest.approx(1.05 * 1.02 - 1)
    assert math.isnan(h2[3]) and math.isnan(h2[4])        # 不滿 2 天
    d1 = strategy._forward(r, 2, 2)                       # 隔天才買：只算第 i+2 天
    assert d1[0] == pytest.approx(-0.1)
    assert list(strategy._streak(np.array([5, 3, -1, -2, -4, np.nan, 2.0]))) == [1, 2, -1, -2, -3, 0, 1]


def test_lab_matches_signal_backtest_definitions(conn, client, market, tmp_path):  # noqa: F811
    build_outcomes(conn)
    d = market[-1]
    lab = strategy.build_lab(conn, d)
    total = conn.execute("SELECT count(*) FROM daily_features").fetchone()[0]
    assert lab["all"][0] == total and lab["all"][1] == 2                      # 每一列＝一檔股票的一天
    # 後續報酬和訊號回測同一套定義：全部股票的筆數與上漲比例，要和 market_forward_returns 加總起來一樣
    for i, h in enumerate(("h5", "h20")):
        n, up = conn.execute("SELECT sum(n), sum(up_share * n) / sum(n) FROM market_forward_returns WHERE horizon = %s",
                             (h,)).fetchone()
        assert lab["all"][4 + i][0] == n and lab["all"][4 + i][1] == pytest.approx(float(up), abs=1e-4)
    # 2330 在第 40 天爆量 6 倍：「量比 ≥ 4」只有那一天；之後不滿 5 天，所以和訊號回測一樣沒有報酬
    vr4 = lab["combos"]["vr4"]
    assert vr4[:3] == [1, 1, 1] and vr4[4] is None and vr4[3] == "f"
    assert all(r[3] in "bswf" for r in lab["combos"].values())
    assert {row[0] for row in lab["today"]} == {"2330", "2317"} and all(isinstance(row[5], str) for row in lab["today"])
    assert lab["period"]["start"] == market[0]

    html = client.get("/backtest").text
    assert "自訂條件回測" in html and 'id="lab-data"' in html and 'id="lab-vr2"' in html and "常見組合" in html
    assert "不是推薦" in html
    api = client.get("/api/strategy").json()
    assert api["max_picks"] == strategy.MAX_PICKS and "vr4" in api["combos"] and api["dims"][0]["key"] == "vol"

    from radar.web.export import render_day
    exported = render_day(conn, d, tmp_path / "lab")
    assert 'id="lab-data"' in exported and "自訂條件回測" in exported
    data = exported.split('<script type="application/json" id="lab-data">')[1].split("</script>")[0]
    assert json.loads(data)["combos"]["vr4"][0] == 1
    assert list((tmp_path / "lab").glob("lab_*.json"))                       # 算一次存檔，網站與公開 JSON 共用
