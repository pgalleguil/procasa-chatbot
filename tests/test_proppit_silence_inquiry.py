from copy import deepcopy
from datetime import datetime

from chatbot.executive_silence_worker import _verified_proppit_inquiry


def fixture():
    return {
        "source_type": "prop360", "phone": "+56912345678",
        "contact_phone_normalized": "+56912345678",
        "phone_is_synthetic": False, "contact_identity_incomplete": False,
        "source_events": [{"source_system": "prop360", "portal_source": "Proppit", "source_event_id": "test-event"}],
        "messages": [{"source": "prop360", "channel": "portal_form", "portal": "Proppit",
                      "source_event_id": "test-event", "actor_type": "customer", "role": "user",
                      "timestamp": "2026-11-25T23:53:47-03:00",
                      "content": "[Proppit Premium] [Mensaje del cliente] Hola, tengo interés en esta propiedad. Quisiera que me contacten para recibir más información."}],
    }


def test_real_inquiry_provenance_and_explicit_request():
    anchor = datetime.fromisoformat("2026-11-26T09:47:42-03:00")
    lead = fixture()
    assert _verified_proppit_inquiry(lead, anchor)
    for key, value in [("source_events", []), ("contact_phone_normalized", "+56987654321"), ("phone_is_synthetic", True)]:
        changed = deepcopy(lead)
        changed[key] = value
        assert not _verified_proppit_inquiry(changed, anchor)
    for text in ["[Mensaje del cliente] Hola", "[Mensaje del cliente] No quiero que me contacten",
                 "Quisiera que me contacten"]:
        changed = deepcopy(lead)
        changed["messages"][0]["content"] = text
        assert not _verified_proppit_inquiry(changed, anchor)
    assert not _verified_proppit_inquiry(lead, datetime.fromisoformat("2026-11-28T09:47:42-03:00"))
