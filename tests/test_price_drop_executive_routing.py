"""Guard rails for per-executive automated price-drop routing (no real sends)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from bson import ObjectId
import mongomock

from chatbot import price_drop_executive_routing as routing
from chatbot import price_drop_leads_v2 as v2

NOW = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
USER = ObjectId("6989c6309dd2ba54e478196c")
LEAD = ObjectId("690000000000000000009901")
PHONE = "+56912345678"
SID = "133526"
CAMPAIGN = "rebaja_6436_exec_test"


def _context():
    db = mongomock.MongoClient()["URLS"]
    db["usuarios"].insert_one({
        "_id": USER, "nombre": "Mariela Arriagada",
        "rol": "agente", "is_active": True,
        "wasender": {"session_id": int(SID)},
    })
    lead = {
        "_id": LEAD, "phone": PHONE, "created_at": NOW - timedelta(days=20),
        "lifecycle": {"assigned_to_user_id": str(USER),
                      "current_assignment_cycle_id": "cycle-1"},
        "messages": [],
        "price_drop_v2": {"campaigns": [{
            "campaign_id": CAMPAIGN, "event_id": CAMPAIGN,
            "status": "APPROVED", "test_mode": False,
            "phone_normalized": PHONE,
            "outbound_message": "Hola, hay una rebaja en la propiedad 6436.",
            "property_code": "6436", "operation": "venta", "currency": "UF",
            "old_price_uf": 2790, "new_price_uf": 2690,
        }]},
    }
    db["leads"].insert_one(lead)
    return db, lead


def _provider_response(sid=SID, status="connected", events=None,
                       webhook=True, url=None, key="session-test-key-not-for-production"):
    data = {
        "id": int(sid), "status": status,
        "webhook_enabled": webhook,
        "webhook_url": (url or
            "https://procasa-chatbot-yr8d.onrender.com/webhook/ejecutivos/" + sid),
        "webhook_events": events if events is not None else
            ["messages.received", "messages.upsert"],
        "api_key": key,
    }
    return SimpleNamespace(status_code=200, json=lambda: {"success": True, "data": data})


def _setup_route(monkeypatch, db, lead):
    monkeypatch.setenv("PRICE_DROP_EXECUTIVE_SEND_ENABLED", "true")
    monkeypatch.delenv("PRICE_DROP_EXEC_AUDIT_BASE_URL", raising=False)
    monkeypatch.setattr(routing, "_current_assignment", lambda db, lead:
                        ({"assignment_cycle_id": "cycle-1"}, str(USER)))
    monkeypatch.setattr(routing.Config, "WASENDER_PERSONAL_ACCESS_TOKEN",
                        "pat-unit-test-not-real")
    monkeypatch.setattr(routing.Config, "WASENDER_BASE_URL",
                        "https://www.wasenderapi.com/api")
    db["leads"].insert_one({
        "_id": ObjectId("690000000000000000009902"), "phone": "+56988888888",
        "messages": [{
            "channel": "executive_whatsapp", "source": "executive_whatsapp",
            "wasender_session_id": SID,
            "timestamp": (NOW - timedelta(days=1)).isoformat(),
        }],
    })


def test_route_only_when_session_connected_and_audit_captured(monkeypatch):
    db, lead = _context()
    _setup_route(monkeypatch, db, lead)
    called = []
    def fake_get(url, headers, timeout):
        called.append(url)
        assert headers["Authorization"] == "Bearer pat-unit-test-not-real"
        return _provider_response()
    route = routing.resolve_route(db, lead, now=NOW, get=fake_get)
    assert route["channel"] == "executive_whatsapp"
    assert route["session_id"] == SID
    assert route["executive_id"] == str(USER)
    assert route["assignment_cycle_id"] == "cycle-1"
    assert route["api_token"] == "session-test-key-not-for-production"
    assert called == ["https://www.wasenderapi.com/api/whatsapp-sessions/" + SID]
    assert "session-test-key-not-for-production" not in str(
        db["leads"].find_one({"_id": LEAD}))


def test_unhealthy_session_or_missing_webhook_evidence_falls_back_to_office(monkeypatch):
    db, lead = _context()
    _setup_route(monkeypatch, db, lead)
    for response in (
        _provider_response(status="disconnected"),
        _provider_response(webhook=False),
        _provider_response(events=["messages.received"]),
        _provider_response(sid="133527"),
        _provider_response(url="https://evil.example/webhook/ejecutivos/" + SID),
    ):
        assert routing.resolve_route(db, lead, now=NOW, get=lambda *a, **k: response) is None
    db["leads"].delete_one({"phone": "+56988888888"})
    assert routing.resolve_route(
        db, lead, now=NOW,
        get=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("no token lookup without audit evidence"))
    ) is None
    monkeypatch.setenv("PRICE_DROP_EXECUTIVE_SEND_ENABLED", "false")
    assert routing.resolve_route(db, lead, now=NOW) is None


def test_executive_send_uses_only_verified_session_token_and_reports_receipt(monkeypatch):
    from chatbot.whatsapp_client import _provider_message_id
    monkeypatch.setattr(routing, "throttle_outgoing_message_sync", lambda: None)
    route = {"channel": "executive_whatsapp", "api_token": "only-exec-session-token"}
    requests_seen = []
    def fake_post(url, headers, json, timeout):
        requests_seen.append((url, headers, json, timeout))
        return SimpleNamespace(status_code=200,
                               json=lambda: {"success": True, "data": {"msgId": "api-exec-1"}})
    receipt = routing.send_with_executive_route(
        route, PHONE, "Texto de rebaja", post=fake_post,
    )
    assert receipt["success"] is True
    assert receipt["provider_message_id"] == "api-exec-1"
    assert requests_seen[0][1]["Authorization"] == "Bearer only-exec-session-token"
    assert requests_seen[0][2]["text"] == "Texto de rebaja"


def test_real_campaign_records_executive_channel_and_does_not_send_twice(monkeypatch):
    db, lead = _context()
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    monkeypatch.setattr(v2, "_real_campaign_send_blocker", lambda *a, **k: None)
    monkeypatch.setattr(v2, "_text_sender_window_blocker", lambda *a, **k: None)
    monkeypatch.setattr(v2, "_verified_campaign_price", lambda *a, **k: None)
    from chatbot import price_drop_executive_routing as routing
    route = {
        "channel": "executive_whatsapp", "session_id": SID,
        "executive_id": str(USER), "assignment_cycle_id": "cycle-1",
        "user_oid": USER, "api_token": "not-real-key",
    }
    monkeypatch.setattr(routing, "resolve_route", lambda *a, **k: route)
    monkeypatch.setattr(routing, "owner_still_current", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(routing, "send_with_executive_route",
                        lambda route, phone, text: (
                            calls.append((route["session_id"], phone))
                            or {"success": True, "provider_message_id": "api-1",
                                "delivery_status": "accepted", "provider_called": True}
                        ))
    sent = v2.send_one(db, lead_id=str(LEAD), campaign_id=CAMPAIGN,
                       now=NOW, executive_transport_enabled=True)
    assert sent["status"] == "SENT"
    assert calls == [(SID, PHONE)]
    saved = db["leads"].find_one({"_id": LEAD})
    campaign = saved["price_drop_v2"]["campaigns"][0]
    assert campaign["status"] == "SENT"
    assert campaign["outbound_transport"] == "executive_whatsapp"
    assert campaign["wasender_session_id"] == SID
    assert campaign["assignment_cycle_id"] == "cycle-1"
    assert len(saved["messages"]) == 1
    message = saved["messages"][0]
    assert message["role"] == "assistant"
    assert message["channel"] == "executive_whatsapp"
    assert message["source"] == "price_drop_campaign_api"
    assert message["provider_message_id"] == "api-1"
    assert message["metadata"]["campaign_id"] == CAMPAIGN
    assert v2.send_one(db, lead_id=str(LEAD), campaign_id=CAMPAIGN,
                       now=NOW, executive_transport_enabled=True)["status"] == "NOT_APPROVED"
    assert calls == [(SID, PHONE)]


def test_executive_reply_attributed_only_to_original_session_and_cycle(monkeypatch):
    db, lead = _context()
    record = db["leads"].find_one({"_id": LEAD})
    record["price_drop_v2"]["campaigns"][0].update({
        "status": "SENT", "sent_at": NOW, "expires_at": NOW + timedelta(days=7),
        "provider_message_id": "api-1",
        "outbound_transport": "executive_whatsapp",
        "wasender_session_id": SID,
        "assignment_cycle_id": "cycle-1",
    })
    db["leads"].replace_one({"_id": LEAD}, record)
    wrong = v2.record_inbound(
        db, phone=PHONE, provider_message_id="reply-1",
        text="Sí me interesa", now=NOW + timedelta(minutes=1),
        source_session_id="133523", assignment_cycle_id="cycle-1",
    )
    assert wrong["status"] == "NOT_CAMPAIGN_REPLY"
    assert v2.record_inbound(
        db, phone=PHONE, provider_message_id="reply-1",
        text="Sí me interesa", now=NOW + timedelta(minutes=1),
    )["status"] == "NOT_CAMPAIGN_REPLY"
    assert v2.record_inbound(
        db, phone=PHONE, provider_message_id="reply-1",
        text="Sí me interesa", now=NOW + timedelta(minutes=1),
        source_session_id=SID, assignment_cycle_id="another-cycle",
    )["status"] == "NOT_CAMPAIGN_REPLY"
    confirmed = v2.record_inbound(
        db, phone=PHONE, provider_message_id="reply-1",
        text="Sí me interesa", now=NOW + timedelta(minutes=1),
        source_session_id=SID, assignment_cycle_id="cycle-1",
    )
    assert confirmed["status"] == "ATTRIBUTED"
    final = db["leads"].find_one({"_id": LEAD})
    assert final["price_drop_v2"]["campaigns"][0]["status"] == "RESPONDED"
    assert final["price_drop_v2"]["campaigns"][0]["reply_count"] == 1
    assert v2.record_inbound(
        db, phone=PHONE, provider_message_id="reply-1",
        text="Sí me interesa", now=NOW + timedelta(minutes=2),
        source_session_id=SID, assignment_cycle_id="cycle-1",
    )["status"] == "DUPLICATE_IGNORED"


def test_stop_on_executive_channel_is_still_attributed_after_global_suppression():
    db, lead = _context()
    record = db["leads"].find_one({"_id": LEAD})
    record["price_drop_v2"]["campaigns"][0].update({
        "status": "SENT", "sent_at": NOW, "expires_at": NOW + timedelta(days=7),
        "provider_message_id": "api-1",
        "outbound_transport": "executive_whatsapp",
        "wasender_session_id": SID, "assignment_cycle_id": "cycle-1",
    })
    record["price_drop_v2"]["opted_out"] = True  # webhook persisted STOP first
    db["leads"].replace_one({"_id": LEAD}, record)
    result = v2.record_inbound(
        db, phone=PHONE, provider_message_id="stop-1",
        text="No quiero recibir más mensajes", now=NOW + timedelta(minutes=1),
        source_session_id=SID, assignment_cycle_id="cycle-1",
    )
    assert result["status"] == "ATTRIBUTED"
    final = db["leads"].find_one({"_id": LEAD})
    assert final["price_drop_v2"]["campaigns"][0]["status"] == "OPTED_OUT"
    assert final["conversation_status"] == "STOPPED_BY_CLIENT"
