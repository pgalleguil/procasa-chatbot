import logging
import json
from datetime import datetime, timedelta
from .storage import obtener_prospecto, actualizar_prospecto, save_pending_notification
from .lead_router import find_responsible_executive, should_send_now, format_whatsapp_template
from .whatsapp_client import send_whatsapp_message

logger = logging.getLogger(__name__)

def should_send_alert(phone: str, lead_type: str, window_minutes: int) -> bool:
    prospecto = obtener_prospecto(phone) or {}
    alerts = prospecto.get("alerts_sent", {})
    
    if isinstance(alerts, str):
        try:
            alerts = json.loads(alerts.replace("'", "\""))
        except:
            alerts = {}
    
    ts_iso = alerts.get(lead_type)
    
    if not ts_iso:
        return True

    try:
        last = datetime.fromisoformat(ts_iso)
    except ValueError:
        return True

    elapsed = datetime.utcnow() - last
    return elapsed > timedelta(minutes=window_minutes)


def mark_alert_sent(phone: str, lead_type: str) -> None:
    prospecto = obtener_prospecto(phone) or {}
    alerts = prospecto.get("alerts_sent", {})
    
    if isinstance(alerts, str):
        try:
            alerts = json.loads(alerts.replace("'", "\""))
        except:
            alerts = {}
    
    alerts[lead_type] = datetime.utcnow().isoformat()
    actualizar_prospecto(phone, {"alerts_sent": alerts})


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
    """
    Gestiona el envío de la alerta (WhatsApp al ejecutivo) para evitar spam.
    window_minutes: Tiempo mínimo entre alertas del MISMO tipo.
    """
    
    # Lógica extra: Si es solo un agradecimiento ("gracias"), aumentamos la restricción
    msg_lower = last_user_msg.lower().strip()
    if len(msg_lower) < 10 and any(w in msg_lower for w in ["gracias", "ok", "bueno", "listo"]):
        logger.info(f"[ALERT] SKIPPED LOW VALUE MSG: {msg_lower}")
        return

    if not should_send_alert(phone, lead_type, window_minutes):
        logger.info(f"[ALERT] SKIPPED DUPLICATE ALERT {lead_type} for {phone} (Wait {window_minutes}m)")
        return

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
            "rut": criteria.get("rut")
        }

        # 2. ENRUTAMIENTO INTELIGENTE
        # Buscamos quién es el responsable REAL (según reglas JPC, Región, etc.)
        exec_name, exec_phone = find_responsible_executive(lead_data["property_code"])
        
        # --- NUEVO: ASIGNACIÓN ROBUSTA (Enterprise Point 2.1) ---
        # Primero aseguramos la asignación en DB, pase lo que pase con el WhatsApp.
        try:
            from .storage import update_lead_state, log_event, EventType, PipelineStage
            
            update_lead_state(phone, metadata={
                "ejecutivo_asignado": exec_name,
                "prospecto.ejecutivo": exec_name,
                "lifecycle.assigned_at": datetime.utcnow().isoformat() + "Z",
                "metodo_asignacion": "LeadRouter"
            })
            
            # Log de auditoría inmutable
            log_event(phone, EventType.ASSIGNMENT, "system", {
                "executive": exec_name,
                "method": "LeadRouter",
                "property_code": lead_data["property_code"]
            })
            
        except Exception as ex_assign:
            logger.error(f"[ALERT] Critical error in lead assignment: {ex_assign}")

        # 3. VERIFICACIÓN DE HORARIO NOTIFICACIÓN
        if should_send_now():
            # Enviar YA
            message = format_whatsapp_template(lead_data, exec_name, lead_data["property_code"])
            sent = await send_whatsapp_message(exec_phone, message)
            
            if sent:
                logger.info(f"[ALERT] WhatsApp enviado a {exec_name} ({exec_phone}) por lead {phone}")
                mark_alert_sent(phone, lead_type)
                from .storage import log_event, EventType
                log_event(phone, EventType.ALERT_SENT, "system", {"to": exec_name, "type": lead_type})
            else:
                logger.error(f"[ALERT] Falló envío WA a {exec_name}. Guardando para reintento.")
                from .storage import log_event, EventType
                log_event(phone, EventType.ASSIGNMENT_FAIL, "system", {"to": exec_name, "reason": "wasender_failure"})
                save_pending_notification({**lead_data, "target_phone": exec_phone, "target_name": exec_name})
        else:
            # Guardar para mañana
            logger.info(f"[ALERT] Fuera de horario (Actual: {datetime.now()}). Guardando lead {phone} para {exec_name}.")
            save_pending_notification({**lead_data, "target_phone": exec_phone, "target_name": exec_name})
            mark_alert_sent(phone, lead_type) 

    except Exception as e:
        logger.error(f"[ALERT] ERROR routing alert: {e}", exc_info=True)
