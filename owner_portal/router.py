"""FastAPI router for the internal PROCASA SUCRE owner portal preview."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime
import logging
import re
from uuid import uuid4

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


@router.get("/owner-portal/executive-whatsapp", include_in_schema=False)
async def executive_whatsapp_click(request: Request, token: str = Query(default="")):
    """A signed click intent only; never an advisor request or authorization."""
    from campanas.owner_campaign_live_events import decode_live_token, persist_owner_whatsapp_click
    from .campaign import LEDGER_COLLECTION, short_key_for_token_hash
    from .executive import resolve_executive_contact, whatsapp_destination
    from .monthly import OWNER_PROPERTY_PORTAL_COLLECTION, owner_property_portal_id, _select_monthly_snapshot, _as_period

    if (not set(request.query_params).issubset({"token", "owner_portal_context_token", "owner_portal_session_id", "owner_portal_client_event_id"})
            or len(request.query_params.getlist("token")) != 1
            or len(request.query_params.getlist("owner_portal_context_token")) > 1
            or len(request.query_params.getlist("owner_portal_session_id")) > 1
            or len(request.query_params.getlist("owner_portal_client_event_id")) > 1):
        raise HTTPException(404, "Página no disponible")
    claims = decode_live_token(token)
    context_token = request.query_params.get("owner_portal_context_token", "")
    client_session_id = request.query_params.get("owner_portal_session_id", "")
    client_event_id = request.query_params.get("owner_portal_client_event_id", "")
    if client_session_id and not re.fullmatch(r"[0-9a-fA-F-]{36}", client_session_id):
        raise HTTPException(404, "Página no disponible")
    if client_event_id and not re.fullmatch(r"[0-9a-fA-F-]{36}", client_event_id):
        raise HTTPException(404, "Página no disponible")
    context_claims = decode_live_token(context_token) if context_token else None
    if (not claims or not all(claims.get(key) for key in ("campaign_id", "property_code", "recipient"))
            or claims.get("action") != "executive_whatsapp_clicked"
            or claims.get("interaction_surface") != "OWNER_PORTAL"
            or claims.get("cta_placement") not in {"TOP", "STICKY"}
            or claims.get("source") not in ACCESS_SOURCES
            or (context_token and (
                not context_claims or context_claims.get("action") != "owner_portal_interaction"
                or context_claims.get("interaction_surface") != "OWNER_PORTAL"
                or any(context_claims.get(key) != claims.get(key) for key in ("campaign_id", "property_code", "recipient", "source"))
            ))):
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
        import hashlib
        import json
        code = str(row["property_code"])
        key = owner_property_portal_id(code, row["owner_email"])
        record = db[OWNER_PROPERTY_PORTAL_COLLECTION].find_one({"_id": key, "owner_key": key, "property_code": code})
        monthly = _select_monthly_snapshot(record, code) or {}
        contact = resolve_executive_contact(db, row, monthly, str(row.get("executive_name") or row.get("executive") or ""))
        url = whatsapp_destination(contact, code)
        campaign_snapshot = row.get("campaign_snapshot") if isinstance(row.get("campaign_snapshot"), dict) else {}
        report_period = _as_period(monthly.get("period") or monthly.get("generated_at") or campaign_snapshot.get("prepared_at")) or datetime.now(BUSINESS_TZ).strftime("%Y-%m")
        snapshot_hash = str(monthly.get("snapshot_hash") or monthly.get("content_sha256") or campaign_snapshot.get("snapshot_hash") or campaign_snapshot.get("content_sha256") or "")
        if not snapshot_hash:
            snapshot_hash = hashlib.sha256(json.dumps(dict(monthly or campaign_snapshot), sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        return contact, url, report_period, snapshot_hash
    contact, url, report_period, snapshot_hash = await run_in_threadpool(destination)
    if not url:
        raise HTTPException(404, "Página no disponible")
    qa_mode = bool(row.get("qa_mode") or row.get("test_mode") or claims.get("qa_mode"))
    event_details = {
        "executive_name": contact["name"],
        "executive_email": contact["email"],
        "report_period": (context_claims or {}).get("report_period") or report_period,
        "session_id": client_session_id or (context_claims or {}).get("session_id") or str(uuid4()),
        "client_event_id": client_event_id or str(uuid4()),
        "section_id": "executive_contact",
        "control_id": "whatsapp_sticky" if claims.get("cta_placement") == "STICKY" else "whatsapp_top",
        "qa_mode": qa_mode,
    }
    event_details["snapshot_hash"] = (context_claims or {}).get("snapshot_hash") or snapshot_hash
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
    from campanas.owner_campaign_live_events import (
        issue_live_token,
    )
    access = row.get("portal_access") if isinstance(row.get("portal_access"), dict) else {}
    expires_at = access.get("expires_at")
    session_id = str(uuid4())
    try:
        if isinstance(expires_at, datetime):
            token = issue_live_token(
                campaign_id=str(row["campaign_id"]), property_code=str(row["property_code"]),
                action="owner_portal_interaction", recipient=str(row.get("owner_email") or ""),
                expires_at=int(expires_at.timestamp()), source=view.get("source"),
                interaction_surface="OWNER_PORTAL", event_id=uuid4().hex,
                report_period=str(report_view.get("report_period") or ""),
                snapshot_hash=str(report_view.get("snapshot_hash") or ""),
                session_id=session_id,
            )
            report_view["interaction_token"] = token
            report_view["portal_session_id"] = session_id
    except (ValueError, LookupError, KeyError, TypeError):
        logger.exception("owner_portal_telemetry_setup_failed campaign_id=%s property_code=%s", row.get("campaign_id"), row.get("property_code"))
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


@router.post("/owner-portal/interaction", include_in_schema=False)
async def owner_portal_interaction(request: Request):
    """Accept a narrow set of signed, in-page owner portal interactions."""
    from campanas.owner_campaign_live_events import (
        OWNER_PORTAL_CONTROLS, OWNER_PORTAL_EVENTS, OWNER_PORTAL_SECTIONS,
        OWNER_PUBLICATION_PORTALS, decode_live_token, persist_owner_portal_interaction,
    )
    from .campaign import LEDGER_COLLECTION

    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Interacción inválida") from exc
    allowed = {
        "token", "event", "client_event_id", "session_id", "section_id", "control_id",
        "previous_state", "target_state", "selected_adjustment_pct", "external_portal", "expanded",
    }
    if not isinstance(data, dict) or set(data) - allowed:
        raise HTTPException(status_code=400, detail="Interacción inválida")
    required = {"token", "event", "client_event_id", "session_id", "section_id", "control_id"}
    if not required.issubset(data):
        raise HTTPException(status_code=400, detail="Interacción incompleta")
    if not isinstance(data["token"], str) or len(data["token"]) > 8192:
        raise HTTPException(status_code=400, detail="Token inválido")
    claims = decode_live_token(str(data["token"]))
    if (not claims or claims.get("action") != "owner_portal_interaction"
            or claims.get("interaction_surface") != "OWNER_PORTAL"
            or claims.get("source") not in ACCESS_SOURCES):
        raise HTTPException(status_code=404, detail="Interacción no disponible")
    if any(not isinstance(data.get(key), str) or len(data[key]) > 80 for key in ("event", "section_id", "control_id")):
        raise HTTPException(status_code=400, detail="Interacción inválida")
    if data["event"] not in OWNER_PORTAL_EVENTS:
        raise HTTPException(status_code=400, detail="Evento no permitido")
    event = data["event"]
    section, control = data["section_id"], data["control_id"]
    if section not in OWNER_PORTAL_SECTIONS or control not in OWNER_PORTAL_CONTROLS:
        raise HTTPException(status_code=400, detail="Control no permitido")
    if not isinstance(data["client_event_id"], str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", data["client_event_id"]):
        raise HTTPException(status_code=400, detail="Identificador inválido")
    if not isinstance(data["session_id"], str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", data["session_id"]):
        raise HTTPException(status_code=400, detail="Sesión inválida")
    normalized = {
        "event": event, "client_event_id": data["client_event_id"],
        "session_id": data["session_id"], "section_id": section, "control_id": control,
    }
    if event in {"section_expanded", "section_collapsed"}:
        valid = {
            "publication_details": "publication_disclosure",
            "appraisal_details": "appraisal_details_toggle",
            "communal_market_details": "communal_market_details_toggle",
            "market_context_details": "market_context_toggle",
            "recommendation_details": "recommendation_details_toggle",
        }
        if valid.get(section) != control or set(data) != required | {"expanded"}:
            raise HTTPException(status_code=400, detail="Expansión no permitida")
        if not isinstance(data["expanded"], bool) or data["expanded"] != (event == "section_expanded"):
            raise HTTPException(status_code=400, detail="Estado de expansión inválido")
        normalized["expanded"] = data["expanded"]
    elif event == "price_simulation_changed":
        if section != "price_simulation" or control not in {"price_simulation_current", "price_simulation_adjusted"}:
            raise HTTPException(status_code=400, detail="Simulación no permitida")
        if set(data) != required | {"previous_state", "target_state", "selected_adjustment_pct"}:
            raise HTTPException(status_code=400, detail="Simulación incompleta")
        if (not isinstance(data["previous_state"], str) or not isinstance(data["target_state"], str)
                or len(data["previous_state"]) > 16 or len(data["target_state"]) > 16
                or (data["previous_state"], data["target_state"]) not in {("current", "adjusted"), ("adjusted", "current")}):
            raise HTTPException(status_code=400, detail="Transición no permitida")
        try:
            pct = float(data["selected_adjustment_pct"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Ajuste inválido")
        if not -100 <= pct <= 100:
            raise HTTPException(status_code=400, detail="Ajuste inválido")
        normalized.update(previous_state=data["previous_state"], target_state=data["target_state"], selected_adjustment_pct=pct)
    else:
        if section != "commercial_activity_publications" or control != "publication_link" or set(data) != required | {"external_portal"}:
            raise HTTPException(status_code=400, detail="Publicación no permitida")
        portal = data["external_portal"]
        if not isinstance(portal, str) or len(portal) > 48 or portal not in OWNER_PUBLICATION_PORTALS:
            raise HTTPException(status_code=400, detail="Portal no permitido")
        normalized["external_portal"] = portal
    db = get_db()
    row = await run_in_threadpool(db[LEDGER_COLLECTION].find_one, {
        "_id": f"{claims['campaign_id']}:{claims['property_code']}",
        "campaign_id": claims["campaign_id"], "property_code": claims["property_code"],
        "owner_email": claims["recipient"],
    })
    if not row:
        raise HTTPException(status_code=404, detail="Interacción no disponible")
    await run_in_threadpool(persist_owner_portal_interaction, db, claims, normalized)
    return {"status": "ok"}


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
