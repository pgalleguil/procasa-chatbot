#!/usr/bin/env python3
# email_retiro_propiedad.py → Versión FINAL con Copia a Ejecutivo (29-12-2025)

import os
import smtplib
import logging
from datetime import datetime
from zoneinfo import ZoneInfo  # Python 3.9+
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication
from urllib.parse import quote
from pymongo import MongoClient
from config import Config
from pathlib import Path

# ==============================================================================
# CONFIGURACIÓN
# ==============================================================================
MODO_PRUEBA = False  # ← Cambia a False para envío real
EMAIL_PRUEBA = "pgalleguillos@procasa.cl"

RENDER_BASE_URL = "https://procasa-chatbot-yr8d.onrender.com"
PUBLICACION_BASE_URL = "https://www.procasa.cl/propiedad/"

# Zona horaria de Chile
TZ_CHILE = ZoneInfo("America/Santiago")

BASE_DIR = Path(__file__).resolve().parent
PLANTILLA = BASE_DIR / "templates" / "email_retiro_propiedad.html"
PDF_PATH = BASE_DIR / "static" / "documentos" / "Carta_Retiro_Procasa.pdf"

LOGO_PATHS = [BASE_DIR / "static" / "logo.png", BASE_DIR / "static" / "propiedades" / "logo.png"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

# ==============================================================================
# VALIDACIONES INICIALES
# ==============================================================================
if not PLANTILLA.exists():
    raise FileNotFoundError(f"Plantilla no encontrada: {PLANTILLA}")
if not PDF_PATH.exists():
    raise FileNotFoundError(f"PDF no encontrado: {PDF_PATH}")

html_template = PLANTILLA.read_text(encoding="utf-8")

# ==============================================================================
# REGISTRO DE ENVÍO EN MONGODB
# ==============================================================================
def registrar_envio_carta(email: str, codigo: str, modo_prueba: bool = False):
    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]
        retiros = db["retiros_propiedades"]

        ahora_utc = datetime.utcnow()
        ahora_chile = datetime.now(TZ_CHILE)

        retiros.update_one(
            {"codigo_propiedad": codigo.upper().strip()},
            {
                "$set": {
                    "email_propietario": email.lower().strip(),
                    "documento": "Carta_Retiro_Procasa.pdf",
                    "accion": "carta_enviada",
                    "fecha": ahora_utc,
                    "fecha_chile": ahora_chile,
                    "ip": "admin_script_local" if not modo_prueba else "admin_prueba",
                    "notas": "Carta enviada vía script administrativo",
                    "modo_prueba": modo_prueba,
                    "fecha_actualizacion": ahora_utc
                }
            },
            upsert=True
        )
        log.info(f"Registrado en DB: propiedad {codigo} - Hora Chile: {ahora_chile}")
    except Exception as e:
        log.error(f"Error registrando envío en MongoDB: {e}")

# ==============================================================================
# ADJUNTOS
# ==============================================================================
def attach_images(msg):
    for path in LOGO_PATHS:
        if path.exists():
            with open(path, "rb") as f:
                img = MIMEImage(f.read())
                img.add_header('Content-ID', '<logo_procasa>')
                msg.attach(img)
            return

def attach_pdf(msg):
    with open(PDF_PATH, "rb") as f:
        pdf = MIMEApplication(f.read(), _subtype="pdf")
        pdf.add_header('Content-Disposition', 'attachment', filename="Carta_Retiro_Procasa.pdf")
        msg.attach(pdf)

# ==============================================================================
# GENERAR HTML
# ==============================================================================
def generar_html(nombre, codigo, email_para_link):
    email_enc = quote(email_para_link)
    codigo_enc = quote(codigo)
    
    link_confirmar = f"{RENDER_BASE_URL}/retiro/confirmar?email={email_enc}&codigo={codigo_enc}"
    link_llamada = f"{RENDER_BASE_URL}/retiro/contactar?email={email_enc}&codigo={codigo_enc}"
    link_publicacion = f"{PUBLICACION_BASE_URL}{codigo}"
    link_whatsapp = f"https://wa.me/56940904971?text=Hola,%20solicito%20info%20propiedad%20{codigo_enc}"

    return html_template \
        .replace("{{ nombre }}", nombre) \
        .replace("{{ codigo }}", codigo) \
        .replace("{{ link_confirmar }}", link_confirmar) \
        .replace("{{ link_llamada }}", link_llamada) \
        .replace("{{ link_publicacion }}", link_publicacion) \
        .replace("{{ link_whatsapp }}", link_whatsapp)

# ==============================================================================
# ENVÍO DE CORREO
# ==============================================================================
def enviar_correo(destinatario: str, asunto: str, html: str, email_ejecutivo: str = None) -> bool:
    msg = MIMEMultipart("mixed")
    related = MIMEMultipart("related")
    related.attach(MIMEText(html, "html", "utf-8"))
    attach_images(related)
    msg.attach(related)
    attach_pdf(msg)

    msg["From"] = f"Gestión Procasa <{Config.GMAIL_USER}>"
    msg["To"] = destinatario
    msg["Subject"] = asunto

    # Configuración de CC (Copias)
    cc_emails = []
    
    if MODO_PRUEBA:
        cc_emails = ["pgalleguillos@procasa.cl"]
    else:
        # Destinatarios fijos en producción
        cc_emails = ["jpcaro@procasa.cl", "pgalleguillos@procasa.cl"]
        
        # Agregar ejecutivo si existe y no es jpcaro (para no duplicar)
        if email_ejecutivo:
            email_ejecutivo = email_ejecutivo.strip().lower()
            if email_ejecutivo and email_ejecutivo not in cc_emails:
                cc_emails.append(email_ejecutivo)

    msg["Cc"] = ", ".join(cc_emails)
    destinatarios_totales = [destinatario] + cc_emails

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
        server.sendmail(Config.GMAIL_USER, destinatarios_totales, msg.as_string())
        server.quit()
        log.info(f"Correo enviado a {destinatario} | CC: {', '.join(cc_emails)}")
        return True
    except Exception as e:
        log.error(f"Error enviando correo a {destinatario}: {e}")
        return False

# ==============================================================================
# ENVÍO POR CÓDIGO
# ==============================================================================
def enviar_por_codigo(codigo: str):
    codigo = codigo.strip().upper()
    nombre = "Cliente"
    email_real = None
    email_ejecutivo = None

    # Búsqueda de datos en MongoDB
    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]
        prop = db[Config.COLLECTION_NAME].find_one({"codigo": codigo})

        if not prop:
            print(f"❌ Propiedad {codigo} no encontrada en {Config.COLLECTION_NAME}")
            return False

        # Email Propietario
        email_real = prop.get("email_propietario", "").strip().lower()
        if not email_real and not MODO_PRUEBA:
            print(f"❌ Propiedad {codigo} sin email de propietario")
            return False

        # Email Ejecutivo (NUEVO)
        email_ejecutivo = prop.get("email_ejecutivo", "").strip().lower()

        # Nombre Propietario
        nombre_raw = prop.get("nombre_propietario", "").strip()
        if nombre_raw:
            nombre = nombre_raw.split()[0].title()

    except Exception as e:
        print(f"❌ Error conectando a MongoDB: {e}")
        return False

    # Definir destinatario según modo
    destinatario = EMAIL_PRUEBA if MODO_PRUEBA else email_real
    email_para_link = EMAIL_PRUEBA if MODO_PRUEBA else email_real
    
    prefijo = "[PRUEBA] " if MODO_PRUEBA else ""
    asunto = f"{prefijo}Retiro de propiedad | {codigo}"

    html = generar_html(nombre, codigo, email_para_link)

    # Log de consola
    ahora_chile = datetime.now(TZ_CHILE)
    print(f"\n📧 Preparando envío (Hora Chile: {ahora_chile.strftime('%d/%m/%Y %H:%M')})")
    print(f"    👤 Destinatario: {destinatario}")
    print(f"    👔 Ejecutivo: {email_ejecutivo if email_ejecutivo else 'No asignado'}")
    print(f"    🏠 Propiedad: {codigo} ({nombre})")
    print(f"    {'🧪 MODO PRUEBA' if MODO_PRUEBA else '✅ ENVÍO REAL'}")

    registrar_envio_carta(email_para_link, codigo, modo_prueba=MODO_PRUEBA)

    if enviar_correo(destinatario, asunto, html, email_ejecutivo):
        print("✅ ¡Correo enviado y registrado con éxito!")
        return True
    else:
        print("❌ Falló el envío del correo")
        return False

# ==============================================================================
# MAIN
# ==============================================================================
if __name__ == "__main__":
    print("=" * 70)
    print("        ENVÍO DE CARTA DE RETIRO - PROCASA 2026")
    print("        + COPIA A EJECUTIVO + REGISTRO EN MONGODB")
    print("=" * 70)
    print(f"Modo prueba: {'SÍ' if MODO_PRUEBA else 'NO'}")
    print("=" * 70)

    while True:
        codigo = input("\n🔑 Ingrese código de propiedad (o 'salir' para terminar): ").strip()
        if codigo.lower() in ["salir", "exit", "q", ""]:
            print("👋 ¡Hasta luego!")
            break
        if not codigo:
            continue
        
        enviar_por_codigo(codigo)