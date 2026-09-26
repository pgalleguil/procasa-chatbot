"""Hash-gated resend path for the existing final visual QA case A (property 5641)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.message import EmailMessage
import hashlib
import hmac
import smtplib
from typing import Any, Callable, Sequence

from analytics.owner_campaign_email_compat import make_email_safe_html
from config import Config

from .owner_campaign_test_runtime import build_owner_campaign_test_cases_live
from .owner_campaign_test_sender import (
    SERVICE_BASE_URL,
    TEST_LEDGER_COLLECTION,
    TEST_RUN_ID,
    PreparedTestMessage,
    TestSenderError,
    _LinkParser,
    prepare_test_messages,
    validate_recipient_envelope,
)
from .test_mode import TEST_CAMPAIGN_ID, TEST_RECIPIENT, test_mode_enabled

PROPERTY_CODE = "5641"
CASE_ID = "A"
FINAL_RESEND_PURPOSE = "FINAL_VISUAL_QA_RESEND_5641_V1"
FINAL_RESEND_SUBJECT = "[TEST FINAL PROCASA] Informe propiedad 5641"
VALID_EXISTING_DELIVERY_STATUSES = frozenset({"pending_test_send", "test_sent"})


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _replace_anchor_label(source_html: str, selector: str, label: str) -> str:
    from lxml import html

    tree = html.fromstring(source_html)
    class_name = selector.removeprefix(".")
    nodes = tree.xpath(f"//*[contains(concat(' ', normalize-space(@class), ' '), ' {class_name} ')]")
    if len(nodes) != 1:
        raise TestSenderError("final_resend_required_cta_missing_or_duplicated")
    node = nodes[0]
    for child in list(node):
        node.remove(child)
    node.text = label
    return html.tostring(tree, encoding="unicode", method="html", doctype="<!doctype html>")


def _final_visual_html(prepared: PreparedTestMessage) -> str:
    html = prepared.html
    html = _replace_anchor_label(html, ".document-action-single", "VER INFORME →")
    html = _replace_anchor_label(html, ".primary", "ACEPTAR NUEVO VALOR →")
    from lxml import html as lxml_html

    tree = lxml_html.fromstring(html)
    strong = tree.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' document-copy-single ')]//strong")
    if len(strong) != 1:
        raise TestSenderError("final_resend_document_section_invalid")
    strong[0].text = "Informe comercial disponible"
    visible = " ".join(tree.text_content().split())
    required = (
        "Informe comercial disponible",
        "Tasación individual",
        "Publicaciones comparables",
        "VER INFORME →",
        "ACEPTAR NUEVO VALOR →",
    )
    if any(label not in visible for label in required):
        raise TestSenderError("final_resend_required_content_missing")
    if "Ver respaldo comercial" in visible:
        raise TestSenderError("final_resend_duplicate_document_cta")
    final_html = lxml_html.tostring(tree, encoding="unicode", method="html", doctype="<!doctype html>")
    safe_html = make_email_safe_html(final_html)
    links = _LinkParser()
    links.feed(safe_html)
    report_links = [href for href in links.hrefs if "/campana/informe?token=" in href]
    action_links = [href for href in links.hrefs if "/campana/test-accion?token=" in href]
    if len(report_links) != 1 or len(action_links) != 1:
        raise TestSenderError("final_resend_signed_links_missing_or_duplicated")
    if not report_links[0].startswith(f"{SERVICE_BASE_URL}/campana/informe?token="):
        raise TestSenderError("final_resend_report_endpoint_mismatch")
    if prepared.report_token is None or prepared.action_token is None:
        raise TestSenderError("final_resend_fresh_tokens_required")
    return safe_html


def _existing_ledger_row(db: Any) -> dict[str, Any]:
    ledger = db[TEST_LEDGER_COLLECTION]
    query = {"campaign_id": TEST_CAMPAIGN_ID, "property_code": PROPERTY_CODE}
    row = ledger.find_one(query)
    if not row:
        raise TestSenderError("final_resend_existing_qa_row_required")
    if hasattr(ledger, "count_documents") and ledger.count_documents(query) != 1:
        raise TestSenderError("final_resend_existing_qa_row_not_unique")
    if (
        row.get("campaign_id") != TEST_CAMPAIGN_ID
        or str(row.get("property_code") or "") != PROPERTY_CODE
        or row.get("test_mode") is not True
        or str(row.get("actual_recipient_email") or "").strip().casefold() != TEST_RECIPIENT
        or row.get("delivery_status") not in VALID_EXISTING_DELIVERY_STATUSES
    ):
        raise TestSenderError("final_resend_existing_qa_row_invalid")
    # A sending marker can mean SMTP accepted the message even if the process
    # lost its response. Fail closed until that ambiguous attempt is reviewed.
    if row.get("last_test_resend_status") == "sending":
        raise TestSenderError("final_visual_test_already_sent")
    return row


def _last_accepted_html_hash(row: dict[str, Any]) -> str | None:
    history = row.get("test_resend_history")
    if isinstance(history, list):
        for attempt in reversed(history):
            if isinstance(attempt, dict) and attempt.get("smtp_accepted") is True:
                value = attempt.get("html_sha256")
                if isinstance(value, str) and value:
                    return value
    if row.get("last_test_resend_smtp_accepted") is True:
        value = row.get("last_test_resend_html_sha256")
        if isinstance(value, str) and value:
            return value
    # Keep compatibility with QA rows that stored the accepted test HTML under
    # an older audit field name.
    for key in ("last_qa_email_html_sha256", "last_test_email_html_sha256"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _require_changed_html(row: dict[str, Any], html_hash: str) -> str | None:
    previous_hash = _last_accepted_html_hash(row)
    if previous_hash is not None and hmac.compare_digest(previous_hash, html_hash):
        raise TestSenderError("same_visual_test_already_sent")
    return previous_hash


def _message_with_hash_gate(html: str, *, expected_hash: str, text: str) -> tuple[EmailMessage, str]:
    smtp_hash = _sha256(html)
    if not hmac.compare_digest(expected_hash, smtp_hash):
        raise TestSenderError("final_resend_html_hash_mismatch")
    message = EmailMessage()
    message["From"] = Config.GMAIL_USER or ""
    message["To"] = TEST_RECIPIENT
    message["Subject"] = FINAL_RESEND_SUBJECT
    from email.utils import make_msgid

    message["Message-ID"] = make_msgid(domain="procasa.cl")
    message.set_content(text)
    # Recheck immediately before the MIME HTML attachment.
    smtp_hash = _sha256(html)
    if not hmac.compare_digest(expected_hash, smtp_hash):
        raise TestSenderError("final_resend_html_hash_mismatch")
    message.add_alternative(html, subtype="html", charset="utf-8")
    validate_recipient_envelope(
        to=message.get_all("To", []),
        cc=message.get_all("Cc", []),
        bcc=message.get_all("Bcc", []),
        envelope_recipients=[TEST_RECIPIENT],
    )
    return message, smtp_hash


def _reserve_existing_row(
    ledger: Any,
    row: dict[str, Any],
    html_hash: str,
    now: datetime,
    previous_hash: str | None,
) -> None:
    if previous_hash != _last_accepted_html_hash(row):
        raise TestSenderError("final_visual_test_already_sent")
    previous_hash_filter = (
        {"last_test_resend_html_sha256": row["last_test_resend_html_sha256"]}
        if "last_test_resend_html_sha256" in row
        else {"last_test_resend_html_sha256": {"$exists": False}}
    )
    status_filter = (
        {"last_test_resend_status": row["last_test_resend_status"]}
        if "last_test_resend_status" in row
        else {"last_test_resend_status": {"$exists": False}}
    )
    result = ledger.update_one(
        {
            "_id": row["_id"],
            "campaign_id": TEST_CAMPAIGN_ID,
            "property_code": PROPERTY_CODE,
            "test_mode": True,
            "actual_recipient_email": TEST_RECIPIENT,
            "delivery_status": row["delivery_status"],
            **previous_hash_filter,
            **status_filter,
        },
        {"$set": {
            "last_test_resend_purpose": FINAL_RESEND_PURPOSE,
            "last_test_resend_status": "sending",
            "last_test_resend_started_at": now,
            "last_test_resend_attempt_html_sha256": html_hash,
        }},
        upsert=False,
    )
    if getattr(result, "matched_count", 0) != 1:
        raise TestSenderError("final_visual_test_already_sent")


def resend_existing_test_case(
    *,
    dry_run: bool = True,
    db: Any = None,
    smtp_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Dry-run or send exactly one fixed final visual QA email for existing case A."""
    if not test_mode_enabled():
        raise TestSenderError("test_mode_disabled")
    if not Config.GMAIL_USER or (not dry_run and not Config.GMAIL_PASSWORD):
        raise TestSenderError("test_smtp_credentials_unavailable")
    if db is None:
        from chatbot.storage import get_db

        db = get_db()
    row = _existing_ledger_row(db)
    cases = build_owner_campaign_test_cases_live(db, case_ids=(CASE_ID,))
    if len(cases) != 1 or cases[0].case_id != CASE_ID or cases[0].property_code != PROPERTY_CODE:
        raise TestSenderError("final_resend_case_a_contract_invalid")
    prepared = prepare_test_messages(cases)
    if len(prepared) != 1 or prepared[0].report_token is None or prepared[0].action_token is None:
        raise TestSenderError("final_resend_signed_links_missing")
    item = prepared[0]
    final_html = _final_visual_html(item)
    final_hash = _sha256(final_html)
    previous_hash = _require_changed_html(row, final_hash)
    message, smtp_hash = _message_with_hash_gate(
        final_html,
        expected_hash=final_hash,
        text="Validación visual PROCASA para propiedad 5641. Los enlaces usan el flujo QA seguro.",
    )
    summary = {
        "status": "preflight_passed_no_send" if dry_run else "ready_to_send",
        "property_code": PROPERTY_CODE,
        "recipient": TEST_RECIPIENT,
        "subject": FINAL_RESEND_SUBJECT,
        "existing_ledger_row_valid": True,
        "old_html_sha256": previous_hash,
        "current_html_sha256": final_hash,
        "html_changed": previous_hash != final_hash,
        "qa_resend_allowed": True,
        "new_ledger_row_created": False,
        "report_link_present": True,
        "price_auth_link_present": True,
        "duplicate_document_cta_removed": True,
        "final_email_safe_html_sha256": final_hash,
        "smtp_html_before_mime_sha256": smtp_hash,
        "html_match": final_hash == smtp_hash,
        "test_email_sent": False,
        "owner_emails_sent": 0,
        "live_price_changed": False,
    }
    if dry_run:
        return summary

    if row.get("test_mode") is not True or row.get("actual_recipient_email") != TEST_RECIPIENT:
        raise TestSenderError("final_resend_existing_qa_row_invalid")
    sent_at = datetime.now(timezone.utc)
    ledger = db[TEST_LEDGER_COLLECTION]
    _reserve_existing_row(ledger, row, final_hash, sent_at, previous_hash)
    smtp_factory = smtp_factory or smtplib.SMTP
    try:
        with smtp_factory("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
            refused = server.sendmail(Config.GMAIL_USER, [TEST_RECIPIENT], message.as_string())
    except Exception as exc:
        ledger.update_one(
            {"_id": row["_id"], "last_test_resend_purpose": FINAL_RESEND_PURPOSE},
            {"$set": {"last_test_resend_status": "smtp_error", "last_test_resend_error_type": type(exc).__name__}},
            upsert=False,
        )
        raise TestSenderError(f"final_resend_smtp_error:{type(exc).__name__}") from exc
    if refused:
        ledger.update_one(
            {"_id": row["_id"], "last_test_resend_purpose": FINAL_RESEND_PURPOSE},
            {"$set": {"last_test_resend_status": "smtp_refused"}},
            upsert=False,
        )
        raise TestSenderError("final_resend_smtp_refused")
    rfc_message_id = str(message["Message-ID"] or "")
    audit = {
        "last_test_resend_at": sent_at,
        "last_test_resend_message_id": rfc_message_id,
        "last_test_resend_subject": FINAL_RESEND_SUBJECT,
        "last_test_resend_html_sha256": final_hash,
        "last_test_resend_accepted_html_sha256": final_hash,
        "last_test_resend_smtp_accepted": True,
        "last_test_resend_status": "test_sent",
        "last_test_resend_purpose": FINAL_RESEND_PURPOSE,
    }
    updated = ledger.update_one(
        {"_id": row["_id"], "last_test_resend_purpose": FINAL_RESEND_PURPOSE, "last_test_resend_status": "sending"},
        {
            "$set": audit,
            "$inc": {"test_resend_count": 1},
            "$push": {
                "test_resend_history": {
                    "sent_at": sent_at,
                    "message_id": rfc_message_id,
                    "subject": FINAL_RESEND_SUBJECT,
                    "html_sha256": final_hash,
                    "smtp_accepted": True,
                }
            },
        },
        upsert=False,
    )
    if getattr(updated, "matched_count", 0) != 1:
        raise TestSenderError("final_resend_audit_persistence_failed_after_smtp")
    return {
        **summary,
        "status": "sent_to_test_recipient",
        "test_email_sent": True,
        "smtp_result": "ACCEPTED",
        "rfc_message_id": rfc_message_id,
        "final_resend_audit_persisted": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-shot final visual QA resend for existing case A / 5641.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)
    result = resend_existing_test_case(dry_run=not args.send)
    for key, value in result.items():
        print(f"{key.upper()}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
