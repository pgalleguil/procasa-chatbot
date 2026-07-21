"""Repair leads with canonical management evidence whose effective stage is still NEW.

Dry-run is the default. Use --apply to create a JSON backup and persist the repair.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from bson import json_util

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chatbot.storage import get_db
from chatbot.crm_updates import bump_crm_leads_version


NEW_VALUES = {None, "", "NEW", "new", "nuevo"}


def effective_stage(lead):
    return lead.get("pipeline_stage") or lead.get("stage") or lead.get("crm_estado") or "NEW"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db = get_db()
    candidates = list(db["leads"].find(
        {"lifecycle.first_valid_management_at": {"$exists": True, "$ne": None}},
        {
            "phone": 1, "pipeline_stage": 1, "stage": 1, "crm_estado": 1,
            "ejecutivo_asignado": 1, "lifecycle.first_valid_management_at": 1,
            "stage_history": 1,
        },
    ))
    affected = [lead for lead in candidates if effective_stage(lead) in NEW_VALUES]
    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "affected_count": len(affected),
        "phones": [lead.get("phone") for lead in affected],
    }, ensure_ascii=False, indent=2))

    if not args.apply or not affected:
        return

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    backup_path = Path(__file__).resolve().parents[1] / "backups" / f"leads_managed_stuck_new_{stamp}.json"
    backup_path.write_text(json_util.dumps(affected, ensure_ascii=False, indent=2), encoding="utf-8")

    repaired = 0
    for lead in affected:
        result = db["leads"].update_one(
            {
                "_id": lead["_id"],
                "lifecycle.first_valid_management_at": {"$exists": True, "$ne": None},
            },
            {
                "$set": {
                    "pipeline_stage": "CONTACTED",
                    "stage": "CONTACTED",
                    "last_crm_update": now,
                    "state_repair": {
                        "version": "managed_stuck_new_v1",
                        "repaired_at": now,
                        "reason": "canonical_management_evidence_with_new_stage",
                    },
                }
            },
        )
        repaired += result.modified_count
        if result.modified_count:
            bump_crm_leads_version(
                db, reason="repair_managed_stuck_new_v1", phone=lead.get("phone")
            )

    print(json.dumps({"repaired_count": repaired, "backup": str(backup_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
