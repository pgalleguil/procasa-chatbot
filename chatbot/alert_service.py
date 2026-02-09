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
    window_minutes: int = 3, # DEFAULT AUMENTADO A 60 MINUTOS
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
        
        if not exec_phone:
            logger.warning(f"[ALERT] No se encontró teléfono para ejecutivo {exec_name}. Lead: {phone}")
            # FALTA: ¿Se debe enviar a un admin por defecto? Por ahora lo guardamos como pendiente o logueamos.
            # Podríamos enviarlo siempre a pendiente si no hay teléfono, pero el requerimiento dice "pendiente si fuera de horario".
            # Asumiremos que si no hay teléfono, no podemos enviar WA.
            return 

        # 3. VERIFICACIÓN DE HORARIO
        if should_send_now():
            # Enviar YA
            message = format_whatsapp_template(lead_data, exec_name, lead_data["property_code"])
            sent = await send_whatsapp_message(exec_phone, message)
            if sent:
                logger.info(f"[ALERT] WhatsApp enviado a {exec_name} ({exec_phone}) por lead {phone}")
                mark_alert_sent(phone, lead_type)
            else:
                logger.error(f"[ALERT] Falló envío WA a {exec_name}. Guardando para reintento.")
                save_pending_notification({**lead_data, "target_phone": exec_phone, "target_name": exec_name})
        else:
            # Guardar para mañana
            logger.info(f"[ALERT] Fuera de horario. Guardando lead {phone} para {exec_name}.")
            save_pending_notification({**lead_data, "target_phone": exec_phone, "target_name": exec_name})
            mark_alert_sent(phone, lead_type) # Marcamos como "procesado" para no spamear, aunque se envíe mañana

    except Exception as e:
        logger.error(f"[ALERT] ERROR routing alert: {e}", exc_info=True)
