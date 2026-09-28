"""Compatibility entry point for the canonical, test-only owner campaign flow."""

from __future__ import annotations

import math
import re
import time
import unicodedata
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


def _campaign_test_page(
    title: str,
    content: str,
    raw_content: bool = False,
    *,
    back_url: str = "javascript:history.back()",
    back_label: str = "VOLVER AL INFORME",
) -> str:
    """Shared, production-looking responsive layout for all owner action pages."""
    safe_title = escape(str(title or "PROCASA"))
    safe_content = str(content or "") if raw_content else f"<p>{escape(str(content or ''))}</p>"
    safe_back_url = escape(str(back_url or "javascript:history.back()"), quote=True)
    safe_back_label = escape(str(back_label or "VOLVER AL INFORME"))
    logo_url = escape((Config.CRM_BASE_URL or "https://procasa.cl").rstrip("/") + "/static/logo.png", quote=True)
    return (
        "<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>PROCASA | {safe_title}</title>"
        "<style>body{margin:0;padding:24px 16px;background:#f4f3fa;font-family:Arial,sans-serif;color:#25224a}"
        ".card{box-sizing:border-box;max-width:600px;margin:7vh auto;padding:30px 30px;background:#fff;"
        "border:1px solid #e5e3f0;border-radius:14px;box-shadow:0 8px 28px rgba(35,29,78,.08)}"
        ".brand{display:block;width:112px;height:auto;max-height:44px;object-fit:contain;margin-bottom:26px}"
        "h1{margin:0 0 16px;color:#17175f;font-size:26px;line-height:1.22}"
        ".copy{font-size:16px;line-height:1.55;color:#4e5571}.facts{margin:20px 0;padding:16px;"
        "background:#f8f7ff;border:1px solid #e5e6ef;border-radius:10px}"
        ".facts div{margin:5px 0;font-size:14px}.facts strong{color:#62698a;font-size:11px;letter-spacing:.04em}"
        ".next{margin:22px 0 0;padding:16px;background:#f8f7ff;border-radius:10px;font-size:14px;line-height:1.55}"
        ".next strong{display:block;margin-bottom:8px;color:#17175f;font-size:12px;letter-spacing:.05em}"
        ".contact{margin-top:18px;padding-top:14px;border-top:1px solid #e5e6ef;font-size:13px;line-height:1.6}"
        ".button{display:inline-block;margin-top:22px;padding:13px 19px;border-radius:999px;background:#17175f;"
        "color:#fff!important;text-decoration:none;font-size:13px;font-weight:bold;letter-spacing:.03em}"
        "@media(max-width:480px){body{padding:14px 10px}.card{margin:3vh auto;padding:24px 18px}"
        ".brand{width:96px;margin-bottom:22px}h1{font-size:23px}.copy{font-size:15px}}</style></head>"
        "<body><main class=\"card\">"
        f"<img class=\"brand\" src=\"{logo_url}\" alt=\"PROCASA\">"
        f"<h1>{safe_title}</h1><section class=\"copy\">{safe_content}</section>"
        f"<a class=\"button\" href=\"{safe_back_url}\">{safe_back_label}</a>"
        "</main></body></html>"
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
    if ledger.get("evidence_segment") != "PRICE_AUTHORIZATION_READY":
        return False
    try:
        current = float(current_uf)
        recommended = float(ledger.get("display_recommended_price"))
    except (TypeError, ValueError):
        return False
    return math.isfinite(current) and math.isfinite(recommended) and 0 < recommended < current


def process_test_action(token: str, *, db: Any = None, confirmed: bool = False) -> dict[str, Any]:
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
    timings["mongo_lookup_ms"] = (time.perf_counter() - mongo_started) * 1000

    tracking_started = time.perf_counter()
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
    timings["tracking_write_ms"] = (time.perf_counter() - tracking_started) * 1000

    if action == ACCEPT_PRICE_ACTION:
        price_check_started = time.perf_counter()
        operation_after, price_after = _live_price_snapshot(database, code)
        if operation_after != operation or price_after != price_before:
            raise OwnerCampaignTestError("test_price_mutation_detected")
        timings["price_safety_check_ms"] = (time.perf_counter() - price_check_started) * 1000
    timings["total_ms"] = (time.perf_counter() - request_started) * 1000
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
        "operation": str(ledger.get("operation") or "").strip().upper(),
        "document_type": str(ledger.get("document_type") or "NONE").strip().upper(),
        "current_price": ledger.get("current_price_at_send"),
        "proposed_price": ledger.get("display_recommended_price"),
        "adjustment_pct": ledger.get("adjustment_pct"),
        "executive_contact": _executive_contact(database, ledger) if action == ADVISOR_ACTION else {},
        "timings_ms": timings,
    }


def handle_test_action(token: str, *, db: Any = None, confirmed: bool = False) -> HTMLResponse:
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
    report_url = "javascript:history.back()"
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
        current_price = _format_uf(result.get("current_price"))
        proposed_price = _format_uf(result.get("proposed_price"))
        adjustment = result.get("adjustment_pct")
        try:
            adjustment_text = f"{float(adjustment):.1f}%".replace(".", ",")
        except (TypeError, ValueError):
            adjustment_text = "No disponible"
        content = (
            '<p>Estás autorizando a PROCASA a gestionar la actualización del valor comercial propuesto para esta propiedad.</p>'
            f'<div class="facts"><div><strong>VALOR ACTUAL</strong><br>{escape(current_price)}</div>'
            f'<div><strong>NUEVO VALOR</strong><br>{escape(proposed_price)}</div>'
            f'<div><strong>AJUSTE</strong><br>{escape(adjustment_text)}</div></div>'
            f'<form method="post" action="{action_url}">'
            '<button class="button" type="submit" style="margin-top:0;border:0;cursor:pointer;">'
            'CONFIRMAR AUTORIZACIÓN</button></form>'
            '<p>El precio publicado no se modificará automáticamente desde esta página. Nuestro equipo revisará la autorización y gestionará los pasos siguientes.</p>'
        )
        response = HTMLResponse(
            _campaign_test_page(
                "Confirma el nuevo valor", content, raw_content=True,
                back_url=report_url, back_label="VOLVER SIN CONFIRMAR",
            ),
            status_code=200,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
    elif result["event"] == "price_authorized":
        title = "Autorización registrada"
        content = (
            '<p>Hemos registrado correctamente tu autorización para revisar el valor comercial de la propiedad.</p>'
            '<div class="next"><strong>QUÉ SIGUE AHORA</strong>'
            '<div>• Nuestro equipo revisará la solicitud.</div>'
            '<div>• Tu ejecutivo coordinará los pasos necesarios.</div>'
            '<div>• Te mantendremos informado sobre el avance de la gestión.</div></div>'
            '<p>El valor publicado no se modifica automáticamente desde esta confirmación.</p>'
        )
    else:
        title = "Solicitud de revisión registrada"
        contact = result.get("executive_contact") if isinstance(result.get("executive_contact"), Mapping) else {}
        contact_name = str(contact.get("nombre") or "").strip()
        contact_email = str(contact.get("email") or "").strip()
        contact_phone = str(contact.get("telefono") or "").strip()
        executive_block = ""
        if contact_name or contact_email or contact_phone:
            executive_block = (
                '<div class="contact"><strong>Ejecutivo a cargo</strong><br>'
                + "<br>".join(escape(value) for value in (contact_name, contact_email, contact_phone) if value)
                + "</div>"
            )
        content = (
            '<p>Hemos registrado tu solicitud de revisión. Tu ejecutivo PROCASA será informado para revisar contigo el posicionamiento actual de tu propiedad y orientarte respecto de los próximos pasos.</p>'
            '<div class="next"><strong>QUÉ SIGUE AHORA</strong>'
            '<div>1. Tu ejecutivo revisará los antecedentes de la propiedad.</div>'
            '<div>2. Se pondrá en contacto contigo para resolver dudas y evaluar alternativas.</div>'
            '<div>3. Podrán definir juntos la estrategia comercial más conveniente.</div></div>'
            + executive_block
        )
    if not result.get("requires_confirmation"):
        response = HTMLResponse(
            _campaign_test_page(title, content, raw_content=True, back_url=report_url),
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
