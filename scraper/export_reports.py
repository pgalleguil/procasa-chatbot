"""CSV and summary exports for the standalone Yapo owner-hunt runner."""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


def export_batch_reports(records: list[dict[str, Any]], batch_id: str, config) -> dict[str, Path]:
    reports = Path(config.reports_dir)
    reports.mkdir(parents=True, exist_ok=True)
    csv_path = reports / f"owner_hunt_{batch_id}.csv"
    summary_path = reports / f"owner_hunt_{batch_id}_summary.json"
    fields = sorted({str(key) for record in records if isinstance(record, dict) for key in record})
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or ["listing_id"], extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({key: json.dumps(value, ensure_ascii=False, default=str)
                             if isinstance(value, (dict, list)) else value
                             for key, value in record.items()})
    states = Counter()
    errors = 0
    for record in records:
        classification = record.get("classification") or {}
        state = classification.get("state") or classification.get("final_state") or "UNKNOWN"
        states[str(state).upper()] += 1
        errors += bool(record.get("error"))
    summary_path.write_text(json.dumps({
        "batch_id": batch_id,
        "record_count": len(records),
        "error_count": errors,
        "classification_counts": dict(states),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"csv": csv_path, "summary": summary_path}
