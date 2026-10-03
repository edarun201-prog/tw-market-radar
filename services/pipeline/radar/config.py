"""設定一律從環境變數讀取，方便 Docker 與本機共用。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")


def _repo_root() -> Path | None:
    for p in Path(__file__).resolve().parents:
        if (p / "docker-compose.yml").exists():
            return p
    return None  # Docker 映像檔裡只有 services/pipeline，找不到根目錄；設定全部由 compose 提供


REPO_ROOT = _repo_root()


def load_dotenv() -> None:
    """本機（不用 Docker）執行時讀 repo 根目錄的 .env；已經設定的環境變數優先。"""
    path = REPO_ROOT / ".env" if REPO_ROOT else None
    if not path or not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True)
class Settings:
    database_url: str
    raw_dir: Path
    request_interval_sec: float  # 兩次請求之間至少間隔幾秒（避免被證交所封鎖）
    request_timeout_sec: float
    max_retries: int
    min_quote_rows: int          # 全市場行情少於此筆數視為資料不完整
    min_flow_rows: int
    max_row_change_ratio: float  # 與前一交易日筆數差距超過此比例視為異常
    schedule_hour: int
    schedule_minute: int
    retry_every_min: int
    retry_until_hour: int
    retry_until_minute: int
    web_database_url: str        # 網站用唯讀帳號；沒設定時用 database_url
    export_dir: Path             # 匯出的 HTML 檔
    live_provider: str = "mis"   # 盤中即時行情的資料來源：mis（證交所基本市況報導，只給自己看）、off（關閉）
    pages_repo: str = ""         # 公開網頁（GitHub Pages）要推到哪個儲存庫；空的就不發布


def load_settings() -> Settings:
    load_dotenv()
    env = os.environ.get
    default_raw = REPO_ROOT / "data" / "raw" if REPO_ROOT else Path("data/raw")
    return Settings(
        database_url=env("DATABASE_URL", "postgresql://radar:radar@localhost:5432/radar"),
        raw_dir=Path(env("RAW_DIR", str(default_raw))),
        request_interval_sec=float(env("REQUEST_INTERVAL_SEC", "4")),
        request_timeout_sec=float(env("REQUEST_TIMEOUT_SEC", "30")),
        max_retries=int(env("MAX_RETRIES", "3")),
        min_quote_rows=int(env("MIN_QUOTE_ROWS", "800")),
        min_flow_rows=int(env("MIN_FLOW_ROWS", "500")),
        max_row_change_ratio=float(env("MAX_ROW_CHANGE_RATIO", "0.2")),
        schedule_hour=int(env("SCHEDULE_HOUR", "18")),
        schedule_minute=int(env("SCHEDULE_MINUTE", "30")),
        retry_every_min=int(env("RETRY_EVERY_MIN", "20")),
        retry_until_hour=int(env("RETRY_UNTIL_HOUR", "21")),
        retry_until_minute=int(env("RETRY_UNTIL_MINUTE", "30")),
        web_database_url=env("WEB_DATABASE_URL") or env("DATABASE_URL", "postgresql://radar:radar@localhost:5432/radar"),
        export_dir=Path(env("EXPORT_DIR", str(REPO_ROOT / "data" / "exports" if REPO_ROOT else "data/exports"))),
        live_provider=env("LIVE_PROVIDER", "mis"),
        pages_repo=env("PAGES_REPO", ""),
    )
