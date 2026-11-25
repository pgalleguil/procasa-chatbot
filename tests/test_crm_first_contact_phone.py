from datetime import datetime, timezone
from chatbot.crm_first_contact_phone import phone_locked, recorded_executive_message

USER = {"rol": "agente"}
BASE = {
    "created_at": "2026-11-26T12:00:00Z",
    "lifecycle": {"current_assignment_cycle_id": "cycle1"},
    "messages": [
        {"role": "user", "actor_type": "customer"},
        {"role": "assistant", "actor_type": "bot", "provider_message_id": "bot1"},
    ],
}


def test_only_new_office_chatbot_and_real_outbound_unlock(monkeypatch):
    monkeypatch.setenv("CRM_FIRST_CONTACT_PHONE_GATE_ENABLED", "1")
    monkeypatch.setenv("CRM_FIRST_CONTACT_PHONE_GATE_CUTOVER_AT", "2026-11-26T00:00:00Z")
    assert phone_locked(BASE, USER, api_enabled=True)
    assert not phone_locked(BASE, USER, api_enabled=False)
    assert not phone_locked(BASE, {"rol": "admin"}, api_enabled=True)
    assert not phone_locked({**BASE, "source_type": "manual"}, USER, api_enabled=True)
    assert not phone_locked({**BASE, "canal_envio": "Convecta (Prop360)"}, USER, api_enabled=True)
    assert not phone_locked({**BASE, "created_at": "2026-11-25T12:00:00Z"}, USER, api_enabled=True)
    m = {"role": "agent", "actor_type": "human_agent", "channel": "executive_whatsapp",
         "source": "crm_manual_api", "provider_message_id": "provider1", "wasender_session_id": "1",
         "assignment_cycle_id": "cycle1", "assigned_executive_id": "owner"}
    assert not phone_locked({**BASE, "messages": BASE["messages"] + [m]}, USER, api_enabled=True)
    assert not phone_locked({**BASE, "first_contact_phone_access": {"verified": True},
                            "lifecycle": {"current_assignment_cycle_id": "cycle2"}}, USER, api_enabled=True)
    assert not phone_locked({**BASE, "first_contact_phone_access": {"contingency": {"cycle_id": "cycle1"}}},
                            USER, api_enabled=True)
    assert phone_locked({**BASE, "first_contact_phone_access": {"contingency": {"cycle_id": "old"}}},
                        USER, api_enabled=True)


def test_auto_greeting_is_valid_evidence():
    assert recorded_executive_message({"messages": [{
        "role": "assistant", "actor_type": "automation", "source": "crm_hot_auto_api",
        "channel": "executive_whatsapp", "provider_message_id": "id", "wasender_session_id": "1",
        "assignment_cycle_id": "c", "assigned_executive_id": "u",
    }]})
    assert not recorded_executive_message(BASE)
