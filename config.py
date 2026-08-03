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
    # Fail closed until the canonical assignment -> delivery flow is deployed and verified.
    # Operational notifications are enabled by default. Deployments can still
    # use the environment variables as emergency kill switches.
    LEAD_HOT_NOTIFICATIONS_ENABLED = os.getenv("LEAD_HOT_NOTIFICATIONS_ENABLED", "true").lower() == "true"
    LEAD_HOT_RECONCILIATION_ENABLED = os.getenv("LEAD_HOT_RECONCILIATION_ENABLED", "false").lower() == "true"
    LEAD_COLD_DIGEST_ENABLED = os.getenv("LEAD_COLD_DIGEST_ENABLED", "false").lower() == "true"
    CRM_SLA_SHADOW_ENABLED = os.getenv("CRM_SLA_SHADOW_ENABLED", "false").lower() == "true"
    CRM_SLA_ALERTS_ENABLED = os.getenv("CRM_SLA_ALERTS_ENABLED", "false").lower() == "true"
    CRM_WEEKLY_REPORT_GENERATION_ENABLED = os.getenv("CRM_WEEKLY_REPORT_GENERATION_ENABLED", "false").lower() == "true"
    CRM_WEEKLY_REPORT_SEND_ENABLED = os.getenv("CRM_WEEKLY_REPORT_SEND_ENABLED", "false").lower() == "true"
    CRM_LEGACY_DAILY_REPORT_ENABLED = os.getenv("CRM_LEGACY_DAILY_REPORT_ENABLED", "false").lower() == "true"
    CRM_INACTIVE_NUDGE_ENABLED = os.getenv("CRM_INACTIVE_NUDGE_ENABLED", "false").lower() == "true"
    CRM_BASE_URL = os.getenv("CRM_BASE_URL", "https://procasa-chatbot-yr8d.onrender.com")

    # Chatbot inbound batching.  The quiet window is renewed for every inbound
    # message, but a bounded total wait keeps an active conversation deliverable.
    CHATBOT_BATCH_QUIET_SECONDS = int(os.getenv("CHATBOT_BATCH_QUIET_SECONDS", "15"))
    CHATBOT_BATCH_MAX_WAIT_SECONDS = int(os.getenv("CHATBOT_BATCH_MAX_WAIT_SECONDS", "60"))
    CHATBOT_BATCH_MAX_REGENERATIONS = int(os.getenv("CHATBOT_BATCH_MAX_REGENERATIONS", "2"))

    # === Phase 2 Management Enforcement Cutover ===
    # Cycles assigned before this timestamp are exempt from the new SLA policy.
    # They show as "Histórico" in the UI and are excluded from compliance metrics,
    # digest, SLA alerts, and escalations.  Management can still be registered.
    CRM_MANAGEMENT_ENFORCEMENT_CUTOVER_AT = os.getenv(
        "CRM_MANAGEMENT_ENFORCEMENT_CUTOVER_AT",
        "2026-07-23T22:00:00Z",
    )

    # === Phase 3 SLA Visual Cutover ===
    # Cycles assigned on or after this timestamp use differentiated SLA thresholds:
    # Lead: 120/150/180 min, Lead Hot: 30/45/60 min.
    # Pre-cutover cycles show as "Histórico" regardless of management status.
    CRM_SLA_VISUAL_CUTOVER_AT = os.getenv(
        "CRM_SLA_VISUAL_CUTOVER_AT",
        "2026-07-23T23:00:00Z",
    )
    CRM_SLA_VISUAL_ENABLED = os.getenv("CRM_SLA_VISUAL_ENABLED", "true").lower() == "true"

    # === Non-Hot Digest (lead qualification) ===
    # When enabled, accumulates non-HOT assignments and sends grouped notifications
    # every CRM_NON_HOT_DIGEST_WINDOW_MINUTES minutes per executive.
    CRM_NON_HOT_DIGEST_ENABLED = os.getenv("CRM_NON_HOT_DIGEST_ENABLED", "true").lower() == "true"
    # Shadow mode: builds & persists the digest record but never calls the provider.
    # Hardcoded: no env-var override. Post-canary activation.
    CRM_NON_HOT_DIGEST_SHADOW_MODE = False
    # Fixed accumulation window in minutes. First non-HOT assignment starts the clock.
    CRM_NON_HOT_DIGEST_WINDOW_MINUTES = int(os.getenv("CRM_NON_HOT_DIGEST_WINDOW_MINUTES", "10"))
    # Maximum property preview items in the WhatsApp message.
    CRM_NON_HOT_DIGEST_MAX_PREVIEW_ITEMS = int(os.getenv("CRM_NON_HOT_DIGEST_MAX_PREVIEW_ITEMS", "3"))
    # When a pending digest reaches this many leads, it is sent immediately
    # without waiting for the 10-minute window to expire.  0 = disabled.
    CRM_NON_HOT_DIGEST_MAX_LEADS_BEFORE_SEND = int(os.getenv("CRM_NON_HOT_DIGEST_MAX_LEADS_BEFORE_SEND", "0"))

    # TEMP: envío inmediato de digests no-HOT (pausa temporal de la ventana de
    # acumulación de 10 min). Mientras esté en "true" el aviso al ejecutivo sale
    # de inmediato. Para reactivar la ventana: set
    # CRM_NON_HOT_DIGEST_IMMEDIATE_SEND=false (o revertir este default).
    CRM_NON_HOT_DIGEST_IMMEDIATE_SEND = os.getenv("CRM_NON_HOT_DIGEST_IMMEDIATE_SEND", "true").lower() == "true"

    # === After-hours notification policy ===
    # These flags control whether HOT notifications are deferred outside business
    # hours.  They do NOT affect the digest window, which is always 10 minutes
    # from the first non-HOT lead regardless of time of day.
    CRM_NOTIFICATION_BUSINESS_START = int(os.getenv("CRM_NOTIFICATION_BUSINESS_START", "9"))
    CRM_NOTIFICATION_BUSINESS_END = int(os.getenv("CRM_NOTIFICATION_BUSINESS_END", "19"))
    # After-hours hot mode: NEXT_BUSINESS_OPEN (queue for next opening) or ON_CALL_IMMEDIATE.
    CRM_AFTER_HOURS_HOT_MODE = os.getenv("CRM_AFTER_HOURS_HOT_MODE", "NEXT_BUSINESS_OPEN")

    # === SLA v2 shadow mode ===
    CRM_SLA_V2_SHADOW_ENABLED = os.getenv("CRM_SLA_V2_SHADOW_ENABLED", "false").lower() == "true"
    CRM_SLA_V2_LIVE_ENABLED = os.getenv("CRM_SLA_V2_LIVE_ENABLED", "false").lower() == "true"
    # Aggregation windows for non-HOT SLA alerts (minutes)
    CRM_NON_HOT_SLA_PRECRITICAL_AGGREGATION_MINUTES = int(os.getenv("CRM_NON_HOT_SLA_PRECRITICAL_AGGREGATION_MINUTES", "10"))
    CRM_NON_HOT_SLA_CRITICAL_AGGREGATION_MINUTES = int(os.getenv("CRM_NON_HOT_SLA_CRITICAL_AGGREGATION_MINUTES", "5"))
    # Cutover date for live SLA alerts. Before this date, all alerts are shadow.
    CRM_SLA_ALERTS_LIVE_CUTOVER_AT = os.getenv("CRM_SLA_ALERTS_LIVE_CUTOVER_AT", "2027-01-01T00:00:00Z")
    # Legacy hot creation is permanently disabled. New hot notifications use
    # crm_notifications_v1 exclusively. pending_notifications is read-only for
    # existing legacy documents.


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
    COLLECTION_NAME = os.getenv("COLLECTION_NAME", "universo_cartera_prop360")
    PROPERTY_COLLECTION_NAME = os.getenv("PROPERTY_COLLECTION_NAME", "universo_cartera_prop360")
    
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
