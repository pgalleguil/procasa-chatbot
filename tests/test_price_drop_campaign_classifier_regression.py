"""Regression coverage for opt-outs and mixed commercial campaign replies."""
from datetime import datetime, timedelta, timezone

import asyncio
import mongomock
import pytest
from bson import ObjectId

from chatbot import price_drop_leads_v2 as v2
from chatbot.chatbot_queue import (
    ST_RESPONDED, create_inbound_job, ensure_queue_indexes, process_one_batch,
)
from chatbot.conversation_policy import (
    alternative_requested, extract_visit_preference, is_explicit_visit_intent,
    property_rejected, should_offer_visit_data, suppress_negated_call_intent,
)
from chatbot.price_drop_campaign_classifier import (
    ALTERNATIVES, DECLINED, INFO, INTEREST, PRIORITY, UNSUBSCRIBED,
    classify_price_drop_reply, has_do_not_call_preference,
)
from chatbot.price_drop_pilot import context_instruction


NOW = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)
PHONE = "+56930001111"
LEAD_ID = ObjectId("690000000000000000000924")
CAMPAIGN_ID = "classification_regression_6436"


# 56 Spanish messages include paraphrases, orthographic errors, negations,
# mixed requests, and deliberate hard negatives from the independent audit.
CLASSIFICATION_CASES = [
    ("No quiero recibir más información", UNSUBSCRIBED),
    ("No deseo recibir mensajes", UNSUBSCRIBED),
    ("No quiero que me sigan escribiendo", UNSUBSCRIBED),
    ("No me sigan escribiendo", UNSUBSCRIBED),
    ("No me contacten más", UNSUBSCRIBED),
    ("No me manden más información", UNSUBSCRIBED),
    ("Dejen de escribirme", UNSUBSCRIBED),
    ("Por favor, no me escriban más", UNSUBSCRIBED),
    ("Sáquenme de la lista", UNSUBSCRIBED),
    ("No kiero resivir mas infomacion", UNSUBSCRIBED),
    ("No me sigan escriviendo", UNSUBSCRIBED),
    ("No quiero que me vuelvan a escribir", UNSUBSCRIBED),
    ("Prefiero que no me contacten", UNSUBSCRIBED),
    ("No quiero continuar la conversación", UNSUBSCRIBED),
    ("No me interesa seguir recibiendo ofertas", UNSUBSCRIBED),
    ("Cancela mi suscripción", UNSUBSCRIBED),
    ("Dejen de mandarme promociones", UNSUBSCRIBED),
    ("No quiero que me escriban más", UNSUBSCRIBED),
    ("No kiero q me manden mensajes", UNSUBSCRIBED),
    ("No deseo recibir actualizaciones", UNSUBSCRIBED),
    ("Basta ya de mensajes", UNSUBSCRIBED),
    ("No quiero que me llamen", INFO),
    ("No me llamen más", INFO),
    ("Prefiero no recibir llamadas", INFO),
    ("Sin llamadas, por favor", INFO),
    ("Sí me interesa, pero no quiero que me llamen", INTEREST),
    ("Me interesa la propiedad, prefiero que no me llamen", INTEREST),
    ("No quiero recibir llamadas, pero sí mensajes por WhatsApp", INFO),
    ("No me interesa visitar, pero quiero saber los gastos comunes", INFO),
    ("No quiero coordinar visita todavía; ¿cuánto son los gastos?", INFO),
    ("No estoy interesado en visitar, pero cuánto cuesta?", INFO),
    ("Prefiero no agendar, sí quiero saber orientación", INFO),
    ("No me interesa ver la propiedad ahora; cuéntame los detalles", INFO),
    ("No me interesa la propiedad", DECLINED),
    ("No me interesa", DECLINED),
    ("No gracias", DECLINED),
    ("Encontré una vivienda y terminé mi búsqueda", UNSUBSCRIBED),
    ("Encontré vivienda, ya cerré mi búsqueda", UNSUBSCRIBED),
    ("Ya encontré una vivienda y finalicé la búsqueda", UNSUBSCRIBED),
    ("Finalmente compré una casa", UNSUBSCRIBED),
    ("Conseguí un departamento y terminé mi búsqueda", UNSUBSCRIBED),
    ("No encontré una vivienda", "SIN_SENAL"),
    ("Todavía no he encontrado vivienda", "SIN_SENAL"),
    ("¿Y si ya encontré una casa?", INFO),
    ("Estoy buscando una casa", "SIN_SENAL"),
    ("Quiero presentar una oferta de UF 2.500", PRIORITY),
    ("Te ofrezco 2.500 UF", PRIORITY),
    ("Podría pagar 2.500 UF; ¿lo aceptarían?", PRIORITY),
    ("Mi oferta sería $145.000.000", PRIORITY),
    ("¿Aceptan ofertas?", INFO),
    ("No quiero hacer una oferta", "SIN_SENAL"),
    ("Busco otras propiedades", ALTERNATIVES),
    ("¿Tienen otros departamentos?", ALTERNATIVES),
    ("Quiero buscar en otra comuna", ALTERNATIVES),
    ("Busco algo en Ñuñoa", ALTERNATIVES),
    ("¿Tienen algo en Ñuñoa?", ALTERNATIVES),
    ("No busco otras comunas", "SIN_SENAL"),
    ("No quiero buscar otra propiedad", "SIN_SENAL"),
    ("No me interesa ver otras propiedades", "SIN_SENAL"),
    ("Me interesa esta, pero no necesito alternativas", INTEREST),
    ("Quiero ver otras propiedades en Providencia", ALTERNATIVES),
    ("¿Y en La Reina?", ALTERNATIVES),
    ("Quiero visitar mañana", PRIORITY),
    ("¿Se puede visitar la propiedad?", PRIORITY),
    ("Me interesa, llámame mañana", PRIORITY),
    ("¿Me puedes llamar?", PRIORITY),
]
assert len(CLASSIFICATION_CASES) == 66


@pytest.mark.parametrize("message,expected", CLASSIFICATION_CASES)
def test_independent_audit_message_matrix(message, expected):
    assert classify_price_drop_reply(message).label == expected


@pytest.mark.parametrize("message,expected_label,expected_intent", [
    ("Me puede llamar una asesora", PRIORITY, "ASK_CONTACT"),
    ("Estoy interesado en coordinar una visita", PRIORITY, "ASK_VISIT"),
    ("¿Podemos ver el departamento?", PRIORITY, "ASK_VISIT"),
    ("No me interesa, no quiero visita", DECLINED, "OTHER"),
    ("No me interesa una visita", DECLINED, "OTHER"),
    ("No quiero agendar, gracias", DECLINED, "OTHER"),
    ("No quiero coordinar una visita", DECLINED, "OTHER"),
    ("No quiero coordinar visita ahora", INFO, "ASK_INFO"),
    ("No puedo visitar mañana", "SIN_SENAL", "OTHER"),
])
def test_campaign_classifier_ci_regressions(message, expected_label, expected_intent):
    result = classify_price_drop_reply(message)
    assert result.label == expected_label
    assert result.intent == expected_intent
    if expected_intent == "ASK_VISIT":
        assert result.explicit_visit is True
    if message == "No puedo visitar mañana":
        assert result.visit_declined is False


def test_standalone_visit_refusal_does_not_reject_property_or_mixed_conversation():
    assert not property_rejected("No me interesa una visita")
    mixed = "No me interesa visitar, pero quiero saber los gastos comunes"
    result = classify_price_drop_reply(mixed)
    assert result.label == INFO
    assert result.visit_declined is True
    assert not property_rejected(mixed)


def _db_with_sent_campaign():
    db = mongomock.MongoClient()["URLS"]
    db["universo_cartera_prop360"].insert_one({
        "codigo": "6436", "disponible_prop360": True,
        "estado": {"oficina": "PROCASA SUCRE"},
        "tipo_operacion": {"venta": True, "precio_venta": {
            "precio_uf": 2690, "precio_publicado": 2690,
            "moneda_publicada": "UF", "gastos_comunes": 98000,
        }},
    })
    db["universo_cartera_prop360_historial"].insert_one({
        "codigo": "6436", "campo": "precio_publicado",
        "fecha": (NOW - timedelta(days=3)).isoformat(),
        "moneda_anterior": "UF", "moneda_nueva": "UF",
        "monto_anterior": 2790, "monto_nuevo": 2690,
    })
    db["leads"].insert_one({
        "_id": LEAD_ID, "phone": PHONE,
        "lead_temperature_effective": "NORMAL",
        "messages": [{"role": "user", "content": "Consulté por el código 6436"}],
        "price_drop_v2": {"campaigns": [{
            "campaign_id": CAMPAIGN_ID,
            "property_code": "6436",
            "phone_normalized": PHONE,
            "old_price_uf": 2790, "new_price_uf": 2690,
            "currency": "UF", "status": "SENT", "test_mode": False,
            "provider_message_id": "customer-outbound-6436",
            "outbound_message": "Aviso de rebaja 6436",
            "sent_at": NOW - timedelta(minutes=10),
            "expires_at": NOW + timedelta(days=7),
            "reply_count": 0,
            "notifications": {
                "administrator": {"status": "NOT_TRIGGERED"},
                "property_executive": {"status": "FROZEN_ADMIN_ONLY"},
            },
        }]},
    })
    return db


def test_mixed_preferences_are_persisted_without_false_escalation(monkeypatch):
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    db = _db_with_sent_campaign()
    interest = v2.record_inbound(
        db, phone=PHONE, provider_message_id="reply-no-call-1",
        text="Sí me interesa, pero no quiero que me llamen", now=NOW,
    )
    assert interest["status"] == "ATTRIBUTED"
    assert interest["classification"] == INTEREST
    lead = db["leads"].find_one({"_id": LEAD_ID})
    campaign = lead["price_drop_v2"]["campaigns"][0]
    assert campaign["status"] == "RESPONDED"
    assert campaign["last_classification"]["human_requested"] is False
    assert campaign["last_classification"]["do_not_call"] is True
    assert lead["price_drop_v2"]["contact_preferences"]["do_not_call"] is True
    context = v2.active_context(
        db, phone=PHONE, message="¿Cuánto cuestan los gastos comunes?",
        now=NOW + timedelta(minutes=1),
    )
    assert context["customer_preferences"]["do_not_call"] is True
    assert "no pedir ni proponer llamadas" in context_instruction(context)

    partial = v2.record_inbound(
        db, phone=PHONE, provider_message_id="reply-visit-no-1",
        text="No me interesa visitar, pero quiero saber los gastos comunes",
        now=NOW + timedelta(minutes=2),
    )
    assert partial["status"] == "ATTRIBUTED"
    assert partial["classification"] == INFO
    lead = db["leads"].find_one({"_id": LEAD_ID})
    campaign = lead["price_drop_v2"]["campaigns"][0]
    assert campaign["status"] == "RESPONDED"
    assert lead.get("conversation_status") != "STOPPED_BY_CLIENT"
    assert campaign["last_classification"]["visit_requested"] is False
    assert campaign["customer_preferences"]["visit_declined"] is True
    context = v2.active_context(db, phone=PHONE, message="gastos comunes",
                                now=NOW + timedelta(minutes=3))
    assert context["campaign_id"] == CAMPAIGN_ID
    assert context["customer_preferences"]["visit_declined"] is True
    instruction = context_instruction(context)
    assert "no volver a proponer ni preguntar por visitas" in instruction
    assert not is_explicit_visit_intent("No me interesa visitar, pero quiero gastos comunes")
    assert not should_offer_visit_data("No me interesa visitar, pero quiero gastos comunes")
    assert extract_visit_preference("No me interesa visitar, mañana no puedo") is None
    assert not property_rejected("No me interesa visitar, pero quiero saber gastos comunes")


def test_call_request_is_not_inferred_from_negation_and_explicit_request_can_revoke(monkeypatch):
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    db = _db_with_sent_campaign()
    first = v2.record_inbound(
        db, phone=PHONE, provider_message_id="no-call-pref-1",
        text="Me interesa, prefiero no recibir llamadas", now=NOW,
    )
    assert first["classification"] == INTEREST
    second = v2.record_inbound(
        db, phone=PHONE, provider_message_id="call-asked-2",
        text="Sí, ahora sí me puedes llamar", now=NOW + timedelta(minutes=1),
    )
    assert second["classification"] == PRIORITY
    lead = db["leads"].find_one({"_id": LEAD_ID})
    assert lead["price_drop_v2"]["contact_preferences"]["do_not_call"] is False
    assert lead["price_drop_v2"]["campaigns"][0]["customer_preferences"]["do_not_call"] is False


def test_model_call_label_is_suppressed_by_negative_call_preference():
    assert suppress_negated_call_intent(
        "contacto_directo", "Sí me interesa, pero no quiero que me llamen",
    ) == "consulta_general"
    assert suppress_negated_call_intent(
        "contacto_directo", "Me interesa, llámame mañana",
    ) == "contacto_directo"
    assert alternative_requested("Quiero ver otras propiedades en Providencia")
    assert not is_explicit_visit_intent("Quiero ver otras propiedades en Providencia")
    assert not should_offer_visit_data("Quiero ver otras propiedades en Providencia")


def test_baja_and_completed_search_suppress_globally_and_info_notice_is_one_shot(monkeypatch):
    from chatbot import lead_router

    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.REAL_ADMIN_NOTICE_FLAG, "true")
    monkeypatch.setenv(v2.ADMIN_PHONE_FLAG, v2.APPROVED_ADMIN_PHONE)
    monkeypatch.setattr(lead_router, "build_secure_crm_url",
                        lambda lead: "https://example.invalid/secure-lead")
    db = _db_with_sent_campaign()
    result = v2.record_inbound(
        db, phone=PHONE, provider_message_id="optout-info-1",
        text="No kiero resivir mas infomacion", now=NOW,
    )
    assert result["classification"] == UNSUBSCRIBED
    lead = db["leads"].find_one({"_id": LEAD_ID})
    campaign = lead["price_drop_v2"]["campaigns"][0]
    assert lead["price_drop_v2"]["opted_out"] is True
    assert lead["price_drop_campaign_v1"]["opted_out"] is True
    assert lead["conversation_status"] == "STOPPED_BY_CLIENT"
    assert v2.active_context(db, phone=PHONE, message="hola",
                             now=NOW + timedelta(minutes=1)) is None
    assert v2.record_inbound(
        db, phone=PHONE, provider_message_id="optout-info-1",
        text="No kiero resivir mas infomacion", now=NOW + timedelta(minutes=2),
    )["status"] == "DUPLICATE_IGNORED"
    assert len(lead["price_drop_v2"]["reply_events"]) == 1

    provider_calls = []
    sender = lambda phone, message: provider_calls.append((phone, message)) or {
        "success": True, "provider_message_id": "mock-admin-info-1",
    }
    notice = v2.send_real_admin_info_notice_once(
        db, sender=sender, now=NOW + timedelta(minutes=5),
    )
    assert notice["status"] == "SENT"
    assert provider_calls[0][0] == v2.APPROVED_ADMIN_PHONE
    assert PHONE in provider_calls[0][1]
    assert "No contactar nuevamente al cliente" in provider_calls[0][1]
    assert v2.send_real_admin_info_notice_once(
        db, sender=sender, now=NOW + timedelta(minutes=10),
    )["status"] == "IDLE"
    assert len(provider_calls) == 1
    assert campaign["status"] == "OPTED_OUT"


def test_real_admin_positive_notice_preserves_no_call_preference_and_is_once(monkeypatch):
    from chatbot import lead_router

    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.REAL_ADMIN_NOTICE_FLAG, "true")
    monkeypatch.setenv(v2.ADMIN_PHONE_FLAG, v2.APPROVED_ADMIN_PHONE)
    monkeypatch.setattr(lead_router, "build_secure_crm_url",
                        lambda lead: "https://example.invalid/secure-lead")
    db = _db_with_sent_campaign()
    v2.record_inbound(
        db, phone=PHONE, provider_message_id="positive-no-call-1",
        text="Sí me interesa, pero no quiero que me llamen", now=NOW,
    )
    provider_calls = []
    sender = lambda phone, message: provider_calls.append((phone, message)) or {
        "success": True, "provider_message_id": "mock-admin-positive-1",
    }
    result = v2.send_real_admin_notice_once(
        db, sender=sender, now=NOW + timedelta(minutes=5),
    )
    assert result["status"] == "SENT"
    assert provider_calls[0][0] == v2.APPROVED_ADMIN_PHONE
    assert PHONE in provider_calls[0][1]
    assert "Preferencia vigente: no llamar al cliente" in provider_calls[0][1]
    assert v2.send_real_admin_notice_once(
        db, sender=sender, now=NOW + timedelta(minutes=6),
    )["status"] == "IDLE"
    assert len(provider_calls) == 1


def test_global_optout_skips_commercial_intake_and_customer_sender(monkeypatch):
    from chatbot import commercial_intake
    from chatbot import chatbot_queue as queue

    db = mongomock.MongoClient()["URLS"]
    db["leads"].insert_one({
        "_id": LEAD_ID, "phone": PHONE,
        "conversation_status": "STOPPED_BY_CLIENT",
        "price_drop_v2": {"opted_out": True},
    })
    calls = []
    monkeypatch.setattr(commercial_intake, "process_inbound",
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    ensure_queue_indexes(db)
    create_inbound_job(
        db, inbound_provider_message_id="queue-optout-1", phone=PHONE,
        text="No me escriban más", received_at=NOW - timedelta(seconds=30),
    )

    async def llm(phone, text):
        return ""

    async def sender(phone, text):
        calls.append((phone, text))
        return {"success": True, "provider_message_id": "unexpected"}

    result = asyncio.run(process_one_batch(
        db, worker_id="optout-worker", llm=llm, sender=sender, now=NOW,
    ))
    assert result["state"] == ST_RESPONDED
    assert calls == []
