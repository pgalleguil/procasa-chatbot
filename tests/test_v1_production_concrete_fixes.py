from pathlib import Path

import mongomock

from chatbot import chatbot_queue as queue
from chatbot.constants import LeadIntent
from chatbot.conversation_policy import (
    is_explicit_visit_intent,
    is_visit_confirmation,
    resolve_visit_intent,
)
from chatbot.crm_service import CrmService
from chatbot.lead_temperature import derive_effective_temperature
from chatbot.property_lookup import (
    canonical_property_context,
    guard_resolved_property_response,
    lookup_property_link,
    property_availability_state,
)
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


def test_semantic_visit_intent_recovers_novel_phrase_without_second_llm_call():
    message = "Después de salir del trabajo podría pasar a conocer el departamento"
    assert not is_explicit_visit_intent(message)

    resolved = resolve_visit_intent(message, "agendar_visita")

    assert resolved["visit_intent"] is True
    assert resolved["semantic_visit"] is True
    assert resolved["deterministic_visit"] is False
    assert resolved["negative_veto"] is False
    assert resolved["resolution_source"] == "model"


def test_semantic_visit_intent_is_vetoed_for_non_visit_meanings():
    negatives = [
        "¿Tienen visita virtual?",
        "Visité otro departamento ayer.",
        "Mañana voy a revisar el aviso.",
        "Mi hermano fue a verlo.",
        "Solo estoy mirando por ahora.",
        "No quiero visitarlo.",
        YAPO_URL,
    ]

    for message in negatives:
        resolved = resolve_visit_intent(message, "agendar_visita")
        assert resolved["visit_intent"] is False, message
        assert resolved["negative_veto"] is True, message


def test_semantic_visit_intent_keeps_optional_data_capture_conservative():
    resolved = resolve_visit_intent(
        "Estoy bastante interesado, quisiera conocerlo antes de decidir.",
        "agendar_visita",
    )

    assert resolved["visit_intent"] is True
    assert resolved["deterministic_visit"] is True


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
