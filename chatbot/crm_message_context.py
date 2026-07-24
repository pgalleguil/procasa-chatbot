"""Canonical lead notification context builder.

Single source of truth for all WhatsApp message data.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from bson import ObjectId

logger = logging.getLogger(__name__)

CHILE_TZ = timezone(timedelta(hours=-4))

# ------------------------------- name validation -------------------------------

_INVALID_NAMES = frozenset({
    "", ".", "-", "None", "null", "Cliente", "Sin nombre", "Desconocido",
})


def _is_valid_client_name(name: str | None) -> bool:
    if not name:
        return False
    stripped = str(name).strip()
    return bool(stripped) and stripped not in _INVALID_NAMES


# ------------------------------- hot reason mapping -------------------------------

_HOT_INTENT_LABELS: Dict[str, str] = {
    "ASK_VISIT": "Solicit\u00F3 coordinar una visita",
    "ASK_CONTACT": "Solicit\u00F3 ser contactado",
    "ASK_INFO": "Solicit\u00F3 informaci\u00F3n de la propiedad",
}


def _resolve_hot_reason(lead: dict) -> str | None:
    intent = str(lead.get("last_intent") or "").strip().upper()
    if intent in _HOT_INTENT_LABELS:
        return _HOT_INTENT_LABELS[intent]
    return None


# ------------------------------- SLA calculation -------------------------------

def _compute_sla_elapsed_minutes(lead: dict, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    # Normalize to UTC-aware
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    assigned = None
    assigned_raw = lead.get("lifecycle", {}).get("assigned_at")
    if isinstance(assigned_raw, datetime):
        assigned = assigned_raw
    elif isinstance(assigned_raw, str):
        try:
            assigned = datetime.fromisoformat(assigned_raw)
        except Exception:
            pass
    if not assigned:
        created = lead.get("created_at")
        if isinstance(created, datetime):
            assigned = created
        elif isinstance(created, str):
            try:
                assigned = datetime.fromisoformat(created)
            except Exception:
                pass
    if assigned is not None:
        # Normalize assigned to UTC
        if isinstance(assigned, datetime):
            if assigned.tzinfo is None:
                assigned = assigned.replace(tzinfo=timezone.utc)
            else:
                assigned = assigned.astimezone(timezone.utc)
            elapsed = int((now - assigned).total_seconds() / 60)
            return max(elapsed, 0)
    return 0


def _format_sla_duration(minutes: int) -> str:
    if minutes < 1:
        return "menos de 1 minuto"
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    mins_remainder = minutes % 60
    days = hours // 24
    hours_remainder = hours % 24
    if days > 0:
        if hours_remainder > 0:
            return f"{days} d\u00EDa {hours_remainder} h"
        return f"{days} d\u00EDa"
    if mins_remainder > 0:
        return f"{hours} h {mins_remainder} min"
    return f"{hours} h"


# ------------------------------- context builder -------------------------------

def build_lead_notification_context(db, lead_id) -> dict:
    """Build a complete notification context dict from a lead _id (ObjectId or str).
    
    Returns a dict with all fields needed to construct WhatsApp messages.
    Missing/optional fields are set to None and omitted by templates.
    """
    from bson import ObjectId as BsonObjectId
    from bson.errors import InvalidId
    
    # Normalize lead_id to ObjectId if possible
    if not isinstance(lead_id, BsonObjectId):
        try:
            lead_id = BsonObjectId(lead_id)
        except (InvalidId, TypeError):
            pass  # Keep as-is for test mocks and legacy IDs

    lead = db["leads"].find_one({"_id": lead_id})
    if not lead:
        return {"_error": "lead_not_found", "lead_id": lead_id}

    prospect = lead.get("prospecto") or {}
    lifecycle = lead.get("lifecycle") or {}

    # --- property code ---
    property_code = (
        prospect.get("codigo")
        or lead.get("codigo")
        or lead.get("property_code")
        or None
    )

    # --- operation ---
    operacion = prospect.get("operacion") or lead.get("operacion") or None
    if not operacion and property_code:
        cartera = db["universo_cartera"].find_one({"codigo": property_code})
        if cartera:
            operacion = cartera.get("operacion") or None

    # --- property type ---
    tipo_propiedad = prospect.get("tipo") or lead.get("tipo") or None
    if not tipo_propiedad and property_code:
        cartera = db["universo_cartera"].find_one({"codigo": property_code}) if not (operacion and prospect.get("tipo")) else None
        if cartera:
            tipo_propiedad = cartera.get("tipo") or None
    if not tipo_propiedad and property_code:
        cartera = db["universo_cartera"].find_one({"codigo": property_code})
        if cartera:
            tipo_propiedad = cartera.get("tipo") or None

    # --- comuna ---
    comuna = prospect.get("comuna") or lead.get("comuna") or None
    if not comuna and property_code:
        cartera = db["universo_cartera"].find_one({"codigo": property_code})
        if cartera:
            comuna = cartera.get("comuna") or None

    # --- client name ---
    nombre_raw = prospect.get("nombre") or lead.get("nombre") or None
    nombre_cliente = str(nombre_raw).strip() if _is_valid_client_name(nombre_raw) else None

    # --- executive ---
    from .crm_delivery import resolve_executive_user, get_executive_phone
    cycle = db["crm_assignment_cycles"].find_one(
        {"lead_id": lead_id, "unassigned_at": None},
        sort=[("assigned_at", -1)],
    )
    assigned_to_user_id = str(cycle.get("assigned_to_user_id") or "") if cycle else ""
    assignment_cycle_id = cycle.get("assignment_cycle_id") if cycle else None
    exec_user = resolve_executive_user(db, assigned_to_user_id) if assigned_to_user_id else None
    exec_name = exec_user.get("nombre") if exec_user else None
    exec_phone = get_executive_phone(exec_user) if exec_user else None

    # --- temperature ---
    temperature = str(lead.get("lead_temperature_effective") or "").upper()

    # --- hot reason ---
    hot_reason = _resolve_hot_reason(lead) if temperature == "HOT" else None

    # --- SLA ---
    sla_minutes = _compute_sla_elapsed_minutes(lead)
    sla_display = _format_sla_duration(sla_minutes)

    # --- secure URL ---
    from .lead_router import build_secure_crm_url
    secure_url = build_secure_crm_url(lead, property_code)

    return {
        "lead_id": lead_id,
        "assignment_cycle_id": assignment_cycle_id,
        "assigned_to_user_id": assigned_to_user_id,
        "exec_name": exec_name,
        "exec_phone": exec_phone,
        "property_code": property_code,
        "operacion": operacion,
        "tipo_propiedad": tipo_propiedad,
        "comuna": comuna,
        "nombre_cliente": nombre_cliente,
        "temperature": temperature,
        "hot_reason": hot_reason,
        "sla_minutes": sla_minutes,
        "sla_display": sla_display,
        "secure_url": secure_url,
    }
