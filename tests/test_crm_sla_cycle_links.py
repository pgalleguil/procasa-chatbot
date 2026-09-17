"""Regression tests for cycle-bound SLA CRM links."""
from datetime import datetime, timezone

import mongomock
import pytest
from bson import ObjectId

from config import Config
from chatbot.crm_sla_cycle_links import (
    INVALID_LINK_CODE,
    SlaCycleLinkError,
    build_sla_cycle_url,
    issue_sla_cycle_link_token,
    validate_sla_cycle_link,
    verify_sla_cycle_link_token,
)


@pytest.fixture
def sla_secret(monkeypatch):
    monkeypatch.setattr(Config, "FOLLOWUP_TOKEN_SECRET", "sla-cycle-test-secret-" + "x" * 32)
    monkeypatch.setattr(Config, "IS_PRODUCTION", False)


def _db():
    return mongomock.MongoClient().get_database("crm")


def _fixture():
    db = _db()
    lead_id = ObjectId()
    db["leads"].insert_one({
        "_id": lead_id,
        "phone": "+56911111111",
        "email": "private@example.invalid",
        "ejecutivo_asignado": "Owner One",
        "assignment_mirror_owner_user_id": "owner-1",
        "lifecycle": {
            "current_assignment_cycle_id": "cycle-1",
            "assigned_to_user_id": "owner-1",
        },
    })
    db["crm_assignment_cycles"].insert_one({
        "assignment_cycle_id": "cycle-1",
        "lead_id": lead_id,
        "assigned_to_user_id": "owner-1",
        "cycle_status": "active",
        "unassigned_at": None,
        "assigned_at": datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
    })
    return db, lead_id


def _token(lead_id, recipient="owner-1", cycle="cycle-1"):
    return issue_sla_cycle_link_token(
        lead_id=lead_id,
        recipient_user_id=recipient,
        assignment_cycle_id=cycle,
    )


def test_token_is_signed_opaque_and_deterministic(sla_secret):
    db, lead_id = _fixture()
    token_a = _token(lead_id)
    token_b = _token(lead_id)

    assert token_a == token_b
    assert str(lead_id) not in build_sla_cycle_url(
        lead_id=lead_id, recipient_user_id="owner-1", assignment_cycle_id="cycle-1",
    )
    payload = verify_sla_cycle_link_token(token_a)
    assert payload["lead_id"] == str(lead_id)
    assert payload["recipient_user_id"] == "owner-1"
    assert payload["assignment_cycle_id"] == "cycle-1"


def test_current_owner_opens_active_cycle_link(sla_secret):
    db, lead_id = _fixture()
    resolved = validate_sla_cycle_link(
        db, _token(lead_id), authenticated_user_id="owner-1",
    )
    assert str(resolved.lead["_id"]) == str(lead_id)
    assert resolved.cycle["assignment_cycle_id"] == "cycle-1"


def test_different_user_is_blocked_without_contact_data(sla_secret):
    db, lead_id = _fixture()
    with pytest.raises(SlaCycleLinkError) as exc:
        validate_sla_cycle_link(db, _token(lead_id), authenticated_user_id="owner-2")
    assert exc.value.code == INVALID_LINK_CODE
    assert exc.value.status_code == 403


def test_closed_cycle_invalidates_old_link(sla_secret):
    db, lead_id = _fixture()
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": "cycle-1"},
        {"$set": {"cycle_status": "reassigned", "unassigned_at": datetime.now(timezone.utc)}},
    )
    with pytest.raises(SlaCycleLinkError) as exc:
        validate_sla_cycle_link(db, _token(lead_id), authenticated_user_id="owner-1")
    assert exc.value.code == INVALID_LINK_CODE
    assert exc.value.status_code == 409

def test_reassignment_invalidates_old_link_and_new_cycle_link_works(sla_secret):
    db, lead_id = _fixture()
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": "cycle-1"},
        {"$set": {"cycle_status": "reassigned", "unassigned_at": datetime.now(timezone.utc)}},
    )
    db["leads"].update_one(
        {"_id": lead_id},
        {"$set": {
            "assignment_mirror_owner_user_id": "owner-2",
            "lifecycle.current_assignment_cycle_id": "cycle-2",
            "lifecycle.assigned_to_user_id": "owner-2",
        }},
    )
    db["crm_assignment_cycles"].insert_one({
        "assignment_cycle_id": "cycle-2",
        "lead_id": lead_id,
        "assigned_to_user_id": "owner-2",
        "cycle_status": "active",
        "unassigned_at": None,
    })

    with pytest.raises(SlaCycleLinkError) as old_exc:
        validate_sla_cycle_link(db, _token(lead_id), authenticated_user_id="owner-1")
    assert old_exc.value.status_code in {403, 409}

    resolved = validate_sla_cycle_link(
        db, _token(lead_id, recipient="owner-2", cycle="cycle-2"),
        authenticated_user_id="owner-2",
    )
    assert resolved.payload["assignment_cycle_id"] == "cycle-2"


def test_invalid_signature_is_blocked(sla_secret):
    db, lead_id = _fixture()
    token = _token(lead_id)
    with pytest.raises(SlaCycleLinkError) as exc:
        validate_sla_cycle_link(db, token[:-1] + ("a" if token[-1] != "a" else "b"), authenticated_user_id="owner-1")
    assert exc.value.code == INVALID_LINK_CODE
    assert exc.value.status_code == 403


def test_stale_owner_mirror_is_blocked(sla_secret):
    db, lead_id = _fixture()
    db["leads"].update_one(
        {"_id": lead_id}, {"$set": {"assigned_to_user_id": "stale-owner"}},
    )
    with pytest.raises(SlaCycleLinkError) as exc:
        validate_sla_cycle_link(db, _token(lead_id), authenticated_user_id="owner-1")
    assert exc.value.status_code == 409
