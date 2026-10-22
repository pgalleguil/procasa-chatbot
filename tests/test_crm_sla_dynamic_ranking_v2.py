from datetime import datetime, timezone

from chatbot.crm_sla_dynamic_ranking_v2 import (
    BASE_WEIGHTS,
    NO_CANDIDATE_CATEGORY,
    RankingParameters,
    filter_candidates,
    select_v2_winner,
    shrink_rate,
)
from chatbot.crm_sla_performance_snapshot import _dynamic_temperature_metrics


def _team():
    return {
        "sla_compliance_rate": 0.75,
        "attention_rate": 0.8,
        "team_p50_average": 40,
        "team_p90_average": 120,
        "temperature_mix": {"HOT": 20, "NORMAL": 80},
        "by_temperature": {
            "HOT": {"sample_size": 20, "sla_compliance_rate": 0.6, "attention_rate": 0.7, "p50": 20, "p90": 60},
            "NORMAL": {"sample_size": 80, "sla_compliance_rate": 0.8, "attention_rate": 0.85, "p50": 40, "p90": 120},
        },
    }


def _candidate(user_id, *, p50, p90, sla=0.9, attention=0.9, sample=40, load=0, region="yes"):
    return {
        "user_id": user_id,
        "active": True,
        "role": "agente",
        "same_region": region,
        "sample_size": sample,
        "by_temperature": {
            "HOT": {"sample_size": sample // 2, "sla_compliance_rate": sla, "attention_rate": attention, "p50": p50, "p90": p90},
            "NORMAL": {"sample_size": sample // 2, "sla_compliance_rate": sla, "attention_rate": attention, "p50": p50, "p90": p90},
        },
        "simulated_open_current": load,
        "simulated_unmanaged_current": load,
        "simulated_expired_current": 0,
        "open_current_policy": load,
        "unmanaged_current_policy": load,
        "expired_current_policy": 0,
        "hot_unmanaged_backlog": 0,
        "recent_rescues_7d": 0,
    }


def test_v2_weights_are_normalized_and_exclude_unverified_commercial_metric():
    assert BASE_WEIGHTS == {"sla": 6 / 18, "management": 5 / 18, "speed": 4 / 18, "capacity": 3 / 18}
    assert sum(BASE_WEIGHTS.values()) == 1
    assert "commercial" not in BASE_WEIGHTS


def test_shrinkage_uses_personal_sample_and_team_prior():
    assert shrink_rate(0.0, 0, 0.8, 20) == 0.8
    assert shrink_rate(1.0, 20, 0.5, 20) == 0.75


def test_jpc_filter_requires_certified_region_and_excludes_owner_history_and_admin():
    lead = {"owner_user_id": "owner", "previous_owner_user_ids": ["previous"]}
    rows = [
        _candidate("eligible", p50=10, p90=30),
        _candidate("wrong-region", p50=10, p90=30, region="no"),
        _candidate("owner", p50=10, p90=30),
        _candidate("previous", p50=10, p90=30),
        _candidate("admin", p50=10, p90=30),
    ]
    eligible, excluded = filter_candidates(
        lead, rows, branch="REGION_JPC_MARIA_HERNAN",
        selection_context_verified=True, admin_user_ids={"admin"},
    )
    assert [row["user_id"] for row in eligible] == ["eligible"]
    reasons = {row["user_id"]: row["excluded_reason"] for row in excluded}
    assert "territory_not_certified" in reasons["wrong-region"]
    assert "current_owner_excluded" in reasons["owner"]
    assert "previous_owner_excluded" in reasons["previous"]
    assert "administrator_excluded" in reasons["admin"]


def test_v2_ranks_with_auditable_scores_and_fails_closed_without_context():
    candidates = [
        _candidate("fast", p50=10, p90=25, sla=0.95, attention=0.95),
        _candidate("slow", p50=70, p90=180, sla=0.55, attention=0.6, load=4),
    ]
    result = select_v2_winner(
        {"owner_user_id": "source", "previous_owner_user_ids": []}, candidates,
        team=_team(), branch="RM_GLOBAL_RESCUE", selection_context_verified=True,
        admin_user_ids=set(),
    )
    assert result["winner"]["user_id"] == "fast"
    assert result["policy_version"] == "crm_sla_reassignment_v2"
    assert result["selection_rule"] == "dynamic_performance_v2"
    assert set(result["winner"]["weights_applied"]) == {"sla", "management", "speed", "capacity"}
    assert abs(sum(result["winner"]["weights_applied"].values()) - 1) < 1e-9

    blocked = select_v2_winner(
        {"owner_user_id": "source"}, candidates, team=_team(),
        branch="RM_GLOBAL_RESCUE", selection_context_verified=False,
        admin_user_ids=set(),
    )
    assert blocked["winner"] is None
    assert blocked["selection_reason"] != NO_CANDIDATE_CATEGORY


def test_capacity_snapshot_incomplete_candidates_are_not_selected():
    candidate = _candidate("missing-capacity", p50=10, p90=20)
    candidate.pop("simulated_open_current")
    candidate.pop("open_current_policy")
    result = select_v2_winner(
        {"owner_user_id": "source"}, [candidate], team=_team(),
        branch="RM_GLOBAL_RESCUE", selection_context_verified=True,
        admin_user_ids=set(),
    )
    assert result["winner"] is None
    assert result["selection_reason"] == "DYNAMIC_V2_CAPACITY_LIMIT"


def test_temperature_snapshot_preserves_hot_and_normal_sample_sizes():
    as_of = datetime(2026, 10, 22, tzinfo=timezone.utc)
    assigned = datetime(2026, 10, 20, tzinfo=timezone.utc)
    records = [
        {"is_post_cutover": True, "canonical": True, "excluded_origin": False, "assigned_at": assigned, "owner_id": "a", "temperature": "HOT", "threshold": 60},
        {"is_post_cutover": True, "canonical": True, "excluded_origin": False, "assigned_at": assigned, "owner_id": "a", "temperature": "NORMAL", "threshold": 180},
        {"is_post_cutover": True, "canonical": False, "excluded_origin": False, "assigned_at": assigned, "owner_id": "a", "temperature": "HOT", "threshold": 60},
    ]
    owners, team = _dynamic_temperature_metrics(
        records, [{"_id": "a"}], as_of=as_of, performance_window_days=60,
    )
    assert owners["a"]["HOT"]["sample_size"] == 1
    assert owners["a"]["NORMAL"]["sample_size"] == 1
    assert team["HOT"]["sample_size"] == 1
    assert team["NORMAL"]["sample_size"] == 1



def test_worker_defaults_to_v1_and_uses_dynamic_selector_only_for_configured_v2(monkeypatch):
    from config import Config
    from chatbot.crm_sla_global_rescue import RescueParameters
    from chatbot.crm_sla_reassignment_worker import _configured_policy_version, _policy_selection

    assert _configured_policy_version() == "crm_sla_reassignment_v1"
    monkeypatch.setattr(Config, "CRM_SLA_REASSIGNMENT_POLICY_VERSION", "crm_sla_reassignment_v2", raising=False)
    assert _configured_policy_version() == "crm_sla_reassignment_v2"

    candidates = [
        _candidate("fast", p50=10, p90=25),
        _candidate("slow", p50=70, p90=180, sla=0.55, attention=0.6, load=4),
    ]
    result = _policy_selection(
        {"owner_user_id": "source", "previous_owner_user_ids": [], "rm_candidates": candidates},
        {}, branch="RM_GLOBAL_RESCUE", state={}, rm_history=[], jpc_counts={}, jpc_total=0,
        jpc_targets={}, team=_team(), dynamic_team=_team(), params=RescueParameters(),
        policy_version="crm_sla_reassignment_v2", selection_context_verified=True,
    )
    assert result["winner"]["user_id"] == "fast"
    assert result["selection_rule"] == "dynamic_performance_v2"


def test_executor_requires_decision_version_to_match_explicit_configuration(monkeypatch):
    from config import Config
    from chatbot.crm_sla_reassignment_executor import _KnownAbort, _prevalidate_decision

    try:
        _prevalidate_decision({"policy_version": "crm_sla_reassignment_v2"})
    except _KnownAbort as exc:
        assert exc.code.value == "ABORT_POLICY_VERSION_CHANGED"
    else:
        raise AssertionError("V2 decision must be rejected while V1 is configured")

    monkeypatch.setattr(Config, "CRM_SLA_REASSIGNMENT_POLICY_VERSION", "crm_sla_reassignment_v2", raising=False)
    try:
        _prevalidate_decision({"policy_version": "crm_sla_reassignment_v1"})
    except _KnownAbort as exc:
        assert exc.code.value == "ABORT_POLICY_VERSION_CHANGED"
    else:
        raise AssertionError("V1 decision must be rejected while V2 is configured")
