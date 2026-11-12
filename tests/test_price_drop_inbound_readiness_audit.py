"""Read-only inbound pilot diagnostics; no sends, no CRM writes."""
from datetime import datetime, timedelta, timezone
import json

from bson import ObjectId
import mongomock
import pytest

from config import Config
from scripts import auditar_recepcion_piloto as audit


NOW = datetime(2026, 11, 12, 13, 32, tzinfo=timezone.utc)


def _data():
    db = mongomock.MongoClient()["URLS"]
    for index, code in enumerate(("16479", "6436", "6377"), 1):
        db["leads"].insert_one({
            "_id": ObjectId(), "phone": f"+5698000010{index}",
            "messages": [
                {"role": "user", "timestamp": NOW - timedelta(days=90),
                 "content": "Consulta inicial"},
                {"role": "assistant", "timestamp": NOW - timedelta(minutes=10),
                 "content": "Aviso de rebaja"},
            ],
            "is_paused": False,
            "price_drop_v2": {"campaigns": [{
                "campaign_id": audit.PILOT, "property_code": code,
                "status": "SENT", "sent_at": NOW - timedelta(minutes=10),
                "provider_status": "pending", "reply_count": 0,
            }]},
        })
    return db


class Response:
    status_code = 200
    def __init__(self, data):
        self.data = data
    def json(self):
        return {"success": True, "data": self.data}


def test_pilot_diagnostic_distinguishes_missing_reply_and_provider_webhook(monkeypatch):
    db = _data()
    before = list(db["leads"].find())
    jobs_before = list(db["chatbot_inbound_jobs"].find())
    monkeypatch.setattr(Config, "WASENDER_PERSONAL_ACCESS_TOKEN",
                        "account-token-test", raising=False)
    monkeypatch.setattr(Config, "WASENDER_TOKEN", "office-key-test", raising=False)
    monkeypatch.setattr(Config, "WASENDER_BASE_URL",
                        "https://wasenderapi.com/api", raising=False)
    calls = []
    def get(url, **kw):
        calls.append(url)
        assert kw["headers"]["Authorization"] == "Bearer account-token-test"
        assert kw["timeout"] == (3, 12)
        if url.endswith("/whatsapp-sessions"):
            return Response([{"id": 99, "log_messages": True}])
        return Response({"id": 99, "api_key": "office-key-test",
                         "status": "connected", "log_messages": True,
                         "webhook_url": "https://procasa-chatbot-yr8d.onrender.com/webhook",
                         "webhook_enabled": True,
                         "webhook_events": ["messages.received", "message.sent"]})
    r = audit.pilot_ingress_audit(db, provider_get=get)
    assert r["status"] == "AUDITED_READ_ONLY"
    assert r["send_attempts"] == r["mongo_mutations"] == 0
    assert len(r["pilot_recipients"]) == 3
    assert all(x["message_inbound_after_send"] == 0 and
               x["durable_queue_inbound_after_send"] == 0 and
               x["is_paused"] is False for x in r["pilot_recipients"])
    s = r["provider_session_audit"]["sessions"][0]
    assert s["office_session_token_match"] is True
    assert s["webhook_url_matches_production"] is True
    assert s["subscribed_receive_events"] == ["messages.received"]
    assert "account-token-test" not in json.dumps(r)
    assert "+569800001" not in json.dumps(r)
    assert "office-key-test" not in json.dumps(r)
    assert list(db["leads"].find()) == before
    assert list(db["chatbot_inbound_jobs"].find()) == jobs_before
    assert len(calls) == 2


def test_audit_unknown_provider_webhook_is_unknown_not_false(monkeypatch):
    monkeypatch.setattr(Config, "WASENDER_PERSONAL_ACCESS_TOKEN", "", raising=False)
    db = _data()
    r = audit.pilot_ingress_audit(db, provider_get=lambda *_args, **_kw:
                                  pytest.fail("no network without PAT"))
    assert r["provider_session_audit"]["status"] == "UNAVAILABLE"
    assert len(r["pilot_recipients"]) == 3
    assert audit._session_webhook({"id": 1})["webhook_enabled"] is None
    assert audit._session_webhook({"id": 1})["subscribed_receive_events"] is None


def test_diagnostic_counts_only_inbounds_after_sent_at(monkeypatch):
    monkeypatch.setattr(Config, "WASENDER_PERSONAL_ACCESS_TOKEN", "", raising=False)
    db = _data()
    row = db["leads"].find_one({"price_drop_v2.campaigns.property_code": "6436"})
    db["leads"].update_one({"_id": row["_id"]}, {"$push": {"messages": {
        "role": "user", "timestamp": (NOW + timedelta(seconds=1)).isoformat(),
        "content": "Sí, me interesa", "provider_message_id": "incoming-1",
    }}})
    db["chatbot_inbound_jobs"].insert_one({
        "kind": "inbound_job", "phone": row["phone"],
        "received_at": NOW + timedelta(seconds=1), "state": "received",
    })
    r = audit.pilot_ingress_audit(db)
    by_code = {x["property_code"]: x for x in r["pilot_recipients"]}
    assert by_code["6436"]["message_inbound_after_send"] == 1
    assert by_code["6436"]["durable_queue_inbound_after_send"] == 1
    assert by_code["6377"]["durable_queue_inbound_after_send"] == 0


def test_pilot_mismatch_fails_closed():
    db = _data()
    db["leads"].delete_one({"price_drop_v2.campaigns.property_code": "6377"})
    assert audit.pilot_ingress_audit(db)["status"] == "BLOCKED_PILOT_SET_UNEXPECTED"
