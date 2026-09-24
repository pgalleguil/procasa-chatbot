from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from analytics import owner_campaign_test_sender as cli
from campanas import owner_campaign_admin_panel as panel


class FakeRequest:
    def __init__(self, body=b"", *, headers=None, query_params=None):
        self._body = body
        self.headers = headers or {}
        self.query_params = query_params or {}

    async def body(self):
        return self._body


def _allow_admin(_request):
    async def allowed():
        return {"rol": "admin"}
    return allowed()


def _headers():
    return {
        "host": "procasa-chatbot-yr8d.onrender.com",
        "origin": "https://procasa-chatbot-yr8d.onrender.com",
        "content-type": "application/x-www-form-urlencoded",
    }


@pytest.mark.parametrize("status", [401, 403])
def test_admin_panel_blocks_unauthenticated_and_non_admin_before_work(status, monkeypatch):
    async def deny(_request):
        raise HTTPException(status_code=status)

    def should_not_run():
        raise AssertionError("admin guard must run before any work")

    monkeypatch.setattr(panel, "_mongo_db_and_ping", should_not_run)
    with pytest.raises(HTTPException) as error:
        asyncio.run(panel.handle_panel_get(FakeRequest(), deny))
    assert error.value.status_code == status


def test_get_is_preview_only_and_never_invokes_sender(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", "test-secret-present-only")
    monkeypatch.setenv("OWNER_CAMPAIGN_MASS_SEND_ENABLED", "false")
    monkeypatch.setattr(panel, "_mongo_db_and_ping", lambda: object())
    monkeypatch.setattr(panel, "_read_delivery_unknown", lambda: 6)
    monkeypatch.setattr(panel, "_secret_present", lambda: True)
    monkeypatch.setattr(panel.Config, "GMAIL_USER", "smtp-user")
    monkeypatch.setattr(panel.Config, "GMAIL_PASSWORD", "smtp-pass")
    monkeypatch.setattr(panel, "_render_panel", lambda **kwargs: SimpleNamespace(body=b"panel"))
    monkeypatch.setattr(cli, "run_test_batch", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("GET sent")), raising=True)

    from analytics import owner_campaign_report_normalization as normalization
    monkeypatch.setattr(normalization, "build_rendered_test_previews", lambda _db: ())
    response = asyncio.run(panel.handle_panel_get(FakeRequest(), _allow_admin))
    assert response.body == b"panel"


def test_get_rejects_query_overrides_and_secret_value_is_never_rendered(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    async def unused_guard(_request):
        return {"rol": "admin"}

    with pytest.raises(HTTPException) as error:
        asyncio.run(panel.handle_panel_get(FakeRequest(query_params={"recipient": "owner@example.com"}), unused_guard))
    assert error.value.status_code == 400

    secret = "never-display-this-secret-value"
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", secret)
    response = panel._render_panel(
        commit="abc123", secret_present=True, mongo_ready=True, smtp_ready=True,
        test_mode=True, mass_send=False, delivery_unknown=6, previews=None,
    )
    body = response.body.decode()
    assert "TOKEN_SECRET_PRESENT" in body and "true" in body
    assert secret not in body


@pytest.mark.parametrize("status", [400, 403, 415])
def test_post_rejects_missing_or_unsafe_confirmation_without_runner(status, monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", "test-secret-present-only")
    monkeypatch.setenv("OWNER_CAMPAIGN_MASS_SEND_ENABLED", "false")
    monkeypatch.setattr(panel.Config, "IS_PRODUCTION", False)
    request_headers = _headers()
    body = b"confirmation=RUN_OWNER_CAMPAIGN_TEST"
    if status == 400:
        body = b"confirmation=wrong"
    elif status == 403:
        request_headers["origin"] = "https://evil.example"
    elif status == 415:
        request_headers["content-type"] = "application/json"
    request = FakeRequest(body, headers=request_headers)
    with pytest.raises(HTTPException) as error:
        asyncio.run(panel.handle_panel_run(request, _allow_admin))
    assert error.value.status_code == status


def test_post_rejects_recipient_override_and_query_parameters(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setattr(panel.Config, "IS_PRODUCTION", False)
    request = FakeRequest(
        b"confirmation=RUN_OWNER_CAMPAIGN_TEST&recipient=owner%40example.com",
        headers=_headers(),
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(panel.handle_panel_run(request, _allow_admin))
    assert error.value.status_code == 400

    request = FakeRequest(
        b"confirmation=RUN_OWNER_CAMPAIGN_TEST", headers=_headers(),
        query_params={"recipient": "owner@example.com"},
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(panel.handle_panel_run(request, _allow_admin))
    assert error.value.status_code == 400


def test_post_missing_secret_fails_closed_before_database_or_smtp(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.delenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("OWNER_CAMPAIGN_MASS_SEND_ENABLED", "false")
    monkeypatch.setattr(panel.Config, "IS_PRODUCTION", False)
    monkeypatch.setattr(panel.Config, "GMAIL_USER", "smtp-user")
    monkeypatch.setattr(panel.Config, "GMAIL_PASSWORD", "smtp-pass")
    monkeypatch.setattr(panel, "_mongo_db_and_ping", lambda: (_ for _ in ()).throw(AssertionError("Mongo should not run")))
    with pytest.raises(HTTPException) as error:
        asyncio.run(panel.handle_panel_run(
            FakeRequest(b"confirmation=RUN_OWNER_CAMPAIGN_TEST", headers=_headers()), _allow_admin
        ))
    assert error.value.status_code == 409


def test_post_calls_only_fixed_all_case_runner_after_literal_confirmation(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", "never-return-this-secret")
    monkeypatch.setenv("OWNER_CAMPAIGN_MASS_SEND_ENABLED", "false")
    monkeypatch.setattr(panel.Config, "IS_PRODUCTION", False)
    monkeypatch.setattr(panel.Config, "GMAIL_USER", "smtp-user")
    monkeypatch.setattr(panel.Config, "GMAIL_PASSWORD", "smtp-pass")
    monkeypatch.setattr(panel, "_mongo_db_and_ping", lambda: object())
    called = {}
    from analytics import owner_campaign_report_normalization as normalization
    monkeypatch.setattr(normalization, "build_rendered_test_previews", lambda _db: ())
    monkeypatch.setattr(panel, "_all_preview_checks", lambda _previews: (True, {}))

    def fake_run(cases, **kwargs):
        called["cases"] = cases
        called.update(kwargs)
        return {
            "status": "test_messages_sent", "test_emails_sent": 5,
            "delivery_unknown_before": 6, "delivery_unknown_after": 6,
            "new_delivery_unknown": 0,
        }

    monkeypatch.setattr(cli, "run_test_batch", fake_run)
    monkeypatch.setattr(panel, "_render_panel", lambda **kwargs: SimpleNamespace(body=b"sent"))
    response = asyncio.run(panel.handle_panel_run(
        FakeRequest(b"confirmation=RUN_OWNER_CAMPAIGN_TEST", headers=_headers()), _allow_admin
    ))
    assert response.body == b"sent"
    assert called["cases"] == ("A", "B", "C", "D", "E")
    assert called["test_mode"] is True
    assert called["dry_run"] is False
    assert called["require_delivery_unknown_baseline"] is True
    assert "recipient" not in called


@pytest.mark.parametrize("count", [5, None])
def test_delivery_unknown_baseline_is_strict_when_requested(monkeypatch, count):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_MASS_SEND_ENABLED", "false")

    class EmptyDB:
        def __getitem__(self, _name):
            return SimpleNamespace(find=lambda *_args, **_kwargs: [])

    with pytest.raises(cli.TestCampaignCLIError, match="delivery_unknown_baseline_not_confirmed"):
        cli.run_test_batch(
            ("A", "B", "D"), test_mode=True, dry_run=True, db=EmptyDB(),
            health_reader=lambda: count, require_delivery_unknown_baseline=True,
        )
