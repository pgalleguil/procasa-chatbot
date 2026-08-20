"""Integration-level regression coverage for the Phase 1.1 safety contract."""

import asyncio
from datetime import datetime, timedelta

import mongomock
import pytest

from chatbot import chatbot_queue as queue
from chatbot import storage
from chatbot import grok_client
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
