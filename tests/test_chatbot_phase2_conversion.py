"""Fase 2: conversiones conversacionales, CTA contextual y memoria de criterios."""

import asyncio

import mongomock
import pytest

from chatbot import core, storage
from chatbot.conversation_policy import (
    classify_objection,
    contains_visit_cta,
    extract_spontaneous_lead_signals,
    has_reasonable_visit_interest,
    is_explicit_visit_intent,
    is_just_browsing,
    should_show_visit_cta,
    visit_cta_reply,
    visit_cta_text,
    remove_visit_cta,
)
from chatbot.rag import extraer_filtros_estructurados


def test_simple_question_has_answer_before_cta():
    answer = "Sí, cuenta con estacionamiento."
    cta = visit_cta_text()
    assert answer in f"{answer}\n\n{cta}"
    assert f"{answer}\n\n{cta}".index(answer) < f"{answer}\n\n{cta}".index(cta)


@pytest.mark.parametrize("text", ["Hola", "Info", "Quiero saber más", "Precio?"])
def test_short_entry_messages_are_not_forms(text):
    assert not is_explicit_visit_intent(text)


def test_no_cta_without_interest_signal():
    assert not should_show_visit_cta("Hola", property_id="P-1")


@pytest.mark.parametrize("text", [
    "¿Tiene estacionamiento?",
    "¿Cuántos dormitorios tiene?",
    "¿Cuál es la ubicación?",
    "¿Tiene gastos comunes?",
    "¿Está disponible?",
    "Me gusta la propiedad",
])
def test_reasonable_interest_signals_are_detected(text):
    assert has_reasonable_visit_interest(text)


def test_cta_is_allowed_for_reasonable_interest():
    assert should_show_visit_cta("¿Tiene estacionamiento?", property_id="P-1")


def test_cta_is_not_repeated_in_next_two_customer_turns():
    state = {"status": "shown", "last_shown_turn": 4, "property_id": "P-1"}
    assert not should_show_visit_cta("¿Tiene bodega?", cta_state=state, turn_index=5, property_id="P-1")
    assert not should_show_visit_cta("¿Tiene terraza?", cta_state=state, turn_index=6, property_id="P-1")


def test_cta_reappears_after_cooldown():
    state = {"status": "shown", "last_shown_turn": 4, "property_id": "P-1"}
    assert should_show_visit_cta("¿Tiene terraza?", cta_state=state, turn_index=7, property_id="P-1")


def test_new_property_bypasses_old_cta_cooldown():
    state = {"status": "shown", "last_shown_turn": 4, "property_id": "P-1"}
    assert should_show_visit_cta("¿Tiene terraza?", cta_state=state, turn_index=5, property_id="P-2")


@pytest.mark.parametrize("text", [
    "Quiero verla",
    "Podemos verla mañana",
    "Quiero visitar",
    "¿Cuándo la puedo ver?",
    "Me gustaría conocerla",
    "Quiero agendar una visita",
])
def test_explicit_visit_signals_are_detected(text):
    assert is_explicit_visit_intent(text)


def test_explicit_visit_does_not_need_cta_repetition():
    assert not should_show_visit_cta("Quiero verla", property_id="P-1")


@pytest.mark.parametrize("text", [
    "Está muy caro",
    "No me sirve esa comuna",
    "Necesito algo más grande",
    "No me gustó",
])
def test_objections_are_classified(text):
    assert classify_objection(text) in {"price", "location", "size", "property_rejected"}


def test_objection_does_not_trigger_visit_cta():
    assert not should_show_visit_cta(
        "Está muy caro", property_id="P-1", objection_type=classify_objection("Está muy caro")
    )


def test_only_looking_does_not_pressure():
    assert is_just_browsing("Solo estoy mirando")
    assert classify_objection("Solo estoy mirando") == "just_browsing"
    assert not should_show_visit_cta("Solo estoy mirando", property_id="P-1", objection_type="just_browsing")


def test_cta_acceptance_is_stateful():
    assert visit_cta_reply("Sí", cta_shown=True) == "accepted"
    assert visit_cta_reply("Quiero verla", cta_shown=True) == "accepted"


def test_cta_decline_is_stateful():
    assert visit_cta_reply("No, gracias", cta_shown=True) == "declined"
    assert visit_cta_reply("Solo estoy mirando", cta_shown=True) == "declined"


def test_cta_reply_without_previous_cta_is_not_visit_intent():
    assert visit_cta_reply("Sí", cta_shown=False) == "none"


def test_cta_text_is_safe_and_short():
    assert len(visit_cta_text().split()) <= 12
    assert "confirm" not in visit_cta_text().lower()
    assert "reserva" not in visit_cta_text().lower()


def test_cta_detector_requires_an_offer_not_any_visit_mention():
    assert contains_visit_cta("¿Te gustaría que el ejecutivo coordine una visita contigo?")
    assert not contains_visit_cta("El ejecutivo coordinará el horario contigo.")


def test_repeated_model_cta_can_be_removed_without_losing_answer():
    response = "Sí, tiene estacionamiento. ¿Te gustaría que el ejecutivo coordine una visita contigo?"
    assert remove_visit_cta(response) == "Sí, tiene estacionamiento."


@pytest.mark.parametrize("query,expected", [
    ("Busco departamento en Ñuñoa de 3 dormitorios hasta 6.000 UF", {"comuna": "Ñuñoa", "dormitorios": 3, "presupuesto": 6000.0}),
    ("Quiero algo con 2 baños", {"banos": 2}),
    ("Busco una propiedad con al menos 2 estacionamientos", {"estacionamientos": 2}),
    ("Busco otra en Maipú hasta 4.000 UF", {"comuna": "Maipú", "presupuesto": 4000.0}),
])
def test_structured_criteria_are_available_for_persistence(query, expected):
    filters, _ = extraer_filtros_estructurados(query)
    if "comuna" in expected:
        assert filters["comunas"][0] == expected["comuna"]
    if "dormitorios" in expected:
        assert filters["dormitorios"] == expected["dormitorios"]
    if "banos" in expected:
        assert filters["banos"] == expected["banos"]
    if "estacionamientos" in expected:
        assert filters["estacionamientos"] == expected["estacionamientos"]
    if "presupuesto" in expected:
        assert filters["precio_uf_max"] == expected["presupuesto"]


def test_analytics_signals_keep_operation_scope():
    assert extract_spontaneous_lead_signals("Tengo crédito preaprobado", "Venta") == {"financing_status": "preapproved"}
    assert extract_spontaneous_lead_signals("Ya tengo todos los documentos", "Venta") == {}
    assert extract_spontaneous_lead_signals("Ya tengo todos los documentos", "Arriendo") == {"rental_docs_readiness": "ready"}


def test_no_phone_is_extracted_as_a_criteria_or_signal():
    assert "phone" not in extract_spontaneous_lead_signals("Mi teléfono es 912345678", "Venta")


def test_core_persists_new_search_criteria_and_conversion_metadata(monkeypatch):
    db = mongomock.MongoClient().phase2_criteria
    db["leads"].insert_one({
        "phone": "+56911112222", "conversation_id": "conv-criteria",
        "prospecto": {"codigo": "P-1", "comuna": "Providencia", "operacion": "Venta"},
        "messages": [],
    })
    events = []
    monkeypatch.setattr(core, "get_db", lambda: db)
    monkeypatch.setattr(storage, "get_db", lambda: db)
    monkeypatch.setattr(core, "record_observability_event", lambda event, payload=None: events.append((event, payload)))
    monkeypatch.setattr(storage, "record_observability_event", lambda event, payload=None: events.append((event, payload)))
    monkeypatch.setattr(core, "es_propietario", lambda _phone: (False, None))
    monkeypatch.setattr(core, "clasificar_corredor_externo", lambda _msg: {"is_external_broker": False})
    monkeypatch.setattr(core, "analizar_mensaje_para_link", lambda *args, **kwargs: (False, None, None, None))
    monkeypatch.setattr(core, "_buscar_propiedad_en_universo", lambda *_args, **_kwargs: {"codigo": "P-1", "comuna": "Providencia", "operacion": "Venta", "tipo": "Departamento"})
    monkeypatch.setattr(core, "registrar_propiedades_vistas", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "obtener_propiedades_vistas", lambda _phone: [])
    monkeypatch.setattr(core, "buscar_semanticamente", lambda *args, **kwargs: [])
    monkeypatch.setattr(core, "LeadProcessingService", type("S", (), {"process_lead": staticmethod(lambda *_args: None)}))
    monkeypatch.setattr(core.CrmService, "update_intent", staticmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(core.CrmService, "calculate_score", staticmethod(lambda _lead: 0))
    monkeypatch.setattr(core.CrmService, "get_lead", staticmethod(lambda phone: db["leads"].find_one({"phone": phone}) or {}))
    monkeypatch.setattr(storage, "get_pending_response", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "formatear_ficha_tecnica", lambda *_args, **_kwargs: "Ficha")
    monkeypatch.setattr(core, "generar_respuesta_estructurada", lambda *_args: {"intencion": "consulta_general", "respuesta_bot": "Revisaré esas alternativas.", "datos_extraidos": {}})
    asyncio.run(core.process_user_message(
        "+56911112222", "Busco departamento en Ñuñoa de 3 dormitorios hasta 6.000 UF",
        telemetry_context={"batch_id": "criteria-b1", "generation_id": "criteria-g1"},
    ))
    state = storage.get_rag_search_state("+56911112222")
    assert state["criteria"]["comuna"] == "Ñuñoa"
    assert state["criteria"]["dormitorios"] == 3
    assert state["criteria"]["presupuesto"] == 6000.0
    criteria_events = [payload for event, payload in events if event == "conversation_criteria_updated"]
    assert criteria_events and "comuna" in criteria_events[-1]["criteria_fields_changed"]


def test_alternative_search_does_not_relax_without_acceptance():
    from chatbot.conversation_policy import filter_relaxation_accepted
    assert not filter_relaxation_accepted("No sé", offer_pending=True)
    assert filter_relaxation_accepted("Sí, ampliemos", offer_pending=True)


def test_core_cta_flow_persists_state_and_conversion_event(monkeypatch):
    db = mongomock.MongoClient().phase2
    db["leads"].insert_one({
        "phone": "+56911112222", "conversation_id": "conv-phase2",
        "prospecto": {"codigo": "P-1", "comuna": "Providencia", "operacion": "Venta"},
        "messages": [],
    })
    events = []
    monkeypatch.setattr(core, "get_db", lambda: db)
    monkeypatch.setattr(storage, "get_db", lambda: db)
    monkeypatch.setattr(core, "record_observability_event", lambda event, payload=None: events.append((event, payload)))
    monkeypatch.setattr(storage, "record_observability_event", lambda event, payload=None: events.append((event, payload)))
    monkeypatch.setattr(core, "es_propietario", lambda _phone: (False, None))
    monkeypatch.setattr(core, "clasificar_corredor_externo", lambda _msg: {"is_external_broker": False})
    monkeypatch.setattr(core, "analizar_mensaje_para_link", lambda *args, **kwargs: (False, None, None, None))
    monkeypatch.setattr(core, "_buscar_propiedad_en_universo", lambda *_args, **_kwargs: {
        "codigo": "P-1", "comuna": "Providencia", "operacion": "Venta", "tipo": "Departamento",
        "precio_uf": 5000, "caracteristicas": {"estacionamientos": 1},
    })
    monkeypatch.setattr(core, "registrar_propiedades_vistas", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "obtener_propiedades_vistas", lambda _phone: [])
    monkeypatch.setattr(core, "LeadProcessingService", type("S", (), {"process_lead": staticmethod(lambda *_args: None)}))
    monkeypatch.setattr(core.CrmService, "update_intent", staticmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(core.CrmService, "calculate_score", staticmethod(lambda _lead: 50))
    monkeypatch.setattr(core.CrmService, "get_lead", staticmethod(lambda phone: db["leads"].find_one({"phone": phone}) or {}))
    monkeypatch.setattr(storage, "get_pending_response", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "formatear_ficha_tecnica", lambda *_args, **_kwargs: "Ficha P-1")
    monkeypatch.setattr(core, "generar_respuesta_estructurada", lambda *_args: {
        "intencion": "consulta_general", "respuesta_bot": "Sí, cuenta con estacionamiento.", "datos_extraidos": {},
    })
    response = asyncio.run(core.process_user_message(
        "+56911112222", "¿Tiene estacionamiento?",
        telemetry_context={"batch_id": "phase2-b1", "generation_id": "phase2-g1"},
    ))
    assert response.startswith("Sí, cuenta con estacionamiento.")
    assert "visita" in response.lower()
    assert storage.get_visit_cta_state("+56911112222")["status"] == "shown"
    assert "visit_cta_shown" in [event for event, _ in events]
    handoffs = []
    async def handoff(**kwargs):
        handoffs.append(kwargs)
        return {"status": "enqueued", "durable": "phase2-test"}
    monkeypatch.setattr(core, "send_alert_once", handoff)
    monkeypatch.setattr(core, "generar_respuesta_estructurada", lambda *_args: {
        "intencion": "consulta_general", "respuesta_bot": "Perfecto.", "datos_extraidos": {},
    })
    accepted = asyncio.run(core.process_user_message(
        "+56911112222", "Sí",
        telemetry_context={"batch_id": "phase2-b2", "generation_id": "phase2-g2"},
    ))
    assert handoffs
    assert "opcional" in accepted.lower()
    assert "visit_cta_accepted" in [event for event, _ in events]


def test_core_only_looking_has_no_cta_or_handoff(monkeypatch):
    db = mongomock.MongoClient().phase2_b
    db["leads"].insert_one({"phone": "+56911112222", "prospecto": {"codigo": "P-1", "comuna": "Providencia", "operacion": "Venta"}, "messages": []})
    monkeypatch.setattr(core, "get_db", lambda: db)
    monkeypatch.setattr(storage, "get_db", lambda: db)
    monkeypatch.setattr(core, "record_observability_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(storage, "record_observability_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(core, "es_propietario", lambda _phone: (False, None))
    monkeypatch.setattr(core, "clasificar_corredor_externo", lambda _msg: {"is_external_broker": False})
    monkeypatch.setattr(core, "analizar_mensaje_para_link", lambda *args, **kwargs: (False, None, None, None))
    monkeypatch.setattr(core, "_buscar_propiedad_en_universo", lambda *_args, **_kwargs: {"codigo": "P-1", "comuna": "Providencia", "operacion": "Venta", "tipo": "Departamento"})
    monkeypatch.setattr(core, "registrar_propiedades_vistas", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "obtener_propiedades_vistas", lambda _phone: [])
    monkeypatch.setattr(core, "LeadProcessingService", type("S", (), {"process_lead": staticmethod(lambda *_args: None)}))
    monkeypatch.setattr(core.CrmService, "update_intent", staticmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(core.CrmService, "calculate_score", staticmethod(lambda _lead: 0))
    monkeypatch.setattr(core.CrmService, "get_lead", staticmethod(lambda phone: db["leads"].find_one({"phone": phone}) or {}))
    monkeypatch.setattr(storage, "get_pending_response", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "formatear_ficha_tecnica", lambda *_args, **_kwargs: "Ficha")
    monkeypatch.setattr(core, "generar_respuesta_estructurada", lambda *_args: {"intencion": "consulta_general", "respuesta_bot": "Claro, puedo ayudarte.", "datos_extraidos": {}})
    response = asyncio.run(core.process_user_message("+56911112222", "Solo estoy mirando"))
    assert "visita" not in response.lower()
    assert storage.get_visit_cta_state("+56911112222") == {}
