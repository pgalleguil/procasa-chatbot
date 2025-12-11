# chatbot/alert_service.py
import logging
from datetime import datetime, timedelta
from .storage import obtener_prospecto, actualizar_prospecto
from .email_utils import send_gmail_alert

logger = logging.getLogger(__name__)

def should_send_alert(phone: str, lead_type: str, window_minutes: int = 10) -> bool:
    """
    Retorna True si NO se ha enviado ya una alerta de este tipo
    en los últimos X minutos.
    """
    prospecto = obtener_prospecto(phone) or {}
    alerts = prospecto.get("alerts_sent", {}) or {}

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
    """
    Marca en el prospecto que se envió una alerta de este tipo.
    """
    prospecto = obtener_prospecto(phone) or {}
    alerts = prospecto.get("alerts_sent", {}) or {}
    alerts[lead_type] = datetime.utcnow().isoformat()
    actualizar_prospecto(phone, {"alerts_sent": alerts})


def send_alert_once(
    phone: str,
    lead_type: str,
    lead_score: int,
    criteria: dict,
    last_response: str,
    last_user_msg: str,
    full_history: list,
    window_minutes: int = 2, # Aumenté un poco el default para evitar spam
    lead_type_label: str | None = None
):
    """
    Gestiona el envío de la alerta:
    1. Verifica si ya se envió recientemente.
    2. Si no, envía el correo.
    3. Marca el envío.
    """
    if not should_send_alert(phone, lead_type, window_minutes):
        logger.info(f"[EMAIL] SKIPPED DUPLICATE ALERT {lead_type} for {phone}")
        return

    try:
        send_gmail_alert(
            phone=phone,
            lead_score=lead_score,
            criteria=criteria or {},
            last_response=last_response,
            last_user_msg=last_user_msg,
            full_history=full_history,
            lead_type=lead_type_label or lead_type
        )
        # Solo marcamos si el envío fue exitoso (o si la función send_gmail_alert maneja sus propios errores)
        mark_alert_sent(phone, lead_type)
        logger.info(f"[EMAIL] ALERT SENT {lead_type} for {phone}")
        
    except Exception as e:
        logger.error(f"[EMAIL] ERROR sending alert: {e}")