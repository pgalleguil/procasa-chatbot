from chatbot import phase3_conversation as policy


def _state(message, operation="Venta", history=None):
    messages = list(history or []) + [{"role": "user", "content": message, "message_id": "m1"}]
    return policy.build_conversation_state(
        {"prospecto": {"operacion": operation}}, messages,
        property_context={"operation": operation, "property_id": "P1", "property_code": "P1"},
    )


def test_explicit_readiness_is_extracted_before_nba():
    assert policy.extract_readiness("Tengo crédito aprobado", "Venta")["financing_status"] == "mortgage_preapproved"
    assert policy.extract_readiness("Todavía no tengo preaprobación", "Venta")["financing_status"] == "mortgage_not_started"
    assert policy.extract_readiness("Finalmente será al contado", "Venta")["financing_status"] == "cash"
    assert policy.extract_readiness("Vendí mi casa, tengo el dinero disponible", "Venta")["financing_status"] == "cash"


def test_visit_precedes_qualification_and_preserves_preferences():
    for message in ("Quiero verla mañana", "viernes 11, 14:15 o 16", "sábado no puedo, mejor domingo"):
        state = _state(message)
        action = policy.select_next_best_action(state, message, facts={}, property_resolved=True)
        assert action["next_best_action"] == "PROPOSE_VISIT"
        assert state["visit_preference"]


def test_human_request_is_terminal_for_the_turn():
    message = "Prefiero hablar con una persona"
    state = _state(message)
    action = policy.select_next_best_action(state, message, facts={}, property_resolved=True)
    response = policy.deterministic_response(state, action, {}, message)
    assert action["next_best_action"] == "HANDOFF_HUMAN"
    assert "?" not in response
    assert "crédito" not in response.casefold()


def test_operation_conflict_does_not_become_qualification():
    message = "¿Esta está en venta? ¿Y no la arriendan?"
    state = _state(message, "Venta")
    action = policy.select_next_best_action(state, message, facts={}, property_resolved=True)
    assert action["next_best_action"] == "ASK_CLARIFICATION"


def test_conditional_cash_offer_does_not_overwrite_readiness():
    assert policy.extract_readiness("Si pago al contado, ¿me la dejan más barata?", "Venta") == {}


def test_reservation_keeps_safe_visit_cta_without_confirming_reservation():
    message = "Resérvamela, llego mañana con el dinero"
    state = _state(message)
    action = policy.select_next_best_action(state, message, facts={}, property_resolved=True)
    response = policy.deterministic_response(state, action, {}, message)
    assert "confirmar esa solicitud" in response.casefold()
    assert "visita" in response.casefold()
    assert "reservada" not in response.casefold()


def test_owner_service_request_is_never_converted_into_visit_intent_or_preference():
    message = "Tengo un departamento de 54 m2, piso 8, 2 dormitorios y 3 cuotas. Busco corredora para coordinar y realizar visitas."
    state = _state(message, "Arriendo")
    assert state["actor_intent"] == "OWNER"
    assert not state["visit_intent"]
    assert state["visit_preference"] is None
    assert policy.select_next_best_action(state, message, facts={}, property_resolved=True)["next_best_action"] == "ANSWER_ONLY"


def test_acknowledgements_and_contact_closure_never_request_property_identifier():
    for message in ("perfecto", "👍", "gracias", "Ya me llamaron, muchas gracias"):
        state = _state(message)
        action = policy.select_next_best_action(state, message, facts={}, property_resolved=False)
        assert action["next_best_action"] == "WAIT_CUSTOMER"
        assert policy.deterministic_response(state, action, {}, message) == ""


def test_property_numbers_and_timestamps_are_not_visit_preferences():
    for message in ("54 m2, piso 8, 2 dormitorios, 3 cuotas", "[19:14] 3/4/2026 código 9000"):
        assert policy.extract_visit_preference(message, visit_context=True) is None


def test_common_visit_typos_are_recognized():
    for message in ("Mw gustaría visitarla", "kiero verla", "quiero visistarla", "se pued visitar"):
        state = _state(message)
        assert policy.select_next_best_action(state, message, facts={}, property_resolved=True)["next_best_action"] == "PROPOSE_VISIT"

def test_can_i_see_it_tomorrow_is_a_visit_intent():
    message='¿La puedo ver mañana?'
    state=_state(message)
    assert policy.select_next_best_action(state,message,facts={},property_resolved=True)['next_best_action']=='PROPOSE_VISIT'

def test_photo_question_is_preserved_when_visit_is_secondary():
    message="¿Tiene fotos del patio? Si se ve bien me gustaría visitarla"
    state=_state(message); action=policy.select_next_best_action(state,message,facts={},property_resolved=True)
    response=policy.deterministic_response(state,action,{},message)
    assert "fotos" in response.casefold() and "visita" in response.casefold()


def test_unverified_patio_claim_is_repaired_without_dropping_visit_flow():
    message = "¿Tiene fotos del patio? Si se ve bien me gustaría visitarla"
    state = _state(message)
    raw = "No tengo fotos del patio, pero la casa tiene un patio bien aprovechable. ¿Qué día te acomoda?"
    facts = {"precio_uf": 5200, "dormitorios": 3}
    validation = policy.validate_response(raw, state=state, facts=facts)
    assert "unsupported_property_fact_claim" in validation["reasons"]
    repaired = policy.repair_response(raw, validation, state=state, facts=facts)
    assert "patio bien aprovechable" not in repaired.casefold()
    assert "disponibilidad" in repaired.casefold()
    assert policy.validate_response(repaired, state=state, facts=facts)["valid"]

def test_response_plan_preserves_facts_before_visit():
    message='¿Está disponible, cuánto salen los gastos comunes y puedo verla mañana?'
    state=_state(message)
    plan=policy.build_response_plan(state,message,{'gastos_comunes':90000})
    action=policy.select_next_best_action(state,message,facts={'gastos_comunes':90000},property_resolved=True)
    response=policy.deterministic_response(state,action,{'gastos_comunes':90000},message)
    assert {'availability','common_expenses'} <= set(plan['primary_intents'])
    assert 'VISIT' in plan['secondary_intents'] and '90000' in response

def test_response_plan_keeps_each_property_topic_visible():
    state=_state('¿Tiene estacionamiento y bodega? ¿Y cuánto mide?')
    plan=policy.build_response_plan(state,'¿Tiene estacionamiento y bodega? ¿Y cuánto mide?',{})
    assert {'parking','storage','surface'} <= set(plan['primary_intents'])


def test_response_plan_tracks_bedrooms_and_commission_questions():
    state = _state("¿Cuánto cuesta y cuántos dormitorios tiene?")
    plan = policy.build_response_plan(
        state, "¿Cuánto cuesta y cuántos dormitorios tiene?", {"precio_uf": 5200, "dormitorios": 3}
    )
    assert {"price", "bedrooms"} <= set(plan["primary_intents"])
    owner_state = _state("¿Cuánto cobran de comisión?", "Arriendo")
    owner_state["actor_intent"] = "OWNER"
    owner_plan = policy.build_response_plan(owner_state, "¿Cuánto cobran de comisión?", {})
    assert "commission" in owner_plan["primary_intents"]


def test_unsupported_operational_promises_and_early_personal_data_are_repaired():
    state=_state('Quiero verla mañana')
    response='Puedo registrar tu interés y un ejecutivo te contactará. ¿Me confirmas tu nombre completo?'
    validation=policy.validate_response(response,state=state,facts={})
    repaired=policy.repair_response(response,validation,state=state,facts={'precio_uf':5200})
    assert not validation['valid']
    assert 'registrar' not in repaired.casefold() and 'nombre completo' not in repaired.casefold()

def test_unsupported_market_comparison_is_removed_without_losing_price_fact():
    state=_state('¿Cuánto cuesta?')
    response='Esta propiedad cuesta UF 5.200. Es un valor referencial dentro del rango de la zona.'
    validation=policy.validate_response(response,state=state,facts={'precio_uf':5200})
    repaired=policy.repair_response(response,validation,state=state,facts={'precio_uf':5200})
    assert 'unsupported_market_claim' in validation['reasons']
    assert 'UF 5.200' in repaired and 'rango de la zona' not in repaired

def test_unsupported_org_fee_and_contact_promises_are_removed():
    state=_state('Quiero arrendar mi departamento')
    response='En Procasa cobramos un mes de arriendo más IVA. Te derivo con un ejecutivo.'
    validation=policy.validate_response(response,state=state,facts={})
    assert {'unsupported_organization_claim','unsupported_operational_claim'} <= set(validation['reasons'])
    assert policy.repair_response(response,validation,state=state) == 'No tengo las condiciones comerciales confirmadas en la información disponible.'


def test_unverified_owner_visit_process_and_exclusivity_claims_are_repaired():
    state = _state("Quiero vender mi casa y saber si trabajan sin exclusividad", "Venta")
    response = (
        "Sí, trabajamos con y sin exclusividad. Nuestro ejecutivo se encarga de agendar las visitas "
        "y siempre te avisamos con anticipación. Es sin costo y sin compromiso. "
        "Si quieres adelantar tu nombre completo, RUT y correo, agilizamos la coordinación."
    )
    validation = policy.validate_response(response, state=state, facts={})
    assert "unsupported_organization_claim" in validation["reasons"]
    assert "unsupported_operational_claim" in validation["reasons"]
    assert "premature_personal_data_request" in validation["reasons"]
    repaired = policy.repair_response(response, validation, state=state, facts={})
    assert "trabajamos" not in repaired.casefold()
    assert "se encarga" not in repaired.casefold()
    assert "nombre completo" not in repaired.casefold()


def test_owner_visit_process_repair_answers_process_without_buyer_cta():
    state = _state("¿Cómo hacen las visitas?", "Venta")
    state["actor_intent"] = "OWNER"
    raw = "Coordinamos las visitas con los interesados. ¿Qué día te acomoda para recibir al ejecutivo?"
    validation = policy.validate_response(raw, state=state, facts={})
    repaired = policy.repair_response(raw, validation, state=state, facts={}, customer_message="¿Cómo hacen las visitas?")
    assert "procedimiento específico" in repaired
    assert "¿Qué día" not in repaired


def test_visit_repair_does_not_duplicate_existing_schedule_question():
    state = _state("¿Qué día puedo ir?", "Venta")
    raw = "No puedo confirmarte un día específico; la disponibilidad la valida el ejecutivo. ¿Qué día te acomoda?"
    validation = policy.validate_response(raw, state=state, facts={})
    repaired = policy.repair_response(raw, validation, state=state, facts={})
    assert repaired.count("¿Qué día") == 1
    assert repaired.casefold().count("disponibilidad") >= 1


def test_unverified_property_narrative_is_removed_from_customer_payload():
    state = _state("Busco un departamento en Providencia")
    raw = "La propiedad tiene buena luz natural y está a pocas cuadras del metro."
    facts = {"dormitorios": 2, "precio_clp": 800000}
    validation = policy.validate_response(raw, state=state, facts=facts)
    assert "unsupported_property_fact_claim" in validation["reasons"]
    repaired = policy.repair_response(raw, validation, state=state, facts=facts)
    assert "luz natural" not in repaired.casefold()
    assert "pocas cuadras" not in repaired.casefold()


def test_explicit_unknown_attribute_answer_is_not_blocked_as_positive_claim():
    state = _state("¿Tiene bodega?")
    response = "No puedo confirmar si incluye bodega con la información disponible."
    validation = policy.validate_response(
        response,
        state=state,
        facts={"estacionamientos": 1, "superficie_util": 120},
    )
    assert validation["valid"] is True


def test_future_executive_contact_variants_are_blocked():
    state = _state("Quiero verla mañana")
    for response in (
        "El ejecutivo asignado te va a contactar.",
        "Un ejecutivo se comunicará contigo.",
        "Te llamará un ejecutivo.",
        "Un ejecutivo de Procasa te contacta y él confirma la disponibilidad.",
    ):
        validation = policy.validate_response(response, state=state, facts={})
        assert "unsupported_operational_claim" in validation["reasons"]
    validation = policy.validate_response(
        "Él confirma la disponibilidad para mañana.", state=state, facts={}
    )
    assert "availability_claim" in validation["reasons"]
