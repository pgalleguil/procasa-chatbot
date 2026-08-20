import asyncio
import inspect

import pytest
from fastapi import HTTPException
from pymongo.errors import NetworkTimeout

from analytics import leads_queries
from analytics import leads_service


class FakeCollection:
    def __init__(self, *, leads=None, properties=None, timeout=False):
        self.leads = leads or []
        self.properties = properties or []
        self.timeout = timeout
        self.find_filters = []

    def aggregate(self, pipeline):
        return self.leads

    def find(self, query, projection=None):
        self.find_filters.append(query)
        if self.timeout:
            raise NetworkTimeout("simulated Mongo network timeout")
        assert query, "the inventory query must never be an open find({})"
        terms = query["$or"]

        def matches(doc):
            for term in terms:
                if term.get("disponible_prop360") is True and doc.get("disponible_prop360") is True:
                    return True
                if term.get("estado.oficina") == (doc.get("estado") or {}).get("oficina"):
                    return True
                if doc.get("codigo") in term.get("codigo", {}).get("$in", []):
                    return True
            return False

        return [doc for doc in self.properties if matches(doc)]


class FakeDatabase:
    def __init__(self, leads_collection, properties_collection):
        self.collections = {"leads": leads_collection, "universo_cartera_prop360": properties_collection}

    def __getitem__(self, name):
        return self.collections[name]


@pytest.fixture(autouse=True)
def clear_inventory_cache():
    leads_service.L1_CACHE.clear()
    yield
    leads_service.L1_CACHE.clear()


def test_normal_query_creates_valid_cache(monkeypatch):
    payload = {"meta": {"data_status": "fresh"}, "inventory": {"active": 3}}
    monkeypatch.setattr(leads_service, "query_demand_capture_dashboard", lambda *args, **kwargs: payload)

    result = leads_service.get_properties_inventory_dashboard()

    assert result["inventory"]["active"] == 3
    assert result["meta"]["data_status"] != "stale"
    assert leads_service.L1_CACHE


def test_timeout_returns_stale_payload_without_replacing_cache(monkeypatch):
    payload = {"meta": {"data_status": "fresh"}, "inventory": {"active": 3}}
    monkeypatch.setattr(leads_service, "query_demand_capture_dashboard", lambda *args, **kwargs: payload)
    leads_service.get_properties_inventory_dashboard()
    key = next(iter(leads_service.L1_CACHE))
    original_timestamp, original_payload = leads_service.L1_CACHE[key]
    leads_service.L1_CACHE[key] = (
        original_timestamp - leads_service.CACHE_TTL - 30,
        original_payload,
    )

    def timeout(*args, **kwargs):
        raise NetworkTimeout("simulated Mongo network timeout")

    monkeypatch.setattr(leads_service, "query_demand_capture_dashboard", timeout)
    result = leads_service.get_properties_inventory_dashboard()

    assert result["inventory"]["active"] == 3
    assert result["meta"]["data_status"] == "stale"
    assert result["meta"]["degraded"] is True
    assert result["meta"]["degraded_reason"] == "mongo_timeout"
    assert result["meta"]["stale_age_seconds"] > leads_service.CACHE_TTL
    assert leads_service.L1_CACHE[key][0] == original_timestamp - leads_service.CACHE_TTL - 30
    assert leads_service.L1_CACHE[key][1] == original_payload
    assert result is not original_payload


def test_timeout_cold_start_raises_controlled_inventory_error(monkeypatch):
    def timeout(*args, **kwargs):
        raise NetworkTimeout("simulated Mongo network timeout")

    monkeypatch.setattr(leads_service, "query_demand_capture_dashboard", timeout)

    with pytest.raises(leads_service.InventoryTemporarilyUnavailable):
        leads_service.get_properties_inventory_dashboard()


def test_inventory_endpoint_maps_cold_start_timeout_to_503(monkeypatch):
    import webhook

    async def admin_user(_request):
        return {"rol": "admin"}

    def timeout(**kwargs):
        raise leads_service.InventoryTemporarilyUnavailable

    monkeypatch.setattr(webhook, "get_current_user_doc", admin_user)
    monkeypatch.setattr(webhook, "get_properties_inventory_dashboard", timeout)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            webhook.api_leads_dashboard_properties_inventory(
                object(), period_start=None, period_end=None, operation=None,
                property_type=None, commune=None, responsible=None,
            )
        )

    assert error.value.status_code == 503
    assert error.value.detail == "Inventario temporalmente no disponible"


def test_inventory_filter_preserves_full_functional_universe(monkeypatch):
    properties = [
        {"codigo": "active-network", "disponible_prop360": True, "estado": {"oficina": "OTRA"}},
        {"codigo": "historic-sucre", "disponible_prop360": False, "estado": {"oficina": "PROCASA SUCRE"}},
        {"codigo": "lead-inactive", "disponible_prop360": False, "estado": {"oficina": "OTRA"}},
        {"codigo": "unreferenced-inactive", "disponible_prop360": False, "estado": {"oficina": "OTRA"}},
    ]
    leads = [{"prospecto": {"codigo": "lead-inactive"}}]
    lead_collection = FakeCollection(leads=leads)
    property_collection = FakeCollection(properties=properties)
    db = FakeDatabase(lead_collection, property_collection)
    monkeypatch.setattr(leads_queries, "get_db", lambda: db)
    monkeypatch.setattr(
        leads_queries,
        "build_demand_capture_contract",
        lambda property_docs, *args, **kwargs: {"property_docs": property_docs},
    )

    result = leads_queries.query_demand_capture_dashboard()
    codes = {doc["codigo"] for doc in result["property_docs"]}

    assert codes == {"active-network", "historic-sucre", "lead-inactive"}
    assert property_collection.find_filters[0] != {}
    assert len(property_collection.find_filters[0]["$or"]) == 3
    assert "find({})" not in inspect.getsource(leads_queries.query_demand_capture_dashboard)


def test_simulation_timeout_is_not_converted_to_empty_dataset(monkeypatch):
    property_collection = FakeCollection(timeout=True)
    db = FakeDatabase(FakeCollection(leads=[]), property_collection)
    monkeypatch.setattr(leads_queries, "get_db", lambda: db)

    with pytest.raises(NetworkTimeout):
        leads_queries.query_capture_simulation_dataset()
