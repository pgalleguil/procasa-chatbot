# api_crm.py
from pymongo import MongoClient
from config import Config
from datetime import datetime

def get_db():
    client = MongoClient(Config.MONGO_URI)
    return client[Config.DB_NAME]

# --- 1. LISTA DE LEADS ---
def get_crm_leads_list(filtro_estado=None):
    db = get_db()
    query = {}
    if filtro_estado:
        query["crm_estado"] = filtro_estado

    # Traemos los leads ordenados por fecha
    leads_cursor = db["conversaciones_whatsapp"].find(query).sort("last_message_time", -1).limit(100)
    
    leads_procesados = []
    now = datetime.now()
    
    for lead in leads_cursor:
        phone = lead.get("phone", "S/N")
        prospecto = lead.get("prospecto", {})
        
        # Datos básicos
        nombre = prospecto.get("nombre") or lead.get("nombre_cliente") or "Desconocido"
        propiedad = f"{prospecto.get('operacion', '')} {prospecto.get('comuna', '')}".strip()
        if len(propiedad) < 3: propiedad = "Interés General"
        
        score = lead.get("lead_score", 5) 
        
        # Validación robusta del ejecutivo
        ejecutivo = lead.get("crm_ejecutivo")
        if not ejecutivo or not ejecutivo.strip():
            ejecutivo = "Sin asignar"
            
        estado = lead.get("crm_estado", "nuevo")
        
        # Lógica de colores y LEDs (SLA)
        msgs = lead.get("messages", [])
        last_ts = now
        if msgs:
            try:
                ts_val = msgs[-1].get("timestamp")
                if isinstance(ts_val, str):
                    last_ts = datetime.fromisoformat(ts_val.replace("Z", ""))
            except:
                pass
        
        diff_hours = (now - last_ts).total_seconds() / 3600
        
        # Asignar clases CSS
        led_class = "led-gray"
        sla_title = "Normal"
        
        if estado == "nuevo":
            if diff_hours > 2:
                led_class = "led-red"
                sla_title = f"Sin Atender (> {int(diff_hours)}h)"
            else:
                led_class = "led-yellow"
                sla_title = "Nuevo ingreso"
        elif estado == "gestion":
            led_class = "led-yellow"
            sla_title = "En Gestión"
        elif estado == "visita":
            led_class = "led-green"
            sla_title = "Visita Agendada"
        elif estado == "perdido":
            led_class = "led-gray"
            sla_title = "Cerrado"
            
        # Determinar badge de score
        score_class = "score-med"
        score_label = "Med"
        if score >= 8:
            score_class = "score-high"
            score_label = "High"
        elif score < 5:
            score_class = "score-med"
            score_label = "Low"

        # Formatear iniciales para avatar (CORRECCIÓN DEL ERROR AQUÍ)
        avatar_text = "?"
        if ejecutivo != "Sin asignar":
            parts = ejecutivo.split()
            if parts: # Verificamos que la lista no esté vacía
                if len(parts) >= 2:
                    avatar_text = f"{parts[0][0]}{parts[1][0]}".upper()
                else:
                    avatar_text = parts[0][:2].upper()
            else:
                avatar_text = "?" 

        leads_procesados.append({
            "phone": phone,
            "nombre": nombre,
            "whatsapp_display": f"+{phone}" if not phone.startswith("+") else phone,
            "propiedad": propiedad,
            "score": score,
            "score_class": score_class,
            "score_label": score_label,
            "ejecutivo": ejecutivo,
            "avatar_text": avatar_text,
            "estado": estado, 
            "estado_badge": estado.upper(),
            "led_class": led_class,
            "sla_title": sla_title,
            "fecha": last_ts.strftime("%d/%m %H:%M"),
            "ultima_accion": "Chat reciente"
        })
        
    return leads_procesados

# --- 2. DETALLE DEL LEAD ---
def get_lead_detail_data(phone):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    lead = db["conversaciones_whatsapp"].find_one({"phone": {"$regex": phone_clean}})
    
    if not lead: 
        return None
    
    prospecto = lead.get("prospecto", {})
    bi_data = lead.get("bi_data", {})
    
    timeline = []
    messages = lead.get("messages", [])
    for m in messages[-20:]:
        role = "chat-user" if m.get("role") == "user" else "chat-bot"
        timeline.append({
            "role": role,
            "content": m.get("content", "")
        })

    return {
        "phone": lead.get("phone"),
        "nombre": prospecto.get("nombre", "Desconocido"),
        "email": prospecto.get("email", "No registrado"),
        "rut": prospecto.get("rut", "No registrado"),
        "origen": lead.get("origen", "Desconocido"),
        "propiedad_cod": prospecto.get("propiedad_id", "S/N"),
        "propiedad_texto": f"{prospecto.get('operacion', '')} {prospecto.get('comuna', '')}",
        "score": lead.get("lead_score", 5),
        "intencion": bi_data.get("INTENCION_CLIENTE", "General"),
        "resumen": bi_data.get("RESUMEN_CONVERSACION", "Sin resumen disponible"),
        "datos_completos": "Datos completos" if prospecto.get("email") and prospecto.get("rut") else "Faltan datos",
        "crm_estado": lead.get("crm_estado", "nuevo"),
        "crm_ejecutivo": lead.get("crm_ejecutivo", ""),
        "crm_notas": lead.get("crm_notas", ""),
        "timeline": timeline
    }

# --- 3. ACTUALIZAR LEAD ---
def update_lead_crm_data(phone, data):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    result = db["conversaciones_whatsapp"].update_one(
        {"phone": {"$regex": phone_clean}},
        {"$set": {
            "crm_ejecutivo": data.get("ejecutivo"),
            "crm_estado": data.get("estado"),
            "crm_notas": data.get("notas"),
            "last_crm_update": datetime.now()
        }}
    )
    return result.modified_count > 0 or result.matched_count > 0