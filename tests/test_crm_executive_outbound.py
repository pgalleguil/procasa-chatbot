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
    monkeypatch.setattr(outgoing, "verify_provider_sender_identity", lambda *_a, **_kw: None)
    monkeypatch.setattr(outgoing, "credit_verified_outbound", lambda *_a, **_kw: "credited")
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
    assert result == {"status": "accepted", "provider_message_id": "WA-PROVIDER-1", "recorded": True,
                      "sla_management": "credited"}
    assert len(calls) == 1
    assert calls[0][0].endswith("/send-message")
    assert calls[0][1]["json"] == {"to": "+56996762064", "text": "Hola, recibí tu consulta"}
    assert calls[0][1]["headers"]["Authorization"] == "Bearer private-session-token"
    messages = db["leads"].document["messages"]
    assert len(messages) == 1
    assert messages[0]["source"] == "crm_manual_api"
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
    assert result == {"status": "accepted", "provider_message_id": "api-98648827", "recorded": True,
                      "sla_management": "credited"}
    messages = coll.find_one({"_id": LEAD_ID})["messages"]
    assert len(messages) == 1
    assert messages[0]["provider_message_id"] == "3EB0ECHO"
    assert messages[0]["crm_api_provider_message_id"] == "api-98648827"
    assert next(iter(db[outgoing.OUTBOX].docs.values()))["canonical_recorded"] is True


@pytest.mark.parametrize(
    "http_status,body,expected",
    [
        (401, {"success": False, "message": "Unauthorized"}, "authentication"),
        (402, {"success": False, "message": "Subscription expired"}, "subscription"),
        (429, {"success": False, "message": "Too many requests"}, "rate_limit"),
        (400, {"success": False, "message": "Session is not connected"}, "session_disconnected"),
        (422, {"success": False, "message": "Recipient not on WhatsApp"}, "recipient_not_registered"),
        (400, {"success": False, "message": "Validation failed"}, "invalid_request"),
        (409, {"success": False, "message": "Other error"}, "provider_rejected"),
    ],
)
def test_provider_rejections_are_classified_with_safe_codes_only(
        context, http_status, body, expected):
    db, _ = context
    def post(*_a, **_kw):
        return SimpleNamespace(status_code=http_status, content=b"error", json=lambda: body)
    result = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER, message="Hola",
        idempotency_key="request-code-test", post=post,
    )
    assert result == {"status": "rejected", "reason_code": expected}
    saved = next(iter(db[outgoing.OUTBOX].docs.values()))
    assert saved["provider_http_status"] == http_status
    assert saved["provider_error_reason"] == expected
    assert saved["provider_message_id"] is None
    assert not db["leads"].document["messages"]
    assert "Authorization" not in str(saved)
    assert "private-session-token" not in str(saved)


def test_server_error_cannot_be_retried_when_provider_delivery_is_uncertain(context):
    db, _ = context
    calls = []
    def post(*_a, **_kw):
        calls.append(1)
        return SimpleNamespace(
            status_code=503, content=b"error",
            json=lambda: {"success": False, "message": "Temporary outage"},
        )
    first = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER, message="Hola",
        idempotency_key="request-server-5xx", post=post,
    )
    second = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER, message="Hola",
        idempotency_key="request-server-5xx", post=post,
    )
    assert first == {"status": "delivery_unknown"}
    assert second["status"] == "duplicate"
    assert second["previous_state"] == "delivery_unknown"
    assert len(calls) == 1
    saved = next(iter(db[outgoing.OUTBOX].docs.values()))
    assert saved["provider_http_status"] == 503
    assert saved["provider_error_reason"] == "provider_unavailable"
    assert not db["leads"].document["messages"]


def test_non_json_provider_rejection_keeps_http_diagnostics(context):
    db, _ = context
    result = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER, message="Hola",
        idempotency_key="request-non-json", post=lambda *_a, **_kw: SimpleNamespace(
            status_code=400, content=b"invalid", json=lambda: (_ for _ in ()).throw(ValueError("bad json")),
        ),
    )
    assert result == {"status": "rejected", "reason_code": "invalid_request"}
    assert next(iter(db[outgoing.OUTBOX].docs.values()))["provider_http_status"] == 400


def test_ambiguous_success_http_without_provider_receipt_is_not_retryable(context):
    db, _ = context
    attempts = []
    def post(*_a, **_kw):
        attempts.append(1)
        return SimpleNamespace(status_code=200, content=b"not-json",
                               json=lambda: (_ for _ in ()).throw(ValueError("not json")))
    first = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER, message="Hola",
        idempotency_key="request-ambiguous-2xx", post=post,
    )
    second = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER, message="Hola",
        idempotency_key="request-ambiguous-2xx", post=post,
    )
    assert first == {"status": "delivery_unknown"}
    assert second["status"] == "duplicate"
    assert len(attempts) == 1
    assert next(iter(db[outgoing.OUTBOX].docs.values()))["provider_error_reason"] == "provider_response_unverifiable"
    assert not db["leads"].document["messages"]


SUPERVISOR = str(ObjectId("69796bc5bbebf240378eb73a"))


class UsersWithSupervisor(Users):
    def find_one(self, query, projection):
        if str(query.get("_id")) == SUPERVISOR:
            return {"_id": ObjectId(SUPERVISOR), "rol": "supervisor", "is_active": True}
        return super().find_one(query, projection)


def _enable_supervised_outbound(context, monkeypatch):
    db, cycle = context
    db["usuarios"] = UsersWithSupervisor()
    monkeypatch.setattr(
        outgoing, "resolve_crm_lead_access_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            access_allowed=True, action_permissions={"contact_whatsapp": True},
        ),
    )
    return db, cycle


def test_supervisor_send_uses_only_current_owner_session_and_preserves_real_actor(context, monkeypatch):
    db, _ = _enable_supervised_outbound(context, monkeypatch)
    admin = {"_id": ObjectId(SUPERVISOR), "rol": "supervisor"}
    info = outgoing.eligible_requester_session(
        db, lead=db["leads"].document, requester_user=admin,
    )
    assert info["owner_id"] == OWNER
    assert info["requester_id"] == SUPERVISOR
    assert info["delegated"] is True
    assert info["session"]["session_id"] == "133517"
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _response(msg="WA-SUPERVISOR-1")
    result = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER, requester_user=admin,
        message="Hola, atención comercial", idempotency_key="admin-request-1234",
        post=post,
    )
    assert result == {
        "status": "accepted", "provider_message_id": "WA-SUPERVISOR-1",
        "recorded": True, "delegated": True, "sla_management": "delegated",
    }
    assert len(calls) == 1
    assert calls[0][0].endswith("/send-message")
    assert calls[0][1]["headers"]["Authorization"] == "Bearer private-session-token"
    assert calls[0][1]["json"]["to"] == "+56996762064"
    record = db["leads"].document["messages"][0]
    assert record["role"] == "agent"
    assert record["actor_type"] == "human_supervisor"
    assert record["actor_id"] == SUPERVISOR
    assert record["assigned_executive_id"] == OWNER
    assert record["initiated_by_user_id"] == SUPERVISOR
    assert record["source"] == "crm_admin_delegated_api"
    assert record["wasender_session_id"] == "133517"
    assert record["provider_message_id"] == "WA-SUPERVISOR-1"
    dispatch = next(iter(db[outgoing.OUTBOX].docs.values()))
    assert dispatch["executive_id"] == OWNER
    assert dispatch["initiated_by_user_id"] == SUPERVISOR
    assert dispatch["mode"] == "manual_crm_admin_delegated"


@pytest.mark.parametrize("role", ["agente", "viewer", "customer", "", None])
def test_other_users_cannot_dispatch_from_another_executive_phone(context, monkeypatch, role):
    db, _ = _enable_supervised_outbound(context, monkeypatch)
    requester = {"_id": ObjectId(SUPERVISOR), "rol": role}
    with pytest.raises(outgoing.OutboundBlocked, match="requester_not_authorized"):
        outgoing.send_manual(
            db, lead_id=LEAD_ID, actor_user_id=OWNER,
            requester_user=requester, message="No enviar",
            idempotency_key="no-cross-user-send",
            post=lambda *_a, **_kw: pytest.fail("unauthorized external send"),
        )
    assert not db[outgoing.OUTBOX].docs


def test_supervisor_role_checked_against_database_not_client_snapshot(context, monkeypatch):
    db, _ = _enable_supervised_outbound(context, monkeypatch)
    class DowngradedUsers(Users):
        def find_one(self, query, projection):
            if str(query.get("_id")) == SUPERVISOR:
                return {"_id": ObjectId(SUPERVISOR), "rol": "agente", "is_active": True}
            return super().find_one(query, projection)
    db["usuarios"] = DowngradedUsers()
    with pytest.raises(outgoing.OutboundBlocked, match="requester_not_authorized"):
        outgoing.eligible_requester_session(
            db, lead=db["leads"].document,
            requester_user={"_id": ObjectId(SUPERVISOR), "rol": "supervisor"},
        )


def test_supervisor_does_not_override_canonical_crm_access_denial(context, monkeypatch):
    db, _ = _enable_supervised_outbound(context, monkeypatch)
    monkeypatch.setattr(
        outgoing, "resolve_crm_lead_access_context",
        lambda *_a, **_kw: SimpleNamespace(
            access_allowed=False, action_permissions={"contact_whatsapp": False},
        ),
    )
    with pytest.raises(outgoing.OutboundBlocked, match="requester_not_authorized"):
        outgoing.eligible_requester_session(
            db, lead=db["leads"].document,
            requester_user={"_id": ObjectId(SUPERVISOR), "rol": "supervisor"},
        )


def test_supervisor_cannot_send_after_ownership_changes_before_provider_boundary(context, monkeypatch):
    db, cycle = _enable_supervised_outbound(context, monkeypatch)
    admin = {"_id": ObjectId(SUPERVISOR), "rol": "supervisor"}
    counts = 0
    def current_cycle(_db, _id):
        nonlocal counts
        counts += 1
        # The first two reads resolve the initiating owner; subsequent reads
        # revalidate immediately before the external HTTP request.
        if counts > 2:
            return dict(cycle, assigned_to_user_id="other-owner")
        return cycle
    monkeypatch.setattr(outgoing, "active_assignment_cycle", current_cycle)
    result = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER, requester_user=admin,
        message="No enviar tras reasignación", idempotency_key="admin-reassigned-1234",
        post=lambda *_a, **_kw: pytest.fail("stale lead was contacted"),
    )
    assert result == {"status": "blocked", "reason": "current_assignment_required"}
    assert not db["leads"].document["messages"]


def test_supervisor_cannot_bypass_optout_or_missing_owner_session(context, monkeypatch):
    db, _ = _enable_supervised_outbound(context, monkeypatch)
    admin = {"_id": ObjectId(SUPERVISOR), "rol": "supervisor"}
    monkeypatch.setattr(outgoing, "lead_must_not_be_contacted", lambda _: True)
    with pytest.raises(outgoing.OutboundBlocked, match="customer_contact_blocked"):
        outgoing.send_manual(
            db, lead_id=LEAD_ID, actor_user_id=OWNER,
            requester_user=admin, message="Bloqueado",
            idempotency_key="admin-optout-1234",
            post=lambda *_a, **_kw: pytest.fail("optout bypassed"),
        )
    monkeypatch.setattr(outgoing, "lead_must_not_be_contacted", lambda _: False)
    monkeypatch.setenv("PROCASA_EXEC_CRM_SEND_SESSION_IDS", "133526")
    with pytest.raises(outgoing.OutboundBlocked, match="executive_session_not_enabled"):
        outgoing.send_manual(
            db, lead_id=LEAD_ID, actor_user_id=OWNER,
            requester_user=admin, message="Bloqueado",
            idempotency_key="admin-disabled-1234",
            post=lambda *_a, **_kw: pytest.fail("unlinked session sent"),
        )


def test_supervisor_webhook_first_reconciles_single_canonical_row(context, monkeypatch):
    import mongomock
    db, cycle = _enable_supervised_outbound(context, monkeypatch)
    admin = {"_id": ObjectId(SUPERVISOR), "rol": "supervisor"}
    lead_copy = deepcopy(db["leads"].document)
    lead_copy["messages"] = [{
        "message_id": "webhook-before-supervisor",
        "role": "agent", "actor_type": "human_agent",
        "source": "whatsapp_direct", "channel": "executive_whatsapp",
        "wasender_session_id": "133517", "assigned_executive_id": OWNER,
        "assignment_cycle_id": CYCLE, "provider_message_id": "3EB0SUPERVISOR",
        "content": "Hola, revisamos la propiedad",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }]
    coll = mongomock.MongoClient().test.supervisor_echo
    coll.insert_one(lead_copy)
    db["leads"] = coll
    monkeypatch.setattr(outgoing, "_authorized_lead", lambda _db, phone, user_id: (
        coll.find_one({"_id": LEAD_ID}), cycle,
    ))
    result = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER,
        requester_user=admin, message="Hola, revisamos la propiedad",
        idempotency_key="supervisor-echo-1234",
        post=lambda *_a, **_kw: _response(msg="api-echo-receipt"),
    )
    assert result["status"] == "accepted"
    assert result["recorded"] is True
    assert result["delegated"] is True
    msgs = coll.find_one({"_id": LEAD_ID})["messages"]
    assert len(msgs) == 1
    assert msgs[0]["source"] == "crm_admin_delegated_api"
    assert msgs[0]["actor_type"] == "human_supervisor"
    assert msgs[0]["actor_id"] == SUPERVISOR
    assert msgs[0]["crm_api_provider_message_id"] == "api-echo-receipt"


def test_supervised_api_message_echo_is_reconciled_without_duplicate():
    from chatbot.executive_whatsapp_echo import find_unique_echo_match
    original = {
        "message_id": "api-before-webhook", "role": "agent",
        "actor_type": "human_supervisor", "actor_id": SUPERVISOR,
        "assigned_executive_id": OWNER, "assignment_cycle_id": CYCLE,
        "source": "crm_admin_delegated_api", "channel": "executive_whatsapp",
        "wasender_session_id": "133517", "provider_message_id": "api-receipt",
        "content": "Hola, mensaje supervisado",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    matched = find_unique_echo_match(
        [original], source="crm_admin_delegated_api",
        content="Hola, mensaje supervisado", session_id="133517",
        cycle_id=CYCLE, owner_id=OWNER, occurred_at=datetime.now(timezone.utc),
    )
    assert matched == original
    original["provider_echo_message_id"] = "3EB0WEBHOOK"
    assert find_unique_echo_match(
        [original], source="crm_admin_delegated_api",
        content="Hola, mensaje supervisado", session_id="133517",
        cycle_id=CYCLE, owner_id=OWNER, occurred_at=datetime.now(timezone.utc),
    ) is None


# Provider identity checks are READ ONLY; never send a test WhatsApp.
def test_sender_attestation_accepts_matching_provider_jid():
    provider = SimpleNamespace(
        status_code=200, json=lambda: {
            "success": True,
            "data": {"id": "56911112222:12@s.whatsapp.net", "name": "Hernán"},
        },
    )
    seen = []
    def get(url, **kwargs):
        seen.append((url, kwargs))
        return provider
    outgoing.verify_provider_sender_identity(
        {"session_id": "133517", "token": "secret-only-in-memory"},
        expected_phone="+56911112222", get=get,
    )
    assert len(seen) == 1
    assert seen[0][0].endswith("/user")
    assert seen[0][1]["headers"]["Authorization"] == "Bearer secret-only-in-memory"


def test_sender_attestation_rejects_crossed_mariela_token():
    def wrong_user(*_args, **_kwargs):
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"success": True, "data": {"id": "56933334444@s.whatsapp.net"}},
        )
    with pytest.raises(outgoing.OutboundBlocked, match="sender_identity_mismatch"):
        outgoing.verify_provider_sender_identity(
            {"session_id": "133517", "token": "not-a-real-key"},
            expected_phone="+56911112222", get=wrong_user,
        )


@pytest.mark.parametrize("provider", [
    SimpleNamespace(status_code=401, json=lambda: {}),
    SimpleNamespace(status_code=200, json=lambda: {"success": False}),
    SimpleNamespace(status_code=200, json=lambda: {"success": True, "data": {}}),
    SimpleNamespace(status_code=200, json=lambda: {"success": True, "data": {"id": "not-a-phone@lid"}}),
])
def test_sender_attestation_fails_closed_without_verified_provider_identity(provider):
    with pytest.raises(outgoing.OutboundBlocked, match="sender_identity_unverified"):
        outgoing.verify_provider_sender_identity(
            {"session_id": "133526", "token": "not-a-real-key"},
            expected_phone="+56933334444",
            get=lambda *_a, **_kw: provider,
        )


def test_sender_attestation_transport_failure_fails_closed():
    def unavailable(*_a, **_kw):
        raise TimeoutError("synthetic - never logged")
    with pytest.raises(outgoing.OutboundBlocked, match="sender_identity_unverified"):
        outgoing.verify_provider_sender_identity(
            {"session_id": "133517", "token": "not-a-real-key"},
            expected_phone="+56911112222", get=unavailable,
        )


def test_sender_identity_error_blocks_send_before_provider_post(context, monkeypatch):
    db, _ = context
    def identity_mismatch(*_a, **_kw):
        raise outgoing.OutboundBlocked("sender_identity_mismatch")
    monkeypatch.setattr(outgoing, "verify_provider_sender_identity", identity_mismatch)
    with pytest.raises(outgoing.OutboundBlocked, match="sender_identity_mismatch"):
        outgoing.send_manual(
            db, lead_id=LEAD_ID, actor_user_id=OWNER,
            message="No enviar por token cruzado",
            idempotency_key="cross-sender-block-1234",
            post=lambda *_a, **_kw: pytest.fail("wrong account sent WhatsApp"),
        )
    assert not db[outgoing.OUTBOX].docs
    assert not db["leads"].document["messages"]


def test_other_executives_keep_manual_link_and_cannot_use_pilot(monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from chatbot import crm_executive_outbound_api as api

    async def auth(_request):
        return {"_id": ObjectId(OWNER), "nombre": "Hernán Castro", "rol": "agente"}
    monkeypatch.setattr(api, "_user", auth)
    monkeypatch.setenv("PROCASA_PABLO_CRM_WASENDER_API_ENABLED", "1")
    assert asyncio.run(api.crm_executive_whatsapp_available(object(), str(LEAD_ID))) == {
        "available": False, "method": "wa_me", "fallback": True,
    }
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.crm_executive_whatsapp_send(object()))
    assert exc.value.status_code == 403
    assert exc.value.detail == "pilot_user_not_authorized"


def test_pablo_pilot_off_never_enables_api(monkeypatch):
    import asyncio
    from chatbot import crm_executive_outbound_api as api

    async def auth(_request):
        return {"_id": ObjectId(api._PABLO_USER_ID),
                "nombre": "Pablo Galleguillos", "rol": "agente"}
    monkeypatch.setattr(api, "_user", auth)
    monkeypatch.delenv("PROCASA_PABLO_CRM_WASENDER_API_ENABLED", raising=False)
    assert asyncio.run(api.crm_executive_whatsapp_available(object(), str(LEAD_ID))) == {
        "available": False, "method": "wa_me",
    }


def test_pablo_pilot_available_and_manual_send_is_scoped_and_idempotent(monkeypatch):
    import asyncio
    from chatbot import crm_executive_outbound_api as api
    from chatbot import storage

    user = {"_id": ObjectId(api._PABLO_USER_ID),
            "nombre": "Pablo Galleguillos", "rol": "agente"}
    monkeypatch.setenv("PROCASA_PABLO_CRM_WASENDER_API_ENABLED", "1")
    async def auth(_request):
        return user
    monkeypatch.setattr(api, "_user", auth)
    import mongomock
    db = mongomock.MongoClient().test
    db["leads"].insert_one({"_id": LEAD_ID, "lifecycle": {
        "current_assignment_cycle_id": CYCLE}})
    monkeypatch.setattr(storage, "get_db", lambda: db)
    monkeypatch.setattr(api, "_session_status", lambda _session, get=None: "connected")
    checks = []
    def eligible(given_db, *, user, lead_id):
        checks.append((given_db, str(user["_id"]), lead_id))
        return {"session": {"session_id": "133286"}}
    monkeypatch.setattr(api, "_eligible_pablo", eligible)
    assert asyncio.run(api.crm_executive_whatsapp_available(object(), str(LEAD_ID))) == {
        "available": True, "method": "executive_wasender_api",
    }
    sent = []
    def fake_send(given_db, **kwargs):
        sent.append((given_db, kwargs))
        return {"status": "accepted", "recorded": True,
                "provider_message_id": "provider-123"}
    monkeypatch.setattr(api, "send_manual", fake_send)
    class Request:
        async def json(self):
            return {"lead_id": str(LEAD_ID), "text": "Hola desde el CRM",
                    "idempotency_key": "pilot_pablo_key_12345"}
    result = asyncio.run(api.crm_executive_whatsapp_send(Request()))
    assert result == {
        "status": "accepted", "recorded": True,
        "provider_message_id": "provider-123",
    }
    assert checks == [(db, api._PABLO_USER_ID, str(LEAD_ID))] * 2
    assert len(sent) == 1
    assert sent[0][0] is db
    assert sent[0][1] == {
        "lead_id": str(LEAD_ID), "actor_user_id": api._PABLO_USER_ID,
        "message": "Hola desde el CRM",
        "idempotency_key": "pilot_pablo_key_12345",
    }


def test_pablo_pilot_fails_closed_when_session_or_lead_not_ready(monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from chatbot import crm_executive_outbound_api as api
    from chatbot import storage

    user = {"_id": ObjectId(api._PABLO_USER_ID),
            "nombre": "Pablo Galleguillos", "rol": "agente"}
    monkeypatch.setenv("PROCASA_PABLO_CRM_WASENDER_API_ENABLED", "1")
    async def auth(_request):
        return user
    monkeypatch.setattr(api, "_user", auth)
    import mongomock
    db = mongomock.MongoClient().test
    db["leads"].insert_one({"_id": LEAD_ID, "phone": "+56996762064", "ejecutivo_asignado": api._PABLO_NAME, "lifecycle": {
        "current_assignment_cycle_id": CYCLE}})
    monkeypatch.setattr(storage, "get_db", lambda: db)
    def fail(_db, *, user, lead_id):
        raise outgoing.OutboundBlocked("pilot_session_mismatch")
    monkeypatch.setattr(api, "_eligible_pablo", fail)
    monkeypatch.setattr(api, "send_manual",
                        lambda *_a, **_k: pytest.fail("No provider POST"))
    assert asyncio.run(api.crm_executive_whatsapp_available(object(), str(LEAD_ID))) == {
        "available": False, "method": "retry", "reason": "pilot_session_mismatch",
    }
    class Request:
        async def json(self):
            return {"lead_id": str(LEAD_ID), "text": "Hola",
                    "idempotency_key": "attempt-12345"}
    assert asyncio.run(api.crm_executive_whatsapp_send(Request())) == {
        "status": "retry", "reason": "pilot_session_mismatch",
    }


def test_pablo_pilot_attests_own_phone_not_erika_account(monkeypatch):
    from chatbot import crm_executive_outbound_api as api
    monkeypatch.setenv("PROCASA_PABLO_CRM_WASENDER_API_ENABLED", "1")
    user = {"_id": ObjectId(api._PABLO_USER_ID),
            "nombre": "Pablo Galleguillos", "rol": "agente"}
    class Users:
        def find_one(self, query, projection):
            assert query["_id"] == ObjectId(api._PABLO_USER_ID)
            return {"telefono": "+56912345678"}
    db = {"leads": SimpleNamespace(find_one=lambda _query: {"_id": LEAD_ID}),
          "usuarios": Users()}
    monkeypatch.setattr(api, "eligible_manual_session",
                        lambda *_a, **_kw: {
                            "session": {"session_id": "133286", "token": "test-token"},
                        })
    attested = []
    monkeypatch.setattr(api, "verify_provider_sender_identity",
                        lambda session, expected_phone, force:
                            attested.append((session["session_id"], expected_phone, force)))
    info = api._eligible_pablo(db, user=user, lead_id=str(LEAD_ID))
    assert info["session"]["session_id"] == "133286"
    assert attested == [("133286", "+56912345678", True)]



@pytest.fixture
def pablo_fallback_context(monkeypatch):
    import mongomock
    from chatbot import crm_executive_outbound_api as api, storage

    db = mongomock.MongoClient().test
    db["leads"].insert_one({"_id": LEAD_ID, "phone": "+56996762064", "ejecutivo_asignado": api._PABLO_NAME, "lifecycle": {
        "assigned_to_user_id": api._PABLO_USER_ID,
        "current_assignment_cycle_id": CYCLE,
    }})
    user = {"_id": ObjectId(api._PABLO_USER_ID),
            "nombre": api._PABLO_NAME, "rol": "agente"}
    monkeypatch.setenv("PROCASA_PABLO_CRM_WASENDER_API_ENABLED", "1")
    monkeypatch.setattr(storage, "get_db", lambda: db)
    monkeypatch.setattr(api, "_eligible_pablo", lambda _db, *, user, lead_id:
                        {"session": {"session_id": "133286", "token": "fake-token"}})
    return api, db, user


@pytest.mark.parametrize("status", ["disconnected", "need_scan", "logged_out",
                                    "connecting", "unavailable", "expired"])
def test_preflight_offline_offers_manual_link_without_provider_send(
        pablo_fallback_context, monkeypatch, status):
    api, db, user = pablo_fallback_context
    monkeypatch.setattr(api, "_session_status", lambda session, get=None: status)
    assessed = api._assess(db, user=user, lead_id=str(LEAD_ID))
    if status in ("connecting", "unavailable"):
        assert assessed == {"available": False, "method": "retry", "reason": "session_status_unverified"}
    else:
        assert assessed == {"available": False, "method": "wa_me", "fallback": True, "reason": "session_unavailable"}
    assert db[api._INCIDENTS].count_documents({"status": "open"}) == 1
    assert db[api.OUTBOX].count_documents({}) == 0


def test_connected_session_resolves_preflight_incident(
        pablo_fallback_context, monkeypatch):
    api, db, user = pablo_fallback_context
    monkeypatch.setattr(api, "_session_status",
                        lambda session, get=None: "disconnected")
    api._assess(db, user=user, lead_id=str(LEAD_ID))
    monkeypatch.setattr(api, "_session_status",
                        lambda session, get=None: "connected")
    assert api._assess(db, user=user, lead_id=str(LEAD_ID)) == {
        "available": True, "method": "executive_wasender_api",
    }
    assert db[api._INCIDENTS].count_documents({"status": "open"}) == 0
    assert db[api._INCIDENTS].count_documents({"status": "resolved"}) == 1


def test_uncertain_history_does_not_block_new_api_messages(
        pablo_fallback_context, monkeypatch):
    api, db, user = pablo_fallback_context
    monkeypatch.setattr(api, "_session_status",
                        lambda session, get=None: "connected")
    db[api.OUTBOX].insert_one({
        "_id": "lost_response", "lead_id": LEAD_ID,
        "assignment_cycle_id": CYCLE,
        "executive_id": api._PABLO_USER_ID, "state": "delivery_unknown",
    })
    assert api._assess(db, user=user, lead_id=str(LEAD_ID)) == {"method": "executive_wasender_api", "available": True}
    assert db[api._INCIDENTS].count_documents({"reason": "delivery_unconfirmed"}) == 0


def test_absent_or_wrong_owner_is_blocked_without_manual_bypass(
        pablo_fallback_context, monkeypatch):
    api, db, user = pablo_fallback_context
    def forbidden(_db, *, user, lead_id):
        raise outgoing.OutboundBlocked("customer_contact_blocked")
    monkeypatch.setattr(api, "_eligible_pablo", forbidden)
    assert api._assess(db, user=user, lead_id=str(LEAD_ID)) == {
        "available": False, "method": "blocked", "reason": "not_authorized",
    }
    assert db[api._INCIDENTS].count_documents({}) == 0


def test_status_endpoint_handles_malformed_nonjson_and_disconnected(monkeypatch):
    from chatbot import crm_executive_outbound_api as api
    from types import SimpleNamespace

    session = {"session_id": "133286", "token": "secret-must-not-log"}
    response = lambda body, http=200: SimpleNamespace(
        status_code=http, json=lambda: body,
    )
    assert api._session_status(session, get=lambda *a, **kw: response(
        {"status": "connected"})) == "connected"
    assert api._session_status(session, get=lambda *a, **kw: response(
        {"status": "need_scan"})) == "need_scan"
    assert api._session_status(session, get=lambda *a, **kw: response(
        {"status": "connected"}, 503)) == "unavailable"
    assert api._session_status(session, get=lambda *a, **kw: response(
        {"status": "not_valid"})) == "unavailable"
    assert api._session_status(session, get=lambda *a, **kw:
                               (_ for _ in ()).throw(ValueError("bad JSON"))) == "unavailable"


def test_explicit_provider_rejection_unlocks_manual_fallback_only_after_attempt(
        pablo_fallback_context, monkeypatch):
    import asyncio
    from chatbot import storage
    api, db, user = pablo_fallback_context
    async def auth(_request):
        return user
    monkeypatch.setattr(api, "_user", auth)
    monkeypatch.setattr(api, "_session_status", lambda session, get=None: "connected")
    calls = []
    def send(_db, **kw):
        calls.append(kw)
        return {"status": "rejected", "reason_code": "session_disconnected"}
    monkeypatch.setattr(api, "send_manual", send)
    class Request:
        async def json(self):
            return {"lead_id": str(LEAD_ID), "text": "Hola",
                    "idempotency_key": "rejected-once-12345"}
    result = asyncio.run(api.crm_executive_whatsapp_send(Request()))
    assert result == {"status": "manual_fallback", "method": "wa_me"}
    assert len(calls) == 1
    assert db[api._INCIDENTS].count_documents({"status": "open"}) == 1


def test_unknown_provider_result_keeps_api(
        pablo_fallback_context, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    api, db, user = pablo_fallback_context
    async def auth(_request):
        return user
    monkeypatch.setattr(api, "_user", auth)
    monkeypatch.setattr(api, "_session_status", lambda session, get=None: "connected")
    monkeypatch.setattr(api, "send_manual",
                        lambda _db, **kw: {"status": "delivery_unknown"})
    class Request:
        async def json(self):
            return {"lead_id": str(LEAD_ID), "text": "Hola",
                    "idempotency_key": "uncertain-once-1234"}
    assert asyncio.run(api.crm_executive_whatsapp_send(Request())) == {
        "status": "delivery_unconfirmed", "method": "executive_wasender_api"}
    assert db[api._INCIDENTS].count_documents({"reason": "delivery_unconfirmed"}) == 1


def test_admin_incidents_read_only_admin_and_deduplicated(
        pablo_fallback_context, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    api, db, user = pablo_fallback_context
    api._incident(db, lead_id=str(LEAD_ID), reason="disconnected", phase="preflight")
    api._incident(db, lead_id=str(LEAD_ID), reason="disconnected", phase="preflight")
    assert db[api._INCIDENTS].count_documents({}) == 1
    async def auth_admin(_request):
        return {"_id": ObjectId(), "rol": "supervisor"}
    monkeypatch.setattr(api, "_user", auth_admin)
    result = asyncio.run(api.crm_executive_whatsapp_admin_incidents(object()))
    assert result["count"] == 1
    assert result["incidents"][0]["occurrences"] == 2
    assert result["incidents"][0]["lead_id"] == str(LEAD_ID)
    async def auth_exec(_request):
        return user
    monkeypatch.setattr(api, "_user", auth_exec)
    with pytest.raises(HTTPException) as err:
        asyncio.run(api.crm_executive_whatsapp_admin_incidents(object()))
    assert err.value.status_code == 403


def test_admin_banner_and_manual_fallback_are_scoped_in_templates():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    detail = (root / "templates/crm_lead_detail.html").read_text(encoding="utf-8")
    lead_list = (root / "templates/crm_leads_list.html").read_text(encoding="utf-8")
    assert "window.crmExecutiveWaMethod = 'wa_me'" in detail
    assert "result.status === 'manual_fallback'" in detail
    assert "Sesión WaSender desconectada" in detail
    assert "window.open(url, '_blank')" in detail
    assert "crmWhatsAppIncidents" in lead_list
    assert "can_administer_leads" in lead_list
    assert "/api/crm/executive-whatsapp/incidents" in lead_list


def test_crm_lead_detail_uses_api_only_for_pablo_link_for_everyone_else():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "templates" /
            "crm_lead_detail.html").read_text(encoding="utf-8")
    assert "https://wa.me/${cleanPhone}?text=${encodeURIComponent(finalMsg)}" in text
    assert "window.open(url, '_blank')" in text
    assert "Abrir WhatsApp" in text
    assert "window.crmPabloApiPilot" in text
    assert "fetch('/api/crm/executive-whatsapp/send'" in text
    assert "fetch('/api/crm/executive-whatsapp/available/" in text
    assert "['executive_wasender_api','retry'].includes(window.crmExecutiveWaMethod)" in text
    assert "window.crmExecutiveOutboundAttemptKey" in text


# Mariela is a second independently enabled pilot, not a global CRM rollout.
@pytest.fixture
def mariela_crm_api_rig(monkeypatch):
    import mongomock
    from chatbot import crm_executive_outbound_api as api
    from chatbot import storage
    db = mongomock.MongoClient().test
    owner = api._MARIELA_USER_ID
    session_id = api._MARIELA_SESSION
    lead_id = ObjectId("6b046d3e1318876d74b06aee")
    cycle_id = "mariela-cycle-20261123"
    phone = "+56996762064"
    db["leads"].insert_one({
        "_id": lead_id, "phone": phone, "messages": [], "ejecutivo_asignado": api._MARIELA_NAME,
        "lifecycle": {"assigned_to_user_id": owner,
                      "current_assignment_cycle_id": cycle_id},
    })
    db["usuarios"].insert_one({
        "_id": ObjectId(owner), "nombre": api._MARIELA_NAME,
        "rol": "agente", "is_active": True,
        "telefono": "+56938725886", "wasender": {"session_id": 133526},
    })
    cycle = {
        "lead_id": lead_id, "assignment_cycle_id": cycle_id,
        "assigned_to_user_id": owner, "cycle_status": "active",
        "unassigned_at": None,
    }
    db["crm_assignment_cycles"].insert_one(cycle)
    monkeypatch.setenv("PROCASA_PABLO_CRM_WASENDER_API_ENABLED", "1")
    monkeypatch.setenv("PROCASA_MARIELA_CRM_WASENDER_API_ENABLED", "1")
    monkeypatch.setenv("PROCASA_EXEC_CRM_DIRECT_SEND_ENABLED", "1")
    monkeypatch.setenv("PROCASA_EXEC_CRM_SEND_SESSION_IDS", "133286")
    monkeypatch.setattr(storage, "get_db", lambda: db)
    monkeypatch.setattr(outgoing, "active_assignment_cycle",
                        lambda _db, lid: cycle if lid == lead_id else None)
    monkeypatch.setattr(outgoing, "_authorized_lead",
                        lambda _db, phone, user_id:
                        (db["leads"].find_one({"_id": lead_id}), cycle)
                        if user_id == owner else None)
    monkeypatch.setattr(outgoing, "_session",
                        lambda _db, sid: {
                            "session_id": sid, "user_id": owner,
                            "capture_enabled": True, "token": "mariela-test-token",
                        } if sid == session_id else None)
    attested = []
    def verify(session, *, expected_phone, force=False, get=None):
        attested.append((session["session_id"], expected_phone, force))
    monkeypatch.setattr(outgoing, "verify_provider_sender_identity", verify)
    monkeypatch.setattr(api, "verify_provider_sender_identity", verify)
    monkeypatch.setattr(api, "_session_status", lambda s, get=None: "connected")
    monkeypatch.setattr(outgoing, "credit_verified_outbound", lambda *_a, **_kw: "credited")
    user = db["usuarios"].find_one({"_id": ObjectId(owner)})
    return api, db, user, lead_id, cycle_id, attested


def test_mariela_flag_preserves_pablo_and_does_not_enable_other_agents(monkeypatch):
    from chatbot import crm_executive_outbound_api as api
    monkeypatch.setenv("PROCASA_EXEC_CRM_SEND_SESSION_IDS", "133286")
    monkeypatch.delenv("PROCASA_MARIELA_CRM_WASENDER_API_ENABLED", raising=False)
    monkeypatch.setenv("PROCASA_PABLO_CRM_WASENDER_API_ENABLED", "1")
    mariela = {"_id": ObjectId(api._MARIELA_USER_ID), "nombre": api._MARIELA_NAME,
               "rol": "agente"}
    pablo = {"_id": ObjectId(api._PABLO_USER_ID), "nombre": api._PABLO_NAME,
             "rol": "agente"}
    assert api._profile(mariela) is None
    assert api._profile(pablo)["session"] == "133286"
    assert outgoing._enabled_session_ids() == {"133286"}
    monkeypatch.setenv("PROCASA_MARIELA_CRM_WASENDER_API_ENABLED", "1")
    assert outgoing._enabled_session_ids() == {"133286", "133526"}
    assert api._profile(mariela) == {
        "id": api._MARIELA_USER_ID, "name": api._MARIELA_NAME,
        "session": "133526",
    }
    assert api._profile({**mariela, "_id": ObjectId()}) is None
    assert api._profile({**mariela, "nombre": "Erika Garrido"}) is None
    assert api._profile({**mariela, "rol": "admin"}) is None
    assert api._profile({"_id": ObjectId(), "nombre": "Susana Ensignia",
                         "rol": "agente"}) is None


def test_mariela_pilot_api_send_persists_only_her_sender_and_is_idempotent(
        mariela_crm_api_rig, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    api, db, mariela, lead_id, cycle_id, attested = mariela_crm_api_rig
    async def auth(_request):
        return mariela
    monkeypatch.setattr(api, "_user", auth)
    seen = []
    def safe_send(url, **kwargs):
        seen.append((url, kwargs))
        return SimpleNamespace(status_code=200, content=b"ok",
            json=lambda: {"success": True, "data": {"msgId": "mariela-provider-001"}})
    monkeypatch.setattr(outgoing.requests, "post", safe_send)
    assert asyncio.run(api.crm_executive_whatsapp_available(object(), str(lead_id))) == {
        "method": "executive_wasender_api", "available": True,
    }
    class Request:
        async def json(self):
            return {"lead_id": str(lead_id), "text": "Hola, prueba simulada",
                    "idempotency_key": "mariela-message-idempotent-01"}
    first = asyncio.run(api.crm_executive_whatsapp_send(Request()))
    assert first["status"] == "accepted" and first["recorded"] is True
    second = asyncio.run(api.crm_executive_whatsapp_send(Request()))
    assert second["status"] == "duplicate"
    assert len(seen) == 1
    assert seen[0][0].endswith("/send-message")
    assert seen[0][1]["headers"]["Authorization"] == "Bearer mariela-test-token"
    assert seen[0][1]["json"]["to"] == "+56996762064"
    msg = db["leads"].find_one({"_id": lead_id})["messages"]
    assert len(msg) == 1
    assert msg[0]["source"] == "crm_manual_api"
    assert msg[0]["actor_id"] == api._MARIELA_USER_ID
    assert msg[0]["assigned_executive_id"] == api._MARIELA_USER_ID
    assert msg[0]["wasender_session_id"] == "133526"
    assert msg[0]["provider_message_id"] == "mariela-provider-001"
    assert db[outgoing.OUTBOX].count_documents({"state": "accepted", "canonical_recorded": True}) == 1
    assert attested and all(x[0] == "133526" and x[1] == "+56938725886" for x in attested)


def test_mariela_cannot_send_to_unassigned_lead_or_wrong_session(
        mariela_crm_api_rig, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    api, db, mariela, lead_id, cycle_id, _ = mariela_crm_api_rig
    async def auth(_request):
        return mariela
    monkeypatch.setattr(api, "_user", auth)
    fake_lead = db["leads"].find_one({"_id": lead_id})
    fake_lead["lifecycle"]["assigned_to_user_id"] = api._PABLO_USER_ID
    db["leads"].replace_one({"_id": lead_id}, fake_lead)
    state = asyncio.run(api.crm_executive_whatsapp_available(object(), str(lead_id)))
    assert state["method"] == "blocked"
    assert db[outgoing.OUTBOX].count_documents({}) == 0
    # Also refuse a token linked to the wrong session, without posting.
    fake_lead["lifecycle"]["assigned_to_user_id"] = api._MARIELA_USER_ID
    db["leads"].replace_one({"_id": lead_id}, fake_lead)
    monkeypatch.setattr(outgoing, "_session",
                        lambda db, sid: {"session_id": "133286",
                                         "user_id": api._MARIELA_USER_ID,
                                         "capture_enabled": True, "token": "wrong"}
                        if sid == "133526" else None)
    state = asyncio.run(api.crm_executive_whatsapp_available(object(), str(lead_id)))
    assert state["method"] == "wa_me" and state["fallback"] is True
    assert db[outgoing.OUTBOX].count_documents({}) == 0


def test_mariela_off_preserves_manual_web_and_blocks_post(
        mariela_crm_api_rig, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    api, db, mariela, lead_id, _, _ = mariela_crm_api_rig
    monkeypatch.setenv("PROCASA_MARIELA_CRM_WASENDER_API_ENABLED", "0")
    async def auth(_request):
        return mariela
    monkeypatch.setattr(api, "_user", auth)
    assert asyncio.run(api.crm_executive_whatsapp_available(object(), str(lead_id))) == {
        "available": False, "method": "wa_me", "fallback": True,
    }
    class Request:
        async def json(self):
            return {"lead_id": str(lead_id), "text": "no send",
                    "idempotency_key": "mariela-disabled-key"}
    with pytest.raises(HTTPException) as err:
        asyncio.run(api.crm_executive_whatsapp_send(Request()))
    assert err.value.status_code == 403
    assert db[outgoing.OUTBOX].count_documents({}) == 0


def test_mariela_uncertain_history_keeps_api(
        mariela_crm_api_rig):
    api, db, mariela, lead_id, cycle_id, _ = mariela_crm_api_rig
    db[outgoing.OUTBOX].insert_one({
        "_id": "uncertain", "lead_id": lead_id,
        "assignment_cycle_id": cycle_id,
        "executive_id": api._MARIELA_USER_ID,
        "state": "delivery_unknown", "session_id": "133526",
    })
    assert api._assess(db, user=mariela, lead_id=str(lead_id)) == {"method": "executive_wasender_api", "available": True}
    assert db[api._INCIDENTS].count_documents({"reason": "delivery_unconfirmed"}) == 0
    assert db[outgoing.OUTBOX].count_documents({}) == 1


def test_mariela_disconnect_triggers_her_own_alert_and_safe_manual_fallback(
        mariela_crm_api_rig, monkeypatch):
    api, db, mariela, lead_id, _, _ = mariela_crm_api_rig
    monkeypatch.setattr(api, "_session_status", lambda *_a, **_kw: "disconnected")
    assert api._assess(db, user=mariela, lead_id=str(lead_id)) == {
        "method": "wa_me", "available": False, "fallback": True,
        "reason": "session_unavailable",
    }
    incident = db[api._INCIDENTS].find_one({"reason": "disconnected"})
    assert incident["executive_user_id"] == api._MARIELA_USER_ID
    assert incident["session_id"] == "133526"
    assert db[api._INCIDENTS].count_documents({"executive_user_id": api._PABLO_USER_ID}) == 0


def test_crm_mariela_button_is_scoped_to_exact_two_pilot_user_names():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent /
            "templates/crm_lead_detail.html").read_text(encoding="utf-8")
    for name in ("Pablo Galleguillos", "Mariela Arriagada",
                 "Hernán Castro", "Susana Ensignia", "Paula Morales"):
        assert name in text
    assert "fetch('/api/crm/executive-whatsapp/available/" in text
    assert "fetch('/api/crm/executive-whatsapp/send'" in text


# Each executive must be independently enabled and must use their own sender.
_NEW_EXECUTIVES = (
    ("6a681413140190dde11f26d1", "Hernán Castro", "133517",
     "PROCASA_HERNAN_CRM_WASENDER_API_ENABLED", "+56984037477"),
    ("6989c6309dd2ba54e478196b", "Susana Ensignia", "133523",
     "PROCASA_SUSANA_CRM_WASENDER_API_ENABLED", "+56939125978"),
    ("699653a96a099d6e9612d519", "Paula Morales", "133527",
     "PROCASA_PAULA_CRM_WASENDER_API_ENABLED", "+56928065512"),
)


@pytest.mark.parametrize("uid,name,sid,flag,phone", _NEW_EXECUTIVES)
def test_team_api_optin_independent_of_other_executives(
        monkeypatch, uid, name, sid, flag, phone):
    from chatbot import crm_executive_outbound_api as api
    user = {"_id": ObjectId(uid), "nombre": name, "rol": "agente"}
    monkeypatch.setenv("PROCASA_EXEC_CRM_SEND_SESSION_IDS", "133286")
    monkeypatch.setenv("PROCASA_PABLO_CRM_WASENDER_API_ENABLED", "1")
    monkeypatch.setenv("PROCASA_MARIELA_CRM_WASENDER_API_ENABLED", "1")
    for row in _NEW_EXECUTIVES:
        monkeypatch.delenv(row[3], raising=False)
    assert api._profile(user) is None
    assert outgoing._enabled_session_ids() == {"133286", "133526"}
    monkeypatch.setenv(flag, "1")
    assert api._profile(user) == {"id": uid, "name": name, "session": sid}
    assert outgoing._enabled_session_ids() == {"133286", "133526", sid}
    assert api._profile({**user, "_id": ObjectId()}) is None
    assert api._profile({**user, "nombre": "Incorrecta"}) is None
    assert api._profile({**user, "rol": "supervisor"}) is None
    monkeypatch.setenv(flag, "true")
    assert api._profile(user) is None


@pytest.fixture
def team_api_rig(monkeypatch, request):
    import mongomock
    from chatbot import crm_executive_outbound_api as api, storage
    owner, name, sid, flag, phone = request.param
    db = mongomock.MongoClient().test
    lead_id = ObjectId("6b046d3e1318876d74b06ae0")
    cycle_id = "team-cycle-20261123"
    db["leads"].insert_one({"_id": lead_id, "phone": "+56996762064", "messages": [], "ejecutivo_asignado": name,
        "lifecycle": {"assigned_to_user_id": owner,
                      "current_assignment_cycle_id": cycle_id}})
    db["usuarios"].insert_one({"_id": ObjectId(owner), "nombre": name,
        "is_active": True, "rol": "agente", "telefono": phone,
        "wasender": {"session_id": int(sid)}})
    cycle = {"lead_id": lead_id, "assignment_cycle_id": cycle_id,
             "assigned_to_user_id": owner, "unassigned_at": None,
             "cycle_status": "active"}
    db["crm_assignment_cycles"].insert_one(cycle)
    monkeypatch.setenv("PROCASA_EXEC_CRM_DIRECT_SEND_ENABLED", "1")
    monkeypatch.setenv("PROCASA_EXEC_CRM_SEND_SESSION_IDS", "133286")
    for row in _NEW_EXECUTIVES:
        monkeypatch.delenv(row[3], raising=False)
    monkeypatch.setenv(flag, "1")
    monkeypatch.setattr(storage, "get_db", lambda: db)
    monkeypatch.setattr(outgoing, "active_assignment_cycle",
                        lambda _db, lid: cycle if lid == lead_id else None)
    monkeypatch.setattr(outgoing, "_authorized_lead",
                        lambda _db, phone, user_id:
                        (db["leads"].find_one({"_id": lead_id}), cycle)
                        if user_id == owner else None)
    monkeypatch.setattr(outgoing, "_session",
                        lambda _db, requested: {
                            "session_id": sid, "user_id": owner,
                            "capture_enabled": True, "token": "fake-team-token",
                        } if requested == sid else None)
    sender_checks = []
    def verify(session, *, expected_phone, force=False, get=None):
        sender_checks.append((session["session_id"], expected_phone, force))
    monkeypatch.setattr(outgoing, "verify_provider_sender_identity", verify)
    monkeypatch.setattr(api, "verify_provider_sender_identity", verify)
    monkeypatch.setattr(api, "_session_status", lambda _session, get=None: "connected")
    monkeypatch.setattr(outgoing, "credit_verified_outbound", lambda *_a, **_kw: "credited")
    return api, db, db["usuarios"].find_one({"_id": ObjectId(owner)}), lead_id, cycle_id, sender_checks


@pytest.mark.parametrize("team_api_rig", _NEW_EXECUTIVES, indirect=True)
def test_team_crm_api_sends_once_from_own_number_and_records_message(
        team_api_rig, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    api, db, user, lead_id, cycle_id, verified = team_api_rig
    async def auth(_req):
        return user
    monkeypatch.setattr(api, "_user", auth)
    post_calls = []
    def accepted(url, **kw):
        post_calls.append((url, kw))
        return SimpleNamespace(status_code=200, content=b"ok", json=lambda: {
            "success": True, "data": {"msgId": "test-msg-" + user["nombre"]}})
    monkeypatch.setattr(outgoing.requests, "post", accepted)
    available = asyncio.run(api.crm_executive_whatsapp_available(object(), str(lead_id)))
    assert available == {"available": True, "method": "executive_wasender_api"}
    class Request:
        async def json(self):
            return {"lead_id": str(lead_id), "text": "Simulado, no real",
                    "idempotency_key": "team-provider-receipt-20261123"}
    first = asyncio.run(api.crm_executive_whatsapp_send(Request()))
    second = asyncio.run(api.crm_executive_whatsapp_send(Request()))
    assert first["status"] == "accepted" and first["recorded"] is True
    assert second["status"] == "duplicate"
    assert len(post_calls) == 1
    assert post_calls[0][0].endswith("/send-message")
    assert post_calls[0][1]["headers"]["Authorization"] == "Bearer fake-team-token"
    assert post_calls[0][1]["json"]["to"] == "+56996762064"
    lead = db["leads"].find_one({"_id": lead_id})
    assert len(lead["messages"]) == 1
    msg = lead["messages"][0]
    assert msg["source"] == "crm_manual_api"
    assert msg["actor_id"] == str(user["_id"])
    assert msg["assigned_executive_id"] == str(user["_id"])
    assert msg["wasender_session_id"] == str(user["wasender"]["session_id"])
    # Internal eligibility may check identity twice (default and forced).
    # All checks must use the correct sender; at least one MUST be forced.
    assert verified and all(sid == msg["wasender_session_id"]
                            and number == user["telefono"]
                            for sid, number, force in verified)
    assert any(force for sid, number, force in verified)
    assert db[outgoing.OUTBOX].count_documents({"canonical_recorded": True}) == 1


@pytest.mark.parametrize("team_api_rig", _NEW_EXECUTIVES, indirect=True)
def test_team_connection_down_or_uncertain_never_sends_twice(
        team_api_rig, monkeypatch):
    api, db, user, lead_id, cycle_id, verified = team_api_rig
    monkeypatch.setattr(api, "_session_status",
                        lambda *_a, **_kw: "disconnected")
    outcome = api._assess(db, user=user, lead_id=str(lead_id))
    assert outcome == {"method": "wa_me", "available": False,
                       "fallback": True, "reason": "session_unavailable"}
    assert db[api._INCIDENTS].find_one({"reason": "disconnected"})["executive_user_id"] == str(user["_id"])
    db[outgoing.OUTBOX].insert_one({"_id": "uncertain", "lead_id": lead_id,
        "assignment_cycle_id": cycle_id, "executive_id": str(user["_id"]),
        "session_id": str(user["wasender"]["session_id"]),
        "state": "delivery_unknown"})
    assert api._assess(db, user=user, lead_id=str(lead_id)) == {"method": "wa_me", "available": False, "fallback": True, "reason": "session_unavailable"}
    monkeypatch.setattr(api, "_session_status", lambda *_a, **_kw: "connected")
    assert api._assess(db, user=user, lead_id=str(lead_id)) == {"method": "executive_wasender_api", "available": True}


def test_team_send_does_not_enable_erika_or_rocio_without_link(monkeypatch):
    from chatbot import crm_executive_outbound_api as api
    for row in _NEW_EXECUTIVES:
        monkeypatch.setenv(row[3], "1")
    assert api._profile({"_id": ObjectId("6989c6309dd2ba54e478196d"),
                         "nombre": "Erika Garrido", "rol": "agente"}) is None
    assert api._profile({"_id": ObjectId("6995e9be6a099d6e9612d120"),
                         "nombre": "Rocío Aliaga", "rol": "agente"}) is None


@pytest.mark.parametrize("reason", ["invalid_request", "invalid_recipient", "provider_rejected",
                                    "subscription", "rate_limit", "delivery_unconfirmed"])
def test_crm_post_send_failure_calls_operator_alert(pablo_fallback_context, monkeypatch, reason):
    import asyncio
    from fastapi import HTTPException
    api, db, user = pablo_fallback_context
    async def auth(_request):
        return user
    monkeypatch.setattr(api, "_user", auth)
    monkeypatch.setattr(api, "_session_status", lambda session, get=None: "connected")
    monkeypatch.setattr(api, "send_manual", lambda *_a, **_k:
        {"status": "delivery_unknown"} if reason == "delivery_unconfirmed"
        else {"status": "rejected", "reason_code": reason})
    alerts = []
    monkeypatch.setattr(api, "_queue_operator_alert", lambda **kw: alerts.append(kw))
    class Request:
        async def json(self):
            return {"lead_id": str(LEAD_ID), "text": "Hola",
                    "idempotency_key": "alert-rejection-12345"}
    if reason == "delivery_unconfirmed":
        assert asyncio.run(api.crm_executive_whatsapp_send(Request())) == {"status": "delivery_unconfirmed", "method": "executive_wasender_api"}
        assert alerts == []
    else:
        assert asyncio.run(api.crm_executive_whatsapp_send(Request())) == {"status": "rejected", "reason": reason}
        assert alerts == [{"user": user, "reason": reason, "lead_id": str(LEAD_ID)}]


def test_provider_validation_diagnostics_preserve_fields_without_private_data(context):
    db, _ = context
    body = {"success": False, "message": "Validation failed", "errors": {
        "to": ["The +56912345678 number is invalid"],
        "text": ["The text must not exceed 100 characters: PRIVATE CONTENT"],
        "private-field": ["private-session-token"],
    }}
    result = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER, message="Hola",
        idempotency_key="request-validation-details", post=lambda *_a, **_kw:
        SimpleNamespace(status_code=422, content=b"error", json=lambda: body),
    )
    assert result == {"status": "rejected", "reason_code": "invalid_request"}
    saved = next(iter(db[outgoing.OUTBOX].docs.values()))
    assert saved["provider_validation_fields"] == {
        "to": ["invalid"], "text": ["length"], "other": ["unspecified"]}
    assert saved["provider_request_text_chars"] == 4
    assert "+56912345678" not in str(saved)
    assert "PRIVATE CONTENT" not in str(saved)
    assert "private-session-token" not in str(saved)


@pytest.mark.parametrize("exists", [True, False])
def test_recipient_lookup_caches_only_explicit_boolean(monkeypatch, exists):
    monkeypatch.setattr(outgoing, "_RECIPIENT_CACHE", outgoing.OrderedDict())
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {
            "success": True, "data": {"exists": exists}})
    session = {"session_id": "133527", "token": "test-token"}
    for _ in range(2):
        assert outgoing.check_recipient_registration(session, "+56911112222", get=get) is exists
    assert len(calls) == 1
    assert calls[0][0].endswith("/on-whatsapp/%2B56911112222")
    assert calls[0][1]["headers"]["Authorization"] == "Bearer test-token"
    assert outgoing.check_recipient_registration(
        {"session_id": "133526", "token": "other"}, "+56911112222", get=get) is exists
    assert len(calls) == 2


@pytest.mark.parametrize("status,body", [
    (429, {"success": False}),
    (503, {"success": False}),
    (200, {"success": False, "data": {"exists": False}}),
    (200, {"success": True, "data": {"exists": "false"}}),
    (200, {"success": True, "data": {}}),
])
def test_recipient_lookup_unknown_is_not_cached_or_negative(monkeypatch, status, body):
    monkeypatch.setattr(outgoing, "_RECIPIENT_CACHE", outgoing.OrderedDict())
    calls = []
    def get(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(status_code=status, json=lambda: body)
    for _ in range(2):
        assert outgoing.check_recipient_registration(
            {"session_id": "133527", "token": "test"}, "+56911112222", get=get) is None
    assert len(calls) == 2


def test_confirmed_missing_recipient_never_posts_or_credits_sla(context, monkeypatch):
    db, _ = context
    monkeypatch.setenv("PROCASA_EXEC_WA_RECIPIENT_CHECK_ENABLED", "1")
    checks, posts, credits = [], [], []
    monkeypatch.setattr(outgoing, "check_recipient_registration",
                        lambda *_a: checks.append(1) or False)
    monkeypatch.setattr(outgoing, "credit_verified_outbound", lambda **_kw: credits.append(1))
    kwargs = dict(lead_id=LEAD_ID, actor_user_id=OWNER, message="Hola",
                  idempotency_key="recipient-check-1234", post=lambda *_a, **_kw: posts.append(1))
    assert outgoing.send_manual(db, **kwargs) == {
        "status": "rejected", "reason_code": "recipient_not_registered"}
    assert outgoing.send_manual(db, **kwargs)["status"] == "duplicate"
    assert checks == [1] and not posts and not credits
    saved = next(iter(db[outgoing.OUTBOX].docs.values()))
    assert saved["state"] == "blocked"
    assert saved["provider_send_attempted"] is False
    assert saved["recipient_registration"] == "not_registered"
    assert not db["leads"].document["messages"]


@pytest.mark.parametrize("lookup", [True, None])
def test_registered_or_unknown_recipient_preserves_normal_send(context, monkeypatch, lookup):
    db, _ = context
    monkeypatch.setenv("PROCASA_EXEC_WA_RECIPIENT_CHECK_ENABLED", "1")
    monkeypatch.setattr(outgoing, "check_recipient_registration", lambda *_a: lookup)
    calls = []
    result = outgoing.send_manual(
        db, lead_id=LEAD_ID, actor_user_id=OWNER, message="Hola",
        idempotency_key="recipient-positive-123", post=lambda *_a, **_kw: calls.append(1) or _response())
    assert result["status"] == "accepted" and calls == [1]


def test_structured_provider_error_identifies_unregistered_recipient():
    assert outgoing._provider_rejection_reason(422, {
        "message": "Validation failed", "errors": {"to": ["Number is not registered on WhatsApp"]}
    }) == "recipient_not_registered"
    assert outgoing._provider_rejection_reason(422, {
        "message": "Validation failed", "errors": {"text": ["Invalid format"]}
    }) == "invalid_request"


def test_unregistered_recipient_no_web_fallback_and_notifies_admin(pablo_fallback_context, monkeypatch):
    import asyncio
    from chatbot import storage
    api, db, user = pablo_fallback_context
    async def auth(_request):
        return user
    monkeypatch.setattr(api, "_user", auth)
    monkeypatch.setattr(api, "_session_status", lambda *_a, **_kw: "connected")
    monkeypatch.setattr(storage, "get_db", lambda: db)
    monkeypatch.setattr(api, "send_manual", lambda *_a, **_kw: {
        "status": "rejected", "reason_code": "recipient_not_registered"})
    alerts = []
    monkeypatch.setattr(api, "_queue_operator_alert", lambda **kw: alerts.append(kw))
    class Request:
        async def json(self):
            return {"lead_id": str(LEAD_ID), "text": "Hola", "idempotency_key": "recipient-block-123"}
    assert asyncio.run(api.crm_executive_whatsapp_send(Request())) == {
        "status": "recipient_not_registered", "method": "blocked"}
    assert alerts == [{"user": user, "reason": "recipient_not_registered", "lead_id": str(LEAD_ID)}]


def test_legacy_cycle_without_pointer_is_not_disconnection(pablo_fallback_context, monkeypatch):
    api, db, user = pablo_fallback_context
    db["leads"].update_one({"_id": LEAD_ID}, {"$unset": {"lifecycle.current_assignment_cycle_id": 1, "lifecycle.assigned_to_user_id": 1}})
    db["crm_assignment_cycles"].insert_one({"lead_id": LEAD_ID, "assignment_cycle_id": CYCLE,
        "assigned_to_user_id": api._PABLO_USER_ID, "cycle_status": "active", "unassigned_at": None})
    def fail(*args, **kwargs):
        raise outgoing.OutboundBlocked("current_assignment_required")
    monkeypatch.setattr(api, "_eligible_pablo", fail)
    assert not api._outstanding_uncertain(db, str(LEAD_ID))
    assert api._assess(db, user=user, lead_id=str(LEAD_ID)) == {
        "method": "retry", "available": False, "reason": "current_assignment_required"}
    db["crm_assignment_cycles"].update_one({"lead_id": LEAD_ID}, {"$set": {"assigned_to_user_id": str(ObjectId())}})
    assert api._assess(db, user=user, lead_id=str(LEAD_ID))["method"] == "blocked"
