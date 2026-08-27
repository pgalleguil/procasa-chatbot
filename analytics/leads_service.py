"""Read-only analytics service for the Leads Dashboard."""
from __future__ import annotations

import logging
import math
import time
import calendar
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

from .leads_queries import (
    query_summary,
    query_trends,
    query_distributions,
    query_table,
    query_detail,
    query_filters,
    query_field_coverage,
    query_executive_load,
    query_source_quality,
    query_priorities,
    query_comparative_trends,
    query_funnel,
    query_management_metrics,
    query_property_ranking,
    query_executive_load_detail,
    query_source_performance,
    query_leads_operational_dashboard,
    query_operational_portfolios,
    query_leads_dashboard_executives,
    query_property_commission_rows,
    query_cartera_demanda_coverage,
    query_properties_inventory_dashboard,
    query_demand_capture_dashboard,
    query_capture_simulation_dataset,
    build_capture_simulation_contract,
    _ops_comparable_eligibility,
)

L1_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 120
MAX_CACHE_ENTRIES = 200
# El overview ejecuta diez consultas independientes. Mantener seis workers
# dejaba cuatro consultas esperando en cola y alargaba cada carga del panel.
_COMMERCIAL_QUERY_POOL = ThreadPoolExecutor(max_workers=10, thread_name_prefix="commercial_analytics")


def _load_commercial_macro_information():
    """Read the central macro configuration without making dashboard loading depend on it."""
    path = Path(__file__).parents[1] / "config" / "commercial_macro.json"
    fallback = {
        "available": False,
        "source": "No configurada",
        "indicators": {
            key: {"label": label, "value": None, "as_of": None, "source": "No configurada", "available": False}
            for key, label in (("uf", "UF Chile"), ("usd", "Dólar observado"), ("tpm", "TPM"))
        },
    }
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else fallback
    except (OSError, ValueError, TypeError):
        return fallback


def _cache_key(prefix: str, **params) -> str:
    parts = [f"{k}={v}" for k, v in sorted(params.items()) if v is not None]
    return f"{prefix}:{'|'.join(parts)}"


def _cache_get(key: str) -> dict | None:
    now = time.time()
    entry = L1_CACHE.get(key)
    if entry and now - entry[0] < CACHE_TTL:
        return entry[1]
    if entry:
        del L1_CACHE[key]
    return None


def _sanitize_non_finite(value):
    """Replace non-JSON numeric values with null in analytics payloads."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _sanitize_non_finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_non_finite(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_non_finite(item) for item in value)
    return value


def _cache_set(key: str, value: dict):
    if len(L1_CACHE) >= MAX_CACHE_ENTRIES:
        oldest = min(L1_CACHE, key=lambda k: L1_CACHE[k][0])
        del L1_CACHE[oldest]
    L1_CACHE[key] = (time.time(), value)


def get_properties_inventory_dashboard(
    period_start: str = None,
    period_end: str = None,
    filters: dict | None = None,
    timing: dict | None = None,
) -> dict:
    """Cached read-only payload for the lazy Propiedades & Inventario tab."""
    started = time.perf_counter()
    filters = {key: value for key, value in (filters or {}).items() if value not in (None, "")}
    key = _cache_key("leads-properties-inventory", ps=period_start, pe=period_end, **filters)
    cached = _cache_get(key)
    if cached is not None:
        if timing is not None:
            timing["cache"] = "HIT"
            timing["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return cached
    if timing is not None:
        timing["cache"] = "MISS"
    payload = query_demand_capture_dashboard(period_start, period_end, filters)
    payload = _sanitize_non_finite(payload)
    _cache_set(key, payload)
    if timing is not None:
        timing["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
        timing["mongo_calls"] = 2
    return payload


def get_capture_simulation(
    params: dict | None = None,
    period_end: str | None = None,
    timing: dict | None = None,
) -> dict:
    """Cached read-only what-if simulation over one historical batch."""
    started = time.perf_counter()
    params = {key: value for key, value in (params or {}).items() if value not in (None, "")}
    key = _cache_key("leads-capture-simulator-dataset", pe=period_end)
    dataset = _cache_get(key)
    cache = "HIT" if dataset is not None else "MISS"
    if dataset is None:
        dataset = query_capture_simulation_dataset(period_end)
        _cache_set(key, dataset)
    payload = _sanitize_non_finite(build_capture_simulation_contract(dataset, params))
    if timing is not None:
        timing.update({"cache": cache, "mongo_calls": 0 if cache == "HIT" else 2, "total_ms": round((time.perf_counter() - started) * 1000, 1), "n_plus_one": False})
    return payload


def get_summary(
    period_start: str = None,
    period_end: str = None,
    executive: str = None,
    role: str = None,
    user_name: str = None,
    filters: dict = None,
) -> dict:
    exec_filter = executive if role in ("admin", "supervisor") else user_name
    key = _cache_key("summary", ps=period_start, pe=period_end, exec=exec_filter, role=role)
    cached = _cache_get(key)
    if cached:
        cached = _sanitize_non_finite(cached)
        _cache_set(key, cached)
        return cached

    data = query_summary(
        period_start=period_start,
        period_end=period_end,
        executive=exec_filter if exec_filter else None,
        filters=filters,
    )

    result = {
        "meta": {
            "period": {
                "start": period_start,
                "end": period_end,
                "timezone": "America/Santiago",
            },
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cache_ttl_seconds": CACHE_TTL,
            "snapshot_scope": "current",
            "temperature_scope": "current_snapshot",
            "period_scope": "created_at_flow",
        },
        "stock": {
            "active_operational": data["stock"]["total_active"],
            "hot": data["stock"]["hot"],
            "cold": data["stock"]["cold"],
            "assigned": data["stock"]["assigned"],
            "unassigned": data["stock"]["unassigned"],
            "closed_won_current": data["closed_won_current"],
        },
        "by_stage": data["by_stage"],
        "by_executive": data["by_executive"],
        "flow": {
            "received_in_period": data["flow"]["received_in_period"],
        },
        "quality": data["quality"],
    }
    result = _sanitize_non_finite(result)
    _cache_set(key, result)
    return result


def get_trends(
    period_start: str = None,
    period_end: str = None,
) -> dict:
    key = _cache_key("trends", ps=period_start, pe=period_end)
    cached = _cache_get(key)
    if cached:
        return cached

    data = query_trends(period_start=period_start, period_end=period_end)
    result = {
        "daily": data["daily"],
        "available": data["available"],
        "period_scope": "created_at_flow",
    }
    _cache_set(key, result)
    return result


def get_distributions(
    period_start: str = None,
    period_end: str = None,
    executive: str = None,
    role: str = None,
    user_name: str = None,
    universe: str = "current_active",
    filters: dict = None,
) -> dict:
    exec_filter = executive if role in ("admin", "supervisor") else user_name
    key = _cache_key(
        "distrib", ps=period_start, pe=period_end, exec=exec_filter, u=universe
    )
    cached = _cache_get(key)
    if cached:
        return cached

    data = query_distributions(
        period_start=period_start,
        period_end=period_end,
        executive=exec_filter if exec_filter else None,
        universe=universe,
        filters=filters,
    )
    _cache_set(key, data)
    return data


def get_table(
    page: int = 1,
    limit: int = 50,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    executive: str = None,
    role: str = None,
    user_name: str = None,
    filters: dict = None,
    search: str = None,
    universe: str = "current_active",
    period_start: str = None,
    period_end: str = None,
) -> dict:
    exec_filter = executive if role in ("admin", "supervisor") else user_name
    data = query_table(
        page=page,
        limit=limit,
        sort_by=sort_by,
        sort_dir=sort_dir,
        executive=exec_filter if exec_filter else None,
        filters=filters,
        search=search,
        universe=universe,
        period_start=period_start,
        period_end=period_end,
    )
    return data


def get_leads_operational_dashboard(
    period_start: str = None,
    period_end: str = None,
    compare: str = "auto",
    period_preset: str = None,
    role: str = None,
    user_name: str = None,
    filters: dict = None,
    timing: dict | None = None,
) -> dict:
    """Dashboard operativo: bandeja priorizada, SLA y carga de trabajo."""
    filters = dict(filters or {})
    if role not in ("admin", "supervisor") and user_name:
        filters["executive"] = user_name
    key = _cache_key("leads-operational", ps=period_start, pe=period_end, compare=compare, preset=period_preset, filters=repr(sorted(filters.items())))
    started = time.perf_counter()
    cached = _cache_get(key)
    if cached:
        if timing is not None:
            timing.update({"cache": "HIT", "total_ms": round((time.perf_counter() - started) * 1000, 1), "mongo_calls": 0})
        return cached
    if timing is not None:
        timing["cache"] = "MISS"
    shared_resources = {}
    data = query_leads_operational_dashboard(
        period_start=period_start,
        period_end=period_end,
        filters=filters,
        timing=timing,
        shared_resources=shared_resources,
    )
    # Operational comparison uses the same cohort contract and filters. Stock
    # and backlog remain current-only; only period metrics receive deltas.
    try:
        from datetime import datetime as dt
        from .commercial_periods import comparison_period, local_today, canonical_preset
        today = local_today()
        current_start = dt.strptime(period_start, "%Y-%m-%d").date()
        current_end = dt.strptime(period_end, "%Y-%m-%d").date()
        preset = canonical_preset(current_start, current_end, period_preset)
        mode = compare if compare in ("auto", "prev", "yoy", "none") else "auto"
        compare_start, compare_end, compare_type = comparison_period(current_start, current_end, mode, preset)
        comparable = None
        if compare_start and compare_end:
            comparable_timing = {}
            comparable_started = time.perf_counter()
            comparable = query_leads_operational_dashboard(
                period_start=compare_start.strftime("%Y-%m-%d"),
                period_end=compare_end.strftime("%Y-%m-%d"),
                filters=filters,
                timing=comparable_timing,
                period_only=True,
                team_executives_override=set((data.get("meta") or {}).get("team_executives") or []),
                shared_resources=shared_resources,
            )
            comparable_timing["total_ms"] = round((time.perf_counter() - comparable_started) * 1000, 1)
            if timing is not None:
                timing["comparable"] = comparable_timing
                timing["mongo_calls_total"] = len((timing.get("mongo") or [])) + len((comparable_timing.get("mongo") or []))
        current_period = data.get("period") or {}
        previous_period = (comparable or {}).get("period") or {}
        previous_execs = {item.get("executive"): item for item in (comparable or {}).get("executives", [])}
        # A period-only comparable naturally omits executives with zero
        # assignments in that window. Preserve the stable team matrix shape
        # by treating those missing period cohorts as explicit zeroes.
        empty_previous_period = {
            "assigned": 0, "managed": 0, "hot_sla_pct": None,
            "normal_sla_pct": None, "visits_scheduled": 0,
        }
        for item in data.get("executives", []):
            previous_execs.setdefault(
                item.get("executive"),
                {"period": dict(empty_previous_period)},
            )
        compare_start_utc = (datetime.combine(compare_start, datetime.min.time(), tzinfo=timezone.utc)
                             if compare_start else None)
        compare_end_utc = (datetime.combine(compare_end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
                           if compare_end else None)
        eligibility = _ops_comparable_eligibility(compare_start_utc, compare_end_utc)
        def eligible(metric):
            return bool(comparable and eligibility.get(metric, {}).get("valid"))
        def delta_abs(key, metric=None):
            metric = metric or key
            return (current_period.get(key) or 0) - (previous_period.get(key) or 0) if eligible(metric) else None
        def delta_pct(key, metric=None):
            metric = metric or key
            return round(delta_abs(key, metric) / previous_period[key] * 100, 1) if eligible(metric) and previous_period.get(key) else None
        def delta_pp(key, metric=None):
            metric = metric or key
            return round((current_period.get(key) - previous_period.get(key)), 1) if eligible(metric) and current_period.get(key) is not None and previous_period.get(key) is not None else None
        assigned = current_period.get("assigned") or 0
        prev_assigned = previous_period.get("assigned") or 0
        previous_values = {
            "assigned": previous_period.get("assigned") if eligible("assigned") else None,
            "managed": previous_period.get("managed") if eligible("managed") else None,
            "coverage": round(previous_period.get("managed", 0) / prev_assigned * 100, 1) if eligible("coverage") and prev_assigned else None,
            "hot_sla_pct": previous_period.get("hot_sla_pct") if eligible("hot_sla_pct") else None,
            "normal_sla_pct": previous_period.get("normal_sla_pct") if eligible("normal_sla_pct") else None,
            "visits_scheduled": previous_period.get("visits_scheduled") if eligible("visits_scheduled") else None,
            "lead_to_visit": round(previous_period.get("visits_scheduled", 0) / prev_assigned * 100, 1) if eligible("lead_to_visit") and prev_assigned else None,
            "activity_attempts": previous_period.get("activity_attempts") if eligible("activity_attempts") else None,
            "contact_effective": previous_period.get("contact_effective") if eligible("contact_effective") else None,
        }
        current_period["comparison"] = {
            "type": compare_type, "start": compare_start.strftime("%Y-%m-%d") if compare_start else None,
            "end": compare_end.strftime("%Y-%m-%d") if compare_end else None,
            **previous_values,
            "eligibility": eligibility,
            "deltas": {"assigned": delta_abs("assigned"), "assigned_pct": delta_pct("assigned"),
                       "managed": delta_abs("managed"), "managed_pct": delta_pct("managed"),
                       "coverage_pp": round((current_period.get("managed", 0) / assigned * 100 if assigned else 0) - (previous_period.get("managed", 0) / prev_assigned * 100 if prev_assigned else 0), 1) if eligible("coverage") else None,
                       "hot_sla_pp": delta_pp("hot_sla_pct"), "normal_sla_pp": delta_pp("normal_sla_pct"),
                       "visits": delta_abs("visits_scheduled"), "visits_pct": delta_pct("visits_scheduled"),
                       "lead_to_visit_pp": round((current_period.get("visits_scheduled", 0) / assigned * 100 if assigned else 0) - (previous_period.get("visits_scheduled", 0) / prev_assigned * 100 if prev_assigned else 0), 1) if eligible("lead_to_visit") else None,
                       "activity_attempts": delta_abs("activity_attempts"), "contact_effective": delta_abs("contact_effective")}
        }
        for executive in data.get("executives", []):
            current_exec_period = executive.get("period") or {}
            previous_exec_period = (previous_execs.get(executive.get("executive")) or {}).get("period") or {}
            exec_assigned = current_exec_period.get("assigned") or 0
            prev_exec_assigned = previous_exec_period.get("assigned") or 0
            if not comparable:
                current_exec_period["comparison"] = None
                continue
            current_exec_period["comparison"] = {
                "assigned": previous_exec_period.get("assigned") if eligible("assigned") else None,
                "managed": previous_exec_period.get("managed") if eligible("managed") else None,
                "coverage": round(previous_exec_period.get("managed", 0) / prev_exec_assigned * 100, 1) if eligible("coverage") and prev_exec_assigned else None,
                "hot_sla_pct": previous_exec_period.get("hot_sla_pct") if eligible("hot_sla_pct") else None,
                "normal_sla_pct": previous_exec_period.get("normal_sla_pct") if eligible("normal_sla_pct") else None,
                "visits_scheduled": previous_exec_period.get("visits_scheduled") if eligible("visits_scheduled") else None,
                "eligibility": eligibility,
                "deltas": {
                    "assigned": exec_assigned - prev_exec_assigned if eligible("assigned") else None,
                    "coverage_pp": round((current_exec_period.get("managed", 0) / exec_assigned * 100 if exec_assigned else 0) - (previous_exec_period.get("managed", 0) / prev_exec_assigned * 100 if prev_exec_assigned else 0), 1) if eligible("coverage") else None,
                    "hot_sla_pp": round((current_exec_period.get("hot_sla_pct") or 0) - (previous_exec_period.get("hot_sla_pct") or 0), 1) if eligible("hot_sla_pct") and current_exec_period.get("hot_sla_pct") is not None and previous_exec_period.get("hot_sla_pct") is not None else None,
                    "normal_sla_pp": round((current_exec_period.get("normal_sla_pct") or 0) - (previous_exec_period.get("normal_sla_pct") or 0), 1) if eligible("normal_sla_pct") and current_exec_period.get("normal_sla_pct") is not None and previous_exec_period.get("normal_sla_pct") is not None else None,
                    "visits": (current_exec_period.get("visits_scheduled") or 0) - (previous_exec_period.get("visits_scheduled") or 0) if eligible("visits_scheduled") else None,
                },
            }
        data["meta"]["comparison"] = current_period["comparison"]
    except (TypeError, ValueError, AttributeError):
        data.setdefault("meta", {})["comparison"] = None
    if timing is not None:
        timing["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    _cache_set(key, data)
    return data


def get_detail(lead_id: str) -> dict | None:
    return query_detail(lead_id)


def get_operational_executive_performance(
    period_start: str = None,
    period_end: str = None,
    filters: dict = None,
) -> dict:
    """Endpoint lazy de rendimiento, separado del Overview ejecutivo."""
    key = _cache_key("leads-operational-executives", ps=period_start, pe=period_end,
                     filters=repr(sorted((filters or {}).items())))
    cached = _cache_get(key)
    if cached:
        return cached
    data = query_leads_dashboard_executives(
        period_start=period_start, period_end=period_end,
        filters=filters or {}, include_comparison=True,
    )
    _cache_set(key, data)
    return data


def get_operational_portfolios() -> dict:
    """Opciones dinámicas del filtro cartera/captador."""
    key = _cache_key("leads-operational-portfolios")
    cached = _cache_get(key)
    if cached:
        return cached
    data = {"portfolios": query_operational_portfolios()}
    _cache_set(key, data)
    return data


def get_filters(
    executive: str = None,
    role: str = None,
    user_name: str = None,
) -> dict:
    exec_filter = executive if role in ("admin", "supervisor") else user_name
    key = _cache_key("filters", exec=exec_filter, role=role)
    cached = _cache_get(key)
    if cached:
        return cached

    data = query_filters(
        executive=exec_filter if exec_filter else None,
    )
    _cache_set(key, data)
    return data


def get_field_coverage(
    executive: str = None,
    role: str = None,
    user_name: str = None,
    universe: str = "current_active",
) -> dict:
    exec_filter = executive if role in ("admin", "supervisor") else user_name
    key = _cache_key("coverage", exec=exec_filter, u=universe)
    cached = _cache_get(key)
    if cached:
        return cached
    data = query_field_coverage(
        executive=exec_filter if exec_filter else None,
        universe=universe,
    )
    _cache_set(key, data)
    return data


def _snapshot_sla_pct(snapshot):
    risk = (snapshot or {}).get("risk") or {}
    buckets = [risk.get("lead") or {}, risk.get("lead_hot") or {}]
    within = sum(bucket.get("managed_within", 0) for bucket in buckets)
    denominator = within + sum(bucket.get("managed_outside", 0) + bucket.get("breached", 0) for bucket in buckets)
    return round(within / denominator * 100, 1) if denominator else None


def _select_executive_contribution(contribution, current_received, previous_received, filters=None):
    available = contribution.get("comparable_available", contribution.get("available")) if contribution else False
    if not contribution or not available:
        return {"available": False, "dimension": None, "segment": None, "current": None, "previous": None, "delta": None, "direction": None}
    filters = filters or {}
    blocked = {
        "source": bool(filters.get("source") or filters.get("prospecto.origen")),
        "executive": bool(filters.get("ejecutive") or filters.get("ejecutivo_asignado")),
        "commune": bool(filters.get("commune") or filters.get("prospecto.comuna")),
    }
    total_delta = (current_received or 0) - (previous_received or 0)
    direction = "up" if total_delta > 0 else "down" if total_delta < 0 else "stable"
    labels = {"source": "Fuente", "executive": "Ejecutivo", "commune": "Comuna"}
    candidates = []
    for dimension, payload in (contribution.get("dimensions") or {}).items():
        if blocked.get(dimension):
            continue
        if payload.get("segments") is not None:
            current = {str(row.get("label")): row.get("current", 0) for row in payload.get("segments", [])}
            previous = {str(row.get("label")): row.get("previous", 0) for row in payload.get("segments", [])}
        else:
            current = {str(row.get("segment")): row.get("count", 0) for row in payload.get("current", [])}
            previous = {str(row.get("segment")): row.get("count", 0) for row in payload.get("previous", [])}
        for segment in set(current) | set(previous):
            cur, prev = current.get(segment, 0), previous.get(segment, 0)
            delta = cur - prev
            if (direction == "up" and delta > 0) or (direction == "down" and delta < 0) or (direction == "stable" and delta != 0):
                candidates.append((abs(delta) if direction == "stable" else delta, dimension, segment, cur, prev, delta))
    if not candidates:
        return {"available": False, "dimension": None, "segment": None, "current": None, "previous": None, "delta": None, "direction": direction}
    selected = max(candidates, key=lambda item: (item[0], item[1], item[2])) if direction != "down" else min(candidates, key=lambda item: (item[0], item[1], item[2]))
    _, dimension, segment, current, previous, delta = selected
    return {"available": True, "dimension": labels[dimension], "segment": segment, "current": current, "previous": previous, "delta": delta, "direction": direction}


def _build_executive_story(executive_summary, sla, period_info, contribution, filters=None, management_targets=None):
    summary = executive_summary or {}
    current = summary.get("current") or {}
    previous = summary.get("previous") or {}
    variations = summary.get("variations") or {}
    received = current.get("received", 0)
    previous_received = previous.get("received") if previous else None
    received_delta = received - previous_received if previous_received is not None else None
    received_pct = round(received_delta / previous_received * 100, 1) if previous_received else None
    current_sla = (sla or {}).get("overall_compliance_pct")
    previous_sla = _snapshot_sla_pct(previous) if previous else None
    current_coverage = current.get("management_coverage_pct")
    current_contactability = current.get("contactability_pct")
    coverage_pp = (variations.get("management_coverage_pct") or {}).get("pp")
    contactability_pp = (variations.get("contactability_pct") or {}).get("pp")
    sla_pp = round(current_sla - previous_sla, 1) if current_sla is not None and previous_sla is not None else None

    hot_breached = (sla or {}).get("lead_hot", {}).get("breached", 0) or 0
    lead_breached = (sla or {}).get("lead", {}).get("breached", 0) or 0
    unassigned = current.get("unassigned", 0) or 0
    backlog = current.get("backlog", 0) or 0
    no_contact = current.get("managed_without_effective_contact", 0) or 0
    risk = {
        "code": "none", "label": "Sin riesgos operativos abiertos relevantes", "priority": "Controlado",
        "affected_leads": 0, "denominator": None, "rate_pct": None, "delta_abs": None,
    }
    if hot_breached:
        risk.update(code="hot_breached_open", label="Lead Hot vencido abierto", priority="Crítica", affected_leads=hot_breached, denominator=(sla or {}).get("lead_hot", {}).get("eligible"), rate_pct=_pct(hot_breached, (sla or {}).get("lead_hot", {}).get("eligible")), delta_abs=_risk_delta(current, previous, "lead_hot", "breached"))
    elif lead_breached:
        risk.update(code="lead_breached_open", label="Lead vencido abierto", priority="Alta", affected_leads=lead_breached, denominator=(sla or {}).get("lead", {}).get("eligible"), rate_pct=_pct(lead_breached, (sla or {}).get("lead", {}).get("eligible")), delta_abs=_risk_delta(current, previous, "lead", "breached"))
    elif unassigned:
        risk.update(code="unassigned", label="Leads sin asignación", priority="Media", affected_leads=unassigned, denominator=received, rate_pct=_pct(unassigned, received), delta_abs=_summary_delta(current, previous, "unassigned"))
    elif backlog:
        risk.update(code="backlog", label="Backlog pendiente de gestión", priority="Media", affected_leads=backlog, denominator=current.get("assigned"), rate_pct=_pct(backlog, current.get("assigned")), delta_abs=_summary_delta(current, previous, "backlog"))
    elif no_contact:
        risk.update(code="no_effective_contact", label="Gestiones sin contacto efectivo", priority="Seguimiento", affected_leads=no_contact, denominator=current.get("managed"), rate_pct=_pct(no_contact, current.get("managed")), delta_abs=_summary_delta(current, previous, "managed_without_effective_contact"))
    elif sla_pp is not None and sla_pp < 0:
        risk.update(code="sla_deterioration", label="Deterioro del cumplimiento SLA", priority="Seguimiento", affected_leads=(sla or {}).get("overall_denominator", 0) or 0, denominator=(sla or {}).get("overall_denominator"), rate_pct=current_sla, delta_abs=sla_pp)
    elif coverage_pp is not None and coverage_pp < 0:
        risk.update(code="management_coverage_deterioration", label="Deterioro de cobertura de gestión", priority="Seguimiento", affected_leads=current.get("assigned", 0) or 0, denominator=current.get("assigned"), rate_pct=current_coverage, delta_abs=coverage_pp)

    action_map = {
        "hot_breached_open": ("prioritize_hot_breached", "Pendiente"),
        "lead_breached_open": ("regularize_breached", "Pendiente"),
        "unassigned": ("assign_pending_leads", "Pendiente"),
        "backlog": ("complete_first_management", "En seguimiento"),
        "no_effective_contact": ("prioritize_follow_up", "En seguimiento"),
        "sla_deterioration": ("review_operational_deviation", "En seguimiento"),
        "management_coverage_deterioration": ("review_operational_deviation", "En seguimiento"),
        "none": ("maintain_operational_follow_up", "Controlado"),
    }
    action_code, status = action_map[risk["code"]]
    selected_contribution = _select_executive_contribution(contribution, received, previous_received, filters)
    target_summary = (management_targets or {}).get("summary") or {}
    target_items = (management_targets or {}).get("items") or []
    target_metric = target_summary.get("main_deviation_metric")
    target_deviation = next((item for item in target_items if item.get("metric") == target_metric), None)
    return {
        "period": {"current_label": (period_info.get("current") or {}).get("label", ""), "comparison_label": (period_info.get("previous") or {}).get("label", "Sin comparación"), "universe": received},
        "outcome": {"received": received, "received_delta_abs": received_delta, "received_delta_pct": received_pct, "management_coverage_pct": current_coverage, "management_coverage_delta_pp": coverage_pp, "contactability_pct": current_contactability, "contactability_delta_pp": contactability_pp, "sla_compliance_pct": current_sla, "sla_compliance_delta_pp": sla_pp},
        "main_contribution": selected_contribution,
        "risk": risk,
        "recommended_action": {"code": action_code, "priority": risk["priority"], "affected_leads": risk["affected_leads"], "status": status},
        "target_deviation": {"metric": target_metric, "label": target_deviation.get("label") if target_deviation else None, "gap": target_deviation.get("gap") if target_deviation else None},
        "coverage": {"comparable": bool(previous), "contribution_analysis_available": bool(selected_contribution.get("available")), "insufficient_data": current.get("insufficient_data", 0) or 0},
    }


def build_executive_insights(demand, conversion, sla, sources, pipeline, funnel=None):
    """Motor determinístico de Insights Ejecutivos (máx. 3).

    Solo usa métricas canónicas ya validadas del payload filtrado:
    - CARD 4: ``in_sla_pct`` y ``open_breached`` (vencidos); nunca las claves
      SLA antiguas del Resumen.
    - CARD 2: ``conversion_pct`` y ``previous_pct``.
    - Origen de Demanda: ``sources.items`` (cantidad, pct, conversion_pct).
    - Cobertura SUCRE: ``pipeline`` (propiedades_con_demanda, cartera_activa,
      pct_cartera_con_demanda).
    - Tendencia: ``demand.variation_pct``.
    - Embudo: pérdidas absolutas/proporcionales y respuestas resumidas por etapa.

    Cada insight conecta datos (no repite KPIs) y entrega título breve,
    interpretación y una acción concreta. No se afirma causalidad.
    """
    global_conv = (conversion or {}).get("conversion_pct")
    prev_conv = (conversion or {}).get("previous_pct")
    in_sla = (sla or {}).get("in_sla_pct")
    vencidos = (sla or {}).get("open_breached", 0) or 0
    variacion = (demand or {}).get("variation_pct")
    items = ((sources or {}).get("items") or []) if sources else []
    funnel = funnel or {}

    def _es(n):
        """Formato decimal es-CL (coma) para un porcentaje/pp con 1 decimal."""
        s = f"{n:.1f}"
        if s.endswith(".0"):
            s = s[:-2]
        return s.replace(".", ",")

    priorities = []
    opportunities = []
    positives = []

    # A. Fricción del embudo: no confundir la mayor pérdida en cantidad con
    # la etapa de mayor caída porcentual. Ambas lecturas son útiles para tomar
    # acción y se muestran juntas en el insight automatizado.
    funnel_stages = {
        stage.get("key"): stage
        for stage in (funnel.get("stages") or [])
        if stage.get("key")
    }
    funnel_counts = {
        key: (funnel_stages.get(key) or {}).get("count", 0) or 0
        for key in ("received", "gestionados", "contacto_efectivo", "visita_agendada", "cierre_negocio")
    }
    loss_specs = [
        ("Recibidos → Gestionados", "received", "gestionados", "recibidos", "gestionados"),
        ("Gestionados → Contacto efectivo", "gestionados", "contacto_efectivo", "gestionados", "contacto_efectivo"),
        ("Contacto efectivo → Visita agendada", "contacto_efectivo", "visita_agendada", "contactos efectivos", "visita_agendada"),
        ("Visitas agendadas → Negocio cerrado", "visita_agendada", "cierre_negocio", "visitas agendadas", "cierre_negocio"),
    ]
    funnel_losses = []
    for label, from_key, to_key, denominator_label, response_key in loss_specs:
        denominator = funnel_counts[from_key]
        loss = max(0, funnel_counts[from_key] - funnel_counts[to_key])
        funnel_losses.append({
            "label": label,
            "loss": loss,
            "denominator": denominator,
            "denominator_label": denominator_label,
            "response_key": response_key,
            "from_key": from_key,
        })
    if funnel_counts["received"] > 0:
        absolute_loss = max(funnel_losses, key=lambda item: item["loss"])
        proportional_candidates = [item for item in funnel_losses if item["denominator"] > 0]
        proportional_loss = max(
            proportional_candidates,
            key=lambda item: item["loss"] / item["denominator"],
        ) if proportional_candidates else absolute_loss

        def _loss_sentence(item):
            pct = item["loss"] / item["denominator"] * 100 if item["denominator"] else 0
            return (f"{item['label']}: {item['loss']} leads ({_es(pct)}% de "
                    f"{item['denominator_label']})")

        response_summary = funnel.get("response_summary") or {}
        response_rows = (response_summary.get(proportional_loss["response_key"]) or [])[:3]
        response_stage_label = proportional_loss["response_key"]
        if not response_rows:
            # Un cierre ganado puede estar respaldado por stage_history sin una
            # respuesta CRM con resultado CLOSED_WON. En ese caso mostramos las
            # respuestas de la etapa inmediatamente anterior, sin inventar un
            # resultado de cierre.
            response_rows = (response_summary.get(proportional_loss["from_key"]) or [])[:3]
            response_stage_label = proportional_loss["from_key"]
        response_text = ""
        if response_rows:
            response_stage_label = {
                "received": "Recibidos",
                "gestionados": "Gestionados",
                "contacto_efectivo": "Contacto efectivo",
                "visita_agendada": "Visitas agendadas",
                "cierre_negocio": "Negocio cerrado",
            }.get(response_stage_label, response_stage_label)
            response_text = " Respuestas más frecuentes registradas en " + response_stage_label + ": " + "; ".join(
                f"{row.get('label', 'Otro resultado')} ({row.get('count', 0)})"
                for row in response_rows
            ) + "."
        priorities.append({
            "tipo": "prioridad",
            "titulo": "Fricción crítica del embudo",
            "texto": (f"La mayor pérdida absoluta es {_loss_sentence(absolute_loss)}. "
                      f"La mayor caída proporcional es {_loss_sentence(proportional_loss)}."
                      f"{response_text}"),
            "accion": (f"Priorizar la etapa {proportional_loss['label']} y revisar los leads que no avanzaron; "
                       "usar la pérdida absoluta para dimensionar el impacto operativo."),
        })

    # B. SLA / capacidad de gestión (sin afirmar causalidad con conversión).
    if in_sla is not None and in_sla < 50 and vencidos > 0:
        priorities.append({
            "tipo": "prioridad",
            "titulo": "Respuesta comercial crítica",
            "texto": (f"Solo {_es(in_sla)}% de los leads permanece dentro de SLA "
                      f"y existen {vencidos} vencidos, lo que requiere intervención "
                      f"sobre la primera gestión."),
            "accion": "Priorizar vencidos, especialmente Hot, y verificar capacidad de primera gestión.",
        })

    # C. Origen con alto volumen y bajo resultado.
    if global_conv is not None and global_conv > 0:
        low = [it for it in items
               if it.get("cantidad", 0) >= 20 and (it.get("pct", 0) or 0) >= 15
               and it.get("conversion_pct") is not None
               and it["conversion_pct"] < global_conv * 0.6]
        if low:
            low.sort(key=lambda it: -it.get("cantidad", 0))
            src = low[0]
            priorities.append({
                "tipo": "prioridad",
                "titulo": "Calidad de la principal fuente",
                "texto": (f"{src['nombre']} aporta {_es(src.get('pct', 0))}% de los leads, "
                          f"pero convierte solo {_es(src['conversion_pct'])}% a visita."),
                "accion": "Revisar calidad, segmentación y gestión de los leads provenientes de ese origen.",
            })

    # E. Volumen vs conversión temporal (en puntos porcentuales, CARD 2).
    if variacion is not None and variacion > 0 and global_conv is not None and prev_conv is not None:
        pp = round(global_conv - prev_conv, 1)
        if pp < 0:
            priorities.append({
                "tipo": "prioridad",
                "titulo": "Volumen y conversión divergen",
                "texto": (f"La demanda creció {_es(variacion)}%, pero la conversión a visita "
                          f"bajó {_es(abs(pp))} pp; el crecimiento no se está traduciendo en visitas."),
                "accion": "Revisar la gestión de los leads nuevos y la calidad de las fuentes que crecieron.",
            })
        elif pp > 0:
            positives.append({
                "tipo": "positivo",
                "titulo": "Crecimiento con mejor conversión",
                "texto": (f"La demanda creció {_es(variacion)}% y la conversión a visita "
                          f"mejoró {_es(pp)} pp; el crecimiento se está traduciendo en visitas."),
                "accion": "Consolidar el proceso actual y mantener el ritmo.",
            })

    # F. Oportunidad por origen (lenguaje prudente si muestra pequeña).
    if global_conv is not None and global_conv > 0:
        favorable = [it for it in items
                     if it.get("cantidad", 0) >= 20 and it.get("conversion_pct") is not None
                     and it["conversion_pct"] > global_conv]
        favorable.sort(key=lambda it: -it.get("conversion_pct", 0))
        if favorable:
            small = any(it.get("cantidad", 0) < 50 for it in favorable)
            chosen = favorable[:2]
            if len(chosen) == 1:
                s = chosen[0]
                texto = (f"{s['nombre']} muestra una conversión a visita superior a la media "
                         f"({_es(s['conversion_pct'])}% vs {_es(global_conv)}%), aunque con volúmenes moderados.")
            else:
                nombres = " y ".join(it["nombre"] for it in chosen)
                texto = (f"{nombres} muestran conversiones a visita superiores a la media "
                         f"({_es(global_conv)}%), aunque todavía con volúmenes moderados.")
            if not small:
                texto += " La muestra ya es suficiente."
            opportunities.append({
                "tipo": "oportunidad",
                "titulo": "Señal favorable en otros orígenes",
                "texto": texto,
                "accion": "Observar si el comportamiento se mantiene al acumular una muestra mayor.",
            })

    # G. Cobertura de cartera (solo si señal material, no prioritaria).
    cov = (pipeline or {}).get("pct_cartera_con_demanda")
    if cov is not None and cov < 15:
        con_demanda = (pipeline or {}).get("propiedades_con_demanda", 0)
        activa = (pipeline or {}).get("cartera_activa", 0)
        opportunities.append({
            "tipo": "oportunidad",
            "titulo": "Cobertura de cartera baja",
            "texto": (f"La demanda cubre solo {_es(cov)}% de la cartera activa "
                      f"({con_demanda} de {activa}); hay potencial sin explorar."),
            "accion": "Evaluar la activación de propiedades sin demanda en el período.",
        })

    # Priorización: máx. 3. Si existe oportunidad/positivo, acotar prioridades
    # a 2 para dar balance; si no, mostrar hasta 3 prioridades reales.
    result = []
    has_balance = bool(opportunities) or bool(positives)
    if has_balance:
        result = priorities[:2]
        for cand in (opportunities + positives):
            if len(result) >= 3:
                break
            result.append(cand)
    else:
        result = priorities[:3]
    return result


def _summary_delta(current, previous, key):
    return None if not previous else (current.get(key, 0) or 0) - (previous.get(key, 0) or 0)


def _risk_delta(current, previous, profile, key):
    return None if not previous else ((current.get("risk") or {}).get(profile) or {}).get(key, 0) - ((previous.get("risk") or {}).get(profile) or {}).get(key, 0)


def _executive_target_info(period_start, period_end, filters=None, *, today=None):
    """Return the calendar-prorated global received-leads target for KPI cards."""
    from .management_targets import load_target_configuration

    filters = filters or {}
    segmented_keys = {
        "executive", "ejecutive", "ejecutivo_asignado", "source", "prospecto.origen",
        "operation", "prospecto.operacion", "type", "prospecto.tipo", "commune",
        "prospecto.comuna", "temperature", "lead_temperature_effective", "property",
        "prospecto.codigo", "assignment", "stage", "pipeline_stage",
    }
    segmented = any(filters.get(key) not in (None, "", [], {}) for key in segmented_keys)
    try:
        start = date.fromisoformat(str(period_start)[:10])
        end = date.fromisoformat(str(period_end)[:10])
    except (TypeError, ValueError):
        return {"available": False, "target": None, "segmented": segmented, "reason": "invalid_period"}
    if end < start or segmented:
        return {"available": False, "target": None, "segmented": segmented, "reason": "segmented" if segmented else "invalid_period"}
    configured = next((item for item in load_target_configuration().get("targets", []) if item.get("metric") == "received_leads"), None)
    if not configured or configured.get("target") is None:
        return {"available": False, "target": None, "segmented": False, "reason": "unconfigured"}
    effective_from = configured.get("effective_from")
    if effective_from and end < date.fromisoformat(str(effective_from)[:10]):
        return {"available": False, "target": None, "segmented": False, "reason": "not_active"}
    target = float(configured["target"])
    total = 0.0
    cursor = start
    while cursor <= end:
        month_end = date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1])
        included_end = min(end, month_end)
        days_included = (included_end - cursor).days + 1
        total += target * days_included / month_end.day
        cursor = included_end + timedelta(days=1)
    if total.is_integer():
        total = int(total)
    result = {"available": True, "target": total, "global_target": target, "segmented": False, "reason": None}
    today = today or date.today()
    current_month = start.day == 1 and end == today and start.month == end.month and start.year == end.year
    if current_month:
        elapsed = (today - start).days + 1
        days_total = calendar.monthrange(today.year, today.month)[1]
        result["pace"] = None  # filled with the received count by the card contract
        result["pace_days_elapsed"] = elapsed
        result["pace_days_total"] = days_total
    else:
        result["pace"] = None
    return result


def _build_executive_kpis(kpis, funnel, properties, sla, accountability, trends, filters, period_start, period_end, previous=None):
    """Build the five-card contract exclusively from already resolved payloads."""
    received = (kpis.get("leads_received") or {}).get("value")
    received = received if isinstance(received, (int, float)) else None
    target = _executive_target_info(period_start, period_end, filters, today=date.today())
    if target.get("available") and target.get("pace_days_elapsed") and received is not None:
        target["pace"] = round(received / target["pace_days_elapsed"] * target["pace_days_total"], 1)
    raw_daily = ((trends or {}).get("current") or {}).get("daily") or []
    daily = []
    for point in raw_daily:
        item = dict(point)
        try:
            point_date = date.fromisoformat(str(point.get("date"))[:10])
            item["target_daily"] = round(float(target.get("global_target")) / calendar.monthrange(point_date.year, point_date.month)[1], 2) if target.get("available") else None
            item["partial"] = point_date == date.today()
        except (TypeError, ValueError):
            item["target_daily"] = None
            item["partial"] = False
        daily.append(item)
    demand = {
        "available": received is not None,
        "received": received,
        "target": target.get("target"),
        "global_target": target.get("global_target"),
        "compliance_pct": round(received / target["target"] * 100, 1) if received is not None and target.get("target") else None,
        "pace": target.get("pace"),
        "target_applicable": bool(target.get("available")),
        "segmented": bool(target.get("segmented")),
        "daily": daily,
        "pace_variance_pct": round((target.get("pace") / target.get("global_target") - 1) * 100, 1) if target.get("pace") is not None and target.get("global_target") else None,
    }
    visit = next((row for row in (funnel or []) if row.get("key") == "visit_scheduled"), {})
    prev_received = (kpis.get("leads_received") or {}).get("previous")
    prev_visit = (previous or {}).get("visit_scheduled")
    conversion = visit.get("count") if isinstance(visit.get("count"), (int, float)) else None
    conversion_rate = round(conversion / received * 100, 1) if conversion is not None and received else None
    previous_rate = round(prev_visit / prev_received * 100, 1) if prev_visit is not None and prev_received else None
    conversion_card = {
        "available": conversion_rate is not None,
        "visited_or_scheduled": conversion,
        "received": received,
        "rate_pct": conversion_rate,
        "variation_pp": round(conversion_rate - previous_rate, 1) if conversion_rate is not None and previous_rate is not None else None,
    }
    valuation_rows = (properties or {}).get("valuation") or (properties or {}).get("opportunity") or []
    valid = []
    seen_codes = set()
    for row in ((properties or {}).get("opportunity") or []):
        code = str(row.get("code") or "").strip()
        price = row.get("avg_price_uf")
        operation = str(row.get("operation") or "").strip().lower()
        if code and code not in seen_codes and isinstance(price, (int, float)) and math.isfinite(float(price)) and float(price) > 0 and operation in {"venta", "arriendo"}:
            seen_codes.add(code)
            valid.append({"code": code, "price_uf": float(price), "operation": operation, "leads": row.get("leads") or 0})
    total_value = sum(row["price_uf"] for row in valid) if valid else None
    net = total_value * 0.04 if total_value is not None else None
    pipeline = {
        "available": bool(valid), "property_value_uf": round(total_value, 1) if total_value is not None else None,
        "net_commission_uf": round(net, 1) if net is not None else None,
        "gross_commission_uf": round(net * 1.19, 1) if net is not None else None,
        "property_count": len(valid), "lead_count": sum(row["leads"] for row in valid),
        "coverage_pct": round(len(valid) / max((properties or {}).get("total_properties") or len(valuation_rows), 1) * 100, 1),
        "by_operation": {op: {"property_count": sum(1 for row in valid if row["operation"] == op), "net_commission_uf": round(sum(row["price_uf"] * 0.04 for row in valid if row["operation"] == op), 1), "value_pct": round(sum(row["price_uf"] for row in valid if row["operation"] == op) / max(total_value or 1, 1) * 100, 1)} for op in ("venta", "arriendo")},
    }
    lead_sla = (sla or {}).get("lead") or {}
    hot_sla = (sla or {}).get("lead_hot") or {}
    sla_velocity = {"available": (sla or {}).get("overall_compliance_pct") is not None, "compliance_pct": (sla or {}).get("overall_compliance_pct"), "lead": {"eligible": lead_sla.get("eligible", 0), "median_minutes": lead_sla.get("median_minutes"), "p90_minutes": lead_sla.get("p90_minutes"), "managed_within": lead_sla.get("managed_within", 0), "managed_outside": lead_sla.get("managed_outside", 0), "open_breached": lead_sla.get("breached", 0)}, "lead_hot": {"eligible": hot_sla.get("eligible", 0), "median_minutes": hot_sla.get("median_minutes"), "p90_minutes": hot_sla.get("p90_minutes"), "managed_within": hot_sla.get("managed_within", 0), "managed_outside": hot_sla.get("managed_outside", 0), "open_breached": hot_sla.get("breached", 0)}}
    summary = (accountability or {}).get("summary") or {}
    by_exec = (accountability or {}).get("by_executive") or []
    def executive_breaches(row):
        lead, hot = row.get("lead") or {}, row.get("lead_hot") or {}
        return (lead.get("breached_with_activity_without_result", 0) or 0) + (hot.get("breached_with_activity_without_result", 0) or 0)
    top_exec = max(by_exec, key=lambda row: (executive_breaches(row), row.get("executive_name") or ""), default={})
    total_breached = summary.get("open_breached")
    registration = {"available": bool(summary), "activity_count": summary.get("breached_with_activity_without_result"), "registration_gap_rate": summary.get("registration_gap_rate"), "activity_without_result": summary.get("breached_with_activity_without_result"), "without_activity": summary.get("breached_without_activity"), "total_breached": total_breached, "highest_concentration": top_exec.get("executive_name"), "highest_concentration_count": executive_breaches(top_exec) if top_exec else None, "reconciled": total_breached is not None and total_breached == (summary.get("breached_with_activity_without_result", 0) + summary.get("breached_without_activity", 0))}
    return {"demand_pace": demand, "visit_conversion": conversion_card, "pipeline_valuation": pipeline, "sla_velocity": sla_velocity, "registration_discipline": registration}


def get_dashboard(
    period_start: str = None,
    period_end: str = None,
    executive: str = None,
    role: str = None,
    user_name: str = None,
) -> dict:
    """Consolidated endpoint returning all dashboard data in one call."""
    exec_filter = executive if role in ("admin", "supervisor") else user_name
    key = _cache_key("dashboard", ps=period_start, pe=period_end, exec=exec_filter, role=role)
    cached = _cache_get(key)
    if cached:
        return cached

    summary = query_summary(
        period_start=period_start, period_end=period_end,
        executive=exec_filter if exec_filter else None,
    )
    comparative = query_comparative_trends(
        period_start=period_start, period_end=period_end,
    )
    funnel_data = query_funnel(
        period_start=period_start, period_end=period_end,
        executive=exec_filter if exec_filter else None,
    )
    management = query_management_metrics(
        executive=exec_filter if exec_filter else None,
    )
    priorities = query_priorities(
        executive=exec_filter if exec_filter else None,
    )
    exec_load = query_executive_load_detail(
        executive=exec_filter if exec_filter else None,
    )
    source_perf = query_source_performance(
        period_start=period_start, period_end=period_end,
        executive=exec_filter if exec_filter else None,
    )
    prop_ranking = query_property_ranking(
        period_start=period_start, period_end=period_end,
        executive=exec_filter if exec_filter else None,
    )
    distributions = query_distributions(
        period_start=period_start, period_end=period_end,
        executive=exec_filter if exec_filter else None,
        universe="received_in_period",
    )
    coverage = query_field_coverage(
        executive=exec_filter if exec_filter else None,
        universe="current_active",
    )

    stock = summary.get("stock", {})
    flow = summary.get("flow", {})
    total_active = stock.get("total_active", 0)
    hot = stock.get("hot", 0)
    cold = stock.get("cold", 0)
    assigned = stock.get("assigned", 0)
    unassigned = stock.get("unassigned", 0)
    received = flow.get("received_in_period", 0)
    closed_won = summary.get("closed_won_current", 0)

    prev_total = comparative.get("previous", {}).get("total", 0)
    var_pct = round(((received - prev_total) / prev_total * 100), 1) if prev_total else None
    var_label = f"un {abs(var_pct)}% {'mas' if var_pct and var_pct > 0 else 'menos'} que el periodo anterior" if var_pct is not None else "sin datos del periodo anterior"

    tem_desktop = hot + cold
    unknown_temp = total_active - tem_desktop
    pending_7d = sum(e.get("pending_gt_7d", 0) for e in exec_load)

    summary_text = (
        f"En el periodo ingresaron {received} leads, {var_label}. "
        f"Actualmente existen {hot} Hot, {cold} Cold y {unassigned} leads sin ejecutivo asignado."
    )
    if pending_7d > 0:
        summary_text += f" {pending_7d} leads permanecen en NEW o sin etapa por mas de siete dias."

    mgmt_coverage_text = "La medicion de primera respuesta no tiene cobertura suficiente."
    if management.get("total_assigned", 0) > 0:
        mgmt_coverage_text += f" {management.get('total_with_evidence', 0)} de {management.get('total_assigned', 0)} leads asignados contienen timestamps verificables."

    result = {
        "meta": {
            "period": {"start": period_start, "end": period_end, "timezone": "America/Santiago"},
            "previous_period": {"start": None, "end": None},
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "snapshot_scope": "current",
            "period_scope": "created_at_flow",
        },
        "summary": {"text": summary_text, "mgmt_coverage_text": mgmt_coverage_text},
        "kpis": {
            "received": {
                "value": received, "previous_value": prev_total,
                "variation_pct": var_pct,
                "variation_label": f"{abs(var_pct)}% {'mas' if var_pct and var_pct > 0 else 'menos'}" if var_pct is not None else None,
                "daily_trend": comparative.get("current", {}).get("daily", []),
                "previous_daily": comparative.get("previous", {}).get("daily", []),
            },
            "active": {"value": total_active},
            "temperature": {
                "hot": hot, "cold": cold, "unknown": unknown_temp,
                "hot_pct": round(hot / tem_desktop * 100, 1) if tem_desktop else 0,
                "cold_pct": round(cold / tem_desktop * 100, 1) if tem_desktop else 0,
            },
            "distribution": {
                "assigned": assigned, "unassigned": unassigned,
                "assigned_pct": round(assigned / total_active * 100, 1) if total_active else 0,
                "unassigned_pct": round(unassigned / total_active * 100, 1) if total_active else 0,
            },
            "pending_attention": {
                "value": pending_7d,
                "pct_of_active": round(pending_7d / total_active * 100, 1) if total_active else 0,
            },
            "management": {
                "total_assigned": management.get("total_assigned", 0),
                "total_with_evidence": management.get("total_with_evidence", 0),
                "coverage_pct": management.get("coverage_pct", 0),
                "sample_sufficient": management.get("sample_sufficient", False),
                "median_minutes": management.get("median_minutes"),
                "p90_minutes": management.get("p90_minutes"),
                "before_threshold_pct": management.get("before_threshold_pct"),
                "distribution": management.get("distribution", []),
                "threshold_minutes": management.get("threshold_minutes", 180),
            },
        },
        "trends": comparative,
        "priorities": priorities,
        "funnel": funnel_data,
        "executive_load": exec_load,
        "source_performance": source_perf,
        "property_ranking": prop_ranking.get("ranking", []),
        "no_code_count": prop_ranking.get("no_code_count", 0),
        "demand": {
            "operations": distributions.get("operations", []),
            "types": distributions.get("types", []),
            "communes": distributions.get("communes", []),
        },
        "coverage": {"fields": {k: v for k, v in coverage.items()}},
    }
    _cache_set(key, result)
    return result


# =============================================================================
# COMMERCIAL DASHBOARD SERVICE
# =============================================================================


def get_commercial_dashboard(
    period_start: str = None,
    period_end: str = None,
    executive: str = None,
    role: str = None,
    user_name: str = None,
    filters: dict = None,
    compare: str = None,
    period_preset: str = None,
) -> dict:
    """Consolidated commercial dashboard data.

    Returns all data needed for the commercial dashboard in one call.
    Strictly read-only, never modifies commercial data.
    """
    exec_filter = executive if role in ("admin", "supervisor") else user_name
    ef = {"ejecutivo_asignado": exec_filter} if exec_filter else None
    merged_filters = {**(filters or {})}
    if ef:
        merged_filters.update(ef)
    from .leads_queries import (
        query_commercial_kpis,
        query_commercial_funnel,
        query_sla_risk_panel,
        query_sla_accountability,
        query_demand_by_price_ranges,
        query_commercial_executive_matrix,
        query_commercial_property_ranking,
        query_commercial_insights,
        query_source_performance,
        query_comparative_trends,
        query_field_coverage,
        query_executive_summary,
        query_variance_drivers,
        query_executive_load_detail,
    )

    # Period comparison — compute previous period based on mode
    from datetime import datetime as dt, timedelta as td
    from .commercial_periods import comparison_period, local_today
    try:
        today = local_today()
        ps_dt = dt.strptime(period_start, "%Y-%m-%d").date() if period_start else today - td(days=29)
        pe_dt = dt.strptime(period_end, "%Y-%m-%d").date() if period_end else today
        pe_dt = min(pe_dt, today)
        ps_dt = min(ps_dt, pe_dt)
    except (ValueError, TypeError):
        pe_dt = local_today()
        ps_dt = pe_dt - td(days=29)

    period_start = ps_dt.strftime("%Y-%m-%d")
    period_end = pe_dt.strftime("%Y-%m-%d")
    kwargs = {"period_start": period_start, "period_end": period_end, "filters": merged_filters or None}
    period_label = f"{period_start} - {period_end}"

    prev_start = ""
    prev_end = ""
    prev_label = ""
    comp_type = "custom_vs_previous"

    mode = compare if compare in ("auto", "prev", "yoy", "none") else "auto"
    preset = period_preset if period_preset in ("today", "week", "month", "30d", "custom") else "custom"
    comp_start, comp_end, comp_type = comparison_period(ps_dt, pe_dt, mode, preset)
    if mode == "none":
        prev_label = "Sin comparaci\u00f3n"
        comp_type = "custom_no_comparison"
    elif mode == "yoy":
        prev_start = comp_start.strftime("%Y-%m-%d")
        prev_end = comp_end.strftime("%Y-%m-%d")
        prev_label = f"{prev_start} - {prev_end} (mismo per\u00edodo a\u00f1o anterior)"
    else:
        prev_start = comp_start.strftime("%Y-%m-%d")
        prev_end = comp_end.strftime("%Y-%m-%d")
        prev_label = f"{prev_start} - {prev_end} (per\u00edodo anterior)"

    period_info = {
        "type": comp_type,
        "timezone": "America/Santiago",
        "preset": preset,
        "comparison_mode": mode,
        "compare_requested": mode,
        "compare_resolved": "none" if mode == "none" else ("yoy" if mode == "yoy" else "prev"),
        "comparison_rule": comp_type,
        "current": {"start": period_start or "", "end": period_end or "", "label": period_label},
        "previous": {"start": prev_start, "end": prev_end, "label": prev_label},
        "comparison": {"start": prev_start, "end": prev_end, "label": prev_label},
    }

    comparison_kwargs = ({"comparison_start": prev_start, "comparison_end": prev_end}
                         if prev_start and prev_end else {"include_comparison": False})
    key = _cache_key(
        "commercial-dashboard-v2", ps=period_start, pe=period_end,
        comparison_start=prev_start, comparison_end=prev_end,
        exec=exec_filter, role=role, cmp=mode, preset=preset,
        filters=repr(sorted((merged_filters or {}).items())),
    )
    cached = _cache_get(key)
    if cached:
        return cached
    # Independent read-only aggregations run concurrently. PyMongo clients are
    # thread-safe and the response contract remains identical; this only
    # reduces cold-load wall time.
    futures = {
        "kpis": _COMMERCIAL_QUERY_POOL.submit(query_commercial_kpis, **kwargs, **comparison_kwargs),
        "funnel": _COMMERCIAL_QUERY_POOL.submit(query_commercial_funnel, **kwargs),
        "sla": _COMMERCIAL_QUERY_POOL.submit(query_sla_risk_panel, **kwargs),
        "sla_accountability": _COMMERCIAL_QUERY_POOL.submit(query_sla_accountability, **kwargs),
        "demand": _COMMERCIAL_QUERY_POOL.submit(query_demand_by_price_ranges, **kwargs),
        "executives": _COMMERCIAL_QUERY_POOL.submit(query_commercial_executive_matrix, **kwargs),
        "properties": _COMMERCIAL_QUERY_POOL.submit(query_commercial_property_ranking, **kwargs),
        "sources": _COMMERCIAL_QUERY_POOL.submit(query_source_performance, **kwargs, **comparison_kwargs),
        "trends": _COMMERCIAL_QUERY_POOL.submit(query_comparative_trends, **kwargs, **comparison_kwargs),
        "variance_drivers": _COMMERCIAL_QUERY_POOL.submit(query_variance_drivers, **kwargs, **comparison_kwargs),
        "coverage": _COMMERCIAL_QUERY_POOL.submit(
            query_field_coverage,
            period_start=period_start,
            period_end=period_end,
            filters=merged_filters or None,
            universe="received_in_period",
        ),
    }
    kpis = futures["kpis"].result()
    funnel = futures["funnel"].result()
    sla = futures["sla"].result()
    sla_accountability = futures["sla_accountability"].result()
    executive_summary = query_executive_summary(
        **kwargs, **comparison_kwargs, sla_risk=sla,
    )
    from .management_targets import build_management_targets
    management_targets = build_management_targets(
        sla,
        executive_summary,
        period_end=period_end,
        comparable_end=prev_end or None,
    )
    kpis["sla_compliance"] = {
        "value": sla.get("overall_compliance_pct"),
        "previous": None,
        "pp_change": None,
        "universe": "sla_risk_panel",
        "sla_policy": "SLA: minutos h\u00e1biles",
    }
    demand_price = futures["demand"].result()
    executives = futures["executives"].result()
    properties = futures["properties"].result()
    sources = futures["sources"].result()
    trends = futures["trends"].result()
    variance_drivers = futures["variance_drivers"].result()
    coverage = futures["coverage"].result()
    executive_kpis = _build_executive_kpis(
        kpis, funnel, properties, sla, sla_accountability, trends, merged_filters,
        period_start, period_end,
    )
    try:
        insights = query_commercial_insights(
            kpis=kpis, funnel=funnel, sla=sla,
            sources=sources, demand=demand_price,
            executives=executives,
            filters=merged_filters or None,
        )
    except Exception as e:
        logger.warning(f"Insights engine error: {e}")
        insights = []

    meta = {
        "period": period_info,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "read_only": True,
        "unit": "lead._id",
        "universe": {
            "leads": kpis.get("leads_received", {}).get("value"),
            "filtered": bool(merged_filters),
            "filters_applied": sorted(merged_filters),
            "period_scope": "created_at",
        },
        "sla_policy": {
            "type": "business_minutes",
            "threshold_minutes": 180,
            "display_label": "SLA vigente: minutos h\u00e1biles",
            "timezone": "America/Santiago",
            "business_hours": "Lunes a viernes, 09:00-19:00",
        },
    }

    result = {
        "meta": meta,
        "kpis": kpis,
        "funnel": funnel,
        "sla_risk": sla,
        "sla_accountability": sla_accountability,
        "executive_summary": executive_summary,
        "executive_story": _build_executive_story(executive_summary, sla, period_info, variance_drivers, merged_filters, management_targets),
        "variance_drivers": variance_drivers,
        "management_targets": management_targets,
        "demand_by_price": demand_price,
        "executives": executives,
        "properties": properties,
        "sources": sources,
        "trends": trends,
        "insights": insights,
        "coverage": coverage,
        "executive_kpis": executive_kpis,
        "macro_indicators": _load_commercial_macro_information(),
    }
    _cache_set(key, result)
    return result


def get_commercial_filter_options() -> dict:
    """Public filter options for commercial dashboard selectors. No sensitive data."""
    from .leads_queries import query_commercial_filter_options
    key = _cache_key("commercial-filters", v="2")
    cached = _cache_get(key)
    if cached:
        return cached
    data = query_commercial_filter_options()
    _cache_set(key, data)
    return data


def _load_received_leads_meta_target() -> int | None:
    """Meta de negocio configurada para 'received_leads' (leads recibidos)."""
    try:
        from .management_targets import CONFIG_PATH
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for target in config.get("targets", []):
            if target.get("metric") == "received_leads" and target.get("target") is not None:
                return int(target["target"])
    except (OSError, ValueError, TypeError):
        pass
    return None


def get_leads_dashboard_overview(
    period_start: str = None,
    period_end: str = None,
    compare: str = None,
    period_preset: str = None,
    timing: dict | None = None,
) -> dict:
    """Resumen para la CARD 1 (Demanda & Meta) del Leads Dashboard.

    Read-only. Calcula leads recibidos en el periodo seleccionado, el periodo
    equivalente anterior, la tendencia diaria (sparkline) y la meta de negocio.
    Reutiliza la misma lógica de periodo/comparación del Dashboard Comercial.
    """
    from datetime import datetime as dt, timedelta as td
    from .commercial_periods import canonical_preset, comparison_period, local_today, preset_range

    request_started = time.perf_counter()
    timing_lock = Lock()

    def record_timing(name: str, started: float, **extra):
        if timing is None:
            return
        item = {"duration_ms": round((time.perf_counter() - started) * 1000, 1)}
        item.update(extra)
        with timing_lock:
            timing.setdefault("components", {})[name] = item

    def run_timed(name: str, fn, kwargs: dict):
        started = time.perf_counter()
        try:
            return fn(**kwargs)
        finally:
            record_timing(name, started, thread=__import__("threading").current_thread().name)

    today = local_today()
    try:
        ps_dt = dt.strptime(period_start, "%Y-%m-%d").date() if period_start else today - td(days=29)
        pe_dt = dt.strptime(period_end, "%Y-%m-%d").date() if period_end else today
        pe_dt = min(pe_dt, today)
        ps_dt = min(ps_dt, pe_dt)
    except (ValueError, TypeError):
        pe_dt = today
        ps_dt = pe_dt - td(days=29)

    # Cuando llega un preset explícito, este manda sobre fechas antiguas que
    # puedan haber quedado en la URL (por ejemplo, “Semana” con un rango de 2 días).
    if period_preset in ("today", "week", "month", "30d"):
        ps_dt, pe_dt = preset_range(period_preset, today)
    period_start = ps_dt.strftime("%Y-%m-%d")
    period_end = pe_dt.strftime("%Y-%m-%d")
    mode = compare if compare in ("auto", "prev", "yoy", "none") else "auto"
    preset = period_preset if period_preset in ("today", "week", "month", "30d", "custom") else "custom"
    preset = canonical_preset(ps_dt, pe_dt, preset)
    comp_start, comp_end, comp_type = comparison_period(ps_dt, pe_dt, mode, preset)

    prev_start = prev_end = None
    if mode != "none" and comp_start and comp_end:
        prev_start = comp_start.strftime("%Y-%m-%d")
        prev_end = comp_end.strftime("%Y-%m-%d")

    key = _cache_key(
        "leads-dashboard-overview", ps=period_start, pe=period_end,
        cmp=mode, preset=preset, ps_prev=prev_start, pe_prev=prev_end,
    )
    cached = _cache_get(key)
    if cached:
        if timing is not None:
            timing["cache"] = "HIT"
            timing["total_ms"] = round((time.perf_counter() - request_started) * 1000, 1)
            logger.info("[OVERVIEW_TIMING] cache=HIT total_ms=%.1f", timing["total_ms"])
        return cached

    if timing is not None:
        timing["cache"] = "MISS"
    concurrent_started = time.perf_counter()

    from .leads_queries import (
        query_leads_dashboard_conversion,
        query_leads_dashboard_pipeline,
        query_sla_risk_panel,
        query_leads_dashboard_sources,
        query_leads_dashboard_funnel,
    )
    conversion_detail = {}
    sources_detail = {}
    funnel_detail = {}
    # La lectura de órdenes firmadas es compartida por Conversión y Origen,
    # pero no debe bloquear el resto del Overview. Se inicia en la primera
    # ola y solo sus consumidores esperan su resultado.
    def load_shared_orders():
        try:
            from chatbot.storage import get_db
            from .leads_queries import CANONICAL_SIGNED_ORDER_STATUSES
            return list(get_db()["visitas"].find(
                {"status": {"$in": list(CANONICAL_SIGNED_ORDER_STATUSES)}},
                {"visita_code": 1, "phone": 1, "property_code": 1, "timeline": 1, "created_at": 1},
            ))
        except Exception as exc:
            logger.warning("Overview shared signed-orders read unavailable; components will fall back: %s", exc)
            return None

    # UF es una lectura local/cacheada y debe estar disponible antes de enviar
    # la consulta de comisión, sin esperar a ningún KPI del Overview.
    macro = _load_commercial_macro_information()
    uf_info = (macro.get("indicators") or {}).get("uf") or {}
    uf_value = uf_info.get("value")
    uf_asof = uf_info.get("as_of")
    try:
        from chatbot.uf_service import leer_uf_cache
        _uf = leer_uf_cache()
        if _uf and _uf.get("valor"):
            uf_value = _uf["valor"]
            uf_asof = _uf.get("fecha") or uf_asof
    except Exception:
        pass

    MIN_VENTA_CLP = 1_000_000
    MIN_ARRIENDO_CLP = 100_000
    uf_clp = float(uf_value) if uf_value else None

    # Primera ola: todo lo que no depende de las órdenes firmadas se inicia de
    # inmediato. Esto solapa las lecturas remotas y evita una segunda ola.
    f_orders = _COMMERCIAL_QUERY_POOL.submit(run_timed, "shared_signed_orders", load_shared_orders, {})
    f_trends = _COMMERCIAL_QUERY_POOL.submit(run_timed, "demand_trend", query_comparative_trends, {
        "period_start": period_start, "period_end": period_end,
        "comparison_start": prev_start, "comparison_end": prev_end,
        "include_comparison": bool(prev_start),
    })
    f_pipe = _COMMERCIAL_QUERY_POOL.submit(run_timed, "valuation_pipeline", query_leads_dashboard_pipeline, {
        "period_start": period_start, "period_end": period_end,
    })
    f_sla = _COMMERCIAL_QUERY_POOL.submit(run_timed, "sla", query_sla_risk_panel, {
        "period_start": period_start, "period_end": period_end,
    })
    f_funnel = _COMMERCIAL_QUERY_POOL.submit(run_timed, "funnel", query_leads_dashboard_funnel, {
        "period_start": period_start, "period_end": period_end,
        "timing": funnel_detail,
        # Funnel espera este Future únicamente al llegar a la evidencia de
        # órdenes, después de haber ejecutado su cohorte y eventos.
        "signed_orders_future": f_orders,
    })
    f_coverage = _COMMERCIAL_QUERY_POOL.submit(
        run_timed, "demand_coverage", query_cartera_demanda_coverage,
        {"period_start": period_start, "period_end": period_end, "oficina": "PROCASA SUCRE"},
    )
    f_property = _COMMERCIAL_QUERY_POOL.submit(
        run_timed, "property_commission", query_property_commission_rows,
        {"period_start": period_start, "period_end": period_end, "uf_value": uf_clp},
    )

    # Segunda ola mínima: solo Conversión y Origen necesitan las órdenes.
    shared_orders = f_orders.result()
    f_conv = _COMMERCIAL_QUERY_POOL.submit(run_timed, "conversion", query_leads_dashboard_conversion, {
        "period_start": period_start, "period_end": period_end,
        "comparison_start": prev_start, "comparison_end": prev_end,
        "include_comparison": bool(prev_start),
        "timing": conversion_detail,
        "signed_orders": shared_orders,
    })
    f_sources = _COMMERCIAL_QUERY_POOL.submit(run_timed, "sources", query_leads_dashboard_sources, {
        "period_start": period_start, "period_end": period_end,
        "comparison_start": prev_start, "comparison_end": prev_end,
        "include_comparison": bool(prev_start),
        "timing": sources_detail,
        "signed_orders": shared_orders,
    })

    trends = f_trends.result()
    conversion = f_conv.result()
    pipeline = f_pipe.result()
    sla_panel = f_sla.result()
    sources = f_sources.result()
    try:
        funnel = f_funnel.result()
    except Exception as exc:
        logger.warning("Leads dashboard funnel unavailable: %s", exc)
        funnel = {"received": 0, "stages": []}
    _cobertura = f_coverage.result()
    _props = f_property.result()
    if timing is not None:
        timing["concurrent_block_ms"] = round((time.perf_counter() - concurrent_started) * 1000, 1)
        timing["component_details"] = {
            "conversion": conversion_detail,
            "sources": sources_detail,
            "funnel": funnel_detail,
        }
    current = trends.get("current", {})
    previous = trends.get("previous", {})
    daily = current.get("daily", []) or []
    daily_history = current.get("daily_history", []) or []
    # La meta mensual debe prorratearse según los días calendario realmente
    # seleccionados. Evita comparar, por ejemplo, 2 días contra la meta total
    # de 200 leads del mes.
    stage_started = time.perf_counter()
    target_info = _executive_target_info(period_start, period_end, today=today)
    meta_target = target_info.get("target") if target_info.get("available") else _load_received_leads_meta_target()
    record_timing("goal", stage_started)

    conv_current = conversion.get("current", {})
    conv_previous = conversion.get("previous", {})
    conv_total = conv_current.get("total", 0)
    conv_citas = conv_current.get("citas", 0)
    conv_evaluable = conv_current.get("evaluable", 0)
    prev_total = conv_previous.get("total", 0)
    prev_citas = conv_previous.get("citas", 0)
    prev_evaluable = conv_previous.get("evaluable", 0)
    orders_ambiguous = conv_current.get("orders_ambiguous", 0)
    # Conversión a visita agendada: citas / TODOS los leads del período.
    # El denominador NO excluye leads sin trazabilidad (decisión BI).
    # diff_pp se calcula sobre tasas SIN redondear para evitar doble redondeo.
    _conv_rate = conv_citas / conv_total if conv_total else None
    _prev_rate = prev_citas / prev_total if (prev_start and prev_total) else None
    conv_pct = round(_conv_rate * 100, 1) if _conv_rate is not None else None
    prev_pct = round(_prev_rate * 100, 1) if _prev_rate is not None else None
    diff_pp = round((_conv_rate - _prev_rate) * 100, 1) if (_conv_rate is not None and _prev_rate is not None) else None
    ratio = round(conv_total / conv_citas, 1) if conv_citas else None
    traceability_pct = round(conv_evaluable / conv_total * 100, 1) if conv_total else None

    total_leads = current.get("total", 0)
    monto_uf = pipeline.get("monto_uf", 0.0)
    venta_uf = pipeline.get("monto_venta_uf", 0.0)
    arriendo_uf = pipeline.get("monto_arriendo_uf", 0.0)
    otro_uf = pipeline.get("monto_otro_uf", 0.0)
    pct_venta = round(venta_uf / monto_uf * 100, 1) if monto_uf else 0.0
    pct_arriendo = round(arriendo_uf / monto_uf * 100, 1) if monto_uf else 0.0
    pct_otro = round(otro_uf / monto_uf * 100, 1) if monto_uf else 0.0
    cobertura = round(pipeline.get("leads_vinculados", 0) / total_leads * 100, 1) if total_leads else 0.0
    propiedades_vinculadas = pipeline.get("propiedades_vinculadas", 0)
    propiedades_cartera = pipeline.get("propiedades_cartera", 0)
    propiedades_con_precio = pipeline.get("propiedades_con_precio", 0)
    propiedades_cartera_valorizadas = pipeline.get("propiedades_cartera_valorizadas", propiedades_con_precio)
    propiedades_venta = pipeline.get("propiedades_venta", 0)
    propiedades_arriendo = pipeline.get("propiedades_arriendo", 0)
    propiedades_otro = pipeline.get("propiedades_otro", 0)
    propiedades_sin_precio = pipeline.get("propiedades_sin_precio", 0)
    propiedades_no_en_cartera = pipeline.get("propiedades_no_en_cartera", 0)

    # Cobertura y comisión ya se ejecutaron en la primera ola. Sus resultados
    # se consumen aquí sin abrir una segunda ronda de consultas remotas.
    propiedades_con_demanda = _cobertura["propiedades_con_demanda"]
    cartera_activa = _cobertura["propiedades_activas"]
    pct_cartera_con_demanda = _cobertura["pct_cartera_con_demanda"]
    comision_venta_uf = 0.0
    comision_arriendo_uf = 0.0
    venta_afectadas_min = 0
    arriendo_afectadas_min = 0
    if uf_clp:
        min_venta_uf = MIN_VENTA_CLP / uf_clp
        min_arriendo_uf = MIN_ARRIENDO_CLP / uf_clp
        for p in _props:
            if p["operacion"] == "venta":
                base = p["precio_uf"] * 0.02
                if base < min_venta_uf:
                    venta_afectadas_min += 1
                    base = min_venta_uf
                comision_venta_uf += base
            elif p["operacion"] == "arriendo":
                base = p["precio_uf"] * 0.50
                if base < min_arriendo_uf:
                    arriendo_afectadas_min += 1
                    base = min_arriendo_uf
                comision_arriendo_uf += base
    comision_venta_uf = round(comision_venta_uf, 1)
    comision_arriendo_uf = round(comision_arriendo_uf, 1)
    comision_potencial_uf = round(comision_venta_uf + comision_arriendo_uf, 1)
    # Reconciliación Venta/Arriendo sobre la cartera valorizada: la suma de
    # propiedades valorizadas en venta y arriendo debe igualar la cartera
    # valorizada; las operaciones "Otro" y las sin precio se reportan aparte.
    pct_valorizadas = round(propiedades_cartera_valorizadas / propiedades_cartera * 100, 1) if propiedades_cartera else None
    reconciliacion_pipeline = {
        "propiedades_venta": propiedades_venta,
        "propiedades_arriendo": propiedades_arriendo,
        "propiedades_cartera_valorizadas": propiedades_cartera_valorizadas,
        "suma_venta_arriendo": propiedades_venta + propiedades_arriendo,
        "ok": (propiedades_venta + propiedades_arriendo) == propiedades_cartera_valorizadas,
        "propiedades_otro": propiedades_otro,
        "propiedades_sin_precio": propiedades_sin_precio,
        "propiedades_cartera": propiedades_cartera,
        "propiedades_no_en_cartera": propiedades_no_en_cartera,
        "footer_ok": (propiedades_cartera_valorizadas + propiedades_otro + propiedades_sin_precio) == propiedades_cartera,
    }

    sla_data = {
        # KPI principal CARD 4: "En SLA al corte" (estado al cierre del período).
        "in_sla_pct": sla_panel.get("overall_in_sla_pct"),
        "in_sla_count": sla_panel.get("in_sla_count", 0),
        "out_sla_count": sla_panel.get("out_sla_count", 0),
        "eligible_total": sla_panel.get("eligible_total", 0),
        "managed": sla_panel.get("managed", 0),
        "open": sla_panel.get("open", 0),
        "open_breached": sla_panel.get("open_breached", 0),
        "not_evaluable": sla_panel.get("not_evaluable", 0),
        "excluded_tests": sla_panel.get("excluded_tests", 0),
        "resolved_compliance_pct": sla_panel.get("resolved_compliance_pct"),
        "lead": sla_panel.get("lead", {}),
        "lead_hot": sla_panel.get("lead_hot", {}),
        "hot_threshold_min": 60,
        "normal_threshold_min": 180,
        # Retrocompatibilidad con otros consumidores (PDF, resumen ejecutivo).
        "mediana_general_min": sla_panel.get("overall_median_minutes"),
        "pct_cumplimiento_sla": sla_panel.get("overall_compliance_pct"),
        "mediana_hot_min": (sla_panel.get("lead_hot") or {}).get("median_minutes"),
        "mediana_normal_min": (sla_panel.get("lead") or {}).get("median_minutes"),
        "leads_evaluados": sla_panel.get("eligible_total", 0),
        "no_gestionados": sla_panel.get("no_management", 0),
        "vencidos": sla_panel.get("critical_open", 0),
    }

    src_items = sources.get("current", [])
    src_total = sources.get("total", 0) or 0
    src_total_visitas = sources.get("total_visitas", 0) or 0
    sources_data = {
        "items": [
            {
                "nombre": s.get("nombre", "Otro"),
                "cantidad": s.get("cantidad", 0),
                "visitas": s.get("visitas", 0),
                "conversion_pct": s.get("conversion_pct"),
                "pct": s.get("pct", 0.0),
                "prev": s.get("prev", 0),
                "diff": s.get("cantidad", 0) - s.get("prev", 0),
                "funnel": s.get("funnel", []),
            }
            for s in src_items
        ],
        "total": src_total,
        "total_visitas": src_total_visitas,
    }

    insights = build_executive_insights(
        demand={"variation_pct": trends.get("variation_pct")},
        conversion={"conversion_pct": conv_pct, "previous_pct": prev_pct},
        sla=sla_data,
        sources=sources_data,
        pipeline=pipeline,
        funnel=funnel,
    )

    serialization_started = time.perf_counter()
    result = _sanitize_non_finite({
        "period": {
            "preset": preset,
            "comparison_mode": mode,
            "compare_resolved": "none" if mode == "none" else ("yoy" if mode == "yoy" else "prev"),
            "current": {"start": period_start, "end": period_end},
            "previous": {"start": prev_start or "", "end": prev_end or ""},
        },
        "demand": {
            "total": total_leads,
            "previous": previous.get("total", 0) if prev_start else 0,
            "variation_pct": trends.get("variation_pct"),
            "avg_daily": current.get("avg_daily", 0),
            "daily": {
                "labels": [d.get("date") for d in daily],
                "values": [d.get("received", 0) for d in daily],
            },
            "daily_history": {
                "labels": [d.get("date") for d in daily_history],
                "values": [d.get("received", 0) for d in daily_history],
            },
            "previous_daily": {
                "labels": [d.get("date") for d in previous.get("daily", [])],
                "values": [d.get("received", 0) for d in previous.get("daily", [])],
            },
        },
        "conversion": {
            "leads": conv_total,
            "leads_previous": prev_total if prev_start else 0,
            "evaluable_leads": conv_evaluable,
            "evaluable_leads_previous": prev_evaluable if prev_start else 0,
            "traceability_pct": traceability_pct,
            "orders_ambiguous": orders_ambiguous,
            "citas": conv_citas,
            "citas_previous": prev_citas if prev_start else 0,
            "conversion_pct": conv_pct,
            "previous_pct": prev_pct if prev_start else None,
            "diff_pp": diff_pp,
            "ratio_leads_per_cita": ratio,
        },
        "pipeline": {
            "monto_uf": monto_uf,
            "comision_potencial_uf": comision_potencial_uf,
            "comision_venta_uf": comision_venta_uf,
            "comision_arriendo_uf": comision_arriendo_uf,
            "comision_venta_afectadas_min": venta_afectadas_min,
            "comision_arriendo_afectadas_min": arriendo_afectadas_min,
            "comision_policy": "2% venta (m\u00edn $1.000.000) \u00b7 50% arriendo (m\u00edn $100.000) \u00b7 neto de IVA",
            "pct_venta": pct_venta,
            "pct_arriendo": pct_arriendo,
            "pct_otro": pct_otro,
            "monto_venta_uf": venta_uf,
            "monto_arriendo_uf": arriendo_uf,
            "monto_otro_uf": otro_uf,
            "propiedades_vinculadas": propiedades_vinculadas,
            "propiedades_cartera": propiedades_cartera,
            "propiedades_cartera_valorizadas": propiedades_cartera_valorizadas,
            "propiedades_valorizadas": propiedades_cartera_valorizadas,
            "propiedades_venta": propiedades_venta,
            "propiedades_arriendo": propiedades_arriendo,
            "propiedades_otro": propiedades_otro,
            "propiedades_sin_precio": propiedades_sin_precio,
            "propiedades_no_en_cartera": propiedades_no_en_cartera,
            "propiedades_con_demanda": propiedades_con_demanda,
            "cartera_activa": cartera_activa,
            "pct_cartera_con_demanda": pct_cartera_con_demanda,
            "reconciliacion": reconciliacion_pipeline,
            "pct_valorizadas": pct_valorizadas,
            "leads_vinculados": pipeline.get("leads_vinculados", 0),
            "pct_cobertura": cobertura,
            "fecha_uf": uf_asof,
            "valor_uf_clp": uf_value,
            "monto_clp": round(monto_uf * uf_value, 0) if uf_value else None,
            "comision_clp": round(comision_potencial_uf * uf_value, 0) if uf_value else None,
            "suma_componentes_uf": round(venta_uf + arriendo_uf + otro_uf, 1),
            "diferencia_redondeo_uf": round(monto_uf - (venta_uf + arriendo_uf + otro_uf), 1),
            "pct_conciliacion": round((venta_uf + arriendo_uf + otro_uf) / monto_uf * 100, 1) if monto_uf else 100.0,
        },
        "funnel": funnel,
        "sla": sla_data,
        "sources": sources_data,
        "insights": insights,
        "meta": {
            "target": meta_target,
            "global_target": target_info.get("global_target") if target_info.get("available") else meta_target,
            "label": "Leads recibidos (meta prorrateada al período)",
            "days_in_period": (pe_dt - ps_dt).days + 1,
        },
    })
    record_timing("serialization", serialization_started)
    _cache_set(key, result)
    if timing is not None:
        timing["total_ms"] = round((time.perf_counter() - request_started) * 1000, 1)
        logger.info(
            "[OVERVIEW_TIMING] cache=MISS total_ms=%.1f concurrent_ms=%.1f components=%s details=%s",
            timing["total_ms"], timing.get("concurrent_block_ms", 0),
            timing.get("components", {}), timing.get("component_details", {}),
        )
    return result
