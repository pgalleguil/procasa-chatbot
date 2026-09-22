"""Real MongoDB regression for repeated SLA reassignment transitions.

Run with ``CRM_SLA_REAL_MONGO_URI`` pointing to a local replica set. The test
is skipped when that opt-in URI is absent, so ordinary CI remains hermetic.
"""

from datetime import datetime, timedelta, timezone
import os

import pytest
from pymongo import MongoClient

from config import Config
from chatbot.crm_sla_hybrid_stabilization import generate_decision_id
from chatbot.crm_sla_reassignment_cutover import CUTOVER_POLICY_VERSION
from chatbot.crm_sla_reassignment_executor import execute_sla_reassignment_transaction


@pytest.mark.skipif(
    not os.getenv("CRM_SLA_REAL_MONGO_URI"),
    reason="requires an explicit local MongoDB replica-set URI",
)
def test_repeated_reassignment_keeps_unique_active_cycle_and_is_idempotent():
    uri = os.environ["CRM_SLA_REAL_MONGO_URI"]
    client = MongoClient(uri, retryWrites=False, tz_aware=True)
    hello = client.admin.command({"hello": 1})
    assert hello.get("setName") or hello.get("msg") == "isdbgrid"

    db = client["crm_sla_repeat_reassignment_test"]
    client.drop_database(db.name)
    cycles = db["crm_assignment_cycles"]
    canonical_filter = {
        "schema_version": "crm_assignment_cycle_v1",
        "cycle_status": "active",
        "lead_id": {"$exists": True},
        "assigned_to_user_id": {"$exists": True},
        "assignment_cycle_id": {"$exists": True},
    }
    cycles.create_index(
        [("lead_id", 1), ("cycle_status", 1)],
        unique=True,
        partialFilterExpression=canonical_filter,
        name="uq_crm_assignment_cycle_active_lead",
    )
    cycles.create_index(
        [("lead_id", 1), ("assigned_to_user_id", 1), ("cycle_status", 1), ("assignment_cycle_id", 1)],
        unique=True,
        partialFilterExpression=canonical_filter,
        name="uq_crm_cycle_identity_v1",
    )

    old_flags = {
        "enabled": getattr(Config, "CRM_SLA_REASSIGNMENT_ENABLED", False),
        "gate": getattr(Config, "CRM_SLA_TRANSACTION_GATE_ENABLED", False),
        "cutover": getattr(Config, "CRM_SLA_REASSIGNMENT_CUTOVER_AT", None),
        "production": getattr(Config, "IS_PRODUCTION", False),
    }
    Config.CRM_SLA_REASSIGNMENT_ENABLED = True
    Config.CRM_SLA_TRANSACTION_GATE_ENABLED = True
    Config.CRM_SLA_REASSIGNMENT_CUTOVER_AT = "2026-09-01T00:00:00+00:00"
    Config.IS_PRODUCTION = False

    now = datetime.now(timezone.utc).replace(microsecond=0)
    lead_id = "lead-repeat-reassignment-test"
    policy = "crm_sla_reassignment_v1"

    def make_cycle(cid, owner_id, owner_name, assigned_at, number, previous):
        return {
            "_id": cid,
            "assignment_cycle_id": cid,
            "lead_id": lead_id,
            "assigned_to_user_id": owner_id,
            "assigned_to_display_name": owner_name,
            "assigned_at": assigned_at,
            "sla_started_at": assigned_at,
            "owner_notified_at": assigned_at,
            "cycle_started_at": assigned_at,
            "temperature_at_assignment": "COLD",
            "unassigned_at": None,
            "assigned_by": "inbound",
            "reason": "inbound_message",
            "cycle_origin": "inbound_message",
            "notification_eligible": True,
            "schema_version": "crm_assignment_cycle_v1",
            "cycle_status": "active",
            "sla_policy_version": "sla_visual_v1_20260723",
            "cycle_version": 1,
            "reassignment_state": "active",
            "reassignment_decision_id": None,
            "automatic_reassignment_number": number,
            "previous_owner_user_ids": list(previous),
            "first_valid_management_at": None,
            "first_contact_attempt_at": None,
            "reassignment_protection_at": None,
        }

    def make_decision(cid, owner_id, target_id, target_name, number, previous, breach):
        decision_id = generate_decision_id(lead_id, cid, policy)
        return {
            "decision_id": decision_id,
            "lead_id": lead_id,
            "current_assignment_cycle_id": cid,
            "previous_owner_user_id": owner_id,
            "previous_owner_user_ids": list(previous),
            "selected_user_id": target_id,
            "selected_user_display_name": target_name,
            "assignment_number": number,
            "automatic_reassignment_number": number,
            "policy_version": policy,
            "policy_branch": "REGION_JPC_MARIA_HERNAN",
            "selection_rule": "E2E",
            "candidate_user_ids": [target_id],
            "candidate_scores_snapshot": [{"user_id": target_id, "score": 1}],
            "source_cycle_sla_breached_at": breach,
            "sla_breached_at": breach,
            "reassignment_cutover_at": Config.CRM_SLA_REASSIGNMENT_CUTOVER_AT,
            "cutover_eligible": True,
            "cutover_policy_version": CUTOVER_POLICY_VERSION,
        }

    try:
        db["usuarios"].insert_many([
            {"_id": "user-maria", "nombre": "María Paz Galleguillos", "is_active": True},
            {"_id": "user-hernan", "nombre": "Hernán Castro", "is_active": True},
            {"_id": "user-third", "nombre": "Ejecutivo Tercero", "is_active": True},
        ])
        source_id = "source-paula"
        assigned = now - timedelta(days=1)
        breach1 = now - timedelta(hours=2)
        db["leads"].insert_one({
            "_id": lead_id,
            "ejecutivo_asignado": "Paula Morales",
            "prospecto": {"ejecutivo": "Paula Morales"},
            "lifecycle": {"current_assignment_cycle_id": source_id},
            "pipeline_stage": "NEW",
            "stage": "NEW",
        })
        source = make_cycle(source_id, "user-paula", "Paula Morales", assigned, 0, [])
        source["sla_breached_at"] = breach1
        cycles.insert_one(source)

        first = make_decision(source_id, "user-paula", "user-maria", "María Paz Galleguillos", 0, ["user-paula"], breach1)
        result1 = execute_sla_reassignment_transaction(db, first, evaluated_at=now)
        assert result1.outcome == "APPLIED"

        destination1 = result1.destination_cycle_id
        cycles.update_one(
            {"assignment_cycle_id": destination1},
            {"$set": {
                "assigned_at": now - timedelta(hours=4),
                "sla_started_at": now - timedelta(hours=4),
                "owner_notified_at": now - timedelta(hours=4),
                "reassignment_state": "active",
                # Simulate a legacy persisted deadline that differs from the
                # fresh evaluator reconstruction.  Both instants remain
                # post-cutover; this must not block the repeated transition.
                "sla_breached_at": now - timedelta(hours=2),
                "sla_expired_at": now - timedelta(hours=2),
            }},
        )
        second = make_decision(
            destination1, "user-maria", "user-hernan", "Hernán Castro", 1,
            ["user-paula", "user-maria"], now - timedelta(hours=1),
        )
        result2 = execute_sla_reassignment_transaction(db, second, evaluated_at=now)
        assert result2.outcome == "APPLIED"
        assert result2.automatic_reassignment_number == 2

        replay = execute_sla_reassignment_transaction(db, second, evaluated_at=now)
        assert replay.outcome == "ALREADY_APPLIED"
        assert replay.idempotent_replay is True

        destination2 = result2.destination_cycle_id
        cycles.update_one(
            {"assignment_cycle_id": destination2},
            {"$set": {
                "assigned_at": now - timedelta(hours=4),
                "sla_started_at": now - timedelta(hours=4),
                "owner_notified_at": now - timedelta(hours=4),
                "reassignment_state": "active",
                "sla_breached_at": now - timedelta(minutes=30),
            }},
        )
        third = make_decision(
            destination2, "user-hernan", "user-third", "Ejecutivo Tercero", 2,
            ["user-paula", "user-maria", "user-hernan"], now - timedelta(minutes=30),
        )
        result3 = execute_sla_reassignment_transaction(db, third, evaluated_at=now)
        assert result3.outcome == "APPLIED"
        assert result3.automatic_reassignment_number == 3
        assert cycles.count_documents({"lead_id": lead_id, "cycle_status": "active"}) == 1
        assert db["crm_sla_reassignment_audit_v1"].count_documents({"lead_id": lead_id}) == 3
    finally:
        Config.CRM_SLA_REASSIGNMENT_ENABLED = old_flags["enabled"]
        Config.CRM_SLA_TRANSACTION_GATE_ENABLED = old_flags["gate"]
        Config.CRM_SLA_REASSIGNMENT_CUTOVER_AT = old_flags["cutover"]
        Config.IS_PRODUCTION = old_flags["production"]
        client.drop_database(db.name)
        client.close()
