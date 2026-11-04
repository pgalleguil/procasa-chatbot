from __future__ import annotations

import json
import re
import unicodedata
from html import unescape
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

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

YAPO_LISTING_PHOTO_HOSTS = frozenset({"photos.encuentra24.com"})


def extract_yapo_listing_images(html: str, listing_id: str, source_url: str = "") -> list[str]:
    """Extract only photo CDN assets whose path identifies this Yapo listing.

    Yapo detail pages include related-property cards and site UI images in the
    same document. On saved pages, the listing's own photo CDN paths use the
    listing ID as the filename prefix (``/{id}_{asset}``). Requiring both the
    known photo host and the exact ID boundary filters those unrelated assets.
    """
    listing_id = str(listing_id or "").strip()
    if not listing_id or not listing_id.isdigit() or not html:
        return []

    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, "html.parser")  # type: ignore[misc]
        nodes = soup.select("img")
        values = [
            node.get(attr)
            for node in nodes
            for attr in ("src", "data-src", "data-original", "data-lazy")
            if node.get(attr)
        ]
    else:  # pragma: no cover - used only where bs4 is unavailable
        values = []
        for tag in re.findall(r"<img\b[^>]*>", html, flags=re.I | re.S):
            for attr, _quote, value in re.findall(
                r"([\w:-]+)\s*=\s*(['\"])(.*?)\2", tag, flags=re.I | re.S
            ):
                if attr.lower() in {"src", "data-src", "data-original", "data-lazy"} and value:
                    values.append(value)

    prefix = re.compile(rf"^{re.escape(listing_id)}[_-](?P<asset>[^/]+)$")
    images: list[str] = []
    seen_assets: set[str] = set()
    for value in values:
        absolute = urljoin(source_url or "", str(value).strip())
        parsed = urlsplit(absolute)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if host not in YAPO_LISTING_PHOTO_HOSTS:
            continue
        basename = unquote(parsed.path.rsplit("/", 1)[-1])
        match = prefix.fullmatch(basename)
        if not match:
            continue
        asset = match.group("asset")
        if asset in seen_assets:
            continue
        seen_assets.add(asset)
        images.append(absolute)
    return images


def _normalized_publisher_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def extract_yapo_publisher_more_ads(
    html_or_soup: Any,
    publisher_name: str = "",
    current_listing_id: str = "",
    seller_profile_id: str = "",
    *,
    parser: str = "html.parser",
) -> dict[str, Any]:
    """Extract only the listing cards under Yapo's ``Más anuncios de`` heading.

    This deliberately scopes to the nearest ancestor containing listing links.
    It does not use the separate ``Anuncios similares`` carousel or profile
    enumeration, so it remains useful when a Yapo profile page is blocked.
    """
    empty = {
        "status": "not_found",
        "publisher_name": "",
        "publisher_profile_id": str(seller_profile_id or ""),
        "heading_text": "",
        "listing_ids": [],
        "distinct_listing_count": 0,
        "source": "yapo_detail_html:more_ads_heading",
    }
    if BeautifulSoup is None or not html_or_soup:
        return {**empty, "status": "unavailable"}
    soup = html_or_soup if hasattr(html_or_soup, "find_all") else BeautifulSoup(str(html_or_soup), parser)  # type: ignore[misc]
    requested_identity = _normalized_publisher_name(publisher_name)
    mismatched_heading = ""
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        heading_text = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).strip()
        normalized_heading = _normalized_publisher_name(heading_text)
        prefix = "mas anuncios de "
        if not normalized_heading.startswith(prefix):
            continue
        section_publisher = heading_text[len("Más anuncios de "):].strip() if heading_text.casefold().startswith("más anuncios de ") else heading_text[len("Mas anuncios de "):].strip()
        section_identity = _normalized_publisher_name(section_publisher)
        if requested_identity and section_identity != requested_identity:
            # Do not attribute another person's seller carousel to this ad.
            mismatched_heading = heading_text
            continue

        for ancestor in heading.parents:
            # The nearest link-bearing ancestor is the card rail itself. Stop
            # before broad containers that can mix similar listings and other
            # page content.
            ancestor_headings = [
                _normalized_publisher_name(node.get_text(" ", strip=True))
                for node in ancestor.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
            ]
            if any(text.startswith("anuncios similares") for text in ancestor_headings):
                break
            listing_ids: list[str] = []
            for anchor in ancestor.select("a[href]"):
                href = str(anchor.get("href") or "").strip()
                path = urlsplit(href).path
                if "/user/profile/" in path.casefold():
                    continue
                match = re.search(r"/(\d{6,})/?$", path)
                if not match:
                    continue
                listing_id = match.group(1)
                if listing_id == str(current_listing_id or "").strip():
                    continue
                if listing_id not in listing_ids:
                    listing_ids.append(listing_id)
            if listing_ids:
                return {
                    "status": "extracted",
                    "publisher_name": section_publisher,
                    "publisher_profile_id": str(seller_profile_id or ""),
                    "heading_text": heading_text,
                    "listing_ids": listing_ids,
                    "distinct_listing_count": len(listing_ids),
                    "source": "yapo_detail_html:more_ads_heading_nearest_listing_scope",
                }
    if mismatched_heading:
        return {**empty, "status": "identity_mismatch", "heading_text": mismatched_heading}
    return empty


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


def _first_text_with_source(soup: Any, selectors: list[str]) -> tuple[str, str]:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            if text:
                return text, selector
    return "", ""


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


def _jsonld_product_details(soup: Any) -> dict[str, Any]:
    """Return the listing Product with an offer, used by Yapo's current detail UI."""
    def walk(value: Any):
        if isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            yield value
            for item in value.values():
                if isinstance(item, (dict, list)):
                    yield from walk(item)

    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for node in walk(payload):
            types = node.get("@type", [])
            types = types if isinstance(types, list) else [types]
            type_names = {str(value).rstrip("/").rsplit("/", 1)[-1].lower() for value in types}
            offers = node.get("offers")
            if "product" in type_names and isinstance(offers, dict) and offers.get("price") is not None:
                return node
    return {}


def _current_yapo_publisher(soup: Any) -> tuple[str, str]:
    """Read the advertiser card introduced by Yapo's current detail template."""
    for heading in soup.select("main h3"):
        if _commune_key(heading.get_text(" ", strip=True)) != "publicado-por":
            continue
        card = heading.parent.parent if heading.parent and heading.parent.parent else heading.parent
        if card is None:
            continue
        names = [node.get_text(" ", strip=True) for node in card.select("h4, [data-qa='publisher-name']")
                 if _commune_key(node.get_text(" ", strip=True)) != "publicado-por"]
        name = next((value for value in names if value), "")
        logo = ""
        for image in card.select("img[alt]"):
            alt = str(image.get("alt") or "").strip()
            if alt and ("logo" in alt.lower() or "foto de perfil" in alt.lower()):
                logo = alt
                break
        if name:
            return name, logo
    return "", ""


def _publisher_profile_account_type(soup: Any, profile_id: str) -> tuple[str, str]:
    """Read a profile badge only when its nearby profile link matches the seller ID."""
    if not profile_id:
        return "", ""
    target = f"/user/profile/id/{profile_id}"
    for node in soup.find_all(["p", "span"]):
        value = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if _commune_key(value) not in {"profesional", "empresa", "agente", "corredor"}:
            continue
        parent = node
        for _ in range(6):
            parent = getattr(parent, "parent", None)
            if parent is None:
                break
            if any(str(link.get("href") or "").rstrip("/").endswith(target) for link in parent.select("a[href*='/user/profile/id/']")):
                return value, "publisher_profile_account_badge"
    return "", ""


def _listing_advertiser_type(soup: Any) -> tuple[str, str]:
    """Extract Yapo's listing-level advertiser attribute separately from profile type."""
    for label in soup.find_all(["p", "dt", "span"]):
        if _commune_key(label.get_text(" ", strip=True)) != "tipo-de-anunciante":
            continue
        parent = label.parent
        if parent is None:
            continue
        values = [
            re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            for node in parent.find_all(["p", "dd", "span"], recursive=True)
            if node is not label and node.get_text(" ", strip=True)
        ]
        value = next((item for item in values if _commune_key(item) != "tipo-de-anunciante"), "")
        if value:
            return value, "yapo_listing_attribute_tipo_anunciante"
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


def _description_from_soup(soup: Any) -> tuple[str, str, str, list[dict[str, Any]]]:
    selectors = [
        (".d3-property-about__text", "d3_property_about_text"),
        ("[data-qa='description']", "data_qa_description"),
        (".description", "class_description"),
        (".item-description", "class_item_description"),
        ("#description", "id_description"),
        ("article", "article"),
    ]
    candidates: list[tuple[int, str, str]] = []
    article_candidates: list[tuple[int, str, str]] = []
    seen_text: set[str] = set()
    # Current Yapo pages render the complete description in a paragraph directly
    # under the section headed "Descripción"; the old d3 selectors no longer match.
    for heading in soup.select("main h2"):
        if _commune_key(heading.get_text(" ", strip=True)) not in {"descripcion", "descripcion-del-anuncio"}:
            continue
        parent = heading.parent
        if parent is None:
            continue
        direct = [child for child in parent.find_all(recursive=False) if child is not heading]
        for node in direct:
            if getattr(node, "name", "") not in {"p", "div"}:
                continue
            text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            if text and text not in seen_text:
                candidates.append((len(text), text, "yapo_detail_section_description"))
                seen_text.add(text)
    for selector, source in selectors:
        for node in soup.select(selector):
            text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            if text and text not in seen_text:
                item = (len(text), text, source)
                (article_candidates if source == "article" else candidates).append(item)
                seen_text.add(text)
    product = _jsonld_product_details(soup)
    jsonld_description = re.sub(r"\s+", " ", str(product.get("description") or "")).strip()
    if jsonld_description and jsonld_description not in seen_text:
        candidates.append((len(jsonld_description), jsonld_description, "yapo_product_jsonld_description"))
        seen_text.add(jsonld_description)
    if not candidates:
        candidates = article_candidates
    if candidates:
        # Some Yapo templates include a short teaser and a full expanded body.
        # Keep the longest actual description node and record candidate sources.
        _, full_text, full_source = max(candidates, key=lambda item: item[0])
        return full_text, full_source, "", [
            {"source": source, "length": length}
            for length, _text, source in sorted(candidates, reverse=True)
        ]
    if jsonld_description:
        return jsonld_description, "yapo_product_jsonld_description", "", [
            {"source": "yapo_product_jsonld_description", "length": len(jsonld_description)}
        ]
    meta_desc = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        # Meta description is commonly a short search snippet. Keep it as a
        # separate diagnostic value; never report it as the listing's full text.
        return "", "missing_full_description", str(meta_desc["content"]).strip(), [
            {"source": "meta_description", "length": len(str(meta_desc["content"]).strip()), "role": "summary_only"}
        ]
    return "", "missing_full_description", "", []


def _is_source_truncated(description: str, source: str, soup: Any) -> bool:
    text = description.strip().lower()
    if not text:
        return False
    if re.search(r"(?:\.{3}|…|ver\s+m[aá]s|continuar\s+leyendo|leer\s+m[aá]s|mostrar\s+m[aá]s)\s*$", text):
        return True
    if source == "yapo_detail_section_description":
        for heading in soup.select("main h2") if soup is not None else []:
            if _commune_key(heading.get_text(" ", strip=True)) == "descripcion":
                section = heading.parent
                if section is not None:
                    if section.select_one("[aria-expanded='false'], [class*='line-clamp']"):
                        return True
                    if any(_commune_key(button.get_text(" ", strip=True)) in {
                        "ver-mas", "continuar-leyendo", "leer-mas", "mostrar-mas",
                    } for button in section.select("button, a[role='button']")):
                        return True
        return False
    if source == "yapo_product_jsonld_description":
        return False
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
    # Yapo's current JSON-LD uses Product -> offers -> availableAtOrFrom -> Place
    # -> PostalAddress rather than a RealEstateListing top-level type.
    product = _jsonld_product_details(soup)
    offers = product.get("offers") if isinstance(product, dict) else None
    place = offers.get("availableAtOrFrom") if isinstance(offers, dict) else None
    address = place.get("address") if isinstance(place, dict) else None
    if isinstance(address, dict):
        address_types = address.get("@type", [])
        address_types = address_types if isinstance(address_types, list) else [address_types]
        if any(str(value).rstrip("/").rsplit("/", 1)[-1].lower() == "postaladdress" for value in address_types):
            locality = address.get("addressLocality")
            if isinstance(locality, str) and locality.strip():
                found.append(locality.strip())
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
    header = _yapo_detail_header(soup)
    header_locality = ""
    header_location = str(header.get("location") or "").strip()
    if header_location:
        parts = [part.strip() for part in header_location.split(",") if part.strip()]
        if len(parts) >= 2:
            header_locality = parts[-1]
    structured_keys = {_commune_identity(value) for value in structured if _commune_identity(value)}
    explicit_key = _commune_identity(explicit)
    header_key = _commune_identity(header_locality)
    if explicit and (
        (structured_keys and structured_keys != {explicit_key})
        or (header_key and header_key != explicit_key)
    ):
        return {
            "comuna": explicit,
            "comuna_source": "explicit_attribute",
            "comuna_evidence": {"status": "pending_ambiguous", "structured_address_localities": structured, "header_location": header_location},
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
    if header_locality:
        if structured_keys and structured_keys != {header_key}:
            return {
                "comuna": "",
                "comuna_source": "yapo_detail_header_location",
                "comuna_evidence": {"status": "pending_ambiguous", "structured_address_localities": structured, "header_location": header_location},
            }
        return {
            "comuna": header_locality,
            "comuna_source": "yapo_detail_header_location",
            "comuna_evidence": {"status": "confirmed_detail", "structured_address_localities": structured, "header_location": header_location},
        }
    return {"comuna": "", "comuna_source": "", "comuna_evidence": {"status": "pending"}}


def _yapo_detail_header(soup: Any) -> dict[str, str]:
    """Read the current Yapo detail header without borrowing carousel cards."""
    heading = soup.find("h1")
    if heading is None:
        return {"price": "", "location": ""}

    def is_card(node: Any) -> bool:
        return node.find_parent(class_=re.compile(r"card_(?:content|layout|price|details)")) is not None

    found = {"price": "", "location": ""}
    for container in heading.parents:
        if getattr(container, "name", None) in {"body", "html"}:
            break
        if not found["price"]:
            for node in container.select("div[class*='text-2xl'], div[class*='text-[24px]']"):
                if is_card(node):
                    continue
                candidate = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
                if re.search(r"(?:\bUF\b|\bCLF\b|\$)\s*[\d.]+(?:,\d+)?", candidate, re.I):
                    found["price"] = candidate
                    break
        if not found["location"]:
            for node in container.select("span, p"):
                if is_card(node):
                    continue
                candidate = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
                if re.fullmatch(r"[^,]{2,},\s*[^,]{2,}", candidate) and re.search(r"\bRegi[oó]n\b", candidate, re.I):
                    found["location"] = candidate
                    break
        if found["price"] and found["location"]:
            return found
    return found


def extract_listing_fields(
    html: str, source_url: str = "", *, parser: str = "html.parser",
    include_images: bool = True, include_listing_details: bool = True,
) -> dict[str, Any]:
    if BeautifulSoup is None:
        text = _strip_html(html)
        title_match = re.search(r"<h1\b[^>]*>(.*?)</h1>|<title\b[^>]*>(.*?)</title>", html, flags=re.I | re.S)
        title = _strip_html(next((group for group in title_match.groups() if group), "")) if title_match else ""
        if not title:
            meta_title = re.search(r"<meta\b[^>]*(?:property|name)=[\"'](?:og:title|title)[\"'][^>]*content=[\"']([^\"']*)[\"']", html, flags=re.I | re.S)
            title = unescape(meta_title.group(1)).strip() if meta_title else ""
        title = title or ""
        return {
            "source_url": source_url,
            "title": title,
            "title_source": "html_heading_or_title" if title else "missing",
            "title_original_len": len(title),
            "title_is_truncated": False,
            "price": "",
            "description": text,
            "descripcion": text,
            "description_summary": "",
            "descripcion_len": len(text),
            "descripcion_source": "html_text_fallback",
            "descripcion_is_truncated": False,
            "description_truncation_reason": "",
            "description_sources": [],
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
            "publisher_more_ads": extract_yapo_publisher_more_ads(html),
        }

    soup = BeautifulSoup(html, parser)  # type: ignore[misc]
    product_jsonld = _jsonld_product_details(soup)
    title, title_source = _first_text_with_source(soup, ["h1", ".d3-property-details__title", "[data-qa='title']", "title"])
    if not title:
        meta_title = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "title"})
        if meta_title and meta_title.get("content"):
            title = meta_title["content"].strip()
            title_source = "meta_title"

    offers = product_jsonld.get("offers") if isinstance(product_jsonld.get("offers"), dict) else {}
    price_value = str(offers.get("price") or "").strip()
    price_currency = str(offers.get("priceCurrency") or "").strip()
    price = f"{price_value} {price_currency}".strip() if price_value else _first_text(
        soup, ["[data-qa='price']", ".d3-property-info__price", ".d3-property-insight__attribute-value", ".price", ".item-price", "strong.price", "span.price"]
    )
    detail_header = _yapo_detail_header(soup)
    if not price and detail_header["price"]:
        price = detail_header["price"]
        amount_match = re.search(r"(?:\bUF\b|\bCLF\b|\$)\s*([\d.]+(?:,\d+)?)", price, re.I)
        price_value = amount_match.group(1) if amount_match else ""
        price_currency = "CLP" if "$" in price else "UF" if re.search(r"\b(?:UF|CLF)\b", price, re.I) else ""
    description, description_source, description_summary, description_sources = _description_from_soup(soup)

    publisher_scopes = _publisher_scopes(soup)
    seller_name, seller_name_selector = _scoped_first_text(
        publisher_scopes,
        ["[data-qa='seller-name']", "[data-qa='user-name']", ".seller-name", ".profile-name", ".contact-name", ".contact_name"],
    )
    seller_text = _strip_html(" ".join(node.get_text(" ", strip=True) for node in publisher_scopes))

    publicador_visible, publicador_selector = _publicador_visible(publisher_scopes)
    current_publisher, current_publisher_logo = _current_yapo_publisher(soup)
    if not publicador_visible and current_publisher:
        publicador_visible = current_publisher
        publicador_selector = "yapo_published_by_heading"
    seller_jsonld_name, seller_jsonld_source = _jsonld_seller_name(soup)
    contact_name = publicador_visible or seller_name or seller_jsonld_name
    contact_logo_alt, logo_source = _contact_logo_alt(publisher_scopes)
    if not contact_logo_alt and current_publisher_logo:
        contact_logo_alt, logo_source = current_publisher_logo, "yapo_publisher_card_logo_alt"
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
    listing_id_match = re.search(r"/(\d{6,})(?:/|$)", urlsplit(source_url).path)
    current_listing_id = listing_id_match.group(1) if listing_id_match else ""
    publisher_more_ads = extract_yapo_publisher_more_ads(
        soup,
        publisher_name=publicador_visible,
        current_listing_id=current_listing_id,
        seller_profile_id=seller_profile_id,
        parser=parser,
    )
    profile_account_type, profile_type_source = _publisher_profile_account_type(soup, seller_profile_id)
    if not seller_type and profile_account_type:
        seller_type = profile_account_type
        seller_type_selector = profile_type_source
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
    if include_images:
        for img in soup.select("img"):
            for attr in ("src", "data-src", "data-original", "data-lazy"):
                value = img.get(attr)
                if value:
                    full = urljoin(source_url or "", value)
                    if full not in images:
                        images.append(full)

    attributes: dict[str, str] = {}
    if include_listing_details:
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

        listing_advertiser_type, listing_advertiser_type_source = _listing_advertiser_type(soup)
        if listing_advertiser_type:
            attributes.setdefault("tipo de anunciante", listing_advertiser_type)
    else:
        listing_advertiser_type, listing_advertiser_type_source = "", ""

    commune = extract_listing_commune(soup, attributes) if include_listing_details else {
        "comuna": "", "comuna_source": "", "comuna_evidence": {"status": "pending"},
    }

    body_text = soup.get_text(" ", strip=True) if include_listing_details else ""
    canonical = soup.find("link", attrs={"rel": "canonical"})
    canonical_url = canonical.get("href", "").strip() if canonical else source_url

    descripcion_is_truncated = bool(description_summary) or _is_source_truncated(description, description_source, soup)

    return {
        "source_url": source_url,
        "title": title,
        "title_source": title_source or "missing",
        "title_original_len": len(title),
        "title_is_truncated": any(hint in title.lower() for hint in DESCRIPTION_TRUNCATION_HINTS),
        "price": price,
        "price_value": price_value,
        "price_currency": price_currency,
        "description": description,
        "descripcion": description,
        "description_summary": description_summary,
        "descripcion_len": len(description),
        "descripcion_source": description_source,
        "descripcion_is_truncated": descripcion_is_truncated,
        "description_truncation_reason": (
            "summary_only" if not description and description_summary else
            "portal_truncation_marker_or_expand_control" if descripcion_is_truncated else ""
        ),
        "description_sources": description_sources,
        "publicador_visible": publicador_visible,
        "contact_name": contact_name,
        "contact_logo_alt": contact_logo_alt,
        "seller_type": seller_type,
        "listing_advertiser_type": listing_advertiser_type,
        "listing_advertiser_type_source": listing_advertiser_type_source,
        "listing_advertiser": listing_advertiser,
        "seller_jsonld_name": seller_jsonld_name,
        "contact_badges_text": contact_badges_text,
        "seller_name": seller_name,
        "seller_text": seller_text,
        "seller_profile_id": seller_profile_id,
        "seller_profile_url": str(profile_link.get("href") or "") if profile_link else "",
        "publisher_field_provenance": provenance,
        "publisher_more_ads": publisher_more_ads,
        "images": images,
        "attributes": attributes,
        **commune,
        "body_text": re.sub(r"\s+", " ", body_text).strip(),
        "canonical_url": canonical_url,
    }
