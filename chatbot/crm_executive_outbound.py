"""Fail-closed, explicitly enabled manual WhatsApp sends from an assigned agent's session.

The office bot and all campaign transports remain unchanged. This module never
counts an API send as formal CRM management or changes a lead's SLA.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import logging
import os
import re
from uuid import uuid4

from bson import ObjectId
from pymongo.errors import DuplicateKeyError
import requests

from .crm_metrics import active_assignment_cycle
from .crm_lead_access import resolve_crm_lead_access_context
from .crm_permissions import can_administer_leads
from .crm_verified_whatsapp_sla import credit_verified_outbound
from .executive_whatsapp_audit import _session, _authorized_lead
from .executive_whatsapp_echo import find_unique_echo_match
from config import Config
from .phone_utils import normalize_phone_strict
from .price_drop_campaign_classifier import lead_must_not_be_contacted
from .whatsapp_client import normalize_whatsapp_recipient, _provider_message_id

OUTBOX = "crm_executive_whatsapp_dispatches"
logger = logging.getLogger(__name__)
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


def eligible_requester_session(db, *, lead: dict, requester_user: dict) -> dict:
    """Authorize the logged-in owner or an active CRM supervisor to use the owner's session.

    A supervisor may initiate outreach ONLY to the single lead of the current
    executive, and never select a sender/session from the browser. All normal
    owner/cycle, phone, consent and provider gates still run.
    """
    if not isinstance(lead, dict) or lead.get("_id") is None:
        raise OutboundBlocked("lead_missing")
    requester_id = str((requester_user or {}).get("_id") or "")
    if not ObjectId.is_valid(requester_id):
        raise OutboundBlocked("requester_not_authorized")
    cycle = active_assignment_cycle(db, lead["_id"])
    if not cycle or cycle.get("cycle_status") not in (None, "active") or cycle.get("unassigned_at") is not None:
        raise OutboundBlocked("current_assignment_required")
    owner_id = str(cycle.get("assigned_to_user_id") or "")
    if not ObjectId.is_valid(owner_id):
        raise OutboundBlocked("current_assignment_required")
    delegated = requester_id != owner_id
    if delegated:
        if not can_administer_leads((requester_user or {}).get("rol")):
            raise OutboundBlocked("requester_not_authorized")
        verified_admin = db["usuarios"].find_one(
            {"_id": ObjectId(requester_id), "is_active": True},
            {"_id": 1, "rol": 1},
        )
        if not verified_admin or not can_administer_leads(verified_admin.get("rol")):
            raise OutboundBlocked("requester_not_authorized")
        access = resolve_crm_lead_access_context(
            db, user=verified_admin, lead=lead, security_enabled=True,
        )
        if not access.access_allowed or not access.action_permissions.get("contact_whatsapp"):
            raise OutboundBlocked("requester_not_authorized")
    info = eligible_manual_session(db, lead=lead, actor_user_id=owner_id)
    return {
        **info, "owner_id": owner_id, "requester_id": requester_id,
        "delegated": delegated,
        "sender_name": str((lead.get("lifecycle") or {}).get("assigned_to_display_name") or "ejecutivo asignado")[:90],
    }


def _provider_rejection_reason(http_status: int, body: dict) -> str:
    """Stable non-PII reason code; never persist raw WaSender errors."""
    if http_status in (401, 403):
        return "authentication"
    if http_status == 402:
        return "subscription"
    if http_status == 429:
        return "rate_limit"
    raw = str(body.get("message") or "")[:300].lower() if isinstance(body, dict) else ""
    if any(term in raw for term in ("not connected", "disconnected", "session inactive", "session is offline")):
        return "session_disconnected"
    if any(term in raw for term in ("subscription", "trial limit", "plan limit", "no active plan")):
        return "subscription"
    if any(term in raw for term in ("rate limit", "too many requests", "account protection")):
        return "rate_limit"
    if any(term in raw for term in ("not on whatsapp", "not registered", "invalid phone", "invalid recipient")):
        return "invalid_recipient"
    if http_status in (400, 422):
        return "invalid_request"
    return "provider_rejected"


def _credited_first_management(db, *, lead_id, actor_user_id: str,
                               cycle_id: str, session_id: str,
                               provider_message_id: str) -> str:
    """Only the exact persisted provider-confirmed human send can stop SLA."""
    try:
        return credit_verified_outbound(
            db, lead_id=lead_id, assignment_cycle_id=cycle_id,
            actor_user_id=actor_user_id, session_id=session_id,
            provider_message_id=provider_message_id,
        )
    except Exception:
        # Never replay a successful WhatsApp send because CRM SLA state failed.
        logger.warning("CRM_EXEC_WA_SLA status=failed")
        return "failed"


def send_manual(db, *, lead_id, actor_user_id: str, message: str,
                idempotency_key: str, post=None, now=None,
                requester_user: dict | None = None) -> dict:
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
    info = (
        eligible_requester_session(db, lead=lead, requester_user=requester_user)
        if requester_user is not None else
        eligible_manual_session(db, lead=lead, actor_user_id=actor_user_id)
    )
    if requester_user is not None and info["owner_id"] != str(actor_user_id):
        raise OutboundBlocked("requester_not_authorized")
    delegated = bool(info.get("delegated"))
    requester_id = str(info.get("requester_id") or actor_user_id)
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
        "mode": "manual_crm_admin_delegated" if delegated else "manual_crm",
        "initiated_by_user_id": requester_id,
        "state": "provider_attempt",
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
        current_info = (
            eligible_requester_session(db, lead=current, requester_user=requester_user)
            if requester_user is not None else
            eligible_manual_session(db, lead=current, actor_user_id=owner)
        )
        if (str(current_info["cycle"]["assignment_cycle_id"]) != cycle_id
                or (requester_user is not None
                    and (current_info["owner_id"] != owner or bool(current_info["delegated"]) != delegated))):
            raise OutboundBlocked("assignment_changed")
    except OutboundBlocked as exc:
        db[OUTBOX].update_one({"_id": identity}, {"$set": {
            "state": "blocked", "block_reason": exc.code,
        }})
        return {"status": "blocked", "reason": exc.code}
    url = Config.WASENDER_BASE_URL.rstrip("/") + "/send-message"
    headers = {"Authorization": "Bearer " + session["token"], "Content-Type": "application/json"}
    do_post = post or requests.post
    http_status, reason = None, None
    try:
        response = do_post(
            url, json={"to": info["recipient"], "text": message},
            headers=headers, timeout=15,
        )
        http_status = response.status_code
        try:
            body = response.json() if response.content else {}
        except (ValueError, TypeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        ok = http_status in (200, 201) and body.get("success") is True
        pid = _provider_message_id(body) if ok else None
        if ok:
            state = "accepted" if pid else "accepted_without_provider_id"
        elif http_status in (200, 201, 202, 204) and body.get("success") is not False:
            # A 2xx without a parseable success flag is not a proven rejection.
            state, reason = "delivery_unknown", "provider_response_unverifiable"
        elif http_status in (408, 425) or http_status >= 500:
            # A server error can occur after a provider accepted the request.
            # Never re-dispatch automatically when acceptance is uncertain.
            state, reason = "delivery_unknown", "provider_unavailable"
        else:
            state = "rejected"
            reason = _provider_rejection_reason(http_status, body)
    except Exception as exc:
        # A network exception may occur after the provider accepted delivery.
        # Preserve idempotency and log only the exception CLASS, never contents.
        state, pid, reason = "delivery_unknown", None, "transport_uncertain"
        logger.warning("CRM_EXEC_WA_SEND session=%s state=delivery_unknown exception_type=%s",
                       sid, type(exc).__name__)
    db[OUTBOX].update_one({"_id": identity}, {"$set": {
        "state": state, "provider_message_id": pid,
        "provider_result_at": datetime.now(timezone.utc),
        "provider_http_status": http_status, "provider_error_reason": reason,
    }})
    if state != "accepted":
        logger.warning("CRM_EXEC_WA_SEND session=%s state=%s http_status=%s reason=%s",
                       sid, state, http_status, reason)
        return {"status": state, **({"reason_code": reason} if state == "rejected" else {})}
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
        echo_fields = {"messages.$.crm_api_provider_message_id": pid}
        if delegated:
            echo_fields.update({
                "messages.$.source": "crm_admin_delegated_api",
                "messages.$.actor_type": "human_supervisor",
                "messages.$.actor_id": requester_id,
                "messages.$.initiated_by_user_id": requester_id,
            })
        merged = db["leads"].update_one({
            "_id": oid, "phone": lead["phone"],
            "lifecycle.current_assignment_cycle_id": cycle_id,
            "lifecycle.assigned_to_user_id": owner,
            "messages": {"$elemMatch": {
                "message_id": echo["message_id"],
                "crm_api_provider_message_id": {"$exists": False},
            }},
        }, {"$set": echo_fields})
        accepted = merged.modified_count == 1
        db[OUTBOX].update_one({"_id": identity}, {"$set": {
            "canonical_recorded": accepted,
        }})
        return {"status": "accepted", "provider_message_id": pid, "recorded": accepted,
                **({"delegated": True} if delegated else {}),
                "sla_management": ("delegated" if delegated and accepted else
                    _credited_first_management(
                        db, lead_id=oid, actor_user_id=owner, cycle_id=cycle_id,
                        session_id=sid, provider_message_id=pid) if accepted else "unverified")}
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
    if delegated:
        entry.update({
            "source": "crm_admin_delegated_api",
            "actor_type": "human_supervisor",
            "actor_id": requester_id,
            "initiated_by_user_id": requester_id,
        })
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
            "recorded": result.modified_count == 1,
            **({"delegated": True} if delegated else {}),
            "sla_management": ("delegated" if delegated and result.modified_count == 1 else
                _credited_first_management(
                    db, lead_id=oid, actor_user_id=owner, cycle_id=cycle_id,
                    session_id=sid, provider_message_id=pid)
                if result.modified_count == 1 else "unverified")}
