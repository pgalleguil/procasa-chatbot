# api_leads_intelligence.py
# MÓDULO DE BUSINESS INTELLIGENCE - CHATBOT LEADS
from pymongo import MongoClient
from config import Config
from datetime import datetime, timedelta

def get_leads_intelligence_data():
    """
    Ejecuta pipelines de agregación en MongoDB para extraer
    insights de negocio sobre la gestión de leads.
    """
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    # Asegúrate que el nombre de la colección coincida con tu config
    # Si usas un nombre directo, cámbialo aquí, ej: db["prospectos"]
    collection = db.get_collection("prospectos") 

    # --- 1. INDICADORES CLÁSICOS (OPERATIVOS) ---
    total_leads = collection.count_documents({})
    
    # Activos en las últimas 48 horas
    hace_48h = datetime.utcnow() - timedelta(hours=48)
    # Nota: Ajustamos la query para soportar fechas ISO string o Date Objects
    leads_activos = collection.count_documents({
        "$or": [
            {"ultimo_mensaje": {"$gte": hace_48h.isoformat()}},
            {"ultimo_mensaje": {"$gte": hace_48h}}
        ]
    })

    leads_calientes = collection.count_documents({"lead_score": {"$gte": 7}})
    visitas_agendadas = collection.count_documents({"intencion_actual": "agendar_visita"})

    # --- 2. BUSINESS ANALYTICS (ESTRATÉGICOS - PIPELINES) ---
    
    # A. Distribución de Intención (¿Qué busca el mercado?)
    pipeline_intencion = [
        {"$group": {"_id": "$intencion_actual", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    data_intencion = list(collection.aggregate(pipeline_intencion))
    
    # B. Histograma de Calidad de Leads (Segmentación de Cartera)
    pipeline_score = [
        {
            "$project": {
                "segmento": {
                    "$switch": {
                        "branches": [
                            {"case": {"$gte": ["$lead_score", 10]}, "then": "VIP (10+)"},
                            {"case": {"$gte": ["$lead_score", 7]}, "then": "Hot (7-9)"},
                            {"case": {"$gte": ["$lead_score", 4]}, "then": "Warm (4-6)"}
                        ],
                        "default": "Cold (0-3)"
                    }
                }
            }
        },
        {"$group": {"_id": "$segmento", "count": {"$sum": 1}}}
    ]
    data_score = list(collection.aggregate(pipeline_score))

    # --- 3. DATA FEED (TABLA RAW) ---
    # Traemos los últimos 50 para la vista rápida
    raw_leads = list(collection.find({}, {"_id": 0}).sort("ultimo_mensaje", -1).limit(50))
    
    # Enriquecimiento simple (Anti-N+1 query)
    propiedades_col = db[Config.COLLECTION_NAME]
    codigos = list(set([str(p.get("codigo")) for p in raw_leads if p.get("codigo")]))
    props_db = list(propiedades_col.find({"codigo": {"$in": codigos}}, {"codigo": 1, "titulo": 1}))
    props_map = {str(p["codigo"]): p.get("titulo", "Propiedad") for p in props_db}

    processed_leads = []
    for p in raw_leads:
        fecha_str = "N/D"
        # Manejo robusto de fechas
        if p.get("ultimo_mensaje"):
            val = p.get("ultimo_mensaje")
            if isinstance(val, str):
                fecha_str = val[:16].replace("T", " ")
            elif isinstance(val, datetime):
                fecha_str = val.strftime("%d/%m %H:%M")

        cod = str(p.get("codigo", ""))
        titulo_prop = props_map.get(cod, "Búsqueda General")

        processed_leads.append({
            "phone": p.get("phone"),
            "nombre": p.get("nombre", "Desconocido"),
            "score": p.get("lead_score", 0),
            "intencion": p.get("intencion_actual", "General"),
            "fecha": fecha_str,
            "codigo": cod,
            "titulo": titulo_prop,
            "link": f"https://www.procasa.cl/{cod}" if cod else "#"
        })

    return {
        "kpis": {
            "total": total_leads,
            "activos": leads_activos,
            "calientes": leads_calientes,
            "conversion": visitas_agendadas
        },
        "analytics": {
            "intencion": data_intencion,
            "segmentacion": data_score
        },
        "tabla": processed_leads
    }