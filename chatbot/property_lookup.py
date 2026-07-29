import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from typing import Any, Dict, Iterable, Optional

from config import Config
from .utils import safe_int_conversion

PROPERTY_COLLECTION_NAME = Config.PROPERTY_COLLECTION_NAME


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _regex(value: str) -> Dict[str, Any]:
    return {"$regex": re.escape(value), "$options": "i"}


def _non_empty(values: Iterable[Any]) -> list[Any]:
    return [v for v in values if v not in (None, "", [])]


def build_property_lookup_queries(raw_value: Any) -> list[Dict[str, Any]]:
    """
    Construye consultas robustas para ubicar una propiedad en la colección
    Prop360 nueva, manteniendo compatibilidad parcial con el esquema antiguo.
    """
    value = _clean_text(raw_value)
    if not value:
        return []

    value_int = safe_int_conversion(value)
    value_lower = value.lower()

    queries: list[Dict[str, Any]] = [
        {"codigo": value},
        {"codigo": value_int},
        {"codigo": {"$in": [value, value_int]}},
        {"publicaciones.portal_inmobiliario.url_pi": value},
        {"publicaciones.portal_inmobiliario.url_mercado_libre": value},
        {"publicaciones.procasa.url_procasa": value},
        {"publicaciones.toctoc.url_toctoc": value},
        {"ubicacion.comuna": value},
        {"ubicacion.comuna": _regex(value)},
        {"ubicacion.region": value},
        {"ubicacion.region": _regex(value)},
        {"estado.ejecutivo": value},
        {"estado.ejecutivo": _regex(value)},
        {"publicaciones.procasa.url_procasa": _regex(value)},
        {"metadata.source_url": value},
        {"metadata.source_url": _regex(value)},
        {"source_url": value},
        {"source_url": _regex(value)},
        {"publicaciones.portal_inmobiliario.url_mercado_libre": _regex(value)},
        {"publicaciones.portal_inmobiliario.url_pi": _regex(value)},
        {"publicaciones.toctoc.url_toctoc": _regex(value)},
        {"publicaciones.yapo.url_yapo": _regex(value)},
        {"publicaciones.codigo_internacional": value},
        {"publicaciones.codigo_internacional": value_int},
        {"publicaciones.portal_inmobiliario.codigo_pi": value},
        {"publicaciones.portal_inmobiliario.codigo_pi": value_int},
        {"publicaciones.yapo.codigo_yapo": value},
        {"publicaciones.yapo.codigo_yapo": value_int},
        {"codigo_pi": value},
        {"codigo_pi": value_int},
        {"codigo_mercadolibre": value},
        {"codigo_mercadolibre": value_int},
        {"codigo_yapo": value},
        {"codigo_yapo": value_int},
        {"codigo_internacional": value},
        {"codigo_internacional": value_int},
        {"toctoc.enlace": value},
        {"toctoc.enlace": _regex(value)},
    ]

    if "http" in value_lower or ".cl" in value_lower or "/" in value:
        queries.extend([
            {"publicaciones.yapo.url_yapo": value},
            {"publicaciones.procasa.url_procasa": _regex(value)},
            {"metadata.source_url": _regex(value)},
            {"source_url": _regex(value)},
            {"publicaciones.portal_inmobiliario.url_mercado_libre": _regex(value)},
            {"publicaciones.portal_inmobiliario.url_pi": _regex(value)},
            {"publicaciones.toctoc.url_toctoc": _regex(value)},
            {"publicaciones.yapo.url_yapo": _regex(value)},
        ])

    # Deduplicar por representación para evitar consultas redundantes.
    seen = set()
    unique_queries = []
    for query in queries:
        key = repr(query)
        if key in seen:
            continue
        seen.add(key)
        unique_queries.append(query)
    return unique_queries


def find_property_by_any_identifier(db, raw_value: Any, collection_name: str = PROPERTY_COLLECTION_NAME):
    collection = db[collection_name]
    for query in build_property_lookup_queries(raw_value):
        prop = collection.find_one(query)
        if prop:
            return prop
    return None


BACKUP_COLLECTION = "universo_cartera"


def find_property_in_any_collection(db, raw_value: Any) -> dict | None:
    """Busca en universo_cartera primero, luego en universo_cartera_prop360 como fallback."""
    prop = find_property_by_any_identifier(db, raw_value, PROPERTY_COLLECTION_NAME)
    if prop:
        return prop
    if PROPERTY_COLLECTION_NAME != BACKUP_COLLECTION:
        prop = find_property_by_any_identifier(db, raw_value, BACKUP_COLLECTION)
        if prop:
            prop["_lookup_fallback"] = True
    return prop


def get_prop_location(prop: Dict[str, Any]) -> Dict[str, Any]:
    ubicacion = prop.get("ubicacion", {}) or {}
    return {
        "region": _clean_text(ubicacion.get("region") or prop.get("region")),
        "comuna": _clean_text(ubicacion.get("comuna") or prop.get("comuna")),
        "sector": _clean_text(ubicacion.get("sector") or prop.get("sector")),
        "direccion": _clean_text(
            ubicacion.get("calle")
            or ubicacion.get("direccion_referencial")
            or prop.get("direccion")
            or prop.get("nombre_calle")
        ),
    }


def get_prop_operation(prop: Dict[str, Any], operation_override: Optional[str] = None) -> Dict[str, Any]:
    tipo_operacion = prop.get("tipo_operacion", {}) or {}
    return {
        "tipo": _clean_text(tipo_operacion.get("tipo") or prop.get("tipo")),
        "operacion": _clean_text(
            operation_override
            or ("Venta" if tipo_operacion.get("venta") else "Arriendo" if tipo_operacion.get("arriendo") else prop.get("operacion"))
        ),
        "precio_uf": (
            tipo_operacion.get("precio_venta", {}) or {}
        ).get("precio_uf")
        or (tipo_operacion.get("precio_arriendo", {}) or {}).get("precio_uf")
        or prop.get("precio_uf"),
        "precio_clp": (
            tipo_operacion.get("precio_venta", {}) or {}
        ).get("precio_clp")
        or (tipo_operacion.get("precio_arriendo", {}) or {}).get("precio_clp")
        or prop.get("precio_clp"),
    }


def get_prop_executive(prop: Dict[str, Any]) -> str:
    estado = prop.get("estado", {}) or {}
    for key in ("ejecutivo", "captador", "responsable"):
        value = _clean_text(estado.get(key) or prop.get(key))
        if value:
            return value
    return ""


PORTAL_ALIASES = {
    "mercadolibre": "mercadolibre",
    "casa.mercadolibre.cl": "mercadolibre",
    "mercadolibre.cl": "mercadolibre",
    "portalinmobiliario": "portal_inmobiliario",
    "portalinmobiliario.com": "portal_inmobiliario",
    "www.portalinmobiliario.com": "portal_inmobiliario",
    "toctoc": "toctoc",
    "toctoc.com": "toctoc",
    "yapo": "yapo",
    "yapo.cl": "yapo",
    "procasa": "procasa",
    "procasa.cl": "procasa",
}
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def canonical_portal(url: str) -> str:
    host = (urlsplit(str(url or "")).hostname or "").lower().removeprefix("www.")
    if host.endswith("mercadolibre.cl"):
        return "mercadolibre"
    if host.endswith("portalinmobiliario.com") or host.endswith("portalinmobiliario.cl"):
        return "portal_inmobiliario"
    if host.endswith("toctoc.com"):
        return "toctoc"
    if host.endswith("yapo.cl"):
        return "yapo"
    if host.endswith("procasa.cl"):
        return "procasa"
    return ""


def normalize_property_url(url: str) -> str:
    """Normaliza una URL de publicaci?n sin perder su identidad comercial."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower().removeprefix("www.")
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/").rstrip("/") or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")]
    return urlunsplit(("https", netloc, path, urlencode(sorted(query)), "")).lower()


def extract_property_external_id(url: str, portal: Optional[str] = None) -> Optional[str]:
    value = str(url or "")
    portal = portal or canonical_portal(value)
    if portal in {"mercadolibre", "portal_inmobiliario"}:
        match = re.search(r"\bMLC[-_]?\d+\b", value, re.I)
        return match.group(0).upper().replace("_", "-") if match else None
    if portal == "toctoc":
        match = re.search(r"/([a-f0-9]{32,})/?(?:[?#]|$)", value, re.I)
        return match.group(1).lower() if match else None
    if portal == "yapo":
        match = re.search(r"/(\d{6,})/?(?:[?#]|$)", value)
        return match.group(1) if match else None
    if portal == "procasa":
        match = re.search(r"/(\d{4,})/?(?:[?#]|$)", value)
        return match.group(1) if match else None
    return None


def operation_from_property_url(url: str) -> Optional[str]:
    text = str(url or "").lower()
    if re.search(r"(?:arriendo|alquiler|rent)", text):
        return "arriendo"
    if re.search(r"(?:venta|vender|compraventa)", text):
        return "venta"
    return None


def lookup_property_link(db, url: str, collection_name: str = PROPERTY_COLLECTION_NAME):
    """Resolve aliases first and return (property, match metadata)."""
    portal = canonical_portal(url)
    normalized = normalize_property_url(url)
    external_id = extract_property_external_id(url, portal)
    collection = db[collection_name]
    if portal and external_id:
        prop = collection.find_one({"publicaciones.aliases": {"$elemMatch": {
            "portal": portal, "external_id": external_id, "activa": {"$ne": False}
        }}})
        if prop:
            aliases = prop.get("publicaciones", {}).get("aliases", []) or []
            alias = next((a for a in aliases if a.get("portal") == portal and a.get("external_id") == external_id), {})
            return prop, {"portal": portal, "external_id": external_id,
                          "operation": (alias.get("operacion") or operation_from_property_url(url)),
                          "url_normalized": normalized, "match_method": "portal_external_id"}
    if normalized:
        prop = collection.find_one({"publicaciones.aliases": {"$elemMatch": {
            "portal": portal, "url_normalized": normalized, "activa": {"$ne": False}
        }}})
        if prop:
            aliases = prop.get("publicaciones", {}).get("aliases", []) or []
            alias = next((a for a in aliases if a.get("url_normalized") == normalized), {})
            return prop, {"portal": portal, "external_id": alias.get("external_id") or external_id,
                          "operation": alias.get("operacion") or operation_from_property_url(url),
                          "url_normalized": normalized, "match_method": "normalized_alias"}
    return None, {"portal": portal, "external_id": external_id,
                   "operation": operation_from_property_url(url),
                   "url_normalized": normalized, "match_method": None}


def build_property_alias(url: str, portal: Optional[str] = None, operation: Optional[str] = None,
                         external_id: Optional[str] = None, active: bool = True) -> Dict[str, Any]:
    portal = portal or canonical_portal(url)
    return {
        "portal": portal,
        "operacion": operation or operation_from_property_url(url),
        "url": str(url).strip(),
        "url_normalized": normalize_property_url(url),
        "external_id": external_id or extract_property_external_id(url, portal),
        "activa": bool(active),
    }


def merge_property_aliases(existing: Any, incoming: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """Merge idempotently by portal + operation + external id + normalized URL."""
    merged = [dict(item) for item in (existing or []) if isinstance(item, dict)]
    for alias in incoming:
        candidate = dict(alias)
        key = (
            candidate.get("portal"), candidate.get("operacion"),
            candidate.get("external_id"), candidate.get("url_normalized"),
        )
        found = False
        for idx, current in enumerate(merged):
            current_key = (
                current.get("portal"), current.get("operacion"),
                current.get("external_id"), current.get("url_normalized"),
            )
            if key == current_key:
                merged[idx] = {**current, **candidate}
                found = True
                break
        if not found:
            merged.append(candidate)
    return merged
