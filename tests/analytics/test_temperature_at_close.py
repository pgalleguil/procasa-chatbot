from datetime import datetime, timezone

from analytics.leads_queries import (
    COMMERCIAL_FUNNEL_STAGES,
    _summarize_temperature_history,
    _temperature_at_cutoff,
)


CUTOFF = datetime(2026, 7, 22, tzinfo=timezone.utc)


def lead(lead_id, history=None, current=None):
    return {"_id": lead_id, "temperature_history": history or [], "lead_temperature_effective": current}


def test_zero_coverage_returns_unknown_not_zero():
    result = _summarize_temperature_history([
        lead("a", current="HOT"),
        lead("b", [{"value": "COLD"}], current="COLD"),
    ], CUTOFF)
    assert result["hot"] is None
    assert result["cold"] is None
    assert result["without_history"] == 2
    assert result["history_coverage_pct"] == 0
    assert result["reconciles"] is True


def test_partial_coverage_reconciles_canonical_leads():
    result = _summarize_temperature_history([
        lead("hot", [{"at": "2026-07-20T12:00:00Z", "value": "HOT"}]),
        lead("cold", [{"at": "2026-07-20T12:00:00Z", "value": "COLD"}]),
        lead("unknown", current="HOT"),
        lead("hot", [{"at": "2026-07-20T12:00:00Z", "value": "HOT"}]),
    ], CUTOFF)
    assert result["total"] == 3
    assert (result["hot"], result["cold"], result["without_history"]) == (1, 1, 1)
    assert result["history_coverage_pct"] == 66.7
    assert result["hot"] + result["cold"] + result["without_history"] == result["total"]


def test_complete_coverage_uses_last_value_at_cutoff():
    result = _summarize_temperature_history([
        lead("changed", [
            {"at": "2026-07-19T12:00:00Z", "value": "HOT"},
            {"at": "2026-07-21T12:00:00Z", "value": "COLD"},
        ], current="HOT"),
        lead("hot", [{"at": "2026-07-20T12:00:00Z", "value": "HOT"}], current="COLD"),
    ], CUTOFF)
    assert (result["hot"], result["cold"], result["without_history"]) == (1, 1, 0)
    assert result["history_coverage_pct"] == 100


def test_future_history_is_not_used_retroactively():
    history = [
        {"at": "2026-07-20T12:00:00Z", "value": "COLD"},
        {"at": "2026-07-23T12:00:00Z", "value": "HOT"},
    ]
    assert _temperature_at_cutoff(history, CUTOFF) == "COLD"


def test_temperature_is_not_a_funnel_stage():
    keys = [key for key, _ in COMMERCIAL_FUNNEL_STAGES]
    assert "hot" not in keys
    assert "cold" not in keys
