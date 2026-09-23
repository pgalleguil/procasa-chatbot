import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from campanas import drive_check


APPRAISALS_ID = "appraisals-test-folder"
COMMUNAL_ID = "communal-test-folder"
SERVICE_EMAIL = "documentos@procasa-gdrive.iam.gserviceaccount.com"


class _Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Files:
    def __init__(self, pdfs=None):
        self.pdfs = pdfs or {APPRAISALS_ID: [], COMMUNAL_ID: []}
        self.get_calls = []
        self.list_calls = []
        self.media_calls = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return _Request({"mimeType": drive_check.FOLDER_MIME_TYPE, "trashed": False})

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        folder_id = kwargs["q"].split("'", 2)[1]
        return _Request({"files": self.pdfs.get(folder_id, [])[: kwargs["pageSize"]]})

    def get_media(self, **kwargs):
        self.media_calls.append(kwargs)
        return SimpleNamespace(file_id=kwargs["fileId"])


class _Permissions:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}
        self.list_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _Request({"permissions": self.mapping.get(kwargs["fileId"], [])})


class _Service:
    def __init__(self, pdfs=None, permissions=None):
        self._files = _Files(pdfs)
        self._permissions = _Permissions(permissions)

    def files(self):
        return self._files

    def permissions(self):
        return self._permissions


class _Downloader:
    instances = []

    def __init__(self, stream, request, chunksize):
        self.stream = stream
        self.request = request
        self.chunksize = chunksize
        self.__class__.instances.append(self)

    def next_chunk(self):
        self.stream.write(b"%PDF-1.7\n")
        return None, True


def _credentials_json(email=SERVICE_EMAIL):
    return json.dumps({
        "client_email": email,
        "private_key": "PRIVATE_KEY_SENTINEL",
        "private_key_id": "PRIVATE_KEY_ID_SENTINEL",
        "token": "TOKEN_SENTINEL",
    })


def test_route_uses_existing_strict_admin_guard_before_drive_check():
    source = (Path(__file__).parents[1] / "webhook.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    route = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "internal_campaign_drive_check"
    )
    awaited_calls = []
    for node in route.body:
        awaited = node.value if isinstance(node, ast.Expr) else getattr(node, "value", None)
        if isinstance(awaited, ast.Await) and isinstance(awaited.value, ast.Call):
            if isinstance(awaited.value.func, ast.Name):
                awaited_calls.append(awaited.value.func.id)
    assert awaited_calls == ["_require_captacion_report_admin", "handle_campaign_drive_check"]


def test_check_reuses_injected_drive_service_limits_samples_and_downloads_one_private_pdf(monkeypatch):
    app_pdfs = [{"id": f"app-pdf-{n}", "mimeType": "application/pdf"} for n in range(7)]
    communal_pdfs = [{"id": "communal-pdf-1", "mimeType": "application/pdf"}]
    service = _Service(
        pdfs={APPRAISALS_ID: app_pdfs, COMMUNAL_ID: communal_pdfs},
        permissions={},
    )
    monkeypatch.setattr(drive_check, "MediaIoBaseDownload", _Downloader)
    _Downloader.instances.clear()

    status_code, result = drive_check.run_campaign_drive_check(
        service,
        _credentials_json(),
        appraisals_folder_id=APPRAISALS_ID,
        communal_folder_id=COMMUNAL_ID,
    )

    assert status_code == 200
    assert result["gdrive_credentials_present"] is True
    assert result["service_account_email"] == SERVICE_EMAIL
    assert result["gdrive_auth_ok"] is True
    assert result["appraisals_folder_readable"] is True
    assert result["communal_folder_readable"] is True
    assert result["appraisals_pdf_count_sample"] == 5
    assert result["communal_pdf_count_sample"] == 1
    assert result["appraisals_anyone_permission"] is False
    assert result["communal_anyone_permission"] is False
    assert result["private_pdf_download_ok"] is True
    assert [call["pageSize"] for call in service._files.list_calls] == [5, 5]
    assert len(_Downloader.instances) == 1
    assert _Downloader.instances[0].chunksize == 1024
    assert service._files.media_calls == [{"fileId": "app-pdf-0"}]
    assert len(service._permissions.list_calls) == 3  # Two folders plus the selected PDF.

    serialized = json.dumps(result)
    assert "PRIVATE_KEY_SENTINEL" not in serialized
    assert "PRIVATE_KEY_ID_SENTINEL" not in serialized
    assert "TOKEN_SENTINEL" not in serialized
    assert "app-pdf-0" not in serialized


def test_drive_permission_error_returns_only_http_status_and_reason(monkeypatch):
    service = _Service()

    class _DriveError(Exception):
        resp = SimpleNamespace(status=403)
        content = json.dumps({
            "error": {
                "message": "private-folder-id must not be returned",
                "errors": [{"reason": "permissionDenied"}],
            }
        }).encode()

    def fail_get(**_kwargs):
        raise _DriveError()

    service._files.get = fail_get
    status_code, result = drive_check.run_campaign_drive_check(
        service,
        _credentials_json(),
        appraisals_folder_id=APPRAISALS_ID,
        communal_folder_id=COMMUNAL_ID,
    )

    assert status_code == 200
    assert result["gdrive_auth_ok"] is False
    assert result["drive_errors"] == [
        {"folder": "appraisals", "stage": "files.get", "http_status": 403, "reason": "permissionDenied"},
        {"folder": "communal", "stage": "files.get", "http_status": 403, "reason": "permissionDenied"},
    ]
    assert "private-folder-id" not in json.dumps(result)


def test_public_folder_permission_is_reported_and_pdf_is_not_downloaded(monkeypatch):
    service = _Service(
        pdfs={APPRAISALS_ID: [{"id": "public-folder-pdf", "mimeType": "application/pdf"}]},
        permissions={APPRAISALS_ID: [{"type": "anyone", "role": "reader"}]},
    )
    monkeypatch.setattr(drive_check, "MediaIoBaseDownload", _Downloader)
    _Downloader.instances.clear()

    _, result = drive_check.run_campaign_drive_check(
        service,
        _credentials_json(),
        appraisals_folder_id=APPRAISALS_ID,
        communal_folder_id=COMMUNAL_ID,
    )

    assert result["appraisals_anyone_permission"] is True
    assert result["private_pdf_download_ok"] is False
    assert service._files.media_calls == []
    assert _Downloader.instances == []


def test_wrong_service_account_identity_fails_closed_without_drive_calls():
    service = _Service()
    status_code, result = drive_check.run_campaign_drive_check(
        service,
        _credentials_json("other@example.iam.gserviceaccount.com"),
        appraisals_folder_id=APPRAISALS_ID,
        communal_folder_id=COMMUNAL_ID,
    )

    assert status_code == 503
    assert result["service_account_email"] == "other@example.iam.gserviceaccount.com"
    assert service._files.get_calls == []


def test_handler_uses_existing_drive_adapter_and_fixed_folder_ids(monkeypatch):
    service = _Service()
    monkeypatch.setenv("GDRIVE_CREDENTIALS_JSON", _credentials_json())
    monkeypatch.setattr(
        drive_check,
        "GDriveSync",
        lambda: SimpleNamespace(service=service),
    )
    observed = {}

    def fake_check(received_service, credentials_json, **kwargs):
        observed["service"] = received_service
        observed["credentials_json"] = credentials_json
        observed.update(kwargs)
        return 200, {"ok": True}

    monkeypatch.setattr(drive_check, "run_campaign_drive_check", fake_check)
    response = asyncio.run(drive_check.handle_campaign_drive_check())

    assert response.status_code == 200
    assert observed["service"] is service
    assert json.loads(observed["credentials_json"])["client_email"] == SERVICE_EMAIL
    assert observed["appraisals_folder_id"] == "1PPlc7QYzbx9T4KfLLsq4LcnnzClDZFnq"
    assert observed["communal_folder_id"] == "1wqku4RRzdDWAaMqJgVJ0AaqYOQEkV3Uh"
