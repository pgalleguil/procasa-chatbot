from datetime import datetime
from pathlib import Path

from analytics.leads_queries import (
    _executive_summary_snapshot,
    build_sla_risk_payload,
    query_executive_summary,
)
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


def test_executive_summary_contract_has_null_safe_metrics():
    result = _executive_summary_snapshot([], d(27, 14).astimezone(__import__("datetime").timezone.utc), {})
    assert result["received"] == 0
    assert result["assignment_rate_pct"] is None
    assert result["contactability_pct"] is None
    assert result["management_time"]["lead_median_minutes"] is None
    assert result["effective_contact_time"]["coverage_pct"] is None


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
    assert html.index('id="kpiRow"') < html.index('id="insights"')
    assert html.index('id="insights"') < html.index('id="evChart"')
    assert html.index('id="evChart"') < html.index('id="slaBody"')
    assert html.index('id="slaBody"') < html.index('id="funnel"')
    import re
    ids = re.findall(r'\bid=["\']([^"\']+)["\']', html)
    assert len(ids) == len(set(ids))


def test_frontend_productive_order_is_unchanged_and_operational_renderer_is_called():
    html = (Path(__file__).parents[1] / "templates" / "analytics" / "commercial_dashboard.html").read_text(encoding="utf-8")
    assert html.index('id="priorityList"') < html.index('id="kpiRow"') < html.index('id="insights"')
    assert html.index('id="insights"') < html.index('id="evChart"') < html.index('id="slaBody"') < html.index('id="funnel"')
    assert "function formatOperationalMinutes(value)" in html
    assert "function renderOperationalControl(summary)" in html
    assert "renderOperationalControl(D.executive_summary)" in html
    assert "function renderExecutivePreview" not in html
