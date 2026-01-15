# api_leads_intelligence.py
from pymongo import MongoClient
from config import Config
from datetime import datetime
from collections import Counter
from bson import ObjectId

def get_creation_date(doc):
    """
    Obtiene la fecha real de creación del lead.
    Prioridad: 
    1. _id generation_time (Inmutable, exacto de la creación en BD).
    2. Timestamp del primer mensaje (Backup).
    3. datetime.now() (Último recurso, casi nunca debería pasar).
    """
    try:
        return doc["_id"].generation_time.replace(tzinfo=None)
    except:
        messages = doc.get("messages", [])
        if messages and len(messages) > 0:
            first_msg_ts = messages[0].get("timestamp")
            if first_msg_ts:
                try:
                    return datetime.fromisoformat(first_msg_ts.replace("Z", ""))
                except:
                    pass
        return datetime.now()

def calculate_score(prospecto, intencion_detectada):
    """Calcula un score de calidad (0-10) para el reporte"""
    score = 0
    
    # 1. Puntos por Datos Capturados (Calidad de contacto)
    if prospecto.get("email"): score += 2
    if prospecto.get("rut"): score += 2
    if prospecto.get("nombre"): score += 1
    
    # 2. Puntos por Intención (Calidad comercial)
    if intencion_detectada == "escalado_urgente": score += 5
    elif intencion_detectada == "agendar_visita": score += 4
    elif intencion_detectada == "contacto_directo": score += 3
    elif intencion_detectada == "consultar_precio": score += 1
    
    return min(score, 10)

def determine_strongest_intent(messages):
    """
    Busca en todo el historial la intención más fuerte mostrada por el usuario/bot.
    Jerarquía: Escalado > Visita > Contacto > Consulta
    """
    intenciones_encontradas = set()
    
    for m in messages:
        if m.get("role") == "assistant" and m.get("intencion"):
            intenciones_encontradas.add(m.get("intencion"))
            
    if "escalado_urgente" in intenciones_encontradas: return "escalado_urgente"
    if "agendar_visita" in intenciones_encontradas: return "agendar_visita"
    if "contacto_directo" in intenciones_encontradas: return "contacto_directo"
    if "consultar_precio" in intenciones_encontradas: return "consultar_precio"
    
    return "consulta_general"

def get_leads_executive_report():
    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]

        # Traemos leads ordenados por creación inversa
        documentos = list(
            db["conversaciones_whatsapp"]
            .find({})
            .sort("_id", -1)
            .limit(2000)
        )

        if not documentos:
            return {"kpis": {}, "charts": {}, "leads": []}

        temporal_diario = {}
        comunas = Counter()
        fuentes = Counter()
        propiedades = Counter()
        
        visitas_count = 0
        con_datos_count = 0
        tiempos_conversion = []
        leads_table = []

        for doc in documentos:
            p = doc.get("prospecto", {})
            messages = doc.get("messages", [])

            phone = doc.get("phone")
            nombre = p.get("nombre") or ""
            email = p.get("email")
            rut = p.get("rut")
            
            codigo = p.get("codigo")
            if codigo in ["General", "N/A", "Sin Código", None, ""]:
                codigo = None
            
            comuna = p.get("comuna") or "Sin Comuna"
            origen = p.get("origen") or "Directo"

            if codigo:
                propiedades[str(codigo)] += 1
            
            comunas[comuna] += 1
            fuentes[origen] += 1

            fecha_obj = get_creation_date(doc)
            fecha_str = fecha_obj.strftime("%Y-%m-%d")

            if fecha_obj.year >= datetime.now().year - 1:
                temporal_diario[fecha_str] = temporal_diario.get(fecha_str, 0) + 1

            intencion_actual = determine_strongest_intent(messages)
            is_hot_lead = intencion_actual in ["agendar_visita", "escalado_urgente", "contacto_directo"]

            if is_hot_lead:
                visitas_count += 1
                if email and rut:
                    con_datos_count += 1

                try:
                    if messages:
                        start_msg = messages[0]
                        end_msg = next(
                            (m for m in messages if m.get("intencion") in ["agendar_visita", "escalado_urgente", "contacto_directo"]),
                            None
                        )
                        if start_msg and end_msg:
                            t1 = datetime.fromisoformat(start_msg.get("timestamp").replace("Z",""))
                            t2 = datetime.fromisoformat(end_msg.get("timestamp").replace("Z",""))
                            delta_min = (t2 - t1).total_seconds() / 60
                            if 0 < delta_min < 43200: 
                                tiempos_conversion.append(delta_min)
                except:
                    pass

            score_lead = calculate_score(p, intencion_actual)

            leads_table.append({
                "nombre": nombre,
                "phone": phone,
                "email": email,
                "rut": rut,
                "intencion": intencion_actual,
                "score": score_lead,
                "origen": origen,
                "fecha": fecha_str,
                "hot_lead": is_hot_lead
            })

        total_leads = len(documentos)
        fechas_ordenadas = sorted(temporal_diario.keys())
        counts_ordenados = [temporal_diario[f] for f in fechas_ordenadas]

        avg_speed = 0
        if tiempos_conversion:
            avg_speed = sum(tiempos_conversion) / len(tiempos_conversion)

        pct_hot_leads = (visitas_count / total_leads * 100) if total_leads > 0 else 0
        tasa_captura_datos = (con_datos_count / total_leads * 100) if total_leads > 0 else 0

        return {
            "kpis": {
                "total_leads": total_leads,
                "leads_calientes": visitas_count,
                "pct_leads_calientes": round(pct_hot_leads, 1),
                "tasa_captura_datos": round(tasa_captura_datos, 1),
                "avg_speed_minutes": round(avg_speed, 1)
            },
            "charts": {
                "temporal": { "labels": fechas_ordenadas, "values": counts_ordenados },
                "fuentes": { "labels": list(fuentes.keys()), "values": list(fuentes.values()) }
            },
            "leads": leads_table
        }

    except Exception as e:
        print("❌ Error en reporte intelligence:", e)
        return {"kpis": {}, "charts": {}, "leads": []}

def get_specific_lead_chat(phone):
    """Recupera los mensajes de un lead específico"""
    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]
        doc = db["conversaciones_whatsapp"].find_one({"phone": phone})
        if doc:
            return {
                "phone": doc.get("phone"),
                "prospecto": doc.get("prospecto", {}),
                "messages": doc.get("messages", [])
            }
        return None
    except Exception as e:
        print(f"❌ Error obteniendo chat {phone}:", e)
        return None