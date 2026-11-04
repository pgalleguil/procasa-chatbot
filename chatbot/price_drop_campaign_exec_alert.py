"""One-time, fail-closed WhatsApp notices to the canonical owner of a campaign lead.

Campaign classifications are not CRM temperatures, assignments or SLA events.
All provider sends are disabled unless explicitly opted in through the
separate PRICE_DROP_CAMPAIGN_EXEC_NOTIFY_ENABLED gate.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable
from .mongo_identity import mongo_id_variants
from .lead_router import build_secure_crm_url
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .crm_assignment_integrity import assignment_context
from .phone_utils import normalize_phone_strict, has_real_phone

COLLECTION = "crm_price_drop_campaign_exec_notices_v1"
GATE = "PRICE_DROP_CAMPAIGN_EXEC_NOTIFY_ENABLED"
ALLOWED_LABELS = frozenset({"INTERES_DECLARADO", "PRIORIDAD_COMERCIAL"})


def _active() -> bool:
    return (
        os.getenv(GATE, "").lower() in {"1", "true", "yes"}
        and os.getenv("PRICE_DROP_REAL_PILOT_CONTEXT_ENABLED", "").lower()
        in {"1", "true", "yes"}
    )


def enqueue_campaign_interest(
    db: Any, dispatch: dict, *, label: str, now: datetime | None = None,
) -> dict:
    """Durable notification intent; no provider send, even when active."""
    if label not in ALLOWED_LABELS or not dispatch.get("sent_at"):
        return {"status": "NOT_ACTIONABLE"}
    if dispatch.get("status") in {"OPTED_OUT", "SKIPPED_OPT_OUT"}:
        return {"status": "NOT_ACTIONABLE"}
    kind = "PRIORITY" if label == "PRIORIDAD_COMERCIAL" else "INTEREST"
    notice_id = f"{dispatch['_id']}:{kind}"
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("timezone_required")
    try:
        db[COLLECTION].insert_one({
            "_id": notice_id,
            "campaign_id": dispatch["campaign_id"],
            "dispatch_id": dispatch["_id"],
            "lead_id": dispatch["lead_id"],
            "property_code": dispatch["property_code"],
            "notice_kind": kind,
            "campaign_response_classification": label,
            "status": "PENDING",
            "created_at": stamp,
            "provider_message_id": None,
        })
    except DuplicateKeyError:
        return {"status": "ALREADY_QUEUED", "notice_id": notice_id}
    return {"status": "QUEUED", "notice_id": notice_id}


def _resolve_current_owner(db: Any, row: dict) -> dict:
    """Never infer owner from original campaign or property capture agent."""
    lead = db["leads"].find_one({"_id": row["lead_id"]})
    if not lead:
        return {"status": "lead_missing"}
    if str((lead.get("prospecto") or {}).get("codigo") or "") != str(row["property_code"]):
        return {"status": "lead_property_changed"}
    state = assignment_context(db, lead)
    if state.get("status") != "consistent":
        return {"status": "assignment_" + str(state.get("status") or "unknown")}
    cycle = state["cycle"]
    owner_id = str(cycle.get("assigned_to_user_id") or "")
    owner_name = str(cycle.get("assigned_to_display_name") or "")
    if not owner_id or not owner_name:
        return {"status": "owner_missing"}
    # The CURRENT active cycle is canonical after all redistributions.
    # Never fall back to the original card owner, a stale display name or
    # the first executive in the assignment history.
    variants = list(mongo_id_variants(owner_id))
    if not variants:
        return {"status": "owner_user_id_invalid"}
    user = db["usuarios"].find_one({
        "_id": {"$in": variants}, "is_active": {"$ne": False},
    })
    if not user:
        return {"status": "current_cycle_owner_not_found"}
    if str(user.get("nombre") or "").strip() != owner_name:
        return {"status": "current_cycle_owner_identity_conflict"}
    phone = normalize_phone_strict(
        user.get("telefono") or user.get("tel") or user.get("movil") or ""
    )
    if not phone or not has_real_phone(phone):
        return {"status": "owner_phone_invalid"}
    crm_url = build_secure_crm_url(lead)
    if not crm_url.startswith(("https://", "http://")) or "/crm/lead-id/" not in crm_url:
        return {"status": "secure_crm_link_unavailable"}
    return {
        "status": "ready", "name": owner_name, "phone": phone,
        "owner_user_id": owner_id,
        "cycle_id": str(cycle.get("assignment_cycle_id") or ""),
        "crm_url": crm_url,
    }


def _notice_text(row: dict, owner: dict, dispatch: dict) -> str:
    code = str(row["property_code"])
    label = str(row["campaign_response_classification"])
    intent = str(dispatch.get("last_turn_intent") or "")
    action_by_intent = {
        "ASK_VISIT": "solicitó coordinar una visita",
        "ASK_CONTACT": "solicitó contacto de un ejecutivo",
        "GIVE_OFFER": "quiere presentar una oferta",
    }
    action = (
        action_by_intent.get(intent, "solicitó atención comercial")
        if label == "PRIORIDAD_COMERCIAL"
        else "expresó interés por la rebaja"
    )
    old_price = dispatch.get("old_price_uf")
    new_price = dispatch.get("new_price_uf")
    if not isinstance(old_price, (float, int)) or not isinstance(new_price, (float, int)):
        raise ValueError("price_information_missing")
    format_price = lambda number: f"{number:,.0f}".replace(",", ".")
    return (
        "🔔 PROCASA — Respuesta a campaña de rebaja de precio\n\n"
        f"Propiedad: {code}\n"
        f"Rebaja informada: UF {format_price(old_price)} → UF {format_price(new_price)}\n"
        f"Estado campaña: {'Alta intención' if label == 'PRIORIDAD_COMERCIAL' else 'Interesado'}\n"
        f"Cliente: {dispatch.get('phone_normalized') or 'Consulta el CRM'}\n"
        f"Motivo: {action}.\n\n"
        f"{owner['name']}, revisa el lead existente en CRM y gestiona su interés.\n"
        f"\n🔗 *Abrir lead en CRM:*\n{owner['crm_url']}\n\n"
        "Es seguimiento de una campaña, NO un lead nuevo ni una reasignación."
    )


def process_one_exec_notice(
    db: Any, *, sender: Callable[[str, str], dict] | None = None,
    now: datetime | None = None,
) -> dict:
    """At-most-once provider attempt across workers; no automatic retry."""
    if not _active():
        return {"status": "DISABLED"}
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("timezone_required")
    pending = db[COLLECTION].find_one(
        {"status": "PENDING"}, sort=[("created_at", 1)]
    )
    if not pending:
        return {"status": "IDLE"}
    # Claim before any side effects. A crashed/unknown provider call remains
    # claimed for manual investigation; restarting Render must not resend.
    row = db[COLLECTION].find_one_and_update(
        {"_id": pending["_id"], "status": "PENDING"},
        {"$set": {"status": "CLAIMED", "attempt_started_at": stamp}},
        return_document=ReturnDocument.AFTER,
    )
    if not row:
        return {"status": "ALREADY_CLAIMED"}
    from .price_drop_campaign_real import DISPATCHES
    dispatch = db[DISPATCHES].find_one({"_id": row["dispatch_id"]})
    if not dispatch or not dispatch.get("sent_at"):
        reason = "dispatch_missing_or_unsent"
    elif dispatch.get("status") == "OPTED_OUT":
        reason = "client_opted_out"
    elif dispatch.get("campaign_response_classification") in {"BAJA", "RECHAZADO"}:
        reason = "interest_withdrawn"
    elif (row["notice_kind"] == "INTEREST"
          and dispatch.get("campaign_response_classification") == "PRIORIDAD_COMERCIAL"):
        reason = "superseded_by_priority"
    else:
        reason = None
    if reason:
        db[COLLECTION].update_one({"_id": row["_id"], "status": "CLAIMED"},
                                  {"$set": {"status": "SKIPPED", "reason": reason}})
        return {"status": "SKIPPED", "reason": reason}
    owner = _resolve_current_owner(db, row)
    if owner.get("status") != "ready":
        db[COLLECTION].update_one(
            {"_id": row["_id"], "status": "CLAIMED"},
            {"$set": {"status": "BLOCKED_ASSIGNMENT", "reason": owner["status"],
                      "blocked_at": stamp}})
        return {"status": "BLOCKED_ASSIGNMENT", "reason": owner["status"]}
    try:
        message = _notice_text(row, owner, dispatch)
    except (ValueError, KeyError) as exc:
        db[COLLECTION].update_one(
            {"_id": row["_id"]}, {"$set": {"status": "BLOCKED_DATA", "reason": str(exc)}})
        return {"status": "BLOCKED_DATA"}
    # Recheck ownership immediately before the provider call. A concurrent
    # SLA redistribution must not send to the executive who just lost the lead.
    fresh = _resolve_current_owner(db, row)
    if (
        fresh.get("status") != "ready"
        or fresh.get("owner_user_id") != owner.get("owner_user_id")
        or fresh.get("cycle_id") != owner.get("cycle_id")
        or fresh.get("phone") != owner.get("phone")
    ):
        db[COLLECTION].update_one(
            {"_id": row["_id"], "status": "CLAIMED"},
            {"$set": {
                "status": "BLOCKED_ASSIGNMENT",
                "reason": "assignment_changed_during_notification",
                "blocked_at": stamp,
            }},
        )
        return {"status": "BLOCKED_ASSIGNMENT", "reason": "assignment_changed_during_notification"}
    if sender is None:
        from .whatsapp_client import send_whatsapp_message_detailed_sync
        sender = send_whatsapp_message_detailed_sync
    try:
        receipt = sender(owner["phone"], message)
    except Exception:
        receipt = {"success": False, "provider_call_uncertain": True,
                   "delivery_status": "delivery_unknown"}
    accepted = bool(
        receipt and receipt.get("success") and receipt.get("provider_message_id")
        and not receipt.get("provider_call_uncertain")
    )
    status = "SENT" if accepted else "DELIVERY_UNKNOWN"
    db[COLLECTION].update_one({"_id": row["_id"], "status": "CLAIMED"}, {
        "$set": {
            "status": status,
            "target_owner_name": owner["name"],
            "target_owner_cycle_id": owner["cycle_id"],
            "target_owner_phone_last4": owner["phone"][-4:],
            "provider_message_id": receipt.get("provider_message_id"),
            "provider_delivery_status": receipt.get("delivery_status", "unknown"),
            "completed_at": stamp,
            **({"sent_at": stamp} if accepted else {}),
        },
    })
    return {
        "status": status, "campaign_id": row["campaign_id"],
        "lead_id": str(row["lead_id"]),
        "notification_kind": row["notice_kind"],
        "owner_name": owner["name"],
        "provider_message_id": receipt.get("provider_message_id"),
    }
