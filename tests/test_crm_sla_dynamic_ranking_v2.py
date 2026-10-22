from datetime import datetime, timedelta, timezone

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
        {"is_post_cutover": True, "canonical": True, "excluded_origin": False, "assigned_at": assigned, "sla_started_at": assigned, "a_stop_at": assigned + timedelta(minutes=10), "hot_started_at": assigned, "deadline": assigned + timedelta(minutes=60), "owner_id": "a", "temperature": "HOT", "threshold": 60},
        {"is_post_cutover": True, "canonical": True, "excluded_origin": False, "assigned_at": assigned, "sla_started_at": assigned, "a_stop_at": assigned + timedelta(minutes=10), "deadline": assigned + timedelta(minutes=180), "owner_id": "a", "temperature": "NORMAL", "threshold": 180},
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



def test_shrinkage_fairness_example_5_of_5_vs_85_of_100():
    team_rate = 0.75
    assert shrink_rate(1.0, 5, team_rate, 20) == 0.80
    assert abs(shrink_rate(0.85, 100, team_rate, 20) - (100 * 0.85 + 20 * 0.75) / 120) < 1e-12
    assert shrink_rate(0.85, 100, team_rate, 20) > shrink_rate(1.0, 5, team_rate, 20)


def test_temperature_denominators_exclude_unevaluable_episodes_and_future_management():
    as_of = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
    assigned = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    records = [
        {
            "is_post_cutover": True, "canonical": True, "excluded_origin": False,
            "assigned_at": assigned, "owner_id": "a", "temperature": "HOT",
            "sla_started_at": assigned, "a_stop_at": datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
            "hot_started_at": assigned,
            "deadline": datetime(2026, 10, 21, 13, tzinfo=timezone.utc), "threshold": 100000,
        },
        {
            "is_post_cutover": True, "canonical": True, "excluded_origin": False,
            "assigned_at": assigned, "owner_id": "a", "temperature": "NORMAL",
            "sla_started_at": assigned, "deadline": datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
            "threshold": 60,
        },
        {
            "is_post_cutover": True, "canonical": True, "excluded_origin": False,
            "assigned_at": assigned, "owner_id": "a", "temperature": "HOT",
            "sla_started_at": assigned, "deadline": datetime(2026, 10, 23, 12, tzinfo=timezone.utc),
            "threshold": 60,
        },
        {
            "is_post_cutover": True, "canonical": True, "excluded_origin": False,
            "assigned_at": assigned, "owner_id": "a", "temperature": "NORMAL",
            "sla_started_at": assigned, "a_stop_at": datetime(2026, 10, 23, 12, tzinfo=timezone.utc),
            "deadline": datetime(2026, 10, 23, 12, tzinfo=timezone.utc), "threshold": 60,
        },
    ]
    owners, team = _dynamic_temperature_metrics(
        records, [{"_id": "a"}], as_of=as_of, performance_window_days=60,
    )
    assert owners["a"]["HOT"]["eligible_count"] == 2
    assert owners["a"]["HOT"]["sample_size"] == 1
    assert owners["a"]["HOT"]["excluded_non_evaluable_count"] == 1
    assert owners["a"]["HOT"]["sla_compliant_count"] == 1
    assert owners["a"]["NORMAL"]["eligible_count"] == 2
    assert owners["a"]["NORMAL"]["sample_size"] == 1
    assert owners["a"]["NORMAL"]["excluded_non_evaluable_count"] == 1
    assert owners["a"]["NORMAL"]["missed_deadline_count"] == 1
    assert team["HOT"]["sample_size"] == 1
    assert team["NORMAL"]["sample_size"] == 1


def test_dynamic_temperature_calculation_is_skipped_for_v1(monkeypatch):
    import chatbot.crm_sla_performance_snapshot as snapshot_module

    called = []
    monkeypatch.setattr(snapshot_module, "_dynamic_temperature_metrics", lambda *args, **kwargs: called.append(True))
    assert snapshot_module._dynamic_metrics_for_policy(
        [], [], as_of=datetime(2026, 10, 22, tzinfo=timezone.utc),
        performance_window_days=60, policy_version="crm_sla_reassignment_v1",
    ) is None
    assert called == []
    result = snapshot_module._dynamic_metrics_for_policy(
        [], [], as_of=datetime(2026, 10, 22, tzinfo=timezone.utc),
        performance_window_days=60, policy_version="crm_sla_reassignment_v2",
    )
    assert called == [True]
    assert result is None


def test_committed_event_loader_bounds_all_legacy_timestamp_aliases(monkeypatch):
    import asyncio
    import chatbot.crm_sla_reassignment_worker as worker

    as_of = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
    since = as_of - timedelta(days=7)
    captured = {}

    async def fake_find_many(collection, query, projection, **kwargs):
        captured.update(query=query, projection=projection, kwargs=kwargs)
        return []

    monkeypatch.setattr(worker, "_find_many", fake_find_many)

    class DummyDB:
        def __getitem__(self, name):
            return name

    asyncio.run(worker._load_committed_events(
        DummyDB(), policy_versions=("crm_sla_reassignment_v1", "crm_sla_reassignment_v2"),
        since=since, until=as_of,
    ))
    query = captured["query"]
    assert query["event_type"] == worker.COMMITTED_EVENT
    assert query["policy_version"] == {"$in": ["crm_sla_reassignment_v1", "crm_sla_reassignment_v2"]}
    assert len(query["$or"]) == 5
    for clause in query["$or"]:
        timestamp_field, bounds = next(iter(clause.items()))
        assert timestamp_field in {"reassigned_at", "commit_time", "created_at", "timestamp", "at"}
        assert bounds == {"$gte": since, "$lte": as_of}
        assert timestamp_field in captured["projection"]


def test_recent_rescue_counts_support_legacy_timestamp_names_and_apply_priority():
    from chatbot.crm_sla_reassignment_worker import _recent_rescue_counts

    as_of = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
    since = as_of - timedelta(days=7)
    rows = [
        {"target_owner_user_id": "reassigned", "reassigned_at": as_of - timedelta(days=1)},
        {"target_owner_user_id": "commit", "commit_time": as_of - timedelta(days=2)},
        {"target_owner_user_id": "created", "created_at": as_of - timedelta(days=3)},
        {"target_owner_user_id": "timestamp", "timestamp": as_of - timedelta(days=4)},
        {"target_owner_user_id": "at", "at": as_of - timedelta(days=5)},
        {"target_owner_user_id": "old", "created_at": since - timedelta(seconds=1)},
        {"target_owner_user_id": "future", "at": as_of + timedelta(seconds=1)},
        {
            "target_owner_user_id": "priority",
            "reassigned_at": since - timedelta(seconds=1),
            "commit_time": as_of - timedelta(days=1),
        },
    ]
    assert _recent_rescue_counts(rows, as_of=as_of) == {
        "reassigned": 1, "commit": 1, "created": 1, "timestamp": 1, "at": 1,
    }


def test_missing_capacity_stays_unknown_and_batch_projection_updates_load():
    from chatbot.crm_sla_reassignment_worker import _initial_candidate_state, _update_v2_state

    state = _initial_candidate_state([{
        "user_id": "unknown",
        "open_current_policy": None,
        "unmanaged_current_policy": None,
        "expired_current_policy": None,
    }])
    assert state["unknown"]["simulated_open_current"] is None
    assert state["unknown"]["simulated_unmanaged_current"] is None
    assert state["unknown"]["simulated_expired_current"] is None

    candidate = _candidate("target", p50=10, p90=20, load=1)
    projected = _initial_candidate_state([{
        "user_id": "target", "open_current_policy": 1,
        "unmanaged_current_policy": 1, "expired_current_policy": 0,
        "hot_unmanaged_backlog": 0, "recent_rescues_7d": 0,
    }])
    params = RankingParameters(max_unmanaged_backlog=2)
    first = select_v2_winner(
        {"owner_user_id": "source"}, [candidate], team=_team(),
        branch="RM_GLOBAL_RESCUE", selection_context_verified=True,
        admin_user_ids=set(), params=params,
    )
    assert first["winner"] is not None
    _update_v2_state(projected, first["winner"], source_user_id="source", temperature="HOT")
    assert projected["target"]["simulated_open_current"] == 2
    assert projected["target"]["simulated_unmanaged_current"] == 2
    assert projected["target"]["hot_unmanaged_backlog"] == 1

    next_candidate = dict(candidate)
    next_candidate.update(projected["target"])
    second = select_v2_winner(
        {"owner_user_id": "another-source"}, [next_candidate], team=_team(),
        branch="RM_GLOBAL_RESCUE", selection_context_verified=True,
        admin_user_ids=set(), params=params,
    )
    assert second["winner"] is None
    assert second["selection_reason"] == "DYNAMIC_V2_CAPACITY_LIMIT"



def test_performance_snapshot_cache_revalidates_policy_and_preserves_ttls():
    import asyncio
    from chatbot.crm_sla_performance_snapshot import PerformanceSnapshotCache

    cache = PerformanceSnapshotCache()
    start = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
    historical_calls = []
    live_calls = []

    async def fetch_historical(version):
        historical_calls.append(version)
        return {"policy_version": version, "team": {"version": version}}

    async def fetch_live(version):
        live_calls.append(version)
        return {"policy_version": "crm_sla_reassignment_v1", "capacity_metrics": {}}

    for offset, version in enumerate((
        "crm_sla_reassignment_v1",
        "crm_sla_reassignment_v2",
        "crm_sla_reassignment_v1",
    )):
        now = start + timedelta(seconds=offset)
        historical = asyncio.run(cache.get_historical(
            now=now, policy_version=version,
            builder=lambda version=version: fetch_historical(version),
        ))
        live = asyncio.run(cache.get_live_capacity(
            now=now, policy_version=version,
            builder=lambda version=version: fetch_live(version),
        ))
        assert historical["policy_version"] == version
        assert live["policy_version"] == version
        assert datetime.fromisoformat(historical["expires_at"]) == now + timedelta(seconds=300)
        assert datetime.fromisoformat(live["expires_at"]) == now + timedelta(seconds=60)

    asyncio.run(cache.get_historical(
        now=start + timedelta(seconds=4), policy_version="crm_sla_reassignment_v1",
        builder=lambda: fetch_historical("unexpected"),
    ))
    asyncio.run(cache.get_live_capacity(
        now=start + timedelta(seconds=4), policy_version="crm_sla_reassignment_v1",
        builder=lambda: fetch_live("unexpected"),
    ))
    assert historical_calls == [
        "crm_sla_reassignment_v1",
        "crm_sla_reassignment_v2",
        "crm_sla_reassignment_v1",
    ]
    assert live_calls == [
        "crm_sla_reassignment_v1",
        "crm_sla_reassignment_v2",
        "crm_sla_reassignment_v1",
    ]
    assert cache.stats()["historical_ttl_seconds"] == 300
    assert cache.stats()["live_capacity_ttl_seconds"] == 60


def test_default_snapshot_distinguishes_verified_zero_from_incomplete_capacity(monkeypatch):
    import asyncio
    import chatbot.crm_sla_reassignment_worker as worker
    from chatbot.crm_sla_performance_snapshot import PerformanceSnapshotCache

    as_of = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)

    async def forbidden_watermark_read(*args, **kwargs):
        raise AssertionError("worker cache hits must not poll source watermarks")

    monkeypatch.setattr(worker, "read_source_watermarks", forbidden_watermark_read)

    async def historical_builder(*args, **kwargs):
        return {
            "candidates": [{
                "user_id": "quiet-agent", "open_current_policy": 91,
                "unmanaged_current_policy": 91, "expired_current_policy": 91,
            }],
            "team": {"sla_compliance_rate": 0.75},
            "users_by_id": {}, "records_by_cycle": {}, "properties": {}, "catalog": {},
            "docs_examined": 0,
        }

    for audit, expected_complete, expected_capacity in (
        ({"lead_missing_or_filtered": 0, "owner_missing": 0}, True, 0),
        ({"lead_missing_or_filtered": 1, "owner_missing": 0}, False, None),
    ):
        async def live_builder(*args, audit=audit, **kwargs):
            return {
                "policy_version": "crm_sla_reassignment_v1",
                "variant": "A1", "capacity_metrics": {}, "cycle_audit": audit, "docs_examined": 0,
            }

        monkeypatch.setattr(worker, "build_historical_performance_snapshot", historical_builder)
        monkeypatch.setattr(worker, "build_live_capacity_variant", live_builder)
        result = asyncio.run(worker._default_performance_snapshot(
            as_of, policy_version="crm_sla_reassignment_v2", db=object(),
            performance_cache=PerformanceSnapshotCache(), shadow_live_capacity=True,
        ))
        candidate = result["candidates"][0]
        assert result["capacity_snapshot_complete"] is expected_complete
        assert candidate["open_current_policy"] == expected_capacity
        assert candidate["unmanaged_current_policy"] == expected_capacity
        assert candidate["expired_current_policy"] == expected_capacity
        assert candidate["hot_unmanaged_backlog"] == expected_capacity
        assert result["live_capacity_variant"] == "A1"
        assert result["source_watermarks"] == {}

        selectable = _candidate("quiet-agent", p50=10, p90=20)
        selectable.update(candidate)
        selected = select_v2_winner(
            {"owner_user_id": "source"}, [selectable], team=_team(),
            branch="RM_GLOBAL_RESCUE", selection_context_verified=True,
            admin_user_ids=set(),
        )
        if expected_complete:
            assert selected["winner"]["user_id"] == "quiet-agent"
        else:
            assert selected["winner"] is None
            assert selected["selection_reason"] == "DYNAMIC_V2_CAPACITY_LIMIT"
