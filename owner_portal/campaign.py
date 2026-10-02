"""Signed, campaign-snapshot-backed property portal helpers."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import urlencode

from campanas.owner_campaign_live_events import (
    decode_live_token,
    issue_live_token,
    persist_live_event,
)
from campanas.owner_campaign_live_config import (
    EXCLUDED_OWNER_EMAILS,
    MANUAL_CAMPAIGN_EXCLUDED_CODES,
    MANUAL_RECENT_EXCLUSION_CODES,
    WAVE2_AMBIGUOUS_OWNER_CODES,
)
from config import Config


ACCESS_SOURCES = frozenset({"EMAIL", "WHATSAPP"})
PUBLIC_ACCESS_STATUSES = frozenset({
    "SENT", "DELIVERY_UNKNOWN", "SKIPPED_STALE_OR_MISMATCH",
})
STALE_STATUSES = frozenset({"SKIPPED_STALE_OR_MISMATCH"})
EXCLUDED_STATUSES = frozenset({"EXCLUDED", "SKIPPED_EXCLUDED", "AMBIGUOUS_OWNER_IDENTITY"})
REPORT_TYPES = frozenset({"COMMUNAL_MARKET_REPORT", "INDIVIDUAL_APPRAISAL"})
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
LEDGER_COLLECTION = Config.COLLECTION_CAMPANAS_LOG
MASTER_COLLECTION = "universo_cartera_prop360"
SHORT_KEY_HEX_LENGTH = 32  # 128 bits derived from the registered signed-token SHA-256.
logger = logging.getLogger(__name__)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _safe_code(value: Any) -> str:
    code = _clean(value)
    return code if code.isdigit() and len(code) <= 12 else ""


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def short_key_for_token_hash(token_hash: Any) -> str:
    """Use a non-enumerable 128-bit prefix of the existing signed-token hash."""
    value = _clean(token_hash).casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        return ""
    return value[:SHORT_KEY_HEX_LENGTH]


def build_short_portal_url(base_url: str, token_hash: Any, *, source: str = "EMAIL") -> str:
    if source not in ACCESS_SOURCES:
        raise ValueError("invalid_portal_source")
    key = short_key_for_token_hash(token_hash)
    if not key:
        raise ValueError("invalid_portal_token_hash")
    return f"{base_url.rstrip('/')}/p/{key}?{urlencode({'source': source})}"


def _valid_email(value: Any) -> str:
    email = _clean(value).casefold()
    return email if EMAIL_RE.fullmatch(email) else ""


def _excluded(row: Mapping[str, Any]) -> bool:
    code = _safe_code(row.get("property_code"))
    email = _valid_email(row.get("owner_email"))
    return (
        not code
        or code in MANUAL_CAMPAIGN_EXCLUDED_CODES
        or code in MANUAL_RECENT_EXCLUSION_CODES
        or code in WAVE2_AMBIGUOUS_OWNER_CODES
        or not email
        or email in EXCLUDED_OWNER_EMAILS
    )


def _token_event_id(campaign_id: str, property_code: str, recipient: str, expires_at: int) -> str:
    raw = f"owner-portal-v1|{campaign_id}|{property_code}|{recipient}|{expires_at}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def issue_portal_token(row: Mapping[str, Any], *, expires_at: int) -> str:
    if _excluded(row):
        raise ValueError("portal_access_not_eligible")
    status = _clean(row.get("send_status")).upper()
    if status not in PUBLIC_ACCESS_STATUSES or status in EXCLUDED_STATUSES:
        raise ValueError("portal_status_not_eligible")
    campaign_id = _clean(row.get("campaign_id"))
    code = _safe_code(row.get("property_code"))
    email = _valid_email(row.get("owner_email"))
    event_id = _token_event_id(campaign_id, code, email, int(expires_at))
    return issue_live_token(
        campaign_id=campaign_id,
        property_code=code,
        action="portal_opened",
        recipient=email,
        expires_at=int(expires_at),
        event_id=event_id,
    )


def portal_access_record(token: str, *, expires_at: datetime) -> dict[str, Any]:
    claims = decode_live_token(token)
    if claims is None or claims.get("action") != "portal_opened":
        raise ValueError("portal_token_signing_failed")
    return {
        "token_id": str(claims["event_id"]),
        "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "issued_at": datetime.now(timezone.utc),
        "expires_at": expires_at,
        "revoked_at": None,
        "status": "ACTIVE",
        "purpose": "owner_portal_view",
    }


def materialize_portal_accesses(
    db: Any,
    *,
    campaign_id: str,
    base_url: str,
    source: str = "EMAIL",
    now: datetime | None = None,
    ttl_days: int = 90,
    persist: bool = True,
) -> dict[str, Any]:
    """Register/reuse one signed portal identity; source is link metadata only."""
    if not campaign_id or "test" in campaign_id.casefold() or ttl_days < 1 or ttl_days > 120:
        raise ValueError("invalid_campaign_portal_scope")
    if source not in ACCESS_SOURCES:
        raise ValueError("invalid_portal_source")
    current = now or datetime.now(timezone.utc)
    ledger = db[LEDGER_COLLECTION]
    counts: dict[str, int] = {"eligible": 0, "created": 0, "reused": 0, "blocked": 0}
    records: list[dict[str, Any]] = []
    cursor = ledger.find({"campaign_id": campaign_id}).sort("property_code", 1)
    for row in cursor:
        status = _clean(row.get("send_status")).upper()
        code = _safe_code(row.get("property_code"))
        email = _valid_email(row.get("owner_email"))
        if status not in PUBLIC_ACCESS_STATUSES or status in EXCLUDED_STATUSES or _excluded(row):
            counts["blocked"] += 1
            continue
        if not code or not email or row.get("_id") != f"{campaign_id}:{code}":
            counts["blocked"] += 1
            continue
        counts["eligible"] += 1
        old = row.get("portal_access") or {}
        old_exp = old.get("expires_at")
        old_active = (
            old.get("status") == "ACTIVE"
            and isinstance(old_exp, datetime)
            and _as_utc(old_exp) > _as_utc(current)
        )
        expiry = old_exp if old_active else current + timedelta(days=ttl_days)
        expiry_epoch = int(_as_utc(expiry).timestamp())
        token = issue_portal_token(row, expires_at=expiry_epoch)
        access = portal_access_record(token, expires_at=expiry)
        if old_active:
            if old.get("token_hash") != access["token_hash"]:
                counts["blocked"] += 1
                counts["eligible"] -= 1
                continue
            counts["reused"] += 1
        else:
            counts["created"] += 1
        if persist and not old_active:
            result = ledger.update_one(
                {"_id": row["_id"], "campaign_id": campaign_id, "property_code": code,
                 "owner_email": row.get("owner_email"), "send_status": status},
                {"$set": {"portal_access": access}},
            )
            if result.modified_count != 1 and not old_active:
                counts["created"] -= 1
                counts["blocked"] += 1
        url = build_short_portal_url(base_url, access.get("token_hash"), source=source)
        records.append({"campaign_id": campaign_id, "property_code": code, "source": source,
                        "send_status": status, "document_type": _clean(row.get("document_type")).upper(),
                        "executive": _clean(row.get("executive_name") or row.get("executive")),
                        "owner_email": email, "private_url": url, "token_expires_at": expiry})
    return {"campaign_id": campaign_id, "source": source, "counts": counts,
            "records": records}


def build_portal_url(base_url: str, property_code: str, token: str, *, source: str = "EMAIL") -> str:
    if source not in ACCESS_SOURCES:
        raise ValueError("invalid_portal_source")
    return f"{base_url.rstrip('/')}/ajuste/{property_code}?{urlencode({'token': token, 'source': source})}"


def get_or_create_portal_url(
    db: Any, row: Mapping[str, Any], *, base_url: str, source: str,
    now: datetime | None = None, ttl_days: int = 90,
) -> str:
    """Return a reproducible URL only when its hash/expiry is ledger-registered."""
    current = now or datetime.now(timezone.utc)
    if source not in ACCESS_SOURCES:
        raise ValueError("invalid_portal_source")
    access = row.get("portal_access") or {}
    expiry = access.get("expires_at")
    if (access.get("status") != "ACTIVE" or not isinstance(expiry, datetime)
            or _as_utc(expiry) <= current):
        expiry = current + timedelta(days=ttl_days)
        token = issue_portal_token(row, expires_at=int(_as_utc(expiry).timestamp()))
        access = portal_access_record(token, expires_at=expiry)
        key = {"_id": row.get("_id"), "campaign_id": row.get("campaign_id"),
               "property_code": row.get("property_code"), "owner_email": row.get("owner_email"),
               "send_status": row.get("send_status")}
        result = db[LEDGER_COLLECTION].update_one(key, {"$set": {"portal_access": access}})
        if result.modified_count != 1:
            raise LookupError("campaign_portal_access_not_persisted")
    token = issue_portal_token(row, expires_at=int(_as_utc(expiry).timestamp()))
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(digest, str(access.get("token_hash") or "")):
        raise LookupError("campaign_portal_access_integrity_error")
    return build_short_portal_url(base_url, digest, source=source)


def _master_identity(db: Any, code: str) -> dict[str, str]:
    master = db[MASTER_COLLECTION].find_one(
        {"codigo": {"$in": [code, int(code)]}},
        {"_id": 0, "codigo": 1, "metadata.tipo_propiedad": 1, "ubicacion.comuna": 1},
    ) or {}
    return {
        "property_type": _clean(_path(master, "metadata.tipo_propiedad")) or "Propiedad",
        "commune": _clean(_path(master, "ubicacion.comuna")) or "Comuna no disponible",
    }


def _path(document: Mapping[str, Any], dotted: str) -> Any:
    value: Any = document
    for part in dotted.split("."):
        value = value.get(part) if isinstance(value, Mapping) else None
    return value


def verify_portal_request(db: Any, *, property_code: str, token: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    code = _safe_code(property_code)
    claims = decode_live_token(token)
    if not code or claims is None:
        return None
    if (claims.get("action") != "portal_opened" or claims.get("test_mode") is not False
            or str(claims.get("property_code")) != code
            or claims.get("source") not in (None, *ACCESS_SOURCES)):
        return None
    campaign_id = _clean(claims.get("campaign_id"))
    recipient = _valid_email(claims.get("recipient"))
    if not campaign_id or not recipient:
        return None
    row = db[LEDGER_COLLECTION].find_one({
        "_id": f"{campaign_id}:{code}", "campaign_id": campaign_id,
        "property_code": code,
    })
    if not row or _excluded(row) or _valid_email(row.get("owner_email")) != recipient:
        return None
    status = _clean(row.get("send_status")).upper()
    if status not in PUBLIC_ACCESS_STATUSES or status in EXCLUDED_STATUSES:
        return None
    access = row.get("portal_access") or {}
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expiry = access.get("expires_at")
    if (access.get("status") != "ACTIVE" or not isinstance(expiry, datetime)
            or access.get("revoked_at") is not None
            or _as_utc(expiry) <= datetime.now(timezone.utc)
            or int(claims.get("exp", 0)) != int(_as_utc(expiry).timestamp())
            or not hmac.compare_digest(digest, str(access.get("token_hash") or ""))):
        return None
    return row, claims


def resolve_short_portal_request(
    db: Any, *, access_key: str, source: str = "EMAIL",
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Resolve a short key only when exactly one active, unexpired signed access matches."""
    key = _clean(access_key).casefold()
    if (not re.fullmatch(rf"[0-9a-f]{{{SHORT_KEY_HEX_LENGTH}}}", key)
            or source not in ACCESS_SOURCES):
        return None
    current = now or datetime.now(timezone.utc)
    cursor = db[LEDGER_COLLECTION].find({
        "portal_access.status": "ACTIVE",
        "portal_access.revoked_at": None,
        "portal_access.expires_at": {"$gt": current},
        "portal_access.token_hash": {"$regex": "^" + re.escape(key)},
    }).limit(2)
    matches = list(cursor)
    if len(matches) != 1:
        if len(matches) > 1:
            logger.warning("owner_portal_short_key_collision match_count=%s", len(matches))
        return None
    row = matches[0]
    access = row.get("portal_access") or {}
    expiry = access.get("expires_at")
    if not isinstance(expiry, datetime) or _as_utc(expiry) <= _as_utc(current):
        return None
    try:
        token = issue_portal_token(row, expires_at=int(_as_utc(expiry).timestamp()))
    except (ValueError, KeyError, TypeError):
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    stored_digest = str(access.get("token_hash") or "").casefold()
    if (not hmac.compare_digest(digest, stored_digest)
            or not hmac.compare_digest(short_key_for_token_hash(stored_digest), key)):
        return None
    return verify_portal_request(db, property_code=str(row.get("property_code") or ""), token=token)


def build_private_page_view(
    db: Any, row: Mapping[str, Any], claims: Mapping[str, Any], *,
    base_url: str, source: str | None = None,
) -> dict[str, Any]:
    from campanas.owner_campaign_live_events import issue_live_token

    code = _safe_code(row.get("property_code"))
    campaign_id = _clean(row.get("campaign_id"))
    recipient = _valid_email(row.get("owner_email"))
    source = source or str(claims.get("source") or "EMAIL")
    if source not in ACCESS_SOURCES:
        raise ValueError("invalid_portal_source")
    expiry = int(claims["exp"])
    snapshot = row.get("campaign_snapshot") if isinstance(row.get("campaign_snapshot"), Mapping) else {}
    operation = _clean(snapshot.get("operation_resolved") or snapshot.get("operation") or row.get("operation_resolved") or row.get("operation")).upper()
    status = _clean(row.get("send_status")).upper()
    document_type = _clean(snapshot.get("document_type") or row.get("document_type")).upper() or "NONE"
    stale = status in STALE_STATUSES
    excluded = status in EXCLUDED_STATUSES or _excluded(row)
    already_authorized = _clean(row.get("authorization_status")).upper() == "PRICE_AUTHORIZED"

    def action_url(
        action: str, document: str | None = None, *, cta_placement: str = "ORIGINAL",
    ) -> str:
        token = issue_live_token(
            campaign_id=campaign_id, property_code=code, action=action,
            recipient=recipient, document_type=document, expires_at=expiry,
            source=source, interaction_surface="OWNER_PORTAL",
            cta_placement=cta_placement,
        )
        if action == "ver_informe":
            return f"{base_url.rstrip('/')}/campana/informe?{urlencode({'token': token})}"
        query = urlencode({
            "email": recipient, "accion": action, "codigos": code,
            "campana": campaign_id, "mode": "owner_campaign", "token": token,
        })
        return f"{base_url.rstrip('/')}/campana/respuesta?{query}"

    report_url = action_url("ver_informe", document_type, cta_placement="ORIGINAL") if document_type in REPORT_TYPES else ""
    def frozen_value(key: str) -> Any:
        return snapshot.get(key) if snapshot.get(key) is not None else row.get(key)

    current = frozen_value("current_price")
    recommended = frozen_value("recommended_price")
    current_clp = frozen_value("current_price_clp")
    recommended_clp = frozen_value("recommended_price_clp")
    if not report_url:
        report_label = ""
    else:
        report_label = "VER / DESCARGAR INFORME"
    saved_model = (
        snapshot.get("email_render_model") if isinstance(snapshot.get("email_render_model"), Mapping)
        else snapshot.get("render_model") if isinstance(snapshot.get("render_model"), Mapping)
        else row.get("email_render_model") if isinstance(row.get("email_render_model"), Mapping)
        else {}
    )
    recommendation_reason = _clean(
        snapshot.get("single_recommendation_text") or snapshot.get("recommendation_text")
        or saved_model.get("single_recommendation_text") or saved_model.get("recommendation_text")
    )
    safe_mode = stale or excluded
    advisor_url = action_url("contactar_ejecutivo", cta_placement="ORIGINAL")
    can_authorize = not safe_mode and not already_authorized and current is not None and recommended is not None
    top_primary_url = action_url("aceptar_rebaja", cta_placement="TOP") if can_authorize else ""
    sticky_primary_url = action_url("aceptar_rebaja", cta_placement="STICKY") if can_authorize else ""
    sticky_advisor_url = action_url("contactar_ejecutivo", cta_placement="STICKY")
    attempts = row.get("send_attempts") if isinstance(row.get("send_attempts"), list) else []
    dated_attempts = [
        item for item in attempts if isinstance(item, Mapping)
        and isinstance(item.get("sent_at") or item.get("attempted_at"), datetime)
    ]
    sent_at = (dated_attempts[-1].get("sent_at") or dated_attempts[-1].get("attempted_at")) if dated_attempts else None
    return {
        "logo_url": f"{base_url.rstrip('/')}/static/logo.png",
        "property_code": code,
        "operation": operation.title() if operation else "Operación no disponible",
        "property_type": _clean(snapshot.get("property_type") or row.get("property_type")),
        "commune": _clean(snapshot.get("commune") or row.get("commune")),
        "current_price": current,
        "current_price_label": _format_client_price(current, operation),
        "current_price_clp_label": _format_client_price(current_clp, operation, clp=True),
        "recommended_adjustment_pct": frozen_value("recommended_adjustment_pct"),
        "recommended_price": recommended,
        "recommended_price_label": _format_client_price(recommended, operation),
        "recommended_price_clp_label": _format_client_price(recommended_clp, operation, clp=True),
        "leads_90d": frozen_value("leads_90d"),
        "comparable_mode": _clean(frozen_value("comparable_mode") or row.get("comparable_status")),
        "comparable_count": frozen_value("comparable_count"),
        "recommendation_reason": recommendation_reason,
        "executive_name": _clean(row.get("executive_name") or row.get("executive")) or "Tu ejecutivo PROCASA",
        "document_available": document_type in REPORT_TYPES,
        "document_label": report_label,
        "report_url": report_url,
        "advisor_url": advisor_url,
        "primary_url": action_url("aceptar_rebaja", cta_placement="ORIGINAL") if can_authorize else "",
        "top_primary_url": top_primary_url,
        "sticky_primary_url": sticky_primary_url,
        "sticky_advisor_url": sticky_advisor_url,
        "safe_mode": safe_mode,
        "already_authorized": already_authorized,
        "source": source,
        "document_type": document_type,
        "snapshot": dict(snapshot),
        "sent_at": sent_at,
    }


def build_email_visual_landing_html(
    row: Mapping[str, Any], view: Mapping[str, Any], *, base_url: str,
) -> str:
    """Render the approved single-property email template from frozen campaign fields."""
    from analytics.owner_campaign_email_v2 import render_owner_campaign_email_v2, _price_label, _clp_price_label

    snapshot = row.get("campaign_snapshot") if isinstance(row.get("campaign_snapshot"), Mapping) else {}
    operation = _clean(snapshot.get("operation_resolved") or snapshot.get("operation") or row.get("operation_resolved") or row.get("operation")).upper()
    is_rental = operation == "ARRIENDO"
    document_type = _clean(snapshot.get("document_type") or row.get("document_type")).upper() or "NONE"
    report_visible = document_type in REPORT_TYPES
    stale = _clean(row.get("send_status")).upper() in STALE_STATUSES
    can_authorize = bool(view.get("primary_url")) and not stale
    if not can_authorize:
        # The approved template's non-price branch presents only the advisor action.
        display_recommended_price = None
    else:
        display_recommended_price = view.get("recommended_price_label")
    saved_model = (
        snapshot.get("email_render_model") if isinstance(snapshot.get("email_render_model"), Mapping)
        else snapshot.get("render_model") if isinstance(snapshot.get("render_model"), Mapping)
        else row.get("email_render_model") if isinstance(row.get("email_render_model"), Mapping)
        else {}
    )
    recommendation_reason = _clean(view.get("recommendation_reason"))
    prop_type = _clean(snapshot.get("property_type") or row.get("property_type"))
    commune = _clean(snapshot.get("commune") or row.get("commune"))
    saved_comparable = saved_model.get("comparable") if isinstance(saved_model.get("comparable"), Mapping) else {}
    saved_market_reference = saved_model.get("market_reference") if isinstance(saved_model.get("market_reference"), Mapping) else {}
    saved_appraisal = saved_model.get("appraisal") if isinstance(saved_model.get("appraisal"), Mapping) else {}
    leads = snapshot.get("leads_90d") if snapshot.get("leads_90d") is not None else row.get("leads_90d")
    activity = saved_model.get("activity_90d") if isinstance(saved_model.get("activity_90d"), Mapping) else {}
    if not activity and leads is not None:
        try:
            lead_count = int(leads)
            activity = {
                "state": "KNOWN_POSITIVE" if lead_count > 0 else "KNOWN_ZERO",
                "total_leads": lead_count,
                "portals": [],
                "conversations": None,
                "visits": None,
                "partial_snapshot": True,
            }
        except (TypeError, ValueError):
            activity = {"state": "UNKNOWN"}
    model = dict(saved_model)
    model.update({
        "code": str(row.get("property_code") or ""),
        "property_type": prop_type,
        "commune": commune,
        "property_heading": " · ".join(part for part in (prop_type, commune) if part),
        "operation_label": "Arriendo" if is_rental else "Venta",
        "operation_raw": operation,
        "is_rental": is_rental,
        "price_label": view.get("current_price_label") or "No disponible",
        "current_price_clp_label": view.get("current_price_clp_label") or "No disponible",
        "recommended_price_label": display_recommended_price,
        "recommended_adjustment_pct": snapshot.get("recommended_adjustment_pct", row.get("recommended_adjustment_pct")),
        "recommended_price_clp_label": view.get("recommended_price_clp_label") or "No disponible",
        "display_adjustment_label": (f"-{snapshot.get('recommended_adjustment_pct', row.get('recommended_adjustment_pct'))}%" if can_authorize and snapshot.get("recommended_adjustment_pct", row.get("recommended_adjustment_pct")) is not None else ""),
        "feature_cards": list(saved_model.get("feature_cards") or []),
        "image": dict(saved_model.get("image") or {"available": False, "url": "", "source": "NONE", "count": 0}),
        "comparable": dict(saved_comparable),
        "comparable_summary_text": str(saved_model.get("comparable_summary_text") or ""),
        "diagnostic_text": str(saved_model.get("diagnostic_text") or ""),
        "single_diagnostic_text": str(saved_model.get("single_diagnostic_text") or snapshot.get("single_diagnostic_text") or ""),
        "single_document_copy": str(saved_model.get("single_document_copy") or snapshot.get("single_document_copy") or ""),
        "single_recommendation_text": recommendation_reason,
        "recommendation_title": "Ajuste de precio sugerido" if can_authorize else "Recomendación en revisión",
        "recommendation_text": recommendation_reason,
        "document": {**dict(saved_model.get("document") or {}), "visible": report_visible, "type": document_type},
        "appraisal": dict(saved_appraisal),
        "market_reference": dict(saved_market_reference),
        "activity_90d": dict(activity or {"state": "UNKNOWN"}),
        "portfolio_summary": dict(saved_model.get("portfolio_summary") or {}),
        "cta": {"primary_url": str(view.get("primary_url") or ""), "advisor_url": str(view.get("advisor_url") or ""), "report_url": str(view.get("report_url") or "") if report_visible else ""},
    })
    # Prices shown to the client come exclusively from the ledger snapshot.
    current_raw = snapshot.get("current_price", row.get("current_price"))
    recommended_raw = snapshot.get("recommended_price", row.get("recommended_price"))
    current_clp = snapshot.get("current_price_clp", row.get("current_price_clp"))
    recommended_clp = snapshot.get("recommended_price_clp", row.get("recommended_price_clp"))
    if current_raw is not None:
        model["price_label"] = _price_label(current_raw, operation)
    if recommended_raw is not None and can_authorize:
        model["recommended_price_label"] = _price_label(recommended_raw, operation)
    if current_clp is not None:
        model["current_price_clp_label"] = _clp_price_label(current_clp, operation)
    if recommended_clp is not None and can_authorize:
        model["recommended_price_clp_label"] = _clp_price_label(recommended_clp, operation)
    if not model.get("single_valuation_slots"):
        from analytics.owner_campaign_email_v2 import _single_property_valuation_slots
        model["single_valuation_slots"] = _single_property_valuation_slots(model)
    executive = {
        "name": _clean(snapshot.get("executive_name") or row.get("executive_name") or row.get("executive")) or "Tu ejecutivo PROCASA",
        "email": _clean(snapshot.get("executive_email") or row.get("executive_email")),
        "phone": "",
    }
    return render_owner_campaign_email_v2(
        [model], email=_clean(row.get("owner_email")), executives=[executive],
        base_url=base_url,
        report_date_override=_campaign_report_date(view.get("sent_at")),
    )


def _campaign_report_date(value: Any) -> str:
    if not isinstance(value, datetime):
        return ""
    from zoneinfo import ZoneInfo
    months = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
    local = _as_utc(value).astimezone(ZoneInfo("America/Santiago"))
    return f"{local.day} de {months[local.month - 1]} de {local.year}"


def _format_client_price(value: Any, operation: str, *, clp: bool = False) -> str:
    if value is None or value == "":
        return "No disponible"
    from analytics.owner_campaign_email_v2 import _clp_price_label, _price_label
    return _clp_price_label(value, operation) if clp else _price_label(value, operation)


def materialize_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Registra enlaces privados para filas de campaña preparadas")
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--source", choices=sorted(ACCESS_SOURCES), default="EMAIL")
    parser.add_argument("--execute", action="store_true", help="Persiste la referencia firmada; por defecto sólo valida")
    parser.add_argument("--output-csv", default="", help="Ruta local opcional para exportar URLs privadas")
    args = parser.parse_args(argv)
    from chatbot.storage import get_db
    base_url = (Config.CRM_BASE_URL or "https://www.procasa.cl").rstrip("/")
    result = materialize_portal_accesses(
        get_db(), campaign_id=args.campaign_id, base_url=base_url,
        source=args.source, persist=args.execute,
    )
    if args.output_csv:
        import csv
        from pathlib import Path
        output = Path(args.output_csv)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "campaign_id", "property_code", "owner_email", "private_url",
                "token_expires_at", "send_status", "document_type", "executive",
            ])
            writer.writeheader()
            writer.writerows(result["records"])
    print({"campaign_id": result["campaign_id"], "source": result["source"],
           "counts": result["counts"], "urls_exported": bool(args.output_csv)})
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(materialize_cli())
