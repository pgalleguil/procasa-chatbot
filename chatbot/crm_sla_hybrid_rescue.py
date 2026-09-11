"""Pure shadow policy engine for the Fase 1E hybrid SLA rescue.

The module receives analytical rows and never touches MongoDB, CRM owners,
assignment cycles, routers, workers, schedulers, endpoints or UI.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any, Iterable, Mapping
import re
import unicodedata

from chatbot.crm_sla_global_rescue import (
    BASE_WEIGHTS,
    WINNER_CATEGORY,
    _number,
    _order_scored,
    filter_global_candidates,
    score_candidates,
)


RM_GLOBAL_RESCUE = "RM_GLOBAL_RESCUE"
REGION_JPC_MARIA_HERNAN = "REGION_JPC_MARIA_HERNAN"
REGIONAL_POLICY_NOT_DEFINED = "REGIONAL_POLICY_NOT_DEFINED"
REGION_REVIEW_REQUIRED = "REGION_REVIEW_REQUIRED"
PROPERTY_EXECUTIVE_UNRESOLVED = "PROPERTY_EXECUTIVE_UNRESOLVED"
NO_ELIGIBLE_JPC_RESCUER = "NO_ELIGIBLE_JPC_RESCUER"
NOT_SIMULATED_POLICY = "NOT_SIMULATED_POLICY"
CONFIDENCE_GUARD_CATEGORY = "CONFIDENCE_GUARD_APPLIED"

REGION_METROPOLITANA = "metropolitanasantiago"
JPC_NAME = "jorge pablo caro"
MARIA_NAME = "maria paz galleguillos"
HERNAN_NAME = "hernan castro"


def _identity_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text.strip().lower())


def performance_confidence(sample_size: Any) -> str:
    n = _number(sample_size)
    if n >= 30:
        return "HIGH"
    if n >= 10:
        return "MEDIUM"
    return "LOW"


def classify_policy(
    canonical_region: str | None,
    *,
    region_resolved: bool,
    property_executive_status: str,
    property_is_jpc: bool,
) -> str:
    """Apply the exact A/B/C precedence supplied for Fase 1E."""
    if not region_resolved or not canonical_region:
        return REGION_REVIEW_REQUIRED
    if canonical_region == REGION_METROPOLITANA:
        return RM_GLOBAL_RESCUE
    if property_executive_status != "RESOLVED":
        return PROPERTY_EXECUTIVE_UNRESOLVED
    if property_is_jpc:
        return REGION_JPC_MARIA_HERNAN
    return REGIONAL_POLICY_NOT_DEFINED


def hybrid_pool(
    policy_category: str,
    candidates: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return the allowed pool before previous-owner exclusion."""
    source = [dict(candidate) for candidate in candidates]
    if policy_category == RM_GLOBAL_RESCUE:
        return source
    if policy_category == REGION_JPC_MARIA_HERNAN:
        return [
            candidate for candidate in source
            if _identity_key(candidate.get("identity_key")) in {MARIA_NAME, HERNAN_NAME}
            or _identity_key(candidate.get("executive_key")) in {MARIA_NAME, HERNAN_NAME}
        ]
    return []


def _tie_order(candidates: Iterable[Mapping[str, Any]], score_field: str, tie_threshold: float) -> list[dict[str, Any]]:
    return _order_scored(candidates, score_field, tie_threshold)


def select_hybrid_winner(
    lead: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    *,
    policy_category: str,
    scenario: str,
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50_average: float | None,
    team_p90_average: float | None,
    weights: Mapping[str, float] = BASE_WEIGHTS,
    params: Any,
    confidence_guard: bool = False,
) -> dict[str, Any]:
    """Score one already-classified lead under a hybrid policy category."""
    if policy_category not in {RM_GLOBAL_RESCUE, REGION_JPC_MARIA_HERNAN}:
        return {
            "policy_category": policy_category,
            "shadow_category": NOT_SIMULATED_POLICY,
            "shadow_winner": None,
            "ordered": [],
            "scored": [],
            "hard_excluded": [],
            "guardrail_excluded": [],
            "score_difference": None,
        }
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
        sample = _number(candidate.get("sample_size"))
        candidate["performance_confidence"] = performance_confidence(sample)
        candidate["non_speed_score"] = (
            weights["sla"] * _number(candidate.get("adjusted_sla_score"))
            + weights["attention"] * _number(candidate.get("adjusted_attention_score"))
            + weights["capacity"] * _number(candidate.get("capacity_score"))
        )
        received = _number(candidate.get("shadow_received_count"))
        candidate["dynamic_penalty"] = min(received * params.dynamic_penalty_per_assignment, params.dynamic_penalty_cap) if scenario == "H1_COMBINED_DYNAMIC" or scenario == "H0_INDEPENDENT_DYNAMIC" or scenario == "CONFIDENCE_GUARD" else 0.0
        candidate["dynamic_rescue_score"] = (
            None if candidate.get("global_rescue_score") is None
            else max(0.0, candidate["global_rescue_score"] - candidate["dynamic_penalty"])
        )
        candidate["confidence_guard_exclusion_reason"] = ""
    if not hard_valid:
        return {
            "policy_category": policy_category,
            "shadow_category": NO_ELIGIBLE_JPC_RESCUER if policy_category == REGION_JPC_MARIA_HERNAN else "NO_ELIGIBLE_RM_RESCUER",
            "shadow_winner": None,
            "ordered": [],
            "scored": scored,
            "hard_excluded": hard_excluded,
            "guardrail_excluded": [],
            "score_difference": None,
        }
    score_field = "dynamic_rescue_score" if scenario in {"H1_COMBINED_DYNAMIC", "H0_INDEPENDENT_DYNAMIC", "CONFIDENCE_GUARD"} else "global_rescue_score"
    valid = [candidate for candidate in scored if candidate.get("performance_data_valid")]
    ordered = _tie_order(valid, score_field, params.tie_threshold)
    confidence_applied = False
    if confidence_guard and ordered:
        winner = ordered[0]
        robust = [candidate for candidate in ordered if candidate.get("performance_confidence") in {"HIGH", "MEDIUM"}]
        if winner.get("performance_confidence") == "LOW" and robust:
            robust_best = max(robust, key=lambda candidate: (_number(candidate.get(score_field)), str(candidate.get("user_id") or "")))
            # The guard is limited to cases where the LOW winner's advantage
            # is caused by speed: its non-speed contribution is lower than the
            # strongest HIGH/MEDIUM alternative. No new numerical threshold is
            # introduced.
            if _number(winner.get("non_speed_score")) < _number(robust_best.get("non_speed_score")):
                winner["confidence_guard_exclusion_reason"] = "LOW_SPEED_ADVANTAGE_OVER_ROBUST"
                ordered = [robust_best] + [candidate for candidate in ordered if candidate is not robust_best]
                confidence_applied = True
    for rank, candidate in enumerate(ordered, start=1):
        candidate["rank"] = rank
    winner = ordered[0] if ordered else None
    second = ordered[1] if len(ordered) > 1 else None
    result = {
        "policy_category": policy_category,
        "shadow_category": WINNER_CATEGORY if winner else "NO_VALID_PERFORMANCE_DATA",
        "shadow_winner": winner,
        "second_place": second,
        "ordered": ordered,
        "scored": scored,
        "hard_excluded": hard_excluded,
        "guardrail_excluded": [],
        "confidence_guard_applied": "yes" if confidence_applied else "no",
        "score_difference": (_number(winner.get(score_field)) - _number(second.get(score_field))) if winner and second else None,
        "score_field": score_field,
    }
    if confidence_applied:
        result["shadow_category"] = CONFIDENCE_GUARD_CATEGORY
        result["winner_category_before_confidence_guard"] = WINNER_CATEGORY
    return result


def lead_sort_key(lead: Mapping[str, Any]) -> tuple[Any, ...]:
    hot_first = 0 if str(lead.get("temperature") or "NORMAL").upper() == "HOT" else 1
    overdue = -_number(lead.get("current_overdue_business_minutes"))
    return hot_first, overdue, str(lead.get("assigned_at") or ""), str(lead.get("lead_id") or "")


def _initial_state(leads: Iterable[Mapping[str, Any]], pool_key: str) -> dict[str, dict[str, float]]:
    state: dict[str, dict[str, float]] = {}
    for lead in leads:
        for candidate in lead.get(pool_key, []):
            user_id = str(candidate.get("user_id") or "")
            state.setdefault(user_id, {
                "simulated_open_current": _number(candidate.get("open_current_policy")),
                "simulated_unmanaged_current": _number(candidate.get("unmanaged_current_policy")),
                "simulated_expired_current": _number(candidate.get("expired_current_policy")),
                "shadow_received_count": _number(candidate.get("shadow_received_count")),
            })
    return state


def _simulate_queue(
    leads: list[Mapping[str, Any]],
    *,
    pool_key: str,
    queue: str,
    scenario: str,
    state: dict[str, dict[str, float]],
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50_average: float | None,
    team_p90_average: float | None,
    weights: Mapping[str, float],
    params: Any,
    confidence_guard: bool = False,
) -> dict[str, Any]:
    decisions = []
    history = []
    candidate_rows = []
    for step, raw_lead in enumerate(sorted((deepcopy(dict(lead)) for lead in leads), key=lead_sort_key), start=1):
        lead = raw_lead
        policy = str(lead.get("policy_category") or "")
        candidates = []
        for raw in lead.get(pool_key, []):
            candidate = dict(raw)
            candidate.update(state.get(str(candidate.get("user_id") or ""), {}))
            candidates.append(candidate)
        decision = select_hybrid_winner(
            lead,
            candidates,
            policy_category=policy,
            scenario=scenario,
            team_sla_rate=team_sla_rate,
            team_attention_rate=team_attention_rate,
            team_p50_average=team_p50_average,
            team_p90_average=team_p90_average,
            weights=weights,
            params=params,
            confidence_guard=confidence_guard,
        )
        winner = decision.get("shadow_winner")
        second = decision.get("second_place")
        decision.update({
            "lead": lead,
            "queue": queue,
            "step": step,
            "winner_user_id": str(winner.get("user_id")) if winner else "",
            "winner_name": winner.get("executive") if winner else "",
            "winner_score": winner.get(decision.get("score_field") or "dynamic_rescue_score") if winner else None,
            "winner_global_score": winner.get("global_rescue_score") if winner else None,
            "second_name": second.get("executive") if second else "",
            "second_score": second.get(decision.get("score_field") or "dynamic_rescue_score") if second else None,
            "candidate_count": len(candidates),
            "hard_excluded_count": len(decision.get("hard_excluded", [])),
        })
        decisions.append(decision)
        for candidate in decision.get("scored", []) + decision.get("hard_excluded", []):
            row = dict(candidate)
            row.update({"lead_id": lead.get("lead_id"), "assignment_cycle_id": lead.get("assignment_cycle_id"), "policy_category": policy, "queue": queue})
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
                "policy_category": policy,
                "queue": queue,
                "winner_user_id": winner_id,
                "winner_name": winner.get("executive"),
                "shadow_received_after": state[winner_id]["shadow_received_count"],
                "dynamic_penalty": winner.get("dynamic_penalty", 0.0),
            })
    return {
        "queue": queue,
        "decisions": decisions,
        "candidate_rows": candidate_rows,
        "final_state": state,
        "history": history,
        "ordered_lead_ids": [str(lead.get("lead_id")) for lead in sorted(leads, key=lead_sort_key)],
    }


def simulate_hybrid(
    leads: Iterable[Mapping[str, Any]],
    *,
    mode: str,
    scenario: str,
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50_average: float | None,
    team_p90_average: float | None,
    weights: Mapping[str, float] = BASE_WEIGHTS,
    params: Any,
    confidence_guard: bool = False,
) -> dict[str, Any]:
    source = [deepcopy(dict(lead)) for lead in leads]
    rm = [lead for lead in source if lead.get("policy_category") == RM_GLOBAL_RESCUE]
    jpc = [lead for lead in source if lead.get("policy_category") == REGION_JPC_MARIA_HERNAN]
    if mode == "H0_INDEPENDENT":
        rm_result = _simulate_queue(rm, pool_key="rm_candidates", queue="RM", scenario="H0_INDEPENDENT_DYNAMIC", state=_initial_state(rm, "rm_candidates"), team_sla_rate=team_sla_rate, team_attention_rate=team_attention_rate, team_p50_average=team_p50_average, team_p90_average=team_p90_average, weights=weights, params=params, confidence_guard=confidence_guard)
        jpc_result = _simulate_queue(jpc, pool_key="jpc_candidates", queue="JPC", scenario="H0_INDEPENDENT_DYNAMIC", state=_initial_state(jpc, "jpc_candidates"), team_sla_rate=team_sla_rate, team_attention_rate=team_attention_rate, team_p50_average=team_p50_average, team_p90_average=team_p90_average, weights=weights, params=params, confidence_guard=confidence_guard)
        decisions = rm_result["decisions"] + jpc_result["decisions"]
        return {
            "mode": mode,
            "decisions": decisions,
            "candidate_rows": rm_result["candidate_rows"] + jpc_result["candidate_rows"],
            "final_state_by_queue": {"RM": rm_result["final_state"], "JPC": jpc_result["final_state"]},
            "final_state": {},
            "history": rm_result["history"] + jpc_result["history"],
            "ordered_lead_ids": rm_result["ordered_lead_ids"] + jpc_result["ordered_lead_ids"],
        }
    state = _initial_state(rm, "rm_candidates")
    jpc_state = _initial_state(jpc, "jpc_candidates")
    for user_id, values in jpc_state.items():
        state.setdefault(user_id, values)
    # The combined queue uses one merged candidate key so RM and JPC rows share
    # the same chronological state.
    merged = []
    for lead in source:
        row = dict(lead)
        row["combined_candidates"] = row.get("rm_candidates", []) if row.get("policy_category") == RM_GLOBAL_RESCUE else row.get("jpc_candidates", [])
        merged.append(row)
    result = _simulate_queue(merged, pool_key="combined_candidates", queue="COMBINED", scenario=scenario, state=_initial_state(merged, "combined_candidates"), team_sla_rate=team_sla_rate, team_attention_rate=team_attention_rate, team_p50_average=team_p50_average, team_p90_average=team_p90_average, weights=weights, params=params, confidence_guard=confidence_guard)
    return {"mode": mode, **result}


def concentration_by_queue(decisions: Iterable[Mapping[str, Any]], queue: str | None = None) -> dict[str, Any]:
    selected = [row for row in decisions if row.get("shadow_category") in {WINNER_CATEGORY, CONFIDENCE_GUARD_CATEGORY} and (queue is None or row.get("queue") == queue)]
    counts = Counter(str(row.get("winner_user_id")) for row in selected)
    total = sum(counts.values())
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    shares = [count / total for _, count in ranked] if total else []
    return {
        "total": total,
        "received": dict(counts),
        "top1_share": ranked[0][1] / total if ranked and total else 0.0,
        "top2_share": sum(count for _, count in ranked[:2]) / total if total else 0.0,
        "top3_share": sum(count for _, count in ranked[:3]) / total if total else 0.0,
        "hhi": sum(share * share for share in shares),
        "max_per_executive": max(counts.values(), default=0),
    }


def maximum_consecutive_for(decisions: Iterable[Mapping[str, Any]], user_id: str | None = None) -> int:
    maximum = 0
    current = 0
    previous = None
    for decision in decisions:
        winner = str(decision.get("winner_user_id") or "")
        if user_id is not None and winner != user_id:
            if winner:
                current = 0
            continue
        if not winner:
            current = 0
            previous = None
            continue
        current = current + 1 if winner == previous else 1
        maximum = max(maximum, current)
        previous = winner
    return maximum
