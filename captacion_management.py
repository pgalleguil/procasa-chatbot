"""Ledger formal de gestiones confirmadas de Captación."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from captacion_workforce import DEFAULT_TIMEZONE, clean_id, localize


ATTEMPT_COLLECTION = "captacion_action_attempts"
LEDGER_COLLECTION = "captacion_management_events"
ANOMALY_COLLECTION = "captacion_management_anomalies"
LEDGER_VERSION = "ledger_v2"

VALID_STARTED_ACTIONS = {
    ("call", "tel"),
    ("message", "wa"),
    ("message", "whatsapp"),
    ("message", "email"),
}
VALID_RESULTS = {
    "no_answer",
    "busy",
    "invalid_number",
    "contacted",
    "callback_requested",
    "message_sent",
}
CONTACT_EFFECTIVE_RESULTS = {"contacted", "callback_requested"}
CANCEL_RESULT = "cancel"
ATTEMPT_TTL_HOURS = 24

_INDEXES_READY = False


def ensure_management_indexes(db) -> None:
    global _INDEXES_READY
    if _INDEXES_READY:
        return
    db[ATTEMPT_COLLECTION].create_index("attempt_id", unique=True, name="captacion_attempt_id")
    db[ATTEMPT_COLLECTION].create_index(
        [("actor_user_id", 1), ("status", 1), ("initiated_at", -1)], name="captacion_attempt_actor_status"
    )
    db[LEDGER_COLLECTION].create_index("event_id", unique=True, sparse=True, name="captacion_event_id")
    db[LEDGER_COLLECTION].create_index("dedup_key", unique=True, sparse=True, name="captacion_management_dedup")
    db[LEDGER_COLLECTION].create_index(
        [("actor_user_id", 1), ("local_date", 1), ("credited", 1)], name="captacion_ledger_actor_day"
    )
    db[ANOMALY_COLLECTION].create_index(
        [("actor_user_id", 1), ("local_date", 1), ("status", 1)], name="captacion_anomaly_review"
    )
    _INDEXES_READY = True


def normalize_started_action(action, channel) -> tuple[str, str]:
    action_value = str(action or "").strip().lower()
    channel_value = str(channel or "").strip().lower()
    if action_value == "call_initiated":
        action_value = "call"
    elif action_value == "message_sent":
        action_value = "message"
    if (action_value, channel_value) not in VALID_STARTED_ACTIONS:
        raise ValueError("Acción o canal no permitido")
    return action_value, channel_value


def normalize_result(result) -> str:
    value = str(result or "").strip().lower()
    aliases = {
        "sin respuesta": "no_answer",
        "no respondió": "no_answer",
        "ocupado": "busy",
        "número inválido": "invalid_number",
        "contactado": "contacted",
        "solicita llamada posterior": "callback_requested",
        "mensaje enviado": "message_sent",
        "cancelar": "cancel",
    }
    value = aliases.get(value, value)
    if value not in VALID_RESULTS | {CANCEL_RESULT}:
        raise ValueError("Resultado de gestión no permitido")
    return value


def assignment_cycle_id(property_doc: dict) -> str:
    gestion = property_doc.get("gestion") or {}
    current = clean_id(gestion.get("assignment_cycle_id"))
    if current:
        return current
    seed = "|".join(
        [
            clean_id(property_doc.get("_id")),
            clean_id(gestion.get("ejecutivo_id")),
            clean_id(gestion.get("fecha_asignacion")),
        ]
    )
    return "legacy-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def management_dedup_key(property_id, actor_user_id, occurred_at, timezone_name=DEFAULT_TIMEZONE) -> str:
    local = localize(occurred_at, timezone_name)
    return f"{clean_id(property_id)}:{clean_id(actor_user_id)}:{local.date().isoformat()}"


def start_management_attempt(
    db,
    *,
    property_doc: dict,
    actor_user: dict,
    action,
    channel,
    message=None,
    phone=None,
    template_used=None,
    now=None,
) -> dict:
    ensure_management_indexes(db)
    action_value, channel_value = normalize_started_action(action, channel)
    actor_user_id = clean_id(actor_user.get("_id"))
    if not actor_user_id:
        raise ValueError("El usuario no posee user_id")
    occurred_at = now or datetime.now(timezone.utc)
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    attempt_id = str(uuid.uuid4())
    cycle_id = assignment_cycle_id(property_doc)
    document = {
        "attempt_id": attempt_id,
        "status": "pending_confirmation",
        "property_id": clean_id(property_doc.get("_id")),
        "assignment_cycle_id": cycle_id,
        "actor_user_id": actor_user_id,
        "actor_name_snapshot": actor_user.get("nombre") or actor_user.get("username") or "",
        "actor_email_snapshot": actor_user.get("email") or "",
        "action": action_value,
        "channel": channel_value,
        "message_snapshot": str(message or ""),
        "phone_snapshot": str(phone or ""),
        "template_snapshot": str(template_used or ""),
        "initiated_at": occurred_at.astimezone(timezone.utc),
        "expires_at": occurred_at.astimezone(timezone.utc) + timedelta(hours=ATTEMPT_TTL_HOURS),
        "source_system": "captacion_crm",
    }
    db[ATTEMPT_COLLECTION].insert_one(document)
    return {"attempt_id": attempt_id, "status": document["status"], "assignment_cycle_id": cycle_id}


def confirm_management_attempt(db, *, attempt_id, actor_user: dict, result, notes=None, now=None) -> dict:
    ensure_management_indexes(db)
    result_value = normalize_result(result)
    actor_user_id = clean_id(actor_user.get("_id"))
    attempt = db[ATTEMPT_COLLECTION].find_one({"attempt_id": clean_id(attempt_id)})
    if not attempt:
        raise LookupError("Intento de gestión no encontrado")
    if clean_id(attempt.get("actor_user_id")) != actor_user_id:
        raise PermissionError("El intento pertenece a otro usuario")
    if attempt.get("status") != "pending_confirmation":
        raise ValueError("El intento ya fue resuelto")

    occurred_at = now or datetime.now(timezone.utc)
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    if occurred_at.astimezone(timezone.utc) > attempt["expires_at"]:
        db[ATTEMPT_COLLECTION].update_one(
            {"attempt_id": attempt["attempt_id"], "status": "pending_confirmation"},
            {"$set": {"status": "expired", "resolved_at": occurred_at.astimezone(timezone.utc)}},
        )
        raise ValueError("El intento expiró; inicia una nueva gestión")

    if result_value == CANCEL_RESULT:
        db[ATTEMPT_COLLECTION].update_one(
            {"attempt_id": attempt["attempt_id"], "status": "pending_confirmation"},
            {"$set": {"status": "cancelled", "result": result_value, "notes": str(notes or ""), "resolved_at": occurred_at.astimezone(timezone.utc)}},
        )
        return {"status": "cancelled", "credited": False, "contact_effective": False}

    local = localize(occurred_at, DEFAULT_TIMEZONE)
    dedup_key = management_dedup_key(attempt["property_id"], actor_user_id, occurred_at)
    event_id = str(uuid.uuid4())
    event = {
        "event_id": event_id,
        "event_type": "management_confirmed",
        "credited": True,
        "dedup_key": dedup_key,
        "property_id": attempt["property_id"],
        "assignment_cycle_id": attempt.get("assignment_cycle_id"),
        "actor_user_id": actor_user_id,
        "actor_name_snapshot": actor_user.get("nombre") or attempt.get("actor_name_snapshot") or "",
        "actor_email_snapshot": actor_user.get("email") or attempt.get("actor_email_snapshot") or "",
        "action": attempt["action"],
        "channel": attempt["channel"],
        "result": result_value,
        "notes": str(notes or ""),
        "contact_effective": result_value in CONTACT_EFFECTIVE_RESULTS,
        "occurred_at": occurred_at.astimezone(timezone.utc),
        "local_date": local.date().isoformat(),
        "timezone": DEFAULT_TIMEZONE,
        "source_event_id": attempt["attempt_id"],
        "source_system": "captacion_crm",
        "migration_version": LEDGER_VERSION,
        "legacy_inferred": False,
        "created_at": occurred_at.astimezone(timezone.utc),
    }
    write = db[LEDGER_COLLECTION].update_one(
        {"dedup_key": dedup_key}, {"$setOnInsert": event}, upsert=True
    )
    credited = bool(getattr(write, "upserted_id", None))
    existing = None if credited else db[LEDGER_COLLECTION].find_one({"dedup_key": dedup_key}, {"event_id": 1})
    resolved_event_id = event_id if credited else clean_id((existing or {}).get("event_id"))
    db[ATTEMPT_COLLECTION].update_one(
        {"attempt_id": attempt["attempt_id"], "status": "pending_confirmation"},
        {"$set": {
            "status": "confirmed",
            "result": result_value,
            "notes": str(notes or ""),
            "resolved_at": occurred_at.astimezone(timezone.utc),
            "credited": credited,
            "ledger_event_id": resolved_event_id,
        }},
    )
    return {
        "status": "confirmed",
        "credited": credited,
        "event_id": resolved_event_id,
        "contact_effective": result_value in CONTACT_EFFECTIVE_RESULTS,
        "local_date": local.date().isoformat(),
    }
