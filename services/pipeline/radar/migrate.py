"""不用 dbmate 也能建表：依檔名順序執行 db/migrations/*.sql 的 migrate:up 段落。

紀錄寫在 dbmate 相同的 schema_migrations 表，之後改用 Docker（dbmate）也不會重跑已完成的 migration。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from radar.config import REPO_ROOT

_DOWN = re.compile(r"^-- migrate:down", re.M)


def migrations_dir() -> Path:
    if os.environ.get("MIGRATIONS_DIR"):
        return Path(os.environ["MIGRATIONS_DIR"])
    if REPO_ROOT is None:
        raise RuntimeError("找不到 db/migrations，請設定 MIGRATIONS_DIR")
    return REPO_ROOT / "db" / "migrations"


def up_sql(text: str) -> str:
    return _DOWN.split(text)[0].replace("-- migrate:up", "")


def migrate(conn, directory: Path | None = None) -> list[str]:
    """回傳這次執行的 migration 檔名；已執行過的會跳過。"""
    directory = directory or migrations_dir()
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version varchar(128) PRIMARY KEY)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    applied = []
    for f in sorted(directory.glob("*.sql")):
        version = f.name.split("_", 1)[0]
        if version in done:
            continue
        with conn.transaction():
            conn.execute(up_sql(f.read_text(encoding="utf-8")))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
        applied.append(f.name)
    return applied
