# from pymongo import MongoClient (Replaced by singleton)
from config import Config
from datetime import datetime, timedelta
from typing import Optional
import pytz
import re
import uuid
import logging
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

try:
    from chatbot.constants import CHILE_TZ
except ImportError:
    import pytz
    CHILE_TZ = pytz.timezone('Chile/Continental')

from chatbot.storage import get_db


def coerce_crm_datetime(value):
    """Normalize any CRM timestamp to an aware UTC datetime or None.

    Accepts:
    - datetime aware (returned as-is in UTC)
    - datetime naive (interpreted as UTC — MongoDB convention)
    - ISO string with Z or +offset
    - ISO string without zone (interpreted as UTC)
    - None or invalid (returns None)
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return pytz.utc.localize(value)
        return value.astimezone(pytz.utc)
    if isinstance(value, str):
        try:
            normalized = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = pytz.utc.localize(parsed)
            else:
                parsed = parsed.astimezone(pytz.utc)
            return parsed
        except (TypeError, ValueError):
            return None
    return None


# Module-level constants for CRM event display (defined before any function).
TYPE_LABELS = {
    "CLICK_WHATSAPP_LEAD": "WhatsApp abierto",
    "CLICK_PHONE_LEAD": "Llamada realizada",
    "CLICK_EMAIL_LEAD": "Click Email (Lead)",
    "SEND_WA_LEAD": "WhatsApp enviado",
    "SEND_EMAIL_LEAD": "Email enviado",
    "CALL_COMPLETED_LEAD": "Llamada realizada",
    "CLICK_WHATSAPP_OWNER": "Click WhatsApp (Prop)",
    "CLICK_PHONE_OWNER": "Llamada Prop. Iniciada",
    "CLICK_EMAIL_OWNER": "Click Email (Prop)",
    "SEND_WA_OWNER": "WhatsApp Enviado (Prop)",
    "SEND_EMAIL_OWNER": "Email Enviado (Prop)",
    "STATUS_CHANGE": "Estado actualizado",
    "HUMAN_NOTE": "Gestión manual",
    "ASSIGNMENT": "Lead asignado",
    "GESTION_LOG": "Gestión manual",
    "ALERT_SENT": "Alerta Enviada",
    "MANUAL_ENTRY": "Ingreso Manual",
}
TELEMETRY_LABEL_TYPES = frozenset(TYPE_LABELS.keys())


def _after_hours_label(assigned_raw, *, sla_started_raw=None, has_real_management=False):
    """Display factual assignment and SLA-start dates for after-hours cycles."""
    dt = coerce_crm_datetime(assigned_raw)
    if not dt or has_real_management:
        return format_relative_time(assigned_raw)
    from chatbot.constants import BUSINESS_DAYS, BUSINESS_START_HOUR, BUSINESS_END_HOUR
    local = dt.astimezone(CHILE_TZ)
    now_local = datetime.now(CHILE_TZ)
    is_after_hours = (
        local.weekday() not in BUSINESS_DAYS
        or local.hour >= BUSINESS_END_HOUR
        or local.hour < BUSINESS_START_HOUR
    )
    if not is_after_hours:
        return format_relative_time(assigned_raw)
    if local.date() == now_local.date() - timedelta(days=1) and local.hour >= BUSINESS_END_HOUR:
        assignment_text = "anoche"
    elif local.date() == now_local.date():
        assignment_text = "hoy"
    else:
        assignment_text = local.strftime("%d/%m/%Y %H:%M")
    sla_dt = coerce_crm_datetime(sla_started_raw) or dt
    sla_local = sla_dt.astimezone(CHILE_TZ)
    if sla_local.date() == now_local.date():
        sla_text = f"hoy {sla_local.strftime('%H:%M')}"
    elif sla_local.date() > now_local.date():
        sla_text = f"el {sla_local.strftime('%d/%m/%Y')} a las {sla_local.strftime('%H:%M')}"
    else:
        sla_text = f"el {sla_local.strftime('%d/%m/%Y')} a las {sla_local.strftime('%H:%M')}"
    return f"{assignment_text} - SLA iniciado {sla_text}"


def format_relative_time(dt_obj):
    if isinstance(dt_obj, str):
        try:
            # Handle Z suffix and +00:00 as UTC
            normalized = dt_obj.replace('Z', '+00:00')
            dt_obj = datetime.fromisoformat(normalized)
        except:
            return "S/I"
    
    if not dt_obj or dt_obj == datetime.min: return "S/I"
    
    chile_tz = pytz.timezone('Chile/Continental')
    now = datetime.now(chile_tz)
    
    # MongoDB returns naive datetimes representing UTC timestamps.
    # Never localize naive datetimes as CLT — first interpret as UTC.
    if dt_obj.tzinfo is None:
        dt_obj = pytz.utc.localize(dt_obj)
    
    # Convert to CLT for display so now-dt_obj works in local time
    dt_obj_cl = dt_obj.astimezone(chile_tz)
    diff = now - dt_obj_cl
    seconds = diff.total_seconds()
    
    if seconds < 0:
        future_sec = abs(seconds)
        future_hours = int(future_sec // 3600)
        future_minutes = int((future_sec % 3600) // 60)
        future_time = dt_obj_cl.strftime("%H:%M")
        if future_hours >= 24:
            future_day = dt_obj_cl.strftime("%d/%m")
            return f"Programado para {future_day} a las {future_time}"
        elif future_hours > 0:
            return f"Programado para hoy a las {future_time}"
        else:
            return f"Programado en {future_minutes}m"
    
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    
    if days > 0: return f"Hace {days}d {hours}h {minutes}m"
    elif hours > 0: return f"Hace {hours}h {minutes}m"
    elif minutes > 0: return f"Hace {minutes}m"
    else: return "Ahora"


def format_duration_minutes(total_minutes):
    """Render a duration with hours and remaining minutes when useful."""
    total_minutes = max(0, int(total_minutes))
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours} h" + (f" {minutes} min" if minutes else "")
    return f"{minutes} min"


def format_relative_compact(dt_obj):
    """Short assignment age for the list, preserving useful minutes."""
    text = format_relative_time(dt_obj)
    if text.startswith("Hace "):
        parts = text[5:].split()
        if parts and parts[0].endswith("d"):
            days = int(parts[0][:-1])
            hours = parts[1][:-1] if len(parts) > 1 and parts[1].endswith("h") else "0"
            minutes = parts[2][:-1] if len(parts) > 2 and parts[2].endswith("m") else "0"
            day_label = "día" if days == 1 else "días"
            result = f"Hace {days} {day_label}"
            if int(hours) > 0:
                result += f" {hours} h"
            return result
        if parts and parts[0].endswith("h"):
            hours = parts[0][:-1]
            minutes = parts[1][:-1] if len(parts) > 1 and parts[1].endswith("m") else "0"
            return f"Hace {hours} h" + (f" {minutes} min" if int(minutes) > 0 else "")
        if parts and parts[0].endswith("m"):
            return f"Hace {parts[0][:-1]} min"
    return text

# --- HELPER: Datos de Propiedad ---
def select_owner_phone(prop, owner):
    """Return the most usable owner phone stored by Prop360.

    Prop360 often keeps an obsolete or concatenated value in ``telefono`` or
    ``fono_1`` while a clean mobile number is present in another ``fono_*``
    field.  This data is used by both the call and WhatsApp actions in the CRM,
    so select by validity rather than by the historical field order.
    """
    from chatbot.phone_utils import normalize_phone_strict

    candidates = []
    for field in ("telefono", "fono_1", "fono_2", "fono_3", "movil_propietario"):
        candidates.append(owner.get(field))
    for field in ("movil_propietario", "fono_propietario"):
        candidates.append(prop.get(field))

    for phones in (owner.get("telefonos"), prop.get("telefonos")):
        if isinstance(phones, (list, tuple)):
            candidates.extend(phones)

    fallback = next((str(phone).strip() for phone in candidates if phone and str(phone).strip()), "S/I")
    valid_phones = []
    seen = set()
    for candidate in candidates:
        normalized = normalize_phone_strict(str(candidate or ""))
        if normalized and normalized not in seen:
            seen.add(normalized)
            valid_phones.append(normalized)

    if not valid_phones:
        return fallback

    # The CRM's owner contact buttons include WhatsApp, for which a Chilean
    # mobile is the most useful choice.  Do not make fono_3 a hard-coded rule:
    # it wins only when it is a valid mobile number.
    return next((phone for phone in valid_phones if phone.startswith("+569")), valid_phones[0])


def get_real_property_data(db, codigo_propiedad):
    """Resolve property detail data from the canonical Prop360 collection.

    Prop360 stores owner, address and operation data in nested documents; keep
    the flat shape expected by the CRM template while preserving legacy fallback
    field names for older records.
    """
    if not codigo_propiedad or codigo_propiedad == "S/N":
        return None
    collection_name = getattr(Config, "PROPERTY_COLLECTION_NAME", "universo_cartera_prop360")
    prop = db[collection_name].find_one({"codigo": str(codigo_propiedad)})
    if not prop:
        return None
    owner = prop.get("datos_propietario") or {}
    location = prop.get("ubicacion") or {}
    summary = prop.get("resumen") or {}
    operation = prop.get("tipo_operacion") or {}
    metadata = prop.get("metadata") or {}
    tipo = prop.get("tipo") or operation.get("tipo") or metadata.get("tipo_propiedad") or "Propiedad"
    venta = bool(operation.get("venta"))
    arriendo = bool(operation.get("arriendo"))
    operation_label = "Venta y arriendo" if venta and arriendo else ("Venta" if venta else ("Arriendo" if arriendo else prop.get("operacion", "Venta")))
    calle = prop.get("calle") or location.get("calle") or ""
    numero = prop.get("numeracion") or prop.get("numero") or location.get("numero") or ""
    precio_uf = prop.get("precio_uf") or summary.get("precio_uf")
    if precio_uf is None:
        precio_uf = (operation.get("precio_venta") or {}).get("precio_uf")
    telefono = select_owner_phone(prop, owner)
    email = owner.get("email") or prop.get("email_propietario") or "S/I"
    nombre = owner.get("nombre") or prop.get("nombre_propietario") or "No registrado"
    comuna = prop.get("comuna") or location.get("comuna") or ""
    region = prop.get("region") or location.get("region") or ""
    return {
        "codigo": prop.get("codigo"),
        "tipo": tipo,
        "operacion": operation_label,
        "precio_uf": precio_uf if precio_uf is not None else 0,
        "comuna": comuna,
        "region": region,
        "calle": calle,
        "numeracion": numero,
        "direccion_completa": " ".join(part for part in (calle, f"#{numero}" if numero else "") if part),
        "nombre_propietario": nombre,
        "movil_propietario": telefono,
        "email_propietario": email,
        "url": f"https://www.procasa.cl/{prop.get('codigo')}"
    }

def detect_property_code(lead):
    p = lead.get("prospecto", {})
    code = p.get("codigo")
    if code: return code
    code = lead.get("datos_propiedad", {}).get("codigo")
    if code: return code
    code = p.get("codigo_yapo")
    if code: return f"Yapo: {code}"
    code = p.get("codigo_mercadolibre")
    if code: return f"ML: {code}"
    return None

def process_chat_timeline(messages):
    processed = []
    if not messages: return []
    for msg in messages:
        role = msg.get("role", "user")
        css_class = "chat-bot" if role in ["assistant", "system"] else "user-message"
        
        ts_obj = msg.get("timestamp")
        if isinstance(ts_obj, str):
            try: ts_obj = datetime.fromisoformat(ts_obj.replace('Z', ''))
            except: ts_obj = datetime.min
        
        if ts_obj is None: ts_obj = datetime.min
            
        processed.append({
            "role": css_class, 
            "content": msg.get("content", ""),
            "timestamp": ts_obj
        })
    return processed

# --- REGISTRO DE EVENTOS (Delegado a storage) ---
from chatbot.storage import log_event # Usamos el logger centralizado
from chatbot.crm_service import CrmService
from chatbot.utils import calculate_business_minutes
from chatbot.constants import PipelineStage, InteractionType, UNASSIGNED_LABEL, CHILE_TZ
from chatbot.lead_temperature import COLD, HOT
from chatbot.crm_permissions import can_administer_leads

CRM_HOT_QUERY = {"lead_temperature_effective": HOT}
CRM_COLD_QUERY = {"lead_temperature_effective": COLD}


def normalize_crm_temperature(value):
    """Return the only three temperature scopes accepted by the CRM."""
    normalized = str(value or "Todos").strip().upper()
    if normalized == HOT:
        return HOT
    if normalized == COLD:
        return COLD
    return "Todos"

CRM_STAGE_GROUPS = {
    "NEW": [PipelineStage.NEW, "nuevo", "new"],
    "GESTION": [
        PipelineStage.CONTACTED, PipelineStage.INTERESTED, PipelineStage.OFFER,
        PipelineStage.NEGOTIATION, "gestion", "contacted",
    ],
    "VISITA": [PipelineStage.VISIT_SCHEDULED, PipelineStage.VISIT_DONE, "visita"],
    "CERRADO": [PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST, "cerrado"],
}


def crm_stage_group(stage):
    """Python mirror used for invariant checks and non-Mongo consumers."""
    def normalize(value):
        return str(getattr(value, "value", value) or "").upper()

    normalized = normalize(stage or PipelineStage.NEW)
    if normalized in {normalize(value) for value in CRM_STAGE_GROUPS["NEW"]}:
        return "NEW"
    if normalized in {normalize(value) for value in CRM_STAGE_GROUPS["VISITA"]}:
        return "VISITA"
    if normalized in {normalize(value) for value in CRM_STAGE_GROUPS["CERRADO"]}:
        return "CERRADO"
    return "GESTION"


def _crm_stage_query(stages):
    """Filtra por la misma etapa efectiva que luego se muestra en el listado."""
    return {
        "$expr": {
            "$in": [
                {
                    "$ifNull": [
                        "$pipeline_stage",
                        {"$ifNull": ["$stage", {"$ifNull": ["$crm_estado", PipelineStage.NEW]}]},
                    ]
                },
                stages,
            ]
        }
    }


def _crm_management_stage_query():
    """Everything classifiable that is neither unattended, visit nor closed."""
    excluded = (
        CRM_STAGE_GROUPS["NEW"]
        + CRM_STAGE_GROUPS["VISITA"]
        + CRM_STAGE_GROUPS["CERRADO"]
    )
    effective_stage = {
        "$ifNull": [
            "$pipeline_stage",
            {"$ifNull": ["$stage", {"$ifNull": ["$crm_estado", PipelineStage.NEW]}]},
        ]
    }
    return {"$expr": {"$not": [{"$in": [effective_stage, excluded]}]}}

# log_crm_event se mantiene como alias por compatibilidad pero usa storage
def log_crm_event(phone, event_type, agent="Sistema", meta_data=None):
    # Adaptador para usar storage.log_event
    return log_event(phone, event_type, agent, meta_data)

def schedule_crm_task(phone, execute_at_str, note, agent="Sistema"):
    if not execute_at_str: return
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    # Resolver tareas previas (Audit consistency)
    db["crm_tasks"].update_many(
        {"phone": phone_clean, "status": "pending"},
        {"$set": {"status": "completed", "resolved_at": datetime.now(), "resolution": "superseded"}}
    )
    
    try: 
        execute_at = datetime.fromisoformat(execute_at_str.replace("Z", ""))
        # Asegurar timezone aware (Chile)
        if execute_at.tzinfo is None:
            execute_at = CHILE_TZ.localize(execute_at)
    except: return
    task = {
        "task_id": str(uuid.uuid4()),
        "phone": phone.replace(" ", "").replace("+", "").strip(),
        "type": "REMINDER_WHATSAPP",
        "status": "pending", "execute_at": execute_at, "created_at": datetime.now(), "note": note, "agent": agent
    }
    db["crm_tasks"].insert_one(task)

# --- 1. LISTA DE LEADS (OPTIMIZADA / BULK QUERY) ---
# Etiquetas cortas para mostrar en la columna Estado qué registró el ejecutivo.
RESULTADO_LABELS = {
    # Canonical CRM management results.
    "message_sent_waiting_response": "Mensaje enviado",
    "call_no_answer": "Sin respuesta",
    "effective_contact": "Contactado",
    "follow_up_requested": "En seguimiento",
    "visit_scheduled": "Visita agendada",
    "not_interested": "No interesado",
    "property_unavailable": "Propiedad no disponible",
    "invalid_number": "Número inválido",
    "closed_won": "Cerrado ganado",
    "closed_lost": "Cerrado perdido",
    "discarded_valid_reason": "Descartado",
    "other_explicit": "Otro",
    "requiere_seguimiento": "En Seguimiento",
    "visita_agendada": "Visita Agendada",
    "intento_fallido": "Intento Fallido",
    "lead_cerrado": "Lead Cerrado",
    # Gestión Propietario
    "autoriza_visita": "Autoriza Visita",
    "acepta_mostrar": "Acepta Mostrar",
    "confirma_disponibilidad": "Confirma Disponibilidad",
    "autoriza_con_condiciones": "Autoriza con Condiciones",
    "solo_mananas": "Solo Mañanas",
    "desde_marzo": "Desde Marzo",
    "con_24h_aviso": "Con 24h de Aviso",
    "solo_fines_semana": "Solo Finde",
    "no_acepta_visitas_aun": "No Acepta Visitas",
    "intento_contacto": "Intento de Contacto",
    "no_logra_contacto": "Sin Contacto",
    "no_responde_llamada": "No Responde Llamadas",
    "no_responde_whatsapp": "No Responde WhatsApp",
    "contactar_otro_horario": "Contactar en Otro Horario",
    "no_quiere_mostrar": "No Quiere Mostrar",
    "no_quiere_visitas": "No Quiere Visitas",
    "no_baja_precio": "No Baja Precio",
    "condiciones_no_aceptadas": "Condiciones No Aceptadas",
    "rechaza_visita": "Rechaza Visita",
    "no_regularizada": "No Regularizada",
    "doc_incompleta": "Doc. Incompleta",
    "rol_incorrecto": "Rol Incorrecto",
    "problema_titulo": "Problema con Título o Inscripción",
    "reparaciones_pendientes": "Reparaciones Pendientes",
    "propietario_retiro": "Retiró Propiedad",
    "vendio_fuera": "Ya no está Disponible",
    "no_disponible_temporal": "No Disponible Temporalmente",
    "no_autoriza_gestion": "No Autoriza Gestión",
}

CANONICAL_MANAGEMENT_RESULTS = frozenset({
    "MESSAGE_SENT_WAITING_RESPONSE", "CALL_NO_ANSWER", "EFFECTIVE_CONTACT",
    "FOLLOW_UP_REQUESTED", "VISIT_SCHEDULED", "NOT_INTERESTED",
    "PROPERTY_UNAVAILABLE", "INVALID_NUMBER", "CLOSED_WON", "CLOSED_LOST",
    "DISCARDED_VALID_REASON", "OTHER_EXPLICIT",
})


def _is_canonical_management_event(ev) -> bool:
    if not ev:
        return False
    event_type = str(ev.get("type") or "").strip().upper()
    result = str(ev.get("result") or (ev.get("meta") or {}).get("result") or "").strip().upper()
    return event_type == "CONTACT_RESULT" or result in CANONICAL_MANAGEMENT_RESULTS


def _resultado_estado_label(ev) -> Optional[str]:
    """Short label of what the executive registered, for the Estado column."""
    if not ev:
        return None
    meta = ev.get("meta") or {}
    result = str(ev.get("result") or meta.get("result") or "").strip().lower()
    label = RESULTADO_LABELS.get(result)
    if label:
        return label
    action = str(meta.get("action_label") or "").strip()
    if action:
        parts = [part.strip() for part in action.split("/") if part.strip()]
        if parts:
            return parts[-1]
    return None


async def get_crm_leads_list(filtro_estado=None, busqueda=None, ordenar_por="sla_priority",
                              user_role="agente", user_name="", ejecutivo_filter=None,
                              temperatura_filter="HOT",
                              page=1, limit=10, property_code=None):
    from chatbot.storage import get_async_db
    db = get_async_db()
    temperatura_filter = normalize_crm_temperature(temperatura_filter)
    # Todo el CRM trabaja exclusivamente con la temperatura normalizada.
    query_parts = [
        {"lead_temperature_effective": {"$in": [HOT, COLD]}},
        {"stage": {"$ne": "ARCHIVED"}},
        {"pipeline_stage": {"$ne": "ARCHIVED"}},
        {"archived_at": {"$exists": False}},
        {"_test_lead": {"$ne": True}},
        {"is_test": {"$ne": True}},
        {"synthetic": {"$ne": True}},
        {"is_synthetic": {"$ne": True}},
        {"suppressed": {"$ne": True}},
        {"stage": {"$ne": "SUPPRESSED"}},
        {"pipeline_stage": {"$ne": "SUPPRESSED"}},
        {"prospecto.nombre": {"$not": re.compile(r"^synthetic-", re.IGNORECASE)}},
    ]
    
    # --- FILTRO DE SEGURIDAD (ROL) ---
    # Si NO es admin/supervisor, solo ver sus propios leads
    if not can_administer_leads(user_role) and user_name:
        regex_name = re.compile(re.escape(user_name), re.IGNORECASE)
        query_parts.append({
            "$or": [
                {"prospecto.ejecutivo": regex_name},
                {"ejecutivo_asignado": regex_name}
            ]
        })
    # Si es admin/supervisor y eligió un ejecutivo específico
    elif ejecutivo_filter and ejecutivo_filter != "Todos":
        regex_exec = re.compile(re.escape(ejecutivo_filter), re.IGNORECASE)
        query_parts.append({
            "$or": [
                {"prospecto.ejecutivo": regex_exec},
                {"ejecutivo_asignado": regex_exec}
            ]
        })

    if busqueda and busqueda.strip():
        term = busqueda.strip()
        # Limpiar caracteres no numéricos para búsqueda exacta por teléfono
        clean_phone = re.sub(r'\D', '', term)
        if clean_phone:
            regex_phone = re.compile(re.escape(clean_phone))
            query_parts.append({"phone": regex_phone})
        else:
            regex_term = re.compile(re.escape(term), re.IGNORECASE)
            query_parts.append({"prospecto.nombre": regex_term})
    
    if property_code and property_code.strip():
        code = re.sub(r'(?i)^(prop\.?|propiedad|codigo)\s*', '', property_code.strip()).strip()
        if code:
            query_parts.append({"$or": [
                {"prospecto.codigo": code},
                {"codigo": code},
                {"property_code": code},
                {"datos_propiedad.codigo": code},
            ]})
    
    # El universo global termina aquí; el alcance activo agrega exactamente una
    # temperatura y será compartido por KPI y las cuatro tarjetas de estado.
    global_kpi_query_parts = list(query_parts)
    if temperatura_filter and temperatura_filter != "Todos":
        if temperatura_filter == "HOT":
            query_parts.append(CRM_HOT_QUERY)
        elif temperatura_filter == "COLD":
            query_parts.append(CRM_COLD_QUERY)
            
    query = {"$and": query_parts} if query_parts else {}

    # 2. El global conserva todos los leads y base_kpi_query el alcance activo;
    # ninguno incluye el filtro de estado, para poder dibujar la distribución.
    global_kpi_query = {"$and": global_kpi_query_parts} if global_kpi_query_parts else {}
    base_kpi_query = query.copy() # Con temperatura para los KPIs por etapa
    
    # --- FILTRO DE ESTADO ---
    UNASSIGNED_VALUES = [None, "", "Sin Asignar", "No asignado", "No Asignado", "Sin asignar"]
    unassigned_filter = {"$or": [{"ejecutivo_asignado": {"$in": UNASSIGNED_VALUES}}, {"ejecutivo_asignado": {"$exists": False}}]}
    effective_new_condition = {
        "$and": [
            _crm_stage_query(CRM_STAGE_GROUPS["NEW"]),
            {"$or": [
                {"lifecycle.first_valid_management_at": {"$exists": False}},
                {"lifecycle.first_valid_management_at": None},
            ]},
        ]
    }
    state_condition = None
    effective_state_ids = None
    
    if filtro_estado and filtro_estado != "Todos":
        if filtro_estado == "UNASSIGNED":
            state_condition = {"$and": [effective_new_condition, unassigned_filter]}
        elif filtro_estado in ["NEW", "nuevo"]:
            # Keep the filter aligned with the effective state rendered in
            # the list, excluding legacy NEW records with management evidence.
            state_condition = effective_new_condition
        elif filtro_estado == "GRUPO_GESTION":
            # Reassignments can leave a historical CONTACTED stage on the
            # lead while the active assignment cycle is still unmanaged. The
            # visible list correctly renders that row as Sin atender, so the
            # En gestión filter must evaluate the effective current-cycle
            # stage as well.
            raw_stage_expr = {
                "$ifNull": [
                    "$pipeline_stage",
                    {"$ifNull": ["$stage", {"$ifNull": ["$crm_estado", PipelineStage.NEW]}]},
                ]
            }
            has_cycle_expr = {"$gt": [{"$size": "$_crm_filter_cycle"}, 0]}
            cycle_management_expr = {
                "$ifNull": [
                    {"$arrayElemAt": ["$_crm_filter_cycle.first_valid_management_at", 0]},
                    None,
                ]
            }
            legacy_management_expr = {"$ifNull": ["$lifecycle.first_valid_management_at", None]}
            closed_stages = CRM_STAGE_GROUPS["CERRADO"] + ["ARCHIVED", "SUPPRESSED"]
            effective_stage_expr = {
                "$cond": [
                    has_cycle_expr,
                    {"$cond": [
                        {"$ne": [cycle_management_expr, None]},
                        {"$cond": [
                            {"$in": [raw_stage_expr, CRM_STAGE_GROUPS["NEW"]]},
                            PipelineStage.CONTACTED,
                            raw_stage_expr,
                        ]},
                        {"$cond": [
                            {"$in": [raw_stage_expr, closed_stages]},
                            raw_stage_expr,
                            PipelineStage.NEW,
                        ]},
                    ]},
                    {"$cond": [
                        {"$ne": [legacy_management_expr, None]},
                        {"$cond": [
                            {"$in": [raw_stage_expr, CRM_STAGE_GROUPS["NEW"]]},
                            PipelineStage.CONTACTED,
                            raw_stage_expr,
                        ]},
                        raw_stage_expr,
                    ]},
                ]
            }
            effective_stage_query = {
                "$expr": {
                    "$not": [{"$in": [
                        effective_stage_expr,
                        CRM_STAGE_GROUPS["NEW"] + CRM_STAGE_GROUPS["VISITA"] + CRM_STAGE_GROUPS["CERRADO"],
                    ]}]
                }
            }
            cycle_lookup = {
                "$lookup": {
                    "from": "crm_assignment_cycles",
                    "let": {"lead_id": "$_id"},
                    "pipeline": [
                        {"$match": {
                            "$expr": {"$eq": ["$lead_id", "$$lead_id"]},
                            "unassigned_at": None,
                            "notification_eligible": True,
                            "reason": {"$in": ["inbound_message", "lead_created", "manual_lead_created"]},
                            "cycle_origin": {"$in": ["inbound_message", "manual_lead"]},
                        }},
                        {"$sort": {"assigned_at": -1}},
                        {"$limit": 1},
                    ],
                    "as": "_crm_filter_cycle",
                }
            }
            effective_state_docs = await db["leads"].aggregate([
                {"$match": {"$and": list(query_parts)}},
                cycle_lookup,
                {"$match": effective_stage_query},
                {"$project": {"_id": 1}},
            ]).to_list(length=None)
            effective_state_ids = [doc["_id"] for doc in effective_state_docs]
            state_condition = {"_id": {"$in": effective_state_ids}}
        elif filtro_estado == "GRUPO_VISITA":
            state_condition = _crm_stage_query(CRM_STAGE_GROUPS["VISITA"])
        elif filtro_estado == "GRUPO_CERRADO":
            state_condition = _crm_stage_query(CRM_STAGE_GROUPS["CERRADO"])
        else:
            # Mapeo invertido para buscar por el valor del Enum o string legacy en la DB
            state_db_value = filtro_estado
            if filtro_estado == "visita": state_db_value = PipelineStage.VISIT_SCHEDULED
            elif filtro_estado == "gestion": state_db_value = PipelineStage.CONTACTED
            elif filtro_estado == "cerrado": state_db_value = PipelineStage.CLOSED_WON
            state_condition = _crm_stage_query([state_db_value])

    query_with_state_parts = list(query_parts)
    if state_condition:
        query_with_state_parts.append(state_condition)
    query_with_state = {"$and": query_with_state_parts} if query_with_state_parts else {}

    # 1. EJECUCION DE KPIs OPTIMIZADA CON $FACET (1 solo roundtrip a MongoDB)
    import time
    t_kpis = time.perf_counter()
    
    # Pipeline de $facet consolida los 7 queries en 1 sola operación en el motor de base de datos
    facet_pipeline = [
        {"$facet": {
            "global_total": [{"$match": global_kpi_query}, {"$count": "count"}],
            "total_hot": [{"$match": {"$and": [global_kpi_query, CRM_HOT_QUERY]}}, {"$count": "count"}],
            "total_cold": [{"$match": {"$and": [global_kpi_query, CRM_COLD_QUERY]}}, {"$count": "count"}],
            "scope_total": [{"$match": base_kpi_query}, {"$count": "count"}],
            "total_pagina": [{"$match": query_with_state}, {"$count": "count"}],
            "sin_asignar_global": [
                {"$match": {"$and": [global_kpi_query, effective_new_condition, unassigned_filter]}},
                {"$count": "count"}
            ],
            "sin_asignar": [
                {"$match": {"$and": [base_kpi_query, effective_new_condition, unassigned_filter]}},
                {"$count": "count"}
            ],
            "nuevo": [
                {"$match": {"$and": [base_kpi_query, effective_new_condition]}},
                {"$count": "count"}
            ],
            "gestion": [
                {"$match": {"$and": [base_kpi_query, _crm_management_stage_query()]}},
                {"$count": "count"}
            ],
            "visita": [
                {"$match": {"$and": [base_kpi_query, _crm_stage_query(CRM_STAGE_GROUPS["VISITA"])]}},
                {"$count": "count"}
            ],
            "cerrado": [
                {"$match": {"$and": [base_kpi_query, _crm_stage_query(CRM_STAGE_GROUPS["CERRADO"])]}},
                {"$count": "count"}
            ],
            # Una sola facet produce las siete cubetas diarias para las tres
            # cards; no se ejecuta una consulta por card ni por día.
            "assignment_series": [
                {"$match": global_kpi_query},
                {"$lookup": {
                    "from": "crm_assignment_cycles",
                    "let": {"lead_id": "$_id"},
                    "pipeline": [
                        {"$match": {"$expr": {"$eq": ["$lead_id", "$$lead_id"]}, "unassigned_at": None}},
                        {"$sort": {"assigned_at": -1}}, {"$limit": 1},
                    ],
                    "as": "_series_cycle",
                }},
                {"$set": {"_series_assigned_at": {"$ifNull": [
                    {"$arrayElemAt": ["$_series_cycle.assigned_at", 0]},
                    {"$ifNull": ["$lifecycle.assigned_at", "$fecha_asignacion"]},
                ]}}},
                {"$match": {"_series_assigned_at": {"$ne": None}}},
                {"$group": {
                    "_id": {"$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": {"$convert": {"input": "$_series_assigned_at", "to": "date", "onError": None, "onNull": None}}
                    }},
                    "total": {"$sum": 1},
                    "hot": {"$sum": {"$cond": [{"$eq": ["$lead_temperature_effective", "HOT"]}, 1, 0]}},
                    "cold": {"$sum": {"$cond": [{"$eq": ["$lead_temperature_effective", "COLD"]}, 1, 0]}},
                }},
                {"$sort": {"_id": 1}},
            ]
        }}
    ]

    # Desglose HOT/COLD por estado para el panel del universo Total. Se calcula
    # dentro del mismo $facet y sobre la misma base de ejecutivo/búsqueda.
    state_kpi_conditions = {
        "nuevo": _crm_stage_query(CRM_STAGE_GROUPS["NEW"]),
        "gestion": _crm_management_stage_query(),
        "visita": _crm_stage_query(CRM_STAGE_GROUPS["VISITA"]),
        "cerrado": _crm_stage_query(CRM_STAGE_GROUPS["CERRADO"]),
    }
    for state_key, state_query in state_kpi_conditions.items():
        for temperature_key, temperature_query in (("hot", CRM_HOT_QUERY), ("cold", CRM_COLD_QUERY)):
            facet_pipeline[0]["$facet"][f"{state_key}_{temperature_key}"] = [
                {"$match": {"$and": [global_kpi_query, temperature_query, state_query]}},
                {"$count": "count"},
            ]

    # ------------------------------------------------------------------
    # 3. TRAER LEADS DESDE MONGO CON PAGINACION REAL
    # ------------------------------------------------------------------
    # "Más Recientes" debe ordenar por fecha de asignación del ejecutivo.
    # La asignación comercial es la única fecha válida para este listado:
    # ciclo activo, luego lifecycle.assigned_at y finalmente fecha_asignacion.
    # -----------------------------------------------------------------------
    # 3b. ORDENAMIENTO INTELIGENTE
    # El orden varía según el filtro activo:
    #
    # Filtro HOT:
    #   1° HOT + Sin Atender (pipeline_stage=NEW) -> por fecha asignación ASC (más urgente primero)
    #   2° HOT + En Gestión/Visita -> por actividad reciente DESC
    #
    # Filtro Todos:
    #   1° HOT + Sin Atender -> por fecha asignación ASC
    #   2° HOT + gestionados -> por actividad reciente DESC
    #   3° COLD/informativos  -> por actividad reciente DESC
    #
    # Otros filtros (gestion, visita, cerrado, sin_asignar):
    #   Actividad reciente DESC (por defecto)
    # -----------------------------------------------------------------------
    # Sort normalisation: canonical names with backward-compatible aliases
    # -----------------------------------------------------------------------
    _sort_map = {
        "sla_urgente": "sla_priority",
        "recientes": "recent_assigned",
        "antiguos": "oldest_assigned",
        "antiguos_sin_atender": "oldest_unmanaged",
        "sla_por_vencer": "sla_priority",
        "mayor_sin_gestion": "oldest_unmanaged",
        "ultima_accion_antigua": "oldest_unmanaged",
        "prioridad": "sla_priority",
        # Canonical names
        "sla_priority": "sla_priority",
        "recent_assigned": "recent_assigned",
        "oldest_assigned": "oldest_assigned",
        "oldest_unmanaged": "oldest_unmanaged",
    }
    ordenar_por = _sort_map.get(ordenar_por, "recent_assigned")
    NEW_STAGES = [PipelineStage.NEW, None, "nuevo", "new", "NEW"]

    # Proyección mínima — solo campos necesarios para el listado
    PROJECTION = {
        "phone": 1,
        "prospecto.nombre": 1,
        "prospecto.ejecutivo": 1,
        "prospecto.codigo": 1,
        "prospecto.codigo_yapo": 1,
        "prospecto.codigo_mercadolibre": 1,
        "prospecto.ultimo_mensaje": 1,
        "pipeline_stage": 1,
        "stage": 1,
        "crm_estado": 1,
        "ejecutivo_asignado": 1,
        "last_event_at": 1,
        "last_message_at": 1,
        "last_message_role": 1,
        "last_message_preview": 1,
        "last_action_label": 1,
        "priority_score": 1,
        "sla_status": 1,
        "lifecycle": 1,
        "created_at": 1,
        "fecha_asignacion": 1,
        "datos_propiedad.codigo": 1,
        "lead_temperature_effective": 1,
        "_has_assigned": 1,
        "_cycle_assigned_at": 1,
        "_legacy_assigned_at": 1,
        "_temperature": 1,
        "_has_management": 1,
    }

    paginated_query = query_with_state.copy()
    page = max(int(page or 1), 1)
    offset = (page - 1) * limit

    # Canonical pipeline: look up the active assignment cycle for every lead,
    # then sort using cycle-anchored fields (assigned_at, temperature,
    # management evidence).  Unassigned leads sort last.
    canonical_pipeline = [
        {"$match": paginated_query},
        {"$lookup": {
            "from": "crm_assignment_cycles",
            "let": {"lead_id": "$_id"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$lead_id", "$$lead_id"]},
                    "unassigned_at": None,
                    "notification_eligible": True,
                    "reason": {"$in": ["inbound_message", "lead_created", "manual_lead_created"]},
                    "cycle_origin": {"$in": ["inbound_message", "manual_lead"]},
                }},
                {"$sort": {"assigned_at": -1}},
                {"$limit": 1},
            ],
            "as": "_active_cycle",
        }},
        # $arrayElemAt([], 0) does NOT reliably return null in MongoDB.
        # Use $size to detect empty arrays before accessing elements.
        {"$set": {
            "_has_cycle": {"$gt": [{"$size": "$_active_cycle"}, 0]},
        }},
        {"$set": {
            "_active_cycle": {"$cond": [
                "$_has_cycle",
                {"$arrayElemAt": ["$_active_cycle", 0]},
                None,
            ]},
        }},
        {"$set": {
            "_has_assigned": {"$cond": ["$_has_cycle", 0, 1]},
            "_cycle_assigned_at": {"$cond": [
                "$_has_cycle",
                {"$ifNull": [
                    "$_active_cycle.sla_started_at",
                    "$_active_cycle.assigned_at",
                ]},
                None,
            ]},
            "_temperature": {"$cond": [
                "$_has_cycle",
                {"$ifNull": [
                    "$_active_cycle.temperature_at_assignment",
                    "$lead_temperature_effective",
                ]},
                "$lead_temperature_effective",
            ]},
            "_has_management": {"$cond": [
                "$_has_cycle",
                {"$cond": [
                    {"$ne": [{"$ifNull": ["$_active_cycle.first_valid_management_at", None]}, None]},
                    0, 1,
                ]},
                # Fallback: use lead-level lifecycle evidence for legacy docs
                {"$cond": [
                    {"$ne": [{"$ifNull": ["$lifecycle.first_valid_management_at", None]}, None]},
                    0, 1,
                ]},
            ]},
            # Fallback assignment date for leads without a commercial cycle
            "_legacy_assigned_at": {"$ifNull": [
                {"$convert": {"input": "$lifecycle.assigned_at", "to": "date", "onError": None, "onNull": None}},
                {"$convert": {"input": "$fecha_asignacion", "to": "date", "onError": None, "onNull": None}},
            ]},
        }},
        {"$set": {
            "_assigned_at": {"$ifNull": ["$_cycle_assigned_at", "$_legacy_assigned_at"]},
        }},
        # A legacy lead can still be assigned even when it has no canonical
        # assignment-cycle document.  Keep it in the assigned side of the
        # recent/oldest assignment sorts when lifecycle.assigned_at or the
        # legacy assignment date is present.
        {"$set": {
            "_has_assigned": {"$cond": [
                {"$or": [
                    "$_has_cycle",
                    {"$ne": ["$_legacy_assigned_at", None]},
                ]},
                0,
                1,
            ]},
        }},
        # SLA overdue minutes computed in a separate $set stage.  Positive =
        # overdue, negative = in-plazo.  Assigned + unmanaged only.
        {"$set": {
            "_overdue_minutes": {"$cond": [
                "$_has_cycle",
                {"$subtract": [
                    {"$dateDiff": {
                        "startDate": {"$convert": {"input": "$_cycle_assigned_at", "to": "date", "onError": None, "onNull": None}},
                        "endDate": "$$NOW",
                        "unit": "minute",
                    }},
                    {"$cond": [{"$eq": ["$_temperature", "HOT"]}, 60, 180]},
                ]},
                None,
            ]},
        }},
    ]

    # Build sort spec from the canonical cycle fields
    _use_python_sla_sort = False
    if ordenar_por == "recent_assigned":
        _sort_spec = {"_has_assigned": 1, "_assigned_at": -1, "_id": -1}
    elif ordenar_por == "oldest_assigned":
        _sort_spec = {"_has_assigned": 1, "_assigned_at": 1, "_id": 1}
    elif ordenar_por == "sla_priority":
        # Include all leads; business-minute sort in Python groups
        # unmanaged+overdue first, managed/closed last.
        # No $match filter — parity must match scope_total.
        _use_python_sla_sort = True
        _sort_spec = {"_id": 1}
    elif ordenar_por == "oldest_unmanaged":
        # For this view, "assigned" means any valid assignment date exists
        # (cycle, lifecycle, or fecha_asignacion).  Legacy leads without
        # a commercial cycle still count as assigned if they have a date.
        # Update _has_assigned to include legacy fallback.
        canonical_pipeline.append({"$set": {
            "_has_assigned": {"$cond": [
                {"$or": [
                    "$_has_cycle",
                    {"$ne": ["$_legacy_assigned_at", None]},
                ]},
                0, 1,
            ]},
        }})
        _sort_spec = {
            "_has_management": -1,     # DESC: 1 (unmanaged) before 0 (managed)
            "_has_assigned": 1,
            "_cycle_assigned_at": 1,
            "_legacy_assigned_at": 1,
            "_id": 1,
        }
    else:
        _sort_spec = {"_has_assigned": 1, "_assigned_at": -1, "_id": -1}

    canonical_pipeline.extend([
        {"$sort": _sort_spec},
    ])

    # Pagination appended after sort for stable ordering
    canonical_pipeline.extend([
        {"$skip": offset},
        {"$limit": limit},
        {"$project": PROJECTION},
    ])
    records_pipeline = canonical_pipeline

    # For SLA priority: fetch all eligible leads (no MongoDB pagination)
    # and compute business minutes + sort in Python.
    if _use_python_sla_sort:
        sla_full_pipeline = list(canonical_pipeline)
        # Remove $skip, $limit, $project; add an aggregate-level $limit as safety cap
        sla_full_pipeline = [s for s in sla_full_pipeline if "$skip" not in s and "$limit" not in s and "$project" not in s]
        sla_full_pipeline.append({"$project": PROJECTION})

    # Conteos y registros provienen del mismo snapshot y de una única agregación.
    facet_pipeline[0]["$facet"]["records"] = records_pipeline
    facet_results = await db["leads"].aggregate(facet_pipeline).to_list(length=1)
    facet_res = facet_results[0] if facet_results else {}
    leads_list = facet_res.get("records", [])

    def get_facet_count(key):
        return facet_res.get(key, [{"count": 0}])[0]["count"] if facet_res.get(key) else 0

    # SLA priority: fetch all scope leads in a separate aggregation so the
    # $facet memory budget is never shared between KPI sub-pipelines and
    # the full-universe sort.
    if _use_python_sla_sort:
        sla_all = await db["leads"].aggregate(sla_full_pipeline).to_list(length=None)
        from chatbot.utils import calculate_business_minutes
        from chatbot.crm_metrics import coerce_utc_datetime
        now_chile = datetime.now(CHILE_TZ)
        for lead in sla_all:
            at_raw = lead.get("_cycle_assigned_at")
            at_dt = coerce_utc_datetime(at_raw)
            if at_dt:
                at_chile = at_dt.astimezone(CHILE_TZ)
            else:
                at_chile = None
            temp = lead.get("_temperature") or lead.get("lead_temperature_effective") or "COLD"
            threshold = 60 if str(temp).upper() == "HOT" else 180
            if at_chile:
                elapsed = calculate_business_minutes(at_chile, now_chile)
                lead["_business_minutes"] = elapsed
                lead["_overdue_minutes"] = elapsed - threshold
            else:
                lead["_business_minutes"] = None
                lead["_overdue_minutes"] = None
        # Sort groups:
        #   0 = unmanaged assigned overdue (by overdue DESC)
        #   1 = unmanaged assigned in-plazo (by remaining ASC)
        #   2 = unassigned
        #   3 = managed assigned
        #   4 = no cycle / historical / missing date
        def _sla_key(lead):
            od = lead.get("_overdue_minutes")
            mgmt = lead.get("_has_management", 1)
            assigned = lead.get("_has_assigned", 1)
            if od is None:
                return (4, 0)
            if assigned == 0 and mgmt == 0:
                return (3, 0)
            if assigned != 0:
                return (2, 0)
            if od > 0:
                return (0, -od)
            return (1, -od)
        sla_all.sort(key=_sla_key)
        total_count = len(sla_all)
        sla_page = sla_all[offset:offset + limit]
        leads_list = sla_page
    else:
        total_count = get_facet_count("total_pagina")

    total_count = get_facet_count("total_pagina")
    global_total = get_facet_count("global_total")
    scope_total = get_facet_count("scope_total")
    kpi_counts = {
        "total": global_total,
        "scope_total": scope_total,
        "hot": get_facet_count("total_hot"),
        "cold": get_facet_count("total_cold"),
        "nuevo": get_facet_count("nuevo"),
        "gestion": get_facet_count("gestion"),
        "visita": get_facet_count("visita"),
        "cerrado": get_facet_count("cerrado"),
        "sin_asignar": get_facet_count("sin_asignar"),
        "sin_asignar_global": get_facet_count("sin_asignar_global"),
    }
    series_rows = facet_res.get("assignment_series") or []
    series_by_day = {str(row.get("_id")): row for row in series_rows if row.get("_id")}
    today_local = datetime.now(CHILE_TZ).date()
    series_days = [(today_local - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    kpi_counts["assignment_series"] = {
        "total": [series_by_day.get(day, {}).get("total", 0) for day in series_days],
        "hot": [series_by_day.get(day, {}).get("hot", 0) for day in series_days],
        "cold": [series_by_day.get(day, {}).get("cold", 0) for day in series_days],
    }
    from chatbot.crm_metrics import validate_list_parity
    # SLA priority fetches the full universe for Python-side business-minute
    # sort; the sub-pipeline counts are independent of record-level filters.
    if _use_python_sla_sort:
        total_count = scope_total
    elif filtro_estado in ("NEW", "nuevo"):
        # The effective NEW filter excludes legacy rows that already carry
        # valid management evidence; keep the displayed KPI in parity with
        # that filtered result set.
        kpi_counts["nuevo"] = total_count
    elif filtro_estado == "GRUPO_GESTION":
        kpi_counts["gestion"] = total_count
    parity = validate_list_parity(kpis=kpi_counts, listed_total=total_count, state_filter=filtro_estado)
    if not parity["validated"]:
        logger.error("[CRM_PARITY] KPI/list mismatch: %s", parity)
        raise RuntimeError(f"CRM KPI/list parity failed: {parity}")
    for state_key in state_kpi_conditions:
        kpi_counts[f"{state_key}_hot"] = get_facet_count(f"{state_key}_hot")
        kpi_counts[f"{state_key}_cold"] = get_facet_count(f"{state_key}_cold")
    # "Con gestión iniciada" = EN_GESTION + VISITAS + CERRADOS.
    kpi_counts["managed"] = kpi_counts["gestion"] + kpi_counts["visita"] + kpi_counts["cerrado"]
    kpi_counts["managed_percent"] = (kpi_counts["managed"] * 100 / scope_total) if scope_total else 0.0
    kpi_counts["hot_percent"] = (kpi_counts["hot"] * 100 / global_total) if global_total else 0.0
    kpi_counts["cold_percent"] = (kpi_counts["cold"] * 100 / global_total) if global_total else 0.0
    for key in ("sin_asignar", "nuevo", "gestion", "visita", "cerrado"):
        kpi_counts[f"{key}_percent"] = (kpi_counts[key] * 100 / scope_total) if scope_total else 0.0
    logger.info(
        f"[PERF] get_crm_leads_list -> single $facet (KPIs + records): "
        f"{(time.perf_counter()-t_kpis)*1000:.1f}ms"
    )


    leads_procesados = []
    # (KPI counts are already calculated via optimized MongoDB queries above)

    # 4b. BULK QUERY DE EVENTOS para los leads de ESTA PÁGINA solamente (máx 10-20 teléfonos)
    # Esto es O(page_size), no O(total_leads). Correcto y eficiente.
    page_phones = [l.get("phone", "").replace("+", "").strip() for l in leads_list]
    phone_candidates = list({value for phone in page_phones for value in (phone, f"+{phone}") if phone})
    phone_leads = await db["leads"].find(
        {"phone": {"$in": phone_candidates}}, {"phone": 1}
    ).to_list(length=max(200, len(phone_candidates) * 2))
    phone_identity_counts = {}
    for candidate in phone_leads:
        normalized = str(candidate.get("phone") or "").replace("+", "").strip()
        phone_identity_counts[normalized] = phone_identity_counts.get(normalized, 0) + 1
    management_types = [
        "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD",
        "CALL_COMPLETED_LEAD",
        "CLICK_PHONE_LEAD", "CLICK_WHATSAPP_LEAD", "CLICK_EMAIL_LEAD",
        "SEND_WA_OWNER", "SEND_EMAIL_OWNER", "CLICK_PHONE_OWNER",
        "CLICK_WHATSAPP_OWNER", "CLICK_EMAIL_OWNER", "STATUS_CHANGE", "ASSIGNMENT", "MANUAL_ENTRY",
        "ALERT_SENT", "alert_sent"
    ]
    page_lead_ids_for_events = [l.get("_id") for l in leads_list if l.get("_id")]
    events_cursor = db["crm_events"].find(
        {"$or": [
            {"phone": {"$in": page_phones}},
            {"lead_id": {"$in": page_lead_ids_for_events}},
        ], "type": {"$in": management_types + ["CONTACT_RESULT"]}},
        sort=[("timestamp", -1)]
    )
    events_list = await events_cursor.to_list(length=200)
    events_map = {}
    recognized_management_map = {}
    recognized_management_by_lead_id = {}
    lead_phone_by_id = {
        str(lead.get("_id")): str(lead.get("phone") or "").replace("+", "").strip()
        for lead in leads_list if lead.get("_id")
    }
    # CONTACT_RESULT is the canonical event created by the quick-management
    # form. SEND/CLICK/STATUS_CHANGE remain telemetry and never enter this map.
    MANAGEMENT_EVENT_TYPES = frozenset({"HUMAN_NOTE", "GESTION_LOG", "CONTACT_RESULT"})
    for ev in events_list:
        phone_ev = ev.get("phone", "").replace("+", "").strip()
        if not phone_ev and ev.get("lead_id") is not None:
            phone_ev = lead_phone_by_id.get(str(ev.get("lead_id")), "")
        if phone_ev not in events_map:
            events_map[phone_ev] = ev
        if (_is_canonical_management_event(ev) or ev.get("type") in MANAGEMENT_EVENT_TYPES) and phone_ev not in recognized_management_map:
            recognized_management_map[phone_ev] = ev
        if (_is_canonical_management_event(ev) or ev.get("type") in MANAGEMENT_EVENT_TYPES) and ev.get("lead_id") is not None:
            recognized_management_by_lead_id.setdefault(str(ev.get("lead_id")), ev)

    # TYPE_LABELS is defined at module level (above). No local type_labels needed.

    # 5b. BULK QUERY DE CICLOS DE ASIGNACIÓN para los leads de esta página.
    # Resuelve todos los ciclos activos en una sola consulta batch.
    page_lead_ids = [l.get("_id") for l in leads_list if l.get("_id")]
    cycle_by_lead_id: dict[str, dict] = {}
    if page_lead_ids:
        from chatbot.storage import get_async_db
        adb = get_async_db()
        # The list must use the same canonical commercial cycle for SLA,
        # temperature and executive. Technical/legacy active cycles are not
        # presentation sources.
        cycles_cursor = adb["crm_assignment_cycles"].find(
            {"lead_id": {"$in": page_lead_ids}, "cycle_status": "active",
             "schema_version": "crm_assignment_cycle_v1",
             "notification_eligible": True,
             "reason": {"$in": ["inbound_message", "lead_created", "manual_lead_created"]},
             "cycle_origin": {"$in": ["inbound_message", "manual_lead"]}},
        ).sort([("assigned_at", -1)])
        cycle_docs = await cycles_cursor.to_list(length=len(page_lead_ids) + 1)
        for c in cycle_docs:
            lid = str(c.get("lead_id"))
            if lid not in cycle_by_lead_id:
                cycle_by_lead_id[lid] = c

    # 5. PROCESAR LEADS EN MEMORIA
    state_map = {
        # Enums
        PipelineStage.NEW:   {"label": "Sin Atender", "led": "led-red",    "priority": 1},
        PipelineStage.CONTACTED: {"label": "En Gestión",  "led": "led-yellow", "priority": 3},
        PipelineStage.INTERESTED: {"label": "Interesado",  "led": "led-yellow", "priority": 3},
        PipelineStage.VISIT_SCHEDULED:  {"label": "Visita Agendada", "led": "led-green",  "priority": 2},
        PipelineStage.VISIT_DONE:  {"label": "Visita Realizada", "led": "led-green",  "priority": 2},
        PipelineStage.OFFER:  {"label": "Oferta", "led": "led-green",  "priority": 2},
        PipelineStage.NEGOTIATION:  {"label": "Negociación", "led": "led-green",  "priority": 2},
        PipelineStage.CLOSED_WON: {"label": "Cerrado Ganado",     "led": "led-gray",   "priority": 4},
        PipelineStage.CLOSED_LOST: {"label": "Cerrado Perdido",     "led": "led-gray",   "priority": 4},
        # Legacy Support
        "nuevo":   {"label": "Sin Atender", "led": "led-red",    "priority": 1},
        "visita":  {"label": "Visita Agendada", "led": "led-green",  "priority": 2},
        "gestion": {"label": "En Gestión",  "led": "led-yellow", "priority": 3},
        "cerrado": {"label": "Cerrado",     "led": "led-gray",   "priority": 4}
    }
    # Tipos de eventos considerados como gestión humana válida
    management_types = [
        "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
        "CLICK_PHONE_LEAD", "CLICK_WHATSAPP_LEAD", "SEND_WA_OWNER", "SEND_EMAIL_OWNER", 
        "CLICK_PHONE_OWNER", "CLICK_WHATSAPP_OWNER", "ALERT_SENT", "alert_sent"
    ]

    def _coerce_crm_datetime(value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
            # PyMongo returns BSON UTC datetimes as naive values unless its
            # client was configured tz_aware.  They are never local Chile
            # wall-clock values, so localizing them here shifts SLA by hours.
            if parsed.tzinfo is None:
                parsed = pytz.utc.localize(parsed)
            return parsed.astimezone(CHILE_TZ)
        except (TypeError, ValueError, AttributeError):
            return None

    # 4. PROCESAR LEADS EN MEMORIA
    for lead in leads_list:
        raw_phone = lead.get("phone", "").replace("+", "").strip()
        estado_db = lead.get("pipeline_stage") or lead.get("stage") or lead.get("crm_estado") or PipelineStage.NEW
        
        # Normalizar strings legacy a Enums
        if isinstance(estado_db, str):
            estado_map_legacy = {
                "nuevo": PipelineStage.NEW,
                "new": PipelineStage.NEW,
                "contacted": PipelineStage.CONTACTED,
                "gestion": PipelineStage.CONTACTED,
                "visita": PipelineStage.VISIT_SCHEDULED,
                "cerrado": PipelineStage.CLOSED_WON
            }
            estado_key = estado_db.lower()
            estado_db = estado_map_legacy.get(estado_key)
            if estado_db is None:
                # Keep canonical PipelineStage values (CLOSED_WON, CLOSED_LOST,
                # INTERESTED, OFFER, NEGOTIATION, VISIT_DONE, ...) instead of
                # collapsing them to NEW.  Only genuinely unknown legacy values
                # fall back to NEW.
                try:
                    estado_db = PipelineStage(estado_key.upper())
                except ValueError:
                    estado_db = PipelineStage.NEW
        
        last_ev = events_map.get(raw_phone)
        recognized_management_ev = recognized_management_by_lead_id.get(str(lead.get("_id")))
        if not recognized_management_ev and phone_identity_counts.get(raw_phone) == 1:
            recognized_management_ev = recognized_management_map.get(raw_phone)
        current_cycle = cycle_by_lead_id.get(str(lead.get("_id"))) if lead.get("_id") else None
        lifecycle = lead.get("lifecycle") or {}
        # Never combine new-cycle SLA with historical lead fields.  If a
        # canonical active cycle exists, it is the sole source for this row.
        # Keep the two business timestamps separate: the assignment date is
        # when the executive received the lead, while the SLA date may be the
        # next business opening for an after-hours assignment.
        cycle_assignment_raw = ((current_cycle or {}).get("assigned_at")
                                or lifecycle.get("assigned_at"))
        cycle_sla_started_raw = ((current_cycle or {}).get("sla_started_at")
                                or lifecycle.get("sla_started_at")
                                or cycle_assignment_raw)
        assigned_for_cycle = _coerce_crm_datetime(
            cycle_assignment_raw or lead.get("fecha_asignacion")
        )
        # Presentation-only "Enviado" timestamp. Never use created_at as a
        # delivery timestamp. Assignment is the honest fallback until reliable
        # delivery confirmation exists on the cycle.
        confirmed_delivery_raw = None
        if (current_cycle or {}).get("delivery_confirmed") is True:
            confirmed_delivery_raw = ((current_cycle or {}).get("delivery_confirmed_at")
                                      or (current_cycle or {}).get("delivered_at"))
        effective_sent_at = _coerce_crm_datetime(
            confirmed_delivery_raw
            or cycle_assignment_raw
            or lifecycle.get("assigned_at")
            or lead.get("fecha_asignacion")
        )
        if confirmed_delivery_raw and effective_sent_at:
            effective_sent_source = "Entrega confirmada"
            effective_sent_confirmed = True
        elif effective_sent_at and (current_cycle or {}).get("assigned_at"):
            effective_sent_source = "Asignación"
            effective_sent_confirmed = False
        elif effective_sent_at:
            effective_sent_source = "Asignación legacy"
            effective_sent_confirmed = False
        else:
            effective_sent_source = "Sin información"
            effective_sent_confirmed = False
        current_cycle_id = ((current_cycle or {}).get("assignment_cycle_id")
                            or lifecycle.get("current_assignment_cycle_id")
                            or lifecycle.get("assignment_cycle_id"))
        from chatbot.crm_metrics import registered_outreach_evidence
        outreach = registered_outreach_evidence(
            recognized_management_ev,
            assigned_at=assigned_for_cycle,
            assignment_cycle_id=current_cycle_id,
            # Previous-cycle outreach may remain visible in the timeline, but
            # it must not mark the current assignment as managed.
            allow_historical_for_presentation=False,
        )
        if recognized_management_ev and _is_canonical_management_event(recognized_management_ev):
            # Canonical management results are already cycle-scoped. Validate
            # their timestamp/cycle here, instead of treating them as outreach
            # telemetry (which intentionally follows different rules).
            result_at = _coerce_crm_datetime(
                recognized_management_ev.get("timestamp") or recognized_management_ev.get("occurred_at")
            )
            result_cycle_id = recognized_management_ev.get("assignment_cycle_id")
            if ((assigned_for_cycle and result_at and result_at < assigned_for_cycle)
                    or (current_cycle_id and result_cycle_id
                        and str(result_cycle_id) != str(current_cycle_id))):
                recognized_management_ev = None
        elif not outreach["recognized"]:
            recognized_management_ev = None
        # Management is cycle-scoped and must come from a canonical human result.
        # A legacy lead-level timestamp must never mark a newer active cycle as
        # managed; SEND_WA/CLICK events are outreach telemetry only.
        cycle_management_at = (current_cycle or {}).get("first_valid_management_at")
        legacy_management_at = (lead.get("lifecycle") or {}).get("first_valid_management_at")
        has_real_management = bool(
            recognized_management_ev is not None
            or cycle_management_at
            or (not current_cycle and legacy_management_at)
        )
        if recognized_management_ev:
            commercial_ev = recognized_management_ev
        else:
            commercial_ev = None
            for candidate_ev in events_list:
                if candidate_ev.get("phone", "").replace("+", "").strip() == raw_phone:
                    if candidate_ev.get("type") in TELEMETRY_LABEL_TYPES:
                        commercial_ev = candidate_ev
                        break
            if not commercial_ev:
                commercial_ev = last_ev
        if commercial_ev:
            event_meta = commercial_ev.get("meta") or commercial_ev.get("metadata") or {}
            last_action_text = (
                _resultado_estado_label(commercial_ev)
                or TYPE_LABELS.get(commercial_ev.get("type"))
                or event_meta.get("action_label")
                or event_meta.get("action")
                or "Sin gestión registrada"
            )
            last_action_note = (event_meta.get("notes") or event_meta.get("note")
                                or event_meta.get("reason") or event_meta.get("outcome") or "")
        else:
            persisted_action = (lead.get("last_action_label") or "").strip()
            last_action_text = (
                "Sin gestión registrada"
                if persisted_action.lower() in {"", "acción registrada", "accion registrada", "sin gestión aún"}
                else persisted_action
            )
            last_action_note = ""

        last_message_at = lead.get("last_message_at")
        message_dt = _coerce_crm_datetime(last_message_at)
        event_dt = _coerce_crm_datetime(commercial_ev.get("timestamp") if commercial_ev else None)
        has_new_customer_reply = bool(
            lead.get("last_message_role") == "user"
            and message_dt
            and (not event_dt or message_dt > event_dt)
        )
        if has_new_customer_reply:
            last_action_text = "Nueva respuesta del cliente"
            last_action_note = lead.get("last_message_preview") or ""
        
        ultimo_msg_ts = lead.get("prospecto", {}).get("ultimo_mensaje")
        lifecycle_ts = (cycle_assignment_raw
                        or lifecycle.get("assigned_at"))
        created_ts = lead.get("created_at")
        
        # Mantener la prioridad histórica de la última gestión, salvo que haya
        # una respuesta del cliente posterior a esa gestión.
        last_ts = (
            last_message_at if has_new_customer_reply else
            ((commercial_ev.get("timestamp") if commercial_ev else None)
             if recognized_management_ev else None) or
            lead.get("last_event_at") or
            (commercial_ev.get("timestamp") if commercial_ev else None) or
            lifecycle_ts or ultimo_msg_ts or created_ts
        )
        
        estado_final = estado_db
        # A stale CONTACTED value can remain on a lead after reassignment or
        # bot outreach. For an active cycle, only canonical human management
        # may promote the row; outreach alone remains Sin Atender.
        if current_cycle and not has_real_management and estado_final not in (
            PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST,
            "ARCHIVED", "SUPPRESSED",
        ):
            estado_final = PipelineStage.NEW
        elif (recognized_management_ev or has_real_management) and estado_final == PipelineStage.NEW:
            # A lead whose active cycle already carries canonical management
            # evidence (for example an "intento_fallido" that stops the SLA)
            # must not remain displayed as "Sin Atender".
            estado_final = PipelineStage.CONTACTED
        
        # Identificar ejecutivo y timestamp real para visualización
        ejecutivo = ((current_cycle or {}).get("assigned_to_display_name")
                     or lead.get("ejecutivo_asignado") or lead.get("prospecto", {}).get("ejecutivo"))
        sort_ts = ((current_cycle or {}).get("sla_started_at")
                   or (current_cycle or {}).get("assigned_at")
                   or lead.get("effective_assigned_at") or lifecycle.get("assigned_at")
                   or lead.get("fecha_asignacion"))
        
        if last_ts:
            try: 
                if isinstance(last_ts, str):
                    last_ts_obj = datetime.fromisoformat(last_ts.replace('Z', '+00:00'))
                else:
                    last_ts_obj = last_ts
            except: last_ts_obj = datetime.now(CHILE_TZ)
        else:
            last_ts_obj = datetime.now(CHILE_TZ)

        # (SaaS Performance: Metrics are precomputed in lead doc)
        config_estado = state_map.get(estado_final, state_map[PipelineStage.CONTACTED])

        # 1. TEMPERATURA Y PRIORIDAD: única fuente persistida, sin reinterpretar
        # alerts_sent ni otras señales durante la consulta/render.
        temp = str((current_cycle or {}).get("temperature_at_assignment")
                   or lead.get("lead_temperature_effective") or "COLD").upper()

        # 5. SLA / TIEMPO DE RESPUESTA
        sla_status = lead.get("sla_status", "good")
        
        # Omitir cálculo de SLA crítico para leads informativos/fríos
        if temp != "HOT":
            sla_status = "informativo"
        elif estado_final == PipelineStage.NEW:
            start_time = lead.get("lifecycle", {}).get("hot_since") or lead.get("lifecycle", {}).get("assigned_at") or lead.get("created_at")
            if start_time:
                try:
                    if isinstance(start_time, str):
                        dt_start = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                    else:
                        dt_start = start_time
                    if dt_start.tzinfo is None:
                        dt_start = CHILE_TZ.localize(dt_start)
                    else:
                        dt_start = dt_start.astimezone(CHILE_TZ)

                    mins = calculate_business_minutes(dt_start, datetime.now(CHILE_TZ))
                    sla_hours = mins / 60.0

                    if sla_hours <= 1.5:
                        sla_status = "good"
                    elif sla_hours < 3.0:
                        sla_status = "near_critical"
                    else:
                        sla_status = "critical"
                except Exception:
                    sla_status = lead.get("sla_status", "good")
            else:
                sla_status = "good"
        else:
            sla_status = "fulfilled"
            
        # One SLA definition for cards, list, detail and monitor.
        from chatbot.crm_metrics import calculate_sla, is_pre_visual_cutover
        assigned_at_raw = ((current_cycle or {}).get("sla_started_at")
                           or (current_cycle or {}).get("assigned_at")
                           or lifecycle.get("sla_started_at") or lifecycle.get("assigned_at")
                           or lead.get("fecha_asignacion"))
        # The list date is already normalized for legacy records. Reuse that
        # normalized value here so the SLA timing cannot disagree with the
        # assignment date shown in the same row.
        assigned_at = (_coerce_crm_datetime(assigned_at_raw)
                       or assigned_for_cycle
                       or effective_sent_at)
        visual_pre = is_pre_visual_cutover((current_cycle or {}).get("assigned_at") or assigned_at) if assigned_at else True
        
        sla_hours = 0
        canonical_sla = {}
        hot_started_at = None
        sla_info = {}
        sla_managed_outside = False
        
        if not assigned_at:
            sla_status = "historical" if visual_pre else "unknown"
            sla_label = "Histórico" if visual_pre else "SLA S/I"
        else:
            # The current canonical cycle was resolved in the batch query.
            if current_cycle is not None and isinstance(current_cycle, dict):
                hot_started_at = current_cycle.get("hot_started_at") or current_cycle.get("temperature_transitioned_at")
            
            # SLA completion is scoped to the active assignment cycle. Never
            # reuse a lead-level historical timestamp after reassignment; an
            # outreach event is telemetry and does not stop the SLA clock.
            cycle_management_at = ((current_cycle or {}).get("first_valid_management_at")
                                   if current_cycle else lifecycle.get("first_valid_management_at"))
            canonical_sla = calculate_sla(
                assigned_at=assigned_at,
                first_valid_management_at=cycle_management_at,
                temperature=temp,
                hot_started_at=hot_started_at,
            )
            sla_managed_outside = (
                canonical_sla.get("canonical_state") == "MANAGED_OUTSIDE_SLA"
            )
            
            if visual_pre and not canonical_sla.get("fulfilled"):
                sla_status = "historical"
            elif canonical_sla.get("status"):
                if temp == "HOT" and canonical_sla.get("hot_minutes") is not None and not canonical_sla.get("fulfilled"):
                    if canonical_sla["status"] == "critical": sla_status = "hot_critical"
                    elif canonical_sla["status"] == "near_critical": sla_status = "hot_near_critical"
                    elif canonical_sla["status"] == "warning": sla_status = "hot_warning"
                    else: sla_status = "good"
                else:
                    sla_status = canonical_sla["status"]
            else:
                sla_status = "unknown"
            
            sla_hours = (canonical_sla.get("minutes") or 0) / 60.0
            measured_minutes = canonical_sla.get("hot_minutes") if temp == "HOT" and canonical_sla.get("hot_minutes") is not None else canonical_sla.get("minutes")
            threshold_minutes = canonical_sla.get("threshold_minutes")
            if measured_minutes is None or threshold_minutes is None:
                sla_timing = "SLA no disponible"
            elif canonical_sla.get("fulfilled"):
                delta = format_duration_minutes(measured_minutes)
                over = format_duration_minutes(measured_minutes - threshold_minutes)
                sla_timing = (f"Dentro de SLA · {delta}" if measured_minutes < threshold_minutes
                              else f"Fuera de SLA · +{over}")
            elif measured_minutes >= threshold_minutes:
                sla_timing = f"Venció hace {format_duration_minutes(measured_minutes - threshold_minutes)}"
            else:
                sla_timing = f"Faltan {format_duration_minutes(threshold_minutes - measured_minutes)}"
            
            # Build SLA info for row tooltip
            if not visual_pre:
                sla_info = {
                    "total_minutes": canonical_sla.get("minutes", 0),
                    "hot_minutes": canonical_sla.get("hot_minutes"),
                    "assigned_at": str(assigned_at),
                }
                if hot_started_at:
                    sla_info["hot_started"] = str(hot_started_at)
        
        sla_labels_map = {
            "critical": "Vencido", "near_critical": "Próximo a vencer", "warning": "Atención",
            "good": "En plazo", "pending": "Pendiente Asignación", "fulfilled": "Gestionado",
            "hot_critical": "Vencido", "hot_near_critical": "Próximo a vencer",
            "hot_warning": "Atención prioritaria",
            "informativo": "Antigüedad", "historical": "Histórico", "unknown": "SLA S/I",
        }
        sla_label = sla_labels_map.get(sla_status, "En tiempo")
        
        if not ejecutivo or ejecutivo in [UNASSIGNED_LABEL, "No asignado", "Sin Asignar", "Sin asignar"]:
             sla_status = "pending"
             sla_label = "Pendiente Asignación"

        if temp == "HOT":
            prioridad_badge = "🔥 Alta"
        else:
            prioridad_badge = "📋 Lead"

        sla_started_display = cycle_sla_started_raw or lifecycle_ts
        management_age = format_relative_time(last_ts_obj).replace("Hace", "hace", 1)
        if estado_final in (PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST):
            age_label = f"Cerrado {format_relative_time(lifecycle_ts or created_ts).replace('Hace', 'hace', 1)}"
        elif last_action_text != "Sin gestión registrada":
            age_label = f"Última gestión {management_age}"
        else:
            assigned_age = _after_hours_label(lifecycle_ts or created_ts, sla_started_raw=sla_started_display, has_real_management=has_real_management)
            if assigned_age.startswith("anoche"):
                age_label = f"Asignado {assigned_age}"
            elif estado_final == PipelineStage.NEW:
                age_label = f"Sin atender {format_relative_time(lifecycle_ts or created_ts).replace('Hace', 'hace', 1)}"
            else:
                age_label = f"Asignado {assigned_age}"

        leads_procesados.append({
            "phone": raw_phone,
            "lead_id": str(lead.get("_id") or ""),
            "phone_is_synthetic": bool(lead.get("phone_is_synthetic")) or str(lead.get("phone", "")).startswith("no-phone-"),
            "sla_status": sla_status,
            "sla_timing": sla_timing if assigned_at else "SLA no disponible",
            "sla_managed_outside": sla_managed_outside,
            "sla_label": sla_label,
            "age_label": age_label,
            "whatsapp_display": ("Sin teléfono" if (str(lead.get("phone", "")).startswith("no-phone-")
                                                    or lead.get("phone_is_synthetic"))
                                 else (f"+{raw_phone}" if raw_phone else "Sin teléfono")),
            "nombre": lead.get("prospecto", {}).get("nombre") or "Desconocido",
            "prioridad_badge": prioridad_badge,
            "lead_temperature_effective": temp,
            "estado": estado_final,
            "estado_badge": config_estado["label"],
            "can_register_management": estado_final not in (
                PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST,
                "ARCHIVED", "SUPPRESSED",
            ),
            "led_class": config_estado["led"],
            "gestionado": bool(has_real_management),
            "estado_resultado": _resultado_estado_label(
                recognized_management_ev or commercial_ev
            ) if has_real_management else None,
            "tiempo_relativo": format_relative_time(last_ts_obj),
            "real_timestamp": last_ts_obj,
            "created_timestamp": lead.get("created_at"),
            "priority_score": config_estado["priority"],
            "codigo_propiedad": detect_property_code(lead) or "S/N",
            "url_propiedad": f"https://www.procasa.cl/{detect_property_code(lead)}" if detect_property_code(lead) else "#",
            "ultima_accion_titulo": last_action_text,
            "ultima_accion_note": last_action_note,
            "ultima_accion_nota": last_action_note,
            "ejecutivo_nombre": ejecutivo or UNASSIGNED_LABEL,
            "fecha_asignacion_relativa": _after_hours_label(lifecycle_ts or lead.get("fecha_asignacion"), sla_started_raw=sla_started_display, has_real_management=has_real_management),
            "assignment_cycle_id": current_cycle_id,
            "assigned_at": assigned_for_cycle,
            # The SLA clock starts at the commercial/business timestamp. For
            # after-hours cycles this is the next opening, normally 09:00.
            "sla_started_at": assigned_at,
            "sla_started_date": assigned_at.strftime("%d/%m/%Y") if assigned_at else None,
            "sla_started_time": assigned_at.strftime("%H:%M") if assigned_at else None,
            "sla_start_differs": bool(
                assigned_for_cycle and assigned_at
                and assigned_for_cycle.strftime("%d/%m/%Y %H:%M")
                != assigned_at.strftime("%d/%m/%Y %H:%M")
            ),
            "effective_sent_at": effective_sent_at,
            "effective_sent_date": effective_sent_at.strftime("%d/%m/%Y") if effective_sent_at else None,
            "effective_sent_time": effective_sent_at.strftime("%H:%M") if effective_sent_at else None,
            # Use the same timestamp that renders the visible assignment date.
            # Legacy records can lack lifecycle.assigned_at while still having
            # fecha_asignacion, which previously left the relative line empty.
            "assigned_relative": format_relative_compact(assigned_for_cycle or effective_sent_at),
            "effective_sent_source": effective_sent_source,
            "effective_sent_confirmed": effective_sent_confirmed,
            "stage": lead.get("stage") or "new",
            "sort_timestamp": sort_ts
        })
    
    # 5. RETORNAR RESULTADOS
    return leads_procesados, kpi_counts, total_count

import time
_executives_cache = {"data": [], "expires_at": 0}

async def get_unique_executives():
    """Retorna lista de nombres únicos de ejecutivos que tienen leads asignados. Cacheado por 5 minutos."""
    global _executives_cache
    if time.time() < _executives_cache["expires_at"]:
        return _executives_cache["data"]

    from chatbot.storage import get_async_db
    adb = get_async_db()
    # Fast-path: usar colección usuarios (mucho menor que leads).
    users = await adb["usuarios"].find(
        {"rol": {"$in": ["agente", "supervisor", "admin", "jefatura"]}},
        {"nombre": 1}
    ).to_list(length=500)
    all_execs = set(str(u.get("nombre", "")).strip() for u in users if u.get("nombre"))
    # Fallback si usuarios viene vacío.
    if not all_execs:
        import asyncio
        execs_1, execs_2 = await asyncio.gather(
            adb["leads"].distinct("ejecutivo_asignado"),
            adb["leads"].distinct("prospecto.ejecutivo")
        )
        all_execs = set([e for e in execs_1 if e] + [e for e in execs_2 if e])
    
    # Limpieza para que el filtro no muestre duplicados
    cleaned_execs = set()
    for e in all_execs:
        words = str(e).strip().split()
        if len(words) > 2:
            cleaned_execs.add(f"{words[0]} {words[1]}")
        else:
            cleaned_execs.add(str(e).strip())
            
    result = sorted(list(cleaned_execs))
    _executives_cache["data"] = result
    _executives_cache["expires_at"] = time.time() + 300
    return result

# --- 2. DETALLE DEL LEAD ---
def get_lead_detail_data(phone, property_code=None, lead_doc=None):
    """Build detail data, optionally from an already-resolved lead document."""
    db = get_db()
    phone = str(phone or "")
    phone_clean = phone.replace(" ", "").replace("+", "").strip()

    lead = lead_doc
    if lead is None:
        query = {"phone": {"$regex": phone_clean}}
        if property_code:
            query["$or"] = [
                {"prospecto.codigo": property_code},
                {"prospecto.codigo": str(property_code)},
                {"datos_propiedad.codigo": property_code},
                {"datos_propiedad.codigo": str(property_code)}
            ]
        lead = db["leads"].find_one(query, sort=[("created_at", -1)])
    if not lead: return None
    
    codigo = detect_property_code(lead)
    datos_propiedad = get_real_property_data(db, codigo)
    
    if not datos_propiedad:
        p = lead.get("prospecto", {})
        datos_propiedad = {
            "codigo": codigo or "S/N",
            "nombre_propietario": p.get("owner_name", "Propietario No Asignado"),
            "movil_propietario": p.get("owner_phone", "S/I"),
            "precio_uf": p.get("precio", "0"),
            "comuna": p.get("comuna", ""),
            "calle": p.get("direccion", ""),
            "url": "#"
        }

    # Se incluyen logs de gestión real para un historial limpio y útil
    new_events_cursor = db["crm_events"].find({
        "phone": phone_clean,
        "type": {"$in": [
            "GESTION_LOG", "STATUS_CHANGE", "HUMAN_NOTE", 
            "CLICK_PHONE_LEAD", "CLICK_PHONE_OWNER",
            "SEND_WA_LEAD", "SEND_EMAIL_LEAD",
            "SEND_WA_OWNER", "SEND_EMAIL_OWNER",
            "ALERT_SENT", "alert_sent", "msg_out"
        ]} 
    }).sort("timestamp", -1)
    
    formatted_new_history = []
    _last_status_transition = None
    for evt in new_events_cursor:
        meta = evt.get("meta", {})
        # Distincion de tipo para UI y filtro de ruido
        evt_type = evt.get("type")
        if evt_type in ["CLICK_WHATSAPP_LEAD", "ASSIGNMENT", "assignment", "MANUAL_ENTRY", "CLICK_WHATSAPP_OWNER"]:
            continue
            
        display_type = "system" if evt_type == "STATUS_CHANGE" else "user"
        
        ts_obj = coerce_crm_datetime(evt.get("timestamp"))
        if ts_obj is not None:
            ts_obj = ts_obj.astimezone(CHILE_TZ)
        else:
            ts_obj = datetime.min.replace(tzinfo=CHILE_TZ)
            
        # ETIQUETAS DINÁMICAS PARA EL HISTORIAL (Mejorado para evitar "Evento CRM")
        type_labels = {
            "CLICK_PHONE_LEAD": "Llamada Iniciada",
            "CLICK_PHONE_OWNER": "Llamada Iniciada (Prop.)",
            "SEND_WA_LEAD": "WhatsApp Enviado",
            "SEND_EMAIL_LEAD": "Email Enviado",
            "SEND_WA_OWNER": "WhatsApp Enviado (Prop.)",
            "SEND_EMAIL_OWNER": "Email Enviado (Prop.)",
            "STATUS_CHANGE": "Cambio de Estado",
            "GESTION_LOG": "Gestión Registrada",
            "HUMAN_NOTE": meta.get("action_label", "Nota de Gestión"),
            "ASSIGNMENT": "Asignación de Lead",
            "assignment": "Asignación de Lead",
            "ALERT_SENT": "Alerta Enviada",
            "alert_sent": "Alerta Enviada",
            "MANUAL_ENTRY": "Ingreso Manual",
            "msg_out": "Respuesta Bot"
        }

        user_action_display = meta.get("action_label") or type_labels.get(evt_type, "Actividad")
        
        # --- MAPEO DE ICONOS DINÁMICOS ---
        # Formato: (Icono, Clase CSS)
        icon_map = {
            "CLICK_WHATSAPP_LEAD": ("fa-brands fa-whatsapp", "tl-wa"),
            "CLICK_PHONE_LEAD": ("fa-solid fa-phone", "tl-phone"),
            "CLICK_EMAIL_LEAD": ("fa-solid fa-envelope", "tl-email"),
            "SEND_WA_LEAD": ("fa-brands fa-whatsapp", "tl-wa"),
            "SEND_EMAIL_LEAD": ("fa-solid fa-envelope", "tl-email"),
            "CLICK_WHATSAPP_OWNER": ("fa-brands fa-whatsapp", "tl-wa"),
            "SEND_WA_OWNER": ("fa-brands fa-whatsapp", "tl-wa"),
            "STATUS_CHANGE": ("fa-solid fa-right-left", "tl-status"),
            "HUMAN_NOTE": ("fa-solid fa-note-sticky", "tl-note"),
            "GESTION_LOG": ("fa-solid fa-clipboard-check", "tl-note"),
            "ASSIGNMENT": ("fa-solid fa-user-check", "tl-status"),
            "MANUAL_ENTRY": ("fa-solid fa-user-plus", "tl-status")
        }
        
        # Valores por defecto
        final_icon, final_class = icon_map.get(evt_type, ("fa-solid fa-check", ""))
        
        # Especialización por canal (sobrescribe tipo base)
        channel = meta.get("interaction_type") or meta.get("channel")
        if channel == 'wa':
            final_icon, final_class = icon_map["SEND_WA_LEAD"]
        elif channel == 'phone':
            final_icon, final_class = icon_map["CLICK_PHONE_LEAD"]
        elif channel == 'email':
            final_icon, final_class = icon_map["SEND_EMAIL_LEAD"]

        # Especialización por resultado en HUMAN_NOTE
        res = str(meta.get("result", "")).lower()
        if evt_type == "HUMAN_NOTE":
            if "visita" in res:
                final_icon, final_class = "fa-solid fa-calendar-check", "tl-visit"
            elif "ganado" in res:
                final_icon, final_class = "fa-solid fa-trophy", "tl-win"
            elif any(x in res for x in ["perdido", "descartado", "inválido", "cerrado"]):
                final_icon, final_class = "fa-solid fa-ban", "tl-loss"

        # Historical de-duplication: the legacy auto-promotion could log two
        # identical STATUS_CHANGE events back-to-back (same from/to).  Collapse
        # consecutive duplicates so the history shows one clean transition.
        if evt_type == "STATUS_CHANGE":
            transition_key = (meta.get("from"), meta.get("to"))
            if transition_key == _last_status_transition:
                continue
            _last_status_transition = transition_key
        else:
            _last_status_transition = None

        formatted_new_history.append({
            "timestamp": ts_obj,
            "user_action": user_action_display if evt_type != "STATUS_CHANGE" else "Cambio de Estado",
            "result": meta.get("result", ""),
            "notes": meta.get("notes", "") or meta.get("to", "") or meta.get("content_preview", ""), 
            "type_class": display_type,
            "raw_type": evt_type,
            "icon": final_icon,
            "icon_class": final_class,
            "channel": channel
        })
        
    timeline = process_chat_timeline(lead.get("messages", []))
    prospecto = lead.get("prospecto", {})

    # Buscar próxima tarea pendiente (Auditoría Canónica)
    next_task = db["crm_tasks"].find_one({
        "phone": phone_clean,
        "status": "pending"
    }, sort=[("execute_at", 1)])

    # Prioridad al stage nuevo
    crm_state = lead.get("stage") or lead.get("crm_estado") or "new"

    # Priority over legacy assignment naming 
    ejec_asignado = lead.get("ejecutivo_asignado") or prospecto.get("ejecutivo")
    if ejec_asignado and isinstance(ejec_asignado, str):
        words = ejec_asignado.strip().split()
        if len(words) > 2:
            ejec_asignado = f"{words[0]} {words[1]}"

    return {
        "phone": lead.get("phone"),
        "timeline": timeline,
        "nombre": prospecto.get("nombre", "Desconocido"),
        "email": prospecto.get("email", "No registrado"),
        "rut": prospecto.get("rut", "No registrado"),
        "lead_temperature_effective": lead.get("lead_temperature_effective"),
        "crm_estado": crm_state,
        "next_action_date": next_task["execute_at"].isoformat() if next_task and isinstance(next_task["execute_at"], datetime) else (next_task["execute_at"] if next_task else None),
        "last_action_label": formatted_new_history[0]["user_action"] if formatted_new_history else "Sin gestión aún",
        "last_action_relative": format_relative_time(formatted_new_history[0]["timestamp"]) if formatted_new_history else None,
        "last_crm_update": lead.get("last_crm_update").isoformat() if isinstance(lead.get("last_crm_update"), datetime) else lead.get("last_crm_update"),
        "crm_history": formatted_new_history, 
        "sticky_notes": lead.get("sticky_notes", []),
        "datos_propiedad": datos_propiedad,
        "last_intent": lead.get("last_intent"),
        "last_intent_at": lead.get("last_intent_at"),
        "ejecutivo_asignado": ejec_asignado # Requerido para RBAC en detalle
    }

# --- 3. ACTUALIZAR LEAD (CON VALIDACIÓN ESTRICTA) ---
def update_lead_crm_data(phone, data):
    db = get_db()
    from chatbot.crm_management import (
        record_legacy_management_result,
    )
    from chatbot.crm_metrics import active_assignment_cycle, resolve_canonical_lead

    resolution = resolve_canonical_lead(db, phone=phone)
    current_lead = resolution.lead
    if not current_lead:
        return False

    assignment_cycle_id = data.get("assignment_cycle_id")
    if not assignment_cycle_id:
        active_cycle = active_assignment_cycle(db, current_lead["_id"])
        assignment_cycle_id = (active_cycle or {}).get("assignment_cycle_id")
    if not assignment_cycle_id:
        raise ValueError("active assignment cycle not found")

    result = record_legacy_management_result(
        db,
        lead=current_lead,
        actor_user_id=str(data.get("_actor_user_id") or ""),
        actor_can_manage_any_cycle=bool(data.get("_actor_can_manage_any_cycle")),
        assignment_cycle_id=str(assignment_cycle_id),
        data=data,
    )
    return {
        "status": "ok",
        "new_state": result.get("new_state"),
        "next_action_date": result.get("next_action_date"),
        "event_id": result.get("_id"),
        "management_result_id": result.get("_id"),
        "assignment_cycle_id": result.get("assignment_cycle_id"),
        "result_type": result.get("result_type"),
    }


def reconcile_invalid_management(phone, actor="Administración"):
    """Repair derived management fields without removing immutable events."""
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    lead = db["leads"].find_one({"phone": {"$regex": phone_clean}})
    if not lead:
        return {"status": "not_found"}

    events = list(db["crm_events"].find({
        "$or": [{"lead_id": lead["_id"]}, {"phone": phone_clean}]
    }))
    from chatbot.crm_metrics import event_evidence
    if any(event_evidence(event).get("management") for event in events):
        return {"status": "valid_management_present"}

    now = datetime.now(CHILE_TZ)
    db["leads"].update_one({"_id": lead["_id"]}, {"$set": {
        "pipeline_stage": PipelineStage.NEW,
        "stage": PipelineStage.NEW,
        "last_crm_update": now,
        "lifecycle.first_valid_management_at": None,
        "lifecycle.first_valid_management_actor": None,
        "management_status": "unmanaged",
    }})
    cycle = db["crm_assignment_cycles"].find_one(
        {"lead_id": lead["_id"], "cycle_status": "active"}, sort=[("assigned_at", -1)]
    )
    if cycle:
        db["crm_assignment_cycles"].update_one({"_id": cycle["_id"]}, {"$set": {
            "first_valid_management_at": None,
            "first_valid_management_actor": None,
            "sla_first_management_status": "pending",
        }})
    log_event(phone_clean, InteractionType.STATUS_CHANGE, actor, {
        "from": lead.get("pipeline_stage") or lead.get("stage"),
        "to": PipelineStage.NEW,
        "reason": "reconcile_invalid_management",
        "meaningful_change": False,
    }, lead_id=lead["_id"], actor_type="administrator")
    return {"status": "repaired", "pipeline_stage": PipelineStage.NEW}

def manage_crm_notes(phone, note_data, action="add", *, lead_id=None,
                     actor_user_id=None, assignment_cycle_id=None):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    lead_query = {"_id": lead_id} if lead_id is not None else {"phone": {"$regex": phone_clean}}

    def _audit(note_action, note_id, timestamp):
        try:
            db["crm_events"].insert_one({
                "_id": f"crm_note:{note_action}:{lead_id}:{note_id}",
                "type": f"CRM_NOTE_{note_action.upper()}",
                "lead_id": lead_id,
                "assignment_cycle_id": assignment_cycle_id,
                "actor_user_id": actor_user_id,
                "actor": actor_user_id,
                "actor_type": "human",
                "timestamp": timestamp,
                "confirmed": False,
                "meta": {"note_id": note_id, "phone": phone_clean},
            })
        except DuplicateKeyError:
            pass

    if action == "add":
        note_id = str(uuid.uuid4())[:8]
        timestamp = note_data.get("timestamp_iso") or datetime.now(CHILE_TZ).isoformat()
        note = {
            "id": note_id, 
            "content": note_data.get("content"), 
            "color": note_data.get("color"), 
            "created_at_str": note_data.get("created_at_str") or datetime.now(CHILE_TZ).strftime("%d/%m/%Y %H:%M"),
            "timestamp_iso": timestamp,
            "lead_id": lead_id,
            "actor_user_id": actor_user_id,
            "assignment_cycle_id": assignment_cycle_id,
        }
        result = db["leads"].update_one(lead_query, {"$push": {"sticky_notes": note}})
        if result.modified_count:
            from chatbot.crm_updates import bump_crm_leads_version
            bump_crm_leads_version(db, reason="note_added", phone=phone_clean)
            _audit("added", note_id, timestamp)
        return note
    elif action == "delete":
        note_id = note_data.get("id")
        result = db["leads"].update_one(lead_query, {"$pull": {"sticky_notes": {"id": note_id}}})
        if result.modified_count:
            from chatbot.crm_updates import bump_crm_leads_version
            bump_crm_leads_version(db, reason="note_deleted", phone=phone_clean)
            _audit("deleted", note_id, datetime.now(CHILE_TZ).isoformat())
        return bool(result.modified_count)
    return False


# --- BÚSQUEDA SEMÁNTICA DE PROPIEDADES ---
def get_semantic_recommendations(query: str, exclude_codes: list = None, limit: int = 3, scope: str = 'local', include_neighbors: bool = False):
    """
    Busca propiedades semánticamente similares a la descripción del cliente.
    Usa embeddings + cosine similarity con filtros estructurados + fallback geográfico.
    scope='local' -> Solo INMOBILIARIA SUCRE SPA
    scope='global' -> Toda la red
    """
    try:
        from chatbot.rag import buscar_semanticamente
        
        oficina = "INMOBILIARIA SUCRE SPA" if scope == 'local' else None
        
        results = buscar_semanticamente(query, limit=limit, exclude_codes=exclude_codes, oficina_filtro=oficina, include_neighbors=include_neighbors)
        # Aplanar esquema anidado Prop360 a campos planos para el frontend CRM.
        results = [_aplanar_propiedad_crm(r) for r in results]
        return {"status": "ok", "results": results, "count": len(results)}
    except Exception as e:
        logger.error(f"[SEMANTIC] Error en búsqueda semántica: {e}", exc_info=True)
        return {"status": "error", "detail": str(e), "results": []}


def _aplanar_propiedad_crm(prop: dict) -> dict:
    """Aplana el esquema anidado Prop360 (tipo_operacion/ubicacion/caracteristicas)
    a los campos planos que consume el frontend CRM y los mensajes de WhatsApp:
    codigo, comuna, tipo, operacion, precio_uf, precio_clp, dormitorios, banos, m2_utiles.
    Conserva todo lo demás (score, expanded_from, _id, ...)."""
    out = dict(prop)
    to = prop.get("tipo_operacion") or {}
    ubi = prop.get("ubicacion") or {}
    car = prop.get("caracteristicas") or {}
    res = prop.get("resumen") or {}
    snap = res.get("snapshot_listado") or {}
    meta = prop.get("metadata") or {}

    tipo = to.get("tipo") or meta.get("tipo_propiedad") or snap.get("tipo") or prop.get("tipo")
    comuna = ubi.get("comuna") or snap.get("comuna") or prop.get("comuna")

    venta = to.get("venta") is True
    arriendo = to.get("arriendo") is True
    if venta:
        operacion = "Venta"
        precio = to.get("precio_venta") or {}
    elif arriendo:
        operacion = "Arriendo"
        precio = to.get("precio_arriendo") or {}
    else:
        operacion = snap.get("operacion") or prop.get("operacion") or ""
        precio = {}

    precio_uf = precio.get("precio_uf")
    precio_clp = precio.get("precio_clp")
    if precio_uf is None:
        precio_uf = prop.get("precio_uf")
    if precio_clp is None:
        precio_clp = prop.get("precio_clp")

    # Si no hay precio_uf pero sí CLP, convertir a UF (valor del Config) para que
    # el frontend muestre UF por defecto.
    if precio_uf is None and precio_clp:
        uf_valor = float(getattr(Config, "UF_VALOR_CLP", 0) or 0)
        if uf_valor > 0:
            try:
                precio_uf = round(float(precio_clp) / uf_valor, 2)
            except (TypeError, ValueError):
                precio_uf = None

    dormitorios = car.get("dormitorios")
    if dormitorios is None:
        dormitorios = prop.get("dormitorios")
    banos = car.get("banos")
    if banos is None:
        banos = prop.get("banos")

    m2_utiles = car.get("superficie_util") or car.get("superficie_construida") or car.get("superficie_total") or prop.get("m2_utiles")

    if tipo:
        out["tipo"] = tipo
    if comuna:
        out["comuna"] = comuna
    if operacion:
        out["operacion"] = operacion
    if precio_uf is not None:
        out["precio_uf"] = precio_uf
    if precio_clp is not None:
        out["precio_clp"] = precio_clp
    if dormitorios is not None:
        out["dormitorios"] = dormitorios
    if banos is not None:
        out["banos"] = banos
    if m2_utiles is not None:
        out["m2_utiles"] = m2_utiles

    return out


def log_recommendation_sent(phone: str, selected_properties: list, user_email: str):
    """
    Registra en crm_history cuando un ejecutivo envía una recomendación de propiedades.
    """
    try:
        db = get_db()
        from datetime import datetime
        now = datetime.utcnow()

        # Build summary of properties
        prop_summary = ", ".join([
            f"{p.get('tipo', 'Prop')} {p.get('codigo', '?')} ({p.get('comuna', '?')})"
            for p in selected_properties
        ])

        history_entry = {
            "timestamp": now,
            "user_action": "Recomendación de propiedades",
            "result": f"Envió {len(selected_properties)} propiedades por WhatsApp",
            "notes": prop_summary,
            "type_class": "recommendation",
            "icon_class": "semantic",
            "icon": "fa-solid fa-brain",
            "source": "crm_semantic",
            "exec_user": user_email
        }

        result = db.leads.update_one(
            {"phone": phone},
            {
                "$push": {"crm_history": {"$each": [history_entry], "$position": 0}},
                "$inc": {"semantic_search_count": 1}
            }
        )
        if result.modified_count:
            from chatbot.crm_updates import bump_crm_leads_version
            bump_crm_leads_version(db, reason="recommendation_sent", phone=phone)
        logger.info(f"[SEMANTIC] Recomendación registrada para {phone}: {prop_summary}")
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"[SEMANTIC] Error registrando recomendación: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}


