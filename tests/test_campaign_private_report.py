from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError
from campanas import private_report


SECRET = "campaign-test-signing-secret-for-unit-tests"
CODE = "16521"


def _token(*, document_type="INDIVIDUAL_APPRAISAL", exp=2_000_000_000, **extra):
    claims = {
        "campaign_id": private_report.TEST_CAMPAIGN_ID,
        "property_code": CODE,
        "action": private_report.REPORT_ACTION,
        "recipient": private_report.TEST_RECIPIENT,
        "exp": exp,
        "test_mode": True,
        "document_type": document_type,
        **extra,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(
        hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"t1.{encoded}.{signature}"


class _Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Files:
    def __init__(self, children, *, media_error=None):
        self.children = children
        self.media_error = media_error
        self.list_calls = []
        self.media_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        parent_id = kwargs["q"].split("'", 2)[1]
        return _Request({"files": self.children.get(parent_id, [])})

    def get_media(self, **kwargs):
        self.media_calls.append(kwargs)
        if self.media_error:
            return _Request(self.media_error)
        return SimpleNamespace(file_id=kwargs["fileId"])


class _Permissions:
    def __init__(self, by_file=None):
        self.by_file = by_file or {}
        self.list_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _Request({"permissions": self.by_file.get(kwargs["fileId"], [])})


class _Service:
    def __init__(self, children, permissions=None, *, media_error=None):
        self._files = _Files(children, media_error=media_error)
        self._permissions = _Permissions(permissions)

    def files(self):
        return self._files

    def permissions(self):
        return self._permissions


class _Downloader:
    def __init__(self, stream, request):
        self.stream = stream
        self.request = request

    def next_chunk(self):
        if isinstance(self.request, _Request):
            self.request.execute()
        self.stream.write(b"%PDF-1.7\nprivate test bytes\n")
        return None, True


def _patch_runtime(monkeypatch, service):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", SECRET)
    monkeypatch.setattr(private_report, "GDriveSync", lambda: SimpleNamespace(service=service))
    monkeypatch.setattr(private_report, "MediaIoBaseDownload", _Downloader)


@pytest.mark.asyncio
async def test_individual_appraisal_is_read_from_fixed_folder_without_database(monkeypatch):
    service = _Service({
        private_report.APPRAISALS_FOLDER_ID: [
            {"id": "server-only-drive-id", "name": f"{CODE}.pdf", "mimeType": private_report.PDF_MIME_TYPE}
        ]
    })
    _patch_runtime(monkeypatch, service)

    response = await private_report.handle_campaign_report(_token())

    assert response.status_code == 200
    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF-")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert service._files.media_calls == [{"fileId": "server-only-drive-id", "supportsAllDrives": True}]
    assert service._files.list_calls[0]["q"].startswith(f"'{private_report.APPRAISALS_FOLDER_ID}' in parents")
    assert "server-only-drive-id" not in str(response.headers)


@pytest.mark.asyncio
async def test_communal_report_resolves_operation_commune_and_type_hierarchy(monkeypatch):
    claims = {
        "operation": "VENTA",
        "commune": "Talca",
        "property_type": "Casa",
    }
    service = _Service({
        private_report.COMMUNAL_FOLDER_ID: [
            {"id": "venta-folder", "name": "Venta", "mimeType": private_report.FOLDER_MIME_TYPE}
        ],
        "venta-folder": [
            {"id": "talca-folder", "name": "Tálca", "mimeType": private_report.FOLDER_MIME_TYPE}
        ],
        "talca-folder": [
            {"id": "communal-pdf", "name": "casa.pdf", "mimeType": private_report.PDF_MIME_TYPE}
        ],
    })
    _patch_runtime(monkeypatch, service)

    response = await private_report.handle_campaign_report(
        _token(document_type="COMMUNAL_MARKET_REPORT", **claims)
    )

    assert response.status_code == 200
    assert response.body.startswith(b"%PDF-")
    assert service._files.media_calls[0]["fileId"] == "communal-pdf"
    assert [call["q"].split("'", 2)[1] for call in service._files.list_calls] == [
        private_report.COMMUNAL_FOLDER_ID, "venta-folder", "talca-folder"
    ]


@pytest.mark.asyncio
async def test_invalid_expired_or_unqualified_community_token_never_calls_drive(monkeypatch):
    calls = []
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", SECRET)
    monkeypatch.setattr(private_report, "GDriveSync", lambda: calls.append("drive"))

    invalid = await private_report.handle_campaign_report("not-a-token")
    expired = await private_report.handle_campaign_report(
        _token(exp=1_000_000_000)
    )
    missing_communal_claims = await private_report.handle_campaign_report(
        _token(document_type="COMMUNAL_MARKET_REPORT")
    )

    assert [invalid.status_code, expired.status_code, missing_communal_claims.status_code] == [404, 404, 404]
    assert calls == []


@pytest.mark.asyncio
async def test_test_mode_disabled_fails_closed_before_drive(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "false")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", SECRET)
    calls = []
    monkeypatch.setattr(private_report, "GDriveSync", lambda: calls.append("drive"))

    response = await private_report.handle_campaign_report(_token())

    assert response.status_code == 404
    assert calls == []


@pytest.mark.asyncio
async def test_public_drive_permission_is_rejected_without_download(monkeypatch):
    service = _Service(
        {private_report.APPRAISALS_FOLDER_ID: [
            {"id": "public-pdf", "name": f"{CODE}.pdf", "mimeType": private_report.PDF_MIME_TYPE}
        ]},
        permissions={"public-pdf": [{"type": "anyone", "role": "reader"}]},
    )
    _patch_runtime(monkeypatch, service)

    response = await private_report.handle_campaign_report(_token())

    assert response.status_code == 403
    assert service._files.media_calls == []


@pytest.mark.asyncio
async def test_drive_http_error_status_is_preserved_without_error_body(monkeypatch):
    error = HttpError(
        SimpleNamespace(status=403, reason="Forbidden"),
        b'{"error":{"message":"private id"}}',
    )
    service = _Service(
        {private_report.APPRAISALS_FOLDER_ID: [
            {"id": "appraisal-pdf", "name": f"{CODE}.pdf", "mimeType": private_report.PDF_MIME_TYPE}
        ]},
        media_error=error,
    )
    _patch_runtime(monkeypatch, service)

    response = await private_report.handle_campaign_report(_token())

    assert response.status_code == 403
    assert b"private id" not in response.body


def test_report_route_only_accepts_signed_token_and_diagnostic_endpoint_is_removed():
    source = (Path(__file__).parents[1] / "webhook.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    route = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "campana_informe"
    )
    assert [argument.arg for argument in route.args.args] == ["request", "token"]
    assert "_require_captacion_report_admin" not in ast.unparse(route)
    assert "query_params" in ast.unparse(route)
    assert "internal_campaign_drive_check" not in source
    report_source = (Path(__file__).parents[1] / "campanas" / "private_report.py").read_text(encoding="utf-8")
    assert "MongoClient" not in report_source
    assert ".create(" not in report_source
    assert ".delete(" not in report_source
    assert ".update(" not in report_source


def test_startup_no_longer_schedules_drive_permission_repair():
    source = (Path(__file__).parents[1] / "webhook.py").read_text(encoding="utf-8-sig")
    assert "_fix_existing_drive_permissions" not in source
    assert "STARTUP_DRIVE_PERMISSION_MUTATION=0" in source
