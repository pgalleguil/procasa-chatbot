"""Durable, domain-isolated reminders for captaci?n follow-ups."""
from __future__ import annotations

import asyncio
import uuid
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ReturnDocument

DOMAIN = "captacion_reminder"
MESSAGE_TYPE = "scheduled_reminder"
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
    # Target identity is persisted by the authenticated producer.  Name lookup
    # is a legacy fallback only when it is unique among active users.
    recipient_id = task.get("target_user_id") or task.get("recipient_user_id")
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
    url = build_followup_open_url(task, base_url=base_url) or f"{base_url}/captacion/{task['obj_id']}"
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
    try:
        content = reminder_text(task, captacion)
    except ValueError as exc:
        db[COLLECTION].update_one(
            {"_id": task["_id"], "status": "processing", "lease_token": token},
            {"$set": {"status": "failed_terminal", "error": str(exc), "updated_at": utc_now()},
             "$unset": {"lease_owner": "", "lease_token": "", "lease_until": ""},
             "$push": {"history": {"at": utc_now(), "state": "failed_terminal",
                                    "reason": "invalid_unicode_input"}}},
        )
        return {"status": "failed_terminal", "provider_message_id": None}
    result = await send_whatsapp_message_detailed(phone, content)
    now = utc_now()
    current_state = canonical_captacion_state(captacion)
    masked_phone = "****" + str(phone)[-4:]
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    update_filter = {"_id": task["_id"], "status": "processing", "lease_token": token}
    if result.get("success"):
        db[COLLECTION].update_one(update_filter, {
            "$set": {"status": "notified", "provider_called": True,
                     "provider_message_id": result.get("provider_message_id"),
                     "actually_delivered": True, "delivered_at": now, "sent_at": now,
                     "recipient_user_id": str(recipient["_id"]),
                     "target_user_id": str(recipient["_id"]),
                     "recipient_name": recipient.get("nombre"),
                     "recipient_phone_masked": masked_phone,
                     "state_at_delivery": current_state,
                     "audit_note_used": canonical_audit_note(task, captacion),
                     "message_content": content, "content_hash": content_hash,
                     "late_delivery_reason": task.get("late_delivery_reason"),
                     "updated_at": now},
            "$unset": {"lease_owner": "", "lease_token": "", "lease_until": "", "next_attempt_at": ""},
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
