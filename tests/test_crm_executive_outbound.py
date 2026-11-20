"""Fail-closed tests: a linked session may only send to its own assigned lead."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

from bson import ObjectId
from pymongo.errors import DuplicateKeyError
import pytest

from chatbot import crm_executive_outbound as outgoing


LEAD_ID = ObjectId("690e13577fb5678251136b01")
OWNER = str(ObjectId("6a681413140190dde11f26d1"))
CYCLE = "authorized-cycle-1"


class LeadCollection:
    def __init__(self):
        self.document = {
            "_id": LEAD_ID, "phone": "+56996762064", "conversation_id": "conv",
            "lead_temperature_effective": "HOT",
            "lifecycle": {
                "assigned_to_user_id": OWNER,
                "current_assignment_cycle_id": CYCLE,
            },
            "messages": [],
        }
        self.last_mutation = None

    def find_one(self, query):
        return deepcopy(self.document) if query.get("_id") == LEAD_ID else None

    def update_one(self, query, mutation):
        if query.get("_id") != LEAD_ID:
            return SimpleNamespace(modified_count=0)
        lifecycle = self.document["lifecycle"]
        if (str(lifecycle["assigned_to_user_id"]) != str(query.get("lifecycle.assigned_to_user_id"))
                or lifecycle["current_assignment_cycle_id"] != query.get("lifecycle.current_assignment_cycle_id")):
            return SimpleNamespace(modified_count=0)
        prohibited = query.get("$nor") or []
        if prohibited:
            check = prohibited[0].get("messages", {}).get("$elemMatch", {})
            if any(m.get("wasender_session_id") == check.get("wasender_session_id")
                   and m.get("provider_message_id") == check.get("provider_message_id")
                   for m in self.document["messages"]):
                return SimpleNamespace(modified_count=0)
        self.last_mutation = deepcopy(mutation)
        self.document["messages"].append(deepcopy(mutation["$push"]["messages"]))
        return SimpleNamespace(modified_count=1)


class Outbox:
    def __init__(self):
        self.docs = {}

    def insert_one(self, value):
        if value["_id"] in self.docs:
            raise DuplicateKeyError("duplicate")
        self.docs[value["_id"]] = deepcopy(value)

    def update_one(self, query, mutation):
        self.docs[query["_id"]].update(mutation["$set"])

    def find_one(self, query, projection=None):
        return self.docs.get(query["_id"])


class Users:
    def find_one(self, query, projection):
        if str(query.get("_id")) != OWNER:
            return None
        return {"_id": ObjectId(OWNER), "wasender": {"session_id": 133517}}


@pytest.fixture
def context(monkeypatch):
    db = {
        "leads": LeadCollection(),
        outgoing.OUTBOX: Outbox(),
        "usuarios": Users(),
    }
    cycle = {
        "assignment_cycle_id": CYCLE, "assigned_to_user_id": OWNER,
        "cycle_status": "active", "unassigned_at": None,
    }
    monkeypatch.setenv("PROCASA_EXEC_CRM_DIRECT_SEND_ENABLED", "1")
    monkeypatch.setenv("PROCASA_EXEC_CRM_SEND_SESSION_IDS", "133517")
    monkeypatch.setattr(outgoing, "active_assignment_cycle", lambda _db, _id: cycle)
    monkeypatch.setattr(outgoing, "_authorized_lead",
                        lambda _db, phone, user_id: (db["leads"].document, cycle))
    monkeypatch.setattr(outgoing, "_session", lambda db, sid: {
        "session_id": sid, "user_id": OWNER, "capture_enabled": True, "token": "private-session-token",
    })
    monkeypatch.setattr(outgoing, "lead_must_not_be_contacted", lambda _: False)
    return db, cycle


def _response(msg="WA-PROVIDER-1", status_code=200, success=True):
    return SimpleNamespace(
        status_code=status_code, content=b"{}",
        json=lambda: {"success": success, "data": {"msgId": msg}},
    )


def test_direct_send_defaults_disabled(context, monkeypatch):
    db, cycle = context
    monkeypatch.delenv("PROCASA_EXEC_CRM_DIRECT_SEND_ENABLED")
    called = []
    with pytest.raises(outgoing.OutboundBlocked, match="crm_direct_send_disabled"):
        outgoing.send_manual(db, lead_id=LEAD_ID, actor_user_id=OWNER,
                             message="Hola", idempotency_key="request-1234",
                             post=lambda *a, **k: called.append(True))
    assert called == []
    assert not db[outgoing.OUTBOX].docs


def test_wrong_owner_or_disabled_session_cannot_send(context, monkeypatch):
    db, _ = context
    with pytest.raises(outgoing.OutboundBlocked, match="current_assignment_required"):
        outgoing.send_manual(db, lead_id=LEAD_ID, actor_user_id=str(ObjectId()),
                             message="Hola", idempotency_key="request-1234")
    monkeypatch.setenv("PROCASA_EXEC_CRM_SEND_SESSION_IDS", "133526")
    with pytest.raises(outgoing.OutboundBlocked, match="executive_session_not_enabled"):
        outgoing.send_manual(db, lead_id=LEAD_ID, actor_user_id=OWNER,
                             message="Hola", idempotency_key="request-1234")
    assert not db[outgoing.OUTBOX].docs


@pytest.mark.parametrize("allowlist", ["*", "133517,133517", "133517,", "bad", "133517,secret"])
def test_invalid_send_allowlist_fails_closed(context, monkeypatch, allowlist):
    db, _ = context
    monkeypatch.setenv("PROCASA_EXEC_CRM_SEND_SESSION_IDS", allowlist)
    assert outgoing._enabled_session_ids() == set()
    with pytest.raises(outgoing.OutboundBlocked):
        outgoing.send_manual(db, lead_id=LEAD_ID, actor_user_id=OWNER,
                             message="Hola", idempotency_key="request-1234")


def test_provider_accepted_stores_one_manual_message_and_no_sla_side_effect(context):
    db, _ = context
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _response()
    result = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER,
        message="Hola, recibí tu consulta", idempotency_key="request-1234",
        post=post,
    )
    assert result == {"status": "accepted", "provider_message_id": "WA-PROVIDER-1", "recorded": True}
    assert len(calls) == 1
    assert calls[0][0].endswith("/send-message")
    assert calls[0][1]["json"] == {"to": "+56996762064", "text": "Hola, recibí tu consulta"}
    assert calls[0][1]["headers"]["Authorization"] == "Bearer private-session-token"
    messages = db["leads"].document["messages"]
    assert len(messages) == 1
    assert messages[0]["source"] == "crm_manual_api"
    assert messages[0]["direction"] == "outgoing"
    assert messages[0]["actor_type"] == "human_agent"
    assert messages[0]["assigned_executive_id"] == OWNER
    assert messages[0]["provider_message_id"] == "WA-PROVIDER-1"
    assert messages[0]["wasender_session_id"] == "133517"
    assert messages[0]["timestamp"].endswith("+00:00")
    assert db["leads"].last_mutation["$set"]["commercial_intelligence.pending"]
    assert not any("sla" in k or "first_valid_management" in k for k in db["leads"].last_mutation["$set"])
    assert next(iter(db[outgoing.OUTBOX].docs.values()))["canonical_recorded"] is True


def test_same_idempotency_key_never_resends_even_if_provider_was_uncertain(context):
    db, _ = context
    calls = []
    def unknown(*a, **k):
        calls.append(True)
        raise TimeoutError("synthetic")
    first = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER, message="Hola",
        idempotency_key="request-1234", post=unknown,
    )
    assert first == {"status": "delivery_unknown"}
    second = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER, message="Hola",
        idempotency_key="request-1234",
        post=lambda *args, **kwargs: pytest.fail("duplicate send"),
    )
    assert second["status"] == "duplicate"
    assert second["previous_state"] == "delivery_unknown"
    assert len(calls) == 1
    assert not db["leads"].document["messages"]


def test_provider_failure_or_missing_id_never_fakes_sla_or_sent_message(context):
    db, _ = context
    failed = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER, message="Hola",
        idempotency_key="request-failed", post=lambda *a, **k: _response(status_code=403, success=False),
    )
    accepted_unconfirmed = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER, message="Hola",
        idempotency_key="request-unconfirmed", post=lambda *a, **k: _response(msg=None),
    )
    assert failed["status"] == "rejected"
    assert accepted_unconfirmed["status"] == "accepted_without_provider_id"
    assert db["leads"].document["messages"] == []


def test_customer_optout_blocks_direct_api(context, monkeypatch):
    db, _ = context
    monkeypatch.setattr(outgoing, "lead_must_not_be_contacted", lambda _: True)
    with pytest.raises(outgoing.OutboundBlocked, match="customer_contact_blocked"):
        outgoing.send_manual(
            db, lead_id=LEAD_ID, actor_user_id=OWNER, message="Hola",
            idempotency_key="request-1234",
            post=lambda *a, **k: pytest.fail("contact denied"),
        )


def test_reassignment_between_claim_and_provider_call_is_blocked(context, monkeypatch):
    db, cycle = context
    calls = 0
    def switch_cycle(_db, _oid):
        nonlocal calls
        calls += 1
        if calls > 1:
            return dict(cycle, assigned_to_user_id="other-owner")
        return cycle
    monkeypatch.setattr(outgoing, "active_assignment_cycle", switch_cycle)
    result = outgoing.send_manual(db, lead_id=LEAD_ID, actor_user_id=OWNER,
                                  message="Hola", idempotency_key="request-1234",
                                  post=lambda *a, **k: pytest.fail("reassigned lead was contacted"))
    assert result == {"status": "blocked", "reason": "current_assignment_required"}
    assert db["leads"].document["messages"] == []



def test_webhook_echo_before_api_acceptance_does_not_create_duplicate(context, monkeypatch):
    import mongomock
    db, cycle = context
    lead_copy = deepcopy(db["leads"].document)
    lead_copy["messages"] = [{
        "message_id": "whatsapp-echo-first", "role": "agent", "actor_type": "human_agent",
        "source": "whatsapp_direct", "channel": "executive_whatsapp",
        "wasender_session_id": "133517", "assigned_executive_id": OWNER,
        "assignment_cycle_id": CYCLE, "provider_message_id": "3EB0ECHO",
        "content": "Hola, recibí tu consulta ",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }]
    coll = mongomock.MongoClient().test.echo_leads
    coll.insert_one(lead_copy)
    db["leads"] = coll
    monkeypatch.setattr(outgoing, "_authorized_lead", lambda _db, phone, user_id: (
        coll.find_one({"_id": LEAD_ID}), cycle,
    ))
    result = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER,
        message="Hola, recibí tu consulta", idempotency_key="request-echo-first",
        post=lambda *_a, **_kw: _response(msg="api-98648827"),
    )
    assert result == {"status": "accepted", "provider_message_id": "api-98648827", "recorded": True}
    messages = coll.find_one({"_id": LEAD_ID})["messages"]
    assert len(messages) == 1
    assert messages[0]["provider_message_id"] == "3EB0ECHO"
    assert messages[0]["crm_api_provider_message_id"] == "api-98648827"
    assert next(iter(db[outgoing.OUTBOX].docs.values()))["canonical_recorded"] is True
