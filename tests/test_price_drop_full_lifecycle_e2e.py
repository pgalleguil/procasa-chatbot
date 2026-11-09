"""Sequential price-drop lifecycle: initial inquiry -> silence -> audited change
-> historical lead eligibility -> simulated provider -> conversation. No real sends.
"""
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from bson import ObjectId

from chatbot import price_drop_leads_v2 as v2
from chatbot.customer_response_semantics import build_specific_property_response
from chatbot.price_drop_v2_customer_response import stabilize_v2_reply
from chatbot.public_property_facts import public_property_summary

PHONE = "+56930001111"  # synthetic, never send to the provider
LEAD = ObjectId("690000000000000000000666")
BEFORE = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
DROP = "2026-10-14T11:52:43+00:00"
SEND = datetime(2026, 11, 6, 14, 0, tzinfo=timezone.utc)
REAL_CAMPAIGN = "pilot_6436_lifecycle_real"
QA_CAMPAIGN = "test_leads_6436_lifecycle_qa"


@pytest.fixture
def db(monkeypatch):
    for field in (v2.SEND_FLAG, v2.CONTEXT_FLAG, v2.APPROVED_ID_FLAG,
                  v2.EXEC_NOTICE_ENABLED_FLAG):
        monkeypatch.delenv(field, raising=False)
    monkeypatch.setenv(v2.TEST_PHONE_FLAG, PHONE)
    monkeypatch.setenv(v2.ADMIN_PHONE_FLAG, v2.APPROVED_ADMIN_PHONE)
    d = mongomock.MongoClient()["URLS"]
    d["universo_cartera_prop360"].insert_one({
        "codigo": "6436", "disponible_prop360": True,
        "estado": {"oficina": "PROCASA SUCRE"},
        "ubicacion": {"comuna": "San Miguel", "calle": "PRIVATE STREET 123"},
        "datos_propietario": {"nombre": "PRIVATE OWNER",
                              "telefono": "+56999999999"},
        "tipo_operacion": {"tipo": "Departamento", "venta": True,
                           "precio_venta": {"precio_uf": 2790},
                           "gastos_comunes": 85000},
        "caracteristicas": {
            "dormitorios": 2, "banos": 2, "superficie_util": 60,
            "superficie_total": 65, "orientacion": "Sur-Poniente",
            "features": ["piscina", "has_grill"],
        },
    })
    # Simulates the initial new-lead conversation, not an invented
    # historical inquiry at the moment of the campaign.
    initial = "Hola, quiero conocer la propiedad código 6436 de San Miguel"
    d["leads"].insert_one({
        "_id": LEAD, "phone": PHONE, "lead_temperature_effective": "HOT",
        "pipeline_stage": "CONTACTED",
        "prospecto": {"codigo": "6436"},
        "messages": [{
            "role": "user", "content": initial,
            "timestamp": BEFORE.isoformat(),
            "metadata": {"property_code": "6436"},
        }],
    })
    return d


def _drop_price(db):
    db["universo_cartera_prop360"].update_one(
        {"codigo": "6436"},
        {"$set": {"tipo_operacion.precio_venta.precio_uf": 2690}},
    )
    db["universo_cartera_prop360_historial"].insert_one({
        "codigo": "6436", "campo": "precio_publicado",
        "fecha": DROP, "moneda_anterior": "UF", "moneda_nueva": "UF",
        "monto_anterior": 2790, "monto_nuevo": 2690,
    })


def _request(cid, test_mode):
    return dict(
        lead_id=str(LEAD), phone=PHONE, campaign_id=cid,
        property_code="6436", old_price_uf=2790, new_price_uf=2690,
        test_mode=test_mode, consent_reviewed=not test_mode,
        consent_basis="Documented explicit commercial contact authorization"
        if not test_mode else "",
    )


def test_new_lead_silence_audited_drop_hot_only_real_dispatch_locked(db, monkeypatch):
    # Initial consultation: full public property facts, no owner or address.
    prop = db["universo_cartera_prop360"].find_one({"codigo": "6436"})
    initial_reply = public_property_summary(prop)
    assert "UF 2.790" in initial_reply
    assert "Dormitorios: 2" in initial_reply
    assert "PRIVATE" not in initial_reply
    # Disengagement here means SILENCE; no STOP or opt-out.
    before = db["leads"].find_one({"_id": LEAD})
    assert len([x for x in before["messages"] if x["role"] == "user"]) == 1
    with pytest.raises(v2.PilotBlocked, match="property_price_or_availability_changed"):
        v2.preview_one(db, **_request(REAL_CAMPAIGN, False))
    _drop_price(db)
    # Audited lower price; prior inquiry, consent evidence and current HOT pass.
    preview = v2.preview_one(db, **_request(REAL_CAMPAIGN, False))
    assert preview["simulated_prior_inquiry"] is False
    assert "UF 2.790" in preview["message"]
    assert "UF 2.690" in preview["message"]
    assert v2.prepare_one(db, now=SEND, **_request(REAL_CAMPAIGN, False))["status"] == "PREPARED"
    assert v2.approve_one(
        db, lead_id=str(LEAD), campaign_id=REAL_CAMPAIGN,
        operator="QA Supervisor", now=SEND
    )["status"] == "APPROVED"
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, REAL_CAMPAIGN)
    calls = []
    outcome = v2.send_one(
        db, lead_id=str(LEAD), campaign_id=REAL_CAMPAIGN,
        sender=lambda *args: calls.append(args), now=SEND)
    assert outcome["status"] == "REAL_SEND_LOCKED_PENDING_ACCEPTANCE"
    assert not calls
    assert len(db["leads"].find_one({"_id": LEAD})["price_drop_v2"]["campaigns"]) == 1


def test_new_lead_silence_drop_qa_dispatch_delivered_and_conversation(db, monkeypatch):
    _drop_price(db)
    # Synthetic mode tests the actual send state machine with a fake provider;
    # no WhatsApp communication happens in this test.
    req = _request(QA_CAMPAIGN, True)
    assert v2.prepare_one(db, now=SEND, **req)["status"] == "PREPARED"
    assert v2.approve_one(db, lead_id=str(LEAD), campaign_id=QA_CAMPAIGN,
                          operator="QA Supervisor", now=SEND)["status"] == "APPROVED"
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, QA_CAMPAIGN)
    calls = []
    def fake_provider(phone, message):
        calls.append((phone, message))
        return {"success": True, "provider_message_id": "fake-6436-1",
                "delivery_status": "accepted"}
    assert v2.send_one(db, lead_id=str(LEAD), campaign_id=QA_CAMPAIGN,
                       sender=fake_provider, now=SEND)["status"] == "SENT"
    assert len(calls) == 1 and calls[0][0] == PHONE
    assert v2.send_one(db, lead_id=str(LEAD), campaign_id=QA_CAMPAIGN,
                       sender=fake_provider, now=SEND)["status"] == "NOT_APPROVED"
    assert len(calls) == 1
    # Provider acceptance alone does not count as delivery.
    campaign = db["leads"].find_one({"_id": LEAD})["price_drop_v2"]["campaigns"][0]
    assert campaign["provider_status"] == "accepted"
    assert v2.record_delivery_update(db, {"data": {"messages": {
        "key": {"id": "fake-6436-1"}, "status": "delivered",
    }}}, now=SEND+timedelta(seconds=5))["status"] == "UPDATED"
    ctx = v2.active_context(db, phone=PHONE, message="¿Cuánto bajó?",
                            now=SEND+timedelta(minutes=1))
    assert ctx["campaign_id"] == QA_CAMPAIGN
    prop = db["universo_cartera_prop360"].find_one({"codigo": "6436"})
    question = ("Me interesa, dame toda la información de la propiedad, "
                "¿cuánto bajó?, ¿se puede negociar?")
    answer = build_specific_property_response(
        question, prop, price_drop_context=ctx)
    for text in ("UF 2.690", "bajó 100 UF", "UF 2.790",
                 "$85.000", "Dormitorios: 2"):
        assert text in answer
    assert "PRIVATE" not in answer
    assert "+56999999999" not in answer
    visit = stabilize_v2_reply(
        "Quisiera visitarlo mañana a las 10 y saber sus gastos comunes",
        build_specific_property_response("¿Gastos comunes?", prop),
        context=ctx, visit_preference="mañana a las 10",
    )
    assert "$85.000" in visit and "mañana a las 10" in visit
    assert "todavía no está confirmada" in visit
    reply = v2.record_inbound(
        db, phone=PHONE, provider_message_id="fake-inbound-1",
        text="Me interesa el departamento", now=SEND+timedelta(minutes=2))
    assert reply["status"] == "ATTRIBUTED"
    assert v2.record_inbound(
        db, phone=PHONE, provider_message_id="fake-inbound-1",
        text="Me interesa el departamento", now=SEND+timedelta(minutes=2)
    )["status"] == "DUPLICATE_IGNORED"
    campaign = db["leads"].find_one({"_id": LEAD})["price_drop_v2"]["campaigns"][0]
    assert campaign["reply_count"] == 1
    assert campaign["provider_status"] == "delivered"
    assert campaign["notifications"]["administrator"]["status"] == "PENDING_SIMULATION"
    assert campaign["notifications"]["property_executive"]["status"] == "FROZEN_ADMIN_ONLY"
    assert set(db.list_collection_names()) == {
        "leads", "universo_cartera_prop360", "universo_cartera_prop360_historial"}


@pytest.mark.parametrize("bad_state", ["cold", "stop", "closed"])
def test_only_optouts_or_closed_are_excluded_from_historical_campaign(db, bad_state):
    _drop_price(db)
    change = {}
    if bad_state == "cold":
        change["lead_temperature_effective"] = "COLD"
    elif bad_state == "stop":
        change["conversation_status"] = "STOPPED_BY_CLIENT"
    else:
        change["pipeline_stage"] = "CLOSED_LOST"
    db["leads"].update_one({"_id": LEAD}, {"$set": change})
    if bad_state == "cold":
        assert v2.preview_one(db, **_request(REAL_CAMPAIGN, False))["test_mode"] is False
    else:
        with pytest.raises(v2.PilotBlocked, match="lead_closed_pipeline_stage|lead_opted_out"):
            v2.preview_one(db, **_request(REAL_CAMPAIGN, False))
    assert "price_drop_v2" not in db["leads"].find_one({"_id": LEAD})
