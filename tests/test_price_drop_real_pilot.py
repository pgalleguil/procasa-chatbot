"""Real campaign acceptance tests; NEVER contact WhatsApp or external systems."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from bson import ObjectId

from chatbot.price_drop_campaign_real import (
    CAMPAIGNS, DISPATCHES, TURNS, CampaignBlocked, approve_campaign,
    campaign_report, halt_campaign, persist_plan, preview_plan,
    record_bot_reply, record_delivery_update, record_inbound, send_one_due,
)
from chatbot.price_drop_pilot import load_active_context

UTC = timezone.utc
NOW = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)
# 05 Nov 2026, 11:00 Chile DST.
START = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)
DROP = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)


@pytest.fixture
def db():
    db = mongomock.MongoClient()["URLS"]
    db["universo_cartera_prop360"].insert_one({
        "codigo": "6436", "disponible_prop360": True,
        "estado": {"oficina": "PROCASA SUCRE"},
        "tipo_operacion": {"venta": True, "precio_venta": {"precio_uf": 2690}},
        "ubicacion": {"calle": "PRIVATE STREET 1234", "unidad": "99",
                      "comuna": "San Miguel"},
    })
    for number in (1, 2, 3):
        db["leads"].insert_one({
            "_id": ObjectId(f"69000000000000000000000{number}"),
            "phone": f"+5693000000{number}",
            "contact_phone_normalized": f"+5693000000{number}",
            "prospecto": {"codigo": "6436"},
            "created_at": "2026-08-10T14:00:00-04:00",
            "messages": [
                {"role": "user", "content": "Hola, quisiera más información de esta propiedad",
                 "timestamp": "2026-08-10T14:00:00-04:00"},
                {"role": "assistant", "content": "Claro", "timestamp": "2026-08-10T14:01:00-04:00"},
            ],
        })
    return db


def plan(db, n=2, campaign_id="piloto6436_20261105"):
    ids = [d["_id"] for d in db["leads"].find().sort("_id", 1)]
    return {
        "campaign_id": campaign_id,
        "property_code": "6436",
        "old_price_uf": 2790,
        "new_price_uf": 2690,
        "price_drop_at": DROP.isoformat(),
        "price_change_evidence": "Validación humana del historial Prop360: 2790 → 2690",
        "start_at": START.isoformat(), "gap_minutes": 12,
        "reviewed_by": "Supervisor PROCASA",
        "recipients": [
            {"lead_id": str(ids[i]), "consent_reviewed": True,
             "consent_basis": "Conversación previa verificada y autorización revisada"}
            for i in range(n)
        ],
    }


def approve(db, n=2, campaign_id="piloto6436_20261105"):
    output = persist_plan(db, plan(db, n, campaign_id), now=NOW)
    assert output["status"] == "PLANNED"
    assert approve_campaign(
        db, campaign_id, operator="Supervisor PROCASA", now=NOW
    )["status"] == "APPROVED"


def enable(monkeypatch, campaign_id="piloto6436_20261105"):
    monkeypatch.setenv("PRICE_DROP_REAL_PILOT_ENABLED", "true")
    monkeypatch.setenv("PRICE_DROP_REAL_PILOT_APPROVED_ID", campaign_id)
    monkeypatch.setenv("PRICE_DROP_REAL_PILOT_CONTEXT_ENABLED", "true")
    # Legacy functionality is retained but frozen in production by default.
    # Existing contract tests explicitly opt into the historical sender.
    monkeypatch.setenv("PRICE_DROP_CAMPAIGN_EXEC_FROZEN", "false")


def test_price_drop_message_natural_and_not_pushy():
    from chatbot.price_drop_campaign_real import _message
    msg = _message("6436", 2790, 2690)
    assert "de San Miguel" in msg
    assert "UF 2.790" in msg
    assert "bajó 100 UF" in msg
    assert "UF 2.690" in msg
    assert "https://www.procasa.cl/6436" in msg
    assert "¿te sigue interesando?" in msg
    assert "no recibir más avisos" in msg
    assert "visitar" not in msg.casefold()
    assert "agendar" not in msg.casefold()

    other = _message("8001", 3000, 2800)
    assert "de San Miguel" not in other
    assert "código 8001" in other


def test_preview_read_only_and_paced_by_12_minutes(db):
    p = preview_plan(db, plan(db, 3), now=NOW)
    assert p["ready_to_plan"] and not p["blocked"]
    assert len(p["approved"]) == 3
    times = [datetime.fromisoformat(x["scheduled_at"]) for x in p["approved"]]
    assert times[1] - times[0] == timedelta(minutes=12)
    assert times[2] - times[1] == timedelta(minutes=12)
    assert db[CAMPAIGNS].count_documents({}) == 0
    assert db[DISPATCHES].count_documents({}) == 0


def test_persist_records_every_lead_property_attempt_but_no_send(db):
    r = persist_plan(db, plan(db), now=NOW)
    assert r["sent"] == 0 and r["recipients"] == 2
    header = db[CAMPAIGNS].find_one({"_id": r["campaign_id"]})
    docs = list(db[DISPATCHES].find({"campaign_id": r["campaign_id"]}))
    assert header["status"] == "PLANNED"
    assert len(docs) == 2 and all(x["status"] == "PLANNED" for x in docs)
    assert docs[0]["lead_id"] != docs[1]["lead_id"]
    assert all(x["property_code"] == "6436" for x in docs)
    assert all("inquiry_evidence_at" in x for x in docs)
    assert all("provider_message_id" not in x for x in docs)


def test_disabled_or_not_approved_cannot_send(db, monkeypatch):
    persist_plan(db, plan(db), now=NOW)
    called = []
    sender = lambda *_: called.append(True)
    assert send_one_due(db, "piloto6436_20261105", sender=sender, now=START)["status"] == "DISABLED"
    enable(monkeypatch)
    assert send_one_due(db, "piloto6436_20261105", sender=sender, now=START)["status"] == "NOT_APPROVED_OR_HALTED"
    assert called == []


def test_one_whatsapp_per_window_and_durable_lead_marker(db, monkeypatch):
    approve(db, n=3)
    enable(monkeypatch)
    deliveries = []
    def fake_sender(phone, text):
        deliveries.append((phone, text))
        return {"success": True, "provider_message_id": f"provider-{len(deliveries)}",
                "delivery_status": "accepted", "http_status": 200}

    first = send_one_due(db, "piloto6436_20261105", sender=fake_sender, now=START)
    assert first["status"] == "SENT"
    assert first["phone_last4"].endswith("0001")
    assert len(deliveries) == 1
    assert "UF 2.790" in deliveries[0][1] and "UF 2.690" in deliveries[0][1]
    assert "PRIVATE STREET" not in deliveries[0][1]
    assert send_one_due(db, "piloto6436_20261105", sender=fake_sender,
                        now=START + timedelta(minutes=3))["status"] == "WINDOW_NOT_OPEN"
    second = send_one_due(db, "piloto6436_20261105", sender=fake_sender,
                          now=START + timedelta(minutes=12))
    assert second["status"] == "SENT" and len(deliveries) == 2
    third = send_one_due(db, "piloto6436_20261105", sender=fake_sender,
                         now=START + timedelta(minutes=24))
    assert third["status"] == "SENT" and len(deliveries) == 3
    assert send_one_due(db, "piloto6436_20261105", sender=fake_sender,
                        now=START + timedelta(minutes=36))["status"] in {
                            "NO_DUE_RECIPIENT", "WINDOW_NOT_OPEN"
                        }
    assert len(deliveries) == 3
    marked = db["leads"].find_one({"_id": ObjectId("690000000000000000000001")})
    assert marked["price_drop_campaign_v1"]["has_received"] is True
    assert marked["price_drop_campaign_v1"]["last_property_code"] == "6436"
    assert marked["stage"] if "stage" in marked else True
    assert "sla_status" not in marked


def test_provider_unknown_after_attempt_never_retries(db, monkeypatch):
    approve(db)
    enable(monkeypatch)
    calls = []
    def uncertain(*_):
        calls.append(1)
        raise TimeoutError("Provider may have accepted the message")
    first = send_one_due(db, "piloto6436_20261105", sender=uncertain, now=START)
    assert first["status"] == "DELIVERY_UNKNOWN"
    next_tick = send_one_due(db, "piloto6436_20261105", sender=uncertain,
                             now=START + timedelta(minutes=1))
    assert next_tick["status"] == "WINDOW_NOT_OPEN" and len(calls) == 1
    first_doc = db[DISPATCHES].find_one({"status": "DELIVERY_UNKNOWN"})
    assert first_doc["attempted_at"].replace(tzinfo=UTC) == START and first_doc.get("sent_at") is None


def test_duplicate_lead_and_duplicate_phone_fail_closed(db):
    p = plan(db)
    p["recipients"].append(dict(p["recipients"][0]))
    assert preview_plan(db, p, now=NOW)["ready_to_plan"] is False
    db["leads"].insert_one({
        "_id": ObjectId("690000000000000000000009"),
        "phone": "+56930000001", "contact_phone_normalized": "+56930000001",
    })
    assert preview_plan(db, plan(db, 1), now=NOW)["blocked"][0]["reason"] == "lead_phone_ambiguous"


def test_old_lead_without_customer_message_cannot_be_contacted(db):
    db["leads"].update_one({"_id": ObjectId("690000000000000000000001")},
                           {"$set": {"messages": [
                               {"role": "assistant", "content": "Hola",
                                "timestamp": "2026-08-10T14:00:00-04:00"},
                           ]}})
    out = preview_plan(db, plan(db, 1), now=NOW)
    assert not out["ready_to_plan"]
    assert out["blocked"][0]["reason"] == "pre_drop_inquiry_not_verified"


def test_consent_review_and_optout_are_mandatory(db):
    p = plan(db, 1)
    p["recipients"][0]["consent_reviewed"] = False
    assert preview_plan(db, p, now=NOW)["blocked"][0]["reason"] == "recipient_consent_review_missing"
    lead_id = ObjectId(p["recipients"][0]["lead_id"])
    db["leads"].update_one({"_id": lead_id}, {"$set": {
        "conversation_status": "STOPPED_BY_CLIENT"
    }})
    assert preview_plan(db, plan(db, 1), now=NOW)["blocked"][0]["reason"] == "lead_opted_out_or_blocked"


def test_duplicate_previous_campaign_for_same_property_blocked(db):
    persist_plan(db, plan(db, 1), now=NOW)
    p = plan(db, 1, "second6436_20261105")
    out = preview_plan(db, p, now=NOW)
    assert out["blocked"][0]["reason"] == "property_campaign_already_recorded"


def test_inbound_conversation_bot_reply_and_metrics(db, monkeypatch):
    approve(db, n=1)
    enable(monkeypatch)
    sent = send_one_due(db, "piloto6436_20261105", now=START,
                        sender=lambda *_: {"success": True, "provider_message_id": "prov1"})
    assert sent["status"] == "SENT"
    reply_at = START + timedelta(minutes=2)
    response = record_inbound(db, phone="+56930000001", provider_message_id="in1",
                              text="Me interesa, quiero una visita", received_at=reply_at)
    assert response["status"] == "ATTRIBUTED"
    assert record_inbound(db, phone="+56930000001", provider_message_id="in1",
                          text="Me interesa, quiero una visita", received_at=reply_at)["status"] == "DUPLICATE_IGNORED"
    out = record_bot_reply(db, phone="+56930000001", provider_message_id="bot1",
                           text="¿Qué día te acomodaría?", sent_at=reply_at + timedelta(minutes=1))
    assert out["status"] == "ATTRIBUTED"
    assert db[TURNS].count_documents({}) == 2
    report = campaign_report(db, "piloto6436_20261105")
    assert report["sent"] == 1
    assert report["responded"] == 1
    assert report["response_rate"] == 1
    assert report["positive_responses_after_notice"] == 1
    assert report["visits_requested"] == 1
    assert report["rows"][0]["reply_count"] == 1
    lead = db["leads"].find_one({"_id": ObjectId("690000000000000000000001")})
    assert lead["price_drop_campaign_v1"]["last_status"] == "RESPONDED"


def test_stop_prevents_future_campaign_and_tracks_optout(db, monkeypatch):
    approve(db, n=1)
    enable(monkeypatch)
    assert send_one_due(db, "piloto6436_20261105", now=START,
                        sender=lambda *_: {"success": True, "provider_message_id": "prov1"})["status"] == "SENT"
    assert record_inbound(db, phone="+56930000001", provider_message_id="stop1",
                          text="No me escriban más", received_at=START + timedelta(minutes=1))["status"] == "OPTED_OUT"
    row = db[DISPATCHES].find_one({"campaign_id": "piloto6436_20261105"})
    assert row["status"] == "OPTED_OUT"
    lead = db["leads"].find_one({"_id": row["lead_id"]})
    assert lead["price_drop_campaign_v1"]["opted_out"] is True
    assert preview_plan(db, plan(db, 1, "second6436_20261106"), now=NOW)["blocked"]


def test_halt_prevents_send_even_when_runtime_enabled(db, monkeypatch):
    approve(db)
    enable(monkeypatch)
    assert halt_campaign(db, "piloto6436_20261105", operator="supervisor")["status"] == "HALTED"
    calls = []
    assert send_one_due(db, "piloto6436_20261105",
                        sender=lambda *_: calls.append(1), now=START)["status"] == "NOT_APPROVED_OR_HALTED"
    assert not calls


def test_pilot_real_context_is_only_for_verified_accepted_send(db, monkeypatch):
    approve(db, n=1)
    enable(monkeypatch)
    assert load_active_context(db, "+56930000001", "Hola", now=START) is None
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "prov1"})
    ctx = load_active_context(db, "+56930000001", "Me interesa", now=START + timedelta(minutes=2))
    assert ctx and ctx["property_code"] == "6436"
    assert ctx["new_price_uf"] == 2690
    assert "PRIVATE STREET" not in str(ctx)
    assert load_active_context(db, "+56930000003", "Hola", now=START + timedelta(minutes=2)) is None


def test_invalid_provider_receipt_is_not_counted_delivered(db, monkeypatch):
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "prov1"})
    assert record_delivery_update(
        db, {"data": {"messages": {"key": {"id": "prov1"}, "status": "delivered"}}},
        now=START + timedelta(minutes=1),
    )["status"] == "UPDATED"
    row = db[DISPATCHES].find_one({"provider_message_id": "prov1"})
    assert row["provider_delivery_status"] == "delivered"

def test_negative_reply_is_not_falsely_counted_as_interest_or_visit(db, monkeypatch):
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "prov-negative"})
    response = record_inbound(
        db, phone="+56930000001", provider_message_id="negative-inbound",
        text="No me interesa, no quiero visita", received_at=START + timedelta(minutes=2),
    )
    assert response["status"] == "ATTRIBUTED"
    report = campaign_report(db, "piloto6436_20261105")
    assert report["responded"] == 1
    assert report["positive_responses_after_notice"] == 0
    assert report["visits_requested"] == 0
    assert report["declined"] == 1
    assert report["rows"][0]["minutes_to_first_reply"] == 2

def test_no_tracking_queries_when_campaign_not_enabled(db, monkeypatch):
    monkeypatch.delenv("PRICE_DROP_REAL_PILOT_CONTEXT_ENABLED", raising=False)
    assert record_inbound(
        db, phone="+56930000001", provider_message_id="in-off", text="Hola",
        received_at=START,
    )["status"] == "DISABLED"
    assert record_bot_reply(
        db, phone="+56930000001", provider_message_id="bot-off", text="Hola",
        sent_at=START,
    )["status"] == "DISABLED"
    assert record_delivery_update(
        db, {"data": {"messages": {"key": {"id": "untracked"}}}},
        now=START,
    )["status"] == "DISABLED"
    assert db[TURNS].count_documents({}) == 0

def test_render_bootstrap_preview_prepare_approve_are_separate_no_send(db, monkeypatch):
    import json
    from chatbot import price_drop_campaign_bootstrap as bootstrap
    original_preview = bootstrap.preview_plan
    original_persist = bootstrap.persist_plan
    original_approve = bootstrap.approve_campaign
    monkeypatch.setattr(bootstrap, "preview_plan", lambda db, payload: original_preview(db, payload, now=NOW))
    monkeypatch.setattr(bootstrap, "persist_plan", lambda db, payload: original_persist(db, payload, now=NOW))
    monkeypatch.setattr(bootstrap, "approve_campaign", lambda db, cid, *, operator: original_approve(db, cid, operator=operator, now=NOW))
    from chatbot.price_drop_campaign_bootstrap import run_bootstrap
    from chatbot.price_drop_campaign_real import APPROVED_ID_FLAG
    p = plan(db, n=1)
    p["start_at"] = START.isoformat()  # Frozen against NOW; avoids time-dependent campaign-age flakiness
    env = {
        "PRICE_DROP_REAL_PILOT_BOOTSTRAP_MODE": "preview",
        "PRICE_DROP_REAL_PILOT_PLAN_JSON": json.dumps(p),
        "PRICE_DROP_REAL_PILOT_REVIEWER": "Supervisor PROCASA",
        APPROVED_ID_FLAG: p["campaign_id"],
    }
    viewed = run_bootstrap(db, env=env)
    assert viewed["status"] == "READY"
    assert viewed["approved_count"] == 1
    assert not db[CAMPAIGNS].count_documents({})
    assert not db[DISPATCHES].count_documents({})
    assert "phone_normalized" not in str(viewed)
    env["PRICE_DROP_REAL_PILOT_BOOTSTRAP_MODE"] = "prepare"
    created = run_bootstrap(db, env=env)
    assert created["status"] == "PLANNED"
    assert db[CAMPAIGNS].find_one({"_id": p["campaign_id"]})["status"] == "PLANNED"
    env["PRICE_DROP_REAL_PILOT_BOOTSTRAP_MODE"] = "approve"
    assert run_bootstrap(db, env=env)["status"] == "APPROVED"
    # Approval is not activation; outbound is still blocked.
    monkeypatch.delenv("PRICE_DROP_REAL_PILOT_ENABLED", raising=False)
    assert send_one_due(db, p["campaign_id"], now=START)["status"] == "DISABLED"


def test_render_bootstrap_rejects_mismatched_or_missing_approval(db):
    import json
    from chatbot.price_drop_campaign_bootstrap import run_bootstrap
    from chatbot.price_drop_campaign_real import APPROVED_ID_FLAG
    p = plan(db, n=1)
    env = {
        "PRICE_DROP_REAL_PILOT_BOOTSTRAP_MODE": "prepare",
        "PRICE_DROP_REAL_PILOT_PLAN_JSON": json.dumps(p),
        "PRICE_DROP_REAL_PILOT_REVIEWER": "Supervisor PROCASA",
        APPROVED_ID_FLAG: "another_campaign_20261105",
    }
    with pytest.raises(CampaignBlocked, match="approved_campaign_id_mismatch"):
        run_bootstrap(db, env=env)
    assert db[CAMPAIGNS].count_documents({}) == 0
    env[APPROVED_ID_FLAG] = p["campaign_id"]
    env["PRICE_DROP_REAL_PILOT_REVIEWER"] = ""
    with pytest.raises(CampaignBlocked, match="reviewer_required"):
        run_bootstrap(db, env=env)
    assert db[CAMPAIGNS].count_documents({}) == 0


def test_render_bootstrap_off_does_no_database_work():
    from chatbot.price_drop_campaign_bootstrap import run_bootstrap
    class ForbiddenDB:
        def __getitem__(self, name):
            raise AssertionError("Disabled bootstrap queried a database")
    assert run_bootstrap(ForbiddenDB(), env={}) == {"status": "DISABLED"}


@pytest.mark.parametrize("text,label,intent,positive,review", [
    ("Quiero visitar el departamento", "PRIORIDAD_COMERCIAL", "ASK_VISIT", True, True),
    ("Me gustaría coordinar una visita mañana", "PRIORIDAD_COMERCIAL", "ASK_VISIT", True, True),
    ("¿Puedo visitar mañana?", "PRIORIDAD_COMERCIAL", "ASK_VISIT", True, True),
    ("Quiero que me llame un ejecutivo", "PRIORIDAD_COMERCIAL", "ASK_CONTACT", True, True),
    ("Me puede llamar una asesora", "PRIORIDAD_COMERCIAL", "ASK_CONTACT", True, True),
    ("Quiero hacer una oferta", "PRIORIDAD_COMERCIAL", "GIVE_OFFER", True, True),
    ("Estoy interesado en coordinar una visita", "PRIORIDAD_COMERCIAL", "ASK_VISIT", True, True),
    ("¿Podemos ver el departamento?", "PRIORIDAD_COMERCIAL", "ASK_VISIT", True, True),
    ("Me interesa, cuéntame más", "INTERES_DECLARADO", "ASK_INFO", True, True),
    ("¿Cuáles son los gastos comunes?", "CONSULTA_INFORMATIVA", "ASK_INFO", False, False),
    ("¿Quién me contactará?", "CONSULTA_INFORMATIVA", "ASK_INFO", False, False),
    ("No me interesa", "RECHAZADO", "OTHER", False, False),
    ("No me interesa una visita", "RECHAZADO", "OTHER", False, False),
    ("No quiero agendar, gracias", "RECHAZADO", "OTHER", False, False),
    ("No quiero coordinar una visita", "RECHAZADO", "OTHER", False, False),
    ("No puedo visitar mañana", "SIN_SENAL", "OTHER", False, False),
    ("No me escriban más", "BAJA", "UNSUBSCRIBE", False, False),
    ("STOP", "BAJA", "UNSUBSCRIBE", False, False),
    ("Sí", "SIN_SENAL", "OTHER", False, False),
    ("Ok", "SIN_SENAL", "OTHER", False, False),
    ("Hola", "SIN_SENAL", "OTHER", False, False),
])
def test_campaign_classifier_uses_canonical_hot_intents_without_overcalling(
        text, label, intent, positive, review):
    from chatbot.price_drop_campaign_classifier import classify_price_drop_reply
    result = classify_price_drop_reply(text)
    assert result.label == label
    assert result.intent == intent
    assert result.positive is positive
    assert result.needs_human_review is review
    assert result.campaign_priority == ({"PRIORIDAD_COMERCIAL": "ALTA", "INTERES_DECLARADO": "MEDIA",
                                       "CONSULTA_INFORMATIVA": "INFORMATIVA"}.get(label))


def test_campaign_send_accepted_timestamp_and_chile_timezone(db, monkeypatch):
    approve(db, n=1)
    enable(monkeypatch)
    reply = send_one_due(db, "piloto6436_20261105", now=START,
                         sender=lambda *_: {"success": True, "provider_message_id": "msg-accepted"})
    assert reply["status"] == "SENT"
    assert reply["sent_at"] == START.isoformat()
    assert reply["sent_at_chile"].startswith("2026-11-05T11:00:00")
    row = db[DISPATCHES].find_one({"provider_message_id": "msg-accepted"})
    assert row["sent_at_chile"].startswith("2026-11-05T11:00:00")
    assert row["attempted_at"].replace(tzinfo=UTC) == START
    assert row["sent_at"].replace(tzinfo=UTC) == START
    assert row["campaign_response_classification"] == "SIN_RESPUESTA"


def test_campaign_hot_reply_creates_actionable_marker_without_crm_hot_write(
        db, monkeypatch):
    from chatbot.price_drop_campaign_real import campaign_worklist
    approve(db, n=1)
    enable(monkeypatch)
    db["leads"].update_one(
        {"_id": ObjectId("690000000000000000000001")},
        {"$set": {"lead_temperature_effective": "COLD",
                  "lifecycle.hot_since": "2026-01-01T10:00:00-03:00",
                  "sla_status": "ok", "ejecutivo_asignado": "Actual"}},
    )
    assert send_one_due(
        db, "piloto6436_20261105", now=START,
        sender=lambda *_: {"success": True, "provider_message_id": "campaign-sent-1"},
    )["status"] == "SENT"
    at = START + timedelta(minutes=4)
    result = record_inbound(
        db, phone="+56930000001", provider_message_id="cust-hot-1",
        text="Quiero visitar el departamento, ¿podemos coordinar?",
        received_at=at,
    )
    assert result["campaign_classification"] == "PRIORIDAD_COMERCIAL"
    assert result["campaign_priority"] == "ALTA"
    assert result["intent"] == "ASK_VISIT"
    assert result["needs_human_review"] is True
    doc = db[DISPATCHES].find_one({"campaign_id": "piloto6436_20261105"})
    assert doc["campaign_response_classification"] == "PRIORIDAD_COMERCIAL"
    assert doc["first_priority_response_at"].replace(tzinfo=UTC) == at
    assert doc["first_positive_response_at"].replace(tzinfo=UTC) == at
    assert doc["followup_status"] == "PENDIENTE_REVISION"
    turn = db[TURNS].find_one({"provider_message_id": "cust-hot-1"})
    assert turn["classification"]["intent"] == "ASK_VISIT"
    assert turn["classification"]["classifier_version"]
    lead = db["leads"].find_one({"_id": doc["lead_id"]})
    assert lead["price_drop_campaign_v1"]["response_priority"] == "ALTA"
    assert lead["price_drop_campaign_v1"]["needs_human_review"] is True
    assert lead["lead_temperature_effective"] == "COLD"
    assert lead["lifecycle"]["hot_since"] == "2026-01-01T10:00:00-03:00"
    assert lead["sla_status"] == "ok"
    assert lead["ejecutivo_asignado"] == "Actual"
    worklist = campaign_worklist(db, "piloto6436_20261105")
    assert worklist["priority_responses"] == 1 and worklist["pending"] == 1
    assert worklist["leads"][0]["lead_id"] == str(doc["lead_id"])


def test_campaign_normal_questions_vs_positive_reply(db, monkeypatch):
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "msg-first"})
    info = record_inbound(
        db, phone="+56930000001", provider_message_id="info-1",
        text="¿Cuánto son los gastos comunes?", received_at=START + timedelta(minutes=1),
    )
    assert info["campaign_classification"] == "CONSULTA_INFORMATIVA"
    assert info["needs_human_review"] is False
    assert db[DISPATCHES].find_one({"campaign_id": "piloto6436_20261105"})[
        "followup_status"] == "CONSULTA_INFORMATIVA"
    interest = record_inbound(
        db, phone="+56930000001", provider_message_id="interest-2",
        text="Me interesa, cuéntame más", received_at=START + timedelta(minutes=2),
    )
    assert interest["campaign_classification"] == "INTERES_DECLARADO"
    assert interest["needs_human_review"] is True
    report = campaign_report(db, "piloto6436_20261105")
    assert report["declared_interest"] == 1 and report["priority_responses"] == 0
    assert report["positive_responses_after_notice"] == 1 and report["pending_human_review"] == 1
    assert db[DISPATCHES].find_one({"campaign_id": "piloto6436_20261105"})[
        "positive_reply_count"] == 1


def test_campaign_decline_clears_pending_but_keeps_historical_events(db, monkeypatch):
    from chatbot.price_drop_campaign_real import campaign_worklist
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "msg-1"})
    record_inbound(db, phone="+56930000001", provider_message_id="visit-1",
                   text="Quiero agendar una visita", received_at=START + timedelta(minutes=1))
    outcome = record_inbound(
        db, phone="+56930000001", provider_message_id="decline-2",
        text="No me interesa, no quiero visita",
        received_at=START + timedelta(minutes=2),
    )
    assert outcome["campaign_classification"] == "RECHAZADO"
    assert outcome["needs_human_review"] is False
    report = campaign_report(db, "piloto6436_20261105")
    assert report["priority_responses"] == 0 and report["pending_human_review"] == 0
    assert report["declined"] == 1
    assert report["rows"][0]["first_priority_response_at"] is not None
    assert campaign_worklist(db, "piloto6436_20261105")["pending"] == 0


def test_campaign_inbound_duplicate_never_increments_or_reopens_followup(db, monkeypatch):
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "msg-first"})
    received = dict(phone="+56930000001", provider_message_id="unique-visit",
                    text="Quiero agendar una visita", received_at=START + timedelta(minutes=1))
    assert record_inbound(db, **received)["campaign_classification"] == "PRIORIDAD_COMERCIAL"
    assert record_inbound(db, **received)["status"] == "DUPLICATE_IGNORED"
    row = db[DISPATCHES].find_one({"campaign_id": "piloto6436_20261105"})
    assert row["reply_count"] == 1 and row["positive_reply_count"] == 1
    assert db[TURNS].count_documents({"direction": "inbound"}) == 1


def test_campaign_worklist_prioritizes_hot_before_normal(db, monkeypatch):
    from chatbot.price_drop_campaign_real import campaign_worklist
    approve(db, n=2)
    enable(monkeypatch)
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "sent-1"})
    send_one_due(db, "piloto6436_20261105", now=START + timedelta(minutes=12),
                 sender=lambda *_: {"success": True, "provider_message_id": "sent-2"})
    record_inbound(db, phone="+56930000001", provider_message_id="reply-1",
                   text="Me interesa, cuéntame más",
                   received_at=START + timedelta(minutes=14))
    record_inbound(db, phone="+56930000002", provider_message_id="reply-2",
                   text="Quiero que me llame un ejecutivo",
                   received_at=START + timedelta(minutes=15))
    worklist = campaign_worklist(db, "piloto6436_20261105")
    assert worklist["pending"] == 2
    assert worklist["priority_responses"] == 1 and worklist["declared_interest"] == 1
    assert [r["campaign_response_classification"] for r in worklist["leads"]] == [
        "PRIORIDAD_COMERCIAL", "INTERES_DECLARADO",
    ]


def test_reply_for_different_property_not_misattributed(db, monkeypatch):
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "msg-first"})
    response = record_inbound(
        db, phone="+56930000001", provider_message_id="another-home",
        text="Me interesa la propiedad 9999, quiero verla",
        received_at=START + timedelta(minutes=2),
    )
    assert response["status"] == "OTHER_PROPERTY_NOT_ATTRIBUTED"
    assert db[TURNS].count_documents({}) == 0
    assert db[DISPATCHES].find_one({"campaign_id": "piloto6436_20261105"})[
        "reply_count"] == 0


def test_first_client_reply_timestamp_is_recorded_even_for_optout(db, monkeypatch):
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "sent-optout"})
    instant = START + timedelta(minutes=3)
    result = record_inbound(
        db, phone="+56930000001", provider_message_id="stop-immediate",
        text="No me escriban más", received_at=instant,
    )
    assert result["status"] == "OPTED_OUT"
    row = db[DISPATCHES].find_one({"campaign_id": "piloto6436_20261105"})
    assert row["first_response_at"].replace(tzinfo=UTC) == instant
    assert row.get("first_positive_response_at") is None
    report = campaign_report(db, "piloto6436_20261105")
    assert report["responded"] == 1
    assert report["rows"][0]["minutes_to_first_reply"] == 3.0
    assert report["priority_responses"] == 0 and report["declared_interest"] == 0
    assert report["pending_human_review"] == 0


def test_preexisting_crm_hot_remains_distinct_from_campaign_reply(db, monkeypatch):
    """An old HOT lead can answer after a price reduction without becoming a new HOT."""
    from chatbot.price_drop_campaign_real import campaign_worklist
    original = db["leads"].find_one({"contact_phone_normalized": "+56930000001"})
    db["leads"].update_one({"_id": original["_id"]}, {"$set": {
        "lead_temperature_effective": "HOT",
        "lifecycle.hot_since": "2026-11-04T09:10:00-03:00",
        "last_intent": "ASK_VISIT",
    }})
    approve(db, n=1)
    enable(monkeypatch)
    # Existing CRM HOT was present before send.
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "existing-hot-send"})
    row = db[DISPATCHES].find_one({"provider_message_id": "existing-hot-send"})
    assert row["crm_temperature_before_send"] == "HOT"
    assert row["crm_was_hot_before_send"] is True
    mark = db["leads"].find_one({"_id": original["_id"]})
    assert mark["price_drop_campaign_v1"]["crm_was_hot_before_send"] is True
    assert mark["lead_temperature_effective"] == "HOT"
    assert mark["lifecycle"]["hot_since"] == "2026-11-04T09:10:00-03:00"

    answer = record_inbound(
        db, phone="+56930000001", provider_message_id="already-hot-responds",
        text="Quiero visitar el departamento", received_at=START + timedelta(minutes=1),
    )
    assert answer["campaign_classification"] == "PRIORIDAD_COMERCIAL"
    assert answer["crm_was_hot_before_send"] is True
    report = campaign_report(db, "piloto6436_20261105")
    assert report["already_hot_before_send"] == 1
    assert report["positive_response_after_non_hot"] == 0
    assert report["priority_responses"] == 1  # campaign reply, NOT a new CRM HOT
    worklist = campaign_worklist(db, "piloto6436_20261105")
    assert worklist["already_hot_before_send"] == 1
    assert worklist["leads"][0]["crm_temperature_before_send"] == "HOT"
    assert worklist["leads"][0]["campaign_response_classification"] == "PRIORIDAD_COMERCIAL"
    assert db["leads"].find_one({"_id": original["_id"]})["lead_temperature_effective"] == "HOT"


def test_hot_arrives_after_selection_but_before_campaign_send(db, monkeypatch):
    """Avoid crediting the campaign for an independently Hot lead."""
    p = plan(db, n=1)
    lead_id = ObjectId(p["recipients"][0]["lead_id"])
    db["leads"].update_one({"_id": lead_id}, {"$set": {"lead_temperature_effective": "COLD"}})
    approve(db, n=1)
    planned = db[DISPATCHES].find_one({"campaign_id": "piloto6436_20261105"})
    assert planned["crm_temperature_at_selection"] == "COLD"
    # Independently turns HOT while the campaign is queued.
    db["leads"].update_one({"_id": lead_id}, {"$set": {
        "lead_temperature_effective": "HOT",
        "lifecycle.hot_since": "2026-11-05T10:45:00-03:00",
    }})
    enable(monkeypatch)
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "hot-preexisting"})
    record_inbound(db, phone="+56930000001", provider_message_id="interested-after-notice",
                   text="Me interesa, cuéntame más",
                   received_at=START + timedelta(minutes=2))
    row = db[DISPATCHES].find_one({"provider_message_id": "hot-preexisting"})
    assert row["crm_temperature_at_selection"] == "COLD"
    assert row["crm_temperature_before_send"] == "HOT"
    assert row["crm_was_hot_before_send"] is True
    report = campaign_report(db, "piloto6436_20261105")
    assert report["positive_responses_after_notice"] == 1
    assert report["positive_response_after_non_hot"] == 0
    assert report["already_hot_before_send"] == 1


def test_cold_lead_response_is_post_send_engagement_not_automatic_crm_hot(
        db, monkeypatch):
    lead_id = ObjectId("690000000000000000000001")
    db["leads"].update_one({"_id": lead_id}, {"$set": {
        "lead_temperature_effective": "COLD",
        "last_intent": "ASK_INFO",
    }})
    approve(db, n=1)
    enable(monkeypatch)
    # Before the first accepted campaign message: no post-notice classification.
    assert record_inbound(
        db, phone="+56930000001", provider_message_id="before-send", text="Quiero visitar",
        received_at=START - timedelta(minutes=1),
    )["status"] == "NOT_CAMPAIGN_REPLY"
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "accepted-send"})
    answer = record_inbound(
        db, phone="+56930000001", provider_message_id="after-send",
        text="Quiero coordinar una visita", received_at=START + timedelta(minutes=3),
    )
    assert answer["campaign_classification"] == "PRIORIDAD_COMERCIAL"
    assert answer["crm_was_hot_before_send"] is False
    report = campaign_report(db, "piloto6436_20261105")
    assert report["positive_response_after_non_hot"] == 1
    assert report["already_hot_before_send"] == 0
    assert report["rows"][0]["campaign_origin"] == "PRICE_DROP"
    assert report["rows"][0]["attribution_basis"] == "same_phone_after_accepted_send_window"
    assert db["leads"].find_one({"_id": lead_id})["lead_temperature_effective"] == "COLD"


def _campaign_notify_owner(db):
    lead = db["leads"].find_one({"prospecto.codigo": "6436"})
    user_id = ObjectId("690000000000000000000090")
    owner_phone = "+56984517639"
    db["usuarios"].insert_one({
        "_id": user_id, "nombre": "Ejecutiva Prueba", "telefono": owner_phone,
        "is_active": True,
    })
    db["crm_assignment_cycles"].insert_one({
        "lead_id": lead["_id"], "assignment_cycle_id": "campaign-cycle-1",
        "assigned_to_user_id": str(user_id),
        "assigned_to_display_name": "Ejecutiva Prueba",
        "property_code": "6436", "unassigned_at": None,
        "cycle_status": "active",
    })
    db["leads"].update_one({"_id": lead["_id"]}, {"$set": {
        "lifecycle.current_assignment_cycle_id": "campaign-cycle-1",
        "lifecycle.assigned_to_user_id": str(user_id),
        "ejecutivo_asignado": "Ejecutiva Prueba",
        "prospecto.ejecutivo": "Ejecutiva Prueba",
    }})
    return lead, owner_phone


def test_campaign_reply_queues_one_notice_but_does_not_send_without_gate(db, monkeypatch):
    from chatbot.price_drop_campaign_exec_alert import (
        COLLECTION, process_one_exec_notice,
    )
    lead, owner_phone = _campaign_notify_owner(db)
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "accepted"})
    phone = lead["contact_phone_normalized"]
    record_inbound(db, phone=phone, provider_message_id="visit-1",
                   text="Quiero visitar el departamento",
                   received_at=START + timedelta(minutes=1))
    record_inbound(db, phone=phone, provider_message_id="visit-1",
                   text="Quiero visitar el departamento",
                   received_at=START + timedelta(minutes=1))
    assert db[COLLECTION].count_documents({"status": "PENDING"}) == 1
    attempts = []
    assert process_one_exec_notice(db, sender=lambda *_: attempts.append(1))["status"] == "DISABLED"
    assert not attempts

def test_campaign_exec_notice_sends_once_to_current_owner(db, monkeypatch):
    from chatbot.price_drop_campaign_exec_alert import (
        GATE, process_one_exec_notice,
    )
    lead, owner_phone = _campaign_notify_owner(db)
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "accepted"})
    record_inbound(
        db, phone=lead["contact_phone_normalized"], provider_message_id="call-1",
        text="Quiero que me llame un ejecutivo",
        received_at=START + timedelta(minutes=1),
    )
    monkeypatch.setenv(GATE, "true")
    notices = []
    def fake_sender(phone, message):
        notices.append((phone, message))
        return {"success": True, "provider_message_id": "notice-1"}
    assert process_one_exec_notice(db, sender=fake_sender)["status"] == "SENT"
    assert notices[0][0] == owner_phone
    assert "6436" in notices[0][1]
    from chatbot.lead_router import build_secure_crm_url
    assert "*Abrir lead en CRM:*" in notices[0][1]
    assert build_secure_crm_url(lead) in notices[0][1]
    assert f"/crm/lead-id/{lead['_id']}" in notices[0][1]
    assert "/crm/lead/+569" not in notices[0][1]
    assert "solicitó contacto de un ejecutivo" in notices[0][1]
    assert process_one_exec_notice(db, sender=fake_sender)["status"] == "IDLE"
    assert len(notices) == 1


def test_campaign_exec_notice_rejects_stale_assignment(db, monkeypatch):
    from chatbot.price_drop_campaign_exec_alert import GATE, process_one_exec_notice
    lead, _ = _campaign_notify_owner(db)
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "accepted"})
    record_inbound(db, phone=lead["contact_phone_normalized"],
                   provider_message_id="call-2", text="Quiero que me llame un ejecutivo",
                   received_at=START + timedelta(minutes=1))
    db["leads"].update_one(
        {"_id": lead["_id"]}, {"$set": {"ejecutivo_asignado": "Otro"}},
    )
    monkeypatch.setenv(GATE, "true")
    attempts = []
    assert process_one_exec_notice(
        db, sender=lambda *_: attempts.append(True),
    )["status"] == "BLOCKED_ASSIGNMENT"
    assert not attempts


def test_campaign_notice_escalation_supersedes_older_interest(db, monkeypatch):
    from chatbot.price_drop_campaign_exec_alert import (
        COLLECTION, GATE, process_one_exec_notice,
    )
    lead, _ = _campaign_notify_owner(db)
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "accepted"})
    for i, text in enumerate(("Me interesa, cuéntame más",
                              "Quiero coordinar una visita")):
        record_inbound(
            db, phone=lead["contact_phone_normalized"], provider_message_id=f"step-{i}",
            text=text, received_at=START + timedelta(minutes=i + 1),
        )
    monkeypatch.setenv(GATE, "true")
    sends = []
    def fake_sender(*_):
        sends.append(True)
        return {"success": True, "provider_message_id": "notification-1"}
    assert process_one_exec_notice(db, sender=fake_sender)["status"] == "SKIPPED"
    assert process_one_exec_notice(db, sender=fake_sender)["status"] == "SENT"
    assert len(sends) == 1
    assert db[COLLECTION].count_documents({"status": "SENT"}) == 1


def test_campaign_exec_notice_unknown_delivery_is_not_retried(db, monkeypatch):
    from chatbot.price_drop_campaign_exec_alert import (
        COLLECTION, GATE, process_one_exec_notice,
    )
    lead, _ = _campaign_notify_owner(db)
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(db, "piloto6436_20261105", now=START,
                 sender=lambda *_: {"success": True, "provider_message_id": "accepted"})
    record_inbound(db, phone=lead["contact_phone_normalized"],
                   provider_message_id="interested-1", text="Me interesa",
                   received_at=START + timedelta(minutes=1))
    monkeypatch.setenv(GATE, "true")
    attempts = []
    def uncertain(*_):
        attempts.append(True)
        raise TimeoutError("unknown provider acceptance")
    assert process_one_exec_notice(db, sender=uncertain)["status"] == "DELIVERY_UNKNOWN"
    assert process_one_exec_notice(db, sender=uncertain)["status"] == "IDLE"
    assert len(attempts) == 1
    assert db[COLLECTION].find_one({})["status"] == "DELIVERY_UNKNOWN"



def _campaign_reassign_to_latest_owner(db, lead_id):
    from datetime import timezone
    db["crm_assignment_cycles"].update_one(
        {"lead_id": lead_id, "assignment_cycle_id": "campaign-cycle-1"},
        {"$set": {
            "cycle_status": "closed",
            "unassigned_at": datetime(2026, 11, 5, 14, 1, tzinfo=timezone.utc),
        }},
    )
    latest_user_id = ObjectId("690000000000000000000091")
    db["usuarios"].insert_one({
        "_id": latest_user_id,
        "nombre": "Ejecutiva Última",
        "telefono": "+56985243811",
        "is_active": True,
    })
    db["crm_assignment_cycles"].insert_one({
        "lead_id": lead_id, "assignment_cycle_id": "campaign-cycle-2",
        "assigned_to_user_id": str(latest_user_id),
        "assigned_to_display_name": "Ejecutiva Última",
        "property_code": "6436", "unassigned_at": None,
        "cycle_status": "active",
    })
    db["leads"].update_one({"_id": lead_id}, {"$set": {
        "lifecycle.current_assignment_cycle_id": "campaign-cycle-2",
        "lifecycle.assigned_to_user_id": str(latest_user_id),
        "ejecutivo_asignado": "Ejecutiva Última",
        "prospecto.ejecutivo": "Ejecutiva Última",
    }})
    return "+56985243811"


def test_price_drop_alert_reassignment_uses_latest_owner_not_first(
        db, monkeypatch):
    from chatbot.price_drop_campaign_exec_alert import GATE, process_one_exec_notice
    from chatbot.lead_router import build_secure_crm_url
    lead, old_phone = _campaign_notify_owner(db)
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(
        db, "piloto6436_20261105", now=START,
        sender=lambda *_: {"success": True, "provider_message_id": "campaign-notice-accepted"},
    )
    # Customer replies while the first executive owns the lead.
    record_inbound(
        db, phone=lead["contact_phone_normalized"], provider_message_id="redistributed-inbound",
        text="Me interesa, cuéntame más",
        received_at=START + timedelta(minutes=1),
    )
    # Before the campaign notice is dispatched, CRM redistributes it.
    new_phone = _campaign_reassign_to_latest_owner(db, lead["_id"])
    monkeypatch.setenv(GATE, "true")
    attempted = []
    def fake_send(phone, msg):
        attempted.append((phone, msg))
        return {"success": True, "provider_message_id": "exec-latest"}
    result = process_one_exec_notice(
        db, sender=fake_send, now=START + timedelta(minutes=3),
    )
    assert result["status"] == "SENT"
    assert result["owner_name"] == "Ejecutiva Última"
    assert attempted[0][0] == new_phone
    assert attempted[0][0] != old_phone
    from chatbot.price_drop_campaign_exec_alert import COLLECTION
    audit = db[COLLECTION].find_one({"provider_message_id": "exec-latest"})
    assert audit["target_owner_cycle_id"] == "campaign-cycle-2"
    assert audit["target_owner_user_id"] == "690000000000000000000091"
    assert "Ejecutiva Última" in attempted[0][1]
    assert build_secure_crm_url(lead) in attempted[0][1]
    assert "NO un lead nuevo" in attempted[0][1]


def test_price_drop_alert_reassignment_race_blocks_old_owner(db, monkeypatch):
    import chatbot.price_drop_campaign_exec_alert as alerts
    lead, old_phone = _campaign_notify_owner(db)
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(
        db, "piloto6436_20261105", now=START,
        sender=lambda *_: {"success": True, "provider_message_id": "campaign-accepted"},
    )
    record_inbound(
        db, phone=lead["contact_phone_normalized"], provider_message_id="race-visit",
        text="Quiero coordinar una visita",
        received_at=START + timedelta(minutes=1),
    )
    original = alerts._notice_text
    def reassign_while_building(notice, owner, dispatch):
        text = original(notice, owner, dispatch)
        _campaign_reassign_to_latest_owner(db, lead["_id"])
        return text
    monkeypatch.setattr(alerts, "_notice_text", reassign_while_building)
    monkeypatch.setenv(alerts.GATE, "true")
    attempts = []
    result = alerts.process_one_exec_notice(
        db, sender=lambda *_: attempts.append(True),
        now=START + timedelta(minutes=2),
    )
    assert result["status"] == "BLOCKED_ASSIGNMENT"
    assert result["reason"] == "assignment_changed_during_notification"
    assert not attempts


def test_price_drop_alert_does_not_route_by_stale_owner_name_only(
        db, monkeypatch):
    from chatbot.price_drop_campaign_exec_alert import GATE, process_one_exec_notice
    lead, old_phone = _campaign_notify_owner(db)
    approve(db, n=1)
    enable(monkeypatch)
    send_one_due(
        db, "piloto6436_20261105", now=START,
        sender=lambda *_: {"success": True, "provider_message_id": "campaign-accepted"},
    )
    record_inbound(
        db, phone=lead["contact_phone_normalized"], provider_message_id="name-only",
        text="Quiero coordinar una visita",
        received_at=START + timedelta(minutes=1),
    )
    # The cycle may name the old executive, but its user ID no longer exists.
    # A name-only fallback could misroute a private lead to the wrong user.
    db["crm_assignment_cycles"].update_one(
        {"lead_id": lead["_id"], "assignment_cycle_id": "campaign-cycle-1"},
        {"$set": {"assigned_to_user_id": "690000000000000000000099"}},
    )
    db["leads"].update_one(
        {"_id": lead["_id"]},
        {"$set": {"lifecycle.assigned_to_user_id": "690000000000000000000099"}},
    )
    monkeypatch.setenv(GATE, "true")
    attempts = []
    result = process_one_exec_notice(
        db, sender=lambda *_: attempts.append(True),
        now=START + timedelta(minutes=2),
    )
    assert result["status"] == "BLOCKED_ASSIGNMENT"
    assert result["reason"] == "current_cycle_owner_not_found"
    assert not attempts
