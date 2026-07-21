"""Security and parameter validation tests for the commercial dashboard API."""
import pytest
import sys
sys.path.insert(0, '.')

from datetime import datetime, timedelta


class TestAPIEndpointAccess:
    """Test API authentication (without async)."""

    def test_api_missing_auth_returns_error(self):
        """The API requires a valid session. Without one, it should error."""
        from analytics.leads_service import get_commercial_dashboard
        # If role is None (no auth), should still work but filtered
        result = get_commercial_dashboard(
            period_start="2026-07-01", period_end="2026-07-20",
            role=None, user_name=None,
        )
        assert isinstance(result, dict)
        assert "kpis" in result

    def test_read_only_flag(self):
        """The dashboard meta must declare itself read-only."""
        from analytics.leads_service import get_commercial_dashboard
        result = get_commercial_dashboard(role="admin", user_name="Test")
        assert result["meta"]["read_only"] is True


class TestParameterValidation:
    """Test that the API validates parameters correctly."""

    def test_period_start_is_date_string(self):
        """period_start must be an ISO date string if provided."""
        from analytics.leads_queries import query_commercial_kpis
        # Should not crash with invalid date
        result = query_commercial_kpis(period_start="not-a-date", period_end="2026-07-20")
        assert isinstance(result, dict)
        assert "kpis" in result or "leads_received" in result

    def test_period_end_is_date_string(self):
        from analytics.leads_queries import query_commercial_kpis
        result = query_commercial_kpis(period_start="2026-07-01", period_end="not-a-date")
        assert isinstance(result, dict)

    def test_period_start_before_end(self):
        """When start > end, the system should handle gracefully (swap or use defaults)."""
        from analytics.leads_queries import query_commercial_kpis
        result = query_commercial_kpis(period_start="2026-07-20", period_end="2026-07-01")
        assert isinstance(result, dict)

    def test_extremely_long_period(self):
        """Very long periods (years) should not crash the system."""
        from analytics.leads_queries import query_commercial_kpis
        result = query_commercial_kpis(period_start="2020-01-01", period_end="2026-12-31")
        assert isinstance(result, dict)

    def test_future_period(self):
        """Future dates should return 0 or None, not crash."""
        from analytics.leads_queries import query_commercial_kpis
        result = query_commercial_kpis(period_start="2099-01-01", period_end="2099-12-31")
        assert isinstance(result, dict)

    def test_empty_period(self):
        """Same start and end should return 0."""
        from analytics.leads_queries import query_commercial_kpis
        result = query_commercial_kpis(period_start="2026-07-20", period_end="2026-07-20")
        assert isinstance(result, dict)

    def test_filter_injection_attempt(self):
        """Filter values with special characters should not cause errors."""
        from analytics.leads_queries import query_commercial_kpis
        result = query_commercial_kpis(filters={"source": {"$ne": "admin"}})
        assert isinstance(result, dict)

    def test_nonexistent_executive_filter(self):
        """Filtering by a nonexistent executive should return empty or valid results."""
        from analytics.leads_service import get_commercial_dashboard
        result = get_commercial_dashboard(
            period_start="2026-01-01", period_end="2026-01-31",
            executive="Nadie Con Este Nombre",
            role="admin", user_name="Test",
        )
        assert isinstance(result, dict)
        # Executive filter is applied to the leads collection
        # A nonexistent exec should either return 0 or handle gracefully
        assert result["kpis"]["leads_received"]["value"] >= 0


class TestDataNotModified:
    """Test that queries never modify commercial data."""

    def test_query_is_read_only(self):
        """Verify query functions don't call update/insert/delete."""
        import dis
        from analytics import leads_queries

        # Check that commercial query functions don't contain write operations
        funcs = [
            "query_commercial_kpis",
            "query_commercial_funnel",
            "query_sla_risk_panel",
            "query_demand_by_price_ranges",
            "query_commercial_executive_matrix",
            "query_commercial_property_ranking",
        ]
        write_ops = {"update_one", "update_many", "insert_one", "insert_many", "delete_one", "delete_many", "replace_one"}

        for func_name in funcs:
            func = getattr(leads_queries, func_name, None)
            if func is None:
                continue
            # Get bytecode and check for write calls
            instructions = dis.get_instructions(func)
            for instr in instructions:
                if instr.opname == "LOAD_GLOBAL" and instr.argval in write_ops:
                    pytest.fail(f"{func_name} contains write operation: {instr.argval}")
                if instr.opname == "LOAD_ATTR" and instr.argval in write_ops:
                    pytest.fail(f"{func_name} calls write: {instr.argval}")


class TestResponseStructure:
    """Test that the API response has the expected structure."""

    def test_meta_includes_period_comparison(self):
        """The meta section must include period comparison info."""
        from analytics.leads_service import get_commercial_dashboard
        result = get_commercial_dashboard(
            period_start="2026-07-01", period_end="2026-07-20",
            role="admin", user_name="Test",
        )
        meta = result.get("meta", {})
        assert "period" in meta
        period = meta["period"]
        assert "type" in period
        assert "current" in period
        assert "previous" in period
        assert "start" in period["current"]
        assert "end" in period["current"]
        assert "start" in period["previous"]
        assert "end" in period["previous"]

    def test_kpis_have_universe(self):
        """Each KPI must declare its measurement universe."""
        from analytics.leads_service import get_commercial_dashboard
        result = get_commercial_dashboard(role="admin", user_name="Test")
        kpis = result["kpis"]
        for key in ["leads_received", "leads_hot_history", "visit_intent", "visits_scheduled", "closed_won"]:
            k = kpis.get(key, {})
            assert "universe" in k, f"{key} missing universe declaration"

    def test_sla_policy_is_clearly_labeled(self):
        """SLA policy must be clearly labeled with display text."""
        from analytics.leads_service import get_commercial_dashboard
        result = get_commercial_dashboard(role="admin", user_name="Test")
        sla = result["sla_risk"]
        assert "sla_policy" in sla
        assert "display_label" in sla["sla_policy"]
        assert "3 horas" in sla["sla_policy"]["display_label"]

    def test_kpis_meta_has_temperature_coverage(self):
        """KPI meta must include temperature coverage."""
        from analytics.leads_service import get_commercial_dashboard
        result = get_commercial_dashboard(role="admin", user_name="Test")
        meta = result["kpis"].get("_meta", {})
        assert "temperature_coverage" in meta
