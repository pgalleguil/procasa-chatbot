"""Fail-closed assignment consistency checks and durable review alerts.

Assignment cycles are the ownership source of truth.  Lead fields and chatbot
messages are mirrors; a conflict must never be resolved by guessing from them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import logging
import time
import uuid
from typing import Any, Mapping

from pymongo.errors import AutoReconnect, ConnectionFailure, NetworkTimeout, ServerSelectionTimeoutError

from .crm_metrics import (
    coerce_utc_datetime,
    is_first_sla_management_completed,
)
from .mongo_identity import mongo_id_variants

logger = logging.getLogger(__name__)

ALERT_COLLECTION = "crm_assignment_integrity_alerts"
AUTHORIZED_TRANSFER_REASONS = frozenset({"crm_list_admin", "sla_reassignment"})
UNASSIGNED_OWNER_LABELS = frozenset({
    "", "no asignado", "sin asignar", "unassigned", "none", "null",
})


def active_assignment_cycles(db: Any, lead_id: Any) -> list[dict[str, Any]]:
    """Return every active cycle for a lead, including legacy ID spellings."""
    variants = list(mongo_id_variants(lead_id))
    if not variants:
        return []
    rows = list(db["crm_assignment_cycles"].find({
        "lead_id": {"$in": variants},
        "unassigned_at": None,
        "$or": [
            {"cycle_status": "active"},
            {"cycle_status": {"$exists": False}},
        ],
    }).limit(20))
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        cycle_id = str(row.get("assignment_cycle_id") or row.get("_id") or "")
        if cycle_id:
            unique[cycle_id] = row
    return list(unique.values())


def assignment_context(
    db: Any,
    lead: Mapping[str, Any],
    *,
    active_cycles_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve a customer-safe owner only when the canonical cycle and mirrors agree."""
    lead_id = lead.get("_id")
    if lead_id is None:
        return {"status": "conflict", "owner_name": None, "cycle": None,
                "active_cycles": [], "issues": ["missing_lead_id"]}

    lifecycle = lead.get("lifecycle") or {}
    prospect = lead.get("prospecto") or {}
    pointers = [
        str(lifecycle.get(field) or "").strip()
        for field in ("current_assignment_cycle_id", "assignment_cycle_id")
        if str(lifecycle.get(field) or "").strip()
    ]
    mirrored_names = [
        str(lead.get("ejecutivo_asignado") or "").strip(),
        str(prospect.get("ejecutivo") or "").strip(),
    ]
    explicit_names = [
        name for name in mirrored_names
        if name and name.casefold() not in UNASSIGNED_OWNER_LABELS
    ]
    if active_cycles_override is None:
        cycles = active_assignment_cycles(db, lead_id)
    else:
        # The reconciler can supply histories loaded in one bounded batch,
        # avoiding one network round-trip per lead while keeping this helper's
        # standalone read behavior unchanged.
        unique: dict[str, dict[str, Any]] = {}
        for row in active_cycles_override:
            if row.get("unassigned_at") is not None or row.get("cycle_status") not in {"active", None}:
                continue
            cycle_id = str(row.get("assignment_cycle_id") or row.get("_id") or "")
            if cycle_id:
                unique[cycle_id] = row
        cycles = list(unique.values())

    if not cycles:
        if pointers or explicit_names:
            return {"status": "conflict", "owner_name": None, "cycle": None,
                    "active_cycles": [], "issues": ["assignment_without_active_cycle"]}
        return {"status": "unassigned", "owner_name": None, "cycle": None,
                "active_cycles": [], "issues": []}

    if len(cycles) != 1:
        return {"status": "conflict", "owner_name": None, "cycle": None,
                "active_cycles": cycles, "issues": ["multiple_active_cycles"]}

    cycle = cycles[0]
    cycle_id = str(cycle.get("assignment_cycle_id") or "")
    owner_name = str(cycle.get("assigned_to_display_name") or "").strip()
    owner_id = str(cycle.get("assigned_to_user_id") or "").strip()
    issues: list[str] = []
    if not cycle_id or not owner_id or not owner_name:
        issues.append("incomplete_active_cycle")
    if not pointers or any(pointer != cycle_id for pointer in pointers):
        issues.append("cycle_pointer_mismatch")
    if any(name != owner_name for name in mirrored_names):
        issues.append("owner_mirror_mismatch")
    lifecycle_owner_id = str(lifecycle.get("assigned_to_user_id") or "").strip()
    if lifecycle_owner_id and lifecycle_owner_id != owner_id:
        issues.append("owner_id_mirror_mismatch")
    cycle_property = str(cycle.get("property_code") or "").strip()
    lead_property = str(prospect.get("codigo") or lead.get("codigo") or "").strip()
    if cycle_property and lead_property and cycle_property != lead_property:
        issues.append("property_mirror_mismatch")
    if str(cycle.get("reassignment_state") or "").upper() == "AWAITING_OWNER_NOTIFICATION":
        issues.append("transfer_pending_owner_notification")

    return {
        "status": "consistent" if not issues else "conflict",
        "owner_name": owner_name if not issues else None,
        "cycle": cycle,
        "active_cycles": cycles,
        "issues": issues,
    }


def prepare_pending_assignment_notification(
    db: Any,
    *,
    lead_phone: str,
    requested_owner_name: str,
    requested_cycle_id: str | None = None,
) -> dict[str, Any]:
    """Gate legacy queued notices against the live canonical assignment.

    An unassigned lead is first assigned through the canonical CRM service.
    A stale notice is suppressed when its target no longer matches the active
    cycle, and unresolved mirrors never authorize a notification.
    """
    from bson import ObjectId

    phone = str(lead_phone or "").strip()
    requested_name = str(requested_owner_name or "").strip()
    from .crm_metrics import resolve_canonical_lead
    resolution = resolve_canonical_lead(db, phone=phone)
    lead = resolution.lead
    if not lead:
        return {"status": "blocked", "reason": "canonical_lead_missing"}

    state = assignment_context(db, lead)
    if state.get("status") == "conflict":
        record_assignment_integrity_alert(
            db, lead_id=lead["_id"], issue_code="pending_notice_blocked_by_assignment_conflict",
            evidence={"issues": state.get("issues"),
                      "signature": f"{lead['_id']}:{':'.join(state.get('issues') or [])}"},
        )
        return {"status": "blocked", "reason": "assignment_conflict"}

    if state.get("status") == "unassigned":
        requested_user = db["usuarios"].find_one({
            "nombre": requested_name, "is_active": {"$ne": False},
        }) if requested_name else None
        if not requested_user:
            record_assignment_integrity_alert(
                db, lead_id=lead["_id"], issue_code="pending_notice_recipient_unresolved",
                evidence={"signature": f"{lead['_id']}:{requested_name.casefold()}"},
            )
            return {"status": "blocked", "reason": "requested_owner_unresolved"}
        from .crm_service import CrmService
        CrmService.assign_executive(phone, requested_name, method="LeadRouter", actor="system")
        lead = db["leads"].find_one({"_id": lead["_id"]}) or lead
        state = assignment_context(db, lead)

    if state.get("status") != "consistent":
        return {"status": "blocked", "reason": "assignment_not_canonical"}

    cycle = state.get("cycle") or {}
    cycle_id = str(cycle.get("assignment_cycle_id") or "")
    owner_id = str(cycle.get("assigned_to_user_id") or "")
    owner_name = str(cycle.get("assigned_to_display_name") or "").strip()
    if not cycle_id or not owner_id or not owner_name:
        return {"status": "blocked", "reason": "canonical_owner_incomplete"}
    if requested_cycle_id and requested_cycle_id != cycle_id:
        return {"status": "blocked", "reason": "stale_assignment_cycle"}
    if requested_name and requested_name.casefold() != owner_name.casefold():
        record_assignment_integrity_alert(
            db, lead_id=lead["_id"], issue_code="pending_notice_target_differs_from_canonical_owner",
            evidence={"cycle_id": cycle_id,
                      "requested_owner_name": requested_name,
                      "canonical_owner_name": owner_name,
                      "signature": f"{cycle_id}:{requested_name.casefold()}:{owner_name.casefold()}"},
        )
        return {"status": "blocked", "reason": "notice_target_is_not_cycle_owner"}

    try:
        owner = db["usuarios"].find_one({
            "_id": ObjectId(owner_id), "is_active": {"$ne": False},
        })
    except Exception:
        owner = None
    if not owner:
        owner = db["usuarios"].find_one({
            "nombre": owner_name, "is_active": {"$ne": False},
        })
    phone_number = (owner or {}).get("telefono") or (owner or {}).get("tel") or (owner or {}).get("movil")
    if not owner or not phone_number:
        record_assignment_integrity_alert(
            db, lead_id=lead["_id"], issue_code="pending_notice_canonical_owner_unavailable",
            evidence={"cycle_id": cycle_id, "owner_user_id": owner_id,
                      "signature": cycle_id},
        )
        return {"status": "blocked", "reason": "canonical_owner_unavailable"}
    return {
        "status": "ready", "lead_id": lead["_id"], "cycle_id": cycle_id,
        "owner_user_id": owner_id, "owner_name": owner_name,
        "owner_phone": str(phone_number),
    }


def cycle_has_human_management(db: Any, cycle: Mapping[str, Any]) -> bool:
    """Read exact-cycle human evidence used by the existing SLA evaluator."""
    if not cycle:
        return False
    cycle_id = str(cycle.get("assignment_cycle_id") or "")
    if not cycle_id:
        return False
    lead_id = cycle.get("lead_id")
    variants = list(mongo_id_variants(lead_id))
    results = list(db["crm_management_results"].find({
        "assignment_cycle_id": cycle_id,
    }).limit(100))
    events = list(db["crm_events"].find({
        "lead_id": {"$in": variants},
    }).limit(500)) if variants else []
    return is_first_sla_management_completed(cycle, results, events)


def record_assignment_integrity_alert(
    db: Any,
    *,
    lead_id: Any,
    issue_code: str,
    evidence: Mapping[str, Any] | None = None,
    detected_at: datetime | None = None,
    detection_run_id: str | None = None,
) -> dict[str, Any]:
    """Upsert one durable, deduplicated internal review alert."""
    when = detected_at or datetime.now(timezone.utc)
    evidence = dict(evidence or {})
    digest = hashlib.sha256(
        (str(lead_id) + "|" + issue_code + "|" + str(evidence.get("signature") or ""))
        .encode("utf-8", "replace")
    ).hexdigest()[:24]
    alert_id = f"assignment-integrity:{digest}"
    if detection_run_id:
        # The reconciler retries transient Mongo read failures. Use a stable
        # per-run id so retrying a partially persisted report cannot inflate
        # alert observations more than once.
        insert_fields = {
            "lead_id": lead_id,
            "issue_code": issue_code,
            "severity": "P0",
            "status": "open",
            "first_detected_at": when,
            "evidence": evidence,
            "review_required": True,
            "observations": 0,
        }
        try:
            db[ALERT_COLLECTION].update_one(
                {"_id": alert_id}, {"$setOnInsert": insert_fields}, upsert=True,
            )
        except Exception as exc:
            # A concurrent run may win the unique _id insert. Only suppress
            # the duplicate-key race; other failures must reach the caller.
            from pymongo.errors import DuplicateKeyError
            if not isinstance(exc, DuplicateKeyError):
                raise
        db[ALERT_COLLECTION].update_one(
            {"_id": alert_id, "last_observation_run_id": {"$ne": detection_run_id}},
            {"$set": {"last_detected_at": when, "last_observation_run_id": detection_run_id},
             "$inc": {"observations": 1}},
        )
        db[ALERT_COLLECTION].update_one(
            {"_id": alert_id, "last_observation_run_id": detection_run_id},
            {"$set": {"last_detected_at": when}},
        )
    else:
        db[ALERT_COLLECTION].update_one(
            {"_id": alert_id},
            {
                "$setOnInsert": {
                    "lead_id": lead_id,
                    "issue_code": issue_code,
                    "severity": "P0",
                    "status": "open",
                    "first_detected_at": when,
                    "evidence": evidence,
                    "review_required": True,
                },
                "$set": {"last_detected_at": when},
                "$inc": {"observations": 1},
            },
            upsert=True,
        )
    return db[ALERT_COLLECTION].find_one({"_id": alert_id}) or {
        "_id": alert_id, "lead_id": lead_id, "issue_code": issue_code,
        "severity": "P0", "status": "open", "evidence": evidence,
    }


def _valid_transfer_transition(
    previous: Mapping[str, Any], current: Mapping[str, Any], *, db: Any = None,
) -> bool:
    reason = str(current.get("reason") or "").strip().lower()
    assigned_by = str(current.get("assigned_by") or "").strip().lower()
    if reason == "crm_list_admin":
        authorization = current.get("transfer_authorization") or {}
        actor = str(authorization.get("actor") or "").strip()
        return (
            authorization.get("kind") == "administrative"
            and bool(actor)
            and actor.lower() not in {"system", "bot", "sistema", "assistant"}
            and actor == str(current.get("assigned_by") or "").strip()
        )
    if assigned_by == "sla_reassignment":
        decision_id = str(current.get("reassignment_decision_id") or "").strip()
        if not decision_id:
            return False
        if db is None:
            # Preserve the pure helper contract for callers without a database;
            # the production reconciliation path always verifies the audit row.
            return True
        audit = db["crm_sla_reassignment_audit_v1"].find_one({
            "_id": decision_id,
            "event_type": "SLA_REASSIGNMENT_COMMITTED",
            "decision_id": decision_id,
            "source_cycle_id": str(previous.get("assignment_cycle_id") or ""),
            "new_cycle_id": str(current.get("assignment_cycle_id") or ""),
        })
        return bool(audit)
    return False


def _was_transition_administratively_corrected(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    cycles: list[dict[str, Any]],
    *,
    db: Any = None,
) -> bool:
    """Suppress only a historical owner change explicitly reversed later.

    The source cycle remains in history. The later correction must point to
    both exact cycles, restore the same owner, carry a valid human admin
    authorization, and be marked excluded from performance scoring.
    """
    if current.get("administratively_corrected") is not True:
        return False
    corrected_cycle_id = str(current.get("assignment_cycle_id") or "")
    correction_cycle_id = str(current.get("correction_cycle_id") or "")
    incident_id = str(current.get("correction_incident_id") or "")
    previous_cycle_id = str(previous.get("assignment_cycle_id") or "")
    previous_owner = str(previous.get("assigned_to_user_id") or "")
    if not all((corrected_cycle_id, correction_cycle_id, incident_id, previous_cycle_id, previous_owner)):
        return False
    correction = next((
        row for row in cycles
        if str(row.get("assignment_cycle_id") or "") == correction_cycle_id
    ), None)
    if not correction:
        return False
    correction_audit = correction.get("administrative_correction") or {}
    incident_matches = (
        str(correction.get("correction_incident_id") or correction_audit.get("incident_id") or "")
        == incident_id
    )
    return bool(
        correction.get("cycle_origin") == "administrative_correction"
        and correction.get("corrects_assignment_cycle_id") == corrected_cycle_id
        and correction.get("restores_assignment_cycle_id") == previous_cycle_id
        and str(correction.get("assigned_to_user_id") or "") == previous_owner
        and correction.get("sla_performance_excluded") is True
        and correction.get("notification_eligible") is False
        and incident_matches
        and _valid_transfer_transition(current, correction, db=db)
    )


def _transition_findings(db: Any, lead_id: Any, cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    ordered = sorted(
        cycles,
        key=lambda row: coerce_utc_datetime(row.get("assigned_at")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    for previous, current in zip(ordered, ordered[1:]):
        previous_owner = str(previous.get("assigned_to_user_id") or "")
        current_owner = str(current.get("assigned_to_user_id") or "")
        if not previous_owner or previous_owner == current_owner:
            continue
        if _valid_transfer_transition(previous, current, db=db):
            continue
        if _was_transition_administratively_corrected(previous, current, ordered, db=db):
            continue
        managed = cycle_has_human_management(db, previous)
        reason = str(current.get("reason") or "").strip().lower()
        prop360_ingest = (
            str(current.get("source_system") or "").strip().lower() == "prop360"
            or reason in {"inbound_message", "lead_created", "new_lead_event"}
        )
        findings.append({
            "issue_code": "managed_lead_reassigned_without_authorization" if managed and prop360_ingest else "owner_change_without_authorization",
            "lead_id": lead_id,
            "previous_property_code": str(previous.get("property_code") or ""),
            "current_property_code": str(current.get("property_code") or ""),
            "previous_cycle_id": previous.get("assignment_cycle_id"),
            "current_cycle_id": current.get("assignment_cycle_id"),
            "previous_owner_user_id": previous_owner,
            "current_owner_user_id": current_owner,
            "previous_owner_name": previous.get("assigned_to_display_name"),
            "current_owner_name": current.get("assigned_to_display_name"),
            "previous_cycle_managed": managed,
            "current_reason": reason,
            "current_source_system": current.get("source_system"),
            "signature": f"{previous.get('assignment_cycle_id')}:{current.get('assignment_cycle_id')}",
        })

    active = [row for row in cycles if row.get("cycle_status") == "active" and row.get("unassigned_at") is None]
    if len(active) > 1:
        ids = sorted(str(row.get("assignment_cycle_id") or "") for row in active)
        findings.append({
            "issue_code": "multiple_active_cycles_for_opportunity",
            "lead_id": lead_id,
            "active_cycle_ids": ids,
            "active_property_codes": sorted({str(row.get("property_code") or "") for row in active}),
            "signature": ":".join(ids),
        })

    return findings


def _notification_findings(
    db: Any,
    lead_id: Any,
    cycles: list[dict[str, Any]],
    *,
    notifications: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    cycle_ids = [str(row.get("assignment_cycle_id") or "") for row in cycles]
    cycle_ids = [value for value in cycle_ids if value]
    if not cycle_ids:
        return []
    if notifications is None:
        notifications = list(db["crm_notifications_v1"].find({
            "$or": [
                {"assignment_cycle_id": {"$in": cycle_ids},
                 "notification_type": "lead_assignment_hot"},
                {"assignment_cycle_ids": {"$in": cycle_ids},
                 "digest_type": "non_hot_digest_v1"},
            ],
            "state": {"$in": ["pending", "sending", "sent", "failed_retryable"]},
        }).limit(500))
    else:
        cycle_id_set = set(cycle_ids)
        notifications = [
            row for row in notifications
            if (
                str(row.get("assignment_cycle_id") or "") in cycle_id_set
                or bool(cycle_id_set.intersection(str(value) for value in row.get("assignment_cycle_ids") or []))
            )
            and row.get("state") in {"pending", "sending", "sent", "failed_retryable"}
            and (
                row.get("notification_type") == "lead_assignment_hot"
                or row.get("digest_type") == "non_hot_digest_v1"
            )
        ]
    delivered = [row for row in notifications if row.get("state") in {"sent", "sending"} or row.get("actually_delivered") is True]
    cycle_by_id = {str(row.get("assignment_cycle_id")): row for row in cycles}
    recipients_by_cycle: dict[str, set[str]] = {}
    notifications_by_cycle: dict[str, list[dict[str, Any]]] = {}
    for notification in delivered:
        attached_ids = notification.get("assignment_cycle_ids") or [notification.get("assignment_cycle_id")]
        recipient = str(notification.get("recipient_user_id") or "")
        for attached_id in attached_ids:
            cycle_id = str(attached_id or "")
            if cycle_id not in cycle_by_id:
                continue
            if recipient:
                recipients_by_cycle.setdefault(cycle_id, set()).add(recipient)
            notifications_by_cycle.setdefault(cycle_id, []).append(notification)

    findings = []
    for cycle_id, recipients in recipients_by_cycle.items():
        expected_recipient = str(cycle_by_id[cycle_id].get("assigned_to_user_id") or "")
        mismatched_recipients = sorted(
            recipient for recipient in recipients if recipient != expected_recipient
        )
        if expected_recipient and mismatched_recipients:
            cycle = cycle_by_id[cycle_id]
            findings.append({
                "issue_code": "notification_recipient_mismatch_for_assignment_cycle",
                "lead_id": lead_id,
                "assignment_cycle_id": cycle_id,
                "property_code": str(cycle.get("property_code") or ""),
                "expected_recipient_user_id": expected_recipient,
                "recipient_user_ids": mismatched_recipients,
                "signature": cycle_id + ":" + expected_recipient + ":" + ":".join(mismatched_recipients),
            })
        if len(recipients) > 1:
            cycle = cycle_by_id[cycle_id]
            findings.append({
                "issue_code": "multiple_notification_recipients_for_assignment_cycle",
                "lead_id": lead_id,
                "assignment_cycle_id": cycle_id,
                "property_code": str(cycle.get("property_code") or ""),
                "recipient_user_ids": sorted(recipients),
                "signature": cycle_id + ":" + ":".join(sorted(recipients)),
            })
    for cycle_id, rows in notifications_by_cycle.items():
        cycle = cycle_by_id[cycle_id]
        # A digest can contain several leads. Count each attached cycle once
        # per notification so multiple digest rows for the same cycle surface.
        notification_ids = {str(row.get("_id") or id(row)) for row in rows}
        if len(notification_ids) > 1:
            findings.append({
                "issue_code": "duplicate_delivered_notifications_for_assignment_cycle",
                "lead_id": lead_id,
                "assignment_cycle_id": cycle_id,
                "property_code": str(cycle.get("property_code") or ""),
                "notification_ids": sorted(notification_ids),
                "signature": cycle_id + ":" + ":".join(sorted(notification_ids)),
            })
    return findings


def _chatbot_owner_findings(lead: Mapping[str, Any], cycles: list[dict[str, Any]], integrity: Mapping[str, Any]) -> list[dict[str, Any]]:
    if integrity.get("status") != "conflict":
        return []
    names = {
        str(value).strip().casefold()
        for row in cycles
        for value in (row.get("assigned_to_display_name"),)
        if str(value or "").strip()
    }
    names.update(
        str(value).strip().casefold()
        for value in (
            lead.get("ejecutivo_asignado"),
            (lead.get("prospecto") or {}).get("ejecutivo"),
        )
        if str(value or "").strip()
        and str(value).strip().casefold() not in UNASSIGNED_OWNER_LABELS
    )
    if not names:
        return []
    for message in lead.get("messages") or []:
        if message.get("role") != "assistant":
            continue
        content = str(message.get("content") or "")
        if any(name in content.casefold() for name in names):
            return [{
                "issue_code": "chatbot_named_executive_during_assignment_conflict",
                "lead_id": lead.get("_id"),
                "cycle_ids": sorted(str(row.get("assignment_cycle_id") or "") for row in cycles),
                "signature": hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:16],
            }]
    return []


def reconcile_assignment_integrity(
    db: Any,
    *,
    now: datetime | None = None,
    lookback_days: int = 30,
    limit: int = 10000,
    persist_alerts: bool = True,
    detection_run_id: str | None = None,
) -> dict[str, Any]:
    """Audit recent and active cycles; persist alerts without changing owners."""
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=max(int(lookback_days), 1))
    cutoff_iso = cutoff.isoformat()
    candidate_projection = {"_id": 1, "lead_id": 1, "assigned_at": 1, "cycle_status": 1, "unassigned_at": 1}
    cursor = db["crm_assignment_cycles"].find({
        "$or": [
            {"assigned_at": {"$gte": cutoff}},
            {"assigned_at": {"$gte": cutoff_iso}},
            {"cycle_status": "active", "unassigned_at": None},
            {"cycle_status": {"$exists": False}, "unassigned_at": None},
        ],
    }, candidate_projection).limit(max(int(limit), 1))
    # This query is intentionally unsorted and projected. The previous full
    # document sort pulled every historical cycle over the network before the
    # audit could begin, exceeding Render's 8-second Mongo socket timeout.
    candidate_rows = list(cursor)
    candidate_leads: dict[str, Any] = {}
    for row in candidate_rows:
        lead_id = row.get("lead_id")
        if lead_id is not None:
            candidate_leads.setdefault(str(lead_id), lead_id)

    # Include leads whose mirrors claim an owner or cycle but have no recent
    # cycle row. These orphan assignments are otherwise invisible to a
    # cycle-led audit even though they can mislead the chatbot and operators.
    ignored_owner_labels = [None, *UNASSIGNED_OWNER_LABELS]
    assigned_mirror_query = {"$or": [
        {"lifecycle.current_assignment_cycle_id": {"$exists": True, "$nin": [None, ""]}},
        {"lifecycle.assignment_cycle_id": {"$exists": True, "$nin": [None, ""]}},
        {"ejecutivo_asignado": {"$exists": True, "$nin": ignored_owner_labels}},
        {"prospecto.ejecutivo": {"$exists": True, "$nin": ignored_owner_labels}},
    ]}
    for lead in db["leads"].find(assigned_mirror_query, {"_id": 1}).limit(max(int(limit), 1)):
        lead_id = lead.get("_id")
        if lead_id is not None:
            candidate_leads.setdefault(str(lead_id), lead_id)

    # Load all candidate histories and their lead mirrors in bounded batches.
    # This keeps the 30-day reconciler from issuing several thousand serial
    # round-trips for active leads and their cycle histories.
    id_variants: list[Any] = []
    for lead_id in candidate_leads.values():
        id_variants.extend(mongo_id_variants(lead_id))
    unique_variants: dict[tuple[str, str], Any] = {}
    for value in id_variants:
        unique_variants[(type(value).__name__, str(value))] = value
    query_ids = list(unique_variants.values())

    history_projection = {
        "_id": 1, "lead_id": 1, "assignment_cycle_id": 1,
        "assigned_to_user_id": 1, "assigned_to_display_name": 1,
        "assigned_at": 1, "cycle_status": 1, "unassigned_at": 1,
        "property_code": 1, "reason": 1, "assigned_by": 1,
        "source_system": 1, "source_event_id": 1,
        "transfer_authorization": 1, "reassignment_decision_id": 1,
        "reassignment_state": 1, "first_valid_management_at": 1,
        "sla_first_management_status": 1, "notification_eligible": 1,
        "administratively_corrected": 1, "correction_cycle_id": 1,
        "correction_incident_id": 1, "corrects_assignment_cycle_id": 1,
        "restores_assignment_cycle_id": 1, "sla_performance_excluded": 1,
    }
    history_limit = min(max(int(limit), 1) * 10, 100000)
    history_rows = list(db["crm_assignment_cycles"].find(
        {"lead_id": {"$in": query_ids}}, history_projection,
    ).limit(history_limit + 1)) if query_ids else []
    if len(history_rows) > history_limit:
        raise RuntimeError("assignment history exceeded the reconciliation safety cap")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in history_rows:
        lead_key = str(row.get("lead_id") or "")
        if lead_key:
            grouped.setdefault(lead_key, []).append(row)
    for key in candidate_leads:
        grouped.setdefault(key, [row for row in candidate_rows if str(row.get("lead_id")) == key])

    lead_projection = {
        "_id": 1, "lifecycle": 1, "ejecutivo_asignado": 1,
        "prospecto.ejecutivo": 1, "prospecto.codigo": 1, "codigo": 1,
        "messages.role": 1, "messages.content": 1,
    }
    lead_rows = list(db["leads"].find(
        {"_id": {"$in": query_ids}}, lead_projection,
    ).limit(max(int(limit), 1))) if query_ids else []
    leads_by_id = {str(row.get("_id")): row for row in lead_rows if row.get("_id") is not None}

    all_cycle_ids = sorted({
        str(row.get("assignment_cycle_id") or "")
        for rows in grouped.values() for row in rows
        if row.get("assignment_cycle_id")
    })
    notifications = list(db["crm_notifications_v1"].find({
        "$or": [
            {"assignment_cycle_id": {"$in": all_cycle_ids}, "notification_type": "lead_assignment_hot"},
            {"assignment_cycle_ids": {"$in": all_cycle_ids}, "digest_type": "non_hot_digest_v1"},
        ],
        "state": {"$in": ["pending", "sending", "sent", "failed_retryable"]},
    }, {
        "_id": 1, "assignment_cycle_id": 1, "assignment_cycle_ids": 1,
        "notification_type": 1, "digest_type": 1, "state": 1,
        "recipient_user_id": 1, "actually_delivered": 1,
    }).limit(max(int(limit), 1))) if all_cycle_ids else []

    finding_rows: list[dict[str, Any]] = []
    leads_checked = 0
    for key, cycle_rows in grouped.items():
        lead_id = candidate_leads[key]
        lead = leads_by_id.get(key)
        leads_checked += 1
        if lead:
            integrity = assignment_context(
                db, lead, active_cycles_override=cycle_rows,
            )
            if integrity.get("status") == "conflict":
                finding_rows.append({
                    "issue_code": "canonical_cycle_mirror_inconsistency",
                    "lead_id": lead_id,
                    "issues": list(integrity.get("issues") or []),
                    "active_cycle_ids": sorted(str(row.get("assignment_cycle_id") or "") for row in integrity.get("active_cycles") or []),
                    "signature": ":".join(sorted(str(row.get("assignment_cycle_id") or "") for row in integrity.get("active_cycles") or [])) + ":" + ":".join(integrity.get("issues") or []),
                })
            finding_rows.extend(_chatbot_owner_findings(lead, cycle_rows, integrity))
        finding_rows.extend(_transition_findings(db, lead_id, cycle_rows))
        finding_rows.extend(_notification_findings(
            db, lead_id, cycle_rows, notifications=notifications,
        ))

    persisted = 0
    if persist_alerts:
        detection_run_id = detection_run_id or uuid.uuid4().hex
        for finding in finding_rows:
            record_assignment_integrity_alert(
                db, lead_id=finding.get("lead_id"), issue_code=str(finding["issue_code"]),
                evidence=finding, detected_at=current, detection_run_id=detection_run_id,
            )
            persisted += 1
    report = {
        "status": "completed",
        "lookback_days": lookback_days,
        "cycles_scanned": sum(len(rows) for rows in grouped.values()),
        "lookback_candidates": len(candidate_rows),
        "leads_checked": leads_checked,
        "findings": len(finding_rows),
        "alerts_upserted": persisted,
        "finding_rows": finding_rows,
    }
    if finding_rows:
        logger.error("[CRM_ASSIGNMENT_INTEGRITY] findings=%s leads=%s", len(finding_rows), leads_checked)
    return report


TRANSIENT_RECONCILIATION_ERRORS = (
    NetworkTimeout, AutoReconnect, ServerSelectionTimeoutError, ConnectionFailure,
)


def reconcile_assignment_integrity_with_retries(
    db: Any,
    *,
    max_attempts: int = 3,
    base_delay_seconds: float = 1.0,
    max_delay_seconds: float = 5.0,
    sleep=None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Retry the bounded read/audit pass on transient Mongo connection errors.

    One run id is reused across attempts, so alert persistence remains
    idempotent even when a connection drops after partial alert writes.
    """
    attempts = max(int(max_attempts), 1)
    wait = sleep or time.sleep
    run_id = str(kwargs.pop("detection_run_id", None) or uuid.uuid4().hex)
    for attempt in range(1, attempts + 1):
        try:
            report = reconcile_assignment_integrity(
                db, detection_run_id=run_id, **kwargs,
            )
            report["attempts"] = attempt
            return report
        except TRANSIENT_RECONCILIATION_ERRORS as exc:
            if attempt >= attempts:
                raise
            delay = min(float(base_delay_seconds) * (2 ** (attempt - 1)), float(max_delay_seconds))
            logger.warning(
                "[CRM_ASSIGNMENT_INTEGRITY] transient Mongo error attempt=%s/%s; retrying in %.1fs (%s)",
                attempt, attempts, delay, type(exc).__name__,
            )
            wait(delay)
    raise RuntimeError("unreachable reconciliation retry state")
