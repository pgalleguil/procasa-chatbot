"""Read-only, explainable data service for the PROCASA SUCRE prototype."""

from __future__ import annotations

import math
import re
import statistics
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from analytics.pricing_intelligence.lead_linkage import LeadLinkageService
from analytics.pricing_intelligence.lead_origin import resolve_lead_origin
from analytics.pricing_intelligence.models import LinkageStatus
from analytics.pricing_intelligence.cohort_reproducibility import (
    COHORT_RULE_VERSION,
    cohort_fingerprint,
)
from analytics.pricing_intelligence.engine_v1 import evaluate_engine_v1
from analytics.pricing_intelligence.observability import (
    SOURCE_FULL,
    WINDOW_UNKNOWN,
    build_observation_evidence,
    demand_observation,
    derive_lead_source_coverage,
)
from analytics.pricing_intelligence.property_identity import (
    build_property_identity_resolver,
    normalize_identifier,
    normalize_alias_identifier,
)
from analytics.pricing_intelligence.time_utils import BUSINESS_TZ, is_in_previous_window, parse_aware_datetime

from .schemas import (
    MarketIntelligenceSnapshotV1,
    OwnerPortalDataQualityV1,
    OwnerPortalEngineV1Contract,
    OwnerPortalActivityPointV1,
    OwnerPortalComparableCohortV1,
    OwnerPortalComparableExampleV1,
    OwnerPortalMarketContextV1,
    OwnerPortalPriceV1,
    OwnerPortalPublicationV1,
    OwnerPortalPositioningV1,
    OwnerPortalPropertyViewV1,
    OwnerPortalTimelineEventV1,
    OwnerPortalProvenanceV1,
    MarketIndicatorV1,
)
from .semantics import (
    canonical_region,
    operation_price,
    region_to_macrozone,
    resolve_property_operations,
)

OFFICE_SCOPE = "PROCASA_SUCRE"
SUCRE_OFFICE_FIELD = "estado.oficina"
SUCRE_OFFICE_VALUE = "PROCASA SUCRE"
SUCRE_OFFICE_VALUES = frozenset({SUCRE_OFFICE_VALUE})
SNAPSHOT_COLLECTION = "pricing_intelligence_property_snapshots_v1"
MASTER_COLLECTION = "universo_cartera_prop360"
CAPTACION_COLLECTION = "propiedades_captacion"
MARKET_COLLECTION = "mercado_comunal"
MARKET_INTELLIGENCE_COLLECTION = "market_intelligence_snapshots_v1"

MASTER_PROJECTION = {
    "_id": 0,
    "codigo": 1,
    "estado.oficina": 1,
    "estado.disponible_prop360": 1,
    "estado.ultima_actualizacion": 1,
    "tipo_operacion": 1,
    "metadata.tipo_propiedad": 1,
    "ubicacion.region": 1,
    "ubicacion.comuna": 1,
    "ubicacion.sector": 1,
    "caracteristicas.dormitorios": 1,
    "caracteristicas.banos": 1,
    "caracteristicas.estacionamientos": 1,
    "caracteristicas.superficie_construida": 1,
    "caracteristicas.superficie_terreno": 1,
    "publicaciones": 1,
    "historial_cambios": 1,
}

MASTER_BASE_PROJECTION = {
    "_id": 0,
    "codigo": 1,
    "estado.oficina": 1,
    "estado.disponible_prop360": 1,
    "estado.ultima_actualizacion": 1,
    "tipo_operacion": 1,
    "metadata.tipo_propiedad": 1,
    "ubicacion.region": 1,
    "ubicacion.comuna": 1,
    "ubicacion.sector": 1,
    "caracteristicas.dormitorios": 1,
    "caracteristicas.banos": 1,
    "caracteristicas.estacionamientos": 1,
    "caracteristicas.superficie_construida": 1,
    "caracteristicas.superficie_terreno": 1,
}

MASTER_PUBLICATION_PROJECTION = {
    **MASTER_BASE_PROJECTION,
    "publicaciones.portal_inmobiliario.publicaciones": 1,
    "publicaciones.portal_inmobiliario.url_pi": 1,
    "publicaciones.portal_inmobiliario.url_mercado_libre": 1,
    "publicaciones.mercadolibre.publicaciones": 1,
    "publicaciones.mercadolibre.url_mercado_libre": 1,
    "publicaciones.yapo.publicaciones": 1,
    "publicaciones.toctoc.publicaciones": 1,
    "publicaciones.chilepropiedades.publicaciones": 1,
    "publicaciones.chilepropiedades.codigo_venta": 1,
    "publicaciones.chilepropiedades.codigo_arriendo": 1,
    "publicaciones.proppit.publicaciones": 1,
    "publicaciones.procasa.publicaciones": 1,
}

IDENTITY_PROJECTION = {
    "_id": 0,
    "codigo": 1,
    "estado.oficina": 1,
    "publicaciones.portal_inmobiliario.publicaciones": 1,
    "publicaciones.portal_inmobiliario.url_pi": 1,
    "publicaciones.portal_inmobiliario.url_mercado_libre": 1,
    "publicaciones.mercadolibre.publicaciones": 1,
    "publicaciones.mercadolibre.url_mercado_libre": 1,
    "publicaciones.yapo.publicaciones": 1,
    "publicaciones.toctoc.publicaciones": 1,
    "publicaciones.chilepropiedades.publicaciones": 1,
    "publicaciones.chilepropiedades.codigo_venta": 1,
    "publicaciones.chilepropiedades.codigo_arriendo": 1,
    "publicaciones.proppit.publicaciones": 1,
    "publicaciones.procasa.publicaciones": 1,
}

LEAD_PROJECTION = {
    "_id": 1,
    "created_at": 1,
    "prospecto.codigo": 1,
    "prospecto.codigo_mercadolibre": 1,
    "prospecto.codigo_yapo": 1,
    "prospecto.codigo_procasa": 1,
    "prospecto.codigo_propiedad": 1,
    "prospecto.propiedad_codigo": 1,
    "prospecto.origen": 1,
    "prospecto.canal_origen": 1,
    "prospecto.plataforma": 1,
    "prospecto.fuente_lead": 1,
    "prospecto.portal_origen": 1,
    "prospecto.origen_anuncio": 1,
    "prospecto.plataforma_origen": 1,
    "origen": 1,
    "source_type": 1,
    "canal_envio": 1,
    "source_events.portal_source": 1,
    "source_events.source_system": 1,
    "messages.portal": 1,
    "messages.source": 1,
    "messages.property_code": 1,
}

_MASTER_ALIAS_PORTALS: tuple[tuple[str, str], ...] = (
    ("mercadolibre", "portal_inmobiliario"),
    ("yapo", "yapo"),
    ("toctoc", "toctoc"),
    ("chilepropiedades", "chilepropiedades"),
    ("proppit", "proppit"),
    ("procasa", "procasa"),
)

_IDENTITY_CACHE: dict[int, tuple[Any, tuple[dict[str, Any], ...]]] = {}
_MARKET_CONTEXT_CACHE: dict[tuple[int, str, str, str], tuple[float, Any, dict[str, Any]]] = {}
_MARKET_CONTEXT_CACHE_TTL_SECONDS = 30.0
_NATIONAL_INDICATORS_CACHE: dict[int, tuple[float, Any, tuple[MarketIndicatorV1, ...]]] = {}
_NATIONAL_INDICATORS_CACHE_TTL_SECONDS = 30.0


def _path(doc: Mapping[str, Any], dotted: str) -> Any:
    current: Any = doc
    for part in dotted.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            text = value.strip().replace("\u00a0", " ").replace(" ", "").replace("$", "")
            if "," in text and "." in text:
                text = text.replace(".", "").replace(",", ".")
            elif "," in text:
                text = text.replace(",", ".")
            elif len(re.findall(r"\.", text)) > 1:
                text = text.replace(".", "")
            result = float(text)
        else:
            result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def _normalized_label(value: Any) -> str:
    """Normalize human labels for exact semantic matching, not fuzzy search."""

    text = _text(value) or ""
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(without_marks.casefold().split())


def _market_match_key(commune: Any, property_type: Any) -> str:
    return f"{_normalized_label(commune)}|{_normalized_label(property_type)}"


def _text_variants(value: Any) -> list[str]:
    """Build a small bounded set of exact Mongo values used by legacy datasets."""

    raw = _text(value)
    if not raw:
        return []
    decomposed = unicodedata.normalize("NFKD", raw)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    values = {
        raw,
        raw.casefold(),
        raw.lower(),
        raw.title(),
        raw.upper(),
        without_marks,
        without_marks.casefold(),
        without_marks.title(),
        without_marks.upper(),
    }
    return sorted(value for value in values if value)


def _property_type(doc: Mapping[str, Any]) -> str | None:
    """Read the property type separately from the transaction operation."""

    return (
        _text(_path(doc, "metadata.tipo_propiedad"))
        or _text(_path(doc, "metadata.tipo_propiedad_detectado"))
        or _text(_path(doc, "tipo_operacion.tipo"))
    )


def _operation(doc: Mapping[str, Any], operation: str | None = None) -> str | None:
    """Return one explicitly selected operation, or the single canonical one."""

    resolved = resolve_property_operations(doc)
    if operation is not None:
        normalized = str(operation).strip().casefold()
        return normalized if normalized in resolved["operations"] else None
    return resolved["primary_operation"]


def _price(doc: Mapping[str, Any], operation: str | None = None) -> dict[str, float | None]:
    """Read the price belonging to one canonical operation, without fallback."""

    selected = _operation(doc, operation)
    return operation_price(doc, selected) if selected else {"uf": None, "clp": None}


def is_procasa_sucre_property(property_doc: Mapping[str, Any]) -> bool:
    """Return true only for the verified exact office value in the master."""

    return _path(property_doc, SUCRE_OFFICE_FIELD) in SUCRE_OFFICE_VALUES


def is_active_property(property_doc: Mapping[str, Any]) -> bool:
    return _path(property_doc, "estado.disponible_prop360") is True


def _publication_listing_ids(doc: Mapping[str, Any]) -> set[str]:
    output: set[str] = set()
    publications = doc.get("publicaciones")
    if not isinstance(publications, Mapping):
        return output
    for portal in publications.values():
        if not isinstance(portal, Mapping):
            continue
        records = portal.get("publicaciones")
        if isinstance(records, Mapping):
            for record in records.values():
                if isinstance(record, Mapping):
                    for field in ("code", "code_unique"):
                        value = normalize_identifier(record.get(field))
                        if value:
                            output.add(value)
        for field in ("codigo_venta", "codigo_arriendo"):
            value = normalize_identifier(portal.get(field))
            if value:
                output.add(value)
    return output


def _image_urls(captacion: Mapping[str, Any]) -> list[str]:
    values = captacion.get("image_urls")
    urls = [str(item).strip() for item in values if str(item).strip()] if isinstance(values, list) else []
    main = _text(captacion.get("main_image_url"))
    if main and main not in urls:
        urls.insert(0, main)
    return urls


def _find_master(
    db: Any,
    code: str | None = None,
    *,
    projection: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    query = {SUCRE_OFFICE_FIELD: SUCRE_OFFICE_VALUE}
    if code is not None:
        query["codigo"] = str(code)
    return list(db[MASTER_COLLECTION].find(query, projection or MASTER_PROJECTION))


def _find_master_in_batches(
    db: Any,
    query: Mapping[str, Any],
    projection: Mapping[str, int],
    *,
    batch_size: int = 100,
) -> list[dict[str, Any]]:
    """Avoid a long Mongo cursor for large publication-heavy documents."""

    documents: list[dict[str, Any]] = []
    offset = 0
    while True:
        chunk = list(db[MASTER_COLLECTION].find(query, projection).skip(offset).limit(batch_size))
        if not chunk:
            break
        documents.extend(chunk)
        offset += len(chunk)
        if len(chunk) < batch_size:
            break
    return documents


def _identity_properties(db: Any) -> tuple[dict[str, Any], ...]:
    client = getattr(db, "client", db)
    cache_key = id(client)
    cached_entry = _IDENTITY_CACHE.get(cache_key)
    if cached_entry is not None and cached_entry[0] is client:
        return cached_entry[1]
    documents = _find_master_in_batches(db, {}, IDENTITY_PROJECTION)
    cached = tuple(documents)
    _IDENTITY_CACHE[cache_key] = (client, cached)
    return cached


def _mongo_identifier_variants(values: Iterable[Any]) -> list[str | int]:
    """Return exact normalized IDs plus numeric variants for mixed Mongo types."""

    variants: list[str | int] = []
    seen: set[tuple[type, str]] = set()
    for value in values:
        normalized = normalize_identifier(value)
        if not normalized:
            continue
        candidates: list[str | int] = [normalized]
        if normalized.isdigit():
            try:
                candidates.append(int(normalized))
            except ValueError:
                pass
        for candidate in candidates:
            key = (type(candidate), str(candidate))
            if key not in seen:
                seen.add(key)
                variants.append(candidate)
    return variants


def _verified_property_alias_values(property_doc: Mapping[str, Any]) -> dict[str, set[str]]:
    """Extract the namespace-aware aliases approved by the V2 resolver."""

    values: dict[str, set[str]] = {source: set() for source, _ in _MASTER_ALIAS_PORTALS}
    publications = property_doc.get("publicaciones")
    if not isinstance(publications, Mapping):
        return values
    for source, portal_key in _MASTER_ALIAS_PORTALS:
        portal = publications.get(portal_key)
        if not isinstance(portal, Mapping):
            continue
        records = portal.get("publicaciones")
        if isinstance(records, Mapping):
            for record in records.values():
                if not isinstance(record, Mapping):
                    continue
                for field_name in ("code", "code_unique"):
                    normalized = normalize_alias_identifier(source, record.get(field_name))
                    if normalized and source in values:
                        values[source].add(normalized)
        if portal_key == "chilepropiedades":
            for field_name in ("codigo_venta", "codigo_arriendo"):
                normalized = normalize_alias_identifier(source, portal.get(field_name))
                if normalized and source in values:
                    values[source].add(normalized)
    return values


def _lead_candidate_query(property_doc: Mapping[str, Any]) -> dict[str, Any]:
    """Build the narrow lead query using only verified, namespaced identifiers."""

    code = normalize_identifier(property_doc.get("codigo"))
    aliases = _verified_property_alias_values(property_doc)
    clauses: list[dict[str, Any]] = []
    if code:
        clauses.append({"prospecto.codigo": {"$in": _mongo_identifier_variants([code])}})
    if aliases["mercadolibre"]:
        clauses.append({"prospecto.codigo_mercadolibre": {"$in": _mongo_identifier_variants(aliases["mercadolibre"])}})
    if aliases["yapo"]:
        clauses.append({"prospecto.codigo_yapo": {"$in": _mongo_identifier_variants(aliases["yapo"])}})
    if aliases["toctoc"]:
        toctoc_values = _mongo_identifier_variants(aliases["toctoc"])
        clauses.extend(
            [
                {"prospecto.codigo_propiedad": {"$in": toctoc_values}},
                {"prospecto.propiedad_codigo": {"$in": toctoc_values}},
            ]
        )
    if aliases["procasa"]:
        procasa_values = _mongo_identifier_variants(aliases["procasa"])
        clauses.append({"prospecto.codigo_procasa": {"$in": procasa_values}})
        clauses.append({"messages.property_code": {"$in": procasa_values}})
    for source in ("mercadolibre", "yapo", "chilepropiedades", "proppit"):
        if not aliases[source]:
            continue
        values = _mongo_identifier_variants(aliases[source])
        clauses.append({"messages.property_code": {"$in": values}})
    return {"$or": clauses} if clauses else {"_id": {"$exists": False}}


def _master_alias_match_expression(alias_values: Mapping[str, set[str]]) -> dict[str, Any] | None:
    """Build a server-side alias collision predicate without materializing master."""

    expressions: list[dict[str, Any]] = []
    for source, portal_key in _MASTER_ALIAS_PORTALS:
        values = _mongo_identifier_variants(alias_values.get(source, set()))
        if not values:
            continue
        records_path = f"$publicaciones.{portal_key}.publicaciones"
        mapped_fields = {
            field_name: {
                "$map": {
                    "input": {"$objectToArray": {"$ifNull": [records_path, {}]}},
                    "as": "record",
                    "in": f"$$record.v.{field_name}",
                }
            }
            for field_name in ("code", "code_unique")
        }
        for mapped in mapped_fields.values():
            expressions.extend({"$in": [value, mapped]} for value in values)
        if portal_key == "chilepropiedades":
            for field_name in ("codigo_venta", "codigo_arriendo"):
                for value in values:
                    expressions.append({"$eq": [f"$publicaciones.{portal_key}.{field_name}", value]})
    return {"$expr": {"$or": expressions}} if expressions else None


def _lead_alias_values(leads: Iterable[Mapping[str, Any]], resolver: Any) -> dict[str, set[str]]:
    values: dict[str, set[str]] = {source: set() for source, _ in _MASTER_ALIAS_PORTALS}
    for lead in leads:
        for alias, _path in resolver.lead_aliases(lead):
            values.setdefault(alias.source, set()).add(alias.external_id)
    return values


def _property_identity_candidates(
    db: Any,
    property_doc: Mapping[str, Any],
    leads: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Fetch only canonical/alias collision candidates needed by these leads."""

    leads = list(leads)
    identity_docs: dict[str, dict[str, Any]] = {}
    current_code = normalize_identifier(property_doc.get("codigo"))
    if current_code:
        identity_docs[current_code] = dict(property_doc)

    canonical_values = {
        normalized
        for lead in leads
        for normalized in [normalize_identifier(_path(lead, "prospecto.codigo"))]
        if normalized and normalized != current_code
    }
    if canonical_values:
        for doc in db[MASTER_COLLECTION].find(
            {"codigo": {"$in": _mongo_identifier_variants(canonical_values)}},
            IDENTITY_PROJECTION,
        ):
            code = normalize_identifier(doc.get("codigo"))
            if code:
                identity_docs[code] = doc

    resolver = build_property_identity_resolver([property_doc], enable_contextual_aliases=True)
    alias_values = _lead_alias_values(leads, resolver)
    alias_expression = _master_alias_match_expression(alias_values)
    if alias_expression:
        for doc in db[MASTER_COLLECTION].find(alias_expression, IDENTITY_PROJECTION):
            code = normalize_identifier(doc.get("codigo"))
            if code:
                identity_docs[code] = doc
    return list(identity_docs.values())


def _captacion_by_ids(db: Any, listing_ids: Iterable[str]) -> list[dict[str, Any]]:
    ids = sorted(set(listing_ids))
    if not ids:
        return []
    projection = {"_id": 0, "listing_id": 1, "image_urls": 1, "main_image_url": 1, "image_urls_count": 1, "fecha_publicacion": 1, "updated_at": 1}
    return list(db[CAPTACION_COLLECTION].find({"listing_id": {"$in": ids}}, projection))


def _eligible_score(doc: Mapping[str, Any], image_count: int) -> tuple[int, str]:
    resolved_operations = resolve_property_operations(doc)
    available_prices = any(
        any(value is not None for value in _price(doc, operation).values())
        for operation in resolved_operations["operations"]
    )
    values = [
        available_prices,
        image_count > 0,
        _path(doc, "ubicacion.comuna") is not None,
        _path(doc, "caracteristicas.dormitorios") is not None,
        _path(doc, "caracteristicas.banos") is not None,
        _path(doc, "caracteristicas.superficie_construida") is not None or _path(doc, "caracteristicas.superficie_terreno") is not None,
    ]
    return sum(values), str(doc.get("codigo") or "")


_PUBLICATION_CATALOG: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("portal_inmobiliario", "Portal Inmobiliario", ("url_pi", "url_mercado_libre")),
    ("mercadolibre", "Mercado Libre", ("url_mercado_libre",)),
    ("toctoc", "TOCTOC", ("url_toctoc",)),
    ("yapo", "Yapo", ("url_yapo",)),
    ("chilepropiedades", "ChilePropiedades", ()),
    ("proppit", "Proppit", ("url_proppit",)),
    ("procasa", "Procasa", ("url_procasa",)),
)


def _verified_publication(record: Mapping[str, Any]) -> bool:
    published = record.get("publicada")
    state = record.get("estado")
    return published is True or state in {1, "1", "active", "activo", "publicada"}


def _publication_presence(
    doc: Mapping[str, Any],
    captures: Iterable[Mapping[str, Any]],
) -> tuple[OwnerPortalPublicationV1, ...]:
    """Expose only portal records with an explicit published/active signal."""

    capture_by_id = {
        normalize_identifier(item.get("listing_id")): item
        for item in captures
        if normalize_identifier(item.get("listing_id"))
    }
    publications = doc.get("publicaciones")
    if not isinstance(publications, Mapping):
        return ()
    result: list[OwnerPortalPublicationV1] = []
    for portal_id, portal_name, url_fields in _PUBLICATION_CATALOG:
        portal = publications.get(portal_id)
        if portal_id == "mercadolibre" and not isinstance(portal, Mapping):
            legacy_portal = publications.get("portal_inmobiliario")
            if isinstance(legacy_portal, Mapping) and _text(legacy_portal.get("url_mercado_libre")):
                portal = legacy_portal
        if not isinstance(portal, Mapping):
            continue
        records = portal.get("publicaciones")
        candidates = list(records.values()) if isinstance(records, Mapping) else []
        for record in candidates:
            if not isinstance(record, Mapping) or not _verified_publication(record):
                continue
            url = None
            for field in url_fields:
                candidate = portal.get(field)
                if isinstance(candidate, list):
                    candidate = candidate[0] if candidate else None
                url = _text(candidate)
                if url:
                    break
            if not url:
                url = _text(record.get("url"))
            if url and not url.casefold().startswith(("http://", "https://")):
                url = None
            identifiers = {
                normalize_identifier(record.get("code")),
                normalize_identifier(record.get("code_unique")),
            }
            capture = next((capture_by_id.get(identifier) for identifier in identifiers if identifier), None)
            result.append(
                OwnerPortalPublicationV1(
                    portal_id=portal_id,
                    portal_name=portal_name,
                    url=url,
                    published_at=_format_date(capture.get("fecha_publicacion")) if capture else None,
                    updated_at=_format_date(capture.get("updated_at")) if capture else None,
                )
            )
            break
    return tuple(result)


def select_preview_property_code(db: Any) -> str | None:
    """Choose a real active SUCRE property deterministically, never hardcoded."""

    base_documents = _find_master_in_batches(
        db,
        {SUCRE_OFFICE_FIELD: SUCRE_OFFICE_VALUE},
        MASTER_BASE_PROJECTION,
    )
    ranked_base = sorted(
        (doc for doc in base_documents if is_active_property(doc)),
        key=lambda doc: _eligible_score(doc, 0),
        reverse=True,
    )
    candidate_codes = [str(doc["codigo"]) for doc in ranked_base[:200] if doc.get("codigo") is not None]
    documents = _find_master_in_batches(
        db,
        {SUCRE_OFFICE_FIELD: SUCRE_OFFICE_VALUE, "codigo": {"$in": candidate_codes}},
        MASTER_PUBLICATION_PROJECTION,
    )
    listing_ids_by_code = {
        str(doc.get("codigo")): _publication_listing_ids(doc)
        for doc in documents
        if doc.get("codigo") is not None
    }
    all_listing_ids = set().union(*listing_ids_by_code.values()) if listing_ids_by_code else set()
    captures = _captacion_by_ids(db, all_listing_ids)
    image_count_by_listing = {
        normalize_identifier(item.get("listing_id")): len(_image_urls(item))
        for item in captures
        if normalize_identifier(item.get("listing_id"))
    }
    best: tuple[tuple[int, str], str] | None = None
    for doc in documents:
        if not is_active_property(doc):
            continue
        image_count = sum(image_count_by_listing.get(listing_id, 0) for listing_id in listing_ids_by_code.get(str(doc.get("codigo")), set()))
        score, code = _eligible_score(doc, image_count)
        if score < 5 or not code:
            continue
        candidate = ((score, code), code)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best else None


def select_owner_intelligence_property_code(
    db: Any,
    as_of: datetime | None = None,
) -> str | None:
    """Choose a real SUCRE case with verified recent demand for the convergent UI.

    This selector is intentionally separate from ``select_preview_property_code``:
    the existing preview remains frozen while the intelligence candidate requires
    a recent, property-linked inquiry and a comparable cohort suitable for the
    owner-facing narrative.
    """

    cutoff = _as_of_or_now(as_of)
    base_documents = [
        doc
        for doc in _find_master_in_batches(
            db,
            {SUCRE_OFFICE_FIELD: SUCRE_OFFICE_VALUE},
            MASTER_PUBLICATION_PROJECTION,
        )
        if is_active_property(doc)
    ]
    if not base_documents:
        return None

    listing_ids_by_code = {
        str(doc.get("codigo")): _publication_listing_ids(doc)
        for doc in base_documents
        if doc.get("codigo") is not None
    }
    all_listing_ids = set().union(*listing_ids_by_code.values()) if listing_ids_by_code else set()
    captures = _captacion_by_ids(db, all_listing_ids)
    image_count_by_listing = {
        normalize_identifier(item.get("listing_id")): len(_image_urls(item))
        for item in captures
        if normalize_identifier(item.get("listing_id"))
    }

    code_by_identifier: dict[str, set[str]] = {}
    alias_by_source: dict[str, dict[str, set[str]]] = {
        source: {} for source, _ in _MASTER_ALIAS_PORTALS
    }
    candidate_documents: dict[str, Mapping[str, Any]] = {}
    for doc in base_documents:
        code = normalize_identifier(doc.get("codigo"))
        if not code:
            continue
        image_count = sum(
            image_count_by_listing.get(listing_id, 0)
            for listing_id in listing_ids_by_code.get(str(doc.get("codigo")), set())
        )
        price = _price(doc)
        has_surface = (
            _number(_path(doc, "caracteristicas.superficie_construida")) is not None
            or _number(_path(doc, "caracteristicas.superficie_terreno")) is not None
        )
        if image_count <= 0 or (price["uf"] is None and price["clp"] is None) or not has_surface:
            continue
        candidate_documents[code] = doc
        code_by_identifier.setdefault(code, set()).add(code)
        aliases = _verified_property_alias_values(doc)
        for source, values in aliases.items():
            for value in values:
                alias_by_source.setdefault(source, {}).setdefault(value, set()).add(code)

    if not candidate_documents:
        return None

    clauses: list[dict[str, Any]] = []
    if code_by_identifier:
        clauses.append({"prospecto.codigo": {"$in": _mongo_identifier_variants(code_by_identifier)}})
    for source, field in (
        ("mercadolibre", "prospecto.codigo_mercadolibre"),
        ("yapo", "prospecto.codigo_yapo"),
        ("toctoc", "prospecto.codigo_propiedad"),
        ("toctoc", "prospecto.propiedad_codigo"),
    ):
        values = alias_by_source.get(source) or {}
        if values:
            clauses.append({field: {"$in": _mongo_identifier_variants(values)}})
    if not clauses:
        return None

    try:
        lead_documents = list(db["leads"].find({"$or": clauses}, LEAD_PROJECTION))
    except Exception:
        return None

    recent_counts: dict[str, int] = {}
    for lead in lead_documents:
        created_at = parse_aware_datetime(lead.get("created_at"))
        if created_at is None or not is_in_previous_window(
            created_at,
            cutoff.astimezone(timezone.utc),
            30,
        ):
            continue
        prospect = lead.get("prospecto") if isinstance(lead.get("prospecto"), Mapping) else {}
        matches: set[str] = set()
        canonical = normalize_identifier(prospect.get("codigo"))
        if canonical:
            matches.update(code_by_identifier.get(canonical, set()))
        for source, field in (
            ("mercadolibre", "codigo_mercadolibre"),
            ("yapo", "codigo_yapo"),
            ("toctoc", "codigo_propiedad"),
            ("toctoc", "propiedad_codigo"),
        ):
            value = normalize_identifier(prospect.get(field))
            if value:
                matches.update(alias_by_source.get(source, {}).get(value, set()))
        for code in matches:
            recent_counts[code] = recent_counts.get(code, 0) + 1

    ranked = sorted(
        (
            doc
            for code, doc in candidate_documents.items()
            if recent_counts.get(code, 0) > 0
        ),
        key=lambda doc: (
            recent_counts.get(normalize_identifier(doc.get("codigo")) or "", 0),
            _eligible_score(
                doc,
                sum(
                    image_count_by_listing.get(listing_id, 0)
                    for listing_id in listing_ids_by_code.get(str(doc.get("codigo")), set())
                ),
            ),
            str(doc.get("codigo") or ""),
        ),
        reverse=True,
    )
    for doc in ranked[:32]:
        code = str(doc.get("codigo"))
        view = get_owner_portal_property_view(db, code, cutoff)
        if (
            view is not None
            and view.inquiries_previous_30d > 0
            and view.data_quality.photo_available
            and view.comparable_cohort is not None
            and view.comparable_cohort.count >= 8
        ):
            return code
    return None


def _safe_property(
    doc: Mapping[str, Any],
    image_urls: list[str],
    captures: Iterable[Mapping[str, Any]] = (),
    operation: str | None = None,
) -> dict[str, Any]:
    resolved_operations = resolve_property_operations(doc)
    requested_operation = operation.strip().casefold() if isinstance(operation, str) else None
    if requested_operation is not None:
        selected_operation = (
            requested_operation if requested_operation in resolved_operations["operations"] else None
        )
    else:
        selected_operation = resolved_operations["primary_operation"]
    selection_required = bool(
        resolved_operations["conflict"]
        or (len(resolved_operations["operations"]) > 1 and selected_operation is None)
        or (operation is not None and selected_operation is None)
    )
    price = _price(doc, selected_operation)
    raw_region = _text(_path(doc, "ubicacion.region")) or ""
    return {
        "code": _text(doc.get("codigo")),
        "operation": selected_operation,
        "operations": tuple(resolved_operations["operations"]),
        "operation_selection_required": selection_required,
        "operation_conflict": bool(resolved_operations["conflict"]),
        "property_type": _property_type(doc) or "",
        "region": raw_region,
        "canonical_region": canonical_region(raw_region),
        "macrozone": region_to_macrozone(raw_region),
        "commune": _text(_path(doc, "ubicacion.comuna")) or "",
        "sector": _text(_path(doc, "ubicacion.sector")) or "",
        "bedrooms": _number(_path(doc, "caracteristicas.dormitorios")),
        "bathrooms": _number(_path(doc, "caracteristicas.banos")),
        "parking": _number(_path(doc, "caracteristicas.estacionamientos")),
        "built_area_m2": _number(_path(doc, "caracteristicas.superficie_construida")),
        "land_area_m2": _number(_path(doc, "caracteristicas.superficie_terreno")),
        "price_uf": price["uf"],
        "price_clp": price["clp"],
        "updated_at_label": _format_date(_path(doc, "estado.ultima_actualizacion")),
        "image_urls": image_urls[:8],
        "publications": _publication_presence(doc, captures),
    }


def _parse_date(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
            try:
                return datetime.strptime(normalized, "%Y-%m-%d").replace(tzinfo=BUSINESS_TZ)
            except ValueError:
                return None
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", normalized):
            try:
                return datetime.strptime(normalized, "%d/%m/%Y").replace(tzinfo=BUSINESS_TZ)
            except ValueError:
                return None
    try:
        return parse_aware_datetime(value, field_name="publication_date")
    except (TypeError, ValueError):
        return None


def _format_timestamp(value: Any) -> str | None:
    parsed = _parse_date(value)
    return parsed.astimezone(timezone.utc).isoformat() if parsed else None


def _format_date(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return datetime.strptime(value.strip(), "%Y-%m-%d").strftime("%d/%m/%Y")
    if isinstance(value, str) and re.fullmatch(r"\d{2}/\d{2}/\d{4}", value.strip()):
        return value.strip()
    parsed = _parse_date(value)
    if parsed is None:
        return None
    return parsed.astimezone(BUSINESS_TZ).strftime("%d/%m/%Y")


def _source_temporal_metadata(
    source_as_of: Any,
    page_as_of: datetime,
) -> tuple[str | None, int | None, bool | None]:
    """Return source date, age and stale flag without changing source data."""

    parsed = _parse_date(source_as_of)
    if parsed is None and isinstance(source_as_of, str):
        text = source_as_of.strip()
        for pattern in ("%Y-%m", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text, pattern).replace(tzinfo=BUSINESS_TZ)
                break
            except ValueError:
                continue
    if parsed is None:
        return (_format_date(source_as_of) or (str(source_as_of) if source_as_of else None), None, None)
    source_date = parsed.astimezone(BUSINESS_TZ).date()
    page_date = page_as_of.astimezone(BUSINESS_TZ).date()
    age_days = (page_date - source_date).days
    return (_format_date(parsed) or parsed.isoformat(), age_days, age_days > 0)


def _format_as_of(as_of: datetime) -> str:
    return as_of.astimezone(timezone.utc).isoformat()


def _as_of_or_now(as_of: datetime | None) -> datetime:
    value = as_of if as_of is not None else datetime.now(BUSINESS_TZ) + timedelta(microseconds=1)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    # Permit only a small clock-skew tolerance for callers constructing an
    # as_of immediately after an event; meaningful future cutoffs are invalid.
    if value > datetime.now(BUSINESS_TZ) + timedelta(seconds=5):
        raise ValueError("as_of cannot be in the future")
    return value


def _market_context(db: Any, prop: Mapping[str, Any], as_of: datetime | None = None) -> dict[str, Any]:
    as_of = _as_of_or_now(as_of)
    commune, property_type = prop["commune"], prop["property_type"]
    operation = prop.get("operation")
    market_client = getattr(db, "client", db)
    cache_key = (
        id(market_client),
        _market_match_key(commune, property_type),
        as_of.astimezone(timezone.utc).date().isoformat(),
        _text(operation) or "",
    )
    now = time.monotonic()
    cached = _MARKET_CONTEXT_CACHE.get(cache_key)
    if cached is not None and cached[1] is market_client and now - cached[0] < _MARKET_CONTEXT_CACHE_TTL_SECONDS:
        return cached[2]
    aggregate_projection = {
        "_id": 0,
        "mercado_venta": 1,
        "rangos_precio_venta": 1,
        "indicadores_mercado": 1,
        "updated_at": 1,
        "fecha_actualizacion": 1,
        "as_of": 1,
        "fecha_corte": 1,
        "source": 1,
    }
    aggregate = db[MARKET_COLLECTION].find_one(
        {"comuna": commune, "tipo_propiedad": property_type},
        aggregate_projection,
    ) or {}
    if not aggregate:
        # The source contains stable match_key values while older rows vary in
        # accents/casing. This remains an exact normalized lookup, never fuzzy.
        aggregate = db[MARKET_COLLECTION].find_one(
            {"match_key": _market_match_key(commune, property_type)},
            aggregate_projection,
        ) or {}
    result = _comparables(db, prop, aggregate, as_of, operation)
    _MARKET_CONTEXT_CACHE[cache_key] = (now, market_client, result)
    if len(_MARKET_CONTEXT_CACHE) > 64:
        oldest_key = min(_MARKET_CONTEXT_CACHE, key=lambda key: _MARKET_CONTEXT_CACHE[key][0])
        _MARKET_CONTEXT_CACHE.pop(oldest_key, None)
    return result


def _comparables(
    db: Any,
    prop: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    as_of: datetime,
    operation: str | None,
) -> dict[str, Any]:
    operation = operation.strip().casefold() if isinstance(operation, str) else None
    if operation not in {"venta", "arriendo"}:
        return {
            "minimum_comparables": 5,
            "comparables_count": 0,
            "median_price_uf": None,
            "median_uf_m2": None,
            "range_price_uf": [None, None],
            "aggregate": {},
            "indicators": {},
            "range_reference": {},
            "valid_rows": [],
            "source": None,
            "aggregate_updated_at": None,
            "market_as_of": None,
            "explanation": "La operación debe estar seleccionada explícitamente para construir comparables.",
        }
    query = {
        "comuna": {"$in": _text_variants(prop["commune"])},
        "tipo_propiedad": {"$in": _text_variants(prop["property_type"])},
        "operacion": {"$in": _text_variants(operation)},
    }
    rows = list(
        db[CAPTACION_COLLECTION].find(
            query,
            {
                "_id": 0,
                "comuna": 1,
                "tipo_propiedad": 1,
                "operacion": 1,
                "precio_uf": 1,
                "superficie": 1,
                "superficie_construida": 1,
                "m2_construidos": 1,
                "m2_totales": 1,
                "dormitorios": 1,
                "banos": 1,
                "listing_id": 1,
                "source_portal": 1,
                "origen": 1,
                "fecha_publicacion": 1,
                "updated_at": 1,
            },
        )
    )
    cutoff = as_of.astimezone(timezone.utc) - timedelta(days=365)
    as_of_utc = as_of.astimezone(timezone.utc)
    valid: list[dict[str, Any]] = []
    seen_listing_ids: set[str] = set()
    for row in rows:
        if _normalized_label(row.get("comuna")) != _normalized_label(prop["commune"]):
            continue
        if _normalized_label(row.get("tipo_propiedad")) != _normalized_label(prop["property_type"]):
            continue
        if _normalized_label(row.get("operacion")) != operation:
            continue
        price = _number(row.get("precio_uf"))
        surface = next(
            (
                number
                for field in ("superficie", "superficie_construida", "m2_construidos", "m2_totales")
                for number in [_number(row.get(field))]
                if number is not None and number > 0
            ),
            None,
        )
        publication_when = _parse_date(row.get("fecha_publicacion"))
        updated_when = _parse_date(row.get("updated_at"))
        when = publication_when or updated_when
        date_basis = (
            "fecha de publicación" if publication_when is not None
            else "última actualización observada" if updated_when is not None
            else None
        )
        if price is None or price <= 0 or surface is None or surface <= 0:
            continue
        if when is not None and (when < cutoff or when >= as_of_utc):
            continue
        listing_id = normalize_identifier(row.get("listing_id"))
        if listing_id and listing_id in seen_listing_ids:
            continue
        if listing_id:
            seen_listing_ids.add(listing_id)
        valid.append(
            {
                "price_uf": price,
                "surface_m2": surface,
                "bedrooms": _number(row.get("dormitorios")),
                "bathrooms": _number(row.get("banos")),
                "portal": _text(row.get("source_portal") or row.get("origen")),
                "listing_id": listing_id,
                "when": when,
                "date_basis": date_basis,
            }
        )
    # ``mercado_comunal`` currently exposes only an audited sale aggregate.
    # Never attach it to an arriendo cohort until a rent aggregate exists.
    market_sale = (
        aggregate.get("mercado_venta")
        if operation == "venta" and isinstance(aggregate.get("mercado_venta"), Mapping)
        else {}
    )
    indicators = (
        aggregate.get("indicadores_mercado")
        if operation == "venta" and isinstance(aggregate.get("indicadores_mercado"), Mapping)
        else {}
    )
    ranges = (
        aggregate.get("rangos_precio_venta")
        if operation == "venta" and isinstance(aggregate.get("rangos_precio_venta"), Mapping)
        else {}
    )
    source = (
        aggregate.get("source")
        if operation == "venta" and isinstance(aggregate.get("source"), Mapping)
        else {}
    )
    market_as_of = _format_date(
        source.get("fecha_reporte")
        or source.get("report_date")
        or (aggregate.get("as_of") if operation == "venta" else None)
        or (aggregate.get("fecha_corte") if operation == "venta" else None)
        or (aggregate.get("fecha_actualizacion") if operation == "venta" else None)
        or (aggregate.get("updated_at") if operation == "venta" else None)
    )
    if len(valid) >= 5:
        prices = [item["price_uf"] for item in valid]
        uf_m2 = [item["price_uf"] / item["surface_m2"] for item in valid]
        return {
            "minimum_comparables": 5,
            "comparables_count": len(valid),
            "median_price_uf": round(statistics.median(prices), 2),
            "median_uf_m2": round(statistics.median(uf_m2), 2),
            "range_price_uf": [round(min(prices), 2), round(max(prices), 2)],
            "aggregate": dict(market_sale),
            "indicators": dict(indicators),
            "range_reference": dict(ranges),
            "valid_rows": valid,
            "source": dict(source) if source else None,
            "aggregate_updated_at": (
                aggregate.get("updated_at") or aggregate.get("fecha_actualizacion")
                if operation == "venta"
                else None
            ),
            "market_as_of": market_as_of,
            "explanation": "Comparables descriptivos: misma comuna, tipo y operación; precio y superficie válidos; fecha reciente cuando está disponible.",
        }
    return {
        "minimum_comparables": 5,
        "comparables_count": len(valid),
        "median_price_uf": None,
        "median_uf_m2": None,
        "range_price_uf": [None, None],
        "aggregate": dict(market_sale),
        "indicators": dict(indicators),
        "range_reference": dict(ranges),
        "valid_rows": valid,
        "source": dict(source) if source else None,
        "aggregate_updated_at": (
            aggregate.get("updated_at") or aggregate.get("fecha_actualizacion")
            if operation == "venta"
            else None
        ),
        "market_as_of": market_as_of,
        "explanation": "Se requieren al menos 5 comparables válidos para mostrar una mediana o rango representativo.",
    }


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return round(ordered[0], 2)
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    value = ordered[lower] if lower == upper else ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(value, 2)


def _comparable_examples(
    rows: Iterable[Mapping[str, Any]],
    *,
    median_uf: float,
) -> tuple[OwnerPortalComparableExampleV1, ...]:
    """Select up to three anonymous examples only when the portal is known."""

    candidates = [
        row for row in rows
        if _text(row.get("portal"))
        and _number(row.get("price_uf")) is not None
        and _number(row.get("surface_m2")) is not None
    ]
    candidates.sort(
        key=lambda row: (
            abs(float(row["price_uf"]) - median_uf),
            str(row.get("portal") or ""),
            str(row.get("listing_id") or ""),
        )
    )
    examples: list[OwnerPortalComparableExampleV1] = []
    for index, row in enumerate(candidates[:3]):
        price = float(row["price_uf"])
        surface = float(row["surface_m2"])
        examples.append(
            OwnerPortalComparableExampleV1(
                label=f"Comparable {chr(65 + index)}",
                price_uf=round(price, 2),
                surface_m2=round(surface, 2),
                bedrooms=_number(row.get("bedrooms")),
                bathrooms=_number(row.get("bathrooms")),
                uf_m2=round(price / surface, 2),
                portal=_text(row.get("portal")) or "Portal no especificado",
                observed_at=_format_date(row.get("when")),
                date_basis=_text(row.get("date_basis")),
            )
        )
    return tuple(examples)


def _cohort_statistics(
    rows: list[Mapping[str, Any]],
    *,
    operation: str,
    level: str,
    label: str,
    surface_rule: str,
    bedroom_rule: str,
    bathroom_rule: str,
    date_rule: str,
    counts: Mapping[str, int],
    as_of: datetime | None = None,
    current_price_uf: float | None = None,
) -> OwnerPortalComparableCohortV1 | None:
    if len(rows) < 8:
        return None
    prices = [float(row["price_uf"]) for row in rows]
    p10 = _percentile(prices, .10)
    p25 = _percentile(prices, .25)
    p60 = _percentile(prices, .60)
    median = _percentile(prices, .50)
    p75 = _percentile(prices, .75)
    p90 = _percentile(prices, .90)
    if None in {p10, p25, p60, median, p75, p90}:
        return None
    uf_m2 = [float(row["price_uf"]) / float(row["surface_m2"]) for row in rows]
    cohort_as_of = _format_as_of(as_of) if as_of is not None else None
    cohort_cutoff = (
        as_of.astimezone(timezone.utc) - timedelta(days=365)
        if as_of is not None
        else None
    )
    fingerprint = (
        cohort_fingerprint(
            rows,
            operation=operation,
            cohort_level=level,
            rule_version=COHORT_RULE_VERSION,
            as_of=as_of,
            cutoff=cohort_cutoff,
        )
        if as_of is not None and cohort_cutoff is not None
        else None
    )
    ecdf_equal_or_below = (
        sum(price <= current_price_uf for price in prices)
        if current_price_uf is not None
        else None
    )
    market_ecdf = (
        round(ecdf_equal_or_below / len(prices) * 100, 2)
        if ecdf_equal_or_below is not None and prices
        else None
    )
    return OwnerPortalComparableCohortV1(
        operation=operation,
        level=level,
        label=label,
        count=len(rows),
        p10_uf=p10,
        p25_uf=p25,
        median_uf=median,
        p75_uf=p75,
        p90_uf=p90,
        median_uf_m2=_percentile(uf_m2, .50),
        surface_rule=surface_rule,
        bedroom_rule=bedroom_rule,
        bathroom_rule=bathroom_rule,
        date_rule=date_rule,
        broad_count=int(counts["broad"]),
        similar_count=int(counts["similar"]),
        high_similarity_count=int(counts["high_similarity"]),
        examples=_comparable_examples(rows, median_uf=median),
        p60_uf=p60,
        market_ecdf=market_ecdf,
        market_ecdf_equal_or_below=ecdf_equal_or_below,
        cohort_as_of=cohort_as_of,
        cohort_fingerprint=fingerprint,
    )


def _select_comparable_cohort(
    prop: Mapping[str, Any],
    market: Mapping[str, Any],
    as_of: datetime | None = None,
) -> OwnerPortalComparableCohortV1 | None:
    """Select the most similar cohort with the explicit N>=8 guard."""

    broad = list(market.get("valid_rows") or [])
    operation = prop.get("operation")
    if operation not in {"venta", "arriendo"}:
        return None
    target_surface = _number(prop.get("built_area_m2")) or _number(prop.get("land_area_m2"))
    target_bedrooms = _number(prop.get("bedrooms"))
    target_bathrooms = _number(prop.get("bathrooms"))

    def dimension_matches(row: Mapping[str, Any], field: str, target: float | None, tolerance: float) -> bool:
        value = _number(row.get(field))
        return target is None or value is None or abs(value - target) <= tolerance

    similar = [
        row for row in broad
        if target_surface is not None
        and abs(float(row["surface_m2"]) - target_surface) / target_surface <= .25
        and dimension_matches(row, "bedrooms", target_bedrooms, 1)
    ]
    high_similarity = [
        row for row in broad
        if target_surface is not None
        and abs(float(row["surface_m2"]) - target_surface) / target_surface <= .20
        and target_bedrooms is not None
        and _number(row.get("bedrooms")) is not None
        and abs(float(row["bedrooms"]) - target_bedrooms) <= 1
        and target_bathrooms is not None
        and _number(row.get("bathrooms")) is not None
        and abs(float(row["bathrooms"]) - target_bathrooms) <= 1
        and row.get("when") is not None
    ]
    counts = {"broad": len(broad), "similar": len(similar), "high_similarity": len(high_similarity)}
    common_date_rule = "fecha dentro de 365 días o ausente; ausencia aceptada"
    selected = (
        _cohort_statistics(
            high_similarity,
            operation=operation,
            level="high_similarity",
            label="Alta similitud",
            surface_rule="superficie ±20%",
            bedroom_rule="dormitorios iguales o ±1",
            bathroom_rule="baños iguales o ±1",
            date_rule="fecha reciente válida",
            counts=counts,
            as_of=as_of,
            current_price_uf=_number(prop.get("price_uf")),
        )
        or _cohort_statistics(
            similar,
            operation=operation,
            level="similar",
            label="Propiedades similares",
            surface_rule="superficie ±25%",
            bedroom_rule="dormitorios iguales o ±1 cuando existen",
            bathroom_rule="sin filtro adicional",
            date_rule=common_date_rule,
            counts=counts,
            as_of=as_of,
            current_price_uf=_number(prop.get("price_uf")),
        )
        or _cohort_statistics(
            broad,
            operation=operation,
            level="broad",
            label="Mercado amplio",
            surface_rule="misma comuna, tipo y operación; superficie válida",
            bedroom_rule="sin filtro adicional",
            bathroom_rule="sin filtro adicional",
            date_rule=common_date_rule,
            counts=counts,
            as_of=as_of,
            current_price_uf=_number(prop.get("price_uf")),
        )
    )
    return selected


def _market_context_dto(
    prop: Mapping[str, Any],
    market: Mapping[str, Any],
    page_as_of: datetime | None = None,
) -> OwnerPortalMarketContextV1 | None:
    """Build the small, source-labelled local snapshot used by the template."""

    aggregate = market.get("aggregate") if isinstance(market.get("aggregate"), Mapping) else {}
    indicators = market.get("indicators") if isinstance(market.get("indicators"), Mapping) else {}
    ranges = market.get("range_reference") if isinstance(market.get("range_reference"), Mapping) else {}
    if not aggregate and not indicators and not ranges and not market.get("market_as_of"):
        return None
    sale = aggregate
    source = market.get("source")
    source_ref = None
    if isinstance(source, Mapping):
        filename = _text(source.get("filename"))
        report_date = _format_date(source.get("report_date") or source.get("fecha_reporte"))
        source_ref = " · ".join(item for item in (filename, f"corte {report_date}" if report_date else None) if item)
    source_as_of, source_age_days, is_stale = _source_temporal_metadata(
        market.get("market_as_of"),
        page_as_of or datetime.now(BUSINESS_TZ),
    )
    return OwnerPortalMarketContextV1(
        scope="comuna",
        geography=_text(prop.get("commune")) or "",
        property_type=_text(prop.get("property_type")) or "",
        available=True,
        comparables_count=int(market.get("comparables_count") or 0),
        median_price_uf=_number(market.get("median_price_uf")),
        median_uf_m2=_number(market.get("median_uf_m2")),
        public_uf_m2=_number(sale.get("uf_m2_publicacion_actual")),
        effective_uf_m2=_number(sale.get("uf_m2_venta_efectiva_actual")),
        effective_uf_m2_semantics=(
            "Valor agregado reportado por el informe comunal; no corresponde a una transacción individual verificable."
            if _number(sale.get("uf_m2_venta_efectiva_actual")) is not None else None
        ),
        price_variation_12m_pct=_number(sale.get("variacion_uf_m2_12m")),
        active_listings=_integer(sale.get("publicaciones_activas")),
        active_listings_semantics=(
            "Publicaciones activas observadas en el corte; no equivale a propiedades únicas."
            if _integer(sale.get("publicaciones_activas")) is not None else None
        ),
        total_listings=_integer(sale.get("publicaciones_totales")),
        trend=_text(sale.get("tendencia_publicaciones")),
        liquidity=_text(indicators.get("liquidez")),
        competition=_text(indicators.get("nivel_competencia")),
        range_price_uf=tuple(market.get("range_price_uf") or [None, None]),
        source_name="mercado_comunal · snapshot interno",
        source_reference=source_ref or "mercado_comunal",
        retrieved_at=_format_timestamp(
            market.get("aggregate_retrieved_at")
            or market.get("aggregate_updated_at")
            or market.get("market_as_of")
        ),
        source_as_of=source_as_of,
        source_age_days=source_age_days,
        is_stale=is_stale,
    )


def _positioning(
    current_price_uf: float | None,
    cohort: OwnerPortalComparableCohortV1 | None,
) -> OwnerPortalPositioningV1 | None:
    if current_price_uf is None or cohort is None:
        return None
    p10, p90 = cohort.p10_uf, cohort.p90_uf
    if p10 <= 0 or p90 < p10:
        return None
    if p90 == p10:
        marker = 50.0
    else:
        marker = 8.0 + 84.0 * max(0.0, min(1.0, (current_price_uf - p10) / (p90 - p10)))
    if current_price_uf < cohort.p25_uf:
        label = "Bajo el tramo central observado"
    elif current_price_uf > cohort.p75_uf:
        label = "Sobre el tramo central observado"
    else:
        label = "Dentro del tramo central observado"
    state = "UNAVAILABLE"
    if cohort.p60_uf is not None and current_price_uf <= cohort.p60_uf:
        state = "ALIGNED"
    elif current_price_uf <= cohort.p75_uf:
        state = "SLIGHTLY_HIGH"
    elif current_price_uf <= cohort.p90_uf:
        state = "HIGH"
    else:
        state = "VERY_HIGH"
    ecdf_equal_or_below = cohort.market_ecdf_equal_or_below
    market_ecdf = cohort.market_ecdf
    owner_text = None
    if market_ecdf is not None and ecdf_equal_or_below is not None:
        approximate_decile = max(1, min(9, round(market_ecdf / 10)))
        owner_text = (
            f"Aproximadamente {approximate_decile} de cada 10 publicaciones comparables "
            "tienen un precio igual o inferior al publicado."
        )
    return OwnerPortalPositioningV1(
        price_uf=round(current_price_uf, 2),
        p10_uf=p10,
        p25_uf=cohort.p25_uf,
        median_uf=cohort.median_uf,
        p75_uf=cohort.p75_uf,
        p90_uf=p90,
        marker_pct=round(marker, 1),
        label=label,
        comparable_count=cohort.count,
        cohort_label=cohort.label,
        market_position_state=state,
        market_ecdf=market_ecdf,
        market_ecdf_equal_or_below=ecdf_equal_or_below,
        market_position_owner_text=owner_text,
    )


def _stored_national_indicators(db: Any, page_as_of: datetime) -> list[MarketIndicatorV1]:
    """Read valid upstream snapshots only; the portal never ingests them."""

    try:
        rows = list(
            db[MARKET_INTELLIGENCE_COLLECTION].find(
                {"scope": "nacional", "status": "valid"},
                {
                    "_id": 0,
                    "indicator_id": 1,
                    "scope": 1,
                    "geography": 1,
                    "value": 1,
                    "unit": 1,
                    "reference_period": 1,
                    "source_name": 1,
                    "source_reference": 1,
                    "retrieved_at_utc": 1,
                    "source_published_at": 1,
                },
            ).sort("reference_period", -1).limit(3)
        )
    except Exception:
        return []
    result: list[MarketIndicatorV1] = []
    for row in rows:
        if not isinstance(row, Mapping) or not _text(row.get("indicator_id")):
            continue
        value = row.get("value")
        if _number(value) is None:
            continue
        source_as_of, source_age_days, is_stale = _source_temporal_metadata(
            row.get("reference_period"),
            page_as_of,
        )
        result.append(
            MarketIndicatorV1(
                indicator_id=_text(row.get("indicator_id")) or "",
                scope=_text(row.get("scope")) or "nacional",
                geography=_text(row.get("geography")) or "Chile",
                value=round(float(value), 6),
                unit=_text(row.get("unit")) or "",
                period=_text(row.get("reference_period")) or "",
                source_name=_text(row.get("source_name")) or "Fuente oficial",
                source_url=_text(row.get("source_reference")),
                retrieved_at=_text(row.get("retrieved_at_utc")) or "",
                valid_until=None,
                source_as_of=source_as_of,
                source_age_days=source_age_days,
                is_stale=is_stale,
            )
        )
    return result


def _national_indicators(db: Any, as_of: datetime) -> tuple[MarketIndicatorV1, ...]:
    """Read stored snapshots and UF cache; never call a public source per pageview."""

    market_client = getattr(db, "client", db)
    cache_key = id(market_client)
    now = time.monotonic()
    cached = _NATIONAL_INDICATORS_CACHE.get(cache_key)
    if cached is not None and cached[1] is market_client and now - cached[0] < _NATIONAL_INDICATORS_CACHE_TTL_SECONDS:
        return cached[2]

    result = _stored_national_indicators(db, as_of)

    row = db["uf_cache"].find_one(
        {},
        {"_id": 0, "valor": 1, "fecha": 1, "fuente": 1, "actualizado_at": 1, "valid_until": 1},
        sort=[("fecha", -1)],
    )
    if isinstance(row, Mapping):
        value = _number(row.get("valor"))
        if value is not None and value > 0:
            period = _format_date(row.get("fecha")) or _text(row.get("fecha")) or ""
            retrieved_at = _format_timestamp(row.get("actualizado_at")) or _format_as_of(as_of)
            source_as_of, source_age_days, is_stale = _source_temporal_metadata(
                row.get("fecha"),
                as_of,
            )
            result.insert(
                0,
                MarketIndicatorV1(
                    indicator_id="uf_reference",
                    scope="nacional",
                    geography="Chile",
                    value=round(value, 2),
                    unit="CLP por UF",
                    period=period,
                    source_name=_text(row.get("fuente")) or "UF · snapshot interno",
                    source_url="https://mindicador.cl/",
                    retrieved_at=retrieved_at,
                    valid_until=_format_date(row.get("valid_until")),
                    source_as_of=source_as_of,
                    source_age_days=source_age_days,
                    is_stale=is_stale,
                ),
            )
    unique: list[MarketIndicatorV1] = []
    seen: set[str] = set()
    for indicator in result:
        if indicator.indicator_id in seen:
            continue
        seen.add(indicator.indicator_id)
        unique.append(indicator)
    final = tuple(unique[:3])
    _NATIONAL_INDICATORS_CACHE[cache_key] = (now, market_client, final)
    if len(_NATIONAL_INDICATORS_CACHE) > 32:
        oldest_key = min(_NATIONAL_INDICATORS_CACHE, key=lambda key: _NATIONAL_INDICATORS_CACHE[key][0])
        _NATIONAL_INDICATORS_CACHE.pop(oldest_key, None)
    return final


def _national_context_note(indicators: tuple[MarketIndicatorV1, ...]) -> str:
    if indicators:
        return (
            "La UF ayuda a comparar precios publicados en una unidad común. Las referencias nacionales tienen su propia "
            "fecha y fuente: sirven como contexto, pero no explican por sí solas la respuesta de una propiedad ni "
            "sustituyen una tasación."
        )
    return (
        "El contexto nacional ayuda a ordenar la lectura del precio publicado. Cuando una referencia está disponible, "
        "se muestra con su propia fecha y fuente; la página no consulta fuentes externas durante cada visita."
    )


def _market_intelligence_snapshot(
    db: Any,
    as_of: datetime,
) -> MarketIntelligenceSnapshotV1 | None:
    """Expose the current internal market snapshot boundary, without ingestion."""

    indicators = _national_indicators(db, as_of)
    if not indicators:
        return None
    period = indicators[0].period.replace("/", "-")
    return MarketIntelligenceSnapshotV1(
        snapshot_id=f"uf_cache:{period}",
        scope="nacional",
        geography="Chile",
        indicators=indicators,
        source_name="UF · snapshot interno",
        retrieved_at=indicators[0].retrieved_at,
        valid_until=indicators[0].valid_until,
        source_as_of=indicators[0].source_as_of,
        source_age_days=indicators[0].source_age_days,
        is_stale=indicators[0].is_stale,
    )


def _regional_context_note(prop: Mapping[str, Any]) -> str:
    region = _text(prop.get("region")) or "la región"
    commune = _text(prop.get("commune")) or "la comuna"
    return (
        f"{region}: el corte disponible no contiene una serie regional comparable; por eso la "
        f"lectura se concentra en {commune}, sin inferir un promedio para toda la región."
    )


def _snapshot(db: Any, code: str, as_of: datetime) -> dict[str, Any] | None:
    local_date = as_of.astimezone(BUSINESS_TZ).date().isoformat()
    return db[SNAPSHOT_COLLECTION].find_one(
        {"property_code": code, "snapshot_date_local": {"$lte": local_date}},
        {"_id": 0},
        sort=[("snapshot_date_local", -1)],
    )


def _lead_metrics_for_property(
    db: Any,
    property_doc: Mapping[str, Any],
    as_of: datetime,
) -> dict[str, Any]:
    """Resolve only leads that can identify this property.

    ``created_at`` is intentionally filtered in Python.  The live collection
    contains mostly ISO strings with mixed offsets plus a small number of
    missing/naive values, so a Mongo temporal predicate would not preserve the
    existing ``parse_aware_datetime`` semantics.
    """

    property_code = normalize_identifier(property_doc.get("codigo")) or ""
    lead_query = _lead_candidate_query(property_doc)
    leads = list(db["leads"].find(lead_query, LEAD_PROJECTION))
    properties = _property_identity_candidates(db, property_doc, leads)
    resolver = build_property_identity_resolver(properties, enable_contextual_aliases=True)
    linkage = LeadLinkageService(resolver)
    records = linkage.link_leads(leads)
    linked_pairs = [
        (lead, record)
        for lead, record in zip(leads, records)
        if record.status in {LinkageStatus.EXACT_CANONICAL, LinkageStatus.EXACT_ALIAS}
        and record.property_code == property_code
    ]
    linked = [record for _lead, record in linked_pairs]
    cutoff = as_of.astimezone(timezone.utc)
    exact_lead_dates = tuple(
        record.created_at.astimezone(timezone.utc)
        for record in linked
        if record.created_at is not None
    )
    publication_captures = _captacion_by_ids(db, _publication_listing_ids(property_doc))
    safe_publication_property = _safe_property(property_doc, [], publication_captures)
    active_publication_portals = tuple(
        publication.portal_id
        for publication in safe_publication_property["publications"]
    )
    publication_dates = {}
    for publication in safe_publication_property["publications"]:
        published_at = _parse_date(publication.published_at)
        if published_at is not None:
            publication_dates[publication.portal_id] = (published_at, "propiedades_captacion.fecha_publicacion")

    origins_by_record: list[tuple[str | None, Any]] = []
    source_observable_from: dict[str, datetime] = {}
    crm_capable_portals: set[str] = set()
    for lead, record in linked_pairs:
        origin = resolve_lead_origin(lead).get("canonical_origin")
        origins_by_record.append((origin, record))
        if origin:
            crm_capable_portals.add(origin)
            if record.created_at is not None:
                previous = source_observable_from.get(origin)
                if previous is None or record.created_at < previous:
                    source_observable_from[origin] = record.created_at
    evidence = build_observation_evidence(
        property_code=property_code,
        operation=property_doc.get("tipo_operacion", {}).get("tipo") if isinstance(property_doc.get("tipo_operacion"), Mapping) else None,
        as_of=as_of,
        active_publication_portals=active_publication_portals,
        publication_dates=publication_dates,
        crm_lead_capable_portals=tuple(sorted(crm_capable_portals)),
        source_observable_from=source_observable_from,
        provenance={"source": "universo_cartera_prop360 + leads + propiedades_captacion"},
    )
    covered_30 = {
        origin
        for origin, record in origins_by_record
        if origin
        and record.created_at is not None
        and is_in_previous_window(record.created_at, cutoff, 30)
    }
    covered_90 = {
        origin
        for origin, record in origins_by_record
        if origin
        and record.created_at is not None
        and is_in_previous_window(record.created_at, cutoff, 90)
    }
    coverage_30 = derive_lead_source_coverage(
        property_code=property_code,
        window=evidence.window_30d,
        active_publication_portals=active_publication_portals,
        crm_lead_capable_portals=tuple(sorted(crm_capable_portals)),
        covered_portals=tuple(sorted(covered_30)),
    )
    coverage_90 = derive_lead_source_coverage(
        property_code=property_code,
        window=evidence.window_90d,
        active_publication_portals=active_publication_portals,
        crm_lead_capable_portals=tuple(sorted(crm_capable_portals)),
        covered_portals=tuple(sorted(covered_90)),
    )
    demand_30 = demand_observation(
        count=sum(record.created_at is not None and is_in_previous_window(record.created_at, cutoff, 30) for record in linked),
        window=evidence.window_30d,
        source_coverage=coverage_30.status,
    )
    demand_90 = demand_observation(
        count=sum(record.created_at is not None and is_in_previous_window(record.created_at, cutoff, 90) for record in linked),
        window=evidence.window_90d,
        source_coverage=coverage_90.status,
    )
    return {
        "total_linked_sucre": len(linked),
        "distinct_properties": 1 if linked else 0,
        "previous_7d": sum(record.created_at is not None and is_in_previous_window(record.created_at, cutoff, 7) for record in linked),
        "previous_30d": sum(record.created_at is not None and is_in_previous_window(record.created_at, cutoff, 30) for record in linked),
        "property_previous_7d": sum(record.created_at is not None and is_in_previous_window(record.created_at, cutoff, 7) for record in linked),
        "property_previous_30d": sum(record.created_at is not None and is_in_previous_window(record.created_at, cutoff, 30) for record in linked),
        "property_previous_90d": sum(record.created_at is not None and is_in_previous_window(record.created_at, cutoff, 90) for record in linked),
        "demand_signal_30d": demand_30["demand_signal"],
        "demand_signal_90d": demand_90["demand_signal"],
        "demand_confidence_30d": demand_30["demand_confidence"],
        "demand_confidence_90d": demand_90["demand_confidence"],
        "demand_source_coverage_30d": coverage_30.status,
        "demand_source_coverage_90d": coverage_90.status,
        "observation_window_30d": evidence.window_30d,
        "observation_window_90d": evidence.window_90d,
        "exact_lead_dates": exact_lead_dates,
        "exact_canonical": sum(record.status is LinkageStatus.EXACT_CANONICAL for record in linked),
        "exact_alias": sum(record.status is LinkageStatus.EXACT_ALIAS for record in linked),
        "excluded_other_office_linked": 0,
        "global_quality": linkage.metrics(records),
    }


def _lead_metrics(db: Any, property_code: str, as_of: datetime) -> dict[str, Any]:
    """Compatibility wrapper for callers that only have a property code."""

    docs = _find_master(db, str(property_code), projection=MASTER_PUBLICATION_PROJECTION)
    if not docs:
        return {
            "total_linked_sucre": 0,
            "distinct_properties": 0,
            "previous_7d": 0,
            "previous_30d": 0,
            "property_previous_7d": 0,
            "property_previous_30d": 0,
            "property_previous_90d": 0,
            "demand_signal_30d": "ZERO_UNCERTAIN",
            "demand_signal_90d": "ZERO_UNCERTAIN",
            "demand_confidence_30d": "unknown",
            "demand_confidence_90d": "unknown",
            "exact_canonical": 0,
            "exact_alias": 0,
            "excluded_other_office_linked": 0,
            "global_quality": {},
        }
    return _lead_metrics_for_property(db, docs[0], as_of)


_MONTH_LABELS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def _activity_series(
    exact_lead_dates: Iterable[datetime],
    as_of: datetime,
) -> tuple[OwnerPortalActivityPointV1, ...]:
    """Build up to twelve weekly buckets from exact linked lead timestamps."""

    as_of_utc = as_of.astimezone(timezone.utc)
    current_week = as_of_utc.date() - timedelta(days=as_of_utc.weekday())
    first_week = current_week - timedelta(days=7 * 11)
    counts: dict[datetime.date, int] = {}
    for value in exact_lead_dates:
        if value is None:
            continue
        timestamp = value.astimezone(timezone.utc)
        if timestamp >= as_of_utc:
            continue
        week = timestamp.date() - timedelta(days=timestamp.weekday())
        if first_week <= week <= current_week:
            counts[week] = counts.get(week, 0) + 1
    if not counts:
        return ()
    maximum = max(counts.values())
    points: list[OwnerPortalActivityPointV1] = []
    for index in range(12):
        week = first_week + timedelta(days=7 * index)
        count = counts.get(week, 0)
        height = 0 if count == 0 else 28 + round(112 * count / maximum)
        points.append(
            OwnerPortalActivityPointV1(
                period=week.isoformat(),
                label=f"{week.day:02d} {_MONTH_LABELS[week.month - 1]}",
                count=count,
                x=16 + index * 58,
                y=164 - height,
                height=height,
            )
        )
    return tuple(points)


def _timeline(
    publications: Iterable[OwnerPortalPublicationV1],
    *,
    updated_at_label: str | None,
    previous_price: OwnerPortalPriceV1 | None,
    last_price_change_at: str | None,
    exact_lead_dates: Iterable[datetime],
) -> tuple[OwnerPortalTimelineEventV1, ...]:
    """Create a short timeline from dates already verified in source records."""

    events: list[tuple[datetime, OwnerPortalTimelineEventV1]] = []

    for publication in publications:
        if not publication.published_at:
            continue
        event_date = _parse_date(publication.published_at)
        if event_date is None:
            continue
        events.append(
            (
                event_date,
                OwnerPortalTimelineEventV1(
                    period=publication.published_at,
                    label=f"Publicación activa en {publication.portal_name}",
                    detail="Registro de publicación con fecha verificada.",
                    source=publication.portal_name,
                ),
            )
        )

    if previous_price is not None and last_price_change_at:
        event_date = _parse_date(last_price_change_at)
        if event_date is not None:
            events.append(
                (
                    event_date,
                    OwnerPortalTimelineEventV1(
                        period=_format_date(event_date) or last_price_change_at,
                        label="Cambio de precio registrado",
                        detail="El historial interno registra una variación anterior del precio.",
                        source="historial de propiedad",
                    ),
                )
            )

    if updated_at_label:
        event_date = _parse_date(updated_at_label)
        if event_date is not None:
            events.append(
                (
                    event_date,
                    OwnerPortalTimelineEventV1(
                        period=updated_at_label,
                        label="Actualización de ficha",
                        detail="Fecha de actualización disponible en la ficha maestra.",
                        source="ficha maestra",
                    ),
                )
            )

    by_local_date: dict[datetime.date, int] = {}
    for value in exact_lead_dates:
        if value is None:
            continue
        timestamp = value.astimezone(timezone.utc)
        by_local_date[timestamp.date()] = by_local_date.get(timestamp.date(), 0) + 1
    for day, count in by_local_date.items():
        event_date = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        events.append(
            (
                event_date,
                OwnerPortalTimelineEventV1(
                    period=event_date.astimezone(BUSINESS_TZ).strftime("%d/%m/%Y"),
                    label="Consulta registrada",
                    detail=(
                        f"{count} consulta registrada en esta fecha."
                        if count == 1
                        else f"{count} consultas registradas en esta fecha."
                    ),
                    source="registro de consultas",
                ),
            )
        )

    events.sort(key=lambda item: item[0], reverse=True)
    return tuple(item[1] for item in events[:12])


def _engine_exposure_state(publications: Iterable[OwnerPortalPublicationV1]) -> str:
    """Map verified active publication rows to the Engine V1 exposure enum."""

    active_count = sum(1 for _publication in publications)
    if active_count == 0:
        return "LOW"
    if active_count == 1:
        return "PARTIAL"
    return "ADEQUATE"


def get_owner_portal_property_view(
    db: Any,
    property_code: str,
    as_of: datetime | None = None,
    operation: str | None = None,
) -> OwnerPortalPropertyViewV1 | None:
    """Build one explicit, read-only, PII-free owner portal DTO."""

    cutoff = _as_of_or_now(as_of)
    docs = _find_master(db, str(property_code), projection=MASTER_PUBLICATION_PROJECTION)
    if not docs or not is_procasa_sucre_property(docs[0]) or not is_active_property(docs[0]):
        return None

    doc = docs[0]
    captures = _captacion_by_ids(db, _publication_listing_ids(doc))
    image_urls: list[str] = []
    for capture in captures:
        for image in _image_urls(capture):
            if image not in image_urls:
                image_urls.append(image)

    prop = _safe_property(doc, image_urls, captures, operation=operation)
    code = prop["code"] or str(property_code)
    selection_required = bool(prop["operation_selection_required"])
    snapshot = None if selection_required else _snapshot(db, code, cutoff)
    leads = _lead_metrics_for_property(db, doc, cutoff)
    market = _market_context(db, prop, cutoff)
    national_indicators = _national_indicators(db, cutoff)

    previous_uf = _number(snapshot.get("previous_price_uf")) if snapshot else None
    previous_clp = _number(snapshot.get("previous_price_clp")) if snapshot else None
    previous_price = (
        OwnerPortalPriceV1(uf=previous_uf, clp=previous_clp)
        if previous_uf is not None or previous_clp is not None
        else None
    )
    last_price_change_at = _format_timestamp(snapshot.get("last_price_change_at")) if snapshot else None
    price_history_available = previous_price is not None and last_price_change_at is not None
    current_price = OwnerPortalPriceV1(uf=prop["price_uf"], clp=prop["price_clp"])
    cohort = _select_comparable_cohort(prop, market, cutoff)
    selected_market = dict(market)
    if cohort is None:
        selected_market.update(
            {
                "comparables_count": 0,
                "median_price_uf": None,
                "median_uf_m2": None,
                "range_price_uf": [None, None],
            }
        )
    else:
        selected_market.update(
            {
                "comparables_count": cohort.count,
                "median_price_uf": cohort.median_uf,
                "median_uf_m2": cohort.median_uf_m2,
                "range_price_uf": [cohort.p10_uf, cohort.p90_uf],
            }
        )
    local_context = _market_context_dto(prop, selected_market, cutoff)
    positioning = _positioning(prop["price_uf"], cohort)
    activity_series = _activity_series(leads.get("exact_lead_dates", ()), cutoff)
    timeline = _timeline(
        prop["publications"],
        updated_at_label=prop.get("updated_at_label"),
        previous_price=previous_price,
        last_price_change_at=last_price_change_at,
        exact_lead_dates=leads.get("exact_lead_dates", ()),
    )
    market_intelligence_snapshot = _market_intelligence_snapshot(db, cutoff)
    market_available = cohort is not None and not selection_required
    data_quality = OwnerPortalDataQualityV1(
        photo_available=bool(image_urls),
        price_available=current_price.uf is not None or current_price.clp is not None,
        surface_available=prop["built_area_m2"] is not None or prop["land_area_m2"] is not None,
        bedrooms_available=prop["bedrooms"] is not None,
        bathrooms_available=prop["bathrooms"] is not None,
        parking_available=prop["parking"] is not None,
        market_data_available=market_available,
        price_history_available=price_history_available,
        lead_linkage_available=True,
    )
    page_as_of = _format_as_of(cutoff)
    market_position_state = positioning.market_position_state if positioning else "UNAVAILABLE"
    market_ecdf = positioning.market_ecdf if positioning else None
    market_ecdf_equal_or_below = positioning.market_ecdf_equal_or_below if positioning else None
    market_position_owner_text = positioning.market_position_owner_text if positioning else None
    uf_indicator = next(
        (indicator for indicator in national_indicators if indicator.indicator_id == "uf_reference"),
        None,
    )
    provenance = OwnerPortalProvenanceV1(
        page_as_of=page_as_of,
        property_source=f"{MASTER_COLLECTION} · código {code}",
        lead_source="leads · LeadLinkageService · exact canonical/alias linkage",
        cohort_source=f"{CAPTACION_COLLECTION} · {COHORT_RULE_VERSION}",
        cohort_as_of=cohort.cohort_as_of if cohort else None,
        cohort_fingerprint=cohort.cohort_fingerprint if cohort else None,
        market_context_source_as_of=local_context.source_as_of if local_context else None,
        market_context_source_age_days=local_context.source_age_days if local_context else None,
        market_context_is_stale=local_context.is_stale if local_context else None,
        uf_source_as_of=uf_indicator.source_as_of if uf_indicator else None,
        uf_source_age_days=uf_indicator.source_age_days if uf_indicator else None,
        uf_is_stale=uf_indicator.is_stale if uf_indicator else None,
    )
    active_portals = tuple(publication.portal_id for publication in prop["publications"])
    cohort_input = {
        "n": cohort.count,
        "p25": cohort.p25_uf,
        "p40": None,
        "p50": cohort.median_uf,
        "p60": cohort.p60_uf,
        "p75": cohort.p75_uf,
        "p90": cohort.p90_uf,
        "ecdf": cohort.market_ecdf,
        "deterministic": bool(cohort.cohort_fingerprint),
        "fingerprint": cohort.cohort_fingerprint,
        # Foundation has no independent robustness flag yet; do not infer one.
        "robustness_warning": False,
    } if cohort else None
    engine_decision = evaluate_engine_v1(
        operation=prop.get("operation"),
        current_price_uf=prop.get("price_uf"),
        cohort=cohort_input,
        commercial_response={
            "inquiries_30d": leads.get("property_previous_30d", 0),
            "inquiries_90d": leads.get("property_previous_90d", 0),
            "signal_30d": leads.get("demand_signal_30d", "ZERO_UNCERTAIN"),
            "signal_90d": leads.get("demand_signal_90d", "ZERO_UNCERTAIN"),
            "confidence_30d": leads.get("demand_confidence_30d", "unknown"),
            "confidence_90d": leads.get("demand_confidence_90d", "unknown"),
        },
        exposure={
            "state": _engine_exposure_state(prop["publications"]),
            "active_portals": active_portals,
        },
        observation={
            "window_quality_30d": leads.get("observation_window_30d", "UNKNOWN"),
            "window_quality_90d": leads.get("observation_window_90d", "UNKNOWN"),
            "source_coverage_30d": leads.get("demand_source_coverage_30d", "UNKNOWN"),
            "source_coverage_90d": leads.get("demand_source_coverage_90d", "UNKNOWN"),
        },
        context={
            "page_as_of": page_as_of,
            "last_price_change_at": last_price_change_at,
            "operation_ambiguity": selection_required,
            "geography_valid": bool(prop.get("canonical_region") and prop.get("commune")),
            "critical_conflict": bool(prop.get("operation_conflict")),
        },
    )
    engine_v1 = OwnerPortalEngineV1Contract(
        status=engine_decision.status,
        recommendation=engine_decision.recommendation,
        eligibility=engine_decision.eligibility,
        confidence=engine_decision.confidence,
        market_position=engine_decision.market_position,
        gap_to_p75_pct=engine_decision.gap_to_p75_pct,
        gradual_price_uf=engine_decision.gradual_price_uf,
        competitive_reference_uf=engine_decision.competitive_reference_uf,
        owner_action=engine_decision.owner_action,
        reasons=engine_decision.reasons,
        warnings=engine_decision.warnings,
        methodology_version=engine_decision.methodology_version,
    )
    return OwnerPortalPropertyViewV1(
        property_code=code,
        property_type=prop["property_type"],
        operation=prop["operation"],
        operations=prop["operations"],
        operation_selection_required=selection_required,
        commune=prop["commune"],
        region=prop["region"],
        canonical_region=prop["canonical_region"],
        macrozone=prop["macrozone"],
        main_image_url=image_urls[0] if image_urls else None,
        current_price_uf=current_price.uf,
        current_price_clp=current_price.clp,
        bedrooms=prop["bedrooms"],
        bathrooms=prop["bathrooms"],
        parking=prop["parking"],
        built_area_m2=prop["built_area_m2"],
        land_area_m2=prop["land_area_m2"],
        inquiries_previous_7d=leads["property_previous_7d"],
        inquiries_previous_30d=leads["property_previous_30d"],
        inquiries_previous_90d=leads.get("property_previous_90d", 0),
        activity_series=activity_series,
        timeline=timeline,
        publications=prop["publications"],
        comparable_count=selected_market["comparables_count"],
        market_median_uf=selected_market["median_price_uf"] if market_available else None,
        market_low_uf=selected_market["range_price_uf"][0] if market_available else None,
        market_high_uf=selected_market["range_price_uf"][1] if market_available else None,
        market_uf_m2=selected_market["median_uf_m2"] if market_available else None,
        market_data_available=market_available,
        market_as_of=market.get("market_as_of"),
        current_price=current_price,
        previous_price=previous_price,
        last_price_change_at=last_price_change_at,
        as_of=page_as_of,
        data_updated_at=_format_date(cutoff) or "",
        property_updated_at=prop.get("updated_at_label"),
        data_quality=data_quality,
        recommendation=None,
        national_indicators=national_indicators,
        national_context_note=_national_context_note(national_indicators),
        regional_context_note=_regional_context_note(prop),
        local_context=local_context,
        positioning=positioning,
        comparable_cohort=cohort,
        market_intelligence_snapshot=market_intelligence_snapshot,
        demand_signal_30d=leads.get("demand_signal_30d", "ZERO_UNCERTAIN"),
        demand_signal_90d=leads.get("demand_signal_90d", "ZERO_UNCERTAIN"),
        demand_confidence_30d=leads.get("demand_confidence_30d", "unknown"),
        demand_confidence_90d=leads.get("demand_confidence_90d", "unknown"),
        page_as_of=page_as_of,
        market_position_state=market_position_state,
        market_ecdf=market_ecdf,
        market_ecdf_equal_or_below=market_ecdf_equal_or_below,
        market_position_owner_text=market_position_owner_text,
        engine_v1=engine_v1,
        provenance=provenance,
    )


def build_owner_portal_view(
    db: Any,
    property_code: str,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Compatibility adapter for legacy internal callers; the router uses the DTO directly."""

    dto = get_owner_portal_property_view(db, property_code, _as_of_or_now(as_of))
    if dto is None:
        return {"status": "not_found", "office_scope": OFFICE_SCOPE, "property_code": str(property_code)}
    payload = dto.to_dict()
    return {
        "status": "ok",
        "office_scope": OFFICE_SCOPE,
        "property_code": dto.property_code,
        "machine_learning_status": "NOT_EXECUTED",
        "property": {
            "code": dto.property_code,
            "operation": dto.operation,
            "property_type": dto.property_type,
            "region": dto.region,
            "commune": dto.commune,
            "bedrooms": dto.bedrooms,
            "bathrooms": dto.bathrooms,
            "parking": dto.parking,
            "built_area_m2": dto.built_area_m2,
            "land_area_m2": dto.land_area_m2,
            "price_uf": dto.current_price_uf,
            "price_clp": dto.current_price_clp,
            "updated_at_label": dto.data_updated_at,
            "image_urls": [dto.main_image_url] if dto.main_image_url else [],
        },
        "has_photo": dto.main_image_url is not None,
        "snapshot": {
            "available": dto.previous_price is not None,
            "price_history": (
                [{"date": dto.last_price_change_at, "previous_uf": dto.previous_price.uf, "current_uf": dto.current_price_uf}]
                if dto.previous_price and dto.last_price_change_at
                else []
            ),
        },
        "lead_metrics": {
            "previous_7d": dto.inquiries_previous_7d,
            "previous_30d": dto.inquiries_previous_30d,
            "property_previous_7d": dto.inquiries_previous_7d,
            "property_previous_30d": dto.inquiries_previous_30d,
            "total_linked_sucre": None,
            "excluded_other_office_linked": None,
        },
        "market": {
            "comparables_count": dto.comparable_count,
            "median_price_uf": dto.market_median_uf,
            "median_uf_m2": dto.market_uf_m2,
            "range_price_uf": [dto.market_low_uf, dto.market_high_uf],
            "minimum_comparables": 5,
            "market_as_of": dto.market_as_of,
            "explanation": "Se requieren al menos 5 comparables válidos para mostrar una mediana o rango representativo.",
            "source": "mercado_comunal + propiedades_captacion",
        },
        "visits": {"status": "NOT_AVAILABLE_V1", "reason": "V1 no verifica asistencia."},
        "recommendation": {"status": "PLACEHOLDER", "message": "Recomendación no calculada en V1."},
        "future_action": {"status": "PLACEHOLDER", "message": "La autorización no está habilitada en V1."},
        "dto_payload": payload,
    }
