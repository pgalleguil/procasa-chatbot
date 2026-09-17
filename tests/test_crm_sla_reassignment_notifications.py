from datetime import datetime, timezone

import mongomock
import pytest

from chatbot.crm_notifications import COLLECTION, individual_identity
from chatbot.crm_sla_reassignment_notifications import (
    SLA_REASSIGNED_TO,
    mark_new_owner_notification_delivered,
    rearm_stale_new_owner_notification,
)


def test_rearm_reuses_the_same_stale_notification_identity():
    db = mongomock.MongoClient()["test"]
    lead_id = "lead-1"
    cycle_id = "cycle-new"
    recipient_id = "user-new"
    db["leads"].insert_one({
        "_id": lead_id,
        "ejecutivo_asignado": "Ejecutivo Nuevo",
        "assignment_mirror_owner_user_id": recipient_id,
        "lifecycle": {
            "current_assignment_cycle_id": cycle_id,
            "assignment_cycle_id": cycle_id,
            "assigned_to_user_id": recipient_id,
        },
    })
    db["crm_assignment_cycles"].insert_one({
        "_id": cycle_id,
        "assignment_cycle_id": cycle_id,
        "lead_id": lead_id,
        "assigned_to_user_id": recipient_id,
        "cycle_status": "active",
        "unassigned_at": None,
    })
    identity = individual_identity(
        lead_id=lead_id,
        assignment_cycle_id=cycle_id,
        notification_type=SLA_REASSIGNED_TO,
        recipient_user_id=recipient_id,
    )
    db[COLLECTION].insert_one({
        "_id": "notification-1",
        "individual_identity": identity,
        "notification_type": SLA_REASSIGNED_TO,
        "notification_role": "new_owner",
        "lead_id": lead_id,
        "assignment_cycle_id": cycle_id,
        "recipient_user_id": recipient_id,
        "state": "stale_not_sent",
        "payload": {"message": "test"},
        "provider_message_id": None,
        "actually_delivered": False,
    })

    result = rearm_stale_new_owner_notification(
        db,
        lead_id=lead_id,
        assignment_cycle_id=cycle_id,
        recipient_user_id=recipient_id,
        now=datetime(2026, 9, 17, 16, 0, tzinfo=timezone.utc),
    )
    assert result["status"] == "rearmed"
    stored = db[COLLECTION].find_one({"_id": "notification-1"})
    assert stored["state"] == "pending"
    assert stored["individual_identity"] == identity
    assert stored["repair_reason"] == "STALE_NEW_OWNER_NOTIFICATION_RECOVERY"

    second = rearm_stale_new_owner_notification(
        db,
        lead_id=lead_id,
        assignment_cycle_id=cycle_id,
        recipient_user_id=recipient_id,
    )
    assert second["status"] == "not_recoverable"


def test_delivery_activates_destination_clock_once_and_uses_delivery_timestamp():
    db = mongomock.MongoClient()["test"]
    delivered_at = datetime(2026, 9, 17, 16, 5, tzinfo=timezone.utc)
    db["leads"].insert_one({
        "_id": "lead-2",
        "ejecutivo_asignado": "Ejecutivo Nuevo",
        "assignment_mirror_owner_user_id": "user-new",
        "lifecycle": {
            "current_assignment_cycle_id": "cycle-new",
            "assignment_cycle_id": "cycle-new",
            "assigned_to_user_id": "user-new",
        },
    })
    db["crm_assignment_cycles"].insert_one({
        "_id": "cycle-new",
        "assignment_cycle_id": "cycle-new",
        "lead_id": "lead-2",
        "assigned_to_user_id": "user-new",
        "cycle_status": "active",
        "reassignment_state": "AWAITING_OWNER_NOTIFICATION",
        "owner_notified_at": None,
        "sla_started_at": None,
    })
    db[COLLECTION].insert_one({
        "_id": "notification-2",
        "individual_identity": individual_identity(
            lead_id="lead-2",
            assignment_cycle_id="cycle-new",
            notification_type=SLA_REASSIGNED_TO,
            recipient_user_id="user-new",
        ),
        "notification_type": SLA_REASSIGNED_TO,
        "notification_role": "new_owner",
        "lead_id": "lead-2",
        "assignment_cycle_id": "cycle-new",
        "recipient_user_id": "user-new",
        "state": "sent",
        "provider_message_id": "provider-2",
        "actually_delivered": True,
    })

    first = mark_new_owner_notification_delivered(
        db,
        notification_id="notification-2",
        provider_message_id="provider-2",
        delivered_at=delivered_at,
    )
    assert first["status"] == "activated"
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-new"})
    lead = db["leads"].find_one({"_id": "lead-2"})
    assert cycle["reassignment_state"] == "active"
    expected_mongo_time = delivered_at.replace(tzinfo=None)
    assert cycle["owner_notified_at"] == expected_mongo_time
    assert cycle["sla_started_at"] == expected_mongo_time
    assert lead["lifecycle"]["owner_notified_at"] == expected_mongo_time
    assert lead["lifecycle"]["sla_started_at"] == expected_mongo_time

    second = mark_new_owner_notification_delivered(
        db,
        notification_id="notification-2",
        provider_message_id="provider-2",
        delivered_at=datetime(2026, 9, 17, 16, 6, tzinfo=timezone.utc),
    )
    assert second["status"] == "already_active"
    assert db[COLLECTION].count_documents({}) == 1


def test_provider_id_alone_cannot_start_destination_sla_clock():
    db = mongomock.MongoClient()["test"]
    db[COLLECTION].insert_one({
        "_id": "notification-unconfirmed",
        "notification_type": SLA_REASSIGNED_TO,
        "notification_role": "new_owner",
        "provider_message_id": "provider-unconfirmed",
        "actually_delivered": False,
    })
    result = mark_new_owner_notification_delivered(
        db,
        notification_id="notification-unconfirmed",
        provider_message_id="provider-unconfirmed",
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "notification_delivery_not_confirmed"


@pytest.mark.parametrize(
    "state",
    ["stale_not_sent", "failed_retryable", "failed_recipient", "failed_validation"],
)
def test_recovery_reuses_same_identity_for_all_retryable_terminal_states(state):
    db = mongomock.MongoClient()["test"]
    lead_id = "lead-recovery"
    cycle_id = "cycle-recovery"
    recipient_id = "user-new"
    db["leads"].insert_one({
        "_id": lead_id,
        "ejecutivo_asignado": "Ejecutivo Nuevo",
        "assignment_mirror_owner_user_id": recipient_id,
        "lifecycle": {
            "current_assignment_cycle_id": cycle_id,
            "assignment_cycle_id": cycle_id,
            "assigned_to_user_id": recipient_id,
        },
    })
    db["crm_assignment_cycles"].insert_one({
        "_id": cycle_id,
        "assignment_cycle_id": cycle_id,
        "lead_id": lead_id,
        "assigned_to_user_id": recipient_id,
        "cycle_status": "active",
        "unassigned_at": None,
    })
    identity = individual_identity(
        lead_id=lead_id,
        assignment_cycle_id=cycle_id,
        notification_type=SLA_REASSIGNED_TO,
        recipient_user_id=recipient_id,
    )
    db[COLLECTION].insert_one({
        "_id": "notification-recovery",
        "individual_identity": identity,
        "notification_type": SLA_REASSIGNED_TO,
        "notification_role": "new_owner",
        "lead_id": lead_id,
        "assignment_cycle_id": cycle_id,
        "recipient_user_id": recipient_id,
        "state": state,
        "payload": {"message": "test"},
        "provider_message_id": None,
        "actually_delivered": False,
    })

    result = rearm_stale_new_owner_notification(
        db,
        lead_id=lead_id,
        assignment_cycle_id=cycle_id,
        recipient_user_id=recipient_id,
    )
    assert result["status"] == "rearmed"
    stored = db[COLLECTION].find_one({"_id": "notification-recovery"})
    assert stored["state"] == "pending"
    assert stored["individual_identity"] == identity
    assert db[COLLECTION].count_documents({}) == 1
