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
from analytics.pricing_intelligence.time_utils import is_in_previous_window, parse_aware_datetime

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
    return {"uf": _number(block.get("precio_uf")), "clp": _number(block.get("precio_clp"))}


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


def _find_master(db: Any, code: str | None = None) -> list[dict[str, Any]]:
    query = {SUCRE_OFFICE_FIELD: SUCRE_OFFICE_VALUE}
    if code is not None:
        query["codigo"] = str(code)
    return list(db[MASTER_COLLECTION].find(query, MASTER_PROJECTION))


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

    documents = _find_master(db)
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
        "image_urls": image_urls[:8],
    }


def _parse_date(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_aware_datetime(value, field_name="publication_date")
    except (TypeError, ValueError):
        return None


def _market_context(db: Any, prop: Mapping[str, Any]) -> dict[str, Any]:
    commune, property_type = prop["commune"], prop["property_type"]
    aggregate = db[MARKET_COLLECTION].find_one(
        {"comuna": commune, "tipo_propiedad": property_type},
        {"_id": 0, "mercado_venta": 1, "rangos_precio_venta": 1, "indicadores_mercado": 1},
    ) or {}
    return _comparables(db, prop, aggregate)


def _comparables(db: Any, prop: Mapping[str, Any], aggregate: Mapping[str, Any]) -> dict[str, Any]:
    query = {"comuna": prop["commune"], "tipo_propiedad": prop["property_type"], "operacion": "venta"}
    rows = list(db[CAPTACION_COLLECTION].find(query, {"_id": 0, "precio_uf": 1, "precio_clp": 1, "superficie": 1, "superficie_construida": 1, "fecha_publicacion": 1, "updated_at": 1}))
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)
    valid: list[tuple[float, float]] = []
    for row in rows:
        price = _number(row.get("precio_uf"))
        if price is None:
            price_clp = _number(row.get("precio_clp"))
            price = price_clp  # CLP is retained only for validity; no conversion is invented.
        surface = _number(row.get("superficie") or row.get("superficie_construida"))
        when = _parse_date(row.get("fecha_publicacion") or row.get("updated_at"))
        if price is None or surface is None or surface <= 0:
            continue
        if when is not None and when < cutoff:
            continue
        valid.append((price, surface))
    market_sale = aggregate.get("mercado_venta") if isinstance(aggregate.get("mercado_venta"), Mapping) else {}
    indicators = aggregate.get("indicadores_mercado") if isinstance(aggregate.get("indicadores_mercado"), Mapping) else {}
    ranges = aggregate.get("rangos_precio_venta") if isinstance(aggregate.get("rangos_precio_venta"), Mapping) else {}
    if len(valid) >= 5:
        prices = [item[0] for item in valid]
        uf_m2 = [price / surface for price, surface in valid]
        return {
            "source": "propiedades_captacion + mercado_comunal",
            "comparables_count": len(valid),
            "median_price_uf": round(statistics.median(prices), 2),
            "median_uf_m2": round(statistics.median(uf_m2), 2),
            "range_price_uf": [round(min(prices), 2), round(max(prices), 2)],
            "aggregate": dict(market_sale),
            "indicators": dict(indicators),
            "range_reference": dict(ranges),
            "explanation": "Comparables descriptivos: misma comuna, tipo y operación; precio y superficie válidos; fecha reciente cuando está disponible.",
        }
    return {
        "source": "mercado_comunal",
        "comparables_count": len(valid),
        "median_price_uf": None,
        "median_uf_m2": _number(market_sale.get("uf_m2_publicacion_actual")),
        "range_price_uf": [ranges.get("min_uf"), ranges.get("max_uf")] if ranges else [None, None],
        "aggregate": dict(market_sale),
        "indicators": dict(indicators),
        "range_reference": dict(ranges),
        "explanation": "Mediana de comparables no disponible en V1; se muestra contexto agregado de mercado comunal.",
    }


def _snapshot(db: Any, code: str) -> dict[str, Any] | None:
    return db[SNAPSHOT_COLLECTION].find_one({"property_code": code}, {"_id": 0}, sort=[("snapshot_date_local", -1)])


def _lead_metrics(db: Any, sucre_codes: set[str]) -> dict[str, Any]:
    properties = list(db[MASTER_COLLECTION].find({}, MASTER_PROJECTION))
    resolver = build_property_identity_resolver(properties, enable_contextual_aliases=True)
    service = LeadLinkageService(resolver)
    lead_projection = {
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
    leads = list(db["leads"].find({}, lead_projection))
    records = service.link_leads(leads)
    linked = [record for record in records if record.status in {LinkageStatus.EXACT_CANONICAL, LinkageStatus.EXACT_ALIAS}]
    sucre = [record for record in linked if record.property_code in sucre_codes]
    now = datetime.now(timezone.utc)
    return {
        "total_linked_sucre": len(sucre),
        "distinct_properties": len({record.property_code for record in sucre}),
        "previous_7d": sum(record.created_at is not None and is_in_previous_window(record.created_at, now, 7) for record in sucre),
        "previous_30d": sum(record.created_at is not None and is_in_previous_window(record.created_at, now, 30) for record in sucre),
        "exact_canonical": sum(record.status is LinkageStatus.EXACT_CANONICAL for record in sucre),
        "exact_alias": sum(record.status is LinkageStatus.EXACT_ALIAS for record in sucre),
        "excluded_other_office_linked": sum(record.property_code is not None and record.property_code not in sucre_codes for record in linked),
        "global_quality": service.metrics(records),
    }


def _safe_snapshot(snapshot: Mapping[str, Any] | None, prop: Mapping[str, Any]) -> dict[str, Any]:
    if not snapshot:
        return {
            "available": False,
            "reason": "No existe snapshot V1 para esta propiedad.",
            "leads_7d": 0,
            "leads_30d": 0,
            "price_history": [],
        }
    history: list[dict[str, Any]] = []
    changed = snapshot.get("last_price_change_at")
    previous_uf = _number(snapshot.get("previous_price_uf"))
    current_uf = _number(snapshot.get("current_price_uf"))
    if changed and previous_uf is not None and current_uf is not None:
        history.append({"date": str(changed)[:10], "previous_uf": previous_uf, "current_uf": current_uf})
    return {
        "available": True,
        "snapshot_date": snapshot.get("snapshot_date_local"),
        "leads_7d": int(snapshot.get("linked_leads_previous_7d") or 0),
        "leads_30d": int(snapshot.get("linked_leads_previous_30d") or 0),
        "price_history": history,
    }


def build_owner_portal_view(db: Any, property_code: str) -> dict[str, Any]:
    """Build a single PII-free view. No writes, models, or recommendation actions."""

    docs = _find_master(db, str(property_code))
    if not docs or not is_procasa_sucre_property(docs[0]):
        return {"status": "not_found", "office_scope": OFFICE_SCOPE, "property_code": str(property_code)}
    doc = docs[0]
    if not is_active_property(doc):
        return {"status": "not_found", "office_scope": OFFICE_SCOPE, "property_code": str(property_code)}
    captures = _captacion_by_ids(db, _publication_listing_ids(doc))
    image_urls: list[str] = []
    for capture in captures:
        for image in _image_urls(capture):
            if image not in image_urls:
                image_urls.append(image)
    prop = _safe_property(doc, image_urls)
    code = prop["code"] or str(property_code)
    master_sucre = _find_master(db)
    sucre_codes = {str(item.get("codigo")) for item in master_sucre if item.get("codigo") is not None}
    snapshot = _snapshot(db, code)
    leads = _lead_metrics(db, sucre_codes)
    # History is intentionally unavailable with only the current daily snapshot.
    lead_history = {"available": False, "reason": "Histórico insuficiente para una evolución confiable en V1.", "points": []}
    visits = {"status": "NOT_AVAILABLE_V1", "reason": "Los registros actuales prueban intención, autorización o firma; no asistencia completada."}
    return {
        "status": "ok",
        "office_scope": OFFICE_SCOPE,
        "property_code": code,
        "machine_learning_status": "NOT_EXECUTED",
        "property": prop,
        "has_photo": bool(image_urls),
        "snapshot": _safe_snapshot(snapshot, prop),
        "lead_metrics": leads,
        "lead_history": lead_history,
        "market": _market_context(db, prop),
        "visits": visits,
        "recommendation": {"status": "PLACEHOLDER", "message": "Recomendación de precio disponible en una fase futura; no se calcula ni se autoriza aquí."},
        "future_action": {"status": "PLACEHOLDER", "message": "La autorización o modificación de precio no está habilitada en este prototipo."},
    }
