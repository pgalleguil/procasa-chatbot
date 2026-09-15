"""Durable commercial intake, separated from chatbot delivery."""
from __future__ import annotations
import re
from datetime import datetime, timezone
from pymongo import ReturnDocument
from .crm_metrics import create_assignment_cycle
from .property_lookup import (
    canonical_portal,
    extract_property_external_id,
    find_property_by_any_identifier,
    get_prop_location,
    lookup_property_link,
    normalize_property_url,
)
from .lead_router import find_responsible_executive

COLLECTION = "commercial_processing_events"
WAITING_PROPERTY = "waiting_property"
WAITING_INVENTORY = "waiting_inventory_sync"
RECONCILING = "reconciling"
INACTIVE_PROPERTY = "inventory_inactive"
FAILED_RECIPIENT = "failed_recipient"
COMPLETED = "completed"

def _now():
    return datetime.now(timezone.utc)

def _candidate(text, lead):
    code = (lead.get("prospecto") or {}).get("codigo")
    if code: return str(code).strip()
    urls = re.findall(r"https?://[^\s]+", str(text or ""), flags=re.I)
    if urls: return urls[-1]
    match = re.search(r"\b\d{4,12}\b", str(text or ""))
    return match.group(0) if match else None


def property_reference_identity(raw):
    """Return stable identity fields for a pending property reference."""
    value = str(raw or "").strip()
    if not value:
        return {}
    embedded_url = re.search(r"https?://[^\s<>\]\)\"]+", value, flags=re.IGNORECASE)
    if embedded_url:
        value = embedded_url.group(0).rstrip(".,;:)")
    if value.lower().startswith(("http://", "https://")):
        portal = canonical_portal(value) or None
        return {
            "source_property_code": value,
            "source_portal": portal,
            "source_external_id": extract_property_external_id(value, portal),
            "source_url_normalized": normalize_property_url(value),
        }
    return {
        "source_property_code": value,
        "source_inventory_code": value,
    }


def _property_is_active(prop):
    """Do not route a lead to a property explicitly marked inactive."""
    if not isinstance(prop, dict):
        return False
    root_value = prop.get("disponible_prop360")
    if root_value is not None:
        return bool(root_value)
    state = prop.get("estado") or {}
    state_value = state.get("disponible_prop360")
    if state_value is not None:
        return bool(state_value)
    status = str(state.get("estado_prop360") or prop.get("estado_prop360") or "").strip().lower()
    if status:
        return status not in {"baja", "pasiva", "no disponible", "inactiva", "inactive"}
    return True


def _pending_other_count(db, lead_id, event_id):
    query = {
        "lead_id": lead_id,
        "_id": {"$ne": event_id},
        "commercial_processing_state": {"$in": [WAITING_INVENTORY, RECONCILING]},
    }
    coll = db[COLLECTION]
    count_documents = getattr(coll, "count_documents", None)
    if callable(count_documents):
        return int(count_documents(query))
    find = getattr(coll, "find", None)
    if callable(find):
        return len(list(find(query)))
    return 0

def ensure_indexes(db):
    db[COLLECTION].create_index("source_inbound_provider_id", unique=True,
        partialFilterExpression={"source_inbound_provider_id": {"$exists": True}},
        name="uq_commercial_source_inbound")
    db[COLLECTION].create_index([("commercial_processing_state", 1), ("next_attempt_at", 1)],
        name="commercial_processing_ready")
    db[COLLECTION].create_index(
        [("commercial_processing_state", 1), ("source_portal", 1), ("source_external_id", 1)],
        name="commercial_waiting_inventory_identity",
    )
    db[COLLECTION].create_index(
        [("commercial_processing_state", 1), ("source_url_normalized", 1)],
        name="commercial_waiting_inventory_url",
    )

def _state(db, event_id, state, reason, now):
    db[COLLECTION].update_one({"_id": event_id}, {"$set": {
        "commercial_processing_state": state, "updated_at": now},
        "$push": {"history": {"at": now, "state": state, "reason": reason}}})

def process_inbound(db, *, inbound_provider_id, phone, text, received_at=None, is_test=False):
    """Persist one commercial event; property-less inbound never gets assigned."""
    from .phone_utils import normalize_phone_strict
    phone = normalize_phone_strict(phone) or phone
    now = _now()
    lead = db["leads"].find_one({"phone": phone})
    event = db[COLLECTION].find_one_and_update(
        {"source_inbound_provider_id": str(inbound_provider_id)},
        {"$setOnInsert": {
            "source_inbound_provider_id": str(inbound_provider_id),
            "lead_id": lead.get("_id") if lead else None, "phone": phone,
            "received_at": received_at or now, "created_at": now, "updated_at": now,
            "attempts": 0, "is_test": bool(is_test),
            "message_domain": "commercial_processing", "message_type": "property_resolution",
            "responsible_service": "commercial_intake",
            "idempotency_key": f"commercial:inbound:{inbound_provider_id}",
            "history": [{"at": now, "state": "received", "reason": "inbound"}]}},
        upsert=True, return_document=ReturnDocument.AFTER)
    db[COLLECTION].update_one({"_id": event["_id"]}, {"$set": {
        "source_message": str(text or ""),
        "updated_at": now,
    }})
    if not lead:
        _state(db, event["_id"], WAITING_PROPERTY, "lead_not_available", now)
        return db[COLLECTION].find_one({"_id": event["_id"]})
    stored_reference = event.get("source_property_code")
    if stored_reference and event.get("commercial_processing_state") in {WAITING_INVENTORY, RECONCILING}:
        # A Prop360 ingestion may have enriched prospecto.codigo after the
        # WhatsApp event failed. Keep the original external reference while
        # resuming that event, rather than replacing the MLC URL with 7733.
        raw = str(stored_reference).strip()
    else:
        raw = _candidate(text, lead)
    if not raw:
        db["leads"].update_one({"_id": lead["_id"]}, {"$set": {
            "commercial_processing_state": WAITING_PROPERTY, "assignment_type": "NO_PROPERTY",
            "commercial_notification_eligible": False}})
        _state(db, event["_id"], WAITING_PROPERTY, "property_not_provided", now)
        return db[COLLECTION].find_one({"_id": event["_id"]})
    prop_meta = {}
    if str(raw).lower().startswith(("http://", "https://")):
        prop, prop_meta = lookup_property_link(db, raw)
    else:
        prop = find_property_by_any_identifier(db, raw)
    if not prop:
        identity = property_reference_identity(raw)
        db["leads"].update_one({"_id": lead["_id"]}, {"$set": {
            "commercial_processing_state": WAITING_INVENTORY, "assignment_type": "MISSING_PROPERTY",
            **identity, "last_lookup_at": now,
            "commercial_notification_eligible": False}, "$inc": {"lookup_attempts": 1}})
        db[COLLECTION].update_one({"_id": event["_id"]}, {"$set": {
            **identity, "source_message": str(text or ""),
            "last_lookup_at": now, "notification_eligible": False},
            "$inc": {"lookup_attempts": 1}})
        _state(db, event["_id"], WAITING_INVENTORY, "property_not_in_inventory", now)
        return db[COLLECTION].find_one({"_id": event["_id"]})
    if not _property_is_active(prop):
        db["leads"].update_one({"_id": lead["_id"]}, {"$set": {
            "commercial_processing_state": INACTIVE_PROPERTY,
            "assignment_type": "INACTIVE_PROPERTY",
            "commercial_notification_eligible": False}})
        db[COLLECTION].update_one({"_id": event["_id"]}, {"$set": {
            "notification_eligible": False, "assignment_type": "INACTIVE_PROPERTY",
            "source_message": str(text or "")}})
        _state(db, event["_id"], INACTIVE_PROPERTY, "property_inactive", now)
        return db[COLLECTION].find_one({"_id": event["_id"]})
    code = str(prop.get("codigo") or raw)
    identity = property_reference_identity(raw)
    operation = prop_meta.get("operation")
    location = get_prop_location(prop)

    # A lead is assigned to a property only ONCE.  process_inbound runs once per
    # inbound message, so follow-up messages about the same property must reuse
    # the existing active cycle and its recipient.  Re-running the router here
    # would advance the round-robin on every message, rotate the executive,
    # close the previous cycle and emit a fresh HOT notification to a different
    # person for the same lead (observed: one conversation notified both Hernán
    # and María Paz, and the lead ended assigned to the wrong executive).
    from .crm_metrics import active_assignment_cycle
    active_cycle = active_assignment_cycle(db, lead["_id"])
    if active_cycle:
        cycle_prop = str((active_cycle or {}).get("property_code") or "").strip()
        if cycle_prop:
            same_property = cycle_prop == str(code).strip()
        else:
            same_property = str(
                (lead.get("prospecto") or {}).get("codigo") or ""
            ).strip() == str(code).strip()
    else:
        same_property = False

    reuse = bool(active_cycle) and same_property
    cycle = None
    if reuse:
        recipient_user_id = str((active_cycle or {}).get("assigned_to_user_id") or "")
        try:
            from bson import ObjectId
            recipient = db["usuarios"].find_one({"_id": ObjectId(recipient_user_id)})
        except Exception:
            recipient = None
        recipient_phone = (
            ((recipient or {}).get("telefono") or (recipient or {}).get("tel")
             or (recipient or {}).get("movil")) if recipient else None
        )
        if recipient and recipient_phone:
            cycle = active_cycle
            assignment_type = "PROPERTY"
        else:
            # Stale cycle (user removed / phone missing). Fall back to a fresh
            # router assignment instead of leaving the lead stranded.
            reuse = False
    if not reuse:
        executive, _unused, assignment_type = find_responsible_executive(
            property_code=code, comuna=location.get("comuna"), lead_phone=phone)
        recipient = db["usuarios"].find_one({"nombre": executive, "is_active": {"$ne": False}})
        recipient_phone = (recipient or {}).get("telefono") or (recipient or {}).get("tel") or (recipient or {}).get("movil")
    if not recipient or not recipient_phone:
        db[COLLECTION].update_one({"_id": event["_id"]}, {"$set": {
            "notification_eligible": False, "provider_called": False, "assignment_type": assignment_type}})
        _state(db, event["_id"], FAILED_RECIPIENT, "active_recipient_phone_missing", now)
        return db[COLLECTION].find_one({"_id": event["_id"]})
    if cycle is None:
        cycle = create_assignment_cycle(db, lead=lead, assigned_to_user_id=str(recipient["_id"]),
            assigned_by="commercial_intake", reason="inbound_message", assigned_at=now,
            assigned_to_display_name=recipient["nombre"], property_code=code)
        db["crm_assignment_cycles"].update_one({"assignment_cycle_id": cycle["assignment_cycle_id"]},
            {"$set": {"source_inbound_provider_id": str(inbound_provider_id),
                      "source_event_id": str(inbound_provider_id), "source_event_verified": True}})
        cycle = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": cycle["assignment_cycle_id"]}) or cycle
    pending_other = _pending_other_count(db, lead["_id"], event["_id"])
    db["leads"].update_one({"_id": lead["_id"]}, {"$set": {
        "ejecutivo_asignado": recipient["nombre"], "prospecto.ejecutivo": recipient["nombre"],
        "prospecto.codigo": code,
        "prospecto.codigo_propiedad": code,
        "prospecto.operacion": operation or (lead.get("prospecto") or {}).get("operacion"),
        "prospecto.operacion_fuente": operation,
        "prospecto.portal_origen": prop_meta.get("portal"),
        "prospecto.external_id_origen": prop_meta.get("external_id"),
        "codigo": code,
        "codigo_propiedad": code,
        "operacion": operation or lead.get("operacion"),
        "operacion_fuente": operation,
        "portal_origen": prop_meta.get("portal"),
        "external_id_origen": prop_meta.get("external_id"),
        "lifecycle.current_assignment_cycle_id": cycle["assignment_cycle_id"],
        "lifecycle.assignment_cycle_id": cycle["assignment_cycle_id"],
        "lifecycle.assigned_at": cycle.get("assigned_at"),
        "lifecycle.cycle_started_at": cycle.get("cycle_started_at") or cycle.get("assigned_at"),
        "lifecycle.sla_started_at": cycle.get("sla_started_at") or cycle.get("assigned_at"),
        "lifecycle.assigned_to_user_id": cycle.get("assigned_to_user_id"),
        "lifecycle.assigned_to_display_name": cycle.get("assigned_to_display_name"),
        "commercial_processing_state": COMPLETED,
        "commercial_notification_eligible": True,
        "prospecto.link_pendiente": bool(pending_other),
    }})
    fresh = db["leads"].find_one({"_id": lead["_id"]}) or lead
    if str(fresh.get("lead_temperature_effective") or "").upper() == "HOT":
        from .crm_hot_delivery import assign_and_enqueue_hot
        notification = assign_and_enqueue_hot(db, lead=fresh, recipient_user_id=str(recipient["_id"]),
            recipient_name=recipient["nombre"], recipient_phone=str(recipient_phone),
            payload={"phone": phone, "property_code": code, "nombre": (fresh.get("prospecto") or {}).get("nombre"),
                     "last_message": text}, assigned_by="commercial_intake", reason="inbound_message",
            assigned_at=now, source_event_id=str(inbound_provider_id))["notification"]
    else:
        from .crm_non_hot_digest import accumulate_non_hot_lead
        notification = accumulate_non_hot_lead(db, lead=fresh, cycle=cycle)
    db[COLLECTION].update_one({"_id": event["_id"]}, {"$set": {
        **identity,
        "lead_id": fresh["_id"], "resolved_property_code": code,
        "assignment_cycle_id": cycle["assignment_cycle_id"], "assignment_type": assignment_type,
        "notification_id": (notification or {}).get("_id"), "notification_eligible": True,
        "property_resolved_at": now, "source_message": str(text or "")}})
    _state(db, event["_id"], COMPLETED, "property_resolved_and_routed", now)
    return db[COLLECTION].find_one({"_id": event["_id"]})
