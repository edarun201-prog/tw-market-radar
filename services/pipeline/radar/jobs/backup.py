"""資料庫備份：每日流程最後用 pg_dump 存一份（自訂格式，可用 pg_restore 還原），只留最近 KEEP 份。

- 存在 repo 根目錄的 data/backups/（不進版本控制）。硬碟壞掉或被勒索軟體加密時，可以從這裡（或複製到隨身碟的那份）還原，
  不用再花一小時重抓；法說會、估值這些「只有最新一期」的資料錯過就抓不回來，備份是唯一的來源。
- 密碼用環境變數 PGPASSWORD 傳給 pg_dump，不放在命令列上（命令列其他程式看得到）。
- 找不到 pg_dump（例：Docker 的 worker 映像檔）就略過；Windows 上 radar/__init__.py 已經把 PostgreSQL 的 bin 加進 PATH。
還原：pg_restore -U radar -h localhost -d radar --clean --if-exists data\\backups\\radar_YYYY-MM-DD.dump
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from radar.config import REPO_ROOT, TAIPEI, Settings

log = logging.getLogger(__name__)

KEEP = 14


def prune(folder: Path, keep: int = KEEP) -> list[Path]:
    """只留最新的 keep 份（檔名有日期，依檔名排序）；回傳刪掉的檔案。"""
    dumps = sorted(folder.glob("radar_*.dump"))
    old = dumps[:-keep] if keep else dumps
    for f in old:
        f.unlink()
    return old


def dump(database_url: str, out: Path, pg_dump: str) -> Path:
    u = urlparse(database_url)
    env = os.environ | {"PGPASSWORD": unquote(u.password or "")}
    args = [pg_dump, "--format=custom", "--no-owner", f"--file={out}", f"--host={u.hostname or 'localhost'}",
            f"--port={u.port or 5432}", f"--username={unquote(u.username or '')}", u.path.lstrip("/")]
    r = subprocess.run(args, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump 失敗：{r.stderr.strip()[:500]}")
    return out


def backup_database(settings: Settings, folder: Path | None = None) -> Path | None:
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        log.info("備份：找不到 pg_dump，略過")
        return None
    folder = folder or (REPO_ROOT / "data" / "backups" if REPO_ROOT else Path("data/backups"))
    folder.mkdir(parents=True, exist_ok=True)
    out = dump(settings.database_url, folder / f"radar_{datetime.now(TAIPEI):%Y-%m-%d}.dump", pg_dump)
    removed = prune(folder)
    log.info("備份：%s（%.1f MB），刪掉舊的 %d 份", out.name, out.stat().st_size / 1e6, len(removed))
    return out
