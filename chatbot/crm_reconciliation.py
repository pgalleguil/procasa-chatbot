"""Read-only universal CRM assignment reconciliation.

The evaluator returns evidence and counters only.  It never creates pending
notifications and never calls a delivery provider.
"""
from __future__ import annotations

from collections import Counter
from datetime import timezone
import uuid

from .crm_metrics import INSTRUMENTATION_CUTOVER, coerce_utc_datetime, historical_temperature_at, utc_now
from .crm_notifications import individual_identity, validate_volume, VolumeLimits


def _active_users(users):
    result = {}
    for user in users:
        key = str(user.get("_id") or user.get("user_id") or user.get("nombre") or "").strip()
        if key and user.get("active", user.get("activo", True)):
            result[key] = user
    return result


def reconcile_shadow(*, leads, cycles, users, deliveries, scan_from, scan_to,
                     run_started_at=None, limits=VolumeLimits()) -> dict:
    started = coerce_utc_datetime(run_started_at) or utc_now()
    start = coerce_utc_datetime(scan_from)
    end = coerce_utc_datetime(scan_to)
    cutover = coerce_utc_datetime(INSTRUMENTATION_CUTOVER)
    if not start or not end or start >= end:
        raise ValueError("invalid reconciliation interval")

    lead_by_id = {str(lead.get("_id")): lead for lead in leads if lead.get("_id") is not None}
    active_users = _active_users(users)
    delivered = {
        doc.get("individual_identity") for doc in deliveries
        if doc.get("state") in {"sent", "sending"} and doc.get("individual_identity")
    }
    digested_leads = {
        str(lead_id) for doc in deliveries if doc.get("state") == "sent"
        for lead_id in ((doc.get("metadata") or {}).get("lead_ids") or [])
    }
    results, volume = [], Counter()
    seen_cycles = set()
    counters = Counter()

    for cycle in cycles:
        lead_id = str(cycle.get("lead_id") or "")
        cycle_id = str(cycle.get("assignment_cycle_id") or "")
        assigned = coerce_utc_datetime(cycle.get("assigned_at"))
        recipient = str(cycle.get("assigned_to_user_id") or "").strip()
        reason = None
        status = "expected"
        if not lead_id or lead_id not in lead_by_id or not cycle_id:
            status, reason = "quarantined", "missing_canonical_identity"
        elif cycle_id in seen_cycles:
            status, reason = "ambiguous", "duplicate_assignment_cycle_id"
        elif not assigned:
            status, reason = "quarantined", "invalid_assigned_at"
        elif assigned < cutover or assigned < start or assigned >= end:
            counters["pre_cutover_exclusions" if assigned < cutover else "outside_scan"] += 1
            continue
        elif cycle.get("unassigned_at"):
            status, reason = "suppressed", "cycle_not_active"
        elif recipient not in active_users:
            status, reason = "suppressed", "inactive_or_unknown_executive"
            counters["inactive_executive_exclusions"] += 1
        seen_cycles.add(cycle_id)

        lead = lead_by_id.get(lead_id) or {}
        temperature = historical_temperature_at(lead, assigned) or "UNKNOWN"
        notification_type = "lead_assignment_hot" if temperature == "HOT" else "lead_assignment_digest_candidate"
        identity = None
        if status == "expected":
            identity = individual_identity(
                lead_id=lead_id, assignment_cycle_id=cycle_id,
                notification_type=notification_type, recipient_user_id=recipient,
            )
            if identity in delivered or lead_id in digested_leads:
                status = "already_delivered"
            else:
                status = "missing_notification"
                counters[f"missing_{temperature.lower()}"] += 1
                volume[recipient] += 1
        counters[status] += 1
        results.append({
            "lead_id": lead_id, "assignment_cycle_id": cycle_id,
            "recipient_user_id": recipient, "temperature": temperature,
            "status": status, "reason": reason, "individual_identity": identity,
        })

    volume_check = validate_volume(
        total=sum(volume.values()), by_executive=volume, per_minute=0, digests=0, limits=limits,
    )
    if not volume_check["allowed"]:
        for row in results:
            if row["status"] == "missing_notification":
                row["status"], row["reason"] = "suppressed", "circuit_breaker"
        counters["circuit_breaker_open"] = 1

    completed = utc_now()
    return {
        "run_id": str(uuid.uuid4()), "started_at": started, "completed_at": completed,
        "scan_from": start, "scan_to": end, "last_successful_scan_at": completed,
        "expected_total": sum(1 for row in results if row["status"] in {"already_delivered", "missing_notification"}),
        "missing_hot": counters["missing_hot"], "missing_cold": counters["missing_cold"],
        "missing_unknown": counters["missing_unknown"],
        "already_delivered": counters["already_delivered"], "suppressed": counters["suppressed"],
        "quarantined": counters["quarantined"], "ambiguous": counters["ambiguous"],
        "inactive_executives": counters["inactive_executive_exclusions"],
        "pre_cutover_exclusions": counters["pre_cutover_exclusions"],
        "volume_by_executive": dict(volume), "circuit_breaker": volume_check,
        "results": results, "persisted": False, "deliverable_records_created": 0,
    }


def persist_shadow_run(db, run: dict) -> dict:
    """Persist audit evidence only; this cannot create a deliverable record."""
    document = dict(run)
    document["persisted"] = True
    document["deliverable_records_created"] = 0
    db["crm_notification_reconciliation_runs"].update_one(
        {"run_id": document["run_id"]}, {"$setOnInsert": document}, upsert=True,
    )
    return document
