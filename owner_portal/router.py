"""FastAPI router for the internal PROCASA SUCRE owner portal preview."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from chatbot.storage import get_db

from .security import require_internal_preview
from .service import build_owner_portal_view, select_preview_property_code

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
    code = await run_in_threadpool(select_preview_property_code, db)
    if not code:
        raise HTTPException(status_code=404, detail="No eligible PROCASA SUCRE property")
    view = await run_in_threadpool(build_owner_portal_view, db, code)
    return _render(request, view)


@router.get("/owner-portal-preview/{property_code}", response_class=HTMLResponse, include_in_schema=False)
async def owner_portal_preview_for_property(
    property_code: str,
    request: Request,
    _user=Depends(require_internal_preview),
) -> HTMLResponse:
    view = await run_in_threadpool(build_owner_portal_view, get_db(), property_code)
    if view.get("status") != "ok":
        raise HTTPException(status_code=404, detail="Property is not available in PROCASA SUCRE scope")
    return _render(request, view)
