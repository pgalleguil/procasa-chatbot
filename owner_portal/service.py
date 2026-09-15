"""Read-only, explainable data service for the PROCASA SUCRE prototype."""

from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from analytics.pricing_intelligence.lead_linkage import LeadLinkageService
from analytics.pricing_intelligence.models import LinkageStatus
from analytics.pricing_intelligence.property_identity import (
    build_property_identity_resolver,
    normalize_identifier,
)
from analytics.pricing_intelligence.time_utils import BUSINESS_TZ, is_in_previous_window, parse_aware_datetime

from .schemas import (
    OwnerPortalDataQualityV1,
    OwnerPortalPriceV1,
    OwnerPortalPropertyViewV1,
)

OFFICE_SCOPE = "PROCASA_SUCRE"
SUCRE_OFFICE_FIELD = "estado.oficina"
SUCRE_OFFICE_VALUE = "PROCASA SUCRE"
SUCRE_OFFICE_VALUES = frozenset({SUCRE_OFFICE_VALUE})
SNAPSHOT_COLLECTION = "pricing_intelligence_property_snapshots_v1"
MASTER_COLLECTION = "universo_cartera_prop360"
CAPTACION_COLLECTION = "propiedades_captacion"
MARKET_COLLECTION = "mercado_comunal"

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
    "prospecto.codigo_propiedad": 1,
    "prospecto.propiedad_codigo": 1,
    "prospecto.origen": 1,
    "prospecto.fuente_lead": 1,
    "prospecto.portal_origen": 1,
    "prospecto.origen_anuncio": 1,
    "prospecto.plataforma_origen": 1,
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


def _operation(doc: Mapping[str, Any]) -> str | None:
    return _text(_path(doc, "tipo_operacion.tipo"))


def _price(doc: Mapping[str, Any]) -> dict[str, float | None]:
    operation = (_operation(doc) or "").casefold()
    block = _path(doc, "tipo_operacion.precio_arriendo") if "arriend" in operation else _path(doc, "tipo_operacion.precio_venta")
    if not isinstance(block, Mapping):
        block = _path(doc, "tipo_operacion.precio_venta") or _path(doc, "tipo_operacion.precio_arriendo")
    block = block if isinstance(block, Mapping) else {}
    uf = _number(block.get("precio_uf"))
    clp = _number(block.get("precio_clp"))
    return {
        "uf": uf if uf is not None and uf > 0 else None,
        "clp": clp if clp is not None and clp > 0 else None,
    }


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

    values: dict[str, set[str]] = {
        "mercadolibre": set(),
        "yapo": set(),
        "toctoc": set(),
    }
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
                    normalized = normalize_identifier(record.get(field_name))
                    if normalized and source in values:
                        values[source].add(normalized)
        if portal_key == "chilepropiedades":
            for field_name in ("codigo_venta", "codigo_arriendo"):
                normalized = normalize_identifier(portal.get(field_name))
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
    values = [
        _price(doc)["uf"] is not None or _price(doc)["clp"] is not None,
        image_count > 0,
        _path(doc, "ubicacion.comuna") is not None,
        _path(doc, "caracteristicas.dormitorios") is not None,
        _path(doc, "caracteristicas.banos") is not None,
        _path(doc, "caracteristicas.superficie_construida") is not None or _path(doc, "caracteristicas.superficie_terreno") is not None,
    ]
    return sum(values), str(doc.get("codigo") or "")


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


def _safe_property(doc: Mapping[str, Any], image_urls: list[str]) -> dict[str, Any]:
    price = _price(doc)
    return {
        "code": _text(doc.get("codigo")),
        "operation": _operation(doc) or "No disponible",
        "property_type": _text(_path(doc, "metadata.tipo_propiedad")) or _operation(doc) or "No disponible",
        "region": _text(_path(doc, "ubicacion.region")) or "No disponible",
        "commune": _text(_path(doc, "ubicacion.comuna")) or "No disponible",
        "sector": _text(_path(doc, "ubicacion.sector")) or "No disponible",
        "bedrooms": _number(_path(doc, "caracteristicas.dormitorios")),
        "bathrooms": _number(_path(doc, "caracteristicas.banos")),
        "parking": _number(_path(doc, "caracteristicas.estacionamientos")),
        "built_area_m2": _number(_path(doc, "caracteristicas.superficie_construida")),
        "land_area_m2": _number(_path(doc, "caracteristicas.superficie_terreno")),
        "price_uf": price["uf"],
        "price_clp": price["clp"],
        "updated_at_label": _format_date(_path(doc, "estado.ultima_actualizacion")),
        "image_urls": image_urls[:8],
    }


def _parse_date(value: Any) -> datetime | None:
    if value is None:
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
    parsed = _parse_date(value)
    if parsed is None:
        return None
    return parsed.astimezone(BUSINESS_TZ).strftime("%d/%m/%Y")


def _format_as_of(as_of: datetime) -> str:
    return as_of.astimezone(timezone.utc).isoformat()


def _as_of_or_now(as_of: datetime | None) -> datetime:
    value = as_of if as_of is not None else datetime.now(BUSINESS_TZ) + timedelta(microseconds=1)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return value


def _market_context(db: Any, prop: Mapping[str, Any], as_of: datetime | None = None) -> dict[str, Any]:
    as_of = _as_of_or_now(as_of)
    commune, property_type = prop["commune"], prop["property_type"]
    aggregate = db[MARKET_COLLECTION].find_one(
        {"comuna": commune, "tipo_propiedad": property_type},
        {
            "_id": 0,
            "mercado_venta": 1,
            "rangos_precio_venta": 1,
            "indicadores_mercado": 1,
            "updated_at": 1,
            "fecha_actualizacion": 1,
            "as_of": 1,
            "fecha_corte": 1,
        },
    ) or {}
    return _comparables(db, prop, aggregate, as_of)


def _comparables(db: Any, prop: Mapping[str, Any], aggregate: Mapping[str, Any], as_of: datetime) -> dict[str, Any]:
    query = {"comuna": prop["commune"], "tipo_propiedad": prop["property_type"], "operacion": "venta"}
    rows = list(
        db[CAPTACION_COLLECTION].find(
            query,
            {
                "_id": 0,
                "precio_uf": 1,
                "superficie": 1,
                "superficie_construida": 1,
                "fecha_publicacion": 1,
                "updated_at": 1,
            },
        )
    )
    cutoff = as_of.astimezone(timezone.utc) - timedelta(days=365)
    as_of_utc = as_of.astimezone(timezone.utc)
    valid: list[tuple[float, float]] = []
    for row in rows:
        price = _number(row.get("precio_uf"))
        surface = _number(row.get("superficie") or row.get("superficie_construida"))
        when = _parse_date(row.get("fecha_publicacion") or row.get("updated_at"))
        if price is None or price <= 0 or surface is None or surface <= 0:
            continue
        if when is not None and (when < cutoff or when >= as_of_utc):
            continue
        valid.append((price, surface))
    market_sale = aggregate.get("mercado_venta") if isinstance(aggregate.get("mercado_venta"), Mapping) else {}
    indicators = aggregate.get("indicadores_mercado") if isinstance(aggregate.get("indicadores_mercado"), Mapping) else {}
    ranges = aggregate.get("rangos_precio_venta") if isinstance(aggregate.get("rangos_precio_venta"), Mapping) else {}
    market_as_of = _format_date(
        aggregate.get("as_of")
        or aggregate.get("fecha_corte")
        or aggregate.get("fecha_actualizacion")
        or aggregate.get("updated_at")
    )
    if len(valid) >= 5:
        prices = [item[0] for item in valid]
        uf_m2 = [price / surface for price, surface in valid]
        return {
            "minimum_comparables": 5,
            "source": "propiedades_captacion + mercado_comunal",
            "comparables_count": len(valid),
            "median_price_uf": round(statistics.median(prices), 2),
            "median_uf_m2": round(statistics.median(uf_m2), 2),
            "range_price_uf": [round(min(prices), 2), round(max(prices), 2)],
            "aggregate": dict(market_sale),
            "indicators": dict(indicators),
            "range_reference": dict(ranges),
            "market_as_of": market_as_of,
            "explanation": "Comparables descriptivos: misma comuna, tipo y operación; precio y superficie válidos; fecha reciente cuando está disponible.",
        }
    return {
        "minimum_comparables": 5,
        "source": "mercado_comunal",
        "comparables_count": len(valid),
        "median_price_uf": None,
        "median_uf_m2": None,
        "range_price_uf": [None, None],
        "aggregate": dict(market_sale),
        "indicators": dict(indicators),
        "range_reference": dict(ranges),
        "market_as_of": market_as_of,
        "explanation": "Se requieren al menos 5 comparables válidos para mostrar una mediana o rango representativo.",
    }


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
    linked = [
        record
        for record in records
        if record.status in {LinkageStatus.EXACT_CANONICAL, LinkageStatus.EXACT_ALIAS}
        and record.property_code == property_code
    ]
    cutoff = as_of.astimezone(timezone.utc)
    return {
        "total_linked_sucre": len(linked),
        "distinct_properties": 1 if linked else 0,
        "previous_7d": sum(record.created_at is not None and is_in_previous_window(record.created_at, cutoff, 7) for record in linked),
        "previous_30d": sum(record.created_at is not None and is_in_previous_window(record.created_at, cutoff, 30) for record in linked),
        "property_previous_7d": sum(record.created_at is not None and is_in_previous_window(record.created_at, cutoff, 7) for record in linked),
        "property_previous_30d": sum(record.created_at is not None and is_in_previous_window(record.created_at, cutoff, 30) for record in linked),
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
            "exact_canonical": 0,
            "exact_alias": 0,
            "excluded_other_office_linked": 0,
            "global_quality": {},
        }
    return _lead_metrics_for_property(db, docs[0], as_of)


def get_owner_portal_property_view(
    db: Any,
    property_code: str,
    as_of: datetime,
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

    prop = _safe_property(doc, image_urls)
    code = prop["code"] or str(property_code)
    snapshot = _snapshot(db, code, cutoff)
    leads = _lead_metrics_for_property(db, doc, cutoff)
    market = _market_context(db, prop, cutoff)

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
    market_available = bool(market.get("comparables_count", 0) >= 5)
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
    return OwnerPortalPropertyViewV1(
        property_code=code,
        property_type=prop["property_type"],
        operation=prop["operation"],
        commune=prop["commune"],
        region=prop["region"],
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
        comparable_count=market["comparables_count"],
        market_median_uf=market["median_price_uf"] if market_available else None,
        market_low_uf=market["range_price_uf"][0] if market_available else None,
        market_high_uf=market["range_price_uf"][1] if market_available else None,
        market_uf_m2=market["median_uf_m2"] if market_available else None,
        market_data_available=market_available,
        market_as_of=market.get("market_as_of"),
        current_price=current_price,
        previous_price=previous_price,
        last_price_change_at=last_price_change_at,
        as_of=_format_as_of(cutoff),
        data_updated_at=_format_date(cutoff) or "Fecha no disponible",
        data_quality=data_quality,
        recommendation=None,
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
