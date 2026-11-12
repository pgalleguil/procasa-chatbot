"""Regression: invalid WaSender callbacks return HTTP 401, never a 307 login redirect."""
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from config import Config
from webhook import app


def test_webhook_wrong_signature_returns_direct_401_without_login_redirect(monkeypatch):
    monkeypatch.setattr(Config, "WASENDER_WEBHOOK_SECRET", "test-webhook-secret")
    client = TestClient(app)
    r = client.post(
        "/webhook", json={"event": "webhook.test"},
        headers={"X-Webhook-Signature": "invalid"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "location" not in r.headers
    assert r.json() == {
        "status": "unauthorized", "reason": "webhook_authentication_failed",
    }


def test_webhook_valid_shared_secret_still_accepts_provider_test(monkeypatch):
    secret = "test-webhook-secret"
    monkeypatch.setattr(Config, "WASENDER_WEBHOOK_SECRET", secret)
    r = TestClient(app).post(
        "/webhook", json={"event": "webhook.test"},
        headers={"X-Webhook-Signature": secret},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_webhook_valid_hmac_still_accepts_provider_test(monkeypatch):
    secret = "test-webhook-secret"
    monkeypatch.setattr(Config, "WASENDER_WEBHOOK_SECRET", secret)
    body = json.dumps({"event": "webhook.test"}, separators=(",", ":")).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    r = TestClient(app).post(
        "/webhook", content=body,
        headers={"X-Webhook-Signature": digest,
                 "Content-Type": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
