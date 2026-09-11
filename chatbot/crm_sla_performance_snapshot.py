"""Read-only performance and live-capacity snapshots for the SLA worker.

The historical snapshot is intentionally independent from the live-capacity
snapshot.  Historical metrics may remain cached for several minutes, while
current open/unmanaged/expired counts can refresh more frequently.  This
module has no import-time database access and does not create indexes or write
operational state.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping

from .crm_sla_snapshot_queries import ReadInstrumentation, find_many


HISTORICAL_TTL_CANDIDATES = (60, 120, 300, 600)
DEFAULT_HISTORICAL_TTL_SECONDS = 300
DEFAULT_LIVE_CAPACITY_TTL_SECONDS = 60


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
    # PyMongo's default BSON codec returns naive UTC datetimes.  The
    # productive CRM parser treats those values as UTC; rejecting them here
    # would silently discard valid management evidence from the live batch.
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: Any) -> str:
    parsed = _utc(value)
    return parsed.isoformat() if parsed else str(value or "")


def _path_value(document: Mapping[str, Any], path: str) -> Any:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _version(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _cursor_rows(cursor: Any, limit: int | None = None) -> list[dict[str, Any]]:
    if limit is not None and hasattr(cursor, "limit"):
        cursor = cursor.limit(limit)
    if hasattr(cursor, "to_list"):
        return list(await _maybe_await(cursor.to_list(length=limit)) or [])
    return list(cursor or [])


async def _find_many(
    collection: Any,
    query: Mapping[str, Any],
    projection: Mapping[str, Any] | None = None,
    *,
    limit: int | None = None,
    sort: list[tuple[str, int]] | None = None,
    instrumentation: ReadInstrumentation | None = None,
    query_name: str = "snapshot.find",
) -> list[dict[str, Any]]:
    return await find_many(
        collection, query, projection, limit=limit, sort=sort,
        instrumentation=instrumentation, query_name=query_name,
    )


EVENT_PROJECTION = {"_id": 1, "lead_id": 1, "assignment_cycle_id": 1, "type": 1, "actor": 1, "actor_type": 1, "confirmed": 1, "result": 1, "meta": 1, "timestamp": 1, "occurred_at": 1}
MANAGEMENT_EVENT_TYPES = (
    "GESTION_LOG", "HUMAN_NOTE", "CONTACT_RESULT", "MANUAL_ENTRY",
    "gestion_log", "human_note", "contact_result", "manual_entry",
)


async def _events_for_leads(
    collection: Any,
    lead_ids: list[Any],
    *,
    since: datetime,
    until: datetime,
    instrumentation: ReadInstrumentation | None = None,
    query_prefix: str = "snapshot.events",
) -> list[dict[str, Any]]:
    """Read timestamped and legacy-timestamp events in one bounded set read."""
    if not lead_ids:
        return []
    rows = await _find_many(
        collection,
        {
            "lead_id": {"$in": lead_ids},
            "type": {"$in": MANAGEMENT_EVENT_TYPES},
            "$or": [
                {"timestamp": {"$gte": since, "$lte": until}},
                {"timestamp": {"$exists": False}},
            ],
        },
        EVENT_PROJECTION,
        instrumentation=instrumentation,
        query_name=f"{query_prefix}.timestamp_or_legacy_window",
    )
    output = [row for row in rows if row.get("timestamp") is not None]
    seen = {str((row.get("_id"), row.get("lead_id"), row.get("type"), row.get("timestamp"), row.get("occurred_at"))) for row in output}
    for row in rows:
        if row.get("timestamp") is not None:
            continue
        occurred = _utc(row.get("occurred_at"))
        if not occurred or occurred < since or occurred > until:
            continue
        key = str((row.get("_id"), row.get("lead_id"), row.get("type"), row.get("timestamp"), row.get("occurred_at")))
        if key not in seen:
            output.append(row)
            seen.add(key)
    return output


@dataclass(frozen=True)
class HistoricalPerformanceSnapshot:
    snapshot_id: str
    generated_at: str
    expires_at: str
    policy_version: str
    source_watermarks: Mapping[str, Any] = field(default_factory=dict)
    executive_metrics: Mapping[str, Any] = field(default_factory=dict)
    team_metrics: Mapping[str, Any] = field(default_factory=dict)
    docs_examined: int = 0
    read_ms: float = 0.0
    payload: Mapping[str, Any] = field(default_factory=dict)

    def is_fresh(self, *, now: Any, source_watermarks: Mapping[str, Any] | None = None) -> bool:
        current = _utc(now)
        expires = _utc(self.expires_at)
        if not current or not expires or current >= expires:
            return False
        return source_watermarks is None or dict(source_watermarks) == dict(self.source_watermarks)

    def to_dict(self) -> dict[str, Any]:
        # The historical payload contains read-only indexed records used by
        # the selector.  Deep-copying every evidence/property object on every
        # cache hit made the hit path itself exceed its latency target.  Copy
        # the mutable top-level metric containers and keep the indexed read
        # maps shared for this immutable snapshot representation.
        result = dict(self.payload)
        for key in ("candidates", "team", "executive_metrics", "team_metrics"):
            if key in result:
                result[key] = copy.deepcopy(result[key])
        for key in ("metrics", "agents", "users_by_id", "records_by_cycle", "properties", "catalog"):
            if key in result and isinstance(result[key], Mapping):
                result[key] = dict(result[key])
        result.update({
            "snapshot_id": self.snapshot_id,
            "generated_at": self.generated_at,
            "expires_at": self.expires_at,
            "policy_version": self.policy_version,
            "source_watermarks": copy.deepcopy(dict(self.source_watermarks)),
            "executive_metrics": copy.deepcopy(dict(self.executive_metrics)),
            "team_metrics": copy.deepcopy(dict(self.team_metrics)),
            "docs_examined": self.docs_examined,
            "read_ms": self.read_ms,
            "snapshot_kind": "historical_performance",
        })
        return result


@dataclass(frozen=True)
class LiveCapacitySnapshot:
    snapshot_id: str
    generated_at: str
    expires_at: str
    policy_version: str
    source_watermarks: Mapping[str, Any] = field(default_factory=dict)
    capacity_metrics: Mapping[str, Any] = field(default_factory=dict)
    docs_examined: int = 0
    read_ms: float = 0.0
    payload: Mapping[str, Any] = field(default_factory=dict)

    def is_fresh(self, *, now: Any, source_watermarks: Mapping[str, Any] | None = None) -> bool:
        current = _utc(now)
        expires = _utc(self.expires_at)
        if not current or not expires or current >= expires:
            return False
        return source_watermarks is None or dict(source_watermarks) == dict(self.source_watermarks)

    def to_dict(self) -> dict[str, Any]:
        result = copy.deepcopy(dict(self.payload))
        result.update({
            "snapshot_id": self.snapshot_id,
            "generated_at": self.generated_at,
            "expires_at": self.expires_at,
            "policy_version": self.policy_version,
            "source_watermarks": copy.deepcopy(dict(self.source_watermarks)),
            "capacity_metrics": copy.deepcopy(dict(self.capacity_metrics)),
            "docs_examined": self.docs_examined,
            "read_ms": self.read_ms,
            "snapshot_kind": "live_capacity",
        })
        return result


def _historical_record(raw: Mapping[str, Any], *, now: datetime, ttl_seconds: int, policy_version: str, source_watermarks: Mapping[str, Any]) -> HistoricalPerformanceSnapshot:
    payload = dict(raw)
    generated = _utc(payload.get("generated_at")) or now
    expires = _utc(payload.get("expires_at")) or generated + timedelta(seconds=ttl_seconds)
    snapshot_id = str(payload.get("snapshot_id") or _version({"kind": "historical", "generated_at": generated.isoformat(), "watermarks": source_watermarks}))
    return HistoricalPerformanceSnapshot(
        snapshot_id=snapshot_id, generated_at=generated.isoformat(), expires_at=expires.isoformat(),
        policy_version=str(payload.get("policy_version") or policy_version),
        source_watermarks=dict(payload.get("source_watermarks") or source_watermarks),
        executive_metrics=payload.get("executive_metrics") or payload.get("metrics") or {},
        team_metrics=payload.get("team_metrics") or payload.get("team") or {},
        docs_examined=int(payload.get("docs_examined") or 0),
        read_ms=float(payload.get("read_ms") or 0.0), payload=payload,
    )


def _live_record(raw: Mapping[str, Any], *, now: datetime, ttl_seconds: int, policy_version: str, source_watermarks: Mapping[str, Any]) -> LiveCapacitySnapshot:
    payload = dict(raw)
    generated = _utc(payload.get("generated_at")) or now
    expires = _utc(payload.get("expires_at")) or generated + timedelta(seconds=ttl_seconds)
    snapshot_id = str(payload.get("snapshot_id") or _version({"kind": "live", "generated_at": generated.isoformat(), "watermarks": source_watermarks}))
    return LiveCapacitySnapshot(
        snapshot_id=snapshot_id, generated_at=generated.isoformat(), expires_at=expires.isoformat(),
        policy_version=str(payload.get("policy_version") or policy_version),
        source_watermarks=dict(payload.get("source_watermarks") or source_watermarks),
        capacity_metrics=payload.get("capacity_metrics") or payload.get("backlog") or {},
        docs_examined=int(payload.get("docs_examined") or 0),
        read_ms=float(payload.get("read_ms") or 0.0), payload=payload,
    )


class PerformanceSnapshotCache:
    """In-process cache with independent historical and live-capacity TTLs."""

    def __init__(self, *, historical_ttl_seconds: int = DEFAULT_HISTORICAL_TTL_SECONDS, live_capacity_ttl_seconds: int = DEFAULT_LIVE_CAPACITY_TTL_SECONDS):
        if historical_ttl_seconds not in HISTORICAL_TTL_CANDIDATES:
            raise ValueError("historical TTL must be one of 60, 120, 300, 600 seconds")
        if live_capacity_ttl_seconds <= 0:
            raise ValueError("live capacity TTL must be positive")
        self.historical_ttl_seconds = int(historical_ttl_seconds)
        self.live_capacity_ttl_seconds = int(live_capacity_ttl_seconds)
        self._historical: HistoricalPerformanceSnapshot | None = None
        self._live: LiveCapacitySnapshot | None = None
        self._stats = {"historical_hits": 0, "historical_misses": 0, "live_hits": 0, "live_misses": 0, "historical_refreshes": 0, "live_refreshes": 0}

    async def get_historical(self, *, now: Any, builder: Callable[[], Mapping[str, Any] | Awaitable[Mapping[str, Any]]], policy_version: str, source_watermarks: Mapping[str, Any] | None = None, force: bool = False) -> dict[str, Any]:
        current = _utc(now) or datetime.now(timezone.utc)
        watermarks = dict(source_watermarks or {})
        if not force and self._historical and self._historical.is_fresh(now=current, source_watermarks=watermarks):
            self._stats["historical_hits"] += 1
            return self._historical.to_dict()
        self._stats["historical_misses"] += 1
        started = time.perf_counter()
        raw = await _maybe_await(builder())
        if not isinstance(raw, Mapping):
            raise RuntimeError("historical_snapshot_builder_invalid")
        raw = dict(raw)
        raw.setdefault("read_ms", (time.perf_counter() - started) * 1000.0)
        self._historical = _historical_record(raw, now=current, ttl_seconds=self.historical_ttl_seconds, policy_version=policy_version, source_watermarks=watermarks)
        self._stats["historical_refreshes"] += 1
        return self._historical.to_dict()

    async def get_live_capacity(self, *, now: Any, builder: Callable[[], Mapping[str, Any] | Awaitable[Mapping[str, Any]]], policy_version: str, source_watermarks: Mapping[str, Any] | None = None, force: bool = False) -> dict[str, Any]:
        current = _utc(now) or datetime.now(timezone.utc)
        watermarks = dict(source_watermarks or {})
        if not force and self._live and self._live.is_fresh(now=current, source_watermarks=watermarks):
            self._stats["live_hits"] += 1
            return self._live.to_dict()
        self._stats["live_misses"] += 1
        started = time.perf_counter()
        raw = await _maybe_await(builder())
        if not isinstance(raw, Mapping):
            raise RuntimeError("live_capacity_snapshot_builder_invalid")
        raw = dict(raw)
        raw.setdefault("read_ms", (time.perf_counter() - started) * 1000.0)
        self._live = _live_record(raw, now=current, ttl_seconds=self.live_capacity_ttl_seconds, policy_version=policy_version, source_watermarks=watermarks)
        self._stats["live_refreshes"] += 1
        return self._live.to_dict()

    def invalidate(self) -> None:
        self._historical = None
        self._live = None

    def stats(self) -> dict[str, Any]:
        return {**self._stats, "historical_ttl_seconds": self.historical_ttl_seconds, "live_capacity_ttl_seconds": self.live_capacity_ttl_seconds, "historical_snapshot_id": self._historical.snapshot_id if self._historical else None, "live_snapshot_id": self._live.snapshot_id if self._live else None}


async def read_source_watermarks(db: Any = None, *, instrumentation: ReadInstrumentation | None = None) -> dict[str, Any]:
    """Read cheap freshness signals only.  Missing signals are explicit."""
    if db is None:
        from .storage import get_async_db
        db = get_async_db()

    specs = (
        ("max_management_result_timestamp", "crm_management_results", "occurred_at"),
        ("max_cycle_updated_at", "crm_assignment_cycles", "updated_at"),
        ("max_lead_lifecycle_updated_at", "leads", "lifecycle.updated_at"),
        ("max_user_active_change", "usuarios", "active_changed_at"),
    )
    output: dict[str, Any] = {}
    supported: dict[str, bool] = {}
    for name, collection_name, field_name in specs:
        try:
            rows = await _find_many(
                db[collection_name], {}, {field_name: 1}, limit=1,
                sort=[(field_name, -1)], instrumentation=instrumentation,
                query_name=f"watermark.{name}",
            )
            value = _path_value(rows[0], field_name) if rows else None
            output[name] = _iso(value) if value else None
            supported[name] = bool(value)
        except Exception:
            output[name] = None
            supported[name] = False
    # updated_at is a conservative fallback for active-user changes only when
    # the dedicated signal is absent.  It is still explicitly marked.
    if not output.get("max_user_active_change"):
        try:
            rows = await _find_many(
                db["usuarios"], {}, {"updated_at": 1}, limit=1,
                sort=[("updated_at", -1)], instrumentation=instrumentation,
                query_name="watermark.max_user_active_change.updated_at_fallback",
            )
            value = rows[0].get("updated_at") if rows else None
            output["max_user_active_change"] = _iso(value) if value else None
            supported["max_user_active_change"] = bool(value)
        except Exception:
            pass
    output["supported"] = supported
    output["watermark_version"] = _version(output)
    return output


async def build_historical_performance_snapshot(
    db: Any,
    *,
    as_of: datetime,
    policy_since: datetime,
    performance_window_days: int = 60,
    policy_version: str = "crm_sla_reassignment_v1",
    instrumentation: ReadInstrumentation | None = None,
) -> dict[str, Any]:
    """Build history from the bounded performance window, not the whole CRM."""
    started = time.perf_counter()
    from scripts.run_phase05_crm_reassignment_audit import enrich_records
    from scripts.run_phase1d_crm_sla_global_rescue import active_agents, historical_metrics
    from scripts.run_phase1c_crm_territory_audit import build_catalog
    from .crm_sla_global_rescue import RescueParameters

    window_start = max(policy_since, as_of - timedelta(days=performance_window_days))
    cycle_projection = {
        "lead_id": 1, "assignment_cycle_id": 1, "assigned_to_user_id": 1,
        "assigned_to_display_name": 1, "assigned_at": 1, "sla_started_at": 1,
        "temperature_at_assignment": 1, "hot_started_at": 1, "cycle_status": 1,
        "unassigned_at": 1, "schema_version": 1, "reason": 1, "cycle_origin": 1,
        "first_valid_management_at": 1, "updated_at": 1,
    }
    cycles = await _find_many(
        db["crm_assignment_cycles"],
        {"assigned_at": {"$gte": window_start, "$lte": as_of}, "assignment_cycle_id": {"$exists": True, "$ne": None}, "lead_id": {"$exists": True, "$ne": None}},
        cycle_projection, instrumentation=instrumentation, query_name="historical.cycles",
    )
    lead_ids = list({row.get("lead_id") for row in cycles if row.get("lead_id") is not None})
    cycle_ids = list({row.get("assignment_cycle_id") for row in cycles if row.get("assignment_cycle_id") is not None})
    lead_projection = {
        "pipeline_stage": 1, "stage": 1, "crm_estado": 1, "ejecutivo_asignado": 1,
        "lead_temperature_effective": 1, "origen": 1, "lead_origin": 1, "origin": 1,
        "source_type": 1, "lifecycle": 1, "prospecto.codigo": 1,
        "prospecto.codigo_propiedad": 1, "prospecto.codigo_referencia": 1,
        "prospecto.origen": 1, "prospecto.operacion": 1, "prospecto.tipo_operacion": 1,
        "prospecto.comuna": 1, "prospecto.region": 1, "comuna": 1, "region": 1,
        "updated_at": 1,
    }
    result_query = {"assignment_cycle_id": {"$in": cycle_ids}, "occurred_at": {"$gte": window_start}} if cycle_ids else {}
    leads, events, management_results, users = await asyncio.gather(
        _find_many(db["leads"], {"_id": {"$in": lead_ids}}, lead_projection, instrumentation=instrumentation, query_name="historical.leads") if lead_ids else asyncio.sleep(0, result=[]),
        _events_for_leads(db["crm_events"], lead_ids, since=window_start, until=as_of, instrumentation=instrumentation, query_prefix="historical.events"),
        _find_many(db["crm_management_results"], result_query, {"lead_id": 1, "assignment_cycle_id": 1, "actor_user_id": 1, "actor_type": 1, "result_type": 1, "occurred_at": 1}, instrumentation=instrumentation, query_name="historical.management_results") if cycle_ids else asyncio.sleep(0, result=[]),
        _find_many(db["usuarios"], {}, {"_id": 1, "nombre": 1, "rol": 1, "is_active": 1, "comunas_interes": 1, "comunas_interes_norm": 1, "region": 1, "region_slug": 1, "oficina": 1, "office": 1, "zonas": 1}, instrumentation=instrumentation, query_name="historical.users"),
    )
    property_codes = sorted({
        str(_path_value(lead, path))
        for lead in leads
        for path in ("property_code", "prospecto.codigo", "prospecto.codigo_propiedad", "prospecto.codigo_referencia")
        if _path_value(lead, path) not in (None, "")
    })
    property_collection = "universo_cartera_prop360"
    try:
        from config import Config
        property_collection = getattr(Config, "PROPERTY_COLLECTION_NAME", property_collection)
    except Exception:
        pass
    property_projection = {
        "codigo": 1, "comuna": 1, "region": 1, "ubicacion": 1, "estado": 1,
        "ejecutivo": 1, "captador": 1, "responsable": 1,
        "tipo_operacion": 1, "operacion": 1,
    }
    properties_rows = await _find_many(
        db[property_collection], {"codigo": {"$in": property_codes}}, property_projection,
        instrumentation=instrumentation, query_name="historical.properties",
    ) if property_codes else []
    properties = {str(row.get("codigo")): row for row in properties_rows if row.get("codigo") not in (None, "")}
    # Territory normalization is a local static catalog lookup, not a Mongo
    # read.  Keeping it in the snapshot preserves the selector's existing
    # regional/JPC classification without adding a per-lead lookup.
    catalog = build_catalog()
    data = {"as_of": as_of, "leads": leads, "cycles": cycles, "events": events, "management_results": management_results, "notifications": [], "users": users, "properties": properties}
    enrich_started = time.perf_counter()
    records = await _maybe_await(__import__("asyncio").to_thread(enrich_records, data))
    enrich_ms = (time.perf_counter() - enrich_started) * 1000.0
    agents = active_agents(users)
    params = RescueParameters(performance_window_days=performance_window_days)
    metrics_started = time.perf_counter()
    metrics, team = historical_metrics(records, agents, as_of=as_of, params=params)
    metrics_ms = (time.perf_counter() - metrics_started) * 1000.0
    candidates = []
    for agent in agents:
        user_id = str(agent.get("_id") or "")
        metric = metrics.get(user_id, {})
        candidates.append({
            "user_id": user_id, "executive": str(agent.get("nombre") or ""),
            "identity_key": str(agent.get("nombre") or "").strip().lower(), "executive_key": str(agent.get("nombre") or "").strip().lower(),
            "active": agent.get("is_active") is True, "role": str(agent.get("rol") or "").strip().lower(),
            "legacy": False, "protected_by_management": False, "data_issue": False, "closed_lead": False,
            "not_currently_expired": False, "sample_size": metric.get("sample_size", 0),
            "sla_compliance_rate": metric.get("sla_compliance_rate", 0.0), "attention_rate": metric.get("attention_rate", 0.0),
            "sla_compliance_rate_adjusted": metric.get("sla_compliance_rate_adjusted", 0.0),
            "attention_rate_adjusted": metric.get("attention_rate_adjusted", 0.0),
            "p50_first_management_business_minutes": metric.get("p50"),
            "p90_first_management_business_minutes": metric.get("p90"),
            "open_current_policy": 0, "unmanaged_current_policy": 0, "expired_current_policy": 0,
            "shadow_received_count": 0,
            # The selector uses the same projected user profile to derive
            # territorial eligibility.  Keep it in the in-memory snapshot;
            # the projection contains no contact PII.
            "user_record": dict(agent),
        })
    docs = {"cycles": len(cycles), "leads": len(leads), "events": len(events), "management_results": len(management_results), "users": len(users), "properties": len(properties)}
    timing_breakdown = {
        "mongo_logical_read_wall_ms": round(sum(call.wall_ms for call in instrumentation.calls), 6) if instrumentation else None,
        "python_enrich_records_ms": round(enrich_ms, 6),
        "python_metrics_aggregation_ms": round(metrics_ms, 6),
        "python_snapshot_construction_ms": round(max(0.0, (time.perf_counter() - started) * 1000.0 - enrich_ms - metrics_ms), 6),
    }
    if instrumentation:
        instrumentation.mark_python("historical.enrich_records", enrich_ms)
        instrumentation.mark_python("historical.metrics_aggregation", metrics_ms)
        instrumentation.metadata["historical_timing_breakdown"] = timing_breakdown
    return {
        "policy_version": policy_version, "window_start": window_start.isoformat(), "window_end": as_of.isoformat(),
        "candidates": candidates, "metrics": metrics, "team": team, "executive_metrics": metrics, "team_metrics": team,
        "agents": agents, "users_by_id": {str(row.get("_id")): row for row in agents},
        "records_by_cycle": {str(row.get("cycle_id")): row for row in records if row.get("cycle_id")},
        "catalog": catalog, "properties": properties,
        "query_cost_breakdown": docs, "docs_examined": sum(docs.values()), "read_ms": (time.perf_counter() - started) * 1000.0,
        "timing_breakdown": timing_breakdown,
        "query_shape": {"cycles": "assigned_at in bounded performance window", "leads": "_id in referenced cycle leads", "events": "lead_id + management event type + timestamp-or-legacy bounded set read", "management_results": "assignment_cycle_id in referenced cycles and occurred_at >= window_start", "users": "projected active-agent profile fields", "properties": "codigo in referenced lead property codes", "catalog": "local static geojson lookup; no Mongo round-trip"},
    }


async def build_live_capacity_snapshot(
    db: Any,
    *,
    as_of: datetime,
    policy_since: datetime,
    policy_version: str = "crm_sla_reassignment_v1",
    instrumentation: ReadInstrumentation | None = None,
) -> dict[str, Any]:
    """Refresh current capacity without re-reading historical performance."""
    started = time.perf_counter()
    from .crm_metrics import calculate_sla, event_evidence, normalize_result
    from .crm_sla_alert_evaluator import CLOSED_STAGES, EXCLUDED_ORIGINS, SLA_STOP_RESULTS, SYNTHETIC_PHONES
    from .crm_sla_alert_settings import CUTOVER_AT

    cycle_projection = {
        "lead_id": 1, "assignment_cycle_id": 1, "assigned_to_user_id": 1,
        "assigned_at": 1, "sla_started_at": 1, "hot_started_at": 1,
        "temperature_at_assignment": 1, "cycle_status": 1, "unassigned_at": 1,
        "schema_version": 1, "reason": 1, "cycle_origin": 1,
    }
    # This reproduces the current-policy gate used by the productive audit:
    # assignment must be post instrumentation cutover, and the effective SLA
    # start must be post operational cutover.  A missing persisted start uses
    # assigned_at as the same production fallback, without projecting PII.
    query = {
        "cycle_status": "active", "unassigned_at": None,
        "assignment_cycle_id": {"$exists": True, "$ne": None},
        "lead_id": {"$exists": True, "$ne": None},
        "assigned_at": {"$exists": True, "$ne": None, "$gte": policy_since},
        "schema_version": "crm_assignment_cycle_v1",
        "reason": {"$nin": list(EXCLUDED_ORIGINS)},
        "cycle_origin": {"$nin": list(EXCLUDED_ORIGINS)},
        "$or": [
            {"sla_started_at": {"$gte": CUTOVER_AT}},
            {"sla_started_at": {"$exists": False}, "assigned_at": {"$gte": CUTOVER_AT}},
        ],
    }
    cycles = await _find_many(db["crm_assignment_cycles"], query, cycle_projection, instrumentation=instrumentation, query_name="live.cycles")
    # current_policy_active() retains only the latest active cycle per lead.
    # Do the same in memory after the single set read; this is not an extra DB
    # call and prevents duplicate active cycles from inflating capacity.
    latest_by_lead: dict[str, dict[str, Any]] = {}
    for cycle in cycles:
        lead_key = str(cycle.get("lead_id") or "")
        current = latest_by_lead.get(lead_key)
        assigned = _utc(cycle.get("assigned_at")) or datetime.min.replace(tzinfo=timezone.utc)
        previous = _utc((current or {}).get("assigned_at")) or datetime.min.replace(tzinfo=timezone.utc)
        if lead_key and (current is None or assigned >= previous):
            latest_by_lead[lead_key] = cycle
    cycles = list(latest_by_lead.values())
    lead_ids = list({row.get("lead_id") for row in cycles if row.get("lead_id") is not None})
    cycle_ids = list({row.get("assignment_cycle_id") for row in cycles if row.get("assignment_cycle_id") is not None})
    leads, results, events = await asyncio.gather(
        _find_many(
            db["leads"],
            {
                "_id": {"$in": lead_ids},
                "phone": {"$nin": list(SYNTHETIC_PHONES)},
                "lead_origin": {"$nin": list(EXCLUDED_ORIGINS)},
                "origin": {"$nin": list(EXCLUDED_ORIGINS)},
                "prospecto.origen": {"$nin": list(EXCLUDED_ORIGINS)},
            },
            {"_id": 1, "pipeline_stage": 1, "stage": 1, "crm_estado": 1, "lead_temperature_effective": 1, "lifecycle": 1},
            instrumentation=instrumentation, query_name="live.leads",
        ) if lead_ids else asyncio.sleep(0, result=[]),
        _find_many(db["crm_management_results"], {"assignment_cycle_id": {"$in": cycle_ids}}, {"assignment_cycle_id": 1, "result_type": 1, "occurred_at": 1}, instrumentation=instrumentation, query_name="live.management_results") if cycle_ids else asyncio.sleep(0, result=[]),
        _events_for_leads(db["crm_events"], lead_ids, since=policy_since, until=as_of, instrumentation=instrumentation, query_prefix="live.events"),
    )
    join_started = time.perf_counter()
    leads_by_id = {str(row.get("_id")): row for row in leads}
    results_by_cycle: dict[str, list[dict[str, Any]]] = {}
    events_by_lead: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        results_by_cycle.setdefault(str(row.get("assignment_cycle_id") or ""), []).append(row)
    for row in events:
        events_by_lead.setdefault(str(row.get("lead_id") or ""), []).append(row)
    capacity: dict[str, dict[str, int]] = {}
    cycle_audit = {
        "current_policy_cycles": len(cycles),
        "lead_documents": len(leads),
        "lead_missing_or_filtered": 0,
        "lead_closed": 0,
        "owner_missing": 0,
        "management_stop_cycles": 0,
        "human_event_stop_cycles": 0,
        "management_result_rows": len(results),
        "legacy_rows": sum(
            1 for cycle in cycles
            if cycle.get("schema_version") != "crm_assignment_cycle_v1"
        ),
    }
    sla_started = time.perf_counter()
    for cycle in cycles:
        lead = leads_by_id.get(str(cycle.get("lead_id")))
        if not lead:
            cycle_audit["lead_missing_or_filtered"] += 1
            continue
        stage = str(lead.get("pipeline_stage") or lead.get("stage") or lead.get("crm_estado") or "").upper()
        if stage in CLOSED_STAGES:
            cycle_audit["lead_closed"] += 1
            continue
        owner = str(cycle.get("assigned_to_user_id") or "")
        if not owner:
            cycle_audit["owner_missing"] += 1
            continue
        row = capacity.setdefault(owner, {"open": 0, "unmanaged": 0, "expired": 0})
        row["open"] += 1
        assigned_at = cycle.get("sla_started_at") or cycle.get("assigned_at")
        factual_assigned_at = _utc(cycle.get("assigned_at")) or _utc(assigned_at) or policy_since
        stop = any(normalize_result(item.get("result_type")) in SLA_STOP_RESULTS for item in results_by_cycle.get(str(cycle.get("assignment_cycle_id") or ""), []))
        management_stop = stop
        event_stop = any(
            event_evidence(item).get("management")
            and _utc(item.get("timestamp") or item.get("occurred_at"))
            and _utc(item.get("timestamp") or item.get("occurred_at")) >= factual_assigned_at
            for item in events_by_lead.get(str(cycle.get("lead_id") or ""), [])
        )
        stop = management_stop or event_stop
        if management_stop:
            cycle_audit["management_stop_cycles"] += 1
        if event_stop:
            cycle_audit["human_event_stop_cycles"] += 1
        if stop:
            continue
        row["unmanaged"] += 1
        if not assigned_at:
            continue
        temperature = str(cycle.get("temperature_at_assignment") or lead.get("lead_temperature_effective") or "NORMAL").upper()
        hot_start = cycle.get("hot_started_at") or (lead.get("lifecycle") or {}).get("hot_since")
        sla = calculate_sla(assigned_at=assigned_at, now=as_of, temperature=temperature, hot_started_at=hot_start)
        if sla.get("status") == "critical":
            row["expired"] += 1
    sla_ms = (time.perf_counter() - sla_started) * 1000.0
    join_ms = (sla_started - join_started) * 1000.0
    timing_breakdown = {
        "mongo_logical_read_wall_ms": round(sum(call.wall_ms for call in instrumentation.calls), 6) if instrumentation else None,
        "python_joins_maps_ms": round(join_ms, 6),
        "python_sla_calculation_and_aggregation_ms": round(sla_ms, 6),
        "python_snapshot_construction_ms": round(max(0.0, (time.perf_counter() - started) * 1000.0 - join_ms - sla_ms), 6),
    }
    if instrumentation:
        instrumentation.mark_python("live.joins_maps", join_ms)
        instrumentation.mark_python("live.sla_calculation_and_aggregation", sla_ms)
        instrumentation.metadata["live_timing_breakdown"] = timing_breakdown
    docs = {"cycles": len(cycles), "leads": len(leads), "management_results": len(results), "events": len(events)}
    return {
        "policy_version": policy_version, "capacity_metrics": capacity, "backlog": capacity,
        "query_cost_breakdown": docs, "docs_examined": sum(docs.values()), "read_ms": (time.perf_counter() - started) * 1000.0,
        "timing_breakdown": timing_breakdown,
        "cycle_audit": cycle_audit,
        "query_shape": {"cycles": "active and assigned/sla_started_at >= policy_since", "leads": "_id in active current cycles", "management_results": "assignment_cycle_id in active current cycles", "events": "lead_id + management event type + indexed timestamp"},
    }


def snapshot_watermark_changed(previous: Mapping[str, Any] | None, current: Mapping[str, Any] | None) -> bool:
    return dict(previous or {}) != dict(current or {})


def cache_configuration() -> dict[str, Any]:
    return {"historical_ttl_candidates_seconds": list(HISTORICAL_TTL_CANDIDATES), "recommended_historical_ttl_seconds": DEFAULT_HISTORICAL_TTL_SECONDS, "live_capacity_ttl_seconds": DEFAULT_LIVE_CAPACITY_TTL_SECONDS, "watermark_invalidation": "enabled_when_signal_present; deterministic_TTL_when_missing"}
