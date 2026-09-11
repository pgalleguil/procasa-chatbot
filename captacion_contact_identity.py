"""Auditable human feedback and contact identity reputation for Captación.

The collection is independent from listing classification. A human result
creates evidence; it does not rewrite old listings or past management.
Assignment consults this evidence for future/pending work.
"""
from __future__ import annotations

import re
import logging
import threading
import unicodedata
from datetime import datetime, timezone
from typing import Any

from config import Config
from chatbot.phone_utils import normalize_phone_strict


COLLECTION = "captacion_contact_identity"
METRICS_COLLECTION = "captacion_phone_learning_metrics"
IDENTITY_VERSION = "contact-identity-v2"
SUPPORTED_PORTALS = frozenset({"chilepropiedades", "yapo", "toctoc"})
PHONE_SOURCES = frozenset({"SCRAPER", "EXECUTIVE", "IMPORT", "UNKNOWN_LEGACY"})
PHONE_KPI_VERSION = "phone-learning-kpi-v1"
_INDEXES_READY = False
_INDEX_LOCK = threading.Lock()
_IDENTITY_LOCKS: dict[str, threading.Lock] = {}
_IDENTITY_LOCKS_GUARD = threading.Lock()
logger = logging.getLogger(__name__)


def _identity_lock(identity_key: str) -> threading.Lock:
    """Serialize same-phone updates inside one worker process."""
    with _IDENTITY_LOCKS_GUARD:
        return _IDENTITY_LOCKS.setdefault(identity_key, threading.Lock())


def _masked_phone(phone: str) -> str:
    phone = str(phone or "")
    return f"***{phone[-4:]}" if phone else "(sin teléfono)"


def _is_duplicate_key(exc: BaseException) -> bool:
    return exc.__class__.__name__ == "DuplicateKeyError" or "duplicate key" in str(exc).lower()


def _utc(value: Any = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def phone_learning_activation_at() -> datetime | None:
    """Return the fixed prospective activation instant in UTC."""
    raw = str(
        getattr(Config, "PHONE_LEARNING_PRODUCTION_ACTIVATED_AT", "")
        or getattr(Config, "PHONE_LEARNING_ACTIVATED_AT", "")
        or ""
    ).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.error("[CP_PHONE_LEARNING] invalid_activation_timestamp")
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def phone_learning_enabled() -> bool:
    return bool(getattr(Config, "PHONE_LEARNING_ENABLED", False))


def phone_learning_global_lookup_enabled() -> bool:
    """Compatibility helper: global lookup follows the single kill switch."""
    return phone_learning_enabled()


def phone_learning_auto_match_enabled() -> bool:
    """Compatibility helper: auto-match follows the single kill switch."""
    return phone_learning_enabled()


def normalize_phone(value: Any) -> str:
    """Return one strict Chilean/international identity representation.

    The stored identity is digits-only for compatibility with the existing
    materialized fields. Invalid and ambiguous values return an empty string;
    this function never performs fuzzy matching or truncation.
    """
    normalized = normalize_phone_strict(str(value or ""))
    return re.sub(r"\D", "", normalized or "")


def normalize_phone_source(value: Any, *, default: str = "UNKNOWN_LEGACY") -> str:
    source = str(value or "").strip().upper()
    return source if source in PHONE_SOURCES else default


def _portal_from_document(property_doc: dict[str, Any], event: dict[str, Any] | None = None) -> str:
    event = event or {}
    portal = str(
        event.get("source_portal")
        or event.get("portal")
        or property_doc.get("source_portal")
        or property_doc.get("origen")
        or ""
    ).strip().lower()
    return "chilepropiedades" if portal == "chilepropiedades.cl" else portal


def _phone_metadata(property_doc: dict[str, Any], event: dict[str, Any] | None = None) -> dict[str, Any]:
    details = property_doc.get("details") or {}
    return {
        "phone_source": normalize_phone_source(
            property_doc.get("phone_source") or details.get("phone_source")
        ),
        "phone_added_at": property_doc.get("phone_added_at") or details.get("phone_added_at"),
        "phone_added_by": property_doc.get("phone_added_by") or details.get("phone_added_by") or "",
        "phone_version": property_doc.get("phone_version") or details.get("phone_version") or 1,
        "portal": _portal_from_document(property_doc, event),
    }


def normalize_seller_name(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _phone_from_document(property_doc: dict[str, Any]) -> str:
    for key in ("phone_normalized", "telefono_normalizado", "contact_phone", "telefono", "whatsapp_phone"):
        phone = normalize_phone(property_doc.get(key))
        if phone:
            return phone
    details = property_doc.get("details") or {}
    for key in ("phone_normalized", "telefono_normalizado", "contact_phone", "telefono", "whatsapp_phone"):
        phone = normalize_phone(details.get(key))
        if phone:
            return phone
    contact = property_doc.get("public_visible_contact") or {}
    for phone in contact.get("phones") or []:
        normalized = normalize_phone(phone)
        if normalized:
            return normalized
    return ""


def _seller_from_document(property_doc: dict[str, Any]) -> str:
    for key in ("seller_name", "publisher_name", "contact_name", "publicador_visible", "listing_advertiser"):
        name = normalize_seller_name(property_doc.get(key))
        if name:
            return name
    return ""


def _identity_key(phone: str, seller_name: str) -> str:
    # Names are descriptive evidence only. They must never create a blocking
    # identity because the same name can belong to owners and brokers.
    return f"phone:{phone}" if phone else ""


def ensure_contact_identity_indexes(db) -> bool:
    global _INDEXES_READY
    if _INDEXES_READY:
        return True
    with _INDEX_LOCK:
        if _INDEXES_READY:
            return True
        try:
            collection = db[COLLECTION]
            collection.create_index("identity_key", unique=True, name="captacion_contact_identity_key")
            collection.create_index("phone_normalized", sparse=True, name="captacion_contact_identity_phone")
            _INDEXES_READY = True
            logger.info(
                "[CP_CONTACT_IDENTITY] indexes_ready collection=%s unique=identity_key phone_lookup=phone_normalized",
                COLLECTION,
            )
            return True
        except Exception as exc:
            logger.warning(
                "[CP_CONTACT_IDENTITY] unavailable action=ensure_indexes error=%s",
                type(exc).__name__,
                exc_info=True,
            )
            return False


def get_contact_identity_evidence(db_or_collection, property_doc: dict[str, Any]) -> dict[str, Any] | None:
    phone = _phone_from_document(property_doc)
    if not phone:
        return None
    try:
        get_collection = getattr(db_or_collection, "get_collection", None)
        if callable(get_collection):
            collection = get_collection(COLLECTION)
        elif hasattr(db_or_collection, "__getitem__") and not callable(getattr(db_or_collection, "find_one", None)):
            collection = db_or_collection[COLLECTION]
        else:
            collection = db_or_collection
        row = collection.find_one({"phone_normalized": phone})
        if row:
            logger.debug(
                "[CP_CONTACT_IDENTITY] lookup=hit phone=%s status=%s",
                _masked_phone(phone),
                row.get("status"),
            )
        return dict(row) if row else None
    except Exception as exc:
        # Identity is enrichment, never a reason to crash assignment. The
        # central CP gate remains fail-closed when this returns None.
        logger.warning(
            "[CP_CONTACT_IDENTITY] lookup=unavailable phone=%s error=%s",
            _masked_phone(phone),
            type(exc).__name__,
        )
        return None


def _append_unique(items: list[Any], value: Any, *, limit: int = 1000) -> list[Any]:
    values = list(items or [])
    if value and value not in values:
        values.append(value)
    return values[-limit:]


def _record_broker_feedback_metric(db, *, event_id: str, has_phone: bool, occurred_at: datetime) -> None:
    """Record prospective broker-feedback counters idempotently.

    This metric document is separate from contact identity so a broker
    feedback without a phone is measurable without fabricating an identity.
    It is intentionally not used by assignment decisions.
    """
    if not phone_learning_enabled():
        return
    activation_at = phone_learning_activation_at()
    if activation_at and occurred_at < activation_at:
        return
    metric_key = f"phone-learning:{activation_at.isoformat() if activation_at else 'unbounded'}"
    lock = _identity_lock(metric_key)
    with lock:
        try:
            collection = db[METRICS_COLLECTION]
            existing = collection.find_one({"_id": metric_key}) or {}
            event_ids = {str(value) for value in existing.get("broker_feedback_event_ids") or []}
            if event_id in event_ids:
                return
            event_ids.add(event_id)
            payload = {
                "_id": metric_key,
                "activation_at": activation_at,
                "broker_feedback_total": int(existing.get("broker_feedback_total") or 0) + 1,
                "broker_feedback_with_phone": int(existing.get("broker_feedback_with_phone") or 0) + (1 if has_phone else 0),
                "broker_feedback_without_phone": int(existing.get("broker_feedback_without_phone") or 0) + (0 if has_phone else 1),
                "broker_feedback_event_ids": list(event_ids)[-10000:],
                "updated_at": datetime.now(timezone.utc),
                "metric_version": PHONE_KPI_VERSION,
            }
            collection.update_one({"_id": metric_key}, {"$set": payload}, upsert=True)
        except Exception as exc:
            logger.warning(
                "[CP_PHONE_LEARNING] broker_feedback_metric_unavailable error=%s",
                type(exc).__name__,
            )


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def calculate_contact_features(db_or_collection, property_doc: dict[str, Any]) -> dict[str, Any]:
    """Calculate phone repetition features without making a classification.

    This deliberately remains descriptive evidence. No value returned here is
    used as a broker verdict or as an assignment veto.
    """
    phone = _phone_from_document(property_doc)
    empty = {
        "phone_publication_count": 0,
        "phone_commune_count": 0,
        "phone_property_count": 0,
        "phone_property_ids": [],
        "phone_name_count": 0,
        "phone_active_property_count": 0,
        "phone_first_seen_at": None,
        "phone_last_seen_at": None,
        "phone_publications_per_30_days": 0.0,
        "phone_portals": [],
        "phone_portal_count": 0,
        "feature_version": IDENTITY_VERSION,
    }
    if not phone:
        return empty
    try:
        get_collection = getattr(db_or_collection, "get_collection", None)
        if callable(get_collection):
            collection = get_collection(Config.CAPTACION_COLLECTION_NAME)
        elif hasattr(db_or_collection, "__getitem__") and not callable(getattr(db_or_collection, "find", None)):
            collection = db_or_collection[Config.CAPTACION_COLLECTION_NAME]
        else:
            collection = db_or_collection
        finder = getattr(collection, "find", None)
        rows = list(finder({"telefono_normalizado": phone})) if callable(finder) else []
    except Exception as exc:
        logger.warning(
            "[CP_CONTACT_IDENTITY] features=unavailable phone=%s error=%s",
            _masked_phone(phone),
            type(exc).__name__,
        )
        return empty
    if not any(str(row.get("listing_id") or row.get("_id")) == str(property_doc.get("listing_id") or property_doc.get("_id")) for row in rows):
        rows.append(property_doc)
    communes = {str(row.get("comuna_slug") or row.get("comuna") or "").strip().lower() for row in rows}
    names = {_seller_from_document(row) for row in rows}
    dates = [_as_datetime(row.get("fecha_publicacion") or row.get("created_at") or row.get("updated_at")) for row in rows]
    dates = [value for value in dates if value]
    first_seen = min(dates) if dates else None
    last_seen = max(dates) if dates else None
    span_days = max((last_seen - first_seen).total_seconds() / 86400.0, 1.0) if first_seen and last_seen else 1.0
    terminal = {"CAPTADO", "CAPTADA", "DESCARTADO", "CORREDOR", "TELÉFONO INVÁLIDO", "PROPIEDAD NO DISPONIBLE", "PUBLICACIÓN EXPIRADA", "NO INTERESADO", "DUPLICADO"}
    active_count = sum(
        1 for row in rows
        if str(row.get("scrape_stage") or "").lower() not in {"ad_removed", "needs_rescrape", "incomplete"}
        and str((row.get("gestion") or {}).get("estado") or "").strip().upper() not in terminal
    )
    return {
        "phone_publication_count": len(rows),
        "phone_commune_count": len({value for value in communes if value}),
        "phone_property_count": len(rows),
        "phone_property_ids": [str(row.get("_id") or row.get("listing_id")) for row in rows if row.get("_id") is not None or row.get("listing_id") is not None][:1000],
        "phone_name_count": len({value for value in names if value}),
        "phone_portals": sorted({str(row.get("origen") or row.get("source_portal") or "").strip().lower() for row in rows if str(row.get("origen") or row.get("source_portal") or "").strip()}),
        "phone_portal_count": len({str(row.get("origen") or row.get("source_portal") or "").strip().lower() for row in rows if str(row.get("origen") or row.get("source_portal") or "").strip()}),
        "phone_active_property_count": active_count,
        "phone_first_seen_at": first_seen,
        "phone_last_seen_at": last_seen,
        "phone_publications_per_30_days": round(len(rows) / span_days * 30.0, 4),
        "feature_version": IDENTITY_VERSION,
    }


def build_phone_learning_kpi_snapshot(
    *,
    worked_count: int,
    broker_count: int,
    worked_with_phone: int,
    broker_with_phone: int,
    reuse_prevented: int,
    known_broker_reentries: int,
    known_broker_auto_match: int = 0,
    broker_feedback_without_phone: int = 0,
    contact_identity_count: int = 0,
    evaluated_at: Any = None,
) -> dict[str, Any]:
    """Build a dated KPI snapshot without reading or writing Mongo.

    The caller supplies the already audited cohort counts. Keeping this
    calculation pure makes it safe for dry-runs and lets a later report job
    persist the same contract idempotently.
    """
    timestamp = _utc(evaluated_at)

    def metric(numerator: int, denominator: int | None = None) -> dict[str, Any]:
        numerator = max(0, int(numerator or 0))
        denominator_value = None if denominator is None else max(0, int(denominator or 0))
        value = (
            round(numerator / denominator_value, 6)
            if denominator_value
            else (float(numerator) if denominator is None else 0.0)
        )
        return {
            "value": value,
            "numerator": numerator,
            "denominator": denominator_value,
            "timestamp": timestamp,
        }

    return {
        "metric_version": PHONE_KPI_VERSION,
        "generated_at": timestamp,
        "BROKER_RATE_WORKED": metric(broker_count, worked_count),
        "BROKER_PHONE_COVERAGE": metric(broker_with_phone, broker_count),
        "BROKER_REUSE_PREVENTED": metric(reuse_prevented),
        "KNOWN_BROKER_REENTRY_RATE": metric(known_broker_reentries, worked_count),
        "KNOWN_BROKER_AUTO_MATCH": metric(known_broker_auto_match),
        "BROKER_FEEDBACK_WITHOUT_PHONE": metric(broker_feedback_without_phone),
        "CONTACT_IDENTITY_COUNT": metric(contact_identity_count),
    }


def record_human_feedback(
    db,
    *,
    property_doc: dict[str, Any],
    event: dict[str, Any],
    classification: str,
) -> dict[str, Any] | None:
    """Record one human broker/owner observation for a supported portal.

    The event id makes this operation idempotent. A contradictory human
    outcome changes the identity status to CONFLICT and is never auto-resolved.
    """
    if not phone_learning_enabled():
        return None
    origen = _portal_from_document(property_doc, event)
    if not origen:
        return None
    classification = str(classification or "").upper()
    if classification not in {"CORREDOR", "OWNER"}:
        return None
    phone = _phone_from_document(property_doc)
    seller_name = _seller_from_document(property_doc)
    identity_key = _identity_key(phone, seller_name)
    event_id = str(event.get("event_id") or event.get("source_event_id") or "")
    property_id = str(event.get("property_id") or property_doc.get("_id") or "")
    event_id = event_id or f"feedback:{event.get('event_type') or 'manual'}:{property_id}:{classification}"
    occurred_at = _utc(event.get("occurred_at"))
    if classification == "CORREDOR":
        _record_broker_feedback_metric(
            db,
            event_id=event_id,
            has_phone=bool(phone),
            occurred_at=occurred_at,
        )
    if not identity_key:
        if classification == "CORREDOR":
            logger.info(
                "[CP_PHONE_LEARNING] broker_feedback_without_phone property_id=%s event_id=%s",
                property_id,
                event_id,
            )
            return {
                "status": "NO_PHONE",
                "phone_identity_created": False,
                "broker_feedback_without_phone": True,
                "event_id": event_id,
                "property_id": property_id,
            }
        return None

    if not ensure_contact_identity_indexes(db):
        return None
    collection = db[COLLECTION]
    lock = _identity_lock(identity_key)
    with lock:
        return _record_human_feedback_locked(
            collection,
            db=db,
            identity_key=identity_key,
            phone=phone,
            seller_name=seller_name,
            property_doc=property_doc,
            event=event,
            classification=classification,
            event_id=event_id,
            portal=origen,
        )


def _record_human_feedback_locked(
    collection,
    *,
    db,
    identity_key: str,
    phone: str,
    seller_name: str,
    property_doc: dict[str, Any],
    event: dict[str, Any],
    classification: str,
    event_id: str,
    portal: str,
) -> dict[str, Any] | None:
    """Write feedback with an atomic event append when PyMongo supports it."""
    try:
        existing = collection.find_one({"identity_key": identity_key}) or {}
        existing_event_ids = {str(item) for item in existing.get("evidence_event_ids") or []}
        if event_id in existing_event_ids:
            return dict(existing)

        now = _utc(event.get("occurred_at"))
        contact_features = calculate_contact_features(db, property_doc)
        phone_meta = _phone_metadata(property_doc, event)
        evidence = {
            "event_id": event_id,
            "event_type": event.get("event_type"),
            "classification": classification,
            "property_id": str(event.get("property_id") or property_doc.get("_id") or ""),
            "phone_normalized": phone,
            "seller_name_normalized": seller_name,
            "actor_user_id": str(event.get("actor_user_id") or ""),
            "actor_name_snapshot": event.get("actor_name_snapshot") or "",
            "occurred_at": now,
            "source_system": event.get("source_system") or "captacion_crm",
            "source": "HUMAN_FEEDBACK",
            "portal": portal,
            "phone_source": phone_meta["phone_source"],
            "phone_added_at": phone_meta["phone_added_at"],
            "phone_added_by": phone_meta["phone_added_by"],
            "phone_version": phone_meta["phone_version"],
            "notes": event.get("notes") or "",
            "classification_version": IDENTITY_VERSION,
            "contact_features": contact_features,
        }

        property_ids = [str(value) for value in contact_features.get("phone_property_ids") or [] if value]
        property_ids.append(str(property_doc.get("_id") or event.get("property_id") or ""))
        property_ids = list(dict.fromkeys(value for value in property_ids if value))[-1000:]
        atomic_writer = getattr(collection, "find_one_and_update", None)
        if callable(atomic_writer):
            try:
                from pymongo import ReturnDocument
                updated = atomic_writer(
                    {"identity_key": identity_key, "evidence_event_ids": {"$ne": event_id}},
                    {
                        "$setOnInsert": {
                            "identity_key": identity_key,
                            "phone_normalized": phone,
                            "seller_name_normalized": seller_name,
                            "first_seen_at": now,
                            "classification_version": IDENTITY_VERSION,
                            "sources": [portal],
                            "confirmed_portals": [portal],
                            "observed_portals": [portal],
                        },
                        "$set": {
                            "last_seen_at": now,
                            "last_human_confirmation_at": now,
                            "confirmed_by_human": True,
                            "updated_at": datetime.now(timezone.utc),
                        },
                        "$inc": {
                            "evidence_count": 1,
                            "confirmed_corredor_count": 1 if classification == "CORREDOR" else 0,
                            "confirmed_owner_count": 1 if classification == "OWNER" else 0,
                        },
                        "$push": {"evidence": {"$each": [evidence], "$slice": -1000}},
                        "$addToSet": {
                            "evidence_event_ids": event_id,
                            "sources": portal,
                            "confirmed_portals": portal,
                            "observed_portals": portal,
                            "property_ids": {"$each": property_ids},
                        },
                    },
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
                if updated is None:
                    updated = collection.find_one({"identity_key": identity_key}) or {}
                payload = _finalize_identity_payload(collection, updated, event, classification, evidence)
                logger.info(
                    "[CP_BROKER_PROPAGATION] property_id=%s classification=%s phone=%s status=%s related_properties=%s",
                    evidence["property_id"], classification, _masked_phone(phone), payload.get("status"), payload.get("property_count", 0),
                )
                return payload
            except Exception as exc:
                if not _is_duplicate_key(exc):
                    raise
                # A concurrent worker inserted the same identity key. Reload
                # and run the idempotent path once more under the process lock.
                existing = collection.find_one({"identity_key": identity_key}) or {}
                if event_id in {str(item) for item in existing.get("evidence_event_ids") or []}:
                    return dict(existing)

        # Compatibility path for lightweight test doubles and non-PyMongo
        # adapters. The per-key lock prevents same-process lost updates.
        existing = collection.find_one({"identity_key": identity_key}) or {}
        existing_event_ids = {str(item) for item in existing.get("evidence_event_ids") or []}
        if event_id in existing_event_ids:
            return dict(existing)

        broker_count = int(existing.get("confirmed_corredor_count") or 0) + (1 if classification == "CORREDOR" else 0)
        owner_count = int(existing.get("confirmed_owner_count") or 0) + (1 if classification == "OWNER" else 0)
        status = "CONFLICT" if broker_count and owner_count else ("CORREDOR_CONFIRMED" if broker_count else "OWNER_CONFIRMED")
        old_evidence = list(existing.get("evidence") or [])
        old_evidence.append(evidence)
        old_property_ids = list(existing.get("property_ids") or [])
        for property_id in property_ids:
            old_property_ids = _append_unique(old_property_ids, property_id)
        conflicts = list(existing.get("conflicts") or [])
        previous = str(existing.get("classification") or "")
        if previous in {"CORREDOR", "OWNER"} and previous != classification:
            conflicts.append({
                "type": "HUMAN_CLASSIFICATION_CONTRADICTION",
                "previous": previous,
                "current": classification,
                "event_id": evidence["event_id"],
                "property_id": evidence["property_id"],
                "occurred_at": now,
            })
        payload = {
            "identity_key": identity_key,
            "phone_normalized": phone,
            "seller_name_normalized": seller_name,
            "classification": "CONFLICT" if status == "CONFLICT" else classification,
            "confidence": 1.0,
            "evidence_count": len(old_evidence),
            "confirmed_by_human": True,
            "confirmed_corredor_count": broker_count,
            "confirmed_owner_count": owner_count,
            "first_seen_at": existing.get("first_seen_at") or now,
            "last_seen_at": now,
            "last_human_confirmation_at": now,
            "property_count": len([item for item in old_property_ids if item]),
            "sources": _append_unique(existing.get("sources") or [], portal),
            "confirmed_portals": _append_unique(existing.get("confirmed_portals") or [], portal),
            "observed_portals": _append_unique(existing.get("observed_portals") or [], portal),
            "phone_source": phone_meta["phone_source"],
            "phone_added_at": phone_meta["phone_added_at"],
            "phone_added_by": phone_meta["phone_added_by"],
            "phone_version": phone_meta["phone_version"],
            "property_ids": [item for item in old_property_ids if item],
            "conflicts": conflicts[-1000:],
            "status": status,
            "classification_version": IDENTITY_VERSION,
            "evidence": old_evidence[-1000:],
            "evidence_event_ids": _append_unique(list(existing_event_ids), event_id),
            "updated_at": datetime.now(timezone.utc),
        }
        collection.update_one({"identity_key": identity_key}, {"$set": payload}, upsert=True)
        logger.info(
            "[CP_BROKER_PROPAGATION] property_id=%s classification=%s phone=%s status=%s related_properties=%s",
            evidence["property_id"], classification, _masked_phone(phone), status, payload["property_count"],
        )
        return payload
    except Exception as exc:
        logger.exception(
            "[CP_CONTACT_IDENTITY] record_failed identity_key=%s error=%s",
            identity_key, type(exc).__name__,
        )
        return None


def record_automatic_broker_evidence(
    db,
    *,
    property_doc: dict[str, Any],
    evidence_type: str,
    classifier: str,
    confidence: Any = None,
    occurred_at: Any = None,
) -> dict[str, Any] | None:
    """Store an automatic broker signal without creating human reputation.

    Automatic evidence is deliberately kept at ``UNRESOLVED`` and never
    increments human broker counts. It can therefore be audited and studied
    later without contaminating cross-portal assignment decisions.
    """
    if not phone_learning_enabled() or not phone_learning_auto_match_enabled():
        return None
    portal = _portal_from_document(property_doc)
    phone = _phone_from_document(property_doc)
    if not portal or not phone:
        return None
    if not ensure_contact_identity_indexes(db):
        return None
    collection = db[COLLECTION]
    identity_key = _identity_key(phone, _seller_from_document(property_doc))
    property_id = str(property_doc.get("_id") or property_doc.get("listing_id") or property_doc.get("url") or "")
    event_id = f"auto:{portal}:{property_id}:{str(evidence_type or 'BROKER_SIGNAL').upper()}:{classifier}"
    occurred = _utc(occurred_at)
    lock = _identity_lock(identity_key)
    with lock:
        existing = collection.find_one({"identity_key": identity_key}) or {}
        event_ids = {str(item) for item in existing.get("evidence_event_ids") or []}
        if event_id in event_ids:
            return dict(existing)
        metadata = _phone_metadata(property_doc)
        evidence = {
            "event_id": event_id,
            "event_type": "automatic_broker_signal",
            "source": "AUTOMATIC_BROKER_SIGNAL",
            "evidence_type": str(evidence_type or "BROKER_SIGNAL").upper(),
            "classifier": str(classifier or "unknown"),
            "confidence": confidence,
            "property_id": property_id,
            "portal": portal,
            "phone_normalized": phone,
            "occurred_at": occurred,
            "phone_source": metadata["phone_source"],
            "classification_version": IDENTITY_VERSION,
        }
        old_evidence = list(existing.get("evidence") or [])
        old_evidence.append(evidence)
        property_ids = _append_unique(existing.get("property_ids") or [], property_id)
        payload = {
            "identity_key": identity_key,
            "phone_normalized": phone,
            "seller_name_normalized": existing.get("seller_name_normalized") or _seller_from_document(property_doc),
            "classification": existing.get("classification") or "UNRESOLVED",
            "status": existing.get("status") or "UNRESOLVED",
            "confidence": existing.get("confidence"),
            "confirmed_by_human": bool(existing.get("confirmed_by_human")),
            "confirmed_corredor_count": int(existing.get("confirmed_corredor_count") or 0),
            "confirmed_owner_count": int(existing.get("confirmed_owner_count") or 0),
            "first_seen_at": existing.get("first_seen_at") or occurred,
            "last_seen_at": occurred,
            "last_human_confirmation_at": existing.get("last_human_confirmation_at"),
            "property_count": len([item for item in property_ids if item]),
            "property_ids": property_ids,
            "sources": _append_unique(existing.get("sources") or [], portal),
            "confirmed_portals": list(existing.get("confirmed_portals") or []),
            "observed_portals": _append_unique(existing.get("observed_portals") or [], portal),
            "phone_source": existing.get("phone_source") or metadata["phone_source"],
            "phone_added_at": existing.get("phone_added_at") or metadata["phone_added_at"],
            "phone_added_by": existing.get("phone_added_by") or metadata["phone_added_by"],
            "phone_version": existing.get("phone_version") or metadata["phone_version"],
            "evidence": old_evidence[-1000:],
            "evidence_event_ids": _append_unique(list(event_ids), event_id),
            "evidence_count": len(old_evidence),
            "classification_version": IDENTITY_VERSION,
            "updated_at": datetime.now(timezone.utc),
        }
        collection.update_one({"identity_key": identity_key}, {"$set": payload}, upsert=True)
        return dict(collection.find_one({"identity_key": identity_key}) or payload)


def _finalize_identity_payload(collection, updated: dict[str, Any], event: dict[str, Any], classification: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Derive visible status after an atomic evidence append."""
    broker_count = int(updated.get("confirmed_corredor_count") or 0)
    owner_count = int(updated.get("confirmed_owner_count") or 0)
    status = "CONFLICT" if broker_count and owner_count else ("CORREDOR_CONFIRMED" if broker_count else "OWNER_CONFIRMED")
    conflicts = list(updated.get("conflicts") or [])
    active_classifications = {str(item.get("classification") or "") for item in updated.get("evidence") or [] if not item.get("revoked")}
    if len(active_classifications & {"CORREDOR", "OWNER"}) > 1:
        conflict_record = {
            "type": "HUMAN_CLASSIFICATION_CONTRADICTION",
            "classifications": sorted(active_classifications & {"CORREDOR", "OWNER"}),
            "event_id": evidence.get("event_id"),
            "property_id": evidence.get("property_id"),
            "occurred_at": evidence.get("occurred_at"),
        }
        if conflict_record not in conflicts:
            conflicts.append(conflict_record)
    payload = {
        "classification": "CONFLICT" if status == "CONFLICT" else classification,
        "status": status,
        "property_count": len([item for item in updated.get("property_ids") or [] if item]),
        "conflicts": conflicts[-1000:],
        "classification_version": IDENTITY_VERSION,
        "last_confirmed_portal": evidence.get("portal") or "",
        "confirmed_portals": _append_unique(updated.get("confirmed_portals") or [], evidence.get("portal")),
        "observed_portals": _append_unique(updated.get("observed_portals") or [], evidence.get("portal")),
        "sources": _append_unique(updated.get("sources") or [], evidence.get("portal")),
        "phone_source": evidence.get("phone_source") or updated.get("phone_source") or "UNKNOWN_LEGACY",
        "phone_added_at": evidence.get("phone_added_at") or updated.get("phone_added_at"),
        "phone_added_by": evidence.get("phone_added_by") or updated.get("phone_added_by") or "",
        "phone_version": evidence.get("phone_version") or updated.get("phone_version") or 1,
        "updated_at": datetime.now(timezone.utc),
    }
    collection.update_one({"identity_key": updated.get("identity_key")}, {"$set": payload}, upsert=False)
    updated.update(payload)
    return dict(updated)


def revoke_human_feedback(
    db,
    *,
    original_event_id: str,
    revocation_event: dict[str, Any],
) -> int:
    """Revoke active identity evidence while preserving its audit trail."""
    if not original_event_id:
        return 0
    if not ensure_contact_identity_indexes(db):
        return 0
    try:
        collection = db[COLLECTION]
        finder = getattr(collection, "find", None)
        rows = list(finder({})) if callable(finder) else []
    except Exception as exc:
        logger.warning(
            "[CP_CONTACT_IDENTITY] revoke=unavailable event_id=%s error=%s",
            original_event_id, type(exc).__name__,
        )
        return 0
    updated = 0
    revoked_at = _utc(revocation_event.get("occurred_at"))
    for row in rows:
        try:
            identity_key = str(row.get("identity_key") or "")
            with _identity_lock(identity_key):
                evidence = list(row.get("evidence") or [])
                changed = False
                for item in evidence:
                    if str(item.get("event_id") or "") == str(original_event_id) and not item.get("revoked"):
                        item["revoked"] = True
                        item["revoked_at"] = revoked_at
                        item["revocation_event_id"] = revocation_event.get("event_id")
                        item["revocation_reason"] = revocation_event.get("reason") or "management_event_reversed"
                        item["revoked_by_user_id"] = revocation_event.get("actor_user_id")
                        changed = True
                if not changed:
                    continue
                active = [item for item in evidence if not item.get("revoked")]
                broker_count = sum(1 for item in active if item.get("classification") == "CORREDOR")
                owner_count = sum(1 for item in active if item.get("classification") == "OWNER")
                status = "CONFLICT" if broker_count and owner_count else (
                    "CORREDOR_CONFIRMED" if broker_count else "OWNER_CONFIRMED" if owner_count else "UNRESOLVED"
                )
                payload = {
                    "evidence": evidence[-1000:],
                    "evidence_count": len(evidence),
                    "active_evidence_count": len(active),
                    "confirmed_corredor_count": broker_count,
                    "confirmed_owner_count": owner_count,
                    "classification": "CONFLICT" if status == "CONFLICT" else ("CORREDOR" if broker_count else "OWNER" if owner_count else "UNRESOLVED"),
                    "status": status,
                    "updated_at": datetime.now(timezone.utc),
                }
                collection.update_one({"identity_key": row.get("identity_key")}, {"$set": payload}, upsert=False)
                logger.info(
                    "[CP_BROKER_PROPAGATION] action=revoke event_id=%s identity_key=%s status=%s",
                    original_event_id, identity_key, status,
                )
                updated += 1
        except Exception as exc:
            logger.warning(
                "[CP_CONTACT_IDENTITY] revoke_failed event_id=%s error=%s",
                original_event_id, type(exc).__name__,
            )
    return updated

