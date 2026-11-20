"""Single-use, opt-in WhatsApp transport probe for an approved PROCASA supervisor.

This module is deliberately separate from the commercial CRM:
- never creates or changes a lead, assignment, SLA, campaign or bot reply;
- never stores the test conversation's body, phone number, or chat JID;
- one provider SEND per nonce, guarded by an atomic MongoDB _id claim;
- only the specifically configured, active supervisor user can receive it;
- webhook observations are metadata-only and require a signed executive event.

Do not enable this for regular operation. Unset its env flags after the test.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import re

from bson import ObjectId
from pymongo.errors import DuplicateKeyError
import requests

from config import Config
from .phone_utils import has_real_phone, normalize_phone_strict
from .whatsapp_client import _provider_message_id, normalize_whatsapp_recipient

logger = logging.getLogger(__name__)

COLLECTION = "crm_executive_whatsapp_e2e_probes"
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{12,80}$")
SESSION_ID = "133526"
MARIELA_USER_ID = "6989c6309dd2ba54e478196c"
TEXT = (
    "Prueba tecnica autorizada por administracion PROCASA desde el "
    "WhatsApp vinculado de Mariela. Por favor responde: RECIBIDO."
)


def _config() -> tuple[str, ObjectId, str] | None:
    if os.getenv("PROCASA_EXEC_E2E_PROBE_ENABLED") != "1":
        return None
    if os.getenv("PROCASA_EXEC_E2E_PROBE_SESSION_ID") != SESSION_ID:
        return None
    nonce = os.getenv("PROCASA_EXEC_E2E_PROBE_NONCE", "")
    recipient_id = os.getenv("PROCASA_EXEC_E2E_PROBE_SUPERVISOR_USER_ID", "")
    suffix = os.getenv("PROCASA_EXEC_E2E_PROBE_SUPERVISOR_PHONE_LAST4", "")
    if not NONCE_RE.fullmatch(nonce):
        return None
    if not ObjectId.is_valid(recipient_id):
        return None
    if not (len(suffix) == 4 and suffix.isascii() and suffix.isdigit()):
        return None
    return nonce, ObjectId(recipient_id), suffix


def _recipient(db, config: tuple[str, ObjectId, str]) -> str | None:
    user = db["usuarios"].find_one({
        "_id": config[1], "rol": "supervisor", "is_active": True,
    }, {"telefono": 1})
    if not user:
        return None
    phone = normalize_phone_strict(user.get("telefono"))
    if not phone or not has_real_phone(phone) or not phone.endswith(config[2]):
        return None
    try:
        return normalize_whatsapp_recipient(phone)
    except ValueError:
        return None


def run_once(db, session_loader) -> None:
    """A one-time, bounded provider call. Never retries on uncertain delivery."""
    cfg = _config()
    if cfg is None:
        return
    nonce, recipient_id, _ = cfg
    session = session_loader(db, SESSION_ID)
    if (not session or session.get("user_id") != MARIELA_USER_ID
            or session.get("capture_enabled") is not True
            or os.getenv("PROCASA_EXEC_AUDIT_WRITE_ENABLED") != "1"):
        logger.info("EXEC_E2E_PROBE session=%s stage=configuration_blocked", SESSION_ID)
        return
    to = _recipient(db, cfg)
    if not to:
        logger.info("EXEC_E2E_PROBE session=%s stage=recipient_blocked", SESSION_ID)
        return
    claim = "executive-e2e:" + nonce
    try:
        db[COLLECTION].insert_one({
            "_id": claim,
            "session_id": SESSION_ID,
            "recipient_user_id": recipient_id,
            "created_at": datetime.now(timezone.utc),
            "state": "provider_attempt",
        })
    except DuplicateKeyError:
        # Shared across all web workers/restarts: no second outbound message.
        logger.info("EXEC_E2E_PROBE session=%s stage=already_claimed", SESSION_ID)
        return

    try:
        response = requests.post(
            Config.WASENDER_BASE_URL.rstrip("/") + "/send-message",
            headers={
                "Authorization": "Bearer " + session["token"],
                "Content-Type": "application/json",
            },
            json={"to": to, "text": TEXT},
            timeout=15,
        )
        body = response.json() if response.content else {}
        body = body if isinstance(body, dict) else {}
        if response.status_code in (200, 201) and body.get("success") is True:
            state = "provider_accepted" if _provider_message_id(body) else "provider_accepted_no_id"
        elif response.status_code in (401, 403):
            state = "provider_auth_rejected"
        elif response.status_code == 429:
            state = "provider_rate_limited"
        elif response.status_code >= 500:
            state = "provider_server_rejected"
        else:
            state = "provider_rejected"
    except Exception:
        state = "delivery_unknown"
    db[COLLECTION].update_one({"_id": claim, "state": "provider_attempt"}, {
        "$set": {"state": state, "provider_result_at": datetime.now(timezone.utc)}
    })
    # No recipient phone, message text, token, provider response, or JID in logs.
    logger.info("EXEC_E2E_PROBE session=%s stage=%s", SESSION_ID, state)


def observe_signed_event(db, session: dict, phone: str, *, from_me: bool) -> None:
    """Record *only* a matching, signed test peer/direction, never chat content."""
    cfg = _config()
    if cfg is None or str(session.get("session_id")) != SESSION_ID:
        return
    target = _recipient(db, cfg)
    if target is None or phone != target:
        return
    claim = "executive-e2e:" + cfg[0]
    row = db[COLLECTION].find_one({
        "_id": claim, "session_id": SESSION_ID,
        "recipient_user_id": cfg[1],
        "state": {"$in": ["provider_accepted", "provider_accepted_no_id"]},
        "created_at": {"$gte": datetime.now(timezone.utc) - timedelta(hours=24)},
    }, {"_id": 1})
    if not row:
        return
    field = "outbound_webhook_at" if from_me else "inbound_webhook_at"
    result = db[COLLECTION].update_one({"_id": claim, field: {"$exists": False}}, {
        "$set": {field: datetime.now(timezone.utc)}
    })
    if result.modified_count:
        logger.info(
            "EXEC_E2E_PROBE session=%s stage=%s",
            SESSION_ID, "outbound_webhook_seen" if from_me else "inbound_webhook_seen",
        )
