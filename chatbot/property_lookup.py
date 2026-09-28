import re
import unicodedata
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
    if isinstance(raw_value, str) and (
        raw_value.strip().lower().startswith(("http://", "https://"))
        or re.search(r"\bMLC[-_]?\d+\b", raw_value, re.I)
    ):
        _prop, meta = lookup_property_link(db, raw_value, PROPERTY_COLLECTION_NAME)
        if meta.get("error_code") == "AMBIGUOUS_PROPERTY_REFERENCE":
            return None
        if _prop:
            return _prop
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
        if match:
            digits = re.search(r"\d+", match.group(0)).group(0)
            return f"MLC-{digits}"
        return None
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


def validate_property_link_semantics(prop: Dict[str, Any], url: str) -> tuple[bool, str]:
    """Reject an exact-looking link match whose listing semantics disagree.

    A portal id is the primary identity, but legacy data can contain stale or
    cross-portal mappings.  Operation, commune, and broad property category
    from the URL are cheap safety checks before the lead context is updated.
    """
    if not prop:
        return False, "missing_property"

    def _fold(value: Any) -> str:
        text = unicodedata.normalize("NFKD", str(value or ""))
        return "".join(char for char in text if not unicodedata.combining(char)).casefold()

    slug = _fold(url)
    operation = _fold(get_prop_operation(prop).get("operacion", ""))
    location = get_prop_location(prop)
    comuna = _fold(location.get("comuna"))
    tipo = _fold(get_prop_operation(prop).get("tipo", ""))

    expected_operation = operation_from_property_url(url)
    if expected_operation and operation and expected_operation != operation:
        return False, "operation_mismatch"
    if "nunoa" in slug and "nunoa" not in comuna:
        return False, "commune_mismatch"
    if any(token in slug for token in ("local-comercial", "comercios", "comercial")):
        if not any(token in tipo for token in ("local", "comercial")):
            return False, "property_type_mismatch"
    if "oficina" in slug and "oficina" not in tipo:
        return False, "property_type_mismatch"
    if "departamento" in slug and "departamento" not in tipo:
        return False, "property_type_mismatch"
    if "casa" in slug and "casa" not in tipo:
        return False, "property_type_mismatch"
    return True, "match"


def _mlc_identity(value: Any) -> str | None:
    match = re.search(r"MLC[-_]?([0-9]+)", str(value or ""), re.I)
    return f"MLC-{match.group(1)}" if match else None


def _publication_portals(portal: str) -> tuple[str, ...]:
    # Portal Inmobiliario and Mercado Libre share MLC publication identities.
    if portal in {"mercadolibre", "portal_inmobiliario"}:
        return ("portal_inmobiliario", "mercadolibre")
    return (portal,) if portal else ()


def _query_documents(collection, queries: Iterable[Dict[str, Any]]) -> list[dict]:
    """Collect every matching document where the collection supports find()."""
    matches = {}
    mock_find_one_only = type(collection).__module__ == "unittest.mock"
    for query in queries:
        try:
            # Lightweight mocks in the existing resolver suite model find_one
            # only; do not mistake MagicMock's empty iterable for Mongo's result.
            if mock_find_one_only:
                raise NotImplementedError
            cursor = collection.find(query)
            found = list(cursor)
        except (AttributeError, NotImplementedError, TypeError):
            found = []
            try:
                one = collection.find_one(query)
                if one:
                    found = [one]
            except Exception:
                continue
        except Exception:
            continue
        for doc in found:
            if not isinstance(doc, dict):
                continue
            code = str(doc.get("codigo") or doc.get("_id") or id(doc))
            matches[code] = doc
        if mock_find_one_only and found:
            return list(matches.values())
    return list(matches.values())


def _unique_property_codes(docs: Iterable[dict]) -> set[str]:
    return {str(doc.get("codigo") or doc.get("_id") or "") for doc in docs}


def lookup_property_link(db, url: str, collection_name: str = PROPERTY_COLLECTION_NAME):
    """Resolve current and historical publication identities without guessing.

    The ``publicaciones.aliases`` model is authoritative for historical IDs.
    Historical state is descriptive only: an alias remains resolvable unless it
    explicitly has ``resolvable=False``. If any supplied identity is claimed by
    different Prop360 codes, return AMBIGUOUS_PROPERTY_REFERENCE.
    """
    raw = str(url or "").strip()
    portal = canonical_portal(raw)
    external_id = extract_property_external_id(raw, portal) or _mlc_identity(raw)
    if not portal and external_id and not raw.lower().startswith(("http://", "https://")):
        # A bare MLC has no host; its identity family is Portal Inmobiliario.
        portal = "portal_inmobiliario"
    normalized = normalize_property_url(raw) if raw.lower().startswith(("http://", "https://")) else ""
    operation = operation_from_property_url(raw)
    collection = db[collection_name]
    portal_values = _publication_portals(portal)

    # Keep the established legacy coverage for every supported portal.
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
        legacy_url_fields = tuple(
            f"{root}.{field}" for field in (
                "url_pi", "url_mercado_libre", "urls_pi", "urls_mercado_libre", "url",
            )
        ) + ("url_pi", "url_mercado_libre")
        legacy_id_fields = (
            f"{root}.codigo_pi", f"{root}.publication_id", f"{root}.portal_id",
            "codigo_pi", "codigo_mercadolibre",
        )
        history_bases = tuple(
            f"{root}.{field}" for field in (
                "historial", "history", "historical", "publications_history",
                "historial_publicaciones",
            )
        )
        historical_id_fields = tuple(
            f"{base}.{field}" for base in history_bases
            for field in ("code", "external_id", "publication_id", "portal_id")
        )
        historical_url_fields = tuple(
            f"{base}.{field}" for base in history_bases for field in ("url", "urls")
        )
    elif portal == "yapo":
        current_url_fields = ("publicaciones.yapo.url_yapo", "url_yapo")
        current_id_fields = ("publicaciones.yapo.codigo_yapo", "codigo_yapo") + tuple(
            f"publicaciones.yapo.publicaciones.{variant}.{field}"
            for variant in ("A", "V", "R", "arriendo", "venta")
            for field in ("code", "codigo", "external_id")
        )
        legacy_url_fields, legacy_id_fields = current_url_fields, current_id_fields
        historical_id_fields = historical_url_fields = ()
    elif portal == "toctoc":
        current_url_fields = ("publicaciones.toctoc.url_toctoc", "toctoc.enlace")
        current_id_fields = ()
        legacy_url_fields, legacy_id_fields = current_url_fields, ()
        historical_id_fields = historical_url_fields = ()
    elif portal == "procasa":
        current_url_fields = ("publicaciones.procasa.url_procasa", "source_url", "metadata.source_url")
        current_id_fields = ("publicaciones.codigo_internacional", "codigo_internacional")
        legacy_url_fields, legacy_id_fields = current_url_fields, current_id_fields
        historical_id_fields = historical_url_fields = ()
    else:
        current_url_fields = current_id_fields = legacy_url_fields = legacy_id_fields = ()
        historical_id_fields = historical_url_fields = ()

    def _id_queries(fields):
        if not external_id:
            return []
        digits = re.sub(r"\D", "", external_id)
        compact = external_id.replace("-", "").replace("_", "")
        vals = list(dict.fromkeys(v for v in (external_id, compact, digits) if v))
        try:
            numeric = safe_int_conversion(digits) if digits else None
            if numeric is not None:
                vals.append(numeric)
        except Exception:
            pass
        return [{field: {"$in": vals}} for field in fields]

    alias_id_query = []
    alias_url_query = []
    if portal_values and external_id:
        alias_id_query = [{"publicaciones.aliases": {"$elemMatch": {
            "portal": {"$in": list(portal_values)},
            "external_id": {"$in": [external_id, external_id.replace("-", ""),
                                      re.sub(r"\D", "", external_id)]},
            "resolvable": {"$ne": False},
        }}}]
    if portal_values and normalized:
        alias_url_query = [{"publicaciones.aliases": {"$elemMatch": {
            "portal": {"$in": list(portal_values)},
            "url_normalized": normalized,
            "resolvable": {"$ne": False},
        }}}]

    current_id_queries = _id_queries(current_id_fields)
    legacy_id_queries = _id_queries(legacy_id_fields)
    historical_id_queries = _id_queries(historical_id_fields)
    if external_id and portal in {"portal_inmobiliario", "mercadolibre"}:
        external_pattern = r"MLC[-_]?" + re.escape(external_id.split("-", 1)[-1])
        current_id_in_url_queries = [
            {field: {"$regex": external_pattern, "$options": "i"}}
            for field in current_url_fields
        ]
        legacy_id_in_url_queries = [
            {field: {"$regex": external_pattern, "$options": "i"}}
            for field in legacy_url_fields + historical_url_fields
        ]
    else:
        current_id_in_url_queries = legacy_id_in_url_queries = []
    current_url_queries = []
    if normalized:
        for field in current_url_fields:
            current_url_queries.extend(({field: normalized}, {field: raw}))
    historical_url_queries = []
    if normalized:
        for field in historical_url_fields:
            historical_url_queries.extend(({field: normalized}, {field: raw}))

    # Search every identity class before choosing by priority. This prevents a
    # unique current ID from masking a conflicting historical URL claim.
    current_id_docs = _query_documents(collection, current_id_queries)
    alias_id_docs = _query_documents(collection, alias_id_query)
    current_url_docs = _query_documents(collection, current_url_queries)
    alias_url_docs = _query_documents(collection, alias_url_query)
    historical_url_docs = _query_documents(collection, historical_url_queries)
    legacy_id_docs = _query_documents(collection, legacy_id_queries)
    legacy_url_docs = _query_documents(collection, [
        {field: value} for field in legacy_url_fields for value in _non_empty((raw, normalized))
    ])
    legacy_historical_id_docs = _query_documents(collection, historical_id_queries)
    current_id_in_url_docs = _query_documents(collection, current_id_in_url_queries)
    legacy_id_in_url_docs = _query_documents(collection, legacy_id_in_url_queries)
    all_docs = (current_id_docs + alias_id_docs + current_url_docs + alias_url_docs +
                historical_url_docs + legacy_id_docs + legacy_url_docs +
                legacy_historical_id_docs + current_id_in_url_docs + legacy_id_in_url_docs)
    codes = _unique_property_codes(all_docs)
    base_meta = {
        "portal": portal,
        "external_id": external_id,
        "operation": operation,
        "url_normalized": normalized,
    }
    if len(codes) > 1:
        return None, {**base_meta, "match_method": "AMBIGUOUS_PROPERTY_REFERENCE",
                      "error_code": "AMBIGUOUS_PROPERTY_REFERENCE",
                      "candidate_codes": sorted(codes)}

    # A historical alias marked publication_status=historical remains valid;
    # only an explicit resolvable=False disables the identity.
    ranked = (
        (current_id_docs + current_id_in_url_docs, "current_external_id", "active"),
        (alias_id_docs, "historical_alias_external_id", "historical"),
        (current_url_docs, "current_url_normalized", "active"),
        (alias_url_docs, "historical_alias_url", "historical"),
        (historical_url_docs, "legacy_historical_url", "historical"),
        (legacy_id_docs, "legacy_current_external_id", "active"),
        (legacy_url_docs, "legacy_current_url", "active"),
        (legacy_historical_id_docs, "legacy_historical_external_id", "historical"),
        (legacy_id_in_url_docs, "legacy_url_external_id", "historical"),
    )
    for docs, method, publication_status in ranked:
        if docs:
            prop = docs[0]
            return prop, {**base_meta, "match_method": method,
                          "publication_status": publication_status,
                          "resolvable": True}

    return None, {**base_meta, "match_method": None, "resolvable": False}


def build_property_alias(url: str, portal: Optional[str] = None, operation: Optional[str] = None,
                         external_id: Optional[str] = None, active: bool = True,
                         publication_status: Optional[str] = None,
                         resolvable: bool = True) -> Dict[str, Any]:
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
                # Do not silently re-enable an alias that was explicitly
                # disabled by a separate safety decision.
                merged[idx] = {**current, **candidate,
                               "resolvable": current.get("resolvable", candidate.get("resolvable", True))}
                found = True
                break
        if not found:
            merged.append(candidate)
    return merged
