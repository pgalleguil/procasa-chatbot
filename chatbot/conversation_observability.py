"""Canonical conversation observability primitives.

This module is deliberately separate from the commercial CRM event model.
It records normalized, append-only conversation facts and keeps PII out of
event metadata.  It does not decide what the bot should say.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from hashlib import sha256
from collections import defaultdict
from threading import Lock

from pymongo import ASCENDING


CONVERSATION_EVENTS_COLLECTION = "conversation_events"

ACTOR_CUSTOMER = "customer"
ACTOR_BOT = "bot"
ACTOR_HUMAN_AGENT = "human_agent"
ACTOR_SYSTEM = "system"
VALID_ACTOR_TYPES = {
    ACTOR_CUSTOMER,
    ACTOR_BOT,
    ACTOR_HUMAN_AGENT,
    ACTOR_SYSTEM,
}

# The queue acquires and releases this fence through separate executor calls.
# A plain Lock is intentionally used here because RLock requires ownership by
# the same OS thread and would intermittently fail during provider delivery.
_DELIVERY_LOCKS = defaultdict(Lock)


def acquire_delivery_guard(key: str):
    """Acquire the in-process send/takeover fence for one conversation key."""
    lock = _DELIVERY_LOCKS[str(key or "unknown")]
    lock.acquire()
    return lock


def release_delivery_guard(lock) -> None:
    if lock is not None:
        lock.release()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_id(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def ensure_conversation_indexes(db) -> None:
    """Create only additive indexes; safe to call during service startup."""
    db[CONVERSATION_EVENTS_COLLECTION].create_index(
        [("conversation_id", ASCENDING), ("timestamp", ASCENDING)],
        name="conversation_timeline",
    )
    db[CONVERSATION_EVENTS_COLLECTION].create_index(
        [("lead_id", ASCENDING), ("timestamp", ASCENDING)],
        name="lead_timeline",
    )
    db[CONVERSATION_EVENTS_COLLECTION].create_index(
        [("event_type", ASCENDING), ("timestamp", ASCENDING)],
        name="event_type_timeline",
    )
    db[CONVERSATION_EVENTS_COLLECTION].create_index(
        [("idempotency_key", ASCENDING)], unique=True,
        partialFilterExpression={"idempotency_key": {"$exists": True}},
        name="uniq_conversation_event_idempotency",
    )


def resolve_or_create_conversation(db, *, lead_id=None, phone=None,
                                   conversation_id=None) -> str:
    """Resolve the stable live conversation ID without changing old IDs.

    Existing conversation IDs always win.  A missing ID is created only for
    the live lead being touched; this function is not a historical backfill.
    Phone is an account lookup key, never the conversation identity.
    """
    conversations = db["leads"]
    selector = None
    if lead_id is not None:
        selector = {"_id": lead_id}
        if not conversations.find_one(selector, {"_id": 1}):
            selector = {"_id": str(lead_id)}
    if selector is None or not conversations.find_one(selector, {"_id": 1}):
        if phone:
            selector = {"phone": phone}
        else:
            selector = None

    if selector:
        doc = conversations.find_one(selector, {"conversation_id": 1}) or {}
        existing = _clean_id(doc.get("conversation_id"))
        if existing:
            return existing

    new_id = _clean_id(conversation_id) or str(uuid4())
    created = False
    if selector:
        created = not bool((doc or {}).get("conversation_id"))
        conversations.update_one(
            selector,
            {"$set": {"conversation_id": new_id},
             "$setOnInsert": {"lead_temperature_effective": "COLD",
                               "conversation_owner": "bot",
                               "human_active": False}},
            upsert=True,
        )
    if created:
        try:
            record_conversation_event(
                db, event_type="conversation_started",
                conversation_id=new_id,
                lead_id=_clean_id((doc or {}).get("_id") or lead_id),
                actor_type=ACTOR_SYSTEM, actor_id="conversation_identity",
                metadata={"identity_source": "lead_id" if lead_id else "phone_resolution"},
            )
        except Exception:
            # Identity creation must not prevent the inbound message from
            # being accepted; the event can be observed through the lead.
            pass
    return new_id


def new_message_id() -> str:
    return str(uuid4())


def actor_type_for(role: str | None, explicit: str | None = None,
                   *, is_human: bool = False) -> str:
    candidate = str(explicit or "").strip().lower()
    if candidate in VALID_ACTOR_TYPES:
        return candidate
    if is_human:
        return ACTOR_HUMAN_AGENT
    role = str(role or "").strip().lower()
    return {
        "user": ACTOR_CUSTOMER,
        "assistant": ACTOR_BOT,
        "system": ACTOR_SYSTEM,
    }.get(role, ACTOR_SYSTEM)


def record_conversation_event(
    db,
    *,
    event_type: str,
    conversation_id: str | None,
    lead_id: str | None = None,
    message_id: str | None = None,
    property_id: str | None = None,
    property_code: str | None = None,
    operation: str | None = None,
    actor_type: str = ACTOR_SYSTEM,
    actor_id: str | None = None,
    timestamp: datetime | str | None = None,
    derived: bool = False,
    confidence: float | None = None,
    method: str | None = None,
    metadata: dict | None = None,
    event_id: str | None = None,
    idempotency_key: str | None = None,
    logical_action_id: str | None = None,
    policy_version: str | None = None,
) -> str:
    """Append one normalized real or derived event; never stores message text."""
    actor_type = actor_type_for(None, actor_type)
    if actor_type not in VALID_ACTOR_TYPES:
        raise ValueError("invalid_actor_type")
    event = {
        "event_id": event_id or str(uuid4()),
        "conversation_id": _clean_id(conversation_id),
        "lead_id": _clean_id(lead_id),
        "message_id": _clean_id(message_id),
        "property_id": _clean_id(property_id),
        "property_code": _clean_id(property_code),
        "operation": _clean_id(operation),
        "actor_type": actor_type,
        "actor_id": _clean_id(actor_id),
        "timestamp": timestamp or utc_now(),
        "event_type": str(event_type),
        "derived": bool(derived),
    }
    if policy_version:
        event["policy_version"] = str(policy_version)
    if idempotency_key is None and logical_action_id:
        idempotency_key = "|".join(str(part or "") for part in (
            conversation_id, message_id, event_type, logical_action_id,
            policy_version,
        ))
    elif idempotency_key is None and message_id:
        idempotency_key = "|".join(str(part or "") for part in (
            conversation_id, message_id, event_type, policy_version,
        ))
    if idempotency_key:
        event["idempotency_key"] = sha256(str(idempotency_key).encode("utf-8")).hexdigest()
    if confidence is not None:
        event["confidence"] = max(0.0, min(float(confidence), 1.0))
    if method:
        event["method"] = str(method)
    if metadata:
        # Callers must provide already-redacted metadata.  Keep this explicit
        # so a future caller cannot accidentally persist transcript/phone data.
        event["metadata"] = dict(metadata)
    try:
        db[CONVERSATION_EVENTS_COLLECTION].insert_one(event)
        return event["event_id"]
    except Exception as exc:
        # Unique-key races are expected when a worker retries the same logical
        # action.  Do not mutate the original append-only event.
        if idempotency_key and exc.__class__.__name__ == "DuplicateKeyError":
            existing = db[CONVERSATION_EVENTS_COLLECTION].find_one(
                {"idempotency_key": event["idempotency_key"]}, {"event_id": 1}
            ) or {}
            if existing.get("event_id"):
                return existing["event_id"]
        raise


def transition_to_human(db, *, lead_id=None, phone=None, conversation_id=None,
                        reason="human_message_received", agent_id=None,
                        source="whatsapp_human_message", at=None) -> dict:
    """Atomically mark ownership as human and return the transition state."""
    at = at or utc_now()
    conversations = db["leads"]
    selector = {"_id": lead_id} if lead_id is not None else {"phone": phone}
    doc = conversations.find_one(selector, {"_id": 1, "conversation_id": 1}) or {}
    if not doc and lead_id is not None:
        selector = {"_id": str(lead_id)}
        doc = conversations.find_one(selector, {"_id": 1, "conversation_id": 1}) or {}
    resolved_conversation = conversation_id or doc.get("conversation_id") or str(uuid4())
    update = {
        "$set": {
            "conversation_owner": "human",
            "human_active": True,
            "human_handoff_reason": str(reason),
            "human_handoff_source": str(source),
            "last_human_message_at": at,
            "conversation_id": resolved_conversation,
        },
    }
    if not (doc or {}).get("human_handoff_at"):
        update["$set"]["human_handoff_at"] = at
        update["$set"]["lifecycle.human_handoff_at"] = at
    if agent_id:
        update["$set"]["human_agent_id"] = str(agent_id)
    guard = acquire_delivery_guard(phone or lead_id or conversation_id or "unknown")
    try:
        result = conversations.update_one(selector, update, upsert=bool(phone and not doc))
    finally:
        release_delivery_guard(guard)
    try:
        record_conversation_event(
            db, event_type="human_handoff_started",
            conversation_id=resolved_conversation,
            lead_id=_clean_id(doc.get("_id")) or _clean_id(lead_id),
            actor_type=ACTOR_HUMAN_AGENT,
            actor_id=agent_id or "human_agent",
            timestamp=at,
            metadata={"reason": str(reason), "source": str(source)},
            logical_action_id="human_handoff_started",
        )
    except Exception:
        # Ownership remains authoritative even if telemetry is unavailable.
        pass
    if not resolved_conversation:
        latest = conversations.find_one(selector, {"conversation_id": 1}) or {}
        resolved_conversation = latest.get("conversation_id")
    return {
        "conversation_id": _clean_id(resolved_conversation),
        "lead_id": _clean_id(doc.get("_id")),
        "human_handoff_at": at,
        "modified": int(result.modified_count or 0),
    }


def complete_human_handoff(db, *, lead_id=None, phone=None, reason="explicit_bot_resume",
                           at=None) -> dict:
    """Explicitly return ownership to the bot; never called implicitly."""
    at = at or utc_now()
    identity_selector = {"_id": lead_id} if lead_id is not None else {"phone": phone}
    selector = {"_id": lead_id, "conversation_owner": "human", "human_active": True} if lead_id is not None else {
        "phone": phone, "conversation_owner": "human", "human_active": True,
    }
    result = db["leads"].update_one(selector, {"$set": {
        "conversation_owner": "bot",
        "human_active": False,
        "handoff_completed_at": at,
        "bot_resume_at": at,
        "handoff_completed_reason": str(reason),
    }})
    latest = db["leads"].find_one(identity_selector, {"_id": 1, "conversation_id": 1}) or {}
    if result.modified_count:
        record_conversation_event(
            db, event_type="human_handoff_completed",
            conversation_id=latest.get("conversation_id"),
            lead_id=str(latest.get("_id")) if latest.get("_id") else _clean_id(lead_id),
            actor_type=ACTOR_SYSTEM, actor_id="handoff_controller", timestamp=at,
            metadata={"reason": str(reason), "bot_resume_explicit": True},
            logical_action_id="explicit_bot_resume",
        )
    return {"modified": int(result.modified_count or 0), "at": at}
