from __future__ import annotations

import json
import re
import unicodedata
from html import unescape
from typing import Any
from urllib.parse import urljoin

try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore


DESCRIPTION_TRUNCATION_HINTS = (
    "...",
    "ver mas",
    "ver más",
    "continuar leyendo",
    "leer mas",
    "leer más",
    "mostrar más",
    "mostrar mas",
)


def _strip_html(text: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return unescape(re.sub(r"\s+", " ", text)).strip()


def _first_text(soup: Any, selectors: list[str]) -> str:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = node.get_text(" ", strip=True)
            if text:
                return re.sub(r"\s+", " ", text).strip()
    return ""


def _jsonld_seller_name(soup: Any) -> tuple[str, str]:
    """Return a name only when JSON-LD links it explicitly to ``seller``."""
    def seller_name(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            name = value.get("name")
            return name.strip() if isinstance(name, str) else ""
        return ""

    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        stack = list(nodes)
        while stack:
            node = stack.pop(0)
            if isinstance(node, list):
                stack[0:0] = node
                continue
            if not isinstance(node, dict):
                continue
            for key, value in node.items():
                if key == "seller":
                    name = seller_name(value)
                    if name:
                        return name, "jsonld_seller_relation"
                elif key == "offers":
                    offers = value if isinstance(value, list) else [value]
                    for offer in offers:
                        if isinstance(offer, dict):
                            name = seller_name(offer.get("seller"))
                            if name:
                                return name, "jsonld_offer_seller_relation"
                if isinstance(value, (dict, list)):
                    stack.append(value)
    return "", ""


def _jsonld_name(soup: Any) -> str:
    """Compatibility wrapper; never returns a property or arbitrary JSON-LD name."""
    return _jsonld_seller_name(soup)[0]


_PUBLISHER_SCOPE_SELECTORS = (
    ".contact_info", ".property-contact", ".contact-info", ".seller-profile",
    ".publisher-profile", ".seller", ".publisher", ".profile",
    "[data-qa='seller']", "[data-qa='publisher']", "[data-qa='contact']",
)


def _publisher_scopes(soup: Any) -> list[Any]:
    scopes: list[Any] = []
    seen: set[int] = set()
    for selector in _PUBLISHER_SCOPE_SELECTORS:
        for node in soup.select(selector):
            if id(node) not in seen:
                scopes.append(node)
                seen.add(id(node))
    for link in soup.select("a[href*='/user/profile/id/']"):
        parent = link.parent
        if parent is not None and id(parent) not in seen:
            scopes.append(parent)
            seen.add(id(parent))
    return scopes


def _scoped_first_text(scopes: list[Any], selectors: list[str]) -> tuple[str, str]:
    for selector in selectors:
        for scope in scopes:
            node = scope.select_one(selector)
            if node:
                value = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
                if value:
                    return value, selector
    return "", ""


def _contact_logo_alt(scopes: list[Any]) -> tuple[str, str]:
    for scope in scopes:
        for node in scope.select("img[alt]"):
            alt = str(node.get("alt") or "").strip()
            src = " ".join(str(node.get(key) or "") for key in ("src", "data-src", "data-original", "data-lazy")).lower()
            classes = " ".join(str(item) for item in (node.get("class") or [])).lower()
            parent_classes = " ".join(str(item) for item in (getattr(node.parent, "get", lambda *_: [])("class") or []))
            logo_marker = (
                "logo" in classes or "logo" in parent_classes.lower()
                or any(marker in src for marker in ("users/logo", "t_user_logo", "user_logo", "publisher-logo", "seller-logo"))
            )
            if alt and logo_marker:
                return alt, "publisher_scoped_logo"
    return "", ""


def _contact_badges_text(scopes: list[Any]) -> str:
    nodes = [node for scope in scopes for node in scope.select(".badge, .badges, .seller-badges, .contact-badges, [class*='badge']")]
    values = [node.get_text(" ", strip=True) for node in nodes]
    values.extend(str(node.get("title")) for scope in scopes for node in scope.select("img[title]") if node.get("title"))
    return _strip_html(" ".join(values))


def _publicador_visible(scopes: list[Any]) -> tuple[str, str]:
    candidates = [
        ".seller-name",
        ".contact-name",
        ".contact_name",
        ".publicador",
        ".publisher",
        ".profile-name",
        "[data-qa='seller-name']",
        "[data-qa='user-name']",
    ]
    return _scoped_first_text(scopes, candidates)


def _description_from_soup(soup: Any) -> tuple[str, str, str]:
    selectors = [
        (".d3-property-about__text", "d3_property_about_text"),
        ("[data-qa='description']", "data_qa_description"),
        (".description", "class_description"),
        (".item-description", "class_item_description"),
        ("#description", "id_description"),
        ("article", "article"),
    ]
    for selector, source in selectors:
        node = soup.select_one(selector)
        if node:
            text = node.get_text(" ", strip=True)
            if text:
                return re.sub(r"\s+", " ", text).strip(), source, ""
    meta_desc = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        # Meta description is commonly a short search snippet. Keep it as a
        # separate diagnostic value; never report it as the listing's full text.
        return "", "missing_full_description", str(meta_desc["content"]).strip()
    return "", "missing_full_description", ""


def _is_source_truncated(description: str, source: str, soup: Any) -> bool:
    text = description.strip().lower()
    if not text:
        return False
    if any(hint in text for hint in DESCRIPTION_TRUNCATION_HINTS):
        return True
    if source == "meta_description":
        # Meta descriptions are often summaries, not the full body.
        return True
    if soup is not None:
        more_markers = soup.select_one("[class*='ver-mas'], [class*='show-more'], [class*='expand'], button[aria-expanded='false']")
        if more_markers is not None:
            return True
    return False


_REAL_ESTATE_SCHEMA_TYPES = {
    "realestatelisting", "house", "singlefamilyresidence", "apartment", "residence",
}


def _schema_type_values(value: Any) -> set[str]:
    values = value if isinstance(value, list) else [value]
    return {
        str(item).rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1].lower()
        for item in values if isinstance(item, str)
    }


def _commune_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")


def _commune_identity(value: str) -> str:
    key = _commune_key(value)
    for suffix in ("-region-metropolitana-de-santiago", "-region-metropolitana"):
        if key.endswith(suffix):
            return key[:-len(suffix)]
    return key


def _structured_real_estate_localities(soup: Any) -> list[str]:
    """Read addressLocality only under a typed real-estate object and PostalAddress."""
    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, dict):
            return
        if _schema_type_values(value.get("@type")) & _REAL_ESTATE_SCHEMA_TYPES:
            addresses = value.get("address", [])
            addresses = addresses if isinstance(addresses, list) else [addresses]
            for address in addresses:
                if not isinstance(address, dict):
                    continue
                if "postaladdress" not in _schema_type_values(address.get("@type")):
                    continue
                locality = address.get("addressLocality")
                if isinstance(locality, str) and locality.strip():
                    found.append(locality.strip())
        for child in value.values():
            if isinstance(child, (dict, list)):
                visit(child)

    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            visit(json.loads(raw))
        except (TypeError, ValueError):
            continue

    # Support equivalent Schema.org microdata while requiring both the real-estate
    # item type and a nested PostalAddress item scope.
    for property_node in soup.select("[itemscope][itemtype]"):
        parent_types = _schema_type_values(property_node.get("itemtype"))
        if not parent_types & _REAL_ESTATE_SCHEMA_TYPES:
            continue
        for address in property_node.select('[itemprop="address"][itemscope][itemtype]'):
            if "postaladdress" not in _schema_type_values(address.get("itemtype")):
                continue
            for locality in address.select('[itemprop="addressLocality"]'):
                value = locality.get("content") or locality.get_text(" ", strip=True)
                if value:
                    found.append(str(value).strip())
    return list(dict.fromkeys(found))


def extract_listing_commune(soup: Any, attributes: dict[str, Any]) -> dict[str, Any]:
    """Resolve commune from explicit detail attributes or typed address evidence.

    Seller text, marketing copy, and arbitrary body-text matches are deliberately
    excluded. Explicit ``Comuna`` attributes take precedence, but contradictions
    remain marked as ambiguous.
    """
    explicit = ""
    for key, value in attributes.items():
        label = _commune_key(str(key)).replace("-", " ").strip()
        if label == "comuna" and str(value).strip():
            explicit = str(value).strip()
            break
    structured = _structured_real_estate_localities(soup)
    structured_keys = {_commune_identity(value) for value in structured if _commune_identity(value)}
    explicit_key = _commune_identity(explicit)
    if explicit and structured_keys and (structured_keys != {explicit_key}):
        return {
            "comuna": explicit,
            "comuna_source": "explicit_attribute",
            "comuna_evidence": {"status": "pending_ambiguous", "structured_address_localities": structured},
        }
    if explicit:
        return {
            "comuna": explicit,
            "comuna_source": "explicit_attribute",
            "comuna_evidence": {"status": "confirmed_detail", "structured_address_localities": structured},
        }
    if len(structured_keys) == 1:
        locality = next(value for value in structured if _commune_identity(value) in structured_keys)
        return {
            "comuna": locality,
            "comuna_source": "structured_real_estate_address",
            "comuna_evidence": {"status": "confirmed_detail", "structured_address_localities": structured},
        }
    if len(structured_keys) > 1:
        return {
            "comuna": "",
            "comuna_source": "structured_real_estate_address",
            "comuna_evidence": {"status": "pending_ambiguous", "structured_address_localities": structured},
        }
    return {"comuna": "", "comuna_source": "", "comuna_evidence": {"status": "pending"}}


def extract_listing_fields(html: str, source_url: str = "") -> dict[str, Any]:
    if BeautifulSoup is None:
        text = _strip_html(html)
        return {
            "source_url": source_url,
            "title": text[:120],
            "price": "",
            "description": text,
            "descripcion": text,
            "description_summary": "",
            "descripcion_len": len(text),
            "descripcion_source": "html_text_fallback",
            "descripcion_is_truncated": False,
            "publicador_visible": "",
            "contact_name": "",
            "contact_logo_alt": "",
            "seller_type": "",
            "listing_advertiser": "",
            "seller_jsonld_name": "",
            "contact_badges_text": "",
            "seller_name": "",
            "seller_text": "",
            "images": [],
            "attributes": {},
            "comuna": "",
            "comuna_source": "",
            "comuna_evidence": {"status": "pending"},
            "body_text": text,
            "canonical_url": source_url,
            "publisher_field_provenance": {},
        }

    soup = BeautifulSoup(html, "html.parser")  # type: ignore[misc]
    title = _first_text(soup, ["h1", ".d3-property-details__title", "[data-qa='title']", "title"])
    if not title:
        meta_title = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "title"})
        if meta_title and meta_title.get("content"):
            title = meta_title["content"].strip()

    price = _first_text(soup, ["[data-qa='price']", ".d3-property-info__price", ".d3-property-insight__attribute-value", ".price", ".item-price", "strong.price", "span.price"])
    description, description_source, description_summary = _description_from_soup(soup)

    publisher_scopes = _publisher_scopes(soup)
    seller_name, seller_name_selector = _scoped_first_text(
        publisher_scopes,
        ["[data-qa='seller-name']", "[data-qa='user-name']", ".seller-name", ".profile-name", ".contact-name", ".contact_name"],
    )
    seller_text = _strip_html(" ".join(node.get_text(" ", strip=True) for node in publisher_scopes))

    publicador_visible, publicador_selector = _publicador_visible(publisher_scopes)
    seller_jsonld_name, seller_jsonld_source = _jsonld_seller_name(soup)
    contact_name = publicador_visible or seller_name or seller_jsonld_name
    contact_logo_alt, logo_source = _contact_logo_alt(publisher_scopes)
    contact_badges_text = _contact_badges_text(publisher_scopes)

    seller_type, seller_type_selector = _scoped_first_text(
        publisher_scopes,
        [
            ".seller-type",
            ".contact-type",
            ".advertiser-type",
            "[data-qa='seller-type']",
        ],
    )
    listing_advertiser, advertiser_selector = _scoped_first_text(
        publisher_scopes,
        [
            ".listing-advertiser",
            "[data-qa='listing-advertiser']",
            "[data-qa='advertiser-name']",
        ],
    )

    seller_profile_id = ""
    profile_link = next((node for scope in publisher_scopes for node in scope.select("a[href*='/user/profile/id/']")), None)
    if profile_link:
        match = re.search(r"/user/profile/id/(\d+)", str(profile_link.get("href") or ""))
        seller_profile_id = match.group(1) if match else ""
    if not seller_type and "profesional" in contact_badges_text.lower():
        seller_type = "PROFESIONAL"

    provenance: dict[str, dict[str, str]] = {}
    provenance_sources = {
        "publicador_visible": f"publisher_scope:{publicador_selector}" if publicador_selector else "",
        "contact_name": "publisher_identity" if publicador_visible or seller_name else seller_jsonld_source,
        "contact_logo_alt": logo_source,
        "seller_type": (f"publisher_scope:{seller_type_selector}" if seller_type_selector else "publisher_scoped_badge") if seller_type else "",
        "listing_advertiser": f"publisher_scope:{advertiser_selector}" if advertiser_selector else "",
        "seller_jsonld_name": seller_jsonld_source,
        "contact_badges_text": "publisher_scoped_badge" if contact_badges_text else "",
    }
    for field, source in provenance_sources.items():
        if source and locals().get(field):
            provenance[field] = {"status": "confirmed", "source": source, "relationship": "publisher"}

    images: list[str] = []
    for img in soup.select("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy"):
            value = img.get(attr)
            if value:
                full = urljoin(source_url or "", value)
                if full not in images:
                    images.append(full)

    attributes: dict[str, str] = {}
    for node in soup.select("table tr, li, [data-qa]"):
        text = node.get_text(" ", strip=True)
        if ":" not in text:
            continue
        key, value = text.split(":", 1)
        key = re.sub(r"\s+", " ", key).strip().lower()
        value = re.sub(r"\s+", " ", value).strip()
        if key and value and key not in attributes:
            attributes[key] = value
    for node in soup.select(".d3-property-insight__attribute-details"):
        key_node = node.select_one("dt")
        value_node = node.select_one("dd")
        if key_node and value_node:
            key = re.sub(r"\s+", " ", key_node.get_text(" ", strip=True)).strip().lower()
            value = re.sub(r"\s+", " ", value_node.get_text(" ", strip=True)).strip()
            if key and value:
                attributes.setdefault(key, value)
    for node in soup.select(".d3-property-details__detail-label"):
        value_node = node.select_one(".d3-property-details__detail")
        if value_node:
            whole = node.get_text(" ", strip=True)
            value = value_node.get_text(" ", strip=True)
            key = whole[:whole.find(value)].strip().lower() if value in whole else ""
            if key and value:
                attributes.setdefault(key, value)

    commune = extract_listing_commune(soup, attributes)

    body_text = soup.get_text(" ", strip=True)
    canonical = soup.find("link", attrs={"rel": "canonical"})
    canonical_url = canonical.get("href", "").strip() if canonical else source_url

    descripcion_is_truncated = bool(description_summary) or _is_source_truncated(description, description_source, soup)

    return {
        "source_url": source_url,
        "title": title,
        "price": price,
        "description": description,
        "descripcion": description,
        "description_summary": description_summary,
        "descripcion_len": len(description),
        "descripcion_source": description_source,
        "descripcion_is_truncated": descripcion_is_truncated,
        "publicador_visible": publicador_visible,
        "contact_name": contact_name,
        "contact_logo_alt": contact_logo_alt,
        "seller_type": seller_type,
        "listing_advertiser": listing_advertiser,
        "seller_jsonld_name": seller_jsonld_name,
        "contact_badges_text": contact_badges_text,
        "seller_name": seller_name,
        "seller_text": seller_text,
        "seller_profile_id": seller_profile_id,
        "seller_profile_url": str(profile_link.get("href") or "") if profile_link else "",
        "publisher_field_provenance": provenance,
        "images": images,
        "attributes": attributes,
        **commune,
        "body_text": re.sub(r"\s+", " ", body_text).strip(),
        "canonical_url": canonical_url,
    }
