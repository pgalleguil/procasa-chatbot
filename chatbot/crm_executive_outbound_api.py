"""Authenticated, ownership-scoped CRM WhatsApp API for connected executives."""
from __future__ import annotations

import asyncio
from bson import ObjectId
from fastapi import APIRouter, HTTPException, Request

from .crm_executive_outbound import (
    OutboundBlocked, eligible_requester_session, send_manual,
)

router = APIRouter()


async def _user(request: Request):
    # Late import avoids a circular dependency with the FastAPI entrypoint.
    from webhook import get_current_user_doc
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user


@router.get("/api/crm/executive-whatsapp/available/{lead_id}")
async def crm_executive_whatsapp_available(request: Request, lead_id: str):
    user = await _user(request)
    if not ObjectId.is_valid(lead_id):
        raise HTTPException(status_code=400, detail="lead_id_invalid")
    from .storage import get_db

    def resolve():
        db = get_db()
        lead = db["leads"].find_one({"_id": ObjectId(lead_id)})
        try:
            info = eligible_requester_session(db, lead=lead, requester_user=user)
            return {
                "available": True, "method": "executive_wasender_api",
                "delegated": info["delegated"], "sender_name": info["sender_name"],
            }
        except OutboundBlocked as exc:
            # A configured sender with a failing gate must never silently
            # switch to wa.me and bypass canonical API logging.
            if exc.code in ("requester_not_authorized", "current_assignment_required",
                            "executive_session_not_ready", "crm_direct_send_disabled",
                            "customer_contact_blocked", "contact_not_verified",
                            "ambiguous_or_unassigned_lead", "executive_inactive"):
                method = "blocked"
            elif exc.code == "executive_session_not_enabled":
                # A registered but temporarily disabled/disallowed linked phone
                # should NOT silently fall back to a different WhatsApp sender.
                owner = str((lead or {}).get("lifecycle", {}).get("assigned_to_user_id") or "")
                profile = db["usuarios"].find_one(
                    {"_id": ObjectId(owner)}, {"wasender.session_id": 1},
                ) if ObjectId.is_valid(owner) else None
                sid = (profile or {}).get("wasender") or {}
                method = "blocked" if sid.get("session_id") else "wa_me"
            else:
                method = "wa_me"
            return {"available": False, "method": method}

    return await asyncio.to_thread(resolve)


@router.post("/api/crm/executive-whatsapp/send")
async def crm_executive_whatsapp_send(request: Request):
    user = await _user(request)
    try:
        data = await request.json()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid_json") from None
    if not isinstance(data, dict) or set(data) != {"lead_id", "text", "idempotency_key"}:
        raise HTTPException(status_code=400, detail="invalid_send_request")
    from .storage import get_db

    def invoke():
        db = get_db()
        if not ObjectId.is_valid(str(data["lead_id"])):
            raise OutboundBlocked("invalid_lead_id")
        lead = db["leads"].find_one({"_id": ObjectId(str(data["lead_id"]))})
        info = eligible_requester_session(db, lead=lead, requester_user=user)
        return send_manual(
            db, lead_id=data["lead_id"], actor_user_id=info["owner_id"],
            requester_user=user,
            message=data["text"], idempotency_key=data["idempotency_key"],
        )
    try:
        result = await asyncio.to_thread(invoke)
    except OutboundBlocked as exc:
        raise HTTPException(status_code=409, detail=exc.code) from None
    if result.get("status") == "accepted" and result.get("recorded") is True:
        return {"status": "accepted", "provider_message_id": result["provider_message_id"],
                "delegated": bool(result.get("delegated")),
                "sla_management": result.get("sla_management", "unverified")}
    if result.get("status") == "duplicate":
        return {"status": "duplicate", "previous_state": result.get("previous_state")}
    if result.get("status") == "rejected":
        # Only our stable, non-PII classification reaches the browser.
        reason = result.get("reason_code") or "provider_rejected"
        allowed = {"authentication", "subscription", "rate_limit", "session_disconnected",
                   "invalid_recipient", "invalid_request", "provider_rejected"}
        if reason not in allowed:
            reason = "provider_rejected"
        raise HTTPException(status_code=502, detail=f"provider_rejected:{reason}")
    if result.get("status") in ("accepted", "accepted_without_provider_id", "delivery_unknown"):
        raise HTTPException(status_code=503, detail="provider_delivery_unconfirmed")
    raise HTTPException(status_code=502, detail="provider_send_failed")
