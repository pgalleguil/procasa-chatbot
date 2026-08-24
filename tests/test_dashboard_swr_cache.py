import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from pymongo.errors import NetworkTimeout

from analytics import leads_service


@pytest.fixture(autouse=True)
def clear_swr_state():
    leads_service.L1_CACHE.clear()
    with leads_service._CACHE_REFRESH_LOCK:
        leads_service._CACHE_REFRESHING.clear()
    yield
    leads_service.L1_CACHE.clear()
    with leads_service._CACHE_REFRESH_LOCK:
        leads_service._CACHE_REFRESHING.clear()


def _age_cache(key, seconds):
    timestamp, payload = leads_service.L1_CACHE[key]
    leads_service.L1_CACHE[key] = (timestamp - seconds, payload)


def test_properties_soft_expired_returns_stale_and_schedules_one_refresh(monkeypatch):
    calls = []
    release = threading.Event()
    payload = {"meta": {}, "inventory": {"active": 4}}

    def load(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return payload
        release.wait(2)
        return {"meta": {}, "inventory": {"active": 5}}

    monkeypatch.setattr(leads_service, "query_demand_capture_dashboard", load)
    assert leads_service.get_properties_inventory_dashboard()["inventory"]["active"] == 4
    key = next(iter(leads_service.L1_CACHE))
    _age_cache(key, leads_service.CACHE_TTL + 1)

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: leads_service.get_properties_inventory_dashboard(), range(5)))
    assert all(result["inventory"]["active"] == 4 for result in results)
    assert all(result["meta"]["data_status"] == "stale" for result in results)
    # One initial load plus one single-flight refresh.
    assert len(calls) == 2
    release.set()


def test_hard_expired_without_usable_payload_preserves_error(monkeypatch):
    monkeypatch.setattr(
        leads_service,
        "query_demand_capture_dashboard",
        lambda *args, **kwargs: {"meta": {}, "inventory": {"active": 1}},
    )
    leads_service.get_properties_inventory_dashboard()
    key = next(iter(leads_service.L1_CACHE))
    _age_cache(key, leads_service.CACHE_HARD_TTL + 1)
    monkeypatch.setattr(
        leads_service,
        "query_demand_capture_dashboard",
        lambda *args, **kwargs: (_ for _ in ()).throw(NetworkTimeout("timeout")),
    )
    with pytest.raises(leads_service.InventoryTemporarilyUnavailable):
        leads_service.get_properties_inventory_dashboard()


def test_failed_background_refresh_keeps_stale_entry(monkeypatch):
    payload = {"meta": {}, "inventory": {"active": 2}}
    monkeypatch.setattr(leads_service, "query_demand_capture_dashboard", lambda *a, **k: payload)
    leads_service.get_properties_inventory_dashboard()
    key = next(iter(leads_service.L1_CACHE))
    original = leads_service.L1_CACHE[key]
    _age_cache(key, leads_service.CACHE_TTL + 1)
    monkeypatch.setattr(
        leads_service,
        "query_demand_capture_dashboard",
        lambda *a, **k: (_ for _ in ()).throw(NetworkTimeout("refresh failed")),
    )
    result = leads_service.get_properties_inventory_dashboard()
    assert result["inventory"]["active"] == 2
    time.sleep(0.05)
    assert leads_service.L1_CACHE[key][1] == original[1]


@pytest.mark.parametrize(
    "function_name,kwargs,payload",
    [
        ("get_leads_dashboard_overview", {"period_preset": "30d", "compare": "auto"}, {"demand": {"total": 3}}),
        ("get_leads_operational_dashboard", {"period_preset": "30d", "compare": "auto"}, {"period": {"assigned": 3}}),
    ],
)
def test_overview_and_operations_soft_expiry_is_single_flight(monkeypatch, function_name, kwargs, payload):
    calls = []
    release = threading.Event()
    private_name = "_compute_leads_dashboard_overview" if function_name == "get_leads_dashboard_overview" else "_compute_leads_operational_dashboard"

    def compute(*args, **inner_kwargs):
        calls.append(1)
        if len(calls) > 1:
            release.wait(2)
        return payload

    monkeypatch.setattr(leads_service, private_name, compute)
    public = getattr(leads_service, function_name)
    assert public(**kwargs) == payload
    key = next(iter(leads_service.L1_CACHE))
    _age_cache(key, leads_service.CACHE_TTL + 1)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: public(**kwargs), range(5)))
    assert results == [payload] * 5
    assert len(calls) == 2
    release.set()


def test_cache_ttl_contract():
    assert leads_service.CACHE_TTL == 120
    assert leads_service.CACHE_HARD_TTL == 600
    assert leads_service.PINNED_REFRESH_AGE == 240
    assert leads_service.PINNED_MAX_STALE == 1800


def test_prewarmer_contract_keeps_only_periodic_keeper_after_blocking_startup_warm():
    source = (Path(__file__).parents[1] / "webhook.py").read_text(encoding="utf-8")
    warmer = source[source.index("async def cache_prewarmer_loop"):source.index("async def event_loop_monitor_loop")]
    assert "warm_pinned_dashboard_cache" not in warmer
    assert "keep_pinned_dashboard_cache" in warmer
    assert "interval_seconds=60" in warmer
    assert "refresh_age_seconds=240" in warmer
    assert "await asyncio.sleep(30)" not in warmer
    assert '"today"' not in warmer
    assert '"week"' not in warmer
    assert '"month"' not in warmer


def test_pinned_jobs_are_the_same_three_30d_request_keys(monkeypatch):
    warm_ps, warm_pe = leads_service._dashboard_30d_period()
    jobs = leads_service._pinned_dashboard_jobs()
    assert [endpoint for endpoint, _, _ in jobs] == ["overview", "operations", "properties"]

    monkeypatch.setattr(leads_service, "_compute_leads_dashboard_overview", lambda **kwargs: {"endpoint": "overview"})
    monkeypatch.setattr(leads_service, "_compute_leads_operational_dashboard", lambda **kwargs: {"endpoint": "operations"})
    monkeypatch.setattr(leads_service, "query_demand_capture_dashboard", lambda *a, **k: {"endpoint": "properties"})
    leads_service.get_leads_dashboard_overview(period_start=warm_ps, period_end=warm_pe, compare="auto", period_preset="30d")
    leads_service.get_leads_operational_dashboard(period_start=warm_ps, period_end=warm_pe, compare="auto", period_preset="30d")
    leads_service.get_properties_inventory_dashboard(period_start=warm_ps, period_end=warm_pe, filters={})
    assert set(leads_service.L1_CACHE) == {key for _, key, _ in jobs}


@pytest.mark.parametrize(
    "public_name,compute_name,payload",
    [
        ("get_leads_dashboard_overview", "_compute_leads_dashboard_overview", {"meta": {}, "endpoint": "overview"}),
        ("get_leads_operational_dashboard", "_compute_leads_operational_dashboard", {"meta": {}, "endpoint": "operations"}),
    ],
)
def test_pinned_hard_expiry_serves_stale_and_schedules_refresh(monkeypatch, public_name, compute_name, payload):
    warm_ps, warm_pe = leads_service._dashboard_30d_period()
    calls = []
    monkeypatch.setattr(leads_service, compute_name, lambda **kwargs: calls.append(1) or payload)
    public = getattr(leads_service, public_name)
    kwargs = {"period_start": warm_ps, "period_end": warm_pe, "compare": "auto", "period_preset": "30d"}
    if public_name == "get_leads_dashboard_overview":
        public(**kwargs)
    else:
        public(**kwargs)
    key = next(iter(leads_service.L1_CACHE))
    _age_cache(key, leads_service.CACHE_HARD_TTL + 1)
    result = public(**kwargs)
    assert result["meta"]["data_status"] == "stale"
    assert result["meta"]["degraded"] is True
    assert result["meta"]["refresh"] == "scheduled"
    time.sleep(0.05)
    assert len(calls) == 2


def test_pinned_keeper_warms_in_declared_order(monkeypatch):
    order = []
    jobs = tuple((name, f"key-{name}", lambda name=name: order.append(name) or {"name": name}) for name in ("overview", "operations", "properties"))
    monkeypatch.setattr(leads_service, "_pinned_dashboard_jobs", lambda: jobs)
    assert leads_service.warm_pinned_dashboard_cache() == ["overview", "operations", "properties"]
    assert order == ["overview", "operations", "properties"]


def test_refresh_scheduler_rejects_duplicate_key():
    started = threading.Event()
    release = threading.Event()

    def loader():
        started.set()
        release.wait(2)
        return {"ok": True}

    key = "test-single-flight"
    assert leads_service._schedule_cache_refresh(key, loader) is True
    assert started.wait(1)
    assert leads_service._schedule_cache_refresh(key, loader) is False
    release.set()
