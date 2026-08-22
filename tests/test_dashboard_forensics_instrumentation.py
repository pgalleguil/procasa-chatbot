import ast
from pathlib import Path

from analytics.dashboard_forensics import key_hash, process_facts


ROOT = Path(__file__).parents[1]
SERVICE = (ROOT / "analytics" / "leads_service.py").read_text(encoding="utf-8")
WEBHOOK = (ROOT / "webhook.py").read_text(encoding="utf-8")


def test_dashboard_forensics_key_hash_and_process_facts_are_aggregate_only():
    raw = "leads-properties-inventory:ps=2020-01-01|phone=never-log-this"
    assert len(key_hash(raw)) == 12
    facts = process_facts()
    assert isinstance(facts["pid"], int)
    assert facts["process_uptime_seconds"] >= 0
    assert "phone" not in facts
    assert "email" not in facts


def test_miss_loaders_receive_request_timing_and_hits_report_zero_mongo():
    tree = ast.parse(SERVICE)
    assert "_compute_properties_inventory_dashboard(period_start, period_end, filters, timing=load_timing)" in SERVICE
    assert "filters=filters, timing=load_timing, _cache_bypass=True" in SERVICE
    assert "timing.update({\"cache\": \"HIT\"" in SERVICE
    assert any(isinstance(node, ast.FunctionDef) and node.name == "_record_cache_diagnostics" for node in ast.walk(tree))


def test_stale_paths_schedule_refresh_without_sync_compute():
    assert "if state == \"soft\":" in SERVICE
    assert "_schedule_cache_refresh(key, load)" in SERVICE
    assert "if state == \"hard\" and pinned" in SERVICE
    assert "result = load(timing)" in SERVICE


def test_web_pool_queue_and_execution_are_measured_inside_worker():
    assert "submitted_at = time.perf_counter()" in WEBHOOK
    assert "worker_started = time.perf_counter()" in WEBHOOK
    assert 'timing["web_pool_queue_wait_ms"]' in WEBHOOK
    assert 'timing["web_pool_execution_ms"]' in WEBHOOK
    assert 'timing["worker_thread"] = threading.current_thread().name' in WEBHOOK


def test_overview_records_components_queue_and_signed_order_barrier():
    for field in ("queue_wait_ms", "execution_ms", "started_offset_ms", "signed_orders_barrier_wait_ms", "analytics_pool_queue_wait_ms"):
        assert field in SERVICE
    for component in ("shared_signed_orders", "demand_trend", "valuation_pipeline", "sla", "funnel", "demand_coverage", "property_commission", "conversion", "sources"):
        assert f'"{component}"' in SERVICE


def test_cache_eviction_and_request_logs_use_hashes():
    forensic = (ROOT / "analytics" / "dashboard_forensics.py").read_text(encoding="utf-8")
    assert "emit_cache_evict" in SERVICE
    assert '"evicted_key_hash"' in forensic
    assert '"cache_key_hash"' in WEBHOOK
    assert '"request_id"' in WEBHOOK


def test_frontend_perceived_latency_is_observable_without_abort_or_ux_changes():
    template = (ROOT / "templates" / "leads_dashboard.html").read_text(encoding="utf-8")
    assert "DASHBOARD_FRONTEND_PERF" in template
    assert "fetch_ms" in template and "render_ms" in template and "perceived_ms" in template
    assert "AbortController" not in template
