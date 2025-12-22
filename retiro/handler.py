# retiro/handler.py → Versión FINAL DEFINITIVA con logo en respuestas al cliente (22-12-2025)

import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from pathlib import Path
from pymongo import MongoClient
from fastapi.responses import HTMLResponse
from config import Config

logger = logging.getLogger(__name__)

# Variable global para detectar modo prueba
ES_MODO_PRUEBA = False

# Ruta al logo
BASE_DIR = Path(__file__).resolve().parent.parent
LOGO_PATH = BASE_DIR / "static" / "logo.png"

def attach_logo(msg):
    if LOGO_PATH.exists():
        with open(LOGO_PATH, "rb") as f:
            img = MIMEImage(f.read())
            img.add_header('Content-ID', '<logo_procasa>')
            msg.attach(img)

def enviar_notificacion_interna(tipo_accion: str, email_cliente: str, codigo_prop: str, email_ejecutivo: str, ip_confirmacion: str = "Desconocida"):
    global ES_MODO_PRUEBA
    email_cliente = email_cliente.lower().strip()

    # Detectar modo prueba
    if "pgalleguillos@procasa.cl" in email_cliente or "galleguil@gmail.com" in email_cliente or "prueba" in email_cliente:
        ES_MODO_PRUEBA = True

    # Destinatarios
    if ES_MODO_PRUEBA:
        destinatarios = ["pgalleguillos@procasa.cl"]
    else:
        destinatarios = ["jpcaro@procasa.cl"]
        if email_ejecutivo and email_ejecutivo.strip() and email_ejecutivo.lower() != "jpcaro@procasa.cl":
            destinatarios.append(email_ejecutivo)

    if not destinatarios:
        logger.warning(f"No hay destinatarios para notificación de {codigo_prop}")
        return

    fecha_formateada = datetime.now().strftime("%d de %B de %Y a las %H:%M horas")

    if tipo_accion == "RETIRO FIRMADO":
        asunto = f"Resciliación Formalizada – Propiedad {codigo_prop}"
        cuerpo = f"""
        <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1e293b; margin: 0; padding: 20px;">
                <div style="max-width: 700px; margin: 0 auto; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden;">
                    <div style="background: #f8fafc; padding: 20px; text-align: center; border-bottom: 1px solid #e2e8f0;">
                        <img src="cid:logo_procasa" alt="Procasa" style="height: 60px;">
                    </div>
                    <div style="padding: 30px;">
                        <h2 style="color: #1e293b; margin-top: 0;">Notificación de Sistema</h2>
                        <p>Estimado equipo,</p>
                        <p>Se ha formalizado la resciliación del encargo de corretaje correspondiente a la siguiente propiedad, mediante confirmación digital del propietario:</p>
                        <br>
                        <table style="width: 100%; border-collapse: collapse;">
                            <tr><td style="padding: 8px 0;"><strong>Código de Propiedad:</strong></td><td>{codigo_prop}</td></tr>
                            <tr><td style="padding: 8px 0;"><strong>Propietario:</strong></td><td>{email_cliente}</td></tr>
                            <tr><td style="padding: 8px 0;"><strong>Fecha y hora de confirmación:</strong></td><td>{fecha_formateada}</td></tr>
                            <tr><td style="padding: 8px 0;"><strong>Dirección IP:</strong></td><td>{ip_confirmacion}</td></tr>
                            <tr><td style="padding: 8px 0;"><strong>Validez legal:</strong></td><td>Firma Electrónica Simple – Ley Nº 19.799</td></tr>
                        </table>
                        <br>
                        <p><strong>Acciones requeridas:</strong></p>
                        <ul style="padding-left: 20px;">
                            <li>Retirar la propiedad de todos los portales externos de publicación.</li>
                            <li>Marcar como no disponible en los sistemas internos de cartera.</li>
                            <li>Archivar la documentación correspondiente en el expediente del propietario.</li>
                        </ul>
                        <br>
                        <p>Queda formalmente rescindido el encargo de venta y/o arriendo.</p>
                        <p>Favor coordinar las gestiones administrativas a la brevedad.</p>
                        <br>
                        <p>Atentamente,<br><strong>Sistema Automático Procasa</strong></p>
                    </div>
                    <div style="background: #f8fafc; padding: 15px; text-align: center; font-size: 12px; color: #64748b; border-top: 1px solid #e2e8f0;">
                        Jorge Pablo Caro Propiedades – División Gestión de Cartera Exclusiva<br>
                        Este es un mensaje automático. No responder.
                    </div>
                </div>
            </body>
        </html>
        """
    else:  # SOLICITUD DE CONTACTO
        asunto = f"Solicitud de Contacto – Propiedad {codigo_prop}"
        cuerpo = f"""
        <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1e293b; margin: 0; padding: 20px;">
                <div style="max-width: 700px; margin: 0 auto; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden;">
                    <div style="background: #f8fafc; padding: 20px; text-align: center; border-bottom: 1px solid #e2e8f0;">
                        <img src="cid:logo_procasa" alt="Procasa" style="height: 60px;">
                    </div>
                    <div style="padding: 30px;">
                        <h2 style="color: #1e293b; margin-top: 0;">Notificación de Sistema</h2>
                        <p>Estimado equipo,</p>
                        <p>El propietario ha solicitado ser contactado antes de proceder con el retiro de su propiedad:</p>
                        <br>
                        <table style="width: 100%; border-collapse: collapse;">
                            <tr><td style="padding: 8px 0;"><strong>Código de Propiedad:</strong></td><td>{codigo_prop}</td></tr>
                            <tr><td style="padding: 8px 0;"><strong>Propietario:</strong></td><td>{email_cliente}</td></tr>
                            <tr><td style="padding: 8px 0;"><strong>Fecha y hora de solicitud:</strong></td><td>{fecha_formateada}</td></tr>
                            <tr><td style="padding: 8px 0;"><strong>Dirección IP:</strong></td><td>{ip_confirmacion}</td></tr>
                        </table>
                        <br>
                        <p><strong>Acción requerida:</strong></p>
                        <ul style="padding-left: 20px;">
                            <li>Contactar al propietario a la brevedad para aclarar sus dudas o requerimientos.</li>
                            <li>No proceder con la baja de la propiedad hasta nueva instrucción.</li>
                        </ul>
                        <br>
                        <p>Favor coordinar el contacto telefónico o por los medios habituales.</p>
                        <br>
                        <p>Atentamente,<br><strong>Sistema Automático Procasa</strong></p>
                    </div>
                    <div style="background: #f8fafc; padding: 15px; text-align: center; font-size: 12px; color: #64748b; border-top: 1px solid #e2e8f0;">
                        Jorge Pablo Caro Propiedades – División Gestión de Cartera Exclusiva<br>
                        Este es un mensaje automático. No responder.
                    </div>
                </div>
            </body>
        </html>
        """

    msg = MIMEMultipart("related")
    msg["From"] = f"Sistema Procasa <{Config.GMAIL_USER}>"
    msg["To"] = ", ".join(destinatarios)
    msg["Subject"] = asunto
    msg.attach(MIMEText(cuerpo, "html"))
    attach_logo(msg)

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
            server.sendmail(Config.GMAIL_USER, destinatarios, msg.as_string())
        logger.info(f"Notificación '{tipo_accion}' enviada para propiedad {codigo_prop} a {', '.join(destinatarios)}")
    except Exception as e:
        logger.error(f"Error enviando notificación interna: {e}")

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
                <div style="text-align: center; margin-bottom: 30px;">
                    <img src="/static/logo.png" alt="Procasa" style="height: 60px;">
                </div>
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
            <div style="text-align: center; margin-bottom: 30px;">
                <img src="/static/logo.png" alt="Procasa" style="height: 60px;">
            </div>
            <h1>Retiro Confirmado con Éxito</h1>
            <p>Estimado/a propietario/a,</p>
            <p>Hemos procesado correctamente la resciliación del encargo correspondiente a su propiedad <strong>código {codigo_norm}</strong>.</p>
            <p>Esta acción constituye una Firma Electrónica Simple conforme a la Ley Nº 19.799.</p>
            <p>Quedamos a su disposición para cualquier consulta futura.</p>
            <p>Muchas gracias por haber confiado en Procasa.</p>
            <div class="footer">
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

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Solicitud Recibida - Procasa</title>
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
            <div style="text-align: center; margin-bottom: 30px;">
                <img src="/static/logo.png" alt="Procasa" style="height: 60px;">
            </div>
            <h1>Solicitud Recibida</h1>
            <p>Un ejecutivo se pondrá en contacto con usted a la brevedad posible.</p>
            <p>Muchas gracias por preferir Procasa.</p>
            <div class="footer">
                <p>Contacto: <a href="https://wa.me/56940904971" class="wa">WhatsApp Corporativo</a> • +56 9 4090 4971</p>
            </div>
        </div>
    </body>
    </html>
    """)