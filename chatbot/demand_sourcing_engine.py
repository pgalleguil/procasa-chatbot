"""Shared, indexed demand-sourcing cases and background jobs.

This module stores references and evidence, not buyer conversations or phone
numbers. External portal listings remain unverified captation candidates and
are never promoted to buyer recommendations by this service.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import unicodedata
from typing import Any

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError


CASE_COLLECTION = "demand_search_cases"
JOB_COLLECTION = "demand_search_jobs"
CATALOG_COLLECTION = "demand_property_index"
CANDIDATE_COLLECTION = "demand_search_candidates"
EVENT_COLLECTION = "demand_search_events"
ALERT_COLLECTION = "demand_search_alert_outbox"
WORKER_STATE_COLLECTION = "demand_search_worker_state"
QUOTA_COLLECTION = "demand_search_source_quotas"

ACTIVE_CASE_STATES = frozenset({
    "NUEVA", "REQUIERE_DATOS", "BUSCANDO", "CANDIDATOS",
    "EN_VERIFICACION", "EN_GESTION", "ERROR",
})
CLOSED_CASE_STATES = frozenset({"RESUELTA", "CERRADA"})
JOB_RETRY_LIMIT = 5
JOB_LEASE_SECONDS = 900
JOB_REUSE_MINUTES = 45
EXTERNAL_STALE_AFTER = timedelta(days=7)
INTERNAL_STALE_AFTER = timedelta(days=2)
EXTERNAL_HUMAN_VERIFICATION_TTL = timedelta(hours=24)
_TERMINAL_MANAGEMENT = {
    "corredor", "descartado", "captado", "publicacion expirada",
    "propiedad no disponible", "telefono invalido", "no contactar",
}
_BROKER_STATES = {
    "CORREDOR_SEGURO", "CORREDOR_PROBABLE", "BROKER_CONFIRMED",
}
VISIBLE_CANDIDATE_STATES = {
    "PROCASA_VERIFICADO", "PROCASA_PENDING_PRICE", "EXTERNAL_PENDING_VERIFICATION",
    "EXTERNAL_PENDING_IDENTITY", "EXTERNAL_PENDING_PRICE", "EN_GESTION_OTRO", "STALE",
    "LOCATION_REVIEW_PENDING",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(char for char in text if not unicodedata.combining(char))


def _clean(values) -> list[str]:
    return sorted({str(value).strip() for value in (values or []) if str(value or "").strip()})


def _hash(value: Any, length: int = 32) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def _as_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool) or value is None:
            return None
        result = int(round(float(value)))
        return result if result > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _safe_field_evidence(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    allowed = {"source", "message_id", "verification", "confidence", "explicit", "verified_at"}
    result = {}
    for field, evidence in list(value.items())[:30]:
        if not isinstance(evidence, dict):
            continue
        safe = {key: evidence[key] for key in allowed if key in evidence
                and isinstance(evidence[key], (str, int, float, bool, datetime))}
        if "source" in safe:
            safe["source"] = str(safe["source"])[:80]
        if "message_id" in safe:
            safe["message_id"] = str(safe["message_id"])[:120]
        if safe:
            result[str(field)[:80]] = safe
    return result


def _redact_contact_details(value: Any) -> str:
    text = str(value or "")[:1000]
    text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[email omitido]", text, flags=re.I)
    text = re.sub(r"(?<!\d)(?:\+?56[\s.-]*)?9[\s.-]*\d{4}[\s.-]*\d{4}(?!\d)",
                  "[teléfono omitido]", text)
    return text[:500]


def ensure_indexes(db) -> dict[str, list[str]]:
    """Create only indexes on the new bounded collections."""
    created: dict[str, list[str]] = {}
    definitions = {
        CASE_COLLECTION: [
            ([ ("case_id", ASCENDING) ],
             {"unique": True, "name": "uq_demand_case_id"}),
            ([ ("lead_id", ASCENDING), ("assignment_cycle_id", ASCENDING) ],
             {"unique": True, "name": "uq_demand_case_lead_cycle"}),
            ([ ("status", ASCENDING), ("priority", DESCENDING), ("updated_at", DESCENDING) ],
             {"name": "idx_demand_case_status_priority"}),
            ([ ("search_signature", ASCENDING), ("status", ASCENDING) ],
             {"name": "idx_demand_case_signature_status"}),
            ([ ("criteria.comunas_normalizadas", ASCENDING), ("criteria.property_types", ASCENDING),
               ("status", ASCENDING) ], {"name": "idx_demand_case_inverse_match"}),
        ],
        JOB_COLLECTION: [
            ([ ("job_key", ASCENDING) ], {"unique": True, "name": "uq_demand_job_key"}),
            ([ ("status", ASCENDING), ("next_attempt_at", ASCENDING), ("created_at", ASCENDING) ],
             {"name": "idx_demand_job_claim"}),
            ([ ("lease_expires_at", ASCENDING) ], {"name": "idx_demand_job_lease"}),
            ([ ("case_ids", ASCENDING), ("created_at", DESCENDING) ],
             {"name": "idx_demand_job_cases"}),
        ],
        CATALOG_COLLECTION: [
            ([ ("operation", ASCENDING), ("commune_norm", ASCENDING), ("property_type", ASCENDING),
               ("price_clp", ASCENDING), ("operational_state", ASCENDING),
               ("source_type", ASCENDING), ("last_seen_at", DESCENDING) ],
             {"name": "idx_demand_catalog_match"}),
            ([ ("canonical_identity", ASCENDING), ("human_veto", ASCENDING) ],
             {"name": "idx_demand_catalog_identity_veto"}),
            ([ ("source_type", ASCENDING), ("listing_key", ASCENDING) ],
             {"unique": True, "name": "uq_demand_catalog_listing"}),
        ],
        CANDIDATE_COLLECTION: [
            ([ ("case_id", ASCENDING), ("listing_key", ASCENDING) ],
             {"unique": True, "name": "uq_demand_candidate_case_listing"}),
            ([ ("case_id", ASCENDING), ("status", ASCENDING), ("priority_score", DESCENDING) ],
             {"name": "idx_demand_candidate_case_status"}),
        ],
        EVENT_COLLECTION: [
            ([ ("case_id", ASCENDING), ("created_at", DESCENDING) ], {"name": "idx_demand_event_case"}),
        ],
        ALERT_COLLECTION: [
            ([ ("alert_key", ASCENDING) ], {"unique": True, "name": "uq_demand_alert_key"}),
            ([ ("state", ASCENDING), ("created_at", DESCENDING) ], {"name": "idx_demand_alert_state"}),
        ],
        WORKER_STATE_COLLECTION: [
            ([ ("updated_at", DESCENDING) ], {"name": "idx_demand_worker_state_updated"}),
        ],
        QUOTA_COLLECTION: [
            ([ ("source", ASCENDING), ("period", ASCENDING) ],
             {"unique": True, "name": "uq_demand_source_quota"}),
        ],
    }
    for collection_name, indexes in definitions.items():
        coll = db[collection_name]
        created[collection_name] = [coll.create_index(keys, **options) for keys, options in indexes]
    db["captacion_management_events"].create_index(
        [("property_id", ASCENDING), ("occurred_at", DESCENDING)],
        name="demand_source_management_property_recent",
    )
    return created


def _criteria_signature(criteria: dict) -> str:
    location = criteria.get("verified_location") or {}
    targets = [_fold(x) for x in (criteria.get("location_targets") or [])]
    value = {
        "operation": str(criteria.get("operation") or "").upper(),
        "max_price_clp": criteria.get("max_price_clp"),
        "communes": [_fold(x) for x in _clean(criteria.get("comunas"))],
        "types": _clean(criteria.get("property_types")),
        "location_targets": sorted(set(targets)),
        "verified_location": {
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "radius_m": location.get("radius_m"),
            "verification": location.get("verification"),
        } if location else None,
    }
    return _hash(value)


def _verified_location_valid(location: Any) -> bool:
    if not isinstance(location, dict) or location.get("verification") != "ADMIN_VERIFIED":
        return False
    latitude, longitude, radius = location.get("latitude"), location.get("longitude"), location.get("radius_m")
    return bool(
        not isinstance(latitude, bool) and isinstance(latitude, (int, float)) and -56 <= latitude <= -17
        and not isinstance(longitude, bool) and isinstance(longitude, (int, float)) and -76 <= longitude <= -66
        and not isinstance(radius, bool) and isinstance(radius, int) and 100 <= radius <= 20_000
        and str(location.get("source") or "").strip()
    )


def missing_criteria(criteria: dict) -> list[str]:
    missing = []
    if str(criteria.get("operation") or "").upper() not in {"VENTA", "ARRIENDO"}:
        missing.append("operation")
    if type(criteria.get("max_price_clp")) is not int or not 10_000_000 <= criteria.get("max_price_clp", 0) <= 2_000_000_000:
        missing.append("max_price_clp")
    if not _clean(criteria.get("property_types")):
        missing.append("property_types")
    if not _clean(criteria.get("comunas")):
        geo = criteria.get("verified_location") or {}
        if not _verified_location_valid(geo):
            missing.append("verified_location")
        # The current Prop360 and portal catalogues have commune indexes but
        # no verified point geometry. A confirmed pin alone is not searchable.
        missing.append("searchable_commune")
    if criteria.get("location_targets"):
        geo = criteria.get("verified_location") or {}
        if not _verified_location_valid(geo):
            missing.append("location_target_confirmation")
    return missing


def _external_status(doc: dict) -> str:
    management = doc.get("gestion") or {}
    return _fold(management.get("estado_captacion") or management.get("estado") or "SIN_GESTION")


def _canonical_identity(doc: dict) -> str:
    try:
        from .demand_sourcing import _signature
        signature = _signature(doc)
        return str(signature or "")
    except Exception:
        address = _fold(doc.get("direccion_exacta") or doc.get("address"))
        digits = re.sub(r"[^a-z0-9]", "", address)
        if len(digits) >= 8 and re.search(r"\d", digits):
            return "street:" + digits + ":" + _fold(doc.get("comuna"))
        return "listing:" + str(doc.get("source_portal") or "") + ":" + str(doc.get("listing_id") or doc.get("_id") or "")


def _operation(value: Any) -> str:
    text = _fold(value)
    if "arriendo" in text or "arrend" in text:
        return "ARRIENDO"
    if "venta" in text or not text:
        return "VENTA"
    return str(value).upper()


def _property_type(value: Any) -> str:
    text = _fold(value)
    if "depart" in text or text in {"depto", "dpto"}:
        return "DEPARTAMENTO"
    if "casa" in text:
        return "CASA"
    if "oficina" in text:
        return "OFICINA"
    if "terreno" in text or "sitio" in text:
        return "TERRENO"
    return str(value or "OTRO").upper()


def _positive_number(*values) -> int | float | None:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 < number < 1_000_000:
            return int(number) if number.is_integer() else number
    return None


def _coordinate(value: Any, lower: float, upper: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if lower <= number <= upper else None


def _property_attributes(doc: dict) -> dict:
    raw = doc.get("attributes") or doc.get("atributos") or {}
    if not isinstance(raw, dict):
        raw = {}
    values = {
        "bedrooms": _positive_number(raw.get("bedrooms"), raw.get("dormitorios"), doc.get("dormitorios")),
        "bathrooms": _positive_number(raw.get("bathrooms"), raw.get("banos"), raw.get("baños"), doc.get("banos"), doc.get("baños")),
        "parking": _positive_number(raw.get("parking"), raw.get("estacionamientos"), doc.get("estacionamientos")),
        "usable_surface_m2": _positive_number(raw.get("usable_surface_m2"), raw.get("superficie_util"), doc.get("superficie_util")),
        "total_surface_m2": _positive_number(raw.get("total_surface_m2"), raw.get("superficie_total"), doc.get("superficie_total")),
    }
    return {key: value for key, value in values.items() if value is not None}


def build_external_catalog_record(doc: dict, *, now: datetime | None = None,
                                  observed_at: datetime | None = None) -> dict | None:
    now = now or utc_now()
    listing_id = str(doc.get("listing_id") or doc.get("id") or doc.get("_id") or "").strip()
    portal = str(doc.get("source_portal") or doc.get("origen") or doc.get("source") or "unknown").strip().lower()
    if not listing_id:
        return None
    management_status = _external_status(doc)
    classification = doc.get("classification") or {}
    try:
        from .demand_sourcing import _class, _policy
        classification_state = _class(doc)
        policy = _policy(doc)
    except Exception:
        classification_state = str(classification.get("effective_state") or classification.get("final_state") or classification.get("state") or "INCIERTO").upper()
        policy = "VERIFY_OWNER"
    hard_veto = (
        policy.startswith("EXCLUDED")
        or classification_state in _BROKER_STATES
        or bool(classification.get("hard_broker_veto") is True)
        or management_status in _TERMINAL_MANAGEMENT
    )
    price_status = str(doc.get("precio_validacion") or "").upper()
    source_price_clp = _as_int(doc.get("precio_clp_normalizado") or doc.get("precio_clp") or doc.get("price_clp"))
    source_price_uf = _as_int(doc.get("precio_uf") or doc.get("price_uf"))
    conversion_source = _fold(doc.get("precio_conversion_source") or "")
    detail_currency = _fold(doc.get("detail_currency") or doc.get("precio_moneda_original") or "")
    currency = str(doc.get("currency") or doc.get("moneda_publicada")
                   or doc.get("precio_moneda_original") or
                   ("UF" if conversion_source == "calculated_from_uf" or detail_currency == "uf" else "CLP")).upper()
    price = source_price_clp
    if price_status.startswith("INVALID"):
        price = None
    uf_date = _as_datetime(doc.get("uf_fecha") or doc.get("price_normalized_at") or doc.get("precio_normalizado_at"))
    conversion_from_uf = (conversion_source == "calculated_from_uf"
                          or (currency == "UF" and detail_currency not in {"uf+clp", "clp+uf"}))
    if conversion_from_uf and (not uf_date or now - uf_date > timedelta(days=2)):
        price = None
    seen = _as_datetime(doc.get("last_seen_at") or doc.get("scraped_at") or doc.get("processed_at") or doc.get("updated_at"))
    if seen is None:
        seen = _as_datetime(observed_at)
    removed = (
        str(doc.get("scrape_stage") or "").lower() in {"ad_removed", "removed"}
        or str(doc.get("listing_status") or "").upper() in {"REMOVED", "EXPIRED", "DELETED"}
        or classification_state == "AD_REMOVED"
    )
    kind = doc.get("tipo_propiedad") or doc.get("tipo") or doc.get("property_type")
    commune = str(doc.get("comuna") or (doc.get("ubicacion") or {}).get("comuna") or "").strip()
    return {
        "listing_key": f"external:{portal}:{listing_id}",
        "source_type": "EXTERNAL",
        "source_collection": "propiedades_captacion",
        "source_document_id": doc.get("_id"),
        "source_portal": portal,
        "listing_id": listing_id,
        "property_code": None,
        "canonical_url": doc.get("canonical_url") or doc.get("url") or "",
        "canonical_identity": _canonical_identity(doc),
        "operation": _operation(doc.get("operacion") or doc.get("operation")),
        "property_type": _property_type(kind),
        "commune": commune,
        "commune_norm": _fold(commune),
        "price_clp": price,
        "source_price_clp": source_price_clp,
        "source_price_uf": source_price_uf,
        "attributes": _property_attributes(doc),
        "geo": {
            "latitude": _coordinate((doc.get("ubicacion") or {}).get("latitude") or doc.get("latitude") or doc.get("latitud"), -56, -17),
            "longitude": _coordinate((doc.get("ubicacion") or {}).get("longitude") or doc.get("longitude") or doc.get("longitud") or doc.get("lng"), -76, -66),
            "verified": False,
            "source": "portal_or_scrape_unverified",
        },
        "currency": currency,
        "published": not removed,
        "operational_state": "REMOVED" if removed else "UNVERIFIED_EXTERNAL",
        "classification_state": classification_state,
        "human_management_status": management_status,
        "human_executive_id": str((doc.get("gestion") or {}).get("ejecutivo_id") or ""),
        "human_veto": bool(hard_veto),
        "human_decision": policy,
        "price_valid": price is not None,
        "last_seen_at": seen,
        "last_price_verified_at": None,
        "last_availability_verified_at": None,
        "indexed_at": now,
        "source_updated_at": _as_datetime(doc.get("updated_at") or doc.get("processed_at")),
        "verification_state": "NEEDS_REVALIDATION",
        "owner_verified_at": _as_datetime(doc.get("owner_verified_at")),
        "commercial_authorization_at": _as_datetime(doc.get("commercial_authorization_at")),
        "normalization_source": str(doc.get("normalization_source") or doc.get("precio_validacion") or "unknown")[:80],
        "price_normalized_at": uf_date,
        "evidence_flags": {
            "human_management_present": bool(management_status not in {"sin_gestion", "nuevo", ""}),
            "classification_state": classification_state,
            "do_not_contact_or_broker_veto": bool(hard_veto),
            "price_validation": price_status or "NOT_VERIFIED",
        },
    }


def build_procasa_catalog_record(doc: dict, *, now: datetime | None = None) -> dict | None:
    now = now or utc_now()
    code = str(doc.get("codigo") or doc.get("property_code") or "").strip()
    if not code:
        return None
    operation_data = doc.get("tipo_operacion") or {}
    price_data = operation_data.get("precio_venta") or operation_data.get("precio_arriendo") or {}
    operation = "VENTA" if operation_data.get("venta") or "precio_venta" in operation_data else "ARRIENDO"
    status_data = doc.get("estado") or {}
    publication = ((doc.get("publicaciones") or {}).get("procasa") or {}).get("publicaciones") or {}
    published_entry = publication.get("V" if operation == "VENTA" else "A") or {}
    is_published = published_entry.get("publicada") is True
    active = (
        doc.get("disponible_prop360") is True
        and str(status_data.get("estado_prop360") or "").casefold() == "activa"
        and is_published
        and str(status_data.get("oficina") or "").upper() in {"PROCASA SUCRE", "INMOBILIARIA SUCRE SPA"}
    )
    currency = str(price_data.get("moneda_publicada") or "CLP").upper()
    source_price = _as_int(price_data.get("precio_clp"))
    conversion_at = _as_datetime(
        price_data.get("price_normalized_at") or price_data.get("precio_normalizado_at")
        or price_data.get("conversion_at") or price_data.get("fecha_conversion")
        or price_data.get("fecha_precio_clp")
    )
    price = source_price
    if currency == "UF" and (not conversion_at or now - conversion_at > EXTERNAL_HUMAN_VERIFICATION_TTL):
        price = None
    commune = str((doc.get("ubicacion") or {}).get("comuna") or "").strip()
    updated = _as_datetime(doc.get("updated_at") or doc.get("fecha_actualizacion"))
    return {
        "listing_key": f"procasa:{code}",
        "source_type": "PROCASA",
        "source_collection": "universo_cartera_prop360",
        "source_document_id": doc.get("_id"),
        "source_portal": "procasa",
        "listing_id": code,
        "property_code": code,
        "canonical_url": published_entry.get("url") or f"https://www.procasa.cl/{code}",
        "canonical_identity": "procasa:" + code,
        "operation": operation,
        "property_type": _property_type(operation_data.get("tipo")),
        "commune": commune,
        "commune_norm": _fold(commune),
        "price_clp": price,
        "attributes": _property_attributes(doc),
        "geo": {
            "latitude": _coordinate((doc.get("ubicacion") or {}).get("latitude") or doc.get("latitude") or doc.get("latitud"), -56, -17),
            "longitude": _coordinate((doc.get("ubicacion") or {}).get("longitude") or doc.get("longitude") or doc.get("longitud") or doc.get("lng"), -76, -66),
            "verified": False,
            "source": "prop360_unverified",
        },
        "currency": str(price_data.get("moneda_publicada") or "CLP").upper(),
        "published": bool(is_published),
        "operational_state": "ACTIVE" if active else "NOT_AVAILABLE",
        "classification_state": "PROCASA_INVENTORY",
        "human_management_status": "PROCASA",
        "human_executive_id": str(status_data.get("ejecutivo_asignado") or ""),
        "human_veto": not active,
        "human_decision": "PROCASA_INVENTORY" if active else "NOT_AVAILABLE",
        "source_price_clp": source_price,
        "source_price_uf": _as_int(price_data.get("precio_uf")),
        "price_normalized_at": conversion_at,
        "price_valid": price is not None,
        "last_seen_at": updated,
        "last_price_verified_at": (conversion_at if currency == "UF" else updated) if price else None,
        "last_availability_verified_at": updated if active else None,
        "indexed_at": now,
        "source_updated_at": updated,
        "verification_state": "PROCASA_VERIFIED" if active and price else (
            "NEEDS_PRICE_VERIFICATION" if active else "NOT_AVAILABLE"),
        "evidence_flags": {"prop360_available": doc.get("disponible_prop360") is True,
                           "procasa_published": is_published,
                           "office": str(status_data.get("oficina") or "")},
    }


def _insert_event(db, *, case_id: str, event_type: str, actor: str,
                  details: dict | None = None, now: datetime | None = None) -> None:
    details = dict(details or {})
    event_key = _hash({"case_id": case_id, "event_type": event_type,
                       "actor": actor, "details": details}, 40)
    db[EVENT_COLLECTION].update_one(
        {"_id": event_key},
        {"$setOnInsert": {"case_id": case_id, "event_type": event_type,
                           "actor": actor, "details": details,
                           "created_at": now or utc_now()}},
        upsert=True,
    )


def _create_alert(db, *, case: dict, alert_type: str,
                  listing_key: str | None = None, now: datetime | None = None) -> str:
    now = now or utc_now()
    identity = _hash({"case_id": case["case_id"], "revision": case.get("revision", 1),
                      "alert_type": alert_type, "listing_key": listing_key})
    case_id = str(case["case_id"])
    link = "/crm/captacion-demanda?case=" + case_id
    criteria = case.get("criteria") or {}
    label = "Nueva alternativa posible" if listing_key else "Comprador activo sin oferta"
    payload = {
        "title": label,
        "executive_name": case.get("buyer_executive_name") or "Ejecutivo por confirmar",
        "operation": criteria.get("operation"),
        "property_types": criteria.get("property_types") or [],
        "communes": criteria.get("comunas") or [],
        "max_price_clp": criteria.get("max_price_clp"),
        "missing_criteria": case.get("missing_criteria") or [],
        "deep_link": link,
        "candidate_reference": listing_key,
    }
    db[ALERT_COLLECTION].update_one(
        {"_id": identity},
        {"$setOnInsert": {"alert_key": identity, "alert_type": alert_type,
                           "listing_key": listing_key, "case_id": case_id,
                           "assignment_cycle_id": case.get("assignment_cycle_id"),
                           "case_revision": int(case.get("revision") or 1),
                           "recipient_role": "admin", "channel": "authenticated_admin_inbox",
                           "state": "pending", "payload": payload,
                           "created_at": now, "updated_at": now}},
        upsert=True,
    )
    return identity


def _invalidate_case_revision(db, *, case_id: str, revision: int,
                              now: datetime) -> None:
    """Hide stale matches and stop pending internal tasks after criteria change."""
    prior = list(db[CANDIDATE_COLLECTION].find(
        {"case_id": case_id, "case_revision": {"$ne": revision}},
        {"_id": 0, "candidate_id": 1, "delivered_task_id": 1},
    ).limit(500))
    for candidate in prior:
        task_id = candidate.get("delivered_task_id")
        if task_id:
            db["crm_tasks"].update_one({"_id": task_id, "status": "pending"}, {
                "$set": {"status": "cancelled", "resolution": "demand_criteria_revision_changed",
                         "resolved_at": now},
            })
    if prior:
        candidate_ids = [row["candidate_id"] for row in prior if row.get("candidate_id")]
        if candidate_ids:
            db["crm_tasks"].update_many({
                "demand_case_id": case_id, "candidate_id": {"$in": candidate_ids},
                "status": "pending",
                "type": {"$in": ["DEMAND_SOURCING_EXECUTIVE_OPPORTUNITY",
                                 "DEMAND_SOURCING_COORDINATION"]},
            }, {"$set": {"status": "cancelled", "resolution": "demand_criteria_revision_changed",
                         "resolved_at": now}})
        db[CANDIDATE_COLLECTION].update_many({"candidate_id": {"$in": candidate_ids}}, {
            "$set": {"status": "EXCLUDED", "exclusion_reason": "CRITERIA_REVISION_CHANGED",
                     "updated_at": now},
            "$unset": {"delivered_task_id": "", "delivered_to_executive_id": "",
                       "delivered_at": ""},
        })
    db[ALERT_COLLECTION].update_many(
        {"case_id": case_id, "case_revision": {"$lt": revision}, "state": "pending"},
        {"$set": {"state": "superseded", "updated_at": now}},
    )


def upsert_catalog_record(db, record: dict, *, inverse_match: bool = True,
                          now: datetime | None = None) -> dict:
    """Idempotently upsert a source listing and optionally run inverse matching."""
    now = now or utc_now()
    if not record or not record.get("listing_key"):
        raise ValueError("catalog listing identity missing")
    existing = db[CATALOG_COLLECTION].find_one({"listing_key": record["listing_key"]})
    document = {**record, "updated_at": now}
    if (record.get("source_type") == "EXTERNAL" and existing
            and existing.get("verification_state") == "HUMAN_VERIFIED_AND_AUTHORIZED"
            and record.get("published") is True and not record.get("human_veto")
            and record.get("source_price_clp") == existing.get("source_price_clp")
            and record.get("source_price_uf") == existing.get("source_price_uf")
            and record.get("currency") == existing.get("currency")
            and record.get("price_normalized_at") == existing.get("price_normalized_at")
            and record.get("canonical_identity") == existing.get("canonical_identity")
            and record.get("human_management_status") == existing.get("human_management_status")
            and record.get("human_executive_id") == existing.get("human_executive_id")):
        for key in ("price_clp", "last_price_verified_at", "last_availability_verified_at",
                    "owner_verified_at", "commercial_authorization_at", "verification_state"):
            if existing.get(key) is not None:
                document[key] = existing[key]
    if (record.get("source_type") == "PROCASA" and existing
            and existing.get("verification_state") == "HUMAN_VERIFIED_INTERNAL_STOCK"
            and record.get("operational_state") == "ACTIVE" and record.get("published") is True
            and record.get("source_price_clp") == existing.get("source_price_clp")
            and record.get("source_price_uf") == existing.get("source_price_uf")
            and record.get("currency") == existing.get("currency")
            and record.get("price_normalized_at") == existing.get("price_normalized_at")):
        for key in ("price_clp", "last_price_verified_at", "last_availability_verified_at",
                    "verification_state"):
            if existing.get(key) is not None:
                document[key] = existing[key]
    db[CATALOG_COLLECTION].update_one(
        {"listing_key": record["listing_key"]},
        {"$set": document, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    stored = db[CATALOG_COLLECTION].find_one({"listing_key": record["listing_key"]}) or document
    refresh_catalog_candidate_relations(db, stored, now=now)
    if inverse_match and not existing:
        inverse_match_property(db, stored, now=now)
    elif inverse_match:
        # Material changes can unlock/block an existing relationship. Refresh
        # only cases selected by indexed commune/type/operation filters.
        inverse_match_property(db, stored, now=now)
    return stored


def refresh_catalog_candidate_relations(db, listing: dict, *,
                                        now: datetime | None = None) -> dict:
    now = now or utc_now()
    updated = 0
    for prior_candidate in db[CANDIDATE_COLLECTION].find({"listing_key": listing.get("listing_key")}):
        case = db[CASE_COLLECTION].find_one({"case_id": prior_candidate.get("case_id")})
        if not case:
            continue
        if int(prior_candidate.get("case_revision") or 0) != int(case.get("revision") or 0):
            task_id = prior_candidate.get("delivered_task_id")
            if task_id:
                db["crm_tasks"].update_one({"_id": task_id, "status": "pending"}, {
                    "$set": {"status": "cancelled", "resolution": "demand_criteria_revision_changed",
                             "resolved_at": now},
                })
            db[CANDIDATE_COLLECTION].update_one({"_id": prior_candidate["_id"]}, {
                "$set": {"status": "EXCLUDED", "exclusion_reason": "CRITERIA_REVISION_CHANGED",
                         "updated_at": now},
                "$unset": {"delivered_task_id": "", "delivered_to_executive_id": "",
                           "delivered_at": ""},
            })
        elif _case_is_current(db, case):
            _upsert_candidate(db, case, listing, now=now)
            updated += 1
    return {"updated": updated}


def _external_management_evidence_batch(db, documents: list[dict]) -> dict[str, list[dict]]:
    """Read only indexed ledger events for this bounded batch of listings."""
    identifiers = set()
    for doc in documents:
        for value in (doc.get("_id"), doc.get("listing_id"), doc.get("id")):
            if value not in (None, ""):
                identifiers.add(str(value))
    if not identifiers:
        return {}
    try:
        rows = list(db["captacion_management_events"].find(
            {"property_id": {"$in": sorted(identifiers)}},
            {"_id": 0, "event_id": 1, "event_type": 1, "property_id": 1,
             "assignment_cycle_id": 1, "actor_user_id": 1, "credited": 1,
             "commercially_valid": 1, "result": 1, "commercial_result": 1,
             "status_snapshot": 1, "notes": 1, "occurred_at": 1},
        ).sort("occurred_at", DESCENDING).limit(min(5000, len(identifiers) * 20)))
        event_ids = [row.get("event_id") for row in rows if row.get("event_id")]
        reversed_ids = set()
        if event_ids:
            reversed_ids = {str(row.get("original_event_id")) for row in db[
                "captacion_management_events"].find(
                    {"event_type": "management_reversed", "original_event_id": {"$in": event_ids}},
                    {"_id": 0, "original_event_id": 1},
                ) if row.get("original_event_id")}
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            if row.get("event_id") and str(row["event_id"]) in reversed_ids:
                continue
            grouped.setdefault(str(row.get("property_id") or ""), []).append(row)
        return grouped
    except Exception:
        # Ledger access failures leave a listing unverified. Human validation
        # is still mandatory before any external candidate can be delivered.
        return {}


def _apply_external_human_evidence(db, doc: dict, record: dict, *,
                                   broker_result: dict | None = None,
                                   ledger_events: list[dict] | None = None) -> dict:
    """Overlay exact broker registry and captacion ledger decisions in memory.

    Raw phones, emails, names and note text are never copied to the catalogue.
    """
    if broker_result is None:
        try:
            from broker_registry import resolve_broker_identity
            broker_result = resolve_broker_identity(db, doc, include_alias=False)
        except Exception:
            broker_result = {"available": False, "matched": False}
    record["broker_registry_state"] = (
        "MATCHED" if broker_result.get("matched") else
        "CONFLICT" if broker_result.get("conflict") else
        "AVAILABLE" if broker_result.get("available") else "UNAVAILABLE"
    )
    if broker_result.get("matched"):
        record["human_veto"] = True
        record["classification_state"] = "BROKER_CONFIRMED"
        record["human_decision"] = "EXCLUDED_KNOWN_BROKER"
        record["evidence_flags"]["broker_registry_exact_match"] = True

    events = list(ledger_events or [])
    if events:
        # Credited, unreversed, human commercial decisions take precedence over
        # a newer portal scrape. Preserve only normalized state and actor ID.
        terminal = {
            "broker identified", "corredor", "descartado", "discarded",
            "captado", "captured", "publicacion expirada", "listing expired",
            "propiedad no disponible", "unavailable", "do not contact",
            "no contactar", "no broker contact", "do_not_contact",
        }
        valid = [event for event in events
                 if event.get("commercially_valid") is not False
                 and event.get("credited") is not False]
        terminal_event = next((event for event in valid if any(
            _fold(value) in terminal for value in (
                event.get("commercial_result"), event.get("result"),
                event.get("status_snapshot"),
            ) if value)), None)
        if terminal_event:
            record["human_veto"] = True
            record["human_decision"] = "EXCLUDED_HUMAN_MANAGEMENT_DECISION"
            record["human_management_status"] = str(
                terminal_event.get("status_snapshot")
                or terminal_event.get("commercial_result")
                or terminal_event.get("result") or "Decisión humana"
            )[:80]
            record["human_executive_id"] = str(terminal_event.get("actor_user_id") or "")
            record["evidence_flags"]["captacion_ledger_veto"] = True
        else:
            current_cycle = str((doc.get("gestion") or {}).get("assignment_cycle_id") or "")
            management_event = next((event for event in valid
                if event.get("event_type") == "management_confirmed"
                and (not current_cycle or not event.get("assignment_cycle_id")
                     or str(event.get("assignment_cycle_id")) == current_cycle)), None)
            if management_event and not record.get("human_executive_id"):
                record["human_executive_id"] = str(management_event.get("actor_user_id") or "")
            if management_event and record.get("human_management_status") in {"sin_gestion", "nuevo", ""}:
                record["human_management_status"] = "En gestión"
                record["human_decision"] = "EXISTING_MANAGEMENT"
                record["evidence_flags"]["captacion_ledger_management"] = True
            # DNC/commission objections written to the ledger remain private;
            # run the established text policy transiently and persist its code.
            note_values = [str(event.get("notes") or "") for event in valid if event.get("notes")]
            if note_values:
                try:
                    from .demand_sourcing import _policy
                    management = dict(doc.get("gestion") or {})
                    existing_notes = management.get("notas") or []
                    if not isinstance(existing_notes, list):
                        existing_notes = [existing_notes]
                    management["notas"] = list(existing_notes) + note_values
                    transient = {**doc, "gestion": management}
                    policy = _policy(transient)
                    if policy.startswith("EXCLUDED"):
                        record["human_veto"] = True
                        record["human_decision"] = policy
                        record["evidence_flags"]["captacion_ledger_veto"] = True
                    elif policy.startswith("ADMIN_REVIEW"):
                        record["human_decision"] = policy
                except Exception:
                    pass
    return record


def index_external_listings_batch(db, documents: list[dict], *,
                                  inverse_match: bool = True,
                                  observed_at: datetime | None = None,
                                  now: datetime | None = None) -> list[dict]:
    """Index a bounded source batch with batched exact broker lookups."""
    now = now or utc_now()
    from broker_registry import resolve_broker_identity_batch
    try:
        broker_results = resolve_broker_identity_batch(db, documents, include_alias=False)
    except Exception:
        broker_results = [{"available": False, "matched": False} for _ in documents]
    by_identifier = _external_management_evidence_batch(db, documents)
    output = []
    for doc, broker in zip(documents, broker_results):
        record = build_external_catalog_record(doc, now=now, observed_at=observed_at)
        if not record:
            continue
        event_rows = []
        for value in (doc.get("_id"), doc.get("listing_id"), doc.get("id")):
            event_rows.extend(by_identifier.get(str(value), []))
        record = _apply_external_human_evidence(
            db, doc, record, broker_result=broker, ledger_events=event_rows,
        )
        output.append(upsert_catalog_record(db, record, inverse_match=inverse_match, now=now))
    return output


def index_external_listing(db, doc: dict, *, inverse_match: bool = True,
                           observed_at: datetime | None = None,
                           now: datetime | None = None) -> dict | None:
    rows = index_external_listings_batch(db, [doc], inverse_match=inverse_match,
                                         observed_at=observed_at, now=now)
    return rows[0] if rows else None


def index_procasa_listing(db, doc: dict, *, inverse_match: bool = True,
                          now: datetime | None = None) -> dict | None:
    record = build_procasa_catalog_record(doc, now=now)
    return upsert_catalog_record(db, record, inverse_match=inverse_match, now=now) if record else None


def _case_compatible(case: dict, listing: dict) -> tuple[bool, str]:
    criteria = case.get("criteria") or {}
    if str(criteria.get("operation") or "").upper() != listing.get("operation"):
        return False, "OPERATION_MISMATCH"
    accepted = {_property_type(x) for x in (criteria.get("property_types") or [])}
    if accepted and listing.get("property_type") not in accepted:
        return False, "TYPE_MISMATCH"
    communes = {_fold(x) for x in criteria.get("comunas") or []}
    if communes and _fold(listing.get("commune")) not in communes:
        return False, "COMMUNE_MISMATCH"
    location = criteria.get("verified_location") or {}
    if criteria.get("location_targets") or location:
        if not _verified_location_valid(location):
            return False, "LOCATION_UNVERIFIED"
        geo = listing.get("geo") or {}
        latitude, longitude = geo.get("latitude"), geo.get("longitude")
        if (geo.get("verified") is not True
                or not isinstance(latitude, (int, float))
                or not isinstance(longitude, (int, float))):
            return False, "LISTING_GEO_UNVERIFIED"
        lat1, lon1 = math.radians(float(location["latitude"])), math.radians(float(location["longitude"]))
        lat2, lon2 = math.radians(float(latitude)), math.radians(float(longitude))
        dlat, dlon = lat2 - lat1, lon2 - lon1
        haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        distance_m = 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(haversine)))
        if distance_m > int(location.get("radius_m") or 0):
            return False, "OUTSIDE_VERIFIED_RADIUS"
    max_price = criteria.get("max_price_clp")
    price = listing.get("price_clp")
    if isinstance(max_price, int):
        if not isinstance(price, int) or price <= 0:
            return False, "PRICE_UNVERIFIED"
        if price > max_price:
            return False, "OVER_BUDGET"
    elif listing.get("source_type") == "PROCASA":
        return False, "BUDGET_NOT_CONFIRMED"
    if listing.get("source_type") == "PROCASA":
        if listing.get("operational_state") != "ACTIVE" or listing.get("published") is not True:
            return False, "PROCASA_NOT_AVAILABLE"
        for field in ("last_price_verified_at", "last_availability_verified_at"):
            value = _as_datetime(listing.get(field))
            if not value or utc_now() - value > INTERNAL_STALE_AFTER:
                return False, "PROCASA_STALE"
        return True, "PROCASA_VERIFICADO"
    return True, "EXTERNAL_PENDING_VERIFICATION"


def _effective_external_veto(db, listing: dict) -> bool:
    if listing.get("human_veto"):
        return True
    identity = str(listing.get("canonical_identity") or "")
    if not identity.startswith("street:"):
        return False
    return db[CATALOG_COLLECTION].find_one({
        "canonical_identity": identity,
        "human_veto": True,
    }, {"_id": 1}) is not None


def _refresh_external_catalog_record(db, listing: dict, *, now: datetime) -> dict | None:
    """Refresh an indexed portal row from its source document before approval."""
    if not listing or listing.get("source_type") != "EXTERNAL":
        return listing
    source_name = str(listing.get("source_collection") or "propiedades_captacion")
    source_id = listing.get("source_document_id")
    if source_name != "propiedades_captacion" or source_id in (None, ""):
        return listing
    try:
        source_doc = db[source_name].find_one({"_id": source_id})
    except Exception:
        return listing
    if not source_doc:
        return {**listing, "published": False, "operational_state": "REMOVED",
                "verification_state": "NOT_AVAILABLE"}
    return index_external_listing(db, source_doc, inverse_match=False, now=now)


def _candidate_id(case_id: str, listing_key: str) -> str:
    return "demand_candidate:" + _hash([case_id, listing_key])


def _upsert_candidate(db, case: dict, listing: dict, *, now: datetime | None = None) -> dict:
    now = now or utc_now()
    compatible, match_reason = _case_compatible(case, listing)
    status = match_reason
    excluded_reason = None
    excluded_codes = {str(value) for value in (case.get("excluded_property_codes") or [])}
    if (listing.get("source_type") == "PROCASA"
            and str(listing.get("property_code") or listing.get("listing_id") or "") in excluded_codes):
        compatible, status, excluded_reason = False, "EXCLUDED", "BUYER_EXCLUDED_PROPERTY"
    elif not compatible and match_reason in {
            "OPERATION_MISMATCH", "TYPE_MISMATCH", "COMMUNE_MISMATCH",
            "OVER_BUDGET", "PROCASA_NOT_AVAILABLE"}:
        status, excluded_reason = "EXCLUDED", match_reason
    if not compatible and status in {"LOCATION_UNVERIFIED", "LISTING_GEO_UNVERIFIED",
                                     "OUTSIDE_VERIFIED_RADIUS"}:
        if status == "OUTSIDE_VERIFIED_RADIUS":
            status, excluded_reason = "EXCLUDED", "OUTSIDE_VERIFIED_RADIUS"
        else:
            status = "LOCATION_REVIEW_PENDING"
    if listing.get("source_type") == "EXTERNAL":
        if _effective_external_veto(db, listing):
            compatible, status, excluded_reason = False, "EXCLUDED", "HUMAN_BROKER_OR_CONTACT_VETO"
        elif listing.get("published") is not True:
            compatible, status, excluded_reason = False, "EXCLUDED", "LISTING_REMOVED"
        elif _as_datetime(listing.get("last_seen_at")) is None or now - _as_datetime(listing.get("last_seen_at")) > EXTERNAL_STALE_AFTER:
            status = "STALE"
        elif status in {"LOCATION_REVIEW_PENDING", "EXCLUDED"}:
            pass
        elif status == "PRICE_UNVERIFIED":
            status = "EXTERNAL_PENDING_PRICE"
        elif (_fold(listing.get("human_management_status")) in {
                "en gestion", "por contactar", "sin respuesta", "contacto exitoso",
                "reunion agendada", "existing management"}
              or listing.get("human_decision") == "EXISTING_MANAGEMENT"
              or bool(listing.get("human_executive_id"))):
            status = "EN_GESTION_OTRO"
        else:
            status = "EXTERNAL_PENDING_IDENTITY"
    elif listing.get("source_type") == "PROCASA" and status in {"PRICE_UNVERIFIED", "PROCASA_STALE"}:
        status = "PROCASA_PENDING_PRICE"
    criteria = case.get("criteria") or {}
    preferred = _property_type(criteria.get("preferred_property_type"))
    score = 100 if listing.get("source_type") == "PROCASA" else 50
    if preferred and preferred == listing.get("property_type"):
        score += 20
    maximum, price = criteria.get("max_price_clp"), listing.get("price_clp")
    if isinstance(maximum, int) and isinstance(price, int) and maximum > 0:
        score += max(0, 10 - int((price / maximum) * 10))
    candidate_id = _candidate_id(case["case_id"], listing["listing_key"])
    prior_candidate = db[CANDIDATE_COLLECTION].find_one({"_id": candidate_id})
    same_revision = bool(prior_candidate and int(prior_candidate.get("case_revision") or 0)
                         == int(case.get("revision") or 0))
    record_reset_auth = bool(prior_candidate and prior_candidate.get("status") == "CAPTATION_AUTHORIZED")
    keep_authorization = bool(same_revision
        and compatible and status not in {"STALE", "EXCLUDED", "LOCATION_REVIEW_PENDING"}
        and prior_candidate.get("status") == "CAPTATION_AUTHORIZED"
        and listing.get("verification_state") == "HUMAN_VERIFIED_AND_AUTHORIZED"
        and not listing.get("human_veto")
        and prior_candidate.get("verified_price_clp") == listing.get("price_clp")
        and prior_candidate.get("source_price_clp") == listing.get("source_price_clp")
        and prior_candidate.get("source_price_uf") == listing.get("source_price_uf"))
    keep_internal_verification = bool(same_revision
        and compatible
        and prior_candidate.get("status") == "PROCASA_VERIFICADO"
        and listing.get("verification_state") == "HUMAN_VERIFIED_INTERNAL_STOCK"
        and prior_candidate.get("price_clp") == listing.get("price_clp")
        and prior_candidate.get("source_price_clp") == listing.get("source_price_clp")
        and prior_candidate.get("source_price_uf") == listing.get("source_price_uf"))
    if keep_authorization:
        status = "CAPTATION_AUTHORIZED"
        record_reset_auth = False
    elif keep_internal_verification:
        status = "PROCASA_VERIFICADO"
        record_reset_auth = False
    elif same_revision and not excluded_reason and prior_candidate.get("decision") == "DISCARD":
        compatible, status, excluded_reason = False, "EXCLUDED", "ADMIN_MARKED_NOT_SUITABLE"
    elif same_revision and not excluded_reason and prior_candidate.get("decision") == "COORDINATE":
        status = "EN_GESTION_OTRO"
    elif same_revision and compatible and prior_candidate.get("decision") == "PRIORITIZE":
        score = max(score, int(prior_candidate.get("priority_score") or score))
    record = {
        "candidate_id": candidate_id,
        "case_id": case["case_id"],
        "lead_id": case.get("lead_id"),
        "assignment_cycle_id": case.get("assignment_cycle_id"),
        "case_revision": case.get("revision", 1),
        "listing_key": listing["listing_key"],
        "source_type": listing.get("source_type"),
        "source_portal": listing.get("source_portal"),
        "listing_id": listing.get("listing_id"),
        "property_code": listing.get("property_code"),
        "canonical_url": listing.get("canonical_url"),
        "canonical_identity": listing.get("canonical_identity"),
        "operation": listing.get("operation"),
        "property_type": listing.get("property_type"),
        "commune": listing.get("commune"),
        "price_clp": listing.get("price_clp"),
        "source_price_clp": listing.get("source_price_clp"),
        "source_price_uf": listing.get("source_price_uf"),
        "last_seen_at": listing.get("last_seen_at"),
        "last_price_verified_at": listing.get("last_price_verified_at"),
        "last_availability_verified_at": listing.get("last_availability_verified_at"),
        "management_status": listing.get("human_management_status"),
        "management_executive_id": listing.get("human_executive_id"),
        "classification_state": listing.get("classification_state"),
        "human_decision": listing.get("human_decision"),
        "status": status if compatible else ("EXCLUDED" if excluded_reason else status),
        "eligible_exact": bool(compatible and listing.get("source_type") == "PROCASA"),
        "exclusion_reason": excluded_reason,
        "verification_state": listing.get("verification_state"),
        "evidence_flags": listing.get("evidence_flags") or {},
        "priority_score": score,
        "updated_at": now,
        "catalog_updated_at": listing.get("updated_at"),
    }
    if record_reset_auth:
        record.update({"decision": None, "decision_actor": None, "decision_at": None,
                       "decision_evidence": {}, "verified_price_clp": None,
                       "last_price_verified_at": None,
                       "last_availability_verified_at": None,
                       "owner_verified_at": None,
                       "commercial_authorization_at": None})
        task_id = (prior_candidate or {}).get("delivered_task_id")
        if task_id:
            db["crm_tasks"].update_one({"_id": task_id, "status": "pending"}, {
                "$set": {"status": "cancelled", "resolution": "demand_candidate_invalidated",
                         "resolved_at": now},
            })
        record.update({"delivered_task_id": None, "delivered_to_executive_id": None,
                       "delivered_at": None})
    elif prior_candidate and not same_revision:
        record.update({"decision": None, "decision_actor": None, "decision_at": None,
                       "decision_evidence": {}, "verified_price_clp": None,
                       "last_price_verified_at": None,
                       "last_availability_verified_at": None,
                       "owner_verified_at": None,
                       "commercial_authorization_at": None,
                       "delivered_task_id": None, "delivered_to_executive_id": None,
                       "delivered_at": None})
    db[CANDIDATE_COLLECTION].update_one(
        {"_id": candidate_id},
        {"$set": record, "$setOnInsert": {"created_at": now, "decision": None}},
        upsert=True,
    )
    return db[CANDIDATE_COLLECTION].find_one({"candidate_id": candidate_id}) or record


def _case_filter_for_listing(listing: dict) -> dict:
    query: dict[str, Any] = {"status": {"$in": list(ACTIVE_CASE_STATES)}}
    if listing.get("operation"):
        query["criteria.operation"] = listing["operation"]
    if listing.get("commune"):
        query["criteria.comunas_normalizadas"] = {"$in": [listing.get("commune_norm") or _fold(listing["commune"])]}
    if listing.get("property_type"):
        query["criteria.property_types"] = {"$in": [listing["property_type"]]}
    if isinstance(listing.get("price_clp"), int):
        query["criteria.max_price_clp"] = {"$gte": listing["price_clp"]}
    return query


def inverse_match_property(db, listing: dict, *, now: datetime | None = None) -> dict:
    """Link one catalog change to compatible active cases without lead scans."""
    now = now or utc_now()
    if not listing:
        return {"cases": 0, "candidates": 0, "alerts": 0}
    query = _case_filter_for_listing(listing)
    cases = db[CASE_COLLECTION].find(query, {
        "_id": 0, "case_id": 1, "lead_id": 1, "assignment_cycle_id": 1,
        "revision": 1, "criteria": 1, "buyer_executive_name": 1,
        "buyer_executive_id": 1, "status": 1, "missing_criteria": 1,
        "excluded_property_codes": 1,
    }).limit(500)
    matched = alerts = 0
    for case in cases:
        if case.get("status") in CLOSED_CASE_STATES:
            continue
        if missing_criteria(case.get("criteria") or {}):
            continue
        if not _case_is_current(db, case):
            continue
        before = db[CANDIDATE_COLLECTION].find_one({
            "case_id": case["case_id"], "listing_key": listing["listing_key"],
        }, {"_id": 1})
        candidate = _upsert_candidate(db, case, listing, now=now)
        if candidate.get("status") in {"PROCASA_VERIFICADO", "EXTERNAL_PENDING_VERIFICATION",
                                         "EXTERNAL_PENDING_IDENTITY", "EXTERNAL_PENDING_PRICE",
                                         "EN_GESTION_OTRO"}:
            matched += 1
            _create_alert(db, case=case, alert_type="NEW_COMPATIBLE_LISTING",
                          listing_key=listing["listing_key"], now=now)
            alerts += 1
            update = {"$set": {"updated_at": now, "last_candidate_at": now}}
            if not before:
                update["$inc"] = {"candidate_count": 1}
            if case.get("status") != "EN_GESTION":
                update["$set"]["status"] = "CANDIDATOS"
            db[CASE_COLLECTION].update_one({"case_id": case["case_id"]}, update)
    return {"cases": matched, "candidates": matched, "alerts": alerts}


def find_indexed_external_candidates(db, search: dict, *, limit: int = 12) -> dict:
    """Query the materialized catalog. Never scans propiedades_captacion."""
    criteria = search.get("criteria") or {}
    communes = _clean(criteria.get("comunas"))
    kinds = [_property_type(x) for x in (criteria.get("property_types") or [])]
    maximum = criteria.get("max_price_clp")
    if (search.get("status") != "ACTIVE" or not communes or not kinds
            or not isinstance(maximum, int) or maximum <= 0):
        return {"candidates": [], "excluded": {}, "scanned": 0, "catalog_ready": False}
    query = {
        "source_type": "EXTERNAL", "operation": _operation(criteria.get("operation")),
        "commune_norm": {"$in": [_fold(value) for value in communes]}, "property_type": {"$in": kinds},
        "$or": [{"price_clp": {"$gt": 0, "$lte": maximum}}, {"price_clp": None}],
    }
    rows = db[CATALOG_COLLECTION].find(query).sort([("human_veto", ASCENDING), ("price_clp", ASCENDING)]).limit(max(1, min(limit * 5, 100)))
    result, excluded, seen = [], {}, set()
    scanned = 0
    for listing in rows:
        scanned += 1
        if _effective_external_veto(db, listing):
            excluded["HUMAN_BROKER_OR_CONTACT_VETO"] = excluded.get("HUMAN_BROKER_OR_CONTACT_VETO", 0) + 1
            continue
        compatible, status = _case_compatible({"criteria": criteria}, listing)
        if not compatible and status == "PRICE_UNVERIFIED":
            status = "EXTERNAL_PENDING_PRICE"
            compatible = True  # visible for human price review; never exact-match eligible
        if not compatible:
            excluded[status] = excluded.get(status, 0) + 1
            continue
        identity = listing.get("canonical_identity") or listing["listing_key"]
        if identity in seen:
            excluded["DUPLICATE"] = excluded.get("DUPLICATE", 0) + 1
            continue
        seen.add(identity)
        candidate_status = "EXTERNAL_PENDING_IDENTITY"
        if (_fold(listing.get("human_management_status")) in {
                "en gestion", "por contactar", "sin respuesta", "contacto exitoso",
                "reunion agendada", "existing management"}
                or listing.get("human_decision") == "EXISTING_MANAGEMENT"
                or bool(listing.get("human_executive_id"))):
            candidate_status = "EN_GESTION_OTRO"
        seen_at = _as_datetime(listing.get("last_seen_at"))
        if not seen_at or utc_now() - seen_at > EXTERNAL_STALE_AFTER:
            candidate_status = "STALE"
        result.append({
            "listing_id": listing.get("listing_id"), "portal": listing.get("source_portal"),
            "url": listing.get("canonical_url"), "type": listing.get("property_type"),
            "comuna": listing.get("commune"), "price_clp_reference": listing.get("price_clp"),
            "classification": listing.get("classification_state"),
            "management_status": listing.get("human_management_status"),
            "previous_executive_id": listing.get("human_executive_id"),
            "decision": listing.get("human_decision"), "status": candidate_status,
            "verification": "RECHECK_PRICE_OWNER_AND_AVAILABILITY",
            "listing_key": listing.get("listing_key"),
        })
    result.sort(key=lambda item: (item["status"] != "EXTERNAL_PENDING_IDENTITY",
                                  item.get("price_clp_reference") or 0,
                                  item.get("portal") or "", item.get("listing_id") or ""))
    return {"candidates": result[:max(1, min(limit, 30))], "excluded": excluded,
            "scanned": scanned, "catalog_ready": True}


def _case_id(lead_id, cycle_id: str) -> str:
    return "demand_case:" + _hash([str(lead_id), str(cycle_id)])


def _case_is_current(db, case: dict) -> bool:
    lead = db["leads"].find_one({
        "_id": case.get("lead_id"),
        "lifecycle.current_assignment_cycle_id": case.get("assignment_cycle_id"),
        "lifecycle.assigned_to_user_id": case.get("buyer_executive_id"),
        "commercial_intelligence.active_search.status": "ACTIVE",
    }, {"_id": 1})
    if not lead:
        return False
    try:
        from .crm_metrics import active_assignment_cycle
        active = active_assignment_cycle(db, case.get("lead_id"))
    except Exception:
        return False
    return bool(active
                and str(active.get("assignment_cycle_id") or "") == str(case.get("assignment_cycle_id") or "")
                and str(active.get("assigned_to_user_id") or "") == str(case.get("buyer_executive_id") or "")
                and active.get("cycle_status", "active") == "active")


def ensure_demand_case(db, *, lead_id, cycle_id: str, owner_id: str,
                       search: dict, now: datetime | None = None) -> dict | None:
    """Create/update one case only for the current active buyer assignment."""
    now = now or utc_now()
    if (search.get("status") != "ACTIVE" or not search.get("followup_required")
            or search.get("opt_out")):
        return None
    lead = db["leads"].find_one({
        "_id": lead_id,
        "lifecycle.current_assignment_cycle_id": cycle_id,
        "lifecycle.assigned_to_user_id": owner_id,
        "commercial_intelligence.active_search.status": "ACTIVE",
    }, {"_id": 1, "lifecycle": 1, "ejecutivo_asignado": 1,
        "commercial_intelligence.active_search": 1})
    if not lead:
        return None
    lifecycle = lead.get("lifecycle") or {}
    try:
        from .crm_metrics import active_assignment_cycle
        active_cycle = active_assignment_cycle(db, lead_id)
    except Exception:
        active_cycle = None
    if (not active_cycle
            or str(active_cycle.get("assignment_cycle_id") or "") != str(cycle_id)
            or str(active_cycle.get("assigned_to_user_id") or "") != str(owner_id)
            or active_cycle.get("cycle_status", "active") != "active"
            or lifecycle.get("current_assignment_cycle_id") != cycle_id
            or lifecycle.get("assigned_to_user_id") != owner_id):
        return None
    criteria_source = search.get("criteria") or {}
    criteria_fields = {"operation", "max_price_clp", "comunas", "property_types",
                       "preferred_property_type", "location_targets", "verified_location",
                       "attributes", "bedrooms", "bathrooms", "parking", "surface_m2"}
    criteria = {key: value for key, value in criteria_source.items() if key in criteria_fields}
    criteria["comunas"] = _clean(criteria.get("comunas"))
    criteria["comunas_normalizadas"] = sorted({_fold(value) for value in criteria["comunas"]})
    criteria["property_types"] = _clean(criteria.get("property_types"))
    criteria["location_targets"] = [str(x).strip() for x in (criteria.get("location_targets") or []) if str(x).strip()]
    signature = _criteria_signature(criteria)
    case_id = _case_id(lead_id, cycle_id)
    prior = db[CASE_COLLECTION].find_one({"case_id": case_id})
    safe_evidence = _safe_field_evidence(search.get("field_evidence"))
    excluded_property_codes = _clean(search.get("excluded_property_codes"))
    criteria_hash = _hash({"criteria": criteria, "field_evidence": safe_evidence,
                           "excluded_property_codes": excluded_property_codes})
    revision = int((prior or {}).get("revision", 0) or 0)
    changed = not prior or prior.get("criteria_hash") != criteria_hash
    if changed:
        revision = max(1, revision + 1)
    missing = missing_criteria(criteria)
    matches = []
    try:
        from .active_search import find_matching_inventory
        matches = find_matching_inventory(db, search, limit=8)
    except Exception:
        matches = []
    if missing:
        status = "REQUIERE_DATOS"
    elif matches:
        status = "CANDIDATOS"
    else:
        status = "NUEVA"
    if prior and prior.get("status") in CLOSED_CASE_STATES and not changed:
        return prior
    case = {
        "case_id": case_id, "lead_id": lead_id, "assignment_cycle_id": cycle_id,
        "buyer_executive_id": owner_id,
        "buyer_executive_name": str(lead.get("ejecutivo_asignado") or ""),
        "status": status, "priority": (prior or {}).get("priority", 50),
        "revision": revision, "criteria": criteria,
        "criteria_hash": criteria_hash, "search_signature": signature,
        "field_evidence": safe_evidence,
        "source_channels": [str(value)[:80] for value in (search.get("source_channels") or [])[:10]
                            if isinstance(value, (str, int))],
        "excluded_property_codes": excluded_property_codes,
        "missing_criteria": missing,
        "criteria_review_required": bool(missing),
        "active_search_status": "ACTIVE",
        "internal_match_count": len(matches),
        "candidate_count": int((prior or {}).get("candidate_count", 0) or 0),
        "last_customer_at": search.get("last_customer_at"),
        "updated_at": now,
        "criteria_confirmed_at": now,
        "created_at": (prior or {}).get("created_at") or now,
    }
    db[CASE_COLLECTION].update_one(
        {"_id": case_id},
        {"$set": case, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    case = db[CASE_COLLECTION].find_one({"_id": case_id}) or case
    if changed:
        _invalidate_case_revision(db, case_id=case_id, revision=revision, now=now)
    for match in matches:
        code = str(match.get("property_code") or "")
        if not code:
            continue
        source = db["universo_cartera_prop360"].find_one({"codigo": code})
        if source:
            listing = index_procasa_listing(db, source, inverse_match=False, now=now)
            if listing:
                _upsert_candidate(db, case, listing, now=now)
    if missing or not matches:
        alert_type = "NEEDS_CRITERIA_REVIEW" if missing else "DEMAND_NEEDS_SOURCING"
        _create_alert(db, case=case, alert_type=alert_type, now=now)
    # Consult the shared materialized index immediately. This is a bounded,
    # indexed query; it never scans the raw external listing collection.
    indexed_rows = find_catalog_candidates_for_case(db, case, limit=100)
    for listing in indexed_rows:
        _upsert_candidate(db, case, listing, now=now)
    indexed_candidates = db[CANDIDATE_COLLECTION].count_documents({
        "case_id": case_id,
        "status": {"$in": list(VISIBLE_CANDIDATE_STATES)},
    })
    if indexed_candidates and not missing:
        db[CASE_COLLECTION].update_one({"case_id": case_id}, {
            "$set": {"status": "CANDIDATOS", "updated_at": now},
            "$max": {"candidate_count": indexed_candidates},
        })
        case = db[CASE_COLLECTION].find_one({"case_id": case_id}) or case
    if not missing and not matches:
        enqueue_search_job(db, case_id=case_id, requested_by="active_search", now=now)
    if changed:
        _insert_event(db, case_id=case_id, event_type="CASE_CREATED" if not prior else "CRITERIA_UPDATED",
                      actor="active_search", details={"revision": revision, "missing_criteria": missing}, now=now)
    return case


def enqueue_search_job(db, *, case_id: str, requested_by: str,
                       source: str = "crm", now: datetime | None = None,
                       force_refresh: bool = False) -> dict:
    """Queue or reuse a shared segment job; never performs provider work inline."""
    now = now or utc_now()
    case = db[CASE_COLLECTION].find_one({"case_id": case_id})
    if not case:
        raise ValueError("demand_case_not_found")
    if case.get("status") in CLOSED_CASE_STATES:
        raise ValueError("demand_case_closed")
    criteria = case.get("criteria") or {}
    if missing_criteria(criteria):
        raise ValueError("demand_criteria_incomplete")
    signature = case.get("search_signature") or _criteria_signature(criteria)
    window = int(now.timestamp() // (JOB_REUSE_MINUTES * 60))
    if force_refresh:
        # Repeated clicks in the same refresh window coalesce to one job.
        window = 100000000 + int(now.timestamp() // (15 * 60))
    job_key = _hash([signature, window], 48)
    job_id = "demand_job:" + job_key
    job = {
        "job_id": job_id, "job_key": job_key,
        "search_signature": signature,
        "lead_count": 1, "criteria_envelope": {
            "operation": criteria.get("operation"),
            "comunas": _clean(criteria.get("comunas")),
            "property_types": _clean(criteria.get("property_types")),
            "max_price_clp": criteria.get("max_price_clp"),
            "location_targets": criteria.get("location_targets") or [],
            "verified_location": criteria.get("verified_location"),
        },
        "requested_sources": ["catalog", "yapo", "toctoc"],
        "status": "queued", "priority": case.get("priority", 50),
        "attempts": 0, "max_attempts": JOB_RETRY_LIMIT,
        "lease_seconds": JOB_LEASE_SECONDS,
        "requested_by": requested_by, "created_at": now, "updated_at": now,
        "next_attempt_at": now, "cost_controls": {"max_pages_per_source": 2,
                            "max_urls_per_source": 30, "max_records_per_source": 30},
    }
    try:
        db[JOB_COLLECTION].update_one(
            {"_id": job_id},
            {"$setOnInsert": job,
             "$addToSet": {"case_ids": case_id},
             "$set": {"updated_at": now,
                       f"case_revisions.{case_id}": int(case.get("revision") or 1)}},
            upsert=True,
        )
    except DuplicateKeyError:
        db[JOB_COLLECTION].update_one({"_id": job_id}, {
            "$addToSet": {"case_ids": case_id},
            "$set": {"updated_at": now,
                     f"case_revisions.{case_id}": int(case.get("revision") or 1)},
        })
    job_doc = db[JOB_COLLECTION].find_one({"_id": job_id}) or job
    db[JOB_COLLECTION].update_one({"_id": job_id}, {
        "$set": {"lead_count": len(job_doc.get("case_ids") or [case_id])},
    })
    job_doc["lead_count"] = len(job_doc.get("case_ids") or [case_id])
    case_state = "BUSCANDO" if case.get("status") not in {"CANDIDATOS", "EN_VERIFICACION", "EN_GESTION"} else case["status"]
    if job_doc.get("status") == "failed_final":
        case_state = "ERROR"
    db[CASE_COLLECTION].update_one({"_id": case_id}, {
        "$addToSet": {"job_ids": job_id}, "$set": {"status": case_state, "latest_job_id": job_id,
                                                      "updated_at": now},
    })
    if job_doc.get("status") in {"completed", "completed_degraded"}:
        # Reuse a finished segment without leaving a new subscriber stuck in
        # BUSCANDO. This is only an indexed catalogue query, never a scraper call.
        process_catalog_job(db, job_doc, now=now)
    _insert_event(db, case_id=case_id, event_type="SEARCH_JOB_QUEUED", actor=requested_by,
                  details={"job_id": job_id, "job_key": job_key}, now=now)
    return job_doc


def find_catalog_candidates_for_case(db, case: dict, *, limit: int = 100) -> list[dict]:
    criteria = case.get("criteria") or {}
    communes = _clean(criteria.get("comunas"))
    kinds = [_property_type(x) for x in (criteria.get("property_types") or [])]
    maximum = criteria.get("max_price_clp")
    if not communes or not kinds or not isinstance(maximum, int) or maximum <= 0:
        return []
    query = {"operation": _operation(criteria.get("operation")),
             "commune_norm": {"$in": [_fold(value) for value in communes]}, "property_type": {"$in": kinds},
             "$or": [{"price_clp": {"$gt": 0, "$lte": maximum}}, {"price_clp": None}]}
    return list(db[CATALOG_COLLECTION].find(query).sort("price_clp", ASCENDING).limit(max(1, min(limit, 500))))


def process_catalog_job(db, job: dict, *, now: datetime | None = None) -> dict:
    """Evaluate indexed inventory individually for every subscribed case."""
    now = now or utc_now()
    results = {"cases": 0, "candidates": 0, "excluded": 0, "catalog_rows": 0}
    for case_id in job.get("case_ids") or []:
        case = db[CASE_COLLECTION].find_one({"case_id": case_id,
                                             "status": {"$in": list(ACTIVE_CASE_STATES)}})
        if not case:
            continue
        if missing_criteria(case.get("criteria") or {}):
            continue
        expected_revision = (job.get("case_revisions") or {}).get(case_id)
        if expected_revision is not None and int(case.get("revision") or 0) != int(expected_revision):
            continue
        # The relationship is tied to the current assignment and criteria
        # revision at delivery time; stale job subscriptions are ignored.
        if not _case_is_current(db, case):
            continue
        results["cases"] += 1
        rows = find_catalog_candidates_for_case(db, case)
        results["catalog_rows"] += len(rows)
        for listing in rows:
            before = db[CANDIDATE_COLLECTION].find_one({"case_id": case_id,
                                                        "listing_key": listing["listing_key"]}, {"_id": 1})
            candidate = _upsert_candidate(db, case, listing, now=now)
            if candidate.get("status") == "EXCLUDED":
                results["excluded"] += 1
            elif candidate.get("status") in VISIBLE_CANDIDATE_STATES:
                results["candidates"] += 1
                if not before:
                    _create_alert(db, case=case, alert_type="CANDIDATES_READY",
                                  listing_key=listing["listing_key"], now=now)
        has_candidates = db[CANDIDATE_COLLECTION].count_documents({
            "case_id": case_id, "status": {"$in": list(VISIBLE_CANDIDATE_STATES)},
        })
        case_status = "EN_GESTION" if case.get("status") == "EN_GESTION" else (
            "CANDIDATOS" if has_candidates else "NUEVA"
        )
        db[CASE_COLLECTION].update_one({"case_id": case_id}, {
            "$set": {"status": case_status,
                     "last_historical_search_at": now, "updated_at": now,
                     "last_job_id": job.get("job_id"),
                     "last_job_status": job.get("status")},
        })
    return results


def _claim_next_job(db, *, worker_id: str, now: datetime | None = None) -> dict | None:
    now = now or utc_now()
    return db[JOB_COLLECTION].find_one_and_update(
        {"$or": [
            {"status": {"$in": ["queued", "failed_retryable"]},
             "$or": [{"next_attempt_at": {"$exists": False}}, {"next_attempt_at": None}, {"next_attempt_at": {"$lte": now}}]},
            {"status": "running", "lease_expires_at": {"$lte": now}},
        ]},
        {"$set": {"status": "running", "lease_owner": worker_id,
                  "lease_expires_at": now + timedelta(seconds=JOB_LEASE_SECONDS),
                  "started_at": now, "updated_at": now}, "$inc": {"attempts": 1}},
        sort=[("priority", DESCENDING), ("created_at", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )


def retry_search_job(db, *, job_id: str, actor: str,
                     now: datetime | None = None) -> dict:
    now = now or utc_now()
    job = db[JOB_COLLECTION].find_one({"job_id": job_id})
    if not job:
        raise ValueError("demand_job_not_found")
    if job.get("status") in {"queued", "running"}:
        return job
    if job.get("status") == "failed_retryable":
        live_cases = []
        for case_id in job.get("case_ids") or []:
            case = db[CASE_COLLECTION].find_one({"case_id": case_id})
            expected = (job.get("case_revisions") or {}).get(case_id)
            if (case and case.get("status") not in CLOSED_CASE_STATES
                    and case.get("latest_job_id") == job_id
                    and (expected is None or int(case.get("revision") or 0) == int(expected))
                    and _case_is_current(db, case)):
                live_cases.append(case_id)
        if not live_cases:
            raise ValueError("demand_job_has_no_current_cases")
        result = db[JOB_COLLECTION].update_one({"job_id": job_id, "status": "failed_retryable"}, {
            "$set": {"status": "queued", "next_attempt_at": now,
                     "lease_owner": None, "lease_expires_at": None,
                     "retry_requested_by": str(actor)[:120],
                     "retry_requested_at": now, "updated_at": now},
        })
        if getattr(result, "modified_count", 0):
            for case_id in job.get("case_ids") or []:
                case = db[CASE_COLLECTION].find_one({"case_id": case_id})
                if (case and case_id in live_cases and case.get("latest_job_id") == job_id
                        and _case_is_current(db, case)):
                    db[CASE_COLLECTION].update_one({"case_id": case_id}, {
                        "$set": {"status": "BUSCANDO", "last_job_status": "queued",
                                 "updated_at": now},
                    })
                    _insert_event(db, case_id=case_id, event_type="SEARCH_JOB_RETRY_REQUESTED",
                                  actor=actor, details={"job_id": job_id}, now=now)
        return db[JOB_COLLECTION].find_one({"job_id": job_id}) or job
    quota_only = (job.get("status") == "completed_degraded"
                  and (job.get("last_result") or {}).get("sources_skipped")
                  and all(row.get("reason") == "daily_quota_reached"
                          for row in (job.get("last_result") or {}).get("sources_skipped") or []))
    if job.get("status") != "failed_final" and not quota_only:
        raise ValueError("demand_job_not_retryable")
    live_cases = []
    for case_id in job.get("case_ids") or []:
        case = db[CASE_COLLECTION].find_one({"case_id": case_id})
        expected = (job.get("case_revisions") or {}).get(case_id)
        if (case and case.get("status") not in CLOSED_CASE_STATES
                and case.get("latest_job_id") == job_id
                and (expected is None or int(case.get("revision") or 0) == int(expected))
                and _case_is_current(db, case)):
            live_cases.append(case_id)
    if not live_cases:
        raise ValueError("demand_job_has_no_current_cases")
    result = db[JOB_COLLECTION].update_one({"job_id": job_id, "status": job.get("status")}, {
        "$set": {"status": "queued", "attempts": 0, "next_attempt_at": now,
                 "lease_owner": None, "lease_expires_at": None,
                 "last_error_code": None, "retry_requested_by": str(actor)[:120],
                 "retry_requested_at": now, "updated_at": now},
        "$unset": {"finished_at": ""},
    })
    if getattr(result, "modified_count", 0):
        for case_id in job.get("case_ids") or []:
            case = db[CASE_COLLECTION].find_one({"case_id": case_id})
            if (case and case.get("status") not in CLOSED_CASE_STATES
                    and case.get("latest_job_id") == job_id and _case_is_current(db, case)):
                db[CASE_COLLECTION].update_one({"case_id": case_id}, {
                    "$set": {"status": "BUSCANDO", "last_job_status": "queued",
                             "updated_at": now},
                })
                _insert_event(db, case_id=case_id, event_type="SEARCH_JOB_RETRY_REQUESTED",
                              actor=actor, details={"job_id": job_id}, now=now)
    return db[JOB_COLLECTION].find_one({"job_id": job_id}) or job


def _finish_job(db, job: dict, *, worker_id: str, status: str,
                result: dict | None = None, error_code: str | None = None,
                now: datetime | None = None) -> dict:
    now = now or utc_now()
    attempts = int(job.get("attempts") or 1)
    if status == "failed_retryable":
        delay = min(3600, 30 * (2 ** max(0, attempts - 1)))
        next_attempt = now + timedelta(seconds=delay)
    else:
        next_attempt = None
    update = {"status": status, "updated_at": now, "finished_at": now,
              "lease_owner": None, "lease_expires_at": None,
              "last_result": dict(result or {}), "last_error_code": error_code,
              "next_attempt_at": next_attempt}
    db[JOB_COLLECTION].update_one({"job_id": job["job_id"], "lease_owner": worker_id}, {"$set": update})
    for case_id in job.get("case_ids") or []:
        case = db[CASE_COLLECTION].find_one({"case_id": case_id})
        if (not case or case.get("status") in CLOSED_CASE_STATES
                or case.get("latest_job_id") != job.get("job_id")):
            continue
        expected_revision = (job.get("case_revisions") or {}).get(case_id)
        if (expected_revision is not None
                and int(case.get("revision") or 0) != int(expected_revision)):
            continue
        if not _case_is_current(db, case):
            continue
        if status in {"completed", "completed_degraded"}:
            candidate_count = db[CANDIDATE_COLLECTION].count_documents({
                "case_id": case_id, "status": {"$in": list(VISIBLE_CANDIDATE_STATES)},
            })
            case_status = ("EN_GESTION" if case.get("status") == "EN_GESTION"
                           else "CANDIDATOS" if candidate_count else "NUEVA")
        elif status == "failed_final":
            case_status = "ERROR"
        else:
            case_status = "BUSCANDO"
        db[CASE_COLLECTION].update_one({"case_id": case_id}, {"$set": {
            "status": case_status, "last_job_status": status,
            "last_job_error_code": error_code, "updated_at": now,
        }})
    return {"job_id": job["job_id"], "status": status, "error_code": error_code,
            "result": result or {}}


def _reserve_quota(db, source: str, *, now: datetime, limit: int) -> bool:
    period = now.strftime("%Y-%m-%d")
    identity = f"{source}:{period}"
    collection = db[QUOTA_COLLECTION]
    try:
        row = collection.find_one_and_update(
            {"_id": identity, "count": {"$lt": limit}},
            {"$inc": {"count": 1}, "$set": {"updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if row:
            return True
        collection.insert_one({"_id": identity, "source": source,
                               "period": period, "count": 1,
                               "created_at": now, "updated_at": now})
        return True
    except DuplicateKeyError:
        return False


def run_one_job(db, *, worker_id: str, now: datetime | None = None,
                scraper_runner=None, external_enabled: bool = False,
                quota_per_source: int = 20) -> dict:
    """Claim and run one bounded job in a standalone worker process."""
    now = now or utc_now()
    job = _claim_next_job(db, worker_id=worker_id, now=now)
    if not job:
        return {"status": "idle"}
    try:
        result = process_catalog_job(db, job, now=now)
        skipped_sources = []
        provider_errors = []
        if external_enabled and scraper_runner:
            for source in ("yapo", "toctoc"):
                if source not in (job.get("requested_sources") or []):
                    continue
                quota_limit = max(1, min(quota_per_source, 1000))
                quota_period = now.strftime("%Y-%m-%d")
                if not _reserve_quota(db, source, now=now, limit=quota_limit):
                    quota_row = db[QUOTA_COLLECTION].find_one({"_id": f"{source}:{quota_period}"}) or {}
                    skipped_sources.append({"source": source, "reason": "daily_quota_reached",
                                            "quota": {"used": int(quota_row.get("count") or 0),
                                                      "limit": quota_limit, "period_utc": quota_period}})
                    continue
                try:
                    source_result = scraper_runner(source, job)
                    records = [row for row in source_result.get("records") or [] if isinstance(row, dict)]
                    for row in records:
                        row.setdefault("source_portal", source)
                        row.setdefault("origen", source)
                        index_external_listing(db, row, inverse_match=True,
                                               observed_at=now, now=now)
                    summary = {key: source_result.get(key) for key in (
                        "status", "processed", "filters_applied", "bounded_pages",
                        "bounded_urls", "bounded_pages_per_segment", "bounded_urls_per_segment",
                        "coverage", "cache_policy",
                    ) if key in source_result}
                    summary["indexed"] = len(records)
                    quota_row = db[QUOTA_COLLECTION].find_one({"_id": f"{source}:{quota_period}"}) or {}
                    summary["quota"] = {"used": int(quota_row.get("count") or 0),
                                         "limit": quota_limit, "period_utc": quota_period}
                    if source_result.get("failures"):
                        summary["failures"] = [
                            {key: failure.get(key) for key in ("commune", "type", "reason") if key in failure}
                            for failure in source_result["failures"] if isinstance(failure, dict)
                        ][:20]
                        provider_errors.extend(summary["failures"])
                    if str(source_result.get("status") or "").lower() in {"failed", "error"}:
                        provider_errors.append({"source": source, "reason": "provider_failed"})
                    result.setdefault("sources", {})[source] = summary
                    if not summary.get("failures") and str(source_result.get("status") or "").lower() in {"completed", "success"}:
                        summary["last_success_at"] = now
                        db[WORKER_STATE_COLLECTION].update_one({"_id": f"source_health:{source}"}, {
                            "$set": {"source": source, "last_status": "completed",
                                     "last_success_at": now, "last_job_id": job.get("job_id"),
                                     "coverage": summary.get("coverage"), "indexed": len(records),
                                     "quota": summary["quota"], "cache_policy": summary.get("cache_policy"),
                                     "updated_at": now},
                            "$setOnInsert": {"created_at": now},
                        }, upsert=True)
                except Exception as exc:
                    provider_errors.append({"source": source, "reason": type(exc).__name__})
            result["sources_skipped"] = skipped_sources
        else:
            result["sources_skipped"] = [
                {"source": source, "reason": "external_scraping_disabled"}
                for source in ("yapo", "toctoc") if source in (job.get("requested_sources") or [])
            ]
        if provider_errors:
            attempts = int(job.get("attempts") or 1)
            terminal = attempts >= int(job.get("max_attempts") or JOB_RETRY_LIMIT)
            status = "failed_final" if terminal else "failed_retryable"
            result["provider_errors"] = provider_errors[:20]
            return _finish_job(db, job, worker_id=worker_id, status=status,
                               error_code="external_source_failed", result=result, now=now)
        if skipped_sources or result.get("sources_skipped"):
            return _finish_job(db, job, worker_id=worker_id, status="completed_degraded",
                               result=result, now=now)
        return _finish_job(db, job, worker_id=worker_id, status="completed", result=result, now=now)
    except Exception as exc:
        attempts = int(job.get("attempts") or 1)
        terminal = attempts >= int(job.get("max_attempts") or JOB_RETRY_LIMIT)
        status = "failed_final" if terminal else "failed_retryable"
        return _finish_job(db, job, worker_id=worker_id, status=status,
                           error_code=type(exc).__name__,
                           result={"message": str(exc)[:240]}, now=now)


def screen_indexed_external_candidates(db, search: dict, *, limit: int = 12) -> dict:
    """Adapter used by the legacy review task; it reads only the catalog."""
    return find_indexed_external_candidates(db, search, limit=limit)


def _source_fields(source: str) -> tuple[str, dict]:
    if source == "propiedades_captacion":
        return "external", {"_id": 1, "listing_id": 1, "id": 1, "source_portal": 1,
            "origen": 1, "source": 1, "canonical_url": 1, "url": 1,
            "seller_profile_id": 1, "profile_id": 1, "seller_client_id": 1,
            "client_id": 1, "phone_normalized": 1, "phone": 1, "telefono": 1,
            "contact_phone": 1, "email": 1, "email_contact": 1,
            "contact_email": 1, "seller_domain": 1, "domain": 1,
            "website": 1, "seller_website": 1, "seller_profile_url": 1,
            "profile_url": 1, "publisher": 1, "publicador_visible": 1,
            "operacion": 1, "tipo_propiedad": 1, "tipo": 1, "property_type": 1,
            "attributes": 1, "atributos": 1, "dormitorios": 1, "banos": 1,
            "baños": 1, "estacionamientos": 1, "superficie_util": 1,
            "superficie_total": 1, "latitude": 1, "longitude": 1,
            "latitud": 1, "longitud": 1, "lng": 1,
            "ubicacion.latitude": 1, "ubicacion.longitude": 1,
            "comuna": 1, "ubicacion.comuna": 1, "precio_clp_normalizado": 1,
            "precio_clp": 1, "precio_uf": 1, "precio_validacion": 1,
            "classification": 1, "gestion.estado_captacion": 1, "gestion.estado": 1,
            "gestion.ejecutivo_id": 1, "gestion.notas": 1, "gestion.actividades": 1,
            "gestion.status_history": 1, "direccion_exacta": 1, "address": 1,
            "last_seen_at": 1, "scraped_at": 1, "processed_at": 1,
            "updated_at": 1, "scrape_stage": 1, "listing_status": 1}
    return "procasa", {"_id": 1, "codigo": 1, "tipo_operacion": 1,
        "attributes": 1, "atributos": 1, "dormitorios": 1, "banos": 1,
        "baños": 1, "estacionamientos": 1, "superficie_util": 1,
        "superficie_total": 1, "latitude": 1, "longitude": 1,
        "latitud": 1, "longitud": 1, "lng": 1,
        "ubicacion.latitude": 1, "ubicacion.longitude": 1,
        "ubicacion.comuna": 1, "estado": 1, "disponible_prop360": 1,
        "publicaciones.procasa.publicaciones": 1, "updated_at": 1,
        "fecha_actualizacion": 1}


def sync_catalog_batch(db, *, batch_size: int = 200, now: datetime | None = None) -> dict:
    """Resumable, bounded bootstrap of source catalogs.

    Live deltas are consumed by the standalone worker's Mongo change streams;
    this checkpointed scan never runs from a webhook or CRM request.
    """
    now = now or utc_now()
    batch_size = max(1, min(int(batch_size), 1000))
    totals = {"processed": 0, "indexed": 0, "sources": {}, "complete": True}
    sources = {
        "propiedades_captacion": db["propiedades_captacion"],
        "universo_cartera_prop360": db["universo_cartera_prop360"],
    }
    for collection_name, collection in sources.items():
        kind, projection = _source_fields(collection_name)
        state_id = "catalog_bootstrap:" + collection_name
        state = db[WORKER_STATE_COLLECTION].find_one({"_id": state_id}) or {}
        if state.get("complete"):
            totals["sources"][collection_name] = {"processed": 0, "complete": True}
            continue
        query = {"_id": {"$gt": state["last_id"]}} if state.get("last_id") is not None else {}
        rows = list(collection.find(query, projection).sort("_id", ASCENDING).limit(batch_size))
        if kind == "external":
            indexed = len(index_external_listings_batch(db, rows, inverse_match=False, now=now))
        else:
            indexed = 0
            for row in rows:
                record = build_procasa_catalog_record(row, now=now)
                if record:
                    upsert_catalog_record(db, record, inverse_match=False, now=now)
                    indexed += 1
        last_id = rows[-1]["_id"] if rows else state.get("last_id")
        complete = len(rows) < batch_size
        db[WORKER_STATE_COLLECTION].update_one(
            {"_id": state_id},
            {"$set": {"source_collection": collection_name, "last_id": last_id,
                      "complete": complete, "updated_at": now},
             "$setOnInsert": {"created_at": now}, "$inc": {"processed_total": len(rows)}},
            upsert=True,
        )
        totals["processed"] += len(rows)
        totals["indexed"] += indexed
        totals["complete"] = totals["complete"] and complete
        totals["sources"][collection_name] = {"processed": len(rows), "indexed": indexed,
                                               "complete": complete, "last_id": str(last_id) if last_id is not None else None}
    return totals


def list_demand_cases(db, *, status: str | None = None, executive: str | None = None,
                      commune: str | None = None, page: int = 1,
                      page_size: int = 30, min_priority: int | None = None,
                      stale_days: int | None = None) -> dict:
    query: dict[str, Any] = {}
    if status and status != "TODAS":
        query["status"] = status
    if executive:
        query["buyer_executive_id"] = executive
    if commune:
        query["criteria.comunas"] = {"$regex": re.escape(commune), "$options": "i"}
    if min_priority is not None:
        query["priority"] = {"$gte": max(0, min(int(min_priority), 100))}
    if stale_days is not None:
        age = max(1, min(int(stale_days), 365))
        query["updated_at"] = {"$lte": utc_now() - timedelta(days=age)}
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 30), 100))
    total = db[CASE_COLLECTION].count_documents(query)
    items = list(db[CASE_COLLECTION].find(query, {"_id": 0}).sort(
        [("priority", DESCENDING), ("updated_at", DESCENDING)]
    ).skip((page - 1) * page_size).limit(page_size))
    counts = {row["_id"]: row["count"] for row in db[CASE_COLLECTION].aggregate([
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ])}
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size), "counts": counts}


def get_demand_case(db, case_id: str) -> dict | None:
    case = db[CASE_COLLECTION].find_one({"case_id": case_id}, {"_id": 0})
    if not case:
        return None
    case["candidates"] = list(db[CANDIDATE_COLLECTION].find(
        {"case_id": case_id}, {"_id": 0},
    ).sort([("priority_score", DESCENDING), ("created_at", DESCENDING)]).limit(100))
    case["jobs"] = list(db[JOB_COLLECTION].find({"case_ids": case_id}, {
        "_id": 0, "job_id": 1, "status": 1, "attempts": 1, "last_result": 1,
        "lead_count": 1, "requested_sources": 1,
        "last_error_code": 1, "created_at": 1, "started_at": 1, "finished_at": 1,
        "lease_expires_at": 1, "next_attempt_at": 1,
    }).sort("created_at", DESCENDING).limit(10))
    case["events"] = list(db[EVENT_COLLECTION].find({"case_id": case_id}, {"_id": 0}).sort(
        "created_at", DESCENDING).limit(40))
    return case


def update_case_criteria(db, *, case_id: str, patch: dict, actor: str,
                         now: datetime | None = None) -> dict:
    now = now or utc_now()
    case = db[CASE_COLLECTION].find_one({"case_id": case_id})
    if not case or case.get("status") in CLOSED_CASE_STATES:
        raise ValueError("demand_case_not_found_or_closed")
    if not _case_is_current(db, case):
        raise ValueError("buyer_assignment_changed_refresh_case")
    prior = dict(case.get("criteria") or {})
    allowed = {"operation", "max_price_clp", "comunas", "property_types",
               "preferred_property_type", "location_targets", "verified_location"}
    if not patch or set(patch) - allowed:
        raise ValueError("criteria_fields_invalid")
    updated = {**prior, **patch}
    if "max_price_clp" in patch:
        if patch.get("max_price_clp") in (None, ""):
            updated["max_price_clp"] = None
        else:
            amount = _as_int(patch.get("max_price_clp"))
            if amount is None or not 10_000_000 <= amount <= 2_000_000_000:
                raise ValueError("budget_invalid")
            updated["max_price_clp"] = amount
    if "comunas" in patch:
        values = _clean(patch.get("comunas"))
        if len(values) > 10:
            raise ValueError("too_many_communes")
        updated["comunas"] = values
        updated["comunas_normalizadas"] = sorted({_fold(value) for value in values})
    elif updated.get("comunas") and not updated.get("comunas_normalizadas"):
        updated["comunas_normalizadas"] = sorted({_fold(value) for value in updated["comunas"]})
    if "property_types" in patch:
        values = _clean(patch.get("property_types"))
        if any(value not in {"CASA", "DEPARTAMENTO", "OFICINA", "TERRENO"} for value in values):
            raise ValueError("property_types_invalid")
        updated["property_types"] = values
    if "preferred_property_type" in patch:
        preferred = str(patch.get("preferred_property_type") or "").strip().upper()
        if preferred and preferred not in {"CASA", "DEPARTAMENTO", "OFICINA", "TERRENO"}:
            raise ValueError("preferred_property_type_invalid")
        if preferred and preferred not in updated.get("property_types", []):
            raise ValueError("preferred_property_type_must_be_accepted")
        updated["preferred_property_type"] = preferred or None
    if "operation" in patch:
        op = str(patch["operation"]).upper()
        if op not in {"VENTA", "ARRIENDO"}:
            raise ValueError("operation_invalid")
        updated["operation"] = op
    if "location_targets" in patch:
        targets = [str(x).strip()[:120] for x in (patch.get("location_targets") or []) if str(x).strip()]
        if len(targets) > 5:
            raise ValueError("too_many_location_targets")
        updated["location_targets"] = targets
    if "verified_location" in patch:
        geo = patch.get("verified_location") or {}
        lat, lon, radius = geo.get("latitude"), geo.get("longitude"), geo.get("radius_m")
        if (isinstance(lat, bool) or not isinstance(lat, (float, int)) or not -56 <= lat <= -17
                or isinstance(lon, bool) or not isinstance(lon, (float, int)) or not -76 <= lon <= -66
                or isinstance(radius, bool) or not isinstance(radius, int) or not 100 <= radius <= 20_000
                or not str(geo.get("source") or "").strip()):
            raise ValueError("verified_location_requires_coordinates_radius_and_source")
        updated["verified_location"] = {"latitude": float(lat), "longitude": float(lon),
            "radius_m": int(radius), "source": str(geo["source"]).strip()[:200],
            "verification": "ADMIN_VERIFIED", "verified_by": actor, "verified_at": now}
    signature = _criteria_signature(updated)
    criteria_hash = _hash({"criteria": updated, "actor": actor, "at": now.isoformat()})
    revision = int(case.get("revision", 1)) + 1
    missing = missing_criteria(updated)
    new_status = "REQUIERE_DATOS" if missing else "NUEVA"
    changed = db[CASE_COLLECTION].update_one({"case_id": case_id, "revision": case.get("revision")}, {
        "$set": {"criteria": updated, "search_signature": signature,
                 "criteria_hash": criteria_hash, "revision": revision,
                 "missing_criteria": missing, "criteria_review_required": bool(missing),
                 "status": new_status, "updated_at": now,
                 "criteria_overrides": (case.get("criteria_overrides") or []) + [{
                     "actor": actor, "at": now, "changed_fields": sorted(patch.keys()),
                     "prior_hash": _hash(prior), "new_hash": _hash(updated),
                 }]},
    })
    if getattr(changed, "matched_count", 0) != 1:
        raise ValueError("demand_criteria_revision_conflict")
    _invalidate_case_revision(db, case_id=case_id, revision=revision, now=now)
    _insert_event(db, case_id=case_id, event_type="CRITERIA_UPDATED_BY_ADMIN", actor=actor,
                  details={"revision": revision, "changed_fields": sorted(patch.keys()),
                           "missing_criteria": missing}, now=now)
    refreshed = db[CASE_COLLECTION].find_one({"case_id": case_id})
    if not missing:
        enqueue_search_job(db, case_id=case_id, requested_by=actor, now=now)
    return refreshed


def review_candidate(db, *, candidate_id: str, action: str, actor: str,
                     evidence: dict | None = None,
                     now: datetime | None = None) -> dict:
    now = now or utc_now()
    evidence = dict(evidence or {})
    candidate = db[CANDIDATE_COLLECTION].find_one({"candidate_id": candidate_id})
    if not candidate:
        raise ValueError("candidate_not_found")
    case = db[CASE_COLLECTION].find_one({"case_id": candidate.get("case_id")})
    if not case or int(candidate.get("case_revision") or 0) != int(case.get("revision") or 0):
        raise ValueError("candidate_criteria_revision_changed_refresh_case")
    if case.get("status") in CLOSED_CASE_STATES:
        raise ValueError("demand_case_closed")
    if not _case_is_current(db, case):
        raise ValueError("buyer_assignment_changed_refresh_case")
    allowed = {"PRIORITIZE", "DISCARD", "COORDINATE", "VERIFY_AUTHORIZATION",
               "VERIFY_INTERNAL_STOCK"}
    if action not in allowed:
        raise ValueError("candidate_action_invalid")
    if action == "VERIFY_AUTHORIZATION":
        required = ("owner_identity_confirmed", "availability_confirmed",
                    "price_confirmed", "commercial_authorization_confirmed")
        if candidate.get("source_type") != "EXTERNAL" or any(evidence.get(key) is not True for key in required):
            raise ValueError("external_candidate_requires_owner_availability_price_and_authorization")
        if not _redact_contact_details(evidence.get("verification_note")).replace("[email omitido]", "").replace("[teléfono omitido]", "").strip():
            raise ValueError("verification_evidence_required")
        if candidate.get("exclusion_reason") or candidate.get("status") == "EXCLUDED":
            raise ValueError("excluded_candidate_cannot_be_authorized")
        if (candidate.get("management_executive_id")
                or _fold(candidate.get("management_status")) not in {"sin_gestion", "nuevo", ""}):
            raise ValueError("listing_has_prior_management_requires_coordination")
        listing = db[CATALOG_COLLECTION].find_one({"listing_key": candidate.get("listing_key")})
        listing = _refresh_external_catalog_record(db, listing, now=now)
        if not listing or _effective_external_veto(db, listing):
            raise ValueError("listing_is_human_vetoed_refresh_case")
        candidate = db[CANDIDATE_COLLECTION].find_one({"candidate_id": candidate_id}) or candidate
        if candidate.get("status") in {"EXCLUDED", "EN_GESTION_OTRO"}:
            raise ValueError("listing_has_current_human_veto_or_management")
        if (candidate.get("management_executive_id")
                or _fold(listing.get("human_management_status")) not in {"sin_gestion", "nuevo", ""}):
            raise ValueError("listing_has_prior_management_requires_coordination")
        if (listing.get("published") is not True
                or not _as_datetime(listing.get("last_seen_at"))
                or now - _as_datetime(listing.get("last_seen_at")) > EXTERNAL_STALE_AFTER):
            raise ValueError("external_listing_needs_fresh_availability")
        compatible, match_reason = _case_compatible(case, listing)
        if not compatible and match_reason != "PRICE_UNVERIFIED":
            raise ValueError("external_listing_no_longer_matches_buyer_criteria")
        confirmed_price = _as_int(evidence.get("verified_price_clp"))
        maximum = (case.get("criteria") or {}).get("max_price_clp")
        if confirmed_price is None or (isinstance(maximum, int) and confirmed_price > maximum):
            raise ValueError("verified_price_must_be_within_buyer_budget")
        result = "CAPTATION_AUTHORIZED"
    elif action == "VERIFY_INTERNAL_STOCK":
        if (candidate.get("source_type") != "PROCASA"
                or evidence.get("availability_confirmed") is not True
                or evidence.get("price_confirmed") is not True):
            raise ValueError("internal_stock_requires_price_and_availability_confirmation")
        if not _redact_contact_details(evidence.get("verification_note")).replace("[email omitido]", "").replace("[teléfono omitido]", "").strip():
            raise ValueError("verification_evidence_required")
        listing = db[CATALOG_COLLECTION].find_one({"listing_key": candidate.get("listing_key")})
        if not listing:
            raise ValueError("procasa_listing_not_in_catalog")
        source_id = listing.get("source_document_id")
        source = db["universo_cartera_prop360"].find_one({"_id": source_id}) if source_id is not None else None
        if not source:
            source = db["universo_cartera_prop360"].find_one({
                "codigo": candidate.get("property_code") or candidate.get("listing_id"),
            })
        if not source:
            raise ValueError("procasa_source_unavailable_refresh_catalog")
        listing = index_procasa_listing(db, source, inverse_match=False, now=now)
        if not listing or listing.get("operational_state") != "ACTIVE" or listing.get("published") is not True:
            raise ValueError("procasa_stock_no_longer_available")
        confirmed_price = _as_int(evidence.get("verified_price_clp"))
        maximum = (case.get("criteria") or {}).get("max_price_clp")
        if confirmed_price is None or not isinstance(maximum, int) or confirmed_price > maximum:
            raise ValueError("verified_price_must_be_within_buyer_budget")
        compatible, reason = _case_compatible(case, {**listing, "price_clp": confirmed_price})
        if not compatible and reason != "PROCASA_STALE":
            raise ValueError("procasa_listing_no_longer_matches_buyer_criteria")
        db[CATALOG_COLLECTION].update_one({"listing_key": listing["listing_key"]}, {
            "$set": {"price_clp": confirmed_price, "price_valid": True,
                     "verified_price_clp": confirmed_price,
                     "last_price_verified_at": now,
                     "last_availability_verified_at": now,
                     "verification_state": "HUMAN_VERIFIED_INTERNAL_STOCK",
                     "updated_at": now},
        })
        result = "PROCASA_VERIFICADO"
    elif action == "DISCARD":
        result = "EXCLUDED"
    elif action == "COORDINATE":
        result = "EN_GESTION_OTRO"
    elif action == "PRIORITIZE":
        result = candidate.get("status")
    else:
        result = candidate.get("status")
    safe_evidence = {}
    if action in {"VERIFY_AUTHORIZATION", "VERIFY_INTERNAL_STOCK"}:
        for key in ("owner_identity_confirmed", "availability_confirmed", "price_confirmed",
                    "commercial_authorization_confirmed"):
            if key in evidence:
                safe_evidence[key] = evidence[key] is True
        safe_evidence["verified_price_clp"] = confirmed_price
        safe_evidence["verification_note"] = _redact_contact_details(evidence.get("verification_note"))
    update = {"status": result, "decision": action,
              "decision_actor": actor, "decision_at": now,
              "decision_evidence": safe_evidence, "updated_at": now}
    if action == "DISCARD":
        update["exclusion_reason"] = "ADMIN_MARKED_NOT_SUITABLE"
    if action == "VERIFY_AUTHORIZATION":
        update.update({"price_clp": confirmed_price, "verified_price_clp": confirmed_price,
                       "last_price_verified_at": now,
                       "last_availability_verified_at": now,
                       "owner_verified_at": now,
                       "commercial_authorization_at": now,
                       "verification_state": "HUMAN_VERIFIED_AND_AUTHORIZED"})
        db[CATALOG_COLLECTION].update_one({"listing_key": candidate.get("listing_key")}, {
            "$set": {"price_clp": confirmed_price, "verified_price_clp": confirmed_price,
                     "price_valid": True,
                     "last_price_verified_at": now,
                     "last_availability_verified_at": now,
                     "owner_verified_at": now,
                     "commercial_authorization_at": now,
                     "verification_state": "HUMAN_VERIFIED_AND_AUTHORIZED",
                     "indexed_at": now, "updated_at": now},
        })
    elif action == "VERIFY_INTERNAL_STOCK":
        update.update({"price_clp": confirmed_price, "verified_price_clp": confirmed_price,
                       "last_price_verified_at": now, "last_availability_verified_at": now,
                       "verification_state": "HUMAN_VERIFIED_INTERNAL_STOCK"})
    elif action == "PRIORITIZE":
        update["priority_score"] = int(candidate.get("priority_score") or 0) + 25
    db[CANDIDATE_COLLECTION].update_one({"candidate_id": candidate_id}, {
        "$set": update,
    })
    coordination_task_id = None
    if action == "COORDINATE":
        coordination_task_id = "demand_coordination:" + _hash([candidate_id, case.get("assignment_cycle_id")])
        db["crm_tasks"].update_one({"_id": coordination_task_id}, {"$setOnInsert": {
            "_id": coordination_task_id, "task_id": coordination_task_id,
            "type": "DEMAND_SOURCING_COORDINATION", "status": "pending",
            "lead_id": case.get("lead_id"),
            "assignment_cycle_id": case.get("assignment_cycle_id"),
            "assigned_to_user_id": case.get("buyer_executive_id"),
            "agent": case.get("buyer_executive_name"),
            "demand_case_id": case.get("case_id"), "candidate_id": candidate_id,
            "listing_key": candidate.get("listing_key"),
            "management_executive_id": candidate.get("management_executive_id"),
            "contact_approved": False, "external_outbound_enabled": False,
            "created_by": actor, "created_at": now,
            "note": "Coordinar internamente la oportunidad con su responsable actual; no cambiar titularidad ni contactar por sistema.",
        }}, upsert=True)
    _insert_event(db, case_id=candidate["case_id"], event_type="CANDIDATE_" + action,
                  actor=actor, details={"candidate_id": candidate_id,
                                       "listing_key": candidate["listing_key"],
                                       "status": result}, now=now)
    refreshed = db[CANDIDATE_COLLECTION].find_one({"candidate_id": candidate_id}) or candidate
    if coordination_task_id:
        refreshed["coordination_task_id"] = coordination_task_id
    return refreshed


def deliver_candidate_to_executive(db, *, candidate_id: str, actor: str,
                                   now: datetime | None = None) -> dict:
    """Create an internal CRM task after rechecking the buyer's active cycle."""
    now = now or utc_now()
    candidate = db[CANDIDATE_COLLECTION].find_one({"candidate_id": candidate_id})
    if not candidate:
        raise ValueError("candidate_not_found")
    case = db[CASE_COLLECTION].find_one({"case_id": candidate.get("case_id")})
    if not case:
        raise ValueError("demand_case_not_found")
    if case.get("status") in CLOSED_CASE_STATES:
        raise ValueError("demand_case_closed")
    if not _case_is_current(db, case):
        raise ValueError("buyer_assignment_changed_refresh_case")
    if int(candidate.get("case_revision") or 0) != int(case.get("revision") or 0):
        raise ValueError("candidate_criteria_revision_changed_refresh_case")
    if candidate.get("source_type") == "EXTERNAL":
        if candidate.get("status") != "CAPTATION_AUTHORIZED":
            raise ValueError("external_candidate_not_authorized")
        if candidate.get("management_executive_id"):
            raise ValueError("listing_already_managed_requires_coordination")
        listing = db[CATALOG_COLLECTION].find_one({"listing_key": candidate.get("listing_key")})
        listing = _refresh_external_catalog_record(db, listing, now=now)
        if (not listing or listing.get("published") is not True
                or _effective_external_veto(db, listing)):
            raise ValueError("listing_is_removed_or_human_vetoed_refresh_case")
        evidence = candidate.get("decision_evidence") or {}
        verified_at = _as_datetime(candidate.get("last_availability_verified_at"))
        price_verified_at = _as_datetime(candidate.get("last_price_verified_at"))
        if (not evidence.get("owner_identity_confirmed")
                or not evidence.get("availability_confirmed")
                or not evidence.get("price_confirmed")
                or not evidence.get("commercial_authorization_confirmed")
                or not verified_at or now - verified_at > EXTERNAL_HUMAN_VERIFICATION_TTL
                or not price_verified_at or now - price_verified_at > EXTERNAL_HUMAN_VERIFICATION_TTL
                or not _as_datetime(listing.get("owner_verified_at"))
                or not _as_datetime(listing.get("commercial_authorization_at"))):
            raise ValueError("fresh_human_verification_required")
        compatible, match_reason = _case_compatible(
            case, {**listing, "price_clp": candidate.get("verified_price_clp")},
        )
        if not compatible:
            raise ValueError("candidate_no_longer_matches_buyer_criteria")
        if candidate.get("verified_price_clp") != listing.get("price_clp"):
            raise ValueError("external_price_changed_after_human_verification")
        maximum = (case.get("criteria") or {}).get("max_price_clp")
        if isinstance(maximum, int) and candidate.get("price_clp", 0) > maximum:
            raise ValueError("candidate_now_exceeds_buyer_budget")
    elif candidate.get("source_type") == "PROCASA":
        listing = db[CATALOG_COLLECTION].find_one({"listing_key": candidate.get("listing_key")})
        if not listing:
            source = db["universo_cartera_prop360"].find_one({
                "codigo": candidate.get("property_code") or candidate.get("listing_id"),
            })
            if source:
                listing = index_procasa_listing(db, source, inverse_match=False, now=now)
        if (not listing or listing.get("operational_state") != "ACTIVE"
                or listing.get("published") is not True):
            raise ValueError("procasa_inventory_no_longer_available")
        if candidate.get("status") != "PROCASA_VERIFICADO":
            raise ValueError("procasa_candidate_not_verified")
        compatible, reason = _case_compatible(case, listing)
        if not compatible:
            raise ValueError("procasa_candidate_no_longer_matches:" + reason.lower())
        for field in ("last_price_verified_at", "last_availability_verified_at"):
            timestamp = _as_datetime(listing.get(field))
            if not timestamp or now - timestamp > INTERNAL_STALE_AFTER:
                raise ValueError("procasa_candidate_requires_fresh_inventory_check")
    else:
        raise ValueError("candidate_source_not_deliverable")
    lead = db["leads"].find_one({
        "_id": case.get("lead_id"),
        "lifecycle.current_assignment_cycle_id": case.get("assignment_cycle_id"),
        "lifecycle.assigned_to_user_id": case.get("buyer_executive_id"),
        "commercial_intelligence.active_search.status": "ACTIVE",
    }, {"_id": 1, "lifecycle": 1})
    if not lead:
        raise ValueError("buyer_assignment_changed_refresh_case")
    from .crm_metrics import active_assignment_cycle
    active = active_assignment_cycle(db, case.get("lead_id"))
    if (not active
            or str(active.get("assignment_cycle_id") or "") != str(case.get("assignment_cycle_id"))
            or str(active.get("assigned_to_user_id") or "") != str(case.get("buyer_executive_id"))):
        raise ValueError("buyer_assignment_changed_refresh_case")
    task_id = "demand_opportunity:" + _hash([candidate_id, case.get("assignment_cycle_id")])
    task = {
        "_id": task_id, "task_id": task_id,
        "type": "DEMAND_SOURCING_EXECUTIVE_OPPORTUNITY",
        "status": "pending", "lead_id": case.get("lead_id"),
        "assignment_cycle_id": case.get("assignment_cycle_id"),
        "assigned_to_user_id": case.get("buyer_executive_id"),
        "agent": case.get("buyer_executive_name"),
        "demand_case_id": case.get("case_id"),
        "candidate_id": candidate_id,
        "listing_key": candidate.get("listing_key"),
        "source_type": candidate.get("source_type"),
        "property_code": candidate.get("property_code"),
        "listing_id": candidate.get("listing_id"),
        "url": candidate.get("canonical_url"),
        "price_clp_reference": candidate.get("price_clp"),
        "verification_state": candidate.get("status"),
        "management_executive_id": candidate.get("management_executive_id"),
        "contact_approved": False, "external_outbound_enabled": False,
        "created_by": actor, "created_at": now,
        "note": "Oportunidad de captación aprobada para revisión del ejecutivo del comprador. No contactar por sistema; verificar acuerdo y gestión humana.",
    }
    db["crm_tasks"].update_one({"_id": task_id}, {"$setOnInsert": task}, upsert=True)
    db[CANDIDATE_COLLECTION].update_one({"candidate_id": candidate_id}, {
        "$set": {"delivered_task_id": task_id, "delivered_to_executive_id": case.get("buyer_executive_id"),
                 "delivered_at": now, "updated_at": now},
    })
    db[CASE_COLLECTION].update_one({"case_id": case["case_id"]}, {
        "$set": {"status": "EN_GESTION", "updated_at": now},
    })
    _insert_event(db, case_id=case["case_id"], event_type="CANDIDATE_DELIVERED_TO_EXECUTIVE",
                  actor=actor, details={"candidate_id": candidate_id, "task_id": task_id,
                                       "executive_id": case.get("buyer_executive_id")}, now=now)
    return {"task_id": task_id, "candidate_id": candidate_id,
            "buyer_executive_id": case.get("buyer_executive_id"), "status": "pending"}


def record_case_tracking(db, *, case_id: str, stage: str, actor: str,
                         note: str = "", candidate_id: str | None = None,
                         now: datetime | None = None) -> dict:
    now = now or utc_now()
    allowed = {"CONTACT_OWNER_HUMAN", "OWNER_VERIFIED_ELIGIBLE", "PRICE_COMMISSION_OBJECTION",
               "NO_ANSWER", "NOT_AVAILABLE", "CAPTATION_AGREEMENT", "OFFER_APPROVED",
               "VISIT", "CLOSED_WON", "CLOSED_LOST", "REOPEN"}
    if stage not in allowed:
        raise ValueError("tracking_stage_invalid")
    case = db[CASE_COLLECTION].find_one({"case_id": case_id})
    if not case:
        raise ValueError("demand_case_not_found")
    if not _case_is_current(db, case):
        raise ValueError("buyer_assignment_changed_refresh_case")
    if candidate_id:
        candidate = db[CANDIDATE_COLLECTION].find_one({"candidate_id": candidate_id, "case_id": case_id})
        if not candidate:
            raise ValueError("candidate_not_found")
        db[CANDIDATE_COLLECTION].update_one({"candidate_id": candidate_id}, {
            "$set": {"tracking_stage": stage, "tracking_updated_at": now},
        })
    status = ("RESUELTA" if stage in {"CLOSED_WON", "CLOSED_LOST"}
              else ("REQUIERE_DATOS" if missing_criteria(case.get("criteria") or {}) else "NUEVA")
              if stage == "REOPEN" else "EN_GESTION")
    db[CASE_COLLECTION].update_one({"case_id": case_id}, {
        "$set": {"status": status, "last_tracking_stage": stage,
                 "updated_at": now},
    })
    if status == "RESUELTA":
        db["crm_tasks"].update_many({
            "demand_case_id": case_id, "status": "pending",
            "type": {"$in": ["DEMAND_SOURCING_EXECUTIVE_OPPORTUNITY",
                             "DEMAND_SOURCING_COORDINATION"]},
        }, {"$set": {"status": "cancelled", "resolution": "demand_case_closed",
                     "resolved_at": now}})
    elif stage == "REOPEN" and not missing_criteria(case.get("criteria") or {}):
        enqueue_search_job(db, case_id=case_id, requested_by=actor, now=now)
    _insert_event(db, case_id=case_id, event_type="TRACKING_" + stage, actor=actor,
                  details={"candidate_id": candidate_id,
                           "note": _redact_contact_details(note).strip()}, now=now)
    return db[CASE_COLLECTION].find_one({"case_id": case_id})


def deliver_pending_admin_alerts(db, *, limit: int = 30,
                                 now: datetime | None = None) -> list[dict]:
    """Read pending in-app alerts; a browser ack marks successful delivery."""
    alerts = list(db[ALERT_COLLECTION].find({"state": "pending"}, {"_id": 0}).sort(
        "created_at", ASCENDING).limit(max(1, min(limit, 100))))
    return alerts


def ack_admin_alerts(db, *, alert_keys: list[str], actor: str,
                     now: datetime | None = None) -> dict:
    """Mark only alerts actually rendered by the authenticated admin inbox."""
    now = now or utc_now()
    keys = _clean(alert_keys)[:100]
    result = db[ALERT_COLLECTION].update_many(
        {"alert_key": {"$in": keys}, "state": "pending", "recipient_role": "admin"},
        {"$set": {"state": "sent", "delivery_channel": "authenticated_admin_inbox",
                  "delivered_at": now, "delivered_to": str(actor)[:120], "updated_at": now}},
    ) if keys else None
    return {"sent": int(getattr(result, "modified_count", 0) or 0), "requested": len(keys)}


def catalog_health(db) -> dict:
    import os
    external = db[CATALOG_COLLECTION].count_documents({"source_type": "EXTERNAL"})
    procasa = db[CATALOG_COLLECTION].count_documents({"source_type": "PROCASA"})
    checkpoints = list(db[WORKER_STATE_COLLECTION].find({"_id": {"$regex": "^catalog_bootstrap:"}}, {"_id": 1, "complete": 1, "processed_total": 1, "updated_at": 1}))
    heartbeat = db[WORKER_STATE_COLLECTION].find_one({"_id": "worker_heartbeat"}) or {}
    heartbeat_at = _as_datetime(heartbeat.get("updated_at"))
    source_health = {row.get("source"): row for row in db[WORKER_STATE_COLLECTION].find(
        {"_id": {"$regex": "^source_health:"}},
        {"_id": 0, "source": 1, "last_status": 1, "last_success_at": 1,
         "last_job_id": 1, "coverage": 1, "indexed": 1, "quota": 1,
         "cache_policy": 1, "updated_at": 1},
    ) if row.get("source")}
    return {"external_indexed": external, "procasa_indexed": procasa,
            "bootstrap": checkpoints,
            "bootstrap_complete": len(checkpoints) == 2 and all(row.get("complete") is True for row in checkpoints),
            "last_worker_heartbeat": heartbeat.get("updated_at"),
            "worker_running": bool(heartbeat_at and utc_now() - heartbeat_at < timedelta(minutes=3)),
            "demand_sourcing_enabled": os.getenv("PROCASA_DEMAND_SOURCING_ENABLED", "0").strip() == "1",
            "external_scraping_enabled": heartbeat.get("external_scraping_enabled") is True,
            "change_streams_enabled": heartbeat.get("change_streams_enabled") is True,
            "source_health": source_health,
            "last_job_status": (db[JOB_COLLECTION].find_one({}, {"_id": 0, "status": 1, "finished_at": 1, "last_error_code": 1}, sort=[("finished_at", DESCENDING)]) or {})}
