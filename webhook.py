# webhook.py → BOT PRO 2025 CON LOGIN REAL + DASHBOARD + CAMPAÑAS 100% ORIGINALES
import asyncio
import logging
import time
import hmac
import hashlib
from typing import Dict, Any
import re
import os
import secrets
from pymongo import MongoClient
from datetime import datetime, timedelta
from pathlib import Path
import uvicorn
import json

import requests
from fastapi import FastAPI, Request, HTTPException, Header, Query, Form, Depends, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from campanas.handler import handle_campana_respuesta


# ========================= USAMOS TU config.py REAL =========================
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("procasa-full")

# ========================= JWT + AUTH (LOGIN REAL) =========================
from jose import JWTError, jwt
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

if not hasattr(Config, "SECRET_KEY") or not Config.SECRET_KEY:
    Config.SECRET_KEY = secrets.token_hex(32)
    logger.warning(f"SECRET_KEY generada automáticamente (guárdala en .env):")
    logger.warning(f"SECRET_KEY={Config.SECRET_KEY}")

def get_password_hash(password: str):
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str):
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(hours=8)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, Config.SECRET_KEY, algorithm="HS256")

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, Config.SECRET_KEY, algorithms=["HS256"])
        username: str = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Token inválido")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido")

def crear_admin_si_no_existe():
    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]
        usuarios = db["usuarios"]
        if usuarios.count_documents({"username": "admin"}) == 0:
            hashed = get_password_hash("procasa2025")
            usuarios.insert_one({
                "username": "admin",
                "hashed_password": hashed,
                "nombre": "Administrador",
                "is_active": True,
                "created_at": datetime.utcnow()
            })
            logger.info("Usuario 'admin' creado → contraseña: procasa2025")
        else:
            logger.info("Usuario 'admin' ya existe")
    except Exception as e:
        logger.error(f"Error creando admin: {e}")

crear_admin_si_no_existe()

# ========================= APP & RUTAS =========================
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(title="Procasa WhatsApp Bot - PRO PAGADO 2025")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

def get_images():
    prop_dir = STATIC_DIR / "propiedades"
    if not prop_dir.exists() or not prop_dir.is_dir():
        return ["propiedades/default.jpg"]
    images = [f"propiedades/{f.name}" for f in prop_dir.iterdir() if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}]
    logger.info(f"Imágenes cargadas ({len(images)}): {images}")
    return images or ["propiedades/default.jpg"]

# === LOGIN Y DASHBOARD ===
# === LOGIN Y DASHBOARD (100% FUNCIONAL EN FASTAPI) ===
@app.get("/")
async def login_get(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "images": get_images(),
            "error": None  # Puedes usar esto si quieres mostrar errores
        }
    )

@app.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]
        usuarios = db["usuarios"]
        user = usuarios.find_one({"username": username})
        
        if user and verify_password(password, user["hashed_password"]):
            token = create_access_token({"sub": username})
            response = RedirectResponse("/dashboard", status_code=303)
            response.set_cookie(
                "access_token", token,
                httponly=True, secure=True, samesite="lax", max_age=28800
            )
            return response
        
        # Si falla: vuelve al login con mensaje de error
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "images": get_images(),
                "error": "Usuario o contraseña incorrectos"
            }
        )
    except Exception as e:
        logger.error(f"Error en login: {e}")
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "images": get_images(),
                "error": "Error del servidor"
            }
        )

@app.get("/dashboard")
async def dashboard(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        return RedirectResponse("/", status_code=303)
    
    try:
        payload = jwt.decode(token, Config.SECRET_KEY, algorithms=["HS256"])
        username = payload.get("sub")
        if not username:
            return RedirectResponse("/", status_code=303)
        
        return templates.TemplateResponse(
            "dashboard.html", 
            {"request": request, "username": username}
        )
    except JWTError:
        return RedirectResponse("/", status_code=303)

@app.get("/logout")
async def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("access_token")
    return response

@app.get("/forgot-password")
async def forgot_password(request: Request):
    return templates.TemplateResponse("forgot_password.html", {"request": request})

@app.get("/reset-password/{token}")
async def reset_password(request: Request, token: str):
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token})

# ========================= WHATSAPP DEBOUNCE (100% ORIGINAL) =========================
pending_tasks: Dict[str, Any] = {}
last_message_time: Dict[str, float] = {}
accumulated_messages: Dict[str, str] = {}
DEBOUNCE_SECONDS = 5.0

try:
    from chatbot import process_user_message
except ImportError:
    def process_user_message(phone, message):
        return f"Respuesta de prueba para {phone}: {message[:50]}..."

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
        except Exception as e:
            logger.error(f"Error procesando {phone}: {e}", exc_info=True)
        finally:
            pending_tasks.pop(phone, None)

    task = asyncio.create_task(delayed_process())
    pending_tasks[phone] = task

import requests
import logging
from config import Config

logger = logging.getLogger(__name__)

async def send_whatsapp_message(number: str, text: str) -> bool:
    # Normaliza número (igual que siempre)
    clean = "".join(filter(str.isdigit, number))
    if len(clean) == 9 and clean.startswith("9"):
        clean = "569" + clean
    elif len(clean) == 11 and clean.startswith("56"):
        clean = clean
    elif len(clean) == 12 and clean.startswith("569"):
        clean = clean[1:]
    elif len(clean) == 11 and clean.startswith("569"):
        clean = clean

    # USA TU CONFIG.EXACTAMENTE COMO LO TENÍAS
    url = f"{Config.WASENDER_BASE_URL}/send-message"   # ← ESTO ES LO QUE FUNCIONA

    payload = {"to": clean, "text": text}
    headers = {
        "Authorization": f"Bearer {Config.WASENDER_TOKEN}",
        "Content-Type": "application/json"
    }

    # 1er intento
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200 and resp.json().get("success"):
            logger.info(f"Enviado a {clean}")
            return True
    except Exception as e:
        logger.error(f"Error envío: {e}")

    await asyncio.sleep(2)
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            logger.info(f"Enviado en reintento a {clean}")
            return True
    except Exception as e:
        logger.error(f"Reintento falló: {e}")

    return False

@app.post("/webhook")
async def webhook(
    request: Request,
    x_webhook_signature: str = Header(None, alias="X-Webhook-Signature")
):
    raw_body = await request.body()

    # === 1. Verificación de firma (WASenderAPI) ===
    if Config.WASENDER_WEBHOOK_SECRET:
        expected = hmac.new(
            Config.WASENDER_WEBHOOK_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, x_webhook_signature or ""):
            logger.warning("Firma inválida en webhook")
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        logger.error(f"JSON inválido: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info(f"[WEBHOOK] Payload completo: {data}")

    # === 2. Test del webhook (WASenderAPI lo manda al activar) ===
    if data.get("event") == "webhook.test":
        logger.info("TEST WEBHOOK EXITOSO")
        return JSONResponse({"ok": True}, status_code=200)

    # === 3. Extraer teléfono y mensaje (compatible con WASenderAPI) ===
    messages_data = data.get("data", {}).get("messages", {}) or {}
    if not messages_data:
        return JSONResponse({"status": "no messages"}, status_code=200)

    # WASenderAPI manda el mensaje como objeto o como string
    msg_obj = messages_data if isinstance(messages_data, dict) else messages_data[0]

    phone = (
        msg_obj.get("key", {}).get("cleanedSenderPn") or
        msg_obj.get("key", {}).get("senderPn", "").split("@")[0] or
        msg_obj.get("from", "").split("@")[0] or
        ""
    ).strip()

    text = (
        msg_obj.get("messageBody") or
        msg_obj.get("message", {}).get("conversation") or
        msg_obj.get("message", {}).get("extendedTextMessage", {}).get("text", "") or
        ""
    ).strip()

    if not phone or not text:
        logger.info("Mensaje vacío o sin teléfono → ignorado")
        return JSONResponse({"status": "ignored"}, status_code=200)

    # === 4. Normalizar teléfono (formato +569XXXXXXXX) ===
    phone = phone.replace("@c.us", "").replace("@s.whatsapp.net", "")
    if phone.startswith("56") and len(phone) == 11:
        phone = "+56" + phone[2:]
    elif phone.startswith("56"):
        phone = "+" + phone
    elif not phone.startswith("+"):
        phone = "+56" + phone.lstrip("0")

    logger.info(f"[WHATSAPP] Mensaje de {phone}: {text}")

    # === 5. LA CLAVE: LLAMAR AL CHATBOT REAL (igual que test_consola.py) ===
    from chatbot import process_user_message
    respuesta = process_user_message(phone, text)

    logger.info(f"[WHATSAPP] Respuesta generada: {respuesta}")

    # === 6. Enviar respuesta por WASenderAPI ===
    try:
        send_url = f"{Config.WASENDER_BASE_URL}/send"
        payload = {
            "token": Config.WASENDER_TOKEN,
            "to": phone,           # número con +
            "message": respuesta
        }
        response = requests.post(send_url, json=payload, timeout=15)
        if response.status_code != 200:
            logger.error(f"Error enviando mensaje: {response.text}")
    except Exception as e:
        logger.error(f"Error al enviar respuesta por WASenderAPI: {e}")

    return JSONResponse({"ok": True}, status_code=200)

@app.get("/health")
async def health_check():
    return {"status": "healthy", "active_conversations": len(pending_tasks), "uptime": time.strftime("%Y-%m-%d %H:%M:%S")}

@app.get("/campana/respuesta")
async def campana_respuesta(
    email: str = Query(...),
    accion: str = Query(...),
    codigos: str = Query("N/A"),
    campana: str = Query(...)
):
    return await handle_campana_respuesta(email, accion, codigos, campana)

@app.get("/api/reporte_real")
async def api_reporte_real():
    from api_reporte_real import get_reporte_real
    data = get_reporte_real()
    return data

@app.post("/api/marcar_gestionado")
async def marcar_gestionado(request: Request):
    data = await request.json()
    email = data.get("email")
    gestionado = data.get("gestionado", False)

    if not email:
        return {"error": "Falta email"}

    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    col = db[Config.COLLECTION_CONTACTOS]

    # Actualiza por email (exacto o case-insensitive)
    result = col.update_one(
        {"email_propietario": email.lower()},
        {"$set": {"gestionado": gestionado}}
    )

    if result.matched_count == 0:
        # Intento case-insensitive
        col.update_one(
            {"email_propietario": {"$regex": f"^{re.escape(email.lower())}$", "$options": "i"}},
            {"$set": {"gestionado": gestionado}}
        )

    return {"status": "ok", "gestionado": gestionado}

# ====================== ARRANQUE CORRECTO ======================
if __name__ == "__main__":
    import pathlib
    module_name = pathlib.Path(__file__).stem
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Bot PRO iniciado → http://localhost:{port}")
    logger.info("Usuario: admin | Contraseña: procasa2025")
    uvicorn.run(f"{module_name}:app", host="0.0.0.0", port=port, reload=True, log_level="info")