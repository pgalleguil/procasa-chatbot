import logging
import json
import asyncio
import pytz
from datetime import datetime, timedelta
from .storage import obtener_prospecto, actualizar_prospecto, save_pending_notification
from .lead_router import find_responsible_executive, should_send_now, format_whatsapp_template
from .lead_router import find_responsible_executive, should_send_now, format_whatsapp_template
from .constants import CHILE_TZ
from .notification_service import NotificationService

logger = logging.getLogger(__name__)

# --- LOCK PARA EVITAR DUPLICADOS DURANTE EL DELAY ---
# Estructura: {(phone, lead_type): timestamp_inicio}
actively_processing_alerts = {}

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

        # 2. ENRUTAMIENTO INTELIGENTE (SOLO SI NO ESTÁ ASIGNADO)
        # Buscamos quién es el responsable REAL (según reglas JPC, Región, etc.)
        
        from .constants import UNASSIGNED_LABEL
        assigned_exec = criteria.get("ejecutivo_asignado") or criteria.get("ejecutivo")
        
        # Si ya tiene ejecutivo y es válido (no un administrativo genérico), mantenemos al mismo.
        unassigned_labels = [UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", "Sin asignar", "N/A"]
        if assigned_exec and assigned_exec not in unassigned_labels:
             exec_name = assigned_exec
             # Intentamos obtener el teléfono de ese ejecutivo existente
             from .lead_router import get_executive_phone
             exec_phone = get_executive_phone(assigned_exec)
             is_new_assignment = False
             logger.info(f"[ALERT] Lead ya asignado a {exec_name}. Manteniendo asignación.")
        else:
             # Si es nuevo o no tiene asignación válida, corremos el router
             exec_name, exec_phone = find_responsible_executive(lead_data["property_code"])
             is_new_assignment = True
        
        logger.info(f"[ALERT] Ruteo: Ejecutivo determineado: {exec_name} | Teléfono: {exec_phone} | Es nuevo: {is_new_assignment}")

        # --- NUEVO: ASIGNACIÓN ROBUSTA (Enterprise Point 2.1) ---
        # Solo actualizamos DB si es una NUEVA asignación
        if is_new_assignment:
            try:
                from .storage import update_lead_state, log_event, EventType
                from .lead_router import get_next_business_slot
                
                # SLA Protection: Si es fuera de horario, la "atención" empieza en el próximo bloque laboral
                now_cl = datetime.now(CHILE_TZ)
                assigned_at = get_next_business_slot(now_cl)
                
                update_lead_state(phone, metadata={
                    "ejecutivo_asignado": exec_name,
                    "prospecto.ejecutivo": exec_name,
                    "lifecycle.assigned_at": assigned_at.isoformat(),
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

        # 3. DELAY PARA PERMITIR QUE EL BOT RECOJA DATOS (2 MINUTOS)
        # --- EVITAR DUPLICADOS EN COLA DE ESPERA ---
        lock_key = (phone, lead_type)
        now = datetime.now(CHILE_TZ)
        if lock_key in actively_processing_alerts:
            last_lock_time = actively_processing_alerts[lock_key]
            if now - last_lock_time < timedelta(minutes=5):
                logger.info(f"[ALERT] Bloqueado (Memo): Ya hay una alerta '{lead_type}' para {phone} en espera (hace {(now - last_lock_time).seconds}s)")
                return
        
        actively_processing_alerts[lock_key] = now

        # --- MARCAR EN DB ANTES DEL DELAY ---
        # Al marcarlo aquí, should_send_alert() rebotará cualquier otro intento en el futuro inmediato.
        mark_alert_sent(phone, lead_type)

        # ALERT_DELAY_SECONDS = 120 (Restaurado a 2 mins por solicitud del usuario para capturar datos)
        ALERT_DELAY_SECONDS = 120
        logger.info(f"[ALERT] Marcado en DB y esperando {ALERT_DELAY_SECONDS}s antes de notificar a {exec_name} sobre {phone} (Prop: {lead_data['property_code']})...")
        await asyncio.sleep(ALERT_DELAY_SECONDS)
        
        # Recargamos los datos del prospecto DESPUÉS del delay para capturar datos nuevos
        prospecto_actualizado = obtener_prospecto(phone) or {}
        lead_data["nombre"] = prospecto_actualizado.get("nombre") or lead_data.get("nombre") or "Cliente"
        lead_data["email"] = prospecto_actualizado.get("email") or lead_data.get("email")
        lead_data["rut"] = prospecto_actualizado.get("rut") or lead_data.get("rut")
        
        # 4. VERIFICACIÓN DE HORARIO NOTIFICACIÓN
        if should_send_now():
            # Bloqueo de seguridad: No enviar WhatsApp al número dummy
            if exec_phone == "+56900000000":
                logger.warning(f"[ALERT] Bloqueado envío a número dummy (+56900000000). Lead {phone} marcado como Sin Asignar.")
                return

            # --- ANTISPAM ROBUSTO PARA SEGUIMIENTOS ---
            # Si NO es asignación nueva, verificamos ventana de tiempo (ej: 24 horas)
            # para no spamear al ejecutivo cada que el cliente respira.
            if not is_new_assignment:
                # 24 horas = 1440 minutos
                if not should_send_alert(phone, "SeguimientoCliente", window_minutes=1440):
                     logger.info(f"[ALERT] Seguimiento omitido para {phone}. Se ha notificado recientemente al ejecutivo.")
                     return
                else:
                    # Marcamos que enviamos notificación de seguimiento AHORA
                    mark_alert_sent(phone, "SeguimientoCliente")

            # Enviar YA (Usando Servicio Idempotente)
            message = format_whatsapp_template(lead_data, exec_name, lead_data["property_code"], is_new_assignment=is_new_assignment)
            
            # --- NUEVO: ENVÍO CENTRALIZADO ---
            sent = await NotificationService.send_notification(
                phone=exec_phone,
                message=message,
                alert_type=lead_type, # Usamos lead_type como clave de deduplicación
                meta={"to": exec_name, "is_new_assignment": is_new_assignment},
                dedup_window_minutes=10 # 10 minutos de protección extra
            )
            
            if not sent:
                logger.error(f"[ALERT] Falló envío WA a {exec_name}. Guardando para reintento.")
                # Si falló (y no fue por duplicado), guardamos para reintento
                # Nota: NotificationService retorna True si fue duplicado, False si error real.
                from .storage import log_event, EventType
                log_event(phone, EventType.ASSIGNMENT_FAIL, "system", {"to": exec_name, "reason": "wasender_failure"})
                save_pending_notification({**lead_data, "target_phone": exec_phone, "target_name": exec_name})
        else:
            # Guardar para mañana
            now_cl = datetime.now(CHILE_TZ)
            logger.info(f"[ALERT] Fuera de horario (Chile: {now_cl.strftime('%H:%M:%S')}). Guardando lead {phone} para {exec_name}.")
            save_pending_notification({**lead_data, "target_phone": exec_phone, "target_name": exec_name})

    except Exception as e:
        logger.error(f"[ALERT] ERROR routing alert: {e}", exc_info=True)
    finally:
        # Liberar el lock siempre
        actively_processing_alerts.pop((phone, lead_type), None)
