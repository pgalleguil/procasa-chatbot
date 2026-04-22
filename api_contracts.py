import os
import uuid
import logging
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
            contract_code = existing["contract_code"]
        else:
            contract_code = str(uuid.uuid4())
        
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
        raise HTTPException(status_code=404, detail="PDF no encontrado o ya fue eliminado del servidor")
        
    from fastapi.responses import FileResponse
    return FileResponse(
        path=pdf_path, 
        filename=f"Convenio_{contract_code[:8]}.pdf",
        media_type="application/pdf",
        content_disposition_type="inline"
    )

@router.get("/api/download_signed/{contract_code}")
async def download_signed_pdf(contract_code: str):
    """Permite descargar o ver el PDF firmado"""
    tmp_dir = BASE_DIR / "tmp" / "contracts" / contract_code
    pdf_path = tmp_dir / "contrato_firmado.pdf"
    
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF firmado no encontrado")
        
    from fastapi.responses import FileResponse
    return FileResponse(
        path=pdf_path, 
        filename=f"Convenio_Firmado_{contract_code[:8]}.pdf",
        media_type="application/pdf",
        content_disposition_type="inline"
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
    
    mensaje = f"Hola {contract['client_data']['nombre']},\n\nAquí tienes tu contrato de corretaje ({contract['property_data']['tipo']}) para la propiedad en {contract['property_data']['direccion']}.\n\nPor favor, revísalo y fírmalo electrónicamente ingresando a este enlace (válido por 24 horas):\n{link}"
    
    await send_whatsapp_message(phone, mensaje)
    return {"status": "ok", "message": "Enviado por WhatsApp"}


@router.get("/view/{token}", response_class=HTMLResponse)
async def view_contract_public(token: str, request: Request):
    """Vista pública para el cliente"""
    db = get_db()
    contract = db["contracts"].find_one({"security.token": token})
    
    if not contract:
        return HTMLResponse("<h1>Enlace inválido o expirado.</h1>", status_code=404)
        
    # Verificar expiración y uso
    expiry = contract["security"]["token_expiry"]
    # Compatibilidad con datetimes aware/naive
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
        
    if datetime.now(timezone.utc) > expiry or contract["security"]["token_used"]:
        return HTMLResponse("<h1>Este enlace ya ha sido utilizado o ha expirado.</h1>", status_code=403)
        
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
    
    db = get_db()
    contract = db["contracts"].find_one({"security.token": token})
    if not contract:
        raise HTTPException(status_code=404, detail="Token inválido")
        
    # Validar RUT de forma simple (en producción usar validador real)
    if rut_ingresado.replace(".", "").replace("-", "").upper() != contract["client_data"]["rut"].replace(".", "").replace("-", "").upper():
        raise HTTPException(status_code=400, detail="RUT no coincide con el registrado.")
        
    otp = SecurityContracts.generate_otp(6)
    otp_expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    
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
                "security.otp_attempts": 0
            },
            "$push": {
                "timeline": {
                    "action": "otp_requested",
                    "server_timestamp": server_timestamp,
                    "ip": ip,
                    "user_agent": ua
                }
            }
        }
    )
    
    mensaje = f"Tu código de verificación para firmar el contrato Procasa es: *{otp}*.\nVálido por 5 minutos."
    await send_whatsapp_message(contract["phone"], mensaje)
    
    return {"status": "ok"}

@router.post("/api/{token}/verify-otp")
async def verify_otp(token: str, request: Request):
    data = await request.json()
    otp_ingresado = data.get("otp", "").strip()
    
    db = get_db()
    contract = db["contracts"].find_one({"security.token": token})
    if not contract:
        raise HTTPException(status_code=404)
        
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    server_timestamp = SecurityContracts.generate_server_timestamp()
    
    # Check attempts
    if contract["security"]["otp_attempts"] >= 3:
        # Registrar fallo crítico
        db["contracts"].update_one(
            {"contract_code": contract["contract_code"]},
            {"$push": {"timeline": {"action": "otp_blocked_max_attempts", "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua}}}
        )
        raise HTTPException(status_code=403, detail="Máximo de intentos alcanzado. Solicita un nuevo código.")
        
    # Check expiry
    expiry = contract["security"]["otp_expiry"]
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expiry:
        raise HTTPException(status_code=400, detail="Código expirado.")
        
    # Validate
    if otp_ingresado != contract["security"]["otp"]:
        db["contracts"].update_one(
            {"contract_code": contract["contract_code"]},
            {
                "$inc": {"security.otp_attempts": 1},
                "$push": {"timeline": {"action": "otp_failed_attempt", "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua, "details": "OTP incorrecto"}}
            }
        )
        raise HTTPException(status_code=400, detail="Código incorrecto.")
        
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

@router.post("/api/{token}/accept")
async def accept_contract(token: str, request: Request, background_tasks: BackgroundTasks):
    db = get_db()
    contract = db["contracts"].find_one({"security.token": token})
    if not contract or contract["status"] != "otp_verified" or contract["security"]["token_used"]:
        raise HTTPException(status_code=403, detail="Contrato no válido para aceptación")
        
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    server_timestamp = SecurityContracts.generate_server_timestamp()
    contract_code = contract["contract_code"]
    
    # 1. Registrar aceptación
    db["contracts"].update_one(
        {"contract_code": contract_code},
        {
            "$set": {
                "status": "accepted", 
                "security.token_used": True # Invalidar Token inmediatamente
            },
            "$push": {"timeline": {"action": "contract_accepted", "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua}}
        }
    )
    
    # Refrescar documento para tener el timeline completo
    contract = db["contracts"].find_one({"contract_code": contract_code})
    timeline = contract["timeline"]
    
    # 2. Generar Firma HMAC del Servidor y Hash del Timeline
    timeline_hash = SecurityContracts.hash_timeline(timeline)
    # Re-leer el PDF original guardado en tmp (o regenerarlo)
    tmp_dir = BASE_DIR / "tmp" / "contracts" / contract_code
    original_pdf_path = tmp_dir / "contrato_original.pdf"
    if original_pdf_path.exists():
        with open(original_pdf_path, "rb") as f:
            original_bytes = f.read()
    else:
        # Fallback regenerando (los hashes pueden variar si cambian timestamps internos, idealmente se usa el archivo original guardado)
        original_bytes = PDFGenerator.generate_original_contract(contract)
        
    original_hash = SecurityContracts.hash_document(original_bytes)
    
    secret_key = getattr(Config, "SECRET_KEY", "default_secret")
    server_hmac = SecurityContracts.generate_server_hmac(contract_code, original_hash, server_timestamp, secret_key)
    
    base_url = str(request.base_url).rstrip('/')
    verify_url = f"{base_url}/contracts/verify/{contract_code}"
    
    evidence_data = {
        "contract_code": contract_code,
        "server_timestamp": server_timestamp,
        "ip": ip,
        "original_hash": original_hash,
        "server_hmac": server_hmac,
        "timeline_hash": timeline_hash
    }
    
    # 3. Generar PDF Firmado
    signed_pdf_bytes = PDFGenerator.generate_signed_contract(original_bytes, evidence_data, verify_url)
    signed_hash = SecurityContracts.hash_document(signed_pdf_bytes)
    evidence_data["signed_hash_placeholder"] = signed_hash # En un flujo de 2 pasos esto se actualiza, acá para prototipo lo dejamos así
    
    # Guardar en tmp
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
        {
            "$set": {
                "security.signed_hash": signed_hash,
                "security.server_hmac": server_hmac,
                "security.timeline_hash": timeline_hash
            }
        }
    )
    
    # 6. Subida a Google Drive en Background
    background_tasks.add_task(sync_to_gdrive_task, contract_code, tmp_dir)
    
    # 7. Notificar a CRM o Agente
    await send_whatsapp_message(contract["phone"], "¡Gracias! Hemos recibido tu aceptación conforme a la Ley 19.799. En breve un agente se comunicará contigo.")
    
    return {"status": "ok", "contract_code": contract_code}
    
def sync_to_gdrive_task(contract_code: str, tmp_dir: Path):
    try:
        folder_id = gdrive_sync.create_folder(f"Expediente_{contract_code}")
        for file_path in tmp_dir.glob("*.*"):
            with open(file_path, "rb") as f:
                content = f.read()
                mime = "application/pdf" if file_path.suffix == ".pdf" else "text/plain"
                gdrive_sync.upload_file(folder_id, file_path.name, content, mime)
        logger.info(f"Expediente {contract_code} subido a GDrive exitosamente.")
    except Exception as e:
        logger.error(f"Error en tarea background GDrive: {e}")

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
