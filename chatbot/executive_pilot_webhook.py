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
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

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
        msg.get("from"),
    ]
    if not from_me:
        candidates.extend([key.get("senderPn"), key.get("cleanedSenderPn")])
    for raw in candidates:
        phone = _phone_number_from_jid(raw)
        if phone:
            return phone
    return None


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


def _parse_message(payload: dict, expected_phone: str) -> dict | None:
    if payload.get("event") != "messages.upsert":
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    messages = data.get("messages")
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
    peer = _remote_contact(key, messages, from_me=from_me)
    if peer != expected_phone:
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
    parsed = _parse_message(payload, cfg["contact_phone"])
    if parsed is None:
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
