"""Pure shadow selector for global CRM SLA rescue.

This module contains no MongoDB, router, scheduler, worker, endpoint,
assignment-cycle mutation, or UI dependency. It receives analytical candidate
rows and simulates G0-G3 entirely in memory.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from math import ceil, floor, isfinite
from typing import Any, Iterable, Mapping


WINNER_CATEGORY = "SHADOW_WINNER_SELECTED"
NO_WINNER_CATEGORY = "NO_SHADOW_WINNER"
NO_VALID_DATA_CATEGORY = "NO_VALID_PERFORMANCE_DATA"
GUARDRAIL_CATEGORY = "ALL_CANDIDATES_GUARDED"

BASE_WEIGHTS = {
    "speed": 0.50,
    "sla": 0.25,
    "attention": 0.15,
    "capacity": 0.10,
}

WEIGHT_SCENARIOS = {
    "W0_GLOBAL_BASE": BASE_WEIGHTS,
    "W1_ULTRA_SPEED": {"speed": 0.65, "sla": 0.20, "attention": 0.10, "capacity": 0.05},
    "W2_SLA_SPEED": {"speed": 0.45, "sla": 0.35, "attention": 0.10, "capacity": 0.10},
    "W3_BALANCED": {"speed": 0.40, "sla": 0.30, "attention": 0.15, "capacity": 0.15},
}


@dataclass(frozen=True)
class RescueParameters:
    performance_window_days: int = 60
    shrinkage_k: float = 20.0
    minimum_speed_sample: int = 5
    tie_threshold: float = 1.0
    winsor_lower: float = 0.05
    winsor_upper: float = 0.95
    speed_p50_weight: float = 0.70
    speed_p90_weight: float = 0.30
    g1_expired_guardrail: int = 50
    g1_unmanaged_guardrail: int = 75
    g2_relative_multiplier: float = 2.0
    g2_relative_buffer: float = 10.0
    dynamic_penalty_per_assignment: float = 2.0
    dynamic_penalty_cap: float = 20.0


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _optional_number(value: Any) -> float | None:
    if value in (None, "", "N/A", "N/D"):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, float(value)))


def mean(values: Iterable[float | int | None]) -> float | None:
    parsed = [float(value) for value in values if value is not None and isfinite(float(value))]
    return sum(parsed) / len(parsed) if parsed else None


def quantile(values: Iterable[float | int | None], probability: float) -> float | None:
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
    """Shrink a personal rate toward the team rate using the approved K."""
    n = max(0.0, _number(sample_size))
    personal = clamp(_number(personal_rate), 0.0, 1.0)
    team = clamp(_number(team_rate), 0.0, 1.0)
    k_value = max(0.0, _number(k))
    if n + k_value == 0:
        return personal
    return (n * personal + k_value * team) / (n + k_value)


def winsorized_bounds(values: Iterable[float | int | None], lower: float = 0.05, upper: float = 0.95) -> tuple[float | None, float | None]:
    clean = [float(value) for value in values if value is not None and isfinite(float(value))]
    if not clean:
        return None, None
    return quantile(clean, lower), quantile(clean, upper)


def lower_is_better_score(
    value: float | None,
    values: Iterable[float | int | None],
    *,
    lower: float = 0.05,
    upper: float = 0.95,
) -> float | None:
    if value is None:
        return None
    low, high = winsorized_bounds(values, lower, upper)
    if low is None or high is None:
        return None
    if high == low:
        return 100.0
    clipped = max(low, min(high, float(value)))
    return clamp(100.0 * (high - clipped) / (high - low))


def load_pressure(candidate: Mapping[str, Any]) -> float:
    return (
        3 * _number(candidate.get("simulated_expired_current", candidate.get("expired_current_policy")))
        + 2 * _number(candidate.get("simulated_unmanaged_current", candidate.get("unmanaged_current_policy")))
        + _number(candidate.get("simulated_open_current", candidate.get("open_current_policy")))
    )


def capacity_score(candidates: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    source = [dict(candidate) for candidate in candidates]
    pressure_by_id = {str(candidate.get("user_id") or ""): load_pressure(candidate) for candidate in source}
    if not pressure_by_id:
        return {}
    lowest = min(pressure_by_id.values())
    highest = max(pressure_by_id.values())
    if lowest == highest:
        return {user_id: 100.0 for user_id in pressure_by_id}
    return {
        user_id: clamp(100.0 * (highest - pressure) / (highest - lowest))
        for user_id, pressure in pressure_by_id.items()
    }


def _reliable_speed(candidate: Mapping[str, Any], params: RescueParameters) -> bool:
    return (
        _number(candidate.get("sample_size")) >= params.minimum_speed_sample
        and _optional_number(candidate.get("p50_first_management_business_minutes")) is not None
        and _optional_number(candidate.get("p90_first_management_business_minutes")) is not None
    )


def impute_speed_metrics(
    candidates: Iterable[Mapping[str, Any]],
    *,
    team_p50_average: float | None,
    team_p90_average: float | None,
    params: RescueParameters = RescueParameters(),
) -> list[dict[str, Any]]:
    """Use team averages when a candidate has insufficient speed evidence."""
    source = [dict(candidate) for candidate in candidates]
    reliable = [candidate for candidate in source if _reliable_speed(candidate, params)]
    fallback_p50 = team_p50_average
    fallback_p90 = team_p90_average
    if fallback_p50 is None:
        fallback_p50 = mean(candidate.get("p50_first_management_business_minutes") for candidate in reliable)
    if fallback_p90 is None:
        fallback_p90 = mean(candidate.get("p90_first_management_business_minutes") for candidate in reliable)
    output = []
    for candidate in source:
        sample = _number(candidate.get("sample_size"))
        raw_p50 = _optional_number(candidate.get("p50_first_management_business_minutes"))
        raw_p90 = _optional_number(candidate.get("p90_first_management_business_minutes"))
        reliable_row = sample >= params.minimum_speed_sample and raw_p50 is not None and raw_p90 is not None
        candidate["p50_effective"] = raw_p50 if reliable_row else fallback_p50
        candidate["p90_effective"] = raw_p90 if reliable_row else fallback_p90
        candidate["speed_imputed"] = "no" if reliable_row else "yes"
        candidate["speed_imputation_source"] = "none" if reliable_row else "team_average"
        output.append(candidate)
    return output


def _candidate_is_valid(candidate: Mapping[str, Any]) -> tuple[bool, str]:
    reasons: list[str] = []
    if candidate.get("active") is not True:
        reasons.append("inactive_user")
    if str(candidate.get("role") or "").lower() != "agente":
        reasons.append("role_not_agent")
    if candidate.get("legacy"):
        reasons.append("legacy")
    if candidate.get("protected_by_management"):
        reasons.append("protected_by_management")
    if candidate.get("data_issue"):
        reasons.append("data_issue")
    if candidate.get("closed_lead"):
        reasons.append("closed_lead")
    if candidate.get("not_currently_expired"):
        reasons.append("not_currently_expired")
    return not reasons, ";".join(reasons)


def filter_global_candidates(lead: Mapping[str, Any], candidates: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply only global rescue exclusions; territory is intentionally absent."""
    owner_id = str(lead.get("owner_user_id") or "")
    valid: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for raw in candidates:
        candidate = dict(raw)
        reasons: list[str] = []
        if str(candidate.get("user_id") or "") == owner_id:
            reasons.append("previous_owner_excluded")
        candidate_valid, reason = _candidate_is_valid(candidate)
        if not candidate_valid and reason:
            reasons.extend(reason.split(";"))
        if reasons:
            candidate["excluded_reason"] = ";".join(dict.fromkeys(reasons))
            excluded.append(candidate)
        else:
            valid.append(candidate)
    return valid, excluded


def guardrail_reason(
    candidate: Mapping[str, Any],
    pool: Iterable[Mapping[str, Any]],
    scenario: str,
    *,
    params: RescueParameters = RescueParameters(),
) -> str | None:
    expired = _number(candidate.get("simulated_expired_current", candidate.get("expired_current_policy")))
    unmanaged = _number(candidate.get("simulated_unmanaged_current", candidate.get("unmanaged_current_policy")))
    if scenario in {"G1_GUARDRAIL_EXTREME", "G1_EXTREME_GUARDRAIL", "G3_BALANCE_DYNAMIC"}:
        reasons = []
        if expired >= params.g1_expired_guardrail:
            reasons.append(f"expired_current_policy>={params.g1_expired_guardrail}")
        if unmanaged >= params.g1_unmanaged_guardrail:
            reasons.append(f"unmanaged_current_policy>={params.g1_unmanaged_guardrail}")
        return ";".join(reasons) if reasons else None
    if scenario == "G2_RELATIVE_CAPACITY":
        pressures = [load_pressure(other) for other in pool]
        if pressures:
            minimum = min(pressures)
            allowed = minimum * params.g2_relative_multiplier + params.g2_relative_buffer
            if load_pressure(candidate) > allowed:
                return f"load_pressure>{minimum:g}*{params.g2_relative_multiplier:g}+{params.g2_relative_buffer:g}"
    return None


def _tie_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _number(candidate.get("p50_effective"), float("inf")),
        -_number(candidate.get("adjusted_sla_rate")),
        _number(candidate.get("simulated_expired_current", candidate.get("expired_current_policy"))),
        _number(candidate.get("simulated_unmanaged_current", candidate.get("unmanaged_current_policy"))),
        _number(candidate.get("shadow_received_count")),
        _number(candidate.get("p90_effective"), float("inf")),
        str(candidate.get("user_id") or ""),
    )


def _order_scored(candidates: Iterable[Mapping[str, Any]], score_field: str, tie_threshold: float) -> list[dict[str, Any]]:
    source = [dict(candidate) for candidate in candidates if candidate.get("performance_data_valid")]
    if not source:
        return []
    by_score = sorted(source, key=lambda candidate: (-_number(candidate.get(score_field)), str(candidate.get("user_id") or "")))
    if len(by_score) >= 2:
        difference = _number(by_score[0].get(score_field)) - _number(by_score[1].get(score_field))
        if difference < tie_threshold:
            leading_score = _number(by_score[0].get(score_field))
            tied = [candidate for candidate in source if leading_score - _number(candidate.get(score_field)) < tie_threshold]
            outside = [candidate for candidate in source if candidate not in tied]
            by_score = sorted(tied, key=_tie_key) + sorted(
                outside,
                key=lambda candidate: (-_number(candidate.get(score_field)), str(candidate.get("user_id") or "")),
            )
            by_score[0]["tie_break_applied"] = "yes"
        else:
            by_score[0]["tie_break_applied"] = "no"
    else:
        by_score[0]["tie_break_applied"] = "no"
    for rank, candidate in enumerate(by_score, start=1):
        candidate["rank"] = rank
    return by_score


def score_candidates(
    candidates: Iterable[Mapping[str, Any]],
    *,
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50_average: float | None,
    team_p90_average: float | None,
    weights: Mapping[str, float] = BASE_WEIGHTS,
    params: RescueParameters = RescueParameters(),
) -> list[dict[str, Any]]:
    prepared = impute_speed_metrics(
        candidates,
        team_p50_average=team_p50_average,
        team_p90_average=team_p90_average,
        params=params,
    )
    p50_values = [candidate.get("p50_effective") for candidate in prepared]
    p90_values = [candidate.get("p90_effective") for candidate in prepared]
    capacity = capacity_score(prepared)
    output = []
    for candidate in prepared:
        user_id = str(candidate.get("user_id") or "")
        sample = int(_number(candidate.get("sample_size")))
        adjusted_sla = shrink_rate(_number(candidate.get("sla_compliance_rate")), sample, team_sla_rate, params.shrinkage_k)
        adjusted_attention = shrink_rate(_number(candidate.get("attention_rate")), sample, team_attention_rate, params.shrinkage_k)
        p50_score = lower_is_better_score(candidate.get("p50_effective"), p50_values, lower=params.winsor_lower, upper=params.winsor_upper)
        p90_score = lower_is_better_score(candidate.get("p90_effective"), p90_values, lower=params.winsor_lower, upper=params.winsor_upper)
        speed = None if p50_score is None or p90_score is None else clamp(
            params.speed_p50_weight * p50_score + params.speed_p90_weight * p90_score
        )
        cap = capacity.get(user_id)
        candidate.update({
            "adjusted_sla_rate": adjusted_sla,
            "adjusted_attention_rate": adjusted_attention,
            "adjusted_sla_score": clamp(adjusted_sla * 100.0),
            "adjusted_attention_score": clamp(adjusted_attention * 100.0),
            "speed_score_p50": p50_score,
            "speed_score_p90": p90_score,
            "speed_score": speed,
            "load_pressure": load_pressure(candidate),
            "capacity_score": cap,
        })
        if speed is None or cap is None:
            candidate["performance_data_valid"] = False
            candidate["global_rescue_score"] = None
        else:
            candidate["performance_data_valid"] = True
            candidate["global_rescue_score"] = clamp(
                weights["speed"] * speed
                + weights["sla"] * candidate["adjusted_sla_score"]
                + weights["attention"] * candidate["adjusted_attention_score"]
                + weights["capacity"] * cap
            )
        output.append(candidate)
    return output


def select_winner(
    lead: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    *,
    scenario: str,
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50_average: float | None,
    team_p90_average: float | None,
    weights: Mapping[str, float] = BASE_WEIGHTS,
    params: RescueParameters = RescueParameters(),
) -> dict[str, Any]:
    hard_valid, hard_excluded = filter_global_candidates(lead, candidates)
    scored = score_candidates(
        hard_valid,
        team_sla_rate=team_sla_rate,
        team_attention_rate=team_attention_rate,
        team_p50_average=team_p50_average,
        team_p90_average=team_p90_average,
        weights=weights,
        params=params,
    )
    for candidate in scored:
        candidate["guardrail_exclusion_reason"] = guardrail_reason(candidate, scored, scenario, params=params)
        received = _number(candidate.get("shadow_received_count"))
        candidate["dynamic_penalty"] = clamp(min(received * params.dynamic_penalty_per_assignment, params.dynamic_penalty_cap)) if scenario == "G3_BALANCE_DYNAMIC" else 0.0
        candidate["dynamic_rescue_score"] = None if candidate.get("global_rescue_score") is None else clamp(candidate["global_rescue_score"] - candidate["dynamic_penalty"])
    available = [candidate for candidate in scored if not candidate.get("guardrail_exclusion_reason")]
    ordered = _order_scored(
        available,
        "dynamic_rescue_score" if scenario == "G3_BALANCE_DYNAMIC" else "global_rescue_score",
        params.tie_threshold,
    )
    result: dict[str, Any] = {
        "lead_id": str(lead.get("lead_id") or ""),
        "scenario": scenario,
        "hard_excluded": hard_excluded,
        "guardrail_excluded": [candidate for candidate in scored if candidate.get("guardrail_exclusion_reason")],
        "scored": scored,
        "ordered": ordered,
        "shadow_category": NO_WINNER_CATEGORY,
        "shadow_winner": None,
        "second_place": None,
        "score_difference": None,
    }
    if not hard_valid:
        result["shadow_category"] = NO_VALID_DATA_CATEGORY
        return result
    if not available:
        result["shadow_category"] = GUARDRAIL_CATEGORY
        return result
    if not ordered:
        result["shadow_category"] = NO_VALID_DATA_CATEGORY
        return result
    winner = ordered[0]
    score_field = "dynamic_rescue_score" if scenario == "G3_BALANCE_DYNAMIC" else "global_rescue_score"
    result["shadow_category"] = WINNER_CATEGORY
    result["shadow_winner"] = winner
    if len(ordered) > 1:
        result["second_place"] = ordered[1]
        result["score_difference"] = _number(winner.get(score_field)) - _number(ordered[1].get(score_field))
    return result


def lead_sort_key(lead: Mapping[str, Any]) -> tuple[Any, ...]:
    temperature = str(lead.get("temperature") or "NORMAL").upper()
    hot_first = 0 if temperature == "HOT" else 1
    overdue = -_number(lead.get("current_overdue_business_minutes", lead.get("overdue_business_minutes")))
    assigned = str(lead.get("assigned_at") or "")
    return hot_first, overdue, assigned, str(lead.get("lead_id") or "")


def simulate_scenario(
    leads: Iterable[Mapping[str, Any]],
    *,
    scenario: str,
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50_average: float | None,
    team_p90_average: float | None,
    weights: Mapping[str, float] = BASE_WEIGHTS,
    params: RescueParameters = RescueParameters(),
) -> dict[str, Any]:
    """Sequential, deterministic, memory-only rescue simulation."""
    ordered_leads = sorted((deepcopy(dict(lead)) for lead in leads), key=lead_sort_key)
    state: dict[str, dict[str, float]] = {}
    for lead in ordered_leads:
        for candidate in lead.get("candidates", []):
            user_id = str(candidate.get("user_id") or "")
            state.setdefault(user_id, {
                "simulated_open_current": _number(candidate.get("open_current_policy")),
                "simulated_unmanaged_current": _number(candidate.get("unmanaged_current_policy")),
                "simulated_expired_current": _number(candidate.get("expired_current_policy")),
                "shadow_received_count": _number(candidate.get("shadow_received_count")),
            })

    decisions: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    for step, lead in enumerate(ordered_leads, start=1):
        candidates = []
        for raw in lead.get("candidates", []):
            candidate = dict(raw)
            candidate.update(state.get(str(candidate.get("user_id") or ""), {}))
            candidates.append(candidate)
        decision = select_winner(
            lead,
            candidates,
            scenario=scenario,
            team_sla_rate=team_sla_rate,
            team_attention_rate=team_attention_rate,
            team_p50_average=team_p50_average,
            team_p90_average=team_p90_average,
            weights=weights,
            params=params,
        )
        winner = decision.get("shadow_winner")
        second = decision.get("second_place")
        score_field = "dynamic_rescue_score" if scenario == "G3_BALANCE_DYNAMIC" else "global_rescue_score"
        decision.update({
            "lead": lead,
            "step": step,
            "winner_user_id": str(winner.get("user_id")) if winner else "",
            "winner_name": winner.get("executive") if winner else "",
            "winner_score": winner.get(score_field) if winner else None,
            "winner_global_score": winner.get("global_rescue_score") if winner else None,
            "second_name": second.get("executive") if second else "",
            "second_score": second.get(score_field) if second else None,
            "score_field": score_field,
            "candidate_count": len(candidates),
            "hard_excluded_count": len(decision.get("hard_excluded", [])),
            "guardrail_excluded_count": len(decision.get("guardrail_excluded", [])),
        })
        decisions.append(decision)
        for candidate in decision.get("scored", []) + decision.get("hard_excluded", []):
            row = dict(candidate)
            row.update({
                "lead_id": lead.get("lead_id"),
                "scenario": scenario,
                "candidate_status": (
                    "GUARDRAIL_EXCLUDED" if candidate.get("guardrail_exclusion_reason")
                    else "HARD_EXCLUDED" if candidate.get("excluded_reason")
                    else "AVAILABLE"
                ),
            })
            candidate_rows.append(row)
        if winner:
            winner_id = str(winner.get("user_id") or "")
            state.setdefault(winner_id, {
                "simulated_open_current": _number(winner.get("open_current_policy")),
                "simulated_unmanaged_current": _number(winner.get("unmanaged_current_policy")),
                "simulated_expired_current": _number(winner.get("expired_current_policy")),
                "shadow_received_count": _number(winner.get("shadow_received_count")),
            })
            state[winner_id]["simulated_open_current"] += 1
            state[winner_id]["simulated_unmanaged_current"] += 1
            state[winner_id]["shadow_received_count"] += 1
            history.append({
                "step": step,
                "lead_id": lead.get("lead_id"),
                "winner_user_id": winner_id,
                "winner_name": winner.get("executive"),
                "global_rescue_score": winner.get("global_rescue_score"),
                "dynamic_penalty": winner.get("dynamic_penalty", 0.0),
                "dynamic_rescue_score": winner.get("dynamic_rescue_score"),
                "shadow_received_after": state[winner_id]["shadow_received_count"],
                "load_pressure_after": 3 * state[winner_id]["simulated_expired_current"] + 2 * state[winner_id]["simulated_unmanaged_current"] + state[winner_id]["simulated_open_current"],
            })

    return {
        "scenario": scenario,
        "decisions": decisions,
        "candidate_rows": candidate_rows,
        "final_state": state,
        "history": history,
        "ordered_lead_ids": [str(lead.get("lead_id")) for lead in ordered_leads],
    }


def concentration(decisions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    winners = [decision for decision in decisions if decision.get("shadow_category") == WINNER_CATEGORY]
    received = Counter(str(decision.get("winner_user_id")) for decision in winners)
    total = sum(received.values())
    ranked = sorted(received.items(), key=lambda item: (-item[1], item[0]))
    shares = {user_id: count / total for user_id, count in received.items()} if total else {}
    return {
        "received": dict(received),
        "total": total,
        "top1_share": ranked[0][1] / total if ranked and total else 0.0,
        "top2_share": sum(value for _, value in ranked[:2]) / total if total else 0.0,
        "top3_share": sum(value for _, value in ranked[:3]) / total if total else 0.0,
        "hhi": sum(share * share for share in shares.values()),
        "ranking": ranked,
    }


def maximum_consecutive(decisions: Iterable[Mapping[str, Any]]) -> int:
    maximum = 0
    current = 0
    previous = None
    for decision in decisions:
        winner = str(decision.get("winner_user_id") or "")
        if not winner:
            current = 0
            previous = None
            continue
        current = current + 1 if winner == previous else 1
        maximum = max(maximum, current)
        previous = winner
    return maximum
