# webhook.py - VERSIÓN FINAL PRO PAGADA - DICIEMBRE 2025 (100% FUNCIONAL)
import asyncio
import logging
import time
import hmac
import hashlib
from typing import Dict, Any

import requests
from fastapi import FastAPI, Request, HTTPException, Header, Query
from fastapi.responses import JSONResponse, HTMLResponse
from pymongo import MongoClient
from datetime import datetime
import uvicorn
import json

from config import Config
from chatbot import process_user_message

# ========================= CONFIG & LOGGER =========================
config = Config()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("procasa-wasender")

app = FastAPI(title="Procasa WhatsApp Bot - PRO PAGADO 2025")

# ========================= DEBOUNCE (por usuario) =========================
pending_tasks: Dict[str, Any] = {}
last_message_time: Dict[str, float] = {}
accumulated_messages: Dict[str, str] = {}
DEBOUNCE_SECONDS = 5.0


async def process_with_debounce(phone: str, full_text: str):
    if phone in pending_tasks and not pending_tasks[phone].done():
        pending_tasks[phone].cancel()
        logger.info(f"[DEBOUNCE] Tarea anterior cancelada para {phone}")

    accumulated_messages[phone] = full_text.strip()
    last_message_time[phone] = time.time()

    async def delayed_process():
        await asyncio.sleep(DEBOUNCE_SECONDS)

        if time.time() - last_message_time.get(phone, 0) < DEBOUNCE_SECONDS - 0.1:
            return

        final_message = accumulated_messages.pop(phone, "").strip()
        if not final_message:
            return

        logger.info(f"[PROCESS] Procesando mensaje de {phone}: {final_message[:80]}...")

        try:
            bot_response = process_user_message(phone, final_message)
            if bot_response and bot_response.strip():
                await send_whatsapp_message(phone, bot_response)
            else:
                logger.warning(f"No hay respuesta del bot para {phone}")
        except Exception as e:
            logger.error(f"Error procesando {phone}: {e}", exc_info=True)
        finally:
            pending_tasks.pop(phone, None)

    task = asyncio.create_task(delayed_process())
    pending_tasks[phone] = task


async def send_whatsapp_message(number: str, text: str):
    url = "https://wasenderapi.com/api/send-message"
    payload = {"to": number, "text": text}
    headers = {
        "Authorization": f"Bearer {config.WASENDER_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            logger.info(f"[WHATSAPP SUCCESS] Enviado a {number}")
            return True
        else:
            logger.error(f"[WHATSAPP ERROR] {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"[WHATSAPP EXCEPTION] Error enviando a {number}: {e}")

    await asyncio.sleep(2)
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            logger.info(f"[WHATSAPP SUCCESS] Enviado en 2do intento a {number}")
            return True
        else:
            logger.error(f"[WHATSAPP ERROR] Falló 2do intento: {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.error(f"[WHATSAPP EXCEPTION] 2do intento falló: {e}")

    return False


@app.get("/")
async def root():
    return {"status": "Procasa Bot PRO ACTIVO - WasenderAPI PAGADO", "time": time.strftime("%Y-%m-%d %H:%M:%S")}


@app.post("/webhook")
async def webhook(request: Request, x_webhook_signature: str = Header(None, alias="X-Webhook-Signature")):
    raw_body = await request.body()
    
    if config.WASENDER_WEBHOOK_SECRET:
        expected_signature = hmac.new(
            config.WASENDER_WEBHOOK_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_signature, x_webhook_signature or ""):
            logger.warning("FIRMA INVÁLIDA - Acceso denegado")
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        logger.error(f"JSON inválido: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info("[WEBHOOK] Mensaje recibido")

    if data.get("event") == "webhook.test":
        logger.info("TEST WEBHOOK EXITOSO")
        return JSONResponse({"ok": True}, status_code=200)

    messages_data = data.get("data", {}).get("messages", {}) or {}

    phone = (
        messages_data.get("key", {}).get("cleanedSenderPn") or
        messages_data.get("key", {}).get("senderPn", "").split("@")[0] or
        messages_data.get("from", "").split("@")[0] or ""
    ).strip()

    text = (
        messages_data.get("messageBody") or
        messages_data.get("message", {}).get("conversation") or
        messages_data.get("message", {}).get("extendedTextMessage", {}).get("text", "") or
        ""
    ).strip()

    if not phone or not text:
        return JSONResponse({"status": "ignored"}, status_code=200)

    if phone.startswith("56") and not phone.startswith("+"):
        phone = "+" + phone
    if not phone.startswith("+"):
        phone = "+56" + phone.lstrip("0")

    logger.info(f"Mensaje de {phone}: {text[:100]}")

    current = accumulated_messages.get(phone, "")
    nuevo_texto = (current + "\n" + text).strip() if current else text
    accumulated_messages[phone] = nuevo_texto
    last_message_time[phone] = time.time()

    asyncio.create_task(process_with_debounce(phone, nuevo_texto))

    return JSONResponse({"ok": True}, status_code=200)


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "active_conversations": len(pending_tasks),
        "uptime": time.strftime("%Y-%m-%d %H:%M:%S")
    }

# ====================== ENDPOINT CAMPAÑA EMAIL - 100% FUNCIONAL ======================
@app.get("/campana/respuesta", response_class=HTMLResponse)
def campana_respuesta(
    email: str = Query(..., description="Email del propietario"),
    accion: str = Query(..., description="ajuste_7 / llamada / mantener / no_disponible / unsubscribe"),
    codigos: str = Query("N/A", description="Códigos de propiedades"),
    campana: str = Query(..., description="Nombre de la campaña")
):
    # Validar acción
    if accion not in ["ajuste_7", "llamada", "mantener", "no_disponible", "unsubscribe"]:
        return HTMLResponse("Acción no válida", status_code=400)

    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]
        contactos = db[Config.COLLECTION_CONTACTOS]
        respuestas = db[Config.COLLECTION_RESPUESTAS]

        ahora = datetime.utcnow()
        email_lower = email.lower().strip()
        codigos_lista = [c.strip() for c in codigos.split(",") if c.strip() and c.strip() != "N/A"]

        # 1. Guardar en histórico
        respuestas.insert_one({
            "email": email_lower,
            "campana_nombre": campana,
            "accion": accion,
            "codigos_propiedad": codigos_lista,
            "fecha_respuesta": ahora,
            "canal_origen": "email"
        })

        # 2. Actualizar contacto
        update = {
            "$set": {
                f"update_price.{campana}.fecha_respuesta": ahora,
                f"update_price.{campana}.respuesta": accion,
                f"update_price.{campana}.accion_elegida": accion
            }
        }

        if accion == "ajuste_7":
            update["$set"].update({
                "estado": "ajuste_autorizado",
                "ultima_accion": f"ajuste_7_{campana}",
                "bloqueo_email": False
            })
            titulo = "¡Autorización recibida!"
            mensaje = """Ya realizamos la actualización del precio de tu propiedad en Procasa.

            El nuevo valor se verá reflejado en los portales inmobiliarios dentro de aproximadamente 72 horas, dependiendo de los tiempos de sincronización de cada sitio.

            Si necesitas realizar otro ajuste o revisar alguna estrategia de visibilidad, quedaremos atentos"""

            color = "#10b981"

        elif accion == "llamada":
            update["$set"].update({
                "estado": "pendiente_llamada",
                "ultima_accion": f"llamada_{campana}",
                "bloqueo_email": False
            })
            titulo = "¡Solicitud recibida!"
            mensaje = """Perfecto, derivamos tu solicitud para que un ejecutivo de Procasa se ponga en contacto contigo.

            El equipo revisará tu caso y te llamarán dentro de las próximas 24 a 48 horas, según disponibilidad.

            Quedaremos atentos si necesitas algo adicional mientras tanto."""

            color = "#3b82f6"

        elif accion == "mantener":
            update["$set"].update({
                "estado": "precio_mantenido",
                "ultima_accion": f"mantener_{campana}",
                "bloqueo_email": False
            })
            titulo = "Precio mantenido"
            mensaje = """Perfecto, dejamos el precio de tu propiedad tal como está.

            Seguiremos monitoreando el comportamiento del mercado para evaluar futuras oportunidades de ajuste si fuese necesario.

            Quedaremos atentos ante cualquier consulta o cambio que quieras realizar."""

            color = "#f59e0b"

        elif accion == "no_disponible":
            update["$set"].update({
                "estado": "no_disponible",
                "bloqueo_email": True,
                "ultima_accion": f"no_disponible_{campana}"
            })
            titulo = "Entendido"
            mensaje = """Perfecto, dejamos marcada tu propiedad como No Disponible en nuestro sistema.

            Si en el futuro cuentas con otra propiedad para vender o arrendar, estaremos encantados de ayudarte con la gestión y apoyarte en todo el proceso.

            Quedaremos atentos a cualquier cosa que necesites."""

            color = "#ef4444"

        else:  # unsubscribe
            update["$set"].update({
                "bloqueo_email": True,
                "estado": "suscripcion_anulada",
                "ultima_accion": "unsubscribe"
            })
            titulo = "Suscripción anulada"
            mensaje = """Perfecto, hemos procesado tu solicitud y quedaste desinscrito de nuestras comunicaciones comerciales.

            Gracias por habernos permitido mantenerte informado. Si en algún momento deseas volver a recibir novedades o necesitas apoyo con una propiedad, estaremos encantados de ayudarte."""

            color = "#6b7280"

        contactos.update_one({"email_propietario": email_lower}, update, upsert=False)

        # 3. Página de confirmación
        html = f"""
        <!DOCTYPE html>
        <html lang="es">
        <head>
            <meta charset="UTF-8">
            <title>Procasa - Confirmación</title>
            <style>
                body {{font-family:Arial,sans-serif;background:#f9fafb;padding:40px;margin:0}}
                .container {{max-width:520px;margin:auto;background:white;border-radius:16px;overflow:hidden;box-shadow:0 10px 25px rgba(0,0,0,0.1)}}
                .header {{background:{color};color:white;padding:30px;text-align:center}}
                .header h1 {{margin:0;font-size:24px;font-weight:600}}
                .content {{padding:40px 30px;text-align:center;color:#374151}}
                .content p {{font-size:16px;line-height:1.6;margin:16px 0}}
                .footer {{background:#f1f5f9;padding:20px;text-align:center;font-size:12px;color:#94a3b8}}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header"><h1>{titulo}</h1></div>
                <div class="content">
                    <p><strong>{accion.replace('_', ' ').title()}</strong></p>
                    <p>{mensaje}</p>
                </div>
                <div class="footer">
                    © 2025 Procasa • Pablo Caro y equipo
                </div>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(html)

    except Exception as e:
        logger.error(f"Error en /campana/respuesta: {e}")
        return HTMLResponse("Error interno del servidor.", status_code=500)


# ====================== ARRANQUE ======================
if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 8001))
    reload_mode = port == 8001
    logger.info(f"Bot PRO iniciado en puerto {port} - MÚLTIPLES USUARIOS ACTIVADO")
    uvicorn.run("webhook:app", host="0.0.0.0", port=port, reload=reload_mode, log_level="info")