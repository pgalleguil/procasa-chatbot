"""SLA executive alerts with fail-closed eligibility and permanent idempotency."""
import asyncio
import logging

from pymongo.errors import DuplicateKeyError

from config import Config
from .constants import PipelineStage
from .crm_metrics import (
    INSTRUMENTATION_CUTOVER, calculate_sla, coerce_utc_datetime,
    event_evidence, utc_now,
)
from .lead_router import get_active_executive_phone, should_send_now
from .notification_service import NotificationService
from .storage import get_async_db

logger = logging.getLogger(__name__)


async def monitor_sla_thresholds():
    if not Config.CRM_SLA_ALERTS_ENABLED:
        logger.info("[SLA_MONITOR] Alertas a ejecutivos desactivadas.")
        return
    if not should_send_now():
        return

    db = get_async_db()
    cutover = coerce_utc_datetime(INSTRUMENTATION_CUTOVER)
    cycles = await db["crm_assignment_cycles"].find({
        "unassigned_at": None,
        "assigned_at": {"$gte": cutover},
        "assignment_cycle_id": {"$exists": True},
    }).to_list(length=2000)
    cycle_by_lead = {str(c["lead_id"]): c for c in cycles if c.get("lead_id") is not None}
    if not cycle_by_lead:
        return

    leads = await db["leads"].find({
        "_id": {"$in": [c["lead_id"] for c in cycles]},
        "lead_temperature_effective": "HOT",
        "$or": [
            {"pipeline_stage": {"$in": [PipelineStage.NEW, PipelineStage.CONTACTED]}},
            {"pipeline_stage": None}, {"pipeline_stage": {"$exists": False}},
        ],
    }, {"messages": 0, "stage_history": 0}).to_list(length=2000)

    for lead in leads:
        try:
            cycle = cycle_by_lead.get(str(lead.get("_id")))
            if not cycle:
                continue
            assigned_at = coerce_utc_datetime(cycle.get("assigned_at"))
            if not assigned_at or assigned_at < cutover:
                continue

            events = await db["crm_events"].find({
                "lead_id": lead["_id"], "timestamp": {"$gte": assigned_at}
            }).to_list(length=2000)
            if any(event_evidence(event)["management"] for event in events):
                continue

            sla = calculate_sla(assigned_at=assigned_at, now=utc_now())
            level = sla["status"] if sla["status"] in {"near_critical", "critical"} else None
            if not level:
                continue

            executive = lead.get("ejecutivo_asignado")
            executive_phone = await asyncio.to_thread(get_active_executive_phone, executive)
            if not executive_phone or executive_phone == "+56900000000":
                continue

            cycle_id = str(cycle["assignment_cycle_id"])
            key = f"crm_sla:{lead['_id']}:{cycle_id}:{level}"

            # Old phone-based critical records are permanent evidence that this
            # historical alert was already delivered. Never replay them.
            phone = _normalize_phone(lead.get("phone"))
            if level == "critical" and phone and await db["crm_sla_warnings"].find_one({
                "phone": phone, "level": "critical", "status": "sent"
            }):
                continue

            claim = {
                "_id": key, "idempotency_key": key, "lead_id": lead["_id"],
                "assignment_cycle_id": cycle_id, "phone": phone,
                "executive": executive, "level": level, "status": "sending",
                "created_at": utc_now(),
            }
            try:
                await db["crm_sla_warnings"].insert_one(claim)
            except DuplicateKeyError:
                continue

            sent = await NotificationService.send_notification(
                phone=executive_phone,
                message=format_sla_warning_message(executive, lead.get("prospecto", {}).get("nombre", "Cliente"), level),
                alert_type=f"SLA_{level.upper()}",
                meta={"to": executive, "level": level, "lead_id": str(lead["_id"]),
                      "assignment_cycle_id": cycle_id, "idempotency_key": key},
            )
            status = "sent" if sent else "failed"
            await db["crm_sla_warnings"].update_one(
                {"_id": key}, {"$set": {"status": status, f"{status}_at": utc_now()}}
            )
        except Exception:
            logger.exception("[SLA_MONITOR] Error procesando lead canónico")


def _normalize_phone(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def format_sla_warning_message(executive_name, client_name, level):
    if level == "critical":
        header = "🔴 *SLA CRÍTICO - SIN RESPUESTA* 🔴"
        time_text = "más de 3 horas"
        footer = "⚠️ Este lead requiere atención URGENTE."
    else:
        header = "🟠 *PRÓXIMO A CRÍTICO - ALERTA SLA* 🟠"
        time_text = "2:30 horas"
        footer = "Por favor, contacta al cliente pronto para evitar indicadores rojos."
    return (
        f"{header}\n\nHola *{executive_name}*, el cliente *{client_name}* lleva "
        f"*{time_text}* asignado sin recibir gestión comercial.\n\n{footer}\n\n"
        "🔗 *Gestionar ahora:* https://procasa-chatbot-yr8d.onrender.com/\n\n¡Mucho éxito! 🚀"
    )
