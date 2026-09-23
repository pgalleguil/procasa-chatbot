"""Temporary, administrator-gated, read-only Google Drive campaign check."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
from typing import Any

from fastapi.responses import JSONResponse
from googleapiclient.http import MediaIoBaseDownload

from services.gdrive_sync import GDriveSync


EXPECTED_SERVICE_ACCOUNT_EMAIL = "documentos@procasa-gdrive.iam.gserviceaccount.com"
PDF_MIME_TYPE = "application/pdf"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
APPRAISALS_FOLDER_ID = "1PPlc7QYzbx9T4KfLLsq4LcnnzClDZFnq"
COMMUNAL_FOLDER_ID = "1wqku4RRzdDWAaMqJgVJ0AaqYOQEkV3Uh"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.iam\.gserviceaccount\.com$", re.I)


def _base_result(credentials_present: bool) -> dict[str, Any]:
    return {
        "gdrive_credentials_present": credentials_present,
        "service_account_email": None,
        "gdrive_auth_ok": False,
        "appraisals_folder_readable": False,
        "communal_folder_readable": False,
        "appraisals_pdf_count_sample": None,
        "communal_pdf_count_sample": None,
        "private_pdf_download_ok": False,
        "appraisals_anyone_permission": None,
        "communal_anyone_permission": None,
        "drive_errors": [],
    }


def _safe_drive_error(exc: Exception) -> dict[str, Any]:
    """Expose only Google's HTTP status/reason, never its message or request data."""
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    if not isinstance(status, int):
        status = None

    reason = None
    content = getattr(exc, "content", None)
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="ignore")
    if isinstance(content, str):
        try:
            payload = json.loads(content)
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            errors = error.get("errors", []) if isinstance(error, dict) else []
            if errors and isinstance(errors[0], dict):
                reason = errors[0].get("reason")
            if not reason and isinstance(error, dict):
                reason = error.get("status")
        except (TypeError, ValueError):
            pass

    if not isinstance(reason, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", reason):
        reason = type(exc).__name__
    return {"http_status": status, "reason": reason}


def _permissions(service: Any, resource_id: str) -> list[dict[str, Any]]:
    all_permissions: list[dict[str, Any]] = []
    page_token = None
    while True:
        request = service.permissions().list(
            fileId=resource_id,
            pageSize=100,
            fields="nextPageToken,permissions(type,role)",
            supportsAllDrives=True,
            **({"pageToken": page_token} if page_token else {}),
        )
        page = request.execute()
        all_permissions.extend(page.get("permissions", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            return all_permissions


def _has_anyone_permission(service: Any, resource_id: str) -> bool:
    return any(permission.get("type") == "anyone" for permission in _permissions(service, resource_id))


def _read_folder(
    service: Any,
    folder_id: str,
    folder_label: str,
    drive_errors: list[dict[str, Any]],
) -> tuple[bool, list[dict[str, str]] | None, bool | None]:
    try:
        metadata = service.files().get(
            fileId=folder_id,
            fields="id,mimeType,trashed",
            supportsAllDrives=True,
        ).execute()
        if metadata.get("mimeType") != FOLDER_MIME_TYPE or metadata.get("trashed") is True:
            return False, None, None
    except Exception as exc:
        drive_errors.append({"folder": folder_label, "stage": "files.get", **_safe_drive_error(exc)})
        return False, None, None

    pdfs = None
    anyone = None
    try:
        listed = service.files().list(
            q=f"'{folder_id}' in parents and mimeType='{PDF_MIME_TYPE}' and trashed=false",
            pageSize=5,
            fields="files(id,mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        pdfs = [
            {"id": str(item.get("id") or ""), "mimeType": str(item.get("mimeType") or "")}
            for item in listed.get("files", [])[:5]
            if item.get("id") and item.get("mimeType") == PDF_MIME_TYPE
        ]
    except Exception as exc:
        drive_errors.append({"folder": folder_label, "stage": "files.list", **_safe_drive_error(exc)})

    try:
        anyone = _has_anyone_permission(service, folder_id)
    except Exception as exc:
        drive_errors.append({"folder": folder_label, "stage": "permissions.list", **_safe_drive_error(exc)})
    return True, pdfs, anyone


def _download_first_pdf_bytes(service: Any, pdf_id: str) -> tuple[bool, dict[str, Any] | None]:
    try:
        if _has_anyone_permission(service, pdf_id):
            return False, None
        response = io.BytesIO()
        downloader = MediaIoBaseDownload(
            response,
            service.files().get_media(fileId=pdf_id),
            chunksize=1024,
        )
        downloader.next_chunk()
        if not response.getvalue().startswith(b"%PDF-"):
            return False, {"http_status": None, "reason": "invalidPdfHeader"}
        return True, None
    except Exception as exc:
        return False, _safe_drive_error(exc)


def run_campaign_drive_check(
    service: Any | None,
    credentials_json: str,
    *,
    appraisals_folder_id: str,
    communal_folder_id: str,
) -> tuple[int, dict[str, Any]]:
    """Run only Drive read operations and return a metadata-minimal result."""
    credentials_present = bool((credentials_json or "").strip())
    result = _base_result(credentials_present)
    if not credentials_present or service is None:
        return 503, result

    try:
        credential_info = json.loads(credentials_json)
        email = credential_info.get("client_email") if isinstance(credential_info, dict) else None
        if not isinstance(email, str) or not _EMAIL_RE.fullmatch(email.strip()):
            return 503, result
        email = email.strip()
        result["service_account_email"] = email
        if email.casefold() != EXPECTED_SERVICE_ACCOUNT_EMAIL.casefold():
            return 503, result
    except Exception:
        return 503, result

    drive_errors = result["drive_errors"]
    appraisals_readable, appraisals_pdfs, appraisals_anyone = _read_folder(
        service, appraisals_folder_id, "appraisals", drive_errors
    )
    communal_readable, communal_pdfs, communal_anyone = _read_folder(
        service, communal_folder_id, "communal", drive_errors
    )
    result["appraisals_folder_readable"] = appraisals_readable
    result["communal_folder_readable"] = communal_readable
    result["appraisals_pdf_count_sample"] = len(appraisals_pdfs) if appraisals_pdfs is not None else None
    result["communal_pdf_count_sample"] = len(communal_pdfs) if communal_pdfs is not None else None
    result["appraisals_anyone_permission"] = appraisals_anyone
    result["communal_anyone_permission"] = communal_anyone
    result["gdrive_auth_ok"] = appraisals_readable or communal_readable
    candidates = []
    if appraisals_anyone is False:
        candidates.extend(("appraisals", pdf) for pdf in appraisals_pdfs or [])
    if communal_anyone is False:
        candidates.extend(("communal", pdf) for pdf in communal_pdfs or [])
    if candidates:
        folder_label, candidate = candidates[0]
        downloaded, download_error = _download_first_pdf_bytes(service, candidate["id"])
        result["private_pdf_download_ok"] = downloaded
        if download_error:
            drive_errors.append({"folder": folder_label, "stage": "pdf.download", **download_error})
    return 200, result


async def handle_campaign_drive_check() -> JSONResponse:
    """Use the application's existing Drive adapter; route auth is enforced in webhook.py."""
    credentials_json = os.getenv("GDRIVE_CREDENTIALS_JSON", "")
    try:
        drive = await asyncio.to_thread(GDriveSync)
        service = drive.service
    except Exception:
        service = None

    status_code, result = await asyncio.to_thread(
        run_campaign_drive_check,
        service,
        credentials_json,
        appraisals_folder_id=APPRAISALS_FOLDER_ID,
        communal_folder_id=COMMUNAL_FOLDER_ID,
    )
    return JSONResponse(result, status_code=status_code)
