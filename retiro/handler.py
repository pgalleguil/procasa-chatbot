# retiro/handler.py → Versión FINAL DEFINITIVA (22-12-2025)
# - En modo prueba: correo interno SÍ llega a pgalleguillos@procasa.cl para revisión
# - Eliminado soporte@procasa.cl
# - Destinatario principal: jpcaro@procasa.cl
# - Textos internos más profesionales y elegantes
# - Un solo registro por codigo_propiedad

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

def enviar_notificacion_interna(tipo_accion, email_cliente, codigo_prop, email_ejecutivo, ip_confirmacion="Desconocida"):
    global ES_MODO_PRUEBA
    email_cliente = email_cliente.lower().strip()

    # Detectar modo prueba
    if "pgalleguillos@procasa.cl" in email_cliente or "galleguil@gmail.com" in email_cliente or "prueba" in email_cliente:
        ES_MODO_PRUEBA = True

    # En modo prueba: enviamos a pgalleguillos@procasa.cl para revisión
    destinatarios = ["pgalleguillos@procasa.cl"] if ES_MODO_PRUEBA else []
    
    # En producción: jefe siempre + ejecutivo si existe
    if not ES_MODO_PRUEBA:
        destinatarios = ["jpcaro@procasa.cl"]
        if email_ejecutivo and email_ejecutivo.strip():
            destinatarios.append(email_ejecutivo)

    if not destinatarios:
        logger.warning(f"No hay destinatarios para notificación interna de {codigo_prop}")
        return

    asunto = f"Retiro de Propiedad {codigo_prop} - Confirmado por Propietario"
    fecha_actual = datetime.now().strftime("%d de %B de %Y a las %H:%M")

    cuerpo = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1e293b;">
            <h2 style="color: #1e293b;">Notificación de Sistema - Procasa</h2>
            <hr style="border: 1px solid #e2e8f0;">
            <p>Estimado equipo,</p>
            <p>Se ha formalizado la resciliación del encargo de la siguiente propiedad mediante confirmación digital del propietario:</p>
            <br>
            <p><strong>Código de Propiedad:</strong> {codigo_prop}</p>
            <p><strong>Propietario:</strong> {email_cliente}</p>
            <p><strong>Fecha y hora de confirmación:</strong> {fecha_actual}</p>
            <p><strong>Dirección IP:</strong> {ip_confirmacion}</p>
            <p><strong>Validez legal:</strong> Firma Electrónica Simple conforme a la Ley Nº 19.799</p>
            <br>
            <p><strong>Acciones a realizar:</strong></p>
            <ul>
                <li>Proceder con la baja definitiva de la propiedad en todos los portales externos.</li>
                <li>Actualizar el estado en los sistemas internos y marcar como no disponible.</li>
                <li>Archivar la documentación correspondiente en el expediente.</li>
            </ul>
            <br>
            <p>Queda formalmente rescindido el encargo de corretaje.</p>
            <p>Favor coordinar las gestiones administrativas correspondientes a la brevedad.</p>
            <br>
            <p>Atentamente,<br><strong>Sistema Automático Procasa</strong></p>
            <hr style="border: 1px solid #e2e8f0;">
            <p style="color: #64748b; font-size: 12px;">Este es un mensaje automático. No es necesario responder.</p>
        </body>
    </html>
    """

    msg = MIMEMultipart()
    msg["From"] = f"Sistema Procasa <{Config.GMAIL_USER}>"
    msg["To"] = ", ".join(destinatarios)
    if not ES_MODO_PRUEBA and email_ejecutivo and email_ejecutivo.strip():
        msg["Cc"] = email_ejecutivo if email_ejecutivo != "jpcaro@procasa.cl" else ""
    msg["Subject"] = asunto
    msg.attach(MIMEText(cuerpo, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
            server.sendmail(Config.GMAIL_USER, destinatarios + (["jpcaro@procasa.cl"] if not ES_MODO_PRUEBA else []), msg.as_string())
        logger.info(f"Notificación interna enviada para propiedad {codigo_prop} a {', '.join(destinatarios)}")
    except Exception as e:
        logger.error(f"Error enviando notificación interna: {e}")

def enviar_confirmacion_cliente(email_destino: str, codigo: str):
    asunto = f"Confirmación de Retiro - Propiedad {codigo}"
    html_cuerpo = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1e293b; background: #f8fafc; padding: 20px;">
            <div style="max-width: 600px; margin: 0 auto; background: white; padding: 40px; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.1);">
                <h2 style="text-align: center; color: #1e293b;">Retiro Confirmado - Procasa</h2>
                <p>Estimado/a propietario/a,</p>
                <p>Hemos recibido y procesado su confirmación de retiro para la propiedad con código <strong>{codigo}</strong>.</p>
                <p>La resciliación del encargo de venta/arriendo queda formalizada mediante Firma Electrónica Simple, conforme a la Ley Nº 19.799.</p>
                <p>Quedamos a su disposición para cualquier consulta o gestión futura.</p>
                <p>Si en algún momento desea volver a confiar en nuestros servicios, será un placer acompañarlo nuevamente.</p>
                <p style="margin-top: 40px;">Atentamente,<br>Equipo Procasa<br>Jorge Pablo Caro Propiedades</p>
                <hr style="margin: 40px 0;">
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
    ES_MODO_PRUEBA = False

    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    col = db["retiros_propiedades"]
    
    email_norm = email.lower().strip()
    codigo_norm = codigo.upper().strip()

    if "pgalleguillos@procasa.cl" in email_norm or "galleguil@gmail.com" in email_norm:
        ES_MODO_PRUEBA = True

    ya_confirmado = col.find_one({
        "codigo_propiedad": codigo_norm,
        "accion": "retiro_confirmado"
    })

    if ya_confirmado:
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html lang="es">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Ya Confirmado - Procasa</title>
            <style>
                body {{ font-family: 'Inter', Arial, sans-serif; background: #f8fafc; color: #1e293b; padding: 40px; text-align: center; }}
                .card {{ max-width: 600px; margin: 0 auto; background: white; border-radius: 16px; padding: 40px; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1); border: 1px solid #e2e8f0; }}
                h1 {{ color: #1e293b; font-size: 28px; margin-bottom: 20px; }}
                p {{ font-size: 16px; line-height: 1.6; margin: 15px 0; }}
                .footer {{ margin-top: 40px; font-size: 14px; color: #64748b; }}
                .wa {{ color: #4ade80; font-weight: bold; }}
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

    prop_data = db["universo_obelix"].find_one({"codigo": codigo_norm})
    email_ejecutivo = prop_data.get("email_ejecutivo") if prop_data else None

    col.update_one(
        {"codigo_propiedad": codigo_norm},
        {
            "$set": {
                "email_propietario": email_norm,
                "accion": "retiro_confirmado",
                "fecha_confirmacion": datetime.now(timezone.utc),
                "ip": ip,
                "ley": "19.799",
                "fecha_actualizacion": datetime.now(timezone.utc)
            },
            "$setOnInsert": {
                "documento": "Carta_Retiro_Procasa.pdf",
                "fecha_envio": datetime.now(timezone.utc),
                "notas": "Creado al confirmar retiro"
            }
        },
        upsert=True
    )

    db["universo_obelix"].update_one(
        {"codigo": codigo_norm},
        {"$set": {
            "disponible": False,
            "fecha_no_disponible": datetime.now(timezone.utc),
            "motivo": "retiro_propietario"
        }}
    )

    enviar_notificacion_interna("RETIRO FIRMADO", email_norm, codigo_norm, email_ejecutivo, ip)
    enviar_confirmacion_cliente(email_norm, codigo_norm)

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Retiro Confirmado - Procasa</title>
        <style>
            body {{ font-family: 'Inter', Arial, sans-serif; background: #f8fafc; color: #1e293b; padding: 40px; text-align: center; }}
            .card {{ max-width: 600px; margin: 0 auto; background: white; border-radius: 16px; padding: 40px; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1); border: 1px solid #e2e8f0; }}
            h1 {{ color: #1e293b; font-size: 28px; margin-bottom: 20px; }}
            p {{ font-size: 16px; line-height: 1.6; margin: 15px 0; }}
            .footer {{ margin-top: 40px; font-size: 14px; color: #64748b; }}
            .wa {{ color: #4ade80; font-weight: bold; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Retiro Confirmado con Éxito</h1>
            <p>Estimado/a propietario/a,</p>
            <p>Hemos procesado correctamente la resciliación del encargo de su propiedad <strong>código {codigo_norm}</strong>.</p>
            <p>Esta confirmación constituye una Firma Electrónica Simple conforme a la Ley Nº 19.799.</p>
            <p>Le hemos enviado un correo con el detalle de esta gestión.</p>
            <p>Quedamos a su disposición para cualquier consulta futura.</p>
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

    if "pgalleguillos@procasa.cl" in email_norm or "galleguil@gmail.com" in email_norm:
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

    enviar_notificacion_interna("SOLICITUD DE CONTACTO", email_norm, codigo_norm, email_ejecutivo, ip)

    return HTMLResponse("""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <style>
            body { font-family: Arial, sans-serif; background: #f8fafc; color: #1e293b; padding: 40px; text-align: center; }
            h1 { color: #1e293b; }
        </style>
    </head>
    <body>
        <h1>Solicitud Recibida</h1>
        <p>Un asesor inmobiliario se pondrá en contacto con usted a la brevedad.</p>
        <p>Gracias por preferir Procasa.</p>
    </body>
    </html>
    """)