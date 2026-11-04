"""Small, explicitly approved price-drop WhatsApp campaign.

Three independent gates: prepared audience, human approval, runtime enablement.
No general marketing blast, no CRM assignment/SLA writes, no automatic retry.
Dedicated campaign/dispatch/turn collections provide auditable outcomes.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .phone_utils import has_real_phone, normalize_phone_strict, phone_identity_variants
from .property_lookup import PROPERTY_COLLECTION_NAME
from .price_drop_campaign_classifier import (
    PRIORITY, INTEREST, INFO, NO_SIGNAL, DECLINED, UNSUBSCRIBED,
    classify_price_drop_reply,
)

CAMPAIGNS = "crm_price_drop_campaigns_v1"
DISPATCHES = "crm_price_drop_campaign_dispatches_v1"
TURNS = "crm_price_drop_campaign_turns_v1"
FLAG = "PRICE_DROP_REAL_PILOT_ENABLED"
APPROVED_ID_FLAG = "PRICE_DROP_REAL_PILOT_APPROVED_ID"
CL_TZ = ZoneInfo("America/Santiago")
MAX_RECIPIENTS = 5
MIN_GAP_MINUTES = 10
STOP = re.compile(
    r"\b(?:stop|baja|no me escriban|dejen de escribir|no quiero (?:mas |más )?mensajes|"
    r"no me contacten|no contactarme|eliminar mi numero|eliminar mi número)\b",
    re.I,
)
VISIT = re.compile(r"\b(?:visitar|visita|agendar|verlo|verla|conocer el departamento)\b", re.I)
HUMAN = re.compile(r"\b(?:ejecutiv[oa]|corredor[ae]?|llamad[ao]|contact[oe]|asesor[ae]?)\b", re.I)
INTEREST = re.compile(r"\b(?:me interesa|interesad[oa]|quiero saber|cuentame|cuéntame|me gustaria|me gustaría)\b", re.I)
DECLINE = re.compile(r"\b(?:no me interesa|no estoy interesad[oa]|no quiero (?:visitar|visita|verla|verlo)|no me conviene|descarto la propiedad|no gracias)\b", re.I)

LEAD_PROJECTION = {
    "_id": 1, "phone": 1, "contact_phone_normalized": 1,
    "prospecto.codigo": 1, "created_at": 1, "messages": 1,
    "conversation_status": 1, "contact_identity_incomplete": 1,
    "phone_is_synthetic": 1, "price_drop_campaign_v1.opted_out": 1,
    "lead_temperature_effective": 1, "lead_temperature": 1,
    "lifecycle.hot_since": 1, "last_intent": 1,
}
PROP_PROJECTION = {
    "_id": 0, "codigo": 1, "disponible_prop360": 1,
    "estado.oficina": 1, "tipo_operacion.venta": 1,
    "tipo_operacion.precio_venta.precio_uf": 1,
}


class CampaignBlocked(ValueError):
    pass


def _utc(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CampaignBlocked("timestamp_invalid") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CampaignBlocked("timestamp_requires_timezone")
    return value.astimezone(timezone.utc)


def _mongo_utc(value: Any) -> datetime:
    """MongoDB BSON datetimes are UTC, but PyMongo returns them timezone-naive."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return _utc(value)


def _lead_id(raw: Any):
    value = str(raw or "").strip()
    if not value:
        raise CampaignBlocked("lead_id_required")
    if ObjectId.is_valid(value):
        return ObjectId(value)
    # Some older lead records use string IDs.
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9:_-]{3,79}", value):
        raise CampaignBlocked("lead_id_invalid")
    return value


def _campaign_id(raw: Any) -> str:
    value = str(raw or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{5,79}", value):
        raise CampaignBlocked("campaign_id_invalid")
    return value


def _property_code(raw: Any) -> str:
    value = str(raw or "").strip()
    if not re.fullmatch(r"\d{4,6}", value):
        raise CampaignBlocked("property_code_invalid")
    return value


def _preview_phone(phone: str) -> str:
    return "***" + str(phone)[-4:]


def _eligible_window(dt: datetime) -> bool:
    local = _utc(dt).astimezone(CL_TZ)
    # One daytime campaign at a time; Monday–Friday, 10:00–18:00 Chile.
    return local.weekday() < 5 and (10 <= local.hour < 18)


def _tracking_enabled() -> bool:
    """No audit collection query for ordinary leads when pilot is off."""
    return os.getenv("PRICE_DROP_REAL_PILOT_CONTEXT_ENABLED", "").casefold() in {"true", "1", "yes"}


def _is_enabled(campaign_id: str) -> bool:
    return (
        os.getenv(FLAG, "").casefold() in {"true", "1", "yes"}
        and os.getenv(APPROVED_ID_FLAG, "") == campaign_id
    )


def _property_valid(db: Any, code: str, expected_price: float) -> bool:
    prop = db[PROPERTY_COLLECTION_NAME].find_one({"codigo": code}, PROP_PROJECTION) or {}
    operation = prop.get("tipo_operacion") or {}
    price = (operation.get("precio_venta") or {}).get("precio_uf")
    return (
        str(prop.get("codigo") or "") == code
        and prop.get("disponible_prop360") is True
        and (prop.get("estado") or {}).get("oficina") == "PROCASA SUCRE"
        and operation.get("venta") is True
        and isinstance(price, (int, float)) and not isinstance(price, bool)
        and abs(float(price) - expected_price) < 0.01
    )


def _inquiry_evidence(lead: dict, code: str, drop_at: datetime) -> str | None:
    """A dated customer turn + matching single-property CRM reference.

    An old lead creation date alone does NOT prove a prior property inquiry.
    We require a customer message in the same lead before the reduction.
    """
    if str((lead.get("prospecto") or {}).get("codigo") or "") != code:
        return None
    for item in lead.get("messages") or []:
        if not isinstance(item, dict) or item.get("role") not in {"user", "customer"}:
            continue
        if not str(item.get("content") or "").strip():
            continue
        try:
            when = _utc(item.get("timestamp"))
        except CampaignBlocked:
            continue
        if when < drop_at:
            return when.isoformat()
    return None


def _check_recipient(
    db: Any, candidate: dict, code: str, drop_at: datetime, *,
    check_duplicates: bool = True,
) -> dict:
    lid = _lead_id(candidate.get("lead_id"))
    if candidate.get("consent_reviewed") is not True:
        raise CampaignBlocked("recipient_consent_review_missing")
    if not str(candidate.get("consent_basis") or "").strip():
        raise CampaignBlocked("recipient_consent_basis_missing")
    lead = db["leads"].find_one({"_id": lid}, LEAD_PROJECTION)
    if not lead:
        raise CampaignBlocked("lead_not_found")
    if lead.get("phone_is_synthetic") or lead.get("contact_identity_incomplete"):
        raise CampaignBlocked("lead_contact_identity_incomplete")
    if (lead.get("conversation_status") or "") in {
        "STOPPED_BY_CLIENT", "BLOCKED_EXTERNAL_BROKER", "STOPPED", "BLOCKED",
    } or (lead.get("price_drop_campaign_v1") or {}).get("opted_out") is True:
        raise CampaignBlocked("lead_opted_out_or_blocked")
    phone = normalize_phone_strict(
        lead.get("contact_phone_normalized") or lead.get("phone") or ""
    )
    if not phone or not has_real_phone(phone):
        raise CampaignBlocked("lead_phone_invalid")
    # Reject ambiguous phone-to-lead identity. Prevent contacting brokers.
    variants = phone_identity_variants(phone)
    duplicates = list(db["leads"].find({
        "$or": [
            {"phone": {"$in": variants}},
            {"contact_phone_normalized": {"$in": variants}},
        ],
    }, {"_id": 1}).limit(2))
    if len(duplicates) != 1 or str(duplicates[0]["_id"]) != str(lid):
        raise CampaignBlocked("lead_phone_ambiguous")
    inquiry_at = _inquiry_evidence(lead, code, drop_at)
    if not inquiry_at:
        raise CampaignBlocked("pre_drop_inquiry_not_verified")
    if check_duplicates and db[DISPATCHES].find_one({
        "$or": [
            {"lead_id": lid, "property_code": code},
            {"phone_normalized": phone, "property_code": code},
        ],
    }, {"_id": 1}):
        raise CampaignBlocked("property_campaign_already_recorded")
    if db[DISPATCHES].find_one({
        "phone_normalized": phone, "opted_out_at": {"$exists": True},
    }, {"_id": 1}):
        raise CampaignBlocked("phone_campaign_opted_out")
    crm_temperature = lead.get("lead_temperature_effective")
    if crm_temperature not in {"HOT", "COLD"}:
        crm_temperature = lead.get("lead_temperature")
    if crm_temperature not in {"HOT", "COLD"}:
        crm_temperature = None
    return {
        "lead_id": lid,
        "phone_normalized": phone,
        "inquiry_evidence_at": inquiry_at,
        "consent_basis": str(candidate["consent_basis"])[:180],
        # Snapshot comes from the official CRM but is never changed by
        # the campaign. None means unknown: never infer a new reactivation.
        "crm_temperature_snapshot": crm_temperature,
        "crm_was_hot_snapshot": crm_temperature == "HOT" if crm_temperature else None,
        "crm_hot_since_snapshot": (lead.get("lifecycle") or {}).get("hot_since"),
        "crm_intent_snapshot": lead.get("last_intent"),
    }


def _message(code: str, old: float, new: float) -> str:
    f = lambda n: f"{n:,.0f}".replace(",", ".")
    return (
        "Hola, ¿cómo estás? Soy de PROCASA 😊\n\n"
        f"Te escribo porque el departamento que consultaste (código {code}) "
        f"bajó de UF {f(old)} a UF {f(new)}.\n\n"
        "Con este nuevo valor quizás valga la pena darle otra mirada. "
        f"Acá está la publicación actualizada: https://www.procasa.cl/{code}\n\n"
        "¿Cómo lo ves con este nuevo precio?"
    )


def preview_plan(db: Any, payload: dict, *, now: datetime | None = None) -> dict:
    """Read-only. Report every rejection; no writes, no provider calls."""
    now = _utc(now or datetime.now(timezone.utc))
    cid = _campaign_id(payload.get("campaign_id"))
    code = _property_code(payload.get("property_code"))
    try:
        old, new = float(payload["old_price_uf"]), float(payload["new_price_uf"])
    except (ValueError, TypeError, KeyError) as exc:
        raise CampaignBlocked("price_invalid") from exc
    if not (2000 <= new <= 5000 and old > new and 0 < old - new < 5000):
        raise CampaignBlocked("discount_not_eligible")
    drop_at = _utc(payload.get("price_drop_at"))
    start_at = _utc(payload.get("start_at"))
    if start_at <= now:
        raise CampaignBlocked("schedule_must_be_future")
    if not now >= drop_at:
        raise CampaignBlocked("drop_in_future")
    if not str(payload.get("price_change_evidence") or "").strip():
        raise CampaignBlocked("audited_price_change_evidence_required")
    gap = payload.get("gap_minutes", 12)
    if type(gap) is not int or not MIN_GAP_MINUTES <= gap <= 120:
        raise CampaignBlocked("invalid_spacing")
    recipients = payload.get("recipients")
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= MAX_RECIPIENTS:
        raise CampaignBlocked("pilot_size_invalid")
    if not str(payload.get("reviewed_by") or "").strip():
        raise CampaignBlocked("operator_review_required")
    if not _property_valid(db, code, new):
        raise CampaignBlocked("current_property_status_or_price_invalid")
    if db[CAMPAIGNS].find_one({"_id": cid}, {"_id": 1}):
        raise CampaignBlocked("campaign_id_already_exists")
    approved = []
    blocked = []
    ids, phones = set(), set()
    for offset, recipient in enumerate(recipients):
        if not isinstance(recipient, dict):
            blocked.append({"slot": offset + 1, "reason": "recipient_invalid"})
            continue
        try:
            selected = _check_recipient(db, recipient, code, drop_at)
            if str(selected["lead_id"]) in ids or selected["phone_normalized"] in phones:
                raise CampaignBlocked("duplicate_recipient_in_batch")
            ids.add(str(selected["lead_id"]))
            phones.add(selected["phone_normalized"])
            scheduled = start_at + timedelta(minutes=gap * offset)
            if not _eligible_window(scheduled):
                raise CampaignBlocked("slot_outside_chilean_business_hours")
            approved.append({
                "slot": offset + 1,
                "lead_id": str(selected["lead_id"]),
                "phone_last4": _preview_phone(selected["phone_normalized"]),
                "inquiry_evidence_at": selected["inquiry_evidence_at"],
                "scheduled_at": scheduled.isoformat(),
                "_recipient": selected,
            })
        except CampaignBlocked as exc:
            blocked.append({
                "slot": offset + 1,
                "lead_id": str(recipient.get("lead_id") or "")[-6:],
                "reason": str(exc),
            })
    return {
        "campaign_id": cid,
        "property_code": code,
        "old_price_uf": old,
        "new_price_uf": new,
        "price_drop_at": drop_at.isoformat(),
        "price_change_evidence": str(payload["price_change_evidence"])[:200],
        "start_at": start_at.isoformat(),
        "gap_minutes": gap,
        "reviewed_by": str(payload["reviewed_by"])[:80],
        "approved": approved,
        "blocked": blocked,
        "ready_to_plan": len(approved) == len(recipients) and not blocked,
        "preview_message": _message(code, old, new),
    }


def persist_plan(db: Any, payload: dict, *, now: datetime | None = None) -> dict:
    """Create immutable, individually approved dispatch slots; never send."""
    now = _utc(now or datetime.now(timezone.utc))
    plan = preview_plan(db, payload, now=now)
    if not plan["ready_to_plan"]:
        raise CampaignBlocked("audience_contains_blocked_recipient")
    cid = plan["campaign_id"]
    docs = []
    for row in plan["approved"]:
        rec = row["_recipient"]
        docs.append({
            "_id": f"{cid}:{plan['property_code']}:{rec['lead_id']}",
            "campaign_id": cid,
            "lead_id": rec["lead_id"],
            "phone_normalized": rec["phone_normalized"],
            "property_code": plan["property_code"],
            "old_price_uf": plan["old_price_uf"],
            "new_price_uf": plan["new_price_uf"],
            "inquiry_evidence_at": row["inquiry_evidence_at"],
            "consent_basis": rec["consent_basis"],
            "crm_temperature_at_selection": rec["crm_temperature_snapshot"],
            "crm_was_hot_at_selection": rec["crm_was_hot_snapshot"],
            "crm_hot_since_at_selection": rec["crm_hot_since_snapshot"],
            "scheduled_at": _mongo_utc(row["scheduled_at"]),
            "created_at": now,
            "status": "PLANNED",
            "reply_count": 0,
            "bot_reply_count": 0,
            "visit_requested": False,
            "human_requested": False,
            "interest_signaled": False,
            "offer_intent": False,
            "campaign_response_classification": "SIN_RESPUESTA",
            "campaign_response_priority": None,
            "last_response_intent": None,
            "campaign_origin": "PRICE_DROP",
            "campaign_attribution_basis": "same_phone_after_accepted_send_window",
            "positive_reply_count": 0,
            "followup_status": "SIN_RESPUESTA",
            "needs_human_review": False,
        })
    header = {
        "_id": cid, "status": "PLANNED",
        "property_code": plan["property_code"],
        "old_price_uf": plan["old_price_uf"],
        "new_price_uf": plan["new_price_uf"],
        "price_drop_at": _utc(plan["price_drop_at"]),
        "price_change_evidence": plan["price_change_evidence"],
        "start_at": _utc(plan["start_at"]),
        "gap_minutes": plan["gap_minutes"],
        "max_attempts": len(docs), "attempts_started": 0,
        "next_allowed_at": _utc(plan["start_at"]),
        "reviewed_by": plan["reviewed_by"],
        "created_at": now,
        "approved_at": None,
    }
    # Header is reserved first; duplicate campaign fails closed even if partial.
    db[CAMPAIGNS].insert_one(header)
    try:
        db[DISPATCHES].insert_many(docs, ordered=True)
    except Exception:
        db[CAMPAIGNS].update_one({"_id": cid}, {"$set": {
            "status": "PREPARATION_INCOMPLETE", "halted_at": now,
        }})
        raise
    return {
        "status": "PLANNED", "campaign_id": cid,
        "recipients": len(docs), "first_slot": plan["start_at"],
        "gap_minutes": plan["gap_minutes"], "sent": 0,
    }


def approve_campaign(db: Any, campaign_id: str, *, operator: str, now=None) -> dict:
    """Separate human approval; cannot self-approve by preparing the plan."""
    cid = _campaign_id(campaign_id)
    if not operator or not str(operator).strip():
        raise CampaignBlocked("approver_required")
    header = db[CAMPAIGNS].find_one({"_id": cid, "status": "PLANNED"})
    if not header:
        raise CampaignBlocked("campaign_not_in_planned_state")
    count = db[DISPATCHES].count_documents({"campaign_id": cid, "status": "PLANNED"})
    if count != header["max_attempts"]:
        raise CampaignBlocked("incomplete_audience_or_ledger")
    if not 1 <= count <= MAX_RECIPIENTS:
        raise CampaignBlocked("size_limit_broken")
    now = _utc(now or datetime.now(timezone.utc))
    if _mongo_utc(header["start_at"]) <= now:
        raise CampaignBlocked("campaign_schedule_expired")
    result = db[CAMPAIGNS].update_one(
        {"_id": cid, "status": "PLANNED"},
        {"$set": {
            "status": "APPROVED",
            "approved_at": now,
            "approved_by": str(operator)[:80],
        }},
    )
    if result.modified_count != 1:
        raise CampaignBlocked("approval_concurrent_change")
    return {"status": "APPROVED", "campaign_id": cid, "recipients": count}


def halt_campaign(db: Any, campaign_id: str, *, operator: str, now=None) -> dict:
    now = _utc(now or datetime.now(timezone.utc))
    cid = _campaign_id(campaign_id)
    result = db[CAMPAIGNS].update_one(
        {"_id": cid, "status": {"$in": ["APPROVED", "PLANNED"]}},
        {"$set": {"status": "HALTED", "halted_at": now,
                  "halted_by": str(operator or "operator")[:80]}},
    )
    return {"status": "HALTED" if result.modified_count else "NOT_ACTIVE",
            "campaign_id": cid}


def _update_lead_marker(db: Any, row: dict, *, status: str, now: datetime) -> None:
    """Campaign namespace only; do not mutate routing, stage or SLA."""
    db["leads"].update_one({"_id": row["lead_id"]}, {
        "$set": {
            "price_drop_campaign_v1.last_campaign_id": row["campaign_id"],
            "price_drop_campaign_v1.last_property_code": row["property_code"],
            "price_drop_campaign_v1.last_status": status,
            "price_drop_campaign_v1.last_sent_at": now,
            "price_drop_campaign_v1.has_received": status == "SENT",
        },
        "$addToSet": {
            "price_drop_campaign_v1.campaign_ids": row["campaign_id"],
        },
    })


def send_one_due(
    db: Any, campaign_id: str, *,
    sender: Callable[[str, str], dict] | None = None,
    now: datetime | None = None,
) -> dict:
    """One due window/one recipient per call, exactly-once attempt semantics.

    A startup/restarted worker can never automatically send: runtime flag AND
    approved campaign ID AND campaign approval AND per-slot time must match.
    """
    cid = _campaign_id(campaign_id)
    if not _is_enabled(cid):
        return {"status": "DISABLED"}
    now = _utc(now or datetime.now(timezone.utc))
    if not _eligible_window(now):
        return {"status": "OUTSIDE_ALLOWED_HOURS"}
    header = db[CAMPAIGNS].find_one({"_id": cid, "status": "APPROVED"})
    if not header:
        return {"status": "NOT_APPROVED_OR_HALTED"}
    if _mongo_utc(header["next_allowed_at"]) > now:
        return {"status": "WINDOW_NOT_OPEN"}
    row = db[DISPATCHES].find_one({
        "campaign_id": cid, "status": "PLANNED",
        "scheduled_at": {"$lte": now},
    }, sort=[("scheduled_at", 1)])
    if not row:
        return {"status": "NO_DUE_RECIPIENT"}
    # Late jobs are held for manual review instead of sending on another day.
    if now - _mongo_utc(row["scheduled_at"]) > timedelta(minutes=45):
        db[DISPATCHES].update_one({"_id": row["_id"], "status": "PLANNED"}, {
            "$set": {"status": "EXPIRED_WINDOW", "skipped_at": now}})
        return {"status": "EXPIRED_WINDOW"}
    if not _property_valid(db, row["property_code"], float(row["new_price_uf"])):
        db[DISPATCHES].update_one({"_id": row["_id"], "status": "PLANNED"}, {
            "$set": {"status": "SKIPPED_PRICE_CHANGE", "skipped_at": now}})
        return {"status": "SKIPPED_PRICE_CHANGE"}
    try:
        recipient = _check_recipient(
            db, {"lead_id": row["lead_id"], "consent_reviewed": True,
                 "consent_basis": row["consent_basis"]},
            row["property_code"], _mongo_utc(header["price_drop_at"]),
            check_duplicates=False,
        )
        if recipient["phone_normalized"] != row["phone_normalized"]:
            raise CampaignBlocked("lead_phone_changed")
    except CampaignBlocked as exc:
        db[DISPATCHES].update_one({"_id": row["_id"], "status": "PLANNED"}, {
            "$set": {"status": "SKIPPED_ELIGIBILITY",
                     "skip_reason": str(exc), "skipped_at": now}})
        return {"status": "SKIPPED_ELIGIBILITY", "reason": str(exc)}
    # Snapshot the *current* official classification immediately before the
    # provider attempt, independent of what the same lead was at selection.
    # This is an audit baseline only: never mutate CRM temperature/SLA.
    crm_at_send = recipient["crm_temperature_snapshot"]
    was_hot_at_send = recipient["crm_was_hot_snapshot"]
    # CAS on campaign header is the cross-worker pace/size gate.
    claimed_slot = db[CAMPAIGNS].find_one_and_update(
        {"_id": cid, "status": "APPROVED",
         "next_allowed_at": {"$lte": now},
         "attempts_started": {"$lt": header["max_attempts"]}},
        {"$inc": {"attempts_started": 1},
         "$set": {"next_allowed_at": now + timedelta(minutes=header["gap_minutes"]),
                  "last_attempt_started_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not claimed_slot:
        return {"status": "WINDOW_LOCKED"}
    claimed = db[DISPATCHES].find_one_and_update(
        {"_id": row["_id"], "status": "PLANNED"},
        {"$set": {"status": "SENDING", "attempted_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not claimed:
        return {"status": "ALREADY_CLAIMED"}
    # A real provider call must be timestamped when accepted, not when a
    # window first opens. Fake senders injected by tests keep the test clock.
    live_provider = sender is None
    if sender is None:
        from .whatsapp_client import send_whatsapp_message_detailed_sync
        sender = send_whatsapp_message_detailed_sync
    message = _message(
        claimed["property_code"], float(claimed["old_price_uf"]),
        float(claimed["new_price_uf"]),
    )
    try:
        receipt = sender(claimed["phone_normalized"], message)
    except Exception:
        receipt = {"success": False, "provider_call_uncertain": True,
                   "delivery_status": "delivery_unknown"}
    accepted = bool(
        receipt.get("success") and receipt.get("provider_message_id")
        and not receipt.get("provider_call_uncertain")
    )
    # For live sends use the instant the provider returns acceptance.
    # UTC BSON and a local Chile timestamp are both durable in the ledger.
    accepted_at = datetime.now(timezone.utc) if live_provider else now
    status = "SENT" if accepted else "DELIVERY_UNKNOWN"
    patch = {
        "status": status, "provider_delivery_status": str(
            receipt.get("delivery_status") or "unknown"
        )[:50], "http_status": receipt.get("http_status"),
        "outbound_message": message[:1600],
        "provider_message_id": receipt.get("provider_message_id"),
        "completed_at": now,
        "crm_temperature_before_send": crm_at_send,
        "crm_was_hot_before_send": was_hot_at_send,
        "crm_hot_since_before_send": recipient["crm_hot_since_snapshot"],
        "crm_intent_before_send": recipient["crm_intent_snapshot"],
    }
    if accepted:
        patch.update({
            "sent_at": accepted_at,
            "sent_at_chile": accepted_at.astimezone(CL_TZ).isoformat(),
            "expires_at": accepted_at + timedelta(days=7),
            "campaign_response_classification": "SIN_RESPUESTA",
            "campaign_response_priority": None,
            "followup_status": "SIN_RESPUESTA",
        })
    db[DISPATCHES].update_one({"_id": claimed["_id"], "status": "SENDING"},
                              {"$set": patch})
    if accepted:
        try:
            _update_lead_marker(db, claimed, status="SENT", now=accepted_at)
        except Exception:
            # Ledger/provider acceptance remains durable. Never retry on a
            # post-send CRM marker failure.
            db[DISPATCHES].update_one({"_id": claimed["_id"]}, {"$set": {
                "lead_marker_sync_pending": True,
            }})
    return {
        "status": status,
        "campaign_id": cid, "property_code": claimed["property_code"],
        "lead_id": str(claimed["lead_id"]),
        "phone_last4": _preview_phone(claimed["phone_normalized"]),
        "provider_message_id": receipt.get("provider_message_id"),
        "sent_at": accepted_at.isoformat() if accepted else None,
        "sent_at_chile": accepted_at.astimezone(CL_TZ).isoformat() if accepted else None,
    }


def _active_dispatch(db: Any, phone: str, now: datetime, *, needs_reply=False):
    normalized = normalize_phone_strict(phone)
    if not normalized:
        return None
    query = {
        "phone_normalized": normalized,
        "status": {"$in": ["SENT", "RESPONDED", "OPTED_OUT"]},
        "sent_at": {"$lte": now},
        "expires_at": {"$gt": now},
    }
    if needs_reply:
        query["reply_count"] = {"$gt": 0}
    return db[DISPATCHES].find_one(query, sort=[("sent_at", -1)])



def record_inbound(
    db: Any, *, phone: str, provider_message_id: str,
    text: str, received_at: datetime | None = None,
) -> dict:
    """Classify each client turn and persist the campaign outcome.

    The canonical CRM pipeline still owns lead_temperature_effective, hot_since,
    assignment and SLA. This separate campaign label is actionable for
    supervisors but never constitutes a human management event.
    """
    if not _tracking_enabled():
        return {"status": "DISABLED"}
    now = _utc(received_at or datetime.now(timezone.utc))
    row = _active_dispatch(db, phone, now)
    if not row or not provider_message_id or not text:
        return {"status": "NOT_CAMPAIGN_REPLY"}
    if row.get("status") == "OPTED_OUT":
        return {"status": "ALREADY_OPTED_OUT"}

    # Do not attribute an explicit inquiry for a different listing to this
    # campaign. The normal CRM/chatbot still processes it independently.
    other_code = re.search(
        r"\b(?:cod(?:igo)?|propiedad)\s*[:#]?\s*(\d{4,6})\b",
        str(text or ""), re.I,
    )
    if other_code and other_code.group(1) != str(row["property_code"]):
        return {"status": "OTHER_PROPERTY_NOT_ATTRIBUTED"}

    classification = classify_price_drop_reply(text)
    event = classification.as_dict()
    key = hashlib.sha256(
        f"{row['_id']}:inbound:{provider_message_id}".encode()
    ).hexdigest()
    try:
        db[TURNS].insert_one({
            "_id": key, "campaign_id": row["campaign_id"],
            "dispatch_id": row["_id"], "lead_id": row["lead_id"],
            "property_code": row["property_code"],
            "direction": "inbound",
            "provider_message_id": str(provider_message_id),
            "text": str(text)[:1600], "created_at": now,
            "created_at_chile": now.astimezone(CL_TZ).isoformat(),
            "classification": event,
        })
    except DuplicateKeyError:
        return {"status": "DUPLICATE_IGNORED"}

    stopped = event["label"] == "BAJA"
    declined = event["label"] == "RECHAZADO"
    previous_label = str(row.get("response_classification") or "")
    previous_pending = bool(row.get("needs_human_review"))
    if stopped:
        overall_label, overall_temp, followup, pending = (
            "BAJA", None, "BAJA", False,
        )
    elif declined:
        overall_label, overall_temp, followup, pending = (
            "RECHAZADO", None, "RECHAZADO", False,
        )
    elif event["label"] == "HOT" or previous_label == "HOT":
        overall_label, overall_temp, followup, pending = (
            "HOT", "HOT", "PENDIENTE_REVISION", True,
        )
    elif event["label"] == "NORMAL" or previous_label == "NORMAL":
        overall_label, overall_temp = "NORMAL", "NORMAL"
        pending = previous_pending or bool(event["positive"])
        followup = "PENDIENTE_REVISION" if pending else "CONSULTA_INFORMATIVA"
    else:
        overall_label, overall_temp, followup, pending = (
            "SIN_SENAL", None, "SIN_SENAL", False,
        )

    fields = {
        "last_customer_reply_at": now,
        "last_customer_reply_at_chile": now.astimezone(CL_TZ).isoformat(),
        "last_turn_classification": event["label"],
        "last_turn_intent": event["intent"],
        "last_turn_reason": event["reason"],
        "classifier_version": event["classifier_version"],
        "last_classified_at": now,
        "response_classification": overall_label,
        "response_temperature": overall_temp,
        "followup_status": followup,
        "needs_human_review": pending,
    }
    for attr in ("visit_requested", "human_requested", "interest_signaled",
                 "offer_intent"):
        if event.get(attr):
            fields[attr] = True
    if declined:
        fields["declined"] = True
        fields["declined_at"] = now
    # The first customer response includes refusals and unsubscribe events;
    # it is not the same as a first positive response.
    if row["status"] == "SENT":
        fields["first_response_at"] = now
    if stopped:
        fields.update({"status": "OPTED_OUT", "opted_out_at": now})
    elif row["status"] == "SENT":
        fields["status"] = "RESPONDED"

    increment = {"reply_count": 1}
    if event["positive"]:
        increment["positive_reply_count"] = 1
        fields["last_positive_response_at"] = now
    update = {"$inc": increment, "$set": fields}
    db[DISPATCHES].update_one({"_id": row["_id"]}, update)

    if event["positive"]:
        db[DISPATCHES].update_one(
            {"_id": row["_id"], "first_positive_response_at": {"$exists": False}},
            {"$set": {"first_positive_response_at": now}},
        )
    if event["label"] == "HOT":
        db[DISPATCHES].update_one(
            {"_id": row["_id"], "first_hot_response_at": {"$exists": False}},
            {"$set": {"first_hot_response_at": now}},
        )

    # The lead mirror is namespaced and does NOT overwrite the official
    # CRM HOT/NORMAL/SLA/assignment lifecycle.
    lead_fields = {
        "price_drop_campaign_v1.last_campaign_id": row["campaign_id"],
        "price_drop_campaign_v1.last_property_code": row["property_code"],
        "price_drop_campaign_v1.last_status":
            "OPTED_OUT" if stopped else "RESPONDED",
        "price_drop_campaign_v1.last_response_at": now,
        "price_drop_campaign_v1.last_classified_at": now,
        "price_drop_campaign_v1.classification": overall_label,
        "price_drop_campaign_v1.temperature": overall_temp,
        "price_drop_campaign_v1.last_intent": event["intent"],
        "price_drop_campaign_v1.classifier_version": event["classifier_version"],
        "price_drop_campaign_v1.followup_status": followup,
        "price_drop_campaign_v1.needs_human_review": pending,
        "price_drop_campaign_v1.has_positive_response":
            bool(event["positive"] or row.get("first_positive_response_at")),
    }
    if event["positive"]:
        lead_fields["price_drop_campaign_v1.last_positive_response_at"] = now
    if event["label"] == "HOT":
        lead_fields["price_drop_campaign_v1.last_hot_response_at"] = now
    if stopped:
        lead_fields.update({
            "price_drop_campaign_v1.opted_out": True,
            "price_drop_campaign_v1.opted_out_at": now,
        })
        db[DISPATCHES].update_many({
            "phone_normalized": row["phone_normalized"], "status": "PLANNED",
        }, {"$set": {"status": "SKIPPED_OPT_OUT", "skipped_at": now}})
    db["leads"].update_one({"_id": row["lead_id"]}, {"$set": lead_fields})
    return {
        "status": "OPTED_OUT" if stopped else "ATTRIBUTED",
        "campaign_id": row["campaign_id"],
        "classification": overall_label,
        "temperature": overall_temp,
        "intent": event["intent"],
        "needs_human_review": pending,
    }


def record_bot_reply(
    db: Any, *, phone: str, provider_message_id: str,
    text: str, sent_at: datetime | None = None,
) -> dict:
    """Link delivered chatbot responses to campaign after a real client reply."""
    if not _tracking_enabled():
        return {"status": "DISABLED"}
    now = _utc(sent_at or datetime.now(timezone.utc))
    row = _active_dispatch(db, phone, now, needs_reply=True)
    if not row or not provider_message_id:
        return {"status": "NOT_CAMPAIGN_CONVERSATION"}
    key = hashlib.sha256(
        f"{row['_id']}:bot:{provider_message_id}".encode()
    ).hexdigest()
    try:
        db[TURNS].insert_one({
            "_id": key, "campaign_id": row["campaign_id"],
            "dispatch_id": row["_id"], "lead_id": row["lead_id"],
            "property_code": row["property_code"],
            "direction": "bot", "provider_message_id": str(provider_message_id),
            "text": str(text or "")[:1600], "created_at": now,
        })
    except DuplicateKeyError:
        return {"status": "DUPLICATE_IGNORED"}
    db[DISPATCHES].update_one({"_id": row["_id"]}, {
        "$inc": {"bot_reply_count": 1},
        "$set": {"last_bot_reply_at": now},
    })
    return {"status": "ATTRIBUTED", "campaign_id": row["campaign_id"]}


def record_delivery_update(db: Any, payload: dict, *, now=None) -> dict:
    """Record provider delivery/read receipts without changing the send result."""
    if not _tracking_enabled():
        return {"status": "DISABLED"}
    from .whatsapp_client import normalize_provider_status
    nested = (payload.get("data") or {})
    item = nested.get("messages") or nested
    if isinstance(item, list):
        item = item[0] if item else {}
    item = item if isinstance(item, dict) else {}
    key = item.get("key") or {}
    msgid = str(
        key.get("id") or item.get("messageId")
        or item.get("msgId") or payload.get("messageId") or ""
    ).strip()
    if not msgid:
        return {"status": "UNMATCHED"}
    row = db[DISPATCHES].find_one({"provider_message_id": msgid},
                                  {"_id": 1, "provider_delivery_status": 1})
    if not row:
        return {"status": "UNMATCHED"}
    raw_status = item.get("status") or nested.get("status") or payload.get("status")
    delivery_status = normalize_provider_status(raw_status)
    if delivery_status in {"unknown", "failed"}:
        # A transient/uncertain provider status never rewrites SENT.
        return {"status": "IGNORED"}
    now = _utc(now or datetime.now(timezone.utc))
    db[DISPATCHES].update_one({"_id": row["_id"]}, {"$set": {
        "provider_delivery_status": delivery_status,
        "last_provider_update_at": now,
    }})
    return {"status": "UPDATED"}


def campaign_report(db: Any, campaign_id: str) -> dict:
    cid = _campaign_id(campaign_id)
    header = db[CAMPAIGNS].find_one({"_id": cid})
    if not header:
        raise CampaignBlocked("campaign_not_found")
    rows = list(db[DISPATCHES].find({"campaign_id": cid}, {
        "_id": 1, "lead_id": 1, "property_code": 1,
        "status": 1, "phone_normalized": 1, "scheduled_at": 1,
        "attempted_at": 1, "sent_at": 1, "sent_at_chile": 1,
        "first_response_at": 1, "last_customer_reply_at": 1,
        "first_positive_response_at": 1, "first_hot_response_at": 1,
        "reply_count": 1, "positive_reply_count": 1, "bot_reply_count": 1,
        "interest_signaled": 1, "declined": 1, "visit_requested": 1,
        "human_requested": 1, "offer_intent": 1, "opted_out_at": 1,
        "response_classification": 1, "response_temperature": 1,
        "last_turn_intent": 1, "last_turn_reason": 1,
        "last_classified_at": 1, "classifier_version": 1,
        "followup_status": 1, "needs_human_review": 1,
        "provider_delivery_status": 1, "provider_message_id": 1,
    }).sort("scheduled_at", 1))
    counts = Counter(row.get("status") or "UNKNOWN" for row in rows)
    sent = [row for row in rows if row.get("sent_at")]
    replied = [row for row in rows if (row.get("reply_count") or 0) > 0]
    return {
        "campaign_id": cid, "property_code": header["property_code"],
        "campaign_status": header["status"], "total": len(rows),
        "attempted": header.get("attempts_started", 0),
        "sent": len(sent), "responded": len(replied),
        "response_rate": round(len(replied) / len(sent), 3) if sent else 0.0,
        "interested": sum(
            row.get("response_classification") in {"HOT", "NORMAL"}
            and bool(row.get("first_positive_response_at")) for row in rows
        ),
        "hot": sum(row.get("response_classification") == "HOT" for row in rows),
        "normal": sum(row.get("response_classification") == "NORMAL" for row in rows),
        "pending_human_review": sum(bool(row.get("needs_human_review")) for row in rows),
        "declined": sum(row.get("response_classification") == "RECHAZADO" for row in rows),
        "visits_requested": sum(bool(row.get("visit_requested")) for row in rows),
        "human_requested": sum(bool(row.get("human_requested")) for row in rows),
        "opted_out": sum(bool(row.get("opted_out_at")) for row in rows),
        "statuses": dict(counts),
        "rows": [{
            "lead_id": str(r["lead_id"]),
            "phone_last4": _preview_phone(r.get("phone_normalized") or ""),
            "property_code": r["property_code"],
            "status": r["status"],
            "scheduled_at": r.get("scheduled_at"),
            "attempted_at": r.get("attempted_at"),
            "sent_at": r.get("sent_at"),
            "sent_at_chile": r.get("sent_at_chile"),
            "provider_message_id": r.get("provider_message_id"),
            "response_classification": r.get("response_classification") or "SIN_RESPUESTA",
            "response_temperature": r.get("response_temperature"),
            "last_turn_intent": r.get("last_turn_intent"),
            "last_turn_reason": r.get("last_turn_reason"),
            "first_positive_response_at": r.get("first_positive_response_at"),
            "first_hot_response_at": r.get("first_hot_response_at"),
            "last_customer_reply_at": r.get("last_customer_reply_at"),
            "needs_human_review": bool(r.get("needs_human_review")),
            "followup_status": r.get("followup_status"),
            "provider_delivery_status": r.get("provider_delivery_status"),
            "reply_count": r.get("reply_count", 0),
            "bot_reply_count": r.get("bot_reply_count", 0),
            "first_response_at": r.get("first_response_at"),
            "minutes_to_first_reply": (
                round((r["first_response_at"] - r["sent_at"]).total_seconds() / 60, 1)
                if r.get("first_response_at") and r.get("sent_at") else None
            ),
        } for r in rows],
    }


def campaign_worklist(db: Any, campaign_id: str) -> dict:
    """Read-only human follow-up queue; never marks a CRM lead as managed."""
    report = campaign_report(db, campaign_id)
    priority = {"HOT": 0, "NORMAL": 1}
    pending = [
        row for row in report["rows"]
        if row.get("needs_human_review")
        and row.get("response_classification") in priority
    ]
    pending.sort(key=lambda x: (
        priority.get(x["response_classification"], 9),
        _mongo_utc(x["first_positive_response_at"])
        if x.get("first_positive_response_at")
        else datetime.max.replace(tzinfo=timezone.utc),
        x["lead_id"],
    ))
    return {
        "campaign_id": report["campaign_id"],
        "property_code": report["property_code"],
        "pending": len(pending),
        "hot": sum(r["response_classification"] == "HOT" for r in pending),
        "normal": sum(r["response_classification"] == "NORMAL" for r in pending),
        "leads": pending,
        "note": (
            "Pendientes de revisión comercial. Esta lista NO constituye "
            "una gestión humana, reasignación o modificación del SLA."
        ),
    }
