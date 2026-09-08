"""Durable, domain-isolated reminders for captaci?n follow-ups."""
from __future__ import annotations

import asyncio
import uuid
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ReturnDocument

DOMAIN = "captacion_reminder"
MESSAGE_TYPE = "scheduled_reminder"
RECIPIENT_ROLE = "executive"
COLLECTION = "crm_tasks"
PROCESSING_LEASE = timedelta(minutes=10)
RETRY_BASE_DELAY = timedelta(minutes=5)
RETRY_MAX_DELAY = timedelta(hours=1)
MAX_DELIVERY_ATTEMPTS = 3
RETRYABLE_STATES = ("failed_retryable", "delivery_unknown")
PROVIDER_TIMEOUT_SECONDS = 90
logger = logging.getLogger(__name__)

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _lease_filter(now: datetime) -> dict[str, Any]:
    return {
        "$or": [
            {"processing_lease_until": {"$lte": now}},
            {
                "processing_lease_until": {"$exists": False},
                "lease_until": {"$lte": now},
            },
            {
                "processing_lease_until": {"$exists": False},
                "lease_until": {"$exists": False},
            },
        ]
    }


def _retry_delay(attempts: int) -> timedelta:
    multiplier = max(0, min(int(attempts or 1) - 1, 6))
    return min(RETRY_BASE_DELAY * (2 ** multiplier), RETRY_MAX_DELAY)


def _claim_filter(now: datetime) -> dict[str, Any]:
    return {
        "message_domain": DOMAIN,
        "$or": [
            {
                "status": "pending",
                "execute_at": {"$lte": now},
                "$or": [
                    {"next_attempt_at": {"$exists": False}},
                    {"next_attempt_at": {"$lte": now}},
                ],
            },
            {
                "status": {"$in": list(RETRYABLE_STATES)},
                "execute_at": {"$lte": now},
                "next_attempt_at": {"$lte": now},
            },
            {
                "status": "processing",
                "execute_at": {"$lte": now},
                **_lease_filter(now),
            },
        ],
    }

def claim_due_reminder(db, *, worker_id: str, now: datetime | None = None, task_id=None):
    now = now or utc_now()
    query: dict[str, Any] = _claim_filter(now)
    if task_id is not None:
        query["_id"] = task_id
    token = str(uuid.uuid4())
    task = db[COLLECTION].find_one_and_update(
        query,
        {"$set": {"status": "processing", "lease_owner": worker_id,
                   "lease_token": token, "claimed_at": now,
                   "processing_started_at": now,
                   "processing_lease_until": now + PROCESSING_LEASE,
                   # Keep the old field during the compatibility window so
                   # an older process cannot mistake the new lease for none.
                   "lease_until": now + PROCESSING_LEASE,
                   "updated_at": now},
         "$inc": {"attempts": 1},
         "$push": {"history": {"at": now, "state": "processing", "reason": "atomic_claim"}}},
        sort=[("execute_at", 1)], return_document=ReturnDocument.AFTER,
    )
    if task and task.get("status") == "processing":
        history = task.get("history") or []
        was_recovered = any(
            isinstance(entry, dict) and entry.get("state") == "processing"
            and entry.get("reason") == "stale_lease_recovery"
            for entry in history[:-1]
        )
        if was_recovered:
            logger.warning(
                "[CAPTACION_REMINDER] stale_processing_recovered task_id=%s obj_id=%s",
                task.get("task_id"), task.get("obj_id"),
            )
    return task

def resolve_recipient(db, task):
    # Target identity is persisted by the authenticated producer.  Name lookup
    # is a legacy fallback only when it is unique among active users.
    recipient_id = task.get("recipient_user_id") or task.get("target_user_id")
    if recipient_id:
        from bson import ObjectId
        try:
            query = {"_id": ObjectId(str(recipient_id))}
        except Exception:
            query = {"_id": recipient_id}
        user = db["usuarios"].find_one({**query, "is_active": {"$ne": False}})
    else:
        name = task.get("recipient_name") or task.get("target_name")
        candidates = list(db["usuarios"].find({"nombre": name, "is_active": {"$ne": False}}).limit(2))
        user = candidates[0] if len(candidates) == 1 else None
    if not user:
        return None, None
    phone = user.get("telefono") or user.get("tel") or user.get("movil")
    return user, phone

def _format_scheduled(task):
    from .constants import CHILE_TZ
    value = task.get("scheduled_at") or task.get("execute_at")
    if not isinstance(value, datetime):
        return "S/I"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CHILE_TZ).strftime("%d/%m/%Y %H:%M")

def canonical_captacion_state(captacion):
    gestion = captacion.get("gestion") or {}
    # This is the state selected and saved from the captaci?n detail.
    return gestion.get("estado_captacion") or gestion.get("estado") or "S/I"

def has_degraded_unicode(*values):
    # A literal question mark in user-facing contact/state/note is unsafe here:
    # it is the observed corruption marker and must never be delivered.
    return any("?" in str(value or "") for value in values)

def _format_uf(value, operation):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    if str(operation or "").lower() == "venta":
        return f"{int(round(amount)):,}".replace(",", ".") + " UF"
    rounded = round(amount, 1)
    if rounded.is_integer():
        return f"{int(rounded):,}".replace(",", ".") + " UF"
    integer, decimal = f"{rounded:.1f}".split(".")
    return f"{int(integer):,}".replace(",", ".") + f",{decimal} UF"

def property_summary(captacion):
    code = captacion.get("codigo") or captacion.get("property_code")
    prop_type = captacion.get("tipo_propiedad")
    comuna = captacion.get("comuna")
    operation = str(captacion.get("operacion") or "").strip().lower()
    operation_label = {"venta": "Venta", "arriendo": "Arriendo"}.get(operation)
    price = _format_uf(captacion.get("precio_uf"), operation)
    parts = [str(v).strip() for v in (operation_label, code, prop_type.title() if prop_type else None, comuna, price) if v]
    return " \u00b7 ".join(parts) or None

def canonical_audit_note(task, captacion):
    requested = task.get("audit_note")
    if not requested:
        return None
    notes = ((captacion.get("gestion") or {}).get("notas") or [])
    for entry in reversed(notes):
        content = entry.get("content") if isinstance(entry, dict) else None
        if content == requested:
            return content
    return None

def reminder_text(task, captacion):
    # Unicode is kept as normal Python strings from source through provider.
    bell, person, house, pin, memo, clock, link, warning = (
        "\U0001f514", "\U0001f464", "\U0001f3e0", "\U0001f4cc",
        "\U0001f4dd", "\U0001f550", "\U0001f517", "\u26a0\ufe0f"
    )
    details = captacion.get("details") or {}
    gestion = captacion.get("gestion") or {}
    contact = task.get("contact_name") or details.get("publicador") or captacion.get("seller_name")
    property_ref = property_summary(captacion)
    current_state = canonical_captacion_state(captacion)
    note = canonical_audit_note(task, captacion)
    if task.get("audit_note") and note is None:
        raise ValueError("canonical_audit_note_missing")
    if has_degraded_unicode(contact, current_state, note):
        raise ValueError("invalid_unicode_input")
    from .followup_tracking import build_followup_open_url
    base_url = str(__import__("config").Config.CRM_BASE_URL).rstrip("/")
    # The worker passes its delivery timestamp so token issuance is explicit
    # and cannot occur while the task is merely being scheduled.
    url = build_followup_open_url(
        task, base_url=base_url, emitted_at=utc_now()
    ) or f"{base_url}/captacion/{task['obj_id']}"
    lines = [f"{bell} *RECORDATORIO DE CAPTACI\u00d3N*", ""]
    if contact:
        lines.append(f"{person} *Contacto:* {contact}")
    if property_ref:
        lines.append(f"{house} *Propiedad:* {property_ref}")
    lines.append(f"{pin} *Estado actual:* {current_state}")
    if note:
        lines.append(f"{memo} *Bit\u00e1cora:* {note}")
    lines.append(f"{clock} *Programada para:* {_format_scheduled(task)}")
    lines.extend(["", f"{link} *Abrir captaci\u00f3n:*", url, "",
                  f"{warning} Registra el resultado de la gesti\u00f3n en el m\u00f3dulo de captaciones."])
    return "\n".join(lines)

def _claimed_filter(task: dict[str, Any]) -> dict[str, Any]:
    return {"_id": task["_id"], "status": "processing", "lease_token": task.get("lease_token")}


def _clear_lease_update(*, clear_next_attempt: bool = False) -> dict[str, Any]:
    fields = {
        "lease_owner": "",
        "lease_token": "",
        "lease_until": "",
        "processing_lease_until": "",
    }
    if clear_next_attempt:
        fields["next_attempt_at"] = ""
    return fields


def _mark_terminal(db, task, *, error: str, reason: str, now: datetime | None = None):
    now = now or utc_now()
    db[COLLECTION].update_one(
        _claimed_filter(task),
        {
            "$set": {
                "status": "failed_terminal",
                "error": error,
                "resolution": "retry_exhausted" if reason == "max_attempts" else None,
                "updated_at": now,
            },
            "$unset": _clear_lease_update(clear_next_attempt=True),
            "$push": {"history": {"at": now, "state": "failed_terminal", "reason": reason}},
        },
    )
    logger.error(
        "[CAPTACION_REMINDER] reminder_delivery_failed task_id=%s obj_id=%s status=failed_terminal reason=%s",
        task.get("task_id"), task.get("obj_id"), reason,
    )
    return {"status": "failed_terminal", "provider_message_id": None}


def _mark_retryable_failure(
    db,
    task,
    *,
    error: str,
    state: str = "failed_retryable",
    provider_called: bool = False,
    provider_message_id: str | None = None,
    now: datetime | None = None,
):
    now = now or utc_now()
    attempts = int(task.get("attempts") or 0)
    if attempts >= MAX_DELIVERY_ATTEMPTS:
        return _mark_terminal(db, task, error=error, reason="max_attempts", now=now)
    next_attempt_at = now + _retry_delay(attempts)
    db[COLLECTION].update_one(
        _claimed_filter(task),
        {
            "$set": {
                "status": state,
                "provider_called": bool(provider_called),
                "last_error": error,
                "updated_at": now,
                "next_attempt_at": next_attempt_at,
            },
            "$unset": _clear_lease_update(),
            "$push": {
                "delivery_attempts": {
                    "at": now,
                    "accepted": False,
                    "delivery_status": error,
                    "provider_message_id": provider_message_id,
                },
                "history": {"at": now, "state": state, "reason": error},
            },
        },
    )
    logger.info(
        "[CAPTACION_REMINDER] retry_scheduled task_id=%s obj_id=%s state=%s attempt=%s next_attempt_at=%s",
        task.get("task_id"), task.get("obj_id"), state, attempts, next_attempt_at.isoformat(),
    )
    return {"status": state, "provider_message_id": provider_message_id}


async def deliver_claimed_reminder(db, task):
    """Deliver one claimed reminder with bounded retries and lease fencing."""
    attempts = int(task.get("attempts") or 0)
    if attempts > MAX_DELIVERY_ATTEMPTS:
        return _mark_terminal(db, task, error="max_delivery_attempts_exceeded", reason="max_attempts")

    recipient, phone = resolve_recipient(db, task)
    if not recipient or not phone:
        return _mark_terminal(
            db, task, error="active_recipient_phone_missing",
            reason="active_recipient_phone_missing",
        )

    from config import Config
    from bson import ObjectId

    obj_id = str(task.get("obj_id") or "").strip()
    if not obj_id:
        return _mark_terminal(db, task, error="captacion_id_missing", reason="task_entity_missing")
    try:
        captacion = Config.get_captacion_collection(db).find_one({"_id": ObjectId(obj_id)})
    except Exception:
        captacion = Config.get_captacion_collection(db).find_one({"_id": obj_id})
    if not captacion:
        return _mark_terminal(db, task, error="captacion_not_found", reason="captacion_not_found")

    try:
        # reminder_text generates and immediately verifies the URL. No provider
        # call is made if the dedicated secret or the token is invalid.
        content = reminder_text(task, captacion)
    except Exception as exc:
        from chatbot.followup_tracking import FollowupConfigurationError, FollowupTokenError

        if isinstance(exc, (FollowupConfigurationError, FollowupTokenError)):
            return _mark_retryable_failure(db, task, error=str(exc) or type(exc).__name__)
        return _mark_terminal(
            db, task, error=str(exc) or type(exc).__name__, reason="message_preflight_failed",
        )

    from .whatsapp_client import send_whatsapp_message_detailed

    try:
        result = await asyncio.wait_for(
            send_whatsapp_message_detailed(phone, content),
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[CAPTACION_REMINDER] reminder_delivery_failed task_id=%s obj_id=%s reason=provider_timeout",
            task.get("task_id"), obj_id,
        )
        result = {
            "success": False,
            "delivery_status": "delivery_unknown",
            "provider_call_uncertain": True,
        }
    except Exception as exc:
        logger.warning(
            "[CAPTACION_REMINDER] reminder_delivery_failed task_id=%s obj_id=%s reason=%s",
            task.get("task_id"), obj_id, type(exc).__name__,
        )
        result = {
            "success": False,
            "delivery_status": "provider_exception",
            "provider_call_uncertain": False,
        }

    now = utc_now()
    current_state = canonical_captacion_state(captacion)
    masked_phone = "****" + str(phone)[-4:]
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    update_filter = _claimed_filter(task)
    if result.get("success"):
        db[COLLECTION].update_one(update_filter, {
            "$set": {"status": "notified", "provider_called": True,
                     "provider_message_id": result.get("provider_message_id"),
                     "actually_delivered": True, "delivered_at": now, "sent_at": now,
                     "recipient_user_id": str(recipient.get("_id") or task.get("recipient_user_id") or ""),
                     "target_user_id": str(recipient.get("_id") or task.get("target_user_id") or ""),
                     "recipient_name": recipient.get("nombre"),
                     "recipient_phone_masked": masked_phone,
                     "state_at_delivery": current_state,
                     "audit_note_used": canonical_audit_note(task, captacion),
                     "message_content": content, "content_hash": content_hash,
                     "late_delivery_reason": task.get("late_delivery_reason"),
                     "updated_at": now},
             "$unset": _clear_lease_update(clear_next_attempt=True),
             "$push": {"delivery_attempts": {"at": now, "accepted": True,
                                               "provider_message_id": result.get("provider_message_id")},
                        "history": {"at": now, "state": "notified", "reason": "provider_accepted"}},
        })
        try:
            from .followup_tracking import record_followup_event
            record_followup_event(
                db, task=task, event_type="reminder_sent", occurred_at=now,
                source="whatsapp_provider",
                extra={"provider_message_id": result.get("provider_message_id")},
            )
        except ValueError:
            # Historical tasks intentionally remain legacy_unattributed.
            pass
        return {"status": "notified", "provider_message_id": result.get("provider_message_id")}

    state = "delivery_unknown" if result.get("delivery_status") == "delivery_unknown" else "failed_retryable"
    return _mark_retryable_failure(
        db,
        task,
        error=str(result.get("delivery_status") or "provider_rejected"),
        state=state,
        provider_called=bool(result.get("provider_call_uncertain")),
        provider_message_id=result.get("provider_message_id"),
        now=now,
    )


async def process_one_due_reminder(db, *, worker_id: str, task_id=None):
    task = claim_due_reminder(db, worker_id=worker_id, task_id=task_id)
    if not task:
        return {"status": "idle", "provider_message_id": None}
    return await deliver_claimed_reminder(db, task)
