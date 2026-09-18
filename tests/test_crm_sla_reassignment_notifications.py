from datetime import datetime, timezone

import mongomock
import pytest

from chatbot.crm_notifications import COLLECTION, individual_identity
from chatbot.crm_sla_reassignment_notifications import (
    SLA_REASSIGNED_TO,
    COLLECTION as SLA_COLLECTION,
    mark_new_owner_notification_delivered,
    migrate_legacy_sla_reassignment_to_waiting,
    process_one_sla_reassignment_sync,
    record_sla_reassignment_delivery_status,
    record_sla_reassignment_delivery_status_webhook,
    rearm_stale_new_owner_notification,
)
from chatbot.crm_sla_reassignment_transaction import build_current_cycle_owner_mirror_repair
from chatbot.crm_sla_reassignment_worker import canonical_expiration_recheck


def _waiting_delivery_fixture(*, notification_id="delivery-notification", provider_id="provider-delivery"):
    db = mongomock.MongoClient()["test"]
    assigned_at = datetime(2026, 9, 17, 15, 0, tzinfo=timezone.utc)
    db["leads"].insert_one({
        "_id": "lead-delivery",
        "ejecutivo_asignado": "Ejecutivo Nuevo",
        "assignment_mirror_owner_user_id": "user-new",
        "lifecycle": {
            "current_assignment_cycle_id": "cycle-delivery",
            "assignment_cycle_id": "cycle-delivery",
            "assigned_to_user_id": "user-new",
            "assigned_to_display_name": "Ejecutivo Nuevo",
            "sla_started_at": None,
            "owner_notified_at": None,
        },
    })
    db["crm_assignment_cycles"].insert_one({
        "_id": "cycle-delivery",
        "assignment_cycle_id": "cycle-delivery",
        "lead_id": "lead-delivery",
        "assigned_to_user_id": "user-new",
        "assigned_to_display_name": "Ejecutivo Nuevo",
        "assigned_at": assigned_at,
        "cycle_status": "active",
        "reassignment_state": "AWAITING_OWNER_NOTIFICATION",
        "owner_notified_at": None,
        "sla_started_at": None,
    })
    db[SLA_COLLECTION].insert_one({
        "_id": notification_id,
        "individual_identity": individual_identity(
            lead_id="lead-delivery",
            assignment_cycle_id="cycle-delivery",
            notification_type=SLA_REASSIGNED_TO,
            recipient_user_id="user-new",
        ),
        "notification_type": SLA_REASSIGNED_TO,
        "notification_role": "new_owner",
        "lead_id": "lead-delivery",
        "assignment_cycle_id": "cycle-delivery",
        "recipient_user_id": "user-new",
        "state": "sent",
        "provider_message_id": provider_id,
        "actually_delivered": False,
        "delivery_status": "sent",
    })
    return db


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


def test_provider_accepted_stays_sent_and_waiting_until_delivery_callback():
    db = mongomock.MongoClient()["test"]
    db["leads"].insert_one({
        "_id": "lead-accepted",
        "assignment_mirror_owner_user_id": "user-new",
        "lifecycle": {"current_assignment_cycle_id": "cycle-accepted", "assigned_to_user_id": "user-new"},
    })
    db["crm_assignment_cycles"].insert_one({
        "_id": "cycle-accepted", "assignment_cycle_id": "cycle-accepted", "lead_id": "lead-accepted",
        "assigned_to_user_id": "user-new", "cycle_status": "active",
        "reassignment_state": "AWAITING_OWNER_NOTIFICATION", "owner_notified_at": None, "sla_started_at": None,
    })
    db["usuarios"].insert_one({"_id": "user-new", "telefono": "+56911111111", "is_active": True})
    db[SLA_COLLECTION].insert_one({
        "_id": "notification-accepted", "individual_identity": "accepted", "notification_type": SLA_REASSIGNED_TO,
        "notification_role": "new_owner", "lead_id": "lead-accepted", "assignment_cycle_id": "cycle-accepted",
        "recipient_user_id": "user-new", "state": "pending", "payload": {"message": "test"},
        "provider_message_id": None, "actually_delivered": False,
        "message_domain": "commercial_notification",
    })
    result = process_one_sla_reassignment_sync(
        db,
        worker_id="delivery-worker",
        sender=lambda *_: {"success": True, "provider_message_id": "accepted-1", "http_status": 200},
    )
    stored = db[SLA_COLLECTION].find_one({"_id": "notification-accepted"})
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-accepted"})
    assert result["status"] == "sent"
    assert stored["state"] == "sent"
    assert stored["actually_delivered"] is False
    assert cycle["reassignment_state"] == "AWAITING_OWNER_NOTIFICATION"
    assert cycle.get("sla_started_at") is None


def test_delivery_callback_uses_event_timestamp_and_read_activates_once():
    db = _waiting_delivery_fixture()
    first_at = datetime(2026, 9, 17, 15, 4, tzinfo=timezone.utc)
    first = record_sla_reassignment_delivery_status(
        db, provider_message_id="provider-delivery", delivery_status="read", delivered_at=first_at,
    )
    assert first["status"] == "delivered"
    assert first["activation"]["status"] == "activated"
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-delivery"})
    notification = db[SLA_COLLECTION].find_one({"_id": "delivery-notification"})
    assert cycle["reassignment_state"] == "active"
    assert cycle["sla_started_at"] == first_at.replace(tzinfo=None)
    assert notification["actually_delivered"] is True
    assert notification["delivered_at"] == first_at.replace(tzinfo=None)

    second = record_sla_reassignment_delivery_status(
        db, provider_message_id="provider-delivery", delivery_status="delivered",
        delivered_at=datetime(2026, 9, 17, 15, 6, tzinfo=timezone.utc),
    )
    assert second["status"] == "delivered"
    assert db[SLA_COLLECTION].find_one({"_id": "delivery-notification"})["delivered_at"] == first_at.replace(tzinfo=None)


def test_sent_without_delivery_or_failed_delivery_keeps_sla_clock_off():
    db = _waiting_delivery_fixture(notification_id="notification-status", provider_id="provider-status")
    accepted = record_sla_reassignment_delivery_status(
        db, provider_message_id="provider-status", delivery_status="sent",
    )
    assert accepted["status"] == "sent"
    assert db["crm_assignment_cycles"].find_one({"_id": "cycle-delivery"})["sla_started_at"] is None
    failed = record_sla_reassignment_delivery_status(
        db, provider_message_id="provider-status", delivery_status="failed",
    )
    assert failed["status"] == "failed"
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-delivery"})
    assert cycle["reassignment_state"] == "AWAITING_OWNER_NOTIFICATION"
    assert cycle.get("sla_started_at") is None


def test_delivery_after_owner_change_is_stale_and_cannot_activate():
    db = _waiting_delivery_fixture(notification_id="notification-stale", provider_id="provider-stale")
    db["leads"].update_one(
        {"_id": "lead-delivery"},
        {"$set": {"assignment_mirror_owner_user_id": "other-user", "lifecycle.assigned_to_user_id": "other-user"}},
    )
    result = record_sla_reassignment_delivery_status(
        db, provider_message_id="provider-stale", delivery_status="delivered",
        delivered_at=datetime(2026, 9, 17, 15, 10, tzinfo=timezone.utc),
    )
    assert result["status"] == "stale"
    assert result["activation"]["status"] == "blocked"
    assert db["crm_assignment_cycles"].find_one({"_id": "cycle-delivery"})["reassignment_state"] == "AWAITING_OWNER_NOTIFICATION"


def test_canonical_provider_webhook_bridges_read_with_provider_event_timestamp():
    db = _waiting_delivery_fixture(notification_id="notification-webhook", provider_id="provider-webhook")
    result = record_sla_reassignment_delivery_status_webhook(
        {
            "event": "messages.update",
            "data": {
                "key": {"id": "provider-webhook"},
                "update": {"status": "read", "timestamp": 1790000000},
            },
        },
        db=db,
    )
    assert result["status"] == "delivered"
    assert result["activation"]["status"] == "activated"
    notification = db[SLA_COLLECTION].find_one({"_id": "notification-webhook"})
    assert notification["actually_delivered"] is True
    assert notification["delivered_at"].year == 2026


def test_frice_provider_delivery_timestamp_is_the_next_sla_start():
    db = _waiting_delivery_fixture(notification_id="frice-notification", provider_id="80500131")
    delivered_at = datetime(2026, 9, 17, 15, 0, tzinfo=timezone.utc)
    result = record_sla_reassignment_delivery_status(
        db,
        provider_message_id="80500131",
        delivery_status="delivered",
        delivered_at=delivered_at,
    )
    assert result["activation"]["status"] == "activated"
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-delivery"})
    lead = db["leads"].find_one({"_id": "lead-delivery"})
    assert db[SLA_COLLECTION].find_one({"_id": "frice-notification"})["actually_delivered"] is True
    assert cycle["sla_started_at"] == delivered_at.replace(tzinfo=None)
    expiration = canonical_expiration_recheck(
        cycle,
        lead,
        now=datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc),
    )
    assert expiration.started_at is not None
    assert expiration.started_at == delivered_at
    assert expiration.expired is True


def test_waiting_cycle_mirror_repair_preserves_null_sla_clock():
    db = _waiting_delivery_fixture(notification_id="notification-mirror", provider_id="provider-mirror")
    lead = db["leads"].find_one({"_id": "lead-delivery"})
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-delivery"})
    lead["ejecutivo_asignado"] = "Owner Viejo"
    lead["lifecycle"]["assigned_to_display_name"] = "Owner Viejo"
    lead["lifecycle"]["sla_started_at"] = datetime(2026, 9, 17, 15, 1)
    update = build_current_cycle_owner_mirror_repair(
        lead=lead,
        active_cycles=[cycle],
        repaired_at=datetime(2026, 9, 17, 15, 5, tzinfo=timezone.utc),
    )
    assert update is not None
    assert update["$set"]["lifecycle.assigned_to_user_id"] == "user-new"
    assert update["$set"]["lifecycle.sla_started_at"] is None
    assert update["$set"]["lifecycle.owner_notified_at"] is None


def test_maria_legacy_migration_clears_old_clock_and_reuses_exact_notification():
    db = mongomock.MongoClient()["test"]
    lead_id = "6aab2a66da2187c900af35f9"
    cycle_id = "sla-reassignment:2b6d721a01dd17305818bfb91fff403b01b8348aef7e5512b24ddbf1e503dc68"
    recipient_id = "69c19b98fbbbf113235ba844"
    old_start = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)
    db["leads"].insert_one({
        "_id": lead_id,
        "ejecutivo_asignado": "María Paz Galleguillos",
        "assignment_mirror_owner_user_id": recipient_id,
        "lifecycle": {
            "current_assignment_cycle_id": cycle_id,
            "assignment_cycle_id": cycle_id,
            "assigned_to_user_id": recipient_id,
            "assigned_to_display_name": "María Paz Galleguillos",
            "sla_started_at": old_start,
        },
    })
    db["crm_assignment_cycles"].insert_one({
        "_id": cycle_id, "assignment_cycle_id": cycle_id, "lead_id": lead_id,
        "assigned_to_user_id": recipient_id, "assigned_to_display_name": "María Paz Galleguillos",
        "assigned_at": datetime(2026, 9, 16, 14, 55, tzinfo=timezone.utc),
        "cycle_status": "active", "reassignment_state": "active", "sla_started_at": old_start,
        "owner_notified_at": old_start, "automatic_reassignment_number": 1,
        "sla_reassignment_decision_id": "legacy-decision",
    })
    identity = individual_identity(
        lead_id=lead_id, assignment_cycle_id=cycle_id, notification_type=SLA_REASSIGNED_TO,
        recipient_user_id=recipient_id,
    )
    db[SLA_COLLECTION].insert_one({
        "_id": "maria-notification", "individual_identity": identity, "notification_type": SLA_REASSIGNED_TO,
        "notification_role": "new_owner", "lead_id": lead_id, "assignment_cycle_id": cycle_id,
        "recipient_user_id": recipient_id, "state": "stale_not_sent", "provider_message_id": None,
        "actually_delivered": False, "payload": {"message": "legacy"},
    })
    migrated = migrate_legacy_sla_reassignment_to_waiting(
        db,
        lead_id=lead_id,
        assignment_cycle_id=cycle_id,
        recipient_user_id=recipient_id,
        now=datetime(2026, 9, 17, 15, 20, tzinfo=timezone.utc),
    )
    assert migrated["status"] == "migrated"
    cycle = db["crm_assignment_cycles"].find_one({"_id": cycle_id})
    lead = db["leads"].find_one({"_id": lead_id})
    assert cycle["reassignment_state"] == "AWAITING_OWNER_NOTIFICATION"
    assert cycle["sla_started_at"] is None
    assert cycle["owner_notified_at"] is None
    assert lead["lifecycle"]["sla_started_at"] is None
    assert lead["lifecycle"]["current_assignment_cycle_id"] == cycle_id
    assert db[SLA_COLLECTION].count_documents({"individual_identity": identity}) == 1
    rearmed = rearm_stale_new_owner_notification(
        db, lead_id=lead_id, assignment_cycle_id=cycle_id, recipient_user_id=recipient_id,
    )
    assert rearmed["status"] == "rearmed"
    assert db[SLA_COLLECTION].find_one({"_id": "maria-notification"})["state"] == "pending"
    repeated = migrate_legacy_sla_reassignment_to_waiting(
        db,
        lead_id=lead_id,
        assignment_cycle_id=cycle_id,
        recipient_user_id=recipient_id,
        now=datetime(2026, 9, 17, 15, 21, tzinfo=timezone.utc),
    )
    assert repeated["status"] == "already_waiting"


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
