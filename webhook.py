# webhook.py - VERSIÓN FINAL PRO PAGADA - NOVIEMBRE 2025
import asyncio
import logging
import time
import hmac
import hashlib
from typing import Dict, Any

import requests
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
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

    # Un solo reintento rápido (por si fue un blip)
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
    
    # Verificación de firma
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

    # Normalización
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

@app.get("/campana/respuesta", response_class=HTMLResponse)
def campana_respuesta(
    email: str = Query(..., description="Email del propietario"),
    accion: str = Query(..., description="'ajuste', 'llamada' o 'baja'"),
    codigos: str = Query("N/A", description="Códigos de propiedad")
):
    """
    Ruta que se activa cuando el propietario hace clic en un botón del email.
    Actualiza MongoDB inmediatamente.
    """
    if not MONGO_URI:
        return HTMLResponse(content=HTML_ERROR.format(error="MONGO_URI no configurado."), status_code=500)
    
    if accion not in ["ajuste", "llamada", "baja"]:
        return HTMLResponse(content=HTML_ERROR.format(error="Acción inválida."), status_code=400)

    try:
        # 1. Conexión a MongoDB
        client = MongoClient(MONGO_URI)
        db = client[DB_NAME]
        collection = db[COLLECTION_NAME]
        
        # 2. Preparación de la Actualización y Mensaje de Cliente
        update_data = {
            f"campanas.{NOMBRE_CAMPANA}.respuesta_cliente": accion,
            f"campanas.{NOMBRE_CAMPANA}.fecha_respuesta": datetime.utcnow(),
            "ultima_accion": f"webhook_{accion}",
            f"campanas.{NOMBRE_CAMPANA}.test_ejecutado": False 
        }
        
        # Lógica para manejar la acción
        if accion == "ajuste":
            update_data["estado"] = "ajuste_autorizado"
            mensaje_cliente = "¡Autorización recibida! Hemos registrado su solicitud para aplicar el ajuste de precio del 7% y actualizar el portal inmediatamente."
            
        elif accion == "llamada":
            update_data["estado"] = "pendiente_llamada"
            mensaje_cliente = "¡Solicitud de contacto registrada! Un ejecutivo revisará su solicitud y le llamará a la brevedad."
            
        elif accion == "baja":
            update_data["estado"] = "baja_solicitada"
            mensaje_cliente = "Su solicitud de baja ha sido registrada. Procederemos a archivar sus propiedades y a eliminar su correo de futuras campañas."
            # Marcamos baja general para futuras campañas, si aplica en tu esquema
            update_data["estado_general"] = "no_contactar" 

        # 3. Ejecutar la Actualización
        # Buscamos todas las propiedades asociadas a ese email que fueron marcadas como enviadas en ESTA campaña
        query = {
            "email_propietario": email,
            f"campanas.{NOMBRE_CAMPANA}.enviado": True 
        }

        result = collection.update_many(
            query,
            {"$set": update_data}
        )

        # 4. Respuesta al Cliente
        if result.modified_count > 0:
            html_final = HTML_CONFIRMACION.format(
                accion=accion.title(), 
                email=email, 
                mensaje=mensaje_cliente,
                codigos=codigos
            )
        else:
            mensaje_re_respuesta = "Su respuesta ha sido registrada previamente. Gracias por su colaboración."
            html_final = HTML_CONFIRMACION.format(
                accion=accion.title(), 
                email=email, 
                mensaje=mensaje_re_respuesta,
                codigos=codigos
            )
            
        return HTMLResponse(content=html_final, status_code=200)

    except Exception as e:
        # Manejo de errores de conexión o DB
        error_msg = str(e).replace('"', '`').replace("'", "`")
        return HTMLResponse(content=HTML_ERROR.format(error=error_msg, email=email), status_code=500)


# ====================================================================
# PLANTILLAS HTML PARA LA RESPUESTA AL CLIENTE (FastAPI compatible)
# NOTA: Usamos .format() en lugar de Jinja/render_template_string
# ====================================================================

HTML_CONFIRMACION = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Respuesta Registrada - Procasa</title>
    <style>
        body {{ font-family: Arial, sans-serif; text-align: center; padding: 50px; background-color: #f4f4f9; }}
        .box {{ background-color: #fff; padding: 40px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1); max-width: 500px; margin: 0 auto; }}
        .success {{ color: #10b981; font-size: 24px; margin-bottom: 20px; font-weight: bold;}}
        .action {{ font-size: 18px; margin-bottom: 10px; color: #333; }}
        .footer-note {{ font-size: 12px; color: #777; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="box">
        <div class="success">🎉 {accion} Registrada</div>
        <p class="action">Propiedades: {codigos}</p>
        <p>{mensaje}</p>
        <p class="footer-note">Hemos actualizado el estado de sus propiedades en nuestra base de datos. Email asociado: {email}.</p>
    </div>
</body>
</html>
"""

HTML_ERROR = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Error de Procesamiento - Procasa</title>
    <style>
        body {{ font-family: Arial, sans-serif; text-align: center; padding: 50px; background-color: #fef2f2; }}
        .box {{ background-color: #fff; padding: 40px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1); max-width: 500px; margin: 0 auto; border: 1px solid #dc2626; }}
        .error {{ color: #dc2626; font-size: 24px; margin-bottom: 20px; font-weight: bold;}}
    </style>
</head>
<body>
    <div class="box">
        <div class="error">⚠️ Error al procesar su solicitud</div>
        <p>Disculpe, ocurrió un problema técnico al intentar registrar su respuesta.</p>
        <p>Por favor, responda directamente al correo que recibió para que un ejecutivo pueda asistirle.</p>
        <p style="font-size: 10px; color: #999;">Detalle Técnico: {error}</p>
    </div>
</body>
</html>
"""


if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 8001))
    reload_mode = port == 8001
    logger.info(f"Bot PRO iniciado en puerto {port} - MÚLTIPLES USUARIOS ACTIVADO")
    uvicorn.run("webhook:app", host="0.0.0.0", port=port, reload=reload_mode, log_level="info")