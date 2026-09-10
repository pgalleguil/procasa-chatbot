"""Explicit, PII-free contracts for the PROCASA SUCRE owner portal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


OWNER_PORTAL_VIEW_ALLOWLIST = frozenset(
    {
        "property_code",
        "property_type",
        "operation",
        "commune",
        "region",
        "main_image_url",
        "current_price_uf",
        "current_price_clp",
        "bedrooms",
        "bathrooms",
        "parking",
        "built_area_m2",
        "land_area_m2",
        "inquiries_previous_7d",
        "inquiries_previous_30d",
        "comparable_count",
        "market_median_uf",
        "market_low_uf",
        "market_high_uf",
        "market_uf_m2",
        "market_data_available",
        "market_as_of",
        "current_price",
        "previous_price",
        "last_price_change_at",
        "as_of",
        "data_updated_at",
        "data_quality",
        "recommendation",
        "national_indicators",
        "national_context_note",
        "regional_context_note",
        "local_context",
        "positioning",
    }
)

OWNER_PORTAL_PRICE_ALLOWLIST = frozenset({"uf", "clp"})
OWNER_PORTAL_QUALITY_ALLOWLIST = frozenset(
    {
        "photo_available",
        "price_available",
        "surface_available",
        "bedrooms_available",
        "bathrooms_available",
        "parking_available",
        "market_data_available",
        "price_history_available",
        "lead_linkage_available",
    }
)


@dataclass(frozen=True)
class OwnerPortalPriceV1:
    uf: float | None
    clp: float | None

    def to_dict(self) -> dict[str, float | None]:
        return {"uf": self.uf, "clp": self.clp}


@dataclass(frozen=True)
class OwnerPortalDataQualityV1:
    photo_available: bool
    price_available: bool
    surface_available: bool
    bedrooms_available: bool
    bathrooms_available: bool
    parking_available: bool
    market_data_available: bool
    price_history_available: bool
    lead_linkage_available: bool

    def to_dict(self) -> dict[str, bool]:
        return {
            "photo_available": self.photo_available,
            "price_available": self.price_available,
            "surface_available": self.surface_available,
            "bedrooms_available": self.bedrooms_available,
            "bathrooms_available": self.bathrooms_available,
            "parking_available": self.parking_available,
            "market_data_available": self.market_data_available,
            "price_history_available": self.price_history_available,
            "lead_linkage_available": self.lead_linkage_available,
        }


@dataclass(frozen=True)
class MarketIndicatorV1:
    """A dated, source-backed market indicator safe for owner-facing use."""

    indicator_id: str
    scope: str
    geography: str
    value: float | str | None
    unit: str
    period: str
    source_name: str
    source_url: str | None
    retrieved_at: str
    valid_until: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "indicator_id": self.indicator_id,
            "scope": self.scope,
            "geography": self.geography,
            "value": self.value,
            "unit": self.unit,
            "period": self.period,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "valid_until": self.valid_until,
        }


@dataclass(frozen=True)
class OwnerPortalMarketContextV1:
    """Descriptive local snapshot; it is not a valuation or recommendation."""

    scope: str
    geography: str
    property_type: str
    available: bool
    comparables_count: int
    median_price_uf: float | None
    median_uf_m2: float | None
    public_uf_m2: float | None
    effective_uf_m2: float | None
    price_variation_12m_pct: float | None
    active_listings: int | None
    total_listings: int | None
    trend: str | None
    liquidity: str | None
    competition: str | None
    range_price_uf: tuple[float | None, float | None]
    source_name: str | None
    source_reference: str | None
    retrieved_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "geography": self.geography,
            "property_type": self.property_type,
            "available": self.available,
            "comparables_count": self.comparables_count,
            "median_price_uf": self.median_price_uf,
            "median_uf_m2": self.median_uf_m2,
            "public_uf_m2": self.public_uf_m2,
            "effective_uf_m2": self.effective_uf_m2,
            "price_variation_12m_pct": self.price_variation_12m_pct,
            "active_listings": self.active_listings,
            "total_listings": self.total_listings,
            "trend": self.trend,
            "liquidity": self.liquidity,
            "competition": self.competition,
            "range_price_uf": list(self.range_price_uf),
            "source_name": self.source_name,
            "source_reference": self.source_reference,
            "retrieved_at": self.retrieved_at,
        }


@dataclass(frozen=True)
class OwnerPortalPositioningV1:
    """A neutral position inside the observed comparable range."""

    price_uf: float
    low_uf: float
    high_uf: float
    marker_pct: float
    label: str
    comparable_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "price_uf": self.price_uf,
            "low_uf": self.low_uf,
            "high_uf": self.high_uf,
            "marker_pct": self.marker_pct,
            "label": self.label,
            "comparable_count": self.comparable_count,
        }


@dataclass(frozen=True)
class OwnerPortalPropertyViewV1:
    """The only data shape that the owner-facing template may receive."""

    property_code: str
    property_type: str
    operation: str
    commune: str
    region: str
    main_image_url: str | None
    current_price_uf: float | None
    current_price_clp: float | None
    bedrooms: float | None
    bathrooms: float | None
    parking: float | None
    built_area_m2: float | None
    land_area_m2: float | None
    inquiries_previous_7d: int
    inquiries_previous_30d: int
    comparable_count: int
    market_median_uf: float | None
    market_low_uf: float | None
    market_high_uf: float | None
    market_uf_m2: float | None
    market_data_available: bool
    market_as_of: str | None
    current_price: OwnerPortalPriceV1
    previous_price: OwnerPortalPriceV1 | None
    last_price_change_at: str | None
    as_of: str
    data_updated_at: str
    data_quality: OwnerPortalDataQualityV1
    recommendation: None = None
    national_indicators: tuple[MarketIndicatorV1, ...] = ()
    national_context_note: str | None = None
    regional_context_note: str | None = None
    local_context: OwnerPortalMarketContextV1 | None = None
    positioning: OwnerPortalPositioningV1 | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize only explicit allowlisted fields; never expose source docs."""

        return {
            "property_code": self.property_code,
            "property_type": self.property_type,
            "operation": self.operation,
            "commune": self.commune,
            "region": self.region,
            "main_image_url": self.main_image_url,
            "current_price_uf": self.current_price_uf,
            "current_price_clp": self.current_price_clp,
            "bedrooms": self.bedrooms,
            "bathrooms": self.bathrooms,
            "parking": self.parking,
            "built_area_m2": self.built_area_m2,
            "land_area_m2": self.land_area_m2,
            "inquiries_previous_7d": self.inquiries_previous_7d,
            "inquiries_previous_30d": self.inquiries_previous_30d,
            "comparable_count": self.comparable_count,
            "market_median_uf": self.market_median_uf,
            "market_low_uf": self.market_low_uf,
            "market_high_uf": self.market_high_uf,
            "market_uf_m2": self.market_uf_m2,
            "market_data_available": self.market_data_available,
            "market_as_of": self.market_as_of,
            "current_price": self.current_price.to_dict(),
            "previous_price": self.previous_price.to_dict() if self.previous_price else None,
            "last_price_change_at": self.last_price_change_at,
            "as_of": self.as_of,
            "data_updated_at": self.data_updated_at,
            "data_quality": self.data_quality.to_dict(),
            "recommendation": None,
            "national_indicators": [item.to_dict() for item in self.national_indicators],
            "national_context_note": self.national_context_note,
            "regional_context_note": self.regional_context_note,
            "local_context": self.local_context.to_dict() if self.local_context else None,
            "positioning": self.positioning.to_dict() if self.positioning else None,
        }


EVENT_SCHEMA_VERSION = "OwnerPortalEventV1"
OWNER_PORTAL_EVENT_NAMES = frozenset(
    {
        "portal_opened",
        "market_section_viewed",
        "price_history_viewed",
        "pricing_recommendation_viewed",
        "price_authorization_started",
        "price_authorized",
        "price_rejected",
        "contact_executive_clicked",
    }
)

EVENT_METADATA_ALLOWLIST: dict[str, frozenset[str]] = {
    "portal_opened": frozenset({"surface"}),
    "market_section_viewed": frozenset({"surface", "comparable_count"}),
    "price_history_viewed": frozenset({"surface", "history_available"}),
    "pricing_recommendation_viewed": frozenset({"surface", "recommendation_available"}),
    "price_authorization_started": frozenset({"surface"}),
    "price_authorized": frozenset({"surface"}),
    "price_rejected": frozenset({"surface", "reason_code"}),
    "contact_executive_clicked": frozenset({"surface", "channel"}),
}

_PII_KEY_PARTS = (
    "email",
    "phone",
    "telefono",
    "teléfono",
    "rut",
    "nombre",
    "mensaje",
    "direccion",
    "dirección",
    "ip",
    "user_agent",
)


def _validate_event_metadata(event_name: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    allowed = EVENT_METADATA_ALLOWLIST[event_name]
    unknown = set(metadata) - allowed
    if unknown:
        raise ValueError(f"Disallowed event metadata: {sorted(unknown)}")
    clean: dict[str, Any] = {}
    for key, value in metadata.items():
        if any(part in str(key).casefold() for part in _PII_KEY_PARTS):
            raise ValueError(f"PII-like event metadata key is not allowed: {key}")
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError(f"Event metadata must be scalar: {key}")
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            raise ValueError(f"Event metadata value is not serializable: {key}")
        clean[key] = value
    return clean


@dataclass(frozen=True)
class OwnerPortalEventV1:
    event_name: str
    property_code: str | None
    event_at_utc: str
    event_at_local: str
    session_id: str | None
    portal_token_id: str | None
    metadata: dict[str, Any]
    schema_version: str = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.event_name not in OWNER_PORTAL_EVENT_NAMES:
            raise ValueError(f"Unknown owner portal event: {self.event_name}")
        if not self.event_at_utc or not self.event_at_local:
            raise ValueError("OwnerPortalEventV1 requires UTC and local timestamps")
        object.__setattr__(self, "metadata", _validate_event_metadata(self.event_name, self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_name": self.event_name,
            "property_code": self.property_code,
            "event_at_utc": self.event_at_utc,
            "event_at_local": self.event_at_local,
            "session_id": self.session_id,
            "portal_token_id": self.portal_token_id,
            "schema_version": self.schema_version,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class OwnerPortalAccessV1:
    """Future access record; token raw is intentionally not a field."""

    token_id: str
    property_code: str
    issued_at: str
    expires_at: str
    revoked_at: str | None
    status: str
    purpose: str
    created_by: str
    token_hash: str

    def __post_init__(self) -> None:
        if self.status not in {"ACTIVE", "EXPIRED", "REVOKED"}:
            raise ValueError(f"Unknown owner portal access status: {self.status}")
        if self.purpose not in {"owner_portal_view", "price_change_authorization"}:
            raise ValueError(f"Unknown owner portal access purpose: {self.purpose}")
        if not self.token_hash or not self.token_id or not self.property_code:
            raise ValueError("OwnerPortalAccessV1 requires identifiers and token_hash")

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "property_code": self.property_code,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "status": self.status,
            "purpose": self.purpose,
            "created_by": self.created_by,
            "token_hash": self.token_hash,
        }


def assert_owner_portal_payload_allowlisted(payload: Mapping[str, Any]) -> None:
    """Raise if a serialized owner payload contains an undeclared field."""

    unknown = set(payload) - OWNER_PORTAL_VIEW_ALLOWLIST
    if unknown:
        raise ValueError(f"Disallowed owner portal fields: {sorted(unknown)}")
    for key in ("current_price", "previous_price"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            extra = set(value) - OWNER_PORTAL_PRICE_ALLOWLIST
            if extra:
                raise ValueError(f"Disallowed price fields: {sorted(extra)}")
    quality = payload.get("data_quality")
    if isinstance(quality, Mapping):
        extra = set(quality) - OWNER_PORTAL_QUALITY_ALLOWLIST
        if extra:
            raise ValueError(f"Disallowed quality fields: {sorted(extra)}")
    indicators = payload.get("national_indicators")
    if isinstance(indicators, (list, tuple)):
        expected = {
            "indicator_id", "scope", "geography", "value", "unit", "period",
            "source_name", "source_url", "retrieved_at", "valid_until",
        }
        for indicator in indicators:
            if isinstance(indicator, Mapping):
                extra = set(indicator) - expected
                if extra:
                    raise ValueError(f"Disallowed market indicator fields: {sorted(extra)}")
    local_context = payload.get("local_context")
    if isinstance(local_context, Mapping):
        expected = {
            "scope", "geography", "property_type", "available", "comparables_count",
            "median_price_uf", "median_uf_m2", "public_uf_m2", "effective_uf_m2",
            "price_variation_12m_pct", "active_listings", "total_listings", "trend",
            "liquidity", "competition", "range_price_uf", "source_name",
            "source_reference", "retrieved_at",
        }
        extra = set(local_context) - expected
        if extra:
            raise ValueError(f"Disallowed local context fields: {sorted(extra)}")
    positioning = payload.get("positioning")
    if isinstance(positioning, Mapping):
        expected = {"price_uf", "low_uf", "high_uf", "marker_pct", "label", "comparable_count"}
        extra = set(positioning) - expected
        if extra:
            raise ValueError(f"Disallowed positioning fields: {sorted(extra)}")
