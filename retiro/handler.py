import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pymongo import MongoClient
from fastapi.responses import HTMLResponse
from config import Config

logger = logging.getLogger(__name__)

def enviar_notificacion_interna(tipo_accion, email_cliente, codigo_prop, email_ejecutivo):
    """Envía la alerta al ejecutivo y a la jefatura."""
    asunto = f"ALERTA: {tipo_accion} - Propiedad {codigo_prop}"
    destinatarios = [email_ejecutivo] if email_ejecutivo else ["soporte@procasa.cl"]
    cc = ["jpcaro@procasa.cl"]
    
    cuerpo = f"""
    <html>
        <body style="font-family: Arial; line-height: 1.6;">
            <h2>Notificación de Sistema - Procasa</h2>
            <p><strong>Acción:</strong> {tipo_accion}</p>
            <p><strong>Propiedad:</strong> {codigo_prop}</p>
            <p><strong>Cliente:</strong> {email_cliente}</p>
            <p><strong>Ejecutivo Responsable:</strong> {email_ejecutivo}</p>
            <hr>
            <p>Favor proceder con las gestiones administrativas correspondientes.</p>
        </body>
    </html>
    """
    
    msg = MIMEMultipart()
    msg["From"] = f"Sistema Procasa <{Config.GMAIL_USER}>"
    msg["To"] = ", ".join(destinatarios)
    msg["Cc"] = ", ".join(cc)
    msg["Subject"] = asunto
    msg.attach(MIMEText(cuerpo, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
            server.sendmail(Config.GMAIL_USER, destinatarios + cc, msg.as_string())
        logger.info(f"Notificación interna enviada para {codigo_prop}")
    except Exception as e:
        logger.error(f"Error enviando notificación interna: {e}")

async def handle_retiro_confirmacion(email: str, codigo: str, ip: str):
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    # Obtener datos del ejecutivo responsable
    prop_data = db["universo_obelix"].find_one({"codigo": codigo.upper()})
    email_ejecutivo = prop_data.get("email_ejecutivo") if prop_data else None

    # 1. Registrar firma legal
    db["retiros_propiedades"].insert_one({
        "email_propietario": email,
        "codigo_propiedad": codigo,
        "accion": "retiro_confirmado",
        "fecha": datetime.now(timezone.utc),
        "ip": ip,
        "ley": "19.799"
    })

    # 2. Desactivar propiedad
    db["universo_obelix"].update_one(
        {"codigo": codigo.upper()},
        {"$set": {"disponible": False, "fecha_no_disponible": datetime.now(timezone.utc), "motivo": "retiro_propietario"}}
    )

    # 3. Notificar a JPCARO y Ejecutivo
    enviar_notificacion_interna("RETIRO FIRMADO", email, codigo, email_ejecutivo)

    return HTMLResponse("<h1>Confirmado</h1><p>El retiro ha sido procesado legalmente bajo la Ley 19.799.</p>")

async def handle_solicitud_contacto(email: str, codigo: str, ip: str):
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    prop_data = db["universo_obelix"].find_one({"codigo": codigo.upper()})
    email_ejecutivo = prop_data.get("email_ejecutivo") if prop_data else None

    # 1. Registrar en BD
    db["retiros_propiedades"].insert_one({
        "email_propietario": email,
        "codigo_propiedad": codigo,
        "accion": "solicitud_contacto_ejecutivo",
        "fecha": datetime.now(timezone.utc),
        "ip": ip
    })

    # 2. Notificar a JPCARO y Ejecutivo
    enviar_notificacion_interna("SOLICITUD DE CONTACTO", email, codigo, email_ejecutivo)

    return HTMLResponse("<h1>Solicitud Recibida</h1><p>Un ejecutivo se pondrá en contacto con usted pronto.</p>")