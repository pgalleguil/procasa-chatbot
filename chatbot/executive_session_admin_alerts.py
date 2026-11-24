"""Event-triggered executive WhatsApp incident alerts exclusively to Pablo.

Never poll the provider on a timer. A CRM WhatsApp preflight already verifies
sender identity and session status; use that result to notify Pablo only
when there is a real sending attempt. Alerts use an independent office sender,
never the executive session. Durable CAS prevents duplicate incident messages.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os

from bson import ObjectId
from pymongo import ReturnDocument

from .crm_executive_outbound import OutboundBlocked, verify_provider_sender_identity
from .crm_executive_outbound_api import _session_status
from .executive_whatsapp_audit import _session

logger = logging.getLogger(__name__)

FLAG = "PROCASA_EXEC_WA_PABLO_ALERTS_ENABLED"
STATE_COLLECTION = "crm_exec_whatsapp_session_health"
PABLO_ID = ObjectId("69796bc4bbebf240378eb739")
PABLO_PHONE = "+56983219804"  # Recipient safety assertion, not a broadcast list.
EXECUTIVES = (
    ("133286", "Pablo Galleguillos"),
    ("133517", "Hernán Castro"),
    ("133523", "Susana Ensignia"),
    ("133526", "Mariela Arriagada"),
    ("133527", "Paula Morales"),
)
DISCONNECTED = frozenset({
    "disconnected", "logged_out", "expired", "need_scan", "need_passkey",
})
ALLOWED_REASONS = DISCONNECTED | frozenset({
    "sender_identity_mismatch", "session_unavailable",
    "owner_phone_unavailable", "provider_unavailable",
    "sender_identity_unverified", "provider_identity_unavailable",
})


def _health(db, session_id: str, executive_name: str) -> str:
    """Read WaSender identity and status with no writes, retries or payload logs."""
    try:
        session = _session(db, session_id)
        if session is None or str(session.get("session_id")) != session_id:
            return "session_unavailable"
        owner = db["usuarios"].find_one({
            "_id": session["user_oid"], "rol": "agente", "is_active": True,
            "nombre": executive_name,
            "wasender.session_id": {"$in": [session_id, int(session_id)]},
        }, {"telefono": 1})
        if not owner or not owner.get("telefono"):
            return "owner_phone_unavailable"
        verify_provider_sender_identity(
            session, expected_phone=owner["telefono"], force=True,
        )
        status = _session_status(session)
        return "connected" if status == "connected" else (
            status if status in DISCONNECTED else "provider_unavailable"
        )
    except OutboundBlocked as error:
        return error.code if error.code in ALLOWED_REASONS else "provider_identity_unavailable"
    except Exception as error:
        logger.warning("EXEC_WA_HEALTH session=%s probe_exception=%s",
                       session_id, type(error).__name__)
        return "provider_unavailable"


def _record_health(db, *, session_id: str, reason: str, now=None) -> bool:
    """Atomically reserve ONE alert after repeated unhealthy probes.

    The reservation is durable and is written BEFORE contacting WaSender.
    Unknown delivery is not automatically retried, avoiding duplicate alerts.
    """
    if session_id not in {row[0] for row in EXECUTIVES}:
        raise ValueError("session_not_authorized")
    now = now or datetime.now(timezone.utc)
    collection = db[STATE_COLLECTION]
    if reason == "connected":
        collection.update_one({"_id": session_id}, {
            "$set": {
                "last_check_at": now, "last_status": "connected",
                "unhealthy_checks": 0, "alert_claimed": False,
                "alert_delivery": "healthy",
            }
        }, upsert=True)
        return False
    # Unknown provider errors require a third check; hard disconnect or a
    # mismatched sender requires two checks to avoid flapping/redeploy noise.
    threshold = 3 if reason in {"provider_unavailable",
                                "provider_identity_unavailable"} else 2
    state = collection.find_one_and_update(
        {"_id": session_id},
        {"$inc": {"unhealthy_checks": 1},
         "$set": {"last_check_at": now, "last_status": reason},
         "$setOnInsert": {"alert_claimed": False}},
        upsert=True, return_document=ReturnDocument.AFTER,
    )
    if int(state.get("unhealthy_checks") or 0) < threshold:
        return False
    claim = collection.find_one_and_update(
        {"_id": session_id, "alert_claimed": False,
         "unhealthy_checks": {"$gte": threshold}},
        {"$set": {
            "alert_claimed": True,
            "alert_delivery": "reserved",
            "alert_reserved_at": now,
            "alert_reason": reason,
        }},
        return_document=ReturnDocument.AFTER,
    )
    return claim is not None


def _pablo_recipient(db) -> str | None:
    from .whatsapp_client import normalize_whatsapp_recipient
    user = db["usuarios"].find_one(
        {"_id": PABLO_ID, "nombre": "Pablo Galleguillos",
         "rol": "agente", "is_active": True},
        {"telefono": 1},
    )
    if not user:
        return None
    try:
        phone = normalize_whatsapp_recipient(user.get("telefono"))
    except (ValueError, TypeError):
        return None
    # Never silently redirect security incidents to a phone changed in Mongo.
    return phone if phone == PABLO_PHONE else None


async def check_sessions_once(*, db=None, send=None) -> list[dict]:
    """A single monitoring pass; suitable for tests or an explicit task run.

    Returns reason codes only, never credentials, numbers or chat content.
    """
    from .storage import get_db
    from .whatsapp_client import send_whatsapp_message_detailed
    db = db if db is not None else await asyncio.to_thread(get_db)
    sender = send or send_whatsapp_message_detailed
    outcomes = []
    for session_id, executive_name in EXECUTIVES:
        reason = await asyncio.to_thread(_health, db, session_id, executive_name)
        claimed = await asyncio.to_thread(
            _record_health, db, session_id=session_id, reason=reason,
        )
        if not claimed:
            outcomes.append({"session": session_id, "state": reason,
                             "alert": "not_required"})
            continue
        recipient = await asyncio.to_thread(_pablo_recipient, db)
        if recipient is None:
            await asyncio.to_thread(db[STATE_COLLECTION].update_one,
                {"_id": session_id},
                {"$set": {"alert_delivery": "recipient_unverified"}})
            logger.error("EXEC_WA_PABLO_ALERT session=%s result=recipient_unverified",
                         session_id)
            outcomes.append({"session": session_id, "state": reason,
                             "alert": "recipient_unverified"})
            continue
        message = (
            "ALERTA PROCASA - WhatsApp ejecutivo\n"
            f"Ejecutivo: {executive_name}\n"
            f"Sesión WaSender: {session_id}\n"
            f"Estado: {reason}\n"
            "Verificar la vinculación y la API en WaSender. "
            "El CRM ofrece WhatsApp Web cuando el envío alternativo es seguro."
        )
        # Delivery uses the office sender, NEVER this disconnected session.
        try:
            result = await sender(recipient, message)
            delivery = ("accepted" if result.get("success") is True
                        and result.get("provider_message_id") else "delivery_unknown"
                        if result.get("provider_call_uncertain")
                        or result.get("success") is True else "failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delivery = "delivery_unknown"
            logger.error("EXEC_WA_PABLO_ALERT session=%s send_exception=%s",
                         session_id, type(exc).__name__)
        await asyncio.to_thread(
            db[STATE_COLLECTION].update_one,
            {"_id": session_id, "alert_delivery": "reserved"},
            {"$set": {"alert_delivery": delivery,
                      "alert_result_at": datetime.now(timezone.utc)}},
        )
        logger.info("EXEC_WA_PABLO_ALERT session=%s result=%s", session_id, delivery)
        outcomes.append({"session": session_id, "state": reason, "alert": delivery})
    return outcomes


async def session_monitor_loop() -> None:
    """Periodic, fail-isolated monitor; no messages when opt-in is disabled."""
    if os.getenv(FLAG, "0") != "1":
        logger.info("EXEC_WA_PABLO_ALERT status=disabled")
        return
    raw_interval = os.getenv("PROCASA_EXEC_WA_PABLO_ALERT_INTERVAL_SECONDS", "180")
    try:
        interval = max(60, min(int(raw_interval), 900))
    except ValueError:
        interval = 180
    logger.info("EXEC_WA_PABLO_ALERT status=enabled interval_seconds=%s", interval)
    while True:
        try:
            outcomes = await check_sessions_once()
            logger.info("EXEC_WA_HEALTH checked=%s alerts_claimed=%s",
                        len(outcomes),
                        sum(x["alert"] != "not_required" for x in outcomes))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("EXEC_WA_HEALTH check_failure=%s", type(exc).__name__)
        await asyncio.sleep(interval)


# Event-driven replacement for the old periodic service. Only CRM WhatsApp
# preflight/send endpoints call this. No extra GET /user or GET /status calls.
_CRM_ALERT_REASONS = frozenset({
    "session_unavailable", "session_disconnected", "authentication",
    "sender_identity_mismatch", "logged_out", "expired", "need_scan",
})


def mark_event_recovered(db, *, session_id: str) -> None:
    """Re-arm a notification ONLY after CRM already verified a live session.

    Does not probe WaSender, send messages or touch other executive sessions.
    In-flight notifications remain reserved to avoid a race with another click.
    """
    if os.getenv(FLAG, "0") != "1":
        return
    if session_id not in dict(EXECUTIVES):
        return
    db[STATE_COLLECTION].update_one(
        {"_id": session_id, "alert_claimed": True,
         "alert_delivery": {"$ne": "reserved"}},
        {"$set": {"alert_claimed": False, "alert_delivery": "healthy",
                  "last_status": "connected", "unhealthy_checks": 0,
                  "last_check_at": datetime.now(timezone.utc)}},
    )


_CRM_SEND_ALERT_REASONS = _CRM_ALERT_REASONS | frozenset({
    "invalid_request", "invalid_recipient", "recipient_not_registered", "provider_rejected",
    "subscription", "rate_limit", "delivery_unconfirmed",
})


async def notify_pablo_on_crm_failure(
    *, session_id: str, executive_name: str, reason: str,
    lead_id: str | None = None, db=None, send=None,
) -> str:
    """Send at most one operator alert per outage *on an executive CRM action*.

    The provider has ALREADY been checked by the authenticated CRM send
    preflight. This method performs zero new session probes. Mongo reservation
    is committed before the independent office WhatsApp send; unknown or
    failed sends are never automatically retried.
    """
    if os.getenv(FLAG, "0") != "1":
        return "disabled"
    if dict(EXECUTIVES).get(session_id) != executive_name:
        return "executive_not_authorized"
    if lead_id is not None and not ObjectId.is_valid(lead_id):
        return "invalid_lead_id"
    if reason not in (_CRM_SEND_ALERT_REASONS if lead_id else _CRM_ALERT_REASONS):
        return "reason_not_alertable"
    # Post-send errors are scoped per lead and reason, not session health.
    # A connected preflight cannot re-arm them. Deduplicate repeated clicks
    # during the local calendar day without suppressing other affected leads.
    from .constants import CHILE_TZ
    alert_id = (f"crm-send:{session_id}:{lead_id}:{reason}:"
                f"{datetime.now(CHILE_TZ).date().isoformat()}"
                if lead_id else session_id)
    from .storage import get_db
    from .whatsapp_client import send_whatsapp_message_detailed
    try:
        db = db if db is not None else await asyncio.to_thread(get_db)
        collection = db[STATE_COLLECTION]

        # Ensure the document exists without overwriting prior state or any
        # other session's health. _id is indexed, avoiding collection scans.
        await asyncio.to_thread(
            collection.update_one, {"_id": alert_id},
            {"$setOnInsert": {"alert_claimed": False}}, upsert=True,
        )
        claimed = await asyncio.to_thread(
            collection.find_one_and_update,
            {"_id": alert_id, "alert_claimed": False},
            {"$set": {
                "alert_claimed": True, "alert_delivery": "reserved",
                "alert_reason": reason,
                "session_id": session_id,
                **({"lead_id": ObjectId(lead_id)} if lead_id else {}),
                "last_status": reason,
                "alert_reserved_at": datetime.now(timezone.utc),
            }},
            return_document=ReturnDocument.AFTER,
        )
        if claimed is None:
            return "already_notified"

        recipient = await asyncio.to_thread(_pablo_recipient, db)
        if recipient is None:
            await asyncio.to_thread(
                collection.update_one, {"_id": alert_id},
                {"$set": {"alert_delivery": "recipient_unverified"}},
            )
            logger.error(
                "EXEC_WA_PABLO_EVENT session=%s result=recipient_unverified",
                session_id,
            )
            return "recipient_unverified"

        message = (
            "ALERTA PROCASA - WhatsApp ejecutivo\\n"
            f"Ejecutivo: {executive_name}\\n"
            f"Sesión WaSender: {session_id}\\n"
            f"Motivo: {reason}\\n"
            + (f"Lead: {lead_id}\\n" if lead_id else "")
            + ("Envío incierto: no reenviar hasta verificar la entrega."
               if reason == "delivery_unconfirmed" else
               "El destinatario no está registrado en WhatsApp. Verificar número o usar llamada/correo; no reenviar por WhatsApp Web."
               if reason == "recipient_not_registered" else
               "No se pudo confirmar el envío por API desde el CRM. "
               "El ejecutivo puede utilizar WhatsApp Web cuando el sistema "
               "ofrezca esa alternativa de forma segura.")
        )
        sender = send or send_whatsapp_message_detailed
        provider_id = None
        try:
            result = await sender(recipient, message)
            provider_id = result.get("provider_message_id")
            delivery = (
                "accepted" if result.get("success") is True
                and result.get("provider_message_id")
                else "delivery_unknown" if result.get("provider_call_uncertain")
                or result.get("success") is True else "failed"
            )
        except asyncio.CancelledError:
            # Reservation survives cancellation: no duplicate on redeploy.
            raise
        except Exception as exc:
            logger.error(
                "EXEC_WA_PABLO_EVENT session=%s send_error=%s",
                session_id, type(exc).__name__,
            )
            delivery = "delivery_unknown"
        await asyncio.to_thread(
            collection.update_one,
            {"_id": alert_id, "alert_delivery": "reserved"},
            {"$set": {
                "alert_delivery": delivery,
                "alert_provider_message_id": provider_id,
                "alert_result_at": datetime.now(timezone.utc),
            }},
        )
        logger.info("EXEC_WA_PABLO_EVENT session=%s result=%s",
                    session_id, delivery)
        return delivery
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "EXEC_WA_PABLO_EVENT session=%s unexpected=%s",
            session_id, type(exc).__name__,
        )
        return "error"
