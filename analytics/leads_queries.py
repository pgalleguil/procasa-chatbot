"""Read-only MongoDB queries for the Leads Analytics Dashboard."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from chatbot.constants import CHILE_TZ
from chatbot.storage import get_db

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
    end_local = CHILE_TZ.localize(datetime(pe.year, pe.month, pe.day, 0, 0, 0)) + td(days=1)
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


def query_field_coverage(
    executive: Optional[str] = None,
    universe: str = "current_active",
) -> dict:
    """Cobertura de campos sobre el universo seleccionado."""
    db = get_db()
    match_parts = []
    if universe != "received_in_period":
        match_parts.append(build_active_filter())
    user_filter = _build_user_filter(executive)
    if user_filter:
        match_parts.append(user_filter)
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
    """Filtros adicionales: stage, temperature, source."""
    conditions = {}
    if not filters:
        return conditions
    if filters.get("stage"):
        conditions["pipeline_stage"] = str(filters["stage"])
    if filters.get("temperature"):
        conditions["lead_temperature_effective"] = str(filters["temperature"])
    if filters.get("source"):
        conditions["prospecto.origen"] = str(filters["source"])
    return conditions


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
) -> list:
    """Rendimiento de fuentes en el periodo: volumen, %hot, %asignados, %avanzados, variacion."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)
    prev_end = start_utc
    prev_start = prev_end - (end_utc - start_utc)
    user_filter = _build_user_filter(executive)

    eff = _effective_stage_expr()

    def _per_source(start_dt, end_dt):
        pipeline = [
            _normalized_created_at_stage(),
            {"$match": {"$expr": {"$and": [{"$gte": ["$_created_normalized", start_dt]}, {"$lt": ["$_created_normalized", end_dt]}]}}},
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
    previous_rows = {r["_id"]: r for r in _per_source(prev_start, prev_end)}

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
) -> dict:
    """Tendencia comparativa: periodo actual vs periodo anterior de igual duracion."""
    db = get_db()
    start_utc, end_utc = _build_chile_period_bounds(period_start, period_end)

    duration = end_utc - start_utc
    prev_end = start_utc
    prev_start = prev_end - duration

    def _daily(ps_utc, pe_utc):
        pipeline = [
            _normalized_created_at_stage(),
            {"$match": {"$expr": {"$and": [{"$gte": ["$_created_normalized", ps_utc]}, {"$lt": ["$_created_normalized", pe_utc]}]}}},
            {"$group": {"_id": _format_date_field("$_created_normalized"), "received": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
            {"$project": {"date": "$_id", "received": 1, "_id": 0}},
        ]
        return list(db["leads"].aggregate(pipeline))

    current_daily = _daily(start_utc, end_utc)
    previous_daily = _daily(prev_start, prev_end)

    current_total = sum(d["received"] for d in current_daily)
    previous_total = sum(d["received"] for d in previous_daily)
    pct_var = round(
        ((current_total - previous_total) / previous_total * 100), 1
    ) if previous_total else 0

    current_avg = round(current_total / max(len(current_daily), 1), 1)
    previous_avg = round(previous_total / max(len(previous_daily), 1), 1)

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
