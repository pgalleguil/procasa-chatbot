#!/usr/bin/env python3
# email_retiro_propiedad.py → Versión FINAL DEFINITIVA + ENLACE PÚBLICO + TEXTO MEJORADO

import os
import smtplib
import logging
from datetime import datetime, timezone
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
MODO_PRUEBA = True  # ← Cambia a False para envío real
EMAIL_PRUEBA = "pgalleguillos@procasa.cl"

RENDER_BASE_URL = "https://procasa-chatbot-yr8d.onrender.com"
PUBLICACION_BASE_URL = "https://www.procasa.cl/propiedad/"

BASE_DIR = Path(__file__).resolve().parent
PLANTILLA = BASE_DIR / "templates" / "email_retiro_propiedad.html"
PDF_PATH = BASE_DIR / "static" / "documentos" / "Carta_Retiro_Procasa.pdf"

LOGO_PATHS = [BASE_DIR / "static" / "logo.png", BASE_DIR / "static" / "propiedades" / "logo.png"]
WA_PATHS = [BASE_DIR / "static" / "whatsapp.png"]

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

        retiros.insert_one({
            "email_propietario": email.lower().strip(),
            "codigo_propiedad": codigo.upper().strip(),
            "documento": "Carta_Retiro_Procasa.pdf",
            "accion": "carta_enviada",
            "fecha": datetime.now(timezone.utc),
            "ip": "admin_script_local" if not modo_prueba else "admin_prueba",
            "notas": "Carta enviada vía script administrativo",
            "modo_prueba": modo_prueba
        })
        log.info(f"Registrado en DB: carta enviada a {email} (propiedad {codigo})")
    except Exception as e:
        log.error(f"Error registrando envío en MongoDB: {e}")

# ==============================================================================
# ADJUNTOS
# ==============================================================================
def attach_images(msg):
    # SOLO EL LOGO, sin basura extra
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
# GENERAR HTML (CORREGIDO Y MEJORADO)
# ==============================================================================
def generar_html(nombre, codigo, email_para_link):
    email_enc = quote(email_para_link)
    codigo_enc = quote(codigo)
    
    # El segundo botón ahora apunta a tu servidor /retiro/contactar
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
def enviar_correo(destinatario: str, asunto: str, html: str) -> bool:
    msg = MIMEMultipart("mixed")
    related = MIMEMultipart("related")
    related.attach(MIMEText(html, "html", "utf-8"))
    attach_images(related)
    msg.attach(related)
    attach_pdf(msg)

    msg["From"] = f"Gestión Procasa <{Config.GMAIL_USER}>"
    msg["To"] = destinatario
    msg["Subject"] = asunto

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        log.info(f"Correo enviado exitosamente a {destinatario}")
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

    if not MODO_PRUEBA:
        try:
            client = MongoClient(Config.MONGO_URI)
            db = client[Config.DB_NAME]
            prop = db["universo_obelix"].find_one({"codigo": codigo})

            if not prop:
                print(f"❌ Propiedad {codigo} no encontrada en universo_obelix")
                return False

            email_real = prop.get("propietario_email", "").strip().lower()
            if not email_real:
                print(f"❌ Propiedad {codigo} sin email de propietario")
                return False

            nombre_raw = prop.get("propietario_nombre", "")
            if nombre_raw.strip():
                nombre = nombre_raw.strip().split()[0].title()
        except Exception as e:
            print(f"❌ Error conectando a MongoDB: {e}")
            return False

    destinatario = EMAIL_PRUEBA if MODO_PRUEBA else email_real
    email_para_link = EMAIL_PRUEBA if MODO_PRUEBA else email_real
    prefijo = "[PRUEBA] " if MODO_PRUEBA else ""
    asunto = f"{prefijo}Retiro de propiedad | {codigo}"

    html = generar_html(nombre, codigo, email_para_link)

    print(f"\n📧 Preparando envío:")
    print(f"   👤 Destinatario: {destinatario}")
    print(f"   🏠 Propiedad: {codigo} ({nombre})")
    print(f"   {'🧪 MODO PRUEBA' if MODO_PRUEBA else '✅ ENVÍO REAL'}")

    registrar_envio_carta(email_para_link, codigo, modo_prueba=MODO_PRUEBA)

    if enviar_correo(destinatario, asunto, html):
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
    print("       ENVÍO DE CARTA DE RETIRO - PROCASA 2025")
    print("       + ENLACE PÚBLICO + REGISTRO EN MONGODB")
    print("=" * 70)
    print(f"URL base confirmación: {RENDER_BASE_URL}/retiro/confirmar")
    print(f"URL pública propiedad: {PUBLICACION_BASE_URL}<código>")
    print(f"Modo prueba: {'SÍ → Todo a ' + EMAIL_PRUEBA if MODO_PRUEBA else 'NO → Envío real'}")
    print("=" * 70)

    while True:
        codigo = input("\n🔑 Ingrese código de propiedad (o 'salir' para terminar): ").strip()
        if codigo.lower() in ["salir", "exit", "q", ""]:
            print("👋 ¡Hasta luego!")
            break
        if not codigo:
            continue
        
        enviar_por_codigo(codigo)