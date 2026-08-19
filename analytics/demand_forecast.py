"""Leakage-safe helpers for the future demand forecast pipeline.

This module is intentionally not connected to the dashboard until a temporal
holdout demonstrates value over a naive baseline.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from math import sqrt
from typing import Any, Iterable, Mapping


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def build_weekly_segment_dataset(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Aggregate lead observations to segment × calendar week without future fields."""
    counts = defaultdict(int)
    for row in rows:
        created = _as_date(row.get("created_at"))
        if created is None:
            continue
        segment = "|".join(str(row.get(key) or "Sin dato") for key in ("operation", "type", "commune", "price_range", "bedrooms"))
        counts[(_week_start(created), segment)] += 1
    return [{"week": week.isoformat(), "segment": segment, "leads": count} for (week, segment), count in sorted(counts.items())]


def chronological_split(dataset: list[Mapping[str, Any]], holdout_weeks: int = 4) -> tuple[list[dict], list[dict]]:
    weeks = sorted({row["week"] for row in dataset})
    if holdout_weeks <= 0 or len(weeks) <= holdout_weeks:
        return list(dataset), []
    cutoff = weeks[-holdout_weeks]
    return ([dict(row) for row in dataset if row["week"] < cutoff], [dict(row) for row in dataset if row["week"] >= cutoff])


def naive_moving_average(train: list[Mapping[str, Any]], test: list[Mapping[str, Any]], window: int = 4) -> list[dict]:
    history = defaultdict(list)
    for row in train:
        history[row["segment"]].append(float(row["leads"]))
    result = []
    for row in test:
        values = history.get(row["segment"], [])
        forecast = sum(values[-window:]) / len(values[-window:]) if values else 0.0
        result.append({"segment": row["segment"], "week": row["week"], "actual": float(row["leads"]), "forecast": forecast})
    return result


def forecast_metrics(predictions: list[Mapping[str, Any]]) -> dict:
    if not predictions:
        return {"n": 0, "mae": None, "rmse": None, "wape": None}
    errors = [abs(row["actual"] - row["forecast"]) for row in predictions]
    squared = [(row["actual"] - row["forecast"]) ** 2 for row in predictions]
    denominator = sum(abs(row["actual"]) for row in predictions)
    return {
        "n": len(predictions),
        "mae": round(sum(errors) / len(errors), 3),
        "rmse": round(sqrt(sum(squared) / len(squared)), 3),
        "wape": round(sum(errors) / denominator * 100, 3) if denominator else None,
    }


def assess_readiness(dataset: list[Mapping[str, Any]], holdout_weeks: int = 4) -> dict:
    weeks = sorted({row["week"] for row in dataset})
    train, test = chronological_split(dataset, holdout_weeks)
    return {
        "available": len(weeks) >= 26 and len({row["week"] for row in train}) >= 16 and bool(test),
        "weeks": len(weeks),
        "train_weeks": len({row["week"] for row in train}),
        "test_weeks": len({row["week"] for row in test}),
        "holdout_weeks": holdout_weeks,
        "reason": "Requiere comparar un modelo candidato contra el baseline y revisar estabilidad por segmento antes de publicar." if weeks else "Sin histórico temporal utilizable.",
    }
