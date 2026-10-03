"""原始回應存檔：data/raw/{source}/{dataset}/{YYYY-MM-DD}.json.gz

修改 normalizer 後可用 `python -m radar reprocess` 直接重算，不必重新請求。
"""
from __future__ import annotations

import gzip
import json
from datetime import date, datetime
from pathlib import Path

from radar.adapters.base import RawPayload


def raw_path(root: Path, source: str, dataset: str, d: date) -> Path:
    return root / source / dataset / f"{d.isoformat()}.json.gz"


def save(root: Path, p: RawPayload) -> Path:
    path = raw_path(root, p.source, p.dataset, p.trade_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "source": p.source,
        "dataset": p.dataset,
        "trade_date": p.trade_date.isoformat(),
        "url": p.url,
        "fetched_at": p.fetched_at.isoformat(),
        "has_data": p.has_data,
        "meta": p.meta,
        "body": p.body,
    }
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    return path


def load(root: Path, source: str, dataset: str, d: date) -> RawPayload:
    path = raw_path(root, source, dataset, d)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        doc = json.load(f)
    return RawPayload(
        source=doc["source"],
        dataset=doc["dataset"],
        trade_date=date.fromisoformat(doc["trade_date"]),
        url=doc["url"],
        fetched_at=datetime.fromisoformat(doc["fetched_at"]),
        body=doc["body"],
        has_data=doc["has_data"],
        meta=doc.get("meta", {}),
    )
