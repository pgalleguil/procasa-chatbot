"""CRM WhatsApp manual link-only transport.

Temporarily restore the pre-WaSender CRM behavior: the browser opens WhatsApp
with a prefilled message, and the human explicitly sends it. No CRM route may
send a message via the WaSender API while this policy is active.

This file does not alter executive WhatsApp audit/webhooks, HOT automation,
the office chatbot, campaigns, or existing canonical delivery records.
"""
from __future__ import annotations

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


async def _user(request: Request):
    from webhook import get_current_user_doc
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user


@router.get("/api/crm/executive-whatsapp/available/{lead_id}")
async def crm_executive_whatsapp_available(request: Request, lead_id: str):
    # Keep the existing contract so cached clients reliably choose the link.
    await _user(request)
    if not ObjectId.is_valid(lead_id):
        raise HTTPException(status_code=400, detail="lead_id_invalid")
    return {"available": False, "method": "wa_me"}


@router.post("/api/crm/executive-whatsapp/send")
async def crm_executive_whatsapp_send(request: Request):
    # Fail closed even if an old tab bypasses the new browser UI or a former
    # PROCASA_EXEC_CRM_DIRECT_SEND_ENABLED=1 remains in a worker environment.
    await _user(request)
    raise HTTPException(status_code=409, detail="crm_manual_link_only")
