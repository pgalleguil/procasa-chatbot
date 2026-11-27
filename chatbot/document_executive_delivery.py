"""Document signing invitations: use only the recorded creator's verified session."""
from datetime import datetime, timezone
import re
from urllib.parse import quote
from uuid import uuid4

import requests
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from config import Config
from chatbot.executive_whatsapp_audit import _session
from chatbot.crm_executive_outbound import (
    OutboundBlocked, verify_provider_sender_identity, _provider_rejection_reason,
)
from chatbot.crm_executive_outbound_api import _session_status, _DISCONNECTED_STATES
from chatbot.whatsapp_client import _provider_message_id, throttle_outgoing_message


def document_recipient(phone):
    raw = str(phone or "").strip()
    if not re.fullmatch(r"[+0-9 ()-]+", raw):
        raise HTTPException(400, "Teléfono inválido para WhatsApp.")
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) == 9 and digits.startswith("9") and not raw.startswith("+"):
        digits = "56" + digits
    if not re.fullmatch(r"[1-9][0-9]{7,14}", digits):
        raise HTTPException(400, "Teléfono inválido para WhatsApp.")
    if digits.startswith("56") and not re.fullmatch(r"569[0-9]{8}", digits):
        raise HTTPException(400, "Teléfono chileno inválido para WhatsApp.")
    return "+" + digits


class ManualDocumentFallback(HTTPException):
    """No API request was made: only the actual creator may send manually."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(409, "WhatsApp no vinculado o desconectado. Use su propio WhatsApp para el envío manual.")


def resolve_document_sender(db, document, username):
    if not username:
        raise HTTPException(401, "Inicie sesión para enviar el documento.")
    actor = db["usuarios"].find_one({"username": username, "is_active": True})
    if not actor:
        raise HTTPException(403, "Usuario no autorizado.")
    creator = document.get("created_by")
    if not creator or (username != creator and actor.get("rol") not in ("admin", "supervisor")):
        raise HTTPException(403, "Solo el creador o un supervisor autorizado puede enviar este documento.")
    if document.get("status") in ("sent", "opened", "otp_requested", "otp_verified", "signed", "accepted") and actor.get("rol") not in ("admin", "supervisor"):
        raise HTTPException(403, "Solo supervisor/admin puede reenviar un documento ya enviado.")
    owner = db["usuarios"].find_one({"username": creator, "is_active": True, "rol": "agente"})
    if not owner:
        raise HTTPException(409, "El creador no tiene una cuenta de ejecutivo activa para este envío.")
    sid = str((owner.get("wasender") or {}).get("session_id") or "")
    if not sid:
        if username == creator:
            raise ManualDocumentFallback("session_not_linked")
        raise HTTPException(409, "El creador debe enviar desde su propio WhatsApp.")
    session = _session(db, sid)
    if not session or str(session.get("user_id")) != str(owner["_id"]):
        raise HTTPException(409, "El ejecutivo creador debe vincular su WhatsApp antes de enviar.")
    status = _session_status(session)
    if status in _DISCONNECTED_STATES and username == creator:
        raise ManualDocumentFallback("session_disconnected")
    if status != "connected":
        raise HTTPException(409, "WhatsApp del ejecutivo no está conectado o no se pudo verificar. No se utilizó el teléfono de oficina.")
    try:
        verify_provider_sender_identity(session, expected_phone=document_recipient(owner.get("telefono") or owner.get("phone")), force=True)
    except (OutboundBlocked, HTTPException):
        raise HTTPException(409, "No se pudo verificar el número de WhatsApp del ejecutivo. Contacte al administrador.") from None
    return session


def dispatch_document(session, recipient, message, *, post=None):
    """One provider request; never automatically retry an uncertain outcome."""
    try:
        response = (post or requests.post)(
            Config.WASENDER_BASE_URL.rstrip("/") + "/send-message",
            json={"to": recipient, "text": message},
            headers={"Authorization": "Bearer " + session["token"], "Content-Type": "application/json"},
            timeout=15,
        )
        status = response.status_code
        try:
            body = response.json()
        except (ValueError, TypeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        if status in (200, 201) and body.get("success") is True:
            pid = _provider_message_id(body)
            return ("accepted", pid, None) if pid else ("delivery_unknown", None, "provider_id_missing")
        if status in (408, 425) or status >= 500 or (200 <= status < 300 and body.get("success") is not False):
            return "delivery_unknown", None, "provider_response_unverifiable"
        return "rejected", None, _provider_rejection_reason(status, body)
    except Exception:
        return "delivery_unknown", None, "provider_transport_uncertain"


async def send_document_invitation(adb, *, collection, document, username, phone,
                                   message, token, expiry, action, server_timestamp, ip, ua):
    """Reserve a durable attempt before the external request; preserve signature state."""
    from chatbot.storage import get_db

    recipient = document_recipient(phone)
    docs = adb[collection]
    # Apply spacing BEFORE checking provider health: a queued request must
    # not use connection evidence from before the throttle wait.
    await throttle_outgoing_message()
    fallback_reason = None
    try:
        session = await run_in_threadpool(resolve_document_sender, get_db(), document, username)
    except ManualDocumentFallback as exc:
        fallback_reason = exc.reason
        session = None
    prior = document.get("whatsapp_invitation") or {}
    if prior.get("state") == "manual_prepared":
        # Once a manual link is exposed we cannot know if it was sent.
        # Never silently switch to API and risk a duplicate.
        raise HTTPException(409, "Ya se preparó un envío manual. Verifique el chat del ejecutivo antes de un nuevo envío.")
    previous_status = document.get("status", "created")
    security = document.get("security") or {}
    attempt = uuid4().hex
    now = datetime.now(timezone.utc)
    if fallback_reason:
        reservation = await docs.update_one(
            {"_id": document["_id"], "created_by": username,
             "status": previous_status, "security.token": security.get("token"),
             "security.token_used": {"$ne": True},
             "whatsapp_invitation.state": {"$nin": ["sending", "delivery_unknown", "manual_prepared"]}},
            {"$set": {"security.token": token, "security.token_expiry": expiry,
                      "security.token_used": False,
                      "whatsapp_invitation": {
                          "attempt_id": attempt, "state": "manual_prepared",
                          "started_at": now, "requested_by": username,
                          "transport": "manual_whatsapp", "reason": fallback_reason,
                      }},
             "$push": {"timeline": {"action": "whatsapp_manual_prepared",
                                     "server_timestamp": server_timestamp,
                                     "reason": fallback_reason, "requested_by": username}}},
        )
        if reservation.modified_count != 1:
            raise HTTPException(409, "El documento cambió o ya tiene un envío pendiente.")
        return {"status": "manual_fallback",
                "whatsapp_url": "https://wa.me/" + recipient.lstrip("+") + "?text=" + quote(message, safe=""),
                "message": "No se ha enviado. Abra su propio WhatsApp y confirme manualmente el envío."}
    reservation = await docs.update_one(
        {"_id": document["_id"], "created_by": document["created_by"],
         "status": previous_status, "security.token": security.get("token"),
         "security.token_used": {"$ne": True},
         "whatsapp_invitation.state": {"$nin": ["sending", "delivery_unknown", "manual_prepared"]}},
        {"$set": {
            "security.token": token, "security.token_expiry": expiry,
            "security.token_used": False,
            "whatsapp_invitation": {
                "attempt_id": attempt, "state": "sending", "started_at": now,
                "sender_user_id": session["user_id"], "sender_session_id": session["session_id"],
                "requested_by": username, "transport": "executive_wasender_api",
            },
        }},
    )
    if reservation.modified_count != 1:
        raise HTTPException(409, "Existe un envío pendiente o cambió el documento. Revise su estado antes de reenviar.")
    state, pid, reason = await run_in_threadpool(dispatch_document, session, recipient, message)
    finish = datetime.now(timezone.utc)
    guard = {"_id": document["_id"], "whatsapp_invitation.attempt_id": attempt}
    fields = {"whatsapp_invitation.state": state, "whatsapp_invitation.finished_at": finish,
              "whatsapp_invitation.provider_message_id": pid, "whatsapp_invitation.reason": reason}
    if state == "accepted":
        # If the recipient opened or signed while the request was in flight,
        # never overwrite the newer status.
        if previous_status == "created":
            await docs.update_one({**guard, "status": "created", "security.token_used": {"$ne": True}},
                                  {"$set": {"status": "sent"}})
        await docs.update_one(guard, {"$set": fields, "$push": {
            "timeline": {"action": action, "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua},
            "messages": {"phone": recipient, "message_content": message, "message_type": action,
                         "timestamp_utc": finish, "sender_user_id": session["user_id"],
                         "sender_session_id": session["session_id"], "provider_message_id": pid,
                         "source": "executive_wasender_api"},
        }})
        return {"status": "ok", "message": "Enviado por WhatsApp del ejecutivo"}
    if state == "rejected":
        await docs.update_one({**guard, "status": previous_status, "security.token": token,
                               "security.token_used": {"$ne": True}},
                              {"$set": {"security.token": security.get("token"),
                                        "security.token_expiry": security.get("token_expiry")}})
    await docs.update_one(guard, {"$set": fields, "$push": {"timeline": {
        "action": "whatsapp_invitation_" + state, "server_timestamp": server_timestamp,
        "reason": reason, "sender_user_id": session["user_id"], "sender_session_id": session["session_id"],
    }}})
    if state == "delivery_unknown":
        raise HTTPException(502, "Envío no confirmado. Verifique el WhatsApp del ejecutivo antes de reenviar; no se usó la oficina.")
    raise HTTPException(502, "WhatsApp rechazó el envío del ejecutivo. Revise la conexión o el número del destinatario.")
