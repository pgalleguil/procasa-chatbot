"""Reglas centralizadas para la meta comercial del equipo de captación."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import logging
import os
import time as _perf_time
from typing import Iterable

import pytz

logger = logging.getLogger(__name__)

from config import Config
from captacion_workforce import (
    DEFAULT_TIMEZONE,
    applicable_target,
    compliance_status,
    get_active_captacion_team as get_explicit_captacion_team,
    preload_calendar_days,
    preload_user_exceptions,
)
from captacion_management import (
    ANOMALY_COLLECTION,
    DAILY_METRICS_COLLECTION,
    VALID_CREDIT_EVENT_TYPES,
    ensure_management_indexes,
    normalize_result,
    normalize_started_action,
    resolve_management_unit_outcomes,
    summarize_final_outcomes,
    summarize_grouped_outcomes,
)


CAPTACION_TIMEZONE = pytz.timezone("America/Santiago")
CAPTACION_DAILY_GOAL = 10
CAPTACION_WORKDAYS = (0, 1, 2, 3, 4)
CAPTACION_WEEKLY_GOAL = CAPTACION_DAILY_GOAL * len(CAPTACION_WORKDAYS)
CAPTACION_GOAL_COLLECTION = "captacion_management_events"
CAPTACION_GOAL_SNAPSHOT_COLLECTION = "captacion_goal_snapshots"
CAPTACION_PRIVILEGED_ROLES = {"admin", "supervisor", "jefatura"}

# Esta configuración queda centralizada para incorporar un calendario de
# feriados confiable más adelante sin dispersar reglas por el proyecto.
CAPTACION_HOLIDAYS: frozenset[str] = frozenset()
_INDEXES_READY = False
_history_count_cache = {}  # {key: (timestamp, value)}
_management_rows_cache = {}  # {(start_day, end_day): (timestamp, rows)}
_MANAGEMENT_ROWS_CACHE_TTL_SECONDS = 300
_GOAL_SNAPSHOT_VERSION = 1


def clear_captacion_management_rows_cache() -> None:
    """Invalida filas de ledger cacheadas después de una nueva gestión."""
    _management_rows_cache.clear()


def captacion_goal_snapshot_key(
    selected_executive=None,
    period_start=None,
    period_end=None,
    now=None,
    excluded_executives=None,
) -> str:
    """Construye una clave estable para el último resultado de metas válido."""
    local_now = _as_chile_datetime(now)
    current_period = not (period_start and period_end)
    if period_start and period_end:
        start = date.fromisoformat(str(period_start))
        end = date.fromisoformat(str(period_end))
    else:
        start = local_now.date() - timedelta(days=local_now.weekday())
        end = start + timedelta(days=6)
    scope = _name_key(selected_executive) or "_team"
    excluded = ",".join(sorted({
        _name_key(value)
        for value in (excluded_executives or ())
        if _name_key(value)
    })) or "_none"
    # El período actual cambia de significado al pasar la medianoche en Chile:
    # el mismo lunes-domingo debe recalcular su columna "hoy" cada día. Evitar
    # reutilizar el snapshot del día anterior, manteniendo estables los
    # snapshots de períodos históricos seleccionados explícitamente.
    day_scope = f":{local_now.date().isoformat()}" if current_period else ""
    return f"v{_GOAL_SNAPSHOT_VERSION}:{scope}:{start.isoformat()}:{end.isoformat()}:{excluded}{day_scope}"


def load_captacion_goal_snapshot(
    db,
    *,
    selected_executive=None,
    period_start=None,
    period_end=None,
    now=None,
    excluded_executives=None,
) -> dict | None:
    """Lee solo el snapshot exacto; `_id` evita una consulta histórica pesada."""
    key = captacion_goal_snapshot_key(
        selected_executive=selected_executive,
        period_start=period_start,
        period_end=period_end,
        now=now,
        excluded_executives=excluded_executives,
    )
    document = db[CAPTACION_GOAL_SNAPSHOT_COLLECTION].find_one(
        {"_id": key},
        {"data": 1, "snapshot_at": 1, "updated_at": 1, "version": 1},
    )
    if not document or document.get("version") != _GOAL_SNAPSHOT_VERSION:
        return None
    data = document.get("data")
    if not isinstance(data, dict):
        return None
    timestamp = document.get("snapshot_at") or document.get("updated_at")
    return {"key": key, "data": data, "timestamp": timestamp}


def save_captacion_goal_snapshot(
    db,
    data: dict,
    *,
    selected_executive=None,
    period_start=None,
    period_end=None,
    now=None,
    excluded_executives=None,
) -> None:
    """Guarda un snapshot pequeño y reemplazable, sin crear índices nuevos."""
    key = captacion_goal_snapshot_key(
        selected_executive=selected_executive,
        period_start=period_start,
        period_end=period_end,
        now=now,
        excluded_executives=excluded_executives,
    )
    snapshot_at = datetime.now(timezone.utc)
    db[CAPTACION_GOAL_SNAPSHOT_COLLECTION].replace_one(
        {"_id": key},
        {
            "_id": key,
            "version": _GOAL_SNAPSHOT_VERSION,
            "snapshot_at": snapshot_at,
            "updated_at": snapshot_at,
            "scope": _name_key(selected_executive) or "_team",
            "period_start": period_start,
            "period_end": period_end,
            "data": data,
        },
        upsert=True,
    )


def _get_history_event_count(db, start_local, end_local):
    cache_key = f"{start_local.isoformat()}_{end_local.isoformat()}"
    entry = _history_count_cache.get(cache_key)
    if entry and (_perf_time.time() - entry[0]) < 300:
        return entry[1]
    count = db[CAPTACION_GOAL_COLLECTION].count_documents({
        "occurred_at": {
            "$gte": start_local.astimezone(timezone.utc),
            "$lt": end_local.astimezone(timezone.utc),
        }
    })
    _history_count_cache[cache_key] = (_perf_time.time(), count)
    return count
LEDGER_CUTOVER_DATE = os.getenv("CAPTACION_LEDGER_CUTOVER_DATE", "2026-07-20")
LEGACY_DUAL_READ_UNTIL = os.getenv("CAPTACION_LEGACY_DUAL_READ_UNTIL", "2026-08-02")
LEGACY_CONFIRMED_RESULTS = {
    "no_answer", "busy", "invalid_number", "contacted", "callback_requested", "message_sent",
    "sin respuesta", "ocupado", "número inválido", "contactado", "solicita llamada posterior", "mensaje enviado",
}

DAY_LABELS = ("Lun", "Mar", "Mié", "Jue", "Vie")


DAY_NAMES = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")


def _clean(value) -> str:
    return str(value or "").strip()


def _name_key(value) -> str:
    return " ".join(_clean(value).casefold().split())


def _as_chile_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(CAPTACION_TIMEZONE)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = pytz.utc.localize(value)
    return value.astimezone(CAPTACION_TIMEZONE)


def is_captacion_workday(value: datetime) -> bool:
    local = _as_chile_datetime(value)
    return local.weekday() in CAPTACION_WORKDAYS and local.date().isoformat() not in CAPTACION_HOLIDAYS


def is_valid_captacion_action(action, channel, result=None, message=None) -> bool:
    """Compatibilidad: valida contra la lista central y exige resultado confirmado."""
    try:
        normalize_started_action(action, channel)
        normalize_result(result)
    except ValueError:
        return False
    return True


def can_manage_captacion(user_doc: dict, property_doc: dict) -> bool:
    role = _clean(user_doc.get("rol") or "agente").lower()
    if role in CAPTACION_PRIVILEGED_ROLES:
        return True

    gestion = property_doc.get("gestion") or {}
    user_id = _clean(user_doc.get("_id"))
    assigned_id = _clean(gestion.get("ejecutivo_id"))
    if assigned_id:
        return bool(user_id and user_id == assigned_id)

    # Compatibilidad transitoria para asignaciones legacy sin ID inmutable.
    user_email = _clean(user_doc.get("email")).casefold()
    assigned_email = _clean(gestion.get("ejecutivo_email")).casefold()
    if user_email and assigned_email:
        return user_email == assigned_email
    user_name = _name_key(user_doc.get("nombre") or user_doc.get("username"))
    assigned_name = _name_key(gestion.get("ejecutivo_asignado"))
    return bool(user_name and assigned_name and user_name == assigned_name)


def management_dedup_key(property_id, actor, occurred_at) -> str:
    local = _as_chile_datetime(occurred_at)
    return f"{_clean(property_id)}:{_name_key(actor)}:{local.date().isoformat()}"


def record_valid_captacion_management(
    db,
    *,
    property_id,
    actor,
    action,
    channel,
    occurred_at=None,
    result=None,
    message=None,
    actor_id=None,
    actor_email=None,
) -> bool:
    """Inserta el crédito diario una sola vez por propiedad y ejecutivo."""
    # Ruta legacy clausurada: los crÃ©ditos nuevos se escriben exclusivamente
    # desde captacion_management con user_id, evento versionado y confirmaciÃ³n.
    return False


def ensure_captacion_goal_indexes(db) -> None:
    global _INDEXES_READY
    if _INDEXES_READY:
        return
    ensure_management_indexes(db)
    _INDEXES_READY = True


def _iter_historical_activity_rows(db, start_local: datetime, end_local: datetime):
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    query = {"gestion.actividades.timestamp": {"$gte": start_utc, "$lt": end_utc}}
    projection = {"gestion.actividades": 1}
    for prop in Config.get_captacion_collection(db).find(query, projection):
        property_id = _clean(prop.get("_id"))
        for activity in (prop.get("gestion") or {}).get("actividades") or []:
            timestamp = activity.get("timestamp")
            if not timestamp:
                continue
            local = _as_chile_datetime(timestamp)
            if not (start_local <= local < end_local):
                continue
            result_value = _clean(activity.get("result")).casefold()
            if result_value not in LEGACY_CONFIRMED_RESULTS:
                continue
            try:
                result_value = normalize_result(result_value)
            except ValueError:
                continue
            yield {
                "property_id": property_id,
                "actor": activity.get("user"),
                "actor_user_id": _clean(activity.get("user_id")),
                "occurred_at": local,
                "local_date": local.date().isoformat(),
                "result": result_value,
                "event_type": "management_confirmed",
                "credited": True,
                "commercially_valid": True,
                "legacy_inferred": True,
            }


def get_captacion_management_rows(db, now=None, period_start=None, period_end=None) -> list[dict]:
    local_now = _as_chile_datetime(now)
    if period_start and period_end:
        start_day = date.fromisoformat(str(period_start))
        end_day = date.fromisoformat(str(period_end))
        if end_day < start_day:
            raise ValueError("El período de captación es inválido")
        start_local = CAPTACION_TIMEZONE.localize(datetime.combine(start_day, time.min))
    else:
        monday = local_now.date() - timedelta(days=local_now.weekday())
        start_day = monday
        end_day = monday + timedelta(days=6)
        start_local = CAPTACION_TIMEZONE.localize(datetime.combine(monday, time.min))
    end_local = start_local + timedelta(days=7)
    if period_start and period_end:
        end_local = CAPTACION_TIMEZONE.localize(datetime.combine(end_day + timedelta(days=1), time.min))
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    # Una semana ya cargada cubre cualquier filtro diario de esa misma semana.
    # Esto evita volver a consultar el ledger completo al cambiar entre la
    # vista semanal y un día, sin alterar qué eventos son válidos.
    cache_now = _perf_time.time()
    cache_key = (start_day.isoformat(), end_day.isoformat())
    cached = _management_rows_cache.get(cache_key)
    if cached and cache_now - cached[0] < _MANAGEMENT_ROWS_CACHE_TTL_SECONDS:
        return list(cached[1])
    for (cached_start, cached_end), (cached_at, cached_rows) in list(_management_rows_cache.items()):
        if cache_now - cached_at >= _MANAGEMENT_ROWS_CACHE_TTL_SECONDS:
            _management_rows_cache.pop((cached_start, cached_end), None)
            continue
        if cached_start <= start_day.isoformat() and cached_end >= end_day.isoformat():
            return [
                row for row in cached_rows
                if start_day <= _as_chile_datetime(row.get("occurred_at")).date() <= end_day
            ]

    rows = []
    ledger_query = {
        "occurred_at": {"$gte": start_utc, "$lt": end_utc},
        "event_type": {"$in": list(VALID_CREDIT_EVENT_TYPES)},
        "$or": [{"credited": True}, {"commercially_valid": True}],
    }
    ledger = list(db[CAPTACION_GOAL_COLLECTION].find(
        ledger_query,
        {
            "event_id": 1,
            "event_type": 1,
            "property_id": 1,
            "actor": 1,
            "actor_user_id": 1,
            "occurred_at": 1,
            "local_date": 1,
            "result": 1,
            "credited": 1,
            "commercially_valid": 1,
            "contact_attempt": 1,
            "contact_effective": 1,
        },
    ))
    event_ids = [event.get("event_id") for event in ledger if event.get("event_id")]
    reversed_ids = {
        row.get("original_event_id")
        for row in db[CAPTACION_GOAL_COLLECTION].find(
            {"event_type": "management_reversed", "original_event_id": {"$in": event_ids}},
            {"original_event_id": 1},
        )
    }
    for event in ledger:
        if event.get("event_id") in reversed_ids:
            continue
        rows.append(
            {
                "property_id": _clean(event.get("property_id")),
                "actor": event.get("actor"),
                "actor_user_id": _clean(event.get("actor_user_id")),
                "occurred_at": _as_chile_datetime(event.get("occurred_at")),
                "local_date": event.get("local_date") or _as_chile_datetime(event.get("occurred_at")).date().isoformat(),
                "event_id": _clean(event.get("event_id")),
                "event_type": event.get("event_type"),
                "result": event.get("result"),
                "credited": bool(event.get("credited")),
                "commercially_valid": bool(event.get("commercially_valid", event.get("credited"))),
                "contact_attempt": bool(event.get("contact_attempt")),
                "contact_effective": bool(event.get("contact_effective")),
            }
        )
    cutover = date.fromisoformat(LEDGER_CUTOVER_DATE)
    dual_read_until = date.fromisoformat(LEGACY_DUAL_READ_UNTIL)
    if local_now.date() <= dual_read_until or (period_start and start_day <= dual_read_until):
        legacy_end = min(end_local, CAPTACION_TIMEZONE.localize(datetime.combine(cutover, time.min)))
        if start_local < legacy_end:
            rows.extend(_iter_historical_activity_rows(db, start_local, legacy_end))
    _management_rows_cache[cache_key] = (cache_now, list(rows))
    if len(_management_rows_cache) > 32:
        oldest_key = min(_management_rows_cache, key=lambda key: _management_rows_cache[key][0])
        _management_rows_cache.pop(oldest_key, None)
    return rows


def build_captacion_goal_dashboard(
    team: Iterable[dict],
    rows: Iterable[dict],
    selected_executive=None,
    now=None,
    period_start=None,
    period_end=None,
) -> dict:
    local_now = _as_chile_datetime(now)
    period_mode = bool(period_start and period_end)
    if period_mode:
        start_day = date.fromisoformat(str(period_start))
        end_day = date.fromisoformat(str(period_end))
        if end_day < start_day:
            raise ValueError("El período de captación es inválido")
        period_days = [start_day + timedelta(days=index) for index in range((end_day - start_day).days + 1)]
        weekdays = [day for day in period_days if day.weekday() in CAPTACION_WORKDAYS]
        monday = start_day
    else:
        monday = local_now.date() - timedelta(days=local_now.weekday())
        weekdays = [monday + timedelta(days=index) for index in CAPTACION_WORKDAYS]
        period_days = [monday + timedelta(days=index) for index in range(7)]
    today = local_now.date()

    members = [member for member in team if _clean(member.get("name"))]
    names = {_name_key(member["name"]): member["name"] for member in members}
    members_by_name = {_name_key(member["name"]): member for member in members}
    selected_key = _name_key(selected_executive)
    if selected_key and selected_key not in names:
        names[selected_key] = _clean(selected_executive)

    counts = defaultdict(lambda: defaultdict(set))
    last_activity = {}
    period_last_activity = {}
    weekend_activity = defaultdict(set)
    for row in rows:
        actor_key = _clean(row.get("actor_user_id")) or _name_key(row.get("actor"))
        property_id = _clean(row.get("property_id"))
        if not actor_key or not property_id:
            continue
        local = _as_chile_datetime(row.get("occurred_at"))
        dedup = f"{actor_key}:{property_id}:{local.date().isoformat()}"
        if row.get("credited", True) and local.date() in period_days:
            counts[actor_key][local.date()].add(dedup)
        if row.get("credited", True) and local.date() in period_days and local.date().weekday() not in CAPTACION_WORKDAYS:
            weekend_activity[actor_key].add(dedup)
        if actor_key not in last_activity or local > last_activity[actor_key]:
            last_activity[actor_key] = local
        if local.date() in period_days and (
            actor_key not in period_last_activity or local > period_last_activity[actor_key]
        ):
            period_last_activity[actor_key] = local

    elapsed_workdays = sum(1 for day in weekdays if day <= today)

    def member_metrics(name_key, display_name):
        member = members_by_name.get(name_key, {})
        identity_key = _clean(member.get("id")) or name_key
        day_targets = member.get("day_targets") or {}
        daily = []
        week_total = 0
        days_met = 0
        days_goal = 0
        week_goal = 0
        # La producción real se observa en todos los días del período. La
        # política de meta, en cambio, se aplica únicamente a los días hábiles
        # y queda resuelta por `target_info` más abajo.
        selected_days = period_days
        for index, day in enumerate(selected_days):
            count = len(counts[identity_key][day]) or len(counts[name_key][day])
            target_info = day_targets.get(day.isoformat()) or ({
                "target": CAPTACION_DAILY_GOAL,
                "exempt": False,
                "reason": None,
                "close_hour": 19,
            } if member else {
                "target": 0,
                "exempt": True,
                "reason": "Sin membresía activa",
                "close_hour": 19,
            })
            if day.weekday() not in CAPTACION_WORKDAYS:
                target_info = {
                    **target_info,
                    "target": 0,
                    "exempt": True,
                    "reason": None,
                }
            target = int(target_info.get("target") or 0)
            week_total += count
            week_goal += target
            days_goal += int(target > 0)
            met = bool(target > 0 and count >= target)
            days_met += int(met)
            daily.append(
                {
                    "label": DAY_NAMES[day.weekday()][:3],
                    "date": day.isoformat(),
                    "count": count,
                    "target": target,
                    "met": met,
                    "future": day > today,
                    "today": day == today,
                    "exempt": bool(target_info.get("exempt")),
                    "reason": target_info.get("reason"),
                    "status": compliance_status(
                        count=count,
                        target=target,
                        local_day=day,
                        now=local_now,
                        close_hour=int(target_info.get("close_hour") or 19),
                        exempt=bool(target_info.get("exempt")),
                    ),
                }
            )
        today_info = next((item for item in daily if item["date"] == today.isoformat()), None)
        today_count = today_info["count"] if today_info else 0
        today_target = today_info["target"] if today_info else 0
        is_workday = bool(today_info and today_target > 0)
        today_status = today_info["status"] if today_info else "SIN_META"
        today_reason = (today_info.get("reason") or DAY_NAMES[today.weekday()]) if today_info else DAY_NAMES[today.weekday()]
        return {
            "user_id": _clean(member.get("id")),
            "name": display_name,
            "today_count": today_count,
            "today_goal": today_target,
            "today_percent": round(today_count * 100 / today_target, 1) if today_target else None,
            "today_remaining": max(0, today_target - today_count) if today_target else 0,
            "met_today": bool(is_workday and today_count >= today_target),
            "is_workday": is_workday,
            "today_status": today_status,
            "today_reason": today_reason,
            "week_count": week_total,
            "week_goal": week_goal,
            "week_percent": round(week_total * 100 / week_goal, 1) if week_goal else 0,
            "days_met": days_met,
            "days_goal": days_goal,
            "expected_to_date": sum(item["target"] for item in daily if date.fromisoformat(item["date"]) <= today),
            "daily": daily,
            "weekend_activity": len(weekend_activity[identity_key]) or len(weekend_activity[name_key]),
            "last_activity": last_activity.get(identity_key) or last_activity.get(name_key),
            "period_last_activity": period_last_activity.get(identity_key) or period_last_activity.get(name_key),
            "contact_attempts": sum(
                int(metric.get("contact_attempts") or 0) for metric in (member.get("daily_metrics") or {}).values()
            ),
            "effective_contacts": sum(
                int(metric.get("effective_contacts") or 0) for metric in (member.get("daily_metrics") or {}).values()
            ),
            "captures": sum(
                int(metric.get("captures") or 0) for metric in (member.get("daily_metrics") or {}).values()
            ),
            "anomaly_count": int(member.get("anomaly_count") or 0),
        }

    period_rows = [
        row for row in rows
        if _as_chile_datetime(row.get("occurred_at")).date() in period_days
    ]
    # El tablero de equipo cuenta exclusivamente actividades de integrantes
    # activos. Los resultados deben usar exactamente la misma poblaci?n: una
    # actividad acreditada de una identidad ajena al equipo no puede inflar el
    # reporte semanal ni romper su paridad.
    active_member_ids = {_clean(member.get("id")) for member in members if _clean(member.get("id"))}
    active_member_names = set(names)
    team_period_rows = [
        row for row in period_rows
        if (_clean(row.get("actor_user_id")) in active_member_ids)
        or (_name_key(row.get("actor")) in active_member_names)
    ]
    outcome_summary = summarize_grouped_outcomes(team_period_rows)

    if selected_key:
        metrics = member_metrics(selected_key, names[selected_key])
        identity_key = metrics.get("user_id") or selected_key
        individual_rows = [
            row for row in period_rows
            if (_clean(row.get("actor_user_id")) or _name_key(row.get("actor"))) in {identity_key, selected_key}
        ]
        return {
            "mode": "individual",
            "timezone": "America/Santiago",
            "period_start": min(item["date"] for item in metrics.get("daily") or []) if metrics.get("daily") else None,
            "period_end": max(item["date"] for item in metrics.get("daily") or []) if metrics.get("daily") else None,
            "period_days": len(metrics.get("daily") or []),
            "period_selected": period_mode,
            "includes_today": bool(next((item for item in metrics.get("daily") or [] if item["date"] == today.isoformat()), None)),
            "final_outcomes": summarize_final_outcomes(individual_rows),
            **{key: value for key, value in summarize_grouped_outcomes(individual_rows).items() if key != "units"},
            **metrics,
        }

    team_rows = [member_metrics(key, display) for key, display in names.items()]
    if period_mode:
        # Una semana histórica se evalúa por el cierre semanal, no por el
        # estado operativo de hoy.
        team_rows.sort(key=lambda row: (-row["week_percent"], _name_key(row["name"])))
    else:
        # En la semana actual primero aparecen quienes más necesitan
        # intervención: brecha más negativa, luego alertas y finalmente nombre.
        team_rows.sort(
            key=lambda row: (
                row["week_count"] - row["expected_to_date"],
                -row["anomaly_count"],
                _name_key(row["name"]),
            )
        )
    member_count = len(team_rows)
    return {
        "mode": "team",
        "timezone": "America/Santiago",
        "period_start": period_days[0].isoformat() if period_days else None,
        "period_end": period_days[-1].isoformat() if period_days else None,
        "period_days": len(period_days),
        "period_selected": period_mode,
        "includes_today": today.isoformat() in {day.isoformat() for day in period_days},
        "member_count": member_count,
        "today_count": sum(row["today_count"] for row in team_rows),
        "today_goal": sum(row["today_goal"] for row in team_rows),
        "executives_met_today": sum(1 for row in team_rows if row["met_today"]),
        "executives_met_week": sum(
            1 for row in team_rows
            if row["week_goal"] > 0 and row["week_count"] >= row["week_goal"]
        ),
        "executives_pending_today": sum(
            1 for row in team_rows if row["today_status"] in {"EN_PROGRESO", "INCUMPLIDO"}
        ),
        "week_count": sum(row["week_count"] for row in team_rows),
        "week_goal": sum(row["week_goal"] for row in team_rows),
        "days_person_met": sum(row["days_met"] for row in team_rows),
        "days_person_goal": sum(row["days_goal"] for row in team_rows),
        "expected_to_date": sum(row["expected_to_date"] for row in team_rows),
        "weekend_activity": sum(row["weekend_activity"] for row in team_rows),
        "contact_attempts": sum(row["contact_attempts"] for row in team_rows),
        "effective_contacts": sum(row["effective_contacts"] for row in team_rows),
        "captures": sum(row["captures"] for row in team_rows),
        "final_outcomes": outcome_summary["detailed_outcomes"],
        **{key: value for key, value in outcome_summary.items() if key not in {"units", "detailed_outcomes"}},
        "anomaly_count": sum(row["anomaly_count"] for row in team_rows),
        "executives": team_rows,
    }


def _capture_normalized_status(value) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    return " ".join("".join(char for char in text if not unicodedata.combining(char)).split())


def _capture_safe_local_datetime(value):
    if value in (None, ""):
        return None
    try:
        return _as_chile_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _capture_median(values):
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 1)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 1)


def _capture_load_current_properties(db, team: Iterable[dict]) -> tuple[list[dict], int]:
    """Carga una sola vez la población visible para el control de captación."""
    team_ids = {_clean(member.get("id")) for member in team if _clean(member.get("id"))}
    if not team_ids:
        return [], 0
    base_query = {
        "origen": {"$in": ["toctoc", "yapo"]},
        "classification.state": {"$in": ["DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"]},
    }
    assignment_query = {**base_query, "gestion.ejecutivo_id": {"$in": list(team_ids)}}
    unassigned_query = {
        **base_query,
        "$or": [
            {"gestion.ejecutivo_id": {"$exists": False}},
            {"gestion.ejecutivo_id": None},
            {"gestion.ejecutivo_id": ""},
        ],
    }
    projection = {
        "_id": 1,
        "title": 1,
        "titulo": 1,
        "details.titulo": 1,
        "comuna": 1,
        "region": 1,
        "operacion": 1,
        "tipo_propiedad": 1,
        "origen": 1,
        "first_seen": 1,
        "fecha_publicacion": 1,
        "gestion.ejecutivo_id": 1,
        "gestion.ejecutivo_asignado": 1,
        "gestion.estado_captacion": 1,
        "gestion.estado": 1,
        "gestion.fecha_asignacion": 1,
        "gestion.first_valid_action_at": 1,
        "gestion.fecha_ultima_gestion": 1,
        "gestion.last_contact": 1,
        "gestion.next_followup": 1,
    }
    properties = []
    collection = Config.get_captacion_collection(db)
    documents = list(collection.find(assignment_query, projection).hint("idx_captacion_asignacion"))
    # El stock sin responsable solo alimenta la alerta de supervisión. Se
    # cuenta exactamente, pero se cargan como máximo los primeros casos para
    # no traer miles de documentos que no son necesarios para el panel.
    unassigned_count = collection.count_documents(unassigned_query, hint="idx_captacion_clasificacion")
    documents.extend(collection.find(unassigned_query, projection).hint("idx_captacion_clasificacion").limit(20))
    for doc in documents:
        gestion = doc.get("gestion") or {}
        status = gestion.get("estado_captacion") or gestion.get("estado") or "NUEVO"
        title = doc.get("title") or doc.get("titulo") or (doc.get("details") or {}).get("titulo") or "Sin título"
        properties.append({
            "id": _clean(doc.get("_id")),
            "title": str(title),
            "comuna": str(doc.get("comuna") or "Sin comuna"),
            "region": str(doc.get("region") or ""),
            "operation": str(doc.get("operacion") or ""),
            "property_type": str(doc.get("tipo_propiedad") or ""),
            "origin": str(doc.get("origen") or "Sin origen"),
            "executive_id": _clean(gestion.get("ejecutivo_id")),
            "executive_name": str(gestion.get("ejecutivo_asignado") or ""),
            "status": str(status),
            "assigned_at": _capture_safe_local_datetime(gestion.get("fecha_asignacion")),
            "first_action_at": _capture_safe_local_datetime(gestion.get("first_valid_action_at")),
            "last_management_at": _capture_safe_local_datetime(gestion.get("fecha_ultima_gestion") or gestion.get("last_contact")),
            "next_followup": _capture_safe_local_datetime(gestion.get("next_followup")),
        })
    return properties, unassigned_count


def _capture_current_assignment_counts(properties: Iterable[dict], team: Iterable[dict]) -> dict[str, int]:
    team_ids = {_clean(member.get("id")) for member in team if _clean(member.get("id"))}
    assigned = defaultdict(set)
    for prop in properties:
        owner_id = _clean(prop.get("executive_id"))
        if owner_id in team_ids and prop.get("id"):
            assigned[owner_id].add(prop["id"])
    return {user_id: len(property_ids) for user_id, property_ids in assigned.items()}


def _capture_event_key(row: dict) -> str:
    event_id = _clean(row.get("event_id"))
    if event_id:
        return event_id
    return ":".join(
        [_clean(row.get("property_id")), _clean(row.get("actor_user_id") or row.get("actor")), _clean(row.get("occurred_at")), _clean(row.get("result"))]
    )


def _capture_pct(numerator, denominator):
    return round(float(numerator or 0) * 100 / denominator, 1) if denominator else None


def _capture_control_payload(db, result: dict, team: list[dict], rows: list[dict], anomalies: list[dict], current_properties: list[dict], unassigned_count: int = 0, now=None) -> dict:
    """Construye el contrato de control sin consultas por propiedad."""
    local_now = _as_chile_datetime(now)
    team_by_id = {_clean(member.get("id")): member for member in team if _clean(member.get("id"))}
    name_by_id = {key: member.get("name") or key for key, member in team_by_id.items()}
    team_ids = set(team_by_id)
    scoped_actor_ids = team_ids
    if result.get("mode") == "individual" and _clean(result.get("user_id")):
        scoped_actor_ids = {_clean(result.get("user_id"))}
    property_by_id = {prop["id"]: prop for prop in current_properties if prop.get("id")}
    assigned_by_actor = defaultdict(set)
    unassigned_properties = []
    for prop in current_properties:
        owner_id = _clean(prop.get("executive_id"))
        if owner_id in team_ids:
            assigned_by_actor[owner_id].add(prop["id"])
        elif not owner_id:
            unassigned_properties.append(prop)

    period_rows = [row for row in rows if row.get("actor_user_id") in team_ids]
    credited_rows = [row for row in period_rows if row.get("credited", True)]
    managed_unique_period = { _clean(row.get("property_id")) for row in credited_rows if _clean(row.get("property_id")) }
    managed_current_by_actor = defaultdict(set)
    attempts_by_actor = defaultdict(set)
    effective_by_actor = defaultdict(set)
    captures_by_actor = defaultdict(set)
    source_metrics = defaultdict(lambda: {"assigned": set(), "managed": set(), "attempts": set(), "effective": set(), "no_answer": set(), "invalid_number": set(), "brokers": set(), "captures": set()})
    for prop in current_properties:
        owner_id = _clean(prop.get("executive_id"))
        if owner_id in scoped_actor_ids:
            source_metrics[prop.get("origin") or "Sin origen"]["assigned"].add(prop["id"])

    for row in period_rows:
        property_id = _clean(row.get("property_id"))
        actor_id = _clean(row.get("actor_user_id"))
        if not property_id or not actor_id:
            continue
        event_key = _capture_event_key(row)
        prop = property_by_id.get(property_id)
        current_owner = _clean((prop or {}).get("executive_id"))
        if row.get("credited", True) and current_owner == actor_id:
            managed_current_by_actor[actor_id].add(property_id)
        if row.get("contact_attempt"):
            attempts_by_actor[actor_id].add(event_key)
        if row.get("contact_effective"):
            effective_by_actor[actor_id].add(event_key)
        if row.get("event_type") == "capture_confirmed" or _capture_normalized_status(row.get("result")) == "captured":
            captures_by_actor[actor_id].add(property_id)
        if prop and current_owner == actor_id:
            source = prop.get("origin") or "Sin origen"
            metrics = source_metrics[source]
            if row.get("credited", True):
                metrics["managed"].add(property_id)
            if row.get("contact_attempt"):
                metrics["attempts"].add(event_key)
            if row.get("contact_effective"):
                metrics["effective"].add(event_key)
            result_key = _capture_normalized_status(row.get("result"))
            if result_key in {"no_answer", "busy"}:
                metrics["no_answer"].add(event_key)
            if result_key == "invalid_number":
                metrics["invalid_number"].add(event_key)
            if result_key == "broker_identified":
                metrics["brokers"].add(property_id)
            if row.get("event_type") == "capture_confirmed" or result_key == "captured":
                metrics["captures"].add(property_id)

    followup_by_actor = defaultdict(lambda: {"today": 0, "overdue": 0, "upcoming": 0})
    backlog_by_actor = defaultdict(list)
    aging_keys = ("d_0_1", "d_2_3", "d_4_5", "d_6_10", "d_11_20", "d_21_30", "d_31_60", "gt_60", "missing_date")
    aging_labels = {"d_0_1": "0–1 día", "d_2_3": "2–3 días", "d_4_5": "4–5 días", "d_6_10": "6–10 días", "d_11_20": "11–20 días", "d_21_30": "21–30 días", "d_31_60": "31–60 días", "gt_60": "> 60 días", "missing_date": "Sin fecha de asignación"}
    aging = {key: 0 for key in aging_keys}
    aging_by_actor = defaultdict(lambda: {key: 0 for key in aging_keys})
    overdue_followups = []
    followup_today = []
    upcoming_followups = []
    closed_states = {"captado", "corredor", "descartado", "propiedad no disponible", "publicacion expirada", "no interesado", "telefono invalido"}
    pending_states = {"", "nuevo", "detectado", "por contactar"}
    for prop in current_properties:
        owner_id = _clean(prop.get("executive_id"))
        is_open = _capture_normalized_status(prop.get("status")) not in closed_states
        followup = prop.get("next_followup")
        if is_open and followup:
            item = {"property": prop, "executive_id": owner_id, "followup": followup}
            if followup < local_now:
                followup_by_actor[owner_id]["overdue"] += 1
                overdue_followups.append(item)
            elif followup.date() == local_now.date():
                followup_by_actor[owner_id]["today"] += 1
                followup_today.append(item)
            else:
                followup_by_actor[owner_id]["upcoming"] += 1
                upcoming_followups.append(item)
        pending_first = (not prop.get("first_action_at")) or _capture_normalized_status(prop.get("status")) in pending_states
        if owner_id in team_ids and pending_first:
            backlog_by_actor[owner_id].append(prop)
            assigned_at = prop.get("assigned_at")
            if assigned_at:
                age = max(0, (local_now.date() - assigned_at.date()).days)
                key = "d_0_1" if age <= 1 else "d_2_3" if age <= 3 else "d_4_5" if age <= 5 else "d_6_10" if age <= 10 else "d_11_20" if age <= 20 else "d_21_30" if age <= 30 else "d_31_60" if age <= 60 else "gt_60"
            else:
                key = "missing_date"
            aging[key] += 1
            aging_by_actor[owner_id][key] += 1

    anomaly_event_ids = {_clean(item.get("event_id")) for item in anomalies if _clean(item.get("event_id"))}
    property_ids_by_event = {_clean(row.get("event_id")): _clean(row.get("property_id")) for row in rows if _clean(row.get("event_id"))}
    anomaly_property_ids = {property_ids_by_event[event_id] for event_id in anomaly_event_ids if property_ids_by_event.get(event_id)}
    attention = []
    attention_seen = set()
    for prop in current_properties:
        property_id = prop.get("id")
        owner_id = _clean(prop.get("executive_id"))
        reasons = []
        priority = 0
        if not owner_id:
            reasons.append((100, "Sin responsable"))
        if prop.get("next_followup") and prop.get("next_followup") < local_now and _capture_normalized_status(prop.get("status")) not in closed_states:
            reasons.append((95, "Seguimiento vencido"))
        if property_id in anomaly_property_ids:
            reasons.append((90, "Anomalía de gestión pendiente de revisión"))
        pending_first = (not prop.get("first_action_at")) or _capture_normalized_status(prop.get("status")) in pending_states
        if pending_first:
            if prop.get("assigned_at"):
                age = max(0, (local_now.date() - prop["assigned_at"].date()).days)
                reasons.append((88 if age > 10 else 80 if age > 5 else 70, "Sin primera gestión"))
            else:
                reasons.append((75, "Sin primera gestión; falta fecha de asignación"))
        if _capture_normalized_status(prop.get("status")) not in closed_states and prop.get("last_management_at") and (local_now - prop["last_management_at"]).days > 5:
            reasons.append((65, "Demasiados días sin actividad"))
        if reasons and property_id not in attention_seen:
            priority, reason = max(reasons, key=lambda item: item[0])
            attention_seen.add(property_id)
            assigned_at = prop.get("assigned_at")
            attention.append({
                "priority": priority,
                "property_id": property_id,
                "title": prop.get("title") or "Sin título",
                "executive": name_by_id.get(owner_id) if owner_id else "Sin asignar",
                "commune": prop.get("comuna") or "Sin comuna",
                "origin": prop.get("origin") or "Sin origen",
                "status": prop.get("status") or "NUEVO",
                "last_management_at": prop.get("last_management_at"),
                "next_followup": prop.get("next_followup"),
                "days_assigned": max(0, (local_now.date() - assigned_at.date()).days) if assigned_at else None,
                "reason": reason,
                "action_url": f"/captacion/{property_id}",
            })
    attention.sort(key=lambda item: (-item["priority"], -(item["days_assigned"] or -1), str(item.get("commune") or "").casefold()))

    executive_payload = { _clean(item.get("user_id")): item for item in result.get("executives") or [] }
    executive_rows = []
    for member in team:
        actor_id = _clean(member.get("id"))
        source = source_metrics
        attempts = len(attempts_by_actor[actor_id])
        effective = len(effective_by_actor[actor_id])
        assigned = len(assigned_by_actor[actor_id])
        managed_current = len(managed_current_by_actor[actor_id])
        first_durations = []
        for prop in current_properties:
            if _clean(prop.get("executive_id")) != actor_id or not prop.get("assigned_at") or not prop.get("first_action_at"):
                continue
            delta = (prop["first_action_at"] - prop["assigned_at"]).total_seconds() / 60
            if delta >= 0:
                first_durations.append(delta)
        executive = dict(executive_payload.get(actor_id, {}))
        executive.update({
            "user_id": actor_id,
            "name": name_by_id.get(actor_id, member.get("name") or actor_id),
            "assigned_count": assigned,
            "managed_unique_period": len({ _clean(row.get("property_id")) for row in credited_rows if _clean(row.get("property_id")) and _clean(row.get("actor_user_id")) == actor_id }),
            "managed_current_portfolio": managed_current,
            "managed_unique_current_portfolio": managed_current,
            "managed_count": managed_current,
            "managed_coverage_pct": _capture_pct(managed_current, assigned) or 0,
            "credited_count": int(executive.get("response_total") or executive.get("week_count") or 0),
            "attempts": len(attempts_by_actor[actor_id]),
            "effective_contacts": len(effective_by_actor[actor_id]),
            "contactability_pct": _capture_pct(effective, attempts),
            "captures_unique": len(captures_by_actor[actor_id]),
            "followups_overdue": followup_by_actor[actor_id]["overdue"],
            "backlog": len(backlog_by_actor[actor_id]),
            "first_action_p50_minutes": _capture_median(first_durations),
            "first_action_sample": len(first_durations),
            "first_action_coverage_pct": _capture_pct(len(first_durations), assigned),
        })
        executive_rows.append(executive)

    assigned_current_ids = set().union(*(assigned_by_actor[actor_id] for actor_id in scoped_actor_ids)) if scoped_actor_ids else set()
    managed_current_ids = set().union(*(managed_current_by_actor[actor_id] for actor_id in scoped_actor_ids)) if scoped_actor_ids else set()
    attempts_total = sum(len(attempts_by_actor[actor_id]) for actor_id in scoped_actor_ids)
    effective_total = sum(len(effective_by_actor[actor_id]) for actor_id in scoped_actor_ids)
    captures_unique = len(set().union(*(captures_by_actor[actor_id] for actor_id in scoped_actor_ids))) if scoped_actor_ids else 0
    source_rows = []
    for origin, metrics in sorted(source_metrics.items(), key=lambda pair: (-len(pair[1]["assigned"]), pair[0].casefold())):
        source_rows.append({
            "dimension": "origen",
            "label": origin,
            "assigned": len(metrics["assigned"]),
            "managed_unique": len(metrics["managed"]),
            "coverage_pct": _capture_pct(len(metrics["managed"]), len(metrics["assigned"])),
            "attempts": len(metrics["attempts"]),
            "effective_contacts": len(metrics["effective"]),
            "contactability_pct": _capture_pct(len(metrics["effective"]), len(metrics["attempts"])),
            "no_answer": len(metrics["no_answer"]),
            "no_answer_pct": _capture_pct(len(metrics["no_answer"]), len(metrics["attempts"])),
            "invalid_number": len(metrics["invalid_number"]),
            "invalid_number_pct": _capture_pct(len(metrics["invalid_number"]), len(metrics["attempts"])),
            "brokers": len(metrics["brokers"]),
            "brokers_pct": _capture_pct(len(metrics["brokers"]), len(metrics["managed"])),
            "captures_unique": len(metrics["captures"]),
        })

    overdue_followups.sort(key=lambda item: item["followup"])
    followup_today.sort(key=lambda item: item["followup"])
    upcoming_followups.sort(key=lambda item: item["followup"])
    def followup_item(item):
        prop = item["property"]
        owner_id = _clean(item.get("executive_id"))
        return {"property_id": prop.get("id"), "title": prop.get("title"), "executive": name_by_id.get(owner_id) or "Sin asignar", "commune": prop.get("comuna"), "status": prop.get("status"), "next_followup": item["followup"], "action_url": f"/captacion/{prop.get('id')}"}

    result["kpis"] = {
        "assigned_current": len(assigned_current_ids),
        "managed_unique_period": len(managed_unique_period),
        "managed_unique_current_portfolio": len(managed_current_ids),
        "coverage_current_pct": _capture_pct(len(managed_current_ids), len(assigned_current_ids)),
        "credited": int(result.get("response_total") or 0),
        "credited_goal": int(result.get("week_goal") or 0),
        "contact_attempts": attempts_total,
        "effective_contacts": effective_total,
        "contactability_pct": _capture_pct(effective_total, attempts_total),
        "captures_unique": captures_unique,
        "capture_rate_managed_pct": _capture_pct(captures_unique, len(managed_unique_period)),
        "capture_rate_effective_pct": _capture_pct(captures_unique, effective_total),
    }
    result["backlog"] = {"total": sum(len(value) for value in backlog_by_actor.values()), "unassigned": int(unassigned_count or 0), "by_executive": [{"name": name_by_id.get(member.get("id"), member.get("name")), "backlog": len(backlog_by_actor[_clean(member.get("id"))])} for member in team]}
    result["aging"] = {"labels": aging_labels, "total": sum(aging.values()), "team": aging, "by_executive": [{"name": name_by_id.get(member.get("id"), member.get("name")), "buckets": aging_by_actor[_clean(member.get("id"))]} for member in team]}
    result["followups"] = {"today": sum(item["today"] for item in followup_by_actor.values()), "overdue": sum(item["overdue"] for item in followup_by_actor.values()), "upcoming": sum(item["upcoming"] for item in followup_by_actor.values()), "overdue_items": [followup_item(item) for item in overdue_followups[:100]], "today_items": [followup_item(item) for item in followup_today[:20]], "upcoming_items": [followup_item(item) for item in upcoming_followups[:20]]}
    result["attention"] = {"total": len(attention), "counts": {"no_first_management": sum(1 for prop in current_properties if _clean(prop.get("executive_id")) in team_ids and ((not prop.get("first_action_at")) or _capture_normalized_status(prop.get("status")) in pending_states)), "overdue_followups": sum(item["overdue"] for item in followup_by_actor.values()), "unassigned": int(unassigned_count or 0), "backlog_critical": sum(1 for prop in current_properties if _clean(prop.get("executive_id")) in team_ids and prop.get("assigned_at") and not prop.get("first_action_at") and (local_now.date() - prop["assigned_at"].date()).days > 10), "anomalies": len(anomalies)}, "cases": attention[:20]}
    result["source_performance"] = source_rows
    if result.get("mode") == "team":
        result["executives"] = executive_rows
    else:
        selected_id = _clean(result.get("user_id"))
        selected_row = next((row for row in executive_rows if row.get("user_id") == selected_id), None)
        if selected_row:
            result.update(selected_row)
    return result


def _managed_data_quality(rows: Iterable[dict], team: Iterable[dict]) -> dict:
    """Resume la calidad declarada por el equipo en las gestiones del período."""
    team_ids = {_clean(member.get("id")) for member in team if _clean(member.get("id"))}
    member_id_by_name = {
        _name_key(member.get("name")): _clean(member.get("id"))
        for member in team
        if _name_key(member.get("name")) and _clean(member.get("id"))
    }
    normalized_rows = []
    for row in rows:
        property_id = _clean(row.get("property_id"))
        if not property_id:
            continue
        actor_id = _clean(row.get("actor_user_id"))
        actor_name = _name_key(row.get("actor"))
        identity = actor_id if actor_id in team_ids else member_id_by_name.get(actor_name)
        if not identity:
            continue
        normalized = dict(row)
        normalized["actor_user_id"] = identity
        normalized_rows.append(normalized)

    def empty():
        return {"total": 0, "corredor": 0, "no_corredor": 0, "incierto": 0, "sin_clasificar": 0}

    total = empty()
    by_executive = defaultdict(empty)
    broker_results = {"broker_identified", "corredor", "es_corredor"}
    for unit in resolve_management_unit_outcomes(normalized_rows):
        identity = _clean(unit.get("actor_user_id"))
        result = _clean(unit.get("result")).casefold()
        detail = _clean(unit.get("detail")).casefold()
        category = "corredor" if result in broker_results or detail == "corredor" else "no_corredor"
        total["total"] += 1
        total[category] += 1
        executive = by_executive[identity]
        executive["total"] += 1
        executive[category] += 1
    total["no_corredor"] = total["total"] - total["corredor"]
    for executive in by_executive.values():
        executive["no_corredor"] = executive["total"] - executive["corredor"]
    return {"total": total, "by_executive": dict(by_executive)}


def _period_managed_counts(rows: Iterable[dict], team: Iterable[dict]) -> dict[str, int]:
    """Cuenta propiedades únicas gestionadas por integrante en el período."""
    team_ids = {_clean(member.get("id")) for member in team if _clean(member.get("id"))}
    team_names = {_name_key(member.get("name")) for member in team}
    managed = defaultdict(set)
    for row in rows:
        if not row.get("credited", True):
            continue
        property_id = _clean(row.get("property_id"))
        actor_id = _clean(row.get("actor_user_id"))
        actor_name = _name_key(row.get("actor"))
        if not property_id:
            continue
        if actor_id in team_ids:
            managed[actor_id].add(property_id)
        elif actor_name in team_names:
            managed[actor_name].add(property_id)
    return {key: len(properties) for key, properties in managed.items()}


def _period_daily_team_counts(
    rows: Iterable[dict],
    team: Iterable[dict],
    days: Iterable[date],
    selected_executive=None,
) -> dict[str, int]:
    """Cuenta unidades diarias del equipo con la misma deduplicación del panel."""
    days_set = {day for day in days}
    team_ids = {_clean(member.get("id")) for member in team if _clean(member.get("id"))}
    team_names = {_name_key(member.get("name")) for member in team if _name_key(member.get("name"))}
    selected_key = _name_key(selected_executive)
    selected_ids = {
        _clean(member.get("id"))
        for member in team
        if selected_key and _name_key(member.get("name")) == selected_key and _clean(member.get("id"))
    }
    selected_identities = {selected_key, *selected_ids} if selected_key else set()
    units_by_day = defaultdict(set)
    for row in rows:
        if not row.get("credited", True):
            continue
        property_id = _clean(row.get("property_id"))
        if not property_id:
            continue
        local = _as_chile_datetime(row.get("occurred_at"))
        if local.date() not in days_set:
            continue
        actor_id = _clean(row.get("actor_user_id"))
        actor_name = _name_key(row.get("actor"))
        if actor_id in team_ids:
            identity = actor_id
        elif actor_name in team_names:
            identity = actor_name
        else:
            continue
        if selected_key and identity not in selected_identities:
            continue
        day_key = local.date().isoformat()
        units_by_day[day_key].add(f"{identity}:{property_id}:{day_key}")
    return {key: len(value) for key, value in units_by_day.items()}


def _dashboard_daily_values(result: dict) -> tuple[dict[str, int], dict[str, int]]:
    """Extrae actual y meta por día desde el resultado ya reconciliado."""
    counts = defaultdict(int)
    targets = defaultdict(int)
    executives = result.get("executives")
    if isinstance(executives, list):
        sources = executives
    else:
        sources = [result] if isinstance(result.get("daily"), list) else []
    for member in sources:
        for item in member.get("daily") or []:
            key = _clean(item.get("date"))
            if not key:
                continue
            counts[key] += int(item.get("count") or 0)
            targets[key] += int(item.get("target") or 0)
    return dict(counts), dict(targets)


def _build_period_series(
    result: dict,
    current_days: list[date],
    comparable_days: list[date],
    comparable_counts: dict[str, int],
) -> list[dict]:
    """Construye la serie acumulada para el gráfico de Gestión Captación."""
    current_counts, targets = _dashboard_daily_values(result)
    current_acc = 0
    comparable_acc = 0
    target_acc = 0
    series = []
    for index, day in enumerate(current_days):
        current = current_counts.get(day.isoformat(), 0)
        comparable_day = comparable_days[index] if index < len(comparable_days) else None
        comparable = comparable_counts.get(comparable_day.isoformat(), 0) if comparable_day else 0
        target = targets.get(day.isoformat(), 0)
        current_acc += current
        comparable_acc += comparable
        target_acc += target
        series.append(
            {
                "date": day.isoformat(),
                "current": current,
                "current_cumulative": current_acc,
                "comparable": comparable,
                "comparable_cumulative": comparable_acc,
                "target": target,
                "target_cumulative": target_acc,
            }
        )
    return series


def _response_breakdown(summary: dict) -> list[dict]:
    """Normaliza los resultados finales para mostrarlos en el dashboard."""
    details = summary.get("detailed_outcomes") or {}
    labels = summary.get("detail_labels") or {}
    return [
        {"key": key, "label": labels.get(key, key), "count": int(count or 0)}
        for key, count in details.items()
        if int(count or 0) > 0
    ]


def get_captacion_goal_dashboard(
    db,
    selected_executive=None,
    now=None,
    period_start=None,
    period_end=None,
    excluded_executives=None,
    include_control=True,
    ensure_indexes=True,
    perf_context=None,
) -> dict:
    _g0 = _perf_time.perf_counter()
    # La página llega después del guard de startup. Mantener el parámetro
    # habilitado por defecto conserva la seguridad de las llamadas externas,
    # pero el cálculo caliente/frío de la vista no vuelve a verificar índices.
    if ensure_indexes:
        ensure_captacion_goal_indexes(db)
    local_now = _as_chile_datetime(now)
    if period_start and period_end:
        selected_start = date.fromisoformat(str(period_start))
        selected_end = date.fromisoformat(str(period_end))
        if selected_end < selected_start:
            raise ValueError("El período de captación es inválido")
        selected_days = [selected_start + timedelta(days=index) for index in range((selected_end - selected_start).days + 1)]
        metric_dates = [day.isoformat() for day in selected_days]
    else:
        monday = local_now.date() - timedelta(days=local_now.weekday())
        selected_days = [monday + timedelta(days=index) for index in range(7)]
        metric_dates = [(monday + timedelta(days=index)).isoformat() for index in CAPTACION_WORKDAYS]

    excluded_keys = {_name_key(value) for value in (excluded_executives or ()) if _name_key(value)}
    _team_started = _perf_time.perf_counter()
    team = [
        member for member in get_explicit_captacion_team(db, local_now.date())
        if _name_key(member.get("name")) not in excluded_keys
    ]
    _team_done = _perf_time.perf_counter()
    _current_ledger_started = _team_done
    rows = get_captacion_management_rows(
        db,
        now=now,
        period_start=period_start,
        period_end=period_end,
    )
    _current_ledger_done = _perf_time.perf_counter()
    # El comparable debe cubrir una ventana inmediatamente anterior de igual
    # longitud. Se mantiene separado del período actual para que la serie y
    # sus métricas no mezclen universos.
    series_days = list(selected_days)
    if series_days:
        comparable_end = series_days[0] - timedelta(days=1)
        comparable_start = comparable_end - timedelta(days=len(series_days) - 1)
        comparable_days = [
            comparable_start + timedelta(days=index)
            for index in range(len(series_days))
        ]
        _comparable_started = _perf_time.perf_counter()
        comparable_rows = get_captacion_management_rows(
            db,
            now=now,
            period_start=comparable_start.isoformat(),
            period_end=comparable_end.isoformat(),
        )
        _comparable_done = _perf_time.perf_counter()
    else:
        comparable_days = []
        comparable_rows = []
        _comparable_started = _comparable_done = _current_ledger_done
    _g1 = _perf_time.perf_counter()

    # Preload calendar + exceptions en 2 consultas (elimina 10N find_one)
    member_ids = [member["id"] for member in team]
    calendar_days = preload_calendar_days(db, metric_dates)
    exceptions = preload_user_exceptions(db, member_ids, metric_dates)
    _g2 = _perf_time.perf_counter()

    for member in team:
        membership = member.get("membership") or {}
        # Los períodos comparables del dashboard deben evaluar un universo de
        # equipo estable. Si un integrante visible fue incorporado al equipo
        # en la fecha actual, no debe hacer que la meta salte solo en el último
        # día del período seleccionado. Se conserva su meta base y se siguen
        # aplicando calendario y excepciones por fecha.
        target_membership = dict(membership)
        if period_start and period_end:
            original_start = str(target_membership.get("start_date") or selected_start.isoformat())
            target_membership["start_date"] = min(original_start, selected_start.isoformat())
            target_membership["workdays"] = list(CAPTACION_WORKDAYS)
        member["day_targets"] = {
            day.isoformat(): applicable_target(
                db, target_membership, day,
                calendar_days=calendar_days, exceptions=exceptions,
            )
            for day in selected_days
        }
    _g3 = _perf_time.perf_counter()

    metrics = list(db[DAILY_METRICS_COLLECTION].find(
        {"user_id": {"$in": member_ids}, "local_date": {"$in": metric_dates}}
    ))
    anomalies = list(db[ANOMALY_COLLECTION].find(
        {"actor_user_id": {"$in": member_ids}, "local_date": {"$in": metric_dates}, "status": "pending_review"},
        {"actor_user_id": 1, "event_id": 1, "type": 1, "detail": 1, "local_date": 1, "status": 1},
    ))
    for member in team:
        member["daily_metrics"] = {
            metric["local_date"]: metric for metric in metrics if metric.get("user_id") == member["id"]
        }
        member["anomaly_count"] = sum(1 for row in anomalies if row.get("actor_user_id") == member["id"])
    _g4 = _perf_time.perf_counter()

    current_properties = []
    unassigned_count = 0
    if include_control:
        current_properties, unassigned_count = _capture_load_current_properties(db, team)
        assigned_counts = _capture_current_assignment_counts(current_properties, team)
        managed_data_quality = _managed_data_quality(rows, team)
        managed_counts = _period_managed_counts(rows, team)

    result = build_captacion_goal_dashboard(
        team,
        rows,
        selected_executive=selected_executive,
        now=now,
        period_start=period_start,
        period_end=period_end,
    )
    if include_control and result.get("mode") == "team":
        for executive in result.get("executives") or []:
            identity = _clean(executive.get("user_id")) or _name_key(executive.get("name"))
            assigned = assigned_counts.get(identity, 0)
            managed = managed_counts.get(identity, 0)
            if not managed:
                managed = managed_counts.get(_name_key(executive.get("name")), 0)
            executive["assigned_count"] = assigned
            executive["managed_count"] = managed
            executive["managed_coverage_pct"] = round(managed * 100 / assigned, 1) if assigned else 0
            executive["managed_data_quality"] = managed_data_quality["by_executive"].get(identity, {
                "total": managed,
                "corredor": 0,
                "no_corredor": managed,
                "incierto": 0,
                "sin_clasificar": 0,
            })
        result["managed_data_quality"] = managed_data_quality["total"]
    elif include_control:
        identity = _clean(result.get("user_id")) or _name_key(result.get("name"))
        assigned = assigned_counts.get(identity, 0)
        managed = managed_counts.get(identity, 0) or managed_counts.get(_name_key(result.get("name")), 0)
        result["assigned_count"] = assigned
        result["managed_count"] = managed
        result["managed_coverage_pct"] = round(managed * 100 / assigned, 1) if assigned else 0
        result["managed_data_quality"] = managed_data_quality["by_executive"].get(identity, {
            "total": managed,
            "corredor": 0,
            "no_corredor": managed,
            "incierto": 0,
            "sin_clasificar": 0,
        })

    period_days_set = set(selected_days)
    period_rows = [
        row for row in rows
        if _as_chile_datetime(row.get("occurred_at")).date() in period_days_set
    ]
    active_member_ids = {_clean(member.get("id")) for member in team if _clean(member.get("id"))}
    active_member_names = {_name_key(member.get("name")) for member in team if _name_key(member.get("name"))}
    team_period_rows = [
        row for row in period_rows
        if (_clean(row.get("actor_user_id")) in active_member_ids)
        or (_name_key(row.get("actor")) in active_member_names)
    ]
    member_id_by_name = {
        _name_key(member.get("name")): _clean(member.get("id"))
        for member in team
        if _name_key(member.get("name")) and _clean(member.get("id"))
    }
    response_period_rows = []
    for row in period_rows:
        actor_id = _clean(row.get("actor_user_id"))
        actor_name = _name_key(row.get("actor"))
        canonical_id = actor_id if actor_id in active_member_ids else member_id_by_name.get(actor_name)
        if not canonical_id:
            continue
        normalized_row = dict(row)
        normalized_row["actor_user_id"] = canonical_id
        response_period_rows.append(normalized_row)
    response_team_rows = [row for row in response_period_rows if row.get("actor_user_id") in active_member_ids]

    def row_matches_executive(row, executive):
        actor_id = _clean(row.get("actor_user_id"))
        actor_name = _name_key(row.get("actor"))
        executive_id = _clean(executive.get("user_id"))
        executive_name = _name_key(executive.get("name"))
        return bool(
            (executive_id and actor_id == executive_id)
            or (executive_name and actor_name == executive_name)
        )

    response_scope = response_team_rows
    if result.get("mode") == "individual":
        response_scope = [
            row for row in response_period_rows
            if row_matches_executive(row, result)
        ]
    response_summary = summarize_grouped_outcomes(response_scope)
    result["response_breakdown"] = _response_breakdown(response_summary)
    result["response_total"] = sum(item["count"] for item in result["response_breakdown"])
    if result.get("mode") == "team":
        for executive in result.get("executives") or []:
            executive_summary = summarize_grouped_outcomes(
                [row for row in response_team_rows if row_matches_executive(row, executive)]
            )
            executive["response_breakdown"] = _response_breakdown(executive_summary)
            executive["response_total"] = sum(item["count"] for item in executive["response_breakdown"])

    # El contrato ampliado concentra el control de cartera, actividad,
    # seguimiento y alertas en una única carga. La página HTML solo necesita
    # el bloque de metas; el endpoint de gestión sigue solicitando este bloque
    # completo mediante include_control=True.
    if include_control:
        result = _capture_control_payload(
            db,
            result,
            team,
            response_team_rows if result.get("mode") == "team" else response_scope,
            anomalies,
            current_properties,
            unassigned_count=unassigned_count,
            now=now,
        )

    # Serie temporal para Gestión Captación: conserva la misma unidad diaria
    # acreditada del tablero y compara contra una ventana inmediatamente
    # anterior de igual longitud. La meta corresponde al período seleccionado.
    comparable_counts = _period_daily_team_counts(
        comparable_rows,
        team,
        comparable_days,
        selected_executive=selected_executive,
    )
    result["period_series"] = _build_period_series(
        result,
        series_days,
        comparable_days,
        comparable_counts,
    )
    result["period_comparable_start"] = comparable_days[0].isoformat() if comparable_days else None
    result["period_comparable_end"] = comparable_days[-1].isoformat() if comparable_days else None
    result["period_comparable_count"] = sum(item["comparable"] for item in result["period_series"])
    result["period_comparable_goal"] = result.get("week_goal", 0)

    start_local = CAPTACION_TIMEZONE.localize(datetime.combine(selected_days[0], time.min))
    history_end_day = selected_days[-1] if period_start and period_end else selected_days[4]
    end_local = CAPTACION_TIMEZONE.localize(datetime.combine(history_end_day + timedelta(days=1), time.min))
    result["history_event_count"] = _get_history_event_count(db, start_local, end_local)
    _g5 = _perf_time.perf_counter()

    logger.debug(
        f"[CAPTACION_GOAL_PERF] team={(_g1-_g0)*1000:.0f} "
        f"preload={(_g2-_g1)*1000:.0f} targets={(_g3-_g2)*1000:.0f} "
        f"metrics_rows={(_g4-_g3)*1000:.0f} build={(_g5-_g4)*1000:.0f} "
        f"total={(_g5-_g0)*1000:.0f}ms members={len(team)} "
        f"team_load={(_team_done-_team_started)*1000:.0f} "
        f"current_ledger={(_current_ledger_done-_current_ledger_started)*1000:.0f} "
        f"comparable_ledger={(_comparable_done-_comparable_started)*1000:.0f}"
    )
    if perf_context is not None:
        perf_context.update({
            "workforce_ms": round((_team_done - _team_started) * 1000, 1),
            "ledger_ms": round((_current_ledger_done - _current_ledger_started) * 1000, 1),
            "comparable_period_ms": round((_comparable_done - _comparable_started) * 1000, 1),
            "goals_rows_and_build_ms": round((_g5 - _g1) * 1000, 1),
            "goals_compute_ms": round((_g5 - _g0) * 1000, 1),
            "goals_members": len(team),
            "goals_include_control": bool(include_control),
        })
    return result
