from __future__ import annotations

from chatbot.crm_sla_shadow_ranking import (
    BASE_WEIGHTS,
    NO_CAPACITY_CATEGORY,
    NO_DATA_CATEGORY,
    TIE_CATEGORY,
    WINNER_CATEGORY,
    ShadowParameters,
    apply_candidate_scores,
    capacity_score,
    concentration,
    impute_speed_metrics,
    select_shadow_winner,
    simulate_sequential,
    shrink_rate,
    saturation_reason,
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
        "open_backlog": 1,
        "unmanaged_backlog": 1,
        "expired_backlog": 0,
        "simulated_shadow_received": 0,
    }
    value.update(overrides)
    return value


def lead(**overrides):
    value = {
        "lead_id": "lead-1",
        "owner_user_id": "owner",
        "owner": "Owner",
        "temperature": "NORMAL",
        "overdue_business_minutes": 20,
        "assigned_at": "2026-09-09T12:00:00+00:00",
    }
    value.update(overrides)
    return value


def score_kwargs(candidates):
    return {
        "candidates": candidates,
        "team_sla_rate": 0.8,
        "team_attention_rate": 0.9,
        "team_p50": 10.0,
        "team_p90": 20.0,
    }


def select_kwargs():
    return {
        "team_sla_rate": 0.8,
        "team_attention_rate": 0.9,
        "team_p50": 10.0,
        "team_p90": 20.0,
    }


def test_shrinkage_uses_configured_k():
    assert shrink_rate(1.0, 3, 0.5, 20) == 0.5652173913043478


def test_speed_is_lower_is_better_and_bounded():
    fast, slow = candidate("fast", p50_first_management_business_minutes=5, p90_first_management_business_minutes=10), candidate("slow", p50_first_management_business_minutes=30, p90_first_management_business_minutes=60)
    scored = apply_candidate_scores(**score_kwargs([fast, slow]))
    assert scored[0]["speed_score"] > scored[1]["speed_score"]
    assert all(0 <= row["speed_score"] <= 100 for row in scored)


def test_low_sample_speed_is_imputed_and_marked():
    rows = impute_speed_metrics([candidate("low", sample_size=2, p50_first_management_business_minutes=None, p90_first_management_business_minutes=None)], team_p50=12, team_p90=24)
    assert rows[0]["metric_imputed"] == "yes"
    assert rows[0]["p50_effective"] == 12
    assert rows[0]["p90_effective"] == 24
    assert rows[0]["speed_imputation_source"] == "team"


def test_capacity_lower_pressure_wins_and_equal_is_100():
    rows = [candidate("low", open_backlog=1, unmanaged_backlog=0, expired_backlog=0), candidate("high", open_backlog=5, unmanaged_backlog=2, expired_backlog=1)]
    scores = capacity_score(rows)
    assert scores["low"] > scores["high"]
    assert capacity_score([candidate("only")])["only"] == 100


def test_saturation_is_inclusive_at_both_hard_limits():
    params = ShadowParameters(max_expired_backlog=5, max_unmanaged_backlog=10)
    assert saturation_reason(candidate("a", expired_backlog=5), params)
    assert saturation_reason(candidate("b", unmanaged_backlog=10), params)
    assert saturation_reason(candidate("c", expired_backlog=4, unmanaged_backlog=9), params) is None


def test_current_owner_is_hard_excluded():
    decision = select_shadow_winner(lead(owner_user_id="owner", owner="Owner"), [candidate("owner", "Owner"), candidate("other", "Other")], **select_kwargs())
    assert decision["shadow_category"] == WINNER_CATEGORY
    assert decision["shadow_winner"]["user_id"] == "other"
    assert any(row.get("excluded_reason") == "current_owner" for row in decision["hard_excluded"])


def test_territory_and_protection_constraints_are_hard_exclusions():
    protected = candidate("protected", protected_by_management=True)
    out_of_territory = candidate("outside", territory_valid=False)
    decision = select_shadow_winner(lead(), [protected, out_of_territory], **select_kwargs())
    assert decision["shadow_category"] == NO_DATA_CATEGORY
    reasons = {row["excluded_reason"] for row in decision["hard_excluded"]}
    assert "protected_by_management" in reasons
    assert "territory_invalid" in reasons


def test_data_failure_returns_no_valid_performance_data():
    invalid = candidate("invalid", p50_first_management_business_minutes=None, p90_first_management_business_minutes=None, sample_size=0)
    decision = select_shadow_winner(lead(), [invalid], team_sla_rate=0, team_attention_rate=0, team_p50=None, team_p90=None)
    assert decision["shadow_category"] == NO_DATA_CATEGORY


def test_base_score_uses_only_four_requested_components():
    rows = apply_candidate_scores(**score_kwargs([candidate("a")]))
    assert rows[0]["performance_score"] == 90.0
    assert set(BASE_WEIGHTS) == {"sla", "attention", "speed", "capacity"}


def test_tie_break_uses_stable_user_id_last():
    first = candidate("a", open_backlog=1, unmanaged_backlog=1)
    second = candidate("b", open_backlog=1, unmanaged_backlog=1)
    decision = select_shadow_winner(lead(), [second, first], **select_kwargs())
    assert decision["shadow_category"] == WINNER_CATEGORY
    assert decision["shadow_winner"]["user_id"] == "a"
    assert decision["shadow_winner"]["tie_break_applied"] == "yes"


def test_tie_without_stable_ids_is_unresolved():
    first = candidate("", executive="A")
    second = candidate("", executive="B")
    decision = select_shadow_winner(lead(), [first, second], **select_kwargs())
    assert decision["shadow_category"] == TIE_CATEGORY
    assert decision["shadow_winner"] is None


def test_sequential_simulation_increments_only_memory_state_of_winner():
    first = candidate("a", open_backlog=0, unmanaged_backlog=0)
    second = candidate("b", open_backlog=10, unmanaged_backlog=10)
    leads = [lead(lead_id="hot", temperature="HOT", overdue_business_minutes=100, candidates=[first, second]), lead(lead_id="normal", candidates=[first, second])]
    result = simulate_sequential(leads, team_sla_rate=0.8, team_attention_rate=0.9, team_p50=10, team_p90=20)
    assert len(result["decisions"]) == 2
    assert result["final_state"]["a"]["simulated_shadow_received"] == 2
    assert result["final_state"]["b"]["simulated_shadow_received"] == 0
    assert first["open_backlog"] == 0


def test_sequential_order_is_hot_then_overdue_then_oldest_then_id():
    rows = [lead(lead_id="normal", temperature="NORMAL", overdue_business_minutes=999), lead(lead_id="hot2", temperature="HOT", overdue_business_minutes=20), lead(lead_id="hot1", temperature="HOT", overdue_business_minutes=20)]
    for row in rows:
        row["candidates"] = [candidate("a")]
    result = simulate_sequential(rows, team_sla_rate=0.8, team_attention_rate=0.9, team_p50=10, team_p90=20)
    assert result["ordered_lead_ids"] == ["hot1", "hot2", "normal"]


def test_sequential_is_deterministic_for_same_input():
    leads = [lead(lead_id="b", candidates=[candidate("a"), candidate("b")]), lead(lead_id="a", candidates=[candidate("a"), candidate("b")])]
    kwargs = {"team_sla_rate": 0.8, "team_attention_rate": 0.9, "team_p50": 10, "team_p90": 20}
    first = simulate_sequential(leads, **kwargs)
    second = simulate_sequential(leads, **kwargs)
    assert first["ordered_lead_ids"] == second["ordered_lead_ids"]
    assert [row["winner_user_id"] for row in first["decisions"]] == [row["winner_user_id"] for row in second["decisions"]]


def test_concentration_reports_top_shares_and_hhi():
    decisions = [{"shadow_category": WINNER_CATEGORY, "winner_user_id": "a"}, {"shadow_category": WINNER_CATEGORY, "winner_user_id": "a"}, {"shadow_category": WINNER_CATEGORY, "winner_user_id": "b"}]
    result = concentration(decisions)
    assert result["top1_share"] == 2 / 3
    assert result["top2_share"] == 1.0
    assert result["hhi"] == (2 / 3) ** 2 + (1 / 3) ** 2


def test_empty_candidate_pool_is_not_a_winner():
    decision = select_shadow_winner(lead(), [], team_sla_rate=0.8, team_attention_rate=0.9, team_p50=10, team_p90=20)
    assert decision["shadow_winner"] is None
    assert decision["shadow_category"] == NO_DATA_CATEGORY


def test_single_candidate_can_win_without_second_place():
    decision = select_shadow_winner(lead(), [candidate("a")], **select_kwargs())
    assert decision["shadow_category"] == WINNER_CATEGORY
    assert decision["second_place"] is None
    assert decision["score_difference"] is None
