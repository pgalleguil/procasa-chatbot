from chatbot.conversation_policy import (
    build_local_fallback_response,
    classify_local_fallback_intent,
    is_property_search_intent,
)
from chatbot.final_response_barrier import _safe_renderer
from chatbot.phase3_conversation import (
    build_conversation_state,
    build_response_plan,
    deterministic_response,
    select_next_best_action,
)


def _stateful_turns(turns):
    lead = {"prospecto": {}, "messages": []}
    states = []
    for message in turns:
        lead["messages"].append({"role": "user", "content": message})
        state = build_conversation_state(lead, lead["messages"])
        action = select_next_best_action(
            state,
            message,
            facts={},
            property_resolved=False,
        )
        response = deterministic_response(state, action, {}, message) or ""
        lead["messages"].append({"role": "assistant", "content": response})
        states.append((state, action, response))
    return states


def test_general_search_is_not_general_fallback():
    assert is_property_search_intent("Busco una propiedad")
    assert classify_local_fallback_intent("Busco una propiedad") == "PROPERTY_SEARCH"

    response, intent = build_local_fallback_response("Busco una propiedad")

    assert intent == "PROPERTY_SEARCH"
    assert "comprar o arrendar" in response.casefold()
    assert "recibimos tu consulta" not in response.casefold()
    assert "enlace" not in response.casefold()
    assert "código" not in response.casefold()


def test_search_fallback_preserves_criteria_from_current_turn():
    response, intent = build_local_fallback_response("Busco departamento en Ñuñoa")

    assert intent == "PROPERTY_SEARCH"
    assert "comprar o arrendar" in response.casefold()
    assert "enlace" not in response.casefold()


def test_specific_reference_stays_out_of_general_search():
    assert classify_local_fallback_intent("https://example.test/listing/7733") == "PROPERTY_SPECIFIC"
    assert classify_local_fallback_intent("información código 7733") == "PROPERTY_SPECIFIC"
    assert not is_property_search_intent("https://example.test/listing/7733")


def test_stateful_search_preserves_criteria_and_asks_one_at_a_time():
    states = _stateful_turns([
        "Hola",
        "Busco una propiedad",
        "Arrendar",
        "Providencia",
        "Departamento",
        "Hasta 900 mil, 2 dormitorios",
    ])

    search_state, search_action, search_response = states[1]
    assert search_state["search_intent"] is True
    assert search_action["next_best_action"] == "SEARCH_PROPERTY"
    assert "comprar o arrendar" in search_response.casefold()

    operation_state, operation_action, operation_response = states[2]
    assert operation_state["search_criteria"]["operation"] == "Arriendo"
    assert operation_action["next_best_action"] == "SEARCH_PROPERTY"
    assert "comuna" in operation_response.casefold() or "sector" in operation_response.casefold()

    commune_state, commune_action, commune_response = states[3]
    assert commune_state["search_criteria"]["commune"] == "Providencia"
    assert commune_action["next_best_action"] == "SEARCH_PROPERTY"
    assert "tipo" in commune_response.casefold()

    complete_state, complete_action, _ = states[-1]
    assert complete_state["search_criteria"]["property_type"] == "Departamento"
    assert complete_state["search_criteria"]["budget"] == "900 mil"
    assert complete_state["search_criteria"]["bedrooms"] == 2
    assert complete_action["next_best_action"] == "SEARCH_PROPERTY"


def test_search_plan_and_rag_result_do_not_require_property_code():
    state = build_conversation_state(
        {"prospecto": {}, "messages": []},
        [{"role": "user", "content": "Busco departamento en Providencia"}],
    )
    plan = build_response_plan(state, "Busco departamento en Providencia", {"rag_result_count": 2})
    action = select_next_best_action(
        state,
        "Busco departamento en Providencia",
        facts={"rag_result_count": 2},
        property_resolved=False,
    )
    response = deterministic_response(state, action, {"rag_result_count": 2}, "Busco departamento en Providencia")

    assert "PROPERTY_SEARCH" in plan["primary_intents"]
    assert "SEARCH_PROPERTY" in plan["business_actions"]
    assert action["next_best_action"] == "SEARCH_PROPERTY"
    assert response and "encontr" in response.casefold()


def test_final_barrier_search_renderer_does_not_ask_for_listing_code():
    state = build_conversation_state(
        {"prospecto": {}, "messages": []},
        [{"role": "user", "content": "Busco una propiedad"}],
    )
    response = _safe_renderer(state, {}, "Busco una propiedad")

    assert response
    assert "enlace" not in response.casefold()
    assert "código" not in response.casefold()


def test_search_context_closes_after_explicit_property_reference():
    lead = {"prospecto": {}, "messages": [
        {"role": "user", "content": "Busco una propiedad"},
        {"role": "assistant", "content": "¿Buscas comprar o arrendar?"},
        {"role": "user", "content": "https://example.test/listing/7733"},
        {"role": "assistant", "content": "Encontré la propiedad."},
        {"role": "user", "content": "¿Tiene estacionamiento?"},
    ]}
    state = build_conversation_state(lead, lead["messages"], property_context={"property_code": "7733"})
    action = select_next_best_action(state, "¿Tiene estacionamiento?", facts={"estacionamientos": 1})

    assert state["property_specific_intent"] is False
    assert state["search_intent"] is False
    assert action["next_best_action"] == "ANSWER_ONLY"


def test_search_context_does_not_override_a_factual_follow_up():
    lead = {"prospecto": {}, "messages": [
        {"role": "user", "content": "Busco departamento en Providencia"},
        {"role": "assistant", "content": "¿Cuántos dormitorios necesitas?"},
        {"role": "user", "content": "¿Tiene estacionamiento?"},
    ]}
    state = build_conversation_state(lead, lead["messages"])
    action = select_next_best_action(
        state,
        "¿Tiene estacionamiento?",
        facts={"estacionamientos": 1},
        property_resolved=False,
    )

    assert state["search_intent"] is True
    assert action["next_best_action"] == "ANSWER_ONLY"
