"""Read-only analytics service for the Leads Dashboard."""
from __future__ import annotations

import logging
import math
import time
import calendar
import json
import copy
import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Optional

from pymongo.errors import NetworkTimeout, ServerSelectionTimeoutError

logger = logging.getLogger(__name__)

from .leads_queries import (
    query_comparative_trends,
    query_funnel,
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
from .portal_costs import build_portal_cost_summary

L1_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 120
CACHE_HARD_TTL = 600
STALE_IF_ERROR_MAX_AGE = CACHE_HARD_TTL
PINNED_REFRESH_AGE = 240
PINNED_MAX_STALE = 1800
PINNED_DASHBOARD_CACHE = ("overview", "operations")
PINNED_STANDARD_PRESETS = ("today", "week", "month", "30d")
MAX_CACHE_ENTRIES = 200
# El overview ejecuta diez consultas independientes. Mantener seis workers
# dejaba cuatro consultas esperando en cola y alargaba cada carga del panel.
_COMMERCIAL_QUERY_POOL = ThreadPoolExecutor(max_workers=10, thread_name_prefix="commercial_analytics")
_CACHE_REFRESH_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="analytics_cache_refresh")
_CACHE_REFRESH_LOCK = Lock()
_CACHE_REFRESHING: set[str] = set()
_SINGLEFLIGHT_LOCK = Lock()
_SINGLEFLIGHT: dict[str, Future] = {}


class InventoryTemporarilyUnavailable(RuntimeError):
    """Mongo cannot serve the inventory and no usable stale payload exists."""


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
    if key in L1_CACHE:
        L1_CACHE[key] = (time.time(), value)
        return
    if len(L1_CACHE) >= MAX_CACHE_ENTRIES:
        pinned_keys = {pinned_key for _, pinned_key, _ in _pinned_dashboard_jobs()}
        candidates = [cache_key for cache_key in L1_CACHE if cache_key not in pinned_keys]
        oldest = min(candidates or L1_CACHE, key=lambda k: L1_CACHE[k][0])
        del L1_CACHE[oldest]
    L1_CACHE[key] = (time.time(), value)


def _cache_state(key: str):
    """Return a copy of the cached payload and its SWR freshness state."""
    entry = L1_CACHE.get(key)
    if not entry:
        return None, None, None
    age = max(0.0, time.time() - entry[0])
    if age < CACHE_TTL:
        return copy.deepcopy(entry[1]), age, "fresh"
    if age < CACHE_HARD_TTL:
        return copy.deepcopy(entry[1]), age, "soft"
    return None, age, "hard"


def _cache_entry_age(key: str) -> float | None:
    entry = L1_CACHE.get(key)
    if not entry:
        return None
    return max(0.0, time.time() - entry[0])


def _cache_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _cache_log(endpoint: str, key: str, state: str, *, age: float | None = None,
               refresh: str | None = None, level: int = logging.INFO) -> None:
    """Emit low-volume cache state without exposing dates or dashboard data."""
    fields = [f"endpoint={endpoint}", f"state={state}", f"key_hash={_cache_hash(key)}"]
    if age is not None:
        fields.append(f"age_seconds={age:.1f}")
    if refresh:
        fields.append(f"refresh={refresh}")
    logger.log(level, "[DASHBOARD_CACHE] %s", " ".join(fields))


def _stale_payload(payload: dict, age: float | None, *, degraded: bool) -> dict:
    stale = copy.deepcopy(payload)
    metadata = dict(stale.get("meta") or {})
    metadata.update({
        "data_status": "stale",
        "degraded": degraded,
        "stale_age_seconds": round(age or 0, 1),
        "refresh": "scheduled",
    })
    stale["meta"] = metadata
    return stale


def _schedule_cache_refresh(key: str, loader) -> bool:
    """Schedule one best-effort refresh per key and preserve stale data on error."""
    with _CACHE_REFRESH_LOCK:
        if key in _CACHE_REFRESHING:
            return False
        _CACHE_REFRESHING.add(key)

    def refresh():
        try:
            value = _singleflight_compute(key, loader)
            if value is not None:
                _cache_set(key, value)
            _cache_log("unknown", key, "REFRESHED", level=logging.DEBUG)
        except Exception:
            logger.warning("[DASHBOARD_CACHE] background refresh failed key_hash=%s", _cache_hash(key), exc_info=True)
        finally:
            with _CACHE_REFRESH_LOCK:
                _CACHE_REFRESHING.discard(key)

    _CACHE_REFRESH_POOL.submit(refresh)
    return True


def _singleflight_compute(key: str, loader, *, endpoint: str = "unknown", timing: dict | None = None):
    """Compute one key once and share its result with concurrent callers."""
    with _SINGLEFLIGHT_LOCK:
        future = _SINGLEFLIGHT.get(key)
        if future is None:
            future = Future()
            _SINGLEFLIGHT[key] = future
            owner = True
        else:
            owner = False

    if not owner:
        if timing is not None:
            timing["singleflight"] = "waiter"
        _cache_log(endpoint, key, "WAIT", level=logging.DEBUG)
        return future.result()

    if timing is not None:
        timing["singleflight"] = "owner"
    try:
        value = loader()
        future.set_result(value)
        return value
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _SINGLEFLIGHT_LOCK:
            if _SINGLEFLIGHT.get(key) is future:
                del _SINGLEFLIGHT[key]


def _dashboard_standard_periods():
    """Return the four canonical dashboard preset ranges."""
    from .commercial_periods import local_today, preset_range
    today = local_today()
    return tuple(
        (preset, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        for preset in PINNED_STANDARD_PRESETS
        for start, end in [preset_range(preset, today)]
    )


def _dashboard_30d_period():
    return next((start, end) for preset, start, end in _dashboard_standard_periods() if preset == "30d")


def _overview_request_key(period_start: str, period_end: str, compare: str, period_preset: str) -> str:
    from .commercial_periods import canonical_preset, comparison_period
    ps_dt = datetime.strptime(period_start, "%Y-%m-%d").date()
    pe_dt = datetime.strptime(period_end, "%Y-%m-%d").date()
    mode = compare if compare in ("auto", "prev", "yoy", "none") else "auto"
    preset = canonical_preset(ps_dt, pe_dt, period_preset)
    comp_start, comp_end, _ = comparison_period(ps_dt, pe_dt, mode, preset)
    return _cache_key(
        "leads-dashboard-overview", ps=period_start, pe=period_end, cmp=mode,
        preset=preset,
        ps_prev=comp_start.strftime("%Y-%m-%d") if comp_start else None,
        pe_prev=comp_end.strftime("%Y-%m-%d") if comp_end else None,
    )


def _pinned_dashboard_jobs():
    """Return real standard Overview/Operations keys and uncached loaders."""
    jobs = []
    for preset, period_start, period_end in _dashboard_standard_periods():
        overview_key = _overview_request_key(period_start, period_end, "auto", preset)
        operations_key = _cache_key(
            "leads-operational", ps=period_start, pe=period_end, compare="auto", preset=preset,
            filters=repr([]),
        )
        jobs.extend((
            ("overview", overview_key, lambda ps=period_start, pe=period_end, pp=preset:
             _compute_leads_dashboard_overview(
                 period_start=ps, period_end=pe, compare="auto", period_preset=pp,
                 timing=None, _cache_bypass=True,
             )),
            ("operations", operations_key, lambda ps=period_start, pe=period_end, pp=preset:
             _compute_leads_operational_dashboard(
                 period_start=ps, period_end=pe, compare="auto", period_preset=pp,
                 role=None, user_name=None, filters={}, timing=None, _cache_bypass=True,
             )),
        ))
    return tuple(jobs)


def warm_pinned_dashboard_cache() -> list[str]:
    """Warm standard keys on explicit/manual request only."""
    warmed = []
    for endpoint, key, loader in _pinned_dashboard_jobs():
        age = _cache_entry_age(key)
        if age is not None and age < PINNED_REFRESH_AGE:
            continue
        value = _singleflight_compute(key, loader, endpoint=endpoint)
        _cache_set(key, value)
        _cache_log(endpoint, key, "MISS", age=age)
        warmed.append(endpoint)
    return warmed


def keep_pinned_dashboard_cache() -> list[str]:
    """Schedule at most one Overview and one Operations refresh when due."""
    due = []
    for endpoint, key, loader in _pinned_dashboard_jobs():
        age = _cache_entry_age(key)
        if age is None or age >= PINNED_REFRESH_AGE:
            due.append((float("inf") if age is None else age, endpoint, key, loader, age))

    selected = []
    for endpoint in ("overview", "operations"):
        candidates = [item for item in due if item[1] == endpoint]
        if candidates:
            selected.append(max(candidates, key=lambda item: item[0]))
    selected_keys = {item[2] for item in selected}
    remaining = sorted(
        (item for item in due if item[2] not in selected_keys),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    selected.extend(remaining[:max(0, 2 - len(selected))])

    scheduled = []
    for _, endpoint, key, loader, age in selected[:2]:
        if _schedule_cache_refresh(key, loader):
            _cache_log(endpoint, key, "STALE", age=age, refresh="scheduled")
            scheduled.append(endpoint)
    return scheduled


def _compute_properties_inventory_dashboard(
    period_start: str = None,
    period_end: str = None,
    filters: dict | None = None,
    timing: dict | None = None,
) -> dict:
    """Uncached read-only payload for the lazy Propiedades & Inventario tab."""
    filters = {key: value for key, value in (filters or {}).items() if value not in (None, "")}
    return _sanitize_non_finite(query_demand_capture_dashboard(period_start, period_end, filters))


def get_properties_inventory_dashboard(
    period_start: str = None,
    period_end: str = None,
    filters: dict | None = None,
    timing: dict | None = None,
) -> dict:
    """SWR-cached read-only payload for the lazy Propiedades & Inventario tab."""
    started = time.perf_counter()
    filters = {key: value for key, value in (filters or {}).items() if value not in (None, "")}
    key = _cache_key("leads-properties-inventory", ps=period_start, pe=period_end, **filters)
    cached, age, state = _cache_state(key)
    if state == "fresh":
        _cache_log("properties", key, "HIT", age=age, level=logging.DEBUG)
        if timing is not None:
            timing.update({"cache": "HIT", "total_ms": round((time.perf_counter() - started) * 1000, 1), "mongo_calls": 0})
        return cached

    def load():
        return _compute_properties_inventory_dashboard(period_start, period_end, filters, timing=None)

    if state == "soft":
        scheduled = _schedule_cache_refresh(key, load)
        _cache_log("properties", key, "STALE", age=age, refresh="scheduled" if scheduled else "already_pending")
        stale_payload = _stale_payload(cached, age, degraded=False)
        if timing is not None:
            timing.update({"cache": "STALE", "stale_age_seconds": round(age or 0, 1),
                           "refresh": "scheduled", "total_ms": round((time.perf_counter() - started) * 1000, 1),
                           "mongo_calls": 0})
        return stale_payload

    if timing is not None:
        timing["cache"] = "MISS"
    _cache_log("properties", key, "MISS", age=age)
    try:
        payload = _singleflight_compute(key, load, endpoint="properties", timing=timing)
    except (NetworkTimeout, ServerSelectionTimeoutError) as exc:
        logger.warning("[DASHBOARD_CACHE] properties miss unavailable key_hash=%s", _cache_hash(key))
        if timing is not None:
            timing.update({"degraded": True, "total_ms": round((time.perf_counter() - started) * 1000, 1)})
        raise InventoryTemporarilyUnavailable from exc
    _cache_set(key, payload)
    if timing is not None:
        timing.update({"total_ms": round((time.perf_counter() - started) * 1000, 1), "mongo_calls": 2})
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


def _compute_leads_operational_dashboard(
    period_start: str = None,
    period_end: str = None,
    compare: str = "auto",
    period_preset: str = None,
    role: str = None,
    user_name: str = None,
    filters: dict = None,
    timing: dict | None = None,
    _cache_bypass: bool = False,
) -> dict:
    """Uncached dashboard operativo computation."""
    filters = dict(filters or {})
    if role not in ("admin", "supervisor") and user_name:
        filters["executive"] = user_name
    key = _cache_key("leads-operational", ps=period_start, pe=period_end, compare=compare, preset=period_preset, filters=repr(sorted(filters.items())))
    started = time.perf_counter()
    cached = _cache_get(key) if not _cache_bypass else None
    if cached is not None:
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
    if not _cache_bypass:
        _cache_set(key, data)
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
    """Dashboard operativo con caché fresh/stale-while-revalidate."""
    filters = dict(filters or {})
    if role not in ("admin", "supervisor") and user_name:
        filters["executive"] = user_name
    key = _cache_key(
        "leads-operational", ps=period_start, pe=period_end, compare=compare,
        preset=period_preset, filters=repr(sorted(filters.items())),
    )
    started = time.perf_counter()
    cached, age, state = _cache_state(key)
    if state == "fresh":
        _cache_log("operations", key, "HIT", age=age, level=logging.DEBUG)
        if timing is not None:
            timing.update({"cache": "HIT", "total_ms": round((time.perf_counter() - started) * 1000, 1), "mongo_calls": 0})
        return cached

    def load():
        return _compute_leads_operational_dashboard(
            period_start=period_start, period_end=period_end, compare=compare,
            period_preset=period_preset, role=role, user_name=user_name,
            filters=filters, timing=None, _cache_bypass=True,
        )

    if state == "soft":
        scheduled = _schedule_cache_refresh(key, load)
        _cache_log("operations", key, "STALE", age=age, refresh="scheduled" if scheduled else "already_pending")
        stale_payload = _stale_payload(cached, age, degraded=False)
        if timing is not None:
            timing.update({"cache": "STALE", "stale_age_seconds": round(age or 0, 1),
                           "refresh": "scheduled", "total_ms": round((time.perf_counter() - started) * 1000, 1),
                           "mongo_calls": 0})
        return stale_payload

    if timing is not None:
        timing["cache"] = "MISS"
    _cache_log("operations", key, "MISS", age=age)
    result = _singleflight_compute(key, load, endpoint="operations", timing=timing)
    _cache_set(key, result)
    if timing is not None:
        timing["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


def get_leads_dashboard_overview(
    period_start: str = None,
    period_end: str = None,
    compare: str = None,
    period_preset: str = None,
    timing: dict | None = None,
) -> dict:
    """Public Overview endpoint using bounded SWR and single-flight caching."""
    from .commercial_periods import canonical_preset, local_today, preset_range, comparison_period

    today = local_today()
    try:
        ps_dt = datetime.strptime(period_start, "%Y-%m-%d").date() if period_start else today - timedelta(days=29)
        pe_dt = datetime.strptime(period_end, "%Y-%m-%d").date() if period_end else today
        pe_dt = min(pe_dt, today)
        ps_dt = min(ps_dt, pe_dt)
    except (ValueError, TypeError):
        pe_dt, ps_dt = today, today - timedelta(days=29)
    if period_preset in PINNED_STANDARD_PRESETS:
        ps_dt, pe_dt = preset_range(period_preset, today)
    ps, pe = ps_dt.strftime("%Y-%m-%d"), pe_dt.strftime("%Y-%m-%d")
    mode = compare if compare in ("auto", "prev", "yoy", "none") else "auto"
    requested_preset = period_preset if period_preset in (*PINNED_STANDARD_PRESETS, "custom") else "custom"
    preset = canonical_preset(ps_dt, pe_dt, requested_preset)
    comp_start, comp_end, _ = comparison_period(ps_dt, pe_dt, mode, preset)
    key = _cache_key(
        "leads-dashboard-overview", ps=ps, pe=pe, cmp=mode, preset=preset,
        ps_prev=comp_start.strftime("%Y-%m-%d") if comp_start else None,
        pe_prev=comp_end.strftime("%Y-%m-%d") if comp_end else None,
    )
    started = time.perf_counter()
    cached, age, state = _cache_state(key)
    pinned = any(key == pinned_key for _, pinned_key, _ in _pinned_dashboard_jobs())

    if state == "fresh":
        _cache_log("overview", key, "HIT", age=age, level=logging.DEBUG)
        if timing is not None:
            timing.update({"cache": "HIT", "total_ms": round((time.perf_counter() - started) * 1000, 1), "mongo_calls": 0})
        return cached

    def load():
        return _compute_leads_dashboard_overview(
            period_start=ps, period_end=pe, compare=mode, period_preset=preset,
            timing=None, _cache_bypass=True,
        )

    if state == "soft":
        scheduled = _schedule_cache_refresh(key, load)
        _cache_log("overview", key, "STALE", age=age, refresh="scheduled" if scheduled else "already_pending")
        stale_payload = _stale_payload(cached, age, degraded=False)
        if timing is not None:
            timing.update({"cache": "STALE", "stale_age_seconds": round(age or 0, 1),
                           "refresh": "scheduled", "total_ms": round((time.perf_counter() - started) * 1000, 1),
                           "mongo_calls": 0})
        return stale_payload

    if state == "hard" and pinned and age is not None and age <= PINNED_MAX_STALE:
        entry = L1_CACHE.get(key)
        if entry:
            stale_payload = _stale_payload(entry[1], age, degraded=True)
            scheduled = _schedule_cache_refresh(key, load)
            _cache_log("overview", key, "STALE", age=age, refresh="scheduled" if scheduled else "already_pending")
            if timing is not None:
                timing.update({"cache": "STALE", "degraded": True,
                               "stale_age_seconds": round(age, 1), "refresh": "scheduled",
                               "total_ms": round((time.perf_counter() - started) * 1000, 1),
                               "mongo_calls": 0})
            return stale_payload

    if timing is not None:
        timing["cache"] = "MISS"
    _cache_log("overview", key, "MISS", age=age)
    result = _singleflight_compute(key, load, endpoint="overview", timing=timing)
    _cache_set(key, result)
    if timing is not None:
        timing["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


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


def _compute_leads_dashboard_overview(
    period_start: str = None,
    period_end: str = None,
    compare: str = None,
    period_preset: str = None,
    timing: dict | None = None,
    _cache_bypass: bool = False,
) -> dict:
    """Uncached computation for the CARD 1 (Demanda & Meta) of the dashboard.

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
    cached = _cache_get(key) if not _cache_bypass else None
    if cached is not None:
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
                "prev_visitas": s.get("prev_visitas", 0),
                "prev_pct": s.get("prev_pct"),
                "prev_conversion_pct": s.get("prev_conversion_pct"),
                "funnel": s.get("funnel", []),
            }
            for s in src_items
        ],
        "total": src_total,
        "total_visitas": src_total_visitas,
    }
    sources_data["costs"] = build_portal_cost_summary(
        sources_data["items"], period_start, period_end
    )

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
    if not _cache_bypass:
        _cache_set(key, result)
    if timing is not None:
        timing["total_ms"] = round((time.perf_counter() - request_started) * 1000, 1)
        logger.info(
            "[OVERVIEW_TIMING] cache=MISS total_ms=%.1f concurrent_ms=%.1f components=%s details=%s",
            timing["total_ms"], timing.get("concurrent_block_ms", 0),
            timing.get("components", {}), timing.get("component_details", {}),
        )
    return result
