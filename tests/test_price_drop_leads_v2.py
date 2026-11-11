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
    monkeypatch.setenv(v2.ADMIN_PHONE_FLAG, PHONE)
    monkeypatch.setenv(v2.TEST_LEAD_ID_FLAG, "690000000000000000000777")
    monkeypatch.delenv(v2.EXEC_NOTICE_ENABLED_FLAG, raising=False)
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


def test_admin_qa_reset_archives_context_without_deleting_campaign_or_message_history(monkeypatch):
    monkeypatch.setenv(v2.TEST_PHONE_FLAG, PHONE)
    qa_id = "test_leads_6436_20261110_qacheck1"
    prior_qa = {"campaign_id": "test_leads_6436_20261109_previous", "started_at": NOW}
    campaign = {
        "campaign_id": prior_qa["campaign_id"], "status": "RESPONDED",
        "test_mode": True, "provider_message_id": "mock-prior-send",
        "reply_count": 1,
    }
    messages = [
        {"role": "assistant", "content": "mensaje de prueba anterior", "message_id": "out-1"},
        {"role": "user", "content": "respuesta de prueba anterior", "message_id": "in-1"},
    ]
    reply_events = [{"campaign_id": campaign["campaign_id"], "provider_message_id": "in-1"}]
    processed_ids = ["in-1"]
    db = mongomock.MongoClient(tz_aware=True)["URLS"]
    db["leads"].insert_one({
        "_id": ObjectId("690000000000000000000790"),
        "phone": PHONE, "conversation_id": "prior-conversation",
        "prospecto": {"codigo": "6436", "comuna": "Macul"},
        "messages": messages,
        "pending_response": {"type": "VISIT_CONFIRMATION"},
        "last_intent": "agendar_visita",
        "price_drop_v2": {
            "campaigns": [campaign], "qa_session": prior_qa,
            "reply_events": reply_events,
            "processed_inbound_provider_ids": processed_ids,
        },
    })

    result = v2.reset_test_qa_session(
        db, campaign_id=qa_id, operator="Operador de QA",
        phone=PHONE, now=NOW + timedelta(minutes=1),
    )
    lead = db["leads"].find_one({"phone": PHONE})
    assert result["status"] == "RESET"
    assert result["historical_messages_preserved"] == len(messages)
    assert lead["messages"] == messages
    assert lead["price_drop_v2"]["campaigns"] == [campaign]
    assert lead["price_drop_v2"]["reply_events"] == reply_events
    assert lead["price_drop_v2"]["processed_inbound_provider_ids"] == processed_ids
    assert lead["price_drop_v2"]["qa_session_archive"] == [prior_qa]
    assert lead["price_drop_v2"]["qa_session"]["campaign_id"] == qa_id
    assert lead["prospecto"] == {}
    assert lead["conversation_id"] != "prior-conversation"
    assert "pending_response" not in lead and "last_intent" not in lead


def test_admin_qa_reset_fails_closed_for_phone_outside_allowlist(monkeypatch):
    monkeypatch.setenv(v2.TEST_PHONE_FLAG, "+56911112222")
    db = mongomock.MongoClient()["URLS"]
    db["leads"].insert_one({"phone": "+56911112222", "messages": []})
    result = v2.reset_test_qa_session(
        db, campaign_id="test_leads_6436_20261110_qacheck2",
        operator="Operador de QA", phone="+56911112222", now=NOW,
    )
    assert result == {"status": "BLOCKED", "reason": "qa_reset_not_authorized"}
    assert db["leads"].find_one({"phone": "+56911112222"}).get("price_drop_v2") is None


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
    assert v2.send_one(db, lead_id=params()["lead_id"],
                       campaign_id=CAMPAIGN, sender=sender, now=NOW)["status"] == "CONTEXT_DISABLED"
    assert calls == []
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
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
    assert lead["messages"][-1]["provider_message_id"] == "test-provider-6436"
    assert lead["messages"][-1]["delivery_status"] == "accepted"
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
    assert c["notifications"]["property_executive"]["status"] == "FROZEN_ADMIN_ONLY"
    assert set(db.list_collection_names()) == {
        "leads", "universo_cartera_prop360",
        "universo_cartera_prop360_historial",
    }


def test_no_real_contact_without_unique_identity_contact_basis_prior_inquiry(db):
    with pytest.raises(v2.PilotBlocked, match="contact_basis_review_required"):
        v2.preview_one(db, **params(test_mode=False))
    with pytest.raises(v2.PilotBlocked, match="ambiguous_lead_identity"):
        v2.preview_one(db, **params(test_mode=False, contact_basis_reviewed=True,
                                    contact_basis_type=v2.DIRECT_RESPONSE_BASIS_TYPE,
                                    contact_basis="Consulta previa de la misma propiedad"))


def test_v2_global_opt_out_persists_and_blocks_future_contact(db, monkeypatch):
    v2.prepare_one(db, now=NOW, **params())
    v2.approve_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                   operator="Supervisor PROCASA", now=NOW)
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CAMPAIGN)
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    sent = v2.send_one(
        db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
        sender=lambda *_: {"success": True, "provider_message_id": "out-optout"},
        now=NOW,
    )
    assert sent["status"] == "SENT"

    result = v2.record_inbound(
        db, phone=PHONE, provider_message_id="in-optout",
        text="No quiero recibir más información, por favor.",
        now=NOW + timedelta(minutes=2),
    )
    assert result["classification"] == "BAJA"
    lead = db["leads"].find_one({"phone": PHONE})
    campaign = lead["price_drop_v2"]["campaigns"][0]
    assert campaign["status"] == "OPTED_OUT"
    assert lead["price_drop_v2"]["opted_out"] is True
    assert lead["price_drop_campaign_v1"]["opted_out"] is True
    assert lead["conversation_status"] == "STOPPED_BY_CLIENT"
    assert campaign["notifications"]["administrator_info"]["status"] == "PENDING_INFO"
    assert v2.active_context(db, phone=PHONE, message="¿sigue disponible?",
                             now=NOW + timedelta(minutes=3)) is None
    with pytest.raises(v2.PilotBlocked, match="lead_opted_out"):
        v2.preview_one(db, **params(campaign_id="new-event-after-optout"))


def test_v2_call_and_visit_preferences_stay_scoped(db, monkeypatch):
    v2.prepare_one(db, now=NOW, **params())
    v2.approve_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                   operator="Supervisor PROCASA", now=NOW)
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CAMPAIGN)
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    v2.send_one(
        db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
        sender=lambda *_: {"success": True, "provider_message_id": "out-preference"},
        now=NOW,
    )
    call_preference = v2.record_inbound(
        db, phone=PHONE, provider_message_id="in-no-call",
        text="Sí me interesa, pero no quiero que me llamen.",
        now=NOW + timedelta(minutes=2),
    )
    assert call_preference["classification"] == "INTERES_DECLARADO"
    lead = db["leads"].find_one({"phone": PHONE})
    assert (lead["price_drop_v2"] or {}).get("opted_out") is not True
    assert lead["price_drop_v2"]["contact_preferences"]["do_not_call"] is True
    assert lead["price_drop_v2"]["campaigns"][0]["status"] == "RESPONDED"
    assert v2.active_context(db, phone=PHONE, message="por este chat",
                             now=NOW + timedelta(minutes=3))["customer_preferences"]["do_not_call"] is True

    visit_decline = v2.record_inbound(
        db, phone=PHONE, provider_message_id="in-no-visit-info",
        text="No me interesa visitar por ahora, pero dime los gastos comunes.",
        now=NOW + timedelta(minutes=4),
    )
    assert visit_decline["classification"] == "CONSULTA_INFORMATIVA"
    lead = db["leads"].find_one({"phone": PHONE})
    assert (lead["price_drop_v2"] or {}).get("opted_out") is not True
    assert lead["price_drop_v2"]["contact_preferences"]["do_not_call"] is True
    assert lead["price_drop_v2"]["contact_preferences"]["visit_declined"] is True
    assert lead["price_drop_v2"]["campaigns"][0]["status"] == "RESPONDED"


def test_v2_context_instruction_disallows_unverified_property_inferences():
    from chatbot.price_drop_pilot import context_instruction

    instruction = context_instruction({
        "property_code": "6436", "currency": "UF",
        "old_price_uf": 2790, "new_price_uf": 2690,
        "operation": "Venta",
    })
    assert "No infieras luminosidad, sol, calor" in instruction
    assert "sin deducir sus efectos" in instruction
    assert "Responde primero a lo que preguntó" in instruction


def test_live_price_change_blocks_test_send(db, monkeypatch):
    v2.prepare_one(db, now=NOW, **params())
    v2.approve_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                   operator="Supervisor", now=NOW)
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CAMPAIGN)
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
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
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
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


def test_real_inquiry_must_reference_same_property_before_drop(db):
    lead = db["leads"].find_one({"phone": PHONE})
    drop = datetime(2026, 10, 14, 11, 52, 43, tzinfo=timezone.utc)
    # A generic earlier conversation cannot be used as campaign evidence.
    assert v2._inquiry_before(lead, "6436", drop) is False
    older = dict(role="user", content="Consulté por el código 6436",
                 timestamp="2026-10-12T12:00:00-03:00")
    lead["messages"].append(older)
    assert v2._inquiry_before(lead, "6436", drop) is True
    older["content"] = "Consulté por el código 9999"
    assert v2._inquiry_before(lead, "6436", drop) is False
    older["metadata"] = {"property_code": "6436"}
    assert v2._inquiry_before(lead, "6436", drop) is True
    older["metadata"] = {}
    older["content"] = "Revisé https://www.procasa.cl/6436, gracias"
    assert v2._inquiry_before(lead, "6436", drop) is True
    older["timestamp"] = "2026-11-05T12:00:00-03:00"
    assert v2._inquiry_before(lead, "6436", drop) is False


def test_delivered_and_read_confirmed_on_lead_without_new_collection(db, monkeypatch):
    v2.prepare_one(db, now=NOW, **params())
    v2.approve_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                   operator="Supervisor", now=NOW)
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CAMPAIGN)
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    v2.send_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                sender=lambda *a: {
                    "success": True, "provider_message_id": "wamid-outbound-1",
                    "delivery_status": "accepted",
                }, now=NOW)
    payload = {"data": {"messages": {
        "key": {"id": "wamid-outbound-1"}, "status": "delivered",
    }}}
    result = v2.record_delivery_update(db, payload, now=NOW+timedelta(seconds=4))
    assert result["status"] == "UPDATED"
    row = db["leads"].find_one({"phone": PHONE})["price_drop_v2"]["campaigns"][0]
    assert row["provider_status"] == "delivered"
    assert row["delivered_at"].replace(tzinfo=timezone.utc) == NOW+timedelta(seconds=4)
    assert v2.record_delivery_update(db, payload, now=NOW+timedelta(seconds=6))["status"] == "UNCHANGED"
    payload["data"]["messages"]["status"] = "read"
    assert v2.record_delivery_update(db, payload, now=NOW+timedelta(seconds=10))["status"] == "UPDATED"
    row = db["leads"].find_one({"phone": PHONE})["price_drop_v2"]["campaigns"][0]
    assert row["provider_status"] == "read"
    assert row["read_at"].replace(tzinfo=timezone.utc) == NOW+timedelta(seconds=10)
    payload["data"]["messages"]["status"] = "delivered"
    assert v2.record_delivery_update(db, payload)["status"] == "UNCHANGED"
    assert set(db.list_collection_names()) == {
        "leads", "universo_cartera_prop360",
        "universo_cartera_prop360_historial",
    }


def test_admin_notice_goes_only_to_approved_phone_and_executive_stays_frozen(db, monkeypatch):
    v2.prepare_one(db, now=NOW, **params())
    v2.approve_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                   operator="Supervisor", now=NOW)
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CAMPAIGN)
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    v2.send_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                sender=lambda *args: {
                    "success": True, "provider_message_id": "out-notice-test",
                    "delivery_status": "accepted",
                }, now=NOW)
    assert v2.record_inbound(db, phone=PHONE, provider_message_id="in-notice-test",
                             text="sí, me interesa", now=NOW+timedelta(seconds=3))["status"] == "ATTRIBUTED"

    calls = []
    def sender(phone, message):
        calls.append((phone, message))
        return {"success": True, "provider_message_id": "internal-" + str(len(calls))}

    assert v2.send_test_notice_once(db, role="administrator", sender=sender)["status"] == "DISABLED"
    assert calls == []
    monkeypatch.setenv(v2.TEST_NOTICE_FLAG, "true")
    assert v2.send_test_notice_once(db, role="administrator", sender=sender, now=NOW+timedelta(seconds=30))["status"] == "DEFERRED_CUSTOMER_ACTIVE"
    assert calls == []
    assert v2.send_test_notice_once(db, role="administrator", sender=sender, now=NOW+timedelta(seconds=180))["status"] == "SENT"
    assert v2.send_test_notice_once(db, role="property_executive", sender=sender, now=NOW+timedelta(seconds=200))["status"] == "EXECUTIVE_NOTICE_FROZEN"
    assert v2.send_test_notice_once(db, role="property_executive", sender=sender, now=NOW+timedelta(seconds=275))["status"] == "EXECUTIVE_NOTICE_FROZEN"
    assert v2.send_test_notice_once(db, role="administrator", sender=sender)["status"] == "IDLE"
    assert v2.send_test_notice_once(db, role="property_executive", sender=sender)["status"] == "EXECUTIVE_NOTICE_FROZEN"
    assert len(calls) == 1 and calls[0][0] == PHONE
    assert "ADMINISTRADOR" in calls[0][1]
    assert "EJECUTIVO DE LA PROPIEDAD" not in calls[0][1]
    assert "sí, me interesa" in calls[0][1]
    assert "/crm/lead-id/690000000000000000000777" in calls[0][1]
    assert "\\n" not in calls[0][1]
    camp = db["leads"].find_one({"phone": PHONE})["price_drop_v2"]["campaigns"][0]
    assert camp["notifications"]["administrator"]["status"] == "SENT"
    assert camp["notifications"]["property_executive"]["status"] == "FROZEN_ADMIN_ONLY"
    assert set(db.list_collection_names()) == {
        "leads", "universo_cartera_prop360",
        "universo_cartera_prop360_historial",
    }


def test_prior_mlc_alias_proves_actual_inquiry_not_generic_chat(db):
    prop = db["universo_cartera_prop360"]
    prop.update_one({"codigo": "6436"}, {"$set": {
        "publicaciones.aliases": [{
            "external_id": "MLC-3776431868", "resolvable": True,
            "publication_status": "historical",
        }],
    }})
    listing_ids = v2._verified_listing_ids(db, "6436")
    assert "mlc-3776431868" in listing_ids
    lead = db["leads"].find_one({"phone": PHONE})
    drop = datetime(2026, 10, 14, 11, 52, 43, tzinfo=timezone.utc)
    lead["messages"].append({
        "role": "user",
        "content": "Hola, preguntas sobre https://departamento.mercadolibre.cl/MLC-3776431868",
        "timestamp": "2026-07-16T14:20:55-04:00",
    })
    assert v2._inquiry_before(lead, "6436", drop, listing_ids)
    assert not v2._inquiry_before(lead, "6436", drop)
    lead["messages"][-1]["content"] = "Hola, busco un departamento"
    assert not v2._inquiry_before(lead, "6436", drop, listing_ids)


def _approved_real_pilot_sample(db, monkeypatch):
    """Three-property allowlist; only one synthetic lead is materialized."""
    lead_id = "690000000000000000000799"
    phone = "+56922117788"
    db["leads"].insert_one({
        "_id": ObjectId(lead_id),
        "phone": phone,
        "lead_temperature_effective": "COLD",
        "pipeline_stage": "CONTACTED",
        "messages": [{
            "role": "user",
            "content": "Estoy interesado en la propiedad código 6436",
            "timestamp": "2026-10-01T10:00:00-03:00",
        }, {
            "role": "user",
            "content": "Sigo interesado en la propiedad código 6436",
            "timestamp": (NOW - timedelta(hours=1)).isoformat(),
        }],
    })
    campaign_id = "pilot_real_rebajas_20261109_v2"
    p = params(
        lead_id=lead_id, phone=phone, campaign_id=campaign_id,
        test_mode=False, contact_basis_reviewed=True,
        contact_basis_type=v2.DIRECT_RESPONSE_BASIS_TYPE,
        contact_basis="Consulta previa de la misma propiedad (fixture de prueba)",
    )
    assert v2.prepare_one(db, now=NOW, **p)["status"] == "PREPARED"
    assert v2.review_whatsapp_contact(
        db, lead_id=lead_id, campaign_id=campaign_id,
        operator="Operador de pruebas",
        evidence="Consentimiento explícito fixture CRM 2026-10-01",
        now=NOW,
    )["status"] == "WHATSAPP_CONTACT_REVIEWED"
    saved = db["leads"].find_one({"_id": ObjectId(lead_id)})
    reusable = saved["price_drop_v2"]["whatsapp_contact_authorization"]
    assert reusable["status"] == "ACTIVE"
    assert reusable["channel"] == "WHATSAPP"
    assert reusable["scope"] == v2.WHATSAPP_CONTACT_SCOPE
    assert reusable["phone_normalized"] == phone
    assert reusable["source_campaign_id"] == campaign_id
    assert saved["lead_temperature_effective"] == "COLD"
    assert saved["pipeline_stage"] == "CONTACTED"
    assert v2.approve_one(db, lead_id=lead_id, campaign_id=campaign_id,
                          operator="Operador de pruebas", now=NOW)["status"] == "APPROVED"
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.WHATSAPP_CONTACT_REVIEWED_FLAG, "true")
    return lead_id, campaign_id, phone


def _clear_campaign_contact_permission(db, lead_id):
    db["leads"].update_one({"_id": ObjectId(lead_id)}, {"$unset": {
        "price_drop_v2.whatsapp_contact_authorization": "",
        "price_drop_v2.campaigns.0.whatsapp_contact_reviewed": "",
        "price_drop_v2.campaigns.0.whatsapp_contact_evidence": "",
        "price_drop_v2.campaigns.0.whatsapp_contact_reviewed_by": "",
        "price_drop_v2.campaigns.0.whatsapp_contact_reviewed_at": "",
    }})


def test_fixed_three_recipient_pilot_allows_verified_prior_inquiry_direct_followup(
        db, monkeypatch):
    lead_id, campaign_id, phone = _approved_real_pilot_sample(db, monkeypatch)
    _clear_campaign_contact_permission(db, lead_id)
    before = db["leads"].find_one({"_id": ObjectId(lead_id)})
    assert before["price_drop_v2"].get("whatsapp_contact_authorization") is None
    sender_calls = []
    result = v2.send_one(
        db, lead_id=lead_id, campaign_id=campaign_id,
        sender=lambda actual_phone, message: sender_calls.append(
            (actual_phone, message)) or {
                "success": True, "provider_message_id": "mock-prior-inquiry",
                "delivery_status": "accepted",
            },
        now=NOW,
    )
    assert result["status"] == "SENT"
    assert len(sender_calls) == 1
    assert sender_calls[0][0] == phone
    sent = db["leads"].find_one({"_id": ObjectId(lead_id)})
    assert sent["price_drop_v2"].get("whatsapp_contact_authorization") is None
    assert sent["price_drop_v2"]["campaigns"][0]["status"] == "SENT"
    assert v2.send_one(
        db, lead_id=lead_id, campaign_id=campaign_id,
        sender=lambda *_: sender_calls.append(("DUPLICATE",)), now=NOW,
    )["status"] == "NOT_APPROVED"
    assert len(sender_calls) == 1


def test_fixed_pilot_exception_does_not_apply_to_other_campaigns(db, monkeypatch):
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    _clear_campaign_contact_permission(db, lead_id)
    db["leads"].update_one({"_id": ObjectId(lead_id)}, {"$set": {
        "price_drop_v2.campaigns.0.campaign_id": "another-price-drop-campaign",
        "price_drop_v2.campaigns.0.event_id": "another-price-drop-campaign",
    }})
    calls = []
    result = v2.send_one(
        db, lead_id=lead_id, campaign_id="another-price-drop-campaign",
        sender=lambda *args: calls.append(args), now=NOW,
    )
    assert result["status"] == "WHATSAPP_CONTACT_AUTHORIZATION_UNVERIFIED"
    assert calls == []


def test_fixed_pilot_still_blocks_optout_without_contact_permission(db, monkeypatch):
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    _clear_campaign_contact_permission(db, lead_id)
    db["leads"].update_one({"_id": ObjectId(lead_id)}, {"$set": {
        "price_drop_v2.opted_out": True,
    }})
    calls = []
    result = v2.send_one(
        db, lead_id=lead_id, campaign_id=campaign_id,
        sender=lambda *args: calls.append(args), now=NOW,
    )
    assert result["status"] == "BLOCKED_OPTED_OUT"
    assert calls == []


def test_fixed_pilot_still_requires_original_property_inquiry(db, monkeypatch):
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    _clear_campaign_contact_permission(db, lead_id)
    lead = db["leads"].find_one({"_id": ObjectId(lead_id)})
    messages = [dict(message, content="Consulto por otra propiedad codigo 9999")
                for message in lead["messages"]]
    db["leads"].update_one({"_id": ObjectId(lead_id)}, {"$set": {
        "messages": messages,
    }})
    calls = []
    result = v2.send_one(
        db, lead_id=lead_id, campaign_id=campaign_id,
        sender=lambda *args: calls.append(args), now=NOW,
    )
    assert result["status"] == "REAL_SEND_INQUIRY_NOT_VERIFIED"
    assert calls == []


def test_real_campaign_uses_shared_send_switch_and_never_runs_on_render(db, monkeypatch):
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    provider_calls = []
    def sender(*args):
        provider_calls.append(args)
        return {"success": True, "provider_message_id": "should-not-send"}
    monkeypatch.setenv("RENDER", "true")
    assert v2.send_one(db, lead_id=lead_id, campaign_id=campaign_id,
                       sender=sender, now=NOW)["status"] == "MANUAL_CAMPAIGNS_DISABLED_ON_RENDER"
    monkeypatch.delenv("RENDER")
    monkeypatch.delenv(v2.SEND_FLAG)
    assert v2.send_one(db, lead_id=lead_id, campaign_id=campaign_id,
                       sender=sender, now=NOW)["status"] == "DISABLED"
    assert provider_calls == []


def test_real_pilot_rechecks_contact_basis_and_allows_exactly_one_mock_send(db, monkeypatch):
    lead_id, campaign_id, phone = _approved_real_pilot_sample(db, monkeypatch)
    db["leads"].update_one(
        {"_id": ObjectId(lead_id)},
        {"$set": {"price_drop_v2.campaigns.0.contact_basis_reviewed": False}},
    )
    provider_calls = []
    def sender(actual_phone, message):
        provider_calls.append((actual_phone, message))
        return {"success": True, "provider_message_id": "mock-real-gate-001",
                "delivery_status": "accepted"}
    assert v2.send_one(db, lead_id=lead_id, campaign_id=campaign_id,
                       sender=sender, now=NOW)["status"] == "REAL_SEND_CONTACT_BASIS_OR_APPROVAL_MISSING"
    assert provider_calls == []
    db["leads"].update_one(
        {"_id": ObjectId(lead_id)},
        {"$set": {"price_drop_v2.campaigns.0.contact_basis_reviewed": True}},
    )
    db["leads"].update_one(
        {"_id": ObjectId(lead_id)},
        {"$set": {"price_drop_v2.campaigns.0.contact_basis_type": "BLANKET_MARKETING"}},
    )
    assert v2.send_one(db, lead_id=lead_id, campaign_id=campaign_id,
                       sender=sender, now=NOW)["status"] == "REAL_SEND_CONTACT_BASIS_OR_APPROVAL_MISSING"
    assert provider_calls == []
    db["leads"].update_one(
        {"_id": ObjectId(lead_id)},
        {"$set": {"price_drop_v2.campaigns.0.contact_basis_type":
                  v2.DIRECT_RESPONSE_BASIS_TYPE}},
    )
    outcome = v2.send_one(db, lead_id=lead_id, campaign_id=campaign_id,
                          sender=sender, now=NOW)
    assert outcome["status"] == "SENT"
    assert len(provider_calls) == 1 and provider_calls[0][0] == phone
    assert v2.send_one(db, lead_id=lead_id, campaign_id=campaign_id,
                       sender=sender, now=NOW)["status"] == "NOT_APPROVED"
    assert len(provider_calls) == 1
    row = db["leads"].find_one({"_id": ObjectId(lead_id)})
    assert row["price_drop_v2"]["campaigns"][0]["provider_message_id"] == "mock-real-gate-001"
    assert row["messages"][-1]["metadata"]["tipo"] == "price_drop_outreach"
    assert set(db.list_collection_names()) == {
        "leads", "universo_cartera_prop360",
        "universo_cartera_prop360_historial",
    }


def test_reusable_whatsapp_authorization_allows_send_without_campaign_review_snapshot(db, monkeypatch):
    lead_id, campaign_id, phone = _approved_real_pilot_sample(db, monkeypatch)
    db["leads"].update_one({"_id": ObjectId(lead_id)}, {"$unset": {
        "price_drop_v2.campaigns.0.whatsapp_contact_reviewed": "",
        "price_drop_v2.campaigns.0.whatsapp_contact_evidence": "",
        "price_drop_v2.campaigns.0.whatsapp_contact_reviewed_by": "",
        "price_drop_v2.campaigns.0.whatsapp_contact_reviewed_at": "",
    }})
    calls = []
    result = v2.send_one(
        db, lead_id=lead_id, campaign_id=campaign_id,
        sender=lambda *args: calls.append(args) or {
            "success": True, "provider_message_id": "mock-reused-auth",
            "delivery_status": "accepted",
        }, now=NOW,
    )
    assert result["status"] == "SENT"
    assert len(calls) == 1 and calls[0][0] == phone


def test_revoked_reusable_authorization_blocks_even_with_campaign_snapshot(db, monkeypatch):
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    db["leads"].update_one({"_id": ObjectId(lead_id)}, {"$set": {
        "price_drop_v2.whatsapp_contact_authorization.status": "REVOKED",
        "price_drop_v2.whatsapp_contact_authorization.revoked_at": NOW,
    }})
    calls = []
    result = v2.send_one(
        db, lead_id=lead_id, campaign_id=campaign_id,
        sender=lambda *args: calls.append(args), now=NOW,
    )
    assert result["status"] == "WHATSAPP_CONTACT_AUTHORIZATION_INVALID_OR_REVOKED"
    assert calls == []


def test_reusable_authorization_does_not_override_stop_or_global_opt_out(db, monkeypatch):
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    db["leads"].update_one({"_id": ObjectId(lead_id)}, {"$set": {
        "price_drop_v2.opted_out": True,
    }})
    calls = []
    result = v2.send_one(
        db, lead_id=lead_id, campaign_id=campaign_id,
        sender=lambda *args: calls.append(args), now=NOW,
    )
    assert result["status"] == "BLOCKED_OPTED_OUT"
    assert calls == []


def test_contact_review_refuses_to_record_reusable_auth_after_opt_out(db, monkeypatch):
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    db["leads"].update_one({"_id": ObjectId(lead_id)}, {"$set": {
        "price_drop_v2.opted_out": True,
    }})
    with pytest.raises(v2.PilotBlocked, match="lead_contact_suppressed"):
        v2.review_whatsapp_contact(
            db, lead_id=lead_id, campaign_id=campaign_id,
            operator="Operador de pruebas",
            evidence="Consentimiento explícito fixture CRM 2026-10-01",
            now=NOW + timedelta(minutes=5),
        )


def test_real_pilot_blocks_duplicate_or_lost_historic_inquiry(db, monkeypatch):
    lead_id, campaign_id, phone = _approved_real_pilot_sample(db, monkeypatch)
    calls = []
    sender = lambda *args: calls.append(args)
    db["leads"].insert_one({
        "phone": phone.removeprefix("+"), "messages": [],
    })
    assert v2.send_one(db, lead_id=lead_id, campaign_id=campaign_id,
                       sender=sender, now=NOW)["status"] == "REAL_SEND_AMBIGUOUS_PHONE"
    db["leads"].delete_one({"phone": phone.removeprefix("+")})
    db["leads"].update_one(
        {"_id": ObjectId(lead_id)},
        {"$set": {"messages": [{"role": "user", "content": "Busco otra zona",
                               "timestamp": "2026-10-01T10:00:00-03:00"}]}},
    )
    assert v2.send_one(db, lead_id=lead_id, campaign_id=campaign_id,
                       sender=sender, now=NOW)["status"] == "REAL_SEND_INQUIRY_NOT_VERIFIED"
    assert calls == []


def test_real_pilot_accepts_only_bson_millisecond_inquiry_precision_loss(db, monkeypatch):
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    persisted_millisecond = datetime(2026, 10, 1, 13, 0, 0, 0, tzinfo=timezone.utc)
    db["leads"].update_one({"_id": ObjectId(lead_id)}, {"$set": {
        "messages.0.timestamp": "2026-10-01T10:00:00.000700-03:00",
        "price_drop_v2.campaigns.0.inquiry_evidence_at": persisted_millisecond,
    }})
    provider_calls = []
    sender = lambda *args: provider_calls.append(args) or {
        "success": True, "provider_message_id": "mock-bson-precision-id",
        "delivery_status": "accepted",
    }
    result = v2.send_one(db, lead_id=lead_id, campaign_id=campaign_id,
                         sender=sender, now=NOW)
    assert result["status"] == "SENT"
    assert len(provider_calls) == 1


def test_real_pilot_still_blocks_inquiry_evidence_over_one_millisecond_apart(db, monkeypatch):
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    db["leads"].update_one({"_id": ObjectId(lead_id)}, {"$set": {
        "messages.0.timestamp": "2026-10-01T10:00:00.000700-03:00",
        "price_drop_v2.campaigns.0.inquiry_evidence_at":
            datetime(2026, 10, 1, 13, 0, 0, 2000, tzinfo=timezone.utc),
    }})
    provider_calls = []
    result = v2.send_one(
        db, lead_id=lead_id, campaign_id=campaign_id,
        sender=lambda *args: provider_calls.append(args), now=NOW,
    )
    assert result["status"] == "REAL_SEND_INQUIRY_NOT_VERIFIED"
    assert provider_calls == []


def test_shared_real_send_blocker_rejects_same_legacy_price_event(db, monkeypatch):
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    db["leads"].update_one({"_id": ObjectId(lead_id)}, {"$push": {
        "price_drop_v2.campaigns": {
            "campaign_id": "legacy_6436_same_drop",
            "property_code": "6436", "currency": "UF",
            "old_price_uf": 2790, "new_price_uf": 2690,
            "status": "SENT", "sent_at": NOW - timedelta(days=1),
        },
    }})
    calls = []
    result = v2.send_one(
        db, lead_id=lead_id, campaign_id=campaign_id,
        sender=lambda *args: calls.append(args), now=NOW,
    )
    assert result["status"] == "same_price_drop_already_recorded"
    assert calls == []


def test_real_wasender_sender_can_follow_up_outside_24_hours_with_reviewed_opt_in(db, monkeypatch):
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    db["leads"].update_one(
        {"_id": ObjectId(lead_id)},
        {"$set": {"messages.1.timestamp": "2026-10-14T11:52:43-03:00"}},
    )
    calls = []
    result = v2.send_one(
        db, lead_id=lead_id, campaign_id=campaign_id,
        sender=lambda *args: calls.append(args) or {
            "success": True, "provider_message_id": "mock-wasender-id",
            "delivery_status": "accepted",
        }, now=NOW,
    )
    assert result["status"] == "SENT"
    assert len(calls) == 1


def test_official_channel_is_never_mislabeled_as_wasender_sender(db, monkeypatch):
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    monkeypatch.setenv("PRICE_DROP_WHATSAPP_CHANNEL_MODE", "official")
    calls = []
    result = v2.send_one(
        db, lead_id=lead_id, campaign_id=campaign_id,
        sender=lambda *args: calls.append(args), now=NOW,
    )
    assert result["status"] == "OFFICIAL_CHANNEL_SENDER_NOT_IMPLEMENTED"
    assert calls == []


def test_legacy_pilot_cli_fails_closed_and_points_to_shared_program(capsys):
    from scripts.price_drop_leads_v2_real_pilot import main
    assert main([]) == 2
    output = capsys.readouterr().out
    assert "BLOCKED" in output
    assert "scripts_enviar_piloto_rebajas" in output


@pytest.mark.parametrize("reply, expected_label, expected_status", [
    ("No me interesa ese departamento", "RECHAZADO", "DECLINED"),
    ("Ya compré una propiedad", "BAJA", "OPTED_OUT"),
    ("BAJA, no quiero más mensajes", "BAJA", "OPTED_OUT"),
])
def test_pilot_closeout_rejections_do_not_create_leads(
        db, monkeypatch, reply, expected_label, expected_status):
    """Real campaign logic on a QA lead with mocked WaSender, no extra collections."""
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CAMPAIGN)
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.TEST_NOTICE_FLAG, "true")
    monkeypatch.setenv(v2.EXEC_NOTICE_ENABLED_FLAG, "true")
    original_count = db["leads"].count_documents({})
    v2.prepare_one(db, now=NOW, **params())
    v2.approve_one(db, lead_id=params()["lead_id"],
                   campaign_id=CAMPAIGN, operator="QA", now=NOW)
    result = v2.send_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
        sender=lambda p, m: {"success": True, "provider_message_id": "mock-rejection-send"},
        now=NOW)
    assert result["status"] == "SENT"
    attributed = v2.record_inbound(
        db, phone=PHONE, provider_message_id="mock-rejection-inbound",
        text=reply, now=NOW + timedelta(minutes=1))
    assert attributed["status"] == "ATTRIBUTED"
    assert attributed["classification"] == expected_label
    lead = db["leads"].find_one({"phone": PHONE})
    campaign = lead["price_drop_v2"]["campaigns"][-1]
    assert campaign["status"] == expected_status
    assert campaign.get("has_positive_response") is not True
    assert campaign["notifications"]["administrator"]["status"] == "CANCELED"
    assert campaign["notifications"]["property_executive"]["status"] == "FROZEN_ADMIN_ONLY"
    assert db["leads"].count_documents({}) == original_count
    assert len(lead["price_drop_v2"]["reply_events"]) == 1
    assert lead["price_drop_v2"]["reply_events"][0]["campaign_id"] == CAMPAIGN
    if expected_status == "OPTED_OUT":
        assert lead["price_drop_v2"]["opted_out"] is True
        assert lead["conversation_status"] == "STOPPED_BY_CLIENT"
        assert campaign["notifications"]["administrator_info"]["status"] == "PENDING_INFO"
        assert v2.active_context(db, phone=PHONE, message="hola",
                                 now=NOW + timedelta(minutes=3)) is None
    else:
        assert lead["price_drop_v2"].get("opted_out") is not True
        assert lead.get("conversation_status") != "STOPPED_BY_CLIENT"
    recipients = []
    assert v2.send_test_notice_once(
        db, role="administrator", sender=lambda *x: recipients.append(x),
        now=NOW + timedelta(minutes=5))["status"] == "IDLE"
    assert recipients == []


def test_closeout_simulated_executive_notice_only_to_9804(db, monkeypatch):
    """Send-role QA mock, never an actual employee or a new lead."""
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CAMPAIGN)
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.EXEC_NOTICE_ENABLED_FLAG, "true")
    monkeypatch.setenv(v2.TEST_NOTICE_FLAG, "true")
    monkeypatch.setenv(v2.ADMIN_PHONE_FLAG, PHONE)
    v2.prepare_one(db, now=NOW, **params())
    v2.approve_one(db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
                   operator="QA", now=NOW)
    assert v2.send_one(
        db, lead_id=params()["lead_id"], campaign_id=CAMPAIGN,
        sender=lambda *_: {"success": True, "provider_message_id": "out-interest"},
        now=NOW)["status"] == "SENT"
    assert v2.record_inbound(
        db, phone=PHONE, provider_message_id="in-interest",
        text="Sí, me interesa", now=NOW + timedelta(minutes=1))["classification"] == "INTERES_DECLARADO"
    sent = []
    def fake_sender(phone, message):
        sent.append((phone, message))
        return {"success": True, "provider_message_id": "qa-notice-" + str(len(sent))}
    first = v2.send_test_notice_once(
        db, role="property_executive", sender=fake_sender,
        now=NOW + timedelta(minutes=4))
    assert first["status"] == "SENT"
    assert len(sent) == 1 and sent[0][0] == PHONE
    assert "PRUEBA INTERNA" in sent[0][1]
    assert "EJECUTIVO DE LA PROPIEDAD" in sent[0][1]
    assert v2.send_test_notice_once(
        db, role="property_executive", sender=fake_sender,
        now=NOW + timedelta(minutes=6))["status"] == "IDLE"
    assert len(sent) == 1
    assert db["leads"].count_documents({}) == 2


@pytest.mark.parametrize("legacy_missing_pointer", [False, True])
def test_real_executive_notice_requires_two_explicit_gates_and_active_cycle(monkeypatch, legacy_missing_pointer):
    from bson import ObjectId
    import mongomock
    d = mongomock.MongoClient()["URLS"]
    lid = ObjectId("690000000000000000000667")
    uid = ObjectId("690000000000000000000668")
    client_phone = "+56930001111"
    exec_phone = "+56932223333"
    cycle_id = "active-price-drop-cycle"
    d["leads"].insert_one({
        "_id": lid, "phone": client_phone,
        "ejecutivo_asignado": "Ejecutiva QA",
        "prospecto": {"ejecutivo": "Ejecutiva QA", "codigo": "6436"},
        "lifecycle": ({"assigned_to_user_id": str(uid)}
                      if legacy_missing_pointer else
                      {"current_assignment_cycle_id": cycle_id,
                       "assignment_cycle_id": cycle_id,
                       "assigned_to_user_id": str(uid)}),
        "price_drop_v2": {"campaigns": [{
            "campaign_id": "real-exec-test", "test_mode": False,
            "status": "RESPONDED", "has_positive_response": True,
            "property_code": "6436", "currency": "UF",
            "old_price_uf": 2790, "new_price_uf": 2690,
            "provider_message_id": "mock-real-outbound",
            "last_customer_reply_at": NOW - timedelta(minutes=5),
            "last_positive_reply_text": "Sí, me interesa",
            "last_positive_classification": {"label": "INTERES_DECLARADO"},
            "notifications": {
                "administrator": {"status": "PENDING_HUMAN_REVIEW"},
                "property_executive": {"status": "PENDING_HUMAN_REVIEW"},
            },
        }]},
    })
    d["crm_assignment_cycles"].insert_one({
        "_id": "fake-cycle-doc", "lead_id": lid,
        "assignment_cycle_id": cycle_id,
        "assigned_to_user_id": str(uid),
        "assigned_to_display_name": "Ejecutiva QA",
        "cycle_status": "active", "unassigned_at": None,
        "property_code": "6436",
    })
    d["usuarios"].insert_one({
        "_id": uid, "nombre": "Ejecutiva QA",
        "rol": "agente", "is_active": True, "telefono": exec_phone,
    })
    calls = []
    fake = lambda phone, message: (calls.append((phone, message)) or
              {"success": True, "provider_message_id": "mock-exec-success"})
    monkeypatch.delenv(v2.EXEC_NOTICE_ENABLED_FLAG, raising=False)
    monkeypatch.setenv(v2.REAL_EXEC_NOTICE_FLAG, "true")
    assert v2.send_real_exec_notice_once(d, sender=fake, now=NOW)["status"] == "DISABLED"
    monkeypatch.setenv(v2.EXEC_NOTICE_ENABLED_FLAG, "true")
    monkeypatch.delenv(v2.REAL_EXEC_NOTICE_FLAG, raising=False)
    assert v2.send_real_exec_notice_once(d, sender=fake, now=NOW)["status"] == "DISABLED"
    monkeypatch.setenv(v2.REAL_EXEC_NOTICE_FLAG, "true")
    result = v2.send_real_exec_notice_once(d, sender=fake, now=NOW)
    assert result["status"] == "SENT"
    assert result["recipient_last4"] == "3333"
    assert len(calls) == 1 and calls[0][0] == exec_phone
    assert "Lead recuperado" in calls[0][1]
    assert "No reasignar ni reiniciar SLA" in calls[0][1]
    assert v2.send_real_exec_notice_once(d, sender=fake, now=NOW)["status"] == "IDLE"
    assert len(calls) == 1
    persisted = d["leads"].find_one({"_id": lid})
    assert persisted["price_drop_v2"]["campaigns"][0]["notifications"]["property_executive"]["status"] == "SENT"
    assert persisted["price_drop_v2"]["campaigns"][0]["notifications"]["administrator"]["status"] == "PENDING_HUMAN_REVIEW"
    assert persisted["lifecycle"].get("current_assignment_cycle_id") == (
        None if legacy_missing_pointer else cycle_id
    )
    assert d["leads"].count_documents({}) == 1


def test_real_exec_notice_blocks_inconsistent_or_inactive_owner(monkeypatch):
    from bson import ObjectId
    import mongomock
    d = mongomock.MongoClient()["URLS"]
    lid = ObjectId("690000000000000000000669")
    d["leads"].insert_one({
        "_id": lid, "phone": "+56930001112",
        "ejecutivo_asignado": "Antiguo Ejecutivo",
        "prospecto": {"ejecutivo": "Antiguo Ejecutivo", "codigo": "6436"},
        "price_drop_v2": {"campaigns": [{
            "campaign_id": "real-exec-no-cycle", "test_mode": False,
            "status": "RESPONDED", "has_positive_response": True,
            "provider_message_id": "mock-out", "property_code": "6436",
            "last_customer_reply_at": NOW - timedelta(minutes=5),
            "notifications": {
                "administrator": {"status": "PENDING_HUMAN_REVIEW"},
                "property_executive": {"status": "PENDING_HUMAN_REVIEW"},
            },
        }]},
    })
    monkeypatch.setenv(v2.EXEC_NOTICE_ENABLED_FLAG, "true")
    monkeypatch.setenv(v2.REAL_EXEC_NOTICE_FLAG, "true")
    calls = []
    status = v2.send_real_exec_notice_once(
        d, sender=lambda *a: calls.append(a), now=NOW)
    assert status["status"] == "BLOCKED_ASSIGNMENT"
    assert calls == []
    campaign = d["leads"].find_one({"_id": lid})["price_drop_v2"]["campaigns"][0]
    assert campaign["notifications"]["administrator"]["status"] == "PENDING_HUMAN_REVIEW"
    assert campaign["notifications"]["property_executive"]["status"] == "BLOCKED_ASSIGNMENT"
