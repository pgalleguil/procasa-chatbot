"""Persistence boundary for V1 snapshots.

The repository intentionally has no write path in this phase.  Keeping the
boundary explicit prevents a dry-run from accidentally creating collections,
indexes, or documents in production MongoDB.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from .models import LeadLinkageRecord, PropertyDailySnapshotV1
from .snapshot_builder import PropertySnapshotBuilder, SnapshotBuildResult


PROPOSED_COLLECTION = "pricing_intelligence_property_snapshots_v1"
PROPOSED_IDEMPOTENT_INDEX = (
    ("snapshot_date_local", 1),
    ("property_code", 1),
)


class PersistenceDisabled(RuntimeError):
    """V1 does not permit persistence, regardless of caller intent."""


class SnapshotRepository:
    def __init__(self, builder: PropertySnapshotBuilder) -> None:
        self.builder = builder

    def build(
        self,
        properties: Iterable[Mapping[str, Any]],
        *,
        as_of=None,
        separate_price_events: Iterable[Mapping[str, Any]] = (),
        property_code: Optional[str] = None,
    ) -> SnapshotBuildResult:
        return self.builder.build(
            properties,
            as_of=as_of,
            separate_price_events=separate_price_events,
            property_code=property_code,
        )

    def persist(self, snapshots: Iterable[PropertyDailySnapshotV1]) -> None:
        # Do not add an opt-in flag here: this phase must be safe by design.
        raise PersistenceDisabled(
            f"V1 no permite persistencia; colección propuesta únicamente: {PROPOSED_COLLECTION}"
        )

    @staticmethod
    def proposed_persistence_contract() -> dict[str, Any]:
        return {
            "collection": PROPOSED_COLLECTION,
            "future_idempotent_index": list(PROPOSED_IDEMPOTENT_INDEX),
            "status": "DOCUMENTED_ONLY_NOT_CREATED",
        }
