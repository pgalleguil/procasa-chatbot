# chatbot/manual_entry.py
import logging
from datetime import datetime
from typing import Dict, Any, Tuple, Optional
from .storage import get_db, log_event, save_pending_notification
from .constants import CHILE_TZ, PipelineStage, InteractionType
from .lead_router import find_responsible_executive

logger = logging.getLogger(__name__)

def check_lead_duplicate(phone: Optional[str], property_code: str, email: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """
    Checks if a lead with the same phone (or email) and property code already exists.
    Returns (exists, executive_name).
    """
    db = get_db()
    property_code = str(property_code).strip()
    
    query_filters = []
    
    if phone:
        # Limpiamos absolutamente todo excepto los dígitos
        import re
        phone_digits = re.sub(r"\D", "", str(phone))
        
        if phone_digits:
            # Si el usuario ingresó 9 dígitos (ej: 990152481), buscamos que coincida con el final
            # Esto permite que "990152481" coincida con "56990152481"
            if len(phone_digits) == 9:
                query_filters.append({"phone": {"$regex": phone_digits + "$"}})
            else:
                # Si ingresó más (ej: 569...), buscamos coincidencia exacta de los dígitos
                query_filters.append({"phone": {"$regex": phone_digits}})
    
    if email:
        email_clean = str(email).strip().lower()
        if email_clean:
            query_filters.append({"prospecto.email": email_clean})

    if not query_filters:
        return False, None

    query = {
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
    
    existing = db["leads"].find_one(query)
    if existing:
        from .constants import UNASSIGNED_LABEL
        exec_name = existing.get("ejecutivo_asignado") or existing.get("prospecto", {}).get("ejecutivo") or UNASSIGNED_LABEL
        return True, exec_name
    
    return False, None

def create_manual_lead(data: Dict[str, Any]) -> Dict[str, Any]:
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
    is_dup, executive = check_lead_duplicate(phone, property_code, email)
    if is_dup:
        return {"status": "error", "message": f"Este contacto ya existe para esta propiedad y está asignado a {executive}"}

    # 2. Assignment Logic
    exec_name, exec_phone = find_responsible_executive(property_code)
    
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

    # 4. Save to DB
    try:
        result = db["leads"].insert_one(lead_doc)
        lead_id = str(result.inserted_id)
        
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
