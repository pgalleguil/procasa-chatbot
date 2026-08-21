import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor

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
        self.aggregate_pipelines = []
        self.aggregate_options = []
        self.find_batch_sizes = []

    def aggregate(self, pipeline, **kwargs):
        self.aggregate_pipelines.append(pipeline)
        self.aggregate_options.append(kwargs)
        return FakeCursor(self.leads)

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

        return FakeCursor([doc for doc in self.properties if matches(doc)], self.find_batch_sizes)


class FakeCursor:
    def __init__(self, documents, batch_sizes=None):
        self.documents = documents
        self.batch_sizes = batch_sizes

    def batch_size(self, value):
        if self.batch_sizes is not None:
            self.batch_sizes.append(value)
        return self

    def __iter__(self):
        return iter(self.documents)


class FakeDatabase:
    def __init__(self, leads_collection, properties_collection):
        self.collections = {"leads": leads_collection, "universo_cartera_prop360": properties_collection}

    def __getitem__(self, name):
        return self.collections[name]


@pytest.fixture(autouse=True)
def clear_inventory_cache():
    leads_service.L1_CACHE.clear()
    leads_queries._DEMAND_CAPTURE_BASE_CACHE = None
    yield
    leads_service.L1_CACHE.clear()
    leads_queries._DEMAND_CAPTURE_BASE_CACHE = None


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
    assert result["meta"]["degraded"] is False
    assert result["meta"]["refresh"] == "scheduled"
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

    assert codes == {"historic-sucre", "lead-inactive"}
    assert "active-network" not in codes
    assert property_collection.find_filters[0] != {}
    assert len(property_collection.find_filters[0]["$or"]) == 2
    assert property_collection.find_batch_sizes == [250]
    assert lead_collection.aggregate_options == [{}]
    assert lead_collection.aggregate_pipelines[0][2]["$project"] == {
        "created_at": 1,
        "prospecto.codigo": 1,
        "lifecycle.first_effective_contact_at": 1,
        "lifecycle.visit_scheduled_at": 1,
    }
    assert "find({})" not in inspect.getsource(leads_queries.query_demand_capture_dashboard)


def test_demand_capture_base_is_reused_across_periods(monkeypatch):
    lead_collection = FakeCollection(leads=[{"prospecto": {"codigo": "lead-code"}}])
    property_collection = FakeCollection(properties=[
        {"codigo": "lead-code", "disponible_prop360": False, "estado": {"oficina": "PROCASA SUCRE"}},
    ])
    db = FakeDatabase(lead_collection, property_collection)
    monkeypatch.setattr(leads_queries, "get_db", lambda: db)
    monkeypatch.setattr(leads_queries, "build_demand_capture_contract", lambda *args, **kwargs: {"ok": True})

    leads_queries.query_demand_capture_dashboard("2026-08-01", "2026-08-07")
    leads_queries.query_demand_capture_dashboard("2026-08-08", "2026-08-14")

    assert len(property_collection.find_filters) == 1
    assert property_collection.find_batch_sizes == [250]


def test_failed_demand_capture_base_is_not_cached(monkeypatch):
    lead_collection = FakeCollection(leads=[])
    property_collection = FakeCollection(timeout=True)
    db = FakeDatabase(lead_collection, property_collection)
    monkeypatch.setattr(leads_queries, "get_db", lambda: db)

    with pytest.raises(NetworkTimeout):
        leads_queries.query_demand_capture_dashboard()

    assert leads_queries._DEMAND_CAPTURE_BASE_CACHE is None


def test_demand_capture_single_flight_loads_base_once(monkeypatch):
    lead_collection = FakeCollection(leads=[{"prospecto": {"codigo": "lead-code"}}])
    property_collection = FakeCollection(properties=[
        {"codigo": "lead-code", "disponible_prop360": False, "estado": {"oficina": "PROCASA SUCRE"}},
    ])
    db = FakeDatabase(lead_collection, property_collection)
    monkeypatch.setattr(leads_queries, "get_db", lambda: db)
    monkeypatch.setattr(leads_queries, "build_demand_capture_contract", lambda *args, **kwargs: {"ok": True})

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: leads_queries.query_demand_capture_dashboard(), range(5)))

    assert len(results) == 5
    assert all(result == {"ok": True} for result in results)
    assert len(property_collection.find_filters) == 1


def test_simulation_uses_explicit_250_document_batches(monkeypatch):
    lead_collection = FakeCollection(leads=[{"prospecto": {"codigo": "lead-code"}}])
    property_collection = FakeCollection(
        properties=[
            {
                "codigo": "lead-code",
                "disponible_prop360": True,
                "estado": {"oficina": "PROCASA SUCRE"},
            }
        ]
    )
    monkeypatch.setattr(leads_queries, "get_db", lambda: FakeDatabase(lead_collection, property_collection))

    leads_queries.query_capture_simulation_dataset(period_end="2026-08-20")

    assert property_collection.find_batch_sizes == [250]
    assert lead_collection.aggregate_options == [{}]
    assert lead_collection.aggregate_pipelines[0][-1]["$project"] == {
        "created_at": 1,
        "prospecto.codigo": 1,
    }


def test_simulation_timeout_is_not_converted_to_empty_dataset(monkeypatch):
    property_collection = FakeCollection(timeout=True)
    db = FakeDatabase(FakeCollection(leads=[]), property_collection)
    monkeypatch.setattr(leads_queries, "get_db", lambda: db)

    with pytest.raises(NetworkTimeout):
        leads_queries.query_capture_simulation_dataset()


def test_demand_capture_records_stage_timings_without_normalizing_all_leads(monkeypatch):
    lead_collection = FakeCollection(leads=[{"prospecto": {"codigo": "lead-code"}}])
    property_collection = FakeCollection(properties=[])
    db = FakeDatabase(lead_collection, property_collection)
    monkeypatch.setattr(leads_queries, "get_db", lambda: db)
    monkeypatch.setattr(
        leads_queries,
        "build_demand_capture_contract",
        lambda *args, **kwargs: {"meta": {"data_status": "fresh"}},
    )
    timing = {}

    leads_queries.query_demand_capture_dashboard(timing=timing)

    assert lead_collection.aggregate_pipelines[0][1].get("$match")
    assert "$set" in lead_collection.aggregate_pipelines[0][0]
    for stage in ("leads_aggregate", "lead_codes", "property_find", "contract_build", "total"):
        assert timing[f"demand_capture.{stage}_ms"] >= 0


def test_demand_capture_timeout_records_property_find_stage(monkeypatch):
    lead_collection = FakeCollection(leads=[])
    property_collection = FakeCollection(timeout=True)
    db = FakeDatabase(lead_collection, property_collection)
    monkeypatch.setattr(leads_queries, "get_db", lambda: db)
    timing = {}

    with pytest.raises(NetworkTimeout):
        leads_queries.query_demand_capture_dashboard(timing=timing)

    assert timing["demand_capture.timeout_stage"] == "property_find"
    assert timing["demand_capture.property_find_ms"] >= 0
    assert timing["demand_capture.total_ms"] >= timing["demand_capture.property_find_ms"]


class _OpsCursor:
    def __iter__(self):
        return iter(())


class _OpsCollection:
    def __init__(self):
        self.aggregate_calls = []
        self.find_calls = []

    def aggregate(self, pipeline, **kwargs):
        self.aggregate_calls.append((pipeline, kwargs))
        return _OpsCursor()

    def find(self, query, projection=None):
        self.find_calls.append((query, projection))
        return _OpsCursor()


class _OpsDatabase:
    def __init__(self):
        self.collections = {"leads": _OpsCollection(), "crm_events": _OpsCollection()}

    def __getitem__(self, name):
        return self.collections[name]


def test_operations_period_projection_removes_only_prospect_name():
    db = _OpsDatabase()
    projection = dict(leads_queries._OPS_PROJECTED_LEAD_PROJECTION)
    projection.pop("prospecto.nombre", None)

    leads_queries._ops_projected_leads(db, {}, operation="aggregate_period_cohort", projection=projection)

    actual = db["leads"].aggregate_calls[0][0][-1]["$project"]
    assert "prospecto.nombre" not in actual
    assert actual["prospecto.codigo"] == 1
    assert actual["lifecycle.first_effective_contact_at"] == 1
    assert actual["stage_history"] == 1


def test_operations_current_stock_projection_preserves_contract():
    db = _OpsDatabase()

    leads_queries._ops_projected_leads(db, {}, operation="aggregate_current_stock")

    actual = db["leads"].aggregate_calls[0][0][-1]["$project"]
    assert actual == leads_queries._OPS_PROJECTED_LEAD_PROJECTION
    assert actual["prospecto.nombre"] == 1
    assert actual["phone"] == 1


def test_operations_activity_projection_has_exact_meta_subfields():
    db = _OpsDatabase()

    leads_queries._ops_collect_activity_signals(
        db,
        [{"_id": "current"}],
        [],
        leads_queries.datetime(2026, 8, 1, tzinfo=leads_queries.timezone.utc),
        leads_queries.datetime(2026, 8, 20, tzinfo=leads_queries.timezone.utc),
    )

    projection = db["crm_events"].find_calls[0][1]
    assert projection["meta.actor_type"] == 1
    assert projection["meta.result"] == 1
    assert projection["meta.contact_result"] == 1
    assert projection["meta.confirmed"] == 1
    assert "meta" not in projection


def test_operations_scheduled_events_can_be_supplied_without_mongo_read(monkeypatch):
    monkeypatch.setattr(leads_queries, "get_db", lambda: (_ for _ in ()).throw(AssertionError("unexpected Mongo read")))

    result = leads_queries._scheduled_visit_lead_ids(
        [{"_id": "lead", "created_at": leads_queries.datetime(2026, 8, 1, tzinfo=leads_queries.timezone.utc)}],
        {"lead"},
        leads_queries.datetime(2026, 8, 20, tzinfo=leads_queries.timezone.utc),
        signed_orders=[],
        scheduled_events=[],
    )

    assert result == set()


class _SynchronousExecutor:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, fn, *args, **kwargs):
        return _SynchronousFuture(fn(*args, **kwargs))


class _SynchronousFuture:
    def __init__(self, value):
        self.value = value

    def result(self):
        return self.value


def test_operations_parallel_reads_preserve_contract(monkeypatch):
    db = object()
    current_docs = [{"_id": "current"}]
    period_docs = [{"_id": "period", "lifecycle": {"assigned_at": "2026-08-05T12:00:00Z"}}]

    monkeypatch.setattr(leads_queries, "get_db", lambda: db)
    monkeypatch.setattr(
        leads_queries,
        "_ops_current_resource",
        lambda _db, _match, _profile=None: {
            "summary": {"active_total": 1, "active_assigned": 1, "unassigned": 0,
                        "by_executive": [{"executive": "Ejecutivo", "active": 1}]},
            "detail_docs": current_docs, "summary_rows": 1,
            "summary_bson_bytes": 0, "detail_bson_bytes": 0, "elapsed_ms": 0,
        },
    )
    monkeypatch.setattr(
        leads_queries,
        "_ops_historical_base",
        lambda _db, _filters, _projection, _profile=None: period_docs,
    )
    monkeypatch.setattr(leads_queries, "_ops_active_executive_names", lambda _db, _profile=None: {"Ejecutivo"})
    monkeypatch.setattr(leads_queries, "_ops_assignment_episode_map", lambda *args, **kwargs: {})
    monkeypatch.setattr(leads_queries, "_ops_fetch_signed_orders", lambda *args, **kwargs: [])
    monkeypatch.setattr(leads_queries, "_ops_fetch_scheduled_events", lambda *args, **kwargs: [])
    monkeypatch.setattr(leads_queries, "_ops_collect_activity_signals", lambda *args, **kwargs: {})
    monkeypatch.setattr(leads_queries, "_scheduled_visit_lead_ids", lambda *args, **kwargs: set())
    monkeypatch.setattr(
        leads_queries,
        "build_operational_contract",
        lambda current, period, *args, **kwargs: {"meta": {}, "current_ids": current, "period_ids": period},
    )

    parallel = leads_queries.query_leads_operational_dashboard(
        period_start="2026-08-01", period_end="2026-08-20", timing={}
    )
    monkeypatch.setattr(leads_queries, "ThreadPoolExecutor", _SynchronousExecutor)
    sequential = leads_queries.query_leads_operational_dashboard(
        period_start="2026-08-01", period_end="2026-08-20", timing={}
    )

    assert parallel == sequential


def test_operations_worker_exception_propagates(monkeypatch):
    monkeypatch.setattr(leads_queries, "get_db", lambda: object())

    def fail(*args, **kwargs):
        raise RuntimeError("mongo read failed")

    monkeypatch.setattr(leads_queries, "_ops_current_resource", fail)

    with pytest.raises(RuntimeError, match="mongo read failed"):
        leads_queries.query_leads_operational_dashboard(
            period_start="2026-08-01", period_end="2026-08-20", timing={}
        )
