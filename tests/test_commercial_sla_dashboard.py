from datetime import datetime, timezone

import pytest

from analytics.leads_queries import build_sla_risk_payload
from chatbot.constants import CHILE_TZ
from chatbot.crm_metrics import calculate_sla


def cl_dt(day, hour, minute=0):
    return CHILE_TZ.localize(datetime(2026, 7, day, hour, minute))


def lead(assigned, managed=None, temperature="COLD", history=None):
    return {
        "lifecycle": {
            "assigned_at": assigned,
            "first_valid_management_at": managed,
        },
        "lead_temperature_effective": temperature,
        "temperature_history": history or [],
    }


def payload(rows, now):
    return build_sla_risk_payload(
        rows,
        now=now,
        cutover_at="2026-01-01T00:00:00Z",
    )


@pytest.mark.parametrize(
    ("minutes", "state"),
    [(119, "ACTIVE_NORMAL"), (120, "ATTENTION"), (150, "NEAR_BREACH"), (180, "BREACHED")],
)
def test_lead_thresholds(minutes, state):
    assigned = cl_dt(27, 9)
    now = cl_dt(27, 9 + minutes // 60, minutes % 60)
    result = calculate_sla(assigned_at=assigned, now=now, temperature="COLD")
    assert result["canonical_state"] == state


@pytest.mark.parametrize(
    ("minutes", "state"),
    [(29, "ACTIVE_NORMAL"), (30, "ATTENTION"), (45, "NEAR_BREACH"), (60, "BREACHED")],
)
def test_hot_thresholds(minutes, state):
    assigned = cl_dt(27, 9)
    now = cl_dt(27, 9 + minutes // 60, minutes % 60)
    result = calculate_sla(
        assigned_at=assigned,
        now=now,
        temperature="HOT",
        hot_started_at=assigned,
        require_hot_start=True,
    )
    assert result["canonical_state"] == state


def test_night_pause():
    result = calculate_sla(assigned_at=cl_dt(27, 18), now=cl_dt(28, 10), temperature="COLD")
    assert result["minutes"] == 120


def test_weekend_pause_and_friday_continuation():
    monday = CHILE_TZ.localize(datetime(2026, 8, 3, 10))
    result = calculate_sla(assigned_at=cl_dt(31, 18), now=monday, temperature="COLD")
    assert result["minutes"] == 120


def test_no_assignment_is_not_started():
    result = calculate_sla(assigned_at=None, now=cl_dt(27, 10), temperature="COLD")
    assert result["canonical_state"] == "SLA_NOT_STARTED"


def test_historical_is_excluded():
    historical_assignment = CHILE_TZ.localize(datetime(2025, 12, 31, 9))
    result = payload([lead(historical_assignment)], cl_dt(27, 10))
    assert result["excluded"]["historical"] == 1
    assert result["overall_denominator"] == 0


def test_managed_outside_sla_is_in_denominator():
    result = payload([lead(cl_dt(27, 9), managed=cl_dt(27, 12))], cl_dt(27, 13))
    assert result["lead"]["managed_outside"] == 1
    assert result["overall_denominator"] == 1
    assert result["overall_numerator"] == 0


def test_click_or_stage_change_does_not_stop_sla():
    row = lead(cl_dt(27, 9), temperature="COLD")
    row["crm_events"] = [{"type": "CLICK_WHATSAPP_LEAD"}]
    row["pipeline_stage"] = "CONTACTED"
    result = payload([row], cl_dt(27, 12))
    assert result["lead"]["breached"] == 1


def test_hot_conversion_uses_history_timestamp():
    row = lead(
        cl_dt(27, 9),
        temperature="HOT",
        history=[{"value": "HOT", "timestamp": cl_dt(27, 10).isoformat()}],
    )
    result = payload([row], cl_dt(27, 10, 30))
    assert result["lead_hot"]["attention"] == 1


def test_current_hot_without_timestamp_is_insufficient():
    result = payload([lead(cl_dt(27, 9), temperature="HOT")], cl_dt(27, 12))
    assert result["excluded"]["insufficient_data"] == 1
    assert result["lead_hot"]["eligible"] == 0


def test_exclusions_are_disjoint_and_not_eligible():
    rows = [
        {"lifecycle": {}, "lead_temperature_effective": "COLD"},
        lead(cl_dt(27, 9), temperature="HOT"),
        lead(CHILE_TZ.localize(datetime(2025, 12, 31, 9))),
    ]
    result = payload(rows, cl_dt(27, 12))
    assert result["excluded"] == {"historical": 1, "not_assigned": 1, "insufficient_data": 1}
    assert result["lead"]["eligible"] + result["lead_hot"]["eligible"] == 0


def test_lead_managed_before_hot_conversion_stays_lead():
    row = lead(
        cl_dt(27, 9), managed=cl_dt(27, 10), temperature="HOT",
        history=[{"value": "HOT", "timestamp": cl_dt(27, 11).isoformat()}],
    )
    result = payload([row], cl_dt(27, 12))
    assert result["lead"]["managed_within"] == 1
    assert result["lead_hot"]["eligible"] == 0


def test_hot_before_management_uses_hot_clock():
    row = lead(
        cl_dt(27, 9), managed=cl_dt(27, 10), temperature="HOT",
        history=[{"value": "HOT", "timestamp": cl_dt(27, 9, 30).isoformat()}],
    )
    result = payload([row], cl_dt(27, 12))
    assert result["lead_hot"]["managed_within"] == 1
    assert result["lead"]["eligible"] == 0


def test_lead_breach_before_hot_cannot_become_hot_compliance():
    row = lead(
        cl_dt(27, 9), managed=cl_dt(27, 12), temperature="HOT",
        history=[{"value": "HOT", "timestamp": cl_dt(27, 11).isoformat()}],
    )
    result = payload([row], cl_dt(27, 13))
    assert result["lead_hot"]["managed_outside"] == 1
    assert result["overall_numerator"] == 0


def test_hot_after_management_does_not_reclassify_closed_lead():
    row = lead(
        cl_dt(27, 9), managed=cl_dt(27, 10), temperature="COLD",
        history=[{"value": "HOT", "timestamp": cl_dt(27, 11).isoformat()}],
    )
    result = payload([row], cl_dt(27, 12))
    assert result["lead"]["managed_within"] == 1
    assert result["lead_hot"]["eligible"] == 0


def test_reconciliation_invariants_hold_for_each_profile_and_global_total():
    rows = [
        lead(cl_dt(27, 9), managed=cl_dt(27, 10)),
        lead(cl_dt(27, 9), managed=cl_dt(27, 13)),
        lead(cl_dt(27, 9)),
        lead(cl_dt(27, 9), temperature="HOT", history=[{"value": "HOT", "timestamp": cl_dt(27, 9).isoformat()}]),
        lead(cl_dt(27, 9), temperature="HOT", history=[{"value": "HOT", "timestamp": cl_dt(27, 9).isoformat()}], managed=cl_dt(27, 10)),
    ]
    result = payload(rows, cl_dt(27, 12))
    for bucket in (result["lead"], result["lead_hot"]):
        assert bucket["eligible"] == sum(bucket[key] for key in (
            "open_normal", "attention", "near_breach", "breached", "managed_within", "managed_outside"
        ))
    assert result["overall_numerator"] == result["lead"]["managed_within"] + result["lead_hot"]["managed_within"]
    assert result["overall_denominator"] == (
        result["lead"]["managed_within"] + result["lead"]["managed_outside"] + result["lead"]["breached"]
        + result["lead_hot"]["managed_within"] + result["lead_hot"]["managed_outside"] + result["lead_hot"]["breached"]
    )


def test_open_not_yet_breached_is_excluded_from_compliance_denominator():
    rows = [
        lead(cl_dt(27, 9), managed=cl_dt(27, 10)),
        lead(cl_dt(27, 9)),
    ]
    result = payload(rows, cl_dt(27, 11))
    assert result["overall_numerator"] == 1
    assert result["overall_denominator"] == 1


def test_median_and_p90_are_separated_by_profile():
    rows = [
        lead(cl_dt(27, 9), managed=cl_dt(27, 10)),
        lead(cl_dt(27, 9), managed=cl_dt(27, 11)),
        lead(cl_dt(27, 9), managed=cl_dt(27, 12)),
        lead(cl_dt(27, 9), managed=cl_dt(27, 9, 30), temperature="HOT", history=[{"value": "HOT", "timestamp": cl_dt(27, 9).isoformat()}]),
        lead(cl_dt(27, 9), managed=cl_dt(27, 10), temperature="HOT", history=[{"value": "HOT", "timestamp": cl_dt(27, 9).isoformat()}]),
    ]
    result = payload(rows, cl_dt(27, 13))
    assert result["lead"]["median_minutes"] == 120
    assert result["lead_hot"]["median_minutes"] == 45
    assert result["lead"]["p90_minutes"] is not None
    assert result["lead_hot"]["p90_minutes"] is not None


def test_contract_contains_sla_profiles_and_exclusions():
    result = payload([], datetime(2026, 7, 27, 13, tzinfo=timezone.utc))
    assert {"policy_timezone", "business_hours", "policy_cutover_at"} <= result.keys()
    assert {"lead", "lead_hot", "excluded"} <= result.keys()
    assert {"overall_compliance_pct", "overall_numerator", "overall_denominator"} <= result.keys()
    for bucket in (result["lead"], result["lead_hot"]):
        assert {"eligible", "open_normal", "attention", "near_breach", "breached", "managed_within", "managed_outside", "median_minutes", "p90_minutes"} <= bucket.keys()
