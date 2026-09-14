"""Durable, prospective reconciliation for human-confirmed broker phones.

This module deliberately does not touch the 88 historical ``HUMAN_BACKFILL``
identities.  A job can only be created from the explicit trigger marker that
``captacion_contact_identity`` writes when a new human broker confirmation
transitions an identity to ``CORREDOR_CONFIRMED``.

The worker uses exact normalized-phone equality and writes a non-crediting
management event for every matched property.  The event is the idempotency
ledger; it is never the normal ``known_broker_auto_match`` credit event.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from bson import ObjectId
from pymongo import ReturnDocument

from config import Config


JOB_COLLECTION = "captacion_phone_reconciliation_jobs"
EVENT_COLLECTION = "captacion_management_events"
PROPERTY_COLLECTION = Config.CAPTACION_COLLECTION_NAME
EVENT_TYPE = "known_broker_phone_propagation"
JOB_VERSION = "phone-reconciliation-v1"
TRIGGER_SOURCE = "HUMAN_FEEDBACK"
SYSTEM_ACTOR_ID = "SYSTEM"
SYSTEM_ACTOR_NAME = "SYSTEM"

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_RETRYABLE = "retryable"
STATUS_FAILED = "failed"
STATUS_CANCELLED_CONFLICT = "cancelled_conflict"

TERMINAL_STATES = {
    "captado", "captada", "captured", "cerrado", "cerrada", "closed",
    "corredor", "teléfono inválido", "telefono invalido", "numero invalido",
    "descartado", "duplicado", "propiedad no disponible",
    "publicación expirada", "publicacion expirada", "no interesado",
    "removed", "ad_removed",
}
OWNER_RESULTS = {"captured", "captado", "captada"}
HUMAN_MANAGEMENT_EVENT_TYPES = {
    "management_confirmed",
    "manual_decision_confirmed",
    "capture_confirmed",
}
PORTAL_VALUES = (
    "chilepropiedades",
    "chilepropiedades.cl",
    "yapo",
    "toctoc",
)
PORTAL_FIELDS = (
    "origen",
    "source_portal",
    "portal",
    "details.origen",
    "details.source_portal",
    "details.portal",
)
PHONE_FIELDS = (
    "phone_normalized",
    "telefono_normalizado",
    "details.phone_normalized",
    "details.telefono_normalizado",
    "details.whatsapp_phone",
    "details.contact_phone",
    "details.telefono",
)

logger = logging.getLogger(__name__)
_INDEXES_READY = False
_INDEX_LOCK = threading.Lock()


def reconciliation_enabled() -> bool:
    return bool(
        getattr(Config, "PHONE_RECONCILIATION_ENABLED", True)
        and getattr(Config, "PHONE_LEARNING_ENABLED", False)
    )


def _utc(value: Any = None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _clean_id(value: Any) -> str:
    return str(value or "").strip()


def _norm_state(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _first(document: dict[str, Any], paths: Iterable[str], default: Any = "") -> Any:
    for path in paths:
        current: Any = document
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if current not in (None, ""):
            return current
    return default


def _portal(document: dict[str, Any]) -> str:
    value = str(_first(document, PORTAL_FIELDS, "")).strip().lower()
    if value == "chilepropiedades.cl":
        return "chilepropiedades"
    if "chilepropiedades" in value:
        return "chilepropiedades"
    if "yapo" in value:
        return "yapo"
    if "toctoc" in value:
        return "toctoc"
    return value or "unknown"


def _property_id(document: dict[str, Any]) -> str:
    return _clean_id(
        document.get("_id")
        or document.get("listing_id")
        or document.get("codigo")
        or document.get("url")
    )


def _property_id_values(document: dict[str, Any]) -> list[Any]:
    values: list[Any] = []
    raw = document.get("_id")
    if raw not in (None, ""):
        values.append(raw)
        values.append(_clean_id(raw))
        if isinstance(raw, str):
            try:
                values.append(ObjectId(raw))
            except Exception:
                pass
    for key in ("listing_id", "codigo", "url"):
        value = document.get(key)
        if value not in (None, ""):
            values.append(value)
            values.append(_clean_id(value))
    return list(dict.fromkeys(values))


def _assigned_context(document: dict[str, Any]) -> dict[str, str]:
    gestion = document.get("gestion") or {}
    assigned_name = str(
        gestion.get("ejecutivo_nombre")
        or gestion.get("ejecutivo_asignado")
        or ""
    ).strip()
    if assigned_name.casefold() in {"sin asignar", "sin asignado", "unassigned", "none", "null"}:
        assigned_name = ""
    return {
        "assigned_executive_id": _clean_id(
            gestion.get("ejecutivo_id")
            or gestion.get("assigned_to")
            or gestion.get("assigned_to_id")
        ),
        "assigned_executive_name": assigned_name,
    }


def _is_assigned(document: dict[str, Any]) -> bool:
    context = _assigned_context(document)
    return bool(context["assigned_executive_id"] or context["assigned_executive_name"])


def _operational_state(document: dict[str, Any]) -> str:
    gestion = document.get("gestion") or {}
    return str(
        gestion.get("estado_captacion")
        or gestion.get("estado")
        or document.get("estado")
        or ""
    ).strip()


def _management_event_query(document: dict[str, Any]) -> dict[str, Any]:
    values = _property_id_values(document)
    if not values:
        return {"_id": None}
    return {"property_id": {"$in": values}}


def _human_management_events(db, document: dict[str, Any]) -> list[dict[str, Any]]:
    rows = db[EVENT_COLLECTION].find(
        _management_event_query(document),
        {
            "_id": 1,
            "event_id": 1,
            "event_type": 1,
            "result": 1,
            "commercial_result": 1,
            "commercially_valid": 1,
            "credited": 1,
            "actor_user_id": 1,
            "actor_name_snapshot": 1,
            "actor_role": 1,
        },
    )
    valid: list[dict[str, Any]] = []
    for row in rows:
        if row.get("event_type") not in HUMAN_MANAGEMENT_EVENT_TYPES:
            continue
        if row.get("commercially_valid") is not True:
            continue
        if row.get("credited") is False:
            continue
        if not _clean_id(row.get("actor_user_id")):
            continue
        valid.append(row)
    return valid


def _identity_query(identity_id: str) -> list[dict[str, Any]]:
    values: list[Any] = [identity_id]
    try:
        values.append(ObjectId(identity_id))
    except Exception:
        pass
    return [{"_id": value} for value in values]


def _find_identity(db, identity_id: str) -> dict[str, Any] | None:
    for query in _identity_query(identity_id):
        row = db["captacion_contact_identity"].find_one(query)
        if row:
            return row
    return None


def _initial_metrics() -> dict[str, Any]:
    return {
        "properties_scanned": 0,
        "properties_matched": 0,
        "portals_matched": 0,
        "portal_counts": {},
        "unassigned_blocked": 0,
        "assigned_unworked_blocked": 0,
        "previously_managed_audited": 0,
        "terminal_skipped": 0,
        "already_corredor": 0,
        "conflicts": 0,
        "conflict_skipped": 0,
        "errors": 0,
        "broker_reuse_prevented_pre_assignment": 0,
        "broker_reuse_prevented_by_reconciliation": 0,
        "duration_ms": 0,
    }


def ensure_phone_reconciliation_indexes(db) -> bool:
    """Create only scoped indexes; all are safe to run repeatedly."""
    global _INDEXES_READY
    if _INDEXES_READY:
        return True
    with _INDEX_LOCK:
        if _INDEXES_READY:
            return True
        try:
            jobs = db[JOB_COLLECTION]
            jobs.create_index(
                [("identity_id", 1), ("human_feedback_event_id", 1)],
                unique=True,
                name="phone_reconciliation_identity_event",
            )
            jobs.create_index(
                [("status", 1), ("lease_expires_at", 1), ("created_at", 1)],
                name="phone_reconciliation_claim",
            )
            jobs.create_index("job_key", unique=True, name="phone_reconciliation_job_key")
            db[EVENT_COLLECTION].create_index(
                [("reconciliation_dedup_key", 1)],
                unique=True,
                sparse=True,
                name="phone_reconciliation_event_dedup",
            )
            properties = db[PROPERTY_COLLECTION]
            properties.create_index("phone_normalized", name="captacion_phone_normalized")
            properties.create_index("telefono_normalizado", name="captacion_telefono_normalizado")
            _INDEXES_READY = True
            return True
        except Exception:
            logger.exception("[CP_PHONE_RECONCILIATION] index_setup_failed")
            return False


def enqueue_phone_reconciliation_job(
    db,
    *,
    identity: dict[str, Any],
    human_feedback_event_id: str,
    now: Any = None,
) -> dict[str, Any] | None:
    """Insert one prospective job; historical backfill can never enter here."""
    if not reconciliation_enabled():
        return None
    event_id = _clean_id(human_feedback_event_id)
    identity_id = _clean_id(identity.get("_id") or identity.get("identity_key"))
    phone = _clean_id(identity.get("phone_normalized"))
    if not event_id or not identity_id or not phone:
        return None
    if str(identity.get("status") or "").upper() != "CORREDOR_CONFIRMED":
        return None
    if str(identity.get("classification") or "").upper() == "CONFLICT":
        return None
    job_key = f"{identity_id}:{event_id}"
    created_at = _utc(now)
    document = {
        "job_key": job_key,
        "identity_id": identity_id,
        "phone_normalized": phone,
        "human_feedback_event_id": event_id,
        "trigger_source": TRIGGER_SOURCE,
        "schema_version": JOB_VERSION,
        "status": STATUS_PENDING,
        "created_at": created_at,
        "started_at": None,
        "finished_at": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "attempts": 0,
        "last_error": None,
        "metrics": _initial_metrics(),
    }
    ensure_phone_reconciliation_indexes(db)
    jobs = db[JOB_COLLECTION]
    jobs.update_one(
        {"identity_id": identity_id, "human_feedback_event_id": event_id},
        {"$setOnInsert": document},
        upsert=True,
    )
    return jobs.find_one(
        {"identity_id": identity_id, "human_feedback_event_id": event_id},
        {"phone_normalized": 0},
    )


def recover_missing_phone_reconciliation_jobs(db, *, limit: int = 25) -> int:
    """Recover only explicit prospective transition markers.

    The marker is absent from all ``HUMAN_BACKFILL`` identities, so this is
    not a historical backfill mechanism.
    """
    if not reconciliation_enabled():
        return 0
    created = 0
    cursor = db["captacion_contact_identity"].find(
        {
            "status": "CORREDOR_CONFIRMED",
            "$or": [
                {"phone_reconciliation_trigger_event_id": {"$exists": True, "$nin": [None, ""]}},
                {"evidence": {"$elemMatch": {"source": TRIGGER_SOURCE, "classification": "CORREDOR"}}},
            ],
        },
        {
            "_id": 1,
            "phone_normalized": 1,
            "status": 1,
            "classification": 1,
            "phone_reconciliation_trigger_event_id": 1,
            "evidence": 1,
        },
    ).limit(max(1, int(limit)))
    for identity in cursor:
        trigger_event_id = _clean_id(identity.get("phone_reconciliation_trigger_event_id"))
        if not trigger_event_id:
            # Recovery for a crash before the marker write.  The first human
            # broker confirmation is the only eligible transition.  A prior
            # HUMAN_BACKFILL or HUMAN_FEEDBACK broker confirmation proves the
            # identity was already confirmed and therefore must not be opened.
            evidence = identity.get("evidence") or []
            for index, item in enumerate(evidence):
                if not isinstance(item, dict):
                    continue
                if (
                    str(item.get("source") or "").upper() == TRIGGER_SOURCE
                    and str(item.get("classification") or "").upper() == "CORREDOR"
                ):
                    prior_broker = any(
                        str(previous.get("classification") or "").upper() == "CORREDOR"
                        and str(previous.get("source") or "").upper()
                        in {"HUMAN_BACKFILL", TRIGGER_SOURCE}
                        for previous in evidence[:index]
                        if isinstance(previous, dict)
                    )
                    if not prior_broker:
                        trigger_event_id = _clean_id(item.get("event_id"))
                    break
            if trigger_event_id:
                marker = db["captacion_contact_identity"].update_one(
                    {
                        "_id": identity.get("_id"),
                        "phone_reconciliation_trigger_event_id": {"$exists": False},
                    },
                    {
                        "$set": {
                            "phone_reconciliation_trigger_event_id": trigger_event_id,
                            "phone_reconciliation_triggered_at": datetime.now(timezone.utc),
                        }
                    },
                )
                if not getattr(marker, "matched_count", 1):
                    refreshed = db["captacion_contact_identity"].find_one({"_id": identity.get("_id")}) or {}
                    trigger_event_id = _clean_id(refreshed.get("phone_reconciliation_trigger_event_id"))
        if not trigger_event_id:
            continue
        existing_job = db[JOB_COLLECTION].find_one(
            {
                "identity_id": _clean_id(identity.get("_id") or identity.get("identity_key")),
                "human_feedback_event_id": trigger_event_id,
            },
            {"_id": 1},
        )
        job = enqueue_phone_reconciliation_job(
            db,
            identity=identity,
            human_feedback_event_id=trigger_event_id,
        )
        if not existing_job and job and int(job.get("attempts") or 0) == 0 and job.get("status") == STATUS_PENDING:
            created += 1
    return created


def _claim_next_job(db, *, worker_id: str, now: datetime | None = None) -> dict[str, Any] | None:
    now = _utc(now)
    stale_before = now
    jobs = db[JOB_COLLECTION]
    return jobs.find_one_and_update(
        {
            "attempts": {"$lt": max(1, int(getattr(Config, "PHONE_RECONCILIATION_MAX_ATTEMPTS", 5)))},
            "$or": [
                {"status": {"$in": [STATUS_PENDING, STATUS_RETRYABLE]}},
                {"status": STATUS_PROCESSING, "lease_expires_at": {"$lte": stale_before}},
            ],
        },
        {
            "$set": {
                "status": STATUS_PROCESSING,
                "lease_owner": worker_id,
                "lease_expires_at": now + timedelta(seconds=max(10, int(getattr(Config, "PHONE_RECONCILIATION_LEASE_SECONDS", 120)))),
                "last_started_at": now,
                "last_error": None,
            },
            "$inc": {"attempts": 1},
        },
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


def _finish_job(db, job: dict[str, Any], *, status: str, metrics: dict[str, Any], error: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    update: dict[str, Any] = {
        "$set": {
            "status": status,
            "metrics": metrics,
            "last_error": error,
            "lease_owner": None,
            "lease_expires_at": None,
        },
        "$unset": {"last_started_at": ""},
    }
    if status in {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED_CONFLICT}:
        update["$set"]["finished_at"] = now
    db[JOB_COLLECTION].update_one(
        {"_id": job.get("_id"), "lease_owner": job.get("lease_owner")},
        update,
    )


def _match_query(phone: str) -> dict[str, Any]:
    return {
        "$and": [
            {"$or": [{field: {"$in": list(PORTAL_VALUES)}} for field in PORTAL_FIELDS]},
            {"$or": [{field: phone} for field in PHONE_FIELDS]},
        ]
    }


def _event_id(dedup_key: str) -> str:
    digest = hashlib.sha256(dedup_key.encode("utf-8")).hexdigest()[:32]
    return f"phone-propagation:{digest}"


def _write_propagation_event(
    db,
    *,
    job: dict[str, Any],
    document: dict[str, Any],
    action: str,
    previous_state: str,
    resulting_state: str,
    assigned: dict[str, str],
    conflict: bool = False,
) -> dict[str, Any]:
    property_id = _property_id(document)
    dedup_key = f"{job.get('identity_id')}:{job.get('human_feedback_event_id')}:{property_id}"
    event = {
        "event_id": _event_id(dedup_key),
        "event_type": EVENT_TYPE,
        "reconciliation_dedup_key": dedup_key,
        "reconciliation_job_id": _clean_id(job.get("_id") or job.get("job_key")),
        "identity_id": _clean_id(job.get("identity_id")),
        "source_human_feedback_event_id": _clean_id(job.get("human_feedback_event_id")),
        "property_id": property_id,
        "listing_id": _clean_id(document.get("listing_id")),
        "portal": _portal(document),
        "previous_operational_state": previous_state,
        "resulting_operational_state": resulting_state,
        "action": action,
        "reuse_prevention_reason": (
            "BROKER_REUSE_PREVENTED_PRE_ASSIGNMENT"
            if action == "unassigned_blocked"
            else "BROKER_REUSE_PREVENTED_BY_RECONCILIATION"
            if action == "assigned_unworked_blocked"
            else None
        ),
        "conflict": bool(conflict),
        "credited": False,
        "commercially_valid": False,
        "contact_attempt": False,
        "contact_effective": False,
        "actor_user_id": SYSTEM_ACTOR_ID,
        "actor_name_snapshot": SYSTEM_ACTOR_NAME,
        "actor_role": "SYSTEM",
        **assigned,
        "source_system": "captacion_phone_reconciliation",
        "occurred_at": datetime.now(timezone.utc),
    }
    db[EVENT_COLLECTION].update_one(
        {"reconciliation_dedup_key": dedup_key},
        {"$setOnInsert": event},
        upsert=True,
    )
    return event


def _reconcile_property(db, *, job: dict[str, Any], document: dict[str, Any], identity: dict[str, Any]) -> str:
    property_id = _property_id(document)
    if not property_id:
        return "error"
    assigned = _assigned_context(document)
    current_state = _operational_state(document)
    normalized_state = _norm_state(current_state)
    human_events = _human_management_events(db, document)
    managed = bool(human_events)
    owner_result = any(
        str(row.get("commercial_result") or row.get("result") or "").strip().lower() in OWNER_RESULTS
        for row in human_events
    )
    identity_conflict = str(identity.get("status") or "").upper() == "CONFLICT"
    property_conflict = owner_result or identity_conflict
    terminal = normalized_state in TERMINAL_STATES
    already_corredor = normalized_state == "corredor" or bool((document.get("gestion") or {}).get("known_broker_auto_match"))
    dedup_key = f"{job.get('identity_id')}:{job.get('human_feedback_event_id')}:{property_id}"
    property_collection = db[PROPERTY_COLLECTION]

    if property_conflict:
        _write_propagation_event(
            db, job=job, document=document, action="conflict_skipped",
            previous_state=current_state, resulting_state=current_state,
            assigned=assigned, conflict=True,
        )
        return "conflict"
    if terminal:
        _write_propagation_event(
            db, job=job, document=document, action="terminal_audit_only",
            previous_state=current_state, resulting_state=current_state,
            assigned=assigned,
        )
        return "already_corredor" if already_corredor else "terminal"
    if managed:
        _write_propagation_event(
            db, job=job, document=document, action="managed_audit_only",
            previous_state=current_state, resulting_state=current_state,
            assigned=assigned,
        )
        return "managed"

    # A property may be textually "En gestión" yet have no valid human
    # management event.  It therefore belongs to the unworked branch.
    now = datetime.now(timezone.utc)
    status_history = {
        "timestamp": now,
        "from_state": current_state,
        "to_state": "Corredor",
        "user": SYSTEM_ACTOR_NAME,
        "source": "KNOWN_BROKER_PHONE_PROPAGATION",
        "reconciliation_job_id": _clean_id(job.get("_id") or job.get("job_key")),
    }
    update = {
        "$set": {
            "gestion.estado": "Corredor",
            "gestion.estado_captacion": "Corredor",
            "gestion.phone_learning_blocked": True,
            "gestion.phone_learning_block_reason": "KNOWN_BROKER_PHONE_PROPAGATION",
            "gestion.phone_learning_reconciliation_job_id": _clean_id(job.get("_id") or job.get("job_key")),
            "gestion.phone_learning_reconciliation_event_id": _event_id(dedup_key),
        },
        "$addToSet": {"gestion.phone_learning_reconciliation_keys": dedup_key},
        "$push": {"gestion.status_history": status_history},
    }
    write = property_collection.update_one(
        {"_id": document.get("_id"), "gestion.phone_learning_reconciliation_keys": {"$ne": dedup_key}},
        update,
    )
    _write_propagation_event(
        db, job=job, document=document, action="assigned_unworked_blocked" if _is_assigned(document) else "unassigned_blocked",
        previous_state=current_state, resulting_state="Corredor", assigned=assigned,
    )
    if _is_assigned(document):
        return "assigned_unworked_blocked" if getattr(write, "matched_count", 1) else "idempotent_skip"
    return "unassigned_blocked" if getattr(write, "matched_count", 1) else "idempotent_skip"


def _metrics_from_events(db, job: dict[str, Any], started_at: float) -> dict[str, Any]:
    job_id = _clean_id(job.get("_id") or job.get("job_key"))
    rows = list(db[EVENT_COLLECTION].find(
        {"event_type": EVENT_TYPE, "reconciliation_job_id": job_id},
        {
            "property_id": 1,
            "portal": 1,
            "action": 1,
            "conflict": 1,
            "reuse_prevention_reason": 1,
            "resulting_operational_state": 1,
        },
    ))
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique[_clean_id(row.get("property_id"))] = row
    metrics = _initial_metrics()
    metrics["properties_scanned"] = len(unique)
    metrics["properties_matched"] = len(unique)
    portals = {str(row.get("portal") or "unknown") for row in unique.values()}
    metrics["portals_matched"] = len(portals)
    portal_counts: dict[str, int] = {}
    for row in unique.values():
        portal = str(row.get("portal") or "unknown")
        portal_counts[portal] = portal_counts.get(portal, 0) + 1
        action = row.get("action")
        if action == "unassigned_blocked":
            metrics["unassigned_blocked"] += 1
        elif action == "assigned_unworked_blocked":
            metrics["assigned_unworked_blocked"] += 1
        elif action == "managed_audit_only":
            metrics["previously_managed_audited"] += 1
        elif action == "terminal_audit_only":
            metrics["terminal_skipped"] += 1
        elif action == "conflict_skipped":
            metrics["conflicts"] += 1
            metrics["conflict_skipped"] += 1
        if row.get("reuse_prevention_reason") == "BROKER_REUSE_PREVENTED_PRE_ASSIGNMENT":
            metrics["broker_reuse_prevented_pre_assignment"] += 1
        elif row.get("reuse_prevention_reason") == "BROKER_REUSE_PREVENTED_BY_RECONCILIATION":
            metrics["broker_reuse_prevented_by_reconciliation"] += 1
        if action == "terminal_audit_only" and str(row.get("resulting_operational_state") or "").casefold() == "corredor":
            metrics["already_corredor"] += 1
    metrics["portal_counts"] = portal_counts
    metrics["duration_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
    return metrics


def process_phone_reconciliation_job(db, job: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    identity = _find_identity(db, _clean_id(job.get("identity_id")))
    if not identity:
        metrics = _initial_metrics()
        metrics["errors"] = 1
        _finish_job(db, job, status=STATUS_FAILED, metrics=metrics, error="identity_not_found")
        return {"status": STATUS_FAILED, "reason": "identity_not_found", "metrics": metrics}
    identity_status = str(identity.get("status") or "").upper()
    if identity_status == "CONFLICT":
        metrics = _initial_metrics()
        metrics["conflicts"] = 1
        metrics["conflict_skipped"] = 1
        _finish_job(db, job, status=STATUS_CANCELLED_CONFLICT, metrics=metrics, error=None)
        return {"status": STATUS_CANCELLED_CONFLICT, "metrics": metrics}
    if identity_status != "CORREDOR_CONFIRMED":
        metrics = _initial_metrics()
        metrics["errors"] = 1
        _finish_job(db, job, status=STATUS_FAILED, metrics=metrics, error="identity_status_changed")
        return {"status": STATUS_FAILED, "reason": "identity_status_changed", "metrics": metrics}
    phone = _clean_id(job.get("phone_normalized") or identity.get("phone_normalized"))
    if not phone:
        metrics = _initial_metrics()
        metrics["errors"] = 1
        _finish_job(db, job, status=STATUS_FAILED, metrics=metrics, error="phone_missing")
        return {"status": STATUS_FAILED, "reason": "phone_missing", "metrics": metrics}

    ensure_phone_reconciliation_indexes(db)
    query = _match_query(phone)
    projection = {
        "_id": 1, "listing_id": 1, "codigo": 1, "origen": 1, "source_portal": 1, "portal": 1,
        "details.origen": 1, "details.source_portal": 1, "details.portal": 1,
        "gestion": 1, "estado": 1, "classification": 1, "clasificacion": 1,
    }
    try:
        for document in db[PROPERTY_COLLECTION].find(query, projection).batch_size(
            max(1, int(getattr(Config, "PHONE_RECONCILIATION_PAGE_SIZE", 100)))
        ):
            try:
                _reconcile_property(db, job=job, document=document, identity=identity)
            except Exception:
                logger.exception(
                    "[CP_PHONE_RECONCILIATION] property_failed job=%s",
                    _clean_id(job.get("_id") or job.get("job_key")),
                )
        metrics = _metrics_from_events(db, job, started)
        _finish_job(db, job, status=STATUS_COMPLETED, metrics=metrics, error=None)
        return {"status": STATUS_COMPLETED, "metrics": metrics}
    except Exception as exc:
        metrics = _metrics_from_events(db, job, started)
        metrics["errors"] = int(metrics.get("errors") or 0) + 1
        attempts = int(job.get("attempts") or 0)
        max_attempts = max(1, int(getattr(Config, "PHONE_RECONCILIATION_MAX_ATTEMPTS", 5)))
        status = STATUS_FAILED if attempts >= max_attempts else STATUS_RETRYABLE
        _finish_job(db, job, status=status, metrics=metrics, error=type(exc).__name__)
        return {"status": status, "reason": type(exc).__name__, "metrics": metrics}


def run_phone_reconciliation_iteration(db, *, worker_id: str | None = None) -> dict[str, Any]:
    if not reconciliation_enabled():
        return {"status": "disabled", "recovered": 0, "processed": False}
    worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    ensure_phone_reconciliation_indexes(db)
    recovered = recover_missing_phone_reconciliation_jobs(db)
    job = _claim_next_job(db, worker_id=worker_id)
    if not job:
        return {"status": "idle", "recovered": recovered, "processed": False}
    result = process_phone_reconciliation_job(db, job)
    result.update({"recovered": recovered, "processed": True, "job_id": _clean_id(job.get("_id") or job.get("job_key"))})
    return result


async def phone_reconciliation_worker_loop(status: dict[str, Any] | None = None) -> None:
    """Run one bounded Mongo-backed iteration and remain restart-safe."""
    from chatbot.storage import get_db

    status = status if status is not None else {}
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    interval = max(1, int(getattr(Config, "PHONE_RECONCILIATION_WORKER_INTERVAL_SECONDS", 3)))
    loop = asyncio.get_running_loop()
    while True:
        try:
            status.update({"status": "running", "worker_id": worker_id, "last_heartbeat": datetime.now(timezone.utc).isoformat()})
            result = await loop.run_in_executor(None, lambda: run_phone_reconciliation_iteration(get_db(), worker_id=worker_id))
            status["last_result"] = result.get("status")
            status["last_metrics"] = result.get("metrics")
        except asyncio.CancelledError:
            status["status"] = "stopped"
            raise
        except Exception as exc:
            status.update({"status": "error", "last_error": type(exc).__name__})
            logger.exception("[CP_PHONE_RECONCILIATION] worker_iteration_failed")
        await asyncio.sleep(interval)
