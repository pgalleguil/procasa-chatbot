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
    # Card URLs commonly carry ``utm_source`` and ``o=listaseo``.  IDs belong
    # to the path, never to the query string.
    path = urlparse(url).path
    m = re.search(r"/(\d+)$", path)
    if m: return m.group(1), "url_numeric_id"
    m = re.search(r"-(\d+)$", path)
    if m: return m.group(1), "url_numeric_id"
    m = re.search(r"/[a-z]_([a-f0-9]{20,})$", path, re.I)
    if m: return m.group(1), "url_hash"
    m = re.search(r"/([a-f0-9]{40})$", path, re.I)
    if m: return m.group(1), "url_hash"
    m = re.search(r"/([a-f0-9]{20,})$", path, re.I)
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

TOCTOC_PROPERTY_TYPES = (
    {"slug": "departamento", "route_slug": "departamento", "label": "Departamento", "residential": True},
    {"slug": "casa", "route_slug": "casa", "label": "Casa", "residential": True},
    {"slug": "oficina", "route_slug": "oficina", "label": "Oficina", "residential": False},
    {"slug": "local-comercial", "route_slug": "local-comercial", "label": "Local Comercial", "residential": False},
    {"slug": "terreno", "route_slug": "terreno", "label": "Terreno", "residential": True},
    {"slug": "parcela", "route_slug": "parcela", "label": "Parcela", "residential": True},
    {"slug": "agricola", "route_slug": "campo-agricola", "label": "Agrícola", "residential": False},
    {"slug": "industrial", "route_slug": "terreno-industrial", "label": "Industrial", "residential": False},
    {"slug": "bodega", "route_slug": "bodega", "label": "Bodega", "residential": False},
    {"slug": "estacionamiento", "route_slug": "estacionamiento", "label": "Estacionamiento", "residential": False},
    {"slug": "vacacional", "route_slug": "lugar-vacacional", "label": "Vacacional", "residential": False},
)

# Kept for callers that still need the residential subset.  ``tipo=todos``
# uses the complete UI catalog above and never silently falls back to these
# two types.
TARGET_PROPERTY_TYPES = frozenset({"casa", "departamento"})


def property_type_catalog() -> list[dict[str, Any]]:
    """Return the property types exposed by the current Toctoc filter UI."""
    return [dict(item) for item in TOCTOC_PROPERTY_TYPES]


def property_type_specs(tipo: str | None) -> list[dict[str, Any]]:
    """Resolve a requested type, including the explicit ``TODOS`` scope."""
    value = str(tipo or "").strip().lower()
    if value in {"todos", "todo", "all", "*"}:
        return property_type_catalog()
    for item in TOCTOC_PROPERTY_TYPES:
        if value in {item["slug"], item["route_slug"]}:
            return [dict(item)]
    return [{"slug": value, "route_slug": value, "label": value, "residential": value in TARGET_PROPERTY_TYPES}]

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
    # Query parameters such as ``utm_source`` and ``o=listaseo`` are common
    # on current cards.  Scope matching must use the path only; otherwise a
    # valid ``/venta/.../<commune>/b_<hash>?...`` card is rejected.
    low = urlparse(url).path.lower()
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


def _remove_price_params(url: str) -> str:
    parsed = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key not in {"precioDesde", "precioHasta"}]
    return urlunparse(parsed._replace(query=urlencode(query)))


def _expected_page_count(expected_results: int | None, page_size: int | None = None) -> int | None:
    """Calculate the expected number of UI pages without guessing a result set."""
    if not expected_results or expected_results <= 0:
        return None
    size = page_size or 20
    return (int(expected_results) + size - 1) // size


def _pagination_page_size(dom_result_count: int, embedded_count: int) -> int:
    """Use the visible page size, not Toctoc's internal preload batch."""
    if 10 <= dom_result_count <= 50:
        return dom_result_count
    # The current frontend preloads up to 260 records, while its paginator
    # advances in pages of 20.  Embedded record count is therefore not a
    # valid page-size signal.
    return 20


def _summarize_network_contract(events: list[dict]) -> dict:
    """Summarize the results request without retaining secrets or bodies."""
    result_events = [
        event for event in events
        if "/properties" in str(event.get("path", "")).lower()
    ]
    recaptcha_events = [
        event for event in events
        if "/recaptcha/verify" in str(event.get("path", "")).lower()
    ]
    if not result_events:
        return {
            "results_endpoint": "",
            "http_method": "",
            "pagination_parameter": "page",
            "page_size": 260,
            "cursor_or_offset": "",
            "total_results_field": "response.total / pageProps.propiedades.total",
            "can_call_endpoint_directly": False,
            "results_request_observed": False,
            "recaptcha_required_observed": False,
            "recaptcha_verification_observed": bool(recaptcha_events),
        }
    first = result_events[0]
    params = first.get("params") or {}
    statuses = [event.get("status") for event in result_events if event.get("event") == "response"]
    recaptcha_required = any(
        event.get("status") == 403 or event.get("recaptcha_required")
        or event.get("body_marker") == "recaptcha_required"
        for event in result_events
    )
    return {
        "results_endpoint": first.get("path", ""),
        "http_method": first.get("method", ""),
        "pagination_parameter": "page" if "page" in params else "",
        "page_size": params.get("limit", 260),
        "cursor_or_offset": params.get("cursor", params.get("offset", "")),
        "total_results_field": "response.total",
        # A successful browser request is not proof that the URL is replayable
        # directly: the current frontend first performs its reCAPTCHA verify
        # flow.  Direct replay is therefore false whenever that flow was part
        # of the same navigation.
        "can_call_endpoint_directly": bool(
            statuses
            and all(status == 200 for status in statuses)
            and not recaptcha_required
            and not recaptcha_events
        ),
        "results_request_observed": True,
        "recaptcha_required_observed": recaptcha_required,
        "recaptcha_verification_observed": bool(recaptcha_events),
    }


_SAFE_REQUEST_HEADERS = {
    "user-agent",
    "referer",
    "origin",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "sec-fetch-user",
}
_TOKEN_HEADER_MARKERS = ("authorization", "cookie", "csrf", "token", "session", "x-api-key")
_TOKEN_QUERY_MARKERS = ("token", "secret", "auth", "session", "cookie", "captcha")


def _secret_fingerprint(value: Any) -> str:
    import hashlib

    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:16]


def _safe_query_params(query: str) -> dict[str, Any]:
    """Return diagnostics-safe query parameters.

    ``filtros`` contains a large search payload and may contain session-bound
    values.  It is useful for comparing requests, but its contents must not be
    emitted to reports.
    """
    safe: dict[str, Any] = {}
    for key, value in parse_qsl(query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower == "filtros":
            safe[key] = {
                "redacted": True,
                "length": len(value),
                "sha256": _secret_fingerprint(value),
            }
        elif any(marker in key_lower for marker in _TOKEN_QUERY_MARKERS):
            safe[key] = "<redacted>"
        else:
            safe[key] = value
    return safe


def _safe_request_context(request, context=None) -> dict[str, Any]:
    """Capture browser-session evidence without persisting credentials/tokens."""
    parsed = urlparse(request.url)
    try:
        headers = request.all_headers()
    except Exception:
        headers = dict(getattr(request, "headers", {}) or {})
    normalized_headers = {str(key).lower(): str(value) for key, value in headers.items()}
    safe_headers = {
        key: normalized_headers[key]
        for key in _SAFE_REQUEST_HEADERS
        if key in normalized_headers
    }
    header_names = sorted(normalized_headers)
    token_header_names = sorted(
        key for key in header_names if any(marker in key for marker in _TOKEN_HEADER_MARKERS)
    )
    request_cookie_names: list[str] = []
    cookie_header = normalized_headers.get("cookie", "")
    if cookie_header:
        request_cookie_names = sorted(
            part.split("=", 1)[0].strip()
            for part in cookie_header.split(";")
            if "=" in part
        )

    context_cookies: list[dict[str, Any]] = []
    if context is not None:
        try:
            for cookie in context.cookies():
                value = cookie.get("value", "")
                context_cookies.append({
                    "name": cookie.get("name", ""),
                    "domain": cookie.get("domain", ""),
                    "path": cookie.get("path", ""),
                    "secure": bool(cookie.get("secure")),
                    "http_only": bool(cookie.get("httpOnly")),
                    "same_site": cookie.get("sameSite", ""),
                    "has_value": bool(value),
                    "value_sha256": _secret_fingerprint(value) if value else "",
                })
        except Exception:
            context_cookies = []

    safe_url = urlunparse(parsed._replace(query=urlencode(_safe_query_params(parsed.query), doseq=True)))
    return {
        "method": request.method,
        "url": safe_url,
        "path": parsed.path,
        "params": _safe_query_params(parsed.query),
        "user_agent": safe_headers.get("user-agent", ""),
        "safe_headers": safe_headers,
        "header_names": header_names,
        "token_header_names": token_header_names,
        "request_cookie_names": request_cookie_names,
        "context_cookies": context_cookies,
        "resource_type": request.resource_type,
    }


def _compare_request_contexts(initial: dict | None, page2: dict | None) -> dict:
    """Compare page 1 and page 2 contexts using names/presence only."""
    if not initial or not page2:
        return {
            "status": "INSUFFICIENT_REQUESTS",
            "initial_available": bool(initial),
            "page2_available": bool(page2),
        }
    initial_headers = set(initial.get("header_names", []))
    page2_headers = set(page2.get("header_names", []))
    initial_cookies = {item.get("name") for item in initial.get("context_cookies", [])}
    page2_cookies = {item.get("name") for item in page2.get("context_cookies", [])}
    return {
        "status": "COMPARED",
        "missing_header_names_in_page2": sorted(initial_headers - page2_headers),
        "missing_request_cookie_names_in_page2": sorted(
            set(initial.get("request_cookie_names", [])) - set(page2.get("request_cookie_names", []))
        ),
        "missing_context_cookie_names_in_page2": sorted(initial_cookies - page2_cookies),
        "safe_header_value_changes": {
            key: {"initial": initial.get("safe_headers", {}).get(key, ""),
                  "page2": page2.get("safe_headers", {}).get(key, "")}
            for key in _SAFE_REQUEST_HEADERS
            if initial.get("safe_headers", {}).get(key, "")
            != page2.get("safe_headers", {}).get(key, "")
        },
        "token_header_names_initial": initial.get("token_header_names", []),
        "token_header_names_page2": page2.get("token_header_names", []),
        "page2_has_same_user_agent": initial.get("user_agent") == page2.get("user_agent"),
        "possible_missing_session_requirement": bool(
            initial_headers - page2_headers
            or set(initial.get("request_cookie_names", [])) - set(page2.get("request_cookie_names", []))
            or initial_cookies - page2_cookies
        ),
    }


def _batch_number_for_page(page_number: int, ui_pages_per_batch: int = 13) -> int:
    return ((max(1, int(page_number)) - 1) // ui_pages_per_batch) + 1


def _batch_bounds(batch_number: int, ui_pages_per_batch: int = 13) -> tuple[int, int]:
    first = (max(1, int(batch_number)) - 1) * ui_pages_per_batch + 1
    return first, first + ui_pages_per_batch - 1


def _build_batch_checkpoint(page_reports: list[dict], batch_number: int, *, status: str,
                            request_status: Any = None, challenge_detected: bool = False,
                            completed: bool = False) -> dict:
    """Summarize one internal 260-result batch for a resumable checkpoint."""
    first_page, last_page = _batch_bounds(batch_number)
    completed_reports = [
        item for item in page_reports
        if item.get("batch_number") == batch_number and item.get("completed", True)
    ]
    ids = [
        str(item.get("first_listing_id"))
        for item in completed_reports
        if item.get("first_listing_id")
    ] + [
        str(item.get("last_listing_id"))
        for item in completed_reports
        if item.get("last_listing_id")
    ]
    unique_ids: list[str] = []
    for item in completed_reports:
        unique_ids.extend(str(value) for value in item.get("listing_ids", []) if value)
    unique_ids = sorted(set(unique_ids))
    import hashlib
    ids_hash = hashlib.sha256("|".join(unique_ids).encode()).hexdigest()[:16] if unique_ids else ""
    return {
        "batch_number": batch_number,
        "portal_page_from": first_page,
        "portal_page_to": last_page,
        "first_listing_id": ids[0] if ids else "",
        "last_listing_id": ids[-1] if ids else "",
        "listing_ids_hash": ids_hash,
        "unique_count": len(unique_ids),
        "request_status": request_status,
        "challenge_detected": bool(challenge_detected),
        "completed": bool(completed),
        "status": status,
    }


def _batch_checkpoints(page_reports: list[dict]) -> list[dict]:
    """Build deterministic batch summaries from completed/attempted pages."""
    batch_numbers = sorted({
        int(item.get("batch_number", _batch_number_for_page(item.get("page", 1))))
        for item in page_reports
        if item.get("page")
    })
    summaries: list[dict] = []
    for batch_number in batch_numbers:
        batch_pages = [item for item in page_reports if item.get("batch_number") == batch_number]
        completed_pages = [item for item in batch_pages if item.get("completed", True)]
        expected_from, expected_to = _batch_bounds(batch_number)
        has_full_batch = {
            int(item.get("page")) for item in completed_pages if item.get("page")
        } >= set(range(expected_from, expected_to + 1))
        last_completed = max(
            completed_pages,
            key=lambda item: int(item.get("page", 0)),
            default={},
        )
        # The final batch can be shorter than the nominal 13 UI pages.  A
        # completed page with no enabled next control is a legitimate batch
        # completion, not an incomplete batch.
        if completed_pages and not last_completed.get("next_button_enabled", True):
            has_full_batch = True
        status = "COMPLETE" if has_full_batch else "IN_PROGRESS"
        challenge = any(item.get("challenge_detected") for item in batch_pages)
        if challenge:
            status = "CHALLENGE_BLOCKED"
        request_statuses = [
            status_value for item in batch_pages
            for status_value in item.get("result_statuses", [])
            if status_value is not None
        ]
        summaries.append(_build_batch_checkpoint(
            page_reports,
            batch_number,
            status=status,
            request_status=(request_statuses[-1] if request_statuses else None),
            challenge_detected=challenge,
            completed=has_full_batch,
        ))
    return summaries


def _is_disabled_pagination_control(element) -> bool:
    if element is None:
        return True
    try:
        return bool(element.get_attribute("disabled") or element.get_attribute("aria-disabled") == "true")
    except Exception:
        return False


def _parse_numeric_amount(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("$", "").replace("UF", "").replace("uf", "")
    text = text.replace(" ", "")
    if not text:
        return None
    # Chilean published prices use dots as thousands separators.  Keep a
    # decimal only when there is no thousands separator.
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "").replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


def published_price_clp(record: dict, uf_valor_clp: float) -> float | None:
    """Normalize only the published price represented in a discovery card."""
    clp = _parse_numeric_amount(record.get("price_clp"))
    if clp is not None:
        return clp
    uf = _parse_numeric_amount(record.get("price_uf"))
    if uf is not None and uf_valor_clp > 0:
        return uf * uf_valor_clp
    return None


def _path_key(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path.rstrip("/").lower()


def _merge_embedded_metadata(records: list[dict], embedded_records: list[dict]) -> list[dict]:
    """Enrich rendered card URLs without replacing the browser page source."""
    by_path = {_path_key(item.get("url", "")): item for item in embedded_records if item.get("url")}
    # The current SPA can render a legacy ``/propiedades/.../<numeric-id>``
    # card href while the SSR/NextData payload exposes the same listing as a
    # public ``/venta/.../<hash>`` URL. The path is not a stable join key;
    # use the portal listing id as a safe fallback so price and operation
    # metadata survive the DOM-first pagination path.
    by_id = {}
    for item in embedded_records:
        item_id = str(item.get("listing_id") or "").strip()
        if item_id:
            by_id[item_id] = item
    merged = []
    for record in records:
        source = by_path.get(_path_key(record.get("url", "")), {})
        if not source:
            record_id = str(record.get("listing_id") or "").strip()
            source = by_id.get(record_id, {})
        combined = dict(source)
        combined.update({key: value for key, value in record.items() if value not in (None, "")})
        merged.append(combined)
    return merged


def _playwright_scope_url(config, start_url: str, estado=None) -> str:
    """Build the browser search shell used by the current Toctoc SPA.

    The old SEO route still provides useful SSR data, but it does not expose
    the live paginator.  The browser must enter the SPA search view so that
    Toctoc itself resolves the commune and polygon context before pagination.
    """
    parsed = urlparse(start_url)
    parts = [part for part in parsed.path.split("/") if part]
    operation = "compra"
    property_type = "departamento"
    if parts and parts[0].lower() == "arriendo":
        operation = "arriendo"
    if len(parts) >= 2 and parts[1]:
        property_type = parts[1]
    query = {"moneda": "2", "pagina": "1"}
    source_query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key in ("estado", "publicador", "precioDesde", "precioHasta", "temporalidad"):
        if source_query.get(key) not in (None, ""):
            query[key] = source_query[key]
    return urlunparse(parsed._replace(
        path=f"/resultados/lista/{operation}/{property_type}/",
        query=urlencode(query),
        fragment="",
    ))


def _playwright_commune_label(commune: str) -> str:
    if _normalize_slug(commune) == "santiago centro":
        return "Santiago"
    return " ".join(part.capitalize() for part in commune.replace("_", "-").split("-"))


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


def _reported_results_from_visible_text(text: str) -> int | None:
    """Read the visible result count without treating arbitrary numbers as totals."""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    match = re.search(r"\b(?:de|of)\s+([\d.]+)\s+resultados?\b", normalized, re.I)
    if not match:
        return None
    try:
        return int(match.group(1).replace(".", ""))
    except ValueError:
        return None


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
                     requested_commune=None, estado=None, min_price_clp=None,
                     uf_valor_clp=0.0, report=None):
    if max_urls is not None and max_urls <= 0:
        return discovered
    config = AppConfig()
    report = report if report is not None else {}
    report.setdefault("method", "ssr_embedded_payload")
    report.setdefault("pages", [])
    report.setdefault("expected_results", None)
    report.setdefault("reported_results", None)
    report.setdefault("pages_expected", None)
    report.setdefault("pages_fetched", 0)
    report.setdefault("raw_listings_found", 0)
    report.setdefault("repeated_page", False)
    report.setdefault("repeated_cursor", False)
    report.setdefault("ssr_page_limit", False)
    report.setdefault("canonical_url_duplicates", 0)
    report.setdefault("route_errors", [])
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
                if resp.status_code == 404:
                    report["route_errors"].append({"url": current_url, "status": 404})
                    report["discovery_degraded"] = True
                    report["run_aborted"] = True
                    report["abort_reason"] = "HTTP_404_ROUTE_NOT_FOUND"
                    break
                resp.raise_for_status()
                html = resp.text
            except Exception as e:
                print(f"  SSR download failed: {e}")
                report.setdefault("fetch_errors", []).append({"url": current_url, "error": str(e)})
                report["run_aborted"] = True
                report["abort_reason"] = "HTTP_OR_NETWORK_FAILURE"
                break
            records, diagnostics = _extract_page_records(html, current_url)
            expected = diagnostics.get("expected_results")
            if expected:
                report["expected_results"] = expected
                report["reported_results"] = expected
                report["pages_expected"] = _expected_page_count(expected, 20)
            props_hash = hash(json.dumps([r.get("url") for r in records], sort_keys=True))
            if prev_hash is not None and props_hash == prev_hash:
                print("  SSR page repeated, stopping.")
                report["repeated_page"] = True
                report["ssr_page_limit"] = True
                report["discovery_degraded"] = True
                report["run_aborted"] = True
                report["abort_reason"] = "PAGINATION_DEGRADED_REPEATED_SSR_PAGE"
                break
            prev_hash = props_hash
            new_on_page = 0
            scope_removed = 0
            duplicates = 0
            unique_before_price_on_page = 0
            below_price_on_page = 0
            invalid_price_on_page = 0
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
                unique_before_price_on_page += 1
                if min_price_clp is not None:
                    published_clp = published_price_clp(rec, uf_valor_clp)
                    if published_clp is None:
                        invalid_price_on_page += 1
                        continue
                    if published_clp < min_price_clp:
                        below_price_on_page += 1
                        continue
                rec.update({
                    "source_search_url": start_url,
                    "source_page_url": current_url,
                    "page_number": pages_visited + 1,
                    "discovered_at": _utcnow(),
                    "batch_id": batch_id,
                })
                discovered.append(rec)
                new_on_page += 1
                if max_urls is not None and len(discovered) >= max_urls:
                    print(f"  Page {pages_visited+1}: {new_on_page} new")
                    report["duplicates"] += duplicates
                    report["scope_removed"] += scope_removed
                    report["pages"].append({"page": pages_visited + 1, "raw_records": len(records), "new_unique_urls": new_on_page,
                                             "duplicates": duplicates, "scope_removed": scope_removed,
                                             "unique_before_price": unique_before_price_on_page,
                                             "below_min_price_excluded": below_price_on_page,
                                             "invalid_price_excluded": invalid_price_on_page, **diagnostics})
                    return discovered
            report["pages"].append({"page": pages_visited + 1, "raw_records": len(records), "new_unique_urls": new_on_page,
                                    "duplicates": duplicates, "scope_removed": scope_removed,
                                    "unique_before_price": unique_before_price_on_page,
                                    "below_min_price_excluded": below_price_on_page,
                                    "invalid_price_excluded": invalid_price_on_page, **diagnostics})
            report["pages_fetched"] = len(report["pages"])
            report["raw_listings_found"] += len(records)
            report["unique_before_price"] = report.get("unique_before_price", 0) + unique_before_price_on_page
            report["below_min_price_excluded"] = report.get("below_min_price_excluded", 0) + below_price_on_page
            report["invalid_price_excluded"] = report.get("invalid_price_excluded", 0) + invalid_price_on_page
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
                             estado=None, min_price_clp=None, uf_valor_clp=0.0, report=None):
    if max_urls is not None and max_urls <= 0:
        return discovered
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        # The protected browser path must fail closed. Falling back to direct
        # HTTP here could turn a session challenge into an incomplete scope.
        if report is not None:
            report.update({
                "discovery_degraded": True,
                "run_aborted": True,
                "abort_reason": "PLAYWRIGHT_REQUIRED_FOR_PROTECTED_DISCOVERY",
            })
        print("Playwright is required for protected Toctoc discovery; stopping safely.")
        return discovered

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
    all_page_reports: list[dict] = []
    search_reports: list[dict] = []
    resume_checkpoint = {}
    if checkpoint_path.exists():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            resume_checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
            report["resume_checkpoint_loaded"] = True
            report["resume_from_page"] = checkpoint.get("next_page")
            report["resume_from_batch"] = checkpoint.get("batches", [])[-1] if checkpoint.get("batches") else {}
        except Exception:
            report["resume_checkpoint_loaded"] = False

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
                # The live SPA renders legacy /propiedades/ links while the
                # public SEO route renders /venta/.../b_<hash> links.
                links = pg.query_selector_all(
                    'a[href*="/propiedad/"], a[href*="/propiedades/"], '
                    'a[href*="/venta/"], a[href*="/arriendo/"]'
                )
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
                pg.wait_for_timeout(600)
            except Exception:
                break
        return list(all_hrefs)

    def _parse_card_price_text(text: str) -> dict:
        """Parse only the price rendered inside one listing card.

        This intentionally does not consult NextData or any other record.  A
        card's href and price must come from the same rendered DOM node.
        """
        raw = re.sub(r"\s+", " ", str(text or "")).strip()
        uf_match = re.search(r"\bUF\s*([0-9][0-9.\s]*(?:,[0-9]+)?)", raw, re.I)
        clp_match = re.search(r"(?:^|[/| ])\$\s*([0-9][0-9.\s]*(?:,[0-9]+)?)", raw, re.I)
        price_uf = _parse_numeric_amount(uf_match.group(1)) if uf_match else None
        price_clp = _parse_numeric_amount(clp_match.group(1)) if clp_match else None
        if price_clp is not None:
            currency = "CLP"
            status = "ABOVE_MIN_PRICE" if price_clp >= 100_000_000 else "BELOW_MIN_PRICE"
        elif price_uf is not None and uf_valor_clp > 0:
            price_clp = price_uf * uf_valor_clp
            currency = "UF"
            status = "ABOVE_MIN_PRICE" if price_clp >= 100_000_000 else "BELOW_MIN_PRICE"
        elif price_uf is not None:
            currency = "UF"
            status = "PRICE_NOT_AVAILABLE"
        else:
            currency = ""
            status = "PRICE_NOT_AVAILABLE"
        return {
            "card_price_raw": raw,
            "card_currency": currency,
            "card_price_uf": price_uf,
            "card_price_clp": price_clp,
            "card_price_status": status,
            "price_source": "LISTING_CARD",
        }

    def _scroll_and_extract_cards(pg, base):
        """Extract href and visible metadata from the same rendered card.

        The browser DOM is the sole source here.  In particular, this does
        not join a DOM href to an embedded/SSR result by position or ID.
        """
        latest: dict[str, dict] = {}
        for _ in range(10):
            try:
                rows = pg.evaluate(
                    """() => {
                      const isListing = href => {
                        try {
                          const u = new URL(href, location.href);
                          if (u.pathname.includes('/resultados/')) return false;
                          return /\\/(propiedades|propiedad|venta|arriendo)\\//i.test(u.pathname)
                            && /(\\/\\d+$|[-_]\\d+$|\\/[a-z]_[a-f0-9]{20,}$|\\/[a-f0-9]{20,}$)/i.test(u.pathname);
                        } catch (_) { return false; }
                      };
                      const candidates = [];
                      for (const a of document.querySelectorAll('a[href]')) {
                        if (!isListing(a.href)) continue;
                        let node = a;
                        let card = null;
                        for (let i = 0; i < 10 && node; i++, node = node.parentElement) {
                          const cls = String(node.className || '');
                          const testid = String(node.getAttribute('data-testid') || '');
                          if (node.tagName === 'ARTICLE' || /card|contenedorCard-ds/i.test(cls + ' ' + testid)) {
                            if ((node.innerText || '').trim()) { card = node; break; }
                          }
                        }
                        if (!card) card = a.parentElement || a;
                        candidates.push({
                          href: a.href,
                          card_text: String(card.innerText || a.innerText || '').trim(),
                          card_class: String(card.className || ''),
                          card_testid: String(card.getAttribute('data-testid') || '')
                        });
                      }
                      return candidates;
                    }"""
                ) or []
                for row in rows:
                    href = urljoin(base, str(row.get("href") or "").strip())
                    if not is_listing_detail_url(href):
                        continue
                    key = _path_key(href)
                    previous = latest.get(key)
                    if previous is None or len(row.get("card_text", "")) > len(previous.get("card_text", "")):
                        latest[key] = {
                            "url": href,
                            **_parse_card_price_text(row.get("card_text", "")),
                            "card_class": row.get("card_class", ""),
                            "card_testid": row.get("card_testid", ""),
                        }
            except Exception:
                pass
            try:
                pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                pg.wait_for_timeout(600)
            except Exception:
                break
        return list(latest.values())

    def _prepare_live_scope(pg, seo_url):
        """Navigate through the real SPA location selector.

        Directly opening the old SEO route gives a page-one SSR payload, but
        the live paginator is only mounted by the SPA search view.  Selecting
        the commune in the browser also lets Toctoc create the current
        polygon/context parameters legitimately, without replaying protected
        API calls ourselves.
        """
        scope_url = _playwright_scope_url(config, seo_url, estado=estado)
        display = _playwright_commune_label(requested_commune or "")
        last_reason = "SPA_LOCATION_INPUT_NOT_FOUND"
        for attempt in range(2):
            pg.goto(scope_url, wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(2500 if attempt == 0 else 1800)
            try:
                pg.wait_for_selector(
                    'input[placeholder*="barrio" i], input[placeholder*="comuna" i], '
                    'input[placeholder*="ciudad" i], input[aria-label*="comuna" i], '
                    'input[aria-label*="ubicación" i], [role="combobox"]',
                    timeout=8000,
                )
            except Exception:
                pass

            location_input = None
            location_selectors = (
                'input[placeholder*="barrio" i], input[placeholder*="comuna" i], input[placeholder*="ciudad" i]',
                'input[aria-label*="comuna" i], input[aria-label*="ubicación" i], input[aria-label*="ubicacion" i]',
                'input[name*="comuna" i], input[name*="location" i], input[data-testid*="location" i]',
                '[role="combobox"] input, input[role="combobox"]',
            )
            for selector in location_selectors:
                candidate = pg.locator(selector).first
                try:
                    if candidate.count() and candidate.is_visible():
                        location_input = candidate
                        break
                except Exception:
                    continue
            if location_input is None:
                # Regionalized SPA builds sometimes omit semantic attributes.
                # Use a lone visible text/search input only; never guess among
                # multiple unrelated filters.
                visible_inputs = []
                inputs = pg.locator('input:not([type="hidden"])')
                for index in range(inputs.count()):
                    candidate = inputs.nth(index)
                    try:
                        if candidate.is_visible():
                            visible_inputs.append(candidate)
                    except Exception:
                        continue
                if len(visible_inputs) == 1:
                    location_input = visible_inputs[0]
            if location_input is None:
                last_reason = "SPA_LOCATION_INPUT_NOT_FOUND"
                continue

            try:
                location_input.click()
                location_input.press("Control+A")
                location_input.fill(display)
                pg.wait_for_timeout(900)
                desired = _normalize_slug(display)
                suggestions = None
                # Prefer explicit autocomplete rows, then exact text within
                # location-like DOM nodes. Avoid clicking broad result cards.
                for selector in (
                    '[role="option"]', '[role="listbox"] [role="option"]',
                    'span:has(i.ic-location)', '[data-testid*="location" i]',
                    '[class*="location" i], [class*="commune" i]',
                    'li, button',
                ):
                    rows = pg.locator(selector)
                    for index in range(min(rows.count(), 80)):
                        row = rows.nth(index)
                        try:
                            if not row.is_visible():
                                continue
                            row_text = _normalize_slug(row.inner_text(timeout=500))
                        except Exception:
                            continue
                        if row_text == desired:
                            suggestions = row
                            break
                    if suggestions is not None:
                        break
                if suggestions is None:
                    exact = pg.get_by_text(display, exact=True).first
                    try:
                        if exact.count() and exact.is_visible():
                            suggestions = exact
                    except Exception:
                        pass
                if suggestions is None:
                    last_reason = "SPA_COMMUNE_SUGGESTION_NOT_FOUND"
                    continue
                suggestions.click()
                pg.wait_for_timeout(2200)
                last_reason = ""
                break
            except Exception as exc:
                last_reason = f"SPA_COMMUNE_SELECTION_FAILED:{type(exc).__name__}"
                continue
        if last_reason:
            return False, last_reason
        current = pg.url.lower()
        expected_slug = _normalize_slug(requested_commune or "").replace(" ", "-")
        if expected_slug == "santiago-centro":
            expected_slug = "santiago"
        if f"/{expected_slug}/" not in current:
            # Some official communes have no SEO directory and therefore do
            # not appear in the resulting URL.  The selection is still
            # trustworthy when the browser's location control retained the
            # exact chosen label; final listing-level scope checks remain
            # mandatory below.
            selected_value = ""
            try:
                selected_value = pg.locator(
                    'input[placeholder*="barrio" i], '
                    'input[placeholder*="comuna" i], '
                    'input[placeholder*="ciudad" i]'
                ).first.input_value()
            except Exception:
                pass
            if _normalize_slug(selected_value) != _normalize_slug(display):
                return False, "SPA_COMMUNE_SELECTION_OUT_OF_SCOPE"
        return True, ""

    def _prepare_ssr_scope(pg, seo_url):
        """Open the SEO result page whose real MUI paginator is browser-driven.

        Directly changing ``pagina`` with requests is not a valid pagination
        operation on the current frontend: the server repeats the bootstrap
        payload.  The rendered page, however, exposes a MUI paginator whose
        click handler loads the next page in the legitimate browser session.
        """
        response = pg.goto(seo_url, wait_until="domcontentloaded", timeout=60000)
        if response is not None and response.status == 404:
            return False, "HTTP_404_ROUTE_NOT_FOUND"
        pg.wait_for_timeout(3500)
        try:
            pg.wait_for_selector(
                'nav[aria-label="pagination navigation"], '
                'a[href*="/venta/"], a[href*="/arriendo/"]',
                timeout=20000,
            )
        except Exception:
            # A server-rendered bootstrap can be HTTP 200 while the current
            # SPA shell never mounts its live paginator.  Falling back to the
            # normal location-selection flow is safer than treating the SSR
            # payload as a complete discovery result.
            return False, "PAGINATION_CONTROL_NOT_MOUNTED"
        return True, ""

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
            # Never print proxy usernames: providers often embed account or
            # session credentials in that field. Host/port is sufficient for
            # operational diagnostics.
            print(f"  Playwright proxy: {p.host_port} (credentials redacted)")
    if not proxy_info["proxy_applied"]:
        print("  Playwright proxy: direct (proxy_applied=false)")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, proxy=pw_proxy)
        context = browser.new_context(user_agent=config.user_agent, viewport={"width": 1920, "height": 1080}, locale="es-CL")
        page = context.new_page()
        network_events = []

        def _relevant_network_url(value):
            low_url = str(value or "").lower()
            return (
                "www.toctoc.com" in low_url
                and (
                    "/gw-lista-seo/" in low_url
                    or "pagina=" in low_url
                    or "/properties" in low_url
                    or "/resultados/" in low_url
                )
            )

        def _request_metadata(request, event_type):
            parsed = urlparse(request.url)
            post_data = request.post_data or ""
            if "/recaptcha/verify" in parsed.path.lower():
                # Never persist or print the ephemeral reCAPTCHA token.
                post_data = "<redacted recaptcha token>"
            metadata = {
                "event": event_type,
                "method": request.method,
                "url": urlunparse(parsed._replace(query=urlencode(_safe_query_params(parsed.query), doseq=True))),
                "path": parsed.path,
                "params": _safe_query_params(parsed.query),
                "post_data": post_data if post_data == "<redacted recaptcha token>" else {
                    "present": bool(post_data),
                    "length": len(post_data),
                    "sha256": _secret_fingerprint(post_data) if post_data else "",
                },
                "resource_type": request.resource_type,
            }
            try:
                metadata["request_context"] = _safe_request_context(request, context)
            except Exception:
                metadata["request_context"] = {"status": "unavailable"}
            return metadata

        def _capture_request(request):
            if _relevant_network_url(request.url):
                network_events.append(_request_metadata(request, "request"))

        def _capture_response(response):
            low_url = response.url.lower()
            should_capture = (
                "/gw-lista-seo/" in low_url
                or "pagina=" in low_url
                or "/properties" in low_url
                or "/resultados/" in low_url
            )
            if not should_capture:
                return
            event = _request_metadata(response.request, "response")
            event["status"] = response.status
            try:
                body = response.text()
                event["recaptcha_required"] = "recaptcha_required" in body.lower()
                event["body_marker"] = next(
                    (marker for marker in ("recaptcha_required", "forbidden", "captcha", "access denied")
                     if marker in body.lower()),
                    "",
                )
                if "/properties" in low_url and response.status == 200:
                    try:
                        api_payload = json.loads(body)
                        api_records = extract_metadata_from_embedded_payload(
                            api_payload, response.url, "browser_results_endpoint"
                        )
                        event["api_record_count"] = len(api_records)
                        event["_api_records"] = api_records
                    except (TypeError, ValueError, json.JSONDecodeError):
                        event["api_record_count"] = 0
            except Exception:
                event["recaptcha_required"] = False
                event["body_marker"] = ""
            network_events.append(event)

        def _capture_request_failed(request):
            failure = request.failure
            low_url = request.url.lower()
            if failure and _relevant_network_url(low_url):
                event = _request_metadata(request, "requestfailed")
                event["status"] = None
                event["request_failed"] = failure
                network_events.append(event)

        page.on("request", _capture_request)
        page.on("response", _capture_response)
        page.on("requestfailed", _capture_request_failed)

        # Block image/media/font resources during discovery (configurable)
        if block_resources:
            page.route("**/*", lambda route: route.abort()
                       if route.request.resource_type in ("image", "media", "font")
                       else route.continue_())

        for start_url in start_urls:
            # Check limit before starting a new search
            if max_urls is not None and len(discovered) >= max_urls:
                stop_reason = "MAX_UNIQUE_URLS_REACHED"
                break

            current_url = _normalize_url(start_url)
            pages_visited = 0
            next_failures = 0
            blocked_pages = 0
            page_reports = []
            previous_page_ids = set()
            network_cursor = 0
            # A signature is scoped to one type/commune search.  Reusing it
            # across separate start URLs would incorrectly mark a legitimate
            # overlap between searches as a pagination failure.
            page_signatures = set()

            # Check limit before network request
            if max_urls is not None and len(discovered) >= max_urls:
                stop_reason = "MAX_UNIQUE_URLS_REACHED"
                break

            try:
                prepared, prepare_reason = _prepare_ssr_scope(page, current_url)
                if not prepared:
                    # A commune without an SEO directory (notably Pedro
                    # Aguirre Cerda) can still be selected through the real
                    # browser location widget.  Never fabricate a protected
                    # request; use the SPA only after the public route says
                    # it does not exist.
                    if prepare_reason in {"HTTP_404_ROUTE_NOT_FOUND", "PAGINATION_CONTROL_NOT_MOUNTED"}:
                        prepared, spa_reason = _prepare_live_scope(page, current_url)
                        if prepared:
                            report.setdefault("spa_scope_fallbacks", []).append({
                                "search_url": start_url,
                                "reason": prepare_reason,
                            })
                            prepare_reason = ""
                        else:
                            prepare_reason = spa_reason
                    if not prepared:
                        stop_reason = prepare_reason
                        report.update({
                            "discovery_degraded": True,
                            "run_aborted": True,
                            "abort_reason": prepare_reason,
                        })
                        report.setdefault("route_errors", []).append({
                            "url": current_url,
                            "status": 404 if prepare_reason == "HTTP_404_ROUTE_NOT_FOUND" else None,
                            "reason": prepare_reason,
                        })
                        break
                try:
                    page.wait_for_selector(
                        'a[href*="/propiedad/"], a[href*="/propiedades/"], '
                        'a[href*="/venta/"], a[href*="/arriendo/"]',
                        timeout=10000,
                    )
                except PwTimeout:
                    pass
            except Exception as e:
                print(f"  Playwright goto failed: {e}")
                stop_reason = "PLAYWRIGHT_ERROR"
                report.update({
                    "discovery_degraded": True,
                    "run_aborted": True,
                    "abort_reason": "PLAYWRIGHT_ERROR",
                })
                break

            # Resume a partial search by replaying only the real browser
            # pagination controls up to the first uncompleted page. The
            # already captured records and page ledger were restored above;
            # intermediate pages are navigated but not reparsed/reclassified.
            checkpoint_url = _normalize_url(resume_checkpoint.get("search_url") or "")
            resume_page = 1
            progress_loaded = progress_path.exists()
            if (
                checkpoint_url == _normalize_url(start_url)
                and progress_loaded
                and not resume_checkpoint.get("challenge_detected")
            ):
                try:
                    candidate_page = int(resume_checkpoint.get("next_page") or 1)
                    if candidate_page > 1 and int(resume_checkpoint.get("last_completed_page") or 0) > 0:
                        resume_page = candidate_page
                except (TypeError, ValueError):
                    resume_page = 1

            if resume_page > 1:
                previous_reports = [
                    dict(item) for item in (resume_checkpoint.get("page_reports") or [])
                    if item.get("completed") and int(item.get("page") or 0) < resume_page
                ]
                page_reports = previous_reports
                pages_visited = resume_page - 1
                page_signatures = {
                    str(item.get("page_signature"))
                    for item in previous_reports if item.get("page_signature")
                }
                if previous_reports:
                    previous_page_ids = set(previous_reports[-1].get("listing_ids") or [])

                def _visible_ids_for_resume():
                    try:
                        hrefs = page.locator(
                            'a[href*="/propiedad/"], a[href*="/propiedades/"], '
                            'a[href*="/venta/"], a[href*="/arriendo/"]'
                        ).evaluate_all("els => els.map(el => el.href)") or []
                    except Exception:
                        hrefs = []
                    ids = {listing_id_from_url(href)[0] for href in hrefs}
                    ids.discard("")
                    return ids

                resume_ok = True
                ids_before_resume = _visible_ids_for_resume()
                for target_page in range(2, resume_page + 1):
                    next_btn = None
                    pagination = page.query_selector(
                        'nav[aria-label="pagination navigation"].MuiPagination-root'
                    )
                    if pagination:
                        next_btn = pagination.query_selector(
                            f'button[aria-label="Go to page {target_page}"]'
                        )
                    if not next_btn:
                        next_btn = page.query_selector('ul.pagination a.page-link[aria-label="Next"]')
                    if not next_btn:
                        next_btn = page.query_selector('ul.pagination a.page-link[aria-label="Siguiente"]')
                    if not next_btn:
                        next_btn = page.query_selector('button[aria-label="Go to next page"]')
                    if not next_btn or _is_disabled_pagination_control(next_btn):
                        resume_ok = False
                        break
                    try:
                        next_btn.click()
                    except Exception:
                        resume_ok = False
                        break
                    changed = False
                    for _ in range(20):
                        page.wait_for_timeout(400)
                        ids_now = _visible_ids_for_resume()
                        if ids_now and ids_now != ids_before_resume:
                            changed = True
                            ids_before_resume = ids_now
                            break
                    if not changed:
                        resume_ok = False
                        break
                if resume_ok:
                    current_url = page.url
                    network_cursor = len(network_events)
                    report["resumed_from_checkpoint"] = True
                    report["resume_from_page"] = resume_page
                else:
                    stop_reason = "RESUME_NAVIGATION_FAILED"
                    report.update({
                        "discovery_degraded": True,
                        "run_aborted": True,
                        "abort_reason": stop_reason,
                        "resume_navigation_failed": True,
                    })
                    all_page_reports.extend(page_reports)
                    search_reports.append({
                        "search_url": start_url,
                        "pages_fetched": len(page_reports),
                        "pages_attempted": len(page_reports),
                        "unique_listings": len(discovered),
                        "stop_reason": stop_reason,
                    })
                    break

            while True:
                if max_pages is not None and pages_visited >= max_pages:
                    stop_reason = "MAX_PAGES_REACHED"
                    break
                if max_urls is not None and len(discovered) >= max_urls:
                    stop_reason = "MAX_UNIQUE_URLS_REACHED"
                    break

                raw_html = page.content()
                visible_result_text = ""
                try:
                    visible_result_text = page.locator("body").inner_text(timeout=3000)
                except Exception:
                    pass
                body_text = re.sub(r"<script\b.*?</script>", " ", raw_html, flags=re.I|re.S, count=50)
                body_text = re.sub(r"<[^>]+>", " ", body_text)
                body_text = re.sub(r"\s+", " ", body_text).strip().lower()
                if any(p in body_text for p in ["access denied", "forbidden", "too many requests", "cloudflare"]):
                    blocked_pages += 1
                    if blocked_pages >= 2:
                        stop_reason = "BLOCKED"
                        break

                embedded_records, diagnostics = _extract_page_records(raw_html, current_url)
                card_records = _scroll_and_extract_cards(page, current_url)
                hrefs = [item["url"] for item in card_records if item.get("url")]
                if not hrefs:
                    hrefs = _scroll_and_extract(page, current_url)
                visible_total = _reported_results_from_visible_text(visible_result_text)
                if visible_total and not diagnostics.get("expected_results"):
                    diagnostics = dict(diagnostics)
                    diagnostics["expected_results"] = visible_total
                    diagnostics["reported_results_source"] = "visible_page_text"
                cards_count = len(hrefs)
                recent_network = network_events[network_cursor:]
                api_records = []
                for event in recent_network:
                    api_records.extend(event.get("_api_records", []))
                result_statuses = [
                    event.get("status")
                    for event in recent_network
                    if event.get("event") == "response"
                    and "/properties" in str(event.get("path", "")).lower()
                ]
                # In the live SPA, react-engine-props can retain a broader
                # bootstrap/search payload while the visible DOM represents
                # the actual current paginator page. Prefer those rendered
                # card links whenever they exist; use embedded data only as
                # the fallback for SSR/legacy pages without DOM cards.
                records = _extract_records_from_hrefs(hrefs, current_url) if hrefs else embedded_records
                if not records and api_records:
                    records = api_records
                    diagnostics = dict(diagnostics)
                    diagnostics["method"] = "browser_results_endpoint"
                if hrefs and embedded_records:
                    records = _merge_embedded_metadata(records, embedded_records)
                card_by_path = {
                    _path_key(item.get("url", "")): item
                    for item in card_records
                    if item.get("url")
                }
                for record in records:
                    card = card_by_path.get(_path_key(record.get("url", "")))
                    if card:
                        # Keep the card evidence separate from embedded data;
                        # no position/ID join is used to obtain the price.
                        record.update({
                            "card_price_raw": card.get("card_price_raw", ""),
                            "card_currency": card.get("card_currency", ""),
                            "card_price_uf": card.get("card_price_uf"),
                            "card_price_clp": card.get("card_price_clp"),
                            "card_price_status": card.get("card_price_status", "PRICE_NOT_AVAILABLE"),
                            "price_source": "LISTING_CARD",
                            "card_class": card.get("card_class", ""),
                            "card_testid": card.get("card_testid", ""),
                        })
                records = [record for record in records if _matches_requested_state(record, estado)]
                page_ids = [r["listing_id"] for r in records if r.get("listing_id")]
                signature = _page_signature(page_ids) if page_ids else _page_signature([r["url"] for r in records])

                def _pagination_diagnostic():
                    def _control_info(selector):
                        control = page.query_selector(selector)
                        if not control:
                            return None
                        try:
                            disabled_attr = control.get_attribute("disabled")
                            aria_disabled = control.get_attribute("aria-disabled")
                            class_name = control.get_attribute("class") or ""
                            disabled = bool(disabled_attr is not None or aria_disabled == "true" or "disabled" in class_name.lower())
                            text = (control.inner_text() or "").strip()
                            return {
                                "selector": selector,
                                "text": text,
                                "disabled": disabled,
                                "aria_disabled": aria_disabled,
                                "state": "disabled" if disabled else "enabled",
                            }
                        except Exception:
                            return {"selector": selector, "text": "", "disabled": False, "state": "unknown"}

                    current_btn = page.query_selector(
                        'nav[aria-label="pagination navigation"] button[aria-current="true"]'
                    )
                    current_page = ""
                    if current_btn:
                        try:
                            current_page = (current_btn.inner_text() or "").strip()
                        except Exception:
                            current_page = ""
                    try:
                        current_page_number = int(current_page)
                    except (TypeError, ValueError):
                        current_page_number = pages_visited + 1
                    next_info = _control_info(
                        f'nav[aria-label="pagination navigation"] button[aria-label="Go to page {current_page_number + 1}"]'
                    )
                    if not next_info:
                        next_info = _control_info('button[aria-label="Go to next page"]')
                    if not next_info:
                        next_info = _control_info('ul.pagination a.page-link[aria-label="Next"]')
                    if not next_info:
                        next_info = _control_info('ul.pagination a.page-link[aria-label="Siguiente"]')
                    parsed = urlparse(page.url)
                    params = {k: v for k, v in parse_qsl(parsed.query, keep_blank_values=True)}
                    return {
                        "page_number": pages_visited + 1,
                        "current_url": page.url,
                        "request_params": params,
                        "cursor": params.get("cursor", ""),
                        "offset": params.get("offset", params.get("skip", "")),
                        "page_param": params.get("pagina", params.get("page", "")),
                        "first_listing_id": page_ids[0] if page_ids else "",
                        "last_listing_id": page_ids[-1] if page_ids else "",
                        "listing_ids_hash": signature,
                        "dom_result_count": cards_count,
                        "next_button_enabled": bool(next_info and not next_info.get("disabled")),
                        "next_button_text": next_info.get("text", "") if next_info else "",
                        "next_button_state": next_info.get("state", "absent") if next_info else "absent",
                        "next_button": next_info or {},
                    }

                if not records:
                    report.update({
                        "discovery_degraded": True,
                        "run_aborted": True,
                        "abort_reason": "HTTP_200_EXPECTED_RESULTS_BUT_ZERO_URLS",
                    })
                    stop_reason = "HTTP_200_EXPECTED_RESULTS_BUT_ZERO_URLS"

                new_on_page = 0
                dup_on_page = 0
                unique_before_price_on_page = 0
                below_price_on_page = 0
                invalid_price_on_page = 0
                for rec in records:
                    if requested_commune and not matches_requested_commune(rec.get("url", ""), requested_commune):
                        continue
                    # Check limit before each URL addition
                    if max_urls is not None and len(discovered) >= max_urls:
                        break
                    if rec["url"] in seen_urls or (rec["listing_id"] and rec["listing_id"] in seen_ids):
                        dup_on_page += 1
                        continue
                    seen_urls.add(rec["url"])
                    if rec["listing_id"]:
                        seen_ids.add(rec["listing_id"])
                    unique_before_price_on_page += 1
                    if min_price_clp is not None:
                        published_clp = published_price_clp(rec, uf_valor_clp)
                        if published_clp is None:
                            invalid_price_on_page += 1
                            continue
                        if published_clp < min_price_clp:
                            below_price_on_page += 1
                            continue
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
                    if max_urls is not None and len(discovered) >= max_urls:
                        break

                page_reports.append({
                    "page": pages_visited + 1,
                    "batch_number": _batch_number_for_page(pages_visited + 1),
                    "batch_page_from": _batch_bounds(_batch_number_for_page(pages_visited + 1))[0],
                    "batch_page_to": _batch_bounds(_batch_number_for_page(pages_visited + 1))[1],
                    "completed": bool(records),
                    "cards_detected": cards_count,
                    "urls_extracted": len(records),
                    "new_unique_urls": new_on_page,
                    "duplicates": dup_on_page,
                    "unique_before_price": unique_before_price_on_page,
                    "below_min_price_excluded": below_price_on_page,
                    "invalid_price_excluded": invalid_price_on_page,
                    "first_listing_id": page_ids[0] if page_ids else "",
                     "last_listing_id": page_ids[-1] if page_ids else "",
                     "listing_ids": list(page_ids),
                     "result_statuses": result_statuses,
                     "challenge_detected": any(
                         event.get("recaptcha_required") or event.get("status") == 403
                         for event in recent_network
                     ),
                    "page_signature": signature,
                    "overlap_with_previous": len(set(page_ids) & previous_page_ids),
                    "discovery_method": "dom_pagination" if hrefs else diagnostics.get("method", "embedded_payload"),
                    "scope_filtered": diagnostics.get("expected_results"),
                    "reported_results": diagnostics.get("expected_results"),
                    "pages_expected": _expected_page_count(
                        diagnostics.get("expected_results"),
                        _pagination_page_size(cards_count, len(records)),
                    ),
                    "network_requests": [
                        {
                            key: value for key, value in event.items()
                            if key != "_api_records"
                        }
                        for event in network_events[network_cursor:]
                        if event.get("event") in {"request", "response", "requestfailed"}
                    ],
                })
                page_reports[-1].update(_pagination_diagnostic())
                network_cursor = len(network_events)
                report["reported_results"] = report.get("reported_results") or diagnostics.get("expected_results")
                report["expected_results"] = report.get("expected_results") or diagnostics.get("expected_results")
                report["pages_expected"] = report.get("pages_expected") or _expected_page_count(
                    report.get("expected_results"), _pagination_page_size(cards_count, len(records))
                )
                report["pages_fetched"] = len(page_reports)
                report["raw_listings_found"] = report.get("raw_listings_found", 0) + len(records)
                report["unique_before_price"] = report.get("unique_before_price", 0) + unique_before_price_on_page
                report["below_min_price_excluded"] = report.get("below_min_price_excluded", 0) + below_price_on_page
                report["invalid_price_excluded"] = report.get("invalid_price_excluded", 0) + invalid_price_on_page
                report["duplicates"] = report.get("duplicates", 0) + dup_on_page
                previous_page_ids = set(page_ids)
                if pages_visited > 0 and new_on_page > 0:
                    report["pagination_working"] = True

                print(f"  PW page {pages_visited+1}: cards={cards_count} extracted={len(records)} new={new_on_page} dup={dup_on_page} total={len(discovered)}")
                _save_atomic(discovered)
                _save_checkpoint(
                    checkpoint_path, batch_id, start_url, pages_visited + 1,
                    len(discovered), stop_reason, page_reports=page_reports,
                    batches=_batch_checkpoints(page_reports),
                )

                if report.get("discovery_degraded"):
                    break

                if max_urls is not None and len(discovered) >= max_urls:
                    stop_reason = "MAX_UNIQUE_URLS_REACHED"
                    break

                if pages_visited > 0 and new_on_page == 0:
                    if dup_on_page > 0:
                        stop_reason = "PAGINATION_DEGRADED_REPEATED_PAGE"
                        report.update({
                            "repeated_page": True,
                            "repeated_cursor": True,
                            "discovery_degraded": True,
                            "run_aborted": True,
                            "abort_reason": stop_reason,
                        })
                        break
                    # A page may legitimately contain only listings removed
                    # by the local price gate.  It still advances pagination.
                    if below_price_on_page + invalid_price_on_page < len(records):
                        stop_reason = "PAGINATION_DEGRADED_NO_NEW_UNIQUE_LISTINGS"
                        report.update({
                            "discovery_degraded": True,
                            "run_aborted": True,
                            "abort_reason": stop_reason,
                        })
                        break

                if signature in page_signatures:
                    stop_reason = "REPEATED_PAGE"
                    report.update({
                        "discovery_degraded": True,
                        "run_aborted": True,
                        "abort_reason": "PAGINATION_DEGRADED_REPEATED_PAGE",
                    })
                    break
                page_signatures.add(signature)

                pages_visited += 1
                # Check limit before next network request
                if max_urls is not None and len(discovered) >= max_urls:
                    stop_reason = "MAX_UNIQUE_URLS_REACHED"
                    break

                next_btn = page.query_selector('ul.pagination a.page-link[aria-label="Next"]')
                if not next_btn:
                    next_btn = page.query_selector('ul.pagination a.page-link[aria-label="Siguiente"]')
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

                if _is_disabled_pagination_control(next_btn):
                    stop_reason = "LAST_PAGE_REACHED"
                    break

                ids_before = set(page_ids)
                previous_signature = signature
                response_count_before = len(network_events)
                # Check limit before click (which triggers network)
                if max_urls is not None and len(discovered) >= max_urls:
                    stop_reason = "MAX_UNIQUE_URLS_REACHED"
                    break
                try:
                    next_btn.click()
                    # The current SPA can paginate from its already-loaded
                    # result state and update only the DOM/URL fragment.  In
                    # other deployments it emits a protected properties
                    # response; both paths are observed here.
                    changed = False
                    for _ in range(20):
                        page.wait_for_timeout(500)
                        new_hrefs = _scroll_and_extract(page, page.url)
                        candidate_ids = {listing_id_from_url(h)[0] for h in new_hrefs}
                        candidate_ids.discard("")
                        if candidate_ids and candidate_ids != ids_before:
                            changed = True
                            break
                        if len(network_events) > response_count_before and any(
                            event.get("status", 0) >= 400 or event.get("recaptcha_required")
                            for event in network_events[response_count_before:]
                        ):
                            break
                    if not changed:
                        recent = network_events[response_count_before:]
                        challenge_detected = any(
                            event.get("status", 0) == 403
                            and (
                                event.get("recaptcha_required")
                                or event.get("body_marker") == "recaptcha_required"
                            )
                            or event.get("recaptcha_required")
                            for event in recent
                        )
                        if challenge_detected:
                            stop_reason = "RECAPTCHA_BLOCKING_BROWSER_FLOW"
                        else:
                            stop_reason = "PAGINATION_DEGRADED_PAGE_DID_NOT_CHANGE"
                        attempt_page = pages_visited + 1
                        attempt_diagnostic = _pagination_diagnostic()
                        attempt_diagnostic.update({
                            "page": attempt_page,
                            "batch_number": _batch_number_for_page(attempt_page),
                            "batch_page_from": _batch_bounds(_batch_number_for_page(attempt_page))[0],
                            "batch_page_to": _batch_bounds(_batch_number_for_page(attempt_page))[1],
                            "completed": False,
                            "cards_detected": 0,
                            "urls_extracted": 0,
                            "new_unique_urls": 0,
                            "duplicates": 0,
                            "unique_before_price": 0,
                            "below_min_price_excluded": 0,
                            "invalid_price_excluded": 0,
                            "first_listing_id": "",
                            "last_listing_id": "",
                            "listing_ids": [],
                            "page_signature": "",
                            "overlap_with_previous": 0,
                            "result_statuses": [
                                event.get("status") for event in recent
                                if event.get("event") == "response"
                                and "/properties" in str(event.get("path", "")).lower()
                            ],
                            "challenge_detected": challenge_detected,
                            "request_status": stop_reason,
                            "network_requests": [
                                {key: value for key, value in event.items() if key != "_api_records"}
                                for event in recent
                                if event.get("event") in {"request", "response", "requestfailed"}
                            ],
                        })
                        page_reports.append(attempt_diagnostic)
                        report.update({
                            "discovery_degraded": True,
                            "run_aborted": True,
                            "abort_reason": stop_reason,
                            "challenge_detected": challenge_detected,
                        })
                        _save_atomic(discovered)
                        _save_checkpoint(
                            checkpoint_path, batch_id, start_url, pages_visited,
                            len(discovered), stop_reason, page_reports=page_reports,
                            batches=_batch_checkpoints(page_reports),
                            next_page=attempt_page,
                        )
                        break
                except Exception as e:
                    next_failures += 1
                    if next_failures >= 2:
                        stop_reason = "PLAYWRIGHT_ERROR"
                        report.update({
                            "discovery_degraded": True,
                            "run_aborted": True,
                            "abort_reason": "PLAYWRIGHT_ERROR",
                        })
                        break
                    continue

                current_url = page.url
                new_hrefs = _scroll_and_extract(page, current_url)
                new_ids = set()
                for h in new_hrefs:
                    lid, _ = listing_id_from_url(h)
                    if lid: new_ids.add(lid)
                if new_ids == ids_before:
                    stop_reason = "PAGINATION_DEGRADED_PAGE_DID_NOT_CHANGE"
                    report.update({
                        "discovery_degraded": True,
                        "run_aborted": True,
                        "abort_reason": stop_reason,
                    })
                    break

            all_page_reports.extend(page_reports)
            search_reports.append({
                "search_url": start_url,
                "pages_expected": _expected_page_count(
                    page_reports[0].get("reported_results") if page_reports else None,
                    _pagination_page_size(
                        page_reports[0].get("dom_result_count", 0),
                        page_reports[0].get("urls_extracted", 20),
                    ) if page_reports else 20,
                ),
                "pages_fetched": sum(1 for item in page_reports if item.get("completed", True)),
                "pages_attempted": len(page_reports),
                "reported_results": page_reports[0].get("reported_results") if page_reports else None,
                "raw_listings_found": sum(p.get("urls_extracted", 0) for p in page_reports),
                "unique_before_price": sum(p.get("unique_before_price", 0) for p in page_reports),
                "unique_listings": sum(p.get("new_unique_urls", 0) for p in page_reports),
                "below_min_price_excluded": sum(p.get("below_min_price_excluded", 0) for p in page_reports),
                "invalid_price_excluded": sum(p.get("invalid_price_excluded", 0) for p in page_reports),
                "stop_reason": stop_reason,
            })

        browser.close()

    stop_reason = stop_reason or "COMPLETED"
    print(f"\n  Discovery finished: {stop_reason}")
    print(f"  Pages: {len(page_reports)}, Total unique URLs: {len(discovered)}")

    report["pages"] = all_page_reports
    report["searches"] = search_reports
    report["total_pages_discovered"] = sum(
        1 for item in all_page_reports if item.get("completed", True)
    )
    report["pages_fetched"] = report["total_pages_discovered"]
    report["pages_attempted"] = len(all_page_reports)
    report["total_raw_listings"] = sum(p.get("urls_extracted", 0) for p in all_page_reports)
    report["raw_listings_found"] = report["total_raw_listings"]
    report["total_unique_listings"] = len(discovered)
    report["duplicates_removed"] = report.get("duplicates", 0)
    report["network_response_captured"] = bool(network_events)
    report["network_responses"] = list(network_events)
    report.update(_summarize_network_contract(network_events))
    result_request_events = [
        event for event in network_events
        if "/properties" in str(event.get("path", "")).lower()
        and event.get("event") == "request"
    ]
    initial_request = next(
        (event.get("request_context") for event in result_request_events
         if str((event.get("params") or {}).get("page", "")) == "1"),
        None,
    )
    page2_request = next(
        (event.get("request_context") for event in result_request_events
         if str((event.get("params") or {}).get("page", "")) == "2"),
        None,
    )
    report["initial_request_context"] = initial_request or {}
    report["page2_request_context"] = page2_request or {}
    report["missing_session_requirement"] = _compare_request_contexts(initial_request, page2_request)
    report["challenge_detected"] = bool(
        report.get("challenge_detected")
        or report.get("recaptcha_required_observed")
        or stop_reason == "RECAPTCHA_BLOCKING_BROWSER_FLOW"
    )
    report["challenge_pause_resume"] = {
        "safe_stop": bool(report.get("challenge_detected")),
        "checkpoint_preserved": bool(report.get("challenge_detected") and checkpoint_path.exists()),
        "resume_requires_new_valid_browser_session": bool(report.get("challenge_detected")),
    }
    report["batches"] = _batch_checkpoints(all_page_reports)
    report["playwright_pagination"] = len(page_reports) > 1 and not report.get("discovery_degraded")
    report["recaptcha_blocking_browser_flow"] = stop_reason == "RECAPTCHA_BLOCKING_BROWSER_FLOW"
    incomplete_searches = [
        item for item in search_reports
        if item.get("reported_results")
        and item.get("pages_expected")
        and item.get("pages_fetched", 0) < item.get("pages_expected", 0)
    ]
    if incomplete_searches:
        if report.get("method") == "ssr_embedded_payload":
            report["ssr_page_limit"] = True
        else:
            report["browser_batch_limit"] = True
        report["pagination_complete"] = False
        if not report.get("discovery_degraded"):
            report.update({
                "discovery_degraded": True,
                "run_aborted": True,
                "abort_reason": "PAGINATION_INCOMPLETE_EXPECTED_PAGE_COUNT_NOT_REACHED",
            })
    else:
        report["pagination_complete"] = bool(search_reports) and not report.get("discovery_degraded")
    if all_page_reports:
        report["first_page_unique"] = all_page_reports[0].get("new_unique_urls", 0)
        report["last_page_unique"] = all_page_reports[-1].get("new_unique_urls", 0)
        report["middle_pages_unique"] = sum(p.get("new_unique_urls", 0) for p in all_page_reports[1:-1])
        report["first_page_only_percent"] = round(
            100 * report["first_page_unique"] / max(1, len(discovered)), 2
        )
        report["first_5_pages_validated"] = (
            len(all_page_reports) >= 5
            and not report.get("discovery_degraded")
            and all(p.get("new_unique_urls", 0) > 0 for p in all_page_reports[:5])
            and all(p.get("overlap_with_previous", 0) == 0 for p in all_page_reports[1:5])
        )
    report["full_scope_discovery_working"] = bool(
        report.get("first_5_pages_validated")
        and not report.get("discovery_degraded")
        and stop_reason in {"LAST_PAGE_REACHED", "NEXT_NOT_FOUND", "NEXT_DISABLED", "COMPLETED"}
    )

    _save_checkpoint(
        checkpoint_path, batch_id, start_url if 'start_url' in dir() else "",
        max((item.get("page", 0) for item in all_page_reports if item.get("completed", True)), default=0),
        len(discovered), stop_reason, all_page_reports,
        batches=report.get("batches", []),
        next_page=(max((item.get("page", 0) for item in all_page_reports), default=0) + 1),
        challenge_detected=bool(report.get("challenge_detected")),
    )
    # Keep the progress/checkpoint evidence whenever the run stopped because
    # the browser encountered a challenge or another degraded state.  A later
    # legitimate browser session can resume from the recorded batch without
    # treating the failed batch as an empty end-of-results page.
    if progress_path.exists() and not report.get("challenge_detected") and not report.get("discovery_degraded"):
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


def _save_checkpoint(path, batch_id, search_url, last_page, total_urls, stop_reason, page_reports=None,
                     batches=None, next_page=None, challenge_detected=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    cp = {
        "batch_id": batch_id, "search_url": search_url,
        "last_completed_page": last_page, "total_unique_urls": total_urls,
        "stop_reason": stop_reason, "page_reports": page_reports or [],
        "batches": batches or _batch_checkpoints(page_reports or []),
        "next_page": next_page if next_page is not None else last_page + 1,
        "challenge_detected": bool(challenge_detected),
        "resume_available": bool(challenge_detected),
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
        "property_types_requested": str(tipo or ""),
        "property_types_available": [item["slug"] for item in property_type_catalog()],
        "property_types_executed": [],
        "route_errors": [],
    }

    if max_urls is not None and max_urls <= 0:
        return discovered

    if not start_urls:
        route_commune = _search_route_commune(comuna)
        start_urls = []
        for spec in property_type_specs(tipo):
            report["property_types_executed"].append(spec["slug"])
            start_urls.append(build_ssr_search_url(
                config,
                operacion=operacion,
                tipo=spec["route_slug"],
                region=region,
                comuna=route_commune,
                pagina=1,
                estado=estado,
                publicador=publicador,
                precio_desde=precio_desde,
                precio_hasta=precio_hasta,
            ))
        if use_playwright:
            # The current browser paginator is protected when legacy price
            # query parameters are replayed on every page.  Keep the approved
            # price threshold as a local discovery gate and let the browser
            # paginate the unmodified result page legitimately.
            start_urls = [_remove_price_params(url) for url in start_urls]
    else:
        # Explicit URLs remain supported for callers that already built a
        # controlled search set.  Do not infer or broaden their scope.
        report["property_types_executed"] = [str(tipo or "")]

    if use_playwright:
        result = discover_via_playwright(start_urls, max_pages, max_urls, batch_id, discovered, seen_urls, seen_ids,
                                         proxy_manager=proxy_manager, block_resources=block_resources,
                                         requested_commune=comuna, estado=estado,
                                         min_price_clp=precio_desde, uf_valor_clp=config.uf_valor_clp,
                                         report=report)
    else:
        result = discover_via_ssr(start_urls, max_pages, max_urls, batch_id, discovered, seen_urls, seen_ids,
                                  requested_commune=comuna, estado=estado,
                                  min_price_clp=precio_desde, uf_valor_clp=config.uf_valor_clp,
                                  report=report)

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
    report["canonical_url_duplicates"] = report.get("duplicates", 0)
    report["raw_url_occurrences"] = report.get("raw_listings_found", report.get("total_raw_listings", len(result)))
    report["unique_before_price"] = report.get("unique_before_price", report["unique_urls"])
    report["unique_after_price"] = report["unique_urls"]
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

