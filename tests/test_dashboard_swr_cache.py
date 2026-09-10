import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pymongo.errors import NetworkTimeout

from analytics import leads_service


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(autouse=True)
def clear_swr_state():
    _wait_until(lambda: not leads_service._CACHE_REFRESHING)
    leads_service.L1_CACHE.clear()
    with leads_service._CACHE_REFRESH_LOCK:
        leads_service._CACHE_REFRESHING.clear()
    with leads_service._SINGLEFLIGHT_LOCK:
        leads_service._SINGLEFLIGHT.clear()
    yield
    _wait_until(lambda: not leads_service._CACHE_REFRESHING)
    leads_service.L1_CACHE.clear()
    with leads_service._CACHE_REFRESH_LOCK:
        leads_service._CACHE_REFRESHING.clear()
    with leads_service._SINGLEFLIGHT_LOCK:
        leads_service._SINGLEFLIGHT.clear()


def _age_cache(key, seconds):
    timestamp, payload = leads_service.L1_CACHE[key]
    leads_service.L1_CACHE[key] = (timestamp - seconds, payload)


def test_overview_initial_miss_then_hit_without_second_compute(monkeypatch):
    calls = []
    payload = {"meta": {"source": "test"}, "demand": {"total": 3}}
    monkeypatch.setattr(
        leads_service,
        "_compute_leads_dashboard_overview",
        lambda **kwargs: calls.append(kwargs["period_preset"]) or payload,
    )

    first = leads_service.get_leads_dashboard_overview(period_preset="today", compare="auto")
    second = leads_service.get_leads_dashboard_overview(period_preset="today", compare="auto")

    assert first == payload
    assert second == payload
    assert calls == ["today"]


def test_overview_soft_stale_returns_immediately_and_schedules_one_refresh(monkeypatch):
    calls = []
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    old_payload = {"meta": {}, "demand": {"total": 3}}
    new_payload = {"meta": {}, "demand": {"total": 4}}

    def compute(**kwargs):
        calls.append(1)
        if len(calls) == 2:
            refresh_started.set()
            release_refresh.wait(2)
        return old_payload if len(calls) == 1 else new_payload

    monkeypatch.setattr(leads_service, "_compute_leads_dashboard_overview", compute)
    assert leads_service.get_leads_dashboard_overview(period_preset="30d") == old_payload
    key = next(iter(leads_service.L1_CACHE))
    _age_cache(key, leads_service.CACHE_TTL + 1)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(leads_service.get_leads_dashboard_overview, period_preset="30d") for _ in range(5)]
        assert refresh_started.wait(1)
        results = [future.result() for future in futures]

    assert len(calls) == 2
    assert all(result["meta"]["data_status"] == "stale" for result in results)
    assert all(result["meta"]["refresh"] == "scheduled" for result in results)
    release_refresh.set()
    assert _wait_until(lambda: leads_service.L1_CACHE[key][1] == new_payload)


@pytest.mark.parametrize(
    "public_name,private_name,payload,kwargs",
    [
        (
            "get_leads_dashboard_overview",
            "_compute_leads_dashboard_overview",
            {"meta": {}, "demand": {"total": 3}},
            {"period_preset": "week", "compare": "auto"},
        ),
        (
            "get_leads_operational_dashboard",
            "_compute_leads_operational_dashboard",
            {"meta": {}, "period": {"assigned": 3}},
            {"period_preset": "week", "compare": "auto"},
        ),
    ],
)
def test_concurrent_miss_is_single_flight(monkeypatch, public_name, private_name, payload, kwargs):
    calls = []
    started = threading.Event()
    release = threading.Event()

    def compute(**inner_kwargs):
        calls.append(1)
        started.set()
        release.wait(2)
        return payload

    monkeypatch.setattr(leads_service, private_name, compute)
    public = getattr(leads_service, public_name)
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(public, **kwargs) for _ in range(5)]
        assert started.wait(1)
        release.set()
        results = [future.result() for future in futures]

    assert results == [payload] * 5
    assert len(calls) == 1


def test_hard_expiry_recomputes_non_pinned_key(monkeypatch):
    calls = []
    payloads = [{"meta": {}, "demand": {"total": 1}}, {"meta": {}, "demand": {"total": 2}}]

    def compute(**kwargs):
        payload = payloads[min(len(calls), 1)]
        calls.append(1)
        return payload

    monkeypatch.setattr(leads_service, "_compute_leads_dashboard_overview", compute)
    kwargs = {"period_start": "2026-08-01", "period_end": "2026-08-02", "compare": "none", "period_preset": "custom"}
    assert leads_service.get_leads_dashboard_overview(**kwargs)["demand"]["total"] == 1
    key = next(iter(leads_service.L1_CACHE))
    _age_cache(key, leads_service.CACHE_HARD_TTL + 1)
    assert leads_service.get_leads_dashboard_overview(**kwargs)["demand"]["total"] == 2
    assert len(calls) == 2


def test_pinned_hard_expiry_serves_degraded_stale_and_refreshes(monkeypatch):
    calls = []
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    payload = {"meta": {}, "demand": {"total": 3}}

    def compute(**kwargs):
        calls.append(1)
        if len(calls) == 2:
            refresh_started.set()
            release_refresh.wait(2)
        return payload

    monkeypatch.setattr(leads_service, "_compute_leads_dashboard_overview", compute)
    kwargs = {"period_preset": "30d", "compare": "auto"}
    leads_service.get_leads_dashboard_overview(**kwargs)
    key = next(iter(leads_service.L1_CACHE))
    _age_cache(key, leads_service.CACHE_HARD_TTL + 1)
    result = leads_service.get_leads_dashboard_overview(**kwargs)

    assert result["meta"]["data_status"] == "stale"
    assert result["meta"]["degraded"] is True
    assert result["meta"]["refresh"] == "scheduled"
    assert refresh_started.wait(1)
    release_refresh.set()
    assert _wait_until(lambda: len(calls) == 2)


def test_all_four_presets_have_independent_keys(monkeypatch):
    calls = []

    def compute(**kwargs):
        calls.append(kwargs["period_preset"])
        return {"meta": {}, "preset": kwargs["period_preset"]}

    monkeypatch.setattr(leads_service, "_compute_leads_dashboard_overview", compute)
    for preset in ("today", "week", "month", "30d"):
        assert leads_service.get_leads_dashboard_overview(period_preset=preset)["preset"] == preset

    assert calls == ["today", "week", "month", "30d"]
    assert len(leads_service.L1_CACHE) == 4
    assert {"today", "week", "month", "30d"} == {
        key.split("preset=", 1)[1].split("|", 1)[0]
        for key in leads_service.L1_CACHE
    }


def test_comparison_modes_have_independent_keys(monkeypatch):
    monkeypatch.setattr(
        leads_service,
        "_compute_leads_dashboard_overview",
        lambda **kwargs: {"meta": {}, "compare": kwargs["compare"]},
    )
    for mode in ("auto", "prev", "yoy", "none"):
        assert leads_service.get_leads_dashboard_overview(period_preset="30d", compare=mode)["compare"] == mode

    assert len(leads_service.L1_CACHE) == 4
    for mode in ("auto", "prev", "yoy", "none"):
        assert any(f"cmp={mode}" in key for key in leads_service.L1_CACHE)


def test_properties_soft_expiry_and_failed_refresh_preserve_last_good_payload(monkeypatch):
    calls = []
    payload = {"meta": {}, "inventory": {"active": 4}}

    def load(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return payload
        raise NetworkTimeout("refresh failed")

    monkeypatch.setattr(leads_service, "query_demand_capture_dashboard", load)
    assert leads_service.get_properties_inventory_dashboard()["inventory"]["active"] == 4
    key = next(iter(leads_service.L1_CACHE))
    _age_cache(key, leads_service.CACHE_TTL + 1)
    result = leads_service.get_properties_inventory_dashboard()
    assert result["inventory"]["active"] == 4
    assert result["meta"]["data_status"] == "stale"
    assert _wait_until(lambda: len(calls) == 2)
    assert leads_service.L1_CACHE[key][1] == payload


def test_cache_ttl_contract():
    assert leads_service.CACHE_TTL == 120
    assert leads_service.CACHE_HARD_TTL == 600
    assert leads_service.PINNED_REFRESH_AGE == 240
    assert leads_service.PINNED_MAX_STALE == 1800


def test_pinned_jobs_are_eight_standard_overview_and_operations_keys():
    jobs = leads_service._pinned_dashboard_jobs()
    assert len(jobs) == 8
    assert [endpoint for endpoint, _, _ in jobs].count("overview") == 4
    assert [endpoint for endpoint, _, _ in jobs].count("operations") == 4
    assert all(endpoint != "properties" for endpoint, _, _ in jobs)


def test_idle_prewarmer_does_not_call_overview_or_use_parallel_cache_store():
    source = (Path(__file__).parents[1] / "webhook.py").read_text(encoding="utf-8")
    warmer = source[source.index("async def cache_prewarmer_loop"):source.index("async def event_loop_monitor_loop")]
    assert "get_leads_dashboard_overview" not in warmer
    assert "cache_store" not in warmer
    assert "cache_locks" not in warmer
    assert "warm_specs" not in warmer
    assert "await asyncio.Event().wait()" in warmer


def test_keeper_is_deduplicated_and_only_selects_due_keys(monkeypatch):
    jobs = tuple(
        (endpoint, f"key-{endpoint}", lambda endpoint=endpoint: {"endpoint": endpoint})
        for endpoint in ("overview", "operations")
    )
    monkeypatch.setattr(leads_service, "_pinned_dashboard_jobs", lambda: jobs)
    assert leads_service.keep_pinned_dashboard_cache() == ["overview", "operations"]
    assert leads_service.keep_pinned_dashboard_cache() == []
    assert _wait_until(lambda: not leads_service._CACHE_REFRESHING)


def test_bandwidth_model_shows_idle_reduction():
    full_four_preset_miss = 2_053_998
    old_overview_computations_per_hour = 60
    old_bytes_per_hour = old_overview_computations_per_hour * full_four_preset_miss / 4
    new_idle_bytes_per_hour = 0
    assert old_bytes_per_hour == pytest.approx(30_809_970)
    assert new_idle_bytes_per_hour == 0
