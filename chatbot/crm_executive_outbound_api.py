"""One-user pilot: CRM sends via the linked WaSender session for Pablo only.

All other CRM accounts continue to use the existing wa.me compose link.
Never auto-fallback to a second transport after an uncertain provider attempt.
Never send without the authenticated user's explicit button click.
"""
from __future__ import annotations

import asyncio
import os

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Request

from .crm_executive_outbound import (
    OutboundBlocked, eligible_manual_session, send_manual,
    verify_provider_sender_identity,
)

router = APIRouter()

_PABLO_USER_ID = "69796bc4bbebf240378eb739"
_PABLO_NAME = "Pablo Galleguillos"
_PABLO_SESSION = "133286"
_PILOT_FLAG = "PROCASA_PABLO_CRM_WASENDER_API_ENABLED"


async def _user(request: Request):
    from webhook import get_current_user_doc
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user


def _pablo_enabled(user: dict) -> bool:
    # Trust only the authenticated DB identity, never user_id/role in the body.
    return (
        os.getenv(_PILOT_FLAG, "0") == "1"
        and str(user.get("_id") or "") == _PABLO_USER_ID
        and user.get("nombre") == _PABLO_NAME
        and user.get("rol") == "agente"
    )


def _eligible_pablo(db, *, user: dict, lead_id: str) -> dict:
    if not _pablo_enabled(user):
        raise OutboundBlocked("pilot_user_not_authorized")
    if not isinstance(lead_id, str) or not ObjectId.is_valid(lead_id):
        raise OutboundBlocked("invalid_lead_id")
    lead = db["leads"].find_one({"_id": ObjectId(lead_id)})
    info = eligible_manual_session(
        db, lead=lead, actor_user_id=_PABLO_USER_ID,
    )
    if str(info["session"]["session_id"]) != _PABLO_SESSION:
        raise OutboundBlocked("pilot_session_mismatch")
    # The session slot was formerly allocated to Erika, but Pablo is its
    # current active operator for testing. Confirm the real WhatsApp number
    # behind the bearer token equals Pablo's registered phone, not Erika's.
    # Session ID / variable name alone is not sufficient sender proof.
    sender = db["usuarios"].find_one(
        {"_id": ObjectId(_PABLO_USER_ID), "is_active": True, "rol": "agente"},
        {"telefono": 1},
    )
    if not sender or not sender.get("telefono"):
        raise OutboundBlocked("pilot_sender_unverified")
    verify_provider_sender_identity(
        info["session"],
        expected_phone=sender["telefono"],
        force=True,
    )
    return info


@router.get("/api/crm/executive-whatsapp/available/{lead_id}")
async def crm_executive_whatsapp_available(request: Request, lead_id: str):
    user = await _user(request)
    if not ObjectId.is_valid(lead_id):
        raise HTTPException(status_code=400, detail="lead_id_invalid")
    if not _pablo_enabled(user):
        # No changed behavior for every other executive or administrator.
        return {"available": False, "method": "wa_me"}
    from .storage import get_db

    def resolve():
        try:
            _eligible_pablo(get_db(), user=user, lead_id=lead_id)
        except OutboundBlocked:
            # A failed pilot credential/session/consent check must not silently
            # fall back to an unrelated sender or untracked WhatsApp Web.
            return {"available": False, "method": "blocked"}
        return {"available": True, "method": "executive_wasender_api"}

    return await asyncio.to_thread(resolve)


@router.post("/api/crm/executive-whatsapp/send")
async def crm_executive_whatsapp_send(request: Request):
    user = await _user(request)
    if not _pablo_enabled(user):
        raise HTTPException(status_code=403, detail="pilot_user_not_authorized")
    try:
        data = await request.json()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid_json") from None
    if (not isinstance(data, dict)
            or set(data) != {"lead_id", "text", "idempotency_key"}):
        raise HTTPException(status_code=400, detail="invalid_send_request")
    from .storage import get_db

    def invoke():
        db = get_db()
        _eligible_pablo(db, user=user, lead_id=data["lead_id"])
        # Reuse the existing atomic outbox and owner/cycle/opt-out guards.
        return send_manual(
            db,
            lead_id=data["lead_id"],
            actor_user_id=_PABLO_USER_ID,
            message=data["text"],
            idempotency_key=data["idempotency_key"],
        )

    try:
        result = await asyncio.to_thread(invoke)
    except OutboundBlocked as exc:
        raise HTTPException(status_code=409, detail=exc.code) from None
    if result.get("status") == "accepted" and result.get("recorded") is True:
        return {
            "status": "accepted",
            "provider_message_id": result["provider_message_id"],
            "recorded": True,
        }
    if result.get("status") == "duplicate":
        return {
            "status": "duplicate",
            "previous_state": result.get("previous_state"),
        }
    if result.get("status") == "blocked":
        raise HTTPException(status_code=409, detail=result.get("reason") or "pilot_blocked")
    if result.get("status") in ("accepted", "accepted_without_provider_id", "delivery_unknown"):
        raise HTTPException(status_code=503, detail="provider_delivery_unconfirmed")
    raise HTTPException(
        status_code=502,
        detail=result.get("reason_code") or "provider_send_failed",
    )
