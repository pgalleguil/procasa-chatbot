from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import requests as req_lib
except Exception:
    req_lib = None

from config import AppConfig
from proxy_manager import ProxyChoice

BLOCK_PATTERNS = ("access denied", "forbidden", "too many requests", "cloudflare", "cf-ray", "cf-browser-verification")
LISTING_REMOVED_PATTERNS = (
    "anuncio borrado", "eliminado por el anunciante", "propiedad no encontrada",
    "pagina no encontrada", "publicacion no existe",
    "esta propiedad ya no se encuentra publicada", "error 404",
    "no se ha encontrado la pagina", "la pagina que buscas no existe",
    "lo sentimos", "esta propiedad ya no se encuentra disponible",
    "ya no esta disponible", "propiedad eliminada",
    "la publicacion expiro", "aviso no encontrado",
)


@dataclass(slots=True)
class DownloadResult:
    url: str
    html: str
    status_code: int | None
    fetch_source: str
    html_path: Path
    validation_status: str
    validation_reason: str
    blocked: bool = False
    proxy_used: bool = False
    proxy_attempt: int = 0
    wire_bytes: int = 0
    decompressed_bytes: int = 0
    content_encoding: str = ""


def md5_url(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def html_path_for_url(url: str, config: AppConfig, batch_id: str | None = None) -> Path:
    fid = md5_url(url)
    if batch_id:
        return config.html_dumps_dir / batch_id / f"{fid}.html"
    return config.html_dumps_dir / f"{fid}.html"


def validate_listing_content(html: str, url: str) -> dict[str, Any]:
    text_lower = re.sub(r"<script\b.*?</script>", " ", html, flags=re.I | re.S)
    text_lower = re.sub(r"<style\b.*?</style>", " ", text_lower, flags=re.I | re.S)
    text_lower = re.sub(r"<[^>]+>", " ", text_lower)
    text_lower = re.sub(r"\s+", " ", text_lower).strip().lower()
    signals = {
        "has_listing_code": bool(re.search(r'TT-(\d+)', text_lower)),
        "has_specific_title": bool(re.search(r'\b(?:departamento|casa|oficina|terreno|parcela|local|bodega)\b', text_lower)),
        "has_price": bool(re.search(r'\b(?:uf\s*\d+\.?\d*|\$\s*[\d.]+|precio)\b', text_lower)),
        "has_property_content": bool(re.search(r'\b(?:dormitorio|baño|m²|metros|superficie)\b', text_lower)),
        "has_real_property_image": bool(re.search(r'/toctoc/fotos/', html)),
    }
    positive_count = sum(1 for v in signals.values() if v)
    return {"signals": signals, "positive_count": positive_count, "is_valid_listing": positive_count >= 2}


def validate_html(html: str, url: str = "") -> dict[str, Any]:
    raw = re.sub(r"\s+", " ", html or "").strip().lower()
    if len(raw) < 200:
        return {"status": "INVALID", "reason": "too_short"}
    body = re.sub(r"<script\b.*?</script>", " ", raw, flags=re.I | re.S)
    body = re.sub(r"<style\b.*?</style>", " ", body, flags=re.I | re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    if any(pattern.lower() in raw for pattern in LISTING_REMOVED_PATTERNS):
        return {"status": "LISTING_REMOVED", "reason": "listing_removed_text"}
    if any(pattern in body for pattern in BLOCK_PATTERNS):
        return {"status": "BLOCKED", "reason": "strong_block_signal"}
    if len(body) < 50:
        return {"status": "INVALID", "reason": "body_too_short"}
    content_check = validate_listing_content(html, url)
    if not content_check["is_valid_listing"]:
        return {"status": "LISTING_REMOVED", "reason": "listing_incomplete_or_empty_detail",
                "content_signals": content_check["signals"], "content_positive_count": content_check["positive_count"]}
    return {"status": "OK", "reason": "valid_html", "content_signals": content_check["signals"]}


def _headers(config: AppConfig) -> dict[str, str]:
    headers = {
        "User-Agent": config.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache", "Pragma": "no-cache",
    }
    headers.update(config.extra_headers())
    return headers


def download_html(
    url: str,
    config: AppConfig,
    *,
    batch_id: str | None = None,
    force_refresh: bool = False,
    attempt: int = 0,
    session: Any = None,
) -> DownloadResult:
    """Download HTML. Like Yapo: attempt 0 = direct, attempt > 0 = proxy fallback."""
    html_path = html_path_for_url(url, config, batch_id=batch_id)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    if html_path.exists() and not force_refresh:
        html = html_path.read_text(encoding="utf-8", errors="replace")
        validation = validate_html(html)
        return DownloadResult(url=url, html=html, status_code=200, fetch_source="cache",
            html_path=html_path, validation_status=validation["status"], validation_reason=validation["reason"],
            blocked=validation["status"] == "BLOCKED")

    headers = _headers(config)
    timeout = config.request_timeout_seconds

    # Like Yapo: attempt 0 = direct (None), attempt > 0 = proxy
    from proxy_manager import get_proxy_for_attempt
    proxy_url = get_proxy_for_attempt(attempt)
    proxy_used = proxy_url is not None

    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}

    _fallback_session = getattr(download_html, "_fallback_session", None)
    if _fallback_session is None and req_lib is not None:
        _fallback_session = req_lib.Session()
        download_html._fallback_session = _fallback_session
    http_session = session if session is not None else (_fallback_session if req_lib is not None else None)
    if http_session is not None:
        try:
            resp = http_session.get(url, headers=headers, proxies=proxies, timeout=timeout, stream=True)
            resp.raise_for_status()
            content_encoding = resp.headers.get("Content-Encoding", "")
            import zlib, brotli
            raw_chunks = []
            for chunk in resp.raw.stream(decode_content=False):
                if chunk:
                    raw_chunks.append(chunk)
            raw_body = b"".join(raw_chunks)
            wire_bytes = len(raw_body)
            if content_encoding == "gzip":
                import gzip
                html_bytes = gzip.decompress(raw_body)
            elif content_encoding == "deflate":
                html_bytes = zlib.decompress(raw_body)
            elif content_encoding == "br":
                html_bytes = brotli.decompress(raw_body)
            else:
                html_bytes = raw_body
            html = html_bytes.decode("utf-8", errors="replace")
            decompressed = len(html_bytes)
            resp.close()
            html_path.write_text(html, encoding="utf-8")
            validation = validate_html(html)
            return DownloadResult(url=url, html=html, status_code=resp.status_code,
                fetch_source="requests_proxy" if proxy_used else "requests", html_path=html_path,
                validation_status=validation["status"], validation_reason=validation["reason"],
                blocked=validation["status"] == "BLOCKED", proxy_used=proxy_used, proxy_attempt=attempt,
                wire_bytes=wire_bytes, decompressed_bytes=decompressed, content_encoding=content_encoding)
        except ImportError:
            pass  # fall through to non-streaming path
        except req_lib.exceptions.RequestException as e:
            raise RuntimeError(f"Failed to download {url} (attempt {attempt}): {e}") from e

    if http_session is not None:
        try:
            response = http_session.get(url, headers=headers, proxies=proxies, timeout=timeout)
            response.raise_for_status()
            html = response.text
            html_path.write_text(html, encoding="utf-8")
            validation = validate_html(html)
            return DownloadResult(url=url, html=html, status_code=response.status_code,
                fetch_source="requests_proxy" if proxy_used else "requests", html_path=html_path,
                validation_status=validation["status"], validation_reason=validation["reason"],
                blocked=validation["status"] == "BLOCKED", proxy_used=proxy_used, proxy_attempt=attempt,
                wire_bytes=len(response.content), decompressed_bytes=len(html.encode("utf-8")),
                content_encoding=response.headers.get("Content-Encoding", ""))
        except req_lib.exceptions.RequestException as e:
            raise RuntimeError(f"Failed to download {url} (attempt {attempt}): {e}") from e
    else:
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                html = response.read().decode("utf-8", errors="replace")
                status_code = getattr(response, "status", None)
            html_path.write_text(html, encoding="utf-8")
            validation = validate_html(html)
            return DownloadResult(url=url, html=html, status_code=status_code, fetch_source="urllib",
                html_path=html_path, validation_status=validation["status"], validation_reason=validation["reason"],
                blocked=validation["status"] == "BLOCKED")
        except (HTTPError, URLError) as exc:
            raise RuntimeError(f"Failed to download {url}: {exc}") from exc
