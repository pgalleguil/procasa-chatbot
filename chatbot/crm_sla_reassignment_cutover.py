"""Future-only cutover contract for automatic SLA reassignment.

The cutover is an explicit, timezone-aware configuration value.  This module
does not read ``now()`` as a configuration fallback and performs no I/O.
Breached timestamps are derived with the same ``add_business_minutes``
function used by the production SLA evaluator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import pytz

from .crm_metrics import commercial_sla_start_at, coerce_utc_datetime
from .crm_sla_alert_evaluator import add_business_minutes


SANTIAGO_TZ = pytz.timezone("America/Santiago")
CUTOVER_POLICY_VERSION = "crm_sla_reassignment_v1_future_only"
CUTOVER_ELIGIBLE = "CUTOVER_ELIGIBLE"
ELIGIBLE = CUTOVER_ELIGIBLE
PRE_CUTOVER_ALREADY_EXPIRED = "PRE_CUTOVER_ALREADY_EXPIRED"
CUTOVER_NOT_CONFIGURED = "CUTOVER_NOT_CONFIGURED"
CUTOVER_CONFIGURATION_INVALID = "CUTOVER_CONFIGURATION_INVALID"


class CutoverConfigurationError(ValueError):
    """Raised when a cutover value is missing, naive or malformed."""


@dataclass(frozen=True)
class CutoverEvaluation:
    cutover_at: datetime | None
    breached_at: datetime | None
    eligible: bool
    outcome: str
    policy_version: str = CUTOVER_POLICY_VERSION
    reason: str = ""


def parse_explicit_santiago_timestamp(value: Any) -> datetime:
    """Parse an explicit aware timestamp and normalize it to UTC.

    Configuration may use an ISO offset such as ``-03:00``/``-04:00`` or
    ``Z``.  A naive value is rejected instead of being silently assigned a
    timezone.  The aware value is interpreted through America/Santiago before
    the internal UTC normalization.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise CutoverConfigurationError("invalid_iso_timestamp") from exc
    else:
        raise CutoverConfigurationError("missing_or_invalid_timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CutoverConfigurationError("naive_timestamp_rejected")
    return parsed.astimezone(SANTIAGO_TZ).astimezone(timezone.utc)


def parse_configured_cutover(value: Any) -> datetime:
    return parse_explicit_santiago_timestamp(value)


def cutover_validation(value: Any) -> tuple[datetime | None, str]:
    """Return ``(cutover_utc, outcome)`` without raising for executor callers."""

    if value in (None, ""):
        return None, CUTOVER_NOT_CONFIGURED
    try:
        return parse_configured_cutover(value), ELIGIBLE
    except CutoverConfigurationError:
        return None, CUTOVER_CONFIGURATION_INVALID


def _aware_breach(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    return coerce_utc_datetime(value)


def canonical_sla_breached_at(
    cycle: Mapping[str, Any],
    *,
    lead: Mapping[str, Any] | None = None,
) -> datetime | None:
    """Resolve the reproducible breach instant for one cycle.

    Persisted breach/deadline fields are preferred when valid.  Otherwise the
    exact production deadline arithmetic is used from the cycle's effective
    SLA start and current temperature.  No alternate SLA formula is created.
    """

    for field in ("sla_breached_at", "sla_expired_at", "deadline_at", "sla_deadline_at"):
        parsed = _aware_breach(cycle.get(field))
        if parsed:
            return parsed

    assigned_at = cycle.get("assigned_at")
    effective_start = _aware_breach(cycle.get("sla_started_at"))
    if effective_start is None:
        effective_start = commercial_sla_start_at(assigned_at)
    if effective_start is None:
        return None

    lead = lead or {}
    temperature = str(
        cycle.get("temperature_at_assignment")
        or lead.get("lead_temperature_effective")
        or "NORMAL"
    ).upper()
    if temperature == "HOT":
        hot_start = _aware_breach(cycle.get("hot_started_at"))
        lifecycle = lead.get("lifecycle") if isinstance(lead.get("lifecycle"), Mapping) else {}
        hot_start = hot_start or _aware_breach(lifecycle.get("hot_since"))
        if hot_start and hot_start < effective_start:
            hot_start = effective_start
        deadline_start = hot_start or effective_start
        threshold = 60
    else:
        deadline_start = effective_start
        threshold = 180
    return add_business_minutes(deadline_start, threshold)


def evaluate_cutover(
    *,
    breached_at: Any,
    cutover_at: Any,
) -> CutoverEvaluation:
    cutover_utc, config_outcome = cutover_validation(cutover_at)
    if config_outcome == CUTOVER_NOT_CONFIGURED:
        return CutoverEvaluation(None, _aware_breach(breached_at), False, CUTOVER_NOT_CONFIGURED, reason="cutover_missing")
    if config_outcome == CUTOVER_CONFIGURATION_INVALID:
        return CutoverEvaluation(None, _aware_breach(breached_at), False, CUTOVER_CONFIGURATION_INVALID, reason="cutover_invalid")
    breach_utc = _aware_breach(breached_at)
    if breach_utc is None:
        return CutoverEvaluation(cutover_utc, None, False, CUTOVER_CONFIGURATION_INVALID, reason="breach_timestamp_invalid")
    if breach_utc < cutover_utc:
        return CutoverEvaluation(cutover_utc, breach_utc, False, PRE_CUTOVER_ALREADY_EXPIRED, reason="breach_before_cutover")
    return CutoverEvaluation(cutover_utc, breach_utc, True, ELIGIBLE, reason="breach_at_or_after_cutover")


def evaluate_cycle_cutover(
    cycle: Mapping[str, Any],
    *,
    lead: Mapping[str, Any] | None = None,
    cutover_at: Any,
) -> CutoverEvaluation:
    return evaluate_cutover(
        breached_at=canonical_sla_breached_at(cycle, lead=lead),
        cutover_at=cutover_at,
    )


def cutover_decision_validation(
    decision: Mapping[str, Any],
    *,
    configured_cutover_at: Any,
) -> CutoverEvaluation:
    """Validate a decision against current config and its own audit fields."""

    evaluation = evaluate_cutover(
        breached_at=decision.get("source_cycle_sla_breached_at") or decision.get("sla_breached_at"),
        cutover_at=configured_cutover_at,
    )
    if evaluation.outcome != ELIGIBLE:
        return evaluation
    decision_cutover = decision.get("reassignment_cutover_at")
    decision_cutover_utc, decision_outcome = cutover_validation(decision_cutover)
    if decision_outcome != ELIGIBLE or decision_cutover_utc != evaluation.cutover_at:
        return CutoverEvaluation(
            evaluation.cutover_at,
            evaluation.breached_at,
            False,
            CUTOVER_CONFIGURATION_INVALID,
            reason="decision_cutover_mismatch",
        )
    if str(decision.get("cutover_policy_version") or "") != CUTOVER_POLICY_VERSION:
        return CutoverEvaluation(
            evaluation.cutover_at,
            evaluation.breached_at,
            False,
            CUTOVER_CONFIGURATION_INVALID,
            reason="decision_cutover_policy_version_mismatch",
        )
    if decision.get("cutover_eligible") is not True:
        return CutoverEvaluation(
            evaluation.cutover_at,
            evaluation.breached_at,
            False,
            CUTOVER_CONFIGURATION_INVALID,
            reason="decision_not_marked_eligible",
        )
    return evaluation


def future_only_expired_cycle_query(cutover_at: Any) -> dict[str, Any]:
    """Build the conceptual future worker filter without connecting it.

    A worker must combine its normal active/open/eligibility predicates with
    this breach-time boundary.  It must never use only ``expired=true``.
    """

    parsed = parse_configured_cutover(cutover_at)
    return {
        "cycle_status": "active",
        "unassigned_at": None,
        "expired": True,
        "sla_breached_at": {"$gte": parsed},
    }
