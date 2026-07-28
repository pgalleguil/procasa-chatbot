# chatbot/manual_entry.py
import logging
from datetime import datetime
from typing import Dict, Any, Tuple, Optional
import re
from bson.objectid import ObjectId
from .storage import get_db, log_event
from .constants import CHILE_TZ, PipelineStage, InteractionType
from .lead_router import find_responsible_executive
from .processing_service import LeadProcessingService
from .phone_utils import normalize_phone_strict
from .property_lookup import (
    PROPERTY_COLLECTION_NAME,
    build_property_lookup_queries,
    find_property_by_any_identifier,
    get_prop_location,
    get_prop_operation,
)

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
    logger.info(
        "[MANUAL_RESOLVE] raw_code=%r normalized=%r collection=%s",
        raw_code,
        code,
        PROPERTY_COLLECTION_NAME,
    )
    if not code:
        return {"status": "error", "message": "Código vacío"}

    # Búsqueda robusta únicamente en la colección nueva.
    lookup_queries = build_property_lookup_queries(code)
    logger.info(
        "[MANUAL_RESOLVE] queries=%s first_queries=%s",
        len(lookup_queries),
        lookup_queries[:8],
    )

    prop = None
    matched_collection = PROPERTY_COLLECTION_NAME
    matched_query = None
    for query in lookup_queries:
        prop = db[PROPERTY_COLLECTION_NAME].find_one(query)
        if prop:
            matched_query = query
            break

    if prop and prop.get("codigo"):
        matched_by = "unknown"
        if matched_query:
            qstr = str(matched_query)
            if "source_url" in qstr or "metadata.source_url" in qstr:
                matched_by = "source_url"
            elif "codigo_internacional" in qstr:
                matched_by = "codigo_internacional"
            elif "codigo_pi" in qstr or "codigo_mercadolibre" in qstr:
                matched_by = "codigo_externo"
            elif "ubicacion." in qstr or "estado." in qstr:
                matched_by = "nested_field"
            else:
                matched_by = "codigo"
        logger.info(
            "[MANUAL_RESOLVE] matched codigo=%s collection=%s matched_by=%s matched_query=%s",
            prop.get("codigo"),
            matched_collection,
            matched_by,
            matched_query,
        )
        return {
            "status": "ok",
            "property_code": str(prop.get("codigo")),
            "matched_by": matched_by,
            "collection": matched_collection,
        }

    logger.warning(
        "[MANUAL_RESOLVE] not_found code=%s collection=%s",
        code,
        PROPERTY_COLLECTION_NAME,
    )
    logger.info(
        "[MANUAL_RESOLVE] samples=%s",
        [q for q in lookup_queries[:10]],
    )
    return {
        "status": "not_found",
        "message": f"No existe propiedad para el código '{code}'",
        "collection": PROPERTY_COLLECTION_NAME
    }

def check_lead_duplicate(phone: Optional[str], property_code: str, email: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """
    Verifica si existe un lead por teléfono/email y propiedad.
    Retorna (status, executive_name). 
    Status: 'not_found', 'duplicate_same_property', 'existing_other_property'
    """
    db = get_db()
    property_code = str(property_code).strip()
    
    # 1. Normalización flexible de teléfono:
    # - Conserva teléfonos internacionales completos cuando vienen en E.164.
    # - Para compatibilidad operativa, también generamos una versión de búsqueda
    #   por últimos dígitos para detectar el mismo contacto aunque cambie el formato.
    phone_last_9 = None
    phone_digits = None
    if phone:
        phone_str = str(phone).strip()
        phone_digits = "".join(filter(str.isdigit, phone_str))
        if len(phone_digits) >= 9:
            phone_last_9 = phone_digits[-9:]

    # 2. Búsqueda por Email (exacto) o Teléfono
    query_filters = []
    if phone_digits:
        if phone_str.startswith("+"):
            query_filters.append({"phone": phone_str})
        else:
            query_filters.append({"phone": {"$regex": re.escape(phone_digits) + "$"}})
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
    raw_phone = str(data.get("phone", "") or "").strip()
    phone = normalize_phone_strict(raw_phone)

    property_code = str(data.get("property_code", "")).strip()
    name = data.get("nombre", "").strip()
    email = data.get("email", "").strip()
    mensaje = data.get("mensaje", "").strip()
    origen = data.get("origen", data.get("channel", "Manual")) # Fallback to channel if origen missing
    # All manual leads enter as Lead normal. HOT cannot be created from this screen.
    # The frontend no longer sends lead_temperature; enforce COLD regardless of payload.
    lead_temperature = "COLD"

    if not property_code:
        return {"status": "error", "message": "Código de Propiedad es obligatorio"}

    prop = find_property_by_any_identifier(db, property_code, PROPERTY_COLLECTION_NAME)
    if not prop:
        logger.warning(
            "[MANUAL_CREATE] property_missing code=%s collection=%s",
            property_code,
            PROPERTY_COLLECTION_NAME,
        )
        return {
            "status": "error",
            "message": (
                f"La propiedad '{property_code}' no existe en '{PROPERTY_COLLECTION_NAME}'. "
                "Primero actualiza la cartera y vuelve a intentar."
            ),
            "property_code": property_code,
        }
    prop_location = get_prop_location(prop)
    prop_operation = get_prop_operation(prop)
    
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
        "lead_temperature": lead_temperature,
        "lead_temperature_effective": lead_temperature,
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
                    "lead_temperature": lead_temperature,
                    "lead_temperature_effective": lead_temperature,
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
            # Create assignment cycle for the new property interest
            from .crm_metrics import create_assignment_cycle
            exec_user = db["usuarios"].find_one({"nombre": exec_name}, {"_id": 1})
            assigned_to = str(exec_user["_id"]) if exec_user else exec_name
            fresh_lead = db["leads"].find_one({"_id": existing_lead["_id"]})
            if fresh_lead:
                cycle = create_assignment_cycle(
                    db, lead=fresh_lead, assigned_to_user_id=assigned_to,
                    assigned_by="supervisor", reason="manual_lead_created",
                    assigned_at=assigned_at, assigned_to_display_name=exec_name,
                )
            
        else:
            # Insert brand new lead
            result = db["leads"].insert_one(lead_doc)
            lead_id = str(result.inserted_id)
            # Create assignment cycle immediately with explicit commercial reason
            from .crm_metrics import create_assignment_cycle
            exec_user = db["usuarios"].find_one({"nombre": exec_name}, {"_id": 1})
            assigned_to = str(exec_user["_id"]) if exec_user else exec_name
            cycle = create_assignment_cycle(
                db, lead=lead_doc, assigned_to_user_id=assigned_to,
                assigned_by="supervisor", reason="manual_lead_created",
                assigned_at=assigned_at, assigned_to_display_name=exec_name,
            )
            
        # Canonical manual source plus immediate non-HOT digest accumulation.
        # A missing/synthetic client phone is irrelevant: delivery targets the
        # active executive resolved from usuarios via the assignment cycle.
        cycle_id = (cycle or {}).get("assignment_cycle_id")
        if not cycle_id:
            logger.error(
                "[MANUAL_CREATE] cycle_creation_failed lead_id=%s assigned_to=%s property_code=%s",
                lead_id, exec_name, property_code,
            )
            raise RuntimeError("manual lead cycle was not created")
        source_event_id = f"manual_lead:{lead_id}:{cycle_id}"
        db["crm_assignment_cycles"].update_one(
            {"assignment_cycle_id": cycle_id},
            {"$set": {"source_event_id": source_event_id,
                      "source_event_verified": True,
                      "source_event_type": "MANUAL_LEAD_CREATED"}},
        )
        fresh_lead = db["leads"].find_one({"_id": ObjectId(lead_id)})
        notification = None
        if fresh_lead and str(fresh_lead.get("lead_temperature_effective") or "").upper() != "HOT":
            from .crm_non_hot_digest import accumulate_non_hot_lead
            notification = accumulate_non_hot_lead(db, lead=fresh_lead, cycle=cycle)
            if notification:
                logger.info(
                    "[MANUAL_CREATE] digest_enqueued lead_id=%s cycle_id=%s notification_id=%s "
                    "recipient_user_id=%s send_after=%s window_started_at=%s",
                    lead_id, cycle_id, notification.get("_id"),
                    notification.get("recipient_user_id"), notification.get("send_after"),
                    notification.get("window_started_at"),
                )
            else:
                logger.error(
                    "[MANUAL_CREATE] digest_not_enqueued lead_id=%s cycle_id=%s "
                    "temperature=%s notification_eligible=%s source_verified=%s",
                    lead_id, cycle_id, fresh_lead.get("lead_temperature_effective"),
                    cycle.get("notification_eligible"), cycle.get("source_event_verified"),
                )

        # 5. Log Event
        log_event(phone or email, "MANUAL_ENTRY", "supervisor", {
            "property_code": property_code,
            "origen": origen
        })

        # 6. Notification policy: manual leads enter as Lead normal (never HOT).
        # Assignment is immediate. Digest remains in shadow — no WASender call.
        logger.info(
            "[MANUAL_CREATE] lead_created phone=%s property_code=%s assigned_to=%s source=%s",
            final_phone,
            property_code,
            exec_name,
            origen,
        )

        return {
            "status": "ok", 
            "message": "Lead creado exitosamente", 
            "lead_id": lead_id,
            "assigned_to": exec_name,
            "exec_phone": exec_phone,
            "phone": final_phone,
            "property_code": property_code,
            "assignment_cycle_id": cycle_id,
            "source_event_id": source_event_id,
            "notification_id": str(notification.get("_id")) if notification else None,
            "notification_send_after": notification.get("send_after") if notification else None,
        }
    except Exception as e:
        logger.error(f"Error creating manual lead: {e}")
        return {"status": "error", "message": str(e)}
