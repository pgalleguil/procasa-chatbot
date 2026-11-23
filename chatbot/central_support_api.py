"""Authenticated central-support inbox and explicit operator send.

No automatic messages to customers. Sender/session chosen only from the
current verified lead owner, never supplied by the browser. Pablo has a
narrow, opt-in operator permission (his legacy CRM role is "agente").
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .crm_executive_outbound import OutboundBlocked, send_manual
from .crm_executive_outbound_api import _outstanding_uncertain

router = APIRouter()
PABLO_ID = ObjectId("69796bc4bbebf240378eb739")
VIEW_FLAG = "PROCASA_CENTRAL_SUPPORT_CONSOLE_ENABLED"
SEND_FLAG = "PROCASA_CENTRAL_SUPPORT_DELEGATED_SEND_ENABLED"


def _operator_authorized(db, user: dict, *, send: bool = False) -> bool:
    if os.getenv(VIEW_FLAG, "0") != "1":
        return False
    if send and os.getenv(SEND_FLAG, "0") != "1":
        return False
    if (not isinstance(user, dict) or str(user.get("_id") or "") != str(PABLO_ID)
            or user.get("nombre") != "Pablo Galleguillos"
            or user.get("rol") != "agente"):
        return False
    return bool(db["usuarios"].find_one({
        "_id": PABLO_ID, "nombre": "Pablo Galleguillos",
        "rol": "agente", "is_active": True,
    }, {"_id": 1}))


async def _operator(request: Request, *, send: bool = False):
    from webhook import get_current_user_doc
    from .storage import get_db

    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="not_authenticated")
    allowed = await asyncio.to_thread(
        lambda: _operator_authorized(get_db(), user, send=send)
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="central_support_unavailable")
    return user


def _open_tasks(db) -> list[dict]:
    # Bounded, one indexed-style read and one bulk lookup. Never scan private
    # conversation messages or other executives' personal WhatsApp.
    tasks = list(db["crm_tasks"].find({
        "type": "SEARCH_ALTERNATIVES",
        "status": "pending",
        "source": "verified_active_search",
    }, {
        "task_id": 1, "lead_id": 1, "assignment_cycle_id": 1,
        "assigned_to_user_id": 1, "phone": 1, "agent": 1,
        "note": 1, "created_at": 1, "property_codes_to_review": 1,
        "last_central_send": 1,
    }).sort("created_at", -1).limit(50))
    if not tasks:
        return []
    ids = [x["lead_id"] for x in tasks if isinstance(x.get("lead_id"), ObjectId)]
    leads = {
        x["_id"]: x for x in db["leads"].find({"_id": {"$in": ids}}, {
            "lifecycle": 1, "commercial_intelligence.active_search": 1,
            "phone_is_synthetic": 1, "contact_identity_incomplete": 1,
            "contact_restrictions.marketing_opt_out": 1,
        })
    }
    rows = []
    for task in tasks:
        lead = leads.get(task.get("lead_id"))
        if not lead or lead.get("phone_is_synthetic") or lead.get("contact_identity_incomplete"):
            continue
        life = lead.get("lifecycle") or {}
        search = ((lead.get("commercial_intelligence") or {}).get("active_search") or {})
        if (str(life.get("current_assignment_cycle_id") or "") != str(task.get("assignment_cycle_id") or "")
                or str(life.get("assigned_to_user_id") or "") != str(task.get("assigned_to_user_id") or "")
                or search.get("status") != "ACTIVE" or not search.get("followup_required")
                or search.get("opt_out")
                or (lead.get("contact_restrictions") or {}).get("marketing_opt_out")):
            continue
        stamp = task.get("created_at")
        rows.append({
            "task_id": str(task.get("task_id") or task["_id"]),
            "lead_id": str(task["lead_id"]),
            "phone": str(task.get("phone") or ""),
            "executive": str(task.get("agent") or ""),
            "note": str(task.get("note") or "")[:1200],
            "property_codes_to_review": (task.get("property_codes_to_review") or [])[:8],
            "created_at": stamp.isoformat() if isinstance(stamp, datetime) else str(stamp or ""),
            "last_central_send": str(task.get("last_central_send") or ""),
        })
    return rows


@router.get("/api/crm/central-support/tasks")
async def central_support_tasks(request: Request):
    await _operator(request)
    from .storage import get_db
    rows = await asyncio.to_thread(lambda: _open_tasks(get_db()))
    return {"tasks": rows, "count": len(rows)}


@router.get("/crm/apoyo-central", response_class=HTMLResponse)
async def central_support_console(request: Request):
    await _operator(request)
    from webhook import templates
    return templates.TemplateResponse(request, "crm_central_support.html", {
        "request": request,
    })


def _send_checked(db, *, user: dict, data: dict) -> dict:
    required = {"lead_id", "task_id", "text", "idempotency_key", "confirmed"}
    if set(data) != required or data.get("confirmed") is not True:
        raise OutboundBlocked("invalid_support_request")
    lead_id = data.get("lead_id")
    if not isinstance(lead_id, str) or not ObjectId.is_valid(lead_id):
        raise OutboundBlocked("invalid_lead_id")
    task_id = data.get("task_id")
    if not isinstance(task_id, str) or not task_id.startswith("active_search:"):
        raise OutboundBlocked("invalid_support_task")
    lead_oid = ObjectId(lead_id)
    task = db["crm_tasks"].find_one({
        "_id": task_id,
        "lead_id": lead_oid,
        "type": "SEARCH_ALTERNATIVES",
        "status": "pending",
        "source": "verified_active_search",
    })
    lead = db["leads"].find_one({"_id": lead_oid})
    if not task or not lead:
        raise OutboundBlocked("active_support_task_required")
    life = lead.get("lifecycle") or {}
    owner_id = str(life.get("assigned_to_user_id") or "")
    cycle = str(life.get("current_assignment_cycle_id") or "")
    if (not ObjectId.is_valid(owner_id) or not cycle
            or cycle != str(task.get("assignment_cycle_id") or "")
            or owner_id != str(task.get("assigned_to_user_id") or "")):
        raise OutboundBlocked("assignment_changed")
    search = (lead.get("commercial_intelligence") or {}).get("active_search") or {}
    if (search.get("status") != "ACTIVE" or search.get("followup_required") is not True
            or search.get("opt_out") or lead.get("phone_is_synthetic")
            or (lead.get("contact_restrictions") or {}).get("marketing_opt_out")):
        raise OutboundBlocked("support_no_longer_allowed")
    if _outstanding_uncertain(db, lead_id, executive_id=owner_id):
        raise OutboundBlocked("previous_delivery_unconfirmed")
    result = send_manual(
        db, lead_id=lead_id, actor_user_id=owner_id,
        requester_user=user, central_support=True,
        message=data.get("text"), idempotency_key=data.get("idempotency_key"),
    )
    if result.get("status") == "accepted" and result.get("recorded") is True:
        db["crm_tasks"].update_one({
            "_id": task_id, "status": "pending",
            "assignment_cycle_id": cycle,
            "assigned_to_user_id": owner_id,
        }, {"$set": {
            "last_central_send": datetime.now(timezone.utc),
            "last_central_send_provider_id": result.get("provider_message_id"),
        }})
    return result


@router.post("/api/crm/central-support/send")
async def central_support_send(request: Request):
    # A cookie-only cross-site form must not be able to dispatch messages.
    if request.headers.get("x-requested-with") != "XMLHttpRequest":
        raise HTTPException(status_code=403, detail="same_origin_action_required")
    user = await _operator(request, send=True)
    try:
        data = await request.json()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid_json") from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="invalid_json")
    from .storage import get_db
    try:
        result = await asyncio.to_thread(
            lambda: _send_checked(get_db(), user=user, data=data)
        )
    except OutboundBlocked:
        # Never reveal recipient/session credentials or other lead identities.
        raise HTTPException(status_code=409, detail="central_support_send_blocked") from None
    if result.get("status") == "accepted" and result.get("recorded") is True:
        return {"status": "accepted", "recorded": True,
                "provider_message_id": result.get("provider_message_id")}
    if result.get("status") == "duplicate":
        return {"status": "duplicate", "previous_state": result.get("previous_state")}
    # Uncertain provider responses must not be automatically retried.
    return {"status": result.get("status", "delivery_unknown"), "recorded": False}
