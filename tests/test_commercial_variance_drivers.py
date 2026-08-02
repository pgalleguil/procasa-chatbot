from pathlib import Path

from analytics.leads_queries import query_variance_drivers
from analytics.leads_service import _build_executive_story


ROOT = Path(__file__).parents[1]


def _row():
    return {
        "current_total": [{"count": 5}],
        "previous_total": [{"count": 3}],
        "current_source": [
            {"segment": "CampaÃ±as Web", "count": 3},
            {"segment": "Portal A", "count": 1},
            {"segment": "Sin fuente", "count": 1},
        ],
        "previous_source": [
            {"segment": "CampaÃ±as Web", "count": 1},
            {"segment": "Portal A", "count": 2},
        ],
        "current_executive": [{"segment": "Ejecutiva Norte", "count": 4}, {"segment": "Sin ejecutivo", "count": 1}],
        "previous_executive": [{"segment": "Ejecutiva Norte", "count": 2}, {"segment": "Sin ejecutivo", "count": 1}],
        "current_commune": [{"segment": "Norte", "count": 3}, {"segment": "Sin comuna", "count": 2}],
        "previous_commune": [{"segment": "Norte", "count": 1}, {"segment": "Sin comuna", "count": 2}],
    }


class Collection:
    def __init__(self, row):
        self.row = row
        self.pipelines = []

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return [self.row]


class DB:
    def __init__(self, row):
        self.leads = Collection(row)

    def __getitem__(self, key):
        return self.leads


def test_variance_drivers_reconcile_all_dimensions_and_deduplicate(monkeypatch):
    db = DB(_row())
    from analytics import leads_queries as queries
    monkeypatch.setattr(queries, "get_db", lambda: db)
    result = query_variance_drivers(
        "2026-07-01", "2026-07-31", "2026-06-01", "2026-06-30",
        filters={"source": "Portal A", "commune": "Norte"},
    )
    assert result["current_total"] == 5
    assert result["previous_total"] == 3
    assert result["total_delta"] == 2
    assert result["comparable_available"] is True
    assert all(result["dimensions"][name]["reconciliation_delta"] == 0 for name in ("source", "executive", "commune"))
    assert result["dimensions"]["source"]["restricted_by_filter"] is True
    assert result["dimensions"]["executive"]["restricted_by_filter"] is False
    assert result["dimensions"]["commune"]["restricted_by_filter"] is True
    assert any(item["label"] == "Sin fuente" for item in result["dimensions"]["source"]["segments"])
    assert len(db.leads.pipelines) == 1
    pipeline_text = str(db.leads.pipelines[0])
    assert pipeline_text.count("$_id") >= 6
    assert pipeline_text.count("prospecto.origen") >= 2


def test_variance_drivers_normalizes_string_null_segments(monkeypatch):
    row = _row()
    row["current_source"] = [{"segment": "None", "count": 2}]
    row["previous_source"] = [{"segment": "null", "count": 1}]
    db = DB(row)
    from analytics import leads_queries as queries
    monkeypatch.setattr(queries, "get_db", lambda: db)
    result = query_variance_drivers("2026-07-01", "2026-07-31", "2026-06-01", "2026-06-30")
    labels = {item["label"] for item in result["dimensions"]["source"]["segments"]}
    assert labels == {"Sin fuente"}


def test_variance_drivers_without_comparison_preserves_unavailable_state(monkeypatch):
    db = DB(_row())
    from analytics import leads_queries as queries
    monkeypatch.setattr(queries, "get_db", lambda: db)
    result = query_variance_drivers("2026-07-01", "2026-07-31", include_comparison=False)
    assert result["comparable_available"] is False
    assert result["previous_total"] is None
    assert result["total_delta"] is None
    assert result["dimensions"]["source"]["reconciliation_delta"] is None


def test_executive_story_reuses_variance_drivers_without_causal_language():
    variance = {
        "current_total": 5, "previous_total": 3, "total_delta": 2, "comparable_available": True,
        "dimensions": {
            "source": {"restricted_by_filter": False, "reconciliation_delta": 0, "segments": [
                {"key": "CampaÃ±as Web", "label": "CampaÃ±as Web", "current": 3, "previous": 1, "delta": 2},
            ]},
            "executive": {"restricted_by_filter": False, "reconciliation_delta": 0, "segments": []},
            "commune": {"restricted_by_filter": False, "reconciliation_delta": 0, "segments": []},
        },
    }
    story = _build_executive_story(
        {"current": {"received": 5}, "previous": {"received": 3}, "variations": {}},
        {"overall_compliance_pct": 80, "lead": {}, "lead_hot": {}},
        {"current": {"label": "julio"}, "previous": {"label": "junio"}}, variance,
    )
    assert story["main_contribution"]["segment"] == "CampaÃ±as Web"
    assert story["main_contribution"]["delta"] == 2
    assert not any(word in str(story).lower() for word in ("causÃ³", "provocÃ³", "generÃ³", "produjo", "responsable de"))


def test_variance_detail_is_closed_and_client_side():
    html = (ROOT / "templates" / "analytics" / "commercial_dashboard.html").read_text(encoding="utf-8")
    assert 'id="executiveStoryContributionDetail" hidden' in html
    assert 'id="executiveStoryContributionToggle"' in html
    assert "function renderVarianceDetail(dimension)" in html
    assert "fetch('/api/analytics/commercial-dashboard" in html
    assert "data-variance-dimension=\"source\"" in html
    assert "slice(0,5)" in html
    assert "ReconciliaciÃ³n no disponible" in html
