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
from chatbot.storage import observability_mark, observability_snapshot_and_reset, observability_event_loop_blocked_recent, run_in_threadpool

# ========================= THREAD POOL CONTROLADO =========================
# Pool separado para request web (evita que tareas batch bloqueen respuestas HTTP).
_WEB_THREAD_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="procasa_web")
# Pool separado para workers de procesamiento de leads.
_WORKER_THREAD_POOL = ThreadPoolExecutor(max_workers=5, thread_name_prefix="procasa_worker")
# Pool dedicado para tareas periódicas (cache warmer) para evitar competir con workers.
_WARMER_THREAD_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="procasa_warmer")

# === NUEVAS IMPORTACIONES PARA GOOGLE ===
import httpx 
from urllib.parse import urlencode, urlsplit

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
from api_crm import get_crm_leads_list, get_lead_detail_data, update_lead_crm_data, log_crm_event, manage_crm_notes, get_unique_executives, get_semantic_recommendations, log_recommendation_sent, normalize_crm_temperature

# ---- ANALYTICS (READ-ONLY) ----
from analytics.leads_service import (
    get_summary, get_trends, get_distributions, get_table as analytics_get_table,
    get_detail as analytics_get_detail, get_filters, get_field_coverage,
    get_dashboard, get_commercial_dashboard,
)

from api_captacion import (
    get_captacion_list, get_captacion_detail, update_captacion_status, update_contact_info,
    distribute_sourced_leads, release_stale_captaciones, redistribute_inactive_agent_captaciones,
    format_relative_time as format_captacion_time,
    get_personal_templates, save_personal_template, delete_personal_template
)
from captacion_kpis import VISIBLE_CLASSIFICATION_STATES, build_kpi_queries
from captacion_goals import (
    CAPTACION_PRIVILEGED_ROLES,
    can_manage_captacion,
    get_captacion_goal_dashboard,
)
from captacion_workforce import create_work_exception, upsert_calendar_day, upsert_membership
from chatbot.manual_entry import create_manual_lead, check_lead_duplicate, resolve_property_code
from chatbot.processing_service import LeadProcessingService
from chatbot.crm_permissions import (
    can_administer_leads,
    lead_is_assigned_to_user,
    payload_attempts_reassignment,
)
from chatbot.crm_service import CrmService
from chatbot.captacion_weekly_report import (
    REPORT_COLLECTION as CAPTACION_WEEKLY_REPORT_COLLECTION,
    acknowledge_outcome_review,
    approve_and_send_report,
    cancel_report,
    create_weekly_report,
    record_delivery_status_webhook,
    regenerate_report_narrative,
    send_test_report,
)

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
        logger.info("[ASYNC_FIX] subprocess forced to threadpool")
        result_box = {}
        error_box = {}

        def _runner():
            try:
                result_box["value"] = asyncio.run(asyncio.to_thread(_ORIG_SUBPROCESS_RUN, *args, **kwargs))
            except Exception as exc:
                error_box["error"] = exc

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()
        if "error" in error_box:
            raise error_box["error"]
        return result_box.get("value")
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
    logger.info("[CRM_FLAGS] %s", {
        "lead_hot_notifications": Config.LEAD_HOT_NOTIFICATIONS_ENABLED,
        "lead_hot_reconciliation": Config.LEAD_HOT_RECONCILIATION_ENABLED,
        "lead_cold_digest": Config.LEAD_COLD_DIGEST_ENABLED,
        "sla_shadow": Config.CRM_SLA_SHADOW_ENABLED,
        "sla_alerts": Config.CRM_SLA_ALERTS_ENABLED,
        "weekly_generation": Config.CRM_WEEKLY_REPORT_GENERATION_ENABLED,
        "weekly_send": Config.CRM_WEEKLY_REPORT_SEND_ENABLED,
        "legacy_daily_report": Config.CRM_LEGACY_DAILY_REPORT_ENABLED,
        "inactive_nudge": Config.CRM_INACTIVE_NUDGE_ENABLED,
    })

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
    from chatbot.crm_weekly_report import crm_weekly_scheduler_loop
    crm_weekly_task = asyncio.create_task(crm_weekly_scheduler_loop())
    
    # Iniciar Consumers
    c1_task = asyncio.create_task(lead_consumer_worker(1))
    c2_task = asyncio.create_task(lead_consumer_worker(2))
    
    # Crear admin y asegurar índices
    crear_admin_si_no_existe()
    asegurar_indices_db()
    
    # Invalidar cachés antiguos de captación (versiones pre-migración)
    try:
        from chatbot.storage import get_db
        _db_clean = get_db()
        result = _db_clean["system_cache"].delete_many({
            "_id": {"$regex": "^(captacion_resp_|captacion_count_|all_raw_comunas_)"}
        })
        if result.deleted_count > 0:
            logger.info(f"Cachés de captación antiguos invalidados: {result.deleted_count}")
    except Exception as e:
        logger.warning(f"No se pudieron invalidar cachés antiguos: {e}")
    
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
    crm_weekly_task.cancel()
    c1_task.cancel()
    c2_task.cancel()
    try:
        await asyncio.gather(
            n_task, s_task, t_task, c_task, r_task, d_task, nudge_task, w_task, el_task, tp_task, crm_weekly_task, c1_task, c2_task,
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
    request.state.trace_id = request_id
    
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
            logger.debug(log_str)
            
            if duration_ms > 3000:
                logger.error(f"[SLOW_REQUEST] ERROR: request_id={request_id} path={request.url.path} duration={duration_ms:.0f}ms")
            elif duration_ms > 1000:
                logger.warning(f"[SLOW_REQUEST] WARNING: request_id={request_id} path={request.url.path} duration={duration_ms:.0f}ms")
                
        response.headers["X-Process-Time"] = str(duration_ms)
        response.headers["X-Request-ID"] = request_id
        try:
            snap = observability_snapshot_and_reset()
            status_level = "OK" if snap["mongo_sync_on_loop"] == 0 and snap["event_loop_blocked"] == 0 else "DEGRADED"
            if snap["mongo_sync_on_loop"] > 5 or snap["event_loop_blocked"] > 5:
                status_level = "CRITICAL"
                
            summary_msg = (
                f"[REQUEST_SUMMARY]\ntrace={request_id}\n"
                f"mongo_calls=none\nmongo_sync_violations={snap['mongo_sync_on_loop']}\n"
                f"event_loop_blocked={snap['event_loop_blocked']}\n"
                f"duration_ms={duration_ms:.0f}\nstatus={status_level}"
            )
            if status_level == "CRITICAL":
                logger.error(summary_msg)
            elif status_level == "DEGRADED":
                logger.warning(summary_msg)
            else:
                logger.debug(summary_msg)
        except Exception:
            logger.exception(f"[REQUEST_SUMMARY] trace={request_id} error=summary_failed")
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

from api_crm_weekly_report import router as crm_weekly_router
app.include_router(crm_weekly_router)

from chatbot.lead_router import should_send_now, format_whatsapp_template
from chatbot.storage import (
    get_db,
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

def _safe_login_next(value: str | None) -> str | None:
    """Accept only local CRM paths as post-login destinations."""
    if not value or not str(value).startswith("/") or str(value).startswith("//"):
        return None
    parsed = urlsplit(str(value))
    if parsed.scheme or parsed.netloc:
        return None
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")

@app.get("/login/google")
async def login_google(request: Request, next: str = Query(None)):
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
    response = RedirectResponse(url)
    next_url = _safe_login_next(next) or _safe_login_next(request.cookies.get("login_next"))
    if next_url:
        response.set_cookie("login_next", next_url, httponly=True, secure=True, samesite="lax", max_age=600)
    return response

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
        default_url = "/leads-dashboard" if user_rol == "supervisor" else "/crm"
        target_url = _safe_login_next(request.cookies.get("login_next")) or default_url
        access_token_jwt = create_access_token({"sub": user_sub})
        response = RedirectResponse(target_url, status_code=303)
        response.set_cookie(key="access_token", value=access_token_jwt, httponly=True, secure=True, samesite="lax", max_age=7200)
        response.delete_cookie("login_next")
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
    next_url = _safe_login_next(request.query_params.get("next")) or _safe_login_next(request.cookies.get("login_next"))
    response = templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "images": get_images(),
            "error": request.query_params.get("error"),
            "next_url": next_url or "",
        }
    )
    if next_url:
        response.set_cookie("login_next", next_url, httponly=True, secure=True, samesite="lax", max_age=600)
    return response

@app.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form(None)):
    try:
        from chatbot.storage import get_async_db
        db = get_async_db()
        usuarios = db["usuarios"]
        user = await usuarios.find_one({"username": username})
        
        if user and verify_password(password, user.get("hashed_password", "")):
            user_rol = user.get("rol", "agente")
            default_url = "/leads-dashboard" if user_rol == "supervisor" else "/crm"
            target_url = _safe_login_next(next) or _safe_login_next(request.cookies.get("login_next")) or default_url

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
            response.delete_cookie("login_next")
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

# ========================= COMMERCIAL DASHBOARD (READ-ONLY) =========================

@app.get("/analytics/commercial", response_class=HTMLResponse)
async def commercial_dashboard_page(request: Request):
    # TEMP: public access for testing — remove after validation
    return templates.TemplateResponse("analytics/commercial_dashboard.html", {
        "request": request,
        "user_role": "admin",
        "user_name": "Test",
    })


@app.get("/api/analytics/commercial-dashboard")
async def api_commercial_dashboard(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
    executive: str = Query(None),
    source: str = Query(None),
    operation: str = Query(None),
    property_type: str = Query(None),
    commune: str = Query(None),
    temperature: str = Query(None),
    property_code: str = Query(None),
    assignment: str = Query(None),
):
    # TEMP: public access for testing — remove after validation
    filters = {}
    if source: filters["source"] = source
    if operation: filters["operation"] = operation
    if property_type: filters["property_type"] = property_type
    if commune: filters["commune"] = commune
    if temperature: filters["temperature"] = temperature
    if property_code: filters["property_code"] = property_code
    if assignment: filters["assignment"] = assignment

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_commercial_dashboard(
            period_start=period_start,
            period_end=period_end,
            executive=executive,
            role="admin",
            user_name="Test",
            filters=filters or None,
        ),
    )

# ========================= ANALYTICS DASHBOARD (READ-ONLY) =========================

@app.get("/analytics/leads", response_class=HTMLResponse)
async def analytics_leads_page(request: Request):
    user = await get_current_user_doc(request)
    if not user:
        return RedirectResponse(url="/?error=sesion_invalida")
    return templates.TemplateResponse("analytics/leads_dashboard.html", {
        "request": request,
        "user_role": user.get("rol", "agente"),
        "user_name": user.get("nombre", ""),
    })


@app.get("/api/analytics/leads/summary")
async def api_analytics_leads_summary(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
    executive: str = Query(None),
    stage: str = Query(None),
    temperature: str = Query(None),
    source: str = Query(None),
):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    loop = asyncio.get_running_loop()
    filters = {}
    if stage:
        filters["stage"] = stage
    if temperature:
        filters["temperature"] = temperature
    if source:
        filters["source"] = source
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_summary(
            period_start=period_start,
            period_end=period_end,
            executive=executive,
            role=user.get("rol"),
            user_name=user.get("nombre"),
            filters=filters or None,
        ),
    )


@app.get("/api/analytics/leads/trends")
async def api_analytics_leads_trends(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_trends(period_start=period_start, period_end=period_end),
    )


@app.get("/api/analytics/leads/distributions")
async def api_analytics_leads_distributions(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
    executive: str = Query(None),
    universe: str = Query("current_active"),
    stage: str = Query(None),
    temperature: str = Query(None),
    source: str = Query(None),
):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    loop = asyncio.get_running_loop()
    filters = {}
    if stage:
        filters["stage"] = stage
    if temperature:
        filters["temperature"] = temperature
    if source:
        filters["source"] = source
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_distributions(
            period_start=period_start,
            period_end=period_end,
            executive=executive,
            role=user.get("rol"),
            user_name=user.get("nombre"),
            universe=universe,
            filters=filters or None,
        ),
    )


@app.get("/api/analytics/leads/table")
async def api_analytics_leads_table(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    sort_by: str = Query("created_at"),
    sort_dir: str = Query("desc"),
    executive: str = Query(None),
    stage: str = Query(None),
    temperature: str = Query(None),
    source: str = Query(None),
    search: str = Query(None),
    universe: str = Query("current_active"),
    period_start: str = Query(None),
    period_end: str = Query(None),
):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    loop = asyncio.get_running_loop()
    filters = {}
    if stage:
        filters["stage"] = stage
    if temperature:
        filters["temperature"] = temperature
    if source:
        filters["source"] = source
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: analytics_get_table(
            page=page,
            limit=limit,
            sort_by=sort_by,
            sort_dir=sort_dir,
            executive=executive,
            role=user.get("rol"),
            user_name=user.get("nombre"),
            filters=filters or None,
            search=search,
            universe=universe,
            period_start=period_start,
            period_end=period_end,
        ),
    )


@app.get("/api/analytics/leads/filters")
async def api_analytics_leads_filters(
    request: Request,
    executive: str = Query(None),
):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_filters(
            executive=executive,
            role=user.get("rol"),
            user_name=user.get("nombre"),
        ),
    )


@app.get("/api/analytics/leads/coverage")
async def api_analytics_leads_coverage(
    request: Request,
    executive: str = Query(None),
    universe: str = Query("current_active"),
):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_field_coverage(
            executive=executive,
            role=user.get("rol"),
            user_name=user.get("nombre"),
            universe=universe,
        ),
    )


@app.get("/api/analytics/leads/dashboard")
async def api_analytics_leads_dashboard(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
    executive: str = Query(None),
):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_dashboard(
            period_start=period_start,
            period_end=period_end,
            executive=executive,
            role=user.get("rol"),
            user_name=user.get("nombre"),
        ),
    )


@app.get("/api/analytics/leads/{lead_id}/detail")
async def api_analytics_leads_detail(request: Request, lead_id: str):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")

    if not lead_id or len(lead_id) < 12:
        raise HTTPException(status_code=400, detail="ID inv\u00e1lido")

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: analytics_get_detail(lead_id),
    )
    if not data:
        raise HTTPException(status_code=404, detail="Lead no encontrado")

    # Verificar propiedad: agente solo ve sus leads
    if user.get("rol") not in ("admin", "supervisor"):
        exec_name = user.get("nombre", "")
        lead_exec = (data.get("public") or {}).get("ejecutivo", "")
        if exec_name and lead_exec and exec_name.strip() != lead_exec.strip():
            raise HTTPException(status_code=403, detail="El lead no est\u00e1 asignado a este ejecutivo")

    # Enmascarar datos sensibles
    if user.get("rol") not in ("admin", "supervisor"):
        phone = (data.get("public") or {}).get("phone", "")
        if phone:
            data["public"]["phone_masked"] = _mask_phone(phone)
            data["public"].pop("phone", None)
    return data


def _mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) >= 8:
        return f"+{digits[:2]}****{digits[-4:]}"
    return "****"

# ========================= FIN ANALYTICS =========================

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

@app.get("/api/leads/resolve-property-code")
async def api_resolve_property_code(request: Request, code: str = Query(...)):
    await get_current_user(request)
    logger.info("[API_RESOLVE] incoming code=%r user=%s", code, getattr(request.state, "user", None) and getattr(request.state.user, "get", lambda *_: None)("email"))
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: resolve_property_code(code)
    )
    logger.info("[API_RESOLVE] result=%s", result)
    return result

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


async def _get_authorized_crm_lead(
    request: Request,
    phone: str,
    *,
    administrative: bool = False,
):
    """Resolve the authenticated user and enforce CRM role/ownership access."""
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    if administrative and not can_administer_leads(user.get("rol")):
        raise HTTPException(status_code=403, detail="Acción reservada a administración")

    loop = asyncio.get_running_loop()
    lead = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: CrmService.get_lead(phone))
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    if not can_administer_leads(user.get("rol")) and not lead_is_assigned_to_user(lead, user):
        raise HTTPException(status_code=403, detail="El lead no está asignado a este ejecutivo")
    return user, lead



@app.get("/crm/lead/{phone}", response_class=HTMLResponse)
async def view_crm_detail(request: Request, phone: str, codigo: str = Query(None)):
    user = await get_current_user_doc(request)
    
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_lead_detail_data(phone, property_code=codigo))
    if not data: 
        return HTMLResponse("Lead no encontrado")
    
    user_name = user.get("nombre", "")
    
    if not can_administer_leads(user.get("rol")) and not lead_is_assigned_to_user(data, user):
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
        user, authorized_lead = await _get_authorized_crm_lead(request, phone)
        actor_user_id = str(user.get("_id") or "")
        actor_name = user.get("nombre") or user.get("username") or actor_user_id

        now_cl = datetime.now(CHILE_TZ)
        if "meta" not in payload:
            payload["meta"] = {}
        payload["meta"]["server_time_cl"] = now_cl.strftime("%Y-%m-%d %H:%M:%S")

        def _sync_log_action():
            from chatbot.storage import get_db
            db = get_db()
            lead = db["leads"].find_one({"_id": authorized_lead["_id"]})

            # A recorded send/completed call is management. Navigation-only
            # clicks remain audit evidence and never complete first-management SLA.
            result_by_event = {
                "SEND_WA_LEAD": "MESSAGE_SENT_WAITING_RESPONSE",
                "SEND_EMAIL_LEAD": "EMAIL_SENT",
                "CALL_COMPLETED_LEAD": "CALL_NO_ANSWER",
            }
            if event_type in result_by_event and lead:
                from chatbot.crm_management import record_management_result
                from chatbot.crm_metrics import active_assignment_cycle
                cycle = active_assignment_cycle(db, lead["_id"])
                if not cycle:
                    raise ValueError("El lead no tiene un ciclo de asignación activo")
                meta = payload.get("meta") or {}
                idempotency_key = str(meta.get("idempotency_key") or "").strip()
                if not idempotency_key:
                    idempotency_key = f"crm-send:{lead['_id']}:{cycle['assignment_cycle_id']}:{event_type}:{now_cl.isoformat()}"
                record_management_result(
                    db, lead_id=lead["_id"], assignment_cycle_id=cycle["assignment_cycle_id"],
                    actor_user_id=actor_user_id, result_type=result_by_event[event_type],
                    occurred_at=None, source="crm_send_action", idempotency_key=idempotency_key,
                )
                from chatbot.metrics import update_lead_metrics
                update_lead_metrics(db, phone, event_at=datetime.now(timezone.utc),
                                    event_type=event_type, lead_id=lead["_id"])
            else:
                log_crm_event(phone=phone, event_type=event_type, agent=actor_name,
                              meta_data=payload.get("meta"))

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_WEB_THREAD_POOL, _sync_log_action)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error logging CRM action: {e}")
        return {"status": "error"}


@app.post("/api/crm/management-result")
async def api_crm_management_result(request: Request):
    data = await request.json()
    phone = str(data.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Falta teléfono")
    user, lead = await _get_authorized_crm_lead(request, phone)
    actor_user_id = str(user.get("_id") or "")
    if not actor_user_id:
        raise HTTPException(status_code=400, detail="Usuario sin identidad canónica")

    def _record():
        from chatbot.crm_management import record_management_result
        from chatbot.crm_metrics import active_assignment_cycle
        from chatbot.storage import get_db
        db = get_db()
        cycle = active_assignment_cycle(db, lead["_id"])
        if not cycle:
            raise ValueError("El lead no tiene un ciclo de asignación activo")
        return record_management_result(
            db, lead_id=lead["_id"], assignment_cycle_id=cycle["assignment_cycle_id"],
            actor_user_id=actor_user_id, result_type=data.get("result_type"),
            occurred_at=None, source="crm_quick_action",
            idempotency_key=str(data.get("idempotency_key") or ""),
            next_follow_up_at=data.get("next_follow_up_at"),
        )
    try:
        result = await asyncio.get_running_loop().run_in_executor(_WEB_THREAD_POOL, _record)
        return {"status": "ok", "result_type": result.get("result_type"),
                "follow_up_required": result.get("follow_up_required")}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
@app.post("/api/crm/update")
async def api_crm_update_lead(request: Request):
    try:
        data = await request.json()
        phone = data.get("phone")
        if not phone:
            raise HTTPException(status_code=400, detail="Falta teléfono")

        user, _lead = await _get_authorized_crm_lead(request, phone)
        if (
            not can_administer_leads(user.get("rol"))
            and payload_attempts_reassignment(data)
        ):
            raise HTTPException(status_code=403, detail="El ejecutivo no puede reasignar leads")

        # Aseguramos que se guarde la hora de actualización en CL
        data["updated_at_cl"] = datetime.now(CHILE_TZ).isoformat()
        data["_actor_name"] = user.get("nombre") or user.get("username") or ""

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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"CRM Update Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/crm/admin/reassign")
async def api_crm_admin_reassign(request: Request):
    data = await request.json()
    phone = str(data.get("phone") or "").strip()
    executive = str(data.get("executive") or "").strip()
    if not phone or not executive:
        raise HTTPException(status_code=400, detail="Faltan teléfono o ejecutivo")

    user, _lead = await _get_authorized_crm_lead(request, phone, administrative=True)
    from chatbot.storage import get_async_db

    targets = await get_async_db()["usuarios"].find(
        {
            "nombre": re.compile(rf"^{re.escape(executive)}(?:\s|$)", re.IGNORECASE),
            "rol": {"$in": ["agente", "supervisor", "admin", "jefatura"]},
            "is_active": {"$ne": False},
        },
        {"nombre": 1},
    ).to_list(length=2)
    if len(targets) != 1:
        raise HTTPException(status_code=400, detail="Ejecutivo no válido o inactivo")
    target = targets[0]

    actor = user.get("nombre") or user.get("username") or "Administración"
    loop = asyncio.get_running_loop()
    changed = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: CrmService.assign_executive(
            phone,
            target["nombre"],
            method="crm_list_admin",
            actor=actor,
        ),
    )
    if not changed:
        raise HTTPException(status_code=409, detail="No fue posible reasignar el lead")
    return {"status": "ok", "executive": target["nombre"]}


@app.post("/api/crm/admin/mark-duplicate")
async def api_crm_admin_mark_duplicate(request: Request):
    data = await request.json()
    phone = str(data.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Falta teléfono")

    user, _lead = await _get_authorized_crm_lead(request, phone, administrative=True)
    actor = user.get("nombre") or user.get("username") or "Administración"
    loop = asyncio.get_running_loop()
    changed = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: CrmService.mark_duplicate(phone, actor=actor),
    )
    if not changed:
        raise HTTPException(status_code=409, detail="No fue posible marcar el lead como duplicado")
    return {"status": "ok"}


@app.post("/api/crm/admin/archive")
async def api_crm_admin_archive(request: Request):
    data = await request.json()
    phone = str(data.get("phone") or "").strip()
    reason = str(data.get("reason") or "Archivo administrativo").strip()[:300]
    if not phone:
        raise HTTPException(status_code=400, detail="Falta teléfono")

    user, _lead = await _get_authorized_crm_lead(request, phone, administrative=True)
    actor = user.get("nombre") or user.get("username") or "Administración"
    loop = asyncio.get_running_loop()
    changed = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: CrmService.archive_lead(phone, actor=actor, reason=reason),
    )
    if not changed:
        raise HTTPException(status_code=409, detail="No fue posible archivar el lead")
    return {"status": "ok"}

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

    if data.get("event") == "messages.update":
        updated = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL, lambda: record_delivery_status_webhook(data)
        )
        return JSONResponse({"status": "delivery_updated" if updated else "delivery_not_tracked"}, status_code=200)

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
        "deploy_commit": os.getenv("RENDER_GIT_COMMIT", "unknown"),
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
    comuna: list[str] = Query(None),
    estado: str = Query(None),
    ejecutivo: str = Query(None),
    operacion: str = Query(None),
    telefono: str = Query(None),
    classification: str = Query(None),
    sort_by: str = Query(None),
    sort_dir: str = Query("desc"),
    page: int = Query(1, ge=1)
):
    from chatbot.storage import get_async_db
    adb = get_async_db()
    user = await get_current_user_doc(request)
    
    if not user:
        return RedirectResponse(url="/?error=sesion_invalida")

    user_role = user.get("rol", "agente")
    user_name = user.get("nombre", "")
    user_id = str(user["_id"])
    user_email = user.get("email", "")
    
    limit = 10
    loop = asyncio.get_running_loop()
    list_task = loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_captacion_list(
            user_role=user_role,
            user_name=user_name,
            user_id=user_id,
            user_email=user_email,
            page=page,
            limit=limit,
            comuna_filter=comuna,
            status_filter=estado,
            executive_filter=ejecutivo,
            operacion_filter=operacion,
            telefono_filter=telefono,
            classification_filter=classification,
            sort_by=sort_by,
            sort_dir=sort_dir
        )
    )
    def _fetch_executives():
        from chatbot.storage import get_db
        db = get_db()
        result = list(db["usuarios"].find(
            {"is_active": True, "rol": "agente", "comunas_interes_norm": {"$exists": True, "$ne": []}},
            {"nombre": 1}
        ).sort("nombre", 1))
        names = [u.get("nombre", "") for u in result if u.get("nombre")]
        names.insert(0, "Sin asignar")
        return names

    if user_role in CAPTACION_PRIVILEGED_ROLES:
        loop2 = asyncio.get_running_loop()
        exec_task = loop2.run_in_executor(None, _fetch_executives)
        items_total, executives = await asyncio.gather(list_task, exec_task)
    else:
        items_total = await list_task
        executives = []
    items, total_count, available_ops = items_total
    
    # Alinear con nombre de variables del template original
    current_comunas = [c for c in (comuna or []) if c]
    current_comuna = current_comunas[0] if len(current_comunas) == 1 else ""
    current_estado = estado
    current_ejecutivo = ejecutivo if ejecutivo and ejecutivo != "Todos" else ""
    current_operacion = (operacion or "").lower()
    current_telefono = telefono or ""
    current_classification = classification or ""
    sort_keys = [s.strip() for s in str(sort_by or "").split(",") if s.strip()]
    sort_dirs = [s.strip().lower() for s in str(sort_dir or "").split(",") if s.strip()]
    current_sorts = {
        key: {
            "direction": sort_dirs[index] if index < len(sort_dirs) and sort_dirs[index] in ("asc", "desc") else "desc",
            "priority": index + 1,
        }
        for index, key in enumerate(sort_keys)
    }

    from urllib.parse import urlencode
    pagination_query = {
        key: value for key, value in {
            "telefono": current_telefono,
            "comuna": current_comunas,
            "operacion": current_operacion,
            "estado": current_estado,
            "ejecutivo": current_ejecutivo,
            "classification": current_classification,
            "sort_by": sort_by or "",
            "sort_dir": ",".join(
                sort_dirs[index] if index < len(sort_dirs) and sort_dirs[index] in ("asc", "desc") else "desc"
                for index in range(len(sort_keys))
            ),
        }.items() if value
    }
    pagination_base_url = "?" + urlencode(pagination_query, doseq=True) + ("&" if pagination_query else "")
    
    # Diagnóstico temporal
    from chatbot.storage import get_db as get_sync_db
    sync_db = get_sync_db()
    base_eligible = Config.get_captacion_collection(sync_db).count_documents({
        "origen": {"$in": ["toctoc", "yapo"]},
        "classification.state": {"$in": list(VISIBLE_CLASSIFICATION_STATES)}
    })
    logger.info(
        f"[CAPTACION] collection={Config.CAPTACION_COLLECTION_NAME} "
        f"user_id={user_id[:8]}... role={user_role} "
        f"base_eligible={base_eligible} rbac_count={total_count} "
        f"items_returned={len(items)} page={page}"
    )
    
    # KPIs de todos los portales soportados.
    base_query = {
        "origen": {"$in": ["toctoc", "yapo"]},
        "classification.state": {"$in": list(VISIBLE_CLASSIFICATION_STATES)}
    }
    if user_role not in CAPTACION_PRIVILEGED_ROLES:
        base_query["$or"] = [
            {"gestion.ejecutivo_asignado": user_name},
            {"$and": [
                {"gestion.ejecutivo_asignado": {"$in": [None, ""]}},
                {"$or": [
                    {"gestion.ejecutivo_id": user_id},
                    {"gestion.ejecutivo_email": user_email},
                ]},
            ]},
        ]
    elif ejecutivo and ejecutivo != "Todos":
        selected_exec_doc = await adb["usuarios"].find_one(
            {"nombre": ejecutivo}, {"email": 1}
        )
        selected_exec_ids = [ejecutivo]
        selected_exec_emails = [ejecutivo]
        if selected_exec_doc:
            selected_exec_ids.append(str(selected_exec_doc["_id"]))
            if selected_exec_doc.get("email"):
                selected_exec_emails.append(selected_exec_doc["email"])
        base_query["$or"] = [
            {"gestion.ejecutivo_asignado": ejecutivo},
            {"$and": [
                {"gestion.ejecutivo_asignado": {"$in": [None, ""]}},
                {"$or": [
                    {"gestion.ejecutivo_id": {"$in": selected_exec_ids}},
                    {"gestion.ejecutivo_email": {"$in": selected_exec_emails}},
                ]},
            ]},
        ]

    import time
    cache_key = f"stats_v7_{user_role}_{user_id}_{ejecutivo}"
    cache_store = getattr(app.state, 'captacion_stats_cache', {})
    now = time.time()
    if cache_key in cache_store and now - cache_store[cache_key]['time'] < 300:
        in_gestion_count = cache_store[cache_key]['in_gestion_count']
        captados_count = cache_store[cache_key]['captados_count']
        descartados_count = cache_store[cache_key]['descartados_count']
        available_count = cache_store[cache_key]['available_count']
        comunas_clean = cache_store[cache_key]['comunas_clean']
    else:
        kpi_queries = build_kpi_queries(base_query)
        available_count, in_gestion_count, captados_count, descartados_count, comunas_list = await asyncio.gather(
            adb[Config.CAPTACION_COLLECTION_NAME].count_documents(kpi_queries["available"]),
            adb[Config.CAPTACION_COLLECTION_NAME].count_documents(kpi_queries["management"]),
            adb[Config.CAPTACION_COLLECTION_NAME].count_documents(kpi_queries["captured"]),
            adb[Config.CAPTACION_COLLECTION_NAME].count_documents(kpi_queries["discarded"]),
            adb[Config.CAPTACION_COLLECTION_NAME].distinct("comuna", base_query)
        )
        comunas_clean = sorted(
            {str(c).strip() for c in comunas_list if c and str(c).strip()},
            key=lambda value: value.casefold(),
        )
        cache_store[cache_key] = {
            'time': now,
            'in_gestion_count': in_gestion_count,
            'captados_count': captados_count,
            'descartados_count': descartados_count,
            'available_count': available_count,
            'comunas_clean': comunas_clean
        }
        app.state.captacion_stats_cache = cache_store
        
    total_pages = (total_count + limit - 1) // limit
    worked_count = in_gestion_count + captados_count + descartados_count

    goal_executive = current_ejecutivo if user_role in CAPTACION_PRIVILEGED_ROLES else user_name
    captacion_goal = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_captacion_goal_dashboard(
            sync_db,
            selected_executive=goal_executive or None,
        ),
    )

    return templates.TemplateResponse("captacion_list.html", {
        "request": request,
        "items": items,
        "total_count": total_count,
        "available_count": available_count,
        "in_gestion_count": in_gestion_count,
        "worked_count": worked_count,
        "captados_count": captados_count,
        "descartados_count": descartados_count,
        "captacion_goal": captacion_goal,
        "comunas": comunas_clean,
        "available_ops": available_ops,
        "user_role": user_role,
        "user_name": user_name,
        "current_comuna": current_comuna,
        "current_comunas": current_comunas,
        "current_estado": current_estado,
        "current_ejecutivo": current_ejecutivo,
        "current_operacion": current_operacion,
        "current_telefono": current_telefono,
        "current_classification": current_classification,
        "current_sort_by": sort_by or "",
        "current_sort_dir": ",".join(
            current_sorts[key]["direction"] for key in sort_keys if key in current_sorts
        ),
        "current_sorts": current_sorts,
        "pagination_base_url": pagination_base_url,
        "executives": executives,
        "pagination": {
            "current_page": page,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1
        }
    }, headers={"Content-Type": "text/html; charset=utf-8"})

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

    # El acceso al detalle y a sus APIs usa una sola regla de permisos.
    if not can_manage_captacion(user, data):
        return RedirectResponse(url="/captacion?error=no_asignada")

    # Ya no calculamos el matching aquí (se hace vía AJAX)
    
    return templates.TemplateResponse("captacion_detail.html", {
        "request": request,
        "prop": data,
        "user_name": user_name,
        "user_role": user.get("rol", "agente")
    }, headers={"Content-Type": "text/html; charset=utf-8"})

# --- PROTECCIÓN ANTI-SPAM PARA MATCHING ---
PENDING_MATCHING_REQUESTS = {} # obj_id -> timestamp

@app.get("/api/captacion/{obj_id}/matching")
async def api_get_matching_leads(request: Request, obj_id: str):
    user_doc = await get_current_user_doc(request)
    if not user_doc:
        raise HTTPException(status_code=401, detail="Sesión inválida")

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
        if not can_manage_captacion(user_doc, data):
            raise HTTPException(status_code=403, detail="No autorizado para gestionar esta captación")

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
        username_str = await get_current_user(request)
        user_doc = await get_current_user_doc(request)
        data = await request.json()
        obj_id = data.get("id")
        status = data.get("status")
        notes = data.get("notes")
        next_followup = data.get("next_followup")
        channel = data.get("channel")
        outcome = data.get("outcome")
        user_name = user_doc.get("nombre", username_str) if user_doc else username_str
        
        if not obj_id or not status:
            raise HTTPException(status_code=400, detail="Faltan datos")

        captacion_doc = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL, lambda: get_captacion_detail(obj_id)
        )
        if not captacion_doc:
            raise HTTPException(status_code=404, detail="Propiedad no encontrada")
        if not user_doc or not can_manage_captacion(user_doc, captacion_doc):
            raise HTTPException(status_code=403, detail="No autorizado para gestionar esta captación")
            
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
                next_followup=next_followup,
                user_doc=user_doc,
            )
        )
        if result:
            # La respuesta ya cambió: las tarjetas no deben conservar hasta cinco
            # minutos el conteo anterior.
            app.state.captacion_stats_cache = {}
            return {"status": "ok"}
        return {"status": "error", "message": "Operación retornó falso"}
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
        user_doc = await get_current_user_doc(request)
        data = await request.json()
        obj_id = data.get("id")
        if not obj_id:
            raise HTTPException(status_code=400, detail="Falta ID")

        captacion_doc = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL, lambda: get_captacion_detail(obj_id)
        )
        if not captacion_doc:
            raise HTTPException(status_code=404, detail="Propiedad no encontrada")
        if not user_doc or not can_manage_captacion(user_doc, captacion_doc):
            raise HTTPException(status_code=403, detail="No autorizado para gestionar esta captación")
        
        # Check user name in session or payload
        user_name = data.get("user_name")
        if not user_name:
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
            lambda: log_captacion_activity(
                obj_id,
                actual_name,
                action,
                channel,
                message,
                phone,
                result,
                template_used,
                user_doc=user_doc,
            )
        )
        return {
            "status": "ok",
            "credited": False,
            "attempt_id": success.get("attempt_id"),
            "attempt_status": success.get("status"),
        } if success else {"status": "error"}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error logging captacion action: {e}")
        raise HTTPException(status_code=500, detail="No fue posible registrar la gestión")


@app.post("/api/captacion/confirm_action")
async def api_captacion_confirm_action(request: Request):
    user_doc = await get_current_user_doc(request)
    if not user_doc:
        raise HTTPException(status_code=401, detail="Sesión inválida")
    payload = await request.json()
    if not payload.get("attempt_id") or not payload.get("result"):
        raise HTTPException(status_code=400, detail="Faltan intento o resultado")
    try:
        from api_captacion import confirm_captacion_activity
        result = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL,
            lambda: confirm_captacion_activity(
                payload["attempt_id"], user_doc, payload["result"], payload.get("notes")
            ),
        )
        app.state.captacion_stats_cache = {}
        return {"status": "ok", **result}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def _require_captacion_workforce_admin(request: Request):
    user_doc = await get_current_user_doc(request)
    if not user_doc:
        raise HTTPException(status_code=401, detail="Sesión inválida")
    if str(user_doc.get("rol") or "").lower() not in CAPTACION_PRIVILEGED_ROLES:
        raise HTTPException(status_code=403, detail="Permiso administrativo requerido")
    return user_doc


async def _require_captacion_report_admin(request: Request):
    user_doc = await get_current_user_doc(request)
    if not user_doc:
        raise HTTPException(status_code=401, detail="Sesión inválida")
    if str(user_doc.get("rol") or "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Permiso de administrador requerido")
    return user_doc


@app.get("/captacion/reporte-semanal", response_class=HTMLResponse)
async def view_captacion_weekly_report(request: Request, report_id: str = Query(None)):
    await _require_captacion_report_admin(request)
    from chatbot.storage import get_db

    def load_report():
        query = {"report_id": report_id} if report_id else {"is_test": False}
        return get_db()[CAPTACION_WEEKLY_REPORT_COLLECTION].find_one(
            query, {"_id": 0, "deepseek_payload": 0}, sort=[("created_at", -1)]
        )

    report = await asyncio.get_running_loop().run_in_executor(_WEB_THREAD_POOL, load_report)
    return templates.TemplateResponse("captacion_weekly_report_preview.html", {
        "request": request,
        "report": report,
        "group_recipient": (report or {}).get("group_recipient") or Config.DAILY_REPORT_GROUP_ID,
    })


@app.post("/api/captacion/weekly-report/test/generate")
async def api_generate_captacion_weekly_test(request: Request):
    user_doc = await _require_captacion_report_admin(request)
    payload = await request.json()
    try:
        report = await create_weekly_report(
            payload.get("period_start"), payload.get("period_end"), is_test=True,
            created_by=user_doc.get("_id") or user_doc.get("username"),
        )
        return {"status": "ok", "report_id": report["report_id"], "snapshot_id": report["snapshot_id"]}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/weekly-report/{report_id}/test-send")
async def api_send_captacion_weekly_test(request: Request, report_id: str):
    await _require_captacion_report_admin(request)
    payload = await request.json()
    try:
        delivery = await send_test_report(report_id, payload.get("recipient"))
        return {"status": "ok", "delivery": delivery}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/weekly-report/{report_id}/approve-send")
async def api_approve_captacion_weekly_report(request: Request, report_id: str):
    user_doc = await _require_captacion_report_admin(request)
    payload = await request.json()
    try:
        delivery = await approve_and_send_report(
            report_id, user_doc, edited_narrative=payload.get("narrative")
        )
        return {"status": "ok", "delivery": delivery}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/weekly-report/{report_id}/acknowledge-outcomes")
async def api_acknowledge_captacion_weekly_outcomes(request: Request, report_id: str):
    user_doc = await _require_captacion_report_admin(request)
    try:
        report = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL, lambda: acknowledge_outcome_review(report_id, user_doc)
        )
        return {"status": "ok", "report_id": report["report_id"]}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/weekly-report/{report_id}/regenerate")
async def api_regenerate_captacion_weekly_report(request: Request, report_id: str):
    user_doc = await _require_captacion_report_admin(request)
    try:
        report = await regenerate_report_narrative(report_id, user_doc)
        return {"status": "ok", "report_id": report["report_id"]}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/weekly-report/{report_id}/cancel")
async def api_cancel_captacion_weekly_report(request: Request, report_id: str):
    user_doc = await _require_captacion_report_admin(request)
    try:
        report = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL, lambda: cancel_report(report_id, user_doc)
        )
        return {"status": "ok", "report_id": report["report_id"], "report_status": report["status"]}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/workforce/membership")
async def api_captacion_workforce_membership(request: Request):
    user_doc = await _require_captacion_workforce_admin(request)
    payload = await request.json()
    from chatbot.storage import get_db
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL,
            lambda: upsert_membership(get_db(), payload, user_doc.get("_id")),
        )
        return {"status": "ok", "membership": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/workforce/exception")
async def api_captacion_workforce_exception(request: Request):
    user_doc = await _require_captacion_workforce_admin(request)
    payload = await request.json()
    from chatbot.storage import get_db
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL,
            lambda: create_work_exception(get_db(), payload, user_doc.get("_id")),
        )
        return {"status": "ok", "exception": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/workforce/calendar")
async def api_captacion_workforce_calendar(request: Request):
    user_doc = await _require_captacion_workforce_admin(request)
    payload = await request.json()
    from chatbot.storage import get_db
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL,
            lambda: upsert_calendar_day(get_db(), payload, user_doc.get("_id")),
        )
        return {"status": "ok", "calendar_day": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/management/{event_id}/reverse")
async def api_reverse_captacion_management(request: Request, event_id: str):
    user_doc = await _require_captacion_workforce_admin(request)
    payload = await request.json()
    from captacion_management import reverse_management_event
    from chatbot.storage import get_db
    try:
        reversal = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL,
            lambda: reverse_management_event(
                get_db(), event_id=event_id, actor_user=user_doc, reason=payload.get("reason")
            ),
        )
        return {"status": "ok", "reversal_event_id": reversal["event_id"]}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/captacion/management/anomalies")
async def api_captacion_management_anomalies(request: Request, status_filter: str = "pending_review"):
    await _require_captacion_workforce_admin(request)
    from captacion_management import ANOMALY_COLLECTION
    from chatbot.storage import get_db
    rows = await asyncio.get_running_loop().run_in_executor(
        _WEB_THREAD_POOL,
        lambda: list(get_db()[ANOMALY_COLLECTION].find(
            {"status": status_filter}, {"_id": 0}
        ).sort("created_at", -1).limit(100)),
    )
    return {"status": "ok", "items": rows}

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
    reassigned = await loop.run_in_executor(
        _WORKER_THREAD_POOL, lambda: redistribute_inactive_agent_captaciones(dry_run=False)
    )
    count = await loop.run_in_executor(_WORKER_THREAD_POOL, distribute_sourced_leads)
    app.state.captacion_stats_cache = {}
    return {"status": "ok", "assigned": count, "reassigned_inactive": reassigned}

async def captacion_distribution_loop():
    logger.info("[BACKGROUND] Iniciando loop de distribución de captaciones...")
    while True:
        try:
            background_tasks_status["captacion_distributor"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["captacion_distributor"]["status"] = "running"
            
            loop = asyncio.get_running_loop()
            
            # 1. Liberar captaciones sin gestión por SLA
            inactive_result = await loop.run_in_executor(
                _WORKER_THREAD_POOL, lambda: redistribute_inactive_agent_captaciones(dry_run=False)
            )
            if inactive_result.get("modified", 0) > 0:
                logger.info(f"[BACKGROUND] Reasignadas por ejecutivo inactivo: {inactive_result}")

            released = await loop.run_in_executor(_WORKER_THREAD_POOL, release_stale_captaciones)
            if released > 0:
                logger.info(f"[BACKGROUND] {released} captaciones liberadas por SLA antes de redistribuir.")
                
            # 2. Distribuir
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

async def _render_crm_list(
    request: Request, 
    estado: str = None, 
    busqueda: str = None, 
    orden: str = "fecha", 
    ejecutivo: str = None,
    temperatura: str = "Todos",
    page: int = 1,
    partial: bool = False,
):
    username = await get_current_user(request)
    from chatbot.storage import get_async_db
    from chatbot.crm_updates import get_crm_leads_version_async
    from chatbot.crm_filters import build_crm_card_urls, build_crm_filter_urls

    # Una sola selección normalizada gobierna consulta, KPI, tarjetas y enlaces.
    temperatura = normalize_crm_temperature(temperatura)

    adb = get_async_db()
    user = await adb["usuarios"].find_one({"username": username})
    
    if not user:
        return RedirectResponse(url="/?error=sesion_invalida")

    user_role = user.get("rol", "agente")
    user_name = user.get("nombre", "")
    can_administer = can_administer_leads(user_role)
    # Se lee antes del listado. Si ocurre un cambio durante el render, el
    # siguiente polling verá una versión mayor y repetirá la actualización.
    crm_version = await get_crm_leads_version_async(adb)

    limit = 15
    leads_task = get_crm_leads_list(
        filtro_estado=estado,
        busqueda=busqueda,
        ordenar_por=orden,
        user_role=user_role,
        user_name=user_name,
        ejecutivo_filter=ejecutivo,
        temperatura_filter=temperatura,
        page=page,
        limit=limit,
    )
    exec_task = get_unique_executives() if can_administer else asyncio.sleep(0, result=[])
    leads_payload, executives = await asyncio.gather(leads_task, exec_task)
    leads, kpis, total_count = leads_payload

    total_pages = max(1, (total_count + limit - 1) // limit)
    page = min(max(page, 1), total_pages)
    pagination_query = {
        "temperatura": temperatura if temperatura != "Todos" else None,
        "estado": estado,
        "busqueda": busqueda,
        "orden": orden,
        "ejecutivo": ejecutivo,
    }
    pagination_query = {
        key: value for key, value in pagination_query.items()
        if value not in (None, "")
    }
    pagination_base_url = "/crm?" + urlencode(pagination_query) + ("&" if pagination_query else "")

    card_query_params = dict(request.query_params)
    if temperatura == "Todos":
        card_query_params.pop("temperatura", None)
    else:
        card_query_params["temperatura"] = temperatura

    response = templates.TemplateResponse("crm_leads_list.html", {
        "request": request, 
        "leads": leads, 
        "kpis": kpis,
        "user_role": user_role,
        "user_name": user_name,
        "can_administer_leads": can_administer,
        "executives": executives,
        "current_ejecutivo": (ejecutivo or "Todos") if can_administer else user_name,
        "current_temperatura": temperatura,
        "crm_version": crm_version,
        "partial": partial,
        "card_urls": build_crm_card_urls(card_query_params),
        "filter_urls": build_crm_filter_urls(card_query_params),
        "pagination_base_url": pagination_base_url,
        "pagination": {
            "total_count": total_count,
            "current_page": page,
            "total_pages": total_pages,
            "has_more": page < total_pages,
            "has_prev": page > 1,
            "limit": limit
        }
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/crm", response_class=HTMLResponse)
async def view_crm_list(
    request: Request,
    estado: str = None,
    busqueda: str = None,
    orden: str = "fecha",
    ejecutivo: str = None,
    temperatura: str = "Todos",
    page: int = Query(1, ge=1),
):
    return await _render_crm_list(
        request,
        estado=estado,
        busqueda=busqueda,
        orden=orden,
        ejecutivo=ejecutivo,
        temperatura=temperatura,
        page=page,
    )


@app.get("/crm/check-updates")
async def check_crm_updates(request: Request, since: int = Query(0, ge=0)):
    """Consulta un solo documento de versión; no examina la colección leads."""
    await get_current_user(request)
    from chatbot.storage import get_async_db
    from chatbot.crm_updates import get_crm_leads_version_async

    version = await get_crm_leads_version_async(get_async_db())
    return JSONResponse(
        {"changed": version != since, "version": version},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/crm/partial", response_class=HTMLResponse)
async def view_crm_list_partial(
    request: Request,
    estado: str = None,
    busqueda: str = None,
    orden: str = "fecha",
    ejecutivo: str = None,
    temperatura: str = "Todos",
    page: int = Query(1, ge=1),
):
    return await _render_crm_list(
        request,
        estado=estado,
        busqueda=busqueda,
        orden=orden,
        ejecutivo=ejecutivo,
        temperatura=temperatura,
        page=page,
        partial=True,
    )

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
    requested_url = request.url.path
    if request.url.query:
        requested_url += f"?{request.url.query}"
    response = RedirectResponse(url="/?" + urlencode({
        "error": "sesion_expirada",
        "next": requested_url,
    }))
    response.set_cookie("login_next", requested_url, httponly=True, secure=True, samesite="lax", max_age=600)
    return response

# ========================= 9. BACKGROUND LOOPS (REFACTORED) =========================

async def process_pending_leads_loop():
    logger.info("[BACKGROUND] Iniciando loop de leads pendientes...")
    while True:
        try:
            background_tasks_status["notifications_loop"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["notifications_loop"]["status"] = "running"
            
            if should_send_now():
                # Canonical Hot worker. Fail-closed flag; it never reads the legacy queue.
                if Config.LEAD_HOT_NOTIFICATIONS_ENABLED:
                    from chatbot.crm_hot_delivery import process_one_hot
                    from chatbot.lead_router import format_whatsapp_template
                    from chatbot.whatsapp_client import send_whatsapp_message_detailed

                    async def _canonical_hot_sender(recipient, payload):
                        message = format_whatsapp_template(
                            payload, payload.get("target_name"), payload.get("property_code"), True
                        )
                        return await send_whatsapp_message_detailed(recipient, message)

                    await process_one_hot(
                        get_db(), sender=_canonical_hot_sender,
                        worker_id=f"render:{os.getpid()}", enabled=True,
                    )
                pending = await run_db("pending_notifications.find", get_pending_notifications)
                if pending:
                    logger.info(f"[BACKGROUND] Analizando {len(pending)} envíos pendientes...")
                    
                    # 1. Agrupar por ejecutivo (target_phone)
                    by_executive = {}
                    for p in pending:
                        lead_data = p.get("lead_data", {})
                        
                        # Fix Bug B: Ignore captacion notifications
                        lead_type = lead_data.get("lead_type") or p.get("lead_type", "")
                        notification_type = lead_data.get("notification_type") or p.get("notification_type", "")
                        if notification_type == "captacion" or lead_type == "AsignacionCaptacion":
                            logger.debug(f"[BACKGROUND] Skipping captacion notification para {p.get('target_phone')}")
                            await run_db("pending_notifications.mark_sent", mark_notification_sent, p["_id"])
                            continue

                        target_phone = lead_data.get("target_phone") or p.get("target_phone")
                        
                        # Fix: Si no hay teléfono o es el dummy, intentamos re-enrutar antes de descartar
                        if not target_phone or target_phone == "+56900000000":
                            from chatbot.lead_router import find_responsible_executive
                            lead_phone = lead_data.get("phone")
                            p_code = lead_data.get("property_code") or lead_data.get("prospecto", {}).get("codigo")
                            if p_code:
                                logger.info(f"[BACKGROUND] Re-enrutando lead {lead_phone} por falta de destino válido...")
                                new_exec, new_phone, assignment_type = await run_in_threadpool(
                                    find_responsible_executive,
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
                    from chatbot.notification_identity import deduplicate_lead_notifications

                    for target_phone, data in by_executive.items():
                        raw_items = data["items"]
                        items = deduplicate_lead_notifications(raw_items)
                        target_name = data["name"]

                        if len(items) != len(raw_items):
                            logger.warning(
                                "[BACKGROUND] Deduplicadas %s notificaciones repetidas para %s",
                                len(raw_items) - len(items),
                                target_name,
                            )
                        
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
                                    for notification_id in item.get("_notification_ids") or [item["_id"]]:
                                        await run_db("pending_notifications.mark_sent", mark_notification_sent, notification_id)

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
                                for notification_id in p.get("_notification_ids") or [p["_id"]]:
                                    await run_db("pending_notifications.mark_sent", mark_notification_sent, notification_id)

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
                            obj_id = str(task.get("obj_id"))
                            lead = await run_db(
                                "propiedades_captacion.find_one",
                                lambda: Config.get_captacion_collection(db).find_one({"_id": ObjectId(obj_id)})
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
                            crm_link = f"{Config.CRM_BASE_URL}/captacion/{obj_id}"
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
                            
                        exec_phone = await run_in_threadpool(get_executive_phone, ejecutivo)
                        if not exec_phone or exec_phone == "+56900000000":
                            continue

                        if is_captacion:
                            # Si hay tareas duplicadas históricas para la misma captación,
                            # dejamos solo una activa para evitar triple envío.
                            await run_db(
                                "crm_tasks.compact_captacion_duplicates",
                                lambda: db["crm_tasks"].update_many(
                                    {
                                        "lead_type": "captacion",
                                        "obj_id": str(task.get("obj_id")),
                                        "status": "pending",
                                        "_id": {"$ne": task["_id"]}
                                    },
                                    {"$set": {"status": "completed", "resolved_at": now.isoformat(), "resolution": "superseded_duplicate"}}
                                )
                            )
                            
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
                            if is_captacion:
                                # Clear the visible reminder only when no newer
                                # pending reminder exists for this captación.
                                has_newer_task = await run_db(
                                    "crm_tasks.has_newer_captacion_reminder",
                                    lambda: db["crm_tasks"].count_documents({
                                        "lead_type": "captacion",
                                        "obj_id": str(task.get("obj_id")),
                                        "status": "pending",
                                        "_id": {"$ne": task["_id"]},
                                    }) > 0,
                                )
                                if not has_newer_task:
                                    await run_db(
                                        "propiedades_captacion.clear_sent_followup",
                                        lambda: Config.get_captacion_collection(db).update_one(
                                            {"_id": lead["_id"]},
                                            {"$set": {
                                                "gestion.next_followup": None,
                                                "gestion.next_followup_sent_at": now,
                                            }},
                                        ),
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
            Config.get_captacion_collection(db).create_index([
                ("gestion.estado", 1), 
                ("gestion.ejecutivo_asignado", 1), 
                ("score_captacion", -1)
            ], name="idx_yapo_gestion_ejecutivo_score")
        except Exception as idx_e:
            if "IndexOptionsConflict" in str(idx_e):
                logger.warning("IndexOptionsConflict detectado. Eliminando índice antiguo...")
                try:
                    Config.get_captacion_collection(db).drop_index("idx_yapo_gestion_ejecutivo_score")
                    Config.get_captacion_collection(db).drop_index("gestion.estado_1_gestion.ejecutivo_asignado_1_score_captacion_-1")
                except:
                    pass
                Config.get_captacion_collection(db).create_index([
                    ("gestion.estado", 1), 
                    ("gestion.ejecutivo_asignado", 1), 
                    ("score_captacion", -1)
                ], name="idx_yapo_gestion_ejecutivo_score")
            else:
                logger.warning(f"Error creando índice propiedades_captacion: {idx_e}")
                
        # Índice para Búsqueda por Comuna Normalizada + Score
        try:
            Config.get_captacion_collection(db).create_index([
                ("details.comuna_norm", 1), 
                ("score_captacion", -1)
            ])
        except Exception as e:
            logger.warning(f"Error creando índice comuna_norm: {e}")
        # Índice compuesto cubriente para get_market_insights():
        # cubre $match(comuna, tipo, precio_uf>0, m2_total>0) → index-only scan
        try:
            db["universo_cartera_prop360"].create_index([
                ("comuna", 1), ("tipo", 1), ("precio_uf", 1), ("m2_total", 1)
            ], name="idx_uc_market_insights")
        except Exception:
            db["universo_cartera_prop360"].create_index([("comuna", 1), ("tipo", 1)])
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
        if not Config.CRM_INACTIVE_NUDGE_ENABLED:
            background_tasks_status["nudge_loop"] = {"status": "disabled", "last_heartbeat": datetime.now(CHILE_TZ).isoformat()}
            await asyncio.sleep(300)
            continue
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
                    logger.debug(f"[CACHE_WARMER] Skip: cache vigente ({ttl_left:.0f}s restantes)")
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
                logger.debug("[CACHE_WARMER] Skip: lock activo en otra instancia")
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
            logger.debug(f"[CACHE_WARMER] LEADS_INTELLIGENCE: cache pre-warmed en {elapsed_ms:.0f}ms")
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
                observability_mark("event_loop_blocked")
                logger.error(f"[EVENT_LOOP_BLOCKED] lag={lag_ms:.0f}ms possible_blocking_operation=true")
                logger.critical(
                    f"[CRITICAL] [ASYNC_VIOLATION] type=event_loop_blocked lag_ms={lag_ms:.0f} "
                    f"op=none collection=none caller=event_loop_monitor_loop trace=none "
                    f"impact=HIGH action_required=true async_context=true thread_type=main safe=false"
                )
                recent = observability_event_loop_blocked_recent(10)
                if recent > 3:
                    logger.critical(
                        f"[CRITICAL] [EVENT_LOOP_SATURATED] count={recent} window=10s action=INVESTIGATE_BLOCKING_CALLS"
                    )
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
                observability_mark("event_loop_blocked")
                logger.critical(
                    f"[CRITICAL] [ASYNC_VIOLATION] type=event_loop_blocked lag_ms={lag_ms:.0f} "
                    f"op=none collection=none caller=event_loop_monitor_loop trace=none "
                    f"impact=HIGH action_required=true async_context=true thread_type=main safe=false"
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[EVENT_LOOP_MONITOR] Error: {e}")
            await asyncio.sleep(5)
        if int(time.time()) % 60 == 0:
            try:
                snap = observability_snapshot_and_reset()
                status_level = "OK" if snap["mongo_sync_on_loop"] == 0 and snap["event_loop_blocked"] == 0 else "DEGRADED"
                if snap["mongo_sync_on_loop"] > 5 or snap["event_loop_blocked"] > 5:
                    status_level = "CRITICAL"
                # Solo loguear en INFO/WARNING si hay degradación; OK va a DEBUG para no contaminar
                summary_msg = (
                    f"[HEALTH_SUMMARY]\nmongo_sync_on_loop={snap['mongo_sync_on_loop']}\n"
                    f"event_loop_blocked={snap['event_loop_blocked']}\nstatus={status_level}"
                )
                if status_level == "CRITICAL":
                    logger.error(summary_msg)
                elif status_level == "DEGRADED":
                    logger.warning(summary_msg)
                else:
                    logger.debug(summary_msg)
            except Exception:
                logger.exception("[HEALTH_SUMMARY] error=failed_to_emit")

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
                # Log solo cuando cambia estado (INFO); heartbeat periódico va a DEBUG para no contaminar.
                if prev != snapshot:
                    logger.info(
                        f"[THREADPOOL_FORENSICS] pool={name} active={active_threads} max={max_workers} queued={queued}"
                    )
                elif (now - last_heartbeat_log) >= 60:
                    logger.debug(
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
    from chatbot.daily_report import check_and_run_daily_report, check_and_run_personalized_summary
    from chatbot.captacion_report import check_and_run_meta_diaria_report
    while True:
        try:
            background_tasks_status["daily_report"] = {
                "status": "running", 
                "last_heartbeat": datetime.now(CHILE_TZ).isoformat()
            }
            # Reporte 1: Leads críticos SLA (09:30 AM)
            await check_and_run_daily_report()
            
            # Reporte 2: Resumen Matutino Personalizado (09:00 AM)
            await check_and_run_personalized_summary()
            
            # Reporte 3: calcula y envía el semanal de Captaciones (lunes 08:30).
            await check_and_run_meta_diaria_report()
            # Monitoreo de anomalias (cada lunes a las 08:00)
            now = datetime.now(CHILE_TZ)
            if now.weekday() == 0 and now.hour == 8 and now.minute < 5:
                try:
                    from scripts.monitor_anomalies import run_anomaly_check
                    run_anomaly_check()
                    logger.info("[DAILY_REPORT] Monitor de anomalias ejecutado (lunes 08:00).")
                except Exception as em:
                    logger.warning(f"[DAILY_REPORT] Error en monitor_anomalias: {em}")
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




