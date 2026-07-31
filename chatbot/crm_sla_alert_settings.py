"""Settings for CRM SLA Alert domain — independent from config.py."""
from __future__ import annotations

import os
from datetime import datetime, timezone


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _parse_cutover(raw: str) -> datetime | None:
    if not raw: return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None: return None
    return parsed.astimezone(timezone.utc)


CRM_SLA_ALERT_CUTOVER_AT = _parse_cutover(_env("CRM_SLA_ALERT_CUTOVER_AT"))
CRM_SLA_ALERT_CUTOVER_RAW = _env("CRM_SLA_ALERT_CUTOVER_AT")
CRM_SLA_ALERT_CANARY_EXPIRES_AT = _parse_cutover(_env("CRM_SLA_ALERT_CANARY_EXPIRES_AT"))

# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

CRM_SLA_ALERTS_ENABLED = _env("CRM_SLA_ALERTS_ENABLED", "false").lower() == "true"
CRM_SLA_ALERTS_DRY_RUN = _env("CRM_SLA_ALERTS_DRY_RUN", "true").lower() == "true"
CRM_SLA_ALERTS_PERSIST = _env("CRM_SLA_ALERTS_PERSIST", "false").lower() == "true"
CRM_SLA_ALERTS_LIVE_SEND = _env("CRM_SLA_ALERTS_LIVE_SEND", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Canary
# ---------------------------------------------------------------------------

CRM_SLA_ALERTS_CANARY_MODE = _env("CRM_SLA_ALERTS_CANARY_MODE", "true").lower() == "true"

CRM_SLA_ALERTS_CANARY_RECIPIENT_USER_IDS: frozenset[str] = frozenset(
    uid.strip() for uid in _env("CRM_SLA_ALERTS_CANARY_RECIPIENT_USER_IDS").split(",")
    if uid.strip()
)

CRM_SLA_ALERTS_PERSIST_CONFIRMATION = _env("CRM_SLA_ALERTS_PERSIST_CONFIRMATION")
REQUIRED_PERSIST_CONFIRMATION = "PERSIST_CRM_SLA_ALERTS_V1"

CRM_SLA_ALERTS_POLICY_VERSION = _env("CRM_SLA_ALERTS_POLICY_VERSION", "crm_sla_alert_v1")

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

CRM_SLA_ALERTS_MAX_PER_RUN = int(_env("CRM_SLA_ALERTS_MAX_PER_RUN", "20") or "20")
CRM_SLA_ALERTS_MAX_PER_RECIPIENT_PER_RUN = int(
    _env("CRM_SLA_ALERTS_MAX_PER_RECIPIENT_PER_RUN", "5") or "5"
)
CRM_SLA_ALERTS_LEASE_SECONDS = int(_env("CRM_SLA_ALERTS_LEASE_SECONDS", "120") or "120")
CRM_SLA_ALERTS_MAX_ATTEMPTS = int(_env("CRM_SLA_ALERTS_MAX_ATTEMPTS", "3") or "3")
CRM_SLA_ALERTS_PROVIDER_TIMEOUT_SECONDS = int(
    _env("CRM_SLA_ALERTS_PROVIDER_TIMEOUT_SECONDS", "15") or "15"
)
CRM_SLA_ALERTS_MAX_CUTOVER_AGE_MINUTES = int(
    _env("CRM_SLA_ALERTS_MAX_CUTOVER_AGE_MINUTES", "15") or "15"
)

# ---------------------------------------------------------------------------
# Reassignment (disabled in this phase)
# ---------------------------------------------------------------------------

CRM_SLA_REASSIGNMENT_ENABLED = _env("CRM_SLA_REASSIGNMENT_ENABLED", "false").lower() == "true"
CRM_SLA_REASSIGNMENT_POLICY_VERSION = _env("CRM_SLA_REASSIGNMENT_POLICY_VERSION", "crm_sla_reassignment_v1")
CRM_SLA_REASSIGNMENT_GRACE_BUSINESS_MINUTES = _env("CRM_SLA_REASSIGNMENT_GRACE_BUSINESS_MINUTES")
CRM_SLA_REASSIGNMENT_GRACE_BUSINESS_MINUTES_PARSED: int | None = (
    int(CRM_SLA_REASSIGNMENT_GRACE_BUSINESS_MINUTES)
    if CRM_SLA_REASSIGNMENT_GRACE_BUSINESS_MINUTES and CRM_SLA_REASSIGNMENT_GRACE_BUSINESS_MINUTES.isdigit()
    else None
)


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------

def validate_live_send_config() -> dict:
    if not CRM_SLA_ALERTS_LIVE_SEND:
        return {"valid": True}
    failures = []
    if not CRM_SLA_ALERTS_ENABLED: failures.append("CRM_SLA_ALERTS_ENABLED must be true")
    if CRM_SLA_ALERTS_DRY_RUN: failures.append("CRM_SLA_ALERTS_DRY_RUN must be false")
    if not CRM_SLA_ALERTS_PERSIST: failures.append("CRM_SLA_ALERTS_PERSIST must be true")
    if CRM_SLA_ALERT_CUTOVER_AT is None: failures.append("CRM_SLA_ALERT_CUTOVER_AT must be valid")
    if failures:
        return {"valid": False, "reason": "invalid_live_send_configuration: " + "; ".join(failures)}
    return {"valid": True}


def validate_check_config() -> dict:
    """Validate preconditions for --check (read-only, any valid cutover allowed)."""
    failures = []
    if CRM_SLA_ALERT_CUTOVER_AT is None:
        failures.append("CRM_SLA_ALERT_CUTOVER_AT must be a valid ISO datetime with timezone")
    if failures:
        return {"valid": False, "reason": "invalid_check_configuration: " + "; ".join(failures)}
    return {"valid": True}


def validate_persist_config() -> dict:
    """Validate all preconditions for persistence (with or without sending)."""
    failures = []
    if not CRM_SLA_ALERTS_ENABLED: failures.append("CRM_SLA_ALERTS_ENABLED must be true")
    if CRM_SLA_ALERTS_DRY_RUN: failures.append("CRM_SLA_ALERTS_DRY_RUN must be false")
    if not CRM_SLA_ALERTS_PERSIST: failures.append("CRM_SLA_ALERTS_PERSIST must be true")
    if CRM_SLA_ALERTS_LIVE_SEND: failures.append("CRM_SLA_ALERTS_LIVE_SEND must be false in this phase")
    if CRM_SLA_ALERT_CUTOVER_AT is None: failures.append("CRM_SLA_ALERT_CUTOVER_AT must be valid")
    if CRM_SLA_ALERTS_PERSIST_CONFIRMATION != REQUIRED_PERSIST_CONFIRMATION:
        failures.append("CRM_SLA_ALERTS_PERSIST_CONFIRMATION must be exactly 'PERSIST_CRM_SLA_ALERTS_V1'")
    if not CRM_SLA_ALERTS_CANARY_MODE: failures.append("CRM_SLA_ALERTS_CANARY_MODE must be true")
    if not CRM_SLA_ALERTS_CANARY_RECIPIENT_USER_IDS:
        failures.append("CRM_SLA_ALERTS_CANARY_RECIPIENT_USER_IDS must not be empty")
    if failures:
        return {"valid": False, "reason": "invalid_persist_configuration: " + "; ".join(failures)}
    return {"valid": True}


def validate_cutover_safe_for_persistence(now=None) -> dict:
    """Cutover must be fixed, on today's business day, within [cutover, expires_at] window.

    The same cutover can be reused across multiple executions during the
    business day.  expires_at cannot exceed 19:00 of the same day.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    import pytz
    chile_tz = pytz.timezone("America/Santiago")
    current = now or _dt.now(_tz.utc)
    current_cl = current.astimezone(chile_tz)

    cutover = CRM_SLA_ALERT_CUTOVER_AT
    expires = CRM_SLA_ALERT_CANARY_EXPIRES_AT

    if cutover is None:
        return {"valid": False, "reason": "cutover_not_safe_for_persistence: cutover is None"}

    # Cutover cannot be in the future
    if cutover > current:
        return {"valid": False, "reason": "cutover_not_safe_for_persistence: cutover is in the future"}

    # Cutover must be on today's business day
    cutover_cl = cutover.astimezone(chile_tz)
    if cutover_cl.date() != current_cl.date():
        return {"valid": False, "reason": (
            f"cutover_not_safe_for_persistence: cutover date {cutover_cl.date()} "
            f"does not match today {current_cl.date()}"
        )}

    # expires_at is required for --persist
    if expires is None:
        return {"valid": False, "reason": "canary_expiration_required: CRM_SLA_ALERT_CANARY_EXPIRES_AT must be set for --persist"}

    expires_cl = expires.astimezone(chile_tz)

    # expires must be after cutover
    if expires <= cutover:
        return {"valid": False, "reason": "cutover_not_safe_for_persistence: expires_at is not after cutover"}

    # expires must not exceed 19:00 on the same day
    end_of_day = chile_tz.localize(_dt(
        cutover_cl.year, cutover_cl.month, cutover_cl.day, 19, 0, 0
    ))
    if expires > end_of_day:
        return {"valid": False, "reason": "cutover_not_safe_for_persistence: expires_at exceeds 19:00"}

    # now must be between cutover and expires
    if current < cutover:
        return {"valid": False, "reason": "cutover_not_safe_for_persistence: now is before cutover"}
    if current > expires:
        return {"valid": False, "reason": "cutover_not_safe_for_persistence: now is after expires_at"}

    return {"valid": True}


def validate_indexes_config() -> dict:
    """Validate preconditions for --ensure-indexes."""
    failures = []
    if CRM_SLA_ALERTS_LIVE_SEND: failures.append("CRM_SLA_ALERTS_LIVE_SEND must be false")
    if CRM_SLA_ALERTS_PERSIST_CONFIRMATION != REQUIRED_PERSIST_CONFIRMATION:
        failures.append("CRM_SLA_ALERTS_PERSIST_CONFIRMATION must be exactly 'PERSIST_CRM_SLA_ALERTS_V1'")
    if failures:
        return {"valid": False, "reason": "invalid_indexes_configuration: " + "; ".join(failures)}
    return {"valid": True}
