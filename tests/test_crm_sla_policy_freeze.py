"""Focused tests for Fase 1G policy-freeze analytics."""
from __future__ import annotations

from chatbot.crm_sla_global_rescue import RescueParameters
from chatbot.crm_sla_hybrid_rescue import HERNAN_NAME, MARIA_NAME, REGION_JPC_MARIA_HERNAN, RM_GLOBAL_RESCUE
from chatbot.crm_sla_hybrid_stabilization import (
    ABORT_CYCLE_CHANGED,
    ABORT_MANAGEMENT_DETECTED,
    R1_CONSECUTIVE,
    R2_ROLLING_SHARE,
    R3_COMBINED,
    SUPERVISOR_REVIEW_REQUIRED,
    build_decision,
    generate_decision_id,
    pretransaction_revalidation_status,
)
from chatbot.crm_sla_policy_freeze import (
    BOOTSTRAP_SEED,
    JPC_LONG_RUN_SIZES,
    POLICY_VERSION,
    aggregate_replicates,
    bootstrap_sequence,
    contract_validation,
    intervention_count,
    sequence_summary,
    simulate_combined,
)


def candidate(user_id: str, name: str, *, p50: float, sample: int = 30, active: bool = True) -> dict:
    identity = name.lower().replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    return {
        "user_id": user_id,
        "executive": name,
        "identity_key": identity,
        "executive_key": identity,
        "active": active,
        "role": "agente",
        "legacy": False,
        "protected_by_management": False,
        "data_issue": False,
        "closed_lead": False,
        "not_currently_expired": False,
        "sample_size": sample,
        "sla_compliance_rate": 0.8,
        "attention_rate": 0.8,
        "p50_first_management_business_minutes": p50,
        "p90_first_management_business_minutes": p50 * 2,
        "open_current_policy": 1,
        "unmanaged_current_policy": 1,
        "expired_current_policy": 0,
        "shadow_received_count": 0,
    }


def lead(index: int, branch: str = RM_GLOBAL_RESCUE) -> dict:
    maria = candidate("maria", "María Paz Galleguillos", p50=20)
    hernan = candidate("hernan", "Hernán Castro", p50=25)
    other = candidate("other", "Other", p50=35)
    return {
        "lead_id": f"lead-{index}",
        "assignment_cycle_id": f"cycle-{index}",
        "owner_user_id": "owner",
        "temperature": "NORMAL",
        "current_overdue_business_minutes": index,
        "assigned_at": f"2026-09-01T10:00:{index % 60:02d}+00:00",
        "policy_category": branch,
        "rm_candidates": [maria, hernan, other],
        "jpc_candidates": [maria, hernan],
    }


def team() -> dict:
    return {"sla_compliance_rate": 0.5, "attention_rate": 0.5, "team_p50_average": 30, "team_p90_average": 60}


def test_n500_bootstrap_is_deterministic_and_supports_requested_sizes() -> None:
    import random

    source = [lead(index) for index in range(3)]
    first = bootstrap_sequence(source, 500, random.Random(BOOTSTRAP_SEED), "test")
    second = bootstrap_sequence(source, 500, random.Random(BOOTSTRAP_SEED), "test")
    assert len(first) == 500
    assert [row["lead_id"] for row in first] == [row["lead_id"] for row in second]


def test_rm_acceptance_is_mechanical() -> None:
    passing = {"coverage": 1.0, "top1": 0.40, "hhi": 0.30, "max_consecutive": 8, "receivers": 5, "regret_average": 4, "intervention_rate": 0, "interventions": 0}
    summary = aggregate_replicates([passing] * 3)
    assert summary["coverage_min"] == 1.0
    assert summary["top1_avg"] <= 0.45
    assert summary["hhi_avg"] <= 0.35
    assert summary["max_consecutive_p95"] <= 10
    assert summary["regret_average"] <= 5
    assert summary["receivers_avg"] >= 4
    assert intervention_count([{"guardrail_applied": True}, {"guardrail_excluded": [{"guardrail_exclusion_reason": "R2"}]}]) == 2


def test_combined_uses_shared_hernan_state_and_excludes_undefined() -> None:
    params = RescueParameters()
    rows = [lead(index, RM_GLOBAL_RESCUE if index % 2 == 0 else REGION_JPC_MARIA_HERNAN) for index in range(20)]
    rows.append({**lead(999, "REGIONAL_POLICY_NOT_DEFINED")})
    result = simulate_combined(rows, rm_policy=R2_ROLLING_SHARE, jpc_targets={"maria": 0.3, "hernan": 0.7}, team=team(), params=params)
    summary = sequence_summary(result["decisions"])
    assert summary["coverage"] == 1.0
    assert all(row["policy_category"] != "REGIONAL_POLICY_NOT_DEFINED" for row in result["decisions"])
    assert result["final_state"]["hernan"]["shadow_received_count"] > 0


def test_jpc_target_sequence_shape_is_bounded() -> None:
    from chatbot.crm_sla_hybrid_stabilization import simulate_jpc_strategy, J3_PERFORMANCE_WEIGHTED_SHARE

    rows = [lead(index, REGION_JPC_MARIA_HERNAN) for index in range(250)]
    result = simulate_jpc_strategy(
        rows,
        strategy=J3_PERFORMANCE_WEIGHTED_SHARE,
        share_targets={"maria": 0.3, "hernan": 0.7},
        team_sla_rate=team()["sla_compliance_rate"],
        team_attention_rate=team()["attention_rate"],
        team_p50_average=team()["team_p50_average"],
        team_p90_average=team()["team_p90_average"],
        params=RescueParameters(),
    )
    summary = sequence_summary(result["decisions"])
    maria = sum(row.get("winner_user_id") == "maria" for row in result["decisions"]) / 250
    hernan = sum(row.get("winner_user_id") == "hernan" for row in result["decisions"]) / 250
    assert 0.30 <= maria <= 0.35
    assert 0.65 <= hernan <= 0.70
    assert summary["max_consecutive"] <= 5
    assert summary["coverage"] == 1.0


def test_stress_unavailability_does_not_create_a_third_jpc_receiver() -> None:
    from chatbot.crm_sla_policy_freeze import apply_stress_availability, build_stress_rows

    rows = [lead(index, RM_GLOBAL_RESCUE if index % 2 == 0 else REGION_JPC_MARIA_HERNAN) for index in range(10)]
    stress = build_stress_rows(rows, rm_policy=R2_ROLLING_SHARE, targets={"maria": 0.3, "hernan": 0.7}, team=team(), params=RescueParameters())
    assert len(stress) == 6
    s5 = next(row for row in stress if row["scenario"] == "S5")
    assert s5["jpc_no_winner"] == 5
    assert all(candidate["executive"] not in {"Tercer receptor"} for row in apply_stress_availability(rows, "S5") for candidate in row["jpc_candidates"])


def test_contract_has_final_fields_and_preconditions_fail_closed() -> None:
    contract = contract_validation()
    assert contract["fields_count"] >= 26
    assert contract["fields_complete"]
    assert contract["preconditions_pass"]
    assert contract["decision_id_deterministic"]
    assert contract["management_race"] == ABORT_MANAGEMENT_DETECTED
    assert contract["cycle_race"] == ABORT_CYCLE_CHANGED
    decision = build_decision(
        lead_id="lead", current_assignment_cycle_id="cycle", previous_owner_user_id="owner", policy_branch=RM_GLOBAL_RESCUE,
        candidate_user_ids=("a", "b"), policy_version=POLICY_VERSION, candidate_scores_snapshot=({"user_id": "a"},),
        selection_rule="R1", guardrail_applied=True, guardrail_reason="R1_CONSECUTIVE_LIMIT", automatic_reassignment_number=0,
        cycle_version="v1", jpc_target_share=None,
    )
    assert decision.decision_id == generate_decision_id("lead", "cycle", POLICY_VERSION)
    assert decision.to_dict()["selection_rule"] == "R1"
    base = {"lead_id": "lead", "assignment_cycle_id": "cycle", "owner_user_id": "owner", "cycle_open": True, "lead_open": True, "sla_expired": True, "human_management_detected": False, "selected_user_active": True, "selected_user_eligible": True, "automatic_reassignment_number": 0, "policy_version": POLICY_VERSION}
    assert pretransaction_revalidation_status(base, {**base, "human_management_detected": True}, selected_user_id="a", expected_policy_version=POLICY_VERSION) == ABORT_MANAGEMENT_DETECTED
    assert pretransaction_revalidation_status(base, {**base, "assignment_cycle_id": "changed"}, selected_user_id="a", expected_policy_version=POLICY_VERSION) == ABORT_CYCLE_CHANGED


def test_policy_version_and_requested_policy_constants() -> None:
    assert POLICY_VERSION == "crm_sla_reassignment_v1"
    assert JPC_LONG_RUN_SIZES == (30, 100, 250)
    assert all((R1_CONSECUTIVE, R2_ROLLING_SHARE, R3_COMBINED))
    assert SUPERVISOR_REVIEW_REQUIRED == "SUPERVISOR_REVIEW_REQUIRED"
