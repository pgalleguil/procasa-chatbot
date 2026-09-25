"""Run the authorized owner-campaign report check from the Render runtime.

This read-only Drive check validates the two prepared PDFs, then performs one
signed test GET for property 5641. That route appends its normal QA click/open
events to the existing test row in ajuste_precio. It never prints tokens or
Drive IDs and never changes Drive permissions, prices, or property records.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from pymongo import MongoClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from campanas import private_report
from campanas.test_mode import TEST_CAMPAIGN_ID, TEST_RECIPIENT, issue_test_token
from config import Config
from services.gdrive_sync import GDriveSync


PROPERTY_CODE = "5641"
COMMUNAL_PROPERTY = "16486"
COMMUNAL_DOCUMENT_TYPE = "COMMUNAL_MARKET_REPORT"
SERVICE_BASE_URL = (
    os.getenv("SERVICE_BASE_URL", "").strip().rstrip("/")
    or Config.CRM_BASE_URL.rstrip("/")
)


def emit(key: str, value: object) -> None:
    print(f"{key}={value}", flush=True)


def _event_row() -> dict | None:
    client = MongoClient(
        Config.MONGO_URI,
        serverSelectionTimeoutMS=8000,
        connectTimeoutMS=8000,
        socketTimeoutMS=10000,
    )
    try:
        collection = client[Config.DB_NAME][Config.COLLECTION_CAMPANAS_LOG]
        return collection.find_one(
            {
                "$or": [
                    {"campana": TEST_CAMPAIGN_ID, "codigo_propiedad": PROPERTY_CODE},
                    {"campaign_id": TEST_CAMPAIGN_ID, "property_code": PROPERTY_CODE},
                ],
                "test_mode": True,
                "response_events": {
                    "$elemMatch": {
                        "event": "report_opened",
                        "action": "ver_informe",
                        "campaign_id": TEST_CAMPAIGN_ID,
                        "property_code": PROPERTY_CODE,
                        "document_type": "INDIVIDUAL_APPRAISAL",
                        "test_mode": True,
                        "token_version": "t1",
                    }
                },
            },
            {"response_events": 1, "campaign_id": 1, "property_code": 1, "campana": 1, "codigo_propiedad": 1},
        )
    finally:
        client.close()


def _communal_fixture_property() -> str | None:
    """Select 16486 only when its prepared QA ledger row says it is communal."""
    client = MongoClient(
        Config.MONGO_URI,
        serverSelectionTimeoutMS=8000,
        connectTimeoutMS=8000,
        socketTimeoutMS=10000,
    )
    try:
        collection = client[Config.DB_NAME][Config.COLLECTION_CAMPANAS_LOG]
        rows = collection.find(
            {
                "test_mode": True,
                "actual_recipient_email": TEST_RECIPIENT,
                "$or": [
                    {"campaign_id": TEST_CAMPAIGN_ID},
                    {"campana": TEST_CAMPAIGN_ID},
                ],
            },
            {
                "property_code": 1,
                "codigo_propiedad": 1,
                "supporting_document_type": 1,
                "document_type": 1,
            },
        )
        selected = []
        for row in rows:
            code = str(row.get("property_code") or row.get("codigo_propiedad") or "").strip()
            doc_type = str(
                row.get("supporting_document_type") or row.get("document_type") or ""
            ).strip().upper()
            if code.isdigit() and doc_type == COMMUNAL_DOCUMENT_TYPE:
                selected.append(code)
        if COMMUNAL_PROPERTY in selected:
            return COMMUNAL_PROPERTY
        return sorted(selected, key=lambda item: (len(item), item))[0] if selected else None
    finally:
        client.close()


def _report_event(document: dict) -> dict | None:
    for event in document.get("response_events") or []:
        if (
            event.get("event") == "report_opened"
            and event.get("action") == "ver_informe"
            and event.get("campaign_id") == TEST_CAMPAIGN_ID
            and event.get("property_code") == PROPERTY_CODE
            and event.get("document_type") == "INDIVIDUAL_APPRAISAL"
            and event.get("test_mode") is True
            and event.get("token_version") == "t1"
        ):
            return event
    return None


def main() -> int:
    secret = os.getenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", "")
    credentials_present = bool(os.getenv("GDRIVE_CREDENTIALS_JSON", "").strip())
    test_mode = os.getenv("OWNER_CAMPAIGN_TEST_MODE", "").strip().casefold() == "true"
    emit("OWNER_CAMPAIGN_TEST_TOKEN_SECRET_PRESENT", "YES" if secret else "NO")
    emit("GDRIVE_CREDENTIALS_JSON_PRESENT", "YES" if credentials_present else "NO")
    emit("OWNER_CAMPAIGN_TEST_MODE", "true" if test_mode else "false")
    if not secret or not credentials_present or not test_mode:
        return 2

    drive = GDriveSync()
    service = drive.service
    if service is None:
        emit("5641_DRIVE_AUTH", "FAIL")
        emit("COMMUNAL_DRIVE_AUTH", "FAIL")
        return 3

    appraisal = None
    try:
        appraisal = private_report._resolve_appraisal(service, PROPERTY_CODE)
        emit("5641_DRIVE_AUTH", "PASS")
        emit("5641_DRIVE_DOCUMENT_FOUND", "YES" if appraisal else "NO")
        if appraisal:
            private_report._assert_private(service, str(appraisal["id"]))
            emit("5641_DRIVE_FILE_IS_PRIVATE", "YES")
            pdf = private_report._download_pdf(service, str(appraisal["id"]))
            emit("5641_DRIVE_PDF_VALID", "YES" if pdf.startswith(b"%PDF-") else "NO")
        else:
            emit("5641_DRIVE_FILE_IS_PRIVATE", "NO")
            emit("5641_DRIVE_PDF_VALID", "NO")
    except Exception as exc:
        emit("5641_DRIVE_AUTH", "PASS")
        emit("5641_DRIVE_DOCUMENT_FOUND", "YES" if appraisal else "NO")
        emit("5641_DRIVE_FILE_IS_PRIVATE", "FAIL")
        emit("5641_DRIVE_PDF_VALID", "FAIL")
        emit("5641_DRIVE_ERROR", type(exc).__name__)

    communal_file = None
    communal_identity = None
    communal_code = None
    try:
        communal_code = _communal_fixture_property()
        emit("COMMUNAL_TEST_PROPERTY", communal_code or "NONE")
        if not communal_code:
            emit("COMMUNAL_DRIVE_AUTH", "NOT_RUN")
            emit("COMMUNAL_DOCUMENT_FOUND", "NO")
            emit("COMMUNAL_PDF_VALID", "NO")
        else:
            communal_identity = private_report._load_property_identity(communal_code)
            if communal_identity:
                communal_file = private_report._resolve_communal_report(service, communal_identity)
            emit("COMMUNAL_DRIVE_AUTH", "PASS")
            emit("COMMUNAL_DOCUMENT_FOUND", "YES" if communal_file else "NO")
            if communal_file:
                private_report._assert_private(service, str(communal_file["id"]))
                communal_pdf = private_report._download_pdf(service, str(communal_file["id"]))
                emit("COMMUNAL_PDF_VALID", "YES" if communal_pdf.startswith(b"%PDF-") else "NO")
            else:
                emit("COMMUNAL_PDF_VALID", "NO")
    except Exception as exc:
        emit("COMMUNAL_TEST_PROPERTY", communal_code or "NONE")
        emit("COMMUNAL_DRIVE_AUTH", "PASS")
        emit("COMMUNAL_DOCUMENT_FOUND", "YES" if communal_file else "NO")
        emit("COMMUNAL_PDF_VALID", "FAIL")
        emit("COMMUNAL_ERROR", type(exc).__name__)

    if not appraisal or not SERVICE_BASE_URL.startswith("https://"):
        emit("REPORT_URL_GENERATED", "NO")
        return 4

    expires_at = int(time.time()) + 15 * 60
    token = issue_test_token(
        campaign_id=TEST_CAMPAIGN_ID,
        property_code=PROPERTY_CODE,
        action="ver_informe",
        secret=secret,
        expires_at=expires_at,
        recipient=TEST_RECIPIENT,
        document_type="INDIVIDUAL_APPRAISAL",
    )
    url = f"{SERVICE_BASE_URL}/campana/informe?{urlencode({'token': token})}"
    emit("REPORT_URL_GENERATED", "YES")

    try:
        response = requests.get(url, timeout=(10, 60), allow_redirects=False)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        resolution = response.headers.get("x-campaign-report-resolution", "missing")
        pdf_ok = response.content.startswith(b"%PDF-")
        emit("REPORT_HTTP_STATUS", response.status_code)
        emit("REPORT_CONTENT_TYPE", content_type or "missing")
        emit("REPORT_RESOLUTION", resolution)
        emit("REPORT_PDF_SIGNATURE", "PASS" if pdf_ok else "FAIL")
        if response.status_code != 200 or content_type != "application/pdf" or resolution != "drive" or not pdf_ok:
            emit("REPORT_EVENT_PERSISTED_REAL", "FAIL")
            return 5
    except Exception as exc:
        emit("REPORT_HTTP_STATUS", "ERROR")
        emit("REPORT_CONTENT_TYPE", "missing")
        emit("REPORT_RESOLUTION", "missing")
        emit("REPORT_PDF_SIGNATURE", "FAIL")
        emit("REPORT_EVENT_PERSISTED_REAL", "FAIL")
        emit("REPORT_REQUEST_ERROR", type(exc).__name__)
        return 6

    try:
        event_document = _event_row()
        event = _report_event(event_document or {})
        if not event:
            emit("REPORT_EVENT_PERSISTED_REAL", "FAIL")
            return 7
        emit("REPORT_EVENT_PERSISTED_REAL", "PASS")
        emit(f"EVENT_campaign_id", event.get("campaign_id", ""))
        emit("EVENT_property_code", event.get("property_code", ""))
        emit("EVENT_event_type", event.get("event", ""))
        emit("EVENT_document_type", event.get("document_type", ""))
        emit("EVENT_test_mode", str(event.get("test_mode", "")).lower())
        emit("EVENT_timestamp", event.get("event_at", ""))
    except Exception as exc:
        emit("REPORT_EVENT_PERSISTED_REAL", "FAIL")
        emit("REPORT_EVENT_CHECK_ERROR", type(exc).__name__)
        return 8

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
