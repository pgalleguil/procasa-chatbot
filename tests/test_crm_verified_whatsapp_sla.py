"""CRM first-SLA integration for signed, owned, persisted executive WA sends.

Synthetic Mongo / provider only. No network calls, personal chats or real sends.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import mongomock
import pytest
from bson import ObjectId

from config import Config
from chatbot.crm_verified_whatsapp_sla import credit_verified_outbound
from chatbot import crm_executive_outbound as outgoing
from chatbot import executive_whatsapp_audit as webhook_audit

LEAD_ID = ObjectId("690e13577fb5678251136b01")
OWNER = str(ObjectId("6a681413140190dde11f26d1"))
CYCLE_ID = "wa-current-cycle-1"
SESSION_ID = "133517"
PHONE = "+56996762064"


@pytest.fixture
def data(monkeypatch):
    db = mongomock.MongoClient().procasa
    now = datetime.now(timezone.utc)
    db["leads"].insert_one({
        "_id": LEAD_ID, "phone": PHONE,
        "lifecycle": {
            "assigned_to_user_id": OWNER,
            "current_assignment_cycle_id": CYCLE_ID,
        },
        "messages": [], "conversation_id": "office-123",
    })
    db["crm_assignment_cycles"].insert_one({
        "_id": ObjectId("690e13577fb5678251136b02"),
        "lead_id": LEAD_ID, "assignment_cycle_id": CYCLE_ID,
        "assigned_to_user_id": OWNER, "assigned_to_display_name": "Ejecutivo",
        "assigned_at": now - timedelta(minutes=15),
        "cycle_status": "active", "unassigned_at": None,
    })
    db["usuarios"].insert_one({
        "_id": ObjectId(OWNER), "rol": "agente",
        "is_active": True, "wasender": {"session_id": 133517},
    })
    monkeypatch.setattr(Config, "CRM_SLA_TRANSACTION_GATE_ENABLED", False)
    return db, now


def message(now, *, provider_id="wa-human-1", source="whatsapp_direct",
            role="agent", actor_type="human_agent", cycle_id=CYCLE_ID,
            owner=OWNER, session=SESSION_ID):
    return {
        "message_id": "canonical-" + provider_id,
        "content": "Mensaje de prueba de envío",
        "role": role, "actor_type": actor_type,
        "source": source, "channel": "executive_whatsapp",
        "assignment_cycle_id": cycle_id, "assigned_executive_id": owner,
        "wasender_session_id": session, "provider_message_id": provider_id,
        "timestamp": now.isoformat(), "timestamp_source": "provider",
    }


def assert_first_management(db):
    cycle = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": CYCLE_ID})
    lead = db["leads"].find_one({"_id": LEAD_ID})
    assert cycle["sla_first_management_status"] == "completed"
    assert cycle["first_valid_management_at"]
    assert lead["lifecycle"]["first_valid_management_at"]
    assert lead["contact_attempted"] is True
    assert lead["effective_contact"] is False
    assert lead["management_status"] == "managed_waiting_response"
    results = list(db["crm_management_results"].find({}))
    assert len(results) == 1
    assert results[0]["result_type"] == "MESSAGE_SENT_WAITING_RESPONSE"
    assert results[0]["source"] == "executive_whatsapp_verified"
    events = list(db["crm_events"].find({"type": "SEND_WA_LEAD"}))
    assert len(events) == 1


def test_authorized_persisted_human_send_stops_first_sla_once(data):
    db, now = data
    db["leads"].update_one({"_id": LEAD_ID}, {"$push": {"messages": message(now)}})
    args = dict(lead_id=LEAD_ID, assignment_cycle_id=CYCLE_ID,
                actor_user_id=OWNER, session_id=SESSION_ID,
                provider_message_id="wa-human-1")
    assert credit_verified_outbound(db, **args) == "credited"
    assert_first_management(db)
    assert credit_verified_outbound(db, **args) == "already_managed"
    assert_first_management(db)


@pytest.mark.parametrize("role,actor_type,source", [
    ("user", "customer", "executive_whatsapp"),
    ("assistant", "automation", "crm_hot_auto_api"),
    ("agent", "human_agent", "crm_hot_auto_api"),
    ("agent", "human_agent", "price_drop_campaign_api"),
])
def test_incoming_or_automatic_or_campaign_cannot_credit_human_sla(
    data, role, actor_type, source,
):
    db, now = data
    db["leads"].update_one({"_id": LEAD_ID}, {"$push": {
        "messages": message(now, role=role, actor_type=actor_type, source=source),
    }})
    assert credit_verified_outbound(
        db, lead_id=LEAD_ID, assignment_cycle_id=CYCLE_ID,
        actor_user_id=OWNER, session_id=SESSION_ID,
        provider_message_id="wa-human-1",
    ) == "unverified"
    assert not list(db["crm_management_results"].find({}))
    assert db["crm_assignment_cycles"].find_one({}) .get("first_valid_management_at") is None


def test_no_message_no_sla_and_wrong_cycle_or_owner_fail_closed(data):
    db, now = data
    args = dict(lead_id=LEAD_ID, assignment_cycle_id=CYCLE_ID,
                actor_user_id=OWNER, session_id=SESSION_ID,
                provider_message_id="wa-human-1")
    assert credit_verified_outbound(db, **args) == "unverified"
    db["leads"].update_one({"_id": LEAD_ID}, {"$push": {"messages": message(now)}})
    assert credit_verified_outbound(db, **{**args, "actor_user_id": str(ObjectId())}) == "unverified"
    assert credit_verified_outbound(db, **{**args, "session_id": "133526"}) == "unverified"
    assert credit_verified_outbound(db, **{**args, "provider_message_id": "wrong"}) == "unverified"
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": CYCLE_ID},
        {"$set": {"cycle_status": "closed", "unassigned_at": now}},
    )
    assert credit_verified_outbound(db, **args) == "stale"
    assert not list(db["crm_events"].find({}))


def test_existing_formal_management_is_never_downgraded(data):
    db, now = data
    db["leads"].update_one({"_id": LEAD_ID}, {"$push": {"messages": message(now)},
        "$set": {"management_status": "managed_contacted", "effective_contact": True}})
    db["crm_assignment_cycles"].update_one({"assignment_cycle_id": CYCLE_ID},
         {"$set": {"first_valid_management_at": now - timedelta(minutes=5)}})
    assert credit_verified_outbound(
        db, lead_id=LEAD_ID, assignment_cycle_id=CYCLE_ID,
        actor_user_id=OWNER, session_id=SESSION_ID,
        provider_message_id="wa-human-1",
    ) == "already_managed"
    lead = db["leads"].find_one({"_id": LEAD_ID})
    assert lead["management_status"] == "managed_contacted"
    assert lead["effective_contact"] is True
    assert not list(db["crm_management_results"].find({}))


def test_direct_crm_api_send_accepted_and_saved_stops_sla(data, monkeypatch):
    db, now = data
    monkeypatch.setenv("PROCASA_EXEC_CRM_DIRECT_SEND_ENABLED", "1")
    monkeypatch.setenv("PROCASA_EXEC_CRM_SEND_SESSION_IDS", SESSION_ID)
    monkeypatch.setattr(outgoing, "_session", lambda _db, _sid: {
        "session_id": SESSION_ID, "user_id": OWNER,
        "capture_enabled": True, "token": "synthetic-token",
    })
    monkeypatch.setattr(outgoing, "_authorized_lead", lambda _db, phone, user_id: (
        db["leads"].find_one({"_id": LEAD_ID}),
        db["crm_assignment_cycles"].find_one({"assignment_cycle_id": CYCLE_ID}),
    ))
    monkeypatch.setattr(outgoing, "lead_must_not_be_contacted", lambda _lead: False)

    response = SimpleNamespace(
        status_code=200, content=b"{}", json=lambda: {
            "success": True, "data": {"msgId": "api-synthetic-100"},
        },
    )
    result = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER,
        message="Hola, respuesta comercial",
        idempotency_key="synthetic-idempotency-100",
        post=lambda *_args, **_kwargs: response,
    )
    assert result == {
        "status": "accepted", "provider_message_id": "api-synthetic-100",
        "recorded": True, "sla_management": "credited",
    }
    assert_first_management(db)
    assert db["leads"].find_one({"_id": LEAD_ID})["messages"][0]["source"] == "crm_manual_api"
    assert list(db[outgoing.OUTBOX].find({}))[0]["canonical_recorded"] is True


def test_signed_outgoing_webhook_saved_credits_sla_and_duplicate_does_not(data):
    db, now = data
    session = {"user_id": OWNER, "session_id": SESSION_ID}
    lead = db["leads"].find_one({"_id": LEAD_ID})
    cycle = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": CYCLE_ID})
    parsed = {
        "from_me": True, "content": "Consulta enviada",
        "timestamp": now.isoformat(), "timestamp_source": "provider",
        "provider_message_id": "webhook-synthetic-001",
    }
    assert webhook_audit._record_authorized(
        db, session=session, phone=PHONE,
        parsed=parsed, lead=lead, cycle=cycle,
    ) == "stored"
    assert_first_management(db)
    assert webhook_audit._record_authorized(
        db, session=session, phone=PHONE,
        parsed=parsed, lead=lead, cycle=cycle,
    ) == "duplicate_or_reassigned"
    assert_first_management(db)


def test_reconciled_crm_api_id_is_valid_only_with_saved_echo(data):
    db, now = data
    row = message(now, provider_id="wa-hex-key-1")
    row["crm_api_provider_message_id"] = "api-number-2"
    db["leads"].update_one({"_id": LEAD_ID}, {"$push": {"messages": row}})
    assert credit_verified_outbound(
        db, lead_id=LEAD_ID, assignment_cycle_id=CYCLE_ID,
        actor_user_id=OWNER, session_id=SESSION_ID,
        provider_message_id="api-number-2",
    ) == "credited"
    assert_first_management(db)
