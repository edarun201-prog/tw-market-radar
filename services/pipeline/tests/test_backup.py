"""資料庫備份：只留最新幾份；有 pg_dump 時實際備份測試資料庫，檔案可以用 pg_restore 讀。"""
import os
import shutil
import subprocess

import pytest

from radar.jobs.backup import dump, prune


def test_prune_keeps_newest(tmp_path):
    for d in ("2026-11-01", "2026-11-03", "2026-11-02", "2026-11-04"):
        (tmp_path / f"radar_{d}.dump").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("不是備份，不能刪")
    removed = prune(tmp_path, keep=2)
    assert sorted(f.name for f in removed) == ["radar_2026-11-01.dump", "radar_2026-11-02.dump"]
    assert sorted(f.name for f in tmp_path.iterdir()) == ["notes.txt", "radar_2026-11-03.dump", "radar_2026-11-04.dump"]


@pytest.mark.skipif(not shutil.which("pg_dump"), reason="沒有 pg_dump（例：Docker 的 worker 映像檔）")
def test_dump_test_database(conn, tmp_path):
    out = dump(os.environ["TEST_DATABASE_URL"], tmp_path / "radar_test.dump", shutil.which("pg_dump"))
    listing = subprocess.run([shutil.which("pg_restore"), "--list", str(out)], capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    assert listing.returncode == 0 and "TABLE public stocks" in listing.stdout and "TABLE public news_items" in listing.stdout
    with pytest.raises(RuntimeError):
        dump(os.environ["TEST_DATABASE_URL"].replace("@", "x@", 1), tmp_path / "bad.dump", shutil.which("pg_dump"))
    assert not (tmp_path / "bad.dump").exists()                       # 失敗時不留半個檔案
