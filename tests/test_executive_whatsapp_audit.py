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
SESSIONS_ENV = "PROCASA_EXEC_WASENDER_SESSIONS_JSON"
SECRET = "audit-test-secret-value-2026"
TOKEN = "session-api-test-only-token"


def session_config(*, capture_enabled=True, secret=SECRET, token=TOKEN):
    return json.dumps({SESSION: {
        "webhook_secret": secret, "api_token": token,
        "capture_enabled": capture_enabled,
    }})


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
        allowed = self.user["wasender"]["session_id"] in query["wasender.session_id"]["$in"]
        if self.user["is_active"] is False or not allowed:
            return Cursor([])
        return Cursor([self.user])
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

    def find(self, query, projection):
        return Cursor([self.lead] if self.lead["phone"] in query["phone"]["$in"] else [])

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
    monkeypatch.setenv(SESSIONS_ENV, session_config())
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


def test_central_credentials_must_be_valid_and_never_fallback_to_mongo(rig, monkeypatch):
    db, cycle, client = rig
    for value in (
        "", "not json", "[]", "{}", "{bad",
        json.dumps({SESSION: {"webhook_secret": SECRET}}),
        session_config(secret="short"),
        session_config(token="short"),
        json.dumps({SESSION: {"webhook_secret": SECRET, "api_token": TOKEN,
                               "capture_enabled": "true"}}),
    ):
        monkeypatch.setenv(SESSIONS_ENV, value)
        cfg = audit._session(db, SESSION)
        if '"capture_enabled": "true"' in value:
            assert cfg is not None and cfg["capture_enabled"] is False
        else:
            assert cfg is None
    monkeypatch.setenv(SESSIONS_ENV, "a" * 33000)
    assert audit._session(db, SESSION) is None


def test_per_session_capture_guard_blocks_global_write(rig, monkeypatch):
    db, cycle, client = rig
    monkeypatch.setenv(SESSIONS_ENV, session_config(capture_enabled=False))
    assert post(client, event(from_me=True)).json()["status"] == "dry_run"
    assert db["leads"].update_calls == 0
    # Unregistered session cannot capture content even with global write on.
    config = json.dumps({"133287": {
        "webhook_secret": SECRET, "api_token": TOKEN,
        "capture_enabled": True,
    }})
    monkeypatch.setenv(SESSIONS_ENV, config)
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
    other = event(from_me=True, event_name="message.sent")
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
