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
    
    leads = list(db["leads"].find(query, {"messages": 0, "stage_history": 0}))
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
    initial_notif_types = ["alert_sent", "ALERT", "ASSIGNMENT", "assignment", "alert"]

    for phone_clean, lead in lead_by_phone.items():
        try:
            # 1. Calcular tiempo inicial (Asegurar que tenemos referencia antes de filtrar eventos)
            raw_start = lead.get("lifecycle", {}).get("assigned_at") or lead.get("created_at")
            if not raw_start: continue

            try:
                if isinstance(raw_start, str): 
                    start_dt = datetime.fromisoformat(raw_start.replace("Z", ""))
                else: 
                    start_dt = raw_start
                if start_dt.tzinfo is None: start_dt = CHILE_TZ.localize(start_dt)
            except: continue

            # 2. Filtrar eventos por fecha (SOLO eventos posteriores a la asignación actual)
            phone_events = events_by_phone.get(phone_clean, [])
            current_events = []
            for e in phone_events:
                e_ts = e.get("timestamp")
                if isinstance(e_ts, str): e_ts = datetime.fromisoformat(e_ts.replace("Z", ""))
                if e_ts.tzinfo is None: e_ts = CHILE_TZ.localize(e_ts)
                
                # Tolerancia de 1 minuto para capturar el ASSIGNMENT que ocurre casi al mismo tiempo
                if e_ts >= (start_dt - timedelta(minutes=1)):
                    current_events.append(e)

            # 3. Verificar si ya tiene advertencia CRÍTICA (Red)
            # Si ya se envió roja, no molestamos más.
            # Si se envió solo naranja, permitimos una roja más tarde.
            existing = list(db["crm_sla_warnings"].find({"phone": phone_clean}))
            has_red_warning = any(w.get("level") == "critical" for w in existing)
            has_orange_warning = any(w.get("level") == "near_critical" for w in existing)

            if has_red_warning: continue

            # 4. Verificar notificación inicial (Cualquier alerta de asignación)
            has_initial = any(e["type"] in initial_notif_types for e in current_events)
            if not has_initial:
                # Caso borde: Si no hay evento pero tiene 'assigned_at' reciente, confiamos en el campo
                if lead.get("lifecycle", {}).get("assigned_at"):
                    has_initial = True
                else:
                    continue

            # 5. Verificar gestión humana RECIENTE
            has_recent_mgmt = any(e["type"] in management_types for e in current_events)
            if has_recent_mgmt:
                # Marcar como gestionado para no procesar en el próximo loop
                if not any(w.get("status") == "ignored_already_managed" for w in existing):
                    db["crm_sla_warnings"].insert_one({
                        "phone": phone_clean,
                        "status": "ignored_already_managed",
                        "timestamp": datetime.now(CHILE_TZ).isoformat()
                    })
                continue

            diff = datetime.now(CHILE_TZ) - start_dt
            minutes_diff = diff.total_seconds() / 60
            
            level = None
            if minutes_diff >= 180:
                level = "critical"
            elif minutes_diff >= 150 and not has_orange_warning:
                level = "near_critical"

            if level:
                ejecutivo = lead.get("ejecutivo_asignado")
                logger.info(f"[SLA_MONITOR] Alerta {level.upper()}! Lead {phone_clean} asignado a {ejecutivo} hace {minutes_diff:.1f} min sin gestión.")
                
                exec_phone = get_executive_phone(ejecutivo)
                if not exec_phone or exec_phone == "+56900000000":
                    continue

                nombre_cliente = lead.get("prospecto", {}).get("nombre", "Cliente")
                message = format_sla_warning_message(ejecutivo, nombre_cliente, level)
                
                sent = await send_whatsapp_message(exec_phone, message)
                if sent:
                    db["crm_sla_warnings"].insert_one({
                        "phone": phone_clean,
                        "executive": ejecutivo,
                        "executive_phone": exec_phone,
                        "sent_at": datetime.now(CHILE_TZ).isoformat(),
                        "level": level,
                        "status": "sent"
                    })
                    
                    log_event(phone_clean, "ALERT_SENT", "system", {
                        "to": ejecutivo, 
                        "level": level,
                        "reason": f"{level}_threshold"
                    })
                    logger.info(f"[SLA_MONITOR] Notificación SLA {level} enviada a {ejecutivo}")
                    await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"[SLA_MONITOR] Error procesando lead {phone_clean}: {e}")

def format_sla_warning_message(executive_name, client_name, level):
    """Formatea el mensaje de advertencia de SLA según el nivel."""
    if level == "critical":
        header = "🔴 *SLA CRÍTICO - SIN RESPUESTA* 🔴"
        time_text = "más de 3 horas"
        footer = "⚠️ Este lead requiere atención URGENTE."
    else:
        header = "🟠 *PRÓXIMO A CRÍTICO - ALERTA SLA* 🟠"
        time_text = "2:30 horas"
        footer = "Por favor, contacta al cliente pronto para evitar indicadores rojos."

    return (
        f"{header}\n\n"
        f"Hola *{executive_name}*, el cliente *{client_name}* lleva *{time_text}* asignado sin recibir gestión comercial.\n\n"
        f"{footer}\n\n"
        f"🔗 *Gestionar ahora:* https://procasa-chatbot-yr8d.onrender.com/\n\n"
        f"¡Mucho éxito! 🚀"
    )
