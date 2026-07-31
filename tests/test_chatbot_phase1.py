import asyncio
import threading
from datetime import datetime, timedelta, timezone

import mongomock

from chatbot import chatbot_queue as queue
from chatbot.classifier import clasificar_corredor_externo
from chatbot.conversation_policy import nudge_eligibility, outbound_phone_request


NOW = datetime(2026, 8, 1, 12, 0)


def db():
    database = mongomock.MongoClient().phase1
    queue.ensure_queue_indexes(database)
    return database


def add(database, provider_id, text, at=NOW):
    return queue.create_inbound_job(
        database, inbound_provider_message_id=provider_id, phone="+56911112222",
        conversation_id="conversation-1", text=text, received_at=at,
    )


def test_sliding_window_and_max_wait():
    database = db()
    add(database, "in-1", "uno")
    add(database, "in-2", "dos", NOW + timedelta(seconds=10))
    batch = database.chatbot_inbound_jobs.find_one({"kind": queue.KIND_BATCH})
    assert batch["window_end_at"] == NOW + timedelta(seconds=25)
    add(database, "in-3", "tres", NOW + timedelta(seconds=59))
    batch = database.chatbot_inbound_jobs.find_one({"kind": queue.KIND_BATCH})
    assert batch["window_end_at"] == NOW + timedelta(seconds=60)


def test_concurrent_producers_create_one_active_batch():
    database = db()
    barrier = threading.Barrier(2)

    def produce(number):
        barrier.wait()
        add(database, f"concurrent-{number}", f"message {number}")

    threads = [threading.Thread(target=produce, args=(number,)) for number in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    batches = list(database.chatbot_inbound_jobs.find({"kind": queue.KIND_BATCH}))
    assert len(batches) == 1
    assert len(batches[0]["job_ids"]) == 2


def test_message_arriving_during_llm_regenerates_before_send():
    database = db()
    add(database, "in-1", "primero")
    generated = []
    sent = []

    async def llm(_phone, text):
        generated.append(text)
        if len(generated) == 1:
            add(database, "in-2", "segundo", NOW + timedelta(seconds=16))
        return f"respuesta: {text}"

    async def sender(_phone, text):
        sent.append(text)
        return {"success": True, "provider_message_id": "out-1", "http_status": 200}

    result = asyncio.run(queue.process_one_batch(
        database, worker_id="worker-1", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=15),
    ))
    assert generated == ["primero", "primero\nsegundo"]
    assert sent == ["respuesta: primero\nsegundo"]
    assert [x["provider_id"] for x in result["snapshot"]] == ["in-1", "in-2"]


def test_explicit_phone_request_is_blocked_but_contact_mention_is_not():
    blocked = ["Indícame tu teléfono.", "Necesito tu número celular.", "¿Cuál es tu WhatsApp?", "Déjame un número de contacto."]
    allowed = ["La ejecutiva te contactará.", "Seguiremos conversando por WhatsApp.", "El teléfono de la propiedad no está publicado.", "Recibimos tus datos de contacto.", "No necesito que me des tu teléfono."]
    assert all(outbound_phone_request(text) for text in blocked)
    assert not any(outbound_phone_request(text) for text in allowed)


def test_phone_request_is_replaced_before_provider_delivery():
    database = db()
    add(database, "in-1", "quiero visitar")
    sent = []

    async def llm(_phone, _text):
        return "¿Me puedes compartir tu número de teléfono para coordinar?"

    async def sender(_phone, text):
        sent.append(text)
        return {"success": True, "provider_message_id": "out-safe", "http_status": 200}

    result = asyncio.run(queue.process_one_batch(
        database, worker_id="worker-safe", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=15),
    ))
    assert sent == ["Para avanzar con la coordinación, ¿Qué día o rango horario te acomoda más?"]
    assert result["delivery_attempts"][0]["status"] == "blocked_phone_request"


def test_broker_rules_cover_positive_and_negative_cases():
    positives = ["Soy corredor", "Soy corredora", "Soy colega", "Trabajo en RE/MAX", "¿Hacen canje?", "¿Comparten comisión?"]
    negatives = ["No soy corredor", "Estoy comprando mediante una corredora", "La corredora no me respondió"]
    assert all(clasificar_corredor_externo(text)["is_external_broker"] for text in positives)
    assert not any(clasificar_corredor_externo(text)["is_external_broker"] for text in negatives)


def test_nudges_are_blocked_for_broker_handoff_and_visit():
    assert not nudge_eligibility({"conversation_status": "BLOCKED_EXTERNAL_BROKER"})["eligible"]
    assert nudge_eligibility({"last_intent": "ASK_CONTACT", "ejecutivo_asignado": "Erika"})["eligible"]
    assert nudge_eligibility({"pending_response": {"type": "VISIT_CONFIRMATION", "status": "waiting"}})["eligible"]
    assert not nudge_eligibility({"human_takeover_at": "2026-08-01T12:00:00Z"})["eligible"]
    assert nudge_eligibility({"stage": "OPEN"})["eligible"]


def test_regeneration_limit_closes_turn_without_sending_or_retry_loop(monkeypatch):
    database = db()
    add(database, "in-1", "primero")
    generated = []
    sent = []
    monkeypatch.setenv("CHATBOT_BATCH_MAX_REGENERATIONS", "2")

    async def llm(_phone, _text):
        generated.append("called")
        add(database, f"in-{len(generated) + 1}", f"nuevo {len(generated)}", NOW + timedelta(seconds=16))
        return "respuesta"

    async def sender(_phone, text):
        sent.append(text)
        return {"success": True, "provider_message_id": "unexpected", "http_status": 200}

    result = asyncio.run(queue.process_one_batch(
        database, worker_id="worker-limit", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=15),
    ))

    assert len(generated) == 3  # initial generation + exactly two regenerations
    assert sent == []
    assert result["state"] == queue.ST_FAILED_TERMINAL
    assert result["last_error"] == "stale_regeneration_limit"
    assert "active_conversation_key" not in result
    assert result["delivery_attempts"][-1]["status"] == "stale_regeneration_limit"
