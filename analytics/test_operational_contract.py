from datetime import datetime

from chatbot.constants import CHILE_TZ
from analytics.leads_queries import build_operational_contract


def local_time(hour: int, minute: int = 0):
    return CHILE_TZ.localize(datetime(2026, 8, 17, hour, minute))


def lead(number, assigned=None, managed=None, temperature="NORMAL", executive="Ana"):
    return {
        "_id": f"lead-{number}",
        "prospecto": {"nombre": f"Lead {number}", "codigo": f"P-{number}"},
        "ejecutivo_asignado": executive,
        "lead_temperature_effective": temperature,
        "lifecycle": {"assigned_at": assigned, "first_valid_management_at": managed},
        "pipeline_stage": "NEW",
    }


def test_current_boundaries_and_mutually_exclusive_priorities():
    now = local_time(12, 1)
    current_docs = [
        lead(44, local_time(11, 17), temperature="HOT"),
        lead(45, local_time(11, 16), temperature="HOT"),
        lead(59, local_time(11, 2), temperature="HOT"),
        lead(60, local_time(11, 1), temperature="HOT"),
        lead(61, local_time(11), executive=None),
    ]
    payload = build_operational_contract(current_docs, [], "2026-08-01", "2026-08-17", now=now)
    assert payload["current"]["active_assigned"] == 4
    assert payload["current"]["unassigned"] == 1
    assert payload["current"]["hot_near_due"] == 2
    assert payload["current"]["hot_overdue"] == 1
    assert payload["current"]["open_overdue"] == 1
    codes = {case["priority_code"] for case in payload["intervention_cases"]}
    assert "hot_near_due" in codes and "hot_open_overdue" in codes


def test_period_separates_managed_late_from_current_open_overdue():
    now = local_time(12, 1)
    current_docs = [lead(1, local_time(9), managed=local_time(12), temperature="HOT")]
    period_docs = [
        lead(2, local_time(10), managed=local_time(10, 59), temperature="HOT"),
        lead(3, local_time(11), managed=local_time(12), temperature="HOT"),
        lead(4, local_time(9), managed=local_time(11, 59), temperature="NORMAL"),
        lead(5, local_time(9), managed=local_time(12), temperature="NORMAL"),
    ]
    payload = build_operational_contract(current_docs, period_docs, "2026-08-01", "2026-08-17", now=now)
    assert payload["current"]["open_overdue"] == 0
    assert payload["period"]["assigned"] == 4
    assert payload["period"]["managed"] == 4
    assert payload["period"]["managed_within_sla"] == 2
    assert payload["period"]["managed_late"] == 2
    assert payload["period"]["hot_late"] == 1
    assert payload["period"]["normal_late"] == 1
    assert payload["period"]["p50_hot"] is not None
    assert payload["period"]["p90_normal"] is not None


def test_current_reconciliation_is_explicit():
    payload = build_operational_contract(
        [lead(1, local_time(10)), lead(2, local_time(10), executive=None)],
        [], "2026-08-01", "2026-08-17", now=local_time(12)
    )
    reconciliation = payload["current_reconciliation"]
    assert reconciliation["active_total"] == 2
    assert reconciliation["active_assigned_plus_unassigned"] == 2
    assert reconciliation["ok"] is True
