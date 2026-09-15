"""FastAPI router for the internal PROCASA SUCRE owner portal preview."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from chatbot.storage import get_db
from analytics.pricing_intelligence.time_utils import BUSINESS_TZ

from .security import require_internal_preview
from .service import get_owner_portal_property_view, select_preview_property_code

router = APIRouter(tags=["owner-portal-preview"])
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _render(request: Request, view: dict) -> HTMLResponse:
    return _templates.TemplateResponse(
        request,
        "owner_portal_preview.html",
        {"request": request, "view": view},
    )


@router.get("/owner-portal-preview", response_class=HTMLResponse, include_in_schema=False)
async def owner_portal_preview(
    request: Request,
    _user=Depends(require_internal_preview),
) -> HTMLResponse:
    db = get_db()
    as_of = datetime.now(BUSINESS_TZ)
    code = await run_in_threadpool(select_preview_property_code, db)
    if not code:
        raise HTTPException(status_code=404, detail="No eligible PROCASA SUCRE property")
    view = await run_in_threadpool(get_owner_portal_property_view, db, code, as_of)
    if view is None:
        raise HTTPException(status_code=404, detail="Property is not available in PROCASA SUCRE scope")
    return _render(request, view.to_dict())


@router.get("/owner-portal-preview/{property_code}", response_class=HTMLResponse, include_in_schema=False)
async def owner_portal_preview_for_property(
    property_code: str,
    request: Request,
    _user=Depends(require_internal_preview),
) -> HTMLResponse:
    as_of = datetime.now(BUSINESS_TZ)
    view = await run_in_threadpool(get_owner_portal_property_view, get_db(), property_code, as_of)
    if view is None:
        raise HTTPException(status_code=404, detail="Property is not available in PROCASA SUCRE scope")
    return _render(request, view.to_dict())
