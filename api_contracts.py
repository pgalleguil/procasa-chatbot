import os
import uuid
import logging
import threading
import psutil
import time
from collections import defaultdict
from contextlib import contextmanager
from fastapi import APIRouter, Request, HTTPException, Form, Depends, Header, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
from pymongo import MongoClient
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config import Config
from chatbot.constants import CHILE_TZ
from chatbot.whatsapp_client import send_whatsapp_message

from services.security_contracts import SecurityContracts
from services.pdf_generator_contracts import PDFGenerator
from services.gdrive_sync import GDriveSync

logger = logging.getLogger("procasa-contracts")
router = APIRouter(prefix="/contracts", tags=["Contracts"])

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

active_signatures_lock = threading.Lock()
ACTIVE_SIGNATURES = 0
MAX_CONCURRENT_SIGNATURES = 100

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

gdrive_sync = GDriveSync()

def get_db():
    client = MongoClient(Config.MONGO_URI)
    return client[Config.DB_NAME]

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0]
    return request.client.host if request.client else "unknown"

@router.post("/api/preview")
async def preview_contract(request: Request):
    """Retorna un PDF generado en caliente para previsualización."""
    try:
        data = await request.json()
        pdf_bytes = PDFGenerator.generate_original_contract(data)
        return Response(content=pdf_bytes, media_type="application/pdf")
    except Exception as e:
        logger.error(f"Error generating preview: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/create")
async def create_contract(request: Request, background_tasks: BackgroundTasks):
    """Crea o actualiza un contrato (desde CRM)"""
    try:
        data = await request.json()
        from chatbot.storage import get_async_db
        adb = get_async_db()
        
        # ── Normalizar campos antes de guardar y generar PDF ─────────────────
        def normalize_fields(d: dict) -> dict:
            # Email → minúsculas
            if d.get("email"):
                d["email"] = d["email"].strip().lower()
            # Dirección → Title Case
            if d.get("propiedad_direccion"):
                d["propiedad_direccion"] = d["propiedad_direccion"].strip().title()
            # Comuna → Title Case
            if d.get("comuna"):
                d["comuna"] = d["comuna"].strip().title()
            # ROL → 00000-000
            if d.get("rol"):
                rol_raw = d["rol"].strip().replace(" ", "")
                if "-" in rol_raw:
                    parts = rol_raw.split("-", 1)
                    manzana = parts[0].zfill(5)[:5]
                    predio  = parts[1].zfill(3)[:3]
                    d["rol"] = f"{manzana}-{predio}"
            # Precio → asegurar UF en mayúsculas
            if d.get("precio"):
                precio = d["precio"].strip()
                precio = precio.replace(" uf", " UF").replace(" Uf", " UF").replace(" uF", " UF")
                if precio and not precio.upper().endswith("UF"):
                    precio = precio + " UF"
                d["precio"] = precio
            # Comisión → agregar % si falta
            if d.get("comision"):
                comision = str(d["comision"]).strip().replace("%", "")
                d["comision"] = comision + "%"
            return d

        data = normalize_fields(data)
        property_code = data.get("property_code", "").strip()
        
        # Verificar si existe contrato previo creado (no firmado)
        existing = None
        if property_code:
            existing = await adb["contracts"].find_one({
                "property_code": property_code, 
                "status": {"$in": ["created", "sent", "opened"]}
            })
            
        if existing:
            if existing.get("status") in ["otp_requested", "otp_verified", "signed"]:
                raise HTTPException(status_code=400, detail="Este contrato ya está en proceso de firma o ha sido firmado. No puede ser modificado.")
            contract_code = existing["contract_code"]
        else:
            year = datetime.now().year
            short_id = str(uuid.uuid4())[:4].upper()
            contract_code = f"PROC-{year}-{short_id}"
        # 1. Preparar rutas (PDF se generar\u00e1 as\u00edncronamente)
        data['contract_code'] = contract_code
        perm_dir = BASE_DIR / "contracts_pdf"
        perm_dir.mkdir(parents=True, exist_ok=True)
        perm_original_path = perm_dir / f"{contract_code}_original.pdf"
            
        server_timestamp = SecurityContracts.generate_server_timestamp()
            
        contract_doc = {
            "contract_code": contract_code,
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
                "tipo": data.get("tipo", "Arriendo"),
                "rol": data.get("rol", ""),
                "vigencia": data.get("vigencia", "30"),
                "precio": data.get("precio", ""),
                "comision": data.get("comision", "")
            },
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
            "created_at_local": datetime.now(CHILE_TZ).strftime('%Y-%m-%d %H:%M:%S')
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
            
            await adb["contracts"].replace_one({"contract_code": contract_code}, contract_doc)
        else:
            await adb["contracts"].insert_one(contract_doc)

        def generate_original_pdf_bg(data_dict, p_code, p_path):
            try:
                from chatbot.storage import get_db
                local_db = get_db()
                pdf_b = PDFGenerator.generate_original_contract(data_dict)
                orig_hash = SecurityContracts.hash_document(pdf_b)
                t_dir = BASE_DIR / "tmp" / "contracts" / p_code
                t_dir.mkdir(parents=True, exist_ok=True)
                with open(t_dir / "contrato_original.pdf", "wb") as f:
                    f.write(pdf_b)
                with open(p_path, "wb") as f:
                    f.write(pdf_b)
                local_db["contracts"].update_one(
                    {"contract_code": p_code},
                    {"$set": {"security.original_hash": orig_hash}}
                )
            except Exception as e:
                logger.error(f"[BG TASK] Error generando original: {e}")

        background_tasks.add_task(generate_original_pdf_bg, data, contract_code, perm_original_path)
            
        url_firma = f"{Config.WEBHOOK_URL}/contracts/view/{contract_code}"
        
        return {
            "status": "success",
            "contract_code": contract_code,
            "url_firma": url_firma
        }
        
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

    # Prioridad 1: ruta permanente guardada en DB
    perm_path_str = contract.get("security", {}).get("original_pdf_path")
    if perm_path_str and os.path.exists(perm_path_str):
        pdf_path = Path(perm_path_str)
    else:
        # Prioridad 2: directorio permanente por convención
        perm_path_conv = BASE_DIR / "contracts_pdf" / f"{contract_code}_original.pdf"
        if perm_path_conv.exists():
            pdf_path = perm_path_conv
        else:
            # Prioridad 3: tmp (efímero)
            tmp_path = BASE_DIR / "tmp" / "contracts" / contract_code / "contrato_original.pdf"
            if tmp_path.exists():
                pdf_path = tmp_path
            else:
                logger.error(f"El documento original para el contrato {contract_code} no se encuentra en caché temporal ni permanente.")
                raise HTTPException(status_code=404, detail="El documento PDF no está disponible. Solo se genera al crear el contrato.")

    prop_code = contract.get('property_code', 'SD')
    tipo_raw = contract.get('property_data', {}).get('tipo', 'Arriendo')
    tipo = tipo_raw.replace(" ", "_")

    from fastapi.responses import StreamingResponse
    import io
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
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
            content_disposition_type="attachment",
            headers={"Cache-Control": "public, max-age=3600"}
        )

    file_id = contract.get("security", {}).get("signed_pdf_drive_id")
    if file_id:
        gdrive = GDriveSync()
        pdf_bytes = gdrive.download_file(file_id)
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
            
    raise HTTPException(status_code=404, detail="Documento firmado no disponible")

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
        gdrive = GDriveSync()
        pdf_bytes = gdrive.download_file(file_id)
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
            
    raise HTTPException(status_code=404, detail="Documento firmado no disponible")

@router.post("/api/{contract_code}/send")
async def send_contract(contract_code: str, request: Request):
    """Genera token y envía por WhatsApp"""
    db = get_db()
    contract = db["contracts"].find_one({"contract_code": contract_code})
    if not contract:
        raise HTTPException(status_code=404, detail="Contrato no encontrado")
        
    # Reusar token si aún es válido
    if contract.get("security", {}).get("token") and not contract["security"]["token_used"]:
        expiry = contract["security"]["token_expiry"]
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < expiry:
            token = contract["security"]["token"]
        else:
            token = str(uuid.uuid4()).replace("-", "")
            expiry = datetime.now(timezone.utc) + timedelta(hours=24)
    else:
        token = str(uuid.uuid4()).replace("-", "")
        expiry = datetime.now(timezone.utc) + timedelta(hours=24)
    
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
    # Usar la base_url de la request actual para que funcione localmente o en prod
    base_url = str(request.base_url).rstrip('/')
    link = f"{base_url}/contracts/view/{token}"
    
    nombre = contract.get('client_data', {}).get('nombre', contract.get('cliente_nombre', ''))
    direccion = contract.get('property_data', {}).get('direccion', contract.get('propiedad_direccion', ''))
    
    mensaje = f"""Hola, este enlace es personal, confidencial e intransferible.

Al acceder y firmar el documento, usted declara ser el titular del número telefónico al que fue enviado este mensaje y acepta el contrato asociado a su propiedad.

Este proceso utiliza firma electrónica conforme a la Ley 19.799.

👉 Ingrese aquí para revisar y firmar:
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

def ensure_document_valid(contract: dict):
    """Verifica la expiración real del documento (24 horas) en todos los endpoints"""
    now = datetime.now(timezone.utc)
    token_expiry = contract["security"].get("token_expiry")
    if token_expiry:
        if token_expiry.tzinfo is None:
            token_expiry = token_expiry.replace(tzinfo=timezone.utc)
        if now > token_expiry:
            raise HTTPException(status_code=410, detail="DOCUMENT_EXPIRED")


@router.get("/view/{token}", response_class=HTMLResponse)
async def view_contract_public(token: str, request: Request):
    """Vista pública para el cliente"""
    db = get_db()
    contract = db["contracts"].find_one({"security.token": token})
    
    if not contract:
        return HTMLResponse("<h1>Enlace inválido o expirado.</h1>", status_code=404)
        
    is_signed = contract["security"].get("token_used", False)
    
    # Solo expira a las 24h si NO está firmado. Si ya se firmó, el acceso es permanente.
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
    
    db["contracts"].update_one(
        {"contract_code": contract["contract_code"]},
        {
            "$push": {
                "access_logs": {"ip": ip, "user_agent": ua, "timestamp": server_timestamp},
                "timeline": {
                    "action": "link_opened",
                    "server_timestamp": server_timestamp,
                    "ip": ip,
                    "user_agent": ua
                }
            },
            "$set": {"status": "opened"}
        }
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
        
    return templates.TemplateResponse("contract_view.html", {
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
    contract = db["contracts"].find_one({"security.token": token})
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
    contract = await adb["contracts"].find_one({"security.token": token})
    if not contract:
        raise HTTPException(status_code=404, detail="Token inv\u00e1lido")
        
    ensure_document_valid(contract)
    
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
    
    mensaje = f"""Tu c\u00f3digo de verificaci\u00f3n para firmar tu contrato es: *{otp}*

Este c\u00f3digo es personal, v\u00e1lido por 5 minutos y no debe compartirse con terceros."""
    
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
    contract = db["contracts"].find_one({"security.token": token})
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
            db["contracts"].update_one({"contract_code": contract["contract_code"]}, update_doc)
            raise HTTPException(status_code=429, detail="OTP_BLOCKED|60")
        else:
            db["contracts"].update_one({"contract_code": contract["contract_code"]}, update_doc)
            remaining = 5 - attempts
            raise HTTPException(status_code=400, detail=f"OTP_INVALID|{remaining}")
        
    # Success
    db["contracts"].update_one(
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
    contract = db["contracts"].find_one({"security.token": token})
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
    contract = db["contracts"].find_one({"security.token": token})
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
    # La expiración de sesión NO invalida el documento (vigencia 24h)
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
    db["contracts"].update_one(
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
    contract = db["contracts"].find_one({"contract_code": contract_code})
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

        evidence_data = {
            "contract_code": contract_code,
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
            "scrolled_to_bottom": "S\u00ed" if scrolled_to_bottom else "No",
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
        db["contracts"].update_one(
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
        db["contracts"].update_one({"contract_code": contract_code}, {"$set": {"security.tsa_stamp": tsa_response}})

        # 6. Generar Informe Legal y Subida a Google Drive en Background
        def finalize_bg_task(c_code, c_doc, e_data, t_line, s_pdf_bytes, o_hash, s_hash, t_hash, s_hmac):
            try:
                # Import din\u00e1mico
                from services.pdf_generator_contracts import PDFGenerator
                l_report_bytes = PDFGenerator.generate_legal_report(c_doc, e_data, t_line)
                
                t_dir = BASE_DIR / "tmp" / "contracts" / c_code
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
    
def upload_to_gdrive_bg(contract_code: str, files: dict):
    """Sube archivos a GDrive recibiendo bytes en memoria, sin depender del filesystem."""
    try:
        folder_id = gdrive_sync.create_folder(f"Expediente_{contract_code}")
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
            db = get_db()
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
    mensaje_conf = """Confirmamos la aceptación electrónica de tu contrato conforme a la Ley 19.799.

Se ha registrado la fecha, hora, dirección IP y verificación de identidad asociada a esta aceptación.

En breve recibirás una copia del documento firmado."""
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
        
        # Destinatarios CC (Desactivado temporalmente para pruebas)
        cc_recipients = [] 
        # cc_recipients = ["jpcaro@procasa.cl"]
        if cc_email and cc_email != email_to and cc_email not in cc_recipients:
            cc_recipients.append(cc_email)
        cc_str = ", ".join(cc_recipients)
        
        all_recipients = [email_to] + cc_recipients

        msg = MIMEMultipart()
        msg["From"] = f"Procasa Sucre <{gmail_user}>"
        msg["To"] = email_to
        msg["Cc"] = cc_str
        msg["Subject"] = asunto

        body = f"""Estimado/a {nombre},

Adjunto encontrará el documento de su convenio de corretaje firmado electrónicamente conforme a la Ley 19.799.

Detalles del convenio:
• Propiedad: {prop_label}
• Código de verificación: {contract_code}

Puede verificar la autenticidad del documento en:
{Config.CRM_BASE_URL}/contracts/verify/{contract_code}

Si tiene alguna duda, no dude en contactarnos.

Saludos,
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
async def delete_contract(contract_code: str):
    """Permite eliminar un contrato lógicamente (soft delete)."""
    db = get_db()
    result = db["contracts"].update_one(
        {"contract_code": contract_code},
        {"$set": {"status": "deleted"}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Contrato no encontrado")
    return {"status": "ok"}

@router.get("/verify/{contract_code}", response_class=HTMLResponse)
async def verify_contract(contract_code: str, request: Request):
    db = get_db()
    contract = db["contracts"].find_one({"contract_code": contract_code})
    if not contract:
        return HTMLResponse("<h1>Contrato no encontrado</h1>", status_code=404)
        
    return templates.TemplateResponse("contract_verify.html", {
        "request": request,
        "contract": contract
    })

@router.get("/dashboard", response_class=HTMLResponse)
async def contract_dashboard(request: Request):
    """Módulo principal para gestión y generación de convenios de corretaje"""
    from chatbot.storage import get_async_db
    adb = get_async_db()
    # Listar los \u00faltimos contratos (excluir los eliminados o manejarlos en frontend)
    contracts_cursor = adb["contracts"].find({"status": {"$ne": "deleted"}}).sort("created_at", -1).limit(100)
    contracts = await contracts_cursor.to_list(length=100)
    
    for c in contracts:
        if c.get("created_at"):
            # PyMongo returns naive UTC, convert to CHILE_TZ
            dt_utc = c["created_at"].replace(tzinfo=timezone.utc)
            c["created_at"] = dt_utc.astimezone(CHILE_TZ)
        
    return templates.TemplateResponse("contract_dashboard.html", {
        "request": request,
        "contracts": contracts,
        "user_role": "admin" # O tomar de la sesión si es necesario
    })
