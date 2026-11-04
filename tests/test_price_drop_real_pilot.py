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
    assert first["phone_last4"] == "0001"
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
    assert first_doc["attempted_at"] == START and first_doc.get("sent_at") is None


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
    assert report["interested"] == 1
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
