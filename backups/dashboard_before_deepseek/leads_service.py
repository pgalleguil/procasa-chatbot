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
    query_funnel,
    query_management_metrics,
    query_property_ranking,
    query_executive_load_detail,
    query_source_performance,
    query_entry_pulse,
    query_entry_forecast,
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


def _get_dashboard_legacy(
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


def get_dashboard(
    period_start: str = None,
    period_end: str = None,
    executive: str = None,
    role: str = None,
    user_name: str = None,
    filters: dict = None,
) -> dict:
    """Payload definitivo, consolidado y estrictamente read-only."""
    exec_filter = executive if role in ("admin", "supervisor") else user_name
    key = _cache_key("dashboard-v2", ps=period_start, pe=period_end, exec=exec_filter,
                     role=role, filters=repr(sorted((filters or {}).items())))
    cached = _cache_get(key)
    if cached:
        return cached

    kwargs = {"period_start": period_start, "period_end": period_end,
              "executive": exec_filter or None, "filters": filters}
    summary = query_summary(**kwargs)
    comparison = query_comparative_trends(**kwargs)
    cohort = query_funnel(**kwargs)
    priorities = query_priorities(executive=exec_filter or None, filters=filters)
    executive_load = query_executive_load_detail(executive=exec_filter or None, filters=filters)
    sources = query_source_performance(**kwargs)
    properties = query_property_ranking(**kwargs)
    demand = query_distributions(**kwargs, universe="received_in_period")
    coverage = query_field_coverage(executive=exec_filter or None, filters=filters)
    management = query_management_metrics(executive=exec_filter or None)

    received = summary["flow"]["received_in_period"]
    previous = comparison["previous"]["total"]
    variation = round((received - previous) / previous * 100, 1) if previous else None
    variation_direction = "up" if variation is not None and variation >= 0 else "down" if variation is not None else "none"

    # Las fuentes con muestra pequeña se agregan antes de llegar al frontend.
    comparable = [row for row in sources if row["received"] >= 15]
    small = [row for row in sources if row["received"] < 15]
    grouped_sources = list(comparable)
    if small:
        total = sum(row["received"] for row in small)
        def weighted(field):
            return round(sum(row[field] * row["received"] for row in small) / total, 1) if total else 0
        grouped_sources.append({
            "source": "Otras fuentes — bajo volumen", "received": total,
            "pct_of_total": round(sum(row["pct_of_total"] for row in small), 1),
            "hot_pct": weighted("hot_pct"), "assigned_pct": weighted("assigned_pct"),
            "advanced_pct": weighted("advanced_pct"), "variation_pct": None,
            "comparable": False,
        })
    for row in grouped_sources:
        row.setdefault("comparable", row["source"] != "Otras fuentes — bajo volumen")
        row["is_highest_volume"] = False
        row["is_best_profile"] = False
    if grouped_sources:
        max(grouped_sources, key=lambda row: row["received"])["is_highest_volume"] = True
    if comparable:
        max(comparable, key=lambda row: row["advanced_pct"])["is_best_profile"] = True

    field_labels = {
        "prospecto.nombre": "Nombre", "prospecto.origen": "Origen",
        "prospecto.operacion": "Operación", "prospecto.tipo": "Tipo de propiedad",
        "prospecto.comuna": "Comuna", "prospecto.codigo": "Código de propiedad",
        "ejecutivo_asignado": "Ejecutivo", "pipeline_stage": "Etapa",
        "lead_temperature_effective": "Temperatura", "created_at": "Fecha válida",
        "stage_conflict": "Sin conflictos de etapas",
    }
    coverage_rows = []
    for key_name, label in field_labels.items():
        row = coverage.get(key_name, {})
        coverage_rows.append({"key": key_name, "label": label,
                              "populated": row.get("populated", row.get("total", 0) - row.get("conflict_count", 0)),
                              "total": row.get("total", 0), "coverage_pct": row.get("coverage_pct", 0),
                              "conflict_count": row.get("conflict_count")})

    management_total = management.get("total_assigned", 0)
    management_covered = management.get("total_with_evidence", 0)
    management_pct = round(management_covered / management_total * 100, 1) if management_total else 0
    unavailable = [
        {"metric": "Primera respuesta", "status": "Disponible" if management_pct >= 60 else "No disponible",
         "reason": "La cobertura de timestamps verificables es inferior a 60%." if management_pct < 60 else "Cobertura suficiente para análisis secundario.",
         "coverage": {"populated": management_covered, "total": management_total, "pct": management_pct},
         "requirement": "Cobertura verificable igual o superior a 60%."},
        {"metric": "SLA", "status": "No disponible", "reason": "No hay medición canónica completa.", "coverage": None,
         "requirement": "Definición e instrumentación canónica de SLA."},
        {"metric": "Conversión histórica", "status": "No disponible", "reason": "El estado disponible es una fotografía actual.", "coverage": None,
         "requirement": "Historial completo y fechado de etapas."},
        {"metric": "Productividad por ejecutivo", "status": "No disponible", "reason": "La carga actual no demuestra desempeño.", "coverage": None,
         "requirement": "Historial completo de gestiones atribuibles."},
        {"metric": "Costo por lead", "status": "No disponible", "reason": "No existe inversión atribuida por fuente.", "coverage": None,
         "requirement": "Costos de campaña enlazados a cada lead."},
    ]

    pulse = query_entry_pulse(executive=exec_filter or None, filters=filters)
    forecast = query_entry_forecast(executive=exec_filter or None, filters=filters)

    top_alerts_raw = priorities.get("alerts", [])
    severity_order = {"high": 0, "medium": 1, "low": 2}
    top_alerts = sorted(top_alerts_raw, key=lambda a: severity_order.get(a.get("severity", "low"), 99))[:3]

    cohort_steps = cohort
    largest_drop = {}
    if cohort_steps and len(cohort_steps) > 1:
        best_loss = -1
        for i in range(1, len(cohort_steps)):
            loss = Number(cohort_steps[i - 1].get("count", 0)) - Number(cohort_steps[i].get("count", 0))
            if loss > best_loss:
                best_loss = loss
                largest_drop = {
                    "from": cohort_steps[i - 1].get("label", "").replace(" actualmente", ""),
                    "to": cohort_steps[i].get("label", "").replace("Cerrados ganados actualmente", "Ganados").replace(" actualmente", ""),
                    "loss": loss,
                }
    received_count = cohort_steps[0].get("count", 0) if cohort_steps else 0

    comparable_sources = [s for s in grouped_sources if s.get("comparable", False)]
    dominant = max(grouped_sources, key=lambda s: s.get("received", 0)) if grouped_sources else {}
    best = max(comparable_sources, key=lambda s: s.get("advanced_pct", 0)) if comparable_sources else {}

    anomaly = None
    # Check for source drop
    for src in comparable_sources:
        var = src.get("variation_pct")
        if var is not None and var < -30 and src.get("received", 0) >= 10:
            anomaly = {"type": "source_drop", "title": f"Caída en {src['source']}", "detail": f"La fuente {src['source']} presenta una caída de {abs(var):.1f}% frente al periodo anterior.", "severity": "warning", "action_label": "Ver fuentes", "action_target": "sources"}
            break
    # Check for property with high interest low advance
    if not anomaly:
        prop_ranking = properties.get("ranking", [])
        if prop_ranking:
            max_count = max(p.get("count", 0) for p in prop_ranking)
            threshold_count = max_count * 0.8
            candidates = [p for p in prop_ranking if p.get("count", 0) >= threshold_count and p.get("advanced_pct", 100) < 30 and p.get("count", 0) >= 5]
            if candidates:
                p = candidates[0]
                anomaly = {"type": "property_stalled", "title": f"Propiedad {p['code']} con interés sin avance", "detail": f"{p['count']} leads, solo {p['advanced_pct']}% avanzados.", "severity": "info", "action_label": "Ver propiedades", "action_target": "properties"}
    # Check for general entry drop
    if not anomaly and variation is not None and variation < -30:
        anomaly = {"type": "entry_drop", "title": "Caída general de entrada", "detail": f"El volumen de entrada disminuyó {abs(variation):.1f}% frente al periodo anterior.", "severity": "warning", "action_label": "Ver tendencias", "action_target": "trends"}

    # Build headline
    headline = _build_headline(priorities_raw=priorities.get("alerts", []), pulse=pulse, anomaly=anomaly, comparison_variation=variation, grouped_sources=grouped_sources)

    home_section = {
        "generated_at": result_meta["generated_at"],
        "headline": headline,
        "entry_pulse": {
            "today": pulse.get("today", 0),
            "yesterday_same_cut": pulse.get("yesterday_same_cut", 0),
            "daily_variation_pct": pulse.get("daily_variation_pct"),
            "current_week": pulse.get("current_week", 0),
            "previous_week_same_cut": pulse.get("previous_week_same_cut", 0),
            "weekly_variation_pct": pulse.get("weekly_variation_pct"),
            "current_month": pulse.get("current_month", 0),
            "previous_month_same_cut": pulse.get("previous_month_same_cut", 0),
            "monthly_variation_pct": pulse.get("monthly_variation_pct"),
        },
        "top_alerts": top_alerts,
        "cohort_summary": {
            "received": received_count,
            "assigned": cohort_steps[1].get("count", 0) if len(cohort_steps) > 1 else 0,
            "assigned_pct": round((cohort_steps[1].get("count", 0) / received_count * 100), 1) if received_count and len(cohort_steps) > 1 else 0,
            "advanced": cohort_steps[2].get("count", 0) if len(cohort_steps) > 2 else 0,
            "advanced_pct": round((cohort_steps[2].get("count", 0) / received_count * 100), 1) if received_count and len(cohort_steps) > 2 else 0,
            "won": cohort_steps[3].get("count", 0) if len(cohort_steps) > 3 else 0,
            "won_pct": round((cohort_steps[3].get("count", 0) / received_count * 100), 1) if received_count and len(cohort_steps) > 3 else 0,
            "largest_drop": largest_drop,
        },
        "source_summary": {
            "dominant": {"source": dominant.get("source"), "received": dominant.get("received", 0)} if dominant else {},
            "best_profile": {"source": best.get("source"), "advanced_pct": best.get("advanced_pct", 0)} if best else {},
        },
        "weekly_anomaly": anomaly,
        "entry_forecast": forecast,
    }

    result = {
        "meta": result_meta,
        "home": home_section,
        "status_strip": {"received": received, "previous_received": previous,
                         "variation_pct": variation, "variation_direction": variation_direction,
                         "active_current": summary["stock"]["total_active"]},
        "priorities": priorities.get("alerts", []),
        "cohort_status": {"steps": cohort},
        "executive_load": executive_load,
        "source_performance": grouped_sources,
        "demand": {"operations": demand.get("operations", []), "types": demand.get("types", []),
                   "communes": demand.get("communes", [])},
        "property_ranking": properties.get("ranking", []),
        "coverage": {"fields": coverage_rows},
        "unavailable_metrics": unavailable,
    }
    _cache_set(key, result)
    return result
