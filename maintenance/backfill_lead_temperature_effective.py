"""Controlled backfill for the canonical CRM lead temperature.

Dry-run is the default. Use ``--apply`` to write only normalization fields.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from pymongo import UpdateOne

from chatbot.constants import CHILE_TZ
from chatbot.crm_updates import bump_crm_leads_version
from chatbot.lead_temperature import COLD, HOT, derive_effective_temperature
from chatbot.storage import get_db


PROJECTION = {
    "phone": 1,
    "lead_temperature": 1,
    "lead_temperature_effective": 1,
    "last_intent": 1,
    "pipeline_stage": 1,
    "stage": 1,
    "crm_estado": 1,
    "prospecto.alerts_sent": 1,
    "alerts_sent": 1,
}


def build_plan(collection):
    counts = Counter()
    changes = []
    for lead in collection.find({}, PROJECTION):
        effective = derive_effective_temperature(lead)
        counts[effective] += 1
        if lead.get("lead_temperature_effective") != effective:
            changes.append((lead, effective))
    return changes, counts


def write_backup(changes) -> Path:
    timestamp = datetime.now(CHILE_TZ).strftime("%Y%m%d_%H%M%S")
    backup_path = Path("backups") / f"lead_temperature_effective_pre_backfill_{timestamp}.json"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "_id": str(lead["_id"]),
            "phone": lead.get("phone"),
            "previous_lead_temperature_effective": lead.get("lead_temperature_effective"),
            "new_lead_temperature_effective": effective,
        }
        for lead, effective in changes
    ]
    backup_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply the planned $set operations")
    args = parser.parse_args()

    db = get_db()
    collection = db["leads"]
    changes, counts = build_plan(collection)
    print(f"classifiable={sum(counts.values())} hot={counts[HOT]} cold={counts[COLD]}")
    print(f"planned_updates={len(changes)} mode={'APPLY' if args.apply else 'DRY_RUN'}")
    if not args.apply or not changes:
        return 0

    backup_path = write_backup(changes)
    now_iso = datetime.now(CHILE_TZ).isoformat()
    operations = [
        UpdateOne(
            {"_id": lead["_id"]},
            {"$set": {
                "lead_temperature_effective": effective,
                "lead_temperature_effective_version": 1,
                "lead_temperature_effective_updated_at": now_iso,
            }},
        )
        for lead, effective in changes
    ]
    result = collection.bulk_write(operations, ordered=False)
    remaining_invalid = collection.count_documents({
        "lead_temperature_effective": {"$nin": [HOT, COLD]}
    })
    if remaining_invalid:
        raise RuntimeError(f"Backfill incompleto: {remaining_invalid} documentos no normalizados")
    bump_crm_leads_version(db, reason="temperature_effective_backfill")
    print(f"matched={result.matched_count} modified={result.modified_count}")
    print(f"backup={backup_path.resolve()}")
    print("verification=OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
