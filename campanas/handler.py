# campanas/handler.py
import logging
import re
from datetime import datetime
from pymongo import MongoClient
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from config import Config
from .utils import get_accion_config, normalize_accion
from .email_service import enviar_alerta_equipo

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="campanas/templates")


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

    email_lower = (email or "").lower().strip()
    codigos_lista = [c.strip() for c in (codigos or "").split(",") if c.strip() and c.strip() != "N/A"]
    ahora = datetime.utcnow()
    config_accion = get_accion_config(accion)

    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]
        contactos = db[Config.COLLECTION_CONTACTOS]
        respuestas = db[Config.COLLECTION_RESPUESTAS]
        historico = db[Config.COLLECTION_CAMPANAS_LOG]

        contacto = contactos.find_one({"email_propietario": {"$regex": f"^{re.escape(email_lower)}$", "$options": "i"}})
        update_price = contacto.get("update_price", {}) if contacto else {}

        if contacto and update_price.get("campana_nombre") == campana and update_price.get("respuesta") == accion:
            try:
                return templates.TemplateResponse(
                    "base.html",
                    {
                        "request": request,
                        "titulo": config_accion["titulo"],
                        "color": config_accion["color"],
                        "accion": accion.replace("_", " ").title(),
                        "mensaje": config_accion["mensaje"],
                    },
                )
            except Exception:
                return HTMLResponse("Tu respuesta ya estaba registrada previamente.", status_code=200)

        respuestas.update_one(
            {
                "email": email_lower,
                "campana_nombre": campana,
                "accion": accion,
                "codigos_propiedad": codigos_lista,
                "mode": mode,
            },
            {
                "$set": {
                    "fecha_respuesta": ahora,
                    "token": token or "",
                }
            },
            upsert=True,
        )

        if token:
            historico.update_one(
                {"token": token},
                {
                    "$set": {
                        "respuesta_propietario": accion,
                        "respuesta_at": ahora.isoformat(),
                        "respuesta_mode": mode,
                        "respuesta_email": email_lower,
                        "estado_respuesta": "respondido",
                    }
                },
            )

        upd = contactos.update_one(
            {"email_propietario": {"$regex": f"^{re.escape(email_lower)}$", "$options": "i"}},
            {
                "$set": {
                    "update_price.campana_nombre": campana,
                    "update_price.respuesta": accion,
                    "update_price.fecha_respuesta": ahora,
                    "estado": config_accion["estado"],
                    "bloqueo_email": accion in {"no_disponible", "unsubscribe"},
                }
            },
        )
        logger.info(
            "[CAMPANA_RESPUESTA] email=%s campana=%s accion=%s mode=%s matched=%s modified=%s",
            email_lower,
            campana,
            accion,
            mode,
            getattr(upd, "matched_count", 0),
            getattr(upd, "modified_count", 0),
        )

        contacto = contactos.find_one({"email_propietario": {"$regex": f"^{re.escape(email_lower)}$", "$options": "i"}})
        nombre = "Sin nombre"
        telefono = "Sin telefono"
        if contacto:
            nombre = f"{contacto.get('nombre_propietario','')} {contacto.get('apellido_paterno_propietario','')} {contacto.get('apellido_materno_propietario','')}".strip() or "Sin nombre"
            telefono = contacto.get("telefono", "Sin telefono")

        accion_texto = config_accion["titulo"].upper().replace("!", "")
        if mode != "test":
            enviar_alerta_equipo(nombre, telefono, email_lower, codigos_lista, accion_texto, campana)

        try:
            return templates.TemplateResponse(
                "base.html",
                {
                    "request": request,
                    "titulo": config_accion["titulo"],
                    "color": config_accion["color"],
                    "accion": accion.replace("_", " ").title(),
                    "mensaje": config_accion["mensaje"],
                },
            )
        except Exception:
            return HTMLResponse("Tu respuesta fue registrada correctamente.", status_code=200)

    except Exception as e:
        logger.error(f"Error en campana: {e}", exc_info=True)
        return HTMLResponse("Error interno del servidor", status_code=500)
