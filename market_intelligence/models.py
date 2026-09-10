from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class MarketIntelligenceSnapshotV1:
    """One source-backed indicator snapshot produced outside the pageview."""

    indicator_id: str
    scope: str
    geography: str
    value: float | str | None
    unit: str
    reference_period: str
    source_name: str
    source_reference: str
    retrieved_at_utc: str
    source_published_at: str | None
    status: str
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "indicator_id": self.indicator_id,
            "scope": self.scope,
            "geography": self.geography,
            "value": self.value,
            "unit": self.unit,
            "reference_period": self.reference_period,
            "source_name": self.source_name,
            "source_reference": self.source_reference,
            "retrieved_at_utc": self.retrieved_at_utc,
            "source_published_at": self.source_published_at,
            "status": self.status,
            "provenance": dict(self.provenance),
        }
