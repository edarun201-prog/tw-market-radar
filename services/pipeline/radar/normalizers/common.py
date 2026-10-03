"""台灣官方報表常見格式的解析工具。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

_TAG = re.compile(r"<[^>]+>")
_EMPTY = {"", "--", "---", "----", "X", "x", "N/A", "null", "None"}


class NormalizeError(ValueError):
    """回應格式與預期不同（表格或欄位找不到）。訊息會列出實際拿到的標題與欄位，方便修 normalizer。"""


def clean_str(v: Any) -> str:
    s = "" if v is None else str(v)
    s = _TAG.sub("", s)
    return s.replace("=", "").replace('"', "").replace("\u3000", " ").strip()


def _numeric_text(v: Any) -> str | None:
    s = clean_str(v).replace(",", "").replace(" ", "")
    if s in _EMPTY:
        return None
    s = re.sub(r"^[^\d\-+.]+", "", s)  # 去掉「X0.50」這類前綴
    return s or None


def parse_int(v: Any) -> int | None:
    s = _numeric_text(v)
    if s is None:
        return None
    try:
        return int(Decimal(s))
    except InvalidOperation:
        return None


def parse_decimal(v: Any) -> Decimal | None:
    s = _numeric_text(v)
    if s is None:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_sign(v: Any) -> tuple[int, bool]:
    """漲跌(+/-) 欄位：回傳 (正負號, 是否為不比價 X)。原始值可能是 HTML，例：<p style= color:red>+</p>"""
    s = clean_str(v)
    if "X" in s.upper():
        return 0, True
    if "+" in s:
        return 1, False
    if "-" in s:
        return -1, False
    return 0, False


_ROC_PARTS = re.compile(r"^(\d{2,3})\D+(\d{1,2})\D+(\d{1,2})\D*$")


def parse_roc_date(s: str) -> date:
    """民國日期 → 西元：115/09/24、115年09月24日、1150924 都是 2026-09-24"""
    s = clean_str(s)
    if re.fullmatch(r"\d{7}", s):
        return date(int(s[:3]) + 1911, int(s[3:5]), int(s[5:]))
    m = _ROC_PARTS.match(s)
    if not m:
        raise ValueError(f"無法解析的民國日期：{s!r}")
    y, mo, d = (int(x) for x in m.groups())
    return date(y + 1911, mo, d)


def parse_ymd(s: str) -> date:
    s = clean_str(s)
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def security_type(symbol: str) -> str:
    if re.fullmatch(r"[1-9]\d{3}", symbol):
        return "stock"
    if re.fullmatch(r"00\d{2,4}[A-Z]?", symbol):
        return "etf"
    return "other"


# ---- 表格定位 -------------------------------------------------------------
@dataclass
class Table:
    title: str
    fields: list[str]
    data: list[list[Any]]


def iter_tables(body: dict[str, Any]) -> list[Table]:
    """支援新版 {"tables": [...]} 與舊版 fieldsN / dataN 兩種格式。"""
    out: list[Table] = []
    if isinstance(body.get("tables"), list):
        for t in body["tables"]:
            if isinstance(t, dict) and t.get("fields"):
                out.append(Table(clean_str(t.get("title")), [clean_str(f) for f in t["fields"]], t.get("data") or []))
        return out
    for key, fields in body.items():
        if key.startswith("fields") and isinstance(fields, list):
            suffix = key[len("fields"):]
            title = body.get(f"subtitle{suffix}") or body.get(f"title{suffix}") or body.get("title") or ""
            out.append(Table(clean_str(title), [clean_str(f) for f in fields], body.get(f"data{suffix}") or []))
    return out


def find_tables(tables: list[Table], keyword: str) -> list[Table]:
    return [t for t in tables if keyword in t.title]


def require_table(tables: list[Table], keyword: str) -> Table:
    found = find_tables(tables, keyword)
    if not found:
        raise NormalizeError(f"找不到標題含「{keyword}」的表格；實際標題：{[t.title for t in tables]}")
    return found[0]


def col(fields: list[str], match: str | Callable[[str], bool], *, required: bool = True) -> int | None:
    pred = match if callable(match) else (lambda f: match in f)
    for i, f in enumerate(fields):
        if pred(f):
            return i
    if required:
        raise NormalizeError(f"找不到欄位「{match}」；實際欄位：{fields}")
    return None


def cols(fields: list[str], pred: Callable[[str], bool]) -> list[int]:
    return [i for i, f in enumerate(fields) if pred(f)]


def sum_cols(row: list[Any], idxs: list[int]) -> int | None:
    if not idxs:
        return None
    vals = [parse_int(row[i]) for i in idxs]
    if all(v is None for v in vals):
        return None
    return sum(v or 0 for v in vals)
