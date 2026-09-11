from config import Config
from datetime import datetime, timezone, timedelta
import pytz
from chatbot.constants import CHILE_TZ
import logging
import uuid
import re
import unicodedata
import threading
import time as _perf_time
from captacion_kpis import (
    VISIBLE_CLASSIFICATION_STATES,
    AVAILABLE_STATES,
    MANAGEMENT_STATES,
    CAPTURED_STATES,
    DISCARDED_STATES,
)
from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from chatbot.storage import get_db, log_event
from chatbot.constants import CHILE_TZ, EventType
from owner_confidence import (
    build_owner_probability_doc,
    detect_source_price_warning,
    resolve_price_display,
)
from captacion_goals import can_manage_captacion
from captacion_management import (
    bump_captacion_kpi_revision,
    confirm_management_attempt,
    evaluate_manual_decision,
    new_assignment_cycle,
    record_manual_management_decision,
    start_management_attempt,
)
from captacion_materialized import build_captacion_materialized_fields
from captacion_assignment_eligibility import assignment_classification_priority
from captacion_contact_identity import (
    get_contact_identity_evidence,
    normalize_phone,
    phone_learning_enabled,
    phone_learning_global_lookup_enabled,
)

logger = logging.getLogger(__name__)

# --- CONFIGURACION CENTRALIZADA ---
# CHILE_TZ is imported from chatbot.constants
MARKET_STATS_CACHE = {} # Legacy - Now using shared_cache in DB
_LOCAL_CACHE_L1 = {}

def get_captacion_collection(db):
    return Config.get_captacion_collection(db)

from comuna_utils import normalize_commune_canonical  # noqa: F401  (canonical, corrige mojibake)

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
    
    # Los datos editados desde "Gestión del Propietario" viven en details.
    # Deben tener prioridad sobre los valores originales del scraper en la raíz
    # del documento; de lo contrario el cambio se guarda, pero la vista vuelve
    # a mostrar el valor antiguo al recargar.
    seller_name = first("details.publicador", "publicador", "details.vendedor_nombre", "seller_name") or ""
    contacto = first(
        "details.whatsapp_phone", "details.contact_phone", "details.telefono",
        "whatsapp_phone", "contact_phone", "telefono",
    ) or ""
    
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
        "vendedor_email": first("details.email", "details.vendedor_email", "email", "vendedor_email"),
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


def format_captacion_portal_label(origin):
    """Nombre legible para un portal detectado desde los datos."""
    value = str(origin or "").strip()
    known_labels = {
        "toctoc": "TocToc",
        "yapo": "Yapo",
        "prop360": "Prop360",
        "portalinmobiliario": "Portal Inmobiliario",
        "mercadolibre": "MercadoLibre",
    }
    return known_labels.get(value.casefold(), value.replace("_", " ").replace("-", " ").title())


def normalize_captacion_property_type(value):
    """Agrupa singular/plural y abreviaturas comunes del tipo de propiedad."""
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""
    normalized = unicodedata.normalize("NFKD", raw_value)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = re.sub(r"[_-]+", " ", normalized.casefold())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    aliases = {
        "casas": "casa",
        "departamentos": "departamento",
        "depto": "departamento",
        "deptos": "departamento",
        "dpto": "departamento",
        "dptos": "departamento",
        "oficinas": "oficina",
        "locales": "local",
        "parcelas": "parcela",
        "terrenos": "terreno",
        "sitios": "sitio",
        "bodegas": "bodega",
        "estacionamientos": "estacionamiento",
        "galpones": "galpon",
    }
    if normalized in aliases:
        return aliases[normalized]
    if len(normalized) > 4 and normalized.endswith("s"):
        normalized = normalized[:-1]
    return normalized


def _captacion_property_type_pattern(value):
    """Incluye variantes históricas al filtrar por el tipo normalizado."""
    canonical = normalize_captacion_property_type(value)
    variants = {
        "casa": ("casa", "casas"),
        "departamento": ("departamento", "departamentos", "depto", "deptos", "dpto", "dptos"),
    }.get(canonical, (canonical, f"{canonical}s" if canonical else ""))
    variants = [re.escape(variant) for variant in variants if variant]
    return re.compile(r"^\s*(?:" + "|".join(variants) + r")\s*$", re.I)


def _parse_captacion_price_bound(value):
    """Normaliza un límite ingresado desde el filtro de monto."""
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip().replace("$", "").replace("UF", "").replace("uf", "")
    raw = re.sub(r"[^0-9,.-]", "", raw)
    if not raw:
        return None
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    elif raw.count(".") == 1:
        left, right = raw.split(".")
        if len(right) == 3 and len(left) <= 3:
            raw = left + right
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _captacion_price_range_condition(currency, price_min=None, price_max=None):
    currency = str(currency or "").strip().upper()
    if currency not in {"UF", "CLP"}:
        return None

    lower = _parse_captacion_price_bound(price_min)
    upper = _parse_captacion_price_bound(price_max)
    if lower is None and upper is None:
        return None
    if lower is not None and upper is not None and lower > upper:
        return {"_id": {"$in": []}}

    bounds = {}
    if lower is not None:
        bounds["$gte"] = lower
    if upper is not None:
        bounds["$lte"] = upper

    suffix = currency.lower()
    normalized_field = f"precio_{suffix}_normalizado"
    source_fields = (
        f"precio_{suffix}",
        f"price_{suffix}",
        f"details.precio_{suffix}",
        f"details.price_{suffix}",
    )
    return {
        "$or": [
            {normalized_field: bounds},
            {
                "$and": [
                    {normalized_field: {"$exists": False}},
                    {"$or": [{field: bounds} for field in source_fields]},
                ]
            },
        ]
    }


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


def _invalidate_captacion_list_cache():
    """Descarta cualquier snapshot local del listado de Captación."""
    for key in list(_LOCAL_CACHE_L1):
        if str(key).startswith("captacion_resp_"):
            _LOCAL_CACHE_L1.pop(key, None)

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


def get_captacion_list(user_role="agente", user_name="", user_id="", user_email="", page=1, limit=10, comuna_filter=None, status_filter=None, executive_filter=None, operacion_filter=None, telefono_filter=None, portal_filter=None, property_type_filter=None, classification_filter=None, price_currency=None, price_min=None, price_max=None, sort_by=None, sort_dir="desc", order_filter=None, gestion_date=None, gestion_week_start=None, perf_context=None, return_portals=False):
    _l_start = _perf_time.perf_counter()
    db = get_db()
    coll = get_captacion_collection(db)
    
    # Base: captaciones elegibles de todos los portales soportados.
    query = {
        "origen": {"$exists": True, "$nin": [None, ""]},
        "classification.state": {"$in": list(VISIBLE_CLASSIFICATION_STATES)}
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
                if executive_doc:
                    executive_ids.append(str(executive_doc["_id"]))
                add_condition({"$or": [
                    {"gestion.ejecutivo_asignado": executive_filter},
                    {"$and": [
                        {"gestion.ejecutivo_asignado": {"$in": [None, ""]}},
                        {"$or": [
                            {"gestion.ejecutivo_id": {"$in": executive_ids}},
                        ]},
                    ]},
                ]})
    elif user_role == "agente":
        assignment_clauses = []
        # Canonico: matchear por ejecutivo_id
        if user_id:
            assignment_clauses.append({"gestion.ejecutivo_id": user_id})
        # Fallback: documentos legacy sin ejecutivo_id, matchear por nombre
        if user_name:
            assignment_clauses.append({"$and": [
                {"gestion.ejecutivo_id": {"$exists": False}},
                {"gestion.ejecutivo_asignado": user_name},
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
        terminal_states = list(CAPTURED_STATES + DISCARDED_STATES)
        if status_filter == "GRUPO_TRABAJADAS":
            query["gestion.estado"] = {
                "$in": list(MANAGEMENT_STATES + CAPTURED_STATES + DISCARDED_STATES)
            }
        elif status_filter in ("GRUPO_GESTION", "GESTION"):
            query["gestion.estado"] = {"$in": list(MANAGEMENT_STATES)}
        elif status_filter in ("GRUPO_CAPTADO", "CAPTADO"):
            query["gestion.estado"] = {"$in": list(CAPTURED_STATES)}
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

    # Catálogo normalizado para que "casa" y "casas" aparezcan como una sola opción.
    type_catalog_query = dict(query)
    if "$and" in type_catalog_query:
        type_catalog_query["$and"] = list(type_catalog_query["$and"])
    type_cache_key = repr(type_catalog_query)
    type_cache = getattr(get_captacion_list, "_property_type_cache", {})
    type_cache_record = type_cache.get(type_cache_key)
    type_cache_ttl = 300
    if type_cache_record and (get_chile_now() - type_cache_record[0]).total_seconds() < type_cache_ttl:
        available_property_types = type_cache_record[1]
    else:
        type_rows = list(coll.aggregate([
            {"$match": type_catalog_query},
            {"$project": {"tipo_catalogo": {"$ifNull": [
                "$tipo_propiedad",
                {"$ifNull": ["$property_type", {"$ifNull": ["$details.tipo_propiedad", "$details.tipo"]}]},
            ]}}},
            {"$match": {"tipo_catalogo": {"$nin": [None, ""]}}},
            {"$group": {"_id": "$tipo_catalogo"}},
        ]))
        type_values = {}
        for row in type_rows:
            canonical_value = normalize_captacion_property_type(row.get("_id"))
            if canonical_value:
                type_values.setdefault(canonical_value, canonical_value)
        available_property_types = [
            {"value": value, "label": value.title()}
            for value in sorted(type_values.values(), key=lambda item: item.casefold())
        ]
        type_cache[type_cache_key] = (get_chile_now(), available_property_types)
        if len(type_cache) > 128:
            oldest_key = min(type_cache, key=lambda key: type_cache[key][0])
            type_cache.pop(oldest_key, None)
        get_captacion_list._property_type_cache = type_cache

    if property_type_filter:
        property_type_pattern = _captacion_property_type_pattern(property_type_filter)
        add_condition({"$or": [
            {"tipo_propiedad": property_type_pattern},
            {"property_type": property_type_pattern},
            {"details.tipo_propiedad": property_type_pattern},
            {"details.tipo": property_type_pattern},
        ]})

    price_condition = _captacion_price_range_condition(
        price_currency, price_min=price_min, price_max=price_max
    )
    if price_condition:
        add_condition(price_condition)
    
    if telefono_filter:
        phone_digits = "".join(char for char in str(telefono_filter) if char.isdigit())
        if phone_digits:
            # El formato canónico permite usar el índice para búsquedas por
            # prefijo. El fallback se limita a documentos sin backfill, para
            # no volver a escanear toda la cartera en cada búsqueda.
            legacy_phone_pattern = re.compile(re.escape(str(telefono_filter)), re.I)
            add_condition({"$or": [
                {"telefono_normalizado": re.compile(f"^{re.escape(phone_digits)}")},
                {"telefono_normalizado": {"$exists": False}, "$or": [
                    {"contact_phone": legacy_phone_pattern},
                    {"whatsapp_phone": legacy_phone_pattern},
                    {"telefono": legacy_phone_pattern},
                    {"details.whatsapp_phone": legacy_phone_pattern},
                    {"details.contact_phone": legacy_phone_pattern},
                    {"details.telefono": legacy_phone_pattern},
                    {"details.phone": legacy_phone_pattern},
                ]},
            ]})
        else:
            tel_pattern = re.compile(re.escape(str(telefono_filter)), re.I)
            add_condition({"$or": [
                {"contact_phone": tel_pattern},
                {"whatsapp_phone": tel_pattern},
                {"telefono": tel_pattern},
                {"details.whatsapp_phone": tel_pattern},
                {"details.contact_phone": tel_pattern},
                {"details.telefono": tel_pattern},
                {"details.phone": tel_pattern},
            ]})

    # El filtro temporal usa la misma fuente de gestiones acreditadas que la
    # tarjeta de metas. Se traduce a IDs de propiedades antes de contar/listar
    # para que el contador y la paginación respeten todos los filtros actuales.
    temporal_portal_base_query = None
    temporal_property_values = None
    if gestion_date or gestion_week_start:
        from captacion_goals import get_captacion_management_rows

        temporal_portal_base_query = dict(query)
        if "$and" in temporal_portal_base_query:
            temporal_portal_base_query["$and"] = list(temporal_portal_base_query["$and"])

        try:
            if gestion_date:
                temporal_start = datetime.strptime(str(gestion_date), "%Y-%m-%d").date()
                temporal_end = temporal_start
            else:
                temporal_start = datetime.strptime(str(gestion_week_start), "%Y-%m-%d").date()
                temporal_end = temporal_start + timedelta(days=6)

            temporal_rows = get_captacion_management_rows(
                db,
                period_start=temporal_start.isoformat(),
                period_end=temporal_end.isoformat(),
            )
            property_values = {
                str(row.get("property_id")).strip()
                for row in temporal_rows
                if row.get("credited", True) and row.get("property_id")
            }
            object_ids = []
            string_ids = []
            for property_value in property_values:
                try:
                    object_ids.append(ObjectId(property_value))
                except Exception:
                    string_ids.append(property_value)
            temporal_property_values = object_ids + string_ids

            temporal_clauses = []
            if object_ids:
                temporal_clauses.append({"_id": {"$in": object_ids}})
            if string_ids:
                temporal_clauses.append({"_id": {"$in": string_ids}})
            add_condition({"$or": temporal_clauses} if temporal_clauses else {"_id": None})
        except (TypeError, ValueError):
            # Un parámetro temporal inválido nunca debe ampliar el universo.
            add_condition({"_id": None})

    # Se calcula sin el portal elegido para que el selector muestre solo los
    # portales que realmente tienen registros en el alcance actual del usuario.
    # El selector de portal depende del alcance actual (RBAC + filtros), por
    # lo que se cachea por consulta y no como catálogo global. Así se conserva
    # exactamente qué opciones quedan disponibles para cada combinación de
    # filtros, evitando repetir el distinct en cada petición idéntica.
    portal_cache_key = repr(query)
    portal_cache = getattr(get_captacion_list, "_portal_cache", {})
    portal_cache_rec = portal_cache.get(portal_cache_key)
    portal_cache_ttl = 300
    if portal_cache_rec and (get_chile_now() - portal_cache_rec[0]).total_seconds() < portal_cache_ttl:
        available_portal_values = portal_cache_rec[1]
        if perf_context is not None:
            perf_context["portal_catalog_cache"] = "hit"
    else:
        if temporal_property_values and temporal_portal_base_query is not None:
            # El filtro temporal ya resolvió IDs concretos desde el ledger.
            # Consultar esos pocos documentos evita un distinct sobre toda la
            # cartera, que era el outlier del fast path diario/semanal.
            temporal_portal_query = dict(temporal_portal_base_query)
            temporal_portal_query.setdefault("$and", []).append({
                "_id": {"$in": temporal_property_values}
            })
            portal_documents = coll.find(temporal_portal_query, {"origen": 1})
            available_portal_values = sorted({
                str(document.get("origen")).strip()
                for document in portal_documents
                if document.get("origen") and str(document.get("origen")).strip()
            }, key=str.casefold)
        else:
            available_portal_values = sorted({
                str(origin).strip()
                for origin in coll.distinct("origen", query)
                if origin and str(origin).strip()
            }, key=str.casefold)
        portal_cache[portal_cache_key] = (get_chile_now(), available_portal_values)
        # Evita que combinaciones de filtros históricas acumulen memoria.
        if len(portal_cache) > 128:
            oldest_key = min(portal_cache, key=lambda key: portal_cache[key][0])
            portal_cache.pop(oldest_key, None)
        get_captacion_list._portal_cache = portal_cache
        if perf_context is not None:
            perf_context["portal_catalog_cache"] = "miss"
    _l_portal = _perf_time.perf_counter()
    available_portals = [
        {"value": origin, "label": format_captacion_portal_label(origin)}
        for origin in available_portal_values
    ]

    if portal_filter in available_portal_values:
        query["origen"] = portal_filter
    
    # El listado no se cachea: sus estados cambian desde la vista de detalle
    # y debe reflejar la actualización inmediatamente al volver a la tabla.
    # La cuenta y la página se obtienen juntas más abajo con un único $facet.
    total_count = None
    _l_count = _l_portal
    
    # Available ops: resultado estático por colección, cache global 300s.
    # No depende de RBAC ni filtros, así que no debe recalcularse por request.
    _global_ops_cache = getattr(get_captacion_list, '_ops_cache', None)
    if _global_ops_cache and (get_chile_now() - _global_ops_cache[0]).total_seconds() < 300:
        available_ops = _global_ops_cache[1]
        if perf_context is not None:
            perf_context["operation_catalog_cache"] = "hit"
    else:
        pipeline_ops = [
            {"$match": {"origen": {"$exists": True, "$nin": [None, ""]}, "classification.state": {"$in": list(VISIBLE_CLASSIFICATION_STATES)}}},
            {"$group": {"_id": None, "ops": {"$addToSet": "$operacion"}}}
        ]
        ops_result = list(coll.aggregate(pipeline_ops))
        raw_ops = ops_result[0]["ops"] if ops_result else []
        available_ops = []
        for o in raw_ops:
            if o and "venta" in str(o).lower():
                available_ops.append("venta")
            if o and "arr" in str(o).lower():
                available_ops.append("arriendo")
        if not available_ops:
            available_ops = ["venta", "arriendo"]
        get_captacion_list._ops_cache = (get_chile_now(), available_ops)
        if perf_context is not None:
            perf_context["operation_catalog_cache"] = "miss"
    _l_ops = _perf_time.perf_counter()
    
    skip = (page - 1) * limit
    sort_fields = {
        "comuna": "captacion_comuna_sort",
        "precio": "captacion_price_sort",
        "owner_probability": "captacion_probability_sort",
        "ultima_gestion": "captacion_management_date",
    }
    allowed_orders = {"prioridad", "recientes", "probabilidad", "antiguas", "ultima_gestion"}
    order_filter = order_filter if order_filter in allowed_orders else None
    sort_keys = [s.strip() for s in str(sort_by or "").split(",") if s.strip()]
    sort_dirs = [s.strip().lower() for s in str(sort_dir or "").split(",") if s.strip()]
    sort_specs = []
    for index, key in enumerate(sort_keys):
        if key not in {*sort_fields, "antiguedad"} or key in [s[0] for s in sort_specs]:
            continue
        direction = 1 if index < len(sort_dirs) and sort_dirs[index] == "asc" else -1
        sort_specs.append((key, direction))
        # Mantener un único criterio evita resultados ambiguos al cambiar
        # de columna desde el listado.
        break
    # Proyección mínima de la vista de listado. La anterior era de exclusión
    # y seguía trayendo clasificación completa, imágenes, metadata del scrape
    # y otros payloads que la tabla no renderiza. Se conservan los alias que
    # normalize_captacion_document() y los helpers de precio/owner usan, más
    # los campos de gestión, fechas y ordenamiento.
    projection = {
        "_id": 1,
        "listing_id": 1,
        "url": 1,
        "title": 1,
        "titulo": 1,
        "comuna": 1,
        "comuna_slug": 1,
        "operation": 1,
        "operacion": 1,
        "tipo_operacion": 1,
        "property_type": 1,
        "tipo_propiedad": 1,
        "precio_raw": 1,
        "precio": 1,
        "price": 1,
        "precio_uf": 1,
        "price_uf": 1,
        "precio_clp": 1,
        "price_clp": 1,
        "contact_phone": 1,
        "whatsapp_phone": 1,
        "telefono": 1,
        "email": 1,
        "vendedor_email": 1,
        "seller_name": 1,
        "publicador": 1,
        "origen": 1,
        "source_portal": 1,
        "score_captacion": 1,
        "probabilidad": 1,
        "first_seen": 1,
        "first_seen_at": 1,
        "created_at": 1,
        "fecha_captura": 1,
        "processed_at": 1,
        "scraped_at": 1,
        "updated_at": 1,
        "details.comuna": 1,
        "details.comuna_norm": 1,
        "details.operacion": 1,
        "details.tipo_operacion": 1,
        "details.tipo_propiedad": 1,
        "details.tipo": 1,
        "details.precio": 1,
        "details.precio_raw": 1,
        "details.price": 1,
        "details.precio_uf": 1,
        "details.price_uf": 1,
        "details.precio_clp": 1,
        "details.price_clp": 1,
        "details.dormitorios": 1,
        "details.banos": 1,
        "details.baños": 1,
        "details.dormitorios_min": 1,
        "details.banos_min": 1,
        "details.m2_total": 1,
        "details.m2_totales": 1,
        "details.m2_construidos": 1,
        "details.superficie_total": 1,
        "details.publicador": 1,
        "details.vendedor_nombre": 1,
        "details.whatsapp_phone": 1,
        "details.contact_phone": 1,
        "details.telefono": 1,
        "details.phone": 1,
        "details.email": 1,
        "details.vendedor_email": 1,
        "gestion.estado": 1,
        "gestion.ejecutivo_asignado": 1,
        "gestion.ejecutivo_id": 1,
        "gestion.intent_count": 1,
        "gestion.fecha_ultima_gestion": 1,
        "classification.state": 1,
        "classification.final_state": 1,
        "classification.owner_probability": 1,
        "classification.owner_probability_signals": 1,
    }
    preloaded_docs = None
    if order_filter or sort_specs:
        if order_filter == "prioridad":
            aggregate_sort = {
                "captacion_priority": 1,
                "captacion_sort_date": -1,
                "_id": -1,
            }
        elif order_filter == "recientes":
            aggregate_sort = {"captacion_sort_date": -1, "_id": -1}
        elif order_filter == "antiguas":
            aggregate_sort = {"captacion_sort_date": 1, "_id": -1}
        elif order_filter == "probabilidad":
            aggregate_sort = {"captacion_probability_sort": -1, "_id": -1}
        elif order_filter == "ultima_gestion":
            aggregate_sort = {"captacion_management_date": -1, "_id": -1}
        else:
            aggregate_sort = {}
            for key, direction in sort_specs:
                sort_key = {
                    "comuna": "captacion_comuna_sort",
                    "precio": "captacion_price_sort",
                    "owner_probability": "captacion_probability_sort",
                    # Antigüedad ASC = menos días = fecha más reciente.
                    "antiguedad": "captacion_sort_date",
                }[key]
                aggregate_sort[sort_key] = -direction if key == "antiguedad" else direction
            aggregate_sort["_id"] = -1
        # Los campos se materializan durante la ingesta/backfill, de modo que
        # Mongo puede usar un índice para ordenar antes de paginar.
        list_sort = aggregate_sort
    else:
        list_sort = {"updated_at": -1, "_id": -1}

    _facet_started = _perf_time.perf_counter()
    if preloaded_docs is not None:
        total_count = coll.count_documents(query)
        _raw_docs = preloaded_docs
    else:
        # Una sola ida a Mongo para el contador y los diez documentos visibles.
        facet_rows = list(coll.aggregate([
            {"$match": query},
            {"$facet": {
                "metadata": [{"$count": "total"}],
                "data": [
                    {"$sort": list_sort},
                    {"$skip": skip},
                    {"$limit": limit},
                    {"$project": projection},
                ],
            }},
        ]))
        facet_row = facet_rows[0] if facet_rows else {}
        metadata = facet_row.get("metadata") or []
        total_count = int((metadata[0] if metadata else {}).get("total") or 0)
        _raw_docs = facet_row.get("data") or []
    _facet_finished = _perf_time.perf_counter()
    _l_count = _facet_finished
    _l_cursor = _perf_time.perf_counter()
    
    items_paginated = []
    for doc in _raw_docs:
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
            "tipo_propiedad": (
                normalize_captacion_property_type(norm.get("tipo_propiedad")) or "S/I"
            ).title(),
            "precio": str(norm["precio"]).split("Ref.")[0].strip() if norm["precio"] else "S/I",
            "precio_uf": norm["precio_uf"],
            "precio_display": norm["precio_display"],
            "precio_uf_display": norm["precio_uf_display"],
            "precio_clp_display": norm["precio_clp_display"],
            "precio_raw_fallback": norm.get("precio_raw_fallback", norm.get("precio", "S/I")),
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
    
    _l_enrich = _perf_time.perf_counter()
    _l_total = (_perf_time.perf_counter() - _l_start) * 1000
    if perf_context is not None:
        perf_context.update({
            "count_ms": round((_facet_finished - _facet_started) * 1000, 1),
            "portal_ms": round((_l_portal - _l_start) * 1000, 1),
            "ops_ms": round((_l_ops - _l_portal) * 1000, 1),
            "query_ms": round((_facet_finished - _facet_started) * 1000, 1),
            "enrich_ms": round((_l_enrich - _l_cursor) * 1000, 1),
            "total_ms": round(_l_total, 1),
        })
    logger.debug(
        f"[CAPTACION_LIST_PERF] count={(_l_count - _l_start)*1000:.0f} "
        f"ops={(_l_ops - _l_count)*1000:.0f} "
        f"query_sort={(_l_cursor - _l_ops)*1000:.0f} "
        f"enrich={(_l_enrich - _l_cursor)*1000:.0f} "
        f"total={_l_total:.0f}ms items={len(items_paginated)}"
    )
    
    if return_portals:
        return items_paginated, total_count, available_ops, available_portals, available_property_types
    return items_paginated, total_count, available_ops


def warm_captacion_shared_catalogs():
    """Precalienta catálogos globales usados por la primera vista.

    Los catálogos no dependen del usuario, paginación ni ordenamiento. Se
    preparan una vez fuera del request para que el primer visitante no pague
    los ``distinct``/agregados de infraestructura del formulario.
    """
    warm_started = _perf_time.perf_counter()
    db = get_db()
    coll = get_captacion_collection(db)
    base_query = {
        "origen": {"$exists": True, "$nin": [None, ""]},
        "classification.state": {"$in": list(VISIBLE_CLASSIFICATION_STATES)},
    }
    now = get_chile_now()

    portal_cache = getattr(get_captacion_list, "_portal_cache", {})
    base_key = repr(base_query)
    portal_values = sorted({
        str(origin).strip()
        for origin in coll.distinct("origen", base_query)
        if origin and str(origin).strip()
    }, key=str.casefold)
    portal_cache[base_key] = (now, portal_values)
    if len(portal_cache) > 128:
        oldest_key = min(portal_cache, key=lambda key: portal_cache[key][0])
        portal_cache.pop(oldest_key, None)
    get_captacion_list._portal_cache = portal_cache

    pipeline_ops = [
        {"$match": base_query},
        {"$group": {"_id": None, "ops": {"$addToSet": "$operacion"}}},
    ]
    ops_result = list(coll.aggregate(pipeline_ops))
    raw_ops = ops_result[0].get("ops", []) if ops_result else []
    available_ops = []
    for operation in raw_ops:
        if operation and "venta" in str(operation).lower():
            available_ops.append("venta")
        if operation and "arr" in str(operation).lower():
            available_ops.append("arriendo")
    if not available_ops:
        available_ops = ["venta", "arriendo"]
    get_captacion_list._ops_cache = (now, available_ops)
    elapsed_ms = (_perf_time.perf_counter() - warm_started) * 1000
    return {
        "portal_count": len(portal_values),
        "operation_count": len(available_ops),
        "elapsed_ms": round(elapsed_ms, 1),
    }

def get_captacion_raw(obj_id):
    """Read the source document without running detail scoring or market queries."""
    from bson.errors import InvalidId

    db = get_db()
    coll = get_captacion_collection(db)
    try:
        query_id = ObjectId(obj_id)
    except (InvalidId, TypeError):
        query_id = str(obj_id)

    doc = coll.find_one({"_id": query_id})
    if not doc and query_id != str(obj_id):
        doc = coll.find_one({"_id": str(obj_id)})
    return doc


def get_captacion_detail(obj_id):
    _detail_cache_key = f"detail_full_{obj_id}"
    _cached = _l1_get(_detail_cache_key)
    if _cached is not None:
        return _cached

    doc = get_captacion_raw(obj_id)
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
    
    # Nombre: se deja vacío cuando no existe para que la plantilla muestre
    # "Propietario" como placeholder y no como un dato que pueda guardarse.
    vendedor_nombre = str(norm["vendedor_nombre"] or "").strip()
    if vendedor_nombre.casefold() in {"particular", "n/a", "no disponible", "propietario"}:
        vendedor_nombre = ""
    
    vendedor_email = norm.get("vendedor_email") or details.get("email") or details.get("vendedor_email") or ""
    notas_contacto = doc.get("notas_contacto") or ""
    
    pipeline_stages = [
        "Por contactar", "En gestión", "Contacto exitoso", "Sin respuesta", "Teléfono inválido",
        "Corredor", "Propiedad no disponible", "Publicación expirada", "No interesado",
        "Reunión agendada", "Captado", "Descartado", "Duplicado"
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
    
    # Historial: merge structured status_history + free-text notas
    historial = []
    status_log = gestion.get("status_history", [])
    if isinstance(status_log, list):
        for entry in status_log:
            ts = entry.get("timestamp")
            historial.append({
                "_sort_ts": ts,
                "fecha": format_relative_time(ts),
                "nota": f"Cambio de estado: {entry.get('from_state', '?')} → {entry.get('to_state', '?')}",
                "usuario": entry.get("user", "Sistema"),
                "canal": "estado",
                "is_status_change": True,
            })
    notas_raw = gestion.get("notas", [])
    if isinstance(notas_raw, list):
        for n in notas_raw:
            ts = n.get("timestamp")
            historial.append({
                "_sort_ts": ts,
                "fecha": format_relative_time(ts),
                "nota": n.get("content", ""),
                "usuario": n.get("usuario", "Sistema"),
                "canal": n.get("canal", "Desconocido"),
                "is_status_change": False,
            })
    historial.sort(
        key=lambda item: (item.get("_sort_ts") or datetime(2000, 1, 1)),
        reverse=True,
    )
    for item in historial:
        item.pop("_sort_ts", None)
    historial = historial[:50]
    
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


def get_captacion_update_state(obj_id, operation_id=None):
    """Read the persisted update marker without using the detail cache."""
    db = get_db()
    coll = get_captacion_collection(db)
    try:
        query_id = ObjectId(obj_id)
    except Exception:
        query_id = str(obj_id)

    doc = coll.find_one({"_id": query_id}, {"_id": 1, "gestion": 1})
    if not doc:
        doc = coll.find_one({"_id": str(obj_id)}, {"_id": 1, "gestion": 1})
    if not doc:
        return None

    gestion = dict(doc.get("gestion") or {})
    stored_operation_id = str(gestion.get("last_update_operation_id") or "").strip() or None
    requested_operation_id = str(operation_id or "").strip() or None
    core_persisted = bool(
        requested_operation_id
        and stored_operation_id
        and requested_operation_id == stored_operation_id
    )
    return {
        "id": str(doc.get("_id") or obj_id),
        "gestion": gestion,
        "status": gestion.get("estado_captacion") or gestion.get("estado") or "NUEVO",
        "operation_id": stored_operation_id,
        "core_persisted": core_persisted,
        "persisted": core_persisted and bool(gestion.get("last_update_completed_at")),
    }

def run_captacion_secondary_effects(obj_id, old_status, status, user_name, notes):
    """Run non-blocking score and audit updates after the durable write."""
    db = get_db()
    try:
        from chatbot.metrics import update_captacion_metrics
        update_captacion_metrics(db, obj_id)
    except Exception:
        logger.exception("Error updating captacion metrics for %s", obj_id)

    try:
        log_event(str(obj_id), EventType.STAGE_CHANGE.value, user_name, {
            "old_stage": old_status,
            "new_stage": status,
            "notes": notes,
            "source": "captacion",
        })
    except Exception:
        logger.exception("Error logging captacion status change event for %s", obj_id)


def update_captacion_status(
    obj_id, status, notes=None, channel=None, outcome=None, user_name="Sistema",
    next_followup=None, user_doc=None, followup_token=None, operation_id=None,
    current_doc=None, defer_secondary_effects=False,
):
    db = get_db()
    operation_id = str(operation_id or "").strip() or None
    
    now = get_chile_now() # Store as Date object, not string
    
    try:
        query_id = ObjectId(obj_id)
    except Exception:
        query_id = str(obj_id)
        
    if current_doc is None:
        current_doc = get_captacion_collection(db).find_one({"_id": query_id})
        if not current_doc:
            current_doc = get_captacion_collection(db).find_one({"_id": str(obj_id)})
            if not current_doc:
                return False

        
    current_gestion = current_doc.get("gestion", {}) or {}
    old_status = current_gestion.get("estado_captacion") or current_gestion.get("estado") or "NUEVO"
    operation_already_applied = bool(
        operation_id
        and str(current_gestion.get("last_update_operation_id") or "").strip() == operation_id
    )
    if operation_already_applied and current_gestion.get("last_update_completed_at"):
        return True
    decision_previous_status = (
        current_gestion.get("last_update_previous_status")
        if operation_already_applied
        else old_status
    ) or old_status
    manual_decision = evaluate_manual_decision(
        status=status,
        previous_status=decision_previous_status,
        notes=notes,
        outcome=outcome,
        is_automatic=not bool(user_doc),
    )
    
    # 1. Preparar campos de actualización de alto nivel
    update_fields = {
        "gestion.estado": status,
        "gestion.estado_captacion": status,
        "gestion.fecha_ultima_gestion": now,
        "captacion_priority": 0 if status in AVAILABLE_STATES else 1,
        "captacion_management_date": now,
    }
    
    followup_error = None
    if next_followup and not operation_already_applied:
        update_fields["gestion.next_followup"] = next_followup
        # Persist the task now; issue the signed URL only when the worker
        # reaches execute_at and is ready to send WhatsApp.
        try:
            from chatbot.followup_tracking import (
                TRACKING_VERSION as FOLLOWUP_TRACKING_VERSION,
                ensure_followup_indexes,
                record_followup_event,
            )

            creator_id = str((user_doc or {}).get("_id") or "").strip()
            if not creator_id:
                logger.error(
                    "[FOLLOWUP] token_issue_failed obj_id=%s reason=creator_missing",
                    obj_id,
                )
                raise ValueError("followup_creator_missing")

            # Handle formats: "2023-12-31 15:00" or ISO.
            date_str = next_followup.replace("T", " ").replace("Z", "")
            try:
                execute_at = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
            except ValueError:
                execute_at = datetime.fromisoformat(date_str)
            if execute_at.tzinfo is None:
                execute_at = CHILE_TZ.localize(execute_at)

            obj_id_str = str(obj_id)
            audit_note = str(notes).strip() if notes and str(notes).strip() else None
            if any("?" in str(value or "") for value in (audit_note, old_status)):
                logger.error("Rejected captacion reminder with degraded Unicode input")
                return False
            idempotency_key = (
                f"captacion_reminder:{obj_id_str}:{execute_at.astimezone(timezone.utc).isoformat()}"
                f":{creator_id}"
            )
            ensure_followup_indexes(db)
            existing_task = db["crm_tasks"].find_one({
                "idempotency_key": idempotency_key,
                "followup_tracking_version": FOLLOWUP_TRACKING_VERSION,
            })
            if not existing_task:
                # Historical v1 tasks with the same key must not block a new
                # v2 task. Only pending tasks are superseded.
                db["crm_tasks"].update_many(
                    {"lead_type": "captacion", "obj_id": obj_id_str, "status": "pending"},
                    {"$set": {"status": "completed", "resolved_at": now, "resolution": "superseded"}}
                )
                inserted_new = True
                task = {
                    "task_id": str(uuid.uuid4()),
                    "message_domain": "captacion_reminder",
                    "message_type": "scheduled_reminder",
                    "recipient_role": "executive",
                    "created_by_user_id": creator_id,
                    "target_user_id": creator_id,
                    "recipient_user_id": creator_id,
                    "recipient_name": (user_doc or {}).get("nombre") or user_name,
                    "state_at_creation": old_status,
                    "audit_note": audit_note,
                    "idempotency_key": idempotency_key,
                    "lead_type": "captacion",
                    "followup_tracking_version": FOLLOWUP_TRACKING_VERSION,
                    "followup_token_version": 2,
                    "attribution_status": "attributed",
                    "phone": "+56900000000",
                    "obj_id": obj_id_str,
                    "target_name": user_name,
                    "type": "REMINDER_CAPTACION",
                    "status": "pending",
                    "scheduled_at": execute_at,
                    "execute_at": execute_at,
                    "attempts": 0,
                    "created_at": now,
                    "note": (
                        str(notes).strip() if notes and str(notes).strip()
                        else f"Contactar captación: {current_doc.get('title') or current_doc.get('details', {}).get('titulo', 'Sin título')} (Score: {current_doc.get('score_captacion', 0)})"
                    ),
                    "agent": user_name,
                }
                try:
                    db["crm_tasks"].insert_one(task)
                except DuplicateKeyError:
                    # A concurrent retry may win the v2 partial unique index.
                    inserted_new = False
                    task = db["crm_tasks"].find_one({
                        "idempotency_key": idempotency_key,
                        "followup_tracking_version": FOLLOWUP_TRACKING_VERSION,
                    })
                    if not task:
                        raise
                if inserted_new:
                    try:
                        record_followup_event(
                            db,
                            task=task,
                            event_type="reminder_scheduled",
                            occurred_at=now,
                            source="captacion_crm",
                            actor_user_id=creator_id,
                        )
                    except ValueError:
                        pass
        except Exception as e:
            logger.error(f"Error scheduling captacion task: {e}")
            followup_error = e

    if followup_error is not None:
        raise RuntimeError("No se pudo guardar el seguimiento programado") from followup_error

    if operation_id and not operation_already_applied:
        update_fields.update({
            "gestion.last_update_operation_id": operation_id,
            "gestion.last_update_operation_at": now,
            "gestion.last_update_previous_status": old_status,
            "gestion.last_update_completed_at": None,
        })
    
    # 2. Incrementar contador de intentos si es una acción de contacto
    inc_fields = {}
    if outcome or (notes and "Intento" in notes):
        inc_fields["gestion.intent_count"] = 1
        update_fields["gestion.last_contact"] = now
        if channel: update_fields["gestion.last_channel"] = channel

    # 3. Empujar nota estructurada solo si hay contenido
    push_fields = {}
    if notes:
        push_fields["gestion.notas"] = {
            "content": notes,
            "timestamp": now,
            "usuario": user_name,
            "canal": channel or "Manual",
            "resultado": outcome
        }
    
    # Si cambió el estado, registrar el cambio
    if old_status != status and not operation_already_applied:
        push_fields["gestion.status_history"] = {
            "timestamp": now,
            "user": user_name,
            "from_state": old_status,
            "to_state": status
        }
    
    update_params = {"$set": update_fields}
    if inc_fields and not operation_already_applied: update_params["$inc"] = inc_fields
    if push_fields and not operation_already_applied: update_params["$push"] = push_fields

    if not operation_already_applied:
        write_result = get_captacion_collection(db).update_one(
            {"_id": current_doc["_id"], **({"gestion.last_update_operation_id": {"$ne": operation_id}} if operation_id else {})},
            update_params,
        )
        matched_count = getattr(write_result, "matched_count", None)
        if operation_id and matched_count == 0:
            latest = get_captacion_collection(db).find_one(
                {"_id": current_doc["_id"]}, {"gestion.last_update_operation_id": 1}
            ) or {}
            latest_operation_id = str(
                (latest.get("gestion") or {}).get("last_update_operation_id") or ""
            ).strip()
            if latest_operation_id != operation_id:
                raise RuntimeError("La actualización no pudo confirmarse en MongoDB")
            operation_already_applied = True
    if old_status != status and not operation_already_applied:
        # La revisión vive en Mongo y permite invalidar snapshots de otros
        # workers, además de la limpieza local que ya realiza este módulo.
        bump_captacion_kpi_revision(db)
    if user_doc and manual_decision.get("eligible"):
        manual_result = record_manual_management_decision(
            db,
            property_doc=current_doc,
            actor_user=user_doc,
            status=status,
            previous_status=decision_previous_status,
            notes=notes,
            outcome=outcome,
            now=now,
            operation_id=operation_id,
        )
        if followup_token and manual_result.get("event_id"):
            try:
                from chatbot.followup_tracking import record_followup_management
                record_followup_management(
                    db,
                    token=followup_token,
                    entity_id=obj_id,
                    executive_id=str(user_doc.get("_id") or ""),
                    management_event_id=manual_result["event_id"],
                    occurred_at=now,
                    followup_cycle_id=manual_result.get("assignment_cycle_id"),
                )
            except ValueError:
                # A bad or legacy token must not invalidate a valid status
                # change; the management simply remains unattributed.
                pass
    _invalidate_detail_cache(obj_id)
    _invalidate_captacion_list_cache()
    try:
        from captacion_goals import clear_captacion_management_rows_cache
        clear_captacion_management_rows_cache()
    except Exception:
        logger.debug("No se pudo invalidar la caché de ledger de captación", exc_info=True)
    
    if not defer_secondary_effects:
        run_captacion_secondary_effects(obj_id, old_status, status, user_name, notes)

    if operation_id:
        get_captacion_collection(db).update_one(
            {"_id": current_doc["_id"], "gestion.last_update_operation_id": operation_id},
            {"$set": {"gestion.last_update_completed_at": now}},
        )

    return True

def update_contact_info(obj_id, nombre=None, telefono=None, email=None, notas=None, user_name="Sistema", user_id=None, return_details=False):
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
        
    details = current_doc.get("details", {}) or {}
    
    update_fields = {}
    audit_changes = []
    now = get_chile_now()
    contact_identity = None
    known_broker_match = None
    
    nombre_limpio = str(nombre or "").strip()
    nombre_actual = str(details.get("publicador") or "").strip()
    if nombre_limpio and nombre_limpio.casefold() != "propietario" and nombre_limpio != nombre_actual:
        update_fields["details.publicador"] = nombre_limpio
        audit_changes.append({
            "timestamp": now,
            "user": user_name,
            "field": "nombre",
            "old_value": details.get("publicador"),
            "new_value": nombre_limpio
        })
        
    if telefono:
        clean_phone = normalize_phone(telefono)
        if not clean_phone:
            logger.warning("[CAPTACION_CONTACT] invalid_phone_rejected property_id=%s", obj_id)
            return False
        previous_phone = normalize_phone(
            current_doc.get("telefono_normalizado")
            or details.get("whatsapp_phone")
            or details.get("telefono")
        )
        if clean_phone != previous_phone:
            phone_actor = str(user_id or user_name or "Sistema")
            old_version = current_doc.get("phone_version") or 0
            try:
                next_version = int(old_version) + 1
            except (TypeError, ValueError):
                next_version = 1
            update_fields["details.whatsapp_phone"] = clean_phone
            updated_doc = dict(current_doc)
            updated_details = dict(details)
            updated_details["whatsapp_phone"] = clean_phone
            updated_doc["details"] = updated_details
            update_fields["telefono_normalizado"] = clean_phone
            update_fields["phone_normalized"] = clean_phone
            update_fields["phone_source"] = "EXECUTIVE"
            update_fields["phone_added_at"] = now
            update_fields["phone_added_by"] = phone_actor
            update_fields["phone_original_value"] = str(telefono)
            update_fields["phone_version"] = next_version
            audit_changes.append({
                "timestamp": now,
                "user": user_name,
                "field": "telefono",
                "old_value": details.get("whatsapp_phone"),
                "new_value": clean_phone,
                "phone_normalized": clean_phone,
                "phone_source": "EXECUTIVE",
                "phone_added_by": phone_actor,
                "phone_version": next_version,
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
        _invalidate_captacion_list_cache()
        
        # LOG EVENT CENTRAL: Registro de Teléfono
        if telefono:
            try:
                log_event(str(obj_id), EventType.REGISTER_PHONE.value, user_name, {
                    "phone_registered": telefono,
                    "phone_normalized": normalize_phone(telefono),
                    "phone_source": "EXECUTIVE",
                    "source": "captacion"
                })
            except Exception as e:
                logger.error(f"Error logging register phone event: {e}")

    if telefono and phone_learning_enabled():
        lookup_doc = dict(current_doc)
        lookup_details = dict(details)
        lookup_doc["details"] = lookup_details
        normalized_phone = normalize_phone(telefono)
        lookup_details["whatsapp_phone"] = normalized_phone
        for key in ("telefono_normalizado", "phone_normalized", "whatsapp_phone", "contact_phone", "telefono"):
            lookup_doc[key] = normalized_phone
        contact_identity = get_contact_identity_evidence(db, lookup_doc)
        if contact_identity and str(contact_identity.get("status") or "").upper() == "CORREDOR_CONFIRMED":
            from captacion_management import record_known_broker_auto_match
            known_broker_match = record_known_broker_auto_match(
                db,
                property_doc=lookup_doc,
                identity=contact_identity,
                actor_user_id=user_id,
                actor_name=user_name,
                now=now,
            )

    if return_details:
        return {
            "ok": True,
            "known_broker_auto_match": bool(known_broker_match and known_broker_match.get("matched")),
            "message": (known_broker_match or {}).get("message"),
            "identity_status": (contact_identity or {}).get("status"),
            "identity_id": str((contact_identity or {}).get("_id") or (contact_identity or {}).get("identity_key") or ""),
            "known_broker_event_id": (known_broker_match or {}).get("event_id"),
        }

    return True

def log_captacion_activity(
    obj_id,
    user_name,
    action,
    channel,
    message,
    phone,
    result,
    template_used=None,
    user_doc=None,
):
    db = get_db()
    now = get_chile_now()

    try:
        query_id = ObjectId(obj_id)
    except Exception:
        query_id = str(obj_id)
    property_doc = get_captacion_collection(db).find_one({"_id": query_id})
    if not property_doc and query_id != str(obj_id):
        property_doc = get_captacion_collection(db).find_one({"_id": str(obj_id)})
    if not property_doc:
        raise LookupError("Propiedad de captación no encontrada")
    if user_doc and not can_manage_captacion(user_doc, property_doc):
        raise PermissionError("No tienes permiso para gestionar esta captación")

    attempt = start_management_attempt(
        db,
        property_doc=property_doc,
        actor_user=user_doc or {},
        action=action,
        channel=channel,
        message=message,
        phone=phone,
        template_used=template_used,
        now=now,
    )

    activity_entry = {
        "timestamp": now,
        "user": user_name,
        "user_id": str((user_doc or {}).get("_id") or ""),
        "action": "action_initiated",
        "original_action": action,
        "channel": channel,
        "message": message,
        "phone": phone,
        "result": "pending_confirmation",
        "attempt_id": attempt["attempt_id"],
    }
    if template_used:
        activity_entry["template_used"] = template_used

    get_captacion_collection(db).update_one(
        {"_id": property_doc["_id"]},
        {"$push": {"gestion.actividades": activity_entry}}
    )
    _invalidate_detail_cache(obj_id)
    _invalidate_captacion_list_cache()

    # Auditoría de intención: abrir una app externa nunca acredita la meta.
    try:
        log_event(str(property_doc["_id"]), "captacion_action_initiated", user_name, {
            "action": action,
            "channel": channel,
            "attempt_id": attempt["attempt_id"],
            "message_summary": message[:100] if message else "",
            "phone_target": phone,
            "source": "captacion"
        })
    except Exception as e:
        logger.error(f"Error logging captacion activity event: {e}")

    return {"ok": True, "credited": False, **attempt}


def confirm_captacion_activity(attempt_id, user_doc, result, notes=None, followup_token=None, commercial_result=None):
    if not user_doc or not user_doc.get("_id"):
        raise PermissionError("Usuario sin identidad válida")
    confirmed = confirm_management_attempt(
        get_db(),
        attempt_id=attempt_id,
        actor_user=user_doc,
        result=result,
        notes=notes,
        commercial_result=commercial_result,
    )
    if followup_token and confirmed.get("event_id"):
        try:
            from chatbot.followup_tracking import record_followup_management
            record_followup_management(
                get_db(),
                token=followup_token,
                entity_id=confirmed.get("property_id") or "",
                executive_id=str(user_doc.get("_id") or ""),
                management_event_id=confirmed["event_id"],
                occurred_at=confirmed.get("occurred_at"),
                followup_cycle_id=confirmed.get("assignment_cycle_id"),
            )
        except ValueError:
            pass
    return confirmed

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

        # 6. Índice para orden por defecto (updated_at DESC) con filtros base
        try:
            coll.create_index([
                ("origen", 1),
                ("classification.state", 1),
                ("updated_at", -1),
                ("_id", -1),
            ], name="idx_captacion_default_sort")
        except Exception:
            pass

        # Índices para los campos derivados del listado. Se crean después del
        # backfill en despliegue; mientras faltan campos, get_captacion_list
        # conserva el mismo orden materializado para los documentos existentes.
        materialized_indexes = (
            ("idx_captacion_priority_materialized", [
                ("origen", 1), ("classification.state", 1),
                ("captacion_priority", 1), ("captacion_sort_date", -1), ("_id", -1),
            ]),
            ("idx_captacion_date_materialized", [
                ("origen", 1), ("classification.state", 1),
                ("captacion_sort_date", -1), ("_id", -1),
            ]),
            ("idx_captacion_price_materialized", [
                ("origen", 1), ("classification.state", 1),
                ("captacion_price_sort", 1), ("_id", -1),
            ]),
            ("idx_captacion_probability_materialized", [
                ("origen", 1), ("classification.state", 1),
                ("captacion_probability_sort", -1), ("_id", -1),
            ]),
            ("idx_captacion_comuna_materialized", [
                ("origen", 1), ("classification.state", 1),
                ("captacion_comuna_sort", 1), ("_id", -1),
            ]),
            ("idx_captacion_phone_normalized", [
                ("telefono_normalizado", 1),
            ]),
            ("idx_captacion_price_uf_range", [
                ("precio_uf_normalizado", 1), ("_id", -1),
            ]),
            ("idx_captacion_price_clp_range", [
                ("precio_clp_normalizado", 1), ("_id", -1),
            ]),
        )
        for index_name, index_spec in materialized_indexes:
            try:
                coll.create_index(index_spec, name=index_name)
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

    # Solo candidatos: asignados, sin gestión reciente (estado NUEVO/GESTION).
    # La evidencia real de gestión (notas/actividades/eventos) se valida en
    # Python con has_management_evidence para no liberar contactos trabajados.
    query = {
        "origen": "toctoc",
        "gestion.ejecutivo_id": {"$exists": True, "$ne": None},
        "gestion.estado": {"$in": ["NUEVO", "GESTION"]},
        "$or": [
            {"gestion.fecha_ultima_gestion": {"$exists": False}, "gestion.fecha_asignacion": {"$lt": umbral}},
            {"gestion.fecha_ultima_gestion": {"$lt": umbral}},
        ]
    }

    candidates = list(coll.find(query, {"_id": 1}))
    if not candidates:
        logger.info("[SLA] Sin captaciones inactivas para liberar.")
        return 0

    # Filtrar por evidencia real de gestión (notas, actividades, eventos,
    # fecha_ultima_gestion). Un contacto con gestión previa nunca se libera.
    try:
        from redistribute_captacion import has_management_evidence
        events_coll = db["captacion_management_events"]
    except Exception:
        events_coll = None

    to_release = []
    for c in candidates:
        prop = coll.find_one({"_id": c["_id"]})
        if prop is None:
            continue
        has_ev, _ = has_management_evidence(prop, events_coll)
        if not has_ev:
            to_release.append(c["_id"])
    if not to_release:
        logger.info("[SLA] Sin captaciones inactivas para liberar (todas con gestión).")
        return 0

    result = coll.update_many(
        {"_id": {"$in": to_release}},
        {"$set": {
            "gestion.ejecutivo_id": None,
            "gestion.ejecutivo_asignado": None,
            "gestion.ejecutivo_nombre": None,
            "gestion.ejecutivo_email": None,
            "gestion.assignment_cycle_id": None,
            "gestion.first_valid_action_at": None,
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


BROKER_GUARD_TERMS = (
    "re max", "re/max", "remax", "fuenzalida", "procasa", "houm", "assetplan",
    "portal inmobiliario", "chilepropiedades", "goplaceit", "easyprop",
    "engel volkers", "coldwell banker", "urbalia", "enlace inmobiliario",
    "capitalizarme", "toctoc",
    "inmobiliaria", "corredor", "corredora", "corretaje", "asesor inmobiliario",
    "asesora inmobiliaria", "agente inmobiliario", "broker inmobiliario",
    "gestion inmobiliaria", "servicios inmobiliarios", "consultora inmobiliaria",
    "bienes raices", "bienes raices", "real estate", "broker", "propiedades",
    "constructora", "limitada", "sociedad",
)

BROKER_GUARD_FIELDS = (
    "company_name", "broker_brand", "publicador_visible",
    "contact_logo_alt", "contact_badges_text",
)

DISTRIBUTION_STATES = ("DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO")
DISTRIBUTION_TERMINAL_STATES = {
    "Captado", "CAPTADO", "Descartado", "DESCARTADO", "Corredor",
    "Telefono invalido", "Teléfono inválido", "Propiedad no disponible",
    "Publicacion expirada", "Publicación expirada", "No interesado",
}
_DISTRIBUTION_RUN_LOCK = threading.Lock()


def _has_broker_identity(doc: dict) -> bool:
    """Defensa en profundidad: aunque la clasificación fallara, nunca asignar
    al equipo una captación cuyo publicador tiene identidad de corredor."""
    from captacion_assignment_eligibility import assignment_eligibility
    ok, reasons = assignment_eligibility(doc)
    if not ok and "commercial_identity_or_profile" in reasons:
        return True
    raw = " ".join(str(doc.get(f) or "") for f in BROKER_GUARD_FIELDS)
    norm = re.sub(r"[^a-z0-9 ]+", " ", raw.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    return any(term in norm for term in BROKER_GUARD_TERMS)


def _distribution_unassigned_clause() -> dict:
    """Require the canonical assignment id to be empty for idempotency.

    Legacy documents may retain an old display name while their canonical
    executive id is null; that name must not make them disappear from the
    normal pool. The atomic id guard is the source of truth for concurrency.
    """
    return {
        "$or": [
            {"gestion.ejecutivo_id": {"$exists": False}},
            {"gestion.ejecutivo_id": None},
            {"gestion.ejecutivo_id": ""},
        ]
    }


def _distribution_sort_key(document: dict) -> tuple:
    captured_at = get_captacion_capture_datetime(document)
    captured_epoch = captured_at.timestamp() if captured_at else 0.0
    return (
        assignment_classification_priority(document),
        -captured_epoch,
        str(document.get("_id") or document.get("listing_id") or document.get("url") or ""),
    )


def _distribution_open_workloads(db, agent_ids: list[str]) -> dict[str, int]:
    if not agent_ids:
        return {}
    rows = db[Config.CAPTACION_COLLECTION_NAME].aggregate([
        {"$match": {
            "gestion.ejecutivo_id": {"$in": agent_ids},
            "gestion.estado": {"$nin": list(DISTRIBUTION_TERMINAL_STATES)},
        }},
        {"$group": {"_id": "$gestion.ejecutivo_id", "count": {"$sum": 1}}},
    ])
    return {str(row.get("_id")): int(row.get("count") or 0) for row in rows}


def _build_bounded_distribution_plan(
    properties: list[dict],
    agents: list[dict],
    open_workload: dict[str, int],
    *,
    batch_size: int,
    max_per_agent: int,
    max_open: int,
) -> tuple[list[tuple[dict, str]], dict[str, int]]:
    """Select a bounded, priority-ordered plan without mutating Mongo."""
    agents_by_id = {agent["id"]: agent for agent in agents}
    commune_to_agents: dict[str, list[str]] = {}
    for agent in agents:
        for commune in agent.get("comunas_interes_norm") or []:
            commune_to_agents.setdefault(commune, []).append(agent["id"])
    run_load = {agent_id: 0 for agent_id in agents_by_id}
    stats = {"no_coverage": 0, "capacity": 0, "selected": 0}
    plan: list[tuple[dict, str]] = []

    for prop in sorted(properties, key=_distribution_sort_key):
        if len(plan) >= max(0, int(batch_size)):
            break
        slug = prop.get("comuna_slug") or normalize_commune_canonical(prop.get("comuna") or "")
        candidate_ids = commune_to_agents.get(slug, [])
        if not candidate_ids:
            stats["no_coverage"] += 1
            continue
        available_ids = [
            agent_id for agent_id in candidate_ids
            if open_workload.get(agent_id, 0) + run_load.get(agent_id, 0) < max_open
            and run_load.get(agent_id, 0) < max_per_agent
        ]
        if not available_ids:
            stats["capacity"] += 1
            continue
        best_id = min(
            available_ids,
            key=lambda agent_id: (
                open_workload.get(agent_id, 0) + run_load.get(agent_id, 0),
                agents_by_id[agent_id]["name"].casefold(),
            ),
        )
        plan.append((prop, best_id))
        run_load[best_id] += 1
        stats["selected"] += 1
    return plan, stats


def _record_distribution_metrics(db, metrics: dict) -> None:
    try:
        db[Config.CAPTACION_DISTRIBUTION_METRICS_COLLECTION].insert_one(metrics)
    except Exception:
        logger.exception("[DISTRIBUCION] No se pudo registrar métrica de corrida")


def _atomic_assign_distribution_candidate(db, coll, events_coll, document, agent, now):
    """Re-evaluate and assign one candidate with an atomic unassigned guard."""
    from bson import ObjectId
    from captacion_assignment_eligibility import calculate_assignment_eligibility
    from redistribute_captacion import has_management_evidence

    fresh = coll.find_one({"_id": document["_id"]})
    if not fresh:
        return "stale"
    has_ev, _ = has_management_evidence(fresh, events_coll)
    if has_ev:
        return "managed"
    identity = get_contact_identity_evidence(db, fresh) if phone_learning_global_lookup_enabled() else None
    decision = calculate_assignment_eligibility(fresh, contact_identity=identity)
    if not decision["assignment_ready"]:
        if "contact_identity_broker_confirmed" in decision["assignment_block_reasons"]:
            return "phone"
        return "quality"
    if _has_broker_identity(fresh):
        return "quality"

    slug = fresh.get("comuna_slug") or normalize_commune_canonical(fresh.get("comuna") or "")
    if not slug or slug not in set(agent.get("comunas_interes_norm") or []):
        return "no_coverage"
    oid = fresh["_id"] if isinstance(fresh["_id"], ObjectId) else ObjectId(str(fresh["_id"]))
    cycle = new_assignment_cycle(
        property_id=fresh["_id"], user_id=agent["id"], assigned_at=now, reason="weighted_commune_background"
    )
    result = coll.update_one(
        {
            "_id": oid,
            "origen": "toctoc",
            "classification.state": {"$in": list(DISTRIBUTION_STATES)},
            "gestion.semantic_review_hold": {"$ne": True},
            **_distribution_unassigned_clause(),
        },
        {"$set": {
            "gestion.ejecutivo_id": agent["id"],
            "gestion.ejecutivo_asignado": agent["name"],
            "gestion.fecha_asignacion": now,
            "gestion.assignment_cycle_id": cycle["assignment_cycle_id"],
            "gestion.first_valid_action_at": None,
            "gestion.asignacion_version": "v2_bounded_weighted_commune",
            "gestion.estado": "NUEVO",
        }},
    )
    return "assigned" if result.modified_count else "already_assigned"


def distribute_sourced_leads():
    """Distribute one bounded, balanced batch from the normal Toctoc pool."""
    started_at = datetime.now(timezone.utc)
    run_id = str(uuid.uuid4())
    metrics = {
        "_id": run_id,
        "run_id": run_id,
        "started_at": started_at,
        "source": "post_scrape_or_manual_distribution",
        "portal_scope": ["toctoc"],
        "batch_requested": int(Config.CAPTACION_DISTRIBUTION_BATCH_SIZE),
        "batch_selected": 0,
        "evaluated": 0,
        "eligible_found": 0,
        "assigned": 0,
        "assigned_by_classification": {state: 0 for state in DISTRIBUTION_STATES},
        "assigned_by_portal": {"toctoc": 0},
        "assigned_by_executive": {},
        "active_agents": 0,
        "open_workload_before": {},
        "max_open_per_executive": int(Config.CAPTACION_MAX_OPEN_ASSIGNMENTS_PER_EXECUTIVE),
        "max_per_executive_per_run": int(Config.CAPTACION_DISTRIBUTION_MAX_PER_EXECUTIVE),
        "skipped_by_phone": 0,
        "skipped_by_quality": 0,
        "skipped_by_capacity": 0,
        "skipped_already_assigned": 0,
        "skipped_managed": 0,
        "skipped_no_coverage": 0,
        "errors": 0,
    }
    if not _DISTRIBUTION_RUN_LOCK.acquire(blocking=False):
        metrics["skipped_concurrent_run"] = 1
        metrics["finished_at"] = datetime.now(timezone.utc)
        _record_distribution_metrics(get_db(), metrics)
        return 0

    try:
        db = get_db()
        coll = get_captacion_collection(db)
        events_coll = db["captacion_management_events"]
        from redistribute_captacion import has_management_evidence
        agents_raw = list(db["usuarios"].find({
            "is_active": True,
            "rol": "agente",
            "comunas_interes_norm": {"$exists": True, "$ne": []},
        }))
        agents = []
        for raw in agents_raw:
            agents.append({
                "id": str(raw["_id"]),
                "name": raw.get("nombre") or str(raw["_id"]),
                "comunas_interes_norm": list(raw.get("comunas_interes_norm") or []),
            })
        if not agents:
            logger.info("[DISTRIBUCION] 0 agentes elegibles.")
            return 0

        agent_by_id = {agent["id"]: agent for agent in agents}
        open_workload = _distribution_open_workloads(db, list(agent_by_id))
        metrics["active_agents"] = len(agents)
        metrics["open_workload_before"] = dict(open_workload)
        max_open = int(Config.CAPTACION_MAX_OPEN_ASSIGNMENTS_PER_EXECUTIVE)
        max_per_agent = int(Config.CAPTACION_DISTRIBUTION_MAX_PER_EXECUTIVE)
        batch_size = int(Config.CAPTACION_DISTRIBUTION_BATCH_SIZE)

        eligible_query = {
            "origen": "toctoc",
            "gestion.semantic_review_hold": {"$ne": True},
            "classification.state": {"$in": list(DISTRIBUTION_STATES)},
            **_distribution_unassigned_clause(),
        }
        raw_props = list(coll.find(eligible_query))
        metrics["evaluated"] = len(raw_props)
        props = []
        for prop in raw_props:
            identity = get_contact_identity_evidence(db, prop) if phone_learning_global_lookup_enabled() else None
            from captacion_assignment_eligibility import calculate_assignment_eligibility
            decision = calculate_assignment_eligibility(prop, contact_identity=identity)
            if not decision["assignment_ready"]:
                if "contact_identity_broker_confirmed" in decision["assignment_block_reasons"]:
                    metrics["skipped_by_phone"] += 1
                else:
                    metrics["skipped_by_quality"] += 1
                continue
            has_ev, _ = has_management_evidence(prop, events_coll)
            if has_ev:
                metrics["skipped_managed"] += 1
                continue
            if _has_broker_identity(prop):
                metrics["skipped_by_quality"] += 1
                continue
            props.append(prop)
        metrics["eligible_found"] = len(props)
        plan, plan_stats = _build_bounded_distribution_plan(
            props,
            agents,
            open_workload,
            batch_size=batch_size,
            max_per_agent=max_per_agent,
            max_open=max_open,
        )
        metrics["batch_selected"] = len(plan)
        metrics["skipped_no_coverage"] = plan_stats["no_coverage"]
        metrics["skipped_by_capacity"] = plan_stats["capacity"]
        now = datetime.now(timezone.utc)

        for prop, best_id in plan:
            try:
                status = _atomic_assign_distribution_candidate(
                    db, coll, events_coll, prop, agent_by_id[best_id], now
                )
            except Exception:
                metrics["errors"] += 1
                logger.exception("[DISTRIBUCION] Error asignando property_id=%s", prop.get("_id"))
                continue
            if status == "assigned":
                state = str((prop.get("classification") or {}).get("state") or "INCIERTO")
                metrics["assigned"] += 1
                metrics["assigned_by_classification"][state] = metrics["assigned_by_classification"].get(state, 0) + 1
                metrics["assigned_by_portal"]["toctoc"] += 1
                agent_name = agent_by_id[best_id]["name"]
                metrics["assigned_by_executive"][agent_name] = metrics["assigned_by_executive"].get(agent_name, 0) + 1
            elif status == "phone":
                metrics["skipped_by_phone"] += 1
            elif status in {"quality", "stale"}:
                metrics["skipped_by_quality"] += 1
            elif status == "managed":
                metrics["skipped_managed"] += 1
            elif status == "no_coverage":
                metrics["skipped_no_coverage"] += 1
            elif status == "already_assigned":
                metrics["skipped_already_assigned"] += 1

        metrics["remaining_in_snapshot"] = max(0, metrics["eligible_found"] - metrics["assigned"])
        logger.info(
            "[DISTRIBUCION] run=%s evaluadas=%s elegibles=%s batch=%s asignadas=%s phone=%s calidad=%s capacidad=%s",
            run_id, metrics["evaluated"], metrics["eligible_found"], metrics["batch_selected"],
            metrics["assigned"], metrics["skipped_by_phone"], metrics["skipped_by_quality"],
            metrics["skipped_by_capacity"],
        )
        return metrics["assigned"]
    finally:
        metrics["finished_at"] = datetime.now(timezone.utc)
        metrics["duration_ms"] = round((metrics["finished_at"] - started_at).total_seconds() * 1000, 1)
        _record_distribution_metrics(locals().get("db") or get_db(), metrics)
        _DISTRIBUTION_RUN_LOCK.release()


def redistribute_inactive_agent_captaciones(dry_run=True):
    """Reasigna solo captaciones NUEVO de agentes inactivos según comuna."""
    db = get_db()
    coll = get_captacion_collection(db)
    active_agents = list(db["usuarios"].find({
        "is_active": True, "rol": "agente",
        "comunas_interes_norm": {"$exists": True, "$ne": []},
    }))
    inactive_names = [a.get("nombre") for a in db["usuarios"].find(
        {"is_active": False, "rol": "agente"}, {"nombre": 1}
    ) if a.get("nombre")]
    if not active_agents or not inactive_names:
        return {"matched": 0, "planned": 0, "modified": 0, "uncovered": 0, "by_executive": {}}

    agents_by_id = {str(a["_id"]): a for a in active_agents}
    commune_agents = {}
    for aid, agent in agents_by_id.items():
        for commune in agent.get("comunas_interes_norm") or []:
            commune_agents.setdefault(commune, []).append(aid)

    eligible_states = list(VISIBLE_CLASSIFICATION_STATES)
    properties = list(coll.find({
        "origen": {"$in": ["toctoc", "yapo"]},
        "classification.state": {"$in": eligible_states},
        "gestion.semantic_review_hold": {"$ne": True},
        "gestion.estado": "NUEVO",
        "gestion.ejecutivo_asignado": {"$in": inactive_names},
    }).sort("_id", 1))
    from captacion_assignment_eligibility import calculate_assignment_eligibility
    properties = [
        prop for prop in properties
        if calculate_assignment_eligibility(
            prop,
            contact_identity=(get_contact_identity_evidence(db, prop) if phone_learning_global_lookup_enabled() else None),
        )["assignment_ready"]
    ]

    workloads = {aid: 0 for aid in agents_by_id}
    active_names = [a.get("nombre") for a in active_agents]
    for row in coll.aggregate([
        {"$match": {
            "classification.state": {"$in": eligible_states},
            "gestion.estado": "NUEVO",
            "gestion.ejecutivo_asignado": {"$in": active_names},
        }},
        {"$group": {"_id": "$gestion.ejecutivo_id", "count": {"$sum": 1}}},
    ]):
        if row.get("_id") is not None:
            workloads[str(row["_id"])] = row["count"]

    plan = []
    uncovered = 0
    for prop in properties:
        commune = prop.get("comuna_slug") or normalize_commune_canonical(prop.get("comuna") or "")
        candidates = commune_agents.get(commune, [])
        if not candidates:
            uncovered += 1
            continue
        chosen = min(candidates, key=lambda aid: (workloads.get(aid, 0), agents_by_id[aid].get("nombre", "")))
        workloads[chosen] = workloads.get(chosen, 0) + 1
        plan.append((prop, agents_by_id[chosen]))

    by_executive = {}
    modified = 0
    now = datetime.now(timezone.utc)
    for prop, agent in plan:
        name = agent.get("nombre", "")
        if dry_run:
            by_executive[name] = by_executive.get(name, 0) + 1
            continue
        previous_name = prop.get("gestion", {}).get("ejecutivo_asignado")
        cycle = new_assignment_cycle(property_id=prop["_id"], user_id=agent["_id"], assigned_at=now, reason="inactive_executive")
        result = coll.update_one(
            {"_id": prop["_id"], "gestion.estado": "NUEVO", "gestion.ejecutivo_asignado": previous_name},
            {"$set": {
                "gestion.ejecutivo_id": str(agent["_id"]),
                "gestion.ejecutivo_email": agent.get("email", ""),
                "gestion.ejecutivo_asignado": name,
                "gestion.fecha_asignacion": now,
                "gestion.assignment_cycle_id": cycle["assignment_cycle_id"],
                "gestion.first_valid_action_at": None,
                "gestion.asignacion_version": "v2_active_commune_workload",
                "gestion.previous_inactive_assignment": previous_name,
                "gestion.reassignment_reason": "inactive_executive",
                "gestion.reassigned_at": now,
            }},
        )
        modified += result.modified_count
        if result.modified_count:
            by_executive[name] = by_executive.get(name, 0) + 1

    return {
        "matched": len(properties), "planned": len(plan), "modified": modified,
        "uncovered": uncovered, "by_executive": by_executive,
    }

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
