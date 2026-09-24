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
from functools import lru_cache
from typing import Any, Mapping
from urllib.parse import quote

from fastapi.responses import Response
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from pymongo import MongoClient
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from config import Config
from services.gdrive_sync import GDriveSync
from .owner_campaign_test_actions import TEST_CAMPAIGN_ID, TEST_RECIPIENT


logger = logging.getLogger(__name__)

APPRAISALS_FOLDER_ID = "1PPlc7QYzbx9T4KfLLsq4LcnnzClDZFnq"
COMMUNAL_FOLDER_ID = "1wqku4RRzdDWAaMqJgVJ0AaqYOQEkV3Uh"
PDF_MIME_TYPE = "application/pdf"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
TEST_CAMPAIGN_PREFIX = "owner_price_campaign_test_"
REPORT_ACTION = "ver_informe"
REPORT_TYPES = frozenset({"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"})
_PROPERTY_CODE_RE = re.compile(r"^[0-9]{1,32}$")


class CampaignReportError(Exception):
    def __init__(self, status_code: int, reason: str):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


def _decode_token_claims(token: str, secret: str, *, now_epoch: int | None = None) -> dict[str, str] | None:
    """Verify the campaign token and retain only identity/authorization claims.

    Commune, type, operation, filenames, and Drive identifiers are deliberately
    discarded. Those values must come from the canonical property record.
    """
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
        campaign_id = str(claims.get("campaign_id") or "")
        if (
            campaign_id != TEST_CAMPAIGN_ID
            or not campaign_id.startswith(TEST_CAMPAIGN_PREFIX)
            or claims.get("test_mode") is not True
            or str(claims.get("recipient") or "").casefold() != TEST_RECIPIENT
            or claims.get("action") != REPORT_ACTION
            or not _PROPERTY_CODE_RE.fullmatch(code)
            or document_type not in REPORT_TYPES
            or int(claims.get("exp") or 0) <= now
        ):
            return None
        return {"property_code": code, "document_type": document_type}
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError, OverflowError):
        return None


def _path(document: Mapping[str, Any], dotted: str) -> Any:
    current: Any = document
    for part in dotted.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _canonical_operation(property_doc: Mapping[str, Any]) -> str | None:
    operation = _path(property_doc, "tipo_operacion.tipo")
    operation_text = str(operation or "").strip().casefold()
    mentions_sale = "venta" in operation_text
    mentions_rent = "arriend" in operation_text
    if mentions_sale != mentions_rent:
        return "VENTA" if mentions_sale else "ARRIENDO"

    operation_data = property_doc.get("tipo_operacion")
    if isinstance(operation_data, Mapping):
        enabled = []
        if operation_data.get("venta") is True:
            enabled.append("VENTA")
        if operation_data.get("arriendo") is True:
            enabled.append("ARRIENDO")
        if len(enabled) == 1:
            return enabled[0]
    return None


def _property_identity(property_doc: Mapping[str, Any] | None, expected_code: str) -> dict[str, str] | None:
    if not isinstance(property_doc, Mapping):
        return None
    actual_code = str(property_doc.get("codigo") or "").strip()
    commune = _path(property_doc, "ubicacion.comuna")
    property_type = _path(property_doc, "metadata.tipo_propiedad")
    operation = _canonical_operation(property_doc)
    if (
        actual_code != expected_code
        or not isinstance(commune, str) or not commune.strip()
        or not isinstance(property_type, str) or not property_type.strip()
        or operation not in {"VENTA", "ARRIENDO"}
    ):
        return None
    return {
        "property_code": actual_code,
        "commune": commune.strip(),
        "property_type": property_type.strip(),
        "operation": operation,
    }


def _load_property_identity(property_code: str) -> dict[str, str] | None:
    """Read canonical commune/type/operation from the master property collection."""
    mongo_uri = getattr(Config, "MONGO_URI", None)
    if not mongo_uri:
        raise RuntimeError("canonical_property_source_unavailable")
    client = MongoClient(
        mongo_uri,
        serverSelectionTimeoutMS=6000,
        connectTimeoutMS=6000,
        socketTimeoutMS=8000,
    )
    try:
        collection_name = getattr(Config, "PROPERTY_COLLECTION_NAME", "universo_cartera_prop360")
        property_doc = client[getattr(Config, "DB_NAME", "URLS")][collection_name].find_one(
            {"codigo": property_code},
            {
                "_id": 0,
                "codigo": 1,
                "ubicacion.comuna": 1,
                "metadata.tipo_propiedad": 1,
                "tipo_operacion.tipo": 1,
                "tipo_operacion.venta": 1,
                "tipo_operacion.arriendo": 1,
            },
        )
    finally:
        client.close()
    return _property_identity(property_doc, property_code)


def _slug(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _children(
    service: Any,
    parent_id: str,
    mime_type: str | None = None,
    name: str | None = None,
) -> list[dict[str, Any]]:
    query = f"'{parent_id}' in parents and trashed = false"
    if mime_type:
        query += f" and mimeType = '{mime_type}'"
    if name:
        escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
        query += f" and name = '{escaped_name}'"
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


def _find_named_children(children: list[dict[str, Any]], name: str, *, pdf_only: bool = False) -> list[dict[str, Any]]:
    expected = _slug(name)
    matches = []
    for item in children:
        item_name = str(item.get("name") or "")
        if pdf_only:
            if item.get("mimeType") != PDF_MIME_TYPE or not item_name.casefold().endswith(".pdf"):
                continue
            item_name = item_name[:-4]
        if item.get("id") and _slug(item_name) == expected:
            matches.append(item)
    return matches


def _optional_child(service: Any, parent_id: str, name: str, mime_type: str) -> dict[str, Any] | None:
    matches = _find_named_children(_children(service, parent_id, mime_type), name)
    if len(matches) > 1:
        raise CampaignReportError(409, "drive_folder_ambiguous")
    return matches[0] if matches else None


def _one_pdf(children: list[dict[str, Any]], stems: tuple[str, ...], reason: str) -> dict[str, Any] | None:
    matches: dict[str, dict[str, Any]] = {}
    expected = {_slug(stem) for stem in stems}
    for item in children:
        filename = str(item.get("name") or "")
        if item.get("mimeType") != PDF_MIME_TYPE or not filename.casefold().endswith(".pdf"):
            continue
        if _slug(filename[:-4]) in expected and item.get("id"):
            matches[str(item["id"])] = item
    if len(matches) > 1:
        raise CampaignReportError(409, reason)
    return next(iter(matches.values()), None)


def _named_pdfs(
    service: Any,
    parent_id: str,
    stems: tuple[str, ...],
    reason: str,
) -> dict[str, Any] | None:
    exact_matches: list[dict[str, Any]] = []
    for stem in stems:
        exact_matches.extend(_children(service, parent_id, PDF_MIME_TYPE, f"{stem}.pdf"))
    if exact_matches:
        return _one_pdf(exact_matches, stems, reason)
    # Case/accent-normalized exact fallback supports historically inconsistent
    # capitalization without fuzzy matching or trusting a legacy filename.
    return _one_pdf(_children(service, parent_id, PDF_MIME_TYPE), stems, reason)


def _resolve_appraisal(service: Any, property_code: str) -> dict[str, Any] | None:
    return _named_pdfs(
        service,
        APPRAISALS_FOLDER_ID,
        (property_code,),
        "appraisal_ambiguous",
    )


def _resolve_communal_report(service: Any, identity: Mapping[str, str]) -> dict[str, Any] | None:
    commune = identity["commune"]
    property_type = identity["property_type"]
    operation = identity["operation"].title()
    canonical_stems = (
        f"{commune} - {property_type}",
        property_type,
    )

    # Prefer an operation-specific section when Drive uses that hierarchy.
    operation_folder = _optional_child(service, COMMUNAL_FOLDER_ID, operation, FOLDER_MIME_TYPE)
    if operation_folder:
        op_id = str(operation_folder["id"])
        commune_folder = _optional_child(service, op_id, commune, FOLDER_MIME_TYPE)
        if commune_folder:
            match = _named_pdfs(
                service,
                str(commune_folder["id"]),
                canonical_stems,
                "communal_report_ambiguous",
            )
            if match:
                return match
        match = _named_pdfs(
            service,
            op_id,
            (f"{commune} - {property_type}",),
            "communal_report_ambiguous",
        )
        if match:
            return match

    # Normalized migration filenames live directly in the communal folder.
    match = _named_pdfs(
        service,
        COMMUNAL_FOLDER_ID,
        (f"{commune} - {property_type}",),
        "communal_report_ambiguous",
    )
    if match:
        return match

    # Also accept a canonical commune directory with a type-named PDF when a
    # single report contains both operation sections.
    commune_folder = _optional_child(service, COMMUNAL_FOLDER_ID, commune, FOLDER_MIME_TYPE)
    if commune_folder:
        return _named_pdfs(
            service,
            str(commune_folder["id"]),
            (property_type,),
            "communal_report_ambiguous",
        )
    return None


def _resolve_drive_file(
    service: Any,
    claims: Mapping[str, str],
    identity: Mapping[str, str] | None,
) -> dict[str, Any] | None:
    if claims["document_type"] == "INDIVIDUAL_APPRAISAL":
        return _resolve_appraisal(service, claims["property_code"])
    if identity is None:
        return None
    return _resolve_communal_report(service, identity)


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


def _safe_client_filename(identity: Mapping[str, str] | None, property_code: str) -> str:
    if identity:
        label = f"Informe Comercial PROCASA - {identity['commune']} - {identity['property_type']}"
    else:
        label = f"Informe Comercial PROCASA - {property_code}"
    label = unicodedata.normalize("NFC", label)
    label = re.sub(r"[\r\n\\/:*?\"<>|]+", " ", label)
    label = re.sub(r"\s+", " ", label).strip(" .")[:180]
    return f"{label or 'Informe Comercial PROCASA'}.pdf"


def _content_disposition(filename: str) -> str:
    ascii_fallback = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    ascii_fallback = re.sub(r"[^A-Za-z0-9._ -]", "", ascii_fallback).strip() or "Informe Comercial PROCASA.pdf"
    return f'inline; filename="{ascii_fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


@lru_cache(maxsize=256)
def _pending_report_pdf(commune: str, property_type: str, property_code: str) -> bytes:
    """Build an honest, branded PDF response while a report is not in Drive."""
    output = io.BytesIO()
    page = canvas.Canvas(output, pagesize=letter, pageCompression=1)
    width, height = letter
    page.setTitle("Informe Comercial PROCASA - Respaldo en preparación")
    page.setFillColor(colors.HexColor("#211b83"))
    page.rect(0, height - 92, width, 92, stroke=0, fill=1)
    page.setFillColor(colors.white)
    page.setFont("Helvetica-Bold", 21)
    page.drawString(48, height - 54, "PROCASA")
    page.setFont("Helvetica", 10)
    page.drawString(48, height - 73, "INFORME COMERCIAL PARA PROPIETARIOS")

    page.setFillColor(colors.HexColor("#22245d"))
    page.setFont("Helvetica-Bold", 17)
    page.drawString(48, height - 145, "Tu respaldo comercial se está preparando")
    page.setFillColor(colors.HexColor("#4b5068"))
    page.setFont("Helvetica", 11)
    identity_label = " · ".join(value for value in (property_type, commune) if value)
    property_label = f"Propiedad {property_code}" + (f" · {identity_label}" if identity_label else "")
    page.drawString(48, height - 176, property_label[:95])
    page.drawString(48, height - 201, "El documento detallado aún no está disponible en el repositorio.")
    page.drawString(48, height - 219, "Puedes volver a abrir este enlace más tarde; se resolverá el informe actualizado.")
    page.setStrokeColor(colors.HexColor("#e5e6ef"))
    page.line(48, height - 250, width - 48, height - 250)
    page.setFillColor(colors.HexColor("#666b82"))
    page.setFont("Helvetica", 9)
    page.drawString(48, height - 273, "Este respaldo no contiene una tasación ni una conclusión de mercado.")
    page.drawString(48, height - 288, "Para más información, comunícate con tu ejecutivo PROCASA.")
    page.save()
    return output.getvalue()


def _response(
    status_code: int,
    content: bytes | str = b"",
    *,
    filename: str | None = None,
    source: str | None = None,
) -> Response:
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if status_code == 200:
        headers["Content-Disposition"] = _content_disposition(filename or "Informe Comercial PROCASA.pdf")
        headers["X-Campaign-Report-Resolution"] = source or "drive"
    return Response(
        content=content,
        status_code=status_code,
        media_type=PDF_MIME_TYPE if status_code == 200 else "text/plain",
        headers=headers,
    )


def _drive_http_status(exc: Exception) -> int:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    return status if isinstance(status, int) and 400 <= status <= 599 else 502


def _fallback_response(identity: Mapping[str, str] | None, property_code: str) -> Response:
    commune = identity.get("commune", "") if identity else ""
    property_type = identity.get("property_type", "") if identity else ""
    return _response(
        200,
        _pending_report_pdf(commune, property_type, property_code),
        filename=_safe_client_filename(identity, property_code),
        source="procasa_fallback",
    )


def _record_test_report_opened(property_code: str, token: str) -> None:
    """Record only the authorized test report-open event in the existing event store."""
    from .owner_campaign_test_actions import (
        OwnerCampaignTestError,
        record_test_report_opened,
    )

    try:
        record_test_report_opened(property_code, token=token)
    except OwnerCampaignTestError as exc:
        raise CampaignReportError(403, "test_report_open_not_authorized") from exc


def _serve_campaign_report(token: str) -> Response:
    claims = _decode_token_claims(
        token,
        os.getenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", ""),
    )
    if claims is None:
        return _response(404, "Documento no disponible.")

    property_code = claims["property_code"]
    try:
        _record_test_report_opened(property_code, token)
        identity = _load_property_identity(property_code)
        if identity is None and claims["document_type"] == "COMMUNAL_MARKET_REPORT":
            return _fallback_response(None, property_code)

        drive = GDriveSync()
        service = drive.service
        if service is None:
            return _response(503, "Servicio de documentos no disponible.")
        file_record = _resolve_drive_file(service, claims, identity)
        if file_record is None:
            logger.info(
                "[CAMPAIGN_REPORT_DRIVE] status=document_pending document_type=%s",
                claims["document_type"],
            )
            return _fallback_response(identity, property_code)
        pdf_bytes = _download_pdf(service, str(file_record["id"]))
        logger.info(
            "[CAMPAIGN_REPORT_DRIVE] status=read_ok document_type=%s bytes=%s",
            claims["document_type"], len(pdf_bytes),
        )
        return _response(
            200,
            pdf_bytes,
            filename=_safe_client_filename(identity, property_code),
            source="drive",
        )
    except CampaignReportError as exc:
        if exc.status_code == 404:
            return _fallback_response(locals().get("identity"), property_code)
        logger.info(
            "[CAMPAIGN_REPORT_DRIVE] status=read_failed http_status=%s reason=%s",
            exc.status_code, exc.reason,
        )
        return _response(exc.status_code, "Documento no disponible.")
    except HttpError as exc:
        status_code = _drive_http_status(exc)
        if status_code == 404:
            return _fallback_response(locals().get("identity"), property_code)
        logger.info("[CAMPAIGN_REPORT_DRIVE] status=read_failed http_status=%s", status_code)
        return _response(status_code, "Documento no disponible.")
    except Exception as exc:
        logger.warning("[CAMPAIGN_REPORT_DRIVE] status=read_failed error_type=%s", type(exc).__name__)
        return _response(502, "Documento no disponible.")


async def handle_campaign_report(token: str) -> Response:
    return await asyncio.to_thread(_serve_campaign_report, token)
