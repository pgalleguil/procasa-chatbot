"""Ledger formal de gestiones confirmadas de Captación."""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from datetime import date, datetime, time, timedelta, timezone

from bson import ObjectId

from captacion_workforce import (
    DEFAULT_TIMEZONE,
    MEMBERSHIP_COLLECTION,
    applicable_target,
    clean_id,
    compliance_status,
    localize,
)
from config import Config


ATTEMPT_COLLECTION = "captacion_action_attempts"
LEDGER_COLLECTION = "captacion_management_events"
ANOMALY_COLLECTION = "captacion_management_anomalies"
DAILY_METRICS_COLLECTION = "captacion_daily_metrics"
ASSIGNMENT_CYCLE_COLLECTION = "captacion_assignment_cycles"
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
VALID_CREDIT_EVENT_TYPES = {
    "management_confirmed",
    "manual_decision_confirmed",
    "capture_confirmed",
}

# Catálogo compartido por /captacion y sus reportes. Sus claves son los
# resultados técnicos que ya producen las reglas centrales de acreditación.
OUTCOME_GROUPS = {
    "pending_next_action": "Pendientes de nueva gestión",
    "management_in_progress": "Gestión en curso",
    "closed_without_capture": "Cerradas sin captación",
    "captured": "Captadas",
    "other_review": "Otros / Por revisar",
}
OUTCOME_COMMUNICATION_LABELS = {
    "no_respondio": "sin respuesta",
}
COMMERCIAL_OUTCOME_DEFINITIONS = {
    "ready_to_contact": ("por_contactar", "Por contactar", "pending_next_action", 10),
    "no_answer": ("no_respondio", "No respondió", "pending_next_action", 20),
    "busy": ("ocupado", "Ocupado", "pending_next_action", 20),
    "callback_requested": ("solicita_llamada_posterior", "Solicita llamada posterior", "pending_next_action", 30),
    "message_sent": ("mensaje_enviado", "Mensaje enviado", "management_in_progress", 40),
    "in_progress": ("en_gestion", "En gestión", "management_in_progress", 40),
    "contacted": ("contactado", "Contactado", "management_in_progress", 50),
    "broker_identified": ("corredor", "Corredor", "closed_without_capture", 70),
    "discarded": ("descartado", "Descartado", "closed_without_capture", 70),
    "unavailable": ("propiedad_no_disponible", "Propiedad no disponible", "closed_without_capture", 70),
    "listing_expired": ("publicacion_expirada", "Publicación expirada", "closed_without_capture", 70),
    "not_interested": ("no_interesado", "No interesado", "closed_without_capture", 70),
    "invalid_number": ("numero_invalido", "Número inválido", "closed_without_capture", 70),
    "captured": ("captado", "Captado", "captured", 100),
}

# Fuente Ãºnica de verdad para decisiones manuales que representan trabajo
# comercial real. El frontend no decide si una acciÃ³n acredita la meta.
MANUAL_DECISION_RULES = {
    "por contactar": {
        "result": "ready_to_contact",
        "once_per_assignment_cycle": True,
        "requires_transition": True,
    },
    "en gestion": {"result": "in_progress", "requires_transition": True},
    "contacto exitoso": {"result": "contacted", "contact_attempt": True, "contact_effective": True},
    "sin respuesta": {"result": "no_answer", "contact_attempt": True},
    "telefono invalido": {"result": "invalid_number", "contact_attempt": True},
    "reunion agendada": {
        "result": "callback_requested",
        "contact_attempt": True,
        "contact_effective": True,
    },
    "corredor": {"result": "broker_identified", "requires_evidence": True},
    "descartado": {"result": "discarded", "requires_evidence": True},
    "captado": {"result": "captured", "capture": True},
    "propiedad no disponible": {"result": "unavailable", "requires_evidence": True},
    "publicacion expirada": {"result": "listing_expired", "requires_evidence": True},
    "no interesado": {"result": "not_interested", "requires_evidence": True},
}

_INDEXES_READY = False


def ensure_management_indexes(db) -> None:
    global _INDEXES_READY
    if _INDEXES_READY:
        return
    db[ATTEMPT_COLLECTION].create_index("attempt_id", unique=True, name="captacion_attempt_id")
    db[ATTEMPT_COLLECTION].create_index(
        [("actor_user_id", 1), ("status", 1), ("initiated_at", -1)], name="captacion_attempt_actor_status"
    )
    ledger = db[LEDGER_COLLECTION]
    index_information = getattr(ledger, "index_information", None)
    existing_dedup = index_information().get("captacion_management_dedup") if index_information else None
    if existing_dedup and not existing_dedup.get("sparse"):
        # El índice v1 no permitía varias observaciones no acreditables sin dedup_key.
        ledger.drop_index("captacion_management_dedup")
    db[LEDGER_COLLECTION].create_index("event_id", unique=True, sparse=True, name="captacion_event_id")
    db[LEDGER_COLLECTION].create_index("source_event_id", unique=True, sparse=True, name="captacion_source_event")
    db[LEDGER_COLLECTION].create_index("dedup_key", unique=True, sparse=True, name="captacion_management_dedup")
    db[LEDGER_COLLECTION].create_index(
        [("actor_user_id", 1), ("local_date", 1), ("credited", 1)], name="captacion_ledger_actor_day"
    )
    db[ANOMALY_COLLECTION].create_index(
        [("actor_user_id", 1), ("local_date", 1), ("status", 1)], name="captacion_anomaly_review"
    )
    db[DAILY_METRICS_COLLECTION].create_index(
        [("user_id", 1), ("local_date", 1)], unique=True, name="captacion_daily_metric_user_day"
    )
    db[ASSIGNMENT_CYCLE_COLLECTION].create_index("assignment_cycle_id", unique=True, name="captacion_assignment_cycle")
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


def normalize_manual_status(status) -> str:
    value = unicodedata.normalize("NFKD", str(status or "").strip().casefold())
    return " ".join("".join(char for char in value if not unicodedata.combining(char)).split())


def evaluate_manual_decision(*, status, previous_status=None, notes=None, outcome=None, is_automatic=False) -> dict:
    """EvalÃºa una decisiÃ³n manual sin escribir ni acreditar eventos."""
    normalized_status = normalize_manual_status(status)
    rule = MANUAL_DECISION_RULES.get(normalized_status)
    evidence = str(notes or outcome or "").strip()
    changed = normalize_manual_status(previous_status) != normalized_status
    if is_automatic:
        return {"eligible": False, "reason": "automatic_change", "status": normalized_status}
    if not rule:
        return {"eligible": False, "reason": "status_not_creditable", "status": normalized_status}
    if rule.get("requires_transition") and not changed:
        return {"eligible": False, "reason": "real_transition_required", "status": normalized_status}
        return {"eligible": False, "reason": "real_transition_required", "status": normalized_status}
    if rule.get("requires_context") and not evidence:
        return {"eligible": False, "reason": "context_required", "status": normalized_status}
    if not changed:
        return {"eligible": False, "reason": "no_meaningful_change", "status": normalized_status}
    return {
        "eligible": True,
        "reason": None,
        "status": normalized_status,
        "result": rule["result"],
        "contact_attempt": bool(rule.get("contact_attempt")),
        "contact_effective": bool(rule.get("contact_effective")),
        "capture": bool(rule.get("capture")),
        "evidence": evidence,
    }


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


def new_assignment_cycle(*, property_id, user_id, assigned_at=None, assigned_by=None, reason="assignment") -> dict:
    assigned_at = assigned_at or datetime.now(timezone.utc)
    if assigned_at.tzinfo is None:
        assigned_at = assigned_at.replace(tzinfo=timezone.utc)
    return {
        "assignment_cycle_id": str(uuid.uuid4()),
        "property_id": clean_id(property_id),
        "user_id": clean_id(user_id),
        "assigned_at": assigned_at.astimezone(timezone.utc),
        "assigned_by": clean_id(assigned_by) or None,
        "reason": reason,
        "status": "active",
        "first_valid_action_at": None,
    }


def ensure_assignment_cycle(db, property_doc: dict) -> str:
    cycle_id = assignment_cycle_id(property_doc)
    gestion = property_doc.get("gestion") or {}
    cycle = {
        "assignment_cycle_id": cycle_id,
        "property_id": clean_id(property_doc.get("_id")),
        "user_id": clean_id(gestion.get("ejecutivo_id")),
        "assigned_at": gestion.get("fecha_asignacion"),
        "status": "active",
        "legacy_inferred": cycle_id.startswith("legacy-"),
        "updated_at": datetime.now(timezone.utc),
    }
    db[ASSIGNMENT_CYCLE_COLLECTION].update_one(
        {"assignment_cycle_id": cycle_id}, {"$setOnInsert": cycle}, upsert=True
    )
    if not gestion.get("assignment_cycle_id"):
        Config.get_captacion_collection(db).update_one(
            {"_id": property_doc["_id"], "gestion.assignment_cycle_id": {"$exists": False}},
            {"$set": {"gestion.assignment_cycle_id": cycle_id}},
        )
    return cycle_id


def management_dedup_key(property_id, actor_user_id, occurred_at, timezone_name=DEFAULT_TIMEZONE) -> str:
    local = localize(occurred_at, timezone_name)
    return f"{clean_id(property_id)}:{clean_id(actor_user_id)}:{local.date().isoformat()}"


def _write_credited_event_or_observation(db, event: dict) -> tuple[bool, str]:
    """Conserva todo evento válido y acredita solo el primero de su unidad diaria."""
    dedup_key = event["dedup_key"]
    write = db[LEDGER_COLLECTION].update_one(
        {"dedup_key": dedup_key}, {"$setOnInsert": event}, upsert=True
    )
    credited = bool(getattr(write, "upserted_id", None))
    if credited:
        return True, clean_id(event.get("event_id"))

    existing = db[LEDGER_COLLECTION].find_one({"dedup_key": dedup_key}, {"event_id": 1}) or {}
    observation = dict(event)
    observation.pop("dedup_key", None)
    observation["credited"] = False
    observation["credit_duplicate"] = True
    observation["duplicate_of_event_id"] = clean_id(existing.get("event_id")) or None
    observation["commercially_valid"] = True
    db[LEDGER_COLLECTION].insert_one(observation)
    return False, observation["duplicate_of_event_id"] or clean_id(observation.get("event_id"))


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
    cycle_id = ensure_assignment_cycle(db, property_doc)
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
        "contact_attempt": True,
        "notes": str(notes or ""),
        "contact_effective": result_value in CONTACT_EFFECTIVE_RESULTS,
        "occurred_at": occurred_at.astimezone(timezone.utc),
        "local_date": local.date().isoformat(),
        "timezone": DEFAULT_TIMEZONE,
        "source_event_id": attempt["attempt_id"],
        "source_system": "captacion_crm",
        "migration_version": LEDGER_VERSION,
        "legacy_inferred": False,
        "commercially_valid": True,
        "created_at": occurred_at.astimezone(timezone.utc),
    }
    credited, resolved_event_id = _write_credited_event_or_observation(db, event)
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
    if credited:
        _record_first_action_for_cycle(db, event)
        recalculate_daily_metric(db, actor_user_id, local.date(), now=occurred_at)
        audit_management_patterns(db, event)
    return {
        "status": "confirmed",
        "credited": credited,
        "event_id": resolved_event_id,
        "contact_effective": result_value in CONTACT_EFFECTIVE_RESULTS,
        "local_date": local.date().isoformat(),
    }


def record_manual_management_decision(
    db,
    *,
    property_doc: dict,
    actor_user: dict,
    status,
    previous_status=None,
    notes=None,
    outcome=None,
    is_automatic=False,
    now=None,
) -> dict:
    """Acredita una conclusiÃ³n comercial manual usando la misma deduplicaciÃ³n diaria."""
    decision = evaluate_manual_decision(
        status=status,
        previous_status=previous_status,
        notes=notes,
        outcome=outcome,
        is_automatic=is_automatic,
    )
    if not decision["eligible"]:
        return {"status": "not_credited", "credited": False, "reason": decision["reason"]}

    ensure_management_indexes(db)
    actor_user_id = clean_id(actor_user.get("_id"))
    property_id = clean_id(property_doc.get("_id"))
    if not actor_user_id or not property_id:
        raise ValueError("La gestiÃ³n requiere una propiedad y un user_id identificables")
    occurred_at = now or datetime.now(timezone.utc)
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    local = localize(occurred_at, DEFAULT_TIMEZONE)
    dedup_key = management_dedup_key(property_id, actor_user_id, occurred_at)
    event_id = str(uuid.uuid4())
    event_type = "capture_confirmed" if decision["capture"] else "manual_decision_confirmed"
    cycle_id = ensure_assignment_cycle(db, property_doc)
    rule = MANUAL_DECISION_RULES[decision["status"]]
    if rule.get("once_per_assignment_cycle") and db[LEDGER_COLLECTION].find_one({
        "property_id": property_id,
        "actor_user_id": actor_user_id,
        "assignment_cycle_id": cycle_id,
        "result": decision["result"],
        "event_type": "manual_decision_confirmed",
    }):
        db[LEDGER_COLLECTION].insert_one({
            "event_id": event_id,
            "event_type": "manual_decision_confirmed",
            "credited": False,
            "credit_duplicate": True,
            "commercially_valid": True,
            "property_id": property_id,
            "assignment_cycle_id": cycle_id,
            "actor_user_id": actor_user_id,
            "actor_name_snapshot": actor_user.get("nombre") or actor_user.get("username") or "",
            "actor_email_snapshot": actor_user.get("email") or "",
            "action": "manual_decision",
            "channel": "manual",
            "result": decision["result"],
            "status_snapshot": str(status or ""),
            "previous_status_snapshot": str(previous_status or ""),
            "notes": decision["evidence"],
            "contact_attempt": decision["contact_attempt"],
            "contact_effective": decision["contact_effective"],
            "occurred_at": occurred_at.astimezone(timezone.utc),
            "local_date": local.date().isoformat(),
            "timezone": DEFAULT_TIMEZONE,
            "source_event_id": f"manual:{event_id}",
            "source_system": "captacion_crm",
            "migration_version": LEDGER_VERSION,
            "legacy_inferred": False,
            "non_credit_reason": "assignment_cycle_decision_already_recorded",
            "created_at": occurred_at.astimezone(timezone.utc),
        })
        return {
            "status": "not_credited",
            "credited": False,
            "reason": "assignment_cycle_decision_already_recorded",
        }
    event = {
        "event_id": event_id,
        "event_type": event_type,
        "credited": True,
        "dedup_key": dedup_key,
        "property_id": property_id,
        "assignment_cycle_id": cycle_id,
        "actor_user_id": actor_user_id,
        "actor_name_snapshot": actor_user.get("nombre") or actor_user.get("username") or "",
        "actor_email_snapshot": actor_user.get("email") or "",
        "action": "manual_decision",
        "channel": "manual",
        "result": decision["result"],
        "status_snapshot": str(status or ""),
        "previous_status_snapshot": str(previous_status or ""),
        "notes": decision["evidence"],
        "contact_attempt": decision["contact_attempt"],
        "contact_effective": decision["contact_effective"],
        "occurred_at": occurred_at.astimezone(timezone.utc),
        "local_date": local.date().isoformat(),
        "timezone": DEFAULT_TIMEZONE,
        "source_event_id": f"manual:{event_id}",
        "source_system": "captacion_crm",
        "migration_version": LEDGER_VERSION,
        "legacy_inferred": False,
        "commercially_valid": True,
        "created_at": occurred_at.astimezone(timezone.utc),
    }
    credited, resolved_event_id = _write_credited_event_or_observation(db, event)
    if credited:
        _record_first_action_for_cycle(db, event)
        recalculate_daily_metric(db, actor_user_id, local.date(), now=occurred_at)
        audit_management_patterns(db, event)
    return {
        "status": "confirmed",
        "credited": credited,
        "event_id": resolved_event_id,
        "contact_effective": decision["contact_effective"],
        "capture": decision["capture"],
        "local_date": local.date().isoformat(),
    }


def _record_first_action_for_cycle(db, event: dict) -> None:
    cycle_id = event.get("assignment_cycle_id")
    occurred_at = event.get("occurred_at")
    db[ASSIGNMENT_CYCLE_COLLECTION].update_one(
        {"assignment_cycle_id": cycle_id, "$or": [{"first_valid_action_at": None}, {"first_valid_action_at": {"$exists": False}}]},
        {"$set": {"first_valid_action_at": occurred_at, "first_event_id": event.get("event_id")}},
    )
    property_id = event.get("property_id")
    candidates = [property_id]
    try:
        candidates.append(ObjectId(property_id))
    except Exception:
        pass
    Config.get_captacion_collection(db).update_one(
        {
            "_id": {"$in": candidates},
            "gestion.assignment_cycle_id": cycle_id,
            "$or": [{"gestion.first_valid_action_at": None}, {"gestion.first_valid_action_at": {"$exists": False}}],
        },
        {"$set": {"gestion.first_valid_action_at": occurred_at, "gestion.fecha_ultima_gestion": occurred_at}},
    )


def _membership_for_day(db, user_id, local_day: date) -> dict | None:
    day_value = local_day.isoformat()
    return db[MEMBERSHIP_COLLECTION].find_one(
        {
            "user_id": clean_id(user_id),
            "enabled": True,
            "start_date": {"$lte": day_value},
            "$or": [{"end_date": None}, {"end_date": {"$exists": False}}, {"end_date": {"$gte": day_value}}],
        }
    )


def _active_credited_events(db, user_id, local_day: date) -> list[dict]:
    rows = list(db[LEDGER_COLLECTION].find(
        {
            "actor_user_id": clean_id(user_id),
            "local_date": local_day.isoformat(),
            "event_type": {"$in": list(VALID_CREDIT_EVENT_TYPES)},
            "$or": [{"credited": True}, {"commercially_valid": True}],
        }
    ))
    credited_ids = {
        clean_id(row.get("event_id")) for row in rows
        if row.get("credited") and row.get("event_id")
    }
    reversed_ids = {
        row.get("original_event_id")
        for row in db[LEDGER_COLLECTION].find(
            {"event_type": "management_reversed", "original_event_id": {"$in": list(credited_ids)}},
            {"original_event_id": 1},
        )
    }
    active_credited_ids = credited_ids - {clean_id(value) for value in reversed_ids}
    return [
        row for row in rows
        if (
            row.get("credited") and clean_id(row.get("event_id")) in active_credited_ids
        ) or (
            row.get("commercially_valid")
            and clean_id(row.get("duplicate_of_event_id")) in active_credited_ids
        )
    ]


def summarize_management_metrics(events: list[dict]) -> dict:
    """Separa las cuatro métricas comerciales usando propiedades únicas."""
    managed = {row["property_id"] for row in events}
    attempts = {
        row["property_id"]
        for row in events
        if row.get("event_type") == "management_confirmed" or row.get("contact_attempt")
    }
    contacts = {row["property_id"] for row in events if row.get("contact_effective")}
    captures = {row["property_id"] for row in events if row.get("event_type") == "capture_confirmed"}
    return {
        "managed_properties": len(managed),
        "contact_attempts": len(attempts),
        "effective_contacts": len(contacts),
        "captures": len(captures),
    }


def _outcome_definition(row: dict) -> dict:
    result = clean_id(row.get("final_result") or row.get("result")).lower()
    definition = COMMERCIAL_OUTCOME_DEFINITIONS.get(result)
    if definition:
        detail, label, group, priority = definition
        if result == "message_sent" and clean_id(row.get("channel")).lower() == "email":
            detail, label = "correo_enviado", "Correo enviado"
        return {"result": result, "detail": detail, "label": label, "group": group, "priority": priority}
    return {
        "result": result or "unknown",
        "detail": result or "sin_resultado",
        "label": clean_id(row.get("result_label") or row.get("result") or "Sin resultado"),
        "group": "other_review",
        "priority": 0,
    }


def resolve_management_unit_outcomes(events: list[dict]) -> list[dict]:
    """Resuelve exactamente un resultado por propiedad/ejecutivo/día."""
    credited_units = set()
    candidates = {}
    for row in events:
        property_id = clean_id(row.get("property_id"))
        actor_user_id = clean_id(row.get("actor_user_id")) or clean_id(row.get("actor"))
        local_date = clean_id(row.get("local_date"))
        if not local_date and row.get("occurred_at"):
            local_date = localize(row.get("occurred_at"), DEFAULT_TIMEZONE).date().isoformat()
        if not property_id or not actor_user_id or not local_date:
            continue
        unit = (property_id, actor_user_id, local_date)
        if row.get("credited", True):
            credited_units.add(unit)
        if not row.get("commercially_valid", row.get("credited", True)):
            continue
        definition = _outcome_definition(row)
        occurred_at = row.get("occurred_at") or datetime.min.replace(tzinfo=timezone.utc)
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        # El resultado más concluyente prevalece; empates se resuelven por el
        # último evento manual válido y finalmente por event_id.
        order = (definition["priority"], occurred_at.astimezone(timezone.utc), clean_id(row.get("event_id")))
        if unit not in candidates or order > candidates[unit][0]:
            candidates[unit] = (order, definition)
    resolved = []
    for unit in sorted(credited_units):
        definition = (candidates.get(unit) or (None, _outcome_definition({})))[1]
        resolved.append({"property_id": unit[0], "actor_user_id": unit[1], "local_date": unit[2], **definition})
    return resolved


def summarize_grouped_outcomes(events: list[dict]) -> dict:
    units = resolve_management_unit_outcomes(events)
    groups = {key: {"label": label, "total": 0, "details": {}} for key, label in OUTCOME_GROUPS.items()}
    detailed = {}
    detail_labels = {}
    for detail, label, group_key, _priority in COMMERCIAL_OUTCOME_DEFINITIONS.values():
        groups[group_key]["details"].setdefault(detail, 0)
        detailed.setdefault(detail, 0)
        detail_labels[detail] = label
    # El CRM confirma email con message_sent + channel=email.
    groups["management_in_progress"]["details"].setdefault("correo_enviado", 0)
    detailed.setdefault("correo_enviado", 0)
    detail_labels["correo_enviado"] = "Correo enviado"
    for unit in units:
        group = groups[unit["group"]]
        group["total"] += 1
        group["details"][unit["detail"]] = group["details"].get(unit["detail"], 0) + 1
        detailed[unit["detail"]] = detailed.get(unit["detail"], 0) + 1
        detail_labels[unit["detail"]] = unit["label"]
    for group in groups.values():
        if sum(group["details"].values()) != group["total"]:
            raise ValueError("El detalle no coincide con su categoría de resultados")
    if sum(group["total"] for group in groups.values()) != len(units):
        raise ValueError("La suma agrupada no coincide con las propiedades gestionadas")
    return {
        "outcome_groups": groups,
        "detailed_outcomes": detailed,
        "detail_labels": detail_labels,
        "requires_outcome_review": groups["other_review"]["total"] > 0,
        "units": units,
    }


def summarize_final_outcomes(events: list[dict]) -> dict:
    """Clasifica una sola vez cada unidad propiedad/ejecutivo/día acreditada.

    Los eventos posteriores de la misma unidad siguen en el ledger como
    observaciones válidas y pueden definir su resultado final, pero nunca
    incrementan la cantidad de propiedades gestionadas.
    """
    return summarize_grouped_outcomes(events)["detailed_outcomes"]


def recalculate_daily_metric(db, user_id, local_day: date | str, now=None) -> dict:
    ensure_management_indexes(db)
    local_day = date.fromisoformat(local_day) if isinstance(local_day, str) else local_day
    membership = _membership_for_day(db, user_id, local_day)
    target_info = applicable_target(db, membership, local_day) if membership else {
        "target": 0, "exempt": True, "reason": "Sin membresía activa", "close_hour": 19
    }
    events = _active_credited_events(db, user_id, local_day)
    counts = summarize_management_metrics(events)
    timestamp = now or datetime.now(timezone.utc)
    status = compliance_status(
        count=counts["managed_properties"],
        target=target_info["target"],
        local_day=local_day,
        now=localize(timestamp, (membership or {}).get("timezone") or DEFAULT_TIMEZONE),
        close_hour=target_info.get("close_hour", 19),
        exempt=target_info.get("exempt", False),
    )
    metric = {
        "user_id": clean_id(user_id),
        "local_date": local_day.isoformat(),
        **counts,
        "target": target_info["target"],
        "compliance_status": status,
        "target_reason": target_info.get("reason"),
        "metric_version": LEDGER_VERSION,
        "recalculated_at": datetime.now(timezone.utc),
    }
    db[DAILY_METRICS_COLLECTION].update_one(
        {"user_id": metric["user_id"], "local_date": metric["local_date"]}, {"$set": metric}, upsert=True
    )
    return metric


def record_capture_event(db, *, property_doc: dict, actor_user: dict, now=None) -> dict:
    return record_manual_management_decision(
        db,
        property_doc=property_doc,
        actor_user=actor_user,
        status="Captado",
        previous_status=None,
        notes=None,
        now=now,
    )


def reverse_management_event(db, *, event_id, actor_user: dict, reason, now=None) -> dict:
    ensure_management_indexes(db)
    original = db[LEDGER_COLLECTION].find_one({"event_id": clean_id(event_id), "credited": True})
    if not original:
        raise LookupError("Evento acreditado no encontrado")
    if db[LEDGER_COLLECTION].find_one({"event_type": "management_reversed", "original_event_id": original["event_id"]}):
        raise ValueError("El evento ya fue reversado")
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("El motivo de reversa es obligatorio")
    occurred_at = now or datetime.now(timezone.utc)
    reversal = {
        "event_id": str(uuid.uuid4()),
        "event_type": "management_reversed",
        "credited": False,
        "original_event_id": original["event_id"],
        "property_id": original.get("property_id"),
        "actor_user_id": clean_id(actor_user.get("_id")),
        "actor_name_snapshot": actor_user.get("nombre") or "",
        "reason": reason,
        "previous_value": {"credited": True, "result": original.get("result")},
        "resulting_effect": {"credited": False},
        "occurred_at": occurred_at.astimezone(timezone.utc),
        "source_system": "captacion_admin",
        "migration_version": LEDGER_VERSION,
        "legacy_inferred": False,
    }
    db[LEDGER_COLLECTION].insert_one(reversal)
    recalculate_daily_metric(db, original["actor_user_id"], original["local_date"], now=occurred_at)
    return reversal


def audit_management_patterns(db, event: dict) -> list[dict]:
    """Genera alertas para supervisión; nunca bloquea la gestión."""
    actor_user_id = event["actor_user_id"]
    occurred_at = event["occurred_at"]
    local_date = event["local_date"]
    findings = []
    recent_count = db[LEDGER_COLLECTION].count_documents(
        {"actor_user_id": actor_user_id, "credited": True, "occurred_at": {"$gte": occurred_at - timedelta(minutes=5), "$lte": occurred_at}}
    )
    if recent_count >= 5:
        findings.append(("high_velocity", f"{recent_count} propiedades en 5 minutos"))
    repeated_count = db[LEDGER_COLLECTION].count_documents(
        {"actor_user_id": actor_user_id, "credited": True, "local_date": local_date, "result": event.get("result")}
    )
    if repeated_count >= 8:
        findings.append(("repetitive_result", f"{repeated_count} resultados {event.get('result')}"))
    for anomaly_type, detail in findings:
        signature = f"{actor_user_id}:{local_date}:{anomaly_type}:{event.get('event_id')}"
        db[ANOMALY_COLLECTION].update_one(
            {"signature": signature},
            {"$setOnInsert": {
                "signature": signature,
                "type": anomaly_type,
                "detail": detail,
                "actor_user_id": actor_user_id,
                "local_date": local_date,
                "event_id": event.get("event_id"),
                "status": "pending_review",
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    return [{"type": kind, "detail": detail} for kind, detail in findings]
