"""Audita y migra decisiones manuales verificables desde ``crm_events``.

El modo por defecto es solo lectura. ``--apply`` crea un respaldo del ledger,
inserta eventos idempotentes y recalcula exclusivamente los días afectados.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from captacion_management import (
    LEDGER_COLLECTION,
    assignment_cycle_id,
    ensure_assignment_cycle,
    ensure_management_indexes,
    evaluate_manual_decision,
    management_dedup_key,
    recalculate_daily_metric,
)
from captacion_workforce import clean_id, localize
from chatbot.storage import get_db
from config import Config


MIGRATION_VERSION = "crm_manual_decisions_v1"
SOURCE_SYSTEM = "crm_events"
TIMEZONE = "America/Santiago"


def parse_timestamp(value) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def property_document(collection, value):
    candidates = [clean_id(value)]
    try:
        candidates.insert(0, ObjectId(clean_id(value)))
    except Exception:
        pass
    return collection.find_one({"_id": {"$in": candidates}})


def assignment_matches(prop: dict, user_id: str, occurred_at: datetime) -> bool:
    gestion = prop.get("gestion") or {}
    assigned_at = parse_timestamp(gestion.get("fecha_asignacion"))
    if clean_id(gestion.get("ejecutivo_id")) == user_id and (not assigned_at or assigned_at <= occurred_at):
        return True
    history = gestion.get("historial_asignaciones") or []
    return any(
        clean_id(row.get("ejecutivo_id")) == user_id
        and (not parse_timestamp(row.get("assigned_at")) or parse_timestamp(row.get("assigned_at")) <= occurred_at)
        for row in history
    )


def source_event_id(event: dict) -> str:
    return f"crm_events:{clean_id(event.get('_id'))}"


def deterministic_event_id(event: dict) -> str:
    digest = hashlib.sha256(source_event_id(event).encode("utf-8")).hexdigest()[:32]
    return f"migration-{digest}"


def audit(db, actor_name: str) -> tuple[dict, list[dict]]:
    user = db["usuarios"].find_one({"nombre": actor_name, "is_active": {"$ne": False}})
    if not user or not clean_id(user.get("_id")):
        raise RuntimeError(f"No existe un usuario activo e inequívoco para {actor_name}")
    user_id = clean_id(user["_id"])
    collection = Config.get_captacion_collection(db)
    raw_events = list(db["crm_events"].find({"actor": actor_name, "meta.source": "captacion"}).sort("timestamp", 1))
    verified = []
    rejected = []
    seen_cycle_decisions = set()

    for event in raw_events:
        reason = None
        meta = event.get("meta") or {}
        if event.get("type") != "stage_change":
            reason = f"non_creditable_event:{event.get('type') or 'unknown'}"
        elif meta.get("automatic") or meta.get("is_automatic"):
            reason = "automatic_change"
        occurred_at = parse_timestamp(event.get("timestamp"))
        prop = property_document(collection, event.get("phone")) if not reason else None
        if not reason and not occurred_at:
            reason = "invalid_timestamp"
        if not reason and not prop:
            reason = "property_not_found"
        if not reason and not assignment_matches(prop, user_id, occurred_at):
            reason = "actor_not_assigned_at_event_time"

        decision = None
        if not reason:
            try:
                decision = evaluate_manual_decision(
                    status=meta.get("new_stage"),
                    previous_status=meta.get("old_stage"),
                    notes=meta.get("notes"),
                    is_automatic=False,
                )
            except ValueError as exc:
                reason = f"insufficient_evidence:{exc}"
            if decision and not decision.get("eligible"):
                reason = decision.get("reason") or "not_creditable"

        if not reason:
            cycle_id = assignment_cycle_id(prop)
            cycle_decision = (clean_id(prop.get("_id")), user_id, cycle_id, decision["result"])
            if decision["result"] == "ready_to_contact" and cycle_decision in seen_cycle_decisions:
                reason = "assignment_cycle_decision_already_seen"
            else:
                seen_cycle_decisions.add(cycle_decision)

        summary = {
            "source_event_id": source_event_id(event),
            "property_id": clean_id((prop or {}).get("_id") or event.get("phone")),
            "occurred_at": occurred_at,
            "old_status": meta.get("old_stage"),
            "new_status": meta.get("new_stage"),
            "notes": meta.get("notes") or "",
        }
        if reason:
            summary["reason"] = reason
            rejected.append(summary)
            continue
        summary.update({"event": event, "property": prop, "decision": decision, "assignment_cycle_id": cycle_id})
        verified.append(summary)

    report = {
        "actor": actor_name,
        "actor_user_id": user_id,
        "source_events": len(raw_events),
        "verifiable": len(verified),
        "non_verifiable": len(rejected),
        "non_verifiable_reasons": dict(Counter(row["reason"] for row in rejected)),
        "verified_events": [
            {key: value for key, value in row.items() if key not in {"event", "property", "decision"}}
            for row in verified
        ],
    }
    return report, verified


def build_ledger_event(row: dict, user: dict) -> dict:
    decision = row["decision"]
    occurred_at = row["occurred_at"]
    property_id = row["property_id"]
    user_id = clean_id(user["_id"])
    event_id = deterministic_event_id(row["event"])
    return {
        "event_id": event_id,
        "event_type": "capture_confirmed" if decision["capture"] else "manual_decision_confirmed",
        "credited": True,
        "dedup_key": management_dedup_key(property_id, user_id, occurred_at),
        "property_id": property_id,
        "assignment_cycle_id": row["assignment_cycle_id"],
        "actor_user_id": user_id,
        "actor_name_snapshot": user.get("nombre") or "",
        "actor_email_snapshot": user.get("email") or "",
        "action": "manual_decision",
        "channel": "manual",
        "result": decision["result"],
        "status_snapshot": row["new_status"] or "",
        "previous_status_snapshot": row["old_status"] or "",
        "notes": row["notes"],
        "contact_attempt": decision["contact_attempt"],
        "contact_effective": decision["contact_effective"],
        "occurred_at": occurred_at.astimezone(timezone.utc),
        "local_date": localize(occurred_at, TIMEZONE).date().isoformat(),
        "timezone": TIMEZONE,
        "source_event_id": row["source_event_id"],
        "source_system": SOURCE_SYSTEM,
        "migration_version": MIGRATION_VERSION,
        "legacy_inferred": True,
        "migrated_at": datetime.now(timezone.utc),
    }


def backup_ledger(db) -> Path:
    path = Path("backups") / f"captacion_management_events_pre_{MIGRATION_VERSION}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(list(db[LEDGER_COLLECTION].find({})), default=str, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", default="Susana Ensignia")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    db = get_db()
    ensure_management_indexes(db)
    report, verified = audit(db, args.actor)
    report.update({"mode": "apply" if args.apply else "audit", "migration_version": MIGRATION_VERSION})

    if args.apply:
        report["backup"] = str(backup_ledger(db))
        user = db["usuarios"].find_one({"_id": ObjectId(report["actor_user_id"])})
        inserted = 0
        already_migrated = 0
        daily_deduplicated = 0
        affected_days = set()
        for row in verified:
            existing_source = db[LEDGER_COLLECTION].find_one({"source_event_id": row["source_event_id"]})
            if existing_source:
                already_migrated += 1
                continue
            event = build_ledger_event(row, user)
            if db[LEDGER_COLLECTION].find_one({"dedup_key": event["dedup_key"], "credited": True}):
                daily_deduplicated += 1
                continue
            event["assignment_cycle_id"] = ensure_assignment_cycle(db, row["property"])
            write = db[LEDGER_COLLECTION].update_one(
                {"source_event_id": event["source_event_id"]}, {"$setOnInsert": event}, upsert=True
            )
            if getattr(write, "upserted_id", None):
                inserted += 1
                affected_days.add(event["local_date"])
        for local_day in sorted(affected_days):
            recalculate_daily_metric(db, report["actor_user_id"], local_day)
        report.update({
            "inserted": inserted,
            "already_migrated": already_migrated,
            "daily_deduplicated": daily_deduplicated,
            "recalculated_days": sorted(affected_days),
        })

    report_path = Path(args.report) if args.report else Path("reports") / f"captacion_manual_decisions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    print(json.dumps({**report, "report": str(report_path)}, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
