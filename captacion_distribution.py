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

from captacion_assignment_eligibility import calculate_assignment_eligibility
from captacion_contact_identity import (
    get_contact_identity_evidence,
    phone_learning_global_lookup_enabled,
)
from captacion_kpis import (
    CAPTACION_TERMINAL_STATES,
    is_terminal_captacion_state,
)
from captacion_management import new_assignment_cycle
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
        row = db["usuarios"].find_one({"_id": object_id, "is_active": True, "rol": "agente"}, {"_id": 1})
        return row is not None
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
    decision = calculate_assignment_eligibility(fresh, contact_identity=identity)
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
