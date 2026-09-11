"""Pure analytical helpers for Fase 1G policy freeze.

This module deliberately receives prepared CRM rows and runs only in-memory
simulations.  It does not import a repository, Mongo client, endpoint, worker,
router, scheduler or UI component.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from math import ceil
import random
import unicodedata
from typing import Any, Iterable, Mapping

from chatbot.crm_sla_global_rescue import (
    BASE_WEIGHTS,
    WINNER_CATEGORY,
    _number,
    _order_scored,
    filter_global_candidates,
    quantile,
    score_candidates,
)
from chatbot.crm_sla_hybrid_rescue import (
    HERNAN_NAME,
    MARIA_NAME,
    REGION_JPC_MARIA_HERNAN,
    RM_GLOBAL_RESCUE,
    lead_sort_key,
    performance_confidence,
)
from chatbot.crm_sla_hybrid_stabilization import (
    ABORT_ASSIGNMENT_LIMIT_REACHED,
    ABORT_CYCLE_CHANGED,
    ABORT_CYCLE_CLOSED,
    ABORT_DECISION_ALREADY_USED,
    ABORT_LEAD_CHANGED,
    ABORT_LEAD_CLOSED,
    ABORT_MANAGEMENT_DETECTED,
    ABORT_OWNER_CHANGED,
    ABORT_POLICY_VERSION_CHANGED,
    ABORT_SELECTED_USER_INACTIVE,
    ABORT_SELECTED_USER_INELIGIBLE,
    ABORT_SLA_NOT_EXPIRED,
    J3_PERFORMANCE_WEIGHTED_SHARE,
    L1_LOW_NEEDS_PLUS_5,
    NO_ELIGIBLE_JPC_RESCUER,
    NO_ELIGIBLE_RESCUER,
    OK_TO_PERSIST,
    R0_NO_GUARDRAIL,
    R1_CONSECUTIVE,
    R2_ROLLING_SHARE,
    R3_COMBINED,
    SUPERVISOR_REVIEW_REQUIRED,
    _guardrail_violation,
    build_decision,
    cycle_race_status,
    decision_pre_persist_status,
    derive_jpc_share_targets,
    generate_decision_id,
    management_race_status,
    pretransaction_revalidation_status,
    reassignment_limit_status,
    simulate_jpc_strategy,
    simulate_rm_guardrail,
)


POLICY_VERSION = "crm_sla_reassignment_v1"
RM_LONG_RUN_POLICIES = (R0_NO_GUARDRAIL, R1_CONSECUTIVE, R2_ROLLING_SHARE, R3_COMBINED)
RM_CANDIDATE_POLICIES = (R1_CONSECUTIVE, R2_ROLLING_SHARE, R3_COMBINED)
RM_LONG_RUN_SIZES = (50, 100, 200, 500)
JPC_LONG_RUN_SIZES = (30, 100, 250)
COMBINED_SIZES = (100, 250, 500)
BOOTSTRAP_REPLICATES = 100
BOOTSTRAP_SEED = 20260910
MAX_AUTOMATIC_REASSIGNMENTS = 2


def _uid(row: Mapping[str, Any]) -> str:
    return str(row.get("user_id") or "")


def _normalised(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.lower().strip().split())


def _candidate_identity(row: Mapping[str, Any]) -> str:
    return _normalised(row.get("identity_key") or row.get("executive_key") or row.get("executive"))


def _state_for(leads: Iterable[Mapping[str, Any]], keys: tuple[str, ...] = ("rm_candidates", "jpc_candidates")) -> dict[str, dict[str, float]]:
    state: dict[str, dict[str, float]] = {}
    for lead in leads:
        for key in keys:
            for raw in lead.get(key, []):
                user_id = _uid(raw)
                if not user_id:
                    continue
                state.setdefault(user_id, {
                    "simulated_open_current": _number(raw.get("open_current_policy")),
                    "simulated_unmanaged_current": _number(raw.get("unmanaged_current_policy")),
                    "simulated_expired_current": _number(raw.get("expired_current_policy")),
                    "shadow_received_count": _number(raw.get("shadow_received_count")),
                })
    return state


def _update_state(state: dict[str, dict[str, float]], winner: Mapping[str, Any]) -> None:
    user_id = _uid(winner)
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


def bootstrap_sequence(pool: Iterable[Mapping[str, Any]], size: int, rng: random.Random, prefix: str) -> list[dict[str, Any]]:
    source = list(pool)
    if not source:
        return []
    sequence = []
    for index in range(size):
        lead = deepcopy(dict(rng.choice(source)))
        lead["lead_id"] = f"{prefix}-lead-{index + 1:04d}"
        lead["assignment_cycle_id"] = f"{prefix}-cycle-{index + 1:04d}"
        sequence.append(lead)
    return sequence


def _with_shared_state(candidates: Iterable[Mapping[str, Any]], state: Mapping[str, Mapping[str, float]]) -> list[dict[str, Any]]:
    prepared = []
    for raw in candidates:
        row = dict(raw)
        row.update(state.get(_uid(row), {}))
        prepared.append(row)
    return prepared


def _previous_owners(lead: Mapping[str, Any]) -> set[str]:
    values = lead.get("previous_sla_owner_ids") or lead.get("excluded_previous_owners") or []
    return {str(value or "") for value in values if str(value or "")}


def _filter_with_history(lead: Mapping[str, Any], candidates: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid, excluded = filter_global_candidates(lead, candidates)
    previous = _previous_owners(lead)
    if not previous:
        return valid, excluded
    kept: list[dict[str, Any]] = []
    for row in valid:
        if _uid(row) in previous:
            item = dict(row)
            item["excluded_reason"] = "previous_sla_owner_excluded"
            excluded.append(item)
        else:
            kept.append(row)
    return kept, excluded


def _score_shared(
    lead: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    state: Mapping[str, Mapping[str, float]],
    *,
    team_sla_rate: float,
    team_attention_rate: float,
    team_p50_average: float | None,
    team_p90_average: float | None,
    params: Any,
    dynamic: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prepared = _with_shared_state(candidates, state)
    valid, excluded = _filter_with_history(lead, prepared)
    scored = score_candidates(
        valid,
        team_sla_rate=team_sla_rate,
        team_attention_rate=team_attention_rate,
        team_p50_average=team_p50_average,
        team_p90_average=team_p90_average,
        weights=BASE_WEIGHTS,
        params=params,
    )
    for row in scored:
        row["performance_confidence"] = performance_confidence(row.get("sample_size"))
        row["non_speed_score"] = (
            BASE_WEIGHTS["sla"] * _number(row.get("adjusted_sla_score"))
            + BASE_WEIGHTS["attention"] * _number(row.get("adjusted_attention_score"))
            + BASE_WEIGHTS["capacity"] * _number(row.get("capacity_score"))
        )
        received = _number(row.get("shadow_received_count"))
        row["dynamic_penalty"] = min(received * params.dynamic_penalty_per_assignment, params.dynamic_penalty_cap) if dynamic else 0.0
        row["dynamic_rescue_score"] = (
            None if row.get("global_rescue_score") is None
            else max(0.0, _number(row.get("global_rescue_score")) - row["dynamic_penalty"])
        )
    return scored, excluded


def _decision_row(
    lead: Mapping[str, Any],
    *,
    queue: str,
    step: int,
    scored: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
    winner: Mapping[str, Any] | None,
    reason: str,
    guardrail_reason: str = "",
    jpc_target_share: Mapping[str, float] | None = None,
    supervisor_review: bool = False,
    review_reason: str = "",
) -> dict[str, Any]:
    best = max((_number(row.get("global_rescue_score")) for row in scored if row.get("performance_data_valid")), default=None)
    selected = _number(winner.get("global_rescue_score")) if winner else None
    return {
        "lead": dict(lead),
        "queue": queue,
        "step": step,
        "policy_category": lead.get("policy_category"),
        "scored": scored,
        "hard_excluded": excluded,
        "guardrail_excluded": [row for row in excluded if row.get("guardrail_exclusion_reason")],
        "winner": dict(winner) if winner else None,
        "winner_user_id": _uid(winner) if winner else "",
        "winner_name": winner.get("executive") if winner else "",
        "winner_category": WINNER_CATEGORY if winner else (NO_ELIGIBLE_JPC_RESCUER if queue == "JPC" else NO_ELIGIBLE_RESCUER),
        "selected_base_score": selected,
        "selected_effective_score": _number(winner.get("effective_selection_score")) if winner else None,
        "best_available_base_score": best,
        "selection_regret": max(0.0, best - selected) if best is not None and selected is not None else None,
        "selection_reason": reason,
        "winner_p50_effective": winner.get("p50_effective") if winner else None,
        "winner_adjusted_sla_rate": winner.get("adjusted_sla_rate") if winner else None,
        "winner_performance_confidence": winner.get("performance_confidence") if winner else "",
        "guardrail_applied": bool(guardrail_reason),
        "guardrail_reason": guardrail_reason,
        "guardrail_excluded_count": sum(1 for row in excluded if row.get("guardrail_exclusion_reason")),
        "jpc_target_share": dict(jpc_target_share or {}),
        "requires_supervisor_review": supervisor_review,
        "review_reason": review_reason,
    }


def _rm_selection(
    lead: Mapping[str, Any],
    state: Mapping[str, Mapping[str, float]],
    rm_history: list[str],
    *,
    scenario: str,
    low_policy: str,
    team: Mapping[str, Any],
    params: Any,
) -> dict[str, Any]:
    scored, excluded = _score_shared(
        lead,
        lead.get("rm_candidates", []),
        state,
        team_sla_rate=team["sla_compliance_rate"],
        team_attention_rate=team["attention_rate"],
        team_p50_average=team["team_p50_average"],
        team_p90_average=team["team_p90_average"],
        params=params,
        dynamic=True,
    )
    ordered = _order_scored([row for row in scored if row.get("performance_data_valid")], "dynamic_rescue_score", params.tie_threshold)
    best_non_low = max((_number(row.get("dynamic_rescue_score")) for row in scored if row.get("performance_data_valid") and row.get("performance_confidence") != "LOW"), default=None)
    guarded: list[dict[str, Any]] = []
    winner: dict[str, Any] | None = None
    applied_reason = ""
    for raw in ordered:
        row = dict(raw)
        if low_policy == L1_LOW_NEEDS_PLUS_5 and row.get("performance_confidence") == "LOW" and best_non_low is not None and _number(row.get("dynamic_rescue_score")) < best_non_low + 5.0:
            row["low_sample_exclusion_reason"] = "LOW_REQUIRES_PLUS_5_OVER_BEST_NON_LOW"
            guarded.append(row)
            continue
        reason = _guardrail_violation(row, scored, rm_history, scenario=scenario)
        if reason:
            row["guardrail_exclusion_reason"] = reason
            guarded.append(row)
            applied_reason = applied_reason or reason
            continue
        winner = row
        break
    exclusions = excluded + guarded
    if winner:
        winner["effective_selection_score"] = winner.get("dynamic_rescue_score")
        result = _decision_row(lead, queue="RM", step=0, scored=scored, excluded=exclusions, winner=winner, reason=POLICY_VERSION if scenario == R0_NO_GUARDRAIL and low_policy == L1_LOW_NEEDS_PLUS_5 else "rm_guardrail_or_low_policy_adjusted", guardrail_reason=applied_reason)
    else:
        result = _decision_row(lead, queue="RM", step=0, scored=scored, excluded=exclusions, winner=None, reason=NO_ELIGIBLE_RESCUER)
    return result


def _jpc_selection(
    lead: Mapping[str, Any],
    state: Mapping[str, Mapping[str, float]],
    jpc_counts: Counter[str],
    total_jpc: int,
    targets: Mapping[str, float],
    *,
    team: Mapping[str, Any],
    params: Any,
) -> dict[str, Any]:
    scored, excluded = _score_shared(
        lead,
        lead.get("jpc_candidates", []),
        state,
        team_sla_rate=team["sla_compliance_rate"],
        team_attention_rate=team["attention_rate"],
        team_p50_average=team["team_p50_average"],
        team_p90_average=team["team_p90_average"],
        params=params,
        dynamic=False,
    )
    available = [row for row in scored if row.get("performance_data_valid")]
    winner: dict[str, Any] | None = None
    if available:
        total_before = sum(jpc_counts.values())
        minimum = int(ceil(total_jpc * 0.30)) if total_jpc else 0
        maximum = int(total_jpc * 0.70) if total_jpc else 0
        ranked: list[tuple[bool, float, float, str, dict[str, Any]]] = []
        for raw in available:
            row = dict(raw)
            user_id = _uid(row)
            projected_counts = dict(jpc_counts)
            projected_counts[user_id] = projected_counts.get(user_id, 0) + 1
            remaining_after = max(0, total_jpc - total_before - 1)
            feasible = all(
                projected_counts.get(candidate_id, 0) <= maximum
                and projected_counts.get(candidate_id, 0) + remaining_after >= minimum
                for candidate_id in {_uid(candidate) for candidate in available}
            ) if len(available) >= 2 else True
            projected_share = projected_counts[user_id] / (total_before + 1)
            ranked.append((feasible, _number(targets.get(user_id), 0.5) - projected_share, _number(row.get("global_rescue_score")), user_id, row))
        feasible_ranked = [row for row in ranked if row[0]] or ranked
        feasible_ranked.sort(key=lambda row: (-row[1], -row[2], row[3]))
        winner = feasible_ranked[0][4]
        winner["effective_selection_score"] = winner.get("global_rescue_score")
    reason = "j3_target_deviation_with_score_tiebreak" if winner else NO_ELIGIBLE_JPC_RESCUER
    return _decision_row(lead, queue="JPC", step=0, scored=scored, excluded=excluded, winner=winner, reason=reason, jpc_target_share=targets)


def simulate_combined(
    leads: Iterable[Mapping[str, Any]],
    *,
    rm_policy: str,
    jpc_targets: Mapping[str, float],
    team: Mapping[str, Any],
    params: Any,
) -> dict[str, Any]:
    source = sorted((deepcopy(dict(lead)) for lead in leads), key=lead_sort_key)
    state = _state_for(source)
    rm_history: list[str] = []
    jpc_counts: Counter[str] = Counter()
    total_jpc = sum(1 for lead in source if lead.get("policy_category") == REGION_JPC_MARIA_HERNAN)
    decisions: list[dict[str, Any]] = []
    for step, lead in enumerate(source, start=1):
        assignment_number = int(_number(lead.get("automatic_reassignment_number", lead.get("assignment_number", 0))))
        if assignment_number >= MAX_AUTOMATIC_REASSIGNMENTS:
            row = _decision_row(
                lead,
                queue="RM" if lead.get("policy_category") == RM_GLOBAL_RESCUE else "JPC",
                step=step,
                scored=[],
                excluded=[],
                winner=None,
                reason=SUPERVISOR_REVIEW_REQUIRED,
                supervisor_review=True,
                review_reason="MAX_AUTOMATIC_SLA_REASSIGNMENTS_REACHED",
                jpc_target_share=jpc_targets if lead.get("policy_category") == REGION_JPC_MARIA_HERNAN else None,
            )
            decisions.append(row)
            continue
        if lead.get("policy_category") == RM_GLOBAL_RESCUE:
            row = _rm_selection(lead, state, rm_history, scenario=rm_policy, low_policy=L1_LOW_NEEDS_PLUS_5, team=team, params=params)
        elif lead.get("policy_category") == REGION_JPC_MARIA_HERNAN:
            row = _jpc_selection(lead, state, jpc_counts, total_jpc, jpc_targets, team=team, params=params)
        else:
            continue
        row["step"] = step
        decisions.append(row)
        winner = row.get("winner")
        if winner:
            _update_state(state, winner)
            if row["queue"] == "RM":
                rm_history.append(_uid(winner))
            else:
                jpc_counts[_uid(winner)] += 1
    return {"decisions": decisions, "final_state": state, "rm_policy": rm_policy, "jpc_counts": dict(jpc_counts)}


def intervention_count(decisions: Iterable[Mapping[str, Any]]) -> int:
    return sum(
        1
        for row in decisions
        if row.get("guardrail_applied")
        or any(item.get("guardrail_exclusion_reason") for item in row.get("guardrail_excluded", []))
    )


def sequence_summary(decisions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(decisions)
    winners = [row for row in rows if row.get("winner_user_id")]
    counts = Counter(_uid(row.get("winner") or {"user_id": row.get("winner_user_id")}) for row in winners)
    total = len(winners)
    shares = [count / total for count in counts.values()] if total else []
    ordered = sorted(counts.values(), reverse=True)
    consecutive = 0
    current = ""
    streak = 0
    for row in rows:
        winner = str(row.get("winner_user_id") or "")
        if not winner:
            current = ""
            streak = 0
            continue
        streak = streak + 1 if winner == current else 1
        consecutive = max(consecutive, streak)
        current = winner
    regrets = [_number(row.get("selection_regret")) for row in winners if row.get("selection_regret") is not None]
    selected_scores = [_number(row.get("selected_base_score")) for row in winners if row.get("selected_base_score") is not None]
    low_winners = Counter(
        str(row.get("winner_name") or row.get("winner_user_id") or "")
        for row in winners
        if row.get("winner_performance_confidence") == "LOW"
    )
    return {
        "total": total,
        "coverage": total / len(rows) if rows else 0.0,
        "no_winner": len(rows) - total,
        "top1": ordered[0] / total if ordered and total else 0.0,
        "hhi": sum((count / total) ** 2 for count in counts.values()) if total else 0.0,
        "receivers": len(counts),
        "max_consecutive": consecutive,
        "score_average": sum(selected_scores) / len(selected_scores) if selected_scores else 0.0,
        "regret_average": sum(regrets) / len(regrets) if regrets else 0.0,
        "regret_p90": quantile(regrets, 0.90) or 0.0,
        "distribution": dict(counts),
        "low_winner_distribution": dict(low_winners),
        "interventions": intervention_count(rows),
        "intervention_rate": intervention_count(rows) / len(rows) if rows else 0.0,
        "supervisor_review": sum(1 for row in rows if row.get("requires_supervisor_review")),
    }


def aggregate_replicates(replicates: list[dict[str, Any]]) -> dict[str, Any]:
    def values(key: str) -> list[float]:
        return [_number(row.get(key)) for row in replicates]

    return {
        "replicates": len(replicates),
        "top1_avg": sum(values("top1")) / len(replicates) if replicates else 0.0,
        "top1_p90": quantile(values("top1"), 0.90) or 0.0,
        "hhi_avg": sum(values("hhi")) / len(replicates) if replicates else 0.0,
        "hhi_p90": quantile(values("hhi"), 0.90) or 0.0,
        "max_consecutive_avg": sum(values("max_consecutive")) / len(replicates) if replicates else 0.0,
        "max_consecutive_p95": quantile(values("max_consecutive"), 0.95) or 0.0,
        "receivers_avg": sum(values("receivers")) / len(replicates) if replicates else 0.0,
        "score_average": sum(values("score_average")) / len(replicates) if replicates else 0.0,
        "regret_average": sum(values("regret_average")) / len(replicates) if replicates else 0.0,
        "regret_p90": quantile(values("regret_average"), 0.90) or 0.0,
        "interventions_avg": sum(values("interventions")) / len(replicates) if replicates else 0.0,
        "interventions_pct": sum(values("intervention_rate")) / len(replicates) if replicates else 0.0,
        "coverage_avg": sum(values("coverage")) / len(replicates) if replicates else 0.0,
        "coverage_min": min(values("coverage"), default=0.0),
        "no_winner_avg": sum(values("no_winner")) / len(replicates) if replicates else 0.0,
        "supervisor_review_avg": sum(values("supervisor_review")) / len(replicates) if replicates else 0.0,
    }


def build_rm_long_run(
    pool: Iterable[Mapping[str, Any]],
    *,
    team: Mapping[str, Any],
    params: Any,
    seed: int = BOOTSTRAP_SEED,
    replicates: int = BOOTSTRAP_REPLICATES,
) -> tuple[list[dict[str, Any]], dict[tuple[str, int], dict[str, Any]]]:
    source = list(pool)
    rng = random.Random(seed)
    raw: dict[tuple[str, int], list[dict[str, Any]]] = {(policy, size): [] for policy in RM_LONG_RUN_POLICIES for size in RM_LONG_RUN_SIZES}
    for replicate in range(1, replicates + 1):
        sampled = bootstrap_sequence(source, max(RM_LONG_RUN_SIZES), rng, f"rm-r{replicate:03d}")
        for size in RM_LONG_RUN_SIZES:
            sequence = sampled[:size]
            for policy in RM_LONG_RUN_POLICIES:
                result = simulate_rm_guardrail(
                    sequence,
                    scenario=policy,
                    low_policy=L1_LOW_NEEDS_PLUS_5,
                    team_sla_rate=team["sla_compliance_rate"],
                    team_attention_rate=team["attention_rate"],
                    team_p50_average=team["team_p50_average"],
                    team_p90_average=team["team_p90_average"],
                    params=params,
                )
                raw[(policy, size)].append(sequence_summary(result["decisions"]))
    rows: list[dict[str, Any]] = []
    aggregate: dict[tuple[str, int], dict[str, Any]] = {}
    for policy in RM_LONG_RUN_POLICIES:
        for size in RM_LONG_RUN_SIZES:
            summary = aggregate_replicates(raw[(policy, size)])
            summary.update({"policy": policy, "sequence_size": size, "seed": seed})
            aggregate[(policy, size)] = summary
            rows.append(summary)
    return rows, aggregate


def rm_acceptance(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    by_policy = {str(row["policy"]): [row for row in rows if row["policy"] == policy and int(row["sequence_size"]) in {200, 500}] for policy in RM_LONG_RUN_POLICIES}
    result: dict[str, Any] = {}
    for policy, policy_rows in by_policy.items():
        failures: list[str] = []
        for row in policy_rows:
            n = int(row["sequence_size"])
            if _number(row.get("coverage_min")) < 1.0:
                failures.append(f"N{n}:coverage")
            if _number(row.get("top1_avg")) > 0.45:
                failures.append(f"N{n}:top1")
            if _number(row.get("hhi_avg")) > 0.35:
                failures.append(f"N{n}:hhi")
            if _number(row.get("max_consecutive_p95")) > 10:
                failures.append(f"N{n}:max_consecutive")
            if _number(row.get("regret_average")) > 5:
                failures.append(f"N{n}:regret")
            if _number(row.get("receivers_avg")) < 4:
                failures.append(f"N{n}:receivers")
        result[policy] = {"acceptable": not failures, "failures": failures}
    return result


def select_rm_policy(rows: Iterable[Mapping[str, Any]], acceptance: Mapping[str, Any]) -> str | None:
    candidates = [policy for policy in RM_CANDIDATE_POLICIES if acceptance.get(policy, {}).get("acceptable")]
    if not candidates:
        return None
    metrics = {policy: [row for row in rows if row["policy"] == policy and int(row["sequence_size"]) in {200, 500}] for policy in candidates}
    complexity = {R1_CONSECUTIVE: 1, R2_ROLLING_SHARE: 1, R3_COMBINED: 2}
    def key(policy: str) -> tuple[float, float, float, float, int]:
        selected = metrics[policy]
        return (
            sum(_number(row.get("regret_average")) for row in selected) / len(selected),
            sum(_number(row.get("top1_avg")) for row in selected) / len(selected),
            sum(_number(row.get("hhi_avg")) for row in selected) / len(selected),
            sum(_number(row.get("interventions_pct")) for row in selected) / len(selected),
            complexity[policy],
        )
    return min(candidates, key=key)


def _both_eligible_jpc(leads: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for lead in leads:
        valid, _ = filter_global_candidates(lead, lead.get("jpc_candidates", []))
        identities = {_candidate_identity(row) for row in valid}
        if {MARIA_NAME, HERNAN_NAME}.issubset(identities):
            output.append(dict(lead))
    return output


def build_jpc_long_run(
    leads: Iterable[Mapping[str, Any]],
    *,
    team: Mapping[str, Any],
    params: Any,
    targets: Mapping[str, float],
    seed: int = BOOTSTRAP_SEED,
) -> list[dict[str, Any]]:
    pool = _both_eligible_jpc(leads)
    rng = random.Random(seed + 17)
    rows: list[dict[str, Any]] = []
    for size in JPC_LONG_RUN_SIZES:
        sequence = bootstrap_sequence(pool, size, rng, f"jpc-n{size}")
        first = simulate_jpc_strategy(
            sequence,
            strategy=J3_PERFORMANCE_WEIGHTED_SHARE,
            share_targets=targets,
            team_sla_rate=team["sla_compliance_rate"],
            team_attention_rate=team["attention_rate"],
            team_p50_average=team["team_p50_average"],
            team_p90_average=team["team_p90_average"],
            params=params,
        )
        second = simulate_jpc_strategy(
            sequence,
            strategy=J3_PERFORMANCE_WEIGHTED_SHARE,
            share_targets=targets,
            team_sla_rate=team["sla_compliance_rate"],
            team_attention_rate=team["attention_rate"],
            team_p50_average=team["team_p50_average"],
            team_p90_average=team["team_p90_average"],
            params=params,
        )
        first_summary = sequence_summary(first["decisions"])
        second_ids = [row.get("winner_user_id") for row in second["decisions"]]
        first_ids = [row.get("winner_user_id") for row in first["decisions"]]
        maria_id = next((_uid(row) for lead in sequence for row in lead.get("jpc_candidates", []) if _candidate_identity(row) == MARIA_NAME), "")
        hernan_id = next((_uid(row) for lead in sequence for row in lead.get("jpc_candidates", []) if _candidate_identity(row) == HERNAN_NAME), "")
        maria_count = sum(1 for row in first["decisions"] if row.get("winner_user_id") == maria_id)
        hernan_count = sum(1 for row in first["decisions"] if row.get("winner_user_id") == hernan_id)
        maria_share = maria_count / size if size else 0.0
        hernan_share = hernan_count / size if size else 0.0
        passed = (
            0.30 <= maria_share <= 0.35
            and 0.65 <= hernan_share <= 0.70
            and first_summary["max_consecutive"] <= 5
            and first_summary["coverage"] == 1.0
            and first_ids == second_ids
        )
        rows.append({
            "sequence_size": size,
            "maria_count": maria_count,
            "hernan_count": hernan_count,
            "maria_share": maria_share,
            "hernan_share": hernan_share,
            "max_consecutive": first_summary["max_consecutive"],
            "coverage": first_summary["coverage"],
            "deterministic": "PASS" if first_ids == second_ids else "FAIL",
            "eligible_profile_count": len(pool),
            "pass": "PASS" if passed else "FAIL",
        })
    return rows


def _branch_sequence(
    rm_pool: list[Mapping[str, Any]],
    jpc_pool: list[Mapping[str, Any]],
    size: int,
    rng: random.Random,
    replica: int,
) -> list[dict[str, Any]]:
    rm_count = int(round(size * len(rm_pool) / (len(rm_pool) + len(jpc_pool)))) if rm_pool and jpc_pool else (size if rm_pool else 0)
    jpc_count = size - rm_count
    rows = bootstrap_sequence(rm_pool, rm_count, rng, f"combined-r{replica:03d}-rm") + bootstrap_sequence(jpc_pool, jpc_count, rng, f"combined-r{replica:03d}-jpc")
    rng.shuffle(rows)
    for index, row in enumerate(rows, start=1):
        row["lead_id"] = f"combined-r{replica:03d}-lead-{index:04d}"
        row["assignment_cycle_id"] = f"combined-r{replica:03d}-cycle-{index:04d}"
    return rows


def build_combined_long_run(
    leads: Iterable[Mapping[str, Any]],
    *,
    rm_policy: str,
    targets: Mapping[str, float],
    team: Mapping[str, Any],
    params: Any,
    seed: int = BOOTSTRAP_SEED,
    replicates: int = BOOTSTRAP_REPLICATES,
) -> tuple[list[dict[str, Any]], dict[tuple[int, int], list[dict[str, Any]]]]:
    source = list(leads)
    rm_pool = [row for row in source if row.get("policy_category") == RM_GLOBAL_RESCUE]
    jpc_pool = [row for row in source if row.get("policy_category") == REGION_JPC_MARIA_HERNAN]
    maria_id = next((_uid(candidate) for lead in jpc_pool for candidate in lead.get("jpc_candidates", []) if _candidate_identity(candidate) == MARIA_NAME), "")
    hernan_id = next((_uid(candidate) for lead in jpc_pool for candidate in lead.get("jpc_candidates", []) if _candidate_identity(candidate) == HERNAN_NAME), "")
    raw: dict[int, list[dict[str, Any]]] = {size: [] for size in COMBINED_SIZES}
    rngs = {replica: random.Random(seed + replica) for replica in range(1, replicates + 1)}
    for replica in range(1, replicates + 1):
        rng = rngs[replica]
        for size in COMBINED_SIZES:
            sequence = _branch_sequence(rm_pool, jpc_pool, size, rng, replica)
            result = simulate_combined(sequence, rm_policy=rm_policy, jpc_targets=targets, team=team, params=params)
            metrics = sequence_summary(result["decisions"])
            rm_rows = [row for row in result["decisions"] if row.get("queue") == "RM"]
            jpc_rows = [row for row in result["decisions"] if row.get("queue") == "JPC"]
            metrics["rm"] = sequence_summary(rm_rows)
            metrics["jpc"] = sequence_summary(jpc_rows)
            metrics["jpc_target_maria"] = targets.get(maria_id, 0.0)
            metrics["jpc_target_hernan"] = targets.get(hernan_id, 0.0)
            metrics["jpc_share_maria"] = metrics["jpc"]["distribution"].get(maria_id, 0) / metrics["jpc"]["total"] if metrics["jpc"]["total"] else 0.0
            metrics["jpc_share_hernan"] = metrics["jpc"]["distribution"].get(hernan_id, 0) / metrics["jpc"]["total"] if metrics["jpc"]["total"] else 0.0
            raw[size].append(metrics)
    rows = []
    aggregate: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for size in COMBINED_SIZES:
        values = raw[size]
        total = aggregate_replicates(values)
        rm = aggregate_replicates([row["rm"] for row in values])
        jpc = aggregate_replicates([row["jpc"] for row in values])
        row = {
            "sequence_size": size,
            "replicates": replicates,
            "seed_base": seed,
            "rm_policy": rm_policy,
            "coverage": total["coverage_avg"],
            "top1": total["top1_avg"],
            "hhi": total["hhi_avg"],
            "receivers": total["receivers_avg"],
            "max_consecutive": total["max_consecutive_avg"],
            "supervisor_review": total["supervisor_review_avg"],
            "guardrail_interventions": total["interventions_avg"],
            "rm_coverage": rm["coverage_avg"],
            "jpc_coverage": jpc["coverage_avg"],
            "jpc_no_winner": jpc["no_winner_avg"],
            "jpc_share_maria": sum(row["jpc_share_maria"] for row in values) / len(values),
            "jpc_share_hernan": sum(row["jpc_share_hernan"] for row in values) / len(values),
            "jpc_target_maria": values[0]["jpc_target_maria"] if values else 0.0,
            "jpc_target_hernan": values[0]["jpc_target_hernan"] if values else 0.0,
            "distribution": dict(Counter({user_id: sum(row["distribution"].get(user_id, 0) for row in values) for user_id in set().union(*(row["distribution"].keys() for row in values))})),
            "low_winners": dict(Counter({user_id: sum(row["low_winner_distribution"].get(user_id, 0) for row in values) for user_id in set().union(*(row["low_winner_distribution"].keys() for row in values))})),
        }
        rows.append(row)
        aggregate[(size, replicates)] = values
    return rows, aggregate


def apply_stress_availability(leads: Iterable[Mapping[str, Any]], scenario: str) -> list[dict[str, Any]]:
    delta = 20 if scenario == "S2" else 50 if scenario == "S3" else 0
    unavailable = set()
    if scenario in {"S1", "S5"}:
        unavailable.add(HERNAN_NAME)
    if scenario in {"S4", "S5"}:
        unavailable.add(MARIA_NAME)
    output = []
    for raw in leads:
        lead = deepcopy(dict(raw))
        for key in ("rm_candidates", "jpc_candidates"):
            adjusted = []
            for candidate in lead.get(key, []):
                row = dict(candidate)
                identity = _candidate_identity(row)
                if identity in unavailable:
                    row["active"] = False
                if delta and identity == HERNAN_NAME:
                    row["open_current_policy"] = _number(row.get("open_current_policy")) + delta
                adjusted.append(row)
            lead[key] = adjusted
        output.append(lead)
    return output


def build_stress_rows(
    leads: Iterable[Mapping[str, Any]],
    *,
    rm_policy: str,
    targets: Mapping[str, float],
    team: Mapping[str, Any],
    params: Any,
) -> list[dict[str, Any]]:
    rows = []
    for scenario in ("S0", "S1", "S2", "S3", "S4", "S5"):
        result = simulate_combined(apply_stress_availability(leads, scenario), rm_policy=rm_policy, jpc_targets=targets, team=team, params=params)
        summary = sequence_summary(result["decisions"])
        rm_rows = [row for row in result["decisions"] if row.get("queue") == "RM"]
        jpc_rows = [row for row in result["decisions"] if row.get("queue") == "JPC"]
        rm = sequence_summary(rm_rows)
        jpc = sequence_summary(jpc_rows)
        rows.append({
            "scenario": scenario,
            "rm_coverage": rm["coverage"],
            "jpc_coverage": jpc["coverage"],
            "coverage": summary["coverage"],
            "top1": summary["top1"],
            "hhi": summary["hhi"],
            "distribution": summary["distribution"],
            "jpc_no_winner": jpc["no_winner"],
            "supervisor_review": summary["supervisor_review"],
            "guardrail_interventions": summary["interventions"],
        })
    return rows


def contract_validation() -> dict[str, Any]:
    fields = list(build_decision(
        lead_id="contract-lead",
        current_assignment_cycle_id="contract-cycle",
        previous_owner_user_id="owner-a",
        policy_branch=RM_GLOBAL_RESCUE,
        candidate_user_ids=("user-b", "user-c"),
        selected_user_id="user-b",
        selected_score=70.0,
        selection_reason="highest_effective_score",
        performance_confidence="HIGH",
        assignment_number=0,
        excluded_previous_owners=("owner-a",),
        management_evidence_checked_at="2026-09-10T12:00:00Z",
        sla_breached_at="2026-09-10T11:00:00Z",
        evaluated_at="2026-09-10T12:00:01Z",
        policy_version=POLICY_VERSION,
        candidate_scores_snapshot=({"user_id": "user-b", "score": 70.0},),
        selection_rule="RM_POLICY_V1",
        guardrail_applied=False,
        guardrail_reason="",
        jpc_target_share=None,
        previous_owner_user_ids=("owner-a",),
        automatic_reassignment_number=0,
        cycle_version="cycle-version-1",
    ).to_dict().keys())
    first = generate_decision_id("lead", "cycle", POLICY_VERSION)
    same = generate_decision_id("lead", "cycle", POLICY_VERSION)
    new_cycle = generate_decision_id("lead", "cycle-2", POLICY_VERSION)
    snapshot = {
        "lead_id": "lead",
        "assignment_cycle_id": "cycle",
        "owner_user_id": "owner-a",
        "human_management_detected": False,
        "management_evidence_version": 1,
        "management_evidence_fingerprint": "fp-1",
    }
    current_management = {**snapshot, "human_management_detected": True, "management_evidence_version": 2, "management_evidence_fingerprint": "fp-2"}
    current_cycle = {**snapshot, "assignment_cycle_id": "cycle-2"}
    base = {
        **snapshot,
        "cycle_open": True,
        "lead_open": True,
        "sla_expired": True,
        "selected_user_active": True,
        "selected_user_eligible": True,
        "automatic_reassignment_number": 0,
        "policy_version": POLICY_VERSION,
    }
    mutations = {
        "same_lead": ({**base, "lead_id": "other"}, ABORT_LEAD_CHANGED),
        "same_cycle": (current_cycle, ABORT_CYCLE_CHANGED),
        "same_owner": ({**base, "owner_user_id": "other"}, ABORT_OWNER_CHANGED),
        "cycle_open": ({**base, "cycle_open": False}, ABORT_CYCLE_CLOSED),
        "lead_open": ({**base, "lead_open": False}, ABORT_LEAD_CLOSED),
        "sla_expired": ({**base, "sla_expired": False}, ABORT_SLA_NOT_EXPIRED),
        "no_management": ({**base, "human_management_detected": True}, ABORT_MANAGEMENT_DETECTED),
        "selected_active": ({**base, "selected_user_active": False}, ABORT_SELECTED_USER_INACTIVE),
        "selected_eligible": ({**base, "selected_user_eligible": False}, ABORT_SELECTED_USER_INELIGIBLE),
        "assignment_limit": ({**base, "automatic_reassignment_number": 2}, ABORT_ASSIGNMENT_LIMIT_REACHED),
        "decision_unused": (base, OK_TO_PERSIST),
        "decision_used": (base, ABORT_DECISION_ALREADY_USED),
        "policy_version": ({**base, "policy_version": "other"}, ABORT_POLICY_VERSION_CHANGED),
    }
    statuses = {}
    for name, (current, expected) in mutations.items():
        statuses[name] = pretransaction_revalidation_status(snapshot, current, selected_user_id="user-b", expected_policy_version=POLICY_VERSION, decision_id_used=name == "decision_used") == expected
    return {
        "fields": fields,
        "fields_count": len(fields),
        "fields_complete": len(fields) >= 26,
        "decision_id_deterministic": first == same and first != new_cycle,
        "management_race": management_race_status(snapshot, current_management),
        "cycle_race": cycle_race_status(snapshot, current_cycle),
        "combined_race": decision_pre_persist_status(snapshot, current_management),
        "preconditions": statuses,
        "preconditions_pass": all(statuses.values()),
        "max2": reassignment_limit_status(2)["status"] == SUPERVISOR_REVIEW_REQUIRED,
    }


def policy_parameters(rm_policy: str | None, rm_status: str) -> list[dict[str, Any]]:
    guardrail = rm_policy or "RM_POLICY_NOT_READY"
    return [
        {"policy_version": POLICY_VERSION, "parameter": "status", "value": rm_status, "locked": "candidate_only"},
        {"policy_version": POLICY_VERSION, "parameter": "eligibility", "value": "current_policy;lead_open;sla_expired;no_human_management;owner_cycle_consistent;no_data_cycle_issue", "locked": "yes"},
        {"policy_version": POLICY_VERSION, "parameter": "legacy", "value": "never_eligible", "locked": "yes"},
        {"policy_version": POLICY_VERSION, "parameter": "owner_exclusion", "value": "current_owner_excluded", "locked": "yes"},
        {"policy_version": POLICY_VERSION, "parameter": "management_protection", "value": "any_auditable_human_activity_protects_reassignment;does_not_retroactively_fix_sla_kpi", "locked": "yes"},
        {"policy_version": POLICY_VERSION, "parameter": "rm_pool", "value": "global_active_agents", "locked": "yes"},
        {"policy_version": POLICY_VERSION, "parameter": "rm_score_weights", "value": "speed=0.50;sla_adjusted=0.25;attention_adjusted=0.15;capacity=0.10", "locked": "yes"},
        {"policy_version": POLICY_VERSION, "parameter": "speed_weights", "value": "p50=0.70;p90=0.30", "locked": "yes"},
        {"policy_version": POLICY_VERSION, "parameter": "shrinkage", "value": "K=20", "locked": "yes"},
        {"policy_version": POLICY_VERSION, "parameter": "performance_window", "value": "60_days_post_cutover", "locked": "yes"},
        {"policy_version": POLICY_VERSION, "parameter": "capacity_formula", "value": "3*expired+2*unmanaged+open", "locked": "yes"},
        {"policy_version": POLICY_VERSION, "parameter": "low_sample", "value": "L1;n<10;low_score>=best_non_low+5;shrinkage_K=20", "locked": "yes"},
        {"policy_version": POLICY_VERSION, "parameter": "rm_guardrail", "value": guardrail, "locked": "candidate_only"},
        {"policy_version": POLICY_VERSION, "parameter": "jpc_policy", "value": "J3_PERFORMANCE_WEIGHTED_SHARE;derived_target;min_share=0.30;max_share=0.70", "locked": "yes"},
        {"policy_version": POLICY_VERSION, "parameter": "anti_ping_pong", "value": "previous_sla_owners_excluded", "locked": "yes"},
        {"policy_version": POLICY_VERSION, "parameter": "max_automatic_sla_reassignments", "value": "2", "locked": "yes"},
        {"policy_version": POLICY_VERSION, "parameter": "regional_policy", "value": "undefined_regional_no_winner;territory_review_no_winner", "locked": "yes"},
    ]


def transaction_preconditions() -> list[dict[str, Any]]:
    checks = [
        (1, "same_lead", "ABORT_LEAD_CHANGED"),
        (2, "same_active_cycle", "ABORT_CYCLE_CHANGED"),
        (3, "same_owner", "ABORT_OWNER_CHANGED"),
        (4, "cycle_open", "ABORT_CYCLE_CLOSED"),
        (5, "lead_open", "ABORT_LEAD_CLOSED"),
        (6, "sla_expired", "ABORT_SLA_NOT_EXPIRED"),
        (7, "no_human_management", "ABORT_MANAGEMENT_DETECTED"),
        (8, "selected_user_active", "ABORT_SELECTED_USER_INACTIVE"),
        (9, "selected_user_eligible", "ABORT_SELECTED_USER_INELIGIBLE"),
        (10, "automatic_reassignment_number<2", "ABORT_ASSIGNMENT_LIMIT_REACHED"),
        (11, "decision_id_not_used", "ABORT_DECISION_ALREADY_USED"),
        (12, "policy_version_matches", "ABORT_POLICY_VERSION_CHANGED"),
    ]
    return [{"order": order, "precondition": check, "failure_result": failure, "write_performed": "no"} for order, check, failure in checks]
