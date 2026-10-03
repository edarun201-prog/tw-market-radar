"""公開網頁（GitHub Pages）：把最新的單一 HTML 檔發布成網站首頁。

- 只在 .env 設定了 PAGES_REPO（例：https://github.com/<帳號>/tw-market-radar.git）時才發布；沒設定就略過。
- 推到獨立的 gh-pages 分支，每次都只留「一個」commit（強制取代），儲存庫不會因為每天的資料越來越大；
  程式碼所在的 main 分支完全不動。GitHub 的 Settings → Pages 要選 gh-pages 分支。
- 內容和上次發布的一樣就不推。推送用這台電腦已登入的 git 憑證（Windows 的 Git Credential Manager）。
- 發布的是盤後資料的離線快照（不含盤中即時行情）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

BRANCH = "gh-pages"
MIN_BYTES = 100_000          # 匯出檔小於這個大小就當成壞掉，不發布


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失敗：{(out.stderr or out.stdout).strip()[:500]}")
    return out.stdout


def _remove_readonly(func, path, _exc) -> None:
    """Windows 上 git 的物件檔是唯讀的，先拿掉唯讀再刪。"""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def publish_pages(src: Path, work_dir: Path, repo_url: str, *, label: str | None = None,
                  force: bool = False) -> str:
    """把匯出的單一 HTML 檔 src 發布成 gh-pages 的 index.html。回傳 published／unchanged。"""
    if not src.exists() or src.stat().st_size < MIN_BYTES:
        raise RuntimeError(f"找不到可以發布的匯出檔：{src}")
    html = src.read_bytes()
    digest = hashlib.sha256(html).hexdigest()
    stamp = work_dir.parent / f"{work_dir.name}.published"
    if not force and stamp.exists() and stamp.read_text(encoding="utf-8").strip() == digest:
        log.info("公開網頁：內容和上次相同，不推送")
        return "unchanged"

    # 每次都從乾淨的資料夾開始，只放首頁與 .nojekyll（不經過 Jekyll，原樣提供）
    if work_dir.exists():
        shutil.rmtree(work_dir, onexc=_remove_readonly)
    work_dir.mkdir(parents=True)
    (work_dir / "index.html").write_bytes(html)
    (work_dir / ".nojekyll").write_bytes(b"")

    _git(work_dir, "init", "-q", "-b", BRANCH)
    _git(work_dir, "add", "-A")
    message = (f"台股雷達 {label} 盤後" if label else "台股雷達 盤後快照") + \
        "\n\n由每日流程自動發布（只保留最新一份）。\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
    _git(work_dir, "commit", "-q", "-m", message)
    _git(work_dir, "push", "-q", "--force", repo_url, f"{BRANCH}:{BRANCH}")
    stamp.write_text(digest, encoding="utf-8")
    log.info("公開網頁：已發布到 %s 的 %s 分支", repo_url, BRANCH)
    return "published"
