# chatbot/alert_service.py
import logging
import json
from datetime import datetime, timedelta
from .storage import obtener_prospecto, actualizar_prospecto
from .email_utils import send_gmail_alert

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


def send_alert_once(
    phone: str,
    lead_type: str,
    lead_score: int,
    criteria: dict,
    last_response: str,
    last_user_msg: str,
    full_history: list,
    window_minutes: int = 1, # DEFAULT AUMENTADO A 60 MINUTOS
    lead_type_label: str | None = None
):
    """
    Gestiona el envío de la alerta para evitar spam.
    window_minutes: Tiempo mínimo entre correos del MISMO tipo.
    """
    
    # Lógica extra: Si es solo un agradecimiento ("gracias"), aumentamos la restricción
    # para evitar duplicar alertas que no aportan valor.
    msg_lower = last_user_msg.lower().strip()
    if len(msg_lower) < 10 and any(w in msg_lower for w in ["gracias", "ok", "bueno", "listo"]):
        logger.info(f"[EMAIL] SKIPPED LOW VALUE MSG: {msg_lower}")
        return

    if not should_send_alert(phone, lead_type, window_minutes):
        logger.info(f"[EMAIL] SKIPPED DUPLICATE ALERT {lead_type} for {phone} (Wait {window_minutes}m)")
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
        # Marcamos envío exitoso
        mark_alert_sent(phone, lead_type)
        print(f"[EMAIL] Enviado a ejecutivo | Score: {lead_score} | Tipo: {lead_type}")

    except Exception as e:
        logger.error(f"[EMAIL] ERROR sending alert: {e}")