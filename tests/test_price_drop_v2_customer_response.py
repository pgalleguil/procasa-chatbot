"""Regression tests for real 2026-11-06 6436 WhatsApp defects."""
from chatbot.customer_response_semantics import build_specific_property_response
from chatbot.price_drop_v2_customer_response import stabilize_v2_reply

CTX = {"version": "price_drop_leads_v2", "property_code": "6436", "new_price_uf": 2690}
PROP = {
    "codigo": "6436",
    "tipo_operacion": {"gastos_comunes": 85000},
    "caracteristicas": {"orientacion": "Sur-Poniente"},
}


def test_question_both_expenses_and_orientation_answered():
    response = build_specific_property_response(
        "cuantos son sus gastos comunes, su orientación ?", PROP
    )
    assert "$85.000" in response
    assert "Sur-Poniente" in response
    assert "ejecutivo" not in response.casefold()


def test_property_fields_not_invented_if_missing():
    response = build_specific_property_response(
        "¿Cuáles son los gastos comunes y orientación?", {"codigo": "6436"}
    )
    assert response.count("no informa") == 2
    assert "$85.000" not in response


def test_v2_interest_response_not_generic_or_automatic_visit():
    response = stabilize_v2_reply(
        "bueno me interesa", "Gracias, sigo atento a tu consulta.", context=CTX
    )
    assert "UF 2.690" in response
    assert "detalle" in response
    assert "visita" not in response.casefold()


def test_v2_preferred_visit_ack_without_booking_claim():
    response = stabilize_v2_reply(
        "bueno me interesa\nmañana a las 10 de la mañana",
        "Gracias, sigo atento a tu consulta.", context=CTX,
        visit_preference="mañana a las 10",
    )
    assert "mañana a las 10" in response
    assert "todavía no está confirmada" in response
    assert "validar" in response


def test_v2_does_not_reinterpret_decline_or_other_property():
    response = "Entiendo. No volveremos a insistir."
    assert stabilize_v2_reply("no me interesa", response, context=CTX) == response
    assert stabilize_v2_reply("me interesa", response, context=None) == response
