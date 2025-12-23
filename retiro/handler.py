# retiro/handler.py → Versión PREMIUM DEFINITIVA con UX mejorada y hora Chile en DB (22-12-2025)

import logging
import smtplib
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from pathlib import Path
from pymongo import MongoClient
from fastapi.responses import HTMLResponse
from config import Config

logger = logging.getLogger(__name__)

# Zona horaria de Chile
TZ_CHILE = ZoneInfo("America/Santiago")

# Ruta al logo y estática
BASE_DIR = Path(__file__).resolve().parent.parent
LOGO_PATH = BASE_DIR / "static" / "logo.png"

# --- DISEÑO UI PROFESIONAL PARA EL CLIENTE ---
CSS_ESTILOS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600&display=swap');
    body { font-family: 'Inter', sans-serif; background-color: #f8fafc; color: #334155; margin: 0; padding: 0; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .container { max-width: 650px; width: 90%; background: #ffffff; border-radius: 24px; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.05), 0 10px 10px -5px rgba(0, 0, 0, 0.02); overflow: hidden; border: 1px solid #e2e8f0; margin: 20px auto; }
    .header { padding: 50px 40px 30px 40px; text-align: center; }
    .logo { width: 240px; height: auto; margin-bottom: 10px; }
    .content { padding: 0 50px 50px 50px; text-align: center; }
    h1 { color: #0f172a; font-size: 28px; font-weight: 600; margin-bottom: 24px; letter-spacing: -0.02em; }
    p { font-size: 16px; line-height: 1.8; color: #475569; margin-bottom: 20px; }
    .highlight { color: #0f172a; font-weight: 600; border-bottom: 2px solid #e2e8f0; }
    .strategy-note { background: #f0f7ff; border-left: 5px solid #3b82f6; padding: 25px; text-align: left; margin: 35px 0; border-radius: 4px 12px 12px 4px; }
    .strategy-note p { margin-bottom: 0; font-style: italic; font-size: 15px; color: #1e40af; line-height: 1.6; }
    .footer { background: #f8fafc; padding: 35px; border-top: 1px solid #e2e8f0; font-size: 14px; color: #64748b; text-align: center; }
    .btn-wa { display: inline-block; background: #22c55e; color: white; padding: 14px 30px; border-radius: 12px; text-decoration: none; font-weight: 600; transition: all 0.3s ease; margin-top: 15px; box-shadow: 0 4px 6px -1px rgba(34, 197, 94, 0.2); }
    .btn-wa:hover { background: #16a34a; transform: translateY(-2px); }
    .badge-legal { display: inline-block; background: #f1f5f9; padding: 8px 16px; border-radius: 8px; font-size: 12px; color: #94a3b8; margin-top: 25px; text-transform: uppercase; letter-spacing: 0.1em; font-weight: 600; }
</style>
"""

def attach_logo(msg):
    """Adjunta el logo para correos que lo requieran."""
    if LOGO_PATH.exists():
        with open(LOGO_PATH, "rb") as f:
            img = MIMEImage(f.read())
            img.add_header('Content-ID', '<logo_procasa>')
            msg.attach(img)

def enviar_notificacion_interna(tipo_accion: str, email_cliente: str, codigo_prop: str, email_ejecutivo: str, ip_confirmacion: str = "Desconocida"):
    """Envía el reporte detallado al equipo de Procasa."""
    email_cliente = email_cliente.lower().strip()

    # Detección de modo prueba
    es_prueba = (
        "pgalleguillos@procasa.cl" in email_cliente or
        "galleguil@gmail.com" in email_cliente or
        "prueba" in email_cliente
    )

    if es_prueba:
        destinatarios = ["pgalleguillos@procasa.cl"]
        log_text = "pgalleguillos@procasa.cl (modo prueba)"
    else:
        destinatarios = [email_ejecutivo.lower()] if email_ejecutivo and email_ejecutivo.strip() else []
        destinatarios.append("jpcaro@procasa.cl")
        destinatarios.append("pgalleguillos@procasa.cl")
        destinatarios = list(dict.fromkeys(destinatarios))
        log_text = ", ".join(destinatarios)

    if not destinatarios:
        logger.warning(f"No hay destinatarios para notificación de {codigo_prop}")
        return

    ahora_chile = datetime.now(TZ_CHILE)
    fecha_formateada = ahora_chile.strftime("%d de %B de %Y a las %H:%M horas")

    if tipo_accion == "RETIRO FIRMADO":
        asunto = f"Resciliación Formalizada – Propiedad {codigo_prop}"
        cuerpo = f"""
        <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1e293b; margin: 0; padding: 20px;">
                <div style="max-width: 700px; margin: 0 auto; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden;">
                    <div style="padding: 30px;">
                        <h2 style="color: #1e293b; margin-top: 0;">Notificación de Sistema</h2>
                        <p>Estimado equipo,</p>
                        <p>Se ha formalizado la resciliación del encargo de corretaje mediante confirmación digital del propietario:</p>
                        <br>
                        <table style="width: 100%; border-collapse: collapse;">
                            <tr><td style="padding: 8px 0;"><strong>Código Propiedad:</strong></td><td>{codigo_prop}</td></tr>
                            <tr><td style="padding: 8px 0;"><strong>Propietario:</strong></td><td>{email_cliente}</td></tr>
                            <tr><td style="padding: 8px 0;"><strong>Fecha/Hora Chile:</strong></td><td>{fecha_formateada}</td></tr>
                            <tr><td style="padding: 8px 0;"><strong>Dirección IP:</strong></td><td>{ip_confirmacion}</td></tr>
                            <tr><td style="padding: 8px 0;"><strong>Validez:</strong></td><td>Firma Electrónica Simple – Ley Nº 19.799</td></tr>
                        </table>
                        <br>
                        <p><strong>Acciones requeridas:</strong></p>
                        <ul style="padding-left: 20px;">
                            <li>Retirar de portales externos.</li>
                            <li>Marcar como no disponible en sistemas internos.</li>
                            <li>Archivar documentación en el expediente.</li>
                        </ul>
                        <br>
                        <p>Atentamente,<br><strong>Sistema Automático Procasa</strong></p>
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
                    <div style="padding: 30px;">
                        <h2 style="color: #1e293b; margin-top: 0;">Notificación de Sistema</h2>
                        <p>Estimado equipo,</p>
                        <p>El propietario solicita contacto con un consultor antes de proceder con el retiro:</p>
                        <br>
                        <table style="width: 100%; border-collapse: collapse;">
                            <tr><td style="padding: 8px 0;"><strong>Código Propiedad:</strong></td><td>{codigo_prop}</td></tr>
                            <tr><td style="padding: 8px 0;"><strong>Propietario:</strong></td><td>{email_cliente}</td></tr>
                            <tr><td style="padding: 8px 0;"><strong>Fecha/Hora Chile:</strong></td><td>{fecha_formateada}</td></tr>
                        </table>
                        <br>
                        <p><strong>Acción requerida:</strong></p>
                        <ul style="padding-left: 20px;">
                            <li>Un consultor debe contactar al propietario a la brevedad.</li>
                        </ul>
                        <br>
                        <p>Atentamente,<br><strong>Sistema Automático Procasa</strong></p>
                    </div>
                </div>
            </body>
        </html>
        """

    msg = MIMEMultipart("alternative")
    msg["From"] = f"Gestión Procasa <{Config.GMAIL_USER}>"
    msg["To"] = ", ".join(destinatarios)
    msg["Subject"] = asunto
    msg.attach(MIMEText(cuerpo, "html"))

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        logger.info(f"Notificación interna enviada: {asunto} → {log_text}")
    except Exception as e:
        logger.error(f"Error enviando notificación interna: {e}")

async def handle_retiro_confirmacion(email: str, codigo: str, ip: str):
    """Procesa la confirmación de retiro y muestra pantalla de éxito al cliente."""
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    col = db["retiros_propiedades"]
    
    email_norm = email.lower().strip()
    codigo_norm = codigo.upper().strip()

    # Verificar si ya existe el retiro
    ya_confirmado = col.find_one({
        "codigo_propiedad": codigo_norm,
        "accion": "retiro_confirmado"
    })

    if ya_confirmado:
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html lang="es">
        <head><meta charset="UTF-8">{CSS_ESTILOS}</head>
        <body>
            <div class="container">
                <div class="header"><img src="/static/logo.png" alt="Procasa" class="logo"></div>
                <div class="content">
                    <h1>Proceso Completado</h1>
                    <p>La gestión para la propiedad <span class="highlight">{codigo_norm}</span> ya fue procesada anteriormente.</p>
                    <p>Su solicitud se encuentra registrada en nuestros sistemas y no requiere acciones adicionales.</p>
                </div>
                <div class="footer">Jorge Pablo Caro Propiedades • Gestión de Cartera</div>
            </div>
        </body>
        </html>
        """)

    # Datos del ejecutivo
    prop_data = db["universo_obelix"].find_one({"codigo": codigo_norm})
    email_ejecutivo = prop_data.get("email_ejecutivo") if prop_data else None

    # Hora local Chile para guardar en DB
    ahora_chile = datetime.now(TZ_CHILE)

    # Actualización en MongoDB
    col.update_one(
        {"codigo_propiedad": codigo_norm},
        {
            "$set": {
                "email_propietario": email_norm,
                "accion": "retiro_confirmado",
                "fecha_confirmacion": datetime.utcnow(),
                "fecha_chile": ahora_chile,  # ← AÑADIDO: hora local Chile
                "ip": ip,
                "ley": "19.799",
                "fecha_actualizacion": datetime.utcnow()
            },
            "$setOnInsert": { 
                "documento": "Carta_Retiro_Procasa.pdf", 
                "fecha_envio": datetime.utcnow() 
            }
        },
        upsert=True
    )

    db["universo_obelix"].update_one(
        {"codigo": codigo_norm},
        {"$set": { "disponible": False, "fecha_no_disponible": datetime.utcnow(), "motivo": "retiro_propietario" }}
    )

    enviar_notificacion_interna("RETIRO FIRMADO", email_norm, codigo_norm, email_ejecutivo, ip)

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html lang="es">
    <head><meta charset="UTF-8">{CSS_ESTILOS}</head>
    <body>
        <div class="container">
            <div class="header"><img src="/static/logo.png" alt="Procasa" class="logo"></div>
            <div class="content">
                <h1>Retiro Confirmado con Éxito</h1>
                <p>Hemos procesado correctamente la resciliación del encargo para su propiedad código <span class="highlight">{codigo_norm}</span>.</p>
                
                <div class="strategy-note">
                    <p>En <strong>Procasa</strong>, sabemos que su propiedad es uno de sus activos más valiosos. Aunque hoy finalizamos este encargo, nuestro equipo de expertos permanece a su entera disposición. Cuando decida retomar su proyecto inmobiliario, estaremos listos para brindarle la gestión de alto nivel que solo una cartera exclusiva puede ofrecer.</p>
                </div>

                <p>Esta confirmación constituye una Firma Electrónica Simple bajo la Ley chilena Nº 19.799.</p>
                <div class="badge-legal">Operación Certificada • ID: {codigo_norm}</div>
            </div>
            <div class="footer">
                Gracias por haber confiado en nosotros.<br>
                <strong>Jorge Pablo Caro Propiedades</strong>
            </div>
        </div>
    </body>
    </html>
    """)

async def handle_solicitud_contacto(email: str, codigo: str, ip: str):
    """Registra la solicitud de contacto y muestra pantalla de agradecimiento."""
    email_norm = email.lower().strip()
    codigo_norm = codigo.upper().strip()

    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    prop_data = db["universo_obelix"].find_one({"codigo": codigo_norm})
    email_ejecutivo = prop_data.get("email_ejecutivo") if prop_data else None

    # Hora local Chile para guardar en DB
    ahora_chile = datetime.now(TZ_CHILE)

    db["retiros_propiedades"].update_one(
        {"codigo_propiedad": codigo_norm},
        {
            "$set": {
                "email_propietario": email_norm,
                "accion": "solicitud_contacto_ejecutivo",
                "fecha": datetime.utcnow(),
                "fecha_chile": ahora_chile,  # ← AÑADIDO: hora local Chile
                "ip": ip,
                "fecha_actualizacion": datetime.utcnow()
            }
        },
        upsert=True
    )

    enviar_notificacion_interna("SOLICITUD DE CONTACTO", email_norm, codigo_norm, email_ejecutivo, ip)

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html lang="es">
    <head><meta charset="UTF-8">{CSS_ESTILOS}</head>
    <body>
        <div class="container">
            <div class="header"><img src="/static/logo.png" alt="Procasa" class="logo"></div>
            <div class="content">
                <h1>Solicitud de Asesoría Recibida</h1>
                <p>Hemos registrado su requerimiento para la propiedad <span class="highlight">{codigo_norm}</span>.</p>
                
                <p>A la brevedad, un <strong>Asesor Inmobiliario de Procasa</strong> se pondrá en contacto con usted para brindarle una asesoría personalizada, resolver sus inquietudes y asegurar que su propiedad reciba el tratamiento comercial adecuado.</p>
                
                <p>Valoramos la oportunidad de seguir trabajando juntos.</p>
                
                <a href="https://wa.me/56940904971" class="btn-wa">Contactar por WhatsApp Directo</a>
            </div>
            <div class="footer">
                <strong>División de Gestión de Cartera Exclusiva</strong><br>
                Jorge Pablo Caro Propiedades
            </div>
        </div>
    </body>
    </html>
    """)