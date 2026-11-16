"""An opt-out is not a ban on responding to a new customer service request."""
from copy import deepcopy
import pytest

from chatbot.customer_initiated_reengagement import customer_initiated_service_request_allowed
from chatbot.price_drop_campaign_classifier import lead_must_not_be_contacted


def stopped_lead():
    return {
        "conversation_status": "STOPPED_BY_CLIENT",
        "conversation_status_reason": "explicit_opt_out",
        "price_drop_v2": {"opted_out": True, "campaigns": [{
            "status": "OPTED_OUT", "last_customer_text": "No quiero más avisos",
        }]},
        "price_drop_campaign_v1": {"opted_out": True},
    }


@pytest.mark.parametrize("message", [
    "Hola, ahora busco un departamento en Maipú",
    "Quiero comprar una casa",
    "Necesito arrendar una propiedad",
    "Busco un arriendo de dos dormitorios",
    "Quiero que me vendan mi departamento",
    "Tengo un departamento que quiero vender",
    "¿Me pueden ayudar a vender una propiedad?",
    "Necesito ayuda con una propiedad",
    "Quiero agendar visita para el departamento 6436",
    "¿Tienen casas en venta en Peñaflor?",
    "Me pueden asesorar con mi casa?",
    "¿Me podrías ayudar?",
    "Quisiera que ustedes administren mi departamento",
    "¿Podrían orientarme para vender mi casa?",
])
def test_explicit_customer_initiated_requests_can_be_answered_without_restoring_marketing(message):
    lead = stopped_lead()
    before = deepcopy(lead)
    assert customer_initiated_service_request_allowed(lead, message) is True
    assert lead_must_not_be_contacted(lead) is True
    assert lead == before


@pytest.mark.parametrize("message", [
    "", "Hola", "Gracias", "Ok", "Muchas gracias pero ya encontré departamento",
    "No quiero más avisos", "STOP", "No quiero comprar una casa",
    "No necesito vender mi propiedad", "No me interesan propiedades",
    "¿Pueden avisarme de futuras rebajas?", "¿Cuándo baja el precio?",
])
def test_greetings_disinterest_and_marketing_requests_do_not_override_stop(message):
    assert customer_initiated_service_request_allowed(stopped_lead(), message) is False


@pytest.mark.parametrize("status", ["BLOCKED", "BLOCKED_EXTERNAL_BROKER"])
def test_security_blocks_remain_blocked(status):
    lead = stopped_lead()
    lead["conversation_status"] = status
    assert customer_initiated_service_request_allowed(
        lead, "Necesito que me ayuden a vender mi casa"
    ) is False


def test_reengagement_is_only_new_inbound_and_does_not_reactivate_campaigns():
    lead = stopped_lead()
    assert customer_initiated_service_request_allowed(
        lead, "Hola, ahora quiero arrendar"
    )
    assert lead["conversation_status"] == "STOPPED_BY_CLIENT"
    assert lead["price_drop_v2"]["opted_out"] is True
    assert lead["price_drop_campaign_v1"]["opted_out"] is True
    assert lead_must_not_be_contacted(lead)


def test_inbound_core_and_commercial_queue_use_same_explicit_request_gate():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    core = (root / "chatbot/core.py").read_text(encoding="utf-8")
    queue = (root / "chatbot/chatbot_queue.py").read_text(encoding="utf-8")
    assert "customer_initiated_service_request_allowed(lead_doc_full, original_message)" in core
    assert "customer_initiated_service_request_allowed(lead, item.get(\"text\") or \"\")" in queue
    assert "for item in allowed_snapshots:" in queue


def test_reengaged_owner_request_never_reuses_old_property_to_assign():
    from chatbot.commercial_intake import _candidate

    lead = {"prospecto": {"codigo": "6436"}}
    assert _candidate("Quiero vender mi casa", lead) == "6436"  # legacy default
    assert _candidate(
        "Quiero vender mi casa", lead, allow_stored_property_fallback=False
    ) is None
    assert _candidate(
        "Ahora me interesa la propiedad 17252", lead,
        allow_stored_property_fallback=False,
    ) == "17252"


def test_suppressed_commercial_intake_disables_old_property_fallback():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] /
           "chatbot/chatbot_queue.py").read_text(encoding="utf-8")
    assert "allow_stored_property_fallback=not globally_suppressed" in src
