"""Compatibility entry point for the canonical, test-only owner campaign flow."""

from __future__ import annotations

import math
import re
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


def _campaign_test_page(title: str, content: str, raw_content: bool = False) -> str:
    """Wrap trusted test-action content in a small responsive PROCASA page."""
    safe_title = escape(str(title or "Acción de prueba"))
    safe_content = str(content or "") if raw_content else f"<p>{escape(str(content or ''))}</p>"
    return (
        "<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{safe_title}</title></head>"
        "<body style=\"margin:0;padding:24px 16px;background:#f4f3fa;font-family:Arial,sans-serif;color:#25224a;\">"
        "<main class=\"card\" style=\"box-sizing:border-box;max-width:520px;margin:8vh auto;padding:28px 24px;background:#fff;"
        "border:1px solid #e5e3f0;border-radius:12px;box-shadow:0 8px 28px rgba(35,29,78,.08);\">"
        f"<div style=\"margin-bottom:18px;color:#4232c5;font-size:13px;font-weight:700;letter-spacing:.08em;\">PROCASA</div>"
        f"<h1 style=\"margin:0 0 16px;font-size:24px;line-height:1.25;\">{safe_title}</h1>"
        f"<section style=\"font-size:16px;line-height:1.55;\">{safe_content}</section>"
        "</main></body></html>"
    )


def test_mode_enabled() -> bool:
    return _test_mode_enabled()


def issue_test_link_token(
    *,
    property_code: str,
    action: str,
    document_type: str | None = None,
    expires_in_seconds: int = 3600,
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
            "actual_recipient_email": TEST_RECIPIENT,
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
    if ledger.get("evidence_segment") != "PRICE_AUTHORIZATION_READY":
        return False
    try:
        current = float(current_uf)
        recommended = float(ledger.get("display_recommended_price"))
    except (TypeError, ValueError):
        return False
    return math.isfinite(current) and math.isfinite(recommended) and 0 < recommended < current


def process_test_action(token: str, *, db: Any = None, confirmed: bool = False) -> dict[str, Any]:
    claims = verify_test_link_token(
        token,
        allowed_actions=frozenset({ACCEPT_PRICE_ACTION, ADVISOR_ACTION}),
    )
    if claims is None:
        raise OwnerCampaignTestError("test_action_token_invalid")
    code = str(claims["property_code"])
    action = str(claims["action"])
    database = _database(db)
    ledger = _test_ledger(database, code)
    if not ledger:
        raise OwnerCampaignTestError("test_campaign_ledger_missing")
    price_before = price_after = None
    if action == ACCEPT_PRICE_ACTION:
        if ledger.get("cta_type") != "PRICE_AUTHORIZATION":
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

    if action == ACCEPT_PRICE_ACTION:
        stored = persist_test_event(
            database[LEDGER_COLLECTION],
            claims,
            stage="confirm" if confirmed else "click",
        )
        event_type = "price_authorized" if confirmed else "cta_clicked"
    else:
        stored = persist_test_event(database[LEDGER_COLLECTION], claims, stage="complete")
        event_type = "advisor_review_requested"

    if action == ACCEPT_PRICE_ACTION:
        operation_after, price_after = _live_price_snapshot(database, code)
        if operation_after != operation or price_after != price_before:
            raise OwnerCampaignTestError("test_price_mutation_detected")
    return {
        "campaign_id": TEST_CAMPAIGN_ID,
        "property_code": code,
        "event": event_type,
        "event_ids": stored.get("event_ids", []),
        "requires_confirmation": action == ACCEPT_PRICE_ACTION and not confirmed,
        "test_mode": True,
        "live_price_before": price_before,
        "live_price_after": price_after,
        "test_price_mutation": False,
    }


def handle_test_action(token: str, *, db: Any = None, confirmed: bool = False) -> HTMLResponse:
    try:
        result = process_test_action(token, db=db, confirmed=confirmed)
    except OwnerCampaignTestError:
        return HTMLResponse(
            "<main><h1>Acción de prueba no disponible</h1><p>Solicita un nuevo enlace a tu asesor.</p></main>",
            status_code=404,
        )
    except Exception:
        return HTMLResponse("<main><h1>No pudimos registrar la prueba</h1></main>", status_code=503)
    if result.get("requires_confirmation"):
        action_url = "/campana/test-accion?token=" + quote(token, safe="")
        content = (
            '<p>Confirma que autorizas el nuevo valor propuesto para esta prueba.</p>'
            f'<form method="post" action="{action_url}">'
            '<button type="submit" style="display:inline-block;padding:12px 18px;border:0;border-radius:7px;'
            'background:#4232c5;color:#fff;font-size:15px;font-weight:700;cursor:pointer;">'
            'CONFIRMAR AUTORIZACIÓN</button></form>'
            '<p>El precio publicado no será modificado automáticamente.</p>'
        )
        return HTMLResponse(
            _campaign_test_page("Confirma el nuevo valor", content, raw_content=True),
            status_code=200,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
    if result["event"] == "price_authorized":
        title = "Autorización registrada"
        message = "La autorización quedó registrada en modo de prueba. El precio publicado no fue modificado."
    else:
        title = "Solicitud de revisión registrada"
        message = "Tu solicitud quedó registrada en modo de prueba."
    return HTMLResponse(
        _campaign_test_page(title, message),
        status_code=200,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
