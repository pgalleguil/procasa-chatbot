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


def test_prewarmer_contract_is_single_initial_30d_pass():
    source = (Path(__file__).parents[1] / "webhook.py").read_text(encoding="utf-8")
    warmer = source[source.index("async def cache_prewarmer_loop"):source.index("async def event_loop_monitor_loop")]
    assert 'preset_range("30d"' in warmer
    assert 'period_preset="30d"' in warmer
    assert 'warm_specs' not in warmer
    assert '"today"' not in warmer
    assert '"week"' not in warmer
    assert '"month"' not in warmer


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
