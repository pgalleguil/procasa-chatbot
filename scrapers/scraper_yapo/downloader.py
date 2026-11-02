from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore

try:
    from .classifier_rules import load_rule_sets
    from .config import AppConfig
    from .proxy_manager import ProxyChoice, ProxyManager
except ImportError:  # direct script execution
    from classifier_rules import load_rule_sets
    from config import AppConfig
    from proxy_manager import ProxyChoice, ProxyManager

# HTTP 403/429 responses are rejected explicitly below. These HTML markers are
# reserved for challenge pages returned with HTTP 200; generic words such as
# "blocked" and "forbidden" are intentionally not searched across source HTML
# because they commonly occur in scripts, styles, and resource URLs.
BLOCK_VISIBLE_PATTERNS = (
    "verify you are human",
    "captcha challenge",
    "access denied",
    "access forbidden",
    "too many requests",
    "your request has been blocked",
    "your request was blocked",
)

BLOCK_HTML_MARKERS = (
    "cf-chl-captcha",
    "/cdn-cgi/challenge-platform/",
    "cf-browser-verification",
)

HTML_HINTS = (
    "<html",
    "<!doctype html",
)
LISTING_DETAIL_HREF_RE = re.compile(
    r"(?:href|data-href)\s*=\s*['\"][^'\"]*?/bienes-raices-(?:venta|alquiler)-"
    r"[^'\"<>\s]*?/\D*\d{6,}(?:[/?#][^'\"<>\s]*)?['\"]",
    re.I,
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


def md5_url(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def html_path_for_url(url: str, config: AppConfig, batch_id: str | None = None) -> Path:
    fid = md5_url(url)
    if batch_id:
        return config.html_dumps_dir / batch_id / f"{fid}.html"
    return config.html_dumps_dir / f"{fid}.html"


def _load_removed_patterns() -> list[str]:
    rule_sets = load_rule_sets()
    return [str(item) for item in rule_sets.get("listing_removed_patterns", []) if str(item).strip()]


def _has_listing_detail_evidence(source: str) -> bool:
    return bool(LISTING_DETAIL_HREF_RE.search(source or ""))


def validate_html(html: str) -> dict[str, Any]:
    source = html or ""
    normalized_source = re.sub(r"\s+", " ", source).strip().lower()
    if len(normalized_source) < 200:
        return {"status": "INVALID", "reason": "too_short"}
    removed_patterns = _load_removed_patterns()
    if any(pattern.lower() in normalized_source for pattern in removed_patterns):
        return {"status": "LISTING_REMOVED", "reason": "listing_removed_text"}

    # Inspect rendered markup/text without script/style content for human-facing
    # block messages. Normal Yapo pages can reference the Cloudflare challenge
    # platform from an ordinary application script, so a raw source marker is
    # only blocking when the page does not also contain listing evidence.
    visible_source = re.sub(r"(?is)<(script|style|noscript)\b[^>]*>.*?</\1\s*>", " ", source)
    visible_normalized = re.sub(r"\s+", " ", visible_source).strip().lower()
    raw_block_signal = any(marker in normalized_source for marker in BLOCK_HTML_MARKERS)
    visible_block_signal = any(marker in visible_normalized for marker in BLOCK_HTML_MARKERS)
    if visible_block_signal or (raw_block_signal and not _has_listing_detail_evidence(source)):
        return {"status": "BLOCKED", "reason": "strong_block_signal"}
    visible_text = re.sub(r"(?s)<[^>]+>", " ", visible_source)
    visible_text = re.sub(r"\s+", " ", visible_text).strip().lower()
    if any(pattern in visible_text for pattern in BLOCK_VISIBLE_PATTERNS):
        return {"status": "BLOCKED", "reason": "strong_block_signal"}
    if not any(pattern in normalized_source for pattern in HTML_HINTS):
        return {"status": "INVALID", "reason": "not_html"}
    return {"status": "OK", "reason": "valid_html"}


def _headers(config: AppConfig) -> dict[str, str]:
    headers = {
        "User-Agent": config.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    headers.update(config.extra_headers())
    return headers


def _requests_proxies(proxy: ProxyChoice | None) -> dict[str, str] | None:
    if proxy is None:
        return None
    return proxy.requests_proxies


def _write_response_diagnostic(html_path: Path, status_code: int | None, body: str) -> Path:
    """Persist only the response body under the batch-scoped hashed path.

    Request headers, proxy URLs, and credentials are intentionally excluded.
    """
    suffix = f".http_{status_code}.html" if status_code is not None else ".http_error.html"
    diagnostic_path = html_path.with_suffix(suffix)
    diagnostic_path.write_text(body or "", encoding="utf-8")
    return diagnostic_path


def download_html(
    url: str,
    config: AppConfig,
    proxy_manager: ProxyManager | None = None,
    *,
    batch_id: str | None = None,
    force_refresh: bool = False,
) -> DownloadResult:
    html_path = html_path_for_url(url, config, batch_id=batch_id)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    if html_path.exists() and not force_refresh:
        html = html_path.read_text(encoding="utf-8", errors="replace")
        validation = validate_html(html)
        return DownloadResult(
            url=url,
            html=html,
            status_code=200,
            fetch_source="cache",
            html_path=html_path,
            validation_status=validation["status"],
            validation_reason=validation["reason"],
            blocked=validation["status"] == "BLOCKED",
        )

    proxy = proxy_manager.next_proxy() if proxy_manager and proxy_manager.has_proxies() else None
    if proxy is None:
        raise RuntimeError("YAPO_PROXY_REQUIRED: no se realiza una conexión directa.")
    headers = _headers(config)
    timeout = config.request_timeout_seconds

    if requests is not None:
        # The caller walks pages serially; a short fixed pause keeps the pilot
        # at one in-flight Yapo request with a moderate request cadence.
        time.sleep(1.0)
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(url, headers=headers, proxies=_requests_proxies(proxy), timeout=timeout)
        except Exception as exc:
            raise RuntimeError(f"YAPO_PROXY_REQUEST_FAILED: {type(exc).__name__}") from None
        if response.status_code >= 400:
            diagnostic_path = _write_response_diagnostic(
                html_path,
                response.status_code,
                str(getattr(response, "text", "") or ""),
            )
            if response.status_code == 407:
                raise RuntimeError(
                    "YAPO_PROXY_AUTH_407: autenticación HTTP del proxy rechazada; "
                    f"respuesta guardada en {diagnostic_path}."
                )
            if response.status_code in {401, 403, 429, 451}:
                raise RuntimeError(
                    f"YAPO_BLOCKED: HTTP {response.status_code}; solicitudes detenidas; "
                    f"respuesta guardada en {diagnostic_path}."
                )
            raise RuntimeError(
                f"YAPO_HTTP_ERROR: HTTP {response.status_code}; "
                f"respuesta guardada en {diagnostic_path}."
            )
        response.raise_for_status()
        html = response.text
        validation = validate_html(html)
        if validation["status"] == "BLOCKED":
            # Keep the exact response in the batch directory for post-run
            # inspection, while leaving the canonical cache absent so a later
            # run is still allowed to retry the URL.
            html_path.with_suffix(".blocked.html").write_text(html, encoding="utf-8")
        else:
            html_path.write_text(html, encoding="utf-8")
        return DownloadResult(
            url=url,
            html=html,
            status_code=response.status_code,
            fetch_source="requests_proxy" if proxy else "requests",
            html_path=html_path,
            validation_status=validation["status"],
            validation_reason=validation["reason"],
            blocked=validation["status"] == "BLOCKED",
        )

    # urllib has no safe proxy configuration here. Fail closed rather than
    # silently bypassing the configured proxy.
    raise RuntimeError("YAPO_PROXY_TRANSPORT_UNAVAILABLE: requests is required for proxy-only access.")
