"""Signed, isolated campaign test actions. Test clicks never mutate properties."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4


TEST_RECIPIENT = "p.galleguil@gmail.com"
QA_PROPERTY_ALLOWLIST = frozenset({"5641", "6132", "16486", "6873", "16521", "16469", "16527", "16492"})
TEST_CAMPAIGN_ID = "owner_price_campaign_test_20260923"
TEST_CAMPAIGN_VERSION = "owner_campaign_test_20260923"
TEST_CAMPAIGN_PREFIX = "owner_price_campaign_test_"
QA_CAMPAIGN_PREFIX = "owner_price_campaign_qa_"
QA_CAMPAIGN_ID_RE = re.compile(r"^owner_price_campaign_(?:test_20260923|qa_[A-Za-z0-9_-]{12,80})$")
QA_RUN_ID_RE = re.compile(r"^qa_run_[A-Za-z0-9_-]{12,80}$")
LEGACY_TEST_RUN_ID = "owner_campaign_email_AE_20260924_v1"
ACCEPT_PRICE_ACTION = "aceptar_rebaja"
ADVISOR_ACTION = "contactar_ejecutivo"
REPORT_ACTION = "ver_informe"
TEST_ACTIONS = {ACCEPT_PRICE_ACTION, ADVISOR_ACTION, REPORT_ACTION}
REPORT_DOCUMENT_TYPES = {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}
PROPERTY_CODE_RE = re.compile(r"^[0-9]{1,32}$")
EVENT_BY_ACTION = {
    ACCEPT_PRICE_ACTION: "price_authorized",
    ADVISOR_ACTION: "advisor_review_requested",
    REPORT_ACTION: "report_opened",
}
PRICE_CONFIRM_PAGE_OPENED = "price_confirm_page_opened"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def test_mode_enabled() -> bool:
    return os.getenv("OWNER_CAMPAIGN_TEST_MODE", "").strip().casefold() == "true"


def issue_test_token(
    *, campaign_id: str, property_code: str, action: str, secret: str,
    expires_at: int, recipient: str = TEST_RECIPIENT, document_type: str | None = None,
    qa_run_id: str | None = None,
) -> str:
    if not isinstance(campaign_id, str) or not QA_CAMPAIGN_ID_RE.fullmatch(campaign_id):
        raise ValueError("Test campaign id must match the isolated E2E campaign")
    if recipient.strip().casefold() != TEST_RECIPIENT:
        raise ValueError("Test tokens may only target the configured test recipient")
    if qa_run_id is not None and (
        not isinstance(qa_run_id, str)
        or (qa_run_id != LEGACY_TEST_RUN_ID and not QA_RUN_ID_RE.fullmatch(qa_run_id))
    ):
        raise ValueError("Invalid QA run id")
    property_code = str(property_code or "").strip()
    if (
        action not in TEST_ACTIONS
        or property_code not in QA_PROPERTY_ALLOWLIST
        or not PROPERTY_CODE_RE.fullmatch(property_code)
        or not secret
    ):
        raise ValueError("Invalid test token claims")
    normalized_document_type = str(document_type or "").strip().upper()
    if action == REPORT_ACTION and normalized_document_type not in REPORT_DOCUMENT_TYPES:
        raise ValueError("Report test tokens require a supported document_type")
    if action != REPORT_ACTION and normalized_document_type:
        raise ValueError("document_type is only valid for report tokens")
    payload = {
        "campaign_id": campaign_id,
        "property_code": property_code,
        "action": action,
        "recipient": TEST_RECIPIENT,
        "qa_recipient": TEST_RECIPIENT,
        "exp": int(expires_at),
        "test_mode": True,
    }
    if qa_run_id:
        payload["qa_run_id"] = qa_run_id
    if action == REPORT_ACTION:
        payload["document_type"] = normalized_document_type
    encoded = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _b64(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"t1.{encoded}.{signature}"


def issue_campaign_test_token(
    *, property_code: str, action: str, document_type: str | None = None,
    expires_in_seconds: int = 86400, now_epoch: int | None = None,
    campaign_id: str = TEST_CAMPAIGN_ID,
    qa_run_id: str | None = None,
) -> str:
    """Issue a one-day token using a safe QA campaign and fixed recipient."""
    if not test_mode_enabled():
        raise ValueError("Test mode is disabled")
    if not isinstance(expires_in_seconds, int) or not 60 <= expires_in_seconds <= 86400:
        raise ValueError("Test token TTL is invalid")
    secret = os.getenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", "")
    expires_at = int(now_epoch if now_epoch is not None else time.time()) + expires_in_seconds
    return issue_test_token(
        campaign_id=campaign_id,
        property_code=property_code,
        action=action,
        secret=secret,
        expires_at=expires_at,
        recipient=TEST_RECIPIENT,
        document_type=document_type,
        qa_run_id=qa_run_id,
    )


def decode_test_token(
    token: str, *, secret: str, now_epoch: int | None = None,
) -> dict[str, Any] | None:
    """Verify the shared signed-token contract and return its claims."""
    if not secret or not token:
        return None
    try:
        version, encoded, signature = token.split(".", 2)
        expected = _b64(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
        if version != "t1" or not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        now = int(now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp())
        document_type = str(payload.get("document_type") or "").strip().upper()
        campaign_id = str(payload.get("campaign_id") or "")
        qa_run_id = payload.get("qa_run_id")
        property_code = str(payload.get("property_code") or "")
        action = str(payload.get("action") or "")
        if (
            not isinstance(payload, dict)
            or payload.get("test_mode") is not True
            or payload.get("recipient") != TEST_RECIPIENT
            or payload.get("qa_recipient") != TEST_RECIPIENT
            or not QA_CAMPAIGN_ID_RE.fullmatch(campaign_id)
            or property_code not in QA_PROPERTY_ALLOWLIST
            or not PROPERTY_CODE_RE.fullmatch(property_code)
            or action not in TEST_ACTIONS
            or (action == REPORT_ACTION and document_type not in REPORT_DOCUMENT_TYPES)
            or (action != REPORT_ACTION and document_type)
            or int(payload.get("exp", 0)) <= now
            or (
                qa_run_id is not None
                and (
                    not isinstance(qa_run_id, str)
                    or (qa_run_id != LEGACY_TEST_RUN_ID and not QA_RUN_ID_RE.fullmatch(qa_run_id))
                )
            )
        ):
            return None
        payload["document_type"] = document_type if document_type else None
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, OverflowError):
        return None


def verify_campaign_test_token(
    token: str, *, allowed_actions: set[str] | frozenset[str] = TEST_ACTIONS,
    now_epoch: int | None = None,
) -> dict[str, Any] | None:
    if not test_mode_enabled():
        return None
    claims = decode_test_token(
        token,
        secret=os.getenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", ""),
        now_epoch=now_epoch,
    )
    if claims is None or claims.get("action") not in allowed_actions:
        return None
    return claims


def verify_test_token(
    token: str, *, secret: str, campaign_id: str, property_code: str,
    action: str, recipient: str, now_epoch: int | None = None,
    document_type: str | None = None,
) -> dict[str, Any] | None:
    payload = decode_test_token(token, secret=secret, now_epoch=now_epoch)
    expected_document_type = str(document_type or "").strip().upper()
    if (
        payload is None
        or recipient.strip().casefold() != TEST_RECIPIENT
        or payload.get("campaign_id") != campaign_id
        or payload.get("property_code") != property_code
        or payload.get("action") != action
        or (expected_document_type and payload.get("document_type") != expected_document_type)
    ):
        return None
    return payload


def _event_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def persist_test_event(
    ledger: Any,
    claims: Mapping[str, Any],
    *,
    now: datetime | None = None,
    stage: str = "complete",
    selected_adjustment_type: str = "RECOMMENDED",
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
    current_query = {
        "campaign_id": campaign_id,
        "property_code": code,
        "test_mode": True,
    }
    legacy_query = {
        "campana": campaign_id,
        "codigo_propiedad": code,
        "test_mode": True,
    }
    existing = ledger.find_one(current_query)
    base_query = current_query
    if not existing:
        existing = ledger.find_one(legacy_query)
        base_query = legacy_query
    if not existing or not (existing.get("intended_owner_email") or existing.get("owner_email")):
        raise LookupError("Prepared test campaign/property ledger row not found")
    if not existing.get("test_mode"):
        raise ValueError("Refusing to append a test event to a live campaign row")
    actual_recipient = str(existing.get("actual_recipient_email") or "").strip().casefold()
    resend_recipient = str(existing.get("test_resend_recipient_email") or "").strip().casefold()
    if TEST_RECIPIENT not in {actual_recipient, resend_recipient}:
        raise ValueError("Prepared test ledger row has an unexpected recipient")
    if action == REPORT_ACTION:
        ledger_document_type = str(
            existing.get("supporting_document_type") or existing.get("document_type") or ""
        ).strip().upper()
        if ledger_document_type != document_type:
            raise ValueError("Report token document_type does not match the prepared property row")

    event_ids = set(existing.get("response_event_ids") or [])
    history = [event for event in (existing.get("response_events") or []) if isinstance(event, Mapping)]
    for stored_event in history:
        if isinstance(stored_event, Mapping) and stored_event.get("event_id"):
            event_ids.add(stored_event["event_id"])
    owner_response = existing.get("owner_response")
    owner_response = owner_response if isinstance(owner_response, Mapping) else {}
    historical_authorizations = [
        event for event in history if event.get("event") == EVENT_BY_ACTION[ACCEPT_PRICE_ACTION]
    ]
    if historical_authorizations and owner_response.get("status") != "PRICE_AUTHORIZED":
        dated_authorizations = [
            (date, event) for event in historical_authorizations
            if (date := _event_datetime(event.get("event_at"))) is not None
        ]
        latest_event = max(
            dated_authorizations, key=lambda item: item[0]
        )[1] if dated_authorizations else historical_authorizations[-1]
        normalize_set: dict[str, Any] = {"owner_response.status": "PRICE_AUTHORIZED"}
        authorized_value = latest_event.get("authorized_value", latest_event.get("proposed_value"))
        current_value = latest_event.get("current_value_at_campaign")
        if authorized_value is not None:
            normalize_set["owner_response.authorized_value"] = authorized_value
        if current_value is not None:
            normalize_set["owner_response.current_value_at_campaign"] = current_value
        normalize_update: dict[str, Any] = {"$set": normalize_set}
        if dated_authorizations:
            normalize_update["$min"] = {
                "owner_response.first_authorized_at": min(item[0] for item in dated_authorizations)
            }
            normalize_update["$max"] = {
                "owner_response.last_authorized_at": max(item[0] for item in dated_authorizations)
            }
        ledger.update_one(
            {**base_query, "owner_response.status": {"$in": [None, "", "PENDING"]}},
            normalize_update,
            upsert=False,
        )
    click_id = hashlib.sha256(f"{token_id}|cta_clicked".encode()).hexdigest()[:24]
    authorization_id = hashlib.sha256(f"{token_id}|confirmed_authorization".encode()).hexdigest()[:24]
    occurrence = timestamp.isoformat()
    occurrence_nonce = uuid4().hex
    confirm_page_id = hashlib.sha256(
        f"{token_id}|{PRICE_CONFIRM_PAGE_OPENED}|{occurrence}|{occurrence_nonce}".encode()
    ).hexdigest()[:24]
    if stage == "click":
        desired_events = []
        if click_id not in event_ids:
            desired_events.append({"event_id": click_id, "event": "cta_clicked", "action": action})
        if action == ACCEPT_PRICE_ACTION and confirm_page_id not in event_ids:
            desired_events.append({
                "event_id": confirm_page_id,
                "event": PRICE_CONFIRM_PAGE_OPENED,
                "action": action,
            })
            desired_events.append({
                "event_id": hashlib.sha256(f"{token_id}|confirmation_page_opened".encode()).hexdigest()[:24],
                "event": "confirmation_page_opened",
                "action": action,
            })
        if action == REPORT_ACTION:
            report_id = hashlib.sha256(f"{token_id}|report_opened".encode()).hexdigest()[:24]
            if report_id not in event_ids:
                desired_events.append({"event_id": report_id, "event": "report_opened", "action": action})
    elif stage == "confirm":
        if action != ACCEPT_PRICE_ACTION:
            raise ValueError("Only price authorization has a confirmation stage")
        if click_id not in event_ids:
            raise ValueError("Price authorization requires a prior CTA click")
        if owner_response.get("status") == "PRICE_AUTHORIZED" or historical_authorizations:
            return {
                "event": event_name, "event_ids": [], "stored_collection": "ajuste_precio",
                "stored_record_id": str(existing.get("_id") or ""), "timestamp": timestamp.isoformat(),
                "test_mode": True, "duplicate": True,
                "proposed_value": existing.get("display_target", existing.get("display_recommended_price")),
            }
        recommended_price = existing.get("display_recommended_price", existing.get("display_target"))
        try:
            current_price = float(existing.get("current_price_at_send"))
            recommended_price_value = float(recommended_price)
            recommended_pct = int(round((current_price - recommended_price_value) * 100 / current_price))
        except (TypeError, ValueError, ZeroDivisionError):
            raise ValueError("QA recommendation values are invalid") from None
        if not 5 <= recommended_pct <= 10:
            raise ValueError("QA recommendation is outside the approved range")
        # Keep QA authorization data aligned with the one canonical offer
        # calculator. Never recreate the retired 5–8% mapping in the ledger.
        from analytics.owner_campaign_email_v2 import calculate_gradual_price_alternative
        from campanas.owner_campaign_confirmation_page import gradual_option_enabled

        gradual_enabled = gradual_option_enabled(campaign_id)
        gradual = calculate_gradual_price_alternative(
            current_price=current_price,
            recommended_adjustment_pct=recommended_pct,
        )
        gradual_pct = gradual.get("adjustment_pct")
        gradual_price = gradual.get("price")
        selected_type = str(selected_adjustment_type or "RECOMMENDED").strip().upper()
        if selected_type not in {"RECOMMENDED", "GRADUAL"}:
            raise ValueError("Selected QA adjustment type is invalid")
        if selected_type == "GRADUAL" and (
            not gradual_enabled or gradual_pct is None or gradual_pct == recommended_pct
        ):
            raise ValueError("Gradual QA adjustment is unavailable")
        selected_pct = recommended_pct if selected_type == "RECOMMENDED" else gradual_pct
        selected_price = recommended_price_value if selected_type == "RECOMMENDED" else gradual_price
        selection_event = "recommended_selected" if selected_type == "RECOMMENDED" else "gradual_selected"
        selection_id = hashlib.sha256(f"{token_id}|{selection_event}".encode()).hexdigest()[:24]
        details = {
            "previous_price": current_price,
            "recommended_adjustment_pct": recommended_pct,
            "recommended_price": recommended_price_value,
            "selected_adjustment_type": selected_type,
            "selected_adjustment_pct": selected_pct,
            "selected_price": selected_price,
            "authorization_status": "PRICE_AUTHORIZED",
            "authorized_at": timestamp.isoformat(),
            "executive_email": existing.get("executive_email"),
        }
        if gradual_enabled:
            details.update({
                "gradual_adjustment_pct": gradual_pct,
                "gradual_price": gradual_price,
            })
        auth_event = {
            "event_id": authorization_id, "event": event_name, "action": action,
            "executive": existing.get("executive"), **details,
        }
        selection = {"event_id": selection_id, "event": selection_event, "action": action, **details}
        desired_events = [auth_event, selection]
    else:
        desired_events = []
        if click_id not in event_ids:
            desired_events.append({"event_id": click_id, "event": "cta_clicked", "action": action})
        # A signed advisor action token represents one logical request. The
        # success page may be refreshed or reopened, but those page views must
        # not create additional commercial requests. Keep other complete-stage
        # events occurrence-based if they are added in the future.
        occurrence_event_id = (
            hashlib.sha256(f"{token_id}|{event_name}".encode()).hexdigest()[:24]
            if action == ADVISOR_ACTION
            else hashlib.sha256(
                f"{token_id}|{event_name}|{occurrence}|{occurrence_nonce}".encode()
            ).hexdigest()[:24]
        )
        if occurrence_event_id not in event_ids:
            desired_events.append({
                "event_id": occurrence_event_id,
                "event": event_name,
                "action": action,
                "executive": existing.get("executive"),
                "selected_adjustment_type": "ADVISOR_REVIEW" if action == ADVISOR_ACTION else None,
                "recommended_adjustment_pct": existing.get("recommended_adjustment_pct"),
                "recommended_price": existing.get("display_recommended_price", existing.get("display_target")),
                "previous_price": existing.get("current_price_at_send"),
                "executive_email": existing.get("executive_email"),
            })
    desired_ids = [event["event_id"] for event in desired_events]
    if not desired_events:
        ledger.update_one(
            {**base_query, "owner_response.status": {"$in": [None, ""]}},
            {"$set": {"owner_response.status": "PENDING"}},
            upsert=False,
        )
        return {
            "event": event_name,
            "event_ids": [],
            "stored_collection": "ajuste_precio",
            "stored_record_id": str(existing.get("_id") or ""),
            "timestamp": timestamp.isoformat(),
            "test_mode": True,
            "duplicate": True,
            "proposed_value": existing.get("display_target", existing.get("display_recommended_price")),
        }
    common = {
        "campaign_id": campaign_id,
        "campaign_version": existing.get("campaign_version") or "OWNER_CAMPAIGN_EMAIL_V2",
        "property_code": code,
        "owner_email": existing.get("intended_owner_email") or existing.get("owner_email"),
        "executive": existing.get("executive"),
        "document_type": document_type,
        "event_at": timestamp.isoformat(),
        "timestamp_utc": timestamp.isoformat(),
        "event_type": None,
        "source": "owner_campaign_test_action",
        "qa_mode": True,
        "token_version": "t1",
        "test_mode": True,
        "test_recipient": TEST_RECIPIENT,
        "recipient_email": TEST_RECIPIENT,
    }
    if claims.get("qa_run_id"):
        common["qa_run_id"] = claims["qa_run_id"]
    events = [{**common, **event, "event_type": event.get("event")} for event in desired_events]
    update: dict[str, Any] = {
        "$set": {
            "campaign_id": campaign_id,
            "property_code": code,
            "owner_response.updated_at": timestamp,
        },
        "$addToSet": {"response_event_ids": {"$each": desired_ids}},
        "$push": {"response_events": {"$each": events}},
    }
    authorized = any(event.get("event") == EVENT_BY_ACTION[ACCEPT_PRICE_ACTION] for event in desired_events)
    if authorized:
        selected_event = next(event for event in desired_events if event.get("event") == event_name)
        response_fields = {
            "owner_response.status": "PRICE_AUTHORIZED",
            "owner_response.authorized_value": selected_event.get("selected_price"),
            "owner_response.current_value_at_campaign": existing.get("current_price_at_send"),
            "owner_response.selected_adjustment_type": selected_event.get("selected_adjustment_type"),
            "owner_response.selected_adjustment_pct": selected_event.get("selected_adjustment_pct"),
            "owner_response.selected_price": selected_event.get("selected_price"),
            "owner_response.recommended_adjustment_pct": selected_event.get("recommended_adjustment_pct"),
            "owner_response.recommended_price": selected_event.get("recommended_price"),
            "owner_response.authorization_status": "PRICE_AUTHORIZED",
            "owner_response.authorized_at": timestamp,
        }
        if gradual_enabled:
            response_fields.update({
                "owner_response.gradual_adjustment_pct": selected_event.get("gradual_adjustment_pct"),
                "owner_response.gradual_price": selected_event.get("gradual_price"),
            })
        update["$set"].update(response_fields)
        update["$min"] = {"owner_response.first_authorized_at": timestamp}
        update["$max"] = {"owner_response.last_authorized_at": timestamp}
    elif action == ADVISOR_ACTION:
        if owner_response.get("status") != "PRICE_AUTHORIZED":
            update["$set"] = {**update.get("$set", {}), "owner_response.selected_adjustment_type": "ADVISOR_REVIEW"}
        update["$max"] = {"owner_response.advisor_requested_at": timestamp}
    elif action == REPORT_ACTION:
        update["$max"] = {"owner_response.last_report_opened_at": timestamp}

    query = {**base_query, "response_event_ids": {"$nin": desired_ids}}
    result = ledger.update_one(query, update, upsert=False)
    modified = bool(getattr(result, "modified_count", 0))
    if modified and not authorized:
        ledger.update_one(
            {**base_query, "owner_response.status": {"$in": [None, ""]}},
            {"$set": {"owner_response.status": "PENDING"}},
            upsert=False,
        )
    record = ledger.find_one(base_query) or existing
    return {
        "event": event_name,
        "event_ids": desired_ids,
        "stored_collection": "ajuste_precio",
        "stored_record_id": str(record.get("_id") or getattr(result, "upserted_id", "") or ""),
        "timestamp": timestamp.isoformat(),
        "test_mode": True,
        "duplicate": not modified,
        "proposed_value": existing.get("display_target", existing.get("display_recommended_price")),
    }
