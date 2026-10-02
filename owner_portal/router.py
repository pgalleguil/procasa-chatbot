"""FastAPI router for the internal PROCASA SUCRE owner portal preview."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import Query

from config import Config
from chatbot.storage import get_db
from analytics.pricing_intelligence.time_utils import BUSINESS_TZ

from .security import require_internal_preview
from .campaign import (
    ACCESS_SOURCES,
    build_email_visual_landing_html,
    build_private_page_view,
    resolve_short_portal_request,
    verify_portal_request,
)
from .service import get_owner_portal_property_view, select_preview_property_code
from .email_artifacts import (
    EMAIL_ARTIFACT_COLLECTION,
    inject_owner_portal_controls,
    masked_html_parity,
    transform_sent_email_to_portal_html,
    verify_original_email_artifact,
)

router = APIRouter(tags=["owner-portal-preview"])
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
logger = logging.getLogger(__name__)


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


@router.get("/p/{access_key}", response_class=HTMLResponse, include_in_schema=False)
async def owner_campaign_short_landing(
    access_key: str,
    request: Request,
    source: str = Query(default="EMAIL"),
) -> HTMLResponse:
    """Resolve an opaque short key to its campaign-bound signed portal identity."""
    if (
        not set(request.query_params.keys()).issubset({"source"})
        or len(request.query_params.getlist("source")) > 1
        or source not in ACCESS_SOURCES
    ):
        raise HTTPException(status_code=404, detail="Página no disponible")
    db = get_db()
    verified = await run_in_threadpool(
        resolve_short_portal_request, db, access_key=access_key, source=source,
    )
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
    status = str(row.get("send_status") or "").strip().upper()
    if status in {"SENT", "DELIVERY_UNKNOWN"}:
        campaign_id = str(row.get("campaign_id") or "")
        property_code = str(row.get("property_code") or "")
        try:
            artifact = db[EMAIL_ARTIFACT_COLLECTION].find_one({
                "_id": f"{campaign_id}:{property_code}",
                "campaign_id": campaign_id,
                "property_code": property_code,
            })
            if not artifact:
                raise ValueError("historical_email_artifact_missing")
            if str(artifact.get("owner_email") or "").strip().casefold() != str(row.get("owner_email") or "").strip().casefold():
                raise ValueError("historical_email_artifact_identity_mismatch")
            attempts = row.get("send_attempts") if isinstance(row.get("send_attempts"), list) else []
            sent_attempt = next((
                item for item in reversed(attempts)
                if isinstance(item, dict)
                and str(item.get("smtp_status") or "").upper() == status
            ), {})
            expected_attempt_id = str(sent_attempt.get("attempt_id") or "").strip()
            if not expected_attempt_id or str(artifact.get("send_attempt_id") or "").strip() != expected_attempt_id:
                raise ValueError("historical_email_artifact_attempt_mismatch")
            expected_message_id = str(sent_attempt.get("message_id") or "").strip()
            if expected_message_id and str(artifact.get("message_id") or "").strip().casefold() != expected_message_id.casefold():
                raise ValueError("historical_email_artifact_message_id_mismatch")
            original_html = verify_original_email_artifact(artifact)
            rendered, _replacement_count = transform_sent_email_to_portal_html(original_html, view, source)
            if not masked_html_parity(original_html, rendered):
                raise ValueError("historical_email_non_href_diff")
        except (ValueError, TypeError):
            logger.exception(
                "owner_portal_historical_email_unavailable campaign_id=%s property_code=%s",
                campaign_id,
                property_code,
            )
            return HTMLResponse(
                "Página temporalmente no disponible",
                status_code=503,
                headers={
                    "Cache-Control": "private, no-store, max-age=0",
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                },
            )
        return HTMLResponse(
            rendered,
            headers={
                "Cache-Control": "private, no-store, max-age=0",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )

    # Stale rows never had an email artifact. Keep the safe renderer and its
    # existing authorization guard for those records only.
    if status not in {"SKIPPED_STALE_OR_MISMATCH"}:
        raise HTTPException(status_code=404, detail="Página no disponible")
    # The shared email renderer consumes frozen campaign values. The portal does
    # not replace stale-case report inputs with today's master-property data.
    landing_row = {
        **row,
        "property_type": view.get("property_type"),
        "commune": view.get("commune"),
    }
    rendered = await run_in_threadpool(
        build_email_visual_landing_html, landing_row, view, base_url=base_url,
    )
    # Stale rows have no sent-email artifact. Add only the portal's persistent
    # advisor affordance; the primary control remains visibly disabled and no
    # top authorization CTA is introduced.
    rendered = inject_owner_portal_controls(rendered, view, include_top_cta=False)
    return HTMLResponse(
        rendered,
        headers={
            "Cache-Control": "private, no-store, max-age=0",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )
