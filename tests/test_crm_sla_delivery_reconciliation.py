import asyncio
from datetime import datetime, timedelta, timezone

import mongomock

from chatbot import whatsapp_client
from chatbot.crm_notifications import individual_identity
from chatbot.crm_sla_reassignment_notifications import (
    COLLECTION,
    SLA_REASSIGNED_AWAY,
    SLA_REASSIGNED_TO,
    reconcile_one_sla_reassignment_delivery_sync,
    record_sla_reassignment_delivery_status,
    process_one_sla_reassignment_sync,
)


UTC = timezone.utc


def _fixture(*, notification_type=SLA_REASSIGNED_TO, state="sent", provider_id="provider-1"):
    db = mongomock.MongoClient()["test"]
    db["leads"].insert_one({
        "_id": "lead-1",
        "ejecutivo_asignado": "Ejecutivo Nuevo",
        "assignment_mirror_owner_user_id": "user-new",
        "lifecycle": {
            "current_assignment_cycle_id": "cycle-1",
            "assignment_cycle_id": "cycle-1",
            "assigned_to_user_id": "user-new",
            "assigned_to_display_name": "Ejecutivo Nuevo",
            "sla_started_at": None,
            "owner_notified_at": None,
        },
    })
    db["crm_assignment_cycles"].insert_one({
        "_id": "cycle-1",
        "assignment_cycle_id": "cycle-1",
        "lead_id": "lead-1",
        "assigned_to_user_id": "user-new",
        "cycle_status": "active",
        "reassignment_state": "AWAITING_OWNER_NOTIFICATION",
        "owner_notified_at": None,
        "sla_started_at": None,
    })
    db[COLLECTION].insert_one({
        "_id": "notification-1",
        "individual_identity": individual_identity(
            lead_id="lead-1",
            assignment_cycle_id="cycle-1",
            notification_type=notification_type,
            recipient_user_id="user-new",
        ),
        "notification_type": notification_type,
        "notification_role": "new_owner" if notification_type == SLA_REASSIGNED_TO else "previous_owner",
        "lead_id": "lead-1",
        "assignment_cycle_id": "cycle-1",
        "recipient_user_id": "user-new",
        "state": state,
        "provider_message_id": provider_id,
        "actually_delivered": False,
        "delivery_status": "sent",
        "created_at": datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
        "next_status_check_at": datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
        "payload": {"message": "test"},
        "message_domain": "commercial_notification",
    })
    return db


def _status(status, *, timestamp=None, **extra):
    return {
        "delivery_status": status,
        "provider_status": status,
        "provider_message_id": "provider-1",
        "provider_event_timestamp": timestamp,
        **extra,
    }


def test_sent_plus_provider_sent_keeps_sla_off_and_never_sends():
    db = _fixture()
    calls = []
    result = reconcile_one_sla_reassignment_delivery_sync(
        db,
        worker_id="reconciler-1",
        now=datetime(2026, 9, 20, 12, 1, tzinfo=UTC),
        status_getter=lambda provider_id: calls.append(provider_id) or _status("sent"),
    )
    stored = db[COLLECTION].find_one({"_id": "notification-1"})
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-1"})
    assert result["status"] == "waiting_delivery"
    assert calls == ["provider-1"]
    assert stored["state"] == "sent"
    assert stored["actually_delivered"] is False
    assert cycle["sla_started_at"] is None


def test_delivered_with_provider_timestamp_activates_destination_cycle():
    db = _fixture()
    delivered_at = datetime(2026, 9, 20, 12, 2, tzinfo=UTC)
    result = reconcile_one_sla_reassignment_delivery_sync(
        db,
        worker_id="reconciler-1",
        now=delivered_at + timedelta(seconds=1),
        status_getter=lambda _provider_id: _status(
            "delivered", timestamp=delivered_at, provider_event_timestamp_source="deliveredAt"
        ),
    )
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-1"})
    assert result["status"] == "delivered"
    assert cycle["reassignment_state"] == "active"
    assert cycle["sla_started_at"] == delivered_at.replace(tzinfo=None)
    assert cycle["owner_notified_at"] == delivered_at.replace(tzinfo=None)


def test_read_activates_once_and_duplicate_poll_is_idle():
    db = _fixture()
    delivered_at = datetime(2026, 9, 20, 12, 3, tzinfo=UTC)
    calls = []

    def getter(provider_id):
        calls.append(provider_id)
        return _status("read", timestamp=delivered_at)

    first = reconcile_one_sla_reassignment_delivery_sync(
        db, worker_id="reconciler-1", now=delivered_at, status_getter=getter
    )
    second = reconcile_one_sla_reassignment_delivery_sync(
        db, worker_id="reconciler-1", now=delivered_at + timedelta(seconds=1), status_getter=getter
    )
    assert first["status"] == "delivered"
    assert second["status"] == "idle"
    assert calls == ["provider-1"]


def test_webhook_then_poll_keeps_first_provider_clock():
    db = _fixture()
    webhook_at = datetime(2026, 9, 20, 12, 4, tzinfo=UTC)
    later_at = datetime(2026, 9, 20, 12, 9, tzinfo=UTC)
    first = record_sla_reassignment_delivery_status(
        db,
        provider_message_id="provider-1",
        delivery_status="delivered",
        delivered_at=webhook_at,
    )
    second = record_sla_reassignment_delivery_status(
        db,
        provider_message_id="provider-1",
        delivery_status="read",
        delivered_at=later_at,
    )
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-1"})
    assert first["activation"]["status"] == "activated"
    assert second["activation"]["status"] == "already_active"
    assert notification["delivered_at"] == webhook_at.replace(tzinfo=None)
    assert cycle["sla_started_at"] == webhook_at.replace(tzinfo=None)


def test_delivered_without_provider_timestamp_never_fabricates_sla_start():
    db = _fixture()
    result = reconcile_one_sla_reassignment_delivery_sync(
        db,
        worker_id="reconciler-1",
        now=datetime(2026, 9, 20, 12, 5, tzinfo=UTC),
        status_getter=lambda _provider_id: _status("delivered"),
    )
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-1"})
    assert result["status"] == "delivered_timestamp_missing"
    assert notification["actually_delivered"] is True
    assert notification["delivery_timestamp_missing"] is True
    assert "delivered_at" not in notification
    assert cycle["sla_started_at"] is None
    assert cycle["reassignment_state"] == "AWAITING_OWNER_NOTIFICATION"


def test_network_error_keeps_sent_without_sla_or_send():
    db = _fixture()

    def getter(_provider_id):
        raise TimeoutError("provider timeout")

    result = reconcile_one_sla_reassignment_delivery_sync(
        db,
        worker_id="reconciler-1",
        now=datetime(2026, 9, 20, 12, 6, tzinfo=UTC),
        status_getter=getter,
    )
    stored = db[COLLECTION].find_one({"_id": "notification-1"})
    assert result["status"] == "network_unknown"
    assert stored["state"] == "sent"
    assert stored["actually_delivered"] is False
    assert stored["next_status_check_at"] is not None
    assert db["crm_assignment_cycles"].find_one({"_id": "cycle-1"})["sla_started_at"] is None


def test_status_auth_error_is_blocked_without_tight_retry():
    db = _fixture()
    calls = []

    def getter(provider_id):
        calls.append(provider_id)
        return _status("unknown", http_status=401, provider_error_code="PROVIDER_AUTH_ERROR")

    first = reconcile_one_sla_reassignment_delivery_sync(
        db, worker_id="reconciler-1", now=datetime(2026, 9, 20, 12, 7, tzinfo=UTC), status_getter=getter
    )
    second = reconcile_one_sla_reassignment_delivery_sync(
        db, worker_id="reconciler-1", now=datetime(2026, 9, 20, 12, 8, tzinfo=UTC), status_getter=getter
    )
    stored = db[COLLECTION].find_one({"_id": "notification-1"})
    assert first["status"] == "provider_auth_error"
    assert second["status"] == "idle"
    assert calls == ["provider-1"]
    assert stored["delivery_error_code"] == "PROVIDER_AUTH_ERROR"
    assert stored["status_reconciliation_blocked"] is True


def test_provider_confirmed_failed_is_terminal_for_reconciliation_without_resend():
    db = _fixture()
    result = reconcile_one_sla_reassignment_delivery_sync(
        db,
        worker_id="reconciler-1",
        now=datetime(2026, 9, 20, 12, 9, tzinfo=UTC),
        status_getter=lambda _provider_id: _status("failed"),
    )
    stored = db[COLLECTION].find_one({"_id": "notification-1"})
    assert result["status"] == "provider_confirmed_failed"
    assert stored["delivery_error_code"] == "PROVIDER_CONFIRMED_FAILED"
    assert stored["status_reconciliation_blocked"] is True
    assert db["crm_assignment_cycles"].find_one({"_id": "cycle-1"})["sla_started_at"] is None


def test_previous_owner_delivery_is_observable_only_and_never_starts_sla():
    db = _fixture(notification_type=SLA_REASSIGNED_AWAY)
    result = reconcile_one_sla_reassignment_delivery_sync(
        db,
        worker_id="reconciler-1",
        now=datetime(2026, 9, 20, 12, 10, tzinfo=UTC),
        status_getter=lambda _provider_id: _status(
            "delivered", timestamp=datetime(2026, 9, 20, 12, 10, tzinfo=UTC)
        ),
    )
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-1"})
    assert result["status"] == "delivered"
    assert cycle["reassignment_state"] == "AWAITING_OWNER_NOTIFICATION"
    assert cycle["sla_started_at"] is None


def test_consumer_restart_reconciles_sent_notification_without_second_send():
    db = _fixture(state="pending", provider_id=None)
    db["usuarios"].insert_one({"_id": "user-new", "telefono": "+56911111111", "is_active": True})
    send_calls = []
    sent = process_one_sla_reassignment_sync(
        db,
        worker_id="sender-1",
        now=datetime(2026, 9, 20, 12, 11, tzinfo=UTC),
        sender=lambda *_: send_calls.append(1) or {
            "success": True,
            "provider_message_id": "provider-1",
            "http_status": 200,
        },
    )
    reconciled = reconcile_one_sla_reassignment_delivery_sync(
        db,
        worker_id="reconciler-after-restart",
        now=datetime(2026, 9, 20, 12, 12, tzinfo=UTC),
        status_getter=lambda _provider_id: _status(
            "delivered", timestamp=datetime(2026, 9, 20, 12, 12, tzinfo=UTC)
        ),
    )
    assert sent["status"] == "sent"
    assert reconciled["status"] == "delivered"
    assert send_calls == [1]


def test_status_client_extracts_provider_delivery_timestamp_without_local_fallback(monkeypatch):
    class Response:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"success": True, "data": {"status": 3, "deliveredAt": "2026-09-20T12:13:00Z"}}

    monkeypatch.setattr(whatsapp_client.requests, "get", lambda *args, **kwargs: Response())
    result = asyncio.run(whatsapp_client.get_whatsapp_message_status("provider-1"))
    assert result["delivery_status"] == "delivered"
    assert result["provider_event_timestamp"] == datetime(2026, 9, 20, 12, 13, tzinfo=UTC)
    assert result["provider_event_timestamp_source"] == "deliveredAt"
