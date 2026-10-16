from datetime import datetime, timezone

from pymongo.errors import NetworkTimeout

from analytics import leads_queries as q
from analytics.leads_queries import build_operational_contract


def _lead(lead_id, assigned_at, managed_at=None, temperature="NORMAL", executive="Executive A"):
    return {
        "_id": lead_id,
        "pipeline_stage": "NEW",
        "ejecutivo_asignado": executive,
        "lead_temperature_effective": temperature,
        "lifecycle": {
            "assigned_at": assigned_at,
            "first_valid_management_at": managed_at,
        },
    }


def test_operational_contract_separates_current_stock_from_period_cohort():
    current_docs = [
        _lead("current-1", datetime(2026, 8, 10, 12, tzinfo=timezone.utc)),
        _lead("current-2", datetime(2026, 7, 21, 12, tzinfo=timezone.utc)),
    ]
    period_a = [
        _lead(
            "period-a",
            datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
            datetime(2026, 8, 10, 12, 30, tzinfo=timezone.utc),
            temperature="HOT",
        )
    ]
    period_b = period_a + [
        _lead(
            "period-b",
            datetime(2026, 7, 21, 12, tzinfo=timezone.utc),
            datetime(2026, 7, 21, 13, tzinfo=timezone.utc),
        )
    ]
    now = datetime(2026, 8, 17, 18, tzinfo=timezone.utc)

    result_a = build_operational_contract(
        current_docs, period_a, "2026-08-01", "2026-08-17", now=now
    )
    result_b = build_operational_contract(
        current_docs, period_b, "2026-07-01", "2026-08-17", now=now
    )

    assert result_a["current"] == result_b["current"]
    assert result_a["period"]["assigned"] == 1
    assert result_b["period"]["assigned"] == 2
    assert result_a["period"]["hot_managed"] == 1
    assert result_a["meta"]["sla_hot_minutes"] == 60
    assert result_a["meta"]["sla_normal_minutes"] == 180


def test_activity_timeout_does_not_fail_operational_dashboard(monkeypatch):
    lead = _lead("lead-1", datetime(2026, 8, 10, 12, tzinfo=timezone.utc))
    profile = {}

    def timeout(*_args, **_kwargs):
        raise NetworkTimeout("crm_events read timed out")

    monkeypatch.setattr(q, "_profile_query", timeout)

    result = q._ops_collect_activity_signals(
        {"crm_events": object()},
        [lead],
        [],
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 17, tzinfo=timezone.utc),
        profile=profile,
    )

    assert result == {}
