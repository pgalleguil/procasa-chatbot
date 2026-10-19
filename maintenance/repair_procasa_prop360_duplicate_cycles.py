#!/usr/bin/env python3
"""Repair one individually reviewed pre-PR-48 Prop360 duplicate cycle.

The command is fail-closed. Capture a fresh backup, create a checksummed
preflight, then execute one MongoDB transaction with an explicit incident
confirmation. The MongoDB URI is read only from managed runtime secrets.
No customer or executive messages are sent by this command.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

from bson import ObjectId, json_util
from pymongo import MongoClient, ReadPreference
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern


DATABASE = "URLS"
BACKUP_FORMAT = "procasa-prop360-duplicate-cycle-v1"
PREFLIGHT_FORMAT = "procasa-prop360-duplicate-cycle-preflight-v1"
PRODUCTION_CLUSTER_PREFIX = "ac-8f1k7c3-shard-"
PRODUCTION_CLUSTER_SUFFIX = ".mzve39k.mongodb.net"

CASES: dict[str, dict[str, Any]] = {
    "17252-other": {
        "incident_id": "PROC-2026-10-P0-17252-SECONDARY-DUPLICATE",
        "confirmation": "P0-17252-OTHER-DUPLICATE",
        "lead_id": "6ad4e3f40035d45d0f875791",
        "property_code": "17252",
        "owner_name": "Mariela Arriagada",
        "owner_user_id": "6989c6309dd2ba54e478196c",
        "original_cycle_id": "da0d5a16-31ca-466e-b8b6-4aeb9692c2dd",
        "duplicate_cycle_id": "54d3b404-b26d-4ef8-9fd2-29bc2626e061",
        "prop360_event_id": "12230",
        "management": {
            "crm_management:55489cbb-16ec-451b-9ea7-39706a733701": {
                "cycle_id": "da0d5a16-31ca-466e-b8b6-4aeb9692c2dd",
                "result_type": "EFFECTIVE_CONTACT",
                "occurred_at": datetime(2026, 10, 18, 18, 10, 20, 248000, tzinfo=timezone.utc),
            },
            "crm_management:b7dd9b04-768c-43b9-ac1f-5543fc488074": {
                "cycle_id": "54d3b404-b26d-4ef8-9fd2-29bc2626e061",
                "result_type": "EFFECTIVE_CONTACT",
                "occurred_at": datetime(2026, 10, 19, 12, 17, 10, 752000, tzinfo=timezone.utc),
            },
        },
    },
    "5806": {
        "incident_id": "PROC-2026-10-P0-5806-DUPLICATE-CYCLE",
        "confirmation": "P0-5806-DUPLICATE-CYCLE",
        "lead_id": "6ad416270035d45d0f875640",
        "property_code": "5806",
        "owner_name": "Rocío Aliaga",
        "owner_user_id": "6995e9be6a099d6e9612d120",
        "original_cycle_id": "14e23261-1608-4329-9634-f6f6ebffa710",
        "duplicate_cycle_id": "9ec2f41c-2007-4130-914f-068617d92ddc",
        "prop360_event_id": "12209",
        "management": {
            "crm_management:a9429442-b991-42d2-b6fc-24d80867b851": {
                "cycle_id": "14e23261-1608-4329-9634-f6f6ebffa710",
                "result_type": "FOLLOW_UP_REQUESTED",
                "occurred_at": datetime(2026, 10, 18, 0, 52, 31, 882000, tzinfo=timezone.utc),
            },
            "crm_management:63c865bc-881b-4329-bcd4-f849a7a0dd46": {
                "cycle_id": "14e23261-1608-4329-9634-f6f6ebffa710",
                "result_type": "FOLLOW_UP_REQUESTED",
                "occurred_at": datetime(2026, 10, 18, 0, 53, 58, 708000, tzinfo=timezone.utc),
            },
            "crm_management:5a569ab8-a48d-4680-ae26-10f55e733468": {
                "cycle_id": "9ec2f41c-2007-4130-914f-068617d92ddc",
                "result_type": "FOLLOW_UP_REQUESTED",
                "occurred_at": datetime(2026, 10, 19, 13, 9, 33, 898000, tzinfo=timezone.utc),
            },
            "crm_management:c20a2a31-2845-420a-983c-5f440e92974e": {
                "cycle_id": "9ec2f41c-2007-4130-914f-068617d92ddc",
                "result_type": "FOLLOW_UP_REQUESTED",
                "occurred_at": datetime(2026, 10, 19, 13, 25, 43, 812000, tzinfo=timezone.utc),
            },
        },
    },
}

COLLECTIONS = {
    "lead": "leads",
    "cycles": "crm_assignment_cycles",
    "management": "crm_management_results",
    "events": "crm_events",
    "alerts": "crm_assignment_integrity_alerts",
    "sla_alerts": "crm_sla_alerts_v1",
    "notifications": "crm_notifications_v1",
    "pending_notifications": "pending_notifications",
    "active_jobs": "chatbot_inbound_jobs",
    "ingest": "lead_ingest_events",
    "tasks": "crm_tasks",
    "visits": "visitas",
    "visit_orders": "ordenes_visitas",
}


class RepairSafetyError(RuntimeError):
    """Live state differs from the individually reviewed incident evidence."""


def _utc(value: Any) -> datetime | None:
    if isinstance(value, Mapping) and "$date" in value:
        return _utc(value["$date"])
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _canonical(value: Any) -> str:
    return json_util.dumps(value, json_options=json_util.CANONICAL_JSON_OPTIONS,
                           sort_keys=True, separators=(",", ":"))


def _fingerprint(rows: list[Mapping[str, Any]]) -> str:
    material = "\n".join(sorted(_canonical(dict(row)) for row in rows))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _manifest_snapshot(snapshot: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, Any]:
    return {key: {"count": len(rows), "sha256": _fingerprint(list(rows))}
            for key, rows in sorted(snapshot.items())}


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    content = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any], *, bson: bool = False) -> str:
    if path.exists():
        raise RepairSafetyError(f"output already exists: {path.name}; choose a new path")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    data = (json_util.dumps(payload, json_options=json_util.CANONICAL_JSON_OPTIONS, indent=2) + "\n"
            if bson else json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")
    temporary.write_text(data, encoding="utf-8")
    os.replace(temporary, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_backup(path: Path, case_name: str) -> dict[str, Any]:
    try:
        backup = json_util.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RepairSafetyError(f"backup cannot be parsed ({type(exc).__name__})") from exc
    case = CASES[case_name]
    if (backup.get("format") != BACKUP_FORMAT or backup.get("database") != DATABASE
            or backup.get("case") != case_name or str(backup.get("lead_id")) != case["lead_id"]):
        raise RepairSafetyError("backup format, database, or incident identity does not match")
    for key, collection in COLLECTIONS.items():
        bundle = (backup.get("collections") or {}).get(key)
        if not isinstance(bundle, Mapping) or bundle.get("collection") != collection:
            raise RepairSafetyError(f"backup is missing collection {key}")
        if not isinstance(bundle.get("documents"), list):
            raise RepairSafetyError(f"backup collection {key} is malformed")
    return backup


def _backup_docs(backup: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    return list(backup["collections"][key]["documents"])


def _find(db: Any, collection: str, query: Mapping[str, Any], session: Any = None) -> list[dict[str, Any]]:
    options = {"session": session} if session is not None else {}
    return list(db[collection].find(dict(query), **options).limit(10000))


def _collect_snapshot(db: Any, case: Mapping[str, Any], session: Any = None) -> dict[str, list[dict[str, Any]]]:
    lead_oid = ObjectId(case["lead_id"])
    lead_variants = [lead_oid, case["lead_id"]]
    options = {"session": session} if session is not None else {}
    lead = db["leads"].find_one({"_id": lead_oid}, **options)
    if not lead:
        raise RepairSafetyError("reviewed lead is absent from production")
    phone = lead.get("phone")
    cycle_ids = [case["original_cycle_id"], case["duplicate_cycle_id"]]
    lead_or_cycle = {"$or": [
        {"lead_id": {"$in": lead_variants}},
        {"assignment_cycle_id": {"$in": cycle_ids}},
    ]}
    return {
        "lead": [lead],
        "cycles": _find(db, "crm_assignment_cycles", {"lead_id": {"$in": lead_variants}}, session),
        "management": _find(db, "crm_management_results", {"$or": [
            {"lead_id": {"$in": lead_variants}}, {"assignment_cycle_id": {"$in": cycle_ids}},
        ]}, session),
        "events": _find(db, "crm_events", {"lead_id": {"$in": lead_variants}}, session),
        "alerts": _find(db, "crm_assignment_integrity_alerts", {"lead_id": {"$in": lead_variants}}, session),
        "sla_alerts": _find(db, "crm_sla_alerts_v1", {"assignment_cycle_id": {"$in": cycle_ids}}, session),
        "notifications": _find(db, "crm_notifications_v1", {"$or": [
            {"assignment_cycle_id": {"$in": cycle_ids}},
            {"assignment_cycle_ids": {"$in": cycle_ids}},
            {"lead_id": {"$in": lead_variants}},
            {"lead_ids": {"$in": lead_variants}},
        ]}, session),
        "pending_notifications": _find(db, "pending_notifications", {"$or": [
            {"lead_id": {"$in": lead_variants}}, {"phone": phone},
            {"telefono": phone}, {"lead_phone": phone},
        ]}, session),
        "active_jobs": _find(db, "chatbot_inbound_jobs", {"$and": [
            {"state": {"$in": ["received", "batching", "pending", "processing"]}},
            {"$or": [{"lead_id": {"$in": lead_variants}}, {"phone": phone}]},
        ]}, session),
        "ingest": _find(db, "lead_ingest_events", {"source_event_id": str(case["prop360_event_id"])}, session),
        "tasks": _find(db, "crm_tasks", lead_or_cycle, session),
        "visits": _find(db, "visitas", lead_or_cycle, session),
        "visit_orders": _find(db, "ordenes_visitas", lead_or_cycle, session),
    }


def _build_backup(case_name: str, snapshot: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, Any]:
    return {
        "format": BACKUP_FORMAT,
        "case": case_name,
        "incident_id": CASES[case_name]["incident_id"],
        "database": DATABASE,
        "lead_id": CASES[case_name]["lead_id"],
        "created_at": datetime.now(timezone.utc),
        "collections": {
            key: {"collection": COLLECTIONS[key], "documents": list(snapshot[key])}
            for key in COLLECTIONS
        },
    }


def _provider_acceptance(notifications: list[Mapping[str, Any]], case: Mapping[str, Any],
                         duplicate_assigned_at: Any) -> dict[str, Any]:
    """Require a provider-accepted original assignment notice before the duplicate cycle."""
    accepted: list[dict[str, Any]] = []
    duplicate_at = _utc(duplicate_assigned_at)
    for notification in notifications:
        cycles = {str(value) for value in (notification.get("assignment_cycle_ids") or [])}
        leads = {str(value) for value in (notification.get("lead_ids") or [])}
        if case["original_cycle_id"] not in cycles or case["lead_id"] not in leads:
            continue
        message_id = str(notification.get("provider_message_id") or "").strip()
        if notification.get("state") != "sent" or not message_id:
            continue
        for attempt in notification.get("delivery_attempts") or []:
            completed_at = _utc(attempt.get("completed_at"))
            status = attempt.get("provider_http_status")
            if (completed_at and duplicate_at and completed_at < duplicate_at
                    and isinstance(status, int) and 200 <= status < 300
                    and attempt.get("result") == "sent"
                    and str(attempt.get("provider_message_id") or "").strip() == message_id):
                accepted.append({
                    "notification_id": str(notification.get("_id")),
                    "provider_message_id": message_id,
                    "provider_http_status": status,
                    "accepted_at": completed_at,
                    "assignment_cycle_id": case["original_cycle_id"],
                    "lead_id": case["lead_id"],
                    "timestamp_source": "crm_notifications_v1.delivery_attempts.completed_at_after_provider_2xx",
                })
    unique = {(row["notification_id"], row["provider_message_id"], row["accepted_at"]): row
              for row in accepted}
    if len(unique) != 1:
        raise RepairSafetyError("expected exactly one provider-accepted original assignment WhatsApp before Prop360 duplicate; SLA start will not be guessed")
    return next(iter(unique.values()))


def _open_pointer_alert(row: Mapping[str, Any], case: Mapping[str, Any]) -> bool:
    if row.get("status") != "open" or row.get("issue_code") not in {
        "chatbot_customer_message_assignment_conflict",
        "commercial_intake_blocked_by_assignment_conflict",
        "canonical_cycle_mirror_inconsistency",
        "assignment_cycle_pointer_mismatch",
    }:
        return False
    evidence = row.get("evidence") or {}
    issues = set(evidence.get("issues") or [])
    linked = {str(value) for value in evidence.get("active_cycle_ids") or []}
    linked.update(str(evidence[key]) for key in ("cycle_id", "assignment_cycle_id") if evidence.get(key))
    return "cycle_pointer_mismatch" in issues and bool(
        linked & {case["original_cycle_id"], case["duplicate_cycle_id"]}
    )


def _validate_scenario(case_name: str, snapshot: Mapping[str, list[Mapping[str, Any]]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    case = CASES[case_name]
    if len(snapshot["lead"]) != 1:
        raise RepairSafetyError("live lead is missing or ambiguous")
    lead = snapshot["lead"][0]
    if (str(lead.get("_id")) != case["lead_id"]
            or lead.get("ejecutivo_asignado") != case["owner_name"]
            or (lead.get("prospecto") or {}).get("ejecutivo") != case["owner_name"]
            or (lead.get("prospecto") or {}).get("codigo") != case["property_code"]):
        raise RepairSafetyError("lead identity, property, or owner differs from the reviewed case")
    lifecycle = lead.get("lifecycle") or {}
    if (lifecycle.get("current_assignment_cycle_id") != case["duplicate_cycle_id"]
            or lifecycle.get("assignment_cycle_id") != case["duplicate_cycle_id"]
            or lifecycle.get("assigned_to_user_id") != case["owner_user_id"]):
        raise RepairSafetyError("live canonical pointers or owner differ from reviewed preflight")
    if lead.get("sla_status") != "fulfilled":
        raise RepairSafetyError("lead SLA is not already fulfilled; refusing to change its status")

    cycles = {str(row.get("assignment_cycle_id")): row for row in snapshot["cycles"]}
    expected_cycles = {case["original_cycle_id"], case["duplicate_cycle_id"]}
    if len(snapshot["cycles"]) != 2 or set(cycles) != expected_cycles:
        raise RepairSafetyError("cycle history changed; expected exactly the individually reviewed two cycles")
    original, duplicate = cycles[case["original_cycle_id"]], cycles[case["duplicate_cycle_id"]]
    if (original.get("assigned_to_user_id") != case["owner_user_id"]
            or duplicate.get("assigned_to_user_id") != case["owner_user_id"]
            or original.get("assigned_to_display_name") != case["owner_name"]
            or duplicate.get("assigned_to_display_name") != case["owner_name"]
            or original.get("cycle_status") != "closed"
            or original.get("unassigned_at") is None
            or duplicate.get("cycle_status") != "active"
            or duplicate.get("unassigned_at") is not None
            or duplicate.get("source_system") != "prop360"
            or str(duplicate.get("source_event_id")) != case["prop360_event_id"]
            or str(original.get("property_code")) != case["property_code"]
            or str(duplicate.get("property_code")) != case["property_code"]):
        raise RepairSafetyError("original/Prop360 duplicate cycle facts no longer match the reviewed case")
    if _utc(original.get("unassigned_at")) != _utc(duplicate.get("assigned_at")):
        raise RepairSafetyError("original cycle close is not temporally tied to the Prop360 cycle creation")

    management = {str(row.get("_id")): row for row in snapshot["management"]}
    expected_management = case["management"]
    if set(management) != set(expected_management):
        raise RepairSafetyError("management history changed; refusing to modify or discard human records")
    for record_id, expected in expected_management.items():
        record = management[record_id]
        if (record.get("assignment_cycle_id") != expected["cycle_id"]
                or record.get("result_type") != expected["result_type"]
                or record.get("actor_user_id") != case["owner_user_id"]
                or record.get("status") != "completed"
                or _utc(record.get("occurred_at")) != expected["occurred_at"]):
            raise RepairSafetyError(f"reviewed human management evidence changed: {record_id}")
    original_results = [expected for expected in expected_management.values()
                        if expected["cycle_id"] == case["original_cycle_id"]]
    duplicate_results = [expected for expected in expected_management.values()
                         if expected["cycle_id"] == case["duplicate_cycle_id"]]
    original_first = min(row["occurred_at"] for row in original_results)
    duplicate_first = min(row["occurred_at"] for row in duplicate_results)
    if (_utc(original.get("first_valid_management_at")) != original_first
            or _utc(original.get("first_effective_contact_at")) != original_first
            or original.get("sla_first_management_status") != "completed"
            or _utc(duplicate.get("first_valid_management_at")) != duplicate_first
            or duplicate.get("sla_first_management_status") != "completed"):
        raise RepairSafetyError("cycle management markers do not agree with recorded human results")

    if (str(lead.get("lead_temperature_effective") or "").upper() != "COLD"
            or str(original.get("temperature_at_assignment") or "").upper() != "COLD"):
        raise RepairSafetyError("temperature is not the audited COLD / non-HOT scenario")
    segments = original.get("sla_segments") or []
    if (len(segments) != 1 or segments[0].get("policy") != "NON_HOT"
            or segments[0].get("segment_end") is not None
            or _utc(segments[0].get("segment_start")) != _utc(original.get("sla_started_at"))):
        raise RepairSafetyError("original SLA segment differs from the reviewed single non-HOT segment")

    ingest = [row for row in snapshot["ingest"]
              if str(row.get("source_event_id")) == case["prop360_event_id"]]
    if len(ingest) != 1 or str(ingest[0].get("lead_id")) not in {case["lead_id"], str(lead.get("_id"))}:
        raise RepairSafetyError("Prop360 source event is absent or maps to another lead")
    if not any(str(row.get("property_code") or "") == case["property_code"]
               for row in ingest):
        raise RepairSafetyError("Prop360 source event property differs from the reviewed lead")

    for alert in snapshot["alerts"]:
        if alert.get("status") == "open" and not _open_pointer_alert(alert, case):
            raise RepairSafetyError("an unrelated active assignment alert requires separate review")
    if snapshot["sla_alerts"]:
        raise RepairSafetyError("an SLA alert exists for these cycles and needs individual review before repair")
    if snapshot["pending_notifications"] or snapshot["active_jobs"]:
        raise RepairSafetyError("pending notification or inbound job exists for this lead/phone; refusing a race with a live worker")

    accepted = _provider_acceptance(snapshot["notifications"], case, duplicate.get("assigned_at"))
    return lead, original, duplicate, accepted


def _write_backup(path: Path, payload: Mapping[str, Any]) -> str:
    return _write_json(path, payload, bson=True)


def _load_manifest(path: Path, backup_sha256: str, case_name: str, actor: str) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RepairSafetyError(f"preflight cannot be parsed ({type(exc).__name__})") from exc
    case = CASES[case_name]
    if (manifest.get("manifest_sha256") != _manifest_digest(manifest)
            or manifest.get("format") != PREFLIGHT_FORMAT
            or manifest.get("case") != case_name
            or manifest.get("incident_id") != case["incident_id"]
            or manifest.get("database") != DATABASE
            or manifest.get("backup_sha256") != backup_sha256
            or manifest.get("actor") != actor):
        raise RepairSafetyError("preflight checksum, incident, backup, database, or actor does not match")
    return manifest


def _cycle_filter(cycle: Mapping[str, Any]) -> dict[str, Any]:
    selector = {
        "_id": cycle["_id"],
        "assignment_cycle_id": cycle["assignment_cycle_id"],
        "lead_id": cycle["lead_id"],
        "assigned_to_user_id": cycle["assigned_to_user_id"],
        "assigned_at": cycle["assigned_at"],
        "sla_started_at": cycle["sla_started_at"],
        "cycle_status": cycle.get("cycle_status"),
        "unassigned_at": cycle.get("unassigned_at"),
    }
    selector["cycle_version"] = cycle.get("cycle_version") if "cycle_version" in cycle else {"$exists": False}
    return selector


def _require_one(result: Any, label: str) -> None:
    if getattr(result, "matched_count", 0) != 1:
        raise RepairSafetyError(f"concurrency check failed at {label}; transaction will abort")


def _verify_applied(db: Any, case_name: str, backup: Mapping[str, Any], session: Any = None,
                    *, diagnostic: bool = False) -> dict[str, Any] | None:
    case = CASES[case_name]
    snapshot = _collect_snapshot(db, case, session)
    lead = snapshot["lead"][0] if len(snapshot["lead"]) == 1 else None
    if not lead:
        return None
    original_id, duplicate_id = case["original_cycle_id"], case["duplicate_cycle_id"]
    cycles = {str(row.get("assignment_cycle_id")): row for row in snapshot["cycles"]}
    original, duplicate = cycles.get(original_id), cycles.get(duplicate_id)
    audit_id = f"crm_event:{case['incident_id']}:duplicate-cycle-repair:v1"
    options = {"session": session} if session is not None else {}
    audit = db["crm_events"].find_one({"_id": audit_id}, **options)
    lifecycle = lead.get("lifecycle") or {}
    notifications = _backup_docs(backup, "notifications")
    sla_start = _provider_acceptance(notifications, case, duplicate.get("assigned_at") if duplicate else None)
    accepted_at = sla_start["accepted_at"]
    management_backup = _backup_docs(backup, "management")
    events_without_audit = [row for row in snapshot["events"] if str(row.get("_id")) != audit_id]
    backup_events = _backup_docs(backup, "events")
    alerts_open = [row for row in snapshot["alerts"] if row.get("status") == "open"
                   and "cycle_pointer_mismatch" in ((row.get("evidence") or {}).get("issues") or [])]
    pointer_context = None
    try:
        from chatbot.crm_assignment_integrity import assignment_context
        pointer_context = assignment_context(db, lead)
    except Exception as exc:
        if diagnostic:
            raise RepairSafetyError(f"postcondition chatbot owner lookup failed ({type(exc).__name__})") from exc
    audit_meta = (audit or {}).get("meta") or {}
    try:
        target_expected = {str(row.get("_id")): row for row in management_backup}
        target_current = {str(row.get("_id")): row for row in snapshot["management"]}
        management_preserved = set(target_current) == set(target_expected) and _fingerprint(
            list(target_current.values())
        ) == _fingerprint(list(target_expected.values()))
    except Exception:
        management_preserved = False
    expected_tasks = {key: _backup_docs(backup, key) for key in ("tasks", "visits", "visit_orders")}
    unchanged_operational_records = all(
        _fingerprint(snapshot[key]) == _fingerprint(expected_tasks[key]) for key in expected_tasks
    )
    notification_history_unchanged = _fingerprint(snapshot["notifications"]) == _fingerprint(
        _backup_docs(backup, "notifications")
    )
    expected_start = accepted_at
    checks = {
        "two_cycles_retained": len(snapshot["cycles"]) == 2 and set(cycles) == {original_id, duplicate_id},
        "one_active_original_cycle": bool(original and original.get("cycle_status") == "active"
                                           and original.get("unassigned_at") is None
                                           and duplicate and duplicate.get("cycle_status") == "closed"
                                           and duplicate.get("unassigned_at") is not None),
        "both_pointers_canonical": lifecycle.get("current_assignment_cycle_id") == original_id
                                    and lifecycle.get("assignment_cycle_id") == original_id,
        "owner_preserved": lead.get("ejecutivo_asignado") == case["owner_name"]
                           and (lead.get("prospecto") or {}).get("ejecutivo") == case["owner_name"]
                           and lifecycle.get("assigned_to_user_id") == case["owner_user_id"],
        "original_provider_acceptance_sla": _utc(lifecycle.get("sla_started_at")) == expected_start
                                           and _utc(original.get("sla_started_at")) == expected_start
                                           and _utc((original.get("sla_segments") or [{}])[0].get("segment_start")) == expected_start,
        "first_management_completed": original.get("sla_first_management_status") == "completed"
                                     and _utc(lifecycle.get("first_valid_management_at")) == _utc(original.get("first_valid_management_at")),
        "lead_sla_remains_fulfilled": lead.get("sla_status") == "fulfilled",
        "duplicate_excluded_from_first_sla": duplicate.get("administratively_corrected") is True
                                             and duplicate.get("corrects_assignment_cycle_id") == original_id
                                             and duplicate.get("sla_first_management_status") == "excluded_duplicate_cycle"
                                             and duplicate.get("sla_performance_excluded") is True
                                             and duplicate.get("notification_eligible") is False,
        "human_management_unchanged": management_preserved,
        "notifications_unchanged_no_new_whatsapp": notification_history_unchanged,
        "tasks_and_visits_unchanged": unchanged_operational_records,
        "no_open_pointer_alerts": not alerts_open,
        "audit_event_present": bool(audit and audit.get("type") == "ADMINISTRATIVE_PROP360_DUPLICATE_CYCLE_REPAIR"
                                     and audit_meta.get("incident_id") == case["incident_id"]
                                     and audit_meta.get("provider_accepted_at") == accepted_at
                                     and audit_meta.get("customer_message_sent") is False
                                     and audit_meta.get("assignment_notification_sent") is False),
        "event_history_preserved": _fingerprint(events_without_audit) == _fingerprint(backup_events),
        "chatbot_owner_consistent": bool(pointer_context and pointer_context.get("status") == "consistent"
                                          and pointer_context.get("owner_name") == case["owner_name"]
                                          and (pointer_context.get("cycle") or {}).get("assignment_cycle_id") == original_id),
    }
    failed = [key for key, passed in checks.items() if not passed]
    if failed:
        if diagnostic:
            raise RepairSafetyError("postcondition failed: " + ", ".join(failed))
        return None
    return {
        "case": case_name,
        "lead_id": case["lead_id"],
        "property_code": case["property_code"],
        "owner": case["owner_name"],
        "active_cycle_id": original_id,
        "corrected_duplicate_cycle_id": duplicate_id,
        "cycles_retained": len(snapshot["cycles"]),
        "human_management_records_preserved": len(snapshot["management"]),
        "provider_accepted_at": accepted_at.isoformat(),
        "sla_status": lead.get("sla_status"),
        "pointer_alerts_open": len(alerts_open),
        "chatbot_owner_status": pointer_context.get("status"),
        "new_messages_sent": False,
    }


def _validate_production_identity(client: MongoClient, db: Any, case: Mapping[str, Any]) -> dict[str, str]:
    hello = client.admin.command("hello")
    hosts = [str(value).rsplit(":", 1)[0] for value in hello.get("hosts", [])]
    if (not hello.get("setName") or not any(
            host.startswith(PRODUCTION_CLUSTER_PREFIX) and host.endswith(PRODUCTION_CLUSTER_SUFFIX)
            for host in hosts)):
        raise RepairSafetyError("connected replica set is not the confirmed Procasa production cluster")
    stats = db.command("dbStats")
    if stats.get("db") != DATABASE or int(stats.get("collections") or 0) < 1:
        raise RepairSafetyError("database identity does not match nonempty production URLS")
    lead = db["leads"].find_one({"_id": ObjectId(case["lead_id"])}, {"ejecutivo_asignado": 1, "prospecto.codigo": 1})
    if (not lead or lead.get("ejecutivo_asignado") != case["owner_name"]
            or (lead.get("prospecto") or {}).get("codigo") != case["property_code"]):
        raise RepairSafetyError("production identity check does not match the reviewed owner and property")
    return {"database": DATABASE, "replica_set": str(hello["setName"]),
            "cluster_prefix": PRODUCTION_CLUSTER_PREFIX}


def _apply(client: MongoClient, db: Any, case_name: str, manifest: Mapping[str, Any], actor: str) -> str:
    case = CASES[case_name]
    lead_oid = ObjectId(case["lead_id"])
    original_id, duplicate_id = case["original_cycle_id"], case["duplicate_cycle_id"]
    audit_id = f"crm_event:{case['incident_id']}:duplicate-cycle-repair:v1"

    def transaction(session: Any) -> str:
        snapshot = _collect_snapshot(db, case, session)
        if _manifest_snapshot(snapshot) != manifest.get("snapshot"):
            raise RepairSafetyError("concurrency check failed: production documents changed after preflight")
        lead, original, duplicate, accepted = _validate_scenario(case_name, snapshot)
        if db["crm_events"].find_one({"_id": audit_id}, session=session):
            raise RepairSafetyError("repair audit exists but postconditions are not yet verified")
        expected_acceptance = {**accepted, "accepted_at": accepted["accepted_at"].isoformat()}
        if manifest.get("provider_acceptance") != expected_acceptance:
            raise RepairSafetyError("provider-acceptance evidence changed after preflight")
        now = datetime.now(timezone.utc)
        accepted_at = accepted["accepted_at"]
        duplicate_management_ids = sorted(
            record_id for record_id, row in case["management"].items()
            if row["cycle_id"] == duplicate_id
        )

        # Close the accidental cycle first for the active-cycle partial unique
        # index. The transaction ensures readers never observe the intermediate state.
        _require_one(db["crm_assignment_cycles"].update_one(
            _cycle_filter(duplicate),
            {"$set": {
                "cycle_status": "closed", "unassigned_at": now, "closed_at": now,
                "closed_reason": "duplicate_prop360_assignment_cycle_corrected",
                "cycle_version": int(duplicate.get("cycle_version") or 0) + 1,
                "administratively_corrected": True,
                "correction_incident_id": case["incident_id"],
                "corrects_assignment_cycle_id": original_id,
                "corrected_by": actor, "corrected_at": now,
                "correction_reason": "same lead, property, and executive; Prop360 alone created a new cycle after prior valid human management",
                "preserved_management_result_ids": duplicate_management_ids,
                "notification_eligible": False,
                "sla_performance_excluded": True,
                "sla_performance_exclusion_reason": "technical duplicate Prop360 cycle; original assignment owns first-management SLA",
                "sla_first_management_status": "excluded_duplicate_cycle",
                "updated_at": now,
            }}, session=session), "duplicate-cycle closure")

        segment = deepcopy((original.get("sla_segments") or [{}])[0])
        previous_start = _utc(original.get("sla_started_at"))
        segment["segment_start"] = accepted_at
        _require_one(db["crm_assignment_cycles"].update_one(
            _cycle_filter(original),
            {"$set": {
                "cycle_status": "active", "unassigned_at": None,
                "sla_started_at": accepted_at,
                "sla_segments": [segment],
                "cycle_version": int(original.get("cycle_version") or 0) + 1,
                "assignment_cycle_restored_at": now,
                "assignment_cycle_restored_by": actor,
                "updated_at": now,
            }, "$unset": {"closed_at": "", "closed_reason": ""},
                "$push": {"administrative_state_history": {
                    "incident_id": case["incident_id"],
                    "action": "restore_original_cycle_and_reverse_prop360_close",
                    "actor": actor, "at": now,
                    "prior_cycle_status": original.get("cycle_status"),
                    "prior_unassigned_at": original.get("unassigned_at"),
                    "prior_closed_at": original.get("closed_at"),
                    "prior_closed_reason": original.get("closed_reason"),
                    "duplicate_cycle_id": duplicate_id,
                    "prior_sla_started_at": previous_start,
                    "sla_started_at_corrected_to_provider_acceptance": accepted_at,
                    "provider_acceptance_evidence": {key: value for key, value in accepted.items()
                                                       if key != "accepted_at"} | {"accepted_at": accepted_at},
                    "first_management_preserved_at": original.get("first_valid_management_at"),
                    "duplicate_management_preserved_ids": duplicate_management_ids,
                }}}, session=session), "original-cycle closure reversal")

        lead_lifecycle = lead.get("lifecycle") or {}
        lead_selector = {
            "_id": lead_oid,
            "ejecutivo_asignado": case["owner_name"],
            "prospecto.ejecutivo": case["owner_name"],
            "prospecto.codigo": case["property_code"],
            "lifecycle.current_assignment_cycle_id": duplicate_id,
            "lifecycle.assignment_cycle_id": duplicate_id,
            "lifecycle.assigned_to_user_id": case["owner_user_id"],
            "lifecycle.sla_started_at": lead_lifecycle.get("sla_started_at"),
            "sla_status": "fulfilled",
        }
        lead_set = {
            "lifecycle.current_assignment_cycle_id": original_id,
            "lifecycle.assignment_cycle_id": original_id,
            "lifecycle.assigned_at": original.get("assigned_at"),
            "lifecycle.cycle_started_at": original.get("cycle_started_at") or original.get("assigned_at"),
            "lifecycle.assigned_to_user_id": case["owner_user_id"],
            "lifecycle.assigned_to_display_name": case["owner_name"],
            "lifecycle.sla_started_at": accepted_at,
            "lifecycle.first_contact_attempt_at": original.get("first_contact_attempt_at"),
            "lifecycle.first_effective_contact_at": original.get("first_effective_contact_at"),
            "lifecycle.first_valid_management_at": original.get("first_valid_management_at"),
            "lifecycle.first_valid_management_actor": next(
                row["actor_user_id"] for row in snapshot["management"]
                if row.get("assignment_cycle_id") == original_id
                and _utc(row.get("occurred_at")) == _utc(original.get("first_valid_management_at"))
            ),
            "lifecycle.sla_first_management_status": "completed",
            "updated_at": now.isoformat(),
            "last_crm_update": now,
        }
        _require_one(db["leads"].update_one(lead_selector, {"$set": lead_set}, session=session),
                     "canonical lead pointers and original SLA mirror")

        resolved_alert_ids: list[str] = []
        for alert in snapshot["alerts"]:
            if alert.get("status") != "open":
                continue
            evidence = alert.get("evidence") or {}
            selector = {"_id": alert["_id"], "status": "open", "issue_code": alert.get("issue_code")}
            if "signature" in evidence:
                selector["evidence.signature"] = evidence["signature"]
            if "updated_at" in alert:
                selector["updated_at"] = alert["updated_at"]
            _require_one(db["crm_assignment_integrity_alerts"].update_one(selector, {"$set": {
                "status": "resolved", "resolved_at": now, "resolved_by": actor,
                "resolution": "restored original managed cycle, closed the Prop360-only duplicate, and aligned both canonical pointers",
                "correction_incident_id": case["incident_id"],
                "correction_cycle_id": original_id,
            }}, session=session), "reviewed pointer-conflict alert resolution")
            resolved_alert_ids.append(str(alert["_id"]))

        db["crm_events"].insert_one({
            "_id": audit_id, "lead_id": lead_oid, "assignment_cycle_id": original_id,
            "actor": actor, "actor_type": "administrator",
            "type": "ADMINISTRATIVE_PROP360_DUPLICATE_CYCLE_REPAIR",
            "result": "ORIGINAL_MANAGED_CYCLE_RESTORED",
            "confirmed": True, "timestamp": now, "source": "p0_incident_remediation",
            "meta": {
                "incident_id": case["incident_id"],
                "property_code": case["property_code"],
                "original_cycle_id": original_id,
                "corrected_duplicate_cycle_id": duplicate_id,
                "prop360_event_id": case["prop360_event_id"],
                "preserved_management_result_ids": sorted(case["management"]),
                "duplicate_cycle_management_preserved_ids": duplicate_management_ids,
                "provider_acceptance_evidence": {key: value for key, value in accepted.items()
                                                  if key != "accepted_at"} | {"accepted_at": accepted_at},
                "provider_accepted_at": accepted_at,
                "prior_sla_started_at": previous_start,
                "sla_restarted": False,
                "first_management_remains_completed": True,
                "duplicate_cycle_excluded_from_first_management_sla": True,
                "cycle_start_preserved": True,
                "customer_message_sent": False,
                "assignment_notification_sent": False,
                "new_outbound_messages_sent": False,
                "visit_or_order_created": False,
                "resolved_alert_ids": sorted(resolved_alert_ids),
            },
        }, session=session)

        if not _verify_applied(db, case_name, _build_backup(case_name, snapshot),
                               session=session, diagnostic=True):
            raise RepairSafetyError("in-transaction postcondition failed; all writes will roll back")
        return "committed"

    with client.start_session() as session:
        return session.with_transaction(
            transaction,
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority"),
            read_preference=ReadPreference.PRIMARY,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=sorted(CASES))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture-backup", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--backup-out", type=Path)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--preflight-out", type=Path)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--confirm-incident")
    args = parser.parse_args(argv)
    if not args.actor.strip() or args.actor.strip().lower() in {"system", "bot", "sistema", "assistant"}:
        parser.error("--actor must identify the authorized human administrator")
    if args.capture_backup:
        if not args.backup_out or args.backup or args.preflight or args.preflight_out or args.confirm_incident:
            parser.error("--capture-backup requires only --case, --backup-out, and --actor")
    elif not args.backup or args.backup_out:
        parser.error("--dry-run/--execute/--verify require --backup; --backup-out is for --capture-backup")
    if args.dry_run and (args.preflight or args.confirm_incident):
        parser.error("--dry-run uses --preflight-out and does not accept execution confirmation")
    if args.execute and (not args.preflight or args.confirm_incident != CASES[args.case]["confirmation"]):
        parser.error(f"--execute requires --preflight and --confirm-incident {CASES[args.case]['confirmation']}")
    if (args.verify or args.capture_backup) and (args.preflight or args.preflight_out or args.confirm_incident):
        parser.error("--verify/--capture-backup does not accept preflight or confirmation arguments")
    if args.execute and args.preflight_out:
        parser.error("--execute uses --preflight, not --preflight-out")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    case = CASES[args.case]
    backup_path = args.backup.expanduser().resolve() if args.backup else None
    backup = _load_backup(backup_path, args.case) if backup_path else None
    backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest() if backup_path else None
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
    if not uri:
        print("ERROR: managed MONGODB_URI/MONGO_URI secret is unavailable; no URI is accepted on the command line.", file=sys.stderr)
        return 2
    client = MongoClient(uri, appName=f"procasa-prop360-duplicate-repair-{args.case}", retryWrites=True,
                         connectTimeoutMS=5000, serverSelectionTimeoutMS=15000, socketTimeoutMS=30000)
    try:
        db = client[DATABASE]
        identity = _validate_production_identity(client, db, case)
        if args.capture_backup:
            output_path = args.backup_out.expanduser().resolve()
            with client.start_session() as session:
                session.start_transaction(read_concern=ReadConcern("snapshot"),
                                          write_concern=WriteConcern("majority"),
                                          read_preference=ReadPreference.PRIMARY)
                try:
                    snapshot = _collect_snapshot(db, case, session)
                    _validate_scenario(args.case, snapshot)
                    backup_payload = _build_backup(args.case, snapshot)
                    session.commit_transaction()
                except Exception:
                    if session.in_transaction:
                        session.abort_transaction()
                    raise
            checksum = _write_backup(output_path, backup_payload)
            print("STATUS: production_backup_captured; no database writes performed")
            print(f"BACKUP: {output_path}")
            print(f"BACKUP_SHA256: {checksum}")
            print(f"DATABASE_IDENTITY: {DATABASE}; confirmed Procasa production replica set")
            print(f"SCOPE: case={args.case}; cycles={len(snapshot['cycles'])}; management_records={len(snapshot['management'])}")
            return 0

        if args.verify:
            result = _verify_applied(db, args.case, backup, diagnostic=True)
            print("STATUS: production_verified; " + json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
            return 0

        if args.dry_run:
            already = _verify_applied(db, args.case, backup)
            if already:
                print("STATUS: already_applied; no writes performed")
                print("PRODUCTION_VERIFICATION: " + json.dumps(already, ensure_ascii=False, sort_keys=True, default=str))
                return 0
            snapshot = _collect_snapshot(db, case)
            lead, original, duplicate, accepted = _validate_scenario(args.case, snapshot)
            expected = {key: _backup_docs(backup, key) for key in COLLECTIONS}
            if _manifest_snapshot(snapshot) != _manifest_snapshot(expected):
                raise RepairSafetyError("live snapshot changed after backup; capture a fresh backup")
            manifest = {
                "format": PREFLIGHT_FORMAT, "case": args.case,
                "incident_id": case["incident_id"], "database": DATABASE,
                "production_identity": identity, "lead_id": case["lead_id"],
                "actor": args.actor.strip(), "backup_sha256": backup_sha256,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "original_cycle_id": case["original_cycle_id"],
                "duplicate_cycle_id": case["duplicate_cycle_id"],
                "provider_acceptance": {**accepted, "accepted_at": accepted["accepted_at"].isoformat()},
                "snapshot": _manifest_snapshot(snapshot),
            }
            output_path = (args.preflight_out or backup_path.with_name(f"{args.case}-preflight.json")).expanduser().resolve()
            manifest["manifest_sha256"] = _manifest_digest(manifest)
            _write_json(output_path, manifest)
            print("STATUS: dry_run_passed; no database writes performed")
            print(f"PREFLIGHT: {output_path}")
            print(f"BACKUP_SHA256: {backup_sha256}")
            print(f"CYCLES: {len(snapshot['cycles'])} retained; restore {case['original_cycle_id']} as the sole active cycle")
            print(f"MANAGEMENT_RECORDS: {len(snapshot['management'])} preserved unchanged")
            print(f"SLA_START: provider accepted at {accepted['accepted_at'].isoformat()}; no timestamp inferred")
            print("NOTIFICATIONS: no outbound messages sent or queued by this repair")
            return 0

        manifest = _load_manifest(args.preflight.expanduser().resolve(), backup_sha256, args.case, args.actor.strip())
        if manifest.get("production_identity") != identity:
            raise RepairSafetyError("preflight was created against a different production replica set")
        if (manifest.get("original_cycle_id") != case["original_cycle_id"]
                or manifest.get("duplicate_cycle_id") != case["duplicate_cycle_id"]):
            raise RepairSafetyError("preflight cycle identities do not match this case")
        already = _verify_applied(db, args.case, backup)
        if already:
            print("STATUS: already_applied; postconditions verified; no writes performed")
            print("PRODUCTION_VERIFICATION: " + json.dumps(already, ensure_ascii=False, sort_keys=True, default=str))
            return 0
        result = _apply(client, db, args.case, manifest, actor=args.actor.strip())
        verification = _verify_applied(db, args.case, backup, diagnostic=True)
        print(f"STATUS: {result}")
        print("PRODUCTION_POSTCOMMIT_VERIFICATION: " + json.dumps(verification, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    except RepairSafetyError as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}; transaction was not reported as successful", file=sys.stderr)
        return 4
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
