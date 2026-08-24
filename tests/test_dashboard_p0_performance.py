import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from analytics import leads_service


TEMPLATE = (Path(__file__).parents[1] / "templates" / "leads_dashboard.html").read_text(encoding="utf-8")
WEBHOOK = (Path(__file__).parents[1] / "webhook.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def clear_p0_state():
    leads_service.L1_CACHE.clear()
    with leads_service._SINGLEFLIGHT_LOCK:
        leads_service._SINGLEFLIGHT.clear()
    with leads_service._CACHE_REFRESH_LOCK:
        leads_service._CACHE_REFRESHING.clear()
    yield
    leads_service.L1_CACHE.clear()
    with leads_service._SINGLEFLIGHT_LOCK:
        leads_service._SINGLEFLIGHT.clear()
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
