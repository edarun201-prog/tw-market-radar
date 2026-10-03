from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from radar.adapters.base import FetchError, RawPayload
from radar.config import TAIPEI, load_dotenv, load_settings
from radar.migrate import migrations_dir, up_sql

FIXTURES = Path(__file__).parent / "fixtures"
load_dotenv()  # 本機執行時從 .env 取得 TEST_DATABASE_URL


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def payload(dataset: str, d: date, body: dict) -> RawPayload:
    return RawPayload(source="TWSE", dataset=dataset, trade_date=d, url=f"fixture://{dataset}",
                      fetched_at=datetime.now(TAIPEI), body=body,
                      has_data=str(body.get("stat", "")).upper() == "OK", meta={"stat": body.get("stat")})


def shift_date(body: dict, d: date) -> dict:
    """把 fixture 改成另一天的資料（測試回補用）。"""
    b = copy.deepcopy(body)
    if "date" in b:
        b["date"] = d.strftime("%Y%m%d")
    return b


class FakeAdapter:
    """依日期回傳預先準備好的 body；沒有準備的日期回傳「查無資料」。"""
    source_code = "TWSE"

    def __init__(self, market: dict[date, dict] | None = None, inst: dict[date, dict] | None = None,
                 fail_on: set[date] | None = None):
        self.market, self.inst, self.fail_on = market or {}, inst or {}, fail_on or set()
        self.calls: list[tuple[str, date]] = []

    def _get(self, ds, d, store):
        self.calls.append((ds, d))
        if d in self.fail_on:
            raise FetchError("模擬網路錯誤")
        return payload(ds, d, store.get(d, load_fixture("twse_no_data.json")))

    def fetch_market_day(self, d):
        return self._get("MI_INDEX", d, self.market)

    def fetch_institutional(self, d):
        return self._get("T86", d, self.inst)


@pytest.fixture
def settings(tmp_path):
    return replace(load_settings(), raw_dir=tmp_path / "raw", min_quote_rows=1, min_flow_rows=1,
                   request_interval_sec=0)


def _migration_up_sql() -> list[str]:
    return [up_sql(f.read_text(encoding="utf-8")) for f in sorted(migrations_dir().glob("*.sql"))]


@pytest.fixture
def conn():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("未設定 TEST_DATABASE_URL，略過資料庫整合測試")
    import psycopg
    from radar.repository import connect
    try:
        c = connect(url)
    except psycopg.OperationalError as e:
        pytest.skip(f"連不到測試資料庫，略過資料庫整合測試：{e}")
    c.execute("DROP SCHEMA IF EXISTS public CASCADE")
    c.execute("CREATE SCHEMA public")
    for sql in _migration_up_sql():
        c.execute(sql)
    yield c
    c.close()
