"""Read-only evaluator for the future CRM SLA alert domain.

It deliberately has no database writes and no provider dependency.  Production
activation requires a separate task and an explicit feature flag.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable, Mapping

from .crm_metrics import calculate_sla, coerce_utc_datetime

MESSAGE_DOMAIN = "crm_sla_alert"
RECIPIENT_ROLE = "executive"
ALERTS = {
    "HOT": ((45, "warning"), (60, "breached")),
    "NORMAL": ((150, "warning"), (180, "breached")),
}
BLOCKED_STAGES = {"CLOSED", "CLOSED_WON", "CLOSED_LOST", "ARCHIVED"}
WAITING_ASSIGNMENT_TYPES = {"WAITING_PROPERTY", "WAITING_INVENTORY_SYNC", "NO_PROPERTY", "MISSING_PROPERTY"}

def _user_id(user: Mapping) -> str:
    return str(user.get("_id") or user.get("user_id") or "")

def _active(user: Mapping) -> bool:
    return bool(user.get("active", user.get("activo", True)))

def _phone(user: Mapping) -> str:
    return str(user.get("telefono") or user.get("phone") or user.get("whatsapp") or "")

def evaluate_sla_alert_dry_run(*, leads: Iterable[Mapping], cycles: Iterable[Mapping],
                               users: Iterable[Mapping], as_of, activation_at=None) -> dict:
    """Return candidates only; never writes, claims, or contacts a provider.

    activation_at is mandatory for actionable candidates: omitting it fails
    closed, so a dry-run cannot turn historical cycles into future alerts.
    """
    now = coerce_utc_datetime(as_of)
    activation = coerce_utc_datetime(activation_at)
    if not now:
        raise ValueError("invalid as_of")
    lead_by_id = {str(lead.get("_id")): lead for lead in leads if lead.get("_id") is not None}
    users_by_id = {_user_id(user): user for user in users if _user_id(user)}
    rows, excluded = [], Counter()
    seen = set()
    for cycle in cycles:
        lead_id = str(cycle.get("lead_id") or "")
        cycle_id = str(cycle.get("assignment_cycle_id") or "")
        recipient = str(cycle.get("assigned_to_user_id") or "")
        lead = lead_by_id.get(lead_id)
        assigned_at = coerce_utc_datetime(cycle.get("assigned_at"))
        user = users_by_id.get(recipient)
        if not lead or not cycle_id or not recipient or not assigned_at:
            excluded["incomplete_assignment"] += 1; continue
        if not activation or assigned_at < activation:
            excluded["pre_activation"] += 1; continue
        if cycle.get("cycle_status") not in (None, "active") or cycle.get("unassigned_at"):
            excluded["inactive_cycle"] += 1; continue
        if lead.get("first_valid_management_at") or cycle.get("first_valid_management_at"):
            excluded["human_management"] += 1; continue
        stage = str(lead.get("pipeline_stage") or lead.get("stage") or "").upper()
        if stage in BLOCKED_STAGES:
            excluded["closed"] += 1; continue
        assignment_type = str(lead.get("assignment_type") or lead.get("status") or "").upper()
        if assignment_type in WAITING_ASSIGNMENT_TYPES:
            excluded["waiting_assignment"] += 1; continue
        if not user or not _active(user) or not _phone(user):
            excluded["invalid_recipient"] += 1; continue
        temperature = "HOT" if str(lead.get("lead_temperature_effective") or "").upper() == "HOT" else "NORMAL"
        sla = calculate_sla(assigned_at=assigned_at, first_valid_management_at=None, now=now)
        minutes = sla.get("minutes")
        if minutes is None:
            excluded["invalid_sla"] += 1; continue
        threshold, level = next(((m, l) for m, l in reversed(ALERTS[temperature]) if minutes >= m), (None, None))
        if threshold is None:
            excluded["below_threshold"] += 1; continue
        alert_type = f"{temperature.lower()}_{level}"
        key = f"{cycle_id}|{alert_type}|{threshold}|{recipient}"
        if key in seen:
            excluded["duplicate_candidate"] += 1; continue
        seen.add(key)
        rows.append({
            "message_domain": MESSAGE_DOMAIN, "message_type": alert_type,
            "recipient_role": RECIPIENT_ROLE, "assignment_cycle_id": cycle_id,
            "lead_id": lead_id, "recipient_user_id": recipient,
            "threshold_business_minutes": threshold, "business_minutes": minutes,
            "idempotency_key": key, "delivery_mode": "dry_run",
            "provider_calls": 0, "state": "would_alert",
        })
    return {"as_of": now, "alerts": rows, "excluded": dict(excluded),
            "provider_calls": 0, "writes": 0, "enabled": False, "dry_run": True}
