#!/usr/bin/env python3
"""Transactionally restore the owner of Procasa lead 6732 after P0 incident.

This script is deliberately manual and fail-closed. It reads MongoDB access
from the process environment, uses a saved preflight between dry-run and
execution, performs all writes in one replica-set transaction, and verifies
the committed state. It never sends WhatsApp messages or creates an appointment.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid
from typing import Any, Mapping

from bson import ObjectId, json_util
from pymongo import MongoClient, ReadPreference
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern


INCIDENT_ID = "PROC-2026-10-P0-6732"
CONFIRMATION = "P0-6732-RESTORE-HERNAN"
DATABASE = "URLS"
BACKUP_FORMAT = "procasa-p0-6732-pre-repair-v3"
LEAD_OID = "6ad613470035d45d0f87595b"
PRODUCTION_CLUSTER_PREFIX = "ac-8f1k7c3-shard-"
PRODUCTION_CLUSTER_SUFFIX = ".mzve39k.mongodb.net"
HERNAN_CYCLE_ID = "0e966571-4e5d-495f-9330-4a8aecf232c0"
MARIA_CYCLE_ID = "36ad5606-91f8-40a7-8609-4bb6794c3def"
HERNAN_USER_ID = "6a681413140190dde11f26d1"
MARIA_USER_ID = "69c19b98fbbbf113235ba844"
MARIA_BREACH_ALERT_ID = f"{MARIA_CYCLE_ID}|breached|{MARIA_USER_ID}"
MARIA_BREACH_ALERT_PROVIDER_ID = "89720542"
MARIA_BREACH_ALERT_CREATED_AT = datetime(2026, 10, 19, 18, 29, 52, 875_000, tzinfo=timezone.utc)
MARIA_BREACH_ALERT_STARTED_AT = datetime(2026, 10, 19, 18, 30, 15, 290_000, tzinfo=timezone.utc)
MARIA_BREACH_ALERT_COMPLETED_AT = datetime(2026, 10, 19, 18, 30, 26, 193_000, tzinfo=timezone.utc)
HERNAN_NAME = "Hernán Castro"
MARIA_NAME = "María Paz Galleguillos"
VISIT_ACCEPTANCE_EVENT_ID = "6ad613bce66c461dd69d69aa"
VISIT_ACCEPTANCE_AT = datetime(2026, 10, 19, 12, 57, 32, 37_000, tzinfo=timezone.utc)
MANAGEMENT_RECORDS = {
    "crm_management:192709ed-61b2-4848-8a91-c22b8cf15ef7": datetime(
        2026, 10, 19, 12, 58, 50, 367_000, tzinfo=timezone.utc
    ),
    "crm_management:57bda719-dd4b-4503-8b49-a846431afa84": datetime(
        2026, 10, 19, 12, 59, 40, 207_000, tzinfo=timezone.utc
    ),
}
ADMINISTRATIVE_CORRECTION_ID = f"PROC_2026_10_P0_6732:{LEAD_OID}"
CORRECTION_CYCLE_ID = f"admin-correction:{ADMINISTRATIVE_CORRECTION_ID}:v1"
CORRECTION_EVENT_ID = f"crm_event:{INCIDENT_ID}:administrative-correction:v1"
CORRECTION_OBJECT_ID = ObjectId(hashlib.sha256(CORRECTION_CYCLE_ID.encode()).hexdigest()[:24])

BACKUP_COLLECTIONS = {
    "lead": "leads",
    "cycles": "crm_assignment_cycles",
    "management_results": "crm_management_results",
    "events": "crm_events",
    "notifications": "crm_notifications_v1",
    "legacy_pending_notifications": "pending_notifications",
    "ingest_events": "lead_ingest_events",
    "integrity_alerts": "crm_assignment_integrity_alerts",
    "sla_reassignment_audit": "crm_sla_reassignment_audit_v1",
    "sla_alerts": "crm_sla_alerts_v1",
}
CORE_COLLECTIONS = ("lead", "cycles", "management_results", "events", "notifications", "ingest_events", "sla_alerts")


class RepairSafetyError(RuntimeError):
    """Raised when live data does not match the reviewed incident snapshot."""


def _validate_production_identity(client: MongoClient, db: Any) -> dict[str, str]:
    hello = client.admin.command("hello")
    hosts = [str(value).rsplit(":", 1)[0] for value in (hello.get("hosts") or [])]
    if (not hello.get("setName") or not any(
            host.startswith(PRODUCTION_CLUSTER_PREFIX) and host.endswith(PRODUCTION_CLUSTER_SUFFIX)
            for host in hosts)):
        raise RepairSafetyError("connected MongoDB replica set is not the confirmed Procasa production cluster")
    stats = db.command("dbStats")
    if stats.get("db") != DATABASE or int(stats.get("collections") or 0) < 1:
        raise RepairSafetyError("connected database identity does not match the nonempty production URLS database")
    lead = db["leads"].find_one({"_id": ObjectId(LEAD_OID)}, {
        "ejecutivo_asignado": 1, "prospecto.codigo": 1,
    })
    if (not lead or lead.get("ejecutivo_asignado") != MARIA_NAME
            or (lead.get("prospecto") or {}).get("codigo") != "6732"):
        raise RepairSafetyError("production identity check did not find the expected María Paz/property 6732 lead")
    return {"database": DATABASE, "replica_set": str(hello["setName"]),
            "cluster_prefix": PRODUCTION_CLUSTER_PREFIX}


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, Mapping) and "$date" in value:
        return _utc(value["$date"])
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _canonical(value: Any) -> str:
    return json_util.dumps(value, json_options=json_util.CANONICAL_JSON_OPTIONS, sort_keys=True, separators=(",", ":"))


def _fingerprint(documents: list[Mapping[str, Any]]) -> str:
    rows = sorted(_canonical(dict(doc)) for doc in documents)
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _id_text(value: Any) -> str:
    return str(value)


def load_backup(path: Path) -> dict[str, Any]:
    try:
        backup = json_util.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RepairSafetyError(f"could not parse backup ({type(exc).__name__})") from exc
    if backup.get("format") != BACKUP_FORMAT:
        raise RepairSafetyError("backup format is not the reviewed P0 pre-repair snapshot")
    if backup.get("database") != DATABASE or str(backup.get("lead_id")) != LEAD_OID:
        raise RepairSafetyError("backup database or lead identity does not match this repair")
    for key in BACKUP_COLLECTIONS:
        bundle = backup.get("collections", {}).get(key)
        if not isinstance(bundle, Mapping) or bundle.get("collection") != BACKUP_COLLECTIONS[key]:
            raise RepairSafetyError(f"backup is missing the expected {key} collection")
        if not isinstance(bundle.get("documents"), list):
            raise RepairSafetyError(f"backup {key} snapshot is malformed")
    return backup


def _build_backup(db: Any, session: Any = None) -> dict[str, Any]:
    """Capture the reviewed 6732 scope from one consistent database snapshot."""
    options = {"session": session} if session is not None else {}
    lead = db["leads"].find_one({"_id": ObjectId(LEAD_OID)}, **options)
    if not lead:
        raise RepairSafetyError("target lead 6732 is absent from the snapshot")

    lead_ids = [ObjectId(LEAD_OID), LEAD_OID]
    phone = lead.get("phone")
    cycles = _find_many(db, "crm_assignment_cycles", {"lead_id": {"$in": lead_ids}}, session=session)
    cycle_ids = [str(row.get("assignment_cycle_id")) for row in cycles]
    management = _find_many(db, "crm_management_results", {"lead_id": {"$in": lead_ids}}, session=session)
    events = _find_many(db, "crm_events", {"lead_id": {"$in": lead_ids}}, session=session)
    notifications = _find_many(db, "crm_notifications_v1", {"$or": [
        {"assignment_cycle_id": {"$in": cycle_ids}},
        {"assignment_cycle_ids": {"$in": cycle_ids}},
        {"lead_id": {"$in": lead_ids}},
        {"lead_ids": {"$in": lead_ids}},
    ]}, session=session)
    ingest_events = _find_many(db, "lead_ingest_events", {"lead_id": {"$in": lead_ids},
                                                           "property_code": "6732"}, session=session)
    integrity_alerts = _find_many(db, "crm_assignment_integrity_alerts",
                                  {"lead_id": {"$in": lead_ids}}, session=session)
    sla_audit = _find_many(db, "crm_sla_reassignment_audit_v1", {"$or": [
        {"source_cycle_id": {"$in": cycle_ids}},
        {"new_cycle_id": {"$in": cycle_ids}},
        {"lead_id": {"$in": lead_ids}, "property_code": "6732"},
    ]}, session=session)
    sla_alerts = _find_many(db, "crm_sla_alerts_v1", {"assignment_cycle_id": {"$in": cycle_ids}}, session=session)
    pending = _find_many(db, "pending_notifications", {"$or": [
        {"lead_id": {"$in": lead_ids}}, {"phone": phone}, {"telefono": phone}, {"lead_phone": phone},
    ]}, session=session)

    documents = {
        "lead": [lead],
        "cycles": cycles,
        "management_results": management,
        "events": events,
        "notifications": notifications,
        "legacy_pending_notifications": pending,
        "ingest_events": ingest_events,
        "integrity_alerts": integrity_alerts,
        "sla_reassignment_audit": sla_audit,
        "sla_alerts": sla_alerts,
    }
    return {
        "format": BACKUP_FORMAT,
        "database": DATABASE,
        "lead_id": LEAD_OID,
        "created_at": datetime.now(timezone.utc),
        "collections": {
            key: {"collection": BACKUP_COLLECTIONS[key], "documents": rows}
            for key, rows in documents.items()
        },
    }


def _write_backup(path: Path, backup: Mapping[str, Any]) -> str:
    if path.exists():
        raise RepairSafetyError(f"backup output already exists: {path.name}; choose a new path")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json_util.dumps(backup, json_options=json_util.CANONICAL_JSON_OPTIONS, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backup_docs(backup: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    return list(backup["collections"][key]["documents"])


def _find_many(db: Any, collection: str, query: Mapping[str, Any], session: Any = None, limit: int = 10000):
    options = {"session": session} if session is not None else {}
    return list(db[collection].find(dict(query), **options).limit(limit))


def _snapshot_queries(backup: Mapping[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    lead_id = ObjectId(LEAD_OID)
    variants = [lead_id, LEAD_OID]
    lead_backup = _backup_docs(backup, "lead")[0]
    cycles = _backup_docs(backup, "cycles")
    cycle_ids = [str(row.get("assignment_cycle_id")) for row in cycles]
    notification_ids = [row.get("_id") for row in _backup_docs(backup, "notifications")]
    ingest_rows = _backup_docs(backup, "ingest_events")
    ingest_ids = [row.get("_id") for row in ingest_rows]
    source_event_ids = [str(row.get("source_event_id")) for row in ingest_rows if row.get("source_event_id")]
    phone = lead_backup.get("phone")
    return {
        "lead": ("leads", {"_id": lead_id}),
        "cycles": ("crm_assignment_cycles", {"lead_id": {"$in": variants}}),
        "management_results": ("crm_management_results", {"lead_id": {"$in": variants}}),
        "events": ("crm_events", {"lead_id": {"$in": variants}}),
        "notifications": ("crm_notifications_v1", {"$or": [
            {"_id": {"$in": notification_ids}},
            {"assignment_cycle_id": {"$in": cycle_ids}},
            {"assignment_cycle_ids": {"$in": cycle_ids}},
            {"lead_ids": {"$in": variants}},
        ]}),
        "pending_notifications": ("pending_notifications", {"$or": [
            {"lead_id": {"$in": variants}}, {"phone": phone}, {"telefono": phone}, {"lead_phone": phone},
        ]}),
        "ingest_events": ("lead_ingest_events", {"$or": [
            {"_id": {"$in": ingest_ids}}, {"source_event_id": {"$in": source_event_ids}},
            {"lead_id": {"$in": variants}, "property_code": "6732"},
        ]}),
        "integrity_alerts": ("crm_assignment_integrity_alerts", {"lead_id": {"$in": variants}}),
        "sla_audit": ("crm_sla_reassignment_audit_v1", {"$or": [
            {"source_cycle_id": {"$in": cycle_ids}}, {"new_cycle_id": {"$in": cycle_ids}},
            {"lead_id": {"$in": variants}, "property_code": "6732"},
        ]}),
        "sla_alerts": ("crm_sla_alerts_v1", {"assignment_cycle_id": {"$in": cycle_ids}}),
        "active_chat_jobs": ("chatbot_inbound_jobs", {"$and": [
            {"state": {"$in": ["received", "batching", "pending", "processing"]}},
            {"$or": [{"lead_id": {"$in": variants}}, {"phone": phone}]},
        ]}),
    }


def collect_snapshot(db: Any, backup: Mapping[str, Any], session: Any = None) -> dict[str, list[dict[str, Any]]]:
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for key, (collection, query) in _snapshot_queries(backup).items():
        snapshot[key] = _find_many(db, collection, query, session=session)
    return snapshot


def _validate_reviewed_sla_alerts(alerts: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Require the one known false breach alert and its exact provider evidence."""
    if len(alerts) != 1:
        raise RepairSafetyError("the 6732 cycles must have exactly one individually reviewed SLA alert")
    alert = dict(alerts[0])
    expected = {
        "_id": MARIA_BREACH_ALERT_ID,
        "assignment_cycle_id": MARIA_CYCLE_ID,
        "recipient_user_id": MARIA_USER_ID,
        "notification_type": "SLA_BREACH_WARNING",
        "state": "sent",
        "delivery_outcome": "confirmed",
        "provider_message_id": MARIA_BREACH_ALERT_PROVIDER_ID,
        "attempt_count": 1,
        "last_error": None,
    }
    if any(alert.get(key) != value for key, value in expected.items()):
        raise RepairSafetyError("the SLA alert identity or provider-accepted state differs from reviewed evidence")
    timestamps = {
        "created_at": MARIA_BREACH_ALERT_CREATED_AT,
        "delivery_started_at": MARIA_BREACH_ALERT_STARTED_AT,
        "delivery_completed_at": MARIA_BREACH_ALERT_COMPLETED_AT,
        "sent_at": MARIA_BREACH_ALERT_COMPLETED_AT,
    }
    if any(_utc(alert.get(key)) != value for key, value in timestamps.items()):
        raise RepairSafetyError("the SLA alert timestamps changed; refresh provider evidence before repair")
    if alert.get("delivered_at") is not None or alert.get("delivered_timestamp") is not None:
        raise RepairSafetyError("a provider delivery receipt appeared; review it before administrative correction")
    if alert.get("administrative_correction") is not None:
        raise RepairSafetyError("an SLA alert correction marker already exists; verify before continuing")
    return alert


def _sla_alert_concurrency_filter(alert: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "_id", "assignment_cycle_id", "recipient_user_id", "notification_type", "state",
        "delivery_started_at", "delivery_completed_at", "delivery_outcome", "provider_message_id",
        "attempt_count", "last_error", "created_at", "sent_at", "updated_at",
    )
    selector = {key: alert[key] for key in fields if key in alert}
    for key in fields:
        if key not in alert:
            selector[key] = {"$exists": False}
    selector["administrative_correction"] = {"$exists": False}
    return selector


def _sla_alert_correction_fields(alert: Mapping[str, Any], *, actor: str, now: datetime) -> dict[str, Any]:
    _validate_reviewed_sla_alerts([alert])
    return {
        "administratively_corrected": True,
        "sla_performance_excluded": True,
        "correction_incident_id": INCIDENT_ID,
        "administrative_correction": {
            "status": "corrected_false_sla_breach",
            "incident_id": INCIDENT_ID,
            "incorrect_assignment_cycle_id": MARIA_CYCLE_ID,
            "correction_cycle_id": CORRECTION_CYCLE_ID,
            "corrected_by": actor,
            "corrected_at": now,
            "reason": "the Prop360-created assignment duplicated a lead whose original first-management SLA was already fulfilled",
            "provider_state_preserved": alert.get("state"),
            "provider_outcome_preserved": alert.get("delivery_outcome"),
            "provider_message_id_preserved": alert.get("provider_message_id"),
            "provider_acceptance_confirmed": True,
            "provider_delivery_receipt_confirmed": False,
            "penalty_excluded": True,
            "resend": False,
        },
    }


def _snapshot_manifest(snapshot: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, Any]:
    return {
        key: {"count": len(rows), "sha256": _fingerprint(list(rows))}
        for key, rows in sorted(snapshot.items())
    }


def _validate_backup_scenario(backup: Mapping[str, Any], snapshot: Mapping[str, list[Mapping[str, Any]]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    lead_docs = _backup_docs(backup, "lead")
    backup_cycles = _backup_docs(backup, "cycles")
    backup_results = _backup_docs(backup, "management_results")
    backup_events = _backup_docs(backup, "events")
    if len(lead_docs) != 1 or len(backup_cycles) != 2 or len(backup_results) != 2:
        raise RepairSafetyError("backup does not contain exactly one lead, two cycles, and two management results")
    lead = snapshot["lead"][0] if len(snapshot["lead"]) == 1 else None
    if not lead:
        raise RepairSafetyError("live lead 6732 is missing or ambiguous")
    if lead.get("ejecutivo_asignado") != MARIA_NAME or (lead.get("prospecto") or {}).get("ejecutivo") != MARIA_NAME:
        raise RepairSafetyError("live lead owner mirrors changed; review the current assignment before repair")
    lifecycle = lead.get("lifecycle") or {}
    if lifecycle.get("current_assignment_cycle_id") != MARIA_CYCLE_ID or lifecycle.get("assignment_cycle_id") != MARIA_CYCLE_ID:
        raise RepairSafetyError("live lead cycle pointers changed; this is no longer the reviewed pre-repair state")
    if lifecycle.get("assigned_to_user_id") != MARIA_USER_ID or lead.get("sla_status") != "fulfilled":
        raise RepairSafetyError("live owner identity or fulfilled SLA state differs from the reviewed preflight")
    if (lead.get("prospecto") or {}).get("link_detectado") is not True:
        raise RepairSafetyError("lead identity-link state changed; the administrative exception needs review")

    backup_cycle_by_id = {str(row.get("assignment_cycle_id")): row for row in backup_cycles}
    live_cycle_by_id = {str(row.get("assignment_cycle_id")): row for row in snapshot["cycles"]}
    if set(backup_cycle_by_id) != {HERNAN_CYCLE_ID, MARIA_CYCLE_ID} or set(live_cycle_by_id) != set(backup_cycle_by_id):
        raise RepairSafetyError("cycle identities changed or a new cycle appeared after the backup")
    hernan = live_cycle_by_id[HERNAN_CYCLE_ID]
    maria = live_cycle_by_id[MARIA_CYCLE_ID]
    first_management_at = min(MANAGEMENT_RECORDS.values())
    if (hernan.get("assigned_to_user_id") != HERNAN_USER_ID or hernan.get("assigned_to_display_name") != HERNAN_NAME
            or hernan.get("cycle_status") != "closed"
            or _utc(hernan.get("sla_started_at")) != _utc(backup_cycle_by_id[HERNAN_CYCLE_ID].get("sla_started_at"))
            or _utc(hernan.get("first_valid_management_at")) != first_management_at
            or hernan.get("sla_first_management_status") != "completed"):
        raise RepairSafetyError("Hernán's original cycle or SLA start differs from the backup")
    if (maria.get("assigned_to_user_id") != MARIA_USER_ID or maria.get("assigned_to_display_name") != MARIA_NAME
            or maria.get("cycle_status") != "active" or maria.get("unassigned_at") is not None
            or str(maria.get("source_event_id")) != "12272"):
        raise RepairSafetyError("María's active Prop360 cycle differs from the reviewed incident")

    expected_results = {str(row.get("_id")): row for row in backup_results}
    live_results = {str(row.get("_id")): row for row in snapshot["management_results"]}
    if set(expected_results) != set(MANAGEMENT_RECORDS) or set(live_results) != set(expected_results):
        raise RepairSafetyError("management-result identities changed; refusing to touch the SLA history")
    for record_id, occurred_at in MANAGEMENT_RECORDS.items():
        expected = expected_results[record_id]
        current = live_results[record_id]
        if (expected.get("result_type") != "EFFECTIVE_CONTACT" or current.get("result_type") != "EFFECTIVE_CONTACT"
                or current.get("assignment_cycle_id") != HERNAN_CYCLE_ID
                or current.get("actor_user_id") != HERNAN_USER_ID
                or _utc(current.get("occurred_at")) != occurred_at or current.get("status") != "completed"):
            raise RepairSafetyError("one of Hernán's two completed EFFECTIVE_CONTACT records changed")

    backup_sla_alerts = _backup_docs(backup, "sla_alerts")
    reviewed_alert = _validate_reviewed_sla_alerts(snapshot["sla_alerts"])
    if (_fingerprint(backup_sla_alerts) != _fingerprint(snapshot["sla_alerts"])
            or str(reviewed_alert.get("_id")) != MARIA_BREACH_ALERT_ID):
        raise RepairSafetyError("the individually reviewed false SLA alert changed after the backup")

    expected_event_ids = {str(row.get("_id")) for row in backup_events}
    live_event_ids = {str(row.get("_id")) for row in snapshot["events"]}
    if len(backup_events) != 30 or live_event_ids != expected_event_ids:
        raise RepairSafetyError("event history changed after the backup; create a new reviewed preflight")
    acceptance = next((row for row in snapshot["events"] if str(row.get("_id")) == VISIT_ACCEPTANCE_EVENT_ID), None)
    if (not acceptance or acceptance.get("type") != "msg_in" or _utc(acceptance.get("timestamp")) != VISIT_ACCEPTANCE_AT
            or "09:00" not in str((acceptance.get("meta") or {}).get("text") or "")):
        raise RepairSafetyError("the contextual visit-preference evidence differs from the reviewed event")

    for key in ("notifications", "ingest_events"):
        expected_ids = {str(row.get("_id")) for row in _backup_docs(backup, key)}
        live_ids = {str(row.get("_id")) for row in snapshot[key]}
        if live_ids != expected_ids:
            raise RepairSafetyError(f"{key} identities changed after the backup")
    if snapshot["pending_notifications"] or snapshot["active_chat_jobs"]:
        raise RepairSafetyError("a customer-message job is pending for this lead; resolve it before repair")
    return lead, hernan, maria


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    if path.exists():
        raise RepairSafetyError(f"preflight output already exists: {path.name}; choose a new path")
    manifest["manifest_sha256"] = _manifest_digest(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_manifest(path: Path, backup_sha256: str, actor: str) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RepairSafetyError(f"could not parse preflight ({type(exc).__name__})") from exc
    if manifest.get("manifest_sha256") != _manifest_digest(manifest):
        raise RepairSafetyError("preflight checksum does not match")
    if manifest.get("incident_id") != INCIDENT_ID or manifest.get("backup_sha256") != backup_sha256:
        raise RepairSafetyError("preflight belongs to another incident or backup")
    if manifest.get("actor") != actor:
        raise RepairSafetyError("execution actor differs from the reviewed preflight actor")
    if manifest.get("correction_cycle_id") != CORRECTION_CYCLE_ID:
        raise RepairSafetyError("preflight correction identity is invalid")
    return manifest


def _is_active(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("cycle_status") == "active" and value.get("unassigned_at") is None


def _verify_already_applied(db: Any, backup: Mapping[str, Any], session: Any = None) -> bool:
    options = {"session": session} if session is not None else {}
    correction = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": CORRECTION_CYCLE_ID}, **options)
    if not correction:
        return False
    lead = db["leads"].find_one({"_id": ObjectId(LEAD_OID)}, **options)
    maria = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": MARIA_CYCLE_ID}, **options)
    hernan = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": HERNAN_CYCLE_ID}, **options)
    event = db["crm_events"].find_one({"_id": CORRECTION_EVENT_ID}, **options)
    corrected_alert = db["crm_sla_alerts_v1"].find_one({"_id": MARIA_BREACH_ALERT_ID}, **options)
    results = list(db["crm_management_results"].find({"lead_id": ObjectId(LEAD_OID)}, **options))
    if not lead or not maria or not hernan or not event or not corrected_alert:
        raise RepairSafetyError("a partial correction marker exists; transaction state needs manual review")
    alert_correction = corrected_alert.get("administrative_correction") or {}
    if (corrected_alert.get("state") != "sent"
            or corrected_alert.get("provider_message_id") != MARIA_BREACH_ALERT_PROVIDER_ID
            or corrected_alert.get("delivery_outcome") != "confirmed"
            or corrected_alert.get("sla_performance_excluded") is not True
            or corrected_alert.get("administratively_corrected") is not True
            or corrected_alert.get("correction_incident_id") != INCIDENT_ID
            or alert_correction.get("status") != "corrected_false_sla_breach"
            or alert_correction.get("correction_cycle_id") != CORRECTION_CYCLE_ID
            or alert_correction.get("provider_delivery_receipt_confirmed") is not False
            or alert_correction.get("resend") is not False):
        raise RepairSafetyError("the false SLA alert does not retain its provider evidence and correction audit")
    event_meta = event.get("meta") or {}
    if (event_meta.get("corrected_false_sla_alert_id") != MARIA_BREACH_ALERT_ID
            or event_meta.get("corrected_false_sla_alert_provider_message_id") != MARIA_BREACH_ALERT_PROVIDER_ID
            or event_meta.get("provider_acceptance_confirmed") is not True
            or event_meta.get("provider_delivery_receipt_confirmed") is not False
            or event_meta.get("false_sla_alert_resent") is not False):
        raise RepairSafetyError("the incident audit does not document the corrected false SLA alert")
    _validate_reviewed_sla_alerts([{
        **corrected_alert,
        "administrative_correction": None,
        "administratively_corrected": None,
        "sla_performance_excluded": None,
        "correction_incident_id": None,
    }])
    related_alerts = _find_many(db, "crm_sla_alerts_v1", {
        "assignment_cycle_id": {"$in": [HERNAN_CYCLE_ID, MARIA_CYCLE_ID, CORRECTION_CYCLE_ID]},
    }, session=session)
    if len(related_alerts) != 1 or str(related_alerts[0].get("_id")) != MARIA_BREACH_ALERT_ID:
        raise RepairSafetyError("an unexpected SLA alert appeared on the reviewed or administrative cycles")
    cycles = _find_many(db, "crm_assignment_cycles", {"lead_id": ObjectId(LEAD_OID)}, session=session)
    active = [row for row in cycles if _is_active(row)]
    if (len(active) != 1 or active[0].get("assignment_cycle_id") != CORRECTION_CYCLE_ID
            or len(cycles) != 3
            or active[0].get("assigned_to_user_id") != HERNAN_USER_ID
            or lead.get("ejecutivo_asignado") != HERNAN_NAME
            or (lead.get("prospecto") or {}).get("ejecutivo") != HERNAN_NAME
            or (lead.get("lifecycle") or {}).get("current_assignment_cycle_id") != CORRECTION_CYCLE_ID
            or (lead.get("lifecycle") or {}).get("assignment_cycle_id") != CORRECTION_CYCLE_ID
            or (lead.get("lifecycle") or {}).get("sla_started_at") != hernan.get("sla_started_at")
            or lead.get("sla_status") != "fulfilled"
            or _utc(hernan.get("first_valid_management_at")) != min(MANAGEMENT_RECORDS.values())
            or hernan.get("sla_first_management_status") != "completed"
            or maria.get("administratively_corrected") is not True
            or maria.get("correction_cycle_id") != CORRECTION_CYCLE_ID
            or correction.get("administrative_correction_id") != ADMINISTRATIVE_CORRECTION_ID
            or correction.get("cycle_origin") != "administrative_correction"
            or correction.get("reason") != "crm_list_admin"
            or correction.get("previous_assignment_cycle_id") != MARIA_CYCLE_ID
            or correction.get("corrects_assignment_cycle_id") != MARIA_CYCLE_ID
            or correction.get("restores_assignment_cycle_id") != HERNAN_CYCLE_ID
            or correction.get("sla_started_at") != hernan.get("sla_started_at")
            or correction.get("sla_first_management_status") != "completed"
            or correction.get("sla_performance_excluded") is not True
            or correction.get("notification_eligible") is not False):
        raise RepairSafetyError("existing correction markers do not satisfy post-commit invariants")
    expected = set(MANAGEMENT_RECORDS)
    results_by_id = {str(row.get("_id")): row for row in results}
    if set(results_by_id) != expected:
        raise RepairSafetyError("original management results changed after the repair")
    for record_id, occurred_at in MANAGEMENT_RECORDS.items():
        row = results_by_id[record_id]
        if (row.get("assignment_cycle_id") != HERNAN_CYCLE_ID
                or row.get("actor_user_id") != HERNAN_USER_ID
                or row.get("result_type") != "EFFECTIVE_CONTACT"
                or row.get("status") != "completed"
                or _utc(row.get("occurred_at")) != occurred_at):
            raise RepairSafetyError("one of Hernán's original EFFECTIVE_CONTACT records was altered")
    return True


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
        query["cycle_version"] = cycle.get("cycle_version")
    else:
        query["cycle_version"] = {"$exists": False}
    return query


def _require_one(result: Any, label: str) -> None:
    if getattr(result, "matched_count", 0) != 1:
        raise RepairSafetyError(f"concurrency check failed while updating {label}; transaction aborted")


def _corrected_sla_segments(hernan_cycle: Mapping[str, Any]) -> list[dict[str, Any]]:
    start = _utc(hernan_cycle.get("sla_started_at"))
    if not start or VISIT_ACCEPTANCE_AT <= start:
        raise RepairSafetyError("original SLA start or contextual HOT transition time is invalid")
    return [
        {"policy": "NON_HOT", "segment_start": start, "segment_end": VISIT_ACCEPTANCE_AT,
         "end_reason": "explicit_visit_preference_recorded"},
        {"policy": "HOT", "segment_start": VISIT_ACCEPTANCE_AT, "segment_end": None, "end_reason": None},
    ]


def _build_correction_cycle(lead: Mapping[str, Any], hernan: Mapping[str, Any], maria: Mapping[str, Any], *, actor: str, now: datetime) -> dict[str, Any]:
    auth = {
        "kind": "administrative", "actor": actor, "method": "crm_list_admin",
        "incident_id": INCIDENT_ID, "basis": "authorized P0 incident remediation",
    }
    markers = {
        key: hernan[key]
        for key in ("first_contact_attempt_at", "first_effective_contact_at", "first_valid_management_actor",
                    "first_valid_management_at", "last_management_result", "sla_first_management_status", "follow_up_required")
        if key in hernan
    }
    cycle = {
        "_id": CORRECTION_OBJECT_ID,
        "assignment_cycle_id": CORRECTION_CYCLE_ID,
        "lead_id": lead["_id"],
        "assigned_to_user_id": HERNAN_USER_ID,
        "assigned_to_display_name": HERNAN_NAME,
        "assigned_at": now,
        "cycle_started_at": now,
        # Carry the original commercial clock; this correction must never
        # start a new first-management SLA.
        "sla_started_at": hernan["sla_started_at"],
        "temperature_at_assignment": "HOT",
        "temperature_on_assignment": "HOT",
        "hot_started_at": VISIT_ACCEPTANCE_AT,
        "temperature_transitioned_at": VISIT_ACCEPTANCE_AT,
        "sla_segments": _corrected_sla_segments(hernan),
        "assigned_by": actor,
        "reason": "crm_list_admin",
        "transfer_authorization": auth,
        "transfer_authorized_at": now,
        "cycle_status": "active",
        "cycle_version": 1,
        "unassigned_at": None,
        "metric_version": hernan.get("metric_version", "crm_metrics_v1"),
        "schema_version": hernan.get("schema_version", "crm_assignment_cycle_v1"),
        "sla_policy_version": hernan.get("sla_policy_version"),
        "administrative_correction_id": ADMINISTRATIVE_CORRECTION_ID,
        "notification_eligible": False,
        "cycle_origin": "administrative_correction",
        "property_code": "6732",
        "source_system": "incident_remediation",
        "source_event_id": INCIDENT_ID,
        "previous_assignment_cycle_id": MARIA_CYCLE_ID,
        "corrects_assignment_cycle_id": MARIA_CYCLE_ID,
        "restores_assignment_cycle_id": HERNAN_CYCLE_ID,
        "correction_incident_id": INCIDENT_ID,
        "correction_authorized_by": actor,
        "sla_performance_excluded": True,
        "sla_performance_exclusion_reason": "administrative compensation cycle; original SLA and management remain on source cycle",
        "sla_alert_claims": {"red": {"status": "suppressed"}, "yellow": {"status": "suppressed"}},
        "sla_first_management_status": "completed",
        "sla_alerts_suppressed": True,
        "sla_alert_suppression_reason": "original SLA already fulfilled on Hernan source cycle",
        "updated_at": now,
        **markers,
    }
    return cycle


def _audit_event(lead: Mapping[str, Any], hernan: Mapping[str, Any], maria: Mapping[str, Any], *, actor: str, now: datetime) -> dict[str, Any]:
    return {
        "_id": CORRECTION_EVENT_ID,
        "lead_id": lead["_id"],
        "phone": lead.get("phone"),
        "assignment_cycle_id": CORRECTION_CYCLE_ID,
        "actor": actor,
        "actor_type": "administrator",
        "type": "ASSIGNMENT",
        "result": "ADMINISTRATIVE_CORRECTION",
        "confirmed": True,
        "timestamp": now,
        "source": "p0_incident_remediation",
        "idempotency_key": CORRECTION_EVENT_ID,
        "meta": {
            "incident_id": INCIDENT_ID,
            "action": "restore_assignment_without_restarting_sla",
            "corrected_assignment_cycle_id": MARIA_CYCLE_ID,
            "restored_from_assignment_cycle_id": HERNAN_CYCLE_ID,
            "original_sla_started_at": hernan.get("sla_started_at"),
            "preserved_management_record_ids": sorted(MANAGEMENT_RECORDS),
            "visit_preference_evidence_event_id": VISIT_ACCEPTANCE_EVENT_ID,
            "visit_preference_only_no_appointment_confirmed": True,
            "corrected_false_sla_alert_id": MARIA_BREACH_ALERT_ID,
            "corrected_false_sla_alert_provider_message_id": MARIA_BREACH_ALERT_PROVIDER_ID,
            "provider_acceptance_confirmed": True,
            "provider_delivery_receipt_confirmed": False,
            "false_sla_penalty_excluded": True,
            "false_sla_alert_resent": False,
            "customer_message_sent": False,
        },
    }


def apply_transaction(client: MongoClient, db: Any, backup: Mapping[str, Any], manifest: Mapping[str, Any], *, actor: str) -> str:
    now = datetime.now(timezone.utc)

    def transaction(session: Any) -> str:
        if _verify_already_applied(db, backup, session=session):
            return "already_applied"
        snapshot = collect_snapshot(db, backup, session=session)
        current_manifest = _snapshot_manifest(snapshot)
        if current_manifest != manifest.get("snapshot"):
            raise RepairSafetyError("live state changed after dry-run; refresh the preflight and review again")
        lead, hernan, maria = _validate_backup_scenario(backup, snapshot)
        _validate_reviewed_sla_alerts(snapshot["sla_alerts"])

        correction = _build_correction_cycle(lead, hernan, maria, actor=actor, now=now)
        segments = _corrected_sla_segments(hernan)
        hernan_update = db["crm_assignment_cycles"].update_one(
            _cycle_filter(hernan),
            {"$set": {
                "temperature_at_assignment": "HOT",
                "hot_started_at": VISIT_ACCEPTANCE_AT,
                "temperature_transitioned_at": VISIT_ACCEPTANCE_AT,
                "sla_segments": segments,
                "temperature_evidence_event_id": VISIT_ACCEPTANCE_EVENT_ID,
                "assignment_restoration_cycle_id": CORRECTION_CYCLE_ID,
                "assignment_restored_at": now,
                "updated_at": now,
                "cycle_version": int(hernan.get("cycle_version") or 0) + 1,
            }},
            session=session,
        )
        _require_one(hernan_update, "Hernán's original cycle")

        maria_update = db["crm_assignment_cycles"].update_one(
            _cycle_filter(maria),
            {"$set": {
                "cycle_status": "closed", "unassigned_at": now, "closed_at": now,
                "closed_reason": "administrative_system_misassignment_correction",
                "administratively_corrected": True,
                "correction_incident_id": INCIDENT_ID,
                "correction_cycle_id": CORRECTION_CYCLE_ID,
                "corrected_by": actor, "corrected_at": now,
                "correction_reason": "Prop360 overwrote an already managed lead; original Hernan cycle is restored",
                "correction_preserves_notification_history": True,
                "notification_eligible": False,
                "sla_performance_excluded": True,
                "sla_performance_exclusion_reason": "system misassignment corrected administratively; no penalty to assigned executive",
                "sla_pending_alerts_cancelled_at": now,
                "updated_at": now,
                "cycle_version": int(maria.get("cycle_version") or 0) + 1,
            }},
            session=session,
        )
        _require_one(maria_update, "María's historical cycle")

        # Cancel only alert records that have not started provider delivery.
        # Sent or uncertain alert history is never erased or rewritten.
        for alert in snapshot["sla_alerts"]:
            if alert.get("state") not in {"pending", "processing", "failed_retryable"}:
                continue
            alert_result = db["crm_sla_alerts_v1"].update_one(
                {"_id": alert["_id"], "state": alert.get("state"), "delivery_started_at": None},
                {"$set": {"state": "cancelled", "cancelled_at": now,
                           "cancellation_reason": "administrative_system_misassignment_correction",
                           "correction_incident_id": INCIDENT_ID, "updated_at": now,
                           "lease_owner": None, "lease_expires_at": None}},
                session=session,
            )
            _require_one(alert_result, "pending SLA alert")

        sent_false_breach_alert = _validate_reviewed_sla_alerts(snapshot["sla_alerts"])
        correction_result = db["crm_sla_alerts_v1"].update_one(
            _sla_alert_concurrency_filter(sent_false_breach_alert),
            {"$set": _sla_alert_correction_fields(sent_false_breach_alert, actor=actor, now=now)},
            session=session,
        )
        _require_one(correction_result, "provider-accepted false SLA alert correction marker")

        lead_selector = {
            "_id": lead["_id"],
            "ejecutivo_asignado": MARIA_NAME,
            "prospecto.ejecutivo": MARIA_NAME,
            "prospecto.link_detectado": True,
            "lifecycle.current_assignment_cycle_id": MARIA_CYCLE_ID,
            "lifecycle.assignment_cycle_id": MARIA_CYCLE_ID,
            "lifecycle.assigned_to_user_id": MARIA_USER_ID,
            "lifecycle.sla_started_at": maria.get("sla_started_at"),
            "sla_status": "fulfilled",
        }
        if "updated_at" in lead:
            lead_selector["updated_at"] = lead["updated_at"]
        lead_update = db["leads"].update_one(
            lead_selector,
            {"$set": {
                "ejecutivo_asignado": HERNAN_NAME,
                "prospecto.ejecutivo": HERNAN_NAME,
                "lifecycle.assigned_to_user_id": HERNAN_USER_ID,
                "lifecycle.assigned_to_display_name": HERNAN_NAME,
                "lifecycle.current_assignment_cycle_id": CORRECTION_CYCLE_ID,
                "lifecycle.assignment_cycle_id": CORRECTION_CYCLE_ID,
                "lifecycle.assigned_at": now,
                "lifecycle.cycle_started_at": now,
                "lifecycle.sla_started_at": hernan["sla_started_at"],
                "lifecycle.first_contact_attempt_at": hernan.get("first_contact_attempt_at"),
                "lifecycle.first_effective_contact_at": hernan.get("first_effective_contact_at"),
                "lifecycle.first_valid_management_at": hernan.get("first_valid_management_at"),
                "lifecycle.first_valid_management_actor": hernan.get("first_valid_management_actor"),
                "lifecycle.sla_first_management_status": "completed",
                "lifecycle.hot_since": VISIT_ACCEPTANCE_AT,
                "lifecycle.temperature_transitioned_at": VISIT_ACCEPTANCE_AT,
                "lifecycle.correction_incident_id": INCIDENT_ID,
                "lifecycle.correction_cycle_id": CORRECTION_CYCLE_ID,
                "lead_temperature": "HOT",
                "lead_temperature_effective": "HOT",
                "updated_at": now,
            }},
            session=session,
        )
        _require_one(lead_update, "lead ownership mirrors")

        db["crm_assignment_cycles"].insert_one(correction, session=session)
        db["crm_events"].insert_one(_audit_event(lead, hernan, maria, actor=actor, now=now), session=session)

        # Resolve any prior alert about this exact Prop360 overwrite without
        # deleting its evidence. A fresh reconciler run also recognizes the
        # correction markers and will not reopen it.
        old_alerts = _find_many(db, "crm_assignment_integrity_alerts", {
            "lead_id": lead["_id"],
            "issue_code": "managed_lead_reassigned_without_authorization",
            "evidence.previous_cycle_id": HERNAN_CYCLE_ID,
            "evidence.current_cycle_id": MARIA_CYCLE_ID,
        }, session=session)
        for alert in old_alerts:
            db["crm_assignment_integrity_alerts"].update_one(
                {"_id": alert["_id"], "status": alert.get("status")},
                {"$set": {"status": "resolved", "resolved_at": now, "resolved_by": actor,
                           "resolution": "administratively corrected; source management history preserved",
                           "correction_incident_id": INCIDENT_ID, "correction_cycle_id": CORRECTION_CYCLE_ID}},
                session=session,
            )
        if not _verify_already_applied(db, backup, session=session):
            raise RepairSafetyError("in-transaction invariant verification failed; all writes are being rolled back")
        return "committed"

    with client.start_session() as session:
        return session.with_transaction(
            transaction,
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority"),
            read_preference=ReadPreference.PRIMARY,
        )


def verify_postcommit(db: Any, backup: Mapping[str, Any]) -> dict[str, Any]:
    if not _verify_already_applied(db, backup):
        raise RepairSafetyError("post-commit verification did not find a complete correction")
    lead = db["leads"].find_one({"_id": ObjectId(LEAD_OID)})
    cycles = list(db["crm_assignment_cycles"].find({"lead_id": ObjectId(LEAD_OID)}))
    results = list(db["crm_management_results"].find({"lead_id": ObjectId(LEAD_OID)}))
    event = db["crm_events"].find_one({"_id": CORRECTION_EVENT_ID})
    false_alert = db["crm_sla_alerts_v1"].find_one({"_id": MARIA_BREACH_ALERT_ID})
    active = [row for row in cycles if _is_active(row)]
    maria = next(row for row in cycles if row.get("assignment_cycle_id") == MARIA_CYCLE_ID)
    corrected = next(row for row in cycles if row.get("assignment_cycle_id") == CORRECTION_CYCLE_ID)
    if (len(active) != 1 or lead.get("ejecutivo_asignado") != HERNAN_NAME
            or (lead.get("prospecto") or {}).get("ejecutivo") != HERNAN_NAME
            or (lead.get("lifecycle") or {}).get("sla_started_at") != active[0].get("sla_started_at")
            or lead.get("sla_status") != "fulfilled"
            or len(results) != 2 or any(row.get("result_type") != "EFFECTIVE_CONTACT" for row in results)
            or maria.get("administratively_corrected") is not True
            or corrected.get("sla_performance_excluded") is not True
            or event.get("meta", {}).get("customer_message_sent") is not False):
        raise RepairSafetyError("one or more post-commit business invariants failed")
    pending = _find_many(db, "pending_notifications", {"$or": [
        {"lead_id": ObjectId(LEAD_OID)}, {"phone": backup["collections"]["lead"]["documents"][0].get("phone")},
    ]})
    if pending:
        raise RepairSafetyError("a customer notification is queued after the transaction; do not send it automatically")
    return {
        "lead_owner": lead.get("ejecutivo_asignado"),
        "active_cycle_id": active[0].get("assignment_cycle_id"),
        "cycles_total": len(cycles),
        "maria_cycle_status": maria.get("cycle_status"),
        "maria_cycle_corrected": maria.get("administratively_corrected") is True,
        "hernan_effective_contacts": len(results),
        "sla_status": lead.get("sla_status"),
        "sla_started_at": _utc(active[0].get("sla_started_at")).isoformat() if _utc(active[0].get("sla_started_at")) else None,
        "correction_cycle_excluded_from_performance": corrected.get("sla_performance_excluded") is True,
        "maria_false_sla_alert_state_preserved": false_alert.get("state") if false_alert else None,
        "maria_false_sla_alert_provider_message_id": false_alert.get("provider_message_id") if false_alert else None,
        "maria_false_sla_alert_performance_excluded": bool(false_alert and false_alert.get("sla_performance_excluded") is True),
        "maria_false_sla_alert_delivery_receipt_confirmed": ((false_alert.get("administrative_correction") or {}).get("provider_delivery_receipt_confirmed") if false_alert else None),
        "maria_false_sla_alert_resent": ((false_alert.get("administrative_correction") or {}).get("resend") if false_alert else None),
        "visit_appointment_created": False,
        "customer_message_sent": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture-backup", action="store_true", help="save a checksummed production snapshot; no DB writes")
    mode.add_argument("--dry-run", action="store_true", help="validate live state and write a checksummed preflight; performs no DB writes")
    mode.add_argument("--execute", action="store_true", help="apply the verified correction in one MongoDB transaction")
    parser.add_argument("--backup", type=Path, help="path to the existing pre-repair BSON/JSON snapshot")
    parser.add_argument("--backup-out", type=Path, help="new output path for --capture-backup")
    parser.add_argument("--preflight", type=Path, help="preflight file previously produced by --dry-run; required for --execute")
    parser.add_argument("--preflight-out", type=Path, help="new preflight output path for --dry-run")
    parser.add_argument("--database", default=DATABASE)
    parser.add_argument("--actor", required=True, help="named human administrator performing the approved repair")
    parser.add_argument("--confirm-incident", help=f"required with --execute: {CONFIRMATION}")
    args = parser.parse_args(argv)
    if args.database != DATABASE:
        parser.error(f"this incident script only permits database {DATABASE}")
    if not args.actor.strip() or args.actor.strip().lower() in {"system", "bot", "sistema", "assistant", "commercial_intake"}:
        parser.error("--actor must identify a human administrator")
    if args.execute and (not args.preflight or args.confirm_incident != CONFIRMATION):
        parser.error(f"--execute requires --preflight and --confirm-incident {CONFIRMATION}")
    if args.dry_run and args.preflight:
        parser.error("use --preflight-out with --dry-run; --preflight is for --execute")
    if args.capture_backup:
        if not args.backup_out or args.backup or args.preflight or args.preflight_out or args.confirm_incident:
            parser.error("--capture-backup requires only --backup-out")
    elif not args.backup or args.backup_out:
        parser.error("--dry-run/--execute require --backup; --backup-out is only for --capture-backup")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    backup_path = args.backup.expanduser().resolve() if args.backup else None
    backup = load_backup(backup_path) if backup_path else None
    backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest() if backup_path else None
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
    if not uri:
        print("ERROR: set MONGODB_URI or MONGO_URI in the authorized environment; no credential is accepted on the command line.", file=sys.stderr)
        return 2
    client = MongoClient(uri, appName="procasa-p0-6732-admin-repair", retryWrites=True,
                         connectTimeoutMS=5000, serverSelectionTimeoutMS=15000, socketTimeoutMS=30000)
    try:
        db = client[DATABASE]
        production_identity = _validate_production_identity(client, db)
        if args.capture_backup:
            backup_path = args.backup_out.expanduser().resolve()
            with client.start_session() as session:
                session.start_transaction(
                    read_concern=ReadConcern("snapshot"),
                    write_concern=WriteConcern("majority"),
                    read_preference=ReadPreference.PRIMARY,
                )
                try:
                    live_backup = _build_backup(db, session=session)
                    snapshot = collect_snapshot(db, live_backup, session=session)
                    session.commit_transaction()
                except Exception:
                    if session.in_transaction:
                        session.abort_transaction()
                    raise
            backup_sha256 = _write_backup(backup_path, live_backup)
            backup = load_backup(backup_path)
            _validate_backup_scenario(backup, snapshot)
            print("STATUS: production_backup_captured; no database writes performed")
            print(f"BACKUP: {backup_path}")
            print(f"BACKUP_SHA256: {backup_sha256}")
            print(f"DATABASE_IDENTITY: {DATABASE}; confirmed Procasa production replica set")
            print("SNAPSHOT: replica-set snapshot transaction; only the 6732 incident scope was captured")
            return 0
        if args.dry_run:
            if _verify_already_applied(db, backup):
                print("STATUS: already_applied; post-commit invariants verified; no writes performed")
                return 0
            snapshot = collect_snapshot(db, backup)
            _validate_backup_scenario(backup, snapshot)
            _validate_reviewed_sla_alerts(snapshot["sla_alerts"])
            output = args.preflight_out or backup_path.with_name("procasa-p0-6732-live-preflight.json")
            manifest = {
                "format": "procasa-p0-6732-live-preflight-v1",
                "incident_id": INCIDENT_ID,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "database": DATABASE,
                "lead_id": LEAD_OID,
                "actor": args.actor.strip(),
                "backup_file": backup_path.name,
                "backup_sha256": backup_sha256,
                "production_identity": production_identity,
                "correction_cycle_id": CORRECTION_CYCLE_ID,
                "snapshot": _snapshot_manifest(snapshot),
            }
            _write_manifest(output, manifest)
            print("STATUS: dry_run_passed; no database writes performed")
            print(f"PREFLIGHT: {output}")
            print(f"BACKUP_SHA256: {backup_sha256}")
            print(f"PRESERVED_EFFECTIVE_CONTACTS: {len(MANAGEMENT_RECORDS)}")
            print("SLA_ACTION: retain original start and completed status; exclude the reviewed false Maria-cycle breach")
            print("SLA_ALERT: provider acceptance is confirmed; no delivery receipt is recorded; preserve history and do not resend")
            print("CUSTOMER_MESSAGES: none will be sent")
            return 0

        manifest = _read_manifest(args.preflight, backup_sha256, args.actor.strip())
        if manifest.get("format") != "procasa-p0-6732-live-preflight-v1" or manifest.get("database") != DATABASE:
            raise RepairSafetyError("preflight format or database is invalid")
        if manifest.get("production_identity") != production_identity:
            raise RepairSafetyError("preflight was created against a different production replica set")
        result = apply_transaction(client, db, backup, manifest, actor=args.actor.strip())
        verification = verify_postcommit(db, backup)
        print(f"STATUS: {result}")
        print("PRODUCTION_POSTCOMMIT_VERIFICATION: " + json.dumps(verification, ensure_ascii=False, sort_keys=True))
        print("NEXT: send the separate internal notice to Hernán and María Paz after reviewing this verification")
        return 0
    except RepairSafetyError as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        # Never print a URI, full connection object, or database document.
        print(f"ERROR: {type(exc).__name__}; transaction was not reported as successful", file=sys.stderr)
        return 4
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
