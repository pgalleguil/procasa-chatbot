"""Conservative recovery of Portal Inmobiliario MLC links republished with new IDs.

This module never guesses by title, street or price. It accepts only an exact
publisher-disclosed internal property code belonging to PROCASA SUCRE, or a
previously verified mapping with the same evidence. All reads use the caller's
DB adapter, preserving the chatbot's safe Prop360 projection.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
COLLECTION = "verified_portal_publication_links"
_PORTAL_HOSTS = frozenset({"portalinmobiliario.com", "portalinmobiliario.cl"})
_MLC_PATH = re.compile(r"^/MLC[-_](\d{8,13})(?:[-/]|$)", re.I)
_PUBLISHED_CODE = re.compile(r"c[oó]digo\s+de\s+la\s+propiedad\s*:?\s*(\d{4,7})\b", re.I)
_SELLER = re.compile(r"publicad[oa]\s+por\s+procasa\s+sucre\s+propiedades", re.I)
_CACHE: dict[str, tuple[float, str | None]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_SECONDS = 300
_MAX_HTML_BYTES = 1_500_000


def publication_identity(url: str) -> tuple[str, str] | None:
    """Return (MLC-..., canonical host) for an exact allowlisted listing URL."""
    try:
        parts = urlsplit(str(url or "").strip())
        host = (parts.hostname or "").casefold().removeprefix("www.")
        if parts.scheme.casefold() != "https" or host not in _PORTAL_HOSTS:
            return None
        if parts.username or parts.password or parts.port not in (None, 443):
            return None
        match = _MLC_PATH.match(parts.path or "")
        return (f"MLC-{match.group(1)}", host) if match else None
    except (ValueError, TypeError):
        return None


def _publisher_disclosed_code(url: str) -> str | None:
    """Parse publisher-controlled HTML; returns None on blocks or ambiguity."""
    from os import getenv
    if getenv("PORTAL_PROPERTY_LIVE_VERIFY", "true").strip().casefold() not in {"true", "1", "yes"}:
        return None
    identity = publication_identity(url)
    if not identity:
        return None
    cache_key = f"{identity[0]}|{urlsplit(url).hostname}"
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

    code = None
    try:
        response = requests.get(
            url, timeout=(2.0, 3.5), allow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ProcasaPropertyIdentity/1.0)",
                     "Accept": "text/html,application/xhtml+xml"},
            stream=True,
        )
        try:
            if response.status_code == 200 and "html" in response.headers.get("Content-Type", "").lower():
                blocks: list[bytes] = []
                total = 0
                for block in response.iter_content(chunk_size=64 * 1024):
                    total += len(block)
                    if total > _MAX_HTML_BYTES:
                        blocks = []
                        break
                    blocks.append(block)
                if blocks:
                    soup = BeautifulSoup(b"".join(blocks), "html.parser")
                    for tag in soup(["script", "style", "noscript"]):
                        tag.decompose()
                    page = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
                    publication_number = identity[0].split("-")[1]
                    visible_publication = re.search(
                        r"publicaci[oó]n\s*#\s*" + re.escape(publication_number) + r"\b",
                        page, re.I,
                    )
                    all_codes = set(_PUBLISHED_CODE.findall(page))
                    if _SELLER.search(page) and visible_publication and len(all_codes) == 1:
                        code = next(iter(all_codes))
        finally:
            response.close()
    except requests.RequestException:
        logger.info("[PORTAL_VERIFY] publisher unavailable external_id=%s", identity[0])
    except Exception:
        logger.exception("[PORTAL_VERIFY] rejected malformed portal document external_id=%s", identity[0])

    with _CACHE_LOCK:
        if len(_CACHE) >= 512:
            _CACHE.clear()
        _CACHE[cache_key] = (time.monotonic() + _CACHE_SECONDS, code)
    return code


def _confirmed_active_prop(db: Any, collection_name: str, raw_code: str, operation: str | None) -> dict | None:
    collection = db[collection_name]
    docs = list(collection.find({"codigo": str(raw_code)}).limit(3))
    if len(docs) != 1:
        return None
    prop = docs[0]
    state = prop.get("estado") or {}
    office = str(state.get("oficina") or prop.get("oficina_nombre") or (prop.get("resumen") or {}).get("oficina") or "")
    if "PROCASA SUCRE" not in office.upper():
        return None
    from .property_lookup import property_availability_state
    if property_availability_state(prop) != "PROPERTY_FOUND_AVAILABLE":
        return None
    kind = prop.get("tipo_operacion") or {}
    if operation == "arriendo" and kind.get("arriendo") is not True:
        return None
    if operation == "venta" and kind.get("venta") is not True:
        return None
    return prop


def recover_verified_portal_link(
    db: Any, url: str, collection_name: str, operation: str | None,
) -> tuple[dict | None, dict]:
    """No broad search: exact MLC -> verified code -> unique active Prop360."""
    identity = publication_identity(url)
    if identity is None:
        return None, {}
    external_id, host = identity
    claim_id = f"portal_inmobiliario:{external_id}"
    claim = db[COLLECTION].find_one({"_id": claim_id})
    if claim:
        if claim.get("portal") != "portal_inmobiliario" or claim.get("external_id") != external_id:
            return None, {"error_code": "VERIFIED_REFERENCE_CONFLICT"}
        prop = _confirmed_active_prop(db, collection_name, claim.get("property_code"), operation)
        if prop:
            return prop, {
                "match_method": "verified_portal_mapping", "verified_external_id": external_id,
                "verified_internal_code": str(prop.get("codigo")),
                "verification_source": str(claim.get("verification_source") or "public_portal"),
            }
        return None, {"error_code": "VERIFIED_REFERENCE_INACTIVE_OR_CONFLICT"}

    published_code = _publisher_disclosed_code(url)
    if not published_code:
        return None, {}
    prop = _confirmed_active_prop(db, collection_name, published_code, operation)
    if not prop:
        return None, {"error_code": "PUBLISHER_CODE_NOT_ACTIVE_IN_PORTFOLIO"}
    return prop, {
        "match_method": "publisher_verified_internal_code",
        "verified_external_id": external_id,
        "verified_internal_code": str(prop.get("codigo")),
        "verification_source": "public_portal",
    }


def persist_verified_portal_link(db: Any, url: str, property_code: str, source: str) -> bool:
    """Idempotent mapping only after exact external evidence and canonical match."""
    from datetime import datetime, timezone
    identity = publication_identity(url)
    if not identity or source not in {"public_portal", "operator_verified_public_portal"}:
        return False
    external_id, _ = identity
    doc_id = f"portal_inmobiliario:{external_id}"
    existing = db[COLLECTION].find_one({"_id": doc_id})
    if existing:
        return existing.get("property_code") == str(property_code)
    try:
        db[COLLECTION].insert_one({
            "_id": doc_id, "portal": "portal_inmobiliario",
            "external_id": external_id, "property_code": str(property_code),
            "url": url, "verification_source": source,
            "verified_at": datetime.now(timezone.utc),
        })
        return True
    except Exception:
        logger.exception("[PORTAL_VERIFY] mapping insertion failed external_id=%s", external_id)
        return False
