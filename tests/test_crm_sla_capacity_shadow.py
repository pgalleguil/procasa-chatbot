from __future__ import annotations

from chatbot.crm_sla_capacity_shadow import (
    CapacityScenario,
    SCENARIOS,
    scenario_capacity_reason,
    select_capacity_winner,
    simulate_capacity_scenario,
)
from chatbot.crm_sla_shadow_ranking import (
    NO_CAPACITY_CATEGORY,
    NO_DATA_CATEGORY,
    WINNER_CATEGORY,
    concentration,
)


def candidate(user_id: str, name: str | None = None, **overrides):
    value = {
        "user_id": user_id,
        "executive": name or user_id,
        "active": True,
        "role": "agente",
        "pool_certified": True,
        "territory_valid": True,
        "cycle_conflict": False,
        "data_issue": False,
        "protected_by_management": False,
        "sample_size": 10,
        "sla_compliance_rate": 0.8,
        "attention_rate": 0.9,
        "p50_first_management_business_minutes": 10.0,
        "p90_first_management_business_minutes": 20.0,
        "open_backlog": 0,
        "unmanaged_backlog": 0,
        "expired_backlog": 0,
        "open_legacy": 0,
        "unmanaged_legacy": 0,
        "expired_legacy": 0,
    }
    value.update(overrides)
    return value


def lead(**overrides):
    value = {
        "lead_id": "lead-1",
        "owner_user_id": "owner",
        "owner": "Owner",
        "temperature": "NORMAL",
        "overdue_business_minutes": 10,
        "assigned_at": "2026-09-09T12:00:00+00:00",
    }
    value.update(overrides)
    return value


def select_kwargs():
    return {
        "team_sla_rate": 0.8,
        "team_attention_rate": 0.9,
        "team_p50": 10.0,
        "team_p90": 20.0,
    }


def test_backlog_legacy_does_not_saturate_current_policy_capacity():
    row = candidate("a", open_legacy=100, unmanaged_legacy=100, expired_legacy=100)
    assert scenario_capacity_reason(row, [row], SCENARIOS[0]) is None


def test_c0_reproduces_current_hard_filter_and_c1_is_identical():
    saturated = candidate("a", expired_backlog=5, unmanaged_backlog=10)
    available = candidate("b")
    current = lead()
    c0 = select_capacity_winner(current, [saturated, available], scenario=SCENARIOS[0], **select_kwargs())
    c1 = select_capacity_winner(current, [saturated, available], scenario=SCENARIOS[1], **select_kwargs())
    assert c0["shadow_category"] == c1["shadow_category"] == WINNER_CATEGORY
    assert c0["shadow_winner"]["user_id"] == c1["shadow_winner"]["user_id"] == "b"
    assert c0["capacity_excluded"][0]["capacity_exclusion_reason"] == c1["capacity_excluded"][0]["capacity_exclusion_reason"]


def test_c2_and_c3_move_the_hard_boundaries_only():
    candidate_at_10 = candidate("a", expired_backlog=10)
    candidate_at_15 = candidate("b", expired_backlog=15)
    assert scenario_capacity_reason(candidate_at_10, [candidate_at_10], SCENARIOS[2])
    assert scenario_capacity_reason(candidate_at_10, [candidate_at_10], SCENARIOS[3]) is None
    assert scenario_capacity_reason(candidate_at_15, [candidate_at_15], SCENARIOS[3])


def test_c4_has_no_hard_barrier_but_retains_capacity_score():
    saturated = candidate("a", expired_backlog=50, unmanaged_backlog=50, open_backlog=50)
    decision = select_capacity_winner(lead(), [saturated], scenario=SCENARIOS[4], **select_kwargs())
    assert decision["shadow_category"] == WINNER_CATEGORY
    assert decision["shadow_winner"]["capacity_score"] == 100.0


def test_c5_uses_relative_pool_pressure_and_not_global_threshold():
    low = candidate("low", expired_backlog=0, unmanaged_backlog=0, open_backlog=0)
    high = candidate("high", expired_backlog=5, unmanaged_backlog=0, open_backlog=0)
    scenario = SCENARIOS[5]
    assert scenario_capacity_reason(low, [low, high], scenario) is None
    assert scenario_capacity_reason(high, [low, high], scenario)
    single_high = candidate("single", expired_backlog=50, unmanaged_backlog=50)
    assert scenario_capacity_reason(single_high, [single_high], scenario) is None


def test_c6_adds_only_the_requested_extreme_guardrail():
    row = candidate("a", expired_backlog=20)
    assert scenario_capacity_reason(row, [row], SCENARIOS[5]) is None
    assert scenario_capacity_reason(row, [row], SCENARIOS[6])


def test_sequential_capacity_recalculates_memory_load():
    a = candidate("a")
    b = candidate("b")
    leads = [lead(lead_id="1", candidates=[a, b]), lead(lead_id="2", candidates=[a, b])]
    result = simulate_capacity_scenario(leads, scenario=SCENARIOS[4], **select_kwargs())
    assert len(result["decisions"]) == 2
    assert sum(result["final_state"][user]["simulated_shadow_received"] for user in ("a", "b")) == 2
    assert a["open_backlog"] == 0


def test_capacity_simulation_is_deterministic():
    leads = [lead(lead_id="b", candidates=[candidate("a"), candidate("b")]), lead(lead_id="a", candidates=[candidate("a"), candidate("b")])]
    first = simulate_capacity_scenario(leads, scenario=SCENARIOS[5], **select_kwargs())
    second = simulate_capacity_scenario(leads, scenario=SCENARIOS[5], **select_kwargs())
    assert first["ordered_lead_ids"] == second["ordered_lead_ids"]
    assert [row["winner_user_id"] for row in first["decisions"]] == [row["winner_user_id"] for row in second["decisions"]]


def test_single_pool_candidate_can_win_only_when_not_hard_filtered():
    decision = select_capacity_winner(lead(), [candidate("a")], scenario=SCENARIOS[0], **select_kwargs())
    assert decision["shadow_category"] == WINNER_CATEGORY
    blocked = select_capacity_winner(lead(), [candidate("a", expired_backlog=5)], scenario=SCENARIOS[0], **select_kwargs())
    assert blocked["shadow_category"] == NO_CAPACITY_CATEGORY


def test_owner_current_is_never_selected():
    owner = candidate("owner", "Owner")
    alternative = candidate("alternative")
    decision = select_capacity_winner(lead(), [owner, alternative], scenario=SCENARIOS[4], **select_kwargs())
    assert decision["shadow_winner"]["user_id"] == "alternative"


def test_invalid_territory_is_not_selected():
    invalid = candidate("invalid", territory_valid=False)
    decision = select_capacity_winner(lead(), [invalid], scenario=SCENARIOS[4], **select_kwargs())
    assert decision["shadow_category"] == NO_DATA_CATEGORY
    assert decision["shadow_winner"] is None


def test_concentration_is_reported_for_capacity_scenarios():
    result = concentration([
        {"shadow_category": WINNER_CATEGORY, "winner_user_id": "a"},
        {"shadow_category": WINNER_CATEGORY, "winner_user_id": "a"},
        {"shadow_category": WINNER_CATEGORY, "winner_user_id": "b"},
    ])
    assert result["top1_share"] == 2 / 3
    assert result["hhi"] == (2 / 3) ** 2 + (1 / 3) ** 2
