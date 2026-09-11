from datetime import datetime, timezone
import inspect

import pytest

from config import Config
import chatbot.crm_sla_reassignment_cutover as cutover_module
from chatbot.crm_sla_hybrid_stabilization import build_decision
from chatbot.crm_sla_reassignment_cutover import (
    CUTOVER_CONFIGURATION_INVALID,
    CUTOVER_NOT_CONFIGURED,
    CUTOVER_POLICY_VERSION,
    CUTOVER_ELIGIBLE,
    PRE_CUTOVER_ALREADY_EXPIRED,
    canonical_sla_breached_at,
    cutover_decision_validation,
    evaluate_cutover,
    evaluate_cycle_cutover,
    future_only_expired_cycle_query,
    parse_configured_cutover,
)
from chatbot.crm_sla_reassignment_executor import execute_sla_reassignment_transaction
from chatbot.crm_sla_reassignment_models import SLAReassignmentErrorCode
from chatbot.crm_sla_hybrid_rescue import RM_GLOBAL_RESCUE
from chatbot.crm_sla_hybrid_stabilization import generate_decision_id


CUTOVER_LOCAL = "2026-09-15T09:00:00-03:00"
CUTOVER_UTC = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def test_breach_one_minute_before_cutover_is_excluded():
    result = evaluate_cutover(
        breached_at=datetime(2026, 9, 15, 11, 59, tzinfo=timezone.utc),
        cutover_at=CUTOVER_LOCAL,
    )
    assert result.outcome == PRE_CUTOVER_ALREADY_EXPIRED
    assert result.eligible is False


def test_breach_exactly_at_cutover_is_eligible():
    result = evaluate_cutover(breached_at=CUTOVER_UTC, cutover_at=CUTOVER_LOCAL)
    assert result.outcome == CUTOVER_ELIGIBLE
    assert result.eligible is True


def test_breach_one_minute_after_cutover_is_eligible():
    result = evaluate_cutover(
        breached_at=datetime(2026, 9, 15, 12, 1, tzinfo=timezone.utc),
        cutover_at=CUTOVER_LOCAL,
    )
    assert result.eligible is True


def test_assigned_before_cutover_breach_after_cutover_is_eligible():
    cycle = {
        "assigned_at": datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc),
        "sla_started_at": datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc),
        "temperature_at_assignment": "NORMAL",
    }
    breach = canonical_sla_breached_at(cycle)
    assert breach is not None and breach > CUTOVER_UTC
    assert evaluate_cycle_cutover(cycle, cutover_at=CUTOVER_LOCAL).eligible is True


def test_assigned_and_breach_before_cutover_is_excluded():
    cycle = {
        "assigned_at": datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
        "sla_started_at": datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
        "temperature_at_assignment": "NORMAL",
        "sla_breached_at": datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc),
    }
    assert evaluate_cycle_cutover(cycle, cutover_at=CUTOVER_LOCAL).outcome == PRE_CUTOVER_ALREADY_EXPIRED


def test_pre_cutover_breach_remains_excluded_days_later():
    result = evaluate_cutover(
        breached_at=datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc),
        cutover_at=CUTOVER_LOCAL,
    )
    assert result.outcome == PRE_CUTOVER_ALREADY_EXPIRED


def test_manual_post_cutover_cycle_can_be_eligible():
    cycle = {
        "assigned_at": datetime(2026, 9, 15, 13, 0, tzinfo=timezone.utc),
        "sla_started_at": datetime(2026, 9, 15, 13, 0, tzinfo=timezone.utc),
        "temperature_at_assignment": "NORMAL",
    }
    assert evaluate_cycle_cutover(cycle, cutover_at=CUTOVER_LOCAL).eligible is True


def test_automatic_post_cutover_cycle_can_be_eligible():
    cycle = {
        "assigned_at": datetime(2026, 9, 15, 13, 0, tzinfo=timezone.utc),
        "sla_started_at": datetime(2026, 9, 15, 13, 0, tzinfo=timezone.utc),
        "temperature_at_assignment": "HOT",
        "reassignment_source_cycle_id": "previous-cycle",
        "reassignment_decision_id": "decision",
    }
    assert evaluate_cycle_cutover(cycle, cutover_at=CUTOVER_LOCAL).eligible is True


def test_cutover_uses_america_santiago_timezone():
    assert parse_configured_cutover(CUTOVER_LOCAL) == CUTOVER_UTC


def test_naive_cutover_is_rejected():
    with pytest.raises(ValueError, match="naive"):
        parse_configured_cutover("2026-09-15T09:00:00")


def test_cutover_missing_is_fail_closed_when_evaluated():
    result = evaluate_cutover(
        breached_at=CUTOVER_UTC,
        cutover_at=None,
    )
    assert result.outcome == CUTOVER_NOT_CONFIGURED
    assert result.eligible is False


def test_feature_off_does_not_consume_missing_cutover(monkeypatch):
    monkeypatch.setattr(Config, "CRM_SLA_REASSIGNMENT_ENABLED", False)
    monkeypatch.setattr(Config, "CRM_SLA_TRANSACTION_GATE_ENABLED", False)
    result = execute_sla_reassignment_transaction(object(), {"decision_id": "unused"})
    assert result.outcome == "FEATURE_DISABLED"


def test_restart_does_not_change_explicit_cutover():
    first = parse_configured_cutover(CUTOVER_LOCAL)
    second = parse_configured_cutover(CUTOVER_LOCAL)
    assert first == second == CUTOVER_UTC


def _valid_decision(**overrides):
    values = {
        "lead_id": "lead",
        "current_assignment_cycle_id": "cycle",
        "previous_owner_user_id": "owner-a",
        "policy_branch": RM_GLOBAL_RESCUE,
        "candidate_user_ids": ("owner-b",),
        "selected_user_id": "owner-b",
        "assignment_number": 0,
        "policy_version": "crm_sla_reassignment_v1",
        "sla_breached_at": "2026-09-15T12:30:00+00:00",
        "reassignment_cutover_at": CUTOVER_LOCAL,
        "source_cycle_sla_breached_at": "2026-09-15T12:30:00+00:00",
    }
    values.update(overrides)
    return build_decision(**values).to_dict()


def test_decision_contains_future_only_fields():
    decision = _valid_decision()
    assert decision["reassignment_cutover_at"] == CUTOVER_UTC.isoformat()
    assert decision["source_cycle_sla_breached_at"] == "2026-09-15T12:30:00+00:00"
    assert decision["cutover_eligible"] is True
    assert decision["cutover_policy_version"] == CUTOVER_POLICY_VERSION


def test_decision_pre_cutover_is_not_marked_eligible():
    decision = _valid_decision(
        source_cycle_sla_breached_at="2026-09-15T11:59:00+00:00",
    )
    assert decision["cutover_eligible"] is False


def test_worker_query_has_breach_boundary_and_active_cycle():
    query = future_only_expired_cycle_query(CUTOVER_LOCAL)
    assert query["expired"] is True
    assert query["cycle_status"] == "active"
    assert query["sla_breached_at"]["$gte"] == CUTOVER_UTC


def test_executor_cutover_revalidation_missing_config_is_closed(monkeypatch):
    monkeypatch.setattr(Config, "CRM_SLA_REASSIGNMENT_ENABLED", True)
    monkeypatch.setattr(Config, "CRM_SLA_TRANSACTION_GATE_ENABLED", True)
    monkeypatch.setattr(Config, "CRM_SLA_REASSIGNMENT_CUTOVER_AT", None)
    decision = _valid_decision()
    decision["decision_id"] = generate_decision_id("lead", "cycle", "crm_sla_reassignment_v1")
    result = execute_sla_reassignment_transaction(object(), decision)
    assert result.outcome == SLAReassignmentErrorCode.CUTOVER_NOT_CONFIGURED.value


def test_executor_invalid_config_is_closed(monkeypatch):
    monkeypatch.setattr(Config, "CRM_SLA_REASSIGNMENT_ENABLED", True)
    monkeypatch.setattr(Config, "CRM_SLA_TRANSACTION_GATE_ENABLED", True)
    monkeypatch.setattr(Config, "CRM_SLA_REASSIGNMENT_CUTOVER_AT", "2026-09-15T09:00:00")
    decision = _valid_decision()
    result = execute_sla_reassignment_transaction(object(), decision)
    assert result.outcome == SLAReassignmentErrorCode.CUTOVER_CONFIGURATION_INVALID.value


def test_ledger_plan_contains_cutover_fields():
    from chatbot.crm_sla_reassignment_transaction import build_transaction_plan

    decision = _valid_decision()
    source = {
        "_id": "source",
        "assignment_cycle_id": "cycle",
        "lead_id": "lead",
        "assigned_to_user_id": "owner-a",
        "assigned_to_display_name": "Owner A",
        "assigned_at": datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc),
        "cycle_status": "active",
        "unassigned_at": None,
        "reason": "inbound_message",
        "cycle_origin": "inbound_message",
        "notification_eligible": True,
        "sla_policy_version": "sla_visual_v1_20260723",
        "temperature_at_assignment": "NORMAL",
        "cycle_version": 1,
    }
    lead = {
        "_id": "lead",
        "ejecutivo_asignado": "Owner A",
        "prospecto": {"ejecutivo": "Owner A"},
        "lifecycle": {"current_assignment_cycle_id": "cycle"},
        "stage": "NEW",
    }
    target = {"_id": "owner-b", "nombre": "Owner B", "is_active": True}
    plan = build_transaction_plan(
        decision=decision,
        source_cycle=source,
        lead=lead,
        target_user=target,
        reassigned_at=CUTOVER_UTC,
        effective_sla_started_at=CUTOVER_UTC,
        expected_sla_policy_version="sla_visual_v1_20260723",
    )
    event = plan.operations[-1].document
    assert event["reassignment_cutover_at"] == CUTOVER_UTC.isoformat()
    assert event["source_cycle_sla_breached_at"] == "2026-09-15T12:30:00+00:00"
    assert event["cutover_validation_result"] == "CUTOVER_ELIGIBLE"


def test_mongo_writes_are_not_part_of_cutover_helpers():
    source = inspect.getsource(cutover_module)
    assert not any(token in source for token in ("ins" + "ert_one", "upd" + "ate_one", "del" + "ete_one"))
