"""Read-only analytics service for the Leads Dashboard."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
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
)

L1_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 120
MAX_CACHE_ENTRIES = 200
_COMMERCIAL_QUERY_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="commercial_analytics")


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


def _cache_set(key: str, value: dict):
    if len(L1_CACHE) >= MAX_CACHE_ENTRIES:
        oldest = min(L1_CACHE, key=lambda k: L1_CACHE[k][0])
        del L1_CACHE[oldest]
    L1_CACHE[key] = (time.time(), value)


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


def get_detail(lead_id: str) -> dict | None:
    return query_detail(lead_id)


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
        query_demand_by_price_ranges,
        query_commercial_executive_matrix,
        query_commercial_property_ranking,
        query_commercial_insights,
        query_source_performance,
        query_comparative_trends,
        query_field_coverage,
        query_executive_summary,
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
        "demand": _COMMERCIAL_QUERY_POOL.submit(query_demand_by_price_ranges, **kwargs),
        "executives": _COMMERCIAL_QUERY_POOL.submit(query_commercial_executive_matrix, **kwargs),
        "properties": _COMMERCIAL_QUERY_POOL.submit(query_commercial_property_ranking, **kwargs),
        "sources": _COMMERCIAL_QUERY_POOL.submit(query_source_performance, **kwargs, **comparison_kwargs),
        "trends": _COMMERCIAL_QUERY_POOL.submit(query_comparative_trends, **kwargs, **comparison_kwargs),
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
    executive_summary = query_executive_summary(
        **kwargs, **comparison_kwargs, sla_risk=sla,
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
    coverage = futures["coverage"].result()
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
        "executive_summary": executive_summary,
        "demand_by_price": demand_price,
        "executives": executives,
        "properties": properties,
        "sources": sources,
        "trends": trends,
        "insights": insights,
        "coverage": coverage,
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
