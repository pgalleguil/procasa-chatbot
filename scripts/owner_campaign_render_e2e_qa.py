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
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from pymongo import MongoClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from campanas import private_report
from campanas.test_mode import TEST_CAMPAIGN_ID, TEST_RECIPIENT, issue_test_token, test_mode_enabled
from campanas.owner_campaign_test_sender import TEST_RECIPIENT as SENDER_TEST_RECIPIENT
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


def _event_row(property_code: str, document_type: str, expires_at: int) -> dict | None:
    token_id = hashlib.sha256(
        f"{TEST_CAMPAIGN_ID}|{property_code}|ver_informe|{document_type}|{expires_at}".encode()
    ).hexdigest()[:24]
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
                    {"campana": TEST_CAMPAIGN_ID, "codigo_propiedad": property_code},
                    {"campaign_id": TEST_CAMPAIGN_ID, "property_code": property_code},
                ],
                "test_mode": True,
                "response_events": {
                    "$elemMatch": {
                        "event": "report_opened",
                        "action": "ver_informe",
                        "campaign_id": TEST_CAMPAIGN_ID,
                        "property_code": property_code,
                        "document_type": document_type,
                        "test_mode": True,
                        "token_version": "t1",
                        "event_id": token_id,
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


def _report_event(document: dict, property_code: str, document_type: str, expires_at: int) -> dict | None:
    token_id = hashlib.sha256(
        f"{TEST_CAMPAIGN_ID}|{property_code}|ver_informe|{document_type}|{expires_at}".encode()
    ).hexdigest()[:24]
    for event in document.get("response_events") or []:
        if (
            event.get("event") == "report_opened"
            and event.get("action") == "ver_informe"
            and event.get("campaign_id") == TEST_CAMPAIGN_ID
            and event.get("property_code") == property_code
            and event.get("document_type") == document_type
            and event.get("test_mode") is True
            and event.get("token_version") == "t1"
            and event.get("event_id") == token_id
        ):
            return event
    return None


def _request_report(property_code: str, document_type: str, secret: str) -> tuple[requests.Response, int]:
    expires_at = int(time.time()) + 15 * 60
    token = issue_test_token(
        campaign_id=TEST_CAMPAIGN_ID,
        property_code=property_code,
        action="ver_informe",
        secret=secret,
        expires_at=expires_at,
        recipient=TEST_RECIPIENT,
        document_type=document_type,
    )
    url = f"{SERVICE_BASE_URL}/campana/informe?{urlencode({'token': token})}"
    return requests.get(url, timeout=(10, 60), allow_redirects=False), expires_at


def _test_ledger_row(property_code: str) -> dict | None:
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
                "campaign_id": TEST_CAMPAIGN_ID,
                "property_code": property_code,
                "test_mode": True,
            },
            {"actual_recipient_email": 1, "response_events": 1},
        )
    finally:
        client.close()


def main() -> int:
    secret = os.getenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", "")
    credentials_present = bool(os.getenv("GDRIVE_CREDENTIALS_JSON", "").strip())
    test_mode = test_mode_enabled()
    emit("OWNER_CAMPAIGN_TEST_TOKEN_SECRET_PRESENT", "YES" if secret else "NO")
    emit("GDRIVE_CREDENTIALS_JSON_PRESENT", "YES" if credentials_present else "NO")
    emit("OWNER_CAMPAIGN_TEST_MODE", "true" if test_mode else "false")
    ledger_5641 = _test_ledger_row(PROPERTY_CODE) if secret and test_mode else None
    ledger_recipient = str((ledger_5641 or {}).get("actual_recipient_email") or "").strip().casefold()
    emit("PRIVATE_REPORT_TEST_RECIPIENT", private_report.TEST_RECIPIENT)
    emit("TOKEN_ISSUER_TEST_RECIPIENT", TEST_RECIPIENT)
    emit("SENDER_TEST_RECIPIENT", SENDER_TEST_RECIPIENT)
    emit("LEDGER_EXPECTED_RECIPIENT", ledger_recipient or "MISSING")
    test_mode_implementations_active = (
        issue_test_token.__module__ == "campanas.test_mode"
        and test_mode_enabled.__module__ == "campanas.test_mode"
        and private_report.decode_test_token.__module__ == "campanas.test_mode"
    )
    emit("TEST_MODE_IMPLEMENTATIONS_ACTIVE", "1" if test_mode_implementations_active else "0")
    recipients_match = len({
        private_report.TEST_RECIPIENT.casefold(),
        TEST_RECIPIENT.casefold(),
        SENDER_TEST_RECIPIENT.casefold(),
        ledger_recipient,
    }) == 1 and bool(ledger_recipient)
    emit("TEST_RECIPIENTS_MATCH", "PASS" if recipients_match else "FAIL")
    if not secret or not credentials_present or not test_mode or not recipients_match or not test_mode_implementations_active:
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
            privacy = private_report._assert_private(service, str(appraisal["id"]))
            emit("DRIVE_PRIVACY_CHECK_5641", privacy)
            pdf = private_report._download_pdf(service, str(appraisal["id"]))
            emit("5641_DRIVE_PDF_VALID", "PASS" if pdf.startswith(b"%PDF-") else "FAIL")
        else:
            emit("DRIVE_PRIVACY_CHECK_5641", "NOT_RUN")
            emit("5641_DRIVE_PDF_VALID", "FAIL")
    except Exception as exc:
        emit("5641_DRIVE_AUTH", "PASS")
        emit("5641_DRIVE_DOCUMENT_FOUND", "YES" if appraisal else "NO")
        emit("DRIVE_PRIVACY_CHECK_5641", "FAIL")
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
            emit("COMMUNAL_DRIVE_PRIVACY_CHECK", "NOT_RUN")
            emit("COMMUNAL_DRIVE_PDF_VALID", "FAIL")
        else:
            communal_identity = private_report._load_property_identity(communal_code)
            if communal_identity:
                communal_file = private_report._resolve_communal_report(service, communal_identity)
            emit("COMMUNAL_DRIVE_AUTH", "PASS")
            emit("COMMUNAL_DOCUMENT_FOUND", "YES" if communal_file else "NO")
            if communal_file:
                communal_privacy = private_report._assert_private(service, str(communal_file["id"]))
                emit("COMMUNAL_DRIVE_PRIVACY_CHECK", communal_privacy)
                communal_pdf = private_report._download_pdf(service, str(communal_file["id"]))
                emit("COMMUNAL_DRIVE_PDF_VALID", "PASS" if communal_pdf.startswith(b"%PDF-") else "FAIL")
            else:
                emit("COMMUNAL_DRIVE_PRIVACY_CHECK", "NOT_RUN")
                emit("COMMUNAL_DRIVE_PDF_VALID", "FAIL")
    except Exception as exc:
        emit("COMMUNAL_TEST_PROPERTY", communal_code or "NONE")
        emit("COMMUNAL_DRIVE_AUTH", "PASS")
        emit("COMMUNAL_DOCUMENT_FOUND", "YES" if communal_file else "NO")
        emit("COMMUNAL_DRIVE_PRIVACY_CHECK", "FAIL")
        emit("COMMUNAL_DRIVE_PDF_VALID", "FAIL")
        emit("COMMUNAL_ERROR", type(exc).__name__)

    if not SERVICE_BASE_URL.startswith("https://"):
        emit("REPORT_URL_GENERATED", "NO")
        return 4

    individual_response = None
    individual_exp = None
    communal_response = None
    communal_exp = None
    try:
        if appraisal:
            individual_response, individual_exp = _request_report(
                PROPERTY_CODE, "INDIVIDUAL_APPRAISAL", secret
            )
            emit("REPORT_URL_GENERATED", "YES")
        else:
            emit("REPORT_URL_GENERATED", "NO")
        if communal_file and communal_code:
            communal_response, communal_exp = _request_report(
                communal_code, COMMUNAL_DOCUMENT_TYPE, secret
            )
    except Exception as exc:
        emit("REPORT_REQUEST_ERROR", type(exc).__name__)

    individual_ok = False
    if individual_response is not None:
        content_type = individual_response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        resolution = individual_response.headers.get("x-campaign-report-resolution", "missing")
        pdf_ok = individual_response.content.startswith(b"%PDF-")
        emit("REPORT_HTTP_STATUS", individual_response.status_code)
        emit("REPORT_CONTENT_TYPE", content_type or "missing")
        emit("REPORT_RESOLUTION", resolution)
        emit("REPORT_PDF_SIGNATURE", "PASS" if pdf_ok else "FAIL")
        individual_ok = (
            individual_response.status_code == 200
            and content_type == "application/pdf"
            and resolution == "drive"
            and pdf_ok
        )
    else:
        emit("REPORT_HTTP_STATUS", "ERROR")
        emit("REPORT_CONTENT_TYPE", "missing")
        emit("REPORT_RESOLUTION", "missing")
        emit("REPORT_PDF_SIGNATURE", "FAIL")

    event = None
    try:
        if individual_ok and individual_exp is not None:
            event_document = _event_row(PROPERTY_CODE, "INDIVIDUAL_APPRAISAL", individual_exp)
            event = _report_event(
                event_document or {}, PROPERTY_CODE, "INDIVIDUAL_APPRAISAL", individual_exp
            )
        emit("REPORT_EVENT_PERSISTED_REAL", "PASS" if event else "FAIL")
    except Exception as exc:
        emit("REPORT_EVENT_PERSISTED_REAL", "FAIL")
        emit("REPORT_EVENT_CHECK_ERROR", type(exc).__name__)

    communal_ok = False
    if communal_response is not None:
        communal_content_type = communal_response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        communal_resolution = communal_response.headers.get("x-campaign-report-resolution", "missing")
        communal_pdf_ok = communal_response.content.startswith(b"%PDF-")
        emit("COMMUNAL_REPORT_HTTP_STATUS", communal_response.status_code)
        emit("COMMUNAL_REPORT_CONTENT_TYPE", communal_content_type or "missing")
        emit("COMMUNAL_REPORT_RESOLUTION", communal_resolution)
        emit("COMMUNAL_REPORT_PDF_SIGNATURE", "PASS" if communal_pdf_ok else "FAIL")
        communal_ok = (
            communal_response.status_code == 200
            and communal_content_type == "application/pdf"
            and communal_resolution == "drive"
            and communal_pdf_ok
        )
    else:
        emit("COMMUNAL_REPORT_HTTP_STATUS", "NOT_RUN")
        emit("COMMUNAL_REPORT_CONTENT_TYPE", "missing")
        emit("COMMUNAL_REPORT_RESOLUTION", "missing")
        emit("COMMUNAL_REPORT_PDF_SIGNATURE", "FAIL")

    return 0 if individual_ok and event and communal_ok else 9


if __name__ == "__main__":
    raise SystemExit(main())
