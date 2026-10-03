"""數字格式：網站、匯出檔、API 的說明文字、AI facts 共用。資料庫存股與元，這裡才換成張與億元。"""
from __future__ import annotations

from datetime import date

WEEKDAYS = "一二三四五六日"


def num(v) -> float | None:
    return None if v is None else float(v)


def fmt_lots(shares) -> str:
    """股 → 張"""
    return "–" if shares is None else f"{float(shares) / 1000:,.0f}"


def fmt_yi(dollars) -> str:
    """元 → 億"""
    return "–" if dollars is None else f"{float(dollars) / 1e8:,.1f} 億"


def fmt_price(v) -> str:
    if v is None:
        return "–"
    s = f"{float(v):,.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def fmt_ret(v) -> str:
    """小數報酬（0.0123）→ +1.23%"""
    return "–" if v is None else f"{float(v) * 100:+.2f}%"


def fmt_pct(v) -> str:
    """已經是百分比的數字（官方漲跌幅 1.23）→ +1.23%"""
    return "–" if v is None else f"{float(v):+.2f}%"


def fmt_abs_ret(v) -> str:
    """小數報酬取絕對值（-0.0283 → 2.83%），用在「下跌 2.83%」這種句子"""
    return "–" if v is None else f"{abs(float(v)) * 100:.2f}%"


def fmt_signed(v) -> str:
    return "–" if v is None else f"{float(v):+,.2f}".rstrip("0").rstrip(".")


def tone(v) -> str:
    v = num(v)
    return "flat" if not v else "up" if v > 0 else "down"


def fmt_day(d: date) -> str:
    return f"{d:%Y/%m/%d}（{WEEKDAYS[d.weekday()]}）"
