# chatbot/crm_service.py
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from .constants import (
    PipelineStage, LeadSource, InteractionType, STAGE_LABELS, 
    ALLOWED_TRANSITIONS, LeadIntent, BOT_ALLOWED_STAGES, STAGE_REQUIREMENTS
)
from .storage import get_db, COLLECTION_CONVERSATIONS, log_event
from .constants import CHILE_TZ

logger = logging.getLogger(__name__)

class CrmService:
    @staticmethod
    def get_lead(phone: str) -> Optional[Dict]:
        """
        Retrieves a normalized lead object. 
        Ensures 'stage' exists, defaults to NEW if missing.
        """
        db = get_db()
        doc = db[COLLECTION_CONVERSATIONS].find_one({"phone": phone})
        if not doc:
            return None
        
        # Normalization on read
        if "stage" not in doc:
            doc["stage"] = PipelineStage.NEW
            
        return doc

    @staticmethod
    def update_stage(phone: str, new_stage: PipelineStage, actor: str = "system", notes: str = None) -> bool:
        """
        Centralizes ALL state changes.
        - Validates transition (soft warning)
        - Updates timestamps
        - Appends to stage_history
        - Logs event
        """
        db = get_db()
        lead = CrmService.get_lead(phone)
        if not lead:
            logger.error(f"Cannot update stage for unknown lead: {phone}")
            return False

        old_stage = lead.get("stage", PipelineStage.NEW)
        
        # ============================================================================
        # ENTERPRISE VALIDATION RULES
        # ============================================================================
        
        # Rule 1: Bot can only set specific stages (NEW, CONTACTED, CLOSED_LOST)
        # All operational milestones require human confirmation
        if actor == "bot" and new_stage not in BOT_ALLOWED_STAGES:
            logger.warning(
                f"[ENTERPRISE RULE] Bot attempted to set stage={new_stage} for {phone}. "
                f"Only human actors can confirm operational milestones. Blocked."
            )
            return False
        
        # Rule 2: Validate required fields for critical stages
        if new_stage in STAGE_REQUIREMENTS:
            requirements = STAGE_REQUIREMENTS[new_stage]
            
            # Skip validation if marked as optional
            if not requirements.get("optional", False):
                required_fields = requirements["required_fields"]
                missing_fields = [f for f in required_fields if not lead.get(f)]
                
                if missing_fields:
                    logger.error(
                        f"[ENTERPRISE RULE] Cannot move {phone} to {new_stage}. "
                        f"Missing required fields: {missing_fields}. "
                        f"Reason: {requirements['description']}"
                    )
                    return False

        if old_stage == new_stage: return True # Sin cambios
        now_cl = datetime.now(CHILE_TZ)
        now_iso = now_cl.isoformat()
        
        update_data = {
            "stage": new_stage,
            "last_crm_update": now_cl # Native datetime for sorting if needed
        }
        
        # Lifecycle Timestamps
        if new_stage == PipelineStage.CONTACTED and not lead.get("lifecycle", {}).get("first_contact_at"):
             update_data["lifecycle.first_contact_at"] = now_iso
        elif new_stage == PipelineStage.VISIT_SCHEDULED:
             update_data["lifecycle.visit_scheduled_at"] = now_iso
        elif new_stage == PipelineStage.CLOSED_WON:
             update_data["lifecycle.closed_at"] = now_iso
        elif new_stage == PipelineStage.CLOSED_LOST:
             update_data["lifecycle.closed_at"] = now_iso

        # History Entry
        history_entry = {
            "from": old_stage,
            "to": new_stage,
            "actor": actor,
            "timestamp": now_iso,
            "notes": notes
        }

        # 3. Perform DB Update
        result = db[COLLECTION_CONVERSATIONS].update_one(
            {"phone": phone},
            {
                "$set": update_data,
                "$push": {"stage_history": history_entry}
            }
        )

        # 4. Global Event Log (Immutable)
        if result.modified_count > 0:
            log_event(phone, InteractionType.STATUS_CHANGE, actor, {
                "from": old_stage,
                "to": new_stage,
                "notes": notes
            })
            return True
        
        return False

    @staticmethod
    def update_intent(phone: str, intent: LeadIntent, actor: str = "bot") -> bool:
        """Actualiza la intención detectada del cliente sin afectar el stage operativo."""
        db = get_db()
        now_iso = datetime.now(CHILE_TZ).isoformat()
        
        result = db[COLLECTION_CONVERSATIONS].update_one(
            {"phone": phone},
            {
                "$set": {
                    "last_intent": intent,
                    "last_intent_at": now_iso,
                    "last_intent_actor": actor
                }
            }
        )
        
        if result.modified_count > 0:
            log_event(phone, InteractionType.BOT_MSG, actor, {
                "action": "intent_detected",
                "intent": intent
            })
            return True
        return False

    @staticmethod
    def calculate_score(lead: Dict) -> int:
        """Calcula el score de lead priorizando identidad, engagement e intención (LeadIntent)."""
        score = 0
        prospecto = lead.get("prospecto", {})
        
        # 1. Identidad (Hechos)
        if prospecto.get("email"): score += 10
        if prospecto.get("rut"): score += 15
        if prospecto.get("nombre"): score += 5
        
        # 2. Engagement
        msg_count = len(lead.get("messages", []))
        if msg_count > 10: score += 15
        elif msg_count > 5: score += 5
        
        # 3. Intención (LeadIntent - Datos de Negocio)
        intent = lead.get("last_intent")
        if intent == LeadIntent.ASK_VISIT: score += 40
        elif intent == LeadIntent.GIVE_OFFER: score += 35
        elif intent == LeadIntent.ASK_PRICE: score += 20
        elif intent == LeadIntent.ASK_INFO: score += 15
        
        # Fallback legacy
        if not intent and prospecto.get("intencion") == "agendar_visita": score += 30
        
        return score

    @staticmethod
    def assign_executive(phone: str, executive_name: str, method: str = "manual") -> bool:
        db = get_db()
        from .lead_router import get_next_business_slot
        
        now_cl = datetime.now(CHILE_TZ)
        assigned_at = get_next_business_slot(now_cl)
        
        res = db[COLLECTION_CONVERSATIONS].update_one(
            {"phone": phone},
            {
                "$set": {
                    "ejecutivo_asignado": executive_name,
                    "prospecto.ejecutivo": executive_name,
                    "lifecycle.assigned_at": assigned_at.isoformat()
                }
            }
        )
        
        if res.modified_count > 0:
            log_event(phone, InteractionType.ASSIGNMENT, "system", {
                "executive": executive_name,
                "method": method
            })
            return True
        return False
