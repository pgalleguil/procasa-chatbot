import logging
import asyncio
from datetime import datetime
from .storage import get_db, log_event
from .constants import CHILE_TZ, PipelineStage, EventType
from .whatsapp_client import send_whatsapp_message
from .lead_router import get_executive_phone, should_send_now

logger = logging.getLogger(__name__)

async def monitor_sla_thresholds():
    """
    Monitorea los leads para detectar aquellos que están próximos a entrar en estado crítico
    (estado Naranja: >= 150 minutos desde la asignación sin gestión).
    Usa la colección 'crm_sla_warnings' para evitar duplicados.
    """
    if not should_send_now():
        logger.info("[SLA_MONITOR] Fuera de horario comercial. Saltando revisión.")
        return

    db = get_db()
    
    # 1. Búsqueda Robusta de Leads en etapas iniciales
    # Incluimos leads donde el stage es NEW, CONTACTED o simplemente NO existe/es null.
    query = {
        "$or": [
            {"pipeline_stage": {"$in": [PipelineStage.NEW, PipelineStage.CONTACTED]}},
            {"pipeline_stage": None},
            {"pipeline_stage": {"$exists": False}},
            {"stage": {"$in": ["new", "nuevo", "gestion", "contacted"]}},
            {"stage": None},
            {"stage": {"$exists": False}}
        ],
        "ejecutivo_asignado": {"$exists": True, "$ne": "No asignado"}
    }
    
    leads = list(db["leads"].find(query))
    if not leads:
        return

    logger.info(f"[SLA_MONITOR] Revisando {len(leads)} leads potenciales para SLA...")

    for lead in leads:
        try:
            phone = lead.get("phone")
            if not phone:
                continue
            
            phone_clean = phone.replace("+", "").replace(" ", "").strip()
            
            # 2. Verificar si ya se envió advertencia para ESTE lead en la nueva colección
            # Esto desacopla la lógica del documento principal del lead
            warning_exists = db["crm_sla_warnings"].find_one({"phone": phone_clean})
            if warning_exists:
                continue

            # 3. Verificar si ya se envió la notificación inicial de nuevo lead (Requisito)
            # Solo enviamos alerta de SLA si el ejecutivo ya fue avisado previamente.
            initial_notif = db["crm_events"].find_one({
                "phone": phone_clean,
                "type": {"$in": ["alert_sent", "ALERT", "ASSIGNMENT"]}
            })
            
            if not initial_notif:
                # Si no se ha notificado la asignación inicial, no enviamos SLA aún
                continue

            # 4. Verificar si ya hubo gestión humana (Doble chequeo de seguridad)
            management_types = [
                "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
                "CLICK_PHONE_LEAD", "SEND_WA_OWNER", "SEND_EMAIL_OWNER", "CLICK_PHONE_OWNER"
            ]
            
            has_mgmt = db["crm_events"].find_one({
                "phone": phone_clean,
                "type": {"$in": management_types}
            })
            
            if has_mgmt:
                # Si ya tiene gestión, marcamos en la colección de advertencias 
                # para no procesarlo más como "pendiente de alerta"
                db["crm_sla_warnings"].insert_one({
                    "phone": phone_clean,
                    "status": "ignored_already_managed",
                    "timestamp": datetime.now(CHILE_TZ).isoformat()
                })
                continue

            # 4. Calcular tiempo transcurrido desde asignación
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
                ejecutivo = lead.get("ejecutivo_asignado")
                logger.info(f"[SLA_MONITOR] Alerta! Lead {phone} asignado a {ejecutivo} hace {minutes_diff:.1f} min sin gestión.")
                
                # 5. Obtener teléfono del ejecutivo
                exec_phone = get_executive_phone(ejecutivo)
                if not exec_phone or exec_phone == "+56900000000":
                    continue

                # 6. Enviar mensaje de WhatsApp
                nombre_cliente = lead.get("prospecto", {}).get("nombre", "Cliente")
                message = format_sla_warning_message(ejecutivo, nombre_cliente)
                
                sent = await send_whatsapp_message(exec_phone, message)
                if sent:
                    # 7. Registrar en la nueva colección de advertencias
                    db["crm_sla_warnings"].insert_one({
                        "phone": phone_clean,
                        "executive": ejecutivo,
                        "executive_phone": exec_phone,
                        "sent_at": datetime.now(CHILE_TZ).isoformat(),
                        "status": "sent"
                    })
                    
                    # 8. Log de evento en el historial del lead
                    log_event(phone_clean, EventType.ALERT_SENT, "system", {
                        "to": ejecutivo, 
                        "type": "sla_warning",
                        "reason": "near_critical_threshold"
                    })
                    logger.info(f"[SLA_MONITOR] Notificación SLA enviada a {ejecutivo} para lead {phone}")
                    
                    # 9. Esperar para evitar rate limit (Account Protection: 5s)
                    await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"[SLA_MONITOR] Error crítico procesando lead {lead.get('phone')}: {e}", exc_info=True)

def format_sla_warning_message(executive_name, client_name):
    """Formatea el mensaje de advertencia de SLA."""
    return (
        f"⚠️ *RECORDATORIO SLA CRÍTICO*\n\n"
        f"Hola *{executive_name}*, el cliente *{client_name}* lleva *2:30 horas* asignado sin recibir gestión comercial.\n\n"
        f"Está por pasar al estado *ROJO (Crítico)* en los indicadores. Por favor, realiza contacto a la brevedad para mejorar el rendimiento del equipo. ⚡\n\n"
        f"🔗 *Accede al CRM para gestionar:* https://procasa-chatbot-yr8d.onrender.com/\n\n"
        f"¡Vamos por esa venta! 🚀"
    )
