"""Isolated, opt-in single-contact WaSender pilot for executive WhatsApp.

This does NOT use the office chatbot webhook, durable chatbot queue, LLM,
automatic sending, or SLA protection. Missing configuration fails closed.
Only messages from one explicitly authorized test contact can be persisted
to an already-existing canonical lead and its current, matching owner cycle.
"""
from __future__ import annotations

import asyncio
import json
import os
import logging
import re
from functools import lru_cache
from urllib.parse import quote
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .phone_utils import has_real_phone, normalize_phone_strict
from .webhook_event_policy import verify_wasender_webhook_signature

router = APIRouter()


def _pilot_config() -> dict[str, str]:
    if os.getenv("PROCASA_EXEC_PILOT_ENABLED", "").strip() != "1":
        logging.getLogger(__name__).warning("EXEC_PILOT_GATE rejected=disabled")
        raise HTTPException(status_code=503, detail="pilot_disabled")
    cfg = {
        "secret": os.getenv("PROCASA_EXEC_PILOT_WEBHOOK_SECRET", "").strip(),
        "lead_id": os.getenv("PROCASA_EXEC_PILOT_LEAD_ID", "").strip(),
        "contact_phone": os.getenv("PROCASA_EXEC_PILOT_CONTACT_PHONE", "").strip(),
        "actor_user_id": os.getenv("PROCASA_EXEC_PILOT_ACTOR_USER_ID", "").strip(),
        "session": os.getenv("PROCASA_EXEC_PILOT_SESSION_LABEL", "").strip(),
    }
    cfg["probe_only"] = os.getenv("PROCASA_EXEC_PILOT_PROBE_ONLY", "").strip() == "1"
    expected = normalize_phone_strict(cfg["contact_phone"])
    identity_valid = (ObjectId.is_valid(cfg["lead_id"])
                      and ObjectId.is_valid(cfg["actor_user_id"]))
    missing = []
    if len(cfg["secret"]) < 12:
        missing.append("webhook_secret")
    if not (expected and has_real_phone(expected)):
        missing.append("allowed_contact")
    if not cfg["session"]:
        missing.append("session_label")
    if not (cfg["probe_only"] or identity_valid):
        missing.append("lead_or_actor_ids")
    if missing:
        # Log ONLY configuration field names. Never log secret values,
        # phone numbers, message bodies, IDs, request payload or signature.
        logging.getLogger(__name__).warning(
            "EXEC_PILOT_GATE rejected=incomplete missing_fields=%s probe_only=%s",
            ",".join(missing), cfg["probe_only"],
        )
        raise HTTPException(status_code=503, detail="pilot_configuration_incomplete")
    cfg["contact_phone"] = expected
    return cfg


def _phone_number_from_jid(value: Any) -> str | None:
    raw = str(value or "").strip()
    if raw.endswith("@s.whatsapp.net"):
        raw = raw.split("@", 1)[0]
    elif "@" in raw:
        return None  # Never assume a @lid, group, or channel is a phone number.
    if not has_real_phone(raw):
        return None
    return normalize_phone_strict(raw)


def _remote_contact(key: dict, msg: dict, *, from_me: bool) -> str | None:
    # For outbound, senderPn can refer to the linked executive, not the lead.
    candidates = [
        key.get("remoteJidAlt"),
        key.get("remoteJid"),
        msg.get("chatId"),
    ]
    if not from_me:
        # Incoming senderPn identifies the customer; outgoing senderPn and
        # "from" may identify the linked executive instead. Never attribute
        # an outbound @lid chat to the executive's own phone by accident.
        candidates.extend([
            key.get("senderPn"), key.get("cleanedSenderPn"),
            msg.get("from"),
        ])
    for raw in candidates:
        phone = _phone_number_from_jid(raw)
        if phone:
            return phone
    return None


def _remote_lid(key: dict, msg: dict) -> str | None:
    """LID of the remote chat, never senderLid (can be the linked account)."""
    for value in (
        key.get("remoteJidAlt"), key.get("remoteJid"),
        msg.get("chatId"), msg.get("from"),
    ):
        raw = str(value or "").strip()
        if re.fullmatch(r"[0-9]{8,25}@lid", raw):
            return raw
    return None


@lru_cache(maxsize=4)
def _authorized_contact_lid(expected_phone: str, session_token: str) -> str | None:
    """Look up ONLY the explicitly approved office phone, not any private peer."""
    pn = expected_phone.lstrip("+") + "@s.whatsapp.net"
    endpoint = (
        "https://www.wasenderapi.com/api/lid-from-pn/"
        + quote(pn, safe="@")
    )
    try:
        with httpx.Client(timeout=4.0, follow_redirects=False) as client:
            response = client.get(
                endpoint, headers={"Authorization": "Bearer " + session_token}
            )
        if response.status_code != 200:
            return None
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        lid = data.get("lid") if isinstance(data, dict) else None
        if isinstance(lid, str) and re.fullmatch(r"[0-9]{8,25}@lid", lid):
            return lid
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    return None


def _get_allowed_lid(payload: dict, expected_phone: str) -> str | None:
    """Resolve fixed approved contact only when the event has an unresolved LID."""
    event_type = payload.get("event")
    if event_type not in ("messages.upsert", "messages.received"):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    msg = data.get("messages")
    if msg is None and isinstance(data.get("key"), dict):
        msg = data
    if isinstance(msg, list):
        msg = msg[0] if len(msg) == 1 else None
    if not isinstance(msg, dict) or not isinstance(msg.get("key"), dict):
        return None
    key = msg["key"]
    if _remote_lid(key, msg) is None:
        return None
    # This credential belongs to the pilot's own WaSender session.
    token = os.getenv("WASENDER_PABLO_GALLEGUILLOS_API_TOKEN", "").strip()
    if not token:
        return None
    return _authorized_contact_lid(expected_phone, token)


def _message_timestamp(msg: dict, payload: dict) -> tuple[str, str]:
    raw = msg.get("messageTimestamp") or payload.get("timestamp")
    if isinstance(raw, dict):
        raw = raw.get("seconds") or raw.get("low")
    try:
        numeric = float(raw)
        if numeric > 10**12:
            numeric /= 1000
        parsed = datetime.fromtimestamp(numeric, timezone.utc)
        now = datetime.now(timezone.utc)
        if abs((now - parsed).total_seconds()) <= 5 * 24 * 3600:
            return parsed.isoformat(), "provider"
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return datetime.now(timezone.utc).isoformat(), "received_at"


def _parse_message(payload: dict, expected_phone: str, expected_lid: str | None = None) -> dict | None:
    event_type = payload.get("event")
    if event_type not in ("messages.upsert", "messages.received"):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    messages = data.get("messages")
    # Older WaSender webhook examples expose the message directly under data.
    if messages is None and isinstance(data.get("key"), dict):
        messages = data
    if isinstance(messages, list):
        if len(messages) != 1:
            return None
        messages = messages[0]
    if not isinstance(messages, dict):
        return None
    key = messages.get("key")
    if not isinstance(key, dict) or not isinstance(key.get("fromMe"), bool):
        return None
    if any("@g.us" in str(v or "") or "@newsletter" in str(v or "")
           for v in (key.get("remoteJid"), key.get("remoteJidAlt"))):
        return None
    from_me = key["fromMe"]
    # WaSender messages.received is strictly inbound. Reject an outgoing
    # event mislabeled as incoming, independent of contact matching.
    if event_type == "messages.received" and from_me:
        return None
    peer = _remote_contact(key, messages, from_me=from_me)
    # Fail closed. A remote @lid is trusted only if WaSender independently
    # resolved the APPROVED office number to exactly that identifier.
    valid_by_lid = (
        peer is None
        and expected_lid is not None
        and _remote_lid(key, messages) == expected_lid
    )
    if peer != expected_phone and not valid_by_lid:
        return None
    provider_id = str(key.get("id") or messages.get("id") or "").strip()
    if not provider_id or len(provider_id) > 256:
        return None
    body = messages.get("message")
    if not isinstance(body, dict):
        body = {}
    extended = body.get("extendedTextMessage")
    if not isinstance(extended, dict):
        extended = {}
    content = messages.get("messageBody") or body.get("conversation") or extended.get("text")
    if not isinstance(content, str) or not content.strip() or len(content) > 8000:
        return None  # Only simple text in the pilot; no media or attachment capture.
    timestamp, timestamp_source = _message_timestamp(messages, payload)
    return {
        "provider_message_id": provider_id,
        "from_me": from_me,
        "content": content.strip(),
        "timestamp": timestamp,
        "timestamp_source": timestamp_source,
    }


def _probe_skip_reason(payload: dict, expected_phone: str, expected_lid: str | None = None) -> str:
    """Only reason codes, never identifiers, text or metadata of private chats."""
    event_type = payload.get("event")
    if event_type not in ("messages.upsert", "messages.received"):
        return "wrong_event_type"
    data = payload.get("data")
    if not isinstance(data, dict):
        return "missing_data"
    messages = data.get("messages")
    if messages is None and isinstance(data.get("key"), dict):
        messages = data
    if isinstance(messages, list):
        if len(messages) != 1:
            return "message_batch_size"
        messages = messages[0]
    if not isinstance(messages, dict):
        return "missing_message_object"
    key = messages.get("key")
    if not isinstance(key, dict) or not isinstance(key.get("fromMe"), bool):
        return "missing_direction"
    if event_type == "messages.received" and key["fromMe"]:
        return "misclassified_outgoing"
    if any("@g.us" in str(v or "") or "@newsletter" in str(v or "")
           for v in (key.get("remoteJid"), key.get("remoteJidAlt"))):
        return "group_or_channel"
    peer = _remote_contact(key, messages, from_me=key["fromMe"])
    if peer is None:
        if expected_lid is None or _remote_lid(key, messages) != expected_lid:
            return "unresolved_contact"
    elif peer != expected_phone:
        return "other_contact"
    provider_id = str(key.get("id") or messages.get("id") or "").strip()
    if not provider_id or len(provider_id) > 256:
        return "missing_message_id"
    body = messages.get("message")
    if not isinstance(body, dict):
        body = {}
    extended = body.get("extendedTextMessage")
    if not isinstance(extended, dict):
        extended = {}
    content = messages.get("messageBody") or body.get("conversation") or extended.get("text")
    if not isinstance(content, str) or not content.strip() or len(content) > 8000:
        return "not_supported_text"
    return "unclassified"


def _save_authorized_message(cfg: dict[str, str], parsed: dict) -> str:
    from .crm_metrics import active_assignment_cycle
    from .storage import get_db

    db = get_db()
    lead_oid = ObjectId(cfg["lead_id"])
    lead = db["leads"].find_one(
        {"_id": lead_oid}, {"_id": 1, "phone": 1, "conversation_id": 1}
    )
    if not lead or normalize_phone_strict(lead.get("phone")) != cfg["contact_phone"]:
        return "not_authorized"
    cycle = active_assignment_cycle(db, lead_oid)
    if not cycle or str(cycle.get("assigned_to_user_id") or "") != cfg["actor_user_id"]:
        return "not_authorized"

    occurred = datetime.fromisoformat(parsed["timestamp"])
    assigned_at = cycle.get("assigned_at")
    if isinstance(assigned_at, str):
        try:
            assigned_at = datetime.fromisoformat(assigned_at.replace("Z", "+00:00"))
        except ValueError:
            return "not_authorized"
    if not isinstance(assigned_at, datetime):
        return "not_authorized"
    if assigned_at.tzinfo is None:
        assigned_at = assigned_at.replace(tzinfo=timezone.utc)
    if occurred < assigned_at.astimezone(timezone.utc):
        return "stale"

    message = {
        "role": "agent" if parsed["from_me"] else "user",
        "content": parsed["content"],
        "timestamp": parsed["timestamp"],
        "message_id": str(uuid4()),
        "conversation_id": lead.get("conversation_id"),
        "lead_id": str(lead_oid),
        "actor_type": "human_agent" if parsed["from_me"] else "customer",
        "actor_id": cfg["actor_user_id"] if parsed["from_me"] else None,
        "assigned_executive_id": cfg["actor_user_id"],
        "assignment_cycle_id": str(cycle["assignment_cycle_id"]),
        "channel": "executive_whatsapp",
        "source": "whatsapp_direct" if parsed["from_me"] else "executive_whatsapp",
        "wasender_session_id": cfg["session"],
        "provider_message_id": parsed["provider_message_id"],
        "timestamp_source": parsed["timestamp_source"],
    }
    # Compare-and-append is atomic for duplicates from webhook retries.
    # Do NOT use guardar_mensaje's 50-item $slice; history must be preserved.
    result = db["leads"].update_one(
        {
            "_id": lead_oid,
            "$nor": [{"messages": {"$elemMatch": {
                "wasender_session_id": cfg["session"],
                "provider_message_id": parsed["provider_message_id"],
            }}}],
            "lifecycle.current_assignment_cycle_id": str(cycle["assignment_cycle_id"]),
            "lifecycle.assigned_to_user_id": cfg["actor_user_id"],
        },
        {"$push": {"messages": message}},
    )
    return "stored" if result.modified_count else "duplicate_or_cycle_changed"


@router.post("/webhook/ejecutivos-piloto")
async def executive_pilot_webhook(
    request: Request,
    x_webhook_signature: str | None = Header(None, alias="X-Webhook-Signature"),
):
    cfg = _pilot_config()
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(status_code=413, detail="payload_too_large")
    if not verify_wasender_webhook_signature(raw, x_webhook_signature, cfg["secret"]):
        raise HTTPException(status_code=401, detail="invalid_signature")
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="invalid_json")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid_payload")
    # Only the allowed office phone is queried for its WhatsApp LID.
    # Never resolve arbitrary event contacts or store unmatched payloads.
    allowed_lid = None
    if cfg["probe_only"]:
        allowed_lid = await asyncio.to_thread(
            _get_allowed_lid, payload, cfg["contact_phone"]
        )
    parsed = _parse_message(payload, cfg["contact_phone"], allowed_lid)
    if parsed is None:
        if cfg["probe_only"]:
            reason = _probe_skip_reason(payload, cfg["contact_phone"], allowed_lid)
            # Never record details or metadata from non-allowed private chats.
            if reason != "other_contact":
                logging.getLogger(__name__).info(
                    "[EXEC_PILOT_DIAGNOSTIC] skipped=%s", reason
                )
        return JSONResponse({"ok": True, "status": "ignored"})
    if cfg["probe_only"]:
        # The first live test must prove the provider can report both directions
        # without opening a MongoDB connection, storing content, or invoking AI.
        direction = "manual_outgoing" if parsed["from_me"] else "customer_incoming"
        logging.getLogger(__name__).info("[EXEC_PILOT_PROBE] direction=%s", direction)
        return JSONResponse({
            "ok": True, "status": "probe_received", "direction": direction,
            "payload_contents_stored": False, "mongo_connections": 0,
            "outbound_messages_sent": 0,
        })
    try:
        status = await asyncio.to_thread(_save_authorized_message, cfg, parsed)
    except Exception:
        # Do not log request body, message text, contact number, or secrets.
        raise HTTPException(status_code=503, detail="pilot_storage_unavailable")
    return JSONResponse({"ok": True, "status": status})

# Startup-only readiness check for operators. Never log payload, phone,
# session, provider IDs, secret values or message content.
if os.getenv("PROCASA_EXEC_PILOT_ENABLED", "").strip() == "1":
    try:
        _ready_configuration = _pilot_config()
    except HTTPException as _readiness_error:
        logging.getLogger(__name__).warning(
            "EXEC_PILOT_STARTUP ready=false reason=%s", _readiness_error.detail,
        )
    else:
        logging.getLogger(__name__).info(
            "EXEC_PILOT_STARTUP ready=true probe_only=%s",
            _ready_configuration["probe_only"],
        )
    finally:
        # Do not keep a secret copy in a module global.
        if "_ready_configuration" in globals():
            del _ready_configuration
