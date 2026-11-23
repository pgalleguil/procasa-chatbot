"""Regression replays from real, authorized executive-chat patterns.

All phone numbers and IDs in this test are synthetic; no provider calls.
"""
from datetime import datetime, timezone

import mongomock
from bson import ObjectId

from chatbot.executive_semantics import intent
from chatbot.executive_temperature import classify_executive_priority
from chatbot.central_support_alerts import notify_central_support_task


def test_visiting_vina_in_two_messages_is_an_actionable_visit():
    assert intent("Yo viajo el miercoles a viña, para coordinar verlo") == "ASK_VISIT"
    assert intent("hola, cree que podamos ver el local a las 2??? avíseme.") == "ASK_VISIT"
    assert intent("No puedo visitar mañana") == "NO_SIGNAL"
    assert intent("Si quisiera visitar, con quién tendría que hablar?") == "NO_SIGNAL"


def test_alternative_requests_in_real_customer_language():
    assert intent("Sería ideal si tiene en su sistema algo parecido, con estacionamiento") == "ASK_ALTERNATIVES"
    assert intent("Busco algo más cerca del San Fco Javier") == "ASK_ALTERNATIVES"
    assert intent("Qué requisitos piden para arrendar?") == "ASK_INFO"
    assert intent("Está regularizada la casa?") == "ASK_INFO"
    assert intent("No tengo presupuesto para eso, 43 millones de subsidio") == "PROPERTY_DECLINED"
    assert intent("Gracias") == "NO_SIGNAL"


def test_temperature_is_independent_from_office_hot_sla_and_gratitude():
    last_thanks = {
        "intent": "NO_SIGNAL",
        "search_status": "ACTIVE",
        "last_customer_message_id": "last-thanks",
        "executive_management": {"pending_customer_request": True},
    }
    open_visit = {
        "intent": "ASK_VISIT",
        "status": "PENDIENTE_GESTION",
        "last_customer_message_id": "visit-1",
        "agent_reply_observed": True,  # acknowledgement != confirmed visit
    }
    result = classify_executive_priority(last_thanks, [open_visit])
    assert result["temperature"] == "CALIENTE"
    assert result["next_action"] == "PRIORIZAR_VISITA_O_CONTACTO"
    assert result["changes_crm_sla"] is False
    assert "visit-1" in result["evidence_provider_ids"]


def test_alternative_without_budget_is_warm_and_requires_central_support():
    result = classify_executive_priority(
        {"intent": "ASK_ALTERNATIVES", "search_status": "ACTIVE"},
        [],
        {"status": "ACTIVE", "followup_required": True,
         "criteria": {"parking_required": True, "near_metro_required": True}},
    )
    assert result["temperature"] == "TIBIO"
    assert result["central_support_required"] is True
    assert result["next_action"] == "BUSCAR_ALTERNATIVAS"


def test_no_customer_evidence_is_unknown_and_stop_is_not_a_cold_lead():
    assert classify_executive_priority(
        {"intent": "NO_SIGNAL"}, [],
    )["temperature"] == "SIN_EVIDENCIA"
    assert classify_executive_priority(
        {"intent": "OPT_OUT"}, [],
    )["next_action"] == "NO_CONTACTAR"


def _support_case(db, *, test=False, opted_out=False):
    lead_id = ObjectId()
    cycle, owner = "cycle-1", "user-1"
    db.leads.insert_one({
        "_id": lead_id, "phone_is_synthetic": test,
        "lifecycle": {"current_assignment_cycle_id": cycle,
                      "assigned_to_user_id": owner},
        "contact_restrictions": {"marketing_opt_out": opted_out},
        "commercial_intelligence": {"active_search": {
            "status": "ACTIVE", "followup_required": True, "opt_out": False,
        }},
    })
    return {
        "task_id": "active_search:test12345678901234567890123",
        "type": "SEARCH_ALTERNATIVES", "lead_id": lead_id,
        "assignment_cycle_id": cycle, "assigned_to_user_id": owner,
        "agent": "Ejecutiva Demo", "phone": "56911111111",
        "note": "Buscar una propiedad con estacionamiento, cerca del Metro",
        "property_codes_to_review": [],
    }


def test_admin_alert_is_event_driven_exactly_once(monkeypatch):
    from chatbot import central_support_alerts as alerts

    db = mongomock.MongoClient().support
    task = _support_case(db)
    monkeypatch.setenv(alerts.ENABLED_FLAG, "1")
    monkeypatch.setattr(alerts, "_pablo_recipient", lambda _db: "56922222222")
    sent = []

    def send(number, text):
        sent.append((number, text))
        return {"success": True, "provider_message_id": "provider-1"}

    assert notify_central_support_task(
        db, task, newly_created=True, send=send,
        now=datetime(2026, 11, 23, tzinfo=timezone.utc),
    ) == "accepted"
    assert len(sent) == 1
    assert sent[0][0] == "56922222222"
    assert "Ejecutiva Demo" in sent[0][1]
    assert "No se envió ningún mensaje al cliente." in sent[0][1]
    assert notify_central_support_task(
        db, task, newly_created=False, send=send,
    ) == "existing"
    assert len(sent) == 1
    assert db.crm_central_support_alerts.find_one({})["delivery"] == "accepted"


def test_admin_alert_never_contacts_opted_out_or_test_leads(monkeypatch):
    from chatbot import central_support_alerts as alerts

    db = mongomock.MongoClient().blocked
    monkeypatch.setenv(alerts.ENABLED_FLAG, "1")
    monkeypatch.setattr(alerts, "_pablo_recipient", lambda _db: "56922222222")
    for is_test, opt_out in ((True, False), (False, True)):
        task = _support_case(db, test=is_test, opted_out=opt_out)
        assert notify_central_support_task(
            db, task, newly_created=True,
            send=lambda *_: (_ for _ in ()).throw(AssertionError("MUST NOT SEND")),
        ) == "not_eligible"
    assert db.crm_central_support_alerts.count_documents({}) == 0


def test_admin_alert_unknown_delivery_is_not_retried(monkeypatch):
    from chatbot import central_support_alerts as alerts

    db = mongomock.MongoClient().uncertain
    task = _support_case(db)
    monkeypatch.setenv(alerts.ENABLED_FLAG, "1")
    monkeypatch.setattr(alerts, "_pablo_recipient", lambda _db: "56922222222")
    sent = []
    def send(*args):
        sent.append(args)
        return {"success": False, "provider_call_uncertain": True}
    assert notify_central_support_task(db, task, newly_created=True, send=send) == "delivery_unknown"
    assert notify_central_support_task(db, task, newly_created=True, send=send) == "existing"
    assert len(sent) == 1
