"""Fixed production settings for CRM SLA alerts.

The only Render-controlled switch for this domain is
``CRM_SLA_ALERTS_ENABLED``.  All other SLA behavior is deliberately fixed in
code so a partial or stale environment cannot silently disable delivery.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytz


def _enabled() -> bool:
    return os.getenv("CRM_SLA_ALERTS_ENABLED", "false").strip().lower() == "true"


CRM_SLA_ALERTS_ENABLED = _enabled()


def sla_alerts_enabled() -> bool:
    """Read the live env flag at call time (not only at import time).

    The orchestrator/worker loops call this every cycle so a change in the
    Render environment takes effect without a full process restart, and so a
    disabled state is reported accurately to the health/observability layer.
    """
    return _enabled()

# Fixed production policy.
DRY_RUN = False
PERSIST = True
LIVE_SEND = True
CANARY_MODE = False
PERSIST_CONFIRMATION = "PERSIST_CRM_SLA_ALERTS_V1"
MAX_PER_RUN = 100
MAX_PER_RECIPIENT_PER_RUN = 50
REASSIGNMENT_ENABLED = False
TIMEZONE = "America/Santiago"
CHILE_TZ = pytz.timezone(TIMEZONE)
BUSINESS_START_HOUR = 9
BUSINESS_END_HOUR = 19
BUSINESS_DAYS = frozenset({0, 1, 2, 3, 4})

# Fixed cutover: only cycles assigned from 03/08/2026 09:00 Chile time.
CUTOVER_AT = CHILE_TZ.localize(datetime(2026, 8, 3, 9, 0)).astimezone(timezone.utc)
POLICY_VERSION = "crm_sla_alert_v1"
REASSIGNMENT_POLICY_VERSION = "crm_sla_reassignment_v1"

# Catch-up guard: alerts are only produced for cycles whose SLA started at/after
# this timestamp, so the accumulated backlog (leads that breached while the
# orchestrator was down) is never re-sent in a burst on recovery.  When unset,
# the pipeline stores a one-time recovery marker on its first run.
CATCH_UP_CUTOVER_AT = os.getenv("CRM_SLA_ALERTS_CATCH_UP_CUTOVER_AT", "").strip() or None

# Operational safety constants are also code-owned, not environment-owned.
LEASE_SECONDS = 120
MAX_ATTEMPTS = 3
PROVIDER_TIMEOUT_SECONDS = 15


def validate_live_send_config() -> dict:
    """Return the fixed policy validation result.

    The enabled switch is handled before any database work by the
    orchestrator/pipeline/worker.  When enabled, this configuration is
    always production persistence + live-send mode.
    """
    if not CRM_SLA_ALERTS_ENABLED:
        return {"valid": True, "reason": "disabled"}
    return {"valid": True}


def validate_persist_config() -> dict:
    return validate_live_send_config()


def validate_check_config() -> dict:
    return {"valid": True}


def validate_cutover_safe_for_persistence(now=None) -> dict:
    current = now or datetime.now(timezone.utc)
    if current < CUTOVER_AT:
        return {"valid": False, "reason": "cutover_not_reached"}
    return {"valid": True}


def validate_indexes_config() -> dict:
    return {"valid": True}
