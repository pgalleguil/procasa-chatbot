"""Integration-level regression coverage for the Phase 1.1 safety contract."""

import asyncio
from datetime import datetime, timedelta

import mongomock
import pytest

from chatbot import chatbot_queue as queue
from chatbot import storage
from chatbot import grok_client
from chatbot import core
from chatbot.conversation_policy import (
    alternative_offer_accepted,
    alternative_offer_declined,
    classify_visit_data_reply,
    duplicate_response_fallback,
    extract_spontaneous_lead_signals,
    outbound_unconfirmed_visit_claim,
    outbound_phone_request,
    safe_visit_claim_free_response,
)
from chatbot.rag import extraer_filtros_estructurados


NOW = datetime(2026, 8, 1, 12, 0)


def _db():
    db = mongomock.MongoClient().phase11
    queue.ensure_queue_indexes(db)
    return db


def _seed_batch(db, provider_id="phase11-inbound"):
    queue.create_inbound_job(
        db,
        inbound_provider_message_id=provider_id,
        phone="+56911112222",
        conversation_id="conv-phase11",
        text="Quiero visitar la propiedad",
        received_at=NOW,
    )
    return db[queue.JOB_COLLECTION].find_one({"kind": queue.KIND_BATCH})


def _generated_callback(db, response, *, takeover=False):
    async def llm(_phone, _text, batch_id=None, generation_id=None, job_id=None):
        db["leads"].update_one(
            {"phone": "+56911112222"},
            {"$push": {"messages": {
                "role": "assistant", "content": response,
                "batch_id": batch_id, "generation_id": generation_id,
                "delivery_status": "generated",
            }}, "$setOnInsert": {"phone": "+56911112222"}},
            upsert=True,
        )
        if takeover:
            db["leads"].update_one(
                {"phone": "+56911112222"},
                {"$set": {"human_takeover_at": NOW + timedelta(seconds=16)}},
                upsert=True,
            )
        return response
    return llm


def test_delivery_state_machine_persists_final_text_and_sent_only_after_acceptance(monkeypatch):
    db = _db()
    _seed_batch(db)
    events = []
    monkeypatch.setattr(storage, "record_observability_event", lambda event, payload=None: events.append((event, payload)))
    sent = []

    async def sender(_phone, text):
        sent.append(text)
        return {"success": True, "provider_message_id": "wamid-phase11", "http_status": 200}

    result = asyncio.run(queue.process_one_batch(
        db, worker_id="phase11-worker", llm=_generated_callback(
            db, "Necesito tu número de teléfono para coordinar"
        ), sender=sender, now=NOW + timedelta(seconds=15),
    ))
    message = db["leads"].find_one({"phone": "+56911112222"})["messages"][-1]
    assert sent == ["Para avanzar con la coordinación, ¿Qué día o rango horario te acomoda más?"]
    assert message["delivery_status"] == "accepted"
    assert message["content"] == sent[0]
    assert message["provider_message_id"] == "wamid-phase11"
    assert result["state"] == queue.ST_RESPONDED
    assert [event for event, _ in events].count("RESPONSE_SENT") == 1


def test_takeover_suppresses_generated_response_and_excludes_it_from_history(monkeypatch):
    db = _db()
    _seed_batch(db, provider_id="phase11-takeover")
    events = []
    monkeypatch.setattr(storage, "record_observability_event", lambda event, payload=None: events.append((event, payload)))
    sent = []

    async def sender(_phone, text):
        sent.append(text)
        return {"success": True, "provider_message_id": "must-not-exist"}

    result = asyncio.run(queue.process_one_batch(
        db, worker_id="phase11-takeover-worker",
        llm=_generated_callback(db, "respuesta generada", takeover=True),
        sender=sender, now=NOW + timedelta(seconds=15),
    ))
    message = db["leads"].find_one({"phone": "+56911112222"})["messages"][-1]
    monkeypatch.setattr(storage, "get_db", lambda: db)
    assert sent == []
    assert message["delivery_status"] == "suppressed"
    assert storage.obtener_conversacion("+56911112222") == []
    assert "RESPONSE_SENT" not in [event for event, _ in events]
    assert "bot_response_suppressed_human_takeover" in [event for event, _ in events]
    assert result["last_error"] == "suppressed_human_takeover"


def test_second_takeover_cutoff_runs_before_provider(monkeypatch):
    db = _db()
    _seed_batch(db, provider_id="phase11-second-cutoff")
    events = []
    monkeypatch.setattr(storage, "record_observability_event", lambda event, payload=None: events.append(event))
    checks = iter([False, True])
    monkeypatch.setattr(queue, "_human_takeover_after_batch_start", lambda *args, **kwargs: next(checks))
    sent = []

    async def sender(_phone, text):
        sent.append(text)
        return {"success": True, "provider_message_id": "must-not-send"}

    asyncio.run(queue.process_one_batch(
        db, worker_id="phase11-second-cutoff-worker",
        llm=_generated_callback(db, "respuesta generada"), sender=sender,
        now=NOW + timedelta(seconds=15),
    ))
    assert sent == []
    assert "bot_response_suppressed_human_takeover" in events


@pytest.mark.parametrize("text", [
    "Tenemos disponibilidad mañana a las 15:00.",
    "Tenemos horarios disponibles esa mañana.",
    "Listo, quedamos para mañana.",
    "Te agendé para el sábado.",
    "Está reservado para ti.",
    "Ya quedó reservado.",
    "Podemos recibirte mañana a las 16:00.",
    "Hay disponibilidad el viernes a las 11.",
])
def test_specific_visit_claims_are_blocked(text):
    assert outbound_unconfirmed_visit_claim(text)
    assert not outbound_unconfirmed_visit_claim(safe_visit_claim_free_response(text))


@pytest.mark.parametrize("text", [
    "El ejecutivo confirmará si existe disponibilidad mañana.",
    "Registré que prefieres mañana por la tarde.",
    "Avisé al ejecutivo y él coordinará el horario.",
])
def test_future_coordination_claims_are_allowed(text):
    assert not outbound_unconfirmed_visit_claim(text)


@pytest.mark.parametrize("text", [
    "El ejecutivo confirmará el horario. Tenemos disponibilidad mañana a las 15:00.",
    "Registré tu preferencia. Te agendé para mañana.",
    "El ejecutivo te contactará. Ya quedó reservada para ti.",
])
def test_mixed_visit_claims_cannot_be_absolved_by_safe_clause(text):
    assert outbound_unconfirmed_visit_claim(text)


@pytest.mark.parametrize("text", [
    "No necesito tu teléfono. Pero pásame tu número de contacto para coordinar.",
    "No hace falta tu celular. Igual indícame tu WhatsApp.",
])
def test_mixed_phone_requests_cannot_be_absolved_by_negative_clause(text):
    assert outbound_phone_request(text)


@pytest.mark.parametrize("text", [
    "No necesito tu teléfono porque ya hablamos por WhatsApp.",
    "El ejecutivo te contactará por WhatsApp.",
    "El teléfono de la oficina está publicado en el sitio.",
])
def test_safe_phone_mentions_remain_allowed(text):
    assert not outbound_phone_request(text)


def test_alternative_offer_state_and_rag_criteria_are_distinct_and_persistent(monkeypatch):
    db = _db()
    monkeypatch.setattr(storage, "get_db", lambda: db)
    storage.update_rag_alternative_offer_state("+56911112222", {
        "status": "offered", "property_id": "P-1", "offered_at": "t", "conversation_id": "c-1",
    })
    assert alternative_offer_accepted("Sí", offer_pending=True)
    assert not alternative_offer_declined("Sí", offer_pending=True)
    storage.update_rag_alternative_offer_state("+56911112222", {"status": "accepted"})
    storage.update_rag_alternative_offer_state("+56911112222", {"status": "consumed"})
    assert storage.get_rag_alternative_offer_state("+56911112222")["status"] == "consumed"

    filters, _ = extraer_filtros_estructurados("Busco otra en Maipú hasta 4.000 UF")
    assert filters["comunas"] == ["Maipú"]
    assert filters["precio_uf_max"] == 4000
    storage.update_rag_search_state("+56911112222", {
        "criteria": {"comuna": "Maipú", "presupuesto": 4000},
    })
    assert storage.get_rag_search_state("+56911112222")["criteria"]["comuna"] == "Maipú"


def test_visit_data_decline_after_acceptance_stops_capture_and_new_cycle_is_scoped():
    assert classify_visit_data_reply("Sí", offer_pending=True) == "accepted"
    assert classify_visit_data_reply("Prefiero darle el RUT al ejecutivo", offer_pending=True) == "declined"
    assert classify_visit_data_reply("Prefiero darle el RUT al ejecutivo", offer_pending=False) == "none"

    state = {"status": "declined", "property_id": "P-1", "cycle_id": "cycle-1", "captured_fields": ["nombre"]}
    assert state["property_id"] != "P-2"
    assert state["captured_fields"] == ["nombre"]


def test_operation_scopes_analytics_without_proactive_questions():
    assert extract_spontaneous_lead_signals("Tengo crédito preaprobado", "Venta") == {"financing_status": "preapproved"}
    assert extract_spontaneous_lead_signals("Ya tengo todos los documentos", "Venta") == {}
    assert extract_spontaneous_lead_signals("Ya tengo todos los documentos", "Arriendo") == {"rental_docs_readiness": "ready"}
    assert extract_spontaneous_lead_signals("Quiero visitar la propiedad", "Venta") == {}


def test_duplicate_fallback_is_neutral_and_never_invents_visit_handoff():
    fallback = duplicate_response_fallback("¿Cuál es el precio?")
    assert "ejecutivo" not in fallback.lower()
    assert "coordinar" not in fallback.lower()


def test_bot_provider_id_resolves_as_bot_message_not_human_takeover(monkeypatch):
    db = _db()
    db["leads"].insert_one({
        "phone": "+56911112222", "conversation_id": "conv-1",
        "messages": [{"role": "assistant", "delivery_status": "accepted",
                       "provider_message_id": "wamid-bot"}],
    })
    monkeypatch.setattr(storage, "get_db", lambda: db)
    resolved = storage.find_bot_outbound_by_provider_id("wamid-bot")
    assert resolved["phone"] == "+56911112222"


def test_whatsapp_identity_uses_remote_peer_for_pn_and_refuses_unresolved_lid():
    from webhook import _remote_peer_phone

    assert _remote_peer_phone({"remoteJid": "56988887777@s.whatsapp.net"}, {}) == "+56988887777"
    assert _remote_peer_phone({"remoteJid": "123456789@lid"}, {}) is None


def test_delivery_unknown_is_not_effective_history_and_telemetry_is_honest(monkeypatch):
    db = _db()
    _seed_batch(db, provider_id="phase11-unknown")
    events = []
    monkeypatch.setattr(storage, "record_observability_event", lambda event, payload=None: events.append((event, payload)))

    async def sender(_phone, _text):
        raise TimeoutError("provider timeout")

    result = asyncio.run(queue.process_one_batch(
        db, worker_id="phase11-unknown-worker",
        llm=_generated_callback(db, "respuesta incierta"), sender=sender,
        now=NOW + timedelta(seconds=15),
    ))
    message = db["leads"].find_one({"phone": "+56911112222"})["messages"][-1]
    monkeypatch.setattr(storage, "get_db", lambda: db)
    assert result["state"] == queue.ST_DELIVERY_UNKNOWN
    assert message["delivery_status"] == "delivery_unknown"
    assert storage.obtener_conversacion("+56911112222") == []
    assert "RESPONSE_SENT" not in [event for event, _ in events]

    telemetry = []
    monkeypatch.setattr(storage, "record_observability_event", lambda event, payload=None: telemetry.append(payload))
    grok_client._record_llm_telemetry(
        model="test-model", started_at=0, usage=None,
        context={"job_id": "job-1", "batch_id": "batch-1", "lead_id": "lead-1"},
    )
    assert telemetry[0]["job_id"] == "job-1"
    assert telemetry[0]["batch_id"] == "batch-1"
    assert telemetry[0]["retries"] is None
    assert telemetry[0]["retries_observable"] is False
    assert "prompt" not in telemetry[0]
    assert "PII" not in str(telemetry[0])


def _patch_core_orchestration(monkeypatch, db, *, search_results=None, handoff=None):
    """Keep the real core orchestration while replacing external boundaries."""
    events = []
    monkeypatch.setattr(core, "get_db", lambda: db)
    monkeypatch.setattr(storage, "get_db", lambda: db)
    monkeypatch.setattr(core, "record_observability_event", lambda event, payload=None: events.append((event, payload)))
    monkeypatch.setattr(storage, "record_observability_event", lambda event, payload=None: events.append((event, payload)))
    monkeypatch.setattr(core, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(core, "es_propietario", lambda _phone: (False, None))
    monkeypatch.setattr(core, "clasificar_corredor_externo", lambda _message: {"is_external_broker": False})
    monkeypatch.setattr(core, "analizar_mensaje_para_link", lambda *args, **kwargs: (False, None, None, None))
    monkeypatch.setattr(core, "_buscar_propiedad_en_universo", lambda _db, _value, _portal=None: {
        "codigo": "P-1", "comuna": "Providencia", "operacion": "Venta",
        "tipo": "Departamento", "precio_uf": 5000,
    })
    monkeypatch.setattr(core, "registrar_propiedades_vistas", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "obtener_propiedades_vistas", lambda _phone: [])
    monkeypatch.setattr(core, "formatear_ficha_tecnica", lambda *_args, **_kwargs: "Ficha P-1")
    monkeypatch.setattr(core, "formatear_resultados_texto", lambda results: "Alternativa P-2")
    monkeypatch.setattr(core, "LeadProcessingService", type("Service", (), {"process_lead": staticmethod(lambda *_args: None)}))
    monkeypatch.setattr(core.CrmService, "update_intent", staticmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(core.CrmService, "calculate_score", staticmethod(lambda _lead: 80))
    monkeypatch.setattr(core.CrmService, "get_lead", staticmethod(lambda phone: db["leads"].find_one({"phone": phone}) or {}))
    monkeypatch.setattr(storage, "get_pending_response", lambda *_args, **_kwargs: None)
    async def default_handoff(**_kwargs):
        return {"status": "enqueued", "durable": "test"}
    monkeypatch.setattr(core, "send_alert_once", handoff or default_handoff)
    if handoff is not None:
        async def awaited_handoff(**_kwargs):
            return handoff(**_kwargs)
        monkeypatch.setattr(core, "send_alert_once", awaited_handoff)
    monkeypatch.setattr(core, "buscar_semanticamente", lambda *args, **kwargs: list(search_results or []))
    return events


def _seed_core_lead(db):
    db["leads"].insert_one({
        "phone": "+56911112222",
        "conversation_id": "conv-core-phase12",
        "prospecto": {
            "codigo": "P-1", "comuna": "Providencia", "operacion": "Venta",
        },
        "messages": [],
    })


def test_real_core_rag_flow_rejection_offer_acceptance_search_and_persistence(monkeypatch):
    db = _db()
    _seed_core_lead(db)
    search_calls = []
    events = _patch_core_orchestration(monkeypatch, db, search_results=[{"codigo": "P-2"}])
    db["leads"].update_one({"phone": "+56911112222"}, {"$set": {
        "prospecto.rag_search_state": {
            "criteria": {"operacion": "Venta", "comuna": "Providencia"},
        },
    }})
    monkeypatch.setattr(core, "buscar_semanticamente", lambda *args, **kwargs: (search_calls.append((args, kwargs)) or [{"codigo": "P-2"}]))
    def rag_llm(messages, *_args):
        context = " ".join(str(item.get("content") or "") for item in messages)
        return {
            "intencion": "consulta_general",
            "respuesta_bot": "Encontré P-2." if "Alternativa P-2" in context else "Respuesta breve",
            "datos_extraidos": {},
        }
    monkeypatch.setattr(core, "generar_respuesta_estructurada", rag_llm)

    first = asyncio.run(core.process_user_message(
        "+56911112222",
        "No me gustó, está muy cara.",
        telemetry_context={"batch_id": "rag-b1", "generation_id": "rag-g1"},
    ))
    offer = storage.get_rag_alternative_offer_state("+56911112222")
    criteria = storage.get_rag_search_state("+56911112222").get("criteria", {})
    assert search_calls == []
    assert offer["status"] == "offered"
    assert "alternativas" in first.lower()
    assert criteria == {"operacion": "Venta", "comuna": "Providencia"}

    second = asyncio.run(core.process_user_message(
        "+56911112222", "Sí",
        telemetry_context={"batch_id": "rag-b2", "generation_id": "rag-g2"},
    ))
    assert len(search_calls) == 1
    _, kwargs = search_calls[0]
    assert kwargs["criterios_estructurados"]["comuna"] == "Providencia"
    assert "P-1" in kwargs["exclude_codes"]
    assert storage.get_rag_alternative_offer_state("+56911112222")["status"] == "consumed"
    assert "P-2" in second
    assert "rag_alternative_offered" in [event for event, _ in events]


def test_real_core_visit_data_flow_accept_name_decline_rut_stops_capture(monkeypatch):
    db = _db()
    _seed_core_lead(db)
    handoffs = []

    async def durable_handoff(**kwargs):
        handoffs.append(kwargs)
        return {"status": "enqueued", "durable": "test_assignment_cycle"}

    _patch_core_orchestration(monkeypatch, db, handoff=lambda **kwargs: {"status": "enqueued"})
    monkeypatch.setattr(core, "send_alert_once", durable_handoff)
    monkeypatch.setattr(core, "generar_respuesta_estructurada", lambda *_args: {
        "intencion": "agendar_visita", "respuesta_bot": "Registré tu interés.", "datos_extraidos": {},
    })

    first = asyncio.run(core.process_user_message(
        "+56911112222", "Quiero verla.",
        telemetry_context={"batch_id": "visit-b1", "generation_id": "visit-g1"},
    ))
    assert "opcional" in first.lower()
    assert len(handoffs) == 1
    assert storage.get_visit_data_state("+56911112222")["status"] == "offered"

    asyncio.run(core.process_user_message(
        "+56911112222", "Sí",
        telemetry_context={"batch_id": "visit-b2", "generation_id": "visit-g2"},
    ))
    assert storage.get_visit_data_state("+56911112222")["status"] == "accepted"

    name_reply = asyncio.run(core.process_user_message(
        "+56911112222", "Juan Pérez",
        telemetry_context={"batch_id": "visit-b3", "generation_id": "visit-g3"},
    ))
    assert "RUT" in name_reply
    state = storage.get_visit_data_state("+56911112222")
    assert "nombre" in state["captured_fields"]

    decline_reply = asyncio.run(core.process_user_message(
        "+56911112222", "Prefiero darle el RUT al ejecutivo",
        telemetry_context={"batch_id": "visit-b4", "generation_id": "visit-g4"},
    ))
    state = storage.get_visit_data_state("+56911112222")
    assert state["status"] == "declined"
    assert "email" not in decline_reply.lower()
    assert "rut" not in decline_reply.lower()
    assert "ejecutivo" in decline_reply.lower()
    assert len(handoffs) == 1


def test_critical_handoff_is_awaited_before_core_returns(monkeypatch):
    db = _db()
    _seed_core_lead(db)
    completion = []

    async def durable_handoff(**_kwargs):
        completion.append("handoff_done")
        return {"status": "enqueued", "durable": "canonical"}

    _patch_core_orchestration(monkeypatch, db)
    monkeypatch.setattr(core, "send_alert_once", durable_handoff)
    monkeypatch.setattr(core, "generar_respuesta_estructurada", lambda *_args: {
        "intencion": "agendar_visita", "respuesta_bot": "El ejecutivo coordinará contigo.", "datos_extraidos": {},
    })
    async def run_and_check():
        await core.process_user_message(
            "+56911112222", "Quiero verla mañana",
            telemetry_context={"batch_id": "hot-b1", "generation_id": "hot-g1"},
        )
        return [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]

    pending = asyncio.run(run_and_check())
    assert completion == ["handoff_done"]
    assert pending == []


def test_early_link_response_enters_delivery_state_machine(monkeypatch):
    db = _db()
    _seed_core_lead(db)
    db["leads"].update_one({"phone": "+56911112222"}, {"$unset": {"prospecto.codigo": 1, "prospecto.comuna": 1}})
    _patch_core_orchestration(monkeypatch, db)
    monkeypatch.setattr(core, "analizar_mensaje_para_link", lambda *args, **kwargs: (True, None, "MercadoLibre", "EXT-1"))
    monkeypatch.setattr(core, "_buscar_propiedad_en_universo", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "generar_respuesta_estructurada", lambda *_args: pytest.fail("early route must return before LLM"))
    text = asyncio.run(core.process_user_message(
        "+56911112222", "https://mercadolibre.cl/inmueble/EXT-1",
        telemetry_context={"batch_id": "early-b1", "generation_id": "early-g1"},
    ))
    message = db["leads"].find_one({"phone": "+56911112222"})["messages"][-1]
    assert message["batch_id"] == "early-b1"
    assert message["generation_id"] == "early-g1"
    assert message["delivery_status"] == "generated"
    assert not any(item.get("role") == "assistant" for item in storage.obtener_conversacion("+56911112222"))

    queue._update_generated_response(
        db, phone="+56911112222", batch_id="early-b1", generation_id="early-g1",
        status="delivery_unknown", content=text,
    )
    assert not any(item.get("role") == "assistant" for item in storage.obtener_conversacion("+56911112222"))
    queue._update_generated_response(
        db, phone="+56911112222", batch_id="early-b1", generation_id="early-g1",
        status="accepted", content=text, provider_message_id="wamid-early",
    )
    assert storage.obtener_conversacion("+56911112222")[-1]["content"] == text


def test_generated_outbound_does_not_promote_last_message_until_accepted(monkeypatch):
    db = _db()
    db["leads"].insert_one({
        "phone": "+56911112222",
        "messages": [],
    })
    monkeypatch.setattr(storage, "get_db", lambda: db)
    storage.guardar_mensaje("+56911112222", "user", "Consulta inicial")
    before = db["leads"].find_one({"phone": "+56911112222"})
    storage.guardar_mensaje(
        "+56911112222", "assistant", "Respuesta generada",
        {"batch_id": "snapshot-b1", "generation_id": "snapshot-g1", "delivery_status": "generated"},
    )
    pending = db["leads"].find_one({"phone": "+56911112222"})
    assert pending["last_message_role"] == before["last_message_role"] == "user"
    assert pending["last_message_preview"] == "Consulta inicial"

    storage.update_generated_response_delivery(
        "+56911112222", db=db, batch_id="snapshot-b1", generation_id="snapshot-g1",
        status="accepted", content="Respuesta enviada", provider_message_id="wamid-snapshot",
    )
    accepted = db["leads"].find_one({"phone": "+56911112222"})
    assert accepted["last_message_role"] == "assistant"
    assert accepted["last_message_preview"] == "Respuesta enviada"
