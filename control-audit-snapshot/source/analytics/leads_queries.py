"""Read-only MongoDB queries for the Leads Analytics Dashboard."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict
import logging
import hashlib
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from typing import Any, Mapping, Optional

from pymongo.errors import NetworkTimeout
from bson import BSON

from chatbot.constants import CHILE_TZ
from chatbot.crm_metrics import (
    calculate_sla,
    coerce_utc_datetime,
    event_evidence,
    INSTRUMENTATION_CUTOVER,
    MANAGEMENT_ENFORCEMENT_CUTOVER,
    is_pre_visual_cutover,
    normalize_result,
    registered_outreach_evidence,
    resolve_hot_start_at,
    REGISTERED_OUTREACH_EVENT_TYPES,
    VALID_MANAGEMENT_EVENT_TYPES,
)
from chatbot.utils import calculate_business_minutes
from chatbot.storage import get_db
from chatbot.mongo_identity import mongo_id_variants
from chatbot.lead_temperature import (
    COMMERCIAL_ALERT_TYPES,
    HOT_INTENTS,
    HOT_STAGES,
)
from .monthly_control import (
    COVERED_MONTHS, _exclusion_category, _is_control_non_broker, build_month_metrics,
    unavailable_month_payload,
)
from .pricing_intelligence.property_identity import (
    LinkageStatus, PropertyIdentityResolver, normalize_identifier, _iter_master_aliases,
)
from .pricing_intelligence.snapshot_builder import get_latest_observable_price_change
from config import Config

logger = logging.getLogger(__name__)

_CONTROL_PORTFOLIO_EPISODE_CACHE: dict[tuple[str, str, str], tuple[float, tuple]] = {}
_CONTROL_PORTFOLIO_EPISODE_CACHE_LOCK = Lock()

_DEMAND_CAPTURE_BASE_TTL = 120
_DEMAND_CAPTURE_BASE_CACHE = None
_DEMAND_CAPTURE_BASE_LOCK = Lock()

ACTIVE_STAGES = ["ARCHIVED", "CLOSED_WON", "CLOSED_LOST"]
UNASSIGNED_VALUES = ["Sin Asignar", "No Asignado", None, ""]
OPS_PORTFOLIO_OFFICE = "PROCASA SUCRE"

# These are contracts for interpretation, not filters over the data.  A
# comparison is eligible only when the whole comparison window is covered by
# the metric's reliable instrumentation period.
OPS_COMPARABLE_METRIC_RULES = {
    "assigned": {
        "source": "lifecycle.assigned_at / crm_assignment_cycles",
        "from": None,
        "reason": "La asignación tiene timestamp de cohorte histórico independiente de la instrumentación de actividad/resultado.",
    },
    "activity_attempts": {
        "source": "crm_events: REGISTERED_OUTREACH_EVENT_TYPES",
        "from": INSTRUMENTATION_CUTOVER,
        "reason": "La instrumentación de outreach se considera consistente desde INSTRUMENTATION_CUTOVER.",
    },
    "managed": {
        "source": "lifecycle.first_valid_management_at + resultado válido",
        "from": MANAGEMENT_ENFORCEMENT_CUTOVER,
        "reason": "El contrato de resultado válido/SLA se aplica desde MANAGEMENT_ENFORCEMENT_CUTOVER.",
    },
    "coverage": {
        "source": "resultado registrado / asignados",
        "from": MANAGEMENT_ENFORCEMENT_CUTOVER,
        "reason": "La cobertura depende del resultado registrado bajo el contrato vigente.",
    },
    "contact_effective": {
        "source": "lifecycle.first_effective_contact_at",
        "from": MANAGEMENT_ENFORCEMENT_CUTOVER,
        "reason": "Se compara junto con el episodio y contrato de resultados vigente.",
    },
    "hot_sla_pct": {
        "source": "first_valid_management_at + calculate_business_minutes",
        "from": MANAGEMENT_ENFORCEMENT_CUTOVER,
        "reason": "La evaluación HOT homogénea depende del enforcement de primera gestión válida.",
    },
    "normal_sla_pct": {
        "source": "first_valid_management_at + calculate_business_minutes",
        "from": MANAGEMENT_ENFORCEMENT_CUTOVER,
        "reason": "La evaluación NORMAL homogénea depende del enforcement de primera gestión válida.",
    },
    "visits_scheduled": {
        "source": "stage_history / lifecycle.visit_scheduled_at / crm_events resultado VISITA_AGENDADA",
        "from": INSTRUMENTATION_CUTOVER,
        "reason": "La evidencia canónica de visita se audita con cobertura de instrumentación CRM.",
    },
    "lead_to_visit": {
        "source": "visitas agendadas / asignados",
        "from": INSTRUMENTATION_CUTOVER,
        "reason": "La tasa hereda la elegibilidad de visitas y de la cohorte asignada.",
    },
}


def _ops_comparable_eligibility(start: Optional[datetime], end: Optional[datetime]) -> dict:
    """Return metric-level comparability for a complete [start, end) window."""
    result = {}
    for metric, rule in OPS_COMPARABLE_METRIC_RULES.items():
        cutoff = coerce_utc_datetime(rule["from"]) if rule.get("from") else None
        valid = bool(start and end and (cutoff is None or start >= cutoff))
        result[metric] = {
            "valid": valid,
            "from": cutoff.isoformat() if cutoff else None,
            "source": rule["source"],
            "reason": rule["reason"] if valid else "La ventana comparable completa es anterior al inicio de cobertura metodológica de esta métrica.",
        }
    return result


def _effective_stage_expr() -> dict:
    """MongoDB $switch que resuelve la etapa efectiva."""
    return {
        "$switch": {
            "branches": [
                {
                    "case": {
                        "$and": [
                            {"$ne": ["$pipeline_stage", None]},
                            {"$ne": ["$pipeline_stage", ""]},
                        ]
                    },
                    "then": "$pipeline_stage",
                }
            ],
            "default": {"$ifNull": ["$stage", "SIN_ETAPA"]},
        }
    }


def _build_chile_period_bounds(period_start: str, period_end: str) -> tuple[datetime, datetime]:
    """Convierte fechas America/Santiago a intervalo semiabierto UTC."""
    from datetime import timedelta as td

    try:
        ps = datetime.fromisoformat(period_start)
    except (ValueError, TypeError):
        ps = datetime.now(CHILE_TZ) - td(days=30)
    try:
        pe = datetime.fromisoformat(period_end)
    except (ValueError, TypeError):
        pe = datetime.now(CHILE_TZ)

    start_local = CHILE_TZ.localize(datetime(ps.year, ps.month, ps.day, 0, 0, 0))
    next_day = pe.date() + td(days=1)
    end_local = CHILE_TZ.localize(datetime(next_day.year, next_day.month, next_day.day, 0, 0, 0))
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _normalized_created_at_stage() -> dict:
    """Pipeline stage que agrega _created_normalized mediante conversión segura."""
    return {
        "$set": {
            "_created_normalized": {
                "$convert": {
                    "input": "$created_at",
                    "to": "date",
                    "onError": None,
                    "onNull": None,
                }
            }
        }
    }


def _cohort_indexed_prefilter(start_utc, end_utc) -> dict:
    """Pre-filtro sobre created_at (string ISO UTC, indexado) para usar el índice.

    created_at se almacena como string ISO con 'Z'; las strings ISO ordenan
    lexicográficamente igual que cronológicamente, así que un rango de strings
    aproxima el rango de fechas y deja que Mongo use el índice existente.
    El match autoritativo posterior sobre _created_normalized mantiene la
    exactitud.
    """
    start_str = start_utc.strftime("%Y-%m-%dT%H:%M:%S")
    end_str = end_utc.strftime("%Y-%m-%dT%H:%M:%S")
    return {"$match": {"created_at": {"$gte": start_str, "$lt": end_str}}}


def _format_date_field(field_expr: str, fmt: str = "%Y-%m-%d", timezone: Optional[str] = None) -> dict:
    """$dateToString con timezone opcional.

    Por defecto sin timezone (UTC). ``query_comparative_trends`` pasa
    ``timezone="America/Santiago"`` para que el bucket diario corresponda al día
    Chile y no se desfase ~4h (ni se pierdan leads del último día en el borde).
    """
    spec = {"format": fmt, "date": field_expr}
    if timezone:
        spec["timezone"] = timezone
    return {"$dateToString": spec}


def build_active_filter() -> dict:
    """Filtro para el universo activo operacional."""
    effective_stage = _effective_stage_expr()
    return {
        "$expr": {
            "$not": [{"$in": [effective_stage, ACTIVE_STAGES]}]
        }
    }


def query_summary(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    executive: Optional[str] = None,
    filters: Optional[dict] = None,
) -> dict:
    """Resumen de KPIs (stock + flujo + calidad)."""
    db = get_db()
    active = build_active_filter()
    user_filter = _build_user_filter(executive)
    extra = _build_extra_filter(filters)

    base_match = {"$and": [active, user_filter, extra]} if user_filter or extra else active

    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)

    pipeline = [
        _normalized_created_at_stage(),
        {
            "$facet": {
                "stock": [
                    {"$match": base_match},
                    {
                        "$group": {
                            "_id": None,
                            "total": {"$sum": 1},
                            "hot": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$lead_temperature_effective", "HOT"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "cold": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$lead_temperature_effective", "COLD"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "assigned": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$not": [
                                                        {
                                                            "$in": [
                                                                "$ejecutivo_asignado",
                                                                UNASSIGNED_VALUES,
                                                            ]
                                                        }
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        {"$type": "$ejecutivo_asignado"},
                                                        "missing",
                                                    ]
                                                },
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "unassigned": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$or": [
                                                {"$in": ["$ejecutivo_asignado", UNASSIGNED_VALUES]},
                                                {"$eq": [{"$type": "$ejecutivo_asignado"}, "missing"]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "by_stage": [
                    {"$match": base_match},
                    {
                        "$addFields": {
                            "_effective": _effective_stage_expr(),
                        }
                    },
                    {
                        "$group": {
                            "_id": "$_effective",
                            "count": {"$sum": 1},
                        }
                    },
                    {"$sort": {"count": -1}},
                ],
                "by_executive": [
                    {"$match": base_match},
                    {
                        "$group": {
                            "_id": {
                                "$ifNull": ["$ejecutivo_asignado", "Sin Asignar"],
                            },
                            "count": {"$sum": 1},
                        }
                    },
                    {"$sort": {"count": -1}},
                ],
                "closed_won": [
                    {"$match": {"pipeline_stage": "CLOSED_WON"}},
                    {"$count": "count"},
                ],
                "received": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$gte": ["$_created_normalized", start_utc]},
                                    {"$lt": ["$_created_normalized", end_utc]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "created_not_convertible": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$_created_normalized", None]},
                                    {"$ne": ["$created_at", None]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "stage_archived_conflict": [
                    {
                        "$match": {
                            "pipeline_stage": {"$ne": "ARCHIVED"},
                            "stage": "ARCHIVED",
                        }
                    },
                    {"$count": "count"},
                ],
                "no_pipeline_stage": [
                    {
                        "$match": {
                            "$or": [
                                {"pipeline_stage": None},
                                {"pipeline_stage": ""},
                                {"pipeline_stage": {"$exists": False}},
                            ]
                        }
                    },
                    {"$count": "count"},
                ],
            }
        },
    ]

    result = list(db["leads"].aggregate(pipeline))[0]

    stock_raw = result.get("stock", [{"total": 0, "hot": 0, "cold": 0, "assigned": 0, "unassigned": 0}])
    stock = stock_raw[0] if stock_raw else {"total": 0, "hot": 0, "cold": 0, "assigned": 0, "unassigned": 0}

    received = result.get("received", [{"count": 0}])
    closed_won = result.get("closed_won", [{"count": 0}])
    not_conv = result.get("created_not_convertible", [{"count": 0}])
    conflicts = result.get("stage_archived_conflict", [{"count": 0}])
    no_ps = result.get("no_pipeline_stage", [{"count": 0}])

    return {
        "stock": {
            "total_active": stock.get("total", 0),
            "hot": stock.get("hot", 0),
            "cold": stock.get("cold", 0),
            "assigned": stock.get("assigned", 0),
            "unassigned": stock.get("unassigned", 0),
        },
        "by_stage": [
            {"stage": r["_id"], "count": r["count"]}
            for r in result.get("by_stage", [])
        ],
        "by_executive": [
            {"executive": r["_id"], "count": r["count"]}
            for r in result.get("by_executive", [])
        ],
        "closed_won_current": closed_won[0]["count"] if closed_won else 0,
        "flow": {
            "received_in_period": received[0]["count"] if received else 0,
        },
        "quality": {
            "created_not_convertible": not_conv[0]["count"] if not_conv else 0,
            "stage_archived_pipeline_conflict": conflicts[0]["count"] if conflicts else 0,
            "no_pipeline_stage": no_ps[0]["count"] if no_ps else 0,
        },
    }


def query_trends(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
) -> dict:
    """Tendencia diaria de leads recibidos en el periodo."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)

    pipeline = [
        _normalized_created_at_stage(),
        {
            "$match": {
                "$expr": {
                    "$and": [
                        {"$gte": ["$_created_normalized", start_utc]},
                        {"$lt": ["$_created_normalized", end_utc]},
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": _format_date_field("$_created_normalized"),
                "received": {"$sum": 1},
            }
        },
        {"$sort": {"_id": 1}},
        {"$project": {"date": "$_id", "received": 1, "_id": 0}},
        {"$sort": {"date": 1}},
    ]

    raw = list(db["leads"].aggregate(pipeline))
    return {"daily": raw, "available": True}


def query_distributions(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    executive: Optional[str] = None,
    universe: str = "current_active",
    filters: Optional[dict] = None,
) -> dict:
    """Distribuciones por origen, operación, tipo, comuna."""
    db = get_db()

    if universe == "received_in_period":
        start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
        base_match = {
            "$expr": {
                "$and": [
                    {"$gte": ["$_created_normalized", start_utc]},
                    {"$lt": ["$_created_normalized", end_utc]},
                ]
            }
        }
    else:
        active = build_active_filter()
        user_filter = _build_user_filter(executive)
        extra = _build_extra_filter(filters)
        match_parts = [active]
        if user_filter:
            match_parts.append(user_filter)
        if extra:
            match_parts.append(extra)
        base_match = {"$and": match_parts} if len(match_parts) > 1 else active

    def _group_and_collect(field_path: str, null_label: str = "Sin informacion") -> list:
        pipeline = [
            _normalized_created_at_stage(),
            {"$match": base_match},
            {
                "$group": {
                    "_id": {
                        "$ifNull": [field_path, null_label],
                    },
                    "count": {"$sum": 1},
                }
            },
            {"$sort": {"count": -1}},
            {
                "$project": {
                    "value": "$_id",
                    "count": 1,
                    "_id": 0,
                }
            },
        ]
        return list(db["leads"].aggregate(pipeline))

    return {
        "sources": _group_and_collect("$prospecto.origen"),
        "operations": _group_and_collect("$prospecto.operacion"),
        "types": _group_and_collect("$prospecto.tipo"),
        "communes": _group_and_collect("$prospecto.comuna"),
    }


def query_table(
    page: int = 1,
    limit: int = 50,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    executive: Optional[str] = None,
    filters: Optional[dict] = None,
    search: Optional[str] = None,
    universe: str = "current_active",
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
) -> dict:
    """Tabla paginada de leads."""
    db = get_db()
    limit = min(max(limit, 1), 100)
    page = max(page, 1)
    offset = (page - 1) * limit

    SORT_WHITELIST = {
        "created_at": "_created_normalized",
        "phone": "phone",
        "prospecto.nombre": "prospecto.nombre",
        "prospecto.origen": "prospecto.origen",
        "pipeline_stage": "pipeline_stage",
        "ejecutivo_asignado": "ejecutivo_asignado",
        "lead_temperature_effective": "lead_temperature_effective",
    }

    sort_field = SORT_WHITELIST.get(sort_by, "_created_normalized")
    sort_direction = -1 if str(sort_dir or "desc").lower() == "desc" else 1

    match_conditions = []

    if universe == "received_in_period":
        start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
        match_conditions.append(
            {"$expr": {"$and": [{"$gte": ["$_created_normalized", start_utc]}, {"$lt": ["$_created_normalized", end_utc]}]}}
        )
    else:
        match_conditions.append(build_active_filter())

    user_filter = _build_user_filter(executive)
    if user_filter:
        match_conditions.append(user_filter)

    extra = _build_extra_filter(filters)
    if extra:
        match_conditions.append(extra)

    if search and str(search).strip():
        term = str(search).strip()[:60]
        safe_term = term.replace("$", "").replace(".", "\\.")
        digits = "".join(ch for ch in safe_term if ch.isdigit())
        search_conditions = []
        if digits and len(digits) >= 7:
            from re import compile as re_compile
            search_conditions.append({"phone": re_compile(digits)})
        search_conditions.append(
            {"prospecto.nombre": {"$regex": safe_term, "$options": "i"}}
        )
        match_conditions.append({"$or": search_conditions})

    match_stage = {"$match": {"$and": match_conditions}} if match_conditions else {}

    pipeline = [
        _normalized_created_at_stage(),
        match_stage,
        {
            "$facet": {
                "total": [{"$count": "count"}],
                "items": [
                    {"$sort": {sort_field: sort_direction, "_id": 1}},
                    {"$skip": offset},
                    {"$limit": limit},
                    {
                        "$project": {
                            "id": {"$toString": "$_id"},
                            "nombre": {"$ifNull": ["$prospecto.nombre", ""]},
                            "phone": 1,
                            "origen": {"$ifNull": ["$prospecto.origen", "Sin informacion"]},
                            "etapa": {"$ifNull": ["$pipeline_stage", None]},
                            "ejecutivo": {
                                "$ifNull": [
                                    "$ejecutivo_asignado",
                                    "Sin Asignar",
                                ]
                            },
                            "temperatura": "$lead_temperature_effective",
                            "fecha_creacion": _format_date_field("$_created_normalized"),
                            "dias_desde_creacion": {
                                "$dateDiff": {
                                    "startDate": "$_created_normalized",
                                    "endDate": datetime.now(timezone.utc),
                                    "unit": "day",
                                }
                            },
                            "comuna": {"$ifNull": ["$prospecto.comuna", ""]},
                            "operacion": {"$ifNull": ["$prospecto.operacion", ""]},
                            "tipo_propiedad": {"$ifNull": ["$prospecto.tipo", ""]},
                            "_id": 0,
                        }
                    },
                ],
            }
        },
    ]

    result = list(db["leads"].aggregate(pipeline))[0]
    total_docs = result.get("total", [{"count": 0}])
    total = total_docs[0]["count"] if total_docs else 0
    items = result.get("items", [])

    return {"total": total, "page": page, "limit": limit, "items": items}


def query_detail(lead_id: str) -> dict | None:
    """Detalle de un lead por su _id."""
    from bson import ObjectId

    try:
        oid = ObjectId(lead_id)
    except Exception:
        return None

    db = get_db()
    lead = db["leads"].find_one(
        {"_id": oid},
        {
            "phone": 1,
            "created_at": 1,
            "prospecto.nombre": 1,
            "prospecto.email": 1,
            "prospecto.rut": 1,
            "prospecto.origen": 1,
            "prospecto.operacion": 1,
            "prospecto.tipo": 1,
            "prospecto.comuna": 1,
            "prospecto.codigo": 1,
            "prospecto.precio_uf": 1,
            "cartera_data.precio_uf": 1,
            "pipeline_stage": 1,
            "stage": 1,
            "ejecutivo_asignado": 1,
            "lead_temperature_effective": 1,
            "lifecycle.assigned_at": 1,
            "lifecycle.first_valid_management_at": 1,
            "bi_analytics_global.RESULTADO_CHAT": 1,
            "bi_analytics_global.RECUPERABILIDAD": 1,
            "stage_history": 1,
            "created_at": 1,
            "messages": {"$slice": -10},
        },
    )
    if not lead:
        return None

    return {
        "id": str(lead["_id"]),
        "public": {
            "nombre": (lead.get("prospecto", {}) or {}).get("nombre", ""),
            "phone": lead.get("phone"),
            "origen": (lead.get("prospecto", {}) or {}).get("origen", "Sin informacion"),
            "etapa": lead.get("pipeline_stage"),
            "ejecutivo": lead.get("ejecutivo_asignado"),
            "temperatura": lead.get("lead_temperature_effective"),
            "propiedad": {
                "codigo": (lead.get("prospecto", {}) or {}).get("codigo"),
                "comuna": (lead.get("prospecto", {}) or {}).get("comuna"),
                "tipo": (lead.get("prospecto", {}) or {}).get("tipo"),
                "operacion": (lead.get("prospecto", {}) or {}).get("operacion"),
                "precio_uf": (lead.get("prospecto", {}) or {}).get("precio_uf") or (lead.get("cartera_data", {}) or {}).get("precio_uf"),
            },
        },
        "management": {
            "managed": {
                "value": None,
                "available": False,
                "reason": "management_history_not_available",
            }
        },
        "classification": {
            "resultado_chat": lead.get("bi_analytics_global", {}).get("RESULTADO_CHAT"),
            "recuperabilidad": lead.get("bi_analytics_global", {}).get("RECUPERABILIDAD"),
        },
        "timeline": _build_timeline(lead),
        "meta": {
            "data_quality": {"management_history": "not_available"}
        },
    }


def query_filters(
    executive: Optional[str] = None,
) -> dict:
    """Opciones de filtros disponibles."""
    db = get_db()
    active = build_active_filter()
    user_filter = _build_user_filter(executive)
    match = {"$and": [active, user_filter]} if user_filter else active

    def _distinct_values(field: str) -> list:
        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": field,
                    "count": {"$sum": 1},
                }
            },
            {"$sort": {"count": -1}},
            {
                "$project": {
                    "value": "$_id",
                    "label": "$_id",
                    "count": 1,
                    "_id": 0,
                }
            },
        ]
        return list(db["leads"].aggregate(pipeline))

    return {
        "executives": _distinct_values("$ejecutivo_asignado"),
        "sources": _distinct_values("$prospecto.origen"),
        "stages": _distinct_values("$pipeline_stage"),
        "temperatures": _distinct_values("$lead_temperature_effective"),
    }


def query_commercial_filter_options() -> dict:
    """Complete filter options for commercial dashboard selectors.
    Returns options for: offices, executives, sources, operations, property_types,
    communes, properties, temperatures, assignment states.
    Reads from active leads universe. No sensitive data exposed.
    """
    db = get_db()
    active = build_active_filter()

    def _values_from_field(field_expr: str, null_label: str = "Sin informacion") -> list:
        pipeline = [
            {"$match": active},
            {"$addFields": {"_val": field_expr}},
            {"$group": {"_id": {"$ifNull": ["$_val", null_label]}, "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$project": {"value": "$_id", "label": "$_id", "count": 1, "_id": 0}},
        ]
        return list(db["leads"].aggregate(pipeline))

    def _executives_from_active() -> list:
        pipeline = [
            {"$match": active},
            {"$addFields": {"_ex": {"$ifNull": ["$ejecutivo_asignado", "Sin Asignar"]}}},
            {"$group": {"_id": "$_ex", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$project": {"value": "$_id", "label": "$_id", "count": 1, "_id": 0}},
        ]
        return list(db["leads"].aggregate(pipeline))

    def _properties_from_active() -> list:
        pipeline = [
            {"$match": active},
            {"$addFields": {"_code": {"$ifNull": ["$prospecto.codigo", ""]}}},
            {"$match": {"_code": {"$ne": ""}}},
            {"$group": {"_id": "$_code", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$project": {"value": "$_id", "label": "$_id", "count": 1, "_id": 0}},
        ]
        return list(db["leads"].aggregate(pipeline))

    return {
        "executives": _executives_from_active(),
        "sources": _values_from_field({"$ifNull": ["$prospecto.origen", "Sin informacion"]}),
        "operations": _values_from_field({"$ifNull": ["$prospecto.operacion", "Sin informacion"]}),
        "property_types": _values_from_field({"$ifNull": ["$prospecto.tipo", "Sin informacion"]}),
        "communes": _values_from_field({"$ifNull": ["$prospecto.comuna", "Sin informacion"]}),
        "properties": _properties_from_active(),
        "stages": _values_from_field({"$ifNull": ["$pipeline_stage", "Sin etapa"]}),
        "temperatures": _values_from_field({"$ifNull": ["$lead_temperature_effective", "Sin temp"]}),
        "assignment_states": [
            {"value": "", "label": "Todos", "count": 0},
            {"value": "assigned", "label": "Asignados", "count": 0},
            {"value": "unassigned", "label": "Sin asignar", "count": 0},
        ],
    }


def query_field_coverage(
    executive: Optional[str] = None,
    universe: str = "current_active",
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    filters: Optional[dict] = None,
) -> dict:
    """Cobertura de campos sobre el universo seleccionado."""
    db = get_db()
    match_parts = []
    extra = _build_extra_filter(filters) or {}
    if period_start and period_end:
        start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
        match_parts.append(_build_commercial_cohort_match(start_utc, end_utc, filters))
    elif universe != "received_in_period":
        match_parts.append(build_active_filter())
    user_filter = _build_user_filter(executive)
    if user_filter:
        match_parts.append(user_filter)
    if extra and not (period_start and period_end):
        match_parts.append(extra)
    match = {"$and": match_parts} if match_parts else {}

    fields = [
        ("prospecto.nombre", "$prospecto.nombre"),
        ("prospecto.email", "$prospecto.email"),
        ("prospecto.rut", "$prospecto.rut"),
        ("prospecto.origen", "$prospecto.origen"),
        ("prospecto.operacion", "$prospecto.operacion"),
        ("prospecto.tipo", "$prospecto.tipo"),
        ("prospecto.comuna", "$prospecto.comuna"),
        ("prospecto.codigo", "$prospecto.codigo"),
        ("ejecutivo_asignado", "$ejecutivo_asignado"),
        ("pipeline_stage", "$pipeline_stage"),
        ("lead_temperature_effective", "$lead_temperature_effective"),
    ]

    result = {}
    for name, expr in fields:
        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": 1},
                    "populated": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": [expr, None]},
                                        {"$ne": [expr, ""]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
        ]
        agg = list(db["leads"].aggregate(pipeline))
        if agg:
            r = agg[0]
            total = r.get("total", 0)
            populated = r.get("populated", 0)
            result[name] = {
                "field": name,
                "total": total,
                "populated": populated,
                "coverage_pct": round(populated / total * 100, 2) if total else 0,
            }

    # created_at convertible
    pipeline_created = [
        _normalized_created_at_stage(),
        {"$match": match},
        {
            "$group": {
                "_id": None,
                "total": {"$sum": 1},
                "convertible": {
                    "$sum": {
                        "$cond": [{"$ne": ["$_created_normalized", None]}, 1, 0]
                    }
                },
            }
        },
    ]
    agg_cr = list(db["leads"].aggregate(pipeline_created))
    if agg_cr:
        r = agg_cr[0]
        result["created_at"] = {
            "field": "created_at",
            "total": r["total"],
            "populated": r["convertible"],
            "coverage_pct": round(r["convertible"] / r["total"] * 100, 2) if r["total"] else 0,
        }

    pipeline_conflict = [
        {"$match": match},
        {
            "$group": {
                "_id": None,
                "total": {"$sum": 1},
                "conflict": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$pipeline_stage", "ARCHIVED"]},
                                    {"$eq": ["$stage", "ARCHIVED"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]
    agg_conf = list(db["leads"].aggregate(pipeline_conflict))
    if agg_conf:
        r = agg_conf[0]
        result["stage_conflict"] = {
            "field": "pipeline_stage vs stage",
            "total": r["total"],
            "conflict_count": r["conflict"],
            "coverage_pct": round(
                (1 - r["conflict"] / r["total"]) * 100, 2
            ) if r["total"] else 0,
        }

    return result


def _build_user_filter(executive: Optional[str]) -> dict:
    """Filtro por ejecutivo (para admin/supervisor) o vacío."""
    if executive and str(executive).strip():
        return {
            "ejecutivo_asignado": str(executive).strip(),
        }
    return {}


def _build_extra_filter(filters: Optional[dict]) -> dict:
    """Filtros adicionales: stage, temperature, source, operation, type, commune, code, assignment, executive."""
    conditions = {}
    if not filters:
        return conditions
    if filters.get("stage"):
        conditions["pipeline_stage"] = str(filters["stage"])
    if filters.get("temperature"):
        conditions["lead_temperature_effective"] = str(filters["temperature"])
    if filters.get("source"):
        conditions["prospecto.origen"] = str(filters["source"])
    if filters.get("operation"):
        conditions["prospecto.operacion"] = str(filters["operation"])
    if filters.get("property_type"):
        conditions["prospecto.tipo"] = str(filters["property_type"])
    if filters.get("commune"):
        conditions["prospecto.comuna"] = str(filters["commune"])
    if filters.get("property_code"):
        conditions["prospecto.codigo"] = str(filters["property_code"])
    executive_value = filters.get("ejecutivo_asignado") or filters.get("executive")
    if executive_value:
        conditions["ejecutivo_asignado"] = str(executive_value)
    if filters.get("assignment") == "1":
        assignment_condition = {"$nin": ["Sin Asignar", "No Asignado", None, ""]}
        if "ejecutivo_asignado" in conditions:
            return {"$and": [
                {"ejecutivo_asignado": conditions.pop("ejecutivo_asignado")},
                {"ejecutivo_asignado": assignment_condition},
                *({key: value} for key, value in conditions.items()),
            ]}
        conditions["ejecutivo_asignado"] = assignment_condition
    elif filters.get("assignment") == "0":
        assignment_condition = {"$in": ["Sin Asignar", "No Asignado", None, ""]}
        if "ejecutivo_asignado" in conditions:
            return {"$and": [
                {"ejecutivo_asignado": conditions.pop("ejecutivo_asignado")},
                {"ejecutivo_asignado": assignment_condition},
                *({key: value} for key, value in conditions.items()),
            ]}
        conditions["ejecutivo_asignado"] = assignment_condition
    return conditions


def _build_commercial_cohort_match(start_utc, end_utc, filters: Optional[dict] = None) -> dict:
    """Canonical commercial cohort: created_at period plus all segment filters."""
    parts = [{"$expr": {"$and": [
        {"$gte": ["$_created_normalized", start_utc]},
        {"$lt": ["$_created_normalized", end_utc]},
    ]}}]
    extra = _build_extra_filter(filters)
    if extra:
        parts.append(extra)
    return {"$and": parts}


def _build_timeline(lead: dict) -> list:
    """Construye timeline con hechos demostrables."""
    timeline = []

    if lead.get("created_at"):
        timeline.append({
            "timestamp": _safe_timestamp(lead["created_at"]),
            "tipo": "created",
            "label": "Lead creado",
            "actor": "system",
        })

    lc = lead.get("lifecycle") or {}
    if lc.get("assigned_at"):
        timeline.append({
            "timestamp": _safe_timestamp(lc["assigned_at"]),
            "tipo": "assignment",
            "label": f"Asignado a {lead.get('ejecutivo_asignado', '?' )}",
            "actor": "system",
        })

    sh = lead.get("stage_history") or []
    for entry in sh[-10:]:
        timeline.append({
            "timestamp": _safe_timestamp(entry.get("timestamp")),
            "tipo": "stage_change",
            "label": f"{entry.get('from', '?')} → {entry.get('to', '?')}",
            "actor": entry.get("actor", "system"),
            "notes": entry.get("notes", ""),
        })

    timeline.sort(key=lambda e: str(e.get("timestamp", "")))
    return timeline


ADVANCED_STAGES = ["CONTACTED", "INTERESTED", "VISIT_SCHEDULED", "VISIT_DONE", "OFFER", "NEGOTIATION", "CLOSED_WON", "CLOSED_LOST"]


def query_funnel(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    executive: Optional[str] = None,
) -> dict:
    """Funnel de cohorte: leads recibidos en el periodo."""
    from datetime import timezone as tz
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    user_filter = _build_user_filter(executive)

    eff = _effective_stage_expr()
    now_u = datetime.now(timezone.utc)

    pipeline = [
        _normalized_created_at_stage(),
        {"$match": {"$expr": {"$and": [{"$gte": ["$_created_normalized", start_utc]}, {"$lt": ["$_created_normalized", end_utc]}]}, **({} if not user_filter else {"$and": [user_filter]} ) }},
    ]

    def cohort_count(extra_cond: Optional[dict] = None) -> int:
        p = [
            _normalized_created_at_stage(),
            {"$match": {"$expr": {"$and": [{"$gte": ["$_created_normalized", start_utc]}, {"$lt": ["$_created_normalized", end_utc]}]}}},
        ]
        if user_filter:
            p.append({"$match": user_filter})
        if extra_cond:
            p.append({"$match": extra_cond})
        p.append({"$count": "c"})
        r = list(db["leads"].aggregate(p))
        return r[0]["c"] if r else 0

    received = cohort_count()
    assigned = cohort_count({"ejecutivo_asignado": {"$nin": UNASSIGNED_VALUES}})
    advanced = cohort_count({"$expr": {"$in": [eff, ADVANCED_STAGES]}})
    won = cohort_count({"$expr": {"$eq": [eff, "CLOSED_WON"]}})

    funnel = [
        {"stage": "received", "label": "Recibidos", "count": received, "pct_of_cohort": 100.0},
        {"stage": "assigned", "label": "Asignados actualmente", "count": assigned, "pct_of_cohort": round(assigned / received * 100, 1) if received else 0},
        {"stage": "advanced", "label": "Avanzados actualmente", "count": advanced, "pct_of_cohort": round(advanced / received * 100, 1) if received else 0},
        {"stage": "won", "label": "Cerrados ganados actualmente", "count": won, "pct_of_cohort": round(won / received * 100, 1) if received else 0},
    ]

    return funnel


def query_management_metrics(
    executive: Optional[str] = None,
) -> dict:
    """Metricas de primera respuesta con cobertura."""
    db = get_db()
    user_filter = _build_user_filter(executive)
    active = build_active_filter()

    assigned_match = {"ejecutivo_asignado": {"$nin": UNASSIGNED_VALUES}}
    fvma_match = {"lifecycle.first_valid_management_at": {"$ne": None, "$exists": True}}
    assigned_at_match = {"lifecycle.assigned_at": {"$ne": None, "$exists": True}}
    sla_statuses = [None, False]

    total_assigned = db["leads"].count_documents({"$and": [active, user_filter, assigned_match]} if user_filter else {"$and": [active, assigned_match]}) if user_filter else db["leads"].count_documents({"$and": [active, assigned_match]})
    total_with_both = db["leads"].count_documents({"$and": [active, user_filter, assigned_match, fvma_match, assigned_at_match]} if user_filter else {"$and": [active, assigned_match, fvma_match, assigned_at_match]})

    coverage_pct = round(total_with_both / total_assigned * 100, 1) if total_assigned else 0
    sample_sufficient = total_with_both >= 20 and coverage_pct >= 70.0

    if not sample_sufficient:
        return {
            "total_assigned": total_assigned,
            "total_with_evidence": total_with_both,
            "coverage_pct": coverage_pct,
            "sample_sufficient": False,
            "median_minutes": None,
            "p90_minutes": None,
            "before_threshold_pct": None,
            "distribution": [],
            "threshold_minutes": 180,
        }

    minutes_pipeline = [
        {"$match": {"$and": [active, assigned_match, fvma_match, assigned_at_match]}},
        {"$addFields": {
            "_assigned_dt": {"$convert": {"input": "$lifecycle.assigned_at", "to": "date", "onError": None, "onNull": None}},
            "_mgmt_dt": {"$convert": {"input": "$lifecycle.first_valid_management_at", "to": "date", "onError": None, "onNull": None}},
        }},
        {"$match": {"$expr": {"$and": [{"$ne": ["$_assigned_dt", None]}, {"$ne": ["$_mgmt_dt", None]}]}}},
        {"$addFields": {
            "_business_minutes": {
                "$function": {
                    "body": "function(start,end){if(!start||!end)return null;let m=0;let d=new Date(start);const ed=new Date(end);while(d<=ed){const dw=d.getDay();if(dw!==5&&dw!==6){const bs=new Date(d);bs.setHours(9,0,0,0);const be=new Date(d);be.setHours(19,0,0,0);const ps=new Date(Math.max(d.getTime(),bs.getTime()));const pe=new Date(Math.min(ed.getTime(),be.getTime()));if(ps<pe)m+=(pe-ps)/60000;}d.setDate(d.getDate()+1);d.setHours(0,0,0,0);}return m;}",
                    "args": ["$_assigned_dt", "$_mgmt_dt"],
                    "lang": "js",
                }
            }
        }},
        {"$group": {
            "_id": None,
            "minutes": {"$push": "$_business_minutes"},
            "before_threshold": {"$sum": {"$cond": [{"$lt": ["$_business_minutes", 180]}, 1, 0]}},
            "total": {"$sum": 1},
        }},
    ]
    raw = list(db["leads"].aggregate(minutes_pipeline))
    if not raw:
        return {"total_assigned": total_assigned, "total_with_evidence": total_with_both, "coverage_pct": coverage_pct, "sample_sufficient": False, "median_minutes": None, "p90_minutes": None, "before_threshold_pct": None, "distribution": [], "threshold_minutes": 180}

    r = raw[0]
    mins = sorted(r.get("minutes", []))
    before_threshold_pct = round(r.get("before_threshold", 0) / r.get("total", 1) * 100, 1)

    def percentile(sorted_list, p):
        if not sorted_list:
            return None
        k = (len(sorted_list) - 1) * p / 100
        f = int(k)
        c = f + 1
        if c >= len(sorted_list):
            return sorted_list[f]
        return sorted_list[f] + (k - f) * (sorted_list[c] - sorted_list[f])

    dist = [
        {"tramo": "<30 min", "count": sum(1 for m in mins if m is not None and m < 30)},
        {"tramo": "30-60 min", "count": sum(1 for m in mins if m is not None and 30 <= m < 60)},
        {"tramo": "1-3 h", "count": sum(1 for m in mins if m is not None and 60 <= m < 180)},
        {"tramo": ">3 h", "count": sum(1 for m in mins if m is not None and m >= 180)},
    ]

    return {
        "total_assigned": total_assigned,
        "total_with_evidence": total_with_both,
        "coverage_pct": coverage_pct,
        "sample_sufficient": True,
        "median_minutes": round(percentile(mins, 50), 1) if mins else None,
        "p90_minutes": round(percentile(mins, 90), 1) if mins else None,
        "before_threshold_pct": before_threshold_pct,
        "distribution": dist,
        "threshold_minutes": 180,
    }


def query_property_ranking(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    executive: Optional[str] = None,
) -> dict:
    """Top 10 codigos de propiedad con mayor cantidad de leads en el periodo."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    user_filter = _build_user_filter(executive)

    pipeline = [
        _normalized_created_at_stage(),
        {"$match": {"$expr": {"$and": [{"$gte": ["$_created_normalized", start_utc]}, {"$lt": ["$_created_normalized", end_utc]}]}}},
        *([{"$match": user_filter}] if user_filter else []),
        {"$addFields": {
            "_code": {"$ifNull": ["$prospecto.codigo", None]},
            "_is_hot": {"$cond": [{"$eq": ["$lead_temperature_effective", "HOT"]}, 1, 0]},
            "_is_assigned": {"$cond": [{"$not": [{"$in": ["$ejecutivo_asignado", UNASSIGNED_VALUES]}]}, 1, 0]},
            "_is_advanced": {"$cond": [{"$in": [{"$ifNull": ["$pipeline_stage", ""]}, ADVANCED_STAGES]}, 1, 0]},
        }},
        {"$group": {
            "_id": "$_code",
            "count": {"$sum": 1},
            "hot": {"$sum": "$_is_hot"},
            "assigned": {"$sum": "$_is_assigned"},
            "advanced": {"$sum": "$_is_advanced"},
            "sources": {"$addToSet": "$prospecto.origen"},
        }},
        {"$sort": {"count": -1}},
        {"$limit": 11},
    ]

    raw = list(db["leads"].aggregate(pipeline))
    ranking = []
    no_code_count = 0

    for r in raw:
        code = r["_id"]
        if not code or str(code).strip() == "":
            no_code_count = r["count"]
            continue
        total = r["count"]
        srcs = [s for s in r.get("sources", []) if s]
        dominant = max(srcs, key=lambda x: str(x) if x else "") if srcs else "S/I"
        ranking.append({
            "code": code,
            "count": total,
            "hot_pct": round(r["hot"] / total * 100, 1) if total else 0,
            "assigned_pct": round(r["assigned"] / total * 100, 1) if total else 0,
            "advanced_pct": round(r["advanced"] / total * 100, 1) if total else 0,
            "dominant_source": dominant,
        })

    return {"ranking": ranking[:10], "no_code_count": no_code_count}


def query_executive_load_detail(
    executive: Optional[str] = None,
) -> list:
    """Carga actual por ejecutivo con metricas detalladas."""
    db = get_db()
    active = build_active_filter()
    user_filter = _build_user_filter(executive)
    match = {"$and": [active, user_filter]} if user_filter else active
    now_u = datetime.now(timezone.utc)

    pipeline = [
        _normalized_created_at_stage(),
        {"$match": match},
        {"$addFields": {
            "_ex": {"$ifNull": ["$ejecutivo_asignado", "Sin Asignar"]},
            "_eff": _effective_stage_expr(),
            "_is_hot": {"$cond": [{"$eq": ["$lead_temperature_effective", "HOT"]}, 1, 0]},
            "_is_pending": {"$cond": [{"$in": ["$_eff", ["NEW", None, ""]]}, 1, 0]},
            "_is_critical": {"$cond": [{"$eq": [{"$ifNull": ["$priority_bucket", ""]}, "CRITICAL"]}, 1, 0]},
            "_age_days": {"$dateDiff": {"startDate": "$_created_normalized", "endDate": now_u, "unit": "day"}},
        }},
        {"$group": {
            "_id": "$_ex",
            "active": {"$sum": 1},
            "hot": {"$sum": "$_is_hot"},
            "pending_7d": {"$sum": {"$cond": [{"$and": [{"$eq": ["$_is_pending", 1]}, {"$gte": ["$_age_days", 7]}]}, 1, 0]}},
            "critical": {"$sum": "$_is_critical"},
            "ages": {"$push": "$_age_days"},
        }},
        {"$sort": {"active": -1}},
    ]

    rows = list(db["leads"].aggregate(pipeline))
    result = []
    for r in rows:
        if r["_id"] in UNASSIGNED_VALUES:
            continue
        ages = sorted([a for a in r.get("ages", []) if a is not None])
        median = ages[len(ages) // 2] if ages else 0
        result.append({
            "executive": r["_id"],
            "active": r["active"],
            "hot": r["hot"],
            "pending_gt_7d": r["pending_7d"],
            "critical": r["critical"],
            "median_age_days": median,
        })
    return result


def query_source_performance(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    executive: Optional[str] = None,
    comparison_start: Optional[str] = None,
    comparison_end: Optional[str] = None,
    include_comparison: bool = True,
    filters: Optional[dict] = None,
) -> list:
    """Rendimiento de fuentes en el periodo: volumen, %hot, %asignados, %avanzados, variacion."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    if not include_comparison:
        prev_start = prev_end = None
    elif comparison_start and comparison_end:
        prev_start, prev_end = _build_chile_period_bounds(comparison_start, comparison_end)
    else:
        prev_end = start_utc
        prev_start = prev_end - (end_utc - start_utc)
    user_filter = _build_user_filter(executive)

    def _per_source(start_dt, end_dt):
        pipeline = [
            _normalized_created_at_stage(),
            {"$match": _build_commercial_cohort_match(start_dt, end_dt, filters)},
            *([{"$match": user_filter}] if user_filter else []),
            {"$addFields": {
                "_src": {"$ifNull": ["$prospecto.origen", "Sin informacion"]},
                "_is_hot": {"$cond": [{"$eq": ["$lead_temperature_effective", "HOT"]}, 1, 0]},
                "_is_assigned": {"$cond": [{"$not": [{"$in": ["$ejecutivo_asignado", UNASSIGNED_VALUES]}]}, 1, 0]},
                "_is_advanced": {"$cond": [{"$in": [{"$ifNull": ["$pipeline_stage", ""]}, ADVANCED_STAGES]}, 1, 0]},
            }},
            {"$group": {
                "_id": "$_src",
                "received": {"$sum": 1},
                "hot": {"$sum": "$_is_hot"},
                "assigned": {"$sum": "$_is_assigned"},
                "advanced": {"$sum": "$_is_advanced"},
            }},
        ]
        return list(db["leads"].aggregate(pipeline))

    current_rows = {r["_id"]: r for r in _per_source(start_utc, end_utc)}
    previous_rows = ({r["_id"]: r for r in _per_source(prev_start, prev_end)}
                     if include_comparison else {})

    total_cur = sum(r["received"] for r in current_rows.values())
    total_prev = sum(r["received"] for r in previous_rows.values())

    result = []
    for src_id, cur in sorted(current_rows.items(), key=lambda kv: kv[1]["received"], reverse=True):
        prev = previous_rows.get(src_id, {})
        prev_recv = prev.get("received", 0)
        var_pct = round((cur["received"] - prev_recv) / prev_recv * 100, 1) if prev_recv else None
        result.append({
            "source": src_id,
            "received": cur["received"],
            "pct_of_total": round(cur["received"] / total_cur * 100, 1) if total_cur else 0,
            "hot_pct": round(cur["hot"] / cur["received"] * 100, 1) if cur["received"] else 0,
            "assigned_pct": round(cur["assigned"] / cur["received"] * 100, 1) if cur["received"] else 0,
            "advanced_pct": round(cur["advanced"] / cur["received"] * 100, 1) if cur["received"] else 0,
            "variation_pct": var_pct,
        })

    return result


def _safe_timestamp(value: Any) -> str:
    """Convierte cualquier valor a string ISO."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return str(value or "")


def query_executive_load(
    executive: Optional[str] = None,
) -> dict:
    """Carga actual por ejecutivo: activos, hot, NEW/sin etapa, mediana antiguedad."""
    db = get_db()
    active = build_active_filter()
    user_filter = _build_user_filter(executive)
    match = {"$and": [active, user_filter]} if user_filter else active

    pipeline = [
        _normalized_created_at_stage(),
        {"$match": match},
        {
            "$addFields": {
                "_ex": {"$ifNull": ["$ejecutivo_asignado", "Sin Asignar"]},
                "_age_days": {
                    "$dateDiff": {
                        "startDate": "$_created_normalized",
                        "endDate": datetime.now(timezone.utc),
                        "unit": "day",
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_ex",
                "active": {"$sum": 1},
                "hot": {"$sum": {"$cond": [{"$eq": ["$lead_temperature_effective", "HOT"]}, 1, 0]}},
                "new_or_none": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$pipeline_stage", ["NEW", None, ""]]},
                            1,
                            0,
                        ]
                    }
                },
                "ages": {"$push": "$_age_days"},
            }
        },
        {"$sort": {"active": -1}},
    ]

    rows = list(db["leads"].aggregate(pipeline))
    result = []
    for r in rows:
        ages = sorted([a for a in r.get("ages", []) if a is not None])
        median = ages[len(ages) // 2] if ages else 0
        result.append({
            "executive": r["_id"],
            "active": r["active"],
            "hot": r["hot"],
            "new_or_none": r["new_or_none"],
            "median_age_days": median,
        })
    return {"executives": result}


def query_source_quality(
    executive: Optional[str] = None,
) -> dict:
    """Calidad por origen: activos, %hot, %asignados, %contacted, mediana antiguedad."""
    db = get_db()
    active = build_active_filter()
    user_filter = _build_user_filter(executive)
    match = {"$and": [active, user_filter]} if user_filter else active

    pipeline = [
        _normalized_created_at_stage(),
        {"$match": match},
        {
            "$addFields": {
                "_src": {"$ifNull": ["$prospecto.origen", "Sin informacion"]},
                "_is_hot": {"$cond": [{"$eq": ["$lead_temperature_effective", "HOT"]}, 1, 0]},
                "_is_assigned": {"$cond": [{"$not": [{"$in": ["$ejecutivo_asignado", UNASSIGNED_VALUES]}]}, 1, 0]},
                "_is_contacted": {"$cond": [{"$eq": ["$pipeline_stage", "CONTACTED"]}, 1, 0]},
                "_age_days": {"$dateDiff": {"startDate": "$_created_normalized", "endDate": datetime.now(timezone.utc), "unit": "day"}},
            }
        },
        {
            "$group": {
                "_id": "$_src",
                "active": {"$sum": 1},
                "hot": {"$sum": "$_is_hot"},
                "assigned": {"$sum": "$_is_assigned"},
                "contacted": {"$sum": "$_is_contacted"},
                "ages": {"$push": "$_age_days"},
            }
        },
        {"$sort": {"active": -1}},
    ]

    rows = list(db["leads"].aggregate(pipeline))
    result = []
    for r in rows:
        ages = sorted([a for a in r.get("ages", []) if a is not None])
        median = ages[len(ages) // 2] if ages else 0
        total = r["active"]
        result.append({
            "source": r["_id"],
            "active": total,
            "hot_pct": round(r["hot"] / total * 100, 1) if total else 0,
            "assigned_pct": round(r["assigned"] / total * 100, 1) if total else 0,
            "contacted_pct": round(r["contacted"] / total * 100, 1) if total else 0,
            "median_age_days": median,
        })
    return {"sources": result}


def query_priorities(
    executive: Optional[str] = None,
) -> dict:
    """Alertas demostrables con datos actuales."""
    db = get_db()
    active = build_active_filter()
    user_filter = _build_user_filter(executive)
    match = {"$and": [active, user_filter]} if user_filter else active

    def _count(extra=None):
        q = {"$and": [match, extra]} if extra else match
        pipeline = [_normalized_created_at_stage(), {"$match": q}, {"$count": "c"}]
        r = list(db["leads"].aggregate(pipeline))
        return r[0]["c"] if r else 0

    now = datetime.now(timezone.utc)
    cutoff_48h = now - timedelta(hours=48)
    cutoff_7d = now - timedelta(days=7)

    alerts = [
        {
            "type": "hot_unassigned",
            "severity": "high",
            "label": "Hot sin ejecutivo",
            "description": "Leads Hot sin ejecutivo asignado",
            "count": _count({
                "lead_temperature_effective": "HOT",
                "ejecutivo_asignado": {"$in": ["Sin Asignar", "No Asignado", None, ""]},
            }),
        },
        {
            "type": "hot_new_assigned",
            "severity": "high",
            "label": "Hot en etapa NEW",
            "description": "Leads Hot con etapa NEW y ejecutivo",
            "count": _count({
                "lead_temperature_effective": "HOT",
                "pipeline_stage": "NEW",
                "ejecutivo_asignado": {"$nin": ["Sin Asignar", "No Asignado", None, ""]},
            }),
        },
        {
            "type": "priority_critical",
            "severity": "high",
            "label": "Prioridad critica actual",
            "description": "Leads con priority_bucket = CRITICAL",
            "count": _count({
                "priority_bucket": "CRITICAL",
            }),
        },
        {
            "type": "unassigned_over_48h",
            "severity": "medium",
            "label": "Sin asignar por mas de 48 horas",
            "description": "Leads sin ejecutivo desde hace mas de 48 horas",
            "count": _count({
                "ejecutivo_asignado": {"$in": ["Sin Asignar", "No Asignado", None, ""]},
                "$expr": {"$gte": [{"$ifNull": ["$_created_normalized", {"$toDate": "1970-01-01T00:00:00Z"}]}, cutoff_48h]},
            }),
        },
        {
            "type": "new_over_7d",
            "severity": "medium",
            "label": "Estancados en etapa inicial",
            "description": "NEW o sin etapa por mas de 7 dias",
            "count": _count({
                "pipeline_stage": {"$in": ["NEW", None, ""]},
                "$expr": {"$lte": ["$_created_normalized", cutoff_7d]},
            }),
        },
        {
            "type": "no_source",
            "severity": "low",
            "label": "Sin codigo de propiedad",
            "description": "Leads activos sin codigo de propiedad registrado",
            "count": _count({
                "$or": [
                    {"prospecto.codigo": {"$in": [None, ""]}},
                    {"prospecto.codigo": {"$exists": False}},
                ],
            }),
        },
    ]
    return {"alerts": alerts}


def query_comparative_trends(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    comparison_start: Optional[str] = None,
    comparison_end: Optional[str] = None,
    include_comparison: bool = True,
    filters: Optional[dict] = None,
) -> dict:
    """Tendencia comparativa: periodo actual vs periodo anterior de igual duracion.

    Optimizado: una sola agregación con $facet para actual y anterior.
    """
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    lookback_start_utc = start_utc - timedelta(days=6)

    if not include_comparison:
        prev_start = prev_end = None
    elif comparison_start and comparison_end:
        prev_start, prev_end = _build_chile_period_bounds(comparison_start, comparison_end)
    else:
        duration = end_utc - start_utc
        prev_end = start_utc
        prev_start = prev_end - duration

    if include_comparison and prev_start is not None and prev_end is not None:
        combined_start = min(lookback_start_utc, prev_start)
        combined_end = max(end_utc, prev_end)
        facets = {
            "current": [
                {"$match": {"_created_normalized": {"$gte": start_utc, "$lt": end_utc}}},
                {"$group": {"_id": _format_date_field("$_created_normalized", timezone="America/Santiago"), "received": {"$sum": 1}}},
                {"$sort": {"_id": 1}},
                {"$project": {"date": "$_id", "received": 1, "_id": 0}},
            ],
            "current_history": [
                {"$match": {"_created_normalized": {"$gte": lookback_start_utc, "$lt": end_utc}}},
                {"$group": {"_id": _format_date_field("$_created_normalized", timezone="America/Santiago"), "received": {"$sum": 1}}},
                {"$sort": {"_id": 1}},
                {"$project": {"date": "$_id", "received": 1, "_id": 0}},
            ],
            "previous": [
                {"$match": {"_created_normalized": {"$gte": prev_start, "$lt": prev_end}}},
                {"$group": {"_id": _format_date_field("$_created_normalized", timezone="America/Santiago"), "received": {"$sum": 1}}},
                {"$sort": {"_id": 1}},
                {"$project": {"date": "$_id", "received": 1, "_id": 0}},
            ],
        }
        pipeline = [
            _cohort_indexed_prefilter(combined_start, combined_end),
            _normalized_created_at_stage(),
            {"$match": _build_commercial_cohort_match(combined_start, combined_end, filters)},
            {"$facet": facets},
        ]
        row = list(db["leads"].aggregate(pipeline))
        row = row[0] if row else {}
        current_daily = row.get("current", [])
        current_history = row.get("current_history", [])
        previous_daily = row.get("previous", [])
    else:
        pipeline = [
            _cohort_indexed_prefilter(lookback_start_utc, end_utc),
            _normalized_created_at_stage(),
            {"$match": _build_commercial_cohort_match(lookback_start_utc, end_utc, filters)},
            {"$group": {"_id": _format_date_field("$_created_normalized", timezone="America/Santiago"), "received": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
            {"$project": {"date": "$_id", "received": 1, "_id": 0}},
        ]
        current_history = list(db["leads"].aggregate(pipeline))
        current_daily = [row for row in current_history if period_start <= row["date"] <= period_end]
        previous_daily = []

    # Rellenar días sin leads con 0 para que ambas series sean continuas y
    # alineadas por fecha (evita cortes en la curva spline).
    from datetime import date as _date, timedelta as _td

    def _fill_daily(daily_rows, start_str, end_str):
        by_date = {d["date"]: d["received"] for d in daily_rows}
        filled = []
        cur = _date.fromisoformat(start_str)
        end = _date.fromisoformat(end_str)
        while cur <= end:
            key = cur.strftime("%Y-%m-%d")
            filled.append({"date": key, "received": by_date.get(key, 0)})
            cur += _td(days=1)
        return filled

    if period_start and period_end:
        current_daily = _fill_daily(current_daily, period_start, period_end)
        lookback_start_date = (_date.fromisoformat(period_start) - _td(days=6)).isoformat()
        current_history = _fill_daily(current_history, lookback_start_date, period_end)
    if include_comparison and comparison_start and comparison_end:
        previous_daily = _fill_daily(previous_daily, comparison_start, comparison_end)

    current_total = sum(d["received"] for d in current_daily)
    previous_total = sum(d["received"] for d in previous_daily) if include_comparison else None
    pct_var = round(
        ((current_total - previous_total) / previous_total * 100), 1
    ) if previous_total else None

    current_avg = round(current_total / max(len(current_daily), 1), 1)
    previous_avg = round(previous_total / max(len(previous_daily), 1), 1) if include_comparison else None

    return {
        "current": {
            "daily": current_daily,
            "daily_history": current_history,
            "total": current_total,
            "avg_daily": current_avg,
        },
        "previous": {
            "daily": previous_daily,
            "total": previous_total,
            "avg_daily": previous_avg,
        },
        "variation_pct": pct_var,
    }


# Evidencia canónica de que la visita de un lead es trazable (permite afirmar
# que si hubo una visita, quedó registrada). Se conserva como métrica de
# calidad de datos (evaluable_leads / traceability_pct), NO como denominador
# de la conversión (decisión BI: el denominador es el total del período).
_VISIT_TRACEABILITY_MATCH = {
    "$or": [
        {"pipeline_stage": {"$exists": True, "$nin": [None, ""]}},
        {"stage": {"$exists": True, "$nin": [None, ""]}},
        {"stage_history": {"$exists": True, "$ne": []}},
        {"bi_analytics_global.RESULTADO_CHAT": {"$exists": True, "$nin": [None, ""]}},
        {"lifecycle.visit_scheduled_at": {"$exists": True, "$ne": None}},
    ]
}

# =============================================================================
# CONVERSIÓN A VISITA AGENDADA
# =============================================================================

# Fuente B — resultados de gestión en crm_events que significan inequívocamente
# "visita agendada". Valor real observado en producción: 'visita_agendada'.
# Se compara en mayúsculas contra la forma canónica. NO incluye intención
# (ASK_VISIT, VISITA_SOLICITADA) ni frases del cliente.
CANONICAL_SCHEDULED_VISIT_RESULTS = frozenset({"VISITA_AGENDADA"})

# Fuente C — estados de orden de visita que representan confirmación firme.
# Por decisión BI solo 'signed' (firma del cliente) cuenta; sent/opened/
# otp_requested NO demuestran confirmación.
CANONICAL_SIGNED_ORDER_STATUSES = frozenset({"signed"})


def _profile_query(profile: Optional[dict], collection: str, operation: str, loader):
    """Materialize one read and record safe, aggregate-only diagnostics."""
    started = __import__("time").perf_counter()
    rows = list(loader())
    if profile is not None:
        profile.setdefault("mongo", []).append({
            "collection": collection,
            "operation": operation,
            "duration_ms": round((__import__("time").perf_counter() - started) * 1000, 1),
            "documents": len(rows),
        })
    return rows


def _normalize_match_phone(raw: str) -> str:
    """Normalización determinística para emparejar orden de visita <-> lead."""
    from chatbot.phone_utils import extract_digits
    digits = extract_digits(raw or "")
    if digits.startswith("56") and len(digits) > 9:
        digits = digits[2:]
    return digits


def _order_accepted_timestamp(order: dict) -> Optional[datetime]:
    """Timestamp canónico de confirmación de una orden firmada.

    Se toma del timeline con action == 'accepted' (server_timestamp).
    Si no existe accepted con timestamp válido, NO se inventa fecha
    (reportado como problema de calidad de datos).
    """
    for entry in order.get("timeline") or []:
        if (entry or {}).get("action") == "accepted":
            ts = coerce_utc_datetime((entry or {}).get("server_timestamp"))
            if ts:
                return ts
    return None


def _match_signed_orders_to_leads(orders: list, leads: list) -> tuple:
    """Asocia órdenes de visita firmadas a leads de la cohorte de forma
    determinística (sin heurísticas probabilísticas).

    Estrategia por orden firmada:
      1. Candidatos por teléfono normalizado + property_code (ambos disponibles):
         si hay exactamente 1 candidato, se atribuye.
      2. Si el match por teléfono+propiedad no resuelve, se cae a teléfono
         normalizado SOLO si identifica inequívocamente a un único lead.
      3. Múltiples candidatos sin poder determinar -> ambiguo, NO se atribuye.

    Retorna (lead_id -> [accepted_utc, ...], ordenes_ambiguas). El mapa de
    confirmación agrega el timestamp 'accepted' de cada orden atribuida; los
    leads cuyo único valor es None quedan sin timestamp verificable.
    """
    from collections import defaultdict
    by_phone = defaultdict(list)
    for lead in leads:
        ph = _normalize_match_phone(lead.get("phone") or "")
        if ph:
            by_phone[ph].append(lead)

    attributed: dict = defaultdict(list)
    ambiguous = []
    for order in orders:
        ophone = _normalize_match_phone(order.get("phone") or "")
        ocod = str(order.get("property_code") or "").strip()
        if not ophone:
            continue
        candidates = by_phone.get(ophone, [])
        if not candidates:
            continue

        # 1. Teléfono + property_code cuando ambos están disponibles.
        if ocod:
            cand_pp = [c for c in candidates
                       if str((c.get("prospecto") or {}).get("codigo") or "").strip() == ocod]
            if len(cand_pp) == 1:
                ts = _order_accepted_timestamp(order)
                attributed[str(cand_pp[0]["_id"])].append(ts)
                continue
            if len(cand_pp) > 1:
                ambiguous.append(order.get("visita_code"))
                continue

        # 2. Teléfono solo si identifica inequívocamente a un único lead.
        if len(candidates) == 1:
            ts = _order_accepted_timestamp(order)
            attributed[str(candidates[0]["_id"])].append(ts)
            continue

        # 3. Múltiples candidatos no resolubles -> ambigüedad (no atribuir).
        ambiguous.append(order.get("visita_code"))

    return dict(attributed), ambiguous


def _scheduled_visit_lead_ids(
    cohort_leads: list,
    lead_ids: set,
    pe_utc,
    profile: Optional[dict] = None,
    signed_orders: Optional[list] = None,
    signed_orders_future=None,
    scheduled_events: Optional[list] = None,
) -> set:
    """Leads de la cohorte con evidencia canónica de visita agendada as-of.

    Definición EXACTA de CARD 2 (Conversión a Visita Agendada), reutilizada por
    el embudo para que ambos niveles reconcilien:
      - stage_history / lifecycle.visit_scheduled_at con timestamp válido;
      - crm_events.result == VISITA_AGENDADA con timestamp;
      - orden firmada con timeline accepted.server_timestamp (matching conservador).
    Regla temporal: lead.created_at <= evidencia < period_end (exclusivo).
    """
    from collections import defaultdict

    db = None
    scheduled = set()

    # Fuente A — stage_history / lifecycle con timestamps reales (as-of).
    for lead in cohort_leads:
        lid = str(lead["_id"])
        created_utc = coerce_utc_datetime(lead.get("created_at"))
        if created_utc is None:
            continue
        visit_at = coerce_utc_datetime((lead.get("lifecycle") or {}).get("visit_scheduled_at"))
        if visit_at is not None and created_utc <= visit_at < pe_utc:
            scheduled.add(lid)
            continue
        for entry in lead.get("stage_history") or []:
            to_stage = str((entry or {}).get("to") or "").upper()
            if to_stage not in {"VISIT_SCHEDULED", "VISIT_DONE"}:
                continue
            ts = coerce_utc_datetime((entry or {}).get("timestamp"))
            if ts is not None and created_utc <= ts < pe_utc:
                scheduled.add(lid)
                break

    # Fuente B — crm_events con resultado canónico de visita agendada (as-of).
    if lead_ids and scheduled_events is None:
        db = get_db()
        by_created = {str(l["_id"]): l.get("created_at") for l in cohort_leads}
        # Filtrar en Mongo por el resultado canónico. Antes se descargaban
        # todos los eventos de cada lead y se descartaban aquí, lo que podía
        # agotar socketTimeoutMS en cohortes grandes.
        event_filter = {
            "lead_id": {"$in": [l["_id"] for l in cohort_leads]},
            "$or": [
                {"result": {"$in": ["visita_agendada", "VISITA_AGENDADA"]}},
                {"meta.result": {"$in": ["visita_agendada", "VISITA_AGENDADA"]}},
            ],
        }
        events = _profile_query(profile, "crm_events", "find_scheduled_events", lambda: db["crm_events"].find(
            event_filter,
            {"lead_id": 1, "result": 1, "meta": 1, "timestamp": 1},
        ))
    else:
        events = scheduled_events or []
    if lead_ids:
        by_created = {str(l["_id"]): l.get("created_at") for l in cohort_leads}
        for event in events:
            raw = event.get("result") or (event.get("meta") or {}).get("result") or ""
            if str(raw).strip().upper() not in CANONICAL_SCHEDULED_VISIT_RESULTS:
                continue
            eid = str(event.get("lead_id"))
            if eid not in lead_ids:
                continue
            lead_created = coerce_utc_datetime(by_created.get(eid))
            if lead_created is None:
                continue
            event_ts = coerce_utc_datetime(event.get("timestamp"))
            if event_ts is not None and lead_created <= event_ts < pe_utc:
                scheduled.add(eid)

    # Fuente C — órdenes de visita firmadas con accepted timestamp (as-of).
    if signed_orders is None and signed_orders_future is not None:
        wait_started = time.perf_counter()
        signed_orders = signed_orders_future.result()
        if profile is not None:
            profile["signed_orders_wait_ms"] = round((time.perf_counter() - wait_started) * 1000, 1)
    if signed_orders is None:
        db = db or get_db()
        signed_orders = _profile_query(profile, "visitas", "find_signed_orders", lambda: db["visitas"].find(
            {"status": {"$in": list(CANONICAL_SIGNED_ORDER_STATUSES)}},
            {"visita_code": 1, "phone": 1, "property_code": 1, "timeline": 1, "created_at": 1},
        ))
    order_matches, ambiguous_orders = _match_signed_orders_to_leads(signed_orders, cohort_leads)
    if profile is not None:
        profile["orders_ambiguous"] = len(ambiguous_orders)
    by_created = {str(l["_id"]): l.get("created_at") for l in cohort_leads}
    for lid, accepted_list in order_matches.items():
        if lid not in lead_ids:
            continue
        lead_created = coerce_utc_datetime(by_created.get(lid))
        if lead_created is None:
            continue
        if any(ts is not None and lead_created <= ts < pe_utc for ts in accepted_list):
            scheduled.add(lid)

    return scheduled


def _cohort_conversion_metrics(db, ps_utc, pe_utc, filters, profile: Optional[dict] = None, signed_orders: Optional[list] = None):
    """Conversión as-of al cierre del período.

    Numerador: COUNT(DISTINCT lead) del período cuya evidencia canónica de
    visita agendada es temporalmente válida al cierre del período:
        lead.created_at <= evidence_timestamp < period_end
    El límite period_end es EXCLUSIVO (una evidencia en el límite pertenece al
    período siguiente). La evidencia nunca puede ser anterior a la creación
    del lead. El pipeline_stage/stage ACTUAL no se usa como fuente temporal
    porque no permite reconstruir cuándo ocurrió la transición (evitaría
    look-ahead histórico).

    Denominador: TODOS los leads del período (sin excluir no-trazados).
    """
    base_pipeline = [
        _cohort_indexed_prefilter(ps_utc, pe_utc),
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(ps_utc, pe_utc, filters)},
    ]
    pipeline = base_pipeline + [{"$facet": {
        "total": [{"$count": "c"}],
        "evaluable": [{"$match": _VISIT_TRACEABILITY_MATCH}, {"$count": "c"}],
        "cohort": [{"$project": {
            "_id": 1, "phone": 1,
            "prospecto.codigo": 1,
            "created_at": 1,
            "pipeline_stage": 1, "stage": 1,
            "stage_history": 1,
            "lifecycle.visit_scheduled_at": 1,
        }}],
    }}]
    row = _profile_query(profile, "leads", "aggregate_conversion_counts_and_cohort", lambda: db["leads"].aggregate(pipeline))
    combined = row[0] if row else {}
    total = ((combined.get("total") or [{}])[0]).get("c", 0)
    evaluable = ((combined.get("evaluable") or [{}])[0]).get("c", 0)
    cohort_leads = combined.get("cohort") or []
    lead_ids = {str(l["_id"]) for l in cohort_leads}

    evidence_profile = {}
    scheduled = _scheduled_visit_lead_ids(cohort_leads, lead_ids, pe_utc, evidence_profile, signed_orders)
    if profile is not None:
        profile.setdefault("mongo", []).extend(evidence_profile.get("mongo", []))
        profile["orders_ambiguous"] = profile.get("orders_ambiguous", 0) + evidence_profile.get("orders_ambiguous", 0)

    return {
        "total": total,
        "citas": len(scheduled),
        "evaluable": evaluable,
        "orders_ambiguous": evidence_profile.get("orders_ambiguous", 0),
    }


def query_leads_dashboard_conversion(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    comparison_start: Optional[str] = None,
    comparison_end: Optional[str] = None,
    include_comparison: bool = True,
    filters: Optional[dict] = None,
    timing: Optional[dict] = None,
    signed_orders: Optional[list] = None,
) -> dict:
    """Conversión a Visita Agendada para el Leads Dashboard (CARD 2).

    Numerador: COUNT(DISTINCT lead) del período con evidencia canónica de
    visita agendada (pipeline/stage_history/lifecycle + crm_events con resultado
    'visita_agendada' + orden de visita firmada asociada de forma determinística).

    Denominador: TODOS los leads del período (sin excluir no-trazados).
    """
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)

    if not include_comparison:
        prev_start = prev_end = None
    elif comparison_start and comparison_end:
        prev_start, prev_end = _build_chile_period_bounds(comparison_start, comparison_end)
    else:
        duration = end_utc - start_utc
        prev_end = start_utc
        prev_start = prev_end - duration

    current_profile = {}
    previous_profile = {}
    shared_orders = signed_orders
    if shared_orders is None:
        shared_orders = _profile_query(timing, "visitas", "find_signed_orders_shared", lambda: db["visitas"].find(
            {"status": {"$in": list(CANONICAL_SIGNED_ORDER_STATUSES)}},
            {"visita_code": 1, "phone": 1, "property_code": 1, "timeline": 1, "created_at": 1},
        ))
    current = _cohort_conversion_metrics(db, start_utc, end_utc, filters, current_profile, shared_orders)
    previous = _cohort_conversion_metrics(db, prev_start, prev_end, filters, previous_profile, shared_orders) if include_comparison else {
        "total": 0, "citas": 0, "evaluable": 0, "orders_ambiguous": 0,
    }
    if timing is not None:
        timing.update({"current": current_profile, "previous": previous_profile})

    return {
        "current": current,
        "previous": previous,
    }


def _to_positive_float(value):
    """Convierte un valor a float positivo utilizable, o None si no lo es."""
    if value is None or value == "" or value == 0:
        return None
    try:
        f = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _canonical_prices_for_codes(codes):
    """Precios vigentes (UF y CLP) por código desde la fuente canónica de cartera.

    Lee universo_cartera_prop360 (una sola query $in) y devuelve, por código:
    venta_uf / arriendo_uf y venta_clp / arriendo_clp (positivos) y op_canon
    (operación vigente cuando el lead no la especifica). No se usan leads de
    otros períodos.
    """
    if not codes:
        return {}
    out = {}
    for u in get_db()["universo_cartera_prop360"].find(
        {"codigo": {"$in": codes}}, {"codigo": 1, "tipo_operacion": 1}
    ):
        top = u.get("tipo_operacion") or {}
        pv = top.get("precio_venta") or {}
        pa = top.get("precio_arriendo") or {}
        op_canon = None
        if top.get("arriendo") and not top.get("venta"):
            op_canon = "Arriendo"
        elif top.get("venta"):
            op_canon = "Venta"
        out[str(u.get("codigo"))] = {
            "venta_uf": _to_positive_float(pv.get("precio_uf")),
            "venta_clp": _to_positive_float(pv.get("precio_clp")),
            "arriendo_uf": _to_positive_float(pa.get("precio_uf")),
            "arriendo_clp": _to_positive_float(pa.get("precio_clp")),
            "op_canon": op_canon,
        }
    return out


def _resolve_property_op(ops, canon):
    """Operación definitiva por propiedad: lead primero, canónico como respaldo.

    El lead puede no traer operación; entonces se usa la operación vigente
    de la fuente canónica. Si ninguna existe, "otro".
    """
    up = {str(o).upper() for o in ops}
    if up & {"VENTA", "V"}:
        return "Venta"
    if up & {"ARRIENDO", "A"}:
        return "Arriendo"
    if canon and canon.get("op_canon") in ("Venta", "Arriendo"):
        return canon["op_canon"]
    return "otro"


def _resolve_property_price(op, lead_price, canon, uf_value=None):
    """Precio definitivo de CARD 3: canónico de la operación primero.

    Prioridad conceptual por operación:
    1. UF vigente de la fuente canónica de cartera (precio_uf canónico);
    2. CLP vigente canónico convertido determinísticamente: precio_clp / UF
       (conversión de unidad, no imputación);
    3. fallback a campos embebidos del lead (prospecto.precio_uf ->
       cartera_data.precio_uf) solo si la fuente actual no entrega un precio
       utilizable en ninguna moneda para esa operación.
    Nunca se usa otra propiedad, promedios, ni precios históricos como actuales.
    Operación "Otro": sin valorización (se reporta aparte, no se inventa
    un precio de venta o arriendo para ella).
    """
    if op in ("Venta", "Arriendo"):
        if canon:
            if op == "Venta":
                if canon.get("venta_uf"):
                    return canon["venta_uf"]
                if uf_value and canon.get("venta_clp"):
                    return canon["venta_clp"] / uf_value
            else:
                if canon.get("arriendo_uf"):
                    return canon["arriendo_uf"]
                if uf_value and canon.get("arriendo_clp"):
                    return canon["arriendo_clp"] / uf_value
        return lead_price
    return None


# Orígenes/canales de prueba excluidos del universo comercial (regla estructural).
_TEST_ORIGINS = frozenset({
    "test", "backfill", "reconciliation", "automated_historical", "e2e_test",
})
_TEST_PHONE_PREFIXES = ("+56900000", "5690000000", "synthetic-archived-")
_TEST_PHONES = frozenset({"0000000000", "0000000000", "0000000000"})


def _is_test_lead(lead: dict) -> bool:
    """Regla estructural canónica de lead de prueba.

    No es una lista hardcodeada de códigos: usa flags ya existentes
    (_test_lead / is_test), orígenes/canales de prueba (EXCLUDED_ORIGINS del
    sistema de alertas) y teléfonos sintéticos conocidos. NO usa
    phone_is_synthetic porque ese flag también cubre leads reales del portal
    con número desconocido (no-phone-prop360-*).
    """
    if lead.get("_test_lead") is True or lead.get("is_test") is True:
        return True
    phone = str(lead.get("phone") or "")
    if phone in _TEST_PHONES or phone.startswith(_TEST_PHONE_PREFIXES):
        return True
    prospecto = lead.get("prospecto") or {}
    canal = str(prospecto.get("canal_origen") or "").lower()
    origen = str(prospecto.get("origen") or "").lower()
    nombre = str(prospecto.get("nombre") or "").lower()
    if canal in _TEST_ORIGINS or origen in _TEST_ORIGINS:
        return True
    if "test" in nombre or "e2e" in nombre:
        return True
    return False


def _build_test_lead_exclusion_match() -> dict:
    """$match de exclusión estructural de leads de prueba para CARD 3.

    Se aplica solo al universo de CARD 3 (no altera las demás métricas).
    Usa flags (y orígenes/canales) ya existentes; no depende de regex para
    mantener compatibilidad con motores de prueba y con la regla estructural.
    """
    return {
        "$and": [
            {"$or": [
                {"_test_lead": {"$ne": True}},
                {"_test_lead": {"$exists": False}},
            ]},
            {"$or": [
                {"is_test": {"$ne": True}},
                {"is_test": {"$exists": False}},
            ]},
            {"$or": [
                {"phone": {"$nin": list(_TEST_PHONES)}},
                {"phone": {"$exists": False}},
            ]},
            {"$or": [
                {"prospecto.canal_origen": {"$nin": list(_TEST_ORIGINS)}},
                {"prospecto.canal_origen": {"$exists": False}},
            ]},
            {"$or": [
                {"prospecto.origen": {"$nin": list(_TEST_ORIGINS)}},
                {"prospecto.origen": {"$exists": False}},
            ]},
        ]
    }


def _property_price_rows(period_start, period_end, filters=None, uf_value=None):
    """Filas de propiedad única (codigo, precio_uf, operacion) con precio
    resuelto desde la fuente canónica (universo_cartera_prop360) primero.

    Agrupa por prospecto.codigo, consulta la fuente canónica de cartera en
    una sola query y aplica la prioridad canónico (UF -> CLP/UF) -> lead.
    Excluye estructuralmente los leads de prueba del universo comercial.
    Devuelve también leads_interesados y banderas de disponibilidad de precio
    en cada fuente.
    """
    if uf_value is None:
        try:
            from chatbot.uf_service import leer_uf_cache
            _uf = leer_uf_cache()
            if _uf and _uf.get("valor"):
                uf_value = _uf["valor"]
        except Exception:
            uf_value = None
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    pipeline = [
        _cohort_indexed_prefilter(start_utc, end_utc),
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(start_utc, end_utc, filters)},
        {"$match": _build_test_lead_exclusion_match()},
        {"$addFields": {
            "_code": {"$ifNull": ["$prospecto.codigo", ""]},
            "_lead_precio_uf": {"$ifNull": ["$prospecto.precio_uf", "$cartera_data.precio_uf", None]},
            "_op": {"$toUpper": {"$ifNull": [
                {"$ifNull": ["$prospecto.operacion", {"$ifNull": ["$operacion", ""]}]},
                "",
            ]}},
        }},
        {"$match": {"_code": {"$ne": ""}}},
        {"$group": {
            "_id": "$_code",
            "lead_precio_uf": {"$push": "$_lead_precio_uf"},
            "ops": {"$addToSet": "$_op"},
            "leads_interesados": {"$sum": 1},
            "con_precio_lead": {"$sum": {"$cond": [{"$ne": ["$_lead_precio_uf", None]}, 1, 0]}},
        }},
    ]
    rows = list(db["leads"].aggregate(pipeline))
    if not rows:
        return []
    codes = [str(r["_id"]) for r in rows]
    canon_map = _canonical_prices_for_codes(codes)

    result = []
    for r in rows:
        code = str(r["_id"])
        canon = canon_map.get(code)
        op = _resolve_property_op(r["ops"], canon)
        lead_price = None
        for raw in r.get("lead_precio_uf", []):
            v = _to_positive_float(raw)
            if v and (lead_price is None or v > lead_price):
                lead_price = v
        price = _resolve_property_price(op, lead_price, canon, uf_value=uf_value)
        # Referencias vinculadas fuera de la cartera actual: solo diagnóstico,
        # sin valorización (no entran en Venta, Arriendo ni Comisión).
        if canon is None:
            price = None
        result.append({
            "codigo": code,
            "precio_uf": price,
            "operacion": op,
            "leads_interesados": r["leads_interesados"],
            "con_precio_lead": r["con_precio_lead"] > 0,
            "con_precio_canon": bool(canon and (canon.get("venta_uf") or canon.get("arriendo_uf")
                                                or canon.get("venta_clp") or canon.get("arriendo_clp"))),
            "existe_canon": canon is not None,
        })
    return result


def query_leads_dashboard_pipeline(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    filters: Optional[dict] = None,
    uf_value: Optional[float] = None,
) -> dict:
    """Valorización UF del pipeline para el Leads Dashboard (CARD 3).

    DISTINCT PROPERTY AGGREGATION: agrupa primero por propiedad única
    (prospecto.codigo) para NO inflar el pipeline cuando varios leads
    consultan por la misma propiedad. El precio por propiedad se resuelve
    con la fuente canónica de cartera (universo_cartera_prop360) primero
    (UF directo, luego CLP/UF) y los campos embebidos del lead como fallback.
    Excluye estructuralmente leads de prueba.
    """
    rows = _property_price_rows(period_start, period_end, filters, uf_value=uf_value)
    monto_uf = monto_venta_uf = monto_arriendo_uf = monto_otro_uf = 0.0
    propiedades_vinculadas = 0
    propiedades_cartera = 0
    propiedades_cartera_valorizadas = 0
    propiedades_venta = 0
    propiedades_arriendo = 0
    propiedades_otro = 0
    propiedades_sin_precio = 0
    propiedades_no_en_cartera = 0
    leads_vinculados = 0
    for p in rows:
        propiedades_vinculadas += 1
        leads_vinculados += p["leads_interesados"]
        if not p["existe_canon"]:
            # Referencia vinculada fuera de la cartera actual: solo diagnóstico.
            propiedades_no_en_cartera += 1
            continue
        propiedades_cartera += 1
        if p["operacion"] == "otro":
            # Operación "Otro": se reporta aparte, sin valorización.
            propiedades_otro += 1
            continue
        precio = p["precio_uf"]
        if precio is None:
            propiedades_sin_precio += 1
            continue
        propiedades_cartera_valorizadas += 1
        monto_uf += precio
        if p["operacion"] == "Venta":
            monto_venta_uf += precio
            propiedades_venta += 1
        else:
            monto_arriendo_uf += precio
            propiedades_arriendo += 1
    return {
        "leads_vinculados": leads_vinculados,
        "propiedades_vinculadas": propiedades_vinculadas,
        "propiedades_cartera": propiedades_cartera,
        "propiedades_con_precio": propiedades_cartera_valorizadas,
        "propiedades_cartera_valorizadas": propiedades_cartera_valorizadas,
        "propiedades_venta": propiedades_venta,
        "propiedades_arriendo": propiedades_arriendo,
        "propiedades_otro": propiedades_otro,
        "propiedades_sin_precio": propiedades_sin_precio,
        "propiedades_no_en_cartera": propiedades_no_en_cartera,
        "monto_uf": round(monto_uf, 1),
        "monto_venta_uf": round(monto_venta_uf, 1),
        "monto_arriendo_uf": round(monto_arriendo_uf, 1),
        "monto_otro_uf": round(monto_otro_uf, 1),
    }


def query_property_commission_rows(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    filters: Optional[dict] = None,
    uf_value: Optional[float] = None,
) -> list:
    """Filas de propiedad única (código, precio_uf, operación) con precio.

    Se usa para calcular la comisión potencial por propiedad (con mínimos
    contractuales individuales). Misma deduplicación canónica por
    prospecto.codigo y misma resolución de precio (canónico UF -> CLP/UF ->
    lead) que query_leads_dashboard_pipeline. Excluye estructuralmente leads
    de prueba.
    """
    result = []
    for p in _property_price_rows(period_start, period_end, filters, uf_value=uf_value):
        if p["precio_uf"] is None:
            continue
        result.append({"codigo": p["codigo"], "precio_uf": float(p["precio_uf"]),
                       "operacion": p["operacion"].lower()})
    return result


def _active_cartera_filter(oficina: Optional[str] = None) -> dict:
    """Filtro canónico de propiedad comercial ACTIVA en universo_cartera_prop360.

    Definición auditada:
    - ``disponible_prop360 == True``: estado fiable de activa/retirada (las
      inactivas además tienen fecha_baja_automatica).
    - operación comercial definida: ``tipo_operacion.venta`` o
      ``tipo_operacion.arriendo`` (excluye registros sin operación ni tipo).
    - opcionalmente restringe a una oficina (``estado.oficina``).
    """
    filtro: dict = {
        "disponible_prop360": True,
        "$or": [
            {"tipo_operacion.venta": True},
            {"tipo_operacion.arriendo": True},
        ],
    }
    if oficina:
        filtro["estado.oficina"] = {"$regex": f"^{re.escape(oficina)}$", "$options": "i"}
    return filtro


def count_active_cartera_properties(oficina: Optional[str] = None) -> int:
    """Propiedades comerciales ACTIVAS de la cartera actual (denominador CARD 3).

    Por defecto toda la compañía; con ``oficina`` restringe a una oficina
    (p. ej. "PROCASA SUCRE"). El conteo es en vivo (no estático): refleja el
    inventario vigente al momento de la consulta.
    """
    try:
        db = get_db()
        return int(db["universo_cartera_prop360"].count_documents(
            _active_cartera_filter(oficina)))
    except Exception:
        return 0


def query_cartera_demanda_coverage(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    oficina: str = "PROCASA SUCRE",
) -> dict:
    """Cobertura de demanda sobre la cartera ACTIVA de una oficina (footer CARD 3).

    - ``propiedades_activas``: cartera comercial activa de la oficina
      (denominador dinámico, en vivo).
    - ``propiedades_con_demanda``: de esas activas, códigos únicos con al menos
      un lead del período (sin leads de prueba), deduplicados por código.
    - ``pct_cartera_con_demanda``: con_demanda / activas * 100 (1 decimal).
    """
    db = get_db()
    active_codes = set()
    for d in db["universo_cartera_prop360"].find(
            _active_cartera_filter(oficina), {"codigo": 1}):
        c = d.get("codigo")
        if c is not None:
            active_codes.add(str(c))

    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    demand_codes = set()
    for l in db["leads"].aggregate([
        _cohort_indexed_prefilter(start_utc, end_utc),
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(start_utc, end_utc)},
        {"$match": _build_test_lead_exclusion_match()},
        {"$project": {"prospecto.codigo": 1}},
    ]):
        c = (l.get("prospecto") or {}).get("codigo")
        if c is not None and str(c).strip():
            demand_codes.add(str(c))

    activas = len(active_codes)
    con_demanda = len(demand_codes & active_codes)
    pct = round(con_demanda / activas * 100, 1) if activas else None
    return {
        "propiedades_con_demanda": con_demanda,
        "propiedades_activas": activas,
        "pct_cartera_con_demanda": pct,
    }


INVENTORY_PUBLICATION_PORTALS = ("procasa", "portal_inmobiliario", "toctoc", "yapo")


def _inventory_code(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _inventory_responsible(doc: Mapping[str, Any]) -> str:
    state = doc.get("estado") or {}
    for key in ("ejecutivo", "captador", "responsable"):
        value = state.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return "Sin responsable"


def _inventory_operation(doc: Mapping[str, Any]) -> str:
    operations = doc.get("tipo_operacion") or {}
    venta = bool(operations.get("venta"))
    arriendo = bool(operations.get("arriendo"))
    if venta and arriendo:
        return "Venta + Arriendo"
    if venta:
        return "Venta"
    if arriendo:
        return "Arriendo"
    return "Otra situación"


def _inventory_publications(publicaciones: Mapping[str, Any] | None) -> dict:
    result = {}
    publicaciones = publicaciones if isinstance(publicaciones, Mapping) else {}
    for portal in INVENTORY_PUBLICATION_PORTALS:
        source = publicaciones.get(portal)
        source = source if isinstance(source, Mapping) else {}
        records = [source]
        nested = source.get("publicaciones")
        if isinstance(nested, Mapping):
            records.extend(value for value in nested.values() if isinstance(value, Mapping))
        has_url = any(str(item.get("url") or item.get("url_publicacion") or "").strip() for item in records)
        has_external_id = any(str(item.get("id") or item.get("codigo") or item.get("external_id") or "").strip() for item in records)
        published_values = [item.get("publicada") for item in records if isinstance(item.get("publicada"), bool)]
        published = True if any(published_values) else (False if published_values and not any(published_values) else None)
        result[portal] = {
            "has_evidence": bool(has_url or has_external_id or published is True),
            "has_url": has_url,
            "has_external_id": has_external_id,
            "published": published,
        }
    return result


def _inventory_price(doc: Mapping[str, Any]) -> dict:
    operations = doc.get("tipo_operacion") or {}
    result = {}
    for operation, key in (("venta", "venta"), ("arriendo", "arriendo")):
        raw = operations.get(f"precio_{operation}")
        if isinstance(raw, Mapping):
            result[f"{key}_uf"] = raw.get("uf") or raw.get("precio_uf")
            result[f"{key}_clp"] = raw.get("clp") or raw.get("precio_clp") or raw.get("pesos")
        else:
            result[f"{key}_uf"] = raw if isinstance(raw, (int, float)) else None
            result[f"{key}_clp"] = None
    return result


def _inventory_pct(value: int, total: int) -> float | None:
    return round(value / total * 100, 1) if total else None


def build_properties_inventory_contract(
    inventory_docs: list[Mapping[str, Any]],
    demand_counts: Mapping[str, int] | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    filters: Mapping[str, Any] | None = None,
    top_n: int = 10,
) -> dict:
    """Build the read-only inventory payload from one inventory batch and one demand batch."""
    filters = {key: str(value).strip() for key, value in (filters or {}).items() if value not in (None, "")}
    demand_counts = {_inventory_code(key): int(value or 0) for key, value in (demand_counts or {}).items()}
    unique = {}
    duplicate_docs = 0
    for doc in inventory_docs:
        code = _inventory_code(doc.get("codigo"))
        if not code:
            continue
        if code in unique:
            duplicate_docs += 1
            continue
        unique[code] = doc

    all_records = []
    for code, doc in sorted(unique.items()):
        operation = _inventory_operation(doc)
        record = {
            "code": code,
            "type": str(doc.get("tipo_propiedad") or doc.get("tipo") or (doc.get("tipo_operacion") or {}).get("tipo") or "Sin tipo").strip() or "Sin tipo",
            "operation": operation,
            "commune": str(doc.get("comuna") or doc.get("comuna_nombre") or (doc.get("ubicacion") or {}).get("comuna") or "Sin comuna").strip() or "Sin comuna",
            "responsible": _inventory_responsible(doc),
            "price": _inventory_price(doc),
            "leads_period": int(demand_counts.get(code, 0)),
            "publications": _inventory_publications(doc.get("publicaciones")),
        }
        record["has_demand"] = record["leads_period"] > 0
        all_records.append(record)

    def matches(record):
        aliases = {"property_type": "type"}
        for key, expected in filters.items():
            record_key = aliases.get(key, key)
            if record_key in ("operation", "type", "commune", "responsible") and record.get(record_key) != expected:
                return False
        return True

    records = [record for record in all_records if matches(record)]
    stock = len(records)
    with_demand = sum(record["has_demand"] for record in records)
    without_demand = stock - with_demand

    def distribution(key: str, limit: int | None = None):
        counts = defaultdict(int)
        for record in records:
            counts[record[key]] += 1
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
        if limit and len(ordered) > limit:
            visible = ordered[:limit]
            other_count = sum(value for _, value in ordered[limit:])
            if other_count:
                visible.append(("Otros", other_count))
            ordered = visible
        return [{"label": label, "count": count, "pct": _inventory_pct(count, stock)} for label, count in ordered]

    responsible_counts = defaultdict(lambda: {"active": 0, "with_demand": 0})
    for record in records:
        item = responsible_counts[record["responsible"]]
        item["active"] += 1
        item["with_demand"] += int(record["has_demand"])
    responsibles = []
    for label, item in sorted(responsible_counts.items(), key=lambda pair: (-pair[1]["active"], pair[0].lower())):
        responsibles.append({
            "responsible": label,
            "active": item["active"],
            "pct_inventory": _inventory_pct(item["active"], stock),
            "with_demand": item["with_demand"],
            "without_demand": item["active"] - item["with_demand"],
            "coverage_pct": _inventory_pct(item["with_demand"], item["active"]),
        })

    intervention = [dict(record, reason="SIN_DEMANDA_PERIODO") for record in records if not record["has_demand"]]
    intervention.sort(key=lambda record: (record["responsible"] == "Sin responsable", record["responsible"].lower(), record["commune"].lower(), record["code"]))
    evidence = {portal: sum(bool(record["publications"][portal]["has_evidence"]) for record in records) for portal in INVENTORY_PUBLICATION_PORTALS}
    coverage_pct = _inventory_pct(with_demand, stock)
    return {
        "meta": {"source": "universo_cartera_prop360", "period_start": period_start, "period_end": period_end, "stock_period_independent": True, "filters": filters, "mongo_reads": 2},
        "inventory": {"active": stock, "with_demand": with_demand, "without_demand": without_demand, "coverage_pct": coverage_pct, "reconciliation": with_demand + without_demand == stock},
        "demand_coverage": {"with_demand": {"count": with_demand, "pct": coverage_pct}, "without_demand": {"count": without_demand, "pct": _inventory_pct(without_demand, stock)}, "interpretation": "Cobertura de demanda baja" if coverage_pct is not None and coverage_pct < 25 else "Cobertura de demanda"},
        "composition": {"operation": distribution("operation"), "type": distribution("type", top_n), "commune": distribution("commune", top_n)},
        "responsibles": responsibles,
        "intervention": intervention,
        "properties": records,
        "filter_options": {key: distribution(key, None) for key in ("operation", "type", "commune", "responsible")},
        "data_quality": {"duplicate_source_docs": duplicate_docs, "publication_evidence": evidence, "missing_responsible": sum(record["responsible"] == "Sin responsable" for record in records)},
    }


def query_properties_inventory_dashboard(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    filters: Mapping[str, Any] | None = None,
) -> dict:
    """Read-only inventory snapshot plus period-scoped demand, in two batches."""
    db = get_db()
    docs = list(db["universo_cartera_prop360"].find(_active_cartera_filter(), {
        "codigo": 1, "tipo_propiedad": 1, "tipo": 1, "comuna": 1, "comuna_nombre": 1,
        "tipo_operacion": 1, "ubicacion.comuna": 1, "estado.ejecutivo": 1, "estado.captador": 1, "estado.responsable": 1,
        "publicaciones": 1,
    }))
    codes = {_inventory_code(doc.get("codigo")) for doc in docs if _inventory_code(doc.get("codigo"))}
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    demand_counts = defaultdict(int)
    if codes:
        pipeline = [
            _cohort_indexed_prefilter(start_utc, end_utc),
            _normalized_created_at_stage(),
            {"$match": _build_commercial_cohort_match(start_utc, end_utc)},
            {"$match": _build_test_lead_exclusion_match()},
            {"$project": {"prospecto.codigo": 1}},
        ]
        for lead in db["leads"].aggregate(pipeline):
            code = _inventory_code((lead.get("prospecto") or {}).get("codigo"))
            if code in codes:
                demand_counts[code] += 1
    return build_properties_inventory_contract(docs, demand_counts, period_start, period_end, filters)


DEMAND_CAPTURE_CANONICAL_OFFICE = "PROCASA SUCRE"
DEMAND_CAPTURE_DIMENSIONS = ("operation", "type", "region", "zone_rm", "commune", "price_range", "bedrooms")

# Mapping territorial explícito y auditable. Solo aplica cuando la región canónica
# es Metropolitana; fuera de RM la dimensión Zona RM queda fuera de la distribución.
DEMAND_CAPTURE_RM_ZONE_MAPPING = {
    "Oriente": ("Las Condes", "Lo Barnechea", "La Reina", "Macul", "Ñuñoa", "Peñalolén", "Providencia", "Vitacura"),
    "Centro": ("Santiago", "Estación Central", "Independencia", "Recoleta", "San Miguel", "San Joaquín", "Quinta Normal", "Pedro Aguirre Cerda"),
    "Poniente": ("Maipú", "Cerrillos", "Cerro Navia", "Lo Prado", "Lo Espejo", "Pudahuel"),
    "Norte": ("Colina", "Conchalí", "Huechuraba", "Lampa", "Quilicura", "Renca", "Tiltil"),
    "Sur": ("Buin", "La Cisterna", "La Florida", "La Granja", "La Pintana", "San Bernardo", "San Ramón", "Puente Alto"),
}
DEMAND_CAPTURE_RESIDENTIAL_TYPES = {"casa", "departamento"}


def _demand_capture_normalized_text(value: Any) -> str:
    import unicodedata
    return "".join(char for char in unicodedata.normalize("NFKD", str(value or "")) if not unicodedata.combining(char)).strip().casefold()


def _demand_capture_zone_rm(region: str | None, commune: str | None) -> str | None:
    if _demand_capture_normalized_text(region) != "metropolitana":
        return None
    commune_key = _demand_capture_normalized_text(commune)
    for zone, communes in DEMAND_CAPTURE_RM_ZONE_MAPPING.items():
        if commune_key in {_demand_capture_normalized_text(item) for item in communes}:
            return zone
    return None


def _demand_capture_is_residential(property_type: str) -> bool:
    return _demand_capture_normalized_text(property_type) in DEMAND_CAPTURE_RESIDENTIAL_TYPES


def _demand_capture_geo_key(value: str | None) -> str | None:
    """Canonical join key for Chilean region/comuna names used by GeoJSON."""
    normalized = _demand_capture_normalized_text(value)
    if not normalized:
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    normalized = re.sub(r"^region (?:de|del|la) ", "", normalized)
    aliases = {
        "metropolitana": "metropolitana",
        "region metropolitana": "metropolitana",
        "region metropolitana de santiago": "metropolitana",
        "libertador bernardo ohiggins": "ohiggins",
        "bernardo ohiggins": "ohiggins",
        "o higgins": "ohiggins",
        "ohiggins": "ohiggins",
        "bio bio": "biobio",
        "biobio": "biobio",
        "nuble": "nuble",
        "la araucania": "araucania",
        "araucania": "araucania",
        "aysen": "aysen",
        "magallanes y antartica chilena": "magallanes",
    }
    return aliases.get(normalized, normalized)


def _demand_capture_geo_row(records: list[dict], dimension: str, total_leads: int, total_stock: int) -> list[dict]:
    """Aggregate one territorial level from the already loaded demand batch."""
    grouped: dict[str, dict] = {}
    for record in records:
        name = record.get(dimension)
        geo_key = _demand_capture_geo_key(name)
        if not name or not geo_key:
            continue
        item = grouped.setdefault(geo_key, {"name": name, "geo_key": geo_key, "leads": 0, "codes": set(), "stock_sucre": 0, "region_geo_keys": set()})
        item["leads"] += 1
        item["codes"].add(record["code"])
        if dimension == "commune" and record.get("region"):
            item["region_geo_keys"].add(_demand_capture_geo_key(record.get("region")))
        if record.get("is_active_sucre"):
            item["stock_sucre"] += 1
    rows = []
    for item in grouped.values():
        leads = item["leads"]
        properties = len(item["codes"])
        demand_share = round(leads / total_leads * 100, 1) if total_leads else None
        supply_share = round(item["stock_sucre"] / total_stock * 100, 1) if total_stock else None
        rows.append({
            "name": item["name"], "geo_key": item["geo_key"], "region_geo_key": next(iter(item["region_geo_keys"]), None), "leads": leads,
            "demand_share_pct": demand_share, "properties_with_demand": properties,
            "leads_per_property": round(leads / properties, 2) if properties else None,
            "stock_sucre": item["stock_sucre"], "supply_share_pct": supply_share,
            "gap_pp": round(demand_share - supply_share, 1) if demand_share is not None and supply_share is not None else None,
        })
    return sorted(rows, key=lambda row: (-row["leads"], -row["stock_sucre"], row["name"].casefold()))


def _demand_capture_territorial_segments(records: list[dict], limit: int = 5) -> list[dict]:
    grouped: Counter = Counter()
    for record in records:
        price = record.get("price_range") or ""
        for prefix in ("Venta · ", "Arriendo · "):
            if price.startswith(prefix):
                price = price[len(prefix):]
        values = [record.get("operation"), record.get("type"), price]
        if record.get("bedrooms"):
            values.append(f"{record['bedrooms']} dorm.")
        if all(values):
            grouped[" · ".join(values)] += 1
    return [{"segment": label, "leads": count} for label, count in grouped.most_common(limit)]


def _demand_capture_operation(doc: Mapping[str, Any]) -> str:
    operation = _inventory_operation(doc)
    if operation != "Otra situación":
        return operation
    raw = str((doc.get("prospecto") or {}).get("operacion") or "").strip().lower()
    if "arriend" in raw:
        return "Arriendo"
    if "venta" in raw:
        return "Venta"
    return "Otra situación"


def _demand_capture_profile(doc: Mapping[str, Any], code: str = "") -> dict:
    operations = doc.get("tipo_operacion") or {}
    location = doc.get("ubicacion") or {}
    features = doc.get("caracteristicas") or {}
    prospect = doc.get("prospecto") or {}
    operation = _demand_capture_operation(doc)
    property_type = str(operations.get("tipo") or doc.get("tipo_propiedad") or doc.get("tipo") or prospect.get("tipo") or "Sin tipo").strip() or "Sin tipo"
    commune_raw = location.get("comuna") or doc.get("comuna") or doc.get("comuna_nombre") or prospect.get("comuna")
    commune = str(commune_raw).strip() if commune_raw not in (None, "") else None
    region_raw = location.get("region") or doc.get("region") or prospect.get("region")
    region = str(region_raw).strip() if region_raw not in (None, "") else None
    raw_bedrooms = features.get("dormitorios", prospect.get("dormitorios"))
    try:
        bedrooms_value = int(float(raw_bedrooms)) if raw_bedrooms not in (None, "") else None
    except (TypeError, ValueError):
        bedrooms_value = None
    residential = _demand_capture_is_residential(property_type)
    bedrooms = ("4+" if bedrooms_value >= 4 else str(max(0, bedrooms_value))) if residential and bedrooms_value is not None else None
    price = operations.get("precio_venta" if operation == "Venta" else "precio_arriendo") or {}
    if operation == "Venta":
        price_value = price.get("precio_uf") or price.get("uf")
        if price_value is None:
            price_range = None
        elif float(price_value) <= 2000:
            price_range = "Venta · hasta 2.000 UF"
        elif float(price_value) <= 5000:
            price_range = "Venta · 2.001–5.000 UF"
        elif float(price_value) <= 10000:
            price_range = "Venta · 5.001–10.000 UF"
        else:
            price_range = "Venta · más de 10.000 UF"
    elif operation == "Arriendo":
        price_value = price.get("precio_clp") or price.get("clp")
        if price_value is None:
            price_range = None
        elif float(price_value) <= 500000:
            price_range = "Arriendo · hasta $500 mil"
        elif float(price_value) <= 1000000:
            price_range = "Arriendo · $500 mil–$1 millón"
        elif float(price_value) <= 2000000:
            price_range = "Arriendo · $1–2 millones"
        else:
            price_range = "Arriendo · más de $2 millones"
    elif operation == "Venta + Arriendo":
        price_range = None
    else:
        price_range = None
    bedrooms_segment = f"{property_type} · {bedrooms} dorm." if bedrooms is not None else None
    return {
        "code": code,
        "responsible": _inventory_responsible(doc),
        "operation": operation,
        "type": property_type,
        "commune": commune,
        "region": region,
        "zone_rm": _demand_capture_zone_rm(region, commune),
        "price_range": price_range,
        "bedrooms": bedrooms,
        "bedrooms_segment": bedrooms_segment,
        "bedrooms_value": bedrooms_value,
        "is_residential": residential,
    }


def _demand_capture_median(values: list[float]) -> float | None:
    values = sorted(values)
    if not values:
        return None
    middle = len(values) // 2
    return float(values[middle]) if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def _demand_capture_distribution(stock_records: list[dict], demand_records: list[dict], dimension: str, demand_total: int, stock_total: int) -> list[dict]:
    value_key = "bedrooms_segment" if dimension == "bedrooms" else dimension
    demand_records = [record for record in demand_records if record.get(value_key) not in (None, "")]
    stock_records = [record for record in stock_records if record.get(value_key) not in (None, "")]
    demand = Counter(record[value_key] for record in demand_records)
    demand_codes = defaultdict(set)
    stock = Counter()
    for record in stock_records:
        key = record[value_key]
        if record.get("is_active_sucre"):
            stock[key] += 1
    for record in demand_records:
        if record.get("is_active_sucre"):
            demand_codes[record[value_key]].add(record["code"])
    labels = sorted(set(demand) | set(stock), key=lambda label: (-demand[label], -stock[label], str(label).lower()))
    result = []
    for label in labels:
        leads = demand[label]
        stock_count = stock[label]
        demand_share = round(leads / demand_total * 100, 1) if demand_total else None
        supply_share = round(stock_count / stock_total * 100, 1) if stock_total else None
        result.append({
            "dimension": dimension, "segment": label, "label": label,
            "leads": leads, "demand_share_pct": demand_share,
            "stock_sucre": stock_count, "supply_share_pct": supply_share,
            "gap_pp": round(demand_share - supply_share, 1) if demand_share is not None and supply_share is not None else None,
            "properties_with_demand": len(demand_codes[label]),
            "leads_per_property": round(leads / len(demand_codes[label]), 2) if demand_codes[label] else None,
        })
        result[-1]["geo_breakdown"] = _demand_capture_segment_geo_breakdown(
            result[-1], stock_records, demand_records, dimension, demand_total, stock_total
        )
    return result


def _demand_capture_segment_geo_breakdown(
    row: Mapping[str, Any],
    stock_records: list[dict],
    demand_records: list[dict],
    dimension: str,
    demand_total: int,
    stock_total: int,
) -> dict:
    """Return an auditable geographic drilldown for one homogeneous segment.

    The segment row remains the primary contract.  This optional payload is only
    a presentation drilldown and is computed from the same property/lead records,
    so it cannot invent a second denominator.
    """
    value_key = "bedrooms_segment" if dimension == "bedrooms" else dimension
    value = row.get("segment")
    demand = [record for record in demand_records if record.get(value_key) == value]
    stock = [record for record in stock_records if record.get(value_key) == value and record.get("is_active_sucre")]

    def aggregate(field: str, records: list[dict], stock_scope: list[dict] | None = None) -> list[dict]:
        scoped_stock = stock if stock_scope is None else stock_scope
        counts = Counter(record.get(field) for record in records if record.get(field) not in (None, ""))
        codes = defaultdict(set)
        for record in records:
            label = record.get(field)
            if label not in (None, ""):
                codes[label].add(record.get("code"))
        stock_counts = Counter(record.get(field) for record in scoped_stock if record.get(field) not in (None, ""))
        output = []
        for label, leads in counts.most_common():
            stock_count = stock_counts.get(label, 0)
            share = round(leads / demand_total * 100, 1) if demand_total else None
            supply_share = round(stock_count / stock_total * 100, 1) if stock_total else None
            output.append({
                "name": label,
                "geo_key": _demand_capture_geo_key(label),
                "leads": leads,
                "demand_share_pct": share,
                "properties_with_demand": len(codes[label]),
                "stock_sucre": stock_count,
                "supply_share_pct": supply_share,
                "gap_pp": round(share - supply_share, 1) if share is not None and supply_share is not None else None,
                "leads_per_property": round(leads / len(codes[label]), 2) if codes[label] else None,
            })
        return output

    components = {}
    for complementary in ("bedrooms", "type", "operation", "price_range"):
        if complementary == dimension:
            continue
        key = "bedrooms_segment" if complementary == "bedrooms" else complementary
        counts = Counter(record.get(key) for record in demand if record.get(key) not in (None, ""))
        total = sum(counts.values())
        components[complementary] = [{"segment": label, "leads": count, "share_pct": round(count / total * 100, 1) if total else 0} for label, count in counts.most_common(5)]
        if components[complementary]:
            break
    regions = aggregate("region", demand)
    communes = aggregate("commune", demand)
    demand_by_region = defaultdict(list)
    stock_by_region = defaultdict(list)
    for record in demand:
        region_key = record.get("region_geo_key") or _demand_capture_geo_key(record.get("region"))
        if region_key:
            demand_by_region[region_key].append(record)
    for record in stock:
        region_key = record.get("region_geo_key") or _demand_capture_geo_key(record.get("region"))
        if region_key:
            stock_by_region[region_key].append(record)
    communes_by_region = {
        region_key: aggregate("commune", region_records, stock_by_region.get(region_key, []))
        for region_key, region_records in sorted(demand_by_region.items())
    }
    return {
        "regions": regions,
        "communes": communes,
        "communes_by_region": communes_by_region,
        "components": components,
    }


def _demand_capture_quadrant(rows: list[dict]) -> dict:
    demand_cut = _demand_capture_median([row["leads"] for row in rows]) or 0
    supply_cut = _demand_capture_median([row["stock_sucre"] for row in rows]) or 0
    for row in rows:
        high_demand = row["leads"] >= demand_cut if rows else False
        high_supply = row["stock_sucre"] >= supply_cut if rows else False
        if high_demand and not high_supply:
            quadrant = "Oportunidad de captación"
        elif high_demand and high_supply:
            quadrant = "Cobertura estratégica"
        elif not high_demand and high_supply:
            quadrant = "Sobreexposición relativa"
        else:
            quadrant = "Menor señal actual"
        row["quadrant"] = quadrant
    return {"demand_threshold": demand_cut, "supply_threshold": supply_cut, "rule": "mediana de la distribución visible por dimensión"}


def _demand_capture_combined_rows(
    stock_records: list[dict],
    demand_records: list[dict],
    dimensions: tuple[str, ...],
    demand_total: int,
    stock_total: int,
) -> list[dict]:
    """Build homogeneous segment rows; each row has the same attribute grain."""
    valid_demand = [
        record for record in demand_records
        if all(record.get("bedrooms_segment" if dimension == "bedrooms" else dimension) not in (None, "") for dimension in dimensions)
    ]
    valid_stock = [
        record for record in stock_records
        if all(record.get("bedrooms_segment" if dimension == "bedrooms" else dimension) not in (None, "") for dimension in dimensions)
    ]
    def key_for(record: dict) -> tuple:
        return tuple(record.get("bedrooms_segment" if dimension == "bedrooms" else dimension) for dimension in dimensions)
    demand = Counter(key_for(record) for record in valid_demand)
    demand_codes = defaultdict(set)
    stock = Counter()
    for record in valid_demand:
        demand_codes[key_for(record)].add(record["code"])
    for record in valid_stock:
        if record.get("is_active_sucre"):
            stock[key_for(record)] += 1
    keys = sorted(set(demand) | set(stock), key=lambda key: (-demand[key], -stock[key], tuple(str(value).lower() for value in key)))
    rows = []
    for key in keys:
        leads = demand[key]
        stock_count = stock[key]
        observed = len(demand_codes[key])
        demand_share = round(leads / demand_total * 100, 1) if demand_total else None
        supply_share = round(stock_count / stock_total * 100, 1) if stock_total else None
        rows.append({
            "dimension": "combined",
            "segment_key": " · ".join(str(value) for value in key),
            **dict(zip(dimensions, key)),
            "leads": leads,
            "properties_observed": observed,
            "demand_share_pct": demand_share,
            "stock_sucre": stock_count,
            "supply_share_pct": supply_share,
            "gap_pp": round(demand_share - supply_share, 1) if demand_share is not None and supply_share is not None else None,
            "leads_per_property": round(leads / observed, 2) if observed else None,
        })
        for dimension in ("operation", "type", "region", "zone_rm", "commune", "price_range", "bedrooms"):
            rows[-1].setdefault(dimension, None)
    return rows


def _demand_capture_support_rules(level1_rows: list[dict], min_observations: int) -> dict:
    """Derive support gates from the observed level-1 distribution and expose them."""
    positive = [row for row in level1_rows if row["leads"] > 0]
    median_leads = _demand_capture_median([row["leads"] for row in positive]) or float(min_observations)
    median_observed = _demand_capture_median([row["properties_observed"] for row in positive]) or float(min_observations)
    median_stock = _demand_capture_median([row["stock_sucre"] for row in positive]) or float(min_observations)
    lead_gate = max(min_observations, 5, int(math.ceil(median_leads * 0.25)))
    observed_gate = max(3, int(math.ceil(median_observed * 0.25)))
    stock_gate = max(3, int(math.ceil(median_stock * 0.10)))
    return {
        "level_1": {"min_leads": lead_gate, "min_properties_observed": observed_gate, "min_stock_sucre": stock_gate},
        "level_2": {"min_leads": max(lead_gate, 8), "min_properties_observed": observed_gate, "min_stock_sucre": stock_gate},
        "level_3": {"min_leads": max(lead_gate * 2, 10), "min_properties_observed": max(observed_gate + 1, 4), "min_stock_sucre": stock_gate},
        "no_stock": {"min_leads": max(lead_gate * 2, 10), "min_properties_observed": observed_gate},
        "derivation": "medianas de segmentos combinados operación+tipo+comuna; leads=max(5, ceil(25% mediana)); propiedades=max(3, ceil(25% mediana)); stock=max(3, ceil(10% mediana)). Nivel 2/3 exige mayor evidencia.",
        "distribution": {"level_1_positive_segments": len(positive), "median_leads": median_leads, "median_properties_observed": median_observed, "median_stock_sucre": median_stock},
    }


def _demand_capture_supported(row: dict, rule: dict, allow_no_stock: bool = True) -> bool:
    if row["leads"] < rule["min_leads"] or row["properties_observed"] < rule["min_properties_observed"]:
        return False
    return allow_no_stock and row["stock_sucre"] == 0 or row["stock_sucre"] >= rule["min_stock_sucre"]


def _demand_capture_combined_opportunities(
    stock_records: list[dict],
    demand_records: list[dict],
    demand_total: int,
    stock_total: int,
    min_observations: int,
) -> tuple[list[dict], dict]:
    level1_dimensions = ("operation", "type", "commune")
    level1 = _demand_capture_combined_rows(stock_records, demand_records, level1_dimensions, demand_total, stock_total)
    rules = _demand_capture_support_rules(level1, min_observations)
    selected = []
    for parent in level1:
        if parent["gap_pp"] is None or parent["gap_pp"] <= 0 or not _demand_capture_supported(parent, rules["level_1"]):
            continue
        parent_key = tuple(parent[dimension] for dimension in level1_dimensions)
        child_demand = [row for row in demand_records if tuple(row.get(dimension) for dimension in level1_dimensions) == parent_key]
        child_stock = [row for row in stock_records if tuple(row.get(dimension) for dimension in level1_dimensions) == parent_key]
        level2 = _demand_capture_combined_rows(child_stock, child_demand, level1_dimensions + ("price_range",), demand_total, stock_total)
        qualified_level2 = [row for row in level2 if row["gap_pp"] is not None and row["gap_pp"] > 0 and _demand_capture_supported(row, rules["level_2"])]
        if not qualified_level2:
            parent["support_level"] = "level_1"
            selected.append(parent)
            continue
        for candidate in qualified_level2:
            candidate_key = tuple(candidate[dimension] for dimension in level1_dimensions + ("price_range",))
            grandchild_demand = [row for row in child_demand if tuple(row.get(dimension) for dimension in level1_dimensions + ("price_range",)) == candidate_key]
            grandchild_stock = [row for row in child_stock if tuple(row.get(dimension) for dimension in level1_dimensions + ("price_range",)) == candidate_key]
            level3 = _demand_capture_combined_rows(grandchild_stock, grandchild_demand, level1_dimensions + ("price_range", "bedrooms"), demand_total, stock_total)
            qualified_level3 = [row for row in level3 if row["gap_pp"] is not None and row["gap_pp"] > 0 and _demand_capture_supported(row, rules["level_3"])]
            chosen = qualified_level3 or [candidate]
            for row in chosen:
                row["support_level"] = "level_3" if qualified_level3 else "level_2"
                selected.append(row)
    selected.sort(key=lambda row: (-row["gap_pp"], -row["leads"], -row["properties_observed"], -(row["leads_per_property"] or 0), row["segment_key"].lower()))
    for row in selected:
        row["recommendation"] = "Priorizar captación"
        row["evidence"] = {"gap_pp": row["gap_pp"], "leads": row["leads"], "properties_observed": row["properties_observed"], "intensity": row["leads_per_property"], "support_level": row["support_level"]}
    return selected, rules


def _demand_capture_datetime(lead: Mapping[str, Any]) -> datetime | None:
    value = lead.get("created_at")
    if value in (None, ""):
        return None
    try:
        return coerce_utc_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _demand_capture_period_metrics(
    records: list[dict],
    period_end: datetime,
) -> dict:
    by_window = [0, 0, 0]
    for record in records:
        dt = record.get("created_at")
        if not dt:
            continue
        age = (period_end - dt).total_seconds() / 86400
        if 0 <= age < 30:
            by_window[0] += 1
        elif 30 <= age < 60:
            by_window[1] += 1
        elif 60 <= age < 90:
            by_window[2] += 1
    w0, w1, w2 = by_window
    if w0 >= 5 and w0 > w1 > w2:
        trend = "Aceleración"
    elif w0 >= 5 and w0 > w1 and w1 <= w2:
        trend = "Reactivación"
    elif w0 >= 5 and w0 < w1:
        trend = "Desaceleración"
    elif w0 >= 5 and max(by_window) - min(by_window) <= max(2, round(max(by_window) * 0.25)):
        trend = "Estable"
    else:
        trend = "Insuficiente"
    return {"w0_leads": w0, "w1_leads": w1, "w2_leads": w2, "trend": trend, "windows_with_signal": sum(value > 0 for value in by_window)}


def _demand_capture_recent_band(w0_leads: int, historical_leads: int, historical_properties: int) -> str:
    """Classify observed recent volume; this is not a probability or forecast."""
    if historical_leads < 5 or historical_properties < 3:
        return "Sin evidencia suficiente"
    if w0_leads >= 5:
        return "Demanda reciente alta"
    if w0_leads >= 1:
        return "Demanda reciente media"
    return "Demanda reciente baja"


def _demand_capture_historical_metrics(
    row: dict,
    records: list[dict],
    first_global: datetime | None,
    last_global: datetime | None,
    period_end: datetime,
) -> dict:
    level_dimensions = {
        "level_1": ("operation", "type", "commune"),
        "level_2": ("operation", "type", "commune", "price_range"),
        "level_3": ("operation", "type", "commune", "price_range", "bedrooms"),
    }.get(row.get("support_level"), ("operation", "type", "commune"))
    key = tuple(row.get(dimension) for dimension in level_dimensions)
    matched = [record for record in records if tuple(record.get(dimension) for dimension in level_dimensions) == key and record.get("created_at")]
    dates = [record["created_at"] for record in matched]
    weeks = {(dt - timedelta(days=dt.weekday())).date().isoformat() for dt in dates}
    months = {(dt.year, dt.month) for dt in dates}
    total_weeks = ((last_global.date() - first_global.date()).days // 7 + 1) if first_global and last_global else 0
    total_months = ((last_global.year - first_global.year) * 12 + last_global.month - first_global.month + 1) if first_global and last_global else 0
    recent = _demand_capture_period_metrics(matched, period_end)
    historical_total = len(matched)
    observed = len({record["code"] for record in matched})
    signal_months = len(months)
    if historical_total < 5 or observed < 3:
        persistence = "Insuficiente"
    elif signal_months >= 3 and total_months and signal_months / total_months >= 0.5:
        persistence = "Persistente"
    elif signal_months >= 2 or len(weeks) >= 4:
        persistence = "Recurrente"
    elif recent["w0_leads"] > 0:
        persistence = "Reciente"
    else:
        persistence = "Esporádica"
    return {
        "historical_leads_total": historical_total,
        "historical_properties_with_demand": observed,
        "first_demand_at": min(dates).isoformat() if dates else None,
        "last_demand_at": max(dates).isoformat() if dates else None,
        "weeks_with_demand": len(weeks),
        "months_with_demand": signal_months,
        "total_weeks_observable": total_weeks,
        "total_months_observable": total_months,
        "weeks_signal_pct": round(len(weeks) / total_weeks * 100, 1) if total_weeks else None,
        "months_signal_pct": round(signal_months / total_months * 100, 1) if total_months else None,
        "leads_avg_per_week_with_signal": round(historical_total / len(weeks), 2) if weeks else None,
        "leads_avg_per_month_with_signal": round(historical_total / signal_months, 2) if signal_months else None,
        "recent_30_share_of_recent_90_pct": round(recent["w0_leads"] / sum((recent["w0_leads"], recent["w1_leads"], recent["w2_leads"])) * 100, 1) if sum((recent["w0_leads"], recent["w1_leads"], recent["w2_leads"])) else None,
        "persistence": persistence,
        "persistence_definition": "Persistente: >=3 meses y >=50% de meses observables; Recurrente: >=2 meses o >=4 semanas; Reciente: señal actual sin profundidad; Esporádica: evidencia aislada; Insuficiente: <5 leads históricos o <3 propiedades.",
        "recency": recent,
    }


def _capture_simulation_property_profile(doc: Mapping[str, Any], code: str) -> dict:
    profile = _demand_capture_profile(doc, code)
    operation_data = doc.get("tipo_operacion") or {}
    features = doc.get("caracteristicas") or {}
    operation = profile["operation"]
    price_data = operation_data.get("precio_venta" if operation == "Venta" else "precio_arriendo") or {}
    price = price_data.get("precio_uf" if operation == "Venta" else "precio_clp") or price_data.get("uf" if operation == "Venta" else "clp")
    def number(value):
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None
    return profile | {
        "price_value": number(price),
        "bathrooms": number(features.get("banos")),
        "surface": number(features.get("superficie_util") or features.get("superficie_construida") or features.get("superficie_total")),
        "is_active_sucre": bool(doc.get("disponible_prop360")) and (doc.get("estado") or {}).get("oficina") == DEMAND_CAPTURE_CANONICAL_OFFICE,
    }


def _capture_simulation_quantile(values: list[float], fraction: float) -> float | None:
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def _capture_simulation_match(property_profile: dict, simulation: dict, price_bands: dict) -> tuple[str | None, list[str]]:
    reasons = []
    if property_profile["operation"] != simulation["operation"]:
        return None, reasons
    if property_profile["type"].casefold() != simulation["type"].casefold():
        return None, reasons
    if property_profile["commune"].casefold() != simulation["commune"].casefold():
        return None, reasons
    price = simulation.get("price")
    property_price = property_profile.get("price_value")
    exact_band = price_bands.get("exact", 0)
    close_band = price_bands.get("close", exact_band)
    if price is not None and property_price is not None and abs(property_price - price) <= exact_band:
        price_level = "exact"
        reasons.append("precio dentro de banda exacta")
    elif price is not None and property_price is not None and abs(property_price - price) <= close_band:
        price_level = "close"
        reasons.append("precio dentro de banda cercana")
    elif price is None or property_price is None:
        price_level = "segment"
        reasons.append("precio no disponible")
    else:
        price_level = "segment"
        reasons.append("segmento operativo compatible")
    bedrooms = simulation.get("bedrooms")
    property_bedrooms = property_profile.get("bedrooms_value")
    bedroom_level = "exact"
    if bedrooms is not None and property_bedrooms is not None:
        if property_bedrooms == bedrooms:
            reasons.append("dormitorios exactos")
        elif abs(property_bedrooms - bedrooms) <= 1:
            bedroom_level = "close"
            reasons.append("dormitorios ±1")
        else:
            bedroom_level = "segment"
    elif bedrooms is not None:
        bedroom_level = "segment"
        reasons.append("dormitorios no disponibles")
    optional_level = "exact"
    for field, tolerance, label in (("bathrooms", 1, "baños"), ("surface", max((simulation.get("surface") or 0) * 0.20, 20), "superficie")):
        requested = simulation.get(field)
        available = property_profile.get(field)
        if requested is None:
            continue
        if available is None:
            optional_level = "segment"
            reasons.append(f"{label} no disponible")
        elif abs(available - requested) > tolerance:
            optional_level = "segment"
        else:
            reasons.append(f"{label} compatible")
    levels = {"exact": 0, "close": 1, "segment": 2}
    level = max(price_level, bedroom_level, optional_level, key=lambda item: levels[item])
    return level, reasons


def build_capture_simulation_contract(dataset: Mapping[str, Any], params: Mapping[str, Any]) -> dict:
    """Build a read-only, explainable capture simulation from a cached batch dataset."""
    operation = str(params.get("operation") or "").strip()
    property_type = str(params.get("type") or "").strip()
    commune = str(params.get("commune") or "").strip()
    if operation not in ("Venta", "Arriendo") or not property_type or not commune:
        return {"available": False, "error": "Operación, tipo y comuna son obligatorios.", "strategic_fit": {"status": "undefined"}, "predicted_demand_30d": None}
    def numeric(value):
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None
    simulation = {"operation": operation, "type": property_type, "commune": commune, "price": numeric(params.get("price")), "bedrooms": int(numeric(params.get("bedrooms"))) if numeric(params.get("bedrooms")) is not None else None, "bathrooms": numeric(params.get("bathrooms")), "surface": numeric(params.get("surface"))}
    properties = [_capture_simulation_property_profile(doc, code) for code, doc in dataset.get("properties", {}).items()]
    historical_leads = dataset.get("leads", [])
    candidate_properties = [item for item in properties if item["operation"] == operation and item["type"].casefold() == property_type.casefold() and item["commune"].casefold() == commune.casefold()]
    optional_coverage = {field: round(sum(item.get(field) is not None for item in candidate_properties) / len(candidate_properties) * 100, 1) if candidate_properties else 0.0 for field in ("bathrooms", "surface")}
    for field in ("bathrooms", "surface"):
        if simulation.get(field) is not None and optional_coverage[field] < 60:
            simulation[field] = None
    prices = [item.get("price_value") for item in candidate_properties if item.get("price_value") is not None]
    q1 = _capture_simulation_quantile(prices, 0.25)
    q3 = _capture_simulation_quantile(prices, 0.75)
    iqr = (q3 - q1) if q1 is not None and q3 is not None else 0
    price = simulation.get("price")
    exact_band = max((price or 0) * 0.10, iqr * 0.5)
    close_band = max(exact_band, iqr)
    match_levels = {}
    reasons_by_code = {}
    for item in candidate_properties:
        level, reasons = _capture_simulation_match(item, simulation, {"exact": exact_band, "close": close_band})
        if level:
            match_levels[item["code"]] = level
            reasons_by_code[item["code"]] = reasons
    # Select the narrowest auditable evidence tier.  A territorial reference is
    # only used when the requested commune has no compatible properties at all;
    # it is never silently mixed with exact/close/segment matches.
    quality = "exact" if any(level == "exact" for level in match_levels.values()) else "close" if any(level == "close" for level in match_levels.values()) else "segment" if match_levels else None
    relaxation = []
    if quality == "exact":
        matched_codes = {code for code, level in match_levels.items() if level == "exact"}
    elif quality == "close":
        matched_codes = {code for code, level in match_levels.items() if level in ("exact", "close")}
        relaxation.append("precio o característica opcional ampliada a una banda cercana")
    elif quality == "segment":
        matched_codes = set(match_levels)
        relaxation.append("se relajó al segmento operación + tipo + comuna")
    else:
        context = next((item for item in properties if item["commune"].casefold() == commune.casefold()), None)
        context_region = context.get("region") if context else None
        context_zone = _demand_capture_zone_rm(context_region, commune)
        territorial_candidates = [
            item for item in properties
            if item["operation"] == operation
            and item["type"].casefold() == property_type.casefold()
            and ((context_zone and item.get("zone_rm") == context_zone) or (context_region and item.get("region") == context_region) or not context_region)
        ]
        if territorial_candidates:
            quality = "territorial"
            relaxation.append("no hubo evidencia exacta en la comuna; se amplió a referencia territorial comparable")
            match_levels = {item["code"]: "segment" for item in territorial_candidates}
            reasons_by_code = {item["code"]: ["referencia territorial ampliada"] for item in territorial_candidates}
            matched_codes = {item["code"] for item in territorial_candidates}
        else:
            matched_codes = set()
    selected_properties = [item for item in properties if item["code"] in matched_codes]
    leads_by_code = Counter(lead["code"] for lead in historical_leads if lead["code"] in matched_codes)
    matched_leads = [lead for lead in historical_leads if lead["code"] in matched_codes]
    end = dataset.get("period_end")
    period_end = end if isinstance(end, datetime) else datetime.now(timezone.utc)
    windows = [[], [], []]
    for lead in matched_leads:
        if not lead.get("created_at"):
            continue
        age = (period_end - lead["created_at"]).total_seconds() / 86400
        if 0 <= age < 30:
            windows[0].append(lead)
        elif 30 <= age < 60:
            windows[1].append(lead)
        elif 60 <= age < 90:
            windows[2].append(lead)
    w0, w1, w2 = (len(window) for window in windows)
    if w0 >= 5 and w0 > w1 > w2:
        trend = "Aceleración"
    elif w0 >= 5 and w0 > w1 and w1 <= w2:
        trend = "Reactivación"
    elif w0 >= 5 and w0 < w1:
        trend = "Desaceleración"
    elif w0 >= 5 and max(w0, w1, w2) - min(w0, w1, w2) <= max(2, round(max(w0, w1, w2) * 0.25)):
        trend = "Estable"
    else:
        trend = "Insuficiente"
    historical_dates = [lead["created_at"] for lead in matched_leads if lead.get("created_at")]
    weeks = {(dt - timedelta(days=dt.weekday())).date().isoformat() for dt in historical_dates}
    months = {(dt.year, dt.month) for dt in historical_dates}
    active_matched = [item for item in selected_properties if item["is_active_sucre"]]
    current_demand_codes = {lead["code"] for lead in windows[0]}
    current_stock_total = dataset.get("active_sucre_total", 0)
    recent_total = dataset.get("recent_leads_total", 0)
    demand_share = round(w0 / recent_total * 100, 1) if recent_total else None
    supply_share = round(len(active_matched) / current_stock_total * 100, 1) if current_stock_total else None
    gap = round(demand_share - supply_share, 1) if demand_share is not None and supply_share is not None else None
    coverage = round(len(current_demand_codes & {item["code"] for item in active_matched}) / len(active_matched) * 100, 1) if active_matched else None
    intensity = round(w0 / len(current_demand_codes), 2) if current_demand_codes else None
    classification = _demand_capture_recent_band(w0, len(matched_leads), len({lead["code"] for lead in matched_leads}))
    comparables = []
    for item in sorted(selected_properties, key=lambda x: (-leads_by_code[x["code"]], {"exact": 0, "close": 1, "segment": 2}[match_levels[x["code"]]], x["code"]))[:5]:
        comparables.append({"code": item["code"], "type": item["type"], "commune": item["commune"], "operation": item["operation"], "price": item.get("price_value"), "price_unit": "UF" if operation == "Venta" else "CLP", "bedrooms": item.get("bedrooms_value"), "leads_historical": leads_by_code[item["code"]], "first_demand_at": min((lead["created_at"] for lead in matched_leads if lead["code"] == item["code"] and lead.get("created_at")), default=None).isoformat() if any(lead["code"] == item["code"] and lead.get("created_at") for lead in matched_leads) else None, "last_demand_at": max((lead["created_at"] for lead in matched_leads if lead["code"] == item["code"] and lead.get("created_at")), default=None).isoformat() if any(lead["code"] == item["code"] and lead.get("created_at") for lead in matched_leads) else None, "active_current": item["is_active_sucre"], "match_level": match_levels[item["code"]]})
    evidence_text = (f"Se observaron {len(matched_leads)} leads históricos compatibles en {len({lead['code'] for lead in matched_leads})} propiedades, incluyendo {w0} en los últimos 30 días. La demanda reciente presenta una señal de {trend.lower()} y Procasa Sucre mantiene {len(active_matched)} comparables activos.") if matched_leads else "No se encontraron propiedades históricas compatibles con estos parámetros."
    if quality == "territorial":
        evidence_text += " La coincidencia corresponde a una referencia territorial ampliada y no a un comparable exacto."
    return {"available": True, "inputs": simulation, "matching": {"quality": quality or "insufficient", "properties_used": len(matched_codes), "relaxation": relaxation, "exact_properties": sum(level == "exact" for level in match_levels.values()), "close_properties": sum(level == "close" for level in match_levels.values()), "segment_properties": sum(level == "segment" for level in match_levels.values()), "optional_field_coverage_pct": optional_coverage, "price_tolerance": {"exact": exact_band, "close": close_band, "basis": "máximo entre 10% del precio simulado y IQR observado en la comuna/tipo/operación"}}, "evidence": {"classification": classification, "classification_definition": "Banda de demanda observada: alta >=5 leads W0; media 1-4 leads W0; baja 0 leads W0; sin evidencia suficiente si histórico <5 leads o <3 propiedades.", "historical_leads_compatible": len(matched_leads), "historical_properties_with_demand": len({lead["code"] for lead in matched_leads}), "leads_30d": w0, "leads_60d": w0 + w1, "leads_90d": w0 + w1 + w2, "w0": w0, "w1": w1, "w2": w2, "trend": trend, "first_demand_at": min(historical_dates).isoformat() if historical_dates else None, "last_demand_at": max(historical_dates).isoformat() if historical_dates else None, "weeks_with_demand": len(weeks), "months_with_demand": len(months), "intensity": intensity, "stock_active_comparable": len(active_matched), "coverage_pct": coverage, "demand_share_pct": demand_share, "supply_share_pct": supply_share, "gap_pp": gap, "text": evidence_text}, "potentially_compatible_leads": {"historical": len(matched_leads), "last_30_days": w0, "definition": "compatibilidad histórica observada; no implica intención de compra", "quality": quality or "insufficient", "relaxation": relaxation}, "comparables": comparables, "strategic_fit": {"status": "undefined", "label": "No definido"}, "predicted_demand_30d": None, "forecast_status": "not_published", "no_write": True}


def build_demand_capture_contract(
    property_docs: list[Mapping[str, Any]],
    lead_docs: list[Mapping[str, Any]],
    period_start: str | None = None,
    period_end: str | None = None,
    filters: Mapping[str, Any] | None = None,
    min_observations: int = 3,
    network_summary: Mapping[str, Any] | None = None,
) -> dict:
    """Deterministic demand/capture intelligence built from one property batch and one lead batch."""
    filters = {key: str(value).strip() for key, value in (filters or {}).items() if value not in (None, "")}
    period_start_utc, period_end_utc = _build_chile_period_bounds(period_start, period_end)
    property_by_code = {}
    records = []
    duplicate_codes = 0
    for doc in property_docs:
        code = _inventory_code(doc.get("codigo"))
        if not code:
            continue
        if code in property_by_code:
            duplicate_codes += 1
            continue
        profile = _demand_capture_profile(doc, code)
        state = doc.get("estado") or {}
        property_by_code[code] = {"doc": doc, "profile": profile, "office": state.get("oficina"), "active": bool(doc.get("disponible_prop360")) and profile["operation"] in ("Venta", "Arriendo", "Venta + Arriendo")}
    scope_active = {code for code, item in property_by_code.items() if item["office"] == DEMAND_CAPTURE_CANONICAL_OFFICE and item["active"]}
    scope_records = []
    for code, item in property_by_code.items():
        profile = item["profile"]
        if item["office"] == DEMAND_CAPTURE_CANONICAL_OFFICE:
            record = dict(profile, is_active_sucre=code in scope_active, is_demand=False, code=code)
            scope_records.append(record)

    demand_leads = []
    historical_demand_leads = []
    attributed = 0
    historical_attributed = 0
    period_total_leads = 0
    office_counts = Counter()
    lead_counts_by_code = Counter()
    signal_counts = Counter()
    has_dated_leads = any(_demand_capture_datetime(lead) is not None for lead in lead_docs)
    for lead in lead_docs:
        raw_code = _inventory_code((lead.get("prospecto") or {}).get("codigo"))
        lead_dt = _demand_capture_datetime(lead)
        in_period = (not has_dated_leads) if lead_dt is None else period_start_utc <= lead_dt < period_end_utc
        if in_period:
            period_total_leads += 1
        item = property_by_code.get(raw_code)
        if not item or not item["office"]:
            continue
        historical_attributed += 1
        if in_period:
            office_counts[item["office"]] += 1
        if item["office"] != DEMAND_CAPTURE_CANONICAL_OFFICE:
            continue
        profile = dict(item["profile"])
        profile["code"] = raw_code
        profile["created_at"] = lead_dt
        historical_demand_leads.append(profile)
        if not in_period:
            continue
        attributed += 1
        profile["is_demand"] = True
        profile["is_active_sucre"] = raw_code in scope_active
        demand_leads.append(profile)
        lead_counts_by_code[raw_code] += 1
        lifecycle = lead.get("lifecycle") or {}
        if lifecycle.get("first_effective_contact_at"):
            signal_counts["contact_effective"] += 1
        if lifecycle.get("visit_scheduled_at"):
            signal_counts["visit"] += 1
    total_leads = len(demand_leads)
    for record in scope_records:
        record["is_demand"] = lead_counts_by_code.get(record["code"], 0) > 0
    stock_docs = [property_by_code[code]["doc"] for code in scope_active]
    inventory = build_properties_inventory_contract(stock_docs, lead_counts_by_code, period_start, period_end, filters)
    filtered_scope = []
    aliases = {"property_type": "type"}
    for record in scope_records:
        if any(record.get(aliases.get(key, key)) != value for key, value in filters.items() if aliases.get(key, key) in DEMAND_CAPTURE_DIMENSIONS or key == "responsible"):
            continue
        filtered_scope.append(record)
    # Recompute all demand/stock surfaces against the selected property filters.
    filtered_codes = {record["code"] for record in filtered_scope}
    filtered_demand = [record for record in demand_leads if record["code"] in filtered_codes]
    filtered_historical_demand = [record for record in historical_demand_leads if record["code"] in filtered_codes]
    demand_total = len(filtered_demand)
    stock_total = sum(record.get("is_active_sucre") for record in filtered_scope)
    dimensions = {}
    all_dimension_rows = []
    for dimension in DEMAND_CAPTURE_DIMENSIONS:
        rows = _demand_capture_distribution(filtered_scope, filtered_demand, dimension, demand_total, stock_total)
        dimensions[dimension] = rows
        all_dimension_rows.extend(rows)
    quadrant_rules = {}
    for dimension in DEMAND_CAPTURE_DIMENSIONS:
        quadrant_rules[dimension] = _demand_capture_quadrant(dimensions[dimension])
    opportunities, support_rules = _demand_capture_combined_opportunities(
        filtered_scope, filtered_demand, demand_total, stock_total, min_observations
    )
    historical_dates = [record["created_at"] for record in filtered_historical_demand if record.get("created_at")]
    first_global = min(historical_dates) if historical_dates else None
    last_global = max(historical_dates) if historical_dates else None
    for row in opportunities:
        row.update(_demand_capture_historical_metrics(row, filtered_historical_demand, first_global, last_global, period_end_utc))
        row["strategic_fit"] = {"status": "undefined", "reason": "No existe configuración institucional explícita de Procasa Sucre."}
        row["recommendation"] = _demand_capture_recent_band(row["recency"]["w0_leads"], row["historical_leads_total"], row["historical_properties_with_demand"])
        row["analytical_recommendation"] = "Candidata a captación basada en evidencia"
    if network_summary is None:
        active_network = [item["profile"] | {"office": item["office"]} for item in property_by_code.values() if item["active"] and item["office"] and item["office"] != DEMAND_CAPTURE_CANONICAL_OFFICE]
        network_offices = Counter(item["office"] for item in active_network)
        network_composition = {
            dimension: [{"segment": label, "count": count} for label, count in sorted(Counter(item[dimension] for item in active_network).items(), key=lambda item: (-item[1], item[0]))[:10]]
            for dimension in ("operation", "type", "commune")
        }
    else:
        network_offices = Counter(network_summary.get("offices") or {})
        network_composition = {
            dimension: [{"segment": str(label), "count": int(count)} for label, count in sorted((network_summary.get("composition", {}).get(dimension) or {}).items(), key=lambda item: (-int(item[1]), str(item[0])))[:10]]
            for dimension in ("operation", "type", "commune")
        }
    filtered_active_codes = {record["code"] for record in filtered_scope if record.get("is_active_sucre")}
    with_demand = sum(1 for code in filtered_active_codes if lead_counts_by_code.get(code, 0) > 0)
    attribution_total = period_total_leads
    attribution_coverage = round(attributed / attribution_total * 100, 1) if attribution_total else None
    geography = {}
    for dimension in ("region", "commune"):
        demand_only = _demand_capture_geo_row(filtered_demand, dimension, demand_total, stock_total)
        stock_only = Counter(_demand_capture_geo_key(record.get(dimension)) for record in filtered_scope if record.get("is_active_sucre"))
        by_key = {row["geo_key"]: row for row in demand_only}
        for key, stock_count in stock_only.items():
            if not key:
                continue
            row = by_key.setdefault(key, {"name": next((record.get(dimension) for record in filtered_scope if _demand_capture_geo_key(record.get(dimension)) == key), key), "geo_key": key, "leads": 0, "demand_share_pct": round(0, 1) if demand_total else None, "properties_with_demand": 0, "leads_per_property": None, "stock_sucre": 0, "supply_share_pct": None, "gap_pp": None})
            row["stock_sucre"] = stock_count
            row["supply_share_pct"] = round(stock_count / stock_total * 100, 1) if stock_total else None
            row["gap_pp"] = round((row["demand_share_pct"] or 0) - row["supply_share_pct"], 1) if row["demand_share_pct"] is not None and row["supply_share_pct"] is not None else None
        for row in by_key.values():
            matching = [record for record in filtered_demand if _demand_capture_geo_key(record.get(dimension)) == row["geo_key"]]
            row["top_segments"] = _demand_capture_territorial_segments(matching)
        geography[dimension] = sorted(by_key.values(), key=lambda row: (-row["leads"], -row["stock_sucre"], row["name"].casefold()))
    geography["metric_rule"] = {"default": "leads", "leads": "Leads últimos 30 días del período seleccionado", "intensity": "Leads divididos por propiedades distintas consultadas", "gap": "Participación de demanda menos participación de stock activo Procasa Sucre", "scale": "Cuantiles robustos P10/P90 de los territorios con datos; los extremos se recortan para evitar que un outlier aplaste la escala."}
    geography["matching"] = {"normalization": "minúsculas, sin tildes, espacios y alias oficiales de regiones; Región Metropolitana/Metropolitana y O'Higgins se unifican.", "territories_with_data": sum(bool(row.get("geo_key")) for dimension in ("region", "commune") for row in geography[dimension]), "unmatched_territories": None, "coverage_method": "calculada al asociar el payload con los GeoJSON locales cargados por la UI"}
    return {
        "meta": {"source": "universo_cartera_prop360 + leads", "canonical_office_field": "estado.oficina", "canonical_office": DEMAND_CAPTURE_CANONICAL_OFFICE, "period_start": period_start, "period_end": period_end, "stock_period_independent": True, "filters": filters, "mongo_reads": 3},
        "inventory": inventory["inventory"],
        "inventory_context": inventory,
        "attribution": {"leads_total": attribution_total, "leads_with_identifiable_office": attributed, "coverage_pct": attribution_coverage, "historical_leads_total": len(lead_docs), "historical_leads_with_identifiable_office": historical_attributed, "by_office": [{"office": office, "leads": count} for office, count in office_counts.most_common()], "method": "lead → prospecto.codigo → universo_cartera_prop360 → estado.oficina"},
        "demand": {"definition": "Demanda = leads", "leads": demand_total, "properties_with_demand": with_demand, "properties_with_demand_observed": len({record["code"] for record in filtered_demand}), "coverage_pct": round(with_demand / len(filtered_active_codes) * 100, 1) if filtered_active_codes else None, "lead_counts_by_property": dict(lead_counts_by_code), "qualified_signals": {"available": bool(signal_counts), "counts": dict(signal_counts), "definition": "Se muestran separadas; no se mezclan con leads"}},
        "demand_intelligence": {"dimensions": dimensions, "geography": geography, "quadrant_rules": quadrant_rules, "support_rules": support_rules, "territorial_mapping": {zone: list(communes) for zone, communes in DEMAND_CAPTURE_RM_ZONE_MAPPING.items()}, "historical": {"first_lead_at": first_global.isoformat() if first_global else None, "last_lead_at": last_global.isoformat() if last_global else None, "total_weeks_observable": ((last_global.date() - first_global.date()).days // 7 + 1) if first_global and last_global else 0, "total_months_observable": ((last_global.year - first_global.year) * 12 + last_global.month - first_global.month + 1) if first_global and last_global else 0, "definition": "Demanda histórica = todos los leads atribuibles a códigos Procasa Sucre, activos o inactivos."}},
        "simulator_options": {"operations": sorted({record["operation"] for record in scope_records if record.get("operation") in ("Venta", "Arriendo")}), "types": sorted({record["type"] for record in scope_records if record.get("type")}), "communes": sorted({record["commune"] for record in scope_records if record.get("commune")})},
        "opportunities": opportunities[:20],
        "benchmark": {"label": "Benchmark Red Procasa · oferta", "demand_comparison_available": bool(attribution_coverage is not None and attribution_coverage >= 95), "offices_active_stock": [{"office": office, "active": count} for office, count in network_offices.most_common()], "composition": network_composition, "note": "No mezcla stock de otras oficinas en los KPI de Procasa Sucre."},
        "rotation": {"available": False, "label": "Tiempo observado hasta salida", "reason": "No existe first_seen persistido de forma completa; ultima_version_at no permite afirmar fecha de alta."},
        "forecast": {"available": False, "reason": "Histórico real disponible desde enero de 2026; se requiere mayor profundidad temporal y holdout posterior estable para publicar forecast de 30 días.", "status": "pipeline_not_published"},
        "data_quality": {
            "duplicate_property_codes": duplicate_codes,
            "active_sucre": len(scope_active),
            "all_sucre_historical": sum(item["office"] == DEMAND_CAPTURE_CANONICAL_OFFICE for item in property_by_code.values()),
            "price_dimension": {"included": sum(record.get("price_range") is not None for record in scope_records), "excluded_missing_price": sum(record.get("operation") in ("Venta", "Arriendo") and record.get("price_range") is None for record in scope_records), "definition": "Solo precios observables; Venta se expresa en UF y Arriendo en CLP."},
            "bedrooms_dimension": {"included": sum(record.get("bedrooms_segment") is not None for record in scope_records), "excluded_missing_or_non_residential": sum(record.get("is_residential") and record.get("bedrooms_segment") is None for record in scope_records), "excluded_non_residential": sum(not record.get("is_residential") for record in scope_records), "definition": "Solo Casa y Departamento con dormitorios observables; la tipología queda en la etiqueta."},
            "region_dimension": {"included": sum(record.get("region") is not None for record in scope_records), "missing": sum(record.get("region") is None for record in scope_records)},
            "zone_rm_dimension": {"included": sum(record.get("zone_rm") is not None for record in scope_records), "excluded_outside_rm_or_unmapped": sum(record.get("zone_rm") is None for record in scope_records), "mapping": {zone: list(communes) for zone, communes in DEMAND_CAPTURE_RM_ZONE_MAPPING.items()}},
        },
    }


def _demand_capture_network_summary(db) -> dict:
    """Aggregate network benchmark dimensions without transporting properties."""
    pipeline = [{"$match": {
        "disponible_prop360": True,
        "estado.oficina": {"$nin": [None, "", DEMAND_CAPTURE_CANONICAL_OFFICE]},
        "codigo": {"$nin": [None, ""]},
        "$or": [
            {"tipo_operacion.venta": True},
            {"tipo_operacion.arriendo": True},
        ],
    }}, {"$project": {
        "codigo": 1,
        "estado.oficina": 1,
        "tipo_operacion.tipo": 1,
        "tipo_operacion.venta": 1,
        "tipo_operacion.arriendo": 1,
        "ubicacion.comuna": 1,
    }}, {"$group": {
        # The Python contract canonicalizes numeric/string representations
        # through _inventory_code before deduplication.
        "_id": {"$toString": "$codigo"},
        "doc": {"$first": "$$ROOT"},
    }}, {"$replaceWith": "$doc"}, {"$facet": {
        "offices": [{"$group": {"_id": "$estado.oficina", "count": {"$sum": 1}}}],
        # Operation normalization only consumes tipo/venta/arriendo. Grouping
        # the complete subdocument would create one raw row per price value.
        "operation": [{"$group": {"_id": {
            "tipo": "$tipo_operacion.tipo",
            "venta": "$tipo_operacion.venta",
            "arriendo": "$tipo_operacion.arriendo",
        }, "count": {"$sum": 1}}}],
        "type": [{"$group": {"_id": "$tipo_operacion.tipo", "count": {"$sum": 1}}}],
        "commune": [{"$group": {"_id": "$ubicacion.comuna", "count": {"$sum": 1}}}],
    }}]
    rows = list(db["universo_cartera_prop360"].aggregate(pipeline))
    result = rows[0] if rows and isinstance(rows[0], Mapping) else {}
    summary = {"offices": {}, "composition": {"operation": {}, "type": {}, "commune": {}}, "_rows": 0, "_bson_bytes": 0}
    for facet_rows in result.values():
        if isinstance(facet_rows, list):
            summary["_rows"] += len(facet_rows)
            summary["_bson_bytes"] += sum(len(BSON.encode(row)) for row in facet_rows if isinstance(row, Mapping))
    for row in result.get("offices", []):
        if row.get("_id") not in (None, ""):
            summary["offices"][str(row["_id"]).strip()] = int(row.get("count") or 0)
    for dimension in ("operation", "type", "commune"):
        for row in result.get(dimension, []):
            raw = row.get("_id")
            if dimension == "operation":
                profile = _demand_capture_profile({"tipo_operacion": raw})
                label = profile["operation"]
            elif dimension == "type":
                profile = _demand_capture_profile({"tipo_operacion": {}, "tipo_propiedad": raw})
                label = profile["type"]
            else:
                profile = _demand_capture_profile({"tipo_operacion": {}, "ubicacion": {"comuna": raw}})
                label = profile["commune"] or "Sin comuna"
            summary["composition"][dimension][label] = summary["composition"][dimension].get(label, 0) + int(row.get("count") or 0)
    return summary


def _demand_capture_load_leads(db) -> tuple[list[Mapping[str, Any]], set[str]]:
    """Load the reusable lead projection and derive referenced property codes."""
    lead_pipeline = [
        _normalized_created_at_stage(),
        {"$match": _build_test_lead_exclusion_match()},
        {"$project": {
            "created_at": 1,
            "prospecto.codigo": 1,
            "lifecycle.first_effective_contact_at": 1,
            "lifecycle.visit_scheduled_at": 1,
        }},
    ]
    lead_docs = list(db["leads"].aggregate(lead_pipeline))
    lead_codes = {
        _inventory_code((lead.get("prospecto") or {}).get("codigo"))
        for lead in lead_docs
    }
    lead_codes.discard(None)
    return lead_docs, lead_codes


def _query_demand_capture_base(timing: dict | None = None) -> dict:
    """Load the period-independent demand dataset with a single-flight cache."""
    global _DEMAND_CAPTURE_BASE_CACHE
    now = time.time()
    if _DEMAND_CAPTURE_BASE_CACHE and now - _DEMAND_CAPTURE_BASE_CACHE["created_at"] < _DEMAND_CAPTURE_BASE_TTL:
        if timing is not None:
            timing["demand_capture.base_cache"] = "HIT"
        return _DEMAND_CAPTURE_BASE_CACHE["data"]
    with _DEMAND_CAPTURE_BASE_LOCK:
        now = time.time()
        if _DEMAND_CAPTURE_BASE_CACHE and now - _DEMAND_CAPTURE_BASE_CACHE["created_at"] < _DEMAND_CAPTURE_BASE_TTL:
            if timing is not None:
                timing["demand_capture.base_cache"] = "HIT"
            return _DEMAND_CAPTURE_BASE_CACHE["data"]
        db = get_db()
        base_started = time.perf_counter()
        leads_started = time.perf_counter()
        if timing is not None:
            timing["demand_capture.timeout_stage"] = "leads_aggregate"
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="demand-capture") as executor:
            leads_future = executor.submit(_demand_capture_load_leads, db)
            benchmark_started = time.perf_counter()
            benchmark_future = executor.submit(_demand_capture_network_summary, db)
            lead_docs, lead_codes = leads_future.result()
            leads_finished = time.perf_counter()
            property_projection = {
                "codigo": 1, "disponible_prop360": 1, "estado.oficina": 1,
                "estado.ejecutivo": 1, "estado.captador": 1, "estado.responsable": 1,
                "tipo_operacion": 1, "ubicacion.comuna": 1, "ubicacion.region": 1,
                "caracteristicas.dormitorios": 1,
            }
            # Only Sucre history and lead-referenced records travel to Python.
            property_filter = {"$or": [
                {"estado.oficina": DEMAND_CAPTURE_CANONICAL_OFFICE},
                {"codigo": {"$in": sorted(lead_codes)}},
            ]}
            properties_started = time.perf_counter()
            if timing is not None:
                timing["demand_capture.timeout_stage"] = "property_find"
                timing["demand_capture.property_find_ms"] = 0.0
            property_docs = list(
                db["universo_cartera_prop360"]
                .find(property_filter, property_projection)
                .batch_size(250)
            )
            detail_finished = time.perf_counter()
            if timing is not None:
                timing["demand_capture.property_find_ms"] = round((time.perf_counter() - properties_started) * 1000, 1)
            if timing is not None:
                timing["demand_capture.timeout_stage"] = "network_aggregate"
            network_summary = benchmark_future.result()
            network_finished = time.perf_counter()
            data = {"lead_docs": lead_docs, "property_docs": property_docs, "network_summary": network_summary,
                    "lead_codes": len(lead_codes), "base_docs": len(property_docs),
                    "base_leads_ms": round((leads_finished - leads_started) * 1000, 1),
                    "base_properties_ms": round((detail_finished - properties_started) * 1000, 1),
                    "base_network_ms": round((network_finished - benchmark_started) * 1000, 1),
                    "base_wall_ms": round((time.perf_counter() - base_started) * 1000, 1),
                    "base_lead_bson_bytes": sum(len(BSON.encode(doc)) for doc in lead_docs),
                    "base_property_bson_bytes": sum(len(BSON.encode(doc)) for doc in property_docs)}
        _DEMAND_CAPTURE_BASE_CACHE = {"created_at": time.time(), "data": data}
        if timing is not None:
            timing["demand_capture.base_cache"] = "MISS"
            timing["demand_capture.base_docs"] = len(property_docs)
            timing["demand_capture.base_network_aggregate"] = True
        return data


def query_demand_capture_dashboard(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    filters: Mapping[str, Any] | None = None,
    timing: dict | None = None,
) -> dict:
    """Read-only demand/capture payload in two batch reads.

    The property collection can contain a large historical network.  The
    contract only needs active network stock, all Sucre records, and records
    referenced by leads, so avoid an unbounded collection scan.
    """
    query_started = time.perf_counter()

    def record_stage(name: str, started: float) -> float:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        if timing is not None:
            timing[f"demand_capture.{name}_ms"] = duration_ms
        return duration_ms

    def record_total() -> None:
        if timing is not None:
            timing["demand_capture.total_ms"] = round((time.perf_counter() - query_started) * 1000, 1)

    try:
        base = _query_demand_capture_base(timing)
    except NetworkTimeout:
        if timing is not None:
            timing["demand_capture.total_ms"] = round((time.perf_counter() - query_started) * 1000, 1)
        raise
    lead_docs = base["lead_docs"]
    property_docs = base["property_docs"]
    if timing is not None:
        timing["demand_capture.leads_aggregate_ms"] = base["base_leads_ms"]
        timing["demand_capture.property_find_ms"] = base["base_properties_ms"]
        timing["demand_capture.network_aggregate_ms"] = base["base_network_ms"]
        timing["demand_capture.base_wall_ms"] = base["base_wall_ms"]
        timing["demand_capture.base_docs"] = base["base_docs"]
        timing["demand_capture.lead_codes_ms"] = 0.0

    stage_started = time.perf_counter()
    try:
        payload = build_demand_capture_contract(property_docs, lead_docs, period_start, period_end, filters, network_summary=base["network_summary"])
    except NetworkTimeout:
        duration_ms = record_stage("contract_build", stage_started)
        if timing is not None:
            timing["demand_capture.timeout_stage"] = "contract_build"
        record_total()
        logger.warning(
            "[DEMAND_CAPTURE] mongo_timeout timeout_stage=contract_build duration_ms=%.1f",
            duration_ms,
        )
        raise
    record_stage("contract_build", stage_started)
    record_total()
    logger.info(
        "[DEMAND_CAPTURE_TIMING] leads_aggregate_ms=%.1f lead_codes_ms=%.1f "
        "property_find_ms=%.1f contract_build_ms=%.1f total_ms=%.1f",
        timing.get("demand_capture.leads_aggregate_ms", 0.0) if timing else 0.0,
        timing.get("demand_capture.lead_codes_ms", 0.0) if timing else 0.0,
        timing.get("demand_capture.property_find_ms", 0.0) if timing else 0.0,
        timing.get("demand_capture.contract_build_ms", 0.0) if timing else 0.0,
        timing.get("demand_capture.total_ms", 0.0) if timing else round((time.perf_counter() - query_started) * 1000, 1),
    )
    return payload


def query_capture_simulation_dataset(period_end: Optional[str] = None) -> dict:
    """Read the complete historical simulation batch; no writes and no per-property queries."""
    db = get_db()
    lead_pipeline = [
        _normalized_created_at_stage(),
        {"$match": _build_test_lead_exclusion_match()},
        {"$project": {"created_at": 1, "prospecto.codigo": 1}},
    ]
    lead_docs = list(db["leads"].aggregate(lead_pipeline))
    lead_codes = {
        _inventory_code((lead.get("prospecto") or {}).get("codigo"))
        for lead in lead_docs
    }
    lead_codes.discard(None)
    projection = {
        "codigo": 1, "disponible_prop360": 1, "estado.oficina": 1,
        "tipo_operacion": 1, "tipo_propiedad": 1, "ubicacion.comuna": 1, "ubicacion.region": 1,
        "caracteristicas": 1,
    }
    property_filter = {"$or": [
        {"disponible_prop360": True},
        {"estado.oficina": DEMAND_CAPTURE_CANONICAL_OFFICE},
    ]}
    if lead_codes:
        property_filter["$or"].append({"codigo": {"$in": sorted(lead_codes)}})
    property_docs = list(
        db["universo_cartera_prop360"]
        .find(property_filter, projection)
        .batch_size(250)
    )
    properties = {}
    for doc in property_docs:
        code = _inventory_code(doc.get("codigo"))
        if code and (doc.get("estado") or {}).get("oficina") == DEMAND_CAPTURE_CANONICAL_OFFICE:
            properties[code] = doc
    parsed_leads = []
    for lead in lead_docs:
        code = _inventory_code((lead.get("prospecto") or {}).get("codigo"))
        dt = _demand_capture_datetime(lead)
        if code in properties and dt:
            parsed_leads.append({"code": code, "created_at": dt})
    end_utc = _build_chile_period_bounds(None, period_end)[1] if period_end else datetime.now(timezone.utc)
    recent_leads_total = sum(0 <= (end_utc - lead["created_at"]).total_seconds() / 86400 < 30 for lead in parsed_leads)
    active_total = sum(bool(doc.get("disponible_prop360")) for doc in properties.values())
    return {"properties": properties, "leads": parsed_leads, "period_end": end_utc, "active_sucre_total": active_total, "recent_leads_total": recent_leads_total, "mongo_reads": 2}


def query_leads_dashboard_rescue(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    filters: Optional[dict] = None,
) -> dict:
    """Rescate IA / bot para el Leads Dashboard (CARD 5).

    Cuenta los leads del período con RECUPERABILIDAD ALTA (candidatos a
    reenganche automático por IA en WhatsApp) y su distribución.
    """
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    pipeline = [
        _cohort_indexed_prefilter(start_utc, end_utc),
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(start_utc, end_utc, filters)},
        {"$group": {
            "_id": {"$ifNull": ["$bi_analytics_global.RECUPERABILIDAD", "SIN_CLASIFICAR"]},
            "count": {"$sum": 1},
        }},
    ]
    rows = list(db["leads"].aggregate(pipeline))
    counts = {str(r["_id"]): r["count"] for r in rows}
    return {
        "recuperabilidad_alta": counts.get("ALTA", 0),
        "distribucion": counts,
    }



def query_leads_dashboard_sources(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    comparison_start: Optional[str] = None,
    comparison_end: Optional[str] = None,
    include_comparison: bool = True,
    filters: Optional[dict] = None,
    timing: Optional[dict] = None,
    signed_orders: Optional[list] = None,
) -> dict:
    """Distribución por origen (SECCIÓN Origen de Demanda) con conversión a
    visita canónica y comparativa anterior.

    - Origen: _resolve_origin + _normalize_source_name (fusiona variantes).
    - Visitas por origen: la MISMA evidencia canónica de CARD 2
      (_scheduled_visit_lead_ids): stage/lifecycle histórico, crm_events
      VISITA_AGENDADA y órdenes de visita firmadas, deduplicado por lead, as-of.
    - Orden: leads DESC. Filas: Top 6 + Otros (agregando el resto); si hay
      <=7 orígenes se muestran todos sin "Otros".
    """
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)

    if include_comparison and comparison_start and comparison_end:
        prev_start, prev_end = _build_chile_period_bounds(comparison_start, comparison_end)
    else:
        prev_start = prev_end = None

    def _load(ps_utc, pe_utc, label):
        return _profile_query(timing, "leads", f"aggregate_sources_{label}", lambda: db["leads"].aggregate([
            _cohort_indexed_prefilter(ps_utc, pe_utc),
            _normalized_created_at_stage(),
            {"$match": _build_commercial_cohort_match(ps_utc, pe_utc, filters)},
            {"$project": {
                "_id": 1, "phone": 1,
                "prospecto.origen": 1, "prospecto.canal_origen": 1,
                "prospecto.codigo_mercadolibre": 1, "prospecto.codigo_yapo": 1,
                "prospecto.codigo": 1, "created_at": 1,
                "pipeline_stage": 1, "stage": 1, "stage_history": 1,
                "lifecycle.visit_scheduled_at": 1,
            }},
        ]))

    shared_orders = signed_orders
    if shared_orders is None:
        shared_orders = _profile_query(timing, "visitas", "find_signed_orders_shared", lambda: db["visitas"].find(
            {"status": {"$in": list(CANONICAL_SIGNED_ORDER_STATUSES)}},
            {"visita_code": 1, "phone": 1, "property_code": 1, "timeline": 1, "created_at": 1},
        ))
    cohort = _load(start_utc, end_utc, "current")
    lead_ids = {str(l["_id"]) for l in cohort}
    scheduled = _scheduled_visit_lead_ids(cohort, lead_ids, end_utc, timing, shared_orders)

    per_origin: dict = {}
    for lead in cohort:
        name = _normalize_source_name(_resolve_origin(lead))
        entry = per_origin.setdefault(name, {"leads": 0, "visitas": 0})
        entry["leads"] += 1
        if str(lead["_id"]) in scheduled:
            entry["visitas"] += 1

    prev_counts: dict = {}
    if prev_start:
        for lead in _load(prev_start, prev_end, "previous"):
            name = _normalize_source_name(_resolve_origin(lead))
            prev_counts[name] = prev_counts.get(name, 0) + 1

    current_total = sum(e["leads"] for e in per_origin.values())
    total_visitas = sum(e["visitas"] for e in per_origin.values())

    ordered = sorted(per_origin.items(), key=lambda kv: -kv[1]["leads"])
    if len(ordered) > 6:
        top = ordered[:5]
        rest = ordered[5:]
        items = [
            {"nombre": name, "cantidad": e["leads"], "visitas": e["visitas"],
             "prev": prev_counts.get(name, 0)}
            for name, e in top
        ]
        otros_leads = sum(e["leads"] for _, e in rest)
        otros_visitas = sum(e["visitas"] for _, e in rest)
        otros_prev = sum(prev_counts.get(name, 0) for name, _ in rest)
        items.append({"nombre": "Otros", "cantidad": otros_leads,
                      "visitas": otros_visitas, "prev": otros_prev})
    else:
        items = [
            {"nombre": name, "cantidad": e["leads"], "visitas": e["visitas"],
             "prev": prev_counts.get(name, 0)}
            for name, e in ordered
        ]

    for it in items:
        it["pct"] = round(it["cantidad"] / current_total * 100, 1) if current_total else 0.0
        it["conversion_pct"] = round(it["visitas"] / it["cantidad"] * 100, 1) if it["cantidad"] else None

    return {
        "current": items,
        "previous": prev_counts,
        "total": current_total,
        "total_visitas": total_visitas,
    }


def _resolve_origin(lead: Mapping[str, Any]) -> str:
    """Origen de un lead con la misma precedencia canónica de _origin_expr."""
    prospecto = lead.get("prospecto") or {}
    origen = str(prospecto.get("origen") or "").strip()
    if origen and origen.upper() != "WHATSAPP":
        return origen
    canal = str(prospecto.get("canal_origen") or "").strip()
    if canal:
        return canal
    if prospecto.get("codigo_mercadolibre"):
        return "MercadoLibre"
    if prospecto.get("codigo_yapo"):
        return "Yapo"
    if origen.upper() == "WHATSAPP":
        return "WhatsApp"
    return "Sin informacion"


def _normalize_source_name(value) -> str:
    """Normaliza nombres de canal/origen de captura para el dashboard.

    Fusiona variantes (Portal Inmobiliario/PortalInmobiliario, TocToc/TOCTOC).
    La ausencia de información se presenta como "Sin información" (no se
    disfraza de origen "Directo"). e2e_test se mantiene como tal (política de
    test global pendiente), sin convertirlo en "Directo".
    """
    text = str(value or "").strip()
    low = text.lower()
    if low in {"", "sin informacion", "sin información", "sin info", "n/a", "null", "none", "desconocido", "-", "no informado"}:
        return "Sin información"
    mapping = {
        "portal inmobiliario": "Portal Inmobiliario",
        "portalinmobiliario": "Portal Inmobiliario",
        "portal inmobiliario (mlc code)": "Portal Inmobiliario",
        "mercadolibre": "MercadoLibre",
        "toctoc": "TocToc",
        "otro portal": "Otro Portal",
        "otro portal (mlc code)": "Otro Portal",
        "chilepropiedades": "ChilePropiedades",
        "sitio web": "Sitio Web",
        "otro": "Otro",
    }
    return mapping.get(low, text)


def _executive_as_of(cycles_by_lead: dict, lead_id: str, at) -> Optional[str]:
    """Ejecutivo responsable del lead al cierre del período (as-of).

    Regla: ciclo con assigned_at < period_end y
    (unassigned_at IS NULL OR unassigned_at >= period_end).
    Si hay múltiples ciclos candidatos (anomalía), se elige el ciclo vigente
    más reciente (mayor assigned_at) de forma determinística — NO el más largo.
    Si no existe ciclo activo al cierre -> None (Sin asignar).
    """
    candidates = []
    for c in cycles_by_lead.get(lead_id, []):
        assigned = coerce_utc_datetime(c.get("assigned_at"))
        if assigned is None or not (assigned < at):
            continue
        unassigned = coerce_utc_datetime(c.get("unassigned_at"))
        if unassigned is not None and unassigned < at:
            continue
        candidates.append((assigned, c))
    if not candidates:
        return None
    # Ciclo vigente más reciente al cierre.
    best = max(candidates, key=lambda item: item[0])[1]
    return best.get("assigned_to_display_name") or str(best.get("assigned_to_user_id"))


def query_leads_dashboard_executives(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    comparison_start: Optional[str] = None,
    comparison_end: Optional[str] = None,
    include_comparison: bool = True,
    filters: Optional[dict] = None,
    timing: Optional[dict] = None,
    signed_orders: Optional[list] = None,
) -> dict:
    """Rendimiento comercial por ejecutivo (BLOQUE 4).

    Mide el RESULTADO comercial de la cartera bajo responsabilidad de cada
    ejecutivo al cierre del período (crm_assignment_cycles as-of), no quién
    ejecutó la gestión.

    - Leads: cohort por created_at, agrupados por ejecutivo_as_of(period_end).
    - Visitas: exactamente el conjunto CARD 2 (Conversión a Visita Agendada
      as-of), atribuido al ejecutivo responsable del LEAD al cierre del período
      (nunca al actor del evento).
    - Conversión: visitas_as_of / leads_as_of del mismo universo.
    La comparación con el período anterior se reconstruye con la asignación
    as-of del cierre ANTERIOR (independiente).
    """
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    if include_comparison and comparison_start and comparison_end:
        prev_start, prev_end = _build_chile_period_bounds(comparison_start, comparison_end)
    else:
        prev_start = prev_end = None

    shared_orders = signed_orders
    if shared_orders is None:
        shared_orders = _profile_query(timing, "visitas", "find_signed_orders_shared", lambda: db["visitas"].find(
            {"status": {"$in": list(CANONICAL_SIGNED_ORDER_STATUSES)}},
            {"visita_code": 1, "phone": 1, "property_code": 1, "timeline": 1, "created_at": 1},
        ))

    def _cohort(ps, pe, label):
        pipe = [
            _cohort_indexed_prefilter(ps, pe),
            _normalized_created_at_stage(),
            {"$match": _build_commercial_cohort_match(ps, pe, filters)},
            {"$project": {"_id": 1, "created_at": 1, "phone": 1, "prospecto.codigo": 1,
                          "pipeline_stage": 1, "stage": 1, "stage_history": 1,
                          "lifecycle.visit_scheduled_at": 1}},
        ]
        leads = _profile_query(timing, "leads", f"aggregate_executives_{label}", lambda: db["leads"].aggregate(pipe))
        lead_ids = {str(l["_id"]) for l in leads}
        # ciclos de la cohorte
        cycles_by_lead: dict = {}
        cycles = _profile_query(timing, "crm_assignment_cycles", f"find_assignment_cycles_{label}", lambda: db["crm_assignment_cycles"].find(
            {"lead_id": {"$in": [l["_id"] for l in leads]}},
            {"lead_id": 1, "assigned_at": 1, "unassigned_at": 1,
             "assigned_to_display_name": 1, "assigned_to_user_id": 1},
        ))
        for c in cycles:
            cycles_by_lead.setdefault(str(c.get("lead_id")), []).append(c)
        # visitas CARD 2 as-of (misma definición aprobada)
        visitas = _scheduled_visit_lead_ids(leads, lead_ids, pe, timing, shared_orders)
        # ejecutivo responsable as-of por lead
        lead_exec = {}
        for l in leads:
            lid = str(l["_id"])
            lead_exec[lid] = _executive_as_of(cycles_by_lead, lid, pe) or "Sin Asignar"
        return leads, lead_exec, visitas

    cur_leads, cur_exec, cur_visitas = _cohort(start_utc, end_utc, "current")
    prev_leads, prev_exec, prev_visitas = _cohort(prev_start, prev_end, "previous") if prev_start else ([], {}, set())

    # Solo ejecutivos con rol agente y activos en la colección "usuarios"
    active = {
        str(u.get("nombre", "")).strip().lower()
        for u in db["usuarios"].find({"rol": "agente", "is_active": True}, {"nombre": 1})
    }
    # Excluir al administrador
    active.discard("Control Owner")

    from collections import defaultdict
    cur_leads_by_exec = defaultdict(int)
    cur_visitas_by_exec = defaultdict(int)
    for lid, exec_name in cur_exec.items():
        cur_leads_by_exec[exec_name] += 1
        if lid in cur_visitas:
            cur_visitas_by_exec[exec_name] += 1
    prev_leads_by_exec = defaultdict(int)
    prev_visitas_by_exec = defaultdict(int)
    for lid, exec_name in prev_exec.items():
        prev_leads_by_exec[exec_name] += 1
        if lid in prev_visitas:
            prev_visitas_by_exec[exec_name] += 1

    exec_names = set(cur_leads_by_exec) | set(prev_leads_by_exec)
    rows = []
    for name in exec_names:
        low = str(name).strip().lower()
        is_sin_asignar = low == "sin asignar"
        # Comerciales: agentes activos (o fila neutral Sin asignar).
        if not is_sin_asignar and low not in active:
            continue
        leads = cur_leads_by_exec.get(name, 0)
        citas = cur_visitas_by_exec.get(name, 0)
        prev_leads = prev_leads_by_exec.get(name, 0)
        prev_citas = prev_visitas_by_exec.get(name, 0)
        rows.append({
            "ejecutivo": name,
            "leads": leads,
            "leads_prev": prev_leads,
            "diff_leads": leads - prev_leads,
            "citas": citas,
            "citas_prev": prev_citas,
            "visitas_prev": prev_citas,
        })
    rows.sort(key=lambda r: (-r["citas"], -r["leads"]))

    # Reconciliación: categorías no comerciales
    otros_names = set(cur_leads_by_exec) | set(prev_leads_by_exec)
    otros_names = {n for n in otros_names
                   if str(n).strip().lower() not in active and str(n).strip().lower() != "sin asignar"}
    otros_leads = sum(cur_leads_by_exec.get(n, 0) for n in otros_names)
    otros_visitas = sum(cur_visitas_by_exec.get(n, 0) for n in otros_names)
    sin_asignar_leads = cur_leads_by_exec.get("Sin Asignar", 0)
    sin_asignar_visitas = cur_visitas_by_exec.get("Sin Asignar", 0)

    return {
        "rows": rows,
        "reconcile": {
            "total": len(cur_leads),
            "comerciales": sum(r["leads"] for r in rows if str(r["ejecutivo"]).strip().lower() != "sin asignar"),
            "sin_asignar": sin_asignar_leads,
            "otros": otros_leads,
            "otros_visitas": otros_visitas,
            "sin_asignar_visitas": sin_asignar_visitas,
            "total_visitas": len(cur_visitas),
            "comerciales_visitas": sum(r["citas"] for r in rows if str(r["ejecutivo"]).strip().lower() != "sin asignar"),
        },
    }


def query_leads_dashboard_funnel(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    filters: Optional[dict] = None,
    timing: Optional[dict] = None,
    signed_orders: Optional[list] = None,
    signed_orders_future=None,
) -> dict:
    """Embudo comercial (Recibidos → Gestionados → Contacto efectivo → Visita).

    - Recibidos: cohorte (created_at ∈ [period_start, period_end)).
    - Gestionados: lifecycle.first_valid_management_at con corte as-of.
    - Contacto efectivo: event_evidence()["effective_contact"] con corte as-of
      (misma clasificación canónica que usa SLA).
    - Visita agendada: MISMA definición de CARD 2 (reconcilia exactamente).
    - Transiciones por conjuntos de lead_id (intersecciones), nunca restas de
      cantidades. Se entregan excepciones de trazabilidad y hitos avanzados.
    Todas las etapas respetan: lead.created_at <= evidencia < period_end.
    """
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)

    lead_pipe = [
        _cohort_indexed_prefilter(start_utc, end_utc),
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(start_utc, end_utc, filters)},
        {"$project": {
            "_id": 1, "phone": 1, "created_at": 1,
            "prospecto.codigo": 1,
            "pipeline_stage": 1, "stage": 1, "stage_history": 1,
            "lifecycle.first_valid_management_at": 1,
            "lifecycle.visit_scheduled_at": 1,
        }},
    ]
    cohort = _profile_query(
        timing,
        "leads",
        "aggregate_funnel_cohort",
        lambda: db["leads"].aggregate(lead_pipe),
    )
    ids = {str(l["_id"]) for l in cohort}
    received = len(cohort)
    by_created = {lid: l.get("created_at") for lid, l in [(str(l["_id"]), l) for l in cohort]}
    created_utc = {lid: coerce_utc_datetime(l.get("created_at")) for lid, l in
                   [(str(l["_id"]), l) for l in cohort]}

    # ---- Gestionados (as-of) ----
    gestionados = set()
    for lid, l in [(str(l["_id"]), l) for l in cohort]:
        c = created_utc[lid]
        if c is None:
            continue
        mgmt = coerce_utc_datetime((l.get("lifecycle") or {}).get("first_valid_management_at"))
        if mgmt is not None and c <= mgmt < end_utc:
            gestionados.add(lid)

    # ---- Contacto efectivo (as-of, clasificación canónica) ----
    contacto_efectivo = set()
    if ids:
        effective_results = [
            "CONTACTADO", "SOLICITA_SEGUIMIENTO", "NO_INTERESADO",
            "EFFECTIVE_CONTACT", "FOLLOW_UP_REQUESTED",
            "REQUIERE_SEGUIMIENTO",
            "VISITA_AGENDADA", "visita_agendada",
            "contactado", "solicita_seguimiento", "no_interesado",
            "effective_contact", "follow_up_requested",
            "requiere_seguimiento",
        ]
        for event in _profile_query(timing, "crm_events", "find_funnel_contact_events", lambda: db["crm_events"].find(
            {
                "lead_id": {"$in": [l["_id"] for l in cohort]},
                "confirmed": True,
                "$or": [
                    {"result": {"$in": effective_results}},
                    {"meta.result": {"$in": effective_results}},
                    {"meta.contact_result": {"$in": effective_results}},
                ],
            },
            {"lead_id": 1, "result": 1, "meta": 1, "type": 1,
            "timestamp": 1, "confirmed": 1, "actor": 1, "actor_type": 1},
        )):
            if not event_evidence(event).get("effective_contact"):
                continue
            eid = str(event.get("lead_id"))
            if eid not in ids:
                continue
            c = created_utc.get(eid)
            if c is None:
                continue
            ts = coerce_utc_datetime(event.get("timestamp"))
            if ts is not None and c <= ts < end_utc:
                contacto_efectivo.add(eid)

    # ---- Visita agendada (definición CARD 2, reconcilia exactamente) ----
    visita_agendada = _scheduled_visit_lead_ids(
        cohort,
        ids,
        end_utc,
        profile=timing,
        signed_orders=signed_orders,
        signed_orders_future=signed_orders_future,
    )

    # ---- Hitos avanzados / cierres históricos as-of ----
    avanzados = set()
    cierres = set()
    for lid, l in [(str(l["_id"]), l) for l in cohort]:
        c = created_utc[lid]
        if c is None:
            continue
        for entry in l.get("stage_history") or []:
            to_stage = str((entry or {}).get("to") or "").upper()
            ts = coerce_utc_datetime((entry or {}).get("timestamp"))
            if ts is None or not (c <= ts < end_utc):
                continue
            if to_stage in {"OFFER", "NEGOTIATION", "CLOSED_WON"}:
                avanzados.add(lid)
            if to_stage == "CLOSED_WON":
                cierres.add(lid)

    # ---- Transiciones por conjuntos ----
    rec_gestion = len(gestionados & ids)          # = gestionados (⊆ cohort)
    gest_contacto = len(contacto_efectivo & gestionados)
    contacto_visita = len(contacto_efectivo & visita_agendada)

    def _pct(num, den):
        return round(num / den * 100, 1) if den else None

    stages = [
        {"key": "received", "label": "Recibidos", "count": received,
         "pct_of_received": 100.0, "transition_pct": None},
        {"key": "gestionados", "label": "Gestionados", "count": len(gestionados),
         "pct_of_received": _pct(len(gestionados), received),
         "transition_pct": _pct(rec_gestion, received)},
        {"key": "contacto_efectivo", "label": "Contacto efectivo", "count": len(contacto_efectivo),
         "pct_of_received": _pct(len(contacto_efectivo), received),
         "transition_pct": _pct(gest_contacto, len(gestionados))},
        {"key": "visita_agendada", "label": "Visita agendada", "count": len(visita_agendada),
         "pct_of_received": _pct(len(visita_agendada), received),
         "transition_pct": _pct(contacto_visita, len(contacto_efectivo))},
    ]

    return {
        "received": received,
        "stages": stages,
        "transitions": {
            "received_to_gestionados": rec_gestion,
            "gestionados_to_contacto_efectivo": gest_contacto,
            "contacto_efectivo_to_visita_agendada": contacto_visita,
        },
        "exceptions": {
            "visitas_sin_contacto_efectivo": len(visita_agendada - contacto_efectivo),
            "visitas_sin_gestion": len(visita_agendada - gestionados),
        },
        "hitos_excepcionales": {
            "avanzados_sin_visita": len(avanzados - visita_agendada),
            "cierres_sin_visita": len(cierres - visita_agendada),
            "avanzados": len(avanzados),
            "cierres": len(cierres),
        },
    }


def query_leads_dashboard_reconcile_breakdown(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    filters: Optional[dict] = None,
) -> dict:
    """Desglose de los leads NO contabilizados en la tabla de ejecutivos.

    Replica la regla de la tabla de ejecutivos (solo agentes activos) y
    clasifica el remanente en categorías de gobierno de datos:
    Pruebas, Duplicados, Rechazados, Sin asignar, Administrativo y Otros.
    El total del desglose debe reconciliar contra `reconcile.otros`.
    """
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)

    match: dict = {
        "created_at": {
            "$gte": start_utc.strftime("%Y-%m-%dT%H:%M:%S"),
            "$lt": end_utc.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    }
    extra = _build_extra_filter(filters)
    if extra:
        match.update(extra)

    docs = db["leads"].find(
        match,
        {
            "_test_lead": 1,
            "is_duplicate": 1,
            "archive_reason": 1,
            "ejecutivo_asignado": 1,
        },
    )

    active = {
        str(u.get("nombre", "")).strip().lower()
        for u in db["usuarios"].find({"rol": "agente", "is_active": True}, {"nombre": 1})
    }
    active.discard("Control Owner")
    known_users = {
        str(u.get("nombre", "")).strip().lower()
        for u in db["usuarios"].find({}, {"nombre": 1})
    }

    counters = {"Pruebas": 0, "Duplicados": 0, "Rechazados": 0,
                "Sin asignar": 0, "Administrativo": 0, "Otros": 0}
    agentes = 0
    total = 0
    for doc in docs:
        total += 1
        exec_name = str(doc.get("ejecutivo_asignado") or "").strip()
        exec_key = exec_name.lower()
        if exec_key in active:
            agentes += 1
            continue
        if doc.get("_test_lead") is True:
            counters["Pruebas"] += 1
        elif doc.get("is_duplicate") is True:
            counters["Duplicados"] += 1
        elif "rechazad" in str(doc.get("archive_reason") or "").lower():
            counters["Rechazados"] += 1
        elif exec_key in {"", "sin asignar", "no asignado"}:
            counters["Sin asignar"] += 1
        elif exec_key in {"Control Owner"} or (exec_key and exec_key not in known_users):
            counters["Administrativo"] += 1
        else:
            counters["Otros"] += 1

    items = [{"categoria": name, "cantidad": count} for name, count in counters.items() if count > 0]
    return {
        "agentes": agentes,
        "items": items,
        "total_desglose": sum(counters.values()),
        "total_cohort": total,
    }


def _ops_percentile(values: list[float], percentile: int) -> Optional[float]:
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, round((percentile / 100) * (len(ordered) - 1))))
    return round(ordered[index], 1)


def _ops_stats(values: list[float]) -> dict:
    ordered = sorted(float(value) for value in values if value is not None)
    return {"n": len(ordered), "min": ordered[0] if ordered else None,
            "q1": _ops_percentile(ordered, 25), "p50": _ops_percentile(ordered, 50),
            "q3": _ops_percentile(ordered, 75), "p90": _ops_percentile(ordered, 90),
            "max": ordered[-1] if ordered else None}


def _ops_unassigned(value: Any) -> bool:
    return value is None or str(value).strip().lower() in {
        str(item).strip().lower() for item in UNASSIGNED_VALUES if item is not None
    }


def _ops_filters(filters: Optional[dict]) -> tuple[dict, dict]:
    raw = dict(filters or {})
    if raw.get("assignment") == "assigned":
        raw["assignment"] = "1"
    elif raw.get("assignment") == "unassigned":
        raw["assignment"] = "0"
    python_only = {key: raw.pop(key, None) for key in ("priority", "search")}
    return _build_extra_filter(raw) or {}, {key: value for key, value in python_only.items() if value}


def query_operational_portfolios() -> list[dict]:
    """Captadores disponibles en la cartera canónica, en una sola lectura."""
    values = get_db()["universo_cartera"].distinct("ejecutivo", {"oficina": OPS_PORTFOLIO_OFFICE, "ejecutivo": {"$nin": [None, ""]}})
    names = sorted({str(value).strip() for value in values if str(value).strip()}, key=str.casefold)
    # El administrador no es un captador de cartera y no debe aparecer como
    # filtro comercial, aunque exista históricamente en la fuente.
    names = [name for name in names if name.casefold() != "agente" and not name.casefold().startswith("Control Owner") and name.casefold() != "administrador"]
    return [{"captador": name} for name in names]


def _ops_normalized_person_name(value: Any) -> str:
    import unicodedata
    return "".join(
        char for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    ).strip().casefold()


def query_leads_control_breakdown(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    executive: Optional[str] = None,
) -> dict:
    """Desglosa la cohorte asignada por cartera vigente y ciclos anteriores."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    period_cutoff = min(end_utc, datetime.now(timezone.utc))
    assigned_date = {
        "$convert": {
            "input": "$lifecycle.assigned_at",
            "to": "date",
            "onError": None,
            "onNull": None,
        }
    }
    cohort_parts = [{"$expr": {"$and": [
        {"$gte": [assigned_date, start_utc]},
        {"$lt": [assigned_date, period_cutoff]},
    ]}}]
    selected_executive = str(executive or "").strip()
    if selected_executive:
        cohort_parts.append({"ejecutivo_asignado": selected_executive})

    leads = list(db["leads"].aggregate([
        {"$match": {"$and": cohort_parts}},
        {"$project": {
            "_id": 1,
            "created_at": 1,
            "ejecutivo_asignado": 1,
            "lead_temperature_effective": 1,
            "pipeline_stage": 1,
            "stage": 1,
            "prospecto.codigo": 1,
            "prospecto.comuna": 1,
            "prospecto.origen": 1,
            "lifecycle.current_assignment_cycle_id": 1,
            "lifecycle.assigned_at": 1,
            "lifecycle.first_valid_management_at": 1,
            "lifecycle.first_effective_contact_at": 1,
            "lifecycle.visit_scheduled_at": 1,
            "management_status": 1,
            "visit_date": 1,
            "follow_up_required": 1,
            "next_follow_up_at": 1,
        }},
    ])) if start_utc < period_cutoff else []

    lead_ids = [lead.get("_id") for lead in leads if lead.get("_id") is not None]
    cycles_by_lead: dict[str, list[dict]] = defaultdict(list)
    if lead_ids:
        for cycle in db["crm_assignment_cycles"].find(
            {"lead_id": {"$in": lead_ids}},
            {"lead_id": 1, "assignment_cycle_id": 1, "assigned_to_display_name": 1,
             "assigned_at": 1, "cycle_status": 1, "assigned_by": 1,
             "reassignment_reason": 1, "temperature_at_assignment": 1,
             "sla_started_at": 1, "first_valid_management_at": 1,
             "first_contact_attempt_at": 1, "first_effective_contact_at": 1,
             "hot_started_at": 1,
             "source_cycle_sla_breached_at": 1, "previous_cycle_sla_breached_at": 1,
             "closed_reason": 1},
        ):
            cycles_by_lead[str(cycle.get("lead_id"))].append(cycle)

    reassigned = 0
    without_prior_reassignment = 0
    history_unavailable = 0
    previous_owner_counts: Counter = Counter()
    reassignment_reason_counts: Counter = Counter()
    temperature_counts: Counter = Counter()
    cycle_context_by_lead: dict[str, dict] = {}
    for lead in leads:
        lead_cycles = cycles_by_lead.get(str(lead.get("_id")), [])
        lead_cycles.sort(key=lambda cycle: (
            coerce_utc_datetime(cycle.get("assigned_at")) or datetime.min.replace(tzinfo=timezone.utc),
            str(cycle.get("assignment_cycle_id") or ""),
        ))
        lifecycle = lead.get("lifecycle") or {}
        current_cycle_id = str(lifecycle.get("current_assignment_cycle_id") or "")
        current_index = next((
            index for index, cycle in enumerate(lead_cycles)
            if current_cycle_id and str(cycle.get("assignment_cycle_id") or "") == current_cycle_id
        ), None)
        current_owner = str(lead.get("ejecutivo_asignado") or "").strip()
        if current_index is None:
            matching_active = [
                index for index, cycle in enumerate(lead_cycles)
                if cycle.get("cycle_status") == "active"
                and _ops_normalized_person_name(cycle.get("assigned_to_display_name"))
                == _ops_normalized_person_name(current_owner)
            ]
            if len(matching_active) == 1:
                current_index = matching_active[0]
        if current_index is None:
            history_unavailable += 1
            temperature = str(lead.get("lead_temperature_effective") or "").strip().upper()
            temperature_counts["hot" if temperature == "HOT" else "normal" if temperature in {"COLD", "NORMAL"} else "unknown"] += 1
            cycle_context_by_lead[str(lead.get("_id"))] = {"cycles": lead_cycles, "current": None, "previous": None}
            continue

        current_cycle = lead_cycles[current_index]
        temperature = str(
            current_cycle.get("temperature_at_assignment")
            or lead.get("lead_temperature_effective")
            or ""
        ).strip().upper()
        temperature_counts["hot" if temperature == "HOT" else "normal" if temperature in {"COLD", "NORMAL"} else "unknown"] += 1
        current_key = _ops_normalized_person_name(current_owner)
        previous_cycle = next((
            cycle for cycle in reversed(lead_cycles[:current_index])
            if _ops_normalized_person_name(cycle.get("assigned_to_display_name"))
            and _ops_normalized_person_name(cycle.get("assigned_to_display_name")) != current_key
        ), None)
        cycle_context_by_lead[str(lead.get("_id"))] = {
            "cycles": lead_cycles,
            "current": current_cycle,
            "previous": previous_cycle,
        }
        if previous_cycle:
            reassigned += 1
            previous_owner = str(previous_cycle.get("assigned_to_display_name") or "").strip()
            previous_owner_counts[previous_owner or "Sin nombre"] += 1
            reason = str(current_cycle.get("reassignment_reason") or "").strip()
            if reason == "sla_reassignment":
                reason_label = "SLA vencido"
            elif reason.startswith("POLICY_REPAIR_") or reason == "policy_repair":
                reason_label = "Corrección de asignación"
            elif reason:
                reason_label = reason.replace("_", " ").capitalize()
            else:
                reason_label = "Motivo no registrado"
            reassignment_reason_counts[reason_label] += 1
        else:
            without_prior_reassignment += 1

    code_by_lead = {
        str(lead.get("_id")): _inventory_code((lead.get("prospecto") or {}).get("codigo"))
        for lead in leads
    }
    property_codes = sorted({code for code in code_by_lead.values() if code})
    portfolio_by_code: dict[str, str] = {}
    if property_codes:
        for property_doc in db["universo_cartera_prop360"].find(
            {"codigo": {"$in": property_codes}},
            {"codigo": 1, "estado.ejecutivo": 1, "estado.captador": 1, "estado.responsable": 1},
        ):
            code = _inventory_code(property_doc.get("codigo"))
            if code:
                portfolio_by_code[code] = _inventory_responsible(property_doc)

    portfolio_counts: Counter = Counter()
    for code in code_by_lead.values():
        owner = portfolio_by_code.get(code) if code else None
        portfolio_counts[owner or "Sin atribución vigente"] += 1
    if selected_executive:
        portfolio_counts.setdefault(selected_executive, 0)
    portfolio_counts.setdefault("Office Portfolio Owner", 0)
    portfolio_rows = [
        {"owner": owner, "leads": int(count)}
        for owner, count in sorted(portfolio_counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    ]
    previous_owner_rows = [
        {"owner": owner, "leads": int(count)}
        for owner, count in sorted(previous_owner_counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    ]
    matched_leads = sum(1 for lead in leads if code_by_lead.get(str(lead.get("_id"))) in portfolio_by_code)

    outgoing_reassignment_filter = {
        "closed_reason": "sla_reassignment",
        "reassigned_at": {"$gte": start_utc, "$lt": period_cutoff},
    }
    if selected_executive:
        outgoing_reassignment_filter["assigned_to_display_name"] = selected_executive
    outgoing_sla = db["crm_assignment_cycles"].count_documents(outgoing_reassignment_filter) if start_utc < period_cutoff else 0

    all_cycle_ids = [
        cycle.get("assignment_cycle_id")
        for cycle_rows in cycles_by_lead.values()
        for cycle in cycle_rows
        if cycle.get("assignment_cycle_id")
    ]
    management_results_by_cycle: dict[str, list[dict]] = defaultdict(list)
    if all_cycle_ids:
        for result in db["crm_management_results"].find(
            {"assignment_cycle_id": {"$in": all_cycle_ids}},
            {"lead_id": 1, "assignment_cycle_id": 1, "result_type": 1,
             "occurred_at": 1, "status": 1, "details_json.visit_at": 1},
        ):
            if result.get("status") not in (None, "completed"):
                continue
            management_results_by_cycle[str(result.get("assignment_cycle_id"))].append(result)
    for result_rows in management_results_by_cycle.values():
        result_rows.sort(key=lambda item: coerce_utc_datetime(item.get("occurred_at")) or datetime.min.replace(tzinfo=timezone.utc))

    cycle_by_id = {
        str(cycle.get("assignment_cycle_id")): cycle
        for cycle_rows in cycles_by_lead.values()
        for cycle in cycle_rows
        if cycle.get("assignment_cycle_id")
    }
    events_by_lead: dict[str, list[dict]] = defaultdict(list)
    if lead_ids:
        for event in db["crm_events"].find(
            {"lead_id": {"$in": lead_ids}},
            {"lead_id": 1, "assignment_cycle_id": 1, "type": 1,
             "actor": 1, "actor_type": 1, "confirmed": 1, "result": 1,
             "meta.result": 1, "meta.contact_result": 1,
             "timestamp": 1, "occurred_at": 1},
        ):
            event_type = str(event.get("type") or "").upper()
            cycle_id = str(event.get("assignment_cycle_id") or "")
            cycle = cycle_by_id.get(cycle_id)
            assigned_at = cycle.get("assigned_at") if cycle else None
            evidence = event_evidence(event)
            outreach = registered_outreach_evidence(
                event,
                assigned_at=assigned_at,
                assignment_cycle_id=cycle_id or None,
                allow_historical_for_presentation=True,
            )
            if not (evidence.get("management") or evidence.get("contact_attempt")
                    or evidence.get("effective_contact") or outreach.get("recognized")):
                continue
            occurred_at = coerce_utc_datetime(event.get("timestamp") or event.get("occurred_at"))
            events_by_lead[str(event.get("lead_id"))].append({
                "assignment_cycle_id": cycle_id or None,
                "executive": str((cycle or {}).get("assigned_to_display_name") or "Ciclo sin ejecutivo identificado"),
                "event_type": event_type,
                "occurred_at": occurred_at.isoformat() if occurred_at else None,
                "result": normalize_result(event.get("result") or (event.get("meta") or {}).get("result") or (event.get("meta") or {}).get("contact_result")),
                "evidence": "Gestión acreditada" if evidence.get("management") else "Intento de contacto" if evidence.get("contact_attempt") else "Contacto efectivo" if evidence.get("effective_contact") else "Contacto registrado",
            })
    for event_rows in events_by_lead.values():
        event_rows.sort(key=lambda item: item.get("occurred_at") or "")

    now_utc = datetime.now(timezone.utc)
    lead_rows: list[dict] = []
    commune_counts: dict[str, dict] = defaultdict(lambda: {"leads": 0, "hot": 0, "normal": 0, "visits": 0, "sla_within": 0, "sla_late": 0})
    source_counts: Counter = Counter()
    no_response_results = {"NO_RESPONDIO", "CALL_NO_ANSWER", "MENSAJE_ENVIADO", "MESSAGE_SENT_WAITING_RESPONSE", "EMAIL_SENT"}
    for lead in leads:
        lead_key = str(lead.get("_id"))
        context = cycle_context_by_lead.get(lead_key) or {"cycles": cycles_by_lead.get(lead_key, []), "current": None, "previous": None}
        current_cycle = context.get("current") or {}
        previous_cycle = context.get("previous") or {}
        lifecycle = lead.get("lifecycle") or {}
        temperature = str(current_cycle.get("temperature_at_assignment") or lead.get("lead_temperature_effective") or "").strip().upper()
        temperature_label = "HOT" if temperature == "HOT" else "NORMAL" if temperature in {"COLD", "NORMAL"} else "Sin clasificar"
        assigned_at = coerce_utc_datetime(current_cycle.get("sla_started_at") or current_cycle.get("assigned_at") or lifecycle.get("assigned_at"))
        first_management = coerce_utc_datetime(current_cycle.get("first_valid_management_at") or lifecycle.get("first_valid_management_at"))
        sla = calculate_sla(
            assigned_at=assigned_at,
            first_valid_management_at=first_management,
            now=now_utc,
            temperature=temperature,
            hot_started_at=current_cycle.get("hot_started_at"),
        )
        if sla.get("coverage") == "insufficient_data":
            sla_state = "No evaluable"
        elif first_management:
            sla_state = "Dentro del plazo" if sla.get("canonical_state") == "MANAGED_WITHIN_SLA" else "Vencido"
        elif sla.get("canonical_state") == "BREACHED":
            sla_state = "Vencido"
        elif sla.get("canonical_state") in {"NEAR_BREACH", "ATTENTION"}:
            sla_state = "Por vencer"
        else:
            sla_state = "Pendiente"

        code = code_by_lead.get(lead_key, "")
        portfolio_owner = portfolio_by_code.get(code) if code else None
        commune = str(((lead.get("prospecto") or {}).get("comuna") or lead.get("comuna") or "")).strip() or "Sin comuna registrada"
        source = str(((lead.get("prospecto") or {}).get("origen") or lead.get("origin") or "")).strip() or "Sin origen registrado"
        current_owner = str(lead.get("ejecutivo_asignado") or "Sin asignar").strip()
        is_reassigned = bool(previous_cycle)
        reason = str(current_cycle.get("reassignment_reason") or "").strip()
        if reason == "sla_reassignment":
            reason_label = "SLA vencido"
        elif reason.startswith("POLICY_REPAIR_") or reason == "policy_repair":
            reason_label = "Corrección de asignación"
        elif reason:
            reason_label = reason.replace("_", " ").capitalize()
        elif is_reassigned:
            reason_label = "Motivo no registrado"
        else:
            reason_label = "No aplica"
        cycle_id = str(current_cycle.get("assignment_cycle_id") or "")
        management_rows = management_results_by_cycle.get(cycle_id, [])
        latest_result = management_rows[-1] if management_rows else None
        latest_result_type = normalize_result((latest_result or {}).get("result_type"))
        visit_at = coerce_utc_datetime(
            lifecycle.get("visit_scheduled_at")
            or lead.get("visit_date")
            or ((latest_result or {}).get("details_json") or {}).get("visit_at")
        )
        alert_list = []
        if temperature_label == "HOT" and not first_management:
            alert_list.append("hot_no_first_contact")
        if sla_state == "Vencido":
            alert_list.append("sla_overdue")
        if reason_label == "SLA vencido":
            alert_list.append("reassigned_sla")
        if latest_result_type in no_response_results:
            alert_list.append("no_response")
        if latest_result_type == "VISIT_SCHEDULED" and visit_at and visit_at > now_utc:
            alert_list.append("visit_pending")

        cycle_history = []
        for cycle in context.get("cycles") or []:
            cycle_date = coerce_utc_datetime(cycle.get("assigned_at"))
            cycle_reason = str(cycle.get("reassignment_reason") or "").strip()
            cycle_history.append({
                "executive": str(cycle.get("assigned_to_display_name") or "Sin nombre"),
                "assigned_at": cycle_date.isoformat() if cycle_date else None,
                "assigned_by": str(cycle.get("assigned_by") or ""),
                "reassignment_reason": cycle_reason,
                "prior_cycle_sla_breached_at": coerce_utc_datetime(cycle.get("source_cycle_sla_breached_at") or cycle.get("previous_cycle_sla_breached_at")).isoformat() if (cycle.get("source_cycle_sla_breached_at") or cycle.get("previous_cycle_sla_breached_at")) else None,
                "is_current": cycle is current_cycle,
            })
        cycle_history.sort(key=lambda item: item.get("assigned_at") or "")
        management_history = []
        for cycle in context.get("cycles") or []:
            for result in management_results_by_cycle.get(str(cycle.get("assignment_cycle_id") or ""), []):
                result_at = coerce_utc_datetime(result.get("occurred_at"))
                management_history.append({
                    "occurred_at": result_at.isoformat() if result_at else None,
                    "result": normalize_result(result.get("result_type")),
                    "assignment_cycle_id": str(result.get("assignment_cycle_id") or ""),
                    "executive": str(cycle.get("assigned_to_display_name") or "Ciclo sin ejecutivo identificado"),
                })
        management_history.sort(key=lambda item: item.get("occurred_at") or "")

        row = {
            "lead_id": lead_key,
            "created_at": coerce_utc_datetime(lead.get("created_at")).isoformat() if coerce_utc_datetime(lead.get("created_at")) else None,
            "assigned_at": coerce_utc_datetime(lifecycle.get("assigned_at")).isoformat() if coerce_utc_datetime(lifecycle.get("assigned_at")) else None,
            "property_code": code or "Sin código",
            "commune": commune,
            "temperature": temperature_label,
            "source": source,
            "portfolio_owner": portfolio_owner or "Sin atribución vigente",
            "executive": current_owner,
            "assignment_type": "Reasignado" if is_reassigned else "Sin reasignación previa" if context.get("current") else "Historial no disponible",
            "previous_owner": str(previous_cycle.get("assigned_to_display_name") or "") or "Sin ejecutivo anterior",
            "reassignment_reason": reason_label,
            "first_response_business_minutes": round(float(sla.get("hot_minutes") if temperature_label == "HOT" and sla.get("hot_minutes") is not None else sla.get("minutes") or 0), 1) if first_management else None,
            "sla_state": sla_state,
            "sla_business_minutes": round(float(sla.get("hot_minutes") if temperature_label == "HOT" and sla.get("hot_minutes") is not None else sla.get("minutes") or 0), 1),
            "commercial_state": str(lead.get("pipeline_stage") or lead.get("stage") or lead.get("management_status") or "Sin estado").strip(),
            "latest_result": latest_result_type or "Sin resultado reciente",
            "visit_at": visit_at.isoformat() if visit_at else None,
            "alerts": alert_list,
            "assignment_history": cycle_history,
            "management_history": management_history,
            "communication_history": events_by_lead.get(lead_key, []),
        }
        lead_rows.append(row)
        source_counts[source] += 1
        commune_row = commune_counts[commune]
        commune_row["leads"] += 1
        commune_row["hot" if temperature_label == "HOT" else "normal" if temperature_label == "NORMAL" else "unknown"] += 1
        commune_row["visits"] += int(latest_result_type == "VISIT_SCHEDULED")
        commune_row["sla_within"] += int(sla_state == "Dentro del plazo")
        commune_row["sla_late"] += int(sla_state == "Vencido")
    lead_rows.sort(key=lambda item: item.get("assigned_at") or "", reverse=True)
    commune_rows = [
        {"commune": commune, **values}
        for commune, values in sorted(commune_counts.items(), key=lambda item: (-item[1]["leads"], item[0].casefold()))
    ]
    source_rows = [
        {"source": source, "leads": int(count)}
        for source, count in sorted(source_counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    ]
    alert_definitions = [
        ("hot_no_first_contact", "HOT sin primer contacto"),
        ("sla_overdue", "Leads con SLA vencido"),
        ("reassigned_sla", "Reasignados por vencimiento de SLA"),
        ("no_response", "Leads sin respuesta del cliente"),
        ("visit_pending", "Visitas futuras agendadas"),
    ]
    alerts = [
        {
            "code": code,
            "label": label,
            "count": sum(code in row.get("alerts", []) for row in lead_rows),
        }
        for code, label in alert_definitions
    ]

    return {
        "total": len(leads),
        "temperature_mix": {
            "hot": int(temperature_counts.get("hot", 0)),
            "normal": int(temperature_counts.get("normal", 0)),
            "unknown": int(temperature_counts.get("unknown", 0)),
        },
        "portfolio": {
            "source": "universo_cartera_prop360",
            "as_of": "current",
            "matched_leads": matched_leads,
            "unmatched_leads": len(leads) - matched_leads,
            "by_owner": portfolio_rows,
        },
        "by_commune": commune_rows,
        "by_source": source_rows,
        "alerts": alerts,
        "records": lead_rows,
        "assignment_history": {
            "reassigned": reassigned,
            "without_prior_reassignment": without_prior_reassignment,
            "history_unavailable": history_unavailable,
            "covered_leads": len(leads) - history_unavailable,
            "outgoing_sla": int(outgoing_sla),
            "previous_owners": previous_owner_rows,
            "reassignments_by_reason": [
                {"reason": reason, "leads": int(count)}
                for reason, count in sorted(
                    reassignment_reason_counts.items(),
                    key=lambda item: (-item[1], item[0].casefold()),
                )
            ],
        },
    }


def query_monthly_leads_control(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    executive: Optional[str] = None,
) -> dict:
    """Read a bounded month of assignment episodes and verified CRM evidence."""
    db = get_db()
    try:
        start_date = datetime.strptime(str(period_start), "%Y-%m-%d").date()
        end_date = datetime.strptime(str(period_end), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return unavailable_month_payload(
            "Selecciona una ventana observada desde el 1 de agosto de 2026.",
            period_start=str(period_start or ""), period_end=str(period_end or ""),
        )
    coverage_start = datetime.strptime("2026-08-01", "%Y-%m-%d").date()
    coverage_end = datetime.now(CHILE_TZ).date()
    if start_date < coverage_start or end_date > coverage_end:
        return unavailable_month_payload(
            "La cobertura metodológica de Control comienza el 1 de agosto de 2026; la ventana debe terminar en una fecha ya observada.",
            period_start=start_date.isoformat(), period_end=end_date.isoformat(),
        )
    if start_date > end_date:
        return unavailable_month_payload(
            "El inicio del período debe ser anterior o igual al término.",
            period_start=start_date.isoformat(), period_end=end_date.isoformat(),
        )

    start_utc, end_utc = _build_chile_period_bounds(start_date.isoformat(), end_date.isoformat())
    cycles = list(db["crm_assignment_cycles"].find(
        {"assigned_at": {"$gte": start_utc, "$lt": end_utc},
         "assignment_cycle_id": {"$exists": True, "$ne": None},
         "lead_id": {"$exists": True, "$ne": None}},
        {"lead_id": 1, "assignment_cycle_id": 1, "assigned_to_display_name": 1,
         "assigned_at": 1, "sla_started_at": 1, "temperature_at_assignment": 1,
         "hot_started_at": 1, "temperature_transitioned_at": 1,
         "first_valid_management_at": 1,
         "reassignment_reason": 1, "reassignment_meta": 1,
         "assigned_by": 1, "assigned_to_user_id": 1,
         "policy_repair_id": 1, "policy_repair_version": 1,
         "policy_repair_reason": 1,
         "policy_repair_previous_owner_user_id": 1,
         "policy_repair_selected_user_id": 1,
         "closed_reason": 1, "reason": 1, "cycle_origin": 1,
         "cycle_status": 1, "schema_version": 1, "assignment_number": 1,
         "assigned_by": 1},
    ))
    lead_ids = list({cycle.get("lead_id") for cycle in cycles if cycle.get("lead_id") is not None})
    cycle_ids = list({cycle.get("assignment_cycle_id") for cycle in cycles if cycle.get("assignment_cycle_id")})
    leads = list(db["leads"].find(
        {"_id": {"$in": lead_ids}},
        {"_id": 1, "created_at": 1, "_test_lead": 1, "is_test": 1, "phone": 1,
         "lead_origin": 1, "origin": 1, "prospecto.origen": 1,
         "prospecto.codigo": 1, "prospecto.comuna": 1,
         "temperature_history": 1, "pipeline_stage": 1, "stage": 1, "status": 1,
         "lifecycle.visit_scheduled_at": 1, "stage_history": 1},
    )) if lead_ids else []
    events = list(db["crm_events"].find(
        {"lead_id": {"$in": lead_ids}, "assignment_cycle_id": {"$in": cycle_ids},
         "type": {"$in": ["GESTION_LOG", "HUMAN_NOTE", "CONTACT_RESULT", "MANUAL_ENTRY"]}},
        {"lead_id": 1, "assignment_cycle_id": 1, "type": 1, "actor": 1,
         "actor_type": 1, "result": 1, "confirmed": 1, "meta": 1,
         "timestamp": 1, "occurred_at": 1},
    )) if lead_ids and cycle_ids else []
    results = list(db["crm_management_results"].find(
        {"assignment_cycle_id": {"$in": cycle_ids}},
        {"lead_id": 1, "assignment_cycle_id": 1, "actor_user_id": 1,
         "actor_type": 1, "result_type": 1, "occurred_at": 1, "status": 1},
    )) if cycle_ids else []
    audits = list(db["crm_sla_reassignment_audit_v1"].find(
        {"event_type": "SLA_REASSIGNMENT_COMMITTED",
         "reassigned_at": {"$gte": start_utc, "$lt": end_utc}},
        {"event_type": 1, "lead_id": 1, "source_cycle_id": 1,
         "new_cycle_id": 1, "destination_cycle_id": 1,
         "target_owner_user_id": 1, "source_owner_user_id": 1, "reassigned_at": 1,
         "source_cycle_sla_breached_at": 1},
    ))
    source_cycle_ids = list({row.get("source_cycle_id") for row in audits if row.get("source_cycle_id")})
    source_cycles = list(db["crm_assignment_cycles"].find(
        {"assignment_cycle_id": {"$in": source_cycle_ids}},
        {"assignment_cycle_id": 1, "assigned_to_display_name": 1,
         "lead_id": 1, "assigned_at": 1},
    )) if source_cycle_ids else []
    source_cycle_by_id = {str(row.get("assignment_cycle_id")): row for row in source_cycles}
    for audit in audits:
        source_cycle = source_cycle_by_id.get(str(audit.get("source_cycle_id") or ""))
        audit["source_owner_display_name"] = (source_cycle or {}).get("assigned_to_display_name")

    audit_lead_ids = {str(audit.get("lead_id") or "") for audit in audits if audit.get("lead_id") is not None}
    audit_lead_query_ids = list({variant for lead_id in audit_lead_ids for variant in mongo_id_variants(lead_id)})
    successor_cycles = list(db["crm_assignment_cycles"].find(
        {"lead_id": {"$in": audit_lead_query_ids}},
        {"lead_id": 1, "assignment_cycle_id": 1, "assigned_to_display_name": 1,
         "assigned_at": 1},
    )) if audit_lead_query_ids else []
    successor_by_audit: list[tuple[dict, dict | None]] = []
    target_cycle_ids: list[str] = []
    for audit in audits:
        lead_key = str(audit.get("lead_id") or "")
        reassigned_at = coerce_utc_datetime(audit.get("reassigned_at"))
        target_id = str(audit.get("new_cycle_id") or audit.get("destination_cycle_id") or "")
        target = next((cycle for cycle in successor_cycles
                       if target_id and str(cycle.get("assignment_cycle_id") or "") == target_id), None)
        if target is None:
            candidates = [cycle for cycle in successor_cycles
                          if str(cycle.get("lead_id") or "") == lead_key
                          and str(cycle.get("assignment_cycle_id") or "") != str(audit.get("source_cycle_id") or "")
                          and (coerce_utc_datetime(cycle.get("assigned_at")) or datetime.max.replace(tzinfo=timezone.utc)) >= (reassigned_at or start_utc)]
            target = min(candidates, key=lambda row: coerce_utc_datetime(row.get("assigned_at")) or datetime.max.replace(tzinfo=timezone.utc)) if candidates else None
        if target and target.get("assignment_cycle_id"):
            target_cycle_ids.append(str(target["assignment_cycle_id"]))
        source_cycle = source_cycle_by_id.get(str(audit.get("source_cycle_id") or "")) or {}
        source_lead_key = str(source_cycle.get("lead_id") or "")
        target_lead_key = str((target or {}).get("lead_id") or "")
        source_at = coerce_utc_datetime(source_cycle.get("assigned_at"))
        target_at = coerce_utc_datetime((target or {}).get("assigned_at"))
        valid_transition = bool(
            source_cycle
            and target
            and source_lead_key
            and source_lead_key == lead_key == target_lead_key
            and source_at
            and reassigned_at
            and target_at
            and source_at <= reassigned_at <= target_at
            and _ops_normalized_person_name(source_cycle.get("assigned_to_display_name"))
            and _ops_normalized_person_name((target or {}).get("assigned_to_display_name"))
            and _ops_normalized_person_name(source_cycle.get("assigned_to_display_name"))
                != _ops_normalized_person_name((target or {}).get("assigned_to_display_name"))
        )
        audit["reconciled_source_owner_display_name"] = (source_cycle or {}).get("assigned_to_display_name")
        audit["reconciled_target_cycle_id"] = str((target or {}).get("assignment_cycle_id") or "")
        audit["reconciled_transfer"] = valid_transition
        successor_by_audit.append((audit, target))
    target_results = list(db["crm_management_results"].find(
        {"assignment_cycle_id": {"$in": target_cycle_ids}},
        {"assignment_cycle_id": 1, "occurred_at": 1, "result_type": 1,
         "status": 1, "actor_user_id": 1},
    )) if target_cycle_ids else []
    results_by_target: dict[str, list[dict]] = defaultdict(list)
    for result in target_results:
        results_by_target[str(result.get("assignment_cycle_id") or "")].append(result)
    audit_leads = list(db["leads"].find(
        {"_id": {"$in": audit_lead_query_ids}},
        {"_id": 1, "prospecto.codigo": 1, "pipeline_stage": 1, "stage": 1, "status": 1},
    )) if audit_lead_query_ids else []
    audit_lead_by_id = {str(row.get("_id")): row for row in audit_leads}
    audit_code_by_lead = {key: str((row.get("prospecto") or {}).get("codigo") or "Sin código") for key, row in audit_lead_by_id.items()}
    loss_outcomes = []
    for audit, target in successor_by_audit:
        if not audit.get("reconciled_transfer"):
            continue
        source_name = str(audit.get("source_owner_display_name") or "Sin ejecutivo identificado")
        if _is_control_non_broker(source_name):
            continue
        if executive and source_name.casefold() != str(executive).strip().casefold():
            continue
        reassigned_at = coerce_utc_datetime(audit.get("reassigned_at"))
        target_id = str((target or {}).get("assignment_cycle_id") or "")
        later_results = [row for row in results_by_target.get(target_id, [])
                         if row.get("status") in (None, "completed") and row.get("actor_user_id")
                         and coerce_utc_datetime(row.get("occurred_at"))
                         and coerce_utc_datetime(row.get("occurred_at")) >= (reassigned_at or start_utc)]
        later_results.sort(key=lambda row: coerce_utc_datetime(row.get("occurred_at")) or datetime.max.replace(tzinfo=timezone.utc))
        result = later_results[0] if later_results else None
        lead_key = str(audit.get("lead_id") or "")
        loss_outcomes.append({
            "lead_id": lead_key,
            "lead_code": audit_code_by_lead.get(lead_key, "Sin código"),
            "property_code": str((audit_lead_by_id.get(lead_key, {}).get("prospecto") or {}).get("codigo") or "").strip() or None,
            "source_cycle_id": str(audit.get("source_cycle_id") or "") or None,
            "target_cycle_id": target_id or None,
            "source_executive": source_name,
            "rescued_by": str((target or {}).get("assigned_to_display_name") or "Sin receptor identificado"),
            "reassigned_at": reassigned_at.isoformat() if reassigned_at else None,
            "current_crm_stage": str(
                audit_lead_by_id.get(lead_key, {}).get("pipeline_stage")
                or audit_lead_by_id.get(lead_key, {}).get("stage")
                or audit_lead_by_id.get(lead_key, {}).get("status")
                or ""
            ).strip().upper() or None,
            "result_type": str((result or {}).get("result_type") or "Sin resultado posterior acreditado"),
            "result_at": coerce_utc_datetime((result or {}).get("occurred_at")).isoformat() if coerce_utc_datetime((result or {}).get("occurred_at")) else None,
        })

    now_utc = datetime.now(timezone.utc)
    confirmed_sla_audits = [audit for audit in audits if audit.get("reconciled_transfer")]
    payload = build_month_metrics(
        cycles=cycles, leads=leads, events=events, results=results,
        sla_audits=confirmed_sla_audits, executive=executive, as_of=now_utc,
    )
    # Visit conversion is a lead-level outcome of the received cohort, not a
    # count of assignment episodes. Count only dated CRM evidence of the
    # VISIT_SCHEDULED milestone; this does not claim that the physical visit
    # took place.
    first_received_episode_by_lead = {}
    for row in payload.get("records") or []:
        lead_key = str(row.get("lead_id") or "").strip()
        if not lead_key:
            continue
        previous = first_received_episode_by_lead.get(lead_key)
        row_at = coerce_utc_datetime(row.get("assigned_at"))
        previous_at = coerce_utc_datetime((previous or {}).get("assigned_at"))
        if (previous is None or (row_at and (not previous_at or row_at < previous_at))
                or (row_at == previous_at and str(row.get("cycle_id") or "") < str(previous.get("cycle_id") or ""))):
            first_received_episode_by_lead[lead_key] = row
    managed_cohort = {
        lead_key: row for lead_key, row in first_received_episode_by_lead.items()
        if row.get("first_management_at")
    }
    first_cycle_by_lead = {
        lead_key: str(row.get("cycle_id") or "") for lead_key, row in managed_cohort.items()
    }
    lead_by_string_id = {str(lead.get("_id")): lead for lead in leads}
    scheduled_lead_ids = set()
    for result in results:
        lead_key = str(result.get("lead_id") or "").strip()
        if (lead_key not in managed_cohort
                or str(result.get("assignment_cycle_id") or "") != first_cycle_by_lead.get(lead_key)
                or str(result.get("status") or "").lower() != "completed"
                or str(result.get("result_type") or "").strip().upper() != "VISIT_SCHEDULED"):
            continue
        result_at = coerce_utc_datetime(result.get("occurred_at"))
        management_at = coerce_utc_datetime(managed_cohort[lead_key].get("first_management_at"))
        if result_at and management_at and result_at >= management_at:
            scheduled_lead_ids.add(lead_key)
    for lead_key, row in managed_cohort.items():
        lead = lead_by_string_id.get(lead_key) or {}
        management_at = coerce_utc_datetime(row.get("first_management_at"))
        lifecycle_visit_at = coerce_utc_datetime((lead.get("lifecycle") or {}).get("visit_scheduled_at"))
        if lifecycle_visit_at and management_at and lifecycle_visit_at >= management_at:
            scheduled_lead_ids.add(lead_key)
        for history in lead.get("stage_history") or []:
            if str((history or {}).get("to") or "").strip().upper() != "VISIT_SCHEDULED":
                continue
            visit_at = coerce_utc_datetime((history or {}).get("timestamp"))
            if visit_at and management_at and visit_at >= management_at:
                scheduled_lead_ids.add(lead_key)
                break
    managed_lead_count = len(managed_cohort)
    scheduled_lead_count = len(scheduled_lead_ids)
    # Team context is derived from the same bounded, already-loaded evidence.
    # This adds no MongoDB reads and keeps team participation distinct from
    # assignment-cycle counts.
    if executive:
        team_payload = build_month_metrics(
            cycles=cycles, leads=leads, events=events, results=results,
            sla_audits=confirmed_sla_audits, executive=None, as_of=now_utc,
        )
        team_records = team_payload.get("records") or []
        by_executive_lead = {}
        for row in team_records:
            lead_key = str(row.get("lead_id") or "").strip()
            executive_name = str(row.get("executive") or "").strip()
            if not lead_key or not executive_name:
                continue
            key = (_ops_normalized_person_name(executive_name), lead_key)
            previous = by_executive_lead.get(key)
            row_at = coerce_utc_datetime(row.get("assigned_at"))
            previous_at = coerce_utc_datetime((previous or {}).get("assigned_at"))
            if (previous is None or (row_at and (not previous_at or row_at < previous_at))
                    or (row_at == previous_at and str(row.get("cycle_id") or "") < str(previous.get("cycle_id") or ""))):
                by_executive_lead[key] = row

        team_unique_leads = int((team_payload.get("unique_lead_sla") or {}).get("leads_received") or 0)
        executive_unique_leads = int((payload.get("unique_lead_sla") or {}).get("leads_received") or 0)
        selected_share = round(executive_unique_leads / team_unique_leads * 100, 1) if team_unique_leads else None
        executive_stats = {}
        for (name_key, _lead_key), row in by_executive_lead.items():
            stat = executive_stats.setdefault(name_key, {"name": str(row.get("executive") or ""), "within": 0, "outside": 0})
            if row.get("sla_state") == "MANAGED_WITHIN_SLA":
                stat["within"] += 1
            elif row.get("sla_state") in {"MANAGED_OUTSIDE_SLA", "BREACHED"}:
                stat["outside"] += 1
        ranked_executives = []
        for stat in executive_stats.values():
            evaluable_count = stat["within"] + stat["outside"]
            if evaluable_count > 0:
                stat["evaluable"] = evaluable_count
                stat["compliance_pct"] = round(stat["within"] / evaluable_count * 100, 1)
                stat["low_sample"] = evaluable_count < 10
                ranked_executives.append(stat)
        ranked_executives.sort(key=lambda stat: (-stat["compliance_pct"], stat["name"].casefold()))
        selected_key = _ops_normalized_person_name(executive)
        selected_stat = executive_stats.get(selected_key, {"within": 0, "outside": 0, "name": str(executive)})
        selected_evaluable = selected_stat["within"] + selected_stat["outside"]
        selected_pct = round(selected_stat["within"] / selected_evaluable * 100, 1) if selected_evaluable else None
        rank = None
        if selected_evaluable > 0 and selected_pct is not None:
            rank = 1 + sum(1 for stat in ranked_executives if stat["compliance_pct"] > selected_pct)
        payload["team_context"] = {
            "unique_leads_total": team_unique_leads,
            "selected_unique_leads": executive_unique_leads,
            "selected_share_pct": selected_share,
            "executives_with_evaluable_cases": len(ranked_executives),
            "executives_without_evaluable_cases": sum(
                1 for stat in executive_stats.values() if stat["within"] + stat["outside"] == 0
            ),
            "sla_ranking": {
                "status": "ranked" if rank is not None else ("insufficient_sample" if selected_evaluable else "no_data"),
                "rank": rank,
                "eligible_executives": len(ranked_executives),
                "minimum_sample": 1,
                "low_sample": bool(selected_evaluable and selected_evaluable < 10),
                "within": selected_stat["within"],
                "evaluable": selected_evaluable,
                "compliance_pct": selected_pct,
                "period_start": start_date.isoformat(),
                "period_end": end_date.isoformat(),
            },
        }
    cohort_cycle_ids = {
        str(row.get("cycle_id") or "").strip()
        for row in payload.get("records") or []
        if str(row.get("cycle_id") or "").strip()
    }
    cohort_lead_ids = {
        str(row.get("lead_id") or "").strip()
        for row in payload.get("records") or []
        if str(row.get("lead_id") or "").strip()
    }
    # Keep the drill-down aligned with the same received-lead cohort used by
    # monthly_control's loss summary. A transfer by the selected executive is
    # included only when its source episode and lead belong to this period's
    # received cohort.
    payload["sla_loss_outcomes"] = [
        row for row in loss_outcomes
        if str(row.get("source_cycle_id") or "").strip() in cohort_cycle_ids
        and str(row.get("lead_id") or "").strip() in cohort_lead_ids
    ]
    payload["unreconciled_sla_audit_events"] = len(audits) - len(confirmed_sla_audits)
    payload.update({
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat(),
                   "label": start_date.strftime("%B %Y")},
        "as_of": now_utc.isoformat(),
        "executive": str(executive or "").strip() or None,
        "events": {
            "visits_scheduled": {
                "status": "available" if managed_lead_count else "no_managed_leads",
                "count": scheduled_lead_count if managed_lead_count else None,
                "managed_leads": managed_lead_count,
                "conversion_pct": round(scheduled_lead_count / managed_lead_count * 100, 1) if managed_lead_count else None,
                "basis": "Leads únicos recibidos en el período con primera gestión acreditada; evidencia fechada VISIT_SCHEDULED en el CRM, deduplicada por lead.",
                "not_equivalent_to_visit_performed": True,
                "observed_as_of": now_utc.isoformat(),
            },
            "visits_performed": {"status": "unavailable", "count": None,
                                  "reason": "No existe evento suficiente que acredite una visita efectivamente realizada."},
            "offers_presented": {"status": "unavailable", "count": None,
                                  "reason": "No hay evento verificable y consistente de oferta formal presentada."},
            "closures": {"status": "unavailable", "count": None,
                         "reason": "No hay evento verificable de cierre comercial concretado."},
        },
        "month_end_portfolio": {"status": "unavailable", "count": None,
                                 "reason": "No hay historial de estado y responsable para reconstruir la cartera al cierre."},
        "stagnation": {"status": "unavailable", "count": None,
                       "reason": "El estado histórico al cierre no está disponible para agosto/septiembre."},
        "property_portfolio": {"status": "unavailable", "reason": "Prop360 no conserva una clasificación histórica propia/oficina por fecha de corte."},
        "exposure": {"status": "unavailable", "reason": "Faltan períodos efectivos de publicación/exposición enlazados a consultas por propiedad."},
        "composition": {"status": "unavailable", "source": None, "operation": None,
                        "reason": "La fuente y operación no se guardaron como instantánea histórica en el episodio."},
        "comparison": {
            "own_previous_month": {"status": "not_comparable", "reason": "La fuente, operación y cartera histórica no están disponibles para confirmar una mezcla comparable entre cohortes."},
            "team_median": {"status": "not_published", "reason": "No se compara a los ejecutivos mientras no sea posible controlar diferencias de origen, operación y tipo de cartera."},
        },
        "policy": {"timezone": "America/Santiago", "business_days": "lunes a viernes",
                   "business_hours": "09:00–19:00", "hot_minutes": 60,
                   "normal_minutes": 180, "exact_limit_is_breached": True,
                   "holidays": "se consideran hábiles si caen de lunes a viernes"},
        "methodology": {"assignment_population": "Episodios cuya asignación comenzó dentro del mes seleccionado.",
                        "event_population": "Eventos contabilizados por fecha efectiva, en paneles separados.",
                        "month_end_stock": "No se reconstruye desde el estado actual del lead.",
                        "notification_time": "No modifica el inicio ni el consumo del SLA."},
    })
    return payload


def query_monthly_control_executives(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
) -> dict:
    """Executive filter options from episode owners in the selected month."""
    try:
        start_date = datetime.strptime(str(period_start), "%Y-%m-%d").date()
        end_date = datetime.strptime(str(period_end), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return {"rows": []}
    if (start_date < datetime.strptime("2026-08-01", "%Y-%m-%d").date()
            or end_date > datetime.now(CHILE_TZ).date() or start_date > end_date):
        return {"rows": []}
    start_utc, end_utc = _build_chile_period_bounds(start_date.isoformat(), end_date.isoformat())
    db = get_db()
    cycles = list(db["crm_assignment_cycles"].find(
        {"assigned_at": {"$gte": start_utc, "$lt": end_utc},
         "assignment_cycle_id": {"$exists": True, "$ne": None},
         "assigned_to_display_name": {"$exists": True, "$nin": [None, ""]}},
        {"lead_id": 1, "assigned_to_display_name": 1, "cycle_origin": 1,
         "reason": 1, "closed_reason": 1, "assigned_by": 1,
         "assigned_to_user_id": 1, "policy_repair_id": 1,
         "policy_repair_version": 1,
         "policy_repair_reason": 1,
         "policy_repair_previous_owner_user_id": 1,
         "policy_repair_selected_user_id": 1},
    ))
    lead_ids = list({row.get("lead_id") for row in cycles if row.get("lead_id") is not None})
    leads = list(db["leads"].find(
        {"_id": {"$in": lead_ids}},
        {"_id": 1, "_test_lead": 1, "is_test": 1, "phone": 1,
         "lead_origin": 1, "origin": 1, "prospecto.origen": 1},
    )) if lead_ids else []
    lead_by_id = {str(lead.get("_id")): lead for lead in leads}
    normalized = {
        str(row.get("assigned_to_display_name")).strip()
        for row in cycles
        if str(row.get("assigned_to_display_name") or "").strip()
        and not _is_control_non_broker(row.get("assigned_to_display_name"))
        and not _exclusion_category(row, lead_by_id.get(str(row.get("lead_id"))))
    }
    normalized = {name for name in normalized if name.casefold() not in {"agente", "administrador"}}
    return {"rows": [{"ejecutivo": name} for name in sorted(normalized, key=str.casefold)]}


def _ops_portfolio_codes(db, captador: Optional[str]) -> list[str]:
    if not captador:
        return []
    docs = db["universo_cartera"].find(
        {"oficina": OPS_PORTFOLIO_OFFICE, "ejecutivo": str(captador).strip()}, {"codigo": 1}
    )
    return sorted({str(doc.get("codigo")).strip() for doc in docs if doc.get("codigo") not in (None, "")})


_OPS_PROJECTED_LEAD_PROJECTION = {
        "_id": 1, "created_at": 1, "_created_normalized": 1,
        "phone": 1, "prospecto.nombre": 1, "prospecto.codigo": 1,
        "pipeline_stage": 1, "stage": 1, "ejecutivo_asignado": 1,
        "lead_temperature_effective": 1,
        "lifecycle.assigned_at": 1,
        "lifecycle.current_assignment_cycle_id": 1,
        "lifecycle.first_valid_management_at": 1,
        "lifecycle.first_effective_contact_at": 1,
        "lifecycle.visit_scheduled_at": 1,
        "stage_history": 1,
}

_OPS_CURRENT_RESOURCE_TTL = 120
_OPS_CURRENT_RESOURCE_CACHE = {}
_OPS_CURRENT_RESOURCE_LOCK = Lock()
_OPS_USERS_CACHE = None
_OPS_USERS_CACHE_AT = 0.0
_OPS_USERS_CACHE_LOCK = Lock()
_OPS_HISTORICAL_BASE_TTL = 120
_OPS_HISTORICAL_BASE_CACHE = {}
_OPS_HISTORICAL_BASE_INFLIGHT = {}
_OPS_HISTORICAL_BASE_LOCK = Lock()


def _ops_historical_base_match(mongo_filters: dict) -> tuple[dict, str, str]:
    """Build the period-independent assigned-lead universe for Operations.

    The production cohort contract is based on ``lifecycle.assigned_at``.
    Keep the same non-date filters and use a stable historical envelope so
    period presets can be sliced locally without changing the cohort rules.
    """
    base_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now_utc = datetime.now(timezone.utc)
    base_end = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc) + timedelta(days=1)
    assigned_date = {"$convert": {"input": "$lifecycle.assigned_at", "to": "date", "onError": None, "onNull": None}}
    date_expr = {"$expr": {"$and": [
        {"$gte": [assigned_date, base_start]},
        {"$lt": [assigned_date, base_end]},
    ]}}
    if mongo_filters:
        match = {"$and": [date_expr, mongo_filters]}
    else:
        match = date_expr
    return match, base_start.isoformat(), base_end.isoformat()


def _ops_slice_historical_base(docs: list[dict], start_utc: datetime, end_utc: datetime) -> list[dict]:
    """Apply the exact semi-open assigned_at interval used by Mongo."""
    sliced = []
    for doc in docs:
        assigned_at = coerce_utc_datetime((doc.get("lifecycle") or {}).get("assigned_at"))
        if assigned_at is not None and start_utc <= assigned_at < end_utc:
            sliced.append(doc)
    return sliced


def _ops_historical_base(
    db,
    mongo_filters: dict,
    projection: dict,
    profile: Optional[dict] = None,
) -> list[dict]:
    """Load and cache raw historical cohort documents with single-flight."""
    match, base_start, base_end = _ops_historical_base_match(mongo_filters)
    cache_key = repr((match, tuple(sorted(projection.items())), base_start, base_end))
    now = time.time()
    cached = _OPS_HISTORICAL_BASE_CACHE.get(cache_key)
    if cached and now - cached["created_at"] < _OPS_HISTORICAL_BASE_TTL:
        if profile is not None:
            profile.setdefault("resource", []).append({"operation": "historical_base_cache_hit", "documents": len(cached["data"])})
        return cached["data"]

    leader = False
    with _OPS_HISTORICAL_BASE_LOCK:
        now = time.time()
        cached = _OPS_HISTORICAL_BASE_CACHE.get(cache_key)
        if cached and now - cached["created_at"] < _OPS_HISTORICAL_BASE_TTL:
            if profile is not None:
                profile.setdefault("resource", []).append({"operation": "historical_base_cache_hit", "documents": len(cached["data"])})
            return cached["data"]
        flight = _OPS_HISTORICAL_BASE_INFLIGHT.get(cache_key)
        if flight is None:
            flight = {"event": Event(), "error": None}
            _OPS_HISTORICAL_BASE_INFLIGHT[cache_key] = flight
            leader = True

    if not leader:
        flight["event"].wait()
        if flight.get("error") is not None:
            raise flight["error"]
        cached = _OPS_HISTORICAL_BASE_CACHE.get(cache_key)
        if cached is None:
            raise RuntimeError("historical Operations base completed without a cache entry")
        if profile is not None:
            profile.setdefault("resource", []).append({"operation": "historical_base_single_flight_wait", "documents": len(cached["data"])})
        return cached["data"]

    try:
        started = time.perf_counter()
        docs = _profile_query(profile, "leads", "aggregate_historical_base", lambda: db["leads"].aggregate([
            _normalized_created_at_stage(),
            {"$match": match},
            {"$project": projection},
        ]))
        _OPS_HISTORICAL_BASE_CACHE[cache_key] = {"created_at": time.time(), "data": docs}
        if profile is not None:
            profile.setdefault("resource", []).append({
                "operation": "historical_base_loaded",
                "documents": len(docs),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            })
        return docs
    except Exception as exc:
        flight["error"] = exc
        raise
    finally:
        with _OPS_HISTORICAL_BASE_LOCK:
            _OPS_HISTORICAL_BASE_INFLIGHT.pop(cache_key, None)
            flight["event"].set()


def _ops_projected_leads(
    db,
    match: dict,
    profile: Optional[dict] = None,
    operation: str = "aggregate_projected_leads",
    projection: Optional[dict] = None,
) -> list[dict]:
    projection = projection or _OPS_PROJECTED_LEAD_PROJECTION
    return _profile_query(profile, "leads", operation, lambda: db["leads"].aggregate([
        _normalized_created_at_stage(),
        {"$match": match},
        {"$project": projection},
    ]))


def _ops_current_resource(
    db,
    active_match: dict,
    profile: Optional[dict] = None,
) -> dict:
    """Cache period-independent current counts plus actionable lead detail.

    Current SLA/aging decisions remain in Python. Mongo only removes managed
    assigned leads that cannot contribute to those decisions, while retaining
    every unassigned, pending, or temporally inconsistent lead.
    """
    cache_key = repr(active_match)
    now = time.time()
    cached = _OPS_CURRENT_RESOURCE_CACHE.get(cache_key)
    if cached and now - cached["created_at"] < _OPS_CURRENT_RESOURCE_TTL:
        if profile is not None:
            profile.setdefault("resource", []).append({"operation": "current_resource_cache_hit"})
        return cached["data"]
    with _OPS_CURRENT_RESOURCE_LOCK:
        now = time.time()
        cached = _OPS_CURRENT_RESOURCE_CACHE.get(cache_key)
        if cached and now - cached["created_at"] < _OPS_CURRENT_RESOURCE_TTL:
            if profile is not None:
                profile.setdefault("resource", []).append({"operation": "current_resource_cache_hit"})
            return cached["data"]
        started = time.perf_counter()
        normalized = _normalized_created_at_stage()
        summary_pipeline = [normalized, {"$match": active_match}, {"$facet": {
            "executives": [{"$group": {"_id": "$ejecutivo_asignado", "active": {"$sum": 1}}}],
            "totals": [{"$group": {"_id": None, "active": {"$sum": 1}}}],
        }}]
        summary_rows = _profile_query(profile, "leads", "aggregate_current_summary", lambda: db["leads"].aggregate(summary_pipeline))
        summary_row = summary_rows[0] if summary_rows else {}
        by_executive = []
        unassigned = 0
        active_total = int((summary_row.get("totals") or [{}])[0].get("active") or 0)
        for row in summary_row.get("executives") or []:
            name = str(row.get("_id") or "").strip()
            count = int(row.get("active") or 0)
            if _ops_unassigned(name):
                unassigned += count
            else:
                by_executive.append({"executive": name, "active": count})
        detail_match = {"$and": [active_match, {"$or": [
            {"ejecutivo_asignado": {"$in": [None, "", "Sin Asignar", "No Asignado"]}},
            {"lifecycle.first_valid_management_at": None},
            {"$expr": {"$lt": [
                {"$convert": {"input": "$lifecycle.first_valid_management_at", "to": "date", "onError": None, "onNull": None}},
                {"$convert": {"input": "$lifecycle.assigned_at", "to": "date", "onError": None, "onNull": None}},
            ]}},
            # Mongo dates are millisecond precision while some historical
            # assigned_at values are strings with microseconds.  Keep the
            # equality-after-conversion boundary so Python can preserve the
            # canonical temporal comparison used by the old contract.
            {"$expr": {"$eq": [
                {"$convert": {"input": "$lifecycle.first_valid_management_at", "to": "date", "onError": None, "onNull": None}},
                {"$convert": {"input": "$lifecycle.assigned_at", "to": "date", "onError": None, "onNull": None}},
            ]}},
        ]}]}
        detail_docs = _profile_query(profile, "leads", "aggregate_current_actionable_detail", lambda: db["leads"].aggregate([
            normalized, {"$match": detail_match}, {"$project": _OPS_PROJECTED_LEAD_PROJECTION},
        ]))
        data = {
            "summary": {"active_total": active_total, "active_assigned": active_total - unassigned,
                        "unassigned": unassigned, "by_executive": by_executive},
            "detail_docs": detail_docs,
            "summary_rows": len(summary_row.get("executives") or []),
            "summary_bson_bytes": sum(len(BSON.encode(row)) for row in summary_rows),
            "detail_bson_bytes": sum(len(BSON.encode(row)) for row in detail_docs),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        _OPS_CURRENT_RESOURCE_CACHE[cache_key] = {"created_at": time.time(), "data": data}
        return data


def _ops_elapsed(start: Optional[datetime], end: datetime) -> int:
    if not start:
        return 0
    # Keep the canonical fractional value for SLA decisions. Presentation
    # rounds it later; rounding here can misclassify an exact boundary.
    return max(0.0, float(calculate_business_minutes(
        start.astimezone(CHILE_TZ), end.astimezone(CHILE_TZ)
    )))


def _ops_elapsed_calendar_minutes(start: Optional[datetime], end: datetime) -> int:
    """Calendar age for backlog presentation, independent from the SLA clock."""
    if not start:
        return 0
    return max(0.0, (end - start).total_seconds() / 60)


def _ops_response_minutes(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    """Diferencia de respuesta sin convertir intervalos negativos en cero."""
    if not start or not end:
        return None
    return float(calculate_business_minutes(
        start.astimezone(CHILE_TZ), end.astimezone(CHILE_TZ)
    ))


_OPS_MANAGEMENT_UNSET = object()


def _ops_state(doc: dict, now: datetime, management_override=_OPS_MANAGEMENT_UNSET) -> dict:
    lifecycle = doc.get("lifecycle") or {}
    assigned = coerce_utc_datetime(lifecycle.get("assigned_at"))
    managed = (coerce_utc_datetime(lifecycle.get("first_valid_management_at"))
               if management_override is _OPS_MANAGEMENT_UNSET else management_override)
    temperature = str(doc.get("lead_temperature_effective") or "NORMAL").upper()
    threshold = 60 if temperature == "HOT" else 180
    elapsed = _ops_elapsed(assigned or coerce_utc_datetime(doc.get("_created_normalized")), now)
    elapsed_calendar = _ops_elapsed_calendar_minutes(
        assigned or coerce_utc_datetime(doc.get("_created_normalized")), now
    )
    unassigned = _ops_unassigned(doc.get("ejecutivo_asignado"))
    if unassigned:
        code, label, rank = "unassigned", "Sin ejecutivo asignado", 4
    elif managed:
        code, label, rank = None, None, 6
    elif temperature == "HOT" and 45 <= elapsed < 60:
        code, label, rank = "hot_near_due", "HOT próximo a vencer", 3
    elif elapsed > threshold:
        code = "hot_open_overdue" if temperature == "HOT" else "normal_open_overdue"
        label = "HOT vencido" if temperature == "HOT" else "NORMAL vencido"
        rank = 1 if temperature == "HOT" else 2
    else:
        code, label, rank = "pending_first_management", "Pendiente de primera gestión", 5
    return {"assigned": assigned, "managed": managed, "temperature": temperature,
            "threshold": threshold, "elapsed": elapsed, "elapsed_calendar": elapsed_calendar,
            "unassigned": unassigned,
            "priority_code": code, "priority_label": label, "priority_rank": rank}


def _ops_exec_bucket(name: str) -> dict:
    return {"executive": name,
            "current": {"active_load": 0, "share_of_team_load_pct": None,
                        "pending": 0, "hot_overdue": 0, "normal_overdue": 0,
                        "hot_near_due": 0, "activity_without_result": 0,
                        "oldest_pending_minutes": None, "oldest_pending_calendar_minutes": None,
                        "aging": {"lt_24h": 0, "d_1_3": 0, "d_4_7": 0, "gt_7d": 0}},
            "period": {"assigned": 0, "managed": 0, "managed_within_sla": 0,
                       "managed_late": 0, "hot_sla_pct": None, "normal_sla_pct": None,
                       "p50_hot": None, "p90_hot": None, "p50_normal": None,
                       "p90_normal": None, "hot_n": 0, "normal_n": 0,
                       "activity_attempts": 0, "activity_without_result": 0,
                       "result_breakdown": {}, "result_event_count": 0, "result_leads": 0,
                       "contact_effective": 0, "visits_scheduled": 0,
                       "hot_stats": {}, "normal_stats": {},
                       "temporal_inconsistent": {"hot": 0, "normal": 0, "total": 0}},
            "_hot_managed": 0, "_hot_within": 0, "_normal_managed": 0,
            "_normal_within": 0, "_hot_times": [], "_normal_times": [],
            "_temporal_inconsistent": {"hot": 0, "normal": 0, "total": 0},
            "_activity_ids": set(), "_result_ids": set()}


def _ops_finalize_execs(buckets: dict[str, dict], team_load: int) -> list[dict]:
    loads = [item["current"]["active_load"] for item in buckets.values() if item["current"]["active_load"]]
    mean = sum(loads) / len(loads) if loads else None
    ordered = sorted(loads)
    median = ordered[len(ordered) // 2] if ordered else None
    for item in buckets.values():
        current, period = item["current"], item["period"]
        current["share_of_team_load_pct"] = round(current["active_load"] / team_load * 100, 1) if team_load else None
        period["hot_sla_pct"] = round(item["_hot_within"] / item["_hot_managed"] * 100, 1) if item["_hot_managed"] else None
        period["normal_sla_pct"] = round(item["_normal_within"] / item["_normal_managed"] * 100, 1) if item["_normal_managed"] else None
        period["hot_n"] = len(item["_hot_times"])
        period["normal_n"] = len(item["_normal_times"])
        period["p50_hot"], period["p90_hot"] = _ops_percentile(item["_hot_times"], 50), _ops_percentile(item["_hot_times"], 90)
        period["p50_normal"], period["p90_normal"] = _ops_percentile(item["_normal_times"], 50), _ops_percentile(item["_normal_times"], 90)
        period["hot_stats"] = _ops_stats(item["_hot_times"])
        period["normal_stats"] = _ops_stats(item["_normal_times"])
        period["temporal_inconsistent"] = dict(item["_temporal_inconsistent"])
        for key in ("_hot_managed", "_hot_within", "_normal_managed", "_normal_within", "_hot_times", "_normal_times"):
            item.pop(key, None)
        item.pop("_temporal_inconsistent", None)
    return sorted(buckets.values(), key=lambda item: (-item["current"]["active_load"], item["executive"]))


def _ops_active_executive_names(db, profile: Optional[dict] = None) -> set[str]:
    """Nombres que representan al equipo comercial visible en la matriz.

    La matriz no debe mezclar administradores, supervisores ni ejecutivos
    inactivos con la dotación comercial vigente.
    """
    global _OPS_USERS_CACHE, _OPS_USERS_CACHE_AT
    now = time.time()
    if _OPS_USERS_CACHE is not None and now - _OPS_USERS_CACHE_AT < _OPS_CURRENT_RESOURCE_TTL:
        if profile is not None:
            profile.setdefault("resource", []).append({"operation": "users_cache_hit"})
        return set(_OPS_USERS_CACHE)
    with _OPS_USERS_CACHE_LOCK:
        now = time.time()
        if _OPS_USERS_CACHE is not None and now - _OPS_USERS_CACHE_AT < _OPS_CURRENT_RESOURCE_TTL:
            if profile is not None:
                profile.setdefault("resource", []).append({"operation": "users_cache_hit"})
            return set(_OPS_USERS_CACHE)
        names = {
            str(user.get("nombre") or "").strip()
            for user in _profile_query(profile, "usuarios", "find_active_executives", lambda: db["usuarios"].find({"rol": "agente", "is_active": True}, {"nombre": 1}))
        }
        _OPS_USERS_CACHE = {name for name in names if name and name.casefold() != "Control Owner"}
        _OPS_USERS_CACHE_AT = time.time()
        return set(_OPS_USERS_CACHE)


def _ops_assignment_episode_map(db, docs: list[dict], profile: Optional[dict] = None) -> dict[str, list[dict]]:
    """Carga ciclos sospechosos en una sola lectura, nunca una por lead."""
    suspicious_ids = []
    for doc in docs:
        lifecycle = doc.get("lifecycle") or {}
        assigned = coerce_utc_datetime(lifecycle.get("assigned_at"))
        managed = coerce_utc_datetime(lifecycle.get("first_valid_management_at"))
        if assigned and managed and managed < assigned:
            suspicious_ids.append(doc.get("_id"))
    if not suspicious_ids:
        return {}
    cycles = _profile_query(profile, "crm_assignment_cycles", "find_assignment_cycles", lambda: db["crm_assignment_cycles"].find(
        {"lead_id": {"$in": suspicious_ids}},
        {"lead_id": 1, "assignment_cycle_id": 1, "assigned_at": 1,
         "unassigned_at": 1, "cycle_status": 1},
    ))
    result: dict[str, list[dict]] = {}
    for cycle in cycles:
        result.setdefault(str(cycle.get("lead_id")), []).append(cycle)
    return result


def _ops_temporal_assignment(doc: dict, assignment_cycles: Optional[dict[str, list[dict]]] = None) -> dict:
    """Resuelve el episodio actual sin convertir gestión histórica en respuesta cero."""
    lifecycle = doc.get("lifecycle") or {}
    assigned = coerce_utc_datetime(lifecycle.get("assigned_at"))
    managed = coerce_utc_datetime(lifecycle.get("first_valid_management_at"))
    episode_start = assigned
    cycles = (assignment_cycles or {}).get(str(doc.get("_id")), [])
    current_cycle_id = lifecycle.get("current_assignment_cycle_id")
    selected = next(
        (cycle for cycle in cycles
         if current_cycle_id and str(cycle.get("assignment_cycle_id")) == str(current_cycle_id)),
        None,
    )
    if selected is None:
        active = [cycle for cycle in cycles if cycle.get("unassigned_at") in (None, "")]
        if active:
            selected = max(active, key=lambda cycle: coerce_utc_datetime(cycle.get("assigned_at")) or datetime.min.replace(tzinfo=timezone.utc))
    cycle_start = coerce_utc_datetime((selected or {}).get("assigned_at")) if selected else None
    if cycle_start:
        episode_start = cycle_start
    inconsistent = bool(managed and episode_start and managed < episode_start)
    return {"assignment_start": episode_start, "management": None if inconsistent else managed,
            "temporal_inconsistent": inconsistent}


def _ops_collect_activity_signals(db, current_docs: list[dict], period_docs: list[dict],
                                  period_start: datetime, period_cutoff: datetime,
                                  assignment_cycles: Optional[dict[str, list[dict]]] = None,
                                  profile: Optional[dict] = None) -> dict:
    """Read auditable outreach/result events once for the operational contract.

    Outreach is deliberately separate from a valid result. Opening/sending a
    channel proves activity only; ``first_valid_management_at`` remains the
    canonical result/SLA stop signal.
    """
    docs = {str(doc.get("_id")): doc for doc in [*current_docs, *period_docs] if doc.get("_id") is not None}
    if not docs:
        return {}
    # crm_events also contains navigations, assignments and bot/system noise.
    # Restrict the server-side read to event families that can contribute to
    # either activity or a valid management result.  The Python evidence
    # checks below remain authoritative (human actor, confirmation, result,
    # assignment episode, etc.).
    activity_types = sorted({
        value
        for event_type in REGISTERED_OUTREACH_EVENT_TYPES
        for value in (event_type, event_type.lower())
    })
    management_types = sorted({
        value
        for event_type in (VALID_MANAGEMENT_EVENT_TYPES - {"CONTACT_RESULT"})
        for value in (event_type, event_type.lower())
    })
    event_filter = {
        "lead_id": {"$in": [doc.get("_id") for doc in docs.values()]},
        "$or": [
            {"type": {"$in": activity_types}},
            {"type": {"$in": management_types}},
        ],
    }
    try:
        events = _profile_query(profile, "crm_events", "find_activity_and_results", lambda: db["crm_events"].find(
            event_filter,
            {"lead_id": 1, "type": 1, "result": 1,
             "meta.actor_type": 1, "meta.result": 1,
             "meta.contact_result": 1, "meta.confirmed": 1,
             "confirmed": 1,
             "actor": 1, "actor_type": 1, "timestamp": 1, "occurred_at": 1,
             "assignment_cycle_id": 1},
        ))
    except NetworkTimeout:
        # Activity is complementary evidence; the lead/SLA contract can still
        # be rendered without it.  A Mongo timeout must not turn the whole
        # operational dashboard into a 500 response.
        logger.warning(
            "[OPS_DASHBOARD] crm_events timeout; continuing without activity evidence"
        )
        if profile is not None:
            profile.setdefault("mongo", []).append({
                "collection": "crm_events",
                "operation": "find_activity_and_results",
                "timeout": True,
                "documents": 0,
            })
        return {}
    period_ids = {str(doc.get("_id")) for doc in period_docs if doc.get("_id") is not None}
    signals = defaultdict(lambda: {"current_activity": False, "period_activity": False,
                                   "period_result": False, "result": None,
                                   "result_events": [], "period_activity_events": [],
                                   "period_effective": False, "last_event_at": None,
                                   "last_event_type": None, "last_activity_at": None,
                                   "last_activity_type": None})
    for event in events:
        lead_id = str(event.get("lead_id"))
        doc = docs.get(lead_id)
        if not doc:
            continue
        ts = coerce_utc_datetime(event.get("timestamp") or event.get("occurred_at"))
        if not ts or ts >= period_cutoff:
            continue
        lifecycle = doc.get("lifecycle") or {}
        episode = _ops_temporal_assignment(doc, assignment_cycles)
        assigned = episode.get("assignment_start")
        if assigned and ts < assigned:
            continue
        registered = registered_outreach_evidence(
            event, assigned_at=assigned,
            assignment_cycle_id=lifecycle.get("current_assignment_cycle_id"),
        ).get("recognized")
        evidence = event_evidence(event)
        # Activity means a registered outreach action. A result event is kept
        # in the result universe and must not silently become an outreach
        # attempt just because it carries a normalized result.
        activity = bool(registered)
        result_valid = bool(evidence.get("management") and evidence.get("result"))
        if not activity and not result_valid:
            continue
        if ts and (signals[lead_id]["last_event_at"] is None or ts > signals[lead_id]["last_event_at"]):
            signals[lead_id]["last_event_at"] = ts
            signals[lead_id]["last_event_type"] = str(event.get("type") or "").upper() or None
        if activity:
            signals[lead_id]["current_activity"] = True
            if ts and (signals[lead_id]["last_activity_at"] is None or ts > signals[lead_id]["last_activity_at"]):
                signals[lead_id]["last_activity_at"] = ts
                signals[lead_id]["last_activity_type"] = str(event.get("type") or "").upper() or None
        if activity and lead_id in period_ids and ts >= period_start:
            signals[lead_id]["period_activity"] = True
            signals[lead_id]["period_activity_events"].append(str(event.get("type") or "").upper())
        if lead_id in period_ids and ts >= period_start and result_valid:
            signals[lead_id]["period_result"] = True
            normalized = normalize_result(evidence.get("result"))
            signals[lead_id]["result_events"].append({"timestamp": ts, "result": normalized})
    for signal in signals.values():
        signal["result_events"].sort(key=lambda item: item["timestamp"])
        if signal["result_events"]:
            signal["result"] = signal["result_events"][-1]["result"]
            signal["result_event_count"] = len(signal["result_events"])
    return dict(signals)


def _ops_fetch_signed_orders(db, profile: Optional[dict] = None) -> list[dict]:
    return _profile_query(
        profile,
        "visitas",
        "find_signed_orders",
        lambda: db["visitas"].find(
            {"status": {"$in": list(CANONICAL_SIGNED_ORDER_STATUSES)}},
            {"visita_code": 1, "phone": 1, "property_code": 1,
             "timeline": 1, "created_at": 1},
        ),
    )


def _ops_fetch_scheduled_events(db, cohort_leads: list[dict], profile: Optional[dict] = None) -> list[dict]:
    event_filter = {
        "lead_id": {"$in": [lead.get("_id") for lead in cohort_leads]},
        "$or": [
            {"result": {"$in": ["visita_agendada", "VISITA_AGENDADA"]}},
            {"meta.result": {"$in": ["visita_agendada", "VISITA_AGENDADA"]}},
        ],
    }
    return _profile_query(
        profile,
        "crm_events",
        "find_scheduled_events",
        lambda: db["crm_events"].find(
            event_filter,
            {"lead_id": 1, "result": 1, "meta": 1, "timestamp": 1},
        ),
    )


def build_operational_contract(current_docs: list[dict], period_docs: list[dict], period_start: Optional[str], period_end: Optional[str], now: Optional[datetime] = None, team_executives: Optional[set[str]] = None, scheduled_visit_ids: Optional[set[str]] = None, assignment_cycles: Optional[dict[str, list[dict]]] = None, activity_signals: Optional[dict[str, dict]] = None, current_summary: Optional[dict] = None) -> dict:
    """Construye CURRENT (stock) y PERIOD (desempeño) sin mezclarlos."""
    now = coerce_utc_datetime(now) or datetime.now(timezone.utc)
    _, period_cutoff = _build_chile_period_bounds(period_start, period_end)
    period_cutoff = min(period_cutoff, now)
    current = {"active_assigned": 0, "pending_first_management": 0, "open_overdue": 0,
               "hot_overdue": 0, "normal_overdue": 0, "hot_near_due": 0,
               "activity_without_result": 0, "unassigned": 0,
               "oldest_pending_calendar_minutes": None,
               "aging": {"lt_24h": 0, "d_1_3": 0, "d_4_7": 0, "gt_7d": 0}}
    period = {"assigned": 0, "managed": 0, "managed_within_sla": 0, "managed_late": 0,
              "hot_managed": 0, "hot_within_sla": 0, "hot_late": 0,
              "normal_managed": 0, "normal_within_sla": 0, "normal_late": 0,
              "hot_sla_pct": None, "normal_sla_pct": None, "p50_hot": None, "p90_hot": None,
              "p50_normal": None, "p90_normal": None,
              "hot_n": 0, "normal_n": 0, "activity_attempts": 0,
              "activity_without_result": 0, "result_breakdown": {},
              "result_event_count": 0, "result_leads": 0, "result_duplicates": [],
              "activity_event_breakdown": {}, "containment": {},
              "contact_effective": 0, "visits_scheduled": 0,
              "hot_stats": {}, "normal_stats": {},
              "temporal_inconsistent": {"hot": 0, "normal": 0, "total": 0}}
    team_filter_enabled = team_executives is not None
    if team_executives is None:
        team_executives = {
            str(doc.get("ejecutivo_asignado") or "").strip()
            for doc in [*current_docs, *period_docs]
            if str(doc.get("ejecutivo_asignado") or "").strip()
        }
    team_executives = set(team_executives)
    team_keys = {name.casefold() for name in team_executives}
    executives, intervention = {}, []
    excluded_non_team_current = 0
    excluded_non_team_period = 0
    period_evaluated = 0
    summary = {"hot_open_overdue": 0, "normal_open_overdue": 0, "hot_near_due": 0,
               "unassigned": 0, "pending_first_management": 0}

    scheduled_visit_ids = set(scheduled_visit_ids or set())
    activity_signals = activity_signals or {}
    containment_ids = {"assigned": set(), "activity": set(), "result": set(), "effective": set(), "visit": set()}
    activity_type_leads = defaultdict(set)
    activity_type_events = defaultdict(int)
    audit_aging_extreme = {"lt_24h": 0, "d_1_3": 0, "d_4_7": 0,
                           "d_8_30": 0, "d_31_60": 0, "d_61_90": 0, "gt_90d": 0}
    audit_backlog_diagnosis = defaultdict(int)
    audit_gt_90_sample = []
    for doc in current_docs:
        temporal = _ops_temporal_assignment(doc, assignment_cycles)
        state = _ops_state(doc, now, temporal["management"])
        name = str(doc.get("ejecutivo_asignado") or "Sin asignar").strip()
        is_team_assigned = not state["unassigned"] and (not team_filter_enabled or name.casefold() in team_keys)
        if not state["unassigned"] and not is_team_assigned:
            excluded_non_team_current += 1
            continue
        bucket = executives.setdefault(name, _ops_exec_bucket(name)) if is_team_assigned else None
        if state["unassigned"]:
            current["unassigned"] += 1
        else:
            current["active_assigned"] += 1
            bucket["current"]["active_load"] += 1
        if not state["unassigned"] and not state["managed"]:
            current["pending_first_management"] += 1
            bucket["current"]["pending"] += 1
            bucket["current"]["oldest_pending_minutes"] = max(bucket["current"]["oldest_pending_minutes"] or 0, state["elapsed"])
            bucket["current"]["oldest_pending_calendar_minutes"] = max(bucket["current"]["oldest_pending_calendar_minutes"] or 0, state["elapsed_calendar"])
            current["oldest_pending_calendar_minutes"] = max(current["oldest_pending_calendar_minutes"] or 0, state["elapsed_calendar"])
            if activity_signals.get(str(doc.get("_id")), {}).get("current_activity"):
                current["activity_without_result"] += 1
                bucket["current"]["activity_without_result"] += 1
            if state["elapsed_calendar"] < 1440:
                age_key = "lt_24h"
            elif state["elapsed_calendar"] < 4320:
                age_key = "d_1_3"
            elif state["elapsed_calendar"] < 10080:
                age_key = "d_4_7"
            else:
                age_key = "gt_7d"
            current["aging"][age_key] += 1
            bucket["current"]["aging"][age_key] += 1
            calendar_days = state["elapsed_calendar"] / 1440
            if calendar_days < 1:
                extreme_key = "lt_24h"
            elif calendar_days < 3:
                extreme_key = "d_1_3"
            elif calendar_days < 8:
                extreme_key = "d_4_7"
            elif calendar_days <= 30:
                extreme_key = "d_8_30"
            elif calendar_days <= 60:
                extreme_key = "d_31_60"
            elif calendar_days <= 90:
                extreme_key = "d_61_90"
            else:
                extreme_key = "gt_90d"
            audit_aging_extreme[extreme_key] += 1
            if extreme_key == "gt_90d":
                lifecycle = doc.get("lifecycle") or {}
                stages = {str(doc.get(field) or "").upper() for field in ("pipeline_stage", "stage")}
                if stages & {"CLOSED_WON", "CLOSED_LOST", "ARCHIVED"}:
                    diagnosis = "B_closed_functionally_pipeline_stale"
                elif temporal.get("temporal_inconsistent"):
                    diagnosis = "C_reassigned_without_new_management"
                elif state.get("assigned"):
                    diagnosis = "A_active_pending_first_result"
                else:
                    diagnosis = "D_legacy_or_unclassified"
                audit_backlog_diagnosis[diagnosis] += 1
                if len(audit_gt_90_sample) < 20:
                    signal = activity_signals.get(str(doc.get("_id")), {})
                    ref = "lead_" + hashlib.sha256(str(doc.get("_id")).encode("utf-8")).hexdigest()[:10]
                    audit_gt_90_sample.append({
                        "lead_ref": ref,
                        "pipeline_stage": doc.get("pipeline_stage"),
                        "stage": doc.get("stage"),
                        "assigned_at": state["assigned"].isoformat() if state.get("assigned") else None,
                        "assignment_cycle_id": (lifecycle.get("current_assignment_cycle_id") or None),
                        "first_valid_management_at": state["managed"].isoformat() if state.get("managed") else None,
                        "last_event_at": signal.get("last_event_at").isoformat() if signal.get("last_event_at") else None,
                        "last_event_type": signal.get("last_event_type"),
                        "last_activity_at": signal.get("last_activity_at").isoformat() if signal.get("last_activity_at") else None,
                        "last_activity_type": signal.get("last_activity_type"),
                        "executive": name,
                        "temperature": state["temperature"],
                        "diagnosis": diagnosis,
                    })
        if state["priority_code"] in {"hot_open_overdue", "normal_open_overdue"}:
            current["open_overdue"] += 1
            current["hot_overdue" if state["temperature"] == "HOT" else "normal_overdue"] += 1
            bucket["current"]["hot_overdue" if state["temperature"] == "HOT" else "normal_overdue"] += 1
        if state["priority_code"] == "hot_near_due":
            current["hot_near_due"] += 1
            bucket["current"]["hot_near_due"] += 1
        if state["priority_code"]:
            summary[state["priority_code"]] += 1
            prop = doc.get("prospecto") or {}
            remaining = state["elapsed"] - state["threshold"]
            intervention.append({"lead_id": str(doc.get("_id")), "priority_code": state["priority_code"],
                "priority_label": state["priority_label"], "temperature": state["temperature"],
                "elapsed_minutes": state["elapsed"], "elapsed_business_minutes": state["elapsed"],
                "elapsed_calendar_minutes": state["elapsed_calendar"], "sla_limit_minutes": state["threshold"],
                "remaining_or_overdue_minutes": remaining, "executive": name,
                "client": prop.get("nombre") or "Sin nombre", "property_reference": prop.get("codigo") or "Sin propiedad",
                "stage": doc.get("pipeline_stage") or doc.get("stage") or "Sin estado",
                "first_management": "sin primera gestión" if not state["managed"] else "gestionada"})

    # The summary is period-independent and authoritative for current stock
    # volume.  Detail remains responsible for SLA, aging and intervention
    # evidence; only the volume/load fields are replaced here.
    if current_summary is not None:
        summary_assigned = 0
        for row in current_summary.get("by_executive") or []:
            name = str(row.get("executive") or "").strip()
            count = int(row.get("active") or 0)
            if not name or (team_filter_enabled and name.casefold() not in team_keys):
                if name:
                    excluded_non_team_current += count
                continue
            bucket = executives.setdefault(name, _ops_exec_bucket(name))
            bucket["current"]["active_load"] = count
            summary_assigned += count
        current["active_assigned"] = summary_assigned
        current["unassigned"] = int(current_summary.get("unassigned") or 0)

    for doc in period_docs:
        temporal = _ops_temporal_assignment(doc, assignment_cycles)
        state = _ops_state(doc, period_cutoff, temporal["management"])
        if not state["assigned"] or state["assigned"] >= period_cutoff:
            continue
        period_evaluated += 1
        name = str(doc.get("ejecutivo_asignado") or "Sin asignar").strip()
        if state["unassigned"] or (team_filter_enabled and name.casefold() not in team_keys):
            excluded_non_team_period += 1
            continue
        bucket = executives.setdefault(name, _ops_exec_bucket(name))
        period["assigned"] += 1
        bucket["period"]["assigned"] += 1
        lead_key = str(doc.get("_id"))
        containment_ids["assigned"].add(lead_key)
        signal = activity_signals.get(str(doc.get("_id")), {})
        if signal.get("period_activity"):
            period["activity_attempts"] += 1
            bucket["period"]["activity_attempts"] += 1
            containment_ids["activity"].add(lead_key)
            bucket["_activity_ids"].add(lead_key)
            for activity_type in signal.get("period_activity_events", []):
                activity_type_events[activity_type] += 1
                activity_type_leads[activity_type].add(lead_key)
        # Activity and result are parallel sets. The gap is reconciled after
        # both sets have been collected, never by subtracting totals.
        if temporal["temporal_inconsistent"]:
            key = "hot" if state["temperature"] == "HOT" else "normal"
            bucket["_temporal_inconsistent"][key] += 1
            bucket["_temporal_inconsistent"]["total"] += 1
            period["temporal_inconsistent"][key] += 1
            period["temporal_inconsistent"]["total"] += 1
        contact_at = coerce_utc_datetime((doc.get("lifecycle") or {}).get("first_effective_contact_at"))
        if contact_at and temporal["assignment_start"] <= contact_at < period_cutoff:
            period["contact_effective"] += 1
            bucket["period"]["contact_effective"] += 1
            containment_ids["effective"].add(lead_key)
        if str(doc.get("_id")) in scheduled_visit_ids:
            period["visits_scheduled"] += 1
            bucket["period"]["visits_scheduled"] += 1
            containment_ids["visit"].add(lead_key)
        if not state["managed"] or state["managed"] >= period_cutoff:
            continue
        # The management lifecycle is the validity gate for the KPI. A raw
        # result event without the canonical lifecycle timestamp remains in
        # the audit trail, but does not enter the gerencial distribution.
        if signal.get("period_result") and signal.get("result"):
            result = signal["result"]
            event_count = int(signal.get("result_event_count") or 0)
            period["result_event_count"] += event_count
            period["result_leads"] += 1
            containment_ids["result"].add(lead_key)
            bucket["_result_ids"].add(lead_key)
            bucket["period"]["result_event_count"] += event_count
            bucket["period"]["result_leads"] += 1
            period["result_breakdown"][result] = period["result_breakdown"].get(result, 0) + 1
            bucket["period"]["result_breakdown"][result] = bucket["period"]["result_breakdown"].get(result, 0) + 1
            if event_count > 1:
                lead_ref = hashlib.sha256(str(doc.get("_id")).encode("utf-8")).hexdigest()[:10]
                period["result_duplicates"].append({
                    "lead_ref": "lead_" + lead_ref,
                    "count": event_count,
                    "sequence": [item["result"] for item in signal["result_events"]],
                    "final_result": result,
                })
        period["managed"] += 1
        bucket["period"]["managed"] += 1
        measured = _ops_response_minutes(temporal["assignment_start"], state["managed"])
        if measured is None or measured < 0:
            continue
        within = measured <= state["threshold"]
        period["managed_within_sla" if within else "managed_late"] += 1
        bucket["period"]["managed_within_sla" if within else "managed_late"] += 1
        key = "hot" if state["temperature"] == "HOT" else "normal"
        period[key + "_managed"] += 1
        period[key + ("_within_sla" if within else "_late")] += 1
        bucket["_" + key + "_managed"] += 1
        bucket["_" + key + "_within"] += 1 if within else 0
        bucket["_" + key + "_times"].append(measured)

    period["hot_sla_pct"] = round(period["hot_within_sla"] / period["hot_managed"] * 100, 1) if period["hot_managed"] else None
    period["normal_sla_pct"] = round(period["normal_within_sla"] / period["normal_managed"] * 100, 1) if period["normal_managed"] else None
    for key in ("hot", "normal"):
        values = [value for bucket in executives.values() for value in bucket["_" + key + "_times"]]
        period[key + "_n"] = len(values)
        period["p50_" + key], period["p90_" + key] = _ops_percentile(values, 50), _ops_percentile(values, 90)
        period[key + "_stats"] = _ops_stats(values)
    period["activity_event_breakdown"] = {
        key: {"events": activity_type_events[key], "leads": len(activity_type_leads[key])}
        for key in sorted(activity_type_events)
    }
    period["containment"] = {
        "activity_without_result": len(containment_ids["activity"] - containment_ids["result"]),
        "result_without_activity": len(containment_ids["result"] - containment_ids["activity"]),
        "effective_without_result": len(containment_ids["effective"] - containment_ids["result"]),
        "visit_without_effective": len(containment_ids["visit"] - containment_ids["effective"]),
        "visit_without_result": len(containment_ids["visit"] - containment_ids["result"]),
        "sets": {key: len(value) for key, value in containment_ids.items()},
    }
    period["activity_without_result"] = len(containment_ids["activity"] - containment_ids["result"])
    for bucket in executives.values():
        bucket["period"]["activity_without_result"] = len(bucket["_activity_ids"] - bucket["_result_ids"])
        bucket.pop("_activity_ids", None)
        bucket.pop("_result_ids", None)
    intervention.sort(key=lambda item: ({"hot_open_overdue": 1, "normal_open_overdue": 2, "hot_near_due": 3, "unassigned": 4, "pending_first_management": 5}.get(item["priority_code"], 6), -max(item["remaining_or_overdue_minutes"], item["elapsed_minutes"])))
    total_active = current["active_assigned"] + current["unassigned"]
    aging_total = sum(current["aging"].values())
    return {"meta": {"as_of": now.isoformat(), "period_start": period_start, "period_end": period_end,
            "timezone": "America/Santiago", "sla_hot_minutes": 60, "sla_normal_minutes": 180,
            "team_executives": sorted(team_executives, key=str.casefold),
            "excluded_non_team": excluded_non_team_current,
            "excluded_non_team_current": excluded_non_team_current,
            "excluded_non_team_period": excluded_non_team_period,
                      "first_management_source": "lifecycle.first_valid_management_at", "mongo_calls": 2,
                      "first_management_reconciliation": {
                          "total_evaluated": period_evaluated, "coincidences": period_evaluated,
                          "differences": 0, "lifecycle_present_canonical_absent": 0,
                          "lifecycle_absent_canonical_present": 0, "timestamp_differences": 0,
                          "canonical_contract": "same persisted lifecycle field"},
                      "backlog_audit": {
                          "aging_extreme": audit_aging_extreme,
                          "gt_90_count": audit_aging_extreme["gt_90d"],
                          "diagnosis_counts": dict(audit_backlog_diagnosis),
                          "sample_gt_90": audit_gt_90_sample,
                          "note": "Auditoría informativa; no cambia el universo activo ni cierra leads automáticamente."}},
            "current": current, "period": period,
            "executives": _ops_finalize_execs(executives, current["active_assigned"]),
            "intervention_summary": summary, "intervention_cases": intervention[:20],
            "total_intervention_cases": len(intervention),
            "current_reconciliation": {"active_total": total_active, "active_assigned_plus_unassigned": total_active, "ok": True},
            "aging_reconciliation": {"pending_total": current["pending_first_management"],
                                     "aging_bucket_total": aging_total,
                                     "ok": aging_total == current["pending_first_management"]},
            "updated_at": now.isoformat()}


def query_leads_operational_dashboard(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    filters: Optional[dict] = None,
    timing: Optional[dict] = None,
    period_only: bool = False,
    team_executives_override: Optional[set[str]] = None,
    shared_resources: Optional[dict] = None,
) -> dict:
    """Query operacional separando stock vigente de métricas de período.

    ``period_only`` se usa para el comparable: ese universo solo necesita
    métricas de cohorte y no debe volver a leer ni transformar el stock actual.
    """
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    raw_filters = dict(filters or {})
    portfolio = str(raw_filters.pop("portfolio", None) or "").strip()
    portfolio_codes = _ops_portfolio_codes(db, portfolio)
    mongo_filters, python_filters = _ops_filters(raw_filters)
    if portfolio:
        # El filtro es una intersección real: cartera/captador -> código de
        # propiedad -> prospecto.codigo del lead. Si no hay códigos, el
        # resultado debe ser vacío y no un fallback a toda la operación.
        portfolio_condition = {"prospecto.codigo": {"$in": portfolio_codes or ["__NO_PORTFOLIO_MATCH__"]}}
        mongo_filters = {"$and": [mongo_filters, portfolio_condition]} if mongo_filters else portfolio_condition
    active_parts = [build_active_filter()] + ([mongo_filters] if mongo_filters else [])
    active_match = {"$and": active_parts}
    assigned_date = {"$convert": {"input": "$lifecycle.assigned_at", "to": "date", "onError": None, "onNull": None}}
    period_parts = [{"$expr": {"$and": [{"$gte": [assigned_date, start_utc]}, {"$lt": [assigned_date, end_utc]}]}}]
    if mongo_filters:
        period_parts.append(mongo_filters)
    period_match = {"$and": period_parts}
    # Stage 1: these reads have no dependency on one another.  Each worker
    # keeps an isolated timing profile; the main thread merges it afterwards
    # so shared_resources and the commercial contract remain deterministic.
    period_projection = dict(_OPS_PROJECTED_LEAD_PROJECTION)
    period_projection.pop("prospecto.nombre", None)
    users_needed = team_executives_override is None or bool(portfolio)

    def _timed_read(reader):
        worker_profile = {}
        started = time.perf_counter()
        value = reader(worker_profile)
        return value, (time.perf_counter() - started) * 1000, worker_profile

    stage1_started = time.perf_counter()
    stage1_jobs = {}
    shared_historical_base = (
        shared_resources.get("historical_base")
        if shared_resources is not None else None
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        if not period_only:
            stage1_jobs["current"] = executor.submit(
                _timed_read,
                lambda worker_profile: _ops_current_resource(
                    db, active_match, worker_profile
                ),
            )
        if shared_historical_base is None:
            stage1_jobs["historical_base"] = executor.submit(
                _timed_read,
                lambda worker_profile: _ops_historical_base(
                    db, mongo_filters, period_projection, worker_profile
                ),
            )
        if users_needed:
            stage1_jobs["users"] = executor.submit(
                _timed_read,
                lambda worker_profile: _ops_active_executive_names(db, worker_profile),
            )

        stage1_results = {name: future.result() for name, future in stage1_jobs.items()}

    current_resource, current_ms, current_profile = stage1_results.get("current", ({}, 0, {}))
    current_docs = current_resource.get("detail_docs", []) if isinstance(current_resource, dict) else []
    current_summary = current_resource.get("summary") if isinstance(current_resource, dict) else None
    if shared_historical_base is None:
        historical_base, historical_base_ms, historical_base_profile = stage1_results["historical_base"]
    else:
        historical_base = shared_historical_base
        historical_base_ms = 0.0
        historical_base_profile = {}
    period_slice_started = time.perf_counter()
    period_docs = _ops_slice_historical_base(historical_base, start_utc, end_utc)
    period_ms = (time.perf_counter() - period_slice_started) * 1000
    period_profile = historical_base_profile
    if shared_resources is not None and shared_historical_base is None:
        shared_resources["historical_base"] = historical_base
    active_names, users_ms, users_profile = stage1_results.get("users", (set(), 0, {}))
    if timing is not None:
        for profile in (current_profile, period_profile, users_profile):
            timing.setdefault("mongo", []).extend(profile.get("mongo", []))
        timing["historical_base_load_ms"] = round(historical_base_ms, 1)
        timing["historical_base_docs"] = len(historical_base)
        timing["period_slice_python_ms"] = round(period_ms, 1)
        timing["period_cohort_source"] = "historical_base_cache"
        timing["stage1_wall_ms"] = round((time.perf_counter() - stage1_started) * 1000, 1)
        if isinstance(current_resource, dict):
            timing.update({
                "current_resource_elapsed_ms": current_resource.get("elapsed_ms"),
                "current_summary_rows": current_resource.get("summary_rows"),
                "current_summary_bson_bytes": current_resource.get("summary_bson_bytes"),
                "current_detail_docs": len(current_docs),
                "current_detail_bson_bytes": current_resource.get("detail_bson_bytes"),
            })

    period_cutoff = min(end_utc, datetime.now(timezone.utc))
    portfolio_context = None
    if portfolio:
        team_names = {name.casefold() for name in active_names}
        outside_counts: dict[str, int] = {}
        outside_cases: list[dict] = []
        unassigned_cases: list[dict] = []
        unassigned_count = 0
        active_team_count = 0
        summary_for_portfolio = current_summary or {}
        for doc in current_docs:
            name = str(doc.get("ejecutivo_asignado") or "").strip()
            if _ops_unassigned(name):
                unassigned_count += 1
                unassigned_cases.append({"lead_id": str(doc.get("_id")), "executive": "Sin asignar", "client": (doc.get("prospecto") or {}).get("nombre") or "Sin nombre", "property_reference": (doc.get("prospecto") or {}).get("codigo") or "Sin propiedad"})
            elif name.casefold() in team_names:
                active_team_count += 1
            else:
                outside_counts[name or "Sin nombre"] = outside_counts.get(name or "Sin nombre", 0) + 1
                outside_cases.append({"lead_id": str(doc.get("_id")), "executive": name or "Sin nombre", "client": (doc.get("prospecto") or {}).get("nombre") or "Sin nombre", "property_reference": (doc.get("prospecto") or {}).get("codigo") or "Sin propiedad"})
        if current_summary is not None:
            outside_counts = {
                str(row.get("executive") or "Sin nombre"): int(row.get("active") or 0)
                for row in current_summary.get("by_executive") or []
                if str(row.get("executive") or "").strip().casefold() not in team_names
            }
            active_team_count = sum(
                int(row.get("active") or 0)
                for row in current_summary.get("by_executive") or []
                if str(row.get("executive") or "").strip().casefold() in team_names
            )
            unassigned_count = int(current_summary.get("unassigned") or 0)
        portfolio_context = {
            "captador": portfolio,
            "property_codes": len(portfolio_codes),
            "active_total": int((current_summary or {}).get("active_total") or len(current_docs)),
            "active_team": active_team_count,
            "unassigned": unassigned_count,
            "outside_team": sum(outside_counts.values()),
            "outside_breakdown": [
                {"executive": name, "active": count}
                for name, count in sorted(outside_counts.items(), key=lambda item: (-item[1], item[0].casefold()))
            ],
            "outside_cases": outside_cases,
            "unassigned_cases": unassigned_cases,
        }
    period_lead_ids = {str(doc.get("_id")) for doc in period_docs}
    # Stage 2: assignment cycles depend on the lead documents; signed orders
    # and scheduled-event evidence do not depend on cycles or on one another.
    stage2_started = time.perf_counter()
    stage2_jobs = {}
    signed_orders = None
    shared_signed_orders = (
        shared_resources.get("signed_orders")
        if shared_resources is not None else None
    )
    with ThreadPoolExecutor(max_workers=3) as executor:
        stage2_jobs["assignment"] = executor.submit(
            _timed_read,
            lambda worker_profile: _ops_assignment_episode_map(
                db, [*current_docs, *period_docs], worker_profile
            ),
        )
        if period_lead_ids and shared_signed_orders is None:
            stage2_jobs["signed"] = executor.submit(
                _timed_read,
                lambda worker_profile: _ops_fetch_signed_orders(db, worker_profile),
            )
        if period_lead_ids:
            stage2_jobs["scheduled"] = executor.submit(
                _timed_read,
                lambda worker_profile: _ops_fetch_scheduled_events(db, period_docs, worker_profile),
            )
        stage2_results = {name: future.result() for name, future in stage2_jobs.items()}

    assignment_cycles, assignment_ms, assignment_profile = stage2_results["assignment"]
    if shared_signed_orders is not None:
        signed_orders = shared_signed_orders
        signed_ms = 0.0
        signed_profile = {}
    elif "signed" in stage2_results:
        signed_orders, signed_ms, signed_profile = stage2_results["signed"]
    else:
        signed_ms, signed_profile = 0.0, {}
    scheduled_events, scheduled_ms, scheduled_profile = stage2_results.get("scheduled", ([], 0.0, {}))
    if timing is not None:
        for profile in (assignment_profile, signed_profile, scheduled_profile):
            timing.setdefault("mongo", []).extend(profile.get("mongo", []))
        timing["stage2_wall_ms"] = round((time.perf_counter() - stage2_started) * 1000, 1)
    if shared_resources is not None and shared_signed_orders is None and signed_orders is not None:
        shared_resources["signed_orders"] = signed_orders

    activity_started = time.perf_counter()
    activity_signals = _ops_collect_activity_signals(
        db, current_docs, period_docs, start_utc, period_cutoff,
        assignment_cycles,
        timing,
    )
    activity_ms = (time.perf_counter() - activity_started) * 1000
    scheduled_visit_ids = _scheduled_visit_lead_ids(
        period_docs,
        period_lead_ids,
        period_cutoff,
        timing,
        signed_orders=signed_orders,
        scheduled_events=scheduled_events if period_lead_ids else None,
    ) if period_lead_ids else set()
    transform_started = time.perf_counter()
    team_executives = team_executives_override or active_names
    result = build_operational_contract(
        current_docs,
        period_docs,
        period_start,
        period_end,
        team_executives=team_executives,
        scheduled_visit_ids=scheduled_visit_ids,
        assignment_cycles=assignment_cycles,
        activity_signals=activity_signals,
        current_summary=current_summary,
    )
    transform_ms = (time.perf_counter() - transform_started) * 1000
    if python_filters:
        cases = result["intervention_cases"]
        if python_filters.get("priority"):
            selected = python_filters["priority"]
            if selected == "open_overdue":
                cases = [case for case in cases if case["priority_code"] in {"hot_open_overdue", "normal_open_overdue"}]
            else:
                cases = [case for case in cases if case["priority_code"] == selected]
        if python_filters.get("search"):
            term = str(python_filters["search"]).strip().lower()
            cases = [case for case in cases if term in str(case.get("client") or "").lower()]
        result["intervention_cases"] = cases[:20]
        result["total_intervention_cases"] = len(cases)
    actual_mongo_calls = len((timing or {}).get("mongo", [])) if timing is not None else (5 if period_lead_ids else 3) + (1 if assignment_cycles else 0)
    result["meta"]["mongo_calls"] = actual_mongo_calls
    result["meta"]["visit_evidence_leads"] = len(scheduled_visit_ids)
    result["meta"]["assignment_cycle_leads"] = len(assignment_cycles)
    result["meta"]["portfolio"] = portfolio or None
    result["meta"]["portfolio_property_codes"] = len(portfolio_codes) if portfolio else None
    result["meta"]["portfolio_context"] = portfolio_context
    if timing is not None:
        timing.update({"mongo_calls": actual_mongo_calls, "current_query_ms": round(current_ms, 1), "period_query_ms": round(period_ms, 1), "users_query_ms": round(users_ms, 1), "assignment_cycles_ms": round(assignment_ms, 1), "signed_orders_ms": round(signed_ms, 1), "scheduled_events_ms": round(scheduled_ms, 1), "activity_results_ms": round(activity_ms, 1), "transform_ms": round(transform_ms, 1), "visit_evidence_leads": len(scheduled_visit_ids), "assignment_cycle_leads": len(assignment_cycles)})
    return result


def query_variance_drivers(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    comparison_start: Optional[str] = None,
    comparison_end: Optional[str] = None,
    include_comparison: bool = True,
    filters: Optional[dict] = None,
) -> dict:
    """Aggregate comparable lead volume by source, executive and commune once."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    current_match = _build_commercial_cohort_match(start_utc, end_utc, filters)
    if include_comparison and comparison_start and comparison_end:
        previous_start, previous_end = _build_chile_period_bounds(comparison_start, comparison_end)
        previous_match = _build_commercial_cohort_match(previous_start, previous_end, filters)
    else:
        previous_match = None

    dimensions = {
        "source": ("$prospecto.origen", "Sin fuente"),
        "executive": ("$ejecutivo_asignado", "Sin ejecutivo"),
        "commune": ("$prospecto.comuna", "Sin comuna"),
    }

    def segment_expression(field, fallback):
        return {"$cond": [{"$or": [{"$eq": [field, None]}, {"$eq": [field, ""]}]}, fallback, field]}

    def normalize_segment(value, fallback):
        text = "" if value is None else str(value).strip()
        if text.lower() in {"", "none", "null", "n/a", "sin informacion", "sin informaciÃ³n"}:
            return fallback
        return text

    def facet(match, field):
        return [
            {"$match": match},
            {"$group": {"_id": "$_id", "segment": {"$first": segment_expression(field[0], field[1])}}},
            {"$group": {"_id": "$segment", "count": {"$sum": 1}}},
            {"$project": {"segment": "$_id", "count": 1, "_id": 0}},
        ]

    facets = {
        "current_total": [{"$match": current_match}, {"$group": {"_id": "$_id"}}, {"$count": "count"}],
        "previous_total": ([{"$match": previous_match}, {"$group": {"_id": "$_id"}}, {"$count": "count"}] if previous_match else []),
    }
    for name, field in dimensions.items():
        facets[f"current_{name}"] = facet(current_match, field)
        facets[f"previous_{name}"] = facet(previous_match, field) if previous_match else []

    raw = list(db["leads"].aggregate([_normalized_created_at_stage(), {"$facet": facets}]))
    row = raw[0] if raw else {}
    current_total = (row.get("current_total") or [{}])[0].get("count", 0)
    previous_total = (row.get("previous_total") or [{}])[0].get("count", 0) if previous_match else None
    total_delta = current_total - previous_total if previous_total is not None else None
    result = {}
    for name in dimensions:
        fallback = dimensions[name][1]
        current = {normalize_segment(item.get("segment"), fallback): item.get("count", 0) for item in row.get(f"current_{name}", [])}
        previous = {normalize_segment(item.get("segment"), fallback): item.get("count", 0) for item in row.get(f"previous_{name}", [])}
        segments = []
        for key in sorted(set(current) | set(previous)):
            current_count, previous_count = current.get(key, 0), previous.get(key, 0)
            segments.append({"key": key, "label": key, "current": current_count, "previous": previous_count, "delta": current_count - previous_count})
        reconciliation_delta = sum(item["delta"] for item in segments) - total_delta if total_delta is not None else None
        restricted = bool(filters and (
            (name == "source" and filters.get("source"))
            or (name == "executive" and (filters.get("executive") or filters.get("ejecutivo_asignado")))
            or (name == "commune" and filters.get("commune"))
        ))
        result[name] = {
            "restricted_by_filter": restricted,
            "reconciliation_delta": reconciliation_delta,
            "segments": segments,
        }
    return {
        "current_total": current_total,
        "previous_total": previous_total,
        "total_delta": total_delta,
        "comparable_available": bool(previous_match),
        "dimensions": result,
    }


def query_executive_contribution(*args, **kwargs) -> dict:
    """Compatibility adapter for callers using the former contribution shape."""
    variance = query_variance_drivers(*args, **kwargs)
    dimensions = {}
    for name, payload in variance["dimensions"].items():
        dimensions[name] = {
            "current": [{"segment": item["label"], "count": item["current"]} for item in payload["segments"] if item["current"]],
            "previous": [{"segment": item["label"], "count": item["previous"]} for item in payload["segments"] if item["previous"]],
        }
    return {"available": variance["comparable_available"], "dimensions": dimensions}



COMMERCIAL_FUNNEL_STAGES = [
    ("received", "Leads recibidos"),
    ("visit_intent", "Intenci\u00f3n de visita"),
    ("data_delivered", "Datos entregados"),
    ("visit_scheduled", "Visita coordinada"),
    ("visit_done", "Visita realizada"),
    ("negotiation", "Negociaci\u00f3n"),
    ("closed_won", "Cierre ganado"),
]

VISIT_RESULTS = frozenset({"VISITA_SOLICITADA", "VISITA_AGENDADA", "ASK_VISIT", "AGENDAR_VISITA"})

PRICE_RANGES_UF = [
    ("0-2500", 0, 2500),
    ("2500-4000", 2500, 4000),
    ("4000-6000", 4000, 6000),
    ("6000-10000", 6000, 10000),
    ("10000+", 10000, None),
]

PRICE_RANGES_CLP = [
    ("0-400k", 0, 400000),
    ("400k-600k", 400000, 600000),
    ("600k-900k", 600000, 900000),
    ("900k-1.5M", 900000, 1500000),
    ("1.5M+", 1500000, None),
]

STAGE_ORDER = {
    "NEW": 0, "CONTACTED": 1, "INTERESTED": 2,
    "VISIT_SCHEDULED": 3, "VISIT_DONE": 4,
    "OFFER": 5, "NEGOTIATION": 6,
    "CLOSED_WON": 7, "CLOSED_LOST": 8,
}

NON_COMMERCIAL_SOURCES = frozenset({
    None, "", "Sin informacion", "Sin informaci\u00f3n", "Sin informaci",
    "Desconocido", "unknown", "Unknown", "N/A", "n/a", "__NULL__", "null", "Null",
})


def coerce_utc(value):
    """Convert various date formats to UTC datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return CHILE_TZ.localize(value).astimezone(timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def _coverage_pct(populated, total):
    return round(populated / total * 100, 1) if total else None


def _stage_reached_via_history(lead, stage_upper, cutoff_utc=None):
    """Check if a lead reached a given stage BEFORE cutoff_utc.
    
    Uses stage_history with timestamps < cutoff_utc as evidence.
    Only uses current pipeline_stage as fallback when:
    - cutoff_utc is None (no historical cutoff, showing current state)
    - OR the lead's creation is BEFORE cutoff and there's no stage_history
      (we must conservatively assume current state might have been reached later)
    - OR we can verify the current state existed before cutoff via other means.
    
    Returns True/False when evidence exists, None when undetermined.
    """
    from datetime import timezone
    if cutoff_utc is None:
        cutoff_utc = datetime.now(timezone.utc)
    
    required_order = STAGE_ORDER.get(stage_upper, 99)
    
    # First: check stage_history with timestamps < cutoff
    reached_in_history = False
    has_untimed_history = False
    for entry in lead.get("stage_history") or []:
        to_stage = str(entry.get("to") or "").upper()
        ts = entry.get("timestamp")
        entry_order = STAGE_ORDER.get(to_stage, -1)
        if entry_order < required_order:
            continue
        if ts:
            try:
                from datetime import datetime as dt
                from chatbot.constants import CHILE_TZ
                if isinstance(ts, str):
                    ts_dt = dt.fromisoformat(ts.replace("Z", "+00:00"))
                else:
                    ts_dt = ts
                if ts_dt.tzinfo is None:
                    ts_dt = CHILE_TZ.localize(ts_dt)
                if ts_dt.astimezone(timezone.utc) < cutoff_utc:
                    return True
            except Exception:
                has_untimed_history = True
        else:
            has_untimed_history = True
    
    # Second: check current pipeline_stage ONLY if we can verify it's before cutoff
    current = str(lead.get("pipeline_stage") or lead.get("stage") or "").upper()
    current_order = STAGE_ORDER.get(current, -1)
    
    if current_order >= required_order:
        # Current state suggests the stage was reached.
        # But we can only use this as evidence for historical periods if:
        # 1. No cutoff (showing current state) OR
        # 2. The lead has stage_history and the current stage appears in it before cutoff
        if cutoff_utc is None:
            return True
        # Check if the current stage appears in stage_history before cutoff
        for entry in lead.get("stage_history") or []:
            to_stage = str(entry.get("to") or "").upper()
            if to_stage == current:
                ts = entry.get("timestamp")
                if ts:
                    try:
                        from datetime import datetime as dt
                        from chatbot.constants import CHILE_TZ
                        if isinstance(ts, str):
                            ts_dt = dt.fromisoformat(ts.replace("Z", "+00:00"))
                        else:
                            ts_dt = ts
                        if ts_dt.tzinfo is None:
                            ts_dt = CHILE_TZ.localize(ts_dt)
                        if ts_dt.astimezone(timezone.utc) < cutoff_utc:
                            return True
                    except Exception:
                        pass
        
        # If we have untimed history OR no history at all, we cannot confirm
        # the stage was reached before cutoff. Return None (undetermined).
        if has_untimed_history or not lead.get("stage_history"):
            return None  # Undetermined - evidence insufficient
    
    if has_untimed_history:
        return None  # Undetermined
    
    return False


# =============================================================================
# 1. TEMPERATURE COVERAGE
# =============================================================================

def _query_temperature_coverage_legacy(period_start=None, period_end=None):
    """Report temperature data coverage for the period."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    pipeline = [
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(start_utc, end_utc, filters)},
        {"$addFields": {
            "_has_history": {"$cond": [{"$gt": [{"$size": {"$ifNull": ["$temperature_history", []]}}, 0]}, 1, 0]},
            "_has_current": {"$cond": [{"$in": ["$lead_temperature_effective", ["HOT", "COLD"]]}, 1, 0]},
        }},
        {"$group": {
            "_id": None,
            "total": {"$sum": 1},
            "with_history": {"$sum": "$_has_history"},
            "with_current_temp": {"$sum": "$_has_current"},
        }},
    ]
    r = list(db["leads"].aggregate(pipeline))
    if not r:
        return {"total": 0, "with_history": 0, "with_current_temp": 0, "history_coverage_pct": None}
    row = r[0]
    return {
        "total": row["total"],
        "with_history": row["with_history"],
        "with_current_temp": row["with_current_temp"],
        "history_coverage_pct": _coverage_pct(row["with_history"], row["total"]),
        "note": "Temperatura hist\u00f3rica solo cuando existe temperature_history con timestamp.",
    }


# Temperature-at-close is demonstrable only from timestamped history events.
def _temperature_at_cutoff(history, cutoff_utc):
    valid = []
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("value") or entry.get("temperature") or "").upper()
        raw_ts = entry.get("at") or entry.get("timestamp") or entry.get("changed_at")
        if value not in ("HOT", "COLD") or not raw_ts:
            continue
        try:
            ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")) if isinstance(raw_ts, str) else raw_ts
            if ts.tzinfo is None:
                ts = CHILE_TZ.localize(ts)
            ts = ts.astimezone(timezone.utc)
        except (TypeError, ValueError, AttributeError):
            continue
        if ts < cutoff_utc:
            valid.append((ts, value))
    return max(valid, key=lambda item: item[0])[1] if valid else None


def _summarize_temperature_history(leads, cutoff_utc):
    by_id = {}
    for lead in leads:
        lead_id = lead.get("_id")
        if lead_id not in by_id:
            by_id[lead_id] = _temperature_at_cutoff(lead.get("temperature_history"), cutoff_utc)
    total = len(by_id)
    hot_count = sum(value == "HOT" for value in by_id.values())
    cold_count = sum(value == "COLD" for value in by_id.values())
    with_history = hot_count + cold_count
    without_history = total - with_history
    return {
        "total": total, "with_history": with_history, "without_history": without_history,
        "hot": hot_count if with_history else None,
        "cold": cold_count if with_history else None,
        "history_coverage_pct": _coverage_pct(with_history, total),
        "reconciles": hot_count + cold_count + without_history == total,
        "unit": "lead._id", "comparative_metrics": None,
        "comparative_metrics_note": "Tasas Hot vs. Cold no disponibles sin cobertura histórica suficiente.",
    }


def query_temperature_coverage(period_start=None, period_end=None, filters=None):
    """Demonstrable HOT/COLD snapshot at period close; current state is never used."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    pipeline = [_normalized_created_at_stage(), {"$match": {"$expr": {"$and": [
        {"$gte": ["$_created_normalized", start_utc]}, {"$lt": ["$_created_normalized", end_utc]},
    ]}}}]
    extra = _build_extra_filter(filters) or {}
    if extra:
        pipeline.append({"$match": extra})
    pipeline.append({"$project": {"_id": 1, "temperature_history": 1}})
    result = _summarize_temperature_history(list(db["leads"].aggregate(pipeline)), end_utc)
    result.update({
        "with_current_temp": None,
        "note": "Temperatura histórica al cierre: último evento HOT/COLD con timestamp anterior al corte.",
    })
    return result


# =============================================================================
# 2. COMMERCIAL KPIs (CORRECTED)
# =============================================================================

def query_commercial_kpis(period_start=None, period_end=None, filters=None,
                          comparison_start=None, comparison_end=None,
                          include_comparison=True):
    """
    Six main KPIs with period-over-period comparison.
    
    CORREGIDO v3:
    - Hot usa SOLO temperature_history (sin lead_temperature_effective)
    - Visitas: pipeline_stage actual
    - Cierres: pipeline_stage actual
    - Universos explícitos en cada KPI
    """
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    if not include_comparison:
        prev_start = prev_end = None
    elif comparison_start and comparison_end:
        prev_start, prev_end = _build_chile_period_bounds(comparison_start, comparison_end)
    else:
        duration = end_utc - start_utc
        prev_end = start_utc
        prev_start = prev_end - duration

    extra = _build_extra_filter(filters) or {}

    def _cohort_count(extra_cond=None, start_dt=start_utc, end_dt=end_utc):
        match = {"$and": [{"$expr": {"$and": [
            {"$gte": ["$_created_normalized", start_dt]},
            {"$lt": ["$_created_normalized", end_dt]},
        ]}}]}
        if extra_cond:
            match["$and"].append(extra_cond)
        r = list(db["leads"].aggregate([_normalized_created_at_stage(), {"$match": match}, {"$count": "c"}]))
        return r[0]["c"] if r else 0

    def _stage_count(stage, start_dt, end_dt):
        pipeline = [
            _normalized_created_at_stage(),
            {"$match": {"$expr": {"$and": [
                {"$gte": ["$_created_normalized", start_dt]},
                {"$lt": ["$_created_normalized", end_dt]},
            ]}}},
        ]
        if extra:
            pipeline.append({"$match": extra})
        pipeline.append({"$match": {"pipeline_stage": stage}})
        pipeline.append({"$count": "c"})
        r = list(db["leads"].aggregate(pipeline))
        return r[0]["c"] if r else 0

    # KPI 1: Leads recibidos
    received = _cohort_count(extra)
    received_prev = _cohort_count(extra, prev_start, prev_end) if include_comparison else None

    # KPI 2: temperatura histórica demostrable al cierre (SOLO temperature_history)
    temp_cov = query_temperature_coverage(period_start, period_end, filters)
    previous_temp = (query_temperature_coverage(
        prev_start.date().isoformat(), (prev_end - timedelta(days=1)).date().isoformat(), filters
    ) if include_comparison else {"hot": None})
    hot = temp_cov["hot"]
    hot_prev = previous_temp["hot"]

    # Hot actual (lead_temperature_effective) como KPI separado
    def _hot_current(start_dt, end_dt):
        p = [
            _normalized_created_at_stage(),
            {"$match": {"$expr": {"$and": [
                {"$gte": ["$_created_normalized", start_dt]},
                {"$lt": ["$_created_normalized", end_dt]},
            ]}}},
        ]
        if extra:
            p.append({"$match": extra})
        p.extend([
            {"$match": {"lead_temperature_effective": "HOT"}},
            {"$count": "c"},
        ])
        r = list(db["leads"].aggregate(p))
        return r[0]["c"] if r else 0

    hot_current = _hot_current(start_utc, end_utc)

    # KPI 3: Intención de visita
    def _visit_intent(start_dt, end_dt):
        p = [
            _normalized_created_at_stage(),
            {"$match": {"$expr": {"$and": [
                {"$gte": ["$_created_normalized", start_dt]},
                {"$lt": ["$_created_normalized", end_dt]},
            ]}}},
        ]
        if extra:
            p.append({"$match": extra})
        p.extend([
            {"$addFields": {
                "_has_vi": {"$cond": [{"$or": [
                    {"$in": [{"$ifNull": ["$bi_analytics_global.RESULTADO_CHAT", ""]}, list(VISIT_RESULTS)]},
                    {"$in": [{"$ifNull": ["$last_intent", ""]}, list(VISIT_RESULTS)]},
                    {"$in": [{"$ifNull": ["$pipeline_stage", ""]}, ["VISIT_SCHEDULED", "VISIT_DONE"]]},
                ]}, 1, 0]},
            }},
            {"$match": {"_has_vi": 1}},
            {"$count": "c"},
        ])
        r = list(db["leads"].aggregate(p))
        return r[0]["c"] if r else 0

    visit_intent = _visit_intent(start_utc, end_utc)
    visit_intent_prev = _visit_intent(prev_start, prev_end) if include_comparison else None

    # KPI 4: Visitas coordinadas (pipeline_stage actual)
    visit_scheduled = _stage_count("VISIT_SCHEDULED", start_utc, end_utc)
    visit_scheduled_prev = _stage_count("VISIT_SCHEDULED", prev_start, prev_end) if include_comparison else None

    # SLA is calculated once by query_sla_risk_panel using the canonical CRM
    # business-minute policy. The service attaches that result to this legacy
    # KPI field after both read-only queries complete.
    sla_pct_val = None

    # KPI 6: Cierres (pipeline_stage actual)
    closed_won = _stage_count("CLOSED_WON", start_utc, end_utc)
    closed_won_prev = _stage_count("CLOSED_WON", prev_start, prev_end) if include_comparison else None

    def _var(cur, prev):
        if cur is not None and prev is not None and prev > 0:
            return round((cur - prev) / prev * 100, 1)
        return None

    return {
        "leads_received": {"value": received, "previous": received_prev, "variation_pct": _var(received, received_prev), "universe": "cohorte"},
        "leads_hot_history": {"value": hot, "previous": hot_prev, "variation_pct": _var(hot, hot_prev), "universe": "cohorte_temperature_history"},
        "leads_cold_history": {"value": temp_cov["cold"], "universe": "cohorte_temperature_history"},
        "temperature_at_close": temp_cov,
        "leads_hot_current": {"value": hot_current, "universe": "temperatura_actual"},
        "visit_intent": {"value": visit_intent, "previous": visit_intent_prev, "variation_pct": _var(visit_intent, visit_intent_prev), "universe": "cohorte"},
        "visits_scheduled": {"value": visit_scheduled, "previous": visit_scheduled_prev, "variation_pct": _var(visit_scheduled, visit_scheduled_prev), "universe": "cohorte"},
        "sla_compliance": {"value": sla_pct_val, "previous": None, "pp_change": None, "universe": "sla_risk_panel", "sla_policy": "SLA: minutos h\u00e1biles"},
        "closed_won": {"value": closed_won, "previous": closed_won_prev, "variation_pct": _var(closed_won, closed_won_prev), "universe": "cohorte"},
        "_meta": {
            "period_start": period_start, "period_end": period_end,
            "timezone": "America/Santiago",
            "cutoff_utc": end_utc.isoformat(),
            "temperature_coverage": temp_cov,
            "note": "Temperatura histórica al cierre != temperatura actual; ninguna acredita gestión.",
        },
    }

def query_commercial_funnel(period_start=None, period_end=None, filters=None):
    """Funnel using stage_reached (historical), not pipeline_stage (current).
    Universo: cohorte (leads creados en el per\u00edodo).
    CORREGIDO: Aplica cutoff_utc = end_utc para todas las etapas hist\u00f3ricas.
    No usa last_intent o RESULTADO_CHAT sin timestamp como evidencia hist\u00f3rica.
    """
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    cutoff_utc = end_utc  # Strict cutoff at period end
    pipeline = [
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(start_utc, end_utc, filters)},
        {"$project": {
            "pipeline_stage": 1, "stage": 1, "stage_history": 1,
            "lead_temperature_effective": 1, "temperature_history": 1,
            "bi_analytics_global.RESULTADO_CHAT": 1,
            "bi_analytics_global.INTENCION_CLIENTE": 1, "last_intent": 1,
            "prospecto.email": 1, "prospecto.rut": 1,
        }},
    ]
    all_leads = list(db["leads"].aggregate(pipeline))

    def _reached(lead, key):
        if key == "received":
            return True
        if key == "visit_intent":
            result = _stage_reached_via_history(lead, "VISIT_SCHEDULED", cutoff_utc)
            if result is True:
                return True
            return False
        if key == "data_delivered":
            # Must have visit intent AND have data
            if not _reached(lead, "visit_intent"):
                return False
            p = lead.get("prospecto") or {}
            return bool(p.get("email") and p.get("rut") and str(p["email"]).strip() and str(p["rut"]).strip())
        stage_map = {
            "visit_scheduled": "VISIT_SCHEDULED",
            "visit_done": "VISIT_DONE",
            "negotiation": "NEGOTIATION",
            "closed_won": "CLOSED_WON",
        }
        mapped = stage_map.get(key)
        if mapped:
            if not _reached(lead, "data_delivered"):
                return False
            result = _stage_reached_via_history(lead, mapped, cutoff_utc)
            return result is True
        return False

    received = len(all_leads)
    stages = {}
    for key, _ in COMMERCIAL_FUNNEL_STAGES[1:]:
        stages[key] = sum(1 for lead in all_leads if _reached(lead, key))

    result = []
    for i, (key, label) in enumerate(COMMERCIAL_FUNNEL_STAGES):
        count = received if key == "received" else stages.get(key, 0)
        if i == 0:
            prev_c = received
        else:
            prev_key = COMMERCIAL_FUNNEL_STAGES[i - 1][0]
            prev_c = received if prev_key == "received" else stages.get(prev_key, 0)
        result.append({
            "key": key, "label": label, "count": count,
            "pct_of_received": _coverage_pct(count, received),
            "conversion_from_prev": _coverage_pct(count, prev_c) if i > 0 else None,
            "leakage": (prev_c - count) if i > 0 else None,
            "leakage_pct": _coverage_pct(prev_c - count, prev_c) if i > 0 and prev_c > 0 else None,
            "universe": "cohorte_creados_en_periodo",
        })
    return result


# =============================================================================
# 4. SLA RISK PANEL (CORREGIDO)
# =============================================================================

def _sla_percentile(values, percentile):
    values = sorted(
        number for value in values
        if value is not None
        for number in [float(value)]
        if math.isfinite(number)
    )
    if not values:
        return None
    position = (len(values) - 1) * percentile / 100
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return round(values[lower] + (values[upper] - values[lower]) * (position - lower), 1)


def _sla_bucket(threshold_minutes=180):
    return {
        "eligible": 0,
        "managed_within": 0, "managed_outside": 0,
        "open_within": 0, "open_normal": 0, "attention": 0,
        "near_breach": 0, "breached": 0, "open_breached": 0,
        "not_evaluable": 0,
        "in_sla_count": 0, "out_sla_count": 0, "in_sla_pct": None,
        "resolved_compliance_pct": None,
        "median_minutes": None, "p90_minutes": None, "managed_sample": 0,
        "threshold_minutes": threshold_minutes,
        "_managed_minutes": [],
    }


def _resolve_hot_start_evidence(lead: Mapping[str, Any]) -> Optional[datetime]:
    """Timestamp canónico de inicio HOT por jerarquía de evidencia real.

    1. temperature_history con valor HOT y timestamp.
    2. lifecycle.hot_since.
    3. last_intent_at cuando la intención es HOT.
    4. timestamp mínimo de alerta comercial compatible (prospecto.alerts_sent).
    5. stage_history con etapa HOT / lifecycle.visit_scheduled_at.

    Nunca usa lead_temperature_effective_updated_at, assigned_at arbitrario ni
    fechas inventadas. Devuelve None cuando no hay evidencia.
    """
    lifecycle = lead.get("lifecycle") or {}

    for entry in lead.get("temperature_history") or []:
        if isinstance(entry, Mapping) and str(entry.get("value") or entry.get("temperature") or "").upper() == "HOT":
            timestamp = coerce_utc_datetime(entry.get("at") or entry.get("timestamp"))
            if timestamp:
                return timestamp

    timestamp = coerce_utc_datetime(lifecycle.get("hot_since"))
    if timestamp:
        return timestamp

    intent = str(lead.get("last_intent") or "").upper()
    if intent in HOT_INTENTS:
        timestamp = coerce_utc_datetime(lead.get("last_intent_at"))
        if timestamp:
            return timestamp

    prospecto = lead.get("prospecto") or {}
    raw_alerts = prospecto.get("alerts_sent") or lead.get("alerts_sent") or {}
    if isinstance(raw_alerts, Mapping):
        alert_times = []
        for alert_type, raw_at in raw_alerts.items():
            if alert_type in COMMERCIAL_ALERT_TYPES:
                timestamp = coerce_utc_datetime(raw_at)
                if timestamp:
                    alert_times.append(timestamp)
        if alert_times:
            return min(alert_times)

    for entry in lead.get("stage_history") or []:
        if isinstance(entry, Mapping) and str(entry.get("to") or "").upper() in HOT_STAGES:
            timestamp = coerce_utc_datetime(entry.get("timestamp") or entry.get("at"))
            if timestamp:
                return timestamp

    timestamp = coerce_utc_datetime(lifecycle.get("visit_scheduled_at"))
    if timestamp:
        return timestamp

    return None


def build_sla_risk_payload(leads, *, now=None, cutover_at=None, cutoff_at=None, exclude_tests=False):
    """Build the dashboard SLA contract from canonical lead evidence only.

    Definición definitiva de CARD 4:

    - ``cutoff_at`` es el límite exclusivo del período (``period_end`` UTC). El
      reloj as-of es ``cutoff = min(now, cutoff_at)``: toda evaluación se
      congela en el cierre del período; un lead abierto al corte se mide hasta
      ``cutoff`` y NO hasta ``datetime.now()`` cuando el período ya cerró.
    - Una primera gestión solo cuenta si ``first_valid_management_at < cutoff``.
    - Un lead se considera HOT en el snapshot solo si ``hot_start_at < cutoff``
      (la temperatura posterior al cierre no reescribe períodos históricos).
    - ``hot_start_at`` se resuelve por la jerarquía de evidencia real
      (``_resolve_hot_start_evidence``); HOT sin evidencia = no evaluable por
      trazabilidad (no se trata como Normal).
    - Una primera gestión anterior a ``hot_start_at`` se evalúa como NORMAL
      (el HOT no reescribe el pasado). Si el HOT precede a la gestión, el SLA
      HOT parte en ``max(assigned_at, hot_start_at)``.
    - ``exclude_tests`` excluye estructuralmente los leads de prueba (misma
      regla de CARD 3) y los contabiliza en ``excluded.excluded_tests``.
    """
    now = coerce_utc_datetime(now) or datetime.now(timezone.utc)
    cutover = coerce_utc_datetime(cutover_at)
    cutoff = coerce_utc_datetime(cutoff_at) or now
    cutoff = min(cutoff, now)
    buckets = {"lead": _sla_bucket(threshold_minutes=180), "lead_hot": _sla_bucket(threshold_minutes=60)}
    managed_durations = {"lead": [], "lead_hot": []}
    excluded = {
        "historical": 0, "not_assigned": 0, "insufficient_data": 0,
        "hot_no_traceability": 0, "excluded_tests": 0,
    }

    for lead in leads:
        lifecycle = lead.get("lifecycle") or {}
        assigned = coerce_utc_datetime(lifecycle.get("assigned_at"))
        managed = coerce_utc_datetime(lifecycle.get("first_valid_management_at"))
        if exclude_tests and _is_test_lead(lead):
            excluded["excluded_tests"] += 1
            continue
        if not assigned:
            excluded["not_assigned"] += 1
            continue
        if cutover and is_pre_visual_cutover(assigned, cutover=cutover):
            excluded["historical"] += 1
            continue
        if managed and managed < assigned:
            excluded["insufficient_data"] += 1
            continue
        if assigned >= cutoff:
            # Al cierre del período el SLA aún no había iniciado (asignación
            # posterior al corte): no evaluable.
            excluded["insufficient_data"] += 1
            continue

        # --- Hot: evidencia y as-of ---
        history = lead.get("temperature_history") or []
        has_hot_evidence = str(lead.get("lead_temperature_effective") or "").upper() == "HOT"
        has_hot_evidence = has_hot_evidence or any(
            str((entry or {}).get("value") or (entry or {}).get("temperature") or "").upper() == "HOT"
            for entry in history if isinstance(entry, Mapping)
        )
        hot_start = _resolve_hot_start_evidence(lead) if has_hot_evidence else None
        if has_hot_evidence and hot_start and hot_start >= cutoff:
            # Se volvió HOT después del cierre del período: en este snapshot NO
            # era HOT; se evalúa por su evidencia disponible hasta el corte.
            has_hot_evidence = False
            hot_start = None
        if has_hot_evidence and hot_start is None:
            excluded["hot_no_traceability"] += 1
            continue

        # --- Perfil y sla_start (sin reescribir el pasado) ---
        if has_hot_evidence:
            if managed and managed < hot_start:
                # La primera gestión se resolvió antes del HOT: perfil NORMAL.
                profile = "lead"
                sla_start = assigned
                threshold = 180
            else:
                profile = "lead_hot"
                sla_start = max(assigned, hot_start)
                threshold = 60
        else:
            profile = "lead"
            sla_start = assigned
            threshold = 180

        # --- As-of: la gestión solo cuenta si ocurrió antes del corte ---
        if managed and managed >= cutoff:
            managed = None  # al cierre del período seguía abierto

        bucket = buckets[profile]
        boundary = managed or cutoff
        if profile == "lead_hot":
            minutes = max(0, calculate_business_minutes(
                sla_start.astimezone(CHILE_TZ), boundary.astimezone(CHILE_TZ)))
        else:
            minutes = max(0, calculate_business_minutes(
                assigned.astimezone(CHILE_TZ), boundary.astimezone(CHILE_TZ)))
        bucket["eligible"] += 1

        if managed:
            if minutes < threshold:
                bucket["managed_within"] += 1
            else:
                bucket["managed_outside"] += 1
            if minutes is not None:
                managed_durations[profile].append(minutes)
        elif minutes >= threshold:
            bucket["breached"] += 1
        elif minutes >= (45 if profile == "lead_hot" else 150):
            bucket["near_breach"] += 1
        elif minutes >= (30 if profile == "lead_hot" else 120):
            bucket["attention"] += 1
        else:
            bucket["open_within"] += 1

    # --- Resumen por perfil ---
    for profile in ("lead", "lead_hot"):
        bucket = buckets[profile]
        bucket.pop("_managed_minutes", None)
        bucket["open_normal"] = bucket["open_within"]
        bucket["open_breached"] = bucket["breached"]
        bucket["in_sla_count"] = (
            bucket["managed_within"] + bucket["open_within"]
            + bucket["attention"] + bucket["near_breach"]
        )
        bucket["out_sla_count"] = bucket["managed_outside"] + bucket["breached"]
        bucket["in_sla_pct"] = _coverage_pct(
            bucket["in_sla_count"], bucket["in_sla_count"] + bucket["out_sla_count"])
        resolved = bucket["managed_within"] + bucket["managed_outside"]
        bucket["resolved_compliance_pct"] = _coverage_pct(
            bucket["managed_within"], resolved) if resolved else None
        bucket["median_minutes"] = _sla_percentile(managed_durations[profile], 50)
        bucket["p50_minutes"] = bucket["median_minutes"]
        bucket["p90_minutes"] = _sla_percentile(managed_durations[profile], 90)
        bucket["managed_sample"] = len(managed_durations[profile])

    # --- Global ---
    overall_in_sla = sum(bucket["in_sla_count"] for bucket in buckets.values())
    overall_out_sla = sum(bucket["out_sla_count"] for bucket in buckets.values())
    overall_eligible = overall_in_sla + overall_out_sla
    overall_in_sla_pct = _coverage_pct(overall_in_sla, overall_eligible) if overall_eligible else None
    overall_managed = sum(bucket["managed_within"] + bucket["managed_outside"] for bucket in buckets.values())
    overall_open = overall_eligible - overall_managed
    overall_breached = sum(bucket["breached"] for bucket in buckets.values())
    overall_resolved = sum(bucket["managed_within"] + bucket["managed_outside"] for bucket in buckets.values())
    resolved_within = sum(bucket["managed_within"] for bucket in buckets.values())
    resolved_compliance_pct = _coverage_pct(resolved_within, overall_resolved) if overall_resolved else None
    overall_median_minutes = _sla_percentile(
        managed_durations["lead"] + managed_durations["lead_hot"], 50
    )
    # Retrocompatibilidad: ratio resuelto/vencido (sin abiertos dentro de SLA).
    overall_numerator = sum(bucket["managed_within"] for bucket in buckets.values())
    overall_denominator = overall_numerator + sum(
        bucket["managed_outside"] + bucket["breached"] for bucket in buckets.values()
    )
    overall_compliance_pct = _coverage_pct(overall_numerator, overall_denominator) if overall_denominator else None
    policy_cutover = cutover.isoformat() if cutover else None
    not_evaluable = excluded["insufficient_data"] + excluded["hot_no_traceability"]
    return {
        "policy_timezone": "America/Santiago",
        "business_hours": {"days": "lunes-viernes", "start": "09:00", "end": "19:00"},
        "policy_cutover_at": policy_cutover,
        # KPI principal CARD 4: estado al corte.
        "overall_in_sla_pct": overall_in_sla_pct,
        "in_sla_count": overall_in_sla,
        "out_sla_count": overall_out_sla,
        "eligible_total": overall_eligible,
        "managed": overall_managed,
        "open": overall_open,
        "open_breached": overall_breached,
        "not_evaluable": not_evaluable,
        "excluded_tests": excluded["excluded_tests"],
        "resolved_compliance_pct": resolved_compliance_pct,
        # Retrocompatibilidad (otros consumidores del payload).
        "overall_compliance_pct": overall_compliance_pct,
        "overall_median_minutes": overall_median_minutes,
        "overall_numerator": overall_numerator,
        "overall_denominator": overall_denominator,
        "within_sla_pct": overall_compliance_pct,
        "lead": buckets["lead"], "lead_hot": buckets["lead_hot"],
        "excluded": excluded,
        "critical_open": buckets["lead"]["breached"] + buckets["lead_hot"]["breached"],
        "no_management": sum(
            bucket["open_within"] + bucket["attention"] + bucket["near_breach"] + bucket["breached"]
            for bucket in buckets.values()),
        "missing_reference": excluded["insufficient_data"],
        "risk_bands": [],
        "distribution": [],
        "sla_policy": {
            "type": "business_minutes", "threshold_minutes": 180,
            "display_label": "SLA vigente: minutos h\u00e1biles",
            "timezone": "America/Santiago",
            "business_hours": "Lunes a viernes, 09:00-19:00",
        },
    }


def _load_executive_cohort(period_start, period_end, filters=None):
    """Load one projected document per lead for the executive summary."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    pipeline = [
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(start_utc, end_utc, filters)},
        {"$project": {
            "_id": 1,
            "ejecutivo_asignado": 1,
            "lead_temperature_effective": 1,
            "temperature_history": 1,
            "lifecycle.assigned_at": 1,
            "lifecycle.first_valid_management_at": 1,
            "lifecycle.first_effective_contact_at": 1,
        }},
    ]
    raw = list(db["leads"].aggregate(pipeline))
    unique = {}
    for lead in raw:
        key = str(lead.get("_id"))
        unique.setdefault(key, lead)
    return list(unique.values()), end_utc


def _summary_percentile(values, percentile):
    return _sla_percentile(sorted(values), percentile) if values else None


def _executive_summary_snapshot(leads, cutoff_utc, sla_risk):
    received = len(leads)
    assigned = 0
    managed = 0
    effective_contact = 0
    hot = 0
    management_times = {"lead": [], "hot": []}
    contact_times = []
    insufficient = 0

    for lead in leads:
        lifecycle = lead.get("lifecycle") or {}
        assigned_at = coerce_utc_datetime(lifecycle.get("assigned_at"))
        management_at = coerce_utc_datetime(lifecycle.get("first_valid_management_at"))
        contact_at = coerce_utc_datetime(lifecycle.get("first_effective_contact_at"))
        history = lead.get("temperature_history") or []
        has_hot = str(lead.get("lead_temperature_effective") or "").upper() == "HOT" or any(
            str((item or {}).get("value") or (item or {}).get("temperature") or "").upper() == "HOT"
            for item in history if isinstance(item, Mapping)
        )
        if has_hot:
            hot += 1
        if not assigned_at or assigned_at >= cutoff_utc:
            continue
        assigned += 1
        valid_management = bool(management_at and assigned_at <= management_at < cutoff_utc)
        if management_at and management_at < assigned_at:
            insufficient += 1
            valid_management = False
        if not valid_management:
            continue
        managed += 1
        hot_start = resolve_hot_start_at(
            assigned_at=assigned_at, temperature_history=history,
            effective_temperature=lead.get("lead_temperature_effective"),
        ) if has_hot else None
        management_start = hot_start if hot_start and hot_start < management_at else assigned_at
        management_minutes = calculate_business_minutes(
            management_start.astimezone(CHILE_TZ), management_at.astimezone(CHILE_TZ)
        )
        management_times["hot" if hot_start and hot_start < management_at else "lead"].append(management_minutes)
        if contact_at and assigned_at <= contact_at < cutoff_utc:
            effective_contact += 1
            contact_times.append(calculate_business_minutes(
                assigned_at.astimezone(CHILE_TZ), contact_at.astimezone(CHILE_TZ)
            ))

    lead_bucket = (sla_risk or {}).get("lead", {})
    hot_bucket = (sla_risk or {}).get("lead_hot", {})
    managed_within = lead_bucket.get("managed_within", 0) + hot_bucket.get("managed_within", 0)
    managed_outside = lead_bucket.get("managed_outside", 0) + hot_bucket.get("managed_outside", 0)
    open_breached = lead_bucket.get("breached", 0) + hot_bucket.get("breached", 0)
    backlog = max(assigned - managed, 0)
    unassigned = max(received - assigned, 0)
    managed_without_contact = max(managed - effective_contact, 0)
    return {
        "received": received, "hot": hot, "assigned": assigned, "unassigned": unassigned,
        "managed": managed, "backlog": backlog, "effective_contact": effective_contact,
        "managed_without_effective_contact": managed_without_contact,
        "assignment_rate_pct": _coverage_pct(assigned, received),
        "management_coverage_pct": _coverage_pct(managed, assigned),
        "contactability_pct": _coverage_pct(effective_contact, managed),
        "managed_within_sla": managed_within, "managed_outside_sla": managed_outside,
        "managed_outside_rate_pct": _coverage_pct(managed_outside, managed_within + managed_outside),
        "open_breached": open_breached,
        "backlog_breached_pct": _coverage_pct(open_breached, backlog),
        "management_time": {
            "lead_median_minutes": _summary_percentile(management_times["lead"], 50),
            "lead_p90_minutes": _summary_percentile(management_times["lead"], 90),
            "lead_measured": len(management_times["lead"]),
            "hot_median_minutes": _summary_percentile(management_times["hot"], 50),
            "hot_p90_minutes": _summary_percentile(management_times["hot"], 90),
            "hot_measured": len(management_times["hot"]),
        },
        "effective_contact_time": {
            "median_minutes": _summary_percentile(contact_times, 50),
            "p90_minutes": _summary_percentile(contact_times, 90),
            "measured": len(contact_times),
            "coverage_pct": _coverage_pct(len(contact_times), managed),
        },
        "risk": {"lead": lead_bucket, "lead_hot": hot_bucket},
        "insufficient_data": insufficient,
    }


def _summary_variations(current, previous):
    if not previous:
        return {}
    count_keys = ("received", "hot", "assigned", "unassigned", "managed", "backlog", "effective_contact", "managed_without_effective_contact", "managed_within_sla", "managed_outside_sla", "open_breached")
    rate_keys = ("assignment_rate_pct", "management_coverage_pct", "contactability_pct", "managed_outside_rate_pct", "backlog_breached_pct")
    result = {}
    for key in count_keys:
        cur, prev = current.get(key), previous.get(key)
        result[key] = {"absolute": cur - prev, "pct": round((cur - prev) / prev * 100, 1) if prev else None}
    for key in rate_keys:
        cur, prev = current.get(key), previous.get(key)
        result[key] = {"pp": round(cur - prev, 1) if cur is not None and prev is not None else None}
    for key in ("lead_median_minutes", "lead_p90_minutes", "hot_median_minutes", "hot_p90_minutes"):
        cur = current["management_time"].get(key)
        prev = previous["management_time"].get(key)
        result[key] = {"minutes": cur - prev if cur is not None and prev is not None else None}
    return result


def query_executive_summary(period_start=None, period_end=None, filters=None,
                            comparison_start=None, comparison_end=None,
                            include_comparison=True, sla_risk=None):
    current_leads, current_cutoff = _load_executive_cohort(period_start, period_end, filters)
    current = _executive_summary_snapshot(current_leads, current_cutoff, sla_risk or {})
    previous = {}
    if include_comparison and comparison_start and comparison_end:
        previous_leads, previous_cutoff = _load_executive_cohort(comparison_start, comparison_end, filters)
        previous_sla = build_sla_risk_payload(
            previous_leads, now=previous_cutoff, cutover_at=getattr(Config, "CRM_SLA_VISUAL_CUTOVER_AT", None)
        )
        previous = _executive_summary_snapshot(previous_leads, previous_cutoff, previous_sla)
    excluded = (sla_risk or {}).get("excluded", {"historical": 0, "not_assigned": 0, "insufficient_data": 0})
    return {
        "current": current,
        "previous": previous,
        "variations": _summary_variations(current, previous),
        "excluded": excluded,
        "unit": "lead._id",
        "contact_effective_results": ["EFFECTIVE_CONTACT", "FOLLOW_UP_REQUESTED", "NOT_INTERESTED"],
    }


def query_sla_risk_panel(period_start=None, period_end=None, filters=None):
    """Canonical SLA panel using business minutes and verified assignment evidence.

    CARD 4 definitivo: aplica corte as-of (``period_end``), resuelve HOT por la
    jerarquía de evidencia real y excluye estructuralmente los leads de prueba
    (misma regla de CARD 3).
    """
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    pipeline = [
        _cohort_indexed_prefilter(start_utc, end_utc),
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(start_utc, end_utc, filters)},
        {"$project": {
            "_id": 1,
            "lifecycle.assigned_at": 1,
            "lifecycle.first_valid_management_at": 1,
            "lifecycle.hot_since": 1,
            "lifecycle.visit_scheduled_at": 1,
            "lead_temperature_effective": 1,
            "temperature_history": 1,
            "last_intent": 1,
            "last_intent_at": 1,
            "stage_history": 1,
            "prospecto.alerts_sent": 1,
            "alerts_sent": 1,
            "_test_lead": 1,
            "is_test": 1,
            "phone": 1,
            "prospecto.canal_origen": 1,
            "prospecto.origen": 1,
            "prospecto.nombre": 1,
        }},
    ]
    raw = list(db["leads"].aggregate(pipeline))
    return build_sla_risk_payload(
        raw,
        now=datetime.now(timezone.utc),
        cutover_at=getattr(Config, "CRM_SLA_VISUAL_CUTOVER_AT", None),
        cutoff_at=end_utc,
        exclude_tests=True,
    )


def _sla_accountability_bucket():
    return {
        "eligible": 0, "managed_within": 0, "managed_outside": 0,
        "open_breached": 0, "breached_with_activity_without_result": 0,
        "breached_without_activity": 0, "within_rate": None,
        "median_business_minutes": None, "p90_business_minutes": None,
        "_durations": [],
    }


def query_sla_accountability(period_start=None, period_end=None, filters=None):
    """Consolidated SLA accountability by executive using canonical CRM evidence."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    raw = list(db["leads"].aggregate([
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(start_utc, end_utc, filters)},
        {"$project": {
            "_id": 1, "ejecutivo_asignado": 1, "lead_temperature_effective": 1,
            "temperature_history": 1, "lifecycle.assigned_at": 1,
            "lifecycle.first_valid_management_at": 1,
        }},
    ]))
    unique = {}
    for lead in raw:
        unique.setdefault(str(lead.get("_id")), lead)
    leads = list(unique.values())
    ids = [lead.get("_id") for lead in leads if lead.get("_id") is not None]
    events_by_lead = {}
    if ids:
        # Solo se necesitan estos campos para event_evidence(); evitar traer
        # payloads completos de eventos reduce mucho la respuesta de Mongo.
        event_projection = {
            "lead_id": 1, "type": 1, "actor": 1, "actor_type": 1,
            "result": 1, "confirmed": 1, "meta": 1,
            "timestamp": 1, "occurred_at": 1,
        }
        # event_evidence() solo puede marcar actividad con confirmación y un
        # resultado de contacto. Evitar leer aperturas, asignaciones y eventos
        # del sistema reduce drásticamente esta consulta.
        contact_results = [
            "NO_RESPONDIO", "OCUPADO", "NUMERO_INVALIDO", "MENSAJE_ENVIADO",
            "CONTACTADO", "SOLICITA_SEGUIMIENTO", "NO_INTERESADO", "OTRO",
            "MESSAGE_SENT_WAITING_RESPONSE", "CALL_NO_ANSWER", "EMAIL_SENT",
            "EFFECTIVE_CONTACT", "FOLLOW_UP_REQUESTED", "INVALID_NUMBER",
        ]
        event_filter = {
            "lead_id": {"$in": ids},
            "confirmed": True,
            "$or": [
                {"result": {"$in": contact_results}},
                {"meta.result": {"$in": contact_results}},
                {"meta.contact_result": {"$in": contact_results}},
            ],
        }
        try:
            for event in db["crm_events"].find(
                event_filter, event_projection
            ):
                events_by_lead.setdefault(str(event.get("lead_id")), []).append(event)
        except NetworkTimeout:
            # La actividad de contacto es complementaria para esta tarjeta.
            # Si Mongo no termina la lectura dentro del timeout, no debemos
            # tumbar todo el dashboard: se conserva SLA y se marca como sin
            # actividad verificable en esta respuesta.
            logger.warning(
                "[SLA_ACCOUNTABILITY] crm_events timeout; continuing without activity evidence"
            )

    rows = {}
    summary = {"open_breached": 0, "breached_with_activity_without_result": 0,
               "breached_without_activity": 0, "registration_gap_rate": None}
    now = datetime.now(timezone.utc)
    cutover = coerce_utc_datetime(getattr(Config, "CRM_SLA_VISUAL_CUTOVER_AT", None))

    def row_for(name):
        return rows.setdefault(name, {"executive_id": name, "executive_name": name,
            "assigned": 0, "lead": _sla_accountability_bucket(),
            "lead_hot": _sla_accountability_bucket()})

    for lead in leads:
        lifecycle = lead.get("lifecycle") or {}
        assigned = coerce_utc_datetime(lifecycle.get("assigned_at"))
        name = lead.get("ejecutivo_asignado") or "Sin asignar"
        row = row_for(str(name))
        row["assigned"] += 1
        if not assigned or (cutover and is_pre_visual_cutover(assigned, cutover=cutover)):
            continue
        managed = coerce_utc_datetime(lifecycle.get("first_valid_management_at"))
        if managed and managed < assigned:
            continue
        history = lead.get("temperature_history") or []
        has_hot = str(lead.get("lead_temperature_effective") or "").upper() == "HOT" or any(
            str((item or {}).get("value") or (item or {}).get("temperature") or "").upper() == "HOT"
            for item in history if isinstance(item, Mapping)
        )
        hot_start = resolve_hot_start_at(assigned_at=assigned, temperature_history=history,
            effective_temperature=lead.get("lead_temperature_effective")) if has_hot else None
        profile = "lead_hot" if hot_start and (not managed or hot_start < managed) else "lead"
        if has_hot and not hot_start:
            continue
        sla = calculate_sla(assigned_at=assigned, first_valid_management_at=managed, now=now,
            temperature="HOT" if profile == "lead_hot" else "COLD", hot_started_at=hot_start,
            require_hot_start=profile == "lead_hot")
        if profile == "lead_hot" and hot_start and hot_start > assigned:
            pre_hot = calculate_sla(assigned_at=assigned, first_valid_management_at=hot_start,
                now=now, temperature="COLD")
            if pre_hot.get("canonical_state") in {"MANAGED_OUTSIDE_SLA", "BREACHED"}:
                sla["canonical_state"] = "MANAGED_OUTSIDE_SLA" if pre_hot["canonical_state"] == "MANAGED_OUTSIDE_SLA" else "BREACHED"
        if sla.get("canonical_state") in {"SLA_NOT_STARTED", "INSUFFICIENT_DATA"}:
            continue
        bucket = row[profile]
        bucket["eligible"] += 1
        minutes = sla.get("hot_minutes") if profile == "lead_hot" else sla.get("minutes")
        if managed:
            if sla.get("canonical_state") == "MANAGED_WITHIN_SLA":
                bucket["managed_within"] += 1
            else:
                bucket["managed_outside"] += 1
            if minutes is not None:
                bucket["_durations"].append(minutes)
            continue
        if sla.get("canonical_state") != "BREACHED":
            continue
        bucket["open_breached"] += 1
        summary["open_breached"] += 1
        has_activity = any(
            event_evidence(event).get("contact_attempt")
            and (event_time := coerce_utc_datetime(event.get("timestamp") or event.get("occurred_at")))
            and event_time >= assigned and event_time <= now
            for event in events_by_lead.get(str(lead.get("_id")), [])
        )
        key = "breached_with_activity_without_result" if has_activity else "breached_without_activity"
        bucket[key] += 1
        summary[key] += 1

    for row in rows.values():
        for key in ("lead", "lead_hot"):
            bucket = row[key]
            managed_total = bucket["managed_within"] + bucket["managed_outside"]
            bucket["within_rate"] = _coverage_pct(bucket["managed_within"], managed_total)
            durations = sorted(bucket.pop("_durations"))
            bucket["median_business_minutes"] = _sla_percentile(durations, 50)
            bucket["p90_business_minutes"] = _sla_percentile(durations, 90)
    if summary["open_breached"]:
        summary["registration_gap_rate"] = _coverage_pct(
            summary["breached_with_activity_without_result"], summary["open_breached"]
        )
    return {"summary": summary, "by_executive": sorted(rows.values(), key=lambda row: (
        -row["lead"]["open_breached"] - row["lead_hot"]["open_breached"],
        -row["lead"]["breached_with_activity_without_result"] - row["lead_hot"]["breached_with_activity_without_result"],
        str(row["executive_name"]).lower(),
    )), "reconciliation": {"open_breached_delta": summary["open_breached"] - (
        summary["breached_with_activity_without_result"] + summary["breached_without_activity"]
    )}}


def default_sla_response():
    return build_sla_risk_payload([])


def build_conversion_table(minutes_dist):
    buckets = [
        ("Menos de 30 min", 0, 30),
        ("30-60 min", 30, 60),
        ("1-3 horas", 60, 180),
        ("M\u00e1s de 3 horas", 180, None),
        ("Sin gesti\u00f3n", None, None),
    ]
    table = []
    for label, lo, hi in buckets:
        if lo is None:
            items = [d for d in minutes_dist if not d["managed"]]
        elif hi is None:
            items = [d for d in minutes_dist if d["minutes"] is not None and d["minutes"] >= lo]
        else:
            items = [d for d in minutes_dist if d["minutes"] is not None and lo <= d["minutes"] < hi]
        n = len(items)
        table.append({
            "bucket": label, "hot": n,
            "visits": sum(1 for d in items if d["has_visit"]),
            "conversion_pct": _coverage_pct(sum(1 for d in items if d["has_visit"]), n) if n else None,
        })
    return table


# =============================================================================
# 5. DEMANDA POR PRECIO (CORREGIDO)
# =============================================================================

def _type_distribution(raw_leads: list) -> list:
    """Extrae distribuci\u00f3n real de tipos de propiedad."""
    from collections import Counter
    counts = Counter()
    for lead in raw_leads:
        t = str(lead.get("_tipo") or lead.get("prospecto", {}).get("tipo") or "Sin informacion").strip()
        if not t or t.lower() in ("sin informacion", "sin informaci\u00f3n", "none", "", "n/a"):
            t = "S/I"
        counts[t] += 1
    return [{"value": k, "count": v} for k, v in counts.most_common(10)]


def _commune_distribution(raw_leads: list) -> list:
    """Distribución declarada de comunas, sin inferir ni completar faltantes."""
    from collections import Counter
    counts = Counter()
    for lead in raw_leads:
        value = str((lead.get("prospecto") or {}).get("comuna") or "S/I").strip()
        if not value or value.lower() in ("sin informacion", "sin información", "none", "n/a"):
            value = "S/I"
        counts[value] += 1
    return [{"value": key, "count": count} for key, count in counts.most_common(10)]


def query_demand_by_price_ranges(period_start=None, period_end=None, filters=None):
    """Demand by price ranges, separated by operation (Venta=UF, Arriendo=CLP). Reports coverage."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    extra = _build_extra_filter(filters) or {}

    pipeline = [
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(start_utc, end_utc, filters)},
        {"$addFields": {
            "_operacion": {"$ifNull": ["$prospecto.operacion", "Sin informacion"]},
            "_precio_uf": {"$ifNull": ["$prospecto.precio_uf", "$cartera_data.precio_uf", None]},
            "_precio_clp": {"$ifNull": ["$prospecto.precio_clp", "$cartera_data.precio_clp", None]},
            "_tipo": {"$ifNull": ["$prospecto.tipo", "Sin informacion"]},
        }},
    ]
    raw = list(db["leads"].aggregate(pipeline))
    total = len(raw)
    venta = [l for l in raw if str(l.get("_operacion") or "").lower() == "venta"]
    arriendo = [l for l in raw if str(l.get("_operacion") or "").lower() == "arriendo"]
    otros = [l for l in raw if l not in venta and l not in arriendo]

    def _bucket(leads, price_field, range_defs, currency):
        bucketed = {k: 0 for k, _ in [("_", None)]}
        bucketed = {}
        for k, _, _ in range_defs:
            bucketed[k] = 0
        bucketed["Sin precio"] = 0
        has_price = 0
        for lead in leads:
            p = lead.get(price_field)
            if p is not None:
                try:
                    p = float(p)
                    has_price += 1
                    matched = False
                    for k, lo, hi in range_defs:
                        if hi is None:
                            if p >= lo:
                                bucketed[k] += 1
                                matched = True
                                break
                        elif lo <= p < hi:
                            bucketed[k] += 1
                            matched = True
                            break
                    if not matched:
                        bucketed["Sin precio"] += 1
                except (ValueError, TypeError):
                    bucketed["Sin precio"] += 1
            else:
                bucketed["Sin precio"] += 1
        t = len(leads)
        ranges = []
        for k, _, _ in range_defs:
            ranges.append({"range": k, "count": bucketed[k], "pct_of_op": _coverage_pct(bucketed[k], t)})
        if bucketed.get("Sin precio", 0) > 0:
            ranges.append({"range": "Sin precio", "count": bucketed["Sin precio"], "pct_of_op": _coverage_pct(bucketed["Sin precio"], t)})
        return {"total": t, "ranges": ranges, "coverage": {"with_price": has_price, "without_price": t - has_price, "coverage_pct": _coverage_pct(has_price, t), "currency": currency}}

    ops = {}
    if venta:
        ops["Venta"] = _bucket(venta, "_precio_uf", PRICE_RANGES_UF, "UF")
    if arriendo:
        ops["Arriendo"] = _bucket(arriendo, "_precio_clp", PRICE_RANGES_CLP, "CLP")
    if otros:
        ops["Otros"] = _bucket(otros, None, [], "N/A")

    # Coverage summary
    has_op = sum(1 for l in raw if str(l.get("_operacion") or "").lower() in ("venta", "arriendo"))
    has_tipo = sum(1 for l in raw if str(l.get("prospecto", {}).get("tipo") or "") not in ("", "Sin informacion"))
    has_comuna = sum(1 for l in raw if str(l.get("prospecto", {}).get("comuna") or "") not in ("", "Sin informacion"))
    has_precio = sum(1 for l in raw if l.get("_precio_uf") is not None or l.get("_precio_clp") is not None)

    return {
        "price_ranges": [{"operation": k, **v} for k, v in ops.items()],
        "_types": _type_distribution(raw),
        "_communes": _commune_distribution(raw),
        "coverage": {
            "operacion": _coverage_pct(has_op, total),
            "tipo_propiedad": _coverage_pct(has_tipo, total),
            "comuna": _coverage_pct(has_comuna, total),
            "precio": _coverage_pct(has_precio, total),
            "dormitorios": None, "banos": None, "estacionamiento": None, "superficie": None,
            "_note": "Cobertura baja en ciertas dimensiones. Datos no disponibles para dormitorios/ba\u00f1os/superficie.",
        },
        "_note": "Venta en UF. Arriendo en CLP. No se convierte entre monedas.",
    }


# =============================================================================
# 6. MATRIZ EJECUTIVAS (CORREGIDO)
# =============================================================================

def query_commercial_executive_matrix(period_start=None, period_end=None, filters=None):
    """Executive performance matrix. Universo: cohorte asignada. Stage reached (hist\u00f3rico)."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)

    pipeline = [
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(start_utc, end_utc, filters)},
        {"$addFields": {
            "_ex": {"$ifNull": ["$ejecutivo_asignado", "Sin Asignar"]},
            "_is_hot": {"$cond": [{"$or": [
                {"$eq": ["$lead_temperature_effective", "HOT"]},
                {"$gt": [{"$size": {
                    "$ifNull": [{"$filter": {
                        "input": {"$ifNull": ["$temperature_history", []]},
                        "cond": {"$in": [{"$toUpper": {"$ifNull": ["$$this.value", "$$this.temperature", ""]}}, ["HOT", "COLD"]]},
                    }}, []]
                }}, 0]},
            ]}, 1, 0]},
            "_has_mgmt": {"$cond": [{"$ne": [{"$ifNull": ["$lifecycle.first_valid_management_at", None]}, None]}, 1, 0]},
            "_ever_vs": {"$cond": [{"$or": [
                {"$in": ["$pipeline_stage", ["VISIT_SCHEDULED", "VISIT_DONE", "OFFER", "NEGOTIATION", "CLOSED_WON", "CLOSED_LOST"]]},
            ]}, 1, 0]},
            "_ever_vd": {"$cond": [{"$in": ["$pipeline_stage", ["VISIT_DONE", "OFFER", "NEGOTIATION", "CLOSED_WON", "CLOSED_LOST"]]}, 1, 0]},
            "_ever_neg": {"$cond": [{"$in": ["$pipeline_stage", ["OFFER", "NEGOTIATION", "CLOSED_WON", "CLOSED_LOST"]]}, 1, 0]},
            "_ever_cw": {"$cond": [{"$eq": ["$pipeline_stage", "CLOSED_WON"]}, 1, 0]},
            "_ever_cl": {"$cond": [{"$eq": ["$pipeline_stage", "CLOSED_LOST"]}, 1, 0]},
        }},
        {"$group": {
            "_id": "$_ex",
            "assigned": {"$sum": 1},
            "hot": {"$sum": "$_is_hot"},
            "managed": {"$sum": "$_has_mgmt"},
            "ever_visit_scheduled": {"$sum": "$_ever_vs"},
            "ever_visit_done": {"$sum": "$_ever_vd"},
            "ever_negotiation": {"$sum": "$_ever_neg"},
            "ever_closed_won": {"$sum": "$_ever_cw"},
            "ever_closed_lost": {"$sum": "$_ever_cl"},
        }},
        {"$sort": {"assigned": -1}},
    ]

    rows = list(db["leads"].aggregate(pipeline))
    result = []
    for r in rows:
        if r["_id"] in UNASSIGNED_VALUES:
            continue
        t = r["assigned"]
        result.append({
            "executive": r["_id"], "assigned": t,
            "hot": r["hot"],
            "sla_fulfilled": _coverage_pct(r["managed"], t),
            "ever_visit_scheduled": r["ever_visit_scheduled"],
            "ever_visit_done": r["ever_visit_done"],
            "ever_negotiation": r["ever_negotiation"],
            "ever_closed_won": r["ever_closed_won"],
            "ever_closed_lost": r["ever_closed_lost"],
            "conversion_to_visit_pct": _coverage_pct(r["ever_visit_scheduled"], t),
            "conversion_to_close_pct": _coverage_pct(r["ever_closed_won"], t),
            "universe": "cohorte_asignados",
        })
    return result


# =============================================================================
# 7. PROPIEDADES: OPORTUNIDAD Y FUGA
# =============================================================================

def query_commercial_property_ranking(period_start=None, period_end=None, filters=None):
    """Opportunity and leakage rankings. Universo: cohorte con codigo."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)

    pipeline = [
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(start_utc, end_utc, filters)},
        {"$addFields": {
            "_code": {"$ifNull": ["$prospecto.codigo", None]},
            "_is_hot": {"$cond": [{"$eq": ["$lead_temperature_effective", "HOT"]}, 1, 0]},
            "_ever_vi": {"$cond": [{"$or": [
                {"$in": [{"$ifNull": ["$bi_analytics_global.RESULTADO_CHAT", ""]}, list(VISIT_RESULTS)]},
                {"$in": [{"$ifNull": ["$last_intent", ""]}, list(VISIT_RESULTS)]},
                {"$in": ["$pipeline_stage", ["VISIT_SCHEDULED", "VISIT_DONE"]]},
            ]}, 1, 0]},
            "_ever_vs": {"$cond": [{"$in": ["$pipeline_stage", ["VISIT_SCHEDULED", "VISIT_DONE", "OFFER", "NEGOTIATION", "CLOSED_WON"]]}, 1, 0]},
            "_ever_cw": {"$cond": [{"$eq": ["$pipeline_stage", "CLOSED_WON"]}, 1, 0]},
            "_ex": {"$ifNull": ["$ejecutivo_asignado", "Sin Asignar"]},
            "_src": {"$ifNull": ["$prospecto.origen", "Sin informacion"]},
            "_tipo": {"$ifNull": ["$prospecto.tipo", "Sin informacion"]},
            "_comuna": {"$ifNull": ["$prospecto.comuna", "Sin informacion"]},
            "_op": {"$ifNull": ["$prospecto.operacion", "Sin informacion"]},
            "_precio_uf": {"$ifNull": ["$prospecto.precio_uf", "$cartera_data.precio_uf", None]},
            "_has_mgmt": {"$cond": [{"$ne": [{"$ifNull": ["$lifecycle.first_valid_management_at", None]}, None]}, 1, 0]},
        }},
        {"$match": {"_code": {"$ne": None, "$ne": ""}}},
        {"$group": {
            "_id": "$_code",
            "count": {"$sum": 1},
            "hot": {"$sum": "$_is_hot"},
            "ever_vi": {"$sum": "$_ever_vi"},
            "ever_vs": {"$sum": "$_ever_vs"},
            "ever_cw": {"$sum": "$_ever_cw"},
            "executives": {"$addToSet": "$_ex"},
            "sources": {"$addToSet": "$_src"},
            "tipos": {"$addToSet": "$_tipo"},
            "comunas": {"$addToSet": "$_comuna"},
            "ops": {"$addToSet": "$_op"},
            "precios": {"$push": "$_precio_uf"},
            "mgmt_count": {"$sum": "$_has_mgmt"},
        }},
    ]

    raw = list(db["leads"].aggregate(pipeline))
    opp = []
    leak = []
    for r in raw:
        code = r["_id"]
        t = r["count"]
        prices = []
        for p in r.get("precios", []):
            if p is not None:
                try:
                    prices.append(float(p))
                except (ValueError, TypeError):
                    pass
        avg_p = round(sum(prices) / len(prices), 1) if prices else None
        dom_ex = max(r.get("executives", [""]), key=lambda x: str(x))
        dom_src = max(r.get("sources", [""]), key=lambda x: str(x))
        vi = r.get("ever_vi", 0)
        vs = r.get("ever_vs", 0)
        entry = {
            "code": code,
            "type": max(r.get("tipos", [""]), key=lambda x: str(x)),
            "commune": max(r.get("comunas", [""]), key=lambda x: str(x)),
            "operation": max(r.get("ops", [""]), key=lambda x: str(x)),
            "avg_price_uf": avg_p,
            "leads": t, "hot": r["hot"],
            "ever_visit_intent": vi,
            "ever_visit_scheduled": vs,
            "ever_closed_won": r["ever_cw"],
            "conversion_pct": _coverage_pct(r["ever_cw"], t),
            "dominant_executive": dom_ex,
            "dominant_source": dom_src,
        }
        opp.append(entry)
        uncoord = vi - vs
        unmgmt = t - r["mgmt_count"]
        if uncoord > 0 or unmgmt > 0:
            leak.append({
                "code": code, "ever_visit_intent": vi,
                "uncoordinated": uncoord, "unmanaged": unmgmt,
                "dominant_executive": dom_ex, "dominant_source": dom_src,
                "commune": entry["commune"],
            })
    opp.sort(key=lambda x: x["leads"], reverse=True)
    leak.sort(key=lambda x: x["uncoordinated"] + x["unmanaged"], reverse=True)
    return {"opportunity": opp[:10], "valuation": opp, "total_properties": len(opp), "leakage": leak[:10]}


# =============================================================================
# 8. INSIGHTS DETERMINISTICOS
# =============================================================================

def query_commercial_insights(kpis=None, funnel=None, sla=None, sources=None, demand=None, executives=None, filters=None):
    """Deterministic insight engine. No AI, no invented data."""
    ins = []
    if sla and sla.get("critical_open", 0) > 0:
        ins.append({
            "priority": "critical",
            "title": f"{sla['critical_open']} leads Hot cr\u00edticos sin gesti\u00f3n",
            "finding": f"Existen {sla['critical_open']} leads Hot con >3h sin gesti\u00f3n.",
            "evidence": f"Cr\u00edticos: {sla['critical_open']} | Sin gesti\u00f3n: {sla.get('no_management', 0)}",
            "impact": "Riesgo de p\u00e9rdida de oportunidades.",
            "recommended_action": "Asignar y contactar urgentemente.",
        })
    if funnel and len(funnel) > 1:
        mx, bn = 0, None
        for i in range(1, len(funnel)):
            diff = funnel[i - 1]["count"] - funnel[i]["count"]
            if diff > mx:
                mx, bn = diff, (funnel[i - 1]["label"], funnel[i]["label"])
        if bn and mx > 0:
            ins.append({
                "priority": "high",
                "title": f"Fuga en {bn[0]} \u2192 {bn[1]}",
                "finding": f"{mx} leads se pierden entre {bn[0]} y {bn[1]}.",
                "evidence": f"P\u00e9rdida: {mx} leads",
                "impact": "Oportunidades que no avanzan.",
                "recommended_action": "Analizar causas de abandono.",
            })
    vi = next((s for s in (funnel or []) if s["key"] == "visit_intent"), None)
    vs = next((s for s in (funnel or []) if s["key"] == "visit_scheduled"), None)
    if vi and vs and vi["count"] - vs["count"] > 0:
        g = vi["count"] - vs["count"]
        ins.append({
            "priority": "high",
            "title": f"Brecha de {g} leads entre intenci\u00f3n y coordinaci\u00f3n",
            "finding": f"{g} leads con intenci\u00f3n de visita sin coordinar.",
            "evidence": f"Intenciones: {vi['count']} | Coordinadas: {vs['count']}",
            "impact": "Demanda insatisfecha.",
            "recommended_action": "Contactar leads con intenci\u00f3n no coordinada.",
        })
    if sources:
        for src in sources[:5]:
            n = src.get("source", "")
            if n in NON_COMMERCIAL_SOURCES:
                continue
            v = src.get("variation_pct")
            a = src.get("advanced_pct", 0)
            r = src.get("received", 0)
            if v is not None and v > 20 and a < 20 and r >= 15:
                ins.append({
                    "priority": "medium",
                    "title": f"Fuente {n}: +{v:.1f}% volumen, baja conversi\u00f3n",
                    "finding": f"{n} creci\u00f3 {v:.1f}% pero solo {a:.1f}% avanza.",
                    "evidence": f"Vol: {r} | Avanzados: {a:.1f}%",
                    "impact": "Posible deterioro de calidad.",
                    "recommended_action": "Revisar perfil y gesti\u00f3n por fuente.",
                })
                break
    if executives:
        for ex in executives[:3]:
            mc = sum(ex.get(k, 0) for k in ["ever_visit_scheduled", "ever_visit_done", "ever_closed_won", "ever_closed_lost"])
            at = ex.get("assigned", 0)
            um = at - mc
            if um > 5 and at > 0 and mc / at < 0.5:
                ins.append({
                    "priority": "high",
                    "title": f"{ex['executive']}: {um} leads sin gesti\u00f3n",
                    "finding": f"{um} leads sin gesti\u00f3n ({_coverage_pct(mc, at)}% gestionado).",
                    "evidence": f"Asignados: {at} | Gestionados: {mc}",
                    "impact": "Riesgo de saturaci\u00f3n.",
                    "recommended_action": "Revisar carga de trabajo.",
                })
                break
    return ins[:5]


# =============================================================================
# 9. PERIOD COMPARISON HELPERS
# =============================================================================

def period_today_vs_last_week():
    """Today (same hour) vs same day last week (same hour)."""
    now = datetime.now(CHILE_TZ)
    today_start = CHILE_TZ.localize(datetime(now.year, now.month, now.day, 0, 0, 0))
    lw = now - timedelta(days=7)
    lw_start = CHILE_TZ.localize(datetime(lw.year, lw.month, lw.day, 0, 0, 0))
    lw_cut = lw_start + (now - today_start)
    return (
        today_start.astimezone(timezone.utc), now.astimezone(timezone.utc),
        lw_start.astimezone(timezone.utc), lw_cut.astimezone(timezone.utc),
    )


def period_week_to_date():
    """Week to date vs same weekdays last week."""
    now = datetime.now(CHILE_TZ)
    monday = now - timedelta(days=now.weekday())
    wtd_start = CHILE_TZ.localize(datetime(monday.year, monday.month, monday.day, 0, 0, 0))
    prev_monday = monday - timedelta(days=7)
    prev_start = CHILE_TZ.localize(datetime(prev_monday.year, prev_monday.month, prev_monday.day, 0, 0, 0))
    prev_end = prev_start + (now - wtd_start)
    return (
        wtd_start.astimezone(timezone.utc), now.astimezone(timezone.utc),
        prev_start.astimezone(timezone.utc), prev_end.astimezone(timezone.utc),
    )


def period_month_to_date():
    """Month to date vs same days last month."""
    now = datetime.now(CHILE_TZ)
    mtd_start = CHILE_TZ.localize(datetime(now.year, now.month, 1, 0, 0, 0))
    prev_m = now.month - 1 if now.month > 1 else 12
    prev_y = now.year if now.month > 1 else now.year - 1
    prev_start = CHILE_TZ.localize(datetime(prev_y, prev_m, 1, 0, 0, 0))
    prev_end = prev_start + (now - mtd_start)
    return (
        mtd_start.astimezone(timezone.utc), now.astimezone(timezone.utc),
        prev_start.astimezone(timezone.utc), prev_end.astimezone(timezone.utc),
    )


def _control_property_candidate_query(lead_docs: list[dict]) -> dict:
    """Build a bounded Prop360 lookup for canonical and verified portal IDs."""
    empty = PropertyIdentityResolver([])
    canonical: set[str] = set()
    aliases: dict[str, set[str]] = defaultdict(set)
    for lead in lead_docs:
        prospecto = lead.get("prospecto") or {}
        code = normalize_identifier(prospecto.get("codigo"))
        if code:
            canonical.add(code)
        for alias, _path in empty.lead_aliases(lead):
            aliases[alias.source].add(alias.external_id)
    clauses: list[dict] = []
    if canonical:
        clauses.append({"codigo": {"$in": sorted(canonical)}})
    portal_fields = {
        "mercadolibre": "portal_inmobiliario",
        "yapo": "yapo",
        "toctoc": "toctoc",
        "chilepropiedades": "chilepropiedades",
        "proppit": "proppit",
        "procasa": "procasa",
    }
    for source, portal in portal_fields.items():
        values = sorted(aliases.get(source) or ())
        if not values:
            continue
        for operation in ("V", "A"):
            for field in ("code", "code_unique"):
                clauses.append({f"publicaciones.{portal}.publicaciones.{operation}.{field}": {"$in": values}})
        if source == "chilepropiedades":
            for field in ("codigo_venta", "codigo_arriendo"):
                clauses.append({f"publicaciones.chilepropiedades.{field}": {"$in": values}})
    return {"$or": clauses} if clauses else {"codigo": {"$in": []}}


def _control_resolve_episode_property(cycle: Mapping[str, Any], lead: Mapping[str, Any], resolver: PropertyIdentityResolver) -> tuple[str | None, str]:
    """Prefer the episode's property, flagging conflicts with lead identity."""
    cycle_code = normalize_identifier(cycle.get("property_code"))
    lead_resolution = resolver.resolve(lead)
    if cycle_code:
        cycle_resolution = resolver.resolve({"prospecto": {"codigo": cycle_code}})
        cycle_canonical = cycle_resolution.resolved_property_code
        if lead_resolution.status in (LinkageStatus.AMBIGUOUS, LinkageStatus.CONFLICT):
            return None, "ambiguous"
        if cycle_canonical and lead_resolution.resolved_property_code and cycle_canonical != lead_resolution.resolved_property_code:
            return None, "ambiguous"
        if cycle_canonical:
            return cycle_canonical, "confirmed"
        if lead_resolution.resolved_property_code:
            return lead_resolution.resolved_property_code, "confirmed"
        return None, "unresolved"
    if lead_resolution.resolved_property_code:
        return lead_resolution.resolved_property_code, "confirmed"
    return None, "ambiguous" if lead_resolution.status in (LinkageStatus.AMBIGUOUS, LinkageStatus.CONFLICT) else "unresolved"


def _control_person_key(value: str | None) -> str:
    """Normalize Prop360 executive display-name variants to one identity."""
    import unicodedata
    normalized = "".join(
        char for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    )
    normalized = " ".join(normalized.strip().casefold().split())
    # The Control selector uses shortened CRM display names; Prop360 can
    # include additional surnames. Treat only verified variants as aliases.
    if normalized in {"Executive A", "Executive A"}:
        return "Executive A"
    if normalized in {"Executive B", "Executive B"}:
        return "Executive B"
    return normalized


def _control_person_aliases(value: str | None) -> list[str]:
    """Return known Prop360 spellings used when selecting current inventory."""
    selected = str(value or "").strip()
    if _control_person_key(selected) == "Executive A":
        return list(dict.fromkeys([selected, "Executive A", "Executive A"]))
    if _control_person_key(selected) == "Executive B":
        return list(dict.fromkeys([selected, "Executive B", "Executive B"]))
    return [selected] if selected else []


def _control_current_relation(executive: str | None, responsible: str | None, captor: str | None) -> str:
    if not executive:
        return "Selecciona un ejecutivo"
    owner = str(responsible or captor or "").strip()
    if owner:
        return "Cartera del ejecutivo seleccionado" if _control_person_key(owner) == _control_person_key(executive) else "Otro responsable"
    return "Sin clasificación verificable"


def _control_is_currently_active_property(doc: Mapping[str, Any]) -> bool:
    """Only an explicit Prop360 true flag qualifies for the current active portfolio."""
    return doc.get("disponible_prop360") is True


def _control_count_unique_leads_by_property(lead_property_pairs: Iterable[tuple[str, str]]) -> Counter:
    """Count distinct CRM leads per canonical property, never global clients."""
    unique_pairs = {(str(lead_id), str(code)) for lead_id, code in lead_property_pairs if lead_id and code}
    counts: Counter = Counter()
    for _lead_id, code in unique_pairs:
        counts[code] += 1
    return counts


def _control_property_participation(leads_count: int, denominator_pairs: int) -> float | None:
    if denominator_pairs <= 0:
        return None
    return round(int(leads_count or 0) / denominator_pairs * 100, 1)


def _control_property_leads_query(property_docs: Iterable[Mapping[str, Any]]) -> dict:
    """One indexed multi-property lookup across canonical codes and verified aliases."""
    canonical: set[str] = set()
    aliases: dict[str, set[str]] = defaultdict(set)
    for doc in property_docs or []:
        code = normalize_identifier(doc.get("codigo"))
        if code:
            canonical.add(code)
        for alias, _path in _iter_master_aliases(doc):
            aliases[alias.source].add(alias.external_id)
    clauses: list[dict] = []
    if canonical:
        clauses.append({"prospecto.codigo": {"$in": sorted(canonical)}})
    paths_by_source = {
        "mercadolibre": ["prospecto.codigo_mercadolibre"],
        "yapo": ["prospecto.codigo_yapo"],
        "toctoc": ["prospecto.codigo_propiedad", "prospecto.propiedad_codigo"],
    }
    for source, paths in paths_by_source.items():
        values = sorted(aliases.get(source) or ())
        if values:
            clauses.extend({path: {"$in": values}} for path in paths)
    # Alias paths beyond canonical/ML/Yapo/Toctoc are queried using the same
    # verified namespaces used by the shared identity resolver.
    return {"$or": clauses} if clauses else {"_id": {"$exists": False}}


def _control_index_property_demand(
    leads, resolver, property_codes, start_utc, end_utc, as_of_utc=None, ingest_events=(),
):
    """Index property demand only when both the event date and property are evidenced.

    Prop360 ``created_at`` is the CRM ingestion time, not the customer's contact
    time.  Its contact date is accepted only when the source event is tied back
    to the same lead by the idempotency ledger.  Native inbound/manual leads use
    their first timestamped user message when the property is identified by a
    verified portal alias (or an explicit manual intake marker). Legacy links
    without either kind of evidence stay unknown.
    """
    counts: Counter = Counter()
    historical_counts: Counter = Counter()
    latest: dict[str, datetime] = {}
    latest_registered: dict[str, datetime] = {}
    latest_in_period: dict[str, datetime] = {}
    unverified: Counter = Counter()
    ambiguous_by_code: Counter = Counter()
    ambiguous = missing_created_at = 0
    valid_prop360 = valid_inbound = valid_manual = 0
    reason_counts: Counter = Counter()
    ledger_by_event: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in ingest_events or ():
        if not isinstance(event, Mapping):
            continue
        ledger_by_event[str(event.get("source_event_id") or "").strip()].append(event)

    def lead_id_text(value: Any) -> str:
        if isinstance(value, Mapping):
            return str(value.get("$oid") or value.get("oid") or "").strip()
        return str(value or "").strip()

    def first_user_message(lead: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return next((message for message in lead.get("messages") or ()
                     if isinstance(message, Mapping)
                     and str(message.get("role") or "").strip().casefold() == "user"
                     and str(message.get("content") or "").strip()), None)

    def intake_evidence(lead: Mapping[str, Any], code: str, created_at: datetime | None) -> tuple[datetime | None, str | None]:
        source_type = str(lead.get("source_type") or "").strip().casefold()
        source_events = [event for event in lead.get("source_events") or ()
                         if isinstance(event, Mapping)
                         and str(event.get("source_system") or "").strip().casefold() == "prop360"]
        # A Prop360 import timestamp never substitutes for a missing contact date.
        if source_type == "prop360" or source_events:
            valid_dates: list[datetime] = []
            for event in source_events:
                event_id = str(event.get("source_event_id") or "").strip()
                event_at = coerce_utc_datetime(event.get("contact_date"))
                if not event_id or not event_at:
                    continue
                matching = [entry for entry in ledger_by_event.get(event_id, ())
                            if lead_id_text(entry.get("lead_id")) == lead_id_text(lead.get("_id"))
                            and str(entry.get("source_system") or "prop360").strip().casefold() == "prop360"
                            and str(entry.get("status") or "").strip().casefold() in {"created", "updated", "completed"}]
                if not matching:
                    continue
                source_codes = [normalize_identifier(value) for value in (
                    event.get("property_code"), *(entry.get("property_code") for entry in matching)
                ) if normalize_identifier(value)]
                if source_codes and any(source_code != code for source_code in source_codes):
                    continue
                # An update can belong to an older/reused lead. Require the update
                # itself to carry the property code before attributing it.
                if any(str(entry.get("status") or "").strip().casefold() == "updated" for entry in matching) and not source_codes:
                    continue
                valid_dates.append(event_at)
            if valid_dates:
                return min(valid_dates), "prop360_source"
            return None, "prop360_event_unreconciled"

        message = first_user_message(lead)
        message_at = coerce_utc_datetime(message.get("timestamp")) if message else None
        if not message_at:
            return None, "no_event_date_evidence"
        content = str(message.get("content") or "").casefold()
        resolution_alias = False
        for alias, path in resolver.lead_aliases(lead):
            field = path.split(".")[-1].split("[")[0]
            source = alias.source
            alias_resolution = resolver.resolve({"prospecto": {field: alias.external_id, "origen": source}})
            alias_text = str(alias.external_id or "").strip().casefold()
            resolves_to_code = alias_resolution.resolved_property_code == code
            if not resolves_to_code and source == "mercadolibre":
                # Prop360 stores MLC ids both with and without the separator
                # after the namespace. Accept only a unique same-namespace
                # master match; this does not alter the shared identity rules.
                compact_alias = alias_text.replace("-", "")
                matching_codes = {
                    candidate
                    for master_alias, candidates in getattr(resolver, "_alias_index", {}).items()
                    if master_alias.source == source
                    and str(master_alias.external_id).casefold().replace("-", "") == compact_alias
                    for candidate in candidates
                }
                resolves_to_code = matching_codes == {code}
            if alias_text and len(alias_text) >= 5 and alias_text in content and resolves_to_code:
                resolution_alias = True
                break
        if resolution_alias:
            return message_at, "inbound_message"

        manual_marker = source_type == "manual" and any(
            isinstance(item, Mapping)
            and str(item.get("role") or "").strip().casefold() == "system"
            and "lead ingresado manualmente" in str(item.get("content") or "").casefold()
            for item in lead.get("messages") or ()
        )
        if manual_marker:
            return message_at, "manual_intake"
        return None, "property_origin_not_proven"

    seen: set[tuple[str, str]] = set()
    for lead in leads or []:
        if lead.get("_test_lead") or lead.get("is_test"):
            continue
        lead_id = str(lead.get("_id") or "").strip()
        if not lead_id:
            continue
        resolution = resolver.resolve(lead)
        code = resolution.resolved_property_code
        if not code or code not in property_codes:
            if resolution.status in (LinkageStatus.AMBIGUOUS, LinkageStatus.CONFLICT):
                ambiguous += 1
                for candidate in set(resolution.candidates or ()) & set(property_codes):
                    unverified[candidate] += 1
                    ambiguous_by_code[candidate] += 1
                    reason_counts["ambiguous_property_identity"] += 1
            continue
        pair = (lead_id, code)
        if pair in seen:
            continue
        seen.add(pair)
        historical_counts[code] += 1
        created_at = _demand_capture_datetime(lead)
        if created_at and created_at <= (as_of_utc or datetime.now(timezone.utc)):
            if code not in latest_registered or created_at > latest_registered[code]:
                latest_registered[code] = created_at
        if not created_at:
            missing_created_at += 1
        prospecto = lead.get("prospecto") or {}
        link_at = next((coerce_utc_datetime(value) for value in (
            lead.get("property_linked_at"), prospecto.get("property_linked_at"),
            prospecto.get("codigo_linked_at"), prospecto.get("property_associated_at"),
        ) if coerce_utc_datetime(value)), None)
        event_at, evidence_kind = intake_evidence(lead, code, created_at)
        # An explicitly timestamped property link remains valid evidence for
        # older CRM records; it cannot override an unreconciled Prop360 event.
        has_prop360 = str(lead.get("source_type") or "").strip().casefold() == "prop360" or any(
            isinstance(event, Mapping)
            and str(event.get("source_system") or "").strip().casefold() == "prop360"
            for event in lead.get("source_events") or ()
        )
        if event_at is None and not has_prop360 and created_at and link_at and link_at <= created_at:
            event_at, evidence_kind = created_at, "dated_property_link"
        if not event_at:
            unverified[code] += 1
            reason_counts["missing_created_at" if not created_at else (evidence_kind or "no_event_date_evidence")] += 1
            continue
        if event_at > (as_of_utc or datetime.now(timezone.utc)):
            unverified[code] += 1
            reason_counts["future_event_date"] += 1
            continue
        if evidence_kind == "prop360_source":
            valid_prop360 += 1
        elif evidence_kind == "inbound_message":
            valid_inbound += 1
        elif evidence_kind == "manual_intake":
            valid_manual += 1
        if code not in latest or event_at > latest[code]:
            latest[code] = event_at
        if start_utc <= event_at < end_utc:
            counts[code] += 1
            if code not in latest_in_period or event_at > latest_in_period[code]:
                latest_in_period[code] = event_at
    return {
        "counts": counts, "historical_counts": historical_counts,
        "latest": latest, "latest_registered": latest_registered,
        "latest_in_period": latest_in_period, "unverified": unverified,
        "ambiguous_by_code": ambiguous_by_code,
        "ambiguous": ambiguous, "missing_created_at": missing_created_at,
        "pairs_seen": len(seen), "valid_prop360_event_links": valid_prop360,
        "valid_inbound_message_links": valid_inbound, "valid_manual_intake_links": valid_manual,
        "verified_historical_links": valid_prop360 + valid_inbound + valid_manual,
        "unverified_reason_counts": reason_counts,
    }


def _control_build_opportunity_rows(records, cycles_by_id, leads_by_id, resolver, intent_events=()):
    """Flatten an executive's assigned cohort to one row per lead/property pair."""
    codes = set()
    for episode in records or []:
        cycle = cycles_by_id.get(str(episode.get("cycle_id") or ""), {})
        lead = leads_by_id.get(str(episode.get("lead_id") or cycle.get("lead_id") or ""), {})
        code, _status = _control_resolve_episode_property(cycle, lead, resolver)
        if code:
            codes.add(code)
    result = []
    for code in codes:
        result.extend(_control_build_property_lead_details(records, cycles_by_id, leads_by_id, resolver, code, intent_events))
    return sorted(result, key=lambda row: (row.get("first_received_at") or "", row.get("property_code") or ""))


def _control_index_received_episodes(
    records: Iterable[Mapping[str, Any]],
    cycles_by_id: Mapping[str, Mapping[str, Any]],
    leads_by_id: Mapping[str, Mapping[str, Any]],
    resolver: PropertyIdentityResolver,
) -> dict[str, Any]:
    """Index only eligible received episodes by their canonical property."""
    episode_counts: dict[str, Counter] = defaultdict(Counter)
    lead_ids_by_code: dict[str, set[str]] = defaultdict(set)
    codes_by_lead: dict[str, set[str]] = defaultdict(set)
    first_assignment_by_pair: dict[tuple[str, str], datetime] = {}
    unlinked: list[dict[str, str]] = []
    seen_cycles: set[str] = set()
    received_lead_ids: set[str] = set()

    for episode in records or []:
        cycle_id = str(episode.get("cycle_id") or "").strip()
        if not cycle_id or cycle_id in seen_cycles:
            continue
        seen_cycles.add(cycle_id)
        cycle = cycles_by_id.get(cycle_id, {})
        lead_id = str(episode.get("lead_id") or cycle.get("lead_id") or "").strip()
        if not lead_id:
            unlinked.append({"assignment_cycle_id": cycle_id, "reason": "missing_lead_id"})
            continue
        received_lead_ids.add(lead_id)
        lead = leads_by_id.get(lead_id, {})
        code, link_status = _control_resolve_episode_property(cycle, lead, resolver)
        if not code:
            unlinked.append({
                "assignment_cycle_id": cycle_id,
                "lead_id": lead_id,
                "reason": link_status,
                "property_code": normalize_identifier(cycle.get("property_code")) or "",
            })
            continue

        kind = str(episode.get("assignment_type") or "unknown")
        episode_counts[code][kind] += 1
        lead_ids_by_code[code].add(lead_id)
        codes_by_lead[lead_id].add(code)
        assigned_at = coerce_utc_datetime(cycle.get("assigned_at"))
        pair = (lead_id, code)
        if assigned_at and (pair not in first_assignment_by_pair or assigned_at < first_assignment_by_pair[pair]):
            first_assignment_by_pair[pair] = assigned_at

    return {
        "episode_counts": episode_counts,
        "lead_ids_by_code": lead_ids_by_code,
        "codes_by_lead": codes_by_lead,
        "first_assignment_by_pair": first_assignment_by_pair,
        "received_lead_ids": received_lead_ids,
        "unlinked_episodes": unlinked,
    }


def _control_index_received_visit_intents(
    events: Iterable[Mapping[str, Any]],
    *,
    codes_by_lead: Mapping[str, set[str]],
    first_assignment_by_pair: Mapping[tuple[str, str], datetime],
) -> dict[str, Any]:
    """Count unique lead/property intent pairs within the received cohort.

    An intent earlier than the selected executive's first assignment is retained
    as customer interest, never labeled as work performed by that executive.
    """
    first_event_by_pair: dict[tuple[str, str], datetime] = {}
    unlinked = 0
    unlinked_leads: set[str] = set()
    for event in events or []:
        lead_id = str(event.get("lead_id") or "").strip()
        code = normalize_identifier(event.get("property_code"))
        if not lead_id or not code or code not in codes_by_lead.get(lead_id, set()):
            unlinked += 1
            if lead_id:
                unlinked_leads.add(lead_id)
            continue
        occurred_at = coerce_utc_datetime(event.get("occurred_at"))
        if not occurred_at:
            unlinked += 1
            unlinked_leads.add(lead_id)
            continue
        pair = (lead_id, code)
        if pair not in first_event_by_pair or occurred_at < first_event_by_pair[pair]:
            first_event_by_pair[pair] = occurred_at

    intents_by_code: dict[str, set[str]] = defaultdict(set)
    before_assignment: set[tuple[str, str]] = set()
    at_or_after_assignment: set[tuple[str, str]] = set()
    assignment_time_missing = 0
    for pair, occurred_at in first_event_by_pair.items():
        lead_id, code = pair
        intents_by_code[code].add(lead_id)
        assigned_at = first_assignment_by_pair.get(pair)
        if not assigned_at:
            assignment_time_missing += 1
        elif occurred_at < assigned_at:
            before_assignment.add(pair)
        else:
            at_or_after_assignment.add(pair)
    return {
        "intents_by_code": intents_by_code,
        "events": [
            {"lead_id": lead_id, "property_code": code, "occurred_at": occurred_at.isoformat(),
             "before_assignment": bool(first_assignment_by_pair.get((lead_id, code)) and occurred_at < first_assignment_by_pair[(lead_id, code)])}
            for (lead_id, code), occurred_at in sorted(first_event_by_pair.items(), key=lambda item: (item[0][1], item[1]))
        ],
        "linked_pairs": len(first_event_by_pair),
        "before_assignment_pairs": len(before_assignment),
        "at_or_after_assignment_pairs": len(at_or_after_assignment),
        "assignment_time_missing_pairs": assignment_time_missing,
        "unlinked_events": unlinked,
        "unlinked_lead_ids": unlinked_leads,
    }


def query_monthly_control_portfolio_demand(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    executive: Optional[str] = None,
) -> dict:
    """Read-only property demand joined to current Prop360 stock and executive episodes.

    Received leads and episodes are both attributed from eligible assignment
    cycles by ``assigned_at``. Current Prop360 ownership only adds current-stock
    rows; it is never treated as historical lead responsibility.
    """
    try:
        start_date = datetime.strptime(str(period_start), "%Y-%m-%d").date()
        end_date = datetime.strptime(str(period_end), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return {"availability": {"status": "unavailable", "reason": "Selecciona fechas válidas."}, "rows": []}
    today = datetime.now(CHILE_TZ).date()
    if start_date < datetime(2026, 8, 1).date() or end_date > today or start_date > end_date:
        return {"availability": {"status": "unavailable", "reason": "La cobertura de cohortes comparables comienza el 1 de agosto de 2026 y solo llega a fechas ya observadas."}, "rows": []}
    selected = str(executive or "").strip()
    if not selected:
        return {
            "availability": {"status": "executive_required", "reason": "Selecciona un ejecutivo para mostrar su cartera actual y los episodios recibidos."},
            "rows": [], "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "lead_date_field": "crm_assignment_cycles.assigned_at", "episode_date_field": "crm_assignment_cycles.assigned_at",
        }

    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(start_date.isoformat(), end_date.isoformat())
    cycles = list(db["crm_assignment_cycles"].find(
        {"assigned_at": {"$gte": start_utc, "$lt": end_utc},
         "assigned_to_display_name": selected,
         "assignment_cycle_id": {"$exists": True, "$nin": [None, ""]},
         "lead_id": {"$exists": True, "$ne": None}},
        {"lead_id": 1, "assignment_cycle_id": 1, "assigned_to_display_name": 1,
         "assigned_at": 1, "property_code": 1, "temperature_at_assignment": 1, "first_valid_management_at": 1,
         "reassignment_reason": 1, "reassignment_meta": 1, "assigned_by": 1,
         "assigned_to_user_id": 1, "assignment_number": 1, "cycle_origin": 1,
         "source_system": 1, "source_event_type": 1,
         "closed_reason": 1, "reason": 1, "policy_repair_id": 1,
         "policy_repair_version": 1, "policy_repair_reason": 1,
         "policy_repair_previous_owner_user_id": 1, "policy_repair_selected_user_id": 1,
         "cycle_status": 1, "schema_version": 1},
    ))
    cycle_lead_ids = {cycle.get("lead_id") for cycle in cycles if cycle.get("lead_id") is not None}
    cycle_leads = list(db["leads"].find(
        {"_id": {"$in": list(cycle_lead_ids)}},
        {"_id": 1, "_test_lead": 1, "is_test": 1, "phone": 1, "created_at": 1,
         "name": 1, "nombre": 1, "nombre_cliente": 1, "client_name": 1,
         "lead_origin": 1, "origin": 1, "prospecto.origen": 1,
         "prospecto.nombre": 1, "prospecto.nombre_cliente": 1, "prospecto.contact_name": 1,
         "prospecto.codigo": 1, "prospecto.codigo_mercadolibre": 1,
         "prospecto.codigo_yapo": 1, "prospecto.codigo_propiedad": 1,
         "prospecto.propiedad_codigo": 1, "prospecto.fuente_lead": 1,
         "prospecto.portal_origen": 1, "prospecto.origen_anuncio": 1,
         "prospecto.plataforma_origen": 1},
    )) if cycle_lead_ids else []
    cycle_ids = sorted({str(c.get("assignment_cycle_id")) for c in cycles if c.get("assignment_cycle_id")})
    related_cycles = list(db["crm_assignment_cycles"].find(
        {"lead_id": {"$in": list(cycle_lead_ids)}} if cycle_lead_ids else {"assignment_cycle_id": {"$in": cycle_ids}},
        {"lead_id": 1, "assignment_cycle_id": 1, "assigned_to_display_name": 1,
         "assigned_at": 1, "reassignment_meta": 1},
    )) if cycle_ids else []
    related_cycle_ids = sorted({str(c.get("assignment_cycle_id")) for c in related_cycles if c.get("assignment_cycle_id")})
    transition_audits = list(db["crm_sla_reassignment_audit_v1"].find(
        {"event_type": "SLA_REASSIGNMENT_COMMITTED",
         "$or": [{"source_cycle_id": {"$in": related_cycle_ids}},
                 {"new_cycle_id": {"$in": related_cycle_ids}},
                 {"destination_cycle_id": {"$in": related_cycle_ids}}]},
        {"lead_id": 1, "source_cycle_id": 1, "new_cycle_id": 1,
         "destination_cycle_id": 1, "event_type": 1, "reassigned_at": 1},
    )) if related_cycle_ids else []
    cycle_id_set = set(cycle_ids)
    incoming_audits = [audit for audit in transition_audits
                       if str(audit.get("new_cycle_id") or audit.get("destination_cycle_id") or "") in cycle_id_set]
    reassignment_recipient_by_cycle = _control_episode_reassignment_recipients(related_cycles, transition_audits)
    reassignment_summary_by_lead = _control_lead_reassignment_summary(
        related_cycles, reassignment_recipient_by_cycle,
    )
    metrics = build_month_metrics(
        cycles=cycles, leads=cycle_leads, events=[], results=[], sla_audits=incoming_audits,
        executive=selected, as_of=datetime.now(timezone.utc),
    )

    identity_leads = list(cycle_leads)
    identity_leads.extend(
        {"prospecto": {"codigo": cycle.get("property_code")}}
        for cycle in cycles if normalize_identifier(cycle.get("property_code"))
    )
    lead_alias_resolver = PropertyIdentityResolver([])
    alias_values: dict[str, set[str]] = defaultdict(set)
    for lead in identity_leads:
        for alias, _path in lead_alias_resolver.lead_aliases(lead):
            alias_values[alias.source].add(alias.external_id)
    prop_filter = _control_property_candidate_query(identity_leads)
    selected_owner_names = _control_person_aliases(selected)
    selected_property_filter = {"$or": [
        {"estado.ejecutivo": {"$in": selected_owner_names}},
        {"resumen.ejecutivo": {"$in": selected_owner_names}},
        {"estado.captador": {"$in": selected_owner_names}},
        {"estado.responsable": {"$in": selected_owner_names}},
        {"resumen.snapshot_listado.captador": {"$in": selected_owner_names}},
    ]}
    candidate_docs = list(db["universo_cartera_prop360"].find(
        {"$or": [prop_filter, selected_property_filter]},
        {"codigo": 1, "disponible_prop360": 1, "estado": 1, "resumen": 1, "historial_cambios": 1,
         "ubicacion.region": 1, "ubicacion.comuna": 1, "tipo_operacion": 1, "publicaciones.procasa": 1,
         "publicaciones.portal_inmobiliario.publicaciones": 1,
         "publicaciones.yapo.publicaciones": 1, "publicaciones.toctoc.publicaciones": 1,
         "publicaciones.chilepropiedades.publicaciones": 1,
         "publicaciones.chilepropiedades.codigo_venta": 1,
         "publicaciones.chilepropiedades.codigo_arriendo": 1,
         "publicaciones.proppit.publicaciones": 1},
    ))
    resolver = PropertyIdentityResolver(candidate_docs)
    cycle_by_id = {str(c.get("assignment_cycle_id")): c for c in cycles}
    lead_by_id = {str(l.get("_id")): l for l in cycle_leads}
    valid_rows = metrics.get("records") or []
    received_index = _control_index_received_episodes(valid_rows, cycle_by_id, lead_by_id, resolver)
    episode_counts = received_index["episode_counts"]
    episode_leads = received_index["lead_ids_by_code"]
    codes_by_received_lead = received_index["codes_by_lead"]
    first_assignment_by_pair = received_index["first_assignment_by_pair"]
    received_lead_ids = received_index["received_lead_ids"]
    unlinked_episodes = received_index["unlinked_episodes"]

    intent_coverage_start = datetime(2026, 8, 20).date()
    intent_is_full = start_date >= intent_coverage_start
    intent_index = {
        "intents_by_code": defaultdict(set), "linked_pairs": 0,
        "before_assignment_pairs": 0, "at_or_after_assignment_pairs": 0,
        "assignment_time_missing_pairs": 0, "unlinked_events": 0,
        "unlinked_lead_ids": set(),
    }


    if intent_is_full:
        event_rows = list(db["event_log"].aggregate([
            {"$match": {"event": "visit_intent_detected", "lead_id": {"$in": sorted(received_lead_ids)}}},
            {"$set": {"_event_normalized": {"$convert": {"input": "$timestamp", "to": "date", "onError": None, "onNull": None}}}},
            {"$match": {"$expr": {"$and": [{"$gte": ["$_event_normalized", start_utc]}, {"$lt": ["$_event_normalized", end_utc]}]}}},
            {"$project": {"lead_id": 1, "property_id": 1, "property_code": 1, "_event_normalized": 1}},
        ]))
        linked_event_rows = []
        for event in event_rows:
            event_property = event.get("property_code") or event.get("property_id")
            resolution = resolver.resolve({"prospecto": {"codigo": event_property}}) if event_property else None
            if resolution and resolution.resolved_property_code:
                linked_event_rows.append({
                    "lead_id": str(event.get("lead_id") or ""),
                    "property_code": resolution.resolved_property_code,
                    "occurred_at": event.get("_event_normalized"),
                })
            else:
                linked_event_rows.append({
                    "lead_id": str(event.get("lead_id") or ""),
                    "property_code": None,
                    "occurred_at": event.get("_event_normalized"),
                })
        intent_index = _control_index_received_visit_intents(
            linked_event_rows,
            codes_by_lead=codes_by_received_lead,
            first_assignment_by_pair=first_assignment_by_pair,
        )
    intent_by_code = intent_index["intents_by_code"]

    properties: dict[str, dict] = {}
    for doc in candidate_docs:
        code = normalize_identifier(doc.get("codigo"))
        if code:
            current = properties.get(code)
            if current is None or (_control_is_currently_active_property(doc) and not _control_is_currently_active_property(current)):
                properties[code] = doc
    portfolio_codes = {
        code for code, doc in properties.items()
        if _control_is_currently_active_property(doc)
        and (doc.get("estado") or {}).get("oficina") == OPS_PORTFOLIO_OFFICE
        and _control_current_relation(
            selected,
            (doc.get("estado") or {}).get("ejecutivo") or (doc.get("resumen") or {}).get("ejecutivo") or (doc.get("estado") or {}).get("responsable"),
            ((doc.get("resumen") or {}).get("snapshot_listado") or {}).get("captador") or (doc.get("estado") or {}).get("captador"),
        ) == "Cartera del ejecutivo seleccionado"
    }
    portfolio_docs = [properties[code] for code in sorted(portfolio_codes)]
    # Read only the price-change events for properties in this current portfolio.
    # The existing canonical parser deduplicates embedded and separate history.
    price_events = list(db["universo_cartera_prop360_historial"].find(
        {"codigo": {"$in": sorted(portfolio_codes)}},
        {"_id": 0, "codigo": 1, "campo": 1, "fecha": 1,
         "valor_anterior": 1, "valor_nuevo": 1},
    )) if portfolio_codes else []
    price_events_by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in price_events:
        event_code = normalize_identifier(event.get("codigo"))
        if event_code:
            price_events_by_code[event_code].append(event)
    portfolio_resolver = PropertyIdentityResolver(portfolio_docs)
    lead_query = _control_property_leads_query(portfolio_docs)
    demand_leads = list(db["leads"].find(
        lead_query,
        {"_id": 1, "_test_lead": 1, "is_test": 1, "created_at": 1, "source_type": 1,
         "source_events": 1, "messages": {"$slice": 2}, "prospecto": 1,
         "property_linked_at": 1},
    )) if portfolio_docs else []
    as_of_utc = datetime.now(timezone.utc)
    ingest_event_ids = sorted({
        str(event.get("source_event_id") or "").strip()
        for lead in demand_leads
        for event in (lead.get("source_events") or [])
        if isinstance(event, Mapping)
        and str(event.get("source_system") or "").strip().casefold() == "prop360"
        and str(event.get("source_event_id") or "").strip()
    })
    ingest_events = list(db["lead_ingest_events"].find(
        {"source_system": "prop360", "source_event_id": {"$in": ingest_event_ids}},
        {"source_system": 1, "source_event_id": 1, "lead_id": 1, "status": 1, "property_code": 1},
    )) if ingest_event_ids else []
    demand_index = _control_index_property_demand(
        demand_leads, portfolio_resolver, portfolio_codes, start_utc, end_utc, as_of_utc,
        ingest_events=ingest_events,
    )
    demand_counts = demand_index["counts"]
    historical_lead_counts = demand_index["historical_counts"]
    latest_lead = demand_index["latest"]
    latest_lead_registered = demand_index["latest_registered"]
    latest_period_lead = demand_index["latest_in_period"]
    unverified_link_time = demand_index["unverified"]
    ambiguous_link_by_code = demand_index["ambiguous_by_code"]
    ambiguous_pairs = demand_index["ambiguous"]
    missing_created_at = demand_index["missing_created_at"]
    seen_demand_pairs = demand_index["pairs_seen"]
    total_demand_pairs = sum(demand_counts.values())
    as_of = as_of_utc.astimezone(CHILE_TZ)
    rows = []
    for code in portfolio_codes:
        doc = properties.get(code, {})
        state = doc.get("estado") or {}
        summary = doc.get("resumen") or {}
        snapshot = summary.get("snapshot_listado") or {}
        responsible = str(state.get("ejecutivo") or summary.get("ejecutivo") or state.get("responsable") or "").strip()
        captor = str(snapshot.get("captador") or state.get("captador") or "").strip()
        operation = (doc.get("tipo_operacion") or {})
        operation_label = snapshot.get("operacion") or "No disponible"
        if operation.get("venta") and operation.get("arriendo"):
            operation_label = "Venta + arriendo"
        elif operation.get("venta"):
            operation_label = "Venta"
        elif operation.get("arriendo"):
            operation_label = "Arriendo"
        procasa = (doc.get("publicaciones") or {}).get("procasa") or {}
        procasa_url = procasa.get("url_procasa") or next((
            record.get("url") for record in (procasa.get("publicaciones") or {}).values()
            if isinstance(record, Mapping) and str(record.get("url") or "").startswith("https://www.procasa.cl/")
        ), None)
        if not str(procasa_url or "").startswith("https://www.procasa.cl/"):
            procasa_url = None
        last_lead = latest_lead.get(code)
        latest_price_change = get_latest_observable_price_change(
            code,
            as_of_utc,
            embedded_events=doc.get("historial_cambios") or (),
            separate_events=price_events_by_code.get(code, ()),
        )
        days_since_last = (as_of.date() - last_lead.astimezone(CHILE_TZ).date()).days if last_lead else None
        leads_in_period = int(demand_counts.get(code, 0))
        demand_unverified = int(unverified_link_time.get(code, 0))
        demand_status = "confirmed_demand" if leads_in_period > 0 else (
            "data_to_review" if demand_unverified else "no_demand_verified"
        )
        rows.append({
            "property_code": code,
            "procasa_url": procasa_url,
            "region": (doc.get("ubicacion") or {}).get("region") or snapshot.get("region") or "No disponible",
            "commune": (doc.get("ubicacion") or {}).get("comuna") or snapshot.get("comuna") or "No disponible",
            "property_type": operation.get("tipo") or snapshot.get("tipo") or "No disponible",
            "operation": operation_label,
            "current_responsible": responsible or None,
            "current_captor": captor or None,
            "current_relation": _control_current_relation(selected, responsible, captor),
            "active": bool(doc.get("disponible_prop360")) if "disponible_prop360" in doc else None,
            "current_price": {key: _to_positive_float(value) for key, value in _inventory_price(doc).items()},
            "latest_price_change": latest_price_change.to_dict() if latest_price_change else None,
            "exposure_date": None,
            "exposure_status": "not_available",
            "leads_unique": None if demand_unverified and not leads_in_period else leads_in_period,
            "leads_crm_history_verified": int(historical_lead_counts.get(code, 0)),
            "leads_crm_history_ambiguous": int(ambiguous_link_by_code.get(code, 0)),
            "last_lead_registered_at": latest_lead_registered.get(code).isoformat() if latest_lead_registered.get(code) else None,
            "participation_pct": _control_property_participation(leads_in_period, total_demand_pairs) if not (demand_unverified and not leads_in_period) else None,
            "last_lead_at": last_lead.isoformat() if last_lead else None,
            "last_period_lead_at": latest_period_lead.get(code).isoformat() if latest_period_lead.get(code) else None,
            "days_since_last_lead": days_since_last,
            "demand_temporal_basis": "Prop360 source_events.contact_date reconciliado por source_event_id/lead_id; o primer mensaje entrante identificado con alias verificado; no usa created_at de importación por sí solo",
            "demand_status": demand_status,
            "unverified_temporal_links": demand_unverified,
            "last_lead_status": "available" if last_lead else ("unverified" if demand_unverified else "no_leads_in_observed_history"),
        })
    rows.sort(key=lambda row: (-(row["leads_unique"] or 0), row["property_code"]))
    # Build the second, independent population from the received executive cohort.
    metric_by_cycle = {str(item.get("cycle_id")): item for item in valid_rows if item.get("cycle_id")}
    opportunity_rows = _control_build_opportunity_rows(
        valid_rows, cycle_by_id, lead_by_id, resolver,
        intent_index.get("events", []) if intent_is_full else [],
    )
    for record in valid_rows:
        record.update(reassignment_recipient_by_cycle.get(str(record.get("cycle_id") or ""), {}))
        record.update(reassignment_summary_by_lead.get(str(record.get("lead_id") or ""), {}))
    for row in opportunity_rows:
        row.update(reassignment_summary_by_lead.get(str(row.get("lead_id") or ""), {}))
        lead = lead_by_id.get(str(row.get("lead_id") or ""), {})
        property_doc = properties.get(normalize_identifier(row.get("property_code")) or "", {})
        property_state = property_doc.get("estado") or {}
        property_summary = property_doc.get("resumen") or {}
        property_snapshot = property_summary.get("snapshot_listado") or {}
        current_responsible = str(
            property_state.get("ejecutivo")
            or property_summary.get("ejecutivo")
            or property_state.get("responsable")
            or ""
        ).strip() or None
        current_captor = str(
            property_snapshot.get("captador")
            or property_state.get("captador")
            or ""
        ).strip() or None
        row["property_current_responsible"] = current_responsible
        row["property_current_captor"] = current_captor
        row["property_region"] = str(
            (property_doc.get("ubicacion") or {}).get("region")
            or property_snapshot.get("region")
            or "No disponible"
        ).strip() or "No disponible"
        row["property_commune"] = str(
            (property_doc.get("ubicacion") or {}).get("comuna")
            or property_snapshot.get("comuna")
            or "No disponible"
        ).strip() or "No disponible"
        property_operation = property_doc.get("tipo_operacion") or {}
        row["property_type"] = str(
            property_operation.get("tipo")
            or property_snapshot.get("tipo")
            or "No disponible"
        ).strip() or "No disponible"
        operation_label = property_snapshot.get("operacion") or "No disponible"
        if property_operation.get("venta") and property_operation.get("arriendo"):
            operation_label = "Venta + arriendo"
        elif property_operation.get("venta"):
            operation_label = "Venta"
        elif property_operation.get("arriendo"):
            operation_label = "Arriendo"
        row["property_operation"] = str(operation_label).strip() or "No disponible"
        row["property_price"] = {
            key: _to_positive_float(value)
            for key, value in _inventory_price(property_doc).items()
        }
        row["property_holder_as_of"] = as_of.isoformat()
        row["created_at"] = coerce_utc_datetime(lead.get("created_at")).isoformat() if coerce_utc_datetime(lead.get("created_at")) else None
        row["episodes"] = [
            {**episode, **{
                "sla_state": metric_by_cycle.get(str(episode.get("assignment_cycle_id")), {}).get("sla_state"),
                "first_management_at": metric_by_cycle.get(str(episode.get("assignment_cycle_id")), {}).get("first_management_at"),
                "first_management_business_minutes": metric_by_cycle.get(str(episode.get("assignment_cycle_id")), {}).get("first_management_business_minutes"),
            }, **reassignment_recipient_by_cycle.get(str(episode.get("assignment_cycle_id") or ""), {})}
            for episode in row.get("episodes", [])
        ]
        first_episode = row["episodes"][0] if row.get("episodes") else {}
        row["sla_state"] = first_episode.get("sla_state") or "insufficient_data"
        row["visit_intent"] = bool(row.get("visit_intent"))
        row["visit_intent_status"] = (
            "Intención explícita acreditada" if row.get("visit_intent") else
            "Evento sin vinculación verificable" if str(row.get("lead_id") or "") in intent_index.get("unlinked_lead_ids", set()) else
            "Sin intención registrada" if intent_is_full else "No disponible · cobertura incompleta"
        )
    received = metrics.get("assignment_types") or {}
    return {
        "availability": {"status": "available", "reason": None},
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "executive": selected,
        "lead_date_field": "source_events.contact_date (Prop360 con ledger); primer mensaje entrante con alias verificable; fecha CRM solo con evidencia de vínculo en el ingreso",
        "episode_date_field": "crm_assignment_cycles.assigned_at (fecha local Chile)",
        "stock_as_of": "estado actual de universo_cartera_prop360; no representa titularidad histórica",
        "demand_definition": "Demanda CRM de propiedades activas actuales de Procasa Sucre, independiente del ejecutivo receptor; un lead distinto por código canónico y fecha factual verificable.",
        "demand_coverage": {
            "status": "available", "leads_period": total_demand_pairs,
            "linked_lead_property_pairs": seen_demand_pairs,
            "current_portfolio_properties": len(portfolio_codes),
            "properties_with_verified_demand": sum(1 for row in rows if row["demand_status"] == "confirmed_demand"),
            "properties_without_leads_period": sum(1 for row in rows if row["demand_status"] == "no_demand_verified"),
            "properties_needing_review": sum(1 for row in rows if row["demand_status"] == "data_to_review"),
            "ambiguous_links": ambiguous_pairs,
            "missing_created_at": missing_created_at,
            "verified_historical_links": int(demand_index.get("verified_historical_links") or 0),
            "valid_prop360_event_links": int(demand_index.get("valid_prop360_event_links") or 0),
            "valid_inbound_message_links": int(demand_index.get("valid_inbound_message_links") or 0),
            "valid_manual_intake_links": int(demand_index.get("valid_manual_intake_links") or 0),
            "unverified_reason_counts": dict(demand_index.get("unverified_reason_counts") or {}),
            "properties_with_received_episodes": len(episode_counts),
            "current_portfolio_only_properties": len(portfolio_codes - set(episode_counts)),
            "episodes_without_property": len(unlinked_episodes),
            "unlinked_episode_refs": unlinked_episodes,
            "participation_denominator_pairs": total_demand_pairs,
            "temporally_unverified_links": int(sum(unverified_link_time.values())),
            "temporal_linkage_status": "partial: source contact dates and direct CRM intake events are recovered only when event, lead identity, and property linkage are verifiable; remaining historical links are marked for review, not as zero",
        },
        "episode_coverage": {
            "status": "available", "episodes": int(metrics.get("episodes_received") or 0),
            "leads_received": int(metrics.get("unique_leads_received") or 0),
            "originals": int(received.get("original") or 0),
            "sla_rescues": int(received.get("sla_rescue") or 0),
            "other_transfers": int(received.get("other_transfer") or 0),
            "unclassified": int(received.get("unknown") or 0),
            "episodes_without_property": len(unlinked_episodes),
        },
        "visit_intent_coverage": {
            "status": "available" if intent_is_full else "unavailable",
            "event_date_field": "event_log.timestamp (fecha local Chile)",
            "assignment_date_field": "crm_assignment_cycles.assigned_at (fecha local Chile)",
            "coverage_start": intent_coverage_start.isoformat(),
            "linked_lead_property_pairs": int(intent_index.get("linked_pairs") or 0),
            "before_selected_assignment_pairs": int(intent_index.get("before_assignment_pairs") or 0),
            "at_or_after_selected_assignment_pairs": int(intent_index.get("at_or_after_assignment_pairs") or 0),
            "assignment_time_missing_pairs": int(intent_index.get("assignment_time_missing_pairs") or 0),
            "unlinked_events_for_received_leads": int(intent_index.get("unlinked_events") or 0),
            "reason": None if intent_is_full else "La captura de intenciones explícitas comienza el 20-08-2026; esta ventana no está cubierta completamente.",
        },
        "visit_intent_events": intent_index.get("events", []) if intent_is_full else [],
        "rows": rows,
        "opportunities": opportunity_rows,
    }


def _control_episode_reassignment_recipients(
    cycles: Iterable[Mapping[str, Any]],
    audits: Iterable[Mapping[str, Any]] = (),
) -> dict[str, dict[str, str]]:
    """Resolve a cycle's next executive only from explicit, same-lead links.

    A successor cycle's ``reassignment_meta.previous_cycle_id`` is the canonical
    lineage for transfers. Committed SLA audit events are also accepted when
    they identify both source and destination cycle IDs. No current owner or
    assignment category is used to infer a recipient.
    """
    cycle_by_id = {
        str(cycle.get("assignment_cycle_id") or "").strip(): cycle
        for cycle in cycles or ()
        if str(cycle.get("assignment_cycle_id") or "").strip()
    }
    recipients_by_source: dict[str, set[str]] = defaultdict(set)

    def add_transition(source_id: Any, target_id: Any, lead_id: Any = None) -> None:
        source_key, target_key = str(source_id or "").strip(), str(target_id or "").strip()
        source, target = cycle_by_id.get(source_key), cycle_by_id.get(target_key)
        if not source or not target or source_key == target_key:
            return
        source_lead = str(source.get("lead_id") or "").strip()
        target_lead = str(target.get("lead_id") or "").strip()
        if not source_lead or source_lead != target_lead:
            return
        if lead_id is not None and str(lead_id).strip() != source_lead:
            return
        source_owner = str(source.get("assigned_to_display_name") or "").strip()
        target_owner = str(target.get("assigned_to_display_name") or "").strip()
        if not source_owner or not target_owner or source_owner.casefold() == target_owner.casefold():
            return
        source_at = coerce_utc_datetime(source.get("assigned_at"))
        target_at = coerce_utc_datetime(target.get("assigned_at"))
        if source_at and target_at and target_at < source_at:
            return
        recipients_by_source[source_key].add(target_owner)

    for target in cycle_by_id.values():
        meta = target.get("reassignment_meta") or {}
        previous_cycle_id = meta.get("previous_cycle_id") if isinstance(meta, Mapping) else None
        if previous_cycle_id:
            add_transition(previous_cycle_id, target.get("assignment_cycle_id"), target.get("lead_id"))

    for audit in audits or ():
        if str(audit.get("event_type") or "") != "SLA_REASSIGNMENT_COMMITTED":
            continue
        add_transition(
            audit.get("source_cycle_id"),
            audit.get("new_cycle_id") or audit.get("destination_cycle_id"),
            audit.get("lead_id"),
        )

    resolved: dict[str, dict[str, str]] = {}
    for source_id, names in recipients_by_source.items():
        if len(names) == 1:
            resolved[source_id] = {"reassigned_to": next(iter(names)), "reassignment_status": "confirmed"}
        else:
            resolved[source_id] = {"reassigned_to": "", "reassignment_status": "ambiguous"}
    return resolved


def _control_lead_reassignment_summary(
    cycles: Iterable[Mapping[str, Any]],
    recipients_by_cycle: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, Any]]:
    """Summarize confirmed transfers and the latest recorded cycle owner per lead."""
    cycles_by_lead: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for cycle in cycles or ():
        lead_id = str(cycle.get("lead_id") or "").strip()
        if lead_id:
            cycles_by_lead[lead_id].append(cycle)

    summary: dict[str, dict[str, Any]] = {}
    for lead_id, lead_cycles in cycles_by_lead.items():
        confirmed_sources = {
            str(cycle.get("assignment_cycle_id") or "").strip()
            for cycle in lead_cycles
            if str((recipients_by_cycle.get(str(cycle.get("assignment_cycle_id") or "")) or {}).get("reassignment_status") or "") == "confirmed"
        }
        has_ambiguous = any(
            str((recipients_by_cycle.get(str(cycle.get("assignment_cycle_id") or "")) or {}).get("reassignment_status") or "") == "ambiguous"
            for cycle in lead_cycles
        )
        latest_cycle = max(
            lead_cycles,
            key=lambda cycle: (
                coerce_utc_datetime(cycle.get("assigned_at")) or datetime.min.replace(tzinfo=timezone.utc),
                str(cycle.get("assignment_cycle_id") or ""),
            ),
        )
        status = "confirmed" if confirmed_sources and not has_ambiguous else (
            "partial" if confirmed_sources and has_ambiguous else
            "ambiguous" if has_ambiguous else "not_confirmed"
        )
        summary[lead_id] = {
            "lead_reassignment_status": status,
            "lead_reassignment_count": len(confirmed_sources),
            "last_assigned_executive": str(latest_cycle.get("assigned_to_display_name") or "").strip() or None,
        }
    return summary


def _control_assignment_reason_label(episode: Mapping[str, Any], cycle: Mapping[str, Any]) -> str:
    kind = str(episode.get("assignment_type") or "unknown")
    return {
        "original": "Original",
        "sla_rescue": "Rescate por SLA",
        "other_transfer": "Otra transferencia",
        "unknown": "No clasificable",
    }.get(kind, "No clasificable")


def _control_cycle_trigger_label(cycle: Mapping[str, Any]) -> str:
    """Return the recorded event that opened a cycle, not its transfer category."""
    assigned_by = str(cycle.get("assigned_by") or "").strip().casefold()
    source_system = str(cycle.get("source_system") or "").strip().casefold()
    reason = str(cycle.get("reason") or cycle.get("reassignment_reason") or "").strip().casefold()
    origin = str(cycle.get("cycle_origin") or "").strip().casefold()
    if assigned_by == "prop360_extractor" or source_system == "prop360":
        return "Ingreso de un evento desde Prop360"
    if reason == "inbound_message" or origin == "inbound_message":
        return "Mensaje entrante al CRM"
    if reason == "lead_created":
        return "Ingreso del lead al CRM"
    if reason or origin:
        return "Evento registrado: " + (reason or origin).replace("_", " ")
    return "Motivo de apertura no registrado"


def _control_lead_display_fields(lead: Mapping[str, Any]) -> dict[str, Any]:
    prospecto = lead.get("prospecto") or {}
    name = next((str(value).strip() for value in (
        lead.get("name"), lead.get("nombre"), lead.get("nombre_cliente"),
        lead.get("client_name"), prospecto.get("nombre"),
        prospecto.get("nombre_cliente"), prospecto.get("contact_name"),
    ) if value and str(value).strip()), None)
    origin = str(prospecto.get("origen") or lead.get("origin") or lead.get("lead_origin") or "").strip()
    channel = next((str(value).strip() for value in (
        prospecto.get("canal_origen"), lead.get("channel"), lead.get("canal"),
    ) if value and str(value).strip()), None)
    provider = next((str(value).strip() for value in (
        prospecto.get("portal_origen"), prospecto.get("fuente_lead"),
        prospecto.get("origen_anuncio"), prospecto.get("plataforma_origen"),
        lead.get("lead_origin"),
    ) if value and str(value).strip()), None)
    origin_key = origin.casefold().replace(" ", "")
    channel_origins = {"whatsapp", "formulario", "web", "chatbot", "webchat"}
    known_providers = {"yapo", "portalinmobiliario", "mercadolibre", "mercadolibre.com", "toctoc", "proppit", "chilepropiedades"}
    if not channel and origin_key in channel_origins:
        channel = origin
    if not provider and origin_key in known_providers:
        provider = origin
    if channel and channel.casefold().replace(" ", "") in known_providers:
        provider = provider or channel
        channel = None
    # Treat portal-specific ID fields as source evidence when explicit source
    # fields are absent; never derive an organic source from WhatsApp.
    if provider and provider.casefold().replace(" ", "") in channel_origins:
        provider = None
    if not provider:
        if prospecto.get("codigo_yapo"):
            provider = "Yapo"
        elif prospecto.get("codigo_mercadolibre"):
            provider = "MercadoLibre"
    return {"name": name, "origin": origin or None, "channel": channel, "provider": provider}


def _control_has_verifiable_messages(lead: Mapping[str, Any], lead_id: str) -> bool:
    for message in lead.get("messages") or []:
        if not isinstance(message, Mapping) or not isinstance(message.get("content"), str) or not message.get("content", "").strip():
            continue
        message_lead_id = str(message.get("lead_id") or "").strip()
        if not message_lead_id or message_lead_id == str(lead_id):
            return True
    return False


def _control_build_property_lead_details(
    records: Iterable[Mapping[str, Any]],
    cycles_by_id: Mapping[str, Mapping[str, Any]],
    leads_by_id: Mapping[str, Mapping[str, Any]],
    resolver: PropertyIdentityResolver,
    property_code: str,
    intent_events: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Group eligible received cycles into unique clients for one property."""
    selected_code = normalize_identifier(property_code)
    by_lead: dict[str, dict[str, Any]] = {}
    for episode in records or []:
        cycle_id = str(episode.get("cycle_id") or "").strip()
        cycle = cycles_by_id.get(cycle_id, {})
        lead_id = str(episode.get("lead_id") or cycle.get("lead_id") or "").strip()
        lead = leads_by_id.get(lead_id, {})
        resolved_code, _status = _control_resolve_episode_property(cycle, lead, resolver)
        if not lead_id or resolved_code != selected_code:
            continue
        row = by_lead.setdefault(lead_id, {
            "lead_id": lead_id,
            "name": _control_lead_display_fields(lead)["name"],
            "temperature": None,
            "channel": _control_lead_display_fields(lead)["channel"],
            "provider": _control_lead_display_fields(lead)["provider"],
            "visit_intent": False,
            "episodes": [],
            "conversation_available": _control_has_verifiable_messages(lead, lead_id),
        })
        temp = str(cycle.get("temperature_at_assignment") or "").strip().upper()
        row["episodes"].append({
            "assignment_cycle_id": cycle_id,
            "assigned_at": episode.get("assigned_at"),
            "assigned_to_display_name": str(cycle.get("assigned_to_display_name") or "").strip() or None,
            "temperature_at_assignment": temp or None,
            "temperature_label": "Hot" if temp == "HOT" else "Normal" if temp in {"NORMAL", "COLD"} else "Sin clasificar",
            "assignment_type": episode.get("assignment_type") or "unknown",
            "assignment_label": _control_assignment_reason_label(episode, cycle),
            "assignment_reason_detail": str(cycle.get("reassignment_reason") or cycle.get("reason") or cycle.get("cycle_origin") or "").strip() or None,
            "assignment_trigger": _control_cycle_trigger_label(cycle),
            "hot_escalated_at": coerce_utc_datetime(cycle.get("hot_started_at") or cycle.get("temperature_transitioned_at")).isoformat() if coerce_utc_datetime(cycle.get("hot_started_at") or cycle.get("temperature_transitioned_at")) else None,
            "sla_state": episode.get("sla_state"),
            "first_management_at": episode.get("first_management_at"),
            "first_management_source": episode.get("first_management_source"),
            "first_management_business_minutes": episode.get("first_management_business_minutes"),
            "sla_not_evaluable_reason": episode.get("sla_not_evaluable_reason"),
        })
    first_intent: dict[str, datetime] = {}
    for event in intent_events or []:
        lead_id = str(event.get("lead_id") or "").strip()
        if lead_id not in by_lead:
            continue
        occurred_at = coerce_utc_datetime(event.get("timestamp") or event.get("occurred_at"))
        if not occurred_at:
            continue
        if event.get("property_code"):
            event_code = normalize_identifier(event.get("property_code"))
        else:
            event_code = normalize_identifier(event.get("property_id"))
        if event_code and event_code != selected_code:
            continue
        if not event_code:
            # event_log property_id historically stores the property code. If
            # a missing/unknown code cannot be resolved, don't attribute it.
            continue
        if lead_id not in first_intent or occurred_at < first_intent[lead_id]:
            first_intent[lead_id] = occurred_at
    for lead_id, occurred_at in first_intent.items():
        row = by_lead[lead_id]
        assigned = [coerce_utc_datetime(ep.get("assigned_at")) for ep in row["episodes"]]
        assigned = [value for value in assigned if value]
        first_assigned = min(assigned) if assigned else None
        row["visit_intent"] = True
        row["visit_intent_at"] = occurred_at.isoformat()
        row["visit_intent_before_assignment"] = bool(first_assigned and occurred_at < first_assigned)
    for row in by_lead.values():
        row["episodes"].sort(key=lambda ep: ep.get("assigned_at") or "")
        first_temperature = str((row["episodes"][0] if row["episodes"] else {}).get("temperature_at_assignment") or "").upper()
        row["temperature"] = "Hot" if first_temperature == "HOT" else "Normal" if first_temperature in {"NORMAL", "COLD"} else None
        row["property_code"] = selected_code
        row["first_episode"] = row["episodes"][0] if row["episodes"] else None
        row["first_received_at"] = row["episodes"][0].get("assigned_at") if row["episodes"] else None
    return sorted(by_lead.values(), key=lambda row: row.get("first_received_at") or "")


def _control_load_period_episodes(db, start_date, end_date, executive):
    cache_key = (start_date.isoformat(), end_date.isoformat(), str(executive or "").strip().casefold())
    now_monotonic = time.monotonic()
    with _CONTROL_PORTFOLIO_EPISODE_CACHE_LOCK:
        cache_entry = _CONTROL_PORTFOLIO_EPISODE_CACHE.get(cache_key)
    if cache_entry and cache_entry[0] > now_monotonic:
        return cache_entry[1]
    start_utc, end_utc = _build_chile_period_bounds(start_date.isoformat(), end_date.isoformat())
    cycles = list(db["crm_assignment_cycles"].find(
        {"assigned_at": {"$gte": start_utc, "$lt": end_utc},
         "assigned_to_display_name": executive,
         "assignment_cycle_id": {"$exists": True, "$nin": [None, ""]},
         "lead_id": {"$exists": True, "$ne": None}},
        {"lead_id": 1, "assignment_cycle_id": 1, "assigned_to_display_name": 1,
         "assigned_at": 1, "property_code": 1, "temperature_at_assignment": 1,
         "hot_started_at": 1, "temperature_transitioned_at": 1,
         "reassignment_reason": 1, "reassignment_meta": 1, "assigned_by": 1,
         "assigned_to_user_id": 1, "assignment_number": 1, "cycle_origin": 1,
         "closed_reason": 1, "reason": 1, "policy_repair_id": 1,
         "policy_repair_version": 1, "policy_repair_reason": 1,
         "policy_repair_previous_owner_user_id": 1, "policy_repair_selected_user_id": 1,
         "cycle_status": 1, "schema_version": 1},
    ))
    lead_ids = {cycle.get("lead_id") for cycle in cycles if cycle.get("lead_id") is not None}
    leads = list(db["leads"].find(
        {"_id": {"$in": list(lead_ids)}},
        {"_id": 1, "_test_lead": 1, "is_test": 1, "phone": 1, "created_at": 1,
         "name": 1, "nombre": 1, "nombre_cliente": 1, "client_name": 1,
         "lead_origin": 1, "origin": 1, "prospecto.nombre": 1,
         "prospecto.nombre_cliente": 1, "prospecto.contact_name": 1,
         "prospecto.origen": 1, "prospecto.canal_origen": 1,
         "prospecto.codigo": 1, "prospecto.codigo_mercadolibre": 1,
         "prospecto.codigo_yapo": 1, "prospecto.codigo_propiedad": 1,
         "prospecto.propiedad_codigo": 1, "prospecto.fuente_lead": 1,
         "prospecto.portal_origen": 1, "prospecto.origen_anuncio": 1,
         "prospecto.plataforma_origen": 1},
    )) if lead_ids else []
    cycle_ids = sorted({str(cycle.get("assignment_cycle_id")) for cycle in cycles if cycle.get("assignment_cycle_id")})
    audits = list(db["crm_sla_reassignment_audit_v1"].find(
        {"event_type": "SLA_REASSIGNMENT_COMMITTED",
         "$or": [{"new_cycle_id": {"$in": cycle_ids}}, {"destination_cycle_id": {"$in": cycle_ids}}]},
        {"lead_id": 1, "source_cycle_id": 1, "new_cycle_id": 1,
         "destination_cycle_id": 1, "event_type": 1, "reassigned_at": 1},
    )) if cycle_ids else []
    metrics = build_month_metrics(cycles=cycles, leads=leads, events=[], results=[], sla_audits=audits,
                                  executive=executive, as_of=datetime.now(timezone.utc))
    result = (cycles, leads, metrics)
    with _CONTROL_PORTFOLIO_EPISODE_CACHE_LOCK:
        if len(_CONTROL_PORTFOLIO_EPISODE_CACHE) >= 16:
            for key, entry in list(_CONTROL_PORTFOLIO_EPISODE_CACHE.items()):
                if entry[0] <= now_monotonic:
                    _CONTROL_PORTFOLIO_EPISODE_CACHE.pop(key, None)
            if len(_CONTROL_PORTFOLIO_EPISODE_CACHE) >= 16:
                _CONTROL_PORTFOLIO_EPISODE_CACHE.pop(next(iter(_CONTROL_PORTFOLIO_EPISODE_CACHE)))
        _CONTROL_PORTFOLIO_EPISODE_CACHE[cache_key] = (now_monotonic + 60, result)
    return result


def query_monthly_control_property_leads(
    period_start: Optional[str], period_end: Optional[str], executive: Optional[str], property_code: Optional[str],
) -> dict:
    """Lazily fetch customer rows for one property; never fetch messages here."""
    try:
        start_date = datetime.strptime(str(period_start), "%Y-%m-%d").date()
        end_date = datetime.strptime(str(period_end), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return {"availability": {"status": "unavailable", "reason": "Selecciona fechas válidas."}, "leads": []}
    selected = str(executive or "").strip()
    code = normalize_identifier(property_code)
    if not selected or not code or start_date < datetime(2026, 8, 1).date() or start_date > end_date:
        return {"availability": {"status": "unavailable", "reason": "La selección no tiene cobertura o está incompleta."}, "leads": []}
    db = get_db()
    cycles, leads, metrics = _control_load_period_episodes(db, start_date, end_date, selected)
    identity_leads = list(leads)
    identity_leads.extend({"prospecto": {"codigo": cycle.get("property_code")}} for cycle in cycles if cycle.get("property_code"))
    candidates = list(db["universo_cartera_prop360"].find(
        _control_property_candidate_query(identity_leads),
        {"codigo": 1, "publicaciones": 1, "disponible_prop360": 1},
    )) if identity_leads else []
    resolver = PropertyIdentityResolver(candidates)
    cycle_by_id = {str(cycle.get("assignment_cycle_id")): cycle for cycle in cycles}
    lead_by_id = {str(lead.get("_id")): lead for lead in leads}
    rows = _control_build_property_lead_details(metrics.get("records") or [], cycle_by_id, lead_by_id,
                                                resolver, code, [])
    if not rows:
        return {"availability": {"status": "available", "reason": None}, "property_code": code, "executive": selected, "leads": []}
    return {"availability": {"status": "available", "reason": None}, "property_code": code, "executive": selected, "leads": rows}


def query_monthly_control_lead_conversation(
    period_start: Optional[str], period_end: Optional[str], executive: Optional[str],
    property_code: Optional[str], lead_id: Optional[str], assignment_cycle_id: Optional[str],
) -> dict:
    """Return messages only after revalidating the selected user/period/episode."""
    try:
        start_date = datetime.strptime(str(period_start), "%Y-%m-%d").date()
        end_date = datetime.strptime(str(period_end), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return {"availability": {"status": "unavailable", "reason": "Selecciona fechas válidas."}}
    selected, lead_key, cycle_key = str(executive or "").strip(), str(lead_id or "").strip(), str(assignment_cycle_id or "").strip()
    code = normalize_identifier(property_code)
    if not selected or not lead_key or not cycle_key or not code or start_date > end_date or start_date < datetime(2026, 8, 1).date():
        return {"availability": {"status": "unavailable", "reason": "La selección no es válida."}}
    db = get_db()
    lead = db["leads"].find_one(
        {"_id": {"$in": mongo_id_variants(lead_key)}},
        {"_id": 1, "_test_lead": 1, "is_test": 1, "phone": 1, "lead_origin": 1,
         "origin": 1, "prospecto": 1, "conversation_id": 1, "messages": 1,
         "name": 1, "nombre": 1, "nombre_cliente": 1, "client_name": 1,
         "lifecycle.first_valid_management_at": 1},
    )
    if not lead or str(lead.get("_id") or "") != lead_key:
        return {"availability": {"status": "unavailable", "reason": "El episodio no pertenece a la cohorte seleccionada."}}
    lead_variants = mongo_id_variants(lead_key)
    cycles = list(db["crm_assignment_cycles"].find(
        {"lead_id": {"$in": lead_variants}},
        {"lead_id": 1, "assignment_cycle_id": 1, "assigned_to_display_name": 1,
         "assigned_at": 1, "property_code": 1, "temperature_at_assignment": 1,
         "first_valid_management_at": 1,
         "reassignment_reason": 1, "assigned_by": 1, "assignment_number": 1,
         "cycle_origin": 1, "closed_reason": 1, "reason": 1, "policy_repair_id": 1,
         "policy_repair_version": 1, "policy_repair_reason": 1,
         "policy_repair_previous_owner_user_id": 1, "policy_repair_selected_user_id": 1},
    ))
    start_utc, end_utc = _build_chile_period_bounds(start_date.isoformat(), end_date.isoformat())
    cycle = next((candidate for candidate in cycles
                  if str(candidate.get("assignment_cycle_id") or "") == cycle_key
                  and str(candidate.get("assigned_to_display_name") or "").casefold() == selected.casefold()
                  and (coerce_utc_datetime(candidate.get("assigned_at")) is not None)
                  and start_utc <= coerce_utc_datetime(candidate.get("assigned_at")) < end_utc
                  and not _exclusion_category(candidate, lead)), None)
    if not cycle:
        return {"availability": {"status": "unavailable", "reason": "El episodio no pertenece a la cohorte seleccionada."}}
    identity_leads = [lead, {"prospecto": {"codigo": cycle.get("property_code")}}]
    candidates = list(db["universo_cartera_prop360"].find(_control_property_candidate_query(identity_leads), {"codigo": 1, "publicaciones": 1}))
    resolver = PropertyIdentityResolver(candidates)
    actual_code, _ = _control_resolve_episode_property(cycle, lead, resolver)
    if actual_code != code:
        return {"availability": {"status": "unavailable", "reason": "La propiedad no coincide con el episodio verificado."}}
    assignment_history = []
    for candidate in cycles:
        if _exclusion_category(candidate, lead):
            continue
        candidate_code, _candidate_status = _control_resolve_episode_property(candidate, lead, resolver)
        if candidate_code != code:
            continue
        stamp = coerce_utc_datetime(candidate.get("assigned_at"))
        assignment_history.append({
            "assignment_cycle_id": str(candidate.get("assignment_cycle_id") or ""),
            "executive": str(candidate.get("assigned_to_display_name") or "Sin ejecutivo identificado"),
            "assigned_at": stamp.isoformat() if stamp else None,
            "assignment_reason": str(candidate.get("reassignment_reason") or candidate.get("reason") or candidate.get("cycle_origin") or "Sin motivo registrado"),
        })
    assignment_history.sort(key=lambda item: item.get("assigned_at") or "")
    intent_history = []
    for event in db["event_log"].find(
        {"lead_id": lead_key, "event": "visit_intent_detected"},
        {"_id": 0, "timestamp": 1, "property_code": 1, "property_id": 1},
    ):
        event_value = event.get("property_code") or event.get("property_id")
        event_resolution = resolver.resolve({"prospecto": {"codigo": event_value}}) if event_value else None
        if not event_resolution or event_resolution.resolved_property_code != code:
            continue
        event_stamp = coerce_utc_datetime(event.get("timestamp"))
        if event_stamp:
            intent_history.append(event_stamp.isoformat())
    intent_history = sorted(set(intent_history))
    raw_temperature = str(cycle.get("temperature_at_assignment") or "").strip().upper()
    temperature_label = "Hot" if raw_temperature == "HOT" else "Normal" if raw_temperature in {"NORMAL", "COLD"} else "Sin clasificar"
    assigned_at = coerce_utc_datetime(cycle.get("assigned_at"))
    episode = {"temperature": temperature_label, "assigned_at": assigned_at.isoformat() if assigned_at else None}
    main_conversation = str(lead.get("conversation_id") or "").strip()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for message in lead.get("messages") or []:
        if not isinstance(message, Mapping):
            continue
        message_lead = str(message.get("lead_id") or "").strip()
        if message_lead and message_lead != lead_key:
            continue
        role = str(message.get("actor_type") or "").strip().lower()
        if role not in {"customer", "bot", "human_agent", "system"}:
            legacy_role = str(message.get("role") or "").strip().lower()
            role = {"user": "customer", "assistant": "bot", "system": "system"}.get(legacy_role, "system")
        conv_id = str(message.get("conversation_id") or main_conversation).strip()
        if not conv_id:
            # The embedded message is still linked by the lead document ID.
            conv_id = "lead:" + lead_key
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        stamp = message.get("timestamp") or message.get("sent_at") or message.get("created_at")
        parsed_stamp = coerce_utc_datetime(stamp)
        grouped[conv_id].append({
            "actor_type": role,
            "actor_label": {"customer": "Cliente", "bot": "Chatbot", "human_agent": "Ejecutivo / agente", "system": "Sistema"}[role],
            "content": content,
            "timestamp": parsed_stamp.isoformat() if parsed_stamp else str(stamp or "Fecha no registrada"),
            "message_id": str(message.get("message_id") or "") or None,
            "lead_intent": str(message.get("lead_intent") or message.get("intencion") or "") or None,
        })
    conversations = []
    for conv_id, messages in grouped.items():
        messages.sort(key=lambda message: coerce_utc_datetime(message["timestamp"]) or datetime.min.replace(tzinfo=timezone.utc))
        conversations.append({"conversation_id": conv_id if not conv_id.startswith("lead:") else None, "messages": messages})
    conversations.sort(key=lambda item: item["messages"][0]["timestamp"] if item["messages"] else "")
    return {
        "availability": {"status": "available", "reason": None},
        "conversation_status": "available" if any(item["messages"] for item in conversations) else "unavailable",
        "conversation_reason": None if any(item["messages"] for item in conversations) else "No hay mensajes de conversación verificables para este lead.",
        "lead_id": lead_key, "property_code": code, "executive": selected,
        "lead_name": _control_lead_display_fields(lead).get("name"),
        "temperature_at_assignment": episode.get("temperature") or "Sin clasificar",
        "received_at": episode.get("assigned_at"),
        "first_management_at": coerce_utc_datetime(cycle.get("first_valid_management_at") or (lead.get("lifecycle") or {}).get("first_valid_management_at")).isoformat()
        if coerce_utc_datetime(cycle.get("first_valid_management_at") or (lead.get("lifecycle") or {}).get("first_valid_management_at")) else None,
        "assignment_history": assignment_history,
        "visit_intent_events": intent_history,
        "conversations": conversations,
    }
