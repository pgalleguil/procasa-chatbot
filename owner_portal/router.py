"""FastAPI router for the internal PROCASA SUCRE owner portal preview."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi import Query

from config import Config
from chatbot.storage import get_db
from analytics.pricing_intelligence.time_utils import BUSINESS_TZ

from .security import require_internal_preview
from .campaign import (
    ACCESS_SOURCES,
    build_private_page_view,
    resolve_short_portal_request,
    verify_portal_request,
)
from .service import get_owner_portal_property_view, select_preview_property_code
from .email_artifacts import (
    EMAIL_ARTIFACT_COLLECTION,
    verify_original_email_artifact,
)
from .monthly import build_monthly_portal_view

router = APIRouter(tags=["owner-portal-preview"])
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
logger = logging.getLogger(__name__)


@router.get("/internal/owner-portal/appraisal-diagnostic/{property_code}", include_in_schema=False)
async def owner_portal_appraisal_diagnostic(
    property_code: str,
    _user=Depends(require_internal_preview),
):
    """TEMPORARY authenticated, read-only diagnostics for one exact appraisal PDF."""
    if not re.fullmatch(r"[0-9]+", property_code):
        raise HTTPException(status_code=404, detail="Not found")

    from campanas.private_report import (
        resolve_appraisal_analysis_cached,
        resolve_appraisal_document_cached,
    )

    resolved = await run_in_threadpool(resolve_appraisal_document_cached, property_code)
    resolver_status = str((resolved or {}).get("status") or "ERROR").upper()
    file_record = (resolved or {}).get("document")
    parsed = {}
    if resolver_status == "FOUND" and isinstance(file_record, dict) and file_record.get("id"):
        parsed = await run_in_threadpool(resolve_appraisal_analysis_cached, property_code, file_record)
    elif resolver_status == "FOUND":
        resolver_status = "ERROR"

    raw_sources = parsed.get("field_sources") if isinstance(parsed, dict) else {}
    labels = []
    if isinstance(raw_sources, dict):
        for value in raw_sources.values():
            label = re.sub(r"\s+", " ", str(value or "").split(":", 1)[0]).strip()
            label = re.sub(r"\s+", " ", label.splitlines()[0] if label else "").strip()
            if label and len(label) <= 64 and not re.search(r"\d", label) and label.casefold() not in {
                item.casefold() for item in labels
            }:
                labels.append(label)

    def safe_number(*keys):
        for key in keys:
            value = parsed.get(key) if isinstance(parsed, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        return None

    return {
        "property_code": property_code,
        "resolver_status": resolver_status,
        "pdf_text_extracted": bool(parsed.get("text_extracted")) if isinstance(parsed, dict) else False,
        "page_count": int(parsed.get("page_count") or 0) if isinstance(parsed, dict) else 0,
        "appraisal_value": safe_number("appraisal_value"),
        "estimated_mid_uf": safe_number("estimated_mid_uf", "appraisal_value"),
        "estimated_low_uf": safe_number("estimated_low_uf"),
        "estimated_high_uf": safe_number("estimated_high_uf"),
        "appraisal_uf_m2": safe_number("appraisal_uf_m2"),
        "appraisal_date": parsed.get("appraisal_date") if isinstance(parsed, dict) else None,
        "methodology": parsed.get("methodology") if isinstance(parsed, dict) else None,
        "matched_labels": labels,
        "extraction_status": str(parsed.get("extraction_status") or "NOT_RUN") if isinstance(parsed, dict) else "NOT_RUN",
        "extraction_confidence": str(parsed.get("extraction_confidence") or "LOW") if isinstance(parsed, dict) else "LOW",
    }


@router.get("/owner-portal/executive-whatsapp", include_in_schema=False)
async def executive_whatsapp_click(request: Request, token: str = Query(default="")):
    """A signed click intent only; never an advisor request or authorization."""
    from campanas.owner_campaign_live_events import decode_live_token, persist_owner_whatsapp_click
    from .campaign import LEDGER_COLLECTION, short_key_for_token_hash
    from .executive import resolve_executive_contact, whatsapp_destination
    from .monthly import OWNER_PROPERTY_PORTAL_COLLECTION, owner_property_portal_id, _select_monthly_snapshot

    if set(request.query_params) != {"token"} or len(request.query_params.getlist("token")) != 1:
        raise HTTPException(404, "Página no disponible")
    claims = decode_live_token(token)
    if (not claims or not all(claims.get(key) for key in ("campaign_id", "property_code", "recipient"))
            or claims.get("action") != "executive_whatsapp_clicked"
            or claims.get("interaction_surface") != "OWNER_PORTAL"
            or claims.get("cta_placement") not in {"TOP", "STICKY"}
            or claims.get("source") not in ACCESS_SOURCES):
        raise HTTPException(404, "Página no disponible")
    db = get_db()
    row = await run_in_threadpool(db[LEDGER_COLLECTION].find_one, {
        "_id": f"{claims['campaign_id']}:{claims['property_code']}",
        "campaign_id": claims["campaign_id"], "property_code": claims["property_code"],
    })
    if not row:
        raise HTTPException(404, "Página no disponible")
    access_hash = (row.get("portal_access") or {}).get("token_hash")
    if not access_hash:
        raise HTTPException(404, "Página no disponible")
    verified = await run_in_threadpool(resolve_short_portal_request, db,
        access_key=short_key_for_token_hash(access_hash), source=claims["source"])
    if not verified or verified[0].get("_id") != row.get("_id") or verified[1].get("recipient") != claims.get("recipient") or verified[1].get("exp") != claims.get("exp"):
        raise HTTPException(404, "Página no disponible")
    def destination():
        code = str(row["property_code"])
        key = owner_property_portal_id(code, row["owner_email"])
        record = db[OWNER_PROPERTY_PORTAL_COLLECTION].find_one({"_id": key, "owner_key": key, "property_code": code})
        monthly = _select_monthly_snapshot(record, code) or {}
        contact = resolve_executive_contact(db, row, monthly, str(row.get("executive_name") or row.get("executive") or ""))
        url = whatsapp_destination(contact, code)
        report_period = str(monthly.get("period") or "")
        snapshot_hash = str(monthly.get("snapshot_hash") or monthly.get("content_sha256") or "")
        return contact, url, report_period, snapshot_hash
    contact, url, report_period, snapshot_hash = await run_in_threadpool(destination)
    if not url:
        raise HTTPException(404, "Página no disponible")
    qa_mode = bool(row.get("qa_mode") or row.get("test_mode") or claims.get("qa_mode"))
    event_details = {
        "executive_name": contact["name"],
        "executive_email": contact["email"],
        "report_period": report_period or None,
        "qa_mode": qa_mode,
    }
    if snapshot_hash:
        event_details["snapshot_hash"] = snapshot_hash
    try:
        await run_in_threadpool(persist_owner_whatsapp_click, db, claims, details=event_details)
    except LookupError as exc:
        raise HTTPException(404, "Página no disponible") from exc
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})


def _render(request: Request, view: dict) -> HTMLResponse:
    return _templates.TemplateResponse(
        request,
        "owner_portal_preview.html",
        {"request": request, "view": view},
    )


def _verified_sent_email_html(db, row: dict, status: str) -> str | None:
    """Return immutable sent HTML only after ledger/artifact identity checks."""
    campaign_id = str(row.get("campaign_id") or "")
    property_code = str(row.get("property_code") or "")
    artifact = db[EMAIL_ARTIFACT_COLLECTION].find_one({
        "_id": f"{campaign_id}:{property_code}",
        "campaign_id": campaign_id,
        "property_code": property_code,
    })
    if not artifact:
        return None
    if str(artifact.get("owner_email") or "").strip().casefold() != str(row.get("owner_email") or "").strip().casefold():
        raise ValueError("historical_email_artifact_identity_mismatch")
    attempts = row.get("send_attempts") if isinstance(row.get("send_attempts"), list) else []
    sent_attempt = next((
        item for item in reversed(attempts)
        if isinstance(item, dict) and str(item.get("smtp_status") or "").upper() == status
    ), {})
    expected_attempt_id = str(sent_attempt.get("attempt_id") or "").strip()
    if not expected_attempt_id or str(artifact.get("send_attempt_id") or "").strip() != expected_attempt_id:
        raise ValueError("historical_email_artifact_attempt_mismatch")
    expected_message_id = str(sent_attempt.get("message_id") or "").strip()
    if expected_message_id and str(artifact.get("message_id") or "").strip().casefold() != expected_message_id.casefold():
        raise ValueError("historical_email_artifact_message_id_mismatch")
    return verify_original_email_artifact(artifact)


def _monthly_response(request: Request, db, row: dict, view: dict, *, email_html: str | None = None) -> HTMLResponse:
    report_view = build_monthly_portal_view(db, row, view, email_html=email_html)
    return _templates.TemplateResponse(
        request,
        "owner_campaign_monthly_portal.html",
        {"request": request, "view": report_view},
        headers={
            "Cache-Control": "private, no-store, max-age=0",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
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
    """Serve the private monthly report only for a registered p1 link."""
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
    status = str(row.get("send_status") or "").strip().upper()
    email_html = None
    if status in {"SENT", "DELIVERY_UNKNOWN"}:
        try:
            email_html = _verified_sent_email_html(db, row, status)
        except (ValueError, TypeError):
            logger.exception("owner_portal_historical_email_unavailable campaign_id=%s property_code=%s", row.get("campaign_id"), row.get("property_code"))
            email_html = None
    elif status != "SKIPPED_STALE_OR_MISMATCH":
        raise HTTPException(status_code=404, detail="Página no disponible")
    return _monthly_response(request, db, row, view, email_html=email_html)


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
    # Sent rows retain immutable original HTML as evidence; the owner portal
    # uses a separate monthly renderer. Stale rows remain advisor-only.
    status = str(row.get("send_status") or "").strip().upper()
    email_html = None
    if status in {"SENT", "DELIVERY_UNKNOWN"}:
        try:
            email_html = _verified_sent_email_html(db, row, status)
        except (ValueError, TypeError):
            logger.exception(
                "owner_portal_historical_email_unavailable campaign_id=%s property_code=%s",
                row.get("campaign_id"), row.get("property_code"),
            )
            email_html = None
    if status not in {"SKIPPED_STALE_OR_MISMATCH"}:
        if status not in {"SENT", "DELIVERY_UNKNOWN"}:
            raise HTTPException(status_code=404, detail="Página no disponible")
    return _monthly_response(request, db, row, view, email_html=email_html)
