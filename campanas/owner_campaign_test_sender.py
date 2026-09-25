"""SMTP sender isolated to five explicit owner-campaign E2E cases.

There is intentionally no recipient parameter, campaign query, batch lookup, or
mass-send path. Every message is addressed to the fixed test mailbox only.
"""

from __future__ import annotations

import math
import re
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, quote, urlsplit

from config import Config

from .test_mode import (
    ACCEPT_PRICE_ACTION,
    ADVISOR_ACTION,
    REPORT_ACTION,
    TEST_CAMPAIGN_ID,
    TEST_CAMPAIGN_VERSION,
    TEST_RECIPIENT,
    issue_campaign_test_token,
    test_mode_enabled,
    verify_campaign_test_token,
)


SERVICE_BASE_URL = "https://procasa-chatbot-yr8d.onrender.com"
MAX_TEST_EMAILS = 5
TEST_CASE_IDS = frozenset({"A", "B", "C", "D", "E"})
TEST_LEDGER_COLLECTION = "ajuste_precio"
TEST_RUN_ID = "owner_campaign_email_AE_20260924_v1"
PROPERTY_CODE_RE = re.compile(r"^[0-9]{1,32}$")
REQUIRED_RENDER_CHECKS = frozenset(
    {
        "real_property_image",
        "executive_name",
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
    cta_type: str = "REPORT_ONLY"
    raw_recommended_price: float | None = None
    display_recommended_price: float | None = None
    adjustment_pct: float | None = None
    evidence_version: str = "cluster_v2_20260921"
    commune: str = ""
    property_type: str = ""
    render_context: Mapping[str, Any] = field(default_factory=dict)
    portfolio_cases: tuple["OwnerCampaignTestCase", ...] = ()


@dataclass(frozen=True)
class PreparedTestMessage:
    case: OwnerCampaignTestCase
    subject: str
    html: str
    text: str
    report_token: str | None
    action_token: str | None
    property_cases: tuple[OwnerCampaignTestCase, ...] = ()


def _property_cases(case: OwnerCampaignTestCase) -> tuple[OwnerCampaignTestCase, ...]:
    return case.portfolio_cases if case.case_id == "E" else (case,)


def build_test_links(case: OwnerCampaignTestCase) -> dict[str, str]:
    """Return signed test-only report/action links for one explicit test case."""
    if not isinstance(case, OwnerCampaignTestCase):
        raise TestSenderError("explicit_test_case_required")
    links: dict[str, str] = {}
    if case.document_type in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}:
        report_token = issue_campaign_test_token(
            property_code=case.property_code,
            action=REPORT_ACTION,
            document_type=case.document_type,
        )
        links["report"] = f"{SERVICE_BASE_URL}/campana/informe?token={quote(report_token, safe='')}"
    action = {
        "PRICE_AUTHORIZATION": ACCEPT_PRICE_ACTION,
        "ADVISOR_REVIEW": ADVISOR_ACTION,
    }.get(case.cta_type)
    if action:
        action_token = issue_campaign_test_token(property_code=case.property_code, action=action)
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


def _action_for_case(case: OwnerCampaignTestCase) -> str | None:
    return {
        "PRICE_AUTHORIZATION": ACCEPT_PRICE_ACTION,
        "ADVISOR_REVIEW": ADVISOR_ACTION,
    }.get(case.cta_type)


def validate_explicit_cases(cases: Sequence[OwnerCampaignTestCase]) -> list[OwnerCampaignTestCase]:
    if not isinstance(cases, (list, tuple)) or any(not isinstance(case, OwnerCampaignTestCase) for case in cases):
        raise TestSenderError("explicit_test_cases_required")
    if not 1 <= len(cases) <= MAX_TEST_EMAILS:
        raise TestSenderError("test_case_count_invalid")
    ids = [case.case_id for case in cases]
    flattened = [item for case in cases for item in _property_cases(case)]
    codes = [str(case.property_code) for case in flattened]
    if (
        len(ids) != len(set(ids))
        or len(codes) != len(set(codes))
        or any(case_id not in TEST_CASE_IDS for case_id in ids)
    ):
        raise TestSenderError("test_case_ids_invalid")
    for case in cases:
        if case.case_id == "E":
            portfolio = _property_cases(case)
            if (
                len(portfolio) < 3
                or any(not item.intended_owner_email.strip() for item in portfolio)
                or len({item.intended_owner_email.strip().casefold() for item in portfolio}) != 1
            ):
                raise TestSenderError("test_case_e_portfolio_invalid")
            if any(item.case_id != "E" or item.cta_type not in {"PRICE_AUTHORIZATION", "ADVISOR_REVIEW"} for item in portfolio):
                raise TestSenderError("test_case_e_property_cta_invalid")
            if any(item.executive != portfolio[0].executive for item in portfolio):
                raise TestSenderError("test_case_e_executive_mismatch")
            for item in portfolio:
                _validate_case_identity(item)
            continue
        _validate_case_identity(case)
    for case in cases:
        if case.case_id == "A":
            if (
                case.property_code != "5641"
                or case.operation != "VENTA"
                or case.evidence_segment != "PRICE_AUTHORIZATION_READY"
                or case.cta_type != "PRICE_AUTHORIZATION"
                or case.document_type != "INDIVIDUAL_APPRAISAL"
                or not 2 <= _reduction_pct(case) <= 10
            ):
                raise TestSenderError("test_case_a_contract_invalid")
        elif case.case_id == "B":
            if case.property_code != "16521" or case.operation != "VENTA" or case.evidence_segment != "MIXED_EVIDENCE" or case.cta_type != "ADVISOR_REVIEW":
                raise TestSenderError("test_case_b_contract_invalid")
        elif case.case_id == "C":
            if case.property_code != "16486" or case.evidence_segment != "INSUFFICIENT_EVIDENCE" or case.cta_type != "ADVISOR_REVIEW":
                raise TestSenderError("test_case_c_contract_invalid")
        elif case.case_id == "D":
            if case.property_code != "16527" or case.operation != "ARRIENDO":
                raise TestSenderError("test_case_d_contract_invalid")
        elif case.case_id == "E":
            continue
    return list(cases)


def _reduction_pct(case: OwnerCampaignTestCase) -> float:
    try:
        return (float(case.current_price) - float(case.display_recommended_price)) / float(case.current_price) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return -1.0


def _validate_case_identity(case: OwnerCampaignTestCase) -> None:
    if (
        not PROPERTY_CODE_RE.fullmatch(str(case.property_code or ""))
        or not str(case.executive or "").strip()
        or not _finite_positive(case.current_price)
        or case.cta_type not in {"PRICE_AUTHORIZATION", "ADVISOR_REVIEW", "REPORT_ONLY"}
    ):
        raise TestSenderError("test_case_identity_invalid")
    if case.cta_type == "PRICE_AUTHORIZATION" and (
        case.evidence_segment != "PRICE_AUTHORIZATION_READY"
        or not _finite_positive(case.raw_recommended_price)
        or not _finite_positive(case.display_recommended_price)
        or float(case.raw_recommended_price) >= float(case.current_price)
        or float(case.display_recommended_price) >= float(case.current_price)
    ):
        raise TestSenderError("price_authorization_case_not_supported")


def _token_from_link(href: str, expected_path: str) -> dict[str, Any] | None:
    parsed = urlsplit(href)
    if parsed.path.rstrip("/") != expected_path:
        return None
    values = parse_qs(parsed.query, keep_blank_values=True)
    if set(values) != {"token"} or len(values["token"]) != 1:
        raise TestSenderError("test_link_query_invalid")
    claims = verify_campaign_test_token(values["token"][0])
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
    property_cases = _property_cases(case)
    content = (subject + "\n" + html + "\n" + text).casefold()
    if any(
        item.intended_owner_email.strip()
        and item.intended_owner_email.strip().casefold() in content
        for item in property_cases
    ):
        raise TestSenderError("intended_owner_email_leaked")

    if case.case_id == "D":
        rent_checks = {"rental_uf_per_month", "rental_estimate", "rental_comparables"}
        if checks.get("sale_fields_present") is not False or any(checks.get(key) is not True for key in rent_checks):
            raise TestSenderError("test_case_d_sale_or_rent_check_failed")

    parser = _LinkParser()
    parser.feed(html)
    report_tokens: dict[str, str] = {}
    action_tokens: dict[str, str] = {}
    expected_host = urlsplit(SERVICE_BASE_URL).netloc
    for href in parser.hrefs:
        parsed = urlsplit(href)
        path = parsed.path.rstrip("/")
        if "localhost" in href.casefold() or "127.0.0.1" in href:
            raise TestSenderError("localhost_link_forbidden")
        if path.endswith("/campana/respuesta") or path == "/campana/respuesta":
            raise TestSenderError("legacy_cta_forbidden")
        if path.endswith("/campana/informe"):
            if parsed.scheme != "https" or parsed.netloc != expected_host:
                raise TestSenderError("public_report_link_invalid")
            claims = _token_from_link(href, path)
            code = str(claims.get("property_code") or "")
            matching = [item for item in property_cases if item.property_code == code]
            if len(matching) != 1 or claims.get("action") != REPORT_ACTION or claims.get("document_type") != matching[0].document_type:
                raise TestSenderError("test_report_link_mismatch")
            token_value = parse_qs(parsed.query)["token"][0]
            if code in report_tokens and report_tokens[code] != token_value:
                raise TestSenderError("conflicting_test_report_link")
            report_tokens[code] = token_value
        elif path.endswith("/campana/test-accion"):
            if parsed.scheme != "https" or parsed.netloc != expected_host:
                raise TestSenderError("public_action_link_invalid")
            claims = _token_from_link(href, path)
            code = str(claims.get("property_code") or "")
            matching = [item for item in property_cases if item.property_code == code]
            if len(matching) != 1 or claims.get("action") != _action_for_case(matching[0]):
                raise TestSenderError("test_action_link_mismatch")
            token_value = parse_qs(parsed.query)["token"][0]
            if code in action_tokens and action_tokens[code] != token_value:
                raise TestSenderError("conflicting_test_action_link")
            action_tokens[code] = token_value

    for property_case in property_cases:
        if property_case.document_type in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"} and property_case.property_code not in report_tokens:
            raise TestSenderError("test_report_link_missing")
        if _action_for_case(property_case) and property_case.property_code not in action_tokens:
            raise TestSenderError("test_action_link_missing")
    if case.case_id == "E" and (len(action_tokens) != len(property_cases) or len(report_tokens) != len(property_cases)):
        raise TestSenderError("test_case_e_property_links_incomplete")

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
        subject=f"[TEST PROCASA] {subject.removeprefix('[TEST PROCASA]').strip()}",
        html=html,
        text=text,
        report_token=report_tokens.get(case.property_code),
        action_token=action_tokens.get(case.property_code),
        property_cases=property_cases,
    )


def prepare_test_messages(
    cases: Sequence[OwnerCampaignTestCase],
    *,
    render_case: Callable[[OwnerCampaignTestCase], Mapping[str, Any]] | None = None,
) -> list[PreparedTestMessage]:
    """Render and validate every explicit case before any SMTP connection."""
    validated_cases = validate_explicit_cases(cases)
    renderer = render_case or render_owner_campaign_v2_test_case
    prepared = [_validate_rendered_case(case, renderer(case)) for case in validated_cases]
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
        for case in item.property_cases or _property_cases(item.case):
            query = {"campaign_id": TEST_CAMPAIGN_ID, "property_code": case.property_code}
            existing = ledger.find_one(query)
            if existing:
                if existing.get("test_mode") is not True or existing.get("actual_recipient_email") != TEST_RECIPIENT:
                    raise TestSenderError("non_test_ledger_collision")
                raise TestSenderError("test_campaign_case_already_registered")
            entries.append((query, {
                "campaign_id": TEST_CAMPAIGN_ID,
                "test_run_id": TEST_RUN_ID,
                "campaign_version": TEST_CAMPAIGN_VERSION,
                "property_code": case.property_code,
                "intended_owner_email": case.intended_owner_email.strip(),
                "actual_recipient_email": TEST_RECIPIENT,
                "recipient": TEST_RECIPIENT,
                "operation": case.operation,
                "current_price_at_send": case.current_price,
                "raw_recommended_price": case.raw_recommended_price,
                "display_recommended_price": case.display_recommended_price,
                "adjustment_pct": case.adjustment_pct,
                "evidence_segment": case.evidence_segment,
                "cta_type": case.cta_type,
                "evidence_version": case.evidence_version,
                "document_type": case.document_type,
                "executive": case.executive,
                "test_mode": True,
                "delivery_status": "pending_test_send",
            }))
    for query, payload in entries:
        deterministic_id = f"{TEST_CAMPAIGN_ID}:{payload['property_code']}"
        result = ledger.update_one(
            {"_id": deterministic_id},
            {"$setOnInsert": payload},
            upsert=True,
        )
        if getattr(result, "upserted_id", None) is None:
            # A concurrent runner already created this test row. Do not send a
            # second message for the same campaign/property pair.
            raise TestSenderError("test_campaign_case_already_registered")


def _build_email(item: PreparedTestMessage) -> EmailMessage:
    message = EmailMessage()
    message["From"] = Config.GMAIL_USER or ""
    message["To"] = TEST_RECIPIENT
    message["Subject"] = item.subject
    # SMTP does not expose Gmail's internal provider message id. Keep a
    # standards-compliant Message-ID for correlation without mislabeling it.
    from email.utils import make_msgid

    message["Message-ID"] = make_msgid(domain="procasa.cl")
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
    render_case: Callable[[OwnerCampaignTestCase], Mapping[str, Any]] | None = None,
    db: Any = None,
    smtp_factory: Callable[..., Any] | None = None,
) -> list[dict[str, str]]:
    """Send only up to five fully validated cases to the fixed test inbox.

    This function is intentionally not called by startup, a campaign route, or
    a batch query. Callers must supply the explicit A–E case objects.
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
            refused = server.sendmail(Config.GMAIL_USER, [TEST_RECIPIENT], message.as_string())
            if refused:
                refused_at = datetime.now(timezone.utc)
                for property_case in item.property_cases or _property_cases(item.case):
                    database[TEST_LEDGER_COLLECTION].update_one(
                        {
                            "campaign_id": TEST_CAMPAIGN_ID,
                            "property_code": property_case.property_code,
                            "test_mode": True,
                            "actual_recipient_email": TEST_RECIPIENT,
                        },
                        {"$set": {
                            "delivery_status": "test_recipient_refused",
                            "smtp_accepted": False,
                            "recipient": TEST_RECIPIENT,
                            "smtp_attempted_at": refused_at,
                        }},
                    )
                raise TestSenderError("test_recipient_refused_by_smtp")
            property_codes = []
            sent_at = datetime.now(timezone.utc)
            rfc_message_id = str(message["Message-ID"])
            for property_case in item.property_cases or _property_cases(item.case):
                database[TEST_LEDGER_COLLECTION].update_one(
                    {
                        "campaign_id": TEST_CAMPAIGN_ID,
                        "property_code": property_case.property_code,
                        "test_mode": True,
                        "actual_recipient_email": TEST_RECIPIENT,
                    },
                    {"$set": {
                        "delivery_status": "test_sent",
                        "smtp_accepted": True,
                        "rfc_message_id": rfc_message_id,
                        "sent_at": sent_at,
                        "recipient": TEST_RECIPIENT,
                    }},
                )
                property_codes.append(property_case.property_code)
            results.append({
                "case_id": item.case.case_id,
                "property_code": item.case.property_code,
                "property_codes": ",".join(property_codes),
                "status": "sent_to_test_recipient",
                "delivery_status": "accepted_by_smtp_relay",
                "smtp_accepted": True,
                "rfc_message_id": rfc_message_id,
                "sent_at": sent_at.isoformat(),
                "recipient": TEST_RECIPIENT,
            })
    return results


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)


def _valid_activity(activity: Mapping[str, Any]) -> tuple[bool, bool]:
    state = str(activity.get("state") or "UNKNOWN").upper()
    if state == "UNKNOWN":
        consistent = (
            activity.get("total_leads") is None
            and activity.get("conversations") is None
            and activity.get("visits") is None
            and not activity.get("portals")
        )
        return True, consistent
    if state == "KNOWN_ZERO":
        consistent = (
            activity.get("total_leads") == 0
            and activity.get("conversations") == 0
            and activity.get("visits") == 0
            and not activity.get("portals")
        )
        return True, consistent
    if state != "KNOWN_POSITIVE":
        return False, False
    portals = activity.get("portals") or []
    try:
        total = int(activity.get("total_leads"))
        portal_total = sum(int(item.get("count")) for item in portals if isinstance(item, Mapping))
        consistent = total > 0 and portal_total == total and all(int(item.get("count")) > 0 for item in portals)
    except (TypeError, ValueError):
        consistent = False
    return True, consistent


def _render_portfolio_case(case: OwnerCampaignTestCase) -> Mapping[str, Any]:
    """Render E as one compact V2 portfolio message with per-property links."""
    executives = case.render_context.get("executives")
    if not isinstance(executives, list) or len(executives) != 1:
        raise TestSenderError("test_case_e_single_executive_required")
    models: list[dict[str, Any]] = []
    activity_checks: list[bool] = []
    portal_checks: list[bool] = []
    for property_case in _property_cases(case):
        context = property_case.render_context
        source_checks = context.get("source_checks")
        model = context.get("property_model")
        if not isinstance(source_checks, Mapping) or not isinstance(model, Mapping):
            raise TestSenderError("approved_view_model_required")
        if any(source_checks.get(key) is not True for key in {
            "real_property_image", "executive_name",
            "activity_90d", "portal_breakdown_valid", "reference_correct",
            "comparables_compatible", "own_listing_excluded", "cta_correct",
        }):
            raise TestSenderError("approved_evidence_check_failed")
        item = dict(model)
        if str(item.get("code") or "") != property_case.property_code:
            raise TestSenderError("view_model_property_mismatch")
        if str(item.get("operation_raw") or "").strip().upper() != property_case.operation:
            raise TestSenderError("view_model_operation_mismatch")
        if bool(item.get("is_rental")) != (property_case.operation == "ARRIENDO"):
            raise TestSenderError("view_model_rental_flag_mismatch")
        if not isinstance(item.get("image"), Mapping) or item["image"].get("available") is not True:
            raise TestSenderError("real_property_image_required")
        if not isinstance(item.get("executive"), Mapping):
            raise TestSenderError("resolved_executive_required")
        executive = item["executive"]
        if not str(executive.get("name") or "").strip():
            raise TestSenderError("resolved_executive_required")
        if str(executive.get("name") or "").strip() != property_case.executive.strip():
            raise TestSenderError("view_model_executive_mismatch")
        if not all(str(executive.get(key) or "").strip() == str(executives[0].get(key) or "").strip() for key in ("name", "email", "phone")):
            raise TestSenderError("test_case_e_executive_mismatch")
        if str((item.get("document") or {}).get("type") or "NONE").upper() != property_case.document_type.upper():
            raise TestSenderError("view_model_document_mismatch")
        if property_case.document_type not in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}:
            raise TestSenderError("test_case_e_report_required")
        links = build_test_links(property_case)
        cta = dict(item.get("cta") or {})
        label = str(cta.get("primary_label") or "").casefold()
        if property_case.cta_type == "PRICE_AUTHORIZATION":
            if "aceptar nuevo valor" not in label:
                raise TestSenderError("approved_price_cta_label_missing")
            target = property_case.display_recommended_price
            if not _finite_positive(target) or float(target) >= property_case.current_price:
                raise TestSenderError("price_authorization_case_not_supported")
            target_label = str(item.get("recommended_price_label") or "")
            match = re.search(r"([0-9][0-9.,]*)", target_label)
            if not match:
                raise TestSenderError("view_model_recommended_price_missing")
            parsed = match.group(1).replace(".", "").replace(",", ".")
            if not math.isclose(float(parsed), float(target), rel_tol=.001, abs_tol=.05):
                raise TestSenderError("view_model_recommended_price_mismatch")
        elif "revisar" not in label or "asesor" not in label:
            raise TestSenderError("approved_advisor_cta_label_missing")
        cta["primary_url"] = links["action"]
        cta["secondary_url"] = links["report"]
        item["cta"] = cta
        item_activity = item.get("activity_90d") if isinstance(item.get("activity_90d"), Mapping) else {}
        valid_activity, valid_portals = _valid_activity(item_activity)
        activity_checks.append(valid_activity)
        portal_checks.append(valid_portals)
        models.append(item)

    from analytics.owner_campaign_email_v2 import render_owner_campaign_email_v2

    html = render_owner_campaign_email_v2(
        models,
        email=TEST_RECIPIENT,
        executives=[dict(executives[0])],
        base_url=SERVICE_BASE_URL,
    )
    text_parser = _VisibleTextParser()
    text_parser.feed(html)
    text = "\n".join(text_parser.parts)
    sale_terms = ("compradores", "comprador", "tasación", "precio de cierre", "precio final de venta", "subsidio", "publicación de venta")
    return {
        "subject": "Seguimiento comercial PROCASA · Resumen de cartera",
        "html": html,
        "text": text,
        "checks": {
            "real_property_image": True,
            "executive_name": True,
            "executive_email": True,
            "executive_phone": True,
            "activity_90d": all(activity_checks),
            "portal_breakdown_valid": all(portal_checks),
            "operation_correct": True,
            "price_correct": True,
            "reference_correct": True,
            "comparables_compatible": True,
            "own_listing_excluded": True,
            "cta_correct": True,
            "sale_fields_present": any(term in text.casefold() for term in sale_terms) if all(item.operation == "ARRIENDO" for item in _property_cases(case)) else False,
        },
    }


def render_owner_campaign_v2_test_case(case: OwnerCampaignTestCase) -> Mapping[str, Any]:
    """Render a prevalidated V3 view model through the exact approved V2 renderer.

    This adapter only injects per-property signed test URLs. All price, evidence,
    executive, activity, comparable, and visual fields come from the supplied
    approved property model unchanged.
    """
    if not isinstance(case, OwnerCampaignTestCase):
        raise TestSenderError("explicit_test_case_required")
    if case.case_id == "E":
        return _render_portfolio_case(case)
    context = case.render_context
    model = context.get("property_model")
    executives = context.get("executives")
    source_checks = context.get("source_checks")
    if not isinstance(model, Mapping) or not isinstance(executives, list) or not executives:
        raise TestSenderError("approved_view_model_required")
    if not isinstance(source_checks, Mapping):
        raise TestSenderError("approved_evidence_checks_required")
    property_model = dict(model)
    if str(property_model.get("code") or "") != case.property_code:
        raise TestSenderError("view_model_property_mismatch")
    if str(property_model.get("operation_raw") or "").strip().upper() != case.operation:
        raise TestSenderError("view_model_operation_mismatch")
    expected_rent = case.operation == "ARRIENDO"
    if bool(property_model.get("is_rental")) != expected_rent:
        raise TestSenderError("view_model_rental_flag_mismatch")
    if str(property_model.get("recommendation") or "") != (
        "con ajuste de precio sustentado" if case.cta_type == "PRICE_AUTHORIZATION" else
        "revisión con asesor" if case.cta_type == "ADVISOR_REVIEW" else
        str(property_model.get("recommendation") or "")
    ):
        raise TestSenderError("view_model_recommendation_mismatch")

    executive = property_model.get("executive") if isinstance(property_model.get("executive"), Mapping) else {}
    image = property_model.get("image") if isinstance(property_model.get("image"), Mapping) else {}
    document = property_model.get("document") if isinstance(property_model.get("document"), Mapping) else {}
    activity = property_model.get("activity_90d") if isinstance(property_model.get("activity_90d"), Mapping) else {}
    comparable = property_model.get("comparable") if isinstance(property_model.get("comparable"), Mapping) else {}
    activity_valid, portal_valid = _valid_activity(activity)
    if not image.get("available") or not str(image.get("url") or "").startswith(("https://", "http://")) or image.get("source") not in {"UNIVERSO_CARTERA", "PROCASA_PUBLICATION"}:
        raise TestSenderError("real_property_image_required")
    if not executive.get("name"):
        raise TestSenderError("resolved_executive_required")
    if str(executive.get("name")).strip() != case.executive.strip():
        raise TestSenderError("view_model_executive_mismatch")
    if not any(
        isinstance(item, Mapping)
        and str(item.get("name") or "").strip() == str(executive.get("name") or "").strip()
        and str(item.get("email") or "").strip().casefold() == str(executive.get("email") or "").strip().casefold()
        and str(item.get("phone") or "").strip() == str(executive.get("phone") or "").strip()
        for item in executives
    ):
        raise TestSenderError("rendered_executive_mismatch")
    if str(document.get("type") or "NONE").upper() != case.document_type.upper():
        raise TestSenderError("view_model_document_mismatch")

    source_check_keys = {
        "real_property_image", "executive_name",
        "reference_correct", "comparables_compatible", "own_listing_excluded", "cta_correct",
    }
    if any(source_checks.get(key) is not True for key in source_check_keys):
        raise TestSenderError("approved_evidence_check_failed")
    price_text = str(property_model.get("price_label") or "")
    price_match = re.search(r"([0-9][0-9.,]*)", price_text)
    if not price_match:
        raise TestSenderError("view_model_price_missing")
    rendered_price = price_match.group(1).replace(".", "").replace(",", ".")
    try:
        price_correct = math.isclose(float(rendered_price), float(case.current_price), rel_tol=0.001, abs_tol=0.05)
    except (TypeError, ValueError):
        price_correct = False
    if not price_correct:
        raise TestSenderError("view_model_price_mismatch")
    if case.cta_type == "PRICE_AUTHORIZATION":
        target_match = re.search(r"([0-9][0-9.,]*)", str(property_model.get("recommended_price_label") or ""))
        if not target_match:
            raise TestSenderError("view_model_recommended_price_missing")
        target_text = target_match.group(1).replace(".", "").replace(",", ".")
        if not math.isclose(float(target_text), float(case.display_recommended_price), rel_tol=0.001, abs_tol=0.05):
            raise TestSenderError("view_model_recommended_price_mismatch")

    links = build_test_links(case)
    cta = dict(property_model.get("cta") or {})
    label = str(cta.get("primary_label") or "").casefold()
    if case.cta_type == "PRICE_AUTHORIZATION" and "aceptar nuevo valor" not in label:
        raise TestSenderError("approved_price_cta_label_missing")
    if case.cta_type == "ADVISOR_REVIEW" and ("revisar" not in label or "asesor" not in label):
        raise TestSenderError("approved_advisor_cta_label_missing")
    if case.cta_type == "PRICE_AUTHORIZATION":
        cta["primary_url"] = links["action"]
    elif case.cta_type == "ADVISOR_REVIEW":
        cta["primary_url"] = links["action"]
    if "report" in links:
        cta["secondary_url"] = links["report"]
    property_model["cta"] = cta

    from analytics.owner_campaign_email_v2 import render_owner_campaign_email_v2

    html = render_owner_campaign_email_v2(
        [property_model],
        email=TEST_RECIPIENT,
        executives=[dict(item) for item in executives],
        base_url=SERVICE_BASE_URL,
    )
    text_parser = _VisibleTextParser()
    text_parser.feed(html)
    text = "\n".join(text_parser.parts)
    lower_text = text.casefold()
    sale_terms = ("compradores", "comprador", "tasación", "precio de cierre", "precio final de venta", "subsidio", "publicación de venta")
    rental_checks = {
        "rental_uf_per_month": expected_rent and ("/mes" in price_text.casefold() or "/ mes" in price_text.casefold()),
        "rental_estimate": expected_rent and bool((property_model.get("appraisal") or {}).get("visible")),
        "rental_comparables": expected_rent and bool(comparable.get("visible")) and any("UF/m²/mes" in str(item.get("unit_label") or "") for item in comparable.get("top3") or []),
        "sale_fields_present": any(term in lower_text for term in sale_terms) if expected_rent else False,
    }
    checks = {
        "real_property_image": True,
        "executive_name": True,
        "executive_email": bool(executive.get("email")),
        "executive_phone": bool(executive.get("phone")),
        "activity_90d": activity_valid,
        "portal_breakdown_valid": portal_valid,
        "operation_correct": True,
        "price_correct": True,
        "reference_correct": source_checks.get("reference_correct") is True,
        "comparables_compatible": source_checks.get("comparables_compatible") is True,
        "own_listing_excluded": source_checks.get("own_listing_excluded") is True,
        "cta_correct": source_checks.get("cta_correct") is True,
        **rental_checks,
    }
    return {
        "subject": "Seguimiento comercial PROCASA",
        "html": html,
        "text": text,
        "checks": checks,
    }
