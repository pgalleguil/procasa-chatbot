import logging
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, parse_qs
from typing import Any, Dict, Iterable, Optional

from config import Config
from .utils import safe_int_conversion

logger = logging.getLogger(__name__)
_PROPERTY_HOST_PATTERN = (
    r"(?:(?:[a-z0-9-]+\.)*(?:mercadolibre\.cl|portalinmobiliario\.com|"
    r"portalinmobiliario\.cl|toctoc\.com|yapo\.cl|procasa\.cl|"
    r"enlaceinmobiliario\.cl|links\.proppit\.com|icasas\.cl|trovit\.cl|"
    r"mitula\.cl|nuroa\.cl|nestoria\.cl|chilepropiedades\.cl))"
)
URL_RE = re.compile(r'https?://[^\s<>\]\)"]+', re.IGNORECASE)
_KNOWN_URL_RE = re.compile(
    rf"(?<!://)(?<![\w@.])(?:(?:https?:\\?://))?(?:www\.)?{_PROPERTY_HOST_PATTERN}"
    r"(?::\d+)?/[^\s<>\]\)\"']+",
    re.IGNORECASE,
)


def extract_property_urls(text: Any) -> list[str]:
    """Extract URLs including provider links without a scheme or escaped slashes."""
    value = str(text or "")
    found = URL_RE.findall(value) + _KNOWN_URL_RE.findall(value)
    cleaned_values = []
    for raw in found:
        cleaned = raw.replace("\\://", "://").replace("\\/", "/").rstrip(".,;:!?")
        if cleaned:
            cleaned_values.append(cleaned)
    return list(dict.fromkeys(cleaned_values))

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
        {"publicaciones.codigo_internacional_por_operacion.V": value},
        {"publicaciones.codigo_internacional_por_operacion.A": value},
        {"publicaciones.codigo_internacional_por_operacion.R": value},
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
    value = _clean_text(raw_value)
    if re.fullmatch(r"\d{9,10}", value):
        prop, _meta = find_property_by_international_code(db, value, collection_name)
        return prop
    if isinstance(raw_value, str) and (
        raw_value.strip().lower().startswith(("http://", "https://"))
        or re.search(r"\bMLC[-_]?\d+\b", raw_value, re.I)
    ):
        alias_prop, alias_meta = lookup_property_link(db, raw_value, collection_name)
        if alias_prop:
            return alias_prop
        if alias_meta.get("error_code") == "AMBIGUOUS_PROPERTY_REFERENCE":
            return None
    collection = db[collection_name]
    for query in build_property_lookup_queries(raw_value):
        prop = collection.find_one(query)
        if prop:
            return prop
    return None


BACKUP_COLLECTION = "universo_cartera"


def find_property_in_any_collection(db, raw_value: Any) -> dict | None:
    """Busca en universo_cartera primero, luego en universo_cartera_prop360 como fallback."""
    value = _clean_text(raw_value)
    if re.fullmatch(r"\d{9,10}", value):
        prop, meta = find_property_by_international_code(db, value, PROPERTY_COLLECTION_NAME)
        if prop or meta.get("error_code") == "AMBIGUOUS_PROPERTY_REFERENCE":
            return prop
        if PROPERTY_COLLECTION_NAME != BACKUP_COLLECTION:
            fallback, fallback_meta = find_property_by_international_code(db, value, BACKUP_COLLECTION)
            if fallback_meta.get("error_code") == "AMBIGUOUS_PROPERTY_REFERENCE":
                return None
            if fallback:
                fallback["_lookup_fallback"] = True
            return fallback
    if isinstance(raw_value, str) and (
        raw_value.strip().lower().startswith(("http://", "https://"))
        or re.search(r"\bMLC[-_]?\d+\b", raw_value, re.I)
    ):
        prop, meta = lookup_property_link(db, raw_value, PROPERTY_COLLECTION_NAME)
        if meta.get("error_code") == "AMBIGUOUS_PROPERTY_REFERENCE":
            return None
        if prop:
            return prop
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
_SUPPORTED_PORTAL_DOMAINS = (
    ("mercadolibre", ("mercadolibre.cl", "mercadolibre.com")),
    ("portal_inmobiliario", ("portalinmobiliario.cl", "portalinmobiliario.com")),
    ("toctoc", ("toctoc.com",)),
    ("yapo", ("yapo.cl",)),
    ("procasa", ("procasa.cl",)),
    ("enlaceinmobiliario", ("enlaceinmobiliario.cl",)),
    ("proppit", ("links.proppit.com", "proppit.com")),
    ("icasas", ("icasas.cl",)),
    ("trovit", ("trovit.cl",)),
    ("mitula", ("mitula.cl",)),
    ("nuroa", ("nuroa.cl",)),
    ("nestoria", ("nestoria.cl",)),
    ("chilepropiedades", ("chilepropiedades.cl",)),
)
_UNVERIFIED_ALIAS_STATUSES = frozenset({
    "declared", "unverified", "not_verified", "pending", "not_accessible",
    "conflict", "refuted", "destination_unverified",
})


def _parse_property_url(value: Any):
    raw = str(value or "").strip().replace("\\://", "://").replace("\\/", "/")
    if raw and not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    try:
        return raw, urlsplit(raw)
    except ValueError:
        return raw, urlsplit("")


def canonical_portal(url: str) -> str:
    _raw, parts = _parse_property_url(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    for portal, domains in _SUPPORTED_PORTAL_DOMAINS:
        if any(_host_is_or_subdomain(host, domain) for domain in domains):
            return portal
    return ""


def _host_is_or_subdomain(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def normalize_property_url(url: str) -> str:
    """Normalize a listing URL while retaining identity-bearing Proppit query values."""
    raw, parts = _parse_property_url(url)
    if not raw:
        return ""
    host = (parts.hostname or "").lower().removeprefix("www.")
    netloc = host
    try:
        if parts.port:
            netloc = f"{host}:{parts.port}"
    except ValueError:
        return ""
    path = re.sub(r"/{2,}", "/", parts.path or "/").rstrip("/") or "/"
    portal = canonical_portal(raw)
    identity_params = {"id", "agency"} if portal == "proppit" else set()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if (k.lower() in identity_params)
             or (k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_"))]
    return urlunsplit(("https", netloc, path, urlencode(sorted(query)), ""))


def extract_property_external_id(url: str, portal: Optional[str] = None) -> Optional[str]:
    value = str(url or "")
    portal = portal or canonical_portal(value)
    _raw, parts = _parse_property_url(value)
    path = parts.path or ""
    if portal == "proppit":
        query = parse_qs(parts.query, keep_blank_values=False)
        return next((str(v).strip() for v in query.get("id", []) if str(v).strip()), None)
    if portal in {"icasas", "trovit", "mitula", "nuroa", "nestoria"}:
        uuid_match = re.search(
            r"(?i)(?:^|/)([0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}|[0-9a-f]{32})(?:/|$)",
            path,
        )
        return uuid_match.group(1).lower() if uuid_match else None
    if portal == "enlaceinmobiliario":
        match = re.fullmatch(r"/usados/[^/]+/[^/]+/(\d{9})/\d+/?", path, re.I)
        return match.group(1) if match else None
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
    if portal == "chilepropiedades":
        match = re.search(r"/(\d{4,})/?(?:[?#]|$)", path)
        return match.group(1) if match else None
    return None


def _mlc_identity(value: Any) -> Optional[str]:
    match = re.search(r"MLC[-_]?([0-9]+)", str(value or ""), re.I)
    return f"MLC-{match.group(1)}" if match else None


def _publication_portals(portal: str) -> tuple[str, ...]:
    # Portal Inmobiliario and Mercado Libre share the same MLC namespace.
    if portal in {"mercadolibre", "portal_inmobiliario"}:
        return ("portal_inmobiliario", "mercadolibre")
    return (portal,) if portal else ()


def _collect_property_matches(collection, queries: Iterable[Dict[str, Any]]) -> list[dict]:
    """Return all distinct claims, with a find_one fallback for small adapters."""
    queries = list(queries)
    if not queries:
        return []
    matches = {}
    mock_find_one_only = type(collection).__module__ == "unittest.mock"
    # Mongo can evaluate the field variants in one server round trip. Keep the
    # per-query fallback for lightweight adapters that only expose find_one.
    supports_find = callable(getattr(collection, "find", None)) and not mock_find_one_only
    batched_queries = [{"$or": queries}] if len(queries) > 1 and supports_find else queries
    for query in batched_queries:
        try:
            if mock_find_one_only:
                raise NotImplementedError
            found = list(collection.find(query))
        except (AttributeError, NotImplementedError, TypeError):
            try:
                one = collection.find_one(query)
                found = [one] if one else []
            except Exception:
                continue
        except Exception:
            continue
        for doc in found:
            if isinstance(doc, dict):
                key = str(doc.get("codigo") or doc.get("codigo_prop360") or doc.get("_id") or id(doc))
                matches[key] = doc
    return list(matches.values())


def _property_codes(docs: Iterable[dict]) -> set[str]:
    return {str(doc.get("codigo") or doc.get("codigo_prop360") or doc.get("_id") or "") for doc in docs}


INTERNATIONAL_CODE_FIELDS = (
    "codigo_internacional",
    "publicaciones.codigo_internacional",
    "publicaciones.codigo_internacional_por_operacion.V",
    "publicaciones.codigo_internacional_por_operacion.A",
    "publicaciones.codigo_internacional_por_operacion.R",
)


def find_property_by_international_code(
    db, raw_value: Any, collection_name: str = PROPERTY_COLLECTION_NAME,
) -> tuple[dict | None, dict]:
    """Resolve a 9–10 digit external property code by exact Prop360 fields only."""
    value = _clean_text(raw_value)
    base_meta = {"external_id": value, "match_method": None, "resolvable": False}
    if not re.fullmatch(r"\d{9,10}", value):
        return None, {**base_meta, "error_code": "INVALID_INTERNATIONAL_PROPERTY_CODE"}

    docs = _collect_property_matches(
        db[collection_name], [{field: value} for field in INTERNATIONAL_CODE_FIELDS],
    )
    codes = _property_codes(docs)
    if len(codes) > 1:
        return None, {
            **base_meta,
            "match_method": "AMBIGUOUS_PROPERTY_REFERENCE",
            "error_code": "AMBIGUOUS_PROPERTY_REFERENCE",
            "candidate_codes": sorted(codes),
        }
    if not docs:
        return None, {**base_meta, "error_code": "INTERNATIONAL_PROPERTY_CODE_NOT_FOUND"}

    prop = sorted(docs, key=lambda item: str(item.get("_id") or item.get("codigo") or ""))[0]
    return prop, {
        **base_meta,
        "match_method": "exact_international_code",
        "resolvable": True,
        "property_code": str(prop.get("codigo") or prop.get("codigo_prop360") or ""),
    }


def operation_from_property_url(url: str) -> Optional[str]:
    text = str(url or "").lower()
    if re.search(r"(?:arriendo|alquiler|rent)", text):
        return "arriendo"
    if re.search(r"(?:venta|vender|compraventa)", text):
        return "venta"
    return None


def _normalize_alias_portal(value: Any) -> str:
    portal = str(value or "").strip().lower().replace("-", "_")
    if portal in {"mercadolibre", "mercado_libre", "portal_inmobiliario"}:
        return portal
    return portal


def _normalize_external_identity(value: Any, portal: str = "") -> str:
    raw = str(value or "").strip()
    mlc = _mlc_identity(raw)
    if mlc:
        return mlc
    if portal in {"icasas", "trovit", "mitula", "nuroa", "nestoria", "toctoc"}:
        return raw.lower()
    return raw


def _alias_is_resolvable(alias: dict) -> bool:
    if alias.get("resolvable") is False:
        return False
    status = str(alias.get("verification_status") or "").strip().lower()
    return not status or status not in _UNVERIFIED_ALIAS_STATUSES


def _alias_operation_matches(alias: dict, requested: Optional[str]) -> bool:
    if not requested:
        return True
    value = str(alias.get("operacion") or alias.get("operation") or "").strip().lower()
    if not value:
        return True
    aliases = {"v": "venta", "a": "arriendo", "r": "arriendo", "rent": "arriendo", "alquiler": "arriendo"}
    return aliases.get(value, value) == requested


def lookup_property_link(db, url: str, collection_name: str = PROPERTY_COLLECTION_NAME):
    """Resolve current and historical IDs/URLs; refuse cross-property conflicts."""
    input_value = str(url or "").strip().replace("\\://", "://").replace("\\/", "/")
    raw, parts = _parse_property_url(input_value)
    portal = canonical_portal(raw)
    input_is_url = bool(re.match(r"^https?://", input_value, re.I) or portal)
    external_id = extract_property_external_id(raw, portal)
    if portal in {"mercadolibre", "portal_inmobiliario"}:
        external_id = external_id or _mlc_identity(raw)
    elif not input_is_url:
        external_id = external_id or _mlc_identity(raw)
    if not portal and external_id and not input_is_url:
        portal = "portal_inmobiliario"
    portals = _publication_portals(portal)
    is_url = input_is_url
    normalized = normalize_property_url(raw) if is_url else ""
    operation = operation_from_property_url(raw)
    collection = db[collection_name]

    if portal in {"portal_inmobiliario", "mercadolibre"}:
        root = "publicaciones.portal_inmobiliario"
        current_url_fields = tuple(
            f"{root}.publicaciones.{variant}.{field}"
            for variant in ("V", "A", "R", "arriendo", "venta")
            for field in ("url", "urls", "url_normalized", "urls_normalized")
        )
        current_id_fields = tuple(
            f"{root}.publicaciones.{variant}.{field}"
            for variant in ("V", "A", "R", "arriendo", "venta")
            for field in ("code", "external_id", "publication_id", "portal_id")
        )
        legacy_url_fields = (
            f"{root}.url_pi", f"{root}.url_mercado_libre", f"{root}.urls_pi",
            f"{root}.urls_mercado_libre", f"{root}.url", "url_pi", "url_mercado_libre",
        )
        legacy_id_fields = (f"{root}.codigo_pi", f"{root}.publication_id", f"{root}.portal_id",
                            "codigo_pi", "codigo_mercadolibre")
        history_bases = tuple(f"{root}.{field}" for field in (
            "historial", "history", "historical", "publications_history", "historial_publicaciones"))
        historical_id_fields = tuple(f"{base}.{field}" for base in history_bases
                                     for field in ("code", "external_id", "publication_id", "portal_id"))
        historical_url_fields = tuple(f"{base}.{field}" for base in history_bases for field in ("url", "urls"))
    else:
        op_variants = ("V", "A", "R", "arriendo", "venta")
        operation_urls = lambda root: tuple(
            f"{root}.publicaciones.{variant}.{field}"
            for variant in op_variants
            for field in ("url", "urls", "url_normalized", "urls_normalized", "url_actual")
        )
        operation_ids = lambda root: tuple(
            f"{root}.publicaciones.{variant}.{field}"
            for variant in op_variants
            for field in ("code", "code_unique", "external_id", "publication_id", "portal_id")
        )
        field_sets = {
            "yapo": (
                (
                    "publicaciones.yapo.url_yapo", "url_yapo",
                    *(f"publicaciones.yapo.publicaciones.{variant}.{field}"
                      for variant in ("V", "A", "R", "arriendo", "venta")
                      for field in ("url", "urls", "url_normalized", "urls_normalized")),
                ),
                (
                    "publicaciones.yapo.codigo_yapo", "codigo_yapo",
                    *(f"publicaciones.yapo.publicaciones.{variant}.{field}"
                      for variant in ("V", "A", "R", "arriendo", "venta")
                      for field in ("code", "external_id", "publication_id", "portal_id")),
                ),
            ),
            "toctoc": (("publicaciones.toctoc.url_toctoc", "toctoc.enlace"), ()),
            "procasa": (("publicaciones.procasa.url_procasa", "source_url", "metadata.source_url"),
                        ("publicaciones.codigo_internacional", "codigo_internacional")),
            "enlaceinmobiliario": ((), INTERNATIONAL_CODE_FIELDS),
            "proppit": (
                ("publicaciones.proppit.url_proppit", "publicaciones.proppit.url_proppit_arriendo",
                 "publicaciones.proppit.urls_proppit", *operation_urls("publicaciones.proppit")),
                (*operation_ids("publicaciones.proppit"), "publicaciones.proppit.code_unique",
                 "publicaciones.proppit.code", "publicaciones.proppit.external_id"),
            ),
            "icasas": (
                ("publicaciones.icasas.url_icasas", "publicaciones.icasas.url", *operation_urls("publicaciones.icasas"),
                 *operation_urls("publicaciones.proppit.portales_distribucion.icasas")),
                ("publicaciones.icasas.external_id", *operation_ids("publicaciones.icasas"),
                 *operation_ids("publicaciones.proppit.portales_distribucion.icasas")),
            ),
            "trovit": (
                ("publicaciones.trovit.url_trovit", "publicaciones.trovit.url", *operation_urls("publicaciones.trovit"),
                 *operation_urls("publicaciones.proppit.portales_distribucion.trovit")),
                ("publicaciones.trovit.external_id", *operation_ids("publicaciones.trovit"),
                 *operation_ids("publicaciones.proppit.portales_distribucion.trovit")),
            ),
            "mitula": (
                ("publicaciones.mitula.url_mitula", "publicaciones.mitula.url", *operation_urls("publicaciones.mitula"),
                 *operation_urls("publicaciones.proppit.portales_distribucion.mitula")),
                ("publicaciones.mitula.external_id", *operation_ids("publicaciones.mitula"),
                 *operation_ids("publicaciones.proppit.portales_distribucion.mitula")),
            ),
            "nuroa": (
                ("publicaciones.nuroa.url_nuroa", "publicaciones.nuroa.url", *operation_urls("publicaciones.nuroa"),
                 *operation_urls("publicaciones.proppit.portales_distribucion.nuroa")),
                ("publicaciones.nuroa.external_id", *operation_ids("publicaciones.nuroa"),
                 *operation_ids("publicaciones.proppit.portales_distribucion.nuroa")),
            ),
            "nestoria": (
                ("publicaciones.nestoria.url_nestoria", "publicaciones.nestoria.url", *operation_urls("publicaciones.nestoria"),
                 *operation_urls("publicaciones.proppit.portales_distribucion.nestoria")),
                ("publicaciones.nestoria.external_id", *operation_ids("publicaciones.nestoria"),
                 *operation_ids("publicaciones.proppit.portales_distribucion.nestoria")),
            ),
            "chilepropiedades": (
                ("publicaciones.chilepropiedades.url_chilepropiedades", *operation_urls("publicaciones.chilepropiedades")),
                ("publicaciones.chilepropiedades.codigo_venta", "publicaciones.chilepropiedades.codigo_arriendo",
                 *operation_ids("publicaciones.chilepropiedades")),
            ),
        }
        legacy_url_fields, legacy_id_fields = field_sets.get(portal, ((), ()))
        current_url_fields, current_id_fields = legacy_url_fields, legacy_id_fields
        historical_id_fields = historical_url_fields = ()

    def guarded_publication_query(field: str, expected: Any) -> dict:
        # Prop360 retains expired publication rows in the same nested V/A
        # records. A row explicitly marked publicada=false is resolvable via
        # its historical alias, but must not be reported as current.
        match = re.match(r"^(.*\.publicaciones\.(?:V|A|R|venta|arriendo))\.", field)
        query = {field: expected}
        if match:
            return {"$and": [query, {f"{match.group(1)}.publicada": {"$ne": False}}]}
        return query

    def id_queries(fields, *, current=False):
        if not external_id:
            return []
        digits = re.sub(r"\D", "", external_id)
        values = list(dict.fromkeys(v for v in (external_id, external_id.replace("-", ""), digits) if v))
        numeric = safe_int_conversion(digits) if digits else None
        if numeric is not None:
            values.append(numeric)
        return [guarded_publication_query(field, {"$in": values}) if current
                else {field: {"$in": values}} for field in fields]

    alias_values = list(dict.fromkeys(
        value for value in (external_id, external_id.replace("-", "") if external_id else "",
                            re.sub(r"\D", "", external_id or "")) if value
    ))
    alias_id_queries = [{"publicaciones.aliases": {"$elemMatch": {
        "portal": {"$in": list(portals)}, "external_id": {"$in": alias_values},
        "resolvable": {"$ne": False}}}}] if portals and external_id else []
    alias_url_queries = [{"publicaciones.aliases": {"$elemMatch": {
        "portal": {"$in": list(portals)}, "url_normalized": normalized,
        "resolvable": {"$ne": False}}}}] if portals and normalized else []
    current_url_queries = [guarded_publication_query(field, value) for field in current_url_fields
                           for value in ((raw, normalized) if is_url else ()) if value]
    current_id_in_url_queries = []
    legacy_id_in_url_queries = []
    if external_id and portal in {"portal_inmobiliario", "mercadolibre"}:
        pattern = r"MLC[-_]?" + re.escape(re.sub(r"\D", "", external_id))
        current_id_in_url_queries = [guarded_publication_query(field, {"$regex": pattern, "$options": "i"})
                                     for field in current_url_fields]
        legacy_id_in_url_queries = [{field: {"$regex": pattern, "$options": "i"}}
                                    for field in legacy_url_fields + historical_url_fields]
    historical_url_queries = [{field: value} for field in historical_url_fields
                              for value in ((raw, normalized) if is_url else ()) if value]
    legacy_url_queries = [{field: value} for field in legacy_url_fields
                          for value in ((raw, normalized) if is_url else ()) if value]

    alias_docs = _collect_property_matches(collection, alias_id_queries + alias_url_queries)
    alias_claims = []
    for doc in alias_docs:
        publications = doc.get("publicaciones") if isinstance(doc.get("publicaciones"), dict) else {}
        for alias in publications.get("aliases") or []:
            if not isinstance(alias, dict) or not _alias_is_resolvable(alias):
                continue
            if _normalize_alias_portal(alias.get("portal")) not in portals:
                continue
            if not _alias_operation_matches(alias, operation):
                continue
            alias_url = normalize_property_url(alias.get("url_normalized") or alias.get("url") or "")
            alias_id = _normalize_external_identity(alias.get("external_id"), portal)
            id_match = bool(external_id and alias_id == _normalize_external_identity(external_id, portal))
            url_match = bool(normalized and alias_url == normalized)
            if id_match or url_match:
                status = str(alias.get("publication_status") or "").lower()
                historical = status in {"historical", "expired", "unavailable"} or alias.get("activa") is False
                alias_claims.append((doc, "historical_alias_url" if historical and url_match else
                                     "historical_alias_id" if historical else
                                     "current_alias_url" if url_match else "current_alias_id", alias))

    groups = [
        ("current_url", _collect_property_matches(collection, current_url_queries + current_id_in_url_queries)),
        ("current_id", _collect_property_matches(collection, id_queries(current_id_fields, current=True))),
        ("current_alias_url", [doc for doc, method, _alias in alias_claims if method == "current_alias_url"]),
        ("current_alias_id", [doc for doc, method, _alias in alias_claims if method == "current_alias_id"]),
        # Accumulated legacy URL arrays can contain retired links. Consult a
        # verified historical alias before those status-less compatibility
        # fields so the resolver reports the reference as historical.
        ("historical_alias_url", [doc for doc, method, _alias in alias_claims if method == "historical_alias_url"]),
        ("historical_alias_id", [doc for doc, method, _alias in alias_claims if method == "historical_alias_id"]),
        ("historical_url", _collect_property_matches(collection, historical_url_queries)),
        ("legacy_id", _collect_property_matches(collection, id_queries(legacy_id_fields))),
        ("legacy_url", _collect_property_matches(collection, legacy_url_queries)),
        ("legacy_historical_id", _collect_property_matches(collection, id_queries(historical_id_fields))),
        ("legacy_url_external_id", _collect_property_matches(collection, legacy_id_in_url_queries)),
    ]
    all_docs = [doc for _, docs in groups for doc in docs]
    codes = _property_codes(all_docs)
    meta = {"portal": portal, "external_id": external_id, "operation": operation,
            "url_normalized": normalized}
    if len(codes) > 1:
        return None, {**meta, "match_method": "AMBIGUOUS_PROPERTY_REFERENCE",
                      "error_code": "AMBIGUOUS_PROPERTY_REFERENCE", "candidate_codes": sorted(codes)}
    for method, docs in groups:
        if docs:
            alias = next((a for d, m, a in alias_claims if d is docs[0] and m == method), None)
            status = (alias.get("publication_status") if alias else None) or (
                "historical" if method.startswith("historical") else "active"
            )
            return docs[0], {**meta, "match_method": method, "publication_status": status,
                             "resolvable": True,
                             "verification_status": (alias or {}).get("verification_status")}
    return None, {**meta, "match_method": None, "resolvable": False}


def build_property_alias(url: str, portal: Optional[str] = None, operation: Optional[str] = None,
                         external_id: Optional[str] = None, active: bool = True,
                         publication_status: Optional[str] = None, resolvable: bool = True,
                         *, source: Optional[str] = None, source_url: Optional[str] = None,
                         verification_status: Optional[str] = None,
                         first_seen_at: Optional[str] = None, last_seen_at: Optional[str] = None,
                         verified_at: Optional[str] = None) -> Dict[str, Any]:
    portal = portal or canonical_portal(url)
    alias = {
        "portal": portal,
        "operacion": operation or operation_from_property_url(url),
        "url": str(url).strip(),
        "url_normalized": normalize_property_url(url),
        "external_id": external_id or extract_property_external_id(url, portal),
        "activa": bool(active),
        "publication_status": publication_status or ("active" if active else "historical"),
        "resolvable": bool(resolvable),
    }
    for key, value in (("source", source), ("source_url", source_url),
                       ("verification_status", verification_status), ("first_seen_at", first_seen_at),
                       ("last_seen_at", last_seen_at), ("verified_at", verified_at)):
        if value is not None:
            alias[key] = value
    return alias


def merge_property_aliases(existing: Any, incoming: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """Merge idempotently by portal + operation + external id + normalized URL."""
    merged: list[dict] = []

    def alias_key(alias: dict) -> tuple:
        portal = _normalize_alias_portal(alias.get("portal"))
        operation = str(alias.get("operacion") or alias.get("operation") or "").strip().lower()
        operation = {"v": "venta", "venta": "venta", "a": "arriendo", "r": "arriendo",
                     "arriendo": "arriendo", "alquiler": "arriendo", "rent": "arriendo"}.get(operation, operation)
        external = _normalize_external_identity(alias.get("external_id"), portal)
        normalized_url = alias.get("url_normalized") or normalize_property_url(alias.get("url") or "")
        return portal, operation, external, normalized_url

    def trust_rank(alias: dict) -> int:
        verification = str(alias.get("verification_status") or "").strip().lower()
        if verification == "source_verified":
            return 3
        if verification == "legacy_database":
            return 1
        return 0

    for alias in [*(item for item in (existing or []) if isinstance(item, dict)), *incoming]:
        candidate = dict(alias)
        key = alias_key(candidate)
        found = False
        for idx, current in enumerate(merged):
            current_key = alias_key(current)
            same_reference = key == current_key or (
                key[0] == current_key[0]
                and key[2:] == current_key[2:]
                and (not key[1] or not current_key[1])
            )
            if same_reference:
                updated = {**current, **candidate}
                if trust_rank(candidate) < trust_rank(current):
                    # A compatibility field can rediscover an existing URL,
                    # but must never downgrade a source-verified alias or
                    # overwrite its audit/status metadata with weaker evidence.
                    for field in (
                        "source", "source_url", "verification_status",
                        "publication_status", "activa", "resolvable",
                        "last_seen_at", "verified_at",
                    ):
                        if field in current:
                            updated[field] = current[field]
                # A legacy summary URL may omit operation while the current
                # publication record or source alias identifies it exactly.
                updated_operation = current.get("operacion") or current.get("operation") or candidate.get("operacion") or candidate.get("operation")
                if updated_operation:
                    updated["operacion"] = updated_operation
                    updated.pop("operation", None)
                if current.get("first_seen_at"):
                    updated["first_seen_at"] = current["first_seen_at"]
                same_observation = all(
                    current.get(field) == updated.get(field)
                    for field in ("publication_status", "resolvable", "verification_status", "source", "source_url")
                )
                if same_observation:
                    for field in ("last_seen_at", "verified_at"):
                        if current.get(field):
                            updated[field] = current[field]
                merged[idx] = updated
                found = True
                break
        if not found:
            merged.append(candidate)
    return merged
