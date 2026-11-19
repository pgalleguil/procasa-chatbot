"""Fail-closed routing for locally executed price-drop campaigns.

Per-session WaSender keys are fetched ephemerally using the existing account PAT;
they are never stored in leads, logs or Git. No send happens in this module
unless the campaign sender explicitly calls send_with_executive_route().
A session without recently persisted signed-webhook evidence is NOT ready
for automation and therefore falls back to the office sender.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import re
from urllib.parse import urlparse

import requests

from .crm_metrics import active_assignment_cycle
from .executive_whatsapp_audit import _authorized_lead
from .phone_utils import normalize_phone_strict
from .whatsapp_client import (
    _provider_message_id,
    normalize_provider_status,
    normalize_whatsapp_recipient,
    provider_error_code,
    throttle_outgoing_message_sync,
)
from config import Config

_ENABLED = "PRICE_DROP_EXECUTIVE_SEND_ENABLED"
_BASE_URL = "https://procasa-chatbot-yr8d.onrender.com"
_SID = re.compile(r"^[1-9][0-9]{0,17}$")
_REQUIRED_EVENTS = {"messages.received", "messages.upsert"}


def enabled() -> bool:
    return os.getenv(_ENABLED, "0").strip().lower() in {"1", "true", "yes"}


def _current_assignment(db, lead: dict):
    """The assignment cycle, canonical owner and verified WhatsApp peer agree."""
    lifecycle = lead.get("lifecycle") or {}
    if not isinstance(lifecycle, dict) or not lifecycle.get("assigned_to_user_id"):
        return None
    cycle = active_assignment_cycle(db, lead["_id"])
    if (not cycle or cycle.get("cycle_status") not in (None, "active")
            or cycle.get("unassigned_at") is not None
            or str(cycle.get("assigned_to_user_id") or "") != str(lifecycle["assigned_to_user_id"])
            or not cycle.get("assignment_cycle_id")
            or str(cycle["assignment_cycle_id"]) != str(lifecycle.get("current_assignment_cycle_id") or "")):
        return None
    owner_id = str(lifecycle["assigned_to_user_id"])
    phone = normalize_phone_strict(lead.get("phone") or "")
    if not phone or lead.get("phone_is_synthetic") or lead.get("contact_identity_incomplete"):
        return None
    authorized = _authorized_lead(db, phone=phone, user_id=owner_id)
    if not authorized or str(authorized[0].get("_id")) != str(lead["_id"]):
        return None
    return cycle, owner_id


def owner_still_current(db, lead: dict, route: dict) -> bool:
    """Fresh DB read before the irreversible provider call."""
    if route.get("channel") != "executive_whatsapp":
        return True
    fresh = db["leads"].find_one({"_id": lead["_id"]})
    if not fresh or normalize_phone_strict(fresh.get("phone") or "") != normalize_phone_strict(lead.get("phone") or ""):
        return False
    assignment = _current_assignment(db, fresh)
    if not assignment:
        return False
    cycle, owner_id = assignment
    return (owner_id == route["executive_id"]
            and str(cycle["assignment_cycle_id"]) == route["assignment_cycle_id"]
            and db["usuarios"].find_one({
                "_id": route["user_oid"], "rol": "agente", "is_active": True,
                "wasender.session_id": {"$in": [route["session_id"], int(route["session_id"])]},
            }, {"_id": 1}) is not None)


def _webhook_matches(details: dict, session_id: str) -> bool:
    address = str(details.get("webhook_url") or "").strip()
    target = urlparse(address)
    expected = urlparse(os.getenv("PRICE_DROP_EXEC_AUDIT_BASE_URL", _BASE_URL))
    return (
        details.get("webhook_enabled") is True
        and target.scheme == "https"
        and target.netloc.casefold() == expected.netloc.casefold()
        and target.path.rstrip("/") == "/webhook/ejecutivos/" + session_id
        and _REQUIRED_EVENTS <= set(details.get("webhook_events") or [])
    )


def _audited_recently(db, session_id: str, now: datetime) -> bool:
    # All executive audit events have provider UTC ISO timestamps; office CRM
    # API sends alone do not prove the webhook/capture path works.
    cutoff = (now.astimezone(timezone.utc) - timedelta(days=30)).isoformat()
    return db["leads"].find_one({
        "messages": {"$elemMatch": {
            "channel": "executive_whatsapp",
            "wasender_session_id": session_id,
            "source": {"$in": ["executive_whatsapp", "whatsapp_direct"]},
            "timestamp": {"$gte": cutoff},
        }}
    }, {"_id": 1}) is not None


def resolve_route(db, lead: dict, *, now: datetime,
                  get=None) -> dict | None:
    """Return a verified executive route or None (office); never log credentials."""
    if not enabled():
        return None
    assignment = _current_assignment(db, lead)
    if not assignment:
        return None
    cycle, owner_id = assignment
    from bson import ObjectId
    if not ObjectId.is_valid(owner_id):
        return None
    user = db["usuarios"].find_one({
        "_id": ObjectId(owner_id), "rol": "agente", "is_active": True,
    }, {"_id": 1, "wasender.session_id": 1}) or {}
    sid = str((user.get("wasender") or {}).get("session_id") or "")
    if not _SID.fullmatch(sid) or not _audited_recently(db, sid, now):
        return None
    pat = str(Config.WASENDER_PERSONAL_ACCESS_TOKEN or "").strip()
    if not pat:
        return None
    url = str(Config.WASENDER_BASE_URL or "").rstrip("/") + "/whatsapp-sessions/" + sid
    if not url.startswith("https://"):
        return None
    try:
        response = (get or requests.get)(
            url, headers={"Authorization": "Bearer " + pat},
            timeout=6,
        )
        if response.status_code != 200:
            return None
        body = response.json()
        details = body.get("data") if isinstance(body, dict) else None
        if not isinstance(details, dict) or body.get("success") is not True:
            return None
        if str(details.get("id") or "") != sid:
            return None
        if str(details.get("status") or "").strip().lower() != "connected":
            return None
        if not _webhook_matches(details, sid):
            return None
        token = details.get("api_key")
        if not isinstance(token, str) or len(token.strip()) < 12:
            return None
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return None
    return {
        "channel": "executive_whatsapp",
        "session_id": sid,
        "executive_id": owner_id,
        "assignment_cycle_id": str(cycle["assignment_cycle_id"]),
        "user_oid": user["_id"],
        "api_token": token.strip(),
    }


def send_with_executive_route(route: dict, number: str, text: str, *, post=None) -> dict:
    """One actual provider request using that session only; explicit uncertainty."""
    if route.get("channel") != "executive_whatsapp" or not text:
        return {"success": False, "delivery_status": "invalid_route",
                "provider_called": False}
    try:
        to = normalize_whatsapp_recipient(number)
    except ValueError:
        return {"success": False, "delivery_status": "invalid_recipient",
                "provider_called": False}
    if "@" in to:
        return {"success": False, "delivery_status": "invalid_recipient",
                "provider_called": False}
    throttle_outgoing_message_sync()
    try:
        response = (post or requests.post)(
            str(Config.WASENDER_BASE_URL).rstrip("/") + "/send-message",
            headers={"Authorization": "Bearer " + route["api_token"],
                     "Content-Type": "application/json"},
            json={"to": to, "text": text},
            timeout=20,
        )
    except requests.RequestException:
        return {"success": False, "delivery_status": "delivery_unknown",
                "provider_call_uncertain": True, "provider_called": True}
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if response.status_code in (200, 201) and data.get("success") is True:
        pid = _provider_message_id(data)
        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
        state = normalize_provider_status(nested.get("status") or data.get("status"))
        return {"success": bool(pid), "provider_message_id": pid,
                "delivery_status": state if state != "unknown" else "accepted",
                "http_status": response.status_code, "provider_called": True,
                "provider_call_uncertain": not bool(pid)}
    return {"success": False, "delivery_status":
            "provider_auth_error" if provider_error_code(response.status_code) else "failed",
            "http_status": response.status_code, "provider_called": True}
