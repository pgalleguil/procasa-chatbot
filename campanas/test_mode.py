"""Signed, isolated campaign test actions. Test clicks never mutate properties."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Mapping


TEST_RECIPIENT = "pgalleguillos@procasa.cl"
TEST_CAMPAIGN_ID = "owner_price_campaign_test_20260923"
TEST_CAMPAIGN_PREFIX = "owner_price_campaign_test_"
TEST_ACTIONS = {"aceptar_rebaja", "contactar_ejecutivo", "ver_informe"}
REPORT_DOCUMENT_TYPES = {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}
EVENT_BY_ACTION = {
    "aceptar_rebaja": "price_authorized",
    "contactar_ejecutivo": "advisor_review_requested",
    "ver_informe": "report_opened",
}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def issue_test_token(
    *, campaign_id: str, property_code: str, action: str, secret: str,
    expires_at: int, recipient: str = TEST_RECIPIENT, document_type: str | None = None,
) -> str:
    if campaign_id != TEST_CAMPAIGN_ID or not campaign_id.startswith(TEST_CAMPAIGN_PREFIX):
        raise ValueError("Test campaign id must match the isolated E2E campaign")
    if recipient.strip().casefold() != TEST_RECIPIENT:
        raise ValueError("Test tokens may only target the configured test recipient")
    if action not in TEST_ACTIONS or not property_code.strip() or not secret:
        raise ValueError("Invalid test token claims")
    normalized_document_type = str(document_type or "").strip().upper()
    if action == "ver_informe" and normalized_document_type not in REPORT_DOCUMENT_TYPES:
        raise ValueError("Report test tokens require a supported document_type")
    if action != "ver_informe" and normalized_document_type:
        raise ValueError("document_type is only valid for report tokens")
    payload = {
        "campaign_id": campaign_id,
        "property_code": property_code.strip(),
        "action": action,
        "recipient": TEST_RECIPIENT,
        "exp": int(expires_at),
        "test_mode": True,
    }
    if action == "ver_informe":
        payload["document_type"] = normalized_document_type
    encoded = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _b64(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"t1.{encoded}.{signature}"


def verify_test_token(
    token: str, *, secret: str, campaign_id: str, property_code: str,
    action: str, recipient: str, now_epoch: int | None = None,
    document_type: str | None = None,
) -> dict[str, Any] | None:
    if not secret or recipient.strip().casefold() != TEST_RECIPIENT:
        return None
    try:
        version, encoded, signature = token.split(".", 2)
        expected = _b64(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
        if version != "t1" or not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        now = int(now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp())
        payload_document_type = str(payload.get("document_type") or "").strip().upper()
        expected_document_type = str(document_type or "").strip().upper()
        if (
            payload.get("test_mode") is not True
            or payload.get("recipient") != TEST_RECIPIENT
            or not str(payload.get("campaign_id", "")).startswith(TEST_CAMPAIGN_PREFIX)
            or payload.get("campaign_id") != TEST_CAMPAIGN_ID
            or payload.get("campaign_id") != campaign_id
            or payload.get("property_code") != property_code
            or payload.get("action") != action
            or action not in TEST_ACTIONS
            or (action == "ver_informe" and payload_document_type not in REPORT_DOCUMENT_TYPES)
            or (expected_document_type and payload_document_type != expected_document_type)
            or (action != "ver_informe" and payload_document_type)
            or int(payload.get("exp", 0)) <= now
        ):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def persist_test_event(
    ledger: Any,
    claims: Mapping[str, Any],
    *,
    now: datetime | None = None,
    stage: str = "complete",
) -> dict[str, Any]:
    """Append to a pre-created test campaign/property row in ajuste_precio.

    Requiring the prepared row preserves the intended owner and campaign
    snapshot and prevents an unbounded click from creating a partial ledger row.
    """
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    campaign_id = str(claims["campaign_id"])
    code = str(claims["property_code"])
    action = str(claims["action"])
    document_type = str(claims.get("document_type") or "").strip().upper() or None
    if stage not in {"click", "confirm", "complete"}:
        raise ValueError("Unsupported test event stage")
    event_name = EVENT_BY_ACTION[action]
    token_id = hashlib.sha256(
        f"{campaign_id}|{code}|{action}|{document_type or ''}|{claims.get('exp')}".encode()
    ).hexdigest()[:24]
    legacy_query = {
        "campana": campaign_id,
        "codigo_propiedad": code,
        "test_mode": True,
    }
    current_query = {
        "campaign_id": campaign_id,
        "property_code": code,
        "test_mode": True,
    }
    existing = ledger.find_one(legacy_query)
    base_query = legacy_query if existing else current_query
    if not existing:
        existing = ledger.find_one(current_query)
    if not existing or not (existing.get("intended_owner_email") or existing.get("owner_email")):
        raise LookupError("Prepared test campaign/property ledger row not found")
    if not existing.get("test_mode"):
        raise ValueError("Refusing to append a test event to a live campaign row")
    actual_recipient = str(existing.get("actual_recipient_email") or "").strip().casefold()
    if actual_recipient != TEST_RECIPIENT:
        raise ValueError("Prepared test ledger row has an unexpected recipient")
    if action == "ver_informe":
        ledger_document_type = str(
            existing.get("supporting_document_type") or existing.get("document_type") or ""
        ).strip().upper()
        if ledger_document_type != document_type:
            raise ValueError("Report token document_type does not match the prepared property row")

    event_ids = existing.get("response_event_ids") or []
    click_id = hashlib.sha256(f"{token_id}|cta_clicked".encode()).hexdigest()[:24]
    authorization_id = hashlib.sha256(f"{token_id}|confirmed_authorization".encode()).hexdigest()[:24]
    if stage == "click":
        desired_events = [] if click_id in event_ids else [
            {"event_id": click_id, "event": "cta_clicked", "action": action}
        ]
        desired_ids = [click_id]
    elif stage == "confirm":
        if action != "aceptar_rebaja":
            raise ValueError("Only price authorization has a confirmation stage")
        if click_id not in event_ids:
            raise ValueError("Price authorization requires a prior CTA click")
        desired_events = [] if authorization_id in event_ids else [{
            "event_id": authorization_id,
            "event": event_name,
            "action": action,
            "executive": existing.get("executive"),
            "proposed_value": existing.get("display_target"),
            "authorized_value": existing.get("display_target"),
            "current_value_at_campaign": existing.get("current_price_at_send"),
        }]
        desired_ids = [authorization_id]
    else:
        desired_events = []
        desired_ids = []
        if click_id not in event_ids:
            desired_events.append({"event_id": click_id, "event": "cta_clicked", "action": action})
            desired_ids.append(click_id)
        if token_id not in event_ids:
            desired_events.append({
                "event_id": token_id,
                "event": event_name,
                "action": action,
                "executive": existing.get("executive"),
            })
            desired_ids.append(token_id)
    if not desired_events:
        return {
            "event": event_name,
            "stored_collection": "ajuste_precio",
            "stored_record_id": str(existing.get("_id") or ""),
            "timestamp": timestamp.isoformat(),
            "test_mode": True,
            "duplicate": True,
            "proposed_value": existing.get("display_target"),
        }
    common = {
        "campaign_id": campaign_id,
        "campaign_version": existing.get("campaign_version") or "OWNER_CAMPAIGN_EMAIL_V2",
        "property_code": code,
        "owner_email": existing.get("intended_owner_email") or existing.get("owner_email"),
        "document_type": document_type,
        "event_at": timestamp.isoformat(),
        "token_version": "t1",
        "test_mode": True,
    }
    events = [{**common, **event} for event in desired_events]
    update = {
        "$addToSet": {"response_event_ids": {"$each": desired_ids}},
        "$push": {"response_events": {"$each": events}},
    }
    query = {**base_query, "actual_recipient_email": TEST_RECIPIENT}
    result = ledger.update_one(query, update, upsert=False)
    record = ledger.find_one(base_query) or existing
    return {
        "event": event_name,
        "stored_collection": "ajuste_precio",
        "stored_record_id": str(record.get("_id") or getattr(result, "upserted_id", "") or ""),
        "timestamp": timestamp.isoformat(),
        "test_mode": True,
        "duplicate": not bool(getattr(result, "modified_count", 0)),
        "proposed_value": existing.get("display_target"),
    }
