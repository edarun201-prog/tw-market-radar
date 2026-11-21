"""法說會：重大訊息（第 12 款）的解析、入庫、個股頁與今日市場。說明欄的格式取自 2026-11-20 的實際公告。"""
from datetime import date, datetime, time, timedelta

from radar.adapters.base import RawPayload
from radar.config import TAIPEI
from radar.jobs.fundamentals import sync_conferences
from radar.normalizers.conferences import normalize_conferences

from .test_web import client, market  # noqa: F401  （共用網站測試的人工市場）


def _roc(d: date) -> str:
    return f"{d.year - 1911}/{d:%m/%d}"


def _payload(rows, source="TWSE"):
    return RawPayload(source=source, dataset="CONF", trade_date=date(2026, 11, 21), url="fixture://", body={"data": rows},
                      fetched_at=datetime.now(TAIPEI), has_data=True, meta={})


def _twse_row(symbol, start: date, end: date | None = None, when="16 時 00 分", where="線上法說會", said: date = date(2026, 11, 20),
              clock="84532"):
    span = _roc(start) + (f" ~ {_roc(end)}" if end else "")
    return {"出表日期": "1151121", "發言日期": f"{said.year - 1911}{said:%m%d}", "發言時間": clock, "公司代號": symbol,
            "公司名稱": "x", "主旨 ": "本公司受邀參加元大證券舉辦之線上法人說明會", "符合條款": "第12款",
            "事實發生日": _roc(start),
            "說明": f"符合條款第四條第XX款：12 事實發生日：{_roc(start)}\n1.召開法人說明會之日期：{span}\n"
                    f"2.召開法人說明會之時間：{when}\n3.召開法人說明會之地點：{where}\n"
                    "4.法人說明會擇要訊息：本公司受邀參加元大證券舉辦之線上法說會，報告本公司 營運狀況。\n"
                    "5.其他應敘明事項：無 完整財務業務資訊請至公開資訊觀測站之法人說明會一覽表或法說會項目下查閱。"}


def test_normalize_formats():
    other = {**_twse_row("2317", date(2026, 11, 24)), "符合條款": "第51款", "說明": "澄清媒體報導"}
    rows = [_twse_row("1446", date(2026, 11, 24)), other,
            _twse_row("6584", date(2026, 11, 23), date(2026, 11, 24), when="09 時 00 分",
                      where="福邦證券股份有限公司會議室(台北市忠孝西路一段6號14樓)", clock="140435"),
            _twse_row("4420", date(2026, 11, 20), when="下午2時30分"),
            _twse_row("2613", date(2026, 11, 20), when="另行通知")]
    confs, warnings = normalize_conferences(_payload(rows))
    assert not warnings and [c.symbol for c in confs] == ["1446", "6584", "4420", "2613"]      # 其他條款的公告不算
    a, b, c, d = confs
    assert (a.starts_on, a.ends_on, a.start_time, a.location) == (date(2026, 11, 24), date(2026, 11, 24), time(16, 0), "線上法說會")
    assert (a.announced_on, a.announced_at) == (date(2026, 11, 20), time(8, 45, 32))           # 發言時間省略前面的 0
    assert a.summary.startswith("本公司受邀參加元大證券") and a.subject.startswith("本公司受邀")  # 上市的「主旨 」多一個空白
    assert (b.starts_on, b.ends_on, b.start_time) == (date(2026, 11, 23), date(2026, 11, 24), time(9, 0))   # 多天的論壇
    assert b.location == "福邦證券股份有限公司會議室(台北市忠孝西路一段6號14樓)"                 # 地址裡的數字不會切斷地點
    assert c.start_time == time(14, 30) and d.start_time is None                               # 看不懂的時間不猜

    otc = {"Date": "1151120", "發言日期": "1151119", "發言時間": "94517", "SecuritiesCompanyCode": "6584", "CompanyName": "南俊國際",
           "主旨": "公告本公司受邀參加花旗證券舉辦之 Citi's 2026 Taiwan Corporate Day", "符合條款": "第12款", "事實發生日": "115/11/23",
           "說明": "1.召開法人說明會之日期：說明如下 2.召開法人說明會之時間：09 時 00 分 3.召開法人說明會之地點：W Hotel Taipei "
                   "4.法人說明會擇要訊息：說明本公司財務及業務相關資訊。"}
    [o], w = normalize_conferences(_payload([otc], "TPEX"))
    assert o.symbol == "6584" and o.starts_on == date(2026, 11, 23) and o.location == "W Hotel Taipei"
    assert o.summary == "說明本公司財務及業務相關資訊。" and "事實發生日" in w[0]                 # 日期寫不清楚：改用事實發生日


class FakeNews:
    source_code = "TWSE"

    def __init__(self, rows):
        self.rows = rows

    def fetch_material_news(self, as_of):
        return _payload(self.rows)


def test_sync_then_stock_page_and_market(conn, client, market, settings):  # noqa: F811
    last = market[-1]
    rows = [_twse_row("2330", last + timedelta(days=2), said=last), _twse_row("9999", last, said=last),
            _twse_row("2317", last + timedelta(days=30), said=last)]
    assert sync_conferences(conn, FakeNews(rows), settings, last) == "success"
    assert sync_conferences(conn, FakeNews(rows), settings, last) == "success"                  # 重跑不重複
    assert conn.execute("SELECT count(*) FROM investor_conferences").fetchone()[0] == 2          # 不在股票清單的略過
    # 更正公告：同一場、較晚的公告，顯示新的地點
    fix = _twse_row("2330", last + timedelta(days=2), said=last, clock="170000", where="台北國際會議中心")
    sync_conferences(conn, FakeNews([fix]), settings, last)

    page = client.get("/stock/2330").text
    assert 'id="conf-2330"' in page and "台北國際會議中心" in page and "16:00" in page
    assert page.count("<li><b>") == 1                                                          # 更正前的那則不再顯示
    home = client.get("/market").text
    assert 'id="conf-title"' in home and "台北國際會議中心" in home and "/stock/2330" in home
    assert home.count("<td class=\"small\">") == 1                                             # 30 天後的不在「接下來 7 天」
    before = client.get("/market", params={"date": str(market[-2])}).text
    assert 'id="conf-title"' not in before                                                     # 過去的日子看不到之後才公告的
