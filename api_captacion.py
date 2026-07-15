from config import Config
from datetime import datetime, timezone, timedelta
import pytz
from chatbot.constants import CHILE_TZ
import logging
import uuid
import re
from bson import ObjectId
from chatbot.storage import get_db, log_event
from chatbot.constants import CHILE_TZ, EventType
from owner_confidence import (
    build_owner_probability_doc,
    detect_source_price_warning,
    resolve_price_display,
)

logger = logging.getLogger(__name__)

# --- CONFIGURACION CENTRALIZADA ---
# CHILE_TZ is imported from chatbot.constants
MARKET_STATS_CACHE = {} # Legacy - Now using shared_cache in DB
_LOCAL_CACHE_L1 = {}

def get_captacion_collection(db):
    return Config.get_captacion_collection(db)

def normalize_commune_canonical(value):
    """Normaliza nombre de comuna a slug canónico (lowercase, sin acentos, guiones)."""
    if not value:
        return None
    import unicodedata
    s = str(value).lower().strip()
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
    s = s.replace('ñ', 'n').replace('Ã±', 'n')
    s = re.sub(r'[^a-z0-9\s_-]', '', s)
    s = re.sub(r'[\s_]+', '-', s.strip())
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')
    return s if s else None

def normalize_captacion_document(doc):
    """View model consistente para lista y detalle desde cualquier origen."""
    if not doc:
        return None
    details = dict(doc.get("details", {}) or {})
    
    def first(*keys):
        for k in keys:
            detail_key = k.split(".", 1)[1] if k.startswith("details.") else k
            v = doc.get(k) or details.get(detail_key)
            if v is not None and v != "" and v != "N/A" and v != "S/I":
                return v
        return None
    
    titulo = first("title", "titulo", "details.titulo")
    if not titulo:
        titulo = "Sin título"
    
    descripcion = first("description", "descripcion", "details.descripcion") or ""
    
    comuna = first("comuna", "details.comuna") or ""
    comuna_slug = first("comuna_slug", "details.comuna_norm") or normalize_commune_canonical(comuna) or ""
    
    operacion = first("operation", "operacion", "tipo_operacion", "details.operacion", "details.tipo_operacion") or ""
    tipo_propiedad = first("property_type", "tipo_propiedad", "details.tipo_propiedad", "details.tipo") or ""
    
    precio_raw = first("precio_raw", "precio", "details.precio") or ""
    precio_uf = first("precio_uf", "details.precio_uf") or 0
    precio_clp = first("precio_clp", "details.precio_clp") or 0

    dormitorios = first("dormitorios", "details.dormitorios", "dormitorios_min")
    banos = first("banos", "baños", "details.banos", "details.baños", "banos_min")
    m2_total = first(
        "m2_total", "m2_totales", "m2_construidos", "superficie_total",
        "details.m2_total", "details.m2_totales", "details.m2_construidos",
        "details.superficie_total",
    )

    # Downstream detail/scoring code consumes canonical keys from details.
    # Merge root-level scraper fields without overwriting richer nested values.
    details.setdefault("operacion", operacion)
    details.setdefault("precio_uf", precio_uf)
    details.setdefault("precio_clp", precio_clp)
    details.setdefault("dormitorios", dormitorios)
    details.setdefault("banos", banos)
    details.setdefault("m2_total", m2_total)
    
    seller_name = first("seller_name", "publicador", "details.publicador", "details.vendedor_nombre") or ""
    contacto = first("contact_phone", "whatsapp_phone", "telefono", "details.telefono", "details.whatsapp_phone") or ""
    
    fotos = first("images", "enlaces_fotos", "details.enlaces_fotos", "image_urls") or []
    if not fotos and isinstance(doc.get("main_image_url"), str) and doc["main_image_url"]:
        fotos = [doc["main_image_url"]]
    if isinstance(fotos, int):
        fotos = []
    
    # Clasificación - buscar en múltiples lugares
    classification = doc.get("classification", {}) or {}
    classification_state = (
        classification.get("final_state") or 
        classification.get("state") or 
        doc.get("classification_state") or 
        details.get("classification_state") or 
        "N/A"
    )
    
    origen = doc.get("origen") or doc.get("source_portal") or ""
    portal_label = "Toctoc" if origen == "toctoc" else "Yapo" if origen == "yapo" else origen
    
    gestion = doc.get("gestion", {}) or {}
    
    result = {
        "id": str(doc["_id"]),
        "_id": doc["_id"],
        "listing_id": doc.get("listing_id"),
        "url": doc.get("url"),
        "titulo": titulo,
        "descripcion": descripcion,
        "comuna": comuna,
        "comuna_slug": comuna_slug,
        "operacion": operacion,
        "tipo_propiedad": tipo_propiedad,
        "precio": precio_raw,
        "precio_uf": precio_uf,
        "precio_clp": precio_clp,
        "dormitorios": dormitorios,
        "banos": banos,
        "m2_total": m2_total,
        "vendedor_nombre": seller_name,
        "vendedor_telefono": contacto,
        "vendedor_email": first("email", "vendedor_email", "details.email"),
        "enlaces_fotos": fotos if isinstance(fotos, list) else [],
        "classification_state": classification_state,
        "classification_source": classification.get("decision_source") or classification.get("source") or "",
        "classification_evidence": classification.get("evidence") or classification.get("reason") or "",
        "origen": origen,
        "portal_label": portal_label,
        "gestion": gestion,
        "score_captacion": doc.get("score_captacion", 0),
        "probabilidad": doc.get("probabilidad", "S/I"),
        "details": details,
        "classification": classification,
    }
    result.update(resolve_price_display(doc))
    result.update(build_owner_probability_doc(doc))
    return result


def get_captacion_capture_datetime(doc):
    """Return the first capture timestamp, never the last modification time."""
    candidates = [
        doc.get("first_seen"), doc.get("first_seen_at"), doc.get("created_at"),
        doc.get("fecha_captura"), doc.get("processed_at"), doc.get("scraped_at"),
    ]
    object_id = doc.get("_id")
    if isinstance(object_id, ObjectId):
        candidates.append(object_id.generation_time)
    candidates.append(doc.get("updated_at"))
    for value in candidates:
        if not value:
            continue
        try:
            parsed = value
            if isinstance(parsed, str):
                parsed = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = CHILE_TZ.localize(parsed)
            else:
                parsed = parsed.astimezone(CHILE_TZ)
            return parsed
        except (TypeError, ValueError):
            continue
    return None

def _invalidate_detail_cache(obj_id):
    """Elimina el cache del detalle para forzar un render fresco tras un cambio."""
    _LOCAL_CACHE_L1.pop(f"detail_full_{obj_id}", None)

def _l1_get(key):
    rec = _LOCAL_CACHE_L1.get(key)
    if not rec:
        return None
    expires_at = rec.get("expires_at")
    if not expires_at or expires_at <= datetime.now(timezone.utc):
        _LOCAL_CACHE_L1.pop(key, None)
        return None
    return rec.get("value")

def _l1_set(key, value, expire_seconds):
    _LOCAL_CACHE_L1[key] = {
        "value": value,
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=expire_seconds),
    }

def get_cached_value(key):
    """Obtiene un valor del caché persistente en MongoDB."""
    l1 = _l1_get(key)
    if l1 is not None:
        return l1
    try:
        db = get_db()
        doc = db["system_cache"].find_one({"_id": key}, {"value": 1, "expires_at": 1})
        if doc:
            expires_at = doc.get("expires_at")
            if expires_at:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at > datetime.now(timezone.utc):
                    value = doc.get("value")
                    _LOCAL_CACHE_L1[key] = {"value": value, "expires_at": expires_at}
                    return value
    except Exception as e:
        logger.error(f"Error reading cache: {e}")
    return None

def set_cached_value(key, value, expire_seconds=300):
    """Guarda un valor en el caché persistente en MongoDB."""
    try:
        db = get_db()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expire_seconds)
        db["system_cache"].update_one(
            {"_id": key},
            {"$set": {"value": value, "expires_at": expires_at}},
            upsert=True
        )
        _l1_set(key, value, expire_seconds)
    except Exception as e:
        logger.error(f"Error writing cache: {e}")

def get_chile_now():
    """Retorna datetime actual en Chile."""
    return datetime.now(CHILE_TZ)

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
    Calcula estadísticas de mercado basadas en universo_cartera.
    Implementa caché de 15 min para evitar agregaciones pesadas.
    """
    cache_key = f"{comuna}_{tipo_propiedad}"
    cached_stats = get_cached_value(cache_key)
    if cached_stats:
        return cached_stats

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
    
    stats = list(db[Config.COLLECTION_NAME].aggregate(pipeline))
    avg_uf_m2 = round(stats[0]["avg_uf_m2"], 1) if stats else 0
    total_market = stats[0]["count"] if stats else 0

    # 2. Popularidad (Leads vinculados en los últimos 90 días)
    res = {
        "avg_uf_m2": avg_uf_m2,
        "total_available": total_market,
        "demand_level": "Alta" if total_market > 50 else "Media" 
    }
    set_cached_value(cache_key, res, expire_seconds=3600) # 60 min: datos de mercado no cambian tan rápido
    return res

def calculate_lead_score_captacion(details, market_stats):
    price_uf = details.get("precio_uf")
    m2 = details.get("m2_total")
    description = details.get("descripcion", "") or ""
    
    # 1. Recuperación agresiva de precio si no viene normalizado
    if not price_uf or price_uf <= 0:
        price_uf = _extract_numeric(details.get("precio", ""))
    if not price_uf or price_uf <= 0:
            titulo = details.get("titulo", "")
            if isinstance(titulo, str):
                import re
                m = re.search(r'por\s+([\d\.]+)', titulo, re.IGNORECASE)
                if m:
                    try: price_uf = float(m.group(1))
                    except: pass

    # 2. Cálculo de Score base (50 pts)
    score = 50
    motivos = []
    uf_m2 = 0
    diff_pct = 0
    
    # 3. Factor Precio/M2 (El más crítico para captación)
    if price_uf and m2 and m2 > 0:
        uf_m2 = round(price_uf / m2, 1)
        if market_stats and market_stats.get("avg_uf_m2", 0) > 0:
            avg = market_stats["avg_uf_m2"]
            diff_pct = ((uf_m2 - avg) / avg) * 100
            
            if diff_pct < -5:
                score += 30
                motivos.append(f"🔥 Oportunidad: {abs(diff_pct):.0f}% BAJO mercado")
            elif diff_pct > 15:
                score += 20
                motivos.append(f"💰 Margen: {diff_pct:.0f}% SOBRE mercado (Negociable)")
            elif diff_pct > 5:
                score += 10
                motivos.append(f"Precio sano ({diff_pct:.0f}% sobre media)")
    else:
        motivos.append("Faltan datos de m2 para análisis de precio")

    # 4. Factor Tiempo (SLA de captación)
    dias = details.get("dias_en_portal")
    if dias is not None:
        if dias <= 2:
            score += 25
            motivos.append(f"⚡ Primicia: Publicado hace {dias} días")
        elif dias > 30:
            score += 15
            motivos.append("⏱️ Madurez: Más de 30 días (Dueño ansioso)")

    # 5. Factor Confianza (Dueño vs Corredor)
    conf = details.get("confianza_propietario", 0.5)
    if conf >= 0.9:
        score += 15
        motivos.append("🤝 Trato Directo: Alta certeza de dueño")

    # 6. IA DE CAPTABILIDAD (Heurísticas en descripción)
    lower_desc = description.lower()
    
    # Detectar dueño frustrado o amateur
    frustracion_keywords = ["sin comisión", "trato directo", "no llamar corredores", "no corredores", "particular", "dueño vende"]
    if any(k in lower_desc for k in frustracion_keywords):
        score += 15
        motivos.append("🧠 IA: Dueño detectado (Evita corredores / Frustrado)")
    
    # Detectar urgencia
    urgencia_keywords = ["oportunidad", "urgente", "remato", "conversable", "precio rebajado"]
    if any(k in lower_desc for k in urgencia_keywords):
        score += 10
        motivos.append("🏃 IA: Detectada Urgencia / Disposición a negociar")

    # 7. Factor Multimedia
    fotos = len(details.get("enlaces_fotos", []))
    if fotos > 10:
        score += 5
        motivos.append("📸 Buen material: Listado con muchas fotos")
        
    score = min(score, 100)
    if score >= 85: prob = "CRÍTICA"
    elif score >= 70: prob = "ALTA"
    elif score >= 50: prob = "MEDIA"
    else: prob = "BAJA"

    return score, prob, motivos, uf_m2, diff_pct

def get_next_action_recommendation(score, diff_pct, dias, publicador, comuna, price_uf, intent_count=0, matching_data=None):
    """Genera la recomendación de acción basada en IA/Lógica de Negocio con demanda real."""
    # Robustez de nombre (Mejora psicológica 5)
    name = str(publicador).strip().split()[0].capitalize() if publicador and str(publicador).lower() not in ["particular", "n/a", "no disponible"] else "Propietario"
    saludo = "Hola" if name == "Propietario" or not name else f"Hola {name}"
    
    # Preparar texto de demanda base (Mejora técnica 3)
    demanda_txt = "Actualmente estamos trabajando con clientes activos buscando propiedades en ese sector."
    if matching_data:
        exact = matching_data.get("exact", 0)
        zone = matching_data.get("zone", 0)
        if exact > 0:
            demanda_txt = f"Actualmente tenemos {exact} clientes buscando exactamente algo como tu propiedad y evaluando opciones esta misma semana."
        elif zone > 0:
            demanda_txt = f"Tenemos {zone} clientes activos buscando en tu sector y en contacto con nuestros ejecutivos ahora mismo."

    # Lógica de Abandono (intent_count >= 5)
    if intent_count >= 5:
        return {
            "title": "ARCHIVAR / SEGUIMIENTO PASIVO",
            "reason": f"Se han realizado {intent_count} intentos sin éxito.",
            "message": f"{saludo}, te escribí hace unos días. Veo que sigues con la venta. Si en el futuro necesitas apoyo profesional en Procasa, mi contacto sigue activo. ¡Éxito!",
            "action_type": "whatsapp",
            "urgency": "low",
            "icon": "archive"
        }

    # Lógica de mensajes mejorados (Basado en feedback estratégico)
    if score >= 85:
        return {
            "title": "¡LLAMADA CRÍTICA AHORA!",
            "reason": "Propiedad nueva y bajo precio. Es una captación segura.",
            "message": f"{saludo}, vi tu propiedad en {comuna} recién publicada 👀. {demanda_txt} ¿Te parece si lo vemos rápido? Podemos conectarlos directamente con tu propiedad.",
            "action_type": "call",
            "urgency": "critical",
            "icon": "fire"
        }
    elif diff_pct < -2:
        return {
            "title": "Enviar WhatsApp: Valor de Mercado",
            "reason": "El precio es competitivo. Ataca por la rapidez de cierre.",
            "message": f"{saludo}, vi tu propiedad en {comuna} 👌. El precio está muy bien posicionado y justo coincide con lo que están buscando varios de nuestros clientes activos. Podríamos ayudarte a mostrarla directamente a personas que ya están evaluando opciones.",
            "action_type": "whatsapp",
            "urgency": "high",
            "icon": "trending_down"
        }
    elif diff_pct > 15:
        return {
            "title": "WhatsApp: Estrategia de Precio",
            "reason": f"Precio {diff_pct:.0f}% sobre promedio.",
            "message": f"{saludo}, vi tu propiedad en {comuna}. Se ve muy bien, pero hoy los clientes están bastante sensibles al precio en el sector. Tenemos varios clientes activos buscando ahí, y con una estrategia correcta se puede posicionar mucho mejor. ¿Te interesa una referencia real de mercado?",
            "action_type": "whatsapp",
            "urgency": "medium",
            "icon": "calculate"
        }
    elif (dias or 0) > 30:
        return {
            "title": "WhatsApp: Rescate de Listado",
            "reason": "Lleva mucho tiempo estancada.",
            "message": f"{saludo}, ¿cómo va la venta de tu propiedad en {comuna}? 🙂 Vi que lleva un tiempo publicada. Justo ahora tenemos clientes activos buscando en ese sector, pero muchas veces no llegan a propiedades que no están bien posicionadas. ¿Te gustaría moverla más rápido?",
            "action_type": "whatsapp",
            "urgency": "medium",
            "icon": "restore"
        }
    else:
        return {
            "title": "Contacto de Cortesía",
            "reason": "Propiedad estándar.",
            "message": f"{saludo}, vi tu propiedad en {comuna} 👋. {demanda_txt} Si en algún momento necesitas apoyo para mostrarla o gestionar interesados, feliz te cuento cómo trabajamos.",
            "action_type": "whatsapp",
            "urgency": "low",
            "icon": "chat"
        }


def resolve_operacion(details: dict) -> str:
    """
    Determina VENTA o ARRIENDO priorizando datos explícitos del scraper
    antes que heurísticas de precio.
    """
    tipo_op = (details.get("tipo_operacion") or "").lower()
    if tipo_op == "venta":
        return "VENTA"
    if tipo_op == "arriendo":
        return "ARRIENDO"

    op = (details.get("operacion") or "").lower()
    if "arr" in op:
        return "ARRIENDO"
    return "VENTA"


def get_captacion_list(user_role="agente", user_name="", user_id="", user_email="", page=1, limit=10, comuna_filter=None, status_filter=None, executive_filter=None, operacion_filter=None, telefono_filter=None, classification_filter=None, sort_by=None, sort_dir="desc"):
    db = get_db()
    coll = get_captacion_collection(db)
    
    # Base: captaciones elegibles de todos los portales soportados.
    query = {
        "origen": {"$in": ["toctoc", "yapo"]},
        "classification.state": {"$in": ["DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"]}
    }

    def add_condition(condition):
        """Combine independent filters without overwriting another $or."""
        query.setdefault("$and", []).append(condition)
    
    # RBAC & Filtering
    if user_role in ["admin", "supervisor"]:
        if executive_filter and executive_filter not in ["Todos", "", None]:
            if executive_filter in ["Sin asignar", "__unassigned__"]:
                add_condition({"$or": [
                    {"gestion.ejecutivo_id": {"$exists": False}},
                    {"gestion.ejecutivo_id": None},
                ]})
            else:
                executive_doc = db["usuarios"].find_one(
                    {"nombre": executive_filter}, {"email": 1}
                )
                executive_ids = [executive_filter]
                executive_emails = [executive_filter]
                if executive_doc:
                    executive_ids.append(str(executive_doc["_id"]))
                    if executive_doc.get("email"):
                        executive_emails.append(executive_doc["email"])
                add_condition({"$or": [
                    {"gestion.ejecutivo_asignado": executive_filter},
                    {"$and": [
                        {"gestion.ejecutivo_asignado": {"$in": [None, ""]}},
                        {"$or": [
                            {"gestion.ejecutivo_id": {"$in": executive_ids}},
                            {"gestion.ejecutivo_email": {"$in": executive_emails}},
                        ]},
                    ]},
                ]})
    elif user_role == "agente":
        fallback_identity_clauses = []
        if user_id:
            fallback_identity_clauses.append({"gestion.ejecutivo_id": user_id})
        if user_email:
            fallback_identity_clauses.append({"gestion.ejecutivo_email": user_email})
        assignment_clauses = []
        if user_name:
            assignment_clauses.append({"gestion.ejecutivo_asignado": user_name})
        if fallback_identity_clauses:
            assignment_clauses.append({"$and": [
                {"gestion.ejecutivo_asignado": {"$in": [None, ""]}},
                {"$or": fallback_identity_clauses},
            ]})
        if assignment_clauses:
            add_condition({"$or": assignment_clauses})
        else:
            # Sin identificador, no devolver nada
            query["_id"] = None
    
    comuna_filters = comuna_filter if isinstance(comuna_filter, (list, tuple)) else [comuna_filter]
    comuna_filters = [str(c).strip() for c in comuna_filters if c and str(c).strip()]
    if comuna_filters:
        commune_clauses = []
        for commune in comuna_filters:
            norm = normalize_commune_canonical(commune)
            if norm:
                commune_clauses.extend([
                    {"comuna_slug": norm},
                    {"comuna": re.compile(f"^{re.escape(commune)}$", re.I)},
                    {"details.comuna_norm": norm},
                ])
        if commune_clauses:
            add_condition({"$or": commune_clauses})
    
    if classification_filter and classification_filter != "Todos":
        query["classification.state"] = classification_filter
    
    if status_filter:
        terminal_states = ["Captado", "CAPTADO", "Descartado", "DESCARTADO", "Corredor",
                          "Teléfono inválido", "Propiedad no disponible", "Publicación expirada", "No interesado"]
        if status_filter in ("GRUPO_GESTION", "GESTION"):
            query["gestion.estado"] = {"$in": ["Por contactar", "Contacto exitoso", "Sin respuesta", "Reunión agendada", "GESTION"]}
        elif status_filter in ("GRUPO_CAPTADO", "CAPTADO"):
            query["gestion.estado"] = {"$in": ["Captado", "CAPTADO"]}
        elif status_filter in ("GRUPO_DESCARTADO", "DESCARTADO"):
            query["gestion.estado"] = {"$in": terminal_states}
        elif status_filter == "NUEVO":
            query["gestion.estado"] = "NUEVO"
        else:
            query["gestion.estado"] = status_filter
    
    if operacion_filter:
        op_pattern = re.compile(re.escape(operacion_filter), re.I)
        add_condition({"$or": [
            {"operacion": op_pattern},
            {"tipo_operacion": op_pattern},
        ]})
    
    if telefono_filter:
        tel_pattern = re.compile(re.escape(telefono_filter), re.I)
        add_condition({"$or": [
            {"contact_phone": tel_pattern},
            {"whatsapp_phone": tel_pattern},
            {"telefono": tel_pattern},
        ]})
    
    # Cache
    response_cache_key = (
        f"captacion_resp_v11_{user_role}_{user_name}_{comuna_filters}_{status_filter}_"
        f"{executive_filter}_{operacion_filter}_{telefono_filter}_{classification_filter}_"
        f"{sort_by}_{sort_dir}_{page}_{limit}"
    )
    cached = get_cached_value(response_cache_key)
    if cached is not None:
        return cached.get("items", []), cached.get("total_count", 0), cached.get("available_ops", ["venta", "arriendo"])
    
    total_count = coll.count_documents(query)
    
    # Available ops
    pipeline_ops = [
        {"$match": {"origen": {"$in": ["toctoc", "yapo"]}, "classification.state": {"$in": ["DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"]}}},
        {"$group": {"_id": None, "ops": {"$addToSet": "$operacion"}}}
    ]
    ops_result = list(coll.aggregate(pipeline_ops))
    raw_ops = ops_result[0]["ops"] if ops_result else []
    available_ops = []
    for o in raw_ops:
        if o and "venta" in str(o).lower():
            available_ops.append("venta")
        if o and "arriend" in str(o).lower():
            available_ops.append("arriendo")
    if not available_ops:
        available_ops = ["venta", "arriendo"]
    
    skip = (page - 1) * limit
    sort_fields = {
        "comuna": "comuna_slug",
        "precio": "precio_uf",
        "owner_probability": "classification.owner_probability",
    }
    sort_keys = [s.strip() for s in str(sort_by or "").split(",") if s.strip()]
    sort_dirs = [s.strip().lower() for s in str(sort_dir or "").split(",") if s.strip()]
    sort_specs = []
    for index, key in enumerate(sort_keys):
        if key not in {*sort_fields, "antiguedad"} or key in [s[0] for s in sort_specs]:
            continue
        direction = 1 if index < len(sort_dirs) and sort_dirs[index] == "asc" else -1
        sort_specs.append((key, direction))
    projection = {"descripcion": 0, "description": 0, "historial": 0}
    if any(key == "antiguedad" for key, _ in sort_specs):
        # Antigüedad ascendente = menos días primero, por eso la fecha va
        # descendente. $ifNull mantiene el orden global aunque falte updated_at.
        aggregate_sort = {}
        for key, direction in sort_specs:
            aggregate_sort["_captacion_sort_date" if key == "antiguedad" else sort_fields[key]] = (
                -direction if key == "antiguedad" else direction
            )
        aggregate_sort["_id"] = -1
        cursor = coll.aggregate([
            {"$match": query},
            {"$addFields": {"_captacion_sort_date": {
                "$ifNull": ["$first_seen", {"$ifNull": ["$first_seen_at", {
                    "$ifNull": ["$created_at", {"$ifNull": ["$fecha_captura", {
                        "$ifNull": ["$processed_at", "$updated_at"]
                    }]}]
                }]}]
            }}},
            {"$sort": aggregate_sort},
            {"$skip": skip},
            {"$limit": limit},
            {"$project": {"descripcion": 0, "description": 0, "historial": 0,
                           "_captacion_sort_date": 0}},
        ])
    else:
        mongo_sort = ([(sort_fields[key], direction) for key, direction in sort_specs] + [("_id", -1)]
                      if sort_specs else [("updated_at", -1), ("_id", -1)])
        cursor = coll.find(query, projection).sort(mongo_sort).skip(skip).limit(limit)
    
    items_paginated = []
    for doc in cursor:
        norm = normalize_captacion_document(doc)
        gestion = norm["gestion"]
        fecha_ref = get_captacion_capture_datetime(doc)
        
        dias_portal = 0
        fecha_str = "S/I"
        if fecha_ref:
            try:
                dt_base = fecha_ref
                if isinstance(dt_base, str):
                    dt_base = datetime.fromisoformat(dt_base.replace("Z", "+00:00"))
                if dt_base.tzinfo is None:
                    dt_base = CHILE_TZ.localize(dt_base)
                elif dt_base.tzinfo != CHILE_TZ:
                    dt_base = dt_base.astimezone(CHILE_TZ)
                dias_portal = max(0, (get_chile_now().date() - dt_base.date()).days)
                fecha_str = dt_base.strftime("%d-%m-%Y")
            except Exception:
                pass
        
        # Resolver operacion display
        op_display = "VENTA"
        op_raw = norm.get("operacion", "")
        if op_raw and "arr" in str(op_raw).lower():
            op_display = "ARRIENDO"
        elif op_raw and "vent" in str(op_raw).lower():
            op_display = "VENTA"
        
        # Calcular UF/m2
        uf_m2_val = 0
        pu = norm["precio_uf"]
        doc_details = doc.get("details", {}) or {}
        m2 = doc_details.get("m2_total") or doc_details.get("m2_construidos") or 0
        if pu and m2 and float(m2) > 0:
            uf_m2_val = round(float(pu) / float(m2), 1)

        items_paginated.append({
            "id": norm["id"],
            "url": norm["url"],
            "titulo": norm["titulo"],
            "comuna": norm["comuna"],
            "comuna_slug": norm["comuna_slug"],
            "operacion": op_display,
            "precio": str(norm["precio"]).split("Ref.")[0].strip() if norm["precio"] else "S/I",
            "precio_uf": norm["precio_uf"],
            "precio_display": norm["precio_display"],
            "precio_uf_display": norm["precio_uf_display"],
            "precio_clp_display": norm["precio_clp_display"],
            "price_source_warning": detect_source_price_warning(
                op_display, norm["precio_uf"], norm["precio_clp"]
            ),
            "owner_probability_display": norm["owner_probability_display"],
            "owner_probability_sort": norm["owner_probability_sort"],
            "owner_probability_title": norm["owner_probability_title"],
            "uf_m2": uf_m2_val,
            "estado": gestion.get("estado", "NUEVO"),
            "ejecutivo": gestion.get("ejecutivo_asignado") or "Sin asignar",
            "ejecutivo_id": gestion.get("ejecutivo_id"),
            "score_captacion": norm["score_captacion"],
            "probabilidad": norm["probabilidad"],
            "classification_state": norm["classification_state"],
            "portal_label": norm["portal_label"],
            "origen": norm["origen"],
            "intentos": gestion.get("intent_count", 0),
            "fecha_detectado": format_relative_time(fecha_ref),
            "sort_date": fecha_ref or "",
            "dias_en_portal": dias_portal,
            "fecha_str": fecha_str,
        })
    
    set_cached_value(response_cache_key, {"items": items_paginated, "total_count": total_count, "available_ops": available_ops}, expire_seconds=45)
    return items_paginated, total_count, available_ops

def get_captacion_detail(obj_id):
    from bson import ObjectId
    from bson.errors import InvalidId

    _detail_cache_key = f"detail_full_{obj_id}"
    _cached = _l1_get(_detail_cache_key)
    if _cached is not None:
        return _cached

    db = get_db()
    coll = get_captacion_collection(db)
    
    try:
        query_id = ObjectId(obj_id)
    except InvalidId:
        query_id = obj_id
        
    doc = coll.find_one({"_id": query_id})
    if not doc:
        return None
    
    norm = normalize_captacion_document(doc)
    details = norm["details"]
    gestion = norm["gestion"]
    
    # Antigüedad
    dias_portal = gestion.get("dias_en_portal", 0)
    try:
        dias_portal = int(dias_portal) if (dias_portal is not None and str(dias_portal) != "") else 0
    except:
        dias_portal = 0
    label_antiguedad = "Publicado"
    
    if dias_portal <= 0:
        dt_base = get_captacion_capture_datetime(doc)
        if dt_base:
            try:
                if isinstance(dt_base, str):
                    dt_base = datetime.fromisoformat(dt_base.replace("Z", "+00:00"))
                if dt_base.tzinfo is None:
                    dt_base = CHILE_TZ.localize(dt_base)
                elif dt_base.tzinfo != CHILE_TZ:
                    dt_base = dt_base.astimezone(CHILE_TZ)
                dias_portal = max(0, (get_chile_now().date() - dt_base.date()).days)
                label_antiguedad = "Captado"
            except Exception as e:
                logger.error(f"Error calculating antiquity for {obj_id}: {e}")
    
    gestion["dias_en_portal"] = dias_portal
    gestion["label_antiguedad"] = label_antiguedad
    
    market = get_market_insights(norm["comuna"], norm["tipo_propiedad"] or "Departamento")
    score, prob, motivos, uf_m2, diff_pct = calculate_lead_score_captacion(details, market)
    
    price_uf = norm["precio_uf"]
    m2 = details.get("m2_total")
    
    # Teléfono
    raw_phone = norm.get("vendedor_telefono") or details.get("whatsapp_phone") or doc.get("whatsapp_phone") or ""
    vendedor_telefono = "".join(filter(str.isdigit, str(raw_phone)))
    if vendedor_telefono.startswith("9") and len(vendedor_telefono) == 9:
        vendedor_telefono = "56" + vendedor_telefono
    
    # Nombre
    vendedor_nombre = norm["vendedor_nombre"] or "Propietario"
    if str(vendedor_nombre).lower() in ["particular", "n/a", "no disponible"]:
        vendedor_nombre = "Propietario"
    else:
        vendedor_nombre = str(vendedor_nombre).split()[0].capitalize()
    
    vendedor_email = norm.get("vendedor_email") or details.get("email") or details.get("vendedor_email") or ""
    notas_contacto = doc.get("notas_contacto") or ""
    
    pipeline_stages = [
        "Por contactar", "Contacto exitoso", "Sin respuesta", "Teléfono inválido",
        "Corredor", "Propiedad no disponible", "Publicación expirada", "No interesado",
        "Reunión agendada", "Captado", "Descartado"
    ]
    
    estado_actual = gestion.get("estado_captacion") or gestion.get("estado") or "Por contactar"
    if estado_actual in ["GESTION", "NUEVO", "DETECTADO", "INTENTO DE CONTACTO"]:
        estado_actual = "Por contactar"
    
    intent_count = gestion.get("intent_count", 0)
    
    comuna_name = norm["comuna"] or "su comuna"
    
    # Matching
    ma = {"exact": 0, "zone": 0, "broad": 0, "top_leads": [], "pitch_text": "Cargando demanda real...", "active_recent": 0, "high_match": 0}
    
    next_action = get_next_action_recommendation(
        score, diff_pct, details.get("dias_en_portal"),
        vendedor_nombre, comuna_name, price_uf, intent_count=intent_count, matching_data=ma
    )
    
    saludo = f"Hola {vendedor_nombre}" if vendedor_nombre != "Propietario" else "Hola"
    total_leads = ma.get("exact", 0) + ma.get("zone", 0)
    sales_count = market.get("sales_count", 0)
    sector_name = market.get("normalized_commune", comuna_name)
    avg_cierre = market.get("avg_uf_m2", 0)
    
    days_published = 0
    first_seen = doc.get("first_seen") or gestion.get("first_seen")
    if first_seen:
        try:
            fs_dt = first_seen
            if isinstance(fs_dt, str):
                fs_dt = datetime.fromisoformat(fs_dt.replace('Z', '+00:00'))
            days_published = (datetime.now(fs_dt.tzinfo if fs_dt.tzinfo else timezone.utc) - fs_dt).days
        except:
            pass
    
    if sales_count > 3:
        default_template = "gancho_cbr"
    elif total_leads > 3:
        default_template = "gancho_demanda"
    else:
        default_template = "gancho_suave"
    
    wa_templates = [
        {"id": "gancho_cbr", "label": "🔥 Gancho CBR", "text": f"{saludo}, ¿cómo estás? 👋\n\nEstuve revisando tu propiedad en {sector_name} y analizando ventas reales del Conservador en esa zona.\n\nHoy hay diferencias importantes entre lo que se publica y lo que realmente se está cerrando.\n\nTengo esos datos específicos de tu sector. ¿Te interesa que te los comparta?"},
        {"id": "gancho_demanda", "label": "🎯 Gancho Demanda", "text": f"{saludo}, ¿cómo estás? 👋\n\nTe escribo porque estamos trabajando con compradores activos buscando propiedades como la tuya en {sector_name}.\n\nPero no todas las propiedades están logrando conectar con esa demanda.\n\n¿Aún la tienes disponible?"},
        {"id": "gancho_suave", "label": "👋 Gancho Suave", "text": f"{saludo}, ¿cómo estás? 👋\n\nVi tu propiedad en {sector_name} y quería saber si aún la tienes disponible.\n\nTe pregunto porque el mercado en tu zona se está moviendo y puede haber una oportunidad si se trabaja bien."},
        {"id": "followup_1", "label": "🔁 Follow-up 1 (Data)", "text": f"{saludo}, te escribo de nuevo porque estuve revisando datos recientes de ventas en tu zona.\n\nHay propiedades que se están vendiendo bien cuando están correctamente posicionadas.\n\nSi aún la tienes disponible, vale la pena revisarlo con datos reales."},
        {"id": "respuesta_suave", "label": "💬 Respuesta Suave", "text": "Buenísimo 👍\n\nPara entender bien, ¿estás buscando vender ahora o solo evaluando opciones?"}
    ]
    
    # Historial
    historial = []
    notas_raw = gestion.get("notas", [])
    if isinstance(notas_raw, list):
        for n in notas_raw[-50:]:
            historial.append({
                "fecha": format_relative_time(n.get("timestamp")),
                "nota": n.get("content", ""),
                "usuario": n.get("usuario", "Sistema"),
                "canal": n.get("canal", "Desconocido")
            })
        historial.reverse()
    
    _result = dict(norm)
    _result.update({
        "ma": ma,
        "m2_total": m2,
        "uf_m2": uf_m2,
        "dormitorios": details.get("dormitorios"),
        "banos": details.get("banos"),
        "score_captacion": score,
        "probabilidad": prob,
        "motivos_score": motivos,
        "wa_templates": wa_templates,
        "vendedor_nombre": vendedor_nombre,
        "vendedor_telefono": vendedor_telefono,
        "vendedor_email": vendedor_email,
        "notas_contacto": notas_contacto,
        "estado_captacion": estado_actual,
        "pipeline_stages": pipeline_stages,
        "default_template_id": default_template,
        "days_published": days_published,
        "sales_count": sales_count,
        "avg_cierre": avg_cierre,
        "sector_name": sector_name,
        "total_leads": total_leads,
        "next_action": next_action,
        "dynamic_actions": doc.get("dynamic_actions", {}),
        "market_stats": market,
        "diff_pct": diff_pct,
        "overprice_pct": diff_pct if diff_pct > 0 else 0,
        "intent_count": intent_count,
        "historial": historial,
        "seduction_context": doc.get("seduction_context", {}),
        "cluster_id": (doc.get("metadata") or {}).get("cluster_id") or doc.get("cluster_id"),
        "zone": (doc.get("metadata") or {}).get("zone") or doc.get("zone"),
        "tipo": norm["tipo_propiedad"],
        "operacion": norm["operacion"],
        "classification_state": norm["classification_state"],
        "classification_source": norm["classification_source"],
        "classification_evidence": norm["classification_evidence"],
        "portal_label": norm["portal_label"],
        "origen": norm["origen"],
        "is_incierto": norm["classification_state"] == "INCIERTO",
    })
    
    _l1_set(_detail_cache_key, _result, expire_seconds=45)
    return _result

def update_captacion_status(obj_id, status, notes=None, channel=None, outcome=None, user_name="Sistema", next_followup=None):
    db = get_db()
    
    now = get_chile_now() # Store as Date object, not string
    
    try:
        query_id = ObjectId(obj_id)
    except Exception:
        query_id = str(obj_id)
        
    current_doc = get_captacion_collection(db).find_one({"_id": query_id})
    if not current_doc:
        current_doc = get_captacion_collection(db).find_one({"_id": str(obj_id)})
        if not current_doc:
            return False

        
    old_status = current_doc.get("gestion", {}).get("estado_captacion") or current_doc.get("gestion", {}).get("estado") or "NUEVO"
    
    # 1. Preparar campos de actualización de alto nivel
    update_fields = {
        "gestion.estado": status,
        "gestion.estado_captacion": status,
        "gestion.fecha_ultima_gestion": now
    }
    
    if next_followup:
        update_fields["gestion.next_followup"] = next_followup
        # Crear tarea en crm_tasks
        try:
            # Handle formats: "2023-12-31 15:00" or ISO
            date_str = next_followup.replace("T", " ").replace("Z", "")
            try:
                execute_at = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
            except ValueError:
                execute_at = datetime.fromisoformat(date_str)
            
            # Localize to Chile
            if execute_at.tzinfo is None:
                execute_at = CHILE_TZ.localize(execute_at)

            obj_id_str = str(obj_id)
            # Deduplicación por captación: el teléfono es un dummy, así que
            # usamos obj_id para evitar crear 2 o 3 recordatorios para la misma gestión.
            db["crm_tasks"].update_many(
                {"lead_type": "captacion", "obj_id": obj_id_str, "status": "pending"},
                {"$set": {"status": "completed", "resolved_at": now, "resolution": "superseded"}}
            )
                
            task = {
                "task_id": str(uuid.uuid4()),
                "lead_type": "captacion",
                "phone": "+56900000000",
                "obj_id": obj_id_str,
                "target_name": user_name,
                "type": "REMINDER_CAPTACION",
                "status": "pending",
                "execute_at": execute_at,
                "created_at": now,
                "note": (
                    str(notes).strip() if notes and str(notes).strip()
                    else f"Contactar captación: {current_doc.get('title') or current_doc.get('details', {}).get('titulo', 'Sin título')} (Score: {current_doc.get('score_captacion', 0)})"
                ),
                "agent": user_name
            }
            db["crm_tasks"].insert_one(task)
        except Exception as e:
            logger.error(f"Error scheduling captacion task: {e}")
    
    # 2. Incrementar contador de intentos si es una acción de contacto
    inc_fields = {}
    if outcome or (notes and "Intento" in notes):
        inc_fields["gestion.intent_count"] = 1
        update_fields["gestion.last_contact"] = now
        if channel: update_fields["gestion.last_channel"] = channel

    # 3. Empujar nota estructurada y estado
    push_fields = {}
    if notes or status:
        push_fields["gestion.notas"] = {
            "content": notes or f"Cambio de estado a {status}",
            "timestamp": now,
            "usuario": user_name,
            "canal": channel or "Manual",
            "resultado": outcome
        }
    
    # Si cambió el estado, registrar el cambio
    if old_status != status:
        push_fields["gestion.status_history"] = {
            "timestamp": now,
            "user": user_name,
            "from_state": old_status,
            "to_state": status
        }
    
    update_params = {"$set": update_fields}
    if inc_fields: update_params["$inc"] = inc_fields
    if push_fields: update_params["$push"] = push_fields
    
    get_captacion_collection(db).update_one(
        {"_id": current_doc["_id"]},
        update_params
    )
    _invalidate_detail_cache(obj_id)
    
    # Precomputación SaaS: Actualizar métricas de captación
    try:
        from chatbot.metrics import update_captacion_metrics
        update_captacion_metrics(db, obj_id)
    except: pass

    # LOG EVENT CENTRAL: Cambio de Estado
    try:
        log_event(str(obj_id), EventType.STAGE_CHANGE.value, user_name, {
            "old_stage": old_status,
            "new_stage": status,
            "notes": notes,
            "source": "captacion"
        })
    except Exception as e:
        logger.error(f"Error logging status change event: {e}")

    return True

def update_contact_info(obj_id, nombre=None, telefono=None, email=None, notas=None, user_name="Sistema"):
    db = get_db()
    
    try:
        query_id = ObjectId(obj_id)
    except Exception:
        query_id = str(obj_id)
        
    current_doc = get_captacion_collection(db).find_one({"_id": query_id})
    if not current_doc:
        current_doc = get_captacion_collection(db).find_one({"_id": str(obj_id)})
        if not current_doc:
            return False
        
    details = current_doc.get("details", {})
    
    update_fields = {}
    audit_changes = []
    now = get_chile_now()
    
    if nombre and nombre != details.get("publicador"):
        update_fields["details.publicador"] = nombre
        audit_changes.append({
            "timestamp": now,
            "user": user_name,
            "field": "nombre",
            "old_value": details.get("publicador"),
            "new_value": nombre
        })
        
    if telefono:
        clean_phone = "".join(filter(str.isdigit, str(telefono)))
        if clean_phone != details.get("whatsapp_phone"):
            update_fields["details.whatsapp_phone"] = clean_phone
            audit_changes.append({
                "timestamp": now,
                "user": user_name,
                "field": "telefono",
                "old_value": details.get("whatsapp_phone"),
                "new_value": clean_phone
            })
            
    if email and email != details.get("email"):
        update_fields["details.email"] = email
        audit_changes.append({
            "timestamp": now,
            "user": user_name,
            "field": "email",
            "old_value": details.get("email"),
            "new_value": email
        })
        
    if notas and notas != current_doc.get("notas_contacto"):
        update_fields["notas_contacto"] = notas
        # No audit for free-text notes, just update it
    
    update_params = {}
    if update_fields:
        update_params["$set"] = update_fields
    if audit_changes:
        update_params["$push"] = {"audit.contact_changes": {"$each": audit_changes}}
        
    if update_params:
        get_captacion_collection(db).update_one(
            {"_id": current_doc["_id"]},
            update_params
        )
        _invalidate_detail_cache(obj_id)
        
        # LOG EVENT CENTRAL: Registro de Teléfono
        if telefono:
            try:
                log_event(str(obj_id), EventType.REGISTER_PHONE.value, user_name, {
                    "phone_registered": telefono,
                    "source": "captacion"
                })
            except Exception as e:
                logger.error(f"Error logging register phone event: {e}")

    return True

def log_captacion_activity(obj_id, user_name, action, channel, message, phone, result, template_used=None):
    db = get_db()
    now = get_chile_now()
    
    activity_entry = {
        "timestamp": now,
        "user": user_name,
        "action": action,
        "channel": channel,
        "message": message,
        "phone": phone,
        "result": result
    }
    
    if template_used:
        activity_entry["template_used"] = template_used
        
    note_content = message
    if template_used and "Plantilla" not in note_content:
        note_content = f"[Plantilla: {template_used}] {message[:100]}..."
        
    note_entry = {
        "content": note_content,
        "timestamp": now,
        "usuario": user_name,
        "canal": channel,
        "resultado": result
    }
    
    get_captacion_collection(db).update_one(
        {"_id": ObjectId(obj_id)},
        {"$push": {
            "gestion.actividades": activity_entry,
            "gestion.notas": note_entry
        }, "$set": {"gestion.fecha_ultima_gestion": now}}
    )
    _invalidate_detail_cache(obj_id)
    
    # LOG EVENT CENTRAL: Gestión de Captación
    try:
        log_event(ObjectId(obj_id), EventType.GESTION_CAPTACION, user_name, {
            "action": action,
            "channel": channel,
            "result": result,
            "message_summary": message[:100] if message else "",
            "phone_target": phone,
            "source": "captacion"
        })
    except Exception as e:
        logger.error(f"Error logging captacion activity event: {e}")

    return True

def normalize_commune(name):
    """
    Normalización profunda de comunas para matching.
    Maneja: acentos, minúsculas, caracteres especiales, y sinónimos comunes.
    """
    if not name: 
        return "unknown"
        
    import unicodedata
    # 1. Básicos: Lowercase y Strip
    name = str(name).lower().strip()
    
    # 2. Sinónimos Críticos
    mapping = {
        "stgo": "santiago",
        "santiago centro": "santiago",
        "nunoa": "ñuñoa",
        "vina del mar": "viña del mar",
        "pena lolen": "peñalolen",
        "penalolen": "peñalolen"
    }
    if name in mapping:
        name = mapping[name]
        
    # 3. Remover acentos y caracteres raros
    name = "".join(c for c in unicodedata.normalize('NFD', name) if unicodedata.category(c) != 'Mn')
    name = name.replace("-", " ").replace("_", " ")
    
    # 4. Limpieza final: solo letras y números
    name = re.sub(r'[^a-z0-9 ]', '', name)
    # Colapsar espacios múltiples y strip
    name = " ".join(name.split())
    
    return name if name else "unknown"

def generate_cluster_id(prop_data):
    """
    Genera un ID único de segmento para matching masivo.
    Formato: [COMUNA]-[TIPO]-[OPERACION]
    Handle de datos anidados (algunas colecciones usan 'details')
    """
    # Intentar obtener datos de la raíz, de 'metadata' o de 'details'
    metadata = prop_data.get("metadata", {})
    details = prop_data.get("details", {})
    
    # Priorizar cluster_id ya existente
    existing_cluster = prop_data.get("cluster_id") or metadata.get("cluster_id")
    if existing_cluster:
        return existing_cluster

    comuna_raw = prop_data.get("comuna") or details.get("comuna") or metadata.get("comuna") or "desconocida"
    comuna = normalize_commune(comuna_raw)
    
    tipo_raw = (prop_data.get("tipo") or prop_data.get("tipo_propiedad") or 
                details.get("tipo") or details.get("tipo_propiedad") or 
                metadata.get("tipo") or "").lower()
    
    if "depto" in tipo_raw or "departamento" in tipo_raw:
        tipo = "DEPTO"
    elif "casa" in tipo_raw:
        tipo = "CASA"
    elif "oficina" in tipo_raw:
        tipo = "OFICINA"
    elif "local" in tipo_raw:
        tipo = "LOCAL"
    elif "sitio" in tipo_raw or "terreno" in tipo_raw:
        tipo = "TERRENO"
    else:
        tipo = "OTRO"
        
    op_raw = (prop_data.get("operacion") or details.get("operacion") or 
              prop_data.get("tipo_operacion") or details.get("tipo_operacion") or 
              metadata.get("operacion") or "").lower()
              
    if "arriendo" in op_raw or "alquiler" in op_raw:
        op = "A"
    else:
        op = "V"
        
    return f"{comuna.upper()}-{tipo}-{op}"

# ============================================
# SISTEMA INTELIGENTE DE MATCHING (3 CAPAS)
# ============================================

# Macro-zonas geográficas (cada comuna en UNA sola zona)
MACRO_ZONES = {
    "RM-SUR": [
        "la florida", "san miguel", "la cisterna", "san joaquin",
        "la granja", "lo espejo", "san ramon", "la pintana",
        "el bosque", "pedro aguirre cerda", "puente alto"
    ],
    "RM-CENTRO": [
        "santiago", "santiago centro", "estacion central",
        "independencia", "recoleta", "quinta normal"
    ],
    "RM-ORIENTE": [
        "providencia", "nunoa", "ñuñoa", "las condes", "vitacura",
        "lo barnechea", "la reina", "penalolen", "peñalolen", "macul"
    ],
    "RM-PONIENTE": [
        "maipu", "cerrillos", "pudahuel", "lo prado",
        "cerro navia", "renca"
    ],
    "RM-NORTE": [
        "quilicura", "huechuraba", "conchali", "colina", "lampa"
    ],
    "COSTA-V": [
        "vina del mar", "viña del mar", "valparaiso", "concon",
        "quilpue", "villa alemana"
    ],
    "LITORAL": [
        "el tabo", "el quisco", "algarrobo", "cartagena",
        "santo domingo", "san antonio"
    ],
}

# Lookup directo: comuna_normalizada -> zona (Diseño Unívoco)
_COMUNA_TO_ZONE = {}
for zone_name, comunas in MACRO_ZONES.items():
    for c in comunas:
        c_norm = normalize_commune(c)
        # Prioridad: No sobreescribir si ya existe (evita duplicados como San Joaquín)
        if c_norm not in _COMUNA_TO_ZONE:
            _COMUNA_TO_ZONE[c_norm] = zone_name


def get_zone_for_comuna(comuna_raw):
    """Retorna la macro-zona para una comuna, o None si no está mapeada."""
    norm = normalize_commune(comuna_raw)
    return _COMUNA_TO_ZONE.get(norm)


def _normalize_tipo(tipo_raw):
    """Normaliza tipo de propiedad a código estándar."""
    if not tipo_raw:
        return "OTRO"
    t = str(tipo_raw).lower()
    if "depto" in t or "departamento" in t or "monoambiente" in t:
        return "DEPTO"
    elif "casa" in t:
        return "CASA"
    elif "oficina" in t:
        return "OFICINA"
    elif "local" in t:
        return "LOCAL"
    elif "sitio" in t or "terreno" in t or "parcela" in t:
        return "TERRENO"
    return "OTRO"


def _normalize_operacion(op_raw):
    """Normaliza operación a V o A."""
    if not op_raw:
        return "V"
    o = str(op_raw).lower()
    if "arriendo" in o or "alquiler" in o or "renta" in o:
        return "A"
    return "V"


def _robust_extract_metadata(prop_data: dict, details: dict) -> dict:
    """
    Fallback: extrae comuna, tipo y operación escaneando texto libre
    cuando los campos estructurados están vacíos.
    Retorna dict con claves: comuna, tipo, operacion (strings crudos).
    """
    # Textos libres para escanear
    url = (prop_data.get("url") or "").lower()
    desc = (details.get("descripcion") or "").lower()
    company = (details.get("nombre_corredora") or details.get("company_name") or "").lower()
    titulo = (details.get("titulo") or "").lower()
    combined = f"{url} {desc} {company} {titulo}"

    result = {"comuna": "", "tipo": "", "operacion": ""}

    # --- 1. Detectar operación ---
    if not result["operacion"]:
        if any(k in combined for k in ["arriendo", "alquiler", "renta", "bienes-raices-alquiler"]):
            result["operacion"] = "Arriendo"
        elif any(k in combined for k in ["venta", "compra", "bienes-raices-venta"]):
            result["operacion"] = "Venta"

    # --- 2. Detectar tipo de propiedad ---
    if not result["tipo"]:
        tipo_keywords = [
            ("Departamento", ["departamento", "dpto", "depto", "apartamento", "apart"]),
            ("Casa", ["casa ", "casas ", "residencia"]),
            ("Oficina", ["oficina", "local comercial", "local"]),
            ("Terreno", ["terreno", "sitio", "parcela"]),
            ("Bodega", ["bodega", "warehouse"]),
        ]
        for tipo_name, kws in tipo_keywords:
            if any(k in combined for k in kws):
                result["tipo"] = tipo_name
                break

    # --- 3. Detectar comuna (escanea contra todas las comunas conocidas) ---
    if not result["comuna"]:
        # Lista de comunas conocidas ordenadas por longitud desc (evita match parcial)
        known_comunas = sorted(COMUNA_TO_ZONE.keys(), key=len, reverse=True)
        for c in known_comunas:
            # Buscar con normalize: ñuñoa → nunoa en el text también
            c_norm = c.replace("ñ", "n").replace("é", "e").replace("á", "a").replace("ó", "o").replace("ú", "u")
            if c in combined or c_norm in combined:
                result["comuna"] = c
                break

    return result

# --- HELPERS & NORMALIZATION ---
# CHILE_TZ imported at top

def get_chile_now():
    """Retorna datetime actual en Chile."""
    return datetime.now(CHILE_TZ)

def ensure_leads_indexes():
    """Asegura índices de performance para propiedades_captacion."""
    try:
        db = get_db()
        coll = get_captacion_collection(db)
        
        # 1. Índice único origen + listing_id
        try:
            coll.create_index(
                [("origen", 1), ("listing_id", 1)],
                unique=True,
                name="idx_captacion_origen_listing_id"
            )
        except Exception:
            pass
        
        # 2. Índice compuesto para asignación
        try:
            coll.create_index([
                ("origen", 1),
                ("classification.state", 1),
                ("comuna_slug", 1),
                ("gestion.ejecutivo_id", 1),
                ("gestion.estado", 1)
            ], name="idx_captacion_asignacion")
        except Exception:
            pass
        
        # 3. Índice para listado de agente
        try:
            coll.create_index([
                ("gestion.ejecutivo_id", 1),
                ("gestion.estado", 1),
                ("comuna_slug", 1),
                ("updated_at", -1)
            ], name="idx_captacion_agente_listado")
        except Exception:
            pass
        
        # 4. Índice para clasificación
        try:
            coll.create_index([
                ("origen", 1),
                ("classification.state", 1),
                ("comuna_slug", 1)
            ], name="idx_captacion_clasificacion")
        except Exception:
            pass
        
        # 5. Índice para priorización global antes de paginar
        try:
            coll.create_index([
                ("origen", 1),
                ("classification.state", 1),
                ("classification.owner_probability", -1),
                ("_id", -1),
            ], name="idx_captacion_owner_probability")
        except Exception:
            pass

        # 6. Índice TTL para caché persistente
        try:
            db["system_cache"].create_index("expires_at", expireAfterSeconds=0)
        except Exception:
            pass
        
        logger.info("Índices de captación optimizados en propiedades_captacion.")
    except Exception as e:
        logger.error(f"Error creando indices: {e}")

def _get_lead_days_old(lead):
    # Priorizamos la última actividad conocida sobre la fecha de creación
    created = lead.get("created_at") or lead.get("fecha_creacion")
    last_act = lead.get("ultima_actualizacion_bi")
    
    # Convertir ambos a datetime para comparar
    def parse_dt(val):
        if not val: return None
        try:
            if isinstance(val, str):
                # Handle YYYY-MM-DD HH:MM:SS format from BI
                if " " in val and ":" in val and "+" not in val and "Z" not in val:
                    dt = datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
                else:
                    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            else:
                dt = val
            
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CHILE_TZ)
            return dt.astimezone(CHILE_TZ)
        except Exception:
            return None

    dt_created = parse_dt(created)
    dt_last = parse_dt(last_act)
    
    # Usar el más reciente
    effective_dt = dt_last or dt_created
    if not effective_dt:
        return 0
        
    diff = get_chile_now() - effective_dt
    return max(0, diff.days)

def _extract_numeric(val):
    """Safely extracts a float from a string or number, handling suffixes like 'UF'."""
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        # Remove anything that's not a digit or a dot (ignoring separators like commas for now)
        # But we must preserve the decimal point. 
        # Actually, simpler: keep only digits and '.'
        clean = "".join(c for c in val if c.isdigit() or c == "." or c == ",")
        if not clean:
            return 0.0
        # If it has a comma and a dot, it's messy. If only comma, replace with dot.
        if "," in clean and "." not in clean:
            clean = clean.replace(",", ".")
        elif "," in clean and "." in clean:
            # Assume 1.234,56 format or similar. Remove the dot (thousands) and replace comma.
            clean = clean.replace(".", "").replace(",", ".")
        
        try:
            return float(clean)
        except ValueError:
            return 0.0
    return 0.0


def _lead_recency_weight(lead):
    """
    Peso por recencia del lead (últimos 30d = 1.0, 30-90d = 0.5, >90d = 0.25).
    """
    days = _get_lead_days_old(lead)
    if days <= 30: return 1.0
    elif days <= 90: return 0.5
    else: return 0.25



def normalize_commune_v2(c):
    if not c: return ""
    c = str(c).strip().lower()
    c = c.replace("-", " ")
    c = " ".join(c.split())
    mapping = {"stgo": "santiago", "santiago centro": "santiago"}
    return mapping.get(c, c)

COMUNA_TO_ZONE = {
    "san miguel": "RM-SUR", "la florida": "RM-SUR", "macul": "RM-SUR", "puente alto": "RM-SUR", "san bernardo": "RM-SUR",
    "peñalolen": "RM-ORIENTE", "penalolen": "RM-ORIENTE", "nunoa": "RM-ORIENTE", "ñuñoa": "RM-ORIENTE",
    "providencia": "RM-ORIENTE", "las condes": "RM-ORIENTE", "vitacura": "RM-ORIENTE", "lo barnechea": "RM-ORIENTE", "la reina": "RM-ORIENTE",
    "santiago": "RM-CENTRO", "estacion central": "RM-CENTRO",
    "independencia": "RM-NORTE", "recoleta": "RM-NORTE", "huechuraba": "RM-NORTE", "conchali": "RM-NORTE", "quilicura": "RM-NORTE",
    "pudahuel": "RM-PONIENTE", "maipu": "RM-PONIENTE", "cerrillos": "RM-PONIENTE"
}

def classify_lead_quality(lead_price, prop_price, days):
    score = 0
    diff = 1.0 # Max diff if missing price
    if prop_price > 0 and lead_price > 0:
        diff = abs(lead_price - prop_price) / prop_price
        if diff < 0.2: score += 2
        elif diff < 0.35: score += 1
    if days < 30: score += 2
    elif days < 90: score += 1
    
    if score >= 3: return "high", diff
    elif score == 2: return "medium", diff
    else: return "low", diff

def get_matching_leads_analysis(prop_data):
    """
    Motor de Matching Profesional - Demanda Real, Activa y Creíble.
    """
    result = {
        "exact": 0, "zone": 0, "broad": 0,
        "active_recent": 0, "high_match": 0, "medium_match": 0,
        "top_leads": [],
        "zone_name": "", "cluster_id": "", "pitch_text": "",
        "debug": {"total": 0, "after_operation": 0, "after_activity": 0, "after_price": 0}
    }
    
    try:
        details = prop_data.get("details", {})
        comuna_raw = prop_data.get("comuna") or details.get("comuna") or ""
        tipo_raw = prop_data.get("tipo") or prop_data.get("tipo_propiedad") or details.get("tipo_propiedad") or ""
        op_raw = prop_data.get("operacion") or details.get("operacion") or details.get("tipo_operacion") or ""

        # --- FALLBACK: Extracción robusta desde texto libre si faltan campos ---
        if not comuna_raw or not tipo_raw or not op_raw:
            fallback = _robust_extract_metadata(prop_data, details)
            if not comuna_raw:
                comuna_raw = fallback.get("comuna", "")
            if not tipo_raw:
                tipo_raw = fallback.get("tipo", "")
            if not op_raw:
                op_raw = fallback.get("operacion", "")

        tipo_code = _normalize_tipo(tipo_raw)
        prop_price = _extract_numeric(details.get("precio_uf") or prop_data.get("precio_uf") or 0)
        
        # Heurística: Si no hay operación pero el precio es bajísimo (< 1000 UF), es Arriendo
        if not op_raw and prop_price > 0 and prop_price < 1000:
            op_code = "A"
        else:
            op_code = _normalize_operacion(op_raw)
            
        comuna_norm = normalize_commune_v2(comuna_raw)
        zone = COMUNA_TO_ZONE.get(comuna_norm) or get_zone_for_comuna(comuna_raw) or "Sin zona"
        
        # Filtro Inteligente de Precio Máximo
        if prop_price < 3000: max_diff = 0.3    # ~ < 110M
        elif prop_price < 4000: max_diff = 0.35 # ~ < 150M
        else: max_diff = 0.4
            
        result["cluster_id"] = f"{comuna_norm.upper()}-{tipo_code}-{op_code}"
        result["zone_name"] = zone
    except Exception as e:
        logger.error(f"Error metadata match: {e}")
        return result

    try:
        db = get_db()
        
        # 1. Preparar filtros DB (Grok Opt)
        now = get_chile_now()
        date_limit = now - timedelta(days=90)
        
        # Filtro de precio base para DB (luego se refina en Python)
        min_lead_price = prop_price * (1 - max_diff)
        max_lead_price = prop_price * (1 + max_diff)
        
        query = {
            "operacion": op_code,
            "estado": {"$in": ["Por contactar", "Contacto exitoso", "Sin respuesta", "Reunión agendada", "Captado", "NUEVO", "DETECTADO"]},
            "comuna_norm": comuna_norm,
            "tipo": tipo_code,
            "ultima_actualizacion_bi": {"$gte": date_limit} # Native datetime object for indexed performance
        }
        
        # Solo agregar filtro de precio si prop_price es válido (Inclusivo para leads sin precio)
        if prop_price > 0:
            query["$or"] = [
                {"prospecto.presupuesto_uf": {"$exists": False}},
                {"prospecto.presupuesto_uf": 0},
                {"prospecto.presupuesto_uf": {"$gte": min_lead_price, "$lte": max_lead_price}},
                {"prospecto.precio": {"$gte": min_lead_price, "$lte": max_lead_price}}
            ]

        # Proyección positiva para máxima velocidad (Senior Opt)
        projection = {
            "prospecto": 1,
            "operacion": 1,
            "estado": 1,
            "comuna_norm": 1,
            "tipo": 1,
            "ultima_actualizacion_bi": 1
        }

        # Query optimizada con limite, sort y proyeccion (Grok/Senior Opt)
        all_active_leads = list(db["leads"].find(
            query, 
            projection
        ).sort("ultima_actualizacion_bi", -1).limit(50))
        result["debug"]["total"] = len(all_active_leads)

        valid_leads = []
        result["exact_leads"] = []
        result["zone_leads"] = []
        result["broad_leads"] = []
        c_op = 0; c_act = 0; c_price = 0

        for lead in all_active_leads:
            prospecto = lead.get("prospecto", {})
            lead_op = prospecto.get("operacion") or lead.get("operacion") or ""
            lead_op_code = _normalize_operacion(lead_op)
            
            # FILTRO 1: Operacion
            if lead_op_code != op_code: continue
            c_op += 1
            
            # FILTRO 2: Actividad
            days_old = int(_get_lead_days_old(lead))
            if days_old > 90: continue
            c_act += 1
            
            # FILTRO 3: Precio
            lead_price = _extract_numeric(prospecto.get("presupuesto_uf") or prospecto.get("precio") or 0)
            if prop_price > 0 and lead_price > 0:
                diff = abs(lead_price - prop_price) / prop_price
                if diff > max_diff: continue
            c_price += 1
            
            # EVALUACION DE CALIDAD
            quality_label, actual_diff = classify_lead_quality(lead_price, prop_price, days_old)
            
            # FILTRO 4: Tipo de Propiedad (Básico Broad)
            lead_tipo = _normalize_tipo(prospecto.get("tipo") or prospecto.get("tipo_propiedad") or lead.get("tipo_interes") or "")
            if lead_tipo != tipo_code: continue
            
            # MATCHING EXCLUYENTE
            lead_comuna = normalize_commune_v2(prospecto.get("comuna") or lead.get("comuna_interes") or "")
            is_exact = (lead_comuna == comuna_norm)
            
            lead_zone = COMUNA_TO_ZONE.get(lead_comuna) or get_zone_for_comuna(lead_comuna)
            is_zone = False
            if not is_exact:
                if (zone and lead_zone and lead_zone == zone):
                    is_zone = True
                elif zone:
                    pref_str = str(prospecto.get("comunas_preferidas") or "").lower()
                    if normalize_commune_v2(zone) in pref_str or lead_comuna in pref_str: 
                        is_zone = True
            
            bucket = "broad"
            if is_exact:
                result["exact"] += 1
                bucket = "exact"
            elif is_zone:
                result["zone"] += 1
                bucket = "zone"
            else:
                result["broad"] += 1
                
            # COUNTEOS EXTRA
            # Threshold sugerido: 30 días para Arriendo (A), 60 días para Venta (V)
            threshold = 30 if op_code == "A" else 60
            if days_old < threshold: result["active_recent"] += 1
            if quality_label == "high": result["high_match"] += 1
            elif quality_label == "medium": result["medium_match"] += 1
            
            # MASK LEAD
            mapped = _mask_lead_for_preview(lead)
            mapped.update({
                "quality_label": quality_label,
                "days_old": int(days_old),
                "bucket": bucket,
                "diff": float(actual_diff)
            })

            valid_leads.append(mapped)
            
            # POPULATE SPECIFIC BUCKETS
            if bucket == "exact" and len(result["exact_leads"]) < 10:
                result["exact_leads"].append(mapped)
            elif bucket == "zone" and len(result["zone_leads"]) < 10:
                result["zone_leads"].append(mapped)
            elif bucket == "broad" and len(result["broad_leads"]) < 10:
                result["broad_leads"].append(mapped)

        result["debug"].update({"after_operation": c_op, "after_activity": c_act, "after_price": c_price})
        
        # GLOBAL TOP LEADS - ORDENADO Y LIMITADO (CTO Opt)
        result["top_leads"] = sorted(valid_leads, key=lambda x: (x["days_old"], x["diff"]))[:20]

        # LÓGICA DE PITCH AUTOMÁTICA
        tipo_display = {"DEPTO": "departamentos", "CASA": "casas", "OFICINA": "oficinas", "LOCAL": "locales", "TERRENO": "terrenos"}.get(tipo_code, "propiedades")
        op_display = "arriendo" if op_code == "A" else "compra"
        comuna_display = comuna_raw or "su sector"
        total_b = result["exact"] + result["zone"] + result["broad"]
        
        if result["exact"] > 0:
            result["pitch_text"] = f"Tenemos {result['exact']} cliente{'s' if result['exact'] > 1 else ''} buscando activamente exactamente una propiedad como la tuya en {comuna_display}."
        elif result["zone"] > 0:
            result["pitch_text"] = f"Tenemos {result['zone']} cliente{'s' if result['zone'] > 1 else ''} activo{'s' if result['zone'] > 1 else ''} buscando en el sector de {result['zone_name']}, varios compatibles con tu propiedad."
        else:
            result["pitch_text"] = f"Tenemos una base activa de {total_b} clientes buscando {tipo_display} asimilables por precio y características."

    except Exception as e:
        logger.error(f"Error in matching logic: {e}")
        
    return result


def _mask_lead_for_preview(lead):
    """Anonymized version for Verification Modal (Privacy Centric). Ensure no ObjectIds."""
    prospecto = lead.get("prospecto", {})
    nombre = str(prospecto.get("nombre") or lead.get("nombre") or "Cliente")
    
    parts = nombre.strip().split()
    if len(parts) >= 2 and len(parts[0]) > 0 and len(parts[1]) > 0:
        initials = f"{parts[0][0].upper()}{parts[1][0].upper()}"
    elif len(nombre) > 0:
        initials = f"{nombre[0].upper()}"
    else:
        initials = "XX"
        
    executive = str(lead.get("ejecutivo_asignado") or lead.get("ejecutivo") or prospecto.get("ejecutivo") or "Sistema")
    
    # Cast codigo to string to avoid ObjectId serialization issues
    codigo_raw = prospecto.get("codigo") or lead.get("datos_propiedad", {}).get("codigo") or lead.get("codigo") or "S/I"
    codigo_str = str(codigo_raw)
    
    return {
        "full_name": f"Cliente {initials}",
        "executive": executive,
        "codigo_consultado": codigo_str
    }

def get_matching_leads_count(prop_data):
    """Wrapper de retrocompatibilidad. Retorna el número más relevante."""
    analysis = get_matching_leads_analysis(prop_data)
    return analysis["exact"] + analysis["zone"] + analysis["broad"]

SLA_CAPTACION_DIAS = 5  # Días de inactividad antes de liberar una captación asignada

def release_stale_captaciones(sla_dias=SLA_CAPTACION_DIAS):
    db = get_db()
    coll = get_captacion_collection(db)
    now_utc = datetime.now(timezone.utc)
    umbral = now_utc - timedelta(days=sla_dias)

    query = {
        "origen": "toctoc",
        "gestion.ejecutivo_id": {"$exists": True, "$ne": None},
        "gestion.estado": {"$in": ["NUEVO", "GESTION"]},
        "$or": [
            {"gestion.fecha_ultima_gestion": {"$exists": False}, "gestion.fecha_asignacion": {"$lt": umbral}},
            {"gestion.fecha_ultima_gestion": {"$lt": umbral}},
        ]
    }

    result = coll.update_many(
        query,
        {"$set": {
            "gestion.ejecutivo_id": None,
            "gestion.ejecutivo_asignado": None,
            "gestion.estado": "NUEVO",
            "gestion.liberada_por_sla": True,
            "gestion.fecha_liberacion": now_utc.isoformat()
        }}
    )

    liberadas = result.modified_count
    if liberadas > 0:
        logger.info(f"[SLA] {liberadas} captacion(es) liberadas por inactividad (>={sla_dias} dias).")
    else:
        logger.info(f"[SLA] Sin captaciones inactivas para liberar.")
    return liberadas


def distribute_sourced_leads():
    """Distribuye propiedades nuevas sin asignar. Versión simplificada para background loop."""
    from bson import ObjectId
    db = get_db()
    coll = get_captacion_collection(db)

    agents = list(db["usuarios"].find({
        "is_active": True,
        "rol": "agente",
        "comunas_interes_norm": {"$exists": True, "$ne": []}
    }))
    if not agents:
        logger.info("[DISTRIBUCION] 0 agentes elegibles.")
        return 0

    comuna_to_agents = {}
    for a in agents:
        aid = str(a["_id"])
        for cn in a.get("comunas_interes_norm", []):
            comuna_to_agents.setdefault(cn, []).append(aid)

    eligible_query = {
        "origen": "toctoc",
        "classification.assignment_ready": True,
        "classification.exclude_from_assignment": {"$ne": True},
        "gestion.semantic_review_hold": {"$ne": True},
        "classification.state": {"$in": ["DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"]},
        "$or": [
            {"gestion.ejecutivo_id": {"$exists": False}},
            {"gestion.ejecutivo_id": None},
        ]
    }
    props = list(coll.find(eligible_query))
    from captacion_assignment_eligibility import assignment_eligibility
    props = [p for p in props if assignment_eligibility(p)[0]]
    logger.info(f"[DISTRIBUCION] {len(props)} propiedades sin asignar.")

    assigned = 0
    now = datetime.now(timezone.utc)
    wl = {a["_id"]: 0 for a in agents}

    for p in props:
        slug = p.get("comuna_slug") or normalize_commune_canonical(p.get("comuna") or "")
        if not slug:
            continue
        candidates = comuna_to_agents.get(slug, [])
        if not candidates:
            continue
        # Ponderado: menor workload
        best = min(candidates, key=lambda aid: wl.get(aid, 0))
        wl[best] = wl.get(best, 0) + 1

        from bson import ObjectId
        oid = p["_id"] if isinstance(p["_id"], ObjectId) else ObjectId(p["_id"])
        coll.update_one(
            {"_id": oid,
             "classification.assignment_ready": True,
             "classification.exclude_from_assignment": {"$ne": True},
             "gestion.semantic_review_hold": {"$ne": True},
             "$or": [{"gestion.ejecutivo_id": {"$exists": False}}, {"gestion.ejecutivo_id": None}]},
            {"$set": {
                "gestion.ejecutivo_id": best,
                "gestion.ejecutivo_asignado": db["usuarios"].find_one({"_id": ObjectId(best) if len(best) == 24 else best}).get("nombre", ""),
                "gestion.fecha_asignacion": now,
                "gestion.asignacion_version": "v1_weighted_commune_background",
                "gestion.estado": "NUEVO",
            }}
        )
        assigned += 1

    logger.info(f"[DISTRIBUCION] Asignadas: {assigned}")
    return assigned

def get_personal_templates(user_name):
    """Retorna las plantillas personalizadas de un usuario."""
    db = get_db()
    templates = list(db["personal_templates"].find({"user_name": user_name}).sort("created_at", -1))
    for t in templates:
        t["_id"] = str(t["_id"])
    return templates

def save_personal_template(user_name, data):
    """Guarda o actualiza una plantilla personalizada."""
    db = get_db()
    data["user_name"] = user_name
    data["created_at"] = get_chile_now().isoformat()
    
    # Limpiar _id si viene vacío o nulo
    if not data.get("_id"):
        data.pop("_id", None)

    if "_id" in data:
        try:
            tid = data.pop("_id")
            db["personal_templates"].update_one(
                {"_id": ObjectId(tid), "user_name": user_name},
                {"$set": data},
                upsert=True
            )
            return str(tid)
        except Exception as e:
            logging.error(f"Error updating template: {e}")
            return None
    else:
        res = db["personal_templates"].insert_one(data)
        return str(res.inserted_id)

def delete_personal_template(template_id, user_name):
    """Elimina una plantilla personalizada asegurando pertenencia."""
    db = get_db()
    try:
        res = db["personal_templates"].delete_one({"_id": ObjectId(template_id), "user_name": user_name})
        return res.deleted_count > 0
    except Exception as e:
        logging.error(f"Error deleting template: {e}")
        return False

# --- AUTO-INITIALIZATION ---
try:
    ensure_leads_indexes()
except:
    pass
