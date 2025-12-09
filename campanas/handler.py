# campanas/handler.py
import logging
import re
from datetime import datetime
from pymongo import MongoClient
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from config import Config
from .utils import get_accion_config
from .email_service import enviar_alerta_equipo

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="campanas/templates")

async def handle_campana_respuesta(email: str, accion: str, codigos: str, campana: str):
    if accion not in ["ajuste_7", "llamada", "mantener", "no_disponible", "unsubscribe"]:
        return HTMLResponse("Acción no válida", status_code=400)

    email_lower = email.lower().strip()
    codigos_lista = [c.strip() for c in codigos.split(",") if c.strip() and c.strip() != "N/A"]
    ahora = datetime.utcnow()
    config_accion = get_accion_config(accion)

    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]
        contactos = db[Config.COLLECTION_CONTACTOS]
        respuestas = db[Config.COLLECTION_RESPUESTAS]

        # 1. Guardar respuesta
        respuestas.insert_one({
            "email": email_lower, "campana_nombre": campana, "accion": accion,
            "codigos_propiedad": codigos_lista, "fecha_respuesta": ahora
        })

        # 2. Actualizar contacto
        contactos.update_one(
            {"email_propietario": {"$regex": f"^{re.escape(email_lower)}$", "$options": "i"}},
            {"$set": {
                "update_price.campana_nombre": campana,
                "update_price.respuesta": accion,
                "update_price.fecha_respuesta": ahora,
                "estado": config_accion["estado"],
                "bloqueo_email": accion in ["no_disponible", "unsubscribe"]
            }}
        )

        # 3. Enviar alerta al equipo
        contacto = contactos.find_one({"email_propietario": {"$regex": f"^{re.escape(email_lower)}$", "$options": "i"}})
        nombre = "Sin nombre"
        telefono = "Sin teléfono"
        if contacto:
            nombre = f"{contacto.get('nombre_propietario','')} {contacto.get('apellido_paterno_propietario','')} {contacto.get('apellido_materno_propietario','')}".strip() or "Sin nombre"
            telefono = contacto.get("telefono", "Sin teléfono")

        accion_texto = config_accion["titulo"].upper().replace("!", "")
        enviar_alerta_equipo(nombre, telefono, email_lower, codigos_lista, accion_texto, campana)

        # 4. Respuesta al cliente
        return templates.TemplateResponse("base.html", {
            "request": None,
            "titulo": config_accion["titulo"],
            "color": config_accion["color"],
            "accion": accion.replace("_", " ").title(),
            "mensaje": config_accion["mensaje"]
        })

    except Exception as e:
        logger.error(f"Error en campaña: {e}", exc_info=True)
        return HTMLResponse("Error interno del servidor", status_code=500)