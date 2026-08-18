"""Reporte semanal de Captaciones construido desde el backend de /captacion."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone

import pytz
from openai import OpenAI
from pymongo.errors import DuplicateKeyError

from captacion_goals import get_captacion_goal_dashboard, get_captacion_management_rows
from captacion_management import (
    OUTCOME_COMMUNICATION_LABELS,
    OUTCOME_GROUPS,
    summarize_grouped_outcomes,
)
from captacion_workforce import DEFAULT_TIMEZONE, get_active_captacion_team
from config import Config
from .captacion_daily_report import (
    DAILY_TARGET,
    _active_assignments,
    _close_bounds,
    _current_visible_assignments,
    _name_key as daily_name_key,
    calculate_period_report,
)
from .storage import get_db
from .whatsapp_client import (
    mask_whatsapp_recipient,
    normalize_whatsapp_recipient,
    normalize_provider_status,
    send_whatsapp_message_detailed,
    wait_for_whatsapp_delivery,
)


logger = logging.getLogger(__name__)
CHILE = pytz.timezone(DEFAULT_TIMEZONE)
SCHEMA_VERSION = "captacion_weekly_report_v3"
ADMIN_RECIPIENT = "+56983219804"
PROMPT_VERSION = Config.CAPTACION_WEEKLY_PROMPT_VERSION
REPORT_COLLECTION = Config.CAPTACION_WEEKLY_REPORT_COLLECTION
DELIVERY_COLLECTION = Config.CAPTACION_WEEKLY_DELIVERY_COLLECTION
MESSAGE_DOMAIN = "captacion_weekly_report"
RESPONSIBLE_SERVICE = "captacion_weekly_report_scheduler"
WEEKLY_SCHEDULE_HOUR = 8
WEEKLY_SCHEDULE_MINUTE = 30
WEEKLY_RECOVERY_DEADLINE_HOUR = 12
WEEKDAYS = tuple(range(5))

OUTCOME_LABELS = {
    "por_contactar": "Por contactar",
    "en_gestion": "En gestión",
    "no_respondio": "No respondió",
    "ocupado": "Ocupado",
    "numero_invalido": "Número inválido",
    "contactado": "Contactado",
    "solicita_llamada_posterior": "Solicita llamada posterior",
    "mensaje_enviado": "Mensaje enviado",
    "corredor": "Corredor",
    "descartado": "Descartado",
    "captado": "Captado",
    "otros": "Otro resultado",
}
NARRATIVE_FIELDS = ("intro", "insight", "weekly_focus", "closing")

WEEKLY_WRITER_PROMPT = """Eres el redactor del reporte semanal interno de Captaciones de PROCASA.

Recibirás métricas calculadas y validadas mediante la misma fuente utilizada por el CRM.

Reglas:

- No inventes datos.
- No recalcules métricas.
- No modifiques cifras.
- No confundas eventos del historial con propiedades gestionadas.
- Una propiedad puede tener múltiples eventos, pero cuenta una vez según la regla de deduplicación.
- Diferencia propiedades gestionadas, intentos, contactos efectivos y captaciones.
- No declares cumplimiento si la meta comercial todavía no está confirmada.
- No hagas comparaciones si el periodo anterior no es comparable.
- No expongas negativamente a ejecutivos.
- No incluyas datos personales.
- No incluyas cifras ni dígitos en la narración; el backend agrega todas las cifras.
- No decidas agrupaciones, resultados ni foco operativo.
- La introducción debe ser neutral y tener máximo ciento veinte caracteres.
- El cierre debe tener máximo noventa caracteres.
- Usa español de Chile, tono profesional, cercano y motivador.
- Devuelve exclusivamente JSON válido con intro, insight, weekly_focus y closing.
"""


def ensure_weekly_report_indexes(db) -> None:
    db[REPORT_COLLECTION].create_index("report_id", unique=True, name="captacion_weekly_report_id")
    db[REPORT_COLLECTION].create_index(
        [("period_start", 1), ("period_end", 1), ("is_test", 1), ("created_at", -1)],
        name="captacion_weekly_period",
    )
    db[DELIVERY_COLLECTION].create_index(
        "idempotency_key", unique=True, name="captacion_weekly_delivery_idempotency"
    )


async def _claim_delivery(db, idempotency_key: str, fields: dict) -> tuple[dict, bool]:
    existing = await asyncio.to_thread(
        db[DELIVERY_COLLECTION].find_one, {"idempotency_key": idempotency_key}, {"_id": 0}
    )
    if existing:
        return existing, False
    claim = {
        "delivery_id": str(uuid.uuid4()),
        "idempotency_key": idempotency_key,
        "delivery_status": "sending",
        "created_at": datetime.now(timezone.utc),
        **fields,
    }
    try:
        await asyncio.to_thread(db[DELIVERY_COLLECTION].insert_one, claim)
        claim.pop("_id", None)
        return claim, True
    except DuplicateKeyError:
        existing = await asyncio.to_thread(
            db[DELIVERY_COLLECTION].find_one, {"idempotency_key": idempotency_key}, {"_id": 0}
        )
        return existing or claim, False


async def _complete_delivery(db, delivery: dict) -> dict:
    payload = {key: value for key, value in delivery.items() if key != "_id"}
    await asyncio.to_thread(
        db[DELIVERY_COLLECTION].update_one,
        {"idempotency_key": delivery["idempotency_key"]},
        {"$set": payload},
    )
    return payload


def _parse_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _period_label(start: date, end: date) -> str:
    months = (
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    )
    if start.month == end.month and start.year == end.year:
        return f"{start.day} al {end.day} de {months[end.month - 1]} de {end.year}"
    return (
        f"{start.day} de {months[start.month - 1]} de {start.year} al "
        f"{end.day} de {months[end.month - 1]} de {end.year}"
    )


def _panel_now(period_end: date) -> datetime:
    return CHILE.localize(datetime.combine(period_end, time(18, 59)))


def _validate_period(start: date, end: date) -> None:
    if start.weekday() != 0 or end.weekday() != 4 or end - start != timedelta(days=4):
        raise ValueError("El periodo semanal debe abarcar de lunes a viernes")


def weekly_production_window_open(run_at: datetime | None = None) -> bool:
    """Lunes 08:30 inclusive hasta antes de 12:00, hora de Chile."""
    local_now = run_at.astimezone(CHILE) if run_at else datetime.now(CHILE)
    if local_now.weekday() != 0:
        return False
    start = local_now.replace(hour=WEEKLY_SCHEDULE_HOUR, minute=WEEKLY_SCHEDULE_MINUTE, second=0, microsecond=0)
    end = local_now.replace(hour=WEEKLY_RECOVERY_DEADLINE_HOUR, minute=0, second=0, microsecond=0)
    return start <= local_now < end


def _safe_executives(panel: dict) -> list[dict]:
    rows = [
        {
            "name": row.get("name") or "Sin nombre",
            "properties_managed_unique": int(row.get("week_count") or 0),
            "effective_contacts_unique": int(row.get("effective_contacts") or 0),
            "captured_properties_unique": int(row.get("captures") or 0),
            "properties_with_contact_attempt_unique": int(row.get("contact_attempts") or 0),
        }
        for row in panel.get("executives") or []
    ]
    return rows


def derive_operational_priority(snapshot: dict) -> dict:
    groups = snapshot["outcome_groups"]
    pending = groups["pending_next_action"]["total"]
    details = snapshot.get("detailed_outcomes") or {}
    contacts = snapshot["team"]["effective_contacts_unique"]
    captures = snapshot["team"]["captured_properties_unique"]
    if pending:
        return {"key": "pending_follow_up", "label": "Priorizar pendientes y contactos sin respuesta", "supporting_total": pending}
    if details.get("corredor"):
        return {"key": "initial_filtering", "label": "Reforzar el filtrado y la clasificación inicial", "supporting_total": details["corredor"]}
    stale = details.get("propiedad_no_disponible", 0) + details.get("publicacion_expirada", 0)
    if stale:
        return {"key": "listing_freshness", "label": "Revisar antigüedad y vigencia de las propiedades", "supporting_total": stale}
    if contacts > captures:
        return {"key": "commercial_proposal", "label": "Reforzar la propuesta comercial después del contacto", "supporting_total": contacts - captures}
    if captures:
        return {"key": "sustain_captures", "label": "Sostener las prácticas que permitieron captar", "supporting_total": captures}
    return {"key": "consistent_recording", "label": "Mantener seguimiento y registro comercial consistente", "supporting_total": 0}


def validate_crm_parity(snapshot: dict, panel: dict) -> dict:
    report_total = int(snapshot["team"]["properties_managed_unique"])
    panel_total = int(panel.get("week_count") or 0)
    report_exec = {
        row["name"]: (
            row["properties_managed_unique"],
            row["effective_contacts_unique"],
            row["captured_properties_unique"],
        )
        for row in snapshot["executives"]
    }
    panel_exec = {
        row.get("name"): (
            int(row.get("week_count") or 0),
            int(row.get("effective_contacts") or 0),
            int(row.get("captures") or 0),
        )
        for row in panel.get("executives") or []
    }
    validated = (
        report_total == panel_total
        and report_exec == panel_exec
        and sum(group["total"] for group in snapshot["outcome_groups"].values()) == report_total
        and all(sum(group["details"].values()) == group["total"] for group in snapshot["outcome_groups"].values())
        and snapshot["team"]["properties_with_contact_attempt_unique"] == int(panel.get("contact_attempts") or 0)
        and snapshot["team"]["effective_contacts_unique"] == int(panel.get("effective_contacts") or 0)
        and snapshot["team"]["captured_properties_unique"] == int(panel.get("captures") or 0)
    )
    result = {
        "validated": validated,
        "panel_properties_managed": panel_total,
        "report_properties_managed": report_total,
    }
    if not validated:
        raise ValueError("Paridad CRM fallida; el reporte semanal fue abortado")
    return result


def build_weekly_snapshot(db, period_start, period_end, *, is_test: bool) -> dict:
    start = _parse_date(period_start)
    end = _parse_date(period_end)
    _validate_period(start, end)

    # Esta es la misma función que usa la ruta /captacion.
    panel = get_captacion_goal_dashboard(db, now=_panel_now(end))
    panel_dates = [item.get("date") for row in panel.get("executives") or [] for item in row.get("daily") or []]
    expected_dates = {(start + timedelta(days=index)).isoformat() for index in range(5)}
    if panel_dates and set(panel_dates) != expected_dates:
        raise ValueError("El backend de /captacion resolvió un periodo distinto al solicitado")

    panel_groups = panel.get("outcome_groups") or {}
    outcome_groups = {}
    for key, label in OUTCOME_GROUPS.items():
        source = panel_groups.get(key) or {}
        outcome_groups[key] = {
            "label": source.get("label") or label,
            "total": int(source.get("total") or 0),
            "details": {name: int(value or 0) for name, value in (source.get("details") or {}).items()},
        }
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "report": {
            "is_test": bool(is_test),
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "period_label": _period_label(start, end),
            "timezone_label": "Hora de Chile",
        },
        "measurement": {
            "main_metric": "propiedades gestionadas",
            "deduplication": "una propiedad por ejecutivo y día",
            "same_source_as_crm": True,
            "commercial_goal_confirmed": False,
        },
        "team": {
            "properties_managed_unique": int(panel.get("week_count") or 0),
            "properties_with_contact_attempt_unique": int(panel.get("contact_attempts") or 0),
            "effective_contacts_unique": int(panel.get("effective_contacts") or 0),
            "captured_properties_unique": int(panel.get("captures") or 0),
        },
        "outcome_groups": outcome_groups,
        "detailed_outcomes": {key: int(value or 0) for key, value in (panel.get("detailed_outcomes") or panel.get("final_outcomes") or {}).items()},
        "detail_labels": dict(panel.get("detail_labels") or {}),
        "requires_outcome_review": bool(panel.get("requires_outcome_review")),
        "executives": _safe_executives(panel),
        "data_quality": {
            "historical_measurement_complete": start >= date.fromisoformat("2026-07-20"),
            "limitations": [] if start >= date.fromisoformat("2026-07-20") else [
                "El periodo corresponde a la transición previa al corte formal del ledger del 20 de julio de 2026.",
                "Un valor cero solo representa eventos acreditados con las reglas actuales, no ausencia total de trabajo.",
            ],
        },
        "crm_parity": {},
        "administrative": {
            "history_events_registered": int(panel.get("history_event_count") or 0),
            "history_events_label": "Eventos registrados en el historial",
            "technical_data": True,
        },
    }
    snapshot["operational_priority"] = derive_operational_priority(snapshot)
    snapshot["crm_parity"] = validate_crm_parity(snapshot, panel)
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    snapshot["snapshot_id"] = "cws_" + hashlib.sha256(canonical).hexdigest()[:24]
    return snapshot


def _es_pct(value: float, *, trim_zero: bool = True) -> str:
    text = f"{value:.1f}%".replace(".", ",")
    return text.replace(",0%", "%") if trim_zero else text


def _es_int(value: int) -> str:
    return f"{int(value):,}".replace(",", ".")


def _weekly_bar(value: float) -> str:
    filled = min(10, max(0, round(value / 10)))
    return "█" * filled + "░" * (10 - filled)


def _weekly_display_names(rows: list[dict]) -> dict[str, str]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        parts = str(row["name"]).split()
        base = " ".join(parts[:2]) if parts[:1] == ["María"] else (parts[0] if parts else "")
        grouped.setdefault(base.casefold(), []).append({"row": row, "base": base, "parts": parts})
    result = {}
    for entries in grouped.values():
        for item in entries:
            if len(entries) == 1:
                result[item["row"]["name"]] = item["base"]
            else:
                suffix = next((part[0] for part in item["parts"][len(item["base"].split()):] if part), "")
                result[item["row"]["name"]] = f"{item['base']} {suffix}." if suffix else item["base"]
    return result


def _weekly_applicability(db, start: date, end: date) -> dict[str, dict[str, int]]:
    """Reconstruct applicable days from active membership and assignment history.

    Limitation: historical classification visibility is not versioned. Applicability
    therefore uses active assignment history at each day close, while the advance
    denominator uses the current visible CRM population separately.
    """
    applicable: dict[str, dict[str, int]] = {}
    current = start
    while current <= end:
        team = get_active_captacion_team(db, current)
        close_utc = _close_bounds(current)[1]
        assignments = _active_assignments(db, close_utc, {str(member["id"]) for member in team})
        for member in team:
            user_id = str(member["id"])
            if assignments.get(user_id):
                applicable.setdefault(user_id, {})[current.isoformat()] = len(assignments[user_id])
        current += timedelta(days=1)
    return applicable


def build_operational_weekly_snapshot(db, period_start, period_end, *, is_test: bool) -> dict:
    """Build the deterministic weekly report using the approved daily semantics."""
    start = _parse_date(period_start)
    end = _parse_date(period_end)
    _validate_period(start, end)
    base = build_weekly_snapshot(db, start, end, is_test=is_test)

    applicability = _weekly_applicability(db, start, end)
    current_report = calculate_period_report(db, start, end)
    current_rows = {str(row["user_id"]): row for row in current_report["executives"]}
    current_team = get_active_captacion_team(db, end)
    names = {str(member["id"]): member["name"] for member in current_team}

    # The existing dashboard already resolves canonical outcomes from the ledger.
    # Reuse those outcome groups, but calculate daily/week totals independently.
    daily_sets: dict[str, dict[str, set[str]]] = {}
    for row in get_captacion_management_rows_for_week(db, start, end):
        actor = str(row.get("actor_user_id") or "").strip()
        property_id = str(row.get("property_id") or "").strip()
        local_date = str(row.get("local_date") or "")
        if actor and property_id and local_date in {
            (start + timedelta(days=index)).isoformat() for index in WEEKDAYS
        } and row.get("credited"):
            daily_sets.setdefault(actor, {}).setdefault(local_date, set()).add(property_id)
            names.setdefault(actor, str(row.get("actor") or actor))

    rows = []
    for user_id, current in current_rows.items():
        applicable_by_day = applicability.get(user_id, {})
        applicable_days = sorted(applicable_by_day)
        if not applicable_days:
            continue
        daily_counts = [len(daily_sets.get(user_id, {}).get(day.isoformat(), set())) for day in (start + timedelta(days=index) for index in WEEKDAYS)]
        week_done = sum(daily_counts)
        week_goal = DAILY_TARGET * len(applicable_days)
        days_met = sum(1 for count, day in zip(daily_counts, (start + timedelta(days=index) for index in WEEKDAYS)) if day.isoformat() in applicable_days and count >= DAILY_TARGET)
        rows.append({
            "user_id": user_id,
            "name": names.get(user_id, current["name"]),
            "daily_counts": daily_counts,
            "daily_assigned_counts": [applicable_by_day.get((start + timedelta(days=index)).isoformat(), 0) for index in WEEKDAYS],
            "gestiones_semana": week_done,
            "meta_semana": week_goal,
            "cumplimiento_semana": week_done * 100 / week_goal if week_goal else 0.0,
            "dias_cumplidos": days_met,
            "dias_aplicables": len(applicable_days),
            "propiedades_unicas_semana": len(set().union(*(daily_sets.get(user_id, {}).get(day.isoformat(), set()) for day in (start + timedelta(days=index) for index in WEEKDAYS)))),
            "total_asignadas": current["total_asignadas"],
            "total_gestionadas_acumuladas": current["total_gestionadas_acumuladas"],
            "avance_cartera": current["avance_cartera"],
            "pendientes": current["pendientes"],
            "cobertura_dias": current["cobertura_dias"],
        })

    rows.sort(key=lambda row: (-row["gestiones_semana"], -row["dias_cumplidos"], -row["avance_cartera"], daily_name_key(row["name"])))
    total_goal = sum(row["meta_semana"] for row in rows)
    total_done = sum(row["gestiones_semana"] for row in rows)
    total_assigned = sum(row["total_asignadas"] for row in rows)
    total_managed = sum(row["total_gestionadas_acumuladas"] for row in rows)
    pending = sum(row["pendientes"] for row in rows)
    base["executives"] = rows
    base["weekly_operational"] = {
        "team_done": total_done,
        "team_goal": total_goal,
        "team_compliance": total_done * 100 / total_goal if total_goal else 0.0,
        "team_days_met": sum(row["dias_cumplidos"] for row in rows),
        "team_days_applicable": sum(row["dias_aplicables"] for row in rows),
        "total_assigned": total_assigned,
        "total_managed": total_managed,
        "pending": pending,
        "availability_pct": pending * 100 / total_assigned if total_assigned else 0.0,
        "coverage_below_threshold_count": sum(row["cobertura_dias"] < 10 for row in rows),
        "current_population_source": "GET /captacion -> origen toctoc/yapo + classification.state visible",
        "applicability_source": "membership active by day + assignment history at daily close",
        "deduplication": "property unique per executive per day; weekly total is sum of daily units",
        "unique_week_properties": sum(row["propiedades_unicas_semana"] for row in rows),
    }
    base["schema_version"] = "captacion_weekly_mobile_v1"
    base["snapshot_id"] = "cws_" + hashlib.sha256(json.dumps(base, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]
    return base


def get_captacion_management_rows_for_week(db, start: date, end: date) -> list[dict]:
    """Read the same canonical weekly ledger rows used by the dashboard."""
    rows = get_captacion_management_rows(db, now=_panel_now(end))
    valid_dates = {(start + timedelta(days=index)).isoformat() for index in WEEKDAYS}
    return [row for row in rows if str(row.get("local_date") or "") in valid_dates]


def build_deepseek_payload(snapshot: dict) -> dict:
    payload = {
        key: deepcopy(snapshot[key])
        for key in (
            "schema_version", "report", "measurement", "team", "outcome_groups",
            "executives", "data_quality", "crm_parity", "operational_priority",
        )
    }
    forbidden = {
        "phone", "telefono", "email", "correo", "address", "direccion", "owner",
        "propietario", "comments", "comentarios", "property_id", "user_id", "event_id",
    }

    def inspect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.casefold() in forbidden:
                    raise ValueError(f"Campo no permitido en payload de DeepSeek: {key}")
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(payload)
    return payload


def validate_narrative(value: dict, *, allow_digits: bool = False) -> dict:
    if not isinstance(value, dict) or set(value) != set(NARRATIVE_FIELDS):
        raise ValueError("La narración debe contener exactamente intro, insight, weekly_focus y closing")
    clean = {key: str(value.get(key) or "").strip() for key in NARRATIVE_FIELDS}
    if any(not text for text in clean.values()):
        raise ValueError("La narración contiene una sección vacía")
    if not allow_digits and any(re.search(r"\d", text) for text in clean.values()):
        raise ValueError("La narración no puede introducir cifras")
    if len(clean["intro"]) > 120 or len(clean["closing"]) > 90:
        raise ValueError("La introducción o el cierre exceden el largo permitido")
    return clean


def _extract_json_object(raw: str) -> dict:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    return json.loads(text)


def generate_narrative(snapshot: dict) -> tuple[dict, str]:
    payload = build_deepseek_payload(snapshot)
    client = OpenAI(api_key=Config.DEEPSEEK_API_KEY, base_url=Config.DEEPSEEK_BASE_URL)
    model = Config.DEEPSEEK_MODEL_FAST
    last_error = None
    messages = [
        {"role": "system", "content": WEEKLY_WRITER_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]
    for attempt in (1, 2, 3):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.1,
            max_tokens=700,
            timeout=Config.DEEPSEEK_TIMEOUT_FAST,
            response_format={"type": "json_object"},
        )
        try:
            raw = response.choices[0].message.content
            return validate_narrative(_extract_json_object(raw)), model
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.warning("[CAPTACION_WEEKLY] invalid_narrative attempt=%s", attempt)
            messages.extend([
                {"role": "assistant", "content": response.choices[0].message.content or "{}"},
                {
                    "role": "user",
                    "content": (
                        "La salida anterior fue rechazada. Reescribe las cuatro secciones sin usar ningún "
                        "dígito, porcentaje, cifra ni cantidad explícita. La introducción debe ser neutral y "
                        "tener máximo ciento veinte caracteres; el cierre, máximo noventa. Devuelve solo el JSON solicitado."
                    ),
                },
            ])
    raise ValueError("DeepSeek no devolvió una narración segura") from last_error


def generate_narrative_with_fallback(snapshot: dict) -> tuple[dict, str, str]:
    try:
        narrative, model = generate_narrative(snapshot)
        return narrative, model, "DeepSeek"
    except Exception:
        logger.exception("[CAPTACION_WEEKLY] using_deterministic_narrative_fallback")
        narrative = {
            "intro": "Compartimos el resumen de las gestiones acreditadas durante el periodo.",
            "insight": "El detalle fue calculado y validado directamente por el CRM.",
            "weekly_focus": "El foco operativo fue determinado por el backend.",
            "closing": "¡Buen inicio de semana! 💪",
        }
        return validate_narrative(narrative), "deterministic_fallback", "fallback"


def _deterministic_focus_message(snapshot: dict) -> str:
    details = snapshot.get("detailed_outcomes") or {}
    if snapshot["operational_priority"]["key"] == "pending_follow_up":
        return (
            f"Contactar las *{details.get('por_contactar', 0)} propiedades pendientes* y retomar las "
            f"*{details.get('no_respondio', 0)} sin respuesta*, registrando siempre el resultado en el CRM."
        )
    return snapshot["operational_priority"]["label"] + ", registrando cada resultado en el CRM."


def assemble_operational_weekly_message(snapshot: dict) -> str:
    op = snapshot["weekly_operational"]
    period = re.sub(r" de \d{4}$", "", snapshot["report"]["period_label"])
    rows = snapshot["executives"]
    display_names = _weekly_display_names(rows)
    result_groups = snapshot["outcome_groups"]
    result_parts = [
        f"{_es_int(result_groups['captured']['total'])} captadas",
        f"{_es_int(result_groups['closed_without_capture']['total'])} cerradas",
        f"{_es_int(result_groups['management_in_progress']['total'])} en gestión",
    ]
    pending_total = result_groups["pending_next_action"]["total"]
    if pending_total:
        result_parts.append(f"{_es_int(pending_total)} pendientes")
    other_total = result_groups["other_review"]["total"]
    if other_total:
        result_parts.append(f"{_es_int(other_total)} otros")
    coverage = (
        "Todos con ≥10 días de datos"
        if not op["coverage_below_threshold_count"]
        else f"{op['coverage_below_threshold_count']} ejecutivos con menos de 10 días de datos"
    )
    lines = [
        f"👤 *Gestión Semanal de Captación | {period}*",
        "",
        "👥 *Equipo*",
        f"Cumplimiento semanal: *{_es_int(op['team_done'])}/{_es_int(op['team_goal'])} · {_es_pct(op['team_compliance'])}*",
        _weekly_bar(op["team_compliance"]),
        "",
        "*Resultados de la semana*",
        " · ".join(result_parts[:2]),
        " · ".join(result_parts[2:]),
        "",
    ]
    for row in rows:
        lines.extend([
            f"*{display_names[row['name']]}* {_weekly_bar(row['cumplimiento_semana'])}",
            f"Semana: *{_es_int(row['gestiones_semana'])}/{_es_int(row['meta_semana'])} · {_es_pct(row['cumplimiento_semana'])}* · "
            f"Días cumplidos: *{row['dias_cumplidos']}/{row['dias_aplicables']}*",
            f"Avance actual: {_es_int(row['total_gestionadas_acumuladas'])} de {_es_int(row['total_asignadas'])} · {_es_pct(row['avance_cartera'], trim_zero=False)}",
            "",
        ])
    lines.extend([
        f"*Disponibilidad actual:* {_es_int(op['pending'])} pendientes · {_es_pct(op['availability_pct'])}",
        f"*Cobertura:* {coverage}",
    ])
    return "\n".join(lines)


def assemble_whatsapp_message(snapshot: dict, narrative: dict) -> str:
    if snapshot.get("weekly_operational"):
        return assemble_operational_weekly_message(snapshot)
    narrative = validate_narrative(narrative, allow_digits=True)
    period = snapshot["report"]["period_label"]
    team = snapshot["team"]
    lines = [
        "🧪 *PRUEBA INTERNA — CAPTACIONES*" if snapshot["report"]["is_test"] else "📊 *CAPTACIONES | INICIO DE SEMANA*",
        f"🗓️ *Resumen del {period}*",
        "",
        f"La semana pasada quedaron registradas *{team['properties_managed_unique']} propiedades gestionadas* por el equipo.",
        "",
        "📋 *Estado al cierre*",
    ]
    nonzero_groups = [group for group in snapshot["outcome_groups"].values() if group["total"]]
    for group in nonzero_groups:
        details = []
        for key, value in group["details"].items():
            if value:
                label = OUTCOME_COMMUNICATION_LABELS.get(key) or snapshot.get("detail_labels", {}).get(key) or key.replace("_", " ")
                details.append((value, label.casefold()))
        if len(nonzero_groups) == 1:
            lines.extend(f"• *{value}* {label}" for value, label in details)
        else:
            lines.append(f"• {group['label']}: *{group['total']}*")
            if details:
                lines.append(f"  _{' · '.join(f'{value} {label}' for value, label in details)}_")
    lines.extend(["", "👥 *Gestión por ejecutiva*"])
    for row in snapshot["executives"]:
        managed = row["properties_managed_unique"]
        if not managed and not snapshot["data_quality"]["historical_measurement_complete"]:
            lines.append(f"• {row['name']}: _sin gestiones registradas en el período_")
        else:
            noun = "gestionada" if managed == 1 else "gestionadas"
            lines.append(f"• {row['name']}: *{managed} {noun}*")
    lines.extend([
        "",
        "🎯 *Foco de esta semana*",
        _deterministic_focus_message(snapshot),
    ])
    if not snapshot["data_quality"]["historical_measurement_complete"]:
        lines.extend([
            "",
            "_Este período corresponde al inicio de la nueva medición y podría no incluir gestiones que no quedaron registradas con las reglas actuales._",
        ])
    lines.extend(["", narrative["closing"]])
    if snapshot["report"]["is_test"]:
        lines.extend(["", "_Prueba enviada únicamente al administrador._"])
    return "\n".join(lines)


def validate_test_preview(snapshot: dict, message: str, *, expected_snapshot_id: str | None = None) -> dict:
    groups = snapshot["outcome_groups"]
    pending = groups["pending_next_action"]
    checks = {
        "snapshot_id": not expected_snapshot_id or snapshot.get("snapshot_id") == expected_snapshot_id,
        "properties_managed": snapshot["team"]["properties_managed_unique"] == 8,
        "pending_next_action": pending["total"] == 8,
        "por_contactar": pending["details"].get("por_contactar") == 5,
        "no_respondio": pending["details"].get("no_respondio") == 3,
        "otros_por_revisar": groups["other_review"]["total"] == 0,
        "group_sum": sum(group["total"] for group in groups.values()) == 8,
        "executives_unique": len(snapshot["executives"]) == len({row["name"] for row in snapshot["executives"]}),
        "message_length": len(message) <= 1100,
        "transitional_note_once": message.count("inicio de la nueva medición") == 1,
        "no_owner_pii": not any(term in message.casefold() for term in ("propietario:", "teléfono:", "dirección:", "correo:")),
    }
    if not all(checks.values()):
        failed = ", ".join(key for key, passed in checks.items() if not passed)
        raise ValueError(f"Validación de prueba fallida: {failed}")
    return checks


def validate_official_report(snapshot: dict, message: str) -> dict:
    if snapshot.get("weekly_operational"):
        op = snapshot["weekly_operational"]
        rows = snapshot["executives"]
        checks = {
            "crm_parity": bool(snapshot.get("crm_parity", {}).get("validated")),
            "daily_sum": op["team_done"] == sum(row["gestiones_semana"] for row in rows),
            "goal_sum": op["team_goal"] == sum(row["meta_semana"] for row in rows),
            "assigned_sum": op["total_assigned"] == sum(row["total_asignadas"] for row in rows),
            "managed_sum": op["total_managed"] == sum(row["total_gestionadas_acumuladas"] for row in rows),
            "pending_sum": op["pending"] == sum(row["pendientes"] for row in rows),
            "no_zero_applicable": all(row["dias_aplicables"] > 0 and row["meta_semana"] > 0 for row in rows),
            "no_other_review": snapshot["outcome_groups"]["other_review"]["total"] == 0,
            "message_length": len(message) <= 1500,
            "official_header": "GESTIÓN SEMANAL DE CAPTACIÓN" in message and "PRUEBA" not in message,
        }
        if not all(checks.values()):
            failed = ", ".join(key for key, passed in checks.items() if not passed)
            raise ValueError(f"Validación semanal móvil fallida: {failed}")
        return checks
    groups = snapshot["outcome_groups"]
    checks = {
        "crm_parity": bool(snapshot.get("crm_parity", {}).get("validated")),
        "group_sum": sum(group["total"] for group in groups.values()) == snapshot["team"]["properties_managed_unique"],
        "details_sum": all(sum(group["details"].values()) == group["total"] for group in groups.values()),
        "no_other_review": groups["other_review"]["total"] == 0,
        "executives_unique": len(snapshot["executives"]) == len({row["name"] for row in snapshot["executives"]}),
        "message_length": len(message) <= 1100,
        "no_owner_pii": not any(term in message.casefold() for term in ("propietario:", "teléfono:", "dirección:", "correo:")),
        "official_header": "CAPTACIONES | INICIO DE SEMANA" in message and "PRUEBA" not in message,
    }
    if not all(checks.values()):
        failed = ", ".join(key for key, passed in checks.items() if not passed)
        raise ValueError(f"Validación oficial fallida: {failed}")
    return checks


async def create_weekly_report(period_start, period_end, *, is_test: bool, created_by="system") -> dict:
    db = get_db()
    await asyncio.to_thread(ensure_weekly_report_indexes, db)
    snapshot = await asyncio.to_thread(
        build_operational_weekly_snapshot, db, period_start, period_end, is_test=is_test
    )
    if not snapshot["crm_parity"]["validated"]:
        raise ValueError("Paridad CRM no validada")
    narrative = {"intro": "", "insight": "", "weekly_focus": "", "closing": ""}
    model = None
    narrative_source = "deterministic"
    message = assemble_operational_weekly_message(snapshot)
    now = datetime.now(timezone.utc)
    document = {
        "report_id": str(uuid.uuid4()),
        "report_type": "captacion_weekly_preview" if is_test else "captacion_weekly_official",
        "message_domain": MESSAGE_DOMAIN,
        "message_type": "weekly_preview" if is_test else "weekly_report",
        "recipient_role": "administrator" if is_test else "captacion_team",
        "responsible_service": RESPONSIBLE_SERVICE,
        "snapshot_id": snapshot["snapshot_id"],
        "schema_version": SCHEMA_VERSION,
        "period_start": snapshot["report"]["period_start"],
        "period_end": snapshot["report"]["period_end"],
        "is_test": bool(is_test),
        "test_recipient": bool(is_test),
        "official_delivery": False,
        "preview_required": False if not is_test else True,
        "automatic_send": True if not is_test else False,
        "status": "ready_for_test" if is_test else "ready_to_send",
        "snapshot": snapshot,
        "crm_parity_validated": True,
        "deepseek_payload": None if snapshot.get("weekly_operational") else build_deepseek_payload(snapshot),
        "narrative": narrative,
        "narrative_history": [],
        "message_original": message,
        "snapshot_hash": snapshot["snapshot_id"].removeprefix("cws_"),
        "message_final": None,
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "narrative_source": narrative_source,
        "created_by": str(created_by or "system"),
        "created_at": now,
        "updated_at": now,
    }
    if is_test:
        document.update({
            "recipient_type": "administrator",
            "recipient_normalized": ADMIN_RECIPIENT,
            "recipient_masked": mask_whatsapp_recipient(ADMIN_RECIPIENT),
        })
    else:
        document["group_recipient"] = Config.CAPTACION_WEEKLY_GROUP_ID
    await asyncio.to_thread(db[REPORT_COLLECTION].insert_one, document)
    document.pop("_id", None)
    return document


async def regenerate_report_narrative(report_id: str, actor: dict) -> dict:
    db = get_db()
    report = await asyncio.to_thread(
        db[REPORT_COLLECTION].find_one, {"report_id": str(report_id)}
    )
    if not report or report.get("status") not in {"pending_approval", "ready_for_test"}:
        raise ValueError("Reporte no disponible para regenerar")
    narrative, model, narrative_source = await asyncio.to_thread(generate_narrative_with_fallback, report["snapshot"])
    message = assemble_whatsapp_message(report["snapshot"], narrative)
    history_entry = {
        "narrative": report.get("narrative"),
        "message": report.get("message_original"),
        "replaced_at": datetime.now(timezone.utc),
        "replaced_by": str(actor.get("_id") or actor.get("username") or "admin"),
    }
    await asyncio.to_thread(
        db[REPORT_COLLECTION].update_one,
        {"report_id": report["report_id"]},
        {"$set": {"narrative": narrative, "message_original": message, "model": model, "narrative_source": narrative_source, "updated_at": datetime.now(timezone.utc)}, "$push": {"narrative_history": history_entry}},
    )
    return await asyncio.to_thread(
        db[REPORT_COLLECTION].find_one, {"report_id": report["report_id"]}, {"_id": 0}
    )


async def send_test_report(report_id: str, recipient: str) -> dict:
    normalized = normalize_whatsapp_recipient(recipient)
    if normalized != ADMIN_RECIPIENT:
        raise PermissionError("Destinatario de prueba no autorizado")
    db = get_db()
    await asyncio.to_thread(ensure_weekly_report_indexes, db)
    report = await asyncio.to_thread(
        db[REPORT_COLLECTION].find_one, {"report_id": str(report_id), "is_test": True}
    )
    if not report or report.get("status") != "ready_for_test":
        raise ValueError("Reporte de prueba no disponible")
    if not report.get("crm_parity_validated"):
        raise ValueError("El reporte de prueba no posee paridad CRM validada")
    idempotency_key = f"test:{report['report_id']}:{normalized}"
    delivery, claimed = await _claim_delivery(db, idempotency_key, {
        "report_type": "captacion_weekly_preview",
        "report_id": report["report_id"],
        "snapshot_id": report["snapshot_id"],
        "is_test": True,
        "test_recipient": True,
        "official_delivery": False,
        "recipient_type": "administrator",
        "recipient_normalized": normalized,
        "recipient_masked": mask_whatsapp_recipient(normalized),
        "prompt_version": report.get("prompt_version"),
        "model": report.get("model"),
    })
    if not claimed:
        return delivery
    result = await send_whatsapp_message_detailed(normalized, report["message_original"])
    if result.get("success") and result.get("provider_message_id"):
        receipt = await wait_for_whatsapp_delivery(result["provider_message_id"], timeout_seconds=30)
        if receipt.get("delivery_status") != "unknown":
            result["delivery_status"] = receipt["delivery_status"]
        if result.get("delivery_status") == "failed":
            result["success"] = False
    now = datetime.now(timezone.utc)
    delivery.update({
        "provider_message_id": result.get("provider_message_id"),
        "delivery_status": result.get("delivery_status"),
        "completed_at": now,
    })
    delivery = await _complete_delivery(db, delivery)
    await asyncio.to_thread(
        db[REPORT_COLLECTION].update_one,
        {"report_id": report["report_id"]},
        {"$set": {
            "status": "test_sent" if result.get("success") else "test_failed",
            "delivery_status": result.get("delivery_status"),
            "provider_message_id": result.get("provider_message_id"),
            "message_final": report["message_original"],
            "test_sent_at": now if result.get("success") else None,
            "official_sent_at": None,
            "updated_at": now,
        }},
    )
    return delivery


def record_delivery_status_webhook(payload: dict) -> bool:
    """Actualiza el recibo de un envío semanal desde `messages.update`."""
    if payload.get("event") != "messages.update":
        return False
    data = payload.get("data") or {}
    key = data.get("key") or {}
    update = data.get("update") or {}
    provider_message_id = str(key.get("id") or "").strip()
    if not provider_message_id:
        return False
    delivery_status = normalize_provider_status(update.get("status"))
    db = get_db()
    delivery = db[DELIVERY_COLLECTION].find_one({"provider_message_id": provider_message_id})
    if not delivery:
        return False
    now = datetime.now(timezone.utc)
    db[DELIVERY_COLLECTION].update_one(
        {"_id": delivery["_id"]},
        {"$set": {"delivery_status": delivery_status, "status_updated_at": now}},
    )
    db[REPORT_COLLECTION].update_one(
        {"report_id": delivery.get("report_id")},
        {"$set": {"delivery_status": delivery_status, "status_updated_at": now}},
    )
    return True


async def approve_and_send_report(report_id: str, actor: dict, edited_narrative: dict | None = None) -> dict:
    raise ValueError("El envío manual oficial está deshabilitado; el scheduler idempotente es la única ruta autorizada")

    # Compatibilidad histórica: código inalcanzable conservado durante la
    # transición de reportes pendientes creados por versiones anteriores.
    db = get_db()
    await asyncio.to_thread(ensure_weekly_report_indexes, db)
    report = await asyncio.to_thread(
        db[REPORT_COLLECTION].find_one, {"report_id": str(report_id), "is_test": False}
    )
    if not report or report.get("status") != "pending_approval":
        raise ValueError("El reporte no está pendiente de aprobación")
    if not report.get("crm_parity_validated"):
        raise ValueError("La paridad CRM no está validada")
    if report.get("snapshot", {}).get("requires_outcome_review") and not report.get("outcome_review_acknowledged"):
        raise ValueError("Hay resultados en Otros / Por revisar; un administrador debe revisarlos antes del envío")
    narrative = validate_narrative(edited_narrative or report["narrative"])
    final_message = assemble_whatsapp_message(report["snapshot"], narrative)
    idempotency_key = f"official:{report['period_start']}:{report['period_end']}"
    group_id = str(report.get("group_recipient") or Config.CAPTACION_WEEKLY_GROUP_ID or "").strip()
    if not group_id.endswith("@g.us"):
        raise ValueError("Destinatario grupal no configurado")
    approval_at = datetime.now(timezone.utc)
    delivery, claimed = await _claim_delivery(db, idempotency_key, {
        "report_id": report["report_id"],
        "snapshot_id": report["snapshot_id"],
        "is_test": False,
        "official_delivery": True,
        "recipient_type": "group",
        "recipient_masked": mask_whatsapp_recipient(group_id),
        "approved_by": str(actor.get("_id") or actor.get("username") or "admin"),
        "approved_by_name": actor.get("nombre") or actor.get("username") or "Administrador",
        "approved_at": approval_at,
    })
    if not claimed:
        return delivery
    result = await send_whatsapp_message_detailed(group_id, final_message)
    delivery.update({
        "provider_message_id": result.get("provider_message_id"),
        "delivery_status": result.get("delivery_status"),
        "completed_at": datetime.now(timezone.utc),
    })
    delivery = await _complete_delivery(db, delivery)
    changes = edited_narrative if edited_narrative and edited_narrative != report.get("narrative") else None
    await asyncio.to_thread(
        db[REPORT_COLLECTION].update_one,
        {"report_id": report["report_id"]},
        {"$set": {
            "status": "sent" if result.get("success") else "send_failed",
            "official_delivery": bool(result.get("success")),
            "approved_by": delivery["approved_by"],
            "approved_by_name": delivery["approved_by_name"],
            "approved_at": approval_at,
            "sent_at": datetime.now(timezone.utc) if result.get("success") else None,
            "official_sent_at": datetime.now(timezone.utc) if result.get("success") else None,
            "message_final": final_message,
            "manual_changes": changes,
            "provider_message_id": result.get("provider_message_id"),
            "delivery_status": result.get("delivery_status"),
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    return delivery


def acknowledge_outcome_review(report_id: str, actor: dict) -> dict:
    db = get_db()
    now = datetime.now(timezone.utc)
    reviewer = str(actor.get("_id") or actor.get("username") or "admin")
    result = db[REPORT_COLLECTION].update_one(
        {"report_id": str(report_id), "is_test": False, "status": "pending_approval", "snapshot.requires_outcome_review": True},
        {"$set": {"outcome_review_acknowledged": True, "outcome_reviewed_by": reviewer, "outcome_reviewed_at": now, "updated_at": now}},
    )
    if not getattr(result, "modified_count", 0):
        raise ValueError("El reporte no requiere revisión de resultados o ya no está pendiente")
    return db[REPORT_COLLECTION].find_one({"report_id": str(report_id)}, {"_id": 0})


def cancel_report(report_id: str, actor: dict) -> dict:
    db = get_db()
    now = datetime.now(timezone.utc)
    result = db[REPORT_COLLECTION].update_one(
        {"report_id": str(report_id), "is_test": False, "status": "pending_approval"},
        {"$set": {
            "status": "cancelled",
            "cancelled_at": now,
            "cancelled_by": str(actor.get("_id") or actor.get("username") or "admin"),
            "updated_at": now,
        }},
    )
    if not getattr(result, "modified_count", 0):
        raise ValueError("Reporte no disponible para cancelar")
    return db[REPORT_COLLECTION].find_one({"report_id": str(report_id)}, {"_id": 0})


async def _notify_admin_once(report: dict, notification_type: str, text: str) -> dict:
    db = get_db()
    key = f"captacion_weekly_notice:{notification_type}:{report['report_id']}"
    delivery, claimed = await _claim_delivery(db, key, {
        "message_domain": MESSAGE_DOMAIN,
        "message_type": "internal_notice",
        "recipient_role": "administrator",
        "responsible_service": RESPONSIBLE_SERVICE,
        "report_id": report["report_id"],
        "snapshot_id": report.get("snapshot_id"),
        "is_test": False,
        "official_delivery": False,
        "recipient_type": "administrator_notification",
        "recipient_masked": mask_whatsapp_recipient(ADMIN_RECIPIENT),
        "notification_type": notification_type,
    })
    if not claimed:
        return delivery
    result = await send_whatsapp_message_detailed(ADMIN_RECIPIENT, text)
    delivery.update({
        "provider_message_id": result.get("provider_message_id"),
        "delivery_status": result.get("delivery_status"),
        "completed_at": datetime.now(timezone.utc),
    })
    return await _complete_delivery(db, delivery)


def _official_idempotency_key(report: dict, group_id: str) -> str:
    return f"captacion_weekly_official:{report['period_start']}:{report['period_end']}:{group_id}"


async def send_official_report(report_id: str, *, now=None) -> dict:
    """Envía al grupo con una sola entrega durable y reintentos acotados."""
    if not Config.CAPTACION_WEEKLY_PRODUCTION_ENABLED or Config.CAPTACION_WEEKLY_TEST_MODE:
        raise PermissionError("Envío al grupo bloqueado: Captación semanal permanece desactivada")
    db = get_db()
    await asyncio.to_thread(ensure_weekly_report_indexes, db)
    report = await asyncio.to_thread(
        db[REPORT_COLLECTION].find_one,
        {"report_id": str(report_id), "is_test": False, "message_domain": MESSAGE_DOMAIN},
    )
    if not report:
        raise ValueError("El reporte oficial no está disponible para envío")
    group_id = str(report.get("group_recipient") or Config.CAPTACION_WEEKLY_GROUP_ID or "").strip()
    if not group_id.endswith("@g.us") or normalize_whatsapp_recipient(group_id) == ADMIN_RECIPIENT:
        raise ValueError("Destinatario grupal oficial no configurado")
    key = _official_idempotency_key(report, group_id)
    existing_delivery = await asyncio.to_thread(
        db[DELIVERY_COLLECTION].find_one,
        {"idempotency_key": key},
        {"_id": 0},
    )
    if existing_delivery and existing_delivery.get("delivery_status") in {"accepted", "sent", "delivered", "read"}:
        return existing_delivery
    if report.get("status") not in {"ready_to_send", "send_retry_pending", "sent"}:
        raise ValueError("El reporte oficial no está disponible para envío")
    validate_official_report(report["snapshot"], report["message_original"])

    local_now = now.astimezone(CHILE) if now and now.tzinfo else (CHILE.localize(now) if now else datetime.now(CHILE))
    deadline = local_now.replace(hour=WEEKLY_RECOVERY_DEADLINE_HOUR, minute=0, second=0, microsecond=0)
    delivery, claimed = await _claim_delivery(db, key, {
        "message_domain": MESSAGE_DOMAIN,
        "message_type": "weekly_report",
        "recipient_role": "captacion_team",
        "responsible_service": RESPONSIBLE_SERVICE,
        "report_type": "captacion_weekly_official",
        "report_id": report["report_id"],
        "period_start": report["period_start"],
        "period_end": report["period_end"],
        "snapshot_id": report["snapshot_id"],
        "snapshot_hash": report.get("snapshot_hash"),
        "crm_parity": report.get("snapshot", {}).get("crm_parity"),
        "prompt_version": report.get("prompt_version"),
        "model": report.get("model"),
        "narrative_source": report.get("narrative_source"),
        "text_generated": report.get("narrative"),
        "text_final": report.get("message_original"),
        "is_test": False,
        "official_delivery": True,
        "recipient_type": "group",
        "recipient_masked": mask_whatsapp_recipient(group_id),
        "attempt_count": 0,
        "attempts": [],
        "errors": [],
    })
    if not claimed and delivery.get("delivery_status") in {"accepted", "sent", "delivered"}:
        return delivery
    attempt_count = int(delivery.get("attempt_count") or 0)
    if local_now > deadline or attempt_count >= Config.CAPTACION_WEEKLY_MAX_SEND_ATTEMPTS:
        status = "retry_window_expired" if local_now > deadline else "send_failed"
        await asyncio.to_thread(
            db[REPORT_COLLECTION].update_one,
            {"report_id": report["report_id"]},
            {"$set": {"status": status, "updated_at": datetime.now(timezone.utc)}},
        )
        await _notify_admin_once(
            report, status,
            f"⚠️ El reporte semanal de Captaciones no fue enviado al grupo. Estado: {status}.",
        )
        delivery["delivery_status"] = status
        return await _complete_delivery(db, delivery)

    attempt_number = attempt_count + 1
    attempted_at = datetime.now(timezone.utc)
    await asyncio.to_thread(
        db[DELIVERY_COLLECTION].update_one,
        {"idempotency_key": key},
        {"$set": {"attempt_count": attempt_number, "delivery_status": "sending", "last_attempt_at": attempted_at},
         "$push": {"attempts": {"attempt": attempt_number, "started_at": attempted_at}}},
    )
    delivery["attempt_count"] = attempt_number
    delivery.setdefault("attempts", []).append({"attempt": attempt_number, "started_at": attempted_at})
    try:
        result = await send_whatsapp_message_detailed(group_id, report["message_original"])
        if result.get("success") and result.get("provider_message_id"):
            receipt = await wait_for_whatsapp_delivery(result["provider_message_id"], timeout_seconds=30)
            if receipt.get("delivery_status") != "unknown":
                result["delivery_status"] = receipt["delivery_status"]
        success = bool(result.get("success")) and result.get("delivery_status") != "failed"
        error = None if success else (result.get("error") or "provider_send_failed")
    except Exception as exc:
        result, success, error = {}, False, str(exc)

    completed_at = datetime.now(timezone.utc)
    final_failure = not success and attempt_number >= Config.CAPTACION_WEEKLY_MAX_SEND_ATTEMPTS
    delivery.update({
        "provider_message_id": result.get("provider_message_id"),
        "delivery_status": result.get("delivery_status") if success else ("failed" if final_failure else "retry_pending"),
        "last_error": error,
        "completed_at": completed_at,
    })
    if error:
        delivery.setdefault("errors", []).append({"attempt": attempt_number, "error": error, "at": completed_at})
    delivery = await _complete_delivery(db, delivery)
    report_status = "sent" if success else ("send_failed" if final_failure else "send_retry_pending")
    await asyncio.to_thread(
        db[REPORT_COLLECTION].update_one,
        {"report_id": report["report_id"]},
        {"$set": {
            "status": report_status,
            "official_delivery": success,
            "message_final": report["message_original"],
            "provider_message_id": result.get("provider_message_id"),
            "delivery_status": delivery["delivery_status"],
            "sent_at": completed_at if success else None,
            "official_sent_at": completed_at if success else None,
            "updated_at": completed_at,
        }},
    )
    if final_failure:
        await _notify_admin_once(
            report, "send_failed",
            "⚠️ El reporte semanal de Captaciones agotó sus reintentos y no fue enviado al grupo.",
        )
    return delivery


async def check_and_prepare_weekly_report(*, force: bool = False, now=None) -> dict | None:
    local_now = now.astimezone(CHILE) if now and now.tzinfo else (CHILE.localize(now) if now else datetime.now(CHILE))
    due = weekly_production_window_open(local_now)
    if not force and not due:
        return None
    period_end = local_now.date() - timedelta(days=3)
    period_start = period_end - timedelta(days=4)
    db = get_db()
    await asyncio.to_thread(ensure_weekly_report_indexes, db)
    existing = await asyncio.to_thread(
        db[REPORT_COLLECTION].find_one,
        {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "is_test": False,
            "scheduler_generated": True,
        },
        {"_id": 0},
    )
    if existing:
        if existing.get("status") in {"ready_to_send", "send_retry_pending"}:
            await send_official_report(existing["report_id"], now=local_now)
            return await asyncio.to_thread(
                db[REPORT_COLLECTION].find_one, {"report_id": existing["report_id"]}, {"_id": 0}
            )
        if existing.get("status") == "blocked_validation" and not existing.get("recovery_attempted_at"):
            # Un ?nico reintento dentro de la misma ventana semanal. Conserva el
            # documento bloqueado como auditor?a y delega la entrega al worker
            # exclusivo de captacion_weekly_report.
            claimed = await asyncio.to_thread(
                db[REPORT_COLLECTION].find_one_and_update,
                {"report_id": existing["report_id"], "status": "blocked_validation",
                 "recovery_attempted_at": {"$exists": False}},
                {"$set": {"recovery_attempted_at": datetime.now(timezone.utc)}},
            )
            if claimed:
                try:
                    recovered = await create_weekly_report(
                        period_start, period_end, is_test=False, created_by="scheduler_recovery"
                    )
                except Exception as exc:
                    await asyncio.to_thread(
                        db[REPORT_COLLECTION].update_one,
                        {"report_id": existing["report_id"]},
                        {"$set": {"recovery_error": str(exc), "updated_at": datetime.now(timezone.utc)}},
                    )
                    return await asyncio.to_thread(
                        db[REPORT_COLLECTION].find_one, {"report_id": existing["report_id"]}, {"_id": 0}
                    )
                await asyncio.to_thread(
                    db[REPORT_COLLECTION].update_one,
                    {"report_id": existing["report_id"]},
                    {"$set": {"status": "superseded_recovered", "recovery_report_id": recovered["report_id"],
                              "updated_at": datetime.now(timezone.utc)}},
                )
                await asyncio.to_thread(
                    db[REPORT_COLLECTION].update_one,
                    {"report_id": recovered["report_id"]},
                    {"$set": {"scheduler_generated": True, "recovered_from_report_id": existing["report_id"]}},
                )
                await send_official_report(recovered["report_id"], now=local_now)
                return await asyncio.to_thread(
                    db[REPORT_COLLECTION].find_one, {"report_id": recovered["report_id"]}, {"_id": 0}
                )
        return existing
    try:
        report = await create_weekly_report(period_start, period_end, is_test=False, created_by="scheduler")
    except Exception as exc:
        now_utc = datetime.now(timezone.utc)
        report = {
            "report_id": str(uuid.uuid4()),
            "report_type": "captacion_weekly_official",
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "is_test": False,
            "official_delivery": False,
            "scheduler_generated": True,
            "status": "blocked_validation",
            "errors": [str(exc)],
            "created_at": now_utc,
            "updated_at": now_utc,
        }
        await asyncio.to_thread(db[REPORT_COLLECTION].insert_one, report)
        await _notify_admin_once(
            report, "blocked_validation",
            f"⚠️ Reporte semanal de Captaciones bloqueado antes del envío: {exc}",
        )
        report.pop("_id", None)
        return report
    await asyncio.to_thread(
        db[REPORT_COLLECTION].update_one,
        {"report_id": report["report_id"]},
        {"$set": {"scheduler_generated": True}},
    )
    report["scheduler_generated"] = True
    if report["snapshot"].get("requires_outcome_review"):
        await asyncio.to_thread(
            db[REPORT_COLLECTION].update_one,
            {"report_id": report["report_id"]},
            {"$set": {"status": "blocked_outcome_review", "updated_at": datetime.now(timezone.utc)}},
        )
        report["status"] = "blocked_outcome_review"
        await _notify_admin_once(
            report, "blocked_outcome_review",
            "⚠️ Reporte semanal de Captaciones bloqueado: existen resultados en Otros / Por revisar.",
        )
        return report
    await send_official_report(report["report_id"], now=local_now)
    return await asyncio.to_thread(
        db[REPORT_COLLECTION].find_one, {"report_id": report["report_id"]}, {"_id": 0}
    )
