"""FastAPI router for the internal PROCASA SUCRE owner portal preview."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import Query

from config import Config
from chatbot.storage import get_db
from analytics.pricing_intelligence.time_utils import BUSINESS_TZ

from .security import require_internal_preview
from .campaign import ACCESS_SOURCES, build_private_page_view, verify_portal_request
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


@router.get("/ajuste/{property_code}", response_class=HTMLResponse, include_in_schema=False)
async def owner_campaign_private_property(
    property_code: str,
    request: Request,
    token: str = Query(default=""),
    source: str = Query(default="EMAIL"),
) -> HTMLResponse:
    """Serve the campaign's frozen recommendation only for a registered p1 link."""
    if (
        not set(request.query_params.keys()).issubset({"token", "source"})
        or "token" not in request.query_params
        or len(request.query_params.getlist("token")) != 1
        or len(request.query_params.getlist("source")) > 1
        or source not in ACCESS_SOURCES
    ):
        raise HTTPException(status_code=404, detail="Página no disponible")
    db = get_db()
    verified = await run_in_threadpool(verify_portal_request, db, property_code=property_code, token=token)
    if verified is None:
        raise HTTPException(status_code=404, detail="Página no disponible")
    row, claims = verified
    from campanas.owner_campaign_live_events import persist_live_event

    try:
        persist_live_event(
            db, {**claims, "source": source},
            event="portal_opened", action="portal_opened",
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Página no disponible") from exc
    base_url = (Config.CRM_BASE_URL or "https://www.procasa.cl").rstrip("/")
    view = build_private_page_view(db, row, claims, base_url=base_url, source=source)
    response = _templates.TemplateResponse(
        request,
        "owner_campaign_private.html",
        {"request": request, "view": view},
        headers={
            "Cache-Control": "private, no-store, max-age=0",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )
    return response
