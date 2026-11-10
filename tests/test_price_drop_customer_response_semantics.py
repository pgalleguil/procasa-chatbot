from chatbot.customer_response_semantics import (
    evaluate_price_drop_final_response,
    rag_customer_response,
    use_natural_verified_alternative_reply,
)
from chatbot.price_drop_campaign_classifier import classify_price_drop_reply
from chatbot.public_property_facts import answer_public_property_question, requested_public_fact_keys


def _property(commune="La Reina", expenses=85000):
    return {
        "codigo": "12027",
        "disponible_prop360": True,
        "tipo_operacion": {
            "venta": True,
            "tipo": "Departamento",
            "precio_venta": {"precio_uf": 3160, "precio_clp": 123240000},
            "gastos_comunes": expenses,
        },
        "ubicacion": {"region": "Metropolitana", "comuna": commune},
        "caracteristicas": {"dormitorios": 2, "banos": 1},
    }


def _classification(message):
    return classify_price_drop_reply(message).as_dict()


def test_alternative_search_does_not_become_original_property_fact_question():
    message = "¿Me puedes mostrar otras propiedades en La Florida con un precio parecido?"
    assert requested_public_fact_keys(message) == []
    assert answer_public_property_question(message, _property()) is None


def test_realistic_natural_no_match_replies_are_kept_only_when_relevant():
    examples = [
        (
            "¿Me puedes mostrar otras propiedades en La Florida con un precio parecido?",
            "Sobre otras alternativas en La Florida, no me aparecen coincidencias disponibles. ¿Quieres que amplíe la búsqueda?",
            "La Florida",
        ),
        (
            "¿Pueden buscar propiedades en otra comuna, por ejemplo La Florida?",
            "Revisé La Florida y por ahora no encontré opciones que calcen. Podemos ampliar los criterios si quieres.",
            "La Florida",
        ),
        (
            "Me gustaría ver otra comuna, como Las Condes",
            "Busqué alternativas en Las Condes y no encontré coincidencias por ahora. ¿Quieres ampliar la búsqueda?",
            "Las Condes",
        ),
        (
            "Estoy buscando algo por Macul",
            "Revisé opciones en Macul y no encontré propiedades que calcen por ahora. Puedo ampliar algún criterio.",
            "Macul",
        ),
    ]
    for message, response, commune in examples:
        assert use_natural_verified_alternative_reply(
            message, response, requested_communes=[commune],
            original_property_code="12027",
        )


def test_alternative_fact_only_and_wrong_commune_replies_are_rejected():
    message = "Me gustaría ver otra comuna, como Las Condes"
    assert not use_natural_verified_alternative_reply(
        message, "Comuna: La Reina", requested_communes=["Las Condes"],
        original_property_code="12027",
    )
    checks = evaluate_price_drop_final_response(
        message, "Comuna: La Reina", _classification(message),
        original_commune="La Reina", requested_communes=["Las Condes"],
    )
    assert checks["ALTERNATIVE_REQUEST_ANSWERED"] is False
    assert checks["ALTERNATIVE_TARGETS_ADDRESSED"] is False
    assert checks["NO_ORIGINAL_COMMUNE_SUBSTITUTION"] is False


def test_generic_no_results_fallback_includes_requested_commune():
    response = rag_customer_response([], criteria={"comunas": ["Las Condes"]})
    assert "Las Condes" in response
    checks = evaluate_price_drop_final_response(
        "Me gustaría ver otra comuna, como Las Condes", response,
        _classification("Me gustaría ver otra comuna, como Las Condes"),
        original_commune="La Reina", requested_communes=["Las Condes"],
    )
    assert checks["PASS"] is True


def test_offer_routing_answers_who_and_how_without_promising_executive_contact():
    message = "Me interesa hacer una oferta por esta vivienda, ¿con quién la reviso?"
    response = answer_public_property_question(message, _property())
    assert response is not None
    checks = evaluate_price_drop_final_response(
        message, response, _classification(message), original_commune="La Reina",
    )
    assert checks["OFFER_HANDLER_ANSWERED"] is True
    assert checks["PASS"] is True

    old_generic = (
        "Una posible negociación debe evaluarla administración con el propietario; "
        "no puedo confirmar descuentos adicionales."
    )
    assert evaluate_price_drop_final_response(
        message, old_generic, _classification(message),
    )["OFFER_HANDLER_ANSWERED"] is False

    next_step = "Quiero ofrecer $140.000.000 por el departamento. ¿Cuál es el siguiente paso?"
    assert answer_public_property_question(next_step, _property()) == response


def test_alternative_reply_still_rejects_unverified_campaign_price_or_private_data():
    message = "¿Me puedes mostrar otras propiedades en La Florida con un precio parecido?"
    context = {
        "version": "price_drop_leads_v2", "currency": "UF",
        "old_price_uf": 3260, "new_price_uf": 3160,
    }
    assert not use_natural_verified_alternative_reply(
        message,
        "Busqué en La Florida y no encontré opciones. Esta propiedad cuesta UF 9.999.",
        requested_communes=["La Florida"], original_property_code="12006",
        campaign_context=context,
    )
    assert not use_natural_verified_alternative_reply(
        message,
        "Busqué en La Florida; el teléfono del propietario es +56911112222.",
        requested_communes=["La Florida"], original_property_code="12006",
        campaign_context=context,
    )


def test_negated_call_preference_rejects_call_invitation_but_accepts_respectful_reply():
    message = "Sí me interesa, pero no quiero que me llamen."
    classification = _classification(message)
    assert classification["do_not_call"] is True
    assert evaluate_price_drop_final_response(
        message, "¿Quieres que el ejecutivo te llame?", classification,
    )["NO_CALL_PREFERENCE_RESPECTED"] is False
    assert evaluate_price_drop_final_response(
        message, "Entendido; seguiremos por este chat y no te llamaré.", classification,
    )["NO_CALL_PREFERENCE_RESPECTED"] is True


def test_opt_out_requires_no_customer_visible_response():
    message = "No quiero recibir más información, por favor."
    classification = _classification(message)
    assert evaluate_price_drop_final_response(message, "", classification)["PASS"] is True
    assert evaluate_price_drop_final_response(
        message, "Gracias por responder.", classification,
    )["GLOBAL_OPT_OUT_SUPPRESSED"] is False


def test_missing_requested_public_fact_must_be_acknowledged():
    message = "¿Cuánto son los gastos comunes?"
    missing = answer_public_property_question(message, _property(expenses=None))
    assert "no informa" in missing
    assert evaluate_price_drop_final_response(
        message, missing, _classification(message), property_doc=_property(expenses=None),
    )["PASS"] is True
    assert evaluate_price_drop_final_response(
        message, "Los gastos comunes son $85.000.", _classification(message),
        property_doc=_property(expenses=None),
    )["MISSING_FACT_ACKNOWLEDGED"] is False
