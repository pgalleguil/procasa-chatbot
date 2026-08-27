from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
HTML_DUMPS_DIR = ROOT_DIR / "html_dumps"
LLM_CACHE_DIR = HTML_DUMPS_DIR / "llm_cache"
REPORTS_DIR = ROOT_DIR / "reports"
DEEPSEEK_FLASH_MODEL = "deepseek-v4-flash"


def _load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def load_env_file() -> str:
    candidates = []
    env_file = os.getenv("ENV_FILE")
    if env_file:
        candidates.append(Path(env_file))
    candidates.extend([
        ROOT_DIR / ".env",
        ROOT_DIR.parent / ".env",
        Path.cwd() / ".env",
    ])
    seen = set()
    for path in candidates:
        path = path.expanduser().resolve()
        if path in seen:
            continue
        seen.add(path)
        if path.exists():
            _load_dotenv_file(path)
            print(f"Env file loaded: {path}")
            return str(path)
    print("Env file loaded: NOT FOUND")
    return ""


LOADED_ENV_PATH = load_env_file()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(slots=True)
class AppConfig:
    root_dir: Path = ROOT_DIR
    data_dir: Path = DATA_DIR
    html_dumps_dir: Path = HTML_DUMPS_DIR
    llm_cache_dir: Path = LLM_CACHE_DIR
    reports_dir: Path = REPORTS_DIR
    base_url: str = field(
        default_factory=lambda: os.getenv("TOCTOC_BASE_URL", "https://www.toctoc.com")
    )
    search_ssr_template: str = field(
        default_factory=lambda: os.getenv(
            "TOCTOC_SSR_SEARCH_TEMPLATE",
            "https://www.toctoc.com/{operacion}/{tipo}/{region}/{comuna}",
        )
    )
    mongo_uri: str = field(default_factory=lambda: os.getenv("MONGO_URI", ""))
    mongo_db: str = field(default_factory=lambda: os.getenv("MONGO_DB", "yapo"))
    mongo_collection: str = field(default_factory=lambda: os.getenv("CAPTACION_COLLECTION_NAME") or os.getenv("MONGO_COLLECTION", "propiedades_captacion"))
    deepseek_api_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""))
    deepseek_base_url: str = field(default_factory=lambda: os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    # Modelo único de producción: no permitir overrides heredados a Pro.
    deepseek_model: str = DEEPSEEK_FLASH_MODEL
    deepseek_enabled: bool = field(default_factory=lambda: _env_bool("DEEPSEEK_ENABLED", True))
    deepseek_timeout_seconds: int = field(default_factory=lambda: _env_int("DEEPSEEK_TIMEOUT_SECONDS", 12))
    deepseek_max_tokens: int = field(default_factory=lambda: _env_int("DEEPSEEK_MAX_TOKENS", 500))
    deepseek_thinking: bool = field(default_factory=lambda: _env_bool("DEEPSEEK_THINKING", False))
    deepseek_max_calls_per_session: int = field(default_factory=lambda: _env_int("DEEPSEEK_MAX_CALLS_PER_SESSION", 500))
    deepseek_description_max_chars: int = field(default_factory=lambda: _env_int("DEEPSEEK_DESCRIPTION_MAX_CHARS", 6000))
    deepseek_description_head_chars: int = field(default_factory=lambda: _env_int("DEEPSEEK_DESCRIPTION_HEAD_CHARS", 2500))
    deepseek_description_tail_chars: int = field(default_factory=lambda: _env_int("DEEPSEEK_DESCRIPTION_TAIL_CHARS", 2500))
    deepseek_description_snippet_radius: int = field(default_factory=lambda: _env_int("DEEPSEEK_DESCRIPTION_SNIPPET_RADIUS", 350))
    uf_valor_clp: float = field(
        default_factory=lambda: float(os.getenv("UF_VALOR_CLP", "40844.79"))
    )
    uf_fecha: str = field(
        default_factory=lambda: os.getenv("UF_FECHA", "2026-07-14")
    )
    request_timeout_seconds: int = field(default_factory=lambda: _env_int("REQUEST_TIMEOUT_SECONDS", 30))
    user_agent: str = field(
        default_factory=lambda: os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        )
    )
    batch_prefix: str = field(default_factory=lambda: os.getenv("BATCH_PREFIX", "toctoc_scrape"))
    proxy_mode: str = field(default_factory=lambda: os.getenv("TOCTOC_PROXY_MODE", "direct").strip().lower())
    proxy_urls: list[str] = field(
        default_factory=lambda: [item.strip() for item in os.getenv("PROXY_URLS", "").split(",") if item.strip()]
    )
    proxy_url: str = field(default_factory=lambda: os.getenv("PROXY_URL", "").strip())
    extra_headers_raw: str = field(default_factory=lambda: os.getenv("EXTRA_HEADERS_JSON", "").strip())

    def ensure_layout(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.html_dumps_dir.mkdir(parents=True, exist_ok=True)
        self.llm_cache_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def generate_batch_id(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = secrets.token_hex(2)
        return f"{self.batch_prefix}_{stamp}_{suffix}"

    def extra_headers(self) -> dict[str, str]:
        if not self.extra_headers_raw:
            return {}
        try:
            data = json.loads(self.extra_headers_raw)
        except Exception:
            return {}
        return {str(k): str(v) for k, v in data.items()}


def get_config() -> AppConfig:
    cfg = AppConfig()
    # Validación tardía del adjudicador (solo se ejecuta al construir el scraper, no al importar)
    if cfg.deepseek_model != DEEPSEEK_FLASH_MODEL:
        raise RuntimeError("DeepSeek Pro no está permitido para el adjudicador; use deepseek-v4-flash.")
    cfg.ensure_layout()
    return cfg
