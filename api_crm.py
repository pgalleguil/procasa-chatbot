from pymongo import MongoClient
from config import Config
from datetime import datetime
import re

def get_db():
    client = MongoClient(Config.MONGO_URI)
    return client[Config.DB_NAME]

def format_relative_time(dt_obj):
    if not dt_obj:
        return "S/I"
    
    now = datetime.now()
    diff = now - dt_obj
    seconds = diff.total_seconds()
    
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    
    if days > 0:
        return f"Hace {days}d {hours}h"
    elif hours > 0:
        return f"Hace {hours}h {minutes}m"
    elif minutes > 0:
        return f"Hace {minutes}m"
    else:
        return "Ahora"

# --- 1. LISTA DE LEADS ---
def get_crm_leads_list(filtro_estado=None, busqueda=None, ordenar_por="prioridad"):
    db = get_db()
    
    # Query Base
    query = {
        "messages.intencion": "agendar_visita"
    }

    if filtro_estado:
        query["crm_estado"] = filtro_estado

    # --- LÓGICA DE BÚSQUEDA CORREGIDA ---
    if busqueda and busqueda.strip():
        term = busqueda.strip()
        # Limpiamos el término para búsqueda telefónica (quitamos +, espacios)
        term_clean = re.sub(r'\D', '', term) 
        regex_term = re.escape(term)
        
        or_conditions = [
            {"prospecto.codigo": {"$regex": regex_term, "$options": "i"}},
            {"prospecto.nombre": {"$regex": regex_term, "$options": "i"}}
        ]
        
        # Si logramos limpiar números, buscamos en el teléfono con regex flexible
        if term_clean:
            or_conditions.append({"phone": {"$regex": term_clean}})
            
        query["$and"] = [
            {"messages.intencion": "agendar_visita"},
            {"$or": or_conditions}
        ]

    # Ejecutar Query
    leads_cursor = db["conversaciones_whatsapp"].find(query).sort("last_message_time", -1).limit(100)
    
    leads_procesados = []
    now = datetime.now()
    
    # --- CÁLCULO DE KPIS (Contadores) ---
    # Para hacer esto eficiente, hacemos un count rápido de la colección TOTAL (sin filtro de búsqueda para los KPI generales)
    # O si prefieres que los KPI se ajusten a la búsqueda, usamos los contadores del loop.
    # Usaremos contadores del loop actual para reflejar lo que se ve.
    kpi_counts = {"nuevo": 0, "gestion": 0, "visita": 0, "total": 0}
    
    for lead in leads_cursor:
        phone = lead.get("phone", "S/N")
        prospecto = lead.get("prospecto", {})
        
        nombre = prospecto.get("nombre") or lead.get("nombre_cliente") or "Desconocido"
        codigo_propiedad = prospecto.get("codigo", "S/N")
        
        url_propiedad = "#"
        if codigo_propiedad and codigo_propiedad != "S/N":
            url_propiedad = f"https://www.procasa.cl/propiedad/{codigo_propiedad}"
            
        ejecutivo = lead.get("crm_ejecutivo")
        if not ejecutivo or not ejecutivo.strip():
            ejecutivo = "Sin asignar"
            
        estado = lead.get("crm_estado", "nuevo")
        
        # Actualizar KPIs
        kpi_counts["total"] += 1
        if estado in kpi_counts:
            kpi_counts[estado] += 1
        
        # Tiempos
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
        tiempo_relativo = format_relative_time(last_ts)
        
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
            
        avatar_text = "?"
        if ejecutivo != "Sin asignar":
            parts = ejecutivo.split()
            if parts:
                avatar_text = parts[0][:2].upper()
        
        leads_procesados.append({
            "phone": phone,
            "nombre": nombre,
            "whatsapp_display": f"+{phone}" if not phone.startswith("+") else phone,
            "codigo_propiedad": codigo_propiedad,
            "url_propiedad": url_propiedad,
            "ejecutivo": ejecutivo,
            "avatar_text": avatar_text,
            "estado": estado, 
            "estado_badge": estado.upper(),
            "led_class": led_class,
            "sla_title": sla_title,
            "fecha_dt": last_ts, 
            "tiempo_relativo": tiempo_relativo,
            "ultima_accion": "Solicitud Visita"
        })
        
    if ordenar_por == "prioridad":
        status_weight = {"nuevo": 1, "gestion": 2, "visita": 3, "perdido": 4}
        leads_procesados.sort(key=lambda x: (status_weight.get(x["estado"], 5), -x["fecha_dt"].timestamp()))
    else:
        leads_procesados.sort(key=lambda x: x["fecha_dt"].timestamp(), reverse=True)
        
    return leads_procesados, kpi_counts

# --- 2. DETALLE DEL LEAD ---
def get_lead_detail_data(phone):
    db = get_db()
    # Limpieza para encontrar el lead exacto
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    lead = db["conversaciones_whatsapp"].find_one({"phone": {"$regex": phone_clean}})
    
    if not lead: 
        return None
    
    prospecto = lead.get("prospecto", {})
    bi_data = lead.get("bi_analytics_global", {})
    
    # Obtener código para buscar en universo_obelix
    codigo_propiedad = prospecto.get("codigo", "S/N")
    url_propiedad = "#"
    propiedad_data = {}

    if codigo_propiedad and codigo_propiedad != "S/N":
        url_propiedad = f"https://www.procasa.cl/propiedad/{codigo_propiedad}"
        # Buscamos datos completos en universo_obelix
        propiedad_data = db["universo_obelix"].find_one({"codigo": codigo_propiedad}) or {}

    datos_propiedad = {
        "codigo": codigo_propiedad,
        "url": url_propiedad,
        "tipo": propiedad_data.get("tipo", "Propiedad"),
        "region": propiedad_data.get("region", ""),
        "comuna": propiedad_data.get("comuna", ""),
        "sector": propiedad_data.get("sector", ""),
        "calle": propiedad_data.get("calle", ""),
        "numeracion": propiedad_data.get("numero", ""),
        "depto": propiedad_data.get("depto_casa", ""),
        "ref1": propiedad_data.get("calle_referencia_1", ""),
        "ref2": propiedad_data.get("calle_referencia_2", ""),
        "precio_uf": propiedad_data.get("precio_uf", 0),
        
        # Datos Propietario desde Universo Obelix
        "nombre_propietario": propiedad_data.get("nombre_propietario", "No registrado"),
        "movil_propietario": propiedad_data.get("movil_propietario", ""),
        "email_propietario": propiedad_data.get("email_propietario", "")
    }

    timeline = []
    messages = lead.get("messages", [])
    
    for m in messages[-40:]:
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
        "origen": prospecto.get("origen", "Desconocido"), 
        "propiedad_texto": f"{prospecto.get('operacion', '')} {prospecto.get('comuna', '')}",
        "score": lead.get("score", 0),
        "resumen": bi_data.get("PENSAMIENTO_AUDITOR", "Sin resumen disponible"),
        "crm_estado": lead.get("crm_estado", "nuevo"),
        "crm_notas": lead.get("crm_notas", ""),
        "timeline": timeline,
        "datos_propiedad": datos_propiedad
    }

# --- 3. ACTUALIZAR LEAD ---
def update_lead_crm_data(phone, data):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    update_fields = {
        "crm_ejecutivo": data.get("ejecutivo"),
        "crm_estado": data.get("estado"),
        "crm_notas": data.get("notas"),
        "last_crm_update": datetime.now(),
        "crm_propietario_estado": data.get("propietario_res"),
        "crm_next_action_date": data.get("next_action_date"),
        "crm_client_avail": data.get("client_avail")
    }
    
    result = db["conversaciones_whatsapp"].update_one(
        {"phone": {"$regex": phone_clean}},
        {"$set": update_fields}
    )
    return result.modified_count > 0 or result.matched_count > 0