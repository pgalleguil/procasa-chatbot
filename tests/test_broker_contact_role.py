import pytest

from chatbot.broker_contact_role import broker_signal, broker_evidence


@pytest.mark.parametrize("message", [
    "HOLA, QUISIERA SABER SI REALIZAN CANJE.",
    "Hola, ¿hacen canjes?",
    "Soy corredora de propiedades, tengo una clienta interesada.",
    "Somos corredores",
    "Tengo un cliente interesado, ¿podemos hacer canje?",
    "¿Comparten la comisión?",
])
def test_broker_collaboration_detected(message):
    assert broker_signal(message)


@pytest.mark.parametrize("message", [
    "Quiero comprar un departamento.",
    "¿Aceptan permuta de mi casa?",
    "¿Realizan canje de mi departamento?",
    "No soy corredor, busco una propiedad.",
    "Mi corredor me recomendó esta casa.",
    "No quiero canje.",
    "¿Cuándo puedo visitar?",
])
def test_ordinary_customer_not_blocked(message):
    assert not broker_signal(message)


def test_canonical_portal_evidence_not_an_executive_reply():
    lead = {"messages": [
        {"message_id": "inquiry", "actor_type": "customer", "role": "user",
         "content": "HOLA, QUISIERA SABER SI REALIZAN CANJE."},
        {"message_id": "agent", "actor_type": "human_agent", "role": "agent",
         "channel": "executive_whatsapp", "content": "¿Quieres visitar?"},
    ]}
    assert broker_evidence(lead)["message_id"] == "inquiry"


def test_agent_text_and_unscoped_executive_do_not_establish_role():
    lead = {"messages": [
        {"message_id": "agent", "actor_type": "human_agent", "role": "agent",
         "content": "Soy corredor de propiedades"},
        {"message_id": "private", "actor_type": "customer", "role": "user",
         "channel": "executive_whatsapp", "content": "¿Hacen canje?"},
    ]}
    assert broker_evidence(lead) is None


def test_manual_classification_persists_without_automated_opt_out():
    lead = {"commercial_intelligence": {"contact_role": {
        "kind": "BROKER_COLLABORATION", "source": "admin_review", "message_id": "m"}}}
    assert broker_evidence(lead)["kind"] == "BROKER_COLLABORATION"
    assert "opted_out" not in lead
