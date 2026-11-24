"""Isolated named-executive WaSender CRM API with safe manual contingency.

Every session requires a dedicated opt-in, authenticated agent identity,
current lead/cycle authorization, and verified provider sender phone.
Other executives retain wa.me. Uncertain delivery never unlocks re-send.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import logging
import os

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Request
import requests

from config import Config
from .crm_permissions import can_administer_leads
from .crm_executive_outbound import (
    OUTBOX, OutboundBlocked, eligible_manual_session, send_manual,
    verify_provider_sender_identity,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_PABLO_USER_ID = "69796bc4bbebf240378eb739"
_PABLO_NAME = "Pablo Galleguillos"
_PABLO_SESSION = "133286"
_PILOT_FLAG = "PROCASA_PABLO_CRM_WASENDER_API_ENABLED"
_MARIELA_USER_ID = "6989c6309dd2ba54e478196c"
_MARIELA_NAME = "Mariela Arriagada"
_MARIELA_SESSION = "133526"
_MARIELA_FLAG = "PROCASA_MARIELA_CRM_WASENDER_API_ENABLED"
_ADDITIONAL_PROFILES = (
    ("6a681413140190dde11f26d1", "Hernán Castro", "133517",
     "PROCASA_HERNAN_CRM_WASENDER_API_ENABLED"),
    ("6989c6309dd2ba54e478196b", "Susana Ensignia", "133523",
     "PROCASA_SUSANA_CRM_WASENDER_API_ENABLED"),
    ("699653a96a099d6e9612d519", "Paula Morales", "133527",
     "PROCASA_PAULA_CRM_WASENDER_API_ENABLED"),
)
_INCIDENTS = "crm_executive_whatsapp_incidents"
_UNCERTAIN_STATES = ("provider_attempt", "delivery_unknown",
                     "accepted_without_provider_id")
_SAFE_PREFLIGHT_FALLBACK = frozenset({
    # All these are checked AFTER current lead/owner/opt-out validation.
    "executive_session_not_enabled", "executive_session_not_ready",
    "pilot_session_mismatch", "pilot_sender_unverified",
    "sender_identity_unverified", "sender_identity_mismatch",
})
_SAFE_REJECTED_REASONS = frozenset({
    "session_disconnected", "authentication", "subscription", "rate_limit",
    "invalid_request", "provider_rejected",
})
_SAFE_STATUS_CODES = frozenset({
    "connected", "connecting", "need_scan", "need_passkey", "disconnected",
    "logged_out", "expired",
})


async def _user(request: Request):
    from webhook import get_current_user_doc
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user


def _pablo_enabled(user: dict) -> bool:
    return (
        os.getenv(_PILOT_FLAG, "0") == "1"
        and str(user.get("_id") or "") == _PABLO_USER_ID
        and user.get("nombre") == _PABLO_NAME
        and user.get("rol") == "agente"
    )


def _mariela_enabled(user: dict) -> bool:
    return (
        os.getenv(_MARIELA_FLAG, "0") == "1"
        and str(user.get("_id") or "") == _MARIELA_USER_ID
        and user.get("nombre") == _MARIELA_NAME
        and user.get("rol") == "agente"
    )


def _profile(user: dict) -> dict | None:
    # Identity comes from the authenticated user document, never from the body.
    if _pablo_enabled(user):
        return {"id": _PABLO_USER_ID, "name": _PABLO_NAME,
                "session": _PABLO_SESSION}
    if _mariela_enabled(user):
        return {"id": _MARIELA_USER_ID, "name": _MARIELA_NAME,
                "session": _MARIELA_SESSION}
    for uid, name, session, flag in _ADDITIONAL_PROFILES:
        if (os.getenv(flag, "0") == "1"
                and str(user.get("_id") or "") == uid
                and user.get("nombre") == name and user.get("rol") == "agente"):
            return {"id": uid, "name": name, "session": session}
    return None


def _eligible_executive(db, *, lead_id: str, profile: dict) -> dict:
    if not isinstance(lead_id, str) or not ObjectId.is_valid(lead_id):
        raise OutboundBlocked("invalid_lead_id")
    lead = db["leads"].find_one({"_id": ObjectId(lead_id)})
    info = eligible_manual_session(
        db, lead=lead, actor_user_id=profile["id"],
    )
    if str(info["session"]["session_id"]) != profile["session"]:
        raise OutboundBlocked("pilot_session_mismatch")
    # Session ID and stored credentials are not sufficient sender proof:
    # compare the provider's /user phone against THIS executive's Mongo number.
    sender = db["usuarios"].find_one(
        {"_id": ObjectId(profile["id"]), "is_active": True, "rol": "agente"},
        {"telefono": 1},
    )
    if not sender or not sender.get("telefono"):
        raise OutboundBlocked("pilot_sender_unverified")
    verify_provider_sender_identity(
        info["session"], expected_phone=sender["telefono"], force=True,
    )
    return info


def _eligible_pablo(db, *, user: dict, lead_id: str) -> dict:
    if not _pablo_enabled(user):
        raise OutboundBlocked("pilot_user_not_authorized")
    return _eligible_executive(db, lead_id=lead_id,
        profile={"id": _PABLO_USER_ID, "name": _PABLO_NAME,
                 "session": _PABLO_SESSION})


def _eligible_mariela(db, *, user: dict, lead_id: str) -> dict:
    if not _mariela_enabled(user):
        raise OutboundBlocked("pilot_user_not_authorized")
    return _eligible_executive(db, lead_id=lead_id,
        profile={"id": _MARIELA_USER_ID, "name": _MARIELA_NAME,
                 "session": _MARIELA_SESSION})


def _outstanding_uncertain(db, lead_id: str, *, executive_id: str = _PABLO_USER_ID) -> bool:
    """Block a second dispatch/wa.me while earlier provider acceptance is unknown."""
    if not ObjectId.is_valid(lead_id):
        return True
    lead = db["leads"].find_one(
        {"_id": ObjectId(lead_id)}, {"lifecycle.current_assignment_cycle_id": 1},
    ) or {}
    cycle = str((lead.get("lifecycle") or {}).get("current_assignment_cycle_id") or "")
    if not cycle:
        return True
    return bool(db[OUTBOX].find_one({
        "lead_id": ObjectId(lead_id),
        "assignment_cycle_id": cycle,
        "executive_id": executive_id,
        "$or": [
            {"state": {"$in": list(_UNCERTAIN_STATES)}},
            {"state": "accepted", "canonical_recorded": {"$ne": True}},
        ],
    }, {"_id": 1}))


def _session_status(session: dict, *, get=None) -> str:
    """Read-only WaSender session health. Unknown is never considered connected."""
    token = str((session or {}).get("token") or "")
    if not token:
        return "unavailable"
    try:
        response = (get or requests.get)(
            Config.WASENDER_BASE_URL.rstrip("/") + "/status",
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            timeout=5,
        )
        if response.status_code != 200:
            return "unavailable"
        body = response.json()
        if not isinstance(body, dict):
            return "unavailable"
        status = body.get("status")
        if isinstance(body.get("data"), dict) and not status:
            status = body["data"].get("status")
        status = str(status or "").strip().lower()
        return status if status in _SAFE_STATUS_CODES else "unavailable"
    except (requests.RequestException, ValueError, TypeError):
        return "unavailable"


def _incident(db, *, lead_id: str, reason: str, phase: str,
              session_id: str = _PABLO_SESSION,
              executive_id: str = _PABLO_USER_ID,
              executive_name: str = _PABLO_NAME) -> None:
    """Durable, deduplicated incident inbox; no numbers, secrets or message text."""
    safe_reason = str(reason or "unknown").lower()
    if not safe_reason.replace("_", "").isalnum() or len(safe_reason) > 55:
        safe_reason = "unknown"
    safe_phase = "preflight" if phase == "preflight" else "post_send"
    digest = sha256(
        f"crm-wa/v1|{executive_id}|{session_id}|{safe_phase}|{safe_reason}".encode()
    ).hexdigest()
    now = datetime.now(timezone.utc)
    try:
        db[_INCIDENTS].update_one(
            {"_id": "crm-wa:" + digest},
            {"$setOnInsert": {
                "first_seen_at": now, "executive_user_id": executive_id,
                "executive_name": executive_name, "session_id": session_id,
                "reason": safe_reason, "phase": safe_phase,
            }, "$set": {
                "status": "open", "last_seen_at": now,
                "last_lead_id": ObjectId(lead_id) if ObjectId.is_valid(lead_id) else None,
            }, "$inc": {"occurrences": 1}},
            upsert=True,
        )
    except Exception as exc:
        logger.error("CRM_EXEC_WA_INCIDENT persist_failed=%s", type(exc).__name__)


def _recover_incidents(db, *, executive_id: str = _PABLO_USER_ID) -> None:
    """Only connectivity incidents recover on a verified connected preflight."""
    try:
        db[_INCIDENTS].update_many({
            "executive_user_id": executive_id, "status": "open",
            "phase": "preflight",
            "reason": {"$in": [
                "disconnected", "logged_out", "expired", "need_scan",
                "need_passkey", "connecting", "unavailable",
                "sender_identity_unverified", "executive_session_not_ready",
            ]},
        }, {"$set": {"status": "resolved",
                     "resolved_at": datetime.now(timezone.utc)}})
    except Exception as exc:
        logger.warning("CRM_EXEC_WA_INCIDENT recover_failed=%s", type(exc).__name__)


def _queue_operator_alert(*, user: dict, reason: str, lead_id: str | None = None) -> None:
    """Signal an already-detected CRM outage, without extra provider probes.

    The send is isolated from the executive's click. A durable Mongo claim
    prevents repeated alerts from multiple clicks, users or app processes.
    """
    if os.getenv("PROCASA_EXEC_WA_PABLO_ALERTS_ENABLED", "0") != "1":
        return
    profile = _profile(user)
    if profile is None:
        return
    from .executive_session_admin_alerts import notify_pablo_on_crm_failure
    asyncio.create_task(notify_pablo_on_crm_failure(
        session_id=profile["session"], executive_name=profile["name"],
        reason=reason, lead_id=lead_id,
    ))


def _assess(db, *, user: dict, lead_id: str, get=None) -> dict:
    """Server-side safe choice; owner, consent and session gates cannot be bypassed."""
    profile = _profile(user)
    if profile is None:
        return {"method": "wa_me", "available": False}
    incident_context = {
        "session_id": profile["session"], "executive_id": profile["id"],
        "executive_name": profile["name"],
    }
    if _outstanding_uncertain(db, lead_id, executive_id=profile["id"]):
        _incident(db, lead_id=lead_id, reason="delivery_unconfirmed",
                  phase="post_send", **incident_context)
        return {"method": "blocked", "available": False,
                "reason": "delivery_unconfirmed"}
    try:
        info = (_eligible_pablo(db, user=user, lead_id=lead_id)
                if profile["id"] == _PABLO_USER_ID
                else _eligible_mariela(db, user=user, lead_id=lead_id)
                if profile["id"] == _MARIELA_USER_ID
                else _eligible_executive(db, lead_id=lead_id, profile=profile))
    except OutboundBlocked as exc:
        if exc.code in _SAFE_PREFLIGHT_FALLBACK:
            _incident(db, lead_id=lead_id, reason=exc.code,
                      phase="preflight", **incident_context)
            return {"method": "wa_me", "available": False,
                    "fallback": True, "reason": "session_unavailable"}
        return {"method": "blocked", "available": False, "reason": "not_authorized"}
    status = _session_status(info["session"], get=get)
    if status != "connected":
        _incident(db, lead_id=lead_id, reason=status,
                  phase="preflight", **incident_context)
        return {"method": "wa_me", "available": False,
                "fallback": True, "reason": "session_unavailable"}
    _recover_incidents(db, executive_id=profile["id"])
    # The existing CRM preflight already verified provider status/identity.
    # Re-arm only a previously raised alert. No additional WaSender requests.
    if os.getenv("PROCASA_EXEC_WA_PABLO_ALERTS_ENABLED", "0") == "1":
        from .executive_session_admin_alerts import mark_event_recovered
        try:
            mark_event_recovered(db, session_id=profile["session"])
        except Exception as exc:
            logger.warning("EXEC_WA_PABLO_EVENT recovery_error=%s",
                           type(exc).__name__)
    return {"method": "executive_wasender_api", "available": True}


@router.get("/api/crm/executive-whatsapp/available/{lead_id}")
async def crm_executive_whatsapp_available(request: Request, lead_id: str):
    user = await _user(request)
    if not ObjectId.is_valid(lead_id):
        raise HTTPException(status_code=400, detail="lead_id_invalid")
    if _profile(user) is None:
        # Named but disabled pilots keep the old link; all other agents' UI
        # is unchanged and this GET never grants their POST permission.
        known_disabled = (
            (str(user.get("_id") or "") == _MARIELA_USER_ID
             and user.get("nombre") == _MARIELA_NAME)
            or any(str(user.get("_id") or "") == uid and user.get("nombre") == name
                   for uid, name, _session, _flag in _ADDITIONAL_PROFILES)
        )
        if known_disabled and user.get("rol") == "agente":
            return {"available": False, "method": "wa_me", "fallback": True}
        return {"available": False, "method": "wa_me"}
    from .storage import get_db
    assessment = await asyncio.to_thread(lambda: _assess(
        get_db(), user=user, lead_id=lead_id,
    ))
    if (assessment.get("fallback") is True
            and assessment.get("reason") == "session_unavailable"):
        _queue_operator_alert(user=user, reason="session_unavailable")
    elif assessment.get("reason") == "delivery_unconfirmed":
        _queue_operator_alert(user=user, reason="delivery_unconfirmed", lead_id=lead_id)
    return assessment


@router.post("/api/crm/executive-whatsapp/send")
async def crm_executive_whatsapp_send(request: Request):
    user = await _user(request)
    if _profile(user) is None:
        raise HTTPException(status_code=403, detail="pilot_user_not_authorized")
    try:
        data = await request.json()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid_json") from None
    if (not isinstance(data, dict)
            or set(data) != {"lead_id", "text", "idempotency_key"}
            or not isinstance(data.get("lead_id"), str)
            or not ObjectId.is_valid(data["lead_id"])):
        raise HTTPException(status_code=400, detail="invalid_send_request")
    from .storage import get_db

    def invoke():
        db = get_db()
        assessment = _assess(db, user=user, lead_id=data["lead_id"])
        if not assessment["available"]:
            return {"status": "manual_fallback" if assessment["method"] == "wa_me"
                    else "blocked", "reason": assessment.get("reason")}
        return send_manual(
            db, lead_id=data["lead_id"], actor_user_id=_profile(user)["id"],
            message=data["text"], idempotency_key=data["idempotency_key"],
        )

    try:
        result = await asyncio.to_thread(invoke)
    except OutboundBlocked as exc:
        # The active cycle or consent may have changed. Never offer a bypass.
        raise HTTPException(status_code=409, detail={
            "code": "operation_blocked",
        }) from None

    if result.get("status") == "accepted" and result.get("recorded") is True:
        return {"status": "accepted", "provider_message_id": result["provider_message_id"],
                "recorded": True}
    if result.get("status") == "duplicate":
        return {"status": "duplicate", "previous_state": result.get("previous_state")}
    if result.get("status") == "manual_fallback":
        # The POST can detect a disconnection after the initial GET preflight.
        if result.get("reason") == "session_unavailable":
            _queue_operator_alert(user=user, reason="session_unavailable")
        return {"status": "manual_fallback", "method": "wa_me"}
    if result.get("status") == "blocked":
        if result.get("reason") == "delivery_unconfirmed":
            _queue_operator_alert(user=user, reason="delivery_unconfirmed", lead_id=data["lead_id"])
        raise HTTPException(status_code=409, detail={"code": "operation_blocked"})
    if result.get("status") == "rejected":
        # The provider explicitly rejected the request; no send was accepted.
        db = await asyncio.to_thread(get_db)
        reason = str(result.get("reason_code") or "provider_rejected")
        profile = _profile(user)
        await asyncio.to_thread(_incident, db, lead_id=data["lead_id"],
                                reason=reason, phase="post_send",
                                session_id=profile["session"],
                                executive_id=profile["id"],
                                executive_name=profile["name"])
        # Every explicit provider rejection must reach the administrator.
        _queue_operator_alert(user=user, reason=reason, lead_id=data["lead_id"])
        if reason == "recipient_not_registered":
            return {"status": "recipient_not_registered", "method": "blocked"}
        if reason in _SAFE_REJECTED_REASONS:
            return {"status": "manual_fallback", "method": "wa_me"}
        raise HTTPException(status_code=422, detail={"code": "operation_blocked"})
    if result.get("status") in ("accepted", "accepted_without_provider_id",
                                "delivery_unknown"):
        db = await asyncio.to_thread(get_db)
        profile = _profile(user)
        await asyncio.to_thread(_incident, db, lead_id=data["lead_id"],
                                reason="delivery_unconfirmed", phase="post_send",
                                session_id=profile["session"],
                                executive_id=profile["id"],
                                executive_name=profile["name"])
        _queue_operator_alert(user=user, reason="delivery_unconfirmed", lead_id=data["lead_id"])
        raise HTTPException(status_code=503, detail={"code": "delivery_unconfirmed"})
    raise HTTPException(status_code=502, detail={"code": "operation_blocked"})


@router.get("/api/crm/executive-whatsapp/incidents")
async def crm_executive_whatsapp_admin_incidents(request: Request):
    user = await _user(request)
    if not can_administer_leads(user.get("rol")):
        raise HTTPException(status_code=403, detail="forbidden")
    from .storage import get_db

    def resolve():
        db = get_db()
        rows = list(db[_INCIDENTS].find(
            {"status": "open"},
            {"executive_name": 1, "session_id": 1, "reason": 1,
             "phase": 1, "last_seen_at": 1, "last_lead_id": 1,
             "occurrences": 1},
        ).sort("last_seen_at", -1).limit(20))
        return {"count": len(rows), "incidents": [{
            "executive": str(row.get("executive_name") or "")[:100],
            "session": str(row.get("session_id") or "")[:22],
            "reason": str(row.get("reason") or "")[:60],
            "phase": str(row.get("phase") or ""),
            "last_seen": row["last_seen_at"].isoformat()
                if isinstance(row.get("last_seen_at"), datetime) else "",
            "lead_id": str(row["last_lead_id"])
                if isinstance(row.get("last_lead_id"), ObjectId) else None,
            "occurrences": min(int(row.get("occurrences") or 1), 99999),
        } for row in rows]}
    return await asyncio.to_thread(resolve)
