"""P0: a recovered customer who already found a property must not be contacted.

No live provider sends, account lookups or production writes.
"""
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from bson import ObjectId

from chatbot.price_drop_campaign_classifier import (
    UNSUBSCRIBED, classify_price_drop_reply, lead_must_not_be_contacted,
)
from chatbot import price_drop_leads_v2 as v2
from chatbot.price_drop_campaign_real import _check_recipient, CampaignBlocked


PHONE = "+56983219804"
CAMPAIGN_ID = "test_leads_6436_20261109_9804_full"
NOW = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)
FOUND = "QUE LASTIMA, YA ENCONTRE PROPIEDAD , GRACIAS"


@pytest.mark.parametrize("message", [
    "QUE LASTIMA, YA ENCONTRE PROPIEDAD , GRACIAS",
    "Ya encontré una propiedad, muchas gracias",
    "Ya compré una casa",
    "Finalmente conseguí otro departamento",
    "Ya tengo mi casa, gracias",
])
def test_completed_search_is_terminal_campaign_contact(message):
    outcome = classify_price_drop_reply(message)
    assert outcome.label == UNSUBSCRIBED
    assert outcome.reason == "already_found_property"
    assert outcome.positive is False


def test_negated_or_hypothetical_completed_search_not_false_positive():
    for message in (
        "Todavía no he encontrado propiedad",
        "Estoy buscando una propiedad",
        "¿Y si ya encontré una casa?",
    ):
        assert classify_price_drop_reply(message).label != UNSUBSCRIBED


def test_legacy_misclassified_9804_history_is_suppressed_without_migration():
    lead = {"phone": PHONE, "price_drop_v2": {"campaigns": [{
        "status": "RESPONDED",
        "last_customer_text": FOUND,
        "last_classification": {"label": "SIN_SENAL"},
    }]}}
    assert lead_must_not_be_contacted(lead)
    assert lead_must_not_be_contacted({
        "phone": PHONE, "messages": [{"role": "user", "content": FOUND}],
    })
    assert not lead_must_not_be_contacted({
        "messages": [{"role": "assistant", "content": FOUND}],
    })


def _db_with_campaign():
    db = mongomock.MongoClient()["URLS"]
    lead_id = ObjectId("690000000000000000000643")
    db["leads"].insert_one({
        "_id": lead_id,
        "phone": PHONE,
        "messages": [{"role": "user", "content": "Consulté el departamento 6436",
                      "timestamp": "2026-10-01T10:00:00-03:00"}],
        "price_drop_v2": {"campaigns": [{
            "campaign_id": CAMPAIGN_ID,
            "property_code": "6436",
            "phone_normalized": PHONE,
            "old_price_uf": 2790,
            "new_price_uf": 2690,
            "status": "SENT",
            "test_mode": True,
            "provider_message_id": "accepted-provider-id",
            "outbound_message": "Aviso 6436",
            "sent_at": NOW - timedelta(minutes=10),
            "expires_at": NOW + timedelta(days=7),
            "reply_count": 1,
            "last_customer_text": "CUANTO HA BAJADO?",
            "last_classification": {"label": "CONSULTA_INFORMATIVA"},
            "notifications": {
                "administrator": {"status": "NOT_TRIGGERED"},
                "property_executive": {"status": "FROZEN_ADMIN_ONLY"},
            },
        }]}
    })
    return db, lead_id


def test_found_property_reply_persists_baja_and_prevents_future_context(monkeypatch):
    db, lead_id = _db_with_campaign()
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.TEST_PHONE_FLAG, PHONE)
    monkeypatch.setenv(v2.TEST_LEAD_ID_FLAG, str(lead_id))
    monkeypatch.setattr(v2, "_property_price", lambda *args: NOW - timedelta(days=1))
    before = v2.active_context(db, phone=PHONE, message=FOUND, now=NOW)
    assert before["campaign_id"] == CAMPAIGN_ID
    result = v2.record_inbound(
        db, phone=PHONE, provider_message_id="customer-found-1",
        text=FOUND, now=NOW,
    )
    assert result["status"] == "ATTRIBUTED"
    assert result["classification"] == "BAJA"
    lead = db["leads"].find_one({"_id": lead_id})
    campaign = lead["price_drop_v2"]["campaigns"][0]
    assert campaign["status"] == "OPTED_OUT"
    assert campaign["last_classification"]["reason"] == "already_found_property"
    assert lead["price_drop_v2"]["opted_out"] is True
    assert lead["price_drop_campaign_v1"]["opted_out"] is True
    assert lead["conversation_status"] == "STOPPED_BY_CLIENT"
    assert campaign["reply_count"] == 2
    assert v2.active_context(db, phone=PHONE, message="hola", now=NOW) is None


def test_existing_9804_reply_blocks_new_campaign_before_send(monkeypatch):
    db, lead_id = _db_with_campaign()
    db["leads"].update_one({"_id": lead_id}, {"$set": {
        "price_drop_v2.campaigns.0.last_customer_text": FOUND,
        "price_drop_v2.campaigns.0.status": "APPROVED",
    }})
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CAMPAIGN_ID)
    monkeypatch.setenv(v2.TEST_PHONE_FLAG, PHONE)
    monkeypatch.setenv(v2.TEST_LEAD_ID_FLAG, str(lead_id))
    sends = []
    result = v2.send_one(
        db, lead_id=str(lead_id), campaign_id=CAMPAIGN_ID,
        sender=lambda phone, text: sends.append(phone) or {
            "success": True, "provider_message_id": "should-not-send",
        },
    )
    assert result["status"] == "BLOCKED_OPTED_OUT"
    assert sends == []
    with pytest.raises(v2.PilotBlocked, match="lead_opted_out"):
        v2.preview_one(
            db, lead_id=str(lead_id), phone=PHONE,
            campaign_id="test_leads_6436_next_9804",
            property_code="6436", old_price_uf=2790, new_price_uf=2690,
            test_mode=True,
        )


def test_legacy_real_campaign_preflight_excludes_closed_search():
    db, lead_id = _db_with_campaign()
    db["leads"].update_one({"_id": lead_id}, {"$set": {
        "price_drop_v2.campaigns.0.last_customer_text": FOUND,
        "price_drop_v2.campaigns.0.status": "RESPONDED",
    }})
    with pytest.raises(CampaignBlocked, match="lead_opted_out_or_blocked"):
        _check_recipient(
            db, {"lead_id": str(lead_id),
                 "consent_reviewed": True, "consent_basis": "test"},
            "6436", NOW - timedelta(days=5),
        )
