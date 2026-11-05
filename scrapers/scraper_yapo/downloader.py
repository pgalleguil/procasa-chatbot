from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from bs4 import BeautifulSoup

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
_RELATED_BLOCK_RE = re.compile(r"related|recomend|similar|sugerid|carousel|listing-card|ad-tile", re.I)
_INACTIVE_STATUS_RE = re.compile(
    r"\b(?:este anuncio|esta publicacion|este aviso)\s+(?:esta|se encuentra|fue|ha sido|ya no esta|no esta)\s+"
    r"(?:desactivad[oa]|retirad[oa]|eliminad[oa]|no disponible|disponible)\b|"
    r"\b(?:anuncio|aviso|publicacion)\s+(?:fue|ha sido|esta)\s+(?:retirad[oa]|eliminad[oa]|desactivad[oa])\b"
)
_ACTIVE_STATUS_RE = re.compile(r"\b(?:este anuncio|esta publicacion|este aviso)\s+(?:esta|permanece)\s+activo\b")
_INACTIVE_BADGE_RE = re.compile(r"\b(?:vendid[oa]|retirad[oa]|desactivad[oa]|eliminad[oa]|no disponible)\b")
_STATUS_NODE_RE = re.compile(r"status|availability|available|inactive|removed|sold|estado|availability|alert|notice", re.I)
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
    proxy_provider: str = "unknown"
    availability_status: str = "NO_VERIFICADO"
    availability_checked_at: str = ""
    availability_source: str = ""
    availability_evidence: list[str] = field(default_factory=list)


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


def _plain_primary_listing_text(source: str) -> tuple[str, list[str]]:
    """Return primary-page visible text and status-node text, excluding related ads."""
    soup = BeautifulSoup(source or "", "html.parser")
    primary = soup.find("main") or soup.select_one('[itemtype*="Product"]') or soup.body or soup
    for node in list(primary.find_all(["script", "style", "noscript"])):
        node.decompose()
    for node in list(primary.find_all(True)):
        if node.attrs is None:  # a related ancestor may already have decomposed this node
            continue
        attrs = " ".join((
            str(node.get("id") or ""),
            " ".join(str(value) for value in (node.get("class") or [])),
            str(node.get("data-testid") or ""),
            str(node.get("aria-label") or ""),
        ))
        if node is not primary and _RELATED_BLOCK_RE.search(attrs):
            node.decompose()

    text = " ".join(html_lib.unescape(primary.get_text(" ", strip=True)).split())
    status_nodes: list[str] = []
    for node in primary.find_all(True):
        attrs = " ".join((
            str(node.get("id") or ""),
            " ".join(str(value) for value in (node.get("class") or [])),
            str(node.get("data-testid") or ""),
            str(node.get("role") or ""),
            str(node.get("data-status") or ""),
            str(node.get("aria-label") or ""),
        ))
        node_text = " ".join(node.get_text(" ", strip=True).split())
        if node_text and (_STATUS_NODE_RE.search(attrs) or str(node.get("role") or "").lower() == "alert"):
            status_nodes.append(node_text)
    return text, list(dict.fromkeys(status_nodes))


def _configured_status_message(status_nodes: list[str]) -> str:
    for text in status_nodes:
        for pattern in _load_removed_patterns():
            normalized_pattern = _normalize_status_text(pattern)[0].strip()
            if not normalized_pattern:
                continue
            evidence = _matched_original_text(text, re.compile(re.escape(normalized_pattern)))
            if evidence:
                return evidence
    return ""


def _normalize_status_text(value: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(value.casefold()):
        for item in unicodedata.normalize("NFKD", char):
            if not unicodedata.combining(item):
                normalized.append(item)
                offsets.append(index)
    return "".join(normalized), offsets


def _matched_original_text(value: str, pattern: re.Pattern[str]) -> str:
    normalized, offsets = _normalize_status_text(value)
    match = pattern.search(normalized)
    if not match or not offsets:
        return ""
    start = offsets[match.start()]
    end = offsets[min(match.end() - 1, len(offsets) - 1)] + 1
    return value[start:end].strip()


def assess_yapo_availability(
    source: str,
    *,
    fetch_source: str,
    validation_status: str = "OK",
    status_code: int | None = None,
    checked_at: str | None = None,
    flashproxy_verified: bool = False,
) -> dict[str, Any]:
    """Assess only explicit primary-listing status; cached HTML is never current proof."""
    visible, status_nodes = _plain_primary_listing_text(source)
    texts = [visible, *status_nodes]
    inactive_evidence = next(
        (match for text in texts if (match := _matched_original_text(text, _INACTIVE_STATUS_RE))),
        "",
    )
    if not inactive_evidence:
        inactive_evidence = next(
            (match for text in status_nodes if (match := _matched_original_text(text, _INACTIVE_BADGE_RE))),
            "",
        )
    if not inactive_evidence:
        inactive_evidence = _configured_status_message(status_nodes)
    active_evidence = next(
        (match for text in status_nodes if (match := _matched_original_text(text, _ACTIVE_STATUS_RE))),
        "",
    )
    live_source = fetch_source == "flashproxy" or flashproxy_verified is True
    evidence_source = "flashproxy" if live_source else (fetch_source or "unknown")
    check_time = checked_at or (datetime.now(timezone.utc).isoformat() if live_source else "")
    if not live_source or status_code is not None and status_code >= 400:
        evidence = ["cached_or_non_live_html_does_not_confirm_current_availability"]
        if inactive_evidence:
            evidence.append(f"historical_status_message:{inactive_evidence}")
        return {
            "availability_status": "NO_VERIFICADO",
            "availability_checked_at": "",
            "availability_source": evidence_source,
            "availability_evidence": evidence,
        }
    if validation_status in {"INVALID", "BLOCKED"}:
        return {
            "availability_status": "NO_VERIFICADO",
            "availability_checked_at": check_time,
            "availability_source": evidence_source,
            "availability_evidence": [f"technical_status:{validation_status}"],
        }
    if inactive_evidence:
        return {
            "availability_status": "DESACTIVADO",
            "availability_checked_at": check_time,
            "availability_source": evidence_source,
            "availability_evidence": [inactive_evidence],
        }
    if active_evidence:
        return {
            "availability_status": "ACTIVO",
            "availability_checked_at": check_time,
            "availability_source": evidence_source,
            "availability_evidence": [active_evidence],
        }
    return {
        "availability_status": "NO_VERIFICADO",
        "availability_checked_at": check_time,
        "availability_source": evidence_source,
        "availability_evidence": ["no_explicit_primary_listing_status"],
    }


def _has_listing_detail_evidence(source: str) -> bool:
    return bool(LISTING_DETAIL_HREF_RE.search(source or ""))


def validate_html(html: str) -> dict[str, Any]:
    source = html or ""
    normalized_source = re.sub(r"\s+", " ", source).strip().lower()
    if len(normalized_source) < 200:
        return {"status": "INVALID", "reason": "too_short"}
    primary_text, status_nodes = _plain_primary_listing_text(source)
    scoped_text = " ".join((primary_text, *status_nodes))
    normalized_scoped = _normalize_status_text(scoped_text)[0]
    removed_patterns = _load_removed_patterns()
    if _INACTIVE_STATUS_RE.search(normalized_scoped) or any(
        _normalize_status_text(pattern)[0] in normalized_scoped for pattern in removed_patterns
    ) or any(_matched_original_text(text, _INACTIVE_BADGE_RE) for text in status_nodes):
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
            proxy_provider="cache",
            **assess_yapo_availability(html, fetch_source="cache", validation_status=validation["status"]),
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
        response = None
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = session.get(url, headers=headers, proxies=_requests_proxies(proxy), timeout=timeout)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    # Retry transport failures through the same paid proxy.
                    # HTTP denials are handled below and are never retried.
                    time.sleep(1.0 + attempt)
        if last_error is not None:
            raise RuntimeError(f"YAPO_PROXY_REQUEST_FAILED: {type(last_error).__name__}") from None
        if response is None:  # pragma: no cover - defensive for unusual transports
            raise RuntimeError("YAPO_PROXY_REQUEST_FAILED: no response")
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
            proxy_provider=str(getattr(proxy_manager, "provider", "generic") or "generic"),
            **assess_yapo_availability(
                html,
                fetch_source="requests_proxy",
                validation_status=validation["status"],
                status_code=response.status_code,
                flashproxy_verified=str(getattr(proxy_manager, "provider", "")) == "flashproxy",
            ),
        )

    # urllib has no safe proxy configuration here. Fail closed rather than
    # silently bypassing the configured proxy.
    raise RuntimeError("YAPO_PROXY_TRANSPORT_UNAVAILABLE: requests is required for proxy-only access.")
