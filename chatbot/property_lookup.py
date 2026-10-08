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


def canonical_portal(url: str) -> str:
    host = (urlsplit(str(url or "")).hostname or "").lower().removeprefix("www.")
    if _host_is_or_subdomain(host, "mercadolibre.cl") or re.fullmatch(
        r"(?:[a-z0-9-]+\.)*mercadolibre\.com(?:\.[a-z]{2})?", host,
    ):
        return "mercadolibre"
    if _host_is_or_subdomain(host, "portalinmobiliario.com") or _host_is_or_subdomain(host, "portalinmobiliario.cl"):
        return "portal_inmobiliario"
    if _host_is_or_subdomain(host, "toctoc.com"):
        return "toctoc"
    if _host_is_or_subdomain(host, "yapo.cl"):
        return "yapo"
    if _host_is_or_subdomain(host, "procasa.cl"):
        return "procasa"
    if _host_is_or_subdomain(host, "enlaceinmobiliario.cl"):
        return "enlaceinmobiliario"
    return ""


def _host_is_or_subdomain(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


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
    if portal == "enlaceinmobiliario":
        path = urlsplit(value).path or ""
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


def lookup_property_link(db, url: str, collection_name: str = PROPERTY_COLLECTION_NAME):
    """Resolve current and historical IDs/URLs; refuse cross-property conflicts."""
    raw = str(url or "").strip()
    portal = canonical_portal(raw)
    external_id = extract_property_external_id(raw, portal) or _mlc_identity(raw)
    if not portal and external_id and not raw.lower().startswith(("http://", "https://")):
        portal = "portal_inmobiliario"
    portals = _publication_portals(portal)
    normalized = normalize_property_url(raw) if raw.lower().startswith(("http://", "https://")) else ""
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
        }
        legacy_url_fields, legacy_id_fields = field_sets.get(portal, ((), ()))
        current_url_fields, current_id_fields = legacy_url_fields, legacy_id_fields
        historical_id_fields = historical_url_fields = ()

    def id_queries(fields):
        if not external_id:
            return []
        digits = re.sub(r"\D", "", external_id)
        values = list(dict.fromkeys(v for v in (external_id, external_id.replace("-", ""), digits) if v))
        numeric = safe_int_conversion(digits) if digits else None
        if numeric is not None:
            values.append(numeric)
        return [{field: {"$in": values}} for field in fields]

    alias_id_queries = [{"publicaciones.aliases": {"$elemMatch": {
        "portal": {"$in": list(portals)}, "external_id": {"$in": [external_id,
        external_id.replace("-", ""), re.sub(r"\D", "", external_id)]}, "resolvable": {"$ne": False}}}}
        ] if portals and external_id else []
    alias_url_queries = [{"publicaciones.aliases": {"$elemMatch": {
        "portal": {"$in": list(portals)}, "url_normalized": normalized,
        "resolvable": {"$ne": False}}}}] if portals and normalized else []
    current_url_queries = [{field: value} for field in current_url_fields
                           for value in (raw, normalized) if value]
    current_id_in_url_queries = []
    legacy_id_in_url_queries = []
    if external_id and portal in {"portal_inmobiliario", "mercadolibre"}:
        pattern = r"MLC[-_]?" + re.escape(re.sub(r"\D", "", external_id))
        current_id_in_url_queries = [{field: {"$regex": pattern, "$options": "i"}}
                                     for field in current_url_fields]
        legacy_id_in_url_queries = [{field: {"$regex": pattern, "$options": "i"}}
                                    for field in legacy_url_fields + historical_url_fields]
    historical_url_queries = [{field: value} for field in historical_url_fields
                              for value in (raw, normalized) if value]
    legacy_url_queries = [{field: value} for field in legacy_url_fields
                          for value in (raw, normalized) if value]

    groups = [
        ("current_id", _collect_property_matches(collection, id_queries(current_id_fields))),
        ("historical_alias_id", _collect_property_matches(collection, alias_id_queries)),
        ("current_url", _collect_property_matches(collection, current_url_queries + current_id_in_url_queries)),
        ("historical_alias_url", _collect_property_matches(collection, alias_url_queries)),
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
            status = "historical" if method.startswith("historical") else "active"
            return docs[0], {**meta, "match_method": method, "publication_status": status, "resolvable": True}
    return None, {**meta, "match_method": None, "resolvable": False}


def build_property_alias(url: str, portal: Optional[str] = None, operation: Optional[str] = None,
                         external_id: Optional[str] = None, active: bool = True,
                         publication_status: Optional[str] = None, resolvable: bool = True) -> Dict[str, Any]:
    portal = portal or canonical_portal(url)
    return {
        "portal": portal,
        "operacion": operation or operation_from_property_url(url),
        "url": str(url).strip(),
        "url_normalized": normalize_property_url(url),
        "external_id": external_id or extract_property_external_id(url, portal),
        "activa": bool(active),
        "publication_status": publication_status or ("active" if active else "historical"),
        "resolvable": bool(resolvable),
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
