import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # === Claves externas ===
    XAI_API_KEY = os.getenv("XAI_API_KEY")  # Nueva: Para Grok
    MONGO_URI = os.getenv("MONGO_URI")
    DB_NAME = os.getenv("DB_NAME", "URLS")

    #APICHAT_TOKEN = os.getenv("APICHAT_TOKEN")
    #APICHAT_CLIENT_ID = os.getenv("APICHAT_CLIENT_ID")
    #APICHAT_BASE_URL = os.getenv("APICHAT_BASE_URL", "https://api.apichat.io/v1")

    # === WASENDERAPI.COM (reemplaza ApiChat) ===
    WASENDER_TOKEN = os.getenv("WASENDER_TOKEN")
    WASENDER_WEBHOOK_SECRET = os.getenv("WASENDER_WEBHOOK_SECRET")
    WASENDER_BASE_URL = os.getenv("WASENDER_BASE_URL", "https://wasenderapi.com/api")

    # === Modo y opciones ===
    SIMULATION_MODE = os.getenv("SIMULATION_MODE", "false").lower() == "true"
    STORE_SEPARATE_CHATS = os.getenv("STORE_SEPARATE_CHATS", "false").lower() == "true"
    APICHAT_TIMEOUT = int(os.getenv("APICHAT_TIMEOUT", 8))
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", 2))
    TEST_PHONE = os.getenv("TEST_PHONE")

    # === Modelos internos (xAI/Grok) ===
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

    # === GMAIL ALERTS ===
    GMAIL_USER = os.getenv("GMAIL_USER")
    GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")
    ALERT_EMAIL_RECIPIENT = os.getenv("ALERT_EMAIL_RECIPIENT", os.getenv("GMAIL_USER", ""))