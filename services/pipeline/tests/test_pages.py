"""公開網頁：gh-pages 只留一個 commit、首頁就是最新的匯出檔、內容沒變不推。用本機的空儲存庫當遠端，不連網路。"""
import subprocess

import pytest

from radar.jobs.pages import BRANCH, publish_pages


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True).stdout


@pytest.fixture
def remote(tmp_path):
    r = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(r)], check=True)
    return r


def test_publish_keeps_one_commit_and_skips_unchanged(tmp_path, remote):
    src = tmp_path / "台股雷達_2026-10-02.html"
    src.write_text("<!doctype html><title>雷達</title>" + "x" * 200_000, encoding="utf-8")
    work = tmp_path / "pages"

    assert publish_pages(src, work, str(remote), label="2026-10-02") == "published"
    assert git(remote, "ls-tree", "--name-only", BRANCH).split() == [".nojekyll", "index.html"]
    assert "台股雷達 2026-10-02 盤後" in git(remote, "log", "-1", "--format=%B", BRANCH)

    assert publish_pages(src, work, str(remote), label="2026-10-02") == "unchanged"   # 內容沒變不推

    src = tmp_path / "台股雷達_2026-10-05.html"
    src.write_text("<!doctype html><title>雷達 新的一天</title>" + "y" * 200_000, encoding="utf-8")
    assert publish_pages(src, work, str(remote), label="2026-10-05") == "published"
    assert git(remote, "rev-list", "--count", BRANCH).strip() == "1"                     # 只留最新一份
    assert "新的一天" in git(remote, "show", f"{BRANCH}:index.html")


def test_refuses_to_publish_a_broken_export(tmp_path, remote):
    src = tmp_path / "台股雷達_2026-10-02.html"
    src.write_text("<html>壞掉</html>", encoding="utf-8")
    with pytest.raises(RuntimeError):
        publish_pages(src, tmp_path / "pages", str(remote))
    with pytest.raises(RuntimeError):
        publish_pages(tmp_path / "不存在.html", tmp_path / "pages", str(remote))
