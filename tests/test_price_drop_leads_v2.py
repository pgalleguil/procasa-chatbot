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
    campaign_id = v2.REAL_PILOT_CAMPAIGN_ID
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
    assert v2.approve_one(db, lead_id=lead_id, campaign_id=campaign_id,
                          operator="Operador de pruebas", now=NOW)["status"] == "APPROVED"
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, campaign_id)
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.WHATSAPP_CONTACT_REVIEWED_FLAG, "true")
    monkeypatch.setenv(v2.REAL_PILOT_RECIPIENTS_FLAG, (
        f"{lead_id}:6436;"
        "690000000000000000000798:6377;"
        "690000000000000000000797:16479"
    ))
    return lead_id, campaign_id, phone


def test_real_pilot_remains_locked_until_separate_runtime_activation(db, monkeypatch):
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    provider_calls = []
    def sender(*args):
        provider_calls.append(args)
        return {"success": True, "provider_message_id": "should-not-send"}
    monkeypatch.setenv("RENDER", "true")
    assert v2.send_one(db, lead_id=lead_id, campaign_id=campaign_id,
                       sender=sender, now=NOW)["status"] == "MANUAL_CAMPAIGNS_DISABLED_ON_RENDER"
    monkeypatch.delenv("RENDER")
    assert v2.send_one(db, lead_id=lead_id, campaign_id=campaign_id,
                       sender=sender, now=NOW)["status"] == "REAL_SEND_LOCKED_PENDING_ACCEPTANCE"
    monkeypatch.setenv(v2.REAL_PILOT_ENABLED_FLAG, "true")
    monkeypatch.setenv(v2.REAL_PILOT_RECIPIENTS_FLAG, f"{lead_id}:6436")
    assert v2.send_one(db, lead_id=lead_id, campaign_id=campaign_id,
                       sender=sender, now=NOW)["status"] == "REAL_SEND_NOT_ALLOWLISTED"
    assert provider_calls == []


def test_real_pilot_rechecks_contact_basis_and_allows_exactly_one_mock_send(db, monkeypatch):
    lead_id, campaign_id, phone = _approved_real_pilot_sample(db, monkeypatch)
    monkeypatch.setenv(v2.REAL_PILOT_ENABLED_FLAG, "true")
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


def test_real_pilot_blocks_duplicate_or_lost_historic_inquiry(db, monkeypatch):
    lead_id, campaign_id, phone = _approved_real_pilot_sample(db, monkeypatch)
    monkeypatch.setenv(v2.REAL_PILOT_ENABLED_FLAG, "true")
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


def test_real_wasender_sender_can_follow_up_outside_24_hours_with_reviewed_opt_in(db, monkeypatch):
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    monkeypatch.setenv(v2.REAL_PILOT_ENABLED_FLAG, "true")
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
    monkeypatch.setenv(v2.REAL_PILOT_ENABLED_FLAG, "true")
    monkeypatch.setenv("PRICE_DROP_WHATSAPP_CHANNEL_MODE", "official")
    calls = []
    result = v2.send_one(
        db, lead_id=lead_id, campaign_id=campaign_id,
        sender=lambda *args: calls.append(args), now=NOW,
    )
    assert result["status"] == "OFFICIAL_CHANNEL_SENDER_NOT_IMPLEMENTED"
    assert calls == []


def test_real_pilot_cli_requires_explicit_confirmation_and_link_check(db, monkeypatch):
    from scripts.price_drop_leads_v2_real_pilot import run_action
    lead_id, campaign_id, _ = _approved_real_pilot_sample(db, monkeypatch)
    status = run_action(db, mode="status", code="6436", campaign_id=campaign_id)
    assert status["status"] == "FOUND"
    assert status["campaign_state"] == "APPROVED"
    assert status["writes"] == status["sends"] == 0
    calls = []
    result = run_action(db, mode="send-one", code="6436",
                        campaign_id=campaign_id, operator="Tester",
                        sender=lambda *args: calls.append(args))
    assert result["reason"] == "explicit_operator_confirmation_required"
    result = run_action(db, mode="send-one", code="6436",
                        campaign_id=campaign_id, operator="Tester",
                        confirm_action="SEND_6436",
                        sender=lambda *args: calls.append(args))
    assert result["reason"] == "published_link_not_verified"
    assert calls == []
    result = run_action(db, mode="send-one", code="6436",
                        campaign_id=campaign_id, operator="Tester",
                        confirm_action="SEND_6436", confirmed_live_link=True,
                        sender=lambda *args: calls.append(args))
    assert result["status"] == "REAL_SEND_LOCKED_PENDING_ACCEPTANCE"
    assert calls == []
