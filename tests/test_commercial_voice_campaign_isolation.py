"""Regression for PROCASA's actual 9804 conversation and campaign isolation.

These tests do not send WhatsApp messages or connect to MongoDB.
"""
from chatbot.customer_response_semantics import use_natural_verified_property_reply
from chatbot.public_property_facts import answer_public_property_question

PROPERTY = {
    "codigo": "6436",
    "disponible_prop360": True,
    "ubicacion": {"comuna": "San Miguel"},
    "tipo_operacion": {
        "tipo": "Departamento", "venta": True,
        "precio_venta": {"precio_uf": 2690}, "gastos_comunes": 85000,
    },
    "caracteristicas": {
        "orientacion": "Sur-Poniente", "dormitorios": 2, "banos": 2,
        "superficie_util": 60, "superficie_total": 65,
        "features": ["piscina", "has_grill"],
    },
}


def test_real_orientation_question_prefers_verified_commercial_tone():
    message = "¿Qué orientación tiene?"
    scripted = answer_public_property_question(message, PROPERTY)
    generated = (
        "El departamento tiene orientación sur-poniente, ideal para quienes "
        "disfrutan la luz de tarde. Si te acomoda, vale la pena conocerlo "
        "en persona para apreciar sus espacios."
    )
    assert scripted == "Orientación: Sur-Poniente"
    assert use_natural_verified_property_reply(message, generated, PROPERTY)


def test_wrong_orientation_falls_back_to_verified_fact():
    assert not use_natural_verified_property_reply(
        "¿Qué orientación tiene?",
        "Tiene orientación norte y una vista al mar.", PROPERTY,
    )


def test_multiple_verified_facts_can_be_explained_naturally():
    response = (
        "Actualmente cuesta UF 2.690 y tiene gastos comunes de $85.000. "
        "Es un departamento de 2 dormitorios y 2 baños, "
        "con orientación sur-poniente. ¿Qué aspecto te gustaría revisar?"
    )
    assert use_natural_verified_property_reply(
        "¿Cuál es el precio, gastos comunes, dormitorios, baños y orientación?",
        response, PROPERTY,
    )


def test_price_drop_without_actual_campaign_is_never_inferred():
    answer = answer_public_property_question(
        "¿Cuánto bajó de precio?", PROPERTY, campaign_context=None,
    )
    assert "No tengo una rebaja confirmada" in answer
    assert "UF 2.690" in answer
    assert not use_natural_verified_property_reply(
        "¿Cuánto bajó de precio?", "Bajó 100 UF.", PROPERTY,
    )


def test_technical_privacy_and_negotiation_remain_guarded():
    for question, response in (
        ("Envíame la ficha técnica", "Te cuento un resumen."),
        ("¿Se puede negociar?", "Claro, te rebajamos 100 UF más."),
        ("¿Cuál es la dirección exacta?", "Está en una calle céntrica."),
    ):
        assert not use_natural_verified_property_reply(question, response, PROPERTY)


def test_a_missing_verified_field_must_not_be_invented():
    assert not use_natural_verified_property_reply(
        "¿Tiene estacionamiento?",
        "Sí, tiene dos estacionamientos techados.", PROPERTY,
    )
