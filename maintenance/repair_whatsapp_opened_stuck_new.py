"""Repair unattended leads with a WhatsApp open in their current assignment."""
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

from chatbot.crm_metrics import coerce_utc_datetime
from chatbot.crm_updates import bump_crm_leads_version
from chatbot.storage import get_db


NEW_VALUES = {None, "", "NEW", "new", "nuevo"}


def effective_stage(lead):
    return lead.get("pipeline_stage") or lead.get("stage") or lead.get("crm_estado") or "NEW"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = get_db()

    candidates = []
    for lead in db["leads"].find({}, {
        "phone": 1, "pipeline_stage": 1, "stage": 1, "crm_estado": 1,
        "ejecutivo_asignado": 1, "lifecycle": 1, "fecha_asignacion": 1,
    }):
        if effective_stage(lead) not in NEW_VALUES:
            continue
        assigned = coerce_utc_datetime(
            (lead.get("lifecycle") or {}).get("assigned_at") or lead.get("fecha_asignacion")
        )
        events = list(db["crm_events"].find({
            "lead_id": lead["_id"], "type": "CLICK_WHATSAPP_LEAD",
            "actor_type": "human",
        }).sort("timestamp", 1))
        valid = [
            event for event in events
            if coerce_utc_datetime(event.get("timestamp"))
            and (not assigned or coerce_utc_datetime(event.get("timestamp")) >= assigned)
        ]
        if valid:
            candidates.append((lead, valid[0]))

    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "affected_count": len(candidates),
        "phones": [lead.get("phone") for lead, _ in candidates],
    }, ensure_ascii=False, indent=2))
    if not args.apply or not candidates:
        return

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    backup = ROOT / "backups" / f"leads_whatsapp_opened_stuck_new_{stamp}.json"
    backup.write_text(
        json_util.dumps([{"lead": lead, "event": event} for lead, event in candidates],
                        ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    repaired = 0
    for lead, event in candidates:
        occurred = event["timestamp"]
        result = db["leads"].update_one(
            {"_id": lead["_id"]},
            {"$set": {
                "pipeline_stage": "CONTACTED", "stage": "CONTACTED",
                "lifecycle.first_valid_management_at": occurred,
                "lifecycle.first_contact_attempt_at": occurred,
                "sla_status": "fulfilled", "priority_score": 0,
                "priority_bucket": "DONE", "last_crm_update": occurred,
                "state_repair": {
                    "version": "whatsapp_opened_management_v1",
                    "repaired_at": now,
                    "reason": "whatsapp_opened_in_current_assignment",
                },
            }},
        )
        repaired += result.modified_count
        if result.modified_count:
            bump_crm_leads_version(
                db, reason="repair_whatsapp_opened_management_v1", phone=lead.get("phone")
            )
    print(json.dumps({"repaired_count": repaired, "backup": str(backup)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
