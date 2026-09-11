"""Pure capacity scenarios for the CRM SLA shadow audit.

This module is intentionally independent from MongoDB, the production router,
assignment-cycle mutations, schedulers and UI. It receives the certified
Phase 1A candidate pools and only changes capacity assumptions in memory.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from chatbot.crm_sla_shadow_ranking import (
    BASE_WEIGHTS,
    NO_CAPACITY_CATEGORY,
    NO_DATA_CATEGORY,
    ShadowParameters,
    TIE_CATEGORY,
    WINNER_CATEGORY,
    _number,
    apply_candidate_scores,
    filter_hard_candidates,
    order_scored_candidates,
)


@dataclass(frozen=True)
class CapacityScenario:
    name: str
    max_expired_backlog: int | None = None
    max_unmanaged_backlog: int | None = None
    relative_load_multiplier: float | None = None
    relative_load_buffer: float = 5.0
    guardrail_expired_backlog: int | None = None
    guardrail_unmanaged_backlog: int | None = None


SCENARIOS = (
    CapacityScenario("C0_ACTUAL", max_expired_backlog=5, max_unmanaged_backlog=10),
    CapacityScenario("C1_CURRENT_POLICY", max_expired_backlog=5, max_unmanaged_backlog=10),
    CapacityScenario("C2_CURRENT_10_20", max_expired_backlog=10, max_unmanaged_backlog=20),
    CapacityScenario("C3_CURRENT_15_30", max_expired_backlog=15, max_unmanaged_backlog=30),
    CapacityScenario("C4_NO_HARD_BARRIER"),
    CapacityScenario("C5_RELATIVE_POOL", relative_load_multiplier=1.50, relative_load_buffer=5.0),
    CapacityScenario(
        "C6_RELATIVE_GUARDRAIL",
        relative_load_multiplier=1.50,
        relative_load_buffer=5.0,
        guardrail_expired_backlog=20,
        guardrail_unmanaged_backlog=40,
    ),
)


def capacity_pressure(candidate: Mapping[str, Any]) -> float:
    return (
        3 * _number(candidate.get("simulated_expired_backlog", candidate.get("expired_backlog")))
        + 2 * _number(candidate.get("simulated_unmanaged_backlog", candidate.get("unmanaged_backlog")))
        + _number(candidate.get("simulated_open_backlog", candidate.get("open_backlog")))
    )


def scenario_capacity_reason(
    candidate: Mapping[str, Any],
    pool: Iterable[Mapping[str, Any]],
    scenario: CapacityScenario,
) -> str | None:
    """Return the exact scenario exclusion reason, if any."""
    expired = _number(candidate.get("simulated_expired_backlog", candidate.get("expired_backlog")))
    unmanaged = _number(candidate.get("simulated_unmanaged_backlog", candidate.get("unmanaged_backlog")))
    reasons: list[str] = []
    if scenario.max_expired_backlog is not None and expired >= scenario.max_expired_backlog:
        reasons.append(f"expired_current_policy>={scenario.max_expired_backlog}")
    if scenario.max_unmanaged_backlog is not None and unmanaged >= scenario.max_unmanaged_backlog:
        reasons.append(f"unmanaged_current_policy>={scenario.max_unmanaged_backlog}")
    if scenario.relative_load_multiplier is not None:
        pressures = [capacity_pressure(other) for other in pool]
        if pressures:
            minimum = min(pressures)
            allowed = minimum * scenario.relative_load_multiplier + scenario.relative_load_buffer
            if capacity_pressure(candidate) > allowed:
                reasons.append(
                    f"relative_pressure>{minimum:.6g}*{scenario.relative_load_multiplier:g}+{scenario.relative_load_buffer:g}"
                )
    if scenario.guardrail_expired_backlog is not None and expired >= scenario.guardrail_expired_backlog:
        reasons.append(f"guardrail_expired_current_policy>={scenario.guardrail_expired_backlog}")
    if scenario.guardrail_unmanaged_backlog is not None and unmanaged >= scenario.guardrail_unmanaged_backlog:
        reasons.append(f"guardrail_unmanaged_current_policy>={scenario.guardrail_unmanaged_backlog}")
    return ";".join(reasons) if reasons else None


def select_capacity_winner(
    lead: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    *,
    scenario: CapacityScenario,
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
        candidate["capacity_scenario"] = scenario.name
        candidate["capacity_pressure"] = capacity_pressure(candidate)
        candidate["capacity_exclusion_reason"] = scenario_capacity_reason(candidate, scored, scenario)
    available = [candidate for candidate in scored if not candidate["capacity_exclusion_reason"]]
    excluded_by_capacity = [candidate for candidate in scored if candidate["capacity_exclusion_reason"]]
    ordered = order_scored_candidates(available, params.tie_threshold)
    result: dict[str, Any] = {
        "lead_id": str(lead.get("lead_id") or ""),
        "scenario": scenario.name,
        "initial_candidate_count": len(hard_valid),
        "after_capacity_count": len(available),
        "hard_excluded": hard_excluded,
        "capacity_excluded": excluded_by_capacity,
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
    winner = ordered[0]
    result["shadow_category"] = WINNER_CATEGORY
    result["shadow_winner"] = winner
    if len(ordered) > 1:
        result["second_place"] = ordered[1]
        result["score_difference"] = _number(winner.get("performance_score")) - _number(ordered[1].get("performance_score"))
    return result


def simulate_capacity_scenario(
    leads: Iterable[Mapping[str, Any]],
    *,
    scenario: CapacityScenario,
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50: float | None,
    team_p90: float | None,
    weights: Mapping[str, float] = BASE_WEIGHTS,
    params: ShadowParameters = ShadowParameters(),
    shrinkage: bool = True,
) -> dict[str, Any]:
    """Sequentially simulate one capacity scenario with memory-only state."""
    ordered_leads = sorted(
        (deepcopy(dict(lead)) for lead in leads),
        key=lambda lead: (
            0 if str(lead.get("temperature") or "NORMAL").upper() == "HOT" else 1,
            -_number(lead.get("overdue_business_minutes", lead.get("overdue_minutes"))),
            str(lead.get("assigned_at") or ""),
            str(lead.get("lead_id") or ""),
        ),
    )
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

    decisions: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    for lead in ordered_leads:
        candidates = []
        for raw in lead.get("candidates", []):
            candidate = dict(raw)
            candidate.update(state.get(str(candidate.get("user_id") or ""), {}))
            candidates.append(candidate)
        decision = select_capacity_winner(
            lead,
            candidates,
            scenario=scenario,
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
        for candidate in decision.get("scored", []) + decision.get("hard_excluded", []):
            row = dict(candidate)
            row.update({
                "lead_id": lead.get("lead_id"),
                "current_owner": lead.get("owner"),
                "scenario": scenario.name,
                "candidate_status": (
                    "CAPACITY_EXCLUDED" if candidate.get("capacity_exclusion_reason")
                    else "HARD_EXCLUDED" if candidate.get("excluded_reason")
                    else "AVAILABLE"
                ),
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
    return {
        "scenario": scenario.name,
        "decisions": decisions,
        "candidate_rows": candidate_rows,
        "final_state": state,
        "ordered_lead_ids": [str(lead.get("lead_id")) for lead in ordered_leads],
    }
