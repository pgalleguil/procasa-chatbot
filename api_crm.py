# from pymongo import MongoClient (Replaced by singleton)
from config import Config
from datetime import datetime
import pytz
import re
import uuid
import logging

logger = logging.getLogger(__name__)

try:
    from chatbot.constants import CHILE_TZ
except ImportError:
    import pytz
    CHILE_TZ = pytz.timezone('Chile/Continental')

from chatbot.storage import get_db

def format_relative_time(dt_obj):
    if isinstance(dt_obj, str):
        try: dt_obj = datetime.fromisoformat(dt_obj.replace('Z', ''))
        except: return "S/I"
    
    if not dt_obj or dt_obj == datetime.min: return "S/I"
            
    # Los datos nuevos ya vienen en hora local (Chile/Continental)
    # Los viejos en UTC, pero priorizamos la consistencia local.
    chile_tz = pytz.timezone('Chile/Continental')
    now = datetime.now(chile_tz)
    
    # Asegurar que dt_obj sea aware si no lo es (asumimos local)
    if dt_obj.tzinfo is None:
        dt_obj = chile_tz.localize(dt_obj)
        
    diff = now - dt_obj
    seconds = diff.total_seconds()
    
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    
    if days > 0: return f"Hace {days}d {hours}h"
    elif hours > 0: return f"Hace {hours}h {minutes}m"
    elif minutes > 0: return f"Hace {minutes}m"
    else: return "Ahora"

# --- HELPER: Datos de Propiedad ---
def get_real_property_data(db, codigo_propiedad):
    if not codigo_propiedad or codigo_propiedad == "S/N":
        return None
    prop = db[Config.COLLECTION_NAME].find_one({"codigo": str(codigo_propiedad)})
    if not prop: return None
    return {
        "codigo": prop.get("codigo"),
        "tipo": prop.get("tipo", "Propiedad"),
        "operacion": prop.get("operacion", "Venta"),
        "precio_uf": prop.get("precio_uf") or prop.get("precio", 0),
        "comuna": prop.get("comuna", ""),
        "region": prop.get("region", ""),
        "calle": prop.get("calle", ""),
        "numeracion": prop.get("numeracion", ""),
        "direccion_completa": f"{prop.get('calle', '')} #{prop.get('numeracion', '')}",
        "nombre_propietario": prop.get("nombre_propietario", "No registrado"),
        "movil_propietario": prop.get("movil_propietario") or prop.get("fono_propietario", "S/I"),
        "email_propietario": prop.get("email_propietario", "S/I"),
        "url": f"https://www.procasa.cl/{prop.get('codigo')}"
    }

def detect_property_code(lead):
    p = lead.get("prospecto", {})
    code = p.get("codigo")
    if code: return code
    code = lead.get("datos_propiedad", {}).get("codigo")
    if code: return code
    code = p.get("codigo_yapo")
    if code: return f"Yapo: {code}"
    code = p.get("codigo_mercadolibre")
    if code: return f"ML: {code}"
    return None

def process_chat_timeline(messages):
    processed = []
    if not messages: return []
    for msg in messages:
        role = msg.get("role", "user")
        css_class = "chat-bot" if role in ["assistant", "system"] else "user-message"
        
        ts_obj = msg.get("timestamp")
        if isinstance(ts_obj, str):
            try: ts_obj = datetime.fromisoformat(ts_obj.replace('Z', ''))
            except: ts_obj = datetime.min
        
        if ts_obj is None: ts_obj = datetime.min
            
        processed.append({
            "role": css_class, 
            "content": msg.get("content", ""),
            "timestamp": ts_obj
        })
    return processed

# --- REGISTRO DE EVENTOS (Delegado a storage) ---
from chatbot.storage import log_event # Usamos el logger centralizado
from chatbot.crm_service import CrmService
from chatbot.utils import calculate_business_minutes
from chatbot.constants import PipelineStage, InteractionType, UNASSIGNED_LABEL

# log_crm_event se mantiene como alias por compatibilidad pero usa storage
def log_crm_event(phone, event_type, agent="Sistema", meta_data=None):
    # Adaptador para usar storage.log_event
    return log_event(phone, event_type, agent, meta_data)

def schedule_crm_task(phone, execute_at_str, note, agent="Sistema"):
    if not execute_at_str: return
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    # Resolver tareas previas (Audit consistency)
    db["crm_tasks"].update_many(
        {"phone": phone_clean, "status": "pending"},
        {"$set": {"status": "completed", "resolved_at": datetime.now(), "resolution": "superseded"}}
    )
    
    try: 
        execute_at = datetime.fromisoformat(execute_at_str.replace("Z", ""))
        # Asegurar timezone aware (Chile)
        if execute_at.tzinfo is None:
            execute_at = CHILE_TZ.localize(execute_at)
    except: return
    task = {
        "task_id": str(uuid.uuid4()),
        "phone": phone.replace(" ", "").replace("+", "").strip(),
        "type": "REMINDER_WHATSAPP",
        "status": "pending", "execute_at": execute_at, "created_at": datetime.now(), "note": note, "agent": agent
    }
    db["crm_tasks"].insert_one(task)

# --- 1. LISTA DE LEADS (OPTIMIZADA / BULK QUERY) ---
async def get_crm_leads_list(filtro_estado=None, busqueda=None, ordenar_por="prioridad",
                             user_role="agente", user_name="", ejecutivo_filter=None,
                             page=1, limit=10, cursor_last_event_at=None):
    from chatbot.storage import get_async_db
    db = get_async_db()
    query_parts = []
    
    # --- FILTRO DE SEGURIDAD (ROL) ---
    # Si NO es admin/supervisor, solo ver sus propios leads
    if user_role not in ["admin", "supervisor"] and user_name:
        regex_name = re.compile(re.escape(user_name), re.IGNORECASE)
        query_parts.append({
            "$or": [
                {"prospecto.ejecutivo": regex_name},
                {"ejecutivo_asignado": regex_name}
            ]
        })
    # Si es admin/supervisor y eligió un ejecutivo específico
    elif ejecutivo_filter and ejecutivo_filter != "Todos":
        regex_exec = re.compile(re.escape(ejecutivo_filter), re.IGNORECASE)
        query_parts.append({
            "$or": [
                {"prospecto.ejecutivo": regex_exec},
                {"ejecutivo_asignado": regex_exec}
            ]
        })

    if busqueda and busqueda.strip():
        term = busqueda.strip()
        # Limpiar caracteres no numéricos para búsqueda exacta por teléfono
        clean_phone = re.sub(r'\D', '', term)
        if clean_phone:
            regex_phone = re.compile(re.escape(clean_phone))
            query_parts.append({"phone": regex_phone})
        else:
            # Búsqueda por nombre si no es teléfono
            regex_term = re.compile(re.escape(term), re.IGNORECASE)
            query_parts.append({"prospecto.nombre": regex_term})
    
    query = {"$and": query_parts} if query_parts else {}
    
    # 2. SEPARATE KPI COUNTS (Globales para la búsqueda actual pero sin filtro de estado)
    base_kpi_query = query.copy() # Query que incluye ejecutivo y término de búsqueda
    
    # --- FILTRO DE ESTADO ---
    query_with_state = query.copy()
    UNASSIGNED_VALUES = [None, "", "Sin Asignar", "No asignado", "No Asignado", "Sin asignar"]
    
    if filtro_estado and filtro_estado != "Todos":
        if filtro_estado == "UNASSIGNED":
            # Caso especial: Sin Asignar (Nuevos sin ejecutivo)
            query_with_state["pipeline_stage"] = {"$in": [PipelineStage.NEW, None, "nuevo", "new"]}
            query_with_state["$or"] = [{"ejecutivo_asignado": {"$in": UNASSIGNED_VALUES}}, {"ejecutivo_asignado": {"$exists": False}}]
        else:
            # Mapeo invertido para buscar por el valor del Enum o string legacy en la DB
            state_db_value = filtro_estado
            if filtro_estado in ["nuevo", "NEW"]: 
                state_db_value = {"$in": [PipelineStage.NEW, None, "nuevo", "new"]}
                # IMPORTANTE: Para el listado "Sin Atender", también excluimos los no asignados
                query_with_state["ejecutivo_asignado"] = {"$nin": UNASSIGNED_VALUES, "$exists": True}
            elif filtro_estado == "visita": state_db_value = PipelineStage.VISIT_SCHEDULED
            elif filtro_estado == "gestion": state_db_value = PipelineStage.CONTACTED
            elif filtro_estado == "cerrado": state_db_value = PipelineStage.CLOSED_WON
            query_with_state["pipeline_stage"] = state_db_value

    # 1. EJECUCION DE KPIs OPTIMIZADA CON $FACET (1 solo roundtrip a MongoDB)
    import time
    t_kpis = time.perf_counter()
    
    assigned_filter = {"ejecutivo_asignado": {"$nin": UNASSIGNED_VALUES, "$exists": True}}
    unassigned_filter = {"$or": [{"ejecutivo_asignado": {"$in": UNASSIGNED_VALUES}}, {"ejecutivo_asignado": {"$exists": False}}]}

    # Pipeline de $facet consolida los 7 queries en 1 sola operación en el motor de base de datos
    facet_pipeline = [
        {"$match": base_kpi_query},
        {"$facet": {
            "global_total": [{"$count": "count"}],
            "total_pagina": [{"$match": query_with_state}, {"$count": "count"}],
            "sin_asignar": [
                {"$match": {"$and": [{"pipeline_stage": {"$in": [PipelineStage.NEW, None, "nuevo", "new"]}}, unassigned_filter]}},
                {"$count": "count"}
            ],
            "nuevo": [
                {"$match": {"$and": [{"pipeline_stage": {"$in": [PipelineStage.NEW, None, "nuevo", "new"]}}, assigned_filter]}},
                {"$count": "count"}
            ],
            "gestion": [
                {"$match": {"pipeline_stage": {"$in": [PipelineStage.CONTACTED, PipelineStage.INTERESTED, PipelineStage.OFFER, PipelineStage.NEGOTIATION, "gestion", "contacted"]}}},
                {"$count": "count"}
            ],
            "visita": [
                {"$match": {"pipeline_stage": {"$in": [PipelineStage.VISIT_SCHEDULED, PipelineStage.VISIT_DONE, "visita"]}}},
                {"$count": "count"}
            ],
            "cerrado": [
                {"$match": {"pipeline_stage": {"$in": [PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST, "cerrado"]}}},
                {"$count": "count"}
            ]
        }}
    ]

    import asyncio
    # Ejecutamos el pipeline asíncronamente
    facet_cursor = db["leads"].aggregate(facet_pipeline)
    facet_results = await facet_cursor.to_list(length=1)
    
    facet_res = facet_results[0] if facet_results else {}
    
    def get_facet_count(key):
        return facet_res.get(key, [{"count": 0}])[0]["count"] if facet_res.get(key) else 0

    total_count = get_facet_count("total_pagina")
    
    kpi_counts = {
        "total": get_facet_count("global_total"), 
        "nuevo": get_facet_count("nuevo"), 
        "gestion": get_facet_count("gestion"), 
        "visita": get_facet_count("visita"), 
        "cerrado": get_facet_count("cerrado"), 
        "sin_asignar": get_facet_count("sin_asignar")
    }
    logger.info(f"[PERF] get_crm_leads_list -> $facet(7x KPIs): {(time.perf_counter()-t_kpis)*1000:.1f}ms")

    # ------------------------------------------------------------------
    # 3. TRAER LEADS DESDE MONGO — CURSOR-BASED PURO (O(1))
    # ------------------------------------------------------------------
    # No hay skip. La página se simula en el frontend con total_count.
    # El cursor es el valor de last_event_at del último item visible.
    #
    # Primer carga (cursor_last_event_at=None): trae los más recientes.
    # Carga siguiente: trae los que tienen last_event_at < cursor.
    # ------------------------------------------------------------------
    # Siempre usamos created_at como cursor principal porque el usuario quiere orden por asignaci\u00f3n m\u00e1s reciente
    sort_criteria = [("created_at", -1)]
    cursor_field = "created_at"

    # Proyección mínima — solo campos necesarios para el listado
    PROJECTION = {
        "phone": 1,
        "prospecto.nombre": 1,
        "prospecto.ejecutivo": 1,
        "prospecto.codigo": 1,
        "prospecto.codigo_yapo": 1,
        "prospecto.codigo_mercadolibre": 1,
        "prospecto.ultimo_mensaje": 1,
        "pipeline_stage": 1,
        "stage": 1,
        "crm_estado": 1,
        "ejecutivo_asignado": 1,
        "last_event_at": 1,
        "last_action_label": 1,
        "priority_score": 1,
        "sla_status": 1,
        "lifecycle": 1,
        "created_at": 1,
        "fecha_asignacion": 1,
        "datos_propiedad.codigo": 1,
    }

    paginated_query = query_with_state.copy()
    if cursor_last_event_at:
        try:
            cursor_dt = datetime.fromisoformat(cursor_last_event_at.replace("Z", "+00:00"))
            # El cursor puede estar guardado como Datetime o como String ISO en la base de datos
            cursor_condition = {
                "$or": [
                    {cursor_field: {"$lt": cursor_dt}},
                    {cursor_field: {"$lt": cursor_last_event_at}}
                ]
            }
            if "$and" in paginated_query:
                # Hacer una copia superficial de la lista $and para no mutar el dict original
                paginated_query["$and"] = list(paginated_query["$and"])
                paginated_query["$and"].append(cursor_condition)
            else:
                paginated_query["$and"] = [cursor_condition]
        except Exception as e:
            logger.warning(f"CRM: cursor inválido ignorado: {e}")
            # Si el cursor es inválido, arranca desde el principio (seguro)

    leads_cursor = db["leads"].find(paginated_query, PROJECTION)\
                              .sort(sort_criteria)\
                              .limit(limit)
    leads_list = await leads_cursor.to_list(length=limit)

    leads_procesados = []
    # (KPI counts are already calculated via optimized MongoDB queries above)

    # 4b. BULK QUERY DE EVENTOS para los leads de ESTA PÁGINA solamente (máx 10-20 teléfonos)
    # Esto es O(page_size), no O(total_leads). Correcto y eficiente.
    page_phones = [l.get("phone", "").replace("+", "").strip() for l in leads_list]
    management_types = [
        "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD",
        "CLICK_PHONE_LEAD", "CLICK_WHATSAPP_LEAD", "SEND_WA_OWNER", "SEND_EMAIL_OWNER",
        "CLICK_PHONE_OWNER", "CLICK_WHATSAPP_OWNER", "ALERT_SENT", "alert_sent"
    ]
    events_cursor = db["crm_events"].find(
        {"phone": {"$in": page_phones}, "type": {"$in": management_types}},
        sort=[("timestamp", -1)]
    )
    events_list = await events_cursor.to_list(length=200)
    events_map = {}
    for ev in events_list:
        phone_ev = ev.get("phone", "")
        if phone_ev not in events_map:
            events_map[phone_ev] = ev

    type_labels = {
        "CLICK_WHATSAPP_LEAD": "Click WhatsApp (Lead)",
        "CLICK_PHONE_LEAD": "Llamada Iniciada",
        "CLICK_EMAIL_LEAD": "Click Email (Lead)",
        "SEND_WA_LEAD": "WhatsApp Enviado",
        "SEND_EMAIL_LEAD": "Email Enviado",
        "CLICK_WHATSAPP_OWNER": "Click WhatsApp (Prop)",
        "CLICK_PHONE_OWNER": "Llamada Prop. Iniciada",
        "CLICK_EMAIL_OWNER": "Click Email (Prop)",
        "SEND_WA_OWNER": "WhatsApp Enviado (Prop)",
        "SEND_EMAIL_OWNER": "Email Enviado (Prop)",
        "STATUS_CHANGE": "Cambio de Estado",
        "HUMAN_NOTE": "Gestión Manual",
        "ASSIGNMENT": "Lead Asignado",
        "GESTION_LOG": "Gestión Registrada",
        "ALERT_SENT": "Alerta Enviada",
        "MANUAL_ENTRY": "Ingreso Manual",
    }

    # 5. PROCESAR LEADS EN MEMORIA
    state_map = {
        # Enums
        PipelineStage.NEW:   {"label": "Sin Atender", "led": "led-red",    "priority": 1},
        PipelineStage.CONTACTED: {"label": "En Gestión",  "led": "led-yellow", "priority": 3},
        PipelineStage.INTERESTED: {"label": "Interesado",  "led": "led-yellow", "priority": 3},
        PipelineStage.VISIT_SCHEDULED:  {"label": "Visita Agendada", "led": "led-green",  "priority": 2},
        PipelineStage.VISIT_DONE:  {"label": "Visita Realizada", "led": "led-green",  "priority": 2},
        PipelineStage.OFFER:  {"label": "Oferta", "led": "led-green",  "priority": 2},
        PipelineStage.NEGOTIATION:  {"label": "Negociación", "led": "led-green",  "priority": 2},
        PipelineStage.CLOSED_WON: {"label": "Cerrado Ganado",     "led": "led-gray",   "priority": 4},
        PipelineStage.CLOSED_LOST: {"label": "Cerrado Perdido",     "led": "led-gray",   "priority": 4},
        # Legacy Support
        "nuevo":   {"label": "Sin Atender", "led": "led-red",    "priority": 1},
        "visita":  {"label": "Visita Agendada", "led": "led-green",  "priority": 2},
        "gestion": {"label": "En Gestión",  "led": "led-yellow", "priority": 3},
        "cerrado": {"label": "Cerrado",     "led": "led-gray",   "priority": 4}
    }
    # Tipos de eventos considerados como gestión humana válida
    management_types = [
        "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
        "CLICK_PHONE_LEAD", "CLICK_WHATSAPP_LEAD", "SEND_WA_OWNER", "SEND_EMAIL_OWNER", 
        "CLICK_PHONE_OWNER", "CLICK_WHATSAPP_OWNER", "ALERT_SENT", "alert_sent"
    ]

    # 4. PROCESAR LEADS EN MEMORIA
    for lead in leads_list:
        raw_phone = lead.get("phone", "").replace("+", "").strip()
        estado_db = lead.get("pipeline_stage") or lead.get("stage") or lead.get("crm_estado") or PipelineStage.NEW
        
        # Normalizar strings legacy a Enums
        if isinstance(estado_db, str):
            estado_map_legacy = {
                "nuevo": PipelineStage.NEW,
                "new": PipelineStage.NEW,
                "contacted": PipelineStage.CONTACTED,
                "gestion": PipelineStage.CONTACTED,
                "visita": PipelineStage.VISIT_SCHEDULED,
                "cerrado": PipelineStage.CLOSED_WON
            }
            estado_db = estado_map_legacy.get(estado_db.lower(), PipelineStage.NEW)
        
        last_ev = events_map.get(raw_phone)
        if last_ev:
            last_action_text = type_labels.get(last_ev.get("type"), "Acción registrada")
            last_action_note = last_ev.get("metadata", {}).get("note", "")
        else:
            last_action_text = lead.get("last_action_label") or "Sin gestión aún"
            last_action_note = ""
        
        ultimo_msg_ts = lead.get("prospecto", {}).get("ultimo_mensaje")
        lifecycle_ts = lead.get("lifecycle", {}).get("assigned_at")
        created_ts = lead.get("created_at")
        
        # Determine original fallback (Prioritize Assignment over Message for SLA consistency)
        # We now use the precomputed last_event_at if available
        last_ts = lead.get("last_event_at") or (last_ev.get("timestamp") if last_ev else None) or lifecycle_ts or ultimo_msg_ts or created_ts
        
        estado_final = estado_db
        
        # Promoción visual de estado: si tiene gestión pero DB dice NEW, mostrar como CONTACTADO
        # Esto es visual solamente — no modifica la DB
        MANAGEMENT_LABELS = {
            "Click WhatsApp (Lead)", "Llamada Iniciada", "WhatsApp Enviado",
            "Email Enviado", "Click WhatsApp (Prop)", "Llamada Prop. Iniciada",
            "WhatsApp Enviado (Prop)", "Email Enviado (Prop)", "Cambio de Estado",
            "Gestión Manual", "Gestión Registrada"
        }
        has_management = last_action_text in MANAGEMENT_LABELS
        
        # Si hay gestión real registrada, ya no debe mostrarse como "Sin Atender".
        if estado_final == PipelineStage.NEW and has_management:
            estado_final = PipelineStage.CONTACTED

        # En filtro NEW/Sin Atender excluimos del listado los que ya tuvieron gestión.
        if filtro_estado in ["NEW", "nuevo"] and has_management:
            continue

        
        # Identificar ejecutivo y timestamp real para visualización
        ejecutivo = lead.get("ejecutivo_asignado") or lead.get("prospecto", {}).get("ejecutivo")
        
        if last_ts:
            try: 
                if isinstance(last_ts, str):
                    last_ts_obj = datetime.fromisoformat(last_ts.replace('Z', '+00:00'))
                else:
                    last_ts_obj = last_ts
            except: last_ts_obj = datetime.now(CHILE_TZ)
        else:
            last_ts_obj = datetime.now(CHILE_TZ)

        # (SaaS Performance: Metrics are precomputed in lead doc)
        config_estado = state_map.get(estado_final, state_map[PipelineStage.CONTACTED])

        # 5. SLA / TIEMPO DE RESPUESTA
        # Corrección funcional: para "Sin Atender" priorizamos SIEMPRE el cálculo real
        # con minutos hábiles desde asignación para no mostrar falsos "En tiempo".
        sla_status = lead.get("sla_status", "good")
        if estado_final == PipelineStage.NEW:
            start_time = lead.get("lifecycle", {}).get("assigned_at") or lead.get("created_at")
            if start_time:
                try:
                    if isinstance(start_time, str):
                        dt_start = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                    else:
                        dt_start = start_time
                    if dt_start.tzinfo is None:
                        dt_start = CHILE_TZ.localize(dt_start)
                    else:
                        dt_start = dt_start.astimezone(CHILE_TZ)

                    mins = calculate_business_minutes(dt_start, datetime.now(CHILE_TZ))
                    sla_hours = mins / 60.0

                    if sla_hours <= 1.5:
                        sla_status = "good"
                    elif sla_hours < 3.0:
                        sla_status = "near_critical"
                    else:
                        sla_status = "critical"
                except Exception:
                    sla_status = lead.get("sla_status", "good")
            else:
                sla_status = "good"
        else:
            sla_status = "fulfilled"
            
        sla_labels_map = {
            "critical": "Crítico",
            "near_critical": "Próximo a Crítico",
            "warning": "Advertencia",
            "good": "En tiempo",
            "pending": "Pendiente Asignación",
            "fulfilled": "Gestionado"
        }
        sla_label = sla_labels_map.get(sla_status, "En tiempo")
        
        # Re-check pending if no executive
        if not ejecutivo or ejecutivo in [UNASSIGNED_LABEL, "No asignado", "Sin Asignar", "Sin asignar"]:
             sla_status = "pending"
             sla_label = "Pendiente Asignación"

        leads_procesados.append({
            "phone": raw_phone,
            "sla_status": sla_status,
            "sla_label": sla_label,
            "whatsapp_display": f"+{raw_phone}",
            "nombre": lead.get("prospecto", {}).get("nombre") or "Desconocido",
            "estado": estado_final,
            "estado_badge": config_estado["label"],
            "led_class": config_estado["led"],
            "tiempo_relativo": format_relative_time(last_ts_obj),
            "real_timestamp": last_ts_obj,
            "created_timestamp": lead.get("created_at"),
            "priority_score": config_estado["priority"],
            "codigo_propiedad": detect_property_code(lead) or "S/N",
            "url_propiedad": f"https://www.procasa.cl/{detect_property_code(lead)}" if detect_property_code(lead) else "#",
            "ultima_accion_titulo": last_action_text,
            "ultima_accion_note": last_action_note,
            "ejecutivo_nombre": ejecutivo or UNASSIGNED_LABEL,
            "fecha_asignacion_relativa": format_relative_time(lead.get("lifecycle", {}).get("assigned_at") or lead.get("fecha_asignacion")),
            "stage": lead.get("stage") or "new"
        })
    
    # 5. RETORNAR RESULTADOS
    return leads_procesados, kpi_counts, total_count

import time
_executives_cache = {"data": [], "expires_at": 0}

async def get_unique_executives():
    """Retorna lista de nombres únicos de ejecutivos que tienen leads asignados. Cacheado por 5 minutos."""
    global _executives_cache
    if time.time() < _executives_cache["expires_at"]:
        return _executives_cache["data"]

    from chatbot.storage import get_async_db
    adb = get_async_db()
    # Fast-path: usar colección usuarios (mucho menor que leads).
    users = await adb["usuarios"].find(
        {"rol": {"$in": ["agente", "supervisor", "admin"]}},
        {"nombre": 1}
    ).to_list(length=500)
    all_execs = set(str(u.get("nombre", "")).strip() for u in users if u.get("nombre"))
    # Fallback si usuarios viene vacío.
    if not all_execs:
        import asyncio
        execs_1, execs_2 = await asyncio.gather(
            adb["leads"].distinct("ejecutivo_asignado"),
            adb["leads"].distinct("prospecto.ejecutivo")
        )
        all_execs = set([e for e in execs_1 if e] + [e for e in execs_2 if e])
    
    # Limpieza para que el filtro no muestre duplicados
    cleaned_execs = set()
    for e in all_execs:
        words = str(e).strip().split()
        if len(words) > 2:
            cleaned_execs.add(f"{words[0]} {words[1]}")
        else:
            cleaned_execs.add(str(e).strip())
            
    result = sorted(list(cleaned_execs))
    _executives_cache["data"] = result
    _executives_cache["expires_at"] = time.time() + 300
    return result

# --- 2. DETALLE DEL LEAD ---
def get_lead_detail_data(phone, property_code=None):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    query = {"phone": {"$regex": phone_clean}}
    if property_code:
        query["$or"] = [
            {"prospecto.codigo": property_code},
            {"prospecto.codigo": str(property_code)},
            {"datos_propiedad.codigo": property_code},
            {"datos_propiedad.codigo": str(property_code)}
        ]
        
    lead = db["leads"].find_one(query, sort=[("created_at", -1)])
    if not lead: return None
    
    codigo = detect_property_code(lead)
    datos_propiedad = get_real_property_data(db, codigo)
    
    if not datos_propiedad:
        p = lead.get("prospecto", {})
        datos_propiedad = {
            "codigo": codigo or "S/N",
            "nombre_propietario": p.get("owner_name", "Propietario No Asignado"),
            "movil_propietario": p.get("owner_phone", "S/I"),
            "precio_uf": p.get("precio", "0"),
            "comuna": p.get("comuna", ""),
            "calle": p.get("direccion", ""),
            "url": "#"
        }

    # Se incluyen logs de gestión real para un historial limpio y útil
    new_events_cursor = db["crm_events"].find({
        "phone": phone_clean,
        "type": {"$in": [
            "GESTION_LOG", "STATUS_CHANGE", "HUMAN_NOTE", 
            "CLICK_PHONE_LEAD", "CLICK_PHONE_OWNER",
            "SEND_WA_LEAD", "SEND_EMAIL_LEAD",
            "SEND_WA_OWNER", "SEND_EMAIL_OWNER",
            "ALERT_SENT", "alert_sent", "msg_out"
        ]} 
    }).sort("timestamp", -1)
    
    formatted_new_history = []
    for evt in new_events_cursor:
        meta = evt.get("meta", {})
        # Distincion de tipo para UI y filtro de ruido
        evt_type = evt.get("type")
        if evt_type in ["CLICK_WHATSAPP_LEAD", "ASSIGNMENT", "assignment", "MANUAL_ENTRY", "CLICK_WHATSAPP_OWNER"]:
            continue
            
        display_type = "system" if evt_type == "STATUS_CHANGE" else "user"
        
        ts_obj = evt["timestamp"]
        if isinstance(ts_obj, str):
            try: ts_obj = datetime.fromisoformat(ts_obj.replace('Z', ''))
            except: ts_obj = datetime.min
        
        if ts_obj is None: ts_obj = datetime.min
            
        # ETIQUETAS DINÁMICAS PARA EL HISTORIAL (Mejorado para evitar "Evento CRM")
        type_labels = {
            "CLICK_PHONE_LEAD": "Llamada Iniciada",
            "CLICK_PHONE_OWNER": "Llamada Iniciada (Prop.)",
            "SEND_WA_LEAD": "WhatsApp Enviado",
            "SEND_EMAIL_LEAD": "Email Enviado",
            "SEND_WA_OWNER": "WhatsApp Enviado (Prop.)",
            "SEND_EMAIL_OWNER": "Email Enviado (Prop.)",
            "STATUS_CHANGE": "Cambio de Estado",
            "GESTION_LOG": "Gestión Registrada",
            "HUMAN_NOTE": meta.get("action_label", "Nota de Gestión"),
            "ASSIGNMENT": "Asignación de Lead",
            "assignment": "Asignación de Lead",
            "ALERT_SENT": "Alerta Enviada",
            "alert_sent": "Alerta Enviada",
            "MANUAL_ENTRY": "Ingreso Manual",
            "msg_out": "Respuesta Bot"
        }

        user_action_display = meta.get("action_label") or type_labels.get(evt_type, "Actividad")
        
        # --- MAPEO DE ICONOS DINÁMICOS ---
        # Formato: (Icono, Clase CSS)
        icon_map = {
            "CLICK_WHATSAPP_LEAD": ("fa-brands fa-whatsapp", "tl-wa"),
            "CLICK_PHONE_LEAD": ("fa-solid fa-phone", "tl-phone"),
            "CLICK_EMAIL_LEAD": ("fa-solid fa-envelope", "tl-email"),
            "SEND_WA_LEAD": ("fa-brands fa-whatsapp", "tl-wa"),
            "SEND_EMAIL_LEAD": ("fa-solid fa-envelope", "tl-email"),
            "CLICK_WHATSAPP_OWNER": ("fa-brands fa-whatsapp", "tl-wa"),
            "SEND_WA_OWNER": ("fa-brands fa-whatsapp", "tl-wa"),
            "STATUS_CHANGE": ("fa-solid fa-right-left", "tl-status"),
            "HUMAN_NOTE": ("fa-solid fa-note-sticky", "tl-note"),
            "GESTION_LOG": ("fa-solid fa-clipboard-check", "tl-note"),
            "ASSIGNMENT": ("fa-solid fa-user-check", "tl-status"),
            "MANUAL_ENTRY": ("fa-solid fa-user-plus", "tl-status")
        }
        
        # Valores por defecto
        final_icon, final_class = icon_map.get(evt_type, ("fa-solid fa-check", ""))
        
        # Especialización por canal (sobrescribe tipo base)
        channel = meta.get("interaction_type") or meta.get("channel")
        if channel == 'wa':
            final_icon, final_class = icon_map["SEND_WA_LEAD"]
        elif channel == 'phone':
            final_icon, final_class = icon_map["CLICK_PHONE_LEAD"]
        elif channel == 'email':
            final_icon, final_class = icon_map["SEND_EMAIL_LEAD"]

        # Especialización por resultado en HUMAN_NOTE
        res = str(meta.get("result", "")).lower()
        if evt_type == "HUMAN_NOTE":
            if "visita" in res:
                final_icon, final_class = "fa-solid fa-calendar-check", "tl-visit"
            elif "ganado" in res:
                final_icon, final_class = "fa-solid fa-trophy", "tl-win"
            elif any(x in res for x in ["perdido", "descartado", "inválido", "cerrado"]):
                final_icon, final_class = "fa-solid fa-ban", "tl-loss"

        formatted_new_history.append({
            "timestamp": ts_obj,
            "user_action": user_action_display if evt_type != "STATUS_CHANGE" else "Cambio de Estado",
            "result": meta.get("result", ""),
            "notes": meta.get("notes", "") or meta.get("to", "") or meta.get("content_preview", ""), 
            "type_class": display_type,
            "raw_type": evt_type,
            "icon": final_icon,
            "icon_class": final_class,
            "channel": channel
        })
        
    timeline = process_chat_timeline(lead.get("messages", []))
    prospecto = lead.get("prospecto", {})

    # Buscar próxima tarea pendiente (Auditoría Canónica)
    next_task = db["crm_tasks"].find_one({
        "phone": phone_clean,
        "status": "pending"
    }, sort=[("execute_at", 1)])

    # Prioridad al stage nuevo
    crm_state = lead.get("stage") or lead.get("crm_estado") or "new"

    # Priority over legacy assignment naming 
    ejec_asignado = lead.get("ejecutivo_asignado") or prospecto.get("ejecutivo")
    if ejec_asignado and isinstance(ejec_asignado, str):
        words = ejec_asignado.strip().split()
        if len(words) > 2:
            ejec_asignado = f"{words[0]} {words[1]}"

    return {
        "phone": lead.get("phone"),
        "timeline": timeline,
        "nombre": prospecto.get("nombre", "Desconocido"),
        "email": prospecto.get("email", "No registrado"),
        "rut": prospecto.get("rut", "No registrado"),
        "crm_estado": crm_state,
        "next_action_date": next_task["execute_at"].isoformat() if next_task and isinstance(next_task["execute_at"], datetime) else (next_task["execute_at"] if next_task else None),
        "last_action_label": formatted_new_history[0]["user_action"] if formatted_new_history else "Sin gestión aún",
        "last_action_relative": format_relative_time(formatted_new_history[0]["timestamp"]) if formatted_new_history else None,
        "last_crm_update": lead.get("last_crm_update").isoformat() if isinstance(lead.get("last_crm_update"), datetime) else lead.get("last_crm_update"),
        "crm_history": formatted_new_history, 
        "sticky_notes": lead.get("sticky_notes", []),
        "datos_propiedad": datos_propiedad,
        "last_intent": lead.get("last_intent"),
        "last_intent_at": lead.get("last_intent_at"),
        "ejecutivo_asignado": ejec_asignado # Requerido para RBAC en detalle
    }

# --- 3. ACTUALIZAR LEAD (CON VALIDACIÓN ESTRICTA) ---
def update_lead_crm_data(phone, data):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    current_lead = db["leads"].find_one({"phone": {"$regex": phone_clean}})
    if not current_lead: return False
    
    # --- VALIDACIÓN DEL TRIÁNGULO DE CONTROL (CRITICA 1 & 3) ---
    interaction_type = data.get("interaction_type")
    result = data.get("resultado_gestion")
    next_date = data.get("next_action_date")
    
    # Regla: Si hablé, OBLIGATORIO definir siguiente paso o cerrar
    if interaction_type == "hable" and result != "lead_cerrado":
        if not next_date:
            # Rechazar gestión incompleta (Backend Enforcement)
            print(f"⚠️ RECHAZADO: Intento de guardar 'Hablé' sin próxima fecha. Lead: {phone_clean}")
            return False 
    
    new_state = data.get("estado_calculado")
    if not new_state:
        res = data.get("resultado_gestion")
        if res == "visita_agendada": new_state = "visita"
        elif res == "lead_cerrado": new_state = "cerrado"
        elif res in ["lead_pausado", "requiere_seguimiento", "intento_fallido"]: new_state = "gestion"
        else: new_state = "gestion"

    old_state = current_lead.get("stage") or current_lead.get("crm_estado", PipelineStage.NEW)
    
    # 1. ACTUALIZACIÓN DE ESTADO VIA SERVICE (Prioridad Absoluta)
    # Forzamos promoción si es NEW y hay gestión
    if (new_state == old_state) and (old_state == PipelineStage.NEW or str(old_state).lower() in ["nuevo", "new"]):
        new_state = "gestion"

    if new_state and new_state != old_state:
        # Mapeo de seguridad por si el frontend manda strings viejos
        valid_stage = new_state
        if new_state == "visita": valid_stage = PipelineStage.VISIT_SCHEDULED
        elif new_state == "cerrado": valid_stage = PipelineStage.CLOSED_WON
        elif new_state == "gestion": valid_stage = PipelineStage.CONTACTED
        
        CrmService.update_stage(phone_clean, valid_stage, actor="agent", notes=data.get("notas"))
        new_state = valid_stage 

    # Agendar tarea solo si hay fecha válida
    if next_date:
        schedule_crm_task(phone_clean, next_date, data.get("notas"))
    elif new_state in [PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST]:
        # Cleanup: Si se cierra el lead, resolver tareas pendientes
        db["crm_tasks"].update_many(
            {"phone": phone_clean, "status": "pending"},
            {"$set": {"status": "completed", "resolved_at": datetime.now(), "resolution": "lead_closed"}}
        )

    # Log de gestión comercial (Acción User) -> Usamos el log centralizado
    log_event(phone_clean, InteractionType.HUMAN_NOTE, "agent", {
        "interaction_type": interaction_type,
        "result": result,
        "notes": data.get("notas"),
        "action_label": data.get("action_label"),
        "details_json": data.get("details_json", {})
    })
    
    # NOTA: No actualizamos "crm_estado" manual en DB, update_stage ya lo hizo.
    # Solo actualizamos last_crm_update si no hubo cambio de estado (si hubo, update_stage lo hizo)
    if new_state == old_state:
         db["leads"].update_one(
            {"phone": {"$regex": phone_clean}},
            {"$set": {"last_crm_update": datetime.now()}} # Mantenemos datetime.now() para sorting interno de mongo si se usa
        )

    return {
        "status": "ok",
        "new_state": new_state,
        "next_action_date": next_date,
        "event_id": "centralized_log"
    }

def manage_crm_notes(phone, note_data, action="add"):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    if action == "add":
        note_id = str(uuid.uuid4())[:8]
        note = {
            "id": note_id, 
            "content": note_data.get("content"), 
            "color": note_data.get("color"), 
            "created_at_str": datetime.now().strftime("%d/%m/%Y"),
            "timestamp_iso": datetime.now().isoformat()
        }
        db["leads"].update_one({"phone": {"$regex": phone_clean}}, {"$push": {"sticky_notes": note}})
        return note
    elif action == "delete":
        db["leads"].update_one({"phone": {"$regex": phone_clean}}, {"$pull": {"sticky_notes": {"id": note_data.get("id")}}})
        return True
    return False


# --- BÚSQUEDA SEMÁNTICA DE PROPIEDADES ---
def get_semantic_recommendations(query: str, exclude_codes: list = None, limit: int = 3, scope: str = 'local', include_neighbors: bool = False):
    """
    Busca propiedades semánticamente similares a la descripción del cliente.
    Usa embeddings + cosine similarity con filtros estructurados + fallback geográfico.
    scope='local' -> Solo INMOBILIARIA SUCRE SPA
    scope='global' -> Toda la red
    """
    try:
        from chatbot.rag import buscar_semanticamente
        
        oficina = "INMOBILIARIA SUCRE SPA" if scope == 'local' else None
        
        results = buscar_semanticamente(query, limit=limit, exclude_codes=exclude_codes, oficina_filtro=oficina, include_neighbors=include_neighbors)
        return {"status": "ok", "results": results, "count": len(results)}
    except Exception as e:
        logger.error(f"[SEMANTIC] Error en búsqueda semántica: {e}", exc_info=True)
        return {"status": "error", "detail": str(e), "results": []}


def log_recommendation_sent(phone: str, selected_properties: list, user_email: str):
    """
    Registra en crm_history cuando un ejecutivo envía una recomendación de propiedades.
    """
    try:
        db = get_db()
        from datetime import datetime
        now = datetime.utcnow()

        # Build summary of properties
        prop_summary = ", ".join([
            f"{p.get('tipo', 'Prop')} {p.get('codigo', '?')} ({p.get('comuna', '?')})"
            for p in selected_properties
        ])

        history_entry = {
            "timestamp": now,
            "user_action": "Recomendación de propiedades",
            "result": f"Envió {len(selected_properties)} propiedades por WhatsApp",
            "notes": prop_summary,
            "type_class": "recommendation",
            "icon_class": "semantic",
            "icon": "fa-solid fa-brain",
            "source": "crm_semantic",
            "exec_user": user_email
        }

        db.leads.update_one(
            {"phone": phone},
            {
                "$push": {"crm_history": {"$each": [history_entry], "$position": 0}},
                "$inc": {"semantic_search_count": 1}
            }
        )
        logger.info(f"[SEMANTIC] Recomendación registrada para {phone}: {prop_summary}")
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"[SEMANTIC] Error registrando recomendación: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}


