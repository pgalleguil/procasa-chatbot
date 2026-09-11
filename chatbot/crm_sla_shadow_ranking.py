"""Pure/read-only shadow ranking for CRM SLA reassignment.

This module deliberately has no MongoDB, scheduler, notification, assignment,
or frontend dependency. It receives already-certified territorial candidates
and computes a deterministic analytical recommendation only.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from math import ceil, floor, isfinite
from typing import Any, Iterable, Mapping


SAFE_CATEGORY = "SAFE_TO_SHADOW_REASSIGN"
NO_CAPACITY_CATEGORY = "NO_CANDIDATE_AFTER_CAPACITY_FILTER"
NO_DATA_CATEGORY = "NO_VALID_PERFORMANCE_DATA"
TIE_CATEGORY = "TIE_UNRESOLVED"
WINNER_CATEGORY = "SHADOW_WINNER_SELECTED"


@dataclass(frozen=True)
class ShadowParameters:
    performance_window_days: int = 60
    shrinkage_k: float = 20.0
    max_expired_backlog: int = 5
    max_unmanaged_backlog: int = 10
    tie_threshold: float = 1.0
    winsor_lower: float = 0.05
    winsor_upper: float = 0.95


BASE_WEIGHTS = {"sla": 0.40, "attention": 0.20, "speed": 0.20, "capacity": 0.20}
SENSITIVITY_WEIGHTS = {
    "BASE": BASE_WEIGHTS,
    "S1_SPEED": {"sla": 0.30, "attention": 0.15, "speed": 0.35, "capacity": 0.20},
    "S2_SLA": {"sla": 0.55, "attention": 0.15, "speed": 0.15, "capacity": 0.15},
    "S3_CAPACITY": {"sla": 0.30, "attention": 0.15, "speed": 0.15, "capacity": 0.40},
    "S4_NO_SHRINKAGE": BASE_WEIGHTS,
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _optional_number(value: Any) -> float | None:
    if value in (None, "", "N/D", "N/A"):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, float(value)))


def mean(values: Iterable[float | int | None]) -> float | None:
    parsed = [float(value) for value in values if value is not None and isfinite(float(value))]
    return sum(parsed) / len(parsed) if parsed else None


def quantile(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(float(value) for value in values if value is not None and isfinite(float(value)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = floor(position)
    upper = ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def shrink_rate(personal_rate: float, sample_size: int, team_rate: float, k: float = 20.0) -> float:
    """Return the configured empirical-Bayes-style shrinkage rate in 0..1."""
    n = max(0.0, _number(sample_size))
    personal = _clamp(_number(personal_rate), 0.0, 1.0)
    team = _clamp(_number(team_rate), 0.0, 1.0)
    k_value = max(0.0, _number(k))
    if n + k_value == 0:
        return personal
    return (n * personal + k_value * team) / (n + k_value)


def winsorized_bounds(values: Iterable[float], lower: float = 0.05, upper: float = 0.95) -> tuple[float | None, float | None]:
    clean_values = [float(value) for value in values if value is not None and isfinite(float(value))]
    if not clean_values:
        return None, None
    return quantile(clean_values, lower), quantile(clean_values, upper)


def monotonic_lower_is_better_score(value: float | None, values: Iterable[float], *, lower: float = 0.05, upper: float = 0.95) -> float | None:
    """Map lower-is-better values to 0..100 after P5/P95 winsorization."""
    if value is None:
        return None
    low, high = winsorized_bounds(values, lower, upper)
    if low is None or high is None:
        return None
    clipped = max(low, min(high, float(value)))
    if high == low:
        return 100.0
    return _clamp(100.0 * (high - clipped) / (high - low))


def _reliable_speed(candidate: Mapping[str, Any]) -> bool:
    return (
        _number(candidate.get("sample_size")) >= 5
        and _optional_number(candidate.get("p50_first_management_business_minutes")) is not None
        and _optional_number(candidate.get("p90_first_management_business_minutes")) is not None
    )


def impute_speed_metrics(
    candidates: Iterable[Mapping[str, Any]],
    *,
    team_p50: float | None = None,
    team_p90: float | None = None,
) -> list[dict[str, Any]]:
    """Impute unreliable speed metrics from reliable pool values, then team values."""
    source = [dict(candidate) for candidate in candidates]
    reliable = [candidate for candidate in source if _reliable_speed(candidate)]
    pool_p50 = mean(_optional_number(candidate.get("p50_first_management_business_minutes")) for candidate in reliable)
    pool_p90 = mean(_optional_number(candidate.get("p90_first_management_business_minutes")) for candidate in reliable)
    fallback_p50 = pool_p50 if pool_p50 is not None else team_p50
    fallback_p90 = pool_p90 if pool_p90 is not None else team_p90
    output = []
    for candidate in source:
        sample = _number(candidate.get("sample_size"))
        raw_p50 = _optional_number(candidate.get("p50_first_management_business_minutes"))
        raw_p90 = _optional_number(candidate.get("p90_first_management_business_minutes"))
        imputed = sample < 5 or raw_p50 is None or raw_p90 is None
        candidate["p50_effective"] = raw_p50 if raw_p50 is not None and sample >= 5 else fallback_p50
        candidate["p90_effective"] = raw_p90 if raw_p90 is not None and sample >= 5 else fallback_p90
        candidate["metric_imputed"] = "yes" if imputed else "no"
        candidate["speed_imputation_source"] = (
            "territorial_pool" if pool_p50 is not None and pool_p90 is not None and imputed
            else "team" if imputed else "none"
        )
        output.append(candidate)
    return output


def capacity_score(candidates: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Score lower load pressure higher, using the exact configured formula."""
    source = [dict(candidate) for candidate in candidates]
    pressures = {
        str(candidate.get("user_id")): (
            3 * _number(candidate.get("simulated_expired_backlog", candidate.get("expired_backlog")))
            + 2 * _number(candidate.get("simulated_unmanaged_backlog", candidate.get("unmanaged_backlog")))
            + _number(candidate.get("simulated_open_backlog", candidate.get("open_backlog")))
        )
        for candidate in source
    }
    if not pressures:
        return {}
    low = min(pressures.values())
    high = max(pressures.values())
    if low == high:
        return {key: 100.0 for key in pressures}
    return {key: _clamp(100.0 * (high - value) / (high - low)) for key, value in pressures.items()}


def apply_candidate_scores(
    candidates: Iterable[Mapping[str, Any]],
    *,
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50: float | None,
    team_p90: float | None,
    weights: Mapping[str, float] = BASE_WEIGHTS,
    params: ShadowParameters = ShadowParameters(),
    shrinkage: bool = True,
) -> list[dict[str, Any]]:
    """Calculate all score components for a single lead's candidate pool."""
    prepared = impute_speed_metrics(candidates, team_p50=team_p50, team_p90=team_p90)
    speeds_p50 = [candidate.get("p50_effective") for candidate in prepared]
    speeds_p90 = [candidate.get("p90_effective") for candidate in prepared]
    capacity = capacity_score(prepared)
    output = []
    for candidate in prepared:
        user_id = str(candidate.get("user_id") or "")
        sample = int(_number(candidate.get("sample_size")))
        raw_sla = _number(candidate.get("sla_compliance_rate"))
        raw_attention = _number(candidate.get("attention_rate"))
        adjusted_sla = shrink_rate(raw_sla, sample, team_sla_rate, params.shrinkage_k) if shrinkage else raw_sla
        adjusted_attention = shrink_rate(raw_attention, sample, team_attention_rate, params.shrinkage_k) if shrinkage else raw_attention
        p50_score = monotonic_lower_is_better_score(candidate.get("p50_effective"), speeds_p50, lower=params.winsor_lower, upper=params.winsor_upper)
        p90_score = monotonic_lower_is_better_score(candidate.get("p90_effective"), speeds_p90, lower=params.winsor_lower, upper=params.winsor_upper)
        speed = None if p50_score is None or p90_score is None else 0.60 * p50_score + 0.40 * p90_score
        cap = capacity.get(user_id)
        candidate.update({
            "adjusted_sla_compliance": adjusted_sla,
            "adjusted_attention_rate": adjusted_attention,
            "adjusted_sla_score": _clamp(adjusted_sla * 100),
            "adjusted_attention_score": _clamp(adjusted_attention * 100),
            "speed_score_p50": p50_score,
            "speed_score_p90": p90_score,
            "speed_score": speed,
            "load_pressure": 3 * _number(candidate.get("simulated_expired_backlog", candidate.get("expired_backlog"))) + 2 * _number(candidate.get("simulated_unmanaged_backlog", candidate.get("unmanaged_backlog"))) + _number(candidate.get("simulated_open_backlog", candidate.get("open_backlog"))),
            "capacity_score": cap,
        })
        if speed is None or cap is None:
            candidate["performance_data_valid"] = False
            candidate["performance_score"] = None
        else:
            candidate["performance_data_valid"] = True
            candidate["performance_score"] = _clamp(
                weights["sla"] * candidate["adjusted_sla_score"]
                + weights["attention"] * candidate["adjusted_attention_score"]
                + weights["speed"] * speed
                + weights["capacity"] * cap
            )
        output.append(candidate)
    return output


def saturation_reason(candidate: Mapping[str, Any], params: ShadowParameters = ShadowParameters()) -> str | None:
    expired = _number(candidate.get("simulated_expired_backlog", candidate.get("expired_backlog")))
    unmanaged = _number(candidate.get("simulated_unmanaged_backlog", candidate.get("unmanaged_backlog")))
    reasons = []
    if expired >= params.max_expired_backlog:
        reasons.append("expired_backlog>=max_expired_backlog")
    if unmanaged >= params.max_unmanaged_backlog:
        reasons.append("unmanaged_backlog>=max_unmanaged_backlog")
    return ";".join(reasons) if reasons else None


def filter_hard_candidates(lead: Mapping[str, Any], candidates: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply only the hard candidate constraints already certified by Phase 0.5."""
    owner_id = str(lead.get("owner_user_id") or "")
    valid, excluded = [], []
    for raw in candidates:
        candidate = dict(raw)
        reasons = []
        if not candidate.get("pool_certified", True):
            reasons.append("not_in_phase05_pool")
        if not candidate.get("active", True):
            reasons.append("inactive_user")
        if str(candidate.get("role", "agente")) != "agente":
            reasons.append("role_not_agent")
        if str(candidate.get("user_id") or "") == owner_id or candidate.get("executive") == lead.get("owner"):
            reasons.append("current_owner")
        if not candidate.get("territory_valid", True):
            reasons.append("territory_invalid")
        if candidate.get("cycle_conflict"):
            reasons.append("cycle_conflict")
        if candidate.get("data_issue"):
            reasons.append("data_issue")
        if candidate.get("protected_by_management"):
            reasons.append("protected_by_management")
        if reasons:
            candidate["excluded_reason"] = ";".join(reasons)
            excluded.append(candidate)
        else:
            valid.append(candidate)
    return valid, excluded


def _tie_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _number(candidate.get("simulated_expired_backlog", candidate.get("expired_backlog"))),
        _number(candidate.get("simulated_unmanaged_backlog", candidate.get("unmanaged_backlog"))),
        -_number(candidate.get("adjusted_sla_compliance")),
        _number(candidate.get("p50_effective"), float("inf")),
        _number(candidate.get("simulated_shadow_received")),
        str(candidate.get("user_id") or ""),
    )


def order_scored_candidates(scored: Iterable[Mapping[str, Any]], tie_threshold: float = 1.0) -> list[dict[str, Any]]:
    source = [dict(candidate) for candidate in scored if candidate.get("performance_data_valid")]
    if not source:
        return []
    # Primary order is score. When the leading score gap is under the
    # configured threshold, apply the exact business tie-break sequence.
    by_score = sorted(source, key=lambda candidate: (-_number(candidate.get("performance_score")), str(candidate.get("user_id") or "")))
    if len(by_score) >= 2 and (_number(by_score[0].get("performance_score")) - _number(by_score[1].get("performance_score"))) < tie_threshold:
        leading_score = _number(by_score[0].get("performance_score"))
        tied = [candidate for candidate in source if leading_score - _number(candidate.get("performance_score")) < tie_threshold]
        outside_tie = [candidate for candidate in source if candidate not in tied]
        by_score = sorted(tied, key=_tie_key) + sorted(
            outside_tie,
            key=lambda candidate: (-_number(candidate.get("performance_score")), str(candidate.get("user_id") or "")),
        )
        by_score[0]["tie_break_applied"] = "yes"
    else:
        by_score[0]["tie_break_applied"] = "no"
    for rank, candidate in enumerate(by_score, start=1):
        candidate["rank"] = rank
    return by_score


def _tie_unresolved(candidates: list[Mapping[str, Any]], tie_threshold: float) -> bool:
    """Return true when the configured tie-break chain cannot be completed."""
    if len(candidates) < 2:
        return False
    ordered = sorted(candidates, key=lambda candidate: (-_number(candidate.get("performance_score")), str(candidate.get("user_id") or "")))
    if (_number(ordered[0].get("performance_score")) - _number(ordered[1].get("performance_score"))) >= tie_threshold:
        return False
    first_key = _tie_key(ordered[0])[:-1]
    second_key = _tie_key(ordered[1])[:-1]
    return first_key == second_key and not str(ordered[0].get("user_id") or "") and not str(ordered[1].get("user_id") or "")


def select_shadow_winner(
    lead: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    *,
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50: float | None,
    team_p90: float | None,
    weights: Mapping[str, float] = BASE_WEIGHTS,
    params: ShadowParameters = ShadowParameters(),
    shrinkage: bool = True,
) -> dict[str, Any]:
    hard_valid, hard_excluded = filter_hard_candidates(lead, candidates)
    scored = apply_candidate_scores(
        hard_valid,
        team_sla_rate=team_sla_rate,
        team_attention_rate=team_attention_rate,
        team_p50=team_p50,
        team_p90=team_p90,
        weights=weights,
        params=params,
        shrinkage=shrinkage,
    )
    for candidate in scored:
        candidate["saturation_reason"] = saturation_reason(candidate, params)
    available = [candidate for candidate in scored if not candidate["saturation_reason"]]
    saturated = [candidate for candidate in scored if candidate["saturation_reason"]]
    ordered = order_scored_candidates(available, params.tie_threshold)
    result: dict[str, Any] = {
        "lead_id": str(lead.get("lead_id") or ""),
        "initial_candidate_count": len(hard_valid),
        "after_capacity_count": len(available),
        "hard_excluded": hard_excluded,
        "saturated": saturated,
        "scored": scored,
        "ordered": ordered,
        "shadow_category": NO_DATA_CATEGORY,
        "shadow_winner": None,
        "second_place": None,
        "score_difference": None,
    }
    if not hard_valid:
        result["shadow_category"] = NO_DATA_CATEGORY
        return result
    if not available:
        result["shadow_category"] = NO_CAPACITY_CATEGORY
        return result
    if not ordered:
        result["shadow_category"] = NO_DATA_CATEGORY
        return result
    if _tie_unresolved(ordered, params.tie_threshold):
        result["shadow_category"] = TIE_CATEGORY
        return result
    winner = ordered[0]
    result["shadow_category"] = WINNER_CATEGORY
    result["shadow_winner"] = winner
    if len(ordered) > 1:
        result["second_place"] = ordered[1]
        result["score_difference"] = _number(winner.get("performance_score")) - _number(ordered[1].get("performance_score"))
    return result


def _lead_sort_key(lead: Mapping[str, Any]) -> tuple[Any, ...]:
    temperature = str(lead.get("temperature") or "NORMAL").upper()
    hot_first = 0 if temperature == "HOT" else 1
    overdue = -_number(lead.get("overdue_business_minutes", lead.get("overdue_minutes")))
    assigned = str(lead.get("assigned_at") or "")
    return hot_first, overdue, assigned, str(lead.get("lead_id") or "")


def simulate_sequential(
    leads: Iterable[Mapping[str, Any]],
    *,
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50: float | None,
    team_p90: float | None,
    weights: Mapping[str, float] = BASE_WEIGHTS,
    params: ShadowParameters = ShadowParameters(),
    shrinkage: bool = True,
) -> dict[str, Any]:
    """Run deterministic sequential shadow allocation using memory-only load."""
    ordered_leads = sorted((deepcopy(dict(lead)) for lead in leads), key=_lead_sort_key)
    state: dict[str, dict[str, float]] = {}
    for lead in ordered_leads:
        for candidate in lead.get("candidates", []):
            user_id = str(candidate.get("user_id") or "")
            state.setdefault(user_id, {
                "simulated_open_backlog": _number(candidate.get("open_backlog")),
                "simulated_unmanaged_backlog": _number(candidate.get("unmanaged_backlog")),
                "simulated_expired_backlog": _number(candidate.get("expired_backlog")),
                "simulated_shadow_received": _number(candidate.get("simulated_shadow_received")),
            })

    decisions = []
    candidate_rows = []
    for lead in ordered_leads:
        candidates = []
        for raw in lead.get("candidates", []):
            candidate = dict(raw)
            candidate.update(state.get(str(candidate.get("user_id") or ""), {}))
            candidates.append(candidate)
        decision = select_shadow_winner(
            lead,
            candidates,
            team_sla_rate=team_sla_rate,
            team_attention_rate=team_attention_rate,
            team_p50=team_p50,
            team_p90=team_p90,
            weights=weights,
            params=params,
            shrinkage=shrinkage,
        )
        winner = decision.get("shadow_winner")
        second = decision.get("second_place")
        decision["lead"] = lead
        decision["winner_user_id"] = str(winner.get("user_id")) if winner else None
        decision["winner_name"] = winner.get("executive") if winner else None
        decision["winner_score"] = winner.get("performance_score") if winner else None
        decision["second_name"] = second.get("executive") if second else None
        decision["second_score"] = second.get("performance_score") if second else None
        decisions.append(decision)
        for candidate in decision.get("scored", []):
            row = dict(candidate)
            row.update({
                "lead_id": lead.get("lead_id"),
                "current_owner": lead.get("owner"),
                "candidate_status": "SATURATED" if candidate.get("saturation_reason") else "AVAILABLE",
            })
            candidate_rows.append(row)
        if winner:
            winner_id = str(winner.get("user_id") or "")
            state.setdefault(winner_id, {
                "simulated_open_backlog": _number(winner.get("open_backlog")),
                "simulated_unmanaged_backlog": _number(winner.get("unmanaged_backlog")),
                "simulated_expired_backlog": _number(winner.get("expired_backlog")),
                "simulated_shadow_received": _number(winner.get("simulated_shadow_received")),
            })
            state[winner_id]["simulated_open_backlog"] += 1
            state[winner_id]["simulated_unmanaged_backlog"] += 1
            state[winner_id]["simulated_shadow_received"] += 1

    return {"decisions": decisions, "candidate_rows": candidate_rows, "final_state": state, "ordered_lead_ids": [str(lead.get("lead_id")) for lead in ordered_leads]}


def concentration(decisions: Iterable[Mapping[str, Any]], *, user_ids: Iterable[str] = ()) -> dict[str, Any]:
    winner_rows = [decision for decision in decisions if decision.get("shadow_category") == WINNER_CATEGORY]
    received = Counter(str(decision.get("winner_user_id")) for decision in winner_rows)
    total = sum(received.values())
    shares = {user_id: count / total for user_id, count in received.items()} if total else {}
    ranking = sorted(received.items(), key=lambda item: (-item[1], item[0]))
    return {
        "received": dict(received),
        "total": total,
        "shares": shares,
        "top1_share": ranking[0][1] / total if ranking and total else 0.0,
        "top2_share": sum(value for _, value in ranking[:2]) / total if total else 0.0,
        "top3_share": sum(value for _, value in ranking[:3]) / total if total else 0.0,
        "hhi": sum(share * share for share in shares.values()),
        "ranking": ranking,
        "all_user_ids": list(user_ids),
    }


def sensitivity_simulations(
    leads: Iterable[Mapping[str, Any]],
    *,
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50: float | None,
    team_p90: float | None,
    params: ShadowParameters = ShadowParameters(),
) -> dict[str, dict[str, Any]]:
    results = {}
    for name, weights in SENSITIVITY_WEIGHTS.items():
        results[name] = simulate_sequential(
            leads,
            team_sla_rate=team_sla_rate,
            team_attention_rate=team_attention_rate,
            team_p50=team_p50,
            team_p90=team_p90,
            weights=weights,
            params=params,
            shrinkage=name != "S4_NO_SHRINKAGE",
        )
    return results


def changed_winners(sensitivity: Mapping[str, Mapping[str, Any]], base_name: str = "BASE") -> dict[str, list[str]]:
    base = {str(row["lead_id"]): row.get("winner_user_id") for row in sensitivity[base_name]["decisions"]}
    changes = {}
    for name, result in sensitivity.items():
        if name == base_name:
            continue
        current = {str(row["lead_id"]): row.get("winner_user_id") for row in result["decisions"]}
        changes[name] = sorted(lead_id for lead_id, winner in base.items() if current.get(lead_id) != winner)
    return changes
