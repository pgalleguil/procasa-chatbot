"""Independent leader lease contract for the prospective SLA worker.

The Mongo adapter is opt-in with ``write_enabled=False`` by default.  The
phase only tests the in-memory model, so no production collection is created
and no lease write is performed during preparation.
"""
from __future__ import annotations

import copy
import inspect
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


LEASE_COLLECTION = "crm_worker_leases"
LEADER_KEY = "crm_sla_reassignment_worker_v1"
LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30
WORKER_INTERVAL_SECONDS = 60


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    parsed = _utc(value)
    return parsed.isoformat() if parsed else None


@dataclass(frozen=True)
class LeaseSettings:
    key: str = LEADER_KEY
    collection: str = LEASE_COLLECTION
    duration_seconds: int = LEASE_SECONDS
    heartbeat_seconds: int = HEARTBEAT_SECONDS
    interval_seconds: int = WORKER_INTERVAL_SECONDS

    def validate(self) -> dict[str, Any]:
        valid = self.duration_seconds > self.heartbeat_seconds and self.heartbeat_seconds < self.interval_seconds <= self.duration_seconds
        return {"valid": valid, "key": self.key, "collection": self.collection, "duration_seconds": self.duration_seconds, "heartbeat_seconds": self.heartbeat_seconds, "interval_seconds": self.interval_seconds, "reason": "heartbeat_and_interval_fit_lease" if valid else "invalid_lease_timing"}


def _lease_doc(*, key: str, holder_id: str, now: datetime, duration_seconds: int, version: int) -> dict[str, Any]:
    return {"_id": key, "key": key, "holder_id": holder_id, "acquired_at": now, "expires_at": now + timedelta(seconds=duration_seconds), "heartbeat_at": now, "version": version}


class InMemoryLeaderLease:
    """Deterministic model for contention, expiry and takeover tests."""

    def __init__(self, *, settings: LeaseSettings = LeaseSettings()):
        self.settings = settings
        self.document: dict[str, Any] | None = None

    def acquire(self, holder_id: str, *, now: Any) -> dict[str, Any]:
        current = _utc(now)
        if not current or not holder_id:
            return {"status": "CONFIG_ERROR", "reason": "holder_or_time_missing"}
        existing = self.document
        if existing and _utc(existing.get("expires_at")) and _utc(existing["expires_at"]) > current and existing.get("holder_id") != holder_id:
            return {"status": "LEASE_NOT_HELD", "reason": "CONTENTION", "holder_id": existing.get("holder_id"), "expires_at": _iso(existing.get("expires_at")), "version": existing.get("version")}
        version = int(existing.get("version") or 0) + 1 if existing else 1
        takeover = bool(existing and existing.get("holder_id") != holder_id and _utc(existing.get("expires_at")) and _utc(existing["expires_at"]) <= current)
        self.document = _lease_doc(key=self.settings.key, holder_id=holder_id, now=current, duration_seconds=self.settings.duration_seconds, version=version)
        return {"status": "ACQUIRED", "takeover": takeover, "lease": copy.deepcopy(self.document)}

    def renew(self, holder_id: str, *, now: Any) -> dict[str, Any]:
        current = _utc(now)
        if not current or not self.document or self.document.get("holder_id") != holder_id or not _utc(self.document.get("expires_at")) or _utc(self.document["expires_at"]) <= current:
            return {"status": "LEASE_NOT_HELD", "reason": "RENEWAL_REJECTED"}
        self.document["heartbeat_at"] = current
        self.document["expires_at"] = current + timedelta(seconds=self.settings.duration_seconds)
        self.document["version"] = int(self.document.get("version") or 0) + 1
        return {"status": "RENEWED", "lease": copy.deepcopy(self.document)}

    def release(self, holder_id: str, *, now: Any) -> dict[str, Any]:
        current = _utc(now)
        if not current or not self.document or self.document.get("holder_id") != holder_id:
            return {"status": "LEASE_NOT_HELD"}
        self.document["expires_at"] = current
        self.document["heartbeat_at"] = current
        self.document["version"] = int(self.document.get("version") or 0) + 1
        return {"status": "RELEASED", "lease": copy.deepcopy(self.document)}

    def status(self, *, now: Any) -> dict[str, Any]:
        current = _utc(now)
        held = bool(self.document and current and _utc(self.document.get("expires_at")) and _utc(self.document["expires_at"]) > current)
        return {"status": "HELD" if held else "EXPIRED_OR_MISSING", "held": held, "lease": copy.deepcopy(self.document)}


async def acquire_leader_lease(db: Any, *, holder_id: str, now: Any = None, settings: LeaseSettings = LeaseSettings(), write_enabled: bool = False) -> dict[str, Any]:
    """Future Mongo adapter.  It is fail-closed unless explicitly enabled."""
    current = _utc(now) or datetime.now(timezone.utc)
    if not write_enabled:
        return {"status": "LEASE_NOT_HELD", "reason": "LEASE_WRITES_DISABLED", "key": settings.key}
    if db is None:
        raise RuntimeError("lease_db_required")
    collection = db[settings.collection]
    query = {"_id": settings.key, "$or": [{"expires_at": {"$lte": current}}, {"holder_id": holder_id}]}
    update = {"$set": {"key": settings.key, "holder_id": holder_id, "acquired_at": current, "expires_at": current + timedelta(seconds=settings.duration_seconds), "heartbeat_at": current}, "$inc": {"version": 1}, "$setOnInsert": {"_id": settings.key}}
    kwargs = {"upsert": True}
    try:
        from pymongo import ReturnDocument
        kwargs["return_document"] = ReturnDocument.AFTER
    except Exception:
        pass
    try:
        result = collection.find_one_and_update(query, update, **kwargs)
        result = await result if inspect.isawaitable(result) else result
    except Exception as exc:
        if exc.__class__.__name__ == "DuplicateKeyError":
            return {"status": "LEASE_NOT_HELD", "reason": "CONTENTION", "key": settings.key}
        raise
    if not result or result.get("holder_id") != holder_id:
        return {"status": "LEASE_NOT_HELD", "reason": "CONTENTION", "key": settings.key}
    return {"status": "ACQUIRED", "takeover": False, "lease": result}


async def renew_leader_lease(
    db: Any,
    *,
    holder_id: str,
    now: Any = None,
    settings: LeaseSettings = LeaseSettings(),
    write_enabled: bool = False,
) -> dict[str, Any]:
    """Renew the global lease only while the same holder still owns it."""
    current = _utc(now) or datetime.now(timezone.utc)
    if not write_enabled:
        return {"status": "LEASE_NOT_HELD", "reason": "LEASE_WRITES_DISABLED", "key": settings.key}
    if db is None:
        raise RuntimeError("lease_db_required")
    collection = db[settings.collection]
    query = {"_id": settings.key, "holder_id": holder_id, "expires_at": {"$gt": current}}
    update = {"$set": {"heartbeat_at": current, "expires_at": current + timedelta(seconds=settings.duration_seconds)}, "$inc": {"version": 1}}
    kwargs = {}
    try:
        from pymongo import ReturnDocument
        kwargs["return_document"] = ReturnDocument.AFTER
    except Exception:
        pass
    result = collection.find_one_and_update(query, update, **kwargs)
    result = await result if inspect.isawaitable(result) else result
    if not result or result.get("holder_id") != holder_id:
        return {"status": "LEASE_NOT_HELD", "reason": "RENEWAL_REJECTED", "key": settings.key}
    return {"status": "RENEWED", "lease": result}


def lease_strategy() -> dict[str, Any]:
    return {"strategy": "GLOBAL_LEADER_LEASE", "key": LEADER_KEY, "collection": LEASE_COLLECTION, "duration_seconds": LEASE_SECONDS, "heartbeat_seconds": HEARTBEAT_SECONDS, "worker_interval_seconds": WORKER_INTERVAL_SECONDS, "coordination_role": "prevents_normal_double_evaluation", "correctness_role": "executor_CAS_and_transaction_ledger_remain_final_authority", "per_cycle_lease": "not_selected_initially_due_to_low_moderate_volume"}


def interval_operational_metrics(*, interval_seconds: int, base_read_operations: int = 9, live_refresh_operations: int = 5, historical_refresh_operations: int = 5, live_refresh_seconds: int = 60, historical_refresh_seconds: int = 300) -> dict[str, Any]:
    """Estimate nominal lag and read operations without contacting Mongo."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    iterations_hour = 3600 / interval_seconds
    reads_hour = (
        base_read_operations * iterations_hour
        + live_refresh_operations * (3600 / live_refresh_seconds)
        + historical_refresh_operations * (3600 / historical_refresh_seconds)
    )
    return {"interval_seconds": interval_seconds, "expected_detection_lag_seconds": interval_seconds / 2, "max_nominal_detection_lag_seconds": interval_seconds, "iterations_per_hour": iterations_hour, "estimated_read_operations_per_hour": reads_hour}
