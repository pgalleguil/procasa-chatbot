import smtplib
import logging
import os
from pathlib import Path
from pymongo import MongoClient
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from config import Config

logger = logging.getLogger(__name__)
config = Config()

# ==============================
# CONFIGURACIÓN DE RUTAS FIJAS
# ==============================
PROJECT_ROOT = Path(__file__).resolve().parent

TEMPLATE_HTML = PROJECT_ROOT / "retiro" / "email_template.html"
PDF_CARTA = PROJECT_ROOT / "static" / "documentos" / "Carta_Retiro_Procasa.pdf"

# Verificar que existan los archivos
if not TEMPLATE_HTML.exists():
    raise FileNotFoundError(f"Template no encontrado: {TEMPLATE_HTML}")

if not PDF_CARTA.exists():
    raise FileNotFoundError(f"PDF de carta no encontrado: {PDF_CARTA}")

# ==============================
# CONFIGURACIÓN DE URL Y MODO PRUEBA
# ==============================
# URL base para el botón de confirmación
# Pon en tu .env: BASE_URL=http://localhost:8000 (o tu dominio real en producción)
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

TEST_EMAIL = True                     # Cambia a False para envío real al cliente
EMAIL_TEST = "pgalleguil@gmail.com"   # Tu email de prueba

# ==============================
# OBTENER PROPIETARIO POR CÓDIGO
# ==============================
def obtener_propietario_por_codigo(codigo: str):
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]

    prop = db["universo_obelix"].find_one(
        {"codigo": codigo.upper().strip()},
        {
            "_id": 0,
            "propietario_nombre": 1,
            "propietario_email": 1
        }
    )

    if not prop:
        raise ValueError(f"Código {codigo} no existe en la colección 'universo_obelix' de la base '{Config.DB_NAME}'")

    if not prop.get("propietario_email"):
        raise ValueError(f"Código {codigo} encontrado pero sin email de propietario")

    return prop["propietario_nombre"], prop["propietario_email"]

# ==============================
# DIAGNÓSTICO DE MONGODB
# ==============================
def diagnostico_mongodb():
    print("\n" + "="*70)
    print("DIAGNÓSTICO DE CONEXIÓN A MONGODB")
    print("="*70)
    print(f"MONGO_URI: {Config.MONGO_URI[:30]}... (oculto)")
    print(f"Base de datos activa (DB_NAME): {Config.DB_NAME}")
    print(f"URL base para confirmaciones: {BASE_URL}")
    
    try:
        client = MongoClient(Config.MONGO_URI)
        print("\nBases de datos disponibles:")
        print(client.list_database_names())
        
        db = client[Config.DB_NAME]
        print(f"\nColecciones en '{Config.DB_NAME}':")
        print(db.list_collection_names())
        
        coleccion = db["universo_obelix"]
        count = coleccion.count_documents({})
        print(f"\nDocumentos en 'universo_obelix': {count}")
        
        if count > 0:
            print("\nPrimeros 10 códigos con email disponible:")
            cursor = coleccion.find(
                {"propietario_email": {"$exists": True, "$ne": None}},
                {"codigo": 1, "propietario_nombre": 1, "propietario_email": 1}
            ).limit(10)
            
            codigos = []
            for i, doc in enumerate(cursor, 1):
                nombre = doc.get("propietario_nombre", "Sin nombre")
                email = doc["propietario_email"]
                codigo = doc["codigo"]
                print(f"  {i}. Código: {codigo} | Nombre: {nombre} | Email: {email}")
                codigos.append(codigo)
            return codigos
        else:
            print("\n¡La colección 'universo_obelix' está vacía!")
            return []
            
    except Exception as e:
        print(f"\nError conectando a MongoDB: {e}")
        return []

    print("="*70 + "\n")

# ==============================
# ENVÍO DE CARTA DE RETIRO
# ==============================
def enviar_carta_retiro_por_codigo(codigo: str):
    codigo = codigo.strip().upper()

    nombre, email_cliente = obtener_propietario_por_codigo(codigo)
    email_destino = EMAIL_TEST if TEST_EMAIL else email_cliente

    # Cargar template HTML
    html = TEMPLATE_HTML.read_text(encoding="utf-8")

    # Link correcto usando BASE_URL
    link_confirmacion = f"{BASE_URL}/retiro/confirmar?email={email_cliente}&codigo={codigo}"

    html = (
        html.replace("{{NOMBRE}}", nombre)
            .replace("{{CODIGO}}", codigo)
            .replace("{{LINK_CONFIRMACION}}", link_confirmacion)
    )

    # Asunto
    asunto = (
        f"[PRUEBA] Carta de Retiro – {codigo}"
        if TEST_EMAIL
        else f"Carta de Retiro – Propiedad {codigo}"
    )

    # Construir mensaje
    msg = MIMEMultipart("alternative")
    msg["From"] = f"Procasa <{Config.GMAIL_USER}>"
    msg["To"] = email_destino
    msg["Subject"] = asunto

    msg.attach(MIMEText(html, "html", "utf-8"))

    # Adjuntar PDF
    with open(PDF_CARTA, "rb") as f:
        pdf = MIMEApplication(f.read(), _subtype="pdf")
        pdf.add_header("Content-Disposition", "attachment", filename="Carta_Retiro_Procasa.pdf")
        msg.attach(pdf)

    # Enviar correo
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
        server.sendmail(Config.GMAIL_USER, [email_destino], msg.as_string())

    logger.info(f"[RETIRO] Carta enviada | codigo={codigo} | destino={email_destino} | TEST={TEST_EMAIL}")
    print(f"\n¡Correo enviado exitosamente a {email_destino}!")
    print(f"   Código: {codigo} | Nombre: {nombre}")
    print(f"   Link de confirmación: {link_confirmacion}")
    print(f"   {'(MODO PRUEBA)' if TEST_EMAIL else '(ENVÍO REAL AL CLIENTE)'}")

# ==============================
# EJECUCIÓN PRINCIPAL
# ==============================
if __name__ == "__main__":
    # 1. Diagnóstico completo
    codigos_disponibles = diagnostico_mongodb()

    # 2. Pedir código al usuario
    if codigos_disponibles:
        print("Ingresa uno de los códigos de arriba para enviar la carta.")
    else:
        print("No se encontraron códigos con email. Revisa tu DB_NAME en .env")

    print("O deja en blanco para salir.\n")
    codigo_input = input("Código de propiedad: ").strip()

    if not codigo_input:
        print("Saliendo sin enviar.")
    else:
        try:
            enviar_carta_retiro_por_codigo(codigo_input)
        except Exception as e:
            print(f"\nError: {e}")