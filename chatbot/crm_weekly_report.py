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
from .whatsapp_client import mask_whatsapp_recipient, send_whatsapp_message_detailed

CHILE = pytz.timezone("America/Santiago")
SCHEMA_VERSION = "crm_weekly_snapshot_v1"
PROMPT_VERSION = "crm_weekly_writer_v1"
REPORT_COLLECTION = Config.CRM_WEEKLY_REPORT_COLLECTION
DELIVERY_COLLECTION = Config.CRM_WEEKLY_DELIVERY_COLLECTION
NARRATIVE_FIELDS = ("focus", "closing")

WRITER_PROMPT = (
    "Eres redactor del reporte semanal interno de Gesti\u00f3n de Leads de PROCASA.\n"
    "Recibes exclusivamente m\u00e9tricas agregadas ya calculadas y validadas.\n"
    "Devuelve solo JSON v\u00e1lido con focus y closing.\n"
    "No recalcules, compares ni modifiques cifras. No crees tendencias ni rankings.\n"
    "No eval\u00faes ejecutivos. No incluyas nombres de clientes ni datos personales.\n"
    "No interpretes null como cero. El foco debe respetar operational_focus.key y sus m\u00e9tricas.\n"
    "Usa espa\u00f1ol de Chile, tono profesional y directo. Focus m\u00e1ximo 180 caracteres y closing m\u00e1ximo 80."
)


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
        "unmanaged_cohort": f"Gestionar los {values.get('unmanaged_at_cutoff_unique', 0)} leads que cerraron la semana sin gesti\u00f3n registrada.",
        "sla_overdue": f"Resolver los {values.get('sla_overdue_publishable_unique', 0)} casos con SLA vencido y registrar el resultado.",
        "effective_follow_up": "Dar continuidad a los contactos efectivos y registrar el siguiente paso comercial.",
    }.get(key, "Mantener una gesti\u00f3n oportuna y registrar cada resultado en el CRM.")
    return {"focus": focus, "closing": "\u00a1Buen inicio de semana! \U0001f4aa"}


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
    bullet = "\u2022"
    lines = ["\U0001f4ca *GESTI\u00d3N DE LEADS | INICIO DE SEMANA*", f"\U0001f5d3\ufe0f *Resumen del {period_label(start, end)}*", "",
             f"Durante la semana ingresaron *{c['received_unique']} leads*. De ellos, *{c['managed_unique']} recibieron gesti\u00f3n* y *{c['unmanaged_at_cutoff_unique']} terminaron el viernes sin gesti\u00f3n registrada*.", "",
             "\U0001f4e5 *Cohorte semanal*", f"{bullet} Recibidos: *{c['received_unique']}*", f"{bullet} Gestionados: *{c['managed_unique']}*",
             f"{bullet} Sin gesti\u00f3n al cierre: *{c['unmanaged_at_cutoff_unique']}*"]
    if c["hot_pending_at_cutoff_unique"] is not None:
        lines.append(f"{bullet} Hot pendientes: *{c['hot_pending_at_cutoff_unique']}*")
    lines += ["", "\U0001f504 *Actividad general del pipeline*"]
    activity_labels = (("leads_with_confirmed_attempt_unique", "Leads con intento confirmado"),
                       ("leads_with_effective_contact_unique", "Contactos efectivos"),
                       ("leads_with_visit_unique", "Leads con visita"), ("closed_won_unique", "Cierres ganados"),
                       ("closed_lost_unique", "Cierres perdidos"))
    for key, label in activity_labels:
        if p[key]: lines.append(f"{bullet} {label}: *{p[key]}*")
    if any(p[key] for key in ("leads_with_visit_unique", "closed_won_unique", "closed_lost_unique")):
        lines += ["", "_Las visitas y cierres pueden corresponder a leads ingresados en semanas anteriores._"]
    lines += ["", "\U0001f465 *Gesti\u00f3n por ejecutivo*"]
    for row in snapshot["executives"]:
        lines.append(f"{bullet} {row['name']}: {row['new_assigned_unique']} nuevos \u00b7 {row['managed_unique']} gestionados \u00b7 {row['current_pending_unique']} pendientes")
    lines += ["", "\U0001f525 *Prioridad de hoy*"]
    if priorities["hot_unattended_unique"] is not None: lines.append(f"{bullet} Hot sin atender: *{priorities['hot_unattended_unique']}*")
    if priorities["sla_overdue_publishable_unique"]: lines.append(f"{bullet} SLA vencidos: *{priorities['sla_overdue_publishable_unique']}*")
    if priorities["oldest_pending_display"]: lines.append(f"{bullet} Pendiente m\u00e1s antiguo: *{priorities['oldest_pending_display']}*")
    for limitation in snapshot["data_quality"]["limitations"]:
        lines.append(f"_Limitaci\u00f3n: {limitation}._")
    lines += ["", "\U0001f3af *Foco de esta semana*", narrative["focus"], "", narrative["closing"]]
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
    if message and (re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", message) or
                    re.search(r"(?<!\d)(?:\+?56)?9\d{8}(?!\d)", message)):
        differences.append("pii_detected")
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


async def get_report(report_id, db=None):
    db = db or get_db()
    report = await asyncio.to_thread(db[REPORT_COLLECTION].find_one, {"report_id": report_id}, {"_id": 0})
    if not report: raise ValueError("Reporte CRM no encontrado")
    return report


async def list_reports(limit=20, db=None):
    db = db or get_db()
    def load(): return list(db[REPORT_COLLECTION].find({}, {"_id": 0}).sort("created_at", -1).limit(limit))
    return await asyncio.to_thread(load)


async def regenerate_narrative(report_id, actor, db=None):
    db = db or get_db(); report = await get_report(report_id, db)
    if report["status"] != "pending_approval": raise ValueError("Solo se regenera un reporte pendiente")
    narrative, source = await generate_narrative(report["snapshot"])
    text = assemble_message(report["snapshot"], narrative)
    validate_snapshot(report["snapshot"], message=text)
    update = {"narrative": narrative, "narrative_source": source, "generated_text": text,
              "regenerated_by": actor, "updated_at": utc_now()}
    await asyncio.to_thread(db[REPORT_COLLECTION].update_one, {"report_id": report_id}, {"$set": update})
    return await get_report(report_id, db)


async def cancel_report(report_id, actor, db=None):
    db = db or get_db(); report = await get_report(report_id, db)
    if report["status"] == "sent": raise ValueError("Un reporte enviado no puede modificarse")
    await asyncio.to_thread(db[REPORT_COLLECTION].update_one, {"report_id": report_id},
                            {"$set": {"status": "cancelled", "cancelled_by": actor, "cancelled_at": utc_now(), "updated_at": utc_now()}})
    return await get_report(report_id, db)


def official_idempotency_key(report, group_id):
    return f"crm_weekly_official:{report['period_start']}:{report['period_end']}:{group_id}"


async def approve_and_send(report_id, actor, final_text=None, db=None, sender=None):
    if not Config.CRM_WEEKLY_REPORT_SEND_ENABLED:
        raise ValueError("CRM weekly report sending is disabled")
    db = db or get_db(); ensure_indexes(db)
    report = await get_report(report_id, db)
    if report["status"] == "sent":
        existing = await asyncio.to_thread(db[DELIVERY_COLLECTION].find_one,
                                           {"report_id": report_id, "status": "sent"}, {"_id": 0})
        return existing or {"status": "sent", "report_id": report_id}
    if report["status"] != "pending_approval": raise ValueError("El reporte no estÃ¡ pendiente de aprobaciÃ³n")
    group_id = str(Config.CRM_WEEKLY_REPORT_GROUP_ID or "").strip()
    text = str(final_text or report["generated_text"]).strip()
    validate_snapshot(report["snapshot"], message=text, group_id=group_id, official=True)
    key = official_idempotency_key(report, group_id)
    delivery_id = str(uuid.uuid4())
    claim = {"delivery_id": delivery_id, "idempotency_key": key, "report_id": report_id,
             "period_start": report["period_start"], "period_end": report["period_end"],
             "recipient": mask_whatsapp_recipient(group_id), "recipient_type": "group",
             "official_delivery": True, "is_test": False, "status": "sending",
             "attempts": 1, "created_at": utc_now()}
    try:
        await asyncio.to_thread(db[DELIVERY_COLLECTION].insert_one, deepcopy(claim))
    except Exception:
        existing = await asyncio.to_thread(db[DELIVERY_COLLECTION].find_one, {"idempotency_key": key}, {"_id": 0})
        if not existing: raise
        if existing.get("status") in {"sent", "sending"}: return existing
        delivery_id = existing["delivery_id"]
        claim = existing
        await asyncio.to_thread(
            db[DELIVERY_COLLECTION].update_one, {"delivery_id": delivery_id},
            {"$set": {"status": "sending", "updated_at": utc_now()}, "$inc": {"attempts": 1}},
        )
    send = sender or send_whatsapp_message_detailed
    result = await send(group_id, text)
    status = "sent" if result.get("success") else "failed"
    delivery_update = {"status": status, "delivery_status": result.get("delivery_status"),
                       "provider_message_id": result.get("provider_message_id"), "updated_at": utc_now()}
    await asyncio.to_thread(db[DELIVERY_COLLECTION].update_one, {"delivery_id": delivery_id}, {"$set": delivery_update})
    if not result.get("success"): raise RuntimeError("El proveedor rechazÃ³ el envÃ­o del reporte CRM")
    report_update = {"status": "sent", "approved_by": actor, "approved_at": utc_now(),
                     "sent_at": utc_now(), "final_text": text, "delivery_id": delivery_id,
                     "updated_at": utc_now()}
    await asyncio.to_thread(db[REPORT_COLLECTION].update_one, {"report_id": report_id, "status": "pending_approval"}, {"$set": report_update})
    return {**claim, **delivery_update}


def previous_complete_week(now_local):
    monday = now_local.date() - timedelta(days=now_local.weekday() + 7)
    return monday, monday + timedelta(days=4)


async def scheduler_tick(now=None, db=None):
    if not Config.CRM_WEEKLY_REPORT_GENERATION_ENABLED:
        return None
    db = db or get_db(); now_local = (now or datetime.now(CHILE)).astimezone(CHILE)
    if now_local.weekday() != 0: return None
    scheduled = CHILE.localize(datetime.combine(now_local.date(), time(Config.CRM_WEEKLY_SCHEDULE_HOUR, Config.CRM_WEEKLY_SCHEDULE_MINUTE)))
    if not (scheduled <= now_local < scheduled + timedelta(minutes=5)): return None
    start, end = previous_complete_week(now_local)
    existing = await asyncio.to_thread(db[REPORT_COLLECTION].find_one,
                                       {"period_start": str(start), "period_end": str(end), "status": {"$ne": "cancelled"}}, {"_id": 0})
    if existing: return existing
    report = await create_preview(period_start=start, period_end=end, priority_as_of=now_local, db=db)
    await asyncio.to_thread(db["admin_notifications"].insert_one,
                            {"type": "crm_weekly_pending_approval", "report_id": report["report_id"],
                             "status": "pending", "created_at": utc_now()})
    return report


async def crm_weekly_scheduler_loop():
    while True:
        try: await scheduler_tick()
        except asyncio.CancelledError: raise
        except Exception: pass
        await asyncio.sleep(60)
