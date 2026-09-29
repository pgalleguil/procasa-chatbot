# campanas/email_service.py
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from config import Config
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

def enviar_alerta_equipo(nombre: str, telefono: str, email: str, codigos: list, accion_texto: str, campana: str):
    try:
        codigo_principal = codigos[0] if codigos else "S/C"
        cuerpo = f"""
¡NUEVA RESPUESTA EN VIVO - {campana.upper()}!

Cliente      : {nombre}
Código(s)    : {", ".join(codigos) if codigos else codigo_principal}
Teléfono     : {telefono}
Email        : {email}
Respuesta    : {accion_texto}
Hora         : {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}  # ← ahora datetime está definido

ENLACE DIRECTO:
https://www.procasa.cl/{codigo_principal}

DASHBOARD EN TIEMPO REAL:
https://procasa-chatbot-yr8d.onrender.com

---
Sistema automático Procasa
"""

        msg = MIMEMultipart()
        msg['From'] = f"Procasa Alertas <{Config.GMAIL_USER}>"
        msg['To'] = "jpcaro@procasa.cl, pgalleguillos@procasa.cl"
        msg['Subject'] = f" NUEVA RESPUESTA: {nombre} - {accion_texto}"

        msg.attach(MIMEText(cuerpo, 'plain', 'utf-8'))

        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
            server.sendmail(Config.GMAIL_USER, [x.strip() for x in msg['To'].split(",")], msg.as_string())

        logger.info(f"Email de alerta enviado → {email}")
    except Exception as e:
        logger.error(f"Error enviando email al equipo: {e}")


def enviar_notificacion_owner_campaign(
    *, owner_name: str, owner_email: str, property_code: str,
    current_price, recommended_price, adjustment_pct, action: str,
    executive_name: str, executive_email: str, boss_cc: str,
) -> bool:
    """Notify assigned staff after a persisted single-property campaign event."""
    import re
    from email.message import EmailMessage

    recipients = [value.strip().casefold() for value in (boss_cc, executive_email) if value and value.strip()]
    if (
        not Config.GMAIL_USER or not Config.GMAIL_PASSWORD or not owner_email
        or not property_code or not executive_name or len(recipients) != 2
        or len(set(recipients)) != 2
        or any(not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) for value in recipients)
    ):
        logger.error("Owner campaign internal notification is not configured or has invalid recipients")
        return False
    body = (
        "Respuesta a campaña PROCASA\n\n"
        f"Propietario: {owner_name}\n"
        f"Email propietario: {owner_email}\n"
        f"Propiedad: {property_code}\n"
        f"Precio actual: {current_price} UF\n"
        f"Precio recomendado: {recommended_price} UF\n"
        f"Ajuste: {adjustment_pct}%\n"
        f"Acción: {action}\n"
        f"Ejecutivo responsable: {executive_name} ({executive_email})\n"
    )
    message = EmailMessage()
    message["From"] = f"Procasa Alertas <{Config.GMAIL_USER}>"
    message["To"] = recipients[0]
    message["Cc"] = recipients[1]
    message["Subject"] = f"Respuesta campaña de precio · Propiedad {property_code}"
    message.set_content(body)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
            refused = server.send_message(message, to_addrs=recipients)
        if refused:
            logger.error("Owner campaign internal notification was refused by SMTP")
            return False
        return True
    except Exception as exc:
        logger.error("Owner campaign internal notification failed (%s)", type(exc).__name__)
        return False
