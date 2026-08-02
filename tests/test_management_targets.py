import json
from pathlib import Path

from analytics.management_targets import build_management_targets, load_target_configuration


def current_inputs(hot=0, lead=0, unassigned=0):
    return (
        {"lead": {"breached": lead}, "lead_hot": {"breached": hot}},
        {"current": {"unassigned": unassigned}, "previous": {"unassigned": 1, "risk": {"lead": {"breached": 1}, "lead_hot": {"breached": 2}}}},
    )


def test_versioned_configuration_contains_only_three_policy_targets():
    config = load_target_configuration()
    assert config["version"] == 1
    assert {item["metric"] for item in config["targets"]} == {"hot_open_breached", "open_breached", "unassigned"}
    assert all(item["source"] == "POLICY" and item["direction"] == "max" and item["target"] == 0 for item in config["targets"])


def test_max_targets_met_and_not_met_with_auditable_gaps():
    sla, summary = current_inputs(hot=0, lead=0, unassigned=0)
    result = build_management_targets(sla, summary, period_end="2026-07-31", comparable_end="2026-06-30")
    assert result["summary"]["configured"] == 3
    assert result["summary"]["met"] == 3
    assert all(item["gap"] == 0 and item["gap_favorable"] is True and item["status"] == "MET" for item in result["items"])

    sla, summary = current_inputs(hot=2, lead=3, unassigned=4)
    result = build_management_targets(sla, summary, period_end="2026-07-31", comparable_end="2026-06-30")
    by_metric = {item["metric"]: item for item in result["items"]}
    assert by_metric["hot_open_breached"]["gap"] == 2
    assert by_metric["hot_open_breached"]["status"] == "NOT_MET"
    assert result["summary"]["main_deviation_metric"] == "open_breached"


def test_min_direction_and_unconfigured_and_not_applicable_states():
    config = {"version": 2, "effective_from": "2026-07-01", "targets": [
        {"metric": "unassigned", "label": "MÃ­nimo", "scope": "all", "direction": "min", "target": 3, "unit": "leads", "source": "BUSINESS"},
        {"metric": "future_metric", "label": "Sin valor", "scope": "all", "direction": "max", "target": None, "unit": "leads", "source": "BUSINESS"},
        {"metric": "not_for_all", "label": "No aplica", "scope": "executive", "direction": "max", "target": 0, "unit": "leads", "source": "BUSINESS"},
    ]}
    result = build_management_targets(*current_inputs(unassigned=2), period_end="2026-07-31", comparable_end=None, config=config)
    by_metric = {item["metric"]: item for item in result["items"]}
    assert by_metric["unassigned"]["gap"] == -1
    assert by_metric["unassigned"]["status"] == "NOT_MET"
    assert by_metric["future_metric"]["status"] == "UNCONFIGURED"
    assert by_metric["not_for_all"]["status"] == "NOT_APPLICABLE"


def test_comparable_value_and_effective_date_are_not_treated_as_previous_meta():
    sla, summary = current_inputs(hot=1, lead=0, unassigned=0)
    result = build_management_targets(sla, summary, period_end="2026-07-31", comparable_end="2026-06-30")
    hot = next(item for item in result["items"] if item["metric"] == "hot_open_breached")
    assert hot["actual"] == 1
    assert hot["target"] == 0
    assert hot["comparable"] is None
    assert hot["comparable_target_valid"] is False
    assert hot["comparable_note"] == "Meta no vigente en el comparable"

    result = build_management_targets(sla, summary, period_end="2026-07-31", comparable_end="2026-06-30", config={"version": 1, "effective_from": "2026-07-01", "targets": load_target_configuration()["targets"]})
    assert all(item["comparable_target_valid"] is False for item in result["items"])
    assert all(item["comparable_note"] == "Meta no vigente en el comparable" for item in result["items"])


def test_target_contract_and_no_non_finite_values():
    result = build_management_targets(*current_inputs(hot=1, lead=2, unassigned=3), period_end="2026-07-31", comparable_end="2026-06-30")
    encoded = json.dumps(result, allow_nan=False)
    assert "NaN" not in encoded and "Infinity" not in encoded
    assert {"metric", "label", "actual", "target", "direction", "unit", "source", "status", "gap", "gap_favorable", "applicable"} <= set(result["items"][0])
