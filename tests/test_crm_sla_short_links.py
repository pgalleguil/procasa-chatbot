from bson import ObjectId
import mongomock
import pytest

from chatbot.crm_sla_cycle_links import (
    SHORT_LINK_COLLECTION,
    SHORT_LINK_PATH,
    SlaCycleLinkError,
    build_sla_short_url,
    ensure_sla_short_link,
    issue_sla_cycle_link_token,
    issue_sla_short_id,
    validate_sla_cycle_link,
    validate_sla_short_link,
)
from chatbot.crm_sla_reassignment_notifications import (
    _new_owner_message,
    _notification_context,
)


def _fixture_db():
    db = mongomock.MongoClient()["crm_sla_short_link_test"]
    lead_id = ObjectId()
    db["leads"].insert_one({
        "_id": lead_id,
        "prospecto": {
            "nombre": "Daniela Fixture",
            "codigo": "6006",
            "operacion": "Arriendo",
            "comuna": "Santiago",
        },
        "lead_temperature_effective": "NORMAL",
        "ejecutivo_asignado": "María Paz",
        "assigned_to_user_id": "user-1",
        "assignment_mirror_owner_user_id": "user-1",
        "lifecycle": {
            "current_assignment_cycle_id": "cycle-1",
            "assigned_to_user_id": "user-1",
            "assigned_to_display_name": "María Paz",
        },
    })
    db["crm_assignment_cycles"].insert_one({
        "assignment_cycle_id": "cycle-1",
        "lead_id": str(lead_id),
        "assigned_to_user_id": "user-1",
        "assigned_to_display_name": "María Paz",
        "cycle_status": "active",
        "unassigned_at": None,
        "temperature_at_assignment": "NORMAL",
        "property_code": "6006",
        "operation": "Arriendo",
        "commune": "Santiago",
    })
    return db, lead_id


def test_short_id_is_opaque_and_idempotent():
    db, lead_id = _fixture_db()
    lead_text = str(lead_id)

    first = ensure_sla_short_link(
        db,
        lead_id=lead_text,
        recipient_user_id="user-1",
        assignment_cycle_id="cycle-1",
    )
    second = ensure_sla_short_link(
        db,
        lead_id=lead_text,
        recipient_user_id="user-1",
        assignment_cycle_id="cycle-1",
    )

    assert first == second == issue_sla_short_id(
        lead_id=lead_text,
        recipient_user_id="user-1",
        assignment_cycle_id="cycle-1",
    )
    assert len(first) == 16
    assert lead_text not in first
    assert "user-1" not in first
    assert "cycle-1" not in first
    assert db[SHORT_LINK_COLLECTION].count_documents({}) == 1
    assert build_sla_short_url(
        lead_id=lead_text,
        recipient_user_id="user-1",
        assignment_cycle_id="cycle-1",
        base_url="https://crm.example",
    ) == f"https://crm.example{SHORT_LINK_PATH}{first}"


def test_short_link_reuses_canonical_access_and_blocks_wrong_recipient():
    db, lead_id = _fixture_db()
    ensure_sla_short_link(
        db, lead_id=str(lead_id), recipient_user_id="user-1", assignment_cycle_id="cycle-1"
    )
    short_id = issue_sla_short_id(
        lead_id=str(lead_id), recipient_user_id="user-1", assignment_cycle_id="cycle-1"
    )

    resolution = validate_sla_short_link(db, short_id, authenticated_user_id="user-1")
    assert str(resolution.lead["_id"]) == str(lead_id)
    assert resolution.cycle["assignment_cycle_id"] == "cycle-1"

    with pytest.raises(SlaCycleLinkError) as denied:
        validate_sla_short_link(db, short_id, authenticated_user_id="user-2")
    assert denied.value.status_code == 403


def test_short_link_invalidates_on_cycle_close_and_pointer_change():
    db, lead_id = _fixture_db()
    lead_text = str(lead_id)
    ensure_sla_short_link(
        db, lead_id=lead_text, recipient_user_id="user-1", assignment_cycle_id="cycle-1"
    )
    short_id = issue_sla_short_id(
        lead_id=lead_text, recipient_user_id="user-1", assignment_cycle_id="cycle-1"
    )

    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": "cycle-1"},
        {"$set": {"cycle_status": "reassigned", "unassigned_at": "closed"}},
    )
    with pytest.raises(SlaCycleLinkError) as closed:
        validate_sla_short_link(db, short_id, authenticated_user_id="user-1")
    assert closed.value.status_code == 409

    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": "cycle-1"},
        {"$set": {"cycle_status": "active", "unassigned_at": None}},
    )
    db["crm_assignment_cycles"].insert_one({
        "assignment_cycle_id": "cycle-2",
        "lead_id": lead_text,
        "assigned_to_user_id": "user-2",
        "cycle_status": "active",
        "unassigned_at": None,
    })
    db["leads"].update_one(
        {"_id": lead_id},
        {"$set": {
            "assigned_to_user_id": "user-2",
            "assignment_mirror_owner_user_id": "user-2",
            "lifecycle.current_assignment_cycle_id": "cycle-2",
            "lifecycle.assigned_to_user_id": "user-2",
        }},
    )
    with pytest.raises(SlaCycleLinkError) as moved:
        validate_sla_short_link(db, short_id, authenticated_user_id="user-1")
    assert moved.value.status_code == 409


def test_unknown_short_id_fails_closed_and_old_signed_link_still_works():
    db, lead_id = _fixture_db()
    with pytest.raises(SlaCycleLinkError) as unknown:
        validate_sla_short_link(db, "A" * 16, authenticated_user_id="user-1")
    assert unknown.value.status_code == 403

    old_token = issue_sla_cycle_link_token(
        lead_id=str(lead_id), recipient_user_id="user-1", assignment_cycle_id="cycle-1"
    )
    resolution = validate_sla_cycle_link(db, old_token, authenticated_user_id="user-1")
    assert resolution.cycle["assignment_cycle_id"] == "cycle-1"


def test_new_owner_message_uses_one_persisted_short_link():
    db, lead_id = _fixture_db()
    first = _notification_context(db, str(lead_id), "cycle-1")
    second = _notification_context(db, str(lead_id), "cycle-1")
    message = _new_owner_message(first)

    assert first["secure_url"] == second["secure_url"]
    assert "/crm/s/" in first["secure_url"]
    assert "/crm/sla-cycle/" not in first["secure_url"]
    assert "/crm/lead-id/" not in first["secure_url"]
    assert db[SHORT_LINK_COLLECTION].count_documents({}) == 1
    assert first["secure_url"] in message
