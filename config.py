# config.py
import os
from dotenv import load_dotenv

# Buscar .env en el mismo directorio que este archivo
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=env_path)

class Config:
    # === Claves externas ===
    XAI_API_KEY = os.getenv("XAI_API_KEY")
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", XAI_API_KEY)
    MONGO_URI = os.getenv("MONGO_URI")
    DB_NAME = os.getenv("DB_NAME", "URLS")

    # === PROXIES ===
    USE_PROXIES = os.getenv("USE_PROXIES", "false").lower() == "true"
    PROXIES = os.getenv("PROXIES", "")  # Lista de proxies separados por coma
    PROXY_USER = os.getenv("PROXY_USER", "")
    PROXY_PASS = os.getenv("PROXY_PASS", "")
    WASENDER_TOKEN = os.getenv("WASENDER_TOKEN")
    WASENDER_WEBHOOK_SECRET = os.getenv("WASENDER_WEBHOOK_SECRET")
    WASENDER_BASE_URL = os.getenv("WASENDER_BASE_URL", "https://wasenderapi.com/api")
    DAILY_REPORT_GROUP_ID = os.getenv("DAILY_REPORT_GROUP_ID")
    CAPTACION_WEEKLY_GROUP_ID = os.getenv("CAPTACION_WEEKLY_GROUP_ID", "").strip()
    CAPTACION_WEEKLY_ADMIN_PHONE = os.getenv("CAPTACION_WEEKLY_ADMIN_PHONE", "+56983219804")
    CAPTACION_WEEKLY_REPORT_COLLECTION = os.getenv(
        "CAPTACION_WEEKLY_REPORT_COLLECTION", "captacion_weekly_reports"
    )
    CAPTACION_WEEKLY_DELIVERY_COLLECTION = os.getenv(
        "CAPTACION_WEEKLY_DELIVERY_COLLECTION", "captacion_weekly_deliveries"
    )
    CAPTACION_WEEKLY_PREVIEW_REQUIRED = False
    CAPTACION_WEEKLY_AUTOMATIC_SEND = True
    CAPTACION_WEEKLY_SCHEDULE_HOUR = 8
    CAPTACION_WEEKLY_SCHEDULE_MINUTE = 30
    CAPTACION_WEEKLY_RETRY_DEADLINE_HOUR = 10
    CAPTACION_WEEKLY_MAX_SEND_ATTEMPTS = 3
    CAPTACION_WEEKLY_PROMPT_VERSION = "captacion_weekly_writer_v4"
    CRM_WEEKLY_REPORT_GROUP_ID = os.getenv("CRM_WEEKLY_REPORT_GROUP_ID", "").strip()
    CRM_WEEKLY_REPORT_COLLECTION = os.getenv("CRM_WEEKLY_REPORT_COLLECTION", "crm_weekly_reports")
    CRM_WEEKLY_DELIVERY_COLLECTION = os.getenv("CRM_WEEKLY_DELIVERY_COLLECTION", "crm_weekly_deliveries")
    CRM_WEEKLY_EXECUTIVE_ORDER = os.getenv("CRM_WEEKLY_EXECUTIVE_ORDER", "Susana,Mariela,Erika,Paula,Pablo")
    CRM_WEEKLY_PREVIEW_REQUIRED = True
    CRM_WEEKLY_AUTOMATIC_SEND = False
    CRM_WEEKLY_SCHEDULE_HOUR = 8
    CRM_WEEKLY_SCHEDULE_MINUTE = 15
    CRM_SLA_ALERTS_ENABLED = os.getenv("CRM_SLA_ALERTS_ENABLED", "false").lower() == "true"
    CRM_BASE_URL = os.getenv("CRM_BASE_URL", "https://procasa-chatbot-yr8d.onrender.com")


    # === GMAIL ===
    GMAIL_USER = os.getenv("GMAIL_USER")
    GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")
    ALERT_EMAIL_RECIPIENT = os.getenv("ALERT_EMAIL_RECIPIENT", os.getenv("GMAIL_USER", ""))

    # === COLECCIONES MONGO – CAMPAÑAS UPDATE PRICE ===
    COLLECTION_CONTACTOS = os.getenv("COLLECTION_CONTACTOS", "contactos")
    COLLECTION_RESPUESTAS = os.getenv("COLLECTION_RESPUESTAS", "price_updates")
    COLLECTION_WHATSAPP_ENVIADOS = os.getenv("COLLECTION_WHATSAPP_ENVIADOS", "whatsapp_price_updates")
    COLLECTION_CAMPANAS_LOG = "ajuste_precio"

    # === Modo y opciones ===
    SIMULATION_MODE = os.getenv("SIMULATION_MODE", "false").lower() == "true"
    STORE_SEPARATE_CHATS = os.getenv("STORE_SEPARATE_CHATS", "false").lower() == "true"
    APICHAT_TIMEOUT = int(os.getenv("APICHAT_TIMEOUT", 8))
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", 2))
    TEST_PHONE = os.getenv("TEST_PHONE")

    # === Modelos DeepSeek / compatibilidad heredada ===
    DEEPSEEK_MODEL_FAST = os.getenv("DEEPSEEK_MODEL_FAST") or os.getenv("DEEPSEEK_MODEL") or "deepseek-v4-flash"
    DEEPSEEK_MODEL_REASONER = os.getenv("DEEPSEEK_MODEL_REASONER") or DEEPSEEK_MODEL_FAST
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL") or os.getenv("GROK_BASE_URL") or "https://api.deepseek.com"
    DEEPSEEK_TEMPERATURE = float(os.getenv("DEEPSEEK_TEMPERATURE") or os.getenv("GROK_TEMPERATURE") or "0.1")
    
    DEEPSEEK_MAX_TOKENS_FAST = int(os.getenv("DEEPSEEK_MAX_TOKENS_FAST") or "1500")
    DEEPSEEK_TIMEOUT_FAST = int(os.getenv("DEEPSEEK_TIMEOUT_FAST") or "30")
    
    DEEPSEEK_MAX_TOKENS_REASONER = int(os.getenv("DEEPSEEK_MAX_TOKENS_REASONER") or "4096")
    DEEPSEEK_TIMEOUT_REASONER = int(os.getenv("DEEPSEEK_TIMEOUT_REASONER") or "60")
    
    DEEPSEEK_RESPONSE_FORMAT = os.getenv("DEEPSEEK_RESPONSE_FORMAT") or ""

    # === DeepSeek Adjudicator (scraper classification) ===
    # NOTA: No hereda de DEEPSEEK_MODEL ni DEEPSEEK_MODEL_FAST.
    # El adjudicador debe ser siempre deepseek-v4-flash, independientemente del modelo del chatbot.
    DEEPSEEK_ADJUDICATOR_MODEL = os.getenv("DEEPSEEK_ADJUDICATOR_MODEL", "deepseek-v4-flash")
    DEEPSEEK_ADJUDICATOR_ENABLED = os.getenv("DEEPSEEK_ADJUDICATOR_ENABLED", "false").lower() == "true"
    DEEPSEEK_ADJUDICATOR_TIMEOUT = int(os.getenv("DEEPSEEK_ADJUDICATOR_TIMEOUT") or "12")
    DEEPSEEK_ADJUDICATOR_MAX_CALLS = int(os.getenv("DEEPSEEK_ADJUDICATOR_MAX_CALLS") or "50")
    DEEPSEEK_ADJUDICATOR_MAX_TOKENS = int(os.getenv("DEEPSEEK_ADJUDICATOR_MAX_TOKENS") or "300")
    DEEPSEEK_ADJUDICATOR_THINKING = os.getenv("DEEPSEEK_ADJUDICATOR_THINKING", "false").lower() == "true"
    DEEPSEEK_ADJUDICATOR_PROMPT_VERSION = os.getenv("DEEPSEEK_ADJUDICATOR_PROMPT_VERSION") or "v0.2_flash_no_thinking"

    # Alias heredados para no romper módulos existentes durante la migración
    GROK_MODEL = DEEPSEEK_MODEL_FAST
    GROK_BASE_URL = DEEPSEEK_BASE_URL
    GROK_TEMPERATURE = DEEPSEEK_TEMPERATURE

    @staticmethod
    def get_captacion_collection(db):
        return db[Config.CAPTACION_COLLECTION_NAME]

    @staticmethod
    def validate_adjudicator_model() -> None:
        """Valida que el modelo del adjudicador sea deepseek-v4-flash.
        Debe llamarse solo al inicializar el scraper/clasificador, NO al importar config."""
        model = Config.DEEPSEEK_ADJUDICATOR_MODEL
        if "pro" in model.lower():
            raise RuntimeError(
                f"DeepSeek Pro model '{model}' is not allowed for Yapo adjudicator. "
                "Set DEEPSEEK_ADJUDICATOR_MODEL=deepseek-v4-flash in .env"
            )

    EMBEDDING_MODEL = "all-MiniLM-L6-v2"
    EMBEDDING_DIM = 384

    # === Colección de captación ===
    CAPTACION_COLLECTION_NAME = os.getenv("CAPTACION_COLLECTION_NAME", "propiedades_captacion")

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
    COLLECTION_NAME = "universo_cartera"
    
    # === Threshold / Configuración de Leads ===
    LEAD_ASSIGNMENT_THRESHOLD = int(os.getenv("LEAD_ASSIGNMENT_THRESHOLD", 40))

    # === Logs y claves ===
    LOG_LEVEL = "INFO"
    CORE_KEYS = ["operacion", "tipo", "comuna"]
    FEATURE_KEYS = ["precio_clp","precio_uf","dormitorios", "banos", "estacionamientos"]

    # === GOOGLE OAUTH (opcional) ===
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
    GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback")

    # === CLAVE SECRETA PARA SESIONES (OBLIGATORIA) ===
    SECRET_KEY = os.getenv("SECRET_KEY", "procasa_stable_secret_session_key_2025_hq")
    # if not SECRET_KEY:
    #     import secrets
    #     SECRET_KEY = secrets.token_hex(32)
