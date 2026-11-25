"""Passive, per-session WaSender -> canonical leads audit.

This endpoint NEVER sends WhatsApp messages, calls the chatbot/LLM or writes
unassigned/private chat content. Disabled by default; persistence is a
separate opt-in flag. Each executive has an independent signed webhook URL.
Only the existing usuarios.wasender.session_id is needed in MongoDB; each
executive's credentials live in their OWN named Render JSON variable.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from contextvars import ContextVar
import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import uuid4

import httpx
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .crm_metrics import active_assignment_cycle, coerce_utc_datetime
from .executive_whatsapp_echo import find_unique_echo_match
from .executive_pilot_webhook import (
    _parse_message, _remote_contact, _remote_lid, _phone_number_from_jid,
)
from .phone_utils import has_real_phone, normalize_phone_strict, phone_identity_variants
from .webhook_event_policy import verify_wasender_webhook_signature

router = APIRouter()
logger = logging.getLogger(__name__)
_SESSION_ID = re.compile(r"^[1-9][0-9]{0,17}$")
_LID = re.compile(r"^[0-9]{8,25}@lid$")
_ALLOWED_EVENTS = frozenset({"messages.upsert", "messages.received", "message.sent"})
_LID_DIAG_SESSION: ContextVar[str] = ContextVar("exec_lid_diag_session", default="")
_REVERSE_LID_CACHE: dict[tuple[str, str, int], tuple[float, str | None]] = {}
_REVERSE_LID_LOCK = threading.Lock()

_DIAGNOSTIC_FLAG = "PROCASA_EXEC_AUDIT_DIAGNOSTIC_SESSIONS"
# These fixed reason codes are the ONLY values ever emitted. Neither content,
# customer identifiers, chat IDs nor provider payloads are logged.
_DIAGNOSTIC_REASONS = frozenset({
    "event_unsupported", "data_missing", "message_shape_unknown",
    "message_batch_unsupported", "direction_missing", "non_direct_chat",
    "event_direction_mismatch", "outbound_not_confirmed", "metadata_other",
    "peer_phone_unresolved", "peer_lid_unresolved",
    "peer_not_authorized", "text_unavailable", "capture_disabled",
    "session_inactive", "stored", "echo_reconciled",
    "duplicate_or_reassigned", "stale", "unverified_timestamp", "invalid_id",
    "lid_token_unavailable", "lid_mapping_unavailable", "lid_response_invalid",
    "lid_transport_error", "lid_http_auth", "lid_http_not_found",
    "lid_reverse_verified", "lid_reverse_miss", "lid_reverse_failed",
    "lid_verified_cached", "lid_provider_unmapped", "lid_candidate_cohort_exceeded",
    "lid_http_rate_limit", "lid_http_server", "lid_http_other", "lid_resolved",
})


def _audit_diagnostic(session: dict, reason: str) -> None:
    """Optional metadata-only diagnosis, explicitly restricted to named sessions.

    The flag contains session IDs, not customer phone numbers. This function
    neither stores private-chat metadata nor copies payloads to application logs.
    """
    raw = os.getenv(_DIAGNOSTIC_FLAG, "")
    if not raw or len(raw) > 128 or reason not in _DIAGNOSTIC_REASONS:
        return
    values = [item.strip() for item in raw.split(",")]
    if (len(values) > 6 or len(values) != len(set(values))
            or any(not _SESSION_ID.fullmatch(item) for item in values)):
        return
    sid = str(session.get("session_id") or "")
    if sid in values:
        logger.info("EXEC_AUDIT_DIAG session=%s reason=%s", sid, reason)


def _lid_diagnostic(reason: str) -> None:
    sid = _LID_DIAG_SESSION.get()
    if sid:
        _audit_diagnostic({"session_id": sid}, reason)


def _ignored(session: dict, reason: str) -> str:
    _audit_diagnostic(session, reason)
    return "ignored"


def _normalize_message_sent(payload: dict) -> dict:
    """Normalize the two documented signed outbound payload shapes.

    The flat success receipt implies outgoing only when status is explicitly
    sent/delivered/read and contains a message ID, recipient and text.
    Both shapes still pass the exact active-lead/session authorization.
    """
    if payload.get("event") != "message.sent":
        return payload
    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    if isinstance(data.get("messages"), (dict, list)) or isinstance(data.get("key"), dict):
        return payload
    status = str(data.get("status") or "").strip().lower()
    message_id = data.get("id")
    peer = data.get("to")
    content = data.get("content")
    if (status not in ("sent", "delivered", "read")
            or not isinstance(message_id, str) or not message_id.strip()
            or not isinstance(peer, str) or not peer.strip()
            or not isinstance(content, str) or not content.strip()):
        return payload
    # Keep the signed payload's original event and timestamp. Do not create a
    # synthetic provider timestamp: _record_authorized will require a real one.
    msg = {
        "key": {"id": message_id, "fromMe": True, "remoteJid": peer},
        "messageBody": content,
    }
    return {**payload, "data": {"messages": msg, "success": True}}


def _metadata_skip_reason(payload: dict) -> str:
    """Explain shape rejection using only enum codes, not payload values."""
    if payload.get("event") not in _ALLOWED_EVENTS:
        return "event_unsupported"
    data = payload.get("data")
    if not isinstance(data, dict):
        return "data_missing"
    msg = data.get("messages")
    if msg is None and isinstance(data.get("key"), dict):
        msg = data
    if isinstance(msg, list):
        if len(msg) != 1:
            return "message_batch_unsupported"
        msg = msg[0]
    if not isinstance(msg, dict):
        return "message_shape_unknown"
    key = msg.get("key")
    if not isinstance(key, dict) or type(key.get("fromMe")) is not bool:
        return "direction_missing"
    if payload.get("event") == "messages.received" and key["fromMe"]:
        return "event_direction_mismatch"
    if payload.get("event") == "message.sent":
        if not key["fromMe"]:
            return "event_direction_mismatch"
        if data.get("success") is False or str(data.get("status") or "").lower() in ("failed", "error"):
            return "outbound_not_confirmed"
    vals = (key.get("remoteJid"), key.get("remoteJidAlt"),
            msg.get("chatId"), msg.get("from"))
    if any(("@g.us" in str(v or "") or "@newsletter" in str(v or "")
            or "@broadcast" in str(v or "")) for v in vals):
        return "non_direct_chat"
    return "metadata_other"



def _variable_name_for_executive(name: object) -> str | None:
    """Human-readable, deterministic one-secret-per-executive Render key.

    The identity comes exclusively from the authenticated MongoDB user,
    never from the webhook payload, URL or client-supplied identifiers.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    normalized = unicodedata.normalize("NFKD", name.strip())
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii").upper()
    slug = re.sub(r"[^A-Z0-9]+", "_", ascii_name).strip("_")
    if not slug or len(slug) > 80:
        return None
    return "PROCASA_WASENDER_" + slug


def _capture_enabled_for_session(session_id: str, configured: bool) -> bool:
    """Preserve existing gates and independently enable only Mariela's session.

    Never overwrite a shared capture allowlist (which may contain other agents).
    An enabled Mariela override still requires the global write flag, signed
    webhook, active user, current assignment cycle and exact lead/peer match.
    """
    if (session_id == "133526"
            and os.getenv("PROCASA_EXEC_AUDIT_MARIELA_133526_CAPTURE_ENABLED", "0") == "1"):
        return True
    extra_raw = os.getenv("PROCASA_EXEC_AUDIT_CAPTURE_EXTRA_SESSION_IDS", "")
    if extra_raw and len(extra_raw) <= 128:
        extras = [value.strip() for value in extra_raw.split(",")]
        if (len(extras) <= 6 and len(extras) == len(set(extras))
                and all(_SESSION_ID.fullmatch(value) for value in extras)
                and session_id in extras):
            return True

    # Original authoritative per-session allowlist remains intact for
    # every session not named in the explicit additional opt-in.
    raw = os.getenv("PROCASA_EXEC_AUDIT_CAPTURE_SESSION_IDS", "")
    if not raw:
        return configured
    if len(raw) > 256:
        return False
    items = [value.strip() for value in raw.split(",")]
    if (len(items) > 10 or len(items) != len(set(items))
            or any(not _SESSION_ID.fullmatch(value) for value in items)):
        return False
    return session_id in items


def _executive_credentials(name: object, session_id: str) -> dict | None:
    """Read only the configured executive's dedicated secret from Render.

    Example value:
      {"session_id": 133286, "webhook_secret": "...", "api_token": "...",
       "capture_enabled": false}
    """
    env_name = _variable_name_for_executive(name)
    if env_name is None:
        return None
    raw = os.getenv(env_name, "")
    if not raw or len(raw) > 4096:
        return None
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(values, dict):
        return None
    # Protect against pasting another executive's JSON into the wrong variable.
    if type(values.get("session_id")) is not int or str(values["session_id"]) != session_id:
        return None
    secret = values.get("webhook_secret")
    token = values.get("api_token")
    if (not isinstance(secret, str) or len(secret.strip()) < 12
            or not isinstance(token, str) or len(token.strip()) < 12):
        return None
    return {
        "secret": secret.strip(),
        "token": token.strip(),
        "capture_enabled": _capture_enabled_for_session(
            session_id, values.get("capture_enabled") is True
        ),
    }


def _session(db, session_id: str) -> dict | None:
    if not _SESSION_ID.fullmatch(session_id):
        return None
    matches = list(db["usuarios"].find(
        {
            "rol": "agente", "is_active": True,
            "wasender.session_id": {"$in": [int(session_id), session_id]},
        },
        {"_id": 1, "nombre": 1, "wasender.session_id": 1},
    ).limit(2))
    if len(matches) != 1:
        return None  # Duplicate session IDs fail closed.
    user = matches[0]
    env_name = _variable_name_for_executive(user.get("nombre"))
    if env_name is None:
        return None
    # A second active account with the same name must not reuse its secret.
    # Read only the smallest fields necessary to reject a naming collision.
    same_name = list(db["usuarios"].find(
        {"rol": "agente", "is_active": True, "nombre": user["nombre"]},
        {"_id": 1},
    ).limit(2))
    if len(same_name) != 1 or same_name[0]["_id"] != user["_id"]:
        return None
    config = _executive_credentials(user["nombre"], session_id)
    if config is None:
        return None
    return {
        "user_id": str(user["_id"]),
        "user_oid": user["_id"],
        "session_id": session_id,
        **config,
    }


def _event_metadata(payload: dict) -> tuple[dict, dict, bool] | None:
    event = payload.get("event")
    if event not in _ALLOWED_EVENTS:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    message = data.get("messages")
    if message is None and isinstance(data.get("key"), dict):
        message = data
    if isinstance(message, list):
        if len(message) != 1:
            return None
        message = message[0]
    if not isinstance(message, dict):
        return None
    key = message.get("key")
    if not isinstance(key, dict) or type(key.get("fromMe")) is not bool:
        return None
    if event == "messages.received" and key["fromMe"]:
        return None
    if event == "message.sent":
        # Never attribute failed sends or inbound messages as an executive send.
        if not key["fromMe"] or data.get("success") is False:
            return None
        if str(data.get("status") or "").lower() in ("failed", "error"):
            return None
    # Nonpersonal conversations and newsletters never enter CRM.
    vals = (key.get("remoteJid"), key.get("remoteJidAlt"),
            message.get("chatId"), message.get("from"))
    if any(("@g.us" in str(v or "") or "@newsletter" in str(v or "")
            or "@broadcast" in str(v or "")) for v in vals):
        return None
    return key, message, key["fromMe"]


def _pn_from_lid(lid: str, token: str | None) -> str | None:
    """Resolve LID using this session's key; diagnose only fixed transport codes.

    No token, LID, remote phone, provider body or customer metadata is logged.
    """
    if not token:
        _lid_diagnostic("lid_token_unavailable")
        return None
    if not _LID.fullmatch(lid):
        return None
    try:
        with httpx.Client(timeout=4.0, follow_redirects=False) as client:
            result = client.get(
                "https://www.wasenderapi.com/api/pn-from-lid/" +
                quote(lid, safe="@"),
                headers={"Authorization": "Bearer " + token},
            )
        if result.status_code != 200:
            status = result.status_code
            reason = (
                "lid_http_auth" if status in (401, 403) else
                "lid_http_not_found" if status == 404 else
                "lid_http_rate_limit" if status == 429 else
                "lid_http_server" if 500 <= status < 600 else
                "lid_http_other"
            )
            _lid_diagnostic(reason)
            return None
        data = result.json()
        # WaSender can reply HTTP 200 with success=false and no mapping.
        # Treat that as an unavailable provider association, not a malformed
        # webhook; never log the LID, provider body, or customer metadata.
        if isinstance(data, dict) and data.get("success") is False:
            _lid_diagnostic("lid_provider_unmapped")
            return None
        nested = data.get("data") if isinstance(data, dict) else None
        if not isinstance(nested, dict):
            _lid_diagnostic("lid_response_invalid")
            return None
        phone = _phone_number_from_jid(nested.get("pn"))
        if not phone or not has_real_phone(phone):
            _lid_diagnostic("lid_mapping_unavailable")
            return None
        _lid_diagnostic("lid_resolved")
        return phone
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        _lid_diagnostic("lid_transport_error")
        return None


def _peer_phone(meta: tuple[dict, dict, bool], token: str | None) -> tuple[str | None, str | None]:
    key, message, from_me = meta
    phone = _remote_contact(key, message, from_me=from_me)
    if phone and has_real_phone(phone):
        return phone, None
    lid = _remote_lid(key, message)
    if lid:
        return _pn_from_lid(lid, token), lid
    return None, None


def _strict_remote_lid(key: dict, message: dict) -> str | None:
    """Only the chat address is a peer LID; message.from can be our own LID."""
    for value in (key.get("remoteJidAlt"), key.get("remoteJid"),
                  message.get("chatId")):
        lid = str(value or "").strip()
        if _LID.fullmatch(lid):
            return lid
    return None


def _lid_from_pn(phone: str, token: str | None) -> str | None:
    """Independent, read-only reverse verification of one authorized lead PN.

    This never queries arbitrary private contacts or stores provider responses.
    """
    if not token or not has_real_phone(phone):
        return None
    try:
        pn = phone.lstrip("+") + "@s.whatsapp.net"
        with httpx.Client(timeout=2.0, follow_redirects=False) as client:
            response = client.get(
                "https://www.wasenderapi.com/api/lid-from-pn/" +
                quote(pn, safe="@"),
                headers={"Authorization": "Bearer " + token},
            )
        if response.status_code != 200:
            return None
        body = response.json()
        data = body.get("data") if isinstance(body, dict) and body.get("success") is True else None
        lid = str(data.get("lid") or "") if isinstance(data, dict) else ""
        return lid if _LID.fullmatch(lid) else None
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return None


def _cached_lid_from_pn(phone: str, session: dict) -> str | None:
    """Short-lived per-session lookup cache to protect provider rate limits."""
    token = session.get("token")
    if not token:
        return None
    key = (str(session["session_id"]), phone, hash(token))
    now = time.monotonic()
    with _REVERSE_LID_LOCK:
        previous = _REVERSE_LID_CACHE.get(key)
        if previous and previous[0] > now:
            return previous[1]
    value = _lid_from_pn(phone, token)
    with _REVERSE_LID_LOCK:
        if len(_REVERSE_LID_CACHE) >= 512:
            _REVERSE_LID_CACHE.clear()
        _REVERSE_LID_CACHE[key] = (time.monotonic() + (600 if value else 20), value)
    return value


def _verified_lid_phone(db, *, session: dict, lid: str) -> str | None:
    """Match a remote LID only against leads already assigned to this executive.

    Cached mappings were written ONLY after a signed, authorized message with
    trustworthy provider time. For a new conversation, query a bounded cohort
    of the most recently created assigned leads with the provider's inverse API.
    Every match is reauthorized against the active assignment cycle.
    Never fetch content from or persist a private/unmatched chat.
    """
    if not _LID.fullmatch(lid) or os.getenv("PROCASA_EXEC_AUDIT_REVERSE_LID_ENABLED", "1") != "1":
        return None
    owner = session["user_id"]
    cached = list(db["leads"].find({
        "lifecycle.assigned_to_user_id": owner,
        "whatsapp_verified_lid.session_id": session["session_id"],
        "whatsapp_verified_lid.lid": lid,
    }, {"_id": 1, "phone": 1}).limit(2))
    if len(cached) > 1:
        return None
    if len(cached) == 1:
        phone = normalize_phone_strict(cached[0].get("phone"))
        matched = _authorized_lead(db, phone=phone, user_id=owner) if phone else None
        if matched and matched[0]["_id"] == cached[0]["_id"]:
            _lid_diagnostic("lid_verified_cached")
            return phone

    # Search the CURRENT newly assigned business cohort, not the latest 12
    # Mongo ObjectIds (which silently omitted assigned leads from the week
    # sessions were linked). This bounded lookback never scans historical
    # messages or unassigned personal contacts. A verified mapping persists
    # beyond the lookback and is still checked against the active cycle.
    assigned_since = datetime.now(timezone.utc) - timedelta(days=14)
    cohort = list(db["leads"].find({
        "lifecycle.assigned_to_user_id": owner,
        "lifecycle.current_assignment_cycle_id": {"$exists": True},
        "lifecycle.assigned_at": {"$gte": assigned_since},
        "phone": {"$type": "string"},
    }, {"_id": 1, "phone": 1, "lifecycle.assigned_at": 1}
    ).sort("lifecycle.assigned_at", -1).limit(49))
    # Avoid silently ignoring the 13th-48th business lead; fail closed if
    # even the bounded recent cohort is too large for safe provider lookups.
    if len(cohort) > 48:
        _lid_diagnostic("lid_candidate_cohort_exceeded")
        return None
    recent = cohort
    candidates = []
    seen = set()
    for item in recent:
        phone = normalize_phone_strict(item.get("phone"))
        if not phone or phone in seen or not has_real_phone(phone):
            continue
        seen.add(phone)
        verified = _authorized_lead(db, phone=phone, user_id=owner)
        if verified and verified[0]["_id"] == item["_id"]:
            candidates.append(phone)
    if not candidates:
        _lid_diagnostic("lid_reverse_miss")
        return None
    # Concurrency makes the 12 checked candidate leads a bounded network
    # operation. No chat content, message IDs or unassigned contacts are read.
    # WaSender recommends 1-5 concurrent requests per session. Never burst
    # eight+ requests while trying to resolve a privacy-sensitive LID.
    with ThreadPoolExecutor(max_workers=4) as pool:
        observed = list(pool.map(
            lambda candidate: _cached_lid_from_pn(candidate, session),
            candidates,
        ))
    matches = [pn for pn, reported in zip(candidates, observed) if reported == lid]
    if len(matches) == 1:
        _lid_diagnostic("lid_reverse_verified")
        return matches[0]
    _lid_diagnostic("lid_reverse_miss" if not matches else "lid_reverse_failed")
    return None


def _authorized_lead(db, *, phone: str, user_id: str):
    """Match only one unambiguous canonical lead owned by this executive.

    Even if a number is a genuine lead, an inactive/stale/mismatched cycle must
    not authorize its WhatsApp history.
    """
    variants = phone_identity_variants(phone)
    if not variants:
        return None
    candidate_docs = list(db["leads"].find(
        {"phone": {"$in": variants}},
        {"_id": 1, "phone": 1, "conversation_id": 1, "lifecycle": 1},
    ).limit(20))
    # Avoid attaching one chat to an arbitrary property/lead for the same phone.
    if len(candidate_docs) != 1:
        return None
    lead = candidate_docs[0]
    if normalize_phone_strict(lead.get("phone")) != phone:
        return None
    cycle = active_assignment_cycle(db, lead["_id"])
    if not cycle or str(cycle.get("assigned_to_user_id") or "") != user_id:
        return None
    if cycle.get("unassigned_at") is not None:
        return None
    if cycle.get("cycle_status") not in (None, "active"):
        return None
    lifecycle = lead.get("lifecycle") or {}
    if (not isinstance(lifecycle, dict)
            or not str(cycle.get("assignment_cycle_id") or "")
            or str(lifecycle.get("current_assignment_cycle_id") or "")
               != str(cycle.get("assignment_cycle_id"))):
        return None
    # A missing or contradictory owner mirror is not auditable.
    if str(lifecycle.get("assigned_to_user_id") or "") != user_id:
        return None
    return lead, cycle


def _record_authorized(db, *, session: dict, phone: str, parsed: dict,
                       lead: dict, cycle: dict,
                       verified_lid: str | None = None) -> str:
    """Atomic compare-and-append; preserve previous chatbot/agent history."""
    timestamp = parsed.get("timestamp")
    occurred = coerce_utc_datetime(timestamp)
    assigned = coerce_utc_datetime(cycle.get("assigned_at"))
    if not occurred or not assigned or occurred < assigned:
        return "stale"
    # Replays without a trustworthy provider time must not be re-dated to now.
    if parsed.get("timestamp_source") != "provider":
        return "unverified_timestamp"
    provider_id = parsed["provider_message_id"]
    if not provider_id:
        return "invalid_id"
    actor = session["user_id"]
    cycle_id = str(cycle["assignment_cycle_id"])
    if parsed["from_me"]:
        # One CRM API send may have TWO provider IDs: an API receipt and a
        # different WhatsApp webhook message key. Reconcile only a unique,
        # same-owner/session/cycle/content echo within 30 seconds. This
        # comparison occurs after verifying that this is an assigned lead.
        stored = db["leads"].find_one({"_id": lead["_id"]}, {"messages": 1}) or {}
        if any(
            isinstance(previous, dict)
            and str(previous.get("wasender_session_id") or "") == str(session["session_id"])
            and previous.get("provider_echo_message_id") == provider_id
            for previous in stored.get("messages", [])
        ):
            return "duplicate_or_reassigned"
        for original_source in ("crm_manual_api", "crm_admin_delegated_api",
                                "crm_hot_auto_api", "price_drop_campaign_api"):
            match = find_unique_echo_match(
                stored.get("messages") or [],
                source=original_source, content=parsed["content"],
                session_id=str(session["session_id"]), cycle_id=cycle_id,
                owner_id=actor, occurred_at=occurred,
            )
            if match:
                reconciled = db["leads"].update_one({
                    "_id": lead["_id"],
                    "lifecycle.current_assignment_cycle_id": cycle_id,
                    "lifecycle.assigned_to_user_id": actor,
                    "messages": {"$elemMatch": {
                        "message_id": match["message_id"],
                        "provider_echo_message_id": {"$exists": False},
                    }},
                }, {"$set": {
                    "messages.$.provider_echo_message_id": provider_id,
                    "messages.$.provider_echo_at": occurred.isoformat(),
                }})
                return "echo_reconciled" if reconciled.modified_count == 1 else "duplicate_or_reassigned"
    entry = {
        "role": "agent" if parsed["from_me"] else "user",
        "content": parsed["content"],
        "timestamp": occurred.isoformat(),
        "message_id": str(uuid4()),
        "conversation_id": lead.get("conversation_id"),
        "lead_id": str(lead["_id"]),
        "actor_type": "human_agent" if parsed["from_me"] else "customer",
        "actor_id": actor if parsed["from_me"] else None,
        "assigned_executive_id": actor,
        "assignment_cycle_id": cycle_id,
        "channel": "executive_whatsapp",
        "source": "whatsapp_direct" if parsed["from_me"] else "executive_whatsapp",
        "wasender_session_id": session["session_id"],
        "provider_message_id": provider_id,
        "timestamp_source": "provider",
    }
    # An unambiguous global STOP from an assigned customer must suppress
    # future marketing in the *same atomic write as the authorized message*.
    # This does not pause responses to customer-initiated service requests,
    # nor does a model prediction or executive outgoing text set this flag.
    # Reuse the existing campaign opt-out fields so every campaign sender's
    # established gate sees the restriction without scanning passive chats.
    suppression = {}
    if not parsed["from_me"]:
        from .executive_semantics import intent as executive_customer_intent
        if executive_customer_intent(parsed["content"]) == "OPT_OUT":
            suppression = {
                "price_drop_v2.opted_out": True,
                "price_drop_campaign_v1.opted_out": True,
                "contact_restrictions.marketing_opt_out": True,
                "contact_restrictions.marketing_opt_out_source": "executive_whatsapp",
                "contact_restrictions.marketing_opt_out_session_id": session["session_id"],
                "contact_restrictions.marketing_opt_out_provider_id": provider_id,
                "contact_restrictions.marketing_opt_out_at": occurred,
            }
    # Restrict both identity and ownership on the write itself: an SLA
    # reassignment between read and write must block the stale agent.
    result = db["leads"].update_one(
        {
            "_id": lead["_id"],
            "phone": lead["phone"],
            "lifecycle.current_assignment_cycle_id": cycle_id,
            "lifecycle.assigned_to_user_id": actor,
            "$nor": [
                {"messages": {"$elemMatch": {
                    "wasender_session_id": session["session_id"],
                    "provider_message_id": provider_id,
                }}},
                {"messages": {"$elemMatch": {
                    "wasender_session_id": session["session_id"],
                    "provider_echo_message_id": provider_id,
                }}},
            ],
        },
        {
            "$push": {"messages": entry},
            # The same CAS as the canonical message append durably marks this
            # lead for hybrid classification; no LLM runs in the webhook.
            "$set": {
                "commercial_intelligence.pending": True,
                "commercial_intelligence.pending_message_id": entry["message_id"],
                "commercial_intelligence.pending_cycle_id": cycle_id,
                "commercial_intelligence.pending_source": "executive_whatsapp",
                "commercial_intelligence.pending_session_id": session["session_id"],
                "commercial_intelligence.pending_at": datetime.now(timezone.utc),
                **suppression,
                **({"whatsapp_verified_lid": {
                    "session_id": str(session["session_id"]),
                    "lid": verified_lid,
                    "cycle_id": cycle_id,
                    "verified_at": occurred,
                }} if verified_lid and _LID.fullmatch(verified_lid) else {}),
            },
        },
    )
    if result.modified_count == 1 and not parsed["from_me"]:
        # A campaign sent from this executive's session must receive the
        # same reply classification/STOP audit as an office-origin campaign,
        # but only for the SAME authenticated session and active CRM cycle.
        # The canonical inbound message is already present in leads.messages.
        try:
            from .price_drop_leads_v2 import record_inbound
            record_inbound(
                db, phone=phone,
                provider_message_id=provider_id,
                text=parsed["content"], now=occurred,
                source_session_id=str(session["session_id"]),
                assignment_cycle_id=cycle_id,
            )
        except Exception:
            logger.warning("EXEC_AUDIT campaign_reply_attribution_failed")
    if result.modified_count == 1 and parsed["from_me"]:
        # A signed, authorized, persisted HUMAN outgoing WhatsApp counts as
        # first management, even when the message was sent directly from the
        # executive's phone instead of the CRM send button.
        try:
            from .crm_verified_whatsapp_sla import credit_verified_outbound
            outcome = credit_verified_outbound(
                db, lead_id=lead["_id"],
                assignment_cycle_id=cycle_id, actor_user_id=actor,
                session_id=str(session["session_id"]),
                provider_message_id=provider_id,
            )
            logger.info("EXEC_WA_SLA status=%s session=%s", outcome,
                        session["session_id"])
        except Exception:
            # Never lose an already-persisted WhatsApp message or ask the
            # provider to replay it because the SLA management write failed.
            logger.warning("EXEC_WA_SLA status=failed session=%s",
                           session["session_id"])
    return "stored" if result.modified_count == 1 else "duplicate_or_reassigned"


def _handle_authorized(db, session: dict, payload: dict, *, write: bool) -> str:
    payload = _normalize_message_sent(payload)
    meta = _event_metadata(payload)
    if meta is None:
        return _ignored(session, _metadata_skip_reason(payload))
    # Context-local diagnostic applies only to this signed executive event.
    # It is reset immediately to avoid cross-request attribution in the pool.
    marker = _LID_DIAG_SESSION.set(str(session["session_id"]))
    try:
        phone, lid = _peer_phone(meta, session.get("token"))
        if not phone:
            # Identity fallback: exact equality of reverse PN->LID for an
            # assigned lead. Never guess from phone fragments or message text.
            peer_lid = _strict_remote_lid(meta[0], meta[1])
            if peer_lid:
                phone = _verified_lid_phone(db, session=session, lid=peer_lid)
                if phone:
                    lid = peer_lid
    finally:
        _LID_DIAG_SESSION.reset(marker)
    if not phone or not has_real_phone(phone):
        # Distinguish a @lid that WaSender could not resolve from an event
        # lacking any usable remote address; neither identifier is logged.
        reason = "peer_lid_unresolved" if _remote_lid(meta[0], meta[1]) else "peer_phone_unresolved"
        return _ignored(session, reason)
    # One opted-in supervisor test may observe only direction metadata from
    # this signed webhook. The personal conversation is never written to leads.
    if os.getenv("PROCASA_EXEC_E2E_PROBE_ENABLED") == "1":
        try:
            from .executive_whatsapp_e2e_probe import observe_signed_event
            observe_signed_event(db, session, phone, from_me=meta[2])
        except Exception:
            logger.warning("EXEC_E2E_PROBE session=%s stage=observation_error",
                           session["session_id"])
    matched = _authorized_lead(db, phone=phone, user_id=session["user_id"])
    if matched is None:
        return _ignored(session, "peer_not_authorized")
    lead, cycle = matched
    # Read message text only after exact-session, phone and ownership checks.
    # Reuse the strict shared parser for the provider's documented direct-key
    # message.sent envelope, without widening the office bot\u0027s webhook.
    parse_payload = ({**payload, "event": "messages.upsert"}
                     if payload.get("event") == "message.sent" else payload)
    parsed = _parse_message(parse_payload, phone, lid)
    if parsed is None:
        if write and os.getenv("PROCASA_EXEC_SILENCE_CONTINUITY_ENABLED", "0") == "1":
            # Audio, images, attachments or unparseable text may be a real
            # commercial reply. Store only a marker on the authorized lead,
            # never the attachment or private payload.
            db["leads"].update_one({
                "_id": lead["_id"],
                "lifecycle.current_assignment_cycle_id": str(cycle["assignment_cycle_id"]),
                "lifecycle.assigned_to_user_id": session["user_id"],
            }, {"$set": {"commercial_intelligence.silence_unparsed_event_at":
                         datetime.now(timezone.utc)}})
        return _ignored(session, "text_unavailable")
    if not write:
        _audit_diagnostic(session, "capture_disabled")
        return "dry_run"
    # Repeat active-user guard immediately before the single document write.
    still_active = db["usuarios"].find_one({
        "_id": session["user_oid"], "is_active": True, "rol": "agente",
        "wasender.session_id": {"$in": [session["session_id"], int(session["session_id"])]},
    }, {"_id": 1})
    if not still_active:
        _audit_diagnostic(session, "session_inactive")
        return "inactive_session"
    verified_lid = _strict_remote_lid(meta[0], meta[1])
    result = _record_authorized(
        db, session=session, phone=phone, parsed=parsed, lead=lead, cycle=cycle,
        verified_lid=verified_lid,
    )
    _audit_diagnostic(session, result)
    return result


def _session_from_storage(session_id: str) -> dict | None:
    from .storage import get_db
    return _session(get_db(), session_id)


def _handle_from_storage(session: dict, payload: dict, write: bool) -> str:
    from .storage import get_db
    if (payload.get("event") == "session.status" and write
            and os.getenv("PROCASA_EXEC_SILENCE_CONTINUITY_ENABLED", "0") == "1"):
        from .executive_capture_continuity import record_status
        data = payload.get("data")
        status = data.get("status") if isinstance(data, dict) else None
        record_status(get_db(), session_id=session["session_id"],
                      owner_id=session["user_id"],
                      status=status if isinstance(status, str) else "unknown")
        return "session_status_recorded"
    return _handle_authorized(get_db(), session, payload, write=write)


def _diagnostic_readiness_session_ids() -> list[str]:
    """Explicitly opted-in session IDs only; avoid scanning all executive accounts."""
    raw = os.getenv(_DIAGNOSTIC_FLAG, "")
    if not raw or len(raw) > 128:
        return []
    values = [item.strip() for item in raw.split(",")]
    if (len(values) > 6 or len(values) != len(set(values))
            or any(not _SESSION_ID.fullmatch(item) for item in values)):
        return []
    return values


def _provider_identity_readiness(session: dict | None, expected_session: str) -> str:
    """Read-only WaSender identity and health; log only a fixed reason code.

    No sends, lead conversations, client numbers, provider identifiers or tokens
    enter the probe logs. The session's executive identity is DB-bound.
    """
    if session is None:
        return "session_unavailable"
    if expected_session not in {"133517", "133523", "133526", "133527"}:
        return "session_unavailable"
    if str(session.get("session_id") or "") != expected_session:
        return "session_unavailable"
    try:
        from .storage import get_db
        from .crm_executive_outbound import (
            OutboundBlocked, verify_provider_sender_identity,
        )
        from .crm_executive_outbound_api import _session_status
        owner = get_db()["usuarios"].find_one(
            {"_id": session["user_oid"], "rol": "agente", "is_active": True,
             "wasender.session_id": {"$in": [expected_session, int(expected_session)]}},
            {"telefono": 1},
        )
        if not owner or not owner.get("telefono"):
            return "owner_phone_unavailable"
        verify_provider_sender_identity(
            session, expected_phone=owner["telefono"], force=True,
        )
        provider_status = _session_status(session)
        if provider_status != "connected":
            return "provider_" + str(provider_status)[:35]
        return "connected_identity_verified"
    except OutboundBlocked as exc:
        return exc.code
    except Exception:
        return "probe_unavailable"


def _mariela_provider_readiness(session: dict | None) -> str:
    """Backwards-compatible Mariela-only probe for existing tests/operations."""
    return _provider_identity_readiness(session, "133526")


def _readiness_state(session: dict | None) -> str:
    """Non-secret configuration health; not a statement about provider delivery."""
    if session is None:
        return "session_unavailable"
    if os.getenv("PROCASA_EXEC_AUDIT_ENABLED", "0") != "1":
        return "audit_disabled"
    if os.getenv("PROCASA_EXEC_AUDIT_WRITE_ENABLED", "0") != "1":
        return "global_write_disabled"
    if session.get("capture_enabled") is not True:
        return "session_capture_disabled"
    return "capture_ready"


@router.on_event("startup")
async def executive_audit_readiness_probe() -> None:
    """Startup-only, read-only and private-data-free configuration check.

    This does not alter capture permission, save events, call WaSender or
    touch the chatbot. A transient DB failure never prevents the service
    from starting and remains visible as a non-secret reason code.
    """
    for session_id in _diagnostic_readiness_session_ids():
        session = None
        try:
            session = await asyncio.to_thread(_session_from_storage, session_id)
        except Exception:
            state = "storage_unavailable"
        else:
            state = _readiness_state(session)
        logger.info("EXEC_AUDIT_READY session=%s state=%s", session_id, state)
        if session_id in {"133517", "133523", "133527", "133526"}:
            # Only named executive sessions; read-only provider attestation.
            # No live messages or credentials in diagnostics.
            try:
                if session_id == "133526":
                    provider_state = await asyncio.to_thread(
                        _mariela_provider_readiness, session,
                    )
                else:
                    provider_state = await asyncio.to_thread(
                        _provider_identity_readiness, session, session_id,
                    )
            except Exception:
                provider_state = "probe_unavailable"
            if session_id == "133526":
                logger.info("MARIELA_PROVIDER_READY session=133526 state=%s", provider_state)
            else:
                logger.info("EXEC_PROVIDER_READY session=%s state=%s", session_id, provider_state)


@router.on_event("startup")
async def executive_whatsapp_e2e_startup() -> None:
    """Explicitly enabled once-only live probe; never part of normal startup."""
    if os.getenv("PROCASA_EXEC_E2E_PROBE_ENABLED") != "1":
        return

    def send_once():
        from .storage import get_db
        from .executive_whatsapp_e2e_probe import run_once
        run_once(get_db(), _session)

    try:
        await asyncio.to_thread(send_once)
    except Exception:
        # Probe failures must not stop CRM, office bot or other executives.
        logger.warning("EXEC_E2E_PROBE session=133526 stage=startup_error")


@router.post("/webhook/ejecutivos/{session_id}")
async def executive_session_audit(
    session_id: str,
    request: Request,
    x_webhook_signature: str | None = Header(None, alias="X-Webhook-Signature"),
):
    if os.getenv("PROCASA_EXEC_AUDIT_ENABLED", "0") != "1":
        raise HTTPException(status_code=503, detail="executive_audit_disabled")
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(status_code=413, detail="payload_too_large")

    # Both Mongo client access and queries stay off the event loop.
    session = await asyncio.to_thread(_session_from_storage, session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="session_not_authorized")
    if not verify_wasender_webhook_signature(
        raw, x_webhook_signature, session["secret"]
    ):
        raise HTTPException(status_code=401, detail="invalid_signature")
    try:
        import json
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=400, detail="invalid_json")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid_payload")
    write = (
        os.getenv("PROCASA_EXEC_AUDIT_WRITE_ENABLED", "0") == "1"
        and session["capture_enabled"] is True
    )
    try:
        status = await asyncio.to_thread(
            _handle_from_storage, session, payload, write,
        )
    except Exception:
        # No traceback, raw payload, phone, message body, token, provider ID,
        # customer JID or lead ID is ever logged here.
        logger.warning("EXEC_AUDIT status=temporary_failure")
        raise HTTPException(status_code=503, detail="audit_temporarily_unavailable")
    if status in ("stored", "echo_reconciled", "duplicate_or_reassigned", "dry_run"):
        logger.info("EXEC_AUDIT status=%s", status)
    return JSONResponse({"ok": True, "status": status})
