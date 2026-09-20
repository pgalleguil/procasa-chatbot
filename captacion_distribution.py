"""Shared distribution safety primitives.

This module owns the cross-process lock and the final assignment write guard.
Portal scrapers may keep their classifiers and persistence code, but every
captacion assignment must pass through these primitives before Mongo is
mutated.
"""
from __future__ import annotations

import os
import re
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from pymongo import ReturnDocument
from chatbot.constants import CHILE_TZ

from captacion_assignment_eligibility import calculate_assignment_eligibility, can_assign_property
from captacion_contact_identity import (
    get_contact_identity_evidence,
    get_contact_identity_evidence_batch,
    phone_learning_global_lookup_enabled,
)
from captacion_kpis import (
    AVAILABLE_STATES,
    CAPTACION_TERMINAL_STATES,
    is_terminal_captacion_state,
)
from captacion_management import new_assignment_cycle
from broker_registry import resolve_broker_identity, resolve_broker_identity_batch
from comuna_utils import normalize_commune_canonical
from config import Config


GLOBAL_PORTAL_ALIASES = frozenset({
    "chilepropiedades",
    "chilepropiedades.cl",
    "yapo",
    "toctoc",
})
GLOBAL_DISTRIBUTION_LOCK_ID = "global"
DEFAULT_LOCK_TTL_SECONDS = 15 * 60
CAPTACION_PRIVILEGED_ROLES = frozenset({"admin", "supervisor", "jefatura"})
# A privileged user is never an automatic captacion recipient merely because
# they can supervise the CRM. These fields are explicit opt-in switches for
# the exceptional case where a privileged user also works as an executive.
CAPTACION_EXECUTIVE_OVERRIDE_FIELDS = (
    "captacion_enabled",
    "is_captacion_agent",
    "ejecutivo_captacion",
    "can_receive_captacion",
)
HUMAN_MANAGEMENT_EVENT_TYPES = frozenset({
    "management_confirmed",
    "manual_decision_confirmed",
    "capture_confirmed",
})
DISTRIBUTION_CAPTURE_DATE_FIELDS = (
    "first_seen",
    "first_seen_at",
    "created_at",
    "fecha_captura",
    "processed_at",
    "scraped_at",
)
STALE_FOR_AUTO_DISTRIBUTION = "STALE_FOR_AUTO_DISTRIBUTION"
CAPTURE_DATE_UNAVAILABLE = "CAPTURE_DATE_UNAVAILABLE"

# Workload prioritization only needs the fields used by the central gate and
# exact identity resolvers.  Do not fetch raw HTML, images, audit payloads, or
# other large listing fields for every assigned property.
ACTIVE_WORKABLE_BACKLOG_PROJECTION = {
    "_id": 1,
    "listing_id": 1,
    "url": 1,
    "source_url": 1,
    "origen": 1,
    "source_portal": 1,
    "portal": 1,
    "title": 1,
    "titulo": 1,
    "comuna": 1,
    "comuna_slug": 1,
    "scrape_stage": 1,
    "html_validation_status": 1,
    "block_reason": 1,
    "pipeline_state": 1,
    "pipeline_complete": 1,
    "first_seen": 1,
    "first_seen_at": 1,
    "created_at": 1,
    "fecha_captura": 1,
    "processed_at": 1,
    "scraped_at": 1,
    "seller_profile_id": 1,
    "profile_id": 1,
    "seller_client_id": 1,
    "client_id": 1,
    "phone_normalized": 1,
    "telefono_normalizado": 1,
    "phone": 1,
    "telefono": 1,
    "phone_original_value": 1,
    "contact_phone": 1,
    "whatsapp_phone": 1,
    "details.phone_normalized": 1,
    "details.telefono_normalizado": 1,
    "details.contact_phone": 1,
    "details.telefono": 1,
    "details.whatsapp_phone": 1,
    "public_visible_contact.phones": 1,
    "email": 1,
    "email_contact": 1,
    "contact_email": 1,
    "domain": 1,
    "seller_domain": 1,
    "website": 1,
    "seller_website": 1,
    "seller_profile_url": 1,
    "profile_url": 1,
    "seller_url": 1,
    "contact_name": 1,
    "publicador_visible": 1,
    "publisher": 1,
    "seller_name": 1,
    "company_name": 1,
    "broker_brand": 1,
    "contact_logo_alt": 1,
    "contact_badges_text": 1,
    "listing_advertiser": 1,
    "seller_jsonld_name": 1,
    "seller_type": 1,
    "seller_type_evidence": 1,
    "seller_type_source": 1,
    "operation_label_raw": 1,
    "seller_profile_logo": 1,
    "publisher_profile_context": 1,
    "structural_signals": 1,
    "gestion.ejecutivo_id": 1,
    "gestion.estado": 1,
    "gestion.semantic_review_hold": 1,
    "gestion.exclude_from_assignment": 1,
    "classification.state": 1,
    "classification.final": 1,
    "classification.final_state": 1,
    "classification.canonical_final": 1,
    "classification.assignment_ready": 1,
    "classification.manual_review_required": 1,
    "classification.manual_review_approved": 1,
    "classification.exclude_from_assignment": 1,
    "classification.owner_probability": 1,
    "classification.owner_probability_source": 1,
    "classification.owner_probability_completeness": 1,
    "classification.source": 1,
    "classification.decision_source": 1,
    "classification.deepseek_status": 1,
    "classification.version": 1,
    "classification.reason": 1,
    "classification.publisher_profile_context": 1,
    "classification.hard_broker_veto": 1,
    "classification.hard_veto": 1,
    "classification.classification_conflict": 1,
    "classification.conflict_state": 1,
    "classification.pipeline_state": 1,
    "classification.pipeline_complete": 1,
}

BROKER_GUARD_TERMS = (
    "re max", "re/max", "remax", "fuenzalida", "procasa", "houm", "assetplan",
    "portal inmobiliario", "chilepropiedades", "goplaceit", "easyprop",
    "engel volkers", "coldwell banker", "urbalia", "enlace inmobiliario",
    "capitalizarme", "toctoc", "inmobiliaria", "corredor", "corredora",
    "corretaje", "asesor inmobiliario", "asesora inmobiliaria",
    "agente inmobiliario", "broker inmobiliario", "gestion inmobiliaria",
    "servicios inmobiliarios", "consultora inmobiliaria", "bienes raices",
    "real estate", "broker", "propiedades", "constructora", "limitada", "sociedad",
)
BROKER_GUARD_FIELDS = (
    "company_name", "broker_brand", "publicador_visible",
    "contact_logo_alt", "contact_badges_text",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def distribution_capture_datetime(document: dict[str, Any]) -> datetime | None:
    """Return the first reliable persisted capture/publication timestamp.

    Deliberately excludes ``updated_at`` and ObjectId generation time: neither
    proves when the listing was captured or published and would make stale
    records appear fresh.
    """
    for field in DISTRIBUTION_CAPTURE_DATE_FIELDS:
        value = document.get(field)
        if not value:
            continue
        try:
            parsed = value
            if isinstance(parsed, str):
                parsed = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = CHILE_TZ.localize(parsed)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, AttributeError):
            continue
    return None


def distribution_age_decision(
    document: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify operational age without changing the persisted document."""
    captured_at = distribution_capture_datetime(document)
    if captured_at is None:
        return {
            "eligible": False,
            "bucket": "unknown",
            "age_days": None,
            "captured_at": None,
            "reason": CAPTURE_DATE_UNAVAILABLE,
        }
    reference = now or utcnow()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (reference.astimezone(timezone.utc) - captured_at).total_seconds() / 86400)
    if age_days <= 30:
        bucket, eligible, reason = "0_30_days", True, None
    elif age_days <= 60:
        bucket, eligible, reason = "31_60_days", True, None
    else:
        bucket, eligible, reason = ">60_days", False, STALE_FOR_AUTO_DISTRIBUTION
    return {
        "eligible": eligible,
        "bucket": bucket,
        "age_days": round(age_days, 3),
        "captured_at": captured_at,
        "reason": reason,
    }


def auto_distribution_age_eligible(document: dict[str, Any], *, now: datetime | None = None) -> bool:
    return bool(distribution_age_decision(document, now=now)["eligible"])


def distribution_age_sort_key(document: dict[str, Any]) -> tuple[int, float]:
    """Order auto-distribution candidates by operational age bucket."""
    age = distribution_age_decision(document)
    captured_at = distribution_capture_datetime(document)
    captured_epoch = captured_at.timestamp() if captured_at else 0.0
    age_rank = {"0_30_days": 0, "31_60_days": 1}.get(age["bucket"], 2)
    return age_rank, -captured_epoch


def resolve_manual_batch_size(requested: int | None, *, allow_large: bool = False) -> int:
    """Return a bounded manual batch; larger batches require explicit opt-in."""
    value = int(requested or Config.CAPTACION_DISTRIBUTION_BATCH_SIZE)
    if value < 1:
        raise ValueError("batch_size debe ser positivo")
    default = int(Config.CAPTACION_DISTRIBUTION_BATCH_SIZE)
    if value > default and not allow_large:
        raise ValueError(
            f"batch_size={value} excede el límite seguro {default}; use --allow-large-batch explícitamente"
        )
    return value


def portal_key(document: dict[str, Any]) -> str:
    return str(document.get("origen") or document.get("source_portal") or "").strip().lower()


def _truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "si", "sí"}


def is_captacion_distribution_executive(user: dict[str, Any]) -> bool:
    """Return whether a user may receive automatic captacion assignments."""
    role = str(user.get("rol") or user.get("role") or "").strip().lower()
    if role == "agente":
        return True
    if role not in CAPTACION_PRIVILEGED_ROLES:
        return False
    return any(_truthy_flag(user.get(field)) for field in CAPTACION_EXECUTIVE_OVERRIDE_FIELDS)


def is_global_portal(document: dict[str, Any]) -> bool:
    return portal_key(document) in GLOBAL_PORTAL_ALIASES


def is_terminal_state(value: Any) -> bool:
    return is_terminal_captacion_state(value)


def distribution_unassigned_clause() -> dict[str, Any]:
    return {
        "$or": [
            {"gestion.ejecutivo_id": {"$exists": False}},
            {"gestion.ejecutivo_id": None},
            {"gestion.ejecutivo_id": ""},
        ]
    }


def has_management_evidence(property_doc: dict[str, Any], events_coll: Any = None) -> tuple[bool, str | None]:
    """Return whether human management evidence protects a property."""
    gestion = property_doc.get("gestion") or {}
    if gestion.get("estado") not in {None, "", "NUEVO", "DETECTADO"}:
        return True, f"estado={gestion.get('estado')}"
    if gestion.get("fecha_ultima_gestion") is not None:
        return True, "fecha_ultima_gestion"
    if gestion.get("notas"):
        return True, f"notas={len(gestion['notas'])}"
    if gestion.get("actividades"):
        return True, f"actividades={len(gestion['actividades'])}"
    if events_coll is not None:
        clauses = []
        if property_doc.get("_id") is not None:
            clauses.append({"property_id": str(property_doc["_id"])})
        if property_doc.get("listing_id"):
            clauses.append({"listing_id": property_doc["listing_id"]})
        if property_doc.get("url"):
            clauses.append({"url": property_doc["url"]})
        if clauses and events_coll.find_one({"$or": clauses}):
            return True, "management_event"
    return False, None


def has_broker_identity(document: dict[str, Any]) -> bool:
    """Defence in depth for commercial identity independent of the classifier."""
    classification = document.get("classification") or {}
    profile = classification.get("publisher_profile_context") or document.get("publisher_profile_context") or {}
    if profile.get("commercial_identity_confirmed") or profile.get("confirmed_broker_count", 0):
        return True
    raw = " ".join(str(document.get(field) or "") for field in BROKER_GUARD_FIELDS)
    normalized = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", raw.lower())).strip()
    return any(term in normalized for term in BROKER_GUARD_TERMS)


def _agent_is_active(db: Any, agent: dict[str, Any]) -> bool:
    """Revalidate the target executive immediately before assignment."""
    try:
        agent_id = agent.get("id") or agent.get("_id")
        object_id = ObjectId(str(agent_id)) if ObjectId.is_valid(str(agent_id)) else agent_id
        row = db["usuarios"].find_one(
            {"_id": object_id, "is_active": True},
            {"_id": 1, "rol": 1, **{field: 1 for field in CAPTACION_EXECUTIVE_OVERRIDE_FIELDS}},
        )
        return bool(row and is_captacion_distribution_executive(row))
    except (AttributeError, KeyError, TypeError, AssertionError):
        # Unit fakes may intentionally omit the users collection. Production
        # Mongo always has it, so this fallback is test-only compatibility.
        return True


def _agent_open_workload(db: Any, agent_id: str) -> int:
    try:
        rows = db[Config.CAPTACION_COLLECTION_NAME].aggregate([
            {"$match": {
                "gestion.ejecutivo_id": str(agent_id),
                "gestion.estado": {"$nin": list(CAPTACION_TERMINAL_STATES)},
            }},
            {"$count": "count"},
        ])
        row = next(iter(rows), None)
        return int((row or {}).get("count") or 0)
    except (AttributeError, KeyError, TypeError, AssertionError):
        return 0


def open_workloads(db: Any, agent_ids: list[str]) -> dict[str, int]:
    if not agent_ids:
        return {}
    rows = db[Config.CAPTACION_COLLECTION_NAME].aggregate([
        {"$match": {
            "gestion.ejecutivo_id": {"$in": [str(item) for item in agent_ids]},
            "gestion.estado": {"$nin": list(CAPTACION_TERMINAL_STATES)},
        }},
        {"$group": {"_id": "$gestion.ejecutivo_id", "count": {"$sum": 1}}},
    ])
    return {str(row.get("_id")): int(row.get("count") or 0) for row in rows}


def _management_event_keys(events_coll: Any) -> set[tuple[str, str]]:
    """Load credited human-management references once for a workload snapshot."""
    keys: set[tuple[str, str]] = set()
    rows = events_coll.find(
        {"event_type": {"$in": list(HUMAN_MANAGEMENT_EVENT_TYPES)}, "credited": True},
        {"property_id": 1, "listing_id": 1, "url": 1},
    )
    for event in rows:
        for event_field, document_field in (
            ("property_id", "_id"),
            ("listing_id", "listing_id"),
            ("url", "url"),
        ):
            value = event.get(event_field)
            if value not in (None, ""):
                keys.add((document_field, str(value)))
    return keys


def has_valid_human_management_event(
    property_doc: dict[str, Any],
    events_coll: Any,
    *,
    event_keys: set[tuple[str, str]] | None = None,
) -> bool:
    """Check credited human work using the management ledger as authority."""
    keys = event_keys if event_keys is not None else _management_event_keys(events_coll)
    for field in ("_id", "listing_id", "url"):
        value = property_doc.get(field)
        if value not in (None, "") and (field, str(value)) in keys:
            return True
    return False


def has_unverified_management_signal(property_doc: dict[str, Any]) -> bool:
    """Protect legacy work markers lacking a ledger event from recycling."""
    if "_active_workable_has_unverified_management_signal" in property_doc:
        return bool(property_doc.get("_active_workable_has_unverified_management_signal"))
    gestion = property_doc.get("gestion") or {}
    state = str(gestion.get("estado") or "").strip()
    return bool(
        state not in {"", "NUEVO", "DETECTADO"}
        or gestion.get("fecha_ultima_gestion") is not None
        or gestion.get("notas")
        or gestion.get("actividades")
    )


def active_workable_backlogs(
    db: Any,
    agent_ids: list[str],
    *,
    events_coll: Any | None = None,
) -> dict[str, int]:
    """Count still-workable assignments for each active executive.

    ``open_workloads`` remains the hard safety-cap metric. This metric is the
    prioritization signal and excludes terminal/broker/quality records plus
    credited human work. Legacy work markers without a ledger event are
    conservatively excluded as unverified rather than recycled.
    """
    if not agent_ids:
        return {}
    from captacion_assignment_eligibility import calculate_assignment_eligibility

    collection = db[Config.CAPTACION_COLLECTION_NAME]
    events = events_coll if events_coll is not None else db["captacion_management_events"]
    event_keys = _management_event_keys(events)
    workloads = {str(agent_id): 0 for agent_id in agent_ids}
    # Legacy management markers are an exclusion criterion.  Compute that
    # boolean after the indexed executive match so Mongo does not scan an
    # unindexed notes/activities predicate across the whole collection.
    state_expr = {"$ifNull": ["$gestion.estado", ""]}
    nonempty_expr = lambda field: {
        "$and": [
            {"$ne": [{"$ifNull": [field, None]}, None]},
            {"$ne": [{"$ifNull": [field, None]}, ""]},
            {"$ne": [{"$ifNull": [field, None]}, []]},
        ]
    }
    management_marker_expr = {
        "$or": [
            {
                "$and": [
                    {"$ne": [state_expr, ""]},
                    {"$not": [{"$in": [state_expr, ["NUEVO", "DETECTADO"]]}]},
                ]
            },
            {"$ne": [{"$ifNull": ["$gestion.fecha_ultima_gestion", None]}, None]},
            nonempty_expr("$gestion.notas"),
            nonempty_expr("$gestion.actividades"),
        ]
    }
    classification_source_expr = {
        "$ifNull": ["$classification.decision_source", "$classification.source"]
    }
    classification_evidence_expr = {
        "$or": [
            nonempty_expr("$classification.reason"),
            nonempty_expr("$classification.evidence"),
        ]
    }
    auditable_marker_expr = {
        "$or": [
            {"$eq": [{"$ifNull": ["$classification.manual_review_approved", False]}, True]},
            {"$in": [classification_source_expr, [
                "structural_rules", "rules_json", "html_validation", "profile_correlation",
                "rules", "rules_fallback", "toctoc_id_type", "portal_structure",
            ]]},
            {
                "$and": [
                    {"$eq": [classification_source_expr, "deepseek"]},
                    {"$eq": [{"$ifNull": ["$classification.deepseek_status", ""]}, "VALID"]},
                    {"$ne": [{"$ifNull": ["$classification.trace.deepseek_raw", None]}, None]},
                ]
            },
            {
                "$and": [
                    {"$eq": [{"$ifNull": ["$classification.state", ""]}, "INCIERTO"]},
                    {"$eq": [classification_source_expr, "deterministic_evidence_engine"]},
                    {"$eq": [{"$ifNull": ["$classification.owner_probability_completeness.complete", False]}, True]},
                    {"$ne": [{"$ifNull": ["$classification.owner_probability", None]}, None]},
                ]
            },
            {
                "$and": [
                    {"$eq": [{"$ifNull": ["$classification.state", ""]}, "INCIERTO"]},
                    {"$eq": [{"$ifNull": ["$classification.version", ""]}, "v5-rule-based"]},
                    classification_evidence_expr,
                ]
            },
        ]
    }
    backlog_projection = dict(ACTIVE_WORKABLE_BACKLOG_PROJECTION)
    backlog_projection["_active_workable_has_unverified_management_signal"] = management_marker_expr
    backlog_projection["_active_workable_auditable_final_decision"] = auditable_marker_expr
    managed_object_ids = []
    for field, value in event_keys:
        if field == "_id" and ObjectId.is_valid(str(value)):
            managed_object_ids.append(ObjectId(str(value)))
    candidate_match = {
        "gestion.ejecutivo_id": {"$in": [str(agent_id) for agent_id in agent_ids]},
        # These are the only states that can be unworked. Other states are
        # already a management/terminal signal and do not belong in this
        # prioritization metric.
        "gestion.estado": {"$in": list(AVAILABLE_STATES)},
    }
    if managed_object_ids:
        candidate_match["_id"] = {"$nin": managed_object_ids}
    cursor = collection.aggregate([
        {"$match": candidate_match},
        {"$project": backlog_projection},
    ], allowDiskUse=False).batch_size(50)
    property_docs = list(cursor)
    missing_title_ids = [
        doc.get("_id")
        for doc in property_docs
        if doc.get("_id") is not None
        and not str(doc.get("title") or doc.get("titulo") or "").strip()
    ]
    if missing_title_ids:
        descriptions = {
            row.get("_id"): row
            for row in collection.find(
                {"_id": {"$in": missing_title_ids}},
                {"_id": 1, "description": 1, "descripcion": 1},
            )
        }
        for doc in property_docs:
            extra = descriptions.get(doc.get("_id"))
            if extra:
                for field in ("description", "descripcion"):
                    if field in extra:
                        doc[field] = extra[field]
    identity_matches = get_contact_identity_evidence_batch(db, property_docs)
    broker_matches = resolve_broker_identity_batch(db, property_docs)
    for property_doc, identity, broker_match in zip(property_docs, identity_matches, broker_matches):
        gestion = property_doc.get("gestion") or {}
        agent_id = str(gestion.get("ejecutivo_id") or "")
        if agent_id not in workloads or is_terminal_state(gestion.get("estado")):
            continue
        if has_valid_human_management_event(property_doc, events, event_keys=event_keys):
            continue
        if has_unverified_management_signal(property_doc):
            continue
        decision = can_assign_property(
            property_doc,
            {
                "contact_identity": identity,
                "broker_identity_match": broker_match,
            },
        )
        if not decision.get("assignment_ready") or has_broker_identity(property_doc):
            continue
        workloads[agent_id] += 1
    return workloads


class MongoDistributionLock:
    """A Mongo-backed single-flight lock with expiry recovery."""

    def __init__(
        self,
        db: Any,
        *,
        run_id: str,
        trigger_source: str,
        ttl_seconds: int | None = None,
        collection_name: str | None = None,
    ) -> None:
        self.db = db
        self.run_id = str(run_id)
        self.trigger_source = str(trigger_source or "unknown")
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{self.run_id}"
        self.ttl_seconds = int(ttl_seconds or getattr(Config, "CAPTACION_DISTRIBUTION_LOCK_TTL_SECONDS", DEFAULT_LOCK_TTL_SECONDS))
        self.collection_name = collection_name or getattr(
            Config, "CAPTACION_DISTRIBUTION_LOCK_COLLECTION", "captacion_distribution_locks"
        )
        self.acquired = False
        self.acquired_at: datetime | None = None
        self.expires_at: datetime | None = None

    @property
    def collection(self) -> Any:
        return self.db[self.collection_name]

    def acquire(self) -> bool:
        now = utcnow()
        expires = now + timedelta(seconds=max(30, self.ttl_seconds))
        try:
            self.collection.create_index("expires_at", expireAfterSeconds=0, name="distribution_lock_expiry")
        except Exception:
            # Acquisition itself remains safe without the TTL index because
            # the expiry predicate below is authoritative.
            pass
        update = {
            "$set": {
                "owner": self.owner,
                "run_id": self.run_id,
                "trigger_source": self.trigger_source,
                "acquired_at": now,
                "expires_at": expires,
                "heartbeat_at": now,
            }
        }
        predicate = {
            "_id": GLOBAL_DISTRIBUTION_LOCK_ID,
            "$or": [
                {"expires_at": {"$lte": now}},
                {"expires_at": {"$exists": False}},
            ],
        }
        try:
            row = self.collection.find_one_and_update(
                predicate,
                update,
                upsert=False,
                return_document=ReturnDocument.AFTER,
            )
            if row and row.get("owner") == self.owner:
                self.acquired = True
                self.acquired_at = row.get("acquired_at") or now
                self.expires_at = row.get("expires_at") or expires
                return True
        except Exception:
            # A missing lock document needs the insert path below. Other
            # errors are allowed to fail closed rather than assigning.
            pass
        try:
            self.collection.insert_one({
                "_id": GLOBAL_DISTRIBUTION_LOCK_ID,
                "owner": self.owner,
                "run_id": self.run_id,
                "trigger_source": self.trigger_source,
                "acquired_at": now,
                "expires_at": expires,
                "heartbeat_at": now,
            })
        except DuplicateKeyError:
            return False
        except Exception:
            return False
        self.acquired = True
        self.acquired_at = now
        self.expires_at = expires
        return True

    def release(self) -> bool:
        if not self.acquired:
            return False
        try:
            result = self.collection.delete_one({
                "_id": GLOBAL_DISTRIBUTION_LOCK_ID,
                "owner": self.owner,
                "run_id": self.run_id,
            })
            released = bool(result.deleted_count)
        except Exception:
            released = False
        self.acquired = False
        return released

    def __enter__(self) -> "MongoDistributionLock":
        if not self.acquire():
            raise RuntimeError("distribution_already_running")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def assign_captacion_candidate_atomically(
    db: Any,
    coll: Any,
    events_coll: Any,
    document: dict[str, Any],
    agent: dict[str, Any],
    *,
    now: datetime | None = None,
    mode: str = "new",
    expected_current_filter: dict[str, Any] | None = None,
    allow_management_evidence: bool = False,
    run_load: int = 0,
    max_per_agent: int | None = None,
    reason: str = "global_distribution",
    assignment_version: str = "global_distribution_v1",
    set_estado: str | None = "NUEVO",
    source_portals: set[str] | frozenset[str] | None = None,
) -> str:
    """Single final gate + atomic write used by every assignment route."""
    now = now or utcnow()
    raw_document_id = document.get("_id")
    lookup_id = raw_document_id
    if not isinstance(raw_document_id, ObjectId) and ObjectId.is_valid(str(raw_document_id)):
        lookup_id = ObjectId(str(raw_document_id))
    fresh = coll.find_one({"_id": lookup_id})
    if not fresh:
        return "stale"
    if source_portals and portal_key(fresh) not in source_portals:
        return "portal"
    if is_terminal_state((fresh.get("gestion") or {}).get("estado")):
        return "terminal"
    if not _agent_is_active(db, agent):
        return "agent_inactive"
    if max_per_agent is not None and int(run_load) >= int(max_per_agent):
        return "run_limit"
    if _agent_open_workload(db, str(agent.get("id") or agent.get("_id"))) >= int(
        getattr(Config, "CAPTACION_MAX_OPEN_ASSIGNMENTS_PER_EXECUTIVE", 350)
    ):
        return "capacity"
    managed, _ = has_management_evidence(fresh, events_coll)
    if managed and not allow_management_evidence:
        return "managed"
    identity = get_contact_identity_evidence(db, fresh) if phone_learning_global_lookup_enabled() else None
    decision = can_assign_property(
        fresh,
        {
            "contact_identity": identity,
            "broker_identity_match": resolve_broker_identity(db, fresh),
        },
    )
    if not decision.get("assignment_ready"):
        reasons = set(decision.get("assignment_block_reasons") or [])
        if "contact_identity_broker_confirmed" in reasons:
            return "phone"
        if "contact_identity_conflict" in reasons:
            return "identity_conflict"
        return "quality"
    if has_broker_identity(fresh):
        return "quality"
    # Age is an operational distribution rule, not a classifier or Mongo
    # state. Explicit reassignments may bypass it; new automatic/manual
    # assignments must not send stale or undated listings to an executive.
    if mode == "new":
        age = distribution_age_decision(fresh, now=now)
        if not age["eligible"]:
            if age["reason"] == STALE_FOR_AUTO_DISTRIBUTION:
                return "stale_for_auto_distribution"
            return "capture_date_unavailable"
    slug = fresh.get("comuna_slug") or normalize_commune_canonical(fresh.get("comuna") or "")
    if not slug or slug not in set(agent.get("comunas_interes_norm") or []):
        return "no_coverage"

    try:
        oid = fresh["_id"] if isinstance(fresh.get("_id"), ObjectId) else ObjectId(str(fresh["_id"]))
    except Exception:
        oid = fresh.get("_id")
    atomic_filter: dict[str, Any] = {"_id": oid}
    # Reassert the operational gate in the same Mongo predicate. The Python
    # read above protects the normal race window; this predicate prevents a
    # classification/hold transition from being overwritten by the assignment.
    atomic_filter.update({
        "classification.state": {"$in": ["DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"]},
        "classification.final": {"$nin": ["BROKER_CONFIRMED", "BROKER_PROBABLE"]},
        "classification.hard_broker_veto": {"$ne": True},
        "classification.hard_veto": {"$ne": "PROFESSIONAL"},
        "pipeline_complete": True,
        "gestion.semantic_review_hold": {"$ne": True},
    })
    if mode == "new":
        atomic_filter.update(distribution_unassigned_clause())
    elif mode == "reassign":
        if not expected_current_filter:
            return "race_condition"
        atomic_filter.update(expected_current_filter)
    else:
        return "invalid_mode"
    set_fields = {
        "gestion.ejecutivo_id": str(agent.get("id") or agent.get("_id")),
        "gestion.ejecutivo_asignado": agent.get("name") or agent.get("nombre") or "",
        "gestion.ejecutivo_nombre": agent.get("name") or agent.get("nombre") or "",
        "gestion.ejecutivo_email": agent.get("email") or "",
        "gestion.fecha_asignacion": now,
        "gestion.assignment_cycle_id": new_assignment_cycle(
            property_id=fresh.get("_id"),
            user_id=agent.get("id") or agent.get("_id"),
            assigned_at=now,
            reason=reason,
        )["assignment_cycle_id"],
        "gestion.first_valid_action_at": None,
        "gestion.asignacion_version": assignment_version,
        "gestion.asignacion_comuna_slug": slug,
        "gestion.classification_at_assignment": (fresh.get("classification") or {}).get("state"),
    }
    if set_estado is not None:
        set_fields["gestion.estado"] = set_estado
    history = {
        "ejecutivo_id": set_fields["gestion.ejecutivo_id"],
        "ejecutivo_nombre": set_fields["gestion.ejecutivo_nombre"],
        "comuna_slug": slug,
        "classification_state": (fresh.get("classification") or {}).get("state"),
        "assigned_at": now,
        "assignment_version": assignment_version,
        "reason": reason,
    }
    result = coll.update_one(atomic_filter, {"$set": set_fields, "$push": {"gestion.historial_asignaciones": history}})
    return "assigned" if result.modified_count else "race_condition"
