"""Fail-closed, explicitly enabled manual WhatsApp sends from an assigned agent's session.

The office bot and all campaign transports remain unchanged. This module never
counts an API send as formal CRM management or changes a lead's SLA.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from urllib.parse import quote
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
_RECIPIENT_CACHE = OrderedDict()
_RECIPIENT_CACHE_LOCK = Lock()
_RECIPIENT_CACHE_MAX = 512
_RECIPIENT_CACHE_TTL = 600
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
    allowed = set(ids)
    # Each named session has its own explicit opt-in. Never rewrite the
    # existing Render allowlist shared by other executives.
    opt_ins = (
        ("PROCASA_MARIELA_CRM_WASENDER_API_ENABLED", "133526"),
        ("PROCASA_HERNAN_CRM_WASENDER_API_ENABLED", "133517"),
        ("PROCASA_SUSANA_CRM_WASENDER_API_ENABLED", "133523"),
        ("PROCASA_PAULA_CRM_WASENDER_API_ENABLED", "133527"),
    )
    for flag, session_id in opt_ins:
        if os.getenv(flag, "0") == "1":
            allowed.add(session_id)
    return allowed


# Two sessions have a documented cross-sender discrepancy in the provider logs.
# Never allow an unaudited API key to emit messages under either identity.
_PROTECTED_SENDER_SESSIONS = frozenset({"133517", "133526"})
_WA_USER_JID = re.compile(r"^([1-9][0-9]{7,14})(?::[0-9]{1,5})?@(s\.whatsapp\.net|c\.us)$")


def verify_provider_sender_identity(session: dict, *, expected_phone: str, get=None, force: bool = False) -> None:
    """Read-only, fail-closed sender attestation before any provider SEND.

    WaSender /api/user is scoped to the bearer session token. The session_id
    stored in our own credential JSON is NOT a provider-side binding proof.
    No API token, provider user ID, phone or personal details are ever logged.
    """
    sid = str((session or {}).get("session_id") or "")
    if not force and sid not in _PROTECTED_SENDER_SESSIONS:
        return
    token = str((session or {}).get("token") or "")
    digits = re.sub(r"[^0-9]", "", str(expected_phone or ""))
    if not token or not re.fullmatch(r"56[0-9]{9}", digits):
        raise OutboundBlocked("sender_identity_unverified")
    try:
        response = (get or requests.get)(
            Config.WASENDER_BASE_URL.rstrip("/") + "/user",
            headers={"Authorization": "Bearer " + token},
            timeout=6,
        )
        if response.status_code != 200:
            raise OutboundBlocked("sender_identity_unverified")
        body = response.json()
    except OutboundBlocked:
        raise
    except Exception:
        raise OutboundBlocked("sender_identity_unverified") from None
    if not isinstance(body, dict) or body.get("success") is not True:
        raise OutboundBlocked("sender_identity_unverified")
    data = body.get("data")
    actual_jid = data.get("id") if isinstance(data, dict) else None
    match = _WA_USER_JID.fullmatch(actual_jid) if isinstance(actual_jid, str) else None
    if not match:
        raise OutboundBlocked("sender_identity_unverified")
    if match.group(1) != digits:
        raise OutboundBlocked("sender_identity_mismatch")


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
        {"_id": 1, "telefono": 1, "wasender.session_id": 1},
    )
    if not user:
        raise OutboundBlocked("executive_inactive")
    sid = str((user.get("wasender") or {}).get("session_id") or "")
    if sid not in _enabled_session_ids():
        raise OutboundBlocked("executive_session_not_enabled")
    session = _session(db, sid)
    if not session or session.get("user_id") != owner or not session.get("capture_enabled"):
        raise OutboundBlocked("executive_session_not_ready")
    try:
        verify_provider_sender_identity(session, expected_phone=user.get("telefono"))
    except OutboundBlocked as exc:
        logger.warning("EXEC_WA_SENDER_GUARD session=%s status=%s", sid, exc.code)
        raise
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


def check_recipient_registration(session: dict, recipient: str, *, get=None) -> bool | None:
    """On-demand, read-only lookup. Unknown is not a negative result.

    Cache only explicit boolean responses, bounded per process and scoped to
    the session/recipient. No phone numbers, tokens or raw responses in logs.
    """
    token = str(session.get("token") or "")
    sid = str(session.get("session_id") or "")
    if not token or not sid or not re.fullmatch(r"\+?[1-9][0-9]{7,14}", recipient):
        return None
    key = sha256(f"{sid}|{recipient.lstrip('+')}".encode()).hexdigest()
    now = monotonic()
    with _RECIPIENT_CACHE_LOCK:
        cached = _RECIPIENT_CACHE.get(key)
        if cached is not None and cached[0] > now:
            _RECIPIENT_CACHE.move_to_end(key)
            return cached[1]
    try:
        response = (get or requests.get)(
            Config.WASENDER_BASE_URL.rstrip("/") + "/on-whatsapp/" + quote(recipient, safe=""),
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            timeout=4,
        )
        if response.status_code != 200:
            return None
        body = response.json()
        data = body.get("data") if isinstance(body, dict) and body.get("success") is True else None
        exists = data.get("exists") if isinstance(data, dict) else None
        if type(exists) is not bool:
            return None
    except Exception:
        return None
    with _RECIPIENT_CACHE_LOCK:
        _RECIPIENT_CACHE[key] = (monotonic() + _RECIPIENT_CACHE_TTL, exists)
        _RECIPIENT_CACHE.move_to_end(key)
        while len(_RECIPIENT_CACHE) > _RECIPIENT_CACHE_MAX:
            _RECIPIENT_CACHE.popitem(last=False)
    return exists


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
    if http_status in (400, 422):
        rules = _provider_validation_diagnostics(body).get("to", [])
        if ("not_registered" in rules or "not on whatsapp" in raw
                or ("not registered" in raw and any(word in raw for word in ("recipient", "number", "phone", "contact")))):
            return "recipient_not_registered"
    if any(term in raw for term in ("not on whatsapp", "not registered", "invalid phone", "invalid recipient")):
        return "invalid_recipient"
    if http_status in (400, 422):
        return "invalid_request"
    return "provider_rejected"


def _provider_validation_diagnostics(body: dict) -> dict:
    """Retain allowlisted validation fields/rules, never raw provider text."""
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, dict):
        return {}
    diagnostics = {}
    for field in ("to", "text"):
        if field not in errors:
            continue
        values = errors[field]
        if not isinstance(values, list):
            values = [values]
        rules = set()
        for value in values[:10]:
            if not isinstance(value, str):
                continue
            raw = value[:500].lower()
            if "not on whatsapp" in raw or "not registered" in raw:
                rules.add("not_registered")
            elif "required" in raw:
                rules.add("required")
            elif "too long" in raw or "maximum" in raw or "greater than" in raw or "characters" in raw:
                rules.add("length")
            elif "format" in raw or "e.164" in raw:
                rules.add("format")
            elif "invalid" in raw or "valid" in raw:
                rules.add("invalid")
            else:
                rules.add("other")
        diagnostics[field] = sorted(rules) or ["unspecified"]
    if any(key not in ("to", "text") for key in errors):
        diagnostics["other"] = ["unspecified"]
    return diagnostics


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
    # Only an explicit CRM send triggers the optional lookup. Reserve the
    # idempotency key first; do not query on modal open or background polling.
    registration = None
    if os.getenv("PROCASA_EXEC_WA_RECIPIENT_CHECK_ENABLED", "0") == "1":
        registration = check_recipient_registration(session, info["recipient"])
    # Revalidate immediately before crossing the irreversible provider boundary.
    try:
        current = db["leads"].find_one({"_id": oid})
        current_info = (
            eligible_requester_session(db, lead=current, requester_user=requester_user)
            if requester_user is not None else
            eligible_manual_session(db, lead=current, actor_user_id=owner)
        )
        if (str(current_info["cycle"]["assignment_cycle_id"]) != cycle_id
                or current_info["recipient"] != info["recipient"]
                or str(current_info["session"]["session_id"]) != sid
                or current_info["session"]["token"] != session["token"]
                or (requester_user is not None
                    and (current_info["owner_id"] != owner or bool(current_info["delegated"]) != delegated))):
            raise OutboundBlocked("assignment_changed")
    except OutboundBlocked as exc:
        db[OUTBOX].update_one({"_id": identity}, {"$set": {
            "state": "blocked", "block_reason": exc.code,
        }})
        return {"status": "blocked", "reason": exc.code}
    if registration is False:
        db[OUTBOX].update_one({"_id": identity}, {"$set": {
            "state": "blocked", "block_reason": "recipient_not_registered",
            "provider_send_attempted": False, "recipient_registration": "not_registered",
            "provider_message_id": None, "provider_error_reason": "recipient_not_registered",
            "provider_result_at": datetime.now(timezone.utc),
        }})
        return {"status": "rejected", "reason_code": "recipient_not_registered"}
    url = Config.WASENDER_BASE_URL.rstrip("/") + "/send-message"
    headers = {"Authorization": "Bearer " + session["token"], "Content-Type": "application/json"}
    do_post = post or requests.post
    http_status, reason = None, None
    validation = {}
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
            if http_status in (400, 422):
                validation = _provider_validation_diagnostics(body)
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
        "provider_send_attempted": True,
        **({"recipient_registration": "registered" if registration is True else "unknown"}
           if os.getenv("PROCASA_EXEC_WA_RECIPIENT_CHECK_ENABLED", "0") == "1" else {}),
        **({"provider_validation_fields": validation,
            "provider_request_text_chars": len(message)} if http_status in (400, 422) else {}),
    }})
    if state != "accepted":
        logger.warning("CRM_EXEC_WA_SEND session=%s state=%s http_status=%s reason=%s validation=%s",
                       sid, state, http_status, reason, validation)
        return {"status": state, **({"reason_code": reason} if state == "rejected" else {})}
    # A signed messages.upsert webhook can arrive before the send-message API
    # response. Reconcile it instead of writing a second canonical row.
    fresh = db["leads"].find_one({"_id": oid}) or {}
    # A signed webhook may have persisted the outgoing message before the API
    # receipt; reconcile either signed direct or supervised attribution.
    echo = None
    for candidate_source in (
        ("crm_admin_delegated_api", "whatsapp_direct") if delegated else ("whatsapp_direct",)
    ):
        echo = find_unique_echo_match(
            fresh.get("messages") or [],
            source=candidate_source, content=message,
            session_id=sid, cycle_id=cycle_id, owner_id=owner,
            occurred_at=datetime.now(timezone.utc),
        )
        if echo:
            break
    if echo:
        echo_fields = {"messages.$.crm_api_provider_message_id": pid,
                       "first_contact_phone_access.verified": True}
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
        "first_contact_phone_access.verified": True,
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
