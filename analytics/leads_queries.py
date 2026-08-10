"""Read-only MongoDB queries for the Leads Analytics Dashboard."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import math
from typing import Any, Mapping, Optional

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
from config import Config

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


def _format_date_field(field_expr: str, fmt: str = "%Y-%m-%d") -> dict:
    return {
        "$dateToString": {"format": fmt, "date": field_expr}
    }


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
                "precio_uf": (lead.get("prospecto", {}) or {}).get("precio_uf"),
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
    """Tendencia comparativa: periodo actual vs periodo anterior de igual duracion."""
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

    def _daily(ps_utc, pe_utc):
        pipeline = [
            _normalized_created_at_stage(),
            {"$match": _build_commercial_cohort_match(ps_utc, pe_utc, filters)},
            {"$group": {"_id": _format_date_field("$_created_normalized"), "received": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
            {"$project": {"date": "$_id", "received": 1, "_id": 0}},
        ]
        return list(db["leads"].aggregate(pipeline))

    current_daily = _daily(start_utc, end_utc)
    previous_daily = _daily(prev_start, prev_end) if include_comparison else []

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


def query_leads_dashboard_conversion(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    comparison_start: Optional[str] = None,
    comparison_end: Optional[str] = None,
    include_comparison: bool = True,
    filters: Optional[dict] = None,
) -> dict:
    """Resumen de conversión para el Leads Dashboard (CARD 2).

    Cuenta el total de leads de la cohorte y cuántos tienen pipeline_stage
    VISIT_SCHEDULED (citas/visitas agendadas), para el periodo actual y el
    periodo comparable anterior.
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

    def _metrics(ps_utc, pe_utc):
        pipeline = [
            _normalized_created_at_stage(),
            {"$match": _build_commercial_cohort_match(ps_utc, pe_utc, filters)},
            {"$facet": {
                "total": [{"$count": "c"}],
                "citas": [{"$match": {"pipeline_stage": "VISIT_SCHEDULED"}}, {"$count": "c"}],
            }},
        ]
        row = list(db["leads"].aggregate(pipeline))
        facet = row[0] if row else {}
        total = (facet.get("total") or [{}])[0].get("c", 0) if facet.get("total") else 0
        citas = (facet.get("citas") or [{}])[0].get("c", 0) if facet.get("citas") else 0
        return {"total": total, "citas": citas}

    current = _metrics(start_utc, end_utc)
    previous = _metrics(prev_start, prev_end) if include_comparison else {"total": 0, "citas": 0}

    return {
        "current": current,
        "previous": previous,
    }


def query_leads_dashboard_pipeline(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    filters: Optional[dict] = None,
) -> dict:
    """Valorización UF del pipeline para el Leads Dashboard (CARD 3).

    Universo: cohorte del período con propiedad vinculada (prospecto.codigo).
    Agrupa por propiedad única, usando el precio promedio por propiedad.
    """
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)

    pipeline = [
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(start_utc, end_utc, filters)},
        {"$addFields": {
            "_code": {"$ifNull": ["$prospecto.codigo", ""]},
            "_precio_uf": {"$ifNull": ["$prospecto.precio_uf", None]},
            "_op": {"$ifNull": ["$prospecto.operacion", "Sin informacion"]},
        }},
        {"$group": {
            "_id": "$_code",
            "count": {"$sum": 1},
            "precios": {"$push": "$_precio_uf"},
            "ops": {"$addToSet": "$_op"},
        }},
    ]
    raw = list(db["leads"].aggregate(pipeline))

    linked_leads = 0
    unique_properties = 0
    venta_uf = 0.0
    arriendo_uf = 0.0
    total_uf = 0.0
    priced = 0

    for r in raw:
        code = r["_id"]
        if not code:
            continue
        linked_leads += r.get("count", 0)
        unique_properties += 1

        prices = []
        for p in r.get("precios", []):
            if p is not None:
                try:
                    prices.append(float(p))
                except (ValueError, TypeError):
                    pass
        if not prices:
            continue
        price = round(sum(prices) / len(prices), 1)
        total_uf += price
        priced += 1

        ops = set(r.get("ops", []))
        if "Venta" in ops:
            op = "Venta"
        elif "Arriendo" in ops:
            op = "Arriendo"
        else:
            op = next((o for o in ops if o != "Sin informacion"), "Otro")
        if op == "Venta":
            venta_uf += price
        elif op == "Arriendo":
            arriendo_uf += price
        else:
            venta_uf += price  # operación desconocida se contabiliza como valor total

    return {
        "leads_vinculados": linked_leads,
        "propiedades_vinculadas": unique_properties,
        "propiedades_con_precio": priced,
        "monto_uf": round(total_uf, 1),
        "monto_venta_uf": round(venta_uf, 1),
        "monto_arriendo_uf": round(arriendo_uf, 1),
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


def _sla_bucket():
    return {
        "eligible": 0, "open_normal": 0, "attention": 0,
        "near_breach": 0, "breached": 0,
        "managed_within": 0, "managed_outside": 0,
        "median_minutes": None, "p90_minutes": None,
        "_managed_minutes": [],
    }


def build_sla_risk_payload(leads, *, now=None, cutover_at=None):
    """Build the dashboard SLA contract from canonical lead evidence only."""
    now = coerce_utc_datetime(now) or datetime.now(timezone.utc)
    cutover = coerce_utc_datetime(cutover_at)
    buckets = {"lead": _sla_bucket(), "lead_hot": _sla_bucket()}
    managed_durations = {"lead": [], "lead_hot": []}
    excluded = {"historical": 0, "not_assigned": 0, "insufficient_data": 0}

    for lead in leads:
        lifecycle = lead.get("lifecycle") or {}
        assigned = coerce_utc_datetime(lifecycle.get("assigned_at"))
        managed = coerce_utc_datetime(lifecycle.get("first_valid_management_at"))
        if not assigned:
            excluded["not_assigned"] += 1
            continue
        if cutover and is_pre_visual_cutover(assigned, cutover=cutover):
            excluded["historical"] += 1
            continue
        if managed and managed < assigned:
            excluded["insufficient_data"] += 1
            continue

        history = lead.get("temperature_history") or []
        has_hot_evidence = str(lead.get("lead_temperature_effective") or "").upper() == "HOT"
        has_hot_evidence = has_hot_evidence or any(
            str((entry or {}).get("value") or (entry or {}).get("temperature") or "").upper() == "HOT"
            for entry in history if isinstance(entry, Mapping)
        )
        hot_start = resolve_hot_start_at(
            assigned_at=assigned,
            temperature_history=history,
            effective_temperature=lead.get("lead_temperature_effective"),
        ) if has_hot_evidence else None
        # The SLA is closed at first valid management. A Hot event after that
        # point cannot reclassify the already-closed Lead cycle.
        profile = "lead_hot" if hot_start and (not managed or hot_start < managed) else "lead"
        if has_hot_evidence and not hot_start:
            excluded["insufficient_data"] += 1
            continue
        sla = calculate_sla(
            assigned_at=assigned,
            first_valid_management_at=managed,
            now=now,
            temperature="HOT" if profile == "lead_hot" else "COLD",
            hot_started_at=hot_start,
            require_hot_start=(profile == "lead_hot"),
        )
        if profile == "lead_hot" and hot_start and hot_start > assigned:
            # A Lead breach before Hot conversion remains an outside-SLA
            # result even if the later Hot segment itself was short.
            pre_hot = calculate_sla(
                assigned_at=assigned,
                first_valid_management_at=hot_start,
                now=now,
                temperature="COLD",
            )
            if pre_hot.get("canonical_state") == "MANAGED_OUTSIDE_SLA":
                sla["canonical_state"] = "MANAGED_OUTSIDE_SLA"
            elif pre_hot.get("canonical_state") == "BREACHED":
                sla["canonical_state"] = "BREACHED"
        if sla.get("canonical_state") in {"SLA_NOT_STARTED", "INSUFFICIENT_DATA"}:
            excluded["insufficient_data"] += 1
            continue

        bucket = buckets[profile]
        bucket["eligible"] += 1
        minutes = sla.get("hot_minutes") if profile == "lead_hot" else sla.get("minutes")
        if managed:
            bucket["managed_within" if sla["canonical_state"] == "MANAGED_WITHIN_SLA" else "managed_outside"] += 1
            if minutes is not None:
                managed_durations[profile].append(minutes)
        elif sla["canonical_state"] == "ACTIVE_NORMAL":
            bucket["open_normal"] += 1
        elif sla["canonical_state"] == "ATTENTION":
            bucket["attention"] += 1
        elif sla["canonical_state"] == "NEAR_BREACH":
            bucket["near_breach"] += 1
        elif sla["canonical_state"] == "BREACHED":
            bucket["breached"] += 1

    for bucket in buckets.values():
        profile = "lead" if bucket is buckets["lead"] else "lead_hot"
        bucket.pop("_managed_minutes", None)
        bucket["median_minutes"] = _sla_percentile(managed_durations[profile], 50)
        bucket["p90_minutes"] = _sla_percentile(managed_durations[profile], 90)
    overall_numerator = sum(bucket["managed_within"] for bucket in buckets.values())
    overall_denominator = overall_numerator + sum(
        bucket["managed_outside"] + bucket["breached"] for bucket in buckets.values()
    )
    overall_pct = _coverage_pct(overall_numerator, overall_denominator) if overall_denominator else None
    policy_cutover = cutover.isoformat() if cutover else None
    return {
        "policy_timezone": "America/Santiago",
        "business_hours": {"days": "lunes-viernes", "start": "09:00", "end": "19:00"},
        "policy_cutover_at": policy_cutover,
        "overall_compliance_pct": overall_pct,
        "overall_numerator": overall_numerator,
        "overall_denominator": overall_denominator,
        "lead": buckets["lead"], "lead_hot": buckets["lead_hot"],
        "excluded": excluded,
        "within_sla_pct": overall_pct,
        "critical_open": buckets["lead"]["breached"] + buckets["lead_hot"]["breached"],
        "no_management": sum(bucket["open_normal"] + bucket["attention"] + bucket["near_breach"] + bucket["breached"] for bucket in buckets.values()),
        "eligible_total": sum(bucket["eligible"] for bucket in buckets.values()),
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
    """Canonical SLA panel using business minutes and verified assignment evidence."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    pipeline = [
        _normalized_created_at_stage(),
        {"$match": _build_commercial_cohort_match(start_utc, end_utc, filters)},
        {"$project": {
            "_id": 1,
            "lifecycle.assigned_at": 1,
            "lifecycle.first_valid_management_at": 1,
            "lead_temperature_effective": 1,
            "temperature_history": 1,
        }},
    ]
    raw = list(db["leads"].aggregate(pipeline))
    return build_sla_risk_payload(
        raw,
        now=datetime.now(timezone.utc),
        cutover_at=getattr(Config, "CRM_SLA_VISUAL_CUTOVER_AT", None),
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
        for event in db["crm_events"].find({"lead_id": {"$in": ids}}):
            events_by_lead.setdefault(str(event.get("lead_id")), []).append(event)

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
            "_precio_uf": {"$ifNull": ["$prospecto.precio_uf", None]},
            "_precio_clp": {"$ifNull": ["$prospecto.precio_clp", None]},
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
            "_precio_uf": {"$ifNull": ["$prospecto.precio_uf", None]},
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
