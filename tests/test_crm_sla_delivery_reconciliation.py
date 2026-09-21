import asyncio
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import mongomock

from chatbot import whatsapp_client
from chatbot.constants import CHILE_TZ
from chatbot.crm_metrics import calculate_sla
from chatbot.crm_notifications import individual_identity
from chatbot.crm_sla_reassignment_notifications import (
    COLLECTION,
    SLA_REASSIGNED_AWAY,
    SLA_REASSIGNED_TO,
    reconcile_one_sla_reassignment_delivery_sync,
    recover_sla_reassignment_delivery_from_evidence,
    record_sla_reassignment_delivery_status,
    process_one_sla_reassignment_sync,
    HISTORICAL_DELIVERY_EVIDENCE_SOURCE,
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
        "http_status": 200,
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


def test_delivered_then_sent_cannot_regress_delivery_or_clock():
    db = _fixture()
    first_at = datetime(2026, 9, 20, 12, 14, tzinfo=UTC)
    first = record_sla_reassignment_delivery_status(
        db,
        provider_message_id="provider-1",
        delivery_status="delivered",
        delivered_at=first_at,
    )
    second = record_sla_reassignment_delivery_status(
        db,
        provider_message_id="provider-1",
        delivery_status="sent",
    )
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-1"})
    assert first["activation"]["status"] == "activated"
    assert second["provider_status"] == "delivered"
    assert notification["delivery_status"] == "delivered"
    assert notification["actually_delivered"] is True
    assert notification["provider_status_regression_observed"] is True
    assert notification["effective_delivery_at"] == first_at.replace(tzinfo=None)
    assert cycle["sla_started_at"] == first_at.replace(tzinfo=None)


def test_concurrent_delivered_and_sent_keeps_confirmed_delivery():
    db = _fixture()
    barrier = Barrier(2)

    def invoke(status):
        barrier.wait()
        return record_sla_reassignment_delivery_status(
            db,
            provider_message_id="provider-1",
            delivery_status=status,
            delivered_at=datetime(2026, 9, 20, 12, 14, tzinfo=UTC)
            if status == "delivered" else None,
        )

    # The barrier is deliberately used only to make both callers overlap;
    # Mongo CAS, not test ordering, decides the winner.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, ("delivered", "sent")))
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    assert notification["actually_delivered"] is True
    assert notification["delivery_status"] == "delivered"
    assert notification["effective_delivery_at"] is not None
    assert any(result["provider_status"] == "delivered" for result in results)


def test_concurrent_delivered_and_failed_keeps_confirmed_delivery():
    db = _fixture()
    barrier = Barrier(2)

    def invoke(status):
        barrier.wait()
        return record_sla_reassignment_delivery_status(
            db,
            provider_message_id="provider-1",
            delivery_status=status,
            delivered_at=datetime(2026, 9, 20, 12, 14, tzinfo=UTC)
            if status == "delivered" else None,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, ("delivered", "failed")))
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    assert notification["actually_delivered"] is True
    assert notification["delivery_status"] == "delivered"
    assert notification["effective_delivery_at"] is not None
    assert any(result["provider_status"] == "delivered" for result in results)


def test_concurrent_read_and_sent_keeps_confirmed_delivery():
    db = _fixture()
    barrier = Barrier(2)

    def invoke(status):
        barrier.wait()
        return record_sla_reassignment_delivery_status(
            db,
            provider_message_id="provider-1",
            delivery_status=status,
            delivered_at=datetime(2026, 9, 20, 12, 15, tzinfo=UTC)
            if status == "read" else None,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, ("read", "sent")))
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    assert notification["actually_delivered"] is True
    assert notification["delivery_status"] == "read"
    assert notification["effective_delivery_at"] is not None
    assert any(result["provider_status"] == "read" for result in results)


def test_concurrent_delivered_observers_share_one_immutable_effective_clock():
    db = _fixture()
    barrier = Barrier(2)
    first_at = datetime(2026, 9, 20, 12, 16, tzinfo=UTC)
    second_at = first_at + timedelta(seconds=1)

    def invoke(observed_at):
        barrier.wait()
        return record_sla_reassignment_delivery_status(
            db,
            provider_message_id="provider-1",
            delivery_status="delivered",
            delivery_confirmed_observed_at=observed_at,
            authenticated_provider_confirmation=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, (first_at, second_at)))
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    assert notification["effective_delivery_at"] in {
        first_at.replace(tzinfo=None), second_at.replace(tzinfo=None)
    }
    assert notification["delivery_confirmed_observed_at"] == notification["effective_delivery_at"]
    assert all(
        result["effective_delivery_at"].replace(tzinfo=None) == notification["effective_delivery_at"]
        for result in results
    )


def test_read_then_delivered_preserves_higher_status_and_clock():
    db = _fixture()
    first_at = datetime(2026, 9, 20, 12, 15, tzinfo=UTC)
    first = record_sla_reassignment_delivery_status(
        db,
        provider_message_id="provider-1",
        delivery_status="read",
        delivered_at=first_at,
    )
    second = record_sla_reassignment_delivery_status(
        db,
        provider_message_id="provider-1",
        delivery_status="delivered",
        delivered_at=first_at + timedelta(seconds=10),
    )
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    assert first["activation"]["status"] == "activated"
    assert second["provider_status"] == "read"
    assert notification["delivery_status"] == "read"
    assert notification["effective_delivery_at"] == first_at.replace(tzinfo=None)


def test_fallback_clock_does_not_move_when_later_webhook_has_older_provider_time():
    db = _fixture()
    observed_at = datetime(2026, 9, 20, 12, 16, tzinfo=UTC)
    provider_at = datetime(2026, 9, 20, 12, 10, tzinfo=UTC)
    first = reconcile_one_sla_reassignment_delivery_sync(
        db,
        worker_id="reconciler-1",
        now=observed_at,
        status_getter=lambda _provider_id: _status("delivered"),
    )
    second = record_sla_reassignment_delivery_status(
        db,
        provider_message_id="provider-1",
        delivery_status="delivered",
        delivered_at=provider_at,
        provider_timestamp_source="webhook.timestamp",
    )
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    assert first["status"] == "delivered"
    assert second["status"] == "delivered"
    assert notification["effective_delivery_at"] == observed_at.replace(tzinfo=None)
    assert notification["delivery_time_source"] == "PROVIDER_STATUS_FIRST_OBSERVED_AT"
    assert notification["provider_delivered_at"] == provider_at.replace(tzinfo=None)


def test_two_delivery_observers_preserve_one_first_observation():
    db = _fixture()
    first_at = datetime(2026, 9, 20, 12, 17, tzinfo=UTC)
    second_at = first_at + timedelta(seconds=1)
    first = record_sla_reassignment_delivery_status(
        db,
        provider_message_id="provider-1",
        delivery_status="delivered",
        delivery_confirmed_observed_at=first_at,
        authenticated_provider_confirmation=True,
    )
    second = record_sla_reassignment_delivery_status(
        db,
        provider_message_id="provider-1",
        delivery_status="delivered",
        delivery_confirmed_observed_at=second_at,
        authenticated_provider_confirmation=True,
    )
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    assert first["status"] == "delivered"
    assert second["status"] == "delivered"
    assert notification["effective_delivery_at"] == first_at.replace(tzinfo=None)
    assert notification["delivery_confirmed_observed_at"] == first_at.replace(tzinfo=None)


def test_delivered_without_provider_timestamp_uses_first_authenticated_observation():
    db = _fixture()
    result = reconcile_one_sla_reassignment_delivery_sync(
        db,
        worker_id="reconciler-1",
        now=datetime(2026, 9, 20, 12, 5, tzinfo=UTC),
        status_getter=lambda _provider_id: _status("delivered"),
    )
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-1"})
    assert result["status"] == "delivered"
    assert notification["actually_delivered"] is True
    assert notification["delivery_timestamp_missing"] is False
    assert notification["delivery_time_source"] == "PROVIDER_STATUS_FIRST_OBSERVED_AT"
    assert notification["delivery_confirmed_observed_at"] == datetime(2026, 9, 20, 12, 5)
    assert notification["effective_delivery_at"] == datetime(2026, 9, 20, 12, 5)
    assert "delivered_at" not in notification
    assert cycle["sla_started_at"] == datetime(2026, 9, 20, 12, 5)
    assert cycle["reassignment_state"] == "active"


def test_read_without_provider_timestamp_uses_first_authenticated_observation():
    db = _fixture()
    observed_at = datetime(2026, 9, 20, 12, 5, 30, tzinfo=UTC)
    result = reconcile_one_sla_reassignment_delivery_sync(
        db,
        worker_id="reconciler-1",
        now=observed_at,
        status_getter=lambda _provider_id: _status("read"),
    )
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-1"})
    assert result["status"] == "delivered"
    assert notification["delivery_status"] == "read"
    assert notification["delivery_time_source"] == "PROVIDER_STATUS_FIRST_OBSERVED_AT"
    assert notification["effective_delivery_at"] == observed_at.replace(tzinfo=None)
    assert cycle["sla_started_at"] == observed_at.replace(tzinfo=None)


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


def test_wrong_provider_id_cannot_confirm_delivery():
    db = _fixture()
    result = reconcile_one_sla_reassignment_delivery_sync(
        db,
        worker_id="reconciler-1",
        now=datetime(2026, 9, 20, 12, 8, tzinfo=UTC),
        status_getter=lambda _provider_id: _status(
            "delivered", provider_message_id="wrong-provider-id"
        ),
    )
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-1"})
    assert result["status"] == "provider_confirmation_invalid"
    assert notification.get("effective_delivery_at") is None
    assert cycle["sla_started_at"] is None


def test_owner_change_blocks_authenticated_delivery_fallback():
    db = _fixture()
    db["leads"].update_one(
        {"_id": "lead-1"},
        {"$set": {
            "assignment_mirror_owner_user_id": "other-user",
            "lifecycle.assigned_to_user_id": "other-user",
        }},
    )
    result = reconcile_one_sla_reassignment_delivery_sync(
        db,
        worker_id="reconciler-1",
        now=datetime(2026, 9, 20, 12, 9, tzinfo=UTC),
        status_getter=lambda _provider_id: _status("delivered"),
    )
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    assert result["status"] == "stale"
    assert notification.get("effective_delivery_at") is None


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
    db["crm_assignment_cycles"].update_one(
        {"_id": "cycle-1"},
        {"$set": {"cycle_status": "reassigned"}},
    )
    db["leads"].update_one(
        {"_id": "lead-1"},
        {"$set": {"lifecycle.current_assignment_cycle_id": "destination-cycle"}},
    )
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


def test_historical_delivery_recovery_is_idempotent_and_activates_existing_cycle():
    db = _fixture()
    observed_at = datetime(2026, 9, 21, 2, 4, 29, tzinfo=UTC)
    first = recover_sla_reassignment_delivery_from_evidence(
        db,
        notification_id="notification-1",
        provider_message_id="provider-1",
        first_confirmed_observed_at=observed_at,
        evidence_source=HISTORICAL_DELIVERY_EVIDENCE_SOURCE,
        evidence_reference="render-event-123",
        now=observed_at + timedelta(seconds=1),
    )
    second = recover_sla_reassignment_delivery_from_evidence(
        db,
        notification_id="notification-1",
        provider_message_id="provider-1",
        first_confirmed_observed_at=observed_at + timedelta(seconds=1),
        evidence_source=HISTORICAL_DELIVERY_EVIDENCE_SOURCE,
        evidence_reference="render-event-123",
        now=observed_at + timedelta(seconds=2),
    )
    notification = db[COLLECTION].find_one({"_id": "notification-1"})
    cycle = db["crm_assignment_cycles"].find_one({"_id": "cycle-1"})
    assert first["status"] == "recovered"
    assert first["activation"]["status"] == "activated"
    assert second["status"] == "already_recovered"
    assert notification["delivery_time_source"] == "PROVIDER_STATUS_FIRST_OBSERVED_AT"
    assert notification["delivery_evidence_reference"] == "render-event-123"
    assert "delivered_at" not in notification
    assert cycle["sla_started_at"] == observed_at.replace(tzinfo=None)


def test_historical_recovery_rejects_non_authenticated_source():
    db = _fixture()
    observed_at = datetime(2026, 9, 21, 2, 4, 29, tzinfo=UTC)
    result = recover_sla_reassignment_delivery_from_evidence(
        db,
        notification_id="notification-1",
        provider_message_id="provider-1",
        first_confirmed_observed_at=observed_at,
        evidence_source="RENDER_LOG",
        evidence_reference="render-event-123",
        now=observed_at + timedelta(seconds=1),
    )
    assert result == {"status": "blocked", "reason": "unsupported_evidence_source"}


def test_historical_recovery_rejects_observation_before_send():
    db = _fixture()
    observed_at = datetime(2026, 9, 20, 11, 59, tzinfo=UTC)
    result = recover_sla_reassignment_delivery_from_evidence(
        db,
        notification_id="notification-1",
        provider_message_id="provider-1",
        first_confirmed_observed_at=observed_at,
        evidence_source=HISTORICAL_DELIVERY_EVIDENCE_SOURCE,
        evidence_reference="render-event-123",
        now=observed_at + timedelta(seconds=1),
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "evidence_before_send"


def test_historical_recovery_rejects_future_observation():
    db = _fixture()
    now = datetime(2026, 9, 21, 2, 4, 29, tzinfo=UTC)
    result = recover_sla_reassignment_delivery_from_evidence(
        db,
        notification_id="notification-1",
        provider_message_id="provider-1",
        first_confirmed_observed_at=now + timedelta(seconds=61),
        evidence_source=HISTORICAL_DELIVERY_EVIDENCE_SOURCE,
        evidence_reference="render-event-123",
        now=now,
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "evidence_in_future"


def test_business_clock_from_sunday_observation_starts_monday():
    sunday = CHILE_TZ.localize(datetime(2026, 9, 20, 23, 4))
    monday_noon = CHILE_TZ.localize(datetime(2026, 9, 21, 12, 1))
    result = calculate_sla(
        assigned_at=sunday,
        first_valid_management_at=None,
        now=monday_noon,
        temperature="NORMAL",
    )
    assert result["minutes"] == 181
    assert result["status"] == "critical"


def test_historical_case_one_management_is_within_deadline():
    start = CHILE_TZ.localize(datetime(2026, 9, 20, 23, 4))
    managed = CHILE_TZ.localize(datetime(2026, 9, 21, 10, 20))
    result = calculate_sla(
        assigned_at=start, first_valid_management_at=managed,
        now=CHILE_TZ.localize(datetime(2026, 9, 21, 12, 30)), temperature="NORMAL",
    )
    assert result["status"] == "fulfilled"
    assert result["minutes"] == 80


def test_historical_case_two_management_is_within_deadline():
    start = CHILE_TZ.localize(datetime(2026, 9, 20, 23, 4))
    managed = CHILE_TZ.localize(datetime(2026, 9, 21, 9, 27))
    result = calculate_sla(
        assigned_at=start, first_valid_management_at=managed,
        now=CHILE_TZ.localize(datetime(2026, 9, 21, 12, 30)), temperature="NORMAL",
    )
    assert result["status"] == "fulfilled"
    assert result["minutes"] == 27


def test_historical_case_three_without_management_is_reassignable_after_deadline():
    start = CHILE_TZ.localize(datetime(2026, 9, 20, 23, 4))
    result = calculate_sla(
        assigned_at=start, first_valid_management_at=None,
        now=CHILE_TZ.localize(datetime(2026, 9, 21, 12, 1)), temperature="NORMAL",
    )
    assert result["status"] == "critical"
    assert result["minutes"] >= 180


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


def test_status_client_rejects_message_timestamp_as_delivery_clock(monkeypatch):
    class Response:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"success": True, "data": {"status": 3, "messageTimestamp": 1790000000}}

    monkeypatch.setattr(whatsapp_client.requests, "get", lambda *args, **kwargs: Response())
    result = asyncio.run(whatsapp_client.get_whatsapp_message_status("provider-1"))
    assert result["delivery_status"] == "delivered"
    assert result["provider_event_timestamp"] is None
    assert result["provider_event_timestamp_source"] is None
