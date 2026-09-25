# campanas/handler.py
import asyncio
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pymongo import MongoClient

from config import Config
from .email_service import enviar_alerta_equipo
from .utils import get_accion_config, normalize_accion
from .test_mode import TEST_ACTIONS, TEST_RECIPIENT, persist_test_event, verify_test_token
from . import private_report

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="campanas/templates")

def _find_contacto_by_email(contactos, email_lower: str):
    contacto = contactos.find_one({"email_propietario_lc": email_lower})
    if contacto:
        return contacto
    # Fallback legacy para datos antiguos sin campo normalizado.
    contacto = contactos.find_one({"email_propietario": {"$regex": f"^{re.escape(email_lower)}$", "$options": "i"}})
    if contacto and contacto.get("email_propietario_lc") != email_lower:
        contactos.update_one({"_id": contacto.get("_id")}, {"$set": {"email_propietario_lc": email_lower}})
    return contacto


def _sync_process_campana_response(
    email: str,
    accion: str,
    codigos: str,
    campana: str,
    mode: str,
    token: str,
    user_agent: str,
    ip: str,
) -> dict:
    email_lower = (email or "").lower().strip()
    codigos_lista = [c.strip() for c in (codigos or "").split(",") if c.strip() and c.strip() != "N/A"]
    ahora = datetime.utcnow()
    if mode == "test":
        # Fail closed before connecting to Mongo. Unlike the legacy test path,
        # this branch never reads or updates contactos or the live property.
        if os.getenv("OWNER_CAMPAIGN_TEST_MODE", "").strip().casefold() != "true":
            return {
                "status_code": 404,
                "titulo": "Acción de prueba no disponible",
                "color": "#6b7280",
                "accion_label": "Modo de prueba desactivado",
                "mensaje": "Este enlace de prueba no está disponible.",
            }
        if (
            len(codigos_lista) != 1
            or accion not in TEST_ACTIONS
            or accion == "ver_informe"
            or email_lower != TEST_RECIPIENT
        ):
            return {
                "status_code": 400,
                "titulo": "Enlace de prueba inválido",
                "color": "#6b7280",
                "accion_label": "Prueba inválida",
                "mensaje": "El enlace de prueba no es válido o expiró.",
            }
        claims = verify_test_token(
            token,
            secret=os.getenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", ""),
            campaign_id=campana,
            property_code=codigos_lista[0],
            action=accion,
            recipient=email_lower,
        )
        if claims is None:
            return {
                "status_code": 400,
                "titulo": "Enlace de prueba inválido",
                "color": "#6b7280",
                "accion_label": "Token inválido",
                "mensaje": "El enlace de prueba no es válido o expiró.",
            }
        client = MongoClient(Config.MONGO_URI)
        try:
            db = client[Config.DB_NAME]
            stored = persist_test_event(db[Config.COLLECTION_CAMPANAS_LOG], claims, now=ahora.replace(tzinfo=timezone.utc))
        finally:
            client.close()
        return {
            "status_code": 200,
            "titulo": "Acción de prueba registrada",
            "color": "#3b82f6",
            "accion_label": stored["event"],
            "mensaje": "Registramos esta acción en modo de prueba. No se modificó el precio ni la propiedad.",
        }
    config_accion = get_accion_config(accion)

    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    contactos = db[Config.COLLECTION_CONTACTOS]
    respuestas = db[Config.COLLECTION_RESPUESTAS]
    historico = db[Config.COLLECTION_CAMPANAS_LOG]
    legacy_historicos = []
    for legacy_name in ["campanas_historico", "campaigns_price_drop_log"]:
        if legacy_name != Config.COLLECTION_CAMPANAS_LOG:
            legacy_historicos.append(db[legacy_name])

    contacto = _find_contacto_by_email(contactos, email_lower)
    update_price = contacto.get("update_price", {}) if contacto else {}

    # Bloqueo fuerte: 1 respuesta por email+campaÃ±a
    if (not token) and update_price.get("campana_nombre") == campana and update_price.get("respuesta"):
        return {
            "status_code": 200,
            "titulo": "Respuesta ya registrada",
            "color": "#6b7280",
            "accion_label": "Registro completo",
            "mensaje": "Ya registramos una respuesta previa para esta campaÃ±a. Si desea cambiar su decisiÃ³n, contacte a su asesor.",
        }

    # Token obligatorio fuera de test; ademÃ¡s bloqueo atÃ³mico por token para doble click
    if token:
        update_payload = {
            "$set": {
                "respuesta_propietario": accion,
                "respuesta_at": ahora.isoformat(),
                "respuesta_mode": mode,
                "respuesta_email": email_lower,
                "estado_respuesta": "respondido",
                "respuesta_confirmada": True,
                "primer_click_at": ahora,
                "click_user_agent": user_agent,
                "click_ip": ip,
            }
        }
        historico_doc = historico.find_one_and_update(
            {"token": token, "respuesta_confirmada": {"$ne": True}},
            update_payload,
            return_document=False,
        )
        # Compatibilidad temporal: si el token estÃ¡ en colecciones histÃ³ricas antiguas
        if historico_doc is None:
            for legacy_col in legacy_historicos:
                historico_doc = legacy_col.find_one_and_update(
                    {"token": token, "respuesta_confirmada": {"$ne": True}},
                    update_payload,
                    return_document=False,
                )
                if historico_doc is not None:
                    break
        if historico_doc is None:
            existing_token = historico.find_one({"token": token})
            if not existing_token:
                for legacy_col in legacy_historicos:
                    existing_token = legacy_col.find_one({"token": token})
                    if existing_token:
                        break
            if existing_token:
                logger.warning(
                    "[CAMPANA_RESPUESTA_BLOQUEADO] token=%s email=%s accion=%s ip=%s",
                    token, email_lower, accion, ip
                )
                return {
                    "status_code": 200,
                    "titulo": "Respuesta ya registrada",
                    "color": "#6b7280",
                    "accion_label": "Registro completo",
                    "mensaje": "Esta respuesta ya fue registrada anteriormente por su ejecutivo. Agradecemos su tiempo y preferencia.",
                }
            logger.warning("[CAMPANA_RESPUESTA_TOKEN_INVALIDO] token=%s email=%s accion=%s", token, email_lower, accion)
            return {
                "status_code": 400,
                "titulo": "Enlace invÃ¡lido",
                "color": "#6b7280",
                "accion_label": "Token invÃ¡lido",
                "mensaje": "Enlace invÃ¡lido o expirado. Solicite un nuevo enlace a su asesor.",
            }
    elif mode != "test":
        logger.warning("[CAMPANA_RESPUESTA_SIN_TOKEN_BLOQUEADA] email=%s campana=%s accion=%s", email_lower, campana, accion)
        return {
            "status_code": 400,
            "titulo": "Enlace invÃ¡lido",
            "color": "#6b7280",
            "accion_label": "Sin token",
            "mensaje": "Este enlace no es vÃ¡lido para respuesta automÃ¡tica. Contacte a su asesor.",
        }

    respuestas.update_one(
        {
            "email": email_lower,
            "campana_nombre": campana,
            "accion": accion,
            "codigos_propiedad": codigos_lista,
            "mode": mode,
        },
        {"$set": {"fecha_respuesta": ahora, "token": token or ""}},
        upsert=True,
    )

    upd = contactos.update_one(
        {"$or": [{"email_propietario_lc": email_lower}, {"email_propietario": {"$regex": f"^{re.escape(email_lower)}$", "$options": "i"}}]},
        {
            "$set": {
                "update_price.campana_nombre": campana,
                "update_price.respuesta": accion,
                "update_price.fecha_respuesta": ahora,
                "estado": config_accion["estado"],
                "bloqueo_email": accion in {"no_disponible", "unsubscribe"},
                "email_propietario_lc": email_lower,
            }
        },
    )
    logger.info(
        "[CAMPANA_RESPUESTA] email=%s campana=%s accion=%s mode=%s matched=%s modified=%s",
        email_lower, campana, accion, mode, getattr(upd, "matched_count", 0), getattr(upd, "modified_count", 0),
    )

    contacto = _find_contacto_by_email(contactos, email_lower)
    nombre = "Sin nombre"
    telefono = "Sin telefono"
    if contacto:
        nombre = f"{contacto.get('nombre_propietario','')} {contacto.get('apellido_paterno_propietario','')} {contacto.get('apellido_materno_propietario','')}".strip() or "Sin nombre"
        telefono = contacto.get("telefono", "Sin telefono")

    if mode != "test":
        accion_texto = config_accion["titulo"].upper().replace("!", "")
        enviar_alerta_equipo(nombre, telefono, email_lower, codigos_lista, accion_texto, campana)

    return {
        "status_code": 200,
        "titulo": config_accion["titulo"],
        "color": config_accion["color"],
        "accion_label": accion.replace("_", " ").title(),
        "mensaje": config_accion["mensaje"],
    }


async def handle_campana_respuesta(
    request: Request,
    email: str,
    accion: str,
    codigos: str,
    campana: str,
    mode: str = "live",
    token: str = "",
):
    accion = normalize_accion(accion)
    valid = {"aceptar_rebaja", "contactar_ejecutivo", "mantener_precio", "no_disponible", "unsubscribe"}
    if mode == "test":
        valid |= TEST_ACTIONS
    if accion not in valid:
        return HTMLResponse("Accion no valida", status_code=400)

    if request.method.upper() == "POST" and not (mode == "test" and accion == "aceptar_rebaja"):
        return HTMLResponse("Método no permitido.", status_code=405)

    if mode == "test" and accion == "aceptar_rebaja":
        return await asyncio.to_thread(
            _resolve_test_price_authorization_response,
            email=email,
            codigos=codigos,
            campana=campana,
            token=token,
            confirmed=request.method.upper() == "POST",
        )

    try:
        user_agent = request.headers.get("user-agent", "")
        ip = request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")
        result = await asyncio.to_thread(
            _sync_process_campana_response,
            email,
            accion,
            codigos,
            campana,
            mode,
            token,
            user_agent,
            ip,
        )
        now = datetime.utcnow()
        try:
            return templates.TemplateResponse(
                "base.html",
                {
                    "request": request,
                    "current_year": now.year,
                    "titulo": result["titulo"],
                    "color": result["color"],
                    "accion": result["accion_label"],
                    "mensaje": result["mensaje"],
                },
                status_code=result.get("status_code", 200),
            )
        except Exception:
            return HTMLResponse(result.get("mensaje", "Tu respuesta fue registrada correctamente."), status_code=result.get("status_code", 200))
    except Exception as e:
        logger.error(f"Error en campana: {e}", exc_info=True)
        return HTMLResponse("Error interno del servidor", status_code=500)


def _resolve_test_price_authorization_response(
    *, email: str, codigos: str, campana: str, token: str, confirmed: bool,
):
    """Require an explicit POST confirmation before recording test price approval."""
    if os.getenv("OWNER_CAMPAIGN_TEST_MODE", "").strip().casefold() != "true":
        return HTMLResponse("Acción de prueba no disponible.", status_code=404)
    email_lower = (email or "").strip().casefold()
    codes = [item.strip() for item in (codigos or "").split(",") if item.strip()]
    if len(codes) != 1 or not codes[0].isdigit() or email_lower != TEST_RECIPIENT:
        return HTMLResponse("Enlace de prueba inválido.", status_code=404)
    code = codes[0]
    claims = verify_test_token(
        token,
        secret=os.getenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", ""),
        campaign_id=campana,
        property_code=code,
        action="aceptar_rebaja",
        recipient=email_lower,
    )
    if claims is None:
        return HTMLResponse("Enlace de prueba inválido o vencido.", status_code=404)

    client = MongoClient(Config.MONGO_URI)
    try:
        db = client[Config.DB_NAME]
        ledger = db[Config.COLLECTION_CAMPANAS_LOG]
        try:
            stored = persist_test_event(ledger, claims, stage="confirm" if confirmed else "click")
        except (LookupError, ValueError):
            return HTMLResponse("La confirmación de prueba no está disponible.", status_code=404)
    finally:
        client.close()

    if confirmed:
        return HTMLResponse(
            _campaign_test_page(
                "Autorización registrada en modo de prueba",
                "Registramos tu autorización para el nuevo valor propuesto. El precio publicado no fue modificado.",
            ),
            status_code=200,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    proposed_value = stored.get("proposed_value")
    if proposed_value is None:
        proposed_copy = "el nuevo valor indicado en el correo"
    else:
        try:
            numeric_value = float(proposed_value)
            decimals = 1 if numeric_value % 1 else 0
            proposed_copy = f"{numeric_value:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".") + " UF"
        except (TypeError, ValueError):
            proposed_copy = "el nuevo valor indicado en el correo"
    from html import escape
    from urllib.parse import urlencode

    post_url = "/campana/respuesta?" + urlencode({
        "email": TEST_RECIPIENT,
        "accion": "aceptar_rebaja",
        "codigos": code,
        "campana": campana,
        "mode": "test",
        "token": token,
    })
    content = (
        f"<p>¿Confirmas que autorizas {escape(proposed_copy)} para la propiedad {escape(code)}?</p>"
        f'<form method="post" action="{escape(post_url, quote=True)}">'
        '<button type="submit">Confirmar autorización</button></form>'
        "<p>Esta acción se registrará en modo de prueba. No modificará el precio publicado.</p>"
    )
    return HTMLResponse(
        _campaign_test_page("Confirma el nuevo valor", content, raw_content=True),
        status_code=200,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


def _campaign_test_page(title: str, body: str, *, raw_content: bool = False) -> str:
    from html import escape

    safe_body = body if raw_content else f"<p>{escape(body)}</p>"
    return (
        '<!doctype html><html lang="es"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>PROCASA | Confirmación de prueba</title>'
        '<body style="margin:0;background:#f4f5fb;font-family:Arial,sans-serif;color:#25205f">'
        '<main style="max-width:560px;margin:12vh auto;padding:32px 28px;background:#fff;'
        'border:1px solid #e5e6ef;border-radius:14px;text-align:center">'
        '<div style="font-size:20px;font-weight:700;letter-spacing:.04em">PROCASA</div>'
        f'<h1 style="font-size:22px;margin:24px 0 12px">{escape(title)}</h1>{safe_body}'
        '</main></body></html>'
    )


def _record_test_report_event(claims, *, stage: str):
    client = MongoClient(Config.MONGO_URI)
    try:
        ledger = client[Config.DB_NAME][Config.COLLECTION_CAMPANAS_LOG]
        return persist_test_event(ledger, claims, now=datetime.now(timezone.utc), stage=stage)
    finally:
        client.close()


def _resolve_test_report_response(*, token: str):
    """Track a signed QA request and delegate Drive lookup/delivery to the historic resolver."""
    secret = os.getenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", "")
    decoded = private_report._decode_token_claims(token, secret)
    if decoded is None:
        return private_report._response(404, "Documento no disponible.")
    claims = verify_test_token(
        token,
        secret=secret,
        campaign_id=private_report.TEST_CAMPAIGN_ID,
        property_code=decoded["property_code"],
        action=private_report.REPORT_ACTION,
        recipient=private_report.TEST_RECIPIENT,
        document_type=decoded["document_type"],
    )
    if claims is None:
        return private_report._response(404, "Documento no disponible.")

    try:
        _record_test_report_event(claims, stage="click")
        response = private_report._serve_campaign_report(token)
        if (
            response.status_code == 200
            and response.headers.get("x-campaign-report-resolution") == "drive"
        ):
            _record_test_report_event(claims, stage="complete")
        return response
    except (LookupError, ValueError):
        logger.info("[CAMPAIGN_REPORT_TRACKING] status=prepared_test_row_missing code=%s", decoded["property_code"])
        return private_report._response(404, "Documento no disponible para esta prueba.")
    except Exception as exc:
        logger.error("Error resolviendo documento de test (%s)", type(exc).__name__, exc_info=True)
        return private_report._response(503, "No pudimos consultar el informe en este momento.")


async def handle_campana_informe(*, token: str):
    return await asyncio.to_thread(_resolve_test_report_response, token=token)
