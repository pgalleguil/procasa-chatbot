import asyncio
import threading
from datetime import datetime, timedelta, timezone

import mongomock

from chatbot import chatbot_queue as queue
from chatbot.classifier import clasificar_corredor_externo
from chatbot.conversation_policy import (
    build_property_identifier_clarification,
    build_pending_visit_preference_acknowledgement,
    build_property_identifier_request,
    contains_property_identifier,
    extract_visit_preference,
    has_near_term_visit_urgency,
    is_explicit_visit_intent,
    nudge_eligibility,
    outbound_phone_request,
    outbound_unconfirmed_visit_claim,
    property_identifier_action,
    is_acknowledgement_only,
)
from chatbot.property_lookup import lookup_property_link, validate_property_link_semantics


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
    assert batch["window_end_at"] == NOW + timedelta(seconds=15)
    add(database, "in-3", "tres", NOW + timedelta(seconds=59))
    batch = database.chatbot_inbound_jobs.find_one({"kind": queue.KIND_BATCH})
    # Conversational accumulation is bounded so a burst cannot remain stale
    # for a minute before its first coherent turn is generated.
    assert batch["window_end_at"] == NOW + timedelta(seconds=20)


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


def test_message_arriving_during_llm_supersedes_generation_and_requeues_batch():
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
    assert generated == ["primero"]
    assert sent == []
    assert result["state"] == queue.ST_BATCHING
    assert result["delivery_attempts"][-1]["status"] == "response_superseded_by_new_inbound"

    result = asyncio.run(queue.process_one_batch(
        database, worker_id="worker-1", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=20),
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


def test_final_freshness_barrier_prevents_provider_call(monkeypatch):
    database = db()
    add(database, "in-1", "primero")
    generated = []
    sent = []
    async def llm(_phone, _text):
        generated.append("called")
        return "respuesta"

    async def sender(_phone, text):
        sent.append(text)
        return {"success": True, "provider_message_id": "unexpected", "http_status": 200}

    original_barrier = queue._batch_snapshot_is_stale

    def stale_after_generation(*args, **kwargs):
        add(database, "in-2", "segundo", NOW + timedelta(seconds=16))
        return original_barrier(*args, **kwargs)

    monkeypatch.setattr(queue, "_batch_snapshot_is_stale", stale_after_generation)
    result = asyncio.run(queue.process_one_batch(
        database, worker_id="worker-limit", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=15),
    ))

    assert len(generated) == 1
    assert sent == []
    assert result["state"] == queue.ST_BATCHING
    assert result["last_error"] == "response_superseded_by_new_inbound"
    assert "active_conversation_key" in result
    assert result["delivery_attempts"][-1]["status"] == "response_superseded_by_new_inbound"


def test_visit_without_property_asks_identifier_first():
    assert is_explicit_visit_intent("Quiero visitar la propiedad mañana")
    assert property_identifier_action(
        visit_requested=True,
        property_resolved=False,
        awaiting_identifier=False,
        identifier_in_message=False,
    ) == "ask"
    assert build_property_identifier_request().startswith(
        "Para poder coordinarte la visita lo antes posible"
    )


def test_manually_sent_identifier_request_is_not_duplicated():
    action = property_identifier_action(
        visit_requested=False,
        property_resolved=False,
        awaiting_identifier=True,
        identifier_in_message=False,
    )
    assert action == "clarify"
    assert build_property_identifier_clarification() != build_property_identifier_request()


def test_pending_visit_timing_is_acknowledged_without_repeating_identifier_request():
    assert property_identifier_action(
        visit_requested=True,
        property_resolved=False,
        awaiting_identifier=True,
        identifier_in_message=False,
    ) == "acknowledge"
    response = build_pending_visit_preference_acknowledgement()
    assert "dejo registrada" in response.lower()
    assert response != build_property_identifier_request()


def test_ack_only_is_deterministic_and_does_not_block_new_information():
    for text in ("👍", "👌", "ok", "muchas gracias, quedo atento", "perfecto gracias"):
        assert is_acknowledgement_only(text)
    for text in (
        "gracias, ¿cuánto sale el gasto común?",
        "ok, puedo visitar mañana",
        "perfecto, mi correo es cliente@example.com",
    ):
        assert not is_acknowledgement_only(text)


def test_next_link_message_resolves_property():
    assert contains_property_identifier("https://www.procasa.cl/propiedad/ABC-123")
    assert property_identifier_action(
        visit_requested=False,
        property_resolved=True,
        awaiting_identifier=True,
        identifier_in_message=True,
    ) == "resolved"


def test_visit_tomorrow_marks_high_urgency():
    assert has_near_term_visit_urgency("¿Se puede visitar mañana en la tarde?")
    assert has_near_term_visit_urgency("¿Puedo ir el jueves?")


def test_visit_with_resolved_property_does_not_ask_link():
    assert property_identifier_action(
        visit_requested=True,
        property_resolved=True,
        awaiting_identifier=False,
        identifier_in_message=False,
    ) == "resolved"


def test_existing_visit_date_is_preserved():
    preference = extract_visit_preference(
        "Quiero visitar mañana a las 18:30",
        visit_context=True,
    )
    assert preference == "manana a las 18:30"


def test_visit_date_range_is_preserved():
    preference = extract_visit_preference(
        "Puedo el jueves en la mañana o desde el 23 en adelante a cualquier horario",
        visit_context=True,
    )
    assert preference == "jueves en la manana desde el 23 en adelante"


def test_no_false_availability_confirmation():
    assert outbound_unconfirmed_visit_claim(
        "La visita está confirmada para mañana."
    )
    assert not outbound_unconfirmed_visit_claim(
        "El ejecutivo confirmará si existe disponibilidad."
    )


def test_YAPO_EXPLICIT_LINK_EXTERNAL_ID_WINS_OVER_CONTEXT():
    database = mongomock.MongoClient().properties
    database.universo_cartera_prop360.insert_one({
        "codigo": "7733",
        "tipo_operacion": {"tipo": "Local Comercial", "arriendo": True},
        "ubicacion": {"comuna": "Ñuñoa"},
        "publicaciones": {"yapo": {"publicaciones": {
            "A": {"code": "32979177", "url": "https://www.yapo.cl/bienes-raices-alquiler-comercios/local-comercial-en-arriendo-en-nunoa/32979177"}
        }}},
    })
    prop, meta = lookup_property_link(
        database,
        "https://www.yapo.cl/bienes-raices-alquiler-comercios/local-comercial-en-arriendo-en-nunoa/32979177",
        "universo_cartera_prop360",
    )
    assert prop["codigo"] == "7733"
    assert meta["match_method"] == "nested_publication_external_id"


def test_NUNOA_RENT_LINK_CANNOT_RESOLVE_TO_VINA_SALE():
    valid, reason = validate_property_link_semantics(
        {
            "codigo": "6186",
            "tipo_operacion": {"tipo": "Departamento", "venta": True},
            "ubicacion": {"comuna": "Viña del Mar"},
        },
        "https://www.yapo.cl/bienes-raices-alquiler-comercios/local-comercial-en-arriendo-en-nunoa/32979177",
    )
    assert valid is False
    assert reason in {"operation_mismatch", "commune_mismatch", "property_type_mismatch"}


def test_EXPLICIT_LINK_LOOKUP_FAILS_SAFE():
    database = mongomock.MongoClient().properties
    database.universo_cartera_prop360.insert_one({"codigo": "6186", "ubicacion": {"comuna": "Viña del Mar"}})
    prop, meta = lookup_property_link(
        database,
        "https://www.yapo.cl/bienes-raices-alquiler-comercios/local-comercial-en-arriendo-en-nunoa/99999999",
        "universo_cartera_prop360",
    )
    assert prop is None
    assert meta["external_id"] == "99999999"
    assert meta["match_method"] is None
