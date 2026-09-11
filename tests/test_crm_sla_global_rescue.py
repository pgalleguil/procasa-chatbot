"""Unit tests for the pure Fase 1D global rescue selector."""
from __future__ import annotations

from pathlib import Path

from chatbot.crm_sla_global_rescue import (
    BASE_WEIGHTS,
    GUARDRAIL_CATEGORY,
    RescueParameters,
    WINNER_CATEGORY,
    capacity_score,
    filter_global_candidates,
    score_candidates,
    select_winner,
    simulate_scenario,
    shrink_rate,
)


def candidate(
    user_id: str,
    *,
    p50: float = 20,
    p90: float = 40,
    sample: int = 20,
    sla: float = 0.8,
    attention: float = 0.9,
    open_backlog: int = 2,
    unmanaged: int = 1,
    expired: int = 0,
    same_region: str = "no",
) -> dict:
    return {
        "user_id": user_id,
        "executive": user_id,
        "active": True,
        "role": "agente",
        "sample_size": sample,
        "sla_compliance_rate": sla,
        "attention_rate": attention,
        "p50_first_management_business_minutes": p50,
        "p90_first_management_business_minutes": p90,
        "open_current_policy": open_backlog,
        "unmanaged_current_policy": unmanaged,
        "expired_current_policy": expired,
        "shadow_received_count": 0,
        "same_commune": "no",
        "same_region": same_region,
    }


def lead(*, owner: str = "owner", temperature: str = "NORMAL", overdue: float = 30, candidates: list[dict] | None = None) -> dict:
    return {
        "lead_id": f"lead-{temperature}-{overdue}-{owner}",
        "owner_user_id": owner,
        "temperature": temperature,
        "current_overdue_business_minutes": overdue,
        "assigned_at": "2026-09-01T10:00:00+00:00",
        "candidates": candidates if candidates is not None else [candidate("owner"), candidate("a"), candidate("b")],
    }


def test_global_pool_excludes_previous_owner_but_not_territory_mismatch() -> None:
    candidates = [candidate("owner"), candidate("remote", same_region="no")]
    valid, excluded = filter_global_candidates({"owner_user_id": "owner"}, candidates)
    assert [row["user_id"] for row in valid] == ["remote"]
    assert excluded[0]["excluded_reason"] == "previous_owner_excluded"


def test_global_pool_excludes_inactive_non_agent_legacy_and_protected() -> None:
    rows = [
        candidate("ok"),
        {**candidate("inactive"), "active": False},
        {**candidate("role"), "role": "supervisor"},
        {**candidate("legacy"), "legacy": True},
        {**candidate("protected"), "protected_by_management": True},
        {**candidate("issue"), "data_issue": True},
        {**candidate("closed"), "closed_lead": True},
        {**candidate("not-expired"), "not_currently_expired": True},
    ]
    valid, excluded = filter_global_candidates({"owner_user_id": "none"}, rows)
    assert [row["user_id"] for row in valid] == ["ok"]
    assert {row["user_id"] for row in excluded} == {"inactive", "role", "legacy", "protected", "issue", "closed", "not-expired"}


def test_shrinkage_uses_k_20() -> None:
    assert shrink_rate(1.0, 0, 0.5, 20) == 0.5
    assert shrink_rate(1.0, 20, 0.5, 20) == 0.75


def test_speed_score_uses_seventy_thirty_p50_p90_and_is_bounded() -> None:
    rows = [candidate("fast", p50=10, p90=20), candidate("mid", p50=20, p90=30), candidate("slow", p50=30, p90=300)]
    scored = score_candidates(
        rows,
        team_sla_rate=0.5,
        team_attention_rate=0.5,
        team_p50_average=20,
        team_p90_average=30,
    )
    by_id = {row["user_id"]: row for row in scored}
    assert by_id["fast"]["speed_score"] == 100
    assert by_id["slow"]["speed_score"] == 0
    assert 0 < by_id["mid"]["speed_score"] < 100
    assert all(0 <= row["global_rescue_score"] <= 100 for row in scored)


def test_low_sample_speed_is_imputed_from_team_average() -> None:
    rows = [candidate("reliable", p50=10, p90=20, sample=20), candidate("thin", p50=999, p90=999, sample=2)]
    scored = score_candidates(
        rows,
        team_sla_rate=0.5,
        team_attention_rate=0.5,
        team_p50_average=10,
        team_p90_average=20,
    )
    thin = next(row for row in scored if row["user_id"] == "thin")
    assert thin["speed_imputed"] == "yes"
    assert thin["p50_effective"] == 10
    assert thin["p90_effective"] == 20


def test_capacity_score_rewards_lower_pressure() -> None:
    rows = [candidate("low", open_backlog=1, unmanaged=0, expired=0), candidate("high", open_backlog=20, unmanaged=10, expired=5)]
    scores = capacity_score(rows)
    assert scores["low"] == 100
    assert scores["high"] == 0


def test_g0_has_no_guardrail_and_g1_excludes_extreme_candidate() -> None:
    rows = [candidate("owner"), candidate("extreme", expired=50), candidate("safe")]
    current_lead = {"lead_id": "x", "owner_user_id": "owner"}
    g0 = select_winner(current_lead, rows, scenario="G0_GLOBAL_NO_GUARDRAIL", team_sla_rate=.5, team_attention_rate=.5, team_p50_average=20, team_p90_average=40)
    g1 = select_winner(current_lead, rows, scenario="G1_EXTREME_GUARDRAIL", team_sla_rate=.5, team_attention_rate=.5, team_p50_average=20, team_p90_average=40)
    assert any(row["user_id"] == "extreme" for row in g0["scored"])
    assert any(row["user_id"] == "extreme" for row in g1["guardrail_excluded"])
    assert g1["shadow_category"] == WINNER_CATEGORY


def test_g2_relative_guardrail_and_g3_dynamic_penalty_are_separate() -> None:
    rows = [candidate("owner"), candidate("low", open_backlog=1), candidate("high", open_backlog=100)]
    g2 = select_winner({"owner_user_id": "owner"}, rows, scenario="G2_RELATIVE_CAPACITY", team_sla_rate=.5, team_attention_rate=.5, team_p50_average=20, team_p90_average=40)
    assert any(row["user_id"] == "high" for row in g2["guardrail_excluded"])
    g3 = select_winner({"owner_user_id": "owner"}, rows, scenario="G3_BALANCE_DYNAMIC", team_sla_rate=.5, team_attention_rate=.5, team_p50_average=20, team_p90_average=40)
    assert all(row["dynamic_penalty"] == 0 for row in g3["scored"])


def test_g3_updates_open_unmanaged_and_received_sequentially() -> None:
    rows = [candidate("owner"), candidate("a"), candidate("b"), candidate("c")]
    leads = [
        {**lead(owner="owner", temperature="NORMAL", overdue=10, candidates=rows), "lead_id": "1"},
        {**lead(owner="owner", temperature="NORMAL", overdue=9, candidates=rows), "lead_id": "2"},
        {**lead(owner="owner", temperature="NORMAL", overdue=8, candidates=rows), "lead_id": "3"},
    ]
    result = simulate_scenario(leads, scenario="G3_BALANCE_DYNAMIC", team_sla_rate=.5, team_attention_rate=.5, team_p50_average=20, team_p90_average=40)
    winners = [row["winner_user_id"] for row in result["decisions"]]
    assert len(winners) == 3
    assert len(set(winners)) == 3
    assert result["final_state"][winners[0]]["shadow_received_count"] == 1
    assert result["final_state"][winners[0]]["simulated_open_current"] == 3
    assert result["final_state"][winners[0]]["simulated_unmanaged_current"] == 2


def test_hot_leads_are_ordered_before_normal_then_overdue() -> None:
    rows = [candidate("owner"), candidate("a"), candidate("b")]
    leads = [
        {**lead(owner="owner", temperature="NORMAL", overdue=100, candidates=rows), "lead_id": "normal"},
        {**lead(owner="owner", temperature="HOT", overdue=1, candidates=rows), "lead_id": "hot"},
        {**lead(owner="owner", temperature="HOT", overdue=50, candidates=rows), "lead_id": "hot-old"},
    ]
    result = simulate_scenario(leads, scenario="G0_GLOBAL_NO_GUARDRAIL", team_sla_rate=.5, team_attention_rate=.5, team_p50_average=20, team_p90_average=40)
    assert result["ordered_lead_ids"] == ["hot-old", "hot", "normal"]


def test_tie_break_prefers_lower_p50_then_lower_user_id() -> None:
    rows = [candidate("owner"), candidate("z", p50=10, p90=20), candidate("a", p50=10, p90=20)]
    result = select_winner({"owner_user_id": "owner"}, rows, scenario="G0_GLOBAL_NO_GUARDRAIL", team_sla_rate=.5, team_attention_rate=.5, team_p50_average=20, team_p90_average=40)
    assert result["shadow_winner"]["user_id"] == "a"
    assert result["shadow_winner"]["tie_break_applied"] == "yes"


def test_absence_of_top_performer_is_a_pool_change_not_a_policy_change() -> None:
    rows = [candidate("owner"), candidate("fast", p50=1, p90=2), candidate("backup", p50=50, p90=60)]
    original = simulate_scenario([{**lead(owner="owner", candidates=rows), "lead_id": "x"}], scenario="G3_BALANCE_DYNAMIC", team_sla_rate=.5, team_attention_rate=.5, team_p50_average=20, team_p90_average=40)
    reduced = [
        {**lead(owner="owner", candidates=[row for row in rows if row["user_id"] != "fast"]), "lead_id": "x"}
    ]
    absent = simulate_scenario(reduced, scenario="G3_BALANCE_DYNAMIC", team_sla_rate=.5, team_attention_rate=.5, team_p50_average=20, team_p90_average=40)
    assert original["decisions"][0]["winner_user_id"] == "fast"
    assert absent["decisions"][0]["winner_user_id"] != "fast"


def test_selector_is_deterministic_and_does_not_reference_mongo_writes() -> None:
    rows = [candidate("owner"), candidate("a"), candidate("b")]
    leads = [{**lead(owner="owner", candidates=rows), "lead_id": "x"}]
    kwargs = dict(scenario="G3_BALANCE_DYNAMIC", team_sla_rate=.5, team_attention_rate=.5, team_p50_average=20, team_p90_average=40)
    first = simulate_scenario(leads, **kwargs)
    second = simulate_scenario(leads, **kwargs)
    assert first["ordered_lead_ids"] == second["ordered_lead_ids"]
    assert [row["winner_user_id"] for row in first["decisions"]] == [row["winner_user_id"] for row in second["decisions"]]
    source_paths = [Path("chatbot/crm_sla_global_rescue.py"), Path("scripts/run_phase1d_crm_sla_global_rescue.py")]
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)
    assert not any(token in source for token in ("insert_one", "update_one", "delete_one", "replace_one", "bulk_write"))
