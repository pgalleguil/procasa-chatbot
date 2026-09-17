import logging
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from typing import Any, Dict, Iterable, Optional

from config import Config
from .utils import safe_int_conversion

logger = logging.getLogger(__name__)
URL_RE = re.compile(r'https?://[^\s<>\]\)"]+', re.IGNORECASE)

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
    if isinstance(raw_value, str) and raw_value.strip().lower().startswith(("http://", "https://")):
        alias_prop, _meta = lookup_property_link(db, raw_value, collection_name)
        if alias_prop:
            return alias_prop
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


PROPERTY_AVAILABILITY_STATES = frozenset({
    "PROPERTY_FOUND",
    "PROPERTY_NOT_FOUND",
    "PROPERTY_FOUND_BUT_INACTIVE",
    "PROPERTY_FOUND_AVAILABILITY_UNKNOWN",
    "PROPERTY_FOUND_AVAILABLE",
})


def property_availability_state(prop: Dict[str, Any] | None) -> str:
    """Return the bounded availability state for an already resolved property.

    Resolving a publication and proving that it is currently available are
    separate facts.  The chatbot must not turn an unknown availability value
    into a false ``not in portfolio`` claim.
    """
    if not prop:
        return "PROPERTY_NOT_FOUND"

    state = prop.get("estado") if isinstance(prop.get("estado"), dict) else {}
    values = [
        prop.get("disponible_prop360"),
        prop.get("disponible"),
        state.get("disponible_prop360"),
        state.get("disponible"),
        state.get("active"),
        state.get("activo"),
    ]
    if any(value is True for value in values):
        return "PROPERTY_FOUND_AVAILABLE"
    if any(value is False for value in values):
        return "PROPERTY_FOUND_BUT_INACTIVE"

    text_status = " ".join(
        str(value or "").strip().casefold()
        for value in (prop.get("status"), prop.get("estado"), state.get("status"), state.get("estado"))
        if not isinstance(value, dict)
    )
    if any(token in text_status for token in ("inactive", "inactivo", "inactiva", "retirad", "vendid", "cerrad", "no disponible")):
        return "PROPERTY_FOUND_BUT_INACTIVE"
    return "PROPERTY_FOUND_AVAILABILITY_UNKNOWN"


def canonical_property_context(prop: Dict[str, Any] | None, operation_override: Optional[str] = None) -> Dict[str, Any]:
    """Build the one turn-local property identity used by downstream code."""
    if not prop:
        return {}
    location = get_prop_location(prop)
    operation = get_prop_operation(prop, operation_override=operation_override)
    return {
        "codigo": _clean_text(prop.get("codigo")),
        "comuna": location.get("comuna") or "",
        "region": location.get("region") or "",
        "tipo": operation.get("tipo") or "",
        "operacion": operation.get("operacion") or "",
        "precio_uf": operation.get("precio_uf"),
        "property_availability_state": property_availability_state(prop),
    }


_NOT_PORTFOLIO_CLAIM_RE = re.compile(
    r"(?:no\s+(?:es|est[aá]|forma\s+parte|pertenece)|fuera\s+de)"
    r".{0,80}(?:nuestro|el|la|un|una)?\s*(?:portafolio|cat[aá]logo|cartera)"
    r"|(?:no\s+(?:est[aá]|forma\s+parte|pertenece))"
    r".{0,80}(?:portafolio|cat[aá]logo|cartera)",
    re.IGNORECASE | re.DOTALL,
)


def guard_resolved_property_response(response: str, prop: Dict[str, Any] | None) -> str:
    """Replace only a false portfolio denial after a successful resolution."""
    if not prop or not _NOT_PORTFOLIO_CLAIM_RE.search(str(response or "")):
        return response

    code = _clean_text(prop.get("codigo")) or "la propiedad"
    state = property_availability_state(prop)
    if state == "PROPERTY_FOUND_BUT_INACTIVE":
        availability = "Su estado actual indica que podría no estar disponible; lo verificaremos antes de coordinar una visita."
    else:
        availability = "La propiedad está registrada en nuestro sistema y verificaré su disponibilidad actual antes de coordinar una visita."
    return f"Encontré la propiedad {code} en nuestro sistema. {availability} ¿Qué información te gustaría revisar?"


def get_prop_executive(prop: Dict[str, Any]) -> str:
    estado = prop.get("estado", {}) or {}
    for key in ("ejecutivo", "captador", "responsable"):
        value = _clean_text(estado.get(key) or prop.get(key))
        if value:
            return value
    return ""


_FALSE_PORTFOLIO_CLAIM = re.compile(
    r"(?:no\s+es|no\s+forma\s+parte|fuera\s+de)\s+(?:una\s+)?(?:propiedad\s+de\s+)?(?:nuestro|el\s+nuestro)\s+portafolio"
    r"|(?:no\s+est[aá]\s+en|no\s+pertenece\s+a)\s+(?:nuestro\s+portafolio|nuestra\s+cartera)",
    re.IGNORECASE,
)
_VISIT_SEMANTIC_TERMS = (
    "quiero", "quisiera", "me gustaría", "me gustaria", "visitar",
    "visista", "verla", "verlo", "agend", "coordin", "puedo ir",
    "podría ir", "podria ir", "mañana", "manana", "jueves", "viernes",
    "sábado", "sabado", "domingo", "hoy",
)


def is_property_reference_only(text: str) -> bool:
    """URLs identify a property, but do not by themselves confirm a visit."""
    normalized = str(text or "").casefold().strip()
    return bool(URL_RE.search(normalized)) and not any(
        term in normalized for term in _VISIT_SEMANTIC_TERMS
    )


def guard_resolved_property_identity_response(response: str, property_doc: dict,
                                              external_id: str | None = None) -> str:
    """Keep a resolved link from being downgraded by a model portfolio claim."""
    if not property_doc or not _FALSE_PORTFOLIO_CLAIM.search(str(response or "")):
        return response
    code = str(property_doc.get("codigo") or "").strip()
    external = str(
        external_id
        or (property_doc.get("_link_match") or {}).get("external_id")
        or property_doc.get("codigo_yapo")
        or ""
    ).strip()
    identity = f"código interno {code}" if code else "la publicación"
    if external:
        identity += f" y código de publicación {external}"
    state = property_availability_state(property_doc)
    if state in {"FOUND_BUT_INACTIVE", "PROPERTY_FOUND_BUT_INACTIVE"}:
        status = "La ficha fue identificada, pero actualmente aparece inactiva; un ejecutivo debe revisar si existe una alternativa vigente."
    elif state in {"FOUND_AVAILABLE", "PROPERTY_FOUND_AVAILABLE"}:
        status = "La ficha figura activa en nuestra cartera, pero la disponibilidad puntual y las condiciones actuales deben confirmarse con el ejecutivo."
    else:
        status = "La ficha fue identificada, pero no tengo la disponibilidad puntual confirmada en la información disponible."
    logger.warning(
        "[PROPERTY_IDENTITY_GUARD] resolved=%s state=%s source=deepseek_not_in_portfolio_claim",
        identity, state,
    )
    return f"Encontré la propiedad asociada a {identity}. {status}"


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
    """Resolve a publication using its canonical external identity.

    Alias records are preferred, but production has historical publication
    documents that predate aliases.  Those documents store the same external
    identity in portal-specific URL and code fields, so they must be queried
    before reporting a link as unresolved.
    """
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

    # Historical publication schemas.  The portal may have changed domains
    # (Portal Inmobiliario / Mercado Libre), while the MLC identity remains
    # stable; resolve it across both URL slots and legacy code fields.
    shared_mlc_url_fields = (
        "publicaciones.portal_inmobiliario.url_pi",
        "publicaciones.portal_inmobiliario.url_mercado_libre",
    )
    field_sets = {
        "portal_inmobiliario": {
            "url": shared_mlc_url_fields,
            "id": ("publicaciones.portal_inmobiliario.codigo_pi", "codigo_pi", "codigo_mercadolibre"),
        },
        "mercadolibre": {
            "url": shared_mlc_url_fields,
            "id": ("publicaciones.portal_inmobiliario.codigo_pi", "codigo_pi", "codigo_mercadolibre"),
        },
        "yapo": {
            "url": ("publicaciones.yapo.url_yapo", "url_yapo"),
            "id": ("publicaciones.yapo.codigo_yapo", "codigo_yapo"),
        },
        "toctoc": {
            "url": ("publicaciones.toctoc.url_toctoc", "toctoc.enlace"),
            "id": (),
        },
        "procasa": {
            "url": ("publicaciones.procasa.url_procasa", "source_url", "metadata.source_url"),
            "id": ("publicaciones.codigo_internacional", "codigo_internacional"),
        },
    }
    fields = field_sets.get(portal, {"url": (), "id": ()})
    for candidate in (str(url).strip(), normalized):
        if not candidate:
            continue
        for field in fields["url"]:
            prop = collection.find_one({field: candidate})
            if prop:
                return prop, {"portal": portal, "external_id": external_id,
                              "operation": operation_from_property_url(url),
                              "url_normalized": normalized, "match_method": "legacy_exact_url"}

    if external_id:
        compact_id = external_id.replace("-", "").replace("_", "")
        id_values = tuple(dict.fromkeys((external_id, compact_id)))
        for field in fields["id"]:
            prop = collection.find_one({field: {"$in": list(id_values)}})
            if prop:
                return prop, {"portal": portal, "external_id": external_id,
                              "operation": operation_from_property_url(url),
                              "url_normalized": normalized, "match_method": "legacy_external_id"}
        # Some early producer documents retained only the external identity
        # inside a URL field.  Match the identity, not the hostname or slug.
        if external_id.upper().startswith("MLC-"):
            external_pattern = r"MLC[-_]?" + re.escape(external_id.split("-", 1)[1])
        else:
            external_pattern = re.escape(external_id)
        for field in fields["url"]:
            prop = collection.find_one({field: {"$regex": external_pattern, "$options": "i"}})
            if prop:
                return prop, {"portal": portal, "external_id": external_id,
                              "operation": operation_from_property_url(url),
                              "url_normalized": normalized, "match_method": "legacy_url_external_id"}
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
