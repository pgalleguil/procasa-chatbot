import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from analytics import leads_queries
from analytics import leads_service


TEMPLATE = (Path(__file__).parents[1] / "templates" / "leads_dashboard.html").read_text(encoding="utf-8")
WEBHOOK = (Path(__file__).parents[1] / "webhook.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def clear_p0_state():
    leads_service.L1_CACHE.clear()
    with leads_service._SINGLEFLIGHT_LOCK:
        leads_service._SINGLEFLIGHT.clear()
    with leads_service._DASHBOARD_UF_LOCK:
        leads_service._DASHBOARD_UF_CACHE = None
        leads_service._DASHBOARD_UF_INFLIGHT = None
    leads_queries._OPS_SIGNED_ORDERS_CACHE = None
    leads_queries._OPS_SIGNED_ORDERS_CACHE_AT = 0.0
    leads_queries._OPS_SIGNED_ORDERS_INFLIGHT = None
    leads_queries._OPS_ASSIGNMENT_EPISODE_CACHE.clear()
    leads_queries._OPS_ASSIGNMENT_EPISODE_INFLIGHT.clear()
    with leads_service._CACHE_REFRESH_LOCK:
        leads_service._CACHE_REFRESHING.clear()
    yield
    leads_service.L1_CACHE.clear()
    with leads_service._SINGLEFLIGHT_LOCK:
        leads_service._SINGLEFLIGHT.clear()
    with leads_service._DASHBOARD_UF_LOCK:
        leads_service._DASHBOARD_UF_CACHE = None
        leads_service._DASHBOARD_UF_INFLIGHT = None
    leads_queries._OPS_SIGNED_ORDERS_CACHE = None
    leads_queries._OPS_SIGNED_ORDERS_CACHE_AT = 0.0
    leads_queries._OPS_SIGNED_ORDERS_INFLIGHT = None
    leads_queries._OPS_ASSIGNMENT_EPISODE_CACHE.clear()
    leads_queries._OPS_ASSIGNMENT_EPISODE_INFLIGHT.clear()
    with leads_service._CACHE_REFRESH_LOCK:
        leads_service._CACHE_REFRESHING.clear()


def test_controlled_five_concurrent_callers_share_one_loader_and_exact_result():
    calls = []
    release = threading.Event()
    barrier = threading.Barrier(5)
    timings = []
    payload = {"same": [1, 2, 3]}

    def loader():
        calls.append(1)
        release.wait(2)
        return payload

    def call():
        barrier.wait()
        timing = {}
        result = leads_service._singleflight_compute("benchmark-key", loader, timing=timing)
        timings.append(timing)
        return result

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(call) for _ in range(5)]
        time.sleep(0.05)
        release.set()
        results = [future.result(timeout=2) for future in futures]

    assert len(calls) == 1
    assert all(result is payload for result in results)
    assert sum(item["singleflight_role"] == "owner" for item in timings) == 1
    assert sum(item["singleflight_role"] == "waiter" for item in timings) == 4
    assert all(item["singleflight_shared"] is (item["singleflight_role"] == "waiter") for item in timings)
    assert all(item["singleflight_wait_ms"] >= 0 for item in timings)


def test_singleflight_exception_is_shared_and_registry_is_clean():
    calls = []
    barrier = threading.Barrier(5)
    timings = []

    def loader():
        calls.append(1)
        time.sleep(0.1)
        raise ValueError("shared failure")

    def call():
        barrier.wait()
        timing = {}
        try:
            leads_service._singleflight_compute("error-key", loader, timing=timing)
        except Exception as exc:
            timings.append(timing)
            return type(exc), str(exc)
        return None

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = [future.result(timeout=2) for future in [pool.submit(call) for _ in range(5)]]

    assert len(calls) == 1
    assert results == [(ValueError, "shared failure")] * 5
    assert all(item["singleflight_role"] in {"owner", "waiter"} for item in timings)
    with leads_service._SINGLEFLIGHT_LOCK:
        assert "error-key" not in leads_service._SINGLEFLIGHT


def test_startup_keeper_and_request_share_one_inflight_calculation():
    calls = []
    release = threading.Event()

    def loader():
        calls.append(1)
        release.wait(2)
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        startup = pool.submit(leads_service._singleflight_compute, "pinned-key", loader)
        deadline = time.time() + 1
        while len(calls) != 1 and time.time() < deadline:
            time.sleep(0.005)
        request = pool.submit(leads_service._singleflight_compute, "pinned-key", loader)
        time.sleep(0.05)
        release.set()
        assert startup.result(timeout=2) == request.result(timeout=2) == {"ok": True}
    assert len(calls) == 1


def test_stale_response_returns_without_waiting_for_background_refresh(monkeypatch):
    key = leads_service._overview_request_key("2026-08-01", "2026-08-02", "auto", "custom")
    payload = {"meta": {}, "value": 1}
    leads_service.L1_CACHE[key] = (time.time() - leads_service.CACHE_TTL - 1, payload)
    scheduled = []
    monkeypatch.setattr(leads_service, "_pinned_dashboard_jobs", lambda: [])
    monkeypatch.setattr(leads_service, "_schedule_cache_refresh", lambda *args, **kwargs: scheduled.append(kwargs["source"]) or True)
    started = time.perf_counter()
    result = leads_service.get_leads_dashboard_overview(period_start="2026-08-01", period_end="2026-08-02", timing={})
    elapsed = time.perf_counter() - started
    assert result["value"] == 1
    assert scheduled == ["request_swr"]
    assert elapsed < 0.2


def test_pinned_entries_are_not_evicted_while_unpinned_entries_exist(monkeypatch):
    pinned = {"pinned-1", "pinned-2", "pinned-3"}
    monkeypatch.setattr(leads_service, "MAX_CACHE_ENTRIES", 5)
    monkeypatch.setattr(leads_service, "_pinned_dashboard_jobs", lambda: [(key, key, lambda: {}) for key in pinned])
    for key in ("unpinned-old", "unpinned-new", *sorted(pinned)):
        leads_service._cache_set(key, {"key": key})
    leads_service._cache_set("incoming", {"key": "incoming"})
    assert pinned.issubset(leads_service.L1_CACHE)
    assert "unpinned-old" not in leads_service.L1_CACHE


def test_frontend_p0_contract_has_tab_matrix_dedupe_abort_and_no_hidden_cross_loads():
    assert "if (activeTab === 'leads') return loadOperationalData();" in TEMPLATE
    assert "if (activeTab === 'properties') return loadInventoryData(mode === 'refresh');" in TEMPLATE
    assert "if (activeTab !== 'executive') return null;" in TEMPLATE
    assert "const operationalPromise" not in TEMPLATE
    assert "dashboardBeginRequest('overview'" in TEMPLATE
    assert "dashboardBeginRequest('operations'" in TEMPLATE
    assert "dashboardBeginRequest('properties'" in TEMPLATE
    assert TEMPLATE.count("new AbortController()") == 1
    assert TEMPLATE.count("signal:request.controller.signal") == 3
    assert TEMPLATE.count("error?.name === 'AbortError'") == 3
    assert "if (INITIAL_TAB === 'executive') loadData('full');" in TEMPLATE


def test_startup_warm_blocks_lifespan_and_keeper_has_no_duplicate_initial_warm():
    assert "await asyncio.wait_for(" in WEBHOOK
    assert "startup_loop.run_in_executor(_WARMER_THREAD_POOL, warm_pinned_dashboard_cache)" in WEBHOOK
    warmer = WEBHOOK[WEBHOOK.index("async def cache_prewarmer_loop"):WEBHOOK.index("async def event_loop_monitor_loop")]
    assert "warm_pinned_dashboard_cache" not in warmer
    assert "keep_pinned_dashboard_cache" in warmer


def test_standard_pinned_set_is_eight_overview_operations_keys_and_excludes_properties():
    jobs = leads_service._pinned_dashboard_jobs()
    assert len(jobs) == 8
    assert [endpoint for endpoint, _, _ in jobs].count("overview") == 4
    assert [endpoint for endpoint, _, _ in jobs].count("operations") == 4
    assert all(endpoint != "properties" for endpoint, _, _ in jobs)
    assert {"today", "week", "month", "30d"}.issubset({key.split("preset=", 1)[1].split("|", 1)[0] for _, key, _ in jobs})


def test_startup_warm_attempts_each_standard_key_once_and_continues_after_failure(monkeypatch):
    jobs = tuple((endpoint, f"{endpoint}:preset={preset}", (lambda endpoint=endpoint, preset=preset: {endpoint: preset}))
                 for preset in ("today", "week", "month", "30d")
                 for endpoint in ("overview", "operations"))
    calls = []
    monkeypatch.setattr(leads_service, "_pinned_dashboard_jobs", lambda: jobs)
    monkeypatch.setattr(leads_service, "_cache_entry_age", lambda key: None)
    monkeypatch.setattr(leads_service, "begin_background", lambda *args, **kwargs: None)
    monkeypatch.setattr(leads_service, "end_background", lambda *args, **kwargs: None)
    monkeypatch.setattr(leads_service, "emit_forensics", lambda *args, **kwargs: None)
    original_singleflight = leads_service._singleflight_compute

    def compute(key, loader, **kwargs):
        calls.append(key)
        if key == "overview:preset=week":
            raise RuntimeError("one warm failed")
        return original_singleflight(key, loader, **kwargs)

    monkeypatch.setattr(leads_service, "_singleflight_compute", compute)
    warmed = leads_service.warm_pinned_dashboard_cache()
    assert len(calls) == 8
    assert calls == [key for _, key, _ in jobs]
    assert len(warmed) == 7


def test_keeper_schedules_at_most_two_oldest_with_overview_operations_preference(monkeypatch):
    jobs = tuple((endpoint, f"{endpoint}:preset={preset}", lambda: {})
                 for preset in ("today", "week", "month", "30d")
                 for endpoint in ("overview", "operations"))
    ages = {key: age for (_, key, _), age in zip(jobs, (100, 400, 500, 600, 300, 200, 50, 250))}
    scheduled = []
    monkeypatch.setattr(leads_service, "_pinned_dashboard_jobs", lambda: jobs)
    monkeypatch.setattr(leads_service, "_cache_entry_age", lambda key: ages[key])
    monkeypatch.setattr(leads_service, "_schedule_cache_refresh", lambda key, loader, **kwargs: scheduled.append(key) or True)
    assert len(leads_service.keep_pinned_dashboard_cache()) == 2
    assert scheduled == ["overview:preset=week", "operations:preset=week"]


def test_hard_stale_standard_returns_stale_without_sync_compute(monkeypatch):
    month_start, month_end = next(
        (start, end)
        for preset, start, end in leads_service._dashboard_standard_periods()
        if preset == "month"
    )
    key = leads_service._overview_request_key(month_start, month_end, "auto", "month")
    payload = {"meta": {}, "value": "cached"}
    leads_service.L1_CACHE[key] = (time.time() - leads_service.CACHE_HARD_TTL - 1, payload)
    monkeypatch.setattr(leads_service, "_pinned_dashboard_jobs", lambda: [("overview", key, lambda: (_ for _ in ()).throw(AssertionError("sync compute")))])
    refreshes = []
    monkeypatch.setattr(leads_service, "_schedule_cache_refresh", lambda *args, **kwargs: refreshes.append(kwargs["source"]) or True)
    result = leads_service.get_leads_dashboard_overview(period_start=month_start, period_end=month_end, compare="auto", period_preset="month")
    assert result["meta"]["data_status"] == "stale"
    assert refreshes == ["request_swr"]


def test_dashboard_uf_cache_reuses_value_expires_and_does_not_cache_failure(monkeypatch):
    calls = []
    values = [None, {"valor": 38000, "fecha": "2026-08-24", "fuente": "test"}, {"valor": 39000, "fecha": "2026-08-25", "fuente": "test"}]
    monkeypatch.setattr(leads_service, "_read_dashboard_uf_source", lambda: calls.append(1) or values.pop(0))
    assert leads_service._get_dashboard_uf() is None
    first = leads_service._get_dashboard_uf()
    second = leads_service._get_dashboard_uf()
    assert first == second
    assert set(first) == {"valor", "fecha", "fuente"}
    assert len(calls) == 2
    leads_service._DASHBOARD_UF_CACHE = (time.time() - leads_service.UF_DASHBOARD_CACHE_TTL - 1, first)
    expired = leads_service._get_dashboard_uf()
    assert expired["valor"] == 39000
    assert len(calls) == 3


def test_operations_signed_orders_resource_is_reused_across_month_and_week(monkeypatch):
    calls = []
    raw = [{"visita_code": "V-1", "timeline": {"signed_at": "2026-08-01"}}]
    monkeypatch.setattr(
        leads_queries,
        "_ops_fetch_signed_orders",
        lambda *args, **kwargs: calls.append(1) or raw,
    )

    month_before = leads_queries._ops_fetch_signed_orders_cached(object(), {})
    week_before = leads_queries._ops_fetch_signed_orders_cached(object(), {})
    month_after = leads_queries._ops_fetch_signed_orders_cached(object(), {})
    week_after = leads_queries._ops_fetch_signed_orders_cached(object(), {})

    assert calls == [1]
    assert month_before == month_after
    assert week_before == week_after
    assert month_before == week_before


def test_operations_assignment_resource_uses_superset_and_is_reused_across_month_and_week(monkeypatch):
    calls = []
    historical_base = [{
        "_id": "historical",
        "lifecycle": {"assigned_at": "2026-07-01", "first_valid_management_at": "2026-06-30"},
    }]
    current_docs = [{
        "_id": "current",
        "lifecycle": {"assigned_at": "2026-08-01", "first_valid_management_at": "2026-07-31"},
    }]
    superset = historical_base + current_docs
    assignment_payload = {"historical": [{"assignment_cycle_id": "H-1"}], "current": [{"assignment_cycle_id": "C-1"}]}
    monkeypatch.setattr(
        leads_queries,
        "_ops_assignment_episode_map",
        lambda *args, **kwargs: calls.append(1) or assignment_payload,
    )

    month_before = leads_queries._ops_assignment_episode_map_cached(object(), superset, {})
    week_before = leads_queries._ops_assignment_episode_map_cached(object(), list(reversed(superset)), {})
    month_after = leads_queries._ops_assignment_episode_map_cached(object(), superset, {})
    week_after = leads_queries._ops_assignment_episode_map_cached(object(), list(reversed(superset)), {})

    assert calls == [1]
    assert month_before == month_after
    assert week_before == week_after
    assert month_before == week_before
