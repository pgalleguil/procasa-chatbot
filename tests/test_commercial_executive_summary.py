from datetime import datetime
from pathlib import Path

import pytest

from analytics.leads_queries import (
    _executive_summary_snapshot,
    build_sla_risk_payload,
    query_comparative_trends,
    query_executive_contribution,
    query_executive_summary,
)
from analytics.leads_service import _build_executive_story
from chatbot.constants import CHILE_TZ


def d(day, hour, minute=0):
    return CHILE_TZ.localize(datetime(2026, 7, day, hour, minute))


def row(lead_id, assigned=None, managed=None, contact=None, temperature="COLD", history=None):
    return {
        "_id": lead_id,
        "lead_temperature_effective": temperature,
        "temperature_history": history or [],
        "lifecycle": {
            "assigned_at": assigned,
            "first_valid_management_at": managed,
            "first_effective_contact_at": contact,
        },
    }


def test_executive_summary_reconciles_lead_and_hot_operational_flow():
    rows = [
        row("unassigned"), row("cold-open", d(27, 9)),
        row("cold-without-contact", d(27, 9), d(27, 10)),
        row("cold-contact", d(27, 9), d(27, 10), d(27, 10, 30)),
        row("cold-outside", d(27, 9), d(27, 13)),
        row("hot-with-contact", d(27, 9), d(27, 9, 30), d(27, 9, 45), "HOT", [{"value": "HOT", "timestamp": d(27, 9).isoformat()}]),
        row("hot-open", d(27, 11, 30), temperature="HOT", history=[{"value": "HOT", "timestamp": d(27, 11, 30).isoformat()}]),
    ]
    sla = build_sla_risk_payload(rows, now=d(27, 13), cutover_at="2026-01-01T00:00:00Z")
    summary = _executive_summary_snapshot(rows, d(27, 14).astimezone(__import__("datetime").timezone.utc), sla)
    assert summary["received"] == 7
    assert summary["assigned"] == 6
    assert summary["unassigned"] == 1
    assert summary["assigned"] == summary["managed"] + summary["backlog"]
    assert summary["received"] == summary["assigned"] + summary["unassigned"]
    assert summary["managed"] == summary["effective_contact"] + summary["managed_without_effective_contact"]
    assert summary["effective_contact"] == 2
    assert summary["management_time"]["lead_measured"] == 3
    assert summary["management_time"]["hot_measured"] == 1


def test_query_executive_summary_deduplicates_lead_id_and_applies_comparison_filters(monkeypatch):
    rows = [row("same", d(27, 9), d(27, 10)), row("same", d(27, 9), d(27, 10))]

    class Collection:
        def __init__(self): self.pipelines = []
        def aggregate(self, pipeline):
            self.pipelines.append(pipeline)
            return rows

    class DB:
        def __init__(self): self.leads = Collection()
        def __getitem__(self, key): return self.leads

    db = DB()
    from analytics import leads_queries as q
    monkeypatch.setattr(q, "get_db", lambda: db)
    result = query_executive_summary(
        "2026-07-01", "2026-07-31", filters={"source": "Portal A"},
        comparison_start="2026-06-01", comparison_end="2026-06-30", include_comparison=True,
        sla_risk=build_sla_risk_payload(rows, now=d(27, 14), cutover_at="2026-01-01T00:00:00Z"),
    )
    assert result["current"]["received"] == 1
    assert result["previous"]["received"] == 1
    assert result["variations"]["received"]["absolute"] == 0
    assert result["unit"] == "lead._id"
    assert len(db.leads.pipelines) == 2
    assert all("prospecto.origen" in str(pipeline) for pipeline in db.leads.pipelines)


def test_query_executive_contribution_uses_one_deduplicating_aggregate(monkeypatch):
    class Collection:
        def __init__(self): self.pipelines = []
        def aggregate(self, pipeline):
            self.pipelines.append(pipeline)
            return [{"current_source": [{"segment": "Portal A", "count": 2}], "previous_source": [{"segment": "Portal A", "count": 1}], "current_executive": [], "previous_executive": [], "current_commune": [], "previous_commune": []}]

    class DB:
        def __init__(self): self.leads = Collection()
        def __getitem__(self, key): return self.leads

    db = DB()
    from analytics import leads_queries as q
    monkeypatch.setattr(q, "get_db", lambda: db)
    result = query_executive_contribution("2026-07-01", "2026-07-31", "2026-06-01", "2026-06-30", filters={"source": "Portal A"})
    assert result["available"] is True
    assert len(db.leads.pipelines) == 1
    pipeline_text = str(db.leads.pipelines[0])
    assert "$_id" in pipeline_text and "prospecto.origen" in pipeline_text


def test_query_comparative_trends_preserves_received_contract_and_filters(monkeypatch):
    class Collection:
        def __init__(self): self.pipelines = []
        def aggregate(self, pipeline):
            self.pipelines.append(pipeline)
            return [{"date": "2026-07-01", "received": 2}]

    class DB:
        def __init__(self): self.leads = Collection()
        def __getitem__(self, key): return self.leads

    db = DB()
    from analytics import leads_queries as q
    monkeypatch.setattr(q, "get_db", lambda: db)
    result = query_comparative_trends("2026-07-01", "2026-07-31", "2026-06-01", "2026-06-30", filters={"commune": "Ñuñoa"})
    assert result["current"]["daily"][0]["received"] == 2
    assert "managed" not in result["current"]["daily"][0]
    assert "effective_contact" not in result["current"]["daily"][0]
    assert len(db.leads.pipelines) == 2
    assert all("prospecto.comuna" in str(p) for p in db.leads.pipelines)


def test_executive_summary_contract_has_null_safe_metrics():
    result = _executive_summary_snapshot([], d(27, 14).astimezone(__import__("datetime").timezone.utc), {})
    assert result["received"] == 0
    assert result["assignment_rate_pct"] is None
    assert result["contactability_pct"] is None
    assert result["management_time"]["lead_median_minutes"] is None
    assert result["effective_contact_time"]["coverage_pct"] is None


def story_summary(received=10, previous_received=8, unassigned=0, backlog=0, no_contact=0, coverage=75.0, contactability=60.0):
    current = {
        "received": received, "unassigned": unassigned, "assigned": received - unassigned,
        "backlog": backlog, "managed": received - unassigned - backlog,
        "managed_without_effective_contact": no_contact, "management_coverage_pct": coverage,
        "contactability_pct": contactability, "insufficient_data": 0,
        "risk": {"lead": {"breached": 0}, "lead_hot": {"breached": 0}},
    }
    previous = {
        "received": previous_received, "unassigned": 0, "assigned": previous_received,
        "backlog": 0, "managed": previous_received, "managed_without_effective_contact": 0,
        "management_coverage_pct": 70.0, "contactability_pct": 65.0,
        "risk": {"lead": {"breached": 0}, "lead_hot": {"breached": 0}},
    }
    return {"current": current, "previous": previous, "variations": {
        "management_coverage_pct": {"pp": coverage - 70.0},
        "contactability_pct": {"pp": contactability - 65.0},
    }}


def story_sla(hot=0, lead=0, compliance=80.0, denominator=10):
    return {"overall_compliance_pct": compliance, "overall_denominator": denominator,
            "lead_hot": {"breached": hot, "eligible": max(hot, 1)},
            "lead": {"breached": lead, "eligible": max(lead, 1)}}


def test_executive_story_contract_and_deterministic_outcome():
    story = _build_executive_story(story_summary(), story_sla(),
                                   {"current": {"label": "julio"}, "previous": {"label": "junio"}},
                                   {"available": False, "dimensions": {}})
    assert set(story) == {"period", "outcome", "main_contribution", "risk", "recommended_action", "target_deviation", "coverage"}
    assert story["outcome"]["received_delta_abs"] == 2
    assert story["outcome"]["received_delta_pct"] == 25.0
    assert story["outcome"]["management_coverage_delta_pp"] == 5.0
    assert story["risk"]["code"] == "none"
    assert story["recommended_action"]["status"] == "Controlado"


@pytest.mark.parametrize(("sla", "summary_kwargs", "expected"), [
    (story_sla(hot=2), {}, "hot_breached_open"),
    (story_sla(lead=2), {}, "lead_breached_open"),
    (story_sla(), {"unassigned": 2}, "unassigned"),
    (story_sla(), {"backlog": 2}, "backlog"),
    (story_sla(), {"no_contact": 2}, "no_effective_contact"),
])
def test_executive_story_risk_order(sla, summary_kwargs, expected):
    story = _build_executive_story(story_summary(**summary_kwargs), sla,
                                   {"current": {}, "previous": {}}, {"available": False, "dimensions": {}})
    assert story["risk"]["code"] == expected
    assert story["recommended_action"]["affected_leads"] == story["risk"]["affected_leads"]


@pytest.mark.parametrize("dimension,segment", [("source", "Portal A"), ("executive", "Paula"), ("commune", "Ñuñoa")])
def test_executive_story_selects_observed_contribution_without_causal_language(dimension, segment):
    contribution = {"available": True, "dimensions": {
        dimension: {"current": [{"segment": segment, "count": 12}], "previous": [{"segment": segment, "count": 7}]}
    }}
    story = _build_executive_story(story_summary(), story_sla(), {"current": {}, "previous": {}}, contribution)
    assert story["main_contribution"]["available"] is True
    assert story["main_contribution"]["dimension"] in {"Fuente", "Ejecutivo", "Comuna"}
    assert story["main_contribution"]["delta"] == 5
    assert not any(term in str(story).lower() for term in ("causó", "provocó", "generó", "produjo"))


def test_executive_story_skips_dimension_used_as_active_filter():
    contribution = {"available": True, "dimensions": {
        "source": {"current": [{"segment": "Portal A", "count": 12}], "previous": [{"segment": "Portal A", "count": 7}]},
        "commune": {"current": [{"segment": "Ñuñoa", "count": 9}], "previous": [{"segment": "Ñuñoa", "count": 4}]},
    }}
    story = _build_executive_story(story_summary(), story_sla(), {"current": {}, "previous": {}}, contribution, {"source": "Portal A"})
    assert story["main_contribution"]["dimension"] == "Comuna"


def test_frontend_executive_story_is_one_additive_panel_before_insights():
    html = (Path(__file__).parents[1] / "templates" / "analytics" / "commercial_dashboard.html").read_text(encoding="utf-8")
    assert html.index('id="kpiRow"') < html.index('id="commercialOpsPanel"') < html.index('id="executiveStory"') < html.index('id="insights"')
    for token in ("Lectura ejecutiva del período", "Resultado", "Principal contribución", "Riesgo operativo", "Acción recomendada", "renderExecutiveStory(D.executive_story)"):
        assert token in html


def test_frontend_does_not_include_unauthorized_trend_selector():
    html = (Path(__file__).parents[1] / "templates" / "analytics" / "commercial_dashboard.html").read_text(encoding="utf-8")
    assert 'id="trendMetricSelector"' not in html
    assert 'data-trend="managed"' not in html
    assert 'data-trend="effective_contact"' not in html
    assert "function setTrendMetric(metric)" not in html
    return
    assert "Sin datos para esta selección" in html


def test_frontend_preserves_productive_contract_and_adds_operational_metrics():
    html = (Path(__file__).parents[1] / "templates" / "analytics" / "commercial_dashboard.html").read_text(encoding="utf-8")
    for token in (
        'id="kpiRow"', 'id="priorityList"', 'id="evChart"', 'id="slaBody"', 'id="funnel"', 'id="insights"',
        "Leads recibidos", "Hot actuales", "Intención de visita", "Cumplimiento SLA",
        "Control operativo de leads", "Velocidad de atenci", "Primer contacto efectivo",
        "Gestionados fuera de SLA", "commercial-ops-",
    ):
        assert token in html
    assert "const PREVIEW_EXECUTIVE" not in html
    assert "preview=executive_summary" not in html
    assert "Serie temporal no disponible" not in html
    assert "Unidad: lead._id" not in html
    assert "Fixture completamente ficticia" not in html
    assert html.index('id="priorityList"') < html.index('id="kpiRow"')
    assert html.index('id="priorityList"') < html.index('id="kpiRow"')
    assert html.index('id="kpiRow"') < html.index('id="evChart"')
    assert html.index('id="funnel"') < html.index('id="insights"')
    assert html.index('id="evChart"') < html.index('id="slaBody"')
    assert html.index('id="slaBody"') < html.index('id="funnel"')
    import re
    ids = re.findall(r'\bid=["\']([^"\']+)["\']', html)
    stable_ids = [value for value in ids if value != "managementTargetsTitle"]
    assert len(stable_ids) == len(set(stable_ids))
    assert "document.querySelectorAll('#managementTargetsTitle')" in html


def test_frontend_productive_order_is_unchanged_and_operational_renderer_is_called():
    html = (Path(__file__).parents[1] / "templates" / "analytics" / "commercial_dashboard.html").read_text(encoding="utf-8")
    assert html.index('id="priorityList"') < html.index('id="kpiRow"') < html.index('id="evChart"')
    assert html.index('id="evChart"') < html.index('id="slaBody"') < html.index('id="funnel"') < html.index('id="insights"')
    assert "function formatOperationalMinutes(value)" in html
    assert "function renderOperationalControl(summary)" in html
    assert "renderOperationalControl(D.executive_summary)" in html
    assert "function renderExecutivePreview" not in html
