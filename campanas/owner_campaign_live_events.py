"""Signed per-property tracking and post-persistence notifications."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from pymongo import MongoClient, ReturnDocument

from config import Config


logger = logging.getLogger(__name__)


LIVE_ACTIONS = {
    "portal_opened": "portal_opened",
    "aceptar_rebaja": "price_authorized",
    "contactar_ejecutivo": "advisor_review_requested",
    "ver_informe": "report_opened",
}

INTERACTION_SURFACES = frozenset({"EMAIL_TEMPLATE", "OWNER_PORTAL"})
INTERACTION_CHANNELS = frozenset({"EMAIL", "WHATSAPP"})
CTA_PLACEMENTS = frozenset({"TOP", "STICKY", "ORIGINAL", "EXECUTIVE"})
OWNER_PORTAL_INTERACTION_ACTION = "owner_portal_interaction"
OWNER_PORTAL_EVENTS = frozenset({
    "section_expanded", "section_collapsed", "price_simulation_changed",
    "publication_link_clicked",
})
OWNER_PORTAL_SECTIONS = frozenset({
    "owner_portal", "commercial_activity_publications", "publication_details",
    "appraisal_details", "communal_market_details", "market_context_details",
    "recommendation_details", "price_simulation", "price_recommendation",
    "appraisal_report", "communal_market_report", "executive_contact",
})
OWNER_PORTAL_CONTROLS = frozenset({
    "portal_load", "publication_disclosure", "appraisal_details_toggle",
    "communal_market_details_toggle", "market_context_toggle",
    "recommendation_details_toggle", "price_simulation_current",
    "price_simulation_adjusted", "publication_link", "review_adjust_top",
    "review_adjust_sticky", "whatsapp_top", "whatsapp_sticky",
    "individual_appraisal_report", "communal_market_report",
})
OWNER_PUBLICATION_PORTALS = frozenset({
    "PROCASA", "PortalInmobiliario", "MercadoLibre", "TOCTOC", "Yapo",
    "Proppit", "ChilePropiedades", "EnlaceInmobiliario", "Other",
})
OWNER_NONCRITICAL_EVENTS = frozenset({
    "portal_opened", "report_opened", "cta_clicked",
    "price_confirm_page_opened", "confirmation_page_opened",
})
HISTORICAL_EMAIL_TEMPLATE_CAMPAIGNS = frozenset({
    "owner_price_sucre_wave1_20260928",
    "owner_price_sucre_wave2_20260930",
})


def derive_interaction_attribution(claims: Mapping[str, Any]) -> dict[str, str]:
    """Classify an interaction without rewriting historical event records.

    `source` was introduced by the private owner portal. Historical email
    action tokens for the two completed waves predate it, so those exact
    campaign identities can be attributed to the email template. Other
    source-less legacy flows stay UNKNOWN rather than being guessed.
    """
    source = str(claims.get("source") or "").strip().upper()
    surface = str(claims.get("interaction_surface") or "").strip().upper()
    channel = str(claims.get("interaction_channel") or "").strip().upper()

    if surface == "OWNER_PORTAL":
        resolved_channel = source if source in INTERACTION_CHANNELS else channel
        return {
            "interaction_surface": "OWNER_PORTAL",
            "interaction_channel": resolved_channel if resolved_channel in INTERACTION_CHANNELS else "UNKNOWN",
        }
    if surface == "EMAIL_TEMPLATE":
        resolved_channel = channel if channel in INTERACTION_CHANNELS else source
        return {
            "interaction_surface": "EMAIL_TEMPLATE",
            "interaction_channel": resolved_channel if resolved_channel in INTERACTION_CHANNELS else "EMAIL",
        }
    if source in INTERACTION_CHANNELS:
        # Backward compatibility: before interaction_surface was explicit,
        # only the owner portal attached source to action tokens.
        return {"interaction_surface": "OWNER_PORTAL", "interaction_channel": source}
    campaign_id = str(claims.get("campaign_id") or "")
    if campaign_id in HISTORICAL_EMAIL_TEMPLATE_CAMPAIGNS:
        return {"interaction_surface": "EMAIL_TEMPLATE", "interaction_channel": "EMAIL"}
    return {"interaction_surface": "UNKNOWN", "interaction_channel": "UNKNOWN"}


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _sign_payload(payload: Mapping[str, Any], secret: str) -> str:
    encoded = _b64(json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"p1.{encoded}.{signature}"


def _secret() -> str:
    return os.getenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "")


def issue_live_token(*, campaign_id: str, property_code: str, action: str, recipient: str, document_type: str | None = None, expires_at: int, source: str | None = None, interaction_surface: str | None = None, cta_placement: str | None = None, event_id: str | None = None, report_period: str | None = None, snapshot_hash: str | None = None, session_id: str | None = None) -> str:
    secret = _secret()
    if not secret or not campaign_id or "test" in campaign_id.casefold() or action not in {*LIVE_ACTIONS, "cta_clicked", "price_confirm_page_opened", "executive_whatsapp_clicked", OWNER_PORTAL_INTERACTION_ACTION}:
        raise ValueError("production_action_token_not_configured")
    if source is not None and source not in {"EMAIL", "WHATSAPP"}:
        raise ValueError("invalid_campaign_token_source")
    if interaction_surface is not None and interaction_surface not in INTERACTION_SURFACES:
        raise ValueError("invalid_campaign_interaction_surface")
    if cta_placement is not None and cta_placement not in CTA_PLACEMENTS:
        raise ValueError("invalid_campaign_cta_placement")
    payload = {
        "campaign_id": campaign_id,
        "property_code": str(property_code),
        "action": action,
        "recipient": str(recipient).strip().casefold(),
        "exp": int(expires_at),
        "test_mode": False,
        "event_id": str(event_id or uuid4().hex),
    }
    if source is not None:
        payload["source"] = source
    if interaction_surface is not None:
        payload["interaction_surface"] = interaction_surface
    if cta_placement is not None:
        payload["cta_placement"] = cta_placement
    if report_period is not None:
        if not re.fullmatch(r"\d{4}-\d{2}", str(report_period)):
            raise ValueError("invalid_report_period")
        payload["report_period"] = str(report_period)
    if snapshot_hash is not None:
        if len(str(snapshot_hash)) > 128:
            raise ValueError("invalid_snapshot_hash")
        payload["snapshot_hash"] = str(snapshot_hash)
    if session_id is not None:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", str(session_id)):
            raise ValueError("invalid_session_id")
        payload["session_id"] = str(session_id)
    if action == "ver_informe":
        if document_type not in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}:
            raise ValueError("invalid_live_document_type")
        payload["document_type"] = document_type
    return _sign_payload(payload, secret)


def issue_attributed_followup_token(claims: Mapping[str, Any], *, action: str) -> str:
    """Issue a follow-up action link without dropping the origin dimensions."""
    attribution = derive_interaction_attribution(claims)
    surface = attribution["interaction_surface"]
    return issue_live_token(
        campaign_id=str(claims["campaign_id"]),
        property_code=str(claims["property_code"]),
        action=action,
        recipient=str(claims["recipient"]),
        expires_at=int(claims["exp"]),
        source=(str(claims["source"]) if claims.get("source") in INTERACTION_CHANNELS else None),
        interaction_surface=(surface if surface in INTERACTION_SURFACES else None),
        cta_placement=(str(claims["cta_placement"]) if claims.get("cta_placement") in CTA_PLACEMENTS else None),
    )


def verify_live_token(token: str, *, campaign_id: str, property_code: str, recipient: str, action: str, now: datetime | None = None) -> dict[str, Any] | None:
    payload = decode_live_token(token, now=now)
    if payload is None:
        return None
    if (
        payload.get("campaign_id") != campaign_id
        or str(payload.get("property_code")) != str(property_code)
        or payload.get("recipient") != str(recipient).strip().casefold()
        or payload.get("action") != action
    ):
        return None
    return payload


def decode_live_token(token: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    secret = _secret()
    if not secret or not token:
        return None
    try:
        version, encoded, supplied = token.split(".", 2)
        expected = _b64(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
        if version != "p1" or not hmac.compare_digest(supplied, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        expiry = datetime.fromtimestamp(int(payload["exp"]), timezone.utc)
        current = now or datetime.now(timezone.utc)
        if not isinstance(payload, dict) or payload.get("test_mode") is not False or expiry <= current:
            return None
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, OverflowError):
        return None


def persist_live_event(
    db: Any, claims: Mapping[str, Any], *, event: str, action: str,
    details: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    campaign_id = str(claims["campaign_id"])
    code = str(claims["property_code"])
    event_id = hashlib.sha256(
        f"{claims.get('event_id') or uuid4().hex}|{event}".encode("utf-8")
    ).hexdigest()
    event_at = datetime.now(timezone.utc)
    payload = {
        "event_id": event_id,
        "event_at": event_at,
        "event": event,
        "action": action,
        "property_code": code,
        "campaign_id": campaign_id,
    }
    attribution = derive_interaction_attribution(claims)
    payload.update({
        "interaction_surface": attribution["interaction_surface"],
        "interaction_channel": attribution["interaction_channel"],
        "source": claims.get("source") or "UNKNOWN",
        "report_period": claims.get("report_period"),
        "snapshot_hash": claims.get("snapshot_hash"),
        "session_id": claims.get("session_id") or str(uuid4()),
        "client_event_id": claims.get("client_event_id") or str(claims.get("event_id") or event_id),
        "section_id": claims.get("section_id") or _event_section(event, claims, details),
        "control_id": claims.get("control_id") or _event_control(event, claims, details),
    })
    if details:
        payload.update(dict(details))
    source = claims.get("source")
    if source in {"EMAIL", "WHATSAPP"}:
        payload["source"] = source
    cta_placement = claims.get("cta_placement")
    if cta_placement in CTA_PLACEMENTS:
        payload["cta_placement"] = cta_placement
    payload.update(derive_interaction_attribution(claims))
    if event == "report_opened" and claims.get("document_type") in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}:
        payload["document_type"] = claims["document_type"]
    ledger = db[Config.COLLECTION_CAMPANAS_LOG]
    key = f"{campaign_id}:{code}"
    # An authorization is monotonic. The atomic update is scoped to one
    # campaign/property row and cannot change another property owner's state.
    query: dict[str, Any] = {"_id": key, "campaign_id": campaign_id, "property_code": code, "events.event_id": {"$ne": event_id}}
    event_push: Any = payload
    if event == "price_authorized":
        selected_type = str(payload.get("selected_adjustment_type") or "").upper()
        selection_event = "gradual_selected" if selected_type == "GRADUAL" else "recommended_selected"
        selection_payload = {
            **payload,
            "event_id": hashlib.sha256(
                f"{claims.get('event_id') or uuid4().hex}|{selection_event}".encode("utf-8")
            ).hexdigest(),
            "event": selection_event,
        }
        event_push = {"$each": [payload, selection_payload]}
    update: dict[str, Any] = {"$push": {"events": event_push}}
    if event == "price_authorized":
        query["authorization_status"] = {"$ne": "PRICE_AUTHORIZED"}
        update["$set"] = {
            "authorization_status": "PRICE_AUTHORIZED",
            "price_authorized_at": event_at,
            "selected_adjustment_type": payload.get("selected_adjustment_type"),
            "selected_adjustment_pct": payload.get("selected_adjustment_pct"),
            "selected_price": payload.get("selected_price"),
            "selected_price_clp": payload.get("selected_price_clp"),
            "authorized_price": payload.get("selected_price"),
        }
        if selected_type == "GRADUAL":
            update["$set"]["gradual_authorized_at"] = event_at
    elif event == "advisor_review_requested":
        cutoff = event_at - timedelta(hours=24)
        query["$or"] = [
            {"advisor_review_requested_at": {"$exists": False}},
            {"advisor_review_requested_at": {"$lte": cutoff}},
        ]
        update["$set"] = {"advisor_review_requested_at": event_at}
    elif event == "cta_clicked":
        update["$set"] = {"last_cta_clicked_at": event_at}
    elif event == "price_confirm_page_opened":
        update["$set"] = {"last_price_confirm_page_opened_at": event_at}
    elif event == "confirmation_page_opened":
        update["$set"] = {"last_confirmation_page_opened_at": event_at}
    elif event == "report_opened":
        update["$set"] = {"last_report_opened_at": event_at}
    result = ledger.update_one(query, update)
    if result.modified_count == 1:
        stored = ledger.find_one({"_id": key}) or {}
        return stored, True
    existing = ledger.find_one({"_id": key, "events.event_id": event_id}) or ledger.find_one({"_id": key})
    if not existing:
        raise LookupError("prepared_campaign_row_missing")
    return existing, False


def log_owner_portal_telemetry(
    status: str, *, claims: Mapping[str, Any] | None = None, event: str = "",
    client_event_id: str = "", portal: str = "", error: Exception | None = None,
) -> None:
    """Emit structured, PII-free telemetry diagnostics for owner portal paths."""
    safe_claims = claims if isinstance(claims, Mapping) else {}
    logger.info(
        "[OWNER_PORTAL_TELEMETRY] status=%s campaign_id=%s property_code=%s event=%s "
        "client_event_id=%s portal=%s error_type=%s",
        status,
        str(safe_claims.get("campaign_id") or "")[:96],
        str(safe_claims.get("property_code") or "")[:32],
        str(event or "")[:48], str(client_event_id or "")[:64],
        str(portal or "")[:48], type(error).__name__ if error else "",
    )


def persist_noncritical_live_event(
    db: Any, claims: Mapping[str, Any], *, event: str, action: str,
    details: Mapping[str, Any] | None = None,
) -> bool:
    """Persist navigation/open events best-effort; commercial writes stay strict."""
    if event not in OWNER_NONCRITICAL_EVENTS:
        log_owner_portal_telemetry(
            "rejected", claims=claims, event=event,
            client_event_id=str(claims.get("client_event_id") or ""),
        )
        return False
    try:
        _stored, inserted = persist_live_event(db, claims, event=event, action=action, details=details)
        log_owner_portal_telemetry(
            "accepted" if inserted else "duplicate", claims=claims, event=event,
            client_event_id=str(claims.get("client_event_id") or ""),
        )
        return True
    except Exception as exc:
        log_owner_portal_telemetry(
            "mongo_error", claims=claims, event=event,
            client_event_id=str(claims.get("client_event_id") or ""), error=exc,
        )
        return False


def _event_section(event: str, claims: Mapping[str, Any], details: Mapping[str, Any] | None) -> str:
    if details and details.get("section_id") in OWNER_PORTAL_SECTIONS:
        return str(details["section_id"])
    if event == "report_opened":
        return "communal_market_report" if (claims.get("document_type") == "COMMUNAL_MARKET_REPORT") else "appraisal_report"
    if event in {"cta_clicked", "price_confirm_page_opened", "confirmation_page_opened", "price_authorized", "recommended_selected", "gradual_selected"}:
        return "price_recommendation"
    if event in {"executive_whatsapp_clicked", "advisor_review_requested"}:
        return "executive_contact"
    return "owner_portal"


def _event_control(event: str, claims: Mapping[str, Any], details: Mapping[str, Any] | None) -> str:
    if details and details.get("control_id") in OWNER_PORTAL_CONTROLS:
        return str(details["control_id"])
    placement = str(claims.get("cta_placement") or "").upper()
    if event in {"cta_clicked", "price_confirm_page_opened", "confirmation_page_opened", "price_authorized", "recommended_selected", "gradual_selected"}:
        return "review_adjust_sticky" if placement == "STICKY" else "review_adjust_top"
    if event == "executive_whatsapp_clicked":
        return "whatsapp_sticky" if placement == "STICKY" else "whatsapp_top"
    if event == "report_opened":
        return "communal_market_report" if claims.get("document_type") == "COMMUNAL_MARKET_REPORT" else "individual_appraisal_report"
    return "portal_load"


def persist_owner_portal_interaction(db: Any, claims: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Persist only a validated in-page event, idempotent by client event ID."""
    event = str(payload["event"])
    client_event_id = str(payload["client_event_id"])
    event_id = hashlib.sha256(f"{claims['campaign_id']}|{claims['property_code']}|{event}|{client_event_id}".encode()).hexdigest()
    event_at = datetime.now(timezone.utc)
    attribution = derive_interaction_attribution(claims)
    stored = {
        "event_id": event_id, "event_at": event_at, "event": event, "action": event,
        "campaign_id": str(claims["campaign_id"]), "property_code": str(claims["property_code"]),
        "interaction_surface": "OWNER_PORTAL", "interaction_channel": attribution["interaction_channel"],
        "source": claims.get("source") or "UNKNOWN", "report_period": claims.get("report_period"),
        "snapshot_hash": claims.get("snapshot_hash"), "section_id": payload["section_id"],
        "control_id": payload["control_id"], "session_id": payload["session_id"],
        "client_event_id": client_event_id,
    }
    for field in ("previous_state", "target_state", "selected_adjustment_pct", "external_portal", "expanded"):
        if field in payload:
            stored[field] = payload[field]
    key = f"{stored['campaign_id']}:{stored['property_code']}"
    result = db[Config.COLLECTION_CAMPANAS_LOG].update_one(
        {
            "_id": key, "campaign_id": stored["campaign_id"],
            "property_code": stored["property_code"],
            "owner_email": claims["recipient"],
            "events.event_id": {"$ne": event_id},
        },
        {"$push": {"events": stored}},
    )
    if result.modified_count == 1:
        return {}, True
    existing = db[Config.COLLECTION_CAMPANAS_LOG].find_one({
        "_id": key, "campaign_id": stored["campaign_id"],
        "property_code": stored["property_code"], "owner_email": claims["recipient"],
    })
    if not existing:
        raise LookupError("prepared_campaign_row_missing")
    if not any(
        isinstance(item, Mapping) and item.get("event_id") == event_id
        for item in existing.get("events", [])
    ):
        raise LookupError("owner_portal_interaction_not_persisted")
    return existing, False


def persist_owner_whatsapp_click(
    db: Any,
    claims: Mapping[str, Any],
    *,
    details: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Atomically append every owner WhatsApp intent with a per-property sequence."""
    campaign_id = str(claims["campaign_id"])
    code = str(claims["property_code"])
    recipient = str(claims.get("recipient") or "").strip().casefold()
    event_at = datetime.now(timezone.utc)
    event_id = uuid4().hex
    secret = _secret()
    if not recipient or not secret:
        raise ValueError("owner_whatsapp_identity_unavailable")
    owner_hash = hmac.new(secret.encode("utf-8"), recipient.encode("utf-8"), hashlib.sha256).hexdigest()
    attribution = derive_interaction_attribution(claims)
    placement = str(claims.get("cta_placement") or "").upper()
    if placement not in {"TOP", "STICKY"}:
        raise ValueError("invalid_owner_whatsapp_placement")

    payload = {
        **dict(details),
        "event_id": event_id,
        "event_at": event_at,
        "event": "executive_whatsapp_clicked",
        "event_type": "OWNER_WHATSAPP_CLICK",
        "action": "executive_whatsapp_clicked",
        "intent": "INTENT_TO_CONTACT",
        "cta_type": "WHATSAPP",
        "placement": placement,
        "cta_placement": placement,
        "property_code": code,
        "campaign_id": campaign_id,
        "owner_key": owner_hash,
        "owner_identity_hash": owner_hash,
        "source": claims.get("source"),
        "channel": attribution["interaction_channel"],
        "interaction_surface": "OWNER_PORTAL",
        "interaction_channel": attribution["interaction_channel"],
        "qa_mode": bool(details.get("qa_mode", False)),
    }

    ledger = db[Config.COLLECTION_CAMPANAS_LOG]
    key = f"{campaign_id}:{code}"
    client_event_id = str(details.get("client_event_id") or "").strip()
    if client_event_id:
        already_persisted = ledger.find_one({
            "_id": key, "campaign_id": campaign_id, "property_code": code,
            "events.client_event_id": client_event_id,
        })
        if already_persisted:
            return already_persisted, False
    current = ledger.find_one({"_id": key, "campaign_id": campaign_id, "property_code": code},
        {"owner_whatsapp_click_count": 1, "events": 1})
    if not current:
        raise LookupError("prepared_campaign_row_missing")
    if "owner_whatsapp_click_count" not in current:
        # Seed the counter from historical clicks once, without rewriting events.
        prior_events = current.get("events") if isinstance(current.get("events"), list) else []
        historical_count = sum(
            1 for item in prior_events
            if isinstance(item, Mapping) and item.get("event") == "executive_whatsapp_clicked"
        )
        ledger.update_one(
            {"_id": key, "campaign_id": campaign_id, "property_code": code,
             "owner_whatsapp_click_count": {"$exists": False}},
            {"$set": {"owner_whatsapp_click_count": historical_count}},
        )

    # The counter increment is atomic, so concurrent TOP/STICKY clicks receive
    # distinct sequence numbers. The event must persist before redirecting.
    sequenced_row = ledger.find_one_and_update(
        {"_id": key, "campaign_id": campaign_id, "property_code": code},
        {"$inc": {"owner_whatsapp_click_count": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if not sequenced_row:
        raise LookupError("prepared_campaign_row_missing")
    sequence = int(sequenced_row.get("owner_whatsapp_click_count") or 0)
    payload["click_sequence_number"] = sequence
    payload["is_first_whatsapp_click"] = sequence == 1
    append_query = {
        "_id": key, "campaign_id": campaign_id, "property_code": code,
        "events.event_id": {"$ne": event_id},
    }
    if client_event_id:
        append_query["events.client_event_id"] = {"$ne": client_event_id}
    result = ledger.update_one(
        append_query,
        {"$push": {"events": payload}},
    )
    if result.modified_count != 1:
        if client_event_id:
            duplicate = ledger.find_one({"_id": key, "events.client_event_id": client_event_id})
            if duplicate:
                return duplicate
        raise LookupError("owner_whatsapp_click_event_not_persisted")
    stored = ledger.find_one({"_id": key, "campaign_id": campaign_id, "property_code": code})
    if not stored:
        raise LookupError("prepared_campaign_row_missing")
    return stored, True


def notify_internal_after_persist(stored: Mapping[str, Any], *, event: str) -> bool:
    """Send internal email only after the event is present in the ledger."""
    try:
        from .email_service import enviar_notificacion_owner_campaign

        return bool(enviar_notificacion_owner_campaign(
            owner_name=str(stored.get("owner_name") or "Sin nombre"),
            owner_email=str(stored.get("owner_email") or ""),
            property_code=str(stored.get("property_code") or ""),
            current_price=stored.get("current_price"),
            recommended_price=stored.get("recommended_price"),
            adjustment_pct=stored.get("recommended_adjustment_pct"),
            selected_adjustment_type=stored.get("selected_adjustment_type"),
            selected_adjustment_pct=stored.get("selected_adjustment_pct"),
            selected_price=stored.get("selected_price"),
            action=event,
            executive_name=str(stored.get("executive_name") or ""),
            executive_email=str(stored.get("executive_email") or ""),
            boss_cc=str(stored.get("boss_cc") or ""),
        ))
    except Exception:
        return False


def _event_identifier(claims: Mapping[str, Any], event: str) -> str:
    return hashlib.sha256(f"{claims.get('event_id')}|{event}".encode()).hexdigest()


def record_notification_result(db: Any, *, campaign_id: str, property_code: str, event: str, channel: str, result: Mapping[str, Any]) -> None:
    event_id = str(result.get("event_id") or "")
    field = f"notifications.{channel}.{event}"
    db[Config.COLLECTION_CAMPANAS_LOG].update_one(
        {"_id": f"{campaign_id}:{property_code}", "campaign_id": campaign_id, "property_code": property_code},
        {"$set": {
            f"{field}.status": str(result.get("status") or "unknown"),
            f"{field}.attempted_at": datetime.now(timezone.utc),
            f"{field}.provider_message_id": result.get("provider_message_id"),
            f"{field}.event_id": event_id,
        }},
    )


def _campaign_whatsapp_text(row: Mapping[str, Any], event: str, event_at: datetime) -> str:
    operation = str(row.get("operation") or "VENTA").upper()
    monthly = operation == "ARRIENDO"
    unit = "UF/mes" if monthly else "UF"
    clp_unit = "CLP/mes" if monthly else "CLP"

    def price(value: Any, *, clp: bool = False) -> str:
        if value in (None, ""):
            return "No disponible"
        try:
            number = float(value)
            rendered = f"{number:,.0f}".replace(",", ".")
        except (TypeError, ValueError):
            rendered = str(value)
        return ("$ " + rendered + " " + clp_unit) if clp else (rendered + " " + unit)

    current = price(row.get("current_price"))
    recommended = price(row.get("recommended_price"))
    selected = price(row.get("selected_price"))
    current_clp = row.get("current_price_clp")
    recommended_clp = row.get("recommended_price_clp")
    selected_clp = row.get("selected_price_clp")
    property_name = str(row.get("property_type") or "Propiedad")
    commune = str(row.get("commune") or "Comuna no disponible")
    owner = str(row.get("owner_name") or "Sin nombre")
    executive = str(row.get("executive_name") or "Sin ejecutivo")
    event_text = event_at.astimezone(timezone.utc).strftime("%d-%m-%Y %H:%M UTC")
    if event == "price_authorized":
        selected_type = str(row.get("selected_adjustment_type") or "RECOMMENDED").upper()
        gradual = selected_type == "GRADUAL"
        lines = [
            "✅ AJUSTE GRADUAL CONFIRMADO" if gradual else "✅ AJUSTE DE PRECIO CONFIRMADO", "",
            f"Código: {row.get('property_code')}", f"{property_name} · {commune}",
            f"Propietario: {owner}", "", f"Precio actual: {price(row.get('previous_price', row.get('current_price')))}",
            f"Recomendación PROCASA: {row.get('recommended_adjustment_pct')}% → {recommended}",
            (
                f"Ajuste gradual autorizado: {row.get('selected_adjustment_pct')}% → {selected}"
                if gradual else
                f"Ajuste recomendado autorizado: {row.get('selected_adjustment_pct')}% → {selected}"
            ),
            f"Nuevo precio autorizado: {selected}", f"Ejecutivo: {executive}",
            f"Fecha: {event_text}", f"Campaña: {row.get('campaign_id')}",
        ]
        if row.get("previous_price_clp", current_clp) is not None:
            lines.insert(7, f"Precio actual: {price(row.get('previous_price_clp', current_clp), clp=True)}")
        if recommended_clp is not None:
            lines.insert(9, f"Recomendación PROCASA CLP: {price(recommended_clp, clp=True)}")
        if selected_clp is not None:
            lines.insert(12, f"Nuevo precio autorizado CLP: {price(selected_clp, clp=True)}")
        return "\n".join(lines)
    return "\n".join([
        "📞 PROPIETARIO SOLICITA CONTACTO", "",
        f"Código: {row.get('property_code')}", f"{property_name} · {commune}",
        f"Propietario: {owner}", "", f"Precio actual: {current}",
        f"Recomendación PROCASA: {row.get('recommended_adjustment_pct')}% → {recommended}",
        f"Ejecutivo responsable: {executive}",
        "Motivo: Quiere revisar el ajuste antes de autorizarlo.",
        f"Fecha: {event_text}", f"Campaña: {row.get('campaign_id')}",
    ])


def _send_admin_whatsapp(row: Mapping[str, Any], event: str, event_at: datetime) -> dict[str, Any]:
    phone = str(getattr(Config, "OWNER_CAMPAIGN_ADMIN_WHATSAPP", "") or "").strip()
    if not phone or not Config.WASENDER_TOKEN:
        return {"status": "not_configured", "provider_message_id": None}
    try:
        import asyncio
        from chatbot.whatsapp_client import send_whatsapp_message_detailed

        result = asyncio.run(send_whatsapp_message_detailed(
            phone, _campaign_whatsapp_text(row, event, event_at),
        ))
        status = "sent" if result.get("success") else str(result.get("delivery_status") or "failed")
        return {"status": status, "provider_message_id": result.get("provider_message_id")}
    except Exception as exc:
        return {"status": "failed", "error_type": type(exc).__name__, "provider_message_id": None}


def notify_campaign_channels_after_persist(db: Any, *, stored: Mapping[str, Any], event: str, claims: Mapping[str, Any]) -> None:
    """Independent internal channels run only after the persisted event is read back."""
    campaign_id = str(claims["campaign_id"])
    code = str(claims["property_code"])
    event_id = _event_identifier(claims, event)
    event_at = next(
        (item.get("event_at") for item in stored.get("events", [])
         if isinstance(item, Mapping) and item.get("event_id") == event_id
         and isinstance(item.get("event_at"), datetime)),
        datetime.now(timezone.utc),
    )
    ledger = db[Config.COLLECTION_CAMPANAS_LOG]
    for channel in ("email", "admin_whatsapp"):
        state_path = f"notifications.{channel}.{event}.status"
        reserve = ledger.update_one(
            {
                "_id": f"{campaign_id}:{code}",
                "campaign_id": campaign_id,
                "property_code": code,
                state_path: {"$nin": ["sending", "sent", "accepted"]},
            },
            {"$set": {
                state_path: "sending",
                f"notifications.{channel}.{event}.event_id": event_id,
                f"notifications.{channel}.{event}.started_at": datetime.now(timezone.utc),
            }},
        )
        if reserve.modified_count != 1:
            continue
        if channel == "email":
            succeeded = notify_internal_after_persist(stored, event=event)
            result = {"status": "sent" if succeeded else "failed", "provider_message_id": None, "event_id": event_id}
        else:
            result = _send_admin_whatsapp(stored, event, event_at)
            result["event_id"] = event_id
        record_notification_result(
            db, campaign_id=campaign_id, property_code=code,
            event=event, channel=channel, result=result,
        )


def record_live_action(*, campaign_id: str, property_code: str, recipient: str, action: str, token: str) -> tuple[dict[str, Any], bool]:
    claims = verify_live_token(
        token, campaign_id=campaign_id, property_code=property_code,
        recipient=recipient, action=action,
    )
    if claims is None:
        raise ValueError("invalid_or_expired_campaign_token")
    if action == "aceptar_rebaja":
        # The action handler must persist the owner's explicit choice and price.
        # A generic event cannot safely authorize the recommendation by default.
        raise ValueError("price_authorization_requires_selected_adjustment")
    client = MongoClient(Config.MONGO_URI)
    try:
        db = client[Config.DB_NAME]
        stored, inserted = persist_live_event(
            db, claims, event=LIVE_ACTIONS[action], action=action,
        )
        # Read-after-write verifies persistence before the notification attempt.
        persisted = db[Config.COLLECTION_CAMPANAS_LOG].find_one({
            "_id": f"{campaign_id}:{property_code}",
            "events.event_id": _event_identifier(claims, LIVE_ACTIONS[action]),
        })
        if inserted and persisted and LIVE_ACTIONS[action] in {"price_authorized", "advisor_review_requested"}:
            notify_campaign_channels_after_persist(
                db, stored=persisted, event=LIVE_ACTIONS[action], claims=claims,
            )
        return stored, inserted
    finally:
        client.close()
