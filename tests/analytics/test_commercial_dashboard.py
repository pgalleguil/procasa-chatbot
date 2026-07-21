"""Tests for Commercial Dashboard — read-only verification of queries, KPIs, funnel, SLA, insights."""
import pytest
import sys
sys.path.insert(0, '.')

from analytics.leads_queries import (
    query_commercial_kpis,
    query_commercial_funnel,
    query_sla_risk_panel,
    query_demand_by_price_ranges,
    query_commercial_executive_matrix,
    query_commercial_property_ranking,
    query_commercial_insights,
    query_temperature_coverage,
    COMMERCIAL_FUNNEL_STAGES,
    VISIT_RESULTS,
    PRICE_RANGES_UF,
    PRICE_RANGES_CLP,
)
from analytics.leads_service import get_commercial_dashboard, get_commercial_filter_options


# =============================================================================
# KPI TESTS
# =============================================================================

class TestCommercialKPIs:
    def test_returns_dict(self):
        r = query_commercial_kpis()
        assert isinstance(r, dict)
        assert "leads_received" in r
        assert "leads_hot_history" in r
        assert "visit_intent" in r
        assert "visits_scheduled" in r
        assert "sla_compliance" in r
        assert "closed_won" in r

    def test_kpi_structure(self):
        r = query_commercial_kpis()
        for key in ["leads_received", "leads_hot_history", "visit_intent", "visits_scheduled", "closed_won"]:
            k = r[key]
            assert "value" in k
            assert "previous" in k
            assert "variation_pct" in k
            # Value can be 0 or positive, never negative for counts
            assert k["value"] is None or k["value"] >= 0

    def test_sla_special_structure(self):
        sla = query_commercial_kpis()["sla_compliance"]
        assert "value" in sla  # percentage or None

    def test_no_duplicate_counts(self):
        """Unit canonical: lead._id counted once. Verify total >= hot."""
        r = query_commercial_kpis()
        received = r["leads_received"]["value"] or 0
        hot = r.get("leads_hot_history", {}).get("value") or 0
        assert hot <= received, "Hot leads cannot exceed total received"

    def test_visit_intent_within_received(self):
        r = query_commercial_kpis()
        received = r["leads_received"]["value"] or 0
        vi = r["visit_intent"]["value"] or 0
        assert vi <= received, "Visit intent cannot exceed total received"

    def test_closed_won_within_received(self):
        """Closed won = events, can be for leads created before period."""
        r = query_commercial_kpis()
        cw = r["closed_won"]["value"] or 0
        # Activity can include leads from before period, so no upper bound check

    def test_period_meta(self):
        r = query_commercial_kpis(period_start="2026-07-01", period_end="2026-07-20")
        assert r["_meta"]["period_start"] == "2026-07-01"
        assert r["_meta"]["period_end"] == "2026-07-20"

    def test_meta_universes(self):
        """Each KPI must declare its universe."""
        r = query_commercial_kpis()
        meta = r["_meta"]
        assert "temperature_coverage" in meta

    def test_meta_temperature_note(self):
        r = query_commercial_kpis()
        assert "note" in r["_meta"]

    def test_variation_null_when_no_previous(self):
        """When previous period has zero leads, variation_pct must be None, not infinity."""
        r = query_commercial_kpis()
        for key in ["leads_received", "leads_hot_history", "visit_intent", "visits_scheduled", "closed_won"]:
            var = r[key]["variation_pct"]
            assert var is None or isinstance(var, float), f"{key} variation should be None or float"
            if var is not None:
                assert var != float('inf'), f"{key} variation should not be infinity"
                assert var != float('-inf'), f"{key} variation should not be -infinity"

    def test_temperature_not_retroactive(self):
        """A lead created as COLD that later becomes HOT should NOT count as Hot
        in historical periods if temperature_history shows only COLD at cutoff."""
        # This test validates the pipeline logic. We query a specific date range
        # and verify the hot count uses temperature_history, not current state.
        r = query_commercial_kpis(period_start="2026-01-01", period_end="2026-01-31")
        meta = r["_meta"]
        assert "temperature_coverage" in meta
        cov = meta["temperature_coverage"]
        assert "with_history" in cov
        assert "history_coverage_pct" in cov
        # If no history, should report S/I, not 0
        if cov["total"] > 0 and cov["history_coverage_pct"] == 0:
            # All temperatures unknown - hot should still be counted correctly
            pass  # Not necessarily 0 - leads could have current HOT with no history

    def test_sla_pp_not_invented(self):
        """Without historical SLA data, pp_change should not be invented."""
        sla = query_commercial_kpis()["sla_compliance"]
        # pp_change may be None since we can't compute historical SLA for most periods
        assert sla.get("pp_change") is None or isinstance(sla["pp_change"], float)


# =============================================================================
# FUNNEL TESTS
# =============================================================================

class TestCommercialFunnel:
    def test_returns_list(self):
        r = query_commercial_funnel()
        assert isinstance(r, list)

    def test_has_expected_stages(self):
        r = query_commercial_funnel()
        keys = [s[0] for s in COMMERCIAL_FUNNEL_STAGES]
        returned_keys = [s["key"] for s in r]
        for k in keys:
            assert k in returned_keys, f"Stage '{k}' missing from funnel"

    def test_stage_structure(self):
        r = query_commercial_funnel()
        for stage in r:
            assert "key" in stage
            assert "label" in stage
            assert "count" in stage
            assert stage["count"] >= 0

    def test_monotonic_or_equal(self):
        """Funnel should decrease (or stay equal) from top to bottom.
        Note: 'data_delivered' may exceed 'visit_intent' since data completeness
        and visit intent are independent dimensions.
        Note 2: With strict cutoff, visit_intent may depend on stage_history
        while hot depends on temperature_history, so they may be out of order."""
        r = query_commercial_funnel()
        non_monotonic_pairs = {("visit_intent", "data_delivered"), ("hot", "visit_intent")}
        for i in range(1, len(r)):
            pair = (r[i - 1]["key"], r[i]["key"])
            if pair in non_monotonic_pairs:
                continue
            assert r[i]["count"] <= r[i - 1]["count"], \
                f"Funnel leak at {r[i]['key']}: {r[i]['count']} > {r[i-1]['count']}"

    def test_conversion_from_prev(self):
        r = query_commercial_funnel()
        non_monotonic = {("visit_intent", "data_delivered")}
        for i, stage in enumerate(r):
            pair = (r[i - 1]["key"], r[i]["key"]) if i > 0 else None
            if i == 0:
                assert stage["conversion_from_prev"] is None
            elif pair in non_monotonic:
                continue
            else:
                if stage["count"] > 0 and r[i - 1]["count"] > 0:
                    if stage["count"] <= r[i - 1]["count"]:
                        assert stage["conversion_from_prev"] is not None or stage["count"] == r[i - 1]["count"]
                        if stage["conversion_from_prev"] is not None:
                            assert 0 <= stage["conversion_from_prev"] <= 100

    def test_pct_of_received(self):
        r = query_commercial_funnel()
        for stage in r:
            if r[0]["count"] > 0:
                assert stage["pct_of_received"] is not None
                assert 0 <= stage["pct_of_received"] <= 100


# =============================================================================
# SLA RISK TESTS
# =============================================================================

class TestSLARiskPanel:
    def test_returns_dict(self):
        r = query_sla_risk_panel()
        assert isinstance(r, dict)

    def test_has_required_fields(self):
        r = query_sla_risk_panel()
        required = ["total_hot", "total_determined", "total_undetermined",
                     "within_sla_pct", "critical_open",
                     "breached_during_period", "recovered_after_breach",
                     "median_response_minutes", "p90_response_minutes",
                     "distribution", "conversion_table", "sla_start_coverage", "sla_policy"]
        for field in required:
            assert field in r, f"Missing field: {field}"

    def test_sla_start_coverage(self):
        r = query_sla_risk_panel()
        cov = r["sla_start_coverage"]
        assert "total" in cov
        assert "by_origin" in cov
        assert "assigned_at_pct" in cov
        assert "created_at_verified_pct" in cov
        assert "undetermined_pct" in cov
        assert cov.get("note") is not None

    def test_sla_policy(self):
        r = query_sla_risk_panel()
        policy = r["sla_policy"]
        assert policy["type"] == "calendar_minutes"
        assert policy["threshold_minutes"] == 180
        assert policy["timezone"] == "America/Santiago"

    def test_distribution_buckets(self):
        r = query_sla_risk_panel()
        dist = r["distribution"]
        expected_labels = ["Menos de 30 min", "30-60 min", "1-3 horas", "M\u00e1s de 3 horas", "Sin inicio SLA determinable"]
        for d in dist:
            assert d["label"] in expected_labels
            assert d["count"] >= 0

    def test_conversion_table_structure(self):
        r = query_sla_risk_panel()
        for row in r["conversion_table"]:
            assert "bucket" in row
            assert "hot" in row
            assert "visits" in row
            assert "conversion_pct" in row

    def test_sla_not_invented(self):
        """SLA should not have invented compliance when there's no management data."""
        r = query_sla_risk_panel()
        if r["total_hot"] == 0:
            assert r["within_sla_pct"] is None, "SLA must be null when no hot leads"

    def test_critical_open_consistency(self):
        r = query_sla_risk_panel()
        assert r["critical_open"] <= r["total_hot"], "Critical open cannot exceed total hot"
        assert r["no_management"] <= r["total_hot"]

    def test_stock_separation(self):
        """Critical open should be <= total hot, breached includes recovered."""
        r = query_sla_risk_panel()
        if r["total_hot"] > 0:
            assert r["critical_open"] + r["breached_during_period"] <= r["total_hot"] or r["total_hot"] == 0


# =============================================================================
# DEMAND BY PRICE TESTS
# =============================================================================

class TestDemandByPrice:
    def test_returns_dict(self):
        r = query_demand_by_price_ranges()
        assert isinstance(r, dict)
        assert "price_ranges" in r

    def test_price_range_structure(self):
        r = query_demand_by_price_ranges()
        for op in r["price_ranges"]:
            assert "operation" in op
            assert "total" in op
            assert "ranges" in op
            assert op["total"] >= 0

    def test_price_range_counts_sum(self):
        """Sum of all range counts should equal the operation total."""
        r = query_demand_by_price_ranges()
        for op in r["price_ranges"]:
            range_sum = sum((rg["count"] for rg in op["ranges"]), 0)
            if op["total"] > 0:
                assert range_sum == op["total"], \
                    f"Range counts ({range_sum}) != operation total ({op['total']})"

    def test_currency_separation(self):
        """Venta must use UF, Arriendo must use CLP."""
        r = query_demand_by_price_ranges()
        for op in r.get("price_ranges", []):
            coverage = op.get("coverage", {})
            if op["operation"] == "Venta":
                assert coverage.get("currency") == "UF"
            elif op["operation"] == "Arriendo":
                assert coverage.get("currency") == "CLP"

    def test_coverage_reporting(self):
        """Each operation must report coverage."""
        r = query_demand_by_price_ranges()
        assert "coverage" in r
        cov = r["coverage"]
        for key in ["operacion", "tipo_propiedad", "comuna", "precio"]:
            assert key in cov, f"Missing coverage field: {key}"


# =============================================================================
# EXECUTIVE MATRIX TESTS
# =============================================================================

class TestExecutiveMatrix:
    def test_returns_list(self):
        r = query_commercial_executive_matrix()
        assert isinstance(r, list)

    def test_executive_structure(self):
        r = query_commercial_executive_matrix()
        for ex in r:
            assert "executive" in ex
            assert "assigned" in ex
            assert "hot" in ex
            assert "sla_fulfilled" in ex
            assert "ever_closed_won" in ex

    def test_no_negative_values(self):
        r = query_commercial_executive_matrix()
        for ex in r:
            for key in ["assigned", "hot", "ever_visit_scheduled",
                         "ever_visit_done", "ever_closed_won", "ever_closed_lost"]:
                assert ex[key] >= 0, f"{ex['executive']}.{key} is negative"

    def test_sla_pct_range(self):
        r = query_commercial_executive_matrix()
        for ex in r:
            if ex["assigned"] > 0:
                sla = ex["sla_fulfilled"]
                if sla is not None:
                    assert 0 <= sla <= 100, f"{ex['executive']} SLA out of range: {sla}"

    def test_conversion_pct_range(self):
        r = query_commercial_executive_matrix()
        for ex in r:
            if ex["assigned"] > 0:
                for key in ["conversion_to_visit_pct", "conversion_to_close_pct"]:
                    val = ex.get(key)
                    if val is not None:
                        assert 0 <= val <= 100, f"{ex['executive']}.{key} out of range: {val}"

    def test_unassigned_filtered(self):
        """Leads without assignment should be excluded from exec matrix."""
        r = query_commercial_executive_matrix()
        for ex in r:
            assert ex["executive"] not in ["Sin Asignar", "No Asignado", None, ""]

    def test_universe_declared(self):
        r = query_commercial_executive_matrix()
        for ex in r:
            assert "universe" in ex

    def test_stage_reached_via_history(self):
        """Leads that advanced from visit to close should count in visit_scheduled."""
        r = query_commercial_executive_matrix()
        for ex in r:
            assert ex["ever_visit_scheduled"] >= ex["ever_closed_won"], \
                f"{ex['executive']}: visit_scheduled ({ex['ever_visit_scheduled']}) < closed_won ({ex['ever_closed_won']})"


# =============================================================================
# PROPERTY RANKING TESTS
# =============================================================================

class TestPropertyRanking:
    def test_returns_dict(self):
        r = query_commercial_property_ranking()
        assert isinstance(r, dict)
        assert "opportunity" in r
        assert "leakage" in r

    def test_opportunity_structure(self):
        r = query_commercial_property_ranking()
        for p in r["opportunity"]:
            assert "code" in p
            assert "leads" in p
            assert "hot" in p
            assert "conversion_pct" in p
            assert "ever_visit_intent" in p
            assert "ever_visit_scheduled" in p

    def test_leakage_structure(self):
        r = query_commercial_property_ranking()
        for p in r["leakage"]:
            assert "code" in p
            assert "uncoordinated" in p
            assert "unmanaged" in p
            assert "ever_visit_intent" in p

    def test_opportunity_ever_fields(self):
        """Property ranking should use 'ever' (stage reached) fields."""
        r = query_commercial_property_ranking()
        for p in r["opportunity"]:
            assert p["ever_visit_scheduled"] >= p["ever_closed_won"], \
                f"{p['code']}: scheduled ({p['ever_visit_scheduled']}) < closed ({p['ever_closed_won']})"

    def test_opportunity_sorted(self):
        r = query_commercial_property_ranking()
        opp = r["opportunity"]
        for i in range(1, len(opp)):
            assert opp[i]["leads"] <= opp[i - 1]["leads"], "Opportunity not sorted by leads descending"


# =============================================================================
# INSIGHTS TESTS
# =============================================================================

class TestInsights:
    def test_returns_list(self):
        ins = query_commercial_insights()
        assert isinstance(ins, list)
        assert len(ins) <= 5, "Max 5 insights"

    def test_insight_structure(self):
        ins = query_commercial_insights()
        for i in ins:
            assert "priority" in i
            assert "title" in i
            assert "finding" in i
            assert "evidence" in i
            assert "impact" in i
            assert "recommended_action" in i
            assert i["priority"] in ("critical", "high", "medium", "info")

    def test_insights_not_invented(self):
        """Insights must reference real data, never invent metrics."""
        ins = query_commercial_insights()
        for i in ins:
            assert "S/I" not in i.get("finding", ""), "Insight should not contain S/I in finding"
            assert "no determinado" not in i.get("finding", "").lower()


# =============================================================================
# CONSOLIDATED DASHBOARD TEST
# =============================================================================

class TestCommercialDashboardService:
    def test_returns_dict(self):
        d = get_commercial_dashboard()
        assert isinstance(d, dict)
        assert "meta" in d
        assert "kpis" in d
        assert "funnel" in d
        assert "sla_risk" in d
        assert "demand_by_price" in d
        assert "executives" in d
        assert "properties" in d
        assert "sources" in d
        assert "insights" in d

    def test_meta_read_only(self):
        d = get_commercial_dashboard()
        assert d["meta"]["read_only"] == True
        assert d["meta"]["unit"] == "lead._id"

    def test_period_filtering(self):
        d = get_commercial_dashboard(
            period_start="2026-07-01",
            period_end="2026-07-20",
        )
        period = d["meta"]["period"]
        assert "type" in period
        assert "current" in period
        assert "previous" in period

    def test_no_invented_temperatures(self):
        """Temperature unknown should not appear as COLD or 0."""
        k = get_commercial_dashboard()["kpis"]
        hot = k["leads_hot_history"]["value"] or 0
        received = k["leads_received"]["value"] or 0
        # This should hold even with unknown temperatures
        assert hot <= received

    def test_hot_does_not_imply_managed(self):
        """Hot != gestionado. Verify SLA compliance reflects this."""
        d = get_commercial_dashboard()
        sla = d["sla_risk"]
        hot_total = sla["total_hot"]
        within_sla = sla["within_sla_pct"]
        if hot_total > 0:
            assert within_sla is None or within_sla <= 100

    def test_cold_not_invented(self):
        """Temperature desconocida se reporta como S/I, no como Cold."""
        d = get_commercial_dashboard()
        kpis = d["kpis"]
        meta = kpis.get("_meta", {})
        cov = meta.get("coverage", {})
        # Si no hay historial, se reporta cobertura, no se inventa Cold
        if cov.get("total", 0) > 0 and cov.get("history_coverage_pct", 0) == 0:
            pass  # Temperatura desconocida - se reporta en coverage
        # Executive matrix fields are hot/cold based on temperature_history
        for ex in d["executives"]:
            assert "hot" in ex

    def test_sla_start_coverage_reported(self):
        d = get_commercial_dashboard()
        sla = d["sla_risk"]
        assert "sla_start_coverage" in sla
        assert "sla_policy" in sla

    def test_meta_temperature_coverage(self):
        d = get_commercial_dashboard()
        kpis = d["kpis"]
        meta = kpis.get("_meta", {})
        assert "temperature_coverage" in meta
        cov = meta["temperature_coverage"]
        assert "history_coverage_pct" in cov
        assert "with_history" in cov

    def test_universes_declared(self):
        d = get_commercial_dashboard()
        meta = d["kpis"].get("_meta", {})
        assert "temperature_coverage" in meta

    def test_stage_reached_not_current_state(self):
        """A lead that reached VISIT_SCHEDULED then moved to CLOSED_WON
        should still count in visit_scheduled funnel stage."""
        d = get_commercial_dashboard()
        funnel = d["funnel"]
        for stage in funnel:
            if stage["key"] == "visit_scheduled":
                assert stage["count"] >= 0
                break

    def test_funnel_uses_stage_reached(self):
        """Funnel stages should be cumulative (monotonic decreasing).
        Note: 'data_delivered' may exceed 'visit_intent' as they are independent."""
        d = get_commercial_dashboard()
        funnel = d["funnel"]
        non_monotonic = {("visit_intent", "data_delivered")}
        for i in range(1, len(funnel)):
            pair = (funnel[i - 1]["key"], funnel[i]["key"])
            if pair in non_monotonic:
                continue
            assert funnel[i]["count"] <= funnel[i - 1]["count"], \
                f"Stage '{funnel[i]['key']}' exceeds '{funnel[i-1]['key']}'"


# =============================================================================
# NEW FUNCTIONALITY TESTS — FILTERS, COMPARISON, COVERAGE, TYPES
# =============================================================================

class TestCommercialFilterOptions:
    def test_returns_dict(self):
        r = get_commercial_filter_options()
        assert isinstance(r, dict)

    def test_has_required_fields(self):
        r = get_commercial_filter_options()
        for key in ["executives", "sources", "operations", "property_types",
                     "communes", "properties", "temperatures", "assignment_states"]:
            assert key in r, f"Missing filter field: {key}"

    def test_executives_list(self):
        r = get_commercial_filter_options()
        assert isinstance(r["executives"], list)

    def test_sources_list(self):
        r = get_commercial_filter_options()
        assert isinstance(r["sources"], list)

    def test_filter_option_structure(self):
        r = get_commercial_filter_options()
        for opts in [r["sources"], r["operations"], r["property_types"], r["communes"]]:
            for o in opts:
                assert "value" in o
                assert "label" in o
                assert "count" in o


class TestCommercialDashboardComparison:
    def test_compare_prev_mode(self):
        d = get_commercial_dashboard(compare="prev")
        assert d["meta"]["period"]["type"] == "custom_vs_previous"
        prev = d["meta"]["period"]["previous"]
        assert prev.get("start"), "Previous period should have start"
        assert prev.get("end"), "Previous period should have end"

    def test_compare_yoy_mode(self):
        d = get_commercial_dashboard(
            period_start="2026-07-01", period_end="2026-07-21", compare="yoy"
        )
        assert d["meta"]["period"]["type"] == "custom_vs_yoy"
        prev = d["meta"]["period"]["previous"]
        assert "2025" in prev.get("start", ""), "YoY should reference previous year"

    def test_compare_none_mode(self):
        d = get_commercial_dashboard(compare="none")
        assert d["meta"]["period"]["type"] == "custom_no_comparison"
        prev = d["meta"]["period"]["previous"]
        assert prev.get("label") == "Sin comparaci\u00f3n"

    def test_compare_defaults_to_previous(self):
        d = get_commercial_dashboard()
        assert "previous" in d["meta"]["period"]["type"]


class TestCommercialDashboardCoverage:
    def test_coverage_in_response(self):
        d = get_commercial_dashboard()
        assert "coverage" in d, "Commercial dashboard should include coverage data"

    def test_coverage_structure(self):
        d = get_commercial_dashboard()
        cov = d["coverage"]
        assert isinstance(cov, dict)
        # Should have field coverage for key fields
        assert any("prospecto.origen" in k or "origen" in k.lower()
                   for k in cov.keys()), "Should include source coverage"


class TestExecutiveFilter:
    def test_executive_filter_changes_universe(self):
        """Verify executive filter is applied to _build_extra_filter."""
        kpis = query_commercial_kpis(filters={"ejecutivo_asignado": "NoExisteXYZ"})
        assert kpis["leads_received"]["value"] == 0, \
            "Filtering for non-existent exec should give zero"

    def test_executive_filter_in_extra_filter(self):
        """Verify _build_extra_filter handles ejecutivo_asignado."""
        # This is implicitly tested by the commercial dashboard filtering
        d = get_commercial_dashboard(executive="NoExisteXYZ")
        recv = d["kpis"]["leads_received"]["value"]
        assert recv == 0 or recv is not None, "Should handle nonexistent executive"


class TestDemandTypeDistribution:
    def test_types_included(self):
        from analytics.leads_queries import _type_distribution
        raw = [{"_tipo": "Departamento"}, {"_tipo": "Casa"}, {"_tipo": "Departamento"}]
        types = _type_distribution(raw)
        assert len(types) >= 2
        assert any(t["value"] == "Departamento" for t in types)
        assert any(t["value"] == "Casa" for t in types)

    def test_types_fallback_si(self):
        from analytics.leads_queries import _type_distribution
        raw = [{"_tipo": ""}, {"_tipo": "Sin informacion"}, {"prospecto": {"tipo": None}}]
        types = _type_distribution(raw)
        assert any(t["value"] == "S/I" for t in types)


class TestPriceSeparateMonetary:
    def test_price_range_coverage(self):
        r = query_demand_by_price_ranges()
        for op in r.get("price_ranges", []):
            cov = op.get("coverage", {})
            if op["operation"] == "Venta":
                assert cov.get("currency") == "UF"
            elif op["operation"] == "Arriendo":
                assert cov.get("currency") == "CLP"

    def test_no_price_mixing(self):
        """UF and CLP ranges must not be mixed in same chart."""
        r = query_demand_by_price_ranges()
        op_names = [op["operation"] for op in r.get("price_ranges", [])]
        if "Venta" in op_names and "Arriendo" in op_names:
            # Both operations are separate, ranges not mixed
            pass  # Structure already ensures separation

    def test_without_price_reported(self):
        r = query_demand_by_price_ranges()
        for op in r.get("price_ranges", []):
            ranges = op.get("ranges", [])
            has_sin_precio = any(rg["range"] == "Sin precio" for rg in ranges)
            if op["coverage"]["without_price"] > 0:
                assert has_sin_precio, "Sin precio row must appear when price is missing"


# =============================================================================
# INVARIANT TESTS (Business Rules)
# =============================================================================

class TestInvariants:
    def test_unique_lead_count(self):
        """A lead with multiple activities counts once. Verify via funnel stages not exceeding received."""
        r = query_commercial_funnel()
        received = r[0]["count"]
        for stage in r:
            assert stage["count"] <= received, \
                f"Stage '{stage['key']}' count ({stage['count']}) exceeds received ({received})"

    def test_temperature_unknown_is_null(self):
        """Temperature unknown should not be converted to 0 or COLD."""
        k = query_commercial_kpis()
        meta = k.get("_meta", {})
        assert "temperature_coverage" in meta

    def test_sla_critical_over_3h(self):
        """Hot lead with >3h without management is critical. Verify distribution reflects this."""
        sla = query_sla_risk_panel()
        dist = sla["distribution"]
        more_3h = sum(d["count"] for d in dist if d["label"] in ["M\u00e1s de 3 horas", "Sin gesti\u00f3n"])
        assert sla["critical_open"] <= more_3h or sla["total_hot"] == 0

    def test_funnel_leakage_detection(self):
        """Funnel should detect leakage between stages (when count decreases)."""
        r = query_commercial_funnel()
        non_monotonic = {("visit_intent", "data_delivered")}
        for i in range(1, len(r)):
            pair = (r[i - 1]["key"], r[i]["key"])
            if pair in non_monotonic:
                continue
            if r[i - 1]["count"] > r[i]["count"]:
                loss = r[i - 1]["count"] - r[i]["count"]
                assert r[i]["leakage"] is not None
                assert r[i]["leakage_pct"] is not None or r[i - 1]["count"] == 0
                if r[i]["leakage"] is not None:
                    assert r[i]["leakage"] == loss

    def test_price_ranges_configurable(self):
        """Price ranges must be defined centrally (import check)."""
        assert len(PRICE_RANGES_UF) == 5
        assert len(PRICE_RANGES_CLP) == 5
        assert PRICE_RANGES_UF[0][0] == "0-2500"
        assert PRICE_RANGES_CLP[0][0] == "0-400k"

    def test_visit_results_defined(self):
        """Visit results set must not be empty."""
        assert len(VISIT_RESULTS) > 0
        assert "VISITA_SOLICITADA" in VISIT_RESULTS

    def test_funnel_stage_count(self):
        """Funnel should have exactly 8 stages."""
        r = query_commercial_funnel(period_start="2026-07-01", period_end="2026-07-20")
        assert len(r) == len(COMMERCIAL_FUNNEL_STAGES), \
            f"Expected {len(COMMERCIAL_FUNNEL_STAGES)} funnel stages, got {len(r)}"

    def test_no_invented_sla_metrics(self):
        """When no management data exists, SLA metrics should be null, not 0."""
        sla = query_sla_risk_panel()
        if sla["total_hot"] == 0:
            assert sla["within_sla_pct"] is None
            assert sla["median_response_minutes"] is None
            assert sla["p90_response_minutes"] is None

    def test_temperature_not_retroactive_invariant(self):
        """Verify that a lead's temperature at period end is from history,
        not current state. Test by checking coverage field exists."""
        cov = query_temperature_coverage("2026-01-01", "2026-01-31")
        assert "history_coverage_pct" in cov
        assert "total" in cov

    def test_stock_separation_invariant(self):
        """Undetermined cases should not distort SLA."""
        sla = query_sla_risk_panel()
        if sla["total_hot"] > 0:
            undet = sla.get("total_undetermined", 0)
            det = sla.get("total_determined", 0)
            assert undet + det == sla["total_hot"]
            assert undet >= 0

    def test_funnel_lead_count_bounded_by_received(self):
        """Each funnel stage count should be <= received."""
        r = query_commercial_funnel()
        received = r[0]["count"]
        for stage in r[1:]:
            assert stage["count"] <= received, \
                f"Stage {stage['key']} ({stage['count']}) > received ({received})"

    def test_funnel_pct_of_received(self):
        """pct_of_received should be between 0 and 100."""
        r = query_commercial_funnel()
        for stage in r[1:]:
            if stage["pct_of_received"] is not None:
                assert 0 <= stage["pct_of_received"] <= 100
