"""Compare canonical commercial metrics between two dashboard JSON payloads."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests


PATHS = (
    "meta.period.current.start",
    "meta.period.current.end",
    "meta.period.previous.start",
    "meta.period.previous.end",
    "meta.period.timezone",
    "meta.unit",
    "kpis.leads_received.value",
    "kpis.leads_received.previous",
    "kpis.leads_hot_current.value",
    "kpis.visit_intent.value",
    "kpis.visits_scheduled.value",
    "kpis.closed_won.value",
    "kpis.temperature_at_close.hot",
    "kpis.temperature_at_close.cold",
    "kpis.temperature_at_close.history_coverage_pct",
    "sla_risk.within_sla_pct",
)


def _load(location: str) -> dict:
    if location.startswith(("http://", "https://")):
        response = requests.get(location, timeout=60)
        response.raise_for_status()
        return response.json()
    return json.loads(Path(location).read_text(encoding="utf-8"))


def _get(payload: dict, path: str):
    value = payload
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def compare(before: dict, after: dict) -> list[dict]:
    return [
        {"metric": path, "before": _get(before, path), "after": _get(after, path), "equal": _get(before, path) == _get(after, path)}
        for path in PATHS
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = compare(_load(args.before), _load(args.after))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0 if all(row["equal"] for row in result) else 1


if __name__ == "__main__":
    raise SystemExit(main())
