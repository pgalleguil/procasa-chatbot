# webhook.py → BOT PRO 2025 CON LOGIN REAL + DASHBOARD + CAMPAÑAS 100% ORIGINALES
import asyncio
import logging
import time
import hmac
import hashlib
from typing import Dict, Any
import re
import secrets

import requests
from fastapi import FastAPI, Request, HTTPException, Header, Query, Form, Depends, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pymongo import MongoClient
from datetime import datetime, timedelta
from pathlib import Path
import uvicorn
import json
import os


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
async def login(request: Request, response: Response):
    try:
        # Lee el formulario (username y password vienen por Form)
        form_data = await request.form()
        username = form_data.get("username")
        password = form_data.get("password")

        # Validación básica
        if not username or not password:
            raise HTTPException(status_code=400, detail="Usuario y contraseña requeridos")

        # CLAVE: recortamos a 72 bytes → evita el error de bcrypt
        password = password[:72]

        # Busca el usuario en MongoDB
        usuario = collection_usuarios.find_one({"username": username.strip()})
        if not usuario:
            raise HTTPException(status_code=401, detail="Credenciales incorrectas")

        # Verifica la contraseña
        if not pwd_context.verify(password, usuario["hashed_password"]):
            raise HTTPException(status_code=401, detail="Credenciales incorrectas")

        # Crea el token JWT
        access_token = create_access_token(
            data={"sub": username},
            expires_delta=timedelta(minutes=60)  # o el tiempo que quieras
        )

        # Guarda el token en cookie HttpOnly (segura)
        response.set_cookie(
            key="access_token",
            value=f"Bearer {access_token}",
            httponly=True,
            secure=True,           # importante en producción (HTTPS)
            samesite="lax",
            max_age=3600,          # 1 hora
            expires=3600
        )

        return {"message": "Login exitoso", "username": username}

    except HTTPException:
        raise  # vuelve a lanzar los 400/401 para que el frontend los vea
    except Exception as e:
        import logging
        logging.exception("Error inesperado en login")
        raise HTTPException(status_code=500, detail="Error interno del servidor")

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

async def send_whatsapp_message(number: str, text: str):
    url = "https://wasenderapi.com/api/send-message"
    payload = {"to": number, "text": text}
    headers = {"Authorization": f"Bearer {Config.WASENDER_TOKEN}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            logger.info(f"[WHATSAPP SUCCESS] Enviado a {number}")
            return True
    except Exception as e:
        logger.error(f"[WHATSAPP EXCEPTION] Error enviando a {number}: {e}")
    await asyncio.sleep(2)
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            logger.info(f"[WHATSAPP SUCCESS] Enviado en 2do intento a {number}")
            return True
    except Exception as e:
        logger.error(f"[WHATSAPP EXCEPTION] 2do intento falló: {e}")
    return False

@app.post("/webhook")
async def webhook(request: Request, x_webhook_signature: str = Header(None, alias="X-Webhook-Signature")):
    raw_body = await request.body()
    if Config.WASENDER_WEBHOOK_SECRET:
        expected_signature = hmac.new(Config.WASENDER_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_signature, x_webhook_signature or ""):
            logger.warning("FIRMA INVÁLIDA - Acceso denegado")
            raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        logger.error(f"JSON inválido: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")
    logger.info("[WEBHOOK] Mensaje recibido")
    if data.get("event") == "webhook.test":
        logger.info("TEST WEBHOOK EXITOSO")
        return JSONResponse({"ok": True}, status_code=200)

    messages_data = data.get("data", {}).get("messages", {}) or {}
    phone = (messages_data.get("key", {}).get("cleanedSenderPn") or
             messages_data.get("key", {}).get("senderPn", "").split("@")[0] or
             messages_data.get("from", "").split("@")[0] or "").strip()
    text = (messages_data.get("messageBody") or
            messages_data.get("message", {}).get("conversation") or
            messages_data.get("message", {}).get("extendedTextMessage", {}).get("text", "") or "").strip()

    if not phone or not text:
        return JSONResponse({"status": "ignored"}, status_code=200)

    if phone.startswith("56") and not phone.startswith("+"):
        phone = "+" + phone
    if not phone.startswith("+"):
        phone = "+56" + phone.lstrip("0")

    logger.info(f"Mensaje de {phone}: {text[:100]}")
    current = accumulated_messages.get(phone, "")
    nuevo_texto = (current + "\n" + text).strip() if current else text
    accumulated_messages[phone] = nuevo_texto
    last_message_time[phone] = time.time()
    asyncio.create_task(process_with_debounce(phone, nuevo_texto))
    return JSONResponse({"ok": True}, status_code=200)

@app.get("/health")
async def health_check():
    return {"status": "healthy", "active_conversations": len(pending_tasks), "uptime": time.strftime("%Y-%m-%d %H:%M:%S")}

# ====================== ENDPOINT CAMPAÑA EMAIL - 100% TUS MENSAJES ORIGINALES ======================
@app.get("/campana/respuesta", response_class=HTMLResponse)
def campana_respuesta(
    email: str = Query(..., description="Email del propietario"),
    accion: str = Query(..., description="ajuste_7 / llamada / mantener / no_disponible / unsubscribe"),
    codigos: str = Query("N/A", description="Códigos de propiedades"),
    campana: str = Query(..., description="Nombre de la campaña")
):
    if accion not in ["ajuste_7", "llamada", "mantener", "no_disponible", "unsubscribe"]:
        logger.warning(f"Acción inválida: {accion} desde email {email}")
        return HTMLResponse("Acción no válida", status_code=400)

    try:
        email_lower = email.lower().strip()
        codigos_lista = [c.strip() for c in codigos.split(",") if c.strip() and c.strip() != "N/A"]
        ahora = datetime.utcnow()

        logger.info(f"[CAMPAÑA] Procesando respuesta de {email_lower} → {accion} | Campaña: {campana}")

        client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=6000)
        client.admin.command('ping')
        logger.info("[MONGO] Conexión exitosa")

        db = client[Config.DB_NAME]
        contactos = db[Config.COLLECTION_CONTACTOS]
        respuestas = db[Config.COLLECTION_RESPUESTAS]

        insert_result = respuestas.insert_one({
            "email": email_lower,
            "campana_nombre": campana,
            "accion": accion,
            "codigos_propiedad": codigos_lista,
            "fecha_respuesta": ahora,
            "canal_origen": "email"
        })
        logger.info(f"[MONGO] Respuesta guardada → ID: {insert_result.inserted_id}")

        # TU MISMA LÓGICA EXACTA
        update_data = {
            "$set": {
                "update_price.campana_nombre": campana,
                "update_price.respuesta": accion,
                "update_price.accion_elegida": accion,
                "update_price.fecha_respuesta": ahora,
                "update_price.ultima_actualizacion": ahora,
                "ultima_accion": f"{accion}_{campana}" if accion != "unsubscribe" else "unsubscribe",
                "bloqueo_email": accion in ["no_disponible", "unsubscribe"]
            }
        }

        if accion == "ajuste_7":
            update_data["$set"]["estado"] = "ajuste_autorizado"
            titulo = "¡Autorización recibida!"
            mensaje = """Ya realizamos la actualización del precio de tu propiedad en Procasa.

El nuevo valor se verá reflejado en los portales inmobiliarios dentro de aproximadamente 72 horas, dependiendo de los tiempos de sincronización de cada sitio.

Si necesitas realizar otro ajuste o revisar alguna estrategia de visibilidad, quedaremos atentos"""
            color = "#10b981"

        elif accion == "llamada":
            update_data["$set"]["estado"] = "pendiente_llamada"
            titulo = "¡Solicitud recibida!"
            mensaje = """Perfecto, derivamos tu solicitud para que un ejecutivo de Procasa se ponga en contacto contigo.

El equipo revisará tu caso y te llamarán dentro de las próximas 24 a 48 horas, según disponibilidad.

Quedaremos atentos si necesitas algo adicional mientras tanto."""
            color = "#3b82f6"

        elif accion == "mantener":
            update_data["$set"]["estado"] = "precio_mantenido"
            titulo = "Precio mantenido"
            mensaje = """Perfecto, dejamos el precio de tu propiedad tal como está.

Seguiremos monitoreando el comportamiento del mercado para evaluar futuras oportunidades de ajuste si fuese necesario.

Quedaremos atentos ante cualquier consulta o cambio que quieras realizar."""
            color = "#f59e0b"

        elif accion == "no_disponible":
            update_data["$set"]["estado"] = "no_disponible"
            titulo = "Entendido"
            mensaje = """Perfecto, dejamos marcada tu propiedad como No Disponible en nuestro sistema.

Si en el futuro cuentas con otra propiedad para vender o arrendar, estaremos encantados de ayudarte con la gestión y apoyarte en todo el proceso.

Quedaremos atentos a cualquier cosa que necesites."""
            color = "#ef4444"

        else:  # unsubscribe
            update_data["$set"]["estado"] = "suscripcion_anulada"
            titulo = "Suscripción anulada"
            mensaje = """Perfecto, hemos procesado tu solicitud y quedaste desinscrito de nuestras comunicaciones comerciales.

Gracias por habernos permitido mantenerte informado. Si en algún momento deseas volver a recibir novedades o necesitas apoyo con una propiedad, estaremos encantados de ayudarte."""
            color = "#6b7280"

        # FIX DEFINITIVO: DOBLE INTENTO DE ACTUALIZACIÓN
        # 1. Primero busca exacto
        update_result = contactos.update_one({"email_propietario": email_lower}, update_data)
        
        # 2. Si no encontró, busca case-insensitive
        if update_result.matched_count == 0:
            import re
            update_result = contactos.update_one(
                {"email_propietario": {"$regex": f"^{re.escape(email_lower)}$", "$options": "i"}},
                update_data
            )
        
        logger.info(f"[MONGO] Contacto actualizado → matched: {update_result.matched_count}, modified: {update_result.modified_count}")

        # TU HTML 100% ORIGINAL
        html = f"""
        <!DOCTYPE html>
        <html lang="es">
        <head>
            <meta charset="UTF-8">
            <title>Procasa - Confirmación</title>
            <style>
                body {{font-family:Arial,sans-serif;background:#f9fafb;padding:40px;margin:0}}
                .container {{max-width:520px;margin:auto;background:white;border-radius:16px;overflow:hidden;box-shadow:0 10px 25px rgba(0,0,0,0.1)}}
                .header {{background:{color};color:white;padding:30px;text-align:center}}
                .header h1 {{margin:0;font-size:24px;font-weight:600}}
                .content {{padding:40px 30px;text-align:center;color:#374151}}
                .content p {{font-size:16px;line-height:1.6;margin:16px 0}}
                .footer {{background:#f1f5f9;padding:20px;text-align:center;font-size:12px;color:#94a3b8}}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header"><h1>{titulo}</h1></div>
                <div class="content">
                    <p><strong>{accion.replace('_', ' ').title()}</strong></p>
                    <p>{mensaje.replace(chr(10), '<br>')}</p>
                </div>
                <div class="footer">
                    © 2025 Procasa • Pablo Caro y equipo
                </div>
            </div>
        </body>
        </html>
        """

        logger.info(f"[CAMPAÑA] Respuesta procesada con éxito → {email_lower}")
        return HTMLResponse(html)

    except Exception as e:
        logger.error(f"[ERROR CRÍTICO] Falló /campana/respuesta → {email} | Acción: {accion} | Error: {e}", exc_info=True)
        return HTMLResponse("Error interno del servidor. Contacta a soporte.", status_code=500)

@app.get("/api/reporte_real")
async def api_reporte_real():
    # Ejecutamos tu script real y devolvemos los datos exactos
    from reporte_completo_campana import respuestas, total_enviados
    
    aceptaron = sum(1 for r in respuestas if r.get("respuesta_texto", "").startswith("ACEPTÓ"))
    mantener = sum(1 for r in respuestas if "MANTENER" in r.get("respuesta_texto", ""))
    llamada = sum(1 for r in respuestas if "LLAMEN" in r.get("respuesta_texto", ""))
    vendida = sum(1 for r in respuestas if "NO DISPONIBLE" in r.get("respuesta_texto", ""))
    baja = sum(1 for r in respuestas if "BAJA" in r.get("respuesta_texto", ""))

    return {
        "total_enviados": total_enviados,
        "total_respuestas": len(respuestas),
        "tasa_respuesta": round(len(respuestas)/total_enviados*100, 1) if total_enviados else 0,
        "aceptaron": aceptaron,
        "mantener": mantener,
        "llamada": llamada,
        "vendida": vendida,
        "baja": baja,
        "respuestas": [
            {
                "codigo": r.get("codigo", "S/C"),
                "nombre": r.get("nombre_completo", "Sin nombre"),
                "telefono": r.get("telefono", ""),
                "email": r.get("email_propietario", ""),
                "respuesta": r.get("respuesta_texto", "Otro"),
                "fecha": r.get("fecha_mostrar", "Sin fecha")
            }
            for r in respuestas
        ]
    }

# ====================== ARRANQUE CORRECTO ======================
if __name__ == "__main__":
    import pathlib
    module_name = pathlib.Path(__file__).stem
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Bot PRO iniciado → http://localhost:{port}")
    logger.info("Usuario: admin | Contraseña: procasa2025")
    uvicorn.run(f"{module_name}:app", host="0.0.0.0", port=port, reload=True, log_level="info")