"""法人說明會（證交所 t187ap04_L、櫃買中心 mopsfin_t187ap04_O：每日重大訊息）→ 標準格式。

法說會的公告是「符合條款：第12款」，說明欄有固定的編號格式：
  1.召開法人說明會之日期：115/11/23 ~ 115/11/24 2.召開法人說明會之時間：09 時 00 分
  3.召開法人說明會之地點：W Hotel Taipei 4.法人說明會擇要訊息：…… 5.其他應敘明事項：……
上市的代號欄位是中文（公司代號），上櫃是英文（SecuritiesCompanyCode）；上市的「主旨 」欄位名稱後面多一個空白。
時間寫法不一（09 時 00 分、下午2時、14:00），看不懂就存 None，不猜；日期看不懂就用事實發生日。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time

from radar.adapters.base import RawPayload
from radar.normalizers.common import NormalizeError, clean_str, parse_roc_date

_ROC = r"\d{2,3}\s*/\s*\d{1,2}\s*/\s*\d{1,2}"
_DATES = re.compile(rf"日期\s*[:：]\s*({_ROC})(?:\s*[~～至\-－]\s*({_ROC}))?")
_TIME = re.compile(r"時間\s*[:：]\s*(上午|下午|早上|晚上)?\s*(\d{1,2})\s*(?:時|點|:|：)\s*(\d{1,2})?")
_LOCATION = re.compile(r"地點\s*[:：]\s*(.*?)\s*\d\s*[.．、]\s*法人說明會擇要訊息")
_SUMMARY = re.compile(r"擇要訊息\s*[:：]\s*(.*?)\s*(?:\d\s*[.．、]\s*其他應敘明事項|$)")


@dataclass(frozen=True)
class Conference:
    symbol: str
    announced_on: date
    announced_at: time
    starts_on: date
    ends_on: date
    start_time: time | None
    location: str | None
    summary: str | None
    subject: str


def _clock(s: str) -> time:
    """發言時間：84532 → 08:45:32（前面的 0 會被省略）。"""
    s = clean_str(s).zfill(6)
    return time(int(s[:2]), int(s[2:4]), int(s[4:6]))


def _start_time(text: str) -> time | None:
    m = _TIME.search(text)
    if not m:
        return None
    half, hour, minute = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    if half in ("下午", "晚上") and hour < 12:
        hour += 12
    return time(hour, minute) if hour < 24 and minute < 60 else None


def _text(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text)
    return (m.group(1).strip() or None) if m else None


def is_conference(r: dict) -> bool:
    return "12" in clean_str(r.get("符合條款")) and "法人說明會" in clean_str(r.get("說明"))


def normalize_conferences(p: RawPayload) -> tuple[list[Conference], list[str]]:
    rows = [{k.strip(): v for k, v in r.items()} for r in p.body.get("data") or []]
    out, warnings = [], []
    for r in rows:
        if not is_conference(r):
            continue
        symbol = clean_str(r.get("公司代號") or r.get("SecuritiesCompanyCode"))
        text = re.sub(r"\s+", " ", clean_str(r.get("說明")))
        try:
            announced_on, announced_at = parse_roc_date(r["發言日期"]), _clock(r["發言時間"])
            m = _DATES.search(text)
            if m:
                starts = parse_roc_date(m.group(1).replace(" ", ""))
                ends = parse_roc_date(m.group(2).replace(" ", "")) if m.group(2) else starts
            else:
                starts = ends = parse_roc_date(r["事實發生日"])
                warnings.append(f"{symbol} 說明裡找不到法說會日期，改用事實發生日 {starts}")
        except (KeyError, ValueError) as e:
            warnings.append(f"{symbol} 日期或發言時間無法解析（{e}）：{text[:80]}")
            continue
        if not symbol:
            warnings.append(f"缺少公司代號：{text[:80]}")
            continue
        out.append(Conference(symbol=symbol, announced_on=announced_on, announced_at=announced_at,
                              starts_on=starts, ends_on=max(starts, ends), start_time=_start_time(text),
                              location=_text(_LOCATION, text), summary=_text(_SUMMARY, text),
                              subject=clean_str(r.get("主旨")) or "法人說明會"))
    if not rows:
        raise NormalizeError("重大訊息是空的")
    return out, warnings
