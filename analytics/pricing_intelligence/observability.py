"""Read-only demand and publication observability contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from .time_utils import BUSINESS_TZ, ensure_aware, to_business_time, to_utc


WINDOW_COMPLETE = "COMPLETE"
WINDOW_PARTIAL = "PARTIAL"
WINDOW_UNKNOWN = "UNKNOWN"

SOURCE_FULL = "FULL"
SOURCE_PARTIAL = "PARTIAL"
SOURCE_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PortalWindowEvidence:
    portal: str
    published_at: datetime | None
    published_at_source: str | None
    source_observable: bool
    source_observable_from: datetime | None
    confidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "portal": self.portal,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "published_at_source": self.published_at_source,
            "source_observable": self.source_observable,
            "source_observable_from": self.source_observable_from.isoformat()
            if self.source_observable_from
            else None,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class PropertyObservationEvidence:
    property_code: str
    operation: str | None
    as_of: datetime
    active_lead_capable_portals: tuple[str, ...] = ()
    portal_windows: tuple[PortalWindowEvidence, ...] = ()
    earliest_verified_publication_at: datetime | None = None
    full_channel_observation_start: datetime | None = None
    verified_days_any_channel: int | None = None
    verified_days_full_channel: int | None = None
    window_30d: str = WINDOW_UNKNOWN
    window_90d: str = WINDOW_UNKNOWN
    provenance: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "property_code": self.property_code,
            "operation": self.operation,
            "as_of": self.as_of.isoformat(),
            "active_lead_capable_portals": list(self.active_lead_capable_portals),
            "portal_windows": [item.to_dict() for item in self.portal_windows],
            "earliest_verified_publication_at": iso(self.earliest_verified_publication_at),
            "full_channel_observation_start": iso(self.full_channel_observation_start),
            "verified_days_any_channel": self.verified_days_any_channel,
            "verified_days_full_channel": self.verified_days_full_channel,
            "window_30d": self.window_30d,
            "window_90d": self.window_90d,
            "provenance": dict(self.provenance),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class LeadSourceCoverage:
    property_code: str
    window: str
    active_publication_portals: tuple[str, ...]
    crm_lead_capable_portals: tuple[str, ...]
    covered_portals: tuple[str, ...]
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "property_code": self.property_code,
            "window": self.window,
            "active_publication_portals": list(self.active_publication_portals),
            "crm_lead_capable_portals": list(self.crm_lead_capable_portals),
            "covered_portals": list(self.covered_portals),
            "status": self.status,
        }


def _window_quality(start: datetime | None, as_of: datetime, days: int) -> str:
    if start is None:
        return WINDOW_UNKNOWN
    return WINDOW_COMPLETE if to_utc(start) <= to_utc(as_of) - timedelta(days=days) else WINDOW_PARTIAL


def _days_between(start: datetime | None, as_of: datetime) -> int | None:
    if start is None:
        return None
    return max(0, (to_business_time(as_of) - to_business_time(start)).days)


def build_observation_evidence(
    *,
    property_code: str,
    operation: str | None,
    as_of: datetime,
    active_publication_portals: tuple[str, ...] | list[str],
    publication_dates: Mapping[str, tuple[datetime, str] | None],
    crm_lead_capable_portals: tuple[str, ...] | list[str],
    source_observable_from: Mapping[str, datetime | None] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> PropertyObservationEvidence:
    """Build evidence from already verified inputs; never queries Mongo."""

    cutoff = ensure_aware(as_of, field_name="as_of")
    active = tuple(sorted({str(item) for item in active_publication_portals if item}))
    capable = tuple(sorted({str(item) for item in crm_lead_capable_portals if item}))
    active_capable = tuple(item for item in active if item in capable)
    source_dates = source_observable_from or {}
    windows: list[PortalWindowEvidence] = []
    verified_dates: list[datetime] = []
    for portal in active:
        publication = publication_dates.get(portal)
        published_at = None
        published_source = None
        if publication is not None:
            published_at, published_source = publication
            published_at = ensure_aware(published_at, field_name="published_at")
            if to_utc(published_at) < to_utc(cutoff):
                verified_dates.append(published_at)
            else:
                published_at = None
                published_source = None
        observable_from = source_dates.get(portal)
        if observable_from is not None:
            observable_from = ensure_aware(observable_from, field_name="source_observable_from")
        windows.append(
            PortalWindowEvidence(
                portal=portal,
                published_at=published_at,
                published_at_source=published_source,
                source_observable=portal in capable,
                source_observable_from=observable_from,
                confidence="exact" if published_at is not None else "unknown",
            )
        )

    earliest = min(verified_dates) if verified_dates else None
    portal_by_name = {item.portal: item for item in windows}
    full_starts: list[datetime] = []
    for portal in active_capable:
        item = portal_by_name[portal]
        # A pipeline-wide first-seen date cannot prove that this property's
        # listing was observable before that date.  Require both pieces of
        # evidence for each channel contributing to the verified window.
        if item.published_at is None or item.source_observable_from is None:
            continue
        full_starts.append(max(item.published_at, item.source_observable_from))
    full_start = None
    warnings: list[str] = []
    if full_starts:
        full_start = max(ensure_aware(value, field_name="full_channel_start") for value in full_starts)
        if len(full_starts) < len(active_capable):
            warnings.append("some_active_channels_lack_property_publication_date")
    elif active_capable:
        warnings.append("full_channel_start_unknown")

    return PropertyObservationEvidence(
        property_code=str(property_code),
        operation=operation,
        as_of=cutoff,
        active_lead_capable_portals=active_capable,
        portal_windows=tuple(windows),
        earliest_verified_publication_at=earliest,
        full_channel_observation_start=full_start,
        verified_days_any_channel=_days_between(earliest, cutoff),
        verified_days_full_channel=_days_between(full_start, cutoff),
        window_30d=_window_quality(full_start, cutoff, 30),
        window_90d=_window_quality(full_start, cutoff, 90),
        provenance=dict(provenance or {}),
        warnings=tuple(warnings),
    )


def derive_lead_source_coverage(
    *,
    property_code: str,
    window: str,
    active_publication_portals: tuple[str, ...] | list[str],
    crm_lead_capable_portals: tuple[str, ...] | list[str],
    covered_portals: tuple[str, ...] | list[str],
) -> LeadSourceCoverage:
    active = tuple(sorted(set(active_publication_portals)))
    capable = tuple(sorted(set(active).intersection(crm_lead_capable_portals)))
    covered = tuple(sorted(set(covered_portals).intersection(capable)))
    if not capable or window == WINDOW_UNKNOWN:
        status = SOURCE_UNKNOWN
    elif set(covered) == set(capable):
        status = SOURCE_FULL
    else:
        status = SOURCE_PARTIAL
    return LeadSourceCoverage(
        property_code=str(property_code),
        window=window,
        active_publication_portals=active,
        crm_lead_capable_portals=capable,
        covered_portals=covered,
        status=status,
    )


def demand_observation(
    *,
    count: int,
    window: str,
    source_coverage: str,
    linkage_quality: str = "exact",
) -> dict[str, Any]:
    """Return separate signal, level and confidence; never collapse them."""

    count = max(0, int(count))
    if count > 0:
        confidence = "high" if window == WINDOW_COMPLETE and source_coverage == SOURCE_FULL else "medium" if window != WINDOW_UNKNOWN else "low"
        if linkage_quality != "exact":
            confidence = "low"
        return {
            "demand_count": count,
            "demand_signal": "OBSERVED_POSITIVE",
            "demand_level": "positive",
            "demand_confidence": confidence,
            "source_coverage": source_coverage,
            "observation_window_quality": window,
        }

    if window == WINDOW_UNKNOWN:
        signal = "ZERO_UNCERTAIN"
    elif source_coverage == SOURCE_FULL:
        signal = "OBSERVED_ZERO_COMPLETE"
    else:
        signal = "OBSERVED_ZERO_PARTIAL"
    confidence = "high" if signal == "OBSERVED_ZERO_COMPLETE" else "low" if signal == "OBSERVED_ZERO_PARTIAL" else "unknown"
    return {
        "demand_count": 0,
        "demand_signal": signal,
        "demand_level": "very_low" if signal != "ZERO_UNCERTAIN" else "unknown",
        "demand_confidence": confidence,
        "source_coverage": source_coverage,
        "observation_window_quality": window,
    }


def coerce_observation_evidence(value: Any) -> PropertyObservationEvidence | None:
    """Accept a model or serialized mapping for snapshot-builder compatibility."""

    if isinstance(value, PropertyObservationEvidence):
        return value
    if not isinstance(value, Mapping):
        return None
    try:
        as_of = datetime.fromisoformat(str(value["as_of"]))
        earliest_raw = value.get("earliest_verified_publication_at")
        full_raw = value.get("full_channel_observation_start")
        return PropertyObservationEvidence(
            property_code=str(value["property_code"]),
            operation=value.get("operation"),
            as_of=ensure_aware(as_of, field_name="observation.as_of"),
            active_lead_capable_portals=tuple(value.get("active_lead_capable_portals") or ()),
            portal_windows=tuple(),
            earliest_verified_publication_at=(
                ensure_aware(datetime.fromisoformat(str(earliest_raw)), field_name="observation.earliest")
                if earliest_raw
                else None
            ),
            full_channel_observation_start=(
                ensure_aware(datetime.fromisoformat(str(full_raw)), field_name="observation.full")
                if full_raw
                else None
            ),
            verified_days_any_channel=value.get("verified_days_any_channel"),
            verified_days_full_channel=value.get("verified_days_full_channel"),
            window_30d=str(value.get("window_30d") or WINDOW_UNKNOWN),
            window_90d=str(value.get("window_90d") or WINDOW_UNKNOWN),
            provenance=value.get("provenance") or {},
            warnings=tuple(value.get("warnings") or ()),
        )
    except (KeyError, TypeError, ValueError):
        return None
