"""Structural tests for v2 captación follow-up links and delivery lifecycle."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import mongomock
import pytest
from bson import ObjectId
from fastapi import HTTPException
from starlette.requests import Request
from urllib.parse import parse_qs, urlsplit

from config import Config
from chatbot.captacion_reminder import (
    MAX_DELIVERY_ATTEMPTS,
    claim_due_reminder,
    process_one_due_reminder,
)
from chatbot.followup_tracking import (
    FollowupConfigurationError,
    FollowupTokenError,
    LEGACY_TRACKING_VERSION,
    TOKEN_VERSION,
    TRACKING_VERSION,
    issue_followup_token,
    _issue_legacy_followup_token,
    record_followup_event,
    record_followup_open,
    validate_followup_token_runtime,
    verify_followup_token,
)


UTC_NOW = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def v2_secret(monkeypatch):
    value = "followup-v2-test-secret-" + "x" * 24
    monkeypatch.setattr(Config, "FOLLOWUP_TOKEN_SECRET", value)
    monkeypatch.setattr(Config, "FOLLOWUP_V1_COMPAT_ENABLED", True)
    return value


def _task(*, task_id="task-1", obj_id=None, status="pending", attempts=0, **extra):
    obj_id = obj_id or str(ObjectId())
    execute_at = UTC_NOW - timedelta(minutes=1)
    task = {
        "_id": ObjectId(),
        "task_id": task_id,
        "message_domain": "captacion_reminder",
        "message_type": "scheduled_reminder",
        "lead_type": "captacion",
        "followup_tracking_version": TRACKING_VERSION,
        "followup_token_version": TOKEN_VERSION,
        "created_by_user_id": "user-1",
        "recipient_user_id": "user-1",
        "target_user_id": "user-1",
        "obj_id": obj_id,
        "status": status,
        "execute_at": execute_at,
        "scheduled_at": execute_at,
        "attempts": attempts,
        "created_at": UTC_NOW - timedelta(hours=1),
    }
    task.update(extra)
    return task


def _db_with_task(task):
    client = mongomock.MongoClient()
    db = client["test_followups"]
    db["crm_tasks"].insert_one(dict(task))
    return db


def _import_webhook_without_network_initialization(monkeypatch):
    import chatbot.storage as storage

    bootstrap_db = mongomock.MongoClient()["webhook_import"]
    monkeypatch.setattr(storage, "get_db", lambda: bootstrap_db)
    import webhook

    return webhook


def test_v2_token_round_trip_and_payload_shape(v2_secret):
    token = issue_followup_token("task-1", now=UTC_NOW)
    payload = verify_followup_token(token, now=UTC_NOW)

    assert payload == {"v": 2, "task_id": "task-1", "exp": int((UTC_NOW + timedelta(days=90)).timestamp())}
    assert v2_secret not in token


def test_v2_token_rejects_alteration_expiry_and_different_secret(v2_secret, monkeypatch):
    token = issue_followup_token("task-1", now=UTC_NOW)
    body, signature = token.split(".", 1)
    altered = f"{body[:-1]}{'A' if body[-1] != 'A' else 'B'}.{signature}"
    with pytest.raises(FollowupTokenError, match="followup_token_invalid"):
        verify_followup_token(altered, now=UTC_NOW)

    expired = issue_followup_token("task-1", now=UTC_NOW - timedelta(days=91))
    with pytest.raises(FollowupTokenError, match="followup_token_expired"):
        verify_followup_token(expired, now=UTC_NOW)

    monkeypatch.setattr(Config, "FOLLOWUP_TOKEN_SECRET", "another-secret-" + "y" * 24)
    with pytest.raises(FollowupTokenError, match="followup_token_invalid"):
        verify_followup_token(token, now=UTC_NOW)


def test_malformed_payload_and_controlled_v1_compatibility(v2_secret, monkeypatch):
    malformed_body = base64.urlsafe_b64encode(b"not-json").decode().rstrip("=")
    with pytest.raises(FollowupTokenError, match="followup_token_invalid"):
        verify_followup_token(f"{malformed_body}.bad")

    legacy = _issue_legacy_followup_token("legacy-task", now=UTC_NOW)
    assert verify_followup_token(legacy, now=UTC_NOW)["v"] == 1
    monkeypatch.setattr(Config, "FOLLOWUP_V1_COMPAT_ENABLED", False)
    with pytest.raises(FollowupConfigurationError):
        verify_followup_token(legacy, now=UTC_NOW)


def test_missing_v2_secret_blocks_issue_even_outside_production(monkeypatch):
    monkeypatch.setattr(Config, "FOLLOWUP_TOKEN_SECRET", None)
    with pytest.raises(FollowupConfigurationError):
        issue_followup_token("task-1", now=UTC_NOW)


def test_production_startup_requires_dedicated_secret(monkeypatch):
    monkeypatch.setattr(Config, "FOLLOWUP_TOKEN_SECRET", None)
    monkeypatch.setattr(Config, "IS_PRODUCTION", True)
    with pytest.raises(RuntimeError, match="FOLLOWUP_TOKEN_SECRET"):
        Config.validate_followup_token_configuration()


def test_runtime_probe_uses_the_same_v2_configuration_for_issue_and_verify(v2_secret):
    result = validate_followup_token_runtime()
    assert result["valid"] is True
    assert result["version"] == 2
    assert result["probe"] == "ok"


def test_v2_task_open_requires_existing_non_terminal_task_and_recipient(v2_secret):
    task = _task()
    db = _db_with_task(task)
    token = issue_followup_token(task["task_id"], now=UTC_NOW)

    event = record_followup_open(
        db, token=token, entity_id=task["obj_id"], actor_user_id="user-1"
    )
    assert event["event_type"] == "lead_opened"
    assert event["actor_user_id"] == "user-1"

    with pytest.raises(FollowupTokenError, match="followup_actor_forbidden"):
        record_followup_open(
            db, token=token, entity_id=task["obj_id"], actor_user_id="user-2"
        )

    for terminal_status, resolution in (("cancelled", None), ("pending", "superseded")):
        terminal = _task(task_id=f"{terminal_status}-{resolution}", status=terminal_status, resolution=resolution)
        terminal_db = _db_with_task(terminal)
        terminal_token = issue_followup_token(terminal["task_id"], now=UTC_NOW)
        with pytest.raises(FollowupTokenError, match="followup_task_unavailable"):
            record_followup_open(
                terminal_db,
                token=terminal_token,
                entity_id=terminal["obj_id"],
                actor_user_id="user-1",
            )


def test_reminder_clicked_is_idempotent_and_updates_task_timestamp(v2_secret):
    task = _task()
    db = _db_with_task(task)
    first = record_followup_event(
        db,
        task=task,
        event_type="reminder_clicked",
        actor_user_id="user-1",
        occurred_at=UTC_NOW,
    )
    second = record_followup_event(
        db,
        task=task,
        event_type="reminder_clicked",
        actor_user_id="user-1",
        occurred_at=UTC_NOW + timedelta(seconds=2),
    )

    assert second["_id"] == first["_id"]
    assert db["followup_events"].count_documents({"task_id": task["task_id"]}) == 1
    clicked_at = db["crm_tasks"].find_one({"_id": task["_id"]})["clicked_at"]
    assert clicked_at.replace(tzinfo=timezone.utc) == UTC_NOW


def test_worker_full_send_generates_self_verifiable_v2_url(v2_secret, monkeypatch):
    property_id = ObjectId()
    task = _task(obj_id=str(property_id))
    db = _db_with_task(task)
    db["usuarios"].insert_one({"_id": "user-1", "nombre": "Ana", "telefono": "56911111111", "is_active": True})
    db["propiedades_captacion"].insert_one({
        "_id": property_id,
        "codigo": "P-1",
        "tipo_propiedad": "departamento",
        "comuna": "Providencia",
        "operacion": "venta",
        "precio_uf": 6650,
        "gestion": {"estado_captacion": "Sin respuesta", "notas": []},
    })
    sent = []

    async def fake_send(phone, content):
        sent.append((phone, content))
        return {"success": True, "provider_message_id": "provider-1"}

    monkeypatch.setattr("chatbot.whatsapp_client.send_whatsapp_message_detailed", fake_send)
    result = asyncio.run(process_one_due_reminder(db, worker_id="worker-1"))

    assert result["status"] == "notified"
    assert len(sent) == 1
    token = sent[0][1].split("/followup/open/", 1)[1].split()[0]
    payload = verify_followup_token(token, now=UTC_NOW)
    assert payload["v"] == 2
    saved = db["crm_tasks"].find_one({"_id": task["_id"]})
    assert saved["status"] == "notified"


def test_long_future_reminder_emits_at_delivery_and_expires_from_execute_at(v2_secret, monkeypatch):
    import chatbot.captacion_reminder as reminder_module
    import chatbot.followup_tracking as tracking_module

    created_at = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    execute_at = datetime(2027, 6, 30, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(reminder_module, "utc_now", lambda: execute_at)
    emitted_at = []
    original_issue = tracking_module.issue_followup_token

    def capture_issue(task_id, *, expires_at=None, now=None):
        emitted_at.append(now)
        return original_issue(task_id, expires_at=expires_at, now=now)

    monkeypatch.setattr(tracking_module, "issue_followup_token", capture_issue)

    property_id = ObjectId()
    task = _task(
        obj_id=str(property_id),
        created_at=created_at,
        execute_at=execute_at,
        scheduled_at=execute_at,
        followup_tracking_version=LEGACY_TRACKING_VERSION,
        followup_token_version=1,
    )
    task.pop("created_by_user_id")
    db = _db_with_task(task)
    db["usuarios"].insert_one({"_id": "user-1", "nombre": "Ana", "telefono": "56911111111", "is_active": True})
    db["propiedades_captacion"].insert_one({"_id": property_id, "gestion": {"estado_captacion": "Sin respuesta", "notas": []}})
    sent = []

    async def fake_send(phone, content):
        sent.append(content)
        return {"success": True, "provider_message_id": "provider-future"}

    monkeypatch.setattr("chatbot.whatsapp_client.send_whatsapp_message_detailed", fake_send)
    result = asyncio.run(reminder_module.process_one_due_reminder(db, worker_id="worker-future"))

    assert result["status"] == "notified"
    assert emitted_at == [execute_at]
    token = sent[0].split("/followup/open/", 1)[1].split()[0]
    payload = verify_followup_token(token, now=execute_at)
    assert payload["v"] == TOKEN_VERSION
    assert payload["exp"] == int((execute_at + timedelta(days=90)).timestamp())
    saved = db["crm_tasks"].find_one({"_id": task["_id"]})
    assert saved["created_at"].replace(tzinfo=timezone.utc) == created_at
    assert saved["followup_tracking_version"] == TRACKING_VERSION
    assert saved["followup_token_version"] == TOKEN_VERSION
    assert "token" not in saved


@pytest.mark.parametrize("legacy_status", ["failed_retryable", "delivery_unknown", "processing"])
def test_legacy_unsent_retryable_and_stale_states_promote_to_v2(v2_secret, monkeypatch, legacy_status):
    import chatbot.captacion_reminder as reminder_module
    import chatbot.followup_tracking as tracking_module

    property_id = ObjectId()
    extra = {
        "followup_tracking_version": LEGACY_TRACKING_VERSION,
        "followup_token_version": 1,
        "recipient_user_id": "user-1",
    }
    if legacy_status in {"failed_retryable", "delivery_unknown"}:
        extra["next_attempt_at"] = UTC_NOW - timedelta(seconds=1)
    else:
        extra["processing_lease_until"] = UTC_NOW - timedelta(seconds=1)
        extra["lease_token"] = "stale-legacy-token"
    task = _task(obj_id=str(property_id), status=legacy_status, **extra)
    db = _db_with_task(task)
    db["usuarios"].insert_one({"_id": "user-1", "nombre": "Ana", "telefono": "56911111111", "is_active": True})
    db["propiedades_captacion"].insert_one({"_id": property_id, "gestion": {"estado_captacion": "Sin respuesta", "notas": []}})
    emitted_versions = []
    original_issue = tracking_module.issue_followup_token

    def capture_issue(task_id, *, expires_at=None, now=None):
        emitted_versions.append(2)
        return original_issue(task_id, expires_at=expires_at, now=now)

    monkeypatch.setattr(tracking_module, "issue_followup_token", capture_issue)

    async def fake_send(phone, content):
        return {"success": True, "provider_message_id": "provider-legacy"}

    monkeypatch.setattr("chatbot.whatsapp_client.send_whatsapp_message_detailed", fake_send)
    result = asyncio.run(reminder_module.process_one_due_reminder(db, worker_id="worker-legacy"))

    saved = db["crm_tasks"].find_one({"_id": task["_id"]})
    assert result["status"] == "notified"
    assert emitted_versions == [2]
    assert saved["followup_tracking_version"] == TRACKING_VERSION
    assert saved["followup_token_version"] == TOKEN_VERSION
    assert "token" not in saved


def test_legacy_unsent_task_without_recipient_is_not_claimed(v2_secret):
    task = _task(
        status="pending",
        followup_tracking_version=LEGACY_TRACKING_VERSION,
        followup_token_version=1,
    )
    task.pop("recipient_user_id")
    task.pop("target_user_id")
    db = _db_with_task(task)

    assert claim_due_reminder(db, worker_id="worker-unsafe", now=UTC_NOW) is None
    saved = db["crm_tasks"].find_one({"_id": task["_id"]})
    assert saved["status"] == "pending"
    assert "followup_tracking_version" not in saved or saved["followup_tracking_version"] == LEGACY_TRACKING_VERSION


def test_delivery_unknown_is_retryable_and_schedules_next_attempt(v2_secret, monkeypatch):
    property_id = ObjectId()
    task = _task(obj_id=str(property_id))
    db = _db_with_task(task)
    db["usuarios"].insert_one({"_id": "user-1", "nombre": "Ana", "telefono": "56911111111", "is_active": True})
    db["propiedades_captacion"].insert_one({"_id": property_id, "gestion": {"estado_captacion": "Sin respuesta", "notas": []}})

    async def uncertain_send(phone, content):
        return {"success": False, "delivery_status": "delivery_unknown", "provider_call_uncertain": True}

    monkeypatch.setattr("chatbot.whatsapp_client.send_whatsapp_message_detailed", uncertain_send)
    result = asyncio.run(process_one_due_reminder(db, worker_id="worker-unknown"))

    saved = db["crm_tasks"].find_one({"_id": task["_id"]})
    assert result["status"] == "delivery_unknown"
    assert saved["status"] == "delivery_unknown"
    assert saved["provider_called"] is True
    assert saved.get("next_attempt_at") is not None
    assert not saved.get("lease_token")


def test_worker_retries_failed_retryable_and_eventually_stops(v2_secret, monkeypatch):
    property_id = ObjectId()
    task = _task(obj_id=str(property_id))
    db = _db_with_task(task)
    db["usuarios"].insert_one({"_id": "user-1", "nombre": "Ana", "telefono": "56911111111", "is_active": True})
    db["propiedades_captacion"].insert_one({"_id": property_id, "gestion": {"estado_captacion": "Sin respuesta", "notas": []}})
    calls = []

    async def always_fail(phone, content):
        calls.append(1)
        return {"success": False, "delivery_status": "provider_rejected"}

    monkeypatch.setattr("chatbot.whatsapp_client.send_whatsapp_message_detailed", always_fail)
    states = []
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        states.append(asyncio.run(process_one_due_reminder(db, worker_id="worker-1"))["status"])
        db["crm_tasks"].update_one(
            {"_id": task["_id"]}, {"$set": {"next_attempt_at": UTC_NOW - timedelta(seconds=1)}}
        )

    assert states == ["failed_retryable", "failed_retryable", "failed_terminal"]
    assert len(calls) == MAX_DELIVERY_ATTEMPTS
    assert db["crm_tasks"].find_one({"_id": task["_id"]})["status"] == "failed_terminal"


def test_worker_reclaims_expired_processing_but_not_live_processing(v2_secret):
    expired = _task(
        task_id="expired",
        status="processing",
        processing_lease_until=UTC_NOW - timedelta(seconds=1),
        lease_token="old-token",
        history=[{"state": "processing", "at": UTC_NOW - timedelta(minutes=11)}],
    )
    db = _db_with_task(expired)
    claimed = claim_due_reminder(db, worker_id="worker-new", now=UTC_NOW)
    assert claimed["task_id"] == "expired"
    assert claimed["lease_owner"] == "worker-new"
    assert claimed["lease_token"] != "old-token"

    live = _task(
        task_id="live",
        status="processing",
        processing_lease_until=UTC_NOW + timedelta(minutes=5),
        lease_token="live-token",
    )
    live_db = _db_with_task(live)
    assert claim_due_reminder(live_db, worker_id="worker-other", now=UTC_NOW) is None


def test_two_workers_cannot_claim_same_pending_task(v2_secret):
    task = _task()
    db = _db_with_task(task)
    first = claim_due_reminder(db, worker_id="worker-1", now=UTC_NOW)
    second = claim_due_reminder(db, worker_id="worker-2", now=UTC_NOW)
    assert first is not None
    assert second is None


def test_worker_does_not_call_provider_when_token_preflight_fails(monkeypatch):
    monkeypatch.setattr(Config, "FOLLOWUP_TOKEN_SECRET", None)
    property_id = ObjectId()
    task = _task(obj_id=str(property_id))
    db = _db_with_task(task)
    db["usuarios"].insert_one({"_id": "user-1", "nombre": "Ana", "telefono": "56911111111", "is_active": True})
    db["propiedades_captacion"].insert_one({"_id": property_id, "gestion": {"estado_captacion": "Sin respuesta", "notas": []}})
    calls = []

    async def should_not_send(phone, content):
        calls.append(1)
        return {"success": True}

    monkeypatch.setattr("chatbot.whatsapp_client.send_whatsapp_message_detailed", should_not_send)
    result = asyncio.run(process_one_due_reminder(db, worker_id="worker-1"))
    assert result["status"] == "failed_retryable"
    assert calls == []


def test_public_followup_route_authenticates_before_click(monkeypatch, v2_secret):
    webhook = _import_webhook_without_network_initialization(monkeypatch)

    task = _task()
    token = issue_followup_token(task["task_id"], now=UTC_NOW)
    fake_db = object()
    events = []
    monkeypatch.setattr(webhook, "find_tracked_task", lambda db, task_id, token_version=None: task)
    monkeypatch.setattr(webhook, "get_captacion_detail", lambda obj_id: {"id": obj_id, "gestion": {"ejecutivo_id": "user-1"}})
    monkeypatch.setattr(webhook, "can_manage_captacion", lambda user, prop: True)
    monkeypatch.setattr(webhook, "record_followup_event", lambda db, **kwargs: events.append(kwargs))
    monkeypatch.setattr("chatbot.storage.get_db", lambda: fake_db)

    async def anonymous(_request):
        raise HTTPException(status_code=401, detail="No autenticado")

    monkeypatch.setattr(webhook, "get_current_user_doc", anonymous)
    anonymous_response = asyncio.run(webhook.open_followup_link(SimpleNamespace(), token))
    assert anonymous_response.status_code in {302, 307}
    assert "followup/open" in anonymous_response.headers["location"] or "followup/open" in anonymous_response.headers.get("set-cookie", "")
    assert events == []

    async def other_user(_request):
        return {"_id": "user-2", "rol": "agente"}

    monkeypatch.setattr(webhook, "get_current_user_doc", other_user)
    with pytest.raises(HTTPException) as forbidden:
        asyncio.run(webhook.open_followup_link(SimpleNamespace(), token))
    assert forbidden.value.status_code == 403
    assert events == []

    async def owner(_request):
        return {"_id": "user-1", "rol": "agente"}

    monkeypatch.setattr(webhook, "get_current_user_doc", owner)
    response = asyncio.run(webhook.open_followup_link(SimpleNamespace(), token))
    assert response.status_code == 302
    assert events and events[-1]["event_type"] == "reminder_clicked"
    assert events[-1]["actor_user_id"] == "user-1"


def test_followup_login_returns_to_same_reminder(monkeypatch, v2_secret):
    webhook = _import_webhook_without_network_initialization(monkeypatch)

    task = _task()
    token = issue_followup_token(task["task_id"], now=UTC_NOW)
    fake_db = object()
    monkeypatch.setattr(webhook, "find_tracked_task", lambda db, task_id, token_version=None: task)
    monkeypatch.setattr("chatbot.storage.get_db", lambda: fake_db)

    async def anonymous(_request):
        raise HTTPException(status_code=401, detail="No autenticado")

    monkeypatch.setattr(webhook, "get_current_user_doc", anonymous)
    open_request = Request({
        "type": "http", "method": "GET", "path": f"/followup/open/{token}",
        "query_string": b"", "headers": [], "scheme": "https",
        "server": ("crm.example", 443), "client": ("127.0.0.1", 1),
    })
    response = asyncio.run(webhook.open_followup_link(open_request, token))
    next_url = parse_qs(urlsplit(response.headers["location"]).query)["next"][0]
    assert next_url == f"/followup/open/{token}"
    assert f"/followup/open/{token}" in response.headers["set-cookie"]

    class FakeUsers:
        async def find_one(self, query):
            return {"_id": "user-1", "username": "user-1", "rol": "agente", "hashed_password": "hash"}

    class FakeAsyncDb:
        def __getitem__(self, name):
            assert name == "usuarios"
            return FakeUsers()

    monkeypatch.setattr("chatbot.storage.get_async_db", lambda: FakeAsyncDb())
    monkeypatch.setattr(webhook, "verify_password", lambda password, hashed: True)
    monkeypatch.setattr(webhook, "create_access_token", lambda data: "jwt-test")
    login_request = Request({
        "type": "http", "method": "POST", "path": "/login",
        "query_string": b"", "headers": [(b"cookie", f"login_next={next_url}".encode())],
        "scheme": "https", "server": ("crm.example", 443), "client": ("127.0.0.1", 1),
    })
    login_response = asyncio.run(webhook.login_post(login_request, "user-1", "pw", None))
    assert login_response.status_code == 303
    assert login_response.headers["location"] == next_url


def test_followup_authorization_matrix_rejects_non_recipient_even_if_admin(monkeypatch, v2_secret):
    webhook = _import_webhook_without_network_initialization(monkeypatch)

    task = _task()
    token = issue_followup_token(task["task_id"], now=UTC_NOW)
    fake_db = object()
    events = []
    monkeypatch.setattr(webhook, "find_tracked_task", lambda db, task_id, token_version=None: task)
    monkeypatch.setattr(webhook, "get_captacion_detail", lambda obj_id: {"id": obj_id, "gestion": {"ejecutivo_id": "user-1"}})
    monkeypatch.setattr(webhook, "record_followup_event", lambda db, **kwargs: events.append(kwargs))
    monkeypatch.setattr("chatbot.storage.get_db", lambda: fake_db)

    async def anonymous(_request):
        raise HTTPException(status_code=401, detail="No autenticado")

    monkeypatch.setattr(webhook, "get_current_user_doc", anonymous)
    anonymous_response = asyncio.run(webhook.open_followup_link(SimpleNamespace(), token))
    assert anonymous_response.status_code in {302, 307}
    assert events == []

    async def other_executive(_request):
        return {"_id": "user-2", "rol": "agente"}

    monkeypatch.setattr(webhook, "get_current_user_doc", other_executive)
    with pytest.raises(HTTPException) as wrong_recipient:
        asyncio.run(webhook.open_followup_link(SimpleNamespace(), token))
    assert wrong_recipient.value.status_code == 403
    assert events == []

    async def recipient_without_permission(_request):
        return {"_id": "user-1", "rol": "agente"}

    monkeypatch.setattr(webhook, "get_current_user_doc", recipient_without_permission)
    monkeypatch.setattr(webhook, "can_manage_captacion", lambda user, prop: False)
    with pytest.raises(HTTPException) as no_permission:
        asyncio.run(webhook.open_followup_link(SimpleNamespace(), token))
    assert no_permission.value.status_code == 403
    assert events == []

    monkeypatch.setattr(webhook, "can_manage_captacion", lambda user, prop: True)
    allowed = asyncio.run(webhook.open_followup_link(SimpleNamespace(), token))
    assert allowed.status_code == 302
    assert len(events) == 1
    assert events[0]["event_type"] == "reminder_clicked"
    assert events[0]["actor_user_id"] == "user-1"

    async def administrator(_request):
        return {"_id": "user-admin", "rol": "admin"}

    monkeypatch.setattr(webhook, "get_current_user_doc", administrator)
    with pytest.raises(HTTPException) as admin_not_recipient:
        asyncio.run(webhook.open_followup_link(SimpleNamespace(), token))
    assert admin_not_recipient.value.status_code == 403
    assert len(events) == 1
