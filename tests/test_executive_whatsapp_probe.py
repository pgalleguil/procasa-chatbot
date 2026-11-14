"""WaSender passive probe never connects MongoDB or stores message content."""
import json

from fastapi.testclient import TestClient

from executive_whatsapp_standalone import create_app
from tests.test_executive_whatsapp_audit import event

SECRET = "synthetic-very-long-secret-never-real"
URL = "/internal/executive-whatsapp/canary01/events"


def config():
    return {
        "PROCASA_EXEC_AUDIT_ENABLED": "true",
        "PROCASA_EXEC_AUDIT_PROBE_ONLY": "true",
        "PROCASA_EXEC_AUDIT_PERSIST": "false",
        "PROCASA_EXEC_AUDIT_PILOT_APPROVED": "AUTHORIZED_ONE_PHONE_CANARY",
        "PROCASA_EXEC_AUDIT_BINDINGS_JSON": json.dumps({
            "canary01": {
                "session_id": "fake-customer-test-session",
                "executive_user_id": "user-hernan",
                "webhook_secret": SECRET,
            },
        }),
    }


def test_probe_cannot_call_db_and_only_returns_counts():
    calls = []

    def forbidden_db():
        calls.append("called")
        raise AssertionError("probe_connected_to_mongodb")

    client = TestClient(create_app(env=config(), db_factory=forbidden_db))
    inbound = client.post(URL, json=event(), headers={"X-Webhook-Signature": SECRET})
    outgoing = client.post(
        URL,
        json=event(outgoing=True, ident="manual"),
        headers={"X-Webhook-Signature": SECRET},
    )
    assert inbound.status_code == 200
    assert outgoing.status_code == 200
    assert inbound.json()["incoming_client_messages"] == 1
    assert outgoing.json()["outgoing_executive_messages"] == 1
    for response in (inbound, outgoing):
        blob = json.dumps(response.json())
        assert "+569" not in blob
        assert "Todavía busco" not in blob
        assert "fake-customer-test-session" not in blob
        assert response.json()["payload_contents_stored"] is False
        assert response.json()["mongo_connections"] == 0
        assert response.json()["outbound_messages_sent"] == 0
    assert calls == []


def test_probe_rejects_bad_signature_before_parsing_or_db():
    client = TestClient(create_app(env=config()))
    assert client.post(URL, json=event()).status_code == 401
    assert client.post(URL, json=event(), headers={"X-Webhook-Signature": "wrong"}).status_code == 401


def test_probe_inactive_by_default_without_session_config():
    app = create_app(env={})
    assert TestClient(app).post(URL, json=event()).status_code == 404


def test_probe_cannot_enable_any_mongodb_persistence():
    values = config()
    values["PROCASA_EXEC_AUDIT_PERSIST"] = "true"
    try:
        create_app(env=values)
        assert False, "should reject persistence"
    except ValueError as error:
        assert "probe_mode_must_not_persist" in str(error)


def test_probe_rejects_group_and_unknown_event():
    client = TestClient(create_app(env=config()))
    res = client.post(URL, json=event(jid="12345@g.us"), headers={"X-Webhook-Signature": SECRET})
    assert res.status_code == 200
    assert res.json()["total_valid"] == 0
    assert res.json()["unmatched_or_unsupported"] == 1
    bad = client.post(URL, json={"event": "message.sent", "data": {"messages": {}}}, headers={"X-Webhook-Signature": SECRET})
    assert bad.status_code == 422
