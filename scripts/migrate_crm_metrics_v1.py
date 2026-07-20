"""Idempotent CRM timestamp/identity migration. Dry-run unless --apply is given."""
from __future__ import annotations

import argparse
from collections import Counter

from chatbot.crm_metrics import coerce_utc_datetime, normalize_phone
from chatbot.storage import get_db


def migrate(db, *, apply=False):
    counters = Counter()
    leads_by_phone = {}
    for lead in db["leads"].find({}, {"_id": 1, "phone": 1}):
        phone = normalize_phone(lead.get("phone"))
        if phone:
            leads_by_phone.setdefault(phone, []).append(lead["_id"])
    for event in db["crm_events"].find({}):
        changes = {}
        parsed = coerce_utc_datetime(event.get("timestamp"))
        if parsed:
            if event.get("timestamp") != parsed:
                changes["timestamp"] = parsed
                counters["timestamps_parseable"] += 1
        else:
            counters["timestamps_invalid"] += 1
        if event.get("lead_id") is None:
            phone = normalize_phone(event.get("phone"))
            candidates = leads_by_phone.get(phone, [])
            if len(candidates) == 1:
                changes["lead_id"] = candidates[0]
                changes["identity_status"] = "resolved_legacy_phone"
                counters["identity_resolved"] += 1
            elif len(candidates) > 1:
                counters["identity_ambiguous"] += 1
            else:
                counters["identity_missing"] += 1
        if changes and apply:
            db["crm_events"].update_one({"_id": event["_id"]}, {"$set": changes})
            counters["events_updated"] += 1
    counters["mode_apply"] = int(apply)
    return dict(counters)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(migrate(get_db(), apply=args.apply))
