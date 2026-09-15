"""Build current operational property snapshots without historical backfill."""

from __future__ import annotations

import json
import math
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .models import (
    DataQuality,
    LeadLinkageRecord,
    LinkageStatus,
    PriceChange,
    PropertyDailySnapshotV1,
    PublicationState,
)
from .property_identity import MASTER_PUBLICATION_PORTALS, normalize_identifier
from .time_utils import (
    BUSINESS_TZ,
    HistoricalSnapshotNotSupported,
    ensure_aware,
    is_in_previous_window,
    is_observable_before,
    parse_aware_datetime,
    to_business_time,
    to_utc,
    validate_operational_as_of,
)


SNAPSHOT_SCHEMA_VERSION = "PropertyDailySnapshotV1"
BUILDER_VERSION = "pricing-intelligence-foundation-v1"


@dataclass(frozen=True)
class SnapshotBuildResult:
    snapshots: tuple[PropertyDailySnapshotV1, ...]
    errors: tuple[str, ...]


def _path_value(document: Mapping[str, Any], path: str) -> Any:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _text(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    value = unicodedata.normalize("NFKC", str(value)).strip()
    return value or None


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        return number if math.isfinite(number) else None
    if not isinstance(value, str):
        return None
    text = value.strip().replace("\u00a0", " ").replace(" ", "")
    if not text:
        return None
    # Source numeric fields are occasionally serialized as strings.  Support
    # unambiguous decimal forms without extracting numbers from free text.
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        number = float(Decimal(text))
    except (InvalidOperation, ValueError):
        return None
    return number if math.isfinite(number) else None


def _boolean(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "si", "sí", "publicada", "activo", "activa"}:
            return True
        if normalized in {"false", "0", "no", "no_publicada", "inactivo", "inactiva"}:
            return False
    return None


def _operation_key(operation: Optional[str]) -> str:
    value = (operation or "").casefold()
    value = "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    )
    return value


def _current_prices(document: Mapping[str, Any]) -> tuple[Optional[float], Optional[float]]:
    operation = _operation_key(_text(_path_value(document, "tipo_operacion.tipo")))
    if "arriend" in operation:
        price_block = _path_value(document, "tipo_operacion.precio_arriendo") or {}
    else:
        price_block = _path_value(document, "tipo_operacion.precio_venta") or {}
    if not isinstance(price_block, Mapping):
        return None, None
    return _number(price_block.get("precio_uf")), _number(price_block.get("precio_clp"))


def _publication_states(document: Mapping[str, Any]) -> tuple[PublicationState, ...]:
    publications = document.get("publicaciones")
    if not isinstance(publications, Mapping):
        return ()
    output: list[PublicationState] = []
    for source, portal_key in MASTER_PUBLICATION_PORTALS:
        portal = publications.get(portal_key)
        if not isinstance(portal, Mapping):
            continue
        operation_records = portal.get("publicaciones")
        if isinstance(operation_records, Mapping):
            for operation, record in sorted(operation_records.items(), key=lambda item: str(item[0])):
                if not isinstance(record, Mapping):
                    continue
                identifier = normalize_identifier(record.get("code")) or normalize_identifier(record.get("code_unique"))
                active = _boolean(record.get("publicada"))
                status = _text(record.get("estado"))
                if identifier or active is not None or status:
                    output.append(
                        PublicationState(
                            portal=source,
                            operation=_text(operation),
                            active=active,
                            status=status,
                            listing_identifier=identifier,
                        )
                    )
        if portal_key == "chilepropiedades":
            for operation, field_name in (("Venta", "codigo_venta"), ("Arriendo", "codigo_arriendo")):
                identifier = normalize_identifier(portal.get(field_name))
                if identifier and not any(
                    item.portal == source and item.operation == operation for item in output
                ):
                    output.append(
                        PublicationState(
                            portal=source,
                            operation=operation,
                            active=None,
                            status=None,
                            listing_identifier=identifier,
                        )
                    )
    return tuple(output)


def _unit_from_field(field: str, before: Any, after: Any) -> Optional[str]:
    normalized = field.casefold()
    if "precio_uf" in normalized:
        return "uf"
    if "precio_clp" in normalized:
        return "clp"
    if "precio_publicado" not in normalized:
        return None
    for value in (before, after):
        decoded = _decode_value(value)
        if isinstance(decoded, Mapping):
            currency = _text(decoded.get("moneda") or decoded.get("currency"))
            if currency and currency.casefold() in {"uf", "unidad de fomento"}:
                return "uf"
            if currency and currency.casefold() in {"clp", "peso", "pesos", "clp$"}:
                return "clp"
    return None


def _decode_value(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def _price_value(value: Any, unit: str) -> Optional[float]:
    value = _decode_value(value)
    if isinstance(value, Mapping):
        currency = _text(value.get("moneda") or value.get("currency"))
        if currency:
            currency_key = currency.casefold()
            if unit == "uf" and currency_key not in {"uf", "unidad de fomento"}:
                return None
            if unit == "clp" and currency_key not in {"clp", "peso", "pesos", "clp$"}:
                return None
        return _number(value.get("monto")) or _number(value.get("valor"))
    return _number(value)


def _event_price_change(event: Mapping[str, Any], source: str) -> Optional[PriceChange]:
    field = _text(event.get("campo")) or ""
    unit = _unit_from_field(field, event.get("valor_anterior"), event.get("valor_nuevo"))
    if not unit:
        return None
    try:
        changed_at = parse_aware_datetime(event.get("fecha"), field_name="price_change.fecha")
    except (TypeError, ValueError):
        return None
    previous = _price_value(event.get("valor_anterior"), unit)
    new = _price_value(event.get("valor_nuevo"), unit)
    if previous is None or new is None:
        return None
    return PriceChange(
        changed_at=changed_at,
        unit=unit,
        previous_value=previous,
        new_value=new,
        source=source,
    )


def get_latest_observable_price_change(
    property_code: str,
    as_of: datetime,
    *,
    embedded_events: Iterable[Mapping[str, Any]] = (),
    separate_events: Iterable[Mapping[str, Any]] = (),
) -> Optional[PriceChange]:
    """Return the latest verifiable price change strictly before ``as_of``.

    Duplicate events are deduplicated using timestamp, unit, previous value,
    and new value.  The separate history collection wins deterministically
    over the embedded copy when all those fields agree.
    """

    as_of = ensure_aware(as_of, field_name="as_of")
    normalized_code = normalize_identifier(property_code)
    if not normalized_code:
        return None

    candidates: list[PriceChange] = []
    for event in embedded_events:
        if isinstance(event, Mapping):
            candidate = _event_price_change(event, "embedded_historial_cambios")
            if candidate and is_observable_before(candidate.changed_at, as_of):
                candidates.append(candidate)
    for event in separate_events:
        if not isinstance(event, Mapping):
            continue
        event_code = normalize_identifier(event.get("codigo"))
        if event_code and event_code != normalized_code:
            continue
        candidate = _event_price_change(event, "universo_cartera_prop360_historial")
        if candidate and is_observable_before(candidate.changed_at, as_of):
            candidates.append(candidate)

    deduped: dict[tuple[datetime, str, float, float], PriceChange] = {}
    source_priority = {
        "universo_cartera_prop360_historial": 0,
        "embedded_historial_cambios": 1,
    }
    for candidate in candidates:
        key = (to_utc(candidate.changed_at), candidate.unit, candidate.previous_value, candidate.new_value)
        current = deduped.get(key)
        if current is None or source_priority[candidate.source] < source_priority[current.source]:
            deduped[key] = candidate

    if not deduped:
        return None
    return max(
        deduped.values(),
        key=lambda item: (to_utc(item.changed_at), -source_priority[item.source]),
    )


class PropertySnapshotBuilder:
    """Build only the current local-date operational snapshot."""

    def __init__(
        self,
        linkage_records: Iterable[LeadLinkageRecord] = (),
        *,
        source_collection: str = "universo_cartera_prop360",
        builder_version: str = BUILDER_VERSION,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.linkage_records = tuple(linkage_records)
        self.source_collection = source_collection
        self.builder_version = builder_version
        self.now_fn = now_fn or (lambda: datetime.now(BUSINESS_TZ))

    def _validate_as_of(self, as_of: Optional[datetime]) -> datetime:
        now = ensure_aware(self.now_fn(), field_name="now")
        requested = now if as_of is None else ensure_aware(as_of, field_name="as_of")
        return validate_operational_as_of(requested, now=now)

    def build(
        self,
        properties: Iterable[Mapping[str, Any]],
        *,
        as_of: Optional[datetime] = None,
        separate_price_events: Iterable[Mapping[str, Any]] = (),
        property_code: Optional[str] = None,
    ) -> SnapshotBuildResult:
        cutoff = self._validate_as_of(as_of)
        cutoff_local = to_business_time(cutoff)
        cutoff_utc = to_utc(cutoff)
        requested_code = normalize_identifier(property_code) if property_code else None

        events_by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for event in separate_price_events:
            if not isinstance(event, Mapping):
                continue
            code = normalize_identifier(event.get("codigo"))
            if code:
                events_by_code[code].append(event)

        linked_by_code: dict[str, list[LeadLinkageRecord]] = defaultdict(list)
        for record in self.linkage_records:
            if record.status in {LinkageStatus.EXACT_CANONICAL, LinkageStatus.EXACT_ALIAS} and record.property_code:
                linked_by_code[record.property_code].append(record)

        snapshots: list[PropertyDailySnapshotV1] = []
        errors: list[str] = []
        seen_codes: set[str] = set()
        found_requested = requested_code is None

        for document in properties:
            raw_code = document.get("codigo") if isinstance(document, Mapping) else None
            code = normalize_identifier(raw_code)
            if not code:
                errors.append("property_without_canonical_code")
                continue
            if requested_code and code != requested_code:
                continue
            found_requested = True
            if code in seen_codes:
                errors.append("duplicate_property_code_in_input")
                continue
            seen_codes.add(code)

            embedded_events = document.get("historial_cambios") if isinstance(document, Mapping) else ()
            if not isinstance(embedded_events, Sequence) or isinstance(embedded_events, (str, bytes)):
                embedded_events = ()
            price_change = get_latest_observable_price_change(
                code,
                cutoff,
                embedded_events=embedded_events,
                separate_events=events_by_code.get(code, ()),
            )

            operation = _text(_path_value(document, "tipo_operacion.tipo"))
            property_type = _text(_path_value(document, "metadata.tipo_propiedad")) or _text(
                _path_value(document, "metadata.tipo_propiedad_detectado")
            )
            current_price_uf, current_price_clp = _current_prices(document)
            bedrooms = _number(_path_value(document, "caracteristicas.dormitorios"))
            bathrooms = _number(_path_value(document, "caracteristicas.banos"))
            parking = _number(_path_value(document, "caracteristicas.estacionamientos"))
            built_area = _number(_path_value(document, "caracteristicas.superficie_construida"))
            land_area = _number(_path_value(document, "caracteristicas.superficie_terreno"))
            property_links = linked_by_code.get(code, [])
            leads_7d = sum(
                record.created_at is not None and is_in_previous_window(record.created_at, cutoff, 7)
                for record in property_links
            )
            leads_30d = sum(
                record.created_at is not None and is_in_previous_window(record.created_at, cutoff, 30)
                for record in property_links
            )

            previous_uf = price_change.previous_value if price_change and price_change.unit == "uf" else None
            previous_clp = price_change.previous_value if price_change and price_change.unit == "clp" else None
            publications = _publication_states(document)
            quality = DataQuality(
                missing_price=current_price_uf is None and current_price_clp is None,
                missing_surface=built_area is None and land_area is None,
                missing_bedrooms=bedrooms is None,
                missing_bathrooms=bathrooms is None,
                lead_linkage_available=bool(property_links),
                price_history_available=price_change is not None,
                publication_data_available=bool(publications),
            )
            source_collections = [self.source_collection]
            if events_by_code.get(code):
                source_collections.append("universo_cartera_prop360_historial")

            snapshots.append(
                PropertyDailySnapshotV1(
                    schema_version=SNAPSHOT_SCHEMA_VERSION,
                    property_code=code,
                    snapshot_date_local=cutoff_local.date(),
                    as_of_local=cutoff_local,
                    as_of_utc=cutoff_utc,
                    operation=operation,
                    property_type=property_type,
                    region=_text(_path_value(document, "ubicacion.region")),
                    commune=_text(_path_value(document, "ubicacion.comuna")),
                    sector=_text(_path_value(document, "ubicacion.sector")),
                    bedrooms=bedrooms,
                    bathrooms=bathrooms,
                    parking=parking,
                    built_area_m2=built_area,
                    land_area_m2=land_area,
                    current_price_uf=current_price_uf,
                    current_price_clp=current_price_clp,
                    last_price_change_at=price_change.changed_at if price_change else None,
                    previous_price_uf=previous_uf,
                    previous_price_clp=previous_clp,
                    publications=publications,
                    linked_leads_previous_7d=leads_7d,
                    linked_leads_previous_30d=leads_30d,
                    data_quality=quality,
                    provenance={
                        "source_collections": sorted(source_collections),
                        "schema_version": SNAPSHOT_SCHEMA_VERSION,
                        "builder_version": self.builder_version,
                        "as_of_rule": "event_time < as_of",
                        "external_listing_views": "NOT_AVAILABLE_V1",
                    },
                    listed_at=None,
                    days_published=None,
                )
            )

        if requested_code and not found_requested:
            errors.append("requested_property_not_found")
        return SnapshotBuildResult(snapshots=tuple(snapshots), errors=tuple(errors))
