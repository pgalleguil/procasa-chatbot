
from config import Config
from datetime import datetime, timezone
import pytz
import logging
import uuid
import re
from chatbot.storage import get_db

logger = logging.getLogger(__name__)

try:
    from chatbot.constants import CHILE_TZ
except ImportError:
    import pytz
    CHILE_TZ = pytz.timezone('Chile/Continental')

def format_relative_time(dt_obj):
    if not dt_obj: return "S/I"
    if isinstance(dt_obj, str):
        try: dt_obj = datetime.fromisoformat(dt_obj.replace('Z', ''))
        except: return "S/I"
    
    now = datetime.now(CHILE_TZ)
    if dt_obj.tzinfo is None:
        dt_obj = CHILE_TZ.localize(dt_obj)
        
    diff = now - dt_obj
    seconds = diff.total_seconds()
    
    if seconds < 0: return "Ahora"
    
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    
    if days > 0: return f"Hace {days}d"
    elif hours > 0: return f"Hace {hours}h"
    elif minutes > 0: return f"Hace {minutes}m"
    else: return "Ahora"

def get_market_insights(comuna, tipo_propiedad):
    """
    Calcula estadísticas de mercado basadas en universo_obelix
    """
    db = get_db()
    
    # 1. UF/M2 Promedio en la comuna para ese tipo
    pipeline = [
        {"$match": {
            "comuna": comuna, 
            "tipo": tipo_propiedad,
            "precio_uf": {"$exists": True, "$ne": None, "$gt": 0},
            "m2_total": {"$exists": True, "$ne": None, "$gt": 0}
        }},
        {"$group": {
            "_id": None,
            "avg_uf_m2": {"$avg": {"$divide": ["$precio_uf", "$m2_total"]}},
            "count": {"$sum": 1}
        }}
    ]
    
    stats = list(db["universo_obelix"].aggregate(pipeline))
    avg_uf_m2 = round(stats[0]["avg_uf_m2"], 1) if stats else 0
    total_market = stats[0]["count"] if stats else 0

    # 2. Popularidad (Leads vinculados en los últimos 90 días)
    return {
        "avg_uf_m2": avg_uf_m2,
        "total_available": total_market,
        "demand_level": "Alta" if total_market > 50 else "Media" 
    }

def get_captacion_list(user_role="agente", user_name="", page=1, limit=10, comuna_filter=None, status_filter=None, executive_filter=None):
    db = get_db()
    query = {"details.es_propietario_directo": True}
    
    # RBAC & Filtering
    if user_role in ["admin", "supervisor"]:
        if executive_filter and executive_filter != "Todos":
            query["gestion.ejecutivo_asignado"] = executive_filter
    else:
        # Los agentes solo ven lo suyo
        query["gestion.ejecutivo_asignado"] = user_name
    
    if comuna_filter:
        query["details.comuna"] = {"$regex": comuna_filter, "$options": "i"}
        
    if status_filter:
        query["gestion.estado"] = status_filter

    total_count = db["yapo_propiedades"].count_documents(query)
    
    skip = (page - 1) * limit
    cursor = db["yapo_propiedades"].find(query).sort("details.fecha_scraping", -1).skip(skip).limit(limit)
    
    items = []
    for doc in cursor:
        details = doc.get("details", {})
        gestion = doc.get("gestion", {})
        price_uf = details.get("precio_uf")
        m2 = details.get("m2_total")
        uf_m2 = round(price_uf / m2, 1) if (price_uf and m2 and m2 > 0) else 0
        
        items.append({
            "id": str(doc["_id"]),
            "url": doc.get("url"),
            "titulo": details.get("titulo", "Sin título"),
            "comuna": details.get("comuna", "S/I"),
            "precio": details.get("precio", "S/I"),
            "precio_uf": price_uf,
            "uf_m2": uf_m2,
            "estado": gestion.get("estado", "NUEVO"),
            "fecha_scraping": format_relative_time(details.get("fecha_scraping")),
            "vendedor": gestion.get("ejecutivo_asignado") or details.get("publicador", "Particular"),
            "fotos": len(details.get("enlaces_fotos", []))
        })
        
    return items, total_count

def get_captacion_detail(obj_id):
    from bson import ObjectId
    db = get_db()
    doc = db["yapo_propiedades"].find_one({"_id": ObjectId(obj_id)})
    if not doc: return None
    
    details = doc.get("details", {})
    # Enriquecer con market stats
    market = get_market_insights(details.get("comuna"), details.get("tipo_propiedad"))
    
    price_uf = details.get("precio_uf")
    m2 = details.get("m2_total")
    uf_m2 = round(price_uf / m2, 1) if (price_uf and m2 and m2 > 0) else 0
    
    # Comparación
    comparison = "below"
    if market["avg_uf_m2"] > 0:
        diff = ((uf_m2 - market["avg_uf_m2"]) / market["avg_uf_m2"]) * 100
        if diff > 10: comparison = "above"
        elif diff < -10: comparison = "below"
        else: comparison = "market"

    return {
        "id": str(doc["_id"]),
        "titulo": details.get("titulo"),
        "descripcion": details.get("descripcion"),
        "url": doc.get("url"),
        "comuna": details.get("comuna"),
        "region": details.get("region"),
        "precio": details.get("precio"),
        "precio_uf": price_uf,
        "m2_total": m2,
        "uf_m2": uf_m2,
        "dormitorios": details.get("dormitorios"),
        "banos": details.get("banos"),
        "enlaces_fotos": details.get("enlaces_fotos", []),
        "vendedor_nombre": details.get("publicador", "Particular"), # El dueño real
        "vendedor_telefono": details.get("vendedor_id"), # El teléfono real extraído
        "ejecutivo_asignado": doc.get("gestion", {}).get("ejecutivo_asignado"),
        "gestion": doc.get("gestion", {}),
        "market_stats": market,
        "price_comparison": comparison,
        "seduction_context": {
            "initial": f"Hola {details.get('publicador', 'Particular')}, vi tu propiedad en {details.get('comuna')} ({price_uf} UF). Trabajo en Procasa y tenemos {market['total_available']*2 if market['total_available'] > 0 else 12} clientes buscando en esa zona esta semana. Me gustaría comentarte cómo podemos asegurar tu venta.",
            "objections": [
                {
                    "title": "No quiero corredores",
                    "script": "Te entiendo, muchos cobran y no hacen nada. En Procasa no solo publicamos; filtramos legalmente a los compradores y te aseguro que solo llevamos gente con crédito aprobado. ¿Te gustaría evitar visitas improductivas?"
                },
                {
                    "title": "La comisión es alta",
                    "script": "Nuestra comisión incluye seguro de arriendo/venta y asesoría legal completa. Un error en el contrato te puede costar mucho más que nuestro servicio. ¿Prefieres seguridad o ahorrar un poco y arriesgar el patrimonio?"
                },
                {
                    "title": "Ya lo tengo vendido",
                    "script": "¡Qué bueno! Si por alguna razón no se concreta el cierre, cuenta con nosotros. Tenemos una base de datos de interesados específicos para esta zona que quedaron fuera de otras ventas."
                }
            ]
        }
    }

def update_captacion_status(obj_id, status, notes=None):
    from bson import ObjectId
    db = get_db()
    update_data = {
        "gestion.estado": status,
        "gestion.fecha_ultima_gestion": datetime.now(timezone.utc).isoformat()
    }
    if notes:
        db["yapo_propiedades"].update_one(
            {"_id": ObjectId(obj_id)},
            {"$push": {"gestion.notas": {
                "content": notes,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }}}
        )
    
    db["yapo_propiedades"].update_one({"_id": ObjectId(obj_id)}, {"$set": update_data})
    return True

def normalize_commune(name):
    if not name: return ""
    import unicodedata
    # Normalización básica: minúsculas, sin acentos y solo caracteres alfanuméricos
    name = str(name).lower().strip()
    name = "".join(c for c in unicodedata.normalize('NFD', name) if unicodedata.category(c) != 'Mn')
    name = re.sub(r'[^a-z0-9]', '', name)
    return name

def distribute_sourced_leads():
    """
    Distribuye propiedades 'NUEVO' sin ejecutivo a los ejecutivos basados en comunas_interes.
    Lógica equitativa.
    """
    db = get_db()
    
    # 1. Buscar ejecutivos y sus comunas (normalizadas)
    ejecutivos_raw = list(db["usuarios"].find({"comunas_interes": {"$exists": True, "$not": {"$size": 0}}}))
    if not ejecutivos_raw:
        return 0
        
    # 2. Mapear comunas normalizadas a ejecutivos
    comuna_to_execs = {}
    for e in ejecutivos_raw:
        for c in e.get("comunas_interes", []):
            c_norm = normalize_commune(c)
            if not c_norm: continue
            if c_norm not in comuna_to_execs: comuna_to_execs[c_norm] = []
            comuna_to_execs[c_norm].append(e["nombre"])
            
    # 3. Buscar propiedades sin asignar (NUEVO + es_propietario_directo)
    query = {
        "details.es_propietario_directo": True,
        "gestion.ejecutivo_asignado": None,
        "gestion.estado": "NUEVO"
    }
    props = list(db["yapo_propiedades"].find(query))
    
    assigned_count = 0
    # Round-robin simplificado por comuna
    exec_counters = {e["nombre"]: 0 for e in ejecutivos_raw}
    
    for p in props:
        details = p.get("details", {})
        comuna_raw = details.get("comuna")
        comuna_norm = normalize_commune(comuna_raw)
        
        potential_execs = comuna_to_execs.get(comuna_norm, [])
        if not potential_execs:
            continue
        
        # Elegir el que tenga menos asignaciones en esta ronda
        target_exec = min(potential_execs, key=lambda x: exec_counters[x])
        
        db["yapo_propiedades"].update_one(
            {"_id": p["_id"]},
            {"$set": {
                "gestion.ejecutivo_asignado": target_exec,
                "gestion.fecha_asignacion": datetime.now(timezone.utc).isoformat()
            }}
        )
        exec_counters[target_exec] += 1
        assigned_count += 1
        
    return assigned_count
