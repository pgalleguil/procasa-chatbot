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
async def create_contract(request: Request):
    """Crea o actualiza un contrato (desde CRM)"""
    try:
        data = await request.json()
        db = get_db()
        
        property_code = data.get("property_code", "").strip()
        
        # Verificar si existe contrato previo creado (no firmado) para evitar duplicados si tiene el mismo property_code
        existing = None
        if property_code:
            existing = db["contracts"].find_one({
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
        
        # 1. Generar PDF original
        data['contract_code'] = contract_code
        pdf_bytes = PDFGenerator.generate_original_contract(data)
        original_hash = SecurityContracts.hash_document(pdf_bytes)
        
        # Guardar archivo temporal (en Render se perderá, pero GDrive es el respaldo)
        tmp_dir = BASE_DIR / "tmp" / "contracts" / contract_code
        tmp_dir.mkdir(parents=True, exist_ok=True)
        with open(tmp_dir / "contrato_original.pdf", "wb") as f:
            f.write(pdf_bytes)
            
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
                "original_hash": original_hash,
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
            # Preservar tokens y estado
            contract_doc["security"] = existing.get("security", contract_doc["security"])
            contract_doc["status"] = existing.get("status", "created")
            
            db["contracts"].replace_one({"contract_code": contract_code}, contract_doc)
        else:
            db["contracts"].insert_one(contract_doc)
            
        return {"status": "ok", "contract_code": contract_code}
        
    except Exception as e:
        logger.error(f"Error creating contract: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/download/{contract_code}")
async def download_original_pdf(contract_code: str):
    """Permite descargar o ver el PDF generado antes de enviarlo"""
    tmp_dir = BASE_DIR / "tmp" / "contracts" / contract_code
    pdf_path = tmp_dir / "contrato_original.pdf"
    
    if not pdf_path.exists():
        db = get_db()
        contract = db["contracts"].find_one({"contract_code": contract_code})
        if not contract:
            raise HTTPException(status_code=404, detail="Contrato no encontrado")
            
        try:
            from services.pdf_generator_contracts import PDFGenerator
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
            pdf_bytes = PDFGenerator.generate_original_contract(data_payload)
            tmp_dir.mkdir(parents=True, exist_ok=True)
            with open(pdf_path, "wb") as f:
                f.write(pdf_bytes)
        except Exception as e:
            logger.error(f"Error regenerando PDF: {e}")
            raise HTTPException(status_code=500, detail="Error al regenerar el documento PDF")

    db = get_db()
    contract = db["contracts"].find_one({"contract_code": contract_code})
    prop_code = contract.get('property_code', 'SD') if contract else 'SD'
    
    from fastapi.responses import StreamingResponse
    import io
    
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
        
    tipo_raw = contract.get('property_data', {}).get('tipo', 'Arriendo') if contract else 'Arriendo'
    tipo = tipo_raw.replace(" ", "_")
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Disposition": f"inline; filename=Contrato_Autorizacion_{tipo}_{prop_code}_{contract_code}.pdf"
        }
    )

@router.get("/api/download_signed/{contract_code}")
async def download_signed_pdf(contract_code: str):
    """Permite descargar o ver el PDF firmado"""
    db = get_db()
    contract = db["contracts"].find_one({"contract_code": contract_code})
    if not contract:
        raise HTTPException(status_code=404, detail="Contrato no encontrado")

    tmp_dir = BASE_DIR / "tmp" / "contracts" / contract_code
    pdf_path = tmp_dir / "contrato_firmado.pdf"
    
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF firmado no encontrado")
        
    prop_code = contract.get('property_code', 'SD')
    tipo_raw = contract.get('property_data', {}).get('tipo', 'Arriendo')
    tipo = tipo_raw.replace(" ", "_")
    filename = f"Contrato_Autorizacion_{tipo}_{prop_code}_{contract_code}.pdf"
    
    from fastapi.responses import FileResponse
    return FileResponse(
        path=pdf_path,
        filename=filename,
        media_type="application/pdf",
        content_disposition_type="inline",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Accept-Ranges": "bytes"
        }
    )

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
    
    mensaje = f"""Hola {nombre},

Te enviamos tu contrato de corretaje para la propiedad {direccion}.

Para revisarlo y aceptarlo de forma electrónica, accede al siguiente enlace (válido por 24 horas):

{link}

Este proceso incluye verificación de identidad para tu seguridad."""
    
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
        
    # Verificar expiración (24h)
    try:
        ensure_document_valid(contract)
    except HTTPException:
        return HTMLResponse("<h1>Enlace inválido o expirado.</h1>", status_code=404)
        
    if contract["security"].get("token_used"):
        return HTMLResponse("<h1>Este enlace ya ha sido utilizado o ha expirado.</h1>", status_code=403)
        
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
    
    return templates.TemplateResponse("contract_view.html", {
        "request": request,
        "contract": contract,
        "token": token
    })

@router.post("/api/{token}/request-otp")
async def request_otp(token: str, request: Request):
    """Genera OTP y lo envía por WA"""
    data = await request.json()
    rut_ingresado = data.get("rut", "").strip()
    
    ip = get_client_ip(request)
    check_rate_limit(ip, otp_rate_limit, 5, window_seconds=60)
    
    db = get_db()
    contract = db["contracts"].find_one({"security.token": token})
    if not contract:
        raise HTTPException(status_code=404, detail="Token inválido")
        
    ensure_document_valid(contract)
    
    if contract.get("status") == "signed" or contract["security"].get("token_used"):
        return JSONResponse(status_code=200, content={"status": "already_signed"})
        
    # Validar RUT de forma simple (en producción usar validador real)
    if rut_ingresado.replace(".", "").replace("-", "").upper() != contract["client_data"]["rut"].replace(".", "").replace("-", "").upper():
        raise HTTPException(status_code=400, detail="RUT no coincide con el registrado.")
        
    # Rate limiting: bloquear si se solicitó OTP hace menos de 30 segundos
    now = datetime.now(timezone.utc)
    last_request = contract["security"].get("last_otp_request")
    if last_request:
        if last_request.tzinfo is None:
            last_request = last_request.replace(tzinfo=timezone.utc)
        seconds_elapsed = (now - last_request).total_seconds()
        if seconds_elapsed < 30:
            raise HTTPException(status_code=429, detail="Debes esperar unos segundos antes de solicitar un nuevo código.")

    otp = SecurityContracts.generate_otp(6)
    otp_expiry = now + timedelta(minutes=5)
    
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    server_timestamp = SecurityContracts.generate_server_timestamp()
    
    db["contracts"].update_one(
        {"contract_code": contract["contract_code"]},
        {
            "$set": {
                "status": "otp_requested",
                "security.otp": otp,
                "security.otp_expiry": otp_expiry,
                "security.otp_attempts": 0,
                "security.last_otp_request": now
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
    
    mensaje = f"""Tu código de verificación para firmar tu contrato es: *{otp}*

Este código es personal, válido por 5 minutos y no debe compartirse con terceros."""
    
    # Guardar mensaje OTP dentro del mismo documento del contrato
    try:
        db["contracts"].update_one(
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
    
    try:
        await send_whatsapp_circuit_breaker(contract["phone"], mensaje)
    except Exception as e:
        logger.error(f"[OTP_FAILED] Error enviando WhatsApp al {contract['phone']}: {e}")
        db["contracts"].update_one(
            {"contract_code": contract["contract_code"]},
            {"$push": {"timeline": {"action": "otp_delivery_failed", "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua, "error": str(e)}}}
        )
        raise HTTPException(status_code=500, detail="Error enviando el código por WhatsApp. Intente nuevamente.")
    
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
        
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    server_timestamp = SecurityContracts.generate_server_timestamp()
    
    # Check attempts
    if contract["security"].get("otp_attempts", 0) >= 5:
        # Registrar fallo crítico
        db["contracts"].update_one(
            {"contract_code": contract["contract_code"]},
            {"$push": {"timeline": {"action": "otp_blocked_max_attempts", "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua}}}
        )
        raise HTTPException(status_code=429, detail="TOO_MANY_OTP_ATTEMPTS")
        
    # Check expiry — NO resetear estado para no perder el contrato, solo devolver error para que Frontend mande al paso 2
    expiry = contract["security"]["otp_expiry"]
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expiry:
        logger.info(f"[USER_RETURNED_TO_STEP2] contract_code={contract['contract_code']} reason=OTP_EXPIRED timestamp={datetime.now(timezone.utc)}")
        raise HTTPException(status_code=400, detail="OTP_EXPIRED")
        
    # Validate
    if otp_ingresado != contract["security"]["otp"]:
        db["contracts"].update_one(
            {"contract_code": contract["contract_code"]},
            {
                "$inc": {"security.otp_attempts": 1},
                "$push": {"timeline": {"action": "otp_failed_attempt", "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua, "details": "OTP incorrecto"}}
            }
        )
        raise HTTPException(status_code=400, detail="OTP_INVALID")
        
    # Success
    db["contracts"].update_one(
        {"contract_code": contract["contract_code"]},
        {
            "$set": {"status": "otp_verified", "security.otp": None}, # Invalidate OTP
            "$push": {"timeline": {"action": "otp_verified", "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua}}
        }
    )
    return {"status": "ok"}

@router.post("/api/{token}/legal_intent")
async def register_legal_intent(token: str, request: Request):
    db = get_db()
    contract = db["contracts"].find_one({"security.token": token})
    if not contract: return {"status": "error"}
    
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    server_timestamp = SecurityContracts.generate_server_timestamp()
    
    db["contracts"].update_one(
        {"contract_code": contract["contract_code"]},
        {"$push": {"timeline": {"action": "explicit_legal_intent_checked", "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua}}}
    )
    return {"status": "ok"}

@router.post("/api/{token}/legal_intent")
async def legal_intent(token: str, request: Request):
    db = get_db()
    contract = db["contracts"].find_one({"security.token": token})
    if not contract:
        raise HTTPException(status_code=404)
        
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    server_timestamp = SecurityContracts.generate_server_timestamp()
    
    db["contracts"].update_one(
        {"contract_code": contract["contract_code"]},
        {"$push": {
            "timeline": {
                "action": "legal_intent_confirmed",
                "server_timestamp": server_timestamp,
                "ip": ip,
                "user_agent": ua
            }
        }}
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
    except:
        read_time = 0
        scrolled_to_bottom = False
        
    ip = get_client_ip(request)
    # Geolocalización simple
    import requests
    try:
        geo_res = requests.get(f"https://ipapi.co/{ip}/json/", timeout=2).json()
        geo_info = f"{geo_res.get('city', 'Desconocido')}, {geo_res.get('country_name', 'Desconocido')}"
    except:
        geo_info = "Localización no disponible"
        
    ua = request.headers.get("user-agent", "")
    server_timestamp = SecurityContracts.generate_server_timestamp()
    contract_code = contract["contract_code"]
    
    # 1. Registrar aceptación
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
                "user_agent": ua,
                "read_time_seconds": read_time,
                "scrolled_to_bottom": scrolled_to_bottom
            }}
        }
    )
    
    logger.info(f"[METRIC] contracts_signed: {contract_code}")
    
    # Refrescar documento para tener el timeline completo
    contract = db["contracts"].find_one({"contract_code": contract_code})
    timeline = contract["timeline"]
    
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
        verify_url = f"{base_url}/contracts/verify/{contract_code}"

        evidence_data = {
            "contract_code": contract_code,
            "server_timestamp": server_timestamp,
            "ip": ip,
            "geo_info": geo_info,
            "original_hash": original_hash,
            "server_hmac": server_hmac,
            "timeline_hash": timeline_hash,
            "read_time_seconds": read_time,
            "scrolled_to_bottom": "Sí" if scrolled_to_bottom else "No"
        }

        # 3. Generar PDF Firmado Completo
        signed_pdf_bytes = PDFGenerator.generate_signed_contract(contract, evidence_data, verify_url)
        signed_hash = SecurityContracts.hash_document(signed_pdf_bytes)

        # Guardar archivos en tmp — garantizar directorio (crítico en Render)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        with open(tmp_dir / "contrato_firmado.pdf", "wb") as f:
            f.write(signed_pdf_bytes)

        # 4. Generar Informe Legal
        legal_report_bytes = PDFGenerator.generate_legal_report(contract, evidence_data, timeline)
        with open(tmp_dir / "informe_legal.pdf", "wb") as f:
            f.write(legal_report_bytes)

        import json
        with open(tmp_dir / "hash.txt", "w") as f:
            f.write(f"Original Hash: {original_hash}\nSigned Hash: {signed_hash}\nTimeline Hash: {timeline_hash}\nHMAC: {server_hmac}")
        with open(tmp_dir / "timeline.json", "w") as f:
            json.dump(timeline, f, indent=4)

        # 5. Guardar Hashes finales en DB
        db["contracts"].update_one(
            {"contract_code": contract_code},
            {"$set": {
                "security.signed_hash": signed_hash,
                "security.server_hmac": server_hmac,
                "security.timeline_hash": timeline_hash
            }}
        )

        # TSA mock
        tsa_response = f"TSA_MOCK_{datetime.now(timezone.utc).timestamp()}_SIGNED"
        db["contracts"].update_one({"contract_code": contract_code}, {"$set": {"security.tsa_stamp": tsa_response}})

        # 6. Subida a Google Drive en Background — pasar bytes, no Path
        background_tasks.add_task(
            upload_to_gdrive_bg,
            contract_code,
            {
                "contrato_firmado.pdf": signed_pdf_bytes,
                "informe_legal.pdf": legal_report_bytes,
                "hash.txt": f"Original Hash: {original_hash}\nSigned Hash: {signed_hash}\nTimeline Hash: {timeline_hash}\nHMAC: {server_hmac}".encode(),
            }
        )

        # 7. Notificar al Cliente — exactly-once delivery
        if not contract.get("notifications_sent"):
            mensaje_conf = """Confirmamos la aceptación electrónica de tu contrato conforme a la Ley 19.799.

Se ha registrado la fecha, hora, dirección IP y verificación de identidad asociada a esta aceptación.

En breve recibirás una copia del documento firmado."""
            try:
                db["contracts"].update_one(
                    {"contract_code": contract_code},
                    {
                        "$push": {"messages": {
                            "phone": contract["phone"],
                            "message_content": mensaje_conf,
                            "message_type": "confirmation_sent",
                            "timestamp_utc": datetime.now(timezone.utc)
                        }},
                        "$set": {"notifications_sent": True}
                    }
                )
            except Exception as e:
                logger.error(f"[MSG_LOG] Error guardando mensaje confirmación: {e}")
            await send_whatsapp_message(contract["phone"], mensaje_conf)

            # 8. Enviar PDF firmado por correo en Background — pasar bytes, no Path
            client_email = contract.get("client_data", {}).get("email", contract.get("email", ""))
            if client_email:
                background_tasks.add_task(
                    send_signed_email_task,
                    contract_code,
                    client_email,
                    contract.get("client_data", {}).get("nombre", ""),
                    signed_pdf_bytes,          # bytes, no Path
                    contract.get("property_code", "")
                )
        else:
            logger.info(f"[CONTRACT_SIGNED] Notificaciones ya enviadas para {contract_code} — saltando.")

        # Log de auditoría legal estructurado
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
    db = get_db()
    # Listar los últimos contratos (excluir los eliminados o manejarlos en frontend)
    contracts_cursor = db["contracts"].find({"status": {"$ne": "deleted"}}).sort("created_at", -1).limit(100)
    contracts = []
    for c in contracts_cursor:
        if c.get("created_at"):
            # PyMongo returns naive UTC, convert to CHILE_TZ
            dt_utc = c["created_at"].replace(tzinfo=timezone.utc)
            c["created_at"] = dt_utc.astimezone(CHILE_TZ)
        contracts.append(c)
        
    return templates.TemplateResponse("contract_dashboard.html", {
        "request": request,
        "contracts": contracts,
        "user_role": "admin" # O tomar de la sesión si es necesario
    })
