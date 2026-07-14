from __future__ import annotations

import json
import re
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


def _jsonld_name(soup: Any) -> str:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for key in ("name", "seller", "brand", "publisher"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, dict):
                    nested = value.get("name")
                    if isinstance(nested, str) and nested.strip():
                        return nested.strip()
    return ""


def _contact_logo_alt(soup: Any) -> str:
    for selector in [".contact_logo img[alt]", ".seller img[alt]", ".profile img[alt]", "img[alt*='logo' i]", "img[alt]"]:
        node = soup.select_one(selector)
        if node and node.get("alt"):
            return str(node.get("alt")).strip()
    return ""


def _contact_badges_text(soup: Any) -> str:
    nodes = soup.select(".badge, .badges, .seller-badges, .contact-badges, [class*='badge']")
    values = [node.get_text(" ", strip=True) for node in nodes]
    values.extend(str(node.get("title")) for node in soup.select(".contact_info img[title], .property-contact img[title]") if node.get("title"))
    return _strip_html(" ".join(values))


def _publicador_visible(soup: Any) -> str:
    candidates = [
        ".seller-name",
        ".contact-name",
        ".contact_name",
        ".publicador",
        ".publisher",
        ".profile-name",
        "[data-qa='seller-name']",
        "[data-qa='user-name']",
        "[class*='seller']",
        "[class*='publisher']",
    ]
    text = _first_text(soup, candidates)
    if text:
        return text
    return _jsonld_name(soup)


def _description_from_soup(soup: Any) -> tuple[str, str]:
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
                return re.sub(r"\s+", " ", text).strip(), source
    meta_desc = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        return str(meta_desc["content"]).strip(), "meta_description"
    return "", ""


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


def extract_listing_fields(html: str, source_url: str = "") -> dict[str, Any]:
    if BeautifulSoup is None:
        text = _strip_html(html)
        return {
            "source_url": source_url,
            "title": text[:120],
            "price": "",
            "description": text,
            "descripcion": text,
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
            "body_text": text,
            "canonical_url": source_url,
        }

    soup = BeautifulSoup(html, "html.parser")  # type: ignore[misc]
    title = _first_text(soup, ["h1", ".d3-property-details__title", "[data-qa='title']", "title"])
    if not title:
        meta_title = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "title"})
        if meta_title and meta_title.get("content"):
            title = meta_title["content"].strip()

    price = _first_text(soup, ["[data-qa='price']", ".d3-property-info__price", ".d3-property-insight__attribute-value", ".price", ".item-price", "strong.price", "span.price"])
    description, description_source = _description_from_soup(soup)
    if not description:
        description = _strip_html(html)
        description_source = "html_text_fallback"

    seller_name = _first_text(soup, [".seller", ".user", ".profile", "[data-qa='seller-name']", "[data-qa='user-name']"])
    seller_text = _strip_html(" ".join(node.get_text(" ", strip=True) for node in soup.select(".seller, .user, .profile")))

    publicador_visible = _publicador_visible(soup)
    seller_jsonld_name = _jsonld_name(soup)
    contact_name = publicador_visible or seller_jsonld_name or seller_name
    contact_logo_alt = _contact_logo_alt(soup)
    contact_badges_text = _contact_badges_text(soup)

    seller_type = _first_text(
        soup,
        [
            ".seller-type",
            ".contact-type",
            ".advertiser-type",
            "[data-qa='seller-type']",
            "[class*='type']",
        ],
    )
    listing_advertiser = _first_text(
        soup,
        [
            ".listing-advertiser",
            ".advertiser",
            ".publisher",
            ".contact-name",
            "[data-qa='listing-advertiser']",
        ],
    )

    seller_profile_id = ""
    profile_link = soup.select_one("a[href*='/user/profile/id/']")
    if profile_link:
        match = re.search(r"/user/profile/id/(\d+)", str(profile_link.get("href") or ""))
        seller_profile_id = match.group(1) if match else ""
    if not seller_type and "profesional" in contact_badges_text.lower():
        seller_type = "PROFESIONAL"

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

    body_text = soup.get_text(" ", strip=True)
    canonical = soup.find("link", attrs={"rel": "canonical"})
    canonical_url = canonical.get("href", "").strip() if canonical else source_url

    descripcion_is_truncated = _is_source_truncated(description, description_source, soup)

    return {
        "source_url": source_url,
        "title": title,
        "price": price,
        "description": description,
        "descripcion": description,
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
        "images": images,
        "attributes": attributes,
        "body_text": re.sub(r"\s+", " ", body_text).strip(),
        "canonical_url": canonical_url,
    }
