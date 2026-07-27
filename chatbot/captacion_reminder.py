"""Durable, domain-isolated reminders for captaci?n follow-ups."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ReturnDocument

DOMAIN = "captacion_reminder"
MESSAGE_TYPE = "followup_reminder"
RECIPIENT_ROLE = "executive"
COLLECTION = "crm_tasks"

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _lease_filter(now: datetime) -> dict[str, Any]:
    return {"$or": [{"lease_until": {"$exists": False}}, {"lease_until": None}, {"lease_until": {"$lte": now}}]}

def claim_due_reminder(db, *, worker_id: str, now: datetime | None = None, task_id=None):
    now = now or utc_now()
    query: dict[str, Any] = {
        "message_domain": DOMAIN, "status": "pending",
        "execute_at": {"$lte": now}, **_lease_filter(now),
    }
    if task_id is not None:
        query["_id"] = task_id
    token = str(uuid.uuid4())
    return db[COLLECTION].find_one_and_update(
        query,
        {"$set": {"status": "processing", "lease_owner": worker_id,
                  "lease_token": token, "claimed_at": now,
                  "lease_until": now + timedelta(minutes=3),
                  "updated_at": now},
         "$inc": {"attempts": 1},
         "$push": {"history": {"at": now, "state": "processing", "reason": "atomic_claim"}}},
        sort=[("execute_at", 1)], return_document=ReturnDocument.AFTER,
    )

def resolve_recipient(db, task):
    recipient_id = task.get("recipient_user_id")
    if recipient_id:
        from bson import ObjectId
        try:
            query = {"_id": ObjectId(str(recipient_id))}
        except Exception:
            query = {"_id": recipient_id}
    else:
        query = {"nombre": task.get("recipient_name") or task.get("target_name")}
    user = db["usuarios"].find_one({**query, "is_active": {"$ne": False}})
    if not user:
        return None, None
    phone = user.get("telefono") or user.get("tel") or user.get("movil")
    return user, phone

def reminder_text(task, captacion):
    owner = (captacion.get("details") or {}).get("publicador") or "captaci?n"
    link = f"/captacion/{task['obj_id']}"
    return (f"? *Recordatorio de captaci?n*\n\n"
            f"Tienes seguimiento pendiente de *{owner}*.\n\n"
            f"?? *Nota:* {task.get('note') or 'Sin detalles'}\n"
            f"?? Gestionar: {link}")

async def deliver_claimed_reminder(db, task):
    """Deliver one already-claimed reminder. Never touches other domains."""
    token = task.get("lease_token")
    recipient, phone = resolve_recipient(db, task)
    if not recipient or not phone:
        db[COLLECTION].update_one(
            {"_id": task["_id"], "status": "processing", "lease_token": token},
            {"$set": {"status": "failed_terminal", "error": "active_recipient_phone_missing",
                      "updated_at": utc_now()},
             "$unset": {"lease_owner": "", "lease_token": "", "lease_until": ""},
             "$push": {"history": {"at": utc_now(), "state": "failed_terminal",
                                    "reason": "active_recipient_phone_missing"}}},
        )
        return {"status": "failed_terminal", "provider_message_id": None}
    from .whatsapp_client import send_whatsapp_message_detailed
    from config import Config
    from bson import ObjectId
    try:
        captacion = Config.get_captacion_collection(db).find_one({"_id": ObjectId(task["obj_id"])})
    except Exception:
        captacion = None
    if not captacion:
        db[COLLECTION].update_one(
            {"_id": task["_id"], "status": "processing", "lease_token": token},
            {"$set": {"status": "failed_terminal", "error": "captacion_not_found", "updated_at": utc_now()},
             "$unset": {"lease_owner": "", "lease_token": "", "lease_until": ""}},
        )
        return {"status": "failed_terminal", "provider_message_id": None}
    result = await send_whatsapp_message_detailed(phone, reminder_text(task, captacion))
    now = utc_now()
    update_filter = {"_id": task["_id"], "status": "processing", "lease_token": token}
    if result.get("success"):
        db[COLLECTION].update_one(update_filter, {
            "$set": {"status": "notified", "provider_called": True,
                     "provider_message_id": result.get("provider_message_id"),
                     "actually_delivered": True, "delivered_at": now,
                     "recipient_user_id": str(recipient["_id"]),
                     "recipient_name": recipient.get("nombre"),
                     "late_delivery_reason": "worker_skipped_unresolved_captacion_assignee",
                     "updated_at": now},
            "$unset": {"lease_owner": "", "lease_token": "", "lease_until": "", "next_attempt_at": ""},
            "$push": {"delivery_attempts": {"at": now, "accepted": True,
                                              "provider_message_id": result.get("provider_message_id")},
                      "history": {"at": now, "state": "notified", "reason": "provider_accepted"}},
        })
        return {"status": "notified", "provider_message_id": result.get("provider_message_id")}
    state = "delivery_unknown" if result.get("delivery_status") == "delivery_unknown" else "failed_retryable"
    db[COLLECTION].update_one(update_filter, {
        "$set": {"status": state, "provider_called": bool(result.get("provider_call_uncertain")),
                 "updated_at": now, "next_attempt_at": now + timedelta(minutes=5),
                 "last_error": result.get("delivery_status")},
        "$unset": {"lease_owner": "", "lease_token": "", "lease_until": ""},
        "$push": {"delivery_attempts": {"at": now, "accepted": False,
                                          "delivery_status": result.get("delivery_status")}},
    })
    return {"status": state, "provider_message_id": None}

async def process_one_due_reminder(db, *, worker_id: str, task_id=None):
    task = claim_due_reminder(db, worker_id=worker_id, task_id=task_id)
    if not task:
        return {"status": "idle", "provider_message_id": None}
    return await deliver_claimed_reminder(db, task)
