"""Compatibility entry point for the canonical, test-only owner campaign flow."""

from __future__ import annotations

import math
import re
import time
import unicodedata
from datetime import datetime, timezone
from html import escape
from typing import Any, Mapping
from urllib.parse import quote

from fastapi.responses import HTMLResponse

from config import Config
from .test_mode import (
    TEST_CAMPAIGN_ID,
    TEST_CAMPAIGN_VERSION,
    TEST_RECIPIENT,
    ACCEPT_PRICE_ACTION,
    ADVISOR_ACTION,
    REPORT_ACTION,
    _event_datetime,
    issue_campaign_test_token,
    persist_test_event,
    test_mode_enabled as _test_mode_enabled,
    verify_campaign_test_token,
)


ALLOWED_ACTIONS = frozenset({REPORT_ACTION, ACCEPT_PRICE_ACTION, ADVISOR_ACTION})
PROPERTY_COLLECTION = "universo_cartera_prop360"
LEDGER_COLLECTION = "ajuste_precio"
PROPERTY_CODE_RE = re.compile(r"^[0-9]{1,32}$")


class OwnerCampaignTestError(ValueError):
    """A test action did not meet the signed test-only authorization contract."""


def _campaign_test_page(
    title: str,
    content: str,
    raw_content: bool = False,
    *,
    eyebrow: str = "PROCASA · ASESORÍA COMERCIAL",
    back_url: str = "javascript:history.back()",
    back_label: str = "VOLVER AL INFORME",
) -> str:
    """Shared premium responsive layout for all owner action pages."""
    safe_title = escape(str(title or "PROCASA"))
    safe_eyebrow = escape(str(eyebrow or "PROCASA"))
    safe_content = str(content or "") if raw_content else f"<p>{escape(str(content or ''))}</p>"
    safe_back_url = escape(str(back_url or "javascript:history.back()"), quote=True)
    safe_back_label = escape(str(back_label or "VOLVER AL INFORME"))
    logo_url = escape((Config.CRM_BASE_URL or "https://procasa.cl").rstrip("/") + "/static/logo.png", quote=True)
    return (
        "<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>PROCASA | {safe_title}</title>"
        "<style>*{box-sizing:border-box}body{margin:0;padding:36px 18px;background:#f3f2fa;"
        "font-family:Arial,Helvetica,sans-serif;color:#25224a}.owner_campaign_action_layout{max-width:780px;"
        "margin:5vh auto;padding:42px 46px;background:#fff;border:1px solid #e7e6f0;border-radius:22px;"
        "box-shadow:0 14px 42px rgba(34,29,89,.10)}.brand-row{padding-bottom:26px;margin-bottom:30px;"
        "border-bottom:1px solid #eeedf5}.brand{display:block;width:116px;height:auto;max-height:48px;object-fit:contain}"
        ".eyebrow{margin:0 0 12px;color:#6252d0;font-size:11px;font-weight:700;letter-spacing:.13em}"
        "h1{margin:0 0 15px;color:#17175f;font-size:32px;line-height:1.18;letter-spacing:-.025em}"
        ".copy{font-size:16px;line-height:1.65;color:#555d7b}.copy p{margin:0 0 18px}"
        ".summary{display:flex;align-items:stretch;gap:14px;margin:26px 0;padding:17px;"
        "background:#f8f7ff;border:1px solid #e8e6f6;border-radius:16px}.summary-cell{flex:1;min-width:0;padding:8px 12px}"
        ".summary-cell.new{background:#eceaff;border-radius:12px}.summary-label{display:block;margin-bottom:8px;"
        "color:#747b99;font-size:10px;font-weight:700;letter-spacing:.1em}.summary-value{display:block;"
        "color:#17175f;font-size:23px;font-weight:700;line-height:1.15}.summary-cell.new .summary-value{"
        "color:#5546c6;font-size:27px}.summary-arrow{align-self:center;color:#8176dc;font-size:22px}"
        ".summary-adjustment{align-self:center;min-width:76px;text-align:center;color:#555d7b;font-size:12px;line-height:1.5}"
        ".notice{margin:20px 0;padding:16px 18px;border-left:3px solid #a69cef;background:#f8f7ff;"
        "border-radius:4px 12px 12px 4px;color:#62698a;font-size:13px;line-height:1.6}"
        ".next{margin:27px 0 0;padding:20px 22px;background:#f8f7ff;border:1px solid #eeedf6;"
        "border-radius:15px;font-size:14px;line-height:1.55}.next-title{display:block;margin-bottom:14px;"
        "color:#17175f;font-size:11px;font-weight:700;letter-spacing:.1em}.step{display:flex;gap:12px;margin:11px 0;color:#555d7b}"
        ".step-no{flex:0 0 30px;color:#6252d0;font-size:12px;font-weight:700}.contact{margin-top:20px;"
        "padding:19px 21px;background:#fff;border:1px solid #e7e6f0;border-radius:15px;font-size:14px;line-height:1.7}"
        ".contact-title{display:block;margin-bottom:8px;color:#17175f;font-size:11px;font-weight:700;letter-spacing:.1em}"
        ".contact a{color:#5144bd;text-decoration:none;overflow-wrap:anywhere}.actions{display:flex;align-items:center;"
        "gap:18px;margin-top:27px}.button{display:inline-flex;justify-content:center;align-items:center;min-height:48px;"
        "padding:0 22px;border:0;border-radius:999px;background:#17175f;color:#fff!important;text-decoration:none;"
        "font-size:12px;font-weight:700;letter-spacing:.045em;cursor:pointer}.button-secondary{display:inline-block;"
        "padding:12px 4px;color:#62698a;text-decoration:none;font-size:13px;font-weight:600}.check{display:flex;"
        "align-items:center;justify-content:center;width:52px;height:52px;margin:0 0 20px;border-radius:50%;"
        "background:#eeecff;color:#5546c6;font-size:27px;font-weight:700}.badge-icon{display:inline-flex;"
        "align-items:center;justify-content:center;width:52px;height:52px;margin:0 0 20px;border-radius:50%;"
        "background:#eeecff;color:#5546c6;font-size:22px}"
        "@media(max-width:600px){body{padding:14px 10px}.owner_campaign_action_layout{margin:2vh auto;padding:25px 19px;border-radius:18px}"
        ".brand-row{padding-bottom:20px;margin-bottom:23px}.brand{width:100px}h1{font-size:27px}.copy{font-size:15px}"
        ".summary{gap:5px;padding:10px;margin:21px 0}.summary-cell{padding:10px 7px}.summary-label{font-size:9px}"
        ".summary-value{font-size:18px}.summary-cell.new .summary-value{font-size:21px}.summary-arrow{font-size:17px}"
        ".summary-adjustment{min-width:53px;font-size:10px}.next{padding:17px}.actions{align-items:stretch;flex-direction:column;gap:8px}"
        ".button{width:100%}.button-secondary{text-align:center}.contact{padding:16px}.step{gap:8px}}"
        "</style></head><body><main id=\"owner_campaign_action_layout\" class=\"owner_campaign_action_layout\">"
        f"<div class=\"brand-row\"><img class=\"brand\" src=\"{logo_url}\" alt=\"PROCASA\"></div>"
        f"<p class=\"eyebrow\">{safe_eyebrow}</p><section class=\"copy\"><h1>{safe_title}</h1>{safe_content}</section>"
        f"</main></body></html>"
    )


def _normalized_name(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "").casefold())
    return " ".join("".join(char for char in raw if not unicodedata.combining(char)).split())


def _executive_contact(db: Any, ledger: Mapping[str, Any]) -> dict[str, str]:
    raw = ledger.get("executive")
    name = str(raw.get("name") or "").strip() if isinstance(raw, Mapping) else str(raw or "").strip()
    if not name:
        return {}
    try:
        users = db["usuarios"]
    except (KeyError, TypeError):
        return {"nombre": name}
    exact = users.find_one(
        {"is_active": True, "nombre": name},
        {"_id": 0, "nombre": 1, "email": 1, "telefono": 1},
    )
    if exact:
        return {key: str(exact.get(key) or "").strip() for key in ("nombre", "email", "telefono")}
    matches = [
        user for user in users.find(
            {"is_active": True}, {"_id": 0, "nombre": 1, "email": 1, "telefono": 1}
        )
        if _normalized_name(user.get("nombre")) == _normalized_name(name)
    ]
    if len(matches) != 1:
        return {"nombre": name}
    return {key: str(matches[0].get(key) or "").strip() for key in ("nombre", "email", "telefono")}


def _format_uf(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "No disponible"
    decimals = 1 if amount % 1 else 0
    return f"{amount:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".") + " UF"


def _campaign_value(value: Any, operation: Any) -> str:
    formatted = _format_uf(value)
    return formatted + ("/mes" if str(operation or "").strip().upper() == "ARRIENDO" else "")


def _numbered_steps(items: tuple[str, ...]) -> str:
    rows = "".join(
        f'<div class="step"><span class="step-no">{index:02d}</span><span>{escape(text)}</span></div>'
        for index, text in enumerate(items, 1)
    )
    return f'<div class="next"><strong class="next-title">QUÉ SIGUE AHORA</strong>{rows}</div>'


def test_mode_enabled() -> bool:
    return _test_mode_enabled()


def issue_test_link_token(
    *,
    property_code: str,
    action: str,
    document_type: str | None = None,
    expires_in_seconds: int = 86400,
    now_epoch: int | None = None,
) -> str:
    """Compatibility wrapper around the canonical campaign token issuer."""
    try:
        return issue_campaign_test_token(
            property_code=property_code,
            action=action,
            document_type=document_type,
            expires_in_seconds=expires_in_seconds,
            now_epoch=now_epoch,
        )
    except ValueError as exc:
        raise OwnerCampaignTestError(str(exc)) from exc


def verify_test_link_token(
    token: str,
    *,
    allowed_actions: frozenset[str] = ALLOWED_ACTIONS,
    now_epoch: int | None = None,
) -> dict[str, Any] | None:
    """Compatibility verifier backed by the single canonical HMAC decoder."""
    return verify_campaign_test_token(token, allowed_actions=allowed_actions, now_epoch=now_epoch)


def _database(db: Any = None):
    if db is not None:
        return db
    from chatbot.storage import get_db

    return get_db()


def _test_ledger(db: Any, property_code: str) -> Mapping[str, Any] | None:
    return db[LEDGER_COLLECTION].find_one(
        {
            "campaign_id": TEST_CAMPAIGN_ID,
            "property_code": property_code,
            "test_mode": True,
        }
    )


def record_test_report_opened(property_code: str, *, token: str, db: Any = None) -> str:
    """Compatibility wrapper that records through the canonical test ledger."""
    if not test_mode_enabled():
        raise OwnerCampaignTestError("test_mode_disabled")
    code = str(property_code or "").strip()
    if not PROPERTY_CODE_RE.fullmatch(code):
        raise OwnerCampaignTestError("property_code_invalid")
    claims = verify_test_link_token(token, allowed_actions=frozenset({REPORT_ACTION}))
    if claims is None or claims.get("property_code") != code:
        raise OwnerCampaignTestError("test_report_token_invalid")
    database = _database(db)
    try:
        stored = persist_test_event(database[LEDGER_COLLECTION], claims, stage="complete")
    except (LookupError, ValueError) as exc:
        raise OwnerCampaignTestError("test_campaign_ledger_invalid") from exc
    return str((stored.get("event_ids") or [""])[-1])


def _live_price_snapshot(db: Any, property_code: str) -> tuple[str, dict[str, Any]]:
    property_doc = db[PROPERTY_COLLECTION].find_one(
        {"codigo": property_code},
        {
            "_id": 0,
            "codigo": 1,
            "tipo_operacion.tipo": 1,
            "tipo_operacion.venta": 1,
            "tipo_operacion.arriendo": 1,
            "tipo_operacion.precio_venta.precio_uf": 1,
            "tipo_operacion.precio_venta.precio_clp": 1,
            "tipo_operacion.precio_arriendo.precio_uf": 1,
            "tipo_operacion.precio_arriendo.precio_clp": 1,
            "estado.ejecutivo": 1,
        },
    )
    if not isinstance(property_doc, Mapping) or str(property_doc.get("codigo") or "") != property_code:
        raise OwnerCampaignTestError("test_property_not_found")
    operation_data = property_doc.get("tipo_operacion") or {}
    operation_text = str(operation_data.get("tipo") or "").casefold()
    sale = "venta" in operation_text
    rent = "arriend" in operation_text
    if sale == rent:
        sale = operation_data.get("venta") is True
        rent = operation_data.get("arriendo") is True
    if sale == rent:
        raise OwnerCampaignTestError("test_property_operation_ambiguous")
    operation = "VENTA" if sale else "ARRIENDO"
    price_key = "precio_venta" if sale else "precio_arriendo"
    price = operation_data.get(price_key) or {}
    if not isinstance(price, Mapping):
        raise OwnerCampaignTestError("test_live_price_missing")
    snapshot = {key: price.get(key) for key in ("precio_uf", "precio_clp")}
    if snapshot["precio_uf"] is None:
        raise OwnerCampaignTestError("test_live_price_missing")
    return operation, snapshot


def _valid_lower_target(ledger: Mapping[str, Any], current_uf: Any) -> bool:
    try:
        current = float(current_uf)
        recommended = float(ledger.get("display_recommended_price"))
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(current) and math.isfinite(recommended) and 0 < recommended < current):
        return False
    # QA email templates now show the confirmation CTA for every property with
    # a valid 5–10% recommendation. Older QA ledger rows may retain an advisor
    # evidence classification, which must not invalidate that signed test CTA.
    reduction_pct = (current - recommended) * 100 / current
    return 5 <= reduction_pct <= 10


def _qa_owner_name(db: Any, property_code: str) -> str:
    try:
        from .owner_campaign_live_prepare import _owner_name

        master = db[PROPERTY_COLLECTION].find_one(
            {"codigo": {"$in": [property_code, int(property_code)]}},
        ) or {}
        return _owner_name(master) or "Sin nombre"
    except (KeyError, TypeError, ValueError):
        return "Sin nombre"


def notify_test_admin_after_persist(
    db: Any, *, row: Mapping[str, Any], property_code: str, event: str, event_id: str,
) -> dict[str, Any]:
    """Send only the administrator WhatsApp after a QA event is read back."""
    if event not in {"price_authorized", "advisor_review_requested"}:
        return {"status": "not_applicable"}
    from .owner_campaign_live_events import _send_admin_whatsapp

    campaign_id = str(row.get("campaign_id") or TEST_CAMPAIGN_ID)
    response = row.get("owner_response") if isinstance(row.get("owner_response"), Mapping) else {}
    executive = row.get("executive")
    executive_name = str(executive.get("name") or "") if isinstance(executive, Mapping) else str(executive or "")
    notification_row = {
        **dict(row),
        "property_code": property_code,
        "campaign_id": campaign_id,
        "owner_email": row.get("intended_owner_email") or row.get("owner_email"),
        "owner_name": _qa_owner_name(db, property_code),
        "property_type": row.get("property_type") or "Propiedad",
        "commune": row.get("commune") or "Comuna no disponible",
        "operation": row.get("operation") or "VENTA",
        "current_price": row.get("current_price_at_send"),
        "previous_price": row.get("current_price_at_send"),
        "recommended_price": row.get("display_recommended_price", row.get("recommended_price")),
        "recommended_adjustment_pct": row.get("recommended_adjustment_pct"),
        "selected_adjustment_type": response.get("selected_adjustment_type"),
        "selected_adjustment_pct": response.get("selected_adjustment_pct"),
        "selected_price": response.get("selected_price"),
        "executive_name": row.get("executive_name") or executive_name,
    }
    ledger = db[LEDGER_COLLECTION]
    base = f"notifications.admin_whatsapp.{event}"
    reservation = ledger.update_one(
        {"campaign_id": campaign_id, "property_code": property_code, "test_mode": True,
         f"{base}.attempted": {"$ne": True}},
        {"$set": {f"{base}.attempted": True, f"{base}.status": "sending",
                  f"{base}.event_id": event_id, f"{base}.started_at": datetime.now(timezone.utc)}},
        upsert=False,
    )
    if getattr(reservation, "modified_count", 0) != 1:
        return {"status": "duplicate_suppressed", "event_id": event_id}
    event_at = next(
        (value.get("event_at") for value in (row.get("response_events") or [])
         if isinstance(value, Mapping) and value.get("event_id") == event_id),
        datetime.now(timezone.utc).isoformat(),
    )
    parsed_at = _event_datetime(event_at) or datetime.now(timezone.utc)
    try:
        result = _send_admin_whatsapp(notification_row, event, parsed_at)
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__, "provider_message_id": None}
    ledger.update_one(
        {"campaign_id": campaign_id, "property_code": property_code, "test_mode": True,
         f"{base}.event_id": event_id},
        {"$set": {f"{base}.status": result.get("status") or "unknown",
                  f"{base}.finished_at": datetime.now(timezone.utc),
                  f"{base}.provider_message_id": result.get("provider_message_id")}},
        upsert=False,
    )
    return {**result, "event_id": event_id}


def process_test_action(
    token: str, *, db: Any = None, confirmed: bool = False,
    selected_adjustment_type: str = "RECOMMENDED",
) -> dict[str, Any]:
    request_started = time.perf_counter()
    timings: dict[str, float] = {}
    token_started = time.perf_counter()
    claims = verify_test_link_token(
        token,
        allowed_actions=frozenset({ACCEPT_PRICE_ACTION, ADVISOR_ACTION}),
    )
    timings["token_validation_ms"] = (time.perf_counter() - token_started) * 1000
    if claims is None:
        raise OwnerCampaignTestError("test_action_token_invalid")
    code = str(claims["property_code"])
    action = str(claims["action"])
    mongo_started = time.perf_counter()
    database = _database(db)
    ledger = _test_ledger(database, code)
    if not ledger:
        raise OwnerCampaignTestError("test_campaign_ledger_missing")
    price_before = price_after = None
    if action == ACCEPT_PRICE_ACTION:
        if ledger.get("cta_type") not in {"PRICE_AUTHORIZATION", "ADVISOR_REVIEW"}:
            raise OwnerCampaignTestError("price_authorization_not_allowed")
        operation, price_before = _live_price_snapshot(database, code)
        if (
            operation not in {"VENTA", "ARRIENDO"}
            or str(ledger.get("operation") or "").strip().upper() != operation
            or not _valid_lower_target(ledger, price_before.get("precio_uf"))
        ):
            raise OwnerCampaignTestError("price_authorization_evidence_invalid")
        try:
            proposed_price = float(ledger.get("display_recommended_price"))
            current_price = float(price_before["precio_uf"])
        except (TypeError, ValueError, KeyError):
            raise OwnerCampaignTestError("price_authorization_values_invalid")
        if not math.isfinite(proposed_price) or not math.isfinite(current_price):
            raise OwnerCampaignTestError("price_authorization_values_invalid")
    elif action == ADVISOR_ACTION and ledger.get("cta_type") not in {
        "ADVISOR_REVIEW", "PRICE_AUTHORIZATION", "REPORT_ONLY"
    }:
        raise OwnerCampaignTestError("advisor_review_not_allowed")
    timings["mongo_lookup_ms"] = (time.perf_counter() - mongo_started) * 1000

    tracking_started = time.perf_counter()
    if action == ACCEPT_PRICE_ACTION:
        stored = persist_test_event(
            database[LEDGER_COLLECTION],
            claims,
            stage="confirm" if confirmed else "click",
            selected_adjustment_type=selected_adjustment_type,
        )
        event_type = "price_authorized" if confirmed and not stored.get("duplicate") else "cta_clicked"
    else:
        stored = persist_test_event(database[LEDGER_COLLECTION], claims, stage="complete")
        event_type = "advisor_review_requested"
    timings["tracking_write_ms"] = (time.perf_counter() - tracking_started) * 1000

    if action == ACCEPT_PRICE_ACTION:
        price_check_started = time.perf_counter()
        operation_after, price_after = _live_price_snapshot(database, code)
        if operation_after != operation or price_after != price_before:
            raise OwnerCampaignTestError("test_price_mutation_detected")
        timings["price_safety_check_ms"] = (time.perf_counter() - price_check_started) * 1000
    refreshed = _test_ledger(database, code) or {}
    expected_ids = list(stored.get("event_ids") or [])
    if expected_ids and not stored.get("duplicate"):
        persisted_ids = {
            str(event.get("event_id")) for event in (refreshed.get("response_events") or [])
            if isinstance(event, Mapping) and event.get("event_id")
        }
        if not set(expected_ids).issubset(persisted_ids):
            raise OwnerCampaignTestError("test_event_readback_failed")
        notification_event = "price_authorized" if confirmed and action == ACCEPT_PRICE_ACTION else "advisor_review_requested" if action == ADVISOR_ACTION else ""
        if notification_event:
            notify_test_admin_after_persist(
                database, row=refreshed, property_code=code,
                event=notification_event, event_id=str(expected_ids[-1] if action == ADVISOR_ACTION else expected_ids[0]),
            )
    timings["total_ms"] = (time.perf_counter() - request_started) * 1000
    recommendation = refreshed.get("recommended_adjustment_pct")
    if recommendation is None:
        try:
            recommendation = int(round((float(refreshed["current_price_at_send"]) - float(refreshed.get("display_recommended_price"))) * 100 / float(refreshed["current_price_at_send"])))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            recommendation = None
    gradual_pct = refreshed.get("gradual_adjustment_pct")
    gradual_price = refreshed.get("gradual_price")
    selected = (refreshed.get("owner_response") or {}) if isinstance(refreshed.get("owner_response"), Mapping) else {}
    executive_value = refreshed.get("executive")
    executive_name = (
        str(executive_value.get("name") or "").strip()
        if isinstance(executive_value, Mapping)
        else str(executive_value or "").strip()
    )
    return {
        "campaign_id": TEST_CAMPAIGN_ID,
        "property_code": code,
        "event": event_type,
        "event_ids": stored.get("event_ids", []),
        "duplicate": bool(stored.get("duplicate")),
        "already_registered": action == ACCEPT_PRICE_ACTION and (
            (selected.get("status") == "PRICE_AUTHORIZED" and not confirmed)
            or (confirmed and bool(stored.get("duplicate")) and selected.get("status") == "PRICE_AUTHORIZED")
        ),
        "requires_confirmation": action == ACCEPT_PRICE_ACTION and not confirmed and selected.get("status") != "PRICE_AUTHORIZED",
        "test_mode": True,
        "live_price_before": price_before,
        "live_price_after": price_after,
        "test_price_mutation": False,
        "operation": str(ledger.get("operation") or "").strip().upper(),
        "document_type": str(ledger.get("document_type") or "NONE").strip().upper(),
        "current_price": refreshed.get("current_price_at_send"),
        "proposed_price": refreshed.get("display_recommended_price"),
        "adjustment_pct": recommendation,
        "recommended_adjustment_pct": recommendation,
        "recommended_price": refreshed.get("display_recommended_price"),
        "gradual_adjustment_pct": gradual_pct,
        "gradual_price": gradual_price,
        "selected_adjustment_type": selected.get("selected_adjustment_type"),
        "selected_adjustment_pct": selected.get("selected_adjustment_pct"),
        "selected_price": selected.get("selected_price"),
        "authorization_status": selected.get("authorization_status") or selected.get("status"),
        "owner_response": selected,
        "owner_name": _qa_owner_name(database, code),
        "property_type": refreshed.get("property_type"),
        "commune": refreshed.get("commune"),
        "executive_name": executive_name,
        "executive_email": refreshed.get("executive_email"),
        "executive_contact": _executive_contact(database, ledger) if action == ADVISOR_ACTION else {},
        "timings_ms": timings,
    }


def _handle_test_action_legacy(token: str, *, db: Any = None, confirmed: bool = False) -> HTMLResponse:
    request_started = time.perf_counter()
    try:
        result = process_test_action(token, db=db, confirmed=confirmed)
    except OwnerCampaignTestError:
        return HTMLResponse(
            "<main><h1>Acción no disponible</h1><p>Solicita un nuevo enlace a tu asesor.</p></main>",
            status_code=404,
        )
    except Exception:
        return HTMLResponse("<main><h1>No pudimos registrar tu solicitud</h1></main>", status_code=503)
    html_started = time.perf_counter()
    report_url = ""
    if result.get("document_type") not in {"", "NONE"}:
        try:
            report_token = issue_test_link_token(
                property_code=str(result["property_code"]),
                action=REPORT_ACTION,
                document_type=str(result["document_type"]),
            )
            report_url = "/campana/informe?token=" + quote(report_token, safe="")
        except (KeyError, ValueError, OwnerCampaignTestError):
            pass
    if result.get("requires_confirmation"):
        action_url = "/campana/test-accion?token=" + quote(token, safe="")
        is_rent = str(result.get("operation") or "").strip().upper() == "ARRIENDO"
        current_price = _campaign_value(result.get("current_price"), result.get("operation"))
        proposed_price = _campaign_value(result.get("proposed_price"), result.get("operation"))
        adjustment = result.get("adjustment_pct")
        try:
            adjustment_text = f"{float(adjustment):.1f}%".replace(".", ",")
        except (TypeError, ValueError):
            adjustment_text = "No disponible"
        content = (
            '<p>Revisa los antecedentes antes de confirmar. Al continuar, autorizas a PROCASA a gestionar '
            f'{"la actualización comercial del canon mensual propuesto para esta propiedad." if is_rent else "la actualización comercial del valor propuesto para esta propiedad."}</p>'
            f'<div class="summary"><div class="summary-cell"><span class="summary-label">'
            f'{"CANON ACTUAL" if is_rent else "VALOR ACTUAL"}</span><strong class="summary-value">{escape(current_price)}</strong></div>'
            '<span class="summary-arrow" aria-hidden="true">→</span>'
            f'<div class="summary-cell new"><span class="summary-label">'
            f'{"NUEVO CANON" if is_rent else "NUEVO VALOR"}</span><strong class="summary-value">{escape(proposed_price)}</strong></div>'
            f'<div class="summary-adjustment"><span class="summary-label">AJUSTE</span><strong>{escape(adjustment_text)}</strong></div></div>'
            f'<div class="notice">Esta autorización no modifica automáticamente el {"canon mensual" if is_rent else "precio"} publicado. '
            'Tu ejecutivo revisará la solicitud y coordinará la actualización correspondiente.</div>'
            f'<form method="post" action="{action_url}">'
            '<div class="actions"><button class="button" type="submit">'
            f'{"CONFIRMAR NUEVO CANON" if is_rent else "CONFIRMAR NUEVO VALOR"} →</button>'
            + (f'<a class="button-secondary" href="{escape(report_url, quote=True)}">Volver sin confirmar</a>' if report_url else '')
            + '</div></form>'
        )
        response = HTMLResponse(
            _campaign_test_page(
                "Confirma el nuevo canon" if is_rent else "Confirma el nuevo valor", content, raw_content=True,
                eyebrow="PROCASA · AUTORIZACIÓN DE CANON" if is_rent else "PROCASA · AUTORIZACIÓN DE PRECIO",
            ),
            status_code=200,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
    elif result["event"] == "price_authorized":
        is_rent = str(result.get("operation") or "").strip().upper() == "ARRIENDO"
        title = "Autorización de canon registrada" if is_rent else "Autorización registrada"
        content = (
            '<div class="check" aria-hidden="true">✓</div>'
            f'<p>Hemos registrado correctamente tu autorización para actualizar el {"canon mensual" if is_rent else "valor comercial"} de la propiedad.</p>'
            f'<div class="summary"><div class="summary-cell new"><span class="summary-label">{"CANON AUTORIZADO" if is_rent else "VALOR AUTORIZADO"}</span>'
            f'<strong class="summary-value">{escape(_campaign_value(result.get("proposed_price"), result.get("operation")))}</strong></div></div>'
            + _numbered_steps((
                "Revisaremos la solicitud y los antecedentes de la propiedad.",
                "Tu ejecutivo coordinará la actualización correspondiente.",
                "Te mantendremos informado sobre el avance de la gestión.",
            ))
            + f'<div class="notice">El {"canon" if is_rent else "precio"} publicado no cambia automáticamente desde esta página.</div>'
            + (f'<div class="actions"><a class="button" href="{escape(report_url, quote=True)}">VOLVER AL INFORME →</a></div>' if report_url else '')
        )
        eyebrow = "PROCASA · SOLICITUD RECIBIDA"
    else:
        title = "Solicitud enviada a tu ejecutivo"
        contact = result.get("executive_contact") if isinstance(result.get("executive_contact"), Mapping) else {}
        contact_name = str(contact.get("nombre") or "").strip()
        contact_email = str(contact.get("email") or "").strip()
        contact_phone = str(contact.get("telefono") or "").strip()
        executive_block = ""
        if contact_name or contact_email or contact_phone:
            email_html = (
                f'<a href="mailto:{escape(contact_email, quote=True)}">{escape(contact_email)}</a>'
                if contact_email else ""
            )
            phone_digits = re.sub(r"[^0-9+]", "", contact_phone)
            phone_html = (
                f'<a href="tel:{escape(phone_digits, quote=True)}">{escape(contact_phone)}</a>'
                if contact_phone else ""
            )
            executive_block = (
                '<div class="contact"><strong class="contact-title">TU EJECUTIVO PROCASA</strong>'
                + "<br>".join(value for value in (escape(contact_name), email_html, phone_html) if value)
                + "</div>"
            )
        content = (
            '<div class="badge-icon" aria-hidden="true">◎</div>'
            '<p>Registramos tu solicitud. Tu ejecutivo PROCASA revisará los antecedentes de la propiedad para conversar contigo sobre su posicionamiento y las alternativas comerciales disponibles.</p>'
            + _numbered_steps((
                "Revisión de los antecedentes comerciales.",
                "Contacto para resolver dudas y evaluar alternativas.",
                "Definición conjunta de los próximos pasos.",
            ))
            + executive_block
            + (f'<div class="actions"><a class="button" href="{escape(report_url, quote=True)}">VOLVER AL INFORME →</a></div>' if report_url else '')
        )
        eyebrow = "PROCASA · ASESORÍA COMERCIAL"
    if not result.get("requires_confirmation"):
        response = HTMLResponse(
            _campaign_test_page(title, content, raw_content=True, eyebrow=eyebrow),
            status_code=200,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
    timings = dict(result.get("timings_ms") or {}) if isinstance(result.get("timings_ms"), Mapping) else {}
    timings["html_render_ms"] = (time.perf_counter() - html_started) * 1000
    timings["total_ms"] = (time.perf_counter() - request_started) * 1000
    response.headers["X-Campaign-Action-Stage"] = "confirmation-post" if confirmed else "action-get"
    response.headers["Server-Timing"] = ", ".join(f"{name};dur={value:.2f}" for name, value in timings.items())
    for name, value in timings.items():
        response.headers["X-Campaign-" + name.replace("_", "-").title()] = f"{value:.2f}"
    return response


def handle_test_action(
    token: str, *, db: Any = None, confirmed: bool = False,
    selected_adjustment_type: str = "RECOMMENDED",
) -> HTMLResponse:
    """Render the same private decision and success pages used by production."""
    request_started = time.perf_counter()
    try:
        result = process_test_action(
            token, db=db, confirmed=confirmed,
            selected_adjustment_type=selected_adjustment_type,
        )
    except OwnerCampaignTestError:
        return HTMLResponse(
            "<main><h1>Acción no disponible</h1><p>Solicita un nuevo enlace a tu ejecutivo.</p></main>",
            status_code=404,
        )
    except Exception:
        return HTMLResponse("<main><h1>No pudimos registrar tu solicitud</h1></main>", status_code=503)

    from .owner_campaign_confirmation_page import render_decision_page, render_success_page

    logo_url = (Config.CRM_BASE_URL or "https://procasa.cl").rstrip("/") + "/static/logo.png"
    code = str(result.get("property_code") or "")
    advisor_url = "/campana/test-accion?token=" + quote(
        issue_test_link_token(property_code=code, action=ADVISOR_ACTION), safe="",
    )
    if result.get("requires_confirmation"):
        if result.get("already_registered"):
            page = render_success_page(
                selected_type="", selected_pct=None, selected_price="",
                advisor_url=advisor_url, already_registered=True, logo_url=logo_url,
            )
        else:
            token_url = "/campana/test-accion?token=" + quote(token, safe="")
            gradual_pct = result.get("gradual_adjustment_pct")
            if gradual_pct is not None and int(gradual_pct) == int(result["recommended_adjustment_pct"]):
                gradual_pct = None
            page = render_decision_page(
                recommended_pct=int(result["recommended_adjustment_pct"]),
                recommended_price=_campaign_value(result.get("recommended_price"), result.get("operation")),
                current_price=_campaign_value(result.get("current_price"), result.get("operation")),
                gradual_pct=int(gradual_pct) if gradual_pct is not None else None,
                gradual_price=_campaign_value(result.get("gradual_price"), result.get("operation")),
                recommended_url=token_url + "&selected_adjustment_type=RECOMMENDED",
                gradual_url=token_url + "&selected_adjustment_type=GRADUAL",
                advisor_url=advisor_url, logo_url=logo_url,
            )
    elif result.get("already_registered"):
        page = render_success_page(
            selected_type="", selected_pct=None, selected_price="",
            advisor_url=advisor_url, already_registered=True, logo_url=logo_url,
        )
    elif result.get("event") == "price_authorized":
        page = render_success_page(
            selected_type=str(result.get("selected_adjustment_type") or selected_adjustment_type),
            selected_pct=result.get("selected_adjustment_pct"),
            selected_price=_campaign_value(result.get("selected_price"), result.get("operation")),
            recommended_pct=result.get("recommended_adjustment_pct"),
            recommended_price=_campaign_value(result.get("recommended_price"), result.get("operation")),
            logo_url=logo_url,
        )
    else:
        page = render_success_page(
            selected_type="ADVISOR_REVIEW", selected_pct=None, selected_price="", logo_url=logo_url,
        )

    response = HTMLResponse(
        page, status_code=200,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
    elapsed = (time.perf_counter() - request_started) * 1000
    response.headers["X-Campaign-Action-Stage"] = "confirmation-post" if confirmed else "action-get"
    response.headers["Server-Timing"] = f"total;dur={elapsed:.2f}"
    response.headers["X-Campaign-Total"] = f"{elapsed:.2f}"
    return response
