"""Isolated acceptance tests for explicit future price-drop watch requests."""
from datetime import datetime, timezone
from pathlib import Path

import mongomock
from bson import ObjectId

from chatbot.price_drop_watch_request import (
    COLLECTION, asks_for_price_drop_notice, register_price_drop_watch,
)

NOW = datetime(2026, 11, 5, 21, 0, tzinfo=timezone.utc)


def test_real_client_request_is_recognized_without_false_marketing_opt_in():
    assert asks_for_price_drop_notice(
        "me interesa pero esta muy cara, favor me avisan cuando bajen de precio"
    )
    assert asks_for_price_drop_notice("Avísame si baja el precio de este departamento")
    assert asks_for_price_drop_notice("¿Me notifican si hay una rebaja?")
    assert not asks_for_price_drop_notice("me interesa pero está muy caro")
    assert not asks_for_price_drop_notice("¿Me avisas qué orientación tiene?")
    assert not asks_for_price_drop_notice("No me avisen cuando baje de precio")
    assert not asks_for_price_drop_notice("No quiero que me avisen si baja")


def test_record_customer_request_once_without_campaign_send():
    db = mongomock.MongoClient()["URLS"]
    lead_id = ObjectId("690000000000000000000111")
    db["leads"].insert_one({"_id": lead_id, "phone": "+56911112222"})
    db["universo_cartera_prop360"].insert_one({"codigo": "17184"})

    params = dict(
        lead_id=lead_id,
        phone="+56911112222",
        property_code="17184",
        customer_text="me interesa pero esta muy cara, favor me avisan cuando bajen de precio",
        now=NOW,
    )
    first = register_price_drop_watch(db, **params)
    again = register_price_drop_watch(db, **params)
    assert first == {"status": "RECORDED", "property_code": "17184"}
    assert again == {"status": "ALREADY_RECORDED", "property_code": "17184"}

    docs = list(db[COLLECTION].find())
    assert len(docs) == 1
    d = docs[0]
    assert d["lead_id"] == lead_id
    assert d["status"] == "PENDING_HUMAN_REVIEW"
    assert d["requested_at"].replace(tzinfo=timezone.utc) == NOW
    assert d["automatic_sending_enabled"] is False
    assert db["crm_price_drop_campaigns_v1"].count_documents({}) == 0
    assert db["crm_price_drop_campaign_dispatches_v1"].count_documents({}) == 0
    assert db["leads"].count_documents({}) == 1


def test_no_record_without_verified_lead_property_or_explicit_request():
    db = mongomock.MongoClient()["URLS"]
    lead_id = ObjectId("690000000000000000000111")
    base = dict(
        lead_id=lead_id, phone="+56911112222", property_code="17184",
        customer_text="me avisan cuando baje de precio", now=NOW,
    )
    assert register_price_drop_watch(db, **base)["status"] == "LEAD_NOT_VERIFIED"
    db["leads"].insert_one({"_id": lead_id, "phone": "+56911112222"})
    assert register_price_drop_watch(db, **base)["status"] == "PROPERTY_NOT_VERIFIED"
    db["universo_cartera_prop360"].insert_one({"codigo": "17184"})
    assert register_price_drop_watch(db, **{**base, "customer_text": "me interesa"})["status"] == "NOT_REQUESTED"
    assert register_price_drop_watch(db, **{**base, "property_code": ""})["status"] == "MISSING_CONTEXT"
    assert db[COLLECTION].count_documents({}) == 0


def test_core_handles_request_before_generic_rag_and_llm():
    code = (Path(__file__).parents[1] / "chatbot" / "core.py").read_text(encoding="utf-8")
    marker = code.index("if asks_for_price_drop_notice(original_message):")
    rag = code.index("# 5. PREPARACIÓN DE MESSAGES PARA GROK")
    assert marker < rag
    assert "register_price_drop_watch" in code[marker:rag]
    assert "No es una alerta automática garantizada" in code[marker:rag]
    assert "solicitud_aviso_rebaja_registrada" in code[marker:rag]
