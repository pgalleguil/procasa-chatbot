import logging
import asyncio
import re
from datetime import datetime
from typing import Optional, Dict, Any, List
from .constants import (
    PipelineStage, LeadSource, InteractionType, STAGE_LABELS, 
    ALLOWED_TRANSITIONS, LeadIntent, BOT_ALLOWED_STAGES, STAGE_REQUIREMENTS
)
from .storage import get_db, COLLECTION_CONVERSATIONS, log_event
from .constants import CHILE_TZ
from .lead_temperature import derive_effective_temperature

logger = logging.getLogger(__name__)

class CrmService:
    @staticmethod
    def get_lead(phone: str) -> Optional[Dict]:
        """
        Retrieves a normalized lead object using flexible phone matching.
        Ensures 'stage' exists, defaults to NEW if missing.
        """
        db = get_db()
        phone_clean = phone.replace("+", "").strip()
        
        # Búsqueda flexible (con o sin +)
        doc = db[COLLECTION_CONVERSATIONS].find_one({
            "phone": {"$regex": f"^{re.escape(phone_clean)}|^\\+{re.escape(phone_clean)}"}
        })
        
        if not doc:
            return None
        
        # Normalization on read
        if "stage" not in doc:
            doc["stage"] = doc.get("pipeline_stage") or PipelineStage.NEW
            
        return doc

    @staticmethod
    def update_stage(phone: str, new_stage: PipelineStage, actor: str = "system", notes: Optional[str] = None) -> bool:
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
        if new_stage == PipelineStage.CONTACTED and str(notes or "").startswith("Auto-promocion por accion rapida"):
            logger.info("[CRM_METRICS] Quick-app opening does not promote lead %s", lead.get("_id"))
            return False
        
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
            "last_crm_update": now_cl, # Native datetime for sorting if needed
            "lead_temperature_effective": derive_effective_temperature(
                lead,
                overrides={"stage": new_stage, "pipeline_stage": new_stage},
            ),
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
        # Usamos el _id del lead encontrado para precisión absoluta
        result = db[COLLECTION_CONVERSATIONS].update_one(
            {"_id": lead["_id"]},
            {
                "$set": {
                    **update_data,
                    "pipeline_stage": new_stage # Sync both fields
                },
                "$push": {"stage_history": history_entry}
            }
        )

        # 4. Global Event Log (Immutable)
        if result.modified_count > 0:
            log_event(phone, InteractionType.STATUS_CHANGE, actor, {
                "from": old_stage,
                "to": new_stage,
                "notes": notes,
                "meaningful_change": True,
            }, lead_id=lead["_id"], actor_type="human" if actor not in {"system", "bot"} else actor)
            return True
        
        return False

    @staticmethod
    def update_intent(phone: str, intent: LeadIntent, actor: str = "bot") -> bool:
        """Actualiza la intención detectada del cliente sin afectar el stage operativo."""
        db = get_db()
        transition_at = datetime.now(CHILE_TZ)
        now_iso = transition_at.isoformat()
        lead = CrmService.get_lead(phone)
        if not lead:
            return False
        # Persist the canonical string value before deriving temperature.  A
        # str-backed Enum otherwise stringifies as ``LeadIntent.ASK_VISIT``
        # on some Python versions and misses the canonical HOT_INTENTS set.
        intent_value = getattr(intent, "value", intent)
        effective_temperature = derive_effective_temperature(
            lead,
            overrides={"last_intent": intent_value},
        )
        
        result = db[COLLECTION_CONVERSATIONS].update_one(
            {"_id": lead["_id"]},
            {
                "$set": {
                    "last_intent": intent_value,
                    "last_intent_at": now_iso,
                    "last_intent_actor": actor,
                    "lead_temperature_effective": effective_temperature,
                }
            }
        )
        
        if result.modified_count > 0:
            if effective_temperature == "HOT":
                # Preserve the first confirmed HOT activation instant on the
                # lead mirror. The assignment cycle uses the same transition
                # value and keeps its own first-valid segment start.
                db[COLLECTION_CONVERSATIONS].update_one(
                    {"_id": lead["_id"], "$or": [
                        {"lifecycle.hot_since": {"$exists": False}},
                        {"lifecycle.hot_since": None},
                        {"lifecycle.hot_since": ""},
                    ]},
                    {"$set": {"lifecycle.hot_since": now_iso}},
                )
                refreshed = db[COLLECTION_CONVERSATIONS].find_one(
                    {"_id": lead["_id"]}, {"lifecycle.hot_since": 1}
                ) or {}
                stored_hot_since = (refreshed.get("lifecycle") or {}).get("hot_since") or now_iso
            else:
                stored_hot_since = now_iso
            from .crm_metrics import sync_active_cycle_temperature
            sync_active_cycle_temperature(
                db, lead["_id"], temperature=effective_temperature,
                transition_at=stored_hot_since,
            )
            log_event(phone, InteractionType.BOT_MSG, actor, {
                "action": "intent_detected",
                "intent": intent_value
            })
            return True
        return False

    @staticmethod
    def reactivate_closed_lead_if_commercial(phone: str, message: str) -> bool:
        """Reopen a closed lead only for a clear new commercial signal.

        The update is compare-and-set on the closed stage, appends stage history,
        and intentionally leaves ``created_at`` and all assignment cycles intact.
        """
        from .conversation_policy import is_explicit_commercial_reactivation

        if not is_explicit_commercial_reactivation(message):
            return False
        db = get_db()
        lead = CrmService.get_lead(phone)
        if not lead:
            return False
        closed_stages = {"ARCHIVED", "CLOSED", "CLOSED_LOST", "CLOSED_WON", "SUPPRESSED"}
        stage = str(lead.get("pipeline_stage") or lead.get("stage") or "").upper()
        if stage not in closed_stages:
            return False
        observed_stage_fields = {
            field: lead.get(field)
            for field in ("pipeline_stage", "stage")
            if field in lead
        }
        if any(
            str(value or "").strip()
            and str(value).strip().upper() not in closed_stages
            for value in observed_stage_fields.values()
        ):
            return False

        stage_preconditions = []
        for field in ("pipeline_stage", "stage"):
            if field in lead:
                stage_preconditions.append({field: lead[field]})
                if lead[field] is None:
                    stage_preconditions.append({field: {"$exists": True}})
            else:
                stage_preconditions.append({field: {"$exists": False}})

        now_str = datetime.now(CHILE_TZ).isoformat()
        result = db[COLLECTION_CONVERSATIONS].update_one(
            {"_id": lead["_id"], "$and": stage_preconditions},
            {"$set": {
                "stage": PipelineStage.NEW,
                "pipeline_stage": PipelineStage.NEW,
                "archive_reason": None,
                "reactivated_at": now_str,
                "reactivation_reason": "explicit_commercial_interest",
                "last_commercial_reactivation_at": now_str,
                "last_processed_at": None,
            }, "$push": {"stage_history": {
                "from": stage,
                "to": PipelineStage.NEW,
                "actor": "customer",
                "timestamp": now_str,
                "notes": "Explicit commercial reactivation in WhatsApp; assignment history preserved.",
            }}},
        )
        return getattr(result, "matched_count", getattr(result, "modified_count", 0)) == 1

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
        elif intent == LeadIntent.ASK_CONTACT: score += 35
        elif intent == LeadIntent.GIVE_OFFER: score += 35
        elif intent == LeadIntent.ASK_PRICE: score += 20
        elif intent == LeadIntent.ASK_INFO: score += 15
        
        # Fallback legacy
        if not intent and prospecto.get("intencion") == "agendar_visita": score += 30
        
        return score

    @staticmethod
    def assign_executive(
        phone: str,
        executive_name: str,
        method: str = "manual",
        actor: str = "system",
    ) -> bool:
        db = get_db()
        lead = CrmService.get_lead(phone)
        if not lead:
            return False
        if lead and (lead.get("prospecto") or {}).get("link_detectado") is True:
            logger.info(f"[CRM_SERVICE] Lead {phone} con link_detectado=True. No se asigna ejecutivo.")
            return False
        from .lead_router import get_next_business_slot
        
        now_cl = datetime.now(CHILE_TZ)
        assigned_at = get_next_business_slot(now_cl)
        from .crm_metrics import create_assignment_cycle, coerce_utc_datetime
        assigned_at_utc = coerce_utc_datetime(assigned_at)
        try:
            recipient_user = db["usuarios"].find_one({"nombre": executive_name, "is_active": {"$ne": False}})
        except (KeyError, TypeError):
            recipient_user = None  # Lightweight adapters retain the display-name fallback.
        assigned_to_user_id = str(recipient_user["_id"]) if recipient_user and recipient_user.get("_id") is not None else executive_name
        from .crm_assignment_integrity import assignment_context, record_assignment_integrity_alert
        assignment_state = assignment_context(db, lead)
        if assignment_state.get("status") == "conflict":
            record_assignment_integrity_alert(
                db, lead_id=lead["_id"], issue_code="crm_assignment_write_blocked_by_conflict",
                evidence={"issues": assignment_state.get("issues"),
                          "requested_owner_user_id": assigned_to_user_id,
                          "signature": f"{lead['_id']}:{':'.join(assignment_state.get('issues') or [])}"},
            )
            return False
        from .crm_metrics import create_assignment_cycle, coerce_utc_datetime
        authorized_admin_transfer = method == "crm_list_admin"
        transfer_auth = (
            {"kind": "administrative", "actor": actor, "method": method}
            if authorized_admin_transfer else None
        )
        lifecycle = lead.get("lifecycle") or {}
        old_pointer = lifecycle.get("current_assignment_cycle_id")
        expected_pointer = (
            {"lifecycle.current_assignment_cycle_id": old_pointer}
            if old_pointer is not None else
            {"$or": [
                {"lifecycle.current_assignment_cycle_id": {"$exists": False}},
                {"lifecycle.current_assignment_cycle_id": None},
            ]}
        )
        old_cycle_alias = lifecycle.get("assignment_cycle_id")
        expected_cycle_alias = (
            {"lifecycle.assignment_cycle_id": old_cycle_alias}
            if old_cycle_alias is not None else
            {"$or": [
                {"lifecycle.assignment_cycle_id": {"$exists": False}},
                {"lifecycle.assignment_cycle_id": None},
            ]}
        )
        old_owner = lead.get("ejecutivo_asignado")
        expected_owner = (
            {"ejecutivo_asignado": old_owner}
            if old_owner is not None else
            {"$or": [
                {"ejecutivo_asignado": {"$exists": False}},
                {"ejecutivo_asignado": None},
                {"ejecutivo_asignado": ""},
            ]}
        )
        old_prospect_owner = (lead.get("prospecto") or {}).get("ejecutivo")
        expected_prospect_owner = (
            {"prospecto.ejecutivo": old_prospect_owner}
            if old_prospect_owner is not None else
            {"$or": [
                {"prospecto.ejecutivo": {"$exists": False}},
                {"prospecto.ejecutivo": None},
                {"prospecto.ejecutivo": ""},
            ]}
        )
        write_filter = {
            "_id": lead["_id"],
            "$and": [expected_pointer, expected_cycle_alias, expected_owner, expected_prospect_owner],
        }

        def _create_and_mirror(session=None, *, indexes_preflighted=False):
            create_options = {"session": session} if session is not None else {}
            current_cycle = create_assignment_cycle(
                db, lead=lead, assigned_to_user_id=assigned_to_user_id,
                assigned_to_display_name=executive_name,
                assigned_by=actor, reason=method, assigned_at=assigned_at_utc,
                property_code=(lead.get("prospecto") or {}).get("codigo") or lead.get("codigo"),
                authorized_transfer=authorized_admin_transfer,
                transfer_authorization=transfer_auth,
                indexes_preflighted=indexes_preflighted,
                **create_options,
            )
            if str(current_cycle.get("assigned_to_user_id") or "") != assigned_to_user_id:
                return current_cycle, None
            update_options = {"session": session} if session is not None else {}
            mirror_result = db[COLLECTION_CONVERSATIONS].update_one(
                write_filter,
                {"$set": {
                    "ejecutivo_asignado": executive_name,
                    "prospecto.ejecutivo": executive_name,
                    "lifecycle.assigned_at": current_cycle.get("assigned_at") or assigned_at_utc,
                    "lifecycle.cycle_started_at": current_cycle.get("cycle_started_at") or current_cycle.get("assigned_at") or assigned_at_utc,
                    "lifecycle.sla_started_at": current_cycle.get("sla_started_at"),
                    "lifecycle.assigned_to_user_id": assigned_to_user_id,
                    "lifecycle.assigned_to_display_name": executive_name,
                    "lifecycle.assignment_cycle_id": current_cycle["assignment_cycle_id"],
                    "lifecycle.current_assignment_cycle_id": current_cycle["assignment_cycle_id"],
                }},
                **update_options,
            )
            return current_cycle, mirror_result

        if authorized_admin_transfer:
            # Ensure the partial uniqueness barrier exists before opening the
            # transaction; index creation cannot run inside a Mongo transaction.
            from .crm_metrics import ensure_assignment_cycle_indexes
            try:
                ensure_assignment_cycle_indexes(db)
                client = getattr(db, "client", None)
                if client is None or not callable(getattr(client, "start_session", None)):
                    raise RuntimeError("transactional admin transfer is unavailable")
                with client.start_session() as session:
                    with session.start_transaction():
                        cycle, res = _create_and_mirror(session, indexes_preflighted=True)
                        if res is None or getattr(res, "matched_count", 0) != 1:
                            raise RuntimeError("assignment mirrors changed during administrative transfer")
            except Exception as exc:
                try:
                    from .crm_assignment_integrity import record_assignment_integrity_alert
                    record_assignment_integrity_alert(
                        db, lead_id=lead["_id"],
                        issue_code="administrative_transfer_transaction_failed",
                        evidence={"reason": type(exc).__name__,
                                  "signature": f"{lead['_id']}:{method}:{executive_name}"},
                    )
                except Exception:
                    pass
                logger.warning("[CRM_ASSIGNMENT] administrative transfer aborted lead_id=%s reason=%s",
                               lead.get("_id"), type(exc).__name__)
                return False
        else:
            try:
                cycle, res = _create_and_mirror()
            except Exception as exc:
                logger.warning("[CRM_ASSIGNMENT] assignment failed lead_id=%s reason=%s",
                               lead.get("_id"), type(exc).__name__)
                return False
            if res is None or getattr(res, "modified_count", 0) == 0:
                logger.warning(
                    "[CRM_ASSIGNMENT] requested transfer lost cycle CAS lead_id=%s cycle_id=%s",
                    lead.get("_id"), cycle.get("assignment_cycle_id"),
                )
                return False

        if res.modified_count > 0:
            log_event(phone, InteractionType.ASSIGNMENT, actor, {
                "executive": executive_name,
                "method": method,
                "assignment_cycle_id": cycle["assignment_cycle_id"],
            }, lead_id=lead["_id"], actor_type="human" if actor not in {"system", "bot"} else actor)

            # Accumulate non-HOT leads in digest window
            if str(lead.get("lead_temperature_effective") or "").upper() != "HOT":
                try:
                    from .crm_non_hot_digest import accumulate_non_hot_lead
                    fresh_lead = db[COLLECTION_CONVERSATIONS].find_one({"_id": lead["_id"]},
                        {"_id": 1, "lead_temperature_effective": 1, "prospecto": 1, "phone": 1})
                    if fresh_lead:
                        accumulate_non_hot_lead(db, lead=fresh_lead, cycle=cycle)
                except Exception:
                    pass

            return True
        return False

    @staticmethod
    def archive_lead(phone: str, actor: str, reason: str = "Archivo administrativo") -> bool:
        """Archive a lead without deleting its commercial data or audit history."""
        db = get_db()
        lead = CrmService.get_lead(phone)
        if not lead:
            return False

        now_cl = datetime.now(CHILE_TZ)
        old_stage = lead.get("stage") or lead.get("pipeline_stage") or PipelineStage.NEW
        result = db[COLLECTION_CONVERSATIONS].update_one(
            {"_id": lead["_id"]},
            {
                "$set": {
                    "stage": "ARCHIVED",
                    "pipeline_stage": "ARCHIVED",
                    "archived_at": now_cl,
                    "archived_by": actor,
                    "archive_reason": reason,
                    "last_crm_update": now_cl,
                },
                "$push": {
                    "stage_history": {
                        "from": old_stage,
                        "to": "ARCHIVED",
                        "actor": actor,
                        "timestamp": now_cl.isoformat(),
                        "notes": reason,
                    }
                },
            },
        )
        if result.modified_count:
            log_event(
                phone,
                InteractionType.STATUS_CHANGE,
                actor,
                {"from": old_stage, "to": "ARCHIVED", "notes": reason},
            )
            # Close the active cycle idempotently
            from .crm_metrics import active_assignment_cycle
            cycle = active_assignment_cycle(db, lead["_id"])
            if cycle:
                cycle_filter = {
                    "_id": cycle["_id"], "unassigned_at": None,
                    "$or": [
                        {"cycle_status": "active"},
                        {"cycle_status": {"$exists": False}},
                    ],
                }
                if cycle.get("cycle_version") is not None:
                    cycle_filter["cycle_version"] = cycle["cycle_version"]
                db["crm_assignment_cycles"].update_one(
                    cycle_filter,
                    {"$set": {"cycle_status": "closed", "closed_at": now_cl,
                              "closed_reason": "lead_archived", "unassigned_at": now_cl,
                              "cycle_version": int(cycle.get("cycle_version") or 0) + 1}},
                )
            return True
        return False

    @staticmethod
    def mark_duplicate(phone: str, actor: str) -> bool:
        """Soft-archive a duplicate while preserving the complete lead record."""
        db = get_db()
        lead = CrmService.get_lead(phone)
        if not lead:
            return False

        now_cl = datetime.now(CHILE_TZ)
        old_stage = lead.get("stage") or lead.get("pipeline_stage") or PipelineStage.NEW
        reason = "Marcado como duplicado desde el listado CRM"
        result = db[COLLECTION_CONVERSATIONS].update_one(
            {"_id": lead["_id"]},
            {
                "$set": {
                    "is_duplicate": True,
                    "duplicate_marked_at": now_cl,
                    "duplicate_marked_by": actor,
                    "stage": "ARCHIVED",
                    "pipeline_stage": "ARCHIVED",
                    "archived_at": now_cl,
                    "archived_by": actor,
                    "archive_reason": reason,
                    "last_crm_update": now_cl,
                },
                "$push": {
                    "stage_history": {
                        "from": old_stage,
                        "to": "ARCHIVED",
                        "actor": actor,
                        "timestamp": now_cl.isoformat(),
                        "notes": reason,
                    }
                },
            },
        )
        if result.modified_count:
            log_event(
                phone,
                InteractionType.STATUS_CHANGE,
                actor,
                {
                    "action": "mark_duplicate",
                    "from": old_stage,
                    "to": "ARCHIVED",
                    "notes": reason,
                },
            )
            return True
        return False
