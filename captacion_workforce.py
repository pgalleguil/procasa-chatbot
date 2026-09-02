"""Configuración auditable del equipo y calendario laboral de Captación."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from copy import deepcopy
import logging
import time as _perf_time
from typing import Iterable

import pytz
from bson import ObjectId


MEMBERSHIP_COLLECTION = "captacion_team_memberships"
EXCEPTION_COLLECTION = "captacion_work_exceptions"
CALENDAR_COLLECTION = "captacion_work_calendar"
WORKFORCE_AUDIT_COLLECTION = "captacion_workforce_audit"

DEFAULT_TIMEZONE = "America/Santiago"
DEFAULT_DAILY_TARGET = 10
DEFAULT_WORKDAYS = (0, 1, 2, 3, 4)
DEFAULT_CLOSE_HOUR = 19
VALID_EXCEPTION_TYPES = {
    "vacaciones",
    "licencia",
    "dia_administrativo",
    "capacitacion",
    "feriado",
    "media_jornada",
}

_INDEXES_READY = False
_TEAM_CACHE = {}
_TEAM_CACHE_TTL_SECONDS = 300

logger = logging.getLogger(__name__)


def clear_captacion_team_cache() -> None:
    """Invalida el equipo cacheado cuando cambia una membresía."""
    _TEAM_CACHE.clear()


def clean_id(value) -> str:
    return str(value or "").strip()


def parse_local_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def localize(value: datetime | None, timezone_name=DEFAULT_TIMEZONE) -> datetime:
    tz = pytz.timezone(timezone_name)
    if value is None:
        return datetime.now(tz)
    if value.tzinfo is None:
        value = pytz.utc.localize(value)
    return value.astimezone(tz)


def ensure_workforce_indexes(db) -> None:
    global _INDEXES_READY
    if _INDEXES_READY:
        return
    db[MEMBERSHIP_COLLECTION].create_index("user_id", unique=True, name="captacion_membership_user")
    db[MEMBERSHIP_COLLECTION].create_index(
        [("enabled", 1), ("start_date", 1), ("end_date", 1)],
        name="captacion_membership_period",
    )
    db[EXCEPTION_COLLECTION].create_index(
        [("user_id", 1), ("local_date", 1)], unique=True, name="captacion_exception_user_day"
    )
    db[CALENDAR_COLLECTION].create_index(
        [("timezone", 1), ("local_date", 1)], unique=True, name="captacion_calendar_day"
    )
    db[WORKFORCE_AUDIT_COLLECTION].create_index("created_at", name="captacion_workforce_audit_date")
    _INDEXES_READY = True


def membership_is_active(membership: dict, local_day: date) -> bool:
    if not membership.get("enabled", False):
        return False
    start = parse_local_date(membership["start_date"])
    end_raw = membership.get("end_date")
    end = parse_local_date(end_raw) if end_raw else None
    return start <= local_day and (end is None or local_day <= end)


def get_active_memberships(db, local_day: date | str) -> list[dict]:
    ensure_workforce_indexes(db)
    day = parse_local_date(local_day)
    query = {
        "enabled": True,
        "start_date": {"$lte": day.isoformat()},
        "$or": [{"end_date": None}, {"end_date": {"$exists": False}}, {"end_date": {"$gte": day.isoformat()}}],
    }
    memberships = list(db[MEMBERSHIP_COLLECTION].find(query).sort("user_id", 1))
    return [membership for membership in memberships if membership_is_active(membership, day)]


def resolve_membership_users(db, memberships: Iterable[dict]) -> list[dict]:
    memberships = list(memberships)
    string_ids = [clean_id(item.get("user_id")) for item in memberships]
    object_ids = []
    for value in string_ids:
        try:
            object_ids.append(ObjectId(value))
        except Exception:
            pass
    users = list(db["usuarios"].find({"$or": [{"_id": {"$in": object_ids}}, {"_id": {"$in": string_ids}}]}))
    by_id = {clean_id(user.get("_id")): user for user in users}
    result = []
    for membership in memberships:
        user_id = clean_id(membership.get("user_id"))
        user = by_id.get(user_id)
        if not user or user.get("is_active") is False:
            continue
        result.append(
            {
                "id": user_id,
                "name": user.get("nombre") or user.get("username") or user_id,
                "email": user.get("email") or "",
                "membership": membership,
            }
        )
    return result


def _auto_agents_without_membership(db, memberships: list[dict], day: date) -> list[dict]:
    explicit_user_ids = {
        clean_id(item.get("user_id")) for item in db[MEMBERSHIP_COLLECTION].find({}, {"user_id": 1})
    }
    membership_user_ids = {clean_id(item.get("user_id")) for item in memberships}
    auto_ids = []
    for user in db["usuarios"].find({"rol": "agente"}):
        user_id = clean_id(user.get("_id"))
        if user.get("is_active") is False:
            continue
        if user_id in membership_user_ids or user_id in explicit_user_ids:
            continue
        auto_ids.append(user_id)
    if not auto_ids:
        return []
    object_ids = []
    for value in auto_ids:
        try:
            object_ids.append(ObjectId(value))
        except Exception:
            pass
    users = list(db["usuarios"].find({"_id": {"$in": auto_ids + object_ids}}))
    result = []
    for user in users:
        user_id = clean_id(user.get("_id"))
        result.append(
            {
                "id": user_id,
                "name": user.get("nombre") or user.get("username") or user_id,
                "email": user.get("email") or "",
                "membership": {
                    "user_id": user_id,
                    "enabled": True,
                    "start_date": day.isoformat(),
                    "end_date": None,
                    "daily_target": DEFAULT_DAILY_TARGET,
                    "workdays": list(DEFAULT_WORKDAYS),
                    "supervisor_id": None,
                    "timezone": DEFAULT_TIMEZONE,
                    "close_hour": DEFAULT_CLOSE_HOUR,
                    "auto_inferred": True,
                },
            }
        )
    return result


def get_active_captacion_team(db, local_day: date | str) -> list[dict]:
    """Equipo del dashboard: membresías explícitas activas + agentes activos sin
    membresía (inferido por `rol=agente`; nunca por comunas). Crear/desactivar la
    membresía sigue siendo la vía para controlar la inclusión."""
    day = parse_local_date(local_day)
    cache_key = (id(db), day.isoformat())
    cache_now = _perf_time.time()
    cached = _TEAM_CACHE.get(cache_key)
    if cached and cache_now - cached[0] < _TEAM_CACHE_TTL_SECONDS:
        return deepcopy(cached[1])

    _t0 = _perf_time.perf_counter()
    memberships = get_active_memberships(db, day)
    _t1 = _perf_time.perf_counter()
    result = resolve_membership_users(db, memberships)
    result.extend(_auto_agents_without_membership(db, memberships, day))
    _t2 = _perf_time.perf_counter()
    logger.debug(
        f"[CAPTACION_GOAL_PERF] team: memberships={(_t1-_t0)*1000:.0f}ms "
        f"resolve_users={(_t2-_t1)*1000:.0f}ms members={len(result)}"
    )
    _TEAM_CACHE[cache_key] = (cache_now, deepcopy(result))
    if len(_TEAM_CACHE) > 16:
        oldest_key = min(_TEAM_CACHE, key=lambda key: _TEAM_CACHE[key][0])
        _TEAM_CACHE.pop(oldest_key, None)
    return deepcopy(result)


def get_calendar_day(db, local_day: date, timezone_name: str) -> dict | None:
    return db[CALENDAR_COLLECTION].find_one(
        {"local_date": local_day.isoformat(), "timezone": timezone_name, "enabled": {"$ne": False}}
    )


def get_user_exception(db, user_id, local_day: date) -> dict | None:
    return db[EXCEPTION_COLLECTION].find_one(
        {
            "user_id": clean_id(user_id),
            "local_date": local_day.isoformat(),
            "approved": True,
            "voided_at": {"$exists": False},
        }
    )


def applicable_target(db, membership: dict, local_day: date, calendar_days=None, exceptions=None) -> dict:
    timezone_name = membership.get("timezone") or DEFAULT_TIMEZONE
    base_target = int(membership.get("daily_target") or DEFAULT_DAILY_TARGET)
    if not membership_is_active(membership, local_day):
        return {
            "target": 0,
            "base_target": base_target,
            "exempt": True,
            "reason": "Fuera del período de membresía",
            "source": "membership_period",
            "timezone": timezone_name,
            "close_hour": int(membership.get("close_hour") or DEFAULT_CLOSE_HOUR),
            "exception_id": None,
        }
    workdays = tuple(int(day) for day in membership.get("workdays") or DEFAULT_WORKDAYS)
    calendar_day = None
    if calendar_days is not None:
        calendar_day = calendar_days.get((local_day.isoformat(), timezone_name))
    else:
        calendar_day = get_calendar_day(db, local_day, timezone_name)
    exception = None
    if exceptions is not None:
        user_id = clean_id(membership.get("user_id"))
        exception = exceptions.get((user_id, local_day.isoformat()))
    else:
        exception = get_user_exception(db, membership.get("user_id"), local_day)

    scheduled = local_day.weekday() in workdays
    target = base_target if scheduled else 0
    reason = None if scheduled else "Día no laborable"
    source = "membership"

    if calendar_day and calendar_day.get("is_working_day") is False:
        target = int(calendar_day.get("target_override") or 0)
        reason = calendar_day.get("label") or "Feriado"
        source = "calendar"

    if exception:
        exception_type = exception.get("type")
        if exception_type not in VALID_EXCEPTION_TYPES:
            raise ValueError(f"Tipo de excepción no válido: {exception_type}")
        if exception.get("target_override") is not None:
            target = max(0, int(exception["target_override"]))
        elif exception_type == "media_jornada":
            target = max(0, (base_target + 1) // 2)
        else:
            target = 0
        reason = exception.get("reason") or exception_type.replace("_", " ").title()
        source = "exception"

    return {
        "target": target,
        "base_target": base_target,
        "exempt": target == 0 and scheduled,
        "reason": reason,
        "source": source,
        "timezone": timezone_name,
        "close_hour": int(membership.get("close_hour") or DEFAULT_CLOSE_HOUR),
        "exception_id": clean_id((exception or {}).get("_id")) or None,
    }


def compliance_status(*, count: int, target: int, local_day: date, now: datetime, close_hour=DEFAULT_CLOSE_HOUR, exempt=False) -> str:
    local_now = localize(now, getattr(now.tzinfo, "zone", None) or DEFAULT_TIMEZONE)
    if exempt:
        return "EXENTO"
    if local_day > local_now.date():
        return "FUTURO"
    if count >= target and target > 0:
        return "CUMPLIDO"
    if target == 0:
        return "EXENTO"
    if local_day < local_now.date() or local_now.time() >= time(close_hour, 0):
        return "INCUMPLIDO"
    return "EN_PROGRESO"


def upsert_membership(db, payload: dict, actor_user_id) -> dict:
    ensure_workforce_indexes(db)
    user_id = clean_id(payload.get("user_id"))
    if not user_id:
        raise ValueError("user_id es obligatorio")
    workdays = [int(day) for day in payload.get("workdays") or DEFAULT_WORKDAYS]
    if any(day < 0 or day > 6 for day in workdays):
        raise ValueError("workdays contiene un día inválido")
    document = {
        "user_id": user_id,
        "enabled": bool(payload.get("enabled", True)),
        "start_date": parse_local_date(payload.get("start_date") or date.today()).isoformat(),
        "end_date": parse_local_date(payload["end_date"]).isoformat() if payload.get("end_date") else None,
        "daily_target": max(0, int(payload.get("daily_target") or DEFAULT_DAILY_TARGET)),
        "workdays": workdays,
        "supervisor_id": clean_id(payload.get("supervisor_id")) or None,
        "timezone": payload.get("timezone") or DEFAULT_TIMEZONE,
        "close_hour": int(payload.get("close_hour") or DEFAULT_CLOSE_HOUR),
        "updated_at": datetime.now(timezone.utc),
        "updated_by": clean_id(actor_user_id),
    }
    db[MEMBERSHIP_COLLECTION].update_one(
        {"user_id": user_id},
        {"$set": document, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    db[WORKFORCE_AUDIT_COLLECTION].insert_one(
        {"type": "membership_upserted", "user_id": user_id, "actor_user_id": clean_id(actor_user_id), "snapshot": document, "created_at": datetime.now(timezone.utc)}
    )
    clear_captacion_team_cache()
    return document


def create_work_exception(db, payload: dict, actor_user_id) -> dict:
    ensure_workforce_indexes(db)
    exception_type = str(payload.get("type") or "").strip().lower()
    if exception_type not in VALID_EXCEPTION_TYPES:
        raise ValueError("Tipo de excepción no válido")
    user_id = clean_id(payload.get("user_id"))
    local_day = parse_local_date(payload.get("local_date"))
    if not user_id:
        raise ValueError("user_id es obligatorio")
    now = datetime.now(timezone.utc)
    document = {
        "user_id": user_id,
        "local_date": local_day.isoformat(),
        "type": exception_type,
        "target_override": int(payload["target_override"]) if payload.get("target_override") is not None else None,
        "reason": str(payload.get("reason") or "").strip(),
        "approved": True,
        "approved_by": clean_id(actor_user_id),
        "approved_at": now,
        "created_by": clean_id(actor_user_id),
        "created_at": now,
    }
    db[EXCEPTION_COLLECTION].update_one(
        {"user_id": user_id, "local_date": local_day.isoformat()}, {"$set": document}, upsert=True
    )
    db[WORKFORCE_AUDIT_COLLECTION].insert_one(
        {"type": "exception_upserted", "user_id": user_id, "local_date": local_day.isoformat(), "actor_user_id": clean_id(actor_user_id), "snapshot": document, "created_at": now}
    )
    return document


def upsert_calendar_day(db, payload: dict, actor_user_id) -> dict:
    ensure_workforce_indexes(db)
    local_day = parse_local_date(payload.get("local_date"))
    timezone_name = payload.get("timezone") or DEFAULT_TIMEZONE
    now = datetime.now(timezone.utc)
    document = {
        "local_date": local_day.isoformat(),
        "timezone": timezone_name,
        "label": str(payload.get("label") or "Feriado").strip(),
        "is_working_day": bool(payload.get("is_working_day", False)),
        "target_override": int(payload["target_override"]) if payload.get("target_override") is not None else 0,
        "source": str(payload.get("source") or "administrative_override"),
        "source_reference": str(payload.get("source_reference") or ""),
        "updated_by": clean_id(actor_user_id),
        "updated_at": now,
        "enabled": True,
    }
    db[CALENDAR_COLLECTION].update_one(
        {"local_date": local_day.isoformat(), "timezone": timezone_name}, {"$set": document}, upsert=True
    )
    db[WORKFORCE_AUDIT_COLLECTION].insert_one(
        {"type": "calendar_day_upserted", "local_date": local_day.isoformat(), "actor_user_id": clean_id(actor_user_id), "snapshot": document, "created_at": now}
    )
    return document


def preload_calendar_days(db, dates: list[str]) -> dict:
    result = {}
    for row in db[CALENDAR_COLLECTION].find(
        {"local_date": {"$in": dates}, "enabled": {"$ne": False}}
    ):
        result[(row["local_date"], row.get("timezone", DEFAULT_TIMEZONE))] = row
    return result


def preload_user_exceptions(db, user_ids: list[str], dates: list[str]) -> dict:
    result = {}
    for row in db[EXCEPTION_COLLECTION].find({
        "user_id": {"$in": user_ids},
        "local_date": {"$in": dates},
        "approved": True,
        "voided_at": {"$exists": False},
    }):
        result[(row["user_id"], row["local_date"])] = row
    return result
