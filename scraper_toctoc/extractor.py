from __future__ import annotations

import json
import re
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse


UI_IMAGE_PATTERNS = (
    "thumbs_up", "check.png", "1x1", "banner", "logo", "icon",
    "sprite", "avatar", "placeholder", "loading", "social",
    "favicon", "marker", "pin", "mapbox", "recaptcha",
    "facebook.com/tr", "google-analytics", "googletagmanager",
    "pixel", "tracking", "beacon",
)
PHOTO_IMAGE_PATTERNS = ("/toctoc/fotos/",)


def fetch_gallery_images(
    html: str,
    source_url: str,
    *,
    session: Any = None,
    timeout: int = 15,
) -> list[str]:
    """Fetch Toctoc's lazy-loaded gallery without re-downloading the listing.

    The detail HTML contains a short-lived ``hashTocToc`` token while the
    actual property photos are served by a separate gallery endpoint.
    """
    token_match = re.search(r'"hashTocToc"\s*:\s*"([^"]+)"', html or "")
    listing_match = re.search(r"/(\d+)(?:[/?#]|$)", source_url or "")
    if not token_match or not listing_match:
        return []

    try:
        import requests
        http = session or requests.Session()
        response = http.get(
            f"https://www.toctoc.com/gwtt/galeria/{listing_match.group(1)}/usado",
            headers={
                "Authorization": f"Bearer {token_match.group(1)}",
                "Accept": "application/json",
                "Referer": source_url,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
                ),
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json() or {}
    except Exception:
        return []

    images = []
    for item in ((payload.get("propiedad") or {}).get("imagenes") or []):
        if not isinstance(item, dict):
            continue
        base = str(item.get("ruta") or "")
        name = str(item.get("nombreTamanoReal") or "")
        url = f"{base}n_wm_{name}" if base and name else ""
        if url and _is_real_property_image(url) and url not in images:
            images.append(url)
    return images[:20]


def _is_real_property_image(src: str) -> bool:
    low = src.lower()
    if any(pat in low for pat in UI_IMAGE_PATTERNS):
        return False
    if src.endswith(".svg") or src.endswith(".gif"):
        return False
    if any(pat in low for pat in PHOTO_IMAGE_PATTERNS):
        return True
    return True  # accept if no UI pattern matched

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

from discovery import _extract_next_data, extract_metadata_from_next_data


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
            data = json.loads(raw)
        except Exception:
            continue
        nodes = data if isinstance(data, list) else [data]
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


def _publicador_visible(soup: Any, html: str) -> tuple[str, str]:
    status = "NOT_FOUND"
    contacto = soup.select_one("div.cf-contacto")
    if contacto:
        name_node = contacto.select_one(".contacto-anunciante .info-anunciante li.titulo strong")
        if name_node:
            text = name_node.get_text(" ", strip=True)
            text = re.sub(r"^Anunciante\s*-\s*", "", text, flags=re.I).strip()
            if text and text.lower() != "anunciante":
                return text, "info-anunciante"
        link_node = contacto.select_one(".contacto-anunciante .info-anunciante li a")
        if link_node:
            text = link_node.get_text(" ", strip=True)
            if text:
                return text, "info-anunciante-link"
        logo_img = contacto.select_one("div.contenedor-logo img")
        if logo_img and logo_img.get("alt"):
            return str(logo_img["alt"]).strip(), "logo_alt"
    candidates = [
        ".seller-name", ".contact-name", ".publicador", ".publisher",
        ".profile-name", "[data-qa='seller-name']", "[class*='seller']",
        "[class*='publisher']", "[class*='publicador']", "[class*='corredor']",
        "[class*='inmobiliaria']", "[class*='operador']",
    ]
    text = _first_text(soup, candidates)
    if text:
        return text, "dom_css"
    jn = _jsonld_name(soup)
    if jn:
        return jn, "json_ld"
    for keyword in ["anunciante", "publicado por", "corredora", "particular", "inmobiliaria"]:
        if keyword in html.lower()[:20000]:
            status = "PRESENT_BUT_NOT_PARSED"
            break
    if "Anunciante" in html or "Particular" in html:
        status = "PRESENT_IN_HTML"
    if status == "NOT_FOUND" and ("contacto" in html.lower() or "contact" in html.lower()):
        idx = html.lower().find("contacto") if "contacto" in html.lower() else html.lower().find("contact")
        context = html[max(0,idx-50):idx+200] if idx >= 0 else ""
        if "cf-contacto" not in context and "btn" in context:
            status = "LOADED_DYNAMICALLY"
    return "", status


def _seller_type(soup: Any) -> dict[str, Any]:
    result = {"seller_type": "DESCONOCIDO", "seller_type_source": "", "seller_type_evidence": ""}
    contacto = soup.select_one("div.cf-contacto")
    if contacto:
        titulo = contacto.select_one(".contacto-anunciante .info-anunciante li.titulo strong")
        if titulo:
            text = titulo.get_text(" ", strip=True).strip()
            evidence = text
            text_lower = text.lower()
            if "particular" in text_lower:
                result.update({"seller_type": "PARTICULAR", "seller_type_source": "info-anunciante", "seller_type_evidence": evidence})
            else:
                result.update({"seller_type": "EMPRESA", "seller_type_source": "info-anunciante", "seller_type_evidence": evidence})
        logo = contacto.select_one("div.contenedor-logo img")
        if logo and logo.get("alt") and result["seller_type"] == "DESCONOCIDO":
            result.update({"seller_type": "EMPRESA", "seller_type_source": "logo_alt", "seller_type_evidence": str(logo.get("alt",""))})
        link = contacto.select_one(".contacto-anunciante .info-anunciante li a")
        if link and link.get("href") and "/inmobiliarias/" in link.get("href","").lower():
            result.update({"seller_type": "EMPRESA", "seller_type_source": "link_inmobiliaria", "seller_type_evidence": link.get("href","")})
    return result


def _collect_all_text(soup: Any, selector: str) -> str:
    """Collect text from ALL matching nodes, not just the first."""
    parts = []
    for node in soup.select(selector):
        text = node.get_text(" ", strip=True)
        if text:
            parts.append(re.sub(r"\s+", " ", text).strip())
    return " ".join(parts)


def _description_from_soup(soup: Any) -> tuple[str, str]:
    # Priority 1: full container c-texto (contains complete description including hidden text)
    full = _collect_all_text(soup, ".c-texto")
    if full and len(full) > 50:
        return full, "c_texto_container"
    
    # Priority 2: individual selectors
    selectors = [
        ("p.text-justify.texto", "toctoc_texto"),
        (".text-justify.texto", "toctoc_texto_class"),
        ("[data-qa='description']", "data_qa_description"),
        (".description", "class_description"),
        ("#description", "id_description"),
        (".property-description", "property_description"),
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


def _extract_from_next_data(next_data: dict[str, Any], source_url: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    records = extract_metadata_from_next_data(next_data)

    props = next_data.get("props", {}).get("pageProps", {})
    propiedades = props.get("propiedades", {})
    if isinstance(propiedades, dict):
        total = propiedades.get("total", 0)
        if total:
            fields["total_results"] = total

    for rec in records:
        if not source_url or rec["url"] in source_url or source_url.endswith(rec["listing_id"]):
            for k, v in rec.items():
                if k in ("url", "source_search_url", "source_page_url", "page_number", "discovered_at", "batch_id", "url_format"):
                    continue
                if v is not None and v != "":
                    fields[k] = v
            break

    if not fields and records:
        rec = records[0]
        for k, v in rec.items():
            if k in ("url", "source_search_url", "source_page_url", "page_number", "discovered_at", "batch_id", "url_format"):
                continue
            if v is not None and v != "":
                fields[k] = v

    return fields


def _extract_comuna_region_from_url(source_url: str) -> dict[str, str]:
    result: dict[str, str] = {}
    if not source_url:
        return result
    low = source_url.lower()
    # Pattern: /propiedades/compranuevo/{tipo}/{comuna}/{slug}/{id}
    m = re.search(r'/propiedades/compranuevo/[^/]+/([^/]+)/([^/]+)/\d+$', low)
    if m:
        result["comuna"] = m.group(1).replace("-", " ").title()
        slug = m.group(2)
        if any(r in low for r in ("metropolitana", "valparaiso", "biobio")):
            result["region"] = "Metropolitana" if "metropolitana" in low else "Valparaiso" if "valparaiso" in low else "Biobio"
        return result
    # Pattern: /propiedad/{slug}-{id} where comuna might be in the slug
    m2 = re.search(r'/propiedad/[^/]+-(\d+)$', low)
    if m2:
        slug_part = low.split("/propiedad/")[1].rsplit("-", 1)[0] if "/propiedad/" in low else ""
        for comuna in ("la-florida", "santiago", "las-condes", "providencia", "nunoa", "vitacura", "lo-barnechea", "maipu", "puente-alto", "la-reina", "penalolen", "macul", "san-miguel"):
            if comuna in slug_part:
                result["comuna"] = comuna.replace("-", " ").title()
                break
        if "metropolitana" in low:
            result["region"] = "Metropolitana"
    if not result.get("region"):
        path = urlparse(source_url).path.lower()
        path_segments = [s for s in path.split("/") if s]
        region_candidates = {"metropolitana": "Metropolitana", "valparaiso": "Valparaiso", "biobio": "Biobio",
                             "araucania": "Araucania", "los-lagos": "Los Lagos", "coquimbo": "Coquimbo",
                             "los-rios": "Los Rios", "antofagasta": "Antofagasta", "tarapaca": "Tarapaca",
                             "maule": "Maule", "nuble": "Nuble", "ohiggins": "O'Higgins"}
        for keyword, region_name in region_candidates.items():
            if keyword in path_segments:
                result["region"] = region_name
                break
    return result


def _extract_detail_from_og(soup: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    og = (
        soup.find("meta", attrs={"property": "og:title"}) or
        soup.find("meta", attrs={"name": "twitter:title"})
    )
    if og and og.get("content"):
        og_text = og["content"].lower()
        for comuna in ("la florida", "santiago", "las condes", "providencia", "nunoa", "vitacura", "lo barnechea", "maipu", "puente alto"):
            if comuna in og_text:
                result["comuna"] = comuna.title()
                break
        for region in ("metropolitana", "valparaiso", "biobio", "araucania"):
            if region in og_text:
                result["region"] = region.title()
                break
    return result


def extract_listing_fields(html: str, source_url: str = "") -> dict[str, Any]:
    next_data = _extract_next_data(html)
    next_fields = _extract_from_next_data(next_data, source_url) if next_data else {}
    url_fields = _extract_comuna_region_from_url(source_url)

    if BeautifulSoup is None:
        text = _strip_html(html)
        return {"source_url": source_url, "title": text[:120], "price_uf": "", "price_clp": "", "description": text,
                "descripcion": text, "descripcion_len": len(text), "descripcion_source": "html_text_fallback",
                "descripcion_is_truncated": False, "publicador_visible": "", "contact_name": "",
                "seller_type": "", "images": [], "attributes": {}, "body_text": text,
                "canonical_url": source_url, **url_fields, **next_fields}

    soup = BeautifulSoup(html, "html.parser")

    header_h1 = soup.select_one("h1.tipo.nv")
    title = ""
    operacion = ""
    if header_h1:
        title = header_h1.get_text(" ", strip=True)
        strong = header_h1.select_one("strong")
        if strong:
            op_text = strong.get_text(" ", strip=True).lower()
            if "venta" in op_text:
                operacion = "venta"
            elif "arriendo" in op_text:
                operacion = "arriendo"
            title = re.sub(r"\s+", " ", title)
    if not title:
        meta_title = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "title"})
        title = (next_fields.get("title") or _first_text(soup, ["h1", "title"]) or
                 (meta_title.get("content", "").strip() if meta_title else ""))

    price_uf = _first_text(soup, ["p.precio-uf", ".precio-uf", "[class*='precio-uf']"])
    price_clp = _first_text(soup, ["p.precio-alt", ".precio-alt", "[class*='precio-alt']"])

    # Canonical description: prefer NextData (complete), then DOM selectors, then fallback
    next_desc = next_fields.get("description") or next_fields.get("descripcion") or ""
    soup_desc, soup_source = _description_from_soup(soup)
    
    # Check if description might be truncated
    has_expand_control = bool(re.search(r'(?:leer\s*m[áa]s|ver\s*m[áa]s|leer\s*informaci[óo]n)', html[:20000], re.I))
    
    if next_desc and len(next_desc) >= len(soup_desc):
        description = next_desc
        description_source = "next_data"
        desc_truncated = False
    elif soup_desc:
        description = soup_desc
        description_source = soup_source
        # description is truncated ONLY if expand control exists AND the full .c-texto was NOT used
        if soup_source == "c_texto_container" and has_expand_control:
            desc_truncated = False  # c-texto already contains the expanded text
        elif has_expand_control:
            desc_truncated = True  # expand exists but we didn't get c-texto
        else:
            desc_truncated = False
    else:
        description = _strip_html(html)
        description_source = "html_text_fallback"
        desc_truncated = False

    pub_name, pub_status = _publicador_visible(soup, html)
    publicador_visible = next_fields.get("publicador_visible") or pub_name
    publisher_extraction_status = pub_status if not pub_name else "FOUND"
    seller_info = _seller_type(soup)
    seller_type_val = seller_info.get("seller_type", "DESCONOCIDO")
    seller_type_source = seller_info.get("seller_type_source", "")
    seller_type_evidence = seller_info.get("seller_type_evidence", "")
    seller_jsonld_name = _jsonld_name(soup)
    contact_name = publicador_visible or seller_jsonld_name

    images: list[str] = next_fields.get("images", [])
    if isinstance(images, str):
        images = [images]
    gallery_selectors = "img[alt='img galería'], img[alt='img galeria'], [class*='galeria'] img, [class*='gallery'] img, [class*='slider'] img"
    if not images:
        for img in soup.select(gallery_selectors):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy") or ""
            if src:
                full = urljoin(source_url or "", src)
                if _is_real_property_image(full) and full not in images:
                    images.append(full)
    if len(images) < 3:
        for img in soup.select("img[src*='toctoc/fotos/'], img[data-src*='toctoc/fotos/']"):
            src = img.get("src") or img.get("data-src") or ""
            if src:
                full = urljoin(source_url or "", src)
                if _is_real_property_image(full) and full not in images:
                    images.append(full)
    if not images:
        for img in soup.select("img"):
            for attr in ("src", "data-src", "data-original", "data-lazy"):
                value = img.get(attr)
                if value:
                    full = urljoin(source_url or "", value)
                    if _is_real_property_image(full) and full not in images:
                        images.append(full)
    images = [img for img in images if _is_real_property_image(img)]

    attributes: dict[str, str] = {}

    programa_ficha = re.search(r'"programaFicha":\s*({[^}]+})', html, re.I)
    if programa_ficha:
        try:
            pf = json.loads(programa_ficha.group(1))
            for src_key, dst_key in [("dormitorios","dormitorios"), ("banos","baños"), ("minDormitorios","dormitorios_min"), ("maxDormitorios","dormitorios_max"), ("minBanos","baños_min"), ("maxBanos","baños_max"), ("superficieUtilDesde","superficie útil"), ("superficieTerreno","superficie terreno"), ("superficieConstruida","superficie construida")]:
                val = pf.get(src_key)
                if val is not None and val != "":
                    attributes[dst_key] = str(val)
        except Exception: pass

    for node in soup.select(".f-programa-text, table tr, li, [data-qa], [class*='attribute'], [class*='caracteristica']"):
        text = node.get_text(" ", strip=True)
        if ":" not in text:
            continue
        parts = text.split(":", 1)
        key = re.sub(r"\s+", " ", parts[0]).strip().lower()
        value = re.sub(r"\s+", " ", parts[1]).strip()
        if key and value and len(key) < 40 and len(value) < 60 and key not in attributes:
            attributes[key] = value

    body_text = soup.get_text(" ", strip=True) if soup else _strip_html(html)
    canonical = soup.find("link", attrs={"rel": "canonical"}) if soup else None
    canonical_url = canonical.get("href", "").strip() if canonical else source_url

    if not next_fields.get("comuna") and not url_fields.get("comuna"):
        og_fields = _extract_detail_from_og(soup)
        url_fields.update(og_fields)

    price = f"{price_uf} / {price_clp}".strip(" / ")

    def _parse_range(val):
        if not val or val == "N/A": return None, None, None
        s = str(val).strip()
        m = re.search(r'(\d+[,.]?\d*)\s*a\s*(\d+[,.]?\d*)', s)
        if m:
            return s, m.group(1), m.group(2)
        m = re.search(r'(\d+[,.]?\d*)', s)
        if m:
            return s, m.group(1), None
        return s, None, None

    def _parse_attribute(attr_keywords, next_val):
        raw = None
        if next_val is not None and str(next_val).strip() not in ("", "N/A"):
            raw = str(next_val).strip()
        else:
            for key in attr_keywords:
                for ak, av in attributes.items():
                    if key in ak:
                        raw = str(av).strip()
                        break
                if raw: break
        if raw:
            full, lo, hi = _parse_range(raw)
            return full, lo, hi
        return None, None, None

    dorm_raw, dorm_lo, dorm_hi = _parse_attribute(("dormitorios", "dorm", "habitacion"), next_fields.get("dormitorios"))
    ban_raw, ban_lo, ban_hi = _parse_attribute(("baños", "banos", "bano", "bannos"), next_fields.get("banos"))
    sup_raw, sup_lo, sup_hi = _parse_attribute(("superficie total", "superficie", "area"), next_fields.get("superficie_total"))
    sup_util_raw, _, _ = _parse_attribute(("superficie útil", "superficie util"), None)
    estac_raw, _, _ = _parse_attribute(("estacionamientos", "estacionamiento"), None)
    bodega_raw, _, _ = _parse_attribute(("bodegas", "bodega"), None)

    comuna_source = "discovery" if next_fields.get("comuna") else "url" if url_fields.get("comuna") else "detail_html"
    region_source = "discovery" if next_fields.get("region") else "url" if url_fields.get("region") else "detail_html"
    comuna_val = next_fields.get("comuna") or url_fields.get("comuna") or ""
    region_val = next_fields.get("region") or url_fields.get("region") or ""

    location_conflict = False
    location_reason = ""
    known_regions = {"Metropolitana", "Valparaiso", "Biobio", "Araucania", "Los Lagos", "Coquimbo",
                     "Antofagasta", "Tarapaca", "Los Rios", "Maule", "Nuble", "O'Higgins", "Aysen", "Magallanes"}
    known_communes_metropolitana = {"La Florida", "Santiago", "Las Condes", "Providencia", "Nunoa", "Vitacura",
                                     "Lo Barnechea", "Maipu", "Puente Alto", "La Reina", "Penalolen", "Macul",
                                     "San Miguel", "Conchali", "Renca", "Recoleta", "Quilicura", "El Bosque"}
    comuna_lower = comuna_val.lower().strip()
    region_lower = region_val.lower().strip()
    if comuna_lower in {c.lower() for c in known_communes_metropolitana}:
        if region_lower not in ("", "metropolitana"):
            if region_val and region_val != "Metropolitana":
                location_conflict = True
                location_reason = f"comuna '{comuna_val}' sugiere Metropolitana, pero region es '{region_val}'"
                region_val = "Metropolitana"
                region_source += "_corrected"
        else:
            region_val = "Metropolitana"
            region_source += "_inferred_from_comuna"
            location_reason = f"region inferida desde comuna '{comuna_val}'"
    if region_val and region_val not in known_regions:
        location_reason = f"region desconocida: {region_val}"

    return {
        "source_url": source_url, "title": title, "price": price, "price_uf": price_uf, "price_clp": price_clp,
        "operacion": operacion or next_fields.get("operacion", url_fields.get("operacion", "")),
        "description": description, "descripcion": description,
        "descripcion_len": len(description), "descripcion_source": description_source,
        "descripcion_is_truncated": desc_truncated if 'desc_truncated' in dir() else False,
        "publicador_visible": publicador_visible or None,
        "publisher_extraction_status": publisher_extraction_status,
        "contact_name": contact_name,
        "seller_jsonld_name": seller_jsonld_name,
        "seller_type": seller_type_val, "seller_type_source": seller_type_source, "seller_type_evidence": seller_type_evidence,
        "listing_advertiser": next_fields.get("listing_advertiser", ""),
        "dormitorios": dorm_lo, "dormitorios_min": dorm_lo, "dormitorios_max": dorm_hi, "dormitorios_raw": dorm_raw,
        "banos": ban_lo, "banos_min": ban_lo, "banos_max": ban_hi, "banos_raw": ban_raw,
        "superficie_total": sup_raw, "superficie_util": sup_util_raw,
        "estacionamientos": estac_raw, "bodegas": bodega_raw,
        "images": images[:20], "attributes": attributes,
        "body_text": re.sub(r"\s+", " ", body_text).strip(),
        "canonical_url": canonical_url,
        "comuna": comuna_val, "comuna_source": comuna_source,
        "region": region_val, "region_source": region_source,
        "location_validation": {"status": "CONFLICT" if location_conflict else "VALID", "reason": location_reason},
        "listing_id_source": next_fields.get("listing_id_source", "discovery"),
        **url_fields, **next_fields,
    }
