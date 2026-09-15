import os
import uuid
import logging
import threading
import psutil
import time
import asyncio
from collections import defaultdict
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, Request, HTTPException, Form, Depends, Header, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
from pymongo import MongoClient
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config import Config
from starlette.concurrency import run_in_threadpool
from chatbot.constants import CHILE_TZ
from chatbot.whatsapp_client import send_whatsapp_message

from services.security_contracts import SecurityContracts
from services.pdf_generator_contracts import PDFGenerator
from services.gdrive_sync import GDriveSync, expedition_folder_name, sanitize_folder_name

logger = logging.getLogger("procasa-contracts")
router = APIRouter(prefix="/contracts", tags=["Contracts"])

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

active_signatures_lock = threading.Lock()
ACTIVE_SIGNATURES = 0
MAX_CONCURRENT_SIGNATURES = 100
DOCUMENT_LINK_VALIDITY_HOURS = 120

otp_rate_limit = defaultdict(list)
verify_rate_limit = defaultdict(list)
rate_limit_lock = threading.Lock()

def check_rate_limit(ip: str, limit_dict: dict, max_requests: int, window_seconds: int = 60):
    now = time.time()
    with rate_limit_lock:
        limit_dict[ip] = [t for t in limit_dict[ip] if now - t < window_seconds]
        if len(limit_dict[ip]) >= max_requests:
            raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intente nuevamente en unos minutos.")
        limit_dict[ip].append(now)

whatsapp_failures = 0
whatsapp_breaker_time = 0
whatsapp_breaker_lock = threading.Lock()

async def send_whatsapp_circuit_breaker(phone: str, message: str):
    global whatsapp_failures, whatsapp_breaker_time
    with whatsapp_breaker_lock:
        if time.time() < whatsapp_breaker_time:
            logger.error("[CIRCUIT_BREAKER] whatsapp_service_unavailable activado")
            raise Exception("whatsapp_service_unavailable")
            
    try:
        await send_whatsapp_message(phone, message)
        with whatsapp_breaker_lock:
            whatsapp_failures = 0
    except Exception as e:
        with whatsapp_breaker_lock:
            whatsapp_failures += 1
            if whatsapp_failures >= 5:
                whatsapp_breaker_time = time.time() + 60
                logger.error("[CIRCUIT_BREAKER] whatsapp_service_unavailable activado debido a 5 fallos consecutivos")
        raise e

@contextmanager
def signature_concurrency():
    global ACTIVE_SIGNATURES
    with active_signatures_lock:
        if ACTIVE_SIGNATURES >= MAX_CONCURRENT_SIGNATURES:
            logger.error("[SERVER_ERROR] Concurrency limit exceeded")
            raise HTTPException(status_code=503, detail="El sistema está procesando demasiadas firmas. Intente nuevamente en unos minutos.")
        ACTIVE_SIGNATURES += 1
    try:
        yield
    finally:
        with active_signatures_lock:
            ACTIVE_SIGNATURES -= 1

@router.get("/api/health")
async def health_check():
    db = get_db()
    status_dict = {"status": "ok", "db": "ok", "memory": "ok", "concurrency": ACTIVE_SIGNATURES}
    try:
        db.command("ping")
    except Exception as e:
        logger.error(f"[SERVER_ERROR] Healthcheck DB failed: {e}")
        status_dict["db"] = "failed"
        status_dict["status"] = "error"
    
    if psutil.virtual_memory().percent > 95:
        logger.warning("[SERVER_ERROR] High memory usage detected")
        status_dict["memory"] = "warning"
        
    return JSONResponse(status_dict, status_code=200 if status_dict["status"] == "ok" else 503)

gdrive_sync = GDriveSync(parent_folder_id=Config.GDRIVE_CONVENIOS_FOLDER_ID)
_CONTRACTS_DB_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="contracts_db")


async def _db_call(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_CONTRACTS_DB_EXECUTOR, lambda: fn(*args, **kwargs))

async def _run_blocking(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_CONTRACTS_DB_EXECUTOR, lambda: fn(*args, **kwargs))

def get_db():
    client = MongoClient(Config.MONGO_URI)
    return client[Config.DB_NAME]

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0]
    return request.client.host if request.client else "unknown"

# Thread-safe in-memory cache for mapping username -> user_role
# Cache entries are stored as (user_role, expiration_timestamp)
# TTL is set to 300 seconds (5 minutes) to avoid frequent queries to 'usuarios' collection.
# Note: Any changes to a user's role in MongoDB may take up to 5 minutes to propagate to this cache.
_USER_ROLE_CACHE = {}
_USER_ROLE_CACHE_LOCK = threading.Lock()

async def _get_request_user(adb, request: Request):
    # 1. Request-level cache (lifetime of a single HTTP request)
    cached_username = getattr(request.state, "contracts_user_name", None)
    cached_role = getattr(request.state, "contracts_user_role", None)
    if cached_username is not None:
        return cached_username, cached_role

    username = None
    user_role = "agente"
    token = request.cookies.get("access_token")
    if not token:
        return username, user_role
    try:
        from jose import jwt
        payload = jwt.decode(token, Config.SECRET_KEY, algorithms=["HS256"])
        username = payload.get("sub")
        if username:
            # 2. Check global in-memory cache
            now = time.time()
            with _USER_ROLE_CACHE_LOCK:
                cached_entry = _USER_ROLE_CACHE.get(username)
                if cached_entry and now < cached_entry[1]:
                    user_role = cached_entry[0]
                    # Cache in request state
                    request.state.contracts_user_name = username
                    request.state.contracts_user_role = user_role
                    return username, user_role

            # 3. Query DB
            user_doc = await adb["usuarios"].find_one({"username": username})
            if user_doc:
                user_role = user_doc.get("rol", "agente")
            
            # Save in global cache
            with _USER_ROLE_CACHE_LOCK:
                _USER_ROLE_CACHE[username] = (user_role, now + 300) # 5 minutes TTL
    except Exception as e:
        logger.error(f"Error decodificando JWT: {e}")
        
    # Cache in request state
    request.state.contracts_user_name = username
    request.state.contracts_user_role = user_role
    return username, user_role

def _normalize_contract_fields(d: dict) -> dict:
    # Normalización robusta de teléfono para convenios: siempre E.164 y Chile por defecto.
    phone_raw = str(d.get("phone", "")).strip()
    if phone_raw:
        phone_digits = "".join(ch for ch in phone_raw if ch.isdigit())
        if phone_digits.startswith("56") and len(phone_digits) >= 10:
            d["phone"] = f"+{phone_digits}"
        elif len(phone_digits) in (8, 9):
            d["phone"] = f"+56{phone_digits}"
        else:
            d["phone"] = f"+{phone_digits}" if phone_digits else ""

    if d.get("email"):
        d["email"] = d["email"].strip().lower()
    if d.get("propiedad_direccion"):
        d["propiedad_direccion"] = d["propiedad_direccion"].strip().title()
    if d.get("comuna"):
        d["comuna"] = d["comuna"].strip().title()
    if d.get("ciudad_firma"):
        d["ciudad_firma"] = d["ciudad_firma"].strip().title()
    if d.get("rol"):
        rol_raw = str(d["rol"]).strip().replace(" ", "")
        if "-" in rol_raw:
            parts = rol_raw.split("-", 1)
            manzana = "".join(ch for ch in parts[0] if ch.isdigit()).zfill(5)[:5]
            predio = "".join(ch for ch in (parts[1] if len(parts) > 1 else "") if ch.isdigit()).zfill(3)[:3]
            d["rol"] = f"{manzana}-{predio}"
    try:
        vig = int(str(d.get("vigencia", "90")).strip())
    except Exception:
        vig = 90
    d["vigencia"] = str(max(30, min(vig, 720)))
    tipo = str(d.get("tipo", "Arriendo")).strip()
    valid_tipos = {"Venta", "Venta Exclusiva", "Arriendo", "Arriendo y Administración"}
    d["tipo"] = tipo if tipo in valid_tipos else "Arriendo"
    comision_raw = str(d.get("comision", "2")).replace("%", "").replace(",", ".").strip()
    if comision_raw not in {"1", "1.0", "1.5", "2", "2.0", "50"}:
        comision_raw = "2"
    if comision_raw in {"1.0", "2.0"}:
        comision_raw = comision_raw.split(".")[0]
    d["comision"] = f"{comision_raw}%"
    moneda = str(d.get("moneda", "UF")).upper().strip()
    d["moneda"] = moneda if moneda in {"UF", "CLP"} else "UF"
    precio_valor = str(d.get("precio_valor", "")).strip()
    if precio_valor:
        precio_valor = "".join(ch for ch in precio_valor if ch.isdigit() or ch in [".", ","])
    precio_input = str(d.get("precio", "")).strip()
    d["precio"] = f"{precio_valor} {d['moneda']}" if precio_valor else precio_input
    return d

def _is_valid_whatsapp_phone(phone: str) -> bool:
    """
    Valida si el teléfono es enviable por WhatsApp.
    - Chile: +56 9XXXXXXXX (9 dígitos locales móviles)
    - Internacional: 8-15 dígitos (E.164 simplificado, no bloquea extranjeros)
    """
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if not digits:
        return False
    if digits.startswith("56"):
        local = digits[2:]
        return len(local) == 9 and local.startswith("9")
    return 8 <= len(digits) <= 15

def _get_missing_required_contract_fields(d: dict):
    required_map = {
        "cliente_nombre": "cliente_nombre",
        "cliente_rut": "cliente_rut",
        "phone": "phone",
        "email": "email",
        "tipo": "tipo",
        "propiedad_direccion": "propiedad_direccion",
        "comuna": "comuna",
        "ciudad_firma": "ciudad_firma",
        "vigencia": "vigencia",
        "rol": "rol",
        "precio_valor": "precio_valor",
        "moneda": "moneda",
        "comision": "comision",
    }
    missing = []
    for field, source_key in required_map.items():
        value = d.get(source_key, "")
        if not str(value).strip():
            missing.append(field)
    return missing

@router.post("/api/preview")
async def preview_contract(request: Request):
    """Retorna un PDF generado en caliente para previsualización."""
    try:
        data = _normalize_contract_fields(await request.json())
        pdf_bytes = await _run_blocking(PDFGenerator.generate_original_contract, data)
        return Response(content=pdf_bytes, media_type="application/pdf")
    except Exception as e:
        logger.error(f"Error generating preview: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/create")
async def create_contract(request: Request, background_tasks: BackgroundTasks):
    """Crea o actualiza un contrato (desde CRM)"""
    try:
        data = _normalize_contract_fields(await request.json())
        from chatbot.storage import get_async_db
        adb = get_async_db()
        # Extraer usuario/rol desde JWT
        created_by, user_role = await _get_request_user(adb, request)
        # El emisor del convenio siempre es el usuario autenticado.
        executive = created_by or ""

        property_code = data.get("property_code", "").strip()
        missing_fields = _get_missing_required_contract_fields(data)
        if missing_fields:
            logger.warning(
                "[contracts.create.validation_error] missing_required_fields "
                f"user={created_by} role={user_role} property_code={property_code} missing={missing_fields}"
            )
            raise HTTPException(
                status_code=400,
                detail=f"Campos obligatorios faltantes: {', '.join(missing_fields)}"
            )
        if not _is_valid_whatsapp_phone(data.get("phone", "")):
            logger.warning(
                "[contracts.create.validation_error] invalid_phone_for_whatsapp "
                f"user={created_by} role={user_role} property_code={property_code} phone={data.get('phone', '')}"
            )
            raise HTTPException(
                status_code=400,
                detail="Teléfono inválido para WhatsApp. Chile: +569XXXXXXXX o 9XXXXXXXX. Extranjeros: incluye código país."
            )
        
        # Verificar si existe contrato previo por código explícito (edición)
        existing = None
        contract_code_in_payload = data.get("contract_code", "").strip()
        if contract_code_in_payload:
            existing = await adb["contracts"].find_one({"contract_code": contract_code_in_payload})

        if existing:
            if existing.get("status") in ["otp_requested", "otp_verified", "signed"]:
                raise HTTPException(status_code=400, detail="Este contrato ya está en proceso de firma o ha sido firmado. No puede ser modificado.")
            contract_code = existing["contract_code"]
        else:
            year = datetime.now().year
            short_id = str(uuid.uuid4())[:4].upper()
            contract_code = f"PROC-{year}-{short_id}"
            
        # 1. Preparar rutas (PDF se generará asíncronamente)
        data['contract_code'] = contract_code
        perm_dir = BASE_DIR / "contracts_pdf"
        perm_dir.mkdir(parents=True, exist_ok=True)
        perm_original_path = perm_dir / f"{contract_code}_original.pdf"
        
        server_timestamp = SecurityContracts.generate_server_timestamp()
            
        # Obtener datos del ejecutivo para guardar en el documento y habilitar CC en emails
        user_doc = await adb["usuarios"].find_one({"username": created_by}) if created_by else None
        if user_doc:
            exec_nombre = user_doc.get("nombre", created_by)
            exec_email = user_doc.get("email", "")
            exec_telefono = user_doc.get("phone") or user_doc.get("telefono", "")
        else:
            exec_nombre = created_by or ""
            exec_email = ""
            exec_telefono = ""

        contract_doc = {
            "contract_code": contract_code,
            "message_domain": "document_signature",
            "message_type": "brokerage_agreement",
            "recipient_role": "client",
            "state_source": "contracts",
            "responsible_service": "document_signature_delivery",
            "idempotency_key": f"document_signature:{contract_code}:{data.get('phone', '')}",
            "origen": data.get("origen", "Captación Interna"),
            "property_code": property_code,
            "phone": data.get("phone", ""),
            "client_data": {
                "nombre": data.get("cliente_nombre", ""),
                "rut": data.get("cliente_rut", ""),
                "email": data.get("email", "")
            },
            "property_data": {
                "direccion": data.get("propiedad_direccion", ""),
                "comuna": data.get("comuna", ""),
                "ciudad_firma": data.get("ciudad_firma", "Santiago de Chile"),
                "tipo": data.get("tipo", "Arriendo"),
                "rol": data.get("rol", ""),
                "vigencia": data.get("vigencia", "30"),
                "precio": data.get("precio", ""),
                "moneda": data.get("moneda", "UF"),
                "comision": data.get("comision", "")
            },
            "executive_data": {
                "nombre": exec_nombre,
                "email": exec_email,
                "telefono": exec_telefono
            },
            "executive_display": exec_nombre,
            "status": "created",
            "security": {
                "original_hash": None, # Calculado en background
                "original_pdf_path": str(perm_original_path),
                "token": None,
                "token_expiry": None,
                "token_used": False,
                "otp": None,
                "otp_expiry": None,
                "otp_attempts": 0,
                "signed_hash": None,
                "server_hmac": None
            },
            "access_logs": [],
            "timeline": [
                {
                    "action": "contract_created",
                    "server_timestamp": server_timestamp,
                    "ip": get_client_ip(request),
                    "user_agent": request.headers.get("user-agent", "")
                }
            ],
            "version": 1,
            "created_at": datetime.now(CHILE_TZ),
            "created_at_local": datetime.now(CHILE_TZ).strftime('%Y-%m-%d %H:%M:%S'),
            "created_by": created_by,
            "executive": executive
        }

        if existing:
            contract_doc["version"] = existing.get("version", 1) + 1
            contract_doc["timeline"] = existing.get("timeline", []) + contract_doc["timeline"]
            contract_doc["created_at"] = existing.get("created_at")
            old_security = existing.get("security", {})
            old_security["original_hash"] = None
            old_security["original_pdf_path"] = str(perm_original_path)
            contract_doc["security"] = old_security
            contract_doc["status"] = existing.get("status", "created")
            # Preserve the original executive identity for the one-off revision
            # requested for this existing convenio.
            if contract_code == "PROC-2026-3400":
                contract_doc["created_by"] = existing.get("created_by", created_by)
                contract_doc["executive"] = existing.get("executive", executive)
                contract_doc["executive_display"] = existing.get("executive_display", exec_nombre)
                contract_doc["executive_data"] = existing.get("executive_data", contract_doc["executive_data"])

        # This one-off replacement must remain attributed to Hernán Castro,
        # even when prepared by an authorized supervisor/admin session.
        target_rut = str(data.get("cliente_rut", "")).replace(".", "").upper()
        if target_rut == "12835828-5" and not existing:
            target_exec = await adb["usuarios"].find_one({"username": "hcastro@procasa.cl"})
            contract_doc["created_by"] = "hcastro@procasa.cl"
            contract_doc["executive"] = "hcastro@procasa.cl"
            contract_doc["executive_display"] = (target_exec or {}).get("nombre") or "Hernán Castro"
            if target_exec:
                contract_doc["executive_data"] = {
                    "nombre": target_exec.get("nombre", "Hernán Castro"),
                    "email": target_exec.get("email", ""),
                    "telefono": target_exec.get("phone") or target_exec.get("telefono", "")
                }

        try:
            from chatbot.storage import get_db
            local_db = get_db()
            pdf_b = await run_in_threadpool(PDFGenerator.generate_original_contract, data)
            orig_hash = SecurityContracts.hash_document(pdf_b)
            contract_doc["security"]["original_hash"] = orig_hash

            # Copia local de respaldo
            t_dir = BASE_DIR / "tmp" / "contracts" / contract_code
            t_dir.mkdir(parents=True, exist_ok=True)
            with open(t_dir / "contrato_original.pdf", "wb") as f:
                f.write(pdf_b)
            with open(perm_original_path, "wb") as f:
                f.write(pdf_b)

            # Subir a carpeta de expediente en Google Drive (síncrono, sin retry loop)
            client_name = data.get("cliente_nombre", "")
            prop_code = data.get("property_code", "") or data.get("propiedad_codigo", "")
            folder_id = await run_in_threadpool(
                _get_or_create_expedition_folder,
                local_db, "contracts", "contract_code", contract_code, client_name, prop_code
            )
            if folder_id:
                contract_doc["security"]["gdrive_folder_id"] = folder_id
                file_id = await run_in_threadpool(
                    gdrive_sync.upload_file,
                    folder_id, f"{contract_code}_original.pdf", pdf_b, "application/pdf"
                )
                if file_id and file_id != "mock_file_id":
                    contract_doc["security"]["original_pdf_drive_id"] = file_id
                    logger.info(f"[CONTRACTS] PDF subido a Drive de forma síncrona code={contract_code} file_id={file_id}")

        except Exception as e_pdf:
            logger.error(f"[CONTRACTS] Error generando original síncronamente: {e_pdf}")

        # Guardar en DB (siempre, aunque Drive falle)
        if existing:
            await adb["contracts"].replace_one({"contract_code": contract_code}, contract_doc)
        else:
            await adb["contracts"].insert_one(contract_doc)
            
        base_url = str(request.base_url).rstrip('/')
        url_firma = f"{base_url}/contracts/view/{contract_code}"
        
        return {
            "status": "success",
            "contract_code": contract_code,
            "url_firma": url_firma
        }
        
    except HTTPException as e:
        logger.warning(f"Error controlado en /api/create: status={e.status_code} detail={e.detail}")
        raise
    except Exception as e:
        logger.error(f"Error en /api/create: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/download/{contract_code}")
async def download_original_pdf(contract_code: str):
    """Permite descargar o ver el PDF original"""
    db = get_db()
    contract = db["contracts"].find_one({"contract_code": contract_code})
    if not contract:
        raise HTTPException(status_code=404, detail="Contrato no encontrado")

    pdf_bytes = None
    security = contract.get("security", {})

    # ── Prioridad 1: Google Drive (fuente primaria permanente) ──────────────
    drive_id = security.get("original_pdf_drive_id")
    if drive_id:
        try:
            gdrive = GDriveSync()
            pdf_bytes = await run_in_threadpool(gdrive.download_file, drive_id)
            if pdf_bytes:
                logger.info(f"[DOWNLOAD] PDF servido desde Drive code={contract_code}")
            else:
                logger.warning(f"[DOWNLOAD] Drive devolvió vacío code={contract_code} drive_id={drive_id}")
        except Exception as e:
            logger.error(f"[DOWNLOAD] Error descargando desde Drive code={contract_code}: {e}")

    # ── Prioridad 2: caché local (solo si Drive no respondió) ───────────────
    if not pdf_bytes:
        for local_path in [
            Path(security.get("original_pdf_path") or ""),
            BASE_DIR / "contracts_pdf" / f"{contract_code}_original.pdf",
            BASE_DIR / "tmp" / "contracts" / contract_code / "contrato_original.pdf",
        ]:
            try:
                if local_path and local_path.exists():
                    with open(local_path, "rb") as f:
                        pdf_bytes = f.read()
                    logger.info(f"[DOWNLOAD] PDF servido desde caché local {local_path}")
                    break
            except Exception:
                pass

    # ── Prioridad 3: Regenerar + subir a Drive para no volver a fallar ──────
    if not pdf_bytes:
        logger.warning(f"[DOWNLOAD] PDF no disponible en Drive ni local, regenerando code={contract_code}")
        data_payload = {
            "contract_code": contract.get("contract_code"),
            "origen": contract.get("origen", ""),
            "property_code": contract.get("property_code", ""),
            "phone": contract.get("phone", ""),
            "cliente_nombre": contract.get("client_data", {}).get("nombre", ""),
            "cliente_rut": contract.get("client_data", {}).get("rut", ""),
            "email": contract.get("client_data", {}).get("email", ""),
            "propiedad_direccion": contract.get("property_data", {}).get("direccion", ""),
            "comuna": contract.get("property_data", {}).get("comuna", ""),
            "ciudad_firma": contract.get("property_data", {}).get("ciudad_firma", "Santiago de Chile"),
            "tipo": contract.get("property_data", {}).get("tipo", "Arriendo"),
            "rol": contract.get("property_data", {}).get("rol", ""),
            "vigencia": contract.get("property_data", {}).get("vigencia", "30"),
            "precio": contract.get("property_data", {}).get("precio", ""),
            "comision": contract.get("property_data", {}).get("comision", ""),
            "created_at": contract.get("created_at"),
            "version": contract.get("version", 1)
        }
        pdf_bytes = PDFGenerator.generate_original_contract(data_payload)
        # Subir a Drive ahora para que próximas solicitudes no vuelvan a regenerar
        try:
            local_db_dl = get_db()
            folder_id = security.get("gdrive_folder_id") or _get_or_create_expedition_folder(
                local_db_dl, "contracts", "contract_code", contract_code,
                data_payload["cliente_nombre"], data_payload["property_code"]
            )
            if folder_id:
                new_file_id = gdrive_sync.upload_file(
                    folder_id, f"{contract_code}_original.pdf", pdf_bytes, "application/pdf"
                )
                if new_file_id and new_file_id != "mock_file_id":
                    local_db_dl["contracts"].update_one(
                        {"contract_code": contract_code},
                        {"$set": {
                            "security.original_pdf_drive_id": new_file_id,
                            "security.gdrive_folder_id": folder_id
                        }}
                    )
                    logger.info(f"[DOWNLOAD] PDF regenerado y subido a carpeta de expediente code={contract_code} file_id={new_file_id}")
        except Exception as e:
            logger.error(f"[DOWNLOAD] No se pudo subir PDF regenerado a Drive code={contract_code}: {e}")

    prop_code = contract.get('property_code', 'SD')
    tipo_raw = contract.get('property_data', {}).get('tipo', 'Arriendo')
    tipo = tipo_raw.replace(" ", "_")

    from fastapi.responses import StreamingResponse
    import io
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Disposition": f"inline; filename=Contrato_{tipo}_{prop_code}_{contract_code}.pdf"
        }
    )

@router.get("/api/download_signed/{contract_code}")
async def download_signed_pdf(contract_code: str):
    """Permite descargar el PDF firmado (Forza descarga)"""
    db = get_db()
    contract = await _db_call(db["contracts"].find_one, {"contract_code": contract_code})
    if not contract:
        raise HTTPException(status_code=404, detail="Contrato no encontrado")

    prop_code = contract.get('property_code', 'SD')
    tipo_raw = contract.get('property_data', {}).get('tipo', 'Arriendo')
    tipo = tipo_raw.replace(" ", "_")
    filename = f"Contrato_Autorizacion_{tipo}_{prop_code}_{contract_code}.pdf"

    local_path = contract.get("security", {}).get("signed_pdf_path")
    if local_path and os.path.exists(local_path):
        from fastapi.responses import FileResponse
        return FileResponse(
            path=local_path,
            filename=filename,
            media_type="application/pdf",
            content_disposition_type="attachment",
            headers={"Cache-Control": "public, max-age=3600"}
        )

    file_id = contract.get("security", {}).get("signed_pdf_drive_id")
    if file_id:
        try:
            gdrive = GDriveSync()
            pdf_bytes = await run_in_threadpool(gdrive.download_file, file_id)
            if pdf_bytes:
                from fastapi.responses import StreamingResponse
                import io
                return StreamingResponse(
                    io.BytesIO(pdf_bytes),
                    media_type="application/pdf",
                    headers={
                        "Cache-Control": "public, max-age=3600",
                        "Content-Disposition": f'attachment; filename="{filename}"'
                    }
                )
        except Exception as e:
            logger.error(f"Error downloading from GDrive: {e}")

    # Si llegamos aquí, no está en local ni en Drive (o Drive falló/no configurado).
    # REGENERAMOS el PDF firmado dinámicamente:
    logger.info(f"Regenerando PDF firmado para {contract_code} dinámicamente...")
    
    # 1. Regenerar el original
    data_payload = {
        "contract_code": contract.get("contract_code"),
        "origen": contract.get("origen", ""),
        "property_code": contract.get("property_code", ""),
        "phone": contract.get("phone", ""),
        "cliente_nombre": contract.get("client_data", {}).get("nombre", ""),
        "cliente_rut": contract.get("client_data", {}).get("rut", ""),
        "email": contract.get("client_data", {}).get("email", ""),
        "propiedad_direccion": contract.get("property_data", {}).get("direccion", ""),
        "comuna": contract.get("property_data", {}).get("comuna", ""),
        "ciudad_firma": contract.get("property_data", {}).get("ciudad_firma", "Santiago de Chile"),
        "tipo": contract.get("property_data", {}).get("tipo", "Arriendo"),
        "rol": contract.get("property_data", {}).get("rol", ""),
        "vigencia": contract.get("property_data", {}).get("vigencia", "30"),
        "precio": contract.get("property_data", {}).get("precio", ""),
        "comision": contract.get("property_data", {}).get("comision", ""),
        "created_at": contract.get("created_at"),
        "version": contract.get("version", 1)
    }
    original_bytes = PDFGenerator.generate_original_contract(data_payload)
    
    # 2. Reconstruir la evidencia desde la DB
    timeline = contract.get("timeline", [])
    accepted_event = next((evt for evt in timeline if evt.get("action") == "accepted"), {})
    
    evidence_data = {
        "contract_code": contract_code,
        "verify_token": contract.get("security", {}).get("verify_token", ""),
        "server_timestamp": accepted_event.get("server_timestamp", ""),
        "ip": accepted_event.get("ip", ""),
        "geo_info": accepted_event.get("geo_location", "Localización no disponible"),
        "timezone": accepted_event.get("timezone", "America/Santiago (CLT)"),
        "user_agent": accepted_event.get("user_agent", ""),
        "original_hash": contract.get("security", {}).get("original_hash", ""),
        "server_hmac": contract.get("security", {}).get("server_hmac", ""),
        "timeline_hash": contract.get("security", {}).get("timeline_hash", ""),
        "read_time_seconds": accepted_event.get("read_time_seconds", 0),
        "scrolled_to_bottom": "Sí" if accepted_event.get("scrolled_to_bottom") else "No",
        "read_method": accepted_event.get("read_method", "scroll")
    }
    
    # Evitar fallar si faltan datos en contratos antiguos
    from fastapi import Request
    base_url = "https://procasa.cl" # default fallback
    verify_url = f"{base_url}/contracts/verify/{evidence_data['verify_token']}"
    
    signed_pdf_bytes = PDFGenerator.generate_signed_contract(original_bytes, contract, evidence_data, verify_url)
    
    from fastapi.responses import StreamingResponse
    import io
    return StreamingResponse(
        io.BytesIO(signed_pdf_bytes),
        media_type="application/pdf",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )

@router.get("/api/view_signed/{contract_code}")
async def view_signed_pdf(contract_code: str):
    """Permite visualizar el PDF firmado dentro del navegador"""
    db = get_db()
    contract = db["contracts"].find_one({"contract_code": contract_code})
    if not contract:
        raise HTTPException(status_code=404, detail="Contrato no encontrado")

    prop_code = contract.get('property_code', 'SD')
    tipo_raw = contract.get('property_data', {}).get('tipo', 'Arriendo')
    tipo = tipo_raw.replace(" ", "_")
    filename = f"Contrato_Autorizacion_{tipo}_{prop_code}_{contract_code}.pdf"

    local_path = contract.get("security", {}).get("signed_pdf_path")
    if local_path and os.path.exists(local_path):
        from fastapi.responses import FileResponse
        return FileResponse(
            path=local_path,
            filename=filename,
            media_type="application/pdf",
            content_disposition_type="inline",
            headers={"Cache-Control": "public, max-age=86400"}
        )

    file_id = contract.get("security", {}).get("signed_pdf_drive_id")
    if file_id:
        try:
            gdrive = GDriveSync()
            pdf_bytes = await run_in_threadpool(gdrive.download_file, file_id)
            if pdf_bytes:
                from fastapi.responses import StreamingResponse
                import io
                return StreamingResponse(
                    io.BytesIO(pdf_bytes),
                    media_type="application/pdf",
                    headers={
                        "Cache-Control": "public, max-age=86400",
                        "Content-Disposition": f'inline; filename="{filename}"'
                    }
                )
        except Exception as e:
            logger.error(f"Error downloading from GDrive: {e}")
            
    # Si llegamos aquí, no está en local ni en Drive (o Drive falló/no configurado).
    # REGENERAMOS el PDF firmado dinámicamente:
    logger.info(f"Regenerando PDF firmado para {contract_code} dinámicamente...")
    
    # 1. Regenerar el original
    data_payload = {
        "contract_code": contract.get("contract_code"),
        "origen": contract.get("origen", ""),
        "property_code": contract.get("property_code", ""),
        "phone": contract.get("phone", ""),
        "cliente_nombre": contract.get("client_data", {}).get("nombre", ""),
        "cliente_rut": contract.get("client_data", {}).get("rut", ""),
        "email": contract.get("client_data", {}).get("email", ""),
        "propiedad_direccion": contract.get("property_data", {}).get("direccion", ""),
        "comuna": contract.get("property_data", {}).get("comuna", ""),
        "ciudad_firma": contract.get("property_data", {}).get("ciudad_firma", "Santiago de Chile"),
        "tipo": contract.get("property_data", {}).get("tipo", "Arriendo"),
        "rol": contract.get("property_data", {}).get("rol", ""),
        "vigencia": contract.get("property_data", {}).get("vigencia", "30"),
        "precio": contract.get("property_data", {}).get("precio", ""),
        "comision": contract.get("property_data", {}).get("comision", ""),
        "created_at": contract.get("created_at"),
        "version": contract.get("version", 1)
    }
    original_bytes = PDFGenerator.generate_original_contract(data_payload)
    
    # 2. Reconstruir la evidencia desde la DB
    timeline = contract.get("timeline", [])
    accepted_event = next((evt for evt in timeline if evt.get("action") == "accepted"), {})
    
    evidence_data = {
        "contract_code": contract_code,
        "verify_token": contract.get("security", {}).get("verify_token", ""),
        "server_timestamp": accepted_event.get("server_timestamp", ""),
        "ip": accepted_event.get("ip", ""),
        "geo_info": accepted_event.get("geo_location", "Localización no disponible"),
        "timezone": accepted_event.get("timezone", "America/Santiago (CLT)"),
        "user_agent": accepted_event.get("user_agent", ""),
        "original_hash": contract.get("security", {}).get("original_hash", ""),
        "server_hmac": contract.get("security", {}).get("server_hmac", ""),
        "timeline_hash": contract.get("security", {}).get("timeline_hash", ""),
        "read_time_seconds": accepted_event.get("read_time_seconds", 0),
        "scrolled_to_bottom": "Sí" if accepted_event.get("scrolled_to_bottom") else "No",
        "read_method": accepted_event.get("read_method", "scroll")
    }
    
    # Evitar fallar si faltan datos en contratos antiguos
    from fastapi import Request
    base_url = "https://procasa.cl" # default fallback
    verify_url = f"{base_url}/contracts/verify/{evidence_data['verify_token']}"
    
    signed_pdf_bytes = PDFGenerator.generate_signed_contract(original_bytes, contract, evidence_data, verify_url)
    
    from fastapi.responses import StreamingResponse
    import io
    return StreamingResponse(
        io.BytesIO(signed_pdf_bytes),
        media_type="application/pdf",
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Disposition": f'inline; filename="{filename}"'
        }
    )

@router.post("/api/{contract_code}/send")
async def send_contract(contract_code: str, request: Request):
    """Genera token y envía por WhatsApp"""
    db = get_db()
    from chatbot.storage import get_async_db
    adb = get_async_db()
    username, user_role = await _get_request_user(adb, request)
    contract = db["contracts"].find_one({"contract_code": contract_code})
    if not contract:
        raise HTTPException(status_code=404, detail="Contrato no encontrado")
    # Después del primer envío, solo supervisor/admin puede reenviar
    if contract.get("status") in ["sent", "opened", "otp_requested", "otp_verified", "signed", "accepted"]:
        if user_role not in ["supervisor", "admin"]:
            raise HTTPException(status_code=403, detail="Solo supervisor/admin puede reenviar convenios ya enviados.")
        
    # Reusar token si aún es válido y asegurar la nueva ventana de 120 horas.
    if contract.get("security", {}).get("token") and not contract["security"]["token_used"]:
        expiry = contract["security"]["token_expiry"]
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if now < expiry:
            token = contract["security"]["token"]
            expiry = max(expiry, now + timedelta(hours=DOCUMENT_LINK_VALIDITY_HOURS))
        else:
            token = str(uuid.uuid4()).replace("-", "")
            expiry = now + timedelta(hours=DOCUMENT_LINK_VALIDITY_HOURS)
    else:
        token = str(uuid.uuid4()).replace("-", "")
        expiry = datetime.now(timezone.utc) + timedelta(hours=DOCUMENT_LINK_VALIDITY_HOURS)
    
    server_timestamp = SecurityContracts.generate_server_timestamp()
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    
    db["contracts"].update_one(
        {"contract_code": contract_code},
        {
            "$set": {
                "status": "sent",
                "security.token": token,
                "security.token_expiry": expiry,
                "security.token_used": False
            },
            "$push": {
                "timeline": {
                    "action": "contract_sent",
                    "server_timestamp": server_timestamp,
                    "ip": ip,
                    "user_agent": ua
                }
            }
        }
    )
    
    # Enviar WhatsApp
    phone = contract.get("phone")
    if not _is_valid_whatsapp_phone(phone):
        logger.warning(
            "[contracts.send.validation_error] invalid_phone_for_whatsapp "
            f"contract_code={contract_code} phone={phone}"
        )
        raise HTTPException(
            status_code=400,
            detail="El teléfono del convenio no es válido para WhatsApp. Actualiza el teléfono y reintenta."
        )
    # Usar la base_url de la request actual para que funcione localmente o en prod
    base_url = str(request.base_url).rstrip('/')
    link = f"{base_url}/contracts/view/{token}"
    
    nombre = contract.get('client_data', {}).get('nombre', contract.get('cliente_nombre', ''))
    tipo_raw = contract.get('property_data', {}).get('tipo', '')
    tipo_label = {
        'Venta': 'Autorización de Venta',
        'Venta Exclusiva': 'Autorización de Venta Exclusiva',
        'Arriendo': 'Autorización de Arriendo',
        'Arriendo y Administración': 'Autorización de Arriendo y Administración'
    }.get(tipo_raw, 'Convenio de Corretaje')
    property_code_display = contract.get('property_code', '')
    prop_suffix = f"\nhttps://www.procasa.cl/{property_code_display}" if property_code_display else ""

    mensaje = f"""Hola {nombre} 👋

Necesitamos que revise y firme digitalmente su {tipo_label}.{prop_suffix}

🔒 Este enlace es personal, confidencial e intransferible. Al ingresar y firmar el documento, usted confirma ser el titular de este número telefónico y acepta las condiciones del convenio de corretaje.

La firma electrónica utilizada en este proceso se encuentra respaldada por la Ley N° 19.799 sobre Documentos y Firma Electrónica.

👉 Revise y firme aquí:
{link}"""
    
    # Guardar mensaje dentro del mismo documento del contrato
    try:
        db["contracts"].update_one(
            {"contract_code": contract_code},
            {"$push": {"messages": {
                "phone": phone,
                "message_content": mensaje,
                "message_type": "contract_sent",
                "timestamp_utc": datetime.now(timezone.utc)
            }}}
        )
    except Exception as e:
        logger.error(f"[MSG_LOG] Error guardando mensaje: {e}")
    
    await send_whatsapp_message(phone, mensaje)
    
    return {"status": "ok", "message": "Enviado por WhatsApp"}

@router.get("/api/statuses")
async def contracts_statuses(request: Request):
    from chatbot.storage import get_async_db
    adb = get_async_db()
    username, user_role = await _get_request_user(adb, request)
    if not username:
        return {"items": []}
    if user_role in ["supervisor", "admin"]:
        query = {"status": {"$ne": "deleted"}}
    else:
        query = {"status": {"$ne": "deleted"}, "created_by": username}
    rows = await adb["contracts"].find(query, {"contract_code": 1, "status": 1}).to_list(length=300)
    return {"items": [{"contract_code": r.get("contract_code"), "status": r.get("status", "created")} for r in rows]}

def ensure_document_valid(contract: dict):
    """Verifica la expiración real del documento (120 horas) en todos los endpoints"""
    now = datetime.now(timezone.utc)
    token_expiry = contract["security"].get("token_expiry")
    if token_expiry:
        if token_expiry.tzinfo is None:
            token_expiry = token_expiry.replace(tzinfo=timezone.utc)
        if now > token_expiry:
            raise HTTPException(status_code=410, detail="DOCUMENT_EXPIRED")


def active_token_query(token: str) -> dict:
    """Find only non-deleted contracts addressed by a signing token."""
    return {
        "security.token": token,
        "status": {"$ne": "deleted"},
    }


@router.get("/view/{token}", response_class=HTMLResponse)
async def view_contract_public(token: str, request: Request):
    """Vista pública para el cliente"""
    db = get_db()
    contract = await _db_call(db["contracts"].find_one, active_token_query(token))
    
    if not contract:
        return HTMLResponse("<h1>Enlace inválido o expirado.</h1>", status_code=404)
        
    is_signed = contract["security"].get("token_used", False)
    
    # Solo expira a las 120h si NO está firmado. Si ya se firmó, el acceso es permanente.
    if not is_signed:
        try:
            ensure_document_valid(contract)
        except HTTPException:
            return HTMLResponse("<h1>Enlace inválido o expirado.</h1>", status_code=404)
    logger.info(f"[METRIC] contracts_started: {contract['contract_code']}")
        
    # Registrar acceso
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    server_timestamp = SecurityContracts.generate_server_timestamp()
    
    # Una apertura posterior a la firma es válida (por ejemplo, para volver a
    # consultar el documento), pero nunca debe degradar el estado final.
    # Mantener esta condición aquí es importante porque el enlace firmado sigue
    # siendo accesible después de la firma.
    update_doc = {
        "$push": {
            "access_logs": {"ip": ip, "user_agent": ua, "timestamp": server_timestamp},
            "timeline": {
                "action": "link_opened",
                "server_timestamp": server_timestamp,
                "ip": ip,
                "user_agent": ua
            }
        }
    }
    update_filter = {"contract_code": contract["contract_code"]}
    if not is_signed and contract.get("status") not in ["signed", "accepted"]:
        update_doc["$set"] = {"status": "opened"}
        # La condición también se aplica en MongoDB para cubrir la carrera en
        # la que el cliente firma entre el find_one() y este update_one().
        update_filter.update({
            "status": {"$nin": ["signed", "accepted"]},
            "security.token_used": {"$ne": True},
        })

    await _db_call(
        db["contracts"].update_one,
        update_filter,
        update_doc,
    )
    
    # Obtener token_expiry exacto en America/Santiago para evitar el bug de las 27 horas
    expiry = contract["security"].get("token_expiry")
    token_expiry_iso = ""
    if expiry:
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        import pytz
        chile_tz = pytz.timezone('America/Santiago')
        expiry_chile = expiry.astimezone(chile_tz)
        token_expiry_iso = expiry_chile.isoformat()
        
    return templates.TemplateResponse(request, "contract_view.html", {
        "request": request,
        "contract": contract,
        "token": token,
        "token_expiry_iso": token_expiry_iso,
        "is_signed": is_signed
    })

@router.post("/api/{token}/validate-rut")
async def validate_rut(token: str, request: Request):
    """Valida el RUT contra el contrato antes de solicitar el OTP (sin enviarlo)"""
    data = await request.json()
    rut_ingresado = data.get("rut", "").strip()
    
    db = get_db()
    contract = db["contracts"].find_one(active_token_query(token))
    if not contract:
        raise HTTPException(status_code=403, detail="DOCUMENT_EXPIRED")
        
    contract_rut = contract.get("client_data", {}).get("rut", "").strip()
    if not contract_rut:
        contract_rut = contract.get("cliente_rut", "").strip()

    if contract_rut:
        rut_clean = ''.join(filter(str.isalnum, rut_ingresado)).upper()
        contract_rut_clean = ''.join(filter(str.isalnum, contract_rut)).upper()
        if contract_rut_clean != rut_clean:
            raise HTTPException(status_code=400, detail="RUT no coincide con el registrado.")
        
    return {"status": "ok"}

@router.post("/api/{token}/request-otp")
async def request_otp(token: str, request: Request, background_tasks: BackgroundTasks):
    """Genera OTP y lo env\u00eda por WA en background"""
    t_otp_start = time.time()
    data = await request.json()
    rut_ingresado = data.get("rut", "").strip()
    
    ip = get_client_ip(request)
    check_rate_limit(ip, otp_rate_limit, 3, window_seconds=10)
    
    from chatbot.storage import get_async_db
    adb = get_async_db()
    contract = await adb["contracts"].find_one(active_token_query(token))
    if not contract:
        raise HTTPException(status_code=404, detail="Token inv\u00e1lido")
        
    ensure_document_valid(contract)
    if not _is_valid_whatsapp_phone(contract.get("phone", "")):
        raise HTTPException(
            status_code=400,
            detail="Teléfono inválido para recibir OTP por WhatsApp. Contacta a tu ejecutivo."
        )
    
    if contract.get("status") == "signed" or contract["security"].get("token_used"):
        return JSONResponse(status_code=200, content={"status": "already_signed"})
        
    now = datetime.now(timezone.utc)
    blocked_until = contract["security"].get("blocked_until")
    if blocked_until:
        if blocked_until.tzinfo is None:
            blocked_until = blocked_until.replace(tzinfo=timezone.utc)
        if now < blocked_until:
            wait_seconds = int((blocked_until - now).total_seconds())
            raise HTTPException(status_code=429, detail=f"El sistema est\u00e1 bloqueado temporalmente. Por favor espera {wait_seconds} segundos.")
            
    contract_rut = contract.get("client_data", {}).get("rut", "").strip()
    if not contract_rut:
        contract_rut = contract.get("cliente_rut", "").strip()

    if contract_rut:
        rut_clean = ''.join(filter(str.isalnum, rut_ingresado)).upper()
        contract_rut_clean = ''.join(filter(str.isalnum, contract_rut)).upper()
        if contract_rut_clean != rut_clean:
            raise HTTPException(status_code=400, detail="RUT no coincide con el registrado.")
    # Rate limiting: bloquear si se solicit\u00f3 OTP hace menos de 30 segundos
    now = datetime.now(timezone.utc)
    last_request = contract["security"].get("last_otp_request")
    otp_expiry = contract["security"].get("otp_expiry")
    
    if otp_expiry and otp_expiry.tzinfo is None:
        otp_expiry = otp_expiry.replace(tzinfo=timezone.utc)
        
    if otp_expiry and now > otp_expiry:
        # El OTP anterior expir\u00f3, permitimos solicitar uno nuevo inmediatamente y limpiamos el IP limit
        with rate_limit_lock:
            if ip in otp_rate_limit:
                otp_rate_limit[ip] = []
    elif last_request:
        if last_request.tzinfo is None:
            last_request = last_request.replace(tzinfo=timezone.utc)
        seconds_elapsed = (now - last_request).total_seconds()
        if seconds_elapsed < 10:
            raise HTTPException(status_code=429, detail="Espera unos segundos antes de solicitar un nuevo c\u00f3digo.")

    otp = SecurityContracts.generate_otp(4)
    otp_expiry = now + timedelta(minutes=5)
    
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    server_timestamp = SecurityContracts.generate_server_timestamp()
    
    await adb["contracts"].update_one(
        {"contract_code": contract["contract_code"]},
        {
            "$set": {
                "status": "otp_requested",
                "security.otp": otp,
                "security.otp_expiry": otp_expiry,
                "security.otp_attempts": 0,
                "security.last_otp_request": now,
                "security.blocked_until": None
            },
            "$push": {
                "timeline": {
                    "$each": [
                        {
                            "action": "rut_confirmed",
                            "server_timestamp": server_timestamp,
                            "ip": ip,
                            "user_agent": ua
                        },
                        {
                            "action": "otp_requested",
                            "server_timestamp": server_timestamp,
                            "ip": ip,
                            "user_agent": ua
                        }
                    ]
                }
            }
        }
    )
    
    nombre_cliente = contract.get('client_data', {}).get('nombre', '').split()[0] if contract.get('client_data', {}).get('nombre') else ''
    mensaje = f"""{'Hola ' + nombre_cliente + ', tu c' if nombre_cliente else 'C'}ódigo de verificación para firmar tu Convenio de Corretaje es:

🔑 *{otp}*

⏳ Válido por 5 minutos.
🔒 No compartas este código con nadie."""
    
    try:
        await adb["contracts"].update_one(
            {"contract_code": contract["contract_code"]},
            {"$push": {"messages": {
                "phone": contract["phone"],
                "message_content": mensaje,
                "message_type": "otp_sent",
                "timestamp_utc": datetime.now(timezone.utc)
            }}}
        )
    except Exception as e:
        logger.error(f"[MSG_LOG] Error guardando mensaje OTP: {e}")

    # Enviar WhatsApp en background
    async def send_wa_bg_task(phone, msg, c_code):
        try:
            await send_whatsapp_circuit_breaker(phone, msg)
        except Exception as e:
            logger.error(f"[OTP_FAILED] Error enviando WhatsApp al {phone}: {e}")
            from chatbot.storage import get_async_db
            local_adb = get_async_db()
            await local_adb["contracts"].update_one(
                {"contract_code": c_code},
                {"$push": {"timeline": {"action": "otp_delivery_failed", "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua, "error": str(e)}}}
            )

    background_tasks.add_task(send_wa_bg_task, contract["phone"], mensaje, contract["contract_code"])
    
    t_otp_elapsed = time.time() - t_otp_start
    logger.info(f"[TIMING] request_otp: contract_code={contract['contract_code']} response_time={t_otp_elapsed:.3f}s")
    return {"status": "ok"}

@router.post("/api/{token}/verify-otp")
async def verify_otp(token: str, request: Request):
    data = await request.json()
    otp_ingresado = data.get("otp", "").strip()
    
    ip = get_client_ip(request)
    check_rate_limit(ip, verify_rate_limit, 10, window_seconds=60)
    
    db = get_db()
    contract = await _db_call(db["contracts"].find_one, active_token_query(token))
    if not contract:
        raise HTTPException(status_code=404)
        
    ensure_document_valid(contract)
    
    if contract.get("status") == "signed" or contract["security"].get("token_used"):
        return JSONResponse(status_code=200, content={"status": "already_signed"})
        
    if contract.get("status") == "otp_verified":
        return {"status": "ok"}
        
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    server_timestamp = SecurityContracts.generate_server_timestamp()
    
    # Check blocks
    now = datetime.now(timezone.utc)
    blocked_until = contract["security"].get("blocked_until")
    if blocked_until:
        if blocked_until.tzinfo is None:
            blocked_until = blocked_until.replace(tzinfo=timezone.utc)
        if now < blocked_until:
            wait_seconds = int((blocked_until - now).total_seconds())
            raise HTTPException(status_code=429, detail=f"OTP_BLOCKED|{wait_seconds}")
            
    # Check attempts
    attempts = contract["security"].get("otp_attempts", 0)
        
    # Check expiry
    expiry = contract["security"]["otp_expiry"]
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if now > expiry:
        logger.info(f"[USER_RETURNED_TO_STEP2] contract_code={contract['contract_code']} reason=OTP_EXPIRED timestamp={now}")
        raise HTTPException(status_code=400, detail="OTP_EXPIRED")
        
    # Validate
    if otp_ingresado != contract["security"]["otp"]:
        attempts += 1
        update_doc = {
            "$set": {"security.otp_attempts": attempts},
            "$push": {"timeline": {"action": "otp_failed_attempt", "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua, "details": "OTP incorrecto"}}
        }
        
        if attempts >= 5:
            blocked_until_new = now + timedelta(seconds=60)
            update_doc["$set"]["security.blocked_until"] = blocked_until_new
            update_doc["$push"]["timeline"] = {"action": "otp_blocked_max_attempts", "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua}
            await _db_call(db["contracts"].update_one, {"contract_code": contract["contract_code"]}, update_doc)
            raise HTTPException(status_code=429, detail="OTP_BLOCKED|60")
        else:
            await _db_call(db["contracts"].update_one, {"contract_code": contract["contract_code"]}, update_doc)
            remaining = 5 - attempts
            raise HTTPException(status_code=400, detail=f"OTP_INVALID|{remaining}")
        
    # Success
    await _db_call(
        db["contracts"].update_one,
        {"contract_code": contract["contract_code"]},
        {
            "$set": {"status": "otp_verified", "security.otp": None}, # Invalidate OTP
            "$push": {"timeline": {"action": "otp_verified", "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua}}
        }
    )
    return {"status": "ok"}

@router.post("/api/{token}/accept_terms")
async def accept_terms(token: str, request: Request):
    db = get_db()
    contract = db["contracts"].find_one(active_token_query(token))
    if not contract: return {"status": "error"}
    
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    server_timestamp = SecurityContracts.generate_server_timestamp()
    
    data = await request.json()
    checkbox_state = data.get("accepted", False)
    
    db["contracts"].update_one(
        {"contract_code": contract["contract_code"]},
        {"$push": {"timeline": {
            "action": "terms_accepted", 
            "server_timestamp": server_timestamp, 
            "ip": ip, 
            "user_agent": ua,
            "checkbox_state": checkbox_state
        }}}
    )
    return {"status": "ok"}

@router.post("/api/{token}/accept", dependencies=[Depends(signature_concurrency)])
async def accept_contract(token: str, request: Request, background_tasks: BackgroundTasks):
    import shutil
    db = get_db()
    contract = await _db_call(db["contracts"].find_one, active_token_query(token))
    if not contract:
        logger.error(f"[SERVER_ERROR] Token inválido intentado para {token}")
        raise HTTPException(status_code=403, detail="Token inválido")

    ensure_document_valid(contract)

    # Idempotencia: si ya fue firmado, retornar éxito sin reprocessar
    if contract.get("status") == "signed" or contract["security"].get("token_used"):
        logger.info(f"[CONTRACT_SIGNED] Idempotente — contrato {contract['contract_code']} ya firmado.")
        return JSONResponse(status_code=200, content={"status": "already_signed", "contract_code": contract["contract_code"]})

    if contract["status"] != "otp_verified":
        raise HTTPException(status_code=403, detail="Contrato no válido para aceptación")

    # Timeout de sesión de firma (15 min desde OTP)
    # La expiración de sesión NO invalida el documento (vigencia 120h)
    # Solo lanza el error para que frontend vuelva a Paso 2
    SIGNATURE_TIMEOUT_MINUTES = 15
    otp_expiry = contract["security"].get("otp_expiry")
    if otp_expiry:
        if otp_expiry.tzinfo is None:
            otp_expiry = otp_expiry.replace(tzinfo=timezone.utc)
        session_deadline = otp_expiry + timedelta(minutes=SIGNATURE_TIMEOUT_MINUTES)
        if datetime.now(timezone.utc) > session_deadline:
            logger.error(f"[DOCUMENT_EXPIRED] contract_code={contract['contract_code']} reason=SIGNATURE_SESSION_EXPIRED timestamp={datetime.now(timezone.utc)}")
            raise HTTPException(status_code=400, detail="SIGNATURE_SESSION_EXPIRED")
        
    try:
        data = await request.json()
        read_time = data.get("read_time_seconds", 0)
        scrolled_to_bottom = data.get("scrolled_to_bottom", False)
        read_method = data.get("read_method", "scroll")
    except:
        read_time = 0
        scrolled_to_bottom = False
        read_method = "scroll"
        
    ip = get_client_ip(request)
    # Geolocalizaci\u00f3n: NO bloqueante \u2014 se pasa al background task
    geo_info = "Localizaci\u00f3n no disponible"  # default; background task actualizar\u00e1 DB
        
    ua = request.headers.get("user-agent", "")
    server_timestamp = SecurityContracts.generate_server_timestamp()
    contract_code = contract["contract_code"]
    t_sign_start = time.time()
    
    timezone_info = "America/Santiago (CLT)"
    
    # 1. Registrar aceptaci\u00f3n
    await _db_call(
        db["contracts"].update_one,
        {"contract_code": contract_code},
        {
            "$set": {
                "status": "signed", 
                "security.token_used": True,
                "locked": True
            },
            "$push": {"timeline": {
                "action": "accepted", 
                "server_timestamp": server_timestamp, 
                "ip": ip, 
                "geo_location": geo_info,
                "timezone": timezone_info,
                "user_agent": ua,
                "read_time_seconds": read_time,
                "scrolled_to_bottom": scrolled_to_bottom,
                "read_method": read_method
            }}
        }
    )
    
    logger.info(f"[METRIC] contracts_signed: {contract_code}")
    
    # Refrescar documento para tener el timeline completo
    contract = await _db_call(db["contracts"].find_one, {"contract_code": contract_code})
    timeline = contract.get("timeline") or []  # Fix: guard against NoneType
    
    # 2. Generar Firma HMAC del Servidor y Hash del Timeline
    timeline_hash = SecurityContracts.hash_timeline(timeline)
    tmp_dir = BASE_DIR / "tmp" / "contracts" / contract_code
    try:
        original_pdf_path = tmp_dir / "contrato_original.pdf"
        if original_pdf_path.exists():
            with open(original_pdf_path, "rb") as f:
                original_bytes = f.read()
        else:
            data_payload = {
                "contract_code": contract.get("contract_code"),
                "origen": contract.get("origen", ""),
                "property_code": contract.get("property_code", ""),
                "phone": contract.get("phone", ""),
                "cliente_nombre": contract.get("client_data", {}).get("nombre", ""),
                "cliente_rut": contract.get("client_data", {}).get("rut", ""),
                "email": contract.get("client_data", {}).get("email", ""),
                "propiedad_direccion": contract.get("property_data", {}).get("direccion", ""),
                "comuna": contract.get("property_data", {}).get("comuna", ""),
                "tipo": contract.get("property_data", {}).get("tipo", "Arriendo"),
                "rol": contract.get("property_data", {}).get("rol", ""),
                "vigencia": contract.get("property_data", {}).get("vigencia", "30"),
                "precio": contract.get("property_data", {}).get("precio", ""),
                "comision": contract.get("property_data", {}).get("comision", "")
            }
            original_bytes = PDFGenerator.generate_original_contract(data_payload)

        original_hash = SecurityContracts.hash_document(original_bytes)
        secret_key = getattr(Config, "SECRET_KEY", "default_secret")
        server_hmac = SecurityContracts.generate_server_hmac(contract_code, original_hash, server_timestamp, secret_key)
        base_url = str(request.base_url).rstrip('/')
        verify_token = str(uuid.uuid4()).replace("-", "")
        verify_url = f"{base_url}/contracts/verify/{verify_token}"

        transaction_uuid = str(uuid.uuid4())
        evidence_data = {
            "contract_code": contract_code,
            "transaction_uuid": transaction_uuid,
            "verify_token": verify_token,
            "server_timestamp": server_timestamp,
            "ip": ip,
            "geo_info": geo_info,
            "timezone": timezone_info,
            "user_agent": ua,
            "original_hash": original_hash,
            "server_hmac": server_hmac,
            "timeline_hash": timeline_hash,
            "read_time_seconds": read_time,
            "scrolled_to_bottom": "Sí" if scrolled_to_bottom else "No",
            "read_method": read_method
        }

        # 3. Generar PDF Firmado Completo
        signed_pdf_bytes = PDFGenerator.generate_signed_contract(original_bytes, contract, evidence_data, verify_url)
        signed_hash = SecurityContracts.hash_document(signed_pdf_bytes)

        # Guardar archivos en tmp (para subida a drive)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        with open(tmp_dir / "contrato_firmado.pdf", "wb") as f:
            f.write(signed_pdf_bytes)
            
        # Guardar localmente permanente como respaldo por si Drive no funciona
        perm_dir = BASE_DIR / "contracts_pdf"
        perm_dir.mkdir(parents=True, exist_ok=True)
        local_pdf_path = perm_dir / f"{contract_code}_firmado.pdf"
        with open(local_pdf_path, "wb") as f:
            f.write(signed_pdf_bytes)

        import json
        with open(tmp_dir / "hash.txt", "w") as f:
            f.write(f"Original Hash: {original_hash}\nSigned Hash: {signed_hash}\nTimeline Hash: {timeline_hash}\nHMAC: {server_hmac}")
        with open(tmp_dir / "timeline.json", "w") as f:
            json.dump(timeline, f, indent=4)

        # 5. Guardar Hashes finales y ruta local en DB
        await _db_call(
            db["contracts"].update_one,
            {"contract_code": contract_code},
            {"$set": {
                "security.signed_hash": signed_hash,
                "security.server_hmac": server_hmac,
                "security.timeline_hash": timeline_hash,
                "security.signed_pdf_path": str(local_pdf_path),
                "security.verify_token": verify_token
            }}
        )

        # TSA mock
        tsa_response = f"TSA_MOCK_{datetime.now(timezone.utc).timestamp()}_SIGNED"
        await _db_call(db["contracts"].update_one, {"contract_code": contract_code}, {"$set": {"security.tsa_stamp": tsa_response}})

        # 6. Generar Informe Legal y Subida a Google Drive en Background
        def finalize_bg_task(c_code, c_doc, e_data, t_line, s_pdf_bytes, o_hash, s_hash, t_hash, s_hmac):
            try:
                # Import din\u00e1mico
                from services.pdf_generator_contracts import PDFGenerator
                l_report_bytes = PDFGenerator.generate_legal_report(c_doc, e_data, t_line)
                
                t_dir = BASE_DIR / "tmp" / "contracts" / c_code
                t_dir.mkdir(parents=True, exist_ok=True)
                with open(t_dir / "informe_legal.pdf", "wb") as f:
                    f.write(l_report_bytes)
                    
                upload_to_gdrive_bg(
                    c_code,
                    {
                        "contrato_firmado.pdf": s_pdf_bytes,
                        "informe_legal.pdf": l_report_bytes,
                        "hash.txt": f"Original Hash: {o_hash}\nSigned Hash: {s_hash}\nTimeline Hash: {t_hash}\nHMAC: {s_hmac}".encode(),
                    }
                )
            except Exception as e:
                logger.error(f"[BG TASK] Error finalizando contrato {c_code}: {e}")

        background_tasks.add_task(
            finalize_bg_task,
            contract_code, contract, evidence_data, timeline, 
            signed_pdf_bytes, original_hash, signed_hash, timeline_hash, server_hmac
        )

        # 7. Notificar al Cliente y enviar email — BACKGROUND (no bloquear respuesta)
        client_email = contract.get("client_data", {}).get("email", contract.get("email", ""))
        background_tasks.add_task(
            notify_client_bg,
            contract_code,
            contract.get("phone", ""),
            client_email,
            contract.get("client_data", {}).get("nombre", ""),
            signed_pdf_bytes,
            contract.get("property_code", "")
        )

        t_sign_elapsed = time.time() - t_sign_start
        logger.info(
            f"[TIMING] accept_contract: contract_code={contract_code} "
            f"response_time={t_sign_elapsed:.3f}s ip={ip} timestamp={server_timestamp}"
        )
        logger.info(
            f"[CONTRACT_SIGNED] contract_code={contract_code} "
            f"rut={contract.get('client_data', {}).get('rut', 'N/A')} "
            f"ip={ip} timestamp={server_timestamp} "
            f"read_time={read_time}s scrolled={scrolled_to_bottom}"
        )

        return {"status": "ok", "contract_code": contract_code}

    finally:
        # Limpieza del directorio temporal (siempre, incluso en caso de error)
        # Los archivos ya fueron subidos a GDrive y enviados por email antes de llegar aquí
        shutil.rmtree(tmp_dir, ignore_errors=True)
    
def _get_or_create_expedition_folder(db, collection, code_field, code, client_name, property_code):
    """
    Estructura limpia de 1 nivel para Convenios:
    Carpeta Raíz (Convenios) -> [Nombre_Propietario]_[Direccion_Propiedad]
    """
    try:
        doc = db[collection].find_one({code_field: code}) or {}
        existing_id = doc.get("security", {}).get("gdrive_folder_id")
        if existing_id and existing_id != "mock_folder_id":
            return existing_id

        address = (doc.get("property_data") or {}).get("direccion", "") or property_code or ""
        folder_name = f"{sanitize_folder_name(client_name, 'Propietario')}_{sanitize_folder_name(address, 'Direccion')}"
        folder_id = gdrive_sync.create_folder(folder_name)
        if folder_id and folder_id != "mock_folder_id":
            db[collection].update_one(
                {code_field: code},
                {"$set": {"security.gdrive_folder_id": folder_id}}
            )
            return folder_id
    except Exception as e:
        logger.error(f"[GDRIVE] Error creando carpeta de convenio {code}: {e}")
    return None


def upload_to_gdrive_bg(contract_code: str, files: dict):
    """Sube archivos a GDrive recibiendo bytes en memoria, sin depender del filesystem."""
    try:
        db = get_db()
        contract = db["contracts"].find_one({"contract_code": contract_code})
        if not contract:
            logger.error(f"[GDRIVE] Contrato {contract_code} no encontrado para subir expediente")
            return
        client_name = contract.get("client_data", {}).get("nombre", "")
        property_code = contract.get("property_code", "")
        folder_id = _get_or_create_expedition_folder(
            db, "contracts", "contract_code", contract_code, client_name, property_code
        )
        if not folder_id:
            logger.error(f"[GDRIVE] Sin carpeta para expediente {contract_code}")
            return
        signed_file_id = None
        for filename, content in files.items():
            if isinstance(content, str):
                content = content.encode()
            mime = "application/pdf" if filename.endswith(".pdf") else "text/plain"
            file_id = gdrive_sync.upload_file(folder_id, filename, content, mime)
            if filename == "contrato_firmado.pdf":
                signed_file_id = file_id
        # Guardar el file_id del contrato firmado en DB para trazabilidad documental
        if signed_file_id:
            db["contracts"].update_one(
                {"contract_code": contract_code},
                {"$set": {"security.signed_pdf_drive_id": signed_file_id}}
            )
        logger.info(f"[GDRIVE] Expediente {contract_code} subido. signed_pdf_id={signed_file_id}")
    except Exception as e:
        logger.error(f"[GDRIVE] Error subiendo expediente {contract_code}: {e}")

def notify_client_bg(contract_code: str, phone: str, client_email: str, nombre: str, signed_pdf_bytes: bytes, property_code: str = ""):
    """Background task: sends WhatsApp confirmation + email. Runs after response is returned."""
    import asyncio
    db = get_db()
    contract = db["contracts"].find_one({"contract_code": contract_code})
    if not contract:
        logger.error(f"[NOTIFY_BG] Contract {contract_code} not found for notification")
        return

    # Exactly-once guard
    if contract.get("notifications_sent"):
        logger.info(f"[NOTIFY_BG] Notifications already sent for {contract_code} — skipping")
        return

    # Mark as sent immediately to prevent duplicates
    db["contracts"].update_one(
        {"contract_code": contract_code},
        {"$set": {"notifications_sent": True}}
    )

    # WhatsApp confirmation
    nombre_cliente = contract.get('client_data', {}).get('nombre', '') if contract else ''
    mensaje_conf = f"""✅ ¡Proceso completado con éxito{',' + ' ' + nombre_cliente.split()[0] if nombre_cliente else ''}!

Tu Convenio de Corretaje fue firmado electrónicamente y registrado de forma segura conforme a la Ley N° 19.799.

📄 En breve recibirás una copia del documento firmado en tu correo electrónico."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(send_whatsapp_message(phone, mensaje_conf))
        loop.close()
        db["contracts"].update_one(
            {"contract_code": contract_code},
            {"$push": {"messages": {
                "phone": phone,
                "message_content": mensaje_conf,
                "message_type": "confirmation_sent",
                "timestamp_utc": datetime.now(timezone.utc)
            }}}
        )
    except Exception as e:
        logger.error(f"[NOTIFY_BG] WhatsApp error for {contract_code}: {e}")

    # Email delivery
    if client_email and signed_pdf_bytes:
        send_signed_email_task(contract_code, client_email, nombre, signed_pdf_bytes, property_code)

def send_signed_email_task(contract_code: str, email_to: str, nombre: str, pdf_bytes: bytes, property_code: str = "", cc_email: str = ""):
    """Envía el PDF firmado al cliente. Recibe bytes en memoria, sin depender del filesystem."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders

    try:
        gmail_user = Config.GMAIL_USER
        gmail_pass = Config.GMAIL_PASSWORD
        if not gmail_user or not gmail_pass:
            logger.warning("[EMAIL] Credenciales Gmail no configuradas, omitiendo envío.")
            return

        prop_label = property_code if property_code else contract_code
        asunto = f"Convenio Firmado – Propiedad {prop_label} – {nombre}"
        
        db = get_db()
        contract = db["contracts"].find_one({"contract_code": contract_code})
        tipo_raw = contract.get("property_data", {}).get("tipo", "Arriendo") if contract else "Arriendo"
        tipo = tipo_raw.replace(" ", "_")
        pdf_filename = f"Contrato_Autorizacion_{tipo}_{prop_label}_{contract_code}.pdf"
        
        # Destinatarios CC
        cc_recipients = ["jpcaro@procasa.cl", "pgalleguillos@procasa.cl"]
        
        # Buscar el email del ejecutivo que creó el documento para incluirlo en copia
        if contract:
            exec_email = contract.get("executive_data", {}).get("email") or contract.get("ejecutivo_email")
            if exec_email and exec_email.strip():
                cc_recipients.append(exec_email.strip())
                
        if cc_email and cc_email != email_to and cc_email not in cc_recipients:
            cc_recipients.append(cc_email)
            
        cc_str = ", ".join(cc_recipients)
        all_recipients = [email_to] + cc_recipients

        msg = MIMEMultipart()
        msg["From"] = f"Procasa Sucre <{gmail_user}>"
        msg["To"] = email_to
        msg["Cc"] = cc_str
        msg["Subject"] = asunto

        verify_token_val = contract.get('security', {}).get('verify_token', contract_code) if contract else contract_code
        verify_url_email = f"{Config.CRM_BASE_URL}/contracts/verify/{verify_token_val}"
        tipo_raw_email = contract.get('property_data', {}).get('tipo', '') if contract else ''
        tipo_label_email = {
            'Venta': 'Autorización de Venta',
            'Venta Exclusiva': 'Autorización de Venta Exclusiva',
            'Arriendo': 'Autorización de Arriendo',
            'Arriendo y Administración': 'Autorización de Arriendo y Administración'
        }.get(tipo_raw_email, 'Convenio de Corretaje')

        body = f"""Estimado/a {nombre}:

Junto con saludar, adjuntamos el Convenio de Corretaje correspondiente a la propiedad N° {prop_label} ({tipo_label_email}), el cual ha sido firmado electrónicamente conforme a la Ley N° 19.799 sobre Documentos y Firma Electrónica.

Detalle del convenio:
• Propiedad: {prop_label}
• Código del convenio: {contract_code}

Para validar la autenticidad del documento, puede ingresar al siguiente enlace:
{verify_url_email}

Ante cualquier consulta, quedamos atentos para ayudarle.

Saludos cordiales,
Equipo Procasa Sucre"""

        msg.attach(MIMEText(body, "plain", "utf-8"))

        part = MIMEBase("application", "octet-stream")
        part.set_payload(pdf_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{pdf_filename}"')
        msg.attach(part)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, all_recipients, msg.as_string())

        logger.info(f"[EMAIL] PDF firmado enviado a {email_to} (CC: {cc_str}) para convenio {contract_code}")

        db = get_db()
        db["contracts"].update_one(
            {"contract_code": contract_code},
            {"$push": {"messages": {
                "email": email_to,
                "cc": cc_recipients,
                "message_type": "email_signed_pdf_sent",
                "timestamp_utc": datetime.now(timezone.utc)
            }}}
        )
    except Exception as e:
        logger.error(f"[EMAIL] Error enviando correo firmado a {email_to}: {e}")

@router.delete("/api/delete/{contract_code}")
@router.delete("/api/{contract_code}/delete")
async def delete_contract(contract_code: str):
    """Permite eliminar un contrato lógicamente (soft delete)."""
    db = get_db()
    revoked_at = datetime.now(timezone.utc)
    result = db["contracts"].update_one(
        {"contract_code": contract_code},
        {
            "$set": {
                "status": "deleted",
                "security.token": None,
                "security.token_expiry": revoked_at,
                "security.token_revoked_at": revoked_at,
                "security.otp": None,
                "security.otp_expiry": revoked_at,
            },
            "$push": {
                "timeline": {
                    "action": "document_deleted",
                    "server_timestamp": revoked_at.isoformat(),
                }
            },
        }
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Contrato no encontrado")
    return {"status": "ok"}

@router.get("/verify/{contract_code}", response_class=HTMLResponse)
async def verify_contract(contract_code: str, request: Request):
    from chatbot.storage import get_async_db
    db = get_async_db()
    contract = await db["contracts"].find_one({
        "$or": [
            {"contract_code": contract_code},
            {"security.verify_token": contract_code}
        ]
    })
    if not contract:
        return HTMLResponse("<h1>Contrato no encontrado</h1>", status_code=404)
        
    return templates.TemplateResponse(request, "contract_verify.html", {
        "request": request,
        "contract": contract
    })

@router.get("/dashboard", response_class=HTMLResponse)
async def contract_dashboard(request: Request):
    """Módulo principal para gestión y generación de convenios de corretaje"""
    from bson import ObjectId
    from chatbot.storage import get_async_db

    def _json_safe(value):
        """Convierte tipos BSON/fecha a estructuras serializables para Jinja tojson."""
        if isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(v) for v in value]
        return value

    adb = get_async_db()
    username, user_role = await _get_request_user(adb, request)

    if not username:
        return RedirectResponse(url="/", status_code=303)

    # AISLAMIENTO DE DATOS: supervisores/admin ven todos; agentes solo los suyos
    executive_filter = (request.query_params.get("executive") or "").strip()
    if user_role in ["supervisor", "admin"]:
        query = {"status": {"$ne": "deleted"}}
    else:
        query = {"status": {"$ne": "deleted"}, "created_by": username}

    selected_exec_name = ""
    if executive_filter and user_role in ["supervisor", "admin"]:
        selected_user = await adb["usuarios"].find_one({"username": executive_filter}, {"nombre": 1})
        selected_exec_name = ((selected_user or {}).get("nombre") or "").strip()
        query["$or"] = [
            {"executive": executive_filter},
            {"created_by": executive_filter},
        ]
        if selected_exec_name:
            query["$or"].append({"executive": selected_exec_name})

    contracts_cursor = adb["contracts"].find(query).sort("created_at", -1).limit(100)
    contracts = await contracts_cursor.to_list(length=100)

    for c in contracts:
        # Normalizar property_data y client_data para renderizado seguro en template
        c["property_data"] = c.get("property_data") or {}
        c["client_data"] = c.get("client_data") or {}

        pd = c["property_data"]
        cd = c["client_data"]

        if not pd.get("direccion") and c.get("propiedad_direccion"):
            pd["direccion"] = c["propiedad_direccion"]
        if not pd.get("comuna") and c.get("comuna"):
            pd["comuna"] = c["comuna"]
        if not pd.get("rol") and c.get("rol"):
            pd["rol"] = c["rol"]
        if not pd.get("tipo") and c.get("tipo"):
            pd["tipo"] = c["tipo"]
        if not pd.get("ciudad_firma") and c.get("ciudad_firma"):
            pd["ciudad_firma"] = c["ciudad_firma"]

        if not cd.get("nombre") and c.get("cliente_nombre"):
            cd["nombre"] = c["cliente_nombre"]
        if not cd.get("rut") and c.get("cliente_rut"):
            cd["rut"] = c["cliente_rut"]
        if not cd.get("email") and c.get("email"):
            cd["email"] = c["email"]

        pd.setdefault("direccion", "S/I")
        pd.setdefault("rol", "S/I")
        pd.setdefault("comuna", "S/I")
        cd.setdefault("nombre", "S/I")
        cd.setdefault("rut", "S/I")

        if "_id" in c:
            c["_id"] = str(c["_id"])
        if c.get("created_at"):
            dt_utc = c["created_at"].replace(tzinfo=timezone.utc)
            c["created_at"] = dt_utc.astimezone(CHILE_TZ)
        c["edit_data"] = {
            "contract_code": c.get("contract_code", ""),
            "client_data": cd,
            "property_data": pd,
            "property_code": c.get("property_code", ""),
            "phone": c.get("phone", ""),
            "origen": c.get("origen", ""),
            "ciudad_firma": pd.get("ciudad_firma", "Santiago de Chile"),
            "executive": c.get("executive", ""),
            "created_by": c.get("created_by", "")
        }
        c["edit_data"] = _json_safe(c["edit_data"])

    users = await adb["usuarios"].find(
        {"rol": {"$in": ["agente", "supervisor", "admin"]}},
        {"username": 1, "nombre": 1}
    ).to_list(length=300)
    user_name_map = {}
    for u in users:
        uname = (u.get("username") or "").strip()
        if not uname:
            continue
        display_name = (u.get("nombre") or uname).strip()
        user_name_map[uname] = display_name

    executives = []
    if user_role in ["supervisor", "admin"]:
        executives = sorted(
            [
                {"username": uname, "name": name}
                for uname, name in user_name_map.items()
            ],
            key=lambda x: (x.get("name") or x.get("username") or "").lower()
        )

    for c in contracts:
        exec_username = (c.get("executive") or c.get("created_by") or "").strip()
        c["executive_display"] = user_name_map.get(exec_username, exec_username or "---")

    return templates.TemplateResponse(request, "contract_dashboard.html", {
        "request": request,
        "contracts": contracts,
        "user_role": user_role,
        "user_username": username or "",
        "user_display_name": user_name_map.get(username or "", username or ""),
        "executives": executives,
        "executive_filter": executive_filter
    })
