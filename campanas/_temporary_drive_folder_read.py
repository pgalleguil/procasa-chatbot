"""One-shot, read-only Render check for the two owner-campaign Drive folders."""

from __future__ import annotations

import json
import os
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


EXPECTED_SERVICE_ACCOUNT = "documentos@procasa-gdrive.iam.gserviceaccount.com"
APPRAISALS_FOLDER_ID = "1PPlc7QYzbx9T4KfLLsq4LcnnzClDZFnq"
COMMUNAL_FOLDER_ID = "1wqku4RRzdDWAaMqJgVJ0AaqYOQEkV3Uh"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    if isinstance(exc, HttpError) and isinstance(status, int):
        reason = ""
        try:
            body = json.loads(exc.content.decode("utf-8"))
            error = body.get("error") or {}
            reason = str(error.get("status") or "")
            if not reason:
                entries = error.get("errors") or []
                reason = str((entries[0] or {}).get("reason") or "") if entries else ""
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            pass
        return f"HTTP_{status}" + (f"_{reason}" if reason else "")
    return f"{type(exc).__name__}"


def _read_folder(service: Any, folder_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "get_ok": False,
        "list_ok": False,
        "is_folder": False,
        "empty": None,
        "get_error": None,
        "list_error": None,
    }

    try:
        metadata = service.files().get(
            fileId=folder_id,
            fields="id,mimeType,trashed",
            supportsAllDrives=True,
        ).execute()
        result["get_ok"] = not metadata.get("trashed", False)
        result["is_folder"] = metadata.get("mimeType") == FOLDER_MIME_TYPE
    except Exception as exc:
        result["get_error"] = _error_code(exc)

    try:
        listing = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            pageSize=1,
            fields="nextPageToken,files(id)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
        ).execute()
        result["list_ok"] = True
        result["empty"] = not bool(listing.get("files") or listing.get("nextPageToken"))
    except Exception as exc:
        result["list_error"] = _error_code(exc)

    result["readable"] = bool(result["get_ok"] and result["is_folder"] and result["list_ok"])
    return result


def check_campaign_drive_folders() -> dict[str, Any]:
    """Authenticate as the configured service account and read folder metadata/listings."""
    raw_credentials = os.getenv("GDRIVE_CREDENTIALS_JSON", "")
    if not raw_credentials:
        return {
            "gdrive_auth_ok": False,
            "communal_folder_readable": False,
            "appraisals_folder_readable": False,
            "communal_folder_empty": None,
            "appraisals_folder_empty": None,
            "drive_error": "GDRIVE_CREDENTIALS_MISSING",
        }

    try:
        credential_info = json.loads(raw_credentials)
        if credential_info.get("client_email") != EXPECTED_SERVICE_ACCOUNT:
            return {
                "gdrive_auth_ok": False,
                "communal_folder_readable": False,
                "appraisals_folder_readable": False,
                "communal_folder_empty": None,
                "appraisals_folder_empty": None,
                "drive_error": "SERVICE_ACCOUNT_MISMATCH",
            }
        credentials = service_account.Credentials.from_service_account_info(
            credential_info, scopes=DRIVE_SCOPES
        )
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    except Exception as exc:
        return {
            "gdrive_auth_ok": False,
            "communal_folder_readable": False,
            "appraisals_folder_readable": False,
            "communal_folder_empty": None,
            "appraisals_folder_empty": None,
            "drive_error": f"CREDENTIAL_SETUP_{type(exc).__name__}",
        }

    communal = _read_folder(service, COMMUNAL_FOLDER_ID)
    appraisals = _read_folder(service, APPRAISALS_FOLDER_ID)
    errors = []
    for label, outcome in (("COMMUNAL", communal), ("APPRAISALS", appraisals)):
        if outcome["get_error"]:
            errors.append(f"{label}_FILES_GET_{outcome['get_error']}")
        elif outcome["get_ok"] and not outcome["is_folder"]:
            errors.append(f"{label}_NOT_A_FOLDER")
        if outcome["list_error"]:
            errors.append(f"{label}_FILES_LIST_{outcome['list_error']}")

    statuses = [communal, appraisals]
    auth_rejected = any(
        error and ("HTTP_401" in error or "HTTP_400" in error)
        for outcome in statuses
        for error in (outcome["get_error"], outcome["list_error"])
    )
    api_replied = any(
        outcome["get_ok"] or outcome["list_ok"]
        or any(error and ("HTTP_403" in error or "HTTP_404" in error) for error in (outcome["get_error"], outcome["list_error"]))
        for outcome in statuses
    )

    return {
        "gdrive_auth_ok": False if auth_rejected else (True if api_replied else None),
        "communal_folder_readable": communal["readable"],
        "appraisals_folder_readable": appraisals["readable"],
        "communal_folder_empty": communal["empty"],
        "appraisals_folder_empty": appraisals["empty"],
        "drive_error": ";".join(errors) if errors else None,
    }
