import logging
import re
import random
import difflib
from datetime import datetime, time, timedelta
import pytz
from typing import Dict, Any, Optional, Tuple
from pymongo import MongoClient
from config import Config
from .storage import get_db
from .utils import safe_int_conversion

logger = logging.getLogger(__name__)

from .constants import CHILE_TZ, BUSINESS_START_HOUR, BUSINESS_END_HOUR, BUSINESS_DAYS

# Constants for specific executives
ERIKA_GARRIDO = "Erika Garrido"
SUSANA_ENSIGNIA = "Susana Ensignia"
MARIELA_ARRIAGADA = "Mariela Arriagada"
RAQUEL_CHENEAUX = "Raquel Cheneaux"
PAULA_MORALES = "Paula Morales"
ROCIO_ALIAGA = "Rocío Aliaga"

EXECUTIVES_ON_VACATION = []

# Mapeo de reemplazos para asignaciones directas (fuera de Round Robin)
VACATION_REPLACEMENTS = {
    ERIKA_GARRIDO: RAQUEL_CHENEAUX
}

def is_raquel_unavailable() -> bool:
    """Retorna True si el día de asignación efectivo es Lunes (0) o Miércoles (2)."""
    now = datetime.now(CHILE_TZ)
    effective_time = get_next_business_slot(now)
    return effective_time.weekday() in [0, 2]

def get_active_executive(name: str, norm_comuna: str = "") -> str:
    """Retorna el reemplazo si el ejecutivo está en vacaciones, o si no está disponible, deriva a RR."""
    if name in EXECUTIVES_ON_VACATION:
        replacement = VACATION_REPLACEMENTS.get(name)
        if replacement:
            logger.info(f"[VACATION] Redirigiendo asignación de {name} a su reemplazo: {replacement}")
            name = replacement
        else:
            return get_next_round_robin_executive(norm_comuna)
            
    if name == RAQUEL_CHENEAUX and is_raquel_unavailable():
        logger.info(f"[VACATION] {name} no trabaja hoy (Lunes o Miércoles). Derivando a Round Robin.")
        return get_next_round_robin_executive(norm_comuna)

    return name

# Lista para Round Robin (Jorge Pablo Caro - RM)
ROUND_ROBIN_TEAM = [MARIELA_ARRIAGADA, SUSANA_ENSIGNIA, ERIKA_GARRIDO, RAQUEL_CHENEAUX]

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
    """Calcula el inicio del próximo bloque laboral si dt está fuera de horario."""
    # Si ya es hora laboral, retornar el mismo
    if dt.hour >= BUSINESS_START_HOUR and dt.hour < BUSINESS_END_HOUR and dt.weekday() in BUSINESS_DAYS:
        return dt
    
    next_slot = dt.replace(hour=BUSINESS_START_HOUR, minute=0, second=0, microsecond=0)
    
    # Si ya pasó la hora de inicio de hoy, es mañana
    if dt.hour >= BUSINESS_END_HOUR or (dt.hour == BUSINESS_START_HOUR and dt.minute > 0):
        next_slot += timedelta(days=1)
    
    # Si cae en fin de semana, saltar al lunes
    while next_slot.weekday() not in BUSINESS_DAYS:
        next_slot += timedelta(days=1)
        
    return next_slot

def get_next_round_robin_executive(norm_comuna: str = "") -> str:
    """
    Obtiene el siguiente ejecutivo de la lista usando un estado persistente en MongoDB.
    Si el ejecutivo seleccionado es Mariela pero la comuna no es de su prioridad, 
    se salta al siguiente de la lista.
    """
    db = get_db()
    state_col = db["lead_routing_state"]
    mariela_comunas = ["macul", "nunoa", "providencia", "las condes", "santiago"]
    
    # Buscamos el estado actual
    state = state_col.find_one({"id": "jpc_rm_round_robin"})
    last_index = state.get("last_index", -1) if state else -1
    
    # Intentamos encontrar el siguiente válido
    for i in range(1, len(ROUND_ROBIN_TEAM) + 1):
        next_index = (last_index + i) % len(ROUND_ROBIN_TEAM)
        candidate = ROUND_ROBIN_TEAM[next_index]
        
        # Filtro Vacaciones: Saltar si está en vacaciones
        if candidate in EXECUTIVES_ON_VACATION:
            logger.info(f"[ROUTER] Saltando a {candidate} (En modo vacaciones).")
            continue
            
        # Filtro Raquel: No trabaja lunes y miércoles
        if candidate == RAQUEL_CHENEAUX and is_raquel_unavailable():
            logger.info(f"[ROUTER] Saltando a {candidate} (No está disponible hoy Lunes/Miércoles).")
            continue
    
        # Filtro Mariela: Si es Mariela, debe ser comuna de prioridad
        if candidate == MARIELA_ARRIAGADA:
            if not any(c in norm_comuna for c in mariela_comunas):
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
    return ERIKA_GARRIDO

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
        p_int = safe_int_conversion(property_code)
        logger.info(f"[ROUTER] Buscando responsable para propiedad: '{property_code}' (int: {p_int})")
        
        # 1. Búsqueda Directa Exacta (Prioridad 1)
        exact_query = {
            "$or": [
                {"codigo": property_code},
                {"codigo": p_int},
                {"codigo_pi": property_code},
                {"codigo_pi": p_int},
                {"codigo_mercadolibre": property_code},
                {"codigo_mercadolibre": p_int},
                {"codigo_yapo": property_code},
                {"codigo_yapo": p_int},
                {"codigo_internacional": property_code},
                {"codigo_internacional": p_int},
                {"publicaciones.codigo_internacional": property_code},
                {"publicaciones.codigo_internacional": p_int},
                {"publicaciones.yapo.codigo_yapo": property_code},
                {"publicaciones.yapo.codigo_yapo": p_int},
                {"publicaciones.portal_inmobiliario.codigo_pi": property_code},
                {"publicaciones.portal_inmobiliario.codigo_pi": p_int},
                {"publicaciones.procasa.url_procasa": {"$regex": re.escape(str(property_code)), "$options": "i"}},
                {"publicaciones.yapo.url_yapo": {"$regex": re.escape(str(property_code)), "$options": "i"}},
                {"publicaciones.portal_inmobiliario.url_mercado_libre": {"$regex": re.escape(str(property_code)), "$options": "i"}},
            ]
        }
        prop = db[Config.COLLECTION_NAME].find_one(exact_query)

        # 2. Búsqueda por URL si no se encontró y si parece un ID largo o URL
        if not prop and len(str(property_code)) >= 5:
            url_query = {
                "$or": [
                    {"codigo_pi": property_code.replace("MLC", "") if isinstance(property_code, str) else property_code},
                    {"publicaciones.portal_inmobiliario.codigo_pi": property_code},
                    {"publicaciones.portal_inmobiliario.url_pi": {"$regex": re.escape(str(property_code)), "$options": "i"}},
                    {"publicaciones.portal_inmobiliario.url_mercado_libre": {"$regex": re.escape(str(property_code)), "$options": "i"}},
                    {"publicaciones.toctoc.url_toctoc": {"$regex": re.escape(str(property_code)), "$options": "i"}},
                    {"publicaciones.toctoc.enlace": {"$regex": re.escape(str(property_code)), "$options": "i"}},
                    {"publicaciones.yapo.url_yapo": {"$regex": re.escape(str(property_code)), "$options": "i"}},
                    {"publicaciones.procasa.url_procasa": {"$regex": re.escape(str(property_code)), "$options": "i"}},
                    {"toctoc.enlace": {"$regex": re.escape(str(property_code)), "$options": "i"}},
                    {"url_yapo": {"$regex": re.escape(str(property_code)), "$options": "i"}}
                ]
            }
            prop = db[Config.COLLECTION_NAME].find_one(url_query)

        # 3. Respaldo: Si property_code parece link, extraer IDs y re-buscar
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
                prop = db[Config.COLLECTION_NAME].find_one({
                    "$or": [
                        {"codigo": {"$in": potential_ids}},
                        {"codigo": {"$in": [safe_int_conversion(x) for x in potential_ids]}},
                        {"codigo_internacional": {"$in": potential_ids}},
                        {"publicaciones.codigo_internacional": {"$in": potential_ids}},
                        {"publicaciones.yapo.codigo_yapo": {"$in": potential_ids}},
                        {"publicaciones.portal_inmobiliario.codigo_pi": {"$in": potential_ids}}
                    ]
                })
    
    region = ""
    norm_region = ""
    norm_comuna = normalize_text(comuna) if comuna else ""
    norm_exec = ""
    original_executive = ""
    phone = None

    if not prop and property_code:
        logger.warning(f"[ROUTER] Propiedad {property_code} NO encontrada en {Config.COLLECTION_NAME}. Usando fallback.")
        target_executive_name = UNASSIGNED_LABEL
        
        # --- Alerta de Propiedad Faltante (Solicitado por usuario) ---
        try:
            from .storage import save_pending_notification
            # Notificar a Pablo Galleguillos (+56983219804)
            lead_info = f" del cliente {lead_name} ({lead_phone})" if lead_phone else ""
            alert_payload = {
                "target_phone": "+56983219804",
                "target_name": "Pablo Galleguillos",
                "property_code": property_code,
                "lead_type": "MISSING_PROPERTY_ALERT",
                "nombre": "Sistema de Alertas",
                "last_message": f"⚠️ ATENCIÓN: Se recibió un lead{lead_info} para la propiedad '{property_code}', pero este código NO existe en la colección 'universo_cartera'. Es probable que la base de datos esté desactualizada."
            }
            save_pending_notification(alert_payload)
            logger.info(f"[ROUTER] Alerta de propiedad faltante programada para el administrador.")
        except Exception as e_alert:
            logger.error(f"[ROUTER] Error al programar alerta de propiedad faltante: {e_alert}")

    elif not prop:
        target_executive_name = ""
    else:
        original_executive = prop.get("ejecutivo", "")
        logger.info(f"[ROUTER] Propiedad encontrada. Ejecutivo original en ficha: '{original_executive}'")
        region = prop.get("region", "")
        prop_comuna = prop.get("comuna", "")
        
        norm_region = normalize_text(region)
        norm_comuna = normalize_text(prop_comuna) if prop_comuna else norm_comuna
        norm_exec = normalize_text(original_executive)

        target_executive_name = original_executive # Default: El que viene en la ficha

    # REGLA 0: Si el ejecutivo de la ficha ya es uno de los nuestros, se queda con él (Lo lógico)
    # PERO: Respetamos si está de vacaciones y redirigimos a su reemplazo
    our_team = [ERIKA_GARRIDO, MARIELA_ARRIAGADA, SUSANA_ENSIGNIA, RAQUEL_CHENEAUX, PAULA_MORALES, ROCIO_ALIAGA]
    
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
             
        # 1.1 RM -> Round Robin entre los 4 (Con filtro Mariela interno)
        if "metropolitana" in norm_region or "xiii" in norm_region:
            target_executive_name = get_next_round_robin_executive(norm_comuna)
        
        # 1.2 Región del Maule (VII) -> Paula Morales
        elif "maule" in norm_region or "vii" in norm_region:
             logger.info(f"[ROUTER] Propiedad en Maule. Asignando a {PAULA_MORALES}")
             target_executive_name = get_active_executive(PAULA_MORALES, norm_comuna)
             
        # 1.3 Ñuble (XVI), Bío Bío (VIII) o Valparaíso (V) -> Rocío Aliaga
        elif any(r in norm_region for r in ["nuble", "bio", "xvi", "viii", "valparaiso", "quinta"]) or " v " in f" {norm_region} ":
             logger.info(f"[ROUTER] Propiedad en Ñuble/BioBio/Valparaíso. Asignando a {ROCIO_ALIAGA}")
             target_executive_name = get_active_executive(ROCIO_ALIAGA, norm_comuna)

        # 1.4 Otras Regiones (Físicas/Norte/Otras) -> Erika Garrido
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
    # The property card in universo_cartera has 'email_ejecutivo' and 'movil_ejecutivo' sometimes.
    if not phone and target_executive_name == original_executive:
         phone = prop.get("movil_ejecutivo") or prop.get("fono_ejecutivo")

    # Final Safety Check: Asegurar que el el final no esté de vacaciones (por si se coló por otra regla)
    target_executive_name = get_active_executive(target_executive_name, norm_comuna)

    # FALLBACK DE EMERGENCIA: Si no hay teléfono o es No Asignado, asignamos a Round Robin
    # PERO: Respetamos la decisión de dejarlo como pendiente si la propiedad NO EXISTE (Rule refinement)
    if target_executive_name == UNASSIGNED_LABEL or target_executive_name == "":
        if not prop:
            # PROPIEDAD DESCONOCIDA: No asignar automáticamente. Dejar que el Admin lo vea.
            logger.warning(f"[ROUTER] Propiedad '{property_code}' desconocida. Dejando lead como '{UNASSIGNED_LABEL}'.")
            return UNASSIGNED_LABEL, None
            
        logger.warning(f"[ROUTER] Fallback: Sin ejecutivo válido. Asignando a Round Robin (RM).")
        target_executive_name = get_next_round_robin_executive("")
        phone = get_executive_phone(target_executive_name)
        
    elif not phone:
        # NUEVA REGLA DE INTEGRIDAD: Si sabemos quién es, pero no está su teléfono, SE LO QUEDA IGUAL,
        # solo que el envío de Whatsapp fallará más abajo (y quedará guardado silenciosamente).
        logger.warning(f"[ROUTER] El ejecutivo '{target_executive_name}' no tiene teléfono en DB. Se mantiene el Lead pero no recibirá Whatsapp.")

    return target_executive_name, phone, assignment_type

def format_whatsapp_template(lead_data: Dict[str, Any], executive_name: str, property_code: str, is_new_assignment: bool = True) -> str:
    """
    Formats the WhatsApp message to be sent to the executive.
    """
    # Importante: esta función puede llamarse desde contextos async.
    # Evitamos cualquier acceso sync a Mongo aquí para no bloquear event loop.
    prop_inline = lead_data.get("property_data", {}) if isinstance(lead_data, dict) else {}
    comuna = (
        lead_data.get("comuna")
        or prop_inline.get("comuna")
        or "N/D"
    )
    region = (
        lead_data.get("region")
        or prop_inline.get("region")
        or "N/D"
    )
    operacion = (
        lead_data.get("operacion")
        or prop_inline.get("operacion")
        or "Operación no especificada"
    )
    

    
    nombre_cliente = lead_data.get("nombre", "Cliente Desconocido")
    fono_cliente = lead_data.get("phone", "No disponible")
    email_cliente = lead_data.get("email", "No disponible")
    mensaje_usuario = lead_data.get("last_message", "Interesado en esta propiedad")

    # Header dinámico según si es nuevo o seguimiento
    header = "🚀 *¡Nuevo Lead Asignado!*" if is_new_assignment else "💬 *Actualización de Lead*"
    
    # Si es seguimiento, enfatizamos que ya tiene dueño
    contexto_extra = ""
    if not is_new_assignment:
        contexto_extra = f"⚠️ _Este cliente ya está asignado a ti._\n"

    crm_url = "https://procasa-chatbot-yr8d.onrender.com/"

    template = (
        f"{header}\n\n"
        f"Hola {executive_name}, se ha asignado un nuevo lead a tu gestión. "
        f"Realiza la gestión a la brevedad para maximizar la conversión. ⚡\n\n"
        f"🏠 *Propiedad*: {property_code} | {operacion}\n"
        f"📍 *Ubicación*: {comuna}, {region}\n"
        f"👤 *Cliente*: {nombre_cliente}\n"
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
    header = f"🚀 *{len(leads_list)} Nuevos Leads Asignados*"
    
    leads_details = ""
    for i, lead in enumerate(leads_list, 1):
        # Los items vienen como docs de pending_notifications: {"lead_data": {...}}
        # Navegamos al nivel real con fallback al item directamente
        ld = lead.get("lead_data") if isinstance(lead.get("lead_data"), dict) else lead
        nombre = ld.get("nombre") or ld.get("prospecto_nombre") or "Cliente"
        p_code = ld.get("property_code") or "S/N"
        canal = ld.get("canal") or ld.get("source") or ld.get("origen") or "Directo"
        
        leads_details += f"\n{i}. *{nombre}* - Prop: {p_code} ({canal})"

    crm_url = "https://procasa-chatbot-yr8d.onrender.com/"

    template = (
        f"{header}\n\n"
        f"Hola {executive_name}, tienes {len(leads_list)} nuevos leads esperando tu gestión:\n"
        f"{leads_details}\n\n"
        f"🔗 *Gestionar todos en el CRM*:\n{crm_url}\n\n"
        f"⚡ _Realiza la gestión a la brevedad para no perder la oportunidad._\n"
        f"¡Mucho éxito! 🚀"
    )
    return template
