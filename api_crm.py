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
    if isinstance(dt_obj, str):
        try: dt_obj = datetime.fromisoformat(dt_obj.replace('Z', ''))
        except: return "S/I"
            
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

# --- HELPER: Obtener datos reales de 'universo_obelix' ---
def get_real_property_data(db, codigo_propiedad):
    """
    Busca la información completa en la colección universo_obelix
    usando el código de propiedad.
    """
    if not codigo_propiedad or codigo_propiedad == "S/N":
        return None

    # Buscamos exacto o por texto
    prop = db["universo_obelix"].find_one({"codigo": str(codigo_propiedad)})
    
    if not prop:
        return None

    # Mapeamos los campos de universo_obelix a lo que espera el Frontend
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
        
        # Datos Propietario (CRUCIALES)
        "nombre_propietario": prop.get("nombre_propietario", "No registrado"),
        "movil_propietario": prop.get("movil_propietario") or prop.get("fono_propietario", "S/I"),
        "email_propietario": prop.get("email_propietario", "S/I"),
        
        # Link para verla (opcional)
        "url": f"https://www.procasa.cl/propiedad/{prop.get('codigo')}"
    }

# --- HELPER: Detectar Código en Chat ---
def detect_property_code(lead):
    # 1. Mirar en prospecto (más fiable)
    code = lead.get("prospecto", {}).get("codigo")
    if code: return code

    # 2. Mirar en datos_propiedad antiguos
    code = lead.get("datos_propiedad", {}).get("codigo")
    if code: return code

    # 3. Escanear chat (último recurso)
    messages = lead.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            # Buscar patrones como "Código 55268" o "código Procasa 55268"
            match = re.search(r'(?:código|cod|propiedad)\s*(?:procasa)?\s*[:#]?\s*(\d{4,6})', content, re.IGNORECASE)
            if match:
                return match.group(1)
    return None

# --- HELPER: Procesar Chat (Bot vs User) ---
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

# --- REGISTRO DE EVENTOS ---
def log_crm_event(phone, event_type, agent="Sistema", meta_data=None):
    db = get_db()
    event = {
        "phone": phone.replace(" ", "").replace("+", "").strip(),
        "timestamp": datetime.now(),
        "type": event_type, "agent": agent, "meta": meta_data or {}
    }
    db["crm_events"].insert_one(event)

def schedule_crm_task(phone, execute_at_str, note, agent="Sistema"):
    if not execute_at_str: return
    db = get_db()
    try: execute_at = datetime.fromisoformat(execute_at_str.replace("Z", ""))
    except: return
    task = {
        "task_id": str(uuid.uuid4()),
        "phone": phone.replace(" ", "").replace("+", "").strip(),
        "type": "REMINDER_WHATSAPP",
        "status": "pending", "execute_at": execute_at, "created_at": datetime.now(), "note": note, "agent": agent
    }
    db["crm_tasks"].insert_one(task)

# --- 1. LISTA DE LEADS ---
def get_crm_leads_list(filtro_estado=None, busqueda=None, ordenar_por="prioridad"):
    db = get_db()
    query_parts = []
    
    if filtro_estado: 
        query_parts.append({"crm_estado": filtro_estado})

    if busqueda and busqueda.strip():
        term = busqueda.strip()
        regex_term = re.escape(term)
        clean_phone = re.sub(r'\D', '', term)
        
        query_parts.append({"$or": [
            {"prospecto.codigo": {"$regex": regex_term, "$options": "i"}},
            {"prospecto.nombre": {"$regex": regex_term, "$options": "i"}},
            {"phone": {"$regex": clean_phone}}
        ]})
    
    # Corregido: Construimos la query sin autorreferencia circular
    query = {"$and": query_parts} if query_parts else {}
    
    leads_cursor = db["leads"].find(query).limit(100)
    
    leads_procesados = []
    kpi_counts = {"nuevo": 0, "gestion": 0, "visita": 0, "total": 0}
    estado_labels = {"nuevo": "Sin Atender", "gestion": "En Gestión", "visita": "Visita Agendada", "cerrado": "Cerrado"}

    for lead in leads_cursor:
        estado = lead.get("crm_estado", "nuevo")
        kpi_counts["total"] += 1
        if estado in kpi_counts: kpi_counts[estado] += 1
        
        prospecto = lead.get("prospecto", {})
        codigo = detect_property_code(lead)
        url_prop = f"https://www.procasa.cl/propiedad/{codigo}" if codigo else "#"
        
        last_ts = datetime.min
        msgs = lead.get("messages", [])
        last_msg_txt = "Sin mensajes"
        
        if msgs:
            last_msg = msgs[-1]
            last_ts = last_msg.get("timestamp") or last_msg.get("created_at") or datetime.min
            txt = last_msg.get("content", "")
            last_msg_txt = (txt[:45] + '...') if len(txt) > 45 else txt

        raw_phone = lead.get("phone", "").replace("+", "").strip()
        
        leads_procesados.append({
            "phone": raw_phone,
            "whatsapp_display": f"+{raw_phone}",
            "nombre": prospecto.get("nombre") or "Desconocido",
            "estado": estado,
            "estado_badge": estado_labels.get(estado, estado.capitalize()),
            "tiempo_relativo": format_relative_time(last_ts),
            "real_timestamp": last_ts,
            "led_class": "led-red" if estado == "nuevo" else ("led-yellow" if estado == "gestion" else "led-green"),
            "sla_title": "Prioridad",
            "codigo_propiedad": codigo or "S/N",
            "url_propiedad": url_prop,
            "ultima_accion": last_msg_txt
        })
        
    leads_procesados.sort(key=lambda x: x['real_timestamp'], reverse=True)
    return leads_procesados, kpi_counts

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

    new_events_cursor = db["crm_events"].find({
        "phone": phone_clean, 
        "type": {"$in": ["GESTION_LOG", "STATUS_CHANGE", "CALL_OUT", "WHATSAPP_OUT"]}
    }).sort("timestamp", -1)
    
    formatted_new_history = []
    for evt in new_events_cursor:
        meta = evt.get("meta", {})
        formatted_new_history.append({
            "timestamp": evt["timestamp"],
            "user_action": meta.get("action_label", evt["type"]),
            "result": meta.get("result", ""),
            "notes": meta.get("notes", "")
        })
        
    timeline = process_chat_timeline(lead.get("messages", []))
    prospecto = lead.get("prospecto", {})

    return {
        "phone": lead.get("phone"),
        "timeline": timeline,
        "nombre": prospecto.get("nombre", "Desconocido"),
        "email": prospecto.get("email", "No registrado"),
        "rut": prospecto.get("rut", "No registrado"),
        "origen": prospecto.get("origen", "Desconocido"), 
        "propiedad_texto": f"{datos_propiedad.get('operacion','')} {datos_propiedad.get('comuna','')}",
        "crm_estado": lead.get("crm_estado", "nuevo"),
        "crm_history": formatted_new_history, 
        "sticky_notes": lead.get("sticky_notes", []),
        "datos_propiedad": datos_propiedad
    }

# --- 3. ACTUALIZAR LEAD ---
def update_lead_crm_data(phone, data):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    current_lead = db["leads"].find_one({"phone": {"$regex": phone_clean}})
    if not current_lead: return False
    
    new_state = data.get("estado")
    old_state = current_lead.get("crm_estado", "nuevo")
    
    if old_state != new_state:
        log_crm_event(phone_clean, "STATUS_CHANGE", meta_data={"from": old_state, "to": new_state})

    if data.get("next_action_date"):
        schedule_crm_task(phone_clean, data.get("next_action_date"), data.get("notas"))

    log_crm_event(phone_clean, "GESTION_LOG", meta_data={
        "interaction_type": data.get("interaction_type"),
        "result": data.get("resultado_gestion"),
        "notes": data.get("notas"),
        "action_label": data.get("action_label"),
        "details_json": data
    })

    db["leads"].update_one(
        {"phone": {"$regex": phone_clean}},
        {"$set": {
            "crm_estado": new_state,
            "last_crm_update": datetime.now(),
            "crm_propietario_estado": data.get("propietario_res")
        }}
    )
    return True

def manage_crm_notes(phone, note_data, action="add"):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    if action == "add":
        note_id = str(uuid.uuid4())[:8]
        note = {"id": note_id, "content": note_data.get("content"), "color": note_data.get("color"), "created_at_str": datetime.now().strftime("%d/%m/%Y")}
        db["leads"].update_one({"phone": {"$regex": phone_clean}}, {"$push": {"sticky_notes": note}})
        return note
    elif action == "delete":
        db["leads"].update_one({"phone": {"$regex": phone_clean}}, {"$pull": {"sticky_notes": {"id": note_data.get("id")}}})
        return True
    return False