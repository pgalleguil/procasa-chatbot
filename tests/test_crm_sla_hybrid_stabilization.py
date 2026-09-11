"""Unit tests for the pure Fase 1F stabilisation simulations."""
from __future__ import annotations

from pathlib import Path

from chatbot.crm_sla_global_rescue import RescueParameters
from chatbot.crm_sla_hybrid_rescue import HERNAN_NAME, MARIA_NAME
from chatbot.crm_sla_hybrid_stabilization import (
    ABORT_CYCLE_CHANGED,
    ABORT_MANAGEMENT_DETECTED,
    J0_SCORE_PURE,
    J1_ROUND_ROBIN,
    J2_PENALTIES,
    J2_SCORE_PLUS_LOAD,
    J3_PERFORMANCE_WEIGHTED_SHARE,
    J4_GAPS,
    J4_SLA_FIRST_BALANCED,
    L0_LOW_CAN_COMPETE,
    L1_LOW_NEEDS_PLUS_5,
    NO_ELIGIBLE_JPC_RESCUER,
    R0_NO_GUARDRAIL,
    R1_CONSECUTIVE,
    R2_ROLLING_SHARE,
    R3_COMBINED,
    SUPERVISOR_REVIEW_REQUIRED,
    build_decision,
    cycle_race_status,
    decision_pre_persist_status,
    derive_jpc_share_targets,
    generate_decision_id,
    management_race_status,
    reassignment_limit_status,
    sequence_metrics,
    simulate_anti_ping_pong,
    simulate_jpc_strategy,
    simulate_rm_guardrail,
)


def candidate(
    user_id: str,
    name: str,
    *,
    p50: float = 20,
    p90: float = 40,
    sample: int = 30,
    sla: float = 0.8,
    attention: float = 0.8,
    open_backlog: int = 2,
    unmanaged: int = 1,
    expired: int = 0,
) -> dict:
    return {
        "user_id": user_id,
        "executive": name,
        "identity_key": " ".join(name.lower().replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u").split()),
        "executive_key": " ".join(name.lower().split()),
        "active": True,
        "role": "agente",
        "legacy": False,
        "protected_by_management": False,
        "data_issue": False,
        "closed_lead": False,
        "not_currently_expired": False,
        "sample_size": sample,
        "sla_compliance_rate": sla,
        "attention_rate": attention,
        "p50_first_management_business_minutes": p50,
        "p90_first_management_business_minutes": p90,
        "open_current_policy": open_backlog,
        "unmanaged_current_policy": unmanaged,
        "expired_current_policy": expired,
        "shadow_received_count": 0,
    }


def kwargs() -> dict:
    return {
        "team_sla_rate": 0.5,
        "team_attention_rate": 0.5,
        "team_p50_average": 30,
        "team_p90_average": 60,
        "params": RescueParameters(),
    }


def jpc_lead(index: int, owner: str = "other", rows: list[dict] | None = None) -> dict:
    maria = candidate("maria", "María Paz Galleguillos", p50=100, p90=200, sla=0.55, attention=0.6)
    hernan = candidate("hernan", "Hernán Castro", p50=20, p90=40, sla=0.95, attention=0.95)
    return {
        "lead_id": f"jpc-{index}",
        "assignment_cycle_id": f"cycle-{index}",
        "owner_user_id": owner,
        "temperature": "NORMAL",
        "current_overdue_business_minutes": index,
        "assigned_at": f"2026-09-01T10:00:{index:02d}+00:00",
        "jpc_candidates": rows or [maria, hernan],
    }


def rm_lead(index: int, owner: str = "owner") -> dict:
    rows = [
        candidate("owner", "Owner", p50=30, p90=60),
        candidate("a", "A", p50=20, p90=40, sla=0.85, attention=0.85),
        candidate("b", "B", p50=25, p90=45, sla=0.8, attention=0.8),
        candidate("c", "C", p50=30, p90=50, sla=0.75, attention=0.75),
    ]
    return {
        "lead_id": f"rm-{index}",
        "assignment_cycle_id": f"rm-cycle-{index}",
        "owner_user_id": owner,
        "temperature": "NORMAL",
        "current_overdue_business_minutes": index,
        "assigned_at": f"2026-09-01T10:00:{index:02d}+00:00",
        "rm_candidates": rows,
    }


def test_j0_is_score_benchmark_and_j1_balances() -> None:
    leads = [jpc_lead(index) for index in range(6)]
    j0 = simulate_jpc_strategy(leads, strategy=J0_SCORE_PURE, **kwargs())
    j1 = simulate_jpc_strategy(leads, strategy=J1_ROUND_ROBIN, **kwargs())
    assert sequence_metrics(j0["decisions"], maria_id="maria", hernan_id="hernan")["hernan_received"] == 6
    assert sequence_metrics(j1["decisions"], maria_id="maria", hernan_id="hernan")["maria_received"] == 3
    assert sequence_metrics(j1["decisions"], maria_id="maria", hernan_id="hernan")["hernan_received"] == 3


def test_j2_runs_all_requested_penalties_and_increases_balance() -> None:
    leads = [jpc_lead(index) for index in range(12)]
    results = {
        p: simulate_jpc_strategy(leads, strategy=J2_SCORE_PLUS_LOAD, parameter=p, **kwargs())
        for p in J2_PENALTIES
    }
    assert tuple(results) == J2_PENALTIES
    assert all(sum(result["counts"].values()) == 12 for result in results.values())
    assert results[10]["counts"].get("maria", 0) >= results[2]["counts"].get("maria", 0)


def test_j3_targets_are_score_derived_bounded_and_deterministic() -> None:
    rows = [candidate("maria", "María Paz Galleguillos", p50=100, p90=200, sla=0.5), candidate("hernan", "Hernán Castro", p50=20, p90=40, sla=0.95)]
    target = derive_jpc_share_targets(rows, **kwargs())
    assert 0.30 <= target["targets"]["maria"] <= 0.70
    assert 0.30 <= target["targets"]["hernan"] <= 0.70
    leads = [jpc_lead(index) for index in range(10)]
    first = simulate_jpc_strategy(leads, strategy=J3_PERFORMANCE_WEIGHTED_SHARE, share_targets=target["targets"], **kwargs())
    second = simulate_jpc_strategy(leads, strategy=J3_PERFORMANCE_WEIGHTED_SHARE, share_targets=target["targets"], **kwargs())
    assert [row["winner_user_id"] for row in first["decisions"]] == [row["winner_user_id"] for row in second["decisions"]]


def test_j4_respects_each_assignment_gap() -> None:
    leads = [jpc_lead(index) for index in range(20)]
    for gap in J4_GAPS:
        result = simulate_jpc_strategy(leads, strategy=J4_SLA_FIRST_BALANCED, parameter=gap, **kwargs())
        metric = sequence_metrics(result["decisions"], maria_id="maria", hernan_id="hernan")
        assert metric["load_gap"] <= gap


def test_regret_is_zero_for_j0_and_positive_when_forced_to_weak_candidate() -> None:
    leads = [jpc_lead(index) for index in range(4)]
    results = {
        J0_SCORE_PURE: simulate_jpc_strategy(leads, strategy=J0_SCORE_PURE, **kwargs()),
        J1_ROUND_ROBIN: simulate_jpc_strategy(leads, strategy=J1_ROUND_ROBIN, **kwargs()),
    }
    from chatbot.crm_sla_hybrid_stabilization import annotate_loss_vs_j0
    annotate_loss_vs_j0(results)
    assert sequence_metrics(results[J0_SCORE_PURE]["decisions"], maria_id="maria", hernan_id="hernan")["regret_average"] == 0
    assert sequence_metrics(results[J1_ROUND_ROBIN]["decisions"], maria_id="maria", hernan_id="hernan")["regret_average"] > 0


def test_rm_guardrails_are_available_and_owner_never_wins() -> None:
    leads = [rm_lead(index, owner="owner") for index in range(30)]
    for scenario in (R0_NO_GUARDRAIL, R1_CONSECUTIVE, R2_ROLLING_SHARE, R3_COMBINED):
        result = simulate_rm_guardrail(leads, scenario=scenario, **kwargs())
        assert all(row["winner_user_id"] != row["lead"]["owner_user_id"] for row in result["decisions"] if row["winner_user_id"])


def test_low_sample_l1_does_not_allow_low_without_plus_five() -> None:
    low = candidate("low", "Pablo Galleguillos", p50=1, p90=1, sample=3, sla=0.5, attention=0.5, open_backlog=0, unmanaged=0)
    robust = candidate("robust", "Robusto", p50=30.1, p90=60.2, sample=30, sla=0.3, attention=0.3, open_backlog=0, unmanaged=0)
    third = candidate("third", "Third", p50=100, p90=200, sample=30, sla=0.5, attention=0.5, open_backlog=0, unmanaged=0)
    lead = {**rm_lead(1), "rm_candidates": [low, robust, third], "owner_user_id": "none"}
    l0 = simulate_rm_guardrail([lead], scenario=R0_NO_GUARDRAIL, low_policy=L0_LOW_CAN_COMPETE, **kwargs())
    l1 = simulate_rm_guardrail([lead], scenario=R0_NO_GUARDRAIL, low_policy=L1_LOW_NEEDS_PLUS_5, **kwargs())
    assert l0["decisions"][0]["winner_user_id"] == "low"
    assert l1["decisions"][0]["winner_user_id"] == "robust"


def test_anti_ping_pong_and_jpc_two_person_limit() -> None:
    rm = simulate_anti_ping_pong([{"user_id": "a"}, {"user_id": "b"}, {"user_id": "c"}], owners=("a", "b"), policy_branch="RM_POLICY_V1")
    jpc = simulate_anti_ping_pong([{"user_id": "maria"}, {"user_id": "hernan"}], owners=("maria", "hernan"), policy_branch="REGION_JPC_MARIA_HERNAN")
    assert rm["path"] == ["b", "c"]
    assert jpc["path"][1] == NO_ELIGIBLE_JPC_RESCUER


def test_reassignment_limit_and_decision_id() -> None:
    assert reassignment_limit_status(0)["status"] == "AUTO_ELIGIBLE"
    assert reassignment_limit_status(1)["status"] == "AUTO_ELIGIBLE"
    assert reassignment_limit_status(2)["status"] == SUPERVISOR_REVIEW_REQUIRED
    assert generate_decision_id("lead", "cycle", "v1") == generate_decision_id("lead", "cycle", "v1")
    assert generate_decision_id("lead", "cycle", "v1") != generate_decision_id("lead", "cycle-2", "v1")


def test_contract_races_and_required_fields() -> None:
    snapshot = {"assignment_cycle_id": "cycle", "owner_user_id": "a", "human_management_detected": False, "management_evidence_version": 1, "management_evidence_fingerprint": "one"}
    management = {**snapshot, "human_management_detected": True, "management_evidence_version": 2, "management_evidence_fingerprint": "two"}
    cycle = {**snapshot, "assignment_cycle_id": "cycle-2"}
    assert management_race_status(snapshot, management) == ABORT_MANAGEMENT_DETECTED
    assert cycle_race_status(snapshot, cycle) == ABORT_CYCLE_CHANGED
    assert decision_pre_persist_status(snapshot, management) == ABORT_MANAGEMENT_DETECTED
    decision = build_decision(lead_id="lead", current_assignment_cycle_id="cycle", previous_owner_user_id="a", policy_branch="RM_POLICY_V1", candidate_user_ids=("b", "c"), policy_version="v1")
    assert decision.decision_id
    assert decision.to_dict()["policy_branch"] == "RM_POLICY_V1"
    assert len(decision.to_dict()) >= 18


def test_analytical_modules_have_no_mongo_write_api_names() -> None:
    paths = [Path("chatbot/crm_sla_hybrid_stabilization.py"), Path("scripts/run_phase1f_crm_sla_hybrid_stabilization.py")]
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    tokens = ("ins" + "ert_one", "upd" + "ate_one", "del" + "ete_one", "repl" + "ace_one", "bulk_" + "write")
    assert not any(token in source for token in tokens)
