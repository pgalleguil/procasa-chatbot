"""Isolated service setup: synthetic events, no Render or real network."""
import json

import pytest
from fastapi.testclient import TestClient

from executive_whatsapp_standalone import create_app
from tests.test_executive_whatsapp_audit import FakeDB, event


ENDPOINT = "/internal/executive-whatsapp/canary01/events"
SECRET = "synthetic-webhook-secret-00000000000000001"


def config(*, enabled=True, persist=False, reviewed=True, sessions=1):
    registry = {
        f"canary0{i}": {
            "session_id": f"fake-session-{i}",
            "executive_user_id": "user-hernan",
            "webhook_secret": SECRET,
        }
        for i in range(1, sessions + 1)
    }
    result = {
        "PROCASA_EXEC_AUDIT_ENABLED": "true" if enabled else "false",
        "PROCASA_EXEC_AUDIT_PERSIST": "true" if persist else "false",
        "PROCASA_EXEC_AUDIT_PILOT_APPROVED": "AUTHORIZED_ONE_PHONE_CANARY",
        "PROCASA_EXEC_AUDIT_PRIVACY_REVIEW": "approved" if reviewed else "pending",
        "PROCASA_EXEC_AUDIT_BINDINGS_JSON": json.dumps(registry),
    }
    return result


def test_default_import_is_not_a_live_webhook_and_needs_no_db():
    app = create_app(env={})
    client = TestClient(app)
    assert client.get("/health").json()["capture_enabled"] is False
    assert client.post(ENDPOINT, json=event()).status_code == 404


def test_dryrun_standalone_accepts_only_authenticated_one_phone():
    db = FakeDB()
    app = create_app(env=config(), db_factory=lambda: db)
    client = TestClient(app)
    reply = client.post(
        ENDPOINT,
        json=event(),
        headers={"X-Webhook-Signature": SECRET},
    )
    assert reply.status_code == 200
    assert reply.json()["mode"] == "dry_run"
    assert "executive_whatsapp_messages" not in db.leads[0]
    assert client.post(ENDPOINT, json=event()).status_code == 401
    assert client.post(
        "/internal/executive-whatsapp/canary02/events",
        json=event(), headers={"X-Webhook-Signature": SECRET},
    ).status_code == 404


def test_explicit_canary_persistence_writes_without_chatbot_changes():
    db = FakeDB()
    client = TestClient(create_app(
        env=config(persist=True), db_factory=lambda: db
    ))
    response = client.post(
        ENDPOINT, json=event(),
        headers={"X-Webhook-Signature": SECRET},
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "persist"
    assert len(db.leads[0]["executive_whatsapp_messages"]) == 1
    assert db.leads[0]["messages"][0]["role"] == "user"


def test_rejects_privacy_gate_missing():
    with pytest.raises(ValueError, match="privacy_approval_required"):
        create_app(env=config(persist=True, reviewed=False), db_factory=FakeDB)


def test_rejects_more_than_one_session_for_canary():
    with pytest.raises(ValueError, match="pilot_requires_exactly_one"):
        create_app(env=config(sessions=2), db_factory=FakeDB)


def test_requires_explicit_canary_approval():
    values = config()
    values["PROCASA_EXEC_AUDIT_PILOT_APPROVED"] = ""
    with pytest.raises(ValueError, match="one_phone_canary_approval_required"):
        create_app(env=values, db_factory=FakeDB)


def test_rejects_persist_without_receiver():
    with pytest.raises(ValueError, match="persistence_cannot_be_enabled"):
        create_app(env=config(enabled=False, persist=True), db_factory=FakeDB)


def test_unconfigured_db_does_not_allow_capture():
    with pytest.raises(ValueError, match="independent_mongodb_access_required"):
        create_app(env=config(), db_factory=None)


def test_standalone_must_not_import_office_webhook():
    from pathlib import Path
    source = Path("executive_whatsapp_standalone.py").read_text(encoding="utf-8")
    assert "from webhook import" not in source
    assert "from chatbot.whatsapp_client import" not in source
    assert "from chatbot.processing_service import" not in source
    assert "send_text" not in source
