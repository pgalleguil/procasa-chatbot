import logging
import asyncio
from datetime import datetime, timedelta
from .storage import get_async_db, log_event
from .constants import CHILE_TZ, PipelineStage, EventType
from .notification_service import NotificationService
from .lead_router import get_executive_phone, should_send_now
from .utils import calculate_business_minutes

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

    from .constants import UNASSIGNED_LABEL
    db = get_async_db()
    
    # 1. Búsqueda Robusta de Leads en etapas iniciales
    # Incluimos leads donde el stage es NEW, CONTACTED o simplemente NO existe/es null.
    # Excluimos explícitamente cualquier variante de "No Asignado" / "Sin Asignar"
    unassigned_patterns = [
        UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", "Sin asignar", 
        "no asignado", "sin asignar", "N/A", "Desconocido"
    ]
    query = {
        "$or": [
            {"pipeline_stage": {"$in": [PipelineStage.NEW, PipelineStage.CONTACTED]}},
            {"pipeline_stage": None},
            {"pipeline_stage": {"$exists": False}},
            {"stage": {"$in": ["new", "nuevo", "gestion", "contacted"]}},
            {"stage": None},
            {"stage": {"$exists": False}}
        ],
        "ejecutivo_asignado": {"$exists": True, "$nin": unassigned_patterns}
    }
    
    leads = await db["leads"].find(query, {"messages": 0, "stage_history": 0}).to_list(length=2000)
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
    existing_warnings = set(await db["crm_sla_warnings"].distinct("phone", {"phone": {"$in": phones_clean}}))
    warnings_docs = await db["crm_sla_warnings"].find({"phone": {"$in": phones_clean}}).to_list(length=5000)
    warnings_by_phone = {}
    for w in warnings_docs:
        ph = w.get("phone")
        if not ph:
            continue
        if ph not in warnings_by_phone:
            warnings_by_phone[ph] = []
        warnings_by_phone[ph].append(w)

    # 2. Obtener eventos relevantes (notificaciones iniciales y gestiones) para todos
    relevant_event_types = ["alert_sent", "ALERT", "ASSIGNMENT"] + [
        "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
        "CLICK_PHONE_LEAD", "CLICK_WHATSAPP_LEAD", "SEND_WA_OWNER", "SEND_EMAIL_OWNER", 
        "CLICK_PHONE_OWNER", "CLICK_WHATSAPP_OWNER"
    ]
    
    events_by_phone = {}
    cursor = db["crm_events"].find({"phone": {"$in": phones_clean}, "type": {"$in": relevant_event_types}})
    for evt in await cursor.to_list(length=20000):
        ph = evt["phone"]
        if ph not in events_by_phone: events_by_phone[ph] = []
        events_by_phone[ph].append(evt)

    # --- PROCESAMIENTO EN MEMORIA ---
    management_types = [
        "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
        "CLICK_PHONE_LEAD", "CLICK_WHATSAPP_LEAD", "SEND_WA_OWNER", "SEND_EMAIL_OWNER", 
        "CLICK_PHONE_OWNER", "CLICK_WHATSAPP_OWNER"
    ]
    initial_notif_types = ["alert_sent", "ALERT", "ASSIGNMENT", "assignment", "alert"]

    for phone_clean, lead in lead_by_phone.items():
        try:
            # 1. Tiempos de referencia
            raw_assigned = lead.get("lifecycle", {}).get("assigned_at")
            raw_created = lead.get("created_at")
            
            if not raw_assigned: continue 

            try:
                # Usamos start_dt para mantener compatibilidad con el resto del archivo
                if isinstance(raw_assigned, datetime):
                    start_dt = raw_assigned
                else:
                    start_dt = datetime.fromisoformat(str(raw_assigned).replace("Z", ""))
                
                if start_dt.tzinfo is None: start_dt = CHILE_TZ.localize(start_dt)
                
                if isinstance(raw_created, datetime):
                    created_dt = raw_created
                else:
                    created_dt = datetime.fromisoformat(str(raw_created).replace("Z", ""))
                    
                if created_dt.tzinfo is None: created_dt = CHILE_TZ.localize(created_dt)
            except: continue

            # 2. Cargar advertencias existentes para este lead específico
            existing = warnings_by_phone.get(phone_clean, [])
            has_red_warning = any(w.get("level") == "critical" for w in existing)
            has_orange_warning = any(w.get("level") == "near_critical" for w in existing)
            
            if has_red_warning: continue

            # 3. Filtrar eventos por fecha (Gestiones desde creación, Alertas desde asignación)
            phone_events = events_by_phone.get(phone_clean, [])
            current_events = []
            has_management_ever = False

            for e in phone_events:
                e_ts = e.get("timestamp")
                if isinstance(e_ts, str): e_ts = datetime.fromisoformat(e_ts.replace("Z", ""))
                if e_ts.tzinfo is None: e_ts = CHILE_TZ.localize(e_ts)
                
                # ¿Es una gestión? (Buscamos desde creación)
                if e.get("type") in management_types and e_ts >= (created_dt - timedelta(minutes=5)):
                    has_management_ever = True
                
                # ¿Es un evento relevante para el flujo actual? (Desde asignación)
                if e_ts >= (start_dt - timedelta(minutes=1)):
                    current_events.append(e)

            # 4. CRITERIO DE EXCLUSIÓN: Si ya tiene gestión (incluso pre-asignación), NO ALERTAR.
            if has_management_ever:
                if not any(w.get("status") == "ignored_already_managed" for w in existing):
                    await db["crm_sla_warnings"].insert_one({
                        "phone": phone_clean,
                        "status": "ignored_already_managed",
                        "reason": "proactive_management_detected",
                        "timestamp": datetime.now(CHILE_TZ).isoformat()
                    })
                continue

            # 5. Verificar notificación inicial (Cualquier alerta de asignación)
            has_initial = any(e["type"] in initial_notif_types for e in current_events)
            if not has_initial:
                # Caso borde: Si no hay evento pero tiene 'assigned_at' reciente, confiamos en el campo
                if lead.get("lifecycle", {}).get("assigned_at"):
                    has_initial = True
                else:
                    continue

            # diff = datetime.now(CHILE_TZ) - start_dt
            # minutes_diff = diff.total_seconds() / 60
            minutes_diff = calculate_business_minutes(start_dt, datetime.now(CHILE_TZ))
            
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
                
                sent = await NotificationService.send_notification(
                    phone=exec_phone,
                    message=message,
                    alert_type=f"SLA_{level.upper()}",
                    meta={"to": ejecutivo, "level": level},
                    dedup_window_minutes=30
                )

                if sent:
                    await db["crm_sla_warnings"].insert_one({
                        "phone": phone_clean,
                        "executive": ejecutivo,
                        "executive_phone": exec_phone,
                        "sent_at": datetime.now(CHILE_TZ).isoformat(),
                        "level": level,
                        "status": "sent"
                    })
                    logger.info(f"[SLA_MONITOR] Notificación SLA {level} procesada para {ejecutivo}")
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
