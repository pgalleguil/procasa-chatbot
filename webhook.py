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
    # (exactamente igual que tenías, solo lo copio para que quede completo)
    if phone in pending_tasks and not pending_tasks[phone].done():
        pending_tasks[phone].cancel()
        logger.info(f"[DEBOUNCE] Tarea anterior cancelada para {phone}")

    accumulated_messages[phone] = full_text.strip()
    last_message_time[phone] = time.time()

    async def delayed_process():
        await asyncio.sleep(DEBOUNCE_SECONDS)
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
                await send_whatsapp_message(phone, bot_response)
        except Exception as e:
            logger.error(f"[ERROR] procesando mensaje de {phone}: {e}", exc_info=True)
        finally:
            pending_tasks.pop(phone, None)
            accumulated_messages.pop(phone, None)
            last_message_time.pop(phone, None)

    task = asyncio.create_task(delayed_process())
    pending_tasks[phone] = task


async def send_whatsapp_message(number: str, text: str):
    """Envío con WasenderAPI.com"""
    url = f"{config.WASENDER_BASE_URL}/send-message"
    payload = {
        "to": number,        # Wasender acepta con o sin +, pero con + funciona perfecto
        "text": text
    }
    headers = {
        "Authorization": f"Bearer {config.WASENDER_TOKEN}",
        "Content-Type": "application/json"
    }

    for intento in range(config.MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            if resp.status_code == 200:
                logger.info(f"[WHATSAPP ✓] Enviado a {number} | {resp.json().get('message_id', '')}")
                return
            else:
                logger.error(f"[WHATSAPP ✗] Error {resp.status_code}: {resp.text}")
        except Exception as e:
            logger.error(f"[WHATSAPP ✗] Excepción intento {intento+1}: {e}")

        if intento < config.MAX_RETRIES:
            await asyncio.sleep(2)


# ========================= ENDPOINTS =========================

@app.get("/")
async def root():
    return {"status": "Procasa + WasenderAPI activo 24/7", "time": time.strftime("%Y-%m-%d %H:%M:%S")}


@app.post("/webhook")
async def webhook(request: Request, x_webhook_secret: str = Header(None, alias="X-Webhook-Secret")):
    # Seguridad
    if config.WASENDER_WEBHOOK_SECRET and x_webhook_secret != config.WASENDER_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Secret inválido")

    raw_data = await request.json()
    
    # DEBUG: ver exactamente qué llega (solo en pruebas)
    logger.info(f"[DEBUG WEBHOOK] Payload completo: {raw_data}")

    # WasenderAPI puede enviar de muchas formas → lo normalizamos todo aquí
    data = raw_data
    
    # Caso 1: viene envuelto en "data"
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]
    
    # Caso 2: viene en array (raro pero pasa)
    if isinstance(data, list) and len(data) > 0:
        data = data[0]

    # Extraer teléfono y mensaje con múltiples nombres posibles
    phone = (data.get("from") or data.get("From") or data.get("sender") or data.get("phone") or "").strip()
    text = (data.get("message") or data.get("text") or data.get("body") or data.get("content") or "").strip()

    if not phone or not text:
        logger.warning(f"Payload sin teléfono o texto válido → {raw_data}")
        return JSONResponse({"status": "ignored"})

    # Normalizar +56
    if not phone.startswith("+"):
        phone = "+" + phone

    logger.info(f"Mensaje recibido de {phone}: {text[:100]}")

    # Debounce (igual que antes)
    current = accumulated_messages.get(phone, "")
    nuevo = (current + "\n" + text).strip() if current else text
    accumulated_messages[phone] = nuevo
    last_message_time[phone] = time.time()

    asyncio.create_task(process_with_debounce(phone, nuevo))

    return JSONResponse({"status": "ok"})


@app.get("/health")
async def health_check():
    return {"status": "healthy", "pending": len(pending_tasks)}

if __name__ == "__main__":
    logger.info("Iniciando Procasa WhatsApp con WasenderAPI en puerto 11422...")
    uvicorn.run("webhook:app", host="0.0.0.0", port=11422, reload=False, workers=1, log_level="info")