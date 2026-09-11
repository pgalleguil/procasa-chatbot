"""Unit coverage for the isolated Fase 1E hybrid shadow policy."""
from __future__ import annotations

from pathlib import Path

from chatbot.crm_sla_global_rescue import BASE_WEIGHTS, score_candidates
from chatbot.crm_sla_hybrid_rescue import (
    CONFIDENCE_GUARD_CATEGORY,
    HERNAN_NAME,
    MARIA_NAME,
    NO_ELIGIBLE_JPC_RESCUER,
    PROPERTY_EXECUTIVE_UNRESOLVED,
    REGION_JPC_MARIA_HERNAN,
    REGION_REVIEW_REQUIRED,
    RM_GLOBAL_RESCUE,
    REGIONAL_POLICY_NOT_DEFINED,
    classify_policy,
    hybrid_pool,
    performance_confidence,
    select_hybrid_winner,
    simulate_hybrid,
)
from chatbot.crm_sla_global_rescue import RescueParameters


def candidate(
    user_id: str,
    name: str | None = None,
    *,
    p50: float = 20,
    p90: float = 40,
    sample: int = 20,
    sla: float = 0.8,
    attention: float = 0.8,
    open_backlog: int = 2,
    unmanaged: int = 1,
    expired: int = 0,
) -> dict:
    label = name or user_id
    return {
        "user_id": user_id,
        "executive": label,
        "executive_key": " ".join(label.lower().split()),
        "identity_key": " ".join(label.lower().split()),
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


def lead(policy: str, *, owner: str = "owner", candidates: list[dict] | None = None, lead_id: str = "lead-1") -> dict:
    rows = candidates or [candidate("owner"), candidate("a"), candidate("b")]
    return {
        "lead_id": lead_id,
        "assignment_cycle_id": f"cycle-{lead_id}",
        "policy_category": policy,
        "owner_user_id": owner,
        "owner": owner,
        "temperature": "NORMAL",
        "current_overdue_business_minutes": 10,
        "assigned_at": "2026-09-01T10:00:00+00:00",
        "canonical_region": "metropolitanasantiago",
        "rm_candidates": rows,
        "jpc_candidates": [row for row in rows if row.get("executive_key") in {MARIA_NAME, HERNAN_NAME}],
    }


def score_kwargs() -> dict:
    return {
        "team_sla_rate": 0.5,
        "team_attention_rate": 0.5,
        "team_p50_average": 30,
        "team_p90_average": 60,
        "params": RescueParameters(),
    }


def test_policy_classification_rm_jpc_undefined_and_review() -> None:
    assert classify_policy("metropolitanasantiago", region_resolved=True, property_executive_status="AMBIGUOUS", property_is_jpc=False) == RM_GLOBAL_RESCUE
    assert classify_policy("maule", region_resolved=True, property_executive_status="RESOLVED", property_is_jpc=True) == REGION_JPC_MARIA_HERNAN
    assert classify_policy("maule", region_resolved=True, property_executive_status="RESOLVED", property_is_jpc=False) == REGIONAL_POLICY_NOT_DEFINED
    assert classify_policy(None, region_resolved=False, property_executive_status="RESOLVED", property_is_jpc=True) == REGION_REVIEW_REQUIRED
    assert classify_policy("maule", region_resolved=True, property_executive_status="AMBIGUOUS", property_is_jpc=False) == PROPERTY_EXECUTIVE_UNRESOLVED


def test_rm_pool_is_global_and_jpc_pool_is_only_maria_hernan() -> None:
    rows = [candidate("1", "María Paz Galleguillos"), candidate("2", "Hernán Castro"), candidate("3", "Rocío Aliaga",)]
    assert {row["user_id"] for row in hybrid_pool(RM_GLOBAL_RESCUE, rows)} == {"1", "2", "3"}
    assert {row["user_id"] for row in hybrid_pool(REGION_JPC_MARIA_HERNAN, rows)} == {"1", "2"}


def test_jpc_owner_exclusion_leaves_only_the_other_rescuer() -> None:
    maria = candidate("maria", "María Paz Galleguillos")
    hernan = candidate("hernan", "Hernán Castro")
    result = select_hybrid_winner({"owner_user_id": "maria"}, [maria, hernan], policy_category=REGION_JPC_MARIA_HERNAN, scenario="H1_COMBINED_DYNAMIC", **score_kwargs())
    assert result["shadow_winner"]["user_id"] == "hernan"


def test_both_jpc_rescuers_unavailable_has_no_winner_and_no_third() -> None:
    result = select_hybrid_winner({"owner_user_id": "other"}, [], policy_category=REGION_JPC_MARIA_HERNAN, scenario="H1_COMBINED_DYNAMIC", **score_kwargs())
    assert result["shadow_category"] == NO_ELIGIBLE_JPC_RESCUER


def test_rm_does_not_filter_territory_or_property_executive() -> None:
    rows = [candidate("owner"), {**candidate("remote"), "same_region": "no", "same_commune": "no"}]
    result = select_hybrid_winner({"owner_user_id": "owner"}, rows, policy_category=RM_GLOBAL_RESCUE, scenario="H1_COMBINED_DYNAMIC", **score_kwargs())
    assert result["shadow_winner"]["user_id"] == "remote"
    assert result["shadow_winner"]["same_region"] == "no"


def test_legacy_protected_and_data_issue_are_excluded() -> None:
    rows = [candidate("ok"), {**candidate("legacy"), "legacy": True}, {**candidate("protected"), "protected_by_management": True}, {**candidate("issue"), "data_issue": True}]
    result = select_hybrid_winner({"owner_user_id": "none"}, rows, policy_category=RM_GLOBAL_RESCUE, scenario="H1_COMBINED_DYNAMIC", **score_kwargs())
    assert [row["user_id"] for row in result["scored"]] == ["ok"]
    assert {row["user_id"] for row in result["hard_excluded"]} == {"legacy", "protected", "issue"}


def test_confidence_bands_are_analytical_only() -> None:
    assert performance_confidence(30) == "HIGH"
    assert performance_confidence(10) == "MEDIUM"
    assert performance_confidence(9) == "LOW"


def test_confidence_guard_prevents_low_speed_only_win_over_robust_candidate() -> None:
    low = candidate("low", p50=1, p90=1, sample=3, sla=0.1, attention=0.1, open_backlog=50, unmanaged=20, expired=5)
    high = candidate("high", p50=100, p90=100, sample=30, sla=0.95, attention=0.95, open_backlog=1, unmanaged=0, expired=0)
    result = select_hybrid_winner({"owner_user_id": "owner"}, [low, high], policy_category=RM_GLOBAL_RESCUE, scenario="CONFIDENCE_GUARD", confidence_guard=True, **score_kwargs())
    assert result["shadow_winner"]["user_id"] == "high"
    assert result["shadow_category"] == CONFIDENCE_GUARD_CATEGORY


def test_balance_dynamic_shares_load_between_receivers() -> None:
    rows = [candidate("owner"), candidate("a"), candidate("b"), candidate("c")]
    leads = [lead(RM_GLOBAL_RESCUE, candidates=rows, lead_id=str(index)) for index in range(3)]
    result = simulate_hybrid(leads, mode="H1_COMBINED", scenario="H1_COMBINED_DYNAMIC", **score_kwargs())
    winners = [row["winner_user_id"] for row in result["decisions"] if row.get("winner_user_id")]
    assert len(winners) == 3
    assert len(set(winners)) == 3


def test_h0_and_h1_are_both_available_and_h1_uses_shared_state() -> None:
    rows = [candidate("owner"), candidate("a"), candidate("b")]
    maria = candidate("maria", "María Paz Galleguillos")
    hernan = candidate("hernan", "Hernán Castro")
    rm_lead = lead(RM_GLOBAL_RESCUE, candidates=rows, lead_id="rm")
    jpc_lead = lead(REGION_JPC_MARIA_HERNAN, candidates=[maria, hernan], lead_id="jpc")
    h0 = simulate_hybrid([rm_lead, jpc_lead], mode="H0_INDEPENDENT", scenario="H0_INDEPENDENT_DYNAMIC", **score_kwargs())
    h1 = simulate_hybrid([rm_lead, jpc_lead], mode="H1_COMBINED", scenario="H1_COMBINED_DYNAMIC", **score_kwargs())
    assert {row["queue"] for row in h0["decisions"]} == {"RM", "JPC"}
    assert {row["queue"] for row in h1["decisions"]} == {"COMBINED"}


def test_confidence_weight_scenarios_match_requested_values() -> None:
    from scripts.run_phase1e_crm_sla_hybrid_rescue import RM_WEIGHTS

    assert RM_WEIGHTS["W0"] == BASE_WEIGHTS
    assert RM_WEIGHTS["W1"] == {"speed": 0.45, "sla": 0.30, "attention": 0.15, "capacity": 0.10}
    assert RM_WEIGHTS["W2"] == {"speed": 0.40, "sla": 0.35, "attention": 0.15, "capacity": 0.10}


def test_absence_of_jpc_rescuers_is_not_replaced_by_a_third_person() -> None:
    maria = candidate("maria", "María Paz Galleguillos")
    hernan = candidate("hernan", "Hernán Castro")
    base = lead(REGION_JPC_MARIA_HERNAN, candidates=[maria, hernan], lead_id="jpc")
    altered = dict(base)
    altered["jpc_candidates"] = []
    result = simulate_hybrid([altered], mode="H1_COMBINED", scenario="H1_COMBINED_DYNAMIC", **score_kwargs())
    assert result["decisions"][0]["shadow_category"] == NO_ELIGIBLE_JPC_RESCUER


def test_determinism_and_no_mongo_write_apis() -> None:
    rows = [candidate("owner"), candidate("a"), candidate("b")]
    leads = [lead(RM_GLOBAL_RESCUE, candidates=rows, lead_id="x")]
    kwargs = dict(mode="H1_COMBINED", scenario="H1_COMBINED_DYNAMIC", **score_kwargs())
    first = simulate_hybrid(leads, **kwargs)
    second = simulate_hybrid(leads, **kwargs)
    assert [row.get("winner_user_id") for row in first["decisions"]] == [row.get("winner_user_id") for row in second["decisions"]]
    source = "\n".join(Path(path).read_text(encoding="utf-8") for path in ("chatbot/crm_sla_hybrid_rescue.py", "scripts/run_phase1e_crm_sla_hybrid_rescue.py"))
    assert not any(token in source for token in ("ins" + "ert_one", "upd" + "ate_one", "del" + "ete_one", "repl" + "ace_one", "bulk" + "_write"))
