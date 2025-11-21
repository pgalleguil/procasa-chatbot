# webhook.py - Versión WasenderAPI.com (2025)
import asyncio
import logging
import time
from typing import Dict, Any

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
import requests
import uvicorn

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

app = FastAPI(title="Procasa WhatsApp WasenderAPI", version="2.1")

# ========================= DEBOUNCE (igual que antes) =========================
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

    # ←←←← AQUÍ EMPIEZA delayed_process ←←←←
    async def delayed_process():
        await asyncio.sleep(DEBOUNCE_SECONDS)   # espera 5 segundos

        # Si llegó otro mensaje mientras esperaba, se cancela y empieza de nuevo
        if time.time() - last_message_time.get(phone, 0) < DEBOUNCE_SECONDS - 0.1:
            logger.info(f"[DEBOUNCE] Nuevo mensaje → reinicia para {phone}")
            return

        final_message = accumulated_messages.get(phone, "").strip()
        if not final_message:
            return

        logger.info(f"[PROCESS] Procesando: {phone} → {final_message[:80]}...")

        try:
            bot_response = process_user_message(phone, final_message)
            if bot_response and bot_response.strip():
                await send_whatsapp_message(phone, bot_response)   # ← AQUÍ SE ENVÍA LA RESPUESTA
            else:
                logger.warning(f"No hay respuesta del bot para {phone}")
        except Exception as e:
            logger.error(f"Error procesando {phone}: {e}", exc_info=True)
        finally:
            pending_tasks.pop(phone, None)
            accumulated_messages.pop(phone, None)
            last_message_time.pop(phone, None)

    # Crea y guarda la tarea
    task = asyncio.create_task(delayed_process())
    pending_tasks[phone] = task


async def send_whatsapp_message(number: str, text: str):
    """Envía mensaje usando WasenderAPI.com - con reintentos y logs claros"""
    url = "https://wasenderapi.com/api/send-message"  # directo, por si la config falla
    payload = {
        "to": number,      # acepta +569...
        "text": text
    }
    headers = {
        "Authorization": f"Bearer {config.WASENDER_TOKEN}",
        "Content-Type": "application/json"
    }

    logger.info(f"[WHATSAPP →] Intentando enviar a {number}: {text[:50]}...")

    for intento in range(3):  # 3 intentos máximo
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=20)
            if response.status_code == 200:
                logger.info(f"[WHATSAPP ✓] ¡Enviado perfecto a {number}! ID: {response.json().get('message_id', 'N/A')}")
                return True
            else:
                logger.error(f"[WHATSAPP ✗] Error {response.status_code}: {response.text}")
        except Exception as e:
            logger.error(f"[WHATSAPP ✗] Excepción intento {intento+1}: {e}")

        await asyncio.sleep(2)

    logger.error(f"[WHATSAPP ✗] Falló envío definitivo a {number}")
    return False


# ========================= ENDPOINTS =========================

@app.get("/")
async def root():
    return {"status": "Procasa + WasenderAPI activo 24/7", "time": time.strftime("%Y-%m-%d %H:%M:%S")}


@app.post("/webhook")
async def webhook(request: Request, x_webhook_secret: str = Header(None, alias="X-Webhook-Secret")):
    # === Seguridad ===
    if config.WASENDER_WEBHOOK_SECRET and x_webhook_secret != config.WASENDER_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Secret inválido")

    raw_data = await request.json()
    
    # DEBUG temporal (puedes borrarlo después)
    logger.info(f"[DEBUG WEBHOOK] Payload completo: {raw_data}")

    # === Extracción 100% compatible con WasenderAPI gratuito ===
    try:
        data = raw_data.get("data", {})
        messages = data.get("messages", {}) or {}
        
        # Número de teléfono (el campo más confiable)
        phone = messages.get("key", {}).get("cleanedSenderPn", "")
        if not phone:
            phone = messages.get("key", {}).get("senderPn", "").split("@")[0]
        
        # Texto del mensaje
        text = messages.get("messageBody", "") or messages.get("message", {}).get("conversation", "")

        if not phone or not text:
            logger.warning(f"No se encontró teléfono o texto → {raw_data}")
            return JSONResponse({"status": "ignored"})

        # Normalizar +56 si falta
        if phone.startswith("56") and not phone.startswith("+"):
            phone = "+" + phone
        elif not phone.startswith("+"):
            phone = "+56" + phone  # Wasender a veces quita el +56

        logger.info(f"✓ Mensaje recibido de {phone}: {text}")

        # === Debounce + acumulación ===
        current = accumulated_messages.get(phone, "")
        nuevo = (current + "\n" + text).strip() if current else text
        accumulated_messages[phone] = nuevo
        last_message_time[phone] = time.time()

        asyncio.create_task(process_with_debounce(phone, nuevo))

        return JSONResponse({"status": "ok", "processed": True})

    except Exception as e:
        logger.error(f"Error parseando webhook: {e}", exc_info=True)
        return JSONResponse({"status": "error"}, status_code=500)


@app.get("/health")
async def health_check():
    return {"status": "healthy", "pending": len(pending_tasks)}

if __name__ == "__main__":
    #logger.info("Iniciando Procasa WhatsApp con WasenderAPI en puerto 11422...")
    uvicorn.run("webhook:app", host="0.0.0.0", port=11422, reload=False, workers=1, log_level="info")