"""Regression tests for one-lead historical opt-out reconciliation."""
from __future__ import annotations

from datetime import datetime, timezone

import mongomock
import pytest
from bson import ObjectId

from chatbot.price_drop_campaign_classifier import lead_must_not_be_contacted
from scripts.reconcile_price_drop_opt_out import ReconciliationBlocked, reconcile_one


PHONE = "+56971234567"
CAMPAIGN_ID = "rebaja_example_arriendo"
PROVIDER_ID = "INBOUND_FAKE_RECONCILE_123"
RECEIVED = datetime(2026, 11, 16, 14, 45, tzinfo=timezone.utc)


def _db(text="No quiero más avisos, gracias", *, second_phone_record=False):
    db = mongomock.MongoClient()["URLS"]
    db["leads"].insert_one({
        "_id": ObjectId(),
        "phone": PHONE, "contact_phone_normalized": PHONE,
        "messages": [{"role": "user", "content": text}],
        "price_drop_v2": {
            "processed_inbound_provider_ids": [PROVIDER_ID],
            "campaigns": [{
                "campaign_id": CAMPAIGN_ID,
                "property_code": "17252", "status": "RESPONDED",
                "last_customer_text": text,
                "last_customer_reply_at": RECEIVED,
                "last_inbound_provider_id": PROVIDER_ID,
                "reply_count": 1,
                "last_classification": {
                    "label": "SIN_SENAL", "classifier_version": "price_drop_response_rules_v4",
                },
                "notifications": {"administrator": {"status": "NOT_TRIGGERED"}},
            }],
            "reply_events": [{
                "campaign_id": CAMPAIGN_ID, "provider_message_id": PROVIDER_ID,
                "received_at": RECEIVED, "text": text,
                "classification": {"label": "SIN_SENAL"},
            }],
        },
    })
    if second_phone_record:
        db["leads"].insert_one({
            "_id": ObjectId(), "phone": PHONE, "contact_phone_normalized": PHONE,
        })
    return db


def test_repair_single_confirmed_opt_out_idempotently():
    db = _db()
    before = db["leads"].find_one({"phone": PHONE})
    preview = reconcile_one(db, phone=PHONE)
    assert preview["status"] == "READY_TO_CORRECT"
    assert db["leads"].find_one({"phone": PHONE}) == before
    result = reconcile_one(db, phone=PHONE, execute=True)
    assert result["status"] == "OPTED_OUT_CONFIRMED"
    lead = db["leads"].find_one({"phone": PHONE})
    assert lead["conversation_status"] == "STOPPED_BY_CLIENT"
    assert lead["price_drop_v2"]["opted_out"] is True
    assert lead["price_drop_campaign_v1"]["opted_out"] is True
    campaign = lead["price_drop_v2"]["campaigns"][0]
    assert campaign["status"] == "OPTED_OUT"
    assert campaign["reply_count"] == 1
    assert campaign["last_classification"]["label"] == "BAJA"
    assert len(lead["price_drop_v2"]["reply_events"]) == 1
    event = lead["price_drop_v2"]["reply_events"][0]
    assert event["classification_original"]["label"] == "SIN_SENAL"
    assert event["classification"]["label"] == "BAJA"
    assert len(lead["price_drop_v2"]["classification_corrections"]) == 1
    assert lead_must_not_be_contacted(lead)
    assert reconcile_one(db, phone=PHONE, execute=True)["status"] == "ALREADY_OPTED_OUT"
    assert len(db["leads"].find_one({"phone": PHONE})["price_drop_v2"]["classification_corrections"]) == 1


@pytest.mark.parametrize("text", [
    "Gracias, necesito información",
    "Sí, me interesa agendar una visita",
    "¿Cuándo baja el precio?",
])
def test_never_repair_regular_customer_responses(text):
    db = _db(text)
    before = db["leads"].find_one({"phone": PHONE})
    with pytest.raises(ReconciliationBlocked, match="expected_exactly_one"):
        reconcile_one(db, phone=PHONE, execute=True)
    assert db["leads"].find_one({"phone": PHONE}) == before


def test_never_update_ambiguous_customer_phone():
    db = _db(second_phone_record=True)
    with pytest.raises(ReconciliationBlocked, match="lead_not_unique"):
        reconcile_one(db, phone=PHONE, execute=True)


def test_never_update_missing_provider_provenance():
    db = _db()
    db["leads"].update_one({"phone": PHONE}, {
        "$set": {"price_drop_v2.campaigns.0.last_inbound_provider_id": "other"},
    })
    with pytest.raises(ReconciliationBlocked, match="campaign_reply_event"):
        reconcile_one(db, phone=PHONE, execute=True)


def test_never_update_other_database():
    with pytest.raises(ReconciliationBlocked, match="unexpected_database"):
        reconcile_one(mongomock.MongoClient()["wrong"], phone=PHONE, execute=True)
