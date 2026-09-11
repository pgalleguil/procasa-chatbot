from __future__ import annotations

from datetime import datetime, timezone

import pytest

from chatbot.crm_sla_reassignment_runtime import ShadowWriteGuardDB, ShadowWriteGuardViolation, _health_for_duration
from chatbot.crm_sla_reassignment_shadow import (
    InMemoryShadowStore,
    SHADOW_POLICY_VERSION,
    SHADOW_SCHEMA_VERSION,
    reconstruct_shadow_distribution_state,
    validate_shadow_configuration,
)


UTC = timezone.utc
CUTOVER = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def test_shadow_count_is_once_per_source_cycle_and_keeps_history():
    store = InMemoryShadowStore()
    first = store.observe({
        "_id": "v1:cycle-1", "source_cycle_id": "cycle-1", "policy_version": SHADOW_POLICY_VERSION,
        "would_execute": True, "selected_user_id": "user-a", "eligibility_outcome": "WOULD_REASSIGN",
        "evaluated_at": CUTOVER.isoformat(),
    }, now=CUTOVER)
    second = store.observe({
        "_id": "v1:cycle-1", "source_cycle_id": "cycle-1", "policy_version": SHADOW_POLICY_VERSION,
        "would_execute": False, "selected_user_id": None, "exclusion_reason": "PROTECTED_BY_MANAGEMENT",
        "human_management_at": "2026-09-11T12:10:00+00:00", "evaluated_at": "2026-09-11T12:10:00+00:00",
    }, now="2026-09-11T12:10:00+00:00")
    assert first["shadow_assignment_count"] == 1
    assert second["shadow_assignment_count"] == 1
    assert second["shadow_assignment_counted_at"] == CUTOVER.isoformat()
    assert second["would_execute"] is True
    assert second["evaluation_count"] == 2


def test_shadow_distribution_excludes_uncounted_and_pre_cutover_rows():
    rows = [
        {"_id": "a", "source_cycle_id": "a", "policy_version": SHADOW_POLICY_VERSION, "shadow_version": SHADOW_SCHEMA_VERSION, "would_execute": True, "selected_user_id": "u1", "branch": "RM_GLOBAL_RESCUE", "shadow_assignment_counted_at": datetime(2026, 9, 10, tzinfo=UTC)},
        {"_id": "b", "source_cycle_id": "b", "policy_version": SHADOW_POLICY_VERSION, "shadow_version": SHADOW_SCHEMA_VERSION, "would_execute": True, "selected_user_id": "u2", "branch": "RM_GLOBAL_RESCUE"},
        {"_id": "c", "source_cycle_id": "c", "policy_version": SHADOW_POLICY_VERSION, "shadow_version": SHADOW_SCHEMA_VERSION, "would_execute": True, "selected_user_id": "u3", "branch": "RM_GLOBAL_RESCUE", "shadow_assignment_counted_at": CUTOVER},
    ]
    state = reconstruct_shadow_distribution_state(rows, cutover_at=CUTOVER)
    assert state["shadow_assignment_counted_cycles"] == ["c"]
    assert state["rm_history"] == ["u3"]


def test_business_writes_are_rejected_by_shadow_adapter():
    class Collection:
        def update_one(self, *_args, **_kwargs):
            return None

    class DB:
        def __getitem__(self, name):
            return Collection()

    with pytest.raises(ShadowWriteGuardViolation):
        ShadowWriteGuardDB(DB())["leads"].update_one({}, {})


def test_shadow_and_lease_writes_remain_allowed():
    class Collection:
        def bulk_write(self, *_args, **_kwargs):
            return None

    class DB:
        def __getitem__(self, name):
            return Collection()

    # Attribute access must reach the allowed collection unchanged.
    assert callable(ShadowWriteGuardDB(DB())["crm_sla_reassignment_shadow_v1"].bulk_write)
    assert callable(ShadowWriteGuardDB(DB())["crm_worker_leases"].bulk_write)


def test_shadow_configuration_requires_explicit_shadow_cutover_and_empty_prod_cutover():
    valid = validate_shadow_configuration(
        worker_enabled=True, shadow_enabled=True, reassignment_enabled=False,
        shadow_cutover_at="2026-09-11T09:00:00-03:00", production_cutover_at=None,
        policy_version=SHADOW_POLICY_VERSION, security_enabled=False,
        transaction_gate_enabled=False,
    )
    assert valid["valid"] is True
    blocked = validate_shadow_configuration(
        worker_enabled=True, shadow_enabled=True, reassignment_enabled=False,
        shadow_cutover_at="2026-09-11T09:00:00-03:00", production_cutover_at="2026-09-10T09:00:00-03:00",
        policy_version=SHADOW_POLICY_VERSION,
    )
    assert "PRODUCTION_CUTOVER_MUST_BE_EMPTY_IN_SHADOW" in blocked["reasons"]


def test_duration_health_guard_stops_after_three_critical_iterations():
    assert _health_for_duration(4999, critical_consecutive=0) == ("SHADOW_HEALTHY", "CONTINUE")
    assert _health_for_duration(5000, critical_consecutive=0) == ("SHADOW_DEGRADED", "CONTINUE")
    assert _health_for_duration(15001, critical_consecutive=2) == ("SHADOW_CRITICAL", "CONTINUE")
    assert _health_for_duration(15001, critical_consecutive=3) == ("SHADOW_ERROR", "STOP_NEW_EVALUATIONS")
