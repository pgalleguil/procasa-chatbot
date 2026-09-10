"""Read-only market intelligence ingestion contracts and dry-run adapters."""

from .models import MarketIntelligenceSnapshotV1
from .sources import BDE_API_ENDPOINT, OFFICIAL_SOURCES, run_dry_run

__all__ = [
    "BDE_API_ENDPOINT",
    "MarketIntelligenceSnapshotV1",
    "OFFICIAL_SOURCES",
    "run_dry_run",
]
