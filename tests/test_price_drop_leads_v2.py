"""One-collection price-drop V2 acceptance tests. No provider calls or live clients."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from bson import ObjectId

from chatbot import price_drop_leads_v2 as v2

NOW = datetime(2026, 11, 5, 21, 0, tzinfo=timezone.utc)
PHONE = "+56983219804"
CAMPAIGN = "test_leads_6436_20261105"


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv(v2.TEST_PHONE_FLAG, PHONE)
    testdb = mongomock.MongoClient()["URLS"]
    testdb["universo_cartera_prop360"].insert_one({
        "codigo": "6436", "disponible_prop360": True,
        "estado": {"oficina": "PROCASA SUCRE", "ejecutivo": "Mariela Arriagada"},
        "tipo_operacion": {"venta": True, "precio_venta": {"precio_uf": 2690}},
    })
    testdb["universo_cartera_prop360_historial"].insert_one({
        "codigo": "6436", "campo": "precio_publicado",
        "fecha": "2026-10-14T11:52:43+00:00",
        "moneda_anterior": "UF", "moneda_nueva": "UF",
        "monto_anterior": 2790, "monto_nuevo": 2690,
    })
    testdb["leads"].insert_one({
        "_id": ObjectId("690000000000000000000777"),
        "phone": PHONE, "prospecto": {"codigo": "17184"},
        "messages": [{"role": "user", "content": "busco en Pudahuel",
                      "timestamp": "2026-10-01T10:00:00-03:00"}],
    })
    # Historical duplicate identity: test may use the explicit canonical
    # document; a real campaign must not accept this ambiguous phone.
    testdb["leads"].insert_one({
        "_id": ObjectId("690000000000000000000778"),
        "phone": "56983219804", "messages": [],
    })
    return testdb


def params(**overrides):
    p = dict(
        lead_id="690000000000000000000777", phone=PHONE,
        campaign_id=CAMPAIGN, property_code="6436",
        old_price_uf=2790, new_price_uf=2690,
        test_mode=True,
    )
    p.update(overrides)
    return p


def test_preview_verifies_historical_drop_without_any_write(db):
    r = v2.preview_one(db, **params())
    assert r["property_code"] == "6436"
    assert "UF 2.790" in r["message"] and "UF 2.690" in r["message"]
    assert r["simulated_prior_inquiry"] is True
    assert "price_drop_v2" not in db["leads"].find_one({"phone": PHONE})
    assert set(db.list_collection_names()) == {
        "leads", "universo_cartera_prop360",
        "universo_cartera_prop360_historial",
    }


def test_test_campaign_lifecycle_only_mutates_existing_canonical_lead(db, monkeypatch):
    assert v2.prepare_one(db, now=NOW, **params())["status"] == "PREPARED"
    lead = db["leads"].find_one({"phone": PHONE})
    assert len(lead["price_drop_v2"]["campaigns"]) == 1
    assert lead["price_drop_v2"]["campaigns"][0]["test_mode"]
    assert db["leads"].find_one({"phone": "56983219804"}).get("price_drop_v2") is None
    assert v2.approve_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                          operator="Supervisor PROCASA", now=NOW)["status"] == "APPROVED"

    calls = []
    def sender(phone, text):
        calls.append((phone, text))
        return {"success": True, "provider_message_id": "test-provider-6436",
                "delivery_status": "accepted"}

    assert v2.send_one(db, lead_id=params()["lead_id"],
                       campaign_id=CAMPAIGN, sender=sender, now=NOW)["status"] == "DISABLED"
    assert calls == []
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CAMPAIGN)
    sent = v2.send_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                       sender=sender, now=NOW)
    assert sent["status"] == "SENT"
    assert len(calls) == 1 and calls[0][0] == PHONE
    assert v2.send_one(db, lead_id=params()["lead_id"],
                       campaign_id=CAMPAIGN, sender=sender, now=NOW)["status"] == "NOT_APPROVED"
    assert len(calls) == 1

    lead = db["leads"].find_one({"phone": PHONE})
    c = lead["price_drop_v2"]["campaigns"][0]
    assert c["provider_message_id"] == "test-provider-6436"
    assert c["status"] == "SENT"
    assert c["expires_at"].replace(tzinfo=timezone.utc) == NOW + timedelta(days=7)
    assert len(lead["messages"]) == 2
    assert lead["messages"][-1]["metadata"]["campaign_id"] == CAMPAIGN
    assert lead["messages"][-1]["metadata"]["provider_message_id"] == "test-provider-6436"
    assert set(db.list_collection_names()) == {
        "leads", "universo_cartera_prop360",
        "universo_cartera_prop360_historial",
    }

    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    context = v2.active_context(db, phone=PHONE, message="me interesa",
                                now=NOW + timedelta(minutes=1))
    assert context["property_code"] == "6436"
    assert context["old_price_uf"] == 2790
    assert context["new_price_uf"] == 2690
    assert context["test_mode"]
    assert v2.active_context(db, phone=PHONE, message="codigo 9999",
                             now=NOW + timedelta(minutes=1)) is None

    response = v2.record_inbound(db, phone=PHONE, provider_message_id="inbound-1",
                                 text="si me interesa",
                                 now=NOW + timedelta(minutes=2))
    assert response["status"] == "ATTRIBUTED"
    assert response["classification"] == "INTERES_DECLARADO"
    duplicate = v2.record_inbound(db, phone=PHONE, provider_message_id="inbound-1",
                                  text="si me interesa",
                                  now=NOW + timedelta(minutes=3))
    assert duplicate["status"] == "DUPLICATE_IGNORED"
    after = db["leads"].find_one({"phone": PHONE})
    c = after["price_drop_v2"]["campaigns"][0]
    assert c["reply_count"] == 1
    assert c["status"] == "RESPONDED"
    assert c["notifications"]["administrator"]["status"] == "PENDING_SIMULATION"
    assert c["notifications"]["property_executive"]["status"] == "PENDING_SIMULATION"
    assert set(db.list_collection_names()) == {
        "leads", "universo_cartera_prop360",
        "universo_cartera_prop360_historial",
    }


def test_no_real_contact_without_unique_identity_consented_prior_inquiry(db):
    with pytest.raises(v2.PilotBlocked, match="consent_review_required"):
        v2.preview_one(db, **params(test_mode=False))
    with pytest.raises(v2.PilotBlocked, match="ambiguous_lead_identity"):
        v2.preview_one(db, **params(test_mode=False, consent_reviewed=True,
                                    consent_basis="Autorización revisada"))


def test_live_price_change_blocks_test_send(db, monkeypatch):
    v2.prepare_one(db, now=NOW, **params())
    v2.approve_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                   operator="Supervisor", now=NOW)
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CAMPAIGN)
    db["universo_cartera_prop360"].update_one(
        {"codigo": "6436"}, {"$set": {"tipo_operacion.precio_venta.precio_uf": 2750}})
    calls = []
    with pytest.raises(v2.PilotBlocked, match="property_price_or_availability_changed"):
        v2.send_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                    sender=lambda *a: calls.append(a), now=NOW)
    assert not calls


def test_unknown_delivery_is_durable_and_not_retried(db, monkeypatch):
    v2.prepare_one(db, now=NOW, **params())
    v2.approve_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                   operator="Supervisor", now=NOW)
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CAMPAIGN)
    attempts = []
    def broken_sender(*a):
        attempts.append(a)
        raise TimeoutError("provider may have accepted")
    result = v2.send_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                         sender=broken_sender, now=NOW)
    assert result["status"] == "DELIVERY_UNKNOWN"
    assert v2.send_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                       sender=broken_sender, now=NOW)["status"] == "NOT_APPROVED"
    assert len(attempts) == 1
    assert db["leads"].find_one({"phone": PHONE})["price_drop_v2"]["campaigns"][0]["status"] == "DELIVERY_UNKNOWN"
