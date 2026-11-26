"""Regression tests for executive hybrid commercial classification.

Pure tests: no live DeepSeek, WaSender, CRM writes, or external HTTP.
"""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from bson import ObjectId

from chatbot.executive_conversation_intelligence import (
    VERSION, _safe, classify_hybrid, rule_intent,
)
from chatbot import executive_intelligence_worker as worker


def turn(actor, text, msg_id="m1", *, cycle="cycle-A", session="133286", owner="69796bc4bbebf240378eb739"):
    return {
        "actor_type": actor, "content": text,
        "channel": "executive_whatsapp",
        "message_id": msg_id, "provider_message_id": msg_id,
        "timestamp": "2026-11-16T16:20:00+00:00" if actor == "human_agent" else "2026-11-16T16:21:00+00:00",
        "assignment_cycle_id": cycle,
        "assigned_executive_id": owner,
        "wasender_session_id": session,
    }


@pytest.mark.parametrize("message,expected", [
    ("No me escriban más", "OPT_OUT"),
    ("Quiero que me den de baja", "OPT_OUT"),
    ("Ya encontré una casa", "SEARCH_COMPLETED"),
    ("Ya arrendé un departamento", "SEARCH_COMPLETED"),
    ("Esa propiedad no me interesa", "PROPERTY_DECLINED"),
    ("No puedo visitar mañana", "NO_SIGNAL"),
    ("Quiero agendar una visita", "ASK_VISIT"),
    ("Quiero hablar con un ejecutivo", "ASK_CONTACT"),
    ("Mi oferta sería 2.700 UF", "GIVE_OFFER"),
    ("Ahora busco en Peñaflor", "ASK_ALTERNATIVES"),
    ("¿Cuánto cuesta?", "ASK_INFO"),
    ("Me interesa", "INTERESTED"),
])
def test_rules_distinguish_semantics(message, expected):
    assert rule_intent(message) == expected


def test_opt_out_not_equivalent_to_completed_search_or_property_decline():
    summaries = {}
    for text in ("No me escriban más", "Ya encontré una casa", "Esa propiedad no me interesa"):
        summaries[text] = classify_hybrid([turn("customer", text)], llm_enabled=False)
    assert summaries["No me escriban más"]["campaign"]["reason"] == "explicit_opt_out"
    assert summaries["Ya encontré una casa"]["campaign"]["reason"] == "search_completed"
    assert summaries["Esa propiedad no me interesa"]["campaign"]["reason"] is None
    assert all(not x["campaign"]["enforced"] for x in summaries.values())
    assert all(x["needs_review"] for x in list(summaries.values())[:2])


def test_hybrid_extracts_only_customer_supported_search_and_visits():
    calls = []
    def model(messages, previous):
        calls.append(messages)
        return {
            "intent": "ASK_VISIT", "confidence": .92, "needs_review": False,
            "search": {
                "operation": "ARRIENDO", "property_type": "DEPARTAMENTO",
                "comunas": ["Maipú", "Las Condes"],
                "max_price_uf": 20, "bedrooms": 2,
            }
        }
    messages = [
        turn("human_agent", "Tengo departamentos en Las Condes; agendemos una visita", "agent-1"),
        turn("customer", "Busco arriendo en Maipú, departamento de 2 dormitorios, hasta 20 UF.", "customer-1"),
    ]
    result = classify_hybrid(messages, llm_enabled=True, propose=model)
    assert calls and len(calls) == 1
    assert result["intent"] == "SEARCH_UPDATED"
    assert result["executive_management"]["case_type"] == "REQUISITOS_ACTUALIZADOS"
    assert result["executive_management"]["next_step_hint"] == "ACTUALIZAR_BUSQUEDA"
    assert "suggested_priority" not in result
    assert result["needs_review"]  # model-only visit requires human review
    assert result["search"] == {
        "operation": "ARRIENDO", "property_type": "DEPARTAMENTO",
        "comunas": ["Maipú"], "max_price_uf": 20, "bedrooms": 2,
    }
    assert result["contact"]["customer_responded"] is True
    assert result["campaign"]["enforced"] is False


def test_outgoing_cannot_create_customer_intent_or_count_as_reply():
    messages = [
        turn("customer", "Hola", "before-agent"),
        turn("human_agent", "No le enviaré más mensajes. Ya compró un departamento.", "out"),
    ]
    messages[0]["timestamp"] = "2026-11-16T16:19:00+00:00"
    summary = classify_hybrid(messages, llm_enabled=False)
    assert summary["intent"] == "NO_SIGNAL"
    assert summary["contact"]["customer_responded"] is False
    assert summary["contact"]["awaiting"] == "CUSTOMER"
    assert summary["contact"]["agent_contacted"] is True


def test_previous_customer_intent_retained_without_reanalysis():
    customer = turn("customer", "¿Cuánto cuesta?", "user-1")
    prior = {"summary": {
        "intent": "ASK_INFO", "last_customer_message_id": "user-1",
        "search": {"comunas": ["Maipú"]},
    }}
    def prohibited(*args):
        raise AssertionError("same customer should not cause another model call")
    summary = classify_hybrid(
        [customer, turn("human_agent", "Le envío detalles", "agent-2")],
        previous=prior, llm_enabled=True, propose=prohibited,
    )
    assert summary["intent"] == "ASK_INFO"
    assert summary["search"]["comunas"] == ["Maipú"]


def test_provider_failure_fails_to_rules_and_never_stops_campaign_automatically(caplog):
    def failing(*_):
        raise RuntimeError("private-payload-sentinel")
    summary = classify_hybrid(
        [turn("customer", "Ya encontré una casa")],
        llm_enabled=True, propose=failing,
    )
    assert summary["intent"] == "SEARCH_COMPLETED"
    assert summary["source"] == "rules_fallback"
    assert summary["needs_review"] is True
    assert "private-payload-sentinel" not in caplog.text


def test_pii_scrubbed_before_model_prompt():
    text = "llámame +56983219804, email ejemplo@test.cl, rut 12.345.678-9"
    safe = _safe(text)
    assert "983219804" not in safe
    assert "ejemplo@test.cl" not in safe
    assert "12.345.678-9" not in safe


def test_low_confidence_model_cannot_invent_global_opt_out():
    summary = classify_hybrid(
        [turn("customer", "Por ahora prefiero esperar")],
        llm_enabled=True,
        propose=lambda *args: {
            "intent": "OPT_OUT", "confidence": .9, "needs_review": False,
            "search": {},
        },
    )
    assert summary["intent"] == "NO_SIGNAL"
    assert summary["campaign"]["reason"] is None


@pytest.fixture(autouse=True)
def capture_config(monkeypatch):
    monkeypatch.setenv("PROCASA_WASENDER_TEST_AGENT", '{"session_id":133286,"webhook_secret":"synthetic-secret-123","api_token":"synthetic-token-123","capture_enabled":true}')


class Cursor(list):
    def limit(self, n):
        return self[:n]


class FakeUsers:
    def find(self, query, projection):
        return Cursor([{"_id": ObjectId("69796bc4bbebf240378eb739"), "nombre": "Test Agent"}])
    def find_one(self, query, projection):
        if query.get("is_active") is True and "133286" in query["wasender.session_id"]["$in"]:
            return {"_id": query["_id"]}
        return None


class FakeLeads:
    def __init__(self):
        self.writes = []
    def find_one(self, selector, projection=None):
        # This synthetic classification fixture contains no office transcript.
        return {}
    def update_one(self, selector, mutation):
        self.writes.append((selector, mutation))
        return SimpleNamespace(modified_count=1)


def fake_doc():
    owner = "69796bc4bbebf240378eb739"
    return {
        "_id": ObjectId("690e13577fb5678251136b01"),
        "lifecycle": {
            "assigned_to_user_id": owner,
            "current_assignment_cycle_id": "cycle-A",
        },
        "commercial_intelligence": {
            "pending": True, "pending_message_id": "uuid-1",
            "pending_cycle_id": "cycle-A",
            "pending_session_id": "133286",
            "pending_source": "executive_whatsapp",
        },
        "messages": [
            turn("human_agent", "Buen día", "outgoing"),
            turn("customer", "Quiero agendar una visita", "incoming"),
        ],
    }


def test_worker_interprets_only_current_authorized_cycle(monkeypatch):
    cycle = {"assignment_cycle_id": "cycle-A",
             "assigned_to_user_id": "69796bc4bbebf240378eb739",
             "cycle_status": "active", "unassigned_at": None}
    monkeypatch.setattr(worker, "active_assignment_cycle", lambda db, lead_id: cycle)
    db = {"usuarios": FakeUsers(), "leads": FakeLeads()}
    assert worker.process_claimed(db, fake_doc(), llm_enabled=False) == "ready"
    selector, mutation = db["leads"].writes[0]
    assert selector["commercial_intelligence.pending_message_id"] == "uuid-1"
    assert mutation["$set"]["commercial_intelligence.summary"]["intent"] == "ASK_VISIT"
    assert mutation["$set"]["commercial_intelligence.summary"]["contact"]["customer_responded"]
    assert mutation["$set"]["commercial_intelligence.summary"]["campaign"]["enforced"] is False


def test_worker_refuses_mismatched_owner_and_never_classifies(monkeypatch):
    cycle = {"assignment_cycle_id": "cycle-A",
             "assigned_to_user_id": "DIFFERENT_OWNER",
             "cycle_status": "active", "unassigned_at": None}
    monkeypatch.setattr(worker, "active_assignment_cycle", lambda db, lead_id: cycle)
    db = {"usuarios": FakeUsers(), "leads": FakeLeads()}
    assert worker.process_claimed(db, fake_doc(), llm_enabled=False) == "stale_assignment"
    assert "commercial_intelligence.summary" not in db["leads"].writes[0][1]["$set"]


def test_worker_filters_old_cycle_messages(monkeypatch):
    cycle = {"assignment_cycle_id": "cycle-A",
             "assigned_to_user_id": "69796bc4bbebf240378eb739",
             "cycle_status": "active", "unassigned_at": None}
    monkeypatch.setattr(worker, "active_assignment_cycle", lambda db, lead_id: cycle)
    doc = fake_doc()
    doc["messages"].append(turn("customer", "No me escriban más", "OLD", cycle="cycle-OLD"))
    db = {"usuarios": FakeUsers(), "leads": FakeLeads()}
    assert worker.process_claimed(db, doc, llm_enabled=False) == "ready"
    summary = db["leads"].writes[0][1]["$set"]["commercial_intelligence.summary"]
    assert summary["intent"] == "ASK_VISIT"

@pytest.mark.parametrize("message,case_type,action", [
    ("Quiero agendar una visita", "VISITA_SOLICITADA", "COORDINAR_VISITA"),
    ("¿Me pueden llamar?", "CONTACTO_SOLICITADO", "CONTACTAR_CLIENTE"),
    ("Quiero presentar una oferta", "OFERTA_PROPUESTA", "REVISAR_OFERTA"),
    ("Me interesa", "INTERES_EXPRESADO", "DAR_SEGUIMIENTO"),
    ("Esa propiedad no me interesa", "PROPIEDAD_DESCARTADA", "ACTUALIZAR_PREFERENCIAS"),
    ("Quiero vender mi casa", "CAPTACION_VENTA", "GESTIONAR_CAPTACION_VENTA"),
    ("Ya encontré una casa", "BUSQUEDA_FINALIZADA", "REVISAR_CIERRE_BUSQUEDA"),
    ("No me escriban más", "BAJA_COMUNICACIONES", "RESPETAR_RESTRICCION_CONTACTO"),
])
def test_executive_cases_are_commercial_not_chatbot_temperature(message, case_type, action):
    result = classify_hybrid([turn("customer", message)], llm_enabled=False)
    assert result["executive_management"]["case_type"] == case_type
    assert result["executive_management"]["next_step_hint"] == action
    assert result["executive_management"]["formal_crm_result"] is False
    assert "suggested_priority" not in result
    assert not ({"lead_temperature", "lead_temperature_effective", "temperature_at_assignment"} & set(result))


def test_executive_case_is_not_affected_by_prior_chatbot_hot_or_cold():
    message = turn("customer", "Quiero agendar una visita", "fresh")
    for temperature in ("HOT", "COLD", "NORMAL"):
        prior = {"lead_temperature_effective": temperature, "temperature_at_assignment": temperature}
        result = classify_hybrid([message], prior, llm_enabled=False)
        assert result["executive_management"]["case_type"] == "VISITA_SOLICITADA"
        assert result["executive_management"]["next_step_hint"] == "COORDINAR_VISITA"
        assert "suggested_priority" not in result
        assert prior == {"lead_temperature_effective": temperature, "temperature_at_assignment": temperature}


def test_executive_new_summary_does_not_retain_old_temperature_suggestion():
    old = {"summary": {"intent": "ASK_VISIT", "suggested_priority": "HOT",
                       "latest_important_request": {"intent": "ASK_VISIT",
                           "provider_message_id": "old", "timestamp": "2026-11-16T16:21:00+00:00",
                           "executive_message_after_request": False,
                           "fulfillment": "UNKNOWN"}}}
    summary = classify_hybrid([turn("customer", "Hola", "fresh")], old, llm_enabled=False)
    assert "suggested_priority" not in summary
    assert summary["executive_management"]["case_type"] == "SIN_SENAL_CLARA"
    assert summary["executive_management"]["next_step_hint"] == "COORDINAR_VISITA"
    assert summary["executive_management"]["pending_customer_request"]



def test_reply_to_automatic_followup_does_not_accept_stale_visit():
    human = turn("human_agent", "¿Quiere agendar una visita?", "human")
    automated = turn("automation", "¿Continúa buscando propiedad?", "auto")
    automated.update(source="crm_exec_silence_api", automatic=True,
                     timestamp="2026-11-16T17:00:00+00:00")
    customer = turn("customer", "Sí, gracias", "reply")
    customer["timestamp"] = "2026-11-16T17:02:00+00:00"
    result = classify_hybrid([human, automated, customer], llm_enabled=False)
    assert result["intent"] != "ASK_VISIT"
    assert result["contact"]["awaiting"] == "EXECUTIVE"
    assert result["contact"]["window_agent_messages"] == 1
    assert result["contact"]["window_customer_messages"] == 1


def test_automatic_followup_never_counts_as_human_management():
    automated = turn("automation", "¿Continúa buscando propiedad?", "auto")
    automated.update(source="crm_exec_silence_api", automatic=True)
    result = classify_hybrid([automated], llm_enabled=False)
    assert result["contact"]["agent_contacted"] is False
    assert result["contact"]["window_agent_messages"] == 0
    assert result["contact"]["last_actor"] == "automation"
    assert result["contact"]["formal_crm_management"] is False


@pytest.mark.parametrize("message,expected,reason", [
    ("Solo estaba mirando. No estaré en condiciones de ahorrar por el próximo año. Más adelante quizás hablemos.", "SEARCH_DEFERRED", "search_deferred"),
    ("No gracias", "CONTACT_DECLINED", "contact_declined"),
    ("Hola, no sé ni que es procasa", "CONTACT_UNRECOGNIZED", "contact_unrecognized"),
    ("Finalmente nos desidimos por otro departamento. Muchas gracias.", "SEARCH_COMPLETED", "search_completed"),
    ("No ne respindiste", "RESPONSE_PENDING", None),
])
def test_observed_customer_cases(message, expected, reason):
    summary = classify_hybrid([turn("customer", message)], llm_enabled=False)
    assert summary["intent"] == expected
    assert summary["campaign"]["reason"] == reason
    assert summary["contact_permission"] == "UNKNOWN"


def test_deferred_outreach_hold_survives_acknowledgment_and_model_disagreement():
    customer = turn("customer", "Solo estaba mirando. Más adelante quizás hablemos.", "c1")
    first = classify_hybrid([customer], llm_enabled=False)
    ack = turn("customer", "Gracias", "c2")
    ack["timestamp"] = "2026-11-16T16:22:00+00:00"
    second = classify_hybrid([customer, ack], previous={"summary": first},
                             llm_enabled=True, propose=lambda *_: {
        "intent": "INTERESTED", "search": {}, "confidence": .99, "needs_review": False,
        "evidence": {"message_id": "c2", "quote": "Gracias"}})
    assert second["search_status"] == "DEFERRED"
    assert second["outreach_hold"] == "SEARCH_DEFERRED"
    assert second["campaign"]["reason"] == "search_deferred"
    assert second["contact"]["awaiting"] == "NONE"
