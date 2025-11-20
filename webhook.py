# webhook.py
import asyncio
import logging
import time
from typing import Dict, Any
from fastapi import FastAPI, Request, HTTPException
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
logger = logging.getLogger("procasa-webhook")

app = FastAPI(title="Procasa WhatsApp Webhook", version="2.0")

# ========================= DEBOUNCE PER PHONE =========================
# Diccionario global: phone → tarea pendiente + timestamp del último mensaje recibido
pending_tasks: Dict[str, Any] = {}  # phone → asyncio.Task
last_message_time: Dict[str, float] = {}  # phone → timestamp
accumulated_messages: Dict[str, str] = {}  # phone → mensajes concatenados

DEBOUNCE_SECONDS = 5.0


async def process_with_debounce(phone: str, full_text: str):
    """Procesa el mensaje (o los acumulados) después del debounce"""
    # 1. Cancelar tarea anterior si existe
    if phone in pending_tasks and not pending_tasks[phone].done():
        pending_tasks[phone].cancel()
        logger.info(f"[DEBOUNCE] Tarea anterior cancelada para {phone}")

    # 2. Limpiar acumulador y tiempo
    accumulated_messages[phone] = full_text.strip()
    last_message_time[phone] = time.time()

    # 3. Crear nueva tarea con delay
    async def delayed_process():
        await asyncio.sleep(DEBOUNCE_SECONDS)

        # Verificar si llegó otro mensaje en estos 5 segundos
        if time.time() - last_message_time.get(phone, 0) < DEBOUNCE_SECONDS - 0.1:
            logger.info(f"[DEBOUNCE] Nuevo mensaje detectado durante espera → se reinicia para {phone}")
            return

        # Tomar mensaje final acumulado
        final_message = accumulated_messages.get(phone, "").strip()
        if not final_message:
            return

        logger.info(f"[PROCESS] Procesando mensaje final de {phone}: {final_message[:80]}...")

        try:
            bot_response = process_user_message(phone, final_message)
            if bot_response and bot_response.strip():
                await send_whatsapp_message(phone, bot_response)
            else:
                logger.warning(f"[PROCESS] No se generó respuesta para {phone}")
        except Exception as e:
            logger.error(f"[ERROR] Fallo al procesar mensaje de {phone}: {e}", exc_info=True)
        finally:
            # Limpiar estado
            pending_tasks.pop(phone, None)
            accumulated_messages.pop(phone, None)
            last_message_time.pop(phone, None)

    # Guardar tarea
    task = asyncio.create_task(delayed_process())
    pending_tasks[phone] = task


async def send_whatsapp_message(number: str, text: str):
    """Envía mensaje vía ApiChat.io"""
    send_url = f"{config.APICHAT_BASE_URL}/sendText"
    payload = {
        "number": number.replace("+", ""),  # ApiChat espera sin +
        "text": text
    }
    headers = {
        "client-id": str(config.APICHAT_CLIENT_ID),
        "token": config.APICHAT_TOKEN,
        "accept": "application/json",
        "Content-Type": "application/json"
    }

    try:
        resp = requests.post(send_url, json=payload, headers=headers, timeout=config.APICHAT_TIMEOUT)
        if resp.status_code == 200:
            msg_id = resp.json().get("id", "N/A")
            logger.info(f"[WHATSAPP] Mensaje enviado a {number} | ID: {msg_id}")
        else:
            logger.error(f"[WHATSAPP] Error envío {number}: {resp.status_code} → {resp.text}")
    except Exception as e:
        logger.error(f"[WHATSAPP] Excepción enviando a {number}: {e}", exc_info=True)


# ========================= ENDPOINTS =========================

@app.get("/")
async def root():
    return {"status": "Procasa WhatsApp Bot activo 24/7", "time": time.strftime("%Y-%m-%d %H:%M:%S")}


@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        logger.error(f"[WEBHOOK] JSON inválido: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # ===========================
    # SOPORTE COMPLETO APICHAT.IO (2025)
    # ===========================
    messages_array = data.get("messages") or data.get("message") or []

    if not messages_array or not isinstance(messages_array, list):
        logger.warning(f"[WEBHOOK] Payload sin mensajes válidos: {data}")
        return JSONResponse({"status": "ignored"})

    # Procesar cada mensaje del array (normalmente viene solo 1)
    for msg in messages_array:
        text = msg.get("text", "").strip()
        msg_type = msg.get("type", "")

        # Solo procesamos mensajes de texto del cliente
        if not text or msg_type != "text" or msg.get("from_me") is True:
            continue

        raw_number = msg.get("number") or msg.get("from") or msg.get("author", "")
        if not raw_number:
            continue

        # Normalizar número
        phone = raw_number.strip()
        if not phone.startswith("+"):
            phone = f"+{phone}"

        logger.info(f"[WEBHOOK] Mensaje recibido de {phone}: {text[:100]}")

        # === DEBOUNCE + ACUMULACIÓN ===
        current_accum = accumulated_messages.get(phone, "")
        new_accum = (current_accum + "\n" + text).strip() if current_accum else text
        accumulated_messages[phone] = new_accum
        last_message_time[phone] = time.time()

        # Re-programar procesamiento
        asyncio.create_task(process_with_debounce(phone, new_accum))

    return JSONResponse({"status": "queued"})


@app.get("/health")
async def health_check():
    return {"status": "healthy", "pending_tasks": len(pending_tasks)}


# ========================= RUN =========================
if __name__ == "__main__":
    logger.info("Iniciando Procasa WhatsApp Webhook en puerto 11422...")
    uvicorn.run(
        "webhook:app",
        host="0.0.0.0",
        port=11422,
        reload=False,        # En producción pon False
        workers=1,           # Un solo worker para estado global simple
        log_level="info"
    )