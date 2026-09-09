"""Small, dependency-light contracts for the pricing intelligence layer.

These models deliberately contain only analytical fields.  They do not carry
owner contact data, messages, full addresses, or complete Mongo documents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping, Optional, Tuple


class LinkageStatus(str, Enum):
    EXACT_CANONICAL = "EXACT_CANONICAL"
    EXACT_ALIAS = "EXACT_ALIAS"
    AMBIGUOUS = "AMBIGUOUS"
    CONFLICT = "CONFLICT"
    UNMATCHED = "UNMATCHED"


@dataclass(frozen=True, order=True)
class AliasKey:
    """Namespaced external identifier.

    The source is part of equality.  Therefore an identifier such as ``123``
    from Yapo cannot collide with ``123`` from another portal.
    """

    source: str
    external_id: str

    def __post_init__(self) -> None:
        if not str(self.source).strip():
            raise ValueError("Alias source cannot be empty")
        if not str(self.external_id).strip():
            raise ValueError("Alias external_id cannot be empty")

    def as_string(self) -> str:
        return f"{self.source}:{self.external_id}"


@dataclass(frozen=True)
class PropertyIdentityResolution:
    status: LinkageStatus
    resolved_property_code: Optional[str]
    evidence: Tuple[str, ...] = ()
    candidates: Tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "resolved_property_code": self.resolved_property_code,
            "evidence": list(self.evidence),
            "candidates": list(self.candidates),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LeadLinkageRecord:
    """PII-free linkage result for one lead document."""

    lead_key: str
    resolution: PropertyIdentityResolution
    created_at: Optional[datetime]
    timestamp_error: Optional[str] = None

    @property
    def status(self) -> LinkageStatus:
        return self.resolution.status

    @property
    def property_code(self) -> Optional[str]:
        return self.resolution.resolved_property_code


@dataclass(frozen=True)
class PriceChange:
    changed_at: datetime
    unit: str
    previous_value: float
    new_value: float
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_at": self.changed_at.isoformat(),
            "unit": self.unit,
            "previous_value": self.previous_value,
            "new_value": self.new_value,
            "source": self.source,
        }


@dataclass(frozen=True)
class PublicationState:
    portal: str
    operation: Optional[str]
    active: Optional[bool]
    status: Optional[str]
    listing_identifier: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "portal": self.portal,
            "operation": self.operation,
            "active": self.active,
            "status": self.status,
            "listing_identifier": self.listing_identifier,
        }


@dataclass(frozen=True)
class DataQuality:
    missing_price: bool
    missing_surface: bool
    missing_bedrooms: bool
    missing_bathrooms: bool
    lead_linkage_available: bool
    price_history_available: bool
    publication_data_available: bool

    def to_dict(self) -> dict[str, bool]:
        return {
            "missing_price": self.missing_price,
            "missing_surface": self.missing_surface,
            "missing_bedrooms": self.missing_bedrooms,
            "missing_bathrooms": self.missing_bathrooms,
            "lead_linkage_available": self.lead_linkage_available,
            "price_history_available": self.price_history_available,
            "publication_data_available": self.publication_data_available,
        }


@dataclass(frozen=True)
class PropertyDailySnapshotV1:
    schema_version: str
    property_code: str
    snapshot_date_local: date
    as_of_local: datetime
    as_of_utc: datetime
    operation: Optional[str]
    property_type: Optional[str]
    region: Optional[str]
    commune: Optional[str]
    sector: Optional[str]
    bedrooms: Optional[float]
    bathrooms: Optional[float]
    parking: Optional[float]
    built_area_m2: Optional[float]
    land_area_m2: Optional[float]
    current_price_uf: Optional[float]
    current_price_clp: Optional[float]
    last_price_change_at: Optional[datetime]
    previous_price_uf: Optional[float]
    previous_price_clp: Optional[float]
    publications: Tuple[PublicationState, ...]
    linked_leads_previous_7d: int
    linked_leads_previous_30d: int
    data_quality: DataQuality
    provenance: Mapping[str, Any]
    listed_at: Optional[datetime] = None
    days_published: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        def iso(value: Optional[datetime]) -> Optional[str]:
            return value.isoformat() if value is not None else None

        return {
            "schema_version": self.schema_version,
            "property_code": self.property_code,
            "snapshot_date_local": self.snapshot_date_local.isoformat(),
            "as_of_local": self.as_of_local.isoformat(),
            "as_of_utc": self.as_of_utc.isoformat(),
            "operation": self.operation,
            "property_type": self.property_type,
            "region": self.region,
            "commune": self.commune,
            "sector": self.sector,
            "bedrooms": self.bedrooms,
            "bathrooms": self.bathrooms,
            "parking": self.parking,
            "built_area_m2": self.built_area_m2,
            "land_area_m2": self.land_area_m2,
            "current_price_uf": self.current_price_uf,
            "current_price_clp": self.current_price_clp,
            "last_price_change_at": iso(self.last_price_change_at),
            "previous_price_uf": self.previous_price_uf,
            "previous_price_clp": self.previous_price_clp,
            "publications": [item.to_dict() for item in self.publications],
            "linked_leads_previous_7d": self.linked_leads_previous_7d,
            "linked_leads_previous_30d": self.linked_leads_previous_30d,
            "data_quality": self.data_quality.to_dict(),
            "provenance": dict(self.provenance),
            "listed_at": iso(self.listed_at),
            "days_published": self.days_published,
        }
