import logging
import re
import random
import difflib
from urllib.parse import quote, urlencode
from datetime import datetime, time, timedelta, timezone
import pytz
from typing import Dict, Any, Optional, Tuple
from pymongo import MongoClient
from config import Config
from .storage import get_db
from .utils import safe_int_conversion
from .property_lookup import (
    PROPERTY_COLLECTION_NAME,
    find_property_by_any_identifier,
    get_prop_executive,
    get_prop_location,
)

logger = logging.getLogger(__name__)

from .constants import CHILE_TZ, BUSINESS_START_HOUR, BUSINESS_END_HOUR, BUSINESS_DAYS


def build_crm_lead_url(lead_data: Dict[str, Any], property_code: Any = None) -> str:
    """Build the authenticated deep link for a specific CRM lead.

    The CRM's existing 401 handler stores this local path in ``login_next``;
    after either password or Google login the executive returns directly to
    this lead.  Phone is normalized to digits so it is safe as a path segment.
    """
    from .phone_utils import is_synthetic_phone

    base_url = str(Config.CRM_BASE_URL or "").rstrip("/")
    phone = (
        lead_data.get("lead_phone")
        or lead_data.get("phone")
        or lead_data.get("whatsapp_phone")
        or ""
    )
    phone_str = str(phone)
    if is_synthetic_phone(phone_str):
        if not phone_str:
            return f"{base_url}/crm?temperatura=HOT"
        url = f"{base_url}/crm/lead/{quote(phone_str, safe='')}"
    else:
        phone_clean = re.sub(r"\D", "", phone_str)
        if not phone_clean:
            return f"{base_url}/crm?temperatura=HOT"
        url = f"{base_url}/crm/lead/{quote(phone_clean, safe='')}"

    code = property_code or lead_data.get("property_code") or lead_data.get("codigo")
    if code not in (None, "", "N/D", "S/N"):
        url += "?" + urlencode({"codigo": str(code)})
    return url

# Constants for specific executives
ERIKA_GARRIDO = "Erika Garrido"
SUSANA_ENSIGNIA = "Susana Ensignia"
MARIELA_ARRIAGADA = "Mariela Arriagada"
MARIA_PAZ_GALLEGUILLOS = "María Paz Galleguillos"
HERNAN_CASTRO = "Hernán Castro"
RAQUEL_CHENEAUX = "Raquel Cheneaux"
PAULA_MORALES = "Paula Morales"
ROCIO_ALIAGA = "Rocío Aliaga"

EXECUTIVES_ON_VACATION = []

# Ejecutivas temporalmente inactivas.
# Para revertir el desvío, basta con quitar el nombre de esta lista.
TEMPORARILY_INACTIVE_EXECUTIVES = [ROCIO_ALIAGA, RAQUEL_CHENEAUX]

# Reemplazo por defecto cuando una ejecutiva está ausente.
DEFAULT_VACATION_REPLACEMENT = ERIKA_GARRIDO

# Reparto especial para los casos de Raquel entre las dos disponibles.
SPECIAL_RAQUEL_TEAM = [MARIELA_ARRIAGADA]

# Mapeo de reemplazos para asignaciones directas (fuera de Round Robin)
VACATION_REPLACEMENTS = {
    ERIKA_GARRIDO: RAQUEL_CHENEAUX
}

def is_raquel_unavailable() -> bool:
    """Retorna True si el día de asignación efectivo es Lunes (0) o Miércoles (2)."""
    now = datetime.now(CHILE_TZ)
    effective_time = get_next_business_slot(now)
    return effective_time.weekday() in [0, 2]

def is_executive_temporarily_inactive(name: str) -> bool:
    """Indica si una ejecutiva debe ser derivada por ausencia o vacaciones."""
    if not name:
        return False
    norm_name = normalize_text(name)
    inactive_team = [normalize_text(n) for n in TEMPORARILY_INACTIVE_EXECUTIVES + EXECUTIVES_ON_VACATION]
    
    # Check if any inactive executive name is contained within the normalized name (e.g. 'rocio aliaga' in 'rocio aliaga valz')
    for inactive in inactive_team:
        if inactive and (inactive in norm_name or norm_name in inactive):
            return True
    return False

def get_special_raquel_replacement() -> str:
    """Alterna entre Erika y Mariela para reemplazar a Raquel."""
    db = get_db()
    state_col = db["lead_routing_state"]
    state = state_col.find_one({"id": "raquel_special_rr"})
    last_index = state.get("last_index", -1) if state else -1
    next_index = (last_index + 1) % len(SPECIAL_RAQUEL_TEAM)
    candidate = SPECIAL_RAQUEL_TEAM[next_index]
    state_col.update_one(
        {"id": "raquel_special_rr"},
        {"$set": {"last_index": next_index}},
        upsert=True
    )
    return candidate

def get_active_executive(name: str, norm_comuna: str = "") -> str:
    """Retorna el reemplazo si el ejecutivo está en vacaciones, o si no está disponible, deriva a RR."""
    norm_name = normalize_text(name)
    if is_executive_temporarily_inactive(name):
        if normalize_text(RAQUEL_CHENEAUX) in norm_name:
            replacement = get_special_raquel_replacement()
            logger.info(f"[VACATION] Redirigiendo asignación de {name} al reemplazo especial: {replacement}")
            name = replacement
        else:
            logger.info(f"[VACATION] Redirigiendo asignación de {name} a reemplazo por defecto: {DEFAULT_VACATION_REPLACEMENT}")
            name = DEFAULT_VACATION_REPLACEMENT
            
    # Check again if the new/current name is Raquel and she is unavailable today
    if normalize_text(RAQUEL_CHENEAUX) in normalize_text(name) and is_raquel_unavailable():
        logger.info(f"[VACATION] {name} no trabaja hoy (Lunes o Miércoles). Derivando a Round Robin.")
        return get_next_round_robin_executive(norm_comuna)

    return name

# Lista para Round Robin (Jorge Pablo Caro - RM).
# Erika deja de recibir propiedades JPC en RM; el reparto queda entre
# Mariela, Hernán y María Paz.
ROUND_ROBIN_TEAM = [MARIELA_ARRIAGADA, HERNAN_CASTRO, MARIA_PAZ_GALLEGUILLOS]

# Phone mapping (This should ideally be in a DB or Config, but hardcoding for now as requested/implied)
# NOTE: You will need to fill in real numbers or ensure they are in the DB users collection.
# For now I will assume the 'ejecutivo' field in DB has the name, and we need to look up their phone
# in the 'usuarios' collection or similar.
# If names don't match exactly, we might need a mapping.

def normalize_text(text: str) -> str:
    """Normalize text for comparison (lowercase, strip accents)."""
    if not text:
        return ""
    import unicodedata
    text = str(text).lower().strip()
    # Normalize unicode to decompose accents, then filter them out
    text = "".join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
    return text


# Comunas RM que Mariela acepta dentro del reparto de Jorge Pablo Caro.
MARIELA_COMUNAS_RM = frozenset(
    normalize_text(comuna)
    for comuna in ("Macul", "Ñuñoa", "Providencia", "Las Condes", "Santiago")
)

def is_business_hours(dt=None) -> bool:
    """Check if the given time (or now) falls within configured notification hours."""
    value = dt or datetime.now(CHILE_TZ)
    start = int(getattr(Config, "CRM_NOTIFICATION_BUSINESS_START", 9))
    end = int(getattr(Config, "CRM_NOTIFICATION_BUSINESS_END", 19))
    from .business_calendar import is_business_time
    return is_business_time(value, start_hour=start, end_hour=end)


def after_hours_hot_mode() -> str:
    return str(getattr(Config, "CRM_AFTER_HOURS_HOT_MODE", "NEXT_BUSINESS_OPEN")).upper()


def next_business_slot_after_minutes(dt, minutes=15):
    """Return the first business slot at least ``minutes`` after ``dt``."""
    slot = get_next_business_slot(dt)
    from datetime import timedelta
    candidate = slot + timedelta(minutes=minutes)
    if candidate.hour >= int(getattr(Config, "CRM_NOTIFICATION_BUSINESS_END", 19)):
        candidate = get_next_business_slot(candidate)
    return candidate


def should_send_now() -> bool:
    """
    Check if current time in Chile is within business hours.
    """
    now = datetime.now(CHILE_TZ)
    weekday = now.weekday()
    hour = now.hour
    
    is_business_day = weekday in BUSINESS_DAYS
    is_in_hours = (hour >= BUSINESS_START_HOUR and hour < BUSINESS_END_HOUR)
    
    result = is_business_day and is_in_hours
    
    # logger.info(f"[SCHEDULE_DEBUG] Chile Time: {now.strftime('%H:%M:%S')} | Hour: {hour} | Weekday: {weekday} | In Hours: {is_in_hours} | Final Result: {result}")
    
    return result

def get_next_business_slot(dt: datetime) -> datetime:
    """Return the current business instant or the next opening in UTC."""
    start = int(getattr(Config, "CRM_NOTIFICATION_BUSINESS_START", 9))
    end = int(getattr(Config, "CRM_NOTIFICATION_BUSINESS_END", 19))
    from .business_calendar import next_business_slot_utc
    return next_business_slot_utc(dt, start_hour=start, end_hour=end)

def get_next_round_robin_executive(norm_comuna: str = "") -> str:
    """
    Obtiene el siguiente ejecutivo de la lista usando un estado persistente en MongoDB.
    Si el ejecutivo seleccionado es Mariela pero la comuna no es de su prioridad, 
    se salta al siguiente de la lista.
    """
    db = get_db()
    state_col = db["lead_routing_state"]
    # Buscamos el estado actual
    state = state_col.find_one({"id": "jpc_rm_round_robin"})
    last_index = state.get("last_index", -1) if state else -1
    
    # Intentamos encontrar el siguiente válido
    for i in range(1, len(ROUND_ROBIN_TEAM) + 1):
        next_index = (last_index + i) % len(ROUND_ROBIN_TEAM)
        candidate = ROUND_ROBIN_TEAM[next_index]
        
        # Filtro Vacaciones: Saltar si está en vacaciones
        if is_executive_temporarily_inactive(candidate):
            logger.info(f"[ROUTER] Saltando a {candidate} (En modo vacaciones).")
            continue
            
        # Filtro Raquel: No trabaja lunes y miércoles
        if candidate == RAQUEL_CHENEAUX and is_raquel_unavailable():
            logger.info(f"[ROUTER] Saltando a {candidate} (No está disponible hoy Lunes/Miércoles).")
            continue
    
        # Filtro Mariela: Si es Mariela, debe ser comuna de prioridad
        if candidate == MARIELA_ARRIAGADA:
            comuna = normalize_text(norm_comuna)
            es_comuna_prioritaria = any(
                comuna == prioritaria or comuna.startswith(f"{prioritaria} ")
                for prioritaria in MARIELA_COMUNAS_RM
            )
            if not es_comuna_prioritaria:
                logger.info(f"[ROUTER] Saltando a Mariela para comuna '{norm_comuna}' (No es prioridad).")
                continue
        
        # Si llegamos aquí, el candidato es válido
        state_col.update_one(
            {"id": "jpc_rm_round_robin"}, 
            {"$set": {"last_index": next_index}}, 
            upsert=True
        )
        logger.info(f"[ROUTER] Round Robin: Turno de {candidate} (index {next_index}) para comuna '{norm_comuna}'")
        return candidate

    # Fallback extremo (si algo fallara en el loop)
    # Evito llamar a get_active_executive con Erika de nuevo para prevenir recursividad, ERIKA puede ser el default
    # Este fallback también debe respetar que Erika no recibe JPC en RM.
    return HERNAN_CASTRO

from .constants import UNASSIGNED_LABEL

_EXECUTIVES_USERS_CACHE = {"data": None, "expires_at": 0}

def get_executive_phone(executive_name: str) -> Optional[str]:
    """
    Look up executive phone in 'usuarios' collection (field 'telefono' or 'movil').
    Uses robust normalization for matching.
    """
    if not executive_name or executive_name == UNASSIGNED_LABEL:
        return None

    import time
    db = get_db()
    # 1. Intento directo exacto
    user = db["usuarios"].find_one({"nombre": executive_name})
    
    # 2. Si falla, búsqueda robusta por normalización
    if not user:
        norm_target = normalize_text(executive_name)
        
        now = time.time()
        if _EXECUTIVES_USERS_CACHE["data"] is None or now > _EXECUTIVES_USERS_CACHE["expires_at"]:
            all_users = list(db["usuarios"].find({}, {"nombre": 1, "telefono": 1, "tel": 1, "movil": 1}))
            _EXECUTIVES_USERS_CACHE["data"] = all_users
            _EXECUTIVES_USERS_CACHE["expires_at"] = now + 300 # 5 min
            
        for candidate in _EXECUTIVES_USERS_CACHE["data"]:
            norm_candidate = normalize_text(candidate.get("nombre"))
            # Match exacto normalizado
            if norm_candidate == norm_target:
                user = candidate
                break
            # Match parcial: "raquel cheneaux" contenido en "raquel cheneaux valz" (o viceversa)
            if norm_candidate and norm_target and (norm_candidate in norm_target or norm_target in norm_candidate):
                user = candidate
                logger.info(f"[LOOKUP] Match parcial: '{candidate.get('nombre')}' ~ '{executive_name}'")
                break
                
            # Fuzzy match para atrapar problemas de encoding de base de datos (Ej: Rocío vs Roco)
            if norm_candidate and norm_target:
                similarity = difflib.SequenceMatcher(None, norm_target, norm_candidate).ratio()
                if similarity > 0.85:
                    user = candidate
                    logger.info(f"[LOOKUP] Match fuzzy por similitud ({similarity:.2f}): '{candidate.get('nombre')}' ~ '{executive_name}'")
                    break
                
    if user:
        phone = user.get("telefono") or user.get("tel") or user.get("movil")
        if phone:
            logger.info(f"[LOOKUP] Usuario encontrado: {user.get('nombre')} | Tel: {phone}")
            return str(phone).strip()
    
    logger.warning(f"[LOOKUP] No se encontró usuario '{executive_name}' en colección 'usuarios'.")
    return None

def get_active_executive_phone(executive_name: str) -> Optional[str]:
    """Return a phone only for an explicitly active, exact CRM user."""
    if not executive_name or executive_name == UNASSIGNED_LABEL:
        return None
    user = get_db()["usuarios"].find_one({"nombre": executive_name, "is_active": True})
    if not user:
        logger.warning("[LOOKUP] Ejecutivo no activo o no inequívoco: %r", executive_name)
        return None
    phone = user.get("telefono") or user.get("tel") or user.get("movil")
    return str(phone).strip() if phone else None


def find_responsible_executive(property_code: Optional[str] = None, comuna: Optional[str] = None, zone: Optional[str] = None, lead_phone: Optional[str] = None, lead_name: Optional[str] = None) -> Tuple[str, Optional[str], str]:
    """
    Determines the responsible executive based on property rules or regional fallbacks.
    Returns (Executive Name, Executive Phone, Assignment Type).
    """
    db = get_db()
    
    assignment_type = "COMMUNE_FALLBACK" if (not property_code and comuna) else "ZONE_FALLBACK" if (not property_code and zone) else "PROPERTY"
    
    # BUSQUEDA ROBUSTA (String o Int)
    prop = None
    if property_code:
        logger.info(f"[ROUTER] Buscando responsable para propiedad: '{property_code}'")
        prop = find_property_by_any_identifier(db, property_code, PROPERTY_COLLECTION_NAME)

        # Respaldo: Si property_code parece link, extraer IDs y re-buscar
        if not prop and ("http" in str(property_code).lower() or ".cl" in str(property_code).lower()):
            from .link_extractor import extraer_codigo_yapo, extraer_codigo_mercadolibre
            potential_ids = []
            
            # Intentar extractores específicos
            c_yapo = extraer_codigo_yapo(str(property_code))
            if c_yapo: potential_ids.append(c_yapo)
            c_ml = extraer_codigo_mercadolibre(str(property_code))
            if c_ml: potential_ids.append(c_ml)
            
            # Extraer cualquier secuencia de 4-10 dígitos (Deep Search)
            potential_ids.extend(re.findall(r"(\d{4,10})", str(property_code)))
            
            if potential_ids:
                logger.info(f"[ROUTER] Intentando match profundo con IDs extraídos de URL: {list(set(potential_ids))}")
                for candidate in potential_ids:
                    prop = find_property_by_any_identifier(db, candidate, PROPERTY_COLLECTION_NAME)
                    if prop:
                        break
    
    region = ""
    norm_region = ""
    norm_comuna = normalize_text(comuna) if comuna else ""
    norm_exec = ""
    original_executive = ""
    phone = None

    if not prop and property_code:
        logger.warning(
            f"[ROUTER] Propiedad {property_code} NO encontrada en {PROPERTY_COLLECTION_NAME}. "
            "No se asigna ejecutivo."
        )
        return UNASSIGNED_LABEL, None, "MISSING_PROPERTY"

    elif not prop:
        logger.info("[ROUTER] Lead sin propiedad confirmada. Se deja sin asignar.")
        return UNASSIGNED_LABEL, None, "NO_PROPERTY"
    else:
        original_executive = get_prop_executive(prop)
        logger.info(f"[ROUTER] Propiedad encontrada. Ejecutivo original en ficha: '{original_executive}'")
        location = get_prop_location(prop)
        region = location["region"]
        prop_comuna = location["comuna"]
        
        norm_region = normalize_text(region)
        norm_comuna = normalize_text(prop_comuna) if prop_comuna else norm_comuna
        norm_exec = normalize_text(original_executive)

        target_executive_name = original_executive # Default: El que viene en la ficha

    # REGLA 0: Si el ejecutivo de la ficha ya es uno de los nuestros, se queda con él (Lo lógico)
    # PERO: Respetamos si está de vacaciones y redirigimos a su reemplazo
    our_team = [
        ERIKA_GARRIDO,
        MARIELA_ARRIAGADA,
        MARIA_PAZ_GALLEGUILLOS,
        HERNAN_CASTRO,
        SUSANA_ENSIGNIA,
        RAQUEL_CHENEAUX,
        PAULA_MORALES,
        ROCIO_ALIAGA,
    ]
    
    # Check if the original executive is active in the users collection
    is_active_user = False
    if original_executive and original_executive != UNASSIGNED_LABEL:
        # get_executive_phone does a lookup in 'usuarios'
        test_phone = get_executive_phone(original_executive)
        if test_phone:
            is_active_user = True

    matched_member = next((member for member in our_team if normalize_text(member) in norm_exec), None)
    
    if matched_member:
        logger.info(f"[ROUTER] Propiedad ya pertenece a alguien del equipo ({original_executive} -> normalizado a {matched_member}). Verificando disponibilidad.")
        target_executive_name = get_active_executive(matched_member, norm_comuna)
    
    # REGLA 1: Distribución Regional (Para JPC o ejecutivos antiguos/inactivos o sin ejecutivo)
    elif "jorge pablo caro" in norm_exec or not is_active_user:
        if "jorge pablo caro" not in norm_exec:
             logger.info(f"[ROUTER] Ejecutivo original '{original_executive}' inactivo o no asignado. Aplicando distribución regional.")
             
        # 1.1 RM -> Mariela, Hernán y María Paz (Con filtro de comunas de Mariela)
        if "metropolitana" in norm_region or "xiii" in norm_region:
            target_executive_name = get_next_round_robin_executive(norm_comuna)
        
        # 1.2 Región del Maule (VII) -> Paula Morales
        elif "maule" in norm_region or "vii" in norm_region:
             logger.info(f"[ROUTER] Propiedad en Maule. Asignando a {PAULA_MORALES}")
             target_executive_name = get_active_executive(PAULA_MORALES, norm_comuna)
             
        # 1.3 Todas las demás regiones -> Erika Garrido
        else:
            logger.info(f"[ROUTER] Propiedad en otra región ({region}). Asignando a {ERIKA_GARRIDO}")
            target_executive_name = get_active_executive(ERIKA_GARRIDO, norm_comuna)
            
    else:
        # Para cualquier otro ejecutivo en la colección usuarios pero no en our_team (ej. perfiles especiales)
        words = target_executive_name.split()
        if len(words) > 2:
            target_executive_name = f"{words[0]} {words[1]}"
            logger.info(f"[ROUTER] Acortando nombre de ejecutivo: '{original_executive}' a '{target_executive_name}'")
    
    # Get phone for the determined executive
    phone = get_executive_phone(target_executive_name)
    
    # If we couldn't find the phone for the target, but we had an original executive with phone in the property card?
    # Algunas fichas nuevas todavía traen teléfonos de respaldo a nivel raíz.
    if not phone and target_executive_name == original_executive:
         phone = prop.get("movil_ejecutivo") or prop.get("fono_ejecutivo")

    # Final Safety Check: Asegurar que el el final no esté de vacaciones (por si se coló por otra regla)
    target_executive_name = get_active_executive(target_executive_name, norm_comuna)

    # FALLBACK DE EMERGENCIA: Si no hay teléfono o es No Asignado, asignamos a Round Robin
    # PERO: Respetamos la decisión de dejarlo como pendiente si la propiedad NO EXISTE (Rule refinement)
    if target_executive_name == UNASSIGNED_LABEL or target_executive_name == "":
        logger.warning(f"[ROUTER] Fallback: Sin ejecutivo válido. Asignando a Round Robin (RM).")
        target_executive_name = get_next_round_robin_executive("")
        phone = get_executive_phone(target_executive_name)
        
        
    elif not phone:
        # NUEVA REGLA DE INTEGRIDAD: Si sabemos quién es, pero no está su teléfono, SE LO QUEDA IGUAL,
        # solo que el envío de Whatsapp fallará más abajo (y quedará guardado silenciosamente).
        logger.warning(f"[ROUTER] El ejecutivo '{target_executive_name}' no tiene teléfono en DB. Se mantiene el Lead pero no recibirá Whatsapp.")

    return target_executive_name, phone, assignment_type

HOT_CONTEXT_INITIAL = "initial_hot"
HOT_CONTEXT_ESCALATED = "escalated_after_digest"
HOT_CONTEXT_REASSIGNMENT = "new_assignment_cycle"
VALID_HOT_CONTEXTS = {HOT_CONTEXT_INITIAL, HOT_CONTEXT_ESCALATED, HOT_CONTEXT_REASSIGNMENT}


def format_hot_whatsapp_template(lead_data: Dict[str, Any], executive_name: str, property_code: str) -> str:
    """
    Formats a differentiated WhatsApp message for HOT leads.
    Supports context types:
    - initial_hot: first HOT notification (new lead or transition before digest)
    - escalated_after_digest: lead was previously notified as non-HOT and later became HOT
    - new_assignment_cycle: new assignment cycle (reassignment)
    """
    hot_context = lead_data.get("hot_context") or HOT_CONTEXT_INITIAL
    if hot_context not in VALID_HOT_CONTEXTS:
        hot_context = HOT_CONTEXT_INITIAL

    prop_inline = lead_data.get("property_data", {}) if isinstance(lead_data, dict) else {}
    comuna = (
        lead_data.get("comuna")
        or prop_inline.get("comuna")
        or ""
    )
    region = (
        lead_data.get("region")
        or prop_inline.get("region")
        or ""
    )
    operacion = (
        lead_data.get("operacion")
        or prop_inline.get("operacion")
        or "Operación no especificada"
    )
    nombre_cliente = lead_data.get("nombre")
    if not nombre_cliente or nombre_cliente == "None":
        nombre_cliente = "Cliente"
    hot_reason = lead_data.get("hot_reason") or lead_data.get("reason") or "Clasificación automática"
    mensaje_usuario = lead_data.get("last_message", "")
    lead_phone = lead_data.get("lead_phone") or lead_data.get("phone") or ""
    created_at = lead_data.get("created_at") or lead_data.get("timestamp") or ""

    crm_url = build_crm_lead_url(lead_data, property_code)

    ubicacion_lines = ""
    if comuna:
        ubicacion_lines += f"📍 *Comuna*: {comuna}\n"
    if region:
        ubicacion_lines += f"📍 *Región*: {region}\n"

    if hot_context == HOT_CONTEXT_ESCALATED:
        return (
            f"🔥 *LEAD ASIGNADO PASÓ A HOT*\n\n"
            f"Hola {executive_name}, un lead que ya estaba asignado a ti ha sido clasificado como *Hot*.\n\n"
            f"👤 *Cliente*: {nombre_cliente}\n"
            f"📱 *Contacto*: {lead_phone}\n"
            f"🏠 *Propiedad*: {property_code} | {operacion}\n"
            f"{ubicacion_lines}"
            f"⚡ *Nuevo motivo*: {hot_reason}\n"
            f"🕐 *Transición*: {created_at}\n\n"
            f"📝 *Mensaje del cliente*: {mensaje_usuario}\n\n"
            f"🔗 *Gestionar ahora en CRM*:\n{crm_url}\n\n"
            f"💡 _Toda gestión debe registrarse en el CRM para el control SLA y seguimiento._\n"
            f"¡Mucho éxito! 🚀"
        )

    if hot_context == HOT_CONTEXT_REASSIGNMENT:
        return (
            f"🔥 *LEAD HOT — NUEVA ASIGNACIÓN*\n\n"
            f"Hola {executive_name}, se te ha reasignado un *Lead Hot* que requiere gestión inmediata.\n\n"
            f"👤 *Cliente*: {nombre_cliente}\n"
            f"📱 *Contacto*: {lead_phone}\n"
            f"🏠 *Propiedad*: {property_code} | {operacion}\n"
            f"{ubicacion_lines}"
            f"⚡ *Motivo*: {hot_reason}\n"
            f"🕐 *Asignado*: {created_at}\n\n"
            f"📝 *Mensaje del cliente*: {mensaje_usuario}\n\n"
            f"🔗 *Gestionar ahora en CRM*:\n{crm_url}\n\n"
            f"💡 _Toda gestión debe registrarse en el CRM para el control SLA y seguimiento._\n"
            f"¡Mucho éxito! 🚀"
        )

    # Default: initial_hot
    return (
        f"🔥 *LEAD HOT — ATENCIÓN PRIORITARIA*\n\n"
        f"Hola {executive_name}, se te ha asignado un *Lead Hot* que requiere gestión inmediata.\n\n"
        f"👤 *Cliente*: {nombre_cliente}\n"
        f"📱 *Contacto*: {lead_phone}\n"
        f"🏠 *Propiedad*: {property_code} | {operacion}\n"
        f"{ubicacion_lines}"
        f"⚡ *Motivo*: {hot_reason}\n"
        f"🕐 *Asignado*: {created_at}\n\n"
        f"📝 *Mensaje del cliente*: {mensaje_usuario}\n\n"
        f"🔗 *Gestionar ahora en CRM*:\n{crm_url}\n\n"
        f"💡 _Toda gestión debe registrarse en el CRM para el control SLA y seguimiento._\n"
        f"¡Mucho éxito! 🚀"
    )


def format_whatsapp_template(lead_data: Dict[str, Any], executive_name: str, property_code: str, is_new_assignment: bool = True) -> str:
    """
    Formats the WhatsApp message to be sent to the executive.
    """
    # Importante: esta función puede llamarse desde contextos async.
    # Evitamos cualquier acceso sync a Mongo aquí para no bloquear event loop.
    
    # --- MENSAJE ESPECIAL PARA ADMIN (PROPIEDAD NO ENCONTRADA / LINK ROTO) ---
    if lead_data.get("assignment_type") == "MISSING_PROPERTY" or lead_data.get("lead_type") == "MissingProperty":
        nombre_cliente = lead_data.get("nombre")
        telefono_cliente = lead_data.get("phone", "Desconocido")
        comentario_cliente = lead_data.get("last_message", "")
        cliente_texto = f"{nombre_cliente} ({telefono_cliente})" if nombre_cliente and nombre_cliente != "None" else telefono_cliente
        
        return (
            f"🚨 *Alerta: Enlace o Propiedad No Encontrada*\n\n"
            f"Hola {executive_name}, el asistente recibió un enlace o código que no existe en nuestra base de datos actualizada (Prop360).\n\n"
            f"📌 *Detalles del caso:*\n"
            f"👤 Cliente: {cliente_texto}\n"
            f"📝 Mensaje recibido:\n{comentario_cliente}\n\n"
            f"⚠️ _Por favor, revisa si es necesario actualizar la cartera o si la propiedad fue dada de baja._\n\n"
            f"🔗 *Ver caso en CRM*:\n{build_crm_lead_url(lead_data, property_code)}"
        )
    prop_inline = lead_data.get("property_data", {}) if isinstance(lead_data, dict) else {}
    comuna = (
        lead_data.get("comuna")
        or prop_inline.get("comuna")
        or ""
    )
    region = (
        lead_data.get("region")
        or prop_inline.get("region")
        or ""
    )
    operacion = (
        lead_data.get("operacion")
        or prop_inline.get("operacion")
        or "Operación no especificada"
    )
    
    nombre_cliente = lead_data.get("nombre")
    if not nombre_cliente or nombre_cliente == "None":
        nombre_cliente = ""

    # Header dinámico: HOT tiene prioridad máxima
    hot_context = lead_data.get("hot_context") or ""
    if hot_context:
        header = "\uD83D\uDD25 *\u00A1NUEVO LEAD HOT!* \uD83D\uDD25"
    else:
        header = "\uD83D\uDE80 *\u00A1Nuevo Lead Asignado!*" if is_new_assignment else "\uD83D\uDCAC *Actualizaci\u00F3n de Lead*"
    
    # Si es seguimiento, enfatizamos que ya tiene dueño
    contexto_extra = ""
    if not is_new_assignment:
        contexto_extra = f"⚠️ _Este cliente ya está asignado a ti._\n"

    crm_url = build_crm_lead_url(lead_data, property_code)

    ubicacion_lines = ""
    if comuna:
        ubicacion_lines += f"📍 *Comuna*: {comuna}\n"
    if region:
        ubicacion_lines += f"📍 *Región*: {region}\n"

    cliente_line = f"👤 *Cliente*: {nombre_cliente}\n" if nombre_cliente else ""

    mensaje_usuario = lead_data.get("last_message", "Interesado en esta propiedad")

    template = (
        f"{header}\n\n"
        f"Hola {executive_name}, se ha asignado un nuevo lead a tu gestión. "
        f"Realiza la gestión a la brevedad para maximizar la conversión. ⚡\n\n"
        f"🏠 *Propiedad*: {property_code} | {operacion}\n"
        f"{ubicacion_lines}"
        f"{cliente_line}"
        f"{contexto_extra}"
        f"📝 *Comentario*: {mensaje_usuario}\n\n"
        f"🔗 *Ver y Gestionar en CRM*:\n{crm_url}\n\n"
        f"💡 _Recuerda ingresar con tu correo corporativo Procasa._\n"
        f"¡Mucho éxito con la gestión! 🚀"
    )
    return template

def format_summary_whatsapp_template(leads_list: list, executive_name: str) -> str:
    """
    Formats a single message summarizing multiple new leads for an executive.
    """
    from .notification_identity import deduplicate_lead_notifications

    leads_list = deduplicate_lead_notifications(leads_list)
    header = f"🚀 *{len(leads_list)} Nuevos Leads Asignados*"
    
    leads_details = ""
    for i, lead in enumerate(leads_list, 1):
        # Los items vienen como docs de pending_notifications: {"lead_data": {...}}
        # Navegamos al nivel real con fallback al item directamente
        ld = lead.get("lead_data") if isinstance(lead.get("lead_data"), dict) else lead
        nombre = ld.get("nombre") or ld.get("prospecto_nombre") or "Cliente"
        p_code = ld.get("property_code") or "S/N"
        canal = ld.get("canal") or ld.get("source") or ld.get("origen") or "Directo"
        
        crm_url = build_crm_lead_url(ld, p_code)
        leads_details += (
            f"\n{i}. *{nombre}* - Prop: {p_code} ({canal})"
            f"\n   🔗 {crm_url}"
        )

    crm_url = f"{str(Config.CRM_BASE_URL or '').rstrip('/')}/crm?temperatura=HOT"

    template = (
        f"{header}\n\n"
        f"Hola {executive_name}, tienes {len(leads_list)} nuevos leads esperando tu gestion:\n"
        f"{leads_details}\n\n"
        f"\U0001F517 *Gestionar todos en el CRM*:\n{crm_url}\n\n"
        f"\u26A1 _Realiza la gestion a la brevedad para no perder la oportunidad._\n"
        f"\u00A1Mucho exito! \U0001F680"
    )
    return template


# ---------------------------------------------------------------------------
# Secure CRM URL builder (no phone in URL)
# ---------------------------------------------------------------------------

def build_secure_crm_url(lead: dict, property_code: str | None = None) -> str:
    """Build a CRM deep-link URL using the lead ObjectId, never the phone.
    
    Property code is resolved server-side, never passed in query string.
    """
    from config import Config
    base = str(getattr(Config, "CRM_BASE_URL", "https://procasa-chatbot-yr8d.onrender.com")).rstrip("/")
    lid = lead.get("_id", "")
    return f"{base}/crm/lead-id/{lid}"


# ---------------------------------------------------------------------------
# Canonical notification messages — production-only templates
# ---------------------------------------------------------------------------

def build_hot_lead_message(ctx: dict) -> str:
    """Build the definitive HOT lead WhatsApp message from a notification context.

    Args:
        ctx: dict from build_lead_notification_context()
    """
    exec_name = ctx.get("exec_name") or "Ejecutivo"
    code = ctx.get("property_code") or "S/N"
    operacion = ctx.get("operacion") or ""
    tipo = ctx.get("tipo_propiedad") or ""
    comuna = ctx.get("comuna") or ""
    hot_reason = ctx.get("hot_reason") or ""
    nombre_cliente = ctx.get("nombre_cliente") or ""
    url = ctx.get("secure_url") or ""

    # Build property line: Prop. CODIGO [· operacion] [· tipo] [· comuna]
    prop_parts = [f"\U0001F3E0 *Prop. {code}*"]
    for part in (operacion, tipo, comuna):
        if part:
            prop_parts.append(f"\u00B7 {part}")
    prop_line = " ".join(prop_parts)

    lines = [
        "\U0001F525 *NUEVO LEAD HOT*",
        "",
        f"Hola {exec_name}, tienes un lead prioritario pendiente de gesti\u00F3n.",
        "",
        prop_line,
    ]
    if hot_reason:
        lines.append(f"\U0001F3AF Motivo: {hot_reason}")
    if nombre_cliente:
        lines.append(f"\U0001F464 Cliente: {nombre_cliente}")
    lines.extend([
        "",
        f"\U0001F517 *Gestionar en CRM:*",
        url,
        "",
        "\u26A0\uFE0F Registra el resultado en el CRM. Abrir WhatsApp o llamar no cuenta como gesti\u00F3n.",
    ])
    return "\n".join(lines)


def build_digest_lead_message(contexts: list[dict], exec_name: str = "") -> str:
    """Build the definitive non-HOT assignment digest WhatsApp message.

    Args:
        contexts: list of dicts from build_lead_notification_context()
        exec_name: executive display name

    This builder is retained for grouped non-HOT notifications.  A digest
    containing one lead is rendered by ``format_whatsapp_template`` instead,
    so it is exactly identical to the normal individual delivery.
    """
    count = len(contexts)
    if not contexts:
        return ""
    exec_display = exec_name or contexts[0].get("exec_name") or "Ejecutivo"

    if count == 1:
        ctx = contexts[0]
        header = "\U0001F195 *NUEVO LEAD ASIGNADO*"
        lead_preview = _format_context_preview(ctx)
        lines = [
            header,
            "",
            f"Hola {exec_display}, te asignaron un lead nuevo para revisar.",
            "",
            lead_preview,
        ]
    else:
        header = f"\U0001F195 *{count} NUEVOS LEADS ASIGNADOS*"
        previews = [_format_context_preview(c) for c in contexts]
        numbered = [f"{i+1}. {p}" for i, p in enumerate(previews)]
        lines = [
            header,
            "",
            f"Hola {exec_display}, te asignaron {count} leads nuevos para revisar.",
            "",
        ] + numbered

    lines.extend([
        "",
        "\u26A0\uFE0F Registra el resultado de la gesti\u00F3n en el CRM. Abrir WhatsApp o llamar no cuenta como gesti\u00F3n.",
    ])
    return "\n".join(lines)


def _format_context_preview(ctx: dict) -> str:
    """Format a single lead preview line for the digest."""
    code = ctx.get("property_code") or "S/N"
    parts = [f"*Prop. {code}*"]
    for field in ("operacion", "tipo_propiedad", "comuna"):
        v = ctx.get(field)
        if v:
            parts.append(f"\u00B7 {v}")
    line = " ".join(parts)
    nombre = ctx.get("nombre_cliente")
    if nombre:
        line += f"\n   \U0001F464 {nombre}"
    secure_url = ctx.get("secure_url")
    if secure_url:
        line += f"\n   \U0001F517 Abrir lead: {secure_url}"
    return line
