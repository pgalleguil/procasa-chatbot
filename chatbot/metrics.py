# chatbot/metrics.py
from datetime import datetime
from config import Config
from .constants import CHILE_TZ, PipelineStage
from .utils import calculate_business_minutes
from .crm_metrics import calculate_sla, resolve_canonical_lead
import logging
from .lead_temperature import (
    HOT,
    derive_effective_temperature,
    effective_temperature_set,
    has_commercial_alert,
)

logger = logging.getLogger(__name__)

HOT_LEAD_NOTIFICATION_TYPE = "LeadHotWhatsapp"

def calculate_priority(lead_doc, now=None):
    """
    Calcula sla_status y priority_score en O(1) basándose solo en los datos del lead.
    """
    if not now:
        now = datetime.now(CHILE_TZ)
    lifecycle = lead_doc.get("lifecycle", {}) or {}
    canonical_sla = calculate_sla(
        assigned_at=lifecycle.get("assigned_at") or lead_doc.get("fecha_asignacion"),
        first_valid_management_at=lifecycle.get("first_valid_management_at"),
        now=now,
    )
    if canonical_sla["status"] != "unknown":
        if canonical_sla["fulfilled"]:
            return "fulfilled", 0, "DONE"
        score, bucket = {
            "critical": (120, "CRITICAL"), "near_critical": (100, "HIGH"),
            "warning": (70, "HIGH"), "good": (40, "NORMAL"),
        }[canonical_sla["status"]]
        return canonical_sla["status"], score, bucket
    
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
        "CALL_COMPLETED_LEAD", "CONTACT_RESULT",
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

def update_lead_metrics(db, phone, event_at=None, event_type=None, lead_id=None):
    """
    Actualiza los campos de performance en el documento del lead.
    """
    try:
        phone_clean = str(phone).replace("+", "").strip()
        # Find lead whether it has a + prefix or not
        lead = resolve_canonical_lead(db, lead_id=lead_id, phone=phone).lead
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
                "CALL_COMPLETED_LEAD": "Llamada realizada",
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
        old_temp = lead.get("lead_temperature_effective") or lead.get("lead_temperature")
        new_temp = derive_effective_temperature(lead)
            
        update_data = {
            "sla_status": sla_status,
            "priority_score": score,
            "priority_bucket": bucket,
            "last_action_label": action_label,
            "updated_at_metrics": datetime.now(CHILE_TZ).isoformat(),
            **effective_temperature_set(new_temp),
        }
        
        became_hot = new_temp == HOT and old_temp != HOT
        if became_hot:
            update_data["lifecycle.hot_since"] = datetime.now(CHILE_TZ).isoformat()
        
        if event_at: update_data["last_event_at"] = event_at
        if event_type: update_data["last_event_type"] = event_type
        
        # --- AUTO-PROMOTION ---
        # Si el evento es de gestión y el lead es nuevo, lo promovemos automáticamente a 'En Gestión'
        is_managed = event_type in [
            "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
            "CALL_COMPLETED_LEAD", "CONTACT_RESULT",
        ]
        
        current_stage = lead.get("pipeline_stage") or lead.get("stage") or PipelineStage.NEW
        # Stage transitions require an explicit, confirmed commercial decision.
        # Metric events (including clicks) never promote a lead implicitly.
        if False and is_managed and (current_stage == PipelineStage.NEW or str(current_stage).lower() in ["new", "nuevo"]):
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
    Canonical-only hot notification path.

    Delegates to ``assign_and_enqueue_hot()`` which writes to
    ``crm_notifications_v1``.  No documents are created in
    ``pending_notifications``.  Legacy pending_notifications documents
    are still read and finalized by the existing worker.
    """
    try:
        if not Config.LEAD_HOT_NOTIFICATIONS_ENABLED:
            logger.info("[HOT_LEAD] suppressed flag=LEAD_HOT_NOTIFICATIONS_ENABLED")
            return
        from .constants import UNASSIGNED_LABEL
        from .lead_router import get_executive_phone
        from .storage import get_db
        from .crm_metrics import active_assignment_cycle
        from .crm_hot_delivery import assign_and_enqueue_hot
        from .crm_notifications import individual_identity, COLLECTION as NOTIF_COLL

        prospecto = lead.get("prospecto", {}) or {}
        alerts_sent = prospecto.get("alerts_sent") or {}
        if has_commercial_alert(alerts_sent):
            logger.info("[HOT_LEAD] %s ya tenia alerta comercial.", lead.get("phone"))
            return

        exec_name = lead.get("ejecutivo_asignado") or prospecto.get("ejecutivo")
        unassigned = {UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", "Sin asignar", "N/A", "", None}
        if exec_name in unassigned:
            logger.info("[HOT_LEAD] %s HOT sin ejecutivo.", lead.get("phone"))
            return

        db = get_db()
        cycle = active_assignment_cycle(db, lead["_id"])
        if not cycle:
            logger.info("[HOT_LEAD] %s HOT sin ciclo.", lead.get("phone"))
            return

        recipient_user_id = str(cycle.get("assigned_to_user_id") or "")
        assignment_cycle_id = str(cycle.get("assignment_cycle_id") or "")

        identity = individual_identity(
            lead_id=lead["_id"], assignment_cycle_id=assignment_cycle_id,
            notification_type=HOT_LEAD_NOTIFICATION_TYPE, recipient_user_id=recipient_user_id,
        )
        if db[NOTIF_COLL].find_one({"individual_identity": identity, "state": {"$in": ["pending", "sending", "sent"]}}):
            logger.info("[HOT_LEAD] %s ya tiene notificacion HOT canónica.", lead.get("phone"))
            return

        try:
            from .crm_non_hot_digest import exclude_from_open_digest
            exclude_from_open_digest(db, lead_id=lead["_id"], assignment_cycle_id=assignment_cycle_id)
        except Exception:
            pass

        exec_phone = get_executive_phone(exec_name)
        if not exec_phone or exec_phone == "+56900000000":
            logger.warning("[HOT_LEAD] %s HOT pero %s sin telefono.", lead.get("phone"), exec_name)
            return

        property_code = (
            lead.get("property_code") or lead.get("codigo")
            or prospecto.get("codigo") or prospecto.get("codigo_interno")
        )
        messages = lead.get("messages") or []
        last_message = messages[-1].get("content", "") if messages else ""

        last_intent = str(lead.get("last_intent") or "").upper()
        stage = str(lead.get("pipeline_stage") or lead.get("stage") or "").upper()
        if last_intent in {"ASK_VISIT", "ASK_CONTACT", "GIVE_OFFER"}:
            hot_reason = f"Intenci\u00f3n: {last_intent}"
        elif stage in {"VISIT_SCHEDULED", "VISIT_DONE", "OFFER", "NEGOTIATION"}:
            hot_reason = f"Etapa: {stage}"
        elif has_commercial_alert(alerts_sent):
            hot_reason = "Alerta comercial previa"
        else:
            hot_reason = "Clasificaci\u00f3n autom\u00e1tica"

        payload = {
            "phone": lead.get("phone"), "lead_phone": lead.get("phone"),
            "property_code": property_code, "lead_type": HOT_LEAD_NOTIFICATION_TYPE,
            "target_name": exec_name, "target_phone": exec_phone,
            "nombre": prospecto.get("nombre") or lead.get("nombre") or "Cliente",
            "last_message": last_message or "El lead se convirtio en HOT.",
            "is_new_assignment": False, "lead_temperature": "HOT", "hot_reason": hot_reason,
        }

        assign_and_enqueue_hot(
            db, lead=lead, recipient_user_id=recipient_user_id,
            recipient_phone=exec_phone, payload=payload,
            assigned_by="system", reason="HOT_transition", recipient_name=exec_name,
        )
        logger.info("[HOT_LEAD] Can\u00f3nica creada para %s por %s (%s).", exec_name, lead.get("phone"), hot_reason)
    except Exception as e:
        logger.error("[HOT_LEAD] Error: %s para %s", e, lead.get("phone"), exc_info=True)

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
        
        doc = db[Config.CAPTACION_COLLECTION_NAME].find_one({"_id": query_id})
        if not doc: return
        
        details = doc.get("details", {})
        c = details.get("comuna")
        t = details.get("tipo_propiedad", "Departamento")
        
        market = get_market_insights(c, t)
        score, prob, motivos, uf_m2, diff_pct = calculate_lead_score_captacion(details, market)
        
        db[Config.CAPTACION_COLLECTION_NAME].update_one(
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
