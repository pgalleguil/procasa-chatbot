import logging
import asyncio
from datetime import datetime, timedelta
from .storage import get_db, log_event
from .constants import CHILE_TZ, PipelineStage, EventType
from .whatsapp_client import send_whatsapp_message
from .lead_router import get_executive_phone, should_send_now

logger = logging.getLogger(__name__)

async def monitor_sla_thresholds():
    """
    Monitorea los leads para detectar aquellos que están próximos a entrar en estado crítico
    (estado Naranja: >= 150 minutos desde la asignación sin gestión).
    """
    if not should_send_now():
        logger.info("[SLA_MONITOR] Fuera de horario comercial. Saltando revisión.")
        return

    db = get_db()
    # 1. Buscar leads que podrían estar en riesgo (Sin Atender o Contactado pero sin gestión real)
    # Buscamos leads asignados que no han sido marcados con advertencia de SLA aún.
    query = {
        "pipeline_stage": {"$in": [PipelineStage.NEW, PipelineStage.CONTACTED]},
        "ejecutivo_asignado": {"$exists": True, "$ne": "No asignado"},
        "sla_warning_sent": {"$ne": True}
    }
    
    leads = list(db["leads"].find(query))
    if not leads:
        return

    logger.info(f"[SLA_MONITOR] Revisando {len(leads)} leads en stages iniciales para SLA...")

    for lead in leads:
        try:
            phone = lead.get("phone")
            if not phone:
                continue
            
            ejecutivo = lead.get("ejecutivo_asignado")
            
            # 2. Verificar si ya hubo gestión humana
            # Si hay eventos de gestión, no enviamos alerta de SLA (ya se atendió)
            management_types = [
                "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
                "CLICK_PHONE_LEAD", "SEND_WA_OWNER", "SEND_EMAIL_OWNER", "CLICK_PHONE_OWNER"
            ]
            
            phone_clean = phone.replace("+", "").replace(" ", "").strip()
            has_mgmt = db["crm_events"].find_one({
                "phone": phone_clean,
                "type": {"$in": management_types}
            })
            
            if has_mgmt:
                # Si ya tiene gestión, marcamos para no revisar más este monitor si sigue en este stage
                db["leads"].update_one({"_id": lead["_id"]}, {"$set": {"sla_warning_sent": True}})
                continue

            # 3. Calcular tiempo transcurrido desde asignación
            start_time = lead.get("lifecycle", {}).get("assigned_at") or lead.get("created_at")
            if not start_time:
                continue

            if isinstance(start_time, str):
                try: 
                    start_dt = datetime.fromisoformat(start_time.replace("Z", ""))
                except: 
                    continue
            else:
                start_dt = start_time
            
            if start_dt.tzinfo is None:
                start_dt = CHILE_TZ.localize(start_dt)
            
            diff = datetime.now(CHILE_TZ) - start_dt
            minutes_diff = diff.total_seconds() / 60
            
            # Umbral Naranja: 150 minutos (2:30 horas)
            if minutes_diff >= 150:
                logger.info(f"[SLA_MONITOR] Alerta! Lead {phone} asignado a {ejecutivo} hace {minutes_diff:.1f} min sin gestión.")
                
                # 4. Obtener teléfono del ejecutivo
                exec_phone = get_executive_phone(ejecutivo)
                if not exec_phone:
                    logger.warning(f"[SLA_MONITOR] No se encontró teléfono para ejecutivo {ejecutivo}")
                    # Marcamos para no reintentar infinitamente si no hay teléfono? 
                    # Preferimos dejarlo por si se actualiza el perfil del usuario
                    continue
                
                if exec_phone == "+56900000000":
                    continue

                # 5. Enviar mensaje de WhatsApp
                nombre_cliente = lead.get("prospecto", {}).get("nombre", "Cliente")
                message = format_sla_warning_message(ejecutivo, nombre_cliente)
                
                sent = await send_whatsapp_message(exec_phone, message)
                if sent:
                    # 6. Marcar como enviado y registrar evento
                    db["leads"].update_one({"_id": lead["_id"]}, {"$set": {"sla_warning_sent": True}})
                    log_event(phone_clean, EventType.ALERT_SENT, "system", {
                        "to": ejecutivo, 
                        "type": "sla_warning",
                        "reason": "near_critical_threshold"
                    })
                    logger.info(f"[SLA_MONITOR] Notificación SLA enviada a {ejecutivo} para lead {phone}")

        except Exception as e:
            logger.error(f"[SLA_MONITOR] Error procesando lead {lead.get('phone')}: {e}")

def format_sla_warning_message(executive_name, client_name):
    """Formatea el mensaje de advertencia de SLA."""
    return (
        f"⚠️ *RECORDATORIO SLA CRÍTICO*\n\n"
        f"Hola *{executive_name}*, el cliente *{client_name}* lleva *2:30 horas* asignado sin recibir gestión comercial.\n\n"
        f"Está por pasar al estado *ROJO (Crítico)* en los indicadores. Por favor, realiza contacto a la brevedad para mejorar el rendimiento del equipo. ⚡\n\n"
        f"🔗 *Accede al CRM para gestionar:* https://procasa-chatbot-yr8d.onrender.com/\n\n"
        f"¡Vamos por esa venta! 🚀"
    )
