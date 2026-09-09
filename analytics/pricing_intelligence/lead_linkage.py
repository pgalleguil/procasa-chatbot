"""Read-only lead-to-property linkage and quality metrics."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Iterable, Mapping

from .models import LeadLinkageRecord, LinkageStatus
from .property_identity import LEAD_ALIAS_PATHS, PropertyIdentityResolver, normalize_identifier
from .time_utils import parse_aware_datetime


class LeadLinkageService:
    def __init__(self, resolver: PropertyIdentityResolver) -> None:
        self.resolver = resolver
        self._last_alias_usage: Counter[str] = Counter()

    def link_leads(self, leads: Iterable[Mapping[str, Any]]) -> list[LeadLinkageRecord]:
        records: list[LeadLinkageRecord] = []
        alias_usage: Counter[str] = Counter()

        for index, lead in enumerate(leads):
            resolution = self.resolver.resolve(lead)
            lead_key = str(lead.get("_id")) if lead.get("_id") is not None else f"row-{index}"

            prospecto = lead.get("prospecto")
            if isinstance(prospecto, Mapping):
                for source, path in LEAD_ALIAS_PATHS:
                    field_name = path.rsplit(".", 1)[-1]
                    if normalize_identifier(prospecto.get(field_name)):
                        alias_usage[source] += 1

            created_at = None
            timestamp_error = None
            if lead.get("created_at") is not None:
                try:
                    created_at = parse_aware_datetime(lead["created_at"], field_name="lead.created_at")
                except (TypeError, ValueError) as exc:
                    timestamp_error = str(exc)

            records.append(
                LeadLinkageRecord(
                    lead_key=lead_key,
                    resolution=resolution,
                    created_at=created_at,
                    timestamp_error=timestamp_error,
                )
            )

        self._last_alias_usage = alias_usage
        return records

    def metrics(self, records: Iterable[LeadLinkageRecord]) -> dict[str, Any]:
        records = list(records)
        total = len(records)
        status_counts = Counter(record.status.value for record in records)
        percentages = {
            status.value: round(status_counts.get(status.value, 0) / total * 100, 2) if total else 0.0
            for status in LinkageStatus
        }
        linked_statuses = {LinkageStatus.EXACT_CANONICAL, LinkageStatus.EXACT_ALIAS}
        linked_properties = {
            record.property_code
            for record in records
            if record.status in linked_statuses and record.property_code
        }
        conflicts_by_source: Counter[str] = Counter()
        for record in records:
            if record.status != LinkageStatus.CONFLICT:
                continue
            for evidence in record.resolution.evidence:
                if evidence.startswith("alias:"):
                    parts = evidence.split(":", 2)
                    if len(parts) >= 2:
                        conflicts_by_source[parts[1]] += 1

        return {
            "total": total,
            "counts_by_status": {status.value: status_counts.get(status.value, 0) for status in LinkageStatus},
            "percentages_by_status": percentages,
            "distinct_linked_properties": len(linked_properties),
            "aliases_most_used": dict(self._last_alias_usage.most_common()),
            "conflicts_by_source": dict(conflicts_by_source),
            "records_with_timestamp_error": sum(record.timestamp_error is not None for record in records),
            "ambiguous_aliases_in_master": self.resolver.ambiguous_alias_count,
        }
