"""Read-only MongoDB queries for the Leads Analytics Dashboard."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import logging
import math
import re
from typing import Any, Mapping, Optional

from pymongo.errors import NetworkTimeout

from chatbot.constants import CHILE_TZ
from chatbot.crm_metrics import (
    calculate_sla,
    coerce_utc_datetime,
    event_evidence,
    is_pre_visual_cutover,
    resolve_hot_start_at,
)
from chatbot.utils import calculate_business_minutes
from chatbot.storage import get_db
from chatbot.lead_temperature import (
    COMMERCIAL_ALERT_TYPES,
    HOT_INTENTS,
    HOT_STAGES,
)
from config import Config

logger = logging.getLogger(__name__)

ACTIVE_STAGES = ["ARCHIVED", "CLOSED_WON", "CLOSED_LOST"]
UNASSIGNED_VALUES = ["Sin Asignar", "No Asignado", None, ""]


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

    if not include_comparison:
        prev_start = prev_end = None
    elif comparison_start and comparison_end:
        prev_start, prev_end = _build_chile_period_bounds(comparison_start, comparison_end)
    else:
        duration = end_utc - start_utc
        prev_end = start_utc
        prev_start = prev_end - duration

    if include_comparison and prev_start is not None and prev_end is not None:
        combined_start = min(start_utc, prev_start)
        combined_end = max(end_utc, prev_end)
        facets = {
            "current": [
                {"$match": {"_created_normalized": {"$gte": start_utc, "$lt": end_utc}}},
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
        previous_daily = row.get("previous", [])
    else:
        pipeline = [
            _cohort_indexed_prefilter(start_utc, end_utc),
            _normalized_created_at_stage(),
            {"$match": _build_commercial_cohort_match(start_utc, end_utc, filters)},
            {"$group": {"_id": _format_date_field("$_created_normalized", timezone="America/Santiago"), "received": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
            {"$project": {"date": "$_id", "received": 1, "_id": 0}},
        ]
        current_daily = list(db["leads"].aggregate(pipeline))
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


def _scheduled_visit_lead_ids(cohort_leads: list, lead_ids: set, pe_utc) -> set:
    """Leads de la cohorte con evidencia canónica de visita agendada as-of.

    Definición EXACTA de CARD 2 (Conversión a Visita Agendada), reutilizada por
    el embudo para que ambos niveles reconcilien:
      - stage_history / lifecycle.visit_scheduled_at con timestamp válido;
      - crm_events.result == VISITA_AGENDADA con timestamp;
      - orden firmada con timeline accepted.server_timestamp (matching conservador).
    Regla temporal: lead.created_at <= evidencia < period_end (exclusivo).
    """
    from collections import defaultdict

    db = get_db()
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
    if lead_ids:
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
        for event in db["crm_events"].find(
            event_filter,
            {"lead_id": 1, "result": 1, "meta": 1, "timestamp": 1},
        ):
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
    signed_orders = list(db["visitas"].find(
        {"status": {"$in": list(CANONICAL_SIGNED_ORDER_STATUSES)}},
        {"visita_code": 1, "phone": 1, "property_code": 1, "timeline": 1, "created_at": 1},
    ))
    order_matches, _ambiguous = _match_signed_orders_to_leads(signed_orders, cohort_leads)
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


def _cohort_conversion_metrics(db, ps_utc, pe_utc, filters):
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
    pipeline = [
        _cohort_indexed_prefilter(ps_utc, pe_utc),
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(ps_utc, pe_utc, filters)},
        {"$facet": {
            "total": [{"$count": "c"}],
            "evaluable": [{"$match": _VISIT_TRACEABILITY_MATCH}, {"$count": "c"}],
        }},
        {"$project": {
            "total": {"$ifNull": [{"$arrayElemAt": ["$total.c", 0]}, 0]},
            "evaluable": {"$ifNull": [{"$arrayElemAt": ["$evaluable.c", 0]}, 0]},
        }},
    ]
    row = list(db["leads"].aggregate(pipeline))
    total = row[0].get("total", 0) if row else 0
    evaluable = row[0].get("evaluable", 0) if row else 0

    # Cargar los leads de la cohorte (proyección mínima) para las fuentes A/C.
    lead_pipe = [
        _cohort_indexed_prefilter(ps_utc, pe_utc),
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(ps_utc, pe_utc, filters)},
        {"$project": {
            "_id": 1, "phone": 1,
            "prospecto.codigo": 1,
            "created_at": 1,
            "pipeline_stage": 1, "stage": 1,
            "stage_history": 1,
            "lifecycle.visit_scheduled_at": 1,
        }},
    ]
    cohort_leads = list(db["leads"].aggregate(lead_pipe))
    lead_ids = {str(l["_id"]) for l in cohort_leads}

    scheduled = _scheduled_visit_lead_ids(cohort_leads, lead_ids, pe_utc)

    return {
        "total": total,
        "citas": len(scheduled),
        "evaluable": evaluable,
        "orders_ambiguous": len(_match_signed_orders_to_leads(
            list(db["visitas"].find(
                {"status": {"$in": list(CANONICAL_SIGNED_ORDER_STATUSES)}},
                {"visita_code": 1, "phone": 1, "property_code": 1, "timeline": 1, "created_at": 1},
            )), cohort_leads)[1]),
    }


def query_leads_dashboard_conversion(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    comparison_start: Optional[str] = None,
    comparison_end: Optional[str] = None,
    include_comparison: bool = True,
    filters: Optional[dict] = None,
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

    current = _cohort_conversion_metrics(db, start_utc, end_utc, filters)
    previous = _cohort_conversion_metrics(db, prev_start, prev_end, filters) if include_comparison else {
        "total": 0, "citas": 0, "evaluable": 0, "orders_ambiguous": 0,
    }

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
_TEST_PHONES = frozenset({"56900000000", "+56900000000", "0000000000"})


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

    def _load(ps_utc, pe_utc):
        return list(db["leads"].aggregate([
            _cohort_indexed_prefilter(ps_utc, pe_utc),
            _normalized_created_at_stage(),
            {"$match": _build_commercial_cohort_match(ps_utc, pe_utc, filters)},
            {"$project": {
                "_id": 1, "phone": 1, "prospecto": 1, "created_at": 1,
                "pipeline_stage": 1, "stage": 1, "stage_history": 1,
                "lifecycle.visit_scheduled_at": 1,
            }},
        ]))

    cohort = _load(start_utc, end_utc)
    lead_ids = {str(l["_id"]) for l in cohort}
    scheduled = _scheduled_visit_lead_ids(cohort, lead_ids, end_utc)

    per_origin: dict = {}
    for lead in cohort:
        name = _normalize_source_name(_resolve_origin(lead))
        entry = per_origin.setdefault(name, {"leads": 0, "visitas": 0})
        entry["leads"] += 1
        if str(lead["_id"]) in scheduled:
            entry["visitas"] += 1

    prev_counts: dict = {}
    if prev_start:
        for lead in _load(prev_start, prev_end):
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

    def _cohort(ps, pe):
        pipe = [
            _cohort_indexed_prefilter(ps, pe),
            _normalized_created_at_stage(),
            {"$match": _build_commercial_cohort_match(ps, pe, filters)},
            {"$project": {"_id": 1, "created_at": 1, "phone": 1, "prospecto.codigo": 1,
                          "pipeline_stage": 1, "stage": 1, "stage_history": 1,
                          "lifecycle.visit_scheduled_at": 1}},
        ]
        leads = list(db["leads"].aggregate(pipe))
        lead_ids = {str(l["_id"]) for l in leads}
        # ciclos de la cohorte
        cycles_by_lead: dict = {}
        for c in db["crm_assignment_cycles"].find({"lead_id": {"$in": [l["_id"] for l in leads]}}):
            cycles_by_lead.setdefault(str(c.get("lead_id")), []).append(c)
        # visitas CARD 2 as-of (misma definición aprobada)
        visitas = _scheduled_visit_lead_ids(leads, lead_ids, pe)
        # ejecutivo responsable as-of por lead
        lead_exec = {}
        for l in leads:
            lid = str(l["_id"])
            lead_exec[lid] = _executive_as_of(cycles_by_lead, lid, pe) or "Sin Asignar"
        return leads, lead_exec, visitas

    cur_leads, cur_exec, cur_visitas = _cohort(start_utc, end_utc)
    prev_leads, prev_exec, prev_visitas = _cohort(prev_start, prev_end) if prev_start else ([], {}, set())

    # Solo ejecutivos con rol agente y activos en la colección "usuarios"
    active = {
        str(u.get("nombre", "")).strip().lower()
        for u in db["usuarios"].find({"rol": "agente", "is_active": True}, {"nombre": 1})
    }
    # Excluir al administrador
    active.discard("pablo galleguillos")

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
    cohort = list(db["leads"].aggregate(lead_pipe))
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
        for event in db["crm_events"].find(
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
        ):
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
    visita_agendada = _scheduled_visit_lead_ids(cohort, ids, end_utc)

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
    active.discard("pablo galleguillos")
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
        elif exec_key in {"pablo galleguillos"} or (exec_key and exec_key not in known_users):
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


def query_leads_operational_dashboard(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    filters: Optional[dict] = None,
) -> dict:
    """Bandeja operativa del dashboard: SLA, asignación y prioridad de acción.

    Este universo es deliberadamente distinto del resumen ejecutivo: devuelve
    leads recibidos en el período con evidencia de gestión y asignación para
    ordenar el trabajo pendiente del equipo.
    """
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    filters = filters or {}
    # En la vista de equipo el período se define por la asignación al ejecutivo,
    # no por la creación del lead. Así no se atribuyen leads antiguos que no
    # fueron asignados durante el período seleccionado.
    match = _build_extra_filter(filters) or {}
    assigned_period_match = {"$expr": {"$and": [
        {"$gte": ["$_assigned_normalized", start_utc]},
        {"$lt": ["$_assigned_normalized", end_utc]},
    ]}}
    projection = {
        "phone": 1, "created_at": 1, "_created_normalized": 1,
        "prospecto.nombre": 1, "prospecto.codigo": 1,
        "prospecto.tipo": 1, "prospecto.operacion": 1,
        "prospecto.comuna": 1, "pipeline_stage": 1, "stage": 1,
        "ejecutivo_asignado": 1, "lead_temperature_effective": 1,
        "lifecycle.assigned_at": 1,
        "lifecycle.first_valid_management_at": 1,
        "stage_history": {"$slice": ["$stage_history", -1]},
    }
    docs = list(db["leads"].aggregate([
        _normalized_created_at_stage(),
        {"$set": {"_assigned_normalized": {"$convert": {
            "input": "$lifecycle.assigned_at",
            "to": "date",
            "onError": None,
            "onNull": None,
        }}}},
        {"$match": assigned_period_match},
        {"$match": match},
        {"$project": projection},
    ]))

    now = datetime.now(timezone.utc)
    rows = []
    by_exec = {}
    response_times = {"HOT": [], "NORMAL": []}
    age_buckets = {"0_60": 0, "61_180": 0, "181_360": 0, "361_1440": 0, "1440_plus": 0}
    counters = {"active_assigned": 0, "pending": 0, "no_first_management": 0, "overdue": 0, "near_due": 0, "unassigned": 0, "hot_overdue": 0, "normal_overdue": 0, "hot_near_due": 0, "hot_risk": 0, "sla_hot_pct": None, "sla_normal_pct": None, "carryover": 0}
    unassigned_values = {str(value).strip().lower() for value in UNASSIGNED_VALUES if value is not None}

    def is_unassigned(value):
        return value is None or str(value).strip().lower() in unassigned_values

    for doc in docs:
        lifecycle = doc.get("lifecycle") or {}
        created = coerce_utc_datetime(doc.get("_created_normalized") or doc.get("created_at"))
        assigned = coerce_utc_datetime(lifecycle.get("assigned_at"))
        managed = coerce_utc_datetime(lifecycle.get("first_valid_management_at"))
        executive = str(doc.get("ejecutivo_asignado") or "Sin asignar").strip()
        temperature = str(doc.get("lead_temperature_effective") or "NORMAL").upper()
        threshold = 60 if temperature == "HOT" else 180
        sla_start = assigned or created
        elapsed = round(calculate_business_minutes(
            sla_start.astimezone(CHILE_TZ), now.astimezone(CHILE_TZ)
        )) if sla_start else 0
        elapsed = max(0, elapsed)
        if elapsed <= 60:
            age_buckets["0_60"] += 1
        elif elapsed <= 180:
            age_buckets["61_180"] += 1
        elif elapsed <= 360:
            age_buckets["181_360"] += 1
        elif elapsed <= 1440:
            age_buckets["361_1440"] += 1
        else:
            age_buckets["1440_plus"] += 1

        if created and created < start_utc:
            counters["carryover"] += 1
        unassigned = is_unassigned(doc.get("ejecutivo_asignado"))
        if unassigned:
            priority, priority_label = "unassigned", "Sin asignar"
            counters["unassigned"] += 1
        elif managed:
            priority, priority_label = "managed", "Gestionado"
        elif elapsed >= threshold:
            priority, priority_label = "overdue", "SLA vencido"
            counters["overdue"] += 1
            counters["pending"] += 1
        elif elapsed / max(threshold, 1) >= 0.75:
            priority, priority_label = "near_due", "Hot próximo a vencer"
            counters["near_due"] += 1
            counters["pending"] += 1
        else:
            priority, priority_label = "pending", "Pendiente"
            counters["pending"] += 1

        if not unassigned:
            counters["active_assigned"] += 1
        if priority != "managed" and not unassigned:
            counters["no_first_management"] += 1
        if priority == "overdue":
            counters["hot_overdue" if temperature == "HOT" else "normal_overdue"] += 1
        if temperature == "HOT" and priority in {"overdue", "near_due"}:
            counters["hot_risk"] += 1
        if temperature == "HOT" and priority == "near_due":
            counters["hot_near_due"] += 1
        if filters.get("priority") and filters["priority"] != priority:
            continue
        if filters.get("assignment") == "assigned" and unassigned:
            continue
        if filters.get("assignment") == "unassigned" and not unassigned:
            continue
        if filters.get("search"):
            term = str(filters["search"]).strip().lower()
            name = str((doc.get("prospecto") or {}).get("nombre") or "").lower()
            phone = str(doc.get("phone") or "").lower()
            if term not in name and term not in phone:
                continue

        stage = doc.get("pipeline_stage") or doc.get("stage") or "Sin estado"
        history = doc.get("stage_history") or []
        last_action = "Sin gestión registrada"
        if history and isinstance(history[-1], Mapping):
            last_action = history[-1].get("to") or history[-1].get("notes") or "Cambio de estado"
        property_data = doc.get("prospecto") or {}
        if managed:
            measured = round(calculate_business_minutes(
                (assigned or created).astimezone(CHILE_TZ), managed.astimezone(CHILE_TZ)
            )) if (assigned or created) else 0
            sla_status = "COMPLIED" if measured <= threshold else "BREACHED"
            sla_delta = measured - threshold
        elif unassigned:
            sla_status = "UNASSIGNED"
            sla_delta = elapsed - threshold
        else:
            sla_status = "EXPIRED" if elapsed >= threshold else ("NEAR_SLA" if elapsed / max(threshold, 1) >= 0.75 else "WITHIN_SLA")
            sla_delta = elapsed - threshold
        row = {
            "id": str(doc.get("_id")),
            "priority": priority, "priority_label": priority_label,
            "sla_minutes": elapsed, "sla_limit_minutes": threshold,
            "temperature": temperature, "nombre": property_data.get("nombre") or "Sin nombre",
            "phone": doc.get("phone") or "", "property": property_data.get("codigo") or "Sin propiedad",
            "property_type": property_data.get("tipo") or "", "operation": property_data.get("operacion") or "",
            "stage": stage, "executive": executive, "last_action": str(last_action),
            "created_at": created.isoformat() if created else None,
            "sla_status": sla_status,
            "sla_delta_minutes": sla_delta,
            "sla_delta_label": ("Vencido +%s min" % abs(sla_delta)) if sla_delta >= 0 else ("%s min restantes" % abs(sla_delta)),
        }
        rows.append(row)
        bucket = by_exec.setdefault(executive, {"executive": executive, "assigned": 0, "pending": 0, "overdue": 0, "managed": 0, "hot_managed": 0, "hot_complied": 0, "normal_managed": 0, "normal_complied": 0, "critical": 0})
        bucket["assigned"] += 1
        if unassigned:
            continue
        if managed:
            bucket["managed"] += 1
            managed_elapsed = round(calculate_business_minutes(
                (assigned or created).astimezone(CHILE_TZ), managed.astimezone(CHILE_TZ)
            )) if (assigned or created) else 0
            metric = "hot" if temperature == "HOT" else "normal"
            bucket[metric + "_managed"] += 1
            if managed_elapsed <= threshold:
                bucket[metric + "_complied"] += 1
            response_times["HOT" if temperature == "HOT" else "NORMAL"].append(managed_elapsed)
        else:
            bucket["pending"] += 1
            if priority == "overdue":
                bucket["overdue"] += 1
        if priority in {"overdue", "near_due", "unassigned"}:
            bucket["critical"] += 1 if priority == "overdue" and temperature == "HOT" else 0

    order = {"overdue": 0, "near_due": 1, "unassigned": 2, "pending": 3, "managed": 4}
    rows.sort(key=lambda row: (
        0 if row["priority"] == "overdue" and row["temperature"] == "HOT" else order.get(row["priority"], 9),
        1 if row["temperature"] != "HOT" else 0,
        -max(row.get("sla_delta_minutes", 0), 0),
    ))
    for bucket in by_exec.values():
        denominator = bucket["managed"] + bucket["overdue"]
        bucket["sla_compliance_pct"] = round(bucket["managed"] / denominator * 100, 1) if denominator else None
        bucket["hot_sla_pct"] = round(bucket["hot_complied"] / bucket["hot_managed"] * 100, 1) if bucket["hot_managed"] else None
        bucket["normal_sla_pct"] = round(bucket["normal_complied"] / bucket["normal_managed"] * 100, 1) if bucket["normal_managed"] else None
    hot_managed = sum(row["hot_managed"] for row in by_exec.values())
    hot_complied = sum(row["hot_complied"] for row in by_exec.values())
    normal_managed = sum(row["normal_managed"] for row in by_exec.values())
    normal_complied = sum(row["normal_complied"] for row in by_exec.values())
    counters["sla_hot_pct"] = round(hot_complied / hot_managed * 100, 1) if hot_managed else None
    counters["sla_normal_pct"] = round(normal_complied / normal_managed * 100, 1) if normal_managed else None
    return {
        "counters": counters,
        "age_buckets": age_buckets,
        "response_times": response_times,
        "executives": sorted(by_exec.values(), key=lambda item: (-item["pending"], -item["assigned"], item["executive"])),
        "rows": rows[:200],
        "total_rows": len(rows),
        "updated_at": now.isoformat(),
    }


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
