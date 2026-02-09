from pymongo import MongoClient
from config import Config
from datetime import datetime
import pytz
import re
import uuid

def get_db():
    client = MongoClient(Config.MONGO_URI)
    return client[Config.DB_NAME]

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
    prop = db["universo_obelix"].find_one({"codigo": str(codigo_propiedad)})
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
        "url": f"https://www.procasa.cl/propiedad/{prop.get('codigo')}"
    }

def detect_property_code(lead):
    code = lead.get("prospecto", {}).get("codigo")
    if code: return code
    code = lead.get("datos_propiedad", {}).get("codigo")
    if code: return code
    return None

def process_chat_timeline(messages):
    processed = []
    if not messages: return []
    for msg in messages:
        role = msg.get("role", "user")
        css_class = "chat-bot" if role in ["assistant", "system"] else "user-message"
        processed.append({
            "role": css_class, 
            "content": msg.get("content", ""),
            "timestamp": msg.get("timestamp")
        })
    return processed

# --- REGISTRO DE EVENTOS (Delegado a storage) ---
from chatbot.storage import log_event # Usamos el logger centralizado
from chatbot.crm_service import CrmService
from chatbot.constants import PipelineStage, InteractionType

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
    
    try: execute_at = datetime.fromisoformat(execute_at_str.replace("Z", ""))
    except: return
    task = {
        "task_id": str(uuid.uuid4()),
        "phone": phone.replace(" ", "").replace("+", "").strip(),
        "type": "REMINDER_WHATSAPP",
        "status": "pending", "execute_at": execute_at, "created_at": datetime.now(), "note": note, "agent": agent
    }
    db["crm_tasks"].insert_one(task)

# --- 1. LISTA DE LEADS (OPTIMIZADA / BULK QUERY) ---
def get_crm_leads_list(filtro_estado=None, busqueda=None, ordenar_por="prioridad", user_role=None, user_name=None, ejecutivo_filter=None):
    db = get_db()
    query_parts = []
    
    # --- FILTRO DE SEGURIDAD (ROL) ---
    # Si NO es admin/supervisor, solo ver sus propios leads
    if user_role not in ["admin", "supervisor"] and user_name:
        regex_name = re.escape(user_name)
        query_parts.append({
            "$or": [
                {"prospecto.ejecutivo": {"$regex": regex_name, "$options": "i"}},
                {"ejecutivo_asignado": {"$regex": regex_name, "$options": "i"}}
            ]
        })
    # Si es admin/supervisor y eligió un ejecutivo específico
    elif ejecutivo_filter and ejecutivo_filter != "Todos":
        regex_exec = re.escape(ejecutivo_filter)
        query_parts.append({
            "$or": [
                {"prospecto.ejecutivo": {"$regex": regex_exec, "$options": "i"}},
                {"ejecutivo_asignado": {"$regex": regex_exec, "$options": "i"}}
            ]
        })

    if busqueda and busqueda.strip():
        term = busqueda.strip()
        regex_term = re.escape(term)
        clean_phone = re.sub(r'\D', '', term)
        query_parts.append({"$or": [
            {"prospecto.codigo": {"$regex": regex_term, "$options": "i"}},
            {"prospecto.nombre": {"$regex": regex_term, "$options": "i"}},
            {"prospecto.ejecutivo": {"$regex": regex_term, "$options": "i"}},
            {"ejecutivo_asignado": {"$regex": regex_term, "$options": "i"}},
            {"phone": {"$regex": clean_phone}}
        ]})
    
    query = {"$and": query_parts} if query_parts else {}
    
    # 1. TRAER LEADS (Ejecutar query inmediata)
    leads_list = list(db["leads"].find(query).limit(200))
    
    # 2. OPTIMIZACIÓN: Obtener lista de teléfonos para hacer UNA SOLA consulta de eventos
    phones_in_page = [l.get("phone", "").replace("+","").strip() for l in leads_list if l.get("phone")]
    
    # 3. BULK QUERY DE EVENTOS (Agregación para obtener el último por teléfono)
    events_map = {}
    if phones_in_page:
        pipeline = [
            {"$match": {
                "phone": {"$in": phones_in_page}, 
                "type": "GESTION_LOG"
            }},
            {"$sort": {"timestamp": -1}},
            {"$group": {
                "_id": "$phone",
                "last_event": {"$first": "$$ROOT"}
            }}
        ]
        # Ejecutamos la agregación rápida
        agg_results = list(db["crm_events"].aggregate(pipeline))
        # Mapeamos para acceso O(1)
        events_map = {r["_id"]: r["last_event"] for r in agg_results}

    leads_procesados = []
    kpi_counts = {"nuevo": 0, "gestion": 0, "visita": 0, "cerrado": 0, "total": 0}
    
    from chatbot.constants import PipelineStage
    
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

    # 4. PROCESAR LEADS EN MEMORIA
    for lead in leads_list:
        raw_phone = lead.get("phone", "").replace("+", "").strip()
        # PRIORIDAD DE ESTADO (Enterprise):
        # 1. pipeline_stage (nuevo campo canónico)
        # 2. stage (campo de transición)
        # 3. crm_estado (legacy)
        # 4. PipelineStage.NEW (default)
        # NOTA: Ignoramos lifecycle.stage que es solo para tracking interno
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
        
        # Recuperar evento desde el mapa en memoria (sin ir a la DB)
        last_action_event = events_map.get(raw_phone)
        
        last_action_text = "Sin gestión aún"
        last_action_note = ""
        
        # PRIORIDAD DE TIMESTAMP para tiempo relativo:
        # 1. ultimo_mensaje (timestamp del último mensaje del chat)
        # 2. lifecycle.assigned_at (cuándo fue asignado)
        # 3. created_at (cuándo se creó el lead)
        ultimo_msg_ts = lead.get("prospecto", {}).get("ultimo_mensaje")
        lifecycle_ts = lead.get("lifecycle", {}).get("assigned_at")
        created_ts = lead.get("created_at")
        
        last_ts = ultimo_msg_ts or lifecycle_ts or created_ts
        
        estado_final = estado_db 
        
        if last_action_event:
            last_ts = last_action_event["timestamp"]
            meta = last_action_event.get("meta", {})
            last_action_text = meta.get("action_label", "Gestión CRM")
            
            if meta.get("notes"):
                last_action_note = meta.get("notes")[:50] + "..."

            # Corrección Visual de Estado
            result_code = meta.get("result", "")
            if result_code == "visita_agendada":
                estado_final = PipelineStage.VISIT_SCHEDULED
            elif result_code == "lead_cerrado":
                estado_final = PipelineStage.CLOSED_WON
            elif result_code in ["lead_pausado", "requiere_seguimiento", "intento_fallido"]:
                estado_final = PipelineStage.CONTACTED
            # ELIMINADO: No forzamos 'nuevo' a 'gestion' solo por existir el lead.
            # Debe permanecer en su estado original hasta que haya acción humana real.
        else:
             msgs = lead.get("messages", [])
             if msgs:
                 last_msg = msgs[-1]
                 # Validación segura de timestamp
                 ts = last_msg.get("timestamp")
                 if ts: last_ts = ts

        # KPIS
        kpi_counts["total"] += 1
        
        # Mapeo de KPI Simplificado
        kpi_key = "gestion" # Default
        if estado_final in [PipelineStage.NEW, "nuevo"]: kpi_key = "nuevo"
        elif estado_final in [PipelineStage.VISIT_SCHEDULED, PipelineStage.VISIT_DONE, "visita"]: kpi_key = "visita"
        elif estado_final in [PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST, "cerrado"]: kpi_key = "cerrado"
        elif estado_final in [PipelineStage.CONTACTED, PipelineStage.INTERESTED, PipelineStage.OFFER, PipelineStage.NEGOTIATION, "gestion"]: kpi_key = "gestion"
        
        if kpi_key in kpi_counts:
            kpi_counts[kpi_key] += 1
        
        if filtro_estado and estado_final != filtro_estado:
            continue

        # Formateo de fecha seguro
        if isinstance(last_ts, str):
            try: last_ts_obj = datetime.fromisoformat(last_ts.replace('Z', ''))
            except: last_ts_obj = datetime.min
        elif isinstance(last_ts, datetime):
            last_ts_obj = last_ts
        else:
            last_ts_obj = datetime.min

        config_estado = state_map.get(estado_final, state_map[PipelineStage.CONTACTED])

        leads_procesados.append({
            "phone": raw_phone,
            "whatsapp_display": f"+{raw_phone}",
            "nombre": lead.get("prospecto", {}).get("nombre") or "Desconocido",
            "estado": estado_final,
            "estado_badge": config_estado["label"],
            "led_class": config_estado["led"],
            "tiempo_relativo": format_relative_time(last_ts_obj),
            "real_timestamp": last_ts_obj,
            "priority_score": config_estado["priority"],
            "codigo_propiedad": detect_property_code(lead) or "S/N",
            "url_propiedad": f"https://www.procasa.cl/propiedad/{detect_property_code(lead)}" if detect_property_code(lead) else "#",
            "ultima_accion_titulo": last_action_text,
            "ultima_accion_nota": last_action_note,
            "ejecutivo_nombre": lead.get("ejecutivo_asignado") or "No asignado",
            "fecha_asignacion_relativa": format_relative_time(lead.get("lifecycle", {}).get("assigned_at") or lead.get("fecha_asignacion")),
            "stage": lead.get("stage") or "new"
        })
    
    def safe_timestamp(dt):
        try: return dt.timestamp()
        except: return 0.0

    if ordenar_por == "prioridad":
        leads_procesados.sort(key=lambda x: (x['priority_score'], -safe_timestamp(x['real_timestamp'])))
    else:
        leads_procesados.sort(key=lambda x: safe_timestamp(x['real_timestamp']), reverse=True)

    return leads_procesados, kpi_counts

def get_unique_executives():
    """Retorna lista de nombres únicos de ejecutivos que tienen leads asignados."""
    db = get_db()
    # Buscamos en ambos campos posibles por legibilidad/historia
    execs_1 = db["leads"].distinct("ejecutivo_asignado")
    execs_2 = db["leads"].distinct("prospecto.ejecutivo")
    
    all_execs = set([e for e in execs_1 if e] + [e for e in execs_2 if e])
    return sorted(list(all_execs))

# --- 2. DETALLE DEL LEAD ---
def get_lead_detail_data(phone):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    lead = db["leads"].find_one({"phone": {"$regex": phone_clean}})
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

    # Se incluyen logs de sistema y gestión para auditoría completa
    new_events_cursor = db["crm_events"].find({
        "phone": phone_clean,
        "type": {"$in": ["GESTION_LOG", "STATUS_CHANGE", "SYSTEM_LOG"]} 
    }).sort("timestamp", -1)
    
    formatted_new_history = []
    for evt in new_events_cursor:
        meta = evt.get("meta", {})
        # Distinción de tipo para UI
        evt_type = evt.get("type")
        display_type = "system" if evt_type == "STATUS_CHANGE" else "user"
        
        formatted_new_history.append({
            "timestamp": evt["timestamp"],
            "user_action": meta.get("action_label", "Evento Sistema") if evt_type != "STATUS_CHANGE" else "Cambio de Estado",
            "result": meta.get("result", ""),
            "notes": meta.get("notes", "") or meta.get("to", ""), 
            "type_class": display_type,
            "raw_type": evt_type,
            "channel": meta.get("interaction_type") # p.ej. 'wa', 'phone', 'email'
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

    return {
        "phone": lead.get("phone"),
        "timeline": timeline,
        "nombre": prospecto.get("nombre", "Desconocido"),
        "email": prospecto.get("email", "No registrado"),
        "rut": prospecto.get("rut", "No registrado"),
        "crm_estado": crm_state,
        "next_action_date": next_task["execute_at"].isoformat() if next_task else None,
        "last_action_label": formatted_new_history[0]["user_action"] if formatted_new_history else "Sin gestión aún",
        "last_action_relative": format_relative_time(formatted_new_history[0]["timestamp"]) if formatted_new_history else None,
        "last_crm_update": lead.get("last_crm_update").isoformat() if lead.get("last_crm_update") else None,
        "crm_history": formatted_new_history, 
        "sticky_notes": lead.get("sticky_notes", []),
        "datos_propiedad": datos_propiedad,
        "last_intent": lead.get("last_intent"),
        "last_intent_at": lead.get("last_intent_at"),
        "ejecutivo_asignado": lead.get("ejecutivo_asignado") # Requerido para RBAC en detalle
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
    if new_state and new_state != old_state:
        # Mapeo de seguridad por si el frontend manda strings viejos
        valid_stage = new_state
        if new_state == "visita": valid_stage = PipelineStage.VISIT_SCHEDULED
        elif new_state == "cerrado": valid_stage = PipelineStage.CLOSED_WON # O Lost, depende del contexto, asumimos flow feliz o el frontend debe ser explícito
        elif new_state == "gestion": valid_stage = PipelineStage.CONTACTED
        
        # Si el frontend ya manda los Enums correctos (ideal), usamos eso.
        # Fallback a lógica de negocio simple:
        
        CrmService.update_stage(phone_clean, valid_stage, actor="agent", notes=data.get("notas"))
        new_state = valid_stage # Actualizamos para el return

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