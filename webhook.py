# --- START OF FILE webhook.py ---

# webhook.py → BOT PRO 2025 CON LOGIN REAL + DASHBOARD + CAMPAÑAS 100% ORIGINALES
import asyncio
import logging
import os
import time
import hmac
import hashlib
from typing import Dict, Any
import re
import secrets
import traceback
import threading
import subprocess
import concurrent.futures
import inspect
from concurrent.futures import ThreadPoolExecutor
from pymongo import MongoClient
from pymongo import ReturnDocument
from datetime import datetime, timedelta
from pathlib import Path
import uvicorn
import json
import pytz # Importante para la hora local

# ========================= THREAD POOL CONTROLADO =========================
# Pool separado para request web (evita que tareas batch bloqueen respuestas HTTP).
_WEB_THREAD_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="procasa_web")
# Pool separado para workers de procesamiento de leads.
_WORKER_THREAD_POOL = ThreadPoolExecutor(max_workers=5, thread_name_prefix="procasa_worker")
# Pool dedicado para tareas periódicas (cache warmer) para evitar competir con workers.
_WARMER_THREAD_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="procasa_warmer")

# === NUEVAS IMPORTACIONES PARA GOOGLE ===
import httpx 
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, Cookie, Request, HTTPException, Depends, status, Form, Header, Query, BackgroundTasks
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
    get_captacion_list, get_captacion_detail, update_captacion_status, update_contact_info,
    distribute_sourced_leads, format_relative_time as format_captacion_time,
    get_personal_templates, save_personal_template, delete_personal_template
)
from chatbot.manual_entry import create_manual_lead, check_lead_duplicate
from chatbot.processing_service import LeadProcessingService

# ========================= CONFIGURACIÓN =========================
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("procasa-full")

# --- BLOCKING DETECTOR (temporal forensics) ---
_ORIG_TIME_SLEEP = time.sleep
_ORIG_SUBPROCESS_RUN = subprocess.run
_ORIG_FUTURE_RESULT = concurrent.futures.Future.result

def _in_event_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False

def _forensic_sleep(seconds):
    if _in_event_loop_thread():
        logger.warning(f"[BLOCKING_DETECTOR] time.sleep({seconds}) llamado dentro de async/event loop")
    return _ORIG_TIME_SLEEP(seconds)

def _forensic_subprocess_run(*args, **kwargs):
    if _in_event_loop_thread():
        try:
            stack = inspect.stack()
            project_frames = [
                fr for fr in stack
                if fr.filename and ("\\ChatBot_v4_Grok\\" in fr.filename or "/ChatBot_v4_Grok/" in fr.filename)
            ]
            short = " > ".join(
                f"{os.path.basename(fr.filename)}:{fr.function}:{fr.lineno}" for fr in project_frames[:5]
            ) if project_frames else "stack_no_disponible"
            logger.warning(f"[BLOCKING_DETECTOR] subprocess.run llamado dentro de async/event loop stack={short}")
        except Exception:
            logger.warning("[BLOCKING_DETECTOR] subprocess.run llamado dentro de async/event loop")
    return _ORIG_SUBPROCESS_RUN(*args, **kwargs)

def _forensic_future_result(self, *args, **kwargs):
    if _in_event_loop_thread():
        try:
            stack = inspect.stack()
            project_frames = [
                fr for fr in stack
                if fr.filename and ("\\ChatBot_v4_Grok\\" in fr.filename or "/ChatBot_v4_Grok/" in fr.filename)
            ]
            if project_frames:
                short = " > ".join(
                    f"{os.path.basename(fr.filename)}:{fr.function}:{fr.lineno}" for fr in project_frames[:4]
                )
                logger.warning(f"[BLOCKING_DETECTOR] Future.result() llamado dentro de async/event loop stack={short}")
        except Exception:
            logger.warning("[BLOCKING_DETECTOR] Future.result() llamado dentro de async/event loop")
    return _ORIG_FUTURE_RESULT(self, *args, **kwargs)

time.sleep = _forensic_sleep
subprocess.run = _forensic_subprocess_run
concurrent.futures.Future.result = _forensic_future_result

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
    "captacion_distributor": {"status": "starting", "last_heartbeat": None},
    "lead_processing": {"status": "starting", "last_heartbeat": None}
}
_OAUTH_HTTP_CLIENT = None

# --- NUEVA ARQUITECTURA DE COLA (PRODUCER/CONSUMER) ---
lead_processing_queue = None  # Se inicializará en lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    logger.info("Bot PRO Iniciando (Lifespan Startup)...")

    logger.info("ThreadPoolExecutor configurado: web=8, worker=5, warmer=1")
    
    global lead_processing_queue, _OAUTH_HTTP_CLIENT
    lead_processing_queue = asyncio.Queue()

    # Preconectar DB para reducir latencia del primer login/request.
    try:
        from chatbot.storage import get_db, get_async_db
        get_db().command("ping")
        await get_async_db().command("ping")
        logger.info("MongoDB preconnect: OK")
    except Exception as e:
        logger.warning(f"MongoDB preconnect warning: {e}")

    # Cliente HTTP compartido para OAuth (evita crear conexión por callback).
    _OAUTH_HTTP_CLIENT = httpx.AsyncClient(timeout=10.0)

    # Iniciar tareas de fondo

    # Iniciar tareas de fondo
    n_task = asyncio.create_task(process_pending_leads_loop())
    s_task = asyncio.create_task(sla_monitor_loop())
    t_task = asyncio.create_task(check_scheduled_tasks_loop())
    c_task = asyncio.create_task(captacion_distribution_loop())
    r_task = asyncio.create_task(reassign_unassigned_leads_loop()) # Ahora es Productor
    d_task = asyncio.create_task(daily_report_loop())
    nudge_task = asyncio.create_task(inactive_lead_nudge_loop())
    w_task = asyncio.create_task(cache_prewarmer_loop())  # PRE-WARMING de cache
    el_task = asyncio.create_task(event_loop_monitor_loop()) # MONITOR EVENT LOOP
    tp_task = asyncio.create_task(threadpool_forensics_loop()) # MONITOR THREAD POOLS
    
    # Iniciar Consumers
    c1_task = asyncio.create_task(lead_consumer_worker(1))
    c2_task = asyncio.create_task(lead_consumer_worker(2))
    
    # Crear admin y asegurar índices
    crear_admin_si_no_existe()
    asegurar_indices_db()
    
    # El modelo de embeddings se cargará bajo demanda para ahorrar RAM en el arranque
    logger.info("Startup completo. Modelo de embeddings se cargará en el primer uso.")
    
    yield
    
    # Shutdown logic
    logger.info("Bot PRO Apagando (Lifespan Shutdown)...")
    n_task.cancel()
    s_task.cancel()
    t_task.cancel()
    c_task.cancel()
    r_task.cancel()
    d_task.cancel()
    nudge_task.cancel()
    w_task.cancel()
    el_task.cancel()
    tp_task.cancel()
    c1_task.cancel()
    c2_task.cancel()
    try:
        await asyncio.gather(
            n_task, s_task, t_task, c_task, r_task, d_task, nudge_task, w_task, el_task, tp_task, c1_task, c2_task,
            return_exceptions=True
        )
    except Exception as e:
        logger.error(f"Error apagando tareas: {e}")
    finally:
        if _OAUTH_HTTP_CLIENT is not None:
            try:
                await _OAUTH_HTTP_CLIENT.aclose()
            except Exception:
                pass
            _OAUTH_HTTP_CLIENT = None
        _WEB_THREAD_POOL.shutdown(wait=False)
        _WORKER_THREAD_POOL.shutdown(wait=False)
        _WARMER_THREAD_POOL.shutdown(wait=False)
        logger.info("ThreadPoolExecutors cerrados.")

app = FastAPI(title="Procasa WhatsApp Bot - PRO PAGADO 2025", lifespan=lifespan)

# ========================= MIDDLEWARE DE OBSERVABILIDAD =========================
import time
import uuid

@app.middleware("http")
async def advanced_perf_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    start_time = time.time()
    
    # Extract user if available (lightweight estimation)
    user = "anon"
    token = request.cookies.get("procasa_token")
    if token:
        try:
            from jose import jwt
            # Only decode without verifying signature to save CPU in middleware, just for logging
            payload = jwt.decode(token, options={"verify_signature": False})
            user = payload.get("sub", "anon").split("@")[0] # keep it short
        except:
            pass

    try:
        response = await call_next(request)
        duration_ms = (time.time() - start_time) * 1000
        
        if not request.url.path.startswith("/static/") and not request.url.path.startswith("/contracts_pdf/"):
            content_length = response.headers.get("content-length", "unknown")
            log_str = f"[HTTP_PERF] request_id={request_id} user={user} method={request.method} path={request.url.path} total={duration_ms:.0f}ms status={response.status_code} size={content_length}"
            logger.info(log_str)
            
            if duration_ms > 3000:
                logger.error(f"[SLOW_REQUEST] ERROR: request_id={request_id} path={request.url.path} duration={duration_ms:.0f}ms")
            elif duration_ms > 1000:
                logger.warning(f"[SLOW_REQUEST] WARNING: request_id={request_id} path={request.url.path} duration={duration_ms:.0f}ms")
                
        response.headers["X-Process-Time"] = str(duration_ms)
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        logger.error(f"[HTTP_PERF] request_id={request_id} user={user} method={request.method} path={request.url.path} ERROR={str(e)} total={duration_ms:.0f}ms")
        raise e

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Mount contracts_pdf to serve PDFs statically and fast
CONTRACTS_PDF_DIR = BASE_DIR / "contracts_pdf"
CONTRACTS_PDF_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/contracts_pdf", StaticFiles(directory=CONTRACTS_PDF_DIR), name="contracts_pdf")

# Mount visitas_pdf
VISITAS_PDF_DIR = BASE_DIR / "visitas_pdf"
VISITAS_PDF_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/visitas_pdf", StaticFiles(directory=VISITAS_PDF_DIR), name="visitas_pdf")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

from api_contracts import router as contracts_router
app.include_router(contracts_router)

from api_visitas import router as visitas_router
app.include_router(visitas_router)

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

def crear_admin_si_no_existe():
    """Asegura usuario admin de emergencia para acceso operativo."""
    try:
        from chatbot.storage import get_db
        db = get_db()
        usuarios = db["usuarios"]

        admin_user = os.getenv("ADMIN_USERNAME", "admin")
        admin_pass = os.getenv("ADMIN_PASSWORD", "admin12345")
        admin_email = os.getenv("ADMIN_EMAIL", "admin@procasa.cl")
        admin_nombre = os.getenv("ADMIN_NOMBRE", "Administrador")

        exists = usuarios.find_one({"username": admin_user}, {"_id": 1})
        if exists:
            logger.info("Usuario 'admin' ya existe")
            return

        usuarios.insert_one({
            "username": admin_user,
            "email": admin_email,
            "nombre": admin_nombre,
            "rol": "admin",
            "hashed_password": get_password_hash(admin_pass),
            "activo": True,
            "created_at": datetime.now(CHILE_TZ).isoformat()
        })
        logger.info("Usuario 'admin' creado correctamente")
    except Exception as e:
        logger.error(f"Error creando admin: {e}")

# --- MIDDLEWARE DE SESION SLIDING (SOLUCION TIMEOUT) ---
@app.middleware("http")
async def slide_session_middleware(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)

    process_time = time.time() - start_time
    if process_time > 1.0:
        logger.warning(f"[LATENCY_ALERT] {request.method} {request.url.path} tardo {process_time:.3f}s")

    if request.url.path.startswith("/static") or request.url.path in ["/logout", "/webhook", "/auth/google/callback"]:
        return response

    token = request.cookies.get("access_token")
    if token:
        try:
            payload = jwt.decode(token, Config.SECRET_KEY, algorithms=["HS256"])
            username = payload.get("sub")
            if username:
                exp_ts = payload.get("exp")
                should_renew = True
                if exp_ts:
                    try:
                        now_ts = datetime.now(pytz.utc).timestamp()
                        should_renew = (float(exp_ts) - now_ts) <= 5400
                    except Exception:
                        should_renew = True
                if should_renew:
                    new_token = create_access_token({"sub": username})
                    response.set_cookie(
                        key="access_token",
                        value=new_token,
                        httponly=True,
                        secure=True,
                        samesite="lax",
                        max_age=7200,
                        path="/"
                    )
        except JWTError:
            pass
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
        raise HTTPException(status_code=401, detail="Token invalido o expirado")

async def get_current_user_doc(request: Request):
    cached = getattr(request.state, "current_user_doc", None)
    if cached is not None:
        return cached
    username = await get_current_user(request)
    from chatbot.storage import get_async_db
    adb = get_async_db()
    user = await adb["usuarios"].find_one(
        {"username": username},
        {"username": 1, "nombre": 1, "rol": 1, "email": 1}
    )
    request.state.current_user_doc = user
    return user

@app.post("/api/session/renew")
async def renew_session(user_name: str = Depends(get_current_user)):
    return {"status": "ok", "user": user_name}

# ========================= 3. LOGIN CON GOOGLE =========================

@app.get("/login/google")
async def login_google():
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
    try:
        token_url = "https://oauth2.googleapis.com/token"
        client = _OAUTH_HTTP_CLIENT
        owns_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=10.0)
            owns_client = True
        try:
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
                return templates.TemplateResponse("login.html", {"request": request, "images": get_images(), "error": "Error al conectar con Google (Token)"})

            access_token = token_data.get("access_token")
            user_info_url = "https://www.googleapis.com/oauth2/v1/userinfo"
            user_resp = await client.get(user_info_url, headers={"Authorization": f"Bearer {access_token}"})
            user_info = user_resp.json()
        finally:
            if owns_client:
                await client.aclose()

        email = user_info.get("email")
        from chatbot.storage import get_async_db as _gadb
        _adb = _gadb()
        usuarios = _adb["usuarios"]
        user = await usuarios.find_one({"$or": [{"email": email}, {"username": email}]}, {"username": 1, "rol": 1, "email": 1})
        if not user:
            logger.warning(f"Intento de acceso denegado: {email}")
            return templates.TemplateResponse("login.html", {"request": request, "images": get_images(), "error": f"Acceso Denegado: El correo {email} no tiene permisos."})

        user_sub = user["username"]
        user_rol = user.get("rol", "agente")
        target_url = "/leads-dashboard" if user_rol == "supervisor" else "/crm"
        access_token_jwt = create_access_token({"sub": user_sub})
        response = RedirectResponse(target_url, status_code=303)
        response.set_cookie(key="access_token", value=access_token_jwt, httponly=True, secure=True, samesite="lax", max_age=7200)
        logger.info("Conexion a MongoDB exitosa")
        logger.info(f"Sesion iniciada para {email} (Rol: {user_rol})")
        return response
    except Exception as e:
        logger.error(f"Error Google Auth Critical: {e}")
        return templates.TemplateResponse("login.html", {"request": request, "images": get_images(), "error": f"Error interno: {str(e)}"})

# ========================= 4. RUTAS DE LOGIN TRADICIONAL =========================

@app.head("/")
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
        from chatbot.storage import get_async_db
        db = get_async_db()
        usuarios = db["usuarios"]
        user = await usuarios.find_one({"username": username})
        
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
                max_age=7200
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
    # FIX: run_in_executor evita bloquear el event loop durante cache miss
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_WEB_THREAD_POOL, get_leads_executive_report)

@app.get("/api/leads-intelligence")
async def leads_intelligence_endpoint():
    # FIX: get_leads_executive_report() es síncrona (pymongo). Sin executor bloqueaba
    # el event loop completo durante ~1.6s en cada cache miss (cada 5 min).
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_WEB_THREAD_POOL, get_leads_executive_report)

@app.get("/leads-dashboard", response_class=HTMLResponse)
async def ver_leads(request: Request):
    user = await get_current_user_doc(request)
    
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
    loop = asyncio.get_running_loop()
    chat_data = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_specific_lead_chat(phone_clean))
    
    if not chat_data:
        chat_data = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_specific_lead_chat(phone))
        
    return templates.TemplateResponse("chat_detail.html", {
        "request": request, 
        "chat": chat_data,
        "phone": phone
    })

# --- RUTAS DE INGRESO MANUAL ---
@app.get("/manual-lead-entry", response_class=HTMLResponse)
async def view_manual_lead_entry(request: Request):
    user = await get_current_user_doc(request)
    
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
    loop = asyncio.get_running_loop()
    status, executive = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: check_lead_duplicate(phone, property_code, email)
    )
    return {"status": status, "exists": status != "not_found", "assigned_to": executive}

@app.post("/api/leads/manual")
async def api_create_manual_lead(request: Request):
    import time as _time
    _t0 = _time.perf_counter()
    # [PERF] user lookup
    _tu = _time.perf_counter()
    user = await get_current_user_doc(request)
    logger.info(f"[PERF] /api/leads/manual user_lookup: {(_time.perf_counter()-_tu)*1000:.1f}ms")

    if not user or user.get("rol") not in ["admin", "supervisor"]:
        raise HTTPException(status_code=403, detail="Acceso denegado")

    # [PERF] json parse
    _tj = _time.perf_counter()
    data = await request.json()
    logger.info(f"[PERF] /api/leads/manual json_parse: {(_time.perf_counter()-_tj)*1000:.1f}ms")

    # [PERF] create_manual_lead (sync: duplicate check + DB insert + executive lookup)
    _tc = _time.perf_counter()
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: create_manual_lead(data))
    logger.info(f"[PERF] /api/leads/manual create_manual_lead: {(_time.perf_counter()-_tc)*1000:.1f}ms")

    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result.get("message"))

    # Encolar para procesamiento en background (evita saturar anyio y el default thread pool)
    lead_id_obj = result.get("lead_id")
    if lead_id_obj:
        await lead_processing_queue.put(lead_id_obj)

    logger.info(
        f"[PERF] /api/leads/manual TOTAL_BEFORE_RESPONSE: {(_time.perf_counter()-_t0)*1000:.1f}ms "
        f"lead_id={result.get('lead_id')} assigned_to={result.get('assigned_to')}"
    )
    return result


# ========================= 11. DETALLE Y GESTIÓN CRM =========================



@app.get("/crm/lead/{phone}", response_class=HTMLResponse)
async def view_crm_detail(request: Request, phone: str, codigo: str = Query(None)):
    user = await get_current_user_doc(request)
    
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_lead_detail_data(phone, property_code=codigo))
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
        event_type = payload.get("type")

        now_cl = datetime.now(CHILE_TZ)
        if "meta" not in payload:
            payload["meta"] = {}
        payload["meta"]["server_time_cl"] = now_cl.strftime("%Y-%m-%d %H:%M:%S")

        def _sync_log_action():
            from chatbot.storage import get_db
            db = get_db()
            phone_clean = str(phone).replace("+", "").strip()
            lead = db["leads"].find_one({"phone": {"$regex": f"^{phone_clean}"}})

            if lead:
                current_stage = lead.get("stage") or lead.get("pipeline_stage")
                from chatbot.constants import PipelineStage
                if str(current_stage).lower() in ["nuevo", "new"] or current_stage == PipelineStage.NEW:
                    management_events = [
                        "SEND_WA_OWNER", "CLICK_WHATSAPP_OWNER", "SEND_EMAIL_OWNER", "CLICK_PHONE_OWNER",
                        "CLICK_WHATSAPP_LEAD", "CLICK_PHONE_LEAD", "SEND_WA_LEAD", "SEND_EMAIL_LEAD"
                    ]
                    if event_type in management_events:
                        from chatbot.crm_service import CrmService
                        try:
                            CrmService.update_stage(
                                phone_clean,
                                PipelineStage.CONTACTED,
                                actor="agent",
                                notes=f"Auto-promocion por accion rapida: {event_type}"
                            )
                        except Exception as prom_err:
                            logger.error(f"Error auto-promoviendo lead tras gestion: {prom_err}")

            log_crm_event(phone=phone, event_type=event_type, meta_data=payload.get("meta"))

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_WEB_THREAD_POOL, _sync_log_action)
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

        # CRITICO: update_lead_crm_data usa PyMongo sync + log_event/update_metrics sync.
        # Debe ejecutarse fuera del event loop para evitar bloqueos y MONGO_SYNC_ON_EVENT_LOOP.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: update_lead_crm_data(phone, data)
        )
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

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: manage_crm_notes(phone, note_data, action))
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
        
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: get_semantic_recommendations(query, exclude_codes=exclude, limit=limit, scope=scope, include_neighbors=include_neighbors)
        )
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
        
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: log_recommendation_sent(phone, properties, user_email))
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
        # ANTI-DUPLICADO: Capturamos referencia a la tarea ACTUAL para compararla luego
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            # Verificación 1: el usuario no envió otro mensaje durante el sleep
            if time.time() - last_message_time.get(phone, 0) < DEBOUNCE_SECONDS - 0.1:
                return
            final_message = accumulated_messages.pop(phone, "").strip()
            if not final_message:
                return
            
            logger.info(f"[PROCESS] Procesando mensaje {'HUMANO' if from_me else 'CLIENTE'} de {phone}")
            
            capture_time = last_message_time.get(phone, 0)
            bot_response = await process_user_message(phone, final_message, is_from_me=from_me)
            
            # Verificación 2: ¿llegó un mensaje nuevo MIENTRAS el LLM procesaba?
            if last_message_time.get(phone, 0) > capture_time:
                logger.warning(f"⚫ [ANTI-DUP-1] {phone}: nuevo mensaje llegó durante LLM. Descartando respuesta vieja.")
                return

            # Verificación 3: ¿ya existe una tarea más reciente en pending_tasks para este phone?
            # Esto cubre el caso donde el cliente envió mensaje justo cuando el LLM terminó.
            registered_task = pending_tasks.get(phone)
            if registered_task is not None and registered_task is not current_task and not registered_task.done():
                logger.warning(f"⚫ [ANTI-DUP-2] {phone}: hay tarea más nueva pendiente. Abortando envío de respuesta actual.")
                return

            if bot_response and bot_response.strip():
                await send_whatsapp_message(phone, bot_response)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error procesando {phone}: {e}", exc_info=True)
        finally:
            # Solo limpiar si somos la tarea registrada actualmente
            if pending_tasks.get(phone) is asyncio.current_task():
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

    # --- LOG AGRESIVO PARA DEBUG ---
    logger.info(f"Incoming Webhook Event: {data.get('event')} | Payload size: {len(raw_body)}")
    if "@g.us" in str(data):
        logger.info(f"🎯 Grupo detectado en el payload! Raw: {json.dumps(data)[:500]}")


    messages_data = data.get("data", {}).get("messages", {}) or {}
    if not messages_data:
        return JSONResponse({"status": "no messages"}, status_code=200)

    msg_obj = messages_data if isinstance(messages_data, dict) else messages_data[0]
    key = msg_obj.get("key", {})
    from_me = key.get("fromMe", False)

    # --- EXTRACCCIÓN ROBUSTA DE TELÉFONO (Movido arriba para evitar NameError) ---
    phone = key.get("cleanedSenderPn") or key.get("senderPn")
    if not phone:
        remote_jid = key.get("remoteJid", "")
        if "@s.whatsapp.net" in remote_jid and not "@lid" in remote_jid:
            phone = remote_jid.split("@")[0]
    if not phone:
        msg_from = msg_obj.get("from", "")
        if "@s.whatsapp.net" in msg_from and not "@lid" in msg_from:
            phone = msg_from.split("@")[0]
    if not phone:
        phone = (key.get("remoteJid") or msg_obj.get("from") or "").split("@")[0]

    # --- SEGURIDAD: FILTRO DE RECENCIA (ANTI-BURST) ---
    msg_ts = msg_obj.get("messageTimestamp")
    if msg_ts:
        try:
            # Robust conversion for cases where Baileys/WASender sends Int64 as a dict {low, high}
            if isinstance(msg_ts, dict):
                ts_int = int(msg_ts.get("low", msg_ts.get("seconds", 0)))
            else:
                ts_int = int(msg_ts)
                
            now_ts = int(time.time())
            diff = now_ts - ts_int
            
            # Logger de diagnóstico
            logger.info(f"[DEBUG TIMESTAMP] Msg TS: {ts_int} | Now: {now_ts} | Diff: {diff}s")

            # Aumentado a 5 días (432000s) para permitir procesar mensajes acumulados del fin de semana
            if diff > 432000: 
                logger.warning(f"[SAFETY] Ignorando mensaje MUY antiguo de {diff}s (Remitente: {phone}).")
                return JSONResponse({"status": "very old message ignored", "diff": diff}, status_code=200)
            
            if diff > 60:
                logger.info(f"[SAFETY] Procesando mensaje con retraso detectado de {diff}s...")
        except Exception as te:
            logger.error(f"Error parseando timestamp ({type(msg_ts)}): {te}")

    # --- DEBUG CRÍTICO: VER EL PAYLOAD COMPLETO ---
    logger.info(f"[DEBUG PAYLOAD] Key: {key}")
    logger.info(f"[DEBUG PAYLOAD] From: {msg_obj.get('from')} | SenderPn: {key.get('senderPn')} | Cleaned: {key.get('cleanedSenderPn')}")
    
    # --- DISCOVERY: CAPTURAR ID DE GRUPO ---
    remote_jid = key.get("remoteJid", "")
    if "@g.us" in remote_jid:
        group_name = msg_obj.get("pushName") or "Grupo Desconocido"
        logger.info(f"🔍 [GROUP_DISCOVERY] ID: {remote_jid} | Name: {group_name}")
    
    phone = str(phone).strip()

    # --- FILTRO DE EJECUTIVOS (Solicitado por usuario) ---
    # Si quien escribe es un ejecutivo (excepto Pablo Galleguillos), 
    # forzamos from_me=True para que el bot no responda.
    loop = asyncio.get_running_loop()
    user_found = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_user_by_phone(phone))
    if user_found and user_found.get("rol") in ["agente", "supervisor"]:
        if user_found.get("nombre") != "Pablo Galleguillos":
            logger.info(f"[FILTER] Mensaje de EJECUTIVO ({user_found.get('nombre')}) detectado. Forzando modo manual.")
            from_me = True

    # --- FILTRO PROPIETARIOS CON CONTRATO ---
    from chatbot.storage import get_db
    _db = await loop.run_in_executor(_WEB_THREAD_POOL, get_db)
    phone_digits_check = "".join(filter(str.isdigit, phone))
    if phone_digits_check and len(phone_digits_check) >= 8:
        contract_active = await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: _db.contracts.find_one({
                "phone": {"$regex": phone_digits_check[-8:] + "$"},
                "status": {"$in": ["created", "viewed", "accepted"]}
            })
        )
        if contract_active:
            logger.info(f"[WHATSAPP] Ignorando mensaje de {phone} porque tiene un contrato en proceso.")
            return JSONResponse({"status": "contract owner ignored"}, status_code=200)

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
        await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: log_event(
                phone_log,
                EventType.MSG_IN if not from_me else EventType.MSG_OUT,
                "user" if not from_me else "agent",
                {"text": text},
            ),
        )
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
    campana: str = Query(...),
    token: str = Query(""),
    mode: str = Query("live")
):
    return await handle_campana_respuesta(request, email, accion, codigos, campana, mode, token)

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
    from chatbot.storage import get_async_db
    adb = get_async_db()
    user = await get_current_user_doc(request)
    
    if not user:
        return RedirectResponse(url="/?error=sesion_invalida")

    user_role = user.get("rol", "agente")
    user_name = user.get("nombre", "")
    
    limit = 10
    loop = asyncio.get_running_loop()
    list_task = loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_captacion_list(
            user_role=user_role,
            user_name=user_name,
            page=page,
            limit=limit,
            comuna_filter=comuna,
            status_filter=estado,
            executive_filter=ejecutivo
        )
    )
    exec_task = get_unique_executives() if user_role in ["admin", "supervisor"] else asyncio.sleep(0, result=[])
    items_total, executives = await asyncio.gather(list_task, exec_task)
    items, total_count = items_total
    
    # KPIs adicionales para el resumen (basados en el ejecutivo/permisos, no en los filtros actuales de lista)
    base_query = {"details.es_propietario_directo": True}
    if user_role not in ["admin", "supervisor"]:
        base_query["gestion.ejecutivo_asignado"] = user_name
    elif ejecutivo and ejecutivo != "Todos":
        base_query["gestion.ejecutivo_asignado"] = ejecutivo

    in_gestion_count, captados_count = await asyncio.gather(
        adb["yapo_propiedades"].count_documents({**base_query, "gestion.estado": "GESTION"}),
        adb["yapo_propiedades"].count_documents({**base_query, "gestion.estado": "CAPTADO"})
    )
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
    user = await get_current_user_doc(request)
    
    if not user:
        return RedirectResponse(url="/?error=sesion_invalida")

    user_role = user.get("rol", "agente")
    user_name = user.get("nombre", "")



    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_captacion_detail(obj_id))
    if not data:
        return HTMLResponse("Propiedad no encontrada")

    # RBAC Check
    user_name = user.get("nombre", "")
    if user.get("rol") == "agente":
        assigned = data.get("gestion", {}).get("ejecutivo_asignado")
        if assigned and user_name.lower() not in assigned.lower():
            return RedirectResponse(url="/captacion?error=no_asignada")

    # Ya no calculamos el matching aquí (se hace vía AJAX)
    
    return templates.TemplateResponse("captacion_detail.html", {
        "request": request,
        "prop": data,
        "user_name": user_name,
        "user_role": user.get("rol", "agente")
    })

# --- PROTECCIÓN ANTI-SPAM PARA MATCHING ---
PENDING_MATCHING_REQUESTS = {} # obj_id -> timestamp

@app.get("/api/captacion/{obj_id}/matching")
async def api_get_matching_leads(request: Request, obj_id: str):
    await get_current_user(request)

    from api_captacion import get_captacion_detail, get_matching_leads_analysis, get_cached_value, set_cached_value

    loop = asyncio.get_running_loop()

    cache_key = f"matching_{obj_id}"
    cached_data = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_cached_value(cache_key))
    if cached_data:
        return cached_data

    now = time.time()
    if obj_id in PENDING_MATCHING_REQUESTS:
        last_req = PENDING_MATCHING_REQUESTS[obj_id]
        if now - last_req < 5:
            return {"status": "processing", "message": "Ya se esta calculando el matching. Por favor espere."}

    PENDING_MATCHING_REQUESTS[obj_id] = now

    try:
        data = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_captacion_detail(obj_id))
        if not data:
            raise HTTPException(status_code=404, detail="Propiedad no encontrada")

        ma = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_matching_leads_analysis(data))

        response_data = {
            "exact": ma.get("exact", 0),
            "zone": ma.get("zone", 0),
            "broad": ma.get("broad", 0),
            "matching_analysis": ma,
            "ma": ma,
            "zone_name": ma.get("zone_name", "Sin zona"),
            "pitch_text": ma.get("pitch_text", "")
        }

        await loop.run_in_executor(_WEB_THREAD_POOL, lambda: set_cached_value(cache_key, response_data, expire_seconds=300))

        return response_data
    finally:
        if obj_id in PENDING_MATCHING_REQUESTS:
            del PENDING_MATCHING_REQUESTS[obj_id]
@app.post("/api/captacion/update")
async def api_update_captacion(request: Request):
    try:
        await get_current_user(request)
        data = await request.json()
        obj_id = data.get("id")
        status = data.get("status")
        notes = data.get("notes")
        next_followup = data.get("next_followup")
        channel = data.get("channel")
        outcome = data.get("outcome")
        user_name = data.get("user_name", "Sistema")
        
        if not obj_id or not status:
            raise HTTPException(status_code=400, detail="Faltan datos")
            
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: update_captacion_status(
                obj_id,
                status,
                notes,
                channel=channel,
                outcome=outcome,
                user_name=user_name,
                next_followup=next_followup
            )
        )
        return {"status": "ok"} if result else {"status": "error", "message": "Operación retornó falso"}
    except HTTPException:
        # Re-lanzar 401/403/400 para que el cliente y el handler global los manejen correctamente
        raise
    except Exception as e:
        logger.error(f"Error updating captacion: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/captacion/contact")
async def api_update_captacion_contact(request: Request):
    try:
        await get_current_user(request)
        data = await request.json()
        obj_id = data.get("id")
        if not obj_id:
            raise HTTPException(status_code=400, detail="Falta ID")
        
        # Check user name in session or payload
        user_name = data.get("user_name")
        if not user_name:
            user_doc = await get_current_user_doc(request)
            user_name = user_doc.get("nombre", user_doc.get("username", "Sistema")) if user_doc else "Sistema"
        
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: update_contact_info(
                obj_id,
                nombre=data.get("nombre"),
                telefono=data.get("telefono"),
                email=data.get("email"),
                notas=data.get("notas"),
                user_name=user_name
            )
        )
        return {"status": "ok"} if result else {"status": "error", "message": "Operación retornó falso"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating captacion contact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/captacion/log_action")
async def api_captacion_log_action(request: Request):
    username_str = await get_current_user(request)
    user_doc = await get_current_user_doc(request)
    actual_name = user_doc.get("nombre", username_str) if user_doc else "Sistema"
    
    data = await request.json()
    obj_id = data.get("id")
    action = data.get("action")
    channel = data.get("channel")
    message = data.get("message")
    phone = data.get("phone")
    result = data.get("result")
    template_used = data.get("template_used")
    
    if not obj_id or not action:
        raise HTTPException(status_code=400, detail="Faltan datos")
        
    try:
        from api_captacion import log_captacion_activity
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: log_captacion_activity(obj_id, actual_name, action, channel, message, phone, result, template_used)
        )
        return {"status": "ok"} if success else {"status": "error"}
    except Exception as e:
        logger.error(f"Error logging captacion action: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/captacion/templates/personal")
async def api_get_personal_templates(request: Request):
    username_str = await get_current_user(request)
    user_doc = await get_current_user_doc(request)
    actual_name = user_doc.get("nombre", username_str) if user_doc else username_str
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_personal_templates(actual_name))

@app.post("/api/captacion/templates/personal")
async def api_save_personal_template(request: Request):
    username_str = await get_current_user(request)
    user_doc = await get_current_user_doc(request)
    actual_name = user_doc.get("nombre", username_str) if user_doc else username_str
    
    data = await request.json()
    loop = asyncio.get_running_loop()
    tid = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: save_personal_template(actual_name, data))
    return {"status": "ok", "id": tid}

@app.delete("/api/captacion/templates/personal")
async def api_delete_personal_template(request: Request):
    username_str = await get_current_user(request)
    user_doc = await get_current_user_doc(request)
    actual_name = user_doc.get("nombre", username_str) if user_doc else username_str
    
    data = await request.json()
    tid = data.get("id")
    if not tid:
        raise HTTPException(status_code=400, detail="Falta ID")
        
    loop = asyncio.get_running_loop()
    success = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: delete_personal_template(tid, actual_name))
    return {"status": "ok"} if success else {"status": "error"}

@app.post("/api/captacion/distribute")
async def api_distribute_captacion(request: Request):
    user = await get_current_user_doc(request)
    
    if user.get("rol") not in ["admin", "supervisor"]:
        raise HTTPException(status_code=403, detail="No autorizado")
        
    loop = asyncio.get_running_loop()
    count = await loop.run_in_executor(_WORKER_THREAD_POOL, distribute_sourced_leads)
    return {"status": "ok", "assigned": count}

async def captacion_distribution_loop():
    logger.info("[BACKGROUND] Iniciando loop de distribución de captaciones...")
    while True:
        try:
            background_tasks_status["captacion_distributor"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["captacion_distributor"]["status"] = "running"
            
            loop = asyncio.get_running_loop()
            count = await loop.run_in_executor(_WORKER_THREAD_POOL, distribute_sourced_leads)
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
    orden: str = "fecha", 
    ejecutivo: str = None,
    cursor: str = Query(None, description="ISO timestamp del último lead visto (cursor-based pagination)")
):
    username = await get_current_user(request)
    from chatbot.storage import get_async_db
    adb = get_async_db()
    user = await adb["usuarios"].find_one({"username": username})
    
    if not user:
        return RedirectResponse(url="/?error=sesion_invalida")

    user_role = user.get("rol", "agente")
    user_name = user.get("nombre", "")

    limit = 15  # Aumentado a 15 (sin skip, el costo es O(1))
    leads_task = get_crm_leads_list(
        filtro_estado=estado,
        busqueda=busqueda,
        ordenar_por=orden,
        user_role=user_role,
        user_name=user_name,
        ejecutivo_filter=ejecutivo,
        limit=limit,
        cursor_last_event_at=cursor
    )
    exec_task = get_unique_executives() if user_role in ["admin", "supervisor"] else asyncio.sleep(0, result=[])
    leads_payload, executives = await asyncio.gather(leads_task, exec_task)
    leads, kpis, total_count = leads_payload

    # next_cursor: el created_at del \u00faltimo lead de esta p\u00e1gina
    next_cursor = None
    if leads and len(leads) == limit:
        last_ts = leads[-1].get("created_timestamp")
        if last_ts:
            try:
                next_cursor = last_ts.isoformat() if hasattr(last_ts, "isoformat") else str(last_ts)
            except Exception:
                next_cursor = None

    return templates.TemplateResponse("crm_leads_list.html", {
        "request": request, 
        "leads": leads, 
        "kpis": kpis,
        "user_role": user_role,
        "user_name": user_name,
        "executives": executives,
        "current_ejecutivo": ejecutivo or "Todos",
        "pagination": {
            "total_count": total_count,
            "has_more": len(leads) == limit,
            "has_prev": cursor is not None,
            "next_cursor": next_cursor,
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
    from chatbot.storage import get_db as _gdb
    _db = _gdb()
    col = _db[Config.COLLECTION_CONTACTOS]
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
    if request.url.path.startswith("/api/"):
        from fastapi.responses import JSONResponse
        return JSONResponse({"status": "error", "message": "Sesión expirada o no autenticado"}, status_code=401)
    return RedirectResponse(url="/?error=sesion_expirada")

# ========================= 9. BACKGROUND LOOPS (REFACTORED) =========================

async def process_pending_leads_loop():
    logger.info("[BACKGROUND] Iniciando loop de leads pendientes...")
    while True:
        try:
            background_tasks_status["notifications_loop"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["notifications_loop"]["status"] = "running"
            
            if should_send_now():
                pending = await run_db("pending_notifications.find", get_pending_notifications)
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
                            p_code = lead_data.get("property_code") or lead_data.get("prospecto", {}).get("codigo")
                            if p_code:
                                logger.info(f"[BACKGROUND] Re-enrutando lead {lead_phone} por falta de destino válido...")
                                new_exec, new_phone, assignment_type = find_responsible_executive(
                                    property_code=p_code,
                                    lead_phone=lead_phone,
                                    lead_name=lead_data.get("nombre")
                                )
                                if new_phone and new_phone != "+56900000000":
                                    target_phone = new_phone
                                    p["target_name"] = new_exec
                                    # Actualizamos la data para que el mensaje se mande bien
                                    lead_data["target_phone"] = new_phone
                                    lead_data["target_name"] = new_exec
                                    logger.info(f"[BACKGROUND] Re-enrutado exitosamente a {new_exec} ({new_phone})")
                        
                        if not target_phone or target_phone == "+56900000000":
                            # Si después de re-enrutar sigue mal, lo marcamos para no ciclar eternamente
                            logger.warning(f"[BACKGROUND] Skipped: No se pudo encontrar destino válido para lead {lead_data.get('phone')}")
                            await run_db("pending_notifications.mark_sent", mark_notification_sent, p["_id"])
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
                                        lead_db = await run_db(
                                            "leads.find_one",
                                            (lambda: CrmService._db()["leads"].find_one({"phone": lead_phone}))
                                        ) if hasattr(CrmService, '_db') else None
                                        existing_exec = (lead_db or {}).get("ejecutivo_asignado") if lead_db else None
                                        from chatbot.constants import UNASSIGNED_LABEL
                                        unassigned = [UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", None, ""]
                                        if not existing_exec or existing_exec in unassigned:
                                            await run_db(
                                                "crm.assign_executive",
                                                CrmService.assign_executive,
                                                lead_phone,
                                                target_name,
                                                "LeadRouter"
                                            )
                                    except: pass
                            
                            success = await NotificationService.send_notification(
                                phone=target_phone,
                                message=msg,
                                alert_type="background_notification_group",
                                meta={"to": target_name, "count": len(items)},
                                dedup_window_minutes=5
                            )
                            
                            if success:
                                for item in items:
                                    await run_db("pending_notifications.mark_sent", mark_notification_sent, item["_id"])

                        # Si es solo uno, enviamos el template normal
                        else:
                            p = items[0]
                            lead_data = p.get("lead_data", {})
                            lead_phone = lead_data.get("phone")
                            prop_code = lead_data.get("property_code")
                            
                            logger.info(f"[BACKGROUND] Enviando lead individual {lead_phone} a {target_name}")
                            msg = await run_db(
                                "lead_router.format_whatsapp_template",
                                format_whatsapp_template,
                                lead_data,
                                target_name,
                                prop_code,
                                True
                            )
                            
                            if lead_phone:
                                try:
                                    from chatbot.storage import get_db as _get_db
                                    _lead_db = await run_db(
                                        "leads.find_one",
                                        (lambda: _get_db()["leads"].find_one({"phone": lead_phone}))
                                    )
                                    existing_exec = (_lead_db or {}).get("ejecutivo_asignado")
                                    from chatbot.constants import UNASSIGNED_LABEL
                                    unassigned = [UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", None, ""]
                                    if not existing_exec or existing_exec in unassigned:
                                        await run_db(
                                            "crm.assign_executive",
                                            CrmService.assign_executive,
                                            lead_phone,
                                            target_name,
                                            "LeadRouter"
                                        )
                                except: pass
                                
                            success = await NotificationService.send_notification(
                                phone=target_phone,
                                message=msg,
                                alert_type="background_notification",
                                meta={"to": target_name, "lead_phone": lead_phone},
                                dedup_window_minutes=5
                            )
                            if success:
                                await run_db("pending_notifications.mark_sent", mark_notification_sent, p["_id"])

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
            
            tasks = await run_db(
                "crm_tasks.find_due",
                lambda: list(db["crm_tasks"].find({"status": "pending", "execute_at": {"$lte": now}}))
            )
            
            if tasks:
                logger.info(f"[TASK_MONITOR] Procesando {len(tasks)} tareas vencidas...")
                for task in tasks:
                    try:
                        phone = task.get("phone")
                        note = task.get("note", "Sin detalles")
                        
                        is_captacion = task.get("lead_type") == "captacion"
                        if is_captacion:
                            from bson import ObjectId
                            lead = await run_db(
                                "yapo_propiedades.find_one",
                                lambda: db["yapo_propiedades"].find_one({"_id": ObjectId(task.get("obj_id"))})
                            )
                            if not lead:
                                await run_db(
                                    "crm_tasks.update_error_captacion_not_found",
                                    lambda: db["crm_tasks"].update_one(
                                        {"_id": task["_id"]},
                                        {"$set": {"status": "error", "error": "captacion_not_found"}}
                                    )
                                )
                                continue
                            ejecutivo = lead.get("gestion", {}).get("ejecutivo_asignado")
                            lead_name = lead.get("details", {}).get("publicador", "Cliente")
                            crm_link = f"https://www.procasa.cl/captacion/{task.get('obj_id')}"
                        else:
                            lead = await run_db(
                                "leads.find_one",
                                lambda: db["leads"].find_one({"phone": phone})
                            )
                            if not lead:
                                await run_db(
                                    "crm_tasks.update_error_lead_not_found",
                                    lambda: db["crm_tasks"].update_one(
                                        {"_id": task["_id"]},
                                        {"$set": {"status": "error", "error": "lead_not_found"}}
                                    )
                                )
                                continue
                            ejecutivo = lead.get("ejecutivo_asignado")
                            lead_name = lead.get("prospecto", {}).get("nombre", "Cliente")
                            crm_link = f"https://www.procasa.cl/crm/lead/{phone}"
                            
                        if not ejecutivo or ejecutivo in ["No asignado", "Sin Asignar"]:
                            continue
                            
                        exec_phone = get_executive_phone(ejecutivo)
                        if not exec_phone or exec_phone == "+56900000000":
                            continue
                            
                        msg_text = (
                            f"⏰ *Recordatorio CRM: {ejecutivo}*\n\n"
                            f"Tienes una acción programada para *{'la captación' if is_captacion else 'el lead'}* de *{lead_name}*.\n\n"
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
                            await run_db(
                                "crm_tasks.update_notified",
                                lambda: db["crm_tasks"].update_one(
                                    {"_id": task["_id"]},
                                    {"$set": {"status": "notified", "notified_at": now.isoformat(), "notification_sent_to": ejecutivo}}
                                )
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
        from chatbot.storage import get_db
        db = get_db()
        db["crm_tasks"].create_index([("status", 1), ("execute_at", 1)])
        db["crm_events"].create_index([("phone", 1), ("type", 1), ("timestamp", -1)])
        
        # --- OPTIMIZACIÓN CAPTACIÓN ---
        # Índice Compuesto para Lista (Estado + Ejecutivo + Score)
        try:
            db["yapo_propiedades"].create_index([
                ("gestion.estado", 1), 
                ("gestion.ejecutivo_asignado", 1), 
                ("score_captacion", -1)
            ], name="idx_yapo_gestion_ejecutivo_score")
        except Exception as idx_e:
            if "IndexOptionsConflict" in str(idx_e):
                logger.warning("IndexOptionsConflict detectado. Eliminando índice antiguo...")
                try:
                    db["yapo_propiedades"].drop_index("idx_yapo_gestion_ejecutivo_score")
                    db["yapo_propiedades"].drop_index("gestion.estado_1_gestion.ejecutivo_asignado_1_score_captacion_-1")
                except:
                    pass
                db["yapo_propiedades"].create_index([
                    ("gestion.estado", 1), 
                    ("gestion.ejecutivo_asignado", 1), 
                    ("score_captacion", -1)
                ], name="idx_yapo_gestion_ejecutivo_score")
            else:
                logger.warning(f"Error creando índice yapo_propiedades: {idx_e}")
                
        # Índice para Búsqueda por Comuna Normalizada + Score
        try:
            db["yapo_propiedades"].create_index([
                ("details.comuna_norm", 1), 
                ("score_captacion", -1)
            ])
        except Exception as e:
            logger.warning(f"Error creando índice comuna_norm: {e}")
        # Índice para Market Insights
        db["universo_cartera"].create_index([("comuna", 1), ("tipo", 1)])
        # Índices para respuestas de campañas por email
        db[Config.COLLECTION_CONTACTOS].create_index([("email_propietario_lc", 1)], name="idx_contactos_email_lc")
        db[Config.COLLECTION_CAMPANAS_LOG].create_index([("token", 1)], name="idx_campanas_token")
        
        logger.info("Índices de CRM y Captación asegurados.")
    except Exception as e:
        logger.warning(f"Error creando índices: {e}")

import functools

async def run_db(operation_name: str, fn, *args, **kwargs):
    """
    Ejecuta operaciones síncronas de PyMongo en el _WORKER_THREAD_POOL.
    Evita congelamientos del Event Loop de FastAPI por timeouts de red de Mongo.
    """
    loop = asyncio.get_running_loop()
    t0 = time.time()
    try:
        if kwargs or args:
            func = functools.partial(fn, *args, **kwargs)
        else:
            func = fn
            
        result = await loop.run_in_executor(_WORKER_THREAD_POOL, func)
        duration_ms = (time.time() - t0) * 1000
        
        # Log solo de operaciones lentas > 2000ms
        if duration_ms > 2000:
            logger.warning(f"[BG_MONGO] loop=background operation={operation_name} duration={duration_ms:.0f}ms")
            
        return result
    except Exception as e:
        logger.error(f"[BG_MONGO] ERROR en {operation_name}: {e}")
        raise e

async def inactive_lead_nudge_loop():
    logger.info("[NUDGE_LOOP] Iniciando monitor de reactivación (Nudge) de leads inactivos...")
    while True:
        try:
            background_tasks_status["nudge_loop"] = {"status": "running", "last_heartbeat": datetime.now(CHILE_TZ).isoformat()}
            
            from chatbot.storage import get_db
            from chatbot.whatsapp_client import send_whatsapp_message
            db = get_db()
            
            now_utc = datetime.utcnow()
            limit_max = now_utc - timedelta(hours=12) # No revivir muertos
            limit_min = now_utc - timedelta(minutes=25) # Buffer antes de chequear el dinamismo real
            
            # INTENCIONES AVANZADAS — no molestar si el lead ya está en proceso de visita/gestión
            INTENTS_NO_NUDGE = {
                "ASK_VISIT", "VISIT_SCHEDULED", "VISIT_DONE",
                "GIVE_OFFER", "NEGOTIATION", "CLOSED_WON"
            }
            # Labels de ejecutivos "sin asignar" — si tiene ejecutivo real, no enviar nudge
            from chatbot.constants import UNASSIGNED_LABEL
            UNASSIGNED_LABELS = {
                UNASSIGNED_LABEL, "No Asignado", "No asignado",
                "Sin Asignar", "Sin asignar", "N/A", "", None
            }

            now_cl = datetime.now(CHILE_TZ)
            today_str = now_cl.strftime("%Y-%m-%d")

            # --- COOLDOWN: max 1 nudge/día, mínimo 6h entre nudges ---
            NUDGE_COOLDOWN_HOURS   = 6    # mínimo entre envios al mismo lead
            NUDGE_MAX_PER_DAY     = 1    # máximo nudges por lead por día
            NUDGE_MAX_TOTAL       = 3    # máximo nudges históricos por lead (abandona después)

            query = {
                "stage": {"$nin": ["ARCHIVED", "REJECTED", "CLOSED_LOST", "CLOSED_WON", "OFFER", "NEGOTIATION", "VISIT_DONE", "VISIT_SCHEDULED"]},
                # Solo leads que: no alcanzaron el máximo de nudges históricos
                "$or": [
                    {"nudge_count": {"$exists": False}},
                    {"nudge_count": {"$lt": NUDGE_MAX_TOTAL}}
                ],
                # Y no han tenido nudge HOY
                "nudge_last_date": {"$ne": today_str},
                "messages.0": {"$exists": True}
            }

            def _fetch_nudge_leads():
                return list(db["leads"].find(query, {
                    "phone": 1, "messages": 1, "stage": 1,
                    "ejecutivo_asignado": 1, "prospecto": 1,
                    "nudge_count": 1, "nudge_sent_at": 1, "nudge_last_date": 1,
                    "last_intent": 1, "lifecycle": 1
                }))
            leads = await run_db("nudge_loop_find", _fetch_nudge_leads)
            
            for lead in leads:
                messages = lead.get("messages", [])
                if not messages:
                    continue

                # ─── FILTRO 1: ya tiene ejecutivo real asignado → el humano se encarga ───
                ejecutivo = (
                    lead.get("ejecutivo_asignado") or
                    lead.get("prospecto", {}).get("ejecutivo")
                )
                if ejecutivo not in UNASSIGNED_LABELS:
                    logger.debug(f"[NUDGE] Omitido {lead.get('phone')}: ya asignado a {ejecutivo}.")
                    _lead_id = lead["_id"]
                    await run_db("nudge_skip_executive", lambda: db["leads"].update_one({"_id": _lead_id}, {"$set": {"nudge_status": "skipped_has_executive"}}))
                    continue

                # ─── FILTRO 2: intención avanzada (visita, oferta, negociación) ───
                last_intent = lead.get("last_intent") or lead.get("prospecto", {}).get("last_intent", "")
                if str(last_intent).upper() in INTENTS_NO_NUDGE:
                    logger.debug(f"[NUDGE] Omitido {lead.get('phone')}: intención avanzada '{last_intent}'.")
                    _lead_id = lead["_id"]
                    await run_db("nudge_skip_intent", lambda: db["leads"].update_one({"_id": _lead_id}, {"$set": {"nudge_status": "skipped_advanced_intent"}}))
                    continue

                last_msg = messages[-1]
                # Solo si el último que habló fue el BOT
                if last_msg.get("role") != "assistant":
                    continue

                # ─── FILTRO 3: el bot ya respondió varias veces (conversación activa) ───
                bot_msgs_count  = sum(1 for m in messages if m.get("role") == "assistant")
                user_msgs_count = sum(1 for m in messages if m.get("role") == "user")

                # Si el cliente interactuó más de 1 vez y el bot respondió más de 2 veces,
                # la conversación ya está en marcha — NO mandar nudge genérico.
                if user_msgs_count >= 2 and bot_msgs_count >= 3:
                    logger.debug(f"[NUDGE] Omitido {lead.get('phone')}: conversación activa ({user_msgs_count}u/{bot_msgs_count}b msgs).")
                    _lead_id = lead["_id"]
                    await run_db("nudge_skip_active", lambda: db["leads"].update_one({"_id": _lead_id}, {"$set": {"nudge_status": "skipped_active_conversation"}}))
                    continue

                # Ver cuándo fue el último mensaje
                last_time_str = last_msg.get("timestamp") or last_msg.get("time")
                if not last_time_str:
                    continue

                try:
                    last_time = datetime.fromisoformat(str(last_time_str).replace("Z", "+00:00")).astimezone(pytz.utc).replace(tzinfo=None)
                except:
                    continue

                time_diff_mins = (now_utc - last_time).total_seconds() / 60.0

                # Descartar si es muy viejo o muy reciente
                if time_diff_mins > 720 or time_diff_mins < 30:
                    continue

                # Timing dinámico: 1 solo mensaje de usuario = esperar 60 min. Varios = 45 min.
                threshold_mins = 60 if user_msgs_count <= 1 else 45

                if time_diff_mins >= threshold_mins:
                    # --- COOLDOWN: m\u00ednimo 6h entre nudges al mismo lead ---
                    nudge_sent_at_str = lead.get("nudge_sent_at")
                    if nudge_sent_at_str:
                        try:
                            last_nudge_dt = datetime.fromisoformat(str(nudge_sent_at_str).replace("Z", "+00:00")).astimezone(pytz.utc).replace(tzinfo=None)
                            hours_since = (now_utc - last_nudge_dt).total_seconds() / 3600
                            if hours_since < NUDGE_COOLDOWN_HOURS:
                                logger.debug(f"[NUDGE] Cooldown activo {lead.get('phone')}: \u00faltimo hace {hours_since:.1f}h (m\u00edn {NUDGE_COOLDOWN_HOURS}h)")
                                continue
                        except Exception:
                            pass

                    # Re-validar en tiempo real antes de enviar (async)
                    _lead_id = lead["_id"]
                    fresh_lead = await run_db("nudge_prevalidate", lambda: db["leads"].find_one({"_id": _lead_id}))
                    fresh_msgs = (fresh_lead.get("messages", []) or []) if fresh_lead else []
                    if fresh_msgs and fresh_msgs[-1].get("role") == "user":
                        logger.info(f"[NUDGE] Cancelado para {lead.get('phone')} (Respondi\u00f3 justo ahora).")
                        continue

                    phone = lead.get("phone")
                    nudge_count = (lead.get("nudge_count") or 0) + 1
                    nudge_text = (
                        "Hola \U0001f642 Solo quer\u00eda saber si tienes alguna pregunta adicional "
                        "sobre la propiedad. Estoy aqu\u00ed para ayudarte. "
                        "Si no es buen momento, no hay problema. \U0001f44d"
                    )

                    logger.info(f"[NUDGE] Enviando reactivaci\u00f3n #{nudge_count} a {phone} (Inactivo {int(time_diff_mins)} min, Umbral: {threshold_mins})")
                    sent = await send_whatsapp_message(phone, nudge_text)

                    if sent:
                        from chatbot.storage import guardar_mensaje
                        now_cl_str = datetime.now(CHILE_TZ).isoformat()
                        guardar_mensaje(phone, "assistant", nudge_text, {
                            "tipo": "nudge_reactivacion",
                            "intencion": "reactivacion_automatica"
                        })
                        _lead_id = lead["_id"]
                        _update = {"$set": {"nudge_sent_at": now_cl_str, "nudge_last_date": today_str, "nudge_count": nudge_count}}
                        await run_db("nudge_update", lambda: db["leads"].update_one({"_id": _lead_id}, _update))
                        await asyncio.sleep(5)  # Throttling ligero
                        
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[NUDGE_LOOP] Error global: {e}")
            if "nudge_loop" in background_tasks_status:
                background_tasks_status["nudge_loop"]["status"] = f"error: {str(e)}"
        
        await asyncio.sleep(300)  # Chequear cada 5 minutos (antes era 2min)

async def lead_consumer_worker(worker_id: int):
    """
    Consumidor aislado. Toma leads de la cola y los procesa usando el _WORKER_THREAD_POOL.
    Mantiene el ritmo estable y no satura el event loop ni el default executor.
    """
    logger.info(f"[CONSUMER-{worker_id}] Worker iniciado y escuchando cola...")
    loop = asyncio.get_running_loop()
    while True:
        try:
            lead_id = await lead_processing_queue.get()
            try:
                t0 = time.time()
                # Worker pool dedicado: evita interferencia con requests HTTP.
                await loop.run_in_executor(_WORKER_THREAD_POOL, LeadProcessingService.process_lead, lead_id)
                elapsed_ms = (time.time() - t0) * 1000
                logger.debug(f"[CONSUMER-{worker_id}] Lead {lead_id} procesado en {elapsed_ms:.0f}ms")
            except Exception as le:
                logger.error(f"[CONSUMER-{worker_id}] Error procesando lead {lead_id}: {le}")
            finally:
                lead_processing_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[CONSUMER-{worker_id}] Error crítico en worker: {e}")
            await asyncio.sleep(5)

async def reassign_unassigned_leads_loop():
    """
    PRODUCTOR: Busca leads pendientes en Mongo cada 5 minutos y los encola.
    Ya no procesa nada por su cuenta ni agrupa en batches pesados.
    """
    logger.info("[PRODUCER] Iniciando scanner de leads pendientes en background...")
    while True:
        try:
            background_tasks_status["lead_processing"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["lead_processing"]["status"] = "running"
            
            db = LeadProcessingService._db() if hasattr(LeadProcessingService, '_db') else None
            if db is None:
                from chatbot.storage import get_db
                db = get_db()

            from chatbot.constants import UNASSIGNED_LABEL
            unassigned_labels = [UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", None, ""]
            
            query = {
                "stage": {"$nin": ["ARCHIVED", "REJECTED", "CLOSED_LOST", "CLOSED_WON"]},
                "$or": [
                    {"cluster_id": {"$exists": False}},
                    {"cluster_id": {"$in": [None, ""]}},
                    {"zone": {"$exists": False}},
                    {"zone": {"$in": [None, ""]}},
                    {"ejecutivo_asignado": {"$in": unassigned_labels}},
                    {"prospecto.ejecutivo": {"$in": unassigned_labels}}
                ]
            }
            
            def _find_unassigned():
                return list(db["leads"].find(query, {"_id": 1}).limit(20))
            leads = await run_db("reassign_producer_find", _find_unassigned)
            if leads:
                logger.info(f"[PRODUCER] Encontró {len(leads)} leads pendientes. Encolando...")
                for lead in leads:
                    await lead_processing_queue.put(lead["_id"])
            
            await asyncio.sleep(3600)  # Revisar cada 1 hora en lugar de 5 minutos
        except asyncio.CancelledError:
            break
        except Exception as e:
            background_tasks_status["lead_processing"]["status"] = f"error: {str(e)}"
            logger.error(f"[BACKGROUND] Error en loop de procesamiento de leads: {e}")
            await asyncio.sleep(60)

async def cache_prewarmer_loop():
    """
    PRE-WARMING DE CACHE: Refresca get_leads_executive_report() cada 4.5 minutos
    en background (en executor), garantizando que NUNCA haya un cache miss
    durante un request HTTP real.
    El cache TTL es 5 min; este loop refresca a 4.5 min → siempre hay datos frescos.
    """
    logger.info("[CACHE_WARMER] Iniciando pre-warming de cache leads-intelligence (smart mode)...")
    # Espera inicial para no competir con el startup
    await asyncio.sleep(30)
    local_warm_in_progress = False
    cache_key = "leads_executive_report_v2"
    lock_key = "lock_cache_prewarm_leads_intel_v1"
    while True:
        try:
            # Evitar solapes locales de warm si el ciclo previo no cerró aún.
            if local_warm_in_progress:
                await asyncio.sleep(30)
                continue

            from chatbot.storage import get_db
            from datetime import timezone
            db = get_db()
            now_utc = datetime.now(timezone.utc)

            # 1) Skip inteligente: si cache aún tiene >120s de vida, no recalcular.
            loop_ref = asyncio.get_running_loop()
            cache_doc = await loop_ref.run_in_executor(
                _WORKER_THREAD_POOL,
                lambda: db["cache_store"].find_one({"_id": cache_key}, {"expires_at": 1})
            )
            expires_at = cache_doc.get("expires_at") if cache_doc else None
            if expires_at:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                ttl_left = (expires_at - now_utc).total_seconds()
                if ttl_left > 120:
                    logger.info(f"[CACHE_WARMER] Skip: cache vigente ({ttl_left:.0f}s restantes)")
                    await asyncio.sleep(60)
                    continue

            # 2) Lock distribuido: evita prewarm simultáneo entre instancias/restarts.
            lock_until = now_utc + timedelta(seconds=90)
            def _acquire_lock():
                return db["cache_locks"].find_one_and_update(
                    {"_id": lock_key, "$or": [{"expires_at": {"$exists": False}}, {"expires_at": {"$lte": now_utc}}]},
                    {"$set": {"expires_at": lock_until, "updated_at": now_utc}},
                    upsert=True, return_document=ReturnDocument.AFTER
                )
            lock_doc = await loop_ref.run_in_executor(_WORKER_THREAD_POOL, _acquire_lock)
            if not lock_doc:
                logger.info("[CACHE_WARMER] Skip: lock activo en otra instancia")
                await asyncio.sleep(60)
                continue

            local_warm_in_progress = True
            loop = asyncio.get_running_loop()
            t0 = time.time()
            # Warmer pool dedicado: evita competir con workers de procesamiento.
            await asyncio.wait_for(
                loop.run_in_executor(_WARMER_THREAD_POOL, get_leads_executive_report),
                timeout=8.0
            )
            elapsed_ms = (time.time() - t0) * 1000
            logger.info(f"[CACHE_WARMER] LEADS_INTELLIGENCE: cache pre-warmed en {elapsed_ms:.0f}ms")
            # Liberar lock explícitamente tras warm exitoso.
            await loop_ref.run_in_executor(_WORKER_THREAD_POOL, lambda: db["cache_locks"].update_one({"_id": lock_key}, {"$set": {"expires_at": now_utc}}))
        except asyncio.TimeoutError:
            logger.warning("[CACHE_WARMER] Timeout >8s; se omite este ciclo para evitar jitter")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[CACHE_WARMER] Error al pre-warm cache: {e}")
        finally:
            local_warm_in_progress = False
        await asyncio.sleep(60)

async def event_loop_monitor_loop():
    """
    Monitor global del event loop. Mide el lag real para detectar operaciones bloqueantes.
    """
    logger.info("[EVENT_LOOP_MONITOR] Iniciando monitor de lag...")
    while True:
        try:
            start = time.time()
            await asyncio.sleep(1.0)
            duration = time.time() - start
            lag_ms = (duration - 1.0) * 1000
            
            if lag_ms > 1000:
                logger.error(f"[EVENT_LOOP_BLOCKED] lag={lag_ms:.0f}ms possible_blocking_operation=true")
                # Dump completo solo en bloqueos severos para evitar ruido excesivo.
                if lag_ms > 5000:
                    now = time.time()
                    for task in asyncio.all_tasks():
                        if task.done():
                            continue
                        coro = task.get_coro()
                        task_name = task.get_name()
                        state = "cancelled" if task.cancelled() else "pending"
                        stack_frames = task.get_stack(limit=8)
                        stack_text = ""
                        if stack_frames:
                            stack_text = "".join(traceback.format_list(traceback.extract_stack(stack_frames[-1])))
                        logger.error(
                            f"[EVENT_LOOP_TASK_DUMP] name={task_name} coro={getattr(coro, '__qualname__', str(coro))} "
                            f"state={state} ts={now:.3f} stack={stack_text[:1200]}"
                        )
            elif lag_ms > 250:
                logger.warning(f"[EVENT_LOOP_BLOCKED] lag={lag_ms:.0f}ms possible_blocking_operation=true")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[EVENT_LOOP_MONITOR] Error: {e}")
            await asyncio.sleep(5)

async def threadpool_forensics_loop():
    """Forensics de threadpools: tamaño, ocupación aproximada y cola."""
    logger.info("[THREADPOOL_MONITOR] Iniciando monitor de threadpools...")
    last_snapshot = {}
    last_heartbeat_log = 0.0
    saturation_streak = {"WEB": 0, "WORKER": 0, "WARMER": 0}
    while True:
        try:
            pools = [
                ("WEB", _WEB_THREAD_POOL),
                ("WORKER", _WORKER_THREAD_POOL),
                ("WARMER", _WARMER_THREAD_POOL),
            ]
            for name, pool in pools:
                max_workers = getattr(pool, "_max_workers", -1)
                threads = getattr(pool, "_threads", set())
                active_threads = len([t for t in threads if t.is_alive()])
                q = getattr(pool, "_work_queue", None)
                queued = q.qsize() if q is not None and hasattr(q, "qsize") else -1
                snapshot = (active_threads, max_workers, queued)
                prev = last_snapshot.get(name)
                now = time.time()
                # Reducir ruido: log solo cuando cambia estado o cada 60s como heartbeat.
                if prev != snapshot or (now - last_heartbeat_log) >= 60:
                    logger.info(
                        f"[THREADPOOL_FORENSICS] pool={name} active={active_threads} max={max_workers} queued={queued}"
                    )
                    last_snapshot[name] = snapshot
                # Saturación real sostenida: evitar alertas por picos breves de cola=1.
                is_saturated_now = (
                    queued >= 3 and
                    max_workers > 0 and
                    active_threads >= max_workers
                )
                if is_saturated_now:
                    saturation_streak[name] = saturation_streak.get(name, 0) + 1
                else:
                    saturation_streak[name] = 0

                # Alertar solo si se mantiene en al menos 2 ciclos consecutivos (~10s).
                if saturation_streak[name] >= 2:
                    logger.warning(
                        f"[THREADPOOL_SATURATED] pool={name} queued={queued} active={active_threads} max={max_workers}"
                    )
                if (now - last_heartbeat_log) >= 60:
                    last_heartbeat_log = now
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[THREADPOOL_MONITOR] Error: {e}")
        await asyncio.sleep(5)

async def daily_report_loop():
    """Loop de fondo para enviar el reporte de SLA y Captaciones una vez al día."""
    logger.info("[DAILY_REPORT] Iniciando monitor de reporte diario (SLA + Captaciones)...")
    from chatbot.daily_report import check_and_run_daily_report
    from chatbot.captacion_report import check_and_run_meta_diaria_report
    while True:
        try:
            background_tasks_status["daily_report"] = {
                "status": "running", 
                "last_heartbeat": datetime.now(CHILE_TZ).isoformat()
            }
            # Reporte 1: Leads críticos SLA (09:30 AM) - DESACTIVADO TEMPORALMENTE A PETICIÓN DEL USUARIO
            # await check_and_run_daily_report()
            # Reporte 2: Meta Diaria de Captaciones (09:00 AM)
            # await check_and_run_meta_diaria_report()  # DESACTIVADO A PETICIÓN DEL USUARIO
        except Exception as e:
            logger.error(f"[DAILY_REPORT] Error en loop: {e}")
            if "daily_report" in background_tasks_status:
                background_tasks_status["daily_report"]["status"] = f"error: {str(e)}"
        
        # Revisar cada 5 minutos
        await asyncio.sleep(300)

if __name__ == "__main__":
    import pathlib
    module_name = pathlib.Path(__file__).stem
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Bot PRO iniciado → http://localhost:{port}")
    uvicorn.run(f"{module_name}:app", host="0.0.0.0", port=port, reload=True, log_level="info")




