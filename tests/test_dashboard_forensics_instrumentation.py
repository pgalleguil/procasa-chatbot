import ast
from pathlib import Path

from analytics import dashboard_forensics
from analytics.dashboard_forensics import background_active, background_count, begin_background, end_background, key_hash, process_facts


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
    assert '_schedule_cache_refresh(key, load, source="request_swr")' in SERVICE
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


def test_background_tracking_uses_refcount(monkeypatch):
    key = "forensics-refcount-test"
    events = []
    monkeypatch.setattr(dashboard_forensics, "emit", lambda prefix, payload: events.append(payload))
    started = 0.0

    begin_background(key, "background_refresh_started", "operations", source="keeper")
    begin_background(key, "background_refresh_started", "operations", source="startup_prewarm")
    assert background_active(key) is True
    assert background_count(key) == 2

    end_background(key, "operations", "background_refresh_finished", started, source="keeper")
    assert background_active(key) is True
    assert background_count(key) == 1

    end_background(key, "operations", "background_refresh_finished", started, source="startup_prewarm")
    assert background_active(key) is False
    assert background_count(key) == 0
    assert [event["background_active_count"] for event in events] == [1, 2, 1, 0]


def test_background_events_have_controlled_sources(monkeypatch):
    events = []
    monkeypatch.setattr(dashboard_forensics, "emit", lambda prefix, payload: events.append(payload))
    for index, source in enumerate(("startup_prewarm", "keeper", "request_swr")):
        key = f"forensics-source-test-{index}"
        begin_background(key, "background_refresh_started", "operations", source=source)
        end_background(key, "operations", "background_refresh_finished", 0.0, source=source)

    assert [event["source"] for event in events] == [
        "startup_prewarm", "startup_prewarm",
        "keeper", "keeper",
        "request_swr", "request_swr",
    ]


def test_refresh_reuses_one_age_before_for_start_and_finish(monkeypatch):
    from analytics import leads_service

    events = []
    ages = iter((300.0, 0.0))

    class ImmediatePool:
        def submit(self, fn):
            fn()
            return None

    monkeypatch.setattr(leads_service, "_CACHE_REFRESH_POOL", ImmediatePool())
    monkeypatch.setattr(leads_service, "_cache_entry_age", lambda key: next(ages))
    monkeypatch.setattr(leads_service, "_cache_set", lambda key, value: None)
    monkeypatch.setattr(
        leads_service,
        "begin_background",
        lambda key, event, endpoint, age_before, **kwargs: events.append(
            {"event": "started", "age_before": age_before, "source": kwargs["source"]}
        ),
    )
    monkeypatch.setattr(
        leads_service,
        "end_background",
        lambda key, endpoint, event, started, age_before, **kwargs: events.append(
            {"event": "finished", "age_before": age_before, "source": kwargs["source"]}
        ),
    )

    leads_service._CACHE_REFRESHING.clear()
    assert leads_service._schedule_cache_refresh(
        "operations:30d", lambda: {"ok": True}, source="keeper"
    ) is True
    assert events == [
        {"event": "started", "age_before": 300.0, "source": "keeper"},
        {"event": "finished", "age_before": 300.0, "source": "keeper"},
    ]


def test_refresh_callers_emit_startup_keeper_and_request_sources(monkeypatch):
    from analytics import leads_service

    sources = []
    monkeypatch.setattr(
        leads_service,
        "begin_background",
        lambda *args, **kwargs: sources.append(kwargs["source"]),
    )
    monkeypatch.setattr(leads_service, "end_background", lambda *args, **kwargs: None)
    monkeypatch.setattr(leads_service, "_cache_set", lambda key, value: None)
    monkeypatch.setattr(leads_service, "_pinned_dashboard_jobs", lambda: [
        ("operations", "operations:30d", lambda: {"ok": True}),
    ])
    monkeypatch.setattr(leads_service, "_cache_entry_age", lambda key: None)

    leads_service.warm_pinned_dashboard_cache()
    assert sources == ["startup_prewarm"]

    monkeypatch.setattr(leads_service, "_cache_entry_age", lambda key: 300.0)
    monkeypatch.setattr(
        leads_service,
        "_schedule_cache_refresh",
        lambda key, loader, **kwargs: sources.append(kwargs["source"]) or True,
    )
    leads_service.keep_pinned_dashboard_cache()
    assert sources[-1] == "keeper"

    monkeypatch.setattr(leads_service, "_pinned_dashboard_jobs", lambda: [])
    monkeypatch.setattr(
        leads_service,
        "_cache_state",
        lambda key: ({"meta": {}}, 300.0, "soft"),
    )
    leads_service.get_leads_dashboard_overview(
        period_start="2026-01-01",
        period_end="2026-01-02",
        timing={},
    )
    assert sources[-1] == "request_swr"
