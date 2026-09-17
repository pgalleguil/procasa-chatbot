from datetime import datetime, timezone

import mongomock

from chatbot.crm_notifications import COLLECTION, individual_identity
from chatbot.crm_sla_reassignment_notifications import (
    SLA_REASSIGNED_TO,
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
    assert second["status"] == "not_stale"
