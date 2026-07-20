"""Weekly CRM preview workflow, isolated from Captaciones."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone

import pytz
from openai import OpenAI

from config import Config
from .crm_metrics import (
    INSTRUMENTATION_CUTOVER, METRIC_VERSION, build_weekly_crm_snapshot,
    coerce_utc_datetime, utc_now,
)
from .storage import get_db

CHILE = pytz.timezone("America/Santiago")
SCHEMA_VERSION = "crm_weekly_snapshot_v1"
PROMPT_VERSION = "crm_weekly_writer_v1"
REPORT_COLLECTION = "crm_weekly_reports"
DELIVERY_COLLECTION = "crm_weekly_deliveries"
NARRATIVE_FIELDS = ("focus", "closing")

WRITER_PROMPT = """Eres redactor del reporte semanal interno de GestiÃ³n de Leads de PROCASA.
Recibes exclusivamente mÃ©tricas agregadas ya calculadas y validadas.
Devuelve solo JSON vÃ¡lido con focus y closing.
No recalcules, compares ni modifiques cifras. No crees tendencias ni rankings.
No evalÃºes ejecutivos. No incluyas nombres de clientes ni datos personales.
No interpretes null como cero. El foco debe respetar operational_focus.key y sus mÃ©tricas.
Usa espaÃ±ol de Chile, tono profesional y directo. Focus mÃ¡ximo 180 caracteres y closing mÃ¡ximo 80.
"""


def ensure_indexes(db):
    db[REPORT_COLLECTION].create_index("report_id", unique=True)
    db[REPORT_COLLECTION].create_index([("period_start", 1), ("period_end", 1), ("created_at", -1)])
    db[DELIVERY_COLLECTION].create_index("idempotency_key", unique=True)


def executive_order():
    configured = getattr(Config, "CRM_WEEKLY_EXECUTIVE_ORDER", "") or ""
    return [name.strip() for name in configured.split(",") if name.strip()]


def derive_operational_focus(snapshot):
    cohort = snapshot["cohort"]
    pipeline = snapshot["pipeline_activity"]
    priority = snapshot["monday_priorities"]
    if priority.get("hot_unattended_unique"):
        return {"key": "hot_unattended", "supporting_metrics": {"hot_unattended_unique": priority["hot_unattended_unique"]}}
    if cohort["unmanaged_at_cutoff_unique"]:
        return {"key": "unmanaged_cohort", "supporting_metrics": {"unmanaged_at_cutoff_unique": cohort["unmanaged_at_cutoff_unique"]}}
    if priority["sla_overdue_publishable_unique"]:
        return {"key": "sla_overdue", "supporting_metrics": {"sla_overdue_publishable_unique": priority["sla_overdue_publishable_unique"]}}
    if pipeline["leads_with_effective_contact_unique"] and not pipeline["closed_won_unique"]:
        return {"key": "effective_follow_up", "supporting_metrics": {"leads_with_effective_contact_unique": pipeline["leads_with_effective_contact_unique"]}}
    return {"key": "consistent_management", "supporting_metrics": {}}


def fallback_narrative(snapshot):
    key = snapshot["operational_focus"]["key"]
    values = snapshot["operational_focus"]["supporting_metrics"]
    focus = {
        "hot_unattended": f"Priorizar los {values.get('hot_unattended_unique', 0)} leads Hot sin atender y registrar cada resultado.",
        "unmanaged_cohort": f"Gestionar los {values.get('unmanaged_at_cutoff_unique', 0)} leads que cerraron la semana sin gestiÃ³n registrada.",
        "sla_overdue": f"Resolver los {values.get('sla_overdue_publishable_unique', 0)} casos con SLA vencido y registrar el resultado.",
        "effective_follow_up": "Dar continuidad a los contactos efectivos y registrar el siguiente paso comercial.",
    }.get(key, "Mantener una gestiÃ³n oportuna y registrar cada resultado en el CRM.")
    return {"focus": focus, "closing": "Â¡Buen inicio de semana! ðŸ’ª"}


def _safe_narrative(value, snapshot):
    fallback = fallback_narrative(snapshot)
    if not isinstance(value, dict) or set(value) != set(NARRATIVE_FIELDS):
        return fallback
    result = {}
    for field, limit in (("focus", 180), ("closing", 80)):
        text = str(value.get(field) or "").strip()
        if not text or len(text) > limit or re.search(r"\b\d+(?:[.,]\d+)?%?\b", text):
            return fallback
        result[field] = text
    return result


async def generate_narrative(snapshot):
    fallback = fallback_narrative(snapshot)
    if not Config.DEEPSEEK_API_KEY:
        return fallback, "fallback"
    safe_payload = {
        "operational_focus": snapshot["operational_focus"],
        "cohort": snapshot["cohort"],
        "pipeline_activity": snapshot["pipeline_activity"],
        "monday_priorities": snapshot["monday_priorities"],
        "data_quality": snapshot["data_quality"],
    }
    def call():
        client = OpenAI(api_key=Config.DEEPSEEK_API_KEY, base_url=Config.DEEPSEEK_BASE_URL)
        response = client.chat.completions.create(
            model=Config.DEEPSEEK_MODEL_FAST, temperature=0.1,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": WRITER_PROMPT},
                      {"role": "user", "content": json.dumps(safe_payload, ensure_ascii=False, default=str)}],
        )
        return json.loads(response.choices[0].message.content)
    try:
        return _safe_narrative(await asyncio.to_thread(call), snapshot), "deepseek"
    except Exception:
        return fallback, "fallback"


def period_label(start, end):
    months = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
    return f"{start.day} al {end.day} de {months[end.month - 1]} de {end.year}"


def assemble_message(snapshot, narrative):
    c, p, priorities = snapshot["cohort"], snapshot["pipeline_activity"], snapshot["monday_priorities"]
    start, end = date.fromisoformat(snapshot["report"]["period_start"]), date.fromisoformat(snapshot["report"]["period_end"])
    lines = ["ðŸ“Š *GESTIÃ“N DE LEADS | INICIO DE SEMANA*", f"ðŸ—“ï¸ *Resumen del {period_label(start, end)}*", "",
             f"Durante la semana ingresaron *{c['received_unique']} leads*. De ellos, *{c['managed_unique']} recibieron gestiÃ³n* y *{c['unmanaged_at_cutoff_unique']} terminaron el viernes sin gestiÃ³n registrada*.", "",
             "ðŸ“¥ *Cohorte semanal*", f"â€¢ Recibidos: *{c['received_unique']}*", f"â€¢ Gestionados: *{c['managed_unique']}*",
             f"â€¢ Sin gestiÃ³n al cierre: *{c['unmanaged_at_cutoff_unique']}*"]
    if c["hot_pending_at_cutoff_unique"] is not None:
        lines.append(f"â€¢ Hot pendientes: *{c['hot_pending_at_cutoff_unique']}*")
    lines += ["", "ðŸ”„ *Actividad general del pipeline*"]
    activity_labels = (("leads_with_confirmed_attempt_unique", "Leads con intento confirmado"),
                       ("leads_with_effective_contact_unique", "Contactos efectivos"),
                       ("leads_with_visit_unique", "Leads con visita"), ("closed_won_unique", "Cierres ganados"),
                       ("closed_lost_unique", "Cierres perdidos"))
    for key, label in activity_labels:
        if p[key]: lines.append(f"â€¢ {label}: *{p[key]}*")
    if any(p[key] for key in ("leads_with_visit_unique", "closed_won_unique", "closed_lost_unique")):
        lines += ["", "_Las visitas y cierres pueden corresponder a leads ingresados en semanas anteriores._"]
    lines += ["", "ðŸ‘¥ *GestiÃ³n por ejecutivo*"]
    for row in snapshot["executives"]:
        lines.append(f"â€¢ {row['name']}: {row['new_assigned_unique']} nuevos Â· {row['managed_unique']} gestionados Â· {row['current_pending_unique']} pendientes")
    lines += ["", "ðŸ”¥ *Prioridad de hoy*"]
    if priorities["hot_unattended_unique"] is not None: lines.append(f"â€¢ Hot sin atender: *{priorities['hot_unattended_unique']}*")
    if priorities["sla_overdue_publishable_unique"]: lines.append(f"â€¢ SLA vencidos: *{priorities['sla_overdue_publishable_unique']}*")
    if priorities["oldest_pending_display"]: lines.append(f"â€¢ Pendiente mÃ¡s antiguo: *{priorities['oldest_pending_display']}*")
    for limitation in snapshot["data_quality"]["limitations"]:
        lines.append(f"_LimitaciÃ³n: {limitation}._")
    lines += ["", "ðŸŽ¯ *Foco de esta semana*", narrative["focus"], "", narrative["closing"]]
    return "\n".join(lines)


def validate_snapshot(snapshot, message=None, group_id=None, official=False):
    differences = []
    c = snapshot["cohort"]
    if c["managed_unique"] + c["unmanaged_at_cutoff_unique"] != c["received_unique"]:
        differences.append("cohort_partition")
    audit = snapshot.get("_audit") or {}
    for key in ("cohort_ids", "managed_ids", "unmanaged_ids"):
        values = audit.get(key) or []
        if len(values) != len(set(map(str, values))): differences.append(f"duplicate_{key}")
    names = [row["name"] for row in snapshot["executives"]]
    if len(names) != len(set(names)): differences.append("duplicate_executive")
    if snapshot["data_quality"].get("excluded_ambiguous_events") and any(
        "ambiguous" in str(value).lower() for value in audit.values()
    ): differences.append("ambiguous_event_credited")
    if snapshot["data_quality"].get("sla_definition") != "team_first_assignment_to_first_valid_management":
        differences.append("sla_definition")
    if not snapshot["data_quality"].get("temperature_publishable") and any(
        c[key] is not None for key in ("hot_at_cutoff_unique", "cold_at_cutoff_unique", "hot_pending_at_cutoff_unique")
    ): differences.append("unreliable_temperature")
    if message and len(message) > 1300: differences.append("message_too_long")
    if official and (not group_id or not str(group_id).endswith("@g.us")): differences.append("invalid_group")
    snapshot["crm_parity"] = {"validated": not differences, "differences": differences}
    if differences: raise ValueError(f"CRM weekly validation failed: {differences}")
    return snapshot["crm_parity"]


async def create_preview(*, period_start, period_end, priority_as_of, db=None):
    db = db or get_db(); ensure_indexes(db)
    snapshot = await asyncio.to_thread(build_weekly_crm_snapshot, db, period_start=period_start,
                                       period_end=period_end, priority_as_of=priority_as_of,
                                       executive_order=executive_order())
    snapshot["operational_focus"] = derive_operational_focus(snapshot)
    validate_snapshot(snapshot)
    narrative, source = await generate_narrative(snapshot)
    message = assemble_message(snapshot, narrative)
    validate_snapshot(snapshot, message=message)
    snapshot_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()
    report = {"report_id": str(uuid.uuid4()), "report_type": "crm_weekly_preview", "status": "pending_approval",
              "period_start": str(period_start), "period_end": str(period_end), "priority_as_of": snapshot["monday_priorities"]["priority_as_of"],
              "snapshot": snapshot, "snapshot_hash": snapshot_hash, "narrative": narrative,
              "narrative_source": source, "prompt_version": PROMPT_VERSION, "model": Config.DEEPSEEK_MODEL_FAST if source == "deepseek" else None,
              "generated_text": message, "final_text": None, "preview_required": True,
              "automatic_send": False, "created_at": utc_now(), "updated_at": utc_now()}
    await asyncio.to_thread(db[REPORT_COLLECTION].insert_one, deepcopy(report))
    report.pop("_id", None)
    return report
