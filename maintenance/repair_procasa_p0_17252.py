#!/usr/bin/env python3
"""Repair the duplicate Prop360 cycle on Procasa lead/property 17252.

The command is fail-closed: it captures a fresh production backup, creates a
checksummed live preflight with --dry-run, then requires that exact preflight
and an explicit incident confirmation for one Mongo transaction.
Mongo access is read from MONGODB_URI or MONGO_URI in the authorized runtime.
This repair never contacts the customer, schedules a visit, or sends WhatsApp.
"""
from __future__ import annotations

import argparse
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


INCIDENT_ID = "PROC-2026-10-P0-17252"
CONFIRMATION = "P0-17252-MERGE-DUPLICATE-CYCLE"
DATABASE = "URLS"
LEAD_OID = "6ad6066de66c461dd69d696d"
ORIGINAL_CYCLE_ID = "88ec815c-b6d7-4eb2-810c-b54c435c1a15"
DUPLICATE_CYCLE_ID = "8b1c8b19-278d-4dc9-b936-e3911656fbe7"
MARIELA_USER_ID = "6989c6309dd2ba54e478196c"
MARIELA_NAME = "Mariela Arriagada"
PROP360_EVENT_ID = "12276"
VALID_MANAGEMENT_ID = "crm_management:c7ad4a4b-9968-446e-a594-22162137a4ad"
VISIT_DAY_EVENT_ID = "6ad658265969a1a4f3907ed2"
VISIT_HOURS_EVENT_ID = "6ad6582c5969a1a4f3907ed3"
HOT_INTENT_EVENT_ID = "6ad6584d5969a1a4f3907eda"
VISIT_REQUEST_AT = datetime(2026, 10, 19, 17, 49, 26, 25_000, tzinfo=timezone.utc)
VISIT_HOURS_AT = datetime(2026, 10, 19, 17, 49, 32, 711_000, tzinfo=timezone.utc)
HOT_EVENT_AT = datetime(2026, 10, 19, 17, 50, 5, 671_000, tzinfo=timezone.utc)
HOT_SINCE_AT = datetime(2026, 10, 19, 17, 50, 4, 23_557, tzinfo=timezone.utc)
AUDIT_EVENT_ID = f"crm_event:{INCIDENT_ID}:duplicate-cycle-repair:v1"
VISIT_AVAILABILITY = "Lunes a viernes, entre las 14:00 y 16:00"
BACKUP_FORMAT = "procasa-p0-17252-preflight-snapshot-v1"
PRODUCTION_CLUSTER_PREFIX = "ac-8f1k7c3-shard-"
PRODUCTION_CLUSTER_SUFFIX = ".mzve39k.mongodb.net"

COLLECTIONS = {
    "lead": "leads",
    "cycles": "crm_assignment_cycles",
    "management": "crm_management_results",
    "events": "crm_events",
    "integrity_alerts": "crm_assignment_integrity_alerts",
    "sla_alerts": "crm_sla_alerts_v1",
    "notifications": "crm_notifications_v1",
    "pending_notifications": "pending_notifications",
    "ingest": "lead_ingest_events",
    "tasks": "crm_tasks",
    "visitas": "visitas",
    "ordenes_visitas": "ordenes_visitas",
}


class RepairSafetyError(RuntimeError):
    """Live state differs from the reviewed incident snapshot."""


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
    return json_util.dumps(
        value, json_options=json_util.CANONICAL_JSON_OPTIONS,
        sort_keys=True, separators=(",", ":"),
    )


def _fingerprint(rows: list[Mapping[str, Any]]) -> str:
    material = "\n".join(sorted(_canonical(dict(row)) for row in rows))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _docs(backup: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    return list(backup["collections"][key]["documents"])


def load_backup(path: Path) -> dict[str, Any]:
    try:
        backup = json_util.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RepairSafetyError(f"backup cannot be parsed ({type(exc).__name__})") from exc
    if backup.get("format") != BACKUP_FORMAT or backup.get("database") != DATABASE:
        raise RepairSafetyError("backup format or database does not match this incident")
    if str(backup.get("lead_id")) != LEAD_OID:
        raise RepairSafetyError("backup lead identity does not match this incident")
    for key, collection in COLLECTIONS.items():
        bundle = backup.get("collections", {}).get(key)
        if not isinstance(bundle, Mapping) or bundle.get("collection") != collection:
            raise RepairSafetyError(f"backup is missing collection {key}")
        if not isinstance(bundle.get("documents"), list):
            raise RepairSafetyError(f"backup collection {key} is malformed")
    return backup


def _find(db: Any, collection: str, query: Mapping[str, Any], session: Any = None, limit: int = 2000):
    options = {"session": session} if session is not None else {}
    return list(db[collection].find(dict(query), **options).limit(limit))


def _snapshot_queries(db: Any, session: Any = None) -> dict[str, tuple[str, dict[str, Any]]]:
    lead_oid = ObjectId(LEAD_OID)
    options = {"session": session} if session is not None else {}
    lead = db["leads"].find_one({"_id": lead_oid}, **options)
    if not lead:
        raise RepairSafetyError("target lead is absent from the connected database")
    phone = lead.get("phone")
    cycle_ids = [ORIGINAL_CYCLE_ID, DUPLICATE_CYCLE_ID]
    return {
        "lead": ("leads", {"_id": lead_oid}),
        "cycles": ("crm_assignment_cycles", {"lead_id": {"$in": [lead_oid, LEAD_OID]}}),
        "management": ("crm_management_results", {"lead_id": {"$in": [lead_oid, LEAD_OID]}}),
        "events": ("crm_events", {"lead_id": {"$in": [lead_oid, LEAD_OID]}}),
        "integrity_alerts": ("crm_assignment_integrity_alerts", {"lead_id": {"$in": [lead_oid, LEAD_OID]}}),
        "sla_alerts": ("crm_sla_alerts_v1", {"assignment_cycle_id": {"$in": cycle_ids}}),
        "notifications": ("crm_notifications_v1", {"$or": [
            {"assignment_cycle_id": {"$in": cycle_ids}},
            {"assignment_cycle_ids": {"$in": cycle_ids}},
            {"lead_id": {"$in": [lead_oid, LEAD_OID]}},
        ]}),
        "pending_notifications": ("pending_notifications", {"$or": [
            {"lead_id": {"$in": [lead_oid, LEAD_OID]}},
            {"phone": phone}, {"telefono": phone}, {"lead_phone": phone},
        ]}),
        "ingest": ("lead_ingest_events", {"$or": [
            {"lead_id": {"$in": [lead_oid, LEAD_OID]}, "property_code": "17252"},
            {"source_event_id": PROP360_EVENT_ID},
        ]}),
        "tasks": ("crm_tasks", {"$or": [
            {"lead_id": {"$in": [lead_oid, LEAD_OID]}},
            {"assignment_cycle_id": {"$in": cycle_ids}},
            {"phone": phone},
        ]}),
        "visitas": ("visitas", {"$or": [
            {"lead_id": {"$in": [lead_oid, LEAD_OID]}},
            {"assignment_cycle_id": {"$in": cycle_ids}},
            {"phone": phone},
        ]}),
        "ordenes_visitas": ("ordenes_visitas", {"$or": [
            {"lead_id": {"$in": [lead_oid, LEAD_OID]}},
            {"assignment_cycle_id": {"$in": cycle_ids}},
            {"phone": phone},
        ]}),
        "active_jobs": ("chatbot_inbound_jobs", {"$and": [
            {"state": {"$in": ["received", "batching", "pending", "processing"]}},
            {"$or": [{"lead_id": {"$in": [lead_oid, LEAD_OID]}}, {"phone": phone}]},
        ]}),
    }


def collect_snapshot(db: Any, backup: Mapping[str, Any] | None = None, session: Any = None) -> dict[str, list[dict[str, Any]]]:
    snapshot = {}
    for key, (collection, query) in _snapshot_queries(db, session=session).items():
        snapshot[key] = _find(db, collection, query, session=session)
    return snapshot


def _provider_acceptance_evidence(
    notifications: list[Mapping[str, Any]], *, lead_id: str = LEAD_OID,
) -> dict[str, Any]:
    """Return one provider-accepted original WhatsApp, never a delivery timestamp.

    A 2xx provider response, returned message ID, successful delivery attempt,
    and its completed_at time are required. Ambiguous or missing evidence
    aborts the repair rather than guessing an SLA start.
    """
    matches: list[dict[str, Any]] = []
    for notification in notifications:
        cycle_ids = {str(value) for value in (notification.get("assignment_cycle_ids") or [])}
        lead_ids = {str(value) for value in (notification.get("lead_ids") or [])}
        if ORIGINAL_CYCLE_ID not in cycle_ids or lead_id not in lead_ids:
            continue
        provider_message_id = str(notification.get("provider_message_id") or "").strip()
        if notification.get("state") != "sent" or not provider_message_id:
            continue
        for attempt in notification.get("delivery_attempts") or []:
            status = attempt.get("provider_http_status")
            accepted_at = _utc(attempt.get("completed_at"))
            attempt_message_id = str(attempt.get("provider_message_id") or "").strip()
            if (isinstance(status, int) and 200 <= status < 300
                    and attempt.get("result") == "sent"
                    and attempt_message_id == provider_message_id
                    and accepted_at is not None):
                matches.append({
                    "notification_id": str(notification.get("_id")),
                    "provider_message_id": provider_message_id,
                    "provider_http_status": status,
                    "accepted_at": accepted_at,
                    "assignment_cycle_id": ORIGINAL_CYCLE_ID,
                    "lead_id": lead_id,
                    "timestamp_source": "crm_notifications_v1.delivery_attempts.completed_at_after_provider_2xx",
                })
    unique = {
        (row["notification_id"], row["provider_message_id"], row["accepted_at"]): row
        for row in matches
    }
    if len(unique) != 1:
        raise RepairSafetyError(
            "expected exactly one provider-accepted original WhatsApp for the original cycle; "
            "refusing to infer an SLA start"
        )
    return next(iter(unique.values()))


def _backup_from_snapshot(snapshot: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    collection_names = {**COLLECTIONS, "active_jobs": "chatbot_inbound_jobs"}
    return {
        "format": BACKUP_FORMAT,
        "created_at": datetime.now(timezone.utc),
        "database": DATABASE,
        "lead_id": LEAD_OID,
        "collections": {
            key: {"collection": collection_names[key], "documents": rows}
            for key, rows in snapshot.items()
        },
    }


def _validate_production_identity(client: MongoClient, db: Any) -> dict[str, Any]:
    hello = client.admin.command("hello")
    set_name = str(hello.get("setName") or "")
    hosts = [str(value).rsplit(":", 1)[0] for value in (hello.get("hosts") or [])]
    if not set_name or not any(
        host.startswith(PRODUCTION_CLUSTER_PREFIX) and host.endswith(PRODUCTION_CLUSTER_SUFFIX)
        for host in hosts
    ):
        raise RepairSafetyError("connected MongoDB replica set is not the confirmed Procasa production cluster")
    stats = db.command("dbStats")
    if stats.get("db") != DATABASE or int(stats.get("collections") or 0) < 1:
        raise RepairSafetyError("connected database identity does not match the nonempty production URLS database")
    lead = db["leads"].find_one({"_id": ObjectId(LEAD_OID)}, {"ejecutivo_asignado": 1, "prospecto.codigo": 1})
    if (not lead or lead.get("ejecutivo_asignado") != MARIELA_NAME
            or (lead.get("prospecto") or {}).get("codigo") != "17252"):
        raise RepairSafetyError("production identity check did not find the expected Mariela/property 17252 lead")
    return {"database": DATABASE, "replica_set": set_name, "cluster_prefix": PRODUCTION_CLUSTER_PREFIX}


def _snapshot_manifest(snapshot: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, Any]:
    return {key: {"count": len(rows), "sha256": _fingerprint(list(rows))}
            for key, rows in sorted(snapshot.items())}


def _id_set(rows: list[Mapping[str, Any]]) -> set[str]:
    return {str(row.get("_id")) for row in rows}


def validate_scenario(backup: Mapping[str, Any], snapshot: Mapping[str, list[dict[str, Any]]]):
    expected_counts = {"lead": 1, "cycles": 2, "management": 1}
    for key, count in expected_counts.items():
        if len(_docs(backup, key)) != count or len(snapshot[key]) != count:
            raise RepairSafetyError(f"expected {count} backup/live {key} documents")
    lead = snapshot["lead"][0]
    if str(lead.get("_id")) != LEAD_OID:
        raise RepairSafetyError("live lead id changed")
    if (lead.get("ejecutivo_asignado") != MARIELA_NAME
            or (lead.get("prospecto") or {}).get("ejecutivo") != MARIELA_NAME):
        raise RepairSafetyError("Mariela's owner mirrors changed")
    if lead.get("sla_status") != "fulfilled" or lead.get("lead_temperature_effective") != "HOT":
        raise RepairSafetyError("current SLA/HOT status differs from the reviewed case")
    lifecycle = lead.get("lifecycle") or {}
    if (lifecycle.get("current_assignment_cycle_id") != DUPLICATE_CYCLE_ID
            or lifecycle.get("assignment_cycle_id") != ORIGINAL_CYCLE_ID
            or lifecycle.get("assigned_to_user_id") != MARIELA_USER_ID):
        raise RepairSafetyError("live cycle pointers/owner no longer match the split-pointer incident")

    expected_cycle_ids = {ORIGINAL_CYCLE_ID, DUPLICATE_CYCLE_ID}
    live_cycles = {str(row.get("assignment_cycle_id")): row for row in snapshot["cycles"]}
    backup_cycles = {str(row.get("assignment_cycle_id")): row for row in _docs(backup, "cycles")}
    if set(live_cycles) != expected_cycle_ids or set(backup_cycles) != expected_cycle_ids:
        raise RepairSafetyError("assignment cycle identities changed since backup")
    original, duplicate = live_cycles[ORIGINAL_CYCLE_ID], live_cycles[DUPLICATE_CYCLE_ID]
    if (original.get("assigned_to_user_id") != MARIELA_USER_ID
            or original.get("assigned_to_display_name") != MARIELA_NAME
            or original.get("cycle_status") != "closed"
            or duplicate.get("assigned_to_user_id") != MARIELA_USER_ID
            or duplicate.get("assigned_to_display_name") != MARIELA_NAME
            or duplicate.get("cycle_status") != "active"
            or duplicate.get("source_system") != "prop360"
            or str(duplicate.get("source_event_id")) != PROP360_EVENT_ID):
        raise RepairSafetyError("live original/duplicate cycle facts changed")
    original_sla_start = _utc(original.get("sla_started_at"))
    if not original_sla_start or _utc(lifecycle.get("sla_started_at")) != original_sla_start:
        raise RepairSafetyError("original SLA start does not match the lead lifecycle")
    if original.get("sla_first_management_status") != "completed":
        raise RepairSafetyError("the source cycle no longer records the first management as completed")
    if _utc(lifecycle.get("hot_since")) != HOT_SINCE_AT:
        raise RepairSafetyError("the lead's HOT transition no longer matches captured lifecycle evidence")

    management = snapshot["management"]
    if _id_set(management) != {VALID_MANAGEMENT_ID}:
        raise RepairSafetyError("valid management result identity changed")
    result = management[0]
    if (result.get("assignment_cycle_id") != ORIGINAL_CYCLE_ID
            or result.get("result_type") != "EFFECTIVE_CONTACT"
            or result.get("actor_user_id") != MARIELA_USER_ID
            or result.get("status") != "completed"
            or _utc(result.get("occurred_at")) != _utc(original.get("first_valid_management_at"))):
        raise RepairSafetyError("Mariela's original EFFECTIVE_CONTACT evidence changed")

    events = snapshot["events"]
    expected_event_ids = _id_set(_docs(backup, "events"))
    if _id_set(events) != expected_event_ids or len(events) != 18:
        raise RepairSafetyError("CRM event history changed after backup")
    by_id = {str(row.get("_id")): row for row in events}
    day_event = by_id.get(VISIT_DAY_EVENT_ID)
    hour_event = by_id.get(VISIT_HOURS_EVENT_ID)
    hot_event = by_id.get(HOT_INTENT_EVENT_ID)
    if (not day_event or "lunes a viernes" not in str((day_event.get("meta") or {}).get("text") or "").lower()
            or _utc(day_event.get("timestamp")) != VISIT_REQUEST_AT
            or not hour_event or "14:00" not in str((hour_event.get("meta") or {}).get("text") or "")
            or "16:00" not in str((hour_event.get("meta") or {}).get("text") or "")
            or _utc(hour_event.get("timestamp")) != VISIT_HOURS_AT
            or not hot_event or (hot_event.get("meta") or {}).get("intent") != "ASK_VISIT"
            or _utc(hot_event.get("timestamp")) != HOT_EVENT_AT):
        raise RepairSafetyError("visit availability or HOT-intent evidence changed")

    alerts = snapshot["integrity_alerts"]
    alert_ids = {"assignment-integrity:49e4aaa7296dce6ebbd29eb1",
                 "assignment-integrity:e9b9e1705c57a16604b422f7"}
    if _id_set(alerts) != alert_ids or any(row.get("status") != "open" for row in alerts):
        raise RepairSafetyError("cycle-pointer alerts changed or are no longer open")
    if any("cycle_pointer_mismatch" not in ((row.get("evidence") or {}).get("issues") or []) for row in alerts):
        raise RepairSafetyError("the alert evidence no longer identifies cycle_pointer_mismatch")

    for key in ("notifications", "pending_notifications", "ingest", "tasks", "visitas", "ordenes_visitas"):
        if _id_set(snapshot[key]) != _id_set(_docs(backup, key)):
            raise RepairSafetyError(f"{key} identities changed since backup")
    provider_acceptance = _provider_acceptance_evidence(snapshot["notifications"])
    backup_acceptance = _provider_acceptance_evidence(_docs(backup, "notifications"))
    if provider_acceptance != backup_acceptance:
        raise RepairSafetyError("provider acceptance evidence changed after the pre-repair backup")
    management_at = _utc(result.get("occurred_at"))
    if (provider_acceptance["accepted_at"] < _utc(original.get("assigned_at"))
            or not management_at or management_at < provider_acceptance["accepted_at"]):
        raise RepairSafetyError("provider acceptance does not bound the original management evidence")
    if (snapshot["sla_alerts"] or snapshot["pending_notifications"] or snapshot["active_jobs"]
            or snapshot["visitas"] or snapshot["ordenes_visitas"]):
        raise RepairSafetyError("an SLA alert, outbound notification, active inbound job, or visit record needs review first")
    return lead, original, duplicate, result, provider_acceptance


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    if path.exists():
        raise RepairSafetyError(f"preflight file already exists: {path.name}; choose a new path")
    manifest["manifest_sha256"] = _manifest_digest(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _write_backup(path: Path, backup: Mapping[str, Any]) -> str:
    if path.exists():
        raise RepairSafetyError(f"backup file already exists: {path.name}; choose a new path")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json_util.dumps(backup, json_options=json_util.CANONICAL_JSON_OPTIONS, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_manifest(path: Path, *, backup_sha256: str, actor: str) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RepairSafetyError(f"preflight cannot be parsed ({type(exc).__name__})") from exc
    if manifest.get("manifest_sha256") != _manifest_digest(manifest):
        raise RepairSafetyError("preflight checksum does not match")
    if (manifest.get("incident_id") != INCIDENT_ID
            or manifest.get("backup_sha256") != backup_sha256
            or manifest.get("actor") != actor):
        raise RepairSafetyError("preflight belongs to another incident, backup, or actor")
    return manifest


def _active(row: Mapping[str, Any]) -> bool:
    return row.get("cycle_status") == "active" and row.get("unassigned_at") is None


def _verify_applied(db: Any, session: Any = None) -> dict[str, Any] | None:
    options = {"session": session} if session is not None else {}
    lead = db["leads"].find_one({"_id": ObjectId(LEAD_OID)}, **options)
    if not lead:
        return None
    cycles = _find(db, "crm_assignment_cycles", {"lead_id": ObjectId(LEAD_OID)}, session=session)
    management = _find(db, "crm_management_results", {"lead_id": ObjectId(LEAD_OID)}, session=session)
    audit = db["crm_events"].find_one({"_id": AUDIT_EVENT_ID}, **options)
    alerts = _find(db, "crm_assignment_integrity_alerts", {"lead_id": ObjectId(LEAD_OID)}, session=session)
    visit_query = {"$or": [
        {"lead_id": {"$in": [ObjectId(LEAD_OID), LEAD_OID]}},
        {"assignment_cycle_id": {"$in": [ORIGINAL_CYCLE_ID, DUPLICATE_CYCLE_ID]}},
        {"phone": lead.get("phone")},
    ]}
    visits = _find(db, "visitas", visit_query, session=session)
    visit_orders = _find(db, "ordenes_visitas", visit_query, session=session)
    tasks = _find(db, "crm_tasks", visit_query, session=session)
    pending = _find(db, "pending_notifications", {"$or": [
        {"lead_id": {"$in": [ObjectId(LEAD_OID), LEAD_OID]}},
        {"phone": lead.get("phone")}, {"telefono": lead.get("phone")},
        {"lead_phone": lead.get("phone")},
    ]}, session=session)
    notifications = _find(db, "crm_notifications_v1", {"$or": [
        {"assignment_cycle_id": ORIGINAL_CYCLE_ID},
        {"assignment_cycle_ids": ORIGINAL_CYCLE_ID},
    ]}, session=session)
    try:
        provider_acceptance = _provider_acceptance_evidence(notifications)
    except RepairSafetyError:
        return None
    by_id = {str(row.get("assignment_cycle_id")): row for row in cycles}
    original, duplicate = by_id.get(ORIGINAL_CYCLE_ID), by_id.get(DUPLICATE_CYCLE_ID)
    lifecycle = lead.get("lifecycle") or {}
    prospect = lead.get("prospecto") or {}
    visit_follow_up = prospect.get("visit_follow_up") or {}
    visit_preference = prospect.get("visit_preference") or {}
    sla_segments = (original or {}).get("sla_segments") or []
    resolved_alerts = [row for row in alerts if str(row.get("_id")) in {
        "assignment-integrity:49e4aaa7296dce6ebbd29eb1",
        "assignment-integrity:e9b9e1705c57a16604b422f7",
    }]
    active_conflicts = [row for row in alerts if row.get("status") == "open"
                        and "cycle_pointer_mismatch" in ((row.get("evidence") or {}).get("issues") or [])]
    restoration_history = (original or {}).get("administrative_state_history") or []
    complete_reversal_recorded = any(
        row.get("incident_id") == INCIDENT_ID
        and row.get("action") == "restore_original_active_cycle_after_duplicate_prop360_cycle"
        and row.get("duplicate_cycle_id") == DUPLICATE_CYCLE_ID
        and _utc(row.get("sla_started_at_corrected_to_provider_acceptance")) == provider_acceptance["accepted_at"]
        for row in restoration_history
    )
    if (not original or not duplicate or not audit
            or lead.get("ejecutivo_asignado") != MARIELA_NAME
            or prospect.get("ejecutivo") != MARIELA_NAME
            or lead.get("lead_temperature_effective") != "HOT"
            or lead.get("lead_temperature") != "HOT"
            or lifecycle.get("current_assignment_cycle_id") != ORIGINAL_CYCLE_ID
            or lifecycle.get("assignment_cycle_id") != ORIGINAL_CYCLE_ID
            or lifecycle.get("assigned_to_user_id") != MARIELA_USER_ID
            or lifecycle.get("assigned_to_display_name") != MARIELA_NAME
            or _utc(lifecycle.get("assigned_at")) != _utc(original.get("assigned_at"))
            or not _active(original) or _active(duplicate)
            or original.get("closed_at") is not None
            or original.get("closed_reason") is not None
            or not complete_reversal_recorded
            or duplicate.get("administratively_corrected") is not True
            or duplicate.get("corrects_assignment_cycle_id") != ORIGINAL_CYCLE_ID
            or duplicate.get("sla_performance_excluded") is not True
            or duplicate.get("notification_eligible") is not False
            or original.get("sla_first_management_status") != "completed"
            or original.get("temperature_evidence_event_id") != HOT_INTENT_EVENT_ID
            or _utc(lifecycle.get("sla_started_at")) != provider_acceptance["accepted_at"]
            or _utc(original.get("sla_started_at")) != provider_acceptance["accepted_at"]
            or _utc(lifecycle.get("cycle_started_at")) != _utc(original.get("cycle_started_at") or original.get("assigned_at"))
            or _utc(lifecycle.get("first_contact_attempt_at")) != _utc(original.get("first_contact_attempt_at"))
            or _utc(lifecycle.get("first_effective_contact_at")) != _utc(original.get("first_effective_contact_at"))
            or _utc(lifecycle.get("first_valid_management_at")) != _utc(original.get("first_valid_management_at"))
            or lifecycle.get("first_valid_management_actor") != MARIELA_USER_ID
            or lifecycle.get("sla_first_management_status") != "completed"
            or _utc(lifecycle.get("hot_since")) != HOT_SINCE_AT
            or _utc(lifecycle.get("temperature_transitioned_at")) != HOT_SINCE_AT
            or lead.get("sla_status") != "fulfilled"
            or _id_set(management) != {VALID_MANAGEMENT_ID}
            or management[0].get("result_type") != "EFFECTIVE_CONTACT"
            or management[0].get("actor_user_id") != MARIELA_USER_ID
            or management[0].get("status") != "completed"
            or _utc(management[0].get("occurred_at")) != _utc(original.get("first_valid_management_at"))
            or visit_follow_up.get("status") != "pending"
            or visit_follow_up.get("type") != "VISIT_COORDINATION"
            or visit_follow_up.get("assignment_cycle_id") != ORIGINAL_CYCLE_ID
            or visit_follow_up.get("property_id") != "17252"
            or visit_follow_up.get("availability") != VISIT_AVAILABILITY
            or _utc(visit_follow_up.get("requested_at")) != VISIT_REQUEST_AT
            or visit_follow_up.get("automation_eligible") is not False
            or visit_follow_up.get("appointment_confirmed") is not False
            or lead.get("visit_follow_up_status") != "pending"
            or visit_preference.get("text") != VISIT_AVAILABILITY
            or visit_preference.get("evidence_event_ids") != [VISIT_DAY_EVENT_ID, VISIT_HOURS_EVENT_ID]
            or active_conflicts
            or any(row.get("status") != "resolved" for row in resolved_alerts)
            or len(resolved_alerts) != 2 or len(cycles) != 2
            or visits or visit_orders or tasks or pending
            or audit.get("meta", {}).get("customer_message_sent") is not False
            or audit.get("meta", {}).get("appointment_created") is not False):
        return None
    if (len(sla_segments) != 2
            or sla_segments[0].get("policy") != "NON_HOT"
            or _utc(sla_segments[0].get("segment_start")) != _utc(original.get("sla_started_at"))
            or _utc(sla_segments[0].get("segment_end")) != HOT_SINCE_AT
            or sla_segments[0].get("end_reason") != "intent_hot"
            or sla_segments[1].get("policy") != "HOT"
            or _utc(sla_segments[1].get("segment_start")) != HOT_SINCE_AT
            or sla_segments[1].get("segment_end") is not None):
        return None
    chatbot_owner = None
    if session is None:
        try:
            from chatbot.crm_assignment_integrity import assignment_context
            chatbot_owner = assignment_context(db, lead)
        except Exception:
            return None
        if (chatbot_owner.get("status") != "consistent"
                or chatbot_owner.get("owner_name") != MARIELA_NAME
                or (chatbot_owner.get("cycle") or {}).get("assignment_cycle_id") != ORIGINAL_CYCLE_ID):
            return None
    return {
        "owner": lead.get("ejecutivo_asignado"),
        "chatbot_owner_status": (chatbot_owner or {}).get("status", "consistent"),
        "chatbot_owner": (chatbot_owner or {}).get("owner_name", MARIELA_NAME),
        "active_cycle_id": original.get("assignment_cycle_id"),
        "cycle_count": len(cycles),
        "duplicate_cycle_status": duplicate.get("cycle_status"),
        "original_sla_started_at": _utc(original.get("sla_started_at")).isoformat(),
        "provider_acceptance_at": provider_acceptance["accepted_at"].isoformat(),
        "provider_acceptance_source": provider_acceptance["timestamp_source"],
        "provider_message_id": provider_acceptance["provider_message_id"],
        "sla_status": lead.get("sla_status"),
        "effective_contact_count": len(management),
        "visit_follow_up_status": visit_follow_up.get("status"),
        "visit_preference": visit_follow_up.get("availability"),
        "resolved_cycle_pointer_alerts": len(resolved_alerts),
        "appointment_created": False,
        "customer_message_sent": False,
    }


def _cycle_filter(cycle: Mapping[str, Any]) -> dict[str, Any]:
    query = {
        "_id": cycle["_id"],
        "assignment_cycle_id": cycle["assignment_cycle_id"],
        "lead_id": cycle["lead_id"],
        "assigned_to_user_id": cycle["assigned_to_user_id"],
        "assigned_at": cycle["assigned_at"],
        "sla_started_at": cycle["sla_started_at"],
        "cycle_status": cycle.get("cycle_status"),
        "unassigned_at": cycle.get("unassigned_at"),
    }
    if "cycle_version" in cycle:
        query["cycle_version"] = cycle["cycle_version"]
    else:
        query["cycle_version"] = {"$exists": False}
    return query


def _require_one(result: Any, label: str) -> None:
    if getattr(result, "matched_count", 0) != 1:
        raise RepairSafetyError(f"concurrency check failed at {label}; the transaction will abort")


def _apply(client: MongoClient, db: Any, backup: Mapping[str, Any], manifest: Mapping[str, Any], *, actor: str) -> str:
    now = datetime.now(timezone.utc)

    def transaction(session: Any) -> str:
        if _verify_applied(db, session=session):
            return "already_applied"
        snapshot = collect_snapshot(db, backup, session=session)
        if _snapshot_manifest(snapshot) != manifest.get("snapshot"):
            raise RepairSafetyError("live state changed after preflight; rerun --dry-run and review")
        lead, original, duplicate, result, provider_acceptance = validate_scenario(backup, snapshot)
        expected_acceptance = {
            **provider_acceptance,
            "accepted_at": provider_acceptance["accepted_at"].isoformat(),
        }
        if manifest.get("provider_acceptance") != expected_acceptance:
            raise RepairSafetyError("provider acceptance proof changed after preflight; rerun --dry-run")
        hot_at = _utc((lead.get("lifecycle") or {}).get("hot_since"))
        previous_sla_start = _utc(original.get("sla_started_at"))
        original_start = provider_acceptance["accepted_at"]
        if hot_at != HOT_SINCE_AT or not original_start or hot_at <= original_start:
            raise RepairSafetyError("original SLA/HOT evidence is invalid")
        segments = [
            {"policy": "NON_HOT", "segment_start": original_start,
             "segment_end": hot_at, "end_reason": "intent_hot"},
            {"policy": "HOT", "segment_start": hot_at,
             "segment_end": None, "end_reason": None},
        ]

        # Close the duplicate first inside the same transaction. The partial
        # unique index on one active cycle per lead is enforced per write, so
        # activating the original first can fail even though the transaction
        # would later close the duplicate. Atomic commit keeps intermediate
        # state invisible to readers.
        duplicate_update = db["crm_assignment_cycles"].update_one(
            _cycle_filter(duplicate),
            {"$set": {
                "cycle_status": "closed", "unassigned_at": now,
                "closed_at": now,
                "closed_reason": "duplicate_prop360_same_assignment_corrected",
                "cycle_version": int(duplicate.get("cycle_version") or 0) + 1,
                "administratively_corrected": True,
                "correction_incident_id": INCIDENT_ID,
                "corrects_assignment_cycle_id": ORIGINAL_CYCLE_ID,
                "corrected_by": actor, "corrected_at": now,
                "correction_reason": "same phone/property/executive; later Prop360 event duplicated an already managed cycle",
                "notification_eligible": False,
                "sla_performance_excluded": True,
                "sla_performance_exclusion_reason": "duplicate Prop360 cycle; first management belongs to original cycle",
                "sla_first_management_status": "excluded_duplicate_cycle",
                "updated_at": now,
            }},
            session=session,
        )
        _require_one(duplicate_update, "duplicate cycle closure")

        original_update = db["crm_assignment_cycles"].update_one(
            _cycle_filter(original),
            {"$set": {
                "cycle_status": "active", "unassigned_at": None,
                "sla_started_at": original_start,
                "cycle_version": int(original.get("cycle_version") or 0) + 1,
                "temperature_at_assignment": "HOT",
                "hot_started_at": hot_at,
                "temperature_transitioned_at": hot_at,
                "temperature_evidence_event_id": HOT_INTENT_EVENT_ID,
                "sla_segments": segments,
                "assignment_cycle_restored_at": now,
                "assignment_cycle_restored_by": actor,
                "updated_at": now,
            }, "$push": {"administrative_state_history": {
                "incident_id": INCIDENT_ID,
                "action": "restore_original_active_cycle_after_duplicate_prop360_cycle",
                "actor": actor,
                "at": now,
                "prior_cycle_status": original.get("cycle_status"),
                "prior_unassigned_at": original.get("unassigned_at"),
                "prior_closed_at": original.get("closed_at"),
                "prior_closed_reason": original.get("closed_reason"),
                "duplicate_cycle_id": DUPLICATE_CYCLE_ID,
                "prior_sla_started_at": previous_sla_start,
                "sla_started_at_corrected_to_provider_acceptance": original_start,
                "provider_acceptance_evidence": {
                    key: value for key, value in provider_acceptance.items() if key != "accepted_at"
                } | {"accepted_at": original_start},
                "first_management_preserved": original.get("first_valid_management_at"),
            }}, "$unset": {"closed_at": "", "closed_reason": ""}},
            session=session,
        )
        _require_one(original_update, "original cycle activation")

        lead_id = lead["_id"]
        preference = dict((lead.get("prospecto") or {}).get("visit_preference") or {})
        preference.update({
            "text": VISIT_AVAILABILITY,
            "property_id": "17252",
            "evidence_event_ids": [VISIT_DAY_EVENT_ID, VISIT_HOURS_EVENT_ID],
        })
        visit_follow_up = {
            "status": "pending",
            "type": "VISIT_COORDINATION",
            "property_id": "17252",
            "availability": VISIT_AVAILABILITY,
            "assignment_cycle_id": ORIGINAL_CYCLE_ID,
            "requested_at": VISIT_REQUEST_AT,
            "evidence_event_ids": [VISIT_DAY_EVENT_ID, VISIT_HOURS_EVENT_ID],
            "automation_eligible": False,
            "appointment_confirmed": False,
            "updated_at": now,
        }
        lead_selector = {
            "_id": lead_id,
            "ejecutivo_asignado": MARIELA_NAME,
            "prospecto.ejecutivo": MARIELA_NAME,
            "lifecycle.current_assignment_cycle_id": DUPLICATE_CYCLE_ID,
            "lifecycle.assignment_cycle_id": ORIGINAL_CYCLE_ID,
            "lifecycle.assigned_to_user_id": MARIELA_USER_ID,
            "lifecycle.sla_started_at": lead["lifecycle"]["sla_started_at"],
            "sla_status": "fulfilled",
            "updated_at": lead.get("updated_at"),
        }
        lead_update = db["leads"].update_one(
            lead_selector,
            {"$set": {
                "lifecycle.current_assignment_cycle_id": ORIGINAL_CYCLE_ID,
                "lifecycle.assignment_cycle_id": ORIGINAL_CYCLE_ID,
                "lifecycle.assigned_at": original.get("assigned_at"),
                "lifecycle.cycle_started_at": original.get("cycle_started_at") or original.get("assigned_at"),
                "lifecycle.assigned_to_user_id": MARIELA_USER_ID,
                "lifecycle.assigned_to_display_name": MARIELA_NAME,
                "lifecycle.sla_started_at": original_start,
                "lifecycle.first_contact_attempt_at": original.get("first_contact_attempt_at"),
                "lifecycle.first_effective_contact_at": original.get("first_effective_contact_at"),
                "lifecycle.first_valid_management_at": original.get("first_valid_management_at"),
                "lifecycle.first_valid_management_actor": original.get("first_valid_management_actor"),
                "lifecycle.sla_first_management_status": "completed",
                "lifecycle.hot_since": hot_at,
                "lifecycle.temperature_transitioned_at": hot_at,
                "ejecutivo_asignado": MARIELA_NAME,
                "prospecto.ejecutivo": MARIELA_NAME,
                "prospecto.visit_preference": preference,
                "prospecto.visit_follow_up": visit_follow_up,
                "visit_follow_up_status": "pending",
                "visit_follow_up_required": True,
                "updated_at": now.isoformat(),
                "last_crm_update": now,
            }},
            session=session,
        )
        _require_one(lead_update, "lead pointers, visit follow-up, and owner mirrors")

        db["crm_events"].insert_one({
            "_id": AUDIT_EVENT_ID,
            "lead_id": lead_id,
            "assignment_cycle_id": ORIGINAL_CYCLE_ID,
            "actor": actor,
            "actor_type": "administrator",
            "type": "ADMINISTRATIVE_CYCLE_REPAIR",
            "result": "DUPLICATE_CYCLE_MERGED",
            "confirmed": True,
            "timestamp": now,
            "source": "p0_incident_remediation",
            "meta": {
                "incident_id": INCIDENT_ID,
                "original_cycle_id": ORIGINAL_CYCLE_ID,
                "duplicate_cycle_id": DUPLICATE_CYCLE_ID,
                "prop360_event_id": PROP360_EVENT_ID,
                "preserved_management_result_id": VALID_MANAGEMENT_ID,
                "prior_sla_started_at": previous_sla_start,
                "sla_started_at_corrected_to_provider_acceptance": original_start,
                "provider_acceptance_evidence": {
                    key: value for key, value in provider_acceptance.items() if key != "accepted_at"
                } | {"accepted_at": original_start},
                "hot_intent_event_id": HOT_INTENT_EVENT_ID,
                "visit_availability_event_ids": [VISIT_DAY_EVENT_ID, VISIT_HOURS_EVENT_ID],
                "appointment_created": False,
                "customer_message_sent": False,
                "automatic_whatsapp_sent": False,
            },
        }, session=session)

        for alert in snapshot["integrity_alerts"]:
            resolved = db["crm_assignment_integrity_alerts"].update_one(
                {"_id": alert["_id"], "status": "open",
                 "issue_code": alert.get("issue_code"),
                 "evidence.signature": (alert.get("evidence") or {}).get("signature")},
                {"$set": {
                    "status": "resolved", "resolved_at": now, "resolved_by": actor,
                    "resolution": "cycle pointers aligned to original managed cycle; duplicate retained as excluded history",
                    "correction_incident_id": INCIDENT_ID,
                    "correction_cycle_id": ORIGINAL_CYCLE_ID,
                }},
                session=session,
            )
            _require_one(resolved, "cycle-pointer alert resolution")

        if not _verify_applied(db, session=session):
            raise RepairSafetyError("in-transaction invariant verification failed; all writes are being rolled back")
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
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture-backup", action="store_true", help="save a fresh, checksummed production snapshot; no DB writes")
    mode.add_argument("--dry-run", action="store_true", help="validate current production state and write a preflight; no DB changes")
    mode.add_argument("--execute", action="store_true", help="apply repair in one Mongo transaction")
    parser.add_argument("--backup", type=Path, help="path to the immutable pre-repair BSON/JSON backup")
    parser.add_argument("--backup-out", type=Path, help="new path for --capture-backup output")
    parser.add_argument("--preflight", type=Path, help="checksummed live preflight from --dry-run")
    parser.add_argument("--preflight-out", type=Path, help="new path for --dry-run preflight output")
    parser.add_argument("--database", default=DATABASE)
    parser.add_argument("--actor", required=True, help="human administrator identity for the audit event")
    parser.add_argument("--confirm-incident", help=f"required with --execute: {CONFIRMATION}")
    args = parser.parse_args(argv)
    if args.database != DATABASE:
        parser.error(f"this repair is restricted to database {DATABASE}")
    if not args.actor.strip() or args.actor.strip().lower() in {"system", "bot", "sistema", "assistant"}:
        parser.error("--actor must identify a human administrator")
    if args.execute and (not args.preflight or args.confirm_incident != CONFIRMATION):
        parser.error(f"--execute requires --preflight and --confirm-incident {CONFIRMATION}")
    if args.dry_run and args.preflight:
        parser.error("--preflight-out is for --dry-run; --preflight is for --execute")
    if args.capture_backup:
        if not args.backup_out or args.backup or args.preflight or args.preflight_out or args.confirm_incident:
            parser.error("--capture-backup requires only --backup-out")
    elif not args.backup or args.backup_out:
        parser.error("--dry-run/--execute require --backup, and --backup-out is only for --capture-backup")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    backup_path = args.backup.expanduser().resolve() if args.backup else None
    backup = load_backup(backup_path) if backup_path else None
    backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest() if backup_path else None
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
    if not uri:
        print("ERROR: set MONGODB_URI or MONGO_URI in the authorized runtime; no credential is accepted on the command line.", file=sys.stderr)
        return 2
    client = MongoClient(uri, appName="procasa-p0-17252-admin-repair", retryWrites=True,
                         connectTimeoutMS=5000, serverSelectionTimeoutMS=15000, socketTimeoutMS=30000)
    try:
        db = client[DATABASE]
        production_identity = _validate_production_identity(client, db)
        if args.capture_backup:
            snapshot = collect_snapshot(db)
            live_backup = _backup_from_snapshot(snapshot)
            backup_path = args.backup_out.expanduser().resolve()
            backup_sha256 = _write_backup(backup_path, live_backup)
            # Persist the complete snapshot before checking its expected incident
            # shape so a failed preflight still leaves a forensic backup.
            validate_scenario(live_backup, snapshot)
            print("STATUS: production_backup_captured; no database writes performed")
            print(f"BACKUP: {backup_path}")
            print(f"BACKUP_SHA256: {backup_sha256}")
            print(f"DATABASE_IDENTITY: {DATABASE}; confirmed Procasa production replica set")
            print("PROVIDER_ACCEPTANCE: checked against a provider 2xx response; delivery timestamps excluded")
            return 0
        if args.dry_run:
            already = _verify_applied(db)
            if already:
                print("STATUS: already_applied; invariants verified; no writes performed")
                print("PRODUCTION_VERIFICATION: " + json.dumps(already, ensure_ascii=False, sort_keys=True))
                return 0
            snapshot = collect_snapshot(db, backup)
            _, _, _, _, provider_acceptance = validate_scenario(backup, snapshot)
            output = args.preflight_out or backup_path.with_name("procasa-p0-17252-live-preflight.json")
            manifest = {
                "format": "procasa-p0-17252-live-preflight-v1",
                "incident_id": INCIDENT_ID,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "database": DATABASE,
                "lead_id": LEAD_OID,
                "actor": args.actor.strip(),
                "backup_file": backup_path.name,
                "backup_sha256": backup_sha256,
                "production_identity": production_identity,
                "provider_acceptance": {
                    **provider_acceptance,
                    "accepted_at": provider_acceptance["accepted_at"].isoformat(),
                },
                "snapshot": _snapshot_manifest(snapshot),
            }
            _write_manifest(output, manifest)
            print("STATUS: dry_run_passed; no database writes performed")
            print(f"PREFLIGHT: {output}")
            print(f"BACKUP_SHA256: {backup_sha256}")
            print("CYCLES: 2 retained; duplicate excluded from SLA performance")
            print("SLA: provider acceptance used as start; original EFFECTIVE_CONTACT and its timestamp preserved")
            print("VISIT: pending internal coordination recorded; no appointment created")
            print("CUSTOMER_MESSAGES: none")
            return 0

        manifest = _read_manifest(args.preflight.expanduser().resolve(),
                                  backup_sha256=backup_sha256, actor=args.actor.strip())
        if manifest.get("format") != "procasa-p0-17252-live-preflight-v1" or manifest.get("database") != DATABASE:
            raise RepairSafetyError("preflight format or database does not match")
        if manifest.get("production_identity") != production_identity:
            raise RepairSafetyError("preflight was created against a different production replica set")
        result = _apply(client, db, backup, manifest, actor=args.actor.strip())
        verified = _verify_applied(db)
        if not verified:
            raise RepairSafetyError("post-commit verification failed; do not declare the incident closed")
        print(f"STATUS: {result}")
        print("PRODUCTION_POSTCOMMIT_VERIFICATION: " + json.dumps(verified, ensure_ascii=False, sort_keys=True))
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
