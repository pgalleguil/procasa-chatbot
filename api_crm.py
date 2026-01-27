from pymongo import MongoClient
from config import Config
from datetime import datetime
import re
import uuid

def get_db():
    client = MongoClient(Config.MONGO_URI)
    return client[Config.DB_NAME]

def format_relative_time(dt_obj):
    if not dt_obj: return "S/I"
    now = datetime.now()
    diff = now - dt_obj
    seconds = diff.total_seconds()
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    if days > 0: return f"Hace {days}d {hours}h"
    elif hours > 0: return f"Hace {hours}h {minutes}m"
    elif minutes > 0: return f"Hace {minutes}m"
    else: return "Ahora"

# --- NUEVO: REGISTRO DE EVENTOS (SLA & REPORTING) ---
def log_crm_event(phone, event_type, agent="Sistema", meta_data=None):
    """
    Guarda eventos en 'crm_events' para reportes de SLA y conversión.
    Estructura plana y rápida de consultar.
    """
    db = get_db()
    event = {
        "phone": phone.replace(" ", "").replace("+", "").strip(),
        "timestamp": datetime.now(),
        "type": event_type,       # Ej: 'CALL_OUT', 'WHATSAPP_SENT', 'STATUS_CHANGE', 'NOTE_ADDED'
        "agent": agent,
        "meta": meta_data or {}   # Aquí guardamos el detalle técnico (duración, notas, resultado)
    }
    db["crm_events"].insert_one(event)

# --- NUEVO: REGISTRO DE TAREAS (AUTOMATIZACIÓN) ---
def schedule_crm_task(phone, execute_at_str, note, agent="Sistema"):
    """
    Guarda recordatorios en 'crm_tasks' para que el Bot los procese automáticamente.
    """
    if not execute_at_str: return
    db = get_db()
    try:
        # Intenta parsear ISO format, asume que viene limpio del frontend
        execute_at = datetime.fromisoformat(execute_at_str.replace("Z", ""))
    except:
        return

    task = {
        "task_id": str(uuid.uuid4()),
        "phone": phone.replace(" ", "").replace("+", "").strip(),
        "type": "REMINDER_WHATSAPP",
        "status": "pending",  # pending -> processing -> sent
        "execute_at": execute_at,
        "created_at": datetime.now(),
        "note": note,
        "agent": agent
    }
    db["crm_tasks"].insert_one(task)

# --- 1. LISTA DE LEADS (Solo lectura optimizada) ---
def get_crm_leads_list(filtro_estado=None, busqueda=None, ordenar_por="prioridad"):
    db = get_db()
    query = {"messages.intencion": "agendar_visita"}
    if filtro_estado: query["crm_estado"] = filtro_estado

    if busqueda and busqueda.strip():
        term = busqueda.strip()
        regex_term = re.escape(term)
        query["$and"] = [
            {"messages.intencion": "agendar_visita"},
            {"$or": [
                {"prospecto.codigo": {"$regex": regex_term, "$options": "i"}},
                {"prospecto.nombre": {"$regex": regex_term, "$options": "i"}},
                {"phone": {"$regex": re.sub(r'\D', '', term)}}
            ]}
        ]

    # Ordenamiento básico
    sort_criteria = [("last_message_time", -1)]
    
    leads_cursor = db["conversaciones_whatsapp"].find(query).sort(sort_criteria).limit(100)
    
    leads_procesados = []
    kpi_counts = {"nuevo": 0, "gestion": 0, "visita": 0, "total": 0}
    
    for lead in leads_cursor:
        estado = lead.get("crm_estado", "nuevo")
        kpi_counts["total"] += 1
        if estado in kpi_counts: kpi_counts[estado] += 1
        
        leads_procesados.append({
            "phone": lead.get("phone"),
            "nombre": lead.get("prospecto", {}).get("nombre") or "Desconocido",
            "estado": estado,
            "tiempo_relativo": format_relative_time(lead.get("last_message_time")),
            "led_class": "led-red" if estado == "nuevo" else "led-green"
        })
        
    return leads_procesados, kpi_counts

# --- 2. DETALLE DEL LEAD (Fusión de Datos) ---
def get_lead_detail_data(phone):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    # 1. Buscar Lead Base
    lead = db["conversaciones_whatsapp"].find_one({"phone": {"$regex": phone_clean}})
    if not lead: return None
    
    # 2. Buscar Historial Nuevo (En crm_events)
    # Buscamos eventos que sean de gestión para mostrar en el timeline visual
    new_events_cursor = db["crm_events"].find({
        "phone": phone_clean, 
        "type": {"$in": ["GESTION_LOG", "STATUS_CHANGE", "CALL_OUT", "WHATSAPP_OUT"]}
    }).sort("timestamp", -1)
    
    formatted_new_history = []
    for evt in new_events_cursor:
        meta = evt.get("meta", {})
        formatted_new_history.append({
            "timestamp": evt["timestamp"],
            "interaction_type": meta.get("interaction_type", "log"),
            "result": meta.get("result", ""),
            "notes": meta.get("notes", ""),
            "user_action": meta.get("action_label", evt["type"])
        })

    # 3. Fusionar con historial antiguo (Legacy) si existe dentro del documento
    legacy_history = lead.get("crm_history", [])
    if isinstance(legacy_history, list):
        # Unimos ambas listas
        full_history = formatted_new_history + legacy_history
    else:
        full_history = formatted_new_history
        
    # Ordenar historial combinado por fecha descendente
    try:
        full_history.sort(key=lambda x: x.get('timestamp') or datetime.min, reverse=True)
    except:
        pass # Si falla ordenamiento por algún formato raro, lo dejamos como está

    # Datos básicos
    prospecto = lead.get("prospecto", {})
    
    return {
        "phone": lead.get("phone"),
        "nombre": prospecto.get("nombre", "Desconocido"),
        "email": prospecto.get("email", "No registrado"),
        "rut": prospecto.get("rut", "No registrado"),
        "origen": prospecto.get("origen", "Desconocido"), 
        "propiedad_texto": f"{prospecto.get('operacion', '')} {prospecto.get('comuna', '')}",
        "crm_estado": lead.get("crm_estado", "nuevo"),
        "crm_history": full_history, # Enviamos la fusión para que el frontend no cambie
        "sticky_notes": lead.get("sticky_notes", []),
        "datos_propiedad": lead.get("datos_propiedad", {})
    }

# --- 3. ACTUALIZAR LEAD (Escritura Distribuida) ---
def update_lead_crm_data(phone, data):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    # A. Detectar cambio de estado para Log de Auditoría
    current_lead = db["conversaciones_whatsapp"].find_one({"phone": {"$regex": phone_clean}})
    old_state = current_lead.get("crm_estado", "nuevo")
    new_state = data.get("estado")
    
    if old_state != new_state:
        log_crm_event(phone_clean, "STATUS_CHANGE", meta_data={
            "from": old_state, "to": new_state, "action_label": "Cambio de Estado"
        })

    # B. Guardar Recordatorio (Si existe) en Colección Tareas
    if data.get("next_action_date"):
        schedule_crm_task(
            phone=phone_clean,
            execute_at_str=data.get("next_action_date"),
            note=data.get("notas") or "Seguimiento agendado"
        )

    # C. Registrar el Evento de Gestión en Colección Eventos
    log_crm_event(phone_clean, "GESTION_LOG", meta_data={
        "interaction_type": data.get("interaction_type"),
        "result": data.get("resultado_gestion"),
        "notes": data.get("notas"),
        "action_label": data.get("action_label"),
        "details_json": data # Guardamos todo el payload técnico por si acaso
    })

    # D. Actualizar Estado Actual en Lead (Documento Ligero)
    # Ya NO hacemos $push a crm_history aquí. Solo estado actual.
    update_fields = {
        "crm_estado": new_state,
        "last_crm_update": datetime.now(),
        "crm_propietario_estado": data.get("propietario_res"),
        # "crm_next_action_date": ... (Ya no es necesario aquí, está en tasks, pero puedes dejarlo si quieres referencia rapida)
    }
    
    db["conversaciones_whatsapp"].update_one(
        {"phone": {"$regex": phone_clean}},
        {"$set": update_fields}
    )
    return True

# --- 4. GESTIONAR NOTAS ADHESIVAS ---
def manage_crm_notes(phone, note_data, action="add"):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    if action == "add":
        note_id = str(uuid.uuid4())[:8]
        note = {
            "id": note_id,
            "content": note_data.get("content"),
            "color": note_data.get("color"),
            "created_at": datetime.now(),
            "created_at_str": datetime.now().strftime("%d/%m/%Y")
        }
        # Notas rápidas SÍ se quedan en el lead porque son de UI
        db["conversaciones_whatsapp"].update_one(
            {"phone": {"$regex": phone_clean}},
            {"$push": {"sticky_notes": note}}
        )
        # Logueamos la acción
        log_crm_event(phone_clean, "NOTE_ADDED", meta_data={"note_id": note_id})
        return note
        
    elif action == "delete":
        # Lógica de borrado (opcional, si la necesitas)
        pass
    return False