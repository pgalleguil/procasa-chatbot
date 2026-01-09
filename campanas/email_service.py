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