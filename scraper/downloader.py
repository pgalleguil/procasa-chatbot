from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore

from classifier_rules import load_rule_sets
from config import AppConfig
from proxy_manager import ProxyChoice, ProxyManager

BLOCK_PATTERNS = (
    "verify you are human",
    "cf-chl-captcha",
    "captcha challenge",
    "access denied",
    "forbidden",
    "too many requests",
    "blocked",
)

HTML_HINTS = (
    "<html",
    "<!doctype html",
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


def validate_html(html: str) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", html or "").strip().lower()
    if len(text) < 200:
        return {"status": "INVALID", "reason": "too_short"}
    removed_patterns = _load_removed_patterns()
    if any(pattern.lower() in text for pattern in removed_patterns):
        return {"status": "LISTING_REMOVED", "reason": "listing_removed_text"}
    if any(pattern in text for pattern in BLOCK_PATTERNS):
        return {"status": "BLOCKED", "reason": "strong_block_signal"}
    if not any(pattern in text for pattern in HTML_HINTS):
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
    headers = _headers(config)
    timeout = config.request_timeout_seconds

    if requests is not None:
        response = requests.get(url, headers=headers, proxies=_requests_proxies(proxy), timeout=timeout)
        response.raise_for_status()
        html = response.text
        html_path.write_text(html, encoding="utf-8")
        validation = validate_html(html)
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

    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            html = response.read().decode("utf-8", errors="replace")
            status_code = getattr(response, "status", None)
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc

    html_path.write_text(html, encoding="utf-8")
    validation = validate_html(html)
    return DownloadResult(
        url=url,
        html=html,
        status_code=status_code,
        fetch_source="urllib",
        html_path=html_path,
        validation_status=validation["status"],
        validation_reason=validation["reason"],
        blocked=validation["status"] == "BLOCKED",
    )
