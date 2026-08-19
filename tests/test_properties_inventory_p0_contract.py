from pathlib import Path
import sys
import types

_chatbot_package = types.ModuleType("chatbot")
_chatbot_package.__path__ = [str(Path(__file__).resolve().parents[1] / "chatbot")]
sys.modules.setdefault("chatbot", _chatbot_package)

from analytics.leads_queries import (
    build_demand_capture_contract,
    build_capture_simulation_contract,
    build_properties_inventory_contract,
    _demand_capture_recent_band,
    _demand_capture_profile,
    _demand_capture_geo_key,
    _demand_capture_zone_rm,
    _inventory_publications,
)
from analytics.demand_forecast import (
    assess_readiness,
    build_weekly_segment_dataset,
    chronological_split,
    forecast_metrics,
    naive_moving_average,
)
from review_fixtures import territorial_review_payload


def prop(code, *, venta=True, arriendo=False, tipo="Casa", comuna="Santiago", responsable="Ana", leads=0):
    return {
        "codigo": code,
        "tipo_propiedad": tipo,
        "comuna": comuna,
        "tipo_operacion": {"venta": venta, "arriendo": arriendo, "precio_venta": {"uf": 5000}},
        "estado": {"ejecutivo": responsable},
        "publicaciones": {"procasa": {"publicaciones": {"V": {"url": "https://procasa.test/1", "codigo": code, "publicada": True}}}},
    }


def test_stock_is_unique_by_canonical_code_and_reconciles_demand():
    payload = build_properties_inventory_contract([prop("1"), prop(1), prop("2")], {"1": 2}, "2026-07-20", "2026-08-19")
    assert payload["inventory"] == {"active": 2, "with_demand": 1, "without_demand": 1, "coverage_pct": 50.0, "reconciliation": True}
    assert payload["data_quality"]["duplicate_source_docs"] == 1


def test_operations_and_composition_support_mixed_operation():
    payload = build_properties_inventory_contract([prop("1", venta=True, arriendo=True), prop("2", venta=False, arriendo=True)])
    assert [item["label"] for item in payload["composition"]["operation"]] == ["Arriendo", "Venta + Arriendo"]


def test_filters_are_combined_and_period_does_not_change_stock():
    docs = [prop("1", tipo="Casa", comuna="Santiago"), prop("2", tipo="Departamento", comuna="Ñuñoa")]
    payload = build_properties_inventory_contract(docs, {"1": 1}, "2026-07-20", "2026-08-19", {"property_type": "Casa", "commune": "Santiago"})
    assert payload["inventory"]["active"] == 1
    assert payload["inventory"]["with_demand"] == 1
    assert payload["meta"]["stock_period_independent"] is True


def test_responsible_fallback_and_intervention_order_are_deterministic():
    docs = [prop("2", responsable="Zoe", comuna="Ñuñoa"), prop("1", responsable="", comuna="Santiago")]
    payload = build_properties_inventory_contract(docs, {})
    assert payload["data_quality"]["missing_responsible"] == 1
    assert payload["intervention"][0]["reason"] == "SIN_DEMANDA_PERIODO"
    assert payload["intervention"][0]["code"] == "2"


def test_publication_is_evidence_based_and_does_not_use_numeric_status():
    result = _inventory_publications({"procasa": {"publicaciones": {"V": {"estado": 2}}}, "yapo": {"publicaciones": {"V": {"codigo": "Y1"}}}})
    assert result["procasa"]["has_evidence"] is False
    assert result["yapo"]["has_evidence"] is True


def test_zero_stock_uses_null_percentage():
    payload = build_properties_inventory_contract([])
    assert payload["inventory"]["coverage_pct"] is None
    assert payload["inventory"]["reconciliation"] is True


def test_dashboard_contract_contains_lazy_properties_route_and_no_placeholder():
    template = Path(__file__).parents[1].joinpath("templates", "leads_dashboard.html").read_text(encoding="utf-8")
    assert "/api/leads-dashboard/properties-inventory" in template
    assert "loadInventoryData" in template
    assert "/static/geo/chile-regiones.geojson" in template
    assert "/static/geo/chile-comunas.geojson" in template
    assert "geoRenderNationalTiles" in template
    assert "data-geo-metric=\"gap\"" in template
    assert "Ver las 16 regiones" in template


def test_territorial_review_uses_d3_hybrid_national_and_communal_renderer():
    template = Path(__file__).parents[1].joinpath("templates", "leads_dashboard.html").read_text(encoding="utf-8")
    assert "d3.geoMercator()" in template
    assert "d3.geoPath(projection)" in template
    assert "projection.fitExtent" in template
    assert "d3.scaleSqrt()" in template
    assert "path.centroid" in template
    assert "geo-demand-symbol" in template
    assert "rows.slice(0,8)" in template
    assert "Mostrar Top 8" in template
    assert "geoD3SelectRegion" in template
    assert "geoD3EnterCommunes" in template
    assert "data-geo-zoom=\"in\"" in template
    assert "event.type==='wheel'?event.ctrlKey:true" in template
    assert "domain([-maxAbs,0,maxAbs])" in template
    assert "geoFoldPanelSvg" not in template
    assert "geoPath(geometry,bounds" not in template
    assert "--accent-primary" not in template
    assert "--bg-input" not in template
    assert "function geoD3RenderNational" in template
    assert "function geoD3RenderCommunes" in template
    assert "if(GEO_METRIC==='leads')" in template
    assert "geo-territory-base" in template
    assert "scaleDiverging" in template


def demand_prop(code, office="PROCASA SUCRE", active=True, tipo="Casa", comuna="Santiago", venta=True):
    return {
        "codigo": code,
        "disponible_prop360": active,
        "estado": {"oficina": office, "ejecutivo": "Ana"},
        "tipo_operacion": {
            "tipo": tipo,
            "venta": venta,
            "arriendo": not venta,
            "precio_venta": {"precio_uf": 1800},
            "precio_arriendo": {"precio_clp": 450000},
        },
        "ubicacion": {"comuna": comuna},
        "caracteristicas": {"dormitorios": 3},
    }


def demand_lead(code, created_at=None):
    result = {"prospecto": {"codigo": code}, "lifecycle": {}}
    if created_at:
        result["created_at"] = created_at
    return result


def test_demand_scope_excludes_other_offices_from_sucre_kpis():
    payload = build_demand_capture_contract(
        [demand_prop("1"), demand_prop("2", office="PROCASA LA GLORIA"), demand_prop("3", active=False)],
        [demand_lead("1"), demand_lead("2"), demand_lead("3")],
        "2026-07-20", "2026-08-19",
    )
    assert payload["meta"]["canonical_office_field"] == "estado.oficina"
    assert payload["meta"]["canonical_office"] == "PROCASA SUCRE"
    assert payload["inventory"]["active"] == 1
    assert payload["demand"]["leads"] == 2
    assert payload["demand"]["properties_with_demand"] == 1
    assert payload["benchmark"]["offices_active_stock"][0]["office"] == "PROCASA LA GLORIA"


def test_demand_share_supply_share_gap_and_no_stock_segment():
    props = [demand_prop("1", tipo="Casa"), demand_prop("3", tipo="Departamento", active=False)]
    leads = [demand_lead("1"), demand_lead("1"), demand_lead("3"), demand_lead("3"), demand_lead("3")]
    payload = build_demand_capture_contract(props, leads)
    type_rows = {row["segment"]: row for row in payload["demand_intelligence"]["dimensions"]["type"]}
    assert type_rows["Casa"]["demand_share_pct"] == 40.0
    assert type_rows["Casa"]["supply_share_pct"] == 100.0
    assert type_rows["Departamento"]["stock_sucre"] == 0
    assert type_rows["Departamento"]["gap_pp"] == 60.0


def test_opportunity_ranking_requires_minimum_observations_and_is_explainable():
    props = [
        demand_prop("1", tipo="Casa", comuna="Santiago"),
        demand_prop("2", tipo="Casa", comuna="Santiago"),
        demand_prop("3", tipo="Casa", comuna="Santiago"),
        demand_prop("4", tipo="Casa", comuna="Ñuñoa"),
        demand_prop("5", tipo="Casa", comuna="Ñuñoa"),
        demand_prop("6", tipo="Casa", comuna="Ñuñoa"),
    ]
    leads = [demand_lead("1"), demand_lead("2"), demand_lead("3"), demand_lead("1"), demand_lead("2")]
    payload = build_demand_capture_contract(props, leads, min_observations=3)
    assert payload["opportunities"]
    assert all(row["recommendation"] in {"Demanda reciente alta", "Demanda reciente media", "Demanda reciente baja", "Sin evidencia suficiente"} for row in payload["opportunities"])
    assert all(row["leads"] >= 3 for row in payload["opportunities"])
    assert all(row["dimension"] == "combined" for row in payload["opportunities"])
    assert all(row["operation"] and row["type"] and row["commune"] for row in payload["opportunities"])
    assert all("score" not in row for row in payload["opportunities"])
    assert all(row["strategic_fit"]["status"] == "undefined" for row in payload["opportunities"])


def test_combined_opportunities_exclude_low_support_and_are_deterministic():
    props = [
        demand_prop("1", tipo="Casa", comuna="Santiago"),
        demand_prop("2", tipo="Casa", comuna="Santiago"),
        demand_prop("3", tipo="Casa", comuna="Santiago"),
        demand_prop("4", tipo="Departamento", comuna="Ñuñoa"),
    ]
    leads = [demand_lead("1"), demand_lead("2"), demand_lead("3"), demand_lead("4"), demand_lead("4")]
    first = build_demand_capture_contract(props, leads)
    second = build_demand_capture_contract(list(reversed(props)), list(reversed(leads)))
    assert [row["segment_key"] for row in first["opportunities"]] == [row["segment_key"] for row in second["opportunities"]]
    assert all(row["properties_observed"] >= 3 for row in first["opportunities"])
    assert all(" + " not in row["operation"] or row["stock_sucre"] >= 3 for row in first["opportunities"])
    assert first["demand_intelligence"]["support_rules"]["level_1"]["min_leads"] >= 3


def test_historical_demand_includes_inactive_but_stock_remains_active_only():
    props = [
        demand_prop("1", tipo="Casa", comuna="Talca"),
        demand_prop("2", tipo="Casa", comuna="Talca"),
        demand_prop("3", tipo="Casa", comuna="Talca"),
        demand_prop("7", tipo="Casa", comuna="Talca", active=False),
        demand_prop("4", tipo="Casa", comuna="Ñuñoa"),
        demand_prop("5", tipo="Casa", comuna="Ñuñoa"),
        demand_prop("6", tipo="Casa", comuna="Ñuñoa"),
    ]
    leads = [
        demand_lead("1", "2026-07-25T12:00:00Z"),
            demand_lead("1", "2026-07-26T12:00:00Z"),
            demand_lead("2", "2026-07-27T12:00:00Z"),
            demand_lead("3", "2026-07-28T12:00:00Z"),
            demand_lead("7", "2026-07-28T12:00:00Z"),
        demand_lead("7", "2026-06-10T12:00:00Z"),
        demand_lead("1", "2026-05-10T12:00:00Z"),
    ]
    payload = build_demand_capture_contract(props, leads, "2026-07-20", "2026-08-19")
    row = next(row for row in payload["opportunities"] if row["commune"] == "Talca")
    assert row["historical_leads_total"] == 7
    assert row["historical_properties_with_demand"] == 4
    assert row["first_demand_at"].startswith("2026-05-10")
    assert row["last_demand_at"].startswith("2026-07-28")
    assert row["weeks_with_demand"] >= 3
    assert row["months_with_demand"] == 3
    assert row["recency"]["w1_leads"] == 0
    assert row["recency"]["w2_leads"] == 1
    assert payload["inventory"]["active"] == 6
    assert payload["demand"]["properties_with_demand"] == 3


def test_recency_zero_middle_window_is_reactivation_not_continuity():
    props = [
        demand_prop("1", tipo="Casa", comuna="Talca"),
        demand_prop("2", tipo="Casa", comuna="Talca"),
        demand_prop("3", tipo="Casa", comuna="Talca"),
        demand_prop("4", tipo="Casa", comuna="Ñuñoa"),
        demand_prop("5", tipo="Casa", comuna="Ñuñoa"),
        demand_prop("6", tipo="Casa", comuna="Ñuñoa"),
    ]
    leads = [
        *[demand_lead("1", "2026-08-01T12:00:00Z") for _ in range(3)],
        *[demand_lead("2", "2026-08-02T12:00:00Z") for _ in range(3)],
        *[demand_lead("3", "2026-08-03T12:00:00Z") for _ in range(2)],
            *[demand_lead("2", "2026-06-10T12:00:00Z") for _ in range(3)],
    ]
    payload = build_demand_capture_contract(props, leads, "2026-07-20", "2026-08-19")
    row = next(row for row in payload["opportunities"] if row["commune"] == "Talca")
    assert row["recency"]["w0_leads"] == 8
    assert row["recency"]["w1_leads"] == 0
    assert row["recency"]["w2_leads"] == 3
    assert row["recency"]["trend"] == "Reactivación"


def test_recent_demand_band_is_primary_and_context_does_not_change_it():
    props = [*([demand_prop(str(i), tipo="Casa", comuna="Talca") for i in range(1, 9)]), *([demand_prop(str(i), tipo="Casa", comuna="Santiago") for i in range(9, 12)])]
    leads = [
        demand_lead("1", "2026-08-01T12:00:00Z"),
        demand_lead("2", "2026-08-02T12:00:00Z"),
        demand_lead("3", "2026-08-03T12:00:00Z"),
        demand_lead("1", "2026-08-04T12:00:00Z"),
        demand_lead("2", "2026-08-05T12:00:00Z"),
        demand_lead("4", "2026-06-01T12:00:00Z"),
        demand_lead("5", "2026-05-01T12:00:00Z"),
    ]
    payload = build_demand_capture_contract(props, leads, "2026-07-20", "2026-08-19")
    row = next(row for row in payload["opportunities"] if row["commune"] == "Talca")
    assert row["recommendation"] == "Demanda reciente alta"
    assert row["recency"]["w0_leads"] == 5
    assert row["persistence"] == "Persistente"
    assert "gap_pp" in row


def test_recent_demand_bands_are_descriptive_and_insufficient_is_not_no_capture():
    assert _demand_capture_recent_band(4, 8, 3) == "Demanda reciente media"
    assert _demand_capture_recent_band(0, 8, 3) == "Demanda reciente baja"
    assert _demand_capture_recent_band(4, 4, 3) == "Sin evidencia suficiente"
    assert "probabilidad" not in _demand_capture_recent_band(4, 8, 3).lower()


def test_demand_dimensions_exclude_incomplete_price_and_bedroom_records():
    missing_price = demand_prop("missing-price", tipo="Casa", comuna="Santiago")
    missing_price["tipo_operacion"]["precio_venta"] = {}
    non_residential = demand_prop("land", tipo="Terreno", comuna="Santiago")
    residential_without_bedrooms = demand_prop("no-bedrooms", tipo="Casa", comuna="Santiago")
    residential_without_bedrooms["caracteristicas"] = {}
    for item in (missing_price, non_residential, residential_without_bedrooms):
        item["ubicacion"]["region"] = "Metropolitana"
    payload = build_demand_capture_contract(
        [missing_price, non_residential, residential_without_bedrooms],
        [demand_lead("missing-price"), demand_lead("land"), demand_lead("no-bedrooms")],
    )
    assert all(row["segment"] is not None for row in payload["demand_intelligence"]["dimensions"]["price_range"])
    assert all(row["segment"] is not None for row in payload["demand_intelligence"]["dimensions"]["bedrooms"])
    quality = payload["data_quality"]
    assert quality["price_dimension"]["excluded_missing_price"] == 1
    assert quality["bedrooms_dimension"]["excluded_non_residential"] == 1
    assert quality["bedrooms_dimension"]["excluded_missing_or_non_residential"] == 1


def test_rm_zone_mapping_is_explicit_and_non_overlapping_for_common_communes():
    assert _demand_capture_zone_rm("Metropolitana", "Providencia") == "Oriente"
    assert _demand_capture_zone_rm("Metropolitana", "Maipú") == "Poniente"
    assert _demand_capture_zone_rm("Valparaíso", "Providencia") is None
    assert _demand_capture_profile({"ubicacion": {"region": "Metropolitana", "comuna": "Providencia"}, "tipo_operacion": {"tipo": "Casa", "venta": True}, "caracteristicas": {"dormitorios": 3}})["zone_rm"] == "Oriente"


def test_geography_contract_aggregates_regions_communes_and_normalizes_names():
    providencia = demand_prop("geo-1", comuna="Providencia")
    providencia["ubicacion"]["region"] = "Metropolitana"
    nunoa = demand_prop("geo-2", comuna="Ñuñoa")
    nunoa["ubicacion"]["region"] = "Región Metropolitana de Santiago"
    payload = build_demand_capture_contract(
        [providencia, nunoa],
        [demand_lead("geo-1"), demand_lead("geo-1"), demand_lead("geo-2")],
        "2026-07-20", "2026-08-19",
    )
    geography = payload["demand_intelligence"]["geography"]
    assert geography["region"][0]["geo_key"] == "metropolitana"
    communes = {row["geo_key"]: row for row in geography["commune"]}
    assert communes["providencia"]["leads"] == 2
    assert communes["nunoa"]["leads"] == 1
    assert geography["metric_rule"]["default"] == "leads"
    assert geography["matching"]["unmatched_territories"] is None
    assert _demand_capture_geo_key("O'Higgins") == "ohiggins"
    assert _demand_capture_geo_key("San José de Maipo") == "san jose de maipo"
    assert _demand_capture_geo_key("Viña del Mar") == "vina del mar"


def test_official_geojson_reconciles_administrative_regions_and_communes():
    regions = __import__("json").loads((Path(__file__).resolve().parents[1] / "static/geo/chile-regiones.geojson").read_text(encoding="utf-8"))["features"]
    communes = __import__("json").loads((Path(__file__).resolve().parents[1] / "static/geo/chile-comunas.geojson").read_text(encoding="utf-8"))["features"]
    administrative_regions = [row for row in regions if _demand_capture_geo_key(row["properties"].get("Region")) != "zona sin demarcar"]
    administrative_communes = [row for row in communes if _demand_capture_geo_key(row["properties"].get("Region")) != "zona sin demarcar"]
    assert len(administrative_regions) == 16
    assert len(administrative_communes) == 345
    assert sum(_demand_capture_geo_key(row["properties"].get("Region")) == "metropolitana" for row in administrative_communes) == 52
    assert sum(_demand_capture_geo_key(row["properties"].get("Region")) == "valparaiso" for row in administrative_communes) == 38
    assert len({row["properties"].get("Comuna") for row in administrative_communes}) == 345


def test_public_review_fixture_is_sanitized_and_deterministic():
    first = territorial_review_payload()
    second = territorial_review_payload()
    regions = first["demand_intelligence"]["geography"]["region"]
    assert first == second
    assert len(regions) == 16
    assert sum(row["leads"] for row in regions) == 297
    assert first["inventory"]["active"] == 442
    assert all("phone" not in str(first).lower() and "email" not in str(first).lower() for _ in [0])


def test_review_mode_isolated_from_private_navigation_and_overview_loads():
    template = (Path(__file__).resolve().parents[1] / "templates/leads_dashboard.html").read_text(encoding="utf-8")
    assert "territorial-review-mode" in template
    assert "api/review/leads-dashboard" in template
    assert "if (REVIEW_MODE)" in template
    assert "territorial-review-mode .sidebar-main-nav" in template
    assert "territorial-review-mode #sessionWarningModal" in template


def test_methodology_note_documents_backtest_and_no_visible_score():
    template = Path(__file__).parents[1].joinpath("templates", "leads_dashboard.html").read_text(encoding="utf-8")
    assert "Validación histórica" in template
    assert "25 cortes temporales semanales" in template
    assert "opportunity_score" not in template.lower()
    assert "91,96" not in template
    assert "forecast" in template.lower()


def test_capture_simulator_matches_history_and_keeps_stock_active_only():
    props = {
        "1": demand_prop("1", tipo="Casa", comuna="Talca"),
        "2": demand_prop("2", tipo="Casa", comuna="Talca"),
        "3": demand_prop("3", tipo="Casa", comuna="Talca", active=False),
        "4": demand_prop("4", tipo="Casa", comuna="Talca", active=True, venta=False),
    }
    from datetime import datetime, timezone
    leads = [
        {"code": "1", "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc)},
        {"code": "1", "created_at": datetime(2026, 6, 1, tzinfo=timezone.utc)},
        {"code": "2", "created_at": datetime(2026, 7, 25, tzinfo=timezone.utc)},
        {"code": "3", "created_at": datetime(2026, 5, 1, tzinfo=timezone.utc)},
    ]
    payload = build_capture_simulation_contract({"properties": props, "leads": leads, "period_end": datetime(2026, 8, 19, tzinfo=timezone.utc), "active_sucre_total": 3, "recent_leads_total": 10}, {"operation": "Venta", "type": "Casa", "commune": "Talca", "price": 1800})
    assert payload["available"] is True
    assert payload["no_write"] is True
    assert payload["evidence"]["historical_leads_compatible"] == 4
    assert payload["evidence"]["historical_properties_with_demand"] == 3
    assert payload["evidence"]["stock_active_comparable"] == 2
    assert payload["strategic_fit"]["status"] == "undefined"
    assert payload["predicted_demand_30d"] is None
    assert len(payload["comparables"]) <= 5


def test_capture_simulator_price_change_recalculates_and_empty_input_is_safe():
    from datetime import datetime, timezone
    props = {"1": demand_prop("1", tipo="Casa", comuna="Talca")}
    dataset = {"properties": props, "leads": [{"code": "1", "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc)}], "period_end": datetime(2026, 8, 19, tzinfo=timezone.utc), "active_sucre_total": 1, "recent_leads_total": 1}
    assert build_capture_simulation_contract(dataset, {})["available"] is False
    first = build_capture_simulation_contract(dataset, {"operation": "Venta", "type": "Casa", "commune": "Talca", "price": 1800})
    second = build_capture_simulation_contract(dataset, {"operation": "Venta", "type": "Casa", "commune": "Talca", "price": 3500})
    assert first["inputs"]["price"] != second["inputs"]["price"]
    assert first["forecast_status"] == "not_published"


def test_attribution_reports_identifiable_office_coverage_separately():
    payload = build_demand_capture_contract([demand_prop("1")], [demand_lead("1"), demand_lead("missing")])
    assert payload["attribution"]["leads_total"] == 2
    assert payload["attribution"]["leads_with_identifiable_office"] == 1
    assert payload["attribution"]["coverage_pct"] == 50.0


def test_inventory_endpoint_keeps_lazy_load_and_batch_contract():
    root = Path(__file__).parents[1]
    template = root.joinpath("templates", "leads_dashboard.html").read_text(encoding="utf-8")
    queries = root.joinpath("analytics", "leads_queries.py").read_text(encoding="utf-8")
    assert "demandRender(data)" in template
    assert "query_demand_capture_dashboard" in queries
    assert '"mongo_reads": 2' in queries
    assert "Oportunidades de Captación" in template
    assert "BENCHMARK RED PROCASA" in template


def test_forecast_dataset_is_weekly_and_split_is_chronological():
    rows = [{"created_at": f"2026-01-{day:02d}T12:00:00Z", "operation": "Venta", "type": "Casa", "commune": "Santiago", "price_range": "Venta · hasta 2.000 UF", "bedrooms": "3"} for day in range(2, 31, 7)]
    dataset = build_weekly_segment_dataset(rows)
    train, test = chronological_split(dataset, holdout_weeks=1)
    assert train and test
    assert max(row["week"] for row in train) < min(row["week"] for row in test)


def test_forecast_baseline_metrics_are_reproducible_and_zero_safe():
    train = [{"week": "2026-01-05", "segment": "a", "leads": 2}, {"week": "2026-01-12", "segment": "a", "leads": 4}]
    test = [{"week": "2026-01-19", "segment": "a", "leads": 3}, {"week": "2026-01-19", "segment": "b", "leads": 0}]
    predictions = naive_moving_average(train, test)
    assert forecast_metrics(predictions)["mae"] == 0.0
    assert forecast_metrics(predictions)["wape"] == 0.0
    assert assess_readiness(train + test, holdout_weeks=1)["available"] is False
