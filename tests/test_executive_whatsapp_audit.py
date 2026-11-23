"""Passive executive WhatsApp audit regression tests (no real WaSender/Mongo calls)."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatbot import executive_whatsapp_audit as audit

USER = ObjectId("69796bc4bbebf240378eb739")
LEAD = ObjectId("690e13577fb5678251136b01")
PHONE = "+56912345678"
PRIVATE = "+56955554444"
SESSION = "133286"
EXEC_ENV = "PROCASA_WASENDER_PABLO_GALLEGUILLOS"
LEGACY_ENV = "PROCASA_EXEC_WASENDER_SESSIONS_JSON"
SECRET = "audit-test-secret-value-2026"
TOKEN = "session-api-test-only-token"


def session_config(*, capture_enabled=True, secret=SECRET, token=TOKEN):
    return json.dumps({
        "session_id": int(SESSION),
        "webhook_secret": secret, "api_token": token,
        "capture_enabled": capture_enabled,
    })


class Cursor(list):
    def limit(self, n):
        return Cursor(self[:n])


class Users:
    def __init__(self):
        self.user = {
            "_id": USER, "nombre": "Pablo Galleguillos", "rol": "agente",
            "is_active": True, "telefono": "+56983219804",
            "wasender": {"session_id": 133286},
        }
    def find(self, query, projection):
        if self.user["is_active"] is False:
            return Cursor([])
        if "wasender.session_id" in query:
            allowed = self.user["wasender"]["session_id"] in query["wasender.session_id"]["$in"]
        else:
            allowed = query.get("nombre") == self.user["nombre"]
        return Cursor([self.user] if allowed else [])
    def find_one(self, query, projection):
        if self.user["is_active"] is not True or query.get("_id") != USER:
            return None
        return {"_id": USER}


class Leads:
    def __init__(self):
        self.lead = {
            "_id": LEAD, "phone": PHONE, "conversation_id": "test-conv",
            "lifecycle": {
                "assigned_to_user_id": str(USER),
                "current_assignment_cycle_id": "cycle-A",
            },
            "messages": [{"role": "assistant", "content": "Bot history"}],
        }
        self.update_calls = 0
        self.raise_on_update = False
        self.last_mutation = None

    def find(self, query, projection):
        return Cursor([self.lead] if self.lead["phone"] in query["phone"]["$in"] else [])

    def find_one(self, query, projection=None):
        return self.lead if query.get("_id") == LEAD else None

    def update_one(self, selector, mutation):
        self.update_calls += 1
        if self.raise_on_update:
            raise RuntimeError("sensitive-message-error")
        if (selector["lifecycle.current_assignment_cycle_id"]
                != self.lead["lifecycle"]["current_assignment_cycle_id"]
                or selector["lifecycle.assigned_to_user_id"]
                != self.lead["lifecycle"]["assigned_to_user_id"]):
            return SimpleNamespace(modified_count=0)
        entry = mutation["$push"]["messages"]
        if any(m.get("provider_message_id") == entry["provider_message_id"]
               and m.get("wasender_session_id") == entry["wasender_session_id"]
               for m in self.lead["messages"]):
            return SimpleNamespace(modified_count=0)
        self.lead["messages"].append(entry)
        self.last_mutation = mutation
        return SimpleNamespace(modified_count=1)


def event(*, from_me, msg_id="mid-1", event_name="messages.upsert",
          phone=PHONE, jid=None, text="Mensaje de prueba", timestamp=None):
    jid = jid if jid is not None else phone.lstrip("+") + "@s.whatsapp.net"
    return {
        "event": event_name,
        "timestamp": timestamp or int(datetime.now(timezone.utc).timestamp()),
        "data": {"messages": {
            "key": {"id": msg_id, "fromMe": from_me, "remoteJid": jid},
            "messageBody": text,
        }},
    }


@pytest.fixture
def rig(monkeypatch):
    db = {"usuarios": Users(), "leads": Leads()}
    cycle = {"assignment_cycle_id": "cycle-A", "assigned_to_user_id": str(USER),
             "assigned_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
             "cycle_status": "active", "unassigned_at": None}
    monkeypatch.setattr(audit, "active_assignment_cycle", lambda _db, _id: cycle)
    from chatbot import storage
    monkeypatch.setattr(storage, "get_db", lambda: db)
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_ENABLED", "1")
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_WRITE_ENABLED", "1")
    monkeypatch.setenv(EXEC_ENV, session_config())
    app = FastAPI()
    app.include_router(audit.router)
    client = TestClient(app)
    return db, cycle, client


def post(client, payload, *, signature=SECRET, session_id=SESSION):
    return client.post(f"/webhook/ejecutivos/{session_id}", json=payload,
                       headers={"X-Webhook-Signature": signature})


def test_only_session_id_is_required_in_mongodb(rig, monkeypatch):
    db, cycle, client = rig
    cfg = audit._session(db, SESSION)
    assert db["usuarios"].user["wasender"] == {"session_id": 133286}
    assert cfg["secret"] == SECRET
    assert cfg["token"] == TOKEN
    assert cfg["capture_enabled"] is True
    assert audit._variable_name_for_executive("Pablo Galleguillos") == EXEC_ENV
    # Legacy secret fields left in MongoDB are ignored (but should be cleaned).
    db["usuarios"].user["wasender"]["webhook_secret_env"] = "plaintext-wrong"
    db["usuarios"].user["wasender"]["api_token_env"] = "plaintext-wrong"
    assert audit._session(db, SESSION)["secret"] == SECRET
    # Unknown sessions are not silently mapped to another user's token.
    assert audit._session(db, "133287") is None
    # A duplicated session cannot be attributed ambiguously.
    original_find = db["usuarios"].find
    def duplicates(query, projection):
        return Cursor([db["usuarios"].user, db["usuarios"].user])
    db["usuarios"].find = duplicates
    assert audit._session(db, SESSION) is None
    db["usuarios"].find = original_find


def test_executive_credentials_must_be_valid_and_never_fallback_to_mongo(rig, monkeypatch):
    db, cycle, client = rig
    for value in (
        "", "not json", "[]", "{}", "{bad",
        json.dumps({"session_id": int(SESSION), "webhook_secret": SECRET}),
        json.dumps({"session_id": int(SESSION) + 1,
                    "webhook_secret": SECRET, "api_token": TOKEN}),
        json.dumps({"session_id": str(SESSION),
                    "webhook_secret": SECRET, "api_token": TOKEN}),
        session_config(secret="short"),
        session_config(token="short"),
        json.dumps({"session_id": int(SESSION), "webhook_secret": SECRET,
                    "api_token": TOKEN, "capture_enabled": "true"}),
    ):
        monkeypatch.setenv(EXEC_ENV, value)
        cfg = audit._session(db, SESSION)
        if '"capture_enabled": "true"' in value:
            assert cfg is not None and cfg["capture_enabled"] is False
        else:
            assert cfg is None
    monkeypatch.setenv(EXEC_ENV, "a" * 5000)
    assert audit._session(db, SESSION) is None


def test_per_session_capture_guard_blocks_global_write(rig, monkeypatch):
    db, cycle, client = rig
    monkeypatch.setenv(EXEC_ENV, session_config(capture_enabled=False))
    assert post(client, event(from_me=True)).json()["status"] == "dry_run"
    assert db["leads"].update_calls == 0
    # A credential accidentally copied from another session must fail closed.
    config = json.dumps({"session_id": 133287,
                         "webhook_secret": SECRET, "api_token": TOKEN,
                         "capture_enabled": True})
    monkeypatch.setenv(EXEC_ENV, config)
    assert post(client, event(from_me=True)).status_code == 401
    assert db["leads"].update_calls == 0


def test_manual_outgoing_and_incoming_are_separately_attributed_and_deduped(rig):
    db, cycle, client = rig
    outbound = event(from_me=True, msg_id="manual-001")
    incoming = event(from_me=False, event_name="messages.received",
                     msg_id="reply-002")
    assert post(client, outbound).json()["status"] == "stored"
    assert post(client, incoming).json()["status"] == "stored"
    assert post(client, incoming).json()["status"] == "duplicate_or_reassigned"
    # The same reply duplicated between both webhook subscriptions is one message.
    dup_upsert = event(from_me=False, event_name="messages.upsert",
                       msg_id="reply-002")
    assert post(client, dup_upsert).json()["status"] == "duplicate_or_reassigned"
    messages = db["leads"].lead["messages"]
    assert len(messages) == 3
    assert messages[0]["content"] == "Bot history"
    assert messages[1]["actor_type"] == "human_agent"
    assert messages[1]["actor_id"] == str(USER)
    assert messages[1]["role"] == "agent"
    assert messages[1]["assignment_cycle_id"] == "cycle-A"
    assert messages[1]["wasender_session_id"] == SESSION
    assert messages[2]["actor_type"] == "customer"
    assert messages[2]["actor_id"] is None
    assert messages[2]["role"] == "user"


def test_message_sent_direct_envelope_is_saved_and_deduped_against_upsert(rig):
    db, cycle, client = rig
    sent = event(from_me=True, event_name="message.sent",
                 msg_id="manual-sent-001", text="Respuesta comercial")
    direct = sent["data"].pop("messages")
    sent["data"].update({"key": direct["key"],
                         "message": {"conversation": "Respuesta comercial"},
                         "success": True})
    assert post(client, sent).json()["status"] == "stored"
    # The same provider ID can also arrive through messages.upsert.
    duplicate = event(from_me=True, msg_id="manual-sent-001",
                      text="Respuesta comercial")
    assert post(client, duplicate).json()["status"] == "duplicate_or_reassigned"
    entries = db["leads"].lead["messages"]
    assert len(entries) == 2
    assert entries[-1]["role"] == "agent"
    assert entries[-1]["source"] == "whatsapp_direct"
    assert entries[-1]["provider_message_id"] == "manual-sent-001"


@pytest.mark.parametrize("from_me,success,status", [
    (False, True, None),
    (True, False, None),
    (True, None, "failed"),
    (True, None, "error"),
])
def test_message_sent_rejects_inbound_and_failed_delivery(rig, from_me, success, status):
    db, cycle, client = rig
    p = event(from_me=from_me, event_name="message.sent", msg_id="no-write")
    if success is not None:
        p["data"]["success"] = success
    if status is not None:
        p["data"]["status"] = status
    assert post(client, p).json()["status"] == "ignored"
    assert db["leads"].update_calls == 0


def test_message_sent_never_captures_private_contact_or_stale_cycle(rig):
    db, cycle, client = rig
    private = event(from_me=True, event_name="message.sent", phone=PRIVATE)
    private["data"]["success"] = True
    assert post(client, private).json()["status"] == "ignored"
    cycle["assigned_to_user_id"] = str(ObjectId("69796bc4bbebf240378eb738"))
    sent = event(from_me=True, event_name="message.sent", msg_id="old-owner")
    sent["data"]["success"] = True
    assert post(client, sent).json()["status"] == "ignored"
    assert db["leads"].update_calls == 0


def test_private_unassigned_and_group_conversations_never_saved(rig, caplog):
    db, cycle, client = rig
    assert post(client, event(from_me=True, phone=PRIVATE,
                              text="UNRELATED PERSONAL SECRET")).json()["status"] == "ignored"
    group = event(from_me=False, jid="121212121@g.us", text="GROUP PERSONAL SECRET")
    assert post(client, group).json()["status"] == "ignored"
    assert db["leads"].update_calls == 0
    assert len(db["leads"].lead["messages"]) == 1
    assert "UNRELATED PERSONAL SECRET" not in caplog.text
    assert "GROUP PERSONAL SECRET" not in caplog.text
    assert PRIVATE not in caplog.text


def test_reassignment_old_cycle_and_closed_user_are_rejected(rig):
    db, cycle, client = rig
    cycle["assigned_to_user_id"] = str(ObjectId("69796bc4bbebf240378eb738"))
    assert post(client, event(from_me=True)).json()["status"] == "ignored"
    cycle["assigned_to_user_id"] = str(USER)
    db["leads"].lead["lifecycle"]["current_assignment_cycle_id"] = "cycle-B"
    assert post(client, event(from_me=False)).json()["status"] == "ignored"
    db["leads"].lead["lifecycle"]["current_assignment_cycle_id"] = "cycle-A"
    db["usuarios"].user["is_active"] = False
    assert post(client, event(from_me=True)).status_code == 401
    assert db["leads"].update_calls == 0


def test_stale_atomic_write_reassignment_returns_not_saved(rig):
    db, cycle, client = rig
    # Simulates another worker transferring the lead after the initial read.
    leads = db["leads"]
    original = leads.update_one
    def concurrent_transfer(query, update):
        leads.lead["lifecycle"]["current_assignment_cycle_id"] = "cycle-B"
        return original(query, update)
    leads.update_one = concurrent_transfer
    assert post(client, event(from_me=True)).json()["status"] == "duplicate_or_reassigned"
    assert len(leads.lead["messages"]) == 1


def test_invalid_signature_disabled_write_and_bad_secret_are_fail_closed(rig, monkeypatch):
    db, cycle, client = rig
    assert post(client, event(from_me=True), signature="bad").status_code == 401
    assert db["leads"].update_calls == 0
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_WRITE_ENABLED", "0")
    assert post(client, event(from_me=True)).json()["status"] == "dry_run"
    assert db["leads"].update_calls == 0
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_ENABLED", "0")
    assert post(client, event(from_me=True)).status_code == 503
    assert db["leads"].update_calls == 0


def test_unambiguous_lid_resolves_with_read_only_metadata_and_filters(rig, monkeypatch):
    db, cycle, client = rig
    calls = []
    def resolver(lid, token):
        calls.append((lid, token))
        return PHONE if lid == "123456789012@lid" else PRIVATE
    monkeypatch.setattr(audit, "_pn_from_lid", resolver)
    outgoing = event(from_me=True, jid="123456789012@lid", msg_id="lid-001")
    assert post(client, outgoing).json()["status"] == "stored"
    private = event(from_me=False, jid="123456789013@lid", msg_id="private-lid")
    assert post(client, private).json()["status"] == "ignored"
    assert len(db["leads"].lead["messages"]) == 2
    assert len(calls) == 2


def test_unknown_or_ambiguous_lead_fails_closed(rig):
    db, cycle, client = rig
    original_find = db["leads"].find
    def duplicates(query, proj):
        return Cursor([db["leads"].lead, dict(db["leads"].lead)])
    db["leads"].find = duplicates
    assert post(client, event(from_me=True)).json()["status"] == "ignored"
    assert db["leads"].update_calls == 0


def test_malformed_event_and_missing_trustworthy_timestamp_ignored(rig):
    db, cycle, client = rig
    missing = event(from_me=True, msg_id="no-timestamp")
    missing.pop("timestamp")
    assert post(client, missing).json()["status"] == "unverified_timestamp"
    other = event(from_me=False, event_name="message.sent")
    assert post(client, other).json()["status"] == "ignored"
    assert db["leads"].update_calls == 0


def test_storage_error_does_not_log_any_personal_content(rig, caplog):
    db, cycle, client = rig
    db["leads"].raise_on_update = True
    p = event(from_me=True, text="PRIVATE TEXT MUST NEVER BE LOGGED")
    resp = post(client, p)
    assert resp.status_code == 503
    assert "PRIVATE TEXT MUST NEVER BE LOGGED" not in caplog.text
    assert "sensitive-message-error" not in caplog.text


def test_name_and_session_binding_prevent_cross_executive_secret_reuse(rig, monkeypatch):
    db, cycle, client = rig
    assert audit._variable_name_for_executive("Mariela Arriagada") == "PROCASA_WASENDER_MARIELA_ARRIAGADA"
    assert audit._variable_name_for_executive("Rocío Aliaga") == "PROCASA_WASENDER_ROCIO_ALIAGA"
    assert audit._variable_name_for_executive(None) is None
    # A renamed executive cannot use the previous name's secret.
    db["usuarios"].user["nombre"] = "Pablo Nueva Cuenta"
    assert post(client, event(from_me=True)).status_code == 401
    db["usuarios"].user["nombre"] = "Pablo Galleguillos"
    # The old shared JSON variable is never consulted.
    monkeypatch.setenv(EXEC_ENV, "")
    monkeypatch.setenv(LEGACY_ENV, json.dumps({
        SESSION: {"webhook_secret": SECRET, "api_token": TOKEN,
                  "capture_enabled": True}
    }))
    assert post(client, event(from_me=True)).status_code == 401
    assert db["leads"].update_calls == 0


def test_duplicated_active_executive_name_is_rejected(rig):
    db, cycle, client = rig
    original_find = db["usuarios"].find
    def ambiguous(query, projection):
        if "nombre" in query:
            return Cursor([db["usuarios"].user, dict(db["usuarios"].user)])
        return original_find(query, projection)
    db["usuarios"].find = ambiguous
    assert post(client, event(from_me=True)).status_code == 401
    assert db["leads"].update_calls == 0


def test_outgoing_lid_never_uses_linked_executive_as_remote_contact(rig, monkeypatch):
    db, cycle, client = rig
    # In some provider payloads "from" is the sending device, not the
    # recipient. Trusting it can misattribute a private outgoing chat.
    outgoing = event(from_me=True, jid="123456789012@lid", msg_id="lid-own-from")
    outgoing["data"]["messages"]["from"] = "+56983219804"
    assert audit._peer_phone(audit._event_metadata(outgoing), None)[0] is None
    # A read-only @lid -> phone lookup may resolve the real remote user.
    monkeypatch.setattr(audit, "_pn_from_lid", lambda lid, token: PHONE)
    assert post(client, outgoing).json()["status"] == "stored"
    assert db["leads"].lead["messages"][-1]["actor_type"] == "human_agent"


def test_outgoing_unknown_lid_with_own_from_never_enters_lead(rig, monkeypatch):
    db, cycle, client = rig
    outgoing = event(from_me=True, jid="123456789012@lid", msg_id="unknown-lid")
    outgoing["data"]["messages"]["from"] = "+56983219804"
    monkeypatch.setattr(audit, "_pn_from_lid", lambda lid, token: None)
    assert post(client, outgoing).json()["status"] == "ignored"
    assert db["leads"].update_calls == 0


def test_authorized_append_atomically_queues_hybrid_classification(rig):
    db, cycle, client = rig
    assert post(client, event(from_me=True, msg_id="queued-out")).json()["status"] == "stored"
    mutation = db["leads"].last_mutation
    assert mutation["$set"]["commercial_intelligence.pending"] is True
    assert mutation["$set"]["commercial_intelligence.pending_cycle_id"] == "cycle-A"
    assert mutation["$set"]["commercial_intelligence.pending_session_id"] == SESSION
    assert mutation["$set"]["commercial_intelligence.pending_message_id"] == mutation["$push"]["messages"]["message_id"]
    prior = db["leads"].last_mutation
    assert post(client, event(from_me=True, msg_id="queued-out")).json()["status"] == "duplicate_or_reassigned"
    assert db["leads"].last_mutation is prior


def test_capture_session_allowlist_is_authoritative_for_incoming_and_outgoing(rig, monkeypatch):
    db, cycle, client = rig
    # A different session's legacy JSON says enabled; the explicit list
    # prevents that session from saving any customer or executive messages.
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_CAPTURE_SESSION_IDS", "133517")
    assert post(client, event(from_me=True, msg_id="out-unselected")).json()["status"] == "dry_run"
    assert post(client, event(from_me=False, msg_id="in-unselected")).json()["status"] == "dry_run"
    assert db["leads"].update_calls == 0

    # An explicitly selected session may capture without replacing its token
    # or webhook secret, even when its legacy JSON has capture_enabled=false.
    monkeypatch.setenv(EXEC_ENV, session_config(capture_enabled=False))
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_CAPTURE_SESSION_IDS", "133286, 133517")
    assert post(client, event(from_me=True, msg_id="manual-selected")).json()["status"] == "stored"
    assert post(client, event(from_me=False, msg_id="reply-selected")).json()["status"] == "stored"
    assert [m["actor_type"] for m in db["leads"].lead["messages"][-2:]] == [
        "human_agent", "customer",
    ]
    assert all(m["wasender_session_id"] == SESSION for m in db["leads"].lead["messages"][-2:])


@pytest.mark.parametrize("invalid", [
    "all", "*", "133286,", "133286,133286", "133286,abc",
    "133286," + ",".join(str(i) for i in range(11)),
])
def test_malformed_session_capture_allowlist_fails_closed(rig, monkeypatch, invalid):
    db, cycle, client = rig
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_CAPTURE_SESSION_IDS", invalid)
    assert post(client, event(from_me=True)).json()["status"] == "dry_run"
    assert db["leads"].update_calls == 0


def test_capture_allowlist_does_not_bypass_global_write_gate(rig, monkeypatch):
    db, cycle, client = rig
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_CAPTURE_SESSION_IDS", SESSION)
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_WRITE_ENABLED", "0")
    assert post(client, event(from_me=False)).json()["status"] == "dry_run"
    assert db["leads"].update_calls == 0

@pytest.mark.parametrize("text", [
    "STOP", "No me contacten más", "No quiero más avisos",
    "No me manden más promociones", "No quiero recibir mensajes",
])
def test_customer_global_stop_atomically_enforces_campaign_guard(rig, text):
    from chatbot.price_drop_campaign_classifier import lead_must_not_be_contacted
    from chatbot.price_drop_leads_v2 import _contact_suppression_reasons
    db, _cycle, client = rig
    response = post(client, event(from_me=False, text=text, msg_id="stop-inbound"))
    assert response.status_code == 200
    assert response.json()["status"] == "stored"
    mutation = db["leads"].last_mutation
    changes = mutation["$set"]
    assert changes["price_drop_v2.opted_out"] is True
    assert changes["price_drop_campaign_v1.opted_out"] is True
    assert changes["contact_restrictions.marketing_opt_out"] is True
    assert changes["contact_restrictions.marketing_opt_out_source"] == "executive_whatsapp"
    assert changes["contact_restrictions.marketing_opt_out_provider_id"] == "stop-inbound"
    assert changes["contact_restrictions.marketing_opt_out_session_id"] == SESSION
    # The sender's existing opt-out guards see durable fields, not the
    # executive transcript (which must stay outside office automation).
    canonical = {"price_drop_v2": {"opted_out": changes["price_drop_v2.opted_out"]},
                 "price_drop_campaign_v1": {"opted_out": changes["price_drop_campaign_v1.opted_out"]}}
    assert lead_must_not_be_contacted(canonical)
    assert "explicit_opt_out" in _contact_suppression_reasons(canonical)
    assert post(client, event(from_me=False, text=text, msg_id="stop-inbound")).json()["status"] == "duplicate_or_reassigned"


@pytest.mark.parametrize("text", [
    "No quiero agendar una visita", "No me escriban más por esa propiedad",
    "No quiero más avisos de esa casa", "No quiero que me den de baja",
    '"No me contacten más" dijo mi vecino',
    "¿Qué ocurre si digo STOP?", "Por favor no me llamen por teléfono",
])
def test_non_global_customer_phrases_never_set_campaign_optout(rig, text):
    db, _cycle, client = rig
    response = post(client, event(from_me=False, text=text))
    assert response.json()["status"] == "stored"
    assert "price_drop_v2.opted_out" not in db["leads"].last_mutation["$set"]
    assert "price_drop_campaign_v1.opted_out" not in db["leads"].last_mutation["$set"]


def test_executive_outgoing_stop_cannot_opt_out_customer(rig):
    db, _cycle, client = rig
    assert post(client, event(from_me=True, text="No me contacten más")).json()["status"] == "stored"
    assert "price_drop_v2.opted_out" not in db["leads"].last_mutation["$set"]


def test_optout_requires_authorized_write_not_dry_run(rig, monkeypatch):
    db, _cycle, client = rig
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_WRITE_ENABLED", "0")
    assert post(client, event(from_me=False, text="No me contacten más")).json()["status"] == "dry_run"
    assert db["leads"].update_calls == 0
    assert db["leads"].last_mutation is None



def test_crm_api_message_and_webhook_echo_are_one_canonical_message():
    """Actual observed provider pattern: numeric API ID + WhatsApp hex key."""
    import mongomock
    from datetime import timedelta
    db = mongomock.MongoClient().echo_test
    now = datetime.now(timezone.utc)
    lead_id = ObjectId()
    cycle = "executive-echo-cycle"
    db.leads.insert_one({
        "_id": lead_id, "phone": "+56996762064",
        "lifecycle": {"current_assignment_cycle_id": cycle, "assigned_to_user_id": str(USER)},
        "messages": [{
            "message_id": "api-accepted-1", "role": "agent",
            "actor_type": "human_agent", "source": "crm_manual_api",
            "channel": "executive_whatsapp",
            "wasender_session_id": SESSION,
            "assigned_executive_id": str(USER), "assignment_cycle_id": cycle,
            "provider_message_id": "98648827",
            "timestamp": (now - timedelta(seconds=1)).isoformat(),
            "content": "Buenas tardes, mi nombre es Hernán. ¿En qué puedo ayudar? ",
        }],
    })
    session = {"session_id": SESSION, "user_id": str(USER)}
    active = {"assignment_cycle_id": cycle, "assigned_at": now - timedelta(minutes=10)}
    parsed = {"from_me": True, "content": "Buenas tardes, mi nombre es Hernán. ¿En qué puedo ayudar?",
              "timestamp": now.isoformat(), "timestamp_source": "provider",
              "provider_message_id": "3EB028719C15348F91AB0D"}
    result = audit._record_authorized(db, session=session, phone="+56996762064",
                                      parsed=parsed, lead=db.leads.find_one({"_id": lead_id}), cycle=active)
    assert result == "echo_reconciled"
    stored = db.leads.find_one({"_id": lead_id})["messages"]
    assert len(stored) == 1
    assert stored[0]["provider_message_id"] == "98648827"
    assert stored[0]["provider_echo_message_id"] == "3EB028719C15348F91AB0D"
    assert audit._record_authorized(db, session=session, phone="+56996762064",
                                    parsed=parsed, lead=db.leads.find_one({"_id": lead_id}),
                                    cycle=active) == "duplicate_or_reassigned"
    assert len(db.leads.find_one({"_id": lead_id})["messages"]) == 1


def test_new_identical_whatsapp_message_after_echo_is_not_suppressed():
    import mongomock
    from datetime import timedelta
    db = mongomock.MongoClient().echo_test2
    now = datetime.now(timezone.utc)
    lead_id = ObjectId()
    cycle = "echo-distinct-cycle"
    db.leads.insert_one({
        "_id": lead_id, "phone": "+56996762064",
        "lifecycle": {"current_assignment_cycle_id": cycle, "assigned_to_user_id": str(USER)},
        "messages": [{
            "message_id": "api-accepted-1", "role": "agent", "source": "crm_manual_api",
            "channel": "executive_whatsapp", "wasender_session_id": SESSION,
            "assigned_executive_id": str(USER), "assignment_cycle_id": cycle,
            "provider_message_id": "api-999", "provider_echo_message_id": "wa-111",
            "timestamp": (now - timedelta(seconds=2)).isoformat(), "content": "Hola",
        }],
    })
    session = {"session_id": SESSION, "user_id": str(USER)}
    active = {"assignment_cycle_id": cycle, "assigned_at": now - timedelta(minutes=10)}
    parsed = {"from_me": True, "content": "Hola", "timestamp": now.isoformat(),
              "timestamp_source": "provider", "provider_message_id": "wa-222"}
    assert audit._record_authorized(db, session=session, phone="+56996762064",
                                    parsed=parsed, lead=db.leads.find_one({"_id": lead_id}),
                                    cycle=active) == "stored"
    assert len(db.leads.find_one({"_id": lead_id})["messages"]) == 2



def test_optional_diagnostics_explain_private_rejection_without_private_data(
        rig, monkeypatch, caplog):
    db, cycle, client = rig
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_DIAGNOSTIC_SESSIONS", SESSION)
    caplog.set_level("INFO")
    private_text = "SECRET_UNRELATED_CONVERSATION"
    assert post(client, event(
        from_me=True, phone=PRIVATE, text=private_text,
    )).json()["status"] == "ignored"
    assert "EXEC_AUDIT_DIAG session=" + SESSION + " reason=peer_not_authorized" in caplog.text
    assert PRIVATE not in caplog.text
    assert private_text not in caplog.text
    # Only an anonymous reason code, not even a hash of the remote peer.
    assert post(client, event(
        from_me=False, jid="121212121@g.us", text="SECRET_GROUP",
    )).json()["status"] == "ignored"
    assert "reason=non_direct_chat" in caplog.text
    assert "SECRET_GROUP" not in caplog.text
    assert db["leads"].update_calls == 0


def test_diagnostics_are_off_by_default_or_other_session(rig, monkeypatch, caplog):
    db, cycle, client = rig
    monkeypatch.delenv("PROCASA_EXEC_AUDIT_DIAGNOSTIC_SESSIONS", raising=False)
    caplog.set_level("INFO")
    assert post(client, event(from_me=True, phone=PRIVATE)).json()["status"] == "ignored"
    assert "EXEC_AUDIT_DIAG" not in caplog.text
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_DIAGNOSTIC_SESSIONS", "133517")
    assert post(client, event(from_me=True, phone=PRIVATE)).json()["status"] == "ignored"
    assert "EXEC_AUDIT_DIAG" not in caplog.text
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_DIAGNOSTIC_SESSIONS", SESSION + "," + SESSION)
    assert post(client, event(from_me=True, phone=PRIVATE)).json()["status"] == "ignored"
    assert "EXEC_AUDIT_DIAG" not in caplog.text


def test_diagnostics_show_authorized_delivery_and_write_gate_without_content(
        rig, monkeypatch, caplog):
    db, cycle, client = rig
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_DIAGNOSTIC_SESSIONS", SESSION)
    caplog.set_level("INFO")
    secret_content = "CONFIDENTIAL_LEAD_TEXT"
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_WRITE_ENABLED", "0")
    assert post(client, event(from_me=True, text=secret_content)).json()["status"] == "dry_run"
    assert "reason=capture_disabled" in caplog.text
    assert secret_content not in caplog.text
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_WRITE_ENABLED", "1")
    assert post(client, event(from_me=True, text=secret_content)).json()["status"] == "stored"
    assert "reason=stored" in caplog.text
    assert secret_content not in caplog.text
    assert db["leads"].update_calls == 1


def test_diagnostics_distinguish_unsupported_webhook_shape_and_unresolved_lid(
        rig, monkeypatch, caplog):
    db, cycle, client = rig
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_DIAGNOSTIC_SESSIONS", SESSION)
    monkeypatch.setattr(audit, "_pn_from_lid", lambda lid, token: None)
    caplog.set_level("INFO")
    body = event(from_me=True, text="PRIVATE_LID",
                 jid="123456789012@lid")
    assert post(client, body).json()["status"] == "ignored"
    assert "reason=peer_lid_unresolved" in caplog.text
    assert "PRIVATE_LID" not in caplog.text
    assert "123456789012@lid" not in caplog.text
    body = {"event": "session.status", "data": {"status": "connected"}}
    assert post(client, body).json()["status"] == "ignored"
    assert "reason=event_unsupported" in caplog.text



def test_lid_diagnostic_classifies_provider_auth_failure_without_logging_private_ids(
        rig, monkeypatch, caplog):
    db, _cycle, client = rig
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_DIAGNOSTIC_SESSIONS", SESSION)
    caplog.set_level("INFO")
    class MockClient:
        def __init__(self, **_kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def get(self, url, headers):
            assert "pn-from-lid/" in url
            assert headers["Authorization"] == "Bearer " + TOKEN
            return SimpleNamespace(status_code=401)
    monkeypatch.setattr(audit.httpx, "Client", MockClient)
    event_text = "PRIVATE_IDENTIFIER_NEVER_LOGGED"
    assert post(client, event(from_me=True, jid="123456789012@lid",
                              text=event_text)).json()["status"] == "ignored"
    assert "reason=lid_http_auth" in caplog.text
    assert "reason=peer_lid_unresolved" in caplog.text
    assert "123456789012@lid" not in caplog.text
    assert event_text not in caplog.text
    assert TOKEN not in caplog.text
    assert db["leads"].update_calls == 0


def test_lid_diagnostic_distinguishes_unmapped_contact_from_invalid_token(
        rig, monkeypatch, caplog):
    db, _cycle, client = rig
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_DIAGNOSTIC_SESSIONS", SESSION)
    caplog.set_level("INFO")
    class MockClient:
        def __init__(self, **_kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def get(self, url, headers):
            return SimpleNamespace(
                status_code=200, json=lambda: {"success": True, "data": {"pn": None}},
            )
    monkeypatch.setattr(audit.httpx, "Client", MockClient)
    assert post(client, event(from_me=True, jid="123456789012@lid")).json()["status"] == "ignored"
    assert "reason=lid_mapping_unavailable" in caplog.text
    assert "reason=lid_http_auth" not in caplog.text
    assert db["leads"].update_calls == 0



def test_startup_diagnostics_identify_disabled_executive_capture_without_secrets(
        rig, monkeypatch, caplog):
    import asyncio
    db, _cycle, _client = rig
    caplog.set_level("INFO")
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_DIAGNOSTIC_SESSIONS", SESSION)
    monkeypatch.setenv(EXEC_ENV, session_config(capture_enabled=False))
    asyncio.run(audit.executive_audit_readiness_probe())
    assert "EXEC_AUDIT_READY session=" + SESSION + " state=session_capture_disabled" in caplog.text
    assert SECRET not in caplog.text
    assert TOKEN not in caplog.text
    monkeypatch.setenv(EXEC_ENV, session_config(capture_enabled=True))
    asyncio.run(audit.executive_audit_readiness_probe())
    assert "EXEC_AUDIT_READY session=" + SESSION + " state=capture_ready" in caplog.text
    assert db["leads"].update_calls == 0


def test_startup_diagnostics_distinguish_global_write_gate_and_ignore_invalid_allowlist(
        rig, monkeypatch, caplog):
    import asyncio
    db, _cycle, _client = rig
    caplog.set_level("INFO")
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_DIAGNOSTIC_SESSIONS", SESSION)
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_WRITE_ENABLED", "0")
    asyncio.run(audit.executive_audit_readiness_probe())
    assert "state=global_write_disabled" in caplog.text
    caplog.clear()
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_DIAGNOSTIC_SESSIONS", SESSION + "," + SESSION)
    asyncio.run(audit.executive_audit_readiness_probe())
    assert "EXEC_AUDIT_READY" not in caplog.text
    assert db["leads"].update_calls == 0



def test_explicit_extra_capture_enables_only_named_session_without_rewriting_credentials(
        rig, monkeypatch):
    db, _cycle, client = rig
    # Reproduce Mariela's production failure: a valid signed session with
    # capture disabled and another executive on the original allowlist.
    monkeypatch.setenv(EXEC_ENV, session_config(capture_enabled=False))
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_CAPTURE_SESSION_IDS", "133517")
    assert post(client, event(from_me=True, msg_id="before-opt-in")).json()["status"] == "dry_run"
    assert db["leads"].update_calls == 0
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_CAPTURE_EXTRA_SESSION_IDS", SESSION)
    assert audit._session(db, SESSION)["capture_enabled"] is True
    assert post(client, event(from_me=True, msg_id="after-opt-in")).json()["status"] == "stored"
    assert db["leads"].update_calls == 1
    # Hernan's existing capture remains enabled; everyone else stays unchanged.
    assert audit._capture_enabled_for_session("133517", False) is True
    assert audit._capture_enabled_for_session("133523", False) is False
    # Executive webhook must still strictly exclude an unrelated private peer.
    assert post(client, event(from_me=True, phone=PRIVATE, msg_id="private")).json()["status"] == "ignored"
    assert db["leads"].update_calls == 1


def test_extra_capture_optin_never_bypasses_global_write_gate(rig, monkeypatch):
    db, _cycle, client = rig
    monkeypatch.setenv(EXEC_ENV, session_config(capture_enabled=False))
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_CAPTURE_EXTRA_SESSION_IDS", SESSION)
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_WRITE_ENABLED", "0")
    assert post(client, event(from_me=True)).json()["status"] == "dry_run"
    assert db["leads"].update_calls == 0


@pytest.mark.parametrize("invalid", [
    "bad", "133286,133286", "133286,", " 133286,133286 ",
    "a" * 129, "0", "133286;133517",
])
def test_malformed_extra_capture_grants_no_new_access(rig, monkeypatch, invalid):
    db, _cycle, client = rig
    monkeypatch.setenv(EXEC_ENV, session_config(capture_enabled=False))
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_CAPTURE_SESSION_IDS", "133517")
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_CAPTURE_EXTRA_SESSION_IDS", invalid)
    assert audit._capture_enabled_for_session(SESSION, False) is False
    assert post(client, event(from_me=True)).json()["status"] == "dry_run"
    assert db["leads"].update_calls == 0
