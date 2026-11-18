"""Fail-closed, explicitly enabled manual WhatsApp sends from an assigned agent's session.

The office bot and all campaign transports remain unchanged. This module never
counts an API send as formal CRM management or changes a lead's SLA.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import os
import re
from uuid import uuid4

from bson import ObjectId
from pymongo.errors import DuplicateKeyError
import requests

from .crm_metrics import active_assignment_cycle
from .executive_whatsapp_audit import _session, _authorized_lead
from .executive_whatsapp_echo import find_unique_echo_match
from config import Config
from .phone_utils import normalize_phone_strict
from .price_drop_campaign_classifier import lead_must_not_be_contacted
from .whatsapp_client import normalize_whatsapp_recipient, _provider_message_id

OUTBOX = "crm_executive_whatsapp_dispatches"
MAX_MESSAGE_CHARS = 2000
_SEND_IDS = re.compile(r"^[1-9][0-9]{0,17}$")
_REQUEST_ID = re.compile(r"^[0-9a-zA-Z_-]{8,100}$")


class OutboundBlocked(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _enabled_session_ids() -> set[str]:
    raw = os.getenv("PROCASA_EXEC_CRM_SEND_SESSION_IDS", "")
    if not raw or len(raw) > 256:
        return set()
    ids = [x.strip() for x in raw.split(",")]
    if (len(ids) > 10 or len(ids) != len(set(ids))
            or any(_SEND_IDS.fullmatch(x) is None for x in ids)):
        return set()
    return set(ids)


def eligible_manual_session(db, *, lead: dict, actor_user_id: str) -> dict:
    """Resolve a single current owner and configured WaSender session, no secrets leaked."""
    if os.getenv("PROCASA_EXEC_CRM_DIRECT_SEND_ENABLED", "0") != "1":
        raise OutboundBlocked("crm_direct_send_disabled")
    if not isinstance(lead, dict) or lead.get("_id") is None:
        raise OutboundBlocked("lead_missing")
    owner = str(actor_user_id or "")
    cycle = active_assignment_cycle(db, lead["_id"])
    lc = lead.get("lifecycle") or {}
    if (not cycle or cycle.get("cycle_status") not in (None, "active")
            or cycle.get("unassigned_at") is not None
            or str(cycle.get("assigned_to_user_id")) != owner
            or str(lc.get("assigned_to_user_id")) != owner
            or str(lc.get("current_assignment_cycle_id"))
               != str(cycle.get("assignment_cycle_id"))):
        raise OutboundBlocked("current_assignment_required")
    if lead.get("phone_is_synthetic") or lead.get("contact_identity_incomplete"):
        raise OutboundBlocked("contact_not_verified")
    try:
        recipient = normalize_whatsapp_recipient(str(lead.get("phone") or ""))
    except ValueError:
        raise OutboundBlocked("invalid_lead_phone") from None
    if "@" in recipient:
        raise OutboundBlocked("individual_phone_required")
    if lead_must_not_be_contacted(lead):
        raise OutboundBlocked("customer_contact_blocked")
    authorized = _authorized_lead(db, phone=normalize_phone_strict(recipient), user_id=owner)
    if not authorized or str(authorized[0]["_id"]) != str(lead["_id"]):
        raise OutboundBlocked("ambiguous_or_unassigned_lead")
    try:
        oid = ObjectId(owner)
    except Exception:
        raise OutboundBlocked("invalid_owner_id") from None
    user = db["usuarios"].find_one(
        {"_id": oid, "rol": "agente", "is_active": True},
        {"_id": 1, "wasender.session_id": 1},
    )
    if not user:
        raise OutboundBlocked("executive_inactive")
    sid = str((user.get("wasender") or {}).get("session_id") or "")
    if sid not in _enabled_session_ids():
        raise OutboundBlocked("executive_session_not_enabled")
    session = _session(db, sid)
    if not session or session.get("user_id") != owner or not session.get("capture_enabled"):
        raise OutboundBlocked("executive_session_not_ready")
    return {"cycle": cycle, "recipient": recipient, "session": session}


def send_manual(db, *, lead_id, actor_user_id: str, message: str,
                idempotency_key: str, post=None, now=None) -> dict:
    """One provider call for one idempotency key; never retry an uncertain send.

    A known-accepted send is appended to the canonical lead with the actual
    session and provider message ID; webhook echoes cannot append duplicates.
    """
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_CHARS:
        raise OutboundBlocked("invalid_message")
    if not isinstance(idempotency_key, str) or not _REQUEST_ID.fullmatch(idempotency_key):
        raise OutboundBlocked("invalid_idempotency_key")
    if not ObjectId.is_valid(str(lead_id)):
        raise OutboundBlocked("invalid_lead_id")
    oid = ObjectId(str(lead_id))
    lead = db["leads"].find_one({"_id": oid})
    info = eligible_manual_session(db, lead=lead, actor_user_id=actor_user_id)
    session, cycle = info["session"], info["cycle"]
    owner, cycle_id = str(actor_user_id), str(cycle["assignment_cycle_id"])
    sid = str(session["session_id"])
    identity = sha256(("\x1f".join(
        [str(oid), cycle_id, owner, sid, idempotency_key]
    )).encode("utf-8")).hexdigest()
    utc_now = now if now is not None else datetime.now(timezone.utc)
    if utc_now.tzinfo is None:
        raise OutboundBlocked("aware_timestamp_required")
    document = {
        "_id": identity,
        "lead_id": oid, "assignment_cycle_id": cycle_id,
        "executive_id": owner, "session_id": sid,
        "mode": "manual_crm", "state": "provider_attempt",
        "attempted_at": utc_now,
        "content_sha256": sha256(message.encode("utf-8")).hexdigest(),
    }
    try:
        db[OUTBOX].insert_one(document)
    except DuplicateKeyError:
        # An unconfirmed timeout may still have sent; a second call could
        # duplicate the message. Only return the prior attempt status.
        old = db[OUTBOX].find_one({"_id": identity}, {"state": 1, "provider_message_id": 1}) or {}
        return {"status": "duplicate", "previous_state": old.get("state", "unknown"),
                "provider_message_id": old.get("provider_message_id")}
    # Revalidate immediately before crossing the irreversible provider boundary.
    try:
        current = db["leads"].find_one({"_id": oid})
        current_info = eligible_manual_session(db, lead=current, actor_user_id=owner)
        if str(current_info["cycle"]["assignment_cycle_id"]) != cycle_id:
            raise OutboundBlocked("assignment_changed")
    except OutboundBlocked as exc:
        db[OUTBOX].update_one({"_id": identity}, {"$set": {
            "state": "blocked", "block_reason": exc.code,
        }})
        return {"status": "blocked", "reason": exc.code}
    url = Config.WASENDER_BASE_URL.rstrip("/") + "/send-message"
    headers = {"Authorization": "Bearer " + session["token"], "Content-Type": "application/json"}
    do_post = post or requests.post
    try:
        response = do_post(
            url, json={"to": info["recipient"], "text": message},
            headers=headers, timeout=15,
        )
        body = response.json() if response.content else {}
        if not isinstance(body, dict):
            body = {}
        ok = response.status_code in (200, 201) and body.get("success") is True
        pid = _provider_message_id(body) if ok else None
        if not ok:
            state = "rejected"
        elif not pid:
            state = "accepted_without_provider_id"
        else:
            state = "accepted"
    except Exception:
        # Never retry unknown delivery automatically.
        state, pid = "delivery_unknown", None
    db[OUTBOX].update_one({"_id": identity}, {"$set": {
        "state": state, "provider_message_id": pid, "provider_result_at": datetime.now(timezone.utc),
    }})
    if state != "accepted":
        return {"status": state}
    # A signed messages.upsert webhook can arrive before the send-message API
    # response. Reconcile it instead of writing a second canonical row.
    fresh = db["leads"].find_one({"_id": oid}) or {}
    echo = find_unique_echo_match(
        fresh.get("messages") or [],
        source="whatsapp_direct", content=message,
        session_id=sid, cycle_id=cycle_id, owner_id=owner,
        occurred_at=datetime.now(timezone.utc),
    )
    if echo:
        merged = db["leads"].update_one({
            "_id": oid, "phone": lead["phone"],
            "lifecycle.current_assignment_cycle_id": cycle_id,
            "lifecycle.assigned_to_user_id": owner,
            "messages": {"$elemMatch": {
                "message_id": echo["message_id"],
                "crm_api_provider_message_id": {"$exists": False},
            }},
        }, {"$set": {"messages.$.crm_api_provider_message_id": pid}})
        accepted = merged.modified_count == 1
        db[OUTBOX].update_one({"_id": identity}, {"$set": {
            "canonical_recorded": accepted,
        }})
        return {"status": "accepted", "provider_message_id": pid, "recorded": accepted}
    # CAS prevents writing an executive message into a reassigned lead.
    entry = {
        "message_id": str(uuid4()), "lead_id": str(oid),
        "conversation_id": lead.get("conversation_id"),
        "role": "agent", "actor_type": "human_agent", "actor_id": owner,
        "assigned_executive_id": owner,
        "assignment_cycle_id": cycle_id,
        "channel": "executive_whatsapp", "source": "crm_manual_api",
        "wasender_session_id": sid, "provider_message_id": pid,
        "content": message, "timestamp": datetime.now(timezone.utc).isoformat(),
        "timestamp_source": "crm_api_accepted",
    }
    result = db["leads"].update_one({
        "_id": oid, "phone": lead["phone"],
        "lifecycle.current_assignment_cycle_id": cycle_id,
        "lifecycle.assigned_to_user_id": owner,
        "$nor": [{"messages": {"$elemMatch": {
            "wasender_session_id": sid, "provider_message_id": pid
        }}}],
    }, {"$push": {"messages": entry}, "$set": {
        "commercial_intelligence.pending": True,
        "commercial_intelligence.pending_message_id": entry["message_id"],
        "commercial_intelligence.pending_cycle_id": cycle_id,
        "commercial_intelligence.pending_source": "executive_whatsapp",
        "commercial_intelligence.pending_session_id": sid,
        "commercial_intelligence.pending_at": datetime.now(timezone.utc),
    }})
    db[OUTBOX].update_one({"_id": identity}, {"$set": {
        "canonical_recorded": result.modified_count == 1,
    }})
    return {"status": "accepted", "provider_message_id": pid,
            "recorded": result.modified_count == 1}
