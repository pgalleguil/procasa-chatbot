"""Repair legacy CRM management events that never stopped the assignment SLA.

The old detail form stored valid owner-management results as ``HUMAN_NOTE``
events but did not populate the active assignment cycle.  This script is
read-only by default.  ``--apply`` writes only candidates with an active cycle
whose canonical first-management timestamp is still missing, after creating a
JSON backup of the exact documents it will touch.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")

from chatbot.crm_metrics import coerce_utc_datetime, normalize_result  # noqa: E402
from chatbot.storage import get_db  # noqa: E402


LEGACY_EVENT_TYPES = ("HUMAN_NOTE", "GESTION_LOG", "MANUAL_ENTRY")
MANAGEMENT_RESULTS = {
    "CALL_NO_ANSWER": {"effective": False, "status": "managed_waiting_response"},
    "EFFECTIVE_CONTACT": {"effective": True, "status": "managed_contacted"},
    "FOLLOW_UP_REQUESTED": {"effective": True, "status": "managed_follow_up"},
    "INVALID_NUMBER": {"effective": False, "status": "managed_closed"},
    "MESSAGE_SENT_WAITING_RESPONSE": {"effective": False, "status": "managed_waiting_response"},
    "EMAIL_SENT": {"effective": False, "status": "managed_waiting_response"},
    "OTHER_EXPLICIT": {"effective": True, "status": "managed_contacted"},
    "CONTACTADO": {"effective": True, "status": "managed_contacted"},
    "SOLICITA_SEGUIMIENTO": {"effective": True, "status": "managed_follow_up"},
    "NO_INTERESADO": {"effective": True, "status": "managed_closed"},
    "OTRO": {"effective": True, "status": "managed_contacted"},
}


def _key(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def _missing_management_query() -> dict:
    return {"$or": [
        {"first_valid_management_at": {"$exists": False}},
        {"first_valid_management_at": None},
    ]}


def _candidate_summary(candidate: dict) -> dict:
    return {
        "lead_id": str(candidate["lead"].get("_id")),
        "phone": candidate["lead"].get("phone"),
        "assignment_cycle_id": candidate["cycle"].get("assignment_cycle_id"),
        "event_id": str(candidate["event"].get("_id")),
        "event_timestamp": str(candidate["occurred_at"]),
        "legacy_result": candidate["raw_result"],
        "result_type": candidate["result_type"],
        "actor_user_id": candidate["actor_user_id"],
    }


def find_candidates(db) -> list[dict]:
    cycles = list(db["crm_assignment_cycles"].find({
        "cycle_status": "active", "unassigned_at": None, **_missing_management_query(),
    }))
    if not cycles:
        return []

    cycle_by_id = {
        str(cycle.get("assignment_cycle_id")): cycle
        for cycle in cycles if cycle.get("assignment_cycle_id")
    }
    lead_ids = [cycle.get("lead_id") for cycle in cycles if cycle.get("lead_id") is not None]
    leads = {lead.get("_id"): lead for lead in db["leads"].find({"_id": {"$in": lead_ids}})}
    users = list(db["usuarios"].find({}, {"_id": 1, "nombre": 1}))
    users_by_id = {str(user.get("_id")): user for user in users}
    users_by_name = {_key(user.get("nombre")): str(user.get("_id")) for user in users if user.get("nombre")}

    events = db["crm_events"].find({
        "lead_id": {"$in": lead_ids},
        "assignment_cycle_id": {"$in": list(cycle_by_id)},
        "type": {"$in": list(LEGACY_EVENT_TYPES)},
        "confirmed": True,
    }).sort("timestamp", 1)

    candidates_by_cycle: dict[str, dict] = {}
    for event in events:
        cycle_id = str(event.get("assignment_cycle_id") or "")
        cycle = cycle_by_id.get(cycle_id)
        lead = leads.get(event.get("lead_id"))
        if not cycle or not lead:
            continue
        actor = str(event.get("actor_user_id") or (event.get("meta") or {}).get("actor_user_id") or "").strip()
        if actor not in users_by_id:
            actor = users_by_name.get(_key(event.get("actor")), "")
        if not actor:
            continue
        occurred_at = coerce_utc_datetime(event.get("timestamp") or event.get("occurred_at"))
        if not occurred_at:
            continue
        meta = event.get("meta") or event.get("metadata") or {}
        raw_result = event.get("result") or meta.get("result") or meta.get("contact_result")
        result_type = normalize_result(raw_result)
        if result_type not in MANAGEMENT_RESULTS:
            continue
        candidate = {
            "lead": lead, "cycle": cycle, "event": event,
            "actor_user_id": actor, "occurred_at": occurred_at,
            "raw_result": str(raw_result), "result_type": result_type,
        }
        previous = candidates_by_cycle.get(cycle_id)
        if previous is None or occurred_at < previous["occurred_at"]:
            candidates_by_cycle[cycle_id] = candidate
    return list(candidates_by_cycle.values())


def _backup(db, candidates: list[dict]) -> str:
    backup_dir = Path.cwd() / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"crm_legacy_management_sla_{stamp}.json"
    payload = {"created_at": datetime.now(timezone.utc), "candidates": []}
    for candidate in candidates:
        cycle_id = candidate["cycle"].get("assignment_cycle_id")
        notifications = list(db["crm_notifications_v1"].find(
            {"assignment_cycle_id": cycle_id, "notification_type": {"$in": ["sla_yellow", "sla_red"]}}
        ))
        existing_result = db["crm_management_results"].find_one({
            "lead_id": candidate["lead"].get("_id"),
            "assignment_cycle_id": cycle_id,
        })
        payload["candidates"].append({
            "summary": _candidate_summary(candidate),
            "lead": candidate["lead"],
            "cycle": candidate["cycle"],
            "event": candidate["event"],
            "existing_result": existing_result,
            "notifications": notifications,
        })
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    return str(path)


def apply_candidates(db, candidates: list[dict]) -> dict:
    backup_path = _backup(db, candidates)
    applied = []
    for candidate in candidates:
        lead = candidate["lead"]
        cycle = candidate["cycle"]
        event = candidate["event"]
        result_type = candidate["result_type"]
        rule = MANAGEMENT_RESULTS[result_type]
        occurred = candidate["occurred_at"]
        legacy_event_id = str(event["_id"])
        idempotency_key = f"legacy-sla:{legacy_event_id}"
        canonical_event_id = f"crm_event:{idempotency_key}"
        meta = event.get("meta") or event.get("metadata") or {}
        details = dict(meta.get("details_json") or {})
        if meta.get("notes") and "notes" not in details:
            details["notes"] = meta["notes"]
        details.update({"legacy_event_id": legacy_event_id, "legacy_result": candidate["raw_result"]})

        result_doc = {
            "_id": f"crm_management:{idempotency_key}",
            "idempotency_key": idempotency_key,
            "management_request_id": idempotency_key,
            "schema_version": "crm_management_result_v1",
            "lead_id": lead["_id"],
            "assignment_cycle_id": cycle["assignment_cycle_id"],
            "actor_user_id": candidate["actor_user_id"],
            "result_type": result_type,
            "occurred_at": occurred,
            "source": "legacy_management_sla_backfill",
            "details_json": details,
            "pipeline_stage_at_result": str(lead.get("pipeline_stage") or "CONTACTED"),
            "legacy_stage_at_result": str(lead.get("stage") or "gestion"),
            "status": "completed",
            "follow_up_required": False,
            "legacy_event_id": legacy_event_id,
        }
        db["crm_management_results"].update_one(
            {"_id": result_doc["_id"]}, {"$setOnInsert": result_doc}, upsert=True,
        )
        canonical_event = {
            "_id": canonical_event_id,
            "lead_id": lead["_id"],
            "phone": lead.get("phone"),
            "assignment_cycle_id": cycle["assignment_cycle_id"],
            "actor": candidate["actor_user_id"],
            "actor_type": "human",
            "type": "CONTACT_RESULT",
            "result": result_type,
            "confirmed": True,
            "timestamp": occurred,
            "source": "legacy_management_sla_backfill",
            "idempotency_key": idempotency_key,
            "management_request_id": idempotency_key,
            "meta": details,
            "legacy_event_id": legacy_event_id,
        }
        db["crm_events"].update_one(
            {"_id": canonical_event_id}, {"$setOnInsert": canonical_event}, upsert=True,
        )

        lifecycle = lead.get("lifecycle") or {}
        lead_updates = {
            "lifecycle.first_valid_management_at": occurred,
            "lifecycle.first_valid_management_actor": candidate["actor_user_id"],
            "lifecycle.first_valid_management_event_id": canonical_event_id,
            "lifecycle.first_valid_management_event_type": "CONTACT_RESULT",
            "lifecycle.first_valid_management_normalized_at": occurred,
            "management_status": rule["status"],
            "contact_attempted": True,
            "effective_contact": rule["effective"],
        }
        if not lifecycle.get("first_contact_attempt_at"):
            lead_updates["lifecycle.first_contact_attempt_at"] = occurred
        if rule["effective"] and not lifecycle.get("first_effective_contact_at"):
            lead_updates["lifecycle.first_effective_contact_at"] = occurred
        db["leads"].update_one(
            {"_id": lead["_id"], **_missing_management_query()}, {"$set": lead_updates}
        )
        cycle_updates = {
            "first_valid_management_at": occurred,
            "first_valid_management_actor": candidate["actor_user_id"],
            "first_contact_attempt_at": occurred,
            "sla_first_management_status": "completed",
            "sla_pending_alerts_cancelled_at": occurred,
            "last_management_result": result_type,
            "sla_alert_claims.yellow.status": "suppressed",
            "sla_alert_claims.red.status": "suppressed",
        }
        if rule["effective"]:
            cycle_updates["first_effective_contact_at"] = occurred
        db["crm_assignment_cycles"].update_one(
            {"assignment_cycle_id": cycle["assignment_cycle_id"], "cycle_status": "active", **_missing_management_query()},
            {"$set": cycle_updates},
        )
        db["crm_notifications_v1"].update_many(
            {"assignment_cycle_id": cycle["assignment_cycle_id"],
             "notification_type": {"$in": ["sla_yellow", "sla_red"]},
             "state": {"$in": ["pending", "failed_retryable"]}},
            {"$set": {"state": "suppressed", "suppressed_reason": "management_completed", "updated_at": occurred}},
        )
        applied.append(_candidate_summary(candidate))
    return {"backup": backup_path, "applied": applied, "count": len(applied)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed repair; dry-run is the default")
    args = parser.parse_args()
    db = get_db()
    candidates = find_candidates(db)
    report = {"mode": "apply" if args.apply else "dry_run", "count": len(candidates),
              "candidates": [_candidate_summary(candidate) for candidate in candidates]}
    if args.apply and candidates:
        report.update(apply_candidates(db, candidates))
    print(json.dumps(report, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
