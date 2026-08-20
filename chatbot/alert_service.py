import logging
import json
import asyncio
import pytz
from datetime import datetime, timedelta
from .storage import obtener_prospecto, actualizar_prospecto, save_pending_notification, run_in_threadpool, record_observability_event
from .lead_router import find_responsible_executive, should_send_now, format_whatsapp_template
from .constants import CHILE_TZ
from .notification_service import NotificationService
from config import Config

logger = logging.getLogger(__name__)

# --- LOCK PARA EVITAR DUPLICADOS DURANTE EL DELAY ---
# Estructura: {(phone, lead_type): timestamp_inicio}
actively_processing_alerts = {}

INVALID_PROPERTY_CODES = {"", "N/D", "NONE", "NULL", "S/N", "ND"}

def _has_valid_property_code(value) -> bool:
    if value is None:
        return False
    return str(value).strip().upper() not in INVALID_PROPERTY_CODES

def should_send_alert(phone: str, lead_type: str, window_minutes: int) -> bool:
    prospecto = obtener_prospecto(phone) or {}
    alerts = prospecto.get("alerts_sent", {})
    
    if isinstance(alerts, str):
        try:
            alerts = json.loads(alerts.replace("'", "\""))
        except:
            alerts = {}
            
    # NUEVO: Anti-spam riguroso para evolución de leads (Caso 3)
    # Si estamos intentando enviar una alerta HOT, validamos si ya se envió alguna vez
    # cualquier alerta de la familia HOT para este prospecto. Si ya se envió, NO enviamos más.
    HOT_ALERT_TYPES = ["InteresVisita", "SolicitudContacto", "EscaladoUrgente", "LeadHotWhatsapp"]
    if lead_type in HOT_ALERT_TYPES:
        for hot_type in HOT_ALERT_TYPES:
            if alerts.get(hot_type):
                logger.info(f"[ALERT] Bloqueando alerta '{lead_type}' porque el lead ya tuvo una alerta HOT previa ('{hot_type}')")
                return False
    
    ts_iso = alerts.get(lead_type)
    
    if not ts_iso:
        return True

    try:
        last = datetime.fromisoformat(ts_iso)
        # Asegurar que sea aware si no lo es (la DB a veces guarda naive aunque usemos isoformat)
        if last.tzinfo is None:
            last = CHILE_TZ.localize(last)
    except ValueError:
        return True

    elapsed = datetime.now(CHILE_TZ) - last
    return elapsed > timedelta(minutes=window_minutes)


def mark_alert_sent(phone: str, lead_type: str) -> None:
    prospecto = obtener_prospecto(phone) or {}
    alerts = prospecto.get("alerts_sent", {})
    
    if isinstance(alerts, str):
        try:
            alerts = json.loads(alerts.replace("'", "\""))
        except:
            alerts = {}
    
    alerts[lead_type] = datetime.now(CHILE_TZ).isoformat()
    actualizar_prospecto(phone, {"alerts_sent": alerts})


def _send_alert_once_sync(
    phone: str,
    lead_type: str,
    lead_score: int,
    criteria: dict,
    last_response: str,
    last_user_msg: str,
    full_history: list,
    window_minutes: int = 60, # MODIFICADO: 60 minutos para evitar duplicidad si el cliente sigue hablando
    lead_type_label: str | None = None
):
    if not Config.LEAD_HOT_NOTIFICATIONS_ENABLED:
        logger.info("[ALERT] delivery suppressed flag=LEAD_HOT_NOTIFICATIONS_ENABLED")
        return {"status": "failed", "reason": "hot_notifications_disabled"}
    """
    Gestiona el envío de la alerta (WhatsApp al ejecutivo) para evitar spam.
    window_minutes: Tiempo mínimo entre alertas del MISMO tipo.
    """
    
    # Lógica extra: Si es solo un agradecimiento ("gracias"), aumentamos la restricción
    msg_lower = last_user_msg.lower().strip()
    if len(msg_lower) < 10 and any(w in msg_lower for w in ["gracias", "ok", "bueno", "listo"]):
        logger.info(f"[ALERT] SKIPPED LOW VALUE MSG: {msg_lower}")
        return {"status": "rejected", "reason": "low_value_message"}

    # EXCEPCIÓN: Si es un escalado urgente (ej: el cliente reclama que no lo llaman), 
    # saltamos el bloqueo de tiempo para asegurar que el ejecutivo se entere.
    is_urgent = lead_type == "EscaladoUrgente"
    
    if not is_urgent and not should_send_alert(phone, lead_type, window_minutes):
        logger.info(f"[ALERT] SKIPPED DUPLICATE ALERT {lead_type} for {phone} (Wait {window_minutes}m)")
        return {"status": "deduplicated", "reason": "alert_window"}

    try:
        # 1. Preparar datos del lead
        lead_data = {
            "phone": phone,
            "lead_type": lead_type,
            "lead_score": lead_score,
            "nombre": criteria.get("nombre"),
            "email": criteria.get("email"),
            "last_message": last_user_msg,
            "property_code": criteria.get("codigo", "N/D"),
            "rut": criteria.get("rut"),
            "operacion": criteria.get("operacion"),
            "comuna": criteria.get("comuna"),
            "region": criteria.get("region")
        }

        # 2. ENRUTAMIENTO INTELIGENTE
        # Solo HOT/urgentes deben llegar al equipo por WhatsApp.
        hot_alerts = {"EscaladoUrgente", "InteresVisita", "SolicitudContacto", "MissingProperty"}
        if lead_type not in hot_alerts:
            logger.info(
                "[ALERT] Non-HOT alert skipped for WhatsApp routing: lead_type=%s phone=%s",
                lead_type,
                phone,
            )
            return {"status": "rejected", "reason": "non_hot_alert"}

        # Buscamos quién es el responsable REAL (según reglas JPC, Región, etc.)
        raw_link_pendiente = criteria.get("link_pendiente")
        is_link_pendiente = str(raw_link_pendiente).lower() == "true" if isinstance(raw_link_pendiente, str) else bool(raw_link_pendiente)
        
        is_missing_property = (
            is_link_pendiente
            or not _has_valid_property_code(criteria.get("codigo"))
            or not _has_valid_property_code(lead_data.get("property_code"))
        )
        
        if is_missing_property:
            admin_phone = "56983219804"
            admin_msg = (
                f"🚨 *Propiedad No Encontrada*\n\n"
                f"Lead: {criteria.get('nombre') or lead_data.get('phone')}\n"
                f"Teléfono: {phone}\n"
                f"Código: {lead_data.get('property_code', 'N/D')}\n\n"
                f"El cliente envió un enlace o código que no existe en Prop360. "
                f"Favor revisar y actualizar la cartera."
            )
            lead_data["target_phone"] = admin_phone
            lead_data["target_name"] = "Pablo Galleguillos"
            lead_data["is_new_assignment"] = False
            lead_data["assignment_type"] = "MISSING_PROPERTY"
            logger.info(f"[ALERT] Missing property detected. Routing alert to admin only for phone={phone}")
            save_pending_notification(lead_data)
            return {"status": "enqueued", "durable": "pending_notification", "reason": "missing_property"}

        from .constants import UNASSIGNED_LABEL
        assigned_exec = criteria.get("ejecutivo_asignado") or criteria.get("ejecutivo")
        
        # Si ya tiene ejecutivo y es válido (no un administrativo genérico), mantenemos al mismo.
        # unassigned_labels = [UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", "Sin asignar", "N/A"]
        if assigned_exec and assigned_exec != UNASSIGNED_LABEL and assigned_exec != "Sin Asignar" and assigned_exec != "N/A":
             exec_name = assigned_exec
             # Intentamos obtener el teléfono de ese ejecutivo existente
             from .lead_router import get_executive_phone
             exec_phone = get_executive_phone(assigned_exec)
             is_new_assignment = False
             logger.info(f"[ALERT] Lead ya asignado a {exec_name}. Manteniendo asignación.")
        else:
             logger.info(f"Asignando lead en base a propiedad {lead_data['property_code']}")
             exec_name, exec_phone, assignment_type = find_responsible_executive(
                 property_code=lead_data["property_code"],
                 lead_phone=phone,
                 lead_name=lead_data.get("nombre")
             )
             lead_data["target_name"] = exec_name
             lead_data["target_phone"] = exec_phone
             lead_data["assignment_type"] = assignment_type
             is_new_assignment = True
             if assignment_type in {"MISSING_PROPERTY", "NO_PROPERTY"} or exec_name == UNASSIGNED_LABEL:
                logger.info(
                     "[ALERT] Router no encontro propiedad valida para phone=%s property_code=%s. No se asigna ejecutivo.",
                     phone,
                     lead_data.get("property_code"),
                )
                return {"status": "failed", "reason": "unroutable_property"}
             logger.info(f"[ALERT] Ruteo: Ejecutivo determinado: {exec_name} | Teléfono: {exec_phone} | Es nuevo: {is_new_assignment}")

        # Canonical path: assignment cycle is persisted before the notification.
        # The legacy phone/property queue is never used while the Hot flag is on.
        from .crm_metrics import resolve_canonical_lead
        from .lead_router import get_next_business_slot
        from .crm_hot_delivery import assign_and_enqueue_hot
        from .storage import get_db
        db = get_db()
        resolution = resolve_canonical_lead(db, lead_id=criteria.get("_id"), phone=phone)
        if not resolution.lead or resolution.status == "ambiguous_phone":
            logger.error("[ALERT] canonical Hot blocked identity_status=%s", resolution.status)
            return {"status": "failed", "reason": f"identity_{resolution.status}"}
        due_local = get_next_business_slot(datetime.now(CHILE_TZ))
        recipient_user = db["usuarios"].find_one({"nombre": exec_name, "is_active": {"$ne": False}})
        if not recipient_user or recipient_user.get("_id") is None:
            logger.error("[ALERT] canonical Hot blocked unresolved recipient_user_id")
            return {"status": "failed", "reason": "unresolved_recipient_user_id"}
        recipient_user_id = str(recipient_user["_id"])
        lead_data["target_name"] = exec_name
        lead_data["target_phone"] = exec_phone
        lead_data["is_new_assignment"] = is_new_assignment
        source_job = db["chatbot_inbound_jobs"].find_one(
            {
                "phone": resolution.lead.get("phone"),
                "inbound_provider_message_id": {"$exists": True, "$ne": None},
            },
            sort=[("received_at", -1)],
        )
        if not source_job:
            logger.error("[ALERT] canonical Hot blocked: no verified inbound source")
            return {"status": "failed", "reason": "no_verified_inbound_source"}
        canonical = assign_and_enqueue_hot(
            db, lead=resolution.lead, recipient_user_id=recipient_user_id, recipient_name=exec_name,
            recipient_phone=exec_phone, payload=lead_data,
            assigned_by="system", reason="inbound_message", assigned_at=due_local,
            send_after=due_local,
            source_event_id=source_job.get("inbound_provider_message_id"),
        )
        logger.info(
            "[ALERT] canonical Hot queued lead_id=%s cycle_id=%s delivery_id=%s",
            resolution.lead.get("_id"), canonical["cycle"]["assignment_cycle_id"],
            canonical["notification"]["delivery_id"],
        )
        mark_alert_sent(phone, lead_type)
        return {
            "status": "deduplicated" if canonical.get("dedup_suppressed") else "enqueued",
            "assignment_cycle_id": canonical["cycle"].get("assignment_cycle_id"),
            "delivery_id": canonical["notification"].get("delivery_id") or canonical["notification"].get("_id"),
            "durable": "crm_hot_delivery",
        }

    except Exception as e:
        logger.error(f"[ALERT] ERROR routing alert: {e}", exc_info=True)
        return {"status": "failed", "reason": type(e).__name__}

    finally:
        # Liberar el lock siempre
        actively_processing_alerts.pop((phone, lead_type), None)


async def send_alert_once(
    phone: str,
    lead_type: str,
    lead_score: int,
    criteria: dict,
    last_response: str,
    last_user_msg: str,
    full_history: list,
    window_minutes: int = 60, # MODIFICADO: 60 minutos para evitar duplicidad si el cliente sigue hablando
    lead_type_label: str | None = None
):
    return await asyncio.to_thread(
        _send_alert_once_sync,
        phone,
        lead_type,
        lead_score,
        criteria,
        last_response,
        last_user_msg,
        full_history,
        window_minutes,
        lead_type_label
    )
