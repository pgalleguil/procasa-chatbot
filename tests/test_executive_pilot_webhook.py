"""Regression tests for the opt-in single-contact executive WhatsApp pilot.

The pilot must not read or store unrelated chats or call the office bot.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from chatbot import executive_pilot_webhook as pilot


PHONE = "+56911112222"
OTHER = "+56999998888"
LEAD_ID = "690e13577fb5678251136b01"
ACTOR_ID = "690e13577fb5678251136b02"


def event(*, from_me=True, jid="56911112222@s.whatsapp.net", msg_id="m1",
          text="Hola, prueba", event_name="messages.upsert", sender_pn=None):
    return {
        "event": event_name,
        "timestamp": 1794866400,
        "data": {"messages": {
            "key": {
                "id": msg_id,
                "fromMe": from_me,
                "remoteJid": jid,
                "senderPn": sender_pn,
            },
            "messageBody": text,
        }},
    }


def test_manual_outbound_is_agent_without_api_send():
    item = pilot._parse_message(event(), PHONE)
    assert item["from_me"] is True
    assert item["content"] == "Hola, prueba"
    assert item["provider_message_id"] == "m1"


def test_reply_is_customer():
    item = pilot._parse_message(event(from_me=False, text="Quiero visitar"), PHONE)
    assert item is not None and item["from_me"] is False


@pytest.mark.parametrize("payload", [
    event(jid="56999998888@s.whatsapp.net"),
    event(jid="123456789@lid"),
    event(jid="123456789@g.us"),
    event(event_name="message.sent"),
    event(text=""),
])
def test_unapproved_and_unsupported_events_ignored(payload):
    assert pilot._parse_message(payload, PHONE) is None


def test_incoming_lid_uses_sender_phone_only_for_inbound():
    incoming = event(from_me=False, jid="123456789@lid",
                     sender_pn="56911112222@s.whatsapp.net")
    outgoing = event(from_me=True, jid="123456789@lid",
                     sender_pn="56911112222@s.whatsapp.net")
    assert pilot._parse_message(incoming, PHONE) is not None
    assert pilot._parse_message(outgoing, PHONE) is None


def test_outbound_lid_can_use_remote_jid_alt():
    payload = event(jid="123456789@lid")
    payload["data"]["messages"]["key"]["remoteJidAlt"] = "56911112222@s.whatsapp.net"
    assert pilot._parse_message(payload, PHONE) is not None


def test_missing_flags_refuse_to_start(monkeypatch):
    monkeypatch.delenv("PROCASA_EXEC_PILOT_ENABLED", raising=False)
    with pytest.raises(Exception) as exc:
        pilot._pilot_config()
    assert getattr(exc.value, "status_code", None) == 503


class FakeLeads:
    def __init__(self):
        self.doc = {
            "_id": pilot.ObjectId(LEAD_ID),
            "phone": PHONE,
            "conversation_id": "conversation-1",
            "lifecycle": {
                "current_assignment_cycle_id": "cycle-A",
                "assigned_to_user_id": ACTOR_ID,
            },
            "messages": [{"role": "assistant", "content": "Original bot message"}],
        }

    def find_one(self, query, projection=None):
        return self.doc if query["_id"] == self.doc["_id"] else None

    def update_one(self, query, update):
        if (query["lifecycle.current_assignment_cycle_id"]
                != self.doc["lifecycle"]["current_assignment_cycle_id"]
                or query["lifecycle.assigned_to_user_id"]
                != self.doc["lifecycle"]["assigned_to_user_id"]):
            return SimpleNamespace(modified_count=0)
        entry = update["$push"]["messages"]
        if any(m.get("wasender_session_id") == entry["wasender_session_id"]
               and m.get("provider_message_id") == entry["provider_message_id"]
               for m in self.doc["messages"]):
            return SimpleNamespace(modified_count=0)
        self.doc["messages"].append(entry)
        return SimpleNamespace(modified_count=1)


def test_append_preserves_bot_history_and_deduplicates(monkeypatch):
    leads = FakeLeads()
    from chatbot import storage, crm_metrics
    monkeypatch.setattr(storage, "get_db", lambda: {"leads": leads})
    monkeypatch.setattr(crm_metrics, "active_assignment_cycle",
                        lambda db, lead_id: {
                            "assignment_cycle_id": "cycle-A",
                            "assigned_to_user_id": ACTOR_ID,
                            "assigned_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                        })
    cfg = {"lead_id": LEAD_ID, "actor_user_id": ACTOR_ID,
           "contact_phone": PHONE, "session": "Procasa-Piloto-Pablo"}
    parsed = {"from_me": True, "content": "Mensaje escrito en WhatsApp",
              "provider_message_id": "provider-123",
              "timestamp": datetime.now(timezone.utc).isoformat(),
              "timestamp_source": "provider"}
    assert pilot._save_authorized_message(cfg, parsed) == "stored"
    assert pilot._save_authorized_message(cfg, parsed) == "duplicate_or_cycle_changed"
    assert leads.doc["messages"][0]["content"] == "Original bot message"
    assert len(leads.doc["messages"]) == 2
    assert leads.doc["messages"][-1]["assignment_cycle_id"] == "cycle-A"
    assert leads.doc["messages"][-1]["actor_type"] == "human_agent"


def test_owner_changed_refuses_to_write(monkeypatch):
    leads = FakeLeads()
    from chatbot import storage, crm_metrics
    monkeypatch.setattr(storage, "get_db", lambda: {"leads": leads})
    monkeypatch.setattr(crm_metrics, "active_assignment_cycle",
                        lambda db, lead_id: {
                            "assignment_cycle_id": "cycle-B",
                            "assigned_to_user_id": "another-executive",
                            "assigned_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                        })
    cfg = {"lead_id": LEAD_ID, "actor_user_id": ACTOR_ID,
           "contact_phone": PHONE, "session": "Procasa-Piloto-Pablo"}
    parsed = {"from_me": True, "content": "After reassignment",
              "provider_message_id": "provider-124",
              "timestamp": datetime.now(timezone.utc).isoformat(),
              "timestamp_source": "provider"}
    assert pilot._save_authorized_message(cfg, parsed) == "not_authorized"
    assert len(leads.doc["messages"]) == 1



def test_fictitious_lead_end_to_end_signed_webhook(monkeypatch):
    """Simulate a full two-way session without sending WhatsApps or touching MongoDB."""
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from chatbot import storage, crm_metrics

    leads = FakeLeads()
    current_cycle = {
        "assignment_cycle_id": "cycle-A",
        "assigned_to_user_id": ACTOR_ID,
        "assigned_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    monkeypatch.setattr(storage, "get_db", lambda: {"leads": leads})
    monkeypatch.setattr(crm_metrics, "active_assignment_cycle",
                        lambda _db, _lid: current_cycle)
    monkeypatch.setenv("PROCASA_EXEC_PILOT_ENABLED", "1")
    monkeypatch.setenv("PROCASA_EXEC_PILOT_WEBHOOK_SECRET", "secret-fictitious-2026")
    monkeypatch.setenv("PROCASA_EXEC_PILOT_LEAD_ID", LEAD_ID)
    monkeypatch.setenv("PROCASA_EXEC_PILOT_CONTACT_PHONE", PHONE)
    monkeypatch.setenv("PROCASA_EXEC_PILOT_ACTOR_USER_ID", ACTOR_ID)
    monkeypatch.setenv("PROCASA_EXEC_PILOT_SESSION_LABEL", "Procasa-Piloto-Pablo")

    app = FastAPI()
    app.include_router(pilot.router)
    client = TestClient(app)
    path = "/webhook/ejecutivos-piloto"
    headers = {"X-Webhook-Signature": "secret-fictitious-2026"}

    # No valid webhook signature -> no access to contents.
    outgoing = event(from_me=True, msg_id="test-send-001",
                     text="Hola, soy el ejecutivo de prueba")
    response = client.post(path, json=outgoing,
                           headers={"X-Webhook-Signature": "wrong-secret"})
    assert response.status_code == 401
    assert len(leads.doc["messages"]) == 1

    # Only authorized lead's manual outgoing messages are persisted.
    response = client.post(path, json=outgoing, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "stored"
    assert leads.doc["messages"][-1]["role"] == "agent"
    assert leads.doc["messages"][-1]["actor_type"] == "human_agent"
    assert leads.doc["messages"][-1]["actor_id"] == ACTOR_ID

    # A customer reply is attributed to the same original lead and cycle.
    reply = event(from_me=False, msg_id="test-reply-002",
                  text="¿Podemos visitar la propiedad?")
    response = client.post(path, json=reply, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "stored"
    assert leads.doc["messages"][-1]["role"] == "user"
    assert leads.doc["messages"][-1]["actor_type"] == "customer"
    assert leads.doc["messages"][-1]["assignment_cycle_id"] == "cycle-A"

    # Retry of the original event must never create duplicates.
    response = client.post(path, json=reply, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "duplicate_or_cycle_changed"
    assert len(leads.doc["messages"]) == 3
    assert leads.doc["messages"][0]["content"] == "Original bot message"

    # Completely unrelated private conversation must be discarded.
    unrelated = event(from_me=False, jid="56999998888@s.whatsapp.net",
                      msg_id="private-003", text="Mensaje personal privado")
    response = client.post(path, json=unrelated, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert len(leads.doc["messages"]) == 3
    assert "Mensaje personal privado" not in json.dumps(leads.doc, default=str)

    # Upon reassignment, the old executive session can no longer save.
    current_cycle.update(assignment_cycle_id="cycle-B",
                         assigned_to_user_id="other-actor")
    leads.doc["lifecycle"]["current_assignment_cycle_id"] = "cycle-B"
    leads.doc["lifecycle"]["assigned_to_user_id"] = "other-actor"
    late = event(from_me=True, msg_id="late-004",
                 text="Mensaje del anterior ejecutivo")
    response = client.post(path, json=late, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "not_authorized"
    assert len(leads.doc["messages"]) == 3

    # The pilot gate is fail-closed, independent of the office bot.
    monkeypatch.setenv("PROCASA_EXEC_PILOT_ENABLED", "0")
    response = client.post(path, json=outgoing, headers=headers)
    assert response.status_code == 503
    assert len(leads.doc["messages"]) == 3



def test_probe_only_real_route_never_uses_db_or_stores_personal_text(monkeypatch, caplog):
    """The office-owned contact can be used in a webhook probe with no test lead."""
    import logging
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from chatbot import storage

    office_number = "+56983219804"  # Explicitly authorized PROCASA office test peer.
    monkeypatch.setenv("PROCASA_EXEC_PILOT_ENABLED", "1")
    monkeypatch.setenv("PROCASA_EXEC_PILOT_PROBE_ONLY", "1")
    monkeypatch.setenv("PROCASA_EXEC_PILOT_WEBHOOK_SECRET", "synthetic-only-test-secret-2026")
    monkeypatch.setenv("PROCASA_EXEC_PILOT_CONTACT_PHONE", office_number)
    monkeypatch.setenv("PROCASA_EXEC_PILOT_SESSION_LABEL", "Procasa-Piloto-Pablo")
    # The probe must not require a lead, cycle or actor ID.
    monkeypatch.delenv("PROCASA_EXEC_PILOT_LEAD_ID", raising=False)
    monkeypatch.delenv("PROCASA_EXEC_PILOT_ACTOR_USER_ID", raising=False)

    def forbidden_db():
        raise AssertionError("probe must not open Mongo")
    monkeypatch.setattr(storage, "get_db", forbidden_db)
    app = FastAPI()
    app.include_router(pilot.router)
    client = TestClient(app)
    url = "/webhook/ejecutivos-piloto"
    headers = {"X-Webhook-Signature": "synthetic-only-test-secret-2026"}

    outgoing = event(jid="56983219804@s.whatsapp.net",
                     from_me=True, msg_id="probe-out-1",
                     text="Texto confidencial del ejecutivo")
    incoming = event(jid="56983219804@s.whatsapp.net",
                     from_me=False, msg_id="probe-in-1",
                     text="Texto confidencial de un cliente")
    with caplog.at_level(logging.INFO, logger="chatbot.executive_pilot_webhook"):
        response_out = client.post(url, json=outgoing, headers=headers)
        response_in = client.post(url, json=incoming, headers=headers)

    assert response_out.status_code == 200
    assert response_in.status_code == 200
    assert response_out.json()["direction"] == "manual_outgoing"
    assert response_in.json()["direction"] == "customer_incoming"
    for response in (response_out, response_in):
        assert response.json()["payload_contents_stored"] is False
        assert response.json()["mongo_connections"] == 0
        assert response.json()["outbound_messages_sent"] == 0
        assert "Texto confidencial" not in response.text
        assert office_number not in response.text

    # No other contacts are probed, even with a valid signature.
    private_chat = event(jid="56999998888@s.whatsapp.net",
                         from_me=False, msg_id="private-1",
                         text="Conversación privada no autorizada")
    rejected = client.post(url, json=private_chat, headers=headers)
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "ignored"
    assert "Texto confidencial" not in caplog.text
    assert "Conversación privada" not in caplog.text
    assert office_number not in caplog.text
    assert "probe-out-1" not in caplog.text


def test_probe_does_not_override_disabled_gate(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PROCASA_EXEC_PILOT_ENABLED", "0")
    monkeypatch.setenv("PROCASA_EXEC_PILOT_PROBE_ONLY", "1")
    app = FastAPI()
    app.include_router(pilot.router)
    response = TestClient(app).post(
        "/webhook/ejecutivos-piloto",
        json=event(), headers={"X-Webhook-Signature": "fake"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "pilot_disabled"
