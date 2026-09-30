"""Reusable production sender for owner campaign manifests.

The default mode is a read-only DRY_RUN. SMTP requires an explicit CLI flag,
environment gate, and batch-specific confirmation string.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import math
import os
import re
import smtplib
import tempfile
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

from config import Config
from campanas.owner_campaign_live_config import (
    BOSS_CC,
    EXCLUDED_OWNER_EMAILS,
    MANUAL_CAMPAIGN_EXCLUDED_CODES,
    MANUAL_RECENT_EXCLUSION_CODES,
    PRODUCTION_CAMPAIGN_ID,
    WAVE2_AMBIGUOUS_OWNER_CODES,
    WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID,
)


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path(os.getenv(
    "OWNER_CAMPAIGN_MANIFEST_PATH",
    str(BASE_DIR / "reports" / "owner_campaign_sucre_wave1_20260928_manifest.csv"),
))
MAX_HTML_BYTES = 2_000_000
REQUIRED_COLUMNS = {
    "batch_id", "campaign_id", "property_code", "owner_email", "operation_resolved",
    "executive_name", "executive_email", "boss_cc", "current_price",
    "recommended_adjustment_pct", "recommended_price", "document_type", "send_status",
}
LIVE_ACTIONS = {
    "primary": "aceptar_rebaja",
    "advisor": "contactar_ejecutivo",
    "report": "ver_informe",
}
EMAIL_SUBJECT_BASE = "Seguimiento comercial PROCASA"
EMAIL_SUBJECT_OFFICE = os.getenv("OWNER_CAMPAIGN_OFFICE", "PROCASA SUCRE").strip()
SMTP_CONNECT_TIMEOUT_SECONDS = 30
SMTP_SEND_TIMEOUT_SECONDS = 60
_SMTP_LOGGER = logging.getLogger("owner_campaign.smtp")


class SenderError(RuntimeError):
    """Safe-to-report sender validation or delivery error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def normalize_property_code(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"[.\s]", "", raw)
    return compact if compact.isdigit() else raw.casefold()


def _compose_subject(base_subject: str, office: str, property_code: Any) -> str:
    """Append campaign office and property identity without changing the base."""
    base = str(base_subject or "").strip()
    office_label = str(office or "").strip()
    code = str(property_code or "").strip()
    if not base or not office_label or not code or any(ch in code for ch in "\r\n"):
        raise SenderError("subject_identity_invalid")
    return f"{base} · {office_label} · {code}"


def _subject_matches_property(subject: str, property_code: Any, office: str = EMAIL_SUBJECT_OFFICE) -> bool:
    expected = _compose_subject(EMAIL_SUBJECT_BASE, office, property_code)
    return str(subject or "").strip() == expected


def _allows_multiowner_single_property_email(campaign_id: str) -> bool:
    """Wave 2 explicitly sends one independent email per property."""
    return str(campaign_id or "").strip() == WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID


def _owner_property_count_allowed(campaign_id: str, count: int) -> bool:
    return count == 1 or _allows_multiowner_single_property_email(campaign_id)


def _wave2_code_is_ambiguous(campaign_id: str, property_code: Any) -> bool:
    return (
        _allows_multiowner_single_property_email(campaign_id)
        and normalize_property_code(property_code) in WAVE2_AMBIGUOUS_OWNER_CODES
    )


def _wave2_ready_rows(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    if any(str(item.get("send_status") or "").upper() == "SENDING" for item in records):
        raise SenderError("campaign_batch_contains_sending_rows")
    return [item for item in records if str(item.get("send_status") or "").upper() == "READY"]


def _valid_email(value: Any) -> str | None:
    email = str(value or "").strip().casefold()
    if not email or len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return None
    return email


def _safe_validation_reason(exc: Exception) -> str:
    code = getattr(exc, "code", None) or getattr(exc, "error_code", None)
    if code:
        return str(code)
    # These are fixed validation codes emitted by issue_live_token; never echo
    # arbitrary exception text because it may contain deployment details.
    safe_codes = {"production_action_token_not_configured", "invalid_live_document_type"}
    if isinstance(exc, ValueError) and str(exc) in safe_codes:
        return str(exc)
    return f"validation_failed:{type(exc).__name__}"


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _same_number(left: Any, right: Any, *, tolerance: float = 0.05) -> bool:
    a, b = _number(left), _number(right)
    return a is not None and b is not None and math.isclose(a, b, rel_tol=0.0005, abs_tol=tolerance)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.text_parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"style", "script", "head", "title"}:
            self._skip_depth += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(html.unescape(href))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"style", "script", "head", "title"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.text_parts.append(data.strip())


def _read_manifest(path: str | Path) -> tuple[list[dict[str, str]], str, str]:
    manifest_path = Path(path)
    with manifest_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - fields
        if missing:
            raise SenderError("manifest_required_fields_missing")
        rows = list(reader)
    if not rows:
        raise SenderError("manifest_empty")
    campaign_ids = {str(row.get("campaign_id") or "").strip() for row in rows}
    batch_ids = {str(row.get("batch_id") or "").strip() for row in rows}
    if len(campaign_ids) != 1 or "" in campaign_ids or len(batch_ids) != 1 or "" in batch_ids:
        raise SenderError("manifest_campaign_or_batch_inconsistent")
    campaign_id, batch_id = next(iter(campaign_ids)), next(iter(batch_ids))
    allowed_campaigns = {PRODUCTION_CAMPAIGN_ID, WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID}
    if campaign_id not in allowed_campaigns or not campaign_id or "test" in campaign_id.casefold():
        raise SenderError("manifest_campaign_mismatch")
    codes = [normalize_property_code(row.get("property_code")) for row in rows]
    owners = [str(row.get("owner_email") or "").strip().casefold() for row in rows]
    if any(not code for code in codes) or len(codes) != len(set(codes)):
        raise SenderError("manifest_duplicate_or_invalid_property")
    if any(not owner for owner in owners) or (
        not _allows_multiowner_single_property_email(campaign_id) and len(owners) != len(set(owners))
    ):
        raise SenderError("manifest_duplicate_or_invalid_owner")
    if any(str(row.get("send_status") or "").strip().upper() != "READY" for row in rows):
        raise SenderError("manifest_row_not_ready")
    return rows, campaign_id, batch_id


def _recipient_cc(row: Mapping[str, Any], runtime: Any) -> list[str]:
    owner = runtime._valid_owner_email(row.get("owner_email"))
    boss = runtime._valid_owner_email(row.get("boss_cc"))
    executive = runtime._valid_owner_email(row.get("executive_email"))
    if not owner:
        raise SenderError("owner_email_invalid")
    if not boss:
        raise SenderError("boss_cc_invalid")
    if not executive:
        raise SenderError("executive_email_invalid")
    final: list[str] = []
    seen = {owner.casefold()}
    for recipient in (boss, executive):
        normalized = recipient.strip().casefold()
        if normalized not in seen:
            final.append(normalized)
            seen.add(normalized)
    return final


def _verify_rendered_links(
    rendered_html: str,
    *,
    campaign_id: str,
    property_code: str,
    recipient: str,
    document_type: str,
    base_url: str,
    now: datetime,
) -> dict[str, bool]:
    from campanas.owner_campaign_live_events import verify_live_token

    parser = _PageParser()
    parser.feed(rendered_html)
    expected_host = urlsplit(base_url).netloc.casefold()
    found: dict[str, bool] = {"primary": False, "advisor": False, "report": False}
    for href in parser.hrefs:
        parsed = urlsplit(href)
        if parsed.scheme != "https" or parsed.netloc.casefold() != expected_host:
            continue
        query = parse_qs(parsed.query, keep_blank_values=True)
        token_values = query.get("token")
        if not token_values or len(token_values) != 1:
            continue
        token = token_values[0]
        if parsed.path.rstrip("/") == "/campana/respuesta":
            action = str(query.get("accion", [""])[0])
            key = "primary" if action == LIVE_ACTIONS["primary"] else "advisor" if action == LIVE_ACTIONS["advisor"] else ""
            if key:
                claims = verify_live_token(
                    token, campaign_id=campaign_id, property_code=property_code,
                    recipient=recipient, action=action, now=now,
                )
                found[key] = bool(
                    claims
                    and claims.get("test_mode") is False
                    and int(claims.get("exp", 0)) > int(now.timestamp())
                    and claims.get("action") == action
                )
        elif parsed.path.rstrip("/") == "/campana/informe":
            claims = verify_live_token(
                token, campaign_id=campaign_id, property_code=property_code,
                recipient=recipient, action=LIVE_ACTIONS["report"], now=now,
            )
            found["report"] = bool(
                claims
                and claims.get("document_type") == document_type
                and document_type in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}
                and claims.get("test_mode") is False
                and int(claims.get("exp", 0)) > int(now.timestamp())
            )
    return found


def _validate_row(
    manifest_row: Mapping[str, Any], *, db: Any, all_properties: Sequence[Mapping[str, Any]],
    percentile_data: Mapping[str, Any], now: datetime, campaign_id: str, batch_id: str,
) -> dict[str, Any]:
    from campanas import owner_campaign_test_runtime as runtime
    from campanas.owner_campaign_live_config import BOSS_CC
    from analytics.owner_campaign_email_v2 import _clp_price_label, _price_label
    from campanas.owner_campaign_live_prepare import _render_one, _row_for

    code = normalize_property_code(manifest_row.get("property_code"))
    matching = [
        item for item in all_properties
        if normalize_property_code(item.get("codigo")) == code
        and runtime._active_available(item) and runtime._is_sucre(item)
    ]
    if len(matching) != 1:
        raise SenderError("property_missing_or_ambiguous")
    master = matching[0]
    raw_code = str(master.get("codigo") or "").strip()
    raw_operation = runtime.resolve_property_operation(master)
    operation = runtime.resolve_property_operation(master, requested_operation=runtime.VENTA) if raw_operation == runtime.VENTA_ARRIENDO else raw_operation
    if operation not in {runtime.VENTA, runtime.ARRIENDO}:
        raise SenderError("operation_unresolved")

    owner = runtime._email_from_property(master)
    normalized_excluded_codes = {
        normalize_property_code(value)
        for value in (MANUAL_CAMPAIGN_EXCLUDED_CODES | MANUAL_RECENT_EXCLUSION_CODES)
    }
    excluded_emails = {str(value).strip().casefold() for value in EXCLUDED_OWNER_EMAILS}
    if code in normalized_excluded_codes or owner.casefold() in excluded_emails:
        raise SenderError("campaign_exclusion_applies")
    if _wave2_code_is_ambiguous(campaign_id, code):
        raise SenderError("ambiguous_owner_identity_shared_email")

    owner_properties = [
        item for item in all_properties
        if runtime._email_from_property(item, required=False).strip().casefold() == owner.casefold()
    ]
    if not _owner_property_count_allowed(campaign_id, len(owner_properties)):
        raise SenderError("owner_is_not_single_property")

    row = _row_for(db, master, percentile_data, now)
    if row["operation_resolved"] != operation:
        raise SenderError("operation_rebuild_mismatch")
    row.update({"campaign_id": campaign_id, "batch_id": batch_id})
    if row.get("send_status") != "READY":
        raise SenderError("campaign_row_not_ready")
    cc = _recipient_cc(row, runtime)
    if _valid_email(owner) != owner:
        raise SenderError("owner_email_invalid")
    assigned_name = str(runtime._path(master, "estado.ejecutivo") or "").strip()
    if not assigned_name or runtime._fold(assigned_name) != runtime._fold(row.get("executive_name")):
        raise SenderError("executive_property_assignment_mismatch")
    if runtime._fold(BOSS_CC) != runtime._fold(row.get("boss_cc")):
        raise SenderError("boss_cc_configuration_mismatch")

    for field in ("owner_email", "executive_email", "boss_cc", "executive_name", "operation_resolved", "document_type"):
        left = str(manifest_row.get(field) or "").strip().casefold()
        right = str(row.get(field) or "").strip().casefold()
        if left != right:
            raise SenderError(f"manifest_{field}_stale_or_mismatch")
    if normalize_property_code(manifest_row.get("property_code")) != normalize_property_code(raw_code):
        raise SenderError("manifest_property_mismatch")
    if not _same_number(manifest_row.get("current_price"), row.get("current_price")):
        raise SenderError("manifest_current_price_stale_or_mismatch")
    if not _same_number(manifest_row.get("recommended_price"), row.get("recommended_price")):
        raise SenderError("manifest_recommended_price_stale_or_mismatch")
    if not _same_number(manifest_row.get("recommended_adjustment_pct"), row.get("recommended_adjustment_pct"), tolerance=0):
        raise SenderError("manifest_recommendation_stale_or_mismatch")
    adjustment = _number(row.get("recommended_adjustment_pct"))
    current = _number(row.get("current_price"))
    recommended = _number(row.get("recommended_price"))
    if adjustment is None or not 5 <= adjustment <= 10 or current is None or current <= 0 or recommended is None or not 0 < recommended < current:
        raise SenderError("recommended_price_invalid")

    model = row.get("_model") if isinstance(row.get("_model"), Mapping) else {}
    comparable = model.get("comparable") if isinstance(model.get("comparable"), Mapping) else {}
    analysis = master.get("analisis_comparables") if isinstance(master.get("analisis_comparables"), Mapping) else {}
    segment = analysis.get("segmento") if isinstance(analysis.get("segmento"), Mapping) else {}
    if comparable.get("visible") and str(segment.get("operacion") or "").strip().upper() != operation:
        raise SenderError("comparable_operation_mismatch")

    document_type = str(row.get("document_type") or "NONE").upper()
    document = model.get("document") if isinstance(model.get("document"), Mapping) else {}
    if document_type not in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT", "NONE"}:
        raise SenderError("document_type_invalid")
    if bool(document.get("visible")) != (document_type != "NONE"):
        raise SenderError("document_visibility_mismatch")

    gradual_enabled = str(os.getenv("OWNER_CAMPAIGN_GRADUAL_OPTION_ENABLED", "false")).strip().casefold() in {"1", "true", "yes", "on"}
    if gradual_enabled:
        raise SenderError("gradual_option_enabled_for_current_campaign")
    rendered = _render_one(row)
    html_bytes = rendered.encode("utf-8")
    if not rendered.strip() or len(html_bytes) > MAX_HTML_BYTES:
        raise SenderError("render_incomplete_or_too_large")
    parser = _PageParser()
    parser.feed(rendered)
    visible_text = " ".join(parser.text_parts).casefold()
    if "revisar / confirmar ajuste" not in visible_text or "revisar con mi ejecutivo" not in visible_text:
        raise SenderError("required_owner_campaign_cta_missing")
    if "opción gradual" in visible_text or "ajuste gradual" in visible_text or "autorizar ajuste gradual" in visible_text:
        raise SenderError("gradual_option_visible")
    # The renderer's selected display labels are canonical. A CLP equivalent
    # may exist in the data without being shown in the email, so validate the
    # exact labels selected by the V2 model instead of requiring every currency
    # representation to appear to the recipient.
    current_label = str(model.get("price_label") or "")
    recommended_label = str(model.get("recommended_price_label") or "")
    canonical_current_options = {
        _price_label(row.get("current_price"), operation),
        _clp_price_label(row.get("current_price_clp"), operation),
    }
    canonical_recommended_options = {
        _price_label(row.get("recommended_price"), operation),
        _clp_price_label(row.get("recommended_price_clp"), operation),
    }
    if (
        (current_label and current_label not in canonical_current_options)
        or (recommended_label and recommended_label not in canonical_recommended_options)
    ):
        raise SenderError("client_price_formatter_mismatch")
    expected_price_labels = [label for label in (current_label, recommended_label) if label]
    if any(label.casefold() not in visible_text for label in expected_price_labels):
        raise SenderError("client_price_formatter_mismatch")
    raw_price_values = (str(row.get("current_price")), str(row.get("recommended_price")))
    localized_price_values = tuple(label.replace(" ", "") for label in expected_price_labels if label)
    if any(raw and raw in visible_text and not any(raw.replace(".", ",") in item or raw in item for item in localized_price_values) for raw in raw_price_values):
        raise SenderError("unformatted_raw_price_visible")

    base_url = (Config.CRM_BASE_URL or "https://www.procasa.cl").rstrip("/")
    token_results = _verify_rendered_links(
        rendered,
        campaign_id=campaign_id,
        property_code=raw_code,
        recipient=owner,
        document_type=document_type,
        base_url=base_url,
        now=now,
    )
    if not token_results["primary"] or not token_results["advisor"]:
        raise SenderError("action_token_sign_or_verify_failed")
    if (document_type == "NONE" and token_results["report"]) or (document_type != "NONE" and not token_results["report"]):
        raise SenderError("document_token_routing_invalid")

    return {
        "property_code": raw_code,
        "owner_email": owner,
        "owner_name": row.get("owner_name") or "",
        "final_cc_list": cc,
        "operation": operation,
        "executive_name": row.get("executive_name"),
        "executive_email": row.get("executive_email"),
        "boss_cc": row.get("boss_cc"),
        "recommended_adjustment_pct": int(adjustment),
        "current_price": row.get("current_price"),
        "recommended_price": row.get("recommended_price"),
        "document_type": document_type,
        "html": rendered,
        "render_status": "PASS",
        "auth_token_valid": token_results["primary"],
        "advisor_token_valid": token_results["advisor"],
        "document_routing_valid": (
            token_results["report"] if document_type != "NONE"
            else not token_results["report"] and not any("/campana/informe" in href for href in parser.hrefs)
        ),
        "gmail_safe": True,
        "price_valid": True,
        "eligibility_status": "PASS",
        "campaign_id": campaign_id,
        "batch_id": batch_id,
    }


def _ledger_base(row: Mapping[str, Any], campaign_id: str, batch_id: str) -> dict[str, Any]:
    return {
        "campaign_id": campaign_id,
        "batch_id": batch_id,
        "property_code": str(row["property_code"]),
        "owner_email": row["owner_email"],
        "owner_name": row.get("owner_name"),
        "executive_name": row.get("executive_name"),
        "executive_email": row.get("executive_email"),
        "boss_cc": row.get("boss_cc"),
        "operation": row.get("operation"),
        "recommended_adjustment_pct": row.get("recommended_adjustment_pct"),
        "current_price": row.get("current_price"),
        "recommended_price": row.get("recommended_price"),
        "document_type": row.get("document_type"),
        "send_status": "READY",
        "created_at": datetime.now(timezone.utc),
        "events": [],
        "authorization_status": "PENDING",
        "send_attempts": [],
    }


def _claim_send(db: Any, row: Mapping[str, Any], campaign_id: str, batch_id: str, attempt_id: str) -> bool:
    ledger = db[Config.COLLECTION_CAMPANAS_LOG]
    key = f"{campaign_id}:{row['property_code']}"
    query_identity = {"_id": key, "campaign_id": campaign_id, "property_code": str(row["property_code"])}
    try:
        ledger.update_one(query_identity, {"$setOnInsert": _ledger_base(row, campaign_id, batch_id)}, upsert=True)
    except DuplicateKeyError:
        pass
    existing = ledger.find_one(query_identity) or {}
    if (
        str(existing.get("owner_email") or "").strip().casefold() != str(row["owner_email"]).casefold()
        or str(existing.get("executive_email") or "").strip().casefold() != str(row["executive_email"]).casefold()
        or str(existing.get("boss_cc") or "").strip().casefold() != str(row["boss_cc"]).casefold()
    ):
        raise SenderError("campaign_ledger_recipient_mismatch")
    now = datetime.now(timezone.utc)
    attempt = {
        "attempt_id": attempt_id,
        "attempted_at": now,
        "smtp_status": "SENDING",
        "owner_email": row["owner_email"],
        "final_cc_list": list(row["final_cc_list"]),
        "batch_id": batch_id,
        "error_type": None,
        "error_message": None,
    }
    claimed = ledger.update_one(
        {
            **query_identity,
            "send_status": "READY",
            "authorization_status": {"$ne": "PRICE_AUTHORIZED"},
        },
        {
            "$set": {"send_status": "SENDING", "batch_id": batch_id, "last_send_attempt_id": attempt_id},
            "$push": {"send_attempts": attempt},
        },
    )
    return claimed.modified_count == 1


def _finish_attempt(
    db: Any, row: Mapping[str, Any], attempt_id: str, *, status: str,
    message_id: str | None = None, error_type: str | None = None, error_message: str | None = None,
) -> None:
    ledger = db[Config.COLLECTION_CAMPANAS_LOG]
    key = f"{row['campaign_id']}:{row['property_code']}"
    now = datetime.now(timezone.utc)
    update: dict[str, Any] = {
        "send_status": status,
        "last_send_status": status,
        "last_send_updated_at": now,
        "last_send_message_id": message_id,
    }
    if status == "SENT":
        update["sent_at"] = now
        update["last_send_error_type"] = None
        update["last_send_error_message"] = None
    else:
        update["last_send_error_type"] = error_type
        update["last_send_error_message"] = error_message
    result = ledger.update_one(
        {"_id": key, "campaign_id": row["campaign_id"], "property_code": str(row["property_code"]),
         "last_send_attempt_id": attempt_id},
        {
            "$set": {**update, "send_attempts.$[attempt].smtp_status": status,
                     "send_attempts.$[attempt].provider_message_id": None,
                     "send_attempts.$[attempt].message_id": message_id,
                     "send_attempts.$[attempt].sent_at": now if status == "SENT" else None,
                     "send_attempts.$[attempt].error_type": error_type,
                     "send_attempts.$[attempt].error_message": error_message},
        },
        array_filters=[{"attempt.attempt_id": attempt_id}],
    )
    if result.modified_count != 1:
        raise SenderError("send_result_persistence_failed")


def _build_message(row: Mapping[str, Any]) -> tuple[EmailMessage, list[str]]:
    parser = _PageParser()
    parser.feed(str(row["html"]))
    text_body = "\n".join(parser.text_parts)
    if not text_body:
        text_body = "Este mensaje requiere un cliente de correo compatible con HTML."
    message = EmailMessage()
    message["From"] = Config.GMAIL_USER or ""
    message["To"] = row["owner_email"]
    cc = list(row["final_cc_list"])
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = _compose_subject(
        EMAIL_SUBJECT_BASE, EMAIL_SUBJECT_OFFICE, row.get("property_code")
    )
    message["Message-ID"] = make_msgid(domain="procasa.cl")
    message.set_content(text_body)
    message.add_alternative(str(row["html"]), subtype="html")
    if not _subject_matches_property(message["Subject"], row.get("property_code")):
        raise SenderError("subject_property_code_mismatch")
    envelope = [row["owner_email"], *cc]
    if len({item.casefold() for item in envelope}) != len(envelope):
        raise SenderError("smtp_envelope_duplicate_recipient")
    return message, envelope


def _log_smtp_event(
    row: Mapping[str, Any], attempt_id: str, phase: str, *,
    exception: BaseException | None = None, exception_phase: str | None = None,
) -> None:
    """Emit a credential-free, content-free SMTP lifecycle event."""
    record = {
        "property_code": str(row.get("property_code") or ""),
        "send_attempt_id": attempt_id,
        "smtp_phase": phase,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    if exception is not None:
        record["exception_type"] = type(exception).__name__
        record["exception_phase"] = exception_phase or phase
    _SMTP_LOGGER.info("owner_campaign_smtp %s", json.dumps(record, ensure_ascii=False, sort_keys=True))


def _smtp_response_ok(code: Any, expected: int) -> bool:
    try:
        return int(code) == expected
    except (TypeError, ValueError):
        return False


def _send_one(db: Any, row: Mapping[str, Any], campaign_id: str, batch_id: str, smtp_factory: Any = None) -> dict[str, Any]:
    if not Config.GMAIL_USER or not Config.GMAIL_PASSWORD:
        raise SenderError("production_smtp_credentials_unavailable")
    message, envelope = _build_message(row)
    if not _subject_matches_property(message.get("Subject", ""), row.get("property_code")):
        raise SenderError("subject_property_code_mismatch")
    attempt_id = uuid4().hex
    if not _claim_send(db, row, campaign_id, batch_id, attempt_id):
        existing = db[Config.COLLECTION_CAMPANAS_LOG].find_one(
            {"_id": f"{campaign_id}:{row['property_code']}"}, {"send_status": 1}
        ) or {}
        raise SenderError(f"send_claim_unavailable_{str(existing.get('send_status') or 'UNKNOWN').casefold()}")
    smtp_factory = smtp_factory or smtplib.SMTP
    server = None
    phase = "CONNECTING"
    data_started = False
    smtp_accepted = False
    result: dict[str, Any]
    try:
        _log_smtp_event(row, attempt_id, "CONNECTING")
        server = smtp_factory("smtp.gmail.com", 587, timeout=SMTP_CONNECT_TIMEOUT_SECONDS)
        phase = "CONNECTED"
        _log_smtp_event(row, attempt_id, phase)
        phase = "STARTTLS"
        server.starttls()
        phase = "AUTHENTICATING"
        server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
        if getattr(server, "sock", None) is not None:
            server.sock.settimeout(SMTP_SEND_TIMEOUT_SECONDS)
        phase = "AUTHENTICATED"
        _log_smtp_event(row, attempt_id, phase)

        phase = "MAIL_STARTED"
        _log_smtp_event(row, attempt_id, phase)
        code, response = server.mail(Config.GMAIL_USER)
        if not _smtp_response_ok(code, 250):
            raise smtplib.SMTPSenderRefused(code, response, Config.GMAIL_USER)

        phase = "RCPT_STARTED"
        _log_smtp_event(row, attempt_id, phase)
        refused: dict[str, tuple[int, bytes]] = {}
        for recipient in envelope:
            code, response = server.rcpt(recipient)
            if not 200 <= int(code) < 300:
                refused[recipient] = (int(code), response)
        if refused:
            # No DATA was issued, so the message cannot have been accepted.
            raise smtplib.SMTPRecipientsRefused(refused)

        phase = "DATA_STARTED"
        data_started = True
        _log_smtp_event(row, attempt_id, phase)
        # Pass text so smtplib normalizes newlines to CRLF before SMTP DATA.
        code, response = server.data(message.as_string())
        if not _smtp_response_ok(code, 250):
            raise smtplib.SMTPDataError(code, response)

        message_id = str(message.get("Message-ID") or "") or None
        smtp_accepted = True
        phase = "SMTP_ACCEPTED"
        _log_smtp_event(row, attempt_id, phase)
        _finish_attempt(db, {**row, "campaign_id": campaign_id}, attempt_id, status="SENT", message_id=message_id)
        result = {
            "property_code": row["property_code"], "smtp_status": "SENT",
            "attempt_id": attempt_id, "message_id": message_id,
        }
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
        _log_smtp_event(row, attempt_id, "SMTP_REJECTED", exception=exc, exception_phase=phase)
        _finish_attempt(
            db, {**row, "campaign_id": campaign_id}, attempt_id, status="FAILED",
            error_type=type(exc).__name__, error_message="SMTP rejected the message recipients or sender",
        )
        result = {"property_code": row["property_code"], "smtp_status": "FAILED", "attempt_id": attempt_id,
                  "error_type": type(exc).__name__}
    except (smtplib.SMTPDataError,) as exc:
        _log_smtp_event(row, attempt_id, "SMTP_REJECTED", exception=exc, exception_phase=phase)
        _finish_attempt(
            db, {**row, "campaign_id": campaign_id}, attempt_id, status="FAILED",
            error_type=type(exc).__name__, error_message="SMTP rejected message data",
        )
        result = {"property_code": row["property_code"], "smtp_status": "FAILED", "attempt_id": attempt_id,
                  "error_type": type(exc).__name__}
    except Exception as exc:
        if smtp_accepted:
            # SMTP accepted DATA; persistence failure is not an SMTP failure
            # and must never trigger a second delivery attempt.
            raise
        _log_smtp_event(row, attempt_id, "SMTP_EXCEPTION", exception=exc, exception_phase=phase)
        unknown = data_started
        status = "DELIVERY_UNKNOWN" if unknown else "FAILED"
        _finish_attempt(
            db, {**row, "campaign_id": campaign_id}, attempt_id, status=status,
            error_type=type(exc).__name__,
            error_message="SMTP outcome requires manual review" if unknown else "SMTP failed before message submission",
        )
        result = {"property_code": row["property_code"], "smtp_status": status, "attempt_id": attempt_id,
                  "error_type": type(exc).__name__}
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception as exc:
                # Once SMTP returned 250 for DATA, a QUIT failure must not
                # downgrade the accepted message or cause a resend.
                _log_smtp_event(row, attempt_id, "QUIT_ERROR", exception=exc, exception_phase="QUIT")
                try:
                    server.close()
                except Exception as close_exc:
                    _log_smtp_event(row, attempt_id, "CLOSE_ERROR", exception=close_exc, exception_phase="CLOSE")
                else:
                    _log_smtp_event(row, attempt_id, "CONNECTION_CLOSED")
            else:
                try:
                    server.close()
                except Exception as exc:
                    _log_smtp_event(row, attempt_id, "CLOSE_ERROR", exception=exc, exception_phase="CLOSE")
                else:
                    _log_smtp_event(row, attempt_id, "CONNECTION_CLOSED")
    return result


def _send_batch(
    db: Any,
    prepared: Sequence[Mapping[str, Any]],
    campaign_id: str,
    batch_id: str,
    *,
    smtp_factory: Any = None,
    send_one: Any = None,
) -> tuple[list[dict[str, Any]], str | None, int]:
    """Send sequentially; Wave 2 isolates row failures and never retries unknowns."""
    sender = send_one or _send_one
    results: list[dict[str, Any]] = []
    stop_reason: str | None = None
    consecutive_transport_errors = 0
    for row in prepared:
        try:
            result = sender(db, row, campaign_id, batch_id, smtp_factory=smtp_factory)
        except Exception as exc:
            stop_reason = _safe_validation_reason(exc)
            results.append({
                "property_code": str(row.get("property_code") or ""),
                "smtp_status": "UNHANDLED_EXCEPTION",
                "error_code": stop_reason,
            })
            break
        results.append(result)
        status = str(result.get("smtp_status") or "").strip().upper()
        if status != "SENT":
            error_type = str(result.get("error_type") or "")
            if _allows_multiowner_single_property_email(campaign_id) and status in {"FAILED", "DELIVERY_UNKNOWN"}:
                if error_type in {"SMTPAuthenticationError", "SMTPConnectError", "SMTPSenderRefused"}:
                    stop_reason = f"systemic_smtp_error:{error_type}"
                    break
                if error_type in {"SMTPServerDisconnected", "TimeoutError", "OSError"}:
                    consecutive_transport_errors += 1
                    if consecutive_transport_errors >= 3:
                        stop_reason = f"repeated_smtp_transport_error:{error_type}"
                        break
                else:
                    consecutive_transport_errors = 0
                continue
            stop_reason = status or "invalid_send_result"
            break
        consecutive_transport_errors = 0
    return results, stop_reason, max(0, len(prepared) - len(results))


def run_manifest(
    manifest_path: str | Path = DEFAULT_MANIFEST,
    *,
    mode: str = "DRY_RUN",
    confirm_send: bool = False,
    db: Any | None = None,
    smtp_factory: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate any complete campaign manifest; SMTP is opt-in and fail-closed."""
    mode = str(mode or "DRY_RUN").strip().upper()
    if mode not in {"DRY_RUN", "SEND"}:
        raise SenderError("mode_invalid")
    rows, campaign_id, batch_id = _read_manifest(manifest_path)
    if mode == "SEND":
        expected = f"SEND_OWNER_CAMPAIGN:{campaign_id}:{batch_id}"
        if (
            not confirm_send
            or str(os.getenv("OWNER_CAMPAIGN_PRODUCTION_SEND_ENABLED", "false")).strip().casefold() not in {"1", "true", "yes", "on"}
            or not hmac_compare(str(os.getenv("OWNER_CAMPAIGN_SEND_CONFIRMATION", "")), expected)
            or str(os.getenv("OWNER_CAMPAIGN_MANUAL_RECENT_EXCLUSIONS_CONFIRMED", "false")).strip().casefold() not in {"1", "true", "yes", "on"}
        ):
            raise SenderError("send_requires_explicit_runtime_authorization")

    owned_client = None
    if db is None:
        if not Config.MONGO_URI:
            raise SenderError("mongo_unavailable")
        owned_client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=15000)
        db = owned_client[Config.DB_NAME]
    try:
        from campanas import owner_campaign_test_runtime as runtime

        now = now or datetime.now(timezone.utc)
        all_properties = list(db[runtime.PROPERTY_COLLECTION].find({
            "estado.oficina": runtime.CAMPAIGN_OFFICE,
            "estado.estado_prop360": {"$regex": "^activa$", "$options": "i"},
            "disponible_prop360": True,
        }))
        all_properties = [item for item in all_properties if runtime._active_available(item) and runtime._is_sucre(item)]
        percentile_data = runtime.calculate_owner_campaign_lead_percentiles(db, now=now)
        results: list[dict[str, Any]] = []
        prepared: list[dict[str, Any]] = []
        for manifest_row in rows:
            code = normalize_property_code(manifest_row.get("property_code"))
            manifest_owner = str(manifest_row.get("owner_email") or "").strip().casefold()
            try:
                manifest_cc = _recipient_cc(manifest_row, runtime)
                recipient_checks_valid = bool(_valid_email(manifest_owner)) and all(_valid_email(address) for address in manifest_cc)
            except SenderError:
                manifest_cc = []
                recipient_checks_valid = False
            try:
                valid = _validate_row(
                    manifest_row, db=db, all_properties=all_properties,
                    percentile_data=percentile_data, now=now,
                    campaign_id=campaign_id, batch_id=batch_id,
                )
                valid["final_cc_list"] = _recipient_cc(valid, runtime)
                ledger_record = db[Config.COLLECTION_CAMPANAS_LOG].find_one(
                    {"_id": f"{campaign_id}:{valid['property_code']}", "campaign_id": campaign_id,
                     "property_code": str(valid["property_code"])},
                    {"send_status": 1, "authorization_status": 1},
                ) or {}
                status = str(ledger_record.get("send_status") or "READY").upper()
                if status != "READY" or ledger_record.get("authorization_status") == "PRICE_AUTHORIZED":
                    raise SenderError("campaign_property_already_claimed_or_authorized")
                prepared.append(valid)
                results.append({
                    "property_code": valid["property_code"], "owner_email": valid["owner_email"],
                    "final_cc_list": valid["final_cc_list"], "operation": valid["operation"],
                    "recommended_adjustment_pct": valid["recommended_adjustment_pct"],
                    "current_price": valid["current_price"], "recommended_price": valid["recommended_price"],
                    "document_type": valid["document_type"], "render_status": valid["render_status"],
                    "auth_token_valid": valid["auth_token_valid"], "advisor_token_valid": valid["advisor_token_valid"],
                    "document_routing_valid": valid["document_routing_valid"],
                    "gmail_safe": valid["gmail_safe"],
                    "price_valid": valid["price_valid"],
                    "eligibility_status": "PASS",
                    "recipient_checks_valid": True,
                })
            except Exception as exc:
                current_value = _number(manifest_row.get("current_price"))
                recommended_value = _number(manifest_row.get("recommended_price"))
                adjustment_value = _number(manifest_row.get("recommended_adjustment_pct"))
                price_valid = bool(
                    current_value is not None and current_value > 0
                    and recommended_value is not None and 0 < recommended_value < current_value
                    and adjustment_value is not None and 5 <= adjustment_value <= 10
                )
                results.append({
                    "property_code": str(manifest_row.get("property_code") or code),
                    "owner_email": manifest_owner,
                    "final_cc_list": manifest_cc, "operation": str(manifest_row.get("operation_resolved") or ""),
                    "recommended_adjustment_pct": manifest_row.get("recommended_adjustment_pct"),
                    "current_price": manifest_row.get("current_price"),
                    "recommended_price": manifest_row.get("recommended_price"),
                    "document_type": manifest_row.get("document_type"),
                    "render_status": "FAIL", "auth_token_valid": False,
                    "advisor_token_valid": False, "document_routing_valid": False,
                    "gmail_safe": False,
                    "price_valid": price_valid,
                    "eligibility_status": _safe_validation_reason(exc),
                    "recipient_checks_valid": recipient_checks_valid,
                })

        if mode == "SEND" and len(prepared) != len(rows):
            raise SenderError("batch_preflight_failed_smtp_not_started")
        if mode == "SEND":
            send_results, send_stop_reason, send_unsent_count = _send_batch(
                db, prepared, campaign_id, batch_id, smtp_factory=smtp_factory,
            )
        else:
            send_results = []
            send_stop_reason = None
            send_unsent_count = 0
        passed = sum(item["eligibility_status"] == "PASS" for item in results)
        return {
            "mode": mode,
            "campaign_id": campaign_id,
            "batch_id": batch_id,
            "manifest_count": len(rows),
            "dry_run_count": len(results),
            "eligible_count": passed,
            "all_eligible": passed == len(rows),
            "production_signing_configured": bool(os.getenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "")),
            "all_to_valid": bool(results) and all(bool(_valid_email(item.get("owner_email"))) for item in results),
            "all_cc_valid": bool(results) and all(bool(item["recipient_checks_valid"]) for item in results),
            "all_cc_deduplicated": bool(results) and all(
                len([item.get("owner_email", "").casefold(), *[x.casefold() for x in item["final_cc_list"]]])
                == len(set([item.get("owner_email", "").casefold(), *[x.casefold() for x in item["final_cc_list"]]]))
                for item in results
            ),
            "all_render_pass": all(item["render_status"] == "PASS" for item in results),
            "all_gmail_safe": bool(results) and all(item.get("gmail_safe") is True for item in results),
            "all_document_routing_valid": all(item["document_routing_valid"] for item in results),
            "all_auth_token_valid": all(item["auth_token_valid"] for item in results),
            "all_advisor_token_valid": all(item["advisor_token_valid"] for item in results),
            "all_price_valid": all(item["price_valid"] for item in results),
            "multiproperty_in_manifest": sum(item["eligibility_status"] == "owner_is_not_single_property" for item in results),
            "excluded_codes_in_manifest": sum(item["eligibility_status"] == "campaign_exclusion_applies" for item in results),
            "excluded_emails_in_manifest": sum(
                item["eligibility_status"] == "campaign_exclusion_applies"
                and str(item.get("owner_email") or "").strip().casefold() in {str(value).strip().casefold() for value in EXCLUDED_OWNER_EMAILS}
                for item in results
            ),
            "duplicates": 0,
            "smtp_called": mode == "SEND" and bool(send_results),
            "send_results": send_results,
            "send_batch_stopped": send_stop_reason is not None,
            "send_stop_reason": send_stop_reason,
            "send_unsent_count": send_unsent_count,
            "rows": results,
        }
    finally:
        if owned_client is not None:
            owned_client.close()


def run_campaign_batch(
    campaign_id: str,
    batch_id: str,
    *,
    mode: str = "DRY_RUN",
    confirm_send: bool = False,
    db: Any | None = None,
    smtp_factory: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the existing sender against a durable campaign batch in Mongo.

    The temporary CSV is only an in-process adapter for the existing sender;
    Mongo remains the durable source of the prepared rows.
    """
    campaign_id = str(campaign_id or "").strip()
    batch_id = str(batch_id or "").strip()
    if not campaign_id or not batch_id:
        raise SenderError("campaign_batch_identity_missing")
    owned_client = None
    if db is None:
        if not Config.MONGO_URI:
            raise SenderError("mongo_unavailable")
        owned_client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=15000)
        db = owned_client[Config.DB_NAME]
    try:
        ledger = db[Config.COLLECTION_CAMPANAS_LOG]
        stage_filter = ["PILOT", "REMAINDER"]
        wave2 = _allows_multiowner_single_property_email(campaign_id)
        if wave2:
            stage_filter.append("WAVE2")
        all_stored = list(ledger.find({
            "campaign_id": campaign_id,
            "batch_id": batch_id,
            "campaign_stage": {"$in": stage_filter},
        }))
        if not all_stored:
            raise SenderError("campaign_batch_not_found")
        if wave2:
            stored = _wave2_ready_rows(all_stored)
            if not stored:
                raise SenderError("campaign_batch_has_no_ready_rows")
        else:
            stored = all_stored
            if any(str(item.get("send_status") or "").upper() != "READY" for item in stored):
                raise SenderError("campaign_batch_contains_non_ready_rows")
        snapshots: list[dict[str, Any]] = []
        for record in stored:
            snapshot = record.get("campaign_snapshot")
            snapshot = snapshot if isinstance(snapshot, Mapping) else {}
            row = {key: record.get(key, snapshot.get(key, "")) for key in REQUIRED_COLUMNS}
            row.update({
                "campaign_id": campaign_id,
                "batch_id": batch_id,
                "property_code": str(record.get("property_code") or ""),
                "owner_email": record.get("owner_email", snapshot.get("owner_email", "")),
                "operation_resolved": record.get("operation_resolved", record.get("operation", snapshot.get("operation_resolved", snapshot.get("operation", "")))),
                "executive_name": record.get("executive_name", snapshot.get("executive_name", "")),
                "executive_email": record.get("executive_email", snapshot.get("executive_email", "")),
                "boss_cc": record.get("boss_cc", snapshot.get("boss_cc", "")),
                "current_price": record.get("current_price", snapshot.get("current_price", "")),
                "recommended_adjustment_pct": record.get("recommended_adjustment_pct", snapshot.get("recommended_adjustment_pct", "")),
                "recommended_price": record.get("recommended_price", snapshot.get("recommended_price", "")),
                "document_type": record.get("document_type", snapshot.get("document_type", "")),
                "send_status": record.get("send_status", ""),
            })
            if not row["property_code"] or any(row.get(key) in (None, "") for key in REQUIRED_COLUMNS):
                raise SenderError("campaign_batch_snapshot_incomplete")
            snapshots.append(row)
        codes = [normalize_property_code(row["property_code"]) for row in snapshots]
        owners = [str(row["owner_email"]).strip().casefold() for row in snapshots]
        if len(codes) != len(set(codes)) or (not wave2 and len(owners) != len(set(owners))):
            raise SenderError("campaign_batch_duplicate_rows")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", suffix=".csv", delete=False) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=sorted(REQUIRED_COLUMNS))
            writer.writeheader()
            writer.writerows(snapshots)
        try:
            return run_manifest(
                temp_path, mode=mode, confirm_send=confirm_send, db=db,
                smtp_factory=smtp_factory, now=now,
            )
        finally:
            temp_path.unlink(missing_ok=True)
    finally:
        if owned_client is not None:
            owned_client.close()


def hmac_compare(value: str, expected: str) -> bool:
    import hmac

    return hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or send a prepared owner campaign manifest")
    parser.add_argument("--manifest")
    parser.add_argument("--campaign-id")
    parser.add_argument("--batch-id")
    parser.add_argument("--mode", choices=("DRY_RUN", "SEND"), default="DRY_RUN")
    parser.add_argument("--confirm-send", action="store_true")
    args = parser.parse_args()
    try:
        if bool(args.campaign_id) != bool(args.batch_id):
            raise SenderError("campaign_batch_identity_incomplete")
        if args.campaign_id:
            if args.manifest:
                raise SenderError("manifest_and_campaign_batch_are_mutually_exclusive")
            result = run_campaign_batch(
                args.campaign_id, args.batch_id, mode=args.mode,
                confirm_send=args.confirm_send,
            )
        else:
            result = run_manifest(args.manifest or DEFAULT_MANIFEST, mode=args.mode, confirm_send=args.confirm_send)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("all_eligible") else 2
    except SenderError as exc:
        print(json.dumps({"mode": args.mode, "status": "BLOCKED", "reason": exc.code}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
