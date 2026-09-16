"""Offline E2E contract for conversational batching and semantic fallbacks."""

import asyncio
from datetime import datetime, timedelta

import mongomock

from chatbot import chatbot_queue as queue
from chatbot import core
from chatbot import storage
from chatbot import whatsapp_client
from chatbot.core import is_chatbot_control_lead
from chatbot.conversation_policy import (
    build_local_fallback_response,
    classify_local_fallback_intent,
    is_acknowledgement_only,
)
from chatbot.deepseek_shadow import (
    build_shadow_responses_request,
    evaluate_shadow_outputs,
)


BASE = datetime(2026, 9, 15, 0, 54, 0)


def _db():
    db = mongomock.MongoClient().conversational_resilience
    queue.ensure_queue_indexes(db)
    return db


def _inbound(db, provider_id, text, at):
    return queue.create_inbound_job(
        db,
        inbound_provider_message_id=provider_id,
        conversation_id="control-conversation",
        phone="+56983219804",
        text=text,
        received_at=at,
    )


def test_NEW_INBOUND_500MS_BEFORE_SEND_SUPPRESSES_OLD_RESPONSE(monkeypatch):
    db = _db()
    _inbound(db, "a", "¿Se puede visitar?", BASE)
    sent = []

    async def llm(_phone, text, **_kwargs):
        return f"respuesta para {text}"

    async def sender(_phone, text, **_kwargs):
        sent.append(text)
        return {"success": True, "provider_message_id": "must-not-send"}

    original = queue._batch_snapshot_is_stale
    injected = {"done": False}

    def inject_before_final_barrier(*args, **kwargs):
        if not injected["done"]:
            injected["done"] = True
            _inbound(db, "b", "Se puede ver mañana temprano?", BASE + timedelta(seconds=4.5))
        return original(*args, **kwargs)

    monkeypatch.setattr(queue, "_batch_snapshot_is_stale", inject_before_final_barrier)
    first = asyncio.run(queue.process_one_batch(
        db, worker_id="w", llm=llm, sender=sender, now=BASE + timedelta(seconds=5),
    ))

    assert first["state"] == queue.ST_BATCHING
    assert sent == []
    assert first["delivery_attempts"][-1]["status"] == "response_superseded_by_new_inbound"
    assert "active_conversation_key" in first

    second = asyncio.run(queue.process_one_batch(
        db, worker_id="w", llm=llm, sender=sender, now=BASE + timedelta(seconds=9.5),
    ))
    assert second["state"] == queue.ST_RESPONDED
    assert sent == ["respuesta para ¿Se puede visitar?\nSe puede ver mañana temprano?"]


def test_NEW_INBOUND_DURING_LLM_REQUEUES_BATCH():
    db = _db()
    _inbound(db, "a", "precio", BASE)
    generated = []
    sent = []

    async def llm(_phone, text, **_kwargs):
        generated.append(text)
        _inbound(db, "b", "y se puede visitar mañana?", BASE + timedelta(seconds=4))
        return "respuesta obsoleta"

    async def sender(_phone, text, **_kwargs):
        sent.append(text)
        return {"success": True, "provider_message_id": "unexpected"}

    result = asyncio.run(queue.process_one_batch(
        db, worker_id="w", llm=llm, sender=sender, now=BASE + timedelta(seconds=5),
    ))
    assert generated == ["precio"]
    assert sent == []
    assert result["state"] == queue.ST_BATCHING
    assert result["last_error"] == "response_superseded_by_new_inbound"


def test_MULTI_MESSAGE_BURST_PRODUCES_ONE_OUTBOUND():
    db = _db()
    _inbound(db, "a", "Hola", BASE)
    _inbound(db, "b", "Busco una propiedad", BASE + timedelta(seconds=2))
    _inbound(db, "c", "¿Se puede visitar mañana?", BASE + timedelta(seconds=4))
    generated = []
    sent = []

    async def llm(_phone, text, **_kwargs):
        generated.append(text)
        return "una respuesta natural para el turno completo"

    async def sender(_phone, text, **_kwargs):
        sent.append(text)
        return {"success": True, "provider_message_id": "out-1"}

    result = asyncio.run(queue.process_one_batch(
        db, worker_id="w", llm=llm, sender=sender, now=BASE + timedelta(seconds=9),
    ))
    assert result["state"] == queue.ST_RESPONDED
    assert generated == ["Hola\nBusco una propiedad\n¿Se puede visitar mañana?"]
    assert len(sent) == 1


def test_STALE_GENERATION_NOT_VISIBLE_TO_NEXT_LLM(monkeypatch):
    db = _db()
    db["leads"].insert_one({
        "phone": "+56983219804",
        "messages": [
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "vieja", "delivery_status": "superseded"},
            {"role": "assistant", "content": "pendiente", "delivery_status": "provider_attempt"},
            {"role": "assistant", "content": "desconocida", "delivery_status": "delivery_unknown"},
            {"role": "assistant", "content": "válida", "delivery_status": "accepted"},
        ],
    })
    monkeypatch.setattr(storage, "get_db", lambda: db)
    visible = storage.obtener_conversacion("+56983219804")
    assert [item["content"] for item in visible] == ["A", "válida"]


def test_PRICE_NEGOTIATION_NOT_GENERIC_PRICE_FALLBACK():
    response, intent = build_local_fallback_response(
        "¿Y el precio se puede bajar un poco?",
        property_facts={"precio_uf": 2500, "operacion": "Arriendo"},
    )
    assert intent == "PRICE_NEGOTIATION"
    assert "2.500 UF" in response
    assert "oferta" in response.lower()
    assert "garantizar" in response.lower()


def test_PRICE_INCLUSIONS_NOT_GENERIC_PRICE_FALLBACK():
    response, intent = build_local_fallback_response("¿El valor incluye los servicios básicos?")
    assert intent == "PRICE_INCLUSIONS"
    assert "no aparece confirmado" in response.lower()
    assert "valor actualizado" not in response.lower()


def test_KNOWN_PROPERTY_FACT_SURVIVES_LLM_EMPTY():
    response, intent = build_local_fallback_response(
        "¿Cuál es la orientación?",
        property_facts={"caracteristicas": {"orientacion": "Norte"}},
    )
    assert intent == "PROPERTY_ATTRIBUTE"
    assert "Norte" in response


def test_KNOWN_PROPERTY_FACT_SURVIVES_CORE_LLM_FAILURE(monkeypatch):
    from tests.test_chatbot_phase1_1_integration import _patch_core_orchestration

    db = _db()
    db["leads"].insert_one({
        "phone": "+56983219804",
        "conversation_id": "semantic-conversation",
        "prospecto": {"codigo": "P-1", "operacion": "Arriendo"},
        "messages": [],
    })
    _patch_core_orchestration(monkeypatch, db)
    monkeypatch.setattr(core, "_buscar_propiedad_en_universo", lambda *_args, **_kwargs: {
        "codigo": "P-1",
        "tipo_operacion": {"tipo": "Departamento", "arriendo": True},
        "caracteristicas": {"orientacion": "Norte"},
        "ubicacion": {"comuna": "Ñuñoa"},
    })
    llm_calls = []

    def empty_llm(*_args, **_kwargs):
        llm_calls.append(True)
        raise AssertionError("known property fact should not need LLM")

    monkeypatch.setattr(core, "generar_respuesta_estructurada", empty_llm)
    response = asyncio.run(core.process_user_message(
        "+56983219804", "¿Cuál es la orientación?",
    ))
    assert "Norte" in response
    assert llm_calls == []


def test_VISIT_AND_PRICE_IN_SAME_BATCH_ANSWERED_TOGETHER():
    db = _db()
    _inbound(db, "a", "¿Se puede bajar un poco?", BASE)
    _inbound(db, "b", "Se puede ver mañana temprano?", BASE + timedelta(seconds=1))
    calls = []
    sent = []

    async def llm(_phone, text, **_kwargs):
        calls.append(text)
        return "respuesta conjunta de precio y visita"

    async def sender(_phone, text, **_kwargs):
        sent.append(text)
        return {"success": True, "provider_message_id": "out-stable"}

    result = asyncio.run(queue.process_one_batch(
        db, worker_id="w", llm=llm, sender=sender, now=BASE + timedelta(seconds=7),
    ))
    assert result["state"] == queue.ST_RESPONDED
    assert len(calls) == len(sent) == 1
    assert "bajar" in calls[0] and "ver" in calls[0]


def test_GLOBAL_PROVIDER_RATE_LIMIT_SHARED_ACROSS_TRAFFIC_CLASSES(monkeypatch):
    monkeypatch.setattr(whatsapp_client, "_NEXT_SEND_AT", 0.0)
    monkeypatch.setattr(whatsapp_client, "_LAST_SEND_AT", 0.0)
    monkeypatch.setattr(whatsapp_client.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(whatsapp_client.random, "uniform", lambda _low, _high: 0.0)
    assert whatsapp_client._reserve_provider_slot("customer_reply") == 0.0
    bulk_wait = whatsapp_client._reserve_provider_slot("bulk_automation")
    assert bulk_wait >= 5.2
    assert whatsapp_client._reserve_provider_slot("customer_reply") >= 12.0


def test_NO_429_IN_SIMULATED_PROTECTED_MODE(monkeypatch):
    monkeypatch.setattr(whatsapp_client, "_NEXT_SEND_AT", 0.0)
    monkeypatch.setattr(whatsapp_client.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(whatsapp_client.random, "uniform", lambda _low, _high: 0.0)
    waits = [whatsapp_client._reserve_provider_slot("customer_reply") for _ in range(3)]
    statuses = ["accepted" if wait >= 0 else "429" for wait in waits]
    assert statuses == ["accepted", "accepted", "accepted"]
    assert waits[1] >= 5.2 and waits[2] >= 5.2


def test_ONE_STABLE_TURN_ONE_OUTBOUND():
    db = _db()
    _inbound(db, "a", "precio", BASE)
    _inbound(db, "b", "visita mañana", BASE + timedelta(seconds=2))
    sent = []

    async def llm(_phone, _text, **_kwargs):
        return "una respuesta estable"

    async def sender(_phone, text, **_kwargs):
        sent.append(text)
        return {"success": True, "provider_message_id": "one-outbound"}

    asyncio.run(queue.process_one_batch(
        db, worker_id="w", llm=llm, sender=sender, now=BASE + timedelta(seconds=7),
    ))
    assert len(sent) == 1


def test_ACK_ONLY_NO_REPLY_has_zero_llm_and_zero_outbound():
    assert is_acknowledgement_only("Muchas gracias, quedo atento")
    db = _db()
    _inbound(db, "ack", "Muchas gracias, quedo atento", BASE)
    calls = []
    sent = []

    async def llm(*_args, **_kwargs):
        calls.append(True)
        return "no debe ejecutarse"

    async def sender(*_args, **_kwargs):
        sent.append(True)
        return {"success": True, "provider_message_id": "nope"}

    asyncio.run(queue.process_one_batch(
        db, worker_id="w", llm=llm, sender=sender, now=BASE + timedelta(seconds=5),
    ))
    assert calls == []
    assert sent == []


def test_EXPLICIT_CONTROL_LEAD_suppresses_side_effects_without_phone_hardcode():
    assert is_chatbot_control_lead({"chatbot_test_mode": True})
    assert is_chatbot_control_lead({"chatbot_control_lead": True})
    assert not is_chatbot_control_lead({"phone": "+56983219804"})


def test_REAL_DIALOGUE_BATCH_RECONSTRUCTION_has_one_generation_and_one_outbound():
    db = _db()
    dialogue = [
        "Hola",
        "Como están?",
        "Busco una propiedad",
        "Hola, tengo preguntas sobre Mercado Libre URL MLC-3776482990",
        "Esta",
        "Esta disponible?",
        "Cual es la orientación",
        "Y el precio se puede bajar un poco?",
        "Se puede ver mañana temprano?",
        "Cuando me contactarán?",
        "Necesito con urgencia",
        "Responde lo que te digo",
        "Rápido",
        "El valor incluye los servicios básicos?",
    ]
    inbound_timeline = []
    for index, text in enumerate(dialogue):
        at = BASE + timedelta(seconds=min(index * 1.4, 19.0))
        inbound_timeline.append(at)
        _inbound(db, f"dialogue-{index}", text, at)

    snapshots = []
    outbound_timeline = []

    async def llm(_phone, text, **_kwargs):
        snapshots.append(text)
        return "respuesta única que cubre precio, visita y urgencia"

    async def sender(_phone, text, **_kwargs):
        outbound_timeline.append(text)
        return {"success": True, "provider_message_id": "dialogue-out"}

    result = asyncio.run(queue.process_one_batch(
        db, worker_id="dialogue-worker", llm=llm, sender=sender,
        now=BASE + timedelta(seconds=20),
    ))
    assert len(inbound_timeline) == 14
    assert len(result["snapshot"]) == 14
    assert len(snapshots) == 1
    assert "precio se puede bajar" in snapshots[0]
    assert "ver mañana" in snapshots[0]
    assert len(outbound_timeline) == 1
    assert result["state"] == queue.ST_RESPONDED
    assert not any(
        attempt["status"] == "response_superseded_by_new_inbound"
        for attempt in result["delivery_attempts"]
    )


def test_SHADOW_RESPONSES_JSON_SCHEMA_is_offline_and_measurable():
    request = build_shadow_responses_request(
        [{"role": "user", "content": "¿Se puede visitar?"}],
        model="deepseek-v4-flash",
    )
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["schema"]["additionalProperties"] is False
    metrics = evaluate_shadow_outputs([
        '{"intencion":"consulta_general","respuesta_bot":"ok","datos_extraidos":{}}',
        "   ",
        "not-json",
    ])
    assert metrics.total == 3
    assert metrics.valid_json == 1
    assert metrics.empty_responses == 1
    assert metrics.parse_errors == 1
