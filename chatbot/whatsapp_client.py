
import logging
import asyncio
import requests
from config import Config

logger = logging.getLogger(__name__)

async def send_whatsapp_message(number: str, text: str) -> bool:
    """
    Envía un mensaje de WhatsApp usando la API configurada (WASender).
    Maneja reintentos simples.
    """
    if not text:
        return False
        
    # Tratamiento de destinatario
    if "@" in number:
        # Es un JID de grupo o canal, lo dejamos tal cual
        clean = number.strip()
    else:
        # Es un número individual, aplicamos limpieza y normalización para Chile
        clean = "".join(filter(str.isdigit, number))
        if len(clean) == 9 and clean.startswith("9"):
            clean = "569" + clean
        elif len(clean) == 12 and clean.startswith("569"):
            # Si ya tiene el 569, nos aseguramos que Wasender reciba el formato que espera
            # (Algunos proveedores prefieren con +, otros sin. WASender suele aceptar ambos pero probamos sin + primero)
            pass

    url = f"{Config.WASENDER_BASE_URL}/send-message"
    payload = {"to": clean, "text": text}
    headers = {
        "Authorization": f"Bearer {Config.WASENDER_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        # Intento 1
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200 and resp.json().get("success"):
            logger.info(f"Enviado correctamente a {clean}")
            return True
        else:
            logger.warning(f"Fallo envío 1 a {clean}: {resp.text}")
    except Exception as e:
        logger.error(f"Excepción envío 1 a {clean}: {e}")

    # Reintento tras espera
    await asyncio.sleep(2)
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            logger.info(f"Enviado en reintento a {clean}")
            return True
    except Exception as e:
        logger.error(f"Excepción reintento a {clean}: {e}")
        
    return False
