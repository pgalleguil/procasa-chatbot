"""Additional security regression gates: no campaign executive notice by default."""
from datetime import datetime, timedelta, timezone

import mongomock
from bson import ObjectId

from chatbot import price_drop_leads_v2 as v2
from chatbot import price_drop_campaign_exec_alert as legacy

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
PHONE = "+56983219804"


def test_legacy_exec_notices_frozen_even_if_old_flags_enabled(monkeypatch):
    monkeypatch.setenv(legacy.GATE, "true")
    monkeypatch.setenv("PRICE_DROP_REAL_PILOT_CONTEXT_ENABLED", "true")
    monkeypatch.delenv("PRICE_DROP_CAMPAIGN_EXEC_FROZEN", raising=False)
    assert legacy._active() is False
    # Implementation remains available for a future separately approved change.
    monkeypatch.setenv("PRICE_DROP_CAMPAIGN_EXEC_FROZEN", "false")
    assert legacy._active() is True


def _pending_test_db(monkeypatch):
    db = mongomock.MongoClient()["URLS"]
    db["leads"].insert_one({
        "_id": ObjectId("690000000000000000000555"),
        "phone": PHONE,
        "price_drop_v2": {"campaigns": [{
            "campaign_id": "test_leads_6436_admin_only",
            "property_code": "6436",
            "old_price_uf": 2790,
            "new_price_uf": 2690,
            "test_mode": True,
            "phone_normalized": PHONE,
            "status": "RESPONDED",
            "has_positive_response": True,
            "provider_message_id": "prior-outbound",
            "last_customer_text": "sí, me interesa",
            "last_customer_reply_at": NOW - timedelta(minutes=5),
            # Simulates an older deployment's pending executive slot.
            "notifications": {
                "administrator": {"status": "PENDING_SIMULATION"},
                "property_executive": {"status": "PENDING_SIMULATION"},
            },
        }]},
    })
    monkeypatch.setenv(v2.TEST_NOTICE_FLAG, "true")
    monkeypatch.setenv(v2.TEST_PHONE_FLAG, PHONE)
    monkeypatch.delenv(v2.EXEC_NOTICE_ENABLED_FLAG, raising=False)
    return db


def test_old_pending_executive_notices_cannot_send(monkeypatch):
    db = _pending_test_db(monkeypatch)
    calls = []
    result = v2.send_test_notice_once(db, role="property_executive",
                                      sender=lambda *x: calls.append(x), now=NOW)
    assert result["status"] == "EXECUTIVE_NOTICE_FROZEN"
    assert calls == []
    slot = db["leads"].find_one()["price_drop_v2"]["campaigns"][0]["notifications"]
    assert slot["property_executive"]["status"] == "PENDING_SIMULATION"


def test_admin_delivery_requires_explicit_exact_admin_phone(monkeypatch):
    db = _pending_test_db(monkeypatch)
    calls = []
    sender = lambda *args: calls.append(args)
    monkeypatch.delenv(v2.ADMIN_PHONE_FLAG, raising=False)
    assert v2.send_test_notice_once(db, role="administrator",
                                    sender=sender, now=NOW)["status"] == "ADMIN_PHONE_NOT_ALLOWLISTED"
    monkeypatch.setenv(v2.ADMIN_PHONE_FLAG, "+56911112222")
    assert v2.send_test_notice_once(db, role="administrator",
                                    sender=sender, now=NOW)["status"] == "ADMIN_PHONE_NOT_ALLOWLISTED"
    assert calls == []
    monkeypatch.setenv(v2.ADMIN_PHONE_FLAG, PHONE)
    result = v2.send_test_notice_once(db, role="administrator",
                                      sender=lambda p, m: {"success": True, "provider_message_id": "admin-1"},
                                      now=NOW)
    assert result["status"] == "SENT"
    assert v2.send_test_notice_once(db, role="administrator",
                                    sender=sender, now=NOW)["status"] == "IDLE"
    assert calls == []
