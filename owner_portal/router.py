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
from .service import (
    get_owner_portal_property_view,
    select_owner_intelligence_property_code,
    select_preview_property_code,
)

router = APIRouter(tags=["owner-portal-preview"])
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _render(request: Request, view: dict) -> HTMLResponse:
    return _templates.TemplateResponse(
        request,
        "owner_portal_preview.html",
        {"request": request, "view": view},
    )


def _render_concept(request: Request, template_name: str, view: dict) -> HTMLResponse:
    return _templates.TemplateResponse(
        request,
        template_name,
        {"request": request, "view": view},
    )


def _convergent_payload(view: dict) -> dict:
    """Add small presentation-only labels without changing the shared DTO."""

    payload = dict(view)
    position = view.get("positioning") or {}
    current = view.get("current_price") or {}
    price_uf = current.get("uf")
    p10 = position.get("p10_uf")
    p90 = position.get("p90_uf")
    if price_uf is not None and p10 is not None and p90 is not None and p90 > p10:
        percentile = round(10 + 80 * max(0, min(1, (price_uf - p10) / (p90 - p10))))
        payload["owner_position_rank"] = max(1, min(9, round(percentile / 10)))
    else:
        payload["owner_position_rank"] = None

    local = view.get("local_context") or {}
    built_area = view.get("built_area_m2") or view.get("land_area_m2")
    payload["property_uf_m2"] = (
        round(price_uf / built_area, 2)
        if price_uf is not None and built_area
        else None
    )
    indicators = []
    if local.get("price_variation_12m_pct") is not None:
        indicators.append({"label": "Variación 12 meses", "value": local["price_variation_12m_pct"], "unit": "%"})
    if local.get("active_listings") is not None:
        indicators.append({"label": "Publicaciones observadas", "value": local["active_listings"], "unit": "avisos"})
    payload["visible_market_indicators"] = indicators[:4]
    quality = view.get("data_quality") or {}
    previous = view.get("previous_price") or {}
    payload["has_last_update"] = bool(
        quality.get("price_history_available")
        and view.get("last_price_change_at")
        and (previous.get("uf") is not None or previous.get("clp") is not None)
    )
    if payload["has_last_update"] and price_uf is not None and previous.get("uf") is not None:
        delta = round(price_uf - previous["uf"], 2)
        payload["last_update_delta_uf"] = delta
        payload["last_update_delta_pct"] = round((delta / previous["uf"]) * 100, 1) if previous["uf"] else None
    else:
        payload["last_update_delta_uf"] = None
        payload["last_update_delta_pct"] = None

    label = str(position.get("label") or "")
    if "Sobre" in label:
        payload["position_descriptor"] = "Sobre el rango central"
    elif "Bajo" in label:
        payload["position_descriptor"] = "Bajo el rango central"
    elif position:
        payload["position_descriptor"] = "Dentro del rango central"
    else:
        payload["position_descriptor"] = "No disponible"

    median_uf = position.get("median_uf") if position else None
    payload["market_delta_pct"] = (
        round(((price_uf - median_uf) / median_uf) * 100, 1)
        if price_uf is not None and median_uf
        else None
    )

    rank = payload.get("owner_position_rank")
    if "Sobre" in label:
        payload["position_translation"] = (
            "Su precio publicado se encuentra por encima de aproximadamente 7 de cada 10 "
            "propiedades similares observadas."
        )
    elif "Bajo" in label:
        payload["position_translation"] = (
            "Su precio publicado se encuentra por debajo de aproximadamente 3 de cada 10 "
            "propiedades similares observadas."
        )
    elif rank is not None and rank >= 8:
        payload["position_translation"] = (
            f"Su precio publicado se encuentra por encima de aproximadamente {rank} de cada 10 "
            "propiedades similares observadas."
        )
    elif rank is not None and rank <= 3:
        payload["position_translation"] = (
            f"Su precio publicado se encuentra por debajo de aproximadamente {10 - rank} de cada 10 "
            "propiedades similares observadas."
        )
    elif rank is not None:
        payload["position_translation"] = (
            "Su precio publicado se encuentra alrededor de la zona media de la muestra comparable."
        )
    else:
        payload["position_translation"] = "No hay una lectura posicional suficiente para esta muestra."

    recent_activity = [
        point for point in (view.get("activity_series") or [])
        if point.get("count", 0) > 0
    ][-5:]
    inquiries_30d = int(view.get("inquiries_previous_30d") or 0)
    if inquiries_30d > 0:
        if recent_activity:
            peak = max(recent_activity, key=lambda point: (point.get("count", 0), point.get("period", "")))
            payload["commercial_insight"] = (
                f"Durante los últimos 30 días se registraron {inquiries_30d} consultas vinculadas a su propiedad. "
                f"La mayor concentración reciente se observó en la semana del {peak.get('label') or peak.get('period')}."
            )
        else:
            payload["commercial_insight"] = (
                f"Durante los últimos 30 días se registraron {inquiries_30d} consultas vinculadas a su propiedad."
            )
    else:
        payload["commercial_insight"] = (
            "No se registraron consultas vinculadas durante los últimos 30 días. "
            "El contexto de mercado completa la lectura disponible para su propiedad."
        )
    return payload


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


async def _owner_portal_concept(
    request: Request,
    property_code: str,
    template_name: str,
) -> HTMLResponse:
    as_of = datetime.now(BUSINESS_TZ)
    view = await run_in_threadpool(get_owner_portal_property_view, get_db(), property_code, as_of)
    if view is None:
        raise HTTPException(status_code=404, detail="Property is not available in PROCASA SUCRE scope")
    return _render_concept(request, template_name, view.to_dict())


@router.get("/owner-portal-concepts/a/{property_code}", response_class=HTMLResponse, include_in_schema=False)
async def owner_portal_concept_a(
    property_code: str,
    request: Request,
    _user=Depends(require_internal_preview),
) -> HTMLResponse:
    return await _owner_portal_concept(request, property_code, "owner_portal_concept_a.html")


@router.get("/owner-portal-concepts/b/{property_code}", response_class=HTMLResponse, include_in_schema=False)
async def owner_portal_concept_b(
    property_code: str,
    request: Request,
    _user=Depends(require_internal_preview),
) -> HTMLResponse:
    return await _owner_portal_concept(request, property_code, "owner_portal_concept_b.html")


@router.get("/owner-portal-concepts/c/{property_code}", response_class=HTMLResponse, include_in_schema=False)
async def owner_portal_concept_c(
    property_code: str,
    request: Request,
    _user=Depends(require_internal_preview),
) -> HTMLResponse:
    return await _owner_portal_concept(request, property_code, "owner_portal_concept_c.html")


async def _owner_portal_convergent(
    request: Request,
    property_code: str,
) -> HTMLResponse:
    as_of = datetime.now(BUSINESS_TZ)
    view = await run_in_threadpool(get_owner_portal_property_view, get_db(), property_code, as_of)
    if view is None:
        raise HTTPException(status_code=404, detail="Property is not available in PROCASA SUCRE scope")
    return _templates.TemplateResponse(
        request,
        "owner_portal_convergent.html",
        {"request": request, "view": _convergent_payload(view.to_dict())},
    )


@router.get("/owner-portal-convergent", response_class=HTMLResponse, include_in_schema=False)
async def owner_portal_convergent(
    request: Request,
    _user=Depends(require_internal_preview),
) -> HTMLResponse:
    as_of = datetime.now(BUSINESS_TZ)
    db = get_db()
    code = await run_in_threadpool(select_owner_intelligence_property_code, db, as_of)
    if not code:
        raise HTTPException(status_code=404, detail="No eligible SUCRE property with recent verified activity")
    return await _owner_portal_convergent(request, code)


@router.get("/owner-portal-convergent/{property_code}", response_class=HTMLResponse, include_in_schema=False)
async def owner_portal_convergent_for_property(
    property_code: str,
    request: Request,
    _user=Depends(require_internal_preview),
) -> HTMLResponse:
    return await _owner_portal_convergent(request, property_code)
