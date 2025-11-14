#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# https://carroll-connectional-noella.ngrok-free.dev/webhook

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn
import requests
import json
from datetime import datetime, timezone
from config import Config
from db import load_chat_history, save_chat_history, CHATS_COLLECTION
from chatbot import process_message  # Importa process_message unificado
from ai_utils import clean_for_json  # Para logging y serialización

import logging
import asyncio  # Para locks y timers
from typing import Dict  # Para dict de locks/timers
from collections import deque  # Para buffer simple (FIFO) - NUEVO
from contextlib import asynccontextmanager  # ← FIX: Para lifespan (anti-deprecation)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
config = Config()

# Template para inicializar contexto de feedback (outreach post-interés/visita)
MENSAJE_TEMPLATE = """
Hola, soy asistente inmobiliaria de PROCASA Jorge Pablo Caro Propiedades. 😊 

Recordamos que hace poco mostraste interés en una de nuestras propiedades y contactaste a uno de nuestros ejecutivos. ¿Pudiste coordinar y visitar alguna opción que te gustara? ¿Qué te pareció la experiencia?

Si sigues en la búsqueda de tu hogar ideal, me encantaría saber qué estás priorizando ahora: ¿dormitorios, comuna, presupuesto? ¡Estoy aquí para mostrarte opciones que se ajusten perfecto a lo que buscas!

Cuéntame un poco más para reconectarte con lo mejor de nuestra cartera.
"""

def initialize_feedback_history(phone: str) -> list:
    """
    Inicializa historial para leads de feedback post-outreach.
    Agrega el template como mensaje inicial de assistant.
    """
    template_msg = {"role": "assistant", "content": MENSAJE_TEMPLATE.strip(), "timestamp": datetime.now(timezone.utc)}
    initial_history = [template_msg]
    print(f"[LOG] Historial inicializado con feedback template para {phone}.")
    return initial_history

# Dict de locks por phone (limpia viejos cada 10min para memoria)
phone_locks: Dict[str, asyncio.Lock] = {}
CLEANUP_INTERVAL = 600  # 10min

async def get_phone_lock(phone: str) -> asyncio.Lock:
    """Obtiene/crea lock por phone. Limpia locks inactivos >1h."""
    if phone not in phone_locks:
        lock = asyncio.Lock()
        lock._acquired_time = datetime.now(timezone.utc)  # Para cleanup
        phone_locks[phone] = lock
    return phone_locks[phone]

async def cleanup_old_locks():
    """Limpia locks viejos (corre en background)."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        now = datetime.now(timezone.utc)
        to_del = []
        for p, lock in list(phone_locks.items()):
            if hasattr(lock, '_last_used') and (now - lock._last_used).total_seconds() > 3600:
                to_del.append(p)
        for p in to_del:
            del phone_locks[p]
        logger.info(f"[LOCK] Limpieza: {len(to_del)} locks viejos eliminados.")

# NUEVO: Queues por phone
message_queues: Dict[str, asyncio.Queue] = {}
BUFFER_SIZE = 3  # Máx mensajes a concatenar/buffer
CLEANUP_QUEUE_INTERVAL = 300  # Limpia queues inactivas cada 5min

async def get_message_queue(phone: str) -> asyncio.Queue:
    """Obtiene/crea queue por phone."""
    if phone not in message_queues:
        q = asyncio.Queue()
        message_queues[phone] = q
    return message_queues[phone]

async def cleanup_old_queues():
    """Limpia queues inactivas >10min."""
    while True:
        await asyncio.sleep(CLEANUP_QUEUE_INTERVAL)
        now = datetime.now(timezone.utc)
        to_del = []
        for p, q in list(message_queues.items()):
            if q.empty() and hasattr(q, '_last_used') and (now - q._last_used).total_seconds() > 600:
                to_del.append(p)
        for p in to_del:
            del message_queues[p]
        logger.info(f"[QUEUE] Limpieza: {len(to_del)} queues viejos eliminados.")

# NUEVO: Timers per-phone (background tasks para buffering temporal)
phone_timers: Dict[str, asyncio.Task] = {}
PROCESS_DELAY = 8  # Segundos a esperar después del último mensaje antes de procesar (ampliado a s para menos frustración)

async def schedule_phone_processor(phone: str, q: asyncio.Queue):
    """Background task: Espera PROCESS_DELAY después del último put, luego drena/procesa."""
    last_activity = datetime.now(timezone.utc)
    while True:
        await asyncio.sleep(PROCESS_DELAY)
        now = datetime.now(timezone.utc)
        if (now - last_activity).total_seconds() >= PROCESS_DELAY and not q.empty():
            # Drena y procesa
            lock = await get_phone_lock(phone)
            async with lock:
                lock._last_used = now
                logger.info(f"[TIMER] Procesando queue para {phone} después de {PROCESS_DELAY}s inactividad")
                
                pending_messages = []
                while not q.empty():
                    pending = await q.get()
                    msg_content = pending["message"].strip().lower()
                    # NUEVO: Filtra ruido (cortos/fillers comunes en WhatsApp)
                    if len(msg_content) > 1 and msg_content not in ['.', '..', ' ', 'ok', 'sip', 'si', 'no', 'ja']:
                        pending_messages.append(pending)
                
                if not pending_messages:
                    continue  # Ignora queues solo con ruido
                
                # Carga history
                history = load_chat_history(phone)
                if len(history) == 0:
                    history = initialize_feedback_history(phone)
                
                # NUEVO: Maneja merge inteligente
                if len(pending_messages) > 1:
                    # Extrae contenidos válidos
                    valid_contents = [pm["message"] for pm in pending_messages]
                    # Chequea si último es "refuerzo" (e.g., "por favor" → no merge, solo boost urgency después)
                    last_msg = pending_messages[-1]["message"].strip().lower()
                    if last_msg in ['por favor', 'plis', 'gracias', 'ok?']:
                        merged_content = valid_contents[-2] if len(valid_contents) > 1 else valid_contents[0]  # Usa penúltimo como main
                        extras = {"urgency_boost": True, **pending_messages[-1]["extras"]}  # Flag para urgency="alta" en process_message
                        print(f"[QUEUE] Refuerzo detectado ('{last_msg}'), usando main: '{merged_content[:50]}...' ({len(pending_messages)} totales)")
                    else:
                        # Merge solo últimos 2 para chains cortas
                        merged_content = ' '.join(valid_contents[-2:])
                        extras = pending_messages[-1]["extras"]
                        print(f"[QUEUE] Procesando merged (filtrado): '{merged_content[:50]}...' ({len(pending_messages)} válidos)")
                    is_active = process_message(phone, merged_content, history=history, extras=extras)
                else:
                    pm = pending_messages[0]
                    is_active = process_message(phone, pm["message"], history=history, extras=pm["extras"])
                
                # Response
                response = ""
                if history and history[-1]["role"] == "assistant":
                    response = history[-1]["content"]
                else:
                    response = "¿En qué puedo ayudarte con propiedades?"

                # NUEVO: Guard final contra None/empty antes de enviar
                if not response or response.strip() == "" or response == "None":
                    response = "Estoy aquí para ayudarte con propiedades en Procasa. ¿Qué buscas hoy? 😊"
                    logger.warning(f"[WEBHOOK] Response era inválida para {phone}; usando default.")

                save_chat_history(phone, history)
                logger.info(f"[WEBHOOK] Historial guardado para {phone} (timer liberado).")

                # NUEVO: Validación antes de enviar - Chequea si llegó algo nuevo en la cola
                # Loop rápido (1s x 3 = 3s max) para no bloquear, pero detectar arrivals recientes
                abort_send = False
                for _ in range(3):  # Chequea 3 veces con 1s delay
                    if not q.empty():
                        logger.info(f"[VALIDATION] Mensaje nuevo detectado en cola para {phone}; abortando envío para merge.")
                        abort_send = True
                        # Reinicia timer inmediatamente
                        await get_or_start_timer(phone, q)
                        break
                    await asyncio.sleep(1)  # Espera 1s antes del próximo chequeo
                
                if abort_send:
                    continue  # Salta el envío y deja que el timer maneje el nuevo batch
                
                # Envío (solo si no abortó)
                number = phone[1:] if phone.startswith('+') else phone
                send_url = f"{config.APICHAT_BASE_URL}/sendText"
                send_data = {'number': number, 'text': response}
                headers = {
                    'client-id': str(config.APICHAT_CLIENT_ID),
                    'token': config.APICHAT_TOKEN,
                    'accept': 'application/json',
                    'Content-Type': 'application/json'
                }
                send_resp = requests.post(send_url, json=send_data, headers=headers, timeout=config.APICHAT_TIMEOUT)
                
                if send_resp.status_code != 200:
                    error_text = send_resp.text if send_resp.text else "Empty response body"
                    logger.error(f"[WEBHOOK] Error enviando response: {send_resp.status_code} - {error_text}")
                    raise HTTPException(status_code=500, detail="Error sending response")
                
                logger.info(f"[WEBHOOK] Response enviada a {phone}: {response[:100]}... (msg_id: {send_resp.json().get('id', 'N/A')})")
        
        # Actualiza last_activity si hay actividad
        if not q.empty():
            last_activity = datetime.now(timezone.utc)
            await asyncio.sleep(0.5)  # Polling ligero

async def get_or_start_timer(phone: str, q: asyncio.Queue):
    """Inicia/reinicia timer per-phone si no existe."""
    if phone not in phone_timers:
        task = asyncio.create_task(schedule_phone_processor(phone, q))
        phone_timers[phone] = task
        logger.info(f"[TIMER] Iniciado processor para {phone}")
    # Reinicia: Cancela y recrea para resetear 'last_activity' implícito
    else:
        phone_timers[phone].cancel()
        new_task = asyncio.create_task(schedule_phone_processor(phone, q))
        phone_timers[phone] = new_task
        logger.info(f"[TIMER] Reiniciado processor para {phone}")

# FIX: Versión robusta de normalize_timestamp (maneja str, datetime, unix)
def normalize_timestamp(ts):
    if ts is None:
        return None
    
    if isinstance(ts, str):
        try:
            if ts.endswith('Z'):
                ts = ts.replace('Z', '+00:00')
            ts = datetime.fromisoformat(ts)
        except ValueError:
            try:
                ts = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            except (ValueError, TypeError):
                ts = datetime.now(timezone.utc)
    
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    
    return None

@asynccontextmanager  # ← FIX: Lifespan anti-deprecation
async def lifespan(app: FastAPI):
    # Startup
    asyncio.create_task(cleanup_old_locks())
    asyncio.create_task(cleanup_old_queues())
    yield
    # Shutdown: Limpia timers
    for task in phone_timers.values():
        task.cancel()
    phone_timers.clear()

app.router.lifespan_context = lifespan  # Asigna al router

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        logger.info(f"[WEBHOOK] Raw payload recibido: {json.dumps(clean_for_json(data), indent=2)[:500]}...")
        
        if 'events' in data and data['events']:
            event = data['events'][0]
            event_type = event.get('type', 'unknown')
            msg_id = event.get('id', 'N/A')
            number = event.get('number', 'N/A')
            logger.info(f"[WEBHOOK] Evento de status: {event_type} para msg_id {msg_id} en {number}")
            return JSONResponse(status_code=200, content={"status": "OK", "type": "event_ack"})
        
        phone = ""
        message = ""
        msg_id = None
        msg_timestamp = None
        
        if 'messages' in data and data['messages']:
            msg = data['messages'][0]
            phone = msg.get('from') or msg.get('number', '') or msg.get('author', '')
            
            msg_id = msg.get('id')
            msg_timestamp_str = msg.get('timestamp')
            if msg_timestamp_str:
                try:
                    msg_timestamp = datetime.fromtimestamp(float(msg_timestamp_str), tz=timezone.utc)
                except ValueError:
                    msg_timestamp = datetime.now(timezone.utc)
            else:
                msg_timestamp = datetime.now(timezone.utc)
            
            text_data = msg.get('text', {})
            if isinstance(text_data, str):
                message = text_data
            elif isinstance(text_data, dict) and 'body' in text_data:
                message = text_data['body']
            else:
                if msg.get('type') != 'text':
                    message = f"[MEDIA] Recibí un medio ({msg.get('type', 'desconocido')}). ¿En qué te ayudo?"
                else:
                    message = ""
        
        # Normalización de phone
        if phone and len(phone) == 11 and phone.startswith('56') and phone[2] == '9':
            phone = f"+{phone}"
        elif phone and len(phone) == 9 and phone.startswith('9'):
            phone = f"+56{phone}"
        
        if not phone or not message.strip():  # ← FIX: Ignora vacíos
            logger.warning(f"[WEBHOOK] Payload incompleto/vacío: phone='{phone}', message='{message}'.")
            return JSONResponse(status_code=200, content={"status": "OK", "ignored": "empty"})
        
        logger.info(f"[WEBHOOK] Mensaje procesado de {phone}: {message} (ID: {msg_id}, TS: {msg_timestamp})")
        
        # Chequeo duplicado ANTES de queue (umbral 10s + lowercase)
        history = load_chat_history(phone)
        is_duplicate = False
        m_ts = normalize_timestamp(msg_timestamp)
        for hmsg in history:
            h_ts = normalize_timestamp(hmsg.get("timestamp"))
            if hmsg.get("role") == "user" and (
                (msg_id and hmsg.get("msg_id") == msg_id) or
                (hmsg.get("content").strip().lower() == message.strip().lower() and h_ts and m_ts and abs((h_ts - m_ts).total_seconds()) < 10)  # ← FIX: <10s + lowercase
            ):
                is_duplicate = True
                logger.info(f"[WEBHOOK] Mensaje duplicado ignorado para {phone}: ID {msg_id} (diff: {abs((h_ts - m_ts).total_seconds()):.1f}s)")
                break

        if is_duplicate:
            return JSONResponse(status_code=200, content={"status": "OK", "duplicate": True})

        # Encola y resetea timer
        q = await get_message_queue(phone)
        q._last_used = datetime.now(timezone.utc)
        await q.put({
            "message": message,
            "msg_id": msg_id,
            "timestamp": msg_timestamp,
            "extras": {"msg_id": msg_id, "timestamp": msg_timestamp}
        })
        print(f"[QUEUE] Mensaje encolado: '{message}' para {phone} (reseteando timer)")
        
        await get_or_start_timer(phone, q)  # ← NUEVO: Inicia/reinicia processor
        
        return JSONResponse(status_code=200, content={"status": "OK", "queued": True, "timer_reset": True})
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[WEBHOOK] Error general: {e} - Original data: {json.dumps(clean_for_json(data), indent=2) if 'data' in locals() else 'N/A'}")
        return JSONResponse(status_code=500, content={"error": str(e)})

if __name__ == "__main__":
    uvicorn.run("webhook:app", host="0.0.0.0", port=11422, reload=True)