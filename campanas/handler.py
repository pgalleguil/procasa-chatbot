# campanas/handler.py
import asyncio
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pymongo import MongoClient

from config import Config
from .email_service import enviar_alerta_equipo
from .utils import get_accion_config, normalize_accion
from .test_mode import TEST_ACTIONS, TEST_RECIPIENT, persist_test_event, test_mode_enabled, verify_test_token
from . import private_report

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="campanas/templates")


def owner_campaign_authorization_is_stale(row: dict) -> bool:
    """Fail closed for a stale price recommendation; advisor contact remains available."""
    return str(row.get("send_status") or "").strip().upper() == "SKIPPED_STALE_OR_MISMATCH"

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
        if not test_mode_enabled():
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


def _process_owner_campaign_action(
    *, email: str, accion: str, codigo: str, campana: str, token: str,
    method: str, selected_adjustment_type: str = "", owner_portal_context_token: str = "",
    owner_portal_session_id: str = "", owner_portal_client_event_id: str = "",
):
    """Handle a signed single-property production campaign action."""
    from html import escape
    from urllib.parse import urlencode
    from pymongo import MongoClient
    from config import Config
    from .owner_campaign_live_events import (
        LIVE_ACTIONS, persist_live_event, persist_noncritical_live_event, verify_live_token,
        notify_campaign_channels_after_persist,
    )
    from .owner_campaign_confirmation_page import gradual_option_enabled

    email_lower = str(email or "").strip().casefold()
    if accion not in {"aceptar_rebaja", "contactar_ejecutivo"} or not codigo.isdigit():
        return HTMLResponse("Enlace de campaña inválido.", status_code=404)
    claims = verify_live_token(
        token, campaign_id=campana, property_code=codigo,
        recipient=email_lower, action=accion,
    )
    if claims is None:
        return HTMLResponse("Enlace de campaña inválido o vencido.", status_code=404)
    from .owner_campaign_live_events import decode_live_token
    context_claims = decode_live_token(owner_portal_context_token) if owner_portal_context_token else None
    if owner_portal_context_token and (
        not context_claims or context_claims.get("action") != "owner_portal_interaction"
        or context_claims.get("interaction_surface") != "OWNER_PORTAL"
        or any(context_claims.get(key) != claims.get(key) for key in ("campaign_id", "property_code", "recipient", "source"))
    ):
        return HTMLResponse("Enlace de campaña inválido o vencido.", status_code=404)
    client = MongoClient(Config.MONGO_URI)
    try:
        db = client[Config.DB_NAME]
        ledger = db[Config.COLLECTION_CAMPANAS_LOG]
        row = ledger.find_one({"_id": f"{campana}:{codigo}", "campaign_id": campana, "property_code": codigo})
        if not row or str(row.get("owner_email") or "").casefold() != email_lower:
            return HTMLResponse("Esta acción no está disponible para la propiedad indicada.", status_code=404)
        from owner_portal.monthly import (
            OWNER_PROPERTY_PORTAL_COLLECTION, _as_period, _select_monthly_snapshot,
            owner_property_portal_id,
        )
        from uuid import uuid4
        portal_key = owner_property_portal_id(codigo, email_lower)
        portal_record = db[OWNER_PROPERTY_PORTAL_COLLECTION].find_one({
            "_id": portal_key, "owner_key": portal_key, "property_code": codigo,
        })
        report_snapshot = _select_monthly_snapshot(portal_record, codigo) or {}
        report_period = _as_period(report_snapshot.get("period") or report_snapshot.get("generated_at")) or datetime.now(timezone.utc).strftime("%Y-%m")
        snapshot_hash = str(report_snapshot.get("snapshot_hash") or report_snapshot.get("content_sha256") or "")
        if not snapshot_hash:
            import hashlib, json
            snapshot_hash = hashlib.sha256(json.dumps(dict(report_snapshot or row.get("campaign_snapshot") or {}), sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        claims = {
            **claims, "report_period": report_period, "snapshot_hash": snapshot_hash,
            "session_id": str(uuid4()),
        }
        if context_claims:
            claims.update({
                "report_period": context_claims.get("report_period"),
                "snapshot_hash": context_claims.get("snapshot_hash"),
                "session_id": context_claims.get("session_id"),
            })
        if owner_portal_session_id:
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", owner_portal_session_id):
                return HTMLResponse("Enlace de campaña inválido o vencido.", status_code=404)
            claims["session_id"] = owner_portal_session_id
        if accion == "aceptar_rebaja" and owner_campaign_authorization_is_stale(row):
            from owner_portal.campaign import price_review_message

            return HTMLResponse(
                f"<main><h1>Recomendación en validación</h1><p>{escape(price_review_message('CAMPAIGN_PREPARATION_BLOCKED'))}</p>"
                "<p>No se registró una nueva autorización. Contacta a tu ejecutivo PROCASA.</p></main>",
                status_code=409,
            )
        from campanas import owner_campaign_test_runtime as runtime
        property_doc = db[runtime.PROPERTY_COLLECTION].find_one(
            {"codigo": {"$in": [codigo, int(codigo)] if codigo.isdigit() else [codigo]}},
        ) or {}
        if accion == "aceptar_rebaja":
            from owner_portal.campaign import (
                price_review_message, validate_campaign_price_integrity,
            )

            integrity = validate_campaign_price_integrity(db, row, property_doc=property_doc)
            if integrity.get("required"):
                message = price_review_message(integrity.get("reason"), already_authorized=(
                    str(row.get("authorization_status") or "").upper() == "PRICE_AUTHORIZED"
                ))
                return HTMLResponse(
                    f"<main><h1>Recomendación en validación</h1><p>{escape(message)}</p>"
                    "<p>No se registró una nueva autorización. Contacta a tu ejecutivo PROCASA.</p></main>",
                    status_code=409,
                )
        operation = str(row.get("operation") or runtime.resolve_property_operation(property_doc)).upper()
        if operation not in {"VENTA", "ARRIENDO"}:
            return HTMLResponse("No se pudo verificar la operación de la propiedad.", status_code=409)
        monthly = operation == "ARRIENDO"
        prop_type = str(row.get("property_type") or runtime._property_type(property_doc))
        commune = str(row.get("commune") or runtime._commune(property_doc))
        price_block = runtime.operation_price_block(property_doc, requested_operation=operation) or {}
        current_price = float(row.get("current_price"))
        recommended_price = float(row.get("recommended_price"))
        recommended_pct = int(row.get("recommended_adjustment_pct"))
        current_clp = row.get("current_price_clp") or price_block.get("precio_clp")
        recommended_clp = row.get("recommended_price_clp")
        if recommended_clp is None and current_clp is not None:
            recommended_clp = round(float(current_clp) * (100 - recommended_pct) / 100)
        gradual_pct = row.get("gradual_adjustment_pct")
        gradual_price = row.get("gradual_price")
        if gradual_pct is None:
            from analytics.owner_campaign_email_v2 import calculate_gradual_price_alternative
            gradual = calculate_gradual_price_alternative(
                current_price=current_price, recommended_adjustment_pct=recommended_pct,
            )
            gradual_pct = gradual.get("adjustment_pct")
            if gradual_price is None:
                gradual_price = gradual.get("price")
        if gradual_price is None and gradual_pct is not None:
            from decimal import Decimal, ROUND_HALF_UP
            gradual_price = float((Decimal(str(current_price)) * (Decimal("100") - Decimal(int(gradual_pct))) / Decimal("100")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        gradual_clp = row.get("gradual_price_clp")
        if gradual_clp is None and current_clp is not None and gradual_pct is not None:
            gradual_clp = round(float(current_clp) * (100 - int(gradual_pct)) / 100)

        base = {"email": email_lower, "campana": campana, "codigos": codigo, "mode": "owner_campaign", "token": token}
        if owner_portal_context_token:
            base["owner_portal_context_token"] = owner_portal_context_token
        if owner_portal_session_id:
            base["owner_portal_session_id"] = owner_portal_session_id
        def action_url(action: str, action_token: str, *, selected_type: str = "") -> str:
            query_args = {**base, "accion": action, "token": action_token}
            if selected_type:
                query_args["selected_adjustment_type"] = selected_type
            return (Config.CRM_BASE_URL or "https://www.procasa.cl").rstrip("/") + "/campana/respuesta?" + urlencode(query_args)

        from .owner_campaign_live_events import issue_attributed_followup_token
        advisor_token = issue_attributed_followup_token(
            claims, action="contactar_ejecutivo",
        )
        advisor_url = action_url("contactar_ejecutivo", advisor_token)
        logo_url = (Config.CRM_BASE_URL or "https://www.procasa.cl").rstrip("/") + "/static/logo.png"

        from analytics.owner_campaign_email_v2 import _clp_price_label, _price_label

        def label(value: Any, *, clp: bool = False) -> str:
            if value is None:
                return ""
            return _clp_price_label(value, operation) if clp else _price_label(value, operation)

        recommendation_price = label(recommended_price)
        if recommended_clp is not None:
            recommendation_price += "<br><small>" + escape(label(recommended_clp, clp=True)) + "</small>"
        current_price_label = label(current_price)
        if current_clp is not None:
            current_price_label += "<br><small>" + escape(label(current_clp, clp=True)) + "</small>"
        gradual_price_label = label(gradual_price)
        if gradual_clp is not None:
            gradual_price_label += "<br><small>" + escape(label(gradual_clp, clp=True)) + "</small>"

        if accion == "aceptar_rebaja" and row.get("authorization_status") == "PRICE_AUTHORIZED":
            from .owner_campaign_confirmation_page import render_success_page
            return HTMLResponse(
                render_success_page(
                    selected_type="", selected_pct=None, selected_price="",
                    advisor_url=advisor_url, already_registered=True, logo_url=logo_url,
                ), headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
            )
        if accion == "aceptar_rebaja" and method == "GET":
            persist_noncritical_live_event(db, claims, event="cta_clicked", action=accion)
            persist_noncritical_live_event(db, claims, event="price_confirm_page_opened", action=accion)
            persist_noncritical_live_event(db, claims, event="confirmation_page_opened", action=accion)
            recommended_form = action_url("aceptar_rebaja", token, selected_type="RECOMMENDED")
            from .owner_campaign_confirmation_page import render_decision_page
            page = render_decision_page(
                recommended_pct=recommended_pct, recommended_price=recommendation_price,
                current_price=current_price_label,
                gradual_pct=int(gradual_pct) if gradual_pct is not None else None,
                gradual_price=gradual_price_label,
                recommended_url=recommended_form,
                gradual_url=action_url("aceptar_rebaja", token, selected_type="GRADUAL"),
                advisor_url=advisor_url, logo_url=logo_url,
                gradual_enabled=gradual_option_enabled(campana),
            )
            return HTMLResponse(page, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
        if accion == "aceptar_rebaja" and method != "POST":
            return HTMLResponse("Método no permitido.", status_code=405)
        if accion == "contactar_ejecutivo" and method != "GET":
            return HTMLResponse("Método no permitido.", status_code=405)
        event = LIVE_ACTIONS[accion]
        selected_type = str(selected_adjustment_type or "").upper()
        if event == "price_authorized" and selected_type == "GRADUAL" and not gradual_option_enabled(campana):
            return HTMLResponse("La opción gradual no está habilitada para esta campaña.", status_code=409)
        persist_noncritical_live_event(db, claims, event="cta_clicked", action=accion)
        details = {}
        if event == "price_authorized":
            if selected_type not in {"RECOMMENDED", "GRADUAL"}:
                return HTMLResponse("Selecciona una alternativa válida.", status_code=400)
            if selected_type == "RECOMMENDED":
                selected_pct, selected_price = recommended_pct, recommended_price
                selected_clp = recommended_clp
            elif gradual_pct is not None and int(gradual_pct) != recommended_pct:
                selected_pct, selected_price, selected_clp = int(gradual_pct), float(gradual_price), gradual_clp
            else:
                return HTMLResponse("La opción gradual no está disponible; solicita revisión con tu ejecutivo.", status_code=400)
            details = {
                "selected_adjustment_type": selected_type,
                "selected_adjustment_pct": int(selected_pct),
                "selected_price": float(selected_price),
                "selected_price_clp": float(selected_clp) if selected_clp is not None else None,
                "recommended_adjustment_pct": recommended_pct,
                "recommended_price": recommended_price,
                "previous_price": current_price,
                "authorization_status": "PRICE_AUTHORIZED",
            }
            if gradual_option_enabled(campana):
                details.update({
                    "gradual_adjustment_pct": int(gradual_pct) if gradual_pct is not None else None,
                    "gradual_price": float(gradual_price) if gradual_price is not None else None,
                    "gradual_price_clp": float(gradual_clp) if gradual_clp is not None else None,
                })
        if event == "advisor_review_requested":
            details = {
                "selected_adjustment_type": "ADVISOR_REVIEW",
                "recommended_adjustment_pct": recommended_pct,
                "recommended_price": recommended_price,
            }
        stored, inserted = persist_live_event(db, claims, event=event, action=accion, details=details)
        event_id = __import__("hashlib").sha256(f"{claims.get('event_id')}|{event}".encode()).hexdigest()
        persisted = ledger.find_one({"_id": f"{campana}:{codigo}", "events.event_id": event_id})
        if inserted and persisted:
            notify_campaign_channels_after_persist(db, stored=persisted, event=event, claims=claims)
        from .owner_campaign_confirmation_page import render_success_page
        if event == "price_authorized":
            selected_type = details.get("selected_adjustment_type", "")
            page = render_success_page(
                selected_type=selected_type, selected_pct=details.get("selected_adjustment_pct"),
                selected_price=label(details.get("selected_price")) + (
                    "<br><small>" + escape(label(details.get("selected_price_clp"), clp=True)) + "</small>"
                    if details.get("selected_price_clp") is not None else ""
                ), recommended_pct=recommended_pct,
                recommended_price=recommendation_price,
                logo_url=logo_url,
            ) if inserted else render_success_page(
                selected_type="", selected_pct=None, selected_price="", advisor_url=advisor_url,
                already_registered=True, logo_url=logo_url,
            )
        else:
            page = render_success_page(selected_type="ADVISOR_REVIEW", selected_pct=None, selected_price="", logo_url=logo_url)
        return HTMLResponse(
            page,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
    finally:
        client.close()


async def handle_campana_respuesta(
    request: Request,
    email: str,
    accion: str,
    codigos: str,
    campana: str,
    mode: str = "live",
    token: str = "",
    selected_adjustment_type: str = "",
    owner_portal_context_token: str = "",
    owner_portal_session_id: str = "",
    owner_portal_client_event_id: str = "",
):
    accion = normalize_accion(accion)
    valid = {"aceptar_rebaja", "contactar_ejecutivo", "mantener_precio", "no_disponible", "unsubscribe"}
    if mode == "test":
        valid |= TEST_ACTIONS
    if accion not in valid:
        return HTMLResponse("Accion no valida", status_code=400)

    if request.method.upper() == "POST" and not (
        (mode == "test" and accion == "aceptar_rebaja")
        or (mode == "owner_campaign" and accion == "aceptar_rebaja")
    ):
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

    if mode == "owner_campaign":
        codigos_lista = [c.strip() for c in (codigos or "").split(",") if c.strip() and c.strip() != "N/A"]
        return await asyncio.to_thread(
            _process_owner_campaign_action,
            email=email,
            accion=accion,
            codigo=codigos_lista[0] if len(codigos_lista) == 1 else "",
            campana=campana,
            token=token,
            method=request.method.upper(),
            selected_adjustment_type=selected_adjustment_type,
            owner_portal_context_token=owner_portal_context_token,
            owner_portal_session_id=owner_portal_session_id,
            owner_portal_client_event_id=owner_portal_client_event_id,
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
    if not test_mode_enabled():
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
        campaign_id=decoded["campaign_id"],
        property_code=decoded["property_code"],
        action=private_report.REPORT_ACTION,
        recipient=private_report.TEST_RECIPIENT,
        document_type=decoded["document_type"],
    )
    if claims is None:
        return private_report._response(404, "Documento no disponible.")

    try:
        _record_test_report_event(claims, stage="click")
        return private_report._serve_campaign_report(token)
    except (LookupError, ValueError):
        logger.info("[CAMPAIGN_REPORT_TRACKING] status=prepared_test_row_missing code=%s", decoded["property_code"])
        return private_report._response(404, "Documento no disponible para esta prueba.")
    except Exception as exc:
        logger.error("Error resolviendo documento de test (%s)", type(exc).__name__, exc_info=True)
        return private_report._response(503, "No pudimos consultar el informe en este momento.")


async def handle_campana_informe(*, token: str, owner_portal_context_token: str = "", owner_portal_session_id: str = "", owner_portal_client_event_id: str = ""):
    # Production links use their own signed-token decoder, live campaign ledger,
    # and document resolver. Keep the historical QA path isolated to test tokens.
    if str(token or "").startswith("p1."):
        if not (owner_portal_context_token or owner_portal_session_id or owner_portal_client_event_id):
            return await private_report.handle_campaign_report(token)
        return await private_report.handle_campaign_report(
            token, owner_portal_context_token=owner_portal_context_token,
            owner_portal_session_id=owner_portal_session_id,
            owner_portal_client_event_id=owner_portal_client_event_id,
        )
    return await asyncio.to_thread(_resolve_test_report_response, token=token)
