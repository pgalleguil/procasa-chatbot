"""Versioned, read-only operational target evaluation for the commercial dashboard."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path


CONFIG_PATH = Path(__file__).parents[1] / "config" / "commercial_targets.json"
STATUS_MET = "MET"
STATUS_NOT_MET = "NOT_MET"
STATUS_UNCONFIGURED = "UNCONFIGURED"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"


def load_target_configuration(path: Path = CONFIG_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _as_date(value):
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _is_active(target: dict, period_end: str | None, config: dict) -> bool:
    end = _as_date(period_end)
    effective_from = _as_date(target.get("effective_from") or config.get("effective_from"))
    effective_to = _as_date(target.get("effective_to"))
    if end is None or effective_from is None:
        return False
    return effective_from <= end and (effective_to is None or end <= effective_to)


def _actual(metric: str, sla_risk: dict, executive_summary: dict) -> int:
    lead = (sla_risk or {}).get("lead") or {}
    hot = (sla_risk or {}).get("lead_hot") or {}
    current = (executive_summary or {}).get("current") or {}
    if metric == "hot_open_breached":
        return hot.get("breached", 0) or 0
    if metric == "open_breached":
        return (lead.get("breached", 0) or 0) + (hot.get("breached", 0) or 0)
    if metric == "unassigned":
        return current.get("unassigned", 0) or 0
    return None


def _status(actual, target, direction, applicable):
    if not applicable:
        return STATUS_UNCONFIGURED
    if actual is None or target is None:
        return STATUS_UNCONFIGURED
    if direction == "min":
        return STATUS_MET if actual >= target else STATUS_NOT_MET
    if direction == "max":
        return STATUS_MET if actual <= target else STATUS_NOT_MET
    return STATUS_UNCONFIGURED


def _gap_favorable(gap, direction):
    if gap is None:
        return None
    return gap >= 0 if direction == "min" else gap <= 0


def build_management_targets(
    sla_risk: dict,
    executive_summary: dict,
    *,
    period_end: str | None,
    comparable_end: str | None = None,
    config: dict | None = None,
) -> dict:
    config = config or load_target_configuration()
    items = []
    for configured in config.get("targets", []):
        metric = configured.get("metric", "")
        direction = configured.get("direction")
        target = configured.get("target")
        scope_applicable = configured.get("scope", "all") == "all"
        applicable = scope_applicable and _is_active(configured, period_end, config)
        actual = _actual(metric, sla_risk, executive_summary)
        gap = actual - target if applicable and actual is not None and target is not None else None
        status = _status(actual, target, direction, applicable) if scope_applicable else STATUS_NOT_APPLICABLE

        previous_summary = (executive_summary or {}).get("previous") or {}
        previous_risk = previous_summary.get("risk") or {}
        previous_sla = {"lead": previous_risk.get("lead") or {}, "lead_hot": previous_risk.get("lead_hot") or {}}
        previous_actual = _actual(metric, previous_sla, {"current": previous_summary}) if comparable_end and _is_active(configured, comparable_end, config) else None
        comparable_valid = bool(comparable_end and _is_active(configured, comparable_end, config))
        comparable_note = None if comparable_valid else ("Meta no vigente en el comparable" if comparable_end else "Sin periodo comparable")
        items.append({
            "metric": metric,
            "label": configured.get("label", metric),
            "actual": actual,
            "target": target if applicable else None,
            "direction": direction,
            "unit": configured.get("unit", "leads"),
            "source": configured.get("source", "POLICY"),
            "source_label": "Política operacional" if configured.get("source") == "POLICY" else "Meta de negocio",
            "status": status,
            "gap": gap,
            "gap_favorable": _gap_favorable(gap, direction),
            "applicable": applicable,
            "comparable": previous_actual,
            "comparable_target_valid": comparable_valid,
            "comparable_note": comparable_note,
        })

    not_met = [item for item in items if item["status"] == STATUS_NOT_MET]
    not_met.sort(key=lambda item: (-(abs(item["gap"] or 0)), item["metric"]))
    summary = {
        "configured": sum(item["applicable"] for item in items),
        "met": sum(item["status"] == STATUS_MET for item in items),
        "not_met": len(not_met),
        "unconfigured": sum(item["status"] == STATUS_UNCONFIGURED for item in items),
        "not_applicable": sum(item["status"] == STATUS_NOT_APPLICABLE for item in items),
        "main_deviation_metric": not_met[0]["metric"] if not_met else None,
    }
    return {
        "version": config.get("version", 1),
        "effective_from": config.get("effective_from", ""),
        "items": items,
        "summary": summary,
    }
