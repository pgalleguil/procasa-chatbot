"""Regression: the real QA question about property 6436 must remain conversational.

Pure tests: never send WhatsApp messages, touch MongoDB or call the LLM.
"""
from chatbot.conversation_policy import (
    is_explicit_visit_intent, is_visit_offer_question, should_offer_visit_data,
)
from chatbot.customer_response_semantics import build_specific_property_response
from chatbot.public_property_facts import (
    prefers_natural_property_overview, public_property_summary,
)


QUESTION_9804 = (
    "Hola, vi un departamento de Procasa en San Miguel, código 6436. "
    "Me gustaría conocer toda la información de la propiedad: precio, "
    "metros cuadrados, dormitorios, baños, gastos comunes, orientación, "
    "piso y equipamiento. ¿Me puedes contar los detalles?"
)
PROPERTY = {
    "codigo": "6436", "disponible_prop360": True,
    "ubicacion": {"comuna": "San Miguel"},
    "tipo_operacion": {
        "tipo": "Departamento", "venta": True,
        "precio_venta": {"precio_uf": 2690},
        "gastos_comunes": 85000,
    },
    "caracteristicas": {
        "dormitorios": 2, "banos": 2, "superficie_util": 60,
        "superficie_total": 65, "piso": 6,
        "orientacion": "Sur-Poniente",
        "features": ["piscina", "has_grill"],
    },
}


def test_full_real_qa_question_uses_conversational_generation():
    assert prefers_natural_property_overview(QUESTION_9804)
    # None means the conversational answer supplied by the grounded LLM is
    # retained, instead of being overwritten with the technical sheet.
    assert build_specific_property_response(QUESTION_9804, PROPERTY) is None
    assert "UF 2.690" in public_property_summary(PROPERTY)


def test_full_technical_sheet_is_still_supported():
    text = "Por favor, envíame la ficha técnica completa del 6436"
    assert not prefers_natural_property_overview(text)
    assert "Dormitorios: 2" in build_specific_property_response(text, PROPERTY)


def test_real_qa_question_is_not_visit_intent():
    assert not is_explicit_visit_intent(QUESTION_9804)
    assert not should_offer_visit_data(QUESTION_9804, "agendar_visita")


def test_general_interest_or_property_facts_do_not_open_visit_confirmation():
    for text in (
        "Propiedad 6436. Baño de visitas, piscina y quincho.",
        "Tiene 2 baños, uno de visitas. ¿Quieres conocer los gastos comunes?",
        "Si te interesa, te cuento las características.",
    ):
        assert not is_visit_offer_question(text)


def test_only_explicit_offer_opens_visit_confirmation():
    assert is_visit_offer_question("¿Te gustaría coordinar una visita?")
    assert is_visit_offer_question("¿Quieres agendar una visita al departamento?")


def test_actual_visit_still_detected():
    for text in (
        "Me gustaría visitarlo mañana a las 10",
        "Quiero conocer el departamento en persona",
        "¿Podemos coordinar una visita?",
    ):
        assert is_explicit_visit_intent(text)
