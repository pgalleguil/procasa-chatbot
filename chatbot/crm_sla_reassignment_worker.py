"""Prospective CRM SLA reassignment worker in shadow mode.

This module is deliberately invocation-only.  Importing it does not create a
Mongo client, schedule a task, call the transaction executor, send a message,
or mutate a lead/cycle.  The worker stops after producing an analytical
``SLAReassignmentShadowEvaluation`` and an in-memory ``SLAReassignmentDecision``.

The selection logic is delegated to the frozen Phase 1G helpers.  Persistent
distribution state is reconstructed from committed audit events; shadow
decisions are kept in local state for the duration of one batch and are never
written as product state.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Iterable, Mapping

from .crm_metrics import INSTRUMENTATION_CUTOVER, calculate_sla, coerce_utc_datetime, event_evidence, normalize_result, utc_now
from .crm_sla_alert_evaluator import CLOSED_STAGES, SLA_STOP_RESULTS, OUTREACH_RESULTS, add_business_minutes
from .crm_sla_global_rescue import RescueParameters
from .crm_sla_hybrid_rescue import (
    REGION_JPC_MARIA_HERNAN,
    REGION_REVIEW_REQUIRED,
    RM_GLOBAL_RESCUE,
    REGIONAL_POLICY_NOT_DEFINED,
)
from .crm_sla_hybrid_stabilization import (
    L1_LOW_NEEDS_PLUS_5,
    R2_ROLLING_SHARE,
    SUPERVISOR_REVIEW_REQUIRED,
    build_decision,
    generate_decision_id,
)
from .crm_sla_policy_freeze import (
    POLICY_VERSION,
    _jpc_selection,
    _rm_selection,
    _update_state,
    derive_jpc_share_targets,
)
from .crm_sla_reassignment_cutover import CUTOVER_POLICY_VERSION
from .crm_sla_performance_snapshot import (
    DEFAULT_HISTORICAL_TTL_SECONDS,
    DEFAULT_LIVE_CAPACITY_TTL_SECONDS,
    PerformanceSnapshotCache,
    build_historical_performance_snapshot,
    build_live_capacity_snapshot,
    read_source_watermarks,
)
from .crm_sla_live_capacity import A1_NAME, build_live_capacity_variant
from .crm_sla_reassignment_shadow import (
    SHADOW_COLLECTION,
    SHADOW_POLICY_VERSION,
    SHADOW_SCHEMA_VERSION,
    build_shadow_document,
    persist_shadow_evaluations,
    reconstruct_shadow_distribution_state,
)
from .crm_sla_snapshot_queries import ReadInstrumentation, find_many
from .crm_sla_territorial_shadow import compact_region_key, commune_key, profile_communes, regional_profile_regions

logger = logging.getLogger(__name__)

WORKER_LOG_PREFIX = "[SLA_REASSIGNMENT_WORKER_SHADOW]"
LEDGER_COLLECTION = "crm_sla_reassignment_audit_v1"
COMMITTED_EVENT = "SLA_REASSIGNMENT_COMMITTED"
ALLOWED_BATCH_SIZES = (10, 25, 50, 100)
ALLOWED_INTERVAL_SECONDS = (30, 60, 120)
PERFORMANCE_WINDOW_DAYS = 60
R2_HISTORY_WINDOW = 19  # current candidate + 19 prior decisions = 20
R2_MAX_PRIOR_SHARE_COUNT = 8  # 9/20 would exceed 40%
R2_SCORE_DISTANCE = 15.0
JPC_MIN_SHARE = 0.30
JPC_MAX_SHARE = 0.70
_DEFAULT_PERFORMANCE_SNAPSHOT_CACHE = PerformanceSnapshotCache(
    historical_ttl_seconds=DEFAULT_HISTORICAL_TTL_SECONDS,
    live_capacity_ttl_seconds=DEFAULT_LIVE_CAPACITY_TTL_SECONDS,
)
_DEFAULT_SHADOW_PERFORMANCE_SNAPSHOT_CACHE = PerformanceSnapshotCache(
    historical_ttl_seconds=DEFAULT_HISTORICAL_TTL_SECONDS,
    live_capacity_ttl_seconds=DEFAULT_LIVE_CAPACITY_TTL_SECONDS,
)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso(value: Any) -> str:
    parsed = coerce_utc_datetime(value)
    return parsed.isoformat() if parsed else _text(value)


def _utc(value: Any) -> datetime | None:
    return coerce_utc_datetime(value)


def _identity(value: Any) -> str:
    import re
    import unicodedata

    text = unicodedata.normalize("NFKD", _text(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text.strip().lower())


def _json_version(rows: Iterable[Mapping[str, Any]]) -> str:
    payload = json.dumps(list(rows), sort_keys=True, default=str, ensure_ascii=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def _cfg(name: str, default: Any) -> Any:
    from config import Config
    return getattr(Config, name, default)


def _validated_batch_size(value: int | None) -> int:
    selected = int(value if value is not None else _cfg("CRM_SLA_REASSIGNMENT_BATCH_SIZE", 25))
    if selected not in ALLOWED_BATCH_SIZES:
        raise ValueError(f"unsupported CRM_SLA_REASSIGNMENT_BATCH_SIZE: {selected}")
    return selected


def _validated_interval(value: int | None) -> int:
    selected = int(value if value is not None else _cfg("CRM_SLA_REASSIGNMENT_WORKER_INTERVAL_SECONDS", 60))
    if selected not in ALLOWED_INTERVAL_SECONDS:
        raise ValueError(f"unsupported CRM_SLA_REASSIGNMENT_WORKER_INTERVAL_SECONDS: {selected}")
    return selected


@dataclass(frozen=True)
class CanonicalExpiration:
    started_at: datetime | None
    deadline_at: datetime | None
    breach_at: datetime | None
    threshold_minutes: int
    elapsed_business_minutes: float | None
    overdue_business_minutes: float
    expired: bool
    temperature: str
    start_source: str
    persisted_deadline_at: datetime | None = None
    persisted_deadline_mismatch: bool = False

    def to_dict(self) -> dict[str, Any]:
        output = asdict(self)
        for key in ("started_at", "deadline_at", "breach_at", "persisted_deadline_at"):
            if output[key]:
                output[key] = output[key].isoformat()
        return output


@dataclass(frozen=True)
class ManagementProtection:
    protected: bool
    human_evidence_count: int = 0
    current_stop_at: datetime | None = None
    first_human_at: datetime | None = None
    human_before_breach: bool = False
    human_after_breach: bool = False
    evidence_types: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        output = asdict(self)
        for key in ("current_stop_at", "first_human_at"):
            if output[key]:
                output[key] = output[key].isoformat()
        return output


@dataclass(frozen=True)
class SLAReassignmentShadowEvaluation:
    evaluated_at: str
    lead_id: str
    source_cycle_id: str
    breach_at: str
    cutover: str
    branch: str
    eligible: bool
    exclusion_reason: str
    candidate_ids: tuple[str, ...] = ()
    selected_user_id: str | None = None
    selected_score: float | None = None
    second_user_id: str | None = None
    score_margin: float | None = None
    guardrail: str = ""
    confidence: str = ""
    assignment_number: int = 0
    decision_id: str = ""
    would_execute: bool = False
    shadow: bool = True
    current_overdue_business_minutes: float = 0.0
    temperature: str = "NORMAL"
    performance_snapshot_version: str = ""
    distribution_state_version: str = ""
    performance_snapshot_id: str = ""
    capacity_snapshot_at: str = ""
    r2_state_snapshot: Mapping[str, Any] = field(default_factory=dict)
    j3_target_share_snapshot: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SLAWorkerIterationResult:
    status: str
    scanned: int = 0
    canonically_expired: int = 0
    pre_cutover_skipped: int = 0
    protected_skipped: int = 0
    undefined_skipped: int = 0
    review_skipped: int = 0
    max2_skipped: int = 0
    not_actually_expired: int = 0
    evaluated: int = 0
    would_reassign: int = 0
    errors: int = 0
    duration_ms: float = 0.0
    error_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _candidate_query_superset(current_policy_since: datetime) -> dict[str, Any]:
    """Return a broad, read-only query that cannot rely on persisted deadlines."""
    return {
        "cycle_status": "active",
        "unassigned_at": None,
        "assignment_cycle_id": {"$exists": True, "$ne": None},
        "lead_id": {"$exists": True, "$ne": None},
        "assigned_at": {"$exists": True, "$ne": None},
        "$and": [
            {
                "$or": [
                    {"assigned_at": {"$gte": current_policy_since}},
                    {"sla_started_at": {"$gte": current_policy_since}},
                ]
            },
            {
                "$or": [
                    {"reassignment_decision_id": {"$exists": False}},
                    {"reassignment_decision_id": None},
                ]
            },
            {
                "$or": [
                    {"reassignment_state": {"$exists": False}},
                    {"reassignment_state": {"$nin": ["completed", "reassigned"]}},
                ]
            },
        ],
    }


def _page_filter(query: dict[str, Any], page_token: Mapping[str, Any] | None) -> dict[str, Any]:
    if not page_token:
        return query
    assigned_at = _utc(page_token.get("assigned_at"))
    lead_id = _text(page_token.get("lead_id"))
    cycle_id = _text(page_token.get("assignment_cycle_id"))
    if not assigned_at:
        return query
    return {
        "$and": [
            query,
            {
                "$or": [
                    {"assigned_at": {"$gt": assigned_at}},
                    {"assigned_at": assigned_at, "lead_id": {"$gt": lead_id}},
                    {"assigned_at": assigned_at, "lead_id": lead_id, "assignment_cycle_id": {"$gt": cycle_id}},
                ]
            },
        ]
    }


async def _cursor_to_list(cursor: Any, length: int | None = None) -> list[dict[str, Any]]:
    method = getattr(cursor, "to_list", None)
    if method is None:
        return list(cursor or [])
    value = method(length=length)
    if inspect.isawaitable(value):
        value = await value
    return list(value or [])


async def _find_many(
    collection: Any,
    query: dict[str, Any],
    projection: dict[str, Any] | None = None,
    *,
    limit: int | None = None,
    sort: list[tuple[str, int]] | None = None,
    instrumentation: ReadInstrumentation | None = None,
    query_name: str = "worker.find",
) -> list[dict[str, Any]]:
    return await find_many(
        collection, query, projection, limit=limit, sort=sort,
        instrumentation=instrumentation, query_name=query_name,
    )


async def scan_sla_reassignment_candidates(
    db: Any = None,
    *,
    batch_size: int | None = None,
    page_token: Mapping[str, Any] | None = None,
    current_policy_since: Any = None,
    read_instrumentation: ReadInstrumentation | None = None,
) -> dict[str, Any]:
    """Scan one stable page of a safe superset; never evaluates or writes."""
    if db is None:
        from .storage import get_async_db
        db = get_async_db()
    size = _validated_batch_size(batch_size)
    policy_since = _utc(current_policy_since) or _utc(INSTRUMENTATION_CUTOVER)
    if not policy_since:
        raise RuntimeError("current policy cutover unavailable")
    base_query = _candidate_query_superset(policy_since)
    query = _page_filter(base_query, page_token)
    projection = {
        "_id": 1, "lead_id": 1, "assignment_cycle_id": 1,
        "assigned_to_user_id": 1, "assigned_to_display_name": 1,
        "assigned_at": 1, "sla_started_at": 1, "hot_started_at": 1,
        "sla_breached_at": 1, "sla_expired_at": 1, "deadline_at": 1, "sla_deadline_at": 1,
        "temperature_at_assignment": 1, "cycle_status": 1, "unassigned_at": 1,
        "schema_version": 1, "sla_policy_version": 1, "policy_version": 1,
        "reason": 1, "cycle_origin": 1, "cycle_version": 1,
        "first_valid_management_at": 1, "first_contact_attempt_at": 1,
        "reassignment_protection_at": 1, "automatic_reassignment_number": 1,
        "assignment_number": 1, "previous_owner_user_ids": 1,
        "reassignment_decision_id": 1, "reassignment_state": 1,
    }
    cycles = await _find_many(
        db["crm_assignment_cycles"], query, projection,
        limit=size,
        sort=[("assigned_at", 1), ("lead_id", 1), ("assignment_cycle_id", 1)],
        instrumentation=read_instrumentation,
        query_name="worker.scanner.cycles",
    )
    next_token = None
    if cycles:
        last = cycles[-1]
        next_token = {
            "assigned_at": _iso(last.get("assigned_at")),
            "lead_id": _text(last.get("lead_id")),
            "assignment_cycle_id": _text(last.get("assignment_cycle_id")),
        }
    return {
        "status": "scanned",
        "cycles": cycles,
        "scanned": len(cycles),
        "documents_examined": len(cycles),
        "batch_size": size,
        "query": query,
        "candidate_query_superset": base_query,
        "current_policy_since": policy_since.isoformat(),
        "next_page_token": next_token,
    }


def canonical_expiration_recheck(cycle: Mapping[str, Any], lead: Mapping[str, Any] | None, *, now: Any) -> CanonicalExpiration:
    """Recalculate expiry with the production ``calculate_sla`` function.

    Persisted deadline fields are read only to report mismatches.  They never
    decide whether a cycle is expired, so an inconsistent deadline cannot
    silently create a false negative.
    """
    assigned_at = _utc(cycle.get("assigned_at"))
    persisted_start = _utc(cycle.get("sla_started_at"))
    started_at = persisted_start or assigned_at
    start_source = "persisted" if persisted_start else "fallback_assigned_at" if assigned_at else "missing"
    temperature = _text(cycle.get("temperature_at_assignment") or (lead or {}).get("lead_temperature_effective") or "NORMAL").upper()
    if temperature not in {"HOT", "NORMAL"}:
        temperature = "NORMAL"
    threshold = 60 if temperature == "HOT" else 180
    lifecycle = (lead or {}).get("lifecycle") or {}
    hot_start = _utc(cycle.get("hot_started_at")) or _utc(lifecycle.get("hot_since"))
    if hot_start and started_at and hot_start < started_at:
        hot_start = started_at
    persisted_deadline = None
    for key in ("sla_breached_at", "sla_expired_at", "deadline_at", "sla_deadline_at"):
        persisted_deadline = _utc(cycle.get(key))
        if persisted_deadline:
            break
    if not started_at:
        return CanonicalExpiration(None, None, None, threshold, None, 0.0, False, temperature, start_source, persisted_deadline, False)
    result = calculate_sla(
        assigned_at=started_at,
        first_valid_management_at=None,
        now=_utc(now) or utc_now(),
        temperature=temperature,
        hot_started_at=hot_start,
    )
    measured = result.get("hot_minutes") if temperature == "HOT" else result.get("minutes")
    deadline_start = hot_start if temperature == "HOT" and hot_start else started_at
    deadline = add_business_minutes(deadline_start, threshold)
    expired = result.get("status") == "critical"
    mismatch = bool(persisted_deadline and abs((persisted_deadline - deadline).total_seconds()) > 1)
    elapsed = _number(measured) if measured is not None else None
    return CanonicalExpiration(
        started_at=started_at,
        deadline_at=deadline,
        breach_at=deadline,
        threshold_minutes=threshold,
        elapsed_business_minutes=elapsed,
        overdue_business_minutes=max(0.0, (elapsed or 0.0) - threshold),
        expired=bool(expired),
        temperature=temperature,
        start_source=start_source,
        persisted_deadline_at=persisted_deadline,
        persisted_deadline_mismatch=mismatch,
    )


def _human_actor(value: Any, actor_type: Any = None) -> bool:
    actor = _identity(value)
    kind = _identity(actor_type)
    if not actor or actor in {"system", "sistema", "bot", "none", "null", "automation", "chatbot"}:
        return False
    return not kind or kind in {"human", "agent", "administrator", "supervisor", "executive"}


def _management_protection(cycle: Mapping[str, Any], lead: Mapping[str, Any] | None, events: Iterable[Mapping[str, Any]], results: Iterable[Mapping[str, Any]], *, breach_at: datetime | None) -> ManagementProtection:
    assigned_at = _utc(cycle.get("assigned_at"))
    human_times: list[datetime] = []
    stop_times: list[datetime] = []
    evidence_types: list[str] = []
    for event in events:
        occurred = _utc(event.get("timestamp") or event.get("occurred_at"))
        if not occurred or (assigned_at and occurred < assigned_at):
            continue
        ev = event_evidence(event)
        raw_type = _text(event.get("type")).upper()
        if ev.get("management"):
            stop_times.append(occurred)
            evidence_types.append(f"event:{raw_type or 'management'}")
        if _human_actor(event.get("actor"), event.get("actor_type") or (event.get("meta") or {}).get("actor_type")) and raw_type in {"CALL_COMPLETED_LEAD", "SEND_WA_LEAD", "SEND_EMAIL_LEAD"}:
            human_times.append(occurred)
            evidence_types.append(f"human:{raw_type}")
    for result in results:
        occurred = _utc(result.get("occurred_at"))
        if not occurred or (assigned_at and occurred < assigned_at):
            continue
        normalized = normalize_result(result.get("result_type"))
        actor = result.get("actor_user_id") or result.get("actor")
        if normalized in SLA_STOP_RESULTS:
            stop_times.append(occurred)
        if _human_actor(actor, result.get("actor_type")) and (normalized in SLA_STOP_RESULTS or normalized in OUTREACH_RESULTS or normalized):
            human_times.append(occurred)
            evidence_types.append(f"result:{normalized or 'UNKNOWN'}")
    persisted = [
        _utc(cycle.get("first_valid_management_at")),
        _utc(cycle.get("first_contact_attempt_at")),
        _utc(cycle.get("reassignment_protection_at")),
        _utc(((lead or {}).get("lifecycle") or {}).get("first_valid_management_at")),
    ]
    persisted = [value for value in persisted if value and (not assigned_at or value >= assigned_at)]
    stop_times.extend(persisted)
    if persisted:
        evidence_types.append("persisted_protection_field")
    first_human = min(human_times) if human_times else None
    current_stop = min(stop_times) if stop_times else None
    human_before = bool(first_human and breach_at and first_human <= breach_at)
    human_after = bool(first_human and breach_at and first_human > breach_at)
    return ManagementProtection(
        protected=bool(human_times or persisted),
        human_evidence_count=len(human_times),
        current_stop_at=current_stop,
        first_human_at=first_human,
        human_before_breach=human_before,
        human_after_breach=human_after,
        evidence_types=tuple(sorted(set(evidence_types))),
    )


def _owner_cycle_issue(cycle: Mapping[str, Any], lead: Mapping[str, Any] | None, active_cycles: Iterable[Mapping[str, Any]], users_by_id: Mapping[str, Mapping[str, Any]]) -> str:
    cycle_id = _text(cycle.get("assignment_cycle_id"))
    owner_id = _text(cycle.get("assigned_to_user_id"))
    if not cycle_id or not owner_id:
        return "DATA_INSUFFICIENT"
    active = [row for row in active_cycles if row.get("cycle_status") == "active" and row.get("unassigned_at") is None]
    if len(active) > 1:
        return "CONCURRENT_ACTIVITY_RISK"
    lifecycle = (lead or {}).get("lifecycle") or {}
    pointer = _text(lifecycle.get("current_assignment_cycle_id"))
    if pointer and pointer != cycle_id:
        return "CYCLE_MISMATCH"
    explicit_owner = _text((lead or {}).get("owner_user_id") or (lead or {}).get("assigned_to_user_id"))
    if explicit_owner and explicit_owner != owner_id:
        return "OWNER_MISMATCH"
    lead_name = _text((lead or {}).get("ejecutivo_asignado") or ((lead or {}).get("prospecto") or {}).get("ejecutivo"))
    cycle_name = _text(cycle.get("assigned_to_display_name"))
    if lead_name and cycle_name and _identity(lead_name) != _identity(cycle_name):
        return "OWNER_MISMATCH"
    user = users_by_id.get(owner_id)
    if not user or user.get("is_active") is not True or _identity(user.get("rol")) != "agente":
        return "OWNER_MISMATCH"
    if not explicit_owner and not lead_name and not pointer:
        return "DATA_INSUFFICIENT"
    return "OWNER_OK"


def _lead_is_open(lead: Mapping[str, Any] | None) -> bool:
    if not lead:
        return False
    return _text(lead.get("pipeline_stage") or lead.get("stage") or lead.get("crm_estado")).upper() not in CLOSED_STAGES


def _assignment_number(cycle: Mapping[str, Any]) -> int:
    return int(_number(cycle.get("automatic_reassignment_number", cycle.get("assignment_number", 0))))


def _previous_owner_ids(cycle: Mapping[str, Any]) -> tuple[str, ...]:
    values = [_text(cycle.get("assigned_to_user_id"))]
    raw = cycle.get("previous_owner_user_ids") or cycle.get("previous_sla_owner_user_ids") or ()
    if isinstance(raw, str):
        raw = [raw]
    values.extend(_text(value) for value in raw)
    return tuple(dict.fromkeys(value for value in values if value))


def _worker_order_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    temperature = _text(row.get("temperature") or "NORMAL").upper()
    hot_first = 0 if temperature == "HOT" else 1
    overdue = -_number(row.get("current_overdue_business_minutes"))
    assigned = _iso(row.get("assigned_at"))
    return hot_first, overdue, assigned, _text(row.get("lead_id")), _text(row.get("assignment_cycle_id"))


async def _batch_context(
    db: Any,
    cycles: list[Mapping[str, Any]],
    *,
    read_instrumentation: ReadInstrumentation | None = None,
) -> dict[str, Any]:
    lead_values = [cycle.get("lead_id") for cycle in cycles if cycle.get("lead_id") is not None]
    cycle_ids = [_text(cycle.get("assignment_cycle_id")) for cycle in cycles if cycle.get("assignment_cycle_id")]
    leads = await _find_many(
        db["leads"], {"_id": {"$in": lead_values}},
        {"_id": 1, "pipeline_stage": 1, "stage": 1, "crm_estado": 1, "lead_temperature_effective": 1,
         "ejecutivo_asignado": 1, "lifecycle": 1, "prospecto.codigo": 1, "prospecto.codigo_propiedad": 1,
         "prospecto.codigo_referencia": 1, "prospecto.origen": 1, "prospecto.operacion": 1,
         "prospecto.comuna": 1, "prospecto.region": 1, "comuna": 1, "region": 1,
         "property_code": 1, "origin": 1, "lead_origin": 1, "operacion": 1},
        instrumentation=read_instrumentation,
        query_name="worker.context.leads",
    ) if lead_values else []
    active_cycles = await _find_many(
        db["crm_assignment_cycles"], {"lead_id": {"$in": lead_values}, "cycle_status": "active", "unassigned_at": None},
        {"lead_id": 1, "assignment_cycle_id": 1, "assigned_to_user_id": 1, "cycle_status": 1, "unassigned_at": 1},
        instrumentation=read_instrumentation,
        query_name="worker.context.active_cycles",
    ) if lead_values else []
    results = await _find_many(
        db["crm_management_results"], {"assignment_cycle_id": {"$in": cycle_ids}},
        {"lead_id": 1, "assignment_cycle_id": 1, "actor_user_id": 1, "actor_type": 1, "result_type": 1, "occurred_at": 1},
        instrumentation=read_instrumentation,
        query_name="worker.context.management_results",
    ) if cycle_ids else []
    events = await _find_many(
        db["crm_events"], {"lead_id": {"$in": lead_values}},
        {"lead_id": 1, "assignment_cycle_id": 1, "type": 1, "actor": 1, "actor_type": 1, "confirmed": 1, "result": 1, "meta": 1, "timestamp": 1, "occurred_at": 1},
        instrumentation=read_instrumentation,
        query_name="worker.context.events",
    ) if lead_values else []
    return {
        "leads_by_id": {_text(row.get("_id")): row for row in leads},
        "active_cycles_by_lead": defaultdict(list, {_text(key): [] for key in lead_values}),
        "results_by_cycle": defaultdict(list),
        "events_by_lead": defaultdict(list),
    } | _index_context(active_cycles, results, events)


def _index_context(active_cycles: Iterable[Mapping[str, Any]], results: Iterable[Mapping[str, Any]], events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    active_by_lead: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    results_by_cycle: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    events_by_lead: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in active_cycles:
        active_by_lead[_text(row.get("lead_id"))].append(dict(row))
    for row in results:
        results_by_cycle[_text(row.get("assignment_cycle_id"))].append(dict(row))
    for row in events:
        events_by_lead[_text(row.get("lead_id"))].append(dict(row))
    return {"active_cycles_by_lead": active_by_lead, "results_by_cycle": results_by_cycle, "events_by_lead": events_by_lead}


async def _legacy_default_performance_snapshot(as_of: datetime) -> dict[str, Any]:
    """Build one read-only historical/capacity snapshot for the batch.

    The analytical loader uses projections without phones or message bodies and
    runs off the event loop.  It is called once per worker iteration, never once
    per lead.
    """
    from .crm_sla_global_rescue import RescueParameters
    from scripts.run_phase05_crm_reassignment_audit import enrich_records
    from scripts.run_phase1b_crm_capacity_audit import load_data_phase1b
    from scripts.run_phase1d_crm_sla_global_rescue import active_agents, current_backlog, historical_metrics
    from scripts.run_phase1c_crm_territory_audit import build_catalog

    started = time.perf_counter()
    data = await asyncio.to_thread(load_data_phase1b)
    data["as_of"] = as_of
    records = await asyncio.to_thread(enrich_records, data)
    agents = active_agents(data.get("users", []))
    params = RescueParameters(performance_window_days=PERFORMANCE_WINDOW_DAYS)
    metrics, team = historical_metrics(records, agents, as_of=as_of, params=params)
    backlog = current_backlog(records, agents)
    try:
        catalog = build_catalog()
    except Exception:
        catalog = {}
    candidates = []
    for agent in agents:
        user_id = _text(agent.get("_id"))
        metric = metrics.get(user_id, {})
        load = backlog.get(user_id, {})
        candidates.append({
            "user_id": user_id, "executive": _text(agent.get("nombre")),
            "identity_key": _identity(agent.get("nombre")), "executive_key": _identity(agent.get("nombre")),
            "active": agent.get("is_active") is True, "role": _identity(agent.get("rol")), "legacy": False,
            "protected_by_management": False, "data_issue": False, "closed_lead": False, "not_currently_expired": False,
            "sample_size": metric.get("sample_size", 0), "sla_compliance_rate": metric.get("sla_compliance_rate", 0.0),
            "attention_rate": metric.get("attention_rate", 0.0),
            "p50_first_management_business_minutes": metric.get("p50"),
            "p90_first_management_business_minutes": metric.get("p90"),
            "open_current_policy": load.get("open", 0), "unmanaged_current_policy": load.get("unmanaged", 0),
            "expired_current_policy": load.get("expired", 0), "shadow_received_count": 0,
            "user_record": dict(agent),
        })
    return {
        "candidates": candidates, "team": {
            "sla_compliance_rate": team.get("sla_compliance_rate", 0.0),
            "attention_rate": team.get("attention_rate", 0.0),
            "team_p50_average": team.get("team_p50_average"),
            "team_p90_average": team.get("team_p90_average"),
        },
        "metrics": metrics, "backlog": backlog, "agents": agents,
        "users_by_id": {_text(agent.get("_id")): agent for agent in agents},
        "catalog": catalog, "properties": data.get("properties", {}),
        "records_by_cycle": {_text(row.get("cycle_id")): row for row in records if row.get("cycle_id")},
        "performance_snapshot_version": _json_version([
            {"user_id": row.get("user_id"), "sample_size": row.get("sample_size"), "open": row.get("open_current_policy"),
             "unmanaged": row.get("unmanaged_current_policy"), "expired": row.get("expired_current_policy")}
            for row in candidates
        ]),
        "docs_examined": sum(len(data.get(key, [])) if isinstance(data.get(key), list) else len(data.get(key, {})) for key in ("cycles", "leads", "events", "management_results", "users", "properties")),
        "read_ms": (time.perf_counter() - started) * 1000.0,
    }


async def _default_performance_snapshot(
    as_of: datetime,
    *,
    db: Any = None,
    performance_cache: PerformanceSnapshotCache | None = None,
    policy_since: Any = None,
    read_instrumentation: ReadInstrumentation | None = None,
    shadow_live_capacity: bool = False,
) -> dict[str, Any]:
    """Return one cached historical snapshot plus a live-capacity view.

    A1 is deliberately selected only by the prospective shadow worker.  A1
    failures are propagated to the caller so the worker fails closed; the
    previous live builder is not a silent fallback for shadow execution.
    """
    if db is None:
        from .storage import get_async_db
        db = get_async_db()
    policy_start = _utc(policy_since) or _utc(INSTRUMENTATION_CUTOVER)
    if not policy_start:
        raise RuntimeError("performance_policy_cutover_unavailable")
    cache = performance_cache or (_DEFAULT_SHADOW_PERFORMANCE_SNAPSHOT_CACHE if shadow_live_capacity else _DEFAULT_PERFORMANCE_SNAPSHOT_CACHE)
    watermarks = await read_source_watermarks(db, instrumentation=read_instrumentation)
    historical = await cache.get_historical(
        now=as_of,
        policy_version=POLICY_VERSION,
        source_watermarks=watermarks,
        builder=lambda: build_historical_performance_snapshot(
            db, as_of=as_of, policy_since=policy_start,
            performance_window_days=PERFORMANCE_WINDOW_DAYS,
            policy_version=POLICY_VERSION,
            instrumentation=read_instrumentation,
        ),
    )
    live = await cache.get_live_capacity(
        now=as_of,
        policy_version=POLICY_VERSION,
        source_watermarks=watermarks,
        builder=(
            lambda: build_live_capacity_variant(
                db, variant=A1_NAME, as_of=as_of, policy_since=policy_start,
                instrumentation=read_instrumentation,
            )
            if shadow_live_capacity else
            lambda: build_live_capacity_snapshot(
                db, as_of=as_of, policy_since=policy_start,
                policy_version=POLICY_VERSION,
                instrumentation=read_instrumentation,
            )
        ),
    )
    combined = dict(historical)
    capacity = dict(live.get("capacity_metrics") or {})
    candidates = []
    for raw in combined.get("candidates") or []:
        row = dict(raw)
        live_row = capacity.get(_text(row.get("user_id")), {})
        row["open_current_policy"] = live_row.get("open", row.get("open_current_policy", 0))
        row["unmanaged_current_policy"] = live_row.get("unmanaged", row.get("unmanaged_current_policy", 0))
        row["expired_current_policy"] = live_row.get("expired", row.get("expired_current_policy", 0))
        candidates.append(row)
    combined["candidates"] = candidates
    combined["backlog"] = capacity
    combined["live_capacity_snapshot_id"] = live.get("snapshot_id")
    combined["capacity_snapshot_at"] = live.get("generated_at")
    combined["live_capacity_variant"] = live.get("variant") or (A1_NAME if shadow_live_capacity else "CURRENT_REFERENCE")
    combined["source_watermarks"] = watermarks
    combined["performance_snapshot_version"] = historical.get("snapshot_id") or historical.get("performance_snapshot_version")
    combined["performance_cache_stats"] = cache.stats()
    combined["historical_docs_examined"] = historical.get("docs_examined", 0)
    combined["live_capacity_docs_examined"] = live.get("docs_examined", 0)
    combined["docs_examined"] = int(historical.get("docs_examined", 0)) + int(live.get("docs_examined", 0))
    combined["read_ms"] = float(historical.get("read_ms", 0.0) or 0.0) + float(live.get("read_ms", 0.0) or 0.0)
    return combined


def _property_code(lead: Mapping[str, Any], cycle: Mapping[str, Any]) -> str:
    prospecto = lead.get("prospecto") or {}
    return _text(cycle.get("property_code") or lead.get("property_code") or prospecto.get("codigo") or prospecto.get("codigo_propiedad") or prospecto.get("codigo_referencia"))


def _path_value(document: Mapping[str, Any] | None, paths: Iterable[str]) -> Any:
    for path in paths:
        current: Any = document
        for part in path.split("."):
            if not isinstance(current, Mapping):
                current = None
                break
            current = current.get(part)
        if current not in (None, "", [], {}):
            return current
    return None


def _fallback_territory(lead: Mapping[str, Any], prop: Mapping[str, Any] | None) -> dict[str, Any]:
    commune = commune_key(_path_value(prop, ("ubicacion.comuna", "comuna")) or _path_value(lead, ("comuna", "prospecto.comuna", "prospecto.ubicacion.comuna")))
    region = compact_region_key(_path_value(prop, ("ubicacion.region", "region")) or _path_value(lead, ("region", "prospecto.region", "prospecto.ubicacion.region")))
    prop_exec = _path_value(prop, ("estado.ejecutivo", "estado.captador", "estado.responsable", "ejecutivo", "captador", "responsable"))
    return {"commune": commune, "region": region, "property_exec": _identity(prop_exec), "resolved": bool(region), "property_exec_resolved": bool(prop_exec)}


def _policy_row(cycle: Mapping[str, Any], lead: Mapping[str, Any], snapshot: Mapping[str, Any], *, state_counts: Mapping[str, int] | None = None) -> dict[str, Any]:
    cycle_id = _text(cycle.get("assignment_cycle_id"))
    record = (snapshot.get("records_by_cycle") or {}).get(cycle_id)
    prop_code = _property_code(lead, cycle)
    prop = (snapshot.get("properties") or {}).get(prop_code)
    territory: dict[str, Any]
    property_status = ""
    property_is_jpc = False
    if record:
        try:
            from scripts.run_phase1e_crm_sla_hybrid_rescue import classify_record
            classification = classify_record(record, snapshot.get("catalog") or {})
            territory_info = classification.get("territory") or {}
            territory = {
                "commune": territory_info.get("property_commune") or territory_info.get("lead_commune") or "",
                "region": territory_info.get("canonical_region") or "",
                "resolved": bool(territory_info.get("resolved")),
            }
            prop_info = classification.get("property_executive") or {}
            property_status = _text(prop_info.get("status"))
            property_is_jpc = bool(prop_info.get("is_jpc"))
            branch = classification.get("policy_category") or REGION_REVIEW_REQUIRED
        except Exception:
            territory = _fallback_territory(lead, prop)
            property_status = "RESOLVED" if territory.get("property_exec_resolved") else "MISSING"
            property_is_jpc = territory.get("property_exec") == "jorge pablo caro"
            branch = REGION_REVIEW_REQUIRED
    else:
        territory = _fallback_territory(lead, prop)
        property_status = "RESOLVED" if territory.get("property_exec_resolved") else "MISSING"
        property_is_jpc = territory.get("property_exec") == "jorge pablo caro"
        supplied_branch = _text(lead.get("policy_category"))
        if supplied_branch in {RM_GLOBAL_RESCUE, REGION_JPC_MARIA_HERNAN, REGIONAL_POLICY_NOT_DEFINED, REGION_REVIEW_REQUIRED}:
            branch = supplied_branch
        elif not territory.get("resolved"):
            branch = REGION_REVIEW_REQUIRED
        elif territory.get("region") == "metropolitanasantiago":
            branch = RM_GLOBAL_RESCUE
        elif property_is_jpc:
            branch = REGION_JPC_MARIA_HERNAN
        else:
            branch = REGIONAL_POLICY_NOT_DEFINED
    current_owner = _text(cycle.get("assigned_to_user_id"))
    previous = _previous_owner_ids(cycle)
    team = snapshot.get("team") or {}
    base_candidates = [dict(row) for row in (snapshot.get("candidates") or [])]
    target_commune = commune_key(territory.get("commune"))
    target_region = compact_region_key(territory.get("region"))
    counts = state_counts or {}
    for candidate in base_candidates:
        user_record = candidate.get("user_record") or {}
        declared = profile_communes(user_record)
        regions = regional_profile_regions(user_record, snapshot.get("catalog") or {}) if snapshot.get("catalog") else set()
        candidate["same_commune"] = "yes" if target_commune and target_commune in declared else "no" if target_commune and declared else "unknown"
        candidate["same_region"] = "yes" if target_region and target_region in regions else "no" if target_region and regions else "unknown"
        candidate["shadow_received_count"] = _number(counts.get(_text(candidate.get("user_id")), candidate.get("shadow_received_count", 0)))
        candidate.pop("user_record", None)
    jpc_candidates = [row for row in base_candidates if _identity(row.get("identity_key") or row.get("executive_key")) in {"maria paz galleguillos", "hernan castro"}]
    return {
        "lead_id": _text(lead.get("_id") or cycle.get("lead_id")), "assignment_cycle_id": cycle_id,
        "owner_user_id": current_owner, "owner": _text(cycle.get("assigned_to_display_name")),
        "temperature": _text(cycle.get("temperature_at_assignment") or lead.get("lead_temperature_effective") or "NORMAL").upper(),
        "assigned_at": _iso(cycle.get("assigned_at")), "policy_category": branch,
        "comuna": territory.get("commune", ""), "region": territory.get("region", ""),
        "canonical_region_key": target_region, "property_code": prop_code,
        "property_executive_status": property_status, "property_is_jorge_pablo_caro": "yes" if property_is_jpc else "no",
        "previous_owner_user_ids": previous, "previous_sla_owner_ids": previous,
        "automatic_reassignment_number": _assignment_number(cycle),
        "rm_candidates": base_candidates, "jpc_candidates": jpc_candidates,
        "team_sla_rate": team.get("sla_compliance_rate", 0.0), "team_attention_rate": team.get("attention_rate", 0.0),
        "team_p50_average": team.get("team_p50_average"), "team_p90_average": team.get("team_p90_average"),
    }


def _initial_candidate_state(rows: Iterable[Mapping[str, Any]], committed_counts: Mapping[str, int] | None = None) -> dict[str, dict[str, float]]:
    committed_counts = committed_counts or {}
    state: dict[str, dict[str, float]] = {}
    for row in rows:
        user_id = _text(row.get("user_id"))
        if not user_id:
            continue
        state.setdefault(user_id, {
            "simulated_open_current": _number(row.get("open_current_policy")),
            "simulated_unmanaged_current": _number(row.get("unmanaged_current_policy")),
            "simulated_expired_current": _number(row.get("expired_current_policy")),
            "shadow_received_count": _number(committed_counts.get(user_id, row.get("shadow_received_count", 0))),
        })
    return state


def _state_counts(state: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    return {user_id: int(_number(values.get("shadow_received_count"))) for user_id, values in state.items()}


def _event_sort_key(event: Mapping[str, Any]) -> tuple[str, str]:
    timestamp = _iso(event.get("reassigned_at") or event.get("commit_time") or event.get("created_at") or event.get("timestamp"))
    return timestamp, _text(event.get("decision_id") or event.get("_id"))


async def _load_committed_events(
    db: Any,
    *,
    events: Iterable[Mapping[str, Any]] | None = None,
    read_instrumentation: ReadInstrumentation | None = None,
) -> list[dict[str, Any]]:
    if events is not None:
        return [dict(row) for row in events]
    if db is None:
        from .storage import get_async_db
        db = get_async_db()
    return await _find_many(
        db[LEDGER_COLLECTION],
        {"event_type": COMMITTED_EVENT, "policy_version": POLICY_VERSION},
        {"_id": 1, "event_type": 1, "decision_id": 1, "lead_id": 1, "source_cycle_id": 1,
         "target_owner_user_id": 1, "selected_user_id": 1, "policy_version": 1, "policy_branch": 1,
         "branch": 1, "source_cycle_sla_breached_at": 1, "sla_breached_at": 1,
         "reassigned_at": 1, "commit_time": 1, "created_at": 1, "assignment_number": 1},
        instrumentation=read_instrumentation,
        query_name="worker.distribution.committed_events",
    )


def _post_cutover_committed(events: Iterable[Mapping[str, Any]], cutover_at: datetime, branch: str) -> list[dict[str, Any]]:
    output = []
    for event in events:
        if _text(event.get("policy_branch") or event.get("branch")) != branch:
            continue
        breach = _utc(event.get("source_cycle_sla_breached_at") or event.get("sla_breached_at"))
        if breach and breach >= cutover_at:
            output.append(dict(event))
    return sorted(output, key=_event_sort_key)


async def load_rm_r2_distribution_state(
    db: Any = None,
    *,
    cutover_at: Any,
    events: Iterable[Mapping[str, Any]] | None = None,
    read_instrumentation: ReadInstrumentation | None = None,
) -> dict[str, Any]:
    cutover = _utc(cutover_at)
    if not cutover:
        raise RuntimeError("reassignment cutover unavailable")
    committed = _post_cutover_committed(await _load_committed_events(db, events=events, read_instrumentation=read_instrumentation), cutover, RM_GLOBAL_RESCUE)
    history = [_text(event.get("target_owner_user_id") or event.get("selected_user_id")) for event in committed]
    history = [value for value in history if value]
    counts = Counter(history)
    state_rows = [{"decision_id": _text(event.get("decision_id") or event.get("_id")), "target_user_id": target, "at": _event_sort_key(event)[0]} for event, target in zip(committed, history)]
    return {
        "policy": R2_ROLLING_SHARE, "history": history, "recent_history": history[-R2_HISTORY_WINDOW:],
        "received_counts": dict(counts), "committed_total": len(history),
        "window_size": 20, "prior_window_size": R2_HISTORY_WINDOW, "max_share": 0.40,
        "score_distance": R2_SCORE_DISTANCE, "events": state_rows,
        "distribution_state_version": _json_version(state_rows),
    }


async def load_jpc_distribution_state(
    db: Any = None,
    *,
    cutover_at: Any,
    events: Iterable[Mapping[str, Any]] | None = None,
    maria_user_id: Any = None,
    hernan_user_id: Any = None,
    read_instrumentation: ReadInstrumentation | None = None,
) -> dict[str, Any]:
    cutover = _utc(cutover_at)
    if not cutover:
        raise RuntimeError("reassignment cutover unavailable")
    committed = _post_cutover_committed(await _load_committed_events(db, events=events, read_instrumentation=read_instrumentation), cutover, REGION_JPC_MARIA_HERNAN)
    counts = Counter(_text(event.get("target_owner_user_id") or event.get("selected_user_id")) for event in committed)
    counts.pop("", None)
    total = sum(counts.values())
    rows = [{"decision_id": _text(event.get("decision_id") or event.get("_id")), "target_user_id": target, "at": _event_sort_key(event)[0]} for event, target in zip(committed, (_text(event.get("target_owner_user_id") or event.get("selected_user_id")) for event in committed)) if target]
    return {
        "policy": "J3_PERFORMANCE_WEIGHTED_SHARE", "counts": dict(counts),
        "maria_received": counts.get(_text(maria_user_id), counts.get("maria", 0)) if maria_user_id is not None else counts.get("maria", 0),
        "hernan_received": counts.get(_text(hernan_user_id), counts.get("hernan", 0)) if hernan_user_id is not None else counts.get("hernan", 0),
        "committed_total": total, "observed_share": {key: value / total for key, value in counts.items()} if total else {},
        "min_share": JPC_MIN_SHARE, "max_share": JPC_MAX_SHARE, "events": rows,
        "distribution_state_version": _json_version(rows),
    }


async def load_shadow_distribution_state(
    db: Any = None,
    *,
    cutover_at: Any,
    read_instrumentation: ReadInstrumentation | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reconstruct R2/J3 only from counted shadow observations.

    Productive committed events are intentionally not consulted by this
    path.  The cutover predicate is applied to the hypothetical count time,
    not merely to the source breach, so pre-shadow backlog can never seed the
    prospective distribution state.
    """
    cutover = _utc(cutover_at)
    if not cutover:
        raise RuntimeError("shadow_cutover_unavailable")
    if db is None:
        from .storage import get_async_db
        db = get_async_db()
    rows = await _find_many(
        db[SHADOW_COLLECTION],
        {
            "policy_version": SHADOW_POLICY_VERSION,
            "shadow_version": SHADOW_SCHEMA_VERSION,
            "would_execute": True,
            "shadow_assignment_counted_at": {"$exists": True, "$ne": None, "$gte": cutover},
        },
        {
            "_id": 1, "source_cycle_id": 1, "branch": 1,
            "selected_user_id": 1, "policy_version": 1,
            "shadow_version": 1, "would_execute": 1,
            "shadow_assignment_counted_at": 1, "evaluated_at": 1,
        },
        sort=[("shadow_assignment_counted_at", 1), ("_id", 1)],
        instrumentation=read_instrumentation,
        query_name="worker.distribution.shadow_state",
    )
    rebuilt = reconstruct_shadow_distribution_state(rows, policy_version=SHADOW_POLICY_VERSION, cutover_at=cutover)
    rm_rows = [row for row in rebuilt.get("state_rows", []) if _text(row.get("branch")) == RM_GLOBAL_RESCUE]
    jpc_rows = [row for row in rebuilt.get("state_rows", []) if _text(row.get("branch")) == REGION_JPC_MARIA_HERNAN]
    rm_history = [_text(row.get("target_user_id")) for row in rm_rows if _text(row.get("target_user_id"))]
    jpc_counts = Counter(_text(row.get("target_user_id")) for row in jpc_rows if _text(row.get("target_user_id")))
    rm_state = {
        "policy": R2_ROLLING_SHARE,
        "history": rm_history,
        "recent_history": rm_history[-R2_HISTORY_WINDOW:],
        "received_counts": Counter(rm_history),
        "committed_total": len(rm_history),
        "window_size": 20,
        "prior_window_size": R2_HISTORY_WINDOW,
        "max_share": 0.40,
        "score_distance": R2_SCORE_DISTANCE,
        "events": rm_rows,
        "distribution_state_version": _json_version(rm_rows),
        "source": SHADOW_COLLECTION,
    }
    jpc_total = sum(jpc_counts.values())
    jpc_state = {
        "policy": "J3_PERFORMANCE_WEIGHTED_SHARE",
        "counts": dict(jpc_counts),
        "committed_total": jpc_total,
        "observed_share": {key: value / jpc_total for key, value in jpc_counts.items()} if jpc_total else {},
        "min_share": JPC_MIN_SHARE,
        "max_share": JPC_MAX_SHARE,
        "events": jpc_rows,
        "distribution_state_version": _json_version(jpc_rows),
        "source": SHADOW_COLLECTION,
    }
    return rm_state, jpc_state


async def load_existing_shadow_documents(
    db: Any,
    cycle_ids: Iterable[Any],
    *,
    read_instrumentation: ReadInstrumentation | None = None,
) -> dict[str, dict[str, Any]]:
    """Read the current shadow view once to preserve one-count semantics."""
    ids = [_text(value) for value in cycle_ids if _text(value)]
    if not ids:
        return {}
    rows = await _find_many(
        db[SHADOW_COLLECTION],
        {"source_cycle_id": {"$in": ids}, "policy_version": SHADOW_POLICY_VERSION},
        {"_id": 1, "source_cycle_id": 1, "shadow_assignment_counted_at": 1, "would_execute": 1, "human_management_at": 1},
        instrumentation=read_instrumentation,
        query_name="worker.shadow.existing_cycles",
    )
    return {_text(row.get("source_cycle_id")): dict(row) for row in rows if _text(row.get("source_cycle_id"))}


def r2_guardrail_status(candidate_user_id: Any, candidate_score: Any, alternatives: Iterable[Mapping[str, Any]], prior_history: Iterable[Any]) -> str:
    """Explicit contract for frozen R2: 20th decision, 40%, distance 15."""
    history = [_text(value) for value in prior_history]
    if len(history) < R2_HISTORY_WINDOW:
        return ""
    if history[-R2_HISTORY_WINDOW:].count(_text(candidate_user_id)) < R2_MAX_PRIOR_SHARE_COUNT:
        return ""
    score = _number(candidate_score)
    if any(score - _number(row.get("dynamic_rescue_score")) <= R2_SCORE_DISTANCE for row in alternatives if row.get("performance_data_valid") and _text(row.get("user_id")) != _text(candidate_user_id)):
        return "R2_ROLLING_SHARE_LIMIT"
    return ""


def _select_second(scored: Iterable[Mapping[str, Any]], winner_id: str, score_field: str) -> tuple[str | None, float | None]:
    rows = [row for row in scored if row.get("performance_data_valid") and _text(row.get("user_id")) != winner_id]
    rows.sort(key=lambda row: (-_number(row.get(score_field)), _text(row.get("user_id"))))
    if not rows:
        return None, None
    return _text(rows[0].get("user_id")), _number(rows[0].get(score_field))


def _normalise_team(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    team = dict(snapshot.get("team") or {})
    return {
        "sla_compliance_rate": _number(team.get("sla_compliance_rate", team.get("sla_rate", 0.0))),
        "attention_rate": _number(team.get("attention_rate", 0.0)),
        "team_p50_average": team.get("team_p50_average", team.get("p50")),
        "team_p90_average": team.get("team_p90_average", team.get("p90")),
    }


def _make_decision(lead_row: Mapping[str, Any], cycle: Mapping[str, Any], *, result: Mapping[str, Any], branch: str, breach_at: datetime, cutover: datetime, evaluated_at: datetime, params: RescueParameters) -> Any:
    winner = result.get("winner") or {}
    scored = list(result.get("scored") or [])
    winner_id = _text(winner.get("user_id")) or None
    score_field = "dynamic_rescue_score" if branch == RM_GLOBAL_RESCUE else "global_rescue_score"
    selected_score = _number(winner.get(score_field)) if winner else None
    second_id, second_score = _select_second(scored, winner_id or "", score_field)
    excluded = list(lead_row.get("previous_owner_user_ids") or ())
    decision = build_decision(
        lead_id=lead_row.get("lead_id"), current_assignment_cycle_id=lead_row.get("assignment_cycle_id"),
        previous_owner_user_id=lead_row.get("owner_user_id"), policy_branch=branch,
        candidate_user_ids=[row.get("user_id") for row in scored], selected_user_id=winner_id,
        selected_score=selected_score, selection_reason=_text(result.get("selection_reason")),
        performance_confidence=_text(winner.get("performance_confidence")), assignment_number=_assignment_number(cycle),
        excluded_previous_owners=excluded, management_evidence_checked_at=evaluated_at.isoformat(),
        sla_breached_at=breach_at.isoformat(), evaluated_at=evaluated_at.isoformat(), policy_version=POLICY_VERSION,
        candidate_scores_snapshot=scored, selection_rule="RM_R2" if branch == RM_GLOBAL_RESCUE else "JPC_J3",
        guardrail_applied=bool(result.get("guardrail_reason")), guardrail_reason=_text(result.get("guardrail_reason")),
        jpc_target_share=result.get("jpc_target_share") if branch == REGION_JPC_MARIA_HERNAN else None,
        previous_owner_user_ids=excluded, automatic_reassignment_number=_assignment_number(cycle),
        cycle_version=_text(cycle.get("cycle_version")), reassignment_cutover_at=cutover.isoformat(),
        source_cycle_sla_breached_at=breach_at.isoformat(), cutover_eligible=True,
        cutover_policy_version=CUTOVER_POLICY_VERSION,
    )
    payload = decision.to_dict()
    payload["selected_user_display_name"] = _text(winner.get("executive")) if winner else ""
    payload["second_user_id"] = second_id
    payload["second_score"] = second_score
    return payload


def _shadow_error(cycle: Mapping[str, Any], *, now: datetime, cutover: datetime, reason: str, branch: str = "") -> SLAReassignmentShadowEvaluation:
    return SLAReassignmentShadowEvaluation(
        evaluated_at=now.isoformat(), lead_id=_text(cycle.get("lead_id")), source_cycle_id=_text(cycle.get("assignment_cycle_id")),
        breach_at="", cutover=cutover.isoformat(), branch=branch, eligible=False, exclusion_reason=reason,
        assignment_number=_assignment_number(cycle), decision_id=generate_decision_id(cycle.get("lead_id"), cycle.get("assignment_cycle_id"), POLICY_VERSION),
    )


def _policy_selection(lead_row: dict[str, Any], cycle: Mapping[str, Any], *, branch: str, state: dict[str, dict[str, float]], rm_history: list[str], jpc_counts: Counter[str], jpc_total: int, jpc_targets: Mapping[str, float], team: Mapping[str, Any], params: RescueParameters) -> dict[str, Any]:
    if branch == RM_GLOBAL_RESCUE:
        return _rm_selection(lead_row, state, rm_history, scenario=R2_ROLLING_SHARE, low_policy=L1_LOW_NEEDS_PLUS_5, team=team, params=params)
    if branch == REGION_JPC_MARIA_HERNAN:
        result = _jpc_selection(lead_row, state, jpc_counts, jpc_total, jpc_targets, team=team, params=params)
        result["jpc_target_share"] = dict(jpc_targets)
        return result
    return {"winner": None, "scored": [], "hard_excluded": [], "selection_reason": branch}


async def run_sla_reassignment_worker_iteration(
    db: Any = None,
    *,
    now: Any = None,
    cutover_at: Any = None,
    shadow_cutover_at: Any = None,
    batch_size: int | None = None,
    page_token: Mapping[str, Any] | None = None,
    scan_result: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    performance_snapshot: Mapping[str, Any] | None = None,
    performance_cache: PerformanceSnapshotCache | None = None,
    committed_events: Iterable[Mapping[str, Any]] | None = None,
    jpc_horizon_total: int | None = None,
    read_instrumentation: ReadInstrumentation | None = None,
    worker_instance_id: str = "",
    batch_id: str = "",
) -> dict[str, Any]:
    """Run exactly one prospective worker iteration.

    The function is fail-closed for configuration/DB/snapshot failures and
    isolates malformed individual cycles.  It never imports or calls the
    transaction executor.
    """
    started = time.perf_counter()
    result = SLAWorkerIterationResult(status="disabled")
    if not bool(_cfg("CRM_SLA_REASSIGNMENT_WORKER_ENABLED", False)):
        result.status = "disabled"
        result.duration_ms = (time.perf_counter() - started) * 1000.0
        return {"status": result.status, "iteration": result.to_dict(), "evaluations": [], "decisions": []}
    if not bool(_cfg("CRM_SLA_REASSIGNMENT_SHADOW_ENABLED", False)):
        result.status = "shadow_required"
        result.duration_ms = (time.perf_counter() - started) * 1000.0
        return {"status": result.status, "iteration": result.to_dict(), "evaluations": [], "decisions": []}
    if bool(_cfg("CRM_SLA_REASSIGNMENT_ENABLED", False)):
        result.status = "shadow_execution_guard_abort"
        result.errors += 1
        result.error_codes.append("MASTER_REASSIGNMENT_MUST_REMAIN_DISABLED")
        result.duration_ms = (time.perf_counter() - started) * 1000.0
        logger.critical("[SLA_SHADOW_ERROR] SHADOW_EXECUTOR_GUARD_ABORT master_reassignment=true")
        return {"status": result.status, "iteration": result.to_dict(), "evaluations": [], "decisions": [], "executor_calls": 0, "business_writes": 0}
    as_of = _utc(now) or utc_now()
    using_shadow_cutover = cutover_at is None
    shadow_state_mode = shadow_cutover_at is not None or using_shadow_cutover
    if cutover_at is not None:
        cutover = _utc(cutover_at)
    else:
        configured_shadow_cutover = shadow_cutover_at if shadow_cutover_at is not None else _cfg("CRM_SLA_REASSIGNMENT_SHADOW_CUTOVER_AT", None)
        try:
            from .crm_sla_reassignment_cutover import parse_explicit_santiago_timestamp
            cutover = parse_explicit_santiago_timestamp(configured_shadow_cutover)
        except Exception:
            cutover = None
    if not cutover:
        result.status = "fatal_configuration_error"
        result.error_codes.append("CUTOVER_CONFIGURATION_INVALID")
        result.duration_ms = (time.perf_counter() - started) * 1000.0
        return {"status": result.status, "iteration": result.to_dict(), "evaluations": [], "decisions": []}
    try:
        size = _validated_batch_size(batch_size)
        if scan_result is None:
            scan_result = await scan_sla_reassignment_candidates(
                db, batch_size=size, page_token=page_token,
                read_instrumentation=read_instrumentation,
            )
        cycles = [dict(row) for row in scan_result.get("cycles", [])]
        result.scanned = len(cycles)
        if context is None:
            if db is None:
                from .storage import get_async_db
                db = get_async_db()
            context = await _batch_context(db, cycles, read_instrumentation=read_instrumentation)
        if performance_snapshot is None:
            try:
                    performance_snapshot = await _default_performance_snapshot(
                        as_of, db=db, performance_cache=performance_cache,
                        read_instrumentation=read_instrumentation,
                        shadow_live_capacity=True,
                    )
            except Exception:
                result.status = "snapshot_unavailable"
                result.errors += 1
                result.error_codes.append("SNAPSHOT_UNAVAILABLE")
                result.duration_ms = (time.perf_counter() - started) * 1000.0
                logger.error("%s outcome=SNAPSHOT_UNAVAILABLE", WORKER_LOG_PREFIX)
                return {"status": result.status, "iteration": result.to_dict(), "evaluations": [], "decisions": []}
        snapshot = dict(performance_snapshot)
        params = RescueParameters(performance_window_days=PERFORMANCE_WINDOW_DAYS)
        team = _normalise_team(snapshot)
        all_candidates = list(snapshot.get("candidates") or [])
        if shadow_state_mode and db is None:
            # Unit/replay callers may inject the complete scan context without
            # a database.  Production startup always supplies the guarded DB;
            # the injected path starts from an empty prospective state.
            rm_state = {"history": [], "recent_history": [], "received_counts": {}, "committed_total": 0, "distribution_state_version": "shadow-empty", "source": "injected-empty"}
            jpc_state = {"counts": {}, "committed_total": 0, "observed_share": {}, "distribution_state_version": "shadow-empty", "source": "injected-empty"}
        elif shadow_state_mode:
            rm_state, jpc_state = await load_shadow_distribution_state(
                db, cutover_at=cutover, read_instrumentation=read_instrumentation,
            )
        else:
            rm_state = await load_rm_r2_distribution_state(
                db, cutover_at=cutover, events=committed_events,
                read_instrumentation=read_instrumentation,
            )
            maria_id = next((_text(row.get("user_id")) for row in (performance_snapshot or {}).get("candidates", []) if _identity(row.get("identity_key") or row.get("executive_key")) == "maria paz galleguillos"), None)
            hernan_id = next((_text(row.get("user_id")) for row in (performance_snapshot or {}).get("candidates", []) if _identity(row.get("identity_key") or row.get("executive_key")) == "hernan castro"), None)
            jpc_state = await load_jpc_distribution_state(
                db, cutover_at=cutover, events=committed_events,
                maria_user_id=maria_id, hernan_user_id=hernan_id,
                read_instrumentation=read_instrumentation,
            )
        maria_id = next((_text(row.get("user_id")) for row in (performance_snapshot or {}).get("candidates", []) if _identity(row.get("identity_key") or row.get("executive_key")) == "maria paz galleguillos"), None)
        hernan_id = next((_text(row.get("user_id")) for row in (performance_snapshot or {}).get("candidates", []) if _identity(row.get("identity_key") or row.get("executive_key")) == "hernan castro"), None)
        jpc_state["maria_received"] = jpc_state.get("counts", {}).get(_text(maria_id), 0) if maria_id else 0
        jpc_state["hernan_received"] = jpc_state.get("counts", {}).get(_text(hernan_id), 0) if hernan_id else 0
        committed_counts = Counter(rm_state.get("received_counts") or {})
        committed_counts.update(jpc_state.get("counts") or {})
        shadow_state = _initial_candidate_state(all_candidates, committed_counts)
        rm_history = list(rm_state.get("history") or [])
        jpc_counts = Counter(jpc_state.get("counts") or {})
        future_jpc_count = 0
        lead_rows: list[tuple[dict[str, Any], dict[str, Any], CanonicalExpiration, ManagementProtection, str]] = []
        protection_by_cycle: dict[str, ManagementProtection] = {}
        existing_shadow = await load_existing_shadow_documents(
            db, [_text(row.get("assignment_cycle_id")) for row in cycles],
            read_instrumentation=read_instrumentation,
        ) if db is not None else {}
        for cycle in cycles:
            lead = (context.get("leads_by_id") or {}).get(_text(cycle.get("lead_id")))
            if lead:
                try:
                    exp = canonical_expiration_recheck(cycle, lead, now=as_of)
                    events = (context.get("events_by_lead") or {}).get(_text(cycle.get("lead_id")), [])
                    results = (context.get("results_by_cycle") or {}).get(_text(cycle.get("assignment_cycle_id")), [])
                    protection = _management_protection(cycle, lead, events, results, breach_at=exp.breach_at)
                    row = _policy_row(cycle, lead, snapshot, state_counts=_state_counts(shadow_state))
                    lead_rows.append((row, cycle, exp, protection, _owner_cycle_issue(cycle, lead, (context.get("active_cycles_by_lead") or {}).get(_text(cycle.get("lead_id")), [cycle]), snapshot.get("users_by_id") or {})))
                    protection_by_cycle[_text(cycle.get("assignment_cycle_id"))] = protection
                    if row.get("policy_category") == REGION_JPC_MARIA_HERNAN:
                        future_jpc_count += 1
                except Exception as exc:
                    result.errors += 1
                    result.error_codes.append("LEAD_EVALUATION_ERROR")
                    logger.warning("%s cycle_id=%s lead_id=%s outcome=error error_type=%s", WORKER_LOG_PREFIX, _text(cycle.get("assignment_cycle_id")), _text(cycle.get("lead_id")), type(exc).__name__)
            else:
                lead_rows.append(({}, cycle, canonical_expiration_recheck(cycle, None, now=as_of), ManagementProtection(False), "DATA_INSUFFICIENT"))
                result.errors += 1
                result.error_codes.append("LEAD_NOT_FOUND")
        ordered = sorted(lead_rows, key=lambda item: _worker_order_key(item[0] or {"lead_id": item[1].get("lead_id"), "assignment_cycle_id": item[1].get("assignment_cycle_id"), "assigned_at": item[1].get("assigned_at"), "temperature": item[1].get("temperature_at_assignment"), "current_overdue_business_minutes": 0}))
        jpc_targets: dict[str, float] = {}
        jpc_template = [dict(row) for row in all_candidates if _identity(row.get("identity_key") or row.get("executive_key")) in {"maria paz galleguillos", "hernan castro"}]
        if jpc_template:
            target_info = derive_jpc_share_targets(jpc_template, team_sla_rate=team["sla_compliance_rate"], team_attention_rate=team["attention_rate"], team_p50_average=team["team_p50_average"], team_p90_average=team["team_p90_average"], params=params)
            jpc_targets = dict(target_info.get("targets") or {})
        evaluations: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        perf_version = _text(snapshot.get("performance_snapshot_version"))
        dist_version = sha256((_text(rm_state.get("distribution_state_version")) + _text(jpc_state.get("distribution_state_version"))).encode("utf-8")).hexdigest()
        for lead_row, cycle, exp, protection, owner_status in ordered:
            try:
                lead_id = _text(cycle.get("lead_id"))
                cycle_id = _text(cycle.get("assignment_cycle_id"))
                decision_id = generate_decision_id(lead_id, cycle_id, POLICY_VERSION)
                # A canonical stop before the calculated breach means the
                # cycle is not actually expired under the current evaluator.
                # A stop after the breach remains an SLA miss and is handled
                # by the protection branch below.
                stop_before_breach = bool(
                    protection.current_stop_at
                    and exp.breach_at
                    and protection.current_stop_at <= exp.breach_at
                )
                if not exp.started_at or not exp.expired or stop_before_breach:
                    result.not_actually_expired += 1
                    evaluations.append(_shadow_error(cycle, now=as_of, cutover=cutover, reason="NOT_ACTUALLY_EXPIRED").to_dict())
                    continue
                result.canonically_expired += 1
                if exp.breach_at and exp.breach_at < cutover:
                    result.pre_cutover_skipped += 1
                    evaluations.append(SLAReassignmentShadowEvaluation(as_of.isoformat(), lead_id, cycle_id, exp.breach_at.isoformat(), cutover.isoformat(), "", False, "PRE_SHADOW_CUTOVER_ALREADY_EXPIRED" if using_shadow_cutover else "PRE_CUTOVER_ALREADY_EXPIRED", assignment_number=_assignment_number(cycle), decision_id=decision_id, current_overdue_business_minutes=exp.overdue_business_minutes, temperature=exp.temperature, performance_snapshot_version=perf_version, distribution_state_version=dist_version, performance_snapshot_id=perf_version, capacity_snapshot_at=_text(snapshot.get("capacity_snapshot_at")), r2_state_snapshot={"state_version": _text(rm_state.get("distribution_state_version")), "recent_history": list(rm_state.get("recent_history") or [])}, j3_target_share_snapshot={"state_version": _text(jpc_state.get("distribution_state_version")), "observed_share": dict(jpc_state.get("observed_share") or {})}).to_dict())
                    continue
                if owner_status != "OWNER_OK" or not _lead_is_open((context.get("leads_by_id") or {}).get(lead_id)):
                    result.review_skipped += 1
                    reason = owner_status if owner_status != "OWNER_OK" else "LEAD_CLOSED"
                    evaluations.append(SLAReassignmentShadowEvaluation(as_of.isoformat(), lead_id, cycle_id, exp.breach_at.isoformat() if exp.breach_at else "", cutover.isoformat(), "", False, reason, assignment_number=_assignment_number(cycle), decision_id=decision_id, current_overdue_business_minutes=exp.overdue_business_minutes, temperature=exp.temperature, performance_snapshot_version=perf_version, distribution_state_version=dist_version).to_dict())
                    continue
                if protection.protected:
                    result.protected_skipped += 1
                    evaluations.append(SLAReassignmentShadowEvaluation(as_of.isoformat(), lead_id, cycle_id, exp.breach_at.isoformat() if exp.breach_at else "", cutover.isoformat(), "", False, "PROTECTED_BY_MANAGEMENT", assignment_number=_assignment_number(cycle), decision_id=decision_id, current_overdue_business_minutes=exp.overdue_business_minutes, temperature=exp.temperature, performance_snapshot_version=perf_version, distribution_state_version=dist_version).to_dict())
                    continue
                assignment_number = _assignment_number(cycle)
                if assignment_number >= 2:
                    result.max2_skipped += 1
                    evaluations.append(SLAReassignmentShadowEvaluation(as_of.isoformat(), lead_id, cycle_id, exp.breach_at.isoformat() if exp.breach_at else "", cutover.isoformat(), "", False, SUPERVISOR_REVIEW_REQUIRED, assignment_number=assignment_number, decision_id=decision_id, current_overdue_business_minutes=exp.overdue_business_minutes, temperature=exp.temperature, performance_snapshot_version=perf_version, distribution_state_version=dist_version).to_dict())
                    continue
                branch = _text(lead_row.get("policy_category"))
                if branch == REGIONAL_POLICY_NOT_DEFINED:
                    result.undefined_skipped += 1
                    reason = REGIONAL_POLICY_NOT_DEFINED
                    evaluations.append(SLAReassignmentShadowEvaluation(as_of.isoformat(), lead_id, cycle_id, exp.breach_at.isoformat() if exp.breach_at else "", cutover.isoformat(), branch, False, reason, assignment_number=assignment_number, decision_id=decision_id, current_overdue_business_minutes=exp.overdue_business_minutes, temperature=exp.temperature, performance_snapshot_version=perf_version, distribution_state_version=dist_version).to_dict())
                    continue
                if branch not in {RM_GLOBAL_RESCUE, REGION_JPC_MARIA_HERNAN}:
                    result.review_skipped += 1
                    evaluations.append(SLAReassignmentShadowEvaluation(as_of.isoformat(), lead_id, cycle_id, exp.breach_at.isoformat() if exp.breach_at else "", cutover.isoformat(), branch, False, REGION_REVIEW_REQUIRED, assignment_number=assignment_number, decision_id=decision_id, current_overdue_business_minutes=exp.overdue_business_minutes, temperature=exp.temperature, performance_snapshot_version=perf_version, distribution_state_version=dist_version).to_dict())
                    continue
                lead_row["rm_candidates"] = [dict(row) for row in lead_row.get("rm_candidates", [])]
                lead_row["jpc_candidates"] = [dict(row) for row in lead_row.get("jpc_candidates", [])]
                counts = _state_counts(shadow_state)
                for candidate in lead_row["rm_candidates"] + lead_row["jpc_candidates"]:
                    candidate["shadow_received_count"] = counts.get(_text(candidate.get("user_id")), 0)
                selection = _policy_selection(lead_row, cycle, branch=branch, state=shadow_state, rm_history=rm_history, jpc_counts=jpc_counts, jpc_total=max(1, int(jpc_horizon_total or (int(jpc_state.get("committed_total", 0)) + future_jpc_count))), jpc_targets=jpc_targets, team=team, params=params)
                winner = selection.get("winner") or {}
                scored = list(selection.get("scored") or [])
                candidate_ids = tuple(_text(row.get("user_id")) for row in scored if _text(row.get("user_id")))
                selected_id = _text(winner.get("user_id")) if winner else None
                score_field = "dynamic_rescue_score" if branch == RM_GLOBAL_RESCUE else "global_rescue_score"
                selected_score = _number(winner.get(score_field)) if winner else None
                second_id, second_score = _select_second(scored, selected_id or "", score_field)
                margin = selected_score - second_score if selected_id and second_score is not None and selected_score is not None else None
                reason = _text(selection.get("selection_reason")) or ("NO_ELIGIBLE_RESCUER" if branch == RM_GLOBAL_RESCUE else "NO_ELIGIBLE_JPC_RESCUER")
                decision = _make_decision(lead_row, cycle, result=selection, branch=branch, breach_at=exp.breach_at, cutover=cutover, evaluated_at=as_of, params=params)
                already_counted = bool((existing_shadow.get(cycle_id) or {}).get("shadow_assignment_counted_at"))
                if selected_id:
                    result.evaluated += 1
                    result.would_reassign += 1
                    if not already_counted:
                        if branch == RM_GLOBAL_RESCUE:
                            rm_history.append(selected_id)
                        else:
                            jpc_counts[selected_id] += 1
                        _update_state(shadow_state, winner)
                    decisions.append(decision)
                    logger.info("%s cycle_id=%s lead_id=%s branch=%s outcome=would_reassign target_id=%s decision_id=%s duration_ms=%.2f", WORKER_LOG_PREFIX, cycle_id, lead_id, branch, selected_id, decision_id, (time.perf_counter() - started) * 1000.0)
                else:
                    result.evaluated += 1
                evaluations.append(SLAReassignmentShadowEvaluation(as_of.isoformat(), lead_id, cycle_id, exp.breach_at.isoformat() if exp.breach_at else "", cutover.isoformat(), branch, True, "" if selected_id else reason, candidate_ids, selected_id, selected_score, second_id, margin, _text(selection.get("guardrail_reason")), _text(winner.get("performance_confidence")), assignment_number, decision_id, bool(selected_id), True, exp.overdue_business_minutes, exp.temperature, perf_version, dist_version, perf_version, _text(snapshot.get("capacity_snapshot_at")), {"state_version": _text(rm_state.get("distribution_state_version")), "recent_history": list(rm_state.get("recent_history") or [])}, {"state_version": _text(jpc_state.get("distribution_state_version")), "targets": dict(jpc_targets), "observed_share": dict(jpc_state.get("observed_share") or {})}).to_dict())
            except Exception as exc:
                result.errors += 1
                result.error_codes.append("LEAD_EVALUATION_ERROR")
                evaluations.append(_shadow_error(cycle, now=as_of, cutover=cutover, reason="LEAD_EVALUATION_ERROR").to_dict())
                logger.warning("%s cycle_id=%s lead_id=%s outcome=error error_type=%s", WORKER_LOG_PREFIX, _text(cycle.get("assignment_cycle_id")), _text(cycle.get("lead_id")), type(exc).__name__)
        persistence_status = "NOT_ATTEMPTED"
        persistence_writes = 0
        persistence_ms = 0.0
        new_would_execute = sum(
            1 for evaluation in evaluations
            if evaluation.get("would_execute") and not (existing_shadow.get(_text(evaluation.get("source_cycle_id"))) or {}).get("shadow_assignment_counted_at")
        )
        if db is not None:
            persist_started = time.perf_counter()
            decision_by_id = {_text(row.get("decision_id")): row for row in decisions}
            documents = []
            for evaluation in evaluations:
                cycle_id = _text(evaluation.get("source_cycle_id"))
                prior = existing_shadow.get(cycle_id) or {}
                counted_at = _utc(prior.get("shadow_assignment_counted_at"))
                protection = protection_by_cycle.get(cycle_id)
                human_after = protection.first_human_at if protection and counted_at and protection.first_human_at and protection.first_human_at >= counted_at else None
                evaluation_payload = dict(evaluation)
                evaluation_payload["performance_snapshot"] = {
                    "id": _text(snapshot.get("performance_snapshot_version")),
                    "live_capacity_variant": _text(snapshot.get("live_capacity_variant") or A1_NAME),
                    "iteration_duration_ms": round(result.duration_ms, 3),
                }
                documents.append(build_shadow_document(
                    evaluation_payload,
                    decision=decision_by_id.get(_text(evaluation.get("decision_id"))),
                    worker_instance_id=worker_instance_id,
                    batch_id=batch_id,
                    human_management_at=human_after,
                    capacity_snapshot_at=snapshot.get("capacity_snapshot_at"),
                ))
            try:
                persisted = await persist_shadow_evaluations(db, documents, dry_run=False)
                persistence_status = _text(persisted.get("status"))
                persistence_writes = int(persisted.get("writes") or 0)
            except Exception as exc:
                persistence_status = "SHADOW_STORAGE_FAILURE_FAIL_CLOSED"
                result.errors += 1
                result.error_codes.append("SHADOW_STORAGE_FAILURE")
                logger.error("[SLA_SHADOW_ERROR] outcome=SHADOW_STORAGE_FAILURE error_type=%s", type(exc).__name__)
            persistence_ms = (time.perf_counter() - persist_started) * 1000.0
        result.status = "shadow" if persistence_status != "SHADOW_STORAGE_FAILURE_FAIL_CLOSED" else "shadow_storage_failed"
        result.duration_ms = (time.perf_counter() - started) * 1000.0
        logger.info("[SLA_SHADOW_ITERATION] status=%s scanned=%s canonically_expired=%s pre_shadow_skipped=%s protected=%s evaluated=%s would_execute=%s shadow_writes=%s duration_ms=%.2f", result.status, result.scanned, result.canonically_expired, result.pre_cutover_skipped, result.protected_skipped, result.evaluated, result.would_reassign, persistence_writes, result.duration_ms)
        return {
            "status": result.status, "iteration": result.to_dict(), "evaluations": evaluations, "decisions": decisions,
            "next_page_token": scan_result.get("next_page_token"), "batch_size": size,
            "performance_snapshot_version": perf_version, "distribution_state_version": dist_version,
            "performance_docs_examined": snapshot.get("docs_examined", 0), "performance_read_ms": snapshot.get("read_ms"),
            "query_documents_examined": scan_result.get("documents_examined", 0),
            "shadow_state_counts": _state_counts(shadow_state),
            "shadow_storage_status": persistence_status,
            "shadow_storage_writes": persistence_writes,
            "shadow_storage_ms": persistence_ms,
            "new_shadow_would_execute": new_would_execute,
            "executor_calls": 0,
            "business_writes": 0,
        }
    except Exception as exc:
        result.status = "fatal_iteration_error"
        result.errors += 1
        result.error_codes.append(type(exc).__name__)
        result.duration_ms = (time.perf_counter() - started) * 1000.0
        logger.error("%s outcome=fatal error_type=%s", WORKER_LOG_PREFIX, type(exc).__name__)
        return {"status": result.status, "iteration": result.to_dict(), "evaluations": [], "decisions": []}


async def crm_sla_reassignment_worker_loop(*, stop_event: asyncio.Event | None = None, **iteration_kwargs: Any) -> None:
    """Optional future loop; never scheduled or imported by application startup."""
    if not bool(_cfg("CRM_SLA_REASSIGNMENT_WORKER_ENABLED", False)) or not bool(_cfg("CRM_SLA_REASSIGNMENT_SHADOW_ENABLED", False)):
        return
    stop_event = stop_event or asyncio.Event()
    interval = _validated_interval(iteration_kwargs.pop("interval_seconds", None))
    while not stop_event.is_set():
        await run_sla_reassignment_worker_iteration(**iteration_kwargs)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def run_sla_reassignment_iteration_with_leader_lease(
    db: Any = None,
    *,
    holder_id: str,
    lease_write_enabled: bool = False,
    lease_settings: Any = None,
    **iteration_kwargs: Any,
) -> dict[str, Any]:
    """Future runner gate: acquire the global lease before evaluating.

    ``lease_write_enabled`` is deliberately false by default.  The preparation
    phase therefore cannot create ``crm_worker_leases`` or advance it by
    accident.  A future supervised deployment must opt in explicitly.
    """
    from .crm_sla_worker_lease import LeaseSettings, acquire_leader_lease

    settings = lease_settings or LeaseSettings()
    lease = await acquire_leader_lease(
        db, holder_id=holder_id, settings=settings,
        write_enabled=lease_write_enabled,
    )
    if lease.get("status") != "ACQUIRED":
        return {
            "status": "lease_not_held",
            "lease": lease,
            "iteration": {"status": "LEASE_NOT_HELD", "scanned": 0, "evaluated": 0, "would_reassign": 0},
            "evaluations": [], "decisions": [],
        }
    result = await run_sla_reassignment_worker_iteration(db=db, **iteration_kwargs)
    result["lease"] = lease
    return result


def multi_worker_strategy() -> dict[str, Any]:
    return {
        "evaluation_delivery": "at_least_once",
        "recommended": "global_leader_lease_plus_deterministic_selector_with_versioned_snapshots_plus_idempotent_executor_CAS",
        "lease": "designed_but_not_enabled_productively",
        "lease_key": "crm_sla_reassignment_worker_v1",
        "lease_duration_seconds": 120,
        "lease_heartbeat_seconds": 30,
        "options": {
            "A": "global_leader_lease_initial",
            "B": "Mongo_TTL_lease_per_cycle_future_scale_out",
            "C": "no_lease_executor_idempotent_only_as_defense",
        },
        "different_winner_race": "same_decision_id_different_payload_must_be_rejected_or_reviewed_by_executor",
        "mitigation_before_production": "acquire_global_leader_lease; keep_decision_id_and_snapshot_versions; executor_CAS_is_final_authority",
    }


def worker_configuration_snapshot() -> dict[str, Any]:
    return {
        "worker_enabled": bool(_cfg("CRM_SLA_REASSIGNMENT_WORKER_ENABLED", False)),
        "shadow_enabled": bool(_cfg("CRM_SLA_REASSIGNMENT_SHADOW_ENABLED", False)),
        "interval_seconds": int(_cfg("CRM_SLA_REASSIGNMENT_WORKER_INTERVAL_SECONDS", 60)),
        "batch_size": int(_cfg("CRM_SLA_REASSIGNMENT_BATCH_SIZE", 25)),
        "policy_version": POLICY_VERSION,
        "cutover_source": "CRM_SLA_REASSIGNMENT_SHADOW_CUTOVER_AT",
        "production_cutover_source": "CRM_SLA_REASSIGNMENT_CUTOVER_AT",
        "shadow_cutover_at": _text(_cfg("CRM_SLA_REASSIGNMENT_SHADOW_CUTOVER_AT", None)) or None,
        "production_cutover_at": _text(_cfg("CRM_SLA_REASSIGNMENT_CUTOVER_AT", None)) or None,
        "executor_connected": False,
        "startup_connected": False,
        "historical_snapshot_ttl_seconds": DEFAULT_HISTORICAL_TTL_SECONDS,
        "live_capacity_snapshot_ttl_seconds": DEFAULT_LIVE_CAPACITY_TTL_SECONDS,
        "shadow_collection": "crm_sla_reassignment_shadow_v1",
        "lease_collection": "crm_worker_leases",
        "lease_key": "crm_sla_reassignment_worker_v1",
        "live_capacity_variant": A1_NAME,
        "performance_duration_guard_seconds": 5,
        "executor_calls": 0,
    }


def validate_worker_configuration() -> dict[str, Any]:
    """Pure startup-like validation; it is not connected to application startup."""
    from .crm_sla_reassignment_shadow import validate_shadow_configuration
    return validate_shadow_configuration(
        worker_enabled=bool(_cfg("CRM_SLA_REASSIGNMENT_WORKER_ENABLED", False)),
        shadow_enabled=bool(_cfg("CRM_SLA_REASSIGNMENT_SHADOW_ENABLED", False)),
        reassignment_enabled=bool(_cfg("CRM_SLA_REASSIGNMENT_ENABLED", False)),
        shadow_cutover_at=_cfg("CRM_SLA_REASSIGNMENT_SHADOW_CUTOVER_AT", None),
        production_cutover_at=_cfg("CRM_SLA_REASSIGNMENT_CUTOVER_AT", None),
        policy_version=POLICY_VERSION,
        security_enabled=bool(_cfg("CRM_SLA_SECURITY_LAYER_ENABLED", False)),
        transaction_gate_enabled=bool(_cfg("CRM_SLA_TRANSACTION_GATE_ENABLED", False)),
    )
