import json
from datetime import date
from pathlib import Path

from analytics.leads_service import _build_executive_kpis, _executive_target_info


def _payload():
    return _build_executive_kpis(
        {"leads_received": {"value": 142, "previous": 120}},
        [
            {"key": "received", "count": 142},
            {"key": "visit_scheduled", "count": 28},
        ],
        {"opportunity": [
            {"code": "A", "avg_price_uf": 1000, "operation": "Venta", "leads": 3},
            {"code": "A", "avg_price_uf": 1000, "operation": "Venta", "leads": 3},
            {"code": "B", "avg_price_uf": 500, "operation": "Arriendo", "leads": 2},
        ]},
        {"lead": {"median_minutes": 42, "p90_minutes": 110, "breached": 2}, "lead_hot": {"median_minutes": 12, "p90_minutes": 25, "breached": 1}},
        {"summary": {"open_breached": 3, "breached_with_activity_without_result": 1, "breached_without_activity": 2, "registration_gap_rate": 33.3}, "by_executive": []},
        {"current": {"daily": [{"date": "2026-08-01", "received": 10}]}},
        {}, "2026-08-01", "2026-08-03",
    )


def test_contract_has_exactly_five_cards_and_uses_business_fields():
    payload = _payload()
    assert list(payload) == ["demand_pace", "visit_conversion", "pipeline_valuation", "sla_velocity", "registration_discipline"]
    assert payload["demand_pace"]["target"] == 200 * 3 / 31
    assert payload["visit_conversion"]["visited_or_scheduled"] == 28
    assert payload["pipeline_valuation"]["property_count"] == 2
    assert payload["pipeline_valuation"]["net_commission_uf"] == 60.0
    assert payload["pipeline_valuation"]["gross_commission_uf"] == 71.4
    assert payload["sla_velocity"]["lead"]["median_minutes"] == 42
    assert "average" not in json.dumps(payload["sla_velocity"])


def test_global_target_is_calendar_prorated_and_segmented_filters_disable_it():
    full = _executive_target_info("2026-08-01", "2026-08-31", {}, today=date(2026, 8, 3))
    partial = _executive_target_info("2026-08-01", "2026-08-03", {}, today=date(2026, 8, 3))
    filtered = _executive_target_info("2026-08-01", "2026-08-03", {"source": "Portal Inmobiliario"}, today=date(2026, 8, 3))
    assert full["target"] == 200
    assert partial["target"] == 200 * 3 / 31
    assert filtered["target"] is None
    assert filtered["segmented"] is True


def test_template_and_css_define_five_card_contract():
    template = Path("templates/analytics/commercial_dashboard.html").read_text(encoding="utf-8")
    css = Path("static/css/commercial_dashboard_v2.css").read_text(encoding="utf-8")
    for label in ("DEMANDA, META & RITMO", "EFECTIVIDAD & CONVERSIÓN", "PIPELINE & VALORIZACIÓN UF", "SLA & VELOCIDAD DE RESPUESTA", "DISCIPLINA DE REGISTRO"):
        assert label in template
    assert "repeat(5, minmax(0, 1fr))" in css
    assert "executive_kpis" in template
