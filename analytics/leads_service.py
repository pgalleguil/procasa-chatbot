"""Read-only analytics service for the Leads Dashboard."""
from __future__ import annotations

import time
from typing import Optional

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
)

L1_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 120
MAX_CACHE_ENTRIES = 200


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
    priorities = query_priorities(
        executive=exec_filter if exec_filter else None,
    )
    exec_load = query_executive_load(
        executive=exec_filter if exec_filter else None,
    )
    source_qual = query_source_quality(
        executive=exec_filter if exec_filter else None,
    )
    distributions = query_distributions(
        executive=exec_filter if exec_filter else None,
        universe="current_active",
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

    prev_total = comparative.get("previous", {}).get("total", 0)
    var_pct = comparative.get("variation_pct", 0)
    var_sign = "mas" if var_pct > 0 else "menos"

    ages = []
    for item in exec_load.get("executives", []):
        a = item.get("median_age_days", 0)
        if a:
            ages.append(a)
    median_age = round(sum(ages) / len(ages)) if ages else 0

    result = {
        "meta": {
            "period": {"start": period_start, "end": period_end, "timezone": "America/Santiago"},
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cache_ttl_seconds": CACHE_TTL,
        },
        "kpis": {
            "received": {"value": received, "variation_pct": var_pct, "variation_label": f"{abs(var_pct)}% {var_sign} que el periodo anterior"},
            "active": {"value": total_active, "scope": "actual"},
            "hot": {"value": hot, "pct": round(hot / total_active * 100, 1) if total_active else 0},
            "unassigned": {"value": unassigned, "pct": round(unassigned / total_active * 100, 1) if total_active else 0},
            "aging": {"value": median_age, "scope": "mediana activos"},
        },
        "summary_text": (
            f"En el periodo ingresaron {received} leads, "
            f"un {abs(var_pct)}% {var_sign} que en el periodo anterior. "
            f"Actualmente hay {unassigned} sin asignar y {hot} clasificados como Hot. "
            f"La mediana de antiguedad de la cartera activa es de {median_age} dias."
        ),
        "comparative_trends": comparative,
        "priorities": priorities,
        "executive_load": exec_load,
        "source_quality": source_qual,
        "by_stage": summary.get("by_stage", []),
        "closed_won_current": summary.get("closed_won_current", 0),
        "distributions": distributions,
        "coverage": coverage,
        "quality": summary.get("quality", {}),
    }
    _cache_set(key, result)
    return result
