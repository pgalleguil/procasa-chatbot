"""One-phone QA reset + replay without losing inquiry evidence or real-customer idempotency."""
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from bson import ObjectId

from chatbot import price_drop_leads_v2 as v2

PHONE = v2.APPROVED_ADMIN_PHONE
LID = ObjectId("690000000000000000000888")
OLD_ID = "test_leads_6436_20261106_9804"
NEW_ID = "test_leads_6436_20261109_9804"
NOW = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv(v2.TEST_PHONE_FLAG, PHONE)
    monkeypatch.setenv(v2.ADMIN_PHONE_FLAG, PHONE)
    monkeypatch.delenv(v2.SEND_FLAG, raising=False)
    monkeypatch.delenv(v2.CONTEXT_FLAG, raising=False)
    monkeypatch.delenv(v2.APPROVED_ID_FLAG, raising=False)
    d = mongomock.MongoClient()["URLS"]
    d["universo_cartera_prop360"].insert_one({
        "codigo": "6436", "disponible_prop360": True,
        "estado": {"oficina": "PROCASA SUCRE"},
        "tipo_operacion": {"venta": True, "precio_venta": {"precio_uf": 2690}},
    })
    d["universo_cartera_prop360_historial"].insert_one({
        "codigo": "6436", "campo": "precio_publicado",
        "fecha": "2026-10-14T11:52:43+00:00",
        "moneda_anterior": "UF", "moneda_nueva": "UF",
        "monto_anterior": 2790, "monto_nuevo": 2690,
    })
    d["leads"].insert_one({
        "_id": LID, "phone": PHONE,
        "conversation_id": "old-conversation",
        "prospecto": {"codigo": "6436",
                      "visit_preference": {"text": "mañana a las 10"}},
        "pending_response": {"type": "VISIT_CONFIRMATION", "status": "waiting"},
        "messages": [{
            "role": "user", "content": "consulta previa 6436",
            "timestamp": "2026-11-06T11:20:00+00:00",
        }],
        "price_drop_v2": {
            "processed_inbound_provider_ids": ["old-reply-id"],
            "campaigns": [{
                "campaign_id": OLD_ID, "property_code": "6436",
                "old_price_uf": 2790, "new_price_uf": 2690,
                "outbound_message": "Aviso anterior",
                "status": "RESPONDED", "test_mode": True,
                "phone_normalized": PHONE,
                "sent_at": NOW - timedelta(days=3),
                "expires_at": NOW + timedelta(days=4),
                "provider_message_id": "old-provider-id",
                "notifications": {
                    "administrator": {"status": "SENT"},
                    "property_executive": {"status": "FROZEN_ADMIN_ONLY"},
                },
            }],
        },
    })
    return d


def request(**kw):
    args = dict(lead_id=str(LID), phone=PHONE, campaign_id=NEW_ID,
                property_code="6436", old_price_uf=2790,
                new_price_uf=2690, test_mode=True)
    args.update(kw)
    return args


def test_one_phone_reset_preserves_evidence_and_replay_attributes_latest(db, monkeypatch):
    from chatbot import storage
    monkeypatch.setattr(storage, "get_db", lambda: db)
    # Pre-reset: old active campaign and history still drive context.
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    assert v2.active_context(db, phone=PHONE, message="me interesa", now=NOW)["campaign_id"] == OLD_ID
    assert len(storage.obtener_conversacion(PHONE)) == 1
    with pytest.raises(v2.PilotBlocked, match="active_campaign_for_property"):
        v2.preview_one(db, **request())

    reset = v2.reset_test_qa_session(
        db, campaign_id=NEW_ID, operator="QA", phone=PHONE, now=NOW)
    assert reset["status"] == "RESET"
    assert reset["historical_messages_preserved"] == 1
    assert v2.reset_test_qa_session(
        db, campaign_id=NEW_ID, operator="QA", phone=PHONE,
        now=NOW + timedelta(seconds=30))["status"] == "ALREADY_RESET"
    d = db["leads"].find_one({"_id": LID})
    assert d["messages"][0]["content"] == "consulta previa 6436"
    assert d["price_drop_v2"]["processed_inbound_provider_ids"] == ["old-reply-id"]
    assert d["price_drop_v2"]["qa_session"]["prior_prospecto"]["codigo"] == "6436"
    assert d["price_drop_v2"]["qa_session"]["prior_pending_response"]["status"] == "waiting"
    assert "pending_response" not in d and d["prospecto"] == {}
    assert d["conversation_id"] != "old-conversation"
    assert storage.obtener_conversacion(PHONE) == []
    assert v2.active_context(db, phone=PHONE, message="me interesa",
                             now=NOW + timedelta(minutes=1)) is None
    db["leads"].update_one({"_id": LID}, {"$push": {"messages": {
        "role": "user", "content": "Quiero consultar por propiedad 6436",
        "timestamp": (NOW + timedelta(minutes=2)).isoformat(),
    }}})
    messages = storage.obtener_conversacion(PHONE)
    assert len(messages) == 1 and "Quiero consultar" in messages[0]["content"]
    assert v2.prepare_one(db, now=NOW + timedelta(minutes=3), **request())["status"] == "PREPARED"
    assert v2.approve_one(db, lead_id=str(LID), campaign_id=NEW_ID,
                          operator="QA", now=NOW + timedelta(minutes=3))["status"] == "APPROVED"
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, NEW_ID)
    calls = []
    result = v2.send_one(db, lead_id=str(LID), campaign_id=NEW_ID,
                         now=NOW + timedelta(minutes=4),
                         sender=lambda p, text: (calls.append((p, text)) or
                               {"success": True, "provider_message_id": "new-provider-id"}))
    assert result["status"] == "SENT"
    assert len(calls) == 1 and calls[0][0] == PHONE
    assert v2.active_context(db, phone=PHONE, message="me interesa",
                             now=NOW + timedelta(minutes=5))["campaign_id"] == NEW_ID
    assert v2.record_inbound(db, phone=PHONE, provider_message_id="new-reply-id",
                             text="Me interesa", now=NOW + timedelta(minutes=5))["status"] == "ATTRIBUTED"
    d = db["leads"].find_one({"_id": LID})
    assert len(d["price_drop_v2"]["campaigns"]) == 2
    assert d["price_drop_v2"]["campaigns"][0]["status"] == "RESPONDED"
    assert d["price_drop_v2"]["campaigns"][1]["reply_count"] == 1
    assert d["price_drop_v2"]["campaigns"][1]["notifications"]["administrator"]["status"] == "PENDING_SIMULATION"
    assert d["price_drop_v2"]["campaigns"][1]["notifications"]["property_executive"]["status"] == "FROZEN_ADMIN_ONLY"
    assert v2.send_one(db, lead_id=str(LID), campaign_id=NEW_ID,
                       sender=lambda *a: calls.append(a))["status"] == "NOT_APPROVED"
    assert len(calls) == 1


def test_reset_requires_exact_tester_and_never_touches_other_phone(db, monkeypatch):
    other = ObjectId("690000000000000000000889")
    db["leads"].insert_one({"_id": other, "phone": "+56930002222",
                            "messages": [{"role": "user", "content": "No tocar"}]})
    blocked = v2.reset_test_qa_session(
        db, campaign_id=NEW_ID, operator="QA", phone="+56930002222", now=NOW)
    assert blocked["status"] == "BLOCKED"
    assert db["leads"].find_one({"_id": other})["messages"][0]["content"] == "No tocar"
    assert "qa_session" not in db["leads"].find_one({"_id": LID})["price_drop_v2"]


def test_reset_does_not_bypass_unfinished_campaign(db):
    db["leads"].update_one({"_id": LID}, {"$set": {
        "price_drop_v2.campaigns.0.status": "DELIVERY_UNKNOWN"}})
    assert v2.reset_test_qa_session(
        db, campaign_id=NEW_ID, operator="QA", phone=PHONE,
        now=NOW)["status"] == "BLOCKED"


def test_non_test_real_client_still_blocked_by_old_property_campaign(db):
    v2.reset_test_qa_session(db, campaign_id=NEW_ID, operator="QA", phone=PHONE, now=NOW)
    with pytest.raises(v2.PilotBlocked, match="consent_review_required"):
        v2.preview_one(db, **request(test_mode=False))
    # Even with consent, an old test campaign must not be overridden on a real send.
    d = db["leads"].find_one({"_id": LID})
    d["lead_temperature_effective"] = "HOT"
    db["leads"].replace_one({"_id": LID}, d)
    with pytest.raises(v2.PilotBlocked, match="active_campaign_for_property|pre_drop_inquiry_not_verified"):
        v2.preview_one(db, **request(test_mode=False,
                                    consent_reviewed=True, consent_basis="documented"))
