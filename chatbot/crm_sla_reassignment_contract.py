"""CRM SLA Reassignment Contract — pure logic, no MongoDB, WhatsApp, workers.

Determines reassignment eligibility from an SLA decision without executing
any reassignment.  Always disabled in this phase.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .crm_sla_alert_settings import (
    CRM_SLA_REASSIGNMENT_ENABLED,
    CRM_SLA_REASSIGNMENT_POLICY_VERSION,
    CRM_SLA_REASSIGNMENT_GRACE_BUSINESS_MINUTES_PARSED,
)

# Future collection: crm_lead_reassignment_candidates_v1


def _coerce_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return None


def add_grace_minutes(breached_at: datetime, grace_minutes: int) -> datetime | None:
    """Add grace business minutes to breached_at. Returns None if grace not configured.

    For now, uses a simple linear addition of calendar minutes.  A future
    iteration should use calculate_business_minutes in reverse.
    """
    if breached_at is None or not grace_minutes:
        return None
    return breached_at + __import__("datetime").timedelta(minutes=grace_minutes)


def build_reassignment_preview(
    sla_decision: dict,
    *,
    settings: Optional[dict] = None,
) -> dict:
    """Return a pure preview of reassignment eligibility. Never persists or executes.

    sla_decision: dict with keys assignment_cycle_id, lead_id, recipient_user_id,
                  alert_level, sla_breached_at, has_valid_management, cycle_active,
                  lead_closed, executive_current.
    """
    # Always disabled in this phase
    if not CRM_SLA_REASSIGNMENT_ENABLED:
        return {
            "eligible_now": False,
            "state": "disabled",
            "reason": "CRM_SLA_REASSIGNMENT_ENABLED is false",
            "policy_version": CRM_SLA_REASSIGNMENT_POLICY_VERSION,
            "assignment_cycle_id": sla_decision.get("assignment_cycle_id"),
            "lead_id": sla_decision.get("lead_id"),
            "current_recipient_user_id": sla_decision.get("recipient_user_id"),
            "sla_breached_at": sla_decision.get("sla_breached_at"),
            "grace_expires_at": None,
        }

    # These are the future checks when enabled
    grace_minutes = CRM_SLA_REASSIGNMENT_GRACE_BUSINESS_MINUTES_PARSED
    breached_at = _coerce_dt(sla_decision.get("sla_breached_at"))

    base = {
        "eligible_now": False,
        "policy_version": CRM_SLA_REASSIGNMENT_POLICY_VERSION,
        "assignment_cycle_id": sla_decision.get("assignment_cycle_id"),
        "lead_id": sla_decision.get("lead_id"),
        "current_recipient_user_id": sla_decision.get("recipient_user_id"),
        "sla_breached_at": sla_decision.get("sla_breached_at"),
        "grace_expires_at": None,
        "state": "disabled",
        "reason": "",
    }

    # Preconditions that make reassignment ineligible
    if sla_decision.get("has_valid_management"):
        base["state"] = "ineligible"
        base["reason"] = "valid_human_management_exists"
        return base

    if sla_decision.get("alert_level") != "breached":
        base["state"] = "ineligible"
        base["reason"] = "not_breached"
        return base

    if not sla_decision.get("cycle_active"):
        base["state"] = "ineligible"
        base["reason"] = "cycle_not_active"
        return base

    if sla_decision.get("lead_closed"):
        base["state"] = "ineligible"
        base["reason"] = "lead_closed"
        return base

    if sla_decision.get("executive_current") != sla_decision.get("recipient_user_id"):
        base["state"] = "ineligible"
        base["reason"] = "reassigned_or_ownership_changed"
        return base

    # Grace period check
    if grace_minutes is None:
        base["state"] = "grace_not_configured"
        base["reason"] = "CRM_SLA_REASSIGNMENT_GRACE_BUSINESS_MINUTES not set"
        return base

    grace_expires = add_grace_minutes(breached_at, grace_minutes) if breached_at else None
    base["grace_expires_at"] = grace_expires.isoformat() if grace_expires else None

    if grace_expires and grace_expires > datetime.now(timezone.utc):
        base["state"] = "grace_period"
        base["reason"] = "grace_period_not_elapsed"
        return base

    base["state"] = "potentially_eligible"
    base["eligible_now"] = True
    base["reason"] = "breached_and_grace_elapsed"
    return base
