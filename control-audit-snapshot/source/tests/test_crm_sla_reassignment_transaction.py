from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "chatbot"
    / "crm_sla_reassignment_transaction.py"
)
SPEC = importlib.util.spec_from_file_location("crm_sla_reassignment_transaction", MODULE_PATH)
transaction = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = transaction
SPEC.loader.exec_module(transaction)


NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
SLA_STARTED = datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc)


def snapshots():
    decision = {
        "decision_id": "decision-001",
        "lead_id": "lead-001",
        "current_assignment_cycle_id": "cycle-001",
        "previous_owner_user_id": "user-old",
        "previous_owner_user_ids": ["user-old", "user-prior"],
        "selected_user_id": "user-new",
        "selected_user_display_name": "Ejecutivo Nuevo",
        "assignment_number": 0,
        "automatic_reassignment_number": 0,
        "policy_version": "crm_sla_reassignment_v1",
        "policy_branch": "RM_R2",
        "selection_rule": "JPC_J3",
        "candidate_user_ids": ["user-new"],
        "candidate_scores_snapshot": [{"user_id": "user-new", "score": 0.8}],
        "sla_breached_at": NOW,
    }
    source_cycle = {
        "_id": "mongo-cycle-001",
        "assignment_cycle_id": "cycle-001",
        "lead_id": "lead-001",
        "assigned_to_user_id": "user-old",
        "assigned_to_display_name": "Ejecutivo Antiguo",
        "assigned_at": datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc),
        "cycle_status": "active",
        "unassigned_at": None,
        "first_valid_management_at": None,
        "reason": "inbound_message",
        "cycle_origin": "inbound_message",
        "notification_eligible": True,
        "sla_policy_version": "sla_visual_v1_20260723",
        "metric_version": "crm_metrics_v1",
        "schema_version": "crm_assignment_cycle_v1",
        "temperature_at_assignment": "HOT",
        "property_code": "P-001",
        "cycle_version": 3,
    }
    lead = {
        "_id": "lead-001",
        "ejecutivo_asignado": "Ejecutivo Antiguo",
        "prospecto": {"ejecutivo": "Ejecutivo Antiguo"},
        "lifecycle": {"current_assignment_cycle_id": "cycle-001"},
        "pipeline_stage": "NEW",
        "stage": "NEW",
    }
    target = {"_id": "user-new", "nombre": "Ejecutivo Nuevo", "is_active": True}
    return decision, source_cycle, lead, target


def make_plan(**overrides):
    decision, source_cycle, lead, target = snapshots()
    values = {
        "decision": decision,
        "source_cycle": source_cycle,
        "lead": lead,
        "target_user": target,
        "reassigned_at": NOW,
        "effective_sla_started_at": SLA_STARTED,
        "expected_sla_policy_version": "sla_visual_v1_20260723",
    }
    values.update(overrides)
    return transaction.build_transaction_plan(**values)


def test_plan_is_pure_four_write_contract_without_pii():
    plan = make_plan().as_dict()
    assert len(plan["operations"]) == 4
    assert [item["operation"] for item in plan["operations"]] == [
        "update_one",
        "insert_one",
        "update_one",
        "insert_one",
    ]
    assert plan["operations"][-1]["collection"] == (
        "crm_sla_reassignment_audit_v1"
    )
    serialized = repr(plan).lower()
    assert "phone" not in serialized
    assert "whatsapp" not in serialized
    assert "mailto" not in serialized
    assert "@" not in serialized


def test_source_filter_is_active_owner_and_management_cas():
    source_filter = make_plan().operations[0].filter
    assert source_filter["cycle_status"] == "active"
    assert source_filter["unassigned_at"] is None
    assert source_filter["assigned_to_user_id"] == "user-old"
    assert source_filter["assignment_cycle_id"] == "cycle-001"
    assert source_filter["_id"] == "mongo-cycle-001"
    assert source_filter["cycle_version"] == 3
    assert source_filter["$and"][0]["$or"]
    assert source_filter["reassignment_decision_id"]["$exists"] is False


def test_new_cycle_preserves_existing_routing_contract_and_marks_reassignment():
    new_cycle = make_plan().operations[1].document
    assert new_cycle["cycle_status"] == "active"
    assert new_cycle["assigned_to_user_id"] == "user-new"
    assert new_cycle["reason"] == "inbound_message"
    assert new_cycle["cycle_origin"] == "inbound_message"
    assert new_cycle["notification_eligible"] is True
    assert new_cycle["reassignment_reason"] == "sla_reassignment"
    assert new_cycle["previous_assignment_cycle_id"] == "cycle-001"
    assert new_cycle["previous_owner_user_ids"] == ["user-old", "user-prior"]
    assert new_cycle["automatic_reassignment_number"] == 1
    assert new_cycle["first_valid_management_at"] is None
    assert new_cycle["sla_started_at"] == SLA_STARTED


def test_source_close_does_not_change_source_owner_and_lead_points_to_new_cycle():
    source_update = make_plan().operations[0].update["$set"]
    lead_update = make_plan().operations[2].update["$set"]
    assert source_update["cycle_status"] == "reassigned"
    assert source_update["reassigned_to_user_id"] == "user-new"
    assert source_update["cycle_version"] == 4
    assert "assigned_to_user_id" not in source_update
    assert lead_update["ejecutivo_asignado"] == "Ejecutivo Nuevo"
    assert lead_update["lifecycle.current_assignment_cycle_id"].startswith(
        "sla-reassignment:decision-001"
    )
    assert lead_update["assignment_mirror_source"] == "crm_assignment_cycles"


def test_idempotency_is_deterministic_and_audit_is_immutable():
    first = make_plan().as_dict()
    second = make_plan().as_dict()
    assert first["decision_id"] == second["decision_id"]
    assert first["new_cycle_id"] == second["new_cycle_id"]
    event = first["operations"][-1]["document"]
    assert event["_id"] == "decision-001"
    assert event["immutable"] is True
    assert transaction.idempotency_key_for_decision("decision-001") == (
        transaction.idempotency_key_for_decision("decision-001")
    )


def test_previous_owner_history_blocks_ping_pong():
    decision, source_cycle, lead, target = snapshots()
    decision["selected_user_id"] = "user-prior"
    target = {"_id": "user-prior", "nombre": "Ejecutivo Anterior", "is_active": True}
    with pytest.raises(transaction.TransactionContractError, match="previous_owner"):
        transaction.build_transaction_plan(
            decision=decision,
            source_cycle=source_cycle,
            lead=lead,
            target_user=target,
            reassigned_at=NOW,
            effective_sla_started_at=SLA_STARTED,
            expected_sla_policy_version="sla_visual_v1_20260723",
        )


def test_fail_closed_for_lead_pointer_or_inactive_target_or_bad_review_enum():
    decision, source_cycle, lead, target = snapshots()
    lead["lifecycle"]["current_assignment_cycle_id"] = "other-cycle"
    with pytest.raises(transaction.TransactionContractError, match="lead_cycle_pointer"):
        make_plan(lead=lead)

    decision, source_cycle, lead, target = snapshots()
    target["is_active"] = False
    with pytest.raises(transaction.TransactionContractError, match="inactive"):
        make_plan(target_user=target)

    with pytest.raises(transaction.TransactionContractError, match="supervisor"):
        make_plan(supervisor_review_status="UNKNOWN")


def test_failure_injection_requires_full_rollback_and_same_decision_retry():
    matrix = transaction.failure_injection_matrix()
    assert len(matrix) == 4
    assert {item["expected"] for item in matrix} == {"ROLLBACK_NO_PARTIAL_STATE"}
    assert {item["requires_same_decision_retry"] for item in matrix} == {"true"}


def test_module_has_no_mongo_dependency():
    source = MODULE_PATH.read_text(encoding="utf-8").lower()
    assert "pymongo" not in source
    assert "motor" not in source
