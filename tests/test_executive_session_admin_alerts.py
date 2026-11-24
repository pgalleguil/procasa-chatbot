"""Synthetic unit tests; no real WhatsApp requests or customer data."""
import asyncio
from datetime import datetime, timezone

import mongomock
import pytest
from bson import ObjectId

from chatbot import executive_session_admin_alerts as alerts


@pytest.fixture
def db():
    database = mongomock.MongoClient().test
    database["usuarios"].insert_one({
        "_id": alerts.PABLO_ID, "nombre": "Pablo Galleguillos",
        "rol": "agente", "is_active": True, "telefono": "+56983219804",
    })
    return database


def test_recipient_is_only_pablo_verified_phone_and_role(db):
    assert alerts._pablo_recipient(db) == "+56983219804"
    db["usuarios"].update_one({"_id": alerts.PABLO_ID},
                              {"$set": {"telefono": "+56981111111"}})
    assert alerts._pablo_recipient(db) is None
    db["usuarios"].update_one({"_id": alerts.PABLO_ID},
                              {"$set": {"telefono": "+56983219804",
                                        "is_active": False}})
    assert alerts._pablo_recipient(db) is None
    db["usuarios"].update_one({"_id": alerts.PABLO_ID},
                              {"$set": {"is_active": True, "rol": "admin"}})
    assert alerts._pablo_recipient(db) is None
    db["usuarios"].update_one({"_id": alerts.PABLO_ID},
                              {"$set": {"rol": "agente"}})
    assert alerts._pablo_recipient(db) == "+56983219804"


def test_two_checks_required_and_recovered_new_outage_is_distinct(db):
    sid = "133517"
    assert alerts._record_health(db, session_id=sid, reason="disconnected") is False
    assert alerts._record_health(db, session_id=sid, reason="disconnected") is True
    assert alerts._record_health(db, session_id=sid, reason="disconnected") is False
    state = db[alerts.STATE_COLLECTION].find_one({"_id": sid})
    assert state["alert_claimed"] is True
    assert state["unhealthy_checks"] == 3
    assert alerts._record_health(db, session_id=sid, reason="connected") is False
    assert alerts._record_health(db, session_id=sid, reason="disconnected") is False
    assert alerts._record_health(db, session_id=sid, reason="disconnected") is True


def test_ambiguous_provider_requires_three_checks(db):
    sid = "133523"
    assert not alerts._record_health(db, session_id=sid, reason="provider_unavailable")
    assert not alerts._record_health(db, session_id=sid, reason="provider_unavailable")
    assert alerts._record_health(db, session_id=sid, reason="provider_unavailable")
    assert not alerts._record_health(db, session_id=sid, reason="provider_unavailable")
    with pytest.raises(ValueError):
        alerts._record_health(db, session_id="888888", reason="disconnected")


def test_no_alert_when_all_sessions_connected(db, monkeypatch):
    monkeypatch.setattr(alerts, "_health", lambda *_args: "connected")
    notifications = []
    async def send(number, message):
        notifications.append((number, message))
        return {"success": True, "provider_message_id": "office-alert-id"}
    results = asyncio.run(alerts.check_sessions_once(db=db, send=send))
    assert len(results) == 5
    assert all(item["state"] == "connected" for item in results)
    assert not notifications
    assert db[alerts.STATE_COLLECTION].count_documents({}) == 5


def test_only_pablo_receives_one_alert_for_one_outage(db, monkeypatch):
    monkeypatch.setattr(alerts, "_health", lambda _db, sid, _name:
                        "disconnected" if sid == "133517" else "connected")
    notifications = []
    async def send(number, message):
        notifications.append((number, message))
        return {"success": True, "provider_message_id": "office-alert-id"}
    first = asyncio.run(alerts.check_sessions_once(db=db, send=send))
    assert not notifications
    second = asyncio.run(alerts.check_sessions_once(db=db, send=send))
    assert len(notifications) == 1
    assert notifications[0][0] == alerts.PABLO_PHONE
    assert "Hernán Castro" in notifications[0][1]
    assert "133517" in notifications[0][1]
    assert "disconnected" in notifications[0][1]
    assert "wa.me" not in notifications[0][1]
    assert [x for x in second if x["alert"] == "accepted"] == [
        {"session": "133517", "state": "disconnected", "alert": "accepted"}
    ]
    asyncio.run(alerts.check_sessions_once(db=db, send=send))
    assert len(notifications) == 1
    assert db[alerts.STATE_COLLECTION].find_one(
        {"_id": "133517"})["alert_delivery"] == "accepted"


def test_each_executive_sends_to_one_fixed_recipient(db, monkeypatch):
    monkeypatch.setattr(alerts, "_health",
                        lambda *_args: "sender_identity_mismatch")
    numbers = []
    async def send(number, message):
        numbers.append(number)
        return {"success": True, "provider_message_id": "office-id"}
    asyncio.run(alerts.check_sessions_once(db=db, send=send))
    asyncio.run(alerts.check_sessions_once(db=db, send=send))
    assert numbers == [alerts.PABLO_PHONE] * 5


def test_unverified_recipient_sends_nothing_and_never_redirects(db, monkeypatch):
    monkeypatch.setattr(alerts, "_health", lambda *_args: "disconnected")
    db["usuarios"].update_one({"_id": alerts.PABLO_ID},
                              {"$set": {"telefono": "+56911110000"}})
    calls = []
    async def send(number, message):
        calls.append(number)
        return {"success": True, "provider_message_id": "abc"}
    asyncio.run(alerts.check_sessions_once(db=db, send=send))
    result = asyncio.run(alerts.check_sessions_once(db=db, send=send))
    assert calls == []
    assert all(x["alert"] == "recipient_unverified" for x in result)
    assert all(x["alert_claimed"] for x in db[alerts.STATE_COLLECTION].find({}))


def test_unknown_delivery_does_not_resend_same_incident(db, monkeypatch):
    monkeypatch.setattr(alerts, "_health", lambda *_args: "disconnected")
    calls = []
    async def send(number, message):
        calls.append(number)
        return {"success": False, "provider_call_uncertain": True}
    asyncio.run(alerts.check_sessions_once(db=db, send=send))
    result = asyncio.run(alerts.check_sessions_once(db=db, send=send))
    assert len(calls) == 5
    assert all(x["alert"] == "delivery_unknown" for x in result)
    asyncio.run(alerts.check_sessions_once(db=db, send=send))
    assert len(calls) == 5


def test_monitor_disabled_by_default_has_no_provider_requests(monkeypatch):
    monkeypatch.delenv(alerts.FLAG, raising=False)
    def unexpected():
        pytest.fail("No connections must be queried when alerts are disabled")
    monkeypatch.setattr(alerts, "check_sessions_once", unexpected)
    asyncio.run(alerts.session_monitor_loop())


def test_health_probe_checks_own_session_and_phone_only(db, monkeypatch):
    from chatbot import crm_executive_outbound
    session = {"session_id": "133517", "user_oid": ObjectId(),
               "token": "fake-token", "capture_enabled": True}
    db["usuarios"].insert_one({
        "_id": session["user_oid"], "nombre": "Hernán Castro",
        "rol": "agente", "is_active": True,
        "telefono": "+56984037477", "wasender": {"session_id": 133517},
    })
    monkeypatch.setattr(alerts, "_session", lambda _db, sid: session)
    probes = []
    def verify(sess, *, expected_phone, force):
        probes.append((sess["session_id"], expected_phone, force))
    monkeypatch.setattr(alerts, "verify_provider_sender_identity", verify)
    monkeypatch.setattr(alerts, "_session_status", lambda sess: "connected")
    assert alerts._health(db, "133517", "Hernán Castro") == "connected"
    assert probes == [("133517", "+56984037477", True)]
    monkeypatch.setattr(alerts, "_session_status", lambda sess: "logged_out")
    assert alerts._health(db, "133517", "Hernán Castro") == "logged_out"
    def mismatch(*_args, **_kwargs):
        raise crm_executive_outbound.OutboundBlocked("sender_identity_mismatch")
    monkeypatch.setattr(alerts, "verify_provider_sender_identity", mismatch)
    assert alerts._health(db, "133517", "Hernán Castro") == "sender_identity_mismatch"
    assert alerts._health(db, "133517", "Susana Ensignia") == "owner_phone_unavailable"


def test_no_scheduled_monitor_or_background_session_polling_in_lifespan():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "webhook.py").read_text(
        encoding="utf-8")
    assert "exec_session_alert_task" not in source
    assert "session_monitor_loop()" not in source
    # The regular WhatsApp process is unaffected.
    assert "non_hot_digest_task = asyncio.create_task(non_hot_digest_worker_loop())" in source


def test_event_alert_never_queries_other_sessions_and_deduplicates(db, monkeypatch):
    monkeypatch.setenv(alerts.FLAG, "1")
    calls = []
    def never_probe(*_args, **_kwargs):
        pytest.fail("On-demand notification must not probe WaSender")
    monkeypatch.setattr(alerts, "_health", never_probe)
    async def send(number, message):
        calls.append((number, message))
        return {"success": True, "provider_message_id": "office-message-1"}
    first = asyncio.run(alerts.notify_pablo_on_crm_failure(
        session_id="133517", executive_name="Hernán Castro",
        reason="session_unavailable", db=db, send=send,
    ))
    second = asyncio.run(alerts.notify_pablo_on_crm_failure(
        session_id="133517", executive_name="Hernán Castro",
        reason="session_unavailable", db=db, send=send,
    ))
    assert first == "accepted" and second == "already_notified"
    assert len(calls) == 1
    assert calls[0][0] == alerts.PABLO_PHONE
    assert "Hernán Castro" in calls[0][1]
    assert "133517" in calls[0][1]
    assert db[alerts.STATE_COLLECTION].find_one(
        {"_id": "133517"})["alert_delivery"] == "accepted"
    # Recovery occurs after an already-verified connected CRM preflight.
    alerts.mark_event_recovered(db, session_id="133517")
    assert db[alerts.STATE_COLLECTION].find_one(
        {"_id": "133517"})["alert_claimed"] is False
    third = asyncio.run(alerts.notify_pablo_on_crm_failure(
        session_id="133517", executive_name="Hernán Castro",
        reason="session_disconnected", db=db, send=send,
    ))
    assert third == "accepted" and len(calls) == 2


def test_event_alert_unknown_delivery_must_not_retry(db, monkeypatch):
    monkeypatch.setenv(alerts.FLAG, "1")
    attempts = []
    async def send(number, message):
        attempts.append(number)
        return {"success": False, "provider_call_uncertain": True}
    first = asyncio.run(alerts.notify_pablo_on_crm_failure(
        session_id="133523", executive_name="Susana Ensignia",
        reason="session_disconnected", db=db, send=send,
    ))
    again = asyncio.run(alerts.notify_pablo_on_crm_failure(
        session_id="133523", executive_name="Susana Ensignia",
        reason="session_disconnected", db=db, send=send,
    ))
    assert first == "delivery_unknown"
    assert again == "already_notified"
    assert attempts == [alerts.PABLO_PHONE]


def test_event_alert_refuses_other_recipient_unknown_session_and_non_disconnect(
        db, monkeypatch):
    monkeypatch.setenv(alerts.FLAG, "1")
    calls = []
    async def send(number, message):
        calls.append(number)
        return {"success": True, "provider_message_id": "1"}
    async def run(**kwargs):
        return await alerts.notify_pablo_on_crm_failure(db=db, send=send, **kwargs)
    assert asyncio.run(run(
        session_id="133517", executive_name="Pablo Galleguillos",
        reason="session_unavailable",
    )) == "executive_not_authorized"
    assert asyncio.run(run(
        session_id="999999", executive_name="Hernán Castro",
        reason="session_unavailable",
    )) == "executive_not_authorized"
    assert asyncio.run(run(
        session_id="133517", executive_name="Hernán Castro",
        reason="delivery_unconfirmed",
    )) == "reason_not_alertable"
    assert calls == []
    db["usuarios"].update_one(
        {"_id": alerts.PABLO_ID}, {"$set": {"telefono": "+56915555555"}})
    result = asyncio.run(run(
        session_id="133517", executive_name="Hernán Castro",
        reason="session_unavailable",
    ))
    assert result == "recipient_unverified"
    assert calls == []


def test_event_alert_disabled_never_does_work(db, monkeypatch):
    monkeypatch.setenv(alerts.FLAG, "0")
    calls = []
    async def send(number, message):
        calls.append(number)
        return {"success": True, "provider_message_id": "fake"}
    assert asyncio.run(alerts.notify_pablo_on_crm_failure(
        session_id="133517", executive_name="Hernán Castro",
        reason="session_unavailable", db=db, send=send,
    )) == "disabled"
    assert calls == []
    assert db[alerts.STATE_COLLECTION].count_documents({}) == 0


def test_crm_get_preflight_event_is_the_trigger_not_a_timer():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent /
            "chatbot/crm_executive_outbound_api.py").read_text(encoding="utf-8")
    assert "asyncio.create_task(notify_pablo_on_crm_failure(" in text
    assert "_queue_operator_alert(user=user, reason=\"session_unavailable\")" in text
    assert "mark_event_recovered(db, session_id=profile[\"session\"])" in text


@pytest.mark.parametrize("reason", ["invalid_request", "invalid_recipient", "provider_rejected",
                                    "subscription", "rate_limit", "delivery_unconfirmed"])
def test_post_send_alert_is_per_lead_not_session_health(db, monkeypatch, reason):
    monkeypatch.setenv(alerts.FLAG, "1")
    calls = []
    async def send(number, message):
        calls.append((number, message))
        return {"success": True, "provider_message_id": "office-receipt"}
    lead_id = str(ObjectId())
    async def notify(lead):
        return await alerts.notify_pablo_on_crm_failure(
            session_id="133527", executive_name="Paula Morales",
            reason=reason, lead_id=lead, db=db, send=send)
    assert asyncio.run(notify(lead_id)) == "accepted"
    alerts.mark_event_recovered(db, session_id="133527")
    assert asyncio.run(notify(lead_id)) == "already_notified"
    assert asyncio.run(notify(str(ObjectId()))) == "accepted"
    assert len(calls) == 2
    assert all(number == alerts.PABLO_PHONE for number, _ in calls)
    assert reason in calls[0][1]
    assert lead_id in calls[0][1]
    assert db[alerts.STATE_COLLECTION].count_documents({
        "alert_provider_message_id": "office-receipt"}) == 2
