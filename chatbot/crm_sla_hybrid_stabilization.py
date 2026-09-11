"""Pure shadow stabilisation tools for the CRM SLA hybrid policy.

The functions in this module receive already prepared analytical rows.  They
do not know how to connect to MongoDB and do not mutate leads, owners, cycles,
flags, routers, workers, schedulers or endpoints.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
from math import ceil, floor
from typing import Any, Iterable, Mapping
import unicodedata

from chatbot.crm_sla_global_rescue import (
    BASE_WEIGHTS,
    WINNER_CATEGORY,
    _number,
    _order_scored,
    filter_global_candidates,
    quantile,
    score_candidates,
)
from chatbot.crm_sla_hybrid_rescue import HERNAN_NAME, MARIA_NAME, REGION_JPC_MARIA_HERNAN, lead_sort_key, select_hybrid_winner
from chatbot.crm_sla_reassignment_cutover import (
    CUTOVER_POLICY_VERSION,
    ELIGIBLE as CUTOVER_ELIGIBLE,
    evaluate_cutover,
)


J0_SCORE_PURE = "J0_SCORE_PURE"
J1_ROUND_ROBIN = "J1_ROUND_ROBIN"
J2_SCORE_PLUS_LOAD = "J2_SCORE_PLUS_LOAD"
J3_PERFORMANCE_WEIGHTED_SHARE = "J3_PERFORMANCE_WEIGHTED_SHARE"
J4_SLA_FIRST_BALANCED = "J4_SLA_FIRST_BALANCED"

J2_PENALTIES = (2, 4, 6, 8, 10)
J4_GAPS = (1, 2, 3, 5)
JPC_MIN_SHARE = 0.30
JPC_MAX_SHARE = 0.70

R0_NO_GUARDRAIL = "R0_NO_GUARDRAIL"
R1_CONSECUTIVE = "R1_CONSECUTIVE_5_WITHIN_10"
R2_ROLLING_SHARE = "R2_ROLLING_20_OVER_40_WITHIN_15"
R3_COMBINED = "R3_R1_PLUS_R2"

L0_LOW_CAN_COMPETE = "L0_LOW_CAN_COMPETE"
L1_LOW_NEEDS_PLUS_5 = "L1_LOW_NEEDS_PLUS_5"

NO_ELIGIBLE_RESCUER = "NO_ELIGIBLE_RESCUER"
NO_ELIGIBLE_JPC_RESCUER = "NO_ELIGIBLE_JPC_RESCUER"
SUPERVISOR_REVIEW_REQUIRED = "SUPERVISOR_REVIEW_REQUIRED"
ABORT_MANAGEMENT_DETECTED = "ABORT_MANAGEMENT_DETECTED"
ABORT_CYCLE_CHANGED = "ABORT_CYCLE_CHANGED"
OK_TO_PERSIST = "OK_TO_PERSIST"
ABORT_LEAD_CHANGED = "ABORT_LEAD_CHANGED"
ABORT_OWNER_CHANGED = "ABORT_OWNER_CHANGED"
ABORT_CYCLE_CLOSED = "ABORT_CYCLE_CLOSED"
ABORT_LEAD_CLOSED = "ABORT_LEAD_CLOSED"
ABORT_SLA_NOT_EXPIRED = "ABORT_SLA_NOT_EXPIRED"
ABORT_SELECTED_USER_INACTIVE = "ABORT_SELECTED_USER_INACTIVE"
ABORT_SELECTED_USER_INELIGIBLE = "ABORT_SELECTED_USER_INELIGIBLE"
ABORT_ASSIGNMENT_LIMIT_REACHED = "ABORT_ASSIGNMENT_LIMIT_REACHED"
ABORT_DECISION_ALREADY_USED = "ABORT_DECISION_ALREADY_USED"
ABORT_POLICY_VERSION_CHANGED = "ABORT_POLICY_VERSION_CHANGED"


def _identity(candidate: Mapping[str, Any]) -> str:
    value = unicodedata.normalize("NFKD", str(candidate.get("identity_key") or candidate.get("executive_key") or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(value.strip().lower().split())


def _name(candidate: Mapping[str, Any]) -> str:
    return str(candidate.get("executive") or "")


def _is_maria(candidate: Mapping[str, Any]) -> bool:
    return _identity(candidate) == MARIA_NAME


def _is_hernan(candidate: Mapping[str, Any]) -> bool:
    return _identity(candidate) == HERNAN_NAME


def _candidate_state(leads: Iterable[Mapping[str, Any]], pool_key: str) -> dict[str, dict[str, float]]:
    state: dict[str, dict[str, float]] = {}
    for lead in leads:
        for raw in lead.get(pool_key, []):
            user_id = str(raw.get("user_id") or "")
            if not user_id:
                continue
            state.setdefault(user_id, {
                "simulated_open_current": _number(raw.get("open_current_policy")),
                "simulated_unmanaged_current": _number(raw.get("unmanaged_current_policy")),
                "simulated_expired_current": _number(raw.get("expired_current_policy")),
                "shadow_received_count": _number(raw.get("shadow_received_count")),
            })
    return state


def _scored_pool(
    lead: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    *,
    state: Mapping[str, Mapping[str, float]],
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50_average: float | None,
    team_p90_average: float | None,
    params: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prepared = []
    for raw in candidates:
        candidate = dict(raw)
        candidate.update(state.get(str(candidate.get("user_id") or ""), {}))
        prepared.append(candidate)
    valid, excluded = filter_global_candidates(lead, prepared)
    scored = score_candidates(
        valid,
        team_sla_rate=team_sla_rate,
        team_attention_rate=team_attention_rate,
        team_p50_average=team_p50_average,
        team_p90_average=team_p90_average,
        weights=BASE_WEIGHTS,
        params=params,
    )
    for candidate in scored:
        received = _number(candidate.get("shadow_received_count"))
        candidate["dynamic_penalty"] = min(
            received * params.dynamic_penalty_per_assignment,
            params.dynamic_penalty_cap,
        )
        candidate["dynamic_rescue_score"] = (
            None
            if candidate.get("global_rescue_score") is None
            else max(0.0, _number(candidate.get("global_rescue_score")) - candidate["dynamic_penalty"])
        )
        candidate["performance_confidence"] = (
            "HIGH" if _number(candidate.get("sample_size")) >= 30
            else "MEDIUM" if _number(candidate.get("sample_size")) >= 10
            else "LOW"
        )
    return scored, excluded


def _ordered(scored: Iterable[Mapping[str, Any]], field: str, params: Any) -> list[dict[str, Any]]:
    return _order_scored(
        [candidate for candidate in scored if candidate.get("performance_data_valid")],
        field,
        params.tie_threshold,
    )


def _update_state(state: dict[str, dict[str, float]], winner: Mapping[str, Any]) -> None:
    user_id = str(winner.get("user_id") or "")
    if not user_id:
        return
    state.setdefault(user_id, {
        "simulated_open_current": _number(winner.get("open_current_policy")),
        "simulated_unmanaged_current": _number(winner.get("unmanaged_current_policy")),
        "simulated_expired_current": _number(winner.get("expired_current_policy")),
        "shadow_received_count": _number(winner.get("shadow_received_count")),
    })
    state[user_id]["simulated_open_current"] += 1
    state[user_id]["simulated_unmanaged_current"] += 1
    state[user_id]["shadow_received_count"] += 1


def _decision(
    lead: Mapping[str, Any],
    strategy: str,
    scored: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
    ordered: list[dict[str, Any]],
    winner: Mapping[str, Any] | None,
    *,
    selection_reason: str,
    parameter: Any = "",
    cursor_before: int | None = None,
    cursor_after: int | None = None,
) -> dict[str, Any]:
    best = max((_number(row.get("global_rescue_score")) for row in scored if row.get("performance_data_valid")), default=None)
    selected_score = _number(winner.get("global_rescue_score")) if winner else None
    regret = best - selected_score if best is not None and selected_score is not None else None
    return {
        "lead": dict(lead),
        "strategy": strategy,
        "parameter": parameter,
        "scored": scored,
        "hard_excluded": excluded,
        "ordered": ordered,
        "winner": dict(winner) if winner else None,
        "winner_user_id": str(winner.get("user_id") or "") if winner else "",
        "winner_name": _name(winner) if winner else "",
        "selected_base_score": selected_score,
        "selected_effective_score": _number(winner.get("effective_selection_score")) if winner else None,
        "best_available_base_score": best,
        "selection_regret": regret,
        "selection_reason": selection_reason,
        "winner_p50_effective": winner.get("p50_effective") if winner else None,
        "winner_adjusted_sla_rate": winner.get("adjusted_sla_rate") if winner else None,
        "winner_performance_confidence": winner.get("performance_confidence") if winner else "",
        "cursor_before": cursor_before,
        "cursor_after": cursor_after,
        "winner_category": WINNER_CATEGORY if winner else (NO_ELIGIBLE_JPC_RESCUER if strategy.startswith("J") else NO_ELIGIBLE_RESCUER),
    }


def _ordered_jpc_candidates(scored: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    canonical = {MARIA_NAME: 0, HERNAN_NAME: 1}
    return sorted(
        (dict(row) for row in scored),
        key=lambda row: (canonical.get(_identity(row), 99), str(row.get("user_id") or "")),
    )


def derive_jpc_share_targets(
    candidates: Iterable[Mapping[str, Any]],
    *,
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50_average: float | None,
    team_p90_average: float | None,
    params: Any,
) -> dict[str, Any]:
    """Derive bounded shares from initial base scores, without a 50/50 rule."""
    rows = [dict(row) for row in candidates]
    scored = score_candidates(
        rows,
        team_sla_rate=team_sla_rate,
        team_attention_rate=team_attention_rate,
        team_p50_average=team_p50_average,
        team_p90_average=team_p90_average,
        weights=BASE_WEIGHTS,
        params=params,
    )
    scores = {str(row.get("user_id") or ""): max(0.0, _number(row.get("global_rescue_score"))) for row in scored if row.get("performance_data_valid")}
    selected = {user_id: score for user_id, score in scores.items() if score > 0}
    total = sum(selected.values())
    raw = {user_id: score / total for user_id, score in selected.items()} if total else {}
    bounded = {user_id: max(JPC_MIN_SHARE, min(JPC_MAX_SHARE, share)) for user_id, share in raw.items()}
    if len(bounded) == 2:
        first, second = list(bounded)
        bounded[second] = 1.0 - bounded[first]
    elif len(bounded) == 1:
        bounded[next(iter(bounded))] = 1.0
    return {"scores": scores, "raw_shares": raw, "targets": bounded, "scored": scored}


def simulate_jpc_strategy(
    leads: Iterable[Mapping[str, Any]],
    *,
    strategy: str,
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50_average: float | None,
    team_p90_average: float | None,
    params: Any,
    parameter: int | None = None,
    share_targets: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Run J0-J4 sequentially using only in-memory state."""
    ordered_leads = sorted((deepcopy(dict(lead)) for lead in leads), key=lead_sort_key)
    state = _candidate_state(ordered_leads, "jpc_candidates")
    total_leads = len(ordered_leads)
    jpc_minimum_count = int(ceil(total_leads * JPC_MIN_SHARE)) if total_leads else 0
    jpc_maximum_count = int(floor(total_leads * JPC_MAX_SHARE)) if total_leads else 0
    decisions: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    cursor = 0
    counts: Counter[str] = Counter()
    targets = dict(share_targets or {})
    for step, lead in enumerate(ordered_leads, start=1):
        scored, excluded = _scored_pool(
            lead,
            lead.get("jpc_candidates", []),
            state=state,
            team_sla_rate=team_sla_rate,
            team_attention_rate=team_attention_rate,
            team_p50_average=team_p50_average,
            team_p90_average=team_p90_average,
            params=params,
        )
        ordered = _ordered(scored, "global_rescue_score", params)
        winner: Mapping[str, Any] | None = None
        reason = ""
        before = cursor
        if strategy == J0_SCORE_PURE:
            winner = ordered[0] if ordered else None
            reason = "highest_base_rescue_score" if winner else NO_ELIGIBLE_JPC_RESCUER
        elif strategy == J1_ROUND_ROBIN:
            canonical = _ordered_jpc_candidates(scored)
            if canonical:
                desired = cursor % 2
                by_turn = {0: next((row for row in canonical if _is_maria(row)), None), 1: next((row for row in canonical if _is_hernan(row)), None)}
                winner = by_turn.get(desired) or by_turn.get(1 - desired)
                cursor = (cursor + 1) % 2
                reason = "deterministic_round_robin" if winner else NO_ELIGIBLE_JPC_RESCUER
            else:
                reason = NO_ELIGIBLE_JPC_RESCUER
        elif strategy == J2_SCORE_PLUS_LOAD:
            penalty = float(parameter or 0)
            for row in scored:
                row["balancing_penalty"] = _number(row.get("shadow_received_count")) * penalty
                row["effective_jpc_score"] = max(0.0, _number(row.get("global_rescue_score")) - row["balancing_penalty"])
            ordered = _ordered(scored, "effective_jpc_score", params)
            winner = ordered[0] if ordered else None
            reason = f"base_score_minus_received_times_{parameter}" if winner else NO_ELIGIBLE_JPC_RESCUER
        elif strategy == J3_PERFORMANCE_WEIGHTED_SHARE:
            available = {str(row.get("user_id") or ""): row for row in scored if row.get("performance_data_valid")}
            if available:
                total_before = sum(counts.values())
                ranked = []
                for user_id, row in available.items():
                    target = _number(targets.get(user_id), 1.0 / len(available))
                    projected = (counts[user_id] + 1) / (total_before + 1)
                    projected_counts = dict(counts)
                    projected_counts[user_id] = projected_counts.get(user_id, 0) + 1
                    remaining_after = max(0, total_leads - total_before - 1)
                    feasible = all(
                        projected_counts.get(candidate_id, 0) <= jpc_maximum_count
                        and projected_counts.get(candidate_id, 0) + remaining_after >= jpc_minimum_count
                        for candidate_id in available
                    ) if len(available) >= 2 else True
                    ranked.append((feasible, target - projected, _number(row.get("global_rescue_score")), user_id, row))
                feasible_ranked = [item for item in ranked if item[0]] or ranked
                feasible_ranked.sort(key=lambda item: (-item[1], -item[2], item[3]))
                winner = feasible_ranked[0][4]
                reason = "most_underrepresented_against_score_derived_target"
            else:
                reason = NO_ELIGIBLE_JPC_RESCUER
        elif strategy == J4_SLA_FIRST_BALANCED:
            gap = int(parameter or 1)
            if ordered:
                best = ordered[0]
                best_id = str(best.get("user_id") or "")
                hypothetical = dict(counts)
                hypothetical[best_id] = hypothetical.get(best_id, 0) + 1
                if len(scored) >= 2 and abs(hypothetical.get(_user_id(scored[0]), 0) - hypothetical.get(_user_id(scored[1]), 0)) > gap:
                    least = min(scored, key=lambda row: (counts[str(row.get("user_id") or "")], -_number(row.get("global_rescue_score")), str(row.get("user_id") or "")))
                    winner = least
                    reason = f"least_loaded_to_respect_gap_{gap}"
                else:
                    winner = best
                    reason = f"best_score_within_gap_{gap}"
            else:
                reason = NO_ELIGIBLE_JPC_RESCUER
        else:
            raise ValueError(f"unknown JPC strategy: {strategy}")
        if winner:
            winner = dict(winner)
            if strategy == J2_SCORE_PLUS_LOAD:
                winner["effective_selection_score"] = winner.get("effective_jpc_score")
            else:
                winner["effective_selection_score"] = winner.get("global_rescue_score")
            counts[str(winner.get("user_id") or "")] += 1
            _update_state(state, winner)
            history.append({"step": step, "lead_id": lead.get("lead_id"), "winner_user_id": winner.get("user_id"), "winner_name": winner.get("executive"), "received_after": counts[str(winner.get("user_id") or "")]})
        decision = _decision(lead, strategy, scored, excluded, ordered, winner, selection_reason=reason, parameter=parameter or "", cursor_before=before, cursor_after=cursor)
        decision["step"] = step
        decision["queue"] = "JPC"
        decision["winner_category"] = WINNER_CATEGORY if winner else NO_ELIGIBLE_JPC_RESCUER
        decisions.append(decision)
    return {
        "strategy": strategy,
        "parameter": parameter or "",
        "decisions": decisions,
        "history": history,
        "final_state": state,
        "counts": dict(counts),
        "share_targets": targets,
        "ordered_lead_ids": [str(row.get("lead_id") or "") for row in ordered_leads],
    }


def _user_id(row: Mapping[str, Any]) -> str:
    return str(row.get("user_id") or "")


def _guardrail_violation(
    candidate: Mapping[str, Any],
    scored: list[Mapping[str, Any]],
    winners: list[str],
    *,
    scenario: str,
) -> str:
    candidate_id = _user_id(candidate)
    score = _number(candidate.get("dynamic_rescue_score"))
    alternatives = [row for row in scored if _user_id(row) != candidate_id and row.get("performance_data_valid")]
    if scenario in {R1_CONSECUTIVE, R3_COMBINED} and alternatives:
        streak = 0
        for winner_id in reversed(winners):
            if winner_id != candidate_id:
                break
            streak += 1
        if streak >= 5 and any(score - _number(row.get("dynamic_rescue_score")) <= 10.0 for row in alternatives):
            return "R1_CONSECUTIVE_LIMIT"
    if scenario in {R2_ROLLING_SHARE, R3_COMBINED} and len(winners) >= 19 and alternatives:
        window = winners[-19:]
        if window.count(candidate_id) >= 8 and any(score - _number(row.get("dynamic_rescue_score")) <= 15.0 for row in alternatives):
            return "R2_ROLLING_SHARE_LIMIT"
    return ""


def simulate_rm_guardrail(
    leads: Iterable[Mapping[str, Any]],
    *,
    scenario: str,
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50_average: float | None,
    team_p90_average: float | None,
    params: Any,
    low_policy: str = L0_LOW_CAN_COMPETE,
    context_leads: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Simulate RM_POLICY_V1 with one analytical guardrail option.

    When ``context_leads`` is supplied, JPC leads are replayed in the same
    chronological memory-only queue used by Fase 1E so their assignments affect
    the shared RM dynamic state.  Only RM decisions are returned as metrics.
    """
    rm_source = [deepcopy(dict(lead)) for lead in leads]
    context_source = [deepcopy(dict(lead)) for lead in (context_leads if context_leads is not None else rm_source)]
    ordered_context = sorted(context_source, key=lead_sort_key)
    rm_ids = {str(lead.get("lead_id") or "") for lead in rm_source}
    state: dict[str, dict[str, float]] = {}
    for lead in ordered_context:
        pool_key = "rm_candidates" if lead.get("policy_category") == "RM_GLOBAL_RESCUE" or str(lead.get("lead_id") or "") in rm_ids else "jpc_candidates"
        for raw in lead.get(pool_key, []):
            user_id = _user_id(raw)
            if user_id:
                state.setdefault(user_id, {
                    "simulated_open_current": _number(raw.get("open_current_policy")),
                    "simulated_unmanaged_current": _number(raw.get("unmanaged_current_policy")),
                    "simulated_expired_current": _number(raw.get("expired_current_policy")),
                    "shadow_received_count": _number(raw.get("shadow_received_count")),
                })
    decisions: list[dict[str, Any]] = []
    winners: list[str] = []
    counts: Counter[str] = Counter()
    rm_step = 0
    for context_step, lead in enumerate(ordered_context, start=1):
        lead_id = str(lead.get("lead_id") or "")
        is_rm = lead.get("policy_category") == "RM_GLOBAL_RESCUE" or lead_id in rm_ids
        if not is_rm:
            if lead.get("policy_category") == REGION_JPC_MARIA_HERNAN:
                jpc_candidates = []
                for raw in lead.get("jpc_candidates", []):
                    candidate = dict(raw)
                    candidate.update(state.get(_user_id(candidate), {}))
                    jpc_candidates.append(candidate)
                jpc_decision = select_hybrid_winner(
                    lead,
                    jpc_candidates,
                    policy_category=REGION_JPC_MARIA_HERNAN,
                    scenario="H1_COMBINED_DYNAMIC",
                    team_sla_rate=team_sla_rate,
                    team_attention_rate=team_attention_rate,
                    team_p50_average=team_p50_average,
                    team_p90_average=team_p90_average,
                    weights=BASE_WEIGHTS,
                    params=params,
                )
                if jpc_decision.get("shadow_winner"):
                    _update_state(state, jpc_decision["shadow_winner"])
            continue
        rm_step += 1
        scored, excluded = _scored_pool(
            lead,
            lead.get("rm_candidates", []),
            state=state,
            team_sla_rate=team_sla_rate,
            team_attention_rate=team_attention_rate,
            team_p50_average=team_p50_average,
            team_p90_average=team_p90_average,
            params=params,
        )
        ordered = _ordered(scored, "dynamic_rescue_score", params)
        best_non_low = max((_number(row.get("dynamic_rescue_score")) for row in scored if row.get("performance_data_valid") and row.get("performance_confidence") != "LOW"), default=None)
        guardrail_excluded: list[dict[str, Any]] = []
        winner = None
        for candidate in ordered:
            candidate = dict(candidate)
            if low_policy == L1_LOW_NEEDS_PLUS_5 and candidate.get("performance_confidence") == "LOW" and best_non_low is not None and _number(candidate.get("dynamic_rescue_score")) < best_non_low + 5.0:
                candidate["low_sample_exclusion_reason"] = "LOW_REQUIRES_PLUS_5_OVER_BEST_NON_LOW"
                guardrail_excluded.append(candidate)
                continue
            reason = _guardrail_violation(candidate, scored, winners, scenario=scenario)
            if reason:
                candidate["guardrail_exclusion_reason"] = reason
                guardrail_excluded.append(candidate)
                continue
            winner = candidate
            break
        if winner:
            winner["effective_selection_score"] = winner.get("dynamic_rescue_score")
            winner_id = _user_id(winner)
            winners.append(winner_id)
            counts[winner_id] += 1
            _update_state(state, winner)
            reason = "RM_POLICY_V1" if scenario == R0_NO_GUARDRAIL and low_policy == L0_LOW_CAN_COMPETE else "guardrail_or_low_policy_adjusted"
        else:
            reason = NO_ELIGIBLE_RESCUER
        decision = _decision(lead, scenario, scored, excluded + guardrail_excluded, ordered, winner, selection_reason=reason)
        decision.update({
            "step": context_step,
            "rm_step": rm_step,
            "queue": "RM",
            "guardrail_excluded": guardrail_excluded,
            "guardrail_excluded_count": len(guardrail_excluded),
            "winner_category": WINNER_CATEGORY if winner else NO_ELIGIBLE_RESCUER,
            "winner_category_detail": "SHADOW_WINNER_SELECTED" if winner else NO_ELIGIBLE_RESCUER,
        })
        decisions.append(decision)
    return {
        "scenario": scenario,
        "low_policy": low_policy,
        "decisions": decisions,
        "final_state": state,
        "counts": dict(counts),
        "winners": winners,
        "context_size": len(ordered_context),
    }


def sequence_metrics(
    decisions: Iterable[Mapping[str, Any]],
    *,
    names: Mapping[str, str] | None = None,
    maria_id: str = "",
    hernan_id: str = "",
) -> dict[str, Any]:
    rows = list(decisions)
    winners = [row for row in rows if row.get("winner_user_id")]
    count = Counter(str(row.get("winner_user_id") or "") for row in winners)
    total = len(winners)
    ordered_counts = sorted(count.values(), reverse=True)
    shares = [value / total for value in count.values()] if total else []
    regrets = [_number(row.get("selection_regret")) for row in winners if row.get("selection_regret") is not None]
    losses = [_number(row.get("loss_vs_j0")) for row in winners if row.get("loss_vs_j0") is not None]
    p50 = [_number(row.get("winner_p50_effective")) for row in winners if row.get("winner_p50_effective") is not None]
    sla = [_number(row.get("winner_adjusted_sla_rate")) for row in winners if row.get("winner_adjusted_sla_rate") is not None]
    max_consecutive: dict[str, int] = {}
    previous = ""
    streak = 0
    for row in rows:
        winner_id = str(row.get("winner_user_id") or "")
        if not winner_id:
            previous = ""
            streak = 0
            continue
        streak = streak + 1 if winner_id == previous else 1
        max_consecutive[winner_id] = max(max_consecutive.get(winner_id, 0), streak)
        previous = winner_id
    maria_received = count.get(maria_id, 0) if maria_id else 0
    hernan_received = count.get(hernan_id, 0) if hernan_id else 0
    return {
        "received": dict(count),
        "maria_received": maria_received,
        "hernan_received": hernan_received,
        "maria_share": maria_received / total if total else 0.0,
        "hernan_share": hernan_received / total if total else 0.0,
        "coverage": total / len(rows) if rows else 0.0,
        "no_winner": len(rows) - total,
        "winner_score_average": sum(_number(row.get("selected_base_score")) for row in winners) / total if total else None,
        "winner_effective_score_average": sum(_number(row.get("selected_effective_score")) for row in winners) / total if total else None,
        "loss_vs_j0_average": sum(losses) / len(losses) if losses else 0.0,
        "p50_average": sum(p50) / len(p50) if p50 else None,
        "sla_adjusted_average": sum(sla) / len(sla) if sla else None,
        "regret_average": sum(regrets) / len(regrets) if regrets else 0.0,
        "regret_median": quantile(regrets, 0.50) or 0.0,
        "regret_p90": quantile(regrets, 0.90) or 0.0,
        "regret_max": max(regrets, default=0.0),
        "max_consecutive_maria": max_consecutive.get(maria_id, 0) if maria_id else 0,
        "max_consecutive_hernan": max_consecutive.get(hernan_id, 0) if hernan_id else 0,
        "max_consecutive": max(max_consecutive.values(), default=0),
        "load_gap": abs(maria_received - hernan_received),
        "receivers": len(count),
        "top1_share": ordered_counts[0] / total if ordered_counts and total else 0.0,
        "hhi": sum(share * share for share in shares),
    }


def annotate_loss_vs_j0(results: Mapping[str, dict[str, Any]], j0_key: str = J0_SCORE_PURE) -> None:
    reference = {
        str(row.get("lead", {}).get("lead_id") or ""): _number(row.get("selected_base_score"))
        for row in results[j0_key].get("decisions", [])
        if row.get("winner_user_id")
    }
    for result in results.values():
        for row in result.get("decisions", []):
            lead_id = str(row.get("lead", {}).get("lead_id") or "")
            if row.get("winner_user_id") and lead_id in reference:
                row["pure_reference_score"] = reference[lead_id]
                row["loss_vs_j0"] = max(0.0, reference[lead_id] - _number(row.get("selected_base_score")))
            else:
                row["pure_reference_score"] = None
                row["loss_vs_j0"] = None


def generate_decision_id(lead_id: Any, cycle_id: Any, policy_version: Any) -> str:
    payload = "|".join(str(value or "") for value in (lead_id, cycle_id, policy_version))
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SLAReassignmentDecision:
    lead_id: str
    current_assignment_cycle_id: str
    previous_owner_user_id: str
    policy_branch: str
    candidate_user_ids: tuple[str, ...]
    selected_user_id: str | None
    selected_score: float | None
    selection_reason: str
    performance_confidence: str
    assignment_number: int
    excluded_previous_owners: tuple[str, ...]
    management_evidence_checked_at: str
    sla_breached_at: str
    evaluated_at: str
    policy_version: str
    decision_id: str
    requires_supervisor_review: bool
    review_reason: str
    candidate_scores_snapshot: tuple[dict[str, Any], ...] = ()
    selection_rule: str = ""
    guardrail_applied: bool = False
    guardrail_reason: str = ""
    jpc_target_share: dict[str, float] | None = None
    previous_owner_user_ids: tuple[str, ...] = ()
    automatic_reassignment_number: int | None = None
    cycle_version: str = ""
    reassignment_cutover_at: str = ""
    source_cycle_sla_breached_at: str = ""
    cutover_eligible: bool = False
    cutover_policy_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_decision(
    *,
    lead_id: Any,
    current_assignment_cycle_id: Any,
    previous_owner_user_id: Any,
    policy_branch: str,
    candidate_user_ids: Iterable[Any],
    selected_user_id: Any = None,
    selected_score: float | None = None,
    selection_reason: str = "",
    performance_confidence: str = "",
    assignment_number: int = 0,
    excluded_previous_owners: Iterable[Any] = (),
    management_evidence_checked_at: str = "",
    sla_breached_at: str = "",
    evaluated_at: str = "",
    policy_version: str = "",
    requires_supervisor_review: bool | None = None,
    review_reason: str = "",
    candidate_scores_snapshot: Iterable[Mapping[str, Any]] = (),
    selection_rule: str = "",
    guardrail_applied: bool = False,
    guardrail_reason: str = "",
    jpc_target_share: Mapping[str, float] | None = None,
    previous_owner_user_ids: Iterable[Any] = (),
    automatic_reassignment_number: int | None = None,
    cycle_version: str = "",
    reassignment_cutover_at: Any = "",
    source_cycle_sla_breached_at: Any = "",
    cutover_eligible: bool | None = None,
    cutover_policy_version: str = "",
) -> SLAReassignmentDecision:
    review = assignment_number >= 2 if requires_supervisor_review is None else bool(requires_supervisor_review)
    reason = review_reason or (SUPERVISOR_REVIEW_REQUIRED if review else "")
    version = str(policy_version or "")
    breach_value = source_cycle_sla_breached_at or sla_breached_at or ""
    cutover_value = reassignment_cutover_at or ""
    cutover_version = str(cutover_policy_version or (CUTOVER_POLICY_VERSION if cutover_value else ""))
    if cutover_value and breach_value:
        cutover_eval = evaluate_cutover(
            breached_at=breach_value,
            cutover_at=cutover_value,
        )
        cutover_value = cutover_eval.cutover_at.isoformat() if cutover_eval.cutover_at else str(cutover_value)
        breach_value = cutover_eval.breached_at.isoformat() if cutover_eval.breached_at else str(breach_value)
        cutover_eligible_value = cutover_eval.outcome == CUTOVER_ELIGIBLE
    else:
        cutover_eligible_value = bool(cutover_eligible) if cutover_eligible is not None else False
    return SLAReassignmentDecision(
        lead_id=str(lead_id or ""),
        current_assignment_cycle_id=str(current_assignment_cycle_id or ""),
        previous_owner_user_id=str(previous_owner_user_id or ""),
        policy_branch=policy_branch,
        candidate_user_ids=tuple(str(value or "") for value in candidate_user_ids),
        selected_user_id=str(selected_user_id) if selected_user_id is not None else None,
        selected_score=selected_score,
        selection_reason=selection_reason,
        performance_confidence=performance_confidence,
        assignment_number=int(assignment_number),
        excluded_previous_owners=tuple(str(value or "") for value in excluded_previous_owners),
        management_evidence_checked_at=management_evidence_checked_at,
        sla_breached_at=sla_breached_at,
        evaluated_at=evaluated_at,
        policy_version=version,
        decision_id=generate_decision_id(lead_id, current_assignment_cycle_id, version),
        requires_supervisor_review=review,
        review_reason=reason,
        candidate_scores_snapshot=tuple(dict(row) for row in candidate_scores_snapshot),
        selection_rule=selection_rule,
        guardrail_applied=bool(guardrail_applied),
        guardrail_reason=guardrail_reason,
        jpc_target_share={str(key): float(value) for key, value in (jpc_target_share or {}).items()} or None,
        previous_owner_user_ids=tuple(str(value or "") for value in (previous_owner_user_ids or excluded_previous_owners)),
        automatic_reassignment_number=int(assignment_number if automatic_reassignment_number is None else automatic_reassignment_number),
        cycle_version=str(cycle_version or ""),
        reassignment_cutover_at=str(cutover_value),
        source_cycle_sla_breached_at=str(breach_value),
        cutover_eligible=cutover_eligible_value,
        cutover_policy_version=cutover_version,
    )


def anti_ping_pong_candidates(
    candidates: Iterable[Mapping[str, Any]],
    *,
    current_owner_user_id: Any,
    excluded_previous_sla_owners: Iterable[Any],
) -> list[dict[str, Any]]:
    excluded = {str(value or "") for value in excluded_previous_sla_owners}
    excluded.add(str(current_owner_user_id or ""))
    return [dict(candidate) for candidate in candidates if str(candidate.get("user_id") or "") not in excluded]


def simulate_anti_ping_pong(
    candidates: Iterable[Mapping[str, Any]],
    *,
    owners: tuple[str, ...],
    policy_branch: str,
) -> dict[str, Any]:
    """Simulate two future rescues while accumulating previous SLA owners."""
    available = [dict(candidate) for candidate in candidates]
    excluded: list[str] = []
    path: list[str] = []
    for assignment_number, current_owner in enumerate(owners, start=1):
        pool = anti_ping_pong_candidates(available, current_owner_user_id=current_owner, excluded_previous_sla_owners=excluded)
        selected = pool[0] if pool else None
        if selected:
            path.append(str(selected.get("user_id") or ""))
            excluded.append(str(current_owner or ""))
        else:
            path.append(NO_ELIGIBLE_JPC_RESCUER if policy_branch == "REGION_JPC_MARIA_HERNAN" else NO_ELIGIBLE_RESCUER)
            excluded.append(str(current_owner or ""))
    return {"owners": owners, "path": path, "excluded_previous_sla_owners": excluded, "no_a_to_b_to_a": len(path) >= 2 and path[1] == owners[0], "policy_branch": policy_branch}


def reassignment_limit_status(automatic_reassignment_count: int, *, max_automatic_sla_reassignments: int = 2) -> dict[str, Any]:
    count = int(automatic_reassignment_count)
    if count >= max_automatic_sla_reassignments:
        return {"status": SUPERVISOR_REVIEW_REQUIRED, "requires_supervisor_review": True, "review_reason": "MAX_AUTOMATIC_SLA_REASSIGNMENTS_REACHED", "automatic_reassignment_count": count}
    return {"status": "AUTO_ELIGIBLE", "requires_supervisor_review": False, "review_reason": "", "automatic_reassignment_count": count}


def management_race_status(snapshot: Mapping[str, Any], current: Mapping[str, Any]) -> str:
    if current.get("human_management_detected") and not snapshot.get("human_management_detected"):
        return ABORT_MANAGEMENT_DETECTED
    if current.get("management_evidence_version") != snapshot.get("management_evidence_version"):
        return ABORT_MANAGEMENT_DETECTED
    if current.get("management_evidence_fingerprint") != snapshot.get("management_evidence_fingerprint"):
        return ABORT_MANAGEMENT_DETECTED
    return OK_TO_PERSIST


def cycle_race_status(snapshot: Mapping[str, Any], current: Mapping[str, Any]) -> str:
    if str(current.get("assignment_cycle_id") or "") != str(snapshot.get("assignment_cycle_id") or ""):
        return ABORT_CYCLE_CHANGED
    if str(current.get("owner_user_id") or "") != str(snapshot.get("owner_user_id") or ""):
        return ABORT_CYCLE_CHANGED
    return OK_TO_PERSIST


def decision_pre_persist_status(snapshot: Mapping[str, Any], current: Mapping[str, Any]) -> str:
    management = management_race_status(snapshot, current)
    return management if management != OK_TO_PERSIST else cycle_race_status(snapshot, current)


def pretransaction_revalidation_status(
    snapshot: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    selected_user_id: Any,
    expected_policy_version: Any,
    decision_id_used: bool = False,
) -> str:
    """Validate the future write preconditions without performing a write.

    The first failing precondition wins.  Each data/state mismatch returns an
    explicit ABORT_* code so a future transactional adapter can fail closed.
    """
    if str(current.get("lead_id") or "") != str(snapshot.get("lead_id") or ""):
        return ABORT_LEAD_CHANGED
    if str(current.get("assignment_cycle_id") or "") != str(snapshot.get("assignment_cycle_id") or ""):
        return ABORT_CYCLE_CHANGED
    if str(current.get("owner_user_id") or "") != str(snapshot.get("owner_user_id") or ""):
        return ABORT_OWNER_CHANGED
    if current.get("cycle_open") is not True:
        return ABORT_CYCLE_CLOSED
    if current.get("lead_open") is not True:
        return ABORT_LEAD_CLOSED
    if current.get("sla_expired") is not True:
        return ABORT_SLA_NOT_EXPIRED
    if current.get("human_management_detected") is True:
        return ABORT_MANAGEMENT_DETECTED
    if current.get("selected_user_active") is not True:
        return ABORT_SELECTED_USER_INACTIVE
    if current.get("selected_user_eligible") is not True:
        return ABORT_SELECTED_USER_INELIGIBLE
    if int(_number(current.get("automatic_reassignment_number"))) >= 2:
        return ABORT_ASSIGNMENT_LIMIT_REACHED
    if decision_id_used:
        return ABORT_DECISION_ALREADY_USED
    if str(current.get("policy_version") or "") != str(expected_policy_version or ""):
        return ABORT_POLICY_VERSION_CHANGED
    return OK_TO_PERSIST
