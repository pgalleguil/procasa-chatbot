"""Tests for the prospective SLA reassignment worker contract.

All fixtures are synthetic and all worker calls are shadow-only.  No test
creates a Mongo client or invokes the transaction executor.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from config import Config
from chatbot.crm_sla_hybrid_rescue import REGION_JPC_MARIA_HERNAN, REGION_REVIEW_REQUIRED, RM_GLOBAL_RESCUE, REGIONAL_POLICY_NOT_DEFINED
from chatbot.crm_sla_reassignment_worker import (
    COMMITTED_EVENT,
    POLICY_VERSION,
    R2_HISTORY_WINDOW,
    SLAReassignmentShadowEvaluation,
    canonical_expiration_recheck,
    crm_sla_reassignment_worker_loop,
    load_jpc_distribution_state,
    load_rm_r2_distribution_state,
    multi_worker_strategy,
    r2_guardrail_status,
    run_sla_reassignment_worker_iteration,
    scan_sla_reassignment_candidates,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)
CUTOVER = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)


def candidate(user_id: str, name: str, p50: float = 20.0, *, active: bool = True) -> dict:
    key = name.lower().replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    return {
        "user_id": user_id, "executive": name, "identity_key": key, "executive_key": key,
        "active": active, "role": "agente", "legacy": False, "protected_by_management": False,
        "data_issue": False, "closed_lead": False, "not_currently_expired": False,
        "sample_size": 30, "sla_compliance_rate": 0.8, "attention_rate": 0.8,
        "p50_first_management_business_minutes": p50, "p90_first_management_business_minutes": p50 * 2,
        "open_current_policy": 1, "unmanaged_current_policy": 1, "expired_current_policy": 0,
        "shadow_received_count": 0,
    }


def cycle(index: int, *, assigned_at: datetime | None = None, temperature: str = "NORMAL", number: int = 0, previous: list[str] | None = None) -> dict:
    return {
        "lead_id": f"lead-{index}", "assignment_cycle_id": f"cycle-{index}",
        "assigned_to_user_id": "owner", "assigned_to_display_name": "Owner",
        "assigned_at": assigned_at or datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        "cycle_status": "active", "unassigned_at": None, "schema_version": "crm_assignment_cycle_v1",
        "temperature_at_assignment": temperature, "automatic_reassignment_number": number,
        "previous_owner_user_ids": previous or [], "cycle_version": 1,
    }


def lead(index: int, *, branch: str = RM_GLOBAL_RESCUE, stage: str = "NEW") -> dict:
    return {
        "_id": f"lead-{index}", "pipeline_stage": stage, "lead_temperature_effective": "NORMAL",
        "ejecutivo_asignado": "Owner", "lifecycle": {"current_assignment_cycle_id": f"cycle-{index}"},
        "policy_category": branch,
    }


def snapshot() -> dict:
    owners = {"owner": {"_id": "owner", "nombre": "Owner", "rol": "agente", "is_active": True}}
    rows = [candidate("owner", "Owner", 50), candidate("a", "A", 10), candidate("b", "B", 25),
            candidate("maria", "María Paz Galleguillos", 30), candidate("hernan", "Hernán Castro", 20)]
    return {
        "candidates": rows, "users_by_id": owners,
        "team": {"sla_compliance_rate": 0.5, "attention_rate": 0.5, "team_p50_average": 30, "team_p90_average": 60},
        "performance_snapshot_version": "snapshot-test", "catalog": {}, "properties": {},
    }


def context(cycles: list[dict], leads: list[dict] | None = None, *, events: dict | None = None, results: dict | None = None) -> dict:
    leads = leads or [lead(int(row["lead_id"].split("-")[-1])) for row in cycles]
    return {
        "leads_by_id": {row["_id"]: row for row in leads},
        "active_cycles_by_lead": {row["lead_id"]: [row] for row in cycles},
        "events_by_lead": events or {}, "results_by_cycle": results or {},
    }


def run(**kwargs):
    old_worker = Config.CRM_SLA_REASSIGNMENT_WORKER_ENABLED
    old_shadow = Config.CRM_SLA_REASSIGNMENT_SHADOW_ENABLED
    Config.CRM_SLA_REASSIGNMENT_WORKER_ENABLED = True
    Config.CRM_SLA_REASSIGNMENT_SHADOW_ENABLED = True
    try:
        return asyncio.run(run_sla_reassignment_worker_iteration(**kwargs))
    finally:
        Config.CRM_SLA_REASSIGNMENT_WORKER_ENABLED = old_worker
        Config.CRM_SLA_REASSIGNMENT_SHADOW_ENABLED = old_shadow


def base_kwargs(rows: list[dict], leads: list[dict] | None = None, **extra):
    return {
        "now": NOW, "cutover_at": CUTOVER,
        "scan_result": {"cycles": rows, "next_page_token": None, "documents_examined": len(rows)},
        "context": context(rows, leads), "performance_snapshot": snapshot(), "committed_events": [], **extra,
    }


def test_flags_off_do_not_scan_or_create_a_result():
    assert asyncio.run(run_sla_reassignment_worker_iteration(scan_result={"cycles": []})) == {
        "status": "disabled", "iteration": {
            "status": "disabled", "scanned": 0, "canonically_expired": 0, "pre_cutover_skipped": 0,
            "protected_skipped": 0, "undefined_skipped": 0, "review_skipped": 0, "max2_skipped": 0,
            "not_actually_expired": 0, "evaluated": 0, "would_reassign": 0, "errors": 0,
            "duration_ms": pytest.approx(0, abs=1000), "error_codes": [],
        }, "evaluations": [], "decisions": [],
    }


def test_worker_enabled_without_shadow_is_reserved_and_does_not_run():
    old_worker = Config.CRM_SLA_REASSIGNMENT_WORKER_ENABLED
    old_shadow = Config.CRM_SLA_REASSIGNMENT_SHADOW_ENABLED
    Config.CRM_SLA_REASSIGNMENT_WORKER_ENABLED = True
    Config.CRM_SLA_REASSIGNMENT_SHADOW_ENABLED = False
    try:
        result = asyncio.run(run_sla_reassignment_worker_iteration(scan_result={"cycles": []}))
        asyncio.run(crm_sla_reassignment_worker_loop(stop_event=asyncio.Event()))
    finally:
        Config.CRM_SLA_REASSIGNMENT_WORKER_ENABLED = old_worker
        Config.CRM_SLA_REASSIGNMENT_SHADOW_ENABLED = old_shadow
    assert result["status"] == "shadow_required"


def test_missing_cutover_fails_closed():
    old_worker = Config.CRM_SLA_REASSIGNMENT_WORKER_ENABLED
    old_shadow = Config.CRM_SLA_REASSIGNMENT_SHADOW_ENABLED
    old_cutover = Config.CRM_SLA_REASSIGNMENT_CUTOVER_AT
    Config.CRM_SLA_REASSIGNMENT_WORKER_ENABLED = True
    Config.CRM_SLA_REASSIGNMENT_SHADOW_ENABLED = True
    Config.CRM_SLA_REASSIGNMENT_CUTOVER_AT = None
    try:
        result = asyncio.run(run_sla_reassignment_worker_iteration(scan_result={"cycles": []}))
    finally:
        Config.CRM_SLA_REASSIGNMENT_WORKER_ENABLED = old_worker
        Config.CRM_SLA_REASSIGNMENT_SHADOW_ENABLED = old_shadow
        Config.CRM_SLA_REASSIGNMENT_CUTOVER_AT = old_cutover
    assert result["status"] == "fatal_configuration_error"


def test_scanner_is_superset_and_keyset_pagination_is_stable():
    class Cursor:
        def __init__(self, rows): self.rows = rows
        def sort(self, value): return self
        def limit(self, value): self.rows = self.rows[:value]; return self
        async def to_list(self, length=None): return self.rows
    class Collection:
        def __init__(self, rows): self.rows = rows; self.query = None
        def find(self, query, projection=None): self.query = query; return Cursor(self.rows)
    class DB:
        def __init__(self, collection): self.collection = collection
        def __getitem__(self, key): return self.collection
    coll = Collection([cycle(1)])
    result = asyncio.run(scan_sla_reassignment_candidates(DB(coll), batch_size=10))
    assert result["scanned"] == 1
    assert result["candidate_query_superset"]["cycle_status"] == "active"
    assert result["candidate_query_superset"]["unassigned_at"] is None
    assert "sla_breached_at" not in repr(result["candidate_query_superset"])
    assert result["next_page_token"]["assignment_cycle_id"] == "cycle-1"


def test_canonical_expiration_ignores_inconsistent_persisted_deadline():
    row = cycle(1)
    row["sla_deadline_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    expiry = canonical_expiration_recheck(row, lead(1), now=NOW)
    assert expiry.expired is True
    assert expiry.persisted_deadline_mismatch is True
    assert expiry.breach_at is not None


def test_pre_cutover_is_skipped_even_when_currently_expired():
    row = cycle(1, assigned_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC))
    result = run(**base_kwargs([row], [lead(1)]))
    assert result["iteration"]["pre_cutover_skipped"] == 1
    assert result["iteration"]["would_reassign"] == 0
    assert result["evaluations"][0]["exclusion_reason"] == "PRE_CUTOVER_ALREADY_EXPIRED"


def test_hot_breach_one_minute_after_cutover_is_future_eligible():
    row = cycle(1, assigned_at=datetime(2026, 9, 10, 14, 0, tzinfo=UTC), temperature="HOT")
    expiry = canonical_expiration_recheck(row, lead(1), now=NOW)
    assert expiry.breach_at is not None
    result = run(**base_kwargs([row], [lead(1)], cutover_at=expiry.breach_at - timedelta(minutes=1)))
    assert result["iteration"]["pre_cutover_skipped"] == 0
    assert result["iteration"]["would_reassign"] == 1


def test_future_rm_creates_decision_but_never_executes():
    row = cycle(1)
    result = run(**base_kwargs([row], [lead(1)], cutover_at=datetime(2026, 9, 1, tzinfo=UTC)))
    assert result["iteration"]["would_reassign"] == 1
    assert result["decisions"][0]["policy_version"] == POLICY_VERSION
    assert result["evaluations"][0]["would_execute"] is True
    assert result["evaluations"][0]["shadow"] is True


def test_hot_order_precedes_normal_and_overdue_order_is_deterministic():
    hot = cycle(1, temperature="HOT")
    normal = cycle(2)
    rows = [normal, hot]
    leads = [lead(1), lead(2)]
    leads[0]["lead_temperature_effective"] = "HOT"
    result = run(**base_kwargs(rows, leads, cutover_at=datetime(2026, 9, 1, tzinfo=UTC)))
    assert [item["lead_id"] for item in result["evaluations"]] == ["lead-1", "lead-2"]


def test_human_protection_is_not_invented_for_automatic_activity():
    row = cycle(1)
    auto = {"lead_id": "lead-1", "type": "BOT_MSG", "timestamp": datetime(2026, 9, 10, 17, 0, tzinfo=UTC)}
    human = {"lead_id": "lead-1", "type": "SEND_WA_LEAD", "actor": "owner", "actor_type": "human", "timestamp": datetime(2026, 9, 10, 17, 0, tzinfo=UTC)}
    auto_kwargs = base_kwargs([row], [lead(1)], cutover_at=datetime(2026, 9, 1, tzinfo=UTC))
    auto_kwargs["context"] = context([row], [lead(1)], events={"lead-1": [auto]})
    human_kwargs = base_kwargs([row], [lead(1)], cutover_at=datetime(2026, 9, 1, tzinfo=UTC))
    human_kwargs["context"] = context([row], [lead(1)], events={"lead-1": [human]})
    automatic_result = run(**auto_kwargs)
    protected_result = run(**human_kwargs)
    assert automatic_result["iteration"]["would_reassign"] == 1
    assert protected_result["iteration"]["protected_skipped"] == 1


@pytest.mark.parametrize("branch,field", [(REGIONAL_POLICY_NOT_DEFINED, "undefined_skipped"), (REGION_REVIEW_REQUIRED, "review_skipped")])
def test_undefined_and_review_branches_never_call_selector(branch, field):
    row = cycle(1)
    result = run(**base_kwargs([row], [lead(1, branch=branch)], cutover_at=datetime(2026, 9, 1, tzinfo=UTC)))
    assert result["iteration"][field] == 1
    assert not result["decisions"]


def test_jpc_uses_only_maria_and_hernan_pool():
    row = cycle(1)
    result = run(**base_kwargs([row], [lead(1, branch=REGION_JPC_MARIA_HERNAN)], cutover_at=datetime(2026, 9, 1, tzinfo=UTC)))
    assert result["iteration"]["would_reassign"] == 1
    decision = result["decisions"][0]
    assert decision["selection_rule"] == "JPC_J3"
    assert decision["selected_user_id"] in {"maria", "hernan"}


def test_max_two_requires_supervisor_without_selector():
    row = cycle(1, number=2, previous=["a", "b"])
    result = run(**base_kwargs([row], [lead(1)], cutover_at=datetime(2026, 9, 1, tzinfo=UTC)))
    assert result["iteration"]["max2_skipped"] == 1
    assert result["evaluations"][0]["exclusion_reason"] == "SUPERVISOR_REVIEW_REQUIRED"
    assert not result["decisions"]


def test_owner_and_previous_owner_are_excluded():
    row = cycle(1, previous=["a"])
    result = run(**base_kwargs([row], [lead(1)], cutover_at=datetime(2026, 9, 1, tzinfo=UTC)))
    evaluation = result["evaluations"][0]
    assert "owner" not in evaluation["candidate_ids"]
    assert "a" not in evaluation["candidate_ids"]


def test_stable_decision_id_for_two_scans_same_cycle():
    row = cycle(1)
    first = run(**base_kwargs([row], [lead(1)], cutover_at=datetime(2026, 9, 1, tzinfo=UTC)))
    second = run(**base_kwargs([row], [lead(1)], cutover_at=datetime(2026, 9, 1, tzinfo=UTC)))
    assert first["evaluations"][0]["decision_id"] == second["evaluations"][0]["decision_id"]


def test_r2_starts_on_twentieth_decision_and_uses_fifteen_point_alternative_distance():
    prior = ["a"] * 8 + ["b"] * 11
    assert len(prior) == R2_HISTORY_WINDOW
    alternative = {"user_id": "b", "dynamic_rescue_score": 90, "performance_data_valid": True}
    assert r2_guardrail_status("a", 100, [alternative], prior) == "R2_ROLLING_SHARE_LIMIT"
    assert r2_guardrail_status("a", 100, [alternative], prior[:-1]) == ""
    far = {"user_id": "b", "dynamic_rescue_score": 80, "performance_data_valid": True}
    assert r2_guardrail_status("a", 100, [far], prior) == ""


def test_r2_state_rebuild_orders_same_timestamp_by_decision_id():
    events = [
        {"_id": "b", "decision_id": "b", "event_type": COMMITTED_EVENT, "policy_version": POLICY_VERSION, "policy_branch": RM_GLOBAL_RESCUE, "source_cycle_sla_breached_at": "2026-09-10T16:00:00+00:00", "reassigned_at": "2026-09-10T17:00:00+00:00", "target_owner_user_id": "b"},
        {"_id": "a", "decision_id": "a", "event_type": COMMITTED_EVENT, "policy_version": POLICY_VERSION, "policy_branch": RM_GLOBAL_RESCUE, "source_cycle_sla_breached_at": "2026-09-10T16:00:00+00:00", "reassigned_at": "2026-09-10T17:00:00+00:00", "target_owner_user_id": "a"},
    ]
    state = asyncio.run(load_rm_r2_distribution_state(events=events, cutover_at=CUTOVER))
    assert state["history"] == ["a", "b"]
    assert state["window_size"] == 20
    assert state["distribution_state_version"]


def test_r2_twenty_plus_restart_has_same_next_guardrail_outcome():
    events = []
    targets = ["a"] * 8 + ["b"] * 12 + ["a"]
    for index, target in enumerate(targets):
        events.append({
            "_id": str(index), "decision_id": str(index), "event_type": COMMITTED_EVENT,
            "policy_version": POLICY_VERSION, "policy_branch": RM_GLOBAL_RESCUE,
            "source_cycle_sla_breached_at": "2026-09-10T16:00:00+00:00",
            "reassigned_at": (NOW + timedelta(seconds=index)).isoformat(),
            "target_owner_user_id": target,
        })
    first = asyncio.run(load_rm_r2_distribution_state(events=events, cutover_at=CUTOVER))
    restarted = asyncio.run(load_rm_r2_distribution_state(events=list(events), cutover_at=CUTOVER))
    alternatives = [{"user_id": "b", "dynamic_rescue_score": 90, "performance_data_valid": True}]
    assert first["history"] == restarted["history"]
    assert r2_guardrail_status("a", 100, alternatives, first["history"]) == r2_guardrail_status("a", 100, alternatives, restarted["history"])


def test_jpc_state_rebuilds_counts_and_observed_share():
    events = [
        {"_id": "1", "decision_id": "1", "event_type": COMMITTED_EVENT, "policy_version": POLICY_VERSION, "policy_branch": REGION_JPC_MARIA_HERNAN, "source_cycle_sla_breached_at": "2026-09-10T16:00:00+00:00", "reassigned_at": "2026-09-10T17:00:00+00:00", "target_owner_user_id": "maria-id"},
        {"_id": "2", "decision_id": "2", "event_type": COMMITTED_EVENT, "policy_version": POLICY_VERSION, "policy_branch": REGION_JPC_MARIA_HERNAN, "source_cycle_sla_breached_at": "2026-09-10T16:01:00+00:00", "reassigned_at": "2026-09-10T17:01:00+00:00", "target_owner_user_id": "hernan-id"},
        {"_id": "3", "decision_id": "3", "event_type": COMMITTED_EVENT, "policy_version": POLICY_VERSION, "policy_branch": REGION_JPC_MARIA_HERNAN, "source_cycle_sla_breached_at": "2026-09-10T16:02:00+00:00", "reassigned_at": "2026-09-10T17:02:00+00:00", "target_owner_user_id": "maria-id"},
    ]
    state = asyncio.run(load_jpc_distribution_state(events=events, cutover_at=CUTOVER, maria_user_id="maria-id", hernan_user_id="hernan-id"))
    assert state["maria_received"] == 2
    assert state["hernan_received"] == 1
    assert state["committed_total"] == 3
    assert state["observed_share"]["maria-id"] == pytest.approx(2 / 3)


def test_jpc_restart_reconstructs_the_same_next_state():
    events = []
    for index in range(25):
        events.append({"_id": str(index), "decision_id": str(index), "event_type": COMMITTED_EVENT, "policy_version": POLICY_VERSION, "policy_branch": REGION_JPC_MARIA_HERNAN, "source_cycle_sla_breached_at": "2026-09-10T16:00:00+00:00", "reassigned_at": f"2026-09-10T17:{index:02d}:00+00:00", "target_owner_user_id": "maria" if index % 3 == 0 else "hernan"})
    first = asyncio.run(load_jpc_distribution_state(events=events, cutover_at=CUTOVER))
    restarted = asyncio.run(load_jpc_distribution_state(events=list(events), cutover_at=CUTOVER))
    assert first["counts"] == restarted["counts"]
    assert first["distribution_state_version"] == restarted["distribution_state_version"]


def test_jpc_fifty_continuous_equals_twenty_five_restart_twenty_five():
    rows = [cycle(index) for index in range(50)]
    leads = [lead(index, branch=REGION_JPC_MARIA_HERNAN) for index in range(50)]
    continuous = run(**base_kwargs(rows, leads, cutover_at=datetime(2026, 9, 1, tzinfo=UTC), jpc_horizon_total=50))
    first_kwargs = base_kwargs(rows[:25], leads[:25], cutover_at=datetime(2026, 9, 1, tzinfo=UTC), jpc_horizon_total=50)
    first = run(**first_kwargs)
    committed = [
        {
            "_id": str(index), "decision_id": decision["decision_id"], "event_type": COMMITTED_EVENT,
            "policy_version": POLICY_VERSION, "policy_branch": REGION_JPC_MARIA_HERNAN,
            "source_cycle_sla_breached_at": decision["source_cycle_sla_breached_at"],
            "reassigned_at": (NOW + timedelta(seconds=index)).isoformat(),
            "target_owner_user_id": decision["selected_user_id"],
        }
        for index, decision in enumerate(first["decisions"])
    ]
    second_kwargs = base_kwargs(rows[25:], leads[25:], cutover_at=datetime(2026, 9, 1, tzinfo=UTC), jpc_horizon_total=50)
    second_kwargs["committed_events"] = committed
    second = run(**second_kwargs)
    continuous_ids = [decision["selected_user_id"] for decision in continuous["decisions"]]
    split_ids = [decision["selected_user_id"] for decision in first["decisions"] + second["decisions"]]
    assert continuous_ids == split_ids



def test_exception_isolation_continues_after_missing_lead():
    rows = [cycle(1), cycle(2)]
    ctx = context(rows, [lead(1)])
    kwargs = base_kwargs(rows, [lead(1)])
    kwargs["context"] = ctx
    kwargs["cutover_at"] = datetime(2026, 9, 1, tzinfo=UTC)
    result = run(**kwargs)
    assert result["iteration"]["errors"] >= 1
    assert result["iteration"]["scanned"] == 2


def test_multi_worker_strategy_is_at_least_once_and_idempotent_at_executor_boundary():
    design = multi_worker_strategy()
    assert design["evaluation_delivery"] == "at_least_once"
    assert "idempotent" in design["recommended"]
    assert "same_decision_id" in design["different_winner_race"]


def test_worker_module_has_no_executor_or_startup_side_effect():
    source = Path("chatbot/crm_sla_reassignment_worker.py").read_text(encoding="utf-8").lower()
    assert "execute_sla_reassignment_transaction" not in source
    assert "create_task(" not in source
    assert "insert_one" not in source
    assert "update_one" not in source
    assert "delete_one" not in source
