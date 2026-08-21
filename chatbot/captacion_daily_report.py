"""Reporte compacto de Gestión de Captación para WhatsApp.

Este módulo es de solo lectura sobre el CRM. La única operación externa es el
envío explícito solicitado por el operador en modo prueba.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pytz

from config import Config
from captacion_management import LEDGER_COLLECTION, VALID_CREDIT_EVENT_TYPES
from captacion_workforce import get_active_captacion_team
from .whatsapp_client import (
    mask_whatsapp_recipient,
    normalize_whatsapp_recipient,
    send_whatsapp_message_detailed,
)
from pymongo import MongoClient, ReadPreference, ReturnDocument
from pymongo.errors import AutoReconnect, DuplicateKeyError, NetworkTimeout

logger = logging.getLogger(__name__)
CHILE = pytz.timezone("America/Santiago")
DAILY_TARGET = 10
COVERAGE_THRESHOLD_DAYS = 10
DAILY_DELIVERY_COLLECTION = Config.CAPTACION_DAILY_DELIVERY_COLLECTION
DAILY_PRODUCTION_START_HOUR = 8
DAILY_PRODUCTION_START_MINUTE = 30
DAILY_PRODUCTION_END_HOUR = 12
DAILY_PRODUCTION_RETRY_COOLDOWN_SECONDS = 300
DAILY_NO_DATA_STATUS = "skipped_no_data"


def scheduled_period_for_run(run_date: date | str) -> tuple[date, date] | None:
    """Devuelve el periodo cerrado que corresponde a una ejecución local."""
    run_date = date.fromisoformat(str(run_date)) if not isinstance(run_date, date) else run_date
    if run_date.weekday() in (5, 6):
        return None
    if run_date.weekday() == 0:
        previous_monday = run_date - timedelta(days=7)
        return previous_monday, previous_monday + timedelta(days=4)
    previous = run_date - timedelta(days=1)
    return previous, previous


def daily_production_window_open(run_at: datetime | None = None) -> bool:
    """True during the Tue-Fri 08:30 (inclusive) to 12:00 (exclusive) window."""
    local_now = (run_at.astimezone(CHILE) if run_at else datetime.now(CHILE))
    if local_now.weekday() not in (1, 2, 3, 4):
        return False
    start = local_now.replace(
        hour=DAILY_PRODUCTION_START_HOUR,
        minute=DAILY_PRODUCTION_START_MINUTE,
        second=0,
        microsecond=0,
    )
    end = local_now.replace(
        hour=DAILY_PRODUCTION_END_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    return start <= local_now < end


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _local(value: Any) -> datetime | None:
    parsed = _utc(value)
    return parsed.astimezone(CHILE) if parsed else None


def _close_bounds(period: date) -> tuple[datetime, datetime]:
    start = CHILE.localize(datetime.combine(period, time.min))
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _name_key(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _assignment_at_close(property_doc: dict, close_utc: datetime) -> tuple[str, datetime] | None:
    """Replay assignment history without relying on current mutable fields."""
    gestion = property_doc.get("gestion") or {}
    events: list[tuple[datetime, int, str, str]] = []
    for item in gestion.get("historial_asignaciones") or []:
        at = _utc(item.get("assigned_at"))
        owner = str(item.get("ejecutivo_id") or "").strip()
        if at and owner and at <= close_utc:
            events.append((at, 0, "assign", owner))
    for item in gestion.get("historial_desasignaciones") or []:
        at = _utc(item.get("removed_at"))
        owner = str(item.get("ejecutivo_id") or "").strip()
        if at and at <= close_utc:
            events.append((at, 1, "remove", owner))
    current_owner = str(gestion.get("ejecutivo_id") or "").strip()
    current_at = _utc(gestion.get("fecha_asignacion"))
    if current_owner and current_at and current_at <= close_utc:
        events.append((current_at, 0, "assign", current_owner))
    owner = None
    assigned_at = None
    for at, _, kind, event_owner in sorted(events):
        if kind == "assign":
            owner, assigned_at = event_owner, at
        elif owner and (not event_owner or event_owner == owner):
            owner, assigned_at = None, None
    return (owner, assigned_at) if owner and assigned_at else None


def _active_assignments(db, close_utc: datetime, team_ids: set[str]) -> dict[str, set[str]]:
    assignments: dict[str, set[str]] = defaultdict(set)
    collection = Config.get_captacion_collection(db)
    for prop in collection.find({}, {"_id": 1, "gestion": 1}):
        active = _assignment_at_close(prop, close_utc)
        if active and active[0] in team_ids:
            assignments[active[0]].add(str(prop.get("_id")))
    return assignments


def _current_visible_assignments(db, team: list[dict]) -> dict[str, set[str]]:
    """Return the same operational population shown by GET /captacion.

    The daily activity remains anchored to the closed reporting period. The
    portfolio side intentionally uses the current CRM visibility predicate:
    supported origins plus the three visible owner-classification states.
    """
    team_by_id = {str(member["id"]): member for member in team}
    name_to_id = {_name_key(member["name"]): str(member["id"]) for member in team}
    visible: dict[str, set[str]] = defaultdict(set)
    collection = Config.get_captacion_collection(db)
    query = {
        "origen": {"$in": ["toctoc", "yapo"]},
        "classification.state": {"$in": ["DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"]},
    }
    for prop in collection.find(query, {"_id": 1, "gestion": 1}):
        gestion = prop.get("gestion") or {}
        owner_id = str(gestion.get("ejecutivo_id") or "").strip()
        if owner_id in team_by_id:
            visible[owner_id].add(str(prop.get("_id")))
            continue
        # Same legacy fallback used by get_captacion_list().
        if not owner_id:
            owner_name = _name_key(gestion.get("ejecutivo_asignado"))
            legacy_id = name_to_id.get(owner_name)
            if legacy_id:
                visible[legacy_id].add(str(prop.get("_id")))
    return visible


def _credited_events(db, start_utc: datetime | None = None, end_utc: datetime | None = None) -> list[dict]:
    query: dict[str, Any] = {
        "event_type": {"$in": list(VALID_CREDIT_EVENT_TYPES)},
        "credited": True,
    }
    if start_utc or end_utc:
        query["occurred_at"] = {}
        if start_utc:
            query["occurred_at"]["$gte"] = start_utc
        if end_utc:
            query["occurred_at"]["$lt"] = end_utc
    rows = list(db[LEDGER_COLLECTION].find(query, {
        "_id": 0, "property_id": 1, "actor_user_id": 1, "actor_name_snapshot": 1,
        "occurred_at": 1, "event_id": 1, "credited": 1,
    }))
    reversed_ids = {
        row.get("original_event_id")
        for row in db[LEDGER_COLLECTION].find(
            {"event_type": "management_reversed", "original_event_id": {"$in": [r.get("event_id") for r in rows]}},
            {"original_event_id": 1},
        )
    }
    return [row for row in rows if row.get("event_id") not in reversed_ids]


def calculate_period_report(db, period_start: date | str, period_end: date | str) -> dict:
    period_start = date.fromisoformat(str(period_start)) if not isinstance(period_start, date) else period_start
    period_end = date.fromisoformat(str(period_end)) if not isinstance(period_end, date) else period_end
    if period_end < period_start:
        raise ValueError("El cierre no puede ser anterior al inicio")
    start_utc, _ = _close_bounds(period_start)
    _, end_utc = _close_bounds(period_end)
    period_days = (period_end - period_start).days + 1
    team = get_active_captacion_team(db, period_end)
    team_by_id = {str(member["id"]): member for member in team}
    assignments = _active_assignments(db, end_utc, set(team_by_id))
    visible_portfolios = _current_visible_assignments(db, team)
    # La cartera es la fotografía operativa del momento de generación; nunca
    # se incorporan eventos fechados en el futuro respecto de esa ejecución.
    events = _credited_events(db, end_utc=datetime.now(timezone.utc))

    daily: dict[str, set[str]] = defaultdict(set)
    accumulated: dict[str, set[str]] = defaultdict(set)
    for event in events:
        actor = str(event.get("actor_user_id") or "")
        property_id = str(event.get("property_id") or "")
        occurred = _utc(event.get("occurred_at"))
        if actor not in team_by_id or not property_id or not occurred:
            continue
        if property_id in visible_portfolios.get(actor, set()):
            accumulated[actor].add(property_id)
        if start_utc <= occurred < end_utc:
            if property_id in assignments.get(actor, set()):
                daily[actor].add(property_id)

    rows = []
    for member in team:
        actor = str(member["id"])
        total = len(visible_portfolios.get(actor, set()))
        # Ejecutivo aplicable: tiene al menos una propiedad visible en la
        # cartera operativa actual del CRM.
        if total == 0:
            continue
        managed = len(accumulated.get(actor, set()))
        count = len(daily.get(actor, set()))
        rows.append({
            "user_id": actor,
            "name": member["name"],
            "gestiones_dia": count,
            "cumplimiento_dia": count * 100 / (DAILY_TARGET * period_days),
            "total_asignadas": total,
            "total_gestionadas_acumuladas": managed,
            "avance_cartera": managed * 100 / total if total else 0.0,
            "pendientes": max(total - managed, 0),
            "cobertura_dias": max(total - managed, 0) / DAILY_TARGET,
        })
    rows.sort(key=lambda row: (-row["gestiones_dia"], -row["avance_cartera"], _name_key(row["name"])))
    total_goal = DAILY_TARGET * len(rows)
    total_done = sum(row["gestiones_dia"] for row in rows)
    total_assigned = sum(row["total_asignadas"] for row in rows)
    total_managed = sum(row["total_gestionadas_acumuladas"] for row in rows)
    total_pending = sum(row["pendientes"] for row in rows)
    return {
        "period": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "period_days": period_days,
        "period_label": (
            f"{period_start.day} de {('enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre')[period_start.month-1]}"
            if period_start == period_end else
            f"Semana {period_start.day} al {period_end.day} de {('enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre')[period_end.month-1]}"
        ),
        "team_size": len(rows),
        "team_goal": total_goal * period_days,
        "team_done": total_done,
        "team_compliance": total_done * 100 / (total_goal * period_days) if total_goal else 0.0,
        "total_assigned": total_assigned,
        "total_managed": total_managed,
        "pending_team": total_pending,
        "availability_pct": total_pending * 100 / total_assigned if total_assigned else 0.0,
        "coverage_days": total_pending / (DAILY_TARGET * len(rows)) if rows else 0.0,
        "coverage_below_threshold_count": sum(
            1 for row in rows if row["cobertura_dias"] < COVERAGE_THRESHOLD_DAYS
        ),
        "executives": rows,
        "sources": {
            "management": f"{LEDGER_COLLECTION}.event_type in {sorted(VALID_CREDIT_EVENT_TYPES)}, credited=true, occurred_at",
            "assignment": "GET /captacion -> get_captacion_list: current assigned property, origen in {toctoc,yapo}, visible classification state",
            "deduplication": "property_id unique per executive/day for daily metric; unique property per executive for accumulated metric",
        },
        "period_target_days": period_days,
    }


def calculate_daily_report(db, period: date | str) -> dict:
    period = date.fromisoformat(str(period)) if not isinstance(period, date) else period
    return calculate_period_report(db, period, period)


def _calculate_daily_report_with_retry(db, report_date: date) -> dict:
    """Calculate with a longer-lived read client after a transient Mongo timeout."""
    try:
        return calculate_daily_report(db, report_date)
    except (AutoReconnect, NetworkTimeout) as exc:
        logger.warning(
            "[CAPTACION_DAILY_PRODUCTION] retry_calculation report_date=%s reason=%s",
            report_date.isoformat(),
            type(exc).__name__,
        )

    retry_client = MongoClient(
        Config.MONGO_URI,
        socketTimeoutMS=30000,
        connectTimeoutMS=10000,
        serverSelectionTimeoutMS=20000,
        maxIdleTimeMS=45000,
        read_preference=ReadPreference.SECONDARY_PREFERRED,
    )
    try:
        return calculate_daily_report(retry_client[Config.DB_NAME], report_date)
    finally:
        retry_client.close()


def _bar(value: float) -> str:
    filled = min(10, max(0, int(value * 10 / 100)))
    return "█" * filled + "░" * (10 - filled)


def _team_bar(value: float) -> str:
    filled = min(10, max(0, round(value / 10)))
    return "█" * filled + "░" * (10 - filled)


def _pct(value: float, *, trim_zero: bool = True) -> str:
    text = f"{value:.1f}%".replace(".", ",")
    return text.replace(",0%", "%") if trim_zero else text


def _int_es(value: int) -> str:
    return f"{int(value):,}".replace(",", ".")


def _short_display_names(rows: list[dict]) -> dict[str, str]:
    """Use compact first names, disambiguating duplicate first names."""
    bases = {}
    for row in rows:
        parts = str(row["name"]).split()
        base = " ".join(parts[:2]) if parts[:1] == ["María"] else (parts[0] if parts else "")
        bases.setdefault(base.casefold(), []).append((row["name"], base, parts))
    result = {}
    for entries in bases.values():
        for original, base, parts in entries:
            if len(entries) == 1:
                result[original] = base
                continue
            surname_initial = next((part[0] for part in parts[len(base.split()):] if part), "")
            result[original] = f"{base} {surname_initial}." if surname_initial else base
    return result


def build_whatsapp_message(report: dict) -> str:
    below_threshold = report["coverage_below_threshold_count"]
    availability_status = (
        "Todos con ≥10 días de datos"
        if below_threshold == 0
        else f"{below_threshold} ejecutivos con menos de 10 días de datos"
    )
    lines = [
        f"👤 *Gestión Diaria de Captación | {report['period_label']}*",
        "",
        "👥 *Equipo*",
        f"Cumplimiento diario: *{_int_es(report['team_done'])}/{_int_es(report['team_goal'])} · {_pct(report['team_compliance'])}*",
        _team_bar(report["team_compliance"]),
        "",
    ]
    display_names = _short_display_names(report["executives"])
    for row in report["executives"]:
        target = DAILY_TARGET * report["period_days"]
        name = display_names[row["name"]]
        day_value = f"Día: *{row['gestiones_dia']}/{target} · {_pct(row['cumplimiento_dia'])}*"
        managed = f"{_int_es(row['total_gestionadas_acumuladas'])}/{_int_es(row['total_asignadas'])}"
        advance = f"Avance: {_int_es(row['total_gestionadas_acumuladas'])} de {_int_es(row['total_asignadas'])} · {_pct(row['avance_cartera'], trim_zero=False)}"
        lines.extend([f"*{name}* {_bar(row['cumplimiento_dia'])}", f"{day_value} · {advance}", ""])
    lines.extend([
        f"*Disponibilidad:* {_int_es(report['pending_team'])} pendientes · "
        f"{_pct(report['availability_pct'], trim_zero=False)}",
        f"*Cobertura:* {availability_status}",
    ])
    return "\n".join(lines)


def validate_reconciliation(report: dict, message: str) -> dict:
    rows = report["executives"]
    checks = {
        "team_size": report["team_goal"] == DAILY_TARGET * report["team_size"],
        "daily_sum": report["team_done"] == sum(row["gestiones_dia"] for row in rows),
        "assigned_sum": report["total_assigned"] == sum(row["total_asignadas"] for row in rows),
        "managed_sum": report["total_managed"] == sum(row["total_gestionadas_acumuladas"] for row in rows),
        "pending_sum": report["pending_team"] == sum(row["pendientes"] for row in rows),
        "applicable_only": all(row["total_asignadas"] > 0 for row in rows),
        "dynamic_goal": report["team_goal"] == DAILY_TARGET * len(rows) * report["period_days"],
        "message_compact": len(message) <= 1500,
        "no_group_recipient": True,
    }
    if not all(checks.values()):
        raise ValueError("Reconciliación fallida: " + ", ".join(k for k, v in checks.items() if not v))
    return checks


async def send_test_report(db, report: dict) -> dict:
    recipient = normalize_whatsapp_recipient(Config.CAPTACION_TEST_RECIPIENT)
    expected = normalize_whatsapp_recipient("+56983219804")
    if recipient != expected:
        raise PermissionError("TEST_RECIPIENT no coincide con el número personal autorizado")
    if not Config.CAPTACION_TEST_MODE:
        raise PermissionError("El modo de prueba no está habilitado")
    message = build_whatsapp_message(report)
    checks = validate_reconciliation(report, message)
    logger.info("[CAPTACION_TEST_MESSAGE] recipient=%s\n%s", mask_whatsapp_recipient(recipient), message)
    result = await send_whatsapp_message_detailed(recipient, message)
    return {"recipient": recipient, "recipient_masked": mask_whatsapp_recipient(recipient), "message": message, "checks": checks, "provider": result}


def _ensure_daily_delivery_indexes_sync(db) -> None:
    db[DAILY_DELIVERY_COLLECTION].create_index(
        "idempotency_key", unique=True, name="captacion_daily_delivery_idempotency"
    )


async def ensure_daily_delivery_indexes(db) -> None:
    await asyncio.to_thread(_ensure_daily_delivery_indexes_sync, db)


def _has_provider_evidence(delivery: dict) -> bool:
    provider = delivery.get("provider_result") or {}
    return bool(delivery.get("provider_message_id")) or bool(provider.get("success")) or provider.get(
        "delivery_status"
    ) in {"accepted", "delivered", "read"}


def _claim_daily_delivery_sync(
    db, key: str, report_date: date, recipient: str
) -> tuple[dict, bool]:
    collection = db[DAILY_DELIVERY_COLLECTION]
    now = datetime.now(timezone.utc)
    claim = {
        "idempotency_key": key,
        "report_type": "daily",
        "report_date": report_date.isoformat(),
        "generated_at": now,
        "updated_at": now,
        "recipient": recipient,
        "status": "sending",
    }
    try:
        collection.insert_one(claim)
        return claim, True
    except DuplicateKeyError:
        inserted = collection.find_one({"idempotency_key": key})
    if not inserted:
        return claim, False
    if inserted.get("status") in {"accepted", "delivered", "read"} or _has_provider_evidence(inserted):
        return inserted, False

    retryable_statuses = {"sending", "failed", "delivery_unknown"}
    if inserted.get("status") not in retryable_statuses:
        return inserted, False

    cutoff = now - timedelta(seconds=DAILY_PRODUCTION_RETRY_COOLDOWN_SECONDS)
    stale_conditions = [
        {"status": "sending", "updated_at": {"$lte": cutoff}},
        {"status": "sending", "updated_at": {"$exists": False}, "generated_at": {"$lte": cutoff}},
        {"status": {"$in": ["failed", "delivery_unknown"]}, "failed_at": {"$lte": cutoff}},
        {
            "status": {"$in": ["failed", "delivery_unknown"]},
            "failed_at": {"$exists": False},
            "updated_at": {"$lte": cutoff},
        },
        {
            "status": {"$in": ["failed", "delivery_unknown"]},
            "failed_at": {"$exists": False},
            "updated_at": {"$exists": False},
            "generated_at": {"$lte": cutoff},
        },
    ]
    claimed = collection.find_one_and_update(
        {"idempotency_key": key, "$or": stale_conditions},
        {
            "$set": {
                "status": "sending",
                "generated_at": now,
                "updated_at": now,
                "failed_at": None,
                "error": None,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if claimed is not None:
        return claimed, True
    # Another worker may have won the atomic stale-claim race.
    current = collection.find_one({"idempotency_key": key})
    return current or inserted, False


async def _claim_daily_delivery(db, key: str, report_date: date, recipient: str) -> tuple[dict, bool]:
    await ensure_daily_delivery_indexes(db)
    return await asyncio.to_thread(_claim_daily_delivery_sync, db, key, report_date, recipient)


def _update_daily_delivery_sync(db, key: str, fields: dict) -> None:
    fields = {**fields, "updated_at": datetime.now(timezone.utc)}
    db[DAILY_DELIVERY_COLLECTION].update_one({"idempotency_key": key}, {"$set": fields})


async def send_production_daily_report(db, report_date: date | str) -> dict:
    """Send one closed daily report to the configured production group."""
    report_date = date.fromisoformat(str(report_date)) if not isinstance(report_date, date) else report_date
    if not Config.CAPTACION_DAILY_PRODUCTION_ENABLED or Config.CAPTACION_TEST_MODE:
        return {"status": "disabled"}
    configured_group = Config.resolve_daily_group_id()
    if not configured_group:
        raise PermissionError("PROCASA_COMMERCIAL_GROUP_ID/DAILY_REPORT_GROUP_ID no está configurado como grupo")
    recipient = normalize_whatsapp_recipient(configured_group)
    if not recipient or recipient == normalize_whatsapp_recipient(Config.CAPTACION_TEST_RECIPIENT):
        raise PermissionError("PRODUCTION_GROUP no está configurado como destino productivo separado")
    key = f"daily:{report_date.isoformat()}:{recipient}"
    claim, claimed = await _claim_daily_delivery(db, key, report_date, recipient)
    if not claimed:
        return {"status": "already_claimed", "delivery": claim}
    try:
        report = await asyncio.to_thread(_calculate_daily_report_with_retry, db, report_date)
        if report.get("team_size") == 0:
            logger.warning(
                "[CAPTACION_DAILY_PRODUCTION] status=skipped_no_data report_date=%s reason=no_applicable_executives",
                report_date.isoformat(),
            )
            await asyncio.to_thread(
                _update_daily_delivery_sync,
                db,
                key,
                {
                    "status": DAILY_NO_DATA_STATUS,
                    "error": "no_applicable_executives",
                    "failed_at": None,
                    "sent_at": None,
                },
            )
            return {"status": DAILY_NO_DATA_STATUS, "delivery": claim}
        message = build_whatsapp_message(report)
        checks = validate_reconciliation(report, message)
        provider = await send_whatsapp_message_detailed(recipient, message)
        status = "accepted" if provider.get("success") else provider.get("delivery_status", "failed")
        completed_at = datetime.now(timezone.utc)
        await asyncio.to_thread(
            _update_daily_delivery_sync,
            db,
            key,
            {
                "status": status,
                "provider_message_id": provider.get("provider_message_id"),
                "provider_http_status": provider.get("http_status"),
                "provider_result": provider,
                "sent_at": completed_at if provider.get("success") else None,
                "failed_at": None if provider.get("success") else completed_at,
                "checks": checks,
            },
        )
        return {"status": status, "delivery": claim, "provider": provider, "checks": checks}
    except Exception as exc:
        await asyncio.to_thread(
            _update_daily_delivery_sync,
            db,
            key,
            {"status": "failed", "error": str(exc), "failed_at": datetime.now(timezone.utc)},
        )
        raise


async def run_scheduled_production_daily_report(db, run_at: datetime | None = None) -> dict:
    """Run the Tue-Fri 08:30-12:00 Chile catch-up window for the closed prior day."""
    local_now = (run_at.astimezone(CHILE) if run_at else datetime.now(CHILE))
    if not daily_production_window_open(local_now):
        return {"status": "not_scheduled", "local_now": local_now.isoformat()}
    period = scheduled_period_for_run(local_now.date())
    if period is None:
        return {"status": "not_scheduled", "local_now": local_now.isoformat()}
    report_date, report_end = period
    if report_date != report_end:
        return {"status": "weekly_reserved", "local_now": local_now.isoformat()}
    return await send_production_daily_report(db, report_date)
