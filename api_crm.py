# from pymongo import MongoClient (Replaced by singleton)
from config import Config
from datetime import datetime
import pytz
import re
import uuid
import logging

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


def _after_hours_label(assigned_raw, *, has_real_management=False):
    """Return display text for assignment time, with after-hours detection.

    After-hours (19:00-09:00, weekends) + no management → 'Asignado anoche ...'
    Otherwise → format_relative_time() result.
    """
    dt = coerce_crm_datetime(assigned_raw)
    if dt and not has_real_management:
        from chatbot.constants import BUSINESS_DAYS, BUSINESS_START_HOUR, BUSINESS_END_HOUR
        local = dt.astimezone(CHILE_TZ)
        is_after_hours = (
            local.weekday() not in BUSINESS_DAYS
            or local.hour >= BUSINESS_END_HOUR
            or local.hour < BUSINESS_START_HOUR
        )
        if is_after_hours:
            return "anoche · SLA iniciado hoy 09:00"
    return format_relative_time(assigned_raw)


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
    
    if days > 0: return f"Hace {days}d {hours}h"
    elif hours > 0: return f"Hace {hours}h {minutes}m"
    elif minutes > 0: return f"Hace {minutes}m"
    else: return "Ahora"

# --- HELPER: Datos de Propiedad ---
def get_real_property_data(db, codigo_propiedad):
    if not codigo_propiedad or codigo_propiedad == "S/N":
        return None
    prop = db[Config.COLLECTION_NAME].find_one({"codigo": str(codigo_propiedad)})
    if not prop: return None
    return {
        "codigo": prop.get("codigo"),
        "tipo": prop.get("tipo", "Propiedad"),
        "operacion": prop.get("operacion", "Venta"),
        "precio_uf": prop.get("precio_uf") or prop.get("precio", 0),
        "comuna": prop.get("comuna", ""),
        "region": prop.get("region", ""),
        "calle": prop.get("calle", ""),
        "numeracion": prop.get("numeracion", ""),
        "direccion_completa": f"{prop.get('calle', '')} #{prop.get('numeracion', '')}",
        "nombre_propietario": prop.get("nombre_propietario", "No registrado"),
        "movil_propietario": prop.get("movil_propietario") or prop.get("fono_propietario", "S/I"),
        "email_propietario": prop.get("email_propietario", "S/I"),
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
from chatbot.constants import PipelineStage, InteractionType, UNASSIGNED_LABEL
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
    state_condition = None
    
    if filtro_estado and filtro_estado != "Todos":
        if filtro_estado == "UNASSIGNED":
            state_condition = {"$and": [_crm_stage_query(CRM_STAGE_GROUPS["NEW"]), unassigned_filter]}
        elif filtro_estado in ["NEW", "nuevo"]:
            state_condition = _crm_stage_query(CRM_STAGE_GROUPS["NEW"])
        elif filtro_estado == "GRUPO_GESTION":
            state_condition = _crm_management_stage_query()
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
                {"$match": {"$and": [global_kpi_query, _crm_stage_query(CRM_STAGE_GROUPS["NEW"]), unassigned_filter]}},
                {"$count": "count"}
            ],
            "sin_asignar": [
                {"$match": {"$and": [base_kpi_query, _crm_stage_query(CRM_STAGE_GROUPS["NEW"]), unassigned_filter]}},
                {"$count": "count"}
            ],
            "nuevo": [
                {"$match": {"$and": [base_kpi_query, _crm_stage_query(CRM_STAGE_GROUPS["NEW"])]}},
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
    # Usamos lifecycle.assigned_at como fuente principal y dejamos fallback a fecha_asignacion/created_at.
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
        "antiguos_sin_atender": "oldest_unmanaged",
        "sla_por_vencer": "sla_priority",
        "mayor_sin_gestion": "oldest_unmanaged",
        "ultima_accion_antigua": "oldest_unmanaged",
        "prioridad": "sla_priority",
    }
    ordenar_por = _sort_map.get(ordenar_por, "sla_priority")
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
                "$_active_cycle.assigned_at",
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
                    {"$or": [
                        {"$ne": ["$_active_cycle.first_valid_management_at", None]},
                        {"$gt": [{"$size": {"$ifNull": ["$_active_cycle.applied_transition_ids", []]}}, 0]},
                    ]},
                    0, 1,
                ]},
                1,
            ]},
        }},
    ]

    # Build sort spec from the canonical cycle fields
    if ordenar_por == "recent_assigned":
        _sort_spec = {"_has_assigned": 1, "_cycle_assigned_at": -1, "_id": -1}
    elif ordenar_por == "sla_priority":
        _sort_spec = {
            "_has_assigned": 1,
            "_temperature": 1,          # HOT=0 first, then rest
            "_cycle_assigned_at": 1,    # oldest HOT first, then oldest COLD
            "_id": 1,
        }
    elif ordenar_por == "oldest_unmanaged":
        _sort_spec = {
            "_has_management": 1,        # unmanaged (1) first? No: 1 means no management
            "_has_assigned": 1,
            "_cycle_assigned_at": 1,
            "_id": 1,
        }
    else:
        _sort_spec = {"_has_assigned": 1, "_cycle_assigned_at": -1, "_id": -1}

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

    # Conteos y registros provienen del mismo snapshot y de una única agregación.
    facet_pipeline[0]["$facet"]["records"] = records_pipeline
    facet_results = await db["leads"].aggregate(facet_pipeline).to_list(length=1)
    facet_res = facet_results[0] if facet_results else {}
    leads_list = facet_res.get("records", [])

    def get_facet_count(key):
        return facet_res.get(key, [{"count": 0}])[0]["count"] if facet_res.get(key) else 0

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
    from chatbot.crm_metrics import validate_list_parity
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
    events_cursor = db["crm_events"].find(
        {"phone": {"$in": page_phones}, "type": {"$in": management_types}},
        sort=[("timestamp", -1)]
    )
    events_list = await events_cursor.to_list(length=200)
    events_map = {}
    recognized_management_map = {}
    # Only HUMAN_NOTE and GESTION_LOG are recognized as management events.
    # SEND/CLICK/STATUS_CHANGE are telemetry and never enter this map.
    MANAGEMENT_EVENT_TYPES = frozenset({"HUMAN_NOTE", "GESTION_LOG"})
    for ev in events_list:
        phone_ev = ev.get("phone", "").replace("+", "").strip()
        if phone_ev not in events_map:
            events_map[phone_ev] = ev
        if ev.get("type") in MANAGEMENT_EVENT_TYPES:
            recognized_management_map[phone_ev] = ev

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
            estado_db = estado_map_legacy.get(estado_db.lower(), PipelineStage.NEW)
        
        last_ev = events_map.get(raw_phone)
        recognized_management_ev = (
            recognized_management_map.get(raw_phone)
            if phone_identity_counts.get(raw_phone) == 1 else None
        )
        current_cycle = cycle_by_lead_id.get(str(lead.get("_id"))) if lead.get("_id") else None
        lifecycle = lead.get("lifecycle") or {}
        # Never combine new-cycle SLA with historical lead fields.  If a
        # canonical active cycle exists, it is the sole source for this row.
        assigned_for_cycle = _coerce_crm_datetime(
            (current_cycle or {}).get("assigned_at") or lifecycle.get("assigned_at") or lead.get("fecha_asignacion")
        )
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
        if not outreach["recognized"]:
            recognized_management_ev = None
        # Real management: canonical event OR lifecycle.first_valid_management_at
        has_real_management = (
            recognized_management_ev is not None
            or (lead.get("lifecycle") or {}).get("first_valid_management_at") is not None
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
                TYPE_LABELS.get(commercial_ev.get("type"))
                or event_meta.get("action_label")
                or event_meta.get("action")
                or "Sin gestión registrada"
            )
            last_action_note = event_meta.get("notes") or event_meta.get("note") or ""
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
        lifecycle_ts = ((current_cycle or {}).get("assigned_at")
                        or lifecycle.get("assigned_at"))
        created_ts = lead.get("created_at")
        
        # Mantener la prioridad histórica de la última gestión, salvo que haya
        # una respuesta del cliente posterior a esa gestión.
        last_ts = (
            last_message_at if has_new_customer_reply else
            lead.get("last_event_at") or
            (commercial_ev.get("timestamp") if commercial_ev else None) or
            lifecycle_ts or ultimo_msg_ts or created_ts
        )
        
        estado_final = estado_db
        if recognized_management_ev and estado_final == PipelineStage.NEW:
            estado_final = PipelineStage.CONTACTED
        
        # Identificar ejecutivo y timestamp real para visualización
        ejecutivo = ((current_cycle or {}).get("assigned_to_display_name")
                     or lead.get("ejecutivo_asignado") or lead.get("prospecto", {}).get("ejecutivo"))
        sort_ts = ((current_cycle or {}).get("assigned_at")
                   or lead.get("effective_assigned_at") or lifecycle.get("assigned_at")
                   or lead.get("fecha_asignacion") or lead.get("created_at"))
        
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
        assigned_at = ((current_cycle or {}).get("sla_started_at")
                       or (current_cycle or {}).get("assigned_at")
                       or lifecycle.get("sla_started_at") or lifecycle.get("assigned_at")
                       or lead.get("fecha_asignacion"))
        visual_pre = is_pre_visual_cutover((current_cycle or {}).get("assigned_at") or assigned_at) if assigned_at else True
        
        sla_hours = 0
        canonical_sla = {}
        hot_started_at = None
        sla_info = {}
        
        if not assigned_at:
            sla_status = "historical" if visual_pre else "unknown"
            sla_label = "Histórico" if visual_pre else "SLA S/I"
        else:
            # The current canonical cycle was resolved in the batch query.
            if current_cycle is not None and isinstance(current_cycle, dict):
                hot_started_at = current_cycle.get("hot_started_at") or current_cycle.get("temperature_transitioned_at")
            
            canonical_sla = calculate_sla(
                assigned_at=assigned_at,
                first_valid_management_at=(lifecycle.get("first_valid_management_at")
                                          if lifecycle.get("current_assignment_cycle_id") == current_cycle_id else None) or
                                          (outreach["occurred_at"] if recognized_management_ev else None),
                temperature=temp,
                hot_started_at=hot_started_at,
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

        management_age = format_relative_time(last_ts_obj).replace("Hace", "hace", 1)
        if estado_final in (PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST):
            age_label = f"Cerrado {format_relative_time(lifecycle_ts or created_ts).replace('Hace', 'hace', 1)}"
        elif last_action_text != "Sin gestión registrada":
            age_label = f"Última gestión {management_age}"
        else:
            assigned_age = _after_hours_label(lifecycle_ts or created_ts, has_real_management=has_real_management)
            if assigned_age.startswith("anoche"):
                age_label = f"Asignado {assigned_age}"
            elif estado_final == PipelineStage.NEW:
                age_label = f"Sin atender {format_relative_time(lifecycle_ts or created_ts).replace('Hace', 'hace', 1)}"
            else:
                age_label = f"Asignado {assigned_age}"

        leads_procesados.append({
            "phone": raw_phone,
            "sla_status": sla_status,
            "sla_label": sla_label,
            "age_label": age_label,
            "whatsapp_display": f"+{raw_phone}",
            "nombre": lead.get("prospecto", {}).get("nombre") or "Desconocido",
            "prioridad_badge": prioridad_badge,
            "lead_temperature_effective": temp,
            "estado": estado_final,
            "estado_badge": config_estado["label"],
            "led_class": config_estado["led"],
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
            "fecha_asignacion_relativa": _after_hours_label(lifecycle_ts or lead.get("fecha_asignacion"), has_real_management=has_real_management),
            "assignment_cycle_id": current_cycle_id,
            "assigned_at": assigned_for_cycle,
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
def get_lead_detail_data(phone, property_code=None):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
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
    for evt in new_events_cursor:
        meta = evt.get("meta", {})
        # Distincion de tipo para UI y filtro de ruido
        evt_type = evt.get("type")
        if evt_type in ["CLICK_WHATSAPP_LEAD", "ASSIGNMENT", "assignment", "MANUAL_ENTRY", "CLICK_WHATSAPP_OWNER"]:
            continue
            
        display_type = "system" if evt_type == "STATUS_CHANGE" else "user"
        
        ts_obj = evt["timestamp"]
        if isinstance(ts_obj, str):
            try: ts_obj = datetime.fromisoformat(ts_obj.replace('Z', ''))
            except: ts_obj = datetime.min
        
        if ts_obj is None: ts_obj = datetime.min
            
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
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    current_lead = db["leads"].find_one({"phone": {"$regex": phone_clean}})
    if not current_lead: return False
    
    # --- VALIDACIÓN DEL TRIÁNGULO DE CONTROL (CRITICA 1 & 3) ---
    interaction_type = data.get("interaction_type")
    result = data.get("resultado_gestion")
    next_date = data.get("next_action_date")
    actor_name = str(data.get("_actor_name") or "").strip()
    
    # Regla: Si hablé, OBLIGATORIO definir siguiente paso o cerrar
    if interaction_type == "hable" and result != "lead_cerrado":
        if not next_date:
            # Rechazar gestión incompleta (Backend Enforcement)
            print(f"⚠️ RECHAZADO: Intento de guardar 'Hablé' sin próxima fecha. Lead: {phone_clean}")
            return False 
    
    new_state = data.get("estado_calculado")
    if not new_state:
        res = data.get("resultado_gestion")
        if res == "visita_agendada": new_state = "visita"
        elif res == "lead_cerrado": new_state = "cerrado"
        elif res in ["lead_pausado", "requiere_seguimiento", "intento_fallido"]: new_state = "gestion"
        else: new_state = "gestion"

    old_state = current_lead.get("stage") or current_lead.get("crm_estado", PipelineStage.NEW)
    
    # 1. ACTUALIZACIÓN DE ESTADO VIA SERVICE (Prioridad Absoluta)
    # Forzamos promoción si es NEW y hay gestión
    if (new_state == old_state) and (old_state == PipelineStage.NEW or str(old_state).lower() in ["nuevo", "new"]):
        new_state = "gestion"

    if new_state and new_state != old_state:
        valid_stage = new_state
        if new_state == "visita": valid_stage = PipelineStage.VISIT_SCHEDULED
        elif new_state == "cerrado":
            close_cat = None
            raw_details = data.get("details_json") or {}
            if isinstance(raw_details, dict):
                close_cat = raw_details.get("close_cat_radio")
            elif isinstance(raw_details, str):
                try:
                    import json
                    close_cat = json.loads(raw_details).get("close_cat_radio")
                except (json.JSONDecodeError, TypeError):
                    pass
            valid_stage = PipelineStage.CLOSED_WON if close_cat == "ganado" else PipelineStage.CLOSED_LOST
        elif new_state == "gestion": valid_stage = PipelineStage.CONTACTED
        
        stage_updated = CrmService.update_stage(
            phone_clean, valid_stage, actor=actor_name or "agent", notes=data.get("notas")
        )

        # A valid human management must never remain as NEW just because a
        # more advanced milestone (for example VISIT_SCHEDULED) is missing a
        # required field.  Preserve the milestone validation, but fall back to
        # CONTACTED so the list and KPIs reflect that the lead was managed.
        if not stage_updated and (
            old_state == PipelineStage.NEW
            or str(old_state).lower() in {"nuevo", "new", "pipelinestage.new"}
        ):
            stage_updated = CrmService.update_stage(
                phone_clean,
                PipelineStage.CONTACTED,
                actor=actor_name or "agent",
                notes=data.get("notas"),
            )

        if not stage_updated:
            logger.error(
                "CRM management saved without a valid stage transition: phone=%s target=%s",
                phone_clean,
                valid_stage,
            )
            return False

        refreshed_lead = db["leads"].find_one({"_id": current_lead["_id"]}, {"pipeline_stage": 1})
        new_state = (refreshed_lead or {}).get("pipeline_stage") or valid_stage

    # Agendar tarea solo si hay fecha válida
    if next_date:
        schedule_crm_task(phone_clean, next_date, data.get("notas"))
    elif new_state in [PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST]:
        # Cleanup: Si se cierra el lead, resolver tareas pendientes y cerrar ciclo
        db["crm_tasks"].update_many(
            {"phone": phone_clean, "status": "pending"},
            {"$set": {"status": "completed", "resolved_at": datetime.now(), "resolution": "lead_closed"}}
        )
        # Close the active cycle idempotently
        from chatbot.crm_metrics import active_assignment_cycle
        cycle_to_close = active_assignment_cycle(db, current_lead["_id"])
        if cycle_to_close:
            db["crm_assignment_cycles"].update_one(
                {"_id": cycle_to_close["_id"], "cycle_status": "active"},
                {"$set": {"cycle_status": "closed", "closed_at": datetime.now(CHILE_TZ),
                          "closed_reason": "lead_closed", "unassigned_at": datetime.now(CHILE_TZ)}},
            )

    # Log de gestión comercial (Acción User) -> Usamos el log centralizado
    log_event(phone_clean, InteractionType.HUMAN_NOTE, actor_name or "unresolved_actor", {
        "interaction_type": interaction_type,
        "result": result,
        "notes": data.get("notas"),
        "action_label": data.get("action_label"),
        "details_json": data.get("details_json", {}),
        "meaningful_change": bool(result or data.get("notas") or next_date),
    }, lead_id=current_lead["_id"], actor_type="human", result=result,
       confirmed=bool(result))
    
    # NOTA: No actualizamos "crm_estado" manual en DB, update_stage ya lo hizo.
    # Solo actualizamos last_crm_update si no hubo cambio de estado (si hubo, update_stage lo hizo)
    if new_state == old_state:
         db["leads"].update_one(
            {"phone": {"$regex": phone_clean}},
            {"$set": {"last_crm_update": datetime.now()}} # Mantenemos datetime.now() para sorting interno de mongo si se usa
        )

    return {
        "status": "ok",
        "new_state": new_state,
        "next_action_date": next_date,
        "event_id": "centralized_log"
    }

def manage_crm_notes(phone, note_data, action="add"):
    db = get_db()
    phone_clean = phone.replace(" ", "").replace("+", "").strip()
    
    if action == "add":
        note_id = str(uuid.uuid4())[:8]
        note = {
            "id": note_id, 
            "content": note_data.get("content"), 
            "color": note_data.get("color"), 
            "created_at_str": datetime.now().strftime("%d/%m/%Y"),
            "timestamp_iso": datetime.now().isoformat()
        }
        result = db["leads"].update_one({"phone": {"$regex": phone_clean}}, {"$push": {"sticky_notes": note}})
        if result.modified_count:
            from chatbot.crm_updates import bump_crm_leads_version
            bump_crm_leads_version(db, reason="note_added", phone=phone_clean)
        return note
    elif action == "delete":
        result = db["leads"].update_one({"phone": {"$regex": phone_clean}}, {"$pull": {"sticky_notes": {"id": note_data.get("id")}}})
        if result.modified_count:
            from chatbot.crm_updates import bump_crm_leads_version
            bump_crm_leads_version(db, reason="note_deleted", phone=phone_clean)
        return True
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
        return {"status": "ok", "results": results, "count": len(results)}
    except Exception as e:
        logger.error(f"[SEMANTIC] Error en búsqueda semántica: {e}", exc_info=True)
        return {"status": "error", "detail": str(e), "results": []}


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


