"""Signed, campaign-snapshot-backed property portal helpers."""

from __future__ import annotations

import argparse
import hashlib
import hmac
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


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _safe_code(value: Any) -> str:
    code = _clean(value)
    return code if code.isdigit() and len(code) <= 12 else ""


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


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
        if persist:
            result = ledger.update_one(
                {"_id": row["_id"], "campaign_id": campaign_id, "property_code": code,
                 "owner_email": row.get("owner_email"), "send_status": status},
                {"$set": {"portal_access": access}},
            )
            if result.modified_count != 1 and not old_active:
                counts["created"] -= 1
                counts["blocked"] += 1
        url = build_portal_url(base_url, code, token, source=source)
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
    return build_portal_url(base_url, str(row["property_code"]), token, source=source)


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
            or _as_utc(expiry) <= datetime.now(timezone.utc)
            or int(claims.get("exp", 0)) != int(_as_utc(expiry).timestamp())
            or not hmac.compare_digest(digest, str(access.get("token_hash") or ""))):
        return None
    return row, claims


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
    identity = _master_identity(db, code)
    operation = _clean(row.get("operation_resolved") or row.get("operation")).upper()
    status = _clean(row.get("send_status")).upper()
    document_type = _clean(row.get("document_type")).upper() or "NONE"
    stale = status in STALE_STATUSES
    excluded = status in EXCLUDED_STATUSES or _excluded(row)
    already_authorized = _clean(row.get("authorization_status")).upper() == "PRICE_AUTHORIZED"

    def action_url(action: str, document: str | None = None) -> str:
        token = issue_live_token(
            campaign_id=campaign_id, property_code=code, action=action,
            recipient=recipient, document_type=document, expires_at=expiry,
            source=source,
        )
        if action == "ver_informe":
            return f"{base_url.rstrip('/')}/campana/informe?{urlencode({'token': token})}"
        query = urlencode({
            "email": recipient, "accion": action, "codigos": code,
            "campana": campaign_id, "mode": "owner_campaign", "token": token,
        })
        return f"{base_url.rstrip('/')}/campana/respuesta?{query}"

    report_url = action_url("ver_informe", document_type) if document_type in REPORT_TYPES else ""
    current = row.get("current_price")
    recommended = row.get("recommended_price")
    current_clp = row.get("current_price_clp")
    recommended_clp = row.get("recommended_price_clp")
    if not report_url:
        report_label = ""
    else:
        report_label = "VER / DESCARGAR INFORME"
    recommendation_reason = _clean(row.get("recommendation_reason"))
    if not recommendation_reason:
        recommendation_reason = "Esta recomendación se preparó con la actividad comercial y las referencias disponibles al momento de la campaña."
    safe_mode = stale or excluded
    advisor_url = action_url("contactar_ejecutivo")
    can_authorize = not safe_mode and not already_authorized and current is not None and recommended is not None
    return {
        "logo_url": f"{base_url.rstrip('/')}/static/logo.png",
        "property_code": code,
        "operation": operation.title() if operation else "Operación no disponible",
        "property_type": _clean(row.get("property_type")) or identity["property_type"],
        "commune": _clean(row.get("commune")) or identity["commune"],
        "current_price": current,
        "current_price_label": _format_client_price(current, operation),
        "current_price_clp_label": _format_client_price(current_clp, operation, clp=True),
        "recommended_adjustment_pct": row.get("recommended_adjustment_pct"),
        "recommended_price": recommended,
        "recommended_price_label": _format_client_price(recommended, operation),
        "recommended_price_clp_label": _format_client_price(recommended_clp, operation, clp=True),
        "leads_90d": row.get("leads_90d"),
        "comparable_mode": _clean(row.get("comparable_mode") or row.get("comparable_status")),
        "comparable_count": row.get("comparable_count"),
        "recommendation_reason": recommendation_reason,
        "executive_name": _clean(row.get("executive_name") or row.get("executive")) or "Tu ejecutivo PROCASA",
        "document_available": document_type in REPORT_TYPES,
        "document_label": report_label,
        "report_url": report_url,
        "advisor_url": advisor_url,
        "primary_url": action_url("aceptar_rebaja") if can_authorize else "",
        "safe_mode": safe_mode,
        "already_authorized": already_authorized,
        "source": source,
        "document_type": document_type,
    }


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
