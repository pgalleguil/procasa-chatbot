"""Pure, deterministic pricing-intelligence Engine V1.

The engine consumes already-resolved foundation data.  It deliberately does
not know about MongoDB, owner-facing copy, authorization, or persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import floor, isfinite
from typing import Any, Mapping


ENGINE_V1_METHODOLOGY_VERSION = "engine_v1"

RECOMMENDATION_MAINTAIN = "MAINTAIN"
RECOMMENDATION_MARKET_REPOSITIONING = "MARKET_REPOSITIONING"
RECOMMENDATION_OBSERVE = "OBSERVE"
RECOMMENDATION_IMPROVE_EXPOSURE = "IMPROVE_EXPOSURE"
RECOMMENDATION_EXECUTIVE_REVIEW = "EXECUTIVE_REVIEW"

ELIGIBILITY_GREEN = "GREEN"
ELIGIBILITY_AMBER = "AMBER"
ELIGIBILITY_RED = "RED"

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"

POSITION_ALIGNED = "ALIGNED"
POSITION_SLIGHTLY_HIGH = "SLIGHTLY_HIGH"
POSITION_HIGH = "HIGH"
POSITION_VERY_HIGH = "VERY_HIGH"
POSITION_UNAVAILABLE = "UNAVAILABLE"

OWNER_NO_ACTION = "NO_ACTION"
OWNER_DISCUSS_WITH_EXECUTIVE = "DISCUSS_WITH_EXECUTIVE"
OWNER_CAN_REQUEST_PRICE_CHANGE = "CAN_REQUEST_PRICE_CHANGE"

COOLDOWN_DAYS = 30
TRIVIAL_GAP_PCT = 2.0


@dataclass(frozen=True)
class EngineV1Decision:
    """Structured decision output; reasons and warnings are machine codes."""

    status: str
    recommendation: str
    eligibility: str
    confidence: str
    market_position: str
    gap_to_p75_pct: float | None
    gradual_price_uf: float | None
    competitive_reference_uf: float | None
    owner_action: str
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    methodology_version: str = ENGINE_V1_METHODOLOGY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "recommendation": self.recommendation,
            "eligibility": self.eligibility,
            "confidence": self.confidence,
            "market_position": self.market_position,
            "gap_to_p75_pct": self.gap_to_p75_pct,
            "gradual_price_uf": self.gradual_price_uf,
            "competitive_reference_uf": self.competitive_reference_uf,
            "owner_action": self.owner_action,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "methodology_version": self.methodology_version,
        }


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _text(value: Any) -> str:
    return str(value or "").strip().upper()


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _cohort_values(cohort: Mapping[str, Any] | None) -> tuple[int, float | None, float | None, float | None, bool, bool, bool]:
    values = cohort if isinstance(cohort, Mapping) else {}
    raw_n = values.get("n", values.get("count", 0))
    try:
        count = max(0, int(raw_n))
    except (TypeError, ValueError):
        count = 0
    p60 = _number(values.get("p60", values.get("p60_uf")))
    p75 = _number(values.get("p75", values.get("p75_uf")))
    p90 = _number(values.get("p90", values.get("p90_uf")))
    deterministic = bool(values.get("deterministic", False))
    fingerprint = bool(values.get("fingerprint", values.get("cohort_fingerprint")))
    robustness_warning = bool(values.get("robustness_warning", False))
    return count, p60, p75, p90, deterministic, fingerprint, robustness_warning


def _market_position(current: float | None, p60: float | None, p75: float | None, p90: float | None) -> str:
    if current is None or p60 is None or p75 is None or p90 is None:
        return POSITION_UNAVAILABLE
    if min(p60, p75, p90) <= 0 or not (p60 <= p75 <= p90):
        return POSITION_UNAVAILABLE
    if current <= p60:
        return POSITION_ALIGNED
    if current <= p75:
        return POSITION_SLIGHTLY_HIGH
    if current <= p90:
        return POSITION_HIGH
    return POSITION_VERY_HIGH


def _cohort_quality(count: int, deterministic: bool, robustness_warning: bool) -> str:
    if count < 8:
        return "LOW"
    if count < 20 or not deterministic or robustness_warning:
        return "MODERATE"
    return "HIGH"


def _confidence(cohort_quality: str, commercial: Mapping[str, Any], *, red: bool) -> str:
    if red or cohort_quality == "LOW":
        return CONFIDENCE_LOW
    if cohort_quality != "HIGH":
        return CONFIDENCE_LOW
    values = (
        _text(commercial.get("confidence_30d", commercial.get("demand_confidence_30d"))),
        _text(commercial.get("confidence_90d", commercial.get("demand_confidence_90d"))),
    )
    return CONFIDENCE_HIGH if values and all(value == "HIGH" for value in values) else CONFIDENCE_MEDIUM


def _cooldown_active(page_as_of: Any, last_price_change_at: Any) -> tuple[bool, bool]:
    page = _parse_datetime(page_as_of)
    change = _parse_datetime(last_price_change_at)
    if last_price_change_at is None:
        return False, False
    if page is None or change is None:
        return False, True
    elapsed = page - change
    if elapsed.total_seconds() < 0:
        return False, True
    return elapsed < timedelta(days=COOLDOWN_DAYS), False


def _initial_rounding_step(current: float) -> int:
    if current < 1000:
        return 10
    if current <= 3000:
        return 25
    if current <= 6000:
        return 50
    return 100


def _safe_gradual_target(current: float, mathematical_target: float) -> float:
    """Floor a reduction target without exceeding the mathematical target."""

    initial = _initial_rounding_step(current)
    lower_steps = {100: (50, 10), 50: (25, 10), 25: (10,), 10: ()}
    steps = (initial, *lower_steps[initial])
    tolerance = current * 0.01
    candidate = floor(mathematical_target / initial) * initial
    for step in steps:
        candidate = floor(mathematical_target / step) * step
        if mathematical_target - candidate <= tolerance or step == steps[-1]:
            break
    candidate = min(float(current), float(candidate), float(mathematical_target))
    return float(int(candidate)) if candidate.is_integer() else round(candidate, 2)


def evaluate_engine_v1(
    *,
    operation: str | None,
    current_price_uf: Any,
    cohort: Mapping[str, Any] | None,
    commercial_response: Mapping[str, Any] | None,
    exposure: Mapping[str, Any] | None,
    observation: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
) -> EngineV1Decision:
    """Evaluate Engine V1 from resolved, side-effect-free foundation inputs."""

    commercial = commercial_response if isinstance(commercial_response, Mapping) else {}
    exposure_values = exposure if isinstance(exposure, Mapping) else {}
    observation_values = observation if isinstance(observation, Mapping) else {}
    context_values = context if isinstance(context, Mapping) else {}
    current = _number(current_price_uf)
    count, p60, p75, p90, deterministic, fingerprint, robustness_warning = _cohort_values(cohort)
    quality = _cohort_quality(count, deterministic and fingerprint, robustness_warning)
    position = POSITION_UNAVAILABLE if count < 8 else _market_position(current, p60, p75, p90)
    reasons: list[str] = []
    warnings: list[str] = []

    operation_code = _text(operation)
    operation_ambiguous = bool(context_values.get("operation_ambiguity", False))
    geography_valid = context_values.get("geography_valid", True) is not False
    critical_conflict = bool(context_values.get("critical_conflict", False))
    exposure_state = _text(exposure_values.get("state")) or "UNKNOWN"
    if not exposure_state:
        exposure_state = "UNKNOWN"

    if position == POSITION_UNAVAILABLE:
        warnings.append("MARKET_POSITION_UNAVAILABLE")
    else:
        reasons.append(f"MARKET_POSITION_{position}")
    if count < 8:
        reasons.append("INSUFFICIENT_MARKET_COMPARABLES")
    if not deterministic or not fingerprint:
        warnings.append("COHORT_NOT_REPRODUCIBLE")
    if robustness_warning:
        warnings.append("COHORT_ROBUSTNESS_WARNING")
    if exposure_state == "LOW":
        warnings.append("EXPOSURE_LOW")
    elif exposure_state == "PARTIAL":
        warnings.append("EXPOSURE_PARTIAL")

    cooldown, cooldown_unverifiable = _cooldown_active(
        context_values.get("page_as_of"),
        context_values.get("last_price_change_at"),
    )
    if cooldown:
        warnings.append("PRICE_CHANGE_COOLDOWN_ACTIVE")
    elif cooldown_unverifiable:
        warnings.append("PRICE_CHANGE_DATE_UNVERIFIABLE")

    confidence = _confidence(quality, commercial, red=False)
    gap: float | None = None
    if current is not None and p75 is not None and current > p75:
        gap = round((current - p75) / current * 100.0, 2)

    critical_reasons: list[str] = []
    if current is None or current <= 0:
        critical_reasons.append("INVALID_CURRENT_PRICE")
    if operation_ambiguous or operation_code not in {"VENTA", "ARRIENDO"}:
        critical_reasons.append("AMBIGUOUS_OR_INVALID_OPERATION")
    if not geography_valid:
        critical_reasons.append("INVALID_GEOGRAPHY")
    if critical_conflict:
        critical_reasons.append("CRITICAL_DATA_CONFLICT")
    if position == POSITION_UNAVAILABLE and count >= 8:
        critical_reasons.append("MARKET_POSITION_UNAVAILABLE")
    if critical_reasons:
        reasons.extend(critical_reasons)
        return EngineV1Decision(
            status="READY",
            recommendation=RECOMMENDATION_EXECUTIVE_REVIEW,
            eligibility=ELIGIBILITY_RED,
            confidence=CONFIDENCE_LOW,
            market_position=position,
            gap_to_p75_pct=gap,
            gradual_price_uf=None,
            competitive_reference_uf=None,
            owner_action=OWNER_NO_ACTION,
            reasons=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    if operation_code == "ARRIENDO":
        reasons.append("RENT_PRICING_NOT_SUPPORTED_V1")
        return EngineV1Decision(
            status="READY",
            recommendation=RECOMMENDATION_EXECUTIVE_REVIEW,
            eligibility=ELIGIBILITY_RED,
            confidence=CONFIDENCE_LOW if count < 20 else confidence,
            market_position=position,
            gap_to_p75_pct=gap,
            gradual_price_uf=None,
            competitive_reference_uf=None,
            owner_action=OWNER_NO_ACTION,
            reasons=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    if count < 8:
        return EngineV1Decision(
            status="READY",
            recommendation=RECOMMENDATION_EXECUTIVE_REVIEW,
            eligibility=ELIGIBILITY_RED,
            confidence=CONFIDENCE_LOW,
            market_position=position,
            gap_to_p75_pct=gap,
            gradual_price_uf=None,
            competitive_reference_uf=None,
            owner_action=OWNER_NO_ACTION,
            reasons=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    signal_30d = _text(commercial.get("signal_30d", commercial.get("demand_signal_30d")))
    confidence_30d = _text(commercial.get("confidence_30d", commercial.get("demand_confidence_30d")))
    confidence_90d = _text(commercial.get("confidence_90d", commercial.get("demand_confidence_90d")))
    if signal_30d == "ZERO_UNCERTAIN":
        warnings.append("ZERO_UNCERTAIN_NOT_PRICING_EVIDENCE")
    elif signal_30d == "OBSERVED_ZERO_PARTIAL":
        warnings.append("OBSERVED_ZERO_PARTIAL_COMPLEMENTARY_ONLY")
    strong_recent = signal_30d == "STRONG_RECENT"
    if strong_recent:
        warnings.append("STRONG_RECENT_DEMAND_NO_AUTOMATIC_REDUCTION")
    if confidence_30d in {"LOW", "UNKNOWN", "PARTIAL", ""} or confidence_90d in {"LOW", "UNKNOWN", "PARTIAL", ""}:
        warnings.append("COMMERCIAL_CONFIDENCE_LIMITED")

    if gap is None or gap < TRIVIAL_GAP_PCT:
        reasons.append("TRIVIAL_OR_NON_POSITIVE_GAP")
        return EngineV1Decision(
            status="READY",
            recommendation=RECOMMENDATION_MAINTAIN,
            eligibility=ELIGIBILITY_AMBER,
            confidence=confidence,
            market_position=position,
            gap_to_p75_pct=gap,
            gradual_price_uf=None,
            competitive_reference_uf=None,
            owner_action=OWNER_NO_ACTION,
            reasons=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    if exposure_state == "LOW":
        reasons.append("LOW_EXPOSURE_PRECEDES_REPOSITIONING")
        return EngineV1Decision(
            status="READY",
            recommendation=RECOMMENDATION_IMPROVE_EXPOSURE,
            eligibility=ELIGIBILITY_AMBER,
            confidence=confidence,
            market_position=position,
            gap_to_p75_pct=gap,
            gradual_price_uf=None,
            competitive_reference_uf=None,
            owner_action=OWNER_NO_ACTION,
            reasons=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    if strong_recent:
        reasons.append("STRONG_RECENT_DEMAND_OVERRIDES_AUTOMATIC_REDUCTION")
        return EngineV1Decision(
            status="READY",
            recommendation=RECOMMENDATION_OBSERVE,
            eligibility=ELIGIBILITY_AMBER,
            confidence=confidence,
            market_position=position,
            gap_to_p75_pct=gap,
            gradual_price_uf=None,
            competitive_reference_uf=None,
            owner_action=OWNER_NO_ACTION,
            reasons=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    if cooldown:
        reasons.append("PRICE_CHANGE_COOLDOWN_BLOCKS_NEW_REDUCTION")
        return EngineV1Decision(
            status="READY",
            recommendation=RECOMMENDATION_OBSERVE,
            eligibility=ELIGIBILITY_AMBER,
            confidence=confidence,
            market_position=position,
            gap_to_p75_pct=gap,
            gradual_price_uf=None,
            competitive_reference_uf=None,
            owner_action=OWNER_NO_ACTION,
            reasons=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    reasons.append("MARKET_REPOSITIONING_SUPPORTED_BY_MARKET_POSITION")
    eligibility_green = (
        operation_code == "VENTA"
        and count >= 20
        and quality == "HIGH"
        and not robustness_warning
        and 2 <= gap <= 12
        and exposure_state == "ADEQUATE"
        and not cooldown
        and not critical_conflict
    )
    eligibility = ELIGIBILITY_GREEN if eligibility_green else ELIGIBILITY_AMBER
    competitive_reference = float(p75) if p75 is not None else None
    mathematical_target = (current + competitive_reference) / 2 if current is not None and competitive_reference is not None else None
    gradual_target = (
        _safe_gradual_target(current, mathematical_target)
        if current is not None and mathematical_target is not None
        else None
    )
    if gap > 8:
        warnings.append("GAP_OUTSIDE_REQUESTABLE_BAND")
    if exposure_state == "PARTIAL":
        warnings.append("PARTIAL_EXPOSURE_REDUCES_ELIGIBILITY")
    if gap > 12:
        warnings.append("GAP_ABOVE_GREEN_BAND")

    requestable = (
        eligibility == ELIGIBILITY_GREEN
        and 2 <= gap <= 8
        and count >= 20
        and not robustness_warning
        and exposure_state == "ADEQUATE"
        and not cooldown
        and operation_code == "VENTA"
        and not critical_conflict
        and gradual_target is not None
    )
    if requestable:
        owner_action = OWNER_CAN_REQUEST_PRICE_CHANGE
        reasons.append("REQUEST_ELIGIBILITY_COMPLETE")
    else:
        owner_action = OWNER_DISCUSS_WITH_EXECUTIVE
        reasons.append("EXECUTIVE_REVIEW_REQUIRED_BEFORE_OWNER_REQUEST")

    return EngineV1Decision(
        status="READY",
        recommendation=RECOMMENDATION_MARKET_REPOSITIONING,
        eligibility=eligibility,
        confidence=confidence,
        market_position=position,
        gap_to_p75_pct=gap,
        gradual_price_uf=gradual_target,
        competitive_reference_uf=competitive_reference,
        owner_action=owner_action,
        reasons=tuple(dict.fromkeys(reasons)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


__all__ = [
    "COOLDOWN_DAYS",
    "ENGINE_V1_METHODOLOGY_VERSION",
    "EngineV1Decision",
    "evaluate_engine_v1",
]
