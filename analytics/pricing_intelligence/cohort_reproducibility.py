"""Deterministic cohort fingerprints and full-precision statistics."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping


COHORT_RULE_VERSION = "cohort-v1-high-similarity-date-aware"


def _number_token(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite():
        return None
    return format(number, "f")


def _datetime_token(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row_token(row: Mapping[str, Any]) -> dict[str, Any]:
    listing_id = str(row.get("listing_id") or row.get("code") or "")
    return {
        "listing_id": listing_id,
        "price_uf": _number_token(row.get("price_uf")),
        "surface_m2": _number_token(row.get("surface_m2")),
        "observed_at": _datetime_token(row.get("observed_at") or row.get("when")),
    }


def canonical_cohort_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (_row_token(row) for row in rows),
        key=lambda item: (
            item["listing_id"],
            item["price_uf"] or "",
            item["surface_m2"] or "",
            item["observed_at"] or "",
        ),
    )


def cohort_fingerprint(
    rows: Iterable[Mapping[str, Any]],
    *,
    operation: str,
    cohort_level: str,
    rule_version: str = COHORT_RULE_VERSION,
    as_of: datetime | str,
    cutoff: datetime | str,
) -> str:
    payload = {
        "operation": operation,
        "cohort_level": cohort_level,
        "cohort_rule_version": rule_version,
        "as_of": _datetime_token(as_of),
        "cutoff": _datetime_token(cutoff),
        "rows": canonical_cohort_rows(rows),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def percentile_full_precision(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def cohort_reproducibility_metadata(
    rows: Iterable[Mapping[str, Any]],
    *,
    operation: str,
    cohort_level: str,
    as_of: datetime | str,
    cutoff: datetime | str,
    rule_version: str = COHORT_RULE_VERSION,
) -> dict[str, Any]:
    materialized = list(rows)
    prices = [float(row["price_uf"]) for row in materialized if row.get("price_uf") is not None]
    updated_values = [
        row.get("source_max_updated_at") or row.get("updated_at")
        for row in materialized
        if row.get("source_max_updated_at") is not None or row.get("updated_at") is not None
    ]
    updated_values = [value for value in updated_values if value is not None]
    source_max_updated_at = max((_datetime_token(value) for value in updated_values), default=None)
    return {
        "cohort_as_of": _datetime_token(as_of),
        "cohort_level": cohort_level,
        "cohort_count": len(materialized),
        "cohort_rule_version": rule_version,
        "source_max_updated_at": source_max_updated_at,
        "listing_ids_sorted": [item["listing_id"] for item in canonical_cohort_rows(materialized)],
        "cohort_fingerprint": cohort_fingerprint(
            materialized,
            operation=operation,
            cohort_level=cohort_level,
            rule_version=rule_version,
            as_of=as_of,
            cutoff=cutoff,
        ),
        "p10": percentile_full_precision(prices, 0.10),
        "p25": percentile_full_precision(prices, 0.25),
        "p50": percentile_full_precision(prices, 0.50),
        "p75": percentile_full_precision(prices, 0.75),
        "p90": percentile_full_precision(prices, 0.90),
    }
