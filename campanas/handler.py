# campanas/handler.py
import asyncio
import logging
import re
from datetime import datetime

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pymongo import MongoClient

from config import Config
from .email_service import enviar_alerta_equipo
from .utils import get_accion_config, normalize_accion

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

    # Bloqueo fuerte: 1 respuesta por email+campaña
    if update_price.get("campana_nombre") == campana and update_price.get("respuesta"):
        return {
            "status_code": 200,
            "titulo": "Respuesta ya registrada",
            "color": "#6b7280",
            "accion_label": "Registro completo",
            "mensaje": "Ya registramos una respuesta previa para esta campaña. Si desea cambiar su decisión, contacte a su asesor.",
        }

    # Token obligatorio fuera de test; además bloqueo atómico por token para doble click
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
        # Compatibilidad temporal: si el token está en colecciones históricas antiguas
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
                "titulo": "Enlace inválido",
                "color": "#6b7280",
                "accion_label": "Token inválido",
                "mensaje": "Enlace inválido o expirado. Solicite un nuevo enlace a su asesor.",
            }
    elif mode != "test":
        logger.warning("[CAMPANA_RESPUESTA_SIN_TOKEN_BLOQUEADA] email=%s campana=%s accion=%s", email_lower, campana, accion)
        return {
            "status_code": 400,
            "titulo": "Enlace inválido",
            "color": "#6b7280",
            "accion_label": "Sin token",
            "mensaje": "Este enlace no es válido para respuesta automática. Contacte a su asesor.",
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
    if accion not in valid:
        return HTMLResponse("Accion no valida", status_code=400)

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
