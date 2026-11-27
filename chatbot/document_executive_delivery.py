"""Verified executive document invitations, with explicit office contingency."""
from datetime import datetime, timezone
import re
import logging
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


class OfficeDocumentFallback(HTTPException):
    """No executive POST occurred: office transport is safe to consider."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(409, "WhatsApp del ejecutivo no disponible.")


def resolve_document_sender(db, document, username):
    if not username:
        raise HTTPException(401, "Inicie sesión para enviar el documento.")
    actor = db["usuarios"].find_one({"username": username, "is_active": True})
    if not actor:
        raise HTTPException(403, "Usuario no autorizado.")
    if document.get("status") == "deleted":
        raise HTTPException(409, "El documento fue eliminado.")
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
        raise OfficeDocumentFallback("session_not_linked")
    session = _session(db, sid)
    if not session or str(session.get("user_id")) != str(owner["_id"]):
        raise OfficeDocumentFallback("session_configuration_invalid")
    status = _session_status(session)
    if status != "connected":
        raise OfficeDocumentFallback("session_disconnected" if status in _DISCONNECTED_STATES else "session_unavailable")
    try:
        verify_provider_sender_identity(session, expected_phone=document_recipient(owner.get("telefono") or owner.get("phone")), force=True)
    except OutboundBlocked as exc:
        raise OfficeDocumentFallback(exc.code) from None
    except HTTPException:
        raise OfficeDocumentFallback("owner_phone_invalid") from None
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


logger = logging.getLogger(__name__)

# Only definite sender-specific rejection permits another customer POST.
# A timeout, ambiguous HTTP response or invalid recipient never does.
OFFICE_RETRY_REASONS = frozenset({
    "session_disconnected", "authentication", "subscription",
})


def office_session(db):
    token = Config.WASENDER_TOKEN
    if not token:
        return None
    # Never use an executive's known token as the office fallback.
    from chatbot.executive_whatsapp_audit import _executive_credentials
    for user in db["usuarios"].find({"rol": "agente", "is_active": True},
                                    {"nombre": 1, "wasender.session_id": 1}):
        sid = str((user.get("wasender") or {}).get("session_id") or "")
        if sid:
            config = _executive_credentials(user.get("nombre"), sid)
            if config and config.get("token") == token:
                return None
    session = {"token": token, "user_id": "office", "session_id": "office"}
    return session if _session_status(session) == "connected" else None


async def alert_document_problem(docs, guard, *, db, document, collection, reason,
                                 transport, outcome, attempt):
    """One durable operator notification per document attempt, no customer link."""
    from chatbot.executive_session_admin_alerts import _pablo_recipient
    claimed = await docs.update_one(
        {**guard, "whatsapp_invitation.admin_alert.state": {"$exists": False}},
        {"$set": {"whatsapp_invitation.admin_alert": {
            "state": "reserved", "reason": reason, "at": datetime.now(timezone.utc),
        }}},
    )
    if claimed.modified_count != 1:
        return
    state, pid, error = "rejected", None, "admin_recipient_unverified"
    try:
        admin = await run_in_threadpool(_pablo_recipient, db)
        if admin:
            await throttle_outgoing_message()
            office = await run_in_threadpool(office_session, db)
            if office:
                label = "Orden de visita" if collection == "visitas" else "Convenio de corretaje"
                code = document.get("visita_code") or document.get("contract_code") or str(document["_id"])
                result_labels = {"accepted": "Aceptado por WhatsApp", "rejected": "No enviado",
                                 "delivery_unknown": "Envío sin confirmar", "blocked": "Bloqueado"}
                reasons = {
                    "session_not_linked": "WhatsApp no vinculado",
                    "session_configuration_invalid": "Configuración de sesión inválida",
                    "session_disconnected": "WhatsApp desconectado",
                    "session_unavailable": "Conexión no verificable",
                    "sender_identity_mismatch": "Número emisor no coincide",
                    "sender_identity_unverified": "Identidad del emisor no verificable",
                    "owner_phone_invalid": "Número del ejecutivo inválido",
                    "authentication": "Credenciales rechazadas",
                    "subscription": "Problema de suscripción",
                    "provider_transport_uncertain": "Respuesta de envío incierta",
                    "provider_response_unverifiable": "Respuesta de envío no verificable",
                    "provider_id_missing": "Proveedor sin identificador de envío",
                    "recipient_not_registered": "Destinatario sin WhatsApp",
                    "invalid_recipient": "Destinatario inválido",
                    "invalid_request": "Solicitud rechazada",
                    "rate_limit": "Límite del proveedor",
                    "office_unavailable": "WhatsApp de oficina no disponible",
                }
                msg = ("ALERTA PROCASA - Envío de documento\n"
                       f"Documento: {label} {code}\n"
                       f"Ejecutivo: {document.get('created_by', '')}\n"
                       f"Motivo: {reasons.get(reason, reason)}\n"
                       f"Canal: {'Oficina (contingencia)' if transport == 'office_wasender_api' else 'Ejecutivo'}\n"
                       f"Resultado: {result_labels.get(outcome, outcome)}")
                state, pid, error = await run_in_threadpool(dispatch_document, office, admin, msg)
            else:
                error = "office_unavailable"
    except Exception as exc:
        error = "alert_exception"
        logger.error("DOCUMENT_ADMIN_ALERT attempt=%s type=%s", attempt, type(exc).__name__)
    await docs.update_one(guard, {"$set": {
        "whatsapp_invitation.admin_alert.state": state,
        "whatsapp_invitation.admin_alert.provider_message_id": pid,
        "whatsapp_invitation.admin_alert.error": error,
        "whatsapp_invitation.admin_alert.finished_at": datetime.now(timezone.utc),
    }})
    if state != "accepted":
        logger.error("DOCUMENT_ADMIN_ALERT attempt=%s state=%s reason=%s", attempt, state, error)


async def send_document_invitation(adb, *, collection, document, username, phone,
                                   message, token, expiry, action, server_timestamp, ip, ua):
    from chatbot.storage import get_db
    recipient = document_recipient(phone)
    docs = adb[collection]
    db = get_db()
    await throttle_outgoing_message()
    fallback_reason = None
    try:
        session = await run_in_threadpool(resolve_document_sender, db, document, username)
    except OfficeDocumentFallback as exc:
        fallback_reason = exc.reason
        session = None

    previous_status = document.get("status", "created")
    security = document.get("security") or {}
    attempt = uuid4().hex
    now = datetime.now(timezone.utc)
    transport = "office_wasender_api" if fallback_reason else "executive_wasender_api"
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
                "sender_user_id": session["user_id"] if session else "office",
                "sender_session_id": session["session_id"] if session else "office",
                "requested_by": username, "transport": transport,
                "fallback_reason": fallback_reason,
            },
        }},
    )
    if reservation.modified_count != 1:
        raise HTTPException(409, "Existe un envío pendiente o manual previo. Revise su estado antes de reenviar.")
    guard = {"_id": document["_id"], "whatsapp_invitation.attempt_id": attempt}
    if fallback_reason:
        session = await run_in_threadpool(office_session, db)
        state, pid, reason = ("rejected", None, "office_unavailable") if not session else await run_in_threadpool(dispatch_document, session, recipient, message)
    else:
        state, pid, reason = await run_in_threadpool(dispatch_document, session, recipient, message)
        if state == "rejected" and reason in OFFICE_RETRY_REASONS:
            fallback_reason = reason
            transport = "office_wasender_api"
            # Persist the confirmed rejection BEFORE switching sender.
            await docs.update_one(guard, {"$set": {
                "whatsapp_invitation.executive_rejection": reason,
                "whatsapp_invitation.fallback_reason": reason,
                "whatsapp_invitation.transport": transport,
                "whatsapp_invitation.sender_user_id": "office",
                "whatsapp_invitation.sender_session_id": "office",
            }})
            await throttle_outgoing_message()
            session = await run_in_threadpool(office_session, db)
            state, pid, reason = ("rejected", None, "office_unavailable") if not session else await run_in_threadpool(dispatch_document, session, recipient, message)

    finish = datetime.now(timezone.utc)
    fields = {"whatsapp_invitation.state": state, "whatsapp_invitation.finished_at": finish,
              "whatsapp_invitation.provider_message_id": pid, "whatsapp_invitation.reason": reason}
    sender_id = session["user_id"] if session else "office"
    sender_sid = session["session_id"] if session else "office"
    if state == "accepted":
        if previous_status == "created":
            await docs.update_one({**guard, "status": "created", "security.token_used": {"$ne": True}},
                                  {"$set": {"status": "sent"}})
        # Keep reservation until notification is persisted to avoid racing a resend.
        await docs.update_one(guard, {"$set": {k: v for k, v in fields.items() if k != "whatsapp_invitation.state"}, "$push": {
            "timeline": {"action": action, "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua,
                         "transport": transport, "fallback_reason": fallback_reason},
            "messages": {"phone": recipient, "message_content": message, "message_type": action,
                         "timestamp_utc": finish, "sender_user_id": sender_id,
                         "sender_session_id": sender_sid, "provider_message_id": pid, "source": transport},
        }})
    else:
        if state == "rejected":
            await docs.update_one({**guard, "status": previous_status, "security.token": token,
                                   "security.token_used": {"$ne": True}},
                                  {"$set": {"security.token": security.get("token"),
                                            "security.token_expiry": security.get("token_expiry")}})
        await docs.update_one(guard, {"$set": {k: v for k, v in fields.items() if k != "whatsapp_invitation.state"},
            "$push": {"timeline": {"action": "whatsapp_invitation_" + state,
                "server_timestamp": server_timestamp, "reason": reason, "transport": transport,
                "fallback_reason": fallback_reason, "sender_user_id": sender_id,
                "sender_session_id": sender_sid}}})
    if fallback_reason or state != "accepted":
        try:
            await alert_document_problem(docs, guard, db=db, document=document, collection=collection,
                reason=fallback_reason or reason or "provider_rejected",
                transport=transport, outcome=state, attempt=attempt)
        except Exception as exc:
            logger.error("DOCUMENT_ADMIN_ALERT attempt=%s persist_error=%s", attempt, type(exc).__name__)
    await docs.update_one(guard, {"$set": fields})
    if state == "accepted":
        return {"status": "ok", "transport": transport,
                "message": "Enviado por WhatsApp de oficina (contingencia)" if fallback_reason else "Enviado por WhatsApp del ejecutivo"}
    if state == "delivery_unknown":
        raise HTTPException(502, "Envío no confirmado. Verifique el chat antes de reenviar; no se realizará otro envío automático.")
    raise HTTPException(502, "WhatsApp rechazó el envío o la contingencia no está disponible. Revise la incidencia.")
