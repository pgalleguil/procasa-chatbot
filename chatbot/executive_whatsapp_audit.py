"""Passive, per-session WaSender -> canonical leads audit.

This endpoint NEVER sends WhatsApp messages, calls the chatbot/LLM or writes
unassigned/private chat content. Disabled by default; persistence is a
separate opt-in flag. Each executive has an independent signed webhook URL.
Only the existing usuarios.wasender.session_id is needed in MongoDB; each
executive's credentials live in their OWN named Render JSON variable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4

import httpx
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .crm_metrics import active_assignment_cycle, coerce_utc_datetime
from .executive_pilot_webhook import (
    _parse_message, _remote_contact, _remote_lid, _phone_number_from_jid,
)
from .phone_utils import has_real_phone, normalize_phone_strict, phone_identity_variants
from .webhook_event_policy import verify_wasender_webhook_signature

router = APIRouter()
logger = logging.getLogger(__name__)
_SESSION_ID = re.compile(r"^[1-9][0-9]{0,17}$")
_LID = re.compile(r"^[0-9]{8,25}@lid$")
_ALLOWED_EVENTS = frozenset({"messages.upsert", "messages.received"})


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
    """Explicit non-secret session allowlist; fail closed if malformed.

    When present, the allowlist is authoritative for ALL sessions, including
    those whose legacy secret JSON sets capture_enabled=true. This permits
    changing capture without reading, retyping or exposing webhook credentials.
    Receipt and signature checks remain independent; global write must also be
    enabled and lead ownership must be verified for every message.
    """
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
    # Nonpersonal conversations and newsletters never enter CRM.
    vals = (key.get("remoteJid"), key.get("remoteJidAlt"),
            message.get("chatId"), message.get("from"))
    if any(("@g.us" in str(v or "") or "@newsletter" in str(v or "")
            or "@broadcast" in str(v or "")) for v in vals):
        return None
    return key, message, key["fromMe"]


def _pn_from_lid(lid: str, token: str | None) -> str | None:
    """Resolve transient addressing metadata; never log unknown/private IDs."""
    if not token or not _LID.fullmatch(lid):
        return None
    try:
        with httpx.Client(timeout=4.0, follow_redirects=False) as client:
            result = client.get(
                "https://www.wasenderapi.com/api/pn-from-lid/" +
                quote(lid, safe="@"),
                headers={"Authorization": "Bearer " + token},
            )
        if result.status_code != 200:
            return None
        data = result.json()
        pn = (data.get("data") or {}).get("pn") if isinstance(data, dict) else None
        phone = _phone_number_from_jid(pn)
        return phone if has_real_phone(phone) else None
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
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
                       lead: dict, cycle: dict) -> str:
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
            "$nor": [{"messages": {"$elemMatch": {
                "wasender_session_id": session["session_id"],
                "provider_message_id": provider_id,
            }}}],
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
            },
        },
    )
    return "stored" if result.modified_count == 1 else "duplicate_or_reassigned"


def _handle_authorized(db, session: dict, payload: dict, *, write: bool) -> str:
    meta = _event_metadata(payload)
    if meta is None:
        return "ignored"
    phone, lid = _peer_phone(meta, session.get("token"))
    if not phone or not has_real_phone(phone):
        return "ignored"
    matched = _authorized_lead(db, phone=phone, user_id=session["user_id"])
    if matched is None:
        return "ignored"
    lead, cycle = matched
    # Read message text only after exact-session, phone and ownership checks.
    parsed = _parse_message(payload, phone, lid)
    if parsed is None:
        return "ignored"
    if not write:
        return "dry_run"
    # Repeat active-user guard immediately before the single document write.
    still_active = db["usuarios"].find_one({
        "_id": session["user_oid"], "is_active": True, "rol": "agente",
        "wasender.session_id": {"$in": [session["session_id"], int(session["session_id"])]},
    }, {"_id": 1})
    if not still_active:
        return "inactive_session"
    return _record_authorized(
        db, session=session, phone=phone, parsed=parsed, lead=lead, cycle=cycle
    )


def _session_from_storage(session_id: str) -> dict | None:
    from .storage import get_db
    return _session(get_db(), session_id)


def _handle_from_storage(session: dict, payload: dict, write: bool) -> str:
    from .storage import get_db
    return _handle_authorized(get_db(), session, payload, write=write)


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
    if status in ("stored", "duplicate_or_reassigned", "dry_run"):
        logger.info("EXEC_AUDIT status=%s", status)
    return JSONResponse({"ok": True, "status": status})
