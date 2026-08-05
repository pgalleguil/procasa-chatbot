"""Yapo.cl property scraper pipeline.

This module rebuilds the scraper as a self-contained pipeline with the same
core stages described in the project notes:

1. Discovery of listing detail URLs from Yapo search pages.
2. Fast-path HTML download with local backup.
3. Offline parsing of the saved HTML.
4. Rule-based seller classification.
5. Optional JSON batch output for downstream QA / Mongo ingestion.

The implementation intentionally avoids hard coupling to the rest of the repo
so it can run even when the surrounding helpers are incomplete or missing.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import re
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

try:  # Optional dependency.
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover - optional dependency.
    BeautifulSoup = None  # type: ignore

try:  # Optional dependency.
    from curl_cffi import requests as curl_requests  # type: ignore
except Exception:  # pragma: no cover - optional dependency.
    curl_requests = None  # type: ignore

LOGGER = logging.getLogger("yapo_scraper")

LISTING_REMOVED_PATTERNS = (
    "anuncio borrado",
    "anuncio eliminado",
    "eliminado por el anunciante",
    "publicaci\u00f3n eliminada",
    "listing removed",
    "removed by the seller",
    "anuncio desactivado",
    "anuncio expirado",
    "publicaci\u00f3n no existe",
    "publicacion no existe",
    "expirado",
    "no encontramos",
    "ya no est\u00e1 disponible",
    "ya no esta disponible",
    "no est\u00e1 disponible",
    "no esta disponible",
)

CORREDOR_PATTERNS = (
    "corredor",
    "corretaje",
    "inmobiliaria",
    "broker",
    "agente",
    "empresa",
    "staff",
    "administradora",
    "gesti\u00f3n inmobiliaria",
)

OWNERSHIP_PATTERNS = (
    "due\u00f1o",
    "dueña",
    "due\u00f1a",
    "particular",
    "sin corredor",
    "trato directo",
    "directo con due\u00f1o",
)

# Strong commercial/broker identity patterns — trigger CORREDOR_SEGURO immediately
STRONG_BROKER_PATTERNS = (
    # English real estate terms
    "properties", "property",
    # Spanish real estate terms
    "propiedades", "inmobiliaria", "corretaje", "corredores",
    "corredora", "corredor", "bienes ra\u00edces", "bienes raices",
    # Business entity / legal form
    "broker", "real estate", "group", "grupo",
    "spa", "ltda", "s.a.", "srl", "eirl", "limitada",
    # Partnership indicators
    "asociados", "asociada",
    # Management / administration
    "administraci\u00f3n", "administracion", "administradora",
    "gesti\u00f3n inmobiliaria", "gestion inmobiliaria",
    # Real estate specific
    "inmuebles", "inmobiliario", "consultora", "consultores",
    "corporaci\u00f3n", "corporacion",
    # Known real estate brand patterns
    "re max", "re/max", "remax",
    "exp chile", "exp realty",
    "coldwell", "banker", "realty",
    "procasa", "nexxos",
    "kutt property", "hunter group", "hyc asociados",
)


DISCOVERY_SEED_URLS = [
    "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana-santiago&q=withcat.bienes-raices-venta-de-propiedades-apartamentos,bienes-raices-venta-de-propiedades-casas|f_price.120000000-|f_currency.CLP",
]
#"https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana-la-florida&q=withcat.bienes-raices-venta-de-propiedades-apartamentos,bienes-raices-venta-de-propiedades-casas|f_price.160000000-"
#"https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana-macul&q=withcat.bienes-raices-venta-de-propiedades-apartamentos,bienes-raices-venta-de-propiedades-casas|f_price.140000000-|f_currency.CLP"

@dataclass(slots=True)
class ScraperConfig:
    base_url: str = "https://www.yapo.cl"
    max_pages: int = 4
    max_urls_per_session: int = 100
    target_new_urls: int = 500
    html_dump_dir: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / "html_dumps"
    )
    output_dir: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / "reports"
    )
    request_timeout: int = 30
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )
    extra_headers: dict[str, str] = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc
    path = parsed.path or "/"
    normalized = parsed._replace(scheme=scheme, netloc=netloc, path=path, params="", fragment="")
    return urlunparse(normalized)


def _md5_url(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def _html_path_for_url(url: str, html_dump_dir: Path) -> Path:
    return html_dump_dir / f"{_md5_url(url)}.html"


def _build_headers(cfg: ScraperConfig) -> dict[str, str]:
    headers = {
        "User-Agent": cfg.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    headers.update(cfg.extra_headers)
    return headers


def _strip_html(text: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _text_contains_any(text: str, patterns: Iterable[str]) -> bool:
    low = text.lower()
    return any(pattern in low for pattern in patterns)


def _safe_json_dump(path: Path, payload: Any) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _html_validation_status(html: str) -> dict[str, Any]:
    text = _strip_html(html)
    lowered = text.lower()
    if not text or len(text) < 200:
        return {
            "status": "INVALID",
            "reason": "html_too_short",
            "evidence": text[:120],
        }
    if _text_contains_any(lowered, LISTING_REMOVED_PATTERNS):
        return {
            "status": "LISTING_REMOVED",
            "reason": "listing_removed_pattern",
            "evidence": next((p for p in LISTING_REMOVED_PATTERNS if p in lowered), "removed"),
        }
    if "captcha" in lowered or "access denied" in lowered or "forbidden" in lowered:
        return {
            "status": "BLOCKED",
            "reason": "access_blocked",
            "evidence": text[:160],
        }
    if "página no encontrada" in lowered or "page not found" in lowered or "no encontrada" in lowered:
        return {
            "status": "INVALID",
            "reason": "page_not_found",
            "evidence": text[:160],
        }
    return {
        "status": "OK",
        "reason": "valid_html",
        "evidence": text[:160],
    }


def html_validator(html: str) -> dict[str, Any]:
    """Public wrapper retained for compatibility with the old pipeline."""
    return _html_validation_status(html)


# ---------------------------------------------------------------------------
# Seller name validation helper
# ---------------------------------------------------------------------------
_SELLER_CTA_RE = re.compile(
    r'\bquieres\b|\bagendar\b|\bd\u00e9janos\b|\benv\u00edanos\b|\bencantados\b'
    r'|\bcontáctame\b|\bcontáctanos\b|\bme interesa\b|\bcompleta tus\b'
    r'|\bbuscas\b|\bagenda\b|\benv\u00eda\b|\bcontactar\b',
    re.I | re.UNICODE
)


def _is_valid_seller_name(text: str) -> bool:
    """Return True only if text looks like an actual seller name, not a CTA phrase or description."""
    if not text or len(text) < 3:
        return False
    t = text.strip()
    low_t = t.lower()
    
    # reject if starts with question mark
    if t.startswith("¿") or t.startswith("?"):
        return False
    # reject if contains hashtag
    if "#" in t:
        return False
    # reject if too many words (CTA sentences are long)
    words = t.split()
    if len(words) > 8:
        return False
        
    # Reject descriptive starts
    _desc_starts = (
        "el edificio", "la propiedad", "el departamento", "la casa", "cuenta con"
    )
    if low_t.startswith(_desc_starts):
        return False
    
    # Reject if it looks like a full sentence (contains period + space + more words)
    if re.search(r'\.\s+\w', t):
        return False
    # Reject if starts with "Documentación" or similar document references
    if low_t.startswith("documentaci"):
        return False
    # Reject if contains "venta directa" or "dueño directo" or "trato directo"
    if re.search(r'\b(venta directa|due.o directo|trato directo|sin comisi.n)\b', low_t):
        return False
    # Reject if "propietario" appears as a descriptive phrase (more than just the word alone)
    if "propietario" in low_t and len(t.split()) > 1:
        return False
    # Reject sentence-like structures with common spanish verbs
    if re.search(r'\b(es|son|tiene|tienen|vende|venden|ofrece|ofrecen|busca|buscan|se vende|se arrienda)\b', low_t) and len(t.split()) > 2:
        return False
        
    # Reject common descriptive verbs indicating a property description rather than a name
    if re.search(r'\b(cuenta\b|incluye\b|dispone\b|ofrece\b|ubicado\b|ubicada\b)\b', low_t):
        return False

    # reject known CTA verbs
    if _SELLER_CTA_RE.search(t):
        return False
    # reject template remnants
    if re.search(r"\$object|'variant'|'form'|'cta'|-->|<!--|=> ", t):
        return False
    # reject generic UI strings
    _ui_strings = {
        "contactar", "llamar", "whatsapp", "mensaje", "enviar", "ver teléfono", "ver telefono",
        "leer más", "leer mas", "leer m\u00e1s", "inicio", "anterior", "siguiente",
        "compartir", "reportar abuso", "volver a resultados", "ver teléfono", "publicar aviso",
    }
    if low_t in _ui_strings:
        return False
    # reject location-only suffixes like "- Las Condes" or "- La Florida, Región..."
    if t.startswith("-"):
        return False
    # reject strings that are only a region name like "Región Metropolitana"
    if re.match(r'^regi[oó]n\b', t, re.I):
        return False
    return True


def _extract_publisher_identity_candidates_bs4(
    soup: Any,
    seller_name: str,
    seller_text: str,
    seller_avatar_alt: str,
    json_ld: dict[str, Any],
) -> list[dict[str, str]]:
    """Extract all publisher identity candidates from BS4-parsed soup."""
    candidates: list[dict[str, str]] = []
    seen_values: set[str] = set()
    _avatar_generic_lower = {g.lower() for g in {"avatar", "user-avatar", "usuario", "foto", "imagen", "user", "logo", "sin imagen"}}

    def _add(source: str, value: str) -> None:
        v = value.strip()
        if not v or len(v) < 3:
            return
        low = v.lower().strip()
        if low in seen_values or low in _avatar_generic_lower:
            return
        seen_values.add(low)
        candidates.append({"source": source, "value": v})

    if seller_name:
        _add("contact_name", seller_name)
    if seller_text:
        _add("seller_text", seller_text)
    if seller_avatar_alt:
        _add("seller_avatar_alt", seller_avatar_alt)

    # /user/profile/ link text
    profile_link = soup.select_one("a[href*='/user/profile/']")
    if profile_link:
        txt = profile_link.get_text(" ", strip=True)
        if txt:
            _add("user_profile_link", txt)

    # Images with specific publisher patterns
    for img in soup.find_all("img", src=True, alt=True):
        src = img.get("src", "")
        alt = img.get("alt", "").strip()
        if not alt or len(alt) < 3:
            continue
        if alt.lower() in _avatar_generic_lower:
            continue
        if "initial-avatar" in src:
            _add("initial-avatar alt", alt)
        elif "t_user_logo" in src:
            _add("t_user_logo alt", alt)
        elif "t_user_photo" in src:
            _add("t_user_photo alt", alt)
        elif "users/photo" in src or "users/logo" in src:
            _add("users_photo_logo alt", alt)

    # JSON-LD seller name (top-level or nested in offers.seller)
    seller_ld = json_ld.get("seller", {})
    if not (isinstance(seller_ld, dict) and seller_ld.get("name")):
        offers = json_ld.get("offers", {})
        if isinstance(offers, dict):
            seller_ld = offers.get("seller", {})
    if isinstance(seller_ld, dict) and seller_ld.get("name"):
        sn = seller_ld["name"].strip()
        _ld_template_words = {"agente", "propietario", "dueño", "dueña", "vendedor", "seller", "owner"}
        if sn and sn.lower().strip() not in _ld_template_words:
            _add("seller_jsonld_name", sn)

    return candidates


def _extract_with_bs4(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")  # type: ignore[misc]
    out: dict[str, Any] = {}
    import json

    # 1. JSON-LD Extraction
    json_ld = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        json_ld.update(item)
            elif isinstance(data, dict):
                json_ld.update(data)
        except Exception:
            continue

    # 2. Title  (JSON-LD name is the site name, not listing title — skip it)
    title = ""
    for selector in ["h1", "meta[property='og:title']", "title"]:
        node = soup.select_one(selector)
        if node:
            txt = node.get_text(" ", strip=True) if node.name != "meta" else node.get("content", "")
            if txt and not txt.startswith("meta[") and txt.lower() != "yapo":
                title = txt
                break
    if not title:
        meta_title = soup.find("meta", attrs={"name": "title"})
        if meta_title and meta_title.get("content") and meta_title["content"].strip().lower() != "yapo":
            title = meta_title["content"].strip()
    out["title"] = title.strip()

    # 3. Price
    price = ""
    body_text_full = soup.get_text(" ", strip=True)
    match = re.search(r'Precio\s+(UF[\d.,]+|\$[\d.,]+)', body_text_full, re.I)
    if match:
        price = match.group(1)
    if not price and json_ld.get("offers", {}).get("price"):
        currency = json_ld.get("offers", {}).get("priceCurrency", "$")
        price = f"{currency}{json_ld['offers']['price']}"
    if not price:
        for selector in ["[data-qa='price']", ".price", ".item-price", "strong.price", "span.price"]:
            node = soup.select_one(selector)
            if node:
                txt = node.get_text(" ", strip=True)
                if "UF" in txt or "$" in txt:
                    price = txt
                    break
    out["price"] = price.strip()

    # 4. Description
    description = ""
    desc_start = False
    desc_lines = []
    
    stop_words = {
        "leer más", "leer m\u00e1s", "enviar mensaje al vendedor", "invalid", "me interesa el anuncio",
        "completa tus datos", "contactar", "llamar", "whatsapp", "beneficios", "inicio inmuebles",
        "más anuncios de este vendedor", "m\u00e1s anuncios de este vendedor", "casas similares", 
        "departamentos similares", "centro de ayuda", "legal"
    }
    
    for el in soup.stripped_strings:
        text = el.strip()
        if not text:
            continue
            
        low_text = text.lower()
        if low_text in ("descripción", "descripci\u00f3n"):
            desc_start = True
            continue
            
        if desc_start:
            if low_text in stop_words or low_text.startswith("inicio inmuebles"):
                break
            desc_lines.append(text)
            
    if desc_lines:
        description = " ".join(desc_lines)
    else:
        desc_node = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
        if desc_node and desc_node.get("content"):
            description = desc_node["content"].strip()
            
    out["description"] = description
    out["meta_description"] = description

    # 5. Seller Name and Text
    seller_name = ""
    seller_text = ""

    # Priority 1: JSON-LD seller block (top-level or nested in offers.seller)
    seller_ld = json_ld.get("seller", {})
    if not (isinstance(seller_ld, dict) and seller_ld.get("name")):
        offers = json_ld.get("offers", {})
        if isinstance(offers, dict):
            seller_ld = offers.get("seller", {})
    if isinstance(seller_ld, dict) and seller_ld.get("name"):
        sn = seller_ld["name"].strip()
        # Only use if meaningful: not template words like "Agente" or "Propietario" alone
        _ld_template_words = {"agente", "propietario", "dueño", "dueña", "vendedor", "seller", "owner"}
        low_sn = sn.lower().strip()
        if low_sn in _ld_template_words:
            # Template/placeholder name — only use if nothing else found
            pass  # fall through to other priorities below
        elif _is_valid_seller_name(sn):
            seller_name = sn

    # Priority 2: scan texts AFTER "Información del vendedor"
    if not seller_name:
        texts = list(soup.stripped_strings)
        _info_triggers = {"información del vendedor", "informacion del vendedor"}
        _stop_after_seller = {
            "contactar", "llamar", "whatsapp", "enviar mensaje al vendedor",
            "me interesa el anuncio", "completa tus datos", "ver teléfono", "ver telefono"
        }
        for i, text in enumerate(texts):
            if text.lower() in _info_triggers:
                for j in range(i + 1, min(i + 6, len(texts))):
                    candidate = texts[j].strip()
                    low_c = candidate.lower()
                    if low_c in _stop_after_seller:
                        break
                    if low_c.startswith("se uni"):
                        break
                    if _is_valid_seller_name(candidate):
                        seller_name = candidate
                        if j + 1 < len(texts):
                            nxt = texts[j + 1].strip()
                            if _is_valid_seller_name(nxt) and "-" in nxt:
                                seller_text = f"{candidate} {nxt}"
                            else:
                                seller_text = candidate
                        else:
                            seller_text = candidate
                        break
                break

    # Priority 3: scan texts BEFORE "Enviar mensaje al vendedor"
    if not seller_name:
        texts = list(soup.stripped_strings)
        _send_triggers = {"enviar mensaje al vendedor"}
        for i, text in enumerate(texts):
            if text.lower() in _send_triggers:
                candidates = []
                for j in range(1, 6):
                    if i - j >= 0:
                        candidates.insert(0, texts[i - j])
                clean_cands = [c for c in candidates if _is_valid_seller_name(c)]
                if clean_cands:
                    seller_name = clean_cands[0]  # first = furthest back = company/person name
                    seller_text = " ".join(clean_cands)
                break

    # Priority 4: CSS selectors
    if not seller_name:
        for selector in [".seller", ".user", ".profile", "[data-qa='seller-name']", "[data-qa='user-name']", ".contact_name"]:
            node = soup.select_one(selector)
            if node:
                candidate = node.get_text(" ", strip=True)
                if _is_valid_seller_name(candidate):
                    seller_name = candidate
                    seller_text = candidate
                    break

    # Priority 4: /user/profile/ link text
    if not seller_name:
        profile_link = soup.select_one("a[href*='/user/profile/']")
        if profile_link:
            candidate = profile_link.get_text(" ", strip=True)
            if _is_valid_seller_name(candidate):
                seller_name = candidate
                seller_text = candidate

    out["seller_name"] = seller_name.strip()
    out["seller_text"] = seller_text.strip()

    # 5b. Seller avatar alt — extract brand name from publisher's avatar/logo image
    seller_avatar_alt = ""
    _avatar_generic = {"avatar", "user-avatar", "usuario", "foto", "imagen", "user", "logo", "sin imagen"}
    _avatar_src_patterns = ("t_user_photo", "t_user_logo", "users/photo", "users/logo", "initial-avatar")
    # Look for img with t_user_photo / t_user_logo / users/photo in src (publisher avatar/logo)
    for img in soup.find_all("img", src=True, alt=True):
        src = img.get("src", "")
        alt = img.get("alt", "").strip()
        if not alt or len(alt) < 3:
            continue
        if alt.lower() in _avatar_generic:
            continue
        if any(p in src for p in _avatar_src_patterns):
            seller_avatar_alt = alt
            break
    if not seller_avatar_alt:
        # Fallback: any img with onerror pointing to user-avatar (Yapo's publisher image pattern)
        for img in soup.find_all("img", src=True, alt=True):
            src = img.get("src", "")
            alt = img.get("alt", "").strip()
            if not alt or len(alt) < 3:
                continue
            if alt.lower() in _avatar_generic:
                continue
            onerror = img.get("onerror", "")
            if "user-avatar.png" in onerror:
                seller_avatar_alt = alt
                break
    if not seller_avatar_alt:
        # Fallback: img near contact/seller sections with meaningful alt
        for selector in [".contact_name img", ".seller img", ".user img", "[data-qa='seller-name'] img",
                         "[data-qa='user-name'] img", ".contact_address img", "a[href*='/user/profile/'] img"]:
            node = soup.select_one(selector)
            if node and node.get("alt"):
                alt = node["alt"].strip()
                if alt.lower() not in _avatar_generic and len(alt) >= 3:
                    seller_avatar_alt = alt
                    break
    out["seller_avatar_alt"] = seller_avatar_alt

    # Images extraction: <img> tags + JSON-LD/scripts (Yapo CDN = photos.encuentra24.com)
    image_urls: list[str] = []
    _ASSET_FRAGS = (
        "/static/", "/assets/", "/img/ui/", "/icons/", "logo", "favicon",
        "placeholder", "spinner", "avatar", "userway", "accessibility",
        "pixel.gif", "pixel.png", "blank.gif", "transparent", "badge",
        "pro-seal", "cnseal", "octagon", "verified", "e24static", "user-avatar",
        "common-library", "header-footer", "buyers/assets",
    )
    _ASSET_DOMAINS = (
        "fonts.gstatic.com", "fonts.googleapis.com",
        "static.yapo.cl", "assets.yapo.cl",
        "storage.googleapis.com",
    )
    # Yapo uses Cloudinary transforms on photos.encuentra24.com (no extension required)
    _YAPO_CDN_DOMAINS = ("photos.encuentra24.com", "media.yapo.cl", "img.yapo.cl")

    def _is_real_photo(url: str) -> bool:
        ul = url.lower()
        if any(d in ul for d in _ASSET_DOMAINS):
            return False
        if any(f in ul for f in _ASSET_FRAGS):
            return False
        # Yapo CDN (Cloudinary) — accept transform paths, reject badge seals
        if any(d in ul for d in _YAPO_CDN_DOMAINS):
            if "/t_cnseal" in ul:
                return False
            if re.search(r"/(?:t_or_|t_thumb|f_auto|v\d+/(?:cl|pe|ar|bo|ec|uy))", ul):
                return True
            if re.search(r"\.(?:jpg|jpeg|png|webp)(?:\?|$)", ul):
                return True
            return False
        # All other domains: must have image extension
        return bool(re.search(r"\.(?:jpg|jpeg|png|webp)(?:\?|$)", ul))

    # 1) <img> tags (src, data-src, srcset)
    for img in soup.select("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy", "srcset", "data-srcset"):
            val = img.get(attr)
            if val:
                for segment in str(val).split(","):
                    candidate = segment.split()[0].strip()
                    if candidate:
                        u = urljoin("https://www.yapo.cl", candidate)
                        if _is_real_photo(u) and u not in image_urls:
                            image_urls.append(u)

    # 2) Script bodies — scan for inline CDN references
    for script in soup.find_all("script"):
        body = script.string or ""
        if not body.strip() or "encuentra24" not in body.lower():
            continue
        for raw_u in re.findall(
            r'https?://(?:photos\.encuentra24\.com|media\.yapo\.cl|img\.yapo\.cl)[^\s"\'\\><]+',
            body, re.I,
        ):
            raw_u = raw_u.rstrip("\\'\"\)")
            if _is_real_photo(raw_u) and raw_u not in image_urls:
                image_urls.append(raw_u)

    # 3) Full HTML raw scan — catches CDN URLs in href/src/data attributes
    #    (Yapo photo URLs appear in the HTML source, not always inside scripts)
    _html_raw = str(soup) if BeautifulSoup is not None else ""
    for raw_u in re.findall(
        r'https?://(?:photos\.encuentra24\.com|media\.yapo\.cl|img\.yapo\.cl)[^\s"\'\\><]+',
        _html_raw, re.I,
    ):
        raw_u = raw_u.rstrip("\\'\"\)")
        if _is_real_photo(raw_u) and raw_u not in image_urls:
            image_urls.append(raw_u)

    _vis_src = "script_or_json_ld" if image_urls else "none_in_static_html"
    out["image_urls"] = image_urls
    out["image_urls_detected_count"] = len(image_urls)
    out["visual_image_source"] = _vis_src

    attrs: dict[str, str] = {}
    for row in soup.select("table tr, li, [class*='label'], [data-qa]"):
        text = row.get_text(" ", strip=True)
        if ":" in text:
            key, value = text.split(":", 1)
            key = re.sub(r"\s+", " ", key).strip().lower()
            value = re.sub(r"\s+", " ", value).strip()
            if key and value and key not in attrs:
                attrs[key] = value
        else:
            # Try to handle <div class="...label">Label <p>Value</p></div>
            # where the label is text and value is in a child tag.
            children = [c for c in row.children if c.name]
            if children and row.text:
                # the first piece of text might be the label
                label_text = ""
                for c in row.contents:
                    if isinstance(c, str) and c.strip():
                        label_text = c.strip()
                        break
                value_text = " ".join(c.get_text(" ", strip=True) for c in children if c.get_text(strip=True))
                if label_text and value_text:
                    k = re.sub(r"\s+", " ", label_text).strip().lower()
                    v = re.sub(r"\s+", " ", value_text).strip()
                    if k and v and k not in attrs:
                        attrs[k] = v

    out["attributes"] = attrs

    out["body_text"] = re.sub(r"\s+", " ", body_text_full)

    # Publisher identity candidates
    out["publisher_identity_candidates"] = _extract_publisher_identity_candidates_bs4(
        soup, out.get("seller_name", ""), out.get("seller_text", ""),
        out.get("seller_avatar_alt", ""), json_ld,
    )

    # 6. Canonical URL
    canonical = soup.find("link", attrs={"rel": "canonical"})
    og_url = soup.find("meta", attrs={"property": "og:url"})
    if canonical and canonical.get("href"):
        out["canonical_url"] = canonical["href"].strip().replace("-departamentos/", "-apartamentos/")
    elif og_url and og_url.get("content"):
        out["canonical_url"] = og_url["content"].strip().replace("-departamentos/", "-apartamentos/")
    else:
        out["canonical_url"] = ""

    return out


def _extract_publisher_identity_candidates_regex(
    html: str,
    seller_name: str,
    seller_text: str,
    seller_avatar_alt: str,
    json_ld: dict[str, Any],
) -> list[dict[str, str]]:
    """Extract publisher identity candidates from raw HTML via regex (no BS4)."""
    candidates: list[dict[str, str]] = []
    seen_values: set[str] = set()
    _avatar_generic_lower = {g.lower() for g in {"avatar", "user-avatar", "usuario", "foto", "imagen", "user", "logo", "sin imagen"}}

    def _add(source: str, value: str) -> None:
        v = value.strip()
        if not v or len(v) < 3:
            return
        low = v.lower().strip()
        if low in seen_values or low in _avatar_generic_lower:
            return
        seen_values.add(low)
        candidates.append({"source": source, "value": v})

    if seller_name:
        _add("contact_name", seller_name)
    if seller_text:
        _add("seller_text", seller_text)
    if seller_avatar_alt:
        _add("seller_avatar_alt", seller_avatar_alt)

    # /user/profile/ link text
    m = re.search(r'<a[^>]+href=["\'][^"\']+/user/profile/[^>]*>\s*([^<]+?)\s*</a>', html, re.I)
    if m:
        txt = m.group(1).strip()
        if txt:
            _add("user_profile_link", txt)

    # Images with specific publisher patterns
    _avatar_src_pat = r'(?:initial-avatar|t_user_logo|t_user_photo|users/photo|users/logo)'
    for m in re.finditer(
        r'<img[^>]*src=["\'][^"\']*(' + _avatar_src_pat + r')[^"\']*["\'][^>]*alt=["\']([^"\']+)["\']',
        html, re.I,
    ):
        src_type = m.group(1)
        alt = m.group(2).strip()
        if alt and len(alt) >= 3 and alt.lower() not in _avatar_generic_lower:
            _add(f"{src_type} alt", alt)

    # JSON-LD seller name (top-level or nested in offers.seller)
    seller_ld = json_ld.get("seller", {})
    if not (isinstance(seller_ld, dict) and seller_ld.get("name")):
        offers = json_ld.get("offers", {})
        if isinstance(offers, dict):
            seller_ld = offers.get("seller", {})
    if isinstance(seller_ld, dict) and seller_ld.get("name"):
        sn = seller_ld["name"].strip()
        _ld_template_words = {"agente", "propietario", "dueño", "dueña", "vendedor", "seller", "owner"}
        if sn and sn.lower().strip() not in _ld_template_words:
            _add("seller_jsonld_name", sn)

    return candidates


def _extract_without_bs4(html: str) -> dict[str, Any]:
    import json
    out: dict[str, Any] = {}
    
    # 1. JSON-LD
    json_ld = {}
    for script_data in re.findall(r'<script\s+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.DOTALL):
        try:
            data = json.loads(script_data)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        json_ld.update(item)
            elif isinstance(data, dict):
                json_ld.update(data)
        except Exception:
            pass

    # 2. Title
    title = ""
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\' ][^"\']*)["\']', html, re.I)
    if m and m.group(1).strip().lower() != "yapo":
        title = m.group(1).strip()
    if not title:
        m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.I | re.DOTALL)
        if m:
            t = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            if t and t.lower() != "yapo":
                title = t
    if not title:
        m = re.search(r'<title[^>]*>(.*?)</title>', html, re.I)
        if m and m.group(1).strip().lower() != "yapo":
            title = re.sub(r'<[^>]+>', '', m.group(1)).strip()
    out["title"] = title.strip()

    # 3. Canonical URL
    canonical_url = ""
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', html, re.I)
    if m: canonical_url = m.group(1).replace("-departamentos/", "-apartamentos/")
    if not canonical_url:
        m = re.search(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if m: canonical_url = m.group(1).replace("-departamentos/", "-apartamentos/")
    out["canonical_url"] = canonical_url.strip()

    # Body Text without JS/CSS
    clean_html = re.sub(r'<script.*?>.*?</script>', ' ', html, flags=re.I | re.DOTALL)
    clean_html = re.sub(r'<style.*?>.*?</style>', ' ', clean_html, flags=re.I | re.DOTALL)
    
    parts = re.split(r'<[^>]+>', clean_html)
    lines = [p.strip() for p in parts if p.strip()]
    
    # 4. Description
    description = ""
    desc_start = False
    desc_lines = []
    stop_words = {
        "leer más", "leer m\u00e1s", "enviar mensaje al vendedor", "invalid", "me interesa el anuncio",
        "completa tus datos", "contactar", "llamar", "whatsapp", "beneficios", "inicio inmuebles",
        "más anuncios de este vendedor", "m\u00e1s anuncios de este vendedor", "casas similares", 
        "departamentos similares", "centro de ayuda", "legal"
    }
    
    for text in lines:
        low_text = text.lower()
        if low_text in ("descripción", "descripci\u00f3n"):
            desc_start = True
            continue
            
        if desc_start:
            if low_text in stop_words or low_text.startswith("inicio inmuebles"):
                break
            desc_lines.append(text)
            
    if desc_lines:
        description = " ".join(desc_lines)
    else:
        m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if m:
            description = m.group(1)
    out["description"] = description.strip()

    # 5. Seller Name and Text
    seller_name = ""
    seller_text = ""

    # Priority 1: JSON-LD seller block
    seller_ld = json_ld.get("seller", {})
    if not (isinstance(seller_ld, dict) and seller_ld.get("name")):
        offers = json_ld.get("offers", {})
        if isinstance(offers, dict):
            seller_ld = offers.get("seller", {})
    if isinstance(seller_ld, dict) and seller_ld.get("name"):
        sn = seller_ld["name"].strip()
        _ld_template_words = {"agente", "propietario", "dueño", "dueña", "vendedor", "seller", "owner"}
        if sn.lower().strip() not in _ld_template_words and _is_valid_seller_name(sn):
            seller_name = sn

    # Priority 2: scan text lines AFTER "Información del vendedor"
    if not seller_name:
        _info_triggers = {"información del vendedor", "informacion del vendedor"}
        _stop_after = {
            "contactar", "llamar", "whatsapp", "enviar mensaje al vendedor",
            "me interesa el anuncio", "completa tus datos", "ver teléfono", "ver telefono"
        }
        for i, text in enumerate(lines):
            if text.lower() in _info_triggers:
                for j in range(i + 1, min(i + 6, len(lines))):
                    candidate = lines[j].strip()
                    low_c = candidate.lower()
                    if low_c in _stop_after:
                        break
                    if low_c.startswith("se uni"):
                        break
                    if _is_valid_seller_name(candidate):
                        seller_name = candidate
                        if j + 1 < len(lines):
                            nxt = lines[j + 1].strip()
                            seller_text = f"{candidate} {nxt}" if _is_valid_seller_name(nxt) else candidate
                        else:
                            seller_text = candidate
                        break
                break

    # Priority 3: scan text lines BEFORE "Enviar mensaje al vendedor"
    if not seller_name:
        _send_triggers = {"enviar mensaje al vendedor"}
        for i, text in enumerate(lines):
            if text.lower() in _send_triggers:
                candidates = []
                for j in range(1, 6):
                    if i - j >= 0:
                        candidates.insert(0, lines[i - j])
                clean_cands = [c for c in candidates if _is_valid_seller_name(c)]
                if clean_cands:
                    seller_name = clean_cands[0]  # first = furthest back = company/person name
                    seller_text = " ".join(clean_cands)
                break

    # Priority 4: /user/profile/ link in raw HTML
    if not seller_name:
        m = re.search(r'<a[^>]+href=["\'][^"\']+/user/profile/[^>]*>([^<]+)</a>', html, re.I)
        if m:
            candidate = m.group(1).strip()
            if _is_valid_seller_name(candidate):
                seller_name = candidate
                seller_text = candidate

    out["seller_name"] = seller_name.strip()
    out["seller_text"] = seller_text.strip()

    # 5b. Seller avatar alt — extract brand name from publisher's avatar/logo image
    seller_avatar_alt = ""
    _avatar_generic = {"avatar", "user-avatar", "usuario", "foto", "imagen", "user", "logo", "sin imagen"}
    _avatar_src_pat = r'(?:t_user_photo|t_user_logo|users/photo|users/logo|initial-avatar)'
    # Priority 1: src contains t_user_photo / t_user_logo / users/photo
    m = re.search(r'<img[^>]+src=["\'][^"\']*' + _avatar_src_pat + r'[^"\']*["\'][^>]*alt=["\']([^"\']+)["\']', html, re.I)
    if m:
        alt = m.group(1).strip()
        if alt.lower() not in _avatar_generic and len(alt) >= 3:
            seller_avatar_alt = alt
    if not seller_avatar_alt:
        # Priority 2: onerror points to user-avatar.png (Yapo publisher image pattern)
        m = re.search(r'<img[^>]+src=["\'][^"\']*["\'][^>]*alt=["\']([^"\']{3,})["\'][^>]*onerror=["\'][^"\']*user-avatar\.png["\']', html, re.I)
        if m:
            alt = m.group(1).strip()
            if alt.lower() not in _avatar_generic:
                seller_avatar_alt = alt
    if not seller_avatar_alt:
        # Fallback: any img with meaningful alt near contact/seller
        m = re.search(r'<img[^>]+alt=["\']([^"\']{3,})["\'][^>]*src=["\'][^"\']*(?:' + _avatar_src_pat + r'|/user/|/profile/|user-avatar)["\']', html, re.I)
        if m:
            alt = m.group(1).strip()
            if alt.lower() not in _avatar_generic:
                seller_avatar_alt = alt
    out["seller_avatar_alt"] = seller_avatar_alt

    # 6. Price
    price = ""
    body_text_full = " ".join(lines)
    match = re.search(r'Precio\s+(UF[\d.,]+|\$[\d.,]+)', body_text_full, re.I)
    if match:
        price = match.group(1)
    if not price and json_ld.get("offers", {}).get("price"):
        currency = json_ld.get("offers", {}).get("priceCurrency", "$")
        price = f"{currency}{json_ld['offers']['price']}"
    if not price:
        match = re.search(r'(UF[\d.,]+|\$[\d.,]+)', body_text_full, re.I)
        if match:
            price = match.group(1)
    out["price"] = price.strip()

    # Images: scan full HTML for encuentra24 CDN photo URLs
    # Yapo uses photos.encuentra24.com (Cloudinary) for property photos.
    # Transforms like t_or_fh_l, t_or_fh_s, t_or_fh_m are listing photos.
    # t_cnseal is badge/seal — reject those.
    _BADGE_FRAGS = ("cnseal", "octagon", "badge", "verified", "e24static",
                    "user-avatar", "common-library", "header-footer", "buyers/assets")

    def _is_property_cdn(url: str) -> bool:
        ul = url.lower()
        if any(b in ul for b in _BADGE_FRAGS):
            return False
        if re.search(r"/(?:t_or_|t_thumb|f_auto/v\d+/(?:cl|pe|ar|bo|ec|uy))", ul):
            return True
        if re.search(r"\.(?:jpg|jpeg|png|webp)(?:\?|$)", ul):
            return True
        return False

    image_urls: list[str] = []
    for raw_u in re.findall(
        r'https?://(?:photos\.encuentra24\.com|media\.yapo\.cl|img\.yapo\.cl)[^\s"\'\\><]+',
        html, re.I,
    ):
        raw_u = raw_u.rstrip("\\'\"\)")
        if _is_property_cdn(raw_u) and raw_u not in image_urls:
            image_urls.append(raw_u)

    _vis_src = "html_raw_scan" if image_urls else "none_in_static_html"
    out["image_urls"] = image_urls
    out["image_urls_detected_count"] = len(image_urls)
    out["visual_image_source"] = _vis_src
    attrs: dict[str, str] = {}
    
    # Clean HTML for attribute extraction (remove scripts and styles)
    clean_html = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', html, flags=re.I | re.DOTALL)
    
    # 1. Extract based on common Yapo structure: label text followed by <p> value
    blocks = re.findall(r'<div[^>]*class=["\'][^"\']*label[^"\']*["\'][^>]*>\s*([^<]+)\s*<p[^>]*>\s*([^<]+)\s*</p>', clean_html, re.I)
    for k, v in blocks:
        k_clean = re.sub(r"\s+", " ", k).strip().lower()
        v_clean = re.sub(r"\s+", " ", v).strip()
        if len(k_clean) < 30 and len(v_clean) < 50:
            if k_clean and v_clean and k_clean not in attrs:
                attrs[k_clean] = v_clean
                
    # 1.5 Extract based on <dt> and <dd> (Definition Lists)
    dl_blocks = re.findall(r'<dt[^>]*>\s*([^<]+)\s*</dt>\s*<dd[^>]*>\s*([^<]+)\s*</dd>', clean_html, re.I)
    for k, v in dl_blocks:
        k_clean = re.sub(r"\s+", " ", k).strip().lower()
        v_clean = re.sub(r"\s+", " ", v).strip()
        if len(k_clean) < 30 and len(v_clean) < 50:
            if k_clean and v_clean and k_clean not in attrs:
                attrs[k_clean] = v_clean
            
    # 2. Extract tr/td or li if they have a colon
    for match in re.finditer(r'<li[^>]*>(.*?)</li>|<tr[^>]*>(.*?)</tr>', clean_html, re.I | re.DOTALL):
        row = match.group(1) or match.group(2)
        text = re.sub(r'<[^>]+>', ' ', row)
        text = re.sub(r'\s+', ' ', text).strip()
        if ':' in text and len(text) < 100:
            k, v = text.split(':', 1)
            k_clean = k.strip().lower()
            v_clean = v.strip()
            if len(k_clean) < 30 and len(v_clean) < 50:
                if k_clean and v_clean and k_clean not in attrs:
                    attrs[k_clean] = v_clean

    out["attributes"] = attrs
    out["body_text"] = body_text_full

    out["publisher_identity_candidates"] = _extract_publisher_identity_candidates_regex(
        html, out.get("seller_name", ""), out.get("seller_text", ""),
        out.get("seller_avatar_alt", ""), json_ld,
    )

    return out


def _enrich_property_fields(parsed: dict[str, Any], url: str, uf_valor_clp: float, uf_fecha: str) -> dict[str, Any]:
    # URL extraction
    parsed["listing_id"] = ""
    parsed["operacion"] = ""
    parsed["tipo_propiedad"] = ""
    
    if url:
        m = re.search(r'/(\d+)$', url)
        if m:
            parsed["listing_id"] = m.group(1)
        
        low_url = url.lower()
        if "venta" in low_url:
            parsed["operacion"] = "venta"
        elif "alquiler" in low_url or "arriendo" in low_url:
            if "temporada" in low_url:
                parsed["operacion"] = "arriendo_temporada"
            else:
                parsed["operacion"] = "arriendo"
                
        if "casa" in low_url:
            parsed["tipo_propiedad"] = "casa"
        elif "apartamento" in low_url or "departamento" in low_url:
            parsed["tipo_propiedad"] = "departamento"
        elif "lote" in low_url or "terreno" in low_url or "sitio" in low_url:
            parsed["tipo_propiedad"] = "sitio"
        elif "parcela" in low_url:
            parsed["tipo_propiedad"] = "parcela"
        elif "oficina" in low_url:
            parsed["tipo_propiedad"] = "oficina"
        elif "local" in low_url or "bodega" in low_url:
            parsed["tipo_propiedad"] = "local/bodega"
        elif "estacionamiento" in low_url:
            parsed["tipo_propiedad"] = "estacionamiento"

    # Attributes extraction
    attrs = parsed.get("attributes", {})
    
    def get_attr(*keys) -> str:
        for k in keys:
            for ak, av in attrs.items():
                if k in ak:
                    return str(av)
        return ""

    # Normalization
    def parse_int(val: str) -> int | None:
        m = re.search(r'\d+', str(val).replace(".", ""))
        return int(m.group()) if m else None

    def parse_float(val: str) -> float | None:
        v = str(val).replace(".", "").replace(",", ".")
        m = re.search(r'[\d.]+', v)
        if m:
            try:
                return float(m.group())
            except ValueError:
                pass
        return None

    parsed["comuna"] = get_attr("comuna", "ubicación", "ubicacion", "localización", "localizacion", "ciudad")
    parsed["region"] = get_attr("región", "region")
    
    parsed["fecha_publicacion_raw"] = get_attr("publicado", "fecha")
    parsed["fecha_publicacion"] = ""
    if parsed["fecha_publicacion_raw"]:
        m = re.search(r'(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})', parsed["fecha_publicacion_raw"])
        if m:
            parsed["fecha_publicacion"] = f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"

    parsed["dormitorios"] = parse_int(get_attr("dormitorio", "habitación", "habitacion"))
    parsed["banos"] = parse_int(get_attr("baño", "bano"))
    parsed["estacionamientos"] = parse_int(get_attr("estacionamiento"))

    # Fallback from title and description
    title_desc = f"{parsed.get('title', '')} {parsed.get('description', '')}".lower()
    
    if parsed["dormitorios"] is None or parsed["dormitorios"] == 0:
        m = re.search(r'(\d+)\s*(?:d\b|dormitorio|habitación|habitacion)', title_desc)
        if m: parsed["dormitorios"] = int(m.group(1))

    if parsed["banos"] is None or parsed["banos"] == 0:
        m = re.search(r'(\d+)\s*(?:b\b|baño|bano)', title_desc)
        if m: parsed["banos"] = int(m.group(1))

    if parsed["estacionamientos"] is None or parsed["estacionamientos"] == 0:
        m = re.search(r'(\d+)\s*estacionamiento', title_desc)
        if m:
            parsed["estacionamientos"] = int(m.group(1))
        elif 'incluye estacionamiento' in title_desc or 'con estacionamiento' in title_desc:
            parsed["estacionamientos"] = 1
            
    # Zero to None check
    if parsed["dormitorios"] == 0: parsed["dormitorios"] = None
    if parsed["banos"] == 0: parsed["banos"] = None
    if parsed["estacionamientos"] == 0: parsed["estacionamientos"] = None

    parsed["m2_construidos"] = parse_float(get_attr("superficie construida", "m2 construido", "m² construido", "útil", "util"))
    parsed["m2_totales"] = parse_float(get_attr("superficie total", "m2 total", "m² total", "terreno"))
    parsed["gastos_comunes"] = parse_int(get_attr("gasto común", "gastos comunes"))
    parsed["direccion_exacta"] = get_attr("dirección", "direccion exact")
    
    # Image Deduplication
    image_urls = parsed.get("image_urls", [])
    parsed["image_urls_detected_count"] = len(image_urls)
    unique_images = []
    seen_ids = set()
    for u in image_urls:
        m = re.search(r'/([^/]+)(?:_[a-f0-9\-]+)?$', u)
        if m:
            img_id = m.group(1)
            if img_id not in seen_ids:
                seen_ids.add(img_id)
                # prefer large 't_or_fh_l' without duplicating f_auto
                u_large = re.sub(r'/(?:t_or_fh_[a-z]+(?:/f_auto)?|f_auto)/', '/t_or_fh_l/f_auto/', u)
                unique_images.append(u_large)
        else:
            if u not in unique_images:
                unique_images.append(u)
    
    parsed["image_urls"] = unique_images[:20]
    parsed["image_urls_count"] = len(parsed["image_urls"])
    parsed["main_image_url"] = parsed["image_urls"][0] if parsed["image_urls"] else ""

    # Price Normalization
    price_raw = parsed.get("price", "")
    parsed["precio_raw"] = price_raw
    parsed["precio_moneda_original"] = "UNKNOWN"
    parsed["precio_original_num"] = None
    parsed["precio_uf"] = None
    parsed["precio_clp"] = None
    parsed["uf_valor_usado"] = uf_valor_clp
    parsed["uf_fecha"] = uf_fecha
    parsed["precio_validacion"] = ""
    parsed["precio_detectado_alternativo"] = ""
    parsed["precio_conversion_source"] = "ENV"

    if price_raw:
        low_price = price_raw.lower()
        if "uf" in low_price:
            parsed["precio_moneda_original"] = "UF"
        elif "$" in low_price or "clp" in low_price or "pesos" in low_price:
            parsed["precio_moneda_original"] = "CLP"

        num_val = parse_float(price_raw.replace("UF", "").replace("uf", "").replace("$", ""))
        parsed["precio_original_num"] = num_val

        if num_val is not None:
            if parsed["precio_moneda_original"] == "UF":
                parsed["precio_uf"] = num_val
                parsed["precio_clp"] = int(round(num_val * uf_valor_clp))
                
                # Validation: Plausibility
                if num_val > 100000:
                    parsed["precio_validacion"] = "sospechoso_uf_excesivo"
                    body = parsed.get("body_text", "")
                    alt_m = re.search(r'(\d{1,3}(?:\.\d{3})*|\d+)\s*UF', body, re.I)
                    if not alt_m:
                        alt_m = re.search(r'UF\s*(\d{1,3}(?:\.\d{3})*|\d+)', body, re.I)
                        
                    if alt_m:
                        alt_val = parse_float(alt_m.group(1))
                        if alt_val and alt_val < 100000:
                            parsed["precio_detectado_alternativo"] = alt_m.group(0)
                            parsed["precio_validacion"] = "conflicto_precio_raw_vs_descripcion"
                            parsed["precio_uf"] = alt_val
                            parsed["precio_clp"] = int(round(alt_val * uf_valor_clp))
                            parsed["precio_original_num"] = alt_val
                            
            elif parsed["precio_moneda_original"] == "CLP":
                parsed["precio_clp"] = int(num_val)
                parsed["precio_uf"] = round(num_val / uf_valor_clp, 2)

    # Images limits
    # NOTE: image_urls_count and main_image_url are already set by _enrich_property_fields dedupe logic
    # Only set as fallback if not already present
    image_urls = parsed.get("image_urls", [])
    if "image_urls_count" not in parsed:
        parsed["image_urls_count"] = len(image_urls)
    if "main_image_url" not in parsed:
        parsed["main_image_url"] = image_urls[0] if image_urls else ""

    return parsed


def _parse_html_fast(html: str, url: str | None = None) -> dict[str, Any]:
    """Parse a Yapo listing HTML file without any network access."""
    parsed = _extract_with_bs4(html) if BeautifulSoup is not None else _extract_without_bs4(html)

    parsed["source_url"] = url or parsed.get("canonical_url", "")
    parsed["html_validation"] = html_validator(html)
    parsed["fetched_at"] = _now_iso()
    parsed["description_available"] = bool(str(parsed.get("description", "")).strip())

    if not parsed.get("title"):
        body = str(parsed.get("body_text", ""))
        parsed["title"] = body[:140].strip()

    from config import get_config
    cfg = get_config()
    parsed = _enrich_property_fields(parsed, parsed["source_url"], cfg.uf_valor_clp, cfg.uf_fecha)

    return parsed


def _is_listing_detail_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
        
    low_netloc = parsed.netloc.lower()
    if low_netloc not in ("www.yapo.cl", "yapo.cl"):
        return False
        
    low_path = parsed.path.lower()
    if not low_path.startswith("/bienes-raices-"):
        return False
        
    for exclude in ("/user/", "/searchresult/", "/ayuda", "/help", "/favoritos", "/login", "/register", "/categorias"):
        if exclude in low_path:
            return False
            
    if any(ext in low_path for ext in ("/assets/", "/static/", "/img/", ".js", ".css")):
        return False
        
    path_no_slash = low_path.rstrip("/")
    if not re.search(r"/\d+$", path_no_slash):
        return False
        
    return True


def _extract_listing_links_from_tree(html: str, page_url: str) -> list[dict]:
    results = []
    seen = set()
    
    if BeautifulSoup is None:
        for href in re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html, flags=re.I):
            if not href:
                continue
            full = urljoin(page_url, href)
            full_clean = urlunparse(urlparse(full)._replace(query="", fragment=""))
            if _is_listing_detail_url(full_clean):
                if full_clean not in seen:
                    seen.add(full_clean)
                    results.append({
                        "url": full_clean,
                        "fid": _md5_url(full_clean),
                        "source_page": page_url,
                        "source_anchor_text": "",
                        "source_href": href,
                        "extraction_method": "anchor_href_listing_detail"
                    })
        return results

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue
            
        full = urljoin(page_url, href)
        full_clean = urlunparse(urlparse(full)._replace(query="", fragment=""))
        
        if _is_listing_detail_url(full_clean):
            if full_clean not in seen:
                seen.add(full_clean)
                results.append({
                    "url": full_clean,
                    "fid": _md5_url(full_clean),
                    "source_page": page_url,
                    "source_anchor_text": a.get_text(" ", strip=True),
                    "source_href": href,
                    "extraction_method": "anchor_href_listing_detail"
                })
                
    return results


def _extract_next_page_url(html: str, page_url: str) -> str | None:
    if BeautifulSoup is None:
        # Without bs4: check both <link rel="next"> and <a rel="next">
        match = re.search(r'<link[^>]+rel=["\']next["\'][^>]*href=["\']([^"\']+)["\']', html, flags=re.I)
        if match:
            return _normalize_url(urljoin(page_url, match.group(1)))
        match = re.search(r'<a[^>]+rel=["\']next["\'][^>]*href=["\']([^"\']+)["\']', html, flags=re.I)
        if match:
            return _normalize_url(urljoin(page_url, match.group(1)))
        return None
        
    soup = BeautifulSoup(html, "html.parser")
    # Priority 1: <link rel="next"> (standard HTML pagination)
    link_next = soup.find("link", attrs={"rel": "next"})
    if link_next and link_next.get("href"):
        href = link_next["href"]
        LOGGER.debug("NEXT-PAGE from <link rel=next> href=%s", href)
        return urljoin(page_url, href)
    # Priority 2: <a rel="next">
    a_next = soup.find("a", attrs={"rel": "next"})
    if a_next and a_next.get("href"):
        href = a_next["href"]
        LOGGER.debug("NEXT-PAGE from <a rel=next> href=%s", href)
        return urljoin(page_url, href)
    # Priority 3: <a> with "siguiente" / "next" text
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True).lower()
        if text in ("siguiente", "next", "siguiente »", "next »", ">", "próxima", "proxima"):
            href = a["href"]
            LOGGER.debug("NEXT-PAGE from <a> text=%r href=%s", text, href)
            return urljoin(page_url, href)
            
    LOGGER.debug("NEXT-PAGE not found in HTML")
    return None


async def _fetch_playwright(url: str, timeout_ms: int = 30_000) -> str:
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency.
        raise RuntimeError("playwright not available") from exc

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(1200)
            return await page.content()
        finally:
            await browser.close()


def _fetch_http(url: str, cfg: ScraperConfig) -> str:
    headers = _build_headers(cfg)
    if curl_requests is not None:
        resp = curl_requests.get(url, headers=headers, timeout=cfg.request_timeout)
        resp.raise_for_status()
        return resp.text

    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=cfg.request_timeout) as response:
        data = response.read()
    return data.decode("utf-8", errors="replace")


def _fetch_html(url: str, cfg: ScraperConfig, *, allow_playwright_fallback: bool = True) -> tuple[str, str]:
    """Return raw HTML and the source used to fetch it."""
    try:
        html = _fetch_http(url, cfg)
        validation = html_validator(html)
        if validation["status"] == "BLOCKED" and allow_playwright_fallback:
            html = asyncio.run(_fetch_playwright(url, cfg.request_timeout * 1000))
            return html, "playwright"
        return html, "http"
    except Exception as first_exc:
        if not allow_playwright_fallback:
            raise
        try:
            html = asyncio.run(_fetch_playwright(url, cfg.request_timeout * 1000))
            return html, "playwright"
        except Exception as second_exc:
            raise RuntimeError(f"Failed to fetch {url}") from second_exc


def extract_fast_path(
    url: str,
    cfg: ScraperConfig | None = None,
    *,
    allow_playwright_fallback: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Download and persist HTML for a URL, unless a valid dump already exists."""
    cfg = cfg or ScraperConfig()
    url = _normalize_url(url)
    html_dir = _ensure_dir(cfg.html_dump_dir)
    html_path = _html_path_for_url(url, html_dir)

    if html_path.exists() and not force_refresh:
        existing_html = html_path.read_text(encoding="utf-8", errors="replace")
        validation = html_validator(existing_html)
        if validation["status"] != "INVALID":
            return {
                "url": url,
                "html_path": str(html_path),
                "html_validation_status": validation["status"],
                "downloaded": False,
                "fetch_source": "cache",
            }

    html, fetch_source = _fetch_html(url, cfg, allow_playwright_fallback=allow_playwright_fallback)
    html_path.write_text(html, encoding="utf-8", errors="replace")
    validation = html_validator(html)

    # URL fallback: if listing seems invalid or path uses departamentos/apartamentos, try alternate
    # Yapo canonicalizes apartamentos (not departamentos), so departamentos often gives 404
    _alt_tried = False
    if "-departamentos/" in url or "-apartamentos/" in url:
        needs_fallback = validation["status"] in ("INVALID", "LISTING_REMOVED", "BLOCKED", "PAGE_NOT_FOUND")
        if not needs_fallback and "-departamentos/" in url:
            # departamentos is non-canonical; always try apartamentos as canonical
            needs_fallback = True
        if needs_fallback:
            alt_url = None
            if "-departamentos/" in url:
                alt_url = url.replace("-departamentos/", "-apartamentos/")
            elif "-apartamentos/" in url:
                alt_url = url.replace("-apartamentos/", "-departamentos/")
            if alt_url:
                LOGGER.info("[URL_FALLBACK] trying alternate path from=%s to=%s",
                            url.split("/")[-2] if "/" in url else url, alt_url[:80])
                try:
                    alt_html, alt_src = _fetch_html(alt_url, cfg, allow_playwright_fallback=False)
                    alt_validation = html_validator(alt_html)
                    if alt_validation["status"] == "OK":
                        html = alt_html
                        fetch_source = f"{fetch_source}_fallback"
                        html_path.write_text(html, encoding="utf-8", errors="replace")
                        validation = alt_validation
                        url = alt_url
                        LOGGER.info("[URL_FALLBACK] listing_id=%s alternate OK src=%s", url.split("/")[-1], alt_src)
                except Exception as _fallback_err:
                    LOGGER.debug("[URL_FALLBACK] alternate also failed: %s", _fallback_err)

    return {
        "url": url,
        "html_path": str(html_path),
        "html_validation_status": validation["status"],
        "downloaded": True,
        "fetch_source": fetch_source,
    }


async def _async_fetch_html(url: str, cfg: ScraperConfig, *, allow_playwright_fallback: bool = True) -> tuple[str, str]:
    try:
        html = _fetch_http(url, cfg)
        validation = html_validator(html)
        if validation["status"] == "BLOCKED" and allow_playwright_fallback:
            html = await _fetch_playwright(url, cfg.request_timeout * 1000)
            return html, "playwright"
        return html, "http"
    except Exception as first_exc:
        if not allow_playwright_fallback:
            raise
        try:
            html = await _fetch_playwright(url, cfg.request_timeout * 1000)
            return html, "playwright"
        except Exception as second_exc:
            raise RuntimeError(f"Failed to fetch {url}") from second_exc


async def _discover_from_page(page_url: str, cfg: ScraperConfig) -> list[dict]:
    html, _ = await _async_fetch_html(page_url, cfg, allow_playwright_fallback=True)
    return _extract_listing_links_from_tree(html, page_url)


async def discover_new_properties(
    cfg: ScraperConfig | None = None,
    *,
    base_url: str | None = None,
    max_pages: int | None = None,
    max_urls_per_session: int | None = None,
    target_new: int | None = None,
    mongo_collection: Any | None = None,
) -> list[str]:
    """Discover new property URLs from paginated Yapo listing pages.
    
    If mongo_collection is provided, checks each URL against existing listing_ids
    and logs new vs existing counts per page. Stops when target_new new URLs found.
    """
    cfg = cfg or ScraperConfig()
    
    if not base_url:
        seeds = DISCOVERY_SEED_URLS
    else:
        seeds = [base_url]
        
    page_limit = max_pages if max_pages is not None else cfg.max_pages
    url_limit = max_urls_per_session if max_urls_per_session is not None else cfg.max_urls_per_session
    target_new_count = target_new if target_new is not None else cfg.target_new_urls
    
    discovered: list[str] = []
    seen: set[str] = set()
    total_new = 0
    pages_traversed = 0

    for start_url in seeds:
        current_url = _normalize_url(start_url)
        for page_number in range(1, page_limit + 1):
            try:
                html, fetch_src = await _async_fetch_html(current_url, cfg, allow_playwright_fallback=True)
                page_links_dicts = _extract_listing_links_from_tree(html, current_url)
                found = len(page_links_dicts) if page_links_dicts else 0
                pages_traversed = page_number
                
                if not page_links_dicts:
                    debug_path = cfg.html_dump_dir / f"discovery_debug_page_{page_number}_{_md5_url(current_url)}.html"
                    _ensure_dir(debug_path.parent)
                    debug_path.write_text(html, encoding="utf-8", errors="replace")
                    LOGGER.warning("[DISCOVERY] page=%d found=0 stopping_reason=no_results url=%s", page_number, current_url[:80])
                    break
                
                page_new = 0
                page_existing = 0
                for item in page_links_dicts:
                    link = item["url"]
                    if link not in seen:
                        seen.add(link)
                        # Check if already in Mongo
                        exists = False
                        if mongo_collection is not None:
                            m = re.search(r'/(\d+)$', link)
                            if m:
                                lid = m.group(1)
                                exists = mongo_collection.count_documents({"listing_id": lid}) > 0
                        if exists:
                            page_existing += 1
                        else:
                            page_new += 1
                            discovered.append(link)
                            total_new += 1
                            if target_new_count and total_new >= target_new_count:
                                LOGGER.info("[DISCOVERY] page=%d found=%d existing=%d new=%d total_new=%d url=%s",
                                            page_number, found, page_existing, page_new, total_new, current_url[:60])
                                LOGGER.info("[DISCOVERY] stopping_reason=target_new_reached target_new=%d total_new=%d total_seen=%d",
                                            target_new_count, total_new, len(seen))
                                return discovered
                
                LOGGER.info("[DISCOVERY] page=%d found=%d existing=%d new=%d total_new=%d url=%s",
                            page_number, found, page_existing, page_new, total_new, current_url[:60])
                
                next_page = _extract_next_page_url(html, current_url)
                if not next_page:
                    LOGGER.info("[DISCOVERY] page=%d stopping_reason=no_next_page total_new=%d total_seen=%d",
                                page_number, total_new, len(seen))
                    return discovered
                current_url = next_page
            except Exception as exc:
                LOGGER.debug("Discovery page failed %s: %s", current_url, exc)
                break

    LOGGER.info("[DISCOVERY] stopping_reason=all_pages_consumed pages=%d total_new=%d total_seen=%d",
                pages_traversed, total_new, len(seen))
    return discovered


def _check_strong_broker_rules(parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Check all publisher_identity_candidates against STRONG_BROKER_PATTERNS.

    Returns a classification dict if a strong pattern matches, None otherwise.
    """
    candidates = parsed.get("publisher_identity_candidates", [])
    if not candidates:
        return None

    strong_patterns = tuple(p.lower() for p in STRONG_BROKER_PATTERNS)
    for candidate in candidates:
        value = str(candidate.get("value", "")).strip().lower()
        source = str(candidate.get("source", ""))
        if not value or len(value) < 3:
            continue
        for pattern in strong_patterns:
            if pattern in value:
                return {
                    "state": "CORREDOR_SEGURO",
                    "confidence": 0.99,
                    "score": 0.99,
                    "signals": {"corredor": True},
                    "evidence": [f"{source} contains commercial term '{pattern}'"],
                    "reason": f"Strong broker pattern '{pattern}' found in {source}: {candidate['value']}",
                    "decision_source": source,
                    "decision_pattern": f"strong_broker_term:{pattern}",
                    "ai_used": False,
                    "final_state": "CORREDOR_SEGURO",
                    "rule_state": "CORREDOR_SEGURO",
                    "version": "v5-rule-based",
                }
    return None


def _looks_like_commercial_identity(text: str) -> tuple[bool, str]:
    """Detect if text looks like a commercial/business identity vs a person name.
    
    Returns (is_commercial, reason) tuple.
    """
    if not text or len(text) < 3:
        return False, ""
    t = text.strip()
    low_t = t.lower()
    
    # Strong business suffixes — always commercial
    _biz_suffixes = ("spa", "ltda", "s.a.", "eirl", "srl", "sa", "limitada", "s.a.s", "inc", "corp")
    if any(low_t.endswith(s) or low_t.endswith(s + ".") for s in _biz_suffixes):
        return True, f"contiene sufijo comercial: {t.split()[-1]}"
    
    # Industry-specific commercial keywords (not just property, but business entity terms)
    _commercial_kws = (
        "propiedades", "inmobiliaria", "corredores", "corredora", "corretaje",
        "properties", "property", "real estate", "bienes rai", "gestion inmobiliaria",
        "ventas", "sales",
        "broker", "remax", "re max", "re/max", "century 21",
        "asesores", "asesoria", "asesorias", "asesor inmobiliario",
        "administracion", "administradora", "administración", "condominios",
        "consultores", "consultora", "corporacion", "corporación",
        "asociados", "asociada", "asociado",
        "gestoria", "gestoría", "gestión inmobiliaria",
        "inmuebles", "inmobiliario",
        # Known real-estate brand prefixes
        "procasa", "nexxos", "kutt property", "engel", "voelkers",
        "prestige", "focolare", "fuster", "schumacher", "wall",
        "premium", "dataprop", "puntoinmobiliario", "corretajes",
        "invierte", "kinast", "grial", "adelof", "goldenrent",
        "prestige", "v&b", "p&d",  "a&p", "m&f", "a&v",
        "group", "grupo",         "coldwell", "banker", "realty", "exp chile", "exp realty",
    )
    for kw in _commercial_kws:
        if kw in low_t:
            return True, f"contiene termino comercial: {kw}"
    
    # Organizational patterns: "X & Y", "X y Y", "X - Y" with capitalized words
    if re.search(r'\b([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+\s*[&]\s*[A-ZÁÉÍÓÚÑ])', t):
        return True, "contiene estructura comercial con &"
    if re.search(r'\b([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+\s+y\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)', t):
        return True, "contiene estructura comercial con 'y'"
    
    # Mixed case with capital letters inside (like "HyC", "M&F")
    if re.search(r'^[A-ZÁÉÍÓÚÑ][a-záéíóúñ]*[A-ZÁÉÍÓÚÑ]', t):
        return True, "contiene mayusculas internas (siglas comerciales)"
    
    # Person name check: 2-4 title-case tokens, all alphabetic → NOT commercial
    tokens = t.split()
    if 2 <= len(tokens) <= 4:
        all_alpha = all(tok.isalpha() for tok in tokens)
        all_title = all(tok.istitle() or tok.isupper() for tok in tokens)
        if all_alpha and all_title:
            return False, "parece nombre de persona"
    
    # If multiple capitalized words with no person structure → likely commercial
    capped = [w for w in tokens if w[0].isupper()] if tokens else []
    if len(capped) >= 2 and len(tokens) >= 2:
        return True, "multiples palabras capitalizadas sin estructura de nombre"
    
    return False, ""


def _score_state(signals: dict[str, bool]) -> tuple[str, float]:
    corredor = sum(1 for key in ("corredor", "empresa", "inmobiliaria", "broker") if signals.get(key))
    owner = sum(1 for key in ("dueno", "particular", "trato_directo") if signals.get(key))

    if corredor == 0 and owner == 0:
        return "INCIERTO", 0.35
    if corredor >= owner + 1:
        return "CORREDOR_SEGURO", min(0.95, 0.55 + 0.12 * corredor)
    if owner >= corredor + 1:
        return "DUEÑO_SEGURO", min(0.95, 0.55 + 0.12 * owner)
    return "INCIERTO", 0.5


def _gather_signals(parsed: dict[str, Any]) -> dict[str, bool]:
    seller_name = str(parsed.get("seller_name", "")).strip()
    seller_text = str(parsed.get("seller_text", "")).strip()
    description = str(parsed.get("description", "")).strip()
    title = str(parsed.get("title", "")).strip()
    seller_avatar_alt = str(parsed.get("seller_avatar_alt", "")).strip()
    
    seller_combined = f"{seller_name} {seller_text}".strip()
    description_low = description.lower()
    
    # ── Generic commercial identity detection on all fields ──
    _is_commercial, _commercial_reason = False, ""
    _commercial_source = ""
    
    # Priority 1: seller_avatar_alt (publisher logo)
    # Only flag as commercial if the alt text actually looks like a business identity,
    # not just any person name (e.g., "Gabriel Tadres" as avatar is not commercial).
    _avatar_generic = {"avatar", "user-avatar", "usuario", "foto", "imagen", "user", "logo", "sin imagen"}
    if seller_avatar_alt and len(seller_avatar_alt) >= 3 and seller_avatar_alt.lower() not in _avatar_generic:
        is_c, _ = _looks_like_commercial_identity(seller_avatar_alt)
        if is_c:
            _is_commercial = True
            _commercial_reason = f"avatar/logo del publicador contiene identidad comercial: {seller_avatar_alt}"
            _commercial_source = "avatar"
    
    # Priority 2: seller_name (if not already detected)
    if not _is_commercial and seller_name:
        is_c, reason = _looks_like_commercial_identity(seller_name)
        if is_c:
            _is_commercial = True
            _commercial_reason = f"nombre del vendedor contiene identidad comercial: {seller_name}"
            _commercial_source = "seller"
    
    # Priority 3: title — only check pipe-delimited commercial brand (specific Yapo format)
    # Do NOT run generic _looks_like_commercial_identity on title because titles naturally
    # contain many capitalized proper nouns (locations, amenity names) causing false positives.
    if not _is_commercial and title:
        _title_pipe_pat = r'[|]\s*[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{2,}(?:PROPIEDADES|INMOBILIARIA|CORREDORES|CORRETAJE|BIENES\s*RA[IÍ]CES|GESTI[ÓO]N\s*INMOBILIARIA)\s*[|]'
        m = re.search(_title_pipe_pat, title)
        if m:
            _is_commercial = True
            _commercial_reason = f"title contiene identidad comercial inmobiliaria: {m.group(0).strip()}"
            _commercial_source = "title"
    
    # ── Check description for commercial broker language ──
    _desc_broker_kws = (
        "corredora", "corredor", "inmobiliaria", "propiedades", "corretaje",
        "asesor inmobiliario", "gestion inmobiliaria", "gestión inmobiliaria",
        "comision", "comisión", "honorarios", "coordinar visita",
        "agenda tu visita", "agenda una visita", "nuestros ejecutivos",
        "nuestro equipo", "cartera de propiedades", "oficina comercial",
        "broker", "remax", "re/max", "century 21",
    )
    _has_desc_broker = _text_contains_any(description_low, _desc_broker_kws)
    
    is_person = False
    if seller_name:
        tokens = [t for t in seller_name.split() if t.isalpha()]
        if 2 <= len(tokens) <= 4 and all(t.istitle() or t.isupper() for t in tokens):
            is_person = True
            
    signals = {
        "corredor": False,
        "empresa": False,
        "inmobiliaria": False,
        "broker": False,
        "dueno": False,
        "particular": False,
        "trato_directo": False,
    }
    
    if _is_commercial:
        signals["corredor"] = True
    
    allow_weak_signals = _is_commercial or _has_desc_broker or not is_person
    
    if allow_weak_signals:
        if _text_contains_any(seller_combined + " " + description_low, ("corredor", "corretaje", "broker")):
            signals["corredor"] = True
        if _text_contains_any(seller_combined + " " + description_low, ("empresa", "sociedad", "spa", "s.a.", "srl")):
            signals["empresa"] = True
        if _text_contains_any(seller_combined + " " + description_low, ("inmobiliaria", "inmueble", "propiedades")):
            signals["inmobiliaria"] = True
    
    if _has_desc_broker:
        signals["corredor"] = True
            
    if _text_contains_any(seller_combined + " " + description_low, ("due\u00f1o", "dueña", "dueño", "due\u00f1a")):
        signals["dueno"] = True
    if _text_contains_any(seller_combined + " " + description_low, ("particular", "sin corredor", "trato directo")):
        signals["particular"] = True
    if _text_contains_any(seller_combined + " " + description_low, ("trato directo", "contacto directo", "directo con due\u00f1o")):
        signals["trato_directo"] = True
        
    # Store commercial identity evidence for the classifier
    if _is_commercial:
        parsed["_commercial_identity_evidence"] = _commercial_reason
        parsed["_commercial_identity_source"] = _commercial_source
        
    return signals


def _classify_with_deepseek_custom(parsed: dict[str, Any], config: Any) -> dict[str, Any]:
    import json
    if not config.deepseek_enabled or not config.deepseek_api_key:
        return {
            "owner_score": 0,
            "broker_score": 0,
            "final_state": "INCIERTO",
            "confidence": 0.5,
            "evidence": [],
            "reason": "DeepSeek disabled or API key missing"
        }
        
    url = f"{config.deepseek_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    
    comuna = parsed.get("attributes", {}).get("comuna", parsed.get("attributes", {}).get("localizacion", "N/A"))
    desc = parsed.get("description", "")
    if len(desc) > 2000:
        desc = desc[:2000] + "..."

    publisher_candidates = parsed.get("publisher_identity_candidates", [])

    payload = {
        "title": parsed.get("title", ""),
        "description": desc,
        "seller_name": parsed.get("seller_name", ""),
        "seller_text": parsed.get("seller_text", ""),
        "seller_avatar_alt": parsed.get("seller_avatar_alt", ""),
        "price": parsed.get("price", ""),
        "comuna": comuna,
        "publisher_identity_candidates": publisher_candidates,
    }

    system_prompt = (
        "Eres un clasificador experto de anuncios de propiedades en Chile. "
        "Tu tarea es determinar si el vendedor es un DUEÑO directo o un CORREDOR (intermediario/empresa). "
        "Analiza especialmente el campo 'publisher_identity_candidates' que contiene todos los nombres "
        "candidatos del publicador extraídos del HTML (contact_name, seller_avatar_alt, logos, etc.). "
        "Da mucho peso a nombres que vienen desde contact_name, /user/profile/, logo/avatar alt o seller_avatar_alt. "
        "No necesitas que el nombre esté en una lista conocida. "
        "Si el nombre parece marca comercial, grupo, empresa, inmobiliaria, corredora, sociedad, consultora o broker, "
        "asigna broker_score alto (70+). "
        "Si parece persona natural simple (nombre+apellido) y no hay señales comerciales en descripción ni en otros campos, "
        "asigna owner_score alto o deja incierto según la descripción.\n\n"
        "Debes responder estrictamente en formato JSON válido con la siguiente estructura:\n"
        "{\n"
        "  \"owner_score\": 0-100,\n"
        "  \"broker_score\": 0-100,\n"
        "  \"final_state\": \"DUEÑO_SEGURO\" | \"DUEÑO_PROBABLE\" | \"CORREDOR_SEGURO\" | \"INCIERTO\",\n"
        "  \"confidence\": 0.0-1.0,\n"
        "  \"evidence\": [\"evidencia1\", \"evidencia2\"],\n"
        "  \"reason\": \"explicación detallada\"\n"
        "}\n\n"
        "Reglas estrictas de puntuación:\n"
        "- Si el seller_name parece persona natural y no hay evidencia fuerte de corredor, broker_score NO debe ser mayor a 49.\n"
        "- Para broker_score >= 60 debe existir evidencia concreta (ej: corredor, corredora, inmobiliaria, broker, comisión de corretaje, equipo comercial, agencia, SpA, Ltda, EIRL, o 'propiedades' en la marca).\n"
        "- Frases genéricas (inversión inmobiliaria, oportunidad, casa en venta, plusvalía, propiedades Santiago) NO bastan para broker_score >= 60.\n"
        "- Si el estado es INCIERTO, mantén ambos scores en zona media (20-59).\n"
        "- Para owner_score >= 70, debe haber evidencia concreta ('vendo mi casa', 'dueño directo', 'sin comisión', 'trato directo').\n"
    )
    
    body = {
        "model": config.deepseek_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
        ],
        "max_tokens": config.deepseek_max_tokens,
        "temperature": 0,
        "response_format": {"type": "json_object"}
    }
    
    if config.deepseek_thinking:
        body["thinking"] = True

    try:
        import requests
        import re
        resp = requests.post(url, headers=headers, json=body, timeout=config.deepseek_timeout_seconds)
        resp.raise_for_status()
        res_data = resp.json()
        content = res_data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        
        # Robust JSON parsing — tolerant of markdown, surrounding text, trailing commas
        import json
        import re
        
        def _extract_json(raw: str) -> dict | None:
            # Strip markdown code fences
            raw = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.I)
            raw = re.sub(r'\s*```\s*$', '', raw.strip())
            # Try direct parse
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
            # Find first { ... } block (greedy) and try to parse it
            brace_match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
            if brace_match:
                candidate = brace_match.group(0)
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass
                # Try with trailing commas removed
                try:
                    cleaned = re.sub(r',\s*}', '}', candidate)
                    return json.loads(cleaned)
                except json.JSONDecodeError:
                    pass
            return None
        
        parsed_res = _extract_json(content)
        if parsed_res is None:
            parsed_res = {
                "owner_score": 0,
                "broker_score": 0,
                "final_state": "INCIERTO",
                "confidence": 0.5,
                "evidence": [],
                "reason": "Invalid JSON from LLM: " + content[:100]
            }
        
        # Post-processing validations
        def _text_contains_any(t: str, words: tuple[str, ...]) -> bool:
            return any(re.search(r'\b' + re.escape(w) + r'\b', t, re.I) for w in words)
            
        combined_text = f"{payload['seller_name']} {payload['seller_text']} {payload['seller_avatar_alt']} {payload['description']}".lower()
        
        # Also check publisher_identity_candidates values for broker patterns
        candidate_values = " ".join(c.get("value", "") for c in publisher_candidates).lower()
        combined_text = f"{combined_text} {candidate_values}"
        
        is_person = False
        tokens = [t for t in payload['seller_name'].split() if t.isalpha()]
        if 2 <= len(tokens) <= 4 and all(t.istitle() or t.isupper() for t in tokens):
            is_person = True
            
        strong_broker_kws = ("corredor", "corredora", "inmobiliaria", "broker", "comisi\u00f3n de corretaje", "equipo comercial", "agencia", "spa", "ltda", "eirl", "s.a.")
        has_strong_broker = _text_contains_any(combined_text, strong_broker_kws)
        
        # Also check ALL publisher_identity_candidates against _looks_like_commercial_identity
        if not has_strong_broker:
            for c in publisher_candidates:
                val = c.get("value", "").strip()
                if val:
                    is_c, _ = _looks_like_commercial_identity(val)
                    if is_c:
                        has_strong_broker = True
                        break
        
        strong_owner_kws = ("dueño directo", "trato directo", "sin comisión", "vendo mi casa", "dueña directa", "dueño", "dueña", "particular")
        has_strong_owner = _text_contains_any(combined_text, strong_owner_kws)
        
        owner_score = parsed_res.get("owner_score", 0)
        broker_score = parsed_res.get("broker_score", 0)
        final_state = parsed_res.get("final_state", "INCIERTO")
        
        # If DeepSeek was confident (raw score >= 70), trust it regardless of is_person
        if broker_score >= 70:
            pass  # trust DeepSeek's judgment
        elif is_person and not has_strong_broker:
            if broker_score > 49:
                broker_score = 49
                
        if not has_strong_owner:
            if owner_score > 69:
                owner_score = 69
                
        if broker_score >= 70:
            final_state = "CORREDOR_SEGURO"
        elif owner_score >= 70 and broker_score < 50:
            final_state = "DUEÑO_PROBABLE" if owner_score < 85 else "DUEÑO_SEGURO"
        elif broker_score <= 49 and owner_score <= 69:
            final_state = "INCIERTO"
            
        parsed_res["owner_score"] = owner_score
        parsed_res["broker_score"] = broker_score
        parsed_res["final_state"] = final_state
        
        return parsed_res
    except Exception as e:
        LOGGER.error(f"Error calling DeepSeek custom API: {e}")
        return {
            "owner_score": 0,
            "broker_score": 0,
            "final_state": "INCIERTO",
            "confidence": 0.5,
            "evidence": [],
            "reason": f"API error: {str(e)}"
        }


def classify_seller_state(parsed: dict[str, Any]) -> dict[str, Any]:
    """Rule-based seller classifier used by the rebuilt pipeline with DeepSeek fallback."""
    validation = parsed.get("html_validation", {}) or {}
    if validation.get("status") == "LISTING_REMOVED":
        return {
            "state": "AD_REMOVED",
            "confidence": 1.0,
            "score": 1.0,
            "signals": {},
            "evidence": [validation.get("evidence", "listing removed")],
            "reason": "HTML validation detected removed listing",
            "version": "v5-rule-based",
            "rule_state": "AD_REMOVED",
            "ai_used": False,
            "ai_model": "",
            "ai_owner_score": 0,
            "ai_broker_score": 0,
            "ai_reason": "",
            "final_state": "AD_REMOVED",
            "publisher_identity_candidates": parsed.get("publisher_identity_candidates", []),
        }

    # ── Step 1: Check strong broker rules on ALL publisher identity candidates ──
    strong_broker = _check_strong_broker_rules(parsed)
    if strong_broker:
        strong_broker["publisher_identity_candidates"] = parsed.get("publisher_identity_candidates", [])
        return strong_broker

    signals = _gather_signals(parsed)
    state, score = _score_state(signals)

    # Determine if we should call DeepSeek
    from config import AppConfig
    app_cfg = AppConfig()
    
    seller_name = str(parsed.get("seller_name", "")).strip()
    is_person = False
    if seller_name:
        tokens = [t for t in seller_name.split() if t.isalpha()]
        if 2 <= len(tokens) <= 4 and all(t.istitle() or t.isupper() for t in tokens):
            is_person = True

    seller_text = str(parsed.get("seller_text", "")).strip()
    seller_combined = f"{seller_name} {seller_text}".lower()
    strong_broker_keywords = ("propiedades", "corredores", "corredora", "broker", "inmobiliaria", "spa", "ltda", "s.a.", "srl")
    has_company = any(kw in seller_combined for kw in strong_broker_keywords)

    ai_used = False
    final_state = state
    ai_owner_score = 0
    ai_broker_score = 0
    ai_reason = ""
    evidence = []
    reason = ""
    _comm_ev = ""

    # Condición de activación para etapa IA
    if state == "INCIERTO" or state == "DUEÑO_PROBABLE" or (is_person and not has_company and state != "CORREDOR_SEGURO"):
        # ── Visual watermark stage (before DeepSeek, safe-fail) ──────────────
        visual_meta: dict[str, Any] = {}
        if state == "INCIERTO" and is_person:
            img_list = parsed.get("image_urls") or parsed.get("images") or []
            try:
                from visual_watermark_detector import detect_watermarks  # type: ignore
                visual_meta = detect_watermarks(img_list, max_images=3, timeout=8)
            except Exception as _ve:
                visual_meta = {
                    "visual_watermark_checked": False,
                    "visual_watermark_error": f"import_error:{_ve}",
                    "visual_watermark_text": "",
                    "visual_watermark_signals": [],
                    "visual_watermark_broker_score": 0,
                    "visual_images_checked": 0,
                    "visual_images_available": 0,
                }

            if visual_meta.get("visual_watermark_broker_score", 0) >= 70:
                # Strong visual evidence → override to CORREDOR_SEGURO immediately
                final_state = "CORREDOR_SEGURO"
                evidence = visual_meta.get("visual_watermark_signals", [])
                reason = (
                    "Watermark/logo in listing image indicates broker or real-estate company. "
                    f"Signals: {evidence}"
                )
                return {
                    "state": final_state,
                    "confidence": 0.9,
                    "score": 0.9,
                    "signals": signals,
                    "evidence": evidence[:8],
                    "reason": reason,
                    "version": "v5-rule-based",
                    "rule_state": state,
                    "ai_used": False,
                    "ai_model": "",
                    "ai_owner_score": 0,
                    "ai_broker_score": 0,
                    "ai_reason": "",
                    "final_state": final_state,
                    **visual_meta,
                }
            # Store visual metadata to be merged into result regardless of score
            _visual_meta_for_merge = visual_meta
        else:
            _visual_meta_for_merge = {}

        if app_cfg.deepseek_enabled and app_cfg.deepseek_api_key:
            ai_res = _classify_with_deepseek_custom(parsed, app_cfg)
            ai_used = True
            final_state = ai_res.get("final_state", state)
            ai_owner_score = ai_res.get("owner_score", 0)
            ai_broker_score = ai_res.get("broker_score", 0)
            ai_reason = ai_res.get("reason", "")
            score = ai_res.get("confidence", score)
            evidence = ai_res.get("evidence", [])
            reason = ai_res.get("reason", "")
    else:
        _visual_meta_for_merge = {}

    if not ai_used:
        for key, matched in signals.items():
            if matched:
                evidence.append(key)
        _comm_ev = parsed.get("_commercial_identity_evidence", "")
        if _comm_ev:
            evidence.append(_comm_ev)
        reason = {
            "CORREDOR_SEGURO": "Seller text points to a broker or real-estate company.",
            "DUEÑO_SEGURO": "Seller text points to an owner or direct listing.",
            "INCIERTO": "The available HTML does not clearly identify seller type.",
        }.get(state, "No obvious evidence.")
        if _comm_ev and state == "CORREDOR_SEGURO":
            reason = _comm_ev

    # Determine decision_source and decision_pattern
    decision_source = ""
    decision_pattern = ""
    if ai_used:
        decision_source = "deepseek"
        decision_pattern = ai_reason[:100] if ai_reason else "deepseek_call"
    elif _comm_ev:
        if "avatar/logo" in _comm_ev:
            decision_source = "seller_avatar_alt"
        elif "title" in _comm_ev:
            decision_source = "title"
        elif "nombre del vendedor" in _comm_ev or "seller" == parsed.get("_commercial_identity_source", ""):
            decision_source = "seller_name"
        else:
            decision_source = parsed.get("_commercial_identity_source", "unknown")
        # Extract pattern from evidence
        for ev_item in evidence:
            if "contiene" in ev_item:
                decision_pattern = ev_item
                break

    publisher_candidates = parsed.get("publisher_identity_candidates", [])

    # Build classification_debug for INCIERTO cases (auditability)
    classification_debug = {}
    if final_state == "INCIERTO":
        classification_debug["publisher_identity_candidates"] = publisher_candidates
        classification_debug["deepseek_payload_included_identity_candidates"] = ai_used
        classification_debug["signal_state_before_ai"] = state
        classification_debug["signals"] = dict(signals)
        if ai_used:
            classification_debug["deepseek_reason"] = ai_reason or ""

    return {
        "state": final_state,
        "confidence": score,
        "score": score,
        "signals": signals,
        "evidence": evidence[:8],
        "reason": reason,
        "decision_source": decision_source,
        "decision_pattern": decision_pattern,
        "version": "v5-rule-based",
        "rule_state": state,
        "ai_used": ai_used,
        "ai_model": app_cfg.deepseek_model if ai_used else "",
        "ai_owner_score": ai_owner_score,
        "ai_broker_score": ai_broker_score,
        "ai_reason": ai_reason,
        "final_state": final_state,
        "publisher_identity_candidates": publisher_candidates,
        "classification_debug": classification_debug,
        **_visual_meta_for_merge,
    }


def _enrich_classification(classification: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    validation = parsed.get("html_validation", {}) or {}
    enriched = dict(classification)
    enriched.setdefault("version", "v5-rule-based")
    enriched["html_validation_status"] = validation.get("status", "UNKNOWN")
    enriched["html_validation_reason"] = validation.get("reason", "")
    enriched["evidence"] = classification.get("evidence", [])
    enriched["confidence"] = float(classification.get("confidence", 0.0))
    enriched["reason"] = classification.get("reason", "")
    return enriched


def _build_rule_based_details(parsed: dict[str, Any]) -> dict[str, Any]:
    validation = parsed.get("html_validation", {}) or {}
    classification = _enrich_classification(classify_seller_state(parsed), parsed)

    scrape_stage = "parsed"
    if validation.get("status") == "LISTING_REMOVED":
        scrape_stage = "ad_removed"
        classification["state"] = "AD_REMOVED"
    elif validation.get("status") != "OK":
        scrape_stage = "needs_rescrape"

    return {
        "listing_id": parsed.get("listing_id", ""),
        "url": parsed.get("source_url", ""),
        "canonical_url": parsed.get("canonical_url", ""),
        "title": parsed.get("title", ""),
        "operacion": parsed.get("operacion", ""),
        "tipo_propiedad": parsed.get("tipo_propiedad", ""),
        "comuna": parsed.get("comuna", ""),
        "region": parsed.get("region", ""),
        "fecha_publicacion_raw": parsed.get("fecha_publicacion_raw", ""),
        "fecha_publicacion": parsed.get("fecha_publicacion", ""),
        "dormitorios": parsed.get("dormitorios"),
        "banos": parsed.get("banos"),
        "estacionamientos": parsed.get("estacionamientos"),
        "m2_construidos": parsed.get("m2_construidos"),
        "m2_totales": parsed.get("m2_totales"),
        "gastos_comunes": parsed.get("gastos_comunes"),
        "direccion_exacta": parsed.get("direccion_exacta", ""),
        "precio_raw": parsed.get("precio_raw", ""),
        "precio_moneda_original": parsed.get("precio_moneda_original", ""),
        "precio_original_num": parsed.get("precio_original_num"),
        "precio_uf": parsed.get("precio_uf"),
        "precio_clp": parsed.get("precio_clp"),
        "uf_valor_usado": parsed.get("uf_valor_usado"),
        "uf_fecha": parsed.get("uf_fecha", ""),
        "precio_validacion": parsed.get("precio_validacion", ""),
        "precio_detectado_alternativo": parsed.get("precio_detectado_alternativo", ""),
        "precio_conversion_source": parsed.get("precio_conversion_source", ""),
        "price": parsed.get("price", ""),
        "description": parsed.get("description", ""),
        "seller_name": parsed.get("seller_name", ""),
        "seller_text": parsed.get("seller_text", ""),
        "seller_avatar_alt": parsed.get("seller_avatar_alt", ""),
        "image_urls_count": parsed.get("image_urls_count", 0),
        "image_urls_detected_count": parsed.get("image_urls_detected_count"),
        "main_image_url": parsed.get("main_image_url", ""),
        "image_urls": parsed.get("image_urls", []),
        "visual_image_source": parsed.get("visual_image_source", "unknown"),
        "attributes": parsed.get("attributes", {}),
        "body_text": parsed.get("body_text", ""),
        "description_available": bool(parsed.get("description_available", False)),
        "html_validation_status": validation.get("status", "UNKNOWN"),
        "html_validation_reason": validation.get("reason", ""),
        "html_validation_evidence": validation.get("evidence", ""),
        "scrape_stage": scrape_stage,
        "classification": classification,
        "publisher_identity_candidates": parsed.get("publisher_identity_candidates", []),
        "processed_at": _now_iso(),
    }


def process_with_ai(parsed: dict[str, Any]) -> dict[str, Any]:
    """Compatibility shim for the previous pipeline.

    The previous system could fall back to AI-assisted classification. The
    rebuilt version keeps the hook but uses the same rule-based classifier so
    callers do not break when they expect this symbol.
    """
    if (parsed.get("html_validation") or {}).get("status") == "LISTING_REMOVED":
        return {"state": "AD_REMOVED", "confidence": 1.0, "reason": "listing removed"}
    return classify_seller_state(parsed)


def build_mongo_document(result: dict[str, Any]) -> dict[str, Any]:
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    body_text = result.get("body_text", "")
    
    return {
        "schema_version": "yapo_propiedades_v1",
        "run_id": run_id,
        "source": "owner_hunt",
        "listing_id": result.get("listing_id"),
        "url": result.get("url"),
        "canonical_url": result.get("canonical_url"),
        "title": result.get("title"),
        
        "operacion": result.get("operacion"),
        "tipo_propiedad": result.get("tipo_propiedad"),
        "comuna": result.get("comuna"),
        "region": result.get("region"),
        
        "fecha_publicacion_raw": result.get("fecha_publicacion_raw"),
        "fecha_publicacion": result.get("fecha_publicacion"),
        
        "price": result.get("price"),
        "precio_raw": result.get("precio_raw"),
        "precio_moneda_original": result.get("precio_moneda_original"),
        "precio_original_num": result.get("precio_original_num"),
        "precio_uf": result.get("precio_uf"),
        "precio_clp": result.get("precio_clp"),
        "precio_validacion": result.get("precio_validacion"),
        "precio_detectado_alternativo": result.get("precio_detectado_alternativo"),
        "uf_valor_usado": result.get("uf_valor_usado"),
        "uf_fecha": result.get("uf_fecha"),
        "precio_conversion_source": result.get("precio_conversion_source"),
        
        "dormitorios": result.get("dormitorios"),
        "banos": result.get("banos"),
        "estacionamientos": result.get("estacionamientos"),
        "m2_construidos": result.get("m2_construidos"),
        "m2_totales": result.get("m2_totales"),
        "gastos_comunes": result.get("gastos_comunes"),
        "direccion_exacta": result.get("direccion_exacta"),
        
        "seller_name": result.get("seller_name"),
        "seller_text": result.get("seller_text"),
        "seller_avatar_alt": result.get("seller_avatar_alt"),
        
        "description": result.get("description"),
        "description_available": result.get("description_available"),
        
        "main_image_url": result.get("main_image_url"),
        "image_urls": result.get("image_urls", []),
        "image_urls_count": result.get("image_urls_count"),
        "image_urls_detected_count": result.get("image_urls_detected_count"),
        
        "classification": result.get("classification"),
        "classification_debug": (result.get("classification") or {}).get("classification_debug", {}),
        "publisher_identity_candidates": result.get("publisher_identity_candidates", []),
        
        "html_path": result.get("html_path"),
        "html_validation_status": result.get("html_validation_status"),
        "html_validation_reason": result.get("html_validation_reason"),
        
        "downloaded": result.get("downloaded"),
        "fetch_source": result.get("fetch_source"),
        "scrape_stage": result.get("scrape_stage"),
        "processed_at": result.get("processed_at"),
        "updated_at": _now_iso(),
        
        "raw_attributes": result.get("attributes", {}),
        "body_text_len": len(body_text),
        "body_text_excerpt": body_text[:500],
    }


def process_url(url: str, cfg: ScraperConfig | None = None, *, force_refresh: bool = False) -> dict[str, Any]:
    cfg = cfg or ScraperConfig()
    fetch_info = extract_fast_path(url, cfg, force_refresh=force_refresh)
    html_path = Path(fetch_info["html_path"])
    html = html_path.read_text(encoding="utf-8", errors="replace")
    parsed = _parse_html_fast(html, url=fetch_info["url"])
    record = _build_rule_based_details(parsed)
    record.update(
        {
            "downloaded": fetch_info["downloaded"],
            "fetch_source": fetch_info["fetch_source"],
            "html_path": fetch_info["html_path"],
        }
    )
    return build_mongo_document(record)


def process_batch(
    urls: Iterable[str],
    cfg: ScraperConfig | None = None,
    *,
    force_refresh: bool = False,
    store: Any | None = None,
) -> list[dict[str, Any]]:
    cfg = cfg or ScraperConfig()
    url_list = list(urls)
    total = len(url_list)
    results: list[dict[str, Any] | None] = [None] * total
    start_time = datetime.now()

    def _work(idx: int, url: str) -> tuple[int, dict[str, Any]]:
        try:
            return idx, process_url(url, cfg, force_refresh=force_refresh)
        except Exception as exc:
            return idx, {
                "url": url,
                "scrape_stage": "error",
                "error": str(exc),
                "processed_at": _now_iso(),
            }

    MAX_WORKERS = min(10, os.cpu_count() or 4, total)
    LOGGER.info("[BATCH] concurrent workers=%d urls=%d", MAX_WORKERS, total)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        fut_map = {pool.submit(_work, i, url): i for i, url in enumerate(url_list)}
        done_count = 0
        for fut in as_completed(fut_map):
            idx = fut_map[fut]
            try:
                _, result = fut.result(timeout=90)
            except Exception as exc:
                result = {"url": url_list[idx], "scrape_stage": "error", "error": str(exc), "processed_at": _now_iso()}
            results[idx] = result
            done_count += 1

            # Incremental Mongo save + progress log
            if store is not None:
                try:
                    store.upsert_listing(result)
                except Exception as _mongo_err:
                    LOGGER.error("[MONGO_ERR] %s: %s", result.get("url","")[-40:], _mongo_err)

            if done_count % 25 == 0 or done_count == total:
                elapsed = (datetime.now() - start_time).total_seconds()
                rate = done_count / (elapsed / 60) if elapsed > 0 else 0
                LOGGER.info(
                    "[PROGRESS] processed=%d/%d failed=%d elapsed=%.0fs rate=%.1f/min",
                    done_count, total,
                    sum(1 for r in results[:done_count] if r and r.get("scrape_stage") == "error"),
                    elapsed, rate,
                )

    return [r for r in results if r is not None]


def _write_report(results: list[dict[str, Any]], cfg: ScraperConfig) -> Path:
    _ensure_dir(cfg.output_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = cfg.output_dir / f"yapo_inciertos_{timestamp}.json"
    uncertain = [r for r in results if r.get("classification", {}).get("state") == "INCIERTO"]
    _safe_json_dump(report_path, {"results": results, "inciertos": uncertain})
    return report_path


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Yapo.cl property scraper")
    parser.add_argument("--base-url", default="https://www.yapo.cl", help="Yapo search/listing base URL")
    parser.add_argument("--max-pages", type=int, default=50, help="Maximum listing pages to discover")
    parser.add_argument("--max-urls", type=int, default=2000, help="Maximum URLs to process per session")
    parser.add_argument("--target-new", type=int, default=500, help="Stop discovery after N new (not-in-Mongo) URLs")
    parser.add_argument("--discover", action="store_true", help="Run discovery stage before scraping")
    parser.add_argument("--input-file", help="JSON/JSONL file with explicit URLs to scrape")
    parser.add_argument("--url", action="append", default=[], help="Single URL to process; can be repeated")
    parser.add_argument("--force-refresh", action="store_true", help="Force redownload even if HTML exists")
    parser.add_argument("--report", action="store_true", help="Write a QA-friendly JSON report")
    parser.add_argument("--no-mongo", action="store_true", help="Do not save results to MongoDB")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    return parser.parse_args(argv)


def _load_urls_from_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return []
    if path.suffix.lower() == ".jsonl":
        urls = []
        for line in text.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                url = item.get("url")
                if url:
                    urls.append(str(url))
        return urls
    data = json.loads(text)
    if isinstance(data, list):
        return [str(item) for item in data if item]
    if isinstance(data, dict):
        if "urls" in data and isinstance(data["urls"], list):
            return [str(item) for item in data["urls"] if item]
        if "url" in data and data["url"]:
            return [str(data["url"])]
    raise ValueError(f"Unsupported URL file format: {path}")


async def _run_discovery(cfg: ScraperConfig, *, mongo_collection: Any | None = None) -> list[str]:
    return await discover_new_properties(
        cfg,
        base_url=cfg.base_url,
        max_pages=cfg.max_pages,
        max_urls_per_session=cfg.max_urls_per_session,
        target_new=cfg.target_new_urls,
        mongo_collection=mongo_collection,
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    cfg = ScraperConfig(
        base_url=args.base_url,
        max_pages=args.max_pages,
        max_urls_per_session=args.max_urls,
        target_new_urls=args.target_new,
    )

    # Mongo setup
    mongo_collection = None
    store = None
    db_name = "N/A"
    coll_name = "N/A"
    if not args.no_mongo:
        try:
            from config import get_config
            from mongo_store import MongoStore
            from pymongo import MongoClient as PMC
            app_cfg = get_config()
            store = MongoStore(config=app_cfg)
            db_name = app_cfg.mongo_db
            coll_name = app_cfg.mongo_collection
            client = PMC(app_cfg.mongo_uri)
            mongo_collection = client[db_name][coll_name]
        except Exception as e:
            LOGGER.error("Failed to initialize Mongo: %s", e)
            store = None

    total_before = mongo_collection.count_documents({}) if mongo_collection is not None else 0

    urls: list[str] = []
    if args.input_file:
        urls.extend(_load_urls_from_file(Path(args.input_file)))
    urls.extend(args.url)

    if args.discover or not urls:
        discovered = asyncio.run(_run_discovery(cfg, mongo_collection=mongo_collection))
        urls = discovered if not urls else list(dict.fromkeys(urls + discovered))

    urls = list(dict.fromkeys(_normalize_url(url) for url in urls))
    if not urls:
        LOGGER.warning("No URLs to process.")
        return 0

    batch_start = datetime.now()
    results = process_batch(
        urls[: cfg.max_urls_per_session],
        cfg,
        force_refresh=args.force_refresh,
        store=store,
    )
    batch_elapsed = (datetime.now() - batch_start).total_seconds()

    if args.report:
        report_path = _write_report(results, cfg)
        LOGGER.info("Report written to %s", report_path)

    total_after = mongo_collection.count_documents({}) if mongo_collection is not None else 0
    new_inserted = total_after - total_before

    print("\n=== REPORTE DE PRUEBA ===")
    print(f"URLs procesadas: {len(results)}")
    print(f"  nuevas insertadas: {new_inserted}")
    print(f"  existentes saltadas: {len(urls) - len(results)} (pre-filtered by discovery)")
    print(f"Tiempo total: {batch_elapsed:.0f}s ({len(results)/(batch_elapsed/60):.1f}/min)")
    if not args.no_mongo:
        print(f"DB Mongo usada: {db_name}")
        print(f"Colección Mongo usada: {coll_name}")
        print(f"Documentos pre-scrape: {total_before}")
        print(f"Documentos post-scrape: {total_after}")

    # Classification summary
    states = {}
    ai_used_count = 0
    invalid_json_count = 0
    empty_seller = 0
    for res in results:
        cls = res.get("classification", {})
        fs = cls.get("final_state", "N/A")
        states[fs] = states.get(fs, 0) + 1
        if cls.get("ai_used"):
            ai_used_count += 1
        if "Invalid JSON from LLM" in cls.get("reason", ""):
            invalid_json_count += 1
        if not res.get("seller_name"):
            empty_seller += 1

    print(f"\n  CORREDOR_SEGURO: {states.get('CORREDOR_SEGURO', 0)}")
    print(f"  DUEÑO_SEGURO:    {states.get('DUEÑO_SEGURO', 0)}")
    print(f"  DUEÑO_PROBABLE:  {states.get('DUEÑO_PROBABLE', 0)}")
    print(f"  INCIERTO:        {states.get('INCIERTO', 0)}")
    print(f"  AD_REMOVED:      {states.get('AD_REMOVED', 0)}")
    print(f"ai_used: {ai_used_count}  |  Invalid JSON: {invalid_json_count}  |  seller_name vacio: {empty_seller}")
    print("-" * 40)

    # Show first 5 results
    for res in results[:5]:
        cls = res.get("classification", {})
        print(f"  {res.get('listing_id','?'):12s} | {cls.get('final_state','?'):18s} | seller={res.get('seller_name','')[:30]:30s} | ai={cls.get('ai_used')}")

    # ---- POST-SCRAPE: distribuir captaciones nuevas ----
    if not args.no_mongo and store is not None and new_inserted > 0:
        _run_post_scrape_distribution()

    return 0


def _run_post_scrape_distribution():
    """Dispara la distribucion de captaciones nuevas tras persistir el lote."""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    script = root / "scripts" / "run_distribution_after_scrape.py"
    if not script.exists():
        print(f"  [POST-SCRAPE] Script de distribucion no encontrado: {script}")
        return
    try:
        r = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=300,
        )
        if r.stdout.strip():
            print(r.stdout.strip())
        if r.returncode != 0 and r.stderr.strip():
            print(f"  [POST-SCRAPE] stderr: {r.stderr.strip()[-400:]}")
    except Exception as e:
        print(f"  [POST-SCRAPE] No se pudo distribuir: {e}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

