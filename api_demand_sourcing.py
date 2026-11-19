"""Authenticated CRM endpoints for demand-driven property sourcing."""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from api_crm_weekly_report import admin_user
from chatbot.storage import get_db
from chatbot.demand_sourcing_engine import (
    catalog_health,
    deliver_candidate_to_executive,
    deliver_pending_admin_alerts,
    ack_admin_alerts,
    enqueue_search_job,
    get_demand_case,
    list_demand_cases,
    record_case_tracking,
    retry_search_job,
    review_candidate,
    update_case_criteria,
)

router = APIRouter(tags=["CRM Demand Sourcing"])
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def _safe_json(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _actor(user: dict) -> str:
    return str(user.get("_id") or user.get("username") or user.get("email") or "admin")


async def _run_sync(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        referer = request.headers.get("referer")
        origin = "" if not referer else f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
    parsed = urlparse(origin or "")
    if (not parsed.netloc or parsed.netloc.casefold() != request.url.netloc.casefold()
            or parsed.scheme.casefold() != request.url.scheme.casefold()):
        raise HTTPException(status_code=403, detail="Origen de solicitud inválido")


@router.get("/crm/captacion-demanda", response_class=HTMLResponse)
async def demand_sourcing_admin_page(request: Request):
    user = await admin_user(request)
    return _templates.TemplateResponse(request, "demand_sourcing_admin.html", {
        "request": request,
        "user_name": user.get("nombre") or user.get("username") or "Administración",
        "initial_case": request.query_params.get("case", ""),
    })


@router.get("/api/crm/demand-sourcing/cases")
async def demand_sourcing_cases(request: Request, status: str | None = None,
                               executive: str | None = None, commune: str | None = None,
                               page: int = 1, page_size: int = 30,
                               min_priority: int | None = None,
                               stale_days: int | None = None):
    await admin_user(request)
    result = await _run_sync(list_demand_cases, get_db(), status=status,
                             executive=executive, commune=commune,
                             page=page, page_size=page_size,
                             min_priority=min_priority, stale_days=stale_days)
    return JSONResponse(_safe_json(result), headers={"Cache-Control": "private, no-store"})


@router.get("/api/crm/demand-sourcing/cases/{case_id}")
async def demand_sourcing_case_detail(request: Request, case_id: str):
    await admin_user(request)
    result = await _run_sync(get_demand_case, get_db(), case_id)
    if not result:
        raise HTTPException(status_code=404, detail="Caso no encontrado")
    return JSONResponse(_safe_json(result), headers={"Cache-Control": "private, no-store"})


@router.post("/api/crm/demand-sourcing/cases/{case_id}/search-now")
async def demand_sourcing_search_now(request: Request, case_id: str):
    user = await admin_user(request)
    _require_same_origin(request)
    try:
        result = await _run_sync(
            enqueue_search_job, get_db(), case_id=case_id,
            requested_by=_actor(user), source="crm", force_refresh=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(_safe_json({"job": result, "queued": True}), status_code=202,
                        headers={"Cache-Control": "no-store"})


@router.post("/api/crm/demand-sourcing/jobs/{job_id}/retry")
async def demand_sourcing_retry_job(request: Request, job_id: str):
    user = await admin_user(request)
    _require_same_origin(request)
    try:
        result = await _run_sync(retry_search_job, get_db(), job_id=job_id,
                                 actor=_actor(user))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(_safe_json({"job": result, "queued": result.get("status") == "queued"}),
                        status_code=202, headers={"Cache-Control": "no-store"})


@router.patch("/api/crm/demand-sourcing/cases/{case_id}/criteria")
async def demand_sourcing_update_criteria(request: Request, case_id: str):
    user = await admin_user(request)
    _require_same_origin(request)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Criterios inválidos") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Criterios inválidos")
    try:
        result = await _run_sync(update_case_criteria, get_db(), case_id=case_id,
                                 patch=body, actor=_actor(user))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(_safe_json(result), headers={"Cache-Control": "no-store"})


@router.post("/api/crm/demand-sourcing/candidates/{candidate_id}/review")
async def demand_sourcing_review_candidate(request: Request, candidate_id: str):
    user = await admin_user(request)
    _require_same_origin(request)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Decisión inválida") from exc
    try:
        result = await _run_sync(review_candidate, get_db(), candidate_id=candidate_id,
                                 action=str(body.get("action") or ""),
                                 actor=_actor(user), evidence=body.get("evidence") or {})
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(_safe_json(result), headers={"Cache-Control": "no-store"})


@router.post("/api/crm/demand-sourcing/candidates/{candidate_id}/deliver")
async def demand_sourcing_deliver_candidate(request: Request, candidate_id: str):
    user = await admin_user(request)
    _require_same_origin(request)
    try:
        result = await _run_sync(deliver_candidate_to_executive, get_db(),
                                 candidate_id=candidate_id, actor=_actor(user))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(_safe_json(result), status_code=201,
                        headers={"Cache-Control": "no-store"})


@router.post("/api/crm/demand-sourcing/cases/{case_id}/tracking")
async def demand_sourcing_tracking(request: Request, case_id: str):
    user = await admin_user(request)
    _require_same_origin(request)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Seguimiento inválido") from exc
    try:
        result = await _run_sync(record_case_tracking, get_db(), case_id=case_id,
                                 stage=str(body.get("stage") or ""),
                                 actor=_actor(user), note=str(body.get("note") or ""),
                                 candidate_id=body.get("candidate_id"))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(_safe_json(result), headers={"Cache-Control": "no-store"})


@router.get("/api/crm/demand-sourcing/alerts")
async def demand_sourcing_alerts(request: Request, limit: int = 30):
    user = await admin_user(request)
    result = await _run_sync(deliver_pending_admin_alerts, get_db(), limit=limit)
    return JSONResponse(_safe_json({"items": result, "recipient": _actor(user)}),
                        headers={"Cache-Control": "private, no-store"})


@router.post("/api/crm/demand-sourcing/alerts/ack")
async def demand_sourcing_alerts_ack(request: Request):
    user = await admin_user(request)
    _require_same_origin(request)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Acuse de alertas inválido") from exc
    keys = body.get("alert_keys") if isinstance(body, dict) else None
    if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
        raise HTTPException(status_code=400, detail="Acuse de alertas inválido")
    result = await _run_sync(ack_admin_alerts, get_db(), alert_keys=keys,
                             actor=_actor(user))
    return JSONResponse(_safe_json(result), headers={"Cache-Control": "no-store"})


@router.get("/api/crm/demand-sourcing/catalog-health")
async def demand_sourcing_catalog_health(request: Request):
    await admin_user(request)
    result = await _run_sync(catalog_health, get_db())
    return JSONResponse(_safe_json(result), headers={"Cache-Control": "private, no-store"})
