"""估值與財報：上市／上櫃兩種格式的解析、入庫、個股頁與 API、公開 JSON；技術指標出現在個股頁。"""
import json
from datetime import date, datetime
from decimal import Decimal

from radar.adapters.base import RawPayload
from radar.config import TAIPEI
from radar.jobs.fundamentals import sync_financial_reports, sync_valuations
from radar.normalizers.fundamentals import normalize_financial_reports, normalize_valuations

from .test_web import client, market  # noqa: F401  （共用網站測試的人工市場）


def _payload(source, dataset, body):
    return RawPayload(source=source, dataset=dataset, trade_date=date(2026, 10, 4), url="fixture://", body=body,
                      fetched_at=datetime.now(TAIPEI), has_data=True, meta={})


def _twse_valuations(d_roc="1151002", extra=0):
    rows = [{"Date": d_roc, "Code": "2330", "Name": "台積電", "PEratio": "28.98", "DividendYield": "0.88", "PBratio": "10.08"},
            {"Date": d_roc, "Code": "2317", "Name": "鴻海", "PEratio": "", "DividendYield": "2.86", "PBratio": "1.85"}]
    return rows + [{"Date": d_roc, "Code": f"9{i:03d}", "Name": "x", "PEratio": "10", "DividendYield": "1", "PBratio": "1"}
                   for i in range(extra)]


def _twse_reports(extra=0):
    ci = [{"出表日期": "1151004", "年度": "115", "季別": "2", "公司代號": "2330", "公司名稱": "台積電",
           "營業收入": "2404483690.00", "淨利（淨損）歸屬於母公司業主": "1279041690.00", "基本每股盈餘（元）": "49.33"}]
    ci += [{"出表日期": "1151004", "年度": "115", "季別": "2", "公司代號": f"9{i:03d}", "公司名稱": "x",
            "營業收入": "1", "淨利（淨損）歸屬於母公司業主": "1", "基本每股盈餘（元）": "0.1"} for i in range(extra)]
    fh = [{"出表日期": "1151004", "年度": "115", "季別": "2", "公司代號": "2317", "公司名稱": "鴻海",
           "淨收益": "4085767.00", "淨利（損）歸屬於母公司業主": "76270308.00", "基本每股盈餘（元）": "4.95"}]
    return {"ci": ci, "basi": [], "bd": [], "fh": fh, "ins": [], "mim": []}


def test_normalize_both_markets():
    day, w = normalize_valuations(_payload("TWSE", "BWIBBU", {"data": _twse_valuations()}))
    assert day.trade_date == date(2026, 10, 2) and not w
    by = {v.symbol: v for v in day.rows}
    assert by["2330"].pe_ratio == Decimal("28.98") and by["2317"].pe_ratio is None          # 空白＝官方未提供
    otc, _ = normalize_valuations(_payload("TPEX", "BWIBBU", {"data": [
        {"Date": "1151002", "SecuritiesCompanyCode": "6488", "CompanyName": "環球晶", "PriceEarningRatio": "57.77",
         "DividendPerShare": "8.0", "YieldRatio": "0.65", "PriceBookRatio": "5.88"},
        {"Date": "1151002", "SecuritiesCompanyCode": "1240", "CompanyName": "茂生農經", "PriceEarningRatio": "0.00",
         "YieldRatio": "0.93", "PriceBookRatio": "1.59"}]}))
    assert [(v.symbol, v.pe_ratio, v.dividend_yield) for v in otc.rows] == [("6488", Decimal("57.77"), Decimal("0.65")),
                                                                           ("1240", None, Decimal("0.93"))]   # 0 ＝ 無法計算

    reports, _ = normalize_financial_reports(_payload("TWSE", "FIN_REPORT", _twse_reports()))
    by = {r.symbol: r for r in reports}
    tsmc = by["2330"]
    assert (tsmc.fiscal_year, tsmc.quarter, tsmc.eps) == (2026, 2, Decimal("49.33"))
    assert tsmc.revenue == Decimal("2404483690000") and tsmc.net_income == Decimal("1279041690000")   # 千元 → 元
    assert by["2317"].revenue is None and by["2317"].eps == Decimal("4.95")                  # 金控的「淨收益」不當營收
    otc_r, _ = normalize_financial_reports(_payload("TPEX", "FIN_REPORT", {"ci": [
        {"Date": "1151004", "Year": "115", "Season": "2", "SecuritiesCompanyCode": "6488", "CompanyName": "環球晶",
         "營業收入": "29199108.00", "淨利（淨損）歸屬於母公司業主": "5674905.00", "基本每股盈餘（元）": "11.87"}]}))
    assert (otc_r[0].symbol, otc_r[0].fiscal_year, otc_r[0].eps) == ("6488", 2026, Decimal("11.87"))


class FakeFundamentals:
    source_code = "TWSE"

    def __init__(self, d: date):
        self.roc = f"{d.year - 1911}{d:%m%d}"

    def fetch_valuations(self, as_of):
        return _payload("TWSE", "BWIBBU", {"data": _twse_valuations(self.roc, extra=120)})

    def fetch_financial_reports(self, as_of):
        return _payload("TWSE", "FIN_REPORT", _twse_reports(extra=120))


def test_sync_then_stock_page_api_and_public_json(conn, client, market, settings, tmp_path):  # noqa: F811
    adapter = FakeFundamentals(market[-1])
    assert sync_valuations(conn, adapter, settings, date(2026, 10, 4)) == "success"
    assert sync_financial_reports(conn, adapter, settings, date(2026, 10, 4)) == "success"
    assert sync_valuations(conn, adapter, settings, date(2026, 10, 4)) == "success"          # 重跑不重複
    assert conn.execute("SELECT count(*) FROM valuations").fetchone()[0] == 2                 # 只寫進已知的股票
    run = conn.execute("SELECT target_date, warning_count FROM ingestion_runs WHERE dataset = 'BWIBBU'").fetchone()
    assert run == (market[-1], 1)                                                            # 記在資料日期；略過的有提醒

    html = client.get("/stock/2330").text
    assert "估值與獲利" in html and "28.98 倍" in html and "49.33 元" in html and "前 2 季累計" in html
    assert 'id="tech-2330"' in html and "SMA20" in html and 'class="chart macd"' in html and 'class="ma std-only"' in html
    assert 'href="#fund-2330"' in html and 'href="#tech-2330"' in html
    assert "官方未提供" in client.get("/stock/2317").text                                      # 本益比空白
    api = client.get("/api/stocks/2330").json()
    assert api["valuation"]["pe_ratio"] == 28.98 and api["financials"][0]["eps"] == 49.33
    ind = client.get("/api/stocks/2330/indicators", params={"days": 5}).json()
    assert len(ind["dates"]) == len(ind["sma5"]) == len(ind["dif"]) == 5 and ind["sma5"][-1] is not None

    from radar.web.public_api import build_public_api
    out = build_public_api(conn, market[-1], tmp_path / "public")
    root = tmp_path / "public" / "api" / "v1"
    assert out["files"] == 4                                                                  # 2 檔股票＋index＋market
    idx = json.loads((root / "index.json").read_text(encoding="utf-8"))
    assert [s[0] for s in idx["stocks"]] == ["2317", "2330"] and "disclaimer" in idx and "series.sma5/sma20/sma60" in idx["fields"]
    doc = json.loads((root / "stocks" / "2330.json").read_text(encoding="utf-8"))
    s = doc["series"]
    assert len(s["date"]) == len(s["close"]) == len(s["sma60"]) == len(s["macd"]) == 45     # 測試資料只有 45 天
    assert s["sma5"][-1] == round(sum(s["close"][-5:]) / 5, 3)
    assert doc["valuation"]["pe_ratio"] == 28.98
    assert doc["financials"] == []          # 2026 Q2 財報最晚 8/31 才公開，3 月的快照不能看到（不洩漏之後的資料）
    assert doc["signals"] and all(g["explanation"] for g in doc["signals"])
    market_doc = json.loads((root / "market.json").read_text(encoding="utf-8"))
    assert market_doc["state"]["label"] and "disclaimer" in market_doc
