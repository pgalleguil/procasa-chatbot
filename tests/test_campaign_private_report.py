from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from googleapiclient.errors import HttpError
from campanas import private_report


SECRET = "campaign-test-signing-secret-for-unit-tests"
CODE = "16521"
PROPERTY_IDENTITY = {
    "property_code": CODE,
    "commune": "Ñuñoa",
    "property_type": "Departamento",
    "operation": "VENTA",
}


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
        items = list(self.children.get(parent_id, []))
        mime_type = "application/vnd.google-apps.folder" if "mimeType = 'application/vnd.google-apps.folder'" in kwargs["q"] else (
            "application/pdf" if "mimeType = 'application/pdf'" in kwargs["q"] else None
        )
        if mime_type:
            items = [item for item in items if item.get("mimeType") == mime_type]
        if " and name = '" in kwargs["q"]:
            name = kwargs["q"].rsplit(" and name = '", 1)[1].rsplit("'", 1)[0]
            items = [item for item in items if item.get("name") == name]
        return _Request({"files": items})

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


def _patch_runtime(monkeypatch, service, identity=PROPERTY_IDENTITY):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", SECRET)
    monkeypatch.setattr(private_report, "GDriveSync", lambda: SimpleNamespace(service=service))
    monkeypatch.setattr(private_report, "MediaIoBaseDownload", _Downloader)
    monkeypatch.setattr(private_report, "_load_property_identity", lambda code: identity)


def _pdf(file_id, filename):
    return {"id": file_id, "name": filename, "mimeType": private_report.PDF_MIME_TYPE}


@pytest.mark.asyncio
async def test_individual_appraisal_is_resolved_by_property_code_and_never_by_token_drive_id(monkeypatch):
    service = _Service({
        private_report.APPRAISALS_FOLDER_ID: [_pdf("server-only-drive-id", f"{CODE}.pdf")]
    })
    _patch_runtime(monkeypatch, service)

    response = await private_report.handle_campaign_report(_token(drive_file_id="attacker-selected-id"))

    assert response.status_code == 200
    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF-")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-campaign-report-resolution"] == "drive"
    assert service._files.media_calls == [{"fileId": "server-only-drive-id", "supportsAllDrives": True}]
    assert service._files.list_calls[0]["q"].startswith(f"'{private_report.APPRAISALS_FOLDER_ID}' in parents")
    assert "server-only-drive-id" not in str(response.headers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "commune,property_type,filename",
    [
        ("Ñuñoa", "Departamento", "Ñuñoa - Departamento.pdf"),
        ("Maipú", "Casa", "Maipú - Casa.pdf"),
        ("Peñaflor", "Casa", "Peñaflor - Casa.pdf"),
        ("Estación Central", "Bodega", "Estación Central - Bodega.pdf"),
        ("Chillán", "Casa", "Chillán - Casa.pdf"),
        ("Isla de Maipo", "Casa", "Isla de Maipo - Casa.pdf"),
    ],
)
async def test_communal_resolution_uses_canonical_property_data_and_unicode_names(
    monkeypatch, commune, property_type, filename
):
    identity = {**PROPERTY_IDENTITY, "commune": commune, "property_type": property_type, "operation": "VENTA"}
    service = _Service({private_report.COMMUNAL_FOLDER_ID: [_pdf("canonical-report", filename)]})
    _patch_runtime(monkeypatch, service, identity)
    forged_metadata = {
        "commune": "Wrong Commune",
        "property_type": "Wrong Type",
        "operation": "ARRIENDO",
        "filename": "other.pdf",
        "drive_file_id": "attacker-selected-id",
    }

    response = await private_report.handle_campaign_report(
        _token(document_type="COMMUNAL_MARKET_REPORT", **forged_metadata)
    )

    assert response.status_code == 200
    assert response.body.startswith(b"%PDF-")
    assert service._files.media_calls[0]["fileId"] == "canonical-report"
    assert response.headers["x-campaign-report-resolution"] == "drive"
    content_disposition = response.headers["content-disposition"]
    encoded_name = content_disposition.split("filename*=UTF-8''", 1)[1]
    assert unquote(encoded_name) == f"Informe Comercial PROCASA - {commune} - {property_type}.pdf"


@pytest.mark.asyncio
async def test_operation_specific_drive_section_takes_precedence_over_shared_root_copy(monkeypatch):
    identity = {**PROPERTY_IDENTITY, "commune": "Talca", "property_type": "Casa", "operation": "ARRIENDO"}
    service = _Service({
        private_report.COMMUNAL_FOLDER_ID: [
            {"id": "arriendo-folder", "name": "Arriendo", "mimeType": private_report.FOLDER_MIME_TYPE},
            _pdf("shared-root-copy", "Talca - Casa.pdf"),
        ],
        "arriendo-folder": [
            {"id": "talca-folder", "name": "Talca", "mimeType": private_report.FOLDER_MIME_TYPE}
        ],
        "talca-folder": [_pdf("arriendo-specific-report", "Casa.pdf")],
    })
    _patch_runtime(monkeypatch, service, identity)

    response = await private_report.handle_campaign_report(
        _token(document_type="COMMUNAL_MARKET_REPORT", operation="VENTA")
    )

    assert response.status_code == 200
    assert service._files.media_calls[0]["fileId"] == "arriendo-specific-report"


@pytest.mark.asyncio
async def test_missing_drive_document_returns_procasa_pdf_fallback_http_200(monkeypatch):
    service = _Service({private_report.COMMUNAL_FOLDER_ID: []})
    _patch_runtime(monkeypatch, service)

    response = await private_report.handle_campaign_report(
        _token(document_type="COMMUNAL_MARKET_REPORT")
    )

    assert response.status_code == 200
    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF-")
    assert response.headers["x-campaign-report-resolution"] == "procasa_fallback"
    assert response.headers["cache-control"] == "private, no-store"
    assert service._files.media_calls == []
    assert response.body.rstrip().endswith(b"%%EOF")


@pytest.mark.asyncio
async def test_missing_appraisal_also_returns_pdf_fallback_not_raw_404(monkeypatch):
    service = _Service({private_report.APPRAISALS_FOLDER_ID: []})
    _patch_runtime(monkeypatch, service)

    response = await private_report.handle_campaign_report(_token())

    assert response.status_code == 200
    assert response.media_type == "application/pdf"
    assert response.headers["x-campaign-report-resolution"] == "procasa_fallback"
    assert response.body.startswith(b"%PDF-")


@pytest.mark.asyncio
async def test_communal_signed_token_does_not_require_location_or_operation_claims(monkeypatch):
    service = _Service({private_report.COMMUNAL_FOLDER_ID: [_pdf("canonical-report", "Ñuñoa - Departamento.pdf")]})
    _patch_runtime(monkeypatch, service)

    response = await private_report.handle_campaign_report(_token(document_type="COMMUNAL_MARKET_REPORT"))

    assert response.status_code == 200
    assert service._files.media_calls[0]["fileId"] == "canonical-report"


@pytest.mark.asyncio
async def test_invalid_or_expired_token_never_calls_database_or_drive(monkeypatch):
    calls = []
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", SECRET)
    monkeypatch.setattr(private_report, "_load_property_identity", lambda code: calls.append("mongo"))
    monkeypatch.setattr(private_report, "GDriveSync", lambda: calls.append("drive"))

    invalid = await private_report.handle_campaign_report("not-a-token")
    expired = await private_report.handle_campaign_report(_token(exp=1_000_000_000))

    assert [invalid.status_code, expired.status_code] == [404, 404]
    assert calls == []


@pytest.mark.asyncio
async def test_test_mode_disabled_fails_closed_before_database_or_drive(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "false")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", SECRET)
    calls = []
    monkeypatch.setattr(private_report, "_load_property_identity", lambda code: calls.append("mongo"))
    monkeypatch.setattr(private_report, "GDriveSync", lambda: calls.append("drive"))

    response = await private_report.handle_campaign_report(_token())

    assert response.status_code == 404
    assert calls == []


@pytest.mark.asyncio
async def test_public_drive_permission_is_rejected_without_download(monkeypatch):
    service = _Service(
        {private_report.APPRAISALS_FOLDER_ID: [_pdf("public-pdf", f"{CODE}.pdf")]},
        permissions={"public-pdf": [{"type": "anyone", "role": "reader"}]},
    )
    _patch_runtime(monkeypatch, service)

    response = await private_report.handle_campaign_report(_token())

    assert response.status_code == 403
    assert service._files.media_calls == []


@pytest.mark.asyncio
async def test_drive_http_error_status_is_preserved_without_error_body(monkeypatch):
    error = HttpError(SimpleNamespace(status=403, reason="Forbidden"), b'{"error":{"message":"private id"}}')
    service = _Service(
        {private_report.APPRAISALS_FOLDER_ID: [_pdf("appraisal-pdf", f"{CODE}.pdf")]},
        media_error=error,
    )
    _patch_runtime(monkeypatch, service)

    response = await private_report.handle_campaign_report(_token())

    assert response.status_code == 403
    assert b"private id" not in response.body


def test_property_identity_uses_canonical_cartera_fields_only():
    document = {
        "codigo": CODE,
        "ubicacion": {"comuna": "Ñuñoa"},
        "metadata": {"tipo_propiedad": "Departamento"},
        "tipo_operacion": {"tipo": "Arriendo", "venta": True, "arriendo": True},
        "commune": "Token override must not be used",
    }

    assert private_report._property_identity(document, CODE) == {
        "property_code": CODE,
        "commune": "Ñuñoa",
        "property_type": "Departamento",
        "operation": "ARRIENDO",
    }


def test_property_identity_is_loaded_read_only_from_configured_master_collection(monkeypatch):
    calls = {}
    expected_doc = {
        "codigo": CODE,
        "ubicacion": {"comuna": "Ñuñoa"},
        "metadata": {"tipo_propiedad": "Departamento"},
        "tipo_operacion": {"tipo": "Venta"},
    }

    class FakeCollection:
        def find_one(self, query, projection):
            calls["query"] = query
            calls["projection"] = projection
            return expected_doc

    class FakeDatabase:
        def __getitem__(self, collection_name):
            calls["collection"] = collection_name
            return FakeCollection()

    class FakeClient:
        def __init__(self, uri, **kwargs):
            calls["uri"] = uri
            calls["client_options"] = kwargs

        def __getitem__(self, db_name):
            calls["database"] = db_name
            return FakeDatabase()

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(private_report.Config, "MONGO_URI", "mongodb://read-only-test")
    monkeypatch.setattr(private_report.Config, "DB_NAME", "URLS")
    monkeypatch.setattr(private_report.Config, "PROPERTY_COLLECTION_NAME", "universo_cartera_prop360")
    monkeypatch.setattr(private_report, "MongoClient", FakeClient)

    assert private_report._load_property_identity(CODE) == PROPERTY_IDENTITY
    assert calls["query"] == {"codigo": CODE}
    assert calls["collection"] == "universo_cartera_prop360"
    assert calls["database"] == "URLS"
    assert calls["closed"] is True
    assert "ubicacion.comuna" in calls["projection"]
    assert "metadata.tipo_propiedad" in calls["projection"]
    assert "tipo_operacion.tipo" in calls["projection"]


def test_response_token_claims_drop_all_untrusted_document_metadata(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    decoded = private_report._decode_token_claims(
        _token(
            document_type="COMMUNAL_MARKET_REPORT",
            commune="fake",
            property_type="fake",
            operation="ARRIENDO",
            filename="fake.pdf",
            drive_file_id="fake-id",
        ),
        SECRET,
        now_epoch=1_900_000_000,
    )
    assert decoded == {"property_code": CODE, "document_type": "COMMUNAL_MARKET_REPORT"}


def test_report_route_only_accepts_signed_token_and_startup_permission_repair_is_removed():
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
    assert "drive_file_id" not in report_source
    assert "find_one(" in report_source
    report_tree = ast.parse(report_source)
    write_methods = {
        "insert_one", "insert_many", "update_one", "update_many", "replace_one",
        "delete_one", "delete_many", "find_one_and_update", "find_one_and_replace",
        "find_one_and_delete", "bulk_write", "drop", "create_index", "drop_index",
    }
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in write_methods
        for node in ast.walk(report_tree)
    )
    assert "filename*=" in report_source


def test_startup_no_longer_schedules_drive_permission_repair():
    source = (Path(__file__).parents[1] / "webhook.py").read_text(encoding="utf-8-sig")
    assert "_fix_existing_drive_permissions" not in source
    assert "STARTUP_DRIVE_PERMISSION_MUTATION=0" in source
