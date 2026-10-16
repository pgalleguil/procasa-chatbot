"""Normalize authorized publication feed/import rows without fetching provider pages."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from chatbot.publication_sync import PROVIDERS
from chatbot.property_lookup import (
    build_property_alias,
    canonical_portal,
    extract_property_external_id,
    normalize_property_url,
)

AUTHORIZED_SOURCE_TYPES = {"proppit_api", "convecta_feed", "platform_export", "admin_evidence_import"}
ATTRIBUTION_STATES = {"declared", "confirmed", "refuted"}
AVAILABILITY_STATES = {"unknown", "available", "unavailable"}
OPERATIONS = {"V", "A", "R"}
SUPPORTED_PROVIDERS = {"proppit", "portal_inmobiliario", "mercadolibre", *PROVIDERS}


def _provider_name(value: str) -> str:
    compact = "".join(char for char in str(value or "").strip().lower() if char.isalnum())
    labels = {
        "portalinmobiliario": "portal_inmobiliario",
        "mercadolibre": "mercadolibre",
        "mlc": "mercadolibre",
        "icasas": "icasas",
        "trovit": "trovit",
        "mitula": "mitula",
        "nuroa": "nuroa",
        "nestoria": "nestoria",
        "proppit": "proppit",
    }
    return labels.get(compact, "")


def load_publication_rows(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if source.suffix.lower() == ".json":
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
        rows = raw.get("records") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            raise ValueError("JSON feed must be a list or contain a records list")
        return [row for row in rows if isinstance(row, dict)]
    if source.suffix.lower() == ".csv":
        with source.open("r", newline="", encoding="utf-8-sig") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    raise ValueError("authorized publication input must be .json or .csv")


def normalize_authorized_publication_rows(rows: Iterable[dict[str, Any]], *,
                                          source_type: str,
                                          imported_at: str | None = None) -> dict:
    """Build resolver-compatible aliases and distribution data with evidence levels.

    User-entered or source-supplied URLs default to declared/unresolvable. Only an
    explicit attribution confirmation with a verifiable evidence reference makes
    a reference resolvable; availability is recorded independently.
    """
    source_type = str(source_type or "").strip().lower()
    if source_type not in AUTHORIZED_SOURCE_TYPES:
        raise ValueError("source_type_not_authorized")
    imported_at = imported_at or datetime.now(timezone.utc).isoformat()
    normalized = {"aliases": [], "proppit": {"portales_distribucion": {}}}
    rejected = []
    for index, row in enumerate(rows):
        code = str(row.get("codigo_prop360") or row.get("codigo") or "").strip()
        provider = _provider_name(str(row.get("portal") or row.get("provider") or ""))
        operation = str(row.get("operacion") or row.get("operation") or "").strip().upper()
        url = str(row.get("url") or row.get("destination_url") or "").strip()
        attribution = str(row.get("attribution_status") or "declared").strip().lower()
        availability = str(row.get("availability_status") or "unknown").strip().lower()
        role = str(row.get("publication_role") or "current").strip().lower()
        evidence = str(row.get("evidence_reference") or "").strip()
        source_record_id = str(row.get("source_record_id") or "").strip()
        if not code or not url or provider not in SUPPORTED_PROVIDERS or operation not in OPERATIONS:
            rejected.append({"row": index, "reason": "missing_or_unsupported_identity"})
            continue
        if attribution not in ATTRIBUTION_STATES or availability not in AVAILABILITY_STATES:
            rejected.append({"row": index, "reason": "invalid_evidence_status"})
            continue
        if role not in {"current", "historical"}:
            rejected.append({"row": index, "reason": "invalid_publication_role"})
            continue
        url_provider = canonical_portal(url)
        provider_namespace = {"mercadolibre", "portal_inmobiliario"} if provider in {"mercadolibre", "portal_inmobiliario"} else {provider}
        if url_provider not in provider_namespace:
            rejected.append({"row": index, "reason": "provider_url_domain_mismatch"})
            continue
        if attribution == "confirmed" and not (evidence and source_record_id):
            rejected.append({"row": index, "reason": "confirmed_attribution_requires_evidence_and_record_id"})
            continue
        if attribution == "refuted":
            rejected.append({"row": index, "reason": "refuted_attribution_not_imported_as_resolvable"})
            continue
        external_id = str(row.get("external_id") or extract_property_external_id(url, provider) or "").strip() or None
        resolvable = attribution == "confirmed"
        status = "historical" if role == "historical" else "active"
        alias = build_property_alias(
            url, provider, operation, external_id,
            active=(role == "current" and availability != "unavailable"),
            publication_status=status, resolvable=resolvable,
            source=source_type,
            verification_status="source_verified" if resolvable else "unverified",
            source_url=str(row.get("source_url") or "").strip() or None,
            first_seen_at=imported_at, last_seen_at=imported_at,
            verified_at=imported_at if resolvable else None,
        )
        alias.update({
            "attribution_status": attribution,
            "availability_status": availability,
            "evidence_reference": evidence or None,
            "source_record_id": source_record_id or None,
            "publication_role": role,
        })
        normalized["aliases"].append(alias)
        if provider in PROVIDERS:
            operation_node = normalized["proppit"]["portales_distribucion"].setdefault(provider, {})
            operation_node[operation] = {
                "url_actual": url if role == "current" and resolvable else None,
                "external_id": external_id if role == "current" and resolvable else None,
                "urls_observadas": [url],
                "source_type": source_type,
                "source_url": str(row.get("source_url") or "").strip() or None,
                "source_record_id": source_record_id or None,
                "attribution_status": attribution,
                "availability_status": availability,
                "verification_status": "source_verified" if resolvable else "unverified",
                "publication_status": status,
                "evidence_reference": evidence or None,
                "observed_at": imported_at,
            }
    normalized["aliases"].sort(key=lambda item: (item["portal"], item["operacion"], item["url_normalized"]))
    return {"source_type": source_type, "imported_at": imported_at,
            "publications": normalized, "rejected_rows": rejected,
            "accepted_rows": len(normalized["aliases"])}

