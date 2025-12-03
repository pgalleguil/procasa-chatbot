# config.py
import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # === Claves externas ===
    XAI_API_KEY = os.getenv("XAI_API_KEY")
    MONGO_URI = os.getenv("MONGO_URI")
    DB_NAME = os.getenv("DB_NAME", "URLS")

    # === WASENDERAPI.COM (WhatsApp) ===
    WASENDER_TOKEN = os.getenv("WASENDER_TOKEN")
    WASENDER_WEBHOOK_SECRET = os.getenv("WASENDER_WEBHOOK_SECRET")
    WASENDER_BASE_URL = os.getenv("WASENDER_BASE_URL", "https://wasenderapi.com/api")

    # === GMAIL ===
    GMAIL_USER = os.getenv("GMAIL_USER")
    GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")
    ALERT_EMAIL_RECIPIENT = os.getenv("ALERT_EMAIL_RECIPIENT", os.getenv("GMAIL_USER", ""))

    # === COLECCIONES MONGO – CAMPAÑAS UPDATE PRICE ===
    COLLECTION_CONTACTOS = os.getenv("COLLECTION_CONTACTOS", "contactos")
    COLLECTION_RESPUESTAS = os.getenv("COLLECTION_RESPUESTAS", "price_updates")
    COLLECTION_WHATSAPP_ENVIADOS = os.getenv("COLLECTION_WHATSAPP_ENVIADOS", "whatsapp_price_updates")
    COLLECTION_CAMPANAS_LOG = os.getenv("COLLECTION_CAMPANAS_LOG", "campanas_historico")

    # === Modo y opciones ===
    SIMULATION_MODE = os.getenv("SIMULATION_MODE", "false").lower() == "true"
    STORE_SEPARATE_CHATS = os.getenv("STORE_SEPARATE_CHATS", "false").lower() == "true"
    APICHAT_TIMEOUT = int(os.getenv("APICHAT_TIMEOUT", 8))
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", 2))
    TEST_PHONE = os.getenv("TEST_PHONE")

    # === Modelos Grok / xAI ===
    GROK_MODEL = os.getenv("GROK_MODEL")
    GROK_BASE_URL = os.getenv("GROK_BASE_URL", "https://api.x.ai/v1")
    GROK_TEMPERATURE = float(os.getenv("GROK_TEMPERATURE", "0.0"))

    EMBEDDING_MODEL = "all-MiniLM-L6-v2"
    EMBEDDING_DIM = 384

    # === Parámetros de búsqueda ===
    MAX_DOCS = 1000
    TOP_K = 3
    HYBRID_WEIGHT = 0.7
    WEIGHTS = [0.3, 0.3, 0.4]
    SEMANTIC_THRESHOLD_BASE = 0.15
    PRIORITY_BOOST = 0.5
    PRIORITY_OFICINA = "INMOBILIARIA SUCRE SPA"

    # === Chatbot / colección ===
    HISTORIAL_MAX = 8
    COLLECTION_NAME = "universo_obelix"

    # === Logs y claves ===
    LOG_LEVEL = "INFO"
    CORE_KEYS = ["operacion", "tipo", "comuna"]
    FEATURE_KEYS = ["precio_clp","precio_uf","dormitorios", "banos", "estacionamientos"]

    # === GOOGLE OAUTH (opcional) ===
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

    # === CLAVE SECRETA PARA SESIONES (OBLIGATORIA) ===
    SECRET_KEY = os.getenv("SECRET_KEY")
    if not SECRET_KEY:
        import secrets
        SECRET_KEY = secrets.token_hex(32)
        #print(f"\nSECRET_KEY generada automáticamente (guárdala en .env):")
        #print(f"SECRET_KEY={SECRET_KEY}\n")