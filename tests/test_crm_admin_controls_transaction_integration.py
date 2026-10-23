from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import uuid

import pytest
from pymongo import MongoClient

from chatbot.crm_admin_controls import AdminControlError, admin_reassign_lead


@pytest.fixture
def transaction_db():
    uri = os.environ.get("CRM_ADMIN_MONGO_TEST_URI")
    if not uri:
        pytest.skip("CRM_ADMIN_MONGO_TEST_URI is not configured")
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        hello = client.admin.command("hello")
    except Exception as exc:
        client.close()
        pytest.fail(f"MongoDB transaction test service is unavailable: {type(exc).__name__}")
    if not hello.get("setName"):
        client.close()
        pytest.fail("MongoDB transaction test service must be a replica set")
    db = client[f"crm_admin_txn_test_{uuid.uuid4().hex}"]
    db["leads"].insert_one({
        "_id": "integration-lead",
        "oficina_id": 7,
        "ejecutivo_asignado": "Agente Inicial",
        "owner_user_id": "owner-initial",
        "lifecycle": {"current_assignment_cycle_id": "cycle-initial"},
    })
    db["usuarios"].insert_many([
        {"_id": "supervisor", "nombre": "Supervisión", "rol": "supervisor", "is_active": True, "office_id": 7},
        {"_id": "agent-a", "nombre": "Agente A", "rol": "agente", "is_active": True, "office_id": 7},
        {"_id": "agent-b", "nombre": "Agente B", "rol": "agente", "is_active": True, "office_id": 7},
    ])
    db["crm_assignment_cycles"].insert_one({
        "_id": "cycle-initial",
        "lead_id": "integration-lead",
        "assignment_cycle_id": "cycle-initial",
        "schema_version": "crm_assignment_cycle_v1",
        "cycle_status": "active",
        "unassigned_at": None,
        "assigned_to_user_id": "owner-initial",
        "assigned_to_display_name": "Agente Inicial",
        "assigned_at": datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc),
        "cycle_version": 1,
        "temperature_at_assignment": "HOT",
        "previous_owner_user_ids": [],
    })
    try:
        yield db
    finally:
        client.drop_database(db.name)
        client.close()


def _transfer(db, target, action_id):
    return admin_reassign_lead(
        db,
        lead_id="integration-lead",
        target_user_id=target,
        actor={"_id": "supervisor", "nombre": "Supervisión", "rol": "supervisor", "office_id": 7},
        reason=f"Prueba aislada {action_id}",
        action_id=action_id,
        hold_hours=2,
        emergency_handoff=True,
        expected_cycle_id="cycle-initial",
        now=datetime(2026, 10, 20, 14, 30, tzinfo=timezone.utc),
    )


def test_transaction_rolls_back_closed_cycle_when_destination_insert_fails(transaction_db):
    action_id = "rollback-" + uuid.uuid4().hex
    cycle_id = f"admin-emergency:{action_id}"
    transaction_db["crm_assignment_cycles"].insert_one({
        "_id": cycle_id,
        "lead_id": "unrelated-fixture",
        "assignment_cycle_id": cycle_id,
        "cycle_status": "closed",
        "unassigned_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
    })

    with pytest.raises(AdminControlError, match="transaction_failed"):
        _transfer(transaction_db, "agent-a", action_id)

    source = transaction_db["crm_assignment_cycles"].find_one({"_id": "cycle-initial"})
    lead = transaction_db["leads"].find_one({"_id": "integration-lead"})
    assert source["cycle_status"] == "active"
    assert source["unassigned_at"] is None
    assert lead["lifecycle"]["current_assignment_cycle_id"] == "cycle-initial"
    assert lead["ejecutivo_asignado"] == "Agente Inicial"
    assert transaction_db["crm_assignment_admin_events"].count_documents({"action_id": action_id}) == 0
    assert transaction_db["crm_assignment_cycles"].count_documents({
        "lead_id": "integration-lead", "cycle_status": "active", "unassigned_at": None,
    }) == 1


def test_concurrent_emergency_transfers_leave_one_canonical_active_cycle(transaction_db):
    suffix = uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_transfer, transaction_db, "agent-a", "race-a-" + suffix),
            pool.submit(_transfer, transaction_db, "agent-b", "race-b-" + suffix),
        ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=30))
            except AdminControlError as exc:
                outcomes.append(exc.code)

    assert sum(isinstance(item, dict) and item["status"] == "ok" for item in outcomes) == 1
    assert sum(item in {"cycle_changed", "transaction_failed"} for item in outcomes if isinstance(item, str)) == 1
    active = list(transaction_db["crm_assignment_cycles"].find({
        "lead_id": "integration-lead", "cycle_status": "active", "unassigned_at": None,
    }))
    assert len(active) == 1
    lead = transaction_db["leads"].find_one({"_id": "integration-lead"})
    assert lead["lifecycle"]["current_assignment_cycle_id"] == active[0]["assignment_cycle_id"]
    assert lead["owner_user_id"] == active[0]["assigned_to_user_id"]
    assert lead["ejecutivo_asignado"] == active[0]["assigned_to_display_name"]
    assert transaction_db["crm_assignment_admin_events"].count_documents({
        "lead_id": "integration-lead", "action": "emergency_reassign",
    }) == 1
