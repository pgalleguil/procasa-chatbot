from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

try:
    from .config import AppConfig
except ImportError:  # pragma: no cover
    from config import AppConfig
try:
    from .proxy_manager import ProxyManager
except ImportError:  # pragma: no cover
    from proxy_manager import ProxyManager

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


class DiscoveryDegradedError(RuntimeError):
    """Raised before processing when Toctoc discovery is not trustworthy."""


def _utcnow(): return datetime.now(timezone.utc).isoformat()


def _normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    return urlunparse(parsed._replace(fragment=""))


def build_ssr_search_url(
    config,
    operacion="venta",
    tipo="departamento",
    region="metropolitana",
    comuna="la-florida",
    pagina=1,
    estado=None,
    publicador=None,
    precio_desde=None,
    precio_hasta=None,
):
    route_commune = "santiago" if comuna == "santiago-centro" else comuna
    url = config.search_ssr_template.format(operacion=operacion, tipo=tipo, region=region, comuna=route_commune)
    query = {"pagina": str(pagina)}
    if estado is not None:
        query["estado"] = str(estado)
    if publicador is not None:
        query["publicador"] = str(publicador)
    if precio_desde is not None:
        query["precioDesde"] = str(precio_desde)
    if precio_hasta is not None:
        query["precioHasta"] = str(precio_hasta)
    return f"{url}?{urlencode(query)}"


def _search_route_commune(comuna: str) -> str:
    """Map commercial preference slugs to Toctoc search route slugs."""
    return "santiago" if comuna == "santiago-centro" else comuna


BUILD_SEARCH_URL_FILTER_VALUES = {
    "estado": {"0": "todos", "1": "nuevo", "2": "usado"},
    "publicador": {"0": "todos", "1": "profesional", "2": "particular"},
}
ALLOWED_SEARCH_PARAMS = {"moneda", "precioDesde", "precioHasta", "pagina", "estado", "publicador", "temporalidad", "texto"}


def _validate_search_query_params(query_params: dict[str, str]) -> list[str]:
    """Check for unexpected or forbidden parameters."""
    warnings: list[str] = []
    for key in query_params:
        if key not in ALLOWED_SEARCH_PARAMS:
            warnings.append(f"UNEXPECTED_SEARCH_FILTER: param={key!r}")
    return warnings


def build_search_url(
    config,
    operacion="compra",
    tipo="departamento",
    region="metropolitana",
    comuna="la-florida",
    pagina=1,
    estado=None,
    publicador=None,
    precio_desde=None,
    precio_hasta=None,
    temporalidad=None,
) -> dict:
    """
    Build a controlled Toctoc SPA search URL with only explicitly requested filters.

    Returns {"url": str, "effective_query_parameters": dict, "requested_filters": dict, "warnings": list[str]}.
    The caller must check warnings and abort on UNEXPECTED_SEARCH_FILTER.
    """
    qp = {"moneda": "2"}
    qp["pagina"] = str(pagina)
    # texto is intentionally omitted; empty value may break SPA rendering
    if precio_desde is not None:
        qp["precioDesde"] = str(precio_desde)
    if precio_hasta is not None:
        qp["precioHasta"] = str(precio_hasta)
    if estado is not None:
        qp["estado"] = str(estado)
    if publicador is not None:
        qp["publicador"] = str(publicador)
    if temporalidad is not None:
        qp["temporalidad"] = str(temporalidad)

    base = f"{config.base_url}/resultados/lista/{operacion}/{tipo}/{region}/{comuna}/"
    qs = "&".join(f"{k}={v}" for k, v in sorted(qp.items()) if v is not None)
    url = f"{base}?{qs}" if qs else base.rstrip("/")

    requested = {
        "operacion": operacion,
        "tipo_propiedad": tipo,
        "region": region,
        "comuna": comuna,
        "pagina": pagina,
        "estado": estado,
        "publicador": publicador,
        "precio_desde": precio_desde,
        "precio_hasta": precio_hasta,
        "temporalidad": temporalidad,
    }
    warnings = _validate_search_query_params(qp)
    return {
        "url": url,
        "effective_query_parameters": dict(qp),
        "requested_filters": requested,
        "warnings": warnings,
    }


def is_listing_detail_url(url: str) -> bool:
    low = url.lower()
    if "/resultados/" in low or "/santander/" in low:
        return False
    if any(key == "o" and value == "menu" for key, value in parse_qsl(urlparse(url).query)):
        return False
    valid_prefixes = ("/propiedades/", "/propiedad/", "/venta/", "/arriendo/")
    if not any(p in low for p in valid_prefixes):
        return False
    path = urlparse(url).path.rstrip("/")
    return bool(
        re.search(r"/\d+$", path)
        or re.search(r"-\d+$", path)
        or re.search(r"/[a-z]_[a-f0-9]{20,}$", path)
        or re.search(r"/[a-f0-9]{20,}$", path)
    )


def listing_id_from_url(url: str) -> tuple[str, str]:
    m = re.search(r"/(\d+)$", url)
    if m: return m.group(1), "url_numeric_id"
    m = re.search(r"-(\d+)$", url)
    if m: return m.group(1), "url_numeric_id"
    m = re.search(r"/[a-z]_([a-f0-9]{20,})$", url, re.I)
    if m: return m.group(1), "url_hash"
    m = re.search(r"/([a-f0-9]{40})$", url, re.I)
    if m: return m.group(1), "url_hash"
    m = re.search(r"/([a-f0-9]{20,})$", url, re.I)
    if m: return m.group(1), "url_hash"
    return "", "not_found"


def listing_id_from_url_fallback(url: str) -> str:
    import hashlib
    return "urlhash_" + hashlib.md5(url.encode("utf-8")).hexdigest()[:16]


def classify_url_format(url: str) -> str:
    if "/propiedades/compranuevo/" in url:
        return "compranuevo"
    if "/propiedades/compracorredorasr/" in url:
        return "compracorredorasr"
    if "/propiedades/compraparticularsr/" in url:
        return "compraparticularsr"
    if "/propiedades/arriendocorredorasr/" in url:
        return "arriendocorredorasr"
    if "/propiedades/arriendoparticularsr/" in url:
        return "arriendoparticularsr"
    if "/propiedades/" in url:
        return "propiedades_otro"
    if "/propiedad/" in url:
        return "propiedad_usado"
    if re.search(r"/(?:venta|arriendo)/[^/]+/[^/]+/[^/]+/[a-z]_[a-f0-9]{20,}$", url, re.I):
        return "public_route"
    return "desconocido"


SKIP_PROFESSIONAL = "SKIP_PROFESSIONAL"
KEEP_OWNER_CANDIDATE = "KEEP_OWNER_CANDIDATE"
KEEP_AMBIGUOUS = "KEEP_AMBIGUOUS"
SKIP_WRONG_COMMUNE = "SKIP_WRONG_COMMUNE"
SKIP_NON_TARGET_TYPE = "SKIP_NON_TARGET_TYPE"

TARGET_PROPERTY_TYPES = frozenset({"casa", "departamento"})

# Property type slugs observed in Toctoc URLs (first token after format/comuna)
NON_TARGET_PROPERTY_SLUGS = frozenset({
    "estacionamiento", "bodega", "oficina", "local-comercial",
    "local", "industrial", "terreno", "parcela", "sitio",
})


def extract_property_type_slug(url: str) -> str:
    """Extract the property type slug from a Toctoc URL.
    Returns the type slug (e.g. 'departamento', 'casa', 'estacionamiento') or ''."""
    low = url.lower()
    # Pattern: /propiedades/<format>/<type>/...
    m = __import__('re').search(r"/propiedades/[^/]+/([^/]+)/", low)
    if m:
        slug = m.group(1).strip("/")
        return slug
    # Pattern: /propiedad/<type>-en-...
    m = __import__('re').search(r"/propiedad/([^-]+)", low)
    if m:
        slug = m.group(1).strip("-")
        slug = slug.replace("-", " ").strip()
        return slug
    return ""


def is_target_property_type(url: str) -> bool:
    """Check if the URL corresponds to a target property type (casa/departamento)."""
    slug = extract_property_type_slug(url)
    if not slug:
        return True  # can't determine, allow through
    if slug in NON_TARGET_PROPERTY_SLUGS:
        return False
    # casa/departamento and anything else we haven't excluded
    return True


PROFESSIONAL_URL_FORMATS = {"compranuevo", "compracorredorasr", "arriendocorredorasr"}
OWNER_CANDIDATE_FORMATS = {"compraparticularsr", "arriendoparticularsr", "propiedad_usado", "propiedades_otro"}


def _normalize_slug(slug: str) -> str:
    """Normalize a commune/region slug for comparison."""
    s = slug.strip().lower()
    # Remove accents
    s = s.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    s = s.replace("ñ", "n").replace("ü", "u")
    # Normalize spaces and dashes
    s = s.replace("-", " ").replace("_", " ")
    s = " ".join(s.split())  # collapse spaces
    return s


def _extract_commune_slug(url: str) -> str:
    """Extract the commune slug from a Toctoc property URL."""
    low = url.lower()
    # Pattern 1: /propiedades/<format>/<tipo>/<comuna>/...
    m = re.search(r"/propiedades/[^/]+/[^/]+/([^/]+)/", low)
    if m:
        return m.group(1).strip("/")
    # Pattern 2: /propiedad/<slug>-<id>
    m = re.search(r"/propiedad/[^/]+-(\d+)$", low)
    if m:
        # Extract comuna from the slug part
        slug_part = low.split("/propiedad/")[1].rsplit("-", 1)[0] if "/propiedad/" in low else ""
        # Common Chilean communes
        for com in ["la-florida", "santiago", "las-condes", "providencia", "nunoa", "vitacura",
                    "lo-barnechea", "maipu", "puente-alto", "la-reina", "penalolen", "macul",
                    "san-miguel", "conchali", "renca", "recoleta", "quilicura", "el-bosque"]:
            if com in slug_part:
                return com
    # Current Toctoc public route: /venta/<tipo>/<region>/<comuna>/<r|b>_<hash>
    m = re.search(r"/(?:venta|arriendo)/[^/]+/[^/]+/([^/]+)/[a-z]_[a-f0-9]{20,}$", low, re.I)
    if m:
        return m.group(1).strip("/")
    return ""


def matches_requested_commune(url: str, requested_commune: str) -> bool:
    """Check if a property URL matches the requested commune.
    Returns True only if the commune slug in the URL matches the requested commune."""
    if not url or not requested_commune:
        return False
    extracted = _extract_commune_slug(url)
    if not extracted:
        return False
    extracted_norm = _normalize_slug(extracted)
    requested_norm = _normalize_slug(requested_commune)
    # Toctoc usa ``santiago`` en las fichas, mientras que el CRM usa
    # ``santiago-centro`` para la preferencia comercial.
    aliases = {
        "santiago-centro": {"santiago", "santiago-centro"},
        "santiago": {"santiago", "santiago-centro"},
    }
    if requested_norm in aliases:
        return extracted_norm in aliases[requested_norm]
    return extracted_norm == requested_norm


def classify_discovery_candidate(url_format: str) -> str:
    """Classify a URL format for discovery pipeline priority.
    
    Returns:
        SKIP_PROFESSIONAL   → professional listing, skip individual download
        KEEP_OWNER_CANDIDATE → potential owner listing, download for classification
        KEEP_AMBIGUOUS      → unknown format, keep for analysis
    """
    if url_format in PROFESSIONAL_URL_FORMATS:
        return SKIP_PROFESSIONAL
    if url_format in OWNER_CANDIDATE_FORMATS:
        return KEEP_OWNER_CANDIDATE
    # "desconocido" or any other format: keep as ambiguous
    return KEEP_AMBIGUOUS


def is_owner_pipeline_eligible(url_format: str) -> tuple[bool, str]:
    """Check if a URL format is eligible for the owner pipeline (non-professional).
    Kept for backward compatibility."""
    decision = classify_discovery_candidate(url_format)
    if decision == SKIP_PROFESSIONAL:
        return False, f"PROFESSIONAL_URL_FORMAT ({url_format})"
    return True, ""


def _extract_next_data(html: str):
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.I | re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _extract_embedded_json(html: str, script_id: str):
    """Parse a JSON script block without requiring a particular frontend."""
    pattern = rf'<script[^>]*id=["\']{re.escape(script_id)}["\'][^>]*>(.*?)</script>'
    match = re.search(pattern, html, re.I | re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1).strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _embedded_script_present(html: str, script_id: str) -> bool:
    return bool(re.search(rf'<script[^>]*id=["\']{re.escape(script_id)}["\'][^>]*>', html, re.I))


def _extract_react_engine_props(html: str):
    """Read the current Toctoc SSR payload (react-engine-props)."""
    return _extract_embedded_json(html, "react-engine-props")


def _iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _first_value(record: dict, *keys: str):
    for key in keys:
        value = record.get(key)
        if value not in (None, "", []):
            return value
    return ""


def _record_from_embedded_object(prop: dict, base_url: str, source: str) -> dict | None:
    url_value = _first_value(
        prop,
        "urlFicha", "url_ficha", "canonical_url", "canonicalUrl", "listing_url",
        "listingUrl", "property_url", "propertyUrl", "detail_url", "detailUrl",
        "href", "link", "url",
    )
    if not isinstance(url_value, str):
        return None
    url = urljoin(base_url, url_value.strip())
    if not is_listing_detail_url(url):
        return None

    listing_id = _first_value(prop, "idProperty", "id_property", "propertyId", "listing_id", "listingId")
    listing_id = str(listing_id) if listing_id not in (None, "", 0, "0") else ""
    parsed_id, id_source = listing_id_from_url(url)
    if not listing_id:
        listing_id, id_source = parsed_id or listing_id_from_url_fallback(url), (id_source if parsed_id else "normalized_url_hash")

    precios = prop.get("precios") or prop.get("prices") or []
    price_uf = ""
    price_clp = ""
    if isinstance(precios, list):
        for price in precios:
            if not isinstance(price, dict):
                continue
            prefix = str(price.get("prefix", price.get("currency", "")))
            value = price.get("value", price.get("amount", ""))
            if str(prefix).upper() == "UF":
                price_uf = f"UF {value}"
            elif value not in (None, ""):
                price_clp = f"$ {value}"

    operation = str(_first_value(prop, "tipoOperacion", "tipo_operacion", "operation", "operation_label"))
    return {
        "url": url,
        "listing_id": listing_id,
        "listing_id_source": id_source,
        "url_format": classify_url_format(url),
        "title": str(_first_value(prop, "titulo", "title", "name")),
        "comuna": str(_first_value(prop, "comuna", "commune", "municipality")),
        "region": str(_first_value(prop, "region", "región")),
        "operacion": "venta" if "venta" in operation.lower() else ("arriendo" if "arriendo" in operation.lower() else ""),
        "tipo_propiedad": str(_first_value(prop, "tipoPropiedad", "tipo_propiedad", "propertyType")).lower(),
        "tipo_operacion": operation,
        "price_uf": price_uf,
        "price_clp": price_clp,
        "dormitorios": None,
        "banos": None,
        "superficie": None,
        "publicador": str(_first_value(prop, "publicador", "publisher", "seller", "clientName")),
        "client_id": str(_first_value(prop, "clientId", "client_id", "seller_client_id")),
        "discovery_source": source,
    }


def extract_metadata_from_embedded_payload(payload: Any, base_url: str, source: str) -> list[dict]:
    """Extract listing metadata from either NextData or react-engine-props.

    The traversal is deliberately schema-tolerant: Toctoc has changed the
    nesting around its result arrays more than once. Only objects containing a
    valid property-detail URL are accepted, so navigation/config URLs cannot
    become discovery candidates.
    """
    records: list[dict] = []
    seen: set[str] = set()
    for item in _iter_dicts(payload):
        record = _record_from_embedded_object(item, base_url, source)
        if not record or record["url"] in seen:
            continue
        seen.add(record["url"])
        records.append(record)
    return records


def _extract_detail_urls_from_html(html: str, base_url: str) -> list[str]:
    """Last-resort extraction of real detail URLs from server-rendered HTML."""
    candidates = re.findall(
        r'(?:https?://[^"\'<>\s]+|/(?:propiedad|propiedades|venta|arriendo)/[^"\'<>\s]+)',
        html,
        re.I,
    )
    urls = []
    seen = set()
    for candidate in candidates:
        url = urljoin(base_url, candidate.rstrip(".,);"))
        if is_listing_detail_url(url) and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _extract_page_records(html: str, base_url: str) -> tuple[list[dict], dict]:
    """Run the discovery adapters in order and return records plus diagnostics."""
    next_data = _extract_next_data(html)
    react_props = _extract_react_engine_props(html)
    diagnostics = {
        "next_data_present": _embedded_script_present(html, "__NEXT_DATA__"),
        "react_engine_props_present": _embedded_script_present(html, "react-engine-props"),
        "react_engine_props_parseable": react_props is not None,
        "listing_ids_present": False,
        "listing_urls_present": False,
        "pagination_present": bool(re.search(r"(?:pagina|page|pagination|siguiente|next)", html, re.I)),
        "expected_results": None,
        "method": "none",
    }

    records: list[dict] = []
    if next_data is not None:
        records = extract_metadata_from_embedded_payload(next_data, base_url, "next_data")
        diagnostics["method"] = "next_data"
    if not records and react_props is not None:
        records = extract_metadata_from_embedded_payload(react_props, base_url, "react_engine_props")
        diagnostics["method"] = "react_engine_props"
    if not records:
        dom_urls = _extract_detail_urls_from_html(html, base_url)
        records = [_record_from_embedded_object({"url": url}, base_url, "html_detail_url") for url in dom_urls]
        records = [record for record in records if record]
        if records:
            diagnostics["method"] = "html_detail_url"

    diagnostics["listing_urls_present"] = bool(records)
    diagnostics["listing_ids_present"] = any(r.get("listing_id") for r in records)

    # Known result-count fields in both the legacy Next payload and the newer
    # server props. Do not infer a positive count from unrelated config data.
    for payload in (next_data, react_props):
        for item in _iter_dicts(payload):
            for key in ("total", "totalResults", "total_results", "totalPropiedades", "resultCount", "result_count"):
                value = item.get(key) if isinstance(item, dict) else None
                if isinstance(value, int) and value > 0:
                    diagnostics["expected_results"] = value
                    break
            if diagnostics["expected_results"]:
                break
        if diagnostics["expected_results"]:
            break
    return records, diagnostics


def _set_page_param(url: str, page: int) -> str:
    parsed = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "pagina"]
    query.append(("pagina", str(page)))
    return urlunparse(parsed._replace(query=urlencode(query)))


def _matches_requested_state(record: dict, estado) -> bool:
    """Apply the used/new scope locally when the SEO route ignores query filters."""
    if estado in (None, "", 0, "0"):
        return True
    operation = str(record.get("tipo_operacion", "")).lower()
    if str(estado) == "2":
        return not operation or "usado" in operation
    if str(estado) == "1":
        return not operation or "nuevo" in operation
    return True


def evaluate_discovery_health(http_status: int | None, expected_results: int | None, discovered_urls: int) -> dict:
    """Fail closed when a successful response hides an expected result set."""
    degraded = bool(http_status == 200 and expected_results and expected_results > 0 and discovered_urls == 0)
    return {
        "discovery_degraded": degraded,
        "run_aborted": degraded,
        "abort_reason": "HTTP_200_EXPECTED_RESULTS_BUT_ZERO_URLS" if degraded else "",
    }


def _payload_without_listings_is_degraded(diagnostics: dict, records: list[dict]) -> bool:
    """Treat a loaded search payload with no discoverable listings as unsafe."""
    if records:
        return False
    if diagnostics.get("expected_results") and diagnostics["expected_results"] > 0:
        return True
    return bool(diagnostics.get("react_engine_props_present") and diagnostics.get("expected_results") is None)


def extract_metadata_from_next_data(next_data) -> list[dict]:
    props = next_data.get("props", {}).get("pageProps", {})
    propiedades = props.get("propiedades", {})
    results = propiedades.get("results", []) if isinstance(propiedades, dict) else []
    records: list[dict] = []
    for prop in results:
        if not isinstance(prop, dict):
            continue
        url_ficha = str(prop.get("urlFicha", "") or prop.get("url", "") or "")
        if not url_ficha or not is_listing_detail_url(url_ficha):
            continue
        precios = prop.get("precios", [])
        price_uf = ""
        price_clp = ""
        if isinstance(precios, list):
            for p in precios:
                prefix = str(p.get("prefix", ""))
                if prefix == "UF":
                    price_uf = f"UF {p.get('value', '')}"
                else:
                    price_clp = f"$ {p.get('value', '')}"
        dorm = prop.get("dormitorios", [])
        ban = prop.get("bannos", [])
        sup = prop.get("superficie", [])
        prop_id = str(prop.get("idProperty", "")) if prop.get("idProperty") else ""
        lid, lisrc = (prop_id, "next_data") if prop_id else listing_id_from_url(url_ficha)
        records.append({
            "url": str(url_ficha).strip(),
            "listing_id": lid or listing_id_from_url_fallback(url_ficha),
            "listing_id_source": lisrc if lid else "normalized_url_hash",
            "url_format": classify_url_format(url_ficha),
            "title": str(prop.get("titulo", "")),
            "comuna": str(prop.get("comuna", "")),
            "region": str(prop.get("region", "")),
            "operacion": "venta" if "venta" in str(prop.get("tipoOperacion", "")).lower() else "arriendo",
            "tipo_propiedad": str(prop.get("tipoPropiedad", "")).lower(),
            "tipo_operacion": str(prop.get("tipoOperacion", "")),
            "price_uf": price_uf,
            "price_clp": price_clp,
            "dormitorios": dorm[0] if dorm else None,
            "banos": ban[0] if ban else None,
            "superficie": sup[0] if sup else None,
            "publicador": str(prop.get("imagenInmobiliaria", {}).get("alt", prop.get("clientId", ""))),
            "client_id": str(prop.get("clientId", "")),
        })
    return records


def discover_via_ssr(start_urls, max_pages, max_urls, batch_id, discovered, seen_urls, seen_ids,
                     requested_commune=None, estado=None, report=None):
    if max_urls is not None and max_urls <= 0:
        return discovered
    config = AppConfig()
    report = report if report is not None else {}
    report.setdefault("method", "ssr_embedded_payload")
    report.setdefault("pages", [])
    report.setdefault("expected_results", None)
    report.setdefault("duplicates", 0)
    report.setdefault("invalid_urls", 0)
    report.setdefault("scope_removed", 0)
    for start_url in start_urls:
        current_url = _normalize_url(start_url)
        pages_visited = 0
        prev_hash = None
        while True:
            if max_pages is not None and pages_visited >= max_pages:
                break
            try:
                import requests
                resp = requests.get(current_url, headers={
                    "User-Agent": config.user_agent,
                    "Accept": "text/html",
                    "Accept-Language": "es-CL,es;q=0.9",
                }, timeout=config.request_timeout_seconds)
                resp.raise_for_status()
                html = resp.text
            except Exception as e:
                print(f"  SSR download failed: {e}")
                report["run_aborted"] = True
                report["abort_reason"] = "HTTP_OR_NETWORK_FAILURE"
                break
            records, diagnostics = _extract_page_records(html, current_url)
            expected = diagnostics.get("expected_results")
            if expected:
                report["expected_results"] = expected
            props_hash = hash(json.dumps([r.get("url") for r in records], sort_keys=True))
            if prev_hash is not None and props_hash == prev_hash:
                print("  SSR page repeated, stopping.")
                report["pagination_working"] = report.get("pagination_working", False)
                break
            prev_hash = props_hash
            new_on_page = 0
            scope_removed = 0
            duplicates = 0
            for rec in records:
                if requested_commune and not matches_requested_commune(rec.get("url", ""), requested_commune):
                    scope_removed += 1
                    continue
                if not _matches_requested_state(rec, estado):
                    scope_removed += 1
                    continue
                if rec["url"] in seen_urls or (rec["listing_id"] and rec["listing_id"] in seen_ids):
                    duplicates += 1
                    continue
                seen_urls.add(rec["url"])
                if rec["listing_id"]:
                    seen_ids.add(rec["listing_id"])
                rec.update({
                    "source_search_url": start_url,
                    "source_page_url": current_url,
                    "page_number": pages_visited + 1,
                    "discovered_at": _utcnow(),
                    "batch_id": batch_id,
                })
                discovered.append(rec)
                new_on_page += 1
                if len(discovered) >= max_urls:
                    print(f"  Page {pages_visited+1}: {new_on_page} new")
                    report["duplicates"] += duplicates
                    report["scope_removed"] += scope_removed
                    report["pages"].append({"page": pages_visited + 1, "raw_records": len(records), "new_unique_urls": new_on_page,
                                             "duplicates": duplicates, "scope_removed": scope_removed, **diagnostics})
                    return discovered
            report["pages"].append({"page": pages_visited + 1, "raw_records": len(records), "new_unique_urls": new_on_page,
                                    "duplicates": duplicates, "scope_removed": scope_removed, **diagnostics})
            if pages_visited > 0 and new_on_page > 0:
                report["pagination_working"] = True
            report["duplicates"] += duplicates
            report["scope_removed"] += scope_removed
            report["invalid_urls"] += diagnostics.get("invalid_urls", 0)
            print(f"  SSR page {pages_visited+1}: raw={len(records)} new={new_on_page} dup={duplicates} scope_removed={scope_removed}")
            health = evaluate_discovery_health(200, expected, len(records))
            if _payload_without_listings_is_degraded(diagnostics, records):
                health = {
                    "discovery_degraded": True,
                    "run_aborted": True,
                    "abort_reason": (
                        "HTTP_200_REACT_PROPS_WITHOUT_DISCOVERABLE_LISTINGS"
                        if diagnostics.get("react_engine_props_present") and not expected
                        else "HTTP_200_EXPECTED_RESULTS_BUT_ZERO_URLS"
                    ),
                }
            if health["discovery_degraded"]:
                report.update(health)
                break
            if new_on_page == 0:
                break
            pages_visited += 1
            if max_pages is not None and pages_visited >= max_pages:
                break
            current_url = _set_page_param(start_url, pages_visited + 1)
            # This becomes true only after a later page contributes at least
            # one new URL; merely changing the query string is not proof that
            # pagination works.
            report["pagination_working"] = report.get("pagination_working", False)
    return discovered


def discover_via_playwright(start_urls, max_pages, max_urls, batch_id, discovered, seen_urls, seen_ids,
                             proxy_manager=None, block_resources=True, requested_commune=None,
                             estado=None, report=None):
    if max_urls is not None and max_urls <= 0:
        return discovered
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        print("Playwright not installed. Falling back to SSR.")
        return discover_via_ssr(start_urls, max_pages, max_urls, batch_id, discovered, seen_urls, seen_ids,
                                requested_commune=requested_commune, estado=estado, report=report)

    config = AppConfig()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    progress_path = REPORTS_DIR / f"discovery_progress_{batch_id}.json"
    checkpoint_path = REPORTS_DIR / f"discovery_checkpoint_{batch_id}.json"

    if progress_path.exists():
        try:
            discovered = json.loads(progress_path.read_text(encoding="utf-8"))
            seen_urls = {r["url"] for r in discovered}
            seen_ids = {r["listing_id"] for r in discovered if r.get("listing_id")}
            print(f"  Restored {len(discovered)} records from progress file")
        except Exception:
            pass

    page_signatures: set[str] = set()
    stop_reason = None

    def _save_atomic(data):
        tmp = progress_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(progress_path)

    def _page_signature(ids):
        import hashlib
        return hashlib.sha256("|".join(sorted(ids)).encode()).hexdigest()[:16]

    def _scroll_and_extract(pg, base):
        """Scroll progressively, extract all visible property links."""
        prev_count = -1
        all_hrefs = set()
        for _ in range(10):
            current_hrefs = set()
            try:
                links = pg.query_selector_all('a[href*="/propiedad/"], a[href*="/propiedades/"]')
                for link in links:
                    href = link.get_attribute("href")
                    if href:
                        full = urljoin(base, href.strip())
                        if is_listing_detail_url(full):
                            current_hrefs.add(full)
            except Exception:
                pass
            if len(current_hrefs) == prev_count:
                break
            prev_count = len(current_hrefs)
            all_hrefs = current_hrefs
            try:
                pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                pg.wait_for_timeout(1000)
            except Exception:
                break
        return list(all_hrefs)

    def _extract_records_from_hrefs(hrefs, base):
        records = []
        for href in hrefs:
            full = urljoin(base, href.strip()) if not href.startswith("http") else href
            if not is_listing_detail_url(full): continue
            lid, lisrc = listing_id_from_url(full)
            records.append({
                "url": full,
                "listing_id": lid or listing_id_from_url_fallback(full),
                "listing_id_source": lisrc if lid else "normalized_url_hash",
                "url_format": classify_url_format(full),
                "title": "", "comuna": "", "region": "",
                "operacion": "", "tipo_propiedad": "",
                "tipo_operacion": "", "price_uf": "", "price_clp": "",
                "dormitorios": None, "banos": None, "superficie": None,
                "publicador": "", "client_id": "",
            })
        return records

    pw_proxy = None
    proxy_info = {"proxy_applied": False, "proxy_host": "", "session_id": ""}
    if proxy_manager and proxy_manager.has_proxies():
        p = proxy_manager.get_current_proxy()
        if p:
            pw_proxy = p.playwright_config
            proxy_info["proxy_applied"] = True
            proxy_info["proxy_host"] = p.host_port
            proxy_info["session_id"] = batch_id[:16] if batch_id else "unknown"
            print(f"  Playwright proxy: {p.safe_url}")
    if not proxy_info["proxy_applied"]:
        print("  Playwright proxy: direct (proxy_applied=false)")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, proxy=pw_proxy)
        context = browser.new_context(user_agent=config.user_agent, viewport={"width": 1920, "height": 1080}, locale="es-CL")
        page = context.new_page()

        # Block image/media/font resources during discovery (configurable)
        if block_resources:
            page.route("**/*", lambda route: route.abort()
                       if route.request.resource_type in ("image", "media", "font")
                       else route.continue_())

        for start_url in start_urls:
            # Check limit before starting a new search
            if len(discovered) >= max_urls:
                stop_reason = "MAX_UNIQUE_URLS_REACHED"
                break

            current_url = _normalize_url(start_url)
            pages_visited = 0
            next_failures = 0
            blocked_pages = 0
            page_reports = []

            # Check limit before network request
            if len(discovered) >= max_urls:
                stop_reason = "MAX_UNIQUE_URLS_REACHED"
                break

            try:
                page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_selector('a[href*="/propiedad/"], a[href*="/propiedades/"]', timeout=10000)
                except PwTimeout:
                    pass
            except Exception as e:
                print(f"  Playwright goto failed: {e}")
                stop_reason = "PLAYWRIGHT_ERROR"
                break

            while True:
                if max_pages is not None and pages_visited >= max_pages:
                    stop_reason = "MAX_PAGES_REACHED"
                    break
                if len(discovered) >= max_urls:
                    stop_reason = "MAX_UNIQUE_URLS_REACHED"
                    break

                raw_html = page.content()
                body_text = re.sub(r"<script\b.*?</script>", " ", raw_html, flags=re.I|re.S, count=50)
                body_text = re.sub(r"<[^>]+>", " ", body_text)
                body_text = re.sub(r"\s+", " ", body_text).strip().lower()
                if any(p in body_text for p in ["access denied", "forbidden", "too many requests", "cloudflare"]):
                    blocked_pages += 1
                    if blocked_pages >= 2:
                        stop_reason = "BLOCKED"
                        break

                embedded_records, diagnostics = _extract_page_records(raw_html, current_url)
                hrefs = _scroll_and_extract(page, current_url)
                cards_count = len(hrefs)
                records = embedded_records or _extract_records_from_hrefs(hrefs, current_url)
                records = [record for record in records if _matches_requested_state(record, estado)]
                page_ids = [r["listing_id"] for r in records if r.get("listing_id")]
                signature = _page_signature(page_ids) if page_ids else _page_signature([r["url"] for r in records])

                new_on_page = 0
                dup_on_page = 0
                for rec in records:
                    if requested_commune and not matches_requested_commune(rec.get("url", ""), requested_commune):
                        continue
                    # Check limit before each URL addition
                    if len(discovered) >= max_urls:
                        break
                    if rec["url"] in seen_urls or (rec["listing_id"] and rec["listing_id"] in seen_ids):
                        dup_on_page += 1
                        continue
                    seen_urls.add(rec["url"])
                    if rec["listing_id"]:
                        seen_ids.add(rec["listing_id"])
                    rec.update({
                        "origen": "toctoc",
                        "source_portal": "toctoc",
                        "canonical_url": rec["url"],
                        "source_search_url": start_url,
                        "source_page_url": current_url,
                        "discovery_page": pages_visited + 1,
                        "discovery_position": len(discovered) + 1,
                        "discovered_at": _utcnow(),
                        "discovery_method": "playwright_pagination",
                        "batch_id": batch_id,
                    })
                    discovered.append(rec)
                    new_on_page += 1
                    # Check limit immediately after addition
                    if len(discovered) >= max_urls:
                        break

                page_reports.append({
                    "page": pages_visited + 1,
                    "cards_detected": cards_count,
                    "urls_extracted": len(records),
                    "new_unique_urls": new_on_page,
                    "duplicates": dup_on_page,
                    "first_listing_id": page_ids[0] if page_ids else "",
                    "last_listing_id": page_ids[-1] if page_ids else "",
                    "page_signature": signature,
                    "discovery_method": diagnostics.get("method", "dom") if embedded_records else "dom",
                    "scope_filtered": diagnostics.get("expected_results"),
                })

                print(f"  PW page {pages_visited+1}: cards={cards_count} extracted={len(records)} new={new_on_page} dup={dup_on_page} total={len(discovered)}")
                _save_atomic(discovered)
                _save_checkpoint(checkpoint_path, batch_id, start_url, pages_visited+1, len(discovered), stop_reason)

                if len(discovered) >= max_urls:
                    stop_reason = "MAX_UNIQUE_URLS_REACHED"
                    break

                if new_on_page == 0 and dup_on_page > 0:
                    stop_reason = "NO_NEW_URLS"
                    break

                if signature in page_signatures:
                    stop_reason = "REPEATED_PAGE"
                    break
                page_signatures.add(signature)

                pages_visited += 1
                # Check limit before next network request
                if len(discovered) >= max_urls:
                    stop_reason = "MAX_UNIQUE_URLS_REACHED"
                    break

                next_btn = page.query_selector('a.page-link[aria-label="Next"], a.page-link[aria-label="Siguiente"]')
                if not next_btn:
                    # --- MUI Pagination fallback (current Toctoc SPA) ---
                    # Find pagination container, current page, and click target_page button
                    pagination = page.query_selector(
                        'nav[aria-label="pagination navigation"].MuiPagination-root'
                    )
                    if pagination:
                        current_btn = pagination.query_selector('button[aria-current="true"]')
                        current_page = 0
                        if current_btn:
                            try:
                                current_page = int((current_btn.inner_text() or "").strip())
                            except (ValueError, TypeError):
                                stop_reason = "PAGINATION_INVALID_CURRENT"
                                break
                        if current_page:
                            target_page = current_page + 1
                            next_btn = pagination.query_selector(
                                f'button[aria-label="Go to page {target_page}"]'
                            )
                        if not next_btn:
                            stop_reason = "LAST_PAGE_REACHED" if current_page else "NEXT_NOT_FOUND"
                            break
                    # --- end MUI fallback ---
                    else:
                        stop_reason = "NEXT_NOT_FOUND"
                        break
                else:
                    # Original selector matched, preserve original disabled check
                    try:
                        is_disabled = next_btn.get_attribute("disabled") or next_btn.get_attribute("aria-disabled")
                        if is_disabled:
                            stop_reason = "NEXT_DISABLED"
                            break
                    except Exception:
                        pass

                ids_before = set(page_ids)
                # Check limit before click (which triggers network)
                if len(discovered) >= max_urls:
                    stop_reason = "MAX_UNIQUE_URLS_REACHED"
                    break
                try:
                    next_btn.click()
                    page.wait_for_timeout(2000)
                    try:
                        page.wait_for_selector('a[href*="/propiedad/"], a[href*="/propiedades/"], a[href*="/venta/"], a[href*="/arriendo/"]', timeout=8000)
                    except PwTimeout:
                        pass
                    page.wait_for_timeout(1000)
                except Exception as e:
                    next_failures += 1
                    if next_failures >= 2:
                        stop_reason = "PLAYWRIGHT_ERROR"
                        break
                    continue

                current_url = page.url
                new_hrefs = _scroll_and_extract(page, current_url)
                new_ids = set()
                for h in new_hrefs:
                    lid, _ = listing_id_from_url(h)
                    if lid: new_ids.add(lid)
                if new_ids == ids_before:
                    stop_reason = "PAGE_DID_NOT_CHANGE"
                    break

        browser.close()

    stop_reason = stop_reason or "COMPLETED"
    print(f"\n  Discovery finished: {stop_reason}")
    print(f"  Pages: {pages_visited+1}, Total unique URLs: {len(discovered)}")

    _save_checkpoint(checkpoint_path, batch_id, start_url if 'start_url' in dir() else "", pages_visited+1, len(discovered), stop_reason, page_reports)
    if progress_path.exists():
        try: progress_path.unlink()
        except: pass
    return discovered


def _extract_from_dom(page, base_url):
    records = []
    try:
        hrefs = set()
        links = page.query_selector_all('a[href*="/propiedad/"], a[href*="/propiedades/"]')
        for link in links:
            href = link.get_attribute("href")
            if href:
                full = urljoin(base_url, href.strip())
                if is_listing_detail_url(full):
                    hrefs.add(full)
        for full in hrefs:
            lid, lisrc = listing_id_from_url(full)
            records.append({
                "url": full, "listing_id": lid or listing_id_from_url_fallback(full),
                "listing_id_source": lisrc if lid else "normalized_url_hash",
                "url_format": classify_url_format(full), "title": "",
                "comuna": "", "region": "", "operacion": "", "tipo_propiedad": "",
                "tipo_operacion": "", "price_uf": "", "price_clp": "",
                "dormitorios": None, "banos": None, "superficie": None,
                "publicador": "", "client_id": "",
            })
    except Exception: pass
    return records


def _save_progress(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _save_checkpoint(path, batch_id, search_url, last_page, total_urls, stop_reason, page_reports=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    cp = {
        "batch_id": batch_id, "search_url": search_url,
        "last_completed_page": last_page, "total_unique_urls": total_urls,
        "stop_reason": stop_reason, "page_reports": page_reports or [],
        "updated_at": _utcnow(),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cp, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def discover_listing_urls(start_urls=None, max_pages=3, max_urls=500, batch_id=None, use_playwright=False,
                          operacion="venta", tipo="departamento", region="metropolitana", comuna="la-florida",
                          estado=None, publicador=None, precio_desde=None, precio_hasta=None,
                          proxy_manager=None, block_resources=True, return_report=False):
    config = AppConfig()
    config.ensure_layout()
    batch_id = batch_id or config.generate_batch_id()
    discovered: list[dict] = []
    seen_urls: set[str] = set()
    seen_ids: set[str] = set()
    report = {
        "method": "playwright" if use_playwright else "ssr_embedded_payload",
        "pages": [],
        "expected_results": None,
        "duplicates": 0,
        "invalid_urls": 0,
        "scope_removed": 0,
        "discovery_degraded": False,
        "run_aborted": False,
        "pagination_working": False,
    }

    if max_urls is not None and max_urls <= 0:
        return discovered

    if not start_urls:
        route_commune = _search_route_commune(comuna)
        if use_playwright:
            op = operacion if operacion != "venta" else "compra"
            builder = build_search_url(config, operacion=op, tipo=tipo, region=region, comuna=route_commune,
                                        pagina=1, estado=estado, publicador=publicador,
                                        precio_desde=precio_desde, precio_hasta=precio_hasta)
            if builder["warnings"]:
                for w in builder["warnings"]:
                    print(f"  WARNING: {w}")
            # The SPA shell does not expose its result payload reliably. The
            # SSR/SEO route remains the primary source for discovery and is
            # parsed with the same frontend-agnostic adapters below.
            start_urls = [build_ssr_search_url(config, operacion=operacion, tipo=tipo, region=region,
                                               comuna=route_commune, pagina=1, estado=estado,
                                               publicador=publicador, precio_desde=precio_desde,
                                               precio_hasta=precio_hasta)]
        else:
            start_urls = [build_ssr_search_url(config, operacion=operacion, tipo=tipo, region=region,
                                               comuna=route_commune, pagina=1, estado=estado,
                                               publicador=publicador, precio_desde=precio_desde,
                                               precio_hasta=precio_hasta)]

    if use_playwright:
        result = discover_via_playwright(start_urls, max_pages, max_urls, batch_id, discovered, seen_urls, seen_ids,
                                         proxy_manager=proxy_manager, block_resources=block_resources,
                                         requested_commune=comuna, estado=estado, report=report)
    else:
        result = discover_via_ssr(start_urls, max_pages, max_urls, batch_id, discovered, seen_urls, seen_ids,
                                  requested_commune=comuna, estado=estado, report=report)

    # Defensa obligatoria: Toctoc puede devolver resultados fuera de la zona
    # pedida cuando la SPA pierde parte de los parámetros de búsqueda.
    before = len(result)
    result = [r for r in result if matches_requested_commune(r.get("url", ""), comuna)]
    rejected = before - len(result)
    if rejected:
        print(f"  Commune guard: rechazadas {rejected} URLs fuera de {comuna}; aceptadas {len(result)}")
    report["urls_found"] = len(result)
    report["unique_urls"] = len({r.get("url") for r in result})
    report["valid_urls"] = sum(1 for r in result if is_listing_detail_url(r.get("url", "")))
    report["duplicates"] += len(result) - report["unique_urls"]
    if not result and any(page.get("react_engine_props_present") for page in report.get("pages", [])):
        report.update({
            "discovery_degraded": True,
            "run_aborted": True,
            "abort_reason": "HTTP_200_REACT_PROPS_URLS_OUTSIDE_REQUESTED_SCOPE",
        })
    if not report.get("discovery_degraded"):
        report.update(evaluate_discovery_health(200, report.get("expected_results"), len(result)))
    if return_report:
        return {"records": result, "report": report}
    if report.get("discovery_degraded"):
        raise DiscoveryDegradedError(
            f"DISCOVERY_DEGRADED=true; RUN_ABORTED=true; reason={report.get('abort_reason', 'unknown')}"
        )
    return result


if __name__ == "__main__":
    assert is_listing_detail_url("https://www.toctoc.com/propiedades/compranuevo/departamento/la-florida/edificio-refugio-new/1384492")
    assert is_listing_detail_url("https://www.toctoc.com/propiedad/departamento-en-venta-la-florida-metropolitana-5663417")
    assert not is_listing_detail_url("https://www.toctoc.com/venta/departamento/metropolitana/la-florida")
    assert listing_id_from_url("https://www.toctoc.com/propiedades/compranuevo/departamento/la-florida/edificio-refugio-new/1384492")[0] == "1384492"
    assert classify_url_format("https://www.toctoc.com/propiedades/compranuevo/departamento/la-florida/edificio-refugio-new/1384492") == "compranuevo"
    assert classify_url_format("https://www.toctoc.com/propiedad/departamento-en-venta-la-florida-metropolitana-5663417") == "propiedad_usado"
    print("All tests passed.")

