# chatbot/metrics.py
from datetime import datetime
from .constants import CHILE_TZ, PipelineStage
from .utils import calculate_business_minutes
import logging

logger = logging.getLogger(__name__)

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
        
        update_data = {
            "sla_status": sla_status,
            "priority_score": score,
            "priority_bucket": bucket,
            "last_action_label": action_label,
            "updated_at_metrics": datetime.now(CHILE_TZ).isoformat()
        }
        
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
        
    except Exception as e:
        logger.error(f"Error updating lead metrics for {phone}: {e}")

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
