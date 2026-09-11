"""Phase 2E tests: cache, live capacity, shadow state, lease and health.

All persistence tests use memory/fakes.  No production Mongo collection is
created and no test opts into the Mongo write adapters.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from chatbot.crm_sla_performance_snapshot import (
    PerformanceSnapshotCache,
    build_live_capacity_snapshot,
    cache_configuration,
    read_source_watermarks,
    snapshot_watermark_changed,
)
from chatbot.crm_sla_reassignment_shadow import (
    MANAGEMENT_AFTER_SHADOW_OUTCOME,
    SHADOW_COLLECTION,
    SHADOW_SCHEMA_VERSION,
    InMemoryShadowStore,
    apply_management_after_shadow,
    build_shadow_document,
    build_worker_heartbeat,
    deterministic_shadow_evaluation_id,
    get_sla_reassignment_worker_health,
    kill_switch_truth_table,
    management_after_shadow_bucket,
    persist_shadow_evaluation,
    reconstruct_shadow_distribution_state,
    score_margin_bucket,
    stable_shadow_document_id,
    shadow_persistence_failure_outcome,
    validate_shadow_configuration,
)
from chatbot.crm_sla_worker_lease import (
    HEARTBEAT_SECONDS,
    LEASE_SECONDS,
    LeaseSettings,
    InMemoryLeaderLease,
    acquire_leader_lease,
    interval_operational_metrics,
    lease_strategy,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)


def test_cache_cold_then_warm_hit_without_rebuild():
    cache = PerformanceSnapshotCache(historical_ttl_seconds=300, live_capacity_ttl_seconds=60)
    calls = {"historical": 0}

    async def builder():
        calls["historical"] += 1
        return {"executive_metrics": {"a": {"p50": 10}}, "docs_examined": 10}

    first = asyncio.run(cache.get_historical(now=NOW, builder=builder, policy_version="p", source_watermarks={"v": 1}))
    second = asyncio.run(cache.get_historical(now=NOW + timedelta(seconds=30), builder=builder, policy_version="p", source_watermarks={"v": 1}))
    assert first["snapshot_id"] == second["snapshot_id"]
    assert calls["historical"] == 1
    assert cache.stats()["historical_hits"] == 1


def test_cache_ttl_expiry_rebuilds():
    cache = PerformanceSnapshotCache(historical_ttl_seconds=60, live_capacity_ttl_seconds=60)
    calls = 0

    def builder():
        nonlocal calls
        calls += 1
        return {"docs_examined": calls}

    asyncio.run(cache.get_historical(now=NOW, builder=builder, policy_version="p"))
    asyncio.run(cache.get_historical(now=NOW + timedelta(seconds=60), builder=builder, policy_version="p"))
    assert calls == 2


def test_cache_watermark_change_invalidates_before_ttl():
    cache = PerformanceSnapshotCache(historical_ttl_seconds=600, live_capacity_ttl_seconds=60)
    calls = 0

    def builder():
        nonlocal calls
        calls += 1
        return {"version": calls}

    asyncio.run(cache.get_historical(now=NOW, builder=builder, policy_version="p", source_watermarks={"cycle": "1"}))
    asyncio.run(cache.get_historical(now=NOW + timedelta(seconds=1), builder=builder, policy_version="p", source_watermarks={"cycle": "2"}))
    assert calls == 2
    assert snapshot_watermark_changed({"cycle": "1"}, {"cycle": "2"}) is True


def test_historical_and_live_capacity_cache_are_independent():
    cache = PerformanceSnapshotCache(historical_ttl_seconds=600, live_capacity_ttl_seconds=60)
    calls = {"h": 0, "l": 0}

    def historical():
        calls["h"] += 1
        return {"docs_examined": 100}

    def live():
        calls["l"] += 1
        return {"capacity_metrics": {"a": {"open": calls["l"]}}}

    asyncio.run(cache.get_historical(now=NOW, builder=historical, policy_version="p", source_watermarks={"v": 1}))
    asyncio.run(cache.get_live_capacity(now=NOW, builder=live, policy_version="p", source_watermarks={"v": 1}))
    asyncio.run(cache.get_historical(now=NOW + timedelta(seconds=61), builder=historical, policy_version="p", source_watermarks={"v": 1}))
    asyncio.run(cache.get_live_capacity(now=NOW + timedelta(seconds=61), builder=live, policy_version="p", source_watermarks={"v": 1}))
    assert calls == {"h": 1, "l": 2}


def test_cache_configuration_exposes_measured_ttl_candidates():
    config = cache_configuration()
    assert config["historical_ttl_candidates_seconds"] == [60, 120, 300, 600]
    assert config["recommended_historical_ttl_seconds"] == 300
    assert config["live_capacity_ttl_seconds"] == 60


def test_missing_watermarks_are_explicit_and_read_only():
    class EmptyCollection:
        def find(self, query, projection=None):
            return self
        def sort(self, value):
            return self
        def limit(self, value):
            return self
        async def to_list(self, length=None):
            return []

    class DB:
        def __getitem__(self, key):
            return EmptyCollection()

    result = asyncio.run(read_source_watermarks(DB()))
    assert result["supported"]["max_cycle_updated_at"] is False
    assert result["watermark_version"]


def test_lease_settings_match_global_leader_contract():
    settings = LeaseSettings()
    assert settings.validate()["valid"] is True
    assert settings.duration_seconds == LEASE_SECONDS == 120
    assert settings.heartbeat_seconds == HEARTBEAT_SECONDS == 30
    assert lease_strategy()["strategy"] == "GLOBAL_LEADER_LEASE"


def test_lease_acquisition_and_same_holder_renewal():
    lease = InMemoryLeaderLease()
    acquired = lease.acquire("A", now=NOW)
    renewed = lease.renew("A", now=NOW + timedelta(seconds=30))
    assert acquired["status"] == "ACQUIRED"
    assert renewed["status"] == "RENEWED"
    assert renewed["lease"]["holder_id"] == "A"


def test_lease_contention_does_not_allow_second_holder():
    lease = InMemoryLeaderLease()
    lease.acquire("A", now=NOW)
    result = lease.acquire("B", now=NOW + timedelta(seconds=60))
    assert result["status"] == "LEASE_NOT_HELD"
    assert result["reason"] == "CONTENTION"


def test_lease_expiry_allows_takeover_after_expiry():
    lease = InMemoryLeaderLease()
    lease.acquire("A", now=NOW)
    before = lease.acquire("B", now=NOW + timedelta(seconds=120 - 1))
    after = lease.acquire("B", now=NOW + timedelta(seconds=120))
    assert before["status"] == "LEASE_NOT_HELD"
    assert after["status"] == "ACQUIRED"
    assert after["takeover"] is True


def test_lease_mongo_adapter_is_fail_closed_by_default():
    class ForbiddenDB:
        def __getitem__(self, key):
            raise AssertionError("must not touch db when lease writes are disabled")

    result = asyncio.run(acquire_leader_lease(ForbiddenDB(), holder_id="A", now=NOW))
    assert result["status"] == "LEASE_NOT_HELD"
    assert result["reason"] == "LEASE_WRITES_DISABLED"


def base_evaluation(**overrides):
    row = {
        "lead_id": "lead-1", "source_cycle_id": "cycle-1", "evaluated_at": NOW.isoformat(),
        "breach_at": (NOW - timedelta(minutes=10)).isoformat(), "cutover": (NOW - timedelta(hours=1)).isoformat(),
        "policy_version": "crm_sla_reassignment_v1", "branch": "RM_GLOBAL_RESCUE",
        "eligible": True, "candidate_ids": ["a", "b"], "selected_user_id": "a", "selected_score": 80.0,
        "second_user_id": "b", "score_margin": 5.0, "assignment_number": 0,
        "decision_id": "decision-1", "would_execute": True, "performance_snapshot_version": "perf-1",
        "distribution_state_version": "dist-1",
    }
    row.update(overrides)
    return row


def test_shadow_document_has_stable_key_and_no_pii_fields():
    doc = build_shadow_document(base_evaluation(), worker_instance_id="instance-a", batch_id="batch-1")
    assert doc["_id"] == stable_shadow_document_id("cycle-1", "crm_sla_reassignment_v1")
    assert doc["shadow_evaluation_id"] == deterministic_shadow_evaluation_id("cycle-1", "crm_sla_reassignment_v1", "dist-1")
    assert doc["schema_version"] == SHADOW_SCHEMA_VERSION
    assert "phone" not in doc and "email" not in doc and "message" not in doc


def test_shadow_store_counts_same_cycle_once():
    store = InMemoryShadowStore()
    first = store.observe(build_shadow_document(base_evaluation()), now=NOW)
    second = store.observe(build_shadow_document(base_evaluation(evaluated_at=(NOW + timedelta(minutes=1)).isoformat())), now=NOW + timedelta(minutes=1))
    assert first["shadow_assignment_count"] == 1
    assert second["shadow_assignment_count"] == 1
    assert second["evaluation_count"] == 2


def test_shadow_late_management_preserves_prior_would_execute():
    store = InMemoryShadowStore()
    first = store.observe(build_shadow_document(base_evaluation()), now=NOW)
    late = apply_management_after_shadow(first, NOW + timedelta(minutes=3), now=NOW + timedelta(minutes=3))
    second = store.observe(build_shadow_document(base_evaluation(evaluated_at=(NOW + timedelta(minutes=3)).isoformat(), would_execute=False, eligible=False, exclusion_reason="PROTECTED_BY_MANAGEMENT", selected_user_id=None), human_management_at=NOW + timedelta(minutes=3)), now=NOW + timedelta(minutes=3))
    assert late["management_after_shadow_bucket"] == "<=5 min"
    assert second["shadow_assignment_count"] == 1
    assert second["eligibility_outcome"] == MANAGEMENT_AFTER_SHADOW_OUTCOME


def test_shadow_state_reconstruction_ignores_non_would_execute_and_wrong_version():
    docs = [
        build_shadow_document(base_evaluation(source_cycle_id="cycle-1", selected_user_id="a")),
        build_shadow_document(base_evaluation(source_cycle_id="cycle-2", selected_user_id="b", would_execute=False, eligible=False, exclusion_reason="NO_WINNER")),
        build_shadow_document(base_evaluation(source_cycle_id="cycle-3", selected_user_id="b", policy_version="other")),
    ]
    state = reconstruct_shadow_distribution_state(docs)
    assert state["shadow_assignment_count"] == 1
    assert state["rm_history"] == ["a"]


def test_shadow_dry_run_adapter_never_writes_by_default():
    class ForbiddenDB:
        def __getitem__(self, key):
            raise AssertionError("dry run must not access collection")

    result = asyncio.run(persist_shadow_evaluation(ForbiddenDB(), build_shadow_document(base_evaluation())))
    assert result["status"] == "DRY_RUN"
    assert result["collection"] == SHADOW_COLLECTION
    assert result["write_performed"] is False


def test_shadow_storage_failure_is_fail_closed():
    result = shadow_persistence_failure_outcome(RuntimeError("storage down"))
    assert result["status"] == "SHADOW_STORAGE_FAILURE_FAIL_CLOSED"
    assert result["executor_allowed"] is False


def test_interval_metrics_are_deterministic_and_include_hourly_read_cost():
    result = interval_operational_metrics(interval_seconds=60)
    assert result["expected_detection_lag_seconds"] == 30
    assert result["max_nominal_detection_lag_seconds"] == 60
    assert result["estimated_read_operations_per_hour"] == 900


@pytest.mark.parametrize("minutes,bucket", [(0, "<=5 min"), (5, "<=5 min"), (6, "6-15 min"), (15, "6-15 min"), (16, "16-30 min"), (30, "16-30 min"), (31, "31-60 min"), (60, "31-60 min"), (61, ">60 min"), (None, "unknown")])
def test_management_after_shadow_buckets(minutes, bucket):
    assert management_after_shadow_bucket(minutes) == bucket


@pytest.mark.parametrize("margin,bucket", [(0.5, "<1"), (1, "1-5"), (5, "1-5"), (6, "5-10"), (10, "5-10"), (11, "10-20"), (20, "10-20"), (21, ">20"), (None, "unknown")])
def test_score_margin_buckets(margin, bucket):
    assert score_margin_bucket(margin) == bucket


def test_heartbeat_contains_operational_fields_only():
    heartbeat = build_worker_heartbeat(instance_id="instance-a", started_at=NOW, completed_at=NOW, scanned=25, evaluated=3, would_execute=2)
    assert heartbeat["worker_name"] == "crm_sla_reassignment_worker_v1"
    assert heartbeat["mode"] == "shadow"
    assert "phone" not in heartbeat and "email" not in heartbeat


def test_health_states_cover_disabled_stale_lease_and_healthy():
    disabled = get_sla_reassignment_worker_health(worker_enabled=False, shadow_enabled=False, now=NOW)
    stale = get_sla_reassignment_worker_health(worker_enabled=True, shadow_enabled=True, heartbeat={}, now=NOW, lease_held=True)
    no_lease = get_sla_reassignment_worker_health(worker_enabled=True, shadow_enabled=True, heartbeat={}, now=NOW, lease_held=False)
    healthy = get_sla_reassignment_worker_health(worker_enabled=True, shadow_enabled=True, heartbeat={"last_completed_at": NOW.isoformat()}, now=NOW + timedelta(seconds=1), lease_held=True)
    snapshot_stale = get_sla_reassignment_worker_health(worker_enabled=True, shadow_enabled=True, heartbeat={"last_completed_at": NOW.isoformat()}, now=NOW + timedelta(seconds=1), lease_held=True, snapshot_age_seconds=301)
    assert disabled["status"] == "DISABLED"
    assert stale["status"] == "SHADOW_STALE"
    assert no_lease["status"] == "LEASE_NOT_HELD"
    assert healthy["status"] == "SHADOW_HEALTHY"
    assert snapshot_stale["status"] == "SNAPSHOT_STALE"


def test_health_config_and_db_fail_closed():
    assert get_sla_reassignment_worker_health(worker_enabled=True, shadow_enabled=True, config_valid=False, now=NOW)["status"] == "CONFIG_ERROR"
    assert get_sla_reassignment_worker_health(worker_enabled=True, shadow_enabled=True, db_error=True, now=NOW)["status"] == "DB_ERROR"


def test_kill_switch_truth_table_keeps_executor_separate():
    rows = kill_switch_truth_table()
    assert any(row["effect"] == "shadow_only;executor_not_called" for row in rows)
    assert any("executor_not_called" in row["effect"] for row in rows)
    assert rows[-1]["effect"] == "executor_gate_still_requires_canary_authorization"


def test_shadow_configuration_requires_explicit_cutover_and_policy():
    invalid = validate_shadow_configuration(worker_enabled=True, shadow_enabled=True, reassignment_enabled=False, cutover_at=None)
    valid = validate_shadow_configuration(worker_enabled=True, shadow_enabled=True, reassignment_enabled=False, cutover_at=NOW, policy_version="crm_sla_reassignment_v1")
    assert invalid["valid"] is False
    assert "CUTOVER_MISSING_OR_INVALID" in invalid["reasons"]
    assert valid["valid"] is True
    assert valid["executor_must_remain_disabled"] is True


def test_live_capacity_snapshot_separates_current_counts_from_history():
    class Cursor:
        def __init__(self, rows): self.rows = rows
        def limit(self, value): return self
        async def to_list(self, length=None): return list(self.rows)

    class Collection:
        def __init__(self, rows): self.rows = rows
        def find(self, query, projection=None): return Cursor(self.rows)

    class DB:
        def __init__(self, mapping): self.mapping = mapping
        def __getitem__(self, key): return self.mapping.get(key, Collection([]))

    db = DB({
        "crm_assignment_cycles": Collection([{"lead_id": "l1", "assignment_cycle_id": "c1", "assigned_to_user_id": "a", "assigned_at": NOW - timedelta(hours=4), "temperature_at_assignment": "NORMAL", "cycle_status": "active", "unassigned_at": None}]),
        "leads": Collection([{"_id": "l1", "pipeline_stage": "NEW", "lead_temperature_effective": "NORMAL", "lifecycle": {}}]),
        "crm_management_results": Collection([]),
        "crm_events": Collection([]),
    })
    result = asyncio.run(build_live_capacity_snapshot(db, as_of=NOW, policy_since=NOW - timedelta(days=1)))
    assert result["capacity_metrics"]["a"]["open"] == 1
    assert result["capacity_metrics"]["a"]["unmanaged"] == 1
    assert result["docs_examined"] == 1 + 1


def test_worker_snapshot_failure_is_explicitly_fail_closed(monkeypatch):
    import chatbot.crm_sla_reassignment_worker as worker
    from config import Config
    old_worker = worker._cfg("CRM_SLA_REASSIGNMENT_WORKER_ENABLED", False)
    old_shadow = worker._cfg("CRM_SLA_REASSIGNMENT_SHADOW_ENABLED", False)
    Config.CRM_SLA_REASSIGNMENT_WORKER_ENABLED = True
    Config.CRM_SLA_REASSIGNMENT_SHADOW_ENABLED = True

    async def fail_snapshot(*args, **kwargs):
        raise RuntimeError("snapshot unavailable")

    monkeypatch.setattr(worker, "_default_performance_snapshot", fail_snapshot)
    try:
        result = asyncio.run(worker.run_sla_reassignment_worker_iteration(scan_result={"cycles": []}, context={"leads_by_id": {}, "active_cycles_by_lead": {}, "events_by_lead": {}, "results_by_cycle": {}}, cutover_at=NOW))
    finally:
        Config.CRM_SLA_REASSIGNMENT_WORKER_ENABLED = old_worker
        Config.CRM_SLA_REASSIGNMENT_SHADOW_ENABLED = old_shadow
    assert result["status"] == "snapshot_unavailable"
    assert "SNAPSHOT_UNAVAILABLE" in result["iteration"]["error_codes"]
