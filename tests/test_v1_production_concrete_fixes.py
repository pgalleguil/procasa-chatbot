from pathlib import Path

import mongomock

from chatbot import chatbot_queue as queue
from chatbot.customer_response_semantics import (
    new_link_context_response as _new_link_context_response,
    new_link_template_contaminated as _new_link_template_contaminated,
    rag_customer_response as _rag_customer_response,
    evaluate_customer_response,
    build_specific_property_response,
)
from chatbot.constants import LeadIntent
from chatbot.classifier import clasificar_corredor_externo
from chatbot.conversation_policy import (
    alternative_offer_accepted,
    classify_visit_data_reply,
    enforce_whatsapp_response_length,
    extract_visit_preference,
    extract_spontaneous_lead_signals,
    is_actionable_customer_message,
    is_explicit_property_search_request,
    is_explicit_visit_intent,
    is_visit_confirmation,
    property_rejected,
)
from chatbot.crm_service import CrmService
from chatbot.lead_temperature import derive_effective_temperature
from chatbot.property_lookup import (
    canonical_property_context,
    guard_resolved_property_response,
    lookup_property_link,
    property_availability_state,
)
from chatbot.utils import extraer_nombre_explicito
from chatbot.prompts import PROMPT_PROPIEDAD_NO_ENCONTRADA
from chatbot.webhook_event_policy import is_outbound_message_event


YAPO_URL = (
    "https://www.yapo.cl/bienes-raices-venta-de-propiedades-apartamentos/"
    "departamento-en-venta-de-1-dorm-en-nunoa/32900652"
)


def test_yapo_identity_is_stable_and_uses_resolved_document_context():
    db = mongomock.MongoClient().properties
    db.universo_cartera_prop360.insert_one({
        "codigo": "17176",
        # The top-level field models the stale compatibility context seen in
        # the production trace; the nested Prop360 location is canonical.
        "comuna": "Providencia",
        "ubicacion": {"comuna": "Ñuñoa", "region": "Metropolitana"},
        "tipo_operacion": {"tipo": "Departamento", "venta": True,
                            "precio_venta": {"precio_uf": 2753}},
        "publicaciones": {"yapo": {"url_yapo": YAPO_URL}},
    })

    prop, meta = lookup_property_link(db, YAPO_URL)

    assert prop["codigo"] == "17176"
    assert meta["external_id"] == "32900652"
    assert meta["match_method"] in {"legacy_exact_url", "legacy_url_external_id"}
    context = canonical_property_context(prop, operation_override=meta["operation"])
    assert context["codigo"] == "17176"
    assert context["comuna"] == "Ñuñoa"
    assert context["operacion"] == "venta"
    assert property_availability_state(prop) == "PROPERTY_FOUND_AVAILABILITY_UNKNOWN"


def test_resolved_property_cannot_be_described_as_outside_portfolio():
    prop = {"codigo": "17176", "ubicacion": {"comuna": "Ñuñoa"}}
    response = "Ese aviso no es una propiedad de nuestro portafolio actual."
    guarded = guard_resolved_property_response(response, prop)
    assert "no es una propiedad" not in guarded
    assert "disponibilidad" in guarded


def test_visit_confirmation_requires_semantic_intent_and_rejects_url_only():
    assert not is_visit_confirmation(YAPO_URL)
    assert not is_visit_confirmation(f"{YAPO_URL} disponible?")
    assert is_visit_confirmation("sí")
    assert is_visit_confirmation(f"{YAPO_URL} se puede visitar mañana")
    assert is_visit_confirmation(f"{YAPO_URL} mañana a las 9")
    assert not is_visit_confirmation("Perfecto, entonces quedo atento.")
    assert is_visit_confirmation("Perfecto, quiero ir mañana.")


def test_name_extractor_rejects_negated_roles_but_keeps_real_introductions():
    assert extraer_nombre_explicito("yo no soy corredor") is None
    assert extraer_nombre_explicito("No soy corredor") is None
    assert extraer_nombre_explicito("Soy comprador") is None
    assert extraer_nombre_explicito("Soy propietario") is None
    assert extraer_nombre_explicito("Soy Pablo") == "Pablo"
    assert extraer_nombre_explicito("Me llamo Pablo Galleguillos") == "Pablo Galleguillos"
    assert extraer_nombre_explicito("Mi nombre es Pablo") == "Pablo"


def test_unknown_property_response_requests_only_publication_link():
    prompt = PROMPT_PROPIEDAD_NO_ENCONTRADA.casefold()
    assert "prc-" not in prompt
    assert "código de 5" not in prompt
    assert "enlace de la publicación" in prompt


def test_other_property_rejection_does_not_reject_active_property():
    assert not property_rejected("También vi otro ayer con un corredor, pero no me gustó")
    assert property_rejected("Esta propiedad no me gustó")


def test_conditional_si_does_not_accept_pending_alternatives():
    assert alternative_offer_accepted("Sí", offer_pending=True)
    assert alternative_offer_accepted("Sí, muéstrame otras", offer_pending=True)
    assert alternative_offer_accepted("Claro, muéstrame opciones", offer_pending=True)
    assert alternative_offer_accepted("Dale, veamos otras propiedades", offer_pending=True)
    assert not alternative_offer_accepted("Si finalmente el banco me pide más antecedentes", offer_pending=True)
    assert not alternative_offer_accepted("Si no alcanzo mañana", offer_pending=True)
    assert not alternative_offer_accepted("Si sigue disponible", offer_pending=True)
    assert not alternative_offer_accepted("Sí, pero primero quiero saber el precio", offer_pending=True)


def test_rag_updates_only_on_explicit_search_language():
    narrative = (
        "Mañana salgo de la pega cerca de Plaza Egaña como a las seis y algo. "
        "Si al final sigo interesado, podría desviarme antes de volver a la casa "
        "y pasar por el departamento."
    )
    assert not is_explicit_property_search_request(narrative)
    assert is_explicit_property_search_request("Muéstrame casas en Ñuñoa")


def test_whatsapp_length_policy_keeps_sentences_and_allows_explicit_detail():
    normal = " ".join(["Esta es una oración completa con información útil."] * 80)
    bounded = enforce_whatsapp_response_length(normal, "¿Está disponible?")
    assert len(bounded) <= 1200
    assert not bounded.endswith("información ú")
    detailed = " ".join(["Detalle completo de la ficha."] * 100)
    assert enforce_whatsapp_response_length(detailed, "Explícame la ficha completa") == detailed


def test_specific_property_question_must_answer_requested_field():
    message = "La cuota no la necesito todavía; ¿la ficha dice si acepta mascotas?"
    property_doc = {"codigo": "B200", "caracteristicas": {}}

    response = build_specific_property_response(message, property_doc)

    assert response == "La ficha no indica si acepta mascotas, así que prefiero no confirmártelo sin verificarlo."
    assert "resumen técnico completo" not in response.casefold()


def test_specific_property_question_uses_verified_pet_fact_when_present():
    response = build_specific_property_response(
        "¿La ficha dice si acepta mascotas?",
        {"codigo": "B200", "caracteristicas": {"acepta_mascotas": True}},
    )
    assert response == "La ficha indica que sí se aceptan mascotas."


def test_rag_results_are_presented_as_concrete_whatsapp_options():
    response = _rag_customer_response([
        {"codigo": "B200", "tipo": "Departamento", "operacion": "Venta",
         "comuna": "Ñuñoa", "precio_uf": 4000, "dormitorios": 2},
        {"codigo": "B201", "tipo": "Casa", "operacion": "Venta",
         "comuna": "Ñuñoa", "precio_uf": 4500, "m2_utiles": 90},
        {"codigo": "B202", "tipo": "Casa", "operacion": "Venta",
         "comuna": "Ñuñoa", "precio_uf": 5000},
    ])

    assert "B200" in response and "B201" in response
    assert "B202" not in response
    assert "https://www.procasa.cl/B200" in response
    assert "https://www.procasa.cl/B201" in response
    assert "Encontré una alternativa de búsqueda compatible" not in response


def test_empty_rag_results_are_explicitly_reported():
    response = _rag_customer_response([])
    assert "no encontré coincidencias exactas" in response.casefold()


def test_new_link_rejection_template_is_detected_and_replaced_by_context():
    contaminated = "Entiendo; dejamos activa la propiedad B200 que estamos evaluando y no descarto esta por ese comentario."
    assert _new_link_template_contaminated(contaminated)
    response = _new_link_context_response(
        {"codigo": "B200", "tipo": "Departamento", "comuna": "Ñuñoa", "operacion": "Venta"},
        external_id="EXT-B200",
    )
    assert "B200" in response
    assert "no descarto" not in response.casefold()
    assert "dejamos activa" not in response.casefold()


def test_semantic_evaluator_does_not_pass_internal_state_only():
    rag_results = [{"codigo": "B200"}]
    audit = evaluate_customer_response(
        "Ahora sí, muéstrame otras opciones parecidas en Ñuñoa, venta, hasta 4.000 UF",
        "Encontré una alternativa de búsqueda compatible.",
        rag_results=rag_results,
    )
    assert audit["RAG_RESULTS_ACTUALLY_PRESENTED"] is False
    assert audit["ANSWER_RELEVANCE"] is False
    assert audit["PASS"] is False


def test_semantic_evaluator_rejects_previous_property_template_on_new_link():
    audit = evaluate_customer_response(
        "https://www.procasa.cl/B200",
        "Entiendo; dejamos activa la propiedad B200 y no descarto esta por ese comentario.",
        resolved_property={"codigo": "B200"},
    )
    assert audit["NO_TEMPLATE_CONTAMINATION"] is False
    assert audit["PASS"] is False


def test_visit_semantics_reject_negation_and_historical_reference():
    assert is_explicit_visit_intent("Quisiera ir a verla cuando se pueda.")
    assert is_explicit_visit_intent("¿Hay algún horario para ir a verla?")
    assert is_explicit_visit_intent("kiero verla mañana")
    assert is_explicit_visit_intent("¿se pued visitar?")
    assert not is_explicit_visit_intent("No quiero visitarla.")
    assert not is_explicit_visit_intent("Cuando fui a verla el mes pasado, me gustó.")
    assert extract_visit_preference(
        "cuando fui a verla el 3/4/2026 a las 19:14",
        visit_context=True,
    ) is None


def test_broker_and_name_variants_remain_semantically_separate():
    assert not clasificar_corredor_externo("mi hermano es corredor")["is_external_broker"]
    assert not clasificar_corredor_externo("hablé con un corredor")["is_external_broker"]
    assert clasificar_corredor_externo(
        "represento a mi cliente y hacemos canje"
    )["is_external_broker"]
    assert extraer_nombre_explicito("soy Pablo González y busco casa") == "Pablo González"
    assert extraer_nombre_explicito("mi nombre es Depto Ñuñoa") is None


def test_explicit_financing_and_narrative_property_reference_are_separate():
    assert extract_spontaneous_lead_signals(
        "Tengo crédito aprobado", "Venta"
    )["financing_status"] == "preapproved"
    assert not is_explicit_property_search_request(
        "La otra propiedad de Macul que mencionaste ayer"
    )


def test_availability_states_do_not_conflate_resolution_with_availability():
    assert property_availability_state(None) == "PROPERTY_NOT_FOUND"
    assert property_availability_state({"codigo": "17176"}) == "PROPERTY_FOUND_AVAILABILITY_UNKNOWN"
    assert property_availability_state({"codigo": "17176", "disponible": True}) == "PROPERTY_FOUND_AVAILABLE"
    assert property_availability_state({"codigo": "17176", "disponible": False}) == "PROPERTY_FOUND_BUT_INACTIVE"


def test_real_visit_urgency_phrases_reach_ask_visit_without_broad_false_positives():
    positives = [
        "Quiero visitar",
        "Se puede visitar mañana?",
        "Quiero verla hoy",
        "Podría pasar a conocerlo después del trabajo",
        "Podemos coordinar para el jueves en la mañana",
        "Mejor el viernes después de las 18",
        "Vista ahora en una hora mas",
        "Te dije visita hoy en una hora mas",
    ]
    negatives = [
        "Quiero saber si admiten mascotas",
        "Solo estoy mirando",
        "Vi otra propiedad",
        "mañana reviso la publicación",
        "¿La propiedad tiene visitas virtuales?",
    ]
    assert all(is_explicit_visit_intent(message) for message in positives)
    assert not any(is_explicit_visit_intent(message) for message in negatives)


def test_visit_sentence_starting_with_despues_is_not_a_data_decline():
    assert classify_visit_data_reply(
        "Después de salir de la oficina podría pasar a conocerlo",
        offer_pending=True,
    ) == "unknown"


def test_closing_after_visit_is_not_a_visit_data_acceptance():
    assert classify_visit_data_reply(
        "Perfecto, entonces quedo atento.", offer_pending=True,
    ) == "unknown"


def test_conditional_si_does_not_accept_pending_visit_confirmation():
    from chatbot.conversation_policy import should_offer_visit_data

    assert not should_offer_visit_data(
        "Si finalmente el banco me pide más antecedentes, ¿me pueden orientar?",
        pending_visit_confirmation=True,
    )


def test_new_commercial_question_is_not_replaced_by_duplicate_fallback():
    assert is_actionable_customer_message(
        "No tengo confirmado el monto de la garantía."
    )


def test_visit_data_decline_accepts_entregar_variant():
    assert classify_visit_data_reply(
        "No quiero entregar RUT ni correo", offer_pending=True,
    ) == "declined"


def test_explicit_privacy_decline_is_remembered_without_pending_offer():
    assert classify_visit_data_reply(
        "No quiero entregar RUT ni correo", offer_pending=False,
    ) == "declined"


def test_replaced_visit_preference_drops_obsolete_day():
    from chatbot.conversation_policy import extract_visit_preference

    assert extract_visit_preference(
        "Al final el jueves se me complica; podría ser el domingo después de almuerzo",
        visit_context=True,
    ) == "domingo despues de almuerzo"


def test_ask_visit_uses_canonical_hot_temperature_and_active_cycle_sync(monkeypatch):
    db = mongomock.MongoClient().crm_hot
    db.leads.insert_one({
        "_id": "lead-hot-1", "phone": "+56911112222",
        "last_intent": "ASK_INFO", "lead_temperature_effective": "COLD",
    })
    db.crm_assignment_cycles.insert_one({
        "_id": "cycle-hot-1", "lead_id": "lead-hot-1",
        "cycle_status": "active", "unassigned_at": None,
        "temperature_at_assignment": "COLD",
    })
    monkeypatch.setattr("chatbot.crm_service.get_db", lambda: db)
    monkeypatch.setattr("chatbot.crm_service.log_event", lambda *args, **kwargs: None)

    assert derive_effective_temperature(
        {"lead_temperature_effective": "COLD"},
        overrides={"last_intent": LeadIntent.ASK_VISIT.value},
    ) == "HOT"
    assert CrmService.update_intent("+56911112222", LeadIntent.ASK_VISIT)

    lead = db.leads.find_one({"_id": "lead-hot-1"})
    cycle = db.crm_assignment_cycles.find_one({"_id": "cycle-hot-1"})
    assert lead["last_intent"] == LeadIntent.ASK_VISIT
    assert lead["lead_temperature_effective"] == "HOT"
    assert cycle["temperature_at_assignment"] == "HOT"


class _LegacyWorkerRaceCollection:
    """Delegate to MongoMock and let the compatibility worker win once."""

    def __init__(self, database):
        self._database = database
        self._inner = database.get_collection(queue.JOB_COLLECTION)
        self._triggered = False

    def insert_one(self, document):
        result = self._inner.insert_one(document)
        if document.get("kind") == queue.KIND_JOB and not self._triggered:
            self._triggered = True
            queue.batch_inbound_jobs(
                self._database,
                phone=document["phone"],
                now=document["received_at"],
            )
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _RaceDatabase:
    def __init__(self):
        self._client = mongomock.MongoClient()
        self._database = self._client.queue_race
        self._jobs = _LegacyWorkerRaceCollection(self._database)

    def __getitem__(self, name):
        if name == queue.JOB_COLLECTION:
            return self._jobs
        return self._database[name]


def test_legacy_worker_attach_race_is_acknowledged_without_duplicate_batch():
    db = _RaceDatabase()
    queue.ensure_queue_indexes(db)

    job_id = queue.create_inbound_job(
        db,
        inbound_provider_message_id="race-provider-1",
        phone="+56911112222",
        text="hola",
        received_at=queue.utc_now(),
    )

    job = db[queue.JOB_COLLECTION].find_one({"_id": job_id})
    batches = list(db[queue.JOB_COLLECTION].find({"kind": queue.KIND_BATCH}))
    assert job["batch_id"]
    assert job["state"] == queue.ST_BATCHING
    assert len(batches) == 1
    assert batches[0]["job_ids"] == [job_id]


def test_message_sent_is_ingress_only_and_never_reaches_ai_queue():
    assert is_outbound_message_event({"event": "message.sent", "data": {"messages": {}}})
    assert not is_outbound_message_event({"event": "messages.received", "data": {"messages": {}}})

    source = (Path(__file__).parents[1] / "webhook.py").read_text(encoding="utf-8")
    route = source[source.index('@app.post("/webhook")'):source.index('@app.get("/health")')]
    guard = route.index("if is_outbound_message_event(data):")
    queue_call = route.index("create_inbound_job")
    assert guard < queue_call
