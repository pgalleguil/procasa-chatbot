"""End-to-end, provider-free V2 campaign audit: recipients, currency, replies, notices.

All WhatsApp operations are injected fakes. Only canonical leads is written.
"""
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from bson import ObjectId

from chatbot import price_drop_leads_v2 as v2
from chatbot.price_drop_campaign_classifier import classify_price_drop_reply
from chatbot.price_drop_pilot import context_instruction
from chatbot.price_drop_v2_customer_response import stabilize_v2_reply


PHONE = "+56930001111"
LEAD_ID = ObjectId("690000000000000000000924")
NOW = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)
CID = "audit_16479_pilot_20261109"
DROP = "2026-10-14T11:52:43+00:00"


@pytest.fixture
def db(monkeypatch):
    for flag in (v2.SEND_FLAG, v2.CONTEXT_FLAG, v2.APPROVED_ID_FLAG,
                 v2.REAL_ADMIN_NOTICE_FLAG, v2.TEST_NOTICE_FLAG):
        monkeypatch.delenv(flag, raising=False)
    d = mongomock.MongoClient()["URLS"]
    d["universo_cartera_prop360"].insert_one({
        "codigo": "16479", "disponible_prop360": True,
        "estado": {"oficina": "PROCASA SUCRE"},
        "tipo_operacion": {"venta": True, "precio_venta": {
            "precio_publicado": 152000000, "moneda_publicada": "CLP",
            "precio_uf": 3693.1,
        }},
        "publicaciones": {"yapo": {"publicaciones": {"V": {
            "code": "32248293", "url": "https://www.yapo.cl/32248293"
        }}}},
    })
    d["universo_cartera_prop360_historial"].insert_one({
        "codigo": "16479", "campo": "precio_publicado",
        "fecha": DROP, "moneda_anterior": "CLP", "moneda_nueva": "CLP",
        "monto_anterior": 160000000, "monto_nuevo": 152000000,
    })
    d["leads"].insert_one({
        "_id": LEAD_ID, "phone": PHONE,
        "lead_temperature_effective": "COLD",
        "pipeline_stage": "CONTACTED",
        "messages": [{
            "role": "user", "message_id": "original-customer-123",
            "content": "Estoy interesado en Yapo: https://www.yapo.cl/32248293",
            "timestamp": "2026-05-31T14:57:39-04:00",
        }],
    })
    return d


def params(**change):
    values = dict(
        lead_id=str(LEAD_ID), phone=PHONE, campaign_id=CID,
        property_code="16479", old_price_uf=None, new_price_uf=None,
        old_price_clp=160000000, new_price_clp=152000000, currency="CLP",
        test_mode=False, contact_basis_reviewed=True,
        contact_basis_type=v2.DIRECT_RESPONSE_BASIS_TYPE,
        contact_basis="Consulta previa verificada sobre la misma propiedad",
    )
    values.update(change)
    return values


@pytest.mark.parametrize("text,label", [
    ("Sí, me interesa", "INTERES_DECLARADO"),
    ("Quisiera agendar una visita", "PRIORIDAD_COMERCIAL"),
    ("Quiero presentar una oferta", "PRIORIDAD_COMERCIAL"),
    ("Busco otras propiedades", "BUSCA_ALTERNATIVAS"),
    ("¿Tienen otros departamentos?", "BUSCA_ALTERNATIVAS"),
    ("No me interesa esta, pero quiero otras opciones", "BUSCA_ALTERNATIVAS"),
    ("No me interesa", "RECHAZADO"),
    ("¿Cuánto bajó de precio?", "CONSULTA_INFORMATIVA"),
    ("No me envíen más mensajes", "BAJA"),
    ("Ya encontré propiedad, gracias", "BAJA"),
])
def test_campaign_intent_matrix(text, label):
    assert classify_price_drop_reply(text).label == label


def test_clp_preview_and_audited_lead_record_are_durable_but_no_send(db, monkeypatch):
    preview = v2.preview_one(db, **params())
    assert preview["currency"] == "CLP"
    assert preview["inquiry_evidence_at"] < preview["price_drop_at"]
    assert preview["inquiry_message_id"] == "original-customer-123"
    assert "$160.000.000" in preview["message"]
    assert "$152.000.000" in preview["message"]
    assert "$8.000.000" in preview["message"]
    assert "UF" not in preview["message"]
    assert v2.prepare_one(db, now=NOW, **params())["status"] == "PREPARED"
    stored = db["leads"].find_one({"_id": LEAD_ID})["price_drop_v2"]["campaigns"][0]
    assert stored["status"] == "PREPARED"
    assert stored["phone_normalized"] == PHONE
    assert stored["inquiry_message_id"] == "original-customer-123"
    assert stored["prepared_at"].replace(tzinfo=timezone.utc) == NOW
    assert stored["contact_basis_reviewed"] is True
    assert stored["contact_basis_type"] == v2.DIRECT_RESPONSE_BASIS_TYPE
    assert stored["contact_basis"]
    assert "consent_reviewed" not in stored
    assert "consent_basis" not in stored
    assert v2.approve_one(db, lead_id=str(LEAD_ID), campaign_id=CID,
                          operator="Supervisor de prueba", now=NOW)["status"] == "APPROVED"
    stored = db["leads"].find_one({"_id": LEAD_ID})["price_drop_v2"]["campaigns"][0]
    assert stored["approved_at"].replace(tzinfo=timezone.utc) == NOW
    assert stored["approved_by"] == "Supervisor de prueba"
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CID)
    calls = []
    result = v2.send_one(db, lead_id=str(LEAD_ID), campaign_id=CID,
                         sender=lambda *a: calls.append(a) or {
                             "success": True, "provider_message_id": "mock-audit-send",
                             "delivery_status": "accepted",
                         }, now=NOW)
    assert result["status"] == "SENT"
    assert len(calls) == 1


def test_clp_qa_context_and_conversational_reply_keep_clp(db, monkeypatch):
    monkeypatch.setenv(v2.TEST_PHONE_FLAG, PHONE)
    request = params(test_mode=True, contact_basis_reviewed=False,
                     contact_basis_type="", contact_basis="")
    v2.prepare_one(db, now=NOW, **request)
    v2.approve_one(db, lead_id=str(LEAD_ID), campaign_id=CID,
                   operator="QA", now=NOW)
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CID)
    result = v2.send_one(
        db, lead_id=str(LEAD_ID), campaign_id=CID,
        sender=lambda *a: {"success": True, "provider_message_id": "clp-accepted",
                           "delivery_status": "accepted"}, now=NOW)
    assert result["status"] == "SENT"
    ctx = v2.active_context(db, phone=PHONE, message="me interesa",
                            now=NOW + timedelta(minutes=1))
    assert ctx["currency"] == "CLP"
    assert ctx["new_price_clp"] == 152000000
    instruction = context_instruction(ctx)
    assert "$160.000.000" in instruction
    assert "$152.000.000" in instruction
    assert "UF 152" not in instruction
    reply = stabilize_v2_reply("me interesa", "Respuesta comercial", context=ctx)
    assert "$152.000.000" in reply
    assert "UF" not in reply
    from chatbot.public_property_facts import answer_public_property_question
    prop = db["universo_cartera_prop360"].find_one({"codigo": "16479"})
    price_answer = answer_public_property_question(
        "¿Cuánto bajó el precio?", prop, campaign_context=ctx)
    assert "$8.000.000" in price_answer
    assert "$160.000.000" in price_answer
    assert "$152.000.000" in price_answer
    assert " UF" not in price_answer


def _approved_simulation(db, monkeypatch):
    monkeypatch.setenv(v2.TEST_PHONE_FLAG, PHONE)
    v2.prepare_one(db, now=NOW, **params(test_mode=True))
    v2.approve_one(db, lead_id=str(LEAD_ID), campaign_id=CID,
                   operator="QA", now=NOW)
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CID)
    result = v2.send_one(
        db, lead_id=str(LEAD_ID), campaign_id=CID,
        sender=lambda *a: {"success": True, "provider_message_id": "clp-accepted"},
        now=NOW)
    assert result["status"] == "SENT"


def test_all_replies_audited_deduplicated_and_decline_cancels_notice(db, monkeypatch):
    _approved_simulation(db, monkeypatch)
    reply1 = v2.record_inbound(db, phone=PHONE, provider_message_id="r1",
                              text="Sí, me interesa", now=NOW+timedelta(minutes=3))
    assert reply1["classification"] == "INTERES_DECLARADO"
    reply2 = v2.record_inbound(db, phone=PHONE, provider_message_id="r2",
                              text="No esta, busco otras casas", now=NOW+timedelta(minutes=4))
    assert reply2["classification"] == "BUSCA_ALTERNATIVAS"
    assert v2.record_inbound(db, phone=PHONE, provider_message_id="r2",
                             text="No esta, busco otras casas",
                             now=NOW+timedelta(minutes=5))["status"] == "DUPLICATE_IGNORED"
    row = db["leads"].find_one({"_id": LEAD_ID})["price_drop_v2"]["campaigns"][0]
    assert row["reply_count"] == 2
    events = db["leads"].find_one({"_id": LEAD_ID})["price_drop_v2"]["reply_events"]
    assert len(events) == 2
    assert events[0]["campaign_id"] == CID
    assert row["first_response_at"].replace(tzinfo=timezone.utc) == NOW+timedelta(minutes=3)
    assert events[1]["classification"]["alternatives_requested"] is True
    assert row["first_positive_reply_text"] == "Sí, me interesa"
    assert row["last_positive_reply_text"] == "No esta, busco otras casas"
    assert row["notifications"]["administrator"]["status"] == "PENDING_SIMULATION"
    decline = v2.record_inbound(db, phone=PHONE, provider_message_id="r3",
                                text="No me interesa", now=NOW+timedelta(minutes=6))
    assert decline["classification"] == "RECHAZADO"
    row = db["leads"].find_one({"_id": LEAD_ID})["price_drop_v2"]["campaigns"][0]
    assert row["status"] == "DECLINED"
    assert row["notifications"]["administrator"]["status"] == "CANCELED"
    assert row["reply_count"] == 3
    assert len(db["leads"].find_one({"_id": LEAD_ID})["price_drop_v2"]["reply_events"]) == 3
    assert v2.active_context(db, phone=PHONE, message="hola",
                             now=NOW+timedelta(minutes=7)) is None


def test_real_admin_notice_gated_exact_admin_only_and_once(db, monkeypatch):
    from chatbot import lead_router
    monkeypatch.setattr(lead_router, "build_secure_crm_url",
                        lambda lead: "https://example.invalid/secure-lead")
    db["leads"].update_one({"_id": LEAD_ID}, {"$set": {"price_drop_v2": {
        "campaigns": [{
            "campaign_id": CID, "property_code": "16479",
            "old_price_clp": 160000000, "new_price_clp": 152000000,
            "currency": "CLP", "test_mode": False, "status": "RESPONDED",
            "provider_message_id": "accepted-customer",
            "has_positive_response": True,
            "last_positive_reply_text": "Quiero conocer otras opciones en Lo Prado",
            "last_classification": {"label": "BUSCA_ALTERNATIVAS",
                                    "reason": "other_property_search"},
            "last_customer_reply_at": NOW,
            "notifications": {
                "administrator": {"status": "PENDING_HUMAN_REVIEW"},
                "property_executive": {"status": "FROZEN_ADMIN_ONLY"},
            },
        }],
    }}})
    calls = []
    sender = lambda phone, message: (calls.append((phone, message)) or
                                     {"success": True, "provider_message_id": "admin-accepted",
                                      "delivery_status": "accepted"})
    assert v2.send_real_admin_notice_once(db, sender=sender,
                                         now=NOW+timedelta(minutes=3))["status"] == "DISABLED"
    monkeypatch.setenv(v2.REAL_ADMIN_NOTICE_FLAG, "true")
    monkeypatch.setenv(v2.ADMIN_PHONE_FLAG, "+56939998888")
    assert v2.send_real_admin_notice_once(db, sender=sender,
                                         now=NOW+timedelta(minutes=3))["status"] == "ADMIN_PHONE_NOT_ALLOWLISTED"
    monkeypatch.setenv(v2.ADMIN_PHONE_FLAG, v2.APPROVED_ADMIN_PHONE)
    response = v2.send_real_admin_notice_once(db, sender=sender,
                                              now=NOW+timedelta(minutes=3))
    assert response["status"] == "SENT"
    assert len(calls) == 1
    assert calls[0][0] == v2.APPROVED_ADMIN_PHONE
    assert PHONE in calls[0][1]
    assert "16479" in calls[0][1]
    assert "$160.000.000" in calls[0][1]
    assert "$152.000.000" in calls[0][1]
    assert "Quiero conocer otras opciones" in calls[0][1]
    assert "https://example.invalid/secure-lead" in calls[0][1]
    row = db["leads"].find_one({"_id": LEAD_ID})["price_drop_v2"]["campaigns"][0]
    assert row["notifications"]["administrator"]["status"] == "SENT"
    assert row["notifications"]["administrator"]["provider_message_id"] == "admin-accepted"
    assert row["notifications"]["administrator"]["sent_at"].replace(tzinfo=timezone.utc) == NOW+timedelta(minutes=3)
    assert row["notifications"]["property_executive"]["status"] == "FROZEN_ADMIN_ONLY"
    assert v2.send_real_admin_notice_once(db, sender=sender,
                                         now=NOW+timedelta(minutes=5))["status"] == "IDLE"
    assert len(calls) == 1


def test_no_admin_notification_after_decline(db, monkeypatch):
    monkeypatch.setenv(v2.REAL_ADMIN_NOTICE_FLAG, "true")
    monkeypatch.setenv(v2.ADMIN_PHONE_FLAG, v2.APPROVED_ADMIN_PHONE)
    db["leads"].update_one({"_id": LEAD_ID}, {"$set": {"price_drop_v2": {
        "campaigns": [{
            "campaign_id": CID, "property_code": "16479",
            "currency": "CLP", "old_price_clp": 160000000,
            "new_price_clp": 152000000,
            "test_mode": False, "status": "DECLINED",
            "provider_message_id": "accepted",
            "has_positive_response": True,
            "notifications": {"administrator": {"status": "PENDING_HUMAN_REVIEW"}},
        }],
    }}})
    calls = []
    assert v2.send_real_admin_notice_once(
        db, sender=lambda *x: calls.append(x),
        now=NOW+timedelta(minutes=5))["status"] == "IDLE"
    assert calls == []


def test_optout_and_completed_search_send_one_admin_info_notice_only(db, monkeypatch):
    from chatbot import lead_router

    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setattr(lead_router, "build_secure_crm_url",
                        lambda lead: "https://example.invalid/secure-lead")
    db["leads"].update_one({"_id": LEAD_ID}, {"$set": {"price_drop_v2": {
        "campaigns": [{
            "campaign_id": CID, "event_id": CID, "property_code": "16479",
            "currency": "CLP", "old_price_clp": 160000000,
            "new_price_clp": 152000000, "test_mode": False,
            "status": "SENT", "provider_message_id": "accepted-customer",
            "price_drop_at": datetime.fromisoformat(DROP),
            "sent_at": NOW, "expires_at": NOW + timedelta(days=7),
            "phone_normalized": PHONE,
            "outbound_message": "Aviso anterior de rebaja para la propiedad 16479",
            "notifications": {
                "administrator": {"status": "NOT_TRIGGERED"},
                "property_executive": {"status": "FROZEN_ADMIN_ONLY"},
            },
        }],
    }}})
    reply = v2.record_inbound(
        db, phone=PHONE, provider_message_id="reply-stop-1",
        text="No me envien mas mensajes", now=NOW + timedelta(minutes=2),
    )
    assert reply["classification"] == "BAJA"
    lead = db["leads"].find_one({"_id": LEAD_ID})
    campaign = lead["price_drop_v2"]["campaigns"][0]
    assert campaign["status"] == "OPTED_OUT"
    assert lead["price_drop_v2"]["opted_out"] is True
    assert lead["price_drop_campaign_v1"]["opted_out"] is True
    assert campaign["notifications"]["administrator_info"]["status"] == "PENDING_INFO"

    monkeypatch.setenv(v2.REAL_ADMIN_NOTICE_FLAG, "true")
    monkeypatch.setenv(v2.ADMIN_PHONE_FLAG, v2.APPROVED_ADMIN_PHONE)
    calls = []
    sender = lambda phone, message: calls.append((phone, message)) or {
        "success": True, "provider_message_id": "admin-info-1", "delivery_status": "accepted",
    }
    notice = v2.send_real_admin_info_notice_once(
        db, sender=sender, now=NOW + timedelta(minutes=5),
    )
    assert notice["status"] == "SENT"
    assert len(calls) == 1
    assert calls[0][0] == v2.APPROVED_ADMIN_PHONE
    assert PHONE in calls[0][1] and "16479" in calls[0][1]
    assert "Solicita no recibir más mensajes" in calls[0][1]
    assert "No me envien mas mensajes" in calls[0][1]
    assert "Exclusión global aplicada" in calls[0][1]
    assert "https://example.invalid/secure-lead" in calls[0][1]
    assert v2.send_real_admin_notice_once(db, sender=sender, now=NOW)["status"] == "IDLE"
    assert v2.send_real_admin_info_notice_once(db, sender=sender, now=NOW)["status"] == "IDLE"
    assert len(calls) == 1
