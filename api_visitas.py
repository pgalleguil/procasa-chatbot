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
from chatbot.constants import CHILE_TZ
from chatbot.property_lookup import PROPERTY_COLLECTION_NAME
from chatbot.whatsapp_client import send_whatsapp_message

from services.security_contracts import SecurityContracts
from services.pdf_generator_visitas import PDFGeneratorVisitas as PDFGenerator
from services.gdrive_sync import GDriveSync, expedition_folder_name

logger = logging.getLogger("procasa-visitas")
router = APIRouter(prefix="/visitas", tags=["Visitas"])

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
        await _db_call(db.command, "ping")
    except Exception as e:
        logger.error(f"[SERVER_ERROR] Healthcheck DB failed: {e}")
        status_dict["db"] = "failed"
        status_dict["status"] = "error"
    
    if psutil.virtual_memory().percent > 95:
        logger.warning("[SERVER_ERROR] High memory usage detected")
        status_dict["memory"] = "warning"
        
    return JSONResponse(status_dict, status_code=200 if status_dict["status"] == "ok" else 503)

gdrive_sync = GDriveSync(parent_folder_id=Config.GDRIVE_VISITAS_FOLDER_ID)
_VISITAS_DB_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="visitas_db")


async def _db_call(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_VISITAS_DB_EXECUTOR, lambda: fn(*args, **kwargs))

async def _run_blocking(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_VISITAS_DB_EXECUTOR, lambda: fn(*args, **kwargs))

def get_db():
    client = MongoClient(Config.MONGO_URI)
    return client[Config.DB_NAME]

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0]
    return request.client.host if request.client else "unknown"

async def _get_request_user(adb, request: Request):
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
            user_doc = await adb["usuarios"].find_one({"username": username})
            if user_doc:
                user_role = user_doc.get("rol", "agente")
    except Exception as e:
        logger.error(f"Error decodificando JWT: {e}")
    return username, user_role

def _normalize_visita_fields(d: dict) -> dict:
    """Minimal normalization for visita order form data."""
    if d.get("email"):
        d["email"] = d["email"].strip().lower()
    if d.get("cliente_nombre"):
        d["cliente_nombre"] = d["cliente_nombre"].strip()
    if d.get("cliente_rut"):
        d["cliente_rut"] = d["cliente_rut"].strip()
    if d.get("cliente_direccion"):
        d["cliente_direccion"] = d["cliente_direccion"].strip()
    if d.get("cliente_comuna"):
        d["cliente_comuna"] = d["cliente_comuna"].strip().title()
    if d.get("property_comuna"):
        d["property_comuna"] = d["property_comuna"].strip().title()
    if d.get("property_region"):
        d["property_region"] = d["property_region"].strip().title()
    return d


async def _enrich_with_property_data(data: dict) -> dict:
    prop_code = data.get("property_code", "").strip()
    if prop_code:
        try:
            from chatbot.storage import get_async_db
            from chatbot.property_lookup import (
                build_property_lookup_queries,
                get_prop_location,
                get_prop_operation
            )
            adb = get_async_db()
            
            prop_data = None
            for query in build_property_lookup_queries(prop_code):
                prop_data = await adb[PROPERTY_COLLECTION_NAME].find_one(query)
                if prop_data:
                    break
                    
            if prop_data:
                data["_property_found"] = True
                
                loc = get_prop_location(prop_data)
                data["property_comuna"] = loc.get("comuna", "")
                data["property_region"] = loc.get("region", "")
                
                op = get_prop_operation(prop_data)
                data["property_tipo"] = op.get("tipo", "")
                
                resumen = prop_data.get("resumen", {}) or {}
                precio_clp = op.get("precio_clp") or resumen.get("precio_clp") or prop_data.get("precio", "")
                precio_uf = op.get("precio_uf") or resumen.get("precio_uf") or prop_data.get("precio_uf", "")
                
                if precio_clp:
                    try:
                        precio_int = int(float(str(precio_clp).replace(",",".").replace(" ","")))
                        data["precio"] = f"${precio_int:,}".replace(",",".")
                    except:
                        data["precio"] = str(precio_clp)
                elif precio_uf:
                    try:
                        precio_float = float(str(precio_uf).replace(",",".").replace(" ",""))
                        if precio_float.is_integer():
                            data["precio"] = f"{int(precio_float):,} UF".replace(",",".")
                        else:
                            data["precio"] = f"{precio_float:,.2f} UF".replace(",",".")
                    except:
                        data["precio"] = f"{precio_uf} UF"
                else:
                    data["precio"] = ""
                data["operacion"] = op.get("operacion", "")
            else:
                data["_property_found"] = False
        except Exception as e:
            logger.warning(f"[ENRICH] Error enriqueciendo propiedad {prop_code}: {e}")
            data["_property_found"] = False
    return data

@router.get("/api/property-lookup/{code}")
async def property_lookup_api(code: str):
    """
    Looks up property details to feed into the frontend form.
    """
    try:
        data = {"property_code": code}
        enriched = await _enrich_with_property_data(data)
        if enriched.get("_property_found"):
            return {
                "success": True,
                "data": {
                    "comuna": enriched.get("property_comuna", ""),
                    "region": enriched.get("property_region", ""),
                    "tipo": enriched.get("property_tipo", ""),
                    "precio": enriched.get("precio", ""),
                    "operacion": enriched.get("operacion", "")
                }
            }
        return {"success": False, "error": "Property not found"}
    except Exception as e:
        logger.error(f"Error looking up property {code}: {e}")
        return {"success": False, "error": str(e)}

@router.post("/api/preview")
async def preview_contract(request: Request):
    """Retorna un PDF generado en caliente para previsualización."""
    try:
        data = _normalize_visita_fields(await request.json())
        # Enriquecer con datos de propiedad si el código existe en la BD
        # El preview nunca bloquea: si la propiedad no existe, simplemente no muestra sus datos
        data = await _enrich_with_property_data(data)
        data.pop("_property_found", None)  # No bloquear en preview
        
        from chatbot.storage import get_async_db
        adb = get_async_db()
        created_by, user_role = await _get_request_user(adb, request)
        user_doc = await adb["usuarios"].find_one({"username": created_by}) if created_by else None
        if user_doc:
            data["ejecutivo_nombre"] = user_doc.get("nombre", created_by)
            data["ejecutivo_email"] = user_doc.get("email", created_by)
            data["ejecutivo_telefono"] = user_doc.get("phone") or user_doc.get("telefono", "")
        else:
            data["ejecutivo_nombre"] = created_by or ""
            data["ejecutivo_email"] = ""
            data["ejecutivo_telefono"] = ""

        pdf_bytes = await _run_blocking(PDFGenerator.generate_original_contract, data)
        return Response(content=pdf_bytes, media_type="application/pdf")
    except Exception as e:
        logger.error(f"Error generating preview: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/create")
async def create_contract(request: Request, background_tasks: BackgroundTasks):
    """Crea o actualiza un orden de visita (desde CRM)"""
    try:
        data = _normalize_visita_fields(await request.json())
        data = await _enrich_with_property_data(data)
        if data.get("_property_found") is False:
            raise HTTPException(status_code=400, detail="El código de propiedad ingresado no existe en nuestra base de datos. Por favor verifique e intente nuevamente.")
        
        from chatbot.storage import get_async_db
        adb = get_async_db()
        # Extraer usuario/rol desde JWT
        created_by, user_role = await _get_request_user(adb, request)
        # El emisor del orden de visita siempre es el usuario autenticado.
        executive = created_by or ""
        
        user_doc = await adb["usuarios"].find_one({"username": created_by}) if created_by else None
        if user_doc:
            data["ejecutivo_nombre"] = user_doc.get("nombre", created_by)
            data["ejecutivo_email"] = user_doc.get("email", created_by)
            data["ejecutivo_telefono"] = user_doc.get("phone") or user_doc.get("telefono", "")
        else:
            data["ejecutivo_nombre"] = created_by or ""
            data["ejecutivo_email"] = ""
            data["ejecutivo_telefono"] = ""

        property_code = data.get("property_code", "").strip()
        
        # Verificar si existe orden de visita previo creado (no firmado)
        existing = None
        if property_code:
            existing = await adb["visitas"].find_one({
                "property_code": property_code, 
                "status": {"$in": ["created", "sent", "opened"]}
            })
            
        if existing:
            if existing.get("status") in ["otp_requested", "otp_verified", "signed"]:
                raise HTTPException(status_code=400, detail="Este orden de visita ya está en proceso de firma o ha sido firmado. No puede ser modificado.")
            visita_code = existing["visita_code"]
        else:
            year = datetime.now().year
            short_id = str(uuid.uuid4())[:4].upper()
            visita_code = f"VIS-{year}-{short_id}"
        # 1. Preparar rutas (PDF se generar\u00e1 as\u00edncronamente)
        data['visita_code'] = visita_code
        perm_dir = BASE_DIR / "visitas_pdf"
        perm_dir.mkdir(parents=True, exist_ok=True)
        perm_original_path = perm_dir / f"{visita_code}_original.pdf"
            
        server_timestamp = SecurityContracts.generate_server_timestamp()
            
        visita_doc = {
            "visita_code": visita_code,
            "message_domain": "document_signature",
            "message_type": "visit_order",
            "recipient_role": "client",
            "state_source": "visitas",
            "responsible_service": "document_signature_delivery",
            "idempotency_key": f"document_signature:{visita_code}:{data.get('phone', '')}",
            "origen": data.get("origen", "CRM"),
            "property_code": property_code,
            "phone": data.get("phone", ""),
            "client_data": {
                "nombre": data.get("cliente_nombre", ""),
                "rut": data.get("cliente_rut", ""),
                "email": data.get("email", ""),
                "direccion": data.get("cliente_direccion", ""),
                "comuna": data.get("cliente_comuna", "")
            },
            "property_data": {
                "property_code": property_code,
                "comuna": data.get("property_comuna", ""),
                "region": data.get("property_region", ""),
                "tipo": data.get("property_tipo", ""),
                "precio": data.get("precio", ""),
                "operacion": data.get("operacion", "")
            },
            "status": "created",
            "executive_data": {
                "nombre": data.get("ejecutivo_nombre", ""),
                "email": data.get("ejecutivo_email", ""),
                "telefono": data.get("ejecutivo_telefono", "")
            },
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
                    "action": "visita_created",
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
            visita_doc["version"] = existing.get("version", 1) + 1
            visita_doc["timeline"] = existing.get("timeline", []) + visita_doc["timeline"]
            old_security = existing.get("security", {})
            old_security["original_hash"] = None
            old_security["original_pdf_path"] = str(perm_original_path)
            visita_doc["security"] = old_security

            prev_status = existing.get("status", "created")
            if prev_status in ["sent", "opened"]:
                # Re-emisión: la orden ya fue enviada al cliente antes. Al crearla
                # nuevamente se reinicia como borrador para poder reenviarla con un
                # token nuevo y se actualiza la fecha a hoy, en lugar de conservar
                # el estado/fecha antiguo (que bloqueaba el reenvío y mostraba una
                # fecha equivocada en la tabla).
                visita_doc["status"] = "created"
                reissue_now = datetime.now(CHILE_TZ)
                visita_doc["created_at"] = reissue_now
                visita_doc["created_at_local"] = reissue_now.strftime('%Y-%m-%d %H:%M:%S')
                old_security["token"] = None
                old_security["token_expiry"] = None
                old_security["token_used"] = False
                old_security["otp"] = None
                old_security["otp_expiry"] = None
                old_security["otp_attempts"] = 0
            else:
                # Borrador aún no enviado: se mantiene la fecha original de creación.
                visita_doc["status"] = "created"
                visita_doc["created_at"] = existing.get("created_at")
                visita_doc["created_at_local"] = existing.get("created_at_local") or datetime.now(CHILE_TZ).strftime('%Y-%m-%d %H:%M:%S')

            await adb["visitas"].replace_one({"visita_code": visita_code}, visita_doc)
        else:
            await adb["visitas"].insert_one(visita_doc)

        def generate_original_pdf_bg(data_dict, p_code, p_path):
            try:
                from chatbot.storage import get_db
                local_db = get_db()
                pdf_b = PDFGenerator.generate_original_contract(data_dict)
                orig_hash = SecurityContracts.hash_document(pdf_b)
                t_dir = BASE_DIR / "tmp" / "visitas" / p_code
                t_dir.mkdir(parents=True, exist_ok=True)
                with open(t_dir / "orden de visita_original.pdf", "wb") as f:
                    f.write(pdf_b)
                with open(p_path, "wb") as f:
                    f.write(pdf_b)
                updates = {"security.original_hash": orig_hash}
                # Subir original a Google Drive dentro de la carpeta de expediente
                try:
                    client_name = data_dict.get("cliente_nombre", "")
                    prop_code = data_dict.get("property_code", "") or data_dict.get("propiedad_codigo", "")
                    folder_id = _get_or_create_expedition_folder(local_db, "visitas", "visita_code", p_code, client_name, prop_code)
                    if folder_id:
                        updates["security.gdrive_folder_id"] = folder_id
                        file_id = gdrive_sync.upload_file(folder_id, f"{p_code}_original.pdf", pdf_b, "application/pdf")
                        if file_id and file_id != "mock_file_id":
                            updates["security.original_pdf_drive_id"] = file_id
                except Exception as e:
                    logger.error(f"[BG TASK] Error subiendo original a GDrive {p_code}: {e}")
                local_db["visitas"].update_one(
                    {"visita_code": p_code},
                    {"$set": updates}
                )
            except Exception as e:
                logger.error(f"[BG TASK] Error generando original: {e}")

        background_tasks.add_task(generate_original_pdf_bg, data, visita_code, perm_original_path)
            
        base_url = str(request.base_url).rstrip('/')
        url_firma = f"{base_url}/visitas/view/{visita_code}"
        
        return {
            "status": "success",
            "visita_code": visita_code,
            "url_firma": url_firma
        }
        
    except Exception as e:
        logger.error(f"Error en /api/create: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/download/{visita_code}")
async def download_original_pdf(visita_code: str):
    """Permite descargar o ver el PDF original"""
    db = get_db()
    contract = await _db_call(db["visitas"].find_one, {"visita_code": visita_code})
    if not contract:
        raise HTTPException(status_code=404, detail="Orden de Visita no encontrado")

    pdf_bytes = None

    # Prioridad 1: ruta permanente guardada en DB
    perm_path_str = contract.get("security", {}).get("original_pdf_path")
    if perm_path_str and os.path.exists(perm_path_str):
        pdf_path = Path(perm_path_str)
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
    else:
        # Prioridad 2: directorio permanente por convención
        perm_path_conv = BASE_DIR / "visitas_pdf" / f"{visita_code}_original.pdf"
        if perm_path_conv.exists():
            with open(perm_path_conv, "rb") as f:
                pdf_bytes = f.read()
        else:
            # Prioridad 3: tmp (efímero)
            tmp_path = BASE_DIR / "tmp" / "visitas" / visita_code / "orden de visita_original.pdf"
            if tmp_path.exists():
                with open(tmp_path, "rb") as f:
                    pdf_bytes = f.read()
            else:
                # Prioridad 4: Google Drive (respaldo permanente si el servidor se reinició)
                drive_id = contract.get("security", {}).get("original_pdf_drive_id")
                if drive_id:
                    try:
                        gdrive = GDriveSync()
                        pdf_bytes = gdrive.download_file(drive_id)
                        if pdf_bytes:
                            logger.info(f"[GDRIVE] Original {visita_code} descargado desde Drive")
                    except Exception as e:
                        logger.error(f"[GDRIVE] Error descargando original {visita_code}: {e}")

                if not pdf_bytes:
                    # Prioridad 5: Regenerar dinámicamente si el servidor se reinició (Render)
                    logger.info(f"Regenerando PDF original para {visita_code} dinámicamente...")
                    data_payload = {
                        "visita_code": contract.get("visita_code"),
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
                        "ejecutivo_nombre": contract.get("executive_data", {}).get("nombre", ""),
                        "ejecutivo_email": contract.get("executive_data", {}).get("email", ""),
                        "created_at": contract.get("created_at"),
                        "version": contract.get("version", 1)
                    }
                    pdf_bytes = PDFGenerator.generate_original_contract(data_payload)
                    # Opcional: Guardar en tmp_path para futuras llamadas rápidas
                    try:
                        tmp_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(tmp_path, "wb") as f:
                            f.write(pdf_bytes)
                    except Exception:
                        pass

    prop_code = contract.get('property_code', 'SD')
    tipo_raw = contract.get('property_data', {}).get('tipo', 'Arriendo')
    tipo = tipo_raw.replace(" ", "_")

    from fastapi.responses import StreamingResponse
    import io
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Content-Disposition": f"inline; filename=Orden_Visita_{tipo}_{prop_code}_{visita_code}.pdf"
        }
    )

@router.get("/api/download_signed/{visita_code}")
async def download_signed_pdf(visita_code: str):
    """Permite descargar el PDF firmado (Forza descarga)"""
    db = get_db()
    contract = await _db_call(db["visitas"].find_one, {"visita_code": visita_code})
    if not contract:
        raise HTTPException(status_code=404, detail="Orden de Visita no encontrado")

    prop_code = contract.get('property_code', 'SD')
    tipo_raw = contract.get('property_data', {}).get('tipo', 'Arriendo')
    tipo = tipo_raw.replace(" ", "_")
    filename = f"Orden_Visita_Autorizacion_{tipo}_{prop_code}_{visita_code}.pdf"

    local_path = contract.get("security", {}).get("signed_pdf_path")
    if local_path and os.path.exists(local_path):
        from fastapi.responses import FileResponse
        return FileResponse(
            path=local_path,
            filename=filename,
            media_type="application/pdf",
            content_disposition_type="attachment",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"}
        )

    file_id = contract.get("security", {}).get("signed_pdf_drive_id")
    if file_id:
        try:
            gdrive = GDriveSync()
            pdf_bytes = gdrive.download_file(file_id)
            if pdf_bytes:
                from fastapi.responses import StreamingResponse
                import io
                return StreamingResponse(
                    io.BytesIO(pdf_bytes),
                    media_type="application/pdf",
                    headers={
                        "Cache-Control": "no-store, no-cache, must-revalidate",
                        "Content-Disposition": f'attachment; filename="{filename}"'
                    }
                )
        except Exception as e:
            logger.error(f"Error downloading from GDrive: {e}")

    # Si llegamos aquí, no está en local ni en Drive (o Drive falló/no configurado).
    # REGENERAMOS el PDF firmado dinámicamente:
    logger.info(f"Regenerando PDF firmado para {visita_code} dinámicamente...")
    
    # 1. Regenerar el original
    data_payload = {
        "visita_code": contract.get("visita_code"),
        "origen": contract.get("origen", ""),
        "property_code": contract.get("property_code", ""),
        "phone": contract.get("phone", ""),
        "cliente_nombre": contract.get("client_data", {}).get("nombre", ""),
        "cliente_rut": contract.get("client_data", {}).get("rut", ""),
        "email": contract.get("client_data", {}).get("email", ""),
        "property_tipo": contract.get("property_data", {}).get("tipo", "Arriendo"),
        "property_comuna": contract.get("property_data", {}).get("comuna", ""),
        "property_region": contract.get("property_data", {}).get("region", ""),
        "operacion": contract.get("property_data", {}).get("operacion", ""),
        "precio": contract.get("property_data", {}).get("precio", ""),
        "ejecutivo_nombre": contract.get("executive_data", {}).get("nombre", ""),
        "ejecutivo_email": contract.get("executive_data", {}).get("email", ""),
        "ejecutivo_telefono": contract.get("executive_data", {}).get("telefono", ""),
        "created_at": contract.get("created_at"),
        "version": contract.get("version", 1)
    }
    original_bytes = PDFGenerator.generate_original_contract(data_payload)
    
    # 2. Reconstruir la evidencia desde la DB
    timeline = contract.get("timeline", [])
    accepted_event = next((evt for evt in timeline if evt.get("action") == "accepted"), {})
    
    security_data = contract.get("security", {})
    transaction_uuid = security_data.get("transaction_uuid")
    if not transaction_uuid:
        vt = security_data.get("verify_token", "")
        if len(vt) == 32:
            transaction_uuid = f"{vt[:8]}-{vt[8:12]}-{vt[12:16]}-{vt[16:20]}-{vt[20:]}"
        else:
            transaction_uuid = str(uuid.uuid4())

    evidence_data = {
        "visita_code": transaction_uuid,
        "contract_code": visita_code,
        "verify_token": security_data.get("verify_token", ""),
        "server_timestamp": accepted_event.get("server_timestamp", ""),
        "ip": accepted_event.get("ip", ""),
        "geo_info": accepted_event.get("geo_location", "Localización no disponible"),
        "timezone": accepted_event.get("timezone", "America/Santiago (CLT)"),
        "user_agent": accepted_event.get("user_agent", ""),
        "original_hash": security_data.get("original_hash", ""),
        "server_hmac": security_data.get("server_hmac", ""),
        "timeline_hash": security_data.get("timeline_hash", ""),
        "read_time_seconds": accepted_event.get("read_time_seconds", 0),
        "scrolled_to_bottom": "Sí" if accepted_event.get("scrolled_to_bottom") else "No",
        "read_method": accepted_event.get("read_method", "scroll")
    }
    
    # Evitar fallar si faltan datos en orden de visitas antiguos
    base_url = getattr(Config, "CRM_BASE_URL", "https://procasa.cl").rstrip('/')
    verify_url = f"{base_url}/visitas/verify/{visita_code}"
    
    signed_pdf_bytes = PDFGenerator.generate_signed_contract(original_bytes, contract, evidence_data, verify_url)
    
    from fastapi.responses import StreamingResponse
    import io
    return StreamingResponse(
        io.BytesIO(signed_pdf_bytes),
        media_type="application/pdf",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )

@router.get("/api/view_signed/{visita_code}")
async def view_signed_pdf(visita_code: str):
    """Permite visualizar el PDF firmado dentro del navegador"""
    db = get_db()
    contract = await _db_call(db["visitas"].find_one, {"visita_code": visita_code})
    if not contract:
        raise HTTPException(status_code=404, detail="Orden de Visita no encontrado")

    prop_code = contract.get('property_code', 'SD')
    tipo_raw = contract.get('property_data', {}).get('tipo', 'Arriendo')
    tipo = tipo_raw.replace(" ", "_")
    filename = f"Orden_Visita_Autorizacion_{tipo}_{prop_code}_{visita_code}.pdf"

    local_path = contract.get("security", {}).get("signed_pdf_path")
    if local_path and os.path.exists(local_path):
        from fastapi.responses import FileResponse
        return FileResponse(
            path=local_path,
            filename=filename,
            media_type="application/pdf",
            content_disposition_type="inline",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"}
        )

    file_id = contract.get("security", {}).get("signed_pdf_drive_id")
    if file_id:
        try:
            gdrive = GDriveSync()
            pdf_bytes = gdrive.download_file(file_id)
            if pdf_bytes:
                from fastapi.responses import StreamingResponse
                import io
                return StreamingResponse(
                    io.BytesIO(pdf_bytes),
                    media_type="application/pdf",
                    headers={
                        "Cache-Control": "no-store, no-cache, must-revalidate",
                        "Content-Disposition": f'inline; filename="{filename}"'
                    }
                )
        except Exception as e:
            logger.error(f"Error downloading from GDrive: {e}")
            
    # Si llegamos aquí, no está en local ni en Drive (o Drive falló/no configurado).
    # REGENERAMOS el PDF firmado dinámicamente:
    logger.info(f"Regenerando PDF firmado para {visita_code} dinámicamente...")
    
    # 1. Regenerar el original
    data_payload = {
        "visita_code": contract.get("visita_code"),
        "origen": contract.get("origen", ""),
        "property_code": contract.get("property_code", ""),
        "phone": contract.get("phone", ""),
        "cliente_nombre": contract.get("client_data", {}).get("nombre", ""),
        "cliente_rut": contract.get("client_data", {}).get("rut", ""),
        "email": contract.get("client_data", {}).get("email", ""),
        "property_tipo": contract.get("property_data", {}).get("tipo", "Arriendo"),
        "property_comuna": contract.get("property_data", {}).get("comuna", ""),
        "property_region": contract.get("property_data", {}).get("region", ""),
        "operacion": contract.get("property_data", {}).get("operacion", ""),
        "precio": contract.get("property_data", {}).get("precio", ""),
        "ejecutivo_nombre": contract.get("executive_data", {}).get("nombre", ""),
        "ejecutivo_email": contract.get("executive_data", {}).get("email", ""),
        "ejecutivo_telefono": contract.get("executive_data", {}).get("telefono", ""),
        "created_at": contract.get("created_at"),
        "version": contract.get("version", 1)
    }
    original_bytes = PDFGenerator.generate_original_contract(data_payload)
    
    # 2. Reconstruir la evidencia desde la DB
    timeline = contract.get("timeline", [])
    accepted_event = next((evt for evt in timeline if evt.get("action") == "accepted"), {})
    
    security_data = contract.get("security", {})
    transaction_uuid = security_data.get("transaction_uuid")
    if not transaction_uuid:
        vt = security_data.get("verify_token", "")
        if len(vt) == 32:
            transaction_uuid = f"{vt[:8]}-{vt[8:12]}-{vt[12:16]}-{vt[16:20]}-{vt[20:]}"
        else:
            transaction_uuid = str(uuid.uuid4())

    evidence_data = {
        "visita_code": transaction_uuid,
        "contract_code": visita_code,
        "verify_token": security_data.get("verify_token", ""),
        "server_timestamp": accepted_event.get("server_timestamp", ""),
        "ip": accepted_event.get("ip", ""),
        "geo_info": accepted_event.get("geo_location", "Localización no disponible"),
        "timezone": accepted_event.get("timezone", "America/Santiago (CLT)"),
        "user_agent": accepted_event.get("user_agent", ""),
        "original_hash": security_data.get("original_hash", ""),
        "server_hmac": security_data.get("server_hmac", ""),
        "timeline_hash": security_data.get("timeline_hash", ""),
        "read_time_seconds": accepted_event.get("read_time_seconds", 0),
        "scrolled_to_bottom": "Sí" if accepted_event.get("scrolled_to_bottom") else "No",
        "read_method": accepted_event.get("read_method", "scroll")
    }
    
    # Evitar fallar si faltan datos en orden de visitas antiguos
    base_url = getattr(Config, "CRM_BASE_URL", "https://procasa.cl").rstrip('/')
    verify_url = f"{base_url}/visitas/verify/{visita_code}"
    
    signed_pdf_bytes = PDFGenerator.generate_signed_contract(original_bytes, contract, evidence_data, verify_url)
    
    from fastapi.responses import StreamingResponse
    import io
    return StreamingResponse(
        io.BytesIO(signed_pdf_bytes),
        media_type="application/pdf",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Content-Disposition": f'inline; filename="{filename}"'
        }
    )

@router.put("/api/{visita_code}/update")
async def update_visita(visita_code: str, request: Request):
    """Actualiza los datos de una orden de visita (solo si no ha sido enviada aún)"""
    from chatbot.storage import get_async_db
    adb = get_async_db()
    username, user_role = await _get_request_user(adb, request)

    contract = await adb["visitas"].find_one({"visita_code": visita_code})
    if not contract:
        raise HTTPException(status_code=404, detail="Orden de Visita no encontrada")

    # Bloqueo estricto si ya se ha enviado o firmado
    if contract.get("status") in ["otp_requested", "otp_verified", "signed"]:
        raise HTTPException(status_code=403, detail="La orden ya está en proceso de firma o firmada y NO puede ser editada por protección de integridad documental.")

    # Solo se puede editar libremente si está en estado 'created' o 'opened'. Para otros, requiere admin.
    if contract.get("status") not in ["created", "opened", "sent"]:
        if user_role not in ["supervisor", "admin"]:
            raise HTTPException(status_code=403, detail="No tiene permisos para editar esta orden en su estado actual.")

    data = _normalize_visita_fields(await request.json())
    
    if data.get("property_code"):
        enriched = await _enrich_with_property_data({"property_code": data["property_code"]})
        if enriched.get("_property_found") is False:
            raise HTTPException(status_code=400, detail="El código de propiedad ingresado no existe en nuestra base de datos.")

    update_fields = {}
    if data.get("cliente_nombre"):
        update_fields["client_data.nombre"] = data["cliente_nombre"]
    if data.get("cliente_rut"):
        update_fields["client_data.rut"] = data["cliente_rut"]
    if data.get("email"):
        update_fields["client_data.email"] = data["email"]
    if data.get("cliente_direccion") is not None:
        update_fields["client_data.direccion"] = data.get("cliente_direccion", "")
    if data.get("cliente_comuna") is not None:
        update_fields["client_data.comuna"] = data.get("cliente_comuna", "")
    if data.get("phone"):
        update_fields["phone"] = data["phone"]
    if data.get("property_code"):
        update_fields["property_code"] = data["property_code"]
        # Re-enriquecer con datos de la propiedad
        enriched = await _enrich_with_property_data({"property_code": data["property_code"]})
        if enriched.get("property_comuna"):
            update_fields["property_data.comuna"] = enriched["property_comuna"]
        if enriched.get("property_region"):
            update_fields["property_data.region"] = enriched["property_region"]
        if enriched.get("property_tipo"):
            update_fields["property_data.tipo"] = enriched["property_tipo"]
        if enriched.get("precio"):
            update_fields["property_data.precio"] = enriched["precio"]
        if enriched.get("operacion"):
            update_fields["property_data.operacion"] = enriched["operacion"]

    if not update_fields:
        return {"status": "success", "message": "Sin cambios"}

    # --- Auditoría de Edición ---
    from datetime import datetime, timezone
    update_fields["updated_by"] = username
    update_fields["updated_at"] = datetime.now(timezone.utc)
    
    edit_record = {
        "updated_by": username,
        "updated_at": datetime.now(timezone.utc),
        "changes": {k: v for k, v in update_fields.items() if k not in ["updated_by", "updated_at"]}
    }

    await adb["visitas"].update_one(
        {"visita_code": visita_code},
        {
            "$set": update_fields,
            "$push": {"edit_history": edit_record}
        }
    )

    # Regenerar el PDF original en background después de editar
    contract_updated = await adb["visitas"].find_one({"visita_code": visita_code})
    if contract_updated:
        exec_data = contract_updated.get("executive_data", {})
        prop_data = contract_updated.get("property_data", {})
        data_payload = {
            "visita_code": visita_code,
            "property_code": contract_updated.get("property_code", ""),
            "phone": contract_updated.get("phone", ""),
            "cliente_nombre": contract_updated.get("client_data", {}).get("nombre", ""),
            "cliente_rut": contract_updated.get("client_data", {}).get("rut", ""),
            "email": contract_updated.get("client_data", {}).get("email", ""),
            "cliente_direccion": contract_updated.get("client_data", {}).get("direccion", ""),
            "cliente_comuna": contract_updated.get("client_data", {}).get("comuna", ""),
            # Datos de propiedad desde property_data (subdocumento en MongoDB)
            "property_comuna": prop_data.get("comuna", ""),
            "property_region": prop_data.get("region", ""),
            "property_tipo": prop_data.get("tipo", ""),
            "precio": prop_data.get("precio", ""),
            "operacion": prop_data.get("operacion", ""),
            # Datos del ejecutivo
            "ejecutivo_nombre": exec_data.get("nombre", ""),
            "ejecutivo_email": exec_data.get("email", ""),
            "ejecutivo_telefono": exec_data.get("phone") or exec_data.get("telefono", ""),
        }
        try:
            pdf_bytes = await _run_blocking(PDFGenerator.generate_original_contract, data_payload)
            perm_dir = BASE_DIR / "visitas_pdf"
            perm_dir.mkdir(parents=True, exist_ok=True)
            perm_path = perm_dir / f"{visita_code}_original.pdf"
            with open(perm_path, "wb") as f:
                f.write(pdf_bytes)
            # También actualizar el tmp para mantener consistencia entre rutas
            tmp_dir_upd = BASE_DIR / "tmp" / "visitas" / visita_code
            tmp_dir_upd.mkdir(parents=True, exist_ok=True)
            try:
                with open(tmp_dir_upd / "orden de visita_original.pdf", "wb") as f:
                    f.write(pdf_bytes)
            except Exception as tmp_err:
                logger.warning(f"[UPDATE] No se pudo actualizar tmp PDF para {visita_code}: {tmp_err}")
            orig_hash = SecurityContracts.hash_document(pdf_bytes)
            await adb["visitas"].update_one(
                {"visita_code": visita_code},
                {"$set": {"security.original_hash": orig_hash, "security.original_pdf_path": str(perm_path)}}
            )
        except Exception as e:
            logger.warning(f"[UPDATE] No se pudo regenerar PDF para {visita_code}: {e}")

    return {"status": "success", "message": "Orden de visita actualizada correctamente"}

@router.post("/api/{visita_code}/send")
async def send_contract(visita_code: str, request: Request):
    """Genera token y envía por WhatsApp"""
    from chatbot.storage import get_async_db
    adb = get_async_db()
    username, user_role = await _get_request_user(adb, request)
    contract = await adb["visitas"].find_one({"visita_code": visita_code})
    if not contract:
        raise HTTPException(status_code=404, detail="Orden de Visita no encontrado")
    # Después del primer envío, solo supervisor/admin puede reenviar
    if contract.get("status") in ["sent", "opened", "otp_requested", "otp_verified", "signed", "accepted"]:
        if user_role not in ["supervisor", "admin"]:
            raise HTTPException(status_code=403, detail="Solo supervisor/admin puede reenviar orden de visitas ya enviados.")
        
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
    
    await adb["visitas"].update_one(
        {"visita_code": visita_code},
        {
            "$set": {
                "status": "sent",
                "security.token": token,
                "security.token_expiry": expiry,
                "security.token_used": False
            },
            "$push": {
                "timeline": {
                    "action": "visita_sent",
                    "server_timestamp": server_timestamp,
                    "ip": ip,
                    "user_agent": ua
                }
            }
        }
    )
    
    # Enviar WhatsApp
    phone = str(contract.get("phone", "")).replace(" ", "").replace("+", "").strip()
    if not phone.startswith("56"):
        phone = "56" + phone
    # Usar la base_url de la request actual para que funcione localmente o en prod
    base_url = str(request.base_url).rstrip('/')
    link = f"{base_url}/visitas/view/{token}"
    
    nombre = contract.get('client_data', {}).get('nombre', contract.get('cliente_nombre', ''))
    direccion = contract.get('property_data', {}).get('direccion', contract.get('propiedad_direccion', ''))
    
    property_code_display = contract.get("property_code", "")
    mensaje = f"""Hola {nombre} 👋

Para coordinar la visita de la propiedad:
https://www.procasa.cl/{property_code_display}

Necesitamos que revise y firme digitalmente la Orden de Visita.

🔒 Este enlace es personal, confidencial e intransferible. Al ingresar y firmar el documento, usted confirma ser el titular de este número telefónico y acepta las condiciones asociadas a la visita.

La firma electrónica utilizada en este proceso se encuentra respaldada por la Ley N° 19.799 sobre Documentos y Firma Electrónica.

👉 Revise y firme aquí:
{link}"""
    
    # Guardar mensaje dentro del mismo documento del orden de visita
    try:
        await adb["visitas"].update_one(
            {"visita_code": visita_code},
            {"$push": {"messages": {
                "phone": phone,
                "message_content": mensaje,
                "message_type": "visita_sent",
                "timestamp_utc": datetime.now(timezone.utc)
            }}}
        )
    except Exception as e:
        logger.error(f"[MSG_LOG] Error guardando mensaje: {e}")
    
    try:
        from chatbot.whatsapp_client import send_whatsapp_message
        success = await send_whatsapp_message(phone, mensaje)
        if not success:
            # Revertimos status si falla
            await adb["visitas"].update_one({"visita_code": visita_code}, {"$set": {"status": contract.get("status", "created")}})
            raise HTTPException(status_code=400, detail="El número de teléfono es inválido o no tiene WhatsApp. Corríjalo e intente nuevamente.")
    except HTTPException:
        raise
    except Exception as e:
        await adb["visitas"].update_one({"visita_code": visita_code}, {"$set": {"status": contract.get("status", "created")}})
        raise HTTPException(status_code=400, detail=f"Error en WhatsApp: {str(e)}")
        
    return {"status": "ok", "message": "Enviado por WhatsApp"}

@router.get("/api/statuses")
async def visitas_statuses(request: Request):
    from chatbot.storage import get_async_db
    adb = get_async_db()
    username, user_role = await _get_request_user(adb, request)
    if user_role in ["supervisor", "admin"]:
        query = {"status": {"$ne": "deleted"}}
    else:
        query = {"status": {"$ne": "deleted"}, "created_by": username}
    rows = await adb["visitas"].find(query, {"visita_code": 1, "status": 1}).to_list(length=300)
    return {"items": [{"visita_code": r.get("visita_code"), "status": r.get("status", "created")} for r in rows]}

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
async def view_visita_public(token: str, request: Request):
    """Vista pública para el cliente"""
    db = get_db()
    contract = await _db_call(db["visitas"].find_one, {"security.token": token})
    
    if not contract:
        return HTMLResponse("<h1>Enlace inválido o expirado.</h1>", status_code=404)
        
    is_signed = contract["security"].get("token_used", False)
    
    # Solo expira a las 24h si NO está firmado. Si ya se firmó, el acceso es permanente.
    if not is_signed:
        try:
            ensure_document_valid(contract)
        except HTTPException:
            return HTMLResponse("<h1>Enlace inválido o expirado.</h1>", status_code=404)
    logger.info(f"[METRIC] visitas_started: {contract['visita_code']}")
        
    # Registrar acceso
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    server_timestamp = SecurityContracts.generate_server_timestamp()
    
    update_query = {
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
    if not is_signed and contract.get("status") not in ["signed", "accepted"]:
        update_query["$set"] = {"status": "opened"}

    await _db_call(
        db["visitas"].update_one,
        {"visita_code": contract["visita_code"]},
        update_query,
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
        
    return templates.TemplateResponse("visita_view.html", {
        "request": request,
        "contract": contract,
        "token": token,
        "token_expiry_iso": token_expiry_iso,
        "is_signed": is_signed
    })

@router.post("/api/{token}/validate-rut")
async def validate_rut(token: str, request: Request):
    """Valida el RUT contra el orden de visita antes de solicitar el OTP (sin enviarlo)"""
    data = await request.json()
    rut_ingresado = data.get("rut", "").strip()
    
    db = get_db()
    contract = await _db_call(db["visitas"].find_one, {"security.token": token})
    if not contract:
        raise HTTPException(status_code=403, detail="DOCUMENT_EXPIRED")
        
    visita_rut = contract.get("client_data", {}).get("rut", "").strip()
    if not visita_rut:
        visita_rut = contract.get("cliente_rut", "").strip()

    if visita_rut:
        rut_clean = ''.join(filter(str.isalnum, rut_ingresado)).upper()
        visita_rut_clean = ''.join(filter(str.isalnum, visita_rut)).upper()
        if visita_rut_clean != rut_clean:
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
    contract = await adb["visitas"].find_one({"security.token": token})
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
            
    visita_rut = contract.get("client_data", {}).get("rut", "").strip()
    if not visita_rut:
        visita_rut = contract.get("cliente_rut", "").strip()

    if visita_rut:
        rut_clean = ''.join(filter(str.isalnum, rut_ingresado)).upper()
        visita_rut_clean = ''.join(filter(str.isalnum, visita_rut)).upper()
        if visita_rut_clean != rut_clean:
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
    
    await adb["visitas"].update_one(
        {"visita_code": contract["visita_code"]},
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
    
    mensaje = f"""Código de verificación: *{otp}*

⏳ Válido por 5 minutos.
🔒 No compartas este código con nadie."""
    
    try:
        await adb["visitas"].update_one(
            {"visita_code": contract["visita_code"]},
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
            await local_adb["visitas"].update_one(
                {"visita_code": c_code},
                {"$push": {"timeline": {"action": "otp_delivery_failed", "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua, "error": str(e)}}}
            )

    background_tasks.add_task(send_wa_bg_task, contract["phone"], mensaje, contract["visita_code"])
    
    t_otp_elapsed = time.time() - t_otp_start
    logger.info(f"[TIMING] request_otp: visita_code={contract['visita_code']} response_time={t_otp_elapsed:.3f}s")
    return {"status": "ok"}

@router.post("/api/{token}/verify-otp")
async def verify_otp(token: str, request: Request):
    data = await request.json()
    otp_ingresado = data.get("otp", "").strip()
    
    ip = get_client_ip(request)
    check_rate_limit(ip, verify_rate_limit, 10, window_seconds=60)
    
    db = get_db()
    contract = await _db_call(db["visitas"].find_one, {"security.token": token})
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
        logger.info(f"[USER_RETURNED_TO_STEP2] visita_code={contract['visita_code']} reason=OTP_EXPIRED timestamp={now}")
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
            await _db_call(db["visitas"].update_one, {"visita_code": contract["visita_code"]}, update_doc)
            raise HTTPException(status_code=429, detail="OTP_BLOCKED|60")
        else:
            await _db_call(db["visitas"].update_one, {"visita_code": contract["visita_code"]}, update_doc)
            remaining = 5 - attempts
            raise HTTPException(status_code=400, detail=f"OTP_INVALID|{remaining}")
        
    # Success
    await _db_call(
        db["visitas"].update_one,
        {"visita_code": contract["visita_code"]},
        {
            "$set": {"status": "otp_verified", "security.otp": None}, # Invalidate OTP
            "$push": {"timeline": {"action": "otp_verified", "server_timestamp": server_timestamp, "ip": ip, "user_agent": ua}}
        }
    )
    return {"status": "ok"}

@router.post("/api/{token}/accept_terms")
async def accept_terms(token: str, request: Request):
    db = get_db()
    contract = await _db_call(db["visitas"].find_one, {"security.token": token})
    if not contract: return {"status": "error"}
    
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    server_timestamp = SecurityContracts.generate_server_timestamp()
    
    data = await request.json()
    checkbox_state = data.get("accepted", False)
    
    await _db_call(
        db["visitas"].update_one,
        {"visita_code": contract["visita_code"]},
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
    contract = await _db_call(db["visitas"].find_one, {"security.token": token})
    if not contract:
        logger.error(f"[SERVER_ERROR] Token inválido intentado para {token}")
        raise HTTPException(status_code=403, detail="Token inválido")

    ensure_document_valid(contract)

    # Idempotencia: si ya fue firmado, retornar éxito sin reprocessar
    if contract.get("status") == "signed" or contract["security"].get("token_used"):
        logger.info(f"[CONTRACT_SIGNED] Idempotente — orden de visita {contract['visita_code']} ya firmado.")
        return JSONResponse(status_code=200, content={"status": "already_signed", "visita_code": contract["visita_code"]})

    if contract["status"] != "otp_verified":
        raise HTTPException(status_code=403, detail="Orden de Visita no válido para aceptación")

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
            logger.error(f"[DOCUMENT_EXPIRED] visita_code={contract['visita_code']} reason=SIGNATURE_SESSION_EXPIRED timestamp={datetime.now(timezone.utc)}")
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
    visita_code = contract["visita_code"]
    t_sign_start = time.time()
    
    timezone_info = "America/Santiago (CLT)"
    
    # 1. Registrar aceptaci\u00f3n
    await _db_call(
        db["visitas"].update_one,
        {"visita_code": visita_code},
        {
            "$set": {
                "status": "signed", 
                "security.token_used": True,
                "security.signature_timestamp": datetime.now(timezone.utc),
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
    
    logger.info(f"[METRIC] visitas_signed: {visita_code}")
    
    # Refrescar documento para tener el timeline completo
    contract = await _db_call(db["visitas"].find_one, {"visita_code": visita_code})
    timeline = contract.get("timeline") or []  # Fix: guard against NoneType
    
    # 2. Generar Firma HMAC del Servidor y Hash del Timeline
    timeline_hash = SecurityContracts.hash_timeline(timeline)
    tmp_dir = BASE_DIR / "tmp" / "visitas" / visita_code
    try:
        # PRIORIDAD 1: PDF permanente actualizado (siempre regenerado al editar)
        # Esto evita usar el tmp obsoleto cuando la orden fue editada antes de firmar.
        perm_original_path = BASE_DIR / "visitas_pdf" / f"{visita_code}_original.pdf"
        perm_path_from_db = contract.get("security", {}).get("original_pdf_path", "")

        if perm_path_from_db and os.path.exists(perm_path_from_db):
            with open(perm_path_from_db, "rb") as f:
                original_bytes = f.read()
            logger.info(f"[SIGN] PDF original leído desde ruta DB permanente: {perm_path_from_db}")
        elif perm_original_path.exists():
            with open(perm_original_path, "rb") as f:
                original_bytes = f.read()
            logger.info(f"[SIGN] PDF original leído desde visitas_pdf permanente: {perm_original_path}")
        else:
            # PRIORIDAD 3: Regenerar desde los datos actualizados de la DB
            # (nunca usar el tmp ya que puede estar desactualizado respecto a ediciones)
            logger.info(f"[SIGN] PDF original no encontrado en disco, regenerando desde DB para {visita_code}")
            data_payload = {
                "visita_code": contract.get("visita_code"),
                "origen": contract.get("origen", ""),
                "property_code": contract.get("property_code", ""),
                "phone": contract.get("phone", ""),
                "cliente_nombre": contract.get("client_data", {}).get("nombre", ""),
                "cliente_rut": contract.get("client_data", {}).get("rut", ""),
                "email": contract.get("client_data", {}).get("email", ""),
                "propiedad_direccion": contract.get("property_data", {}).get("direccion", ""),
                "property_comuna": contract.get("property_data", {}).get("comuna", ""),
                "property_region": contract.get("property_data", {}).get("region", ""),
                "property_tipo": contract.get("property_data", {}).get("tipo", "Arriendo"),
                "rol": contract.get("property_data", {}).get("rol", ""),
                "vigencia": contract.get("property_data", {}).get("vigencia", "30"),
                "precio": contract.get("property_data", {}).get("precio", ""),
                "comision": contract.get("property_data", {}).get("comision", ""),
                "ejecutivo_nombre": contract.get("executive_data", {}).get("nombre", ""),
                "ejecutivo_email": contract.get("executive_data", {}).get("email", ""),
                "ejecutivo_telefono": contract.get("executive_data", {}).get("telefono", "")
            }
            original_bytes = PDFGenerator.generate_original_contract(data_payload)

        original_hash = SecurityContracts.hash_document(original_bytes)
        secret_key = getattr(Config, "SECRET_KEY", "default_secret")
        server_hmac = SecurityContracts.generate_server_hmac(visita_code, original_hash, server_timestamp, secret_key)
        base_url = str(request.base_url).rstrip('/')
        verify_token = str(uuid.uuid4()).replace("-", "")
        verify_url = f"{base_url}/visitas/verify/{visita_code}"

        transaction_uuid = str(uuid.uuid4())
        evidence_data = {
            "visita_code": transaction_uuid,
            "contract_code": visita_code,
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
        with open(tmp_dir / "orden de visita_firmado.pdf", "wb") as f:
            f.write(signed_pdf_bytes)
            
        # Guardar localmente permanente como respaldo por si Drive no funciona
        perm_dir = BASE_DIR / "visitas_pdf"
        perm_dir.mkdir(parents=True, exist_ok=True)
        local_pdf_path = perm_dir / f"{visita_code}_firmado.pdf"
        with open(local_pdf_path, "wb") as f:
            f.write(signed_pdf_bytes)

        import json
        with open(tmp_dir / "hash.txt", "w") as f:
            f.write(f"Original Hash: {original_hash}\nSigned Hash: {signed_hash}\nTimeline Hash: {timeline_hash}\nHMAC: {server_hmac}")
        with open(tmp_dir / "timeline.json", "w") as f:
            json.dump(timeline, f, indent=4)

        # 5. Guardar Hashes finales y ruta local en DB
        await _db_call(
            db["visitas"].update_one,
            {"visita_code": visita_code},
            {"$set": {
                "security.signed_hash": signed_hash,
                "security.server_hmac": server_hmac,
                "security.timeline_hash": timeline_hash,
                "security.signed_pdf_path": str(local_pdf_path),
                "security.verify_token": verify_token,
                "security.transaction_uuid": transaction_uuid
            }}
        )

        # TSA mock
        tsa_response = f"TSA_MOCK_{datetime.now(timezone.utc).timestamp()}_SIGNED"
        await _db_call(db["visitas"].update_one, {"visita_code": visita_code}, {"$set": {"security.tsa_stamp": tsa_response}})

        # 6. Generar Informe Legal y Subida a Google Drive en Background
        def finalize_bg_task(c_code, c_doc, e_data, t_line, s_pdf_bytes, o_hash, s_hash, t_hash, s_hmac):
            try:
                # Import din\u00e1mico
                from services.pdf_generator_visitas import PDFGeneratorVisitas as PDFGenerator
                l_report_bytes = PDFGenerator.generate_legal_report(c_doc, e_data, t_line)
                
                t_dir = BASE_DIR / "tmp" / "visitas" / c_code
                t_dir.mkdir(parents=True, exist_ok=True)
                with open(t_dir / "informe_legal.pdf", "wb") as f:
                    f.write(l_report_bytes)
                    
                upload_to_gdrive_bg(
                    c_code,
                    {
                        "orden de visita_firmado.pdf": s_pdf_bytes,
                        "informe_legal.pdf": l_report_bytes,
                        "hash.txt": f"Original Hash: {o_hash}\nSigned Hash: {s_hash}\nTimeline Hash: {t_hash}\nHMAC: {s_hmac}".encode(),
                    }
                )
            except Exception as e:
                logger.error(f"[BG TASK] Error finalizando orden de visita {c_code}: {e}")

        background_tasks.add_task(
            finalize_bg_task,
            visita_code, contract, evidence_data, timeline, 
            signed_pdf_bytes, original_hash, signed_hash, timeline_hash, server_hmac
        )

        # 7. Notificar al Cliente y enviar email — BACKGROUND (no bloquear respuesta)
        client_email = contract.get("client_data", {}).get("email", contract.get("email", ""))
        background_tasks.add_task(
            notify_client_bg,
            visita_code,
            contract.get("phone", ""),
            client_email,
            contract.get("client_data", {}).get("nombre", ""),
            signed_pdf_bytes,
            contract.get("property_code", "")
        )

        t_sign_elapsed = time.time() - t_sign_start
        logger.info(
            f"[TIMING] accept_contract: visita_code={visita_code} "
            f"response_time={t_sign_elapsed:.3f}s ip={ip} timestamp={server_timestamp}"
        )
        logger.info(
            f"[CONTRACT_SIGNED] visita_code={visita_code} "
            f"rut={contract.get('client_data', {}).get('rut', 'N/A')} "
            f"ip={ip} timestamp={server_timestamp} "
            f"read_time={read_time}s scrolled={scrolled_to_bottom}"
        )

        return {"status": "ok", "visita_code": visita_code}

    finally:
        # Limpieza del directorio temporal (siempre, incluso en caso de error)
        # Los archivos ya fueron subidos a GDrive y enviados por email antes de llegar aquí
        shutil.rmtree(tmp_dir, ignore_errors=True)
    
def _get_or_create_expedition_folder(db, collection, code_field, code, client_name, property_code):
    """Obtiene (o crea) la carpeta de expediente para un documento. Reusa la de la DB si existe."""
    try:
        doc = db[collection].find_one({code_field: code})
        existing_id = (doc or {}).get("security", {}).get("gdrive_folder_id")
        if existing_id and existing_id != "mock_folder_id":
            return existing_id
        folder_id = gdrive_sync.create_folder(expedition_folder_name(client_name, property_code))
        if folder_id and folder_id != "mock_folder_id":
            db[collection].update_one(
                {code_field: code},
                {"$set": {"security.gdrive_folder_id": folder_id}}
            )
            return folder_id
    except Exception as e:
        logger.error(f"[GDRIVE] Error obteniendo/creando carpeta expediente {code}: {e}")
    return None


def upload_to_gdrive_bg(visita_code: str, files: dict):
    """Sube archivos a GDrive recibiendo bytes en memoria, sin depender del filesystem."""
    try:
        db = get_db()
        contract = db["visitas"].find_one({"visita_code": visita_code})
        if not contract:
            logger.error(f"[GDRIVE] Orden {visita_code} no encontrada para subir expediente")
            return
        client_name = contract.get("client_data", {}).get("nombre", "")
        property_code = contract.get("property_code", "")
        folder_id = _get_or_create_expedition_folder(
            db, "visitas", "visita_code", visita_code, client_name, property_code
        )
        if not folder_id:
            logger.error(f"[GDRIVE] Sin carpeta para expediente {visita_code}")
            return
        signed_file_id = None
        for filename, content in files.items():
            if isinstance(content, str):
                content = content.encode()
            mime = "application/pdf" if filename.endswith(".pdf") else "text/plain"
            file_id = gdrive_sync.upload_file(folder_id, filename, content, mime)
            if filename == "orden de visita_firmado.pdf":
                signed_file_id = file_id
        # Guardar el file_id del orden de visita firmado en DB para trazabilidad documental
        if signed_file_id:
            db["visitas"].update_one(
                {"visita_code": visita_code},
                {"$set": {"security.signed_pdf_drive_id": signed_file_id}}
            )
        logger.info(f"[GDRIVE] Expediente {visita_code} subido. signed_pdf_id={signed_file_id}")
    except Exception as e:
        logger.error(f"[GDRIVE] Error subiendo expediente {visita_code}: {e}")

def notify_client_bg(visita_code: str, phone: str, client_email: str, nombre: str, signed_pdf_bytes: bytes, property_code: str = ""):
    """Background task: sends WhatsApp confirmation + email. Runs after response is returned."""
    import asyncio
    db = get_db()
    contract = db["visitas"].find_one({"visita_code": visita_code})
    if not contract:
        logger.error(f"[NOTIFY_BG] Contract {visita_code} not found for notification")
        return

    # Exactly-once guard
    if contract.get("notifications_sent"):
        logger.info(f"[NOTIFY_BG] Notifications already sent for {visita_code} — skipping")
        return

    # Mark as sent immediately to prevent duplicates
    db["visitas"].update_one(
        {"visita_code": visita_code},
        {"$set": {"notifications_sent": True}}
    )

    # WhatsApp confirmation
    mensaje_conf = """✅ ¡Proceso completado con éxito!

Tu Orden de Visita fue firmada electrónicamente y registrada de forma segura conforme a la Ley N° 19.799.

📄 En breve recibirás una copia del documento firmado en tu correo o medio de contacto registrado."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(send_whatsapp_message(phone, mensaje_conf))
        loop.close()
        db["visitas"].update_one(
            {"visita_code": visita_code},
            {"$push": {"messages": {
                "phone": phone,
                "message_content": mensaje_conf,
                "message_type": "confirmation_sent",
                "timestamp_utc": datetime.now(timezone.utc)
            }}}
        )
    except Exception as e:
        logger.error(f"[NOTIFY_BG] WhatsApp error for {visita_code}: {e}")

    # Email delivery
    if client_email and signed_pdf_bytes:
        send_signed_email_task(visita_code, client_email, nombre, signed_pdf_bytes, property_code)

def send_signed_email_task(visita_code: str, email_to: str, nombre: str, pdf_bytes: bytes, property_code: str = "", cc_email: str = ""):
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

        prop_label = property_code if property_code else visita_code
        asunto = f"Orden de Visita Firmada – Propiedad {prop_label} – {nombre}"
        
        db = get_db()
        contract = db["visitas"].find_one({"visita_code": visita_code})
        tipo_raw = contract.get("property_data", {}).get("tipo", "Arriendo") if contract else "Arriendo"
        tipo = tipo_raw.replace(" ", "_")
        pdf_filename = f"Orden_Visita_Autorizacion_{tipo}_{prop_label}_{visita_code}.pdf"
        
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

        verify_url_email = f"{Config.CRM_BASE_URL}/visitas/verify/{visita_code}"
        body = f"""Estimado/a {nombre}:

Junto con saludar, adjuntamos la Orden de Visita correspondiente a la propiedad N° {prop_label}, la cual ha sido firmada electrónicamente conforme a la Ley N° 19.799 sobre Documentos y Firma Electrónica.

Detalle del documento:
• Propiedad: {prop_label}
• Código de verificación: {visita_code}

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

        logger.info(f"[EMAIL] PDF firmado enviado a {email_to} (CC: {cc_str}) para orden de visita {visita_code}")

        db = get_db()
        db["visitas"].update_one(
            {"visita_code": visita_code},
            {"$push": {"messages": {
                "email": email_to,
                "cc": cc_recipients,
                "message_type": "email_signed_pdf_sent",
                "timestamp_utc": datetime.now(timezone.utc)
            }}}
        )
    except Exception as e:
        logger.error(f"[EMAIL] Error enviando correo firmado a {email_to}: {e}")

@router.delete("/api/delete/{visita_code}")
async def delete_contract(visita_code: str):
    """Permite eliminar un orden de visita lógicamente (soft delete)."""
    db = get_db()
    result = await _db_call(
        db["visitas"].update_one,
        {"visita_code": visita_code},
        {"$set": {"status": "deleted"}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Orden de Visita no encontrado")
    return {"status": "ok"}

@router.get("/verify/{visita_code}", response_class=HTMLResponse)
async def verify_contract(visita_code: str, request: Request):
    from bson import ObjectId
    def _json_safe(value):
        if isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(v) for v in value]
        return value

    from chatbot.storage import get_async_db
    db = get_async_db()
    contract = await db["visitas"].find_one({"visita_code": visita_code})
    if not contract:
        return HTMLResponse("<h1>Orden de Visita no encontrado</h1>", status_code=404)
    
    # Limpiar datos para evitar error 500 de serialización
    safe_contract = _json_safe(contract)
    
    # Formatear fecha de firma para visualización humana
    signature_date_raw = contract.get("security", {}).get("signature_timestamp")
    formatted_date = "---"
    if signature_date_raw:
        try:
            if isinstance(signature_date_raw, datetime):
                dt = signature_date_raw
            else:
                dt = datetime.fromisoformat(signature_date_raw)
            
            if dt.tzinfo is None: 
                dt = dt.replace(tzinfo=timezone.utc)
            dt_cl = dt.astimezone(CHILE_TZ)
            formatted_date = dt_cl.strftime("%d-%m-%Y %H:%M CLT")
        except Exception as e:
            pass
        
    return templates.TemplateResponse("visita_verify.html", {
        "request": request,
        "contract": safe_contract,
        "signature_date_clt": formatted_date
    })

@router.get("/dashboard", response_class=HTMLResponse)
async def visita_dashboard(request: Request):
    """Módulo principal para gestión y generación de orden de visitas de corretaje"""
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

    visitas_cursor = adb["visitas"].find(query).sort("created_at", -1).limit(100)
    contracts = await visitas_cursor.to_list(length=100)

    for c in contracts:
        if "_id" in c:
            c["_id"] = str(c["_id"])
        if c.get("created_at"):
            dt_utc = c["created_at"].replace(tzinfo=timezone.utc)
            c["created_at"] = dt_utc.astimezone(CHILE_TZ)
        c["edit_data"] = {
            "visita_code": c.get("visita_code", ""),
            "client_data": c.get("client_data", {}),
            "property_data": c.get("property_data", {}),
            "property_code": c.get("property_code", ""),
            "phone": c.get("phone", ""),
            "origen": c.get("origen", ""),
            "ciudad_firma": c.get("property_data", {}).get("ciudad_firma", "Santiago de Chile"),
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

    return templates.TemplateResponse("visita_dashboard.html", {
        "request": request,
        "contracts": contracts,
        "user_role": user_role,
        "user_username": username or "",
        "user_display_name": user_name_map.get(username or "", username or ""),
        "executives": executives,
        "executive_filter": executive_filter
    })
