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

from captacion_goals import get_captacion_goal_dashboard
from captacion_management import OUTCOME_COMMUNICATION_LABELS, OUTCOME_GROUPS
from captacion_workforce import DEFAULT_TIMEZONE
from config import Config
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
            "closing": "Gracias por mantener cada gestión correctamente registrada en el CRM.",
        }
        return validate_narrative(narrative), "deterministic_fallback", "fallback"


def _deterministic_focus_message(snapshot: dict) -> str:
    details = snapshot.get("detailed_outcomes") or {}
    if snapshot["operational_priority"]["key"] == "pending_follow_up":
        return (
            f"Priorizar las *{details.get('por_contactar', 0)} propiedades por contactar* y retomar las "
            f"*{details.get('no_respondio', 0)} sin respuesta*, registrando el resultado de cada nueva gestión en el CRM."
        )
    return snapshot["operational_priority"]["label"] + ", registrando cada resultado en el CRM."


def assemble_whatsapp_message(snapshot: dict, narrative: dict) -> str:
    narrative = validate_narrative(narrative, allow_digits=True)
    period = snapshot["report"]["period_label"]
    team = snapshot["team"]
    lines = [
        "🧪 *PRUEBA INTERNA — CAPTACIONES*" if snapshot["report"]["is_test"] else "🏠 *REPORTE SEMANAL — CAPTACIONES*",
        f"🗓️ *Resumen del {period}*",
        "",
        narrative["intro"],
        "",
        f"La semana pasada quedaron registradas *{team['properties_managed_unique']} propiedades gestionadas* por el equipo.",
        "",
        "📋 *Resultado de las gestiones*",
    ]
    for group in snapshot["outcome_groups"].values():
        if not group["total"]:
            continue
        lines.append(f"• {group['label']}: *{group['total']}*")
        details = []
        for key, value in group["details"].items():
            if value:
                label = OUTCOME_COMMUNICATION_LABELS.get(key) or snapshot.get("detail_labels", {}).get(key) or key.replace("_", " ")
                details.append(f"{value} {label.casefold()}")
        if details:
            lines.append(f"  _{' · '.join(details)}_")
    lines.extend(["", "👥 *Gestión por ejecutiva*"])
    for row in snapshot["executives"]:
        managed = row["properties_managed_unique"]
        if not managed and not snapshot["data_quality"]["historical_measurement_complete"]:
            lines.append(f"• {row['name']}: _sin gestiones acreditables registradas_")
        else:
            noun = "gestionada" if managed == 1 else "gestionadas"
            lines.append(f"• {row['name']}: *{managed} {noun}*")
    lines.extend([
        "",
        "🎯 *Foco de esta semana*",
        _deterministic_focus_message(snapshot),
        "",
        narrative["closing"],
    ])
    if not snapshot["data_quality"]["historical_measurement_complete"]:
        lines.extend([
            "",
            "_Nota: este periodo corresponde a la etapa inicial de medición y puede no representar gestiones que no quedaron registradas bajo las reglas actuales._",
        ])
    if snapshot["report"]["is_test"]:
        lines.extend(["", "_Este mensaje de prueba fue enviado únicamente al administrador._"])
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
        "transitional_note_once": message.count("etapa inicial de medición") == 1,
        "no_owner_pii": not any(term in message.casefold() for term in ("propietario:", "teléfono:", "dirección:", "correo:")),
    }
    if not all(checks.values()):
        failed = ", ".join(key for key, passed in checks.items() if not passed)
        raise ValueError(f"Validación de prueba fallida: {failed}")
    return checks


async def create_weekly_report(period_start, period_end, *, is_test: bool, created_by="system") -> dict:
    db = get_db()
    await asyncio.to_thread(ensure_weekly_report_indexes, db)
    snapshot = await asyncio.to_thread(
        build_weekly_snapshot, db, period_start, period_end, is_test=is_test
    )
    if not snapshot["crm_parity"]["validated"]:
        raise ValueError("Paridad CRM no validada")
    narrative, model, narrative_source = await asyncio.to_thread(generate_narrative_with_fallback, snapshot)
    message = assemble_whatsapp_message(snapshot, narrative)
    now = datetime.now(timezone.utc)
    document = {
        "report_id": str(uuid.uuid4()),
        "report_type": "captacion_weekly_preview" if is_test else "captacion_weekly_official",
        "snapshot_id": snapshot["snapshot_id"],
        "schema_version": SCHEMA_VERSION,
        "period_start": snapshot["report"]["period_start"],
        "period_end": snapshot["report"]["period_end"],
        "is_test": bool(is_test),
        "test_recipient": bool(is_test),
        "official_delivery": False,
        "preview_required": True,
        "automatic_send": False,
        "status": "ready_for_test" if is_test else "pending_approval",
        "snapshot": snapshot,
        "crm_parity_validated": True,
        "deepseek_payload": build_deepseek_payload(snapshot),
        "narrative": narrative,
        "narrative_history": [],
        "message_original": message,
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
        document["group_recipient"] = Config.DAILY_REPORT_GROUP_ID
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
    group_id = str(report.get("group_recipient") or Config.DAILY_REPORT_GROUP_ID or "").strip()
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


async def _notify_pending_approval(report: dict) -> dict:
    db = get_db()
    key = f"approval_notice:{report['report_id']}"
    delivery, claimed = await _claim_delivery(db, key, {
        "report_id": report["report_id"],
        "snapshot_id": report["snapshot_id"],
        "is_test": False,
        "official_delivery": False,
        "recipient_type": "administrator_notification",
        "recipient_masked": mask_whatsapp_recipient(ADMIN_RECIPIENT),
    })
    if not claimed:
        return delivery
    text = (
        "📋 *Reporte semanal de Captaciones disponible*\n"
        f"Periodo: *{report['snapshot']['report']['period_label']}*\n"
        "Estado: *pendiente de aprobación*\n\n"
        "Revisa la vista administrativa para aprobar, editar, regenerar o cancelar. "
        "No se enviará automáticamente al grupo."
    )
    result = await send_whatsapp_message_detailed(ADMIN_RECIPIENT, text)
    delivery.update({
        "provider_message_id": result.get("provider_message_id"),
        "delivery_status": result.get("delivery_status"),
        "completed_at": datetime.now(timezone.utc),
    })
    return await _complete_delivery(db, delivery)


async def check_and_prepare_weekly_report(*, force: bool = False, now=None) -> dict | None:
    local_now = now.astimezone(CHILE) if now and now.tzinfo else (CHILE.localize(now) if now else datetime.now(CHILE))
    scheduled_time_reached = (local_now.hour, local_now.minute) >= (
        Config.CAPTACION_WEEKLY_SCHEDULE_HOUR,
        Config.CAPTACION_WEEKLY_SCHEDULE_MINUTE,
    )
    due = local_now.weekday() == 0 and scheduled_time_reached
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
        return existing
    report = await create_weekly_report(period_start, period_end, is_test=False, created_by="scheduler")
    await asyncio.to_thread(
        db[REPORT_COLLECTION].update_one,
        {"report_id": report["report_id"]},
        {"$set": {"scheduler_generated": True, "status": "pending_approval"}},
    )
    report["scheduler_generated"] = True
    report["status"] = "pending_approval"
    await _notify_pending_approval(report)
    return report
