"""Reglas centralizadas para la meta comercial del equipo de captación."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import os
from typing import Iterable

import pytz

from config import Config
from captacion_workforce import (
    DEFAULT_TIMEZONE,
    applicable_target,
    compliance_status,
    get_active_captacion_team as get_explicit_captacion_team,
)
from captacion_management import (
    ANOMALY_COLLECTION,
    DAILY_METRICS_COLLECTION,
    VALID_CREDIT_EVENT_TYPES,
    ensure_management_indexes,
    normalize_result,
    normalize_started_action,
    summarize_final_outcomes,
)


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


def get_captacion_management_rows(db, now=None) -> list[dict]:
    local_now = _as_chile_datetime(now)
    monday = local_now.date() - timedelta(days=local_now.weekday())
    start_local = CAPTACION_TIMEZONE.localize(datetime.combine(monday, time.min))
    end_local = start_local + timedelta(days=7)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

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
    if local_now.date() <= dual_read_until:
        legacy_end = min(end_local, CAPTACION_TIMEZONE.localize(datetime.combine(cutover, time.min)))
        if start_local < legacy_end:
            rows.extend(_iter_historical_activity_rows(db, start_local, legacy_end))
    return rows


def build_captacion_goal_dashboard(team: Iterable[dict], rows: Iterable[dict], selected_executive=None, now=None) -> dict:
    local_now = _as_chile_datetime(now)
    monday = local_now.date() - timedelta(days=local_now.weekday())
    weekdays = [monday + timedelta(days=index) for index in CAPTACION_WORKDAYS]
    today = local_now.date()

    members = [member for member in team if _clean(member.get("name"))]
    names = {_name_key(member["name"]): member["name"] for member in members}
    members_by_name = {_name_key(member["name"]): member for member in members}
    selected_key = _name_key(selected_executive)
    if selected_key and selected_key not in names:
        names[selected_key] = _clean(selected_executive)

    counts = defaultdict(lambda: defaultdict(set))
    last_activity = {}
    weekend_activity = defaultdict(set)
    for row in rows:
        actor_key = _clean(row.get("actor_user_id")) or _name_key(row.get("actor"))
        property_id = _clean(row.get("property_id"))
        if not actor_key or not property_id:
            continue
        local = _as_chile_datetime(row.get("occurred_at"))
        dedup = f"{actor_key}:{property_id}:{local.date().isoformat()}"
        if row.get("credited", True) and local.date() in weekdays:
            counts[actor_key][local.date()].add(dedup)
        elif row.get("credited", True) and monday <= local.date() <= monday + timedelta(days=6):
            weekend_activity[actor_key].add(dedup)
        if actor_key not in last_activity or local > last_activity[actor_key]:
            last_activity[actor_key] = local

    elapsed_workdays = sum(1 for day in weekdays if day <= today)

    def member_metrics(name_key, display_name):
        member = members_by_name.get(name_key, {})
        identity_key = _clean(member.get("id")) or name_key
        day_targets = member.get("day_targets") or {}
        daily = []
        week_total = 0
        days_met = 0
        days_goal = len(weekdays)
        week_goal = 0
        for index, day in enumerate(weekdays):
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
            target = int(target_info.get("target") or 0)
            week_total += count
            week_goal += target
            met = bool(target > 0 and count >= target)
            days_met += int(met)
            daily.append(
                {
                    "label": DAY_LABELS[index],
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
        today_reason = today_info.get("reason") if today_info else DAY_NAMES[today.weekday()]
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

    weekday_rows = [
        row for row in rows
        if monday <= _as_chile_datetime(row.get("occurred_at")).date() <= monday + timedelta(days=4)
    ]
    final_outcomes = summarize_final_outcomes(weekday_rows)

    if selected_key:
        metrics = member_metrics(selected_key, names[selected_key])
        identity_key = metrics.get("user_id") or selected_key
        individual_rows = [
            row for row in weekday_rows
            if (_clean(row.get("actor_user_id")) or _name_key(row.get("actor"))) in {identity_key, selected_key}
        ]
        return {
            "mode": "individual",
            "timezone": "America/Santiago",
            "final_outcomes": summarize_final_outcomes(individual_rows),
            **metrics,
        }

    team_rows = [member_metrics(key, display) for key, display in names.items()]
    team_rows.sort(key=lambda row: (row["met_today"], row["today_count"] == 0, row["today_count"], row["name"].casefold()))
    member_count = len(team_rows)
    return {
        "mode": "team",
        "timezone": "America/Santiago",
        "member_count": member_count,
        "today_count": sum(row["today_count"] for row in team_rows),
        "today_goal": sum(row["today_goal"] for row in team_rows),
        "executives_met_today": sum(1 for row in team_rows if row["met_today"]),
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
        "final_outcomes": final_outcomes,
        "anomaly_count": sum(row["anomaly_count"] for row in team_rows),
        "executives": team_rows,
    }


def get_captacion_goal_dashboard(db, selected_executive=None, now=None) -> dict:
    ensure_captacion_goal_indexes(db)
    local_now = _as_chile_datetime(now)
    monday = local_now.date() - timedelta(days=local_now.weekday())
    team = get_explicit_captacion_team(db, local_now.date())
    for member in team:
        membership = member.get("membership") or {}
        member["day_targets"] = {
            (monday + timedelta(days=index)).isoformat(): applicable_target(
                db, membership, monday + timedelta(days=index)
            )
            for index in CAPTACION_WORKDAYS
        }
    member_ids = [member["id"] for member in team]
    week_dates = [(monday + timedelta(days=index)).isoformat() for index in CAPTACION_WORKDAYS]
    metrics = list(db[DAILY_METRICS_COLLECTION].find(
        {"user_id": {"$in": member_ids}, "local_date": {"$in": week_dates}}
    ))
    anomalies = list(db[ANOMALY_COLLECTION].find(
        {"actor_user_id": {"$in": member_ids}, "local_date": {"$in": week_dates}, "status": "pending_review"},
        {"actor_user_id": 1},
    ))
    for member in team:
        member["daily_metrics"] = {
            metric["local_date"]: metric for metric in metrics if metric.get("user_id") == member["id"]
        }
        member["anomaly_count"] = sum(1 for row in anomalies if row.get("actor_user_id") == member["id"])
    rows = get_captacion_management_rows(db, now=now)
    result = build_captacion_goal_dashboard(team, rows, selected_executive=selected_executive, now=now)
    start_local = CAPTACION_TIMEZONE.localize(datetime.combine(monday, time.min))
    end_local = start_local + timedelta(days=5)
    result["history_event_count"] = db[CAPTACION_GOAL_COLLECTION].count_documents({
        "occurred_at": {
            "$gte": start_local.astimezone(timezone.utc),
            "$lt": end_local.astimezone(timezone.utc),
        }
    })
    return result
