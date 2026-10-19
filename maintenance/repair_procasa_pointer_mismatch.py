#!/usr/bin/env python3
"""Transactionally align reviewed Prop360 lead-cycle pointers.

This repair is intentionally one lead per invocation. It does not change cycle
records, SLA fields, ownership, management results, notifications, or visits.
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


DATABASE = "URLS"
BACKUP_FORMAT = "procasa-cycle-pointer-repair-v1"
PRODUCTION_CLUSTER_PREFIX = "ac-8f1k7c3-shard-"
PRODUCTION_CLUSTER_SUFFIX = ".mzve39k.mongodb.net"
CASES: dict[str, dict[str, Any]] = {
    "17252-other": {
        "incident_id": "PROC-2026-10-P0-17252-SECONDARY-POINTER",
        "confirmation": "P0-17252-OTHER-POINTER",
        "lead_id": "6ad4e3f40035d45d0f875791",
        "property_code": "17252",
        "owner_name": "Mariela Arriagada",
        "owner_user_id": "6989c6309dd2ba54e478196c",
        "active_cycle_id": "54d3b404-b26d-4ef8-9fd2-29bc2626e061",
        "closed_cycle_id": "da0d5a16-31ca-466e-b8b6-4aeb9692c2dd",
        "source_event_id": "12230",
        "alert_id": "assignment-integrity:111e788e6ad9f59136289a01",
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
        "incident_id": "PROC-2026-10-P0-5806-POINTER",
        "confirmation": "P0-5806-POINTER",
        "lead_id": "6ad416270035d45d0f875640",
        "property_code": "5806",
        "owner_name": "Rocío Aliaga",
        "owner_user_id": "6995e9be6a099d6e9612d120",
        "active_cycle_id": "9ec2f41c-2007-4130-914f-068617d92ddc",
        "closed_cycle_id": "14e23261-1608-4329-9634-f6f6ebffa710",
        "source_event_id": "12209",
        "alert_id": "assignment-integrity:50771479416db6d5276ea1ca",
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
    "notifications": "crm_notifications_v1",
    "sla_alerts": "crm_sla_alerts_v1",
    "pending_notifications": "pending_notifications",
    "active_jobs": "chatbot_inbound_jobs",
}


class RepairSafetyError(RuntimeError):
    """Raised when live state differs from the reviewed incident evidence."""


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
    return json_util.dumps(value, json_options=json_util.CANONICAL_JSON_OPTIONS,
                           sort_keys=True, separators=(",", ":"))


def _fingerprint(rows: list[Mapping[str, Any]]) -> str:
    return hashlib.sha256("\n".join(sorted(_canonical(dict(row)) for row in rows)).encode()).hexdigest()


def _manifest_snapshot(snapshot: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, dict[str, Any]]:
    return {key: {"count": len(rows), "sha256": _fingerprint(list(rows))}
            for key, rows in sorted(snapshot.items())}


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any], *, bson: bool = False) -> str:
    if path.exists():
        raise RepairSafetyError(f"output already exists: {path.name}; choose a new path")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if bson:
        data = json_util.dumps(payload, json_options=json_util.CANONICAL_JSON_OPTIONS, indent=2) + "\n"
    else:
        data = json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"
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
            raise RepairSafetyError(f"backup is missing the reviewed {key} collection")
        if not isinstance(bundle.get("documents"), list):
            raise RepairSafetyError(f"backup collection {key} is malformed")
    return backup


def _backup_docs(backup: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    return list(backup["collections"][key]["documents"])


def _find(db: Any, collection: str, query: Mapping[str, Any], session: Any = None) -> list[dict[str, Any]]:
    options = {"session": session} if session is not None else {}
    return list(db[collection].find(dict(query), **options).limit(10000))


def _collect_snapshot(db: Any, case: Mapping[str, Any], session: Any = None) -> dict[str, list[dict[str, Any]]]:
    lead_id = case["lead_id"]
    lead_oid = ObjectId(lead_id)
    variants = [lead_oid, lead_id]
    options = {"session": session} if session is not None else {}
    lead = db["leads"].find_one({"_id": lead_oid}, **options)
    if not lead:
        raise RepairSafetyError("reviewed lead is absent from production")
    phone = lead.get("phone")
    cycle_ids = [case["active_cycle_id"], case["closed_cycle_id"]]
    return {
        "lead": [lead],
        "cycles": _find(db, "crm_assignment_cycles", {"lead_id": {"$in": variants}}, session),
        "management": _find(db, "crm_management_results", {"$or": [
            {"lead_id": {"$in": variants}}, {"assignment_cycle_id": {"$in": cycle_ids}},
        ]}, session),
        "events": _find(db, "crm_events", {"lead_id": {"$in": variants}}, session),
        "alerts": _find(db, "crm_assignment_integrity_alerts", {"lead_id": {"$in": variants}}, session),
        "notifications": _find(db, "crm_notifications_v1", {"$or": [
            {"assignment_cycle_id": {"$in": cycle_ids}},
            {"assignment_cycle_ids": {"$in": cycle_ids}},
            {"lead_id": {"$in": variants}}, {"lead_ids": {"$in": variants}},
        ]}, session),
        "sla_alerts": _find(db, "crm_sla_alerts_v1", {"assignment_cycle_id": {"$in": cycle_ids}}, session),
        "pending_notifications": _find(db, "pending_notifications", {"$or": [
            {"lead_id": {"$in": variants}}, {"phone": phone}, {"telefono": phone}, {"lead_phone": phone},
        ]}, session),
        "active_jobs": _find(db, "chatbot_inbound_jobs", {"$and": [
            {"state": {"$in": ["received", "batching", "pending", "processing"]}},
            {"$or": [{"lead_id": {"$in": variants}}, {"phone": phone}]},
        ]}, session),
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


def _validate_pre_repair(case_name: str, snapshot: Mapping[str, list[Mapping[str, Any]]]) -> None:
    case = CASES[case_name]
    if len(snapshot["lead"]) != 1:
        raise RepairSafetyError("live lead is missing or ambiguous")
    lead = snapshot["lead"][0]
    prospect = lead.get("prospecto") or {}
    lifecycle = lead.get("lifecycle") or {}
    if (str(prospect.get("codigo") or lead.get("codigo") or "") != case["property_code"]
            or lead.get("ejecutivo_asignado") != case["owner_name"]
            or prospect.get("ejecutivo") != case["owner_name"]
            or lead.get("sla_status") != "fulfilled"):
        raise RepairSafetyError("owner mirrors, property identity, or fulfilled SLA differs from reviewed state")
    if (lifecycle.get("current_assignment_cycle_id") != case["active_cycle_id"]
            or lifecycle.get("assignment_cycle_id") != case["closed_cycle_id"]):
        raise RepairSafetyError("live cycle pointers changed; this is no longer the reviewed incident")

    by_id = {str(row.get("assignment_cycle_id")): row for row in snapshot["cycles"]}
    if len(snapshot["cycles"]) != 2 or set(by_id) != {case["active_cycle_id"], case["closed_cycle_id"]}:
        raise RepairSafetyError("cycle history changed; expected exactly the two individually audited cycles")
    active = by_id[case["active_cycle_id"]]
    closed = by_id[case["closed_cycle_id"]]
    if (active.get("cycle_status") != "active" or active.get("unassigned_at") is not None
            or closed.get("cycle_status") != "closed" or closed.get("unassigned_at") is None
            or _utc(closed.get("unassigned_at")) != _utc(active.get("assigned_at"))):
        raise RepairSafetyError("cycle lifecycle is not the reviewed single-active sequential history")
    for cycle in (active, closed):
        if (str(cycle.get("assigned_to_user_id")) != case["owner_user_id"]
                or cycle.get("assigned_to_display_name") != case["owner_name"]
                or cycle.get("sla_first_management_status") != "completed"
                or not cycle.get("sla_started_at")):
            raise RepairSafetyError("cycle owner or completed SLA evidence differs from reviewed history")
    if (str(active.get("source_event_id")) != case["source_event_id"]
            or active.get("reason") != "lead_created"
            or active.get("cycle_status") != "active"):
        raise RepairSafetyError("current active cycle is not the individually reviewed Prop360-origin cycle")

    results = {str(row.get("_id")): row for row in snapshot["management"]}
    expected = case["management"]
    if set(results) != set(expected):
        raise RepairSafetyError("human management history changed; exact reviewed records are required")
    first_by_cycle: dict[str, list[datetime]] = {}
    for record_id, facts in expected.items():
        row = results[record_id]
        occurred = _utc(row.get("occurred_at"))
        if (row.get("assignment_cycle_id") != facts["cycle_id"]
                or row.get("result_type") != facts["result_type"]
                or row.get("status") != "completed"
                or str(row.get("actor_user_id")) != case["owner_user_id"]
                or occurred != facts["occurred_at"]):
            raise RepairSafetyError("a reviewed human management record changed")
        first_by_cycle.setdefault(facts["cycle_id"], []).append(facts["occurred_at"])
    for cycle in (active, closed):
        expected_first = min(first_by_cycle[str(cycle["assignment_cycle_id"])])
        if _utc(cycle.get("first_valid_management_at")) != expected_first:
            raise RepairSafetyError("cycle first-management evidence does not match preserved human records")

    target_alert = next((row for row in snapshot["alerts"] if str(row.get("_id")) == case["alert_id"]), None)
    if (not target_alert or target_alert.get("status") != "open"
            or "cycle_pointer_mismatch" not in ((target_alert.get("evidence") or {}).get("issues") or [])):
        raise RepairSafetyError("the reviewed open cycle-pointer alert is missing or changed")
    open_pointer_alerts = {
        str(row.get("_id")) for row in snapshot["alerts"]
        if row.get("status") == "open"
        and "cycle_pointer_mismatch" in ((row.get("evidence") or {}).get("issues") or [])
    }
    if open_pointer_alerts != {case["alert_id"]}:
        raise RepairSafetyError("additional open pointer alerts need their own review")
    if snapshot["pending_notifications"] or snapshot["active_jobs"]:
        raise RepairSafetyError("a customer notification or inbound job is pending; wait and recapture")
    if any(row.get("state") in {"pending", "sending", "failed_retryable"}
           for row in snapshot["notifications"]):
        raise RepairSafetyError("an assignment notification is pending; resolve it before pointer repair")


def _write_manifest(path: Path, manifest: dict[str, Any]) -> str:
    manifest["manifest_sha256"] = _manifest_digest(manifest)
    return _write_json(path, manifest)


def _load_manifest(path: Path, backup_sha256: str, case_name: str, actor: str) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RepairSafetyError(f"preflight cannot be parsed ({type(exc).__name__})") from exc
    case = CASES[case_name]
    if manifest.get("manifest_sha256") != _manifest_digest(manifest):
        raise RepairSafetyError("preflight checksum does not match")
    if (manifest.get("format") != "procasa-cycle-pointer-preflight-v1"
            or manifest.get("incident_id") != case["incident_id"]
            or manifest.get("case") != case_name
            or manifest.get("backup_sha256") != backup_sha256
            or manifest.get("actor") != actor):
        raise RepairSafetyError("preflight belongs to another case, backup, or administrator")
    return manifest


def _verify_applied(db: Any, case_name: str, backup: Mapping[str, Any],
                    session: Any = None, *, diagnostic: bool = False) -> dict[str, Any] | None:
    case = CASES[case_name]
    snapshot = _collect_snapshot(db, case, session=session)
    lead = snapshot["lead"][0] if len(snapshot["lead"]) == 1 else None
    if not lead:
        return None
    lifecycle = lead.get("lifecycle") or {}
    cycles = {str(row.get("assignment_cycle_id")): row for row in snapshot["cycles"]}
    audit_id = f"crm_event:{case['incident_id']}:pointer-repair:v1"
    audit = db["crm_events"].find_one({"_id": audit_id}, **({"session": session} if session else {}))
    alert = next((row for row in snapshot["alerts"] if str(row.get("_id")) == case["alert_id"]), None)
    resolved_pointer_alerts = [row for row in snapshot["alerts"] if row.get("status") == "open"
                               and "cycle_pointer_mismatch" in ((row.get("evidence") or {}).get("issues") or [])]
    original_cycles = _backup_docs(backup, "cycles")
    original_management = _backup_docs(backup, "management")
    original_notifications = _backup_docs(backup, "notifications")
    original_sla_alerts = _backup_docs(backup, "sla_alerts")
    original_events = _backup_docs(backup, "events")
    current_events = [row for row in snapshot["events"] if str(row.get("_id")) != audit_id]
    try:
        from chatbot.crm_assignment_integrity import assignment_context
        context = assignment_context(db, lead)
    except Exception as exc:
        if diagnostic:
            raise RepairSafetyError(f"postcondition chatbot_owner lookup failed ({type(exc).__name__})") from exc
        return None
    audit_meta = (audit or {}).get("meta") or {}
    checks = {
        "two_cycles_retained": len(snapshot["cycles"]) == 2 and set(cycles) == {case["active_cycle_id"], case["closed_cycle_id"]},
        "single_active_cycle": sum(1 for row in snapshot["cycles"] if row.get("cycle_status") == "active" and row.get("unassigned_at") is None) == 1,
        "both_pointers_canonical": lifecycle.get("current_assignment_cycle_id") == case["active_cycle_id"] and lifecycle.get("assignment_cycle_id") == case["active_cycle_id"],
        "owner_preserved": lead.get("ejecutivo_asignado") == case["owner_name"] and (lead.get("prospecto") or {}).get("ejecutivo") == case["owner_name"],
        "sla_unchanged": lead.get("sla_status") == "fulfilled" and _utc(lifecycle.get("sla_started_at")) == _utc((_backup_docs(backup, "lead")[0].get("lifecycle") or {}).get("sla_started_at")),
        "cycle_documents_unchanged": _fingerprint(snapshot["cycles"]) == _fingerprint(original_cycles),
        "management_history_unchanged": _fingerprint(snapshot["management"]) == _fingerprint(original_management),
        "assignment_notifications_unchanged": _fingerprint(snapshot["notifications"]) == _fingerprint(original_notifications),
        "sla_alerts_unchanged": _fingerprint(snapshot["sla_alerts"]) == _fingerprint(original_sla_alerts),
        "event_history_preserved": _fingerprint(current_events) == _fingerprint(original_events),
        "pointer_alert_resolved_auditably": bool(alert and alert.get("status") == "resolved" and alert.get("correction_incident_id") == case["incident_id"] and alert.get("correction_cycle_id") == case["active_cycle_id"] and not resolved_pointer_alerts),
        "audit_event_present": bool(audit and audit.get("type") == "ADMINISTRATIVE_CYCLE_POINTER_REPAIR" and audit_meta.get("incident_id") == case["incident_id"] and audit_meta.get("customer_message_sent") is False and audit_meta.get("assignment_notification_sent") is False),
        "chatbot_owner_consistent": context.get("status") == "consistent" and context.get("owner_name") == case["owner_name"] and (context.get("cycle") or {}).get("assignment_cycle_id") == case["active_cycle_id"],
        "no_customer_jobs": not snapshot["pending_notifications"] and not snapshot["active_jobs"],
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
        "active_cycle_id": case["active_cycle_id"],
        "closed_cycle_id": case["closed_cycle_id"],
        "cycle_count": len(snapshot["cycles"]),
        "preserved_management_records": len(snapshot["management"]),
        "sla_status": lead.get("sla_status"),
        "sla_started_at_preserved": True,
        "pointer_alerts_open": len(resolved_pointer_alerts),
        "chatbot_owner_status": context.get("status"),
        "customer_message_sent": False,
        "assignment_notification_sent": False,
    }


def _validate_production_identity(client: MongoClient, db: Any) -> dict[str, str]:
    hello = client.admin.command("hello")
    hosts = [str(value).rsplit(":", 1)[0] for value in hello.get("hosts", [])]
    if not hello.get("setName") or not any(host.startswith(PRODUCTION_CLUSTER_PREFIX)
                                            and host.endswith(PRODUCTION_CLUSTER_SUFFIX) for host in hosts):
        raise RepairSafetyError("connected replica set is not the confirmed Procasa production cluster")
    stats = db.command("dbStats")
    if stats.get("db") != DATABASE or int(stats.get("collections") or 0) < 1:
        raise RepairSafetyError("database identity does not match the nonempty production URLS database")
    return {"database": DATABASE, "replica_set": str(hello["setName"]), "cluster_prefix": PRODUCTION_CLUSTER_PREFIX}


def _apply(client: MongoClient, db: Any, case_name: str, backup: Mapping[str, Any], manifest: Mapping[str, Any], actor: str) -> str:
    case = CASES[case_name]
    lead_oid = ObjectId(case["lead_id"])
    audit_id = f"crm_event:{case['incident_id']}:pointer-repair:v1"

    def transaction(session: Any) -> str:
        live = _collect_snapshot(db, case, session=session)
        if _manifest_snapshot(live) != manifest.get("snapshot"):
            raise RepairSafetyError("concurrency check failed; live documents changed after preflight")
        _validate_pre_repair(case_name, live)
        if db["crm_events"].find_one({"_id": audit_id}, session=session):
            raise RepairSafetyError("an administrative repair audit already exists; verify before retrying")
        lead = live["lead"][0]
        now = datetime.now(timezone.utc)
        lead_update = db["leads"].update_one({
            "_id": lead_oid,
            "ejecutivo_asignado": case["owner_name"],
            "prospecto.ejecutivo": case["owner_name"],
            "lifecycle.current_assignment_cycle_id": case["active_cycle_id"],
            "lifecycle.assignment_cycle_id": case["closed_cycle_id"],
            "lifecycle.assigned_to_user_id": case["owner_user_id"],
        }, {"$set": {"lifecycle.assignment_cycle_id": case["active_cycle_id"]}}, session=session)
        if lead_update.matched_count != 1:
            raise RepairSafetyError("concurrency check failed while aligning the lead's canonical pointers")
        alert = next(row for row in live["alerts"] if str(row.get("_id")) == case["alert_id"])
        alert_filter = {"_id": case["alert_id"], "status": "open", "evidence.issues": "cycle_pointer_mismatch"}
        if (alert.get("evidence") or {}).get("signature") is not None:
            alert_filter["evidence.signature"] = (alert.get("evidence") or {}).get("signature")
        alert_update = db["crm_assignment_integrity_alerts"].update_one(alert_filter, {"$set": {
            "status": "resolved",
            "resolved_at": now,
            "resolved_by": actor,
            "resolution": "canonical cycle pointers aligned to the already-active Prop360 cycle; cycle and SLA history retained",
            "correction_incident_id": case["incident_id"],
            "correction_cycle_id": case["active_cycle_id"],
        }}, session=session)
        if alert_update.matched_count != 1:
            raise RepairSafetyError("concurrency check failed while resolving the reviewed pointer alert")
        db["crm_events"].insert_one({
            "_id": audit_id,
            "lead_id": lead_oid,
            "assignment_cycle_id": case["active_cycle_id"],
            "actor": actor,
            "actor_type": "administrator",
            "type": "ADMINISTRATIVE_CYCLE_POINTER_REPAIR",
            "result": "CANONICAL_POINTERS_ALIGNED_TO_ACTIVE_CYCLE",
            "confirmed": True,
            "timestamp": now,
            "source": "p0_incident_remediation",
            "meta": {
                "incident_id": case["incident_id"],
                "property_code": case["property_code"],
                "active_cycle_id": case["active_cycle_id"],
                "closed_historical_cycle_id": case["closed_cycle_id"],
                "prop360_event_id": case["source_event_id"],
                "preserved_management_result_ids": sorted(case["management"]),
                "prior_assignment_cycle_id": case["closed_cycle_id"],
                "new_assignment_cycle_id": case["active_cycle_id"],
                "cycle_documents_changed": False,
                "sla_restarted": False,
                "customer_message_sent": False,
                "assignment_notification_sent": False,
                "resolved_alert_ids": [case["alert_id"]],
            },
        }, session=session)
        if not _verify_applied(db, case_name, backup, session=session, diagnostic=True):
            raise RepairSafetyError("in-transaction postcondition failed; transaction will roll back")
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
    mode.add_argument("--verify", action="store_true", help="read-only post-repair verification")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--backup-out", type=Path)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--preflight-out", type=Path)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--confirm-incident")
    args = parser.parse_args(argv)
    if not args.actor.strip() or args.actor.strip().lower() in {"system", "bot", "sistema", "assistant"}:
        parser.error("--actor must identify a human administrator")
    if args.capture_backup:
        if not args.backup_out or args.backup or args.preflight or args.preflight_out or args.confirm_incident:
            parser.error("--capture-backup requires only --case, --backup-out, and --actor")
    elif not args.backup or args.backup_out:
        parser.error("--dry-run/--execute/--verify require --backup; --backup-out is only for --capture-backup")
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
        print("ERROR: set MONGODB_URI or MONGO_URI in the authorized runtime; no credential is accepted on the command line.", file=sys.stderr)
        return 2
    client = MongoClient(uri, appName=f"procasa-pointer-repair-{args.case}", retryWrites=True,
                         connectTimeoutMS=5000, serverSelectionTimeoutMS=15000, socketTimeoutMS=30000)
    try:
        db = client[DATABASE]
        production_identity = _validate_production_identity(client, db)
        if args.capture_backup:
            backup_path = args.backup_out.expanduser().resolve()
            with client.start_session() as session:
                session.start_transaction(read_concern=ReadConcern("snapshot"),
                                          write_concern=WriteConcern("majority"),
                                          read_preference=ReadPreference.PRIMARY)
                try:
                    snapshot = _collect_snapshot(db, case, session=session)
                    live_backup = _build_backup(args.case, snapshot)
                    session.commit_transaction()
                except Exception:
                    if session.in_transaction:
                        session.abort_transaction()
                    raise
            backup_sha256 = _write_json(backup_path, live_backup, bson=True)
            backup = _load_backup(backup_path, args.case)
            _validate_pre_repair(args.case, snapshot)
            print("STATUS: production_backup_captured; no database writes performed")
            print(f"BACKUP: {backup_path}")
            print(f"BACKUP_SHA256: {backup_sha256}")
            print(f"DATABASE_IDENTITY: {DATABASE}; confirmed Procasa production replica set")
            print(f"SCOPE: case={args.case}; cycles={len(snapshot['cycles'])}; management_records={len(snapshot['management'])}")
            return 0
        if args.verify:
            verification = _verify_applied(db, args.case, backup, diagnostic=True)
            print("STATUS: production_verified; " + json.dumps(verification, ensure_ascii=False, sort_keys=True, default=str))
            return 0
        if args.dry_run:
            already = _verify_applied(db, args.case, backup)
            if already:
                print("STATUS: already_applied; post-commit invariants verified; no writes performed")
                print("PRODUCTION_VERIFICATION: " + json.dumps(already, ensure_ascii=False, sort_keys=True, default=str))
                return 0
            snapshot = _collect_snapshot(db, case)
            _validate_pre_repair(args.case, snapshot)
            backup_snapshot = {key: _backup_docs(backup, key) for key in COLLECTIONS}
            if _manifest_snapshot(snapshot) != _manifest_snapshot(backup_snapshot):
                raise RepairSafetyError("live snapshot changed after backup; capture a new backup")
            output = args.preflight_out or backup_path.with_name(f"{args.case}-preflight.json")
            manifest = {
                "format": "procasa-cycle-pointer-preflight-v1",
                "case": args.case,
                "incident_id": case["incident_id"],
                "database": DATABASE,
                "production_identity": production_identity,
                "lead_id": case["lead_id"],
                "actor": args.actor.strip(),
                "backup_sha256": backup_sha256,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "active_cycle_id": case["active_cycle_id"],
                "closed_cycle_id": case["closed_cycle_id"],
                "snapshot": _manifest_snapshot(snapshot),
            }
            _write_manifest(output, manifest)
            print("STATUS: dry_run_passed; no database writes performed")
            print(f"PREFLIGHT: {output}")
            print(f"BACKUP_SHA256: {backup_sha256}")
            print(f"CYCLES: {len(snapshot['cycles'])} retained; one already-active cycle is canonical")
            print(f"MANAGEMENT_RECORDS: {len(snapshot['management'])} preserved")
            print("SLA_ACTION: none; existing fulfilled state and timestamps will remain unchanged")
            print("NOTIFICATIONS: none sent or queued by this repair")
            return 0
        manifest = _load_manifest(args.preflight.expanduser().resolve(), backup_sha256, args.case, args.actor.strip())
        if manifest.get("production_identity") != production_identity:
            raise RepairSafetyError("preflight was created against a different production replica set")
        if manifest.get("active_cycle_id") != case["active_cycle_id"] or manifest.get("closed_cycle_id") != case["closed_cycle_id"]:
            raise RepairSafetyError("preflight cycle identities do not match this case")
        if _verify_applied(db, args.case, backup):
            print("STATUS: already_applied; post-commit invariants verified; no writes performed")
            return 0
        result = _apply(client, db, args.case, backup, manifest, actor=args.actor.strip())
        verified = _verify_applied(db, args.case, backup, diagnostic=True)
        print(f"STATUS: {result}")
        print("PRODUCTION_POSTCOMMIT_VERIFICATION: " + json.dumps(verified, ensure_ascii=False, sort_keys=True, default=str))
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
