"""Read-only analytics service for the Leads Dashboard."""
from __future__ import annotations

import logging
import math
import time
import calendar
import json
from datetime import date, timedelta
from pathlib import Path
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


def _pct(value, denominator):
    return round(value / denominator * 100, 1) if denominator else None


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
) -> dict:
    """Resumen para la CARD 1 (Demanda & Meta) del Leads Dashboard.

    Read-only. Calcula leads recibidos en el periodo seleccionado, el periodo
    equivalente anterior, la tendencia diaria (sparkline) y la meta de negocio.
    Reutiliza la misma lógica de periodo/comparación del Dashboard Comercial.
    """
    from datetime import datetime as dt, timedelta as td
    from .commercial_periods import canonical_preset, comparison_period, local_today, preset_range

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
        return cached

    from .leads_queries import (
        query_leads_dashboard_conversion,
        query_leads_dashboard_pipeline,
        query_sla_risk_panel,
        query_leads_dashboard_rescue,
        query_leads_dashboard_sources,
        query_leads_dashboard_executives,
        query_sla_accountability,
        query_leads_dashboard_funnel,
        query_leads_dashboard_reconcile_breakdown,
    )

    f_trends = _COMMERCIAL_QUERY_POOL.submit(
        query_comparative_trends,
        period_start=period_start,
        period_end=period_end,
        comparison_start=prev_start,
        comparison_end=prev_end,
        include_comparison=bool(prev_start),
    )
    f_conv = _COMMERCIAL_QUERY_POOL.submit(
        query_leads_dashboard_conversion,
        period_start=period_start,
        period_end=period_end,
        comparison_start=prev_start,
        comparison_end=prev_end,
        include_comparison=bool(prev_start),
    )
    f_pipe = _COMMERCIAL_QUERY_POOL.submit(
        query_leads_dashboard_pipeline,
        period_start=period_start,
        period_end=period_end,
    )
    f_sla = _COMMERCIAL_QUERY_POOL.submit(
        query_sla_risk_panel,
        period_start=period_start,
        period_end=period_end,
    )
    f_rescue = _COMMERCIAL_QUERY_POOL.submit(
        query_leads_dashboard_rescue,
        period_start=period_start,
        period_end=period_end,
    )
    f_sources = _COMMERCIAL_QUERY_POOL.submit(
        query_leads_dashboard_sources,
        period_start=period_start,
        period_end=period_end,
        comparison_start=prev_start,
        comparison_end=prev_end,
        include_comparison=bool(prev_start),
    )
    f_exec = _COMMERCIAL_QUERY_POOL.submit(
        query_leads_dashboard_executives,
        period_start=period_start,
        period_end=period_end,
        comparison_start=prev_start,
        comparison_end=prev_end,
        include_comparison=bool(prev_start),
    )
    f_sla_acc = _COMMERCIAL_QUERY_POOL.submit(
        query_sla_accountability,
        period_start=period_start,
        period_end=period_end,
    )
    f_funnel = _COMMERCIAL_QUERY_POOL.submit(
        query_leads_dashboard_funnel,
        period_start=period_start,
        period_end=period_end,
    )
    f_reconcile = _COMMERCIAL_QUERY_POOL.submit(
        query_leads_dashboard_reconcile_breakdown,
        period_start=period_start,
        period_end=period_end,
    )

    trends = f_trends.result()
    conversion = f_conv.result()
    pipeline = f_pipe.result()
    sla_panel = f_sla.result()
    rescue = f_rescue.result()
    sources = f_sources.result()
    exec_rows = f_exec.result()
    sla_acc = f_sla_acc.result()
    try:
        funnel = f_funnel.result()
    except Exception as exc:
        logger.warning("Leads dashboard funnel unavailable: %s", exc)
        funnel = {"received": 0, "stages": []}
    try:
        reconcile_breakdown = f_reconcile.result()
    except Exception as exc:
        logger.warning("Leads dashboard reconciliation unavailable: %s", exc)
        reconcile_breakdown = {"items": [], "total": 0}

    current = trends.get("current", {})
    previous = trends.get("previous", {})
    daily = current.get("daily", []) or []
    # La meta mensual debe prorratearse según los días calendario realmente
    # seleccionados. Evita comparar, por ejemplo, 2 días contra la meta total
    # de 200 leads del mes.
    target_info = _executive_target_info(period_start, period_end, today=today)
    meta_target = target_info.get("target") if target_info.get("available") else _load_received_leads_meta_target()

    conv_current = conversion.get("current", {})
    conv_previous = conversion.get("previous", {})
    conv_total = conv_current.get("total", 0)
    conv_citas = conv_current.get("citas", 0)
    prev_total = conv_previous.get("total", 0)
    prev_citas = conv_previous.get("citas", 0)
    conv_pct = round(conv_citas / conv_total * 100, 1) if conv_total else 0.0
    prev_pct = round(prev_citas / prev_total * 100, 1) if prev_total else 0.0
    diff_pp = round(conv_pct - prev_pct, 1) if prev_start else None
    ratio = round(conv_total / conv_citas, 1) if conv_citas else None

    total_leads = current.get("total", 0)
    monto_uf = pipeline.get("monto_uf", 0.0)
    venta_uf = pipeline.get("monto_venta_uf", 0.0)
    arriendo_uf = pipeline.get("monto_arriendo_uf", 0.0)
    otro_uf = pipeline.get("monto_otro_uf", 0.0)
    pct_venta = round(venta_uf / monto_uf * 100, 1) if monto_uf else 0.0
    pct_arriendo = round(arriendo_uf / monto_uf * 100, 1) if monto_uf else 0.0
    pct_otro = round(otro_uf / monto_uf * 100, 1) if monto_uf else 0.0
    cobertura = round(pipeline.get("leads_vinculados", 0) / total_leads * 100, 1) if total_leads else 0.0
    comision_pct = 4.0  # comisión para la oficina
    comision_venta_uf = round(venta_uf * 0.04, 1)       # 4% sobre ventas
    comision_arriendo_uf = round(arriendo_uf * 0.50 * 2, 1)  # 50% arrendatario + 50% arrendador (1er mes)
    comision_uf = round(comision_venta_uf + comision_arriendo_uf, 1)

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

    sla_data = {
        "mediana_general_min": sla_panel.get("overall_median_minutes"),
        "pct_cumplimiento_sla": sla_panel.get("overall_compliance_pct"),
        "mediana_hot_min": (sla_panel.get("lead_hot") or {}).get("median_minutes"),
        "mediana_normal_min": (sla_panel.get("lead") or {}).get("median_minutes"),
        "leads_evaluados": sla_panel.get("eligible_total", 0),
        "no_gestionados": sla_panel.get("no_management", 0),
        "vencidos": sla_panel.get("critical_open", 0),
        "hot_threshold_min": 60,
        "normal_threshold_min": 180,
    }

    rescatados = rescue.get("recuperabilidad_alta", 0)
    vencidos = sla_panel.get("critical_open", 0)
    eligible = sla_panel.get("eligible_total", 0) or 0
    pct_al_dia = round((1 - vencidos / eligible) * 100, 1) if eligible else 100.0
    pct_rescatados = round(rescatados / total_leads * 100, 1) if total_leads else 0.0
    rescue_data = {
        "leads_rescatados": rescatados,
        "pct_rescatados": pct_rescatados,
        "vencidos_abiertos": vencidos,
        "pct_al_dia": pct_al_dia,
    }

    src_items = sources.get("current", [])
    src_prev = sources.get("previous", {}) or {}
    src_total = sources.get("total", 0) or 0
    sources_data = {
        "items": [
            {
                "nombre": s.get("nombre", "Otro"),
                "cantidad": s.get("cantidad", 0),
                "pct": round(s.get("cantidad", 0) / src_total * 100, 1) if src_total else 0.0,
                "diff": s.get("cantidad", 0) - src_prev.get(s.get("nombre", ""), 0),
            }
            for s in src_items
        ],
        "total": src_total,
    }

    sla_by_exec = {
        str(r.get("executive_name")): (r.get("lead") or {}).get("median_business_minutes")
        for r in (sla_acc.get("by_executive") or [])
    }

    def _estado(name, leads_count, sla_median):
        if name == "Sin Asignar":
            return "Auto Rescate"
        if sla_median is None:
            return "En Regla"
        if sla_median < 30:
            return "Top Performer" if leads_count >= 60 else "En Regla"
        return "SLA Crítico"

    _sum_exec = sum(r.get("leads", 0) for r in exec_rows)
    executives_data = {
        "items": [
            {
                "nombre": r.get("ejecutivo", "Sin Asignar"),
                "leads": r.get("leads", 0),
                "leads_prev": r.get("leads_prev", 0),
                "diff_leads": r.get("diff_leads", 0),
                "pct_leads": round(r.get("leads", 0) / total_leads * 100, 1) if total_leads else 0.0,
                "citas": r.get("citas", 0),
                "conversion_pct": round(r.get("citas", 0) / r.get("leads", 0) * 100, 1) if r.get("leads", 0) else 0.0,
                "uf": r.get("uf", 0.0),
                "sla_median": sla_by_exec.get(r.get("ejecutivo", "Sin Asignar")),
                "estado": _estado(r.get("ejecutivo", "Sin Asignar"), r.get("leads", 0), sla_by_exec.get(r.get("ejecutivo", "Sin Asignar"))),
            }
            for r in exec_rows
        ],
        "total": len(exec_rows),
        "reconcile": {
            "contabilizado": _sum_exec,
            "total": total_leads,
            "otros": max(total_leads - _sum_exec, 0),
            "pct": round(_sum_exec / total_leads * 100, 1) if total_leads else 0.0,
            "desglose_otros": (reconcile_breakdown or {}).get("items", []),
        },
    }

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
            "previous_daily": {
                "labels": [d.get("date") for d in previous.get("daily", [])],
                "values": [d.get("received", 0) for d in previous.get("daily", [])],
            },
        },
        "conversion": {
            "leads": conv_total,
            "leads_previous": prev_total if prev_start else 0,
            "citas": conv_citas,
            "citas_previous": prev_citas if prev_start else 0,
            "conversion_pct": conv_pct,
            "previous_pct": prev_pct if prev_start else None,
            "diff_pp": diff_pp,
            "ratio_leads_per_cita": ratio,
        },
        "pipeline": {
            "monto_uf": monto_uf,
            "pct_comision": comision_pct,
            "comision_estimada_uf": comision_uf,
            "comision_venta_uf": comision_venta_uf,
            "comision_arriendo_uf": comision_arriendo_uf,
            "comision_policy": "4% venta \u00b7 50% arrendatario + 50% arrendador (1er mes arriendo)",
            "pct_venta": pct_venta,
            "pct_arriendo": pct_arriendo,
            "pct_otro": pct_otro,
            "monto_venta_uf": venta_uf,
            "monto_arriendo_uf": arriendo_uf,
            "monto_otro_uf": otro_uf,
            "propiedades_vinculadas": pipeline.get("propiedades_vinculadas", 0),
            "leads_vinculados": pipeline.get("leads_vinculados", 0),
            "pct_cobertura": cobertura,
            "fecha_uf": uf_asof,
            "valor_uf_clp": uf_value,
            "monto_clp": round(monto_uf * uf_value, 0) if uf_value else None,
            "comision_clp": round(comision_uf * uf_value, 0) if uf_value else None,
            "suma_componentes_uf": round(venta_uf + arriendo_uf + otro_uf, 1),
            "diferencia_redondeo_uf": round(monto_uf - (venta_uf + arriendo_uf + otro_uf), 1),
            "pct_conciliacion": round((venta_uf + arriendo_uf + otro_uf) / monto_uf * 100, 1) if monto_uf else 100.0,
        },
        "funnel": funnel,
        "sla": sla_data,
        "rescue": rescue_data,
        "sources": sources_data,
        "executives": executives_data,
        "meta": {
            "target": meta_target,
            "global_target": target_info.get("global_target") if target_info.get("available") else meta_target,
            "label": "Leads recibidos (meta prorrateada al período)",
            "days_in_period": (pe_dt - ps_dt).days + 1,
        },
    })
    _cache_set(key, result)
    return result
