"""Reglas centralizadas para la meta comercial del equipo de captación."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from typing import Iterable

import pytz

from config import Config


CAPTACION_TIMEZONE = pytz.timezone("America/Santiago")
CAPTACION_DAILY_GOAL = 10
CAPTACION_WORKDAYS = (0, 1, 2, 3, 4)
CAPTACION_WEEKLY_GOAL = CAPTACION_DAILY_GOAL * len(CAPTACION_WORKDAYS)
CAPTACION_GOAL_COLLECTION = "captacion_management_events"
CAPTACION_PRIVILEGED_ROLES = {"admin", "supervisor", "jefatura"}

# Esta configuración queda centralizada para incorporar un calendario de
# feriados confiable más adelante sin dispersar reglas por el proyecto.
CAPTACION_HOLIDAYS: frozenset[str] = frozenset()
_INDEXES_READY = False

VALID_CAPTACION_ACTIONS = {
    ("call_initiated", "tel"),
    ("message_sent", "wa"),
    ("message_sent", "whatsapp"),
    ("message_sent", "email"),
    ("manual_contact", "manual"),
}

DAY_LABELS = ("Lun", "Mar", "Mié", "Jue", "Vie")


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
    pair = (_clean(action).lower(), _clean(channel).lower())
    if pair not in VALID_CAPTACION_ACTIONS:
        return False
    if pair == ("manual_contact", "manual"):
        return bool(_clean(result) or _clean(message))
    return True


def can_manage_captacion(user_doc: dict, property_doc: dict) -> bool:
    role = _clean(user_doc.get("rol") or "agente").lower()
    if role in CAPTACION_PRIVILEGED_ROLES:
        return True

    gestion = property_doc.get("gestion") or {}
    user_name = _name_key(user_doc.get("nombre") or user_doc.get("username"))
    assigned_name = _name_key(gestion.get("ejecutivo_asignado"))
    if user_name and assigned_name and user_name == assigned_name:
        return True

    user_id = _clean(user_doc.get("_id"))
    assigned_id = _clean(gestion.get("ejecutivo_id"))
    user_email = _clean(user_doc.get("email")).casefold()
    assigned_email = _clean(gestion.get("ejecutivo_email")).casefold()
    return bool((user_id and user_id == assigned_id) or (user_email and user_email == assigned_email))


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
    if not _clean(property_id) or not _clean(actor):
        return False
    if not is_valid_captacion_action(action, channel, result=result, message=message):
        return False

    local = _as_chile_datetime(occurred_at)
    dedup_key = management_dedup_key(property_id, actor, local)
    event = {
        "dedup_key": dedup_key,
        "property_id": _clean(property_id),
        "actor": _clean(actor),
        "actor_key": _name_key(actor),
        "actor_id": _clean(actor_id),
        "actor_email": _clean(actor_email).casefold(),
        "action": _clean(action).lower(),
        "channel": _clean(channel).lower(),
        "result": _clean(result),
        "occurred_at": local.astimezone(timezone.utc),
        "local_date": local.date().isoformat(),
        "timezone": "America/Santiago",
    }
    result_doc = db[CAPTACION_GOAL_COLLECTION].update_one(
        {"dedup_key": dedup_key},
        {"$setOnInsert": event},
        upsert=True,
    )
    return bool(getattr(result_doc, "upserted_id", None))


def ensure_captacion_goal_indexes(db) -> None:
    global _INDEXES_READY
    if _INDEXES_READY:
        return
    collection = db[CAPTACION_GOAL_COLLECTION]
    collection.create_index("dedup_key", unique=True, name="captacion_management_dedup")
    collection.create_index(
        [("occurred_at", 1), ("actor_key", 1)],
        name="captacion_management_period_actor",
    )
    _INDEXES_READY = True


def get_active_captacion_team(db) -> list[dict]:
    query = {
        "is_active": True,
        "rol": "agente",
        "comunas_interes_norm": {"$exists": True, "$ne": []},
        "captacion_goal_enabled": {"$ne": False},
    }
    projection = {"nombre": 1, "email": 1, "captacion_goal_enabled": 1}
    users = list(db["usuarios"].find(query, projection).sort("nombre", 1))
    return [
        {"id": _clean(user.get("_id")), "name": _clean(user.get("nombre")), "email": _clean(user.get("email"))}
        for user in users
        if _clean(user.get("nombre"))
    ]


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
            if not is_valid_captacion_action(
                activity.get("action"),
                activity.get("channel"),
                result=activity.get("result"),
                message=activity.get("message"),
            ):
                continue
            yield {
                "property_id": property_id,
                "actor": activity.get("user"),
                "occurred_at": local,
            }


def get_captacion_management_rows(db, now=None) -> list[dict]:
    local_now = _as_chile_datetime(now)
    monday = local_now.date() - timedelta(days=local_now.weekday())
    start_local = CAPTACION_TIMEZONE.localize(datetime.combine(monday, time.min))
    end_local = start_local + timedelta(days=7)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    rows = []
    ledger = db[CAPTACION_GOAL_COLLECTION].find(
        {"occurred_at": {"$gte": start_utc, "$lt": end_utc}},
        {"property_id": 1, "actor": 1, "occurred_at": 1},
    )
    for event in ledger:
        rows.append(
            {
                "property_id": _clean(event.get("property_id")),
                "actor": event.get("actor"),
                "occurred_at": _as_chile_datetime(event.get("occurred_at")),
            }
        )
    rows.extend(_iter_historical_activity_rows(db, start_local, end_local))
    return rows


def build_captacion_goal_dashboard(team: Iterable[dict], rows: Iterable[dict], selected_executive=None, now=None) -> dict:
    local_now = _as_chile_datetime(now)
    monday = local_now.date() - timedelta(days=local_now.weekday())
    weekdays = [monday + timedelta(days=index) for index in CAPTACION_WORKDAYS]
    today = local_now.date()

    members = [member for member in team if _clean(member.get("name"))]
    names = {_name_key(member["name"]): member["name"] for member in members}
    selected_key = _name_key(selected_executive)
    if selected_key and selected_key not in names:
        names[selected_key] = _clean(selected_executive)

    counts = defaultdict(lambda: defaultdict(set))
    last_activity = {}
    weekend_activity = defaultdict(set)
    for row in rows:
        actor_key = _name_key(row.get("actor"))
        property_id = _clean(row.get("property_id"))
        if not actor_key or not property_id:
            continue
        local = _as_chile_datetime(row.get("occurred_at"))
        dedup = f"{actor_key}:{property_id}:{local.date().isoformat()}"
        if local.date() in weekdays:
            counts[actor_key][local.date()].add(dedup)
        elif monday <= local.date() <= monday + timedelta(days=6):
            weekend_activity[actor_key].add(dedup)
        if actor_key not in last_activity or local > last_activity[actor_key]:
            last_activity[actor_key] = local

    elapsed_workdays = sum(1 for day in weekdays if day <= today)

    def member_metrics(name_key, display_name):
        daily = []
        week_total = 0
        days_met = 0
        for index, day in enumerate(weekdays):
            count = len(counts[name_key][day])
            week_total += count
            met = count >= CAPTACION_DAILY_GOAL
            days_met += int(met)
            daily.append(
                {
                    "label": DAY_LABELS[index],
                    "date": day.isoformat(),
                    "count": count,
                    "met": met,
                    "future": day > today,
                    "today": day == today,
                }
            )
        today_count = len(counts[name_key][today]) if today in weekdays else 0
        is_workday = today in weekdays
        return {
            "name": display_name,
            "today_count": today_count,
            "today_goal": CAPTACION_DAILY_GOAL if is_workday else 0,
            "today_percent": round(today_count * 100 / CAPTACION_DAILY_GOAL, 1) if is_workday else None,
            "today_remaining": max(0, CAPTACION_DAILY_GOAL - today_count) if is_workday else 0,
            "met_today": bool(is_workday and today_count >= CAPTACION_DAILY_GOAL),
            "is_workday": is_workday,
            "week_count": week_total,
            "week_goal": CAPTACION_WEEKLY_GOAL,
            "week_percent": round(week_total * 100 / CAPTACION_WEEKLY_GOAL, 1),
            "days_met": days_met,
            "days_goal": len(CAPTACION_WORKDAYS),
            "expected_to_date": elapsed_workdays * CAPTACION_DAILY_GOAL,
            "daily": daily,
            "weekend_activity": len(weekend_activity[name_key]),
            "last_activity": last_activity.get(name_key),
        }

    if selected_key:
        metrics = member_metrics(selected_key, names[selected_key])
        return {"mode": "individual", "timezone": "America/Santiago", **metrics}

    team_rows = [member_metrics(key, display) for key, display in names.items()]
    team_rows.sort(key=lambda row: (row["met_today"], row["today_count"] == 0, row["today_count"], row["name"].casefold()))
    member_count = len(team_rows)
    return {
        "mode": "team",
        "timezone": "America/Santiago",
        "member_count": member_count,
        "today_count": sum(row["today_count"] for row in team_rows),
        "today_goal": member_count * CAPTACION_DAILY_GOAL if today in weekdays else 0,
        "executives_met_today": sum(1 for row in team_rows if row["met_today"]),
        "executives_pending_today": sum(1 for row in team_rows if not row["met_today"]) if today in weekdays else 0,
        "week_count": sum(row["week_count"] for row in team_rows),
        "week_goal": member_count * CAPTACION_WEEKLY_GOAL,
        "days_person_met": sum(row["days_met"] for row in team_rows),
        "days_person_goal": member_count * len(CAPTACION_WORKDAYS),
        "expected_to_date": member_count * elapsed_workdays * CAPTACION_DAILY_GOAL,
        "weekend_activity": sum(row["weekend_activity"] for row in team_rows),
        "executives": team_rows,
    }


def get_captacion_goal_dashboard(db, selected_executive=None, now=None) -> dict:
    ensure_captacion_goal_indexes(db)
    team = get_active_captacion_team(db)
    rows = get_captacion_management_rows(db, now=now)
    return build_captacion_goal_dashboard(team, rows, selected_executive=selected_executive, now=now)
