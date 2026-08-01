from datetime import datetime
from pathlib import Path

import pytest

from analytics import leads_queries as q
from chatbot.constants import CHILE_TZ


CANONICAL = {
    "executive": "Paula Morales",
    "source": "Portal A",
    "operation": "Venta",
    "property_type": "Casa",
    "commune": "Ñuñoa",
    "temperature": "HOT",
    "property_code": "P-100",
    "assignment": "1",
    "stage": "CONTACTED",
}


class FakeCollection:
    def __init__(self):
        self.pipelines = []

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return []


class FakeDB:
    def __init__(self):
        self.leads = FakeCollection()

    def __getitem__(self, name):
        assert name == "leads"
        return self.leads


def _has_filter(match, key, value):
    if isinstance(match, dict):
        if match.get(key) == value:
            return True
        return any(_has_filter(v, key, value) for v in match.values())
    if isinstance(match, list):
        return any(_has_filter(v, key, value) for v in match)
    return False


def _period_and_filter_matches(pipeline):
    return [stage["$match"] for stage in pipeline if "$match" in stage]


def test_shared_filter_helper_maps_all_nine_dimensions_and_keeps_created_period():
    start = datetime(2026, 7, 1, tzinfo=CHILE_TZ)
    end = datetime(2026, 8, 1, tzinfo=CHILE_TZ)
    match = q._build_commercial_cohort_match(start, end, {
        **CANONICAL,
        "ejecutivo_asignado": CANONICAL["executive"],
    })
    assert set(match) == {"$and"}
    assert _has_filter(match, "prospecto.origen", "Portal A")
    assert _has_filter(match, "prospecto.operacion", "Venta")
    assert _has_filter(match, "prospecto.tipo", "Casa")
    assert _has_filter(match, "prospecto.comuna", "Ñuñoa")
    assert _has_filter(match, "lead_temperature_effective", "HOT")
    assert _has_filter(match, "prospecto.codigo", "P-100")
    assert _has_filter(match, "ejecutivo_asignado", "Paula Morales")
    assert _has_filter(match, "pipeline_stage", "CONTACTED")
    assert any("_created_normalized" in str(part) for part in match["$and"])


@pytest.mark.parametrize(
    "function_name",
    [
        "query_commercial_funnel",
        "query_sla_risk_panel",
        "query_demand_by_price_ranges",
        "query_commercial_executive_matrix",
        "query_commercial_property_ranking",
    ],
)
def test_all_commercial_components_apply_the_same_filter(monkeypatch, function_name):
    db = FakeDB()
    monkeypatch.setattr(q, "get_db", lambda: db)
    function = getattr(q, function_name)
    function(
        "2026-07-01",
        "2026-07-31",
        filters={"source": "Portal A", "operation": "Venta", "commune": "Ñuñoa"},
    )
    assert db.leads.pipelines
    for pipeline in db.leads.pipelines:
        matches = _period_and_filter_matches(pipeline)
        assert any(_has_filter(match, "prospecto.origen", "Portal A") for match in matches)
        assert any(_has_filter(match, "prospecto.operacion", "Venta") for match in matches)
        assert any(_has_filter(match, "prospecto.comuna", "Ñuñoa") for match in matches)


def test_sources_and_trends_apply_identical_filters_to_current_and_comparable(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(q, "get_db", lambda: db)
    filters = {"executive": "Paula Morales", "temperature": "HOT", "stage": "CONTACTED"}
    q.query_source_performance(
        "2026-07-01", "2026-07-31", comparison_start="2026-06-01",
        comparison_end="2026-06-30", filters=filters,
    )
    q.query_comparative_trends(
        "2026-07-01", "2026-07-31", comparison_start="2026-06-01",
        comparison_end="2026-06-30", filters=filters,
    )
    assert len(db.leads.pipelines) == 4
    for pipeline in db.leads.pipelines:
        matches = _period_and_filter_matches(pipeline)
        assert any(_has_filter(match, "lead_temperature_effective", "HOT") for match in matches)
        assert any(_has_filter(match, "pipeline_stage", "CONTACTED") for match in matches)


def test_empty_filtered_universe_is_coherent_without_mongo_data(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(q, "get_db", lambda: db)
    funnel = q.query_commercial_funnel("2026-07-01", "2026-07-31", {"source": "No existe"})
    assert funnel and all(row["count"] == 0 for row in funnel)
    assert q.query_sla_risk_panel("2026-07-01", "2026-07-31", {"source": "No existe"})["eligible_total"] == 0
    demand = q.query_demand_by_price_ranges("2026-07-01", "2026-07-31", {"source": "No existe"})
    assert demand["price_ranges"] == []


def test_temperature_history_or_events_do_not_create_duplicate_lead_units():
    rows = [{"_id": "lead-1", "temperature_history": [
        {"value": "COLD", "timestamp": "2026-07-01T12:00:00Z"},
        {"value": "HOT", "timestamp": "2026-07-01T13:00:00Z"},
    ]}]
    result = q._summarize_temperature_history(rows, datetime(2026, 7, 2, tzinfo=CHILE_TZ))
    assert result["total"] == 1
    assert result["reconciles"] is True


def test_frontend_persists_filters_and_renders_backend_universe_once():
    html = (Path(__file__).parents[1] / "templates" / "analytics" / "commercial_dashboard.html").read_text(encoding="utf-8")
    for token in (
        "new URLSearchParams()",
        "history[usePush?'pushState':'replaceState']",
        "Universo filtrado:",
        "D.meta?.universe",
        "if(FILTERS.source)p.set('source'",
        "if(FILTERS.exec)p.set('executive'",
        "if(FILTERS.asgn)p.set('assignment'",
    ):
        assert token in html


def test_filter_clear_restores_empty_filter_state():
    html = (Path(__file__).parents[1] / "templates" / "analytics" / "commercial_dashboard.html").read_text(encoding="utf-8")
    assert "function resetFilters()" in html
    assert "FILTERS={period_start:FILTERS.period_start,period_end:FILTERS.period_end,period_preset:FILTERS.period_preset}" in html
    assert "renderChips();loadData()" in html
