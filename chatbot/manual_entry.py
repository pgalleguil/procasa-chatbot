# chatbot/manual_entry.py
import logging
from datetime import datetime
from typing import Dict, Any, Tuple, Optional
from .storage import get_db, log_event, save_pending_notification
from .constants import CHILE_TZ, PipelineStage, InteractionType
from .lead_router import find_responsible_executive
from .processing_service import LeadProcessingService

logger = logging.getLogger(__name__)


def resolve_property_code(raw_code: str) -> Dict[str, Any]:
    """
    Resuelve un código ingresado por usuario a código interno de Procasa.
    - Limpia puntos y espacios.
    - Si coincide por `codigo`, devuelve ese código.
    - Si no, intenta por `codigo_internacional` y `publicaciones.codigo_internacional`.
    """
    db = get_db()
    code = str(raw_code or "").strip().replace(".", "").replace(" ", "")
    if not code:
        return {"status": "error", "message": "Código vacío"}

    # 1) Búsqueda directa por código interno
    prop = db["universo_cartera"].find_one(
        {"$or": [{"codigo": code}, {"codigo": str(code)}]},
        {"codigo": 1}
    )
    if prop and prop.get("codigo"):
        return {
            "status": "ok",
            "property_code": str(prop.get("codigo")),
            "matched_by": "codigo"
        }

    # 2) Búsqueda por códigos internacionales (ej: TocToc)
    prop = db["universo_cartera"].find_one(
        {
            "$or": [
                {"codigo_internacional": code},
                {"codigo_internacional": str(code)},
                {"publicaciones.codigo_internacional": code},
                {"publicaciones.codigo_internacional": str(code)}
            ]
        },
        {"codigo": 1}
    )
    if prop and prop.get("codigo"):
        return {
            "status": "ok",
            "property_code": str(prop.get("codigo")),
            "matched_by": "codigo_internacional"
        }

    return {
        "status": "not_found",
        "message": f"No existe propiedad para el código '{code}'"
    }

def check_lead_duplicate(phone: Optional[str], property_code: str, email: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """
    Verifica si existe un lead por teléfono/email y propiedad.
    Retorna (status, executive_name). 
    Status: 'not_found', 'duplicate_same_property', 'existing_other_property'
    """
    db = get_db()
    property_code = str(property_code).strip()
    
    # 1. Normalización de teléfono a los últimos 9 dígitos (Standard Chile)
    phone_last_9 = None
    if phone:
        digits = "".join(filter(str.isdigit, str(phone)))
        if len(digits) >= 9:
            phone_last_9 = digits[-9:]

    # 2. Búsqueda por Email (exacto) o Teléfono (últimos 9 dígitos)
    query_filters = []
    if phone_last_9:
        query_filters.append({"phone": {"$regex": phone_last_9 + "$"}})
    if email:
        email_clean = str(email).strip().lower()
        if email_clean:
            query_filters.append({"prospecto.email": email_clean})

    if not query_filters:
        return "not_found", None

    # PASO 1: Buscar coincidencia TOTAL (Mismo contacto Y Misma propiedad)
    dup_query = {
        "$or": query_filters,
        "$and": [
            {
                "$or": [
                    {"prospecto.codigo": property_code},
                    {"prospecto.codigo": str(property_code)},
                    {"datos_propiedad.codigo": property_code},
                    {"datos_propiedad.codigo": str(property_code)}
                ]
            }
        ]
    }
    
    existing_same = db["leads"].find_one(dup_query)
    if existing_same:
        from .constants import UNASSIGNED_LABEL
        exec_name = existing_same.get("ejecutivo_asignado") or existing_same.get("prospecto", {}).get("ejecutivo") or UNASSIGNED_LABEL
        return "duplicate_same_property", exec_name
    
    # PASO 2: Buscar coincidencia PARCIAL (Mismo contacto pero OTRA propiedad)
    existing_other = db["leads"].find_one({"$or": query_filters})
    if existing_other:
        exec_name = existing_other.get("ejecutivo_asignado") or "Desconocido"
        return "existing_other_property", exec_name

    return "not_found", None

def create_manual_lead(data: Dict[str, Any], background_tasks=None) -> Dict[str, Any]:
    """
    Orchestrates the creation of a manual lead.
    Expected data: phone (optional), property_code, name, email, origen
    """
    db = get_db()
    import re
    phone_digits = re.sub(r"\D", "", data.get("phone", ""))
    
    # Normalización para Chile: si tiene 9 dígitos, anteponemos +56
    # Si ya tiene prefijo o es internacional, nos aseguramos que empiece con +
    if len(phone_digits) == 9:
        phone = "+56" + phone_digits
    elif phone_digits:
        phone = "+" + phone_digits
    else:
        phone = None

    property_code = str(data.get("property_code", "")).strip()
    name = data.get("nombre", "").strip()
    email = data.get("email", "").strip()
    mensaje = data.get("mensaje", "").strip()
    origen = data.get("origen", data.get("channel", "Manual")) # Fallback to channel if origen missing

    if not property_code:
        return {"status": "error", "message": "Código de Propiedad es obligatorio"}
    
    if not phone and not email:
        return {"status": "error", "message": "Debe proporcionar al menos un Teléfono o un Email"}

    # 1. Final duplicate check (phone or email + property_code)
    status, executive = check_lead_duplicate(phone, property_code, email)
    if status == "duplicate_same_property":
        return {"status": "error", "message": f"Este contacto ya existe para esta propiedad y está asignado a {executive}"}

    # 3. Asignar ejecutivo
    exec_name, exec_phone, assignment_type = find_responsible_executive(
        property_code=property_code,
        lead_phone=phone,
        lead_name=name
    )
    
    # 3. Prepare Lead Document
    now = datetime.now(CHILE_TZ)
    final_phone = phone or f"no-phone-{now.timestamp()}"
    
    messages = [
        {
            "role": "system",
            "content": f"Lead ingresado manualmente. Origen: {origen}",
            "timestamp": now.isoformat()
        }
    ]

    # Si el usuario ingresó un mensaje inicial, lo agregamos como primer mensaje del usuario
    if mensaje:
        messages.append({
            "role": "user",
            "content": mensaje,
            "timestamp": now.isoformat()
        })

    from .lead_router import get_next_business_slot
    assigned_at = get_next_business_slot(now)

    lead_doc = {
        "phone": final_phone, 
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "last_crm_update": now.isoformat(),
        "source_type": "manual",
        "origen": origen,
        "stage": PipelineStage.NEW,
        "pipeline_stage": PipelineStage.NEW,
        "ejecutivo_asignado": exec_name,
        "prospecto": {
            "nombre": name,
            "email": email,
            "phone": phone,
            "codigo": property_code,
            "ejecutivo": exec_name,
            "canal_origen": origen
        },
        "lifecycle": {
            "created_at": now.isoformat(),
            "assigned_at": assigned_at.isoformat()
        },
        "messages": messages,
        "stage_history": [
            {
                "from": None,
                "to": PipelineStage.NEW,
                "actor": "supervisor",
                "timestamp": now.isoformat(),
                "notes": "Ingreso manual"
            }
        ]
    }

    # 4. Chequear si el lead ya existe (cualquier propiedad)
    existing_query = []
    if final_phone: existing_query.append({"phone": final_phone})
    if email: existing_query.append({"prospecto.email": email})
    
    existing_lead = None
    if existing_query:
        existing_lead = db["leads"].find_one({"$or": existing_query})

    try:
        if existing_lead:
            lead_id = str(existing_lead["_id"])
            # Update sequence for returning user
            update_payload = {
                "$set": {
                    "last_crm_update": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "origen": origen,
                    "stage": PipelineStage.NEW, # Bump to NEW so it flags as Sin Atender 
                    "pipeline_stage": PipelineStage.NEW,
                    "prospecto.codigo": property_code, # Update active property
                    "prospecto.canal_origen": origen
                },
                "$push": {
                    "messages": {
                        "role": "system",
                        "content": f"El cliente se interesó en una nueva propiedad ({property_code}) vía {origen}.",
                        "timestamp": now.isoformat()
                    }
                }
            }
            if mensaje:
                msg_system = update_payload["$push"]["messages"]
                update_payload["$push"]["messages"] = {
                    "$each": [
                        msg_system,
                        {"role": "user", "content": mensaje, "timestamp": now.isoformat()}
                    ]
                }
            
            # Lógica de ejecutivo: Priorizamos SIEMPRE mantener al ejecutivo que ya lo está atendiendo.
            current_exec = existing_lead.get("ejecutivo_asignado") or existing_lead.get("prospecto", {}).get("ejecutivo")
            from .constants import UNASSIGNED_LABEL
            if current_exec and current_exec not in [UNASSIGNED_LABEL, "No asignado"]:
                # Respetamos que ya tiene un ejecutivo (ej: Raquel) aunque la propiedad sea de otro (ej: Erika)
                exec_name = current_exec
                from .lead_router import get_executive_phone
                exec_phone = get_executive_phone(exec_name)
                logger.info(f"[MANUAL] Lead existente. Manteniendo ejecutivo actual: {exec_name}")
            else:
                # Si no tiene ejecutivo o está desasignado, le damos el de la propiedad
                update_payload["$set"]["ejecutivo_asignado"] = exec_name
                update_payload["$set"]["prospecto.ejecutivo"] = exec_name
                update_payload["$set"]["lifecycle.assigned_at"] = assigned_at.isoformat()
                logger.info(f"[MANUAL] Lead sin ejecutivo previo. Asignando a dueño de ficha: {exec_name}")

            db["leads"].update_one({"_id": existing_lead["_id"]}, update_payload)
            lead_id = str(existing_lead["_id"])
            if background_tasks:
                background_tasks.add_task(LeadProcessingService.process_lead, existing_lead["_id"])
            else:
                LeadProcessingService.process_lead(existing_lead["_id"])
            
        else:
            # Insert brand new lead
            result = db["leads"].insert_one(lead_doc)
            lead_id = str(result.inserted_id)
            if background_tasks:
                background_tasks.add_task(LeadProcessingService.process_lead, result.inserted_id)
            else:
                LeadProcessingService.process_lead(result.inserted_id)
            
        # 5. Log Event
        log_event(phone or email, InteractionType.ASSIGNMENT, "supervisor", {
            "executive": exec_name,
            "method": "manual_entry",
            "property_code": property_code,
            "source": origen
        })
        
        log_event(phone or email, "MANUAL_ENTRY", "supervisor", {
            "property_code": property_code,
            "origen": origen
        })

        # 6. Notify Executive
        notification_data = {
            "phone": final_phone,
            "email": email,
            "target_phone": exec_phone,
            "target_name": exec_name,
            "property_code": property_code,
            "prospecto_nombre": name,
            "canal": origen
        }
        save_pending_notification(notification_data)

        return {
            "status": "ok", 
            "message": "Lead creado exitosamente", 
            "lead_id": lead_id,
            "assigned_to": exec_name,
            "exec_phone": exec_phone,
            "phone": final_phone,
            "property_code": property_code
        }
    except Exception as e:
        logger.error(f"Error creating manual lead: {e}")
        return {"status": "error", "message": str(e)}
