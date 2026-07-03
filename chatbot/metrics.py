# chatbot/metrics.py
from datetime import datetime
from .constants import CHILE_TZ, PipelineStage
from .utils import calculate_business_minutes
import logging

logger = logging.getLogger(__name__)

HOT_LEAD_NOTIFICATION_TYPE = "LeadHotWhatsapp"

def calculate_priority(lead_doc, now=None):
    """
    Calcula sla_status y priority_score en O(1) basándose solo en los datos del lead.
    """
    if not now:
        now = datetime.now(CHILE_TZ)
    
    last_event_at = lead_doc.get("last_event_at")
    if isinstance(last_event_at, str):
        try:
            last_event_at = datetime.fromisoformat(last_event_at.replace('Z', '+00:00'))
        except:
            last_event_at = None
    
    # Fallbacks si no hay evento previo (usar creación o asignación)
    if not last_event_at:
        last_event_at = lead_doc.get("lifecycle", {}).get("assigned_at") or lead_doc.get("created_at")
        if isinstance(last_event_at, str):
            try: last_event_at = datetime.fromisoformat(last_event_at.replace('Z', '+00:00'))
            except: last_event_at = None

    if not last_event_at:
        return "good", 10, "NORMAL"

    # Asegurar TZ
    if last_event_at.tzinfo is None:
        last_event_at = CHILE_TZ.localize(last_event_at)

    biz_mins = calculate_business_minutes(last_event_at, now)
    stage = lead_doc.get("pipeline_stage") or lead_doc.get("stage") or PipelineStage.NEW
    
    # Lógica de SLA
    sla_status = "good"
    priority_bucket = "NORMAL"
    
    # Si ya está gestionado o cerrado, el SLA es 'fulfilled' (verde fijo)
    is_managed = lead_doc.get("last_event_type") in [
        "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
        "CLICK_PHONE_LEAD", "CLICK_WHATSAPP_LEAD", "CLICK_EMAIL_LEAD",
        "SEND_WA_OWNER", "SEND_EMAIL_OWNER", "CLICK_PHONE_OWNER", "CLICK_WHATSAPP_OWNER"
    ]
    
    if is_managed or stage != PipelineStage.NEW:
        return "fulfilled", 0, "DONE"

    # Clasificación por tiempo
    if biz_mins >= 180:
        sla_status = "critical"
        priority_bucket = "CRITICAL"
        score = 100
    elif biz_mins >= 150:
        sla_status = "near_critical"
        priority_bucket = "HIGH"
        score = 80
    elif biz_mins >= 60:
        sla_status = "warning"
        priority_bucket = "HIGH"
        score = 50
    else:
        sla_status = "good"
        priority_bucket = "NORMAL"
        score = 20

    # Boost por etapa (Nuevo es más prioritario que en gestión)
    if stage == PipelineStage.NEW:
        score += 20
    
    return sla_status, score, priority_bucket

def update_lead_metrics(db, phone, event_at=None, event_type=None):
    """
    Actualiza los campos de performance en el documento del lead.
    """
    try:
        phone_clean = str(phone).replace("+", "").strip()
        # Find lead whether it has a + prefix or not
        lead = db["leads"].find_one({"phone": {"$regex": f"\\+?{phone_clean}"}})
        if not lead:
            return
        
        # Si se pasa un nuevo evento, actualizarlo primero en memoria para el cálculo
        if event_at:
            lead["last_event_at"] = event_at
        if event_type:
            lead["last_event_type"] = event_type
            
        # Para el Backfill: si falta el evento, buscarlo una vez
        if not event_at or not event_type:
            # En crm_events los telefonos NO tienen prefijo +, los leads SI lo tienen.
            last_ev = db["crm_events"].find_one({"phone": phone_clean}, sort=[("timestamp", -1)])
            if last_ev:
                event_at = last_ev["timestamp"]
                event_type = last_ev["type"]
                lead["last_event_at"] = event_at
                lead["last_event_type"] = event_type

        sla_status, score, bucket = calculate_priority(lead)
        
        # Obtener etiquetas descriptivas del evento
        action_label = "Gestión CRM"
        action_note = ""
        
        if event_type:
            type_labels = {
                "CLICK_WHATSAPP_LEAD": "Click WhatsApp (Lead)",
                "CLICK_PHONE_LEAD": "Llamada Iniciada",
                "CLICK_EMAIL_LEAD": "Click Email (Lead)",
                "SEND_WA_LEAD": "WhatsApp Enviado",
                "SEND_EMAIL_LEAD": "Email Enviado",
                "CLICK_WHATSAPP_OWNER": "Click WhatsApp (Prop)",
                "CLICK_PHONE_OWNER": "Llamada Prop. Iniciada",
                "CLICK_EMAIL_OWNER": "Click Email (Prop)",
                "SEND_WA_OWNER": "WhatsApp Enviado (Prop)",
                "SEND_EMAIL_OWNER": "Email Enviado (Prop)",
                "STATUS_CHANGE": "Cambio de Estado",
                "HUMAN_NOTE": "Gestión Manual",
                "ASSIGNMENT": "Lead Asignado"
            }
            action_label = type_labels.get(event_type, "Acción registrada")
        
        # --- LEAD TEMPERATURE & DYNAMIC SLA ---
        # Fuente de verdad: last_intent (detectado por bot en core.py via CrmService.update_intent)
        # Fuente secundaria: pipeline_stage (confirmado manualmente por ejecutivo)
        # NOTA: bi_analytics_global es campo legacy que ya no se usa en el flujo activo.
        last_intent = str(lead.get("last_intent", "")).upper()
        pipeline_stage = str(lead.get("pipeline_stage") or lead.get("stage") or "").upper()
        old_temp = lead.get("lead_temperature")
        
        HOT_INTENT = {"ASK_VISIT", "GIVE_OFFER"}
        HOT_STAGES = {"VISIT_SCHEDULED", "VISIT_DONE", "OFFER", "NEGOTIATION"}
        
        if last_intent in HOT_INTENT or pipeline_stage in HOT_STAGES:
            new_temp = "HOT"
        elif old_temp == "HOT":
            # Si el lead ya era HOT, mantenerlo HOT a menos que se cierre o desuscriba
            if pipeline_stage in ["CLOSED_LOST", "CLOSED_WON"] or last_intent == "UNSUBSCRIBE":
                new_temp = "COLD"
            else:
                new_temp = "HOT"
        else:
            new_temp = "COLD"
            
        update_data = {
            "sla_status": sla_status,
            "priority_score": score,
            "priority_bucket": bucket,
            "last_action_label": action_label,
            "updated_at_metrics": datetime.now(CHILE_TZ).isoformat(),
            "lead_temperature": new_temp
        }
        
        became_hot = new_temp == "HOT" and old_temp != "HOT"
        if became_hot:
            update_data["lifecycle.hot_since"] = datetime.now(CHILE_TZ).isoformat()
        
        if event_at: update_data["last_event_at"] = event_at
        if event_type: update_data["last_event_type"] = event_type
        
        # --- AUTO-PROMOTION ---
        # Si el evento es de gestión y el lead es nuevo, lo promovemos automáticamente a 'En Gestión'
        is_managed = event_type in [
            "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
            "CLICK_PHONE_LEAD", "CLICK_WHATSAPP_LEAD", "CLICK_EMAIL_LEAD",
            "SEND_WA_OWNER", "SEND_EMAIL_OWNER", "CLICK_PHONE_OWNER", "CLICK_WHATSAPP_OWNER"
        ]
        
        current_stage = lead.get("pipeline_stage") or lead.get("stage") or PipelineStage.NEW
        if is_managed and (current_stage == PipelineStage.NEW or str(current_stage).lower() in ["new", "nuevo"]):
            update_data["pipeline_stage"] = PipelineStage.CONTACTED
            update_data["stage"] = "gestion"
            # Registrar cambio de estado para el historial
            try:
                from .storage import log_event
                log_event(phone, "STATUS_CHANGE", "system", {"from": current_stage, "to": PipelineStage.CONTACTED, "reason": "auto_promotion_on_gestion"})
            except: pass

        # Use _id for exact matching instead of potentially un-prefixed phone
        db["leads"].update_one({"_id": lead["_id"]}, {"$set": update_data})

        if became_hot:
            _enqueue_hot_lead_notification(lead)
        
    except Exception as e:
        logger.error(f"Error updating lead metrics for {phone}: {e}")

def _enqueue_hot_lead_notification(lead):
    """
    A lead can be assigned while still cold and become hot later as the chat evolves.
    When that transition happens, notify the already assigned executive.
    """
    try:
        from .constants import UNASSIGNED_LABEL
        from .lead_router import get_executive_phone
        from .storage import save_pending_notification

        prospecto = lead.get("prospecto", {}) or {}
        exec_name = lead.get("ejecutivo_asignado") or prospecto.get("ejecutivo")
        unassigned = {UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", "Sin asignar", "N/A", "", None}
        if exec_name in unassigned:
            logger.info(f"[HOT_LEAD] Lead {lead.get('phone')} quedo HOT pero no tiene ejecutivo asignado.")
            return

        exec_phone = get_executive_phone(exec_name)
        if not exec_phone or exec_phone == "+56900000000":
            logger.warning(f"[HOT_LEAD] Lead {lead.get('phone')} quedo HOT pero {exec_name} no tiene telefono valido.")
            return

        property_code = (
            lead.get("property_code")
            or lead.get("codigo")
            or prospecto.get("codigo")
            or prospecto.get("codigo_interno")
        )
        messages = lead.get("messages") or []
        last_message = ""
        if messages:
            last_message = str(messages[-1].get("content") or "")

        notification_data = {
            "phone": lead.get("phone"),
            "lead_phone": lead.get("phone"),
            "property_code": property_code,
            "lead_type": HOT_LEAD_NOTIFICATION_TYPE,
            "target_name": exec_name,
            "target_phone": exec_phone,
            "nombre": prospecto.get("nombre") or lead.get("nombre") or "Cliente",
            "last_message": last_message or "El lead se convirtio en HOT durante la conversacion.",
            "is_new_assignment": False,
            "lead_temperature": "HOT",
        }
        save_pending_notification(notification_data)
        logger.info(f"[HOT_LEAD] Notificacion HOT encolada para {exec_name} por lead {lead.get('phone')}.")
    except Exception as e:
        logger.error(f"[HOT_LEAD] Error encolando notificacion HOT para {lead.get('phone')}: {e}", exc_info=True)

def update_captacion_metrics(db, obj_id):
    """
    Actualiza el score_captacion y probabilidad en yapo_propiedades para lectura rápida.
    """
    try:
        try:
            from bson import ObjectId
        except ImportError:
            ObjectId = None
            
        # Try to use ObjectId if it's a valid hex string, otherwise use raw string
        query_id = obj_id
        if ObjectId and isinstance(obj_id, str) and len(obj_id) == 24:
            try: query_id = ObjectId(obj_id)
            except: pass
        elif isinstance(obj_id, str) and (len(obj_id) != 24 or not all(c in "0123456789abcdefABCDEF" for c in obj_id)):
            query_id = obj_id # Keep as string (legacy/direct IDs)
            
        from api_captacion import calculate_lead_score_captacion, get_market_insights
        
        doc = db["yapo_propiedades"].find_one({"_id": query_id})
        if not doc: return
        
        details = doc.get("details", {})
        c = details.get("comuna")
        t = details.get("tipo_propiedad", "Departamento")
        
        market = get_market_insights(c, t)
        score, prob, motivos, uf_m2, diff_pct = calculate_lead_score_captacion(details, market)
        
        db["yapo_propiedades"].update_one(
            {"_id": query_id},
            {"$set": {
                "score_captacion": score,
                "probabilidad": prob,
                "uf_m2_cache": uf_m2,
                "updated_at_metrics": datetime.now(CHILE_TZ).isoformat()
            }}
        )
    except Exception as e:
        logger.error(f"Error updating captacion metrics for {obj_id}: {e}")
