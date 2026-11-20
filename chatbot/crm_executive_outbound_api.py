"""Authenticated, ownership-scoped CRM WhatsApp API for connected executives."""
from __future__ import annotations

import asyncio
from bson import ObjectId
from fastapi import APIRouter, HTTPException, Request

from .crm_executive_outbound import (
    OutboundBlocked, eligible_manual_session, send_manual,
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
            info = eligible_manual_session(
                db, lead=lead, actor_user_id=str(user.get("_id") or ""),
            )
            return {"available": True, "method": "executive_wasender_api"}
        except OutboundBlocked:
            return {"available": False, "method": "wa_me"}

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
        return send_manual(
            get_db(), lead_id=data["lead_id"],
            actor_user_id=str(user.get("_id") or ""),
            message=data["text"], idempotency_key=data["idempotency_key"],
        )
    try:
        result = await asyncio.to_thread(invoke)
    except OutboundBlocked as exc:
        raise HTTPException(status_code=409, detail=exc.code) from None
    if result.get("status") == "accepted" and result.get("recorded") is True:
        return {"status": "accepted", "provider_message_id": result["provider_message_id"],
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
