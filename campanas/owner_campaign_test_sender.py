"""SMTP sender isolated to four explicit owner-campaign E2E cases.

There is intentionally no recipient parameter, campaign query, batch lookup, or
mass-send path. Every message is addressed to the fixed test mailbox only.
"""

from __future__ import annotations

import math
import re
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, quote, urlsplit

from config import Config

from .owner_campaign_test_actions import (
    ACCEPT_PRICE_ACTION,
    ADVISOR_ACTION,
    REPORT_ACTION,
    TEST_CAMPAIGN_ID,
    issue_test_link_token,
    test_mode_enabled,
    verify_test_link_token,
)


TEST_RECIPIENT = "pgalleguillos@procasa.cl"
SERVICE_BASE_URL = "https://procasa-chatbot-yr8d.onrender.com"
MAX_TEST_EMAILS = 4
TEST_CASE_IDS = frozenset({"A", "B", "C", "D"})
TEST_B_UNICODE_COMMUNES = frozenset({"Ñuñoa", "Maipú", "Peñaflor", "Estación Central", "Chillán", "Isla de Maipo"})
TEST_LEDGER_COLLECTION = "ajuste_precio"
PROPERTY_CODE_RE = re.compile(r"^[0-9]{1,32}$")
REQUIRED_RENDER_CHECKS = frozenset(
    {
        "real_property_image",
        "executive_name",
        "executive_email",
        "executive_phone",
        "activity_90d",
        "portal_breakdown_valid",
        "operation_correct",
        "price_correct",
        "reference_correct",
        "comparables_compatible",
        "own_listing_excluded",
        "cta_correct",
    }
)


class TestSenderError(ValueError):
    """A test send failed a fail-closed recipient, scope, or render guard."""


@dataclass(frozen=True)
class OwnerCampaignTestCase:
    case_id: str
    property_code: str
    intended_owner_email: str
    operation: str
    evidence_segment: str
    document_type: str
    executive: str
    current_price: float
    raw_recommended_price: float | None = None
    display_recommended_price: float | None = None
    adjustment_pct: float | None = None
    evidence_version: str = "cluster_v2_20260921"
    commune: str = ""
    property_type: str = ""
    render_context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedTestMessage:
    case: OwnerCampaignTestCase
    subject: str
    html: str
    text: str
    report_token: str | None
    action_token: str | None


def build_test_links(case: OwnerCampaignTestCase) -> dict[str, str]:
    """Return signed test-only report/action links for one explicit test case."""
    if not isinstance(case, OwnerCampaignTestCase):
        raise TestSenderError("explicit_test_case_required")
    links: dict[str, str] = {}
    if case.document_type in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}:
        report_token = issue_test_link_token(
            property_code=case.property_code,
            action=REPORT_ACTION,
            document_type=case.document_type,
        )
        links["report"] = f"{SERVICE_BASE_URL}/campana/informe?token={quote(report_token, safe='')}"
    action = {
        "A": ACCEPT_PRICE_ACTION,
        "C": ADVISOR_ACTION,
        "D": ADVISOR_ACTION,
    }.get(case.case_id)
    if action:
        action_token = issue_test_link_token(property_code=case.property_code, action=action)
        links["action"] = f"{SERVICE_BASE_URL}/campana/test-accion?token={quote(action_token, safe='')}"
    return links


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.hrefs.append(href)


def _address_list(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else list(value)
    return [str(item).strip().casefold() for item in values if str(item).strip()]


def validate_recipient_envelope(
    *,
    to: str | Sequence[str],
    cc: str | Sequence[str] | None = None,
    bcc: str | Sequence[str] | None = None,
    envelope_recipients: str | Sequence[str] | None = None,
) -> None:
    """Require one exact TO and reject all CC/BCC or alternate SMTP recipients."""
    to_values = _address_list(to)
    if to_values != [TEST_RECIPIENT]:
        raise TestSenderError("test_recipient_mismatch")
    if _address_list(cc):
        raise TestSenderError("test_cc_forbidden")
    if _address_list(bcc):
        raise TestSenderError("test_bcc_forbidden")
    if envelope_recipients is not None and _address_list(envelope_recipients) != [TEST_RECIPIENT]:
        raise TestSenderError("test_envelope_mismatch")


def _finite_positive(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def validate_explicit_cases(cases: Sequence[OwnerCampaignTestCase]) -> list[OwnerCampaignTestCase]:
    if not isinstance(cases, (list, tuple)) or any(not isinstance(case, OwnerCampaignTestCase) for case in cases):
        raise TestSenderError("explicit_test_cases_required")
    if not 1 <= len(cases) <= MAX_TEST_EMAILS:
        raise TestSenderError("test_case_count_invalid")
    ids = [case.case_id for case in cases]
    codes = [str(case.property_code) for case in cases]
    if (
        len(ids) != len(set(ids))
        or len(codes) != len(set(codes))
        or any(case_id not in TEST_CASE_IDS for case_id in ids)
    ):
        raise TestSenderError("test_case_ids_invalid")
    for case in cases:
        if (
            not PROPERTY_CODE_RE.fullmatch(str(case.property_code or ""))
            or not str(case.intended_owner_email or "").strip()
            or not str(case.executive or "").strip()
            or not _finite_positive(case.current_price)
        ):
            raise TestSenderError("test_case_identity_invalid")
        if case.case_id == "A":
            if (
                case.operation != "VENTA"
                or case.evidence_segment != "PRICE_AUTHORIZATION_READY"
                or case.document_type != "INDIVIDUAL_APPRAISAL"
                or not _finite_positive(case.raw_recommended_price)
                or not _finite_positive(case.display_recommended_price)
                or float(case.display_recommended_price) >= float(case.current_price)
            ):
                raise TestSenderError("test_case_a_contract_invalid")
        elif case.case_id == "B":
            if (
                case.operation != "VENTA"
                or case.document_type != "COMMUNAL_MARKET_REPORT"
                or case.commune not in TEST_B_UNICODE_COMMUNES
                or not case.property_type.strip()
            ):
                raise TestSenderError("test_case_b_contract_invalid")
        elif case.case_id == "C":
            if case.property_code != "16521" or case.evidence_segment != "MIXED_EVIDENCE":
                raise TestSenderError("test_case_c_contract_invalid")
        elif case.case_id == "D":
            if case.property_code != "16527" or case.operation != "ARRIENDO":
                raise TestSenderError("test_case_d_contract_invalid")
    return list(cases)


def _token_from_link(href: str, expected_path: str) -> dict[str, Any] | None:
    parsed = urlsplit(href)
    if parsed.path.rstrip("/") != expected_path:
        return None
    values = parse_qs(parsed.query, keep_blank_values=True)
    if set(values) != {"token"} or len(values["token"]) != 1:
        raise TestSenderError("test_link_query_invalid")
    claims = verify_test_link_token(values["token"][0])
    if claims is None:
        raise TestSenderError("test_link_signature_invalid")
    return claims


def _validate_rendered_case(case: OwnerCampaignTestCase, rendered: Mapping[str, Any]) -> PreparedTestMessage:
    if not isinstance(rendered, Mapping):
        raise TestSenderError("test_render_invalid")
    subject = str(rendered.get("subject") or "").strip()
    html = str(rendered.get("html") or "")
    text = str(rendered.get("text") or "")
    checks = rendered.get("checks")
    if not subject or "\r" in subject or "\n" in subject or not html.strip() or not text.strip():
        raise TestSenderError("test_render_incomplete")
    if not isinstance(checks, Mapping) or any(checks.get(key) is not True for key in REQUIRED_RENDER_CHECKS):
        raise TestSenderError("test_render_quality_check_failed")
    if case.intended_owner_email.strip().casefold() in (subject + "\n" + html + "\n" + text).casefold():
        raise TestSenderError("intended_owner_email_leaked")

    if case.case_id == "D":
        rent_checks = {"rental_uf_per_month", "rental_estimate", "rental_comparables"}
        if checks.get("sale_fields_present") is not False or any(checks.get(key) is not True for key in rent_checks):
            raise TestSenderError("test_case_d_sale_or_rent_check_failed")

    parser = _LinkParser()
    parser.feed(html)
    report_token = None
    action_token = None
    expected_action = {
        "A": ACCEPT_PRICE_ACTION,
        "C": ADVISOR_ACTION,
        "D": ADVISOR_ACTION,
    }.get(case.case_id)
    for href in parser.hrefs:
        parsed = urlsplit(href)
        path = parsed.path.rstrip("/")
        if path.endswith("/campana/respuesta") or path == "/campana/respuesta":
            raise TestSenderError("legacy_cta_forbidden")
        if path.endswith("/campana/informe"):
            claims = _token_from_link(href, path)
            if (
                claims.get("action") != REPORT_ACTION
                or str(claims.get("property_code")) != case.property_code
                or claims.get("document_type") != case.document_type
            ):
                raise TestSenderError("test_report_link_mismatch")
            report_token = parse_qs(parsed.query)["token"][0]
        elif path.endswith("/campana/test-accion"):
            claims = _token_from_link(href, path)
            if (
                str(claims.get("property_code")) != case.property_code
                or claims.get("action") != expected_action
            ):
                raise TestSenderError("test_action_link_mismatch")
            action_token = parse_qs(parsed.query)["token"][0]

    if case.case_id in {"A", "B"} and report_token is None:
        raise TestSenderError("test_report_link_missing")
    if expected_action and action_token is None:
        raise TestSenderError("test_action_link_missing")

    # The renderer cannot override recipients. If it attempts to do so, abort
    # the complete batch before SMTP is even initialized.
    if any(key in rendered for key in ("to", "cc", "bcc", "envelope_recipients")):
        validate_recipient_envelope(
            to=rendered.get("to", TEST_RECIPIENT),
            cc=rendered.get("cc"),
            bcc=rendered.get("bcc"),
            envelope_recipients=rendered.get("envelope_recipients", [TEST_RECIPIENT]),
        )
    return PreparedTestMessage(
        case=case,
        subject=f"[PRUEBA E2E] {subject}",
        html=html,
        text=text,
        report_token=report_token,
        action_token=action_token,
    )


def prepare_test_messages(
    cases: Sequence[OwnerCampaignTestCase],
    *,
    render_case: Callable[[OwnerCampaignTestCase], Mapping[str, Any]],
) -> list[PreparedTestMessage]:
    """Render and validate every explicit case before any SMTP connection."""
    validated_cases = validate_explicit_cases(cases)
    prepared = [_validate_rendered_case(case, render_case(case)) for case in validated_cases]
    for _ in prepared:
        validate_recipient_envelope(
            to=TEST_RECIPIENT,
            cc=(),
            bcc=(),
            envelope_recipients=[TEST_RECIPIENT],
        )
    return prepared


def _write_test_ledger_entries(db: Any, prepared: Sequence[PreparedTestMessage]) -> None:
    ledger = db[TEST_LEDGER_COLLECTION]
    entries = []
    for item in prepared:
        case = item.case
        query = {"campaign_id": TEST_CAMPAIGN_ID, "property_code": case.property_code}
        existing = ledger.find_one(query)
        if existing:
            if (
                existing.get("test_mode") is not True
                or existing.get("actual_recipient_email") != TEST_RECIPIENT
            ):
                raise TestSenderError("non_test_ledger_collision")
            raise TestSenderError("test_campaign_case_already_registered")
        entries.append((query, {
            "campaign_id": TEST_CAMPAIGN_ID,
            "campaign_version": "owner_campaign_test_20260923",
            "property_code": case.property_code,
            "intended_owner_email": case.intended_owner_email.strip(),
            "actual_recipient_email": TEST_RECIPIENT,
            "operation": case.operation,
            "current_price_at_send": case.current_price,
            "raw_recommended_price": case.raw_recommended_price,
            "display_recommended_price": case.display_recommended_price,
            "adjustment_pct": case.adjustment_pct,
            "evidence_segment": case.evidence_segment,
            "cta_type": (
                "PRICE_AUTHORIZATION" if case.case_id == "A" else
                "ADVISOR_REVIEW" if case.case_id in {"C", "D"} else "REPORT_ONLY"
            ),
            "evidence_version": case.evidence_version,
            "document_type": case.document_type,
            "executive": case.executive,
            "test_mode": True,
            "delivery_status": "pending_test_send",
        }))
    for query, payload in entries:
        ledger.update_one(
            {**query, "test_mode": True},
            {"$setOnInsert": payload},
            upsert=True,
        )


def _build_email(item: PreparedTestMessage) -> EmailMessage:
    message = EmailMessage()
    message["From"] = Config.GMAIL_USER or ""
    message["To"] = TEST_RECIPIENT
    message["Subject"] = item.subject
    message.set_content(item.text)
    message.add_alternative(item.html, subtype="html")
    validate_recipient_envelope(
        to=message.get_all("To", []),
        cc=message.get_all("Cc", []),
        bcc=message.get_all("Bcc", []),
        envelope_recipients=[TEST_RECIPIENT],
    )
    return message


def send_test_messages(
    cases: Sequence[OwnerCampaignTestCase],
    *,
    render_case: Callable[[OwnerCampaignTestCase], Mapping[str, Any]],
    db: Any = None,
    smtp_factory: Callable[..., Any] | None = None,
) -> list[dict[str, str]]:
    """Send only 1–4 fully validated cases to the fixed test inbox.

    This function is intentionally not called by startup, a campaign route, or
    a batch query. Callers must supply the explicit A–D case objects.
    """
    if not test_mode_enabled():
        raise TestSenderError("test_mode_disabled")
    if not Config.GMAIL_USER or not Config.GMAIL_PASSWORD:
        raise TestSenderError("test_smtp_credentials_unavailable")
    prepared = prepare_test_messages(cases, render_case=render_case)
    database = db
    if database is None:
        from chatbot.storage import get_db

        database = get_db()
    # Every render and recipient check completes before ledger changes or SMTP.
    _write_test_ledger_entries(database, prepared)
    smtp_factory = smtp_factory or smtplib.SMTP
    results: list[dict[str, str]] = []
    with smtp_factory("smtp.gmail.com", 587, timeout=30) as server:
        server.starttls()
        server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
        for item in prepared:
            message = _build_email(item)
            server.sendmail(Config.GMAIL_USER, [TEST_RECIPIENT], message.as_string())
            database[TEST_LEDGER_COLLECTION].update_one(
                {
                    "campaign_id": TEST_CAMPAIGN_ID,
                    "property_code": item.case.property_code,
                    "test_mode": True,
                    "actual_recipient_email": TEST_RECIPIENT,
                },
                {"$set": {"delivery_status": "test_sent"}},
            )
            results.append({"case_id": item.case.case_id, "property_code": item.case.property_code, "status": "sent_to_test_recipient"})
    return results
