"""API adapter tests. Uses an in-memory FakeDB, no real HTTP or WaSender."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatbot.executive_whatsapp_gateway import (
    SessionBinding, build_unmounted_executive_audit_router,
)
from tests.test_executive_whatsapp_audit import FakeDB, event

SECRET = "synthetic-test-secret-00000001"
ENDPOINT = "/internal/executive-whatsapp/sim-hernan/events"


def build_client(*, enabled=True, persist=False, db=None, factory=None, max_bytes=65536):
    db = db if db is not None else FakeDB()
    factory = factory if factory is not None else (lambda: db)
    app = FastAPI()
    app.include_router(build_unmounted_executive_audit_router(
        session_bindings={
            "sim-hernan": SessionBinding(
                session_id="executive-session-1",
                executive_user_id="user-hernan",
                webhook_secret=SECRET,
            )
        },
        db_factory=factory,
        enabled=enabled,
        persist_enabled=persist,
        max_request_bytes=max_bytes,
    ))
    return TestClient(app), db


def send(client, payload=None, *, signature=SECRET, **kwargs):
    headers = {"x-webhook-signature": signature}
    return client.post(ENDPOINT, headers=headers, json=event() if payload is None else payload, **kwargs)


def test_disabled_router_does_not_touch_database():
    calls = []
    client, _ = build_client(enabled=False, factory=lambda: calls.append(1))
    res = send(client)
    assert res.status_code == 404
    assert calls == []


def test_unknown_alias_and_bad_secret_rejected_before_db_access():
    calls = []
    client, _ = build_client(factory=lambda: calls.append(1))
    unknown = client.post(
        "/internal/executive-whatsapp/not-registered/events",
        headers={"x-webhook-signature": SECRET},
        json=event(),
    )
    wrong = send(client, signature="wrong")
    assert unknown.status_code == 404
    assert wrong.status_code == 401
    assert calls == []


def test_isolated_valid_message_is_dry_run_by_default():
    client, db = build_client()
    original = list(db.leads[0]["messages"])
    res = send(client)
    assert res.status_code == 200
    assert res.json()["mode"] == "dry_run"
    assert res.json()["results"] == {"planned": 1}
    assert "executive_whatsapp_messages" not in db.leads[0]
    assert db.leads[0]["messages"] == original


def test_explicit_test_only_persistence_and_idempotency():
    client, db = build_client(persist=True)
    res1, res2 = send(client), send(client)
    assert res1.status_code == 200
    assert res1.json()["results"] == {"saved": 1}
    assert res2.json()["results"] == {"duplicate_or_missing": 1}
    assert len(db.leads[0]["executive_whatsapp_messages"]) == 1
    assert db.leads[0]["messages"][0]["role"] == "user"


def test_content_type_rejected_before_database_access():
    calls = []
    client, _ = build_client(factory=lambda: calls.append(1))
    res = client.post(
        ENDPOINT,
        headers={"x-webhook-signature": SECRET, "content-type": "text/plain"},
        content="bad",
    )
    assert res.status_code == 415
    assert not calls


def test_malformed_and_duplicate_json_keys_rejected():
    client, _ = build_client()
    for raw in ('{bad', '{"event":"messages.upsert","event":"messages.upsert","data":{}}'):
        res = client.post(
            ENDPOINT,
            content=raw,
            headers={"x-webhook-signature": SECRET, "content-type": "application/json"},
        )
        assert res.status_code == 400
        assert res.json()["detail"] == "invalid_json"


def test_unrelated_events_are_not_ingested():
    client, db = build_client(persist=True)
    res = send(client, {"event": "message.sent", "data": {"id": "x"}})
    assert res.status_code == 422
    assert "executive_whatsapp_messages" not in db.leads[0]


def test_body_limit_and_message_count_limit():
    client, _ = build_client(max_bytes=1024)
    too_long = send(client, event(body="A" * 1500))
    assert too_long.status_code == 413
    large_batch = event()
    large_batch["data"]["messages"] = [large_batch["data"]["messages"]] * 51
    client2, _ = build_client()
    assert send(client2, large_batch).status_code == 413


def test_does_not_leak_phone_message_or_secret_on_db_failure():
    def broken_factory():
        raise RuntimeError("Private phone +56912345678: no access")
    client, _ = build_client(factory=broken_factory)
    res = send(client)
    assert res.status_code == 503
    assert "56912345678" not in res.text
    assert SECRET not in res.text
    assert "Private phone" not in res.text


def test_alias_from_request_body_cannot_change_executive_binding():
    client, db = build_client(persist=True)
    payload = event()
    payload["session_id"] = "office-bot"
    payload["executive_user_id"] = "other-user"
    res = send(client, payload)
    assert res.status_code == 200
    item = db.leads[0]["executive_whatsapp_messages"][0]
    assert item["executive_user_id"] == "user-hernan"
    assert item["session_id"] == "executive-session-1"


def test_no_router_is_mounted_unless_an_app_explicitly_includes_it():
    app = FastAPI()
    with TestClient(app) as client:
        res = client.post(ENDPOINT, json=event())
    assert res.status_code == 404


def test_invalid_binding_rejected_before_app_can_mount():
    with pytest.raises(ValueError):
        SessionBinding(session_id="x", executive_user_id="e", webhook_secret="")
