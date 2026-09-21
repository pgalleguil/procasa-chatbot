"""Pre-LLM extractor health metrics and fail-closed regression gate."""
from __future__ import annotations

from typing import Any, Iterable


HEALTH_FIELDS = (
    "publisher_present",
    "seller_type_present",
    "profile_id_present",
    "client_id_present",
    "description_present",
    "title_present",
    "price_present",
    "commune_present",
    "structural_signal_present",
)

# Floors are intentionally conservative. A supplied historical baseline takes
# precedence for fields whose normal availability is known by the project.
DEFAULT_MIN_RATES = {
    "publisher_present": 0.70,
    "seller_type_present": 0.50,
    "profile_id_present": 0.20,
    "client_id_present": 0.20,
    "description_present": 0.90,
    "title_present": 0.90,
    "price_present": 0.70,
    "commune_present": 0.90,
    "structural_signal_present": 0.00,
}


def _present(value: Any) -> bool:
    if value in (None, "", [], {}, ()):  # noqa: SIM118
        return False
    if isinstance(value, str):
        return value.strip().casefold() not in {"n/a", "na", "s/i", "desconocido"}
    return True


def _value(document: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = document.get(key)
        if _present(value):
            return value
    for container_name in ("details", "canonical_identity", "source_signals"):
        container = document.get(container_name)
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = container.get(key)
            if _present(value):
                return value
    return None


def _has_price(document: dict[str, Any]) -> bool:
    return any(
        _present(_value(document, key))
        for key in ("precio_clp", "price_clp", "precio_uf", "price_uf", "price", "precio_raw")
    )


def _has_structural_signal(document: dict[str, Any]) -> bool:
    structural = _value(document, "structural_signals")
    if isinstance(structural, dict) and any(_present(v) for v in structural.values()):
        return True
    return any(
        _present(_value(document, key))
        for key in ("seller_profile_url", "seller_type_evidence", "operation_label_raw", "seller_profile_id", "seller_client_id")
    )


def measure_extractor_health(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(records)
    total = len(rows)
    counts = {field: 0 for field in HEALTH_FIELDS}
    for row in rows:
        checks = {
            "publisher_present": _value(row, "publicador_visible", "publisher", "seller_name", "contact_name"),
            "seller_type_present": _value(row, "seller_type"),
            "profile_id_present": _value(row, "seller_profile_id", "profile_id"),
            "client_id_present": _value(row, "seller_client_id", "client_id"),
            "description_present": _value(row, "description", "descripcion"),
            "title_present": _value(row, "title", "titulo"),
            "price_present": _has_price(row),
            "commune_present": _value(row, "comuna", "comuna_slug"),
            "structural_signal_present": _has_structural_signal(row),
        }
        for field, value in checks.items():
            if value is True or _present(value):
                counts[field] += 1
    rates = {field: round((counts[field] / total) if total else 0.0, 6) for field in HEALTH_FIELDS}
    return {
        "sample_size": total,
        "counts": counts,
        "rates": rates,
        "fields": list(HEALTH_FIELDS),
    }


def evaluate_extractor_health(
    metrics: dict[str, Any],
    *,
    baseline: dict[str, float] | None = None,
    min_rates: dict[str, float] | None = None,
    min_sample_size: int = 20,
    max_drop_ratio: float = 0.35,
) -> dict[str, Any]:
    rates = dict(metrics.get("rates") or {})
    sample_size = int(metrics.get("sample_size") or 0)
    baseline = baseline or {}
    min_rates = {**DEFAULT_MIN_RATES, **(min_rates or {})}
    anomalies: list[dict[str, Any]] = []
    comparisons: dict[str, dict[str, Any]] = {}
    if sample_size < max(1, int(min_sample_size)):
        return {
            **metrics,
            "healthy": True,
            "degraded": False,
            "anomalies": [],
            "comparisons": comparisons,
            "sample_insufficient_for_regression": True,
        }

    for field in HEALTH_FIELDS:
        actual = float(rates.get(field, 0.0) or 0.0)
        configured_floor = float(min_rates.get(field, 0.0) or 0.0)
        baseline_value = baseline.get(field)
        comparison = {"actual": actual, "floor": configured_floor}
        if baseline_value is not None:
            baseline_value = float(baseline_value)
            comparison["baseline"] = baseline_value
            if baseline_value > 0 and actual < baseline_value * (1.0 - max_drop_ratio):
                anomalies.append({
                    "field": field,
                    "type": "BASELINE_DROP",
                    "actual": actual,
                    "baseline": baseline_value,
                    "drop_ratio": round(1.0 - (actual / baseline_value), 6),
                })
        elif actual < configured_floor:
            anomalies.append({
                "field": field,
                "type": "BELOW_MINIMUM_FLOOR",
                "actual": actual,
                "floor": configured_floor,
            })
        comparisons[field] = comparison
    return {
        **metrics,
        "healthy": not anomalies,
        "degraded": bool(anomalies),
        "anomalies": anomalies,
        "comparisons": comparisons,
        "sample_insufficient_for_regression": False,
    }


def health_gate(
    records: Iterable[dict[str, Any]],
    *,
    baseline: dict[str, float] | None = None,
    min_sample_size: int = 20,
    max_drop_ratio: float = 0.35,
) -> dict[str, Any]:
    metrics = measure_extractor_health(records)
    return evaluate_extractor_health(
        metrics,
        baseline=baseline,
        min_sample_size=min_sample_size,
        max_drop_ratio=max_drop_ratio,
    )
