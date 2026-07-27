"""Durable commercial intake, separated from chatbot delivery."""
from __future__ import annotations
import re
from datetime import datetime, timezone
from pymongo import ReturnDocument
from .crm_metrics import create_assignment_cycle
from .property_lookup import find_property_by_any_identifier, get_prop_location
from .lead_router import find_responsible_executive

COLLECTION = "commercial_processing_events"
WAITING_PROPERTY = "waiting_property"
WAITING_INVENTORY = "waiting_inventory_sync"
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

def ensure_indexes(db):
    db[COLLECTION].create_index("source_inbound_provider_id", unique=True,
        partialFilterExpression={"source_inbound_provider_id": {"$exists": True}},
        name="uq_commercial_source_inbound")
    db[COLLECTION].create_index([("commercial_processing_state", 1), ("next_attempt_at", 1)],
        name="commercial_processing_ready")

def _state(db, event_id, state, reason, now):
    db[COLLECTION].update_one({"_id": event_id}, {"$set": {
        "commercial_processing_state": state, "updated_at": now},
        "$push": {"history": {"at": now, "state": state, "reason": reason}}})

def process_inbound(db, *, inbound_provider_id, phone, text, received_at=None, is_test=False):
    """Persist one commercial event; property-less inbound never gets assigned."""
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
    if not lead:
        _state(db, event["_id"], WAITING_PROPERTY, "lead_not_available", now)
        return db[COLLECTION].find_one({"_id": event["_id"]})
    raw = _candidate(text, lead)
    if not raw:
        db["leads"].update_one({"_id": lead["_id"]}, {"$set": {
            "commercial_processing_state": WAITING_PROPERTY, "assignment_type": "NO_PROPERTY",
            "commercial_notification_eligible": False}})
        _state(db, event["_id"], WAITING_PROPERTY, "property_not_provided", now)
        return db[COLLECTION].find_one({"_id": event["_id"]})
    prop = find_property_by_any_identifier(db, raw)
    if not prop:
        db["leads"].update_one({"_id": lead["_id"]}, {"$set": {
            "commercial_processing_state": WAITING_INVENTORY, "assignment_type": "MISSING_PROPERTY",
            "source_property_code": raw, "last_lookup_at": now,
            "commercial_notification_eligible": False}, "$inc": {"lookup_attempts": 1}})
        db[COLLECTION].update_one({"_id": event["_id"]}, {"$set": {
            "source_property_code": raw, "last_lookup_at": now, "notification_eligible": False},
            "$inc": {"lookup_attempts": 1}})
        _state(db, event["_id"], WAITING_INVENTORY, "property_not_in_inventory", now)
        return db[COLLECTION].find_one({"_id": event["_id"]})
    code = str(prop.get("codigo") or raw)
    location = get_prop_location(prop)
    executive, _unused, assignment_type = find_responsible_executive(
        property_code=code, comuna=location.get("comuna"), lead_phone=phone)
    recipient = db["usuarios"].find_one({"nombre": executive, "is_active": {"$ne": False}})
    recipient_phone = (recipient or {}).get("telefono") or (recipient or {}).get("tel") or (recipient or {}).get("movil")
    if not recipient or not recipient_phone:
        db[COLLECTION].update_one({"_id": event["_id"]}, {"$set": {
            "notification_eligible": False, "provider_called": False, "assignment_type": assignment_type}})
        _state(db, event["_id"], FAILED_RECIPIENT, "active_recipient_phone_missing", now)
        return db[COLLECTION].find_one({"_id": event["_id"]})
    cycle = create_assignment_cycle(db, lead=lead, assigned_to_user_id=str(recipient["_id"]),
        assigned_by="commercial_intake", reason="inbound_message", assigned_at=now,
        assigned_to_display_name=recipient["nombre"])
    db["crm_assignment_cycles"].update_one({"assignment_cycle_id": cycle["assignment_cycle_id"]},
        {"$set": {"source_inbound_provider_id": str(inbound_provider_id),
                  "source_event_id": str(inbound_provider_id), "source_event_verified": True}})
    cycle = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": cycle["assignment_cycle_id"]}) or cycle
    db["leads"].update_one({"_id": lead["_id"]}, {"$set": {
        "ejecutivo_asignado": recipient["nombre"], "prospecto.ejecutivo": recipient["nombre"],
        "prospecto.codigo": code,
        "lifecycle.current_assignment_cycle_id": cycle["assignment_cycle_id"],
        "lifecycle.assignment_cycle_id": cycle["assignment_cycle_id"],
        "lifecycle.assigned_at": cycle.get("assigned_at"),
        "lifecycle.cycle_started_at": cycle.get("cycle_started_at") or cycle.get("assigned_at"),
        "lifecycle.sla_started_at": cycle.get("sla_started_at") or cycle.get("assigned_at"),
        "lifecycle.assigned_to_user_id": cycle.get("assigned_to_user_id"),
        "lifecycle.assigned_to_display_name": cycle.get("assigned_to_display_name"),
        "commercial_processing_state": COMPLETED, "commercial_notification_eligible": True}})
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
        "lead_id": fresh["_id"], "resolved_property_code": code,
        "assignment_cycle_id": cycle["assignment_cycle_id"], "assignment_type": assignment_type,
        "notification_id": (notification or {}).get("_id"), "notification_eligible": True,
        "property_resolved_at": now}})
    _state(db, event["_id"], COMPLETED, "property_resolved_and_routed", now)
