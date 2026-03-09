# --- START OF FILE webhook.py ---

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
import pytz # Importante para la hora local

# === NUEVAS IMPORTACIONES PARA GOOGLE ===
import httpx 
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, Cookie, Request, HTTPException, Depends, status, Form, Header, Query
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

# === TUS MÓDULOS PROPIOS ===
from campanas.handler import handle_campana_respuesta
from retiro.handler import handle_retiro_confirmacion, handle_solicitud_contacto
from api_leads_intelligence import get_leads_executive_report, get_specific_lead_chat
from api_crm import get_crm_leads_list, get_lead_detail_data, update_lead_crm_data, log_crm_event, manage_crm_notes, get_unique_executives, get_semantic_recommendations, log_recommendation_sent
from api_captacion import (
    get_captacion_list, get_captacion_detail, update_captacion_status, 
    distribute_sourced_leads, format_relative_time as format_captacion_time
)
from chatbot.manual_entry import create_manual_lead, check_lead_duplicate

# ========================= CONFIGURACIÓN =========================
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("procasa-full")

# CONFIGURACIÓN ZONA HORARIA CHILE
from chatbot.constants import CHILE_TZ

# --- CONFIGURACIÓN DE DIRECTORIOS ---
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

# Global state for background tasks monitoring
background_tasks_status = {
    "notifications_loop": {"status": "starting", "last_heartbeat": None},
    "sla_monitor": {"status": "starting", "last_heartbeat": None},
    "task_monitor": {"status": "starting", "last_heartbeat": None},
    "captacion_distributor": {"status": "starting", "last_heartbeat": None}
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    logger.info("Bot PRO Iniciando (Lifespan Startup)...")
    
    # Iniciar tareas de fondo
    n_task = asyncio.create_task(process_pending_leads_loop())
    s_task = asyncio.create_task(sla_monitor_loop())
    t_task = asyncio.create_task(check_scheduled_tasks_loop())
    c_task = asyncio.create_task(captacion_distribution_loop())
    
    # Crear admin y asegurar índices
    crear_admin_si_no_existe()
    asegurar_indices_db()
    
    # Pre-cargar modelo de embeddings (Evita hang en primer uso)
    try:
        from chatbot.semantic_engine import get_model
        logger.info("Pre-cargando modelo de embeddings en background...")
        get_model()
    except Exception as e:
        logger.error(f"Error pre-cargando modelo: {e}")
    
    yield
    
    # Shutdown logic
    logger.info("Bot PRO Apagando (Lifespan Shutdown)...")
    n_task.cancel()
    s_task.cancel()
    t_task.cancel()
    c_task.cancel()
    try:
        await asyncio.gather(n_task, s_task, t_task, c_task, return_exceptions=True)
    except Exception as e:
        logger.error(f"Error apagando tareas: {e}")

app = FastAPI(title="Procasa WhatsApp Bot - PRO PAGADO 2025", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

from chatbot.lead_router import should_send_now, format_whatsapp_template
from chatbot.storage import (
    get_pending_notifications, 
    mark_notification_sent, 
    save_pending_notification,
    get_user_by_phone
)
from chatbot.whatsapp_client import send_whatsapp_message

# Función auxiliar para imágenes (necesaria globalmente)
def get_images():
    prop_dir = STATIC_DIR / "propiedades"
    if not prop_dir.exists() or not prop_dir.is_dir():
        return ["propiedades/default.jpg"]
    images = [f"propiedades/{f.name}" for f in prop_dir.iterdir() if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}]
    return images or ["propiedades/default.jpg"]

# ========================= 2. SEGURIDAD, JWT Y MIDDLEWARE DE SESIÓN =========================
from jose import JWTError, jwt
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

if not hasattr(Config, "SECRET_KEY") or not Config.SECRET_KEY:
    # Si no hay clave en Config, usamos una por defecto PERO estable para evitar que cada worker tenga una distinta
    Config.SECRET_KEY = "procasa_secret_default_key_2025"
    logger.warning("ATENCIÓN: Usando SECRET_KEY por defecto. Se recomienda configurar una en variables de entorno para máxima seguridad.")
else:
    logger.info("SECRET_KEY cargada correctamente desde Config.")

def get_password_hash(password: str):
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str):
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict):
    to_encode = data.copy()
    # Expiración aumentada a 120 minutos (2 horas) según plan de estabilidad
    expire = datetime.now(pytz.utc) + timedelta(minutes=120)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, Config.SECRET_KEY, algorithm="HS256")

# --- MIDDLEWARE DE SESIÓN SLIDING (SOLUCIÓN TIMEOUT) ---
@app.middleware("http")
async def slide_session_middleware(request: Request, call_next):
    # 1. Ejecutar la petición primero
    response = await call_next(request)
    
    # 2. Rutas exentas
    if request.url.path.startswith("/static") or request.url.path in ["/logout", "/webhook", "/auth/google/callback"]:
        return response

    # 3. Lógica de Sliding Session (JWT)
    token = request.cookies.get("access_token")
    if token:
        try:
            payload = jwt.decode(token, Config.SECRET_KEY, algorithms=["HS256"])
            username = payload.get("sub")
            
            if username:
                # Renovación automática con cada interacción (Sliding Session)
                new_token = create_access_token({"sub": username})
                
                # Seteamos la nueva cookie con 2 HORAS (7200 segundos)
                response.set_cookie(
                    key="access_token",
                    value=new_token,
                    httponly=True,
                    secure=True,
                    samesite="lax",
                    max_age=7200,       # 120 minutos / 2 horas
                    path="/"
                )
                # Log ocasional para no saturar, pero útil para diagnóstico
                if time.time() % 30 < 5: # Log aproximadamente cada 30s de actividad
                    logger.info(f"[SESSION_RENEW] Sesión renovada para {username} (2h de margen)")
        except JWTError as e:
            # Solo logueamos si no es una simple expiración (para detectar ataques o desajustes de clave)
            if "expired" not in str(e).lower():
                logger.warning(f"[SESSION_ERROR] Error validando token: {e}")
            pass
            
        return response

    return response

async def get_current_user(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
    
    if not token:
        logger.warning("Intento de acceso sin token")
        raise HTTPException(status_code=401, detail="No autenticado")
        
    try:
        payload = jwt.decode(token, Config.SECRET_KEY, algorithms=["HS256"])
        username = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Token sin usuario")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")

# --- ENDPOINT ESPECIAL PARA RENOVACIÓN DE SESIÓN (HEARTBEAT) ---
@app.post("/api/session/renew")
async def renew_session(user_name: str = Depends(get_current_user)):
    """
    Endpoint ligero llamado por el frontend para mantener la sesión viva.
    El middleware 'slide_session_middleware' interceptará esta llamada
    y renovará la cookie automáticamente si el token es válido.
    """
    return {"status": "renewed", "user": user_name}
    

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
                "created_at": datetime.now(CHILE_TZ)
            })
            logger.info("Usuario 'admin' creado → contraseña: procasa2025")
        else:
            logger.info("Usuario 'admin' ya existe")
    except Exception as e:
        logger.error(f"Error creando admin: {e}")

crear_admin_si_no_existe()

# ========================= 3. LOGIN CON GOOGLE =========================

@app.get("/login/google")
async def login_google():
    """Inicia el flujo de OAuth2 con Google"""
    params = {
        "client_id": Config.GOOGLE_CLIENT_ID,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": Config.GOOGLE_REDIRECT_URI,
        "state": secrets.token_hex(16),
        "access_type": "offline",
        "prompt": "select_account"
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return RedirectResponse(url)

@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, code: str):
    """Recibe el código de Google y obtiene el token y datos del usuario"""
    try:
        # 1. Canjear código por token
        token_url = "https://oauth2.googleapis.com/token"
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(token_url, data={
                "client_id": Config.GOOGLE_CLIENT_ID,
                "client_secret": Config.GOOGLE_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": Config.GOOGLE_REDIRECT_URI,
            })
            token_data = token_resp.json()
        
        if "error" in token_data:
            logger.error(f"Error Token Google: {token_data}")
            return templates.TemplateResponse("login.html", {
                "request": request, "images": get_images(), 
                "error": "Error al conectar con Google (Token)"
            })

        access_token = token_data.get("access_token")

        # 2. Obtener datos del usuario
        user_info_url = "https://www.googleapis.com/oauth2/v1/userinfo"
        async with httpx.AsyncClient() as client:
            user_resp = await client.get(user_info_url, headers={
                "Authorization": f"Bearer {access_token}"
            })
            user_info = user_resp.json()

        email = user_info.get("email")
        
        # 3. Guardar o Buscar en MongoDB
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]
        usuarios = db["usuarios"]
        
        user = usuarios.find_one({
            "$or": [
                {"email": email}, 
                {"username": email}
            ]
        })

        if not user:
            logger.warning(f"Intento de acceso denegado: {email}")
            return templates.TemplateResponse("login.html", {
                "request": request, 
                "images": get_images(), 
                "error": f"Acceso Denegado: El correo {email} no tiene permisos."
            })
        
        user_sub = user["username"]
        user_rol = user.get("rol", "agente")
        target_url = "/leads-dashboard" if user_rol == "supervisor" else "/crm"

        SESSION_TIME = 1800 
        
        access_token_jwt = create_access_token({"sub": user_sub})
        
        response = RedirectResponse(target_url, status_code=303)
        
        response.set_cookie(
            key="access_token", 
            value=access_token_jwt,
            httponly=True, 
            secure=True,    # Cambiado a True para Render (HTTPS)
            samesite="lax", 
            max_age=SESSION_TIME 
        )

        logger.info("Conexión a MongoDB exitosa")
        logger.info(f"Sesión iniciada para {email} (Rol: {user_rol})")
        return response

    except Exception as e:
        logger.error(f"Error Google Auth Critical: {e}")
        return templates.TemplateResponse("login.html", {
            "request": request, "images": get_images(), 
            "error": f"Error interno: {str(e)}"
        })

# ========================= 4. RUTAS DE LOGIN TRADICIONAL =========================

@app.get("/")
async def login_get(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "images": get_images(),
            "error": None
        }
    )

@app.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]
        usuarios = db["usuarios"]
        user = usuarios.find_one({"username": username})
        
        if user and verify_password(password, user.get("hashed_password", "")):
            user_rol = user.get("rol", "agente")
            target_url = "/leads-dashboard" if user_rol == "supervisor" else "/crm"

            token = create_access_token({"sub": username})
            
            response = RedirectResponse(target_url, status_code=303) 
            response.set_cookie(
                "access_token", 
                token,
                httponly=True, 
                secure=True,   # Cambiado a True para Render (HTTPS)
                samesite="lax", 
                max_age=1800
            )
            return response
        
        return templates.TemplateResponse("login.html", {
            "request": request, "images": get_images(), "error": "Usuario o contraseña incorrectos"
        })
    except Exception as e:
        logger.error(f"Error en login tradicional: {e}")
        return templates.TemplateResponse("login.html", {
            "request": request, "images": get_images(), "error": "Error del servidor"
        })

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

# ========================= 5. DASHBOARD & REPORTES =========================

@app.get("/dashboard", response_class=HTMLResponse)
async def ver_campanas(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/api/leads_reporte")
async def api_leads_reporte(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="No autorizado")
    return get_leads_executive_report()

@app.get("/api/leads-intelligence")
async def leads_intelligence_endpoint():
    return get_leads_executive_report()

@app.get("/leads-dashboard", response_class=HTMLResponse)
async def ver_leads(request: Request):
    username = await get_current_user(request)
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    user = db["usuarios"].find_one({"username": username})
    
    if not user or user.get("rol") not in ["admin", "supervisor"]:
        return RedirectResponse(url="/crm?error=acceso_denegado")
    
    return templates.TemplateResponse("leads_dashboard.html", {
        "request": request,
        "user_role": user.get("rol", "agente"),
        "user_name": user.get("nombre", "")
    })

@app.get("/chat-detail/{phone}", response_class=HTMLResponse)
async def ver_detalle_chat(request: Request, phone: str):
    phone_clean = phone.replace(" ", "").replace("+", "")
    chat_data = get_specific_lead_chat(phone_clean)
    
    if not chat_data:
        chat_data = get_specific_lead_chat(phone)
        
    return templates.TemplateResponse("chat_detail.html", {
        "request": request, 
        "chat": chat_data,
        "phone": phone
    })

# --- RUTAS DE INGRESO MANUAL ---
@app.get("/manual-lead-entry", response_class=HTMLResponse)
async def view_manual_lead_entry(request: Request):
    username = await get_current_user(request)
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    user = db["usuarios"].find_one({"username": username})
    
    if not user or user.get("rol") not in ["admin", "supervisor"]:
        return RedirectResponse(url="/crm?error=acceso_denegado")
    
    # LÓGICA FINAL SIMPLE: Usar estrictamente el correo/usuario con el que se identificó.
    email = user.get("email") or user.get("username")
    if email: email = email.strip()

    return templates.TemplateResponse("manual_lead_entry.html", {
        "request": request,
        "user_email": email,
        "user_role": user.get("rol", "agente"),
        "user_name": user.get("nombre", "")
    })

@app.get("/api/leads/check-duplicate")
async def api_check_duplicate(request: Request, phone: str = Query(None), property_code: str = Query(...), email: str = Query(None)):
    # Seguridad básica
    await get_current_user(request)
    exists, executive = check_lead_duplicate(phone, property_code, email)
    return {"exists": exists, "assigned_to": executive}

@app.post("/api/leads/manual")
async def api_create_manual_lead(request: Request):
    username = await get_current_user(request)
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    user = db["usuarios"].find_one({"username": username})
    
    if not user or user.get("rol") not in ["admin", "supervisor"]:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    
    data = await request.json()
    result = create_manual_lead(data)
    
    if result.get("status") == "ok":
        return result
    else:
        raise HTTPException(status_code=400, detail=result.get("message"))


# ========================= 11. DETALLE Y GESTIÓN CRM =========================



@app.get("/crm/lead/{phone}", response_class=HTMLResponse)
async def view_crm_detail(request: Request, phone: str, codigo: str = Query(None)):
    username = await get_current_user(request)
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    user = db["usuarios"].find_one({"username": username})
    
    data = get_lead_detail_data(phone, property_code=codigo)
    if not data: 
        return HTMLResponse("Lead no encontrado")
    
    user_name = user.get("nombre", "")
    
    if user.get("rol") == "agente":
        # Comparamos verificando si el nombre de usuario está contenido en el nombre asignado (para manejar casos de 2 apellidos como Raquel Cheneaux Valz vs Raquel Cheneaux)
        ejecutivo_asignado = str(data.get("ejecutivo_asignado") or "").strip()
        if user_name.lower() not in ejecutivo_asignado.lower():
            return RedirectResponse(url="/crm?error=no_es_tu_lead")
    
    # LÓGICA FINAL SIMPLE (Solicitada por usuario): 
    # Usar estrictamente el correo/usuario con el que se identificó.
    email = user.get("email") or user.get("username")
    
    # Limpieza básica por si viene sucio
    if email: 
        email = email.strip()

    return templates.TemplateResponse("crm_lead_detail.html", {
        "request": request, 
        "lead": data,
        "user_email": email,
        "user_role": user.get("rol", "agente"),
        "user_name": user.get("nombre", "")
    })

@app.post("/api/crm/log_action")
async def api_crm_log_action(request: Request):
    try:
        data = await request.json()
        phone = data.get("phone")
        payload = data.get("data", {})
        
        # INYECTAR HORA DE CHILE EN EL METADATA
        now_cl = datetime.now(CHILE_TZ)
        if "meta" not in payload:
            payload["meta"] = {}
        payload["meta"]["server_time_cl"] = now_cl.strftime("%Y-%m-%d %H:%M:%S")

        log_crm_event(
            phone=phone, 
            event_type=payload.get("type"), 
            meta_data=payload.get("meta")
        )
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error logging CRM action: {e}")
        return {"status": "error"}

@app.post("/api/crm/update")
async def api_crm_update_lead(request: Request):
    try:
        data = await request.json()
        phone = data.get("phone")
        if not phone:
            raise HTTPException(status_code=400, detail="Falta teléfono")

        # Aseguramos que se guarde la hora de actualización en CL
        data["updated_at_cl"] = datetime.now(CHILE_TZ).isoformat()

        result = update_lead_crm_data(phone, data)
        if result and isinstance(result, dict) and result.get("status") == "ok":
            return result
        elif result is True: # Fallback just in case
            return {"status": "ok"}
        else:
            raise HTTPException(status_code=500, detail="No se pudo actualizar")
    except Exception as e:
        logger.error(f"CRM Update Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/crm/notes")
async def api_crm_notes(request: Request):
    try:
        data = await request.json()
        action = data.get("action", "add")
        phone = data.get("phone")
        note_data = data.get("note", {})

        # SOLUCIÓN HORA NOTAS: Forzar la hora de Chile en la creación
        if action == "add":
            now_cl = datetime.now(CHILE_TZ)
            # Sobreescribimos/Agregamos fecha formateada con HORA
            note_data["created_at_str"] = now_cl.strftime("%d/%m/%Y %H:%M")
            # Añadimos timestamp ISO para ordenamiento backend
            note_data["timestamp_iso"] = now_cl.isoformat()

        result = manage_crm_notes(phone, note_data, action)
        if result:
            return {"status": "ok", "note": result}
        return {"status": "error"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

# --- BÚSQUEDA SEMÁNTICA ---
@app.post("/api/crm/recommendations")
async def api_crm_recommendations(request: Request):
    try:
        data = await request.json()
        query = data.get("query", "")
        exclude = data.get("exclude", [])
        limit = data.get("limit", 3)
        scope = data.get("scope", "local")
        include_neighbors = data.get("include_neighbors", False)

        if not query or len(query.strip()) < 5:
            raise HTTPException(status_code=400, detail="Query muy corta")
        
        result = get_semantic_recommendations(query, exclude_codes=exclude, limit=limit, scope=scope, include_neighbors=include_neighbors)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SEMANTIC] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- ENVÍO DE RECOMENDACIÓN ---
@app.post("/api/crm/send_recommendation")
async def api_crm_send_recommendation(request: Request):
    try:
        data = await request.json()
        phone = data.get("phone", "")
        properties = data.get("properties", [])
        user_email = data.get("user_email", "")
        
        if not phone or not properties:
            raise HTTPException(status_code=400, detail="Faltan datos")
        
        result = log_recommendation_sent(phone, properties, user_email)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SEMANTIC] Error send_recommendation: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ========================= 7. WHATSAPP LOGIC (CORE) =========================
pending_tasks: Dict[str, Any] = {}
last_message_time: Dict[str, float] = {}
accumulated_messages: Dict[str, str] = {}
DEBOUNCE_SECONDS = 15.0

try:
    from chatbot import process_user_message
except ImportError:
    def process_user_message(phone, message):
        return f"Respuesta de prueba para {phone}: {message[:50]}..."

from chatbot.whatsapp_client import send_whatsapp_message
from chatbot.notification_service import NotificationService

async def process_with_debounce(phone: str, full_text: str, is_from_me: bool = False):
    if phone in pending_tasks and not pending_tasks[phone].done():
        pending_tasks[phone].cancel()
        logger.info(f"[DEBOUNCE] Tarea anterior cancelada para {phone}")
    
    current_text = accumulated_messages.get(phone, "")
    if current_text:
        accumulated_messages[phone] = current_text + " " + full_text.strip()
    else:
        accumulated_messages[phone] = full_text.strip()
        
    last_message_time[phone] = time.time()

    async def delayed_process(from_me: bool):
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            if time.time() - last_message_time.get(phone, 0) < DEBOUNCE_SECONDS - 0.1:
                return
            final_message = accumulated_messages.pop(phone, "").strip()
            if not final_message:
                return
            
            # Si es mensaje nuestro, lo registramos pero el bot no debe responder 
            # (Excepto si es el comando de toggle)
            logger.info(f"[PROCESS] Procesando mensaje {'HUMANO' if from_me else 'CLIENTE'} de {phone}")
            bot_response = await process_user_message(phone, final_message, is_from_me=from_me)
            
            if bot_response and bot_response.strip():
                await send_whatsapp_message(phone, bot_response)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error procesando {phone}: {e}", exc_info=True)
        finally:
            if phone in pending_tasks:
                pending_tasks.pop(phone, None)

    task = asyncio.create_task(delayed_process(is_from_me))
    pending_tasks[phone] = task

# ========================= 8. WEBHOOK & API ENDPOINTS =========================

@app.post("/webhook")
async def webhook(
    request: Request,
    x_webhook_signature: str = Header(None, alias="X-Webhook-Signature")
):
    raw_body = await request.body()
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

    if data.get("event") == "webhook.test":
        logger.info("TEST WEBHOOK EXITOSO")
        return JSONResponse({"ok": True}, status_code=200)

    messages_data = data.get("data", {}).get("messages", {}) or {}
    if not messages_data:
        return JSONResponse({"status": "no messages"}, status_code=200)

    msg_obj = messages_data if isinstance(messages_data, dict) else messages_data[0]
    key = msg_obj.get("key", {})
    from_me = key.get("fromMe", False)

    # --- DEBUG CRÍTICO: VER EL PAYLOAD COMPLETO ---
    logger.info(f"[DEBUG PAYLOAD] Key: {key}")
    logger.info(f"[DEBUG PAYLOAD] From: {msg_obj.get('from')} | SenderPn: {key.get('senderPn')} | Cleaned: {key.get('cleanedSenderPn')}")
    # ----------------------------------------------
    
    # --- EXTRACCIÓN ROBUSTA DE TELÉFONO ---
    # 1. Intentamos obtener el número limpio directamente del payload (lo más fiable)
    phone = key.get("cleanedSenderPn") or key.get("senderPn")
    
    # 2. Si no existe, usamos el 'remoteJid' pero SOLO si parece un número normal (evitamos LIDs)
    if not phone:
        remote_jid = key.get("remoteJid", "")
        if "@s.whatsapp.net" in remote_jid and not "@lid" in remote_jid:
            phone = remote_jid.split("@")[0]
            
    # 3. Fallback: usamos 'from' del mensaje
    if not phone:
        msg_from = msg_obj.get("from", "")
        if "@s.whatsapp.net" in msg_from and not "@lid" in msg_from:
            phone = msg_from.split("@")[0]

    # 4. Último recurso (puede ser peligroso si es LID, pero es mejor que nada)
    if not phone:
        phone = (key.get("remoteJid") or msg_obj.get("from") or "").split("@")[0]

    phone = str(phone).strip()

    # --- FILTRO DE EJECUTIVOS (Solicitado por usuario) ---
    # Si quien escribe es un ejecutivo (excepto Pablo Galleguillos), 
    # forzamos from_me=True para que el bot no responda.
    user_found = get_user_by_phone(phone)
    if user_found and user_found.get("rol") in ["agente", "supervisor"]:
        if user_found.get("nombre") != "Pablo Galleguillos":
            logger.info(f"[FILTER] Mensaje de EJECUTIVO ({user_found.get('nombre')}) detectado. Forzando modo manual.")
            from_me = True

    # Si detectamos que es un grupo (@g.us), lo ignoramos
    if "@g.us" in (key.get("remoteJid") or ""):
         logger.info(f"[WHATSAPP] Ignorando mensaje de grupo")
         return JSONResponse({"status": "group message ignored"}, status_code=200)

    # --- EXTRACCIÓN DEL TEXTO (RESTAURADA) ---
    text = (
        msg_obj.get("messageBody") or
        msg_obj.get("message", {}).get("conversation") or
        msg_obj.get("message", {}).get("extendedTextMessage", {}).get("text", "") or
        ""
    ).strip()
    # -----------------------------------------

    # Limpiamos el número: nos quedamos solo con dígitos
    phone_digits = "".join(filter(str.isdigit, phone))
    
    if not phone_digits:
        return JSONResponse({"status": "invalid phone"}, status_code=200)

    # Normalización para Chile (Casos comunes de entrada: 912345678, 56912345678, +56912345678)
    if phone_digits.startswith("56") and len(phone_digits) >= 11:
        phone = "+" + phone_digits
    elif len(phone_digits) == 9 and phone_digits.startswith("9"):
        phone = "+56" + phone_digits
    else:
        # Fallback genérico si no es Chile o ya tiene formato internacional
        phone = "+" + phone_digits

    logger.info(f"[WHATSAPP] {'[HUMANO]' if from_me else '[CLIENTE]'} Mensaje en {phone}: {text}")
    try:
        from chatbot.storage import log_event, EventType
        # Para el log de eventos, usamos el número limpio sin el '+'
        phone_log = phone.replace("+", "")
        log_event(phone_log, EventType.MSG_IN if not from_me else EventType.MSG_OUT, "user" if not from_me else "agent", {"text": text})
    except:
        pass
        
    await process_with_debounce(phone, text, is_from_me=from_me)
    return JSONResponse({"ok": True}, status_code=200)

@app.get("/health")
async def health_check():
    now = datetime.now(CHILE_TZ).isoformat()
    return {
        "status": "healthy", 
        "server_time": now,
        "active_conversations": len(pending_tasks),
        "background_tasks": background_tasks_status,
        "uptime_now": time.strftime("%Y-%m-%d %H:%M:%S")
    }

@app.post("/api/keep-alive")
async def api_keep_alive(request: Request):
    """Endpoint ligero para renovar la cookie de sesión sin recargar"""
    return {"status": "ok", "timestamp": time.time()}

@app.get("/campana/respuesta")
async def campana_respuesta(
    request: Request,
    email: str = Query(...),
    accion: str = Query(...),
    codigos: str = Query("N/A"),
    campana: str = Query(...)
):
    return await handle_campana_respuesta(request, email, accion, codigos, campana)

@app.get("/api/reporte_real")
async def api_reporte_real():
    from api_reporte_real import get_reporte_real
    data = get_reporte_real()
    return data
    

# ========================= 10. RUTAS CAPTACIÓN (NUEVO) =========================

@app.get("/captacion", response_class=HTMLResponse)
async def view_captaciones(
    request: Request,
    comuna: str = Query(None),
    estado: str = Query(None),
    ejecutivo: str = Query(None),
    page: int = Query(1, ge=1)
):
    username = await get_current_user(request)
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    user = db["usuarios"].find_one({"username": username})
    
    if not user:
        return RedirectResponse(url="/?error=sesion_invalida")

    user_role = user.get("rol", "agente")
    user_name = user.get("nombre", "")
    
    if user_role not in ["admin", "supervisor"]:
        return RedirectResponse(url="/crm?error=access_denied")
    
    # Lista de ejecutivos para el filtro (solo admin/supervisor)
    executives = []
    if user_role in ["admin", "supervisor"]:
        executives = get_unique_executives()
    
    limit = 10
    items, total_count = get_captacion_list(
        user_role=user_role,
        user_name=user_name,
        page=page,
        limit=limit,
        comuna_filter=comuna,
        status_filter=estado,
        executive_filter=ejecutivo
    )
    
    # KPIs adicionales para el resumen (basados en el ejecutivo/permisos, no en los filtros actuales de lista)
    base_query = {"details.es_propietario_directo": True}
    if user_role not in ["admin", "supervisor"]:
        base_query["gestion.ejecutivo_asignado"] = user_name
    elif ejecutivo and ejecutivo != "Todos":
        base_query["gestion.ejecutivo_asignado"] = ejecutivo

    in_gestion_count = db["yapo_propiedades"].count_documents({**base_query, "gestion.estado": "GESTION"})
    captados_count = db["yapo_propiedades"].count_documents({**base_query, "gestion.estado": "CAPTADO"})
    total_pages = (total_count + limit - 1) // limit

    return templates.TemplateResponse("captacion_list.html", {
        "request": request,
        "items": items,
        "total_count": total_count,
        "in_gestion_count": in_gestion_count,
        "captados_count": captados_count,
        "user_role": user_role,
        "user_name": user_name,
        "current_comuna": comuna,
        "current_estado": estado,
        "current_ejecutivo": ejecutivo,
        "executives": executives,
        "pagination": {
            "current_page": page,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1
        }
    })

@app.get("/captacion/{obj_id}", response_class=HTMLResponse)
async def view_captacion_detail_route(request: Request, obj_id: str):
    username = await get_current_user(request)
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    user = db["usuarios"].find_one({"username": username})
    
    if not user:
        return RedirectResponse(url="/?error=sesion_invalida")

    user_role = user.get("rol", "agente")
    user_name = user.get("nombre", "")

    if user_role not in ["admin", "supervisor"]:
        return RedirectResponse(url="/crm?error=access_denied")

    data = get_captacion_detail(obj_id)
    if not data:
        return HTMLResponse("Propiedad no encontrada")

    # RBAC Check
    user_name = user.get("nombre", "")
    if user.get("rol") == "agente":
        assigned = data.get("gestion", {}).get("ejecutivo_asignado")
        if assigned and user_name.lower() not in assigned.lower():
            return RedirectResponse(url="/captacion?error=no_asignada")

    return templates.TemplateResponse("captacion_detail.html", {
        "request": request,
        "prop": data,
        "user_name": user_name,
        "user_role": user.get("rol", "agente")
    })

@app.post("/api/captacion/update")
async def api_update_captacion(request: Request):
    await get_current_user(request)
    data = await request.json()
    obj_id = data.get("id")
    status = data.get("status")
    notes = data.get("notes")
    
    if not obj_id or not status:
        raise HTTPException(status_code=400, detail="Faltan datos")
        
    result = update_captacion_status(obj_id, status, notes)
    return {"status": "ok"} if result else {"status": "error"}

@app.post("/api/captacion/distribute")
async def api_distribute_captacion(request: Request):
    username = await get_current_user(request)
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    user = db["usuarios"].find_one({"username": username})
    
    if user.get("rol") not in ["admin", "supervisor"]:
        raise HTTPException(status_code=403, detail="No autorizado")
        
    count = distribute_sourced_leads()
    return {"status": "ok", "assigned": count}

async def captacion_distribution_loop():
    logger.info("[BACKGROUND] Iniciando loop de distribución de captaciones...")
    while True:
        try:
            background_tasks_status["captacion_distributor"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["captacion_distributor"]["status"] = "running"
            
            count = distribute_sourced_leads()
            if count > 0:
                logger.info(f"[BACKGROUND] Se asignaron {count} nuevas captaciones automáticamente.")
                
            # Ejecutar cada 1 hora
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            background_tasks_status["captacion_distributor"]["status"] = "error"
            logger.error(f"[BACKGROUND] Error en distribuidor de captaciones: {e}")
            await asyncio.sleep(60)

# ========================= 6. RUTAS CRM (MODIFICADAS PARA HORA LOCAL) =========================

@app.get("/crm", response_class=HTMLResponse)
async def view_crm_list(
    request: Request, 
    estado: str = None, 
    busqueda: str = None, 
    orden: str = "prioridad", 
    ejecutivo: str = None,
    page: int = Query(1, ge=1)
):
    username = await get_current_user(request)
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    user = db["usuarios"].find_one({"username": username})
    
    if not user:
        return RedirectResponse(url="/?error=sesion_invalida")

    user_role = user.get("rol", "agente")
    user_name = user.get("nombre", "")

    limit = 10
    leads, kpis, total_count = get_crm_leads_list(
        filtro_estado=estado, 
        busqueda=busqueda, 
        ordenar_por=orden,
        user_role=user_role,
        user_name=user_name,
        ejecutivo_filter=ejecutivo,
        page=page,
        limit=limit
    )
    
    total_pages = (total_count + limit - 1) // limit
    executives = get_unique_executives() if user_role in ["admin", "supervisor"] else []

    return templates.TemplateResponse("crm_leads_list.html", {
        "request": request, 
        "leads": leads, 
        "kpis": kpis,
        "user_role": user_role,
        "user_name": user_name,
        "executives": executives,
        "current_ejecutivo": ejecutivo or "Todos",
        "pagination": {
            "current_page": page,
            "total_pages": total_pages,
            "total_count": total_count,
            "has_next": page < total_pages,
            "has_prev": page > 1,
            "limit": limit
        }
    })

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
    result = col.update_one(
        {"email_propietario": email.lower()},
        {"$set": {"gestionado": gestionado}}
    )
    if result.matched_count == 0:
        col.update_one(
            {"email_propietario": {"$regex": f"^{re.escape(email.lower())}$", "$options": "i"}},
            {"$set": {"gestionado": gestionado}}
        )
    return {"status": "ok", "gestionado": gestionado}

@app.get("/retiro/confirmar")
async def retiro_confirmar(request: Request, email: str = Query(...), codigo: str = Query(...)):
    ip = request.client.host if request.client else "0.0.0.0"
    return await handle_retiro_confirmacion(email, codigo, ip)

@app.get("/retiro/contactar")
async def retiro_contactar(request: Request, email: str = Query(...), codigo: str = Query(...)):
    ip = request.client.host if request.client else "0.0.0.0"
    return await handle_solicitud_contacto(email, codigo, ip)

@app.exception_handler(401)
async def unauthorized_exception_handler(request: Request, exc: HTTPException):
    # Si el usuario intenta acceder a una ruta de la interfaz (HTML), lo mandamos al login
    logger.warning(f"Redirigiendo a login por sesión expirada en: {request.url.path}")
    return RedirectResponse(url="/?error=sesion_expirada")

# ========================= 9. BACKGROUND LOOPS (REFACTORED) =========================

async def process_pending_leads_loop():
    logger.info("[BACKGROUND] Iniciando loop de leads pendientes...")
    while True:
        try:
            background_tasks_status["notifications_loop"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["notifications_loop"]["status"] = "running"
            
            if should_send_now():
                pending = get_pending_notifications()
                if pending:
                    logger.info(f"[BACKGROUND] Analizando {len(pending)} envíos pendientes...")
                    
                    # 1. Agrupar por ejecutivo (target_phone)
                    by_executive = {}
                    for p in pending:
                        lead_data = p.get("lead_data", {})
                        target_phone = lead_data.get("target_phone") or p.get("target_phone")
                        
                        # Fix: Si no hay teléfono o es el dummy, intentamos re-enrutar antes de descartar
                        if not target_phone or target_phone == "+56900000000":
                            from chatbot.lead_router import find_responsible_executive
                            lead_phone = lead_data.get("phone")
                            p_code = lead_data.get("property_code")
                            if p_code:
                                logger.info(f"[BACKGROUND] Re-enrutando lead {lead_phone} por falta de destino válido...")
                                new_exec, new_phone = find_responsible_executive(p_code)
                                if new_phone and new_phone != "+56900000000":
                                    target_phone = new_phone
                                    data["name"] = new_exec
                                    # Actualizamos la data para que el mensaje se mande bien
                                    lead_data["target_phone"] = new_phone
                                    lead_data["target_name"] = new_exec
                                    logger.info(f"[BACKGROUND] Re-enrutado exitosamente a {new_exec} ({new_phone})")
                        
                        if not target_phone or target_phone == "+56900000000":
                            # Si después de re-enrutar sigue mal, lo marcamos para no ciclar eternamente
                            logger.warning(f"[BACKGROUND] Skipped: No se pudo encontrar destino válido para lead {lead_data.get('phone')}")
                            mark_notification_sent(p["_id"])
                            continue
                            
                        if target_phone not in by_executive:
                            by_executive[target_phone] = {"name": p.get("target_name") or lead_data.get("target_name"), "items": []}
                        
                        by_executive[target_phone]["items"].append(p)

                    # 2. Procesar cada ejecutivo
                    from chatbot.lead_router import format_whatsapp_template, format_summary_whatsapp_template
                    from chatbot.notification_service import NotificationService
                    from chatbot.crm_service import CrmService

                    for target_phone, data in by_executive.items():
                        items = data["items"]
                        target_name = data["name"]
                        
                        # Si tiene más de uno, enviamos resumen agrupado
                        if len(items) > 1:
                            logger.info(f"[BACKGROUND] Enviando resumen de {len(items)} leads a {target_name}")
                            msg = format_summary_whatsapp_template(items, target_name)
                            
                            # Marcamos todos como asignados en CRM (solo si no tienen ejecutivo aún)
                            for item in items:
                                lead_phone = item.get("lead_data", {}).get("phone")
                                if lead_phone:
                                    try:
                                        lead_db = CrmService._db()["leads"].find_one({"phone": lead_phone}) if hasattr(CrmService, '_db') else None
                                        existing_exec = (lead_db or {}).get("ejecutivo_asignado") if lead_db else None
                                        from chatbot.constants import UNASSIGNED_LABEL
                                        unassigned = [UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", None, ""]
                                        if not existing_exec or existing_exec in unassigned:
                                            CrmService.assign_executive(lead_phone, target_name, method="LeadRouter")
                                    except: pass
                            
                            success = await NotificationService.send_notification(
                                phone=target_phone,
                                message=msg,
                                alert_type="background_notification_group",
                                meta={"to": target_name, "count": len(items)},
                                dedup_window_minutes=5
                            )
                            
                            if success:
                                for item in items: mark_notification_sent(item["_id"])

                        # Si es solo uno, enviamos el template normal
                        else:
                            p = items[0]
                            lead_data = p.get("lead_data", {})
                            lead_phone = lead_data.get("phone")
                            prop_code = lead_data.get("property_code")
                            
                            logger.info(f"[BACKGROUND] Enviando lead individual {lead_phone} a {target_name}")
                            msg = format_whatsapp_template(lead_data, target_name, prop_code, is_new_assignment=True)
                            
                            if lead_phone:
                                try:
                                    from chatbot.storage import get_db as _get_db
                                    _lead_db = _get_db()["leads"].find_one({"phone": lead_phone})
                                    existing_exec = (_lead_db or {}).get("ejecutivo_asignado")
                                    from chatbot.constants import UNASSIGNED_LABEL
                                    unassigned = [UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", None, ""]
                                    if not existing_exec or existing_exec in unassigned:
                                        CrmService.assign_executive(lead_phone, target_name, method="LeadRouter")
                                except: pass
                                
                            success = await NotificationService.send_notification(
                                phone=target_phone,
                                message=msg,
                                alert_type="background_notification",
                                meta={"to": target_name},
                                dedup_window_minutes=5
                            )
                            if success: mark_notification_sent(p["_id"])

                        # Throttling Anti-Spam: 30 segundos entre ejecutivos (Aumentado por precaución de Meta)
                        logger.info(f"[BACKGROUND] Pausa anti-spam (30s) para siguiente destinatario...")
                        await asyncio.sleep(30)
                        
        except Exception as e:
            logger.error(f"[BACKGROUND] Error en loop de pendientes: {e}")
            background_tasks_status["notifications_loop"]["status"] = f"error: {str(e)}"
        
        await asyncio.sleep(60)

async def check_scheduled_tasks_loop():
    from chatbot.storage import get_db
    from chatbot.lead_router import get_executive_phone
    from chatbot.notification_service import NotificationService
    
    logger.info("[TASK_MONITOR] Iniciando monitor de tareas agendadas...")
    
    while True:
        try:
            background_tasks_status["task_monitor"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["task_monitor"]["status"] = "running"
            
            db = get_db()
            now = datetime.now(CHILE_TZ)
            
            tasks = list(db["crm_tasks"].find({
                "status": "pending",
                "execute_at": {"$lte": now}
            }))
            
            if tasks:
                logger.info(f"[TASK_MONITOR] Procesando {len(tasks)} tareas vencidas...")
                for task in tasks:
                    try:
                        phone = task.get("phone")
                        note = task.get("note", "Sin detalles")
                        lead = db["leads"].find_one({"phone": phone})
                        if not lead:
                            db["crm_tasks"].update_one({"_id": task["_id"]}, {"$set": {"status": "error", "error": "lead_not_found"}})
                            continue
                            
                        ejecutivo = lead.get("ejecutivo_asignado")
                        if not ejecutivo or ejecutivo in ["No asignado", "Sin Asignar"]:
                            continue
                            
                        exec_phone = get_executive_phone(ejecutivo)
                        if not exec_phone or exec_phone == "+56900000000":
                            continue
                            
                        crm_link = f"https://www.procasa.cl/crm/lead/{phone}"
                        msg_text = (
                            f"⏰ *Recordatorio CRM: {ejecutivo}*\n\n"
                            f"Tienes una acción programada para el lead *{lead.get('prospecto', {}).get('nombre', 'Cliente')}* ({phone}).\n\n"
                            f"📝 *Nota:* {note}\n"
                            f"🔗 Gestionar: {crm_link}"
                        )
                        
                        sent = await NotificationService.send_notification(
                            phone=exec_phone,
                            message=msg_text,
                            alert_type="TASK_REMINDER",
                            meta={"task_id": str(task["_id"]), "to": ejecutivo},
                            dedup_window_minutes=60 
                        )
                        
                        if sent:
                            db["crm_tasks"].update_one(
                                {"_id": task["_id"]}, 
                                {"$set": {"status": "notified", "notified_at": now.isoformat(), "notification_sent_to": ejecutivo}}
                            )
                            # Sleep breve para tareas
                            await asyncio.sleep(6)
                            
                    except Exception as e:
                        logger.error(f"[TASK_MONITOR] Error procesando tarea {task.get('_id')}: {e}")
            
        except Exception as e:
            logger.error(f"[TASK_MONITOR] Error en loop de tareas: {e}")
            background_tasks_status["task_monitor"]["status"] = f"error: {str(e)}"
            
        await asyncio.sleep(60)

async def sla_monitor_loop():
    logger.info("[SLA_MONITOR] Iniciando monitor de SLA...")
    while True:
        try:
            background_tasks_status["sla_monitor"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["sla_monitor"]["status"] = "running"
            
            from chatbot.sla_service import monitor_sla_thresholds
            await monitor_sla_thresholds()
        except Exception as e:
            logger.error(f"[BACKGROUND] Error en loop de SLA: {e}")
            background_tasks_status["sla_monitor"]["status"] = f"error: {str(e)}"
            
        await asyncio.sleep(60)

def asegurar_indices_db():
    try:
        db = MongoClient(Config.MONGO_URI)[Config.DB_NAME]
        db["crm_tasks"].create_index([("status", 1), ("execute_at", 1)])
        db["crm_events"].create_index([("phone", 1), ("type", 1), ("timestamp", -1)])
        logger.info("Índices de CRM asegurados.")
    except Exception as e:
        logger.warning(f"Error creando índices: {e}")

if __name__ == "__main__":
    import pathlib
    module_name = pathlib.Path(__file__).stem
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Bot PRO iniciado → http://localhost:{port}")
    uvicorn.run(f"{module_name}:app", host="0.0.0.0", port=port, reload=True, log_level="info")