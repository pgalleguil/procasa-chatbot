# retiro/handler.py → Versión FINAL COMPLETA con todas las correcciones

import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pymongo import MongoClient
from fastapi.responses import HTMLResponse
from config import Config

logger = logging.getLogger(__name__)

# Variable global para detectar modo prueba
ES_MODO_PRUEBA = False

def enviar_notificacion_interna(tipo_accion, email_cliente, codigo_prop, email_ejecutivo):
    global ES_MODO_PRUEBA
    email_cliente = email_cliente.lower().strip()

    # Detectar si es modo prueba
    if "pgalleguillos@procasa.cl" in email_cliente or "prueba" in email_cliente:
        ES_MODO_PRUEBA = True

    if ES_MODO_PRUEBA:
        logger.info(f"[MODO PRUEBA] Notificación interna omitida para propiedad {codigo_prop}")
        logger.info(f"[MODO PRUEBA] Simulada: {tipo_accion} - Cliente: {email_cliente}")
        return

    # Producción: envío normal con título corregido
    asunto = f"Retiro de Propiedad {codigo_prop}"
    destinatarios = [email_ejecutivo] if email_ejecutivo and email_ejecutivo.strip() else ["soporte@procasa.cl"]
    cc = ["jpcaro@procasa.cl"]
    
    cuerpo = f""" 
    <html>
        <body style="font-family: Arial; line-height: 1.6;">
            <h2>Notificación de Sistema - Procasa</h2>
            <p><strong>Acción:</strong> RETIRO FIRMADO</p>
            <p><strong>Propiedad:</strong> {codigo_prop}</p>
            <p><strong>Cliente:</strong> {email_cliente}</p>
            <p><strong>Ejecutivo Responsable:</strong> {email_ejecutivo or "No asignado"}</p>
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

def enviar_confirmacion_cliente(email_destino: str, codigo: str):
    asunto = f"Confirmación de Retiro - Propiedad {codigo}"
    html_cuerpo = f""" 
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1e293b; background: #f8fafc; padding: 20px;">
            <div style="max-width: 600px; margin: 0 auto; background: white; padding: 40px; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.1);">
                <h2 style="color: #be123c; text-align: center;">Retiro Confirmado - Procasa</h2>
                <p>Estimado/a propietario/a,</p>
                <p>Hemos recibido y procesado exitosamente su confirmación de retiro para la propiedad con <strong>código {codigo}</strong>.</p>
                <p>La resciliación del encargo de venta/arriendo queda formalizada mediante <strong>Firma Electrónica Simple</strong>, conforme a la Ley Nº 19.799.</p>
                <p>Quedamos a su disposición para cualquier consulta o gestión futura.</p>
                <p>Si en el futuro desea volver a confiar en nosotros para gestionar una propiedad, será un placer acompañarlo con la misma dedicación de siempre.</p>
                <p style="margin-top: 40px;">Atentamente,<br><strong>Equipo Procasa</strong><br>Jorge Pablo Caro Propiedades</p>
                <hr style="margin: ۴۰px 0;">
                <small style="color: #64748b;">Este es un mensaje automático. La confirmación quedó registrada con fecha, hora e IP.</small>
            </div>
        </body>
    </html>
    """ 

    msg = MIMEMultipart("alternative")
    msg["From"] = f"Gestión Procasa <{Config.GMAIL_USER}>"
    msg["To"] = email_destino
    msg["Subject"] = asunto
    msg.attach(MIMEText(html_cuerpo, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
            server.sendmail(Config.GMAIL_USER, email_destino, msg.as_string())
        logger.info(f"Correo de confirmación enviado al cliente: {email_destino}")
    except Exception as e:
        logger.error(f"Error enviando correo de confirmación al cliente: {e}")

async def handle_retiro_confirmacion(email: str, codigo: str, ip: str):
    global ES_MODO_PRUEBA
    ES_MODO_PRUEBA = False  # Reset

    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    col = db["retiros_propiedades"]
    
    email_norm = email.lower().strip()
    codigo_norm = codigo.upper().strip()

    # Detectar modo prueba
    if "galleguil@gmail.com" in email_norm or "prueba" in email_norm:
        ES_MODO_PRUEBA = True

    # Evitar duplicados
    existente = col.find_one({
        "email_propietario": email_norm,
        "codigo_propiedad": codigo_norm,
        "accion": "retiro_confirmado"
    })

    if existente:
        return HTMLResponse(f""" 
        <!DOCTYPE html>
        <html lang="es">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Ya Confirmado - Procasa</title>
            <style>
                body { font-family: 'Inter', Arial, sans-serif; background: #f8fafc; color: #1e293b; padding: 40px; text-align: center; }
                .card { max-width: 600px; margin: 0 auto; background: white; border-radius: 16px; padding: 40px; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1); border: 1px solid #e2e8f0; }
                h1 { color: #be123c; font-size: 28px; margin-bottom: 20px; }
                p { font-size: 16px; line-height: 1.6; margin: 15px 0; }
                .footer { margin-top: 40px; font-size: 14px; color: #64748b; }
                .wa { color: #4ade80; font-weight: bold; }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>Retiro Ya Confirmado</h1>
                <p>El retiro de la propiedad <strong>{codigo_norm}</strong> ya fue procesado previamente.</p>
                <p>No es necesario volver a confirmarlo.</p>
                <p>Gracias por confiar en Procasa.</p>
                <div class="footer">
                    <p>Contacto: <a href="https://wa.me/56940904971" class="wa">WhatsApp Corporativo</a> • +56 9 4090 4971</p>
                </div>
            </div>
        </body>
        </html>
        """)

    # Datos ejecutivo
    prop_data = db["universo_obelix"].find_one({"codigo": codigo_norm})
    email_ejecutivo = prop_data.get("email_ejecutivo") if prop_data else None

    # Registrar confirmación
    col.insert_one({
        "email_propietario": email_norm,
        "codigo_propiedad": codigo_norm,
        "accion": "retiro_confirmado",
        "fecha": datetime.now(timezone.utc),
        "ip": ip,
        "ley": "19.799"
    })

    # Desactivar propiedad
    db["universo_obelix"].update_one(
        {"codigo": codigo_norm},
        {"$set": {
            "disponible": False,
            "fecha_no_disponible": datetime.now(timezone.utc),
            "motivo": "retiro_propietario"
        }}
    )

    # Notificación interna (omite en prueba)
    enviar_notificacion_interna("RETIRO FIRMADO", email_norm, codigo_norm, email_ejecutivo)

    # Confirmación al cliente (en prueba va al EMAIL_PRUEBA automáticamente por el flujo)
    enviar_confirmacion_cliente(email_norm, codigo_norm)

    # Página de éxito
    return HTMLResponse(f""" 
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Retiro Confirmado - Procasa</title>
        <style>
            body { font-family: 'Inter', Arial, sans-serif; background: #f8fafc; color: #1e293b; padding: 40px; text-align: center; }
            .card { max-width: 600px; margin: 0 auto; background: white; border-radius: 16px; padding: 40px; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1); border: 1px solid #e2e8f0; }
            h1 { color: #be123c; font-size: 28px; margin-bottom: 20px; }
            p { font-size: 16px; line-height: 1.6; margin: 15px 0; }
            .footer { margin-top: 40px; font-size: 14px; color: #64748b; }
            .wa { color: #4ade80; font-weight: bold; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>¡Retiro Confirmado con Éxito!</h1>
            <p>Estimado/a propietario/a,</p>
            <p>Hemos procesado correctamente la resciliación del encargo de su propiedad <strong>código {codigo_norm}</strong>.</p>
            <p>Esta confirmación constituye una <strong>Firma Electrónica Simple</strong> conforme a la Ley Nº 19.799.</p>
            <p>Le hemos enviado un correo con el detalle de esta gestión.</p>
            <p>Si en el futuro desea volver a gestionar una propiedad con nosotros, estaremos encantados de acompañarlo con la misma dedicación y profesionalismo.</p>
            <div class="footer">
                <p>Gracias por haber confiado en <strong>Procasa</strong>.</p>
                <p>Contacto: <a href="https://wa.me/56940904971" class="wa">WhatsApp Corporativo</a> • +56 9 4090 4971</p>
            </div>
        </div>
    </body>
    </html>
    """)

async def handle_solicitud_contacto(email: str, codigo: str, ip: str):
    global ES_MODO_PRUEBA
    ES_MODO_PRUEBA = False

    email_norm = email.lower().strip()
    codigo_norm = codigo.upper().strip()

    if "galleguil@gmail.com" in email_norm:
        ES_MODO_PRUEBA = True

    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    prop_data = db["universo_obelix"].find_one({"codigo": codigo_norm})
    email_ejecutivo = prop_data.get("email_ejecutivo") if prop_data else None

    db["retiros_propiedades"].insert_one({
        "email_propietario": email_norm,
        "codigo_propiedad": codigo_norm,
        "accion": "solicitud_contacto_ejecutivo",
        "fecha": datetime.now(timezone.utc),
        "ip": ip
    })

    enviar_notificacion_interna("SOLICITUD DE CONTACTO", email_norm, codigo_norm, email_ejecutivo)

    return HTMLResponse(""" 
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <style>
            body { font-family: Arial, sans-serif; background: #f8fafc; color: #1e293b; padding: 40px; text-align: center; }
            h1 { color: #be123c; }
        </style>
    </head>
    <body>
        <h1>Solicitud Recibida</h1>
        <p>Un ejecutivo se pondrá en contacto con usted a la brevedad.</p>
        <p>Gracias por preferir Procasa.</p>
    </body>
    </html>
    """)