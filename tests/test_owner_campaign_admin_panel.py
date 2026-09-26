from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from analytics import owner_campaign_test_sender as cli
from campanas import owner_campaign_admin_panel as panel
from campanas import owner_campaign_test_sender as campaign_sender


class FakeRequest:
    def __init__(self, body=b"", *, headers=None, query_params=None):
        self._body = body
        self.headers = headers or {}
        self.query_params = query_params or {}

    async def body(self):
        return self._body


class FakeLedger:
    def __init__(self, row=None):
        self.row = row

    def find_one(self, _query):
        return self.row

    def find(self, _query):
        return [self.row] if self.row else []


class FakeDB:
    def __init__(self, row=None):
        self.ledger = FakeLedger(row)

    def __getitem__(self, _name):
        return self.ledger


def _headers():
    return {
        "host": "procasa-chatbot-yr8d.onrender.com",
        "origin": "https://procasa-chatbot-yr8d.onrender.com",
        "content-type": "application/x-www-form-urlencoded",
    }


def test_get_preview_requires_no_crm_session_and_never_sends(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", "test-secret-present-only")
    monkeypatch.setenv("OWNER_CAMPAIGN_MASS_SEND_ENABLED", "false")
    monkeypatch.setattr(panel, "_mongo_db_and_ping", lambda: FakeDB())
    monkeypatch.setattr(panel, "_read_delivery_unknown", lambda: 6)
    monkeypatch.setattr(panel, "_secret_present", lambda: True)
    monkeypatch.setattr(panel.Config, "GMAIL_USER", "smtp-user")
    monkeypatch.setattr(panel.Config, "GMAIL_PASSWORD", "smtp-pass")
    monkeypatch.setattr(panel, "_render_panel", lambda **kwargs: SimpleNamespace(body=b"preview"))
    monkeypatch.setattr(
        cli, "run_test_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("GET sent")), raising=True,
    )

    from analytics import owner_campaign_report_normalization as normalization
    monkeypatch.setattr(normalization, "build_rendered_test_previews", lambda _db: ())
    # No CRM cookie, username, or guard callback is supplied.
    response = asyncio.run(panel.handle_panel_get(FakeRequest()))
    assert response.body == b"preview"


def test_get_rejects_overrides_and_page_never_discloses_secret(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    with pytest.raises(HTTPException) as error:
        asyncio.run(panel.handle_panel_get(FakeRequest(query_params={"recipient": "owner@example.com"})))
    assert error.value.status_code == 400

    secret = "never-display-this-secret-value"
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", secret)
    response = panel._render_panel(
        commit="abc123", secret_present=True, mongo_ready=True, smtp_ready=True,
        test_mode=True, mass_send=False, delivery_unknown=6, previews=None,
    )
    body = response.body.decode()
    assert "TOKEN_SECRET_PRESENT" in body and "true" in body
    assert "owner_campaign_email_AE_20260924_v1" in body
    assert secret not in body
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow, noarchive"


def test_panel_displays_frozen_fixture_provenance_and_safe_case_error():
    from analytics.owner_campaign_report_normalization import PreviewResult

    response = panel._render_panel(
        commit="abc123", secret_present=True, mongo_ready=True, smtp_ready=True,
        test_mode=True, mass_send=False, delivery_unknown=6,
        previews=(PreviewResult("E", None, "current_multi_property_owner_group_unavailable"),),
        qa_evidence={
            "source": "FROZEN_APPROVED_REPORT",
            "sha256": "a" * 64,
            "records": 406,
        },
    )
    body = response.body.decode()
    assert "QA_EVIDENCE_SOURCE" in body and "FROZEN_APPROVED_REPORT" in body
    assert "QA_EVIDENCE_SHA256" in body and "a" * 64 in body
    assert "QA_EVIDENCE_RECORDS" in body and "406" in body
    assert "E_ERROR_CODE=current_multi_property_owner_group_unavailable" in body
    assert "Envío deshabilitado" in body


def test_owner_email_prefers_current_property_owner_field_and_keeps_fallbacks():
    from campanas.owner_campaign_test_runtime import (
        _email_from_property, _email_source_from_property,
    )

    master = {
        "datos_propietario": {"email": " Mauro.Owner@Example.com "},
        "email_propietario": "legacy@example.com",
    }
    assert _email_from_property(master) == "mauro.owner@example.com"
    assert _email_source_from_property(master) == "datos_propietario.email"

    fallback = {"email_propietario": "legacy@example.com"}
    assert _email_from_property(fallback) == "legacy@example.com"
    assert _email_source_from_property(fallback) == "email_propietario"


def test_e_portfolio_scan_is_limited_to_approved_codes_to_avoid_cursor_timeout():
    from datetime import datetime, timezone

    from pymongo.errors import NetworkTimeout

    from campanas.owner_campaign_test_runtime import (
        LiveTestCaseBuildError, _build_portfolio,
    )

    segments = {
        "17081": "MIXED_EVIDENCE",
        "6331": "MIXED_EVIDENCE",
        "6348": "MIXED_EVIDENCE",
    }

    class Collection:
        query = None
        projection = None

        def find(self, query, projection=None):
            self.query = query
            self.projection = projection
            if not isinstance(query.get("codigo", {}).get("$in"), list):
                raise NetworkTimeout("unbounded active-property cursor timed out")
            if not projection or "analisis_comparables" in projection:
                raise NetworkTimeout("full property payload cursor timed out")
            return []

    collection = Collection()

    class Database:
        def __getitem__(self, name):
            assert name == "universo_cartera_prop360"
            return collection

    with pytest.raises(LiveTestCaseBuildError) as error:
        _build_portfolio(
            Database(), datetime.now(timezone.utc), qa_segments=segments,
        )

    assert error.value.error_code == "current_multi_property_owner_group_unavailable"
    assert collection.query["estado.estado_prop360"] == "Activa"
    assert collection.query["disponible_prop360"] is True
    assert collection.query["codigo"]["$in"] == [
        "17081", 17081, "6331", 6331, "6348", 6348,
    ]
    assert collection.projection["datos_propietario.email"] == 1
    assert collection.projection["estado.ejecutivo"] == 1
    assert "analisis_comparables" not in collection.projection


def test_e_preview_contract_requires_primary_owner_email_source():
    from analytics.owner_campaign_report_normalization import PreviewResult

    child_cases = tuple(
        SimpleNamespace(property_code=code, operation="VENTA", evidence_segment="MIXED_EVIDENCE", cta_type="ADVISOR_REVIEW")
        for code in ("17081", "6331", "6348")
    )
    case = SimpleNamespace(
        case_id="E", portfolio_cases=child_cases,
        render_context={"owner_email_source": "datos_propietario.email"},
    )
    prepared = SimpleNamespace(
        case=case,
        html=("/campana/test-accion?token=x /campana/test-accion?token=y "
              "/campana/test-accion?token=z /campana/informe?token=x "
              "/campana/informe?token=y /campana/informe?token=z"),
        text="rendered",
    )
    checks, _detail = panel._case_checks(PreviewResult("E", prepared, None))
    assert all(checks.values())

    case.render_context["owner_email_source"] = "email_propietario"
    checks, _detail = panel._case_checks(PreviewResult("E", prepared, None))
    assert not checks["case_contract"]


def test_preview_builder_exposes_only_symbolic_safe_error_codes(monkeypatch):
    from analytics import owner_campaign_report_normalization as normalization
    from campanas.owner_campaign_test_runtime import LiveTestCaseBuildError
    from campanas.owner_campaign_test_sender import TestSenderError

    def raise_safe_error(_db, *, case_ids):
        raise LiveTestCaseBuildError("current_multi_property_owner_group_unavailable")

    monkeypatch.setattr(normalization, "build_owner_campaign_test_cases_live", raise_safe_error)
    results = normalization.build_rendered_test_previews(FakeDB())
    assert len(results) == 5
    assert {result.error_code for result in results} == {"current_multi_property_owner_group_unavailable"}

    def raise_sender_error(_db, *, case_ids):
        raise TestSenderError("test_case_e_property_links_incomplete")

    monkeypatch.setattr(normalization, "build_owner_campaign_test_cases_live", raise_sender_error)
    results = normalization.build_rendered_test_previews(FakeDB())
    assert {result.error_code for result in results} == {"test_case_e_property_links_incomplete"}

    def raise_untrusted_error(_db, *, case_ids):
        raise ValueError("owner@example.com secret material")

    monkeypatch.setattr(normalization, "build_owner_campaign_test_cases_live", raise_untrusted_error)
    results = normalization.build_rendered_test_previews(FakeDB())
    assert {result.error_code for result in results} == {"preview_build_failed"}


def test_e_preview_failure_logs_redacted_traceback_and_selected_portfolio(monkeypatch, caplog):
    import logging

    from analytics import owner_campaign_report_normalization as normalization

    children = tuple(SimpleNamespace(property_code=code) for code in ("17081", "6331", "6348"))
    case = SimpleNamespace(
        case_id="E", portfolio_cases=children,
        render_context={"owner_email_source": "datos_propietario.email"},
    )
    monkeypatch.setattr(normalization, "ALL_CASES", ("E",))
    monkeypatch.setattr(normalization, "build_owner_campaign_test_cases_live", lambda _db, *, case_ids: [case])

    def fail_during_render(_cases):
        raise RuntimeError("failed owner@example.com token=eyJabcdefghijklmnop.abc.def")

    monkeypatch.setattr(normalization, "prepare_test_messages", fail_during_render)
    with caplog.at_level(logging.ERROR, logger=normalization.__name__):
        results = normalization.build_rendered_test_previews(FakeDB())

    assert results[0].error_code == "preview_build_failed"
    assert "OWNER_CAMPAIGN_E_PREVIEW_EXCEPTION phase=prepare_render_and_links" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert "owner_email_source=datos_propietario.email" in caplog.text
    assert "property_count=3" in caplog.text
    assert "property_codes=17081,6331,6348" in caplog.text
    assert "owner@example.com" not in caplog.text
    assert "eyJabcdefghijklmnop" not in caplog.text
    assert "in build_rendered_test_previews" in caplog.text
    assert "in fail_during_render" in caplog.text


@pytest.mark.parametrize("status", [400, 403, 415])
def test_post_rejects_missing_or_unsafe_confirmation_without_runner(status, monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", "test-secret-present-only")
    monkeypatch.setenv("OWNER_CAMPAIGN_MASS_SEND_ENABLED", "false")
    monkeypatch.setattr(panel.Config, "IS_PRODUCTION", False)
    headers = _headers()
    body = b"confirmation=RUN_OWNER_CAMPAIGN_TEST"
    if status == 400:
        body = b"confirmation=wrong"
    elif status == 403:
        headers["origin"] = "https://evil.example"
    elif status == 415:
        headers["content-type"] = "application/json"
    with pytest.raises(HTTPException) as error:
        asyncio.run(panel.handle_panel_run(FakeRequest(body, headers=headers)))
    assert error.value.status_code == status


def test_post_rejects_recipient_override_and_query_parameters(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setattr(panel.Config, "IS_PRODUCTION", False)
    request = FakeRequest(
        b"confirmation=RUN_OWNER_CAMPAIGN_TEST&recipient=owner%40example.com",
        headers=_headers(),
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(panel.handle_panel_run(request))
    assert error.value.status_code == 400

    request = FakeRequest(
        b"confirmation=RUN_OWNER_CAMPAIGN_TEST", headers=_headers(),
        query_params={"recipient": "owner@example.com"},
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(panel.handle_panel_run(request))
    assert error.value.status_code == 400


def test_post_missing_secret_fails_closed_before_database_or_smtp(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.delenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("OWNER_CAMPAIGN_MASS_SEND_ENABLED", "false")
    monkeypatch.setattr(panel.Config, "IS_PRODUCTION", False)
    monkeypatch.setattr(panel.Config, "GMAIL_USER", "smtp-user")
    monkeypatch.setattr(panel.Config, "GMAIL_PASSWORD", "smtp-pass")
    monkeypatch.setattr(
        panel, "_mongo_db_and_ping",
        lambda: (_ for _ in ()).throw(AssertionError("Mongo should not run")),
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(panel.handle_panel_run(
            FakeRequest(b"confirmation=RUN_OWNER_CAMPAIGN_TEST", headers=_headers())
        ))
    assert error.value.status_code == 409


def test_post_sends_only_fixed_all_case_batch_after_preview_and_confirmation(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", "never-return-this-secret")
    monkeypatch.setenv("OWNER_CAMPAIGN_MASS_SEND_ENABLED", "false")
    monkeypatch.setattr(panel.Config, "IS_PRODUCTION", False)
    monkeypatch.setattr(panel.Config, "GMAIL_USER", "smtp-user")
    monkeypatch.setattr(panel.Config, "GMAIL_PASSWORD", "smtp-pass")
    monkeypatch.setattr(panel, "_mongo_db_and_ping", lambda: FakeDB())
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
        FakeRequest(b"confirmation=RUN_OWNER_CAMPAIGN_TEST", headers=_headers())
    ))
    assert response.body == b"sent"
    assert called["cases"] == ("A", "B", "C", "D", "E")
    assert called["test_mode"] is True
    assert called["dry_run"] is False
    assert called["require_delivery_unknown_baseline"] is True
    assert "recipient" not in called


def test_post_blocks_previously_registered_batch_before_build_or_smtp(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", "test-secret-present-only")
    monkeypatch.setenv("OWNER_CAMPAIGN_MASS_SEND_ENABLED", "false")
    monkeypatch.setattr(panel.Config, "IS_PRODUCTION", False)
    monkeypatch.setattr(panel.Config, "GMAIL_USER", "smtp-user")
    monkeypatch.setattr(panel.Config, "GMAIL_PASSWORD", "smtp-pass")
    monkeypatch.setattr(panel, "_mongo_db_and_ping", lambda: FakeDB({"delivery_status": "test_sent"}))
    from analytics import owner_campaign_report_normalization as normalization
    monkeypatch.setattr(
        normalization, "build_rendered_test_previews",
        lambda _db: (_ for _ in ()).throw(AssertionError("registered run must not render/send")),
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(panel.handle_panel_run(
            FakeRequest(b"confirmation=RUN_OWNER_CAMPAIGN_TEST", headers=_headers())
        ))
    assert error.value.status_code == 409


def test_fixed_test_run_id_is_persisted_in_existing_campaign_ledger():
    class RecordingLedger:
        def __init__(self):
            self.payloads = []

        def find_one(self, _query):
            return None

        def update_one(self, query, update, *, upsert):
            assert upsert is True
            self.payloads.append({"_id": query["_id"], **update["$setOnInsert"]})
            return SimpleNamespace(upserted_id=query["_id"])

    ledger = RecordingLedger()
    database = {"ajuste_precio": ledger}
    case = SimpleNamespace(
        case_id="A",
        property_code="5641", intended_owner_email="owner@example.com", operation="VENTA",
        current_price=3000, raw_recommended_price=2857, display_recommended_price=2857,
        adjustment_pct=-4.77, evidence_segment="STRONG_PRICE_ADJUSTMENT",
        cta_type="PRICE_AUTHORIZATION", evidence_version="v1", document_type="appraisal",
        executive="Ejecutivo PROCASA",
    )
    campaign_sender._write_test_ledger_entries(
        database, [SimpleNamespace(case=case, property_cases=None)]
    )
    assert len(ledger.payloads) == 1
    assert ledger.payloads[0]["test_run_id"] == "owner_campaign_email_AE_20260924_v1"
    assert ledger.payloads[0]["campaign_id"] == "owner_price_campaign_test_20260923"
    assert ledger.payloads[0]["test_mode"] is True
    assert ledger.payloads[0]["actual_recipient_email"] == campaign_sender.TEST_RECIPIENT


def test_registered_test_run_is_rejected_before_any_delivery_check(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_MASS_SEND_ENABLED", "false")
    database = FakeDB({
        "campaign_id": "owner_price_campaign_test_20260923",
        "test_run_id": "owner_campaign_email_AE_20260924_v1",
        "test_mode": True,
    })
    with pytest.raises(cli.TestCampaignCLIError, match="test_run_already_registered"):
        cli.run_test_batch(
            cli.ALL_CASES, test_mode=True, dry_run=True, db=database,
            health_reader=lambda: (_ for _ in ()).throw(AssertionError("must block first")),
        )


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
