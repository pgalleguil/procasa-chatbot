"""One-collection price-drop campaign ledger embedded in canonical leads.

Only `leads` is mutated. Prop360 and its price history are read-only.
No sending at import/startup, and the live provider requires explicit gates.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from pymongo import ReturnDocument

from .phone_utils import normalize_phone_strict, phone_identity_variants, has_real_phone
from .price_drop_campaign_classifier import (
    classify_price_drop_reply, lead_must_not_be_contacted, ALTERNATIVES,
)
from .price_drop_campaign_filters import (
    effective_temperature, lead_age_details, normalize_lead_max_age_days,
    normalize_temperature_filter,
    temperature_filter_matches, temperature_review_is_required,
)
from .price_drop_campaign_real import _lead_id, _message

LEADS = "leads"
ROOT = "price_drop_v2.campaigns"
CONTEXT_FLAG = "PRICE_DROP_LEADS_V2_CONTEXT_ENABLED"
SEND_FLAG = "PRICE_DROP_LEADS_V2_SEND_ENABLED"
APPROVED_ID_FLAG = "PRICE_DROP_LEADS_V2_APPROVED_ID"
TEST_PHONE_FLAG = "PRICE_DROP_PILOT_TEST_PHONE"
TEST_LEAD_ID_FLAG = "PRICE_DROP_LEADS_V2_TEST_LEAD_ID"
TEST_REAUTH_EVIDENCE_FLAG = "PRICE_DROP_LEADS_V2_TEST_REAUTH_EVIDENCE"
TEST_QA_REAUTHORIZATIONS = "price_drop_v2.qa_reauthorizations"
TEST_NOTICE_FLAG = "PRICE_DROP_LEADS_V2_TEST_NOTICES_ENABLED"
ADMIN_PHONE_FLAG = "PRICE_DROP_LEADS_V2_ADMIN_PHONE"
EXEC_NOTICE_ENABLED_FLAG = "PRICE_DROP_LEADS_V2_EXECUTIVE_NOTICE_ENABLED"
REAL_ADMIN_NOTICE_FLAG = "PRICE_DROP_LEADS_V2_REAL_ADMIN_NOTICES_ENABLED"
REAL_PILOT_ENABLED_FLAG = "PRICE_DROP_LEADS_V2_REAL_PILOT_ENABLED"
REAL_PILOT_RECIPIENTS_FLAG = "PRICE_DROP_LEADS_V2_REAL_PILOT_RECIPIENTS"
REAL_CAMPAIGNS_ENABLED_FLAG = "PRICE_DROP_LEADS_V2_REAL_CAMPAIGNS_ENABLED"
WHATSAPP_CONTACT_REVIEWED_FLAG = "PRICE_DROP_LEADS_V2_WHATSAPP_CONTACT_REVIEWED"
REAL_PILOT_CAMPAIGN_ID = "pilot_real_rebajas_20261109_v2"
# Narrow price windows for the approved three-property proposal. These are
# public property values, not customer identifiers. The three recipient IDs
# must be configured separately, after a human reviews the contact basis.
REAL_PILOT_PRICE_WINDOWS = {
    "6436": ("UF", 2790, 2690),
    "6377": ("UF", 5123, 5074),
    "16479": ("CLP", 160000000, 152000000),
}

# The real pilot is limited to a follow-up requested by the lead about the
# same property. This is distinct from a blanket marketing opt-in.
DIRECT_RESPONSE_BASIS_TYPE = "PRIOR_PROPERTY_INQUIRY"

# Explicitly allowlist the one approved pilot administrator. This phone is
# not a customer audience and must never be replaced by a property's owner.
APPROVED_ADMIN_PHONE = "+56983219804"


def executive_notices_enabled() -> bool:
    """Frozen by default; preserving the code is not permission to notify."""
    return os.getenv(EXEC_NOTICE_ENABLED_FLAG, "").lower() in {"1", "true", "yes"}

ACTIVE_STATES = ("SENDING", "SENT", "RESPONDED", "DECLINED", "OPTED_OUT", "DELIVERY_UNKNOWN")
VALID_CODE = re.compile(r"^\d{4,6}$")
VALID_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{5,79}$")


class PilotBlocked(ValueError):
    pass


def is_hot_campaign_eligible(lead: dict) -> bool:
    """Only current canonical HOT leads in open stages may receive campaigns.

    Read the CRM-persisted canonical value, never reconstruct temperature from
    old interests, "hot_since", assignment history or an outreach response.
    """
    if str(lead.get("lead_temperature_effective") or "").strip().upper() != "HOT":
        return False
    # Either CRM stage mirror can veto eligibility. Never allow an open
    # stale mirror to mask CLOSED_LOST / CLOSED_WON in the other field.
    return all(
        str(lead.get(field) or "").strip().upper() not in {"CLOSED_LOST", "CLOSED_WON"}
        for field in ("pipeline_stage", "stage")
    )


def is_open_campaign_eligible(lead: dict) -> bool:
    """Historical property inquiry qualifies without HOT; closed lead does not."""
    return all(
        str(lead.get(field) or "").strip().upper() not in {"CLOSED_LOST", "CLOSED_WON"}
        for field in ("pipeline_stage", "stage")
    )


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise PilotBlocked("datetime_required")
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _same_phone(lead: dict, phone: str) -> bool:
    actual = lead.get("contact_phone_normalized") or lead.get("phone")
    return normalize_phone_strict(actual or "") == normalize_phone_strict(phone or "")


def _contact_suppression_reasons(lead: dict | None) -> set[str]:
    """Explain the existing contact guard without weakening it globally."""
    if not isinstance(lead, dict):
        return set()
    reasons: set[str] = set()
    if lead.get("conversation_status") in {
        "STOPPED_BY_CLIENT", "STOPPED", "BLOCKED", "BLOCKED_EXTERNAL_BROKER",
    }:
        status_reason = str(lead.get("conversation_status_reason") or "")
        status_evidence = str(lead.get("conversation_status_evidence") or "")
        status_classification = classify_price_drop_reply(status_evidence)
        if (status_reason == "already_found_property"
                or (status_classification.label == "BAJA"
                    and status_classification.reason == "already_found_property")):
            reasons.add("completed_search")
        else:
            reasons.add("contact_block")
    if ((lead.get("price_drop_v2") or {}).get("opted_out") is True
            or (lead.get("price_drop_campaign_v1") or {}).get("opted_out") is True):
        reasons.add("explicit_opt_out")
    for campaign in ((lead.get("price_drop_v2") or {}).get("campaigns") or []):
        if not isinstance(campaign, dict):
            continue
        if campaign.get("status") == "OPTED_OUT":
            reasons.add("explicit_opt_out")
        classification = classify_price_drop_reply(campaign.get("last_customer_text") or "")
        if classification.label == "BAJA":
            reasons.add("completed_search" if classification.reason == "already_found_property"
                        else "explicit_opt_out")
    for message in lead.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") not in {"user", "customer"}:
            continue
        classification = classify_price_drop_reply(message.get("content") or "")
        if classification.label == "BAJA":
            reasons.add("completed_search" if classification.reason == "already_found_property"
                        else "explicit_opt_out")
    return reasons


def _test_qa_webhook_canonical_lead(db, *, lead_id: Any, phone: str) -> dict | None:
    """Require the same unique identity query used by the V2 inbound resolver."""
    canonical = normalize_phone_strict(phone)
    if not canonical:
        return None
    matches = list(db[LEADS].find(
        {"$or": [{"phone": canonical}, {"contact_phone_normalized": canonical}]},
        {"_id": 1},
    ).limit(2))
    try:
        lid = _lead_id(lead_id)
    except (TypeError, ValueError):
        return None
    if len(matches) != 1 or matches[0].get("_id") != lid:
        return None
    lead = db[LEADS].find_one({"_id": lid})
    return lead if lead and _same_phone(lead, canonical) else None


def _test_qa_reauthorization(lead: dict, *, campaign_id: str, phone: str,
                             property_code: str, now: datetime,
                             expected_authorization_id: str | None = None,
                             allow_consumed: bool = False) -> dict | None:
    """Validate a single admin QA exception for completed-search suppression.

    Explicit STOP/opt-out and other contact blocks remain non-overridable.
    This helper is never used for real-customer campaigns.
    """
    canonical = normalize_phone_strict(phone)
    if (canonical != APPROVED_ADMIN_PHONE
            or canonical != normalize_phone_strict(os.getenv(TEST_PHONE_FLAG, ""))
            or canonical != normalize_phone_strict(os.getenv(ADMIN_PHONE_FLAG, ""))
            or property_code != "6436"
            or not campaign_id.startswith("test_leads_6436_")
            or not _same_phone(lead, canonical)):
        return None
    if str(lead.get("_id")) != str(os.getenv(TEST_LEAD_ID_FLAG, "")).strip():
        return None
    qa = (lead.get("price_drop_v2") or {}).get("qa_session") or {}
    if qa.get("campaign_id") != campaign_id:
        return None
    allowed_statuses = {"ACTIVE", "CONSUMED"} if allow_consumed else {"ACTIVE"}
    records = [row for row in ((lead.get("price_drop_v2") or {}).get(
        "qa_reauthorizations") or [])
        if isinstance(row, dict) and row.get("campaign_id") == campaign_id]
    if len(records) != 1:
        return None
    authorization = records[0]
    if (authorization.get("status") not in allowed_statuses
            or authorization.get("phone_normalized") != canonical
            or authorization.get("property_code") != "6436"
            or authorization.get("max_messages") != 1
            or not str(authorization.get("operator") or "").strip()
            or len(str(authorization.get("evidence_reference") or "").strip()) < 8
            or not authorization.get("authorization_id")):
        return None
    expires_at = authorization.get("expires_at")
    if not isinstance(expires_at, datetime) or utc(expires_at) <= utc(now):
        return None
    reasons = _contact_suppression_reasons(lead)
    if reasons and reasons != {"completed_search"}:
        return None
    if lead_must_not_be_contacted(lead) and reasons != {"completed_search"}:
        return None
    if (expected_authorization_id is not None
            and authorization.get("authorization_id") != expected_authorization_id):
        return None
    return authorization


def authorize_test_qa_reauthorization(db, *, lead_id: str, campaign_id: str,
                                      phone: str, operator: str,
                                      evidence_reference: str, now=None) -> dict:
    """Append one expiring admin QA authorization without clearing lead history.

    This narrow exception is only for a recorded completed-search suppression
    on the allowlisted admin test phone. Global STOP/opt-out protections remain
    in force for every other reason and every real-customer campaign.
    """
    canonical = normalize_phone_strict(phone)
    operator = str(operator or "").strip()
    evidence = str(evidence_reference or "").strip()
    if (canonical != APPROVED_ADMIN_PHONE
            or canonical != normalize_phone_strict(os.getenv(TEST_PHONE_FLAG, ""))
            or canonical != normalize_phone_strict(os.getenv(ADMIN_PHONE_FLAG, ""))
            or not VALID_ID.fullmatch(campaign_id)
            or not campaign_id.startswith("test_leads_6436_")
            or not operator or len(evidence) < 8):
        return {"status": "BLOCKED", "reason": "qa_reauthorization_not_authorized"}
    try:
        lid = _lead_id(lead_id)
    except (TypeError, ValueError):
        return {"status": "BLOCKED", "reason": "qa_canonical_lead_id_invalid"}
    if str(lid) != str(os.getenv(TEST_LEAD_ID_FLAG, "")).strip():
        return {"status": "BLOCKED", "reason": "qa_canonical_lead_id_not_pinned"}
    lead = _test_qa_webhook_canonical_lead(db, lead_id=lid, phone=canonical)
    if not lead:
        return {"status": "BLOCKED", "reason": "qa_canonical_lead_ambiguous_or_mismatched"}
    if campaign_id in {
            str(row.get("campaign_id") or "")
            for row in ((lead.get("price_drop_v2") or {}).get("campaigns") or [])
            if isinstance(row, dict)}:
        return {"status": "BLOCKED", "reason": "qa_campaign_already_exists"}
    campaigns = (lead.get("price_drop_v2") or {}).get("campaigns") or []
    if any(row.get("status") in {
            "PREPARED", "APPROVED", "SENDING", "DELIVERY_UNKNOWN"}
            for row in campaigns if isinstance(row, dict)):
        return {"status": "BLOCKED", "reason": "qa_unresolved_campaign_exists"}
    reasons = _contact_suppression_reasons(lead)
    if reasons != {"completed_search"} or not lead_must_not_be_contacted(lead):
        return {"status": "BLOCKED", "reason": "qa_suppression_not_completed_search_only"}

    from uuid import uuid4
    stamp = utc(now or datetime.now(timezone.utc))
    prior_session = (lead.get("price_drop_v2") or {}).get("qa_session")
    authorization = {
        "authorization_id": str(uuid4()),
        "campaign_id": campaign_id,
        "phone_normalized": canonical,
        "property_code": "6436",
        "max_messages": 1,
        "status": "ACTIVE",
        "operator": operator[:80],
        "evidence_reference": evidence[:240],
        "authorized_at": stamp,
        "expires_at": stamp + timedelta(hours=24),
    }
    qa_session = {
        "campaign_id": campaign_id,
        "started_at": stamp,
        "operator": operator[:80],
        "reauthorization_id": authorization["authorization_id"],
        "history_preserved": True,
    }
    query = {
        "_id": lid,
        TEST_QA_REAUTHORIZATIONS: {"$not": {"$elemMatch": {
            "campaign_id": campaign_id,
        }}},
        ROOT: {"$not": {"$elemMatch": {"campaign_id": campaign_id}}},
    }
    update = {
        "$set": {"price_drop_v2.qa_session": qa_session},
        "$push": {TEST_QA_REAUTHORIZATIONS: authorization},
    }
    if prior_session:
        update["$push"]["price_drop_v2.qa_session_archive"] = prior_session
    result = db[LEADS].update_one(query, update)
    if result.modified_count != 1:
        return {"status": "BLOCKED", "reason": "qa_reauthorization_concurrent_change"}
    return {"status": "AUTHORIZED", "campaign_id": campaign_id,
            "authorization_id": authorization["authorization_id"],
            "expires_at": authorization["expires_at"],
            "recipient_last4": canonical[-4:], "max_messages": 1,
            "historical_messages_preserved": len(lead.get("messages") or []),
            "historical_campaigns_preserved": len(campaigns)}


def _real_campaign_history_blocker(lead: dict, campaign: dict) -> str | None:
    code = str(campaign.get("property_code") or "")
    campaign_id = str(campaign.get("campaign_id") or "")
    event_id = str(campaign.get("event_id") or campaign_id)
    campaigns = (lead.get("price_drop_v2") or {}).get("campaigns") or []
    if any(row.get("campaign_id") != campaign_id
           and str(row.get("property_code") or "") == code
           and row.get("status") == "DECLINED" for row in campaigns):
        return "property_previously_declined"
    for message in lead.get("messages") or []:
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        if (metadata.get("campaign_id") in {campaign_id, event_id}
                or metadata.get("price_drop_event_id") == event_id
                or event_id in (metadata.get("history_event_ids") or [])):
            return "event_already_in_message_history"
    return None


def _text_sender_window_blocker(lead: dict, now: datetime) -> str | None:
    """Apply the 24-hour rule only to the official Cloud API channel.

    The current WaSender integration posts plain text to WaSender's active
    WhatsApp session endpoint. It has no template endpoint or Cloud API
    conversation-window field. Keep explicit contact authorization and opt-out
    checks in the V2 send path; do not treat the Cloud API's technical template
    gate as a WaSender endpoint restriction.
    """
    mode = os.getenv("PRICE_DROP_WHATSAPP_CHANNEL_MODE", "wasenderapi").strip().lower()
    if mode == "wasenderapi":
        return None
    if mode != "official":
        return "whatsapp_channel_mode_invalid"
    inbound = []
    for message in lead.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") not in {"user", "customer"}:
            continue
        value = message.get("timestamp")
        if isinstance(value, datetime):
            parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                continue
        else:
            continue
        if parsed.utcoffset() is not None:
            inbound.append(parsed.astimezone(timezone.utc))
    if not inbound:
        return "whatsapp_24_hour_window_expired_template_required"
    latest = max(inbound)
    if latest > now or now - latest > timedelta(hours=24):
        return "whatsapp_24_hour_window_expired_template_required"
    return None


def _real_pilot_send_blocker(db, lead: dict, campaign: dict) -> str | None:
    """Fail closed until exactly three independently reviewed recipients are set.

    This is a temporary three-property pilot release gate. The recipient
    allowlist is configured at runtime, never embedded in this public source.
    The gate never prepares/approves a campaign or sends on startup.
    """
    if os.getenv(REAL_PILOT_ENABLED_FLAG, "").strip().lower() not in {"1", "true", "yes"}:
        return "REAL_SEND_LOCKED_PENDING_ACCEPTANCE"

    rows = [row.strip() for row in os.getenv(
        REAL_PILOT_RECIPIENTS_FLAG, "").split(";") if row.strip()]
    if len(rows) != 3:
        return "REAL_SEND_NOT_ALLOWLISTED"
    allowed: dict[str, str] = {}
    for row in rows:
        parts = [part.strip() for part in row.split(":")]
        if len(parts) != 2:
            return "REAL_SEND_NOT_ALLOWLISTED"
        lead_id, property_code = parts
        if (not re.fullmatch(r"[0-9a-fA-F]{24}", lead_id)
                or property_code not in REAL_PILOT_PRICE_WINDOWS
                or lead_id.lower() in allowed
                or property_code in allowed.values()):
            return "REAL_SEND_NOT_ALLOWLISTED"
        allowed[lead_id.lower()] = property_code
    if set(allowed.values()) != set(REAL_PILOT_PRICE_WINDOWS):
        return "REAL_SEND_NOT_ALLOWLISTED"

    code = str(campaign.get("property_code") or "")
    if allowed.get(str(lead.get("_id") or "").lower()) != code:
        return "REAL_SEND_NOT_ALLOWLISTED"
    actual_window = (
        campaign.get("currency") or "UF",
        campaign.get("old_price_clp") if code == "16479" else campaign.get("old_price_uf"),
        campaign.get("new_price_clp") if code == "16479" else campaign.get("new_price_uf"),
    )
    if actual_window != REAL_PILOT_PRICE_WINDOWS[code]:
        return "REAL_SEND_WRONG_PRICE_OR_PROPERTY"

    if (campaign.get("contact_basis_reviewed") is not True
            or campaign.get("contact_basis_type") != DIRECT_RESPONSE_BASIS_TYPE
            or not str(campaign.get("contact_basis") or "").strip()
            or not str(campaign.get("approved_by") or "").strip()
            or not isinstance(campaign.get("approved_at"), datetime)):
        return "REAL_SEND_CONTACT_BASIS_OR_APPROVAL_MISSING"
    if (campaign.get("whatsapp_contact_reviewed") is not True
            or not str(campaign.get("whatsapp_contact_evidence") or "").strip()):
        return "WHATSAPP_CONTACT_AUTHORIZATION_MISSING"
    if campaign.get("simulated_prior_inquiry") is not False:
        return "REAL_SEND_SIMULATED_INQUIRY"
    history_blocker = _real_campaign_history_blocker(lead, campaign)
    if history_blocker:
        return history_blocker

    phone = normalize_phone_strict(campaign.get("phone_normalized") or "")
    if (not phone or not has_real_phone(phone)
            or not _same_phone(lead, phone)
            or lead.get("phone_is_synthetic")
            or lead.get("contact_identity_incomplete")):
        return "REAL_SEND_PHONE_CHANGED"
    variants = phone_identity_variants(phone)
    matches = list(db[LEADS].find({
        "$or": [{"phone": {"$in": variants}},
                {"contact_phone_normalized": {"$in": variants}}],
    }, {"_id": 1}).limit(2))
    if len(matches) != 1 or matches[0]["_id"] != lead["_id"]:
        return "REAL_SEND_AMBIGUOUS_PHONE"

    if (not isinstance(campaign.get("price_drop_at"), datetime)
            or not isinstance(campaign.get("inquiry_evidence_at"), datetime)):
        return "REAL_SEND_INQUIRY_NOT_VERIFIED"
    original = _inquiry_before(
        lead, code, utc(campaign["price_drop_at"]),
        _verified_listing_ids(db, code), return_evidence=True,
    )
    if (not original
            or utc(original["inquiry_at"]) != utc(campaign["inquiry_evidence_at"])):
        return "REAL_SEND_INQUIRY_NOT_VERIFIED"
    return None


def _real_campaign_send_blocker(db, lead: dict, campaign: dict) -> str | None:
    """Fail-closed gate for separately approved, event-specific campaigns."""
    if os.getenv(REAL_CAMPAIGNS_ENABLED_FLAG, "").strip().lower() not in {"1", "true", "yes"}:
        return "REAL_SEND_LOCKED_PENDING_ACCEPTANCE"
    if (campaign.get("contact_basis_reviewed") is not True
            or campaign.get("contact_basis_type") != DIRECT_RESPONSE_BASIS_TYPE
            or not str(campaign.get("contact_basis") or "").strip()
            or not str(campaign.get("approved_by") or "").strip()
            or not isinstance(campaign.get("approved_at"), datetime)):
        return "REAL_SEND_CONTACT_BASIS_OR_APPROVAL_MISSING"
    if (campaign.get("whatsapp_contact_reviewed") is not True
            or not str(campaign.get("whatsapp_contact_evidence") or "").strip()):
        return "WHATSAPP_CONTACT_AUTHORIZATION_MISSING"
    if campaign.get("simulated_prior_inquiry") is not False:
        return "REAL_SEND_SIMULATED_INQUIRY"
    if str(campaign.get("event_id") or campaign.get("campaign_id") or "") != str(
            campaign.get("campaign_id") or ""):
        return "campaign_event_identity_mismatch"
    history_blocker = _real_campaign_history_blocker(lead, campaign)
    if history_blocker:
        return history_blocker
    phone = normalize_phone_strict(campaign.get("phone_normalized") or "")
    if (not phone or not has_real_phone(phone) or not _same_phone(lead, phone)
            or lead.get("phone_is_synthetic") or lead.get("contact_identity_incomplete")):
        return "REAL_SEND_PHONE_CHANGED"
    variants = phone_identity_variants(phone)
    matches = list(db[LEADS].find({
        "$or": [{"phone": {"$in": variants}},
                {"contact_phone_normalized": {"$in": variants}}],
    }, {"_id": 1}).limit(2))
    if len(matches) != 1 or matches[0]["_id"] != lead["_id"]:
        return "REAL_SEND_AMBIGUOUS_PHONE"
    if (not isinstance(campaign.get("price_drop_at"), datetime)
            or not isinstance(campaign.get("inquiry_evidence_at"), datetime)):
        return "REAL_SEND_INQUIRY_NOT_VERIFIED"
    code = str(campaign.get("property_code") or "")
    evidence = _inquiry_before(
        lead, code, utc(campaign["price_drop_at"]),
        _verified_listing_ids(db, code), return_evidence=True,
    )
    if (not evidence
            or utc(evidence["inquiry_at"]) != utc(campaign["inquiry_evidence_at"])):
        return "REAL_SEND_INQUIRY_NOT_VERIFIED"
    return None


def _property_price(db, code: str, old: int, new: int,
                    *, currency: str = "UF") -> datetime:
    if currency not in {"UF", "CLP"}:
        raise PilotBlocked("unsupported_campaign_currency")
    prop = db["universo_cartera_prop360"].find_one(
        {"codigo": code},
        {"codigo": 1, "disponible_prop360": 1, "estado.oficina": 1,
         "tipo_operacion.venta": 1, "tipo_operacion.precio_venta.precio_uf": 1,
         "tipo_operacion.precio_venta.precio_publicado": 1,
         "tipo_operacion.precio_venta.moneda_publicada": 1},
    ) or {}
    sale = prop.get("tipo_operacion") or {}
    pricing = sale.get("precio_venta") or {}
    current = (pricing.get("precio_uf") if currency == "UF"
               else pricing.get("precio_publicado"))
    current_currency = str(pricing.get("moneda_publicada") or "")
    if not (prop.get("disponible_prop360") is True
            and (prop.get("estado") or {}).get("oficina") == "PROCASA SUCRE"
            and sale.get("venta") is True
            and (currency == "UF" or current_currency == "CLP")
            and isinstance(old, int) and isinstance(new, int)
            and current == new and old > new > 0):
        raise PilotBlocked("property_price_or_availability_changed")
    change = db["universo_cartera_prop360_historial"].find_one(
        {"codigo": code, "campo": "precio_publicado",
         "moneda_anterior": currency, "moneda_nueva": currency,
         "monto_anterior": old, "monto_nuevo": new},
        {"fecha": 1},
        sort=[("fecha", -1)],
    )
    if not change:
        raise PilotBlocked("audited_price_change_missing")
    try:
        when = datetime.fromisoformat(str(change["fecha"]).replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError) as exc:
        raise PilotBlocked("audited_price_date_invalid") from exc
    return utc(when)


def _verified_listing_ids(db, code: str) -> set[str]:
    """Read current and historic publication IDs linked to this exact Prop360 property."""
    prop = db["universo_cartera_prop360"].find_one(
        {"codigo": code}, {"publicaciones": 1},
    ) or {}
    pub = prop.get("publicaciones") or {}
    values: set[str] = set()
    for alias in pub.get("aliases") or []:
        if isinstance(alias, dict) and alias.get("resolvable") is True:
            ext = str(alias.get("external_id") or "").strip()
            if len(ext) >= 7:
                values.add(ext.casefold())
    for key in ("portal_inmobiliario", "yapo", "toctoc", "procasa"):
        portal = pub.get(key) or {}
        for publication in (portal.get("publicaciones") or {}).values():
            if not isinstance(publication, dict):
                continue
            for attr in ("external_id", "code", "code_unique"):
                raw = publication.get(attr)
                entries = raw if isinstance(raw, list) else [raw]
                for candidate in entries:
                    external = str(candidate or "").strip()
                    if len(external) >= 7:
                        values.add(external.casefold())
    return values


def _inquiry_before(lead: dict, code: str, drop_at: datetime,
                    listing_ids: set[str] | None = None, *,
                    return_evidence: bool = False) -> bool | dict | None:
    """Require dated, *property-specific* customer evidence before the drop.

    An unrelated old customer message plus the current prospecto.codigo is
    not evidence of a past inquiry about that listing.
    """
    for message in lead.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") not in ("user", "customer"):
            continue
        text = str(message.get("content") or "")
        if not text.strip():
            continue
        raw_stamp = str(message.get("timestamp") or "")
        try:
            parsed = datetime.fromisoformat(raw_stamp.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        # Chile normally uses NEGATIVE UTC offsets. Check parsed tzinfo,
        # never a '+' substring, or valid Santiago inquiry times disappear.
        if parsed.tzinfo is None:
            continue
        stamp = utc(parsed)
        if stamp >= drop_at:
            continue
        metadata = message.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        keys = ("property_code", "codigo_propiedad", "codigo", "listing_code")
        declared = {str(metadata.get(k) or "").strip() for k in keys}
        has_explicit_code = code in declared
        reference = f"metadata_property_code:{code}" if has_explicit_code else ""
        # Also accept an explicit property code/link in the customer text.
        referenced = re.search(
            r"\b(?:c[oó]d(?:igo)?|propiedad)\s*[:#]?\s*(\d{4,6})\b",
            text, re.I,
        )
        if referenced:
            if referenced.group(1) == code:
                has_explicit_code = True
                reference = f"property_code:{code}"
        if re.search(r"https?://(?:www\.)?procasa\.cl/(?:propiedad/)?"
                     + re.escape(code) + r"(?!\d)", text, re.I):
            has_explicit_code = True
            reference = f"procasa_url:{code}"
        if not has_explicit_code:
            lowered = text.casefold()
            normalized_text = re.sub(r"[^a-z0-9]", "", lowered)
            for listing_id in (listing_ids or set()):
                if len(listing_id) < 7:
                    continue
                normalized_id = re.sub(r"[^a-z0-9]", "", listing_id.casefold())
                if (re.search(r"(?<![a-z0-9])" + re.escape(listing_id.casefold())
                              + r"(?![a-z0-9])", lowered)
                        or re.search(r"(?<![a-z0-9])" + re.escape(normalized_id)
                                     + r"(?![a-z0-9])", normalized_text)):
                    has_explicit_code = True
                    reference = f"listing_id:{listing_id}"
                    break
        if has_explicit_code:
            if return_evidence:
                return {
                    "inquiry_at": stamp,
                    "message_id": str(message.get("message_id") or
                                      message.get("provider_message_id") or "")[:128],
                    "reference": reference or f"verified_listing:{code}",
                }
            return True
    return None if return_evidence else False

def _old_completed_test(campaign: dict, code: str, cutoff: datetime | None) -> bool:
    """Only earlier, finished test sends may be superseded in one-phone QA."""
    return bool(
        cutoff is not None
        and campaign.get("property_code") == code
        and campaign.get("test_mode") is True
        and campaign.get("status") in {"SENT", "RESPONDED"}
        and isinstance(campaign.get("sent_at"), datetime)
        and utc(campaign["sent_at"]) < cutoff
    )


def _same_price_drop_event(campaign: dict, *, event_id: str,
                           history_event_ids: list[str], currency: str,
                           old_price: int, new_price: int) -> bool:
    """Detect duplicate price events while allowing later drops on one listing."""
    existing_event = str(campaign.get("event_id") or "")
    existing_history = {
        str(value) for value in (campaign.get("history_event_ids") or [])
    }
    current_history = {str(value) for value in history_event_ids or []}
    if existing_event and existing_event == str(event_id):
        return True
    if existing_history.intersection(current_history | {str(event_id)}):
        return True
    # Pre-event-ledger campaigns cannot be matched by event ID. Treat the
    # same currency and price pair as a duplicate until a distinct event ID
    # is available, while permitting a later recurrence once both rows carry
    # their event IDs.
    if existing_event or existing_history:
        return False
    campaign_currency = str(campaign.get("currency") or "").upper()
    if not campaign_currency:
        campaign_currency = "CLP" if campaign.get("old_price_clp") is not None else "UF"
    old_value = (campaign.get("old_price_clp") if currency == "CLP"
                 else campaign.get("old_price_uf"))
    new_value = (campaign.get("new_price_clp") if currency == "CLP"
                 else campaign.get("new_price_uf"))
    return (
        campaign_currency == currency
        and old_value == old_price
        and new_value == new_price
    )


def reset_test_qa_session(db, *, campaign_id: str, operator: str,
                          phone: str, now=None) -> dict:
    """One-time clean context for the allowlisted 9804, preserving all evidence.

    Archives prior conversational fields under the SAME lead. Never deletes
    customer messages, campaign records, or processed inbound IDs.
    """
    tester = normalize_phone_strict(phone)
    if (tester != APPROVED_ADMIN_PHONE
            or tester != normalize_phone_strict(os.getenv(TEST_PHONE_FLAG, ""))
            or not VALID_ID.fullmatch(campaign_id)
            or not campaign_id.startswith("test_leads_6436_")
            or not str(operator or "").strip()):
        return {"status": "BLOCKED", "reason": "qa_reset_not_authorized"}
    rows = list(db[LEADS].find({"phone": tester}).limit(2))
    if len(rows) != 1:
        return {"status": "BLOCKED", "reason": "qa_test_lead_ambiguous"}
    lead = rows[0]
    if lead_must_not_be_contacted(lead):
        return {"status": "BLOCKED", "reason": "qa_test_lead_opted_out"}
    prior = (lead.get("price_drop_v2") or {}).get("qa_session") or {}
    if prior.get("campaign_id") == campaign_id:
        return {"status": "ALREADY_RESET", "campaign_id": campaign_id}
    campaigns = (lead.get("price_drop_v2") or {}).get("campaigns") or []
    if any(c.get("status") not in {"SENT", "RESPONDED"}
           or c.get("test_mode") is not True for c in campaigns):
        return {"status": "BLOCKED", "reason": "qa_prior_campaign_not_completed"}
    stamp = utc(now or datetime.now(timezone.utc))
    from uuid import uuid4
    reset_record = {
        "campaign_id": campaign_id,
        "started_at": stamp,
        "operator": str(operator)[:80],
        "prior_conversation_id": lead.get("conversation_id"),
        "prior_prospecto": lead.get("prospecto") or {},
        "prior_pending_response": lead.get("pending_response") or {},
        "prior_campaign_ids": [c.get("campaign_id") for c in campaigns],
        "preserved_message_count": len(lead.get("messages") or []),
    }
    # A prior reset has its own snapshot. Preserve it too, rather than
    # replacing an already archived context.
    filter_ = {
        "_id": lead["_id"],
        "phone": tester,
        "price_drop_v2.qa_session.campaign_id": {"$ne": campaign_id},
        "price_drop_v2.campaigns": lead.get("price_drop_v2", {}).get("campaigns") or [],
    }
    update = {
        "$set": {
            "price_drop_v2.qa_session": reset_record,
            "prospecto": {},
            "conversation_id": str(uuid4()),
        },
        "$unset": {
            "pending_response": "",
            "last_intent": "",
            "last_intent_actor": "",
            "last_intent_at": "",
        },
    }
    if prior:
        update["$push"] = {"price_drop_v2.qa_session_archive": prior}
    result = db[LEADS].update_one(filter_, update)
    if result.modified_count != 1:
        return {"status": "BLOCKED", "reason": "qa_concurrent_change"}
    return {"status": "RESET", "campaign_id": campaign_id,
            "recipient_last4": tester[-4:],
            "historical_messages_preserved": reset_record["preserved_message_count"]}


def _clp_outreach_message(code: str, old: int, new: int) -> str:
    format_clp = lambda v: "$" + f"{v:,}".replace(",", ".")
    return (
        "¡Hola! ¿Cómo estás? 😊\n\n"
        "Te escribimos de PROCASA porque hace un tiempo consultaste por "
        f"la propiedad {code}, cuando costaba {format_clp(old)}.\n\n"
        f"Te quería contar que bajó {format_clp(old-new)} y ahora está a "
        f"{format_clp(new)}.\n\n"
        f"Te dejo la publicación actualizada: https://www.procasa.cl/{code}\n\n"
        "Con este nuevo precio, ¿te sigue interesando? "
        "Si prefieres no recibir más avisos, solo dínoslo."
    )


def preview_one(db, *, lead_id: str, phone: str, campaign_id: str,
                property_code: str, old_price_uf: int | None,
                new_price_uf: int | None,
                test_mode: bool, contact_basis_reviewed: bool = False,
                contact_basis_type: str = "",
                contact_basis: str = "", currency: str = "UF",
                old_price_clp: int | None = None,
                new_price_clp: int | None = None,
                event_id: str | None = None,
                history_event_ids: list[str] | None = None,
                price_reference: int | float | None = None,
                price_history_steps: list[dict] | None = None,
                outbound_message_override: str | None = None,
                campaign_filters_enabled: bool = False,
                temperature_filter: str = "ALL",
                temperature_review: dict[str, Any] | None = None,
                lead_max_age_days: int | None = None,
                lead_age_days_snapshot: int | None = None,
                message_variant_id: str | None = None, now=None) -> dict:
    """Read-only preflight. Tests use an explicit existing canonical lead."""
    if not VALID_ID.fullmatch(campaign_id) or not VALID_CODE.fullmatch(property_code):
        raise PilotBlocked("invalid_id_or_property")
    if currency not in {"UF", "CLP"}:
        raise PilotBlocked("unsupported_campaign_currency")
    if currency == "UF":
        if type(old_price_uf) is not int or type(new_price_uf) is not int:
            raise PilotBlocked("integer_uf_required")
        old_value, new_value = old_price_uf, new_price_uf
    else:
        if type(old_price_clp) is not int or type(new_price_clp) is not int:
            raise PilotBlocked("integer_clp_required")
        if old_price_uf is not None or new_price_uf is not None:
            raise PilotBlocked("mixed_currency_values")
        old_value, new_value = old_price_clp, new_price_clp
    canonical = normalize_phone_strict(phone)
    if not canonical or not has_real_phone(canonical):
        raise PilotBlocked("invalid_phone")
    lid = _lead_id(lead_id)
    lead = db[LEADS].find_one({"_id": lid})
    if not lead or not _same_phone(lead, canonical):
        raise PilotBlocked("lead_phone_mismatch")
    if test_mode and _test_qa_webhook_canonical_lead(
            db, lead_id=lid, phone=canonical) is None:
        raise PilotBlocked("ambiguous_test_lead_webhook_route")
    clean_temperature_filter = normalize_temperature_filter(temperature_filter)
    if campaign_filters_enabled:
        selected_temperature = effective_temperature(lead, temperature_review)
        if selected_temperature == "REVISIÓN_PENDIENTE":
            raise PilotBlocked("temperature_review_required")
        if temperature_review_is_required(lead, temperature_review):
            raise PilotBlocked("temperature_review_required")
        if not temperature_filter_matches(selected_temperature, clean_temperature_filter):
            raise PilotBlocked("temperature_filter_mismatch")
        age = lead_age_details(
            lead, now=utc(now or datetime.now(timezone.utc)),
            max_age_days=lead_max_age_days,
        )
        if age["lead_age_status"] != "valid":
            raise PilotBlocked(str(age["lead_age_status"]))
        if age["excluded_by_age"]:
            raise PilotBlocked("lead_age_exceeded")
        if lead_age_days_snapshot is not None and age["lead_age_days"] != lead_age_days_snapshot:
            # A stored age is a review aid; changing the lead's created_at is
            # unusual and requires a fresh review before preparation.
            raise PilotBlocked("lead_created_at_changed_after_candidate_review")
        if temperature_review and (
                str(lead.get("lead_temperature_effective") or
                    lead.get("lead_temperature") or "").strip().upper()
                in {"HOT", "NORMAL", "COLD"}):
            raise PilotBlocked("temperature_changed_after_manual_review")
    if lead.get("phone_is_synthetic") or lead.get("contact_identity_incomplete"):
        raise PilotBlocked("incomplete_contact_identity")
    qa_reauthorization = None
    if lead_must_not_be_contacted(lead):
        if test_mode:
            qa_reauthorization = _test_qa_reauthorization(
                lead, campaign_id=campaign_id, phone=canonical,
                property_code=property_code,
                now=utc(now or datetime.now(timezone.utc)),
            )
        if qa_reauthorization is None:
            raise PilotBlocked("lead_opted_out")
    if test_mode:
        if canonical != normalize_phone_strict(os.getenv(TEST_PHONE_FLAG, "")):
            raise PilotBlocked("test_phone_not_allowlisted")
    else:
        if (not contact_basis_reviewed
                or contact_basis_type != DIRECT_RESPONSE_BASIS_TYPE
                or not str(contact_basis or "").strip()):
            raise PilotBlocked("contact_basis_review_required")
        variants = phone_identity_variants(canonical)
        duplicates = list(db[LEADS].find({
            "$or": [
                {"phone": {"$in": variants}},
                {"contact_phone_normalized": {"$in": variants}},
            ],
        }, {"_id": 1}).limit(2))
        if len(duplicates) != 1 or duplicates[0]["_id"] != lid:
            raise PilotBlocked("ambiguous_lead_identity")
        if not is_open_campaign_eligible(lead):
            raise PilotBlocked("lead_closed_pipeline_stage")
    drop_at = _property_price(
        db, property_code, old_value, new_value, currency=currency,
    )
    inquiry_evidence = None
    if not test_mode:
        inquiry_evidence = _inquiry_before(
            lead, property_code, drop_at, _verified_listing_ids(db, property_code),
            return_evidence=True,
        )
        if not inquiry_evidence:
            raise PilotBlocked("pre_drop_inquiry_not_verified")
    qa = (lead.get("price_drop_v2") or {}).get("qa_session") or {}
    cutoff = utc(qa["started_at"]) if (
        test_mode and qa.get("campaign_id") == campaign_id
        and isinstance(qa.get("started_at"), datetime)
    ) else None
    if test_mode and qa and qa.get("campaign_id") != campaign_id:
        raise PilotBlocked("qa_session_campaign_mismatch")
    if any(c.get("campaign_id") == campaign_id for c in
           (lead.get("price_drop_v2") or {}).get("campaigns") or []):
        raise PilotBlocked("campaign_already_recorded")
    same_property = [
        c for c in (lead.get("price_drop_v2") or {}).get("campaigns") or []
        if c.get("property_code") == property_code
    ]
    if any(c.get("status") in {"SENDING", "DELIVERY_UNKNOWN"} for c in same_property):
        raise PilotBlocked("unresolved_previous_delivery")
    if any(c.get("status") in {"DECLINED", "OPTED_OUT"} for c in same_property):
        raise PilotBlocked("property_previously_declined")
    if any(c.get("status") in {"PREPARED", "APPROVED"} for c in same_property):
        raise PilotBlocked("active_campaign_for_property")
    if any(
        c.get("status") in {"SENT", "RESPONDED"}
        and not _old_completed_test(c, property_code, cutoff)
        and _same_price_drop_event(
            c, event_id=str(event_id or campaign_id),
            history_event_ids=list(history_event_ids or [event_id or campaign_id]),
            currency=currency, old_price=old_value, new_price=new_value,
        )
        for c in same_property
    ):
        raise PilotBlocked("active_campaign_for_property")
    message = (
        str(outbound_message_override).strip()
        if outbound_message_override is not None
        else (_message(property_code, old_value, new_value)
              if currency == "UF" else _clp_outreach_message(
                  property_code, old_value, new_value))
    )
    if not message:
        raise PilotBlocked("outbound_message_required")
    if price_reference is not None and (
            isinstance(price_reference, bool) or not isinstance(price_reference, (int, float))
            or price_reference <= 0):
        raise PilotBlocked("price_reference_invalid")
    clean_history = []
    for step in price_history_steps or []:
        if not isinstance(step, dict):
            raise PilotBlocked("price_history_step_invalid")
        try:
            step_currency = str(step.get("currency") or "").upper()
            step_old, step_new = int(step["old_price"]), int(step["new_price"])
            step_id = str(step["event_id"])
            step_at = str(step["dropped_at"])
            if (step_currency not in {"UF", "CLP"} or not step_old > step_new > 0
                    or not step_id or not step_at):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise PilotBlocked("price_history_step_invalid") from exc
        clean_history.append({
            "event_id": step_id[:100], "dropped_at": step_at[:40],
            "currency": step_currency, "old_price": step_old, "new_price": step_new,
        })
    return {
        "lead_id": lid, "phone_normalized": canonical,
        "campaign_id": campaign_id, "property_code": property_code,
        "old_price_uf": old_price_uf, "new_price_uf": new_price_uf,
        "old_price_clp": old_price_clp, "new_price_clp": new_price_clp,
        "currency": currency, "price_drop_at": drop_at,
        "test_mode": bool(test_mode),
        "qa_reauthorization_id": (qa_reauthorization or {}).get("authorization_id"),
        "contact_basis_reviewed": bool(contact_basis_reviewed) if not test_mode else False,
        "contact_basis_type": contact_basis_type if not test_mode else "TEST_ONLY",
        "contact_basis": str(contact_basis or "")[:240] if not test_mode else "test_only_no_contact_basis",
        "simulated_prior_inquiry": bool(test_mode),
        "qa_session_started_at": cutoff,
        "inquiry_evidence_at": (inquiry_evidence or {}).get("inquiry_at"),
        "inquiry_message_id": (inquiry_evidence or {}).get("message_id"),
        "inquiry_reference": (inquiry_evidence or {}).get("reference"),
        "event_id": str(event_id or campaign_id),
        "history_event_ids": sorted(set(
            str(item) for item in (history_event_ids or [event_id or campaign_id])
        )),
        "price_reference": price_reference,
        "price_history_steps": clean_history,
        "campaign_filters_enabled": bool(campaign_filters_enabled),
        "temperature_filter": clean_temperature_filter,
        "temperature_at_prepare": effective_temperature(lead, temperature_review),
        "temperature_review": (dict(temperature_review) if temperature_review else None),
        "lead_max_age_days": lead_max_age_days,
        "lead_age_days_snapshot": lead_age_days_snapshot,
        "message_variant_id": str(message_variant_id or "")[:40] or None,
        "message": message,
    }


def prepare_one(db, *, now=None, **data) -> dict:
    """Append exactly one PREPARED campaign to the selected lead."""
    stamp = utc(now or datetime.now(timezone.utc))
    p = preview_one(db, now=stamp, **data)
    row = {k: p[k] for k in (
        "campaign_id", "property_code", "old_price_uf", "new_price_uf",
        "old_price_clp", "new_price_clp", "currency",
        "price_drop_at", "test_mode", "contact_basis_reviewed",
        "contact_basis_type", "contact_basis",
        "simulated_prior_inquiry", "phone_normalized", "qa_session_started_at",
        "inquiry_evidence_at", "inquiry_message_id", "inquiry_reference")}
    row.update({
        "event_id": p["event_id"],
        "history_event_ids": p["history_event_ids"],
        "price_reference": p["price_reference"],
        "price_history_steps": p["price_history_steps"],
    })
    if p.get("qa_reauthorization_id"):
        row["qa_reauthorization_id"] = p["qa_reauthorization_id"]
    if p["campaign_filters_enabled"]:
        row.update({
            "campaign_filters_enabled": True,
            "temperature_filter": p["temperature_filter"],
            "temperature_at_prepare": p["temperature_at_prepare"],
            "temperature_review": p["temperature_review"],
            "lead_max_age_days": p["lead_max_age_days"],
            "lead_age_days_snapshot": p["lead_age_days_snapshot"],
            "message_variant_id": p["message_variant_id"],
        })
    row.update({
        "status": "PREPARED", "prepared_at": stamp,
        "outbound_message": p["message"],
        "reply_count": 0, "notifications": {
            "administrator": {"status": "NOT_TRIGGERED"},
            "property_executive": {"status": "FROZEN_ADMIN_ONLY"},
        },
    })
    # Closed leads cannot be prepared, but a change from HOT to COLD is allowed.
    claim_filter = {
        "_id": p["lead_id"],
        f"{ROOT}.campaign_id": {"$ne": p["campaign_id"]},
        ROOT: {"$not": {"$elemMatch": {
            "property_code": p["property_code"],
            "status": {"$in": [
                "PREPARED", "APPROVED", "SENDING", "DELIVERY_UNKNOWN",
                "DECLINED", "OPTED_OUT",
            ]},
        }}},
    }
    if p["test_mode"] and p["qa_session_started_at"] is not None:
        claim_filter[ROOT] = {"$not": {"$elemMatch": {
            "property_code": p["property_code"],
            "$or": [
                {"test_mode": {"$ne": True}},
                {"status": {"$nin": ["SENT", "RESPONDED"]}},
                {"sent_at": {"$gte": p["qa_session_started_at"]}},
            ],
        }}}
        claim_filter["price_drop_v2.qa_session.campaign_id"] = p["campaign_id"]
    if not p["test_mode"]:
        claim_filter.update({
            "pipeline_stage": {"$nin": ["CLOSED_LOST", "CLOSED_WON"]},
            "stage": {"$nin": ["CLOSED_LOST", "CLOSED_WON"]},
        })
    result = db[LEADS].update_one(
        claim_filter,
        {"$push": {ROOT: row}},
    )
    if result.modified_count != 1:
        raise PilotBlocked("concurrent_campaign_or_lead_change")
    return {"status": "PREPARED", "campaign_id": p["campaign_id"],
            "lead_id": str(p["lead_id"]), "test_mode": bool(p["test_mode"])}


def approve_one(db, *, lead_id: str, campaign_id: str, operator: str, now=None) -> dict:
    if not str(operator or "").strip():
        raise PilotBlocked("operator_approval_required")
    stamp = utc(now or datetime.now(timezone.utc))
    # Campaigns must still target open opportunities at approval.
    lead = db[LEADS].find_one({"_id": _lead_id(lead_id)}, {"price_drop_v2.campaigns": 1,
        "lead_temperature_effective": 1, "pipeline_stage": 1, "stage": 1}) or {}
    campaign = next((row for row in ((lead.get("price_drop_v2") or {}).get("campaigns") or [])
                     if row.get("campaign_id") == campaign_id), None)
    if campaign and not campaign.get("test_mode") and not is_open_campaign_eligible(lead):
        raise PilotBlocked("lead_closed_pipeline_stage")
    approval_filter = {
        "_id": _lead_id(lead_id),
        ROOT: {"$elemMatch": {"campaign_id": campaign_id, "status": "PREPARED"}},
    }
    if campaign and not campaign.get("test_mode"):
        approval_filter.update({
            "pipeline_stage": {"$nin": ["CLOSED_LOST", "CLOSED_WON"]},
            "stage": {"$nin": ["CLOSED_LOST", "CLOSED_WON"]},
        })
    result = db[LEADS].update_one(
        approval_filter,
        {"$set": {f"{ROOT}.$.status": "APPROVED",
                  f"{ROOT}.$.approved_by": str(operator)[:80],
                  f"{ROOT}.$.approved_at": stamp}},
    )
    if result.modified_count != 1:
        raise PilotBlocked("not_prepared_or_already_approved")
    return {"status": "APPROVED", "campaign_id": campaign_id}


def review_whatsapp_contact(db, *, lead_id: str, campaign_id: str,
                            operator: str, evidence: str, now=None) -> dict:
    """Record a human-verified, campaign-specific WhatsApp opt-in reference.

    Evidence should identify the source and date in the CRM; do not paste a
    full consent transcript or unrelated personal data into this field.
    Only prepared or approved, unsent campaigns may be reviewed.
    """
    operator = str(operator or "").strip()
    evidence = str(evidence or "").strip()
    if not operator or len(evidence) < 8:
        raise PilotBlocked("whatsapp_contact_authorization_evidence_required")
    stamp = utc(now or datetime.now(timezone.utc))
    result = db[LEADS].update_one(
        {"_id": _lead_id(lead_id), ROOT: {"$elemMatch": {
            "campaign_id": campaign_id, "status": {"$in": ["PREPARED", "APPROVED"]},
            "provider_message_id": {"$in": [None, ""]},
        }}},
        {"$set": {
            f"{ROOT}.$.whatsapp_contact_reviewed": True,
            f"{ROOT}.$.whatsapp_contact_evidence": evidence[:240],
            f"{ROOT}.$.whatsapp_contact_reviewed_by": operator[:80],
            f"{ROOT}.$.whatsapp_contact_reviewed_at": stamp,
        }},
    )
    if result.modified_count != 1:
        raise PilotBlocked("campaign_not_reviewable_or_already_attempted")
    return {"status": "WHATSAPP_CONTACT_REVIEWED", "campaign_id": campaign_id}


def send_one(db, *, lead_id: str, campaign_id: str,
             sender: Callable[[str, str], dict] | None = None, now=None,
             recovery_journal_path=None,
             temperature_filter_override: str | None = None,
             lead_max_age_days_override: int | None = None,
             enforce_campaign_filters: bool = False) -> dict:
    """One irreversible attempt; no sender invoked without separate gates."""
    if not (os.getenv(SEND_FLAG, "").lower() in {"true", "1", "yes"}
            and os.getenv(APPROVED_ID_FLAG) == campaign_id):
        return {"status": "DISABLED"}
    if os.getenv(CONTEXT_FLAG, "").lower() not in {"true", "1", "yes"}:
        return {"status": "CONTEXT_DISABLED"}
    lead = db[LEADS].find_one({"_id": _lead_id(lead_id)}) or {}
    all_campaigns = (lead.get("price_drop_v2") or {}).get("campaigns") or []
    matching_campaigns = [x for x in all_campaigns
                          if x.get("campaign_id") == campaign_id]
    if len(matching_campaigns) > 1:
        return {"status": "DUPLICATE_CAMPAIGN_ID"}
    c = matching_campaigns[0] if matching_campaigns else None
    if not c or c.get("status") != "APPROVED":
        return {"status": "NOT_APPROVED"}
    channel = os.getenv("PRICE_DROP_WHATSAPP_CHANNEL_MODE", "wasenderapi").strip().lower()
    if channel == "official":
        return {"status": "OFFICIAL_CHANNEL_SENDER_NOT_IMPLEMENTED"}
    if channel != "wasenderapi":
        return {"status": "WHATSAPP_CHANNEL_MODE_INVALID"}
    stamp = utc(now or datetime.now(timezone.utc))
    if not c.get("test_mode") and (
            os.getenv("RENDER", "").strip().lower() == "true"
            or os.getenv("RENDER_SERVICE_ID")):
        return {"status": "MANUAL_CAMPAIGNS_DISABLED_ON_RENDER"}
    qa_reauthorization = None
    if c.get("test_mode"):
        test_phone = normalize_phone_strict(c.get("phone_normalized", ""))
        if (_test_qa_webhook_canonical_lead(
                db, lead_id=lead["_id"], phone=test_phone) is None
                or (test_phone == APPROVED_ADMIN_PHONE
                    and str(lead.get("_id"))
                    != str(os.getenv(TEST_LEAD_ID_FLAG, "")).strip())):
            return {"status": "TEST_LEAD_IDENTITY_NOT_PINNED"}
    if c.get("test_mode") and c.get("qa_reauthorization_id"):
        qa_reauthorization = _test_qa_reauthorization(
            lead, campaign_id=campaign_id,
            phone=c.get("phone_normalized", ""),
            property_code=str(c.get("property_code") or ""), now=stamp,
            expected_authorization_id=str(c.get("qa_reauthorization_id")),
        )
        if qa_reauthorization is None:
            return {"status": "BLOCKED_QA_REAUTHORIZATION_INVALID"}
    if lead_must_not_be_contacted(lead):
        if c.get("test_mode") and qa_reauthorization is None:
            qa_reauthorization = _test_qa_reauthorization(
                lead, campaign_id=campaign_id,
                phone=c.get("phone_normalized", ""),
                property_code=str(c.get("property_code") or ""), now=stamp,
                expected_authorization_id=str(c.get("qa_reauthorization_id") or ""),
            )
        if qa_reauthorization is None:
            return {"status": "BLOCKED_OPTED_OUT"}
    # Historical recovery is NOT HOT-only; closed opportunities remain blocked.
    if not c.get("test_mode") and not is_open_campaign_eligible(lead):
        return {"status": "BLOCKED_CLOSED_PIPELINE"}
    # The approved three-recipient campaign retains its narrow allowlist.
    # A future event with the same property and price is still a new campaign.
    if not c.get("test_mode"):
        blocker = (_real_pilot_send_blocker(db, lead, c)
                   if campaign_id == REAL_PILOT_CAMPAIGN_ID
                   else _real_campaign_send_blocker(db, lead, c))
        if blocker:
            return {"status": blocker}
        if os.getenv(WHATSAPP_CONTACT_REVIEWED_FLAG, "").strip().lower() not in {
            "1", "true", "yes"
        }:
            return {"status": "WHATSAPP_CONTACT_REVIEW_REQUIRED"}
        window_blocker = _text_sender_window_blocker(lead, stamp)
        if window_blocker:
            return {"status": window_blocker}
    filters_enabled = (not c.get("test_mode") and (
        c.get("campaign_filters_enabled") is True or enforce_campaign_filters
    ))
    if filters_enabled:
        review = c.get("temperature_review") or {}
        temperature = effective_temperature(lead, review)
        if temperature == "REVISIÓN_PENDIENTE":
            return {"status": "BLOCKED_TEMPERATURE_REVIEW_REQUIRED"}
        try:
            selected_filter = normalize_temperature_filter(
                temperature_filter_override if temperature_filter_override is not None
                else c.get("temperature_filter", "ALL")
            )
            stored_filter = normalize_temperature_filter(c.get("temperature_filter", "ALL"))
        except ValueError:
            return {"status": "BLOCKED_TEMPERATURE_FILTER_INVALID"}
        known_temperatures = {"HOT", "NORMAL", "COLD"}
        selected_set = (known_temperatures if selected_filter == "ALL"
                        else set(selected_filter.split(",")))
        stored_set = (known_temperatures if stored_filter == "ALL"
                      else set(stored_filter.split(",")))
        if not selected_set <= stored_set:
            return {"status": "BLOCKED_TEMPERATURE_FILTER_WIDENED"}
        if not temperature_filter_matches(temperature, selected_filter):
            return {"status": "BLOCKED_TEMPERATURE_FILTER_MISMATCH"}
        if temperature_review_is_required(lead, review):
            return {"status": "BLOCKED_TEMPERATURE_REVIEW_REQUIRED"}
        current_temperature = str(lead.get("lead_temperature_effective") or
                                  lead.get("lead_temperature") or "").strip().upper()
        if review and current_temperature in {"HOT", "NORMAL", "COLD"}:
            return {"status": "BLOCKED_TEMPERATURE_CHANGED_AFTER_REVIEW"}
        selected_age_raw = (
            lead_max_age_days_override
            if enforce_campaign_filters and lead_max_age_days_override is not None
            else c.get("lead_max_age_days", 120)
        )
        selected_age = normalize_lead_max_age_days(selected_age_raw)
        stored_age = normalize_lead_max_age_days(c.get("lead_max_age_days"))
        if (enforce_campaign_filters and stored_age is not None
                and (selected_age is None or selected_age > stored_age)):
            return {"status": "BLOCKED_LEAD_AGE_FILTER_WIDENED"}
        age = lead_age_details(lead, now=stamp, max_age_days=selected_age)
        if age["lead_age_status"] not in {"valid", "lead_age_exceeded"}:
            return {"status": "BLOCKED_LEAD_AGE_REVIEW_REQUIRED",
                    "reason": age["lead_age_status"]}
        if age["excluded_by_age"]:
            return {"status": "BLOCKED_LEAD_AGE_EXCEEDED"}
    if c.get("test_mode") and normalize_phone_strict(os.getenv(TEST_PHONE_FLAG, "")) != c.get("phone_normalized"):
        return {"status": "TEST_PHONE_NOT_ALLOWLISTED"}
    currency = c.get("currency") or "UF"
    old = c.get("old_price_clp") if currency == "CLP" else c.get("old_price_uf")
    new = c.get("new_price_clp") if currency == "CLP" else c.get("new_price_uf")
    if currency == "CLP":
        _property_price(db, c["property_code"], old, new, currency="CLP")
    else:
        _property_price(db, c["property_code"], old, new)
    campaign_index = next((i for i, row in enumerate(all_campaigns)
                           if row.get("campaign_id") == campaign_id), None)
    if campaign_index is None:
        return {"status": "NOT_APPROVED"}
    campaign_path = f"{ROOT}.{campaign_index}"
    send_claim = {"_id": lead["_id"],
                  campaign_path + ".campaign_id": campaign_id,
                  campaign_path + ".status": "APPROVED"}
    claim_update = {"$set": {
        campaign_path + ".status": "SENDING",
        campaign_path + ".attempted_at": stamp,
    }}
    if qa_reauthorization is not None:
        auth_rows = ((lead.get("price_drop_v2") or {}).get(
            "qa_reauthorizations") or [])
        auth_index = next((i for i, row in enumerate(auth_rows)
                           if isinstance(row, dict) and row.get("authorization_id")
                           == qa_reauthorization.get("authorization_id")), None)
        if auth_index is None:
            return {"status": "BLOCKED_QA_REAUTHORIZATION_MISSING"}
        auth_path = f"{TEST_QA_REAUTHORIZATIONS}.{auth_index}"
        send_claim[auth_path + ".authorization_id"] = qa_reauthorization["authorization_id"]
        send_claim[auth_path + ".status"] = "ACTIVE"
        claim_update["$set"].update({
            auth_path + ".status": "CONSUMED",
            auth_path + ".consumed_at": stamp,
        })
    if not c.get("test_mode"):
        send_claim.update({
            "pipeline_stage": {"$nin": ["CLOSED_LOST", "CLOSED_WON"]},
            "stage": {"$nin": ["CLOSED_LOST", "CLOSED_WON"]},
        })
    claimed = db[LEADS].update_one(
        send_claim,
        claim_update,
    )
    if claimed.modified_count != 1:
        return {"status": "ALREADY_ATTEMPTED"}
    if sender is None:
        from .whatsapp_client import send_whatsapp_message_detailed_sync
        sender = send_whatsapp_message_detailed_sync
    try:
        receipt = sender(c["phone_normalized"], c["outbound_message"])
    except Exception:
        receipt = {"success": False, "provider_call_uncertain": True}
    accepted = bool(receipt.get("success") and receipt.get("provider_message_id")
                    and not receipt.get("provider_call_uncertain"))
    explicit_failure = bool(
        not accepted and not receipt.get("provider_call_uncertain")
        and (receipt.get("provider_called") is False
             or (receipt.get("http_status") is not None
                 and str(receipt.get("delivery_status") or "").lower()
                 in {"failed", "provider_auth_error"}))
    )
    status = "SENT" if accepted else "FAILED" if explicit_failure else "DELIVERY_UNKNOWN"
    set_fields = {
        f"{ROOT}.$.status": status,
        f"{ROOT}.$.provider_message_id": receipt.get("provider_message_id"),
        f"{ROOT}.$.provider_status": str(receipt.get("delivery_status") or "unknown")[:40],
    }
    if accepted:
        set_fields[f"{ROOT}.$.sent_at"] = stamp
        set_fields[f"{ROOT}.$.expires_at"] = stamp + timedelta(days=7)
    if accepted:
        journal_recorded = False
        if recovery_journal_path is not None:
            try:
                from .price_drop_reconciliation import record_accepted_receipt
                record_accepted_receipt(
                    recovery_journal_path, lead_id=str(lead["_id"]),
                    campaign_id=campaign_id,
                    provider_message_id=str(receipt["provider_message_id"]),
                    accepted_at=stamp,
                )
                journal_recorded = True
            except Exception:
                # The Mongo write below is still attempted with the receipt in
                # memory. If it also fails, the run stops for manual recovery.
                pass
        message = {
            "role": "assistant", "content": c["outbound_message"],
            "provider_message_id": receipt["provider_message_id"],
            "delivery_status": "accepted", "timestamp": stamp.isoformat(),
            "metadata": {
                "tipo": "price_drop_outreach", "campaign_id": campaign_id,
                "price_drop_event_id": str(c.get("event_id") or campaign_id),
                "property_code": str(c.get("property_code") or ""),
                "currency": currency,
                "provider_message_id": receipt["provider_message_id"],
                "provider_status": str(receipt.get("delivery_status") or "accepted"),
                "old_price_uf": c.get("old_price_uf"),
                "new_price_uf": c.get("new_price_uf"),
                "old_price_clp": c.get("old_price_clp"),
                "new_price_clp": c.get("new_price_clp"),
                "price_drop_at": c.get("price_drop_at"),
                "history_event_ids": c.get("history_event_ids") or [],
                "price_reference": c.get("price_reference"),
                "price_history_steps": c.get("price_history_steps") or [],
                **({"message_variant_id": c.get("message_variant_id")}
                   if c.get("message_variant_id") else {}),
            },
        }
        try:
            persisted = db[LEADS].update_one(
                {"_id": lead["_id"],
                 ROOT: {"$elemMatch": {"campaign_id": campaign_id, "status": "SENDING"}},
                 "messages.provider_message_id": {"$ne": receipt["provider_message_id"]}},
                {"$set": set_fields, "$push": {"messages": message}},
            )
            if persisted.modified_count != 1:
                # A genuine WaSender echo can win the race and append first.
                echo = db[LEADS].find_one(
                    {"_id": lead["_id"], "messages.provider_message_id":
                     receipt["provider_message_id"]}, {"_id": 1})
                if not echo:
                    raise RuntimeError("accepted_message_persistence_conflict")
                persisted = db[LEADS].update_one(
                    {"_id": lead["_id"],
                     ROOT: {"$elemMatch": {"campaign_id": campaign_id, "status": "SENDING"}}},
                    {"$set": set_fields},
                )
                if persisted.modified_count != 1:
                    raise RuntimeError("accepted_campaign_persistence_conflict")
            if journal_recorded:
                from .price_drop_reconciliation import clear_accepted_receipt
                clear_accepted_receipt(
                    recovery_journal_path, lead_id=str(lead["_id"]),
                    campaign_id=campaign_id,
                    provider_message_id=str(receipt["provider_message_id"]),
                )
        except Exception:
            return {"status": "HISTORY_RECONCILIATION_REQUIRED",
                    "campaign_id": campaign_id,
                    "provider_message_id": receipt.get("provider_message_id")}
    else:
        try:
            db[LEADS].update_one(
                {"_id": lead["_id"],
                 ROOT: {"$elemMatch": {"campaign_id": campaign_id, "status": "SENDING"}}},
                {"$set": set_fields},
            )
        except Exception:
            return {"status": "HISTORY_RECONCILIATION_REQUIRED",
                    "campaign_id": campaign_id,
                    "provider_message_id": receipt.get("provider_message_id")}
    return {"status": status, "campaign_id": campaign_id,
            "provider_message_id": receipt.get("provider_message_id"),
            "provider_status": str(receipt.get("delivery_status") or "unknown")[:40],
            "provider_attempted": receipt.get("provider_called") is not False,
            "provider_call_uncertain": bool(receipt.get("provider_call_uncertain"))}


def active_context(db, *, phone: str, message: str = "", now=None) -> dict | None:
    """Resolve a verified SENT campaign from the canonical lead, no campaign table."""
    if os.getenv(CONTEXT_FLAG, "").lower() not in {"1", "true", "yes"}:
        return None
    canonical = normalize_phone_strict(phone)
    if not canonical:
        return None
    now = utc(now or datetime.now(timezone.utc))
    docs = list(db[LEADS].find(
        {"$or": [{"phone": canonical},
                 {"contact_phone_normalized": canonical}],
         ROOT: {"$elemMatch": {"status": {"$in": ["SENT", "RESPONDED"]},
                               "expires_at": {"$gt": now}}}},
        {"_id": 1, "phone": 1, "contact_phone_normalized": 1,
         "price_drop_v2.campaigns": 1, "price_drop_v2.qa_session": 1,
         "price_drop_v2.qa_reauthorizations": 1,
         "price_drop_v2.contact_preferences": 1,
         "price_drop_v2.opted_out": 1, "price_drop_campaign_v1.opted_out": 1,
         "conversation_status": 1},
    ).limit(2))
    if len(docs) != 1:
        return None
    lead = docs[0]
    qa = (lead.get("price_drop_v2") or {}).get("qa_session") or {}
    cutoff = utc(qa["started_at"]) if (
        canonical == APPROVED_ADMIN_PHONE
        and isinstance(qa.get("started_at"), datetime)
    ) else None
    candidates = [
        x for x in (lead.get("price_drop_v2") or {}).get("campaigns") or []
        if x.get("status") in ("SENT", "RESPONDED")
        and str(x.get("provider_message_id") or "").strip()
        and isinstance(x.get("sent_at"), datetime)
        and isinstance(x.get("expires_at"), datetime)
        and utc(x["sent_at"]) <= now < utc(x["expires_at"])
        and (cutoff is None or not x.get("test_mode")
             or utc(x["sent_at"]) >= cutoff)
    ]
    campaign = max(candidates, key=lambda x: utc(x["sent_at"])) if candidates else None
    if not campaign or not _same_phone(lead, canonical):
        return None
    qa_reauthorization = None
    if campaign.get("test_mode"):
        if _test_qa_webhook_canonical_lead(
                db, lead_id=lead["_id"], phone=canonical) is None:
            return None
        expected_authorization_id = str(campaign.get("qa_reauthorization_id") or "")
        if expected_authorization_id:
            qa_reauthorization = _test_qa_reauthorization(
                lead, campaign_id=str(campaign.get("campaign_id") or ""),
                phone=canonical, property_code=str(campaign.get("property_code") or ""),
                now=now, expected_authorization_id=expected_authorization_id,
                allow_consumed=True,
            )
    if lead_must_not_be_contacted(lead):
        if (not campaign.get("test_mode") or qa_reauthorization is None):
            return None
    code = campaign["property_code"]
    # An explicit other-property reference (including a link) must not be
    # attributed to this campaign. Reuse the existing resolver used by V1.
    from .price_drop_pilot import _other_property_referenced
    if _other_property_referenced(message or "", code):
        own_url = f"https://www.procasa.cl/{code}"
        if own_url not in str(message or ""):
            return None
    currency = campaign.get("currency") or "UF"
    old = campaign.get("old_price_clp") if currency == "CLP" else campaign.get("old_price_uf")
    new = campaign.get("new_price_clp") if currency == "CLP" else campaign.get("new_price_uf")
    try:
        if currency == "CLP":
            _property_price(db, code, old, new, currency="CLP")
        else:
            _property_price(db, code, old, new)
    except PilotBlocked:
        return None
    return {
        "version": "price_drop_leads_v2",
        "campaign_id": campaign["campaign_id"],
        "property_code": code,
        "old_price_uf": (float(campaign["old_price_uf"])
                         if currency == "UF" else None),
        "new_price_uf": (float(campaign["new_price_uf"])
                         if currency == "UF" else None),
        "old_price_clp": (int(campaign["old_price_clp"])
                          if currency == "CLP" else None),
        "new_price_clp": (int(campaign["new_price_clp"])
                          if currency == "CLP" else None),
        "currency": currency,
        "operation": "Venta",
        "outbound_message": campaign["outbound_message"],
        "test_mode": bool(campaign.get("test_mode")),
        "customer_preferences": dict(
            (lead.get("price_drop_v2") or {}).get("contact_preferences") or {}
        ),
    }


def record_inbound(db, *, phone: str, provider_message_id: str,
                   text: str, now=None) -> dict:
    """Classify campaign replies directly on the lead without new collections."""
    if not provider_message_id or not text or not text.strip():
        return {"status": "NOT_CAMPAIGN_REPLY"}
    # Report duplicates consistently even after an opt-out closed campaign
    # context. The provider receipt ID is globally unique and this read is
    # safe for replies whose original campaign is no longer active.
    if db[LEADS].find_one(
        {"price_drop_v2.processed_inbound_provider_ids": str(provider_message_id)},
        {"_id": 1},
    ):
        return {"status": "DUPLICATE_IGNORED"}
    ctx = active_context(db, phone=phone, message=text, now=now)
    if not ctx:
        return {"status": "NOT_CAMPAIGN_REPLY"}
    now = utc(now or datetime.now(timezone.utc))
    classification = classify_price_drop_reply(text)
    label = classification.as_dict()
    cid = ctx["campaign_id"]
    if ctx.get("test_mode"):
        canonical = normalize_phone_strict(phone)
        routed = list(db[LEADS].find(
            {"$or": [{"phone": canonical}, {"contact_phone_normalized": canonical}],
             ROOT: {"$elemMatch": {"campaign_id": cid,
                                   "status": {"$in": ["SENT", "RESPONDED"]}}}},
            {"_id": 1},
        ).limit(2))
        lead = None
        if len(routed) == 1:
            routed_id = routed[0]["_id"]
            if (canonical != APPROVED_ADMIN_PHONE
                    or str(routed_id) == str(os.getenv(TEST_LEAD_ID_FLAG, "")).strip()):
                lead = _test_qa_webhook_canonical_lead(
                    db, lead_id=routed_id, phone=canonical)
    else:
        lead = db[LEADS].find_one(
            {"$or": [{"phone": normalize_phone_strict(phone)},
                     {"contact_phone_normalized": normalize_phone_strict(phone)}],
             ROOT: {"$elemMatch": {"campaign_id": cid, "status": {
                 "$in": ["SENT", "RESPONDED"]}}}},
            {"_id": 1, "phone": 1, "contact_phone_normalized": 1,
             "price_drop_v2.campaigns": 1,
             "price_drop_v2.contact_preferences": 1},
        )
    if not lead or not _same_phone(lead, phone):
        return {"status": "NOT_CAMPAIGN_REPLY"}
    # Campaign array entries are append-only; an explicit verified index
    # avoids ambiguous positional updates under concurrent $elemMatch.
    campaign_index = next(
        (i for i, row in enumerate((lead.get("price_drop_v2") or {}).get("campaigns") or [])
         if row.get("campaign_id") == cid), None,
    )
    if campaign_index is None:
        return {"status": "NOT_CAMPAIGN_REPLY"}
    field = f"{ROOT}.{campaign_index}"
    stopped = label["label"] == "BAJA"
    declined = label["label"] == "RECHAZADO"
    positive = label["label"] in {"INTERES_DECLARADO", "PRIORIDAD_COMERCIAL", ALTERNATIVES}
    existing_campaign = (lead.get("price_drop_v2") or {}).get("campaigns", [])[campaign_index]
    resolved_status = "OPTED_OUT" if stopped else "DECLINED" if declined else "RESPONDED"
    update = {
        "$set": {
            f"{field}.status": resolved_status,
            f"{field}.last_classification": label,
            f"{field}.last_customer_reply_at": now,
            f"{field}.last_inbound_provider_id": str(provider_message_id),
            f"{field}.last_customer_text": str(text)[:350],
        },
        "$addToSet": {
            "price_drop_v2.processed_inbound_provider_ids": str(provider_message_id),
        },
        # Root-level immutable audit log avoids positional-array $push:
        # every event retains its campaign ID and unique provider receipt.
        "$push": {"price_drop_v2.reply_events": {
            "campaign_id": cid,
            "provider_message_id": str(provider_message_id),
            "received_at": now,
            "text": str(text)[:350],
            "classification": label,
        }},
        "$inc": {f"{field}.reply_count": 1},
    }
    if existing_campaign.get("reply_count", 0) == 0:
        update["$set"][f"{field}.first_response_at"] = now
    if stopped or declined:
        update["$set"][f"{field}.notifications.administrator.status"] = "CANCELED"
        update["$set"][f"{field}.notifications.property_executive.status"] = "FROZEN_ADMIN_ONLY"
        if declined:
            update["$set"][f"{field}.declined_at"] = now
    if stopped:
        # Closed search / explicit opt-out is global to outbound campaigns and
        # future chatbot turns, without changing CRM temperature or SLA.
        update["$set"].update({
            "price_drop_v2.opted_out": True,
            "price_drop_v2.opted_out_at": now,
            "price_drop_campaign_v1.opted_out": True,
            "conversation_status": "STOPPED_BY_CLIENT",
            "conversation_status_reason": label["reason"],
            "conversation_status_evidence": str(text)[:300],
            "conversation_status_at": now.isoformat(),
            "conversation_status_version": "commercial_close_no_contact_v1",
            f"{field}.opted_out_at": now,
            # A separate administrator-only information queue records the
            # customer's global stop request without reopening contact.
            f"{field}.notifications.administrator_info.status": "PENDING_INFO",
            f"{field}.notifications.administrator_info.reason": label["reason"],
        })
    if classification.do_not_call:
        preference = {
            "price_drop_v2.contact_preferences.do_not_call": True,
            "price_drop_v2.contact_preferences.do_not_call_evidence": str(text)[:300],
            "price_drop_v2.contact_preferences.do_not_call_updated_at": now,
            f"{field}.customer_preferences.do_not_call": True,
            f"{field}.customer_preferences.do_not_call_evidence": str(text)[:300],
            f"{field}.customer_preferences.updated_at": now,
        }
        update["$set"].update(preference)
    elif classification.explicit_contact and (
            (lead.get("price_drop_v2") or {}).get("contact_preferences") or {}
    ).get("do_not_call") is True:
        # A newer direct request for a call is an explicit change to the
        # earlier channel preference. Keep the audit trail while lifting it.
        update["$set"].update({
            "price_drop_v2.contact_preferences.do_not_call": False,
            "price_drop_v2.contact_preferences.do_not_call_revoked_at": now,
            "price_drop_v2.contact_preferences.do_not_call_revoked_by_campaign": cid,
            f"{field}.customer_preferences.do_not_call": False,
            f"{field}.customer_preferences.do_not_call_revoked_at": now,
        })
    if classification.visit_declined:
        update["$set"].update({
            "price_drop_v2.contact_preferences.visit_declined": True,
            "price_drop_v2.contact_preferences.visit_declined_evidence": str(text)[:300],
            "price_drop_v2.contact_preferences.visit_declined_updated_at": now,
            f"{field}.customer_preferences.visit_declined": True,
            f"{field}.customer_preferences.visit_declined_evidence": str(text)[:300],
            f"{field}.customer_preferences.updated_at": now,
        })
    elif classification.explicit_visit and (
            (lead.get("price_drop_v2") or {}).get("contact_preferences") or {}
    ).get("visit_declined") is True:
        update["$set"].update({
            "price_drop_v2.contact_preferences.visit_declined": False,
            "price_drop_v2.contact_preferences.visit_declined_revoked_at": now,
            "price_drop_v2.contact_preferences.visit_declined_revoked_by_campaign": cid,
            f"{field}.customer_preferences.visit_declined": False,
            f"{field}.customer_preferences.visit_declined_revoked_at": now,
        })
    if positive:
        update["$set"][f"{field}.last_positive_reply_text"] = str(text)[:350]
        update["$set"][f"{field}.last_positive_reply_at"] = now
        update["$set"][f"{field}.last_positive_classification"] = label
        stored_campaign = next(
            (row for row in (lead.get("price_drop_v2") or {}).get("campaigns") or []
             if row.get("campaign_id") == cid), {},
        )
        if not stored_campaign.get("has_positive_response"):
            update["$set"][f"{field}.first_positive_reply_text"] = str(text)[:350]
            update["$set"][f"{field}.first_positive_reply_at"] = now
            role_status = (
                "PENDING_SIMULATION" if ctx["test_mode"]
                else "PENDING_HUMAN_REVIEW"
            )
            # Campaign replies route to the administrator only. Preserve the
            # executive notification slot for a separately approved future
            # feature, without scheduling any executive provider send.
            update["$set"][f"{field}.notifications.administrator.status"] = role_status
            if executive_notices_enabled():
                update["$set"][f"{field}.notifications.property_executive.status"] = role_status
            else:
                update["$set"][f"{field}.notifications.property_executive.status"] = "FROZEN_ADMIN_ONLY"
        update["$set"][f"{field}.has_positive_response"] = True
    result = db[LEADS].update_one(
        {"_id": lead["_id"],
         "price_drop_v2.processed_inbound_provider_ids": {
             "$ne": str(provider_message_id),
         },
         f"{field}.campaign_id": cid,
         f"{field}.status": {"$in": ["SENT", "RESPONDED"]}},
        update,
    )
    if result.modified_count != 1:
        return {"status": "DUPLICATE_IGNORED"}
    return {"status": "ATTRIBUTED", "campaign_id": cid,
            "classification": label["label"], "test_mode": ctx["test_mode"]}


def record_delivery_update(db, payload: dict, *, now=None) -> dict:
    """Track actual delivered/read receipts in leads, not a campaign ledger.

    Provider acceptance is not the same as delivery to a customer's device.
    Unknown/failure callbacks do not imply delivery and cannot auto-resend.
    """
    from .whatsapp_client import normalize_provider_status
    nested = payload.get("data") or {}
    item = nested.get("messages") or nested
    if isinstance(item, list):
        item = item[0] if item else {}
    if not isinstance(item, dict):
        item = {}
    key = item.get("key") or {}
    mid = str(key.get("id") or item.get("messageId")
              or item.get("msgId") or payload.get("messageId") or "").strip()
    if not mid:
        return {"status": "UNMATCHED"}
    raw_status = item.get("status") or nested.get("status") or payload.get("status")
    state = normalize_provider_status(raw_status)
    if state not in {"delivered", "read"}:
        return {"status": "IGNORED"}
    candidates = list(db[LEADS].find(
        {f"{ROOT}.provider_message_id": mid},
        {"_id": 1, "price_drop_v2.campaigns": 1},
    ).limit(2))
    if len(candidates) != 1:
        return {"status": "UNMATCHED_OR_AMBIGUOUS"}
    lead = candidates[0]
    found = [(i, c) for i, c in
             enumerate((lead.get("price_drop_v2") or {}).get("campaigns") or [])
             if c.get("provider_message_id") == mid]
    if len(found) != 1:
        return {"status": "UNMATCHED_OR_AMBIGUOUS"}
    index, campaign = found[0]
    current = campaign.get("provider_status") or "accepted"
    if current == "read" or current == state or (
            current == "delivered" and state == "accepted"):
        return {"status": "UNCHANGED"}
    field = f"{ROOT}.{index}"
    stamp = utc(now or datetime.now(timezone.utc))
    allowed = ["accepted", "sent", "unknown"] if state == "delivered" else [
        "accepted", "sent", "unknown", "delivered"
    ]
    update = {"$set": {f"{field}.provider_status": state,
                       f"{field}.last_provider_update_at": stamp}}
    if state == "delivered":
        update["$set"][f"{field}.delivered_at"] = stamp
    if state == "read":
        update["$set"][f"{field}.read_at"] = stamp
    result = db[LEADS].update_one({
        "_id": lead["_id"], f"{field}.provider_message_id": mid,
        f"{field}.provider_status": {"$in": allowed},
    }, update)
    return {"status": "UPDATED" if result.modified_count else "UNCHANGED"}


def send_test_notice_once(db, *, role: str,
                          sender: Callable[[str, str], dict] | None = None,
                          now=None) -> dict:
    """Send one simulated internal notice to the allowlisted tester only.

    Both roles target the TEST phone, never employee numbers. Each attempted
    provider call is claimed on the same lead before I/O; an uncertain call
    is quarantined rather than retried.
    """
    if os.getenv(TEST_NOTICE_FLAG, "").lower() not in {"1", "true", "yes"}:
        return {"status": "DISABLED"}
    if role not in {"administrator", "property_executive"}:
        return {"status": "INVALID_ROLE"}
    if role == "property_executive" and not executive_notices_enabled():
        return {"status": "EXECUTIVE_NOTICE_FROZEN"}
    tester = normalize_phone_strict(os.getenv(TEST_PHONE_FLAG, ""))
    if not tester:
        return {"status": "TEST_PHONE_NOT_CONFIGURED"}
    if role == "administrator":
        configured_admin = normalize_phone_strict(os.getenv(ADMIN_PHONE_FLAG, ""))
        if configured_admin != APPROVED_ADMIN_PHONE or tester != configured_admin:
            return {"status": "ADMIN_PHONE_NOT_ALLOWLISTED"}
    candidates = list(db[LEADS].find({
        "$or": [{"phone": tester}, {"contact_phone_normalized": tester}],
        f"{ROOT}.notifications.{role}.status": "PENDING_SIMULATION",
    }, {"_id": 1, "phone": 1, "contact_phone_normalized": 1,
        "price_drop_v2.campaigns": 1}).limit(2))
    selected = []
    for lead in candidates:
        if not _same_phone(lead, tester):
            continue
        for i, c in enumerate((lead.get("price_drop_v2") or {}).get("campaigns") or []):
            if (c.get("test_mode") is True
                    and c.get("phone_normalized") == tester
                    and c.get("provider_message_id")
                    and c.get("has_positive_response")
                    and c.get("notifications", {}).get(role, {}).get("status") == "PENDING_SIMULATION"):
                selected.append((lead["_id"], i, c))
    if not selected:
        return {"status": "IDLE"}
    # Stable first selection avoids nondeterministic notification order.
    selected.sort(key=lambda row: (str(row[2].get("campaign_id")), str(row[0])))
    lead_id, index, campaign = selected[0]
    field = f"{ROOT}.{index}"
    stamp = utc(now or datetime.now(timezone.utc))
    # Simulated internal alerts share the customer test phone. Do not compete
    # with replies: postpone both notices until the customer has been idle.
    recent_reply = campaign.get("last_customer_reply_at")
    if isinstance(recent_reply, datetime) and stamp - utc(recent_reply) < timedelta(seconds=120):
        return {"status": "DEFERRED_CUSTOMER_ACTIVE"}
    for other_notice in (campaign.get("notifications") or {}).values():
        sent_at = other_notice.get("sent_at") if isinstance(other_notice, dict) else None
        if isinstance(sent_at, datetime) and stamp - utc(sent_at) < timedelta(seconds=90):
            return {"status": "DEFERRED_INTERNAL_THROTTLE"}
    claim = db[LEADS].update_one({
        "_id": lead_id, f"{field}.campaign_id": campaign["campaign_id"],
        f"{field}.test_mode": True,
        f"{field}.notifications.{role}.status": "PENDING_SIMULATION",
    }, {"$set": {
        f"{field}.notifications.{role}.status": "CLAIMED",
        f"{field}.notifications.{role}.attempted_at": stamp,
    }})
    if claim.modified_count != 1:
        return {"status": "ALREADY_CLAIMED"}
    role_name = "ADMINISTRADOR" if role == "administrator" else "EJECUTIVO DE LA PROPIEDAD"
    old_text = f"{campaign['old_price_uf']:,}".replace(",", ".")
    new_text = f"{campaign['new_price_uf']:,}".replace(",", ".")
    from .lead_router import build_secure_crm_url
    crm_link = build_secure_crm_url({"_id": lead_id})
    message = (
        f"PRUEBA INTERNA — AVISO SIMULADO AL {role_name}\n\n"
        "🔔 PROCASA — Interesado en rebaja de precio\n"
        f"🏠 Propiedad: {campaign['property_code']}\n"
        f"📉 UF {old_text} → UF {new_text}\n"
        f"📱 Cliente de prueba: {tester}\n"
        f"💬 Respuesta: {str(campaign.get('last_customer_text') or 'Interés registrado')[:160]}\n"
        f"🔗 Abrir lead en CRM: {crm_link}\n"
        "Aviso de prueba: no se notificó a un ejecutivo real."
    )
    if sender is None:
        from .whatsapp_client import send_whatsapp_message_detailed_sync
        sender = send_whatsapp_message_detailed_sync
    try:
        receipt = sender(tester, message)
    except Exception:
        receipt = {"success": False, "provider_call_uncertain": True}
    accepted = bool(receipt.get("success") and receipt.get("provider_message_id")
                    and not receipt.get("provider_call_uncertain"))
    status = "SENT" if accepted else "DELIVERY_UNKNOWN"
    db[LEADS].update_one({
        "_id": lead_id, f"{field}.campaign_id": campaign["campaign_id"],
        f"{field}.notifications.{role}.status": "CLAIMED",
    }, {"$set": {
        f"{field}.notifications.{role}.status": status,
        f"{field}.notifications.{role}.provider_message_id": receipt.get("provider_message_id"),
        f"{field}.notifications.{role}.completed_at": stamp,
        f"{field}.notifications.{role}.sent_to": "TEST_PHONE_ONLY",
        **({f"{field}.notifications.{role}.sent_at": stamp} if accepted else {}),
    }})
    return {"status": status, "campaign_id": campaign["campaign_id"],
            "role": role, "recipient_last4": tester[-4:]}



def send_real_admin_notice_once(db, *,
                                sender: Callable[[str, str], dict] | None = None,
                                now=None) -> dict:
    """Single, separately authorized administrator alert for a real V2 response.

    The WhatsApp recipient is the allowlisted administrator, never an executive.
    The notice is stored on the canonical lead and is never retried after an
    uncertain provider attempt. Disabled by default independently of campaigns.
    """
    if os.getenv(REAL_ADMIN_NOTICE_FLAG, "").lower() not in {"1", "true", "yes"}:
        return {"status": "DISABLED"}
    admin = normalize_phone_strict(os.getenv(ADMIN_PHONE_FLAG, ""))
    if admin != APPROVED_ADMIN_PHONE:
        return {"status": "ADMIN_PHONE_NOT_ALLOWLISTED"}
    candidates = list(db[LEADS].find({
        "price_drop_v2.campaigns": {"$elemMatch": {
            "test_mode": False, "status": "RESPONDED",
            "notifications.administrator.status": "PENDING_HUMAN_REVIEW",
        }},
    }, {"_id": 1, "phone": 1, "contact_phone_normalized": 1,
        "conversation_status": 1, "price_drop_v2": 1,
        "price_drop_campaign_v1.opted_out": 1}).limit(20))
    selected = []
    for lead in candidates:
        if lead_must_not_be_contacted(lead):
            continue
        for i, campaign in enumerate((lead.get("price_drop_v2") or {}).get("campaigns") or []):
            if not isinstance(campaign, dict):
                continue
            notice = (campaign.get("notifications") or {}).get("administrator") or {}
            if (campaign.get("test_mode") is False
                    and campaign.get("status") == "RESPONDED"
                    and campaign.get("provider_message_id")
                    and campaign.get("has_positive_response") is True
                    and notice.get("status") == "PENDING_HUMAN_REVIEW"):
                selected.append((lead, i, campaign))
    if not selected:
        return {"status": "IDLE"}
    selected.sort(key=lambda x: (str(x[2].get("campaign_id")), str(x[0]["_id"])))
    lead, index, campaign = selected[0]
    stamp = utc(now or datetime.now(timezone.utc))
    last_reply = campaign.get("last_customer_reply_at")
    if isinstance(last_reply, datetime) and stamp - utc(last_reply) < timedelta(seconds=120):
        return {"status": "DEFERRED_CUSTOMER_ACTIVE"}

    from .lead_router import build_secure_crm_url
    try:
        crm_link = build_secure_crm_url({"_id": lead["_id"]})
    except Exception:
        return {"status": "BLOCKED_CRM_URL"}
    if not str(crm_link or "").startswith("https://"):
        return {"status": "BLOCKED_CRM_URL"}
    phone = normalize_phone_strict(lead.get("contact_phone_normalized") or lead.get("phone") or "")
    if not phone or phone == admin or not _same_phone(lead, phone):
        return {"status": "BLOCKED_CONTACT_IDENTITY"}
    currency = str(campaign.get("currency") or "UF")
    old = campaign.get("old_price_clp") if currency == "CLP" else campaign.get("old_price_uf")
    new = campaign.get("new_price_clp") if currency == "CLP" else campaign.get("new_price_uf")
    if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return {"status": "BLOCKED_CAMPAIGN_PRICE"}
    if currency not in {"UF", "CLP"}:
        return {"status": "BLOCKED_CAMPAIGN_CURRENCY"}
    def price_text(value):
        if currency == "CLP":
            return "$" + f"{int(value):,}".replace(",", ".")
        return "UF " + f"{int(value):,}".replace(",", ".")

    code = str(campaign.get("property_code") or "")
    campaign_id = str(campaign.get("campaign_id") or "")
    last_positive = str(campaign.get("last_positive_reply_text") or
                        campaign.get("first_positive_reply_text") or "")[:160]
    classification = (campaign.get("last_positive_classification")
                      or campaign.get("last_classification") or {})
    signal = {
        "PRIORIDAD_COMERCIAL": "Visita, oferta o contacto solicitado",
        "INTERES_DECLARADO": "Interés declarado",
        "BUSCA_ALTERNATIVAS": "Solicita otras propiedades",
    }.get(classification.get("label"), "Interés comercial registrado")
    reason = str(classification.get("reason") or "interest_recorded")
    preferences = campaign.get("customer_preferences") or {}
    preference_lines = []
    if preferences.get("do_not_call") is True:
        preference_lines.append("Preferencia vigente: no llamar al cliente.")
    if preferences.get("visit_declined") is True:
        preference_lines.append("No proponer visita mientras esa preferencia siga vigente.")
    notice_lines = [
        "🔔 PROCASA — Respuesta a campaña de rebaja",
        f"Propiedad: {code} | {price_text(old)} → {price_text(new)}",
        f"Cliente: {phone}",
        f"Señal: {signal} ({reason})",
        f"Respuesta: {last_positive or 'Interés registrado'}",
        *preference_lines,
        f"CRM: {crm_link}",
        "Revisar conversación antes de gestionar. No es un lead nuevo.",
    ]
    notice_text = "\n".join(notice_lines)
    field = f"{ROOT}.{index}"
    claimed = db[LEADS].update_one({
        "_id": lead["_id"],
        f"{field}.campaign_id": campaign_id,
        f"{field}.status": "RESPONDED",
        f"{field}.test_mode": False,
        f"{field}.notifications.administrator.status": "PENDING_HUMAN_REVIEW",
    }, {"$set": {
        f"{field}.notifications.administrator.status": "CLAIMED",
        f"{field}.notifications.administrator.attempted_at": stamp,
        f"{field}.notifications.administrator.message": notice_text,
        f"{field}.notifications.administrator.sent_to": admin,
    }})
    if claimed.modified_count != 1:
        return {"status": "ALREADY_CLAIMED"}
    if sender is None:
        from .whatsapp_client import send_whatsapp_message_detailed_sync
        sender = send_whatsapp_message_detailed_sync
    try:
        receipt = sender(admin, notice_text)
    except Exception:
        receipt = {"success": False, "provider_call_uncertain": True}
    accepted = bool(receipt.get("success") and receipt.get("provider_message_id")
                    and not receipt.get("provider_call_uncertain"))
    status = "SENT" if accepted else "DELIVERY_UNKNOWN"
    fields = {
        f"{field}.notifications.administrator.status": status,
        f"{field}.notifications.administrator.provider_message_id": receipt.get("provider_message_id"),
        f"{field}.notifications.administrator.provider_status": str(receipt.get("delivery_status") or "unknown")[:40],
        f"{field}.notifications.administrator.completed_at": stamp,
    }
    if accepted:
        fields[f"{field}.notifications.administrator.sent_at"] = stamp
    db[LEADS].update_one({
        "_id": lead["_id"], f"{field}.campaign_id": campaign_id,
        f"{field}.notifications.administrator.status": "CLAIMED",
    }, {"$set": fields})
    return {"status": status, "campaign_id": campaign_id,
            "recipient_last4": admin[-4:],
            "provider_message_id": receipt.get("provider_message_id")}


def send_real_admin_info_notice_once(db, *,
                                    sender: Callable[[str, str], dict] | None = None,
                                    now=None) -> dict:
    """Send one admin-only note for an opt-out or completed property search.

    This channel is independent from positive commercial alerts. It never
    addresses the customer and is gated by the same explicit admin allowlist.
    A failed or uncertain provider call is not automatically retried.
    """
    if os.getenv(REAL_ADMIN_NOTICE_FLAG, "").lower() not in {"1", "true", "yes"}:
        return {"status": "DISABLED"}
    admin = normalize_phone_strict(os.getenv(ADMIN_PHONE_FLAG, ""))
    if admin != APPROVED_ADMIN_PHONE:
        return {"status": "ADMIN_PHONE_NOT_ALLOWLISTED"}
    candidates = list(db[LEADS].find({
        "price_drop_v2.campaigns": {"$elemMatch": {
            "test_mode": False, "status": "OPTED_OUT",
            "notifications.administrator_info.status": "PENDING_INFO",
        }},
    }, {"_id": 1, "phone": 1, "contact_phone_normalized": 1,
        "price_drop_v2": 1}).limit(20))
    selected = []
    for lead in candidates:
        for index, campaign in enumerate((lead.get("price_drop_v2") or {}).get("campaigns") or []):
            info = ((campaign.get("notifications") or {}).get("administrator_info") or {})
            classification = (campaign.get("last_classification") or {})
            reason = str(info.get("reason") or classification.get("reason") or "")
            if (campaign.get("test_mode") is False
                    and campaign.get("status") == "OPTED_OUT"
                    and campaign.get("provider_message_id")
                    and info.get("status") == "PENDING_INFO"
                    and reason in {"explicit_opt_out", "already_found_property"}):
                selected.append((lead, index, campaign, reason))
    if not selected:
        return {"status": "IDLE"}
    selected.sort(key=lambda row: (str(row[2].get("campaign_id")), str(row[0]["_id"])))
    lead, index, campaign, reason = selected[0]
    stamp = utc(now or datetime.now(timezone.utc))
    from .lead_router import build_secure_crm_url
    try:
        crm_link = build_secure_crm_url({"_id": lead["_id"]})
    except Exception:
        return {"status": "BLOCKED_CRM_URL"}
    if not str(crm_link or "").startswith("https://"):
        return {"status": "BLOCKED_CRM_URL"}
    phone = normalize_phone_strict(
        lead.get("contact_phone_normalized") or lead.get("phone") or ""
    )
    if not phone or phone == admin or not _same_phone(lead, phone):
        return {"status": "BLOCKED_CONTACT_IDENTITY"}
    currency = str(campaign.get("currency") or "UF").upper()
    old = campaign.get("old_price_clp") if currency == "CLP" else campaign.get("old_price_uf")
    new = campaign.get("new_price_clp") if currency == "CLP" else campaign.get("new_price_uf")
    if currency not in {"UF", "CLP"} or not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return {"status": "BLOCKED_CAMPAIGN_PRICE"}

    def price_text(value):
        return "$" + f"{int(value):,}".replace(",", ".") if currency == "CLP" else (
            "UF " + f"{int(value):,}".replace(",", ".")
        )

    code = str(campaign.get("property_code") or "")
    campaign_id = str(campaign.get("campaign_id") or "")
    response = str(campaign.get("last_customer_text") or "")[:160]
    label = "Búsqueda de propiedad terminada" if reason == "already_found_property" else "Solicita no recibir más mensajes"
    text = (
        "ℹ️ PROCASA — Aviso informativo de campaña de rebaja\n"
        f"Propiedad: {code} | {price_text(old)} → {price_text(new)}\n"
        f"Cliente: {phone}\nMotivo: {label} ({reason})\n"
        f"Respuesta: {response or 'Solicitud registrada'}\nCRM: {crm_link}\n"
        "Exclusión global aplicada. No contactar nuevamente al cliente."
    )
    field = f"{ROOT}.{index}"
    claimed = db[LEADS].update_one({
        "_id": lead["_id"],
        f"{field}.campaign_id": campaign_id,
        f"{field}.status": "OPTED_OUT",
        f"{field}.test_mode": False,
        f"{field}.notifications.administrator_info.status": "PENDING_INFO",
    }, {"$set": {
        f"{field}.notifications.administrator_info.status": "CLAIMED",
        f"{field}.notifications.administrator_info.attempted_at": stamp,
        f"{field}.notifications.administrator_info.message": text,
        f"{field}.notifications.administrator_info.sent_to": admin,
    }})
    if claimed.modified_count != 1:
        return {"status": "ALREADY_CLAIMED"}
    if sender is None:
        from .whatsapp_client import send_whatsapp_message_detailed_sync
        sender = send_whatsapp_message_detailed_sync
    try:
        receipt = sender(admin, text)
    except Exception:
        receipt = {"success": False, "provider_call_uncertain": True}
    accepted = bool(receipt.get("success") and receipt.get("provider_message_id")
                    and not receipt.get("provider_call_uncertain"))
    status = "SENT" if accepted else "DELIVERY_UNKNOWN"
    fields = {
        f"{field}.notifications.administrator_info.status": status,
        f"{field}.notifications.administrator_info.provider_message_id": receipt.get("provider_message_id"),
        f"{field}.notifications.administrator_info.provider_status": str(receipt.get("delivery_status") or "unknown")[:40],
        f"{field}.notifications.administrator_info.completed_at": stamp,
    }
    if accepted:
        fields[f"{field}.notifications.administrator_info.sent_at"] = stamp
    db[LEADS].update_one({
        "_id": lead["_id"], f"{field}.campaign_id": campaign_id,
        f"{field}.notifications.administrator_info.status": "CLAIMED",
    }, {"$set": fields})
    return {"status": status, "campaign_id": campaign_id,
            "recipient_last4": admin[-4:],
            "provider_message_id": receipt.get("provider_message_id")}
