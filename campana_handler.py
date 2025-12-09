# campana_handler.py → Lógica extraída y modularizada de la campaña de ajuste de precios
import logging
import re
from datetime import datetime
from pymongo import MongoClient
from config import Config
from fastapi.responses import HTMLResponse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib

logger = logging.getLogger(__name__)

async def handle_campana_respuesta(email: str, accion: str, codigos: str, campana: str):
    """
    Maneja la respuesta de la campaña: valida, actualiza MongoDB, envía email al equipo y genera HTML de confirmación.
    Retorna HTMLResponse para el cliente.
    """
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

        # Guardar en colección respuestas (opcional, por historial)
        respuestas.insert_one({
            "email": email_lower,
            "campana_nombre": campana,
            "accion": accion,
            "codigos_propiedad": codigos_lista,
            "fecha_respuesta": ahora,
            "canal_origen": "email"
        })

        # Datos para actualizar el contacto
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

        # Configuración por acción
        if accion == "ajuste_7":
            update_data["$set"]["estado"] = "ajuste_autorizado"
            titulo = "¡Autorización recibida!"
            mensaje = """Ya realizamos la actualización del precio de tu propiedad en Procasa.\n\nEl nuevo valor se verá reflejado en los portales inmobiliarios dentro de aproximadamente 72 horas.\n\nQuedaremos atentos"""
            color = "#10b981"

        elif accion == "llamada":
            update_data["$set"]["estado"] = "pendiente_llamada"
            titulo = "¡Solicitud recibida!"
            mensaje = """Perfecto, derivamos tu solicitud para que un ejecutivo de Procasa se ponga en contacto contigo.\n\nTe llamaremos dentro de las próximas 24-48 horas.\n\n¡Gracias por confiar en nosotros!"""
            color = "#3b82f6"

        elif accion == "mantener":
            update_data["$set"]["estado"] = "precio_mantenido"
            titulo = "Precio mantenido"
            mensaje = """Perfecto, dejamos el precio de tu propiedad tal como está.\n\nSeguiremos monitoreando el mercado para avisarte si cambia la situación.\n\nQuedamos a tu disposición."""
            color = "#f59e0b"

        elif accion == "no_disponible":
            update_data["$set"]["estado"] = "no_disponible"
            titulo = "Entendido"
            mensaje = """Perfecto, marcamos tu propiedad como no disponible.\n\nSi en el futuro tienes otra para vender o arrendar, aquí estaremos.\n\n¡Gracias por tu confianza!"""
            color = "#ef4444"

        else:  # unsubscribe
            update_data["$set"]["estado"] = "suscripcion_anulada"
            titulo = "Suscripción anulada"
            mensaje = """Hemos procesado tu solicitud y quedaste desinscrito de nuestras comunicaciones.\n\nSi deseas volver a recibir novedades, solo avísanos.\n\n¡Gracias por haber sido parte de Procasa!"""
            color = "#6b7280"

        # Actualizar en MongoDB
        result1 = contactos.update_one({"email_propietario": email_lower}, update_data)
        if result1.matched_count == 0:
            contactos.update_one(
                {"email_propietario": {"$regex": f"^{re.escape(email_lower)}$", "$options": "i"}},
                update_data
            )

        # ===========================================================
        # === ENVÍO AUTOMÁTICO DE EMAIL AL EQUIPO (CON DASHBOARD Y CREDENCIALES) ===
        # ===========================================================
        try:
            contacto = contactos.find_one({
                "email_propietario": {"$regex": f"^{re.escape(email_lower)}$", "$options": "i"}
            })

            nombre = "Nombre no encontrado"
            telefono = "Sin teléfono"
            codigo_principal = codigos_lista[0] if codigos_lista else "S/C"

            if contacto:
                nombre = f"{contacto.get('nombre_propietario','')} {contacto.get('apellido_paterno_propietario','')} {contacto.get('apellido_materno_propietario','')}".strip()
                if not nombre.strip():
                    nombre = "Nombre no encontrado"
                telefono = contacto.get("telefono", "Sin teléfono")

            accion_texto = {
                "ajuste_7": "ACEPTÓ EL AJUSTE DEL 7%",
                "llamada": "SOLICITÓ QUE LO LLAMEN",
                "mantener": "DECIDIÓ MANTENER EL PRECIO",
                "no_disponible": "PROPIEDAD YA NO DISPONIBLE",
                "unsubscribe": "SE DIO DE BAJA"
            }.get(accion, accion.upper())

            cuerpo = f"""
¡NUEVA RESPUESTA EN VIVO - CAMPAÑA AJUSTE DE PRECIO DICIEMBRE 2025!

Cliente      : {nombre}
Código(s)    : {", ".join(codigos_lista) if codigos_lista else codigo_principal}
Teléfono     : {telefono}
Email        : {email_lower}
Respuesta    : {accion_texto}
Hora         : {ahora.strftime('%d/%m/%Y %H:%M')}

ENLACE DIRECTO A LA PROPIEDAD:
https://www.procasa.cl/{codigo_principal}

DASHBOARD EN TIEMPO REAL (para marcar como gestionado):
https://procasa-chatbot-yr8d.onrender.com

Usuario     : admin
Contraseña  : procasa2025

¡Entrar YA y gestionar esta respuesta caliente!

---
Sistema automático Procasa
"""

            msg = MIMEMultipart()
            msg['From'] = f"Procasa Alertas <{Config.GMAIL_USER}>"
            msg['To'] = "jpcaro@procasa.cl, pgalleguillos@procasa.cl ,p.galleguil@gmail.com"  # ← TUS CORREOS AQUÍ
            msg['Subject'] = f" NUEVA RESPUESTA: {nombre} - {accion_texto}"

            msg.attach(MIMEText(cuerpo, 'plain', 'utf-8'))

            with smtplib.SMTP('smtp.gmail.com', 587) as server:
                server.starttls()
                server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
                server.sendmail(Config.GMAIL_USER, [x.strip() for x in msg['To'].split(",")], msg.as_string())

            logger.info(f"Email de alerta con enlace y credenciales enviado → {email_lower}")

        except Exception as e:
            logger.error(f"Error enviando email al equipo: {e}")

        # ===========================================================
        # === RESPUESTA AL CLIENTE (HTML bonito) ===
        # ===========================================================
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
        logger.error(f"[ERROR CRÍTICO] Falló /campana/respuesta → {email} | Error: {e}", exc_info=True)
        return HTMLResponse("Error interno del servidor. Contacta a soporte.", status_code=500)