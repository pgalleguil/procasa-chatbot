
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

from .constants import CHILE_TZ

# --- CONFIGURACIÓN DE HORARIOS ---
BUSINESS_START_HOUR = 9
BUSINESS_END_HOUR = 18  # Extendido para pruebas
BUSINESS_DAYS = [0, 1, 2, 3, 4] # Lunes a Viernes

# Constants for specific executives
JORGE_PABLO_CARO = "Jorge Pablo Caro"
MARIELA_ARRIAGADA = "Mariela Arriagada"
SUSANA_ENSIGNIA = "Susana Ensignia"
ERIKA_GARRIDO = "Erika Garrido"

# Phone mapping (This should ideally be in a DB or Config, but hardcoding for now as requested/implied)
# NOTE: You will need to fill in real numbers or ensure they are in the DB users collection.
# For now I will assume the 'ejecutivo' field in DB has the name, and we need to look up their phone
# in the 'usuarios' collection or similar.
# If names don't match exactly, we might need a mapping.

def normalize_text(text: str) -> str:
    """Normalize text for comparison (lowercase, strip accents)."""
    if not text:
        return ""
    replacements = (
        ("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"),
        ("ñ", "n"), ("ü", "u")
    )
    text = text.lower().strip()
    for a, b in replacements:
        text = text.replace(a, b)
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

def get_executive_phone(executive_name: str) -> Optional[str]:
    """
    Look up executive phone in 'usuarios' collection (field 'telefono' or 'movil').
    """
    db = get_db()
    # 1. Prioridad: Buscar en la colección 'usuarios'
    user = db["usuarios"].find_one({"nombre": executive_name})
    
    if not user:
        import re
        user = db["usuarios"].find_one({"nombre": {"$regex": f"^{re.escape(executive_name)}$", "$options": "i"}})
        
    if user:
        # Buscamos el campo 'telefono' (según sugeriste), 'tel' o 'movil'
        phone = user.get("telefono") or user.get("tel") or user.get("movil")
        if phone:
            logger.info(f"[LOOKUP] Usuario encontrado: {user.get('nombre')} | Tel: {phone}")
            return str(phone).strip()
    
    logger.warning(f"[LOOKUP] No se encontró usuario '{executive_name}' en colección 'usuarios'.")

    # 2. Respaldo: Números encontrados hoy (mientras los pasas a la base de datos)
    fallbacks = {
        MARIELA_ARRIAGADA: "+56991788250",
        SUSANA_ENSIGNIA: "+56939125978"
    }
    return fallbacks.get(executive_name)

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
            {"codigo_mercadolibre": property_code},
            {"codigo_mercadolibre": p_int},
            {"codigo_yapo": property_code},
            {"codigo_yapo": p_int}
        ]
    }
    prop = db["universo_obelix"].find_one(query)
    
    if not prop:
        logger.warning(f"[ROUTER] Propiedad {property_code} NO encontrada en universo_obelix (ni en portales).")
        # FALLBACK ROBUSTO: Retornar Administrativo o Default para no romper el flujo
        return "Sin Asignar", "+56900000000" 

    original_executive = prop.get("ejecutivo", "")
    logger.info(f"[ROUTER] Propiedad encontrada. Ejecutivo original en ficha: '{original_executive}'")
    region = prop.get("region", "")
    comuna = prop.get("comuna", "")
    
    norm_region = normalize_text(region)
    norm_comuna = normalize_text(comuna)
    norm_exec = normalize_text(original_executive)

    target_executive_name = original_executive # Default to the one in DB

    # Logic for Jorge Pablo Caro
    if "jorge pablo caro" in norm_exec:
        # Rule 1: XIII Region Metropolitana
        if "metropolitana" in norm_region or "xiii" in norm_region:
            priority_comunas = ["nunoa", "providencia", "santiago", "santiago centro", "macul"]
            
            if any(c in norm_comuna for c in priority_comunas):
                target_executive_name = MARIELA_ARRIAGADA
            else:
                # Distribute between Susana and Erika
                # Simple random distribution for now
                target_executive_name = random.choice([SUSANA_ENSIGNIA, ERIKA_GARRIDO])
                
        # Rule 2: V Region de Valparaiso
        elif "valparaiso" in norm_region or "v region" in norm_region:
             target_executive_name = ERIKA_GARRIDO
    
    # Get phone for the determined executive
    phone = get_executive_phone(target_executive_name)
    
    # If we couldn't find the phone for the target, but we had an original executive with phone in the property card?
    # The property card in universo_obelix has 'email_ejecutivo' and 'movil_ejecutivo' sometimes.
    if not phone and target_executive_name == original_executive:
         phone = prop.get("movil_ejecutivo") or prop.get("fono_ejecutivo")

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
    header = "🔔 *Nuevo Lead Asignado*" if is_new_assignment else "💬 *Cliente Escribió Nuevamente*"
    
    # Si es seguimiento, enfatizamos que ya tiene dueño
    contexto_extra = ""
    if not is_new_assignment:
        contexto_extra = f"\n⚠️ _Este cliente ya está asignado a ti._\n"

    template = (
        f"{header}\n"
        f"🏠 *Propiedad*: {property_code} | {operacion}\n"
        f"📍 *Ubicación*: {comuna}, {region}\n\n"
        f"👤 *Cliente*: {nombre_cliente}\n"
        f"📱 *Teléfono*: {fono_cliente}\n"
        f"✉️ *Email*: {email_cliente}\n"
        f"{contexto_extra}"
        f"📝 *Comentario*: {mensaje_usuario}\n\n"
        f"🚀 _Por favor contactar a la brevedad._"
    )
    return template
