import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import mongomock
import pytest

from chatbot import chatbot_queue as queue
from chatbot import grok_client
from chatbot.conversation_policy import (
    alternative_requested,
    build_visit_data_prompt,
    classify_visit_data_reply,
    extract_spontaneous_lead_signals,
    filter_relaxation_accepted,
    is_explicit_visit_intent,
    is_substantial_duplicate,
    outbound_phone_request,
    outbound_unconfirmed_visit_claim,
    property_rejected,
    safe_phone_free_response,
    safe_visit_claim_free_response,
    should_offer_visit_data,
    visit_data_declined_response,
    visit_data_fields_missing,
)


NOW = datetime(2026, 8, 1, 12, 0)


@pytest.mark.parametrize("text", [
    "Quiero verla mañana",
    "Quiero verlo",
    "Me gustaría visitarla",
    "¿Se puede visitar?",
    "¿Cuándo la puedo ver?",
    "¿Puedo ir mañana?",
    "¿Tienen hora para verla?",
    "Agendemos",
    "Coordinemos una visita",
])
def test_explicit_visit_language_is_operational_intent(text):
    assert is_explicit_visit_intent(text)


def test_generic_interest_does_not_offer_rut():
    assert not should_offer_visit_data("Me interesa", "agendar_visita")


def test_pending_affirmative_accepts_visit_offer():
    assert should_offer_visit_data("Sí, adelante", "consulta_general", pending_visit_confirmation=True)


@pytest.mark.parametrize("text", ["Sí", "Claro, adelante", "Puedes hacerlo"])
def test_data_offer_acceptance(text):
    assert classify_visit_data_reply(text, offer_pending=True) == "accepted"


@pytest.mark.parametrize("text", ["No quiero darlo", "Prefiero dárselos al ejecutivo", "Después"])
def test_data_offer_decline_stops_capture(text):
    assert classify_visit_data_reply(text, offer_pending=True) == "declined"
    assert "ejecutivo" in visit_data_declined_response()


def test_progressive_data_order_skips_captured_fields():
    state = {"captured_fields": ["nombre"], "status": "accepted"}
    assert visit_data_fields_missing(state, {}) == ["rut", "email"]
    assert "RUT" in build_visit_data_prompt("rut")


@pytest.mark.parametrize("text", [
    "Indícame tu teléfono",
    "Para coordinar necesito tu número",
    "Envíame el celular",
    "¿Cuál es tu WhatsApp?",
])
def test_all_phone_request_variants_are_blocked(text):
    assert outbound_phone_request(text)


@pytest.mark.parametrize("text", [
    "El ejecutivo te contactará por WhatsApp",
    "La propiedad no tiene teléfono publicado",
    "No necesito que me des tu teléfono",
])
def test_legitimate_contact_mentions_are_allowed(text):
    assert not outbound_phone_request(text)


def test_phone_sanitizer_produces_safe_follow_up():
    result = safe_phone_free_response("¿Me das tu número para coordinar?")
    assert "número" not in result.lower()
    assert "día" in result.lower()


@pytest.mark.parametrize("text", [
    "Tu visita quedó agendada para mañana",
    "La visita está confirmada",
    "Te esperamos mañana a las 15:00",
    "Horario confirmado",
])
def test_unconfirmed_visit_claims_are_blocked(text):
    assert outbound_unconfirmed_visit_claim(text)
    safe = safe_visit_claim_free_response(text)
    assert "confirmará" in safe or "coordinará" in safe


def test_safe_future_coordination_claim_is_allowed():
    assert not outbound_unconfirmed_visit_claim("El ejecutivo coordinará el horario contigo.")


def test_duplicate_guard_is_exact_and_recent():
    assert is_substantial_duplicate("¿Cuál es tu RUT?", ["¿Cuál es tu RUT?"])
    assert not is_substantial_duplicate("¿Cuál es tu correo?", ["¿Cuál es tu RUT?"])


@pytest.mark.parametrize("text", [
    "¿Tienen algo parecido?",
    "Muéstrame otras",
    "Busco otra propiedad",
    "Quiero otra comuna",
])
def test_alternative_requests_are_detected(text):
    assert alternative_requested(text)


@pytest.mark.parametrize("text", ["No me gustó", "Está muy caro", "No me sirve", "Esa comuna no me acomoda"])
def test_property_rejections_are_detected(text):
    assert property_rejected(text)


def test_rejection_without_alternative_does_not_imply_search():
    assert property_rejected("No me gustó")
    assert not alternative_requested("No me gustó")


def test_relaxation_requires_explicit_acceptance():
    assert not filter_relaxation_accepted("No hay problema", offer_pending=True)
    assert filter_relaxation_accepted("Sí, amplía un poco", offer_pending=True)
    assert not filter_relaxation_accepted("Sí", offer_pending=False)


@pytest.mark.parametrize(("text", "expected"), [
    ("Recién empecé a buscar", {"search_duration_bucket": "just_started"}),
    ("Llevo cuatro meses buscando", {"search_duration_bucket": "3_6_months"}),
    ("Tengo crédito preaprobado", {"financing_status": "preapproved"}),
    ("Estoy evaluando el crédito", {"financing_status": "under_evaluation"}),
    ("Voy a comprar al contado", {"financing_status": "cash"}),
    ("Ya tengo todos los documentos", {"rental_docs_readiness": "ready"}),
])
def test_spontaneous_analytics_signals(text, expected):
    assert extract_spontaneous_lead_signals(text) == expected


def test_no_spontaneous_signal_means_no_proactive_question():
    assert extract_spontaneous_lead_signals("Quiero conocer la propiedad") == {}


def test_pending_batch_is_cancelled_when_human_takes_over():
    database = mongomock.MongoClient().phase1
    queue.ensure_queue_indexes(database)
    queue.create_inbound_job(
        database, inbound_provider_message_id="phase1-human-1",
        phone="+56911112222", conversation_id="conv-1", text="hola", received_at=NOW,
    )
    cancelled = queue.cancel_pending_batches_for_human(database, phone="+56911112222")
    assert cancelled == 1
    batch = database[queue.JOB_COLLECTION].find_one({"kind": queue.KIND_BATCH})
    assert batch["state"] == queue.ST_FAILED_TERMINAL
    assert batch["last_error"] == "human_takeover_before_delivery"


def test_processing_batch_is_suppressed_after_human_message():
    database = mongomock.MongoClient().phase1
    queue.ensure_queue_indexes(database)
    queue.create_inbound_job(
        database, inbound_provider_message_id="phase1-human-2",
        phone="+56911112222", conversation_id="conv-2", text="hola", received_at=NOW,
    )
    sent = []

    async def llm(_phone, _text):
        database["leads"].update_one(
            {"phone": "+56911112222"},
            {"$set": {"human_takeover_at": NOW + timedelta(seconds=16)}},
            upsert=True,
        )
        return "respuesta automática"

    async def sender(_phone, text):
        sent.append(text)
        return {"success": True, "provider_message_id": "must-not-send", "http_status": 200}

    result = asyncio.run(queue.process_one_batch(
        database, worker_id="human-worker", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=15),
    ))
    assert sent == []
    assert result["last_error"] == "suppressed_human_takeover"
    assert result["delivery_attempts"][-1]["status"] == "bot_response_suppressed_human_takeover"


def test_duplicate_response_is_replaced_before_delivery():
    database = mongomock.MongoClient().phase1
    queue.ensure_queue_indexes(database)
    database["leads"].insert_one({
        "phone": "+56911112222",
        "messages": [
            {"role": "assistant", "content": "Tomé nota de tu solicitud."},
            {"role": "assistant", "content": "Tomé nota de tu solicitud."},
        ],
    })
    queue.create_inbound_job(
        database, inbound_provider_message_id="phase1-duplicate",
        phone="+56911112222", conversation_id="conv-3", text="hola", received_at=NOW,
    )
    sent = []

    async def llm(_phone, _text):
        return "Tomé nota de tu solicitud."

    async def sender(_phone, text):
        sent.append(text)
        return {"success": True, "provider_message_id": "duplicate-safe", "http_status": 200}

    asyncio.run(queue.process_one_batch(
        database, worker_id="duplicate-worker", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=15),
    ))
    assert sent == ["Tomé nota de tu solicitud. El ejecutivo podrá coordinarla directamente contigo."]


def test_llm_usage_telemetry_contains_no_prompt_or_pii(monkeypatch):
    events = []
    monkeypatch.setattr(grok_client, "_record_llm_telemetry", lambda **payload: events.append(payload))
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=4, total_tokens=14)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=usage,
    )
    monkeypatch.setattr(grok_client.client.chat.completions, "create", lambda **_kwargs: response)
    assert grok_client.generar_respuesta(
        [{"role": "user", "content": "Mi RUT es privado"}],
        telemetry_context={"trace_id": "trace-safe", "lead_id": "lead-safe"},
    ) == "ok"
    assert events[0]["usage"].prompt_tokens == 10
    assert "messages" not in events[0]
    assert "RUT" not in str(events[0])
