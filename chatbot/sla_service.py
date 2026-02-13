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
        logger.debug("[SLA_MONITOR] Fuera de horario comercial. Saltando revisión.")
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
        "ejecutivo_asignado": {"$exists": True, "$nin": ["No asignado", "Sin Asignar"]}
    }
    
    leads = list(db["leads"].find(query))
    if not leads:
        return

    logger.debug(f"[SLA_MONITOR] Revisando {len(leads)} leads potenciales para SLA...")

    # --- OPTIMIZACIÓN: BULK QUERIES ---
    phones_clean = []
    lead_by_phone = {}
    for l in leads:
        p = l.get("phone")
        if p:
            pc = p.replace("+", "").replace(" ", "").strip()
            phones_clean.append(pc)
            lead_by_phone[pc] = l

    # 1. Obtener todas las advertencias existentes de una vez
    existing_warnings = set(db["crm_sla_warnings"].distinct("phone", {"phone": {"$in": phones_clean}}))

    # 2. Obtener eventos relevantes (notificaciones iniciales y gestiones) para todos
    relevant_event_types = ["alert_sent", "ALERT", "ASSIGNMENT"] + [
        "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
        "CLICK_PHONE_LEAD", "SEND_WA_OWNER", "SEND_EMAIL_OWNER", "CLICK_PHONE_OWNER"
    ]
    
    events_by_phone = {}
    cursor = db["crm_events"].find({"phone": {"$in": phones_clean}, "type": {"$in": relevant_event_types}})
    for evt in cursor:
        ph = evt["phone"]
        if ph not in events_by_phone: events_by_phone[ph] = []
        events_by_phone[ph].append(evt)

    # --- PROCESAMIENTO EN MEMORIA ---
    management_types = [
        "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
        "CLICK_PHONE_LEAD", "SEND_WA_OWNER", "SEND_EMAIL_OWNER", "CLICK_PHONE_OWNER"
    ]

    for phone_clean, lead in lead_by_phone.items():
        try:
            # A. Saltar si ya tiene advertencia
            if phone_clean in existing_warnings:
                continue

            phone_events = events_by_phone.get(phone_clean, [])
            
            # B. Verificar notificación inicial
            has_initial = any(e["type"] in ["alert_sent", "ALERT", "ASSIGNMENT"] for e in phone_events)
            if not has_initial:
                continue

            # C. Verificar gestión humana
            has_mgmt = any(e["type"] in management_types for e in phone_events)
            if has_mgmt:
                db["crm_sla_warnings"].insert_one({
                    "phone": phone_clean,
                    "status": "ignored_already_managed",
                    "timestamp": datetime.now(CHILE_TZ).isoformat()
                })
                continue

            # D. Calcular tiempo
            start_time = lead.get("lifecycle", {}).get("assigned_at") or lead.get("created_at")
            if not start_time: continue

            if isinstance(start_time, str):
                try: start_dt = datetime.fromisoformat(start_time.replace("Z", ""))
                except: continue
            else:
                start_dt = start_time
            
            if start_dt.tzinfo is None:
                start_dt = CHILE_TZ.localize(start_dt)
            
            diff = datetime.now(CHILE_TZ) - start_dt
            minutes_diff = diff.total_seconds() / 60
            
            # Umbral Naranja: 150 minutos
            if minutes_diff >= 150:
                ejecutivo = lead.get("ejecutivo_asignado")
                logger.info(f"[SLA_MONITOR] Alerta! Lead {phone_clean} asignado a {ejecutivo} hace {minutes_diff:.1f} min sin gestión.")
                
                exec_phone = get_executive_phone(ejecutivo)
                if not exec_phone or exec_phone == "+56900000000":
                    continue

                nombre_cliente = lead.get("prospecto", {}).get("nombre", "Cliente")
                message = format_sla_warning_message(ejecutivo, nombre_cliente)
                
                sent = await send_whatsapp_message(exec_phone, message)
                if sent:
                    db["crm_sla_warnings"].insert_one({
                        "phone": phone_clean,
                        "executive": ejecutivo,
                        "executive_phone": exec_phone,
                        "sent_at": datetime.now(CHILE_TZ).isoformat(),
                        "status": "sent"
                    })
                    
                    log_event(phone_clean, EventType.ALERT_SENT, "system", {
                        "to": ejecutivo, 
                        "type": "sla_warning",
                        "reason": "near_critical_threshold"
                    })
                    logger.info(f"[SLA_MONITOR] Notificación SLA enviada a {ejecutivo} para lead {phone_clean}")
                    await asyncio.sleep(2) # Reducido sleep para bulk

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
