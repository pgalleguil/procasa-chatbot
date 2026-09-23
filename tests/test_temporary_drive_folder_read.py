from __future__ import annotations

import json
from types import SimpleNamespace

from campanas import _temporary_drive_folder_read as checker


class _Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Files:
    def __init__(self, folder_responses, list_responses):
        self.folder_responses = folder_responses
        self.list_responses = list_responses
        self.get_calls = []
        self.list_calls = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return _Request(self.folder_responses[kwargs["fileId"]])

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        parent_id = kwargs["q"].split("'", 2)[1]
        return _Request(self.list_responses[parent_id])


class _Service:
    def __init__(self, folder_responses, list_responses):
        self._files = _Files(folder_responses, list_responses)

    def files(self):
        return self._files


def _setup(monkeypatch, service):
    monkeypatch.setenv(
        "GDRIVE_CREDENTIALS_JSON",
        json.dumps({"client_email": checker.EXPECTED_SERVICE_ACCOUNT, "private_key": "never-logged"}),
    )
    monkeypatch.setattr(
        checker.service_account.Credentials,
        "from_service_account_info",
        staticmethod(lambda info, scopes: object()),
    )
    monkeypatch.setattr(checker, "build", lambda *args, **kwargs: service)


def test_readable_empty_folders_pass_and_only_get_and_list_are_called(monkeypatch):
    service = _Service(
        {
            checker.COMMUNAL_FOLDER_ID: {"mimeType": checker.FOLDER_MIME_TYPE},
            checker.APPRAISALS_FOLDER_ID: {"mimeType": checker.FOLDER_MIME_TYPE},
        },
        {checker.COMMUNAL_FOLDER_ID: {"files": []}, checker.APPRAISALS_FOLDER_ID: {"files": []}},
    )
    _setup(monkeypatch, service)

    result = checker.check_campaign_drive_folders()

    assert result == {
        "gdrive_auth_ok": True,
        "communal_folder_readable": True,
        "appraisals_folder_readable": True,
        "communal_folder_empty": True,
        "appraisals_folder_empty": True,
        "drive_error": None,
    }
    assert {call["fileId"] for call in service._files.get_calls} == {
        checker.COMMUNAL_FOLDER_ID,
        checker.APPRAISALS_FOLDER_ID,
    }
    assert len(service._files.list_calls) == 2
    assert all(call["pageSize"] == 1 for call in service._files.list_calls)


def test_nonempty_folder_is_readable_without_exposing_file_metadata(monkeypatch):
    service = _Service(
        {
            checker.COMMUNAL_FOLDER_ID: {"mimeType": checker.FOLDER_MIME_TYPE},
            checker.APPRAISALS_FOLDER_ID: {"mimeType": checker.FOLDER_MIME_TYPE},
        },
        {
            checker.COMMUNAL_FOLDER_ID: {"files": [{"id": "private-file-id"}]},
            checker.APPRAISALS_FOLDER_ID: {"files": []},
        },
    )
    _setup(monkeypatch, service)

    result = checker.check_campaign_drive_folders()

    assert result["communal_folder_readable"] is True
    assert result["communal_folder_empty"] is False
    assert result["appraisals_folder_empty"] is True
    assert "private-file-id" not in str(result)


def test_service_account_mismatch_fails_closed_without_drive_calls(monkeypatch):
    monkeypatch.setenv("GDRIVE_CREDENTIALS_JSON", json.dumps({"client_email": "unexpected@example.com"}))
    monkeypatch.setattr(checker, "build", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))

    result = checker.check_campaign_drive_folders()

    assert result["gdrive_auth_ok"] is False
    assert result["drive_error"] == "SERVICE_ACCOUNT_MISMATCH"
    assert result["communal_folder_empty"] is None
    assert result["appraisals_folder_empty"] is None
