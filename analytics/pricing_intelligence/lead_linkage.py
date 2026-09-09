"""Read-only lead-to-property linkage and quality metrics."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping

from .models import LeadLinkageRecord, LinkageStatus
from .property_identity import (
    PropertyIdentityResolver,
    normalize_identifier,
    normalize_portal_source,
)
from .time_utils import BUSINESS_TZ, parse_aware_datetime


_SENSITIVE_FIELD_PARTS = (
    "email",
    "correo",
    "phone",
    "telefono",
    "teléfono",
    "rut",
    "nombre",
    "mensaje",
    "direccion",
    "dirección",
    "coment",
    "consulta",
    "pregunta",
    "observacion",
    "observación",
    "nota",
)
_STRUCTURED_ID_PARTS = (
    "codigo",
    "code",
    "listing",
    "property",
    "propiedad",
    "publicacion",
    "publicación",
    "url",
    "enlace",
    "link",
    "external_id",
)


def _prospecto(lead: Mapping[str, Any]) -> Mapping[str, Any]:
    value = lead.get("prospecto")
    return value if isinstance(value, Mapping) else {}


def _declared_source(prospecto: Mapping[str, Any]) -> str:
    for field_name in (
        "origen",
        "fuente_lead",
        "portal_origen",
        "origen_anuncio",
        "plataforma_origen",
    ):
        normalized = normalize_portal_source(prospecto.get(field_name))
        if normalized:
            return normalized
        raw = normalize_identifier(prospecto.get(field_name))
        if raw:
            key = raw.casefold().replace(" ", "").replace("_", "").replace("-", "")
            if key == "whatsapp":
                return "whatsapp"
            if key == "otroportal":
                return "otro_portal"
            if key == "test":
                return "test"
    return "MISSING_ORIGIN"


def _is_structured_identifier_field(field_name: str) -> bool:
    normalized = field_name.casefold()
    return (
        any(part in normalized for part in _STRUCTURED_ID_PARTS)
        and not any(part in normalized for part in _SENSITIVE_FIELD_PARTS)
    )


def _has_other_structured_identifier(prospecto: Mapping[str, Any]) -> bool:
    excluded = {"codigo", "codigo_mercadolibre", "codigo_yapo"}
    return any(
        field_name not in excluded
        and _is_structured_identifier_field(str(field_name))
        and normalize_identifier(value)
        for field_name, value in prospecto.items()
    )


def _has_url(prospecto: Mapping[str, Any]) -> bool:
    return any(
        any(part in str(field_name).casefold() for part in ("url", "link", "enlace"))
        and isinstance(value, str)
        and bool(value.strip())
        for field_name, value in prospecto.items()
    )


def classify_unmatched_reason(
    lead: Mapping[str, Any],
    resolver: PropertyIdentityResolver,
    legacy_code_sets: Mapping[str, set[str]],
) -> str | None:
    """Return one mutually exclusive, diagnostic-only reason for UNMATCHED."""

    if resolver.resolve(lead).status is not LinkageStatus.UNMATCHED:
        return None
    prospecto = _prospecto(lead)
    raw_canonical = prospecto.get("codigo")
    canonical = normalize_identifier(raw_canonical)
    if raw_canonical is not None and canonical is None:
        return "INVALID_IDENTIFIER"
    if canonical:
        if any(canonical in values for values in legacy_code_sets.values()):
            return "CANONICAL_NOT_IN_CURRENT_MASTER_BUT_FOUND_LEGACY"
        return "CANONICAL_NOT_FOUND_ANYWHERE"

    if normalize_identifier(prospecto.get("codigo_mercadolibre")) or normalize_identifier(
        prospecto.get("codigo_yapo")
    ):
        return "NO_CANONICAL_HAS_PORTAL_ID"
    if _has_other_structured_identifier(prospecto) or _has_url(prospecto):
        return "UNSUPPORTED_OR_UNVERIFIED_IDENTIFIER"
    return "NO_PROPERTY_IDENTIFIER"


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

            for alias, _path in self.resolver.lead_aliases(lead):
                alias_usage[alias.source] += 1

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

    def unmatched_reason_distribution(
        self,
        leads: Iterable[Mapping[str, Any]],
        legacy_code_sets: Mapping[str, set[str]],
    ) -> dict[str, dict[str, float | int]]:
        counts = Counter(
            reason
            for lead in leads
            if (reason := classify_unmatched_reason(lead, self.resolver, legacy_code_sets)) is not None
        )
        total = sum(counts.values())
        return {
            reason: {
                "count": count,
                "pct": round(count / total * 100, 2) if total else 0.0,
            }
            for reason, count in sorted(counts.items())
        }

    def unmatched_diagnostic_dimensions(
        self,
        leads: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Aggregate dimensions for unmatched leads without returning values."""

        source_counts: Counter[str] = Counter()
        field_counts: Counter[str] = Counter()
        date_counts: Counter[str] = Counter()
        presence = Counter()
        url_host_counts: Counter[str] = Counter()

        for lead in leads:
            if self.resolver.resolve(lead).status is not LinkageStatus.UNMATCHED:
                continue
            prospecto = _prospecto(lead)
            source_counts[_declared_source(prospecto)] += 1
            canonical_raw = prospecto.get("codigo")
            canonical = normalize_identifier(canonical_raw)
            if canonical:
                presence["canonical_nonempty"] += 1
            elif canonical_raw is not None and canonical_raw != "":
                presence["canonical_invalid"] += 1
            else:
                presence["canonical_missing_or_empty"] += 1
            if normalize_identifier(prospecto.get("codigo_mercadolibre")):
                presence["mercadolibre_identifier"] += 1
            if normalize_identifier(prospecto.get("codigo_yapo")):
                presence["yapo_identifier"] += 1
            if _has_other_structured_identifier(prospecto):
                presence["other_structured_identifier"] += 1
            if _has_url(prospecto):
                presence["url_present"] += 1
            for field_name, value in prospecto.items():
                if _is_structured_identifier_field(str(field_name)) and normalize_identifier(value):
                    field_counts[str(field_name)] += 1
            created_at = lead.get("created_at")
            if created_at is None:
                date_counts["MISSING"] += 1
            else:
                try:
                    parsed = parse_aware_datetime(created_at, field_name="lead.created_at")
                    date_counts[parsed.astimezone(BUSINESS_TZ).strftime("%Y-%m")] += 1
                except (TypeError, ValueError):
                    date_counts["INVALID"] += 1
            for field_name, value in prospecto.items():
                if not _has_url({field_name: value}):
                    continue
                try:
                    from urllib.parse import urlparse

                    host = urlparse(str(value).strip()).hostname or "INVALID_HOST"
                except ValueError:
                    host = "INVALID_HOST"
                host_key = next(
                    (
                        label
                        for label in (
                            "mercadolibre",
                            "portalinmobiliario",
                            "yapo",
                            "toctoc",
                            "chilepropiedades",
                            "proppit",
                            "procasa",
                        )
                        if label in host.casefold()
                    ),
                    "OTHER_HOST",
                )
                url_host_counts[host_key] += 1

        return {
            "source": dict(source_counts.most_common()),
            "identifier_presence": dict(presence),
            "structured_identifier_fields": dict(field_counts.most_common()),
            "lead_date_local_month": dict(sorted(date_counts.items())),
            "url_hosts": dict(url_host_counts),
        }
