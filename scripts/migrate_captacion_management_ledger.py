"""Inventario, backfill idempotente y conciliación del histórico de Captación."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from captacion_management import LEDGER_COLLECTION, LEDGER_VERSION, ensure_management_indexes, management_dedup_key, normalize_result
from captacion_workforce import clean_id, localize
from chatbot.storage import get_db
from config import Config


MIGRATION_VERSION = "captacion_legacy_v1"


def name_key(value):
    return " ".join(str(value or "").casefold().split())


def source_id(property_id, index, activity):
    raw = "|".join(
        [clean_id(property_id), str(index), clean_id(activity.get("timestamp")), clean_id(activity.get("action")), clean_id(activity.get("channel"))]
    )
    return "legacy:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def inventory(db):
    collection = Config.get_captacion_collection(db)
    users = list(db["usuarios"].find({}, {"nombre": 1, "email": 1}))
    users_by_name = {name_key(user.get("nombre")): user for user in users if user.get("nombre")}
    groups = Counter()
    rows = []
    unresolved = Counter()
    for prop in collection.find({"gestion.actividades.0": {"$exists": True}}, {"gestion.actividades": 1}):
        for index, activity in enumerate((prop.get("gestion") or {}).get("actividades") or []):
            groups[(activity.get("action"), activity.get("channel"), activity.get("result"))] += 1
            user = users_by_name.get(name_key(activity.get("user")))
            if not user:
                unresolved[activity.get("user") or "<sin actor>"] += 1
            confirmed_result = None
            try:
                candidate = normalize_result(activity.get("result"))
                if candidate != "cancel":
                    confirmed_result = candidate
            except ValueError:
                pass
            rows.append(
                {
                    "property_id": clean_id(prop.get("_id")),
                    "activity": activity,
                    "source_event_id": source_id(prop.get("_id"), index, activity),
                    "user": user,
                    "confirmed_result": confirmed_result,
                }
            )
    return rows, groups, unresolved


def backup_ledger(db):
    path = Path("backups") / f"captacion_management_events_pre_{MIGRATION_VERSION}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(db[LEDGER_COLLECTION].find({})), default=str, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_event(row):
    activity = row["activity"]
    timestamp = activity.get("timestamp")
    if not isinstance(timestamp, datetime):
        timestamp = datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    user = row.get("user")
    credited = bool(row.get("confirmed_result") and user)
    event = {
        "event_id": "migration-" + row["source_event_id"].split(":", 1)[1][:32],
        "event_type": "management_confirmed" if credited else "legacy_observation",
        "credited": credited,
        "property_id": row["property_id"],
        "actor_user_id": clean_id((user or {}).get("_id")),
        "actor_name_snapshot": activity.get("user") or "",
        "actor_email_snapshot": (user or {}).get("email") or "",
        "action": activity.get("action"),
        "channel": activity.get("channel"),
        "result": row.get("confirmed_result") or activity.get("result"),
        "contact_effective": row.get("confirmed_result") in {"contacted", "callback_requested"},
        "occurred_at": timestamp.astimezone(timezone.utc),
        "local_date": localize(timestamp, "America/Santiago").date().isoformat(),
        "timezone": "America/Santiago",
        "source_event_id": row["source_event_id"],
        "source_system": "gestion.actividades",
        "migration_version": MIGRATION_VERSION,
        "legacy_inferred": True,
        "migrated_at": datetime.now(timezone.utc),
    }
    if credited:
        event["dedup_key"] = management_dedup_key(row["property_id"], event["actor_user_id"], timestamp)
    return event


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    db = get_db()
    ensure_management_indexes(db)
    rows, groups, unresolved = inventory(db)
    events = [build_event(row) for row in rows]
    result = {
        "mode": "apply" if args.apply else "inventory",
        "migration_version": MIGRATION_VERSION,
        "legacy_events": len(rows),
        "creditable_confirmed": sum(1 for event in events if event["credited"]),
        "non_creditable_observations": sum(1 for event in events if not event["credited"]),
        "groups": [{"action": key[0], "channel": key[1], "result": key[2], "count": count} for key, count in groups.items()],
        "unresolved_actors": dict(unresolved),
        "central_crm_events": db["crm_events"].count_documents({"type": "gestion_captacion"}),
        "ledger_before": db[LEDGER_COLLECTION].count_documents({}),
        "cutover_date": "2026-07-20",
        "dual_read_until": "2026-08-02",
    }
    if args.apply:
        result["backup"] = str(backup_ledger(db))
        inserted = 0
        for event in events:
            write = db[LEDGER_COLLECTION].update_one(
                {"source_event_id": event["source_event_id"]}, {"$setOnInsert": event}, upsert=True
            )
            inserted += int(bool(getattr(write, "upserted_id", None)))
        result["inserted"] = inserted
        result["ledger_after"] = db[LEDGER_COLLECTION].count_documents({})
    report_path = Path(args.report) if args.report else Path("reports") / f"captacion_ledger_reconciliation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    print(json.dumps({**result, "report": str(report_path)}, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
