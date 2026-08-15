from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from config import AppConfig
from proxy_manager import ProxyManager

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def _utcnow(): return datetime.now(timezone.utc).isoformat()


def _normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    return urlunparse(parsed._replace(fragment=""))


def build_ssr_search_url(config, operacion="venta", tipo="departamento", region="metropolitana", comuna="la-florida"):
    route_commune = "santiago" if comuna == "santiago-centro" else comuna
    return config.search_ssr_template.format(operacion=operacion, tipo=tipo, region=region, comuna=route_commune)


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
    valid_prefixes = ("/propiedades/", "/propiedad/")
    if not any(p in low for p in valid_prefixes):
        return False
    path = urlparse(url).path.rstrip("/")
    return bool(re.search(r"/\d+$", path) or re.search(r"-\d+$", path) or re.search(r"/[a-f0-9]{40}$", path))


def listing_id_from_url(url: str) -> tuple[str, str]:
    m = re.search(r"/(\d+)$", url)
    if m: return m.group(1), "url_numeric_id"
    m = re.search(r"-(\d+)$", url)
    if m: return m.group(1), "url_numeric_id"
    m = re.search(r"/([a-f0-9]{40})$", url)
    if m: return m.group(1), "url_hash"
    m = re.search(r"/([a-f0-9]{20,})$", url)
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
                     requested_commune=None):
    if max_urls is not None and max_urls <= 0:
        return discovered
    config = AppConfig()
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
                break
            nd = _extract_next_data(html)
            if nd:
                props_hash = hash(json.dumps(nd.get("props", {}).get("pageProps", {}).get("propiedades", {}).get("results", []), sort_keys=True))
                if prev_hash is not None and props_hash == prev_hash:
                    print("  SSR page repeated, stopping.")
                    break
                prev_hash = props_hash
                total = nd.get("props", {}).get("pageProps", {}).get("propiedades", {}).get("total", 0)
                print(f"  SSR page {pages_visited+1}: total={total}")
            records = extract_metadata_from_next_data(nd) if nd else []
            new_on_page = 0
            for rec in records:
                if requested_commune and not matches_requested_commune(rec.get("url", ""), requested_commune):
                    continue
                if rec["url"] in seen_urls or (rec["listing_id"] and rec["listing_id"] in seen_ids):
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
                    return discovered
            print(f"  Page {pages_visited+1}: {new_on_page} new")
            if new_on_page == 0:
                break
            pages_visited += 1
            break
    return discovered


GW_LISTA_ENDPOINT = "/gw-lista-seo/propiedades"
GW_LISTA_ORDER = 1
GW_LISTA_MIN_COMMUNE_PRECISION = 0.95


def _gw_filter_payload_from_url(url: str) -> list[dict[str, Any]]:
    """Read the current filter contract from a real Toctoc BFF request."""
    values = parse_qs(urlparse(url).query).get("filtros", [])
    if not values:
        raise ValueError("GW_LISTA_FILTERS_MISSING")
    payload = json.loads(values[0])
    if not isinstance(payload, list) or not payload:
        raise ValueError("GW_LISTA_FILTERS_INVALID")
    return payload


def build_gw_lista_request(
    *,
    base_url: str,
    filtros: list[dict[str, Any]],
    operacion: str,
    tipo: str,
    region: str,
    comuna: str,
    page: int,
    order: int = GW_LISTA_ORDER,
) -> dict[str, Any]:
    """Build one auditable request for the current gw-lista-seo contract."""
    if page < 1:
        raise ValueError("page must be >= 1")
    endpoint = base_url.rstrip("/") + GW_LISTA_ENDPOINT
    query = urlencode({
        "filtros": json.dumps(filtros, ensure_ascii=False, separators=(",", ":")),
        "order": str(order),
        "page": str(page),
    })
    effective = {}
    for item in filtros:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", ""))
        values = item.get("values") or item.get("value") or []
        effective[item_id] = values
    return {
        "url": f"{endpoint}?{query}",
        "page": page,
        "order": order,
        "requested_filters": {
            "operacion": operacion,
            "tipo_propiedad": tipo,
            "region": region,
            "comuna": comuna,
        },
        "effective_numeric_filters": effective,
        "filters": filtros,
    }


def _gw_page_signature(ids: list[str]) -> str:
    import hashlib
    return hashlib.sha256(",".join(ids).encode("utf-8")).hexdigest()


def _gw_response_items(data: Any, page_num: int, requested_commune: str) -> tuple[list[dict], dict]:
    if not isinstance(data, dict):
        raise ValueError("INVALID_RESPONSE")
    results = data.get("results")
    if not isinstance(results, list):
        raise ValueError("INVALID_RESPONSE_RESULTS")
    metadata = {
        "page": data.get("page", page_num),
        "total": data.get("total"),
        "results_raw": len(results),
    }
    records = []
    for prop in results:
        if not isinstance(prop, dict):
            continue
        url_ficha = str(prop.get("urlFicha", "") or prop.get("url", "")).strip()
        if not url_ficha or not is_listing_detail_url(url_ficha):
            continue
        lid = str(prop.get("idProperty", "") or listing_id_from_url(url_ficha)[0])
        if not lid:
            continue
        rec = {
            "url": url_ficha,
            "listing_id": lid,
            "listing_id_source": "gw_lista_seo",
            "url_format": classify_url_format(url_ficha),
            "title": str(prop.get("titulo", "")),
            "comuna": str(prop.get("comuna", "") or _extract_commune_slug(url_ficha)),
            "region": str(prop.get("region", "")),
            "operacion": "venta" if "venta" in str(prop.get("tipoOperacion", "")).lower() else "arriendo",
            "tipo_propiedad": str(prop.get("tipoPropiedad", "")).lower(),
            "tipo_operacion": str(prop.get("tipoOperacion", "")),
            "price_uf": "",
            "price_clp": "",
            "publicador": str(prop.get("imagenInmobiliaria", {}).get("alt", prop.get("clientId", ""))) if isinstance(prop.get("imagenInmobiliaria", {}), dict) else "",
            "client_id": str(prop.get("clientId", "")),
            "discovery_page": page_num,
        }
        records.append(rec)
    metadata["results_target_commune"] = sum(matches_requested_commune(r["url"], requested_commune) for r in records)
    metadata["results_wrong_commune"] = sum(not matches_requested_commune(r["url"], requested_commune) for r in records)
    return records, metadata


def discover_via_gw_lista(
    max_pages: int | None,
    max_urls: int | None,
    batch_id: str,
    discovered: list[dict],
    seen_urls: set[str],
    seen_ids: set[str],
    *,
    operacion: str,
    tipo: str,
    region: str,
    comuna: str,
    block_resources: bool = True,
) -> list[dict]:
    """Primary geographic discovery using Toctoc's live gw-lista-seo contract."""
    if max_urls is not None and max_urls <= 0:
        return discovered
    from playwright.sync_api import sync_playwright

    config = AppConfig()
    route_url = f"{config.base_url.rstrip('/')}/{operacion}/{tipo}/{_search_route_commune(region)}/{_search_route_commune(comuna)}"
    trace = {
        "source": "gw_lista_seo",
        "requested_commune": comuna,
        "requested_operation": operacion,
        "requested_type": tipo,
        "requested_region": region,
        "route_url": route_url,
        "pages": [],
        "stop_reason": "",
        "batch_id": batch_id,
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(locale="es-CL", viewport={"width": 1920, "height": 1080}, user_agent=config.user_agent)
        page = context.new_page()
        if block_resources:
            def _route(route):
                if route.request.resource_type in ("image", "media", "font"):
                    route.abort()
                else:
                    route.continue_()
            page.route("**/*", _route)
        bff_urls: list[str] = []
        page.on("request", lambda req: bff_urls.append(req.url) if GW_LISTA_ENDPOINT in req.url and "page=1" in req.url else None)
        try:
            page.goto(route_url, wait_until="domcontentloaded", timeout=config.request_timeout_seconds * 1000)
            page.wait_for_timeout(4000)
        except Exception as exc:
            trace["stop_reason"] = "HTTP_ERROR"
            trace["error"] = str(exc)[:500]
            browser.close()
            _save_gw_trace(trace, batch_id)
            return discovered
        if not bff_urls:
            trace["stop_reason"] = "INVALID_RESPONSE"
            trace["error"] = "No se capturó la solicitud real de gw-lista-seo"
            browser.close()
            _save_gw_trace(trace, batch_id)
            return discovered
        try:
            filtros = _gw_filter_payload_from_url(bff_urls[-1])
        except Exception as exc:
            trace["stop_reason"] = "INVALID_RESPONSE"
            trace["error"] = str(exc)
            browser.close()
            _save_gw_trace(trace, batch_id)
            return discovered
        trace["resolved_commune_id"] = next((
            (item.get("values") or [{}])[0].get("id")
            for item in filtros if isinstance(item, dict) and item.get("id") == "comuna"
        ), None)
        trace["resolved_operation_filter"] = next((
            item.get("values") or item.get("value")
            for item in filtros if isinstance(item, dict) and item.get("id") == "tipo-de-busqueda"
        ), None)
        seen_page_signatures: set[str] = set()
        page_num = 1
        expected = None
        while True:
            if max_pages is not None and page_num > max_pages:
                trace["stop_reason"] = "MAX_PAGES_REACHED"
                break
            request_meta = build_gw_lista_request(base_url=config.base_url, filtros=filtros,
                operacion=operacion, tipo=tipo, region=region, comuna=comuna, page=page_num)
            trace["resolved_filters"] = request_meta["effective_numeric_filters"]
            try:
                data = page.evaluate("""async (url) => { const r = await fetch(url); return {status:r.status, data: await r.json()}; }""", request_meta["url"])
                if not isinstance(data, dict) or data.get("status") != 200:
                    raise ValueError(f"HTTP_ERROR:{data.get('status') if isinstance(data, dict) else 'unknown'}")
                records, meta = _gw_response_items(data.get("data"), page_num, comuna)
            except Exception as exc:
                trace["stop_reason"] = "HTTP_ERROR" if str(exc).startswith("HTTP_ERROR") else "INVALID_RESPONSE"
                trace["error"] = str(exc)[:500]
                break
            raw_ids = [r["listing_id"] for r in records]
            signature = _gw_page_signature(raw_ids)
            expected = None
            total = meta.get("total")
            observed_page_size = meta.get("results_raw") or 0
            if not trace.get("page_size") and observed_page_size:
                trace["page_size"] = observed_page_size
            page_size = trace.get("page_size") or observed_page_size
            if isinstance(total, (int, float)) and page_size:
                expected = (int(total) + page_size - 1) // page_size
            page_report = {
                **request_meta,
                "page": page_num,
                "page_size": observed_page_size,
                "total_reported": total,
                "results_raw": meta["results_raw"],
                "results_target_commune": meta["results_target_commune"],
                "results_wrong_commune": meta["results_wrong_commune"],
                "page_signature": signature,
                "listing_ids": raw_ids,
                "first_listing_id": raw_ids[0] if raw_ids else "",
                "last_listing_id": raw_ids[-1] if raw_ids else "",
            }
            trace["pages"].append(page_report)
            if not raw_ids and meta["results_raw"] == 0:
                trace["stop_reason"] = "EMPTY_PAGE"
                break
            precision = meta["results_target_commune"] / len(records) if records else 0
            if records and precision < GW_LISTA_MIN_COMMUNE_PRECISION:
                trace["stop_reason"] = "COMMUNE_PRECISION_FAILURE"
                break
            if signature in seen_page_signatures:
                trace["stop_reason"] = "REPEATED_PAGE"
                break
            seen_page_signatures.add(signature)
            for rec in records:
                if not matches_requested_commune(rec["url"], comuna):
                    continue
                if rec["url"] in seen_urls or rec["listing_id"] in seen_ids:
                    continue
                seen_urls.add(rec["url"]); seen_ids.add(rec["listing_id"])
                rec.update({"source": "gw_lista_seo", "source_search_url": route_url,
                            "source_page_url": request_meta["url"], "page_number": page_num,
                            "discovery_page": page_num, "batch_id": batch_id,
                            "discovered_at": _utcnow()})
                discovered.append(rec)
                if max_urls is not None and len(discovered) >= max_urls:
                    trace["stop_reason"] = "MAX_URLS_REACHED"
                    break
            if trace["stop_reason"] == "MAX_URLS_REACHED":
                break
            if expected is not None and page_num >= expected:
                trace["stop_reason"] = "TOTAL_PAGES_COMPLETED"
                break
            page_num += 1
        trace["pages_expected"] = expected
        trace["pages_processed"] = len(trace["pages"])
        trace["total_reported"] = trace["pages"][0].get("total_reported") if trace["pages"] else None
        trace["raw_urls"] = sum(p["results_raw"] for p in trace["pages"])
        trace["unique_urls"] = len(discovered)
        trace["commune_precision"] = (sum(p["results_target_commune"] for p in trace["pages"]) /
                                      max(1, sum(p["results_raw"] for p in trace["pages"])))
        browser.close()
    _save_gw_trace(trace, batch_id)
    print(f"  gw-lista-seo: {trace['pages_processed']} páginas, {trace['unique_urls']} URLs, "
          f"precisión comuna={trace['commune_precision']:.1%}, stop={trace['stop_reason']}")
    return discovered


def _save_gw_trace(trace: dict, batch_id: str) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / f"discovery_gw_lista_{batch_id}.json").write_text(
        json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")


def discover_via_playwright(start_urls, max_pages, max_urls, batch_id, discovered, seen_urls, seen_ids,
                             proxy_manager=None, block_resources=True, requested_commune=None):
    if max_urls is not None and max_urls <= 0:
        return discovered
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        print("Playwright not installed. Falling back to SSR.")
        return discover_via_ssr(start_urls, max_pages, max_urls, batch_id, discovered, seen_urls, seen_ids,
                                requested_commune=requested_commune)

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

                hrefs = _scroll_and_extract(page, current_url)
                cards_count = len(hrefs)
                records = _extract_records_from_hrefs(hrefs, current_url)
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
                        page.wait_for_selector('a[href*="/propiedad/"], a[href*="/propiedades/"]', timeout=8000)
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
                          proxy_manager=None, block_resources=True):
    config = AppConfig()
    config.ensure_layout()
    batch_id = batch_id or config.generate_batch_id()
    discovered: list[dict] = []
    seen_urls: set[str] = set()
    seen_ids: set[str] = set()

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
            start_urls = [builder["url"]]
        else:
            start_urls = [build_ssr_search_url(config, operacion=operacion, tipo=tipo, region=region, comuna=route_commune)]

    if use_playwright:
        result = discover_via_playwright(start_urls, max_pages, max_urls, batch_id, discovered, seen_urls, seen_ids,
                                         proxy_manager=proxy_manager, block_resources=block_resources,
                                         requested_commune=comuna)
    else:
        # Primary geographic source: the live BFF request made by Toctoc's SSR route.
        # The legacy SSR parser remains available above for controlled diagnostics.
        result = discover_via_gw_lista(
            max_pages, max_urls, batch_id, discovered, seen_urls, seen_ids,
            operacion=operacion, tipo=tipo, region=region, comuna=comuna,
            block_resources=block_resources,
        )

    # Defensa obligatoria: Toctoc puede devolver resultados fuera de la zona
    # pedida cuando la SPA pierde parte de los parámetros de búsqueda.
    before = len(result)
    result = [r for r in result if matches_requested_commune(r.get("url", ""), comuna)]
    rejected = before - len(result)
    if rejected:
        print(f"  Commune guard: rechazadas {rejected} URLs fuera de {comuna}; aceptadas {len(result)}")
    return result


if __name__ == "__main__":
    assert is_listing_detail_url("https://www.toctoc.com/propiedades/compranuevo/departamento/la-florida/edificio-refugio-new/1384492")
    assert is_listing_detail_url("https://www.toctoc.com/propiedad/departamento-en-venta-la-florida-metropolitana-5663417")
    assert not is_listing_detail_url("https://www.toctoc.com/venta/departamento/metropolitana/la-florida")
    assert listing_id_from_url("https://www.toctoc.com/propiedades/compranuevo/departamento/la-florida/edificio-refugio-new/1384492")[0] == "1384492"
    assert classify_url_format("https://www.toctoc.com/propiedades/compranuevo/departamento/la-florida/edificio-refugio-new/1384492") == "compranuevo"
    assert classify_url_format("https://www.toctoc.com/propiedad/departamento-en-venta-la-florida-metropolitana-5663417") == "propiedad_usado"
    print("All tests passed.")
