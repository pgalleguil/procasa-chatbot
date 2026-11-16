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



def test_incomplete_config_logs_only_missing_field_names(monkeypatch, caplog):
    """Sensitive values must never leak to app logs on 503 configuration failures."""
    import logging
    from fastapi import HTTPException

    monkeypatch.setenv("PROCASA_EXEC_PILOT_ENABLED", "1")
    monkeypatch.setenv("PROCASA_EXEC_PILOT_PROBE_ONLY", "1")
    monkeypatch.delenv("PROCASA_EXEC_PILOT_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("PROCASA_EXEC_PILOT_CONTACT_PHONE", PHONE)
    monkeypatch.setenv("PROCASA_EXEC_PILOT_SESSION_LABEL", "Procasa-Piloto-Pablo")
    with caplog.at_level(logging.WARNING, logger="chatbot.executive_pilot_webhook"):
        with pytest.raises(HTTPException) as exc:
            pilot._pilot_config()
    assert exc.value.status_code == 503
    assert "missing_fields=webhook_secret" in caplog.text
    assert PHONE not in caplog.text
    assert "Procasa-Piloto-Pablo" not in caplog.text
    assert "secret_value" not in caplog.text


def test_legacy_wa_sender_direct_data_upsert_is_supported():
    payload = event()
    payload["data"] = payload["data"]["messages"]
    parsed = pilot._parse_message(payload, PHONE)
    assert parsed and parsed["from_me"] is True


def test_pilot_reasons_without_exposing_unrelated_message(monkeypatch, caplog):
    import logging
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PROCASA_EXEC_PILOT_ENABLED", "1")
    monkeypatch.setenv("PROCASA_EXEC_PILOT_PROBE_ONLY", "1")
    monkeypatch.setenv("PROCASA_EXEC_PILOT_WEBHOOK_SECRET", "test-only-secret-of-pilot")
    monkeypatch.setenv("PROCASA_EXEC_PILOT_CONTACT_PHONE", PHONE)
    monkeypatch.setenv("PROCASA_EXEC_PILOT_SESSION_LABEL", "Procasa-Piloto-Pablo")

    app = FastAPI()
    app.include_router(pilot.router)
    client = TestClient(app)
    headers = {"X-Webhook-Signature": "test-only-secret-of-pilot"}

    private = event(jid="56999998888@s.whatsapp.net", text="PRIVATE CHAT MUST NOT LOG")
    with caplog.at_level(logging.INFO, logger="chatbot.executive_pilot_webhook"):
        response = client.post("/webhook/ejecutivos-piloto", data=json.dumps(private), headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert "PRIVATE CHAT MUST NOT LOG" not in caplog.text
    assert "other_contact" not in caplog.text
    assert "56999998888" not in caplog.text

    caplog.clear()
    wrong_event = event(event_name="messages.received")
    with caplog.at_level(logging.INFO, logger="chatbot.executive_pilot_webhook"):
        response = client.post("/webhook/ejecutivos-piloto", data=json.dumps(wrong_event), headers=headers)
    assert response.status_code == 200
    assert "skipped=wrong_event_type" in caplog.text
    assert PHONE not in caplog.text



def test_allowed_lid_matches_only_known_approved_office():
    approved_lid = "123456789012@lid"
    outgoing = event(from_me=True, jid=approved_lid, msg_id="manual-send-1")
    incoming = event(from_me=False, jid=approved_lid, msg_id="bot-reply-1")
    assert pilot._parse_message(outgoing, PHONE) is None
    assert pilot._parse_message(outgoing, PHONE, approved_lid)["from_me"] is True
    assert pilot._parse_message(incoming, PHONE, approved_lid)["from_me"] is False
    other = event(from_me=True, jid="900000000012@lid", msg_id="private")
    assert pilot._parse_message(other, PHONE, approved_lid) is None


def test_lid_lookup_never_queries_unapproved_peer(monkeypatch):
    approved_lid = "123456789012@lid"
    monkeypatch.setenv("WASENDER_PABLO_GALLEGUILLOS_API_TOKEN", "session-fake-token")
    calls = []

    def fake_lookup(allowed_phone, token):
        calls.append((allowed_phone, token))
        return approved_lid

    monkeypatch.setattr(pilot, "_authorized_contact_lid", fake_lookup)
    private = event(jid="900000000012@lid", from_me=True)
    assert pilot._get_allowed_lid(private, PHONE) == approved_lid
    assert calls == [(PHONE, "session-fake-token")]
    # The private chat's LID must not be authorized merely because
    # the approved office LID was resolved successfully.
    assert pilot._parse_message(private, PHONE, approved_lid) is None
    assert "900000000012" not in str(calls)

    calls.clear()
    without_lid = event(jid="56999998888@s.whatsapp.net")
    assert pilot._get_allowed_lid(without_lid, PHONE) is None
    assert calls == []


def test_lid_request_is_read_only_and_does_not_echo_token(monkeypatch):
    class FakeResponse:
        status_code = 200
        def json(self):
            return {"success": True, "data": {"lid": "123456789012@lid"}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            assert kwargs["follow_redirects"] is False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def get(self, url, headers):
            assert url.endswith("56911112222@s.whatsapp.net")
            assert headers["Authorization"] == "Bearer fake-session-token"
            return FakeResponse()

    monkeypatch.setattr(pilot.httpx, "Client", FakeClient)
    pilot._authorized_contact_lid.cache_clear()
    try:
        assert pilot._authorized_contact_lid(PHONE, "fake-session-token") == "123456789012@lid"
    finally:
        pilot._authorized_contact_lid.cache_clear()

