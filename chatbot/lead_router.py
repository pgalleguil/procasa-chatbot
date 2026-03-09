
import logging
import random
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

# --- MODO VACACIONES ---
# Agregue aquí los nombres de los ejecutivos que no están disponibles
EXECUTIVES_ON_VACATION = []

# Mapeo de reemplazos para asignaciones directas (fuera de Round Robin)
VACATION_REPLACEMENTS = {
    ERIKA_GARRIDO: RAQUEL_CHENEAUX
}

def get_active_executive(name: str) -> str:
    """Retorna el reemplazo si el ejecutivo está en vacaciones, de lo contrario retorna el mismo nombre."""
    if name in EXECUTIVES_ON_VACATION:
        replacement = VACATION_REPLACEMENTS.get(name)
        if replacement:
            logger.info(f"[VACATION] Redirigiendo asignación de {name} a su reemplazo: {replacement}")
            return replacement
        # Si no hay reemplazo definido pero está en vacaciones, el Round Robin o fallback se encargará
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
    return get_active_executive(ERIKA_GARRIDO)

from .constants import UNASSIGNED_LABEL

def get_executive_phone(executive_name: str) -> Optional[str]:
    """
    Look up executive phone in 'usuarios' collection (field 'telefono' or 'movil').
    Uses robust normalization for matching.
    """
    if not executive_name or executive_name == UNASSIGNED_LABEL:
        return None

    db = get_db()
    # 1. Intento directo exacto
    user = db["usuarios"].find_one({"nombre": executive_name})
    
    # 2. Si falla, búsqueda robusta por normalización
    if not user:
        norm_target = normalize_text(executive_name)
        all_users = list(db["usuarios"].find({}, {"nombre": 1, "telefono": 1, "tel": 1, "movil": 1}))
        for candidate in all_users:
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
                
    if user:
        phone = user.get("telefono") or user.get("tel") or user.get("movil")
        if phone:
            logger.info(f"[LOOKUP] Usuario encontrado: {user.get('nombre')} | Tel: {phone}")
            return str(phone).strip()
    
    logger.warning(f"[LOOKUP] No se encontró usuario '{executive_name}' en colección 'usuarios'.")
    return None

def find_responsible_executive(property_code: str) -> Tuple[str, Optional[str]]:
    """
    Determines the responsible executive based on property rules.
    Returns (Executive Name, Executive Phone).
    """
    db = get_db()
    
    # BUSQUEDA ROBUSTA (String o Int)
    p_int = safe_int_conversion(property_code)
    logger.info(f"[ROUTER] Buscando responsable para propiedad: '{property_code}' (int: {p_int})")
    
    query = {
        "$or": [
            {"codigo": property_code},
            {"codigo": p_int},
            {"codigo": f"'{property_code}'"},  # Handle literal quotes like '12345'
            {"codigo_mercadolibre": property_code},
            {"codigo_mercadolibre": p_int},
            {"codigo_yapo": property_code},
            {"codigo_yapo": p_int}
        ]
    }
    prop = db["universo_obelix"].find_one(query)
    
    norm_region = ""
    norm_comuna = ""
    norm_exec = ""
    original_executive = ""
    phone = None

    if not prop:
        logger.warning(f"[ROUTER] Propiedad {property_code} NO encontrada en universo_obelix. Usando fallback.")
        target_executive_name = UNASSIGNED_LABEL
    else:
        original_executive = prop.get("ejecutivo", "")
        logger.info(f"[ROUTER] Propiedad encontrada. Ejecutivo original en ficha: '{original_executive}'")
        region = prop.get("region", "")
        comuna = prop.get("comuna", "")
        
        norm_region = normalize_text(region)
        norm_comuna = normalize_text(comuna)
        norm_exec = normalize_text(original_executive)

        target_executive_name = original_executive # Default: El que viene en la ficha

    # REGLA 0: Si el ejecutivo de la ficha ya es uno de los nuestros, se queda con él (Lo lógico)
    # PERO: Respetamos si está de vacaciones y redirigimos a su reemplazo
    our_team = [ERIKA_GARRIDO, MARIELA_ARRIAGADA, SUSANA_ENSIGNIA, RAQUEL_CHENEAUX, PAULA_MORALES, ROCIO_ALIAGA]
    
    matched_member = next((member for member in our_team if normalize_text(member) in norm_exec), None)
    
    if matched_member:
        logger.info(f"[ROUTER] Propiedad ya pertenece a alguien del equipo ({original_executive} -> normalizado a {matched_member}). Verificando disponibilidad.")
        target_executive_name = get_active_executive(matched_member)
    
    # REGLA 0.1: Si el ejecutivo es el Supervisor (Pablo), redirigimos al Round Robin del equipo
    #elif "pablo galleguillos" in norm_exec:
    #    logger.info(f"[ROUTER] Propiedad de Supervisor ({original_executive}). Derivando al equipo (Round Robin).")
    #    target_executive_name = get_next_round_robin_executive(norm_comuna)

    # REGLA 1: Jorge Pablo Caro (Distribución Especial)
    elif "jorge pablo caro" in norm_exec:
        # 1.1 RM -> Round Robin entre los 4 (Con filtro Mariela interno)
        if "metropolitana" in norm_region or "xiii" in norm_region:
            target_executive_name = get_next_round_robin_executive(norm_comuna)
        
        # 1.2 Región del Maule (VII) -> Paula Morales
        elif "maule" in norm_region or "vii" in norm_region:
             logger.info(f"[ROUTER] Propiedad de JPC en Maule. Asignando a {PAULA_MORALES}")
             target_executive_name = get_active_executive(PAULA_MORALES)
             
        # 1.3 Ñuble (XVI) o Bío Bío (VIII) -> Rocío Aliaga
        elif any(r in norm_region for r in ["nuble", "bio", "xvi", "viii"]):
             logger.info(f"[ROUTER] Propiedad de JPC en Ñuble/BioBio. Asignando a {ROCIO_ALIAGA}")
             target_executive_name = get_active_executive(ROCIO_ALIAGA)

        # 1.4 Otras Regiones (Físicas/Norte/Otras) -> Erika Garrido
        else:
            logger.info(f"[ROUTER] Propiedad de JPC en otra región ({region}). Asignando a {ERIKA_GARRIDO}")
            target_executive_name = get_active_executive(ERIKA_GARRIDO)
            
    else:
        # Para cualquier otro ejecutivo (externo o no contemplado), asegurarnos de usar solo Nombre + Apellido (2 palabras)
        words = target_executive_name.split()
        if len(words) > 2:
            target_executive_name = f"{words[0]} {words[1]}"
            logger.info(f"[ROUTER] Acortando nombre de ejecutivo: '{original_executive}' a '{target_executive_name}'")
    
    # Get phone for the determined executive
    phone = get_executive_phone(target_executive_name)
    
    # If we couldn't find the phone for the target, but we had an original executive with phone in the property card?
    # The property card in universo_obelix has 'email_ejecutivo' and 'movil_ejecutivo' sometimes.
    if not phone and target_executive_name == original_executive:
         phone = prop.get("movil_ejecutivo") or prop.get("fono_ejecutivo")

    # Final Safety Check: Asegurar que el el final no esté de vacaciones (por si se coló por otra regla)
    target_executive_name = get_active_executive(target_executive_name)

    # FALLBACK DE EMERGENCIA: Si no hay teléfono o es No Asignado, asignamos a Round Robin
    # PERO: Respetamos la decisión de dejarlo como pendiente si la propiedad NO EXISTE (Rule refinement)
    if not phone or target_executive_name == UNASSIGNED_LABEL:
        if not prop:
            # PROPIEDAD DESCONOCIDA: No asignar automáticamente. Dejar que el Admin lo vea.
            logger.warning(f"[ROUTER] Propiedad '{property_code}' desconocida. Dejando lead como '{UNASSIGNED_LABEL}'.")
            return UNASSIGNED_LABEL, None
            
        logger.warning(f"[ROUTER] Fallback: Ejecutivo '{target_executive_name}' sin teléfono. Asignando a Round Robin (RM).")
        target_executive_name = get_next_round_robin_executive("")
        phone = get_executive_phone(target_executive_name)

    return target_executive_name, phone

def format_whatsapp_template(lead_data: Dict[str, Any], executive_name: str, property_code: str, is_new_assignment: bool = True) -> str:
    """
    Formats the WhatsApp message to be sent to the executive.
    """
    db = get_db()
    prop = db["universo_obelix"].find_one({"codigo": property_code}) or {}
    
    comuna = prop.get("comuna", "N/D")
    region = prop.get("region", "N/D")
    operacion = prop.get("operacion", "Operación no especificada")
    
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
        nombre = lead.get("nombre") or lead.get("prospecto_nombre") or "Cliente Desconocido"
        p_code = lead.get("property_code") or "S/N"
        canal = lead.get("canal") or lead.get("source") or "Directo"
        
        leads_details += f"\n{i}. *{nombre}* - Casa: {p_code} ({canal})"

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
