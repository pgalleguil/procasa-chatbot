"""Token-gated, read-only delivery of private campaign PDFs from Google Drive."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import time
import unicodedata
from typing import Any

from fastapi.responses import Response
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from services.gdrive_sync import GDriveSync


logger = logging.getLogger(__name__)

APPRAISALS_FOLDER_ID = "1PPlc7QYzbx9T4KfLLsq4LcnnzClDZFnq"
COMMUNAL_FOLDER_ID = "1wqku4RRzdDWAaMqJgVJ0AaqYOQEkV3Uh"
PDF_MIME_TYPE = "application/pdf"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
TEST_CAMPAIGN_ID = "owner_price_campaign_test_20260923"
TEST_CAMPAIGN_PREFIX = "owner_price_campaign_test_"
TEST_RECIPIENT = "pgalleguillos@procasa.cl"
REPORT_ACTION = "ver_informe"
REPORT_TYPES = frozenset({"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"})
_PROPERTY_CODE_RE = re.compile(r"^[0-9]{1,32}$")


class CampaignReportError(Exception):
    def __init__(self, status_code: int, reason: str):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


def _decode_token_claims(token: str, secret: str, *, now_epoch: int | None = None) -> dict[str, Any] | None:
    """Verify the existing t1 HMAC campaign-test token without database access."""
    if not token or not secret or os.getenv("OWNER_CAMPAIGN_TEST_MODE", "").strip().casefold() != "true":
        return None
    try:
        version, encoded, signature = token.split(".", 2)
        if version != "t1":
            return None
        expected = base64.urlsafe_b64encode(
            hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        ).rstrip(b"=").decode("ascii")
        if not hmac.compare_digest(signature, expected):
            return None
        payload_bytes = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        claims = json.loads(payload_bytes.decode("utf-8"))
        if not isinstance(claims, dict):
            return None
        now = int(now_epoch if now_epoch is not None else time.time())
        code = str(claims.get("property_code") or "")
        document_type = str(claims.get("document_type") or "").strip().upper()
        if (
            claims.get("campaign_id") != TEST_CAMPAIGN_ID
            or not str(claims.get("campaign_id") or "").startswith(TEST_CAMPAIGN_PREFIX)
            or claims.get("test_mode") is not True
            or str(claims.get("recipient") or "").casefold() != TEST_RECIPIENT
            or claims.get("action") != REPORT_ACTION
            or not _PROPERTY_CODE_RE.fullmatch(code)
            or document_type not in REPORT_TYPES
            or int(claims.get("exp") or 0) <= now
        ):
            return None
        claims["property_code"] = code
        claims["document_type"] = document_type
        if document_type == "COMMUNAL_MARKET_REPORT":
            operation = str(claims.get("operation") or "").strip().upper()
            commune = str(claims.get("commune") or "").strip()
            property_type = str(claims.get("property_type") or "").strip()
            if operation not in {"VENTA", "ARRIENDO"} or not commune or not property_type:
                return None
            claims["operation"] = operation
            claims["commune"] = commune
            claims["property_type"] = property_type
        return claims
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError, OverflowError):
        return None


def _slug(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _children(service: Any, parent_id: str, mime_type: str | None = None) -> list[dict[str, Any]]:
    query = f"'{parent_id}' in parents and trashed = false"
    if mime_type:
        query += f" and mimeType = '{mime_type}'"
    items: list[dict[str, Any]] = []
    page_token = None
    while True:
        response = service.files().list(
            q=query,
            pageSize=1000,
            fields="nextPageToken,files(id,name,mimeType,parents)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
            **({"pageToken": page_token} if page_token else {}),
        ).execute()
        items.extend(response.get("files") or [])
        page_token = response.get("nextPageToken")
        if not page_token:
            return items


def _find_unique_child(service: Any, parent_id: str, name: str, mime_type: str) -> dict[str, Any]:
    expected = _slug(name)
    matches = [
        item for item in _children(service, parent_id, mime_type)
        if item.get("id") and _slug(item.get("name")) == expected
    ]
    if len(matches) != 1:
        raise CampaignReportError(404 if not matches else 409, "document_not_found_or_ambiguous")
    return matches[0]


def _resolve_drive_file(service: Any, claims: dict[str, Any]) -> dict[str, Any]:
    code = claims["property_code"]
    document_type = claims["document_type"]
    if document_type == "INDIVIDUAL_APPRAISAL":
        root_id = APPRAISALS_FOLDER_ID
        filename = f"{code}.pdf"
        candidates = [
            item for item in _children(service, root_id, PDF_MIME_TYPE)
            if str(item.get("name") or "").casefold() == filename.casefold()
        ]
        if len(candidates) != 1:
            raise CampaignReportError(404 if not candidates else 409, "appraisal_not_found_or_ambiguous")
        return candidates[0]

    operation_folder = _find_unique_child(
        service, COMMUNAL_FOLDER_ID, claims["operation"], FOLDER_MIME_TYPE
    )
    commune_folder = _find_unique_child(
        service, str(operation_folder["id"]), claims["commune"], FOLDER_MIME_TYPE
    )
    filename_slug = _slug(claims["property_type"])
    candidates = [
        item for item in _children(service, str(commune_folder["id"]), PDF_MIME_TYPE)
        if _slug(str(item.get("name") or "").rsplit(".", 1)[0]) == filename_slug
        and str(item.get("name") or "").casefold().endswith(".pdf")
    ]
    if len(candidates) != 1:
        raise CampaignReportError(404 if not candidates else 409, "communal_report_not_found_or_ambiguous")
    return candidates[0]


def _assert_private(service: Any, file_id: str) -> None:
    page_token = None
    while True:
        response = service.permissions().list(
            fileId=file_id,
            pageSize=100,
            fields="nextPageToken,permissions(type,role)",
            supportsAllDrives=True,
            **({"pageToken": page_token} if page_token else {}),
        ).execute()
        if any((permission or {}).get("type") == "anyone" for permission in response.get("permissions") or []):
            raise CampaignReportError(403, "public_drive_permission_refused")
        page_token = response.get("nextPageToken")
        if not page_token:
            return


def _download_pdf(service: Any, file_id: str) -> bytes:
    _assert_private(service, file_id)
    stream = io.BytesIO()
    downloader = MediaIoBaseDownload(
        stream,
        service.files().get_media(fileId=file_id, supportsAllDrives=True),
    )
    done = False
    while not done:
        _, done = downloader.next_chunk()
    content = stream.getvalue()
    if not content.startswith(b"%PDF-"):
        raise CampaignReportError(502, "invalid_pdf_content")
    return content


def _response(status_code: int, content: bytes | str = b"") -> Response:
    return Response(
        content=content,
        status_code=status_code,
        media_type=PDF_MIME_TYPE if status_code == 200 else "text/plain",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            **({"Content-Disposition": 'inline; filename="respaldo.pdf"'} if status_code == 200 else {}),
        },
    )


def _drive_http_status(exc: Exception) -> int:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    return status if isinstance(status, int) and 400 <= status <= 599 else 502


def _serve_campaign_report(token: str) -> Response:
    claims = _decode_token_claims(
        token,
        os.getenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", ""),
    )
    if claims is None:
        return _response(404, "Documento no disponible.")

    try:
        drive = GDriveSync()
        service = drive.service
        if service is None:
            return _response(503, "Servicio de documentos no disponible.")
        file_record = _resolve_drive_file(service, claims)
        pdf_bytes = _download_pdf(service, str(file_record["id"]))
        logger.info(
            "[CAMPAIGN_REPORT_DRIVE] status=read_ok document_type=%s bytes=%s",
            claims["document_type"], len(pdf_bytes),
        )
        return _response(200, pdf_bytes)
    except CampaignReportError as exc:
        logger.info(
            "[CAMPAIGN_REPORT_DRIVE] status=read_failed http_status=%s reason=%s",
            exc.status_code, exc.reason,
        )
        return _response(exc.status_code, "Documento no disponible.")
    except HttpError as exc:
        status_code = _drive_http_status(exc)
        logger.info("[CAMPAIGN_REPORT_DRIVE] status=read_failed http_status=%s", status_code)
        return _response(status_code, "Documento no disponible.")
    except Exception as exc:
        logger.warning("[CAMPAIGN_REPORT_DRIVE] status=read_failed error_type=%s", type(exc).__name__)
        return _response(502, "Documento no disponible.")


async def handle_campaign_report(token: str) -> Response:
    return await asyncio.to_thread(_serve_campaign_report, token)
