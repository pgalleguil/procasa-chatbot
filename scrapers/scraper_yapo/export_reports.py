from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from config import AppConfig


def _flatten_record(record: dict[str, Any]) -> dict[str, Any]:
    classification = record.get("classification") or {}
    return {
        "url": record.get("url", ""),
        "html_path": record.get("html_path", ""),
        "source_search_url": record.get("source_search_url", ""),
        "source_page_url": record.get("source_page_url", ""),
        "page_number": record.get("page_number", ""),
        "title": record.get("title", ""),
        "price": record.get("price", ""),
        "state": classification.get("state", record.get("state", "")),
        "confidence": classification.get("confidence", record.get("confidence", "")),
        "reason": classification.get("reason", record.get("reason", "")),
        "scrape_stage": record.get("scrape_stage", ""),
        "html_validation_status": record.get("html_validation_status", ""),
        "processed_at": record.get("processed_at", ""),
    }


def export_batch_reports(records: list[dict[str, Any]], batch_id: str, config: AppConfig) -> dict[str, Path]:
    config.ensure_layout()
    csv_path = config.reports_dir / f"{batch_id}.csv"
    json_path = config.reports_dir / f"{batch_id}.json"
    summary_path = config.reports_dir / f"{batch_id}_summary.json"

    flat_records = [_flatten_record(record) for record in records]
    if flat_records:
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(flat_records[0].keys()))
            writer.writeheader()
            writer.writerows(flat_records)
    else:
        csv_path.write_text("", encoding="utf-8")

    json_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = Counter(record.get("classification", {}).get("state", record.get("state", "UNKNOWN")) for record in records)
    summary = {
        "batch_id": batch_id,
        "total": len(records),
        "states": dict(counts),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"csv": csv_path, "json": json_path, "summary": summary_path}
