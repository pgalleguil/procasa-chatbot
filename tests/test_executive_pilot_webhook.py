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
