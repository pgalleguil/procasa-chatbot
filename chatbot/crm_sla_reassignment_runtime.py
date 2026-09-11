"""Controlled production runtime for the CRM SLA reassignment shadow worker.

This module is the only startup bridge for the prospective worker.  It keeps
the business collections read-only through an explicit Mongo adapter, gates
execution on the global lease, and records operational health in memory for
the existing application health endpoint.  It never imports the transaction
executor and never sends notifications.
"""
from __future__ import annotations

import asyncio
import logging
import os
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from config import Config

from .crm_sla_reassignment_cutover import parse_explicit_santiago_timestamp
from .crm_sla_reassignment_shadow import SHADOW_COLLECTION, SHADOW_POLICY_VERSION
from .crm_sla_reassignment_worker import (
    _default_performance_snapshot,
    run_sla_reassignment_iteration_with_leader_lease,
    validate_worker_configuration,
)
from .crm_sla_worker_lease import LeaseSettings, renew_leader_lease
from .storage import get_async_db, get_db


logger = logging.getLogger(__name__)
LEASE_COLLECTION = "crm_worker_leases"
WRITE_METHODS = frozenset({
    "insert_one", "insert_many", "update_one", "update_many", "replace_one",
    "delete_one", "delete_many", "find_one_and_update", "find_one_and_replace",
    "find_one_and_delete", "bulk_write", "create_index", "drop_index",
    "drop_indexes", "drop", "rename",
})
ALLOWED_WRITE_COLLECTIONS = frozenset({SHADOW_COLLECTION, LEASE_COLLECTION})
WORKER_HEALTHY_SECONDS = 5.0
WORKER_DEGRADED_SECONDS = 15.0


class ShadowWriteGuardViolation(RuntimeError):
    """Raised when shadow runtime attempts a business-data write."""


class _ShadowGuardCollection:
    def __init__(self, collection: Any, *, name: str):
        self._collection = collection
        self.name = name

    def __getattr__(self, operation: str) -> Any:
        attribute = getattr(self._collection, operation)
        if operation not in WRITE_METHODS:
            return attribute
        if self.name not in ALLOWED_WRITE_COLLECTIONS:
            def blocked(*_args: Any, **_kwargs: Any) -> Any:
                logger.critical(
                    "[SLA_SHADOW_WRITE_GUARD_VIOLATION] collection=%s operation=%s",
                    self.name, operation,
                )
                raise ShadowWriteGuardViolation(f"shadow_write_blocked:{self.name}:{operation}")
            return blocked
        return attribute


class ShadowWriteGuardDB:
    """Forward reads and lease/shadow writes; reject every other write."""

    def __init__(self, db: Any):
        self._db = db

    def __getitem__(self, name: str) -> _ShadowGuardCollection:
        return _ShadowGuardCollection(self._db[name], name=str(name))

    def __getattr__(self, name: str) -> Any:
        if name == "create_collection":
            def blocked(*_args: Any, **_kwargs: Any) -> Any:
                logger.critical("[SLA_SHADOW_WRITE_GUARD_VIOLATION] database_operation=create_collection")
                raise ShadowWriteGuardViolation("shadow_write_blocked:database:create_collection")
            return blocked
        return getattr(self._db, name)


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


def _instance_id() -> str:
    value = str(os.getenv("RENDER_INSTANCE_ID") or os.getenv("HOSTNAME") or f"pid-{os.getpid()}")
    return value[:120]


def _pctl(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 3)


@dataclass
class ShadowRuntimeMetrics:
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    durations_ms: list[float] = field(default_factory=list)
    iterations: int = 0
    scanned: int = 0
    pre_shadow_skipped: int = 0
    post_shadow_breaches: int = 0
    would_execute: int = 0
    shadow_writes: int = 0
    errors: int = 0
    critical_consecutive: int = 0
    last_result: dict[str, Any] = field(default_factory=dict)

    def record(self, result: Mapping[str, Any]) -> dict[str, Any]:
        iteration = dict(result.get("iteration") or {})
        duration = float(iteration.get("duration_ms") or 0.0)
        self.iterations += 1
        self.durations_ms.append(duration)
        self.durations_ms = self.durations_ms[-30:]
        self.scanned += int(iteration.get("scanned") or 0)
        self.pre_shadow_skipped += int(iteration.get("pre_cutover_skipped") or 0)
        self.post_shadow_breaches += int(iteration.get("canonically_expired") or 0) - int(iteration.get("pre_cutover_skipped") or 0)
        self.would_execute += int(result.get("new_shadow_would_execute") or 0)
        self.shadow_writes += int(result.get("shadow_storage_writes") or 0)
        self.errors += int(iteration.get("errors") or 0)
        if duration > WORKER_DEGRADED_SECONDS * 1000:
            self.critical_consecutive += 1
        else:
            self.critical_consecutive = 0
        self.last_result = dict(result)
        return {
            "iterations": self.iterations,
            "scanned": self.scanned,
            "pre_shadow_skipped": self.pre_shadow_skipped,
            "post_shadow_breaches": self.post_shadow_breaches,
            "would_execute": self.would_execute,
            "shadow_writes": self.shadow_writes,
            "errors": self.errors,
            "iteration_p50_ms": _pctl(self.durations_ms, 0.50),
            "iteration_p90_ms": _pctl(self.durations_ms, 0.90),
            "iteration_max_ms": max(self.durations_ms) if self.durations_ms else None,
        }


def _health_for_duration(duration_ms: float, *, critical_consecutive: int) -> tuple[str, str]:
    if critical_consecutive >= 3:
        return "SHADOW_ERROR", "STOP_NEW_EVALUATIONS"
    seconds = duration_ms / 1000.0
    if seconds < WORKER_HEALTHY_SECONDS:
        return "SHADOW_HEALTHY", "CONTINUE"
    if seconds <= WORKER_DEGRADED_SECONDS:
        return "SHADOW_DEGRADED", "CONTINUE"
    return "SHADOW_CRITICAL", "CONTINUE"


def _set_status(status: Mapping[str, Any], **values: Any) -> None:
    if hasattr(status, "update"):
        status.update(values)


async def _lease_heartbeat(
    db: ShadowWriteGuardDB,
    *,
    holder_id: str,
    settings: LeaseSettings,
    stop_event: asyncio.Event,
    status: Mapping[str, Any],
) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.heartbeat_seconds)
            return
        except asyncio.TimeoutError:
            try:
                renewed = await renew_leader_lease(
                    db, holder_id=holder_id, settings=settings, write_enabled=True,
                )
                _set_status(status, last_lease_heartbeat=datetime.now(timezone.utc).isoformat(), lease=renewed)
                if renewed.get("status") != "RENEWED":
                    logger.warning("[SLA_SHADOW_SKIPPED] reason=LEASE_HEARTBEAT_NOT_HELD")
            except Exception as exc:
                logger.error("[SLA_SHADOW_ERROR] reason=LEASE_HEARTBEAT_ERROR error_type=%s", type(exc).__name__)
                _set_status(status, health="SHADOW_STALE", last_error=type(exc).__name__)


async def run_crm_sla_shadow_worker(
    *,
    stop_event: asyncio.Event | None = None,
    status: Mapping[str, Any] | None = None,
) -> None:
    """Run a guarded, lease-coordinated, shadow-only prospective worker."""
    stop_event = stop_event or asyncio.Event()
    status = status if status is not None else {}
    config = validate_worker_configuration()
    if not config.get("valid"):
        _set_status(status, status="disabled", health="CONFIG_ERROR", config=config)
        logger.error("[SLA_SHADOW_START] status=CONFIG_ERROR reasons=%s", ",".join(config.get("reasons") or []))
        return
    try:
        cutover = parse_explicit_santiago_timestamp(Config.CRM_SLA_REASSIGNMENT_SHADOW_CUTOVER_AT)
    except Exception:
        _set_status(status, status="disabled", health="CONFIG_ERROR")
        logger.error("[SLA_SHADOW_START] status=CONFIG_ERROR reason=SHADOW_CUTOVER_INVALID")
        return
    if Config.CRM_SLA_REASSIGNMENT_CUTOVER_AT not in (None, ""):
        _set_status(status, status="disabled", health="CONFIG_ERROR")
        logger.error("[SLA_SHADOW_START] status=CONFIG_ERROR reason=PRODUCTION_CUTOVER_NOT_EMPTY")
        return

    holder_id = _instance_id()
    settings = LeaseSettings(interval_seconds=int(Config.CRM_SLA_REASSIGNMENT_WORKER_INTERVAL_SECONDS))
    if not settings.validate().get("valid"):
        _set_status(status, status="disabled", health="CONFIG_ERROR")
        logger.error("[SLA_SHADOW_START] status=CONFIG_ERROR reason=LEASE_TIMING_INVALID")
        return
    try:
        raw_db = get_async_db()
        # Motor's command path can remain pending indefinitely in the Render
        # runtime even though the application's synchronous Mongo client is
        # healthy.  Keep the startup guard read-only and non-blocking by
        # probing that existing client in the worker thread; the A1 snapshot
        # below remains the authoritative async read-path check.
        await asyncio.wait_for(
            asyncio.to_thread(lambda: get_db().command("ping")),
            timeout=15.0,
        )
        # Historical cold load is allowed at startup, but no cycle can be
        # evaluated until both it and the A1 live path return successfully.
        await asyncio.wait_for(
            _default_performance_snapshot(
                datetime.now(timezone.utc), db=raw_db, shadow_live_capacity=True,
            ),
            timeout=60.0,
        )
    except Exception as exc:
        _set_status(status, status="disabled", health="SHADOW_STALE", last_error=type(exc).__name__)
        logger.error("[SLA_SHADOW_START] status=SNAPSHOT_UNAVAILABLE error_type=%s", type(exc).__name__)
        return

    guarded_db = ShadowWriteGuardDB(raw_db)
    metrics = ShadowRuntimeMetrics()
    instance_config = {
        "holder_id": holder_id,
        "cutover_at": cutover.isoformat(),
        "timezone": "America/Santiago",
        "policy_version": SHADOW_POLICY_VERSION,
        "live_capacity_variant": "A1_AGGREGATION_PER_CYCLE_MINIMAL",
        "interval_seconds": settings.interval_seconds,
        "batch_size": int(Config.CRM_SLA_REASSIGNMENT_BATCH_SIZE),
        "lease": settings.validate(),
        "business_writes_allowed": False,
        "executor_calls": 0,
    }
    _set_status(status, health="SHADOW_HEALTHY", mode="shadow", config=instance_config, metrics={})
    logger.info("[SLA_SHADOW_START] status=RUNNING holder=%s cutover_at=%s timezone=America/Santiago interval_seconds=%s batch_size=%s variant=A1_AGGREGATION_PER_CYCLE_MINIMAL master=false transaction_gate=false security=false", holder_id, cutover.isoformat(), settings.interval_seconds, Config.CRM_SLA_REASSIGNMENT_BATCH_SIZE)

    page_token: Mapping[str, Any] | None = None
    heartbeat_stop = asyncio.Event()
    heartbeat_task: asyncio.Task[Any] | None = None
    halted = False
    try:
        while not stop_event.is_set() and not halted:
            batch_id = f"{holder_id}:{metrics.iterations + 1}"
            started = time.perf_counter()
            try:
                result = await run_sla_reassignment_iteration_with_leader_lease(
                    guarded_db,
                    holder_id=holder_id,
                    lease_write_enabled=True,
                    lease_settings=settings,
                    cutover_at=cutover,
                    shadow_cutover_at=cutover,
                    batch_size=int(Config.CRM_SLA_REASSIGNMENT_BATCH_SIZE),
                    page_token=page_token,
                    worker_instance_id=holder_id,
                    batch_id=batch_id,
                )
                lease = result.get("lease") or {}
                if lease.get("status") == "ACQUIRED" and heartbeat_task is None:
                    heartbeat_task = asyncio.create_task(_lease_heartbeat(guarded_db, holder_id=holder_id, settings=settings, stop_event=heartbeat_stop, status=status))
                if result.get("status") == "lease_not_held":
                    _set_status(status, status="running", health="LEASE_NOT_HELD", last_lease=result.get("lease"), last_heartbeat=datetime.now(timezone.utc).isoformat())
                    logger.info("[SLA_SHADOW_SKIPPED] reason=LEASE_NOT_HELD")
                else:
                    page_token = result.get("next_page_token")
                    metrics_snapshot = metrics.record(result)
                    duration_ms = float((result.get("iteration") or {}).get("duration_ms") or ((time.perf_counter() - started) * 1000.0))
                    health, action = _health_for_duration(duration_ms, critical_consecutive=metrics.critical_consecutive)
                    if duration_ms >= settings.interval_seconds * 1000:
                        health, action = "SHADOW_ERROR", "STOP_NEW_EVALUATIONS"
                        logger.critical("[SLA_SHADOW_ERROR] reason=ITERATION_EXCEEDS_INTERVAL duration_ms=%.2f interval_seconds=%s", duration_ms, settings.interval_seconds)
                    pre_shadow_violation = any(
                        row.get("would_execute") and _utc(row.get("breach_at")) and _utc(row.get("breach_at")) < cutover
                        for row in (result.get("evaluations") or [])
                    )
                    if pre_shadow_violation:
                        health, action = "SHADOW_ERROR", "STOP_NEW_EVALUATIONS"
                        logger.critical("[SLA_SHADOW_ERROR] reason=PRE_SHADOW_WOULD_EXECUTE")
                    if (result.get("status") == "shadow_storage_failed"):
                        health, action = "SHADOW_ERROR", "STOP_NEW_EVALUATIONS"
                    _set_status(status, status="running" if action != "STOP_NEW_EVALUATIONS" else "error", health=health, last_heartbeat=datetime.now(timezone.utc).isoformat(), last_result=result.get("iteration"), metrics=metrics_snapshot, shadow_storage_ms=result.get("shadow_storage_ms"), last_lease=lease)
                    logger.info("[SLA_SHADOW_HEALTH] health=%s action=%s iteration_p90_ms=%s iteration_max_ms=%s", health, action, metrics_snapshot.get("iteration_p90_ms"), metrics_snapshot.get("iteration_max_ms"))
                    for decision in result.get("decisions") or []:
                        logger.info("[SLA_SHADOW_DECISION] branch=%s selected_user_id=%s score=%s margin=%s would_execute=true", decision.get("policy_branch"), decision.get("selected_user_id"), decision.get("selected_score"), decision.get("score_margin"))
                    if action == "STOP_NEW_EVALUATIONS":
                        halted = True
            except ShadowWriteGuardViolation:
                metrics.errors += 1
                halted = True
                _set_status(status, status="error", health="SHADOW_ERROR", last_error="SHADOW_WRITE_GUARD_VIOLATION")
            except Exception as exc:
                metrics.errors += 1
                _set_status(status, status="error", health="SHADOW_STALE", last_error=type(exc).__name__, last_heartbeat=datetime.now(timezone.utc).isoformat())
                logger.error("[SLA_SHADOW_ERROR] reason=ITERATION_ERROR error_type=%s", type(exc).__name__)
            if halted:
                break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.interval_seconds)
            except asyncio.TimeoutError:
                continue
    finally:
        heartbeat_stop.set()
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if not halted:
            _set_status(status, status="stopped", health="SHADOW_STALE")
        logger.info("[SLA_SHADOW_START] status=STOPPED holder=%s halted=%s", holder_id, halted)
