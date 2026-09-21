from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

import mongomock
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from owner_portal import service
from owner_portal.router import router
from owner_portal.security import require_internal_preview
from owner_portal.schemas import (
    OWNER_PORTAL_EVENT_NAMES,
    OWNER_PORTAL_VIEW_ALLOWLIST,
    OwnerPortalAccessV1,
    OwnerPortalEventV1,
    PriceResponseSimulationV1,
    assert_owner_portal_payload_allowlisted,
)
from market_intelligence.models import MarketIntelligenceSnapshotV1
from market_intelligence.sources import OFFICIAL_SOURCES, run_dry_run


def master_doc(code: str, office: str = "PROCASA SUCRE", *, active: bool = True, price: bool = True, photo_code: str | None = None, **extra):
    doc = {
        "codigo": code,
        "estado": {"oficina": office, "disponible_prop360": active},
        "tipo_operacion": {
            "tipo": "Departamento",
            "venta": True,
            "precio_venta": {"precio_uf": 3200, "precio_clp": 120000000} if price else {},
        },
        "metadata": {"tipo_propiedad": "Departamento"},
        "ubicacion": {"region": "Metropolitana", "comuna": "Santiago", "sector": "Centro"},
        "caracteristicas": {"dormitorios": 2, "banos": 2, "superficie_construida": 65},
        "publicaciones": {"yapo": {"publicaciones": {"venta": {"code": photo_code or code}}}},
    }
    doc.update(extra)
    return doc


def make_db(*docs, captures=(), leads=(), market=True):
    client = mongomock.MongoClient()
    db = client["test"]
    db["universo_cartera_prop360"].insert_many(list(docs))
    if captures:
        db["propiedades_captacion"].insert_many(list(captures))
    if leads:
        db["leads"].insert_many(list(leads))
    if market:
        db["mercado_comunal"].insert_one({
            "comuna": "Santiago",
            "tipo_propiedad": "Departamento",
            "mercado_venta": {"uf_m2_publicacion_actual": 55.23},
            "rangos_precio_venta": {"min_uf": 1288, "max_uf": 3799},
            "indicadores_mercado": {"nivel_competencia": "alto"},
        })
    return db


def internal_client(monkeypatch, db):
    monkeypatch.setattr("owner_portal.router.get_db", lambda: db)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_internal_preview] = lambda: {"auth": "test"}
    return TestClient(app)


def test_sucre_identity_exact():
    assert service.is_procasa_sucre_property({"estado": {"oficina": "PROCASA SUCRE"}})


def test_other_office_is_excluded():
    assert not service.is_procasa_sucre_property({"estado": {"oficina": "PROCASA FRANCISCO VIAL"}})


def test_unknown_office_is_excluded_without_aliases():
    assert not service.is_procasa_sucre_property({"estado": {"oficina": "INMOBILIARIA SUCRE SPA"}})
    assert not service.is_procasa_sucre_property({"estado": {"oficina": "sucre"}})


def test_other_office_leads_are_not_sucre():
    db = make_db(
        master_doc("S1", photo_code="S1"),
        master_doc("O1", office="PROCASA VILLARRICA", photo_code="O1"),
        captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}],
        leads=[{"_id": "l1", "created_at": datetime.now(timezone.utc), "prospecto": {"codigo": "O1"}}],
    )
    view = service.get_owner_portal_property_view(db, "S1", datetime.now(timezone.utc) + timedelta(seconds=1))
    assert view is not None
    assert view.inquiries_previous_7d == 0


def test_owner_counts_only_exact_consultations_for_open_property():
    now = datetime.now(timezone.utc)
    db = make_db(
        master_doc("S1", photo_code="S1"),
        master_doc("S2", photo_code="S2"),
        captures=[
            {"listing_id": "S1", "image_urls": ["https://img/s1.jpg"]},
            {"listing_id": "S2", "image_urls": ["https://img/s2.jpg"]},
        ],
        leads=[
            {"_id": "l1", "created_at": now.isoformat(), "prospecto": {"codigo": "S1"}},
            {"_id": "l2", "created_at": now.isoformat(), "prospecto": {"codigo": "S2"}},
        ],
    )
    view = service.get_owner_portal_property_view(db, "S1", now + timedelta(seconds=1))
    assert view is not None
    assert view.inquiries_previous_7d == 1
    assert view.inquiries_previous_30d == 1


def test_property_view_uses_scoped_lead_lookup_without_global_identity_materialization(monkeypatch):
    db = make_db(
        master_doc("S1", photo_code="S1"),
        master_doc("S2", photo_code="S2"),
        leads=[
            {"_id": "l1", "created_at": datetime.now(timezone.utc).isoformat(), "prospecto": {"codigo": "S1"}},
            {"_id": "l2", "created_at": datetime.now(timezone.utc).isoformat(), "prospecto": {"codigo": "S2"}},
        ],
    )
    observed_lead_queries = []
    original_leads = db["leads"]

    class RecordingCollection:
        def find(self, query=None, *args, **kwargs):
            if query is None:
                query = {}
            observed_lead_queries.append(query)
            return original_leads.find(query, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(original_leads, name)

    class RecordingDb:
        def __getitem__(self, name):
            return RecordingCollection() if name == "leads" else db[name]

        def __getattr__(self, name):
            return getattr(db, name)

    monkeypatch.setattr(service, "_identity_properties", lambda _db: pytest.fail("global identity cache must not be used"))
    view = service.get_owner_portal_property_view(
        RecordingDb(),
        "S1",
        datetime.now(timezone.utc) + timedelta(seconds=1),
    )
    assert view is not None
    assert view.inquiries_previous_7d == 1
    assert observed_lead_queries
    assert all(query != {} for query in observed_lead_queries)
    assert "$or" in observed_lead_queries[0]


def test_preview_only_sucre(monkeypatch):
    db = make_db(master_doc("S1"), master_doc("O1", office="PROCASA VILLARRICA"), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    client = internal_client(monkeypatch, db)
    assert client.get("/owner-portal-preview/S1").status_code == 200
    assert client.get("/owner-portal-preview/O1").status_code == 404


def test_view_contains_no_pii(monkeypatch):
    db = make_db(master_doc("S1", photo_code="S1", owner_email="owner@example.com", owner_phone="+56911111111"), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    response = internal_client(monkeypatch, db).get("/owner-portal-preview/S1")
    assert response.status_code == 200
    assert "owner@example.com" not in response.text
    assert "+56911111111" not in response.text


def test_missing_property_is_404(monkeypatch):
    client = internal_client(monkeypatch, make_db(master_doc("S1")))
    assert client.get("/owner-portal-preview/NOPE").status_code == 404


def test_internal_access_is_required(monkeypatch):
    async def deny(_request):
        raise HTTPException(status_code=401, detail="CRM authentication required")

    monkeypatch.setattr("owner_portal.security._existing_crm_user", deny)
    scope = {"type": "http", "method": "GET", "path": "/owner-portal-preview/S1", "headers": [], "client": ("testserver", 80), "query_string": b"", "scheme": "http", "server": ("testserver", 80)}
    request = Request(scope)
    with pytest.raises(HTTPException) as exc:
        import asyncio
        asyncio.run(require_internal_preview(request))
    assert exc.value.status_code == 401


def test_production_host_never_uses_local_preview_bypass(monkeypatch):
    async def deny(_request):
        raise HTTPException(status_code=401, detail="CRM authentication required")

    monkeypatch.setenv("OWNER_PORTAL_PREVIEW_DEV_MODE", "true")
    monkeypatch.setattr("owner_portal.security._existing_crm_user", deny)
    scope = {"type": "http", "method": "GET", "path": "/owner-portal-preview/6786", "headers": [], "client": ("10.0.0.8", 80), "query_string": b"", "scheme": "https", "server": ("example.onrender.com", 443)}
    with pytest.raises(HTTPException) as exc:
        import asyncio
        asyncio.run(require_internal_preview(Request(scope)))
    assert exc.value.status_code == 401


def test_missing_crm_user_document_is_rejected(monkeypatch):
    async def missing(_request):
        return None

    monkeypatch.setattr("owner_portal.security._existing_crm_user", missing)
    scope = {"type": "http", "method": "GET", "path": "/owner-portal-preview/6786", "headers": [], "client": ("10.0.0.8", 80), "query_string": b"", "scheme": "https", "server": ("example.onrender.com", 443)}
    with pytest.raises(HTTPException) as exc:
        import asyncio
        asyncio.run(require_internal_preview(Request(scope)))
    assert exc.value.status_code == 401


def test_no_photo_fallback(monkeypatch):
    db = make_db(master_doc("S1"))
    response = internal_client(monkeypatch, db).get("/owner-portal-preview/S1")
    assert response.status_code == 200
    assert "hero-placeholder" in response.text


def test_missing_price_is_hidden_without_placeholder(monkeypatch):
    db = make_db(master_doc("S1", price=False), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    response = internal_client(monkeypatch, db).get("/owner-portal-preview/S1")
    assert response.status_code == 200
    assert "Precio publicado" not in response.text


def test_external_views_are_not_exposed(monkeypatch):
    db = make_db(master_doc("S1"), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    text = internal_client(monkeypatch, db).get("/owner-portal-preview/S1").text.casefold()
    assert "pageview" not in text
    assert "impression" not in text
    assert "ctr" not in text
    assert "visualizaciones del aviso" not in text
    assert "tiempo real" not in text
    assert "Leads" not in text


def test_visits_are_not_claimed_as_completed(monkeypatch):
    db = make_db(master_doc("S1"), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    text = internal_client(monkeypatch, db).get("/owner-portal-preview/S1").text.casefold()
    assert "visitas realizadas" not in text
    assert "no verifica asistencia" not in text


def test_responsive_render_contract(monkeypatch):
    db = make_db(master_doc("S1"), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    text = internal_client(monkeypatch, db).get("/owner-portal-preview/S1").text
    assert 'name="viewport"' in text
    assert "@media (max-width: 560px)" in text


def test_updated_date_uses_verified_master_field(monkeypatch):
    db = make_db(master_doc("S1", estado={"oficina": "PROCASA SUCRE", "disponible_prop360": True, "ultima_actualizacion": "2026-09-10T10:00:00-03:00"}), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    text = internal_client(monkeypatch, db).get("/owner-portal-preview/S1").text
    assert re.search(r"corte \d{2}/\d{2}/\d{4}", text)


def test_market_below_minimum_hides_representative_median_and_range():
    db = make_db(master_doc("S1"), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    prop = service._safe_property(db["universo_cartera_prop360"].find_one({"codigo": "S1"}), ["https://img/s.jpg"])
    market = service._market_context(db, prop, datetime.now(timezone.utc))
    assert market["minimum_comparables"] == 5
    assert market["comparables_count"] == 0
    assert market["median_price_uf"] is None
    assert market["range_price_uf"] == [None, None]


def test_market_at_minimum_exposes_descriptive_median():
    db = make_db(master_doc("S1"), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    for index in range(5):
        db["propiedades_captacion"].insert_one({"listing_id": f"cmp-{index}", "comuna": "Santiago", "tipo_propiedad": "Departamento", "operacion": "venta", "precio_uf": 3000 + index * 100, "superficie": 60 + index})
    prop = service._safe_property(db["universo_cartera_prop360"].find_one({"codigo": "S1"}), ["https://img/s.jpg"])
    market = service._market_context(db, prop, datetime.now(timezone.utc))
    assert market["comparables_count"] == 5
    assert market["median_price_uf"] == 3200
    assert market["range_price_uf"] == [3000, 3400]


def test_no_ml_execution():
    db = make_db(master_doc("S1"))
    view = service.get_owner_portal_property_view(db, "S1", datetime.now(timezone.utc))
    assert view is not None
    assert view.recommendation is None


def test_auto_selection_is_dynamic_and_requires_photo(monkeypatch):
    db = make_db(
        master_doc("NO_PHOTO"),
        master_doc("ELIGIBLE"),
        captures=[{"listing_id": "ELIGIBLE", "image_urls": ["https://img/e.jpg"]}],
    )
    assert service.select_preview_property_code(db) == "ELIGIBLE"
    client = internal_client(monkeypatch, db)
    assert client.get("/owner-portal-preview").status_code == 200


def test_future_event_contract_is_not_persisted():
    from owner_portal.analytics import portal_event_contract

    event = portal_event_contract(
        "portal_opened",
        property_code="S1",
        event_at_utc="2026-09-10T15:00:00+00:00",
        event_at_local="2026-09-10T12:00:00-03:00",
        metadata={"surface": "mobile"},
    )
    assert event.event_name == "portal_opened"
    assert "email" not in event.metadata


def test_owner_portal_dto_is_recursively_allowlisted():
    db = make_db(master_doc("S1"), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    view = service.get_owner_portal_property_view(db, "S1", datetime.now(timezone.utc))
    assert view is not None
    payload = view.to_dict()
    assert set(payload) == OWNER_PORTAL_VIEW_ALLOWLIST
    assert_owner_portal_payload_allowlisted(payload)
    assert set(payload["current_price"]) == {"uf", "clp"}
    assert set(payload["data_quality"]) <= {
        "photo_available", "price_available", "surface_available", "bedrooms_available",
        "bathrooms_available", "parking_available", "market_data_available",
        "price_history_available", "lead_linkage_available",
    }


def test_pii_cannot_enter_owner_portal_dto():
    db = make_db(
        master_doc(
            "S1",
            owner_name="Ana Example",
            owner_email="owner@example.com",
            owner_phone="+56911111111",
            owner_rut="12.345.678-9",
            direccion_exacta="Calle privada 123",
            notas_crm="nota interna",
        )
    )
    view = service.get_owner_portal_property_view(db, "S1", datetime.now(timezone.utc))
    assert view is not None
    payload_text = repr(view.to_dict())
    for value in ("Ana Example", "owner@example.com", "+56911111111", "12.345.678-9", "Calle privada 123", "nota interna"):
        assert value not in payload_text


def test_conflict_is_excluded_from_property_consultations():
    db = make_db(
        master_doc("S1", photo_code="YA1"),
        master_doc("S2", photo_code="YA2"),
        leads=[
            {
                "_id": "conflict",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "prospecto": {"codigo": "S1", "codigo_yapo": "YA2"},
            }
        ],
    )
    view = service.get_owner_portal_property_view(db, "S1", datetime.now(timezone.utc) + timedelta(seconds=1))
    assert view is not None
    assert view.inquiries_previous_7d == 0
    assert view.inquiries_previous_30d == 0


def test_market_requires_same_commune_type_and_operation():
    db = make_db(master_doc("S1"), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    db["propiedades_captacion"].insert_many(
        [
            {"comuna": "Santiago", "tipo_propiedad": "Departamento", "operacion": "venta", "precio_uf": 3000, "superficie": 60},
            {"comuna": "Las Condes", "tipo_propiedad": "Departamento", "operacion": "venta", "precio_uf": 3100, "superficie": 60},
            {"comuna": "Santiago", "tipo_propiedad": "Casa", "operacion": "venta", "precio_uf": 3200, "superficie": 60},
            {"comuna": "Santiago", "tipo_propiedad": "Departamento", "operacion": "arriendo", "precio_uf": 3300, "superficie": 60},
        ]
    )
    prop = service._safe_property(db["universo_cartera_prop360"].find_one({"codigo": "S1"}), [])
    market = service._market_context(db, prop, datetime.now(timezone.utc))
    assert market["comparables_count"] == 1


def test_market_normalizes_labels_and_uses_verified_surface_fallbacks():
    doc = master_doc("V1", **{
        "ubicacion": {"region": "Valparaiso", "comuna": "Viña del Mar", "sector": "Centro"},
    })
    db = make_db(doc, market=False)
    db["mercado_comunal"].insert_one({
        "match_key": "vina del mar|departamento",
        "mercado_venta": {"uf_m2_publicacion_actual": 68.23},
        "rangos_precio_venta": {"min_uf": 1288, "max_uf": 3799},
    })
    for index in range(5):
        db["propiedades_captacion"].insert_one({
            "comuna": "Vina Del Mar",
            "tipo_propiedad": "departamento",
            "operacion": "Venta",
            "precio_uf": 3000 + index * 100,
            "m2_construidos": 60 + index,
        })
    prop = service._safe_property(db["universo_cartera_prop360"].find_one({"codigo": "V1"}), [])
    market = service._market_context(db, prop, datetime.now(timezone.utc))
    assert market["comparables_count"] == 5
    assert market["median_price_uf"] == 3200
    assert market["median_uf_m2"] == 51.61


def test_market_indicator_contract_is_dated_and_source_backed():
    db = make_db(master_doc("UF1"))
    db["uf_cache"].insert_one({
        "valor": 40846.11,
        "fecha": "2026-08-10",
        "fuente": "mindicador.cl",
        "actualizado_at": "2026-08-10T16:11:16+00:00",
    })
    view = service.get_owner_portal_property_view(db, "UF1", datetime(2026, 9, 10, tzinfo=timezone.utc))
    assert view is not None
    indicator = view.to_dict()["national_indicators"][0]
    assert set(indicator) == {
        "indicator_id", "scope", "geography", "value", "unit", "period",
        "source_name", "source_url", "retrieved_at", "valid_until",
        "source_as_of", "source_age_days", "is_stale",
    }
    assert indicator["period"] == "10/08/2026"
    assert indicator["source_url"] == "https://mindicador.cl/"


def test_editorial_template_omits_empty_operational_placeholders(monkeypatch):
    db = make_db(master_doc("ED1"), captures=[{"listing_id": "ED1", "image_urls": ["https://img/e.jpg"]}])
    text = internal_client(monkeypatch, db).get("/owner-portal-preview/ED1").text.casefold()
    assert "próximamente" not in text
    assert "no verifica asistencia" not in text
    assert "recomendación de precio" not in text
    assert "autorización" not in text


def test_invalid_prices_and_surfaces_are_excluded_from_market():
    db = make_db(master_doc("S1"))
    db["propiedades_captacion"].insert_many(
        [
            {"comuna": "Santiago", "tipo_propiedad": "Departamento", "operacion": "venta", "precio_uf": 0, "superficie": 60},
            {"comuna": "Santiago", "tipo_propiedad": "Departamento", "operacion": "venta", "precio_uf": -1, "superficie": 60},
            {"comuna": "Santiago", "tipo_propiedad": "Departamento", "operacion": "venta", "precio_uf": 3000, "superficie": 0},
            {"comuna": "Santiago", "tipo_propiedad": "Departamento", "operacion": "venta", "precio_uf": 3100, "superficie": -4},
            {"comuna": "Santiago", "tipo_propiedad": "Departamento", "operacion": "venta", "precio_uf": "Infinity", "superficie": 60},
            {"comuna": "Santiago", "tipo_propiedad": "Departamento", "operacion": "venta", "precio_uf": 3200, "superficie": 60},
        ]
    )
    prop = service._safe_property(db["universo_cartera_prop360"].find_one({"codigo": "S1"}), [])
    market = service._market_context(db, prop, datetime.now(timezone.utc))
    assert market["comparables_count"] == 1
    assert market["median_price_uf"] is None


def test_single_as_of_is_reused_by_leads_and_market(monkeypatch):
    db = make_db(master_doc("S1"))
    as_of = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
    observed: list[datetime] = []
    monkeypatch.setattr(
        service,
        "_lead_metrics_for_property",
        lambda _db, _property_doc, cutoff: (observed.append(cutoff) or {"property_previous_7d": 0, "property_previous_30d": 0}),
    )
    monkeypatch.setattr(
        service,
        "_market_context",
        lambda _db, _prop, cutoff: (observed.append(cutoff) or {"comparables_count": 0, "median_price_uf": None, "range_price_uf": [None, None], "median_uf_m2": None, "market_as_of": None}),
    )
    view = service.get_owner_portal_property_view(db, "S1", as_of)
    assert view is not None
    assert observed == [as_of, as_of]
    assert view.as_of == as_of.isoformat()
    assert view.data_updated_at == "10/09/2026"


def test_event_name_outside_allowlist_is_rejected():
    with pytest.raises(ValueError):
        OwnerPortalEventV1(
            event_name="not_allowed",
            property_code="S1",
            event_at_utc="2026-09-10T15:00:00+00:00",
            event_at_local="2026-09-10T12:00:00-03:00",
            session_id=None,
            portal_token_id=None,
            metadata={},
        )
    assert "portal_opened" in OWNER_PORTAL_EVENT_NAMES


def test_event_metadata_is_allowlisted_and_scalar():
    from owner_portal.analytics import portal_event_contract

    with pytest.raises(ValueError):
        portal_event_contract(
            "portal_opened",
            event_at_utc="2026-09-10T15:00:00+00:00",
            event_at_local="2026-09-10T12:00:00-03:00",
            metadata={"email": "owner@example.com"},
        )
    with pytest.raises(ValueError):
        portal_event_contract(
            "portal_opened",
            event_at_utc="2026-09-10T15:00:00+00:00",
            event_at_local="2026-09-10T12:00:00-03:00",
            metadata={"surface": {"arbitrary": "payload"}},
        )


def test_access_schema_has_hash_only_and_no_raw_token():
    access = OwnerPortalAccessV1(
        token_id="tok-1",
        property_code="S1",
        issued_at="2026-09-10T15:00:00+00:00",
        expires_at="2026-09-17T15:00:00+00:00",
        revoked_at=None,
        status="ACTIVE",
        purpose="owner_portal_view",
        created_by="system",
        token_hash="sha256:example",
    )
    payload = access.to_dict()
    assert "raw_token" not in payload
    assert "token" not in payload
    assert payload["token_hash"] == "sha256:example"


def test_percentiles_and_cohort_hierarchy_use_robust_statistics():
    assert service._percentile([10, 20, 30, 40, 50], 0.10) == 14.0
    assert service._percentile([10, 20, 30, 40, 50], 0.50) == 30.0
    prop = service._safe_property(master_doc("COHORT"), [])
    rows = [
        {"price_uf": 2000 + index * 100, "surface_m2": 65 + index % 3, "bedrooms": 2, "bathrooms": 2, "when": datetime.now(timezone.utc)}
        for index in range(10)
    ]
    cohort = service._select_comparable_cohort(prop, {"valid_rows": rows})
    assert cohort is not None
    assert cohort.level == "high_similarity"
    assert cohort.count == 10
    assert cohort.p10_uf <= cohort.p25_uf <= cohort.median_uf <= cohort.p75_uf <= cohort.p90_uf


def test_activity_series_contains_only_real_weekly_dates():
    as_of = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    dates = (as_of - timedelta(days=2), as_of - timedelta(days=40))
    points = service._activity_series(dates, as_of)
    assert len(points) == 12
    assert all(point.period and len(point.period) == 10 for point in points)
    assert sum(point.count for point in points) == 2
    assert service._activity_series((as_of - timedelta(days=200),), as_of) == ()


def test_verified_portals_are_the_only_presence_records(monkeypatch):
    doc = master_doc("PORTALS")
    doc["publicaciones"] = {
        "yapo": {"publicaciones": {"venta": {"code": "YA-1", "estado": "active", "url": "https://yapo.example/YA-1"}}},
        "toctoc": {"publicaciones": {"venta": {"code": "TO-1", "estado": "draft", "url": "https://toctoc.example/TO-1"}}},
    }
    db = make_db(doc, captures=[{"listing_id": "YA-1", "image_urls": ["https://img/ya.jpg"], "fecha_publicacion": "2026-09-01"}])
    view = service.get_owner_portal_property_view(db, "PORTALS", datetime.now(timezone.utc))
    assert view is not None
    assert [item.portal_name for item in view.publications] == ["Yapo"]
    assert view.publications[0].published_at == "01/09/2026"
    text = internal_client(monkeypatch, db).get("/owner-portal-preview/PORTALS").text
    assert "Yapo" in text
    assert "TOCTOC" not in text


def test_premium_template_contract_hides_future_and_extreme_range_language(monkeypatch):
    db = make_db(master_doc("PREMIUM"), captures=[{"listing_id": "PREMIUM", "image_urls": ["https://img/p.jpg"]}])
    text = internal_client(monkeypatch, db).get("/owner-portal-preview/PREMIUM").text
    assert "/static/logo.png" in text
    assert "Tu propiedad hoy" in text
    assert "El mercado inmobiliario chileno hoy" not in text
    assert "min_uf" not in text
    assert "max_uf" not in text
    assert "DemandForecastViewV1" not in text
    assert "forecast" not in text.casefold()
    assert "@media (prefers-reduced-motion: reduce)" in text


def test_structural_redesign_has_navigation_and_client_side_scenario(monkeypatch):
    db = make_db(master_doc("REDESIGN"), captures=[{"listing_id": "REDESIGN", "image_urls": ["https://img/r.jpg"]}])
    for index in range(10):
        db["propiedades_captacion"].insert_one({
            "listing_id": f"cmp-{index}", "comuna": "Santiago", "tipo_propiedad": "Departamento",
            "operacion": "venta", "precio_uf": 3000 + index * 100, "superficie": 65 + index % 3,
            "dormitorios": 2, "banos": 2,
        })
    text = internal_client(monkeypatch, db).get("/owner-portal-preview/REDESIGN").text
    for anchor in ("#resumen", "#actividad", "#mercado", "#comparables", "#escenarios"):
        assert anchor in text
    assert 'class="kpi-strip' in text
    assert 'type="range"' in text
    assert "data-scenario" in text
    assert "Esto no es una predicción de demanda." in text
    assert "DemandForecastViewV1" not in text
    assert "forecast" not in text.casefold()


def test_deterministic_observation_is_rendered_only_from_observed_data(monkeypatch):
    db = make_db(master_doc("OBSERVED"))
    text = internal_client(monkeypatch, db).get("/owner-portal-preview/OBSERVED").text
    assert "Las señales de tu propiedad" in text
    assert "Durante este período no se registraron consultas vinculadas." in text
    assert "debería bajar" not in text.casefold()
    assert "sobrevalorada" not in text.casefold()


def _scenario_db(code="SCENARIO"):
    db = make_db(master_doc(code), market=False)
    db["mercado_comunal"].insert_one({
        "comuna": "Santiago",
        "tipo_propiedad": "Departamento",
        "source": {"fecha_reporte": "28/04/2026", "filename": "santiago_departamento.pdf"},
        "mercado_venta": {
            "uf_m2_publicacion_actual": 55.23,
            "uf_m2_venta_efectiva_actual": 56.07,
            "variacion_uf_m2_12m": -9.98,
            "publicaciones_activas": 9215,
            "publicaciones_totales": 68325,
        },
    })
    for index in range(12):
        db["propiedades_captacion"].insert_one({
            "listing_id": f"safe-cmp-{index}",
            "comuna": "Santiago",
            "tipo_propiedad": "Departamento",
            "operacion": "venta",
            "precio_uf": 2700 + index * 100,
            "superficie": 60 + index % 4,
            "dormitorios": 2,
            "banos": 2,
            "source_portal": "Yapo",
        })
    return db


def test_scenario_counts_use_owner_facing_labels_and_no_approximate_symbol(monkeypatch):
    text = internal_client(monkeypatch, _scenario_db()).get("/owner-portal-preview/SCENARIO").text
    assert "Propiedades con menor precio" in text
    assert "Propiedades con mayor precio" in text
    assert "data-scenario-below" in text
    assert "data-scenario-above" in text
    assert "≈" not in text


def test_scenario_presets_use_real_current_p75_and_median_values(monkeypatch):
    text = internal_client(monkeypatch, _scenario_db()).get("/owner-portal-preview/SCENARIO").text
    assert 'data-scenario-preset="current" data-value="3200.0"' in text
    assert 'data-scenario-preset="p75"' in text
    assert 'data-scenario-preset="median"' in text
    assert "Cercano al P75" in text
    assert "Cercano a la mediana" in text
    assert "data-scenario-narrative" in text
    assert "no es una predicción de demanda" in text.casefold()
    assert "expected_inquiries" not in text


def test_local_market_semantics_and_cutoff_are_explicit(monkeypatch):
    db = _scenario_db()
    text = internal_client(monkeypatch, db).get("/owner-portal-preview/SCENARIO").text
    assert "Datos de mercado · corte 28/04/2026" in text
    assert "Publicaciones activas observadas" in text
    assert "UF/m² publicado" in text
    assert "UF/m² efectivo" in text
    assert "uf_m2_venta_efectiva_actual" in text
    assert "Actualizado al " in text
    assert "Datos de mercado · corte 28/04/2026" in text
    view = service.get_owner_portal_property_view(db, "SCENARIO", datetime(2026, 9, 10, 15, tzinfo=timezone.utc))
    assert view is not None
    assert view.data_updated_at == "10/09/2026"
    assert view.market_as_of == "28/04/2026"
    assert view.local_context is not None
    assert view.local_context.active_listings_semantics == (
        "Publicaciones activas observadas en el corte; no equivale a propiedades únicas."
    )
    assert view.local_context.effective_uf_m2_semantics is not None


def test_phase_2d5_6464_current_and_historical_windows_are_reproducible():
    current_as_of = datetime(2026, 9, 20, 23, 0, tzinfo=timezone.utc)
    historical_as_of = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    event_times = [
        datetime(2026, 6, 20, 2, 42, 47, tzinfo=timezone.utc),
        datetime(2026, 6, 25, 14, 24, 7, tzinfo=timezone.utc),
        datetime(2026, 6, 25, 14, 46, 32, tzinfo=timezone.utc),
        datetime(2026, 7, 3, 13, 21, 42, tzinfo=timezone.utc),
        datetime(2026, 7, 7, 18, 34, 11, tzinfo=timezone.utc),
        datetime(2026, 8, 5, 14, 38, 37, tzinfo=timezone.utc),
        datetime(2026, 8, 10, 13, 0, 25, tzinfo=timezone.utc),
        datetime(2026, 8, 10, 15, 0, 2, tzinfo=timezone.utc),
        datetime(2026, 8, 17, 13, 0, 22, tzinfo=timezone.utc),
        datetime(2026, 8, 17, 13, 0, 36, tzinfo=timezone.utc),
        datetime(2026, 8, 17, 13, 0, 43, tzinfo=timezone.utc),
    ]
    db = make_db(
        master_doc("6464", photo_code="6464"),
        leads=[
            {"_id": f"phase2d5-{index}", "created_at": value.isoformat(), "prospecto": {"codigo": "6464"}}
            for index, value in enumerate(event_times)
        ],
    )
    before_counts = {name: db[name].count_documents({}) for name in ("universo_cartera_prop360", "leads")}

    current = service.get_owner_portal_property_view(db, "6464", current_as_of)
    historical = service.get_owner_portal_property_view(db, "6464", historical_as_of)

    assert current is not None and historical is not None
    assert (current.inquiries_previous_30d, current.inquiries_previous_90d) == (0, 10)
    assert (historical.inquiries_previous_30d, historical.inquiries_previous_90d) == (3, 11)
    assert current.page_as_of == current.as_of
    assert current.engine_v1.status == "NOT_IMPLEMENTED"
    assert current.engine_v1.recommendation is None
    assert current.engine_v1.gradual_price_uf is None
    assert current.engine_v1.competitive_reference_uf is None
    assert {name: db[name].count_documents({}) for name in before_counts} == before_counts


def test_phase_2d5_market_ecdf_is_dynamic_and_fingerprint_repeats():
    as_of = datetime(2026, 9, 20, 23, 0, tzinfo=timezone.utc)
    prop = {
        "price_uf": 7000,
        "operation": "venta",
        "built_area_m2": 65,
        "bedrooms": 2,
        "bathrooms": 2,
    }
    rows = [
        {
            "listing_id": f"cmp-{index}",
            "price_uf": 1000 + index * 1000,
            "surface_m2": 65,
            "bedrooms": 2,
            "bathrooms": 2,
            "portal": "yapo",
            "when": as_of - timedelta(days=index + 1),
            "date_basis": "fecha de publicación",
        }
        for index in range(8)
    ]
    market = {"valid_rows": rows}
    first = service._select_comparable_cohort(prop, market, as_of)
    second = service._select_comparable_cohort(prop, market, as_of)
    assert first is not None and second is not None
    assert first.cohort_fingerprint == second.cohort_fingerprint
    positioning = service._positioning(7000, first)
    assert positioning is not None
    assert positioning.market_ecdf == 87.5
    assert positioning.market_ecdf_equal_or_below == 7
    assert "7 de cada 10" not in positioning.market_position_owner_text
    assert positioning.market_position_owner_text == (
        "Aproximadamente 9 de cada 10 publicaciones comparables tienen un precio igual o inferior al publicado."
    )


def test_phase_2d5_snapshot_source_age_is_independent_from_page_as_of():
    page_as_of = datetime(2026, 9, 20, 23, 0, tzinfo=timezone.utc)
    assert service._source_temporal_metadata("28/04/2026", page_as_of) == ("28/04/2026", 145, True)
    assert service._source_temporal_metadata("10/08/2026", page_as_of) == ("10/08/2026", 41, True)


def test_phase_2d5_future_as_of_is_rejected():
    db = make_db(master_doc("FUTURE"))
    with pytest.raises(ValueError, match="future"):
        service.get_owner_portal_property_view(
            db,
            "FUTURE",
            datetime.now(timezone.utc) + timedelta(days=1),
        )


def test_comparable_examples_are_bounded_and_anonymous(monkeypatch):
    db = _scenario_db()
    view = service.get_owner_portal_property_view(db, "SCENARIO", datetime.now(timezone.utc))
    assert view is not None and view.comparable_cohort is not None
    assert len(view.comparable_cohort.examples) <= 3
    payload = view.to_dict()
    assert all("listing_id" not in example for example in payload["comparable_cohort"]["examples"])
    rendered = internal_client(monkeypatch, db).get("/owner-portal-preview/SCENARIO").text
    assert "safe-cmp-" not in rendered
    assert "Comparable A" in rendered
    assert "teléfono" in rendered
    assert "dirección" in rendered


def test_navigation_contract_handles_scrollspy_and_direct_hash():
    from pathlib import Path

    template = Path("templates/owner_portal_preview.html").read_text(encoding="utf-8")
    assert "IntersectionObserver" in template
    assert "scroll-margin-top:118px" in template
    assert "hashTarget.scrollIntoView" in template
    assert "history.pushState" in template
    assert "event.preventDefault()" in template


def test_future_scenario_events_are_contract_only():
    from owner_portal.analytics import portal_event_contract

    changed = portal_event_contract(
        "price_scenario_changed",
        property_code="SCENARIO",
        metadata={"surface": "simulator"},
        event_at="2026-09-10T15:00:00+00:00",
    )
    preset = portal_event_contract(
        "price_scenario_preset_selected",
        property_code="SCENARIO",
        metadata={"surface": "simulator", "preset": "median"},
        event_at="2026-09-10T15:00:00+00:00",
    )
    assert changed.event_name == "price_scenario_changed"
    assert preset.metadata == {"surface": "simulator", "preset": "median"}


def test_market_intelligence_snapshot_v1_has_exact_source_boundary():
    snapshot = MarketIntelligenceSnapshotV1(
        indicator_id="mortgage_rate_uf",
        scope="nacional",
        geography="Chile",
        value=4.04,
        unit="porcentaje anualizado",
        reference_period="2026-08",
        source_name="Banco Central de Chile",
        source_reference="https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx",
        retrieved_at_utc="2026-09-10T15:00:00+00:00",
        source_published_at=None,
        status="valid",
        provenance={"endpoint": "https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx"},
    )
    assert set(snapshot.to_dict()) == {
        "indicator_id", "scope", "geography", "value", "unit", "reference_period",
        "source_name", "source_reference", "retrieved_at_utc", "source_published_at",
        "status", "provenance",
    }


def test_market_intelligence_dry_run_does_not_write_or_use_token(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network must not be called without a token")

    monkeypatch.delenv("BCCH_BDE_API_TOKEN", raising=False)
    monkeypatch.setattr("market_intelligence.sources.urllib.request.urlopen", forbidden)
    result = run_dry_run(token=None)
    assert result["mode"] == "dry-run"
    assert result["writes"] == 0
    assert result["mongo_writes"] == 0
    assert result["pageview_fetches"] == 0
    assert len(result["records"]) == len(OFFICIAL_SOURCES) == 3
    assert all(record["status"] == "not_configured" for record in result["records"])
    assert all(record["provenance"]["parsing_status"] == "blocked_missing_api_token" for record in result["records"])
    assert all("BCCH_BDE_API_TOKEN" not in str(record) for record in result["records"])
    assert all(record["value"] is None for record in result["records"])


def test_pageview_does_not_fetch_official_sources(monkeypatch):
    db = _scenario_db()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("pageview attempted an external fetch")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    view = service.get_owner_portal_property_view(db, "SCENARIO", datetime.now(timezone.utc))
    assert view is not None


def test_owner_portal_renders_at_most_three_primary_signals_and_ml_is_hidden(monkeypatch):
    text = internal_client(monkeypatch, _scenario_db()).get("/owner-portal-preview/SCENARIO").text
    assert text.count("<li><strong>") <= 3
    assert "DemandForecastViewV1" not in text
    assert "forecast" not in text.casefold()
    assert "PriceResponseSimulationV1" not in text


def test_price_response_simulation_future_contract_is_defined_without_calculation():
    from dataclasses import fields

    assert {field.name for field in fields(PriceResponseSimulationV1)} == {
        "scenario_price", "expected_inquiries_30d", "baseline_expected_inquiries_30d",
        "delta_expected", "lower_bound", "upper_bound", "model_version",
        "training_cutoff", "confidence_status",
    }


def test_concept_routes_redirect_to_one_executive_landing(monkeypatch):
    db = _scenario_db()
    client = internal_client(monkeypatch, db)
    for concept in ("a", "b", "c"):
        response = client.get(f"/owner-portal-concepts/{concept}/SCENARIO", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/owner-portal-convergent/SCENARIO"
        landing = client.get(f"/owner-portal-concepts/{concept}/SCENARIO")
        assert landing.status_code == 200
        assert "Qué está pasando con su propiedad" in landing.text
        assert "su propiedad" in landing.text
        assert 'data-report="owner-performance"' in landing.text


def test_concept_routes_keep_internal_protection(monkeypatch):
    monkeypatch.delenv("OWNER_PORTAL_PREVIEW_DEV_MODE", raising=False)

    async def deny(_request):
        raise HTTPException(status_code=401, detail="CRM authentication required")

    monkeypatch.setattr("owner_portal.security._existing_crm_user", deny)
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/owner-portal-concepts/a/SCENARIO")
    assert response.status_code == 401


def test_concept_templates_are_removed_after_single_landing_convergence():
    from pathlib import Path

    for concept in ("a", "b", "c"):
        assert not Path(f"templates/owner_portal_concept_{concept}.html").exists()


def test_concept_pageviews_do_not_fetch_external_sources(monkeypatch):
    db = _scenario_db()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("concept pageview attempted an external fetch")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    client = internal_client(monkeypatch, db)
    for concept in ("a", "b", "c"):
        response = client.get(
            f"/owner-portal-concepts/{concept}/SCENARIO",
            follow_redirects=False,
        )
        assert response.status_code == 307


def test_concept_routes_do_not_expose_owner_pii_or_internal_ids(monkeypatch):
    db = make_db(
        master_doc(
            "PII-CONCEPT",
            owner_email="owner@example.com",
            owner_phone="+56911111111",
            direccion_exacta="Calle privada 123",
        ),
        market=False,
    )
    client = internal_client(monkeypatch, db)
    for concept in ("a", "b", "c"):
        text = client.get(f"/owner-portal-concepts/{concept}/PII-CONCEPT").text
        assert "owner@example.com" not in text
        assert "+56911111111" not in text
        assert "Calle privada 123" not in text


def _convergent_db(*, with_lead: bool = True):
    db = _scenario_db()
    db["propiedades_captacion"].insert_one({"listing_id": "SCENARIO", "image_urls": ["https://img/scenario.jpg"]})
    if with_lead:
        db["leads"].insert_one({
            "_id": "convergent-lead",
            "created_at": datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc).isoformat(),
            "prospecto": {"codigo": "SCENARIO"},
        })
    return db


def legacy_convergent_candidate_uses_verified_recent_activity_and_real_data(monkeypatch):
    db = _convergent_db()
    assert service.select_owner_intelligence_property_code(
        db, datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
    ) == "SCENARIO"
    response = internal_client(monkeypatch, db).get("/owner-portal-convergent")
    assert response.status_code == 200
    assert 'data-portal="owner-performance-report"' in response.text
    assert 'data-property-code="SCENARIO"' in response.text
    assert "3.200 UF" in response.text
    assert "9.215" in response.text


def test_convergent_route_keeps_internal_protection(monkeypatch):
    monkeypatch.delenv("OWNER_PORTAL_PREVIEW_DEV_MODE", raising=False)

    async def deny(_request):
        raise HTTPException(status_code=401, detail="CRM authentication required")

    monkeypatch.setattr("owner_portal.security._existing_crm_user", deny)
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/owner-portal-convergent/SCENARIO")
    assert response.status_code == 401


def legacy_convergent_report_is_continuous_and_renders_without_external_fetch(monkeypatch):
    db = _convergent_db()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("convergent page attempted an external fetch")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    text = internal_client(monkeypatch, db).get("/owner-portal-convergent/SCENARIO").text
    assert "Qué está pasando con su propiedad." in text
    assert "Resumen de desempeño" in text
    assert "Posición frente al mercado" in text
    assert "Gestión PROCASA" in text
    assert text.count('class="kpi"') == 4
    assert 'href="#diagnostico"' in text
    assert 'href="#mercado"' in text
    assert "type=\"range\"" not in text
    assert "simul" not in text.casefold()
    assert "forecast" not in text.casefold()
    assert "RECOMENDADO" not in text
    assert "Autorizar ajuste recomendado" not in text
    assert "fetch(" not in text
    assert "CRM" not in text


def legacy_convergent_bento_visual_contract_preserves_core_modules_and_marquee(monkeypatch):
    db = _convergent_db()
    db["universo_cartera_prop360"].update_one(
        {"codigo": "SCENARIO"},
        {"$set": {"publicaciones": {"yapo": {"publicaciones": {"venta": {
            "code": "SCENARIO",
            "estado": "active",
            "url": "https://example.test/scenario",
        }}}}}},
    )
    text = internal_client(monkeypatch, db).get("/owner-portal-convergent/SCENARIO").text
    assert 'class="report bento-report"' in text
    assert '<style id="bento-evolution">' in text
    assert 'id="diagnostico"' in text
    assert 'id="mercado"' in text
    assert 'id="gestion"' in text
    assert 'id="propuesta"' in text
    assert 'src="/static/logo.png" alt="Logo oficial PROCASA"' in text
    assert 'data-portal-marquee' in text
    assert 'class="portal-marquee-track"' in text
    assert 'type="range"' not in text


def legacy_convergent_executive_report_contract_uses_real_dates_portals_and_owner_language(monkeypatch):
    text = internal_client(monkeypatch, _convergent_db()).get("/owner-portal-convergent/SCENARIO").text
    assert "Actualización de la información" in text
    assert re.search(r"Actualización de la información</strong>\d{2}/\d{2}/\d{4}", text)
    assert "Corte de información de mercado" in text
    assert "28/04/2026" in text
    assert "Yapo" in text
    assert "Su precio publicado" in text
    assert "tu propiedad" not in text.casefold()
    assert "podría generar más consultas" not in text.casefold()
    assert "tiempo estimado de venta" not in text.casefold()
    assert "recomendación comercial" not in text.casefold()
    assert "simulador" not in text.casefold()
    assert "forecast" not in text.casefold()
    assert "fetch(" not in text


def legacy_convergent_activity_supports_zero_and_nonzero_states(monkeypatch):
    zero = internal_client(monkeypatch, _convergent_db(with_lead=False)).get("/owner-portal-convergent/SCENARIO").text
    nonzero = internal_client(monkeypatch, _convergent_db(with_lead=True)).get("/owner-portal-convergent/SCENARIO").text
    assert "Sin eventos recientes visibles" in zero
    assert "Consulta registrada" not in zero
    assert "Consulta registrada" in nonzero
    assert "Presencia comercial" in nonzero
    assert "Su propiedad, visible en múltiples canales" in nonzero
    assert "En los últimos 30 días se registró 1 consulta vinculada a su propiedad." in nonzero
    assert "Consultas vinculadas" in nonzero
    assert "7 días:" in nonzero and "30 días:" in nonzero and "90 días:" in nonzero


def legacy_convergent_report_omits_future_controls_and_keeps_market_dates(monkeypatch):
    db = _convergent_db()
    text = internal_client(monkeypatch, db).get("/owner-portal-convergent/SCENARIO").text
    assert 'id="tab-historial"' not in text
    assert "Desde tu última actualización" not in text
    assert "Corte de información de mercado" in text
    assert "Recomendación comercial" not in text
    assert "Tiempo estimado de venta" not in text

    db["pricing_intelligence_property_snapshots_v1"].insert_one({
        "property_code": "SCENARIO",
        "snapshot_date_local": "2026-09-09",
        "previous_price_uf": 3000,
        "previous_price_clp": 112000000,
        "last_price_change_at": "2026-08-20T15:00:00+00:00",
    })
    with_history = internal_client(monkeypatch, db).get("/owner-portal-convergent/SCENARIO").text
    assert 'id="tab-historial"' not in with_history
    assert "Recomendación comercial" not in with_history


def legacy_convergent_comparables_use_owner_facing_position_language_and_no_pii(monkeypatch):
    db = _convergent_db()
    db["universo_cartera_prop360"].update_one(
        {"codigo": "SCENARIO"},
        {"$set": {"owner_email": "owner@example.com", "owner_phone": "+56911111111", "direccion_exacta": "Calle privada 123"}},
    )
    text = internal_client(monkeypatch, db).get("/owner-portal-convergent/SCENARIO").text
    assert "Su precio publicado" in text
    assert "Publicaciones comparables observadas" in text
    assert "no son transacciones" in text
    assert "Precio actual" in text and "Mediana de propiedades similares" in text
    assert "owner@example.com" not in text
    assert "+56911111111" not in text
    assert "Calle privada 123" not in text


def legacy_convergent_footer_keeps_professional_logo_and_metadata(monkeypatch):
    text = internal_client(monkeypatch, _convergent_db()).get("/owner-portal-convergent/SCENARIO").text
    assert '<img class="footer-logo" src="/static/logo.png" alt="PROCASA">' in text
    assert "SUCRE" in text
    assert "PROCASA SUCRE" not in text
    assert "Propiedad SCENARIO" in text
    assert "Corte de mercado" in text
    assert "La publicación no reemplaza una tasación profesional." in text


def legacy_convergent_real_photo_preserves_image_and_has_load_fallback(monkeypatch):
    text = internal_client(monkeypatch, _convergent_db()).get("/owner-portal-convergent/SCENARIO").text
    assert 'src="https://img/scenario.jpg"' in text
    assert "object-fit:cover" in text
    assert "image-failed" in text
    assert "hero-photo-placeholder" in text


def legacy_convergent_no_photo_uses_corporate_fallback_without_missing_photo_copy(monkeypatch):
    db = _convergent_db()
    db["propiedades_captacion"].delete_many({"listing_id": "SCENARIO"})
    text = internal_client(monkeypatch, db).get("/owner-portal-convergent/SCENARIO").text
    assert 'aria-label="Composición visual PROCASA"' in text
    assert "hero-photo-placeholder::before" in text
    assert "Fotografía no disponible" not in text


def legacy_convergent_without_comparables_uses_real_kpi_fallback_and_compact_market_block(monkeypatch):
    db = _convergent_db(with_lead=True)
    db["mercado_comunal"].delete_many({})
    db["propiedades_captacion"].delete_many({"listing_id": {"$regex": "^safe-cmp-"}})
    text = internal_client(monkeypatch, db).get("/owner-portal-convergent/SCENARIO").text
    assert "Superficie" in text
    assert "65 m²" in text
    assert "Posición competitiva" not in text
    assert "Actualmente no contamos con una muestra suficiente" in text
    assert "Tres referencias anónimas" not in text
    assert "Comparables utilizados" not in text
    assert "No disponible" not in text


def legacy_convergent_content_separates_comparable_dates_from_market_cut(monkeypatch):
    db = _convergent_db()
    db["propiedades_captacion"].update_many(
        {"listing_id": {"$regex": "^safe-cmp-"}},
        {"$set": {"fecha_publicacion": "2026-08-20"}},
    )

    view = service.get_owner_portal_property_view(
        db,
        "SCENARIO",
        datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc),
    )
    assert view is not None and view.comparable_cohort is not None
    assert view.market_as_of == "28/04/2026"
    assert view.comparable_cohort.examples
    assert view.comparable_cohort.examples[0].observed_at == "20/08/2026"
    assert view.comparable_cohort.examples[0].date_basis == "fecha de publicación"

    text = internal_client(monkeypatch, db).get("/owner-portal-convergent/SCENARIO").text
    assert "Fecha útil" in text
    assert "20/08/2026" in text
    assert "28/04/2026" in text
    assert "propiedades_captacion" in text
    assert "mercado_comunal" in text


def legacy_convergent_local_context_does_not_expose_untraceable_effective_price(monkeypatch):
    text = internal_client(monkeypatch, _convergent_db()).get("/owner-portal-convergent/SCENARIO").text.casefold()
    assert "uf/m² publicado" in text
    assert "uf/m² efectivo" not in text
    assert "precio efectivo" not in text
    assert "precio de cierre" not in text
    assert "valor transado" not in text


def legacy_convergent_national_context_requires_two_stored_indicators(monkeypatch):
    db = _convergent_db()
    service._NATIONAL_INDICATORS_CACHE.clear()
    without_context = internal_client(monkeypatch, db).get("/owner-portal-convergent/SCENARIO").text
    assert "Contexto Chile" not in without_context

    db["market_intelligence_snapshots_v1"].insert_many([
        {
            "indicator_id": "mortgage_rate_uf",
            "scope": "nacional",
            "geography": "Chile",
            "value": 4.04,
            "unit": "% anual",
            "reference_period": "2026-08",
            "source_name": "Banco Central de Chile",
            "source_reference": "https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx",
            "retrieved_at_utc": "2026-09-10T15:00:00+00:00",
            "status": "valid",
        },
        {
            "indicator_id": "housing_price_index",
            "scope": "nacional",
            "geography": "Chile",
            "value": 1.2,
            "unit": "% variación interanual",
            "reference_period": "2026-T2",
            "source_name": "Banco Central de Chile",
            "source_reference": "https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx",
            "retrieved_at_utc": "2026-09-10T15:00:00+00:00",
            "status": "valid",
        },
    ])
    service._NATIONAL_INDICATORS_CACHE.clear()
    with_context = internal_client(monkeypatch, db).get("/owner-portal-convergent/SCENARIO").text
    assert "Contexto Chile" in with_context
    assert "Referencias nacionales vigentes" in with_context


def test_convergent_narratives_are_deterministic_descriptive_and_bounded():
    from owner_portal.router import _convergent_payload

    view = service.get_owner_portal_property_view(
        _convergent_db(),
        "SCENARIO",
        datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc),
    )
    assert view is not None
    payload = _convergent_payload(view.to_dict())
    local_narrative = payload["local_market_narrative"]
    commercial_insight = payload["commercial_insight"]
    assert local_narrative.count(".") <= 2
    assert commercial_insight.count(".") <= 2
    for phrase in ("el mercado está cayendo", "hay menos demanda", "podría generar más consultas"):
        assert phrase not in f"{local_narrative} {commercial_insight}".casefold()


def legacy_convergent_v9_keeps_logo_assets_and_does_not_fabricate_recommendation(monkeypatch):
    from pathlib import Path
    from owner_portal.router import _convergent_payload

    text = internal_client(monkeypatch, _convergent_db()).get("/owner-portal-convergent/SCENARIO").text
    template_text = Path("templates/owner_portal_convergent.html").read_text(encoding="utf-8")
    assert "La lectura de su propiedad, hoy." in text
    assert "El comprador también está tomando decisiones en un entorno más exigente." in text
    assert "Tres decisiones posibles. Una recomendación." in text
    assert 'src="/static/logo.png" alt="Logo oficial PROCASA"' in text
    assert 'src="/static/logo.png" alt="PROCASA"' in text
    assert "/static/portal_logos/yapo.png" in template_text
    assert "/static/portal_logos/toctoc.png" in template_text
    assert "/static/portal_logos/portal-inmobiliario.png" in template_text
    assert "Revisión con ejecutivo" in text
    assert "RECOMENDADO" not in text
    assert "3.490 UF" not in text
    assert "-6,2%" not in text
    assert 'id="authorization-modal"' not in text

    view = service.get_owner_portal_property_view(
        _convergent_db(),
        "SCENARIO",
        datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc),
    )
    assert view is not None
    payload = _convergent_payload(view.to_dict())
    assert [signal["label"] for signal in payload["diagnosis_signals"]] == ["Exposición", "Respuesta", "Precio"]
    assert "podría generar más consultas" not in payload["diagnosis_narrative"].casefold()


def legacy_convergent_activity_feed_is_capped_and_discloses_older_events(monkeypatch):
    db = _convergent_db(with_lead=False)
    for index in range(6):
        db["leads"].insert_one({
            "_id": f"convergent-history-{index}",
            "created_at": datetime(2026, 9, 8 - index, 12, 0, tzinfo=timezone.utc).isoformat(),
            "prospecto": {"codigo": "SCENARIO"},
        })

    view = service.get_owner_portal_property_view(
        db,
        "SCENARIO",
        datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc),
    )
    assert view is not None
    assert len(view.timeline) == 6
    assert len(view.timeline[:5]) == 5

    text = internal_client(monkeypatch, db).get("/owner-portal-convergent/SCENARIO").text
    assert "Ver actividad anterior" in text


def legacy_convergent_market_context_explicitly_says_observed_publications(monkeypatch):
    text = internal_client(monkeypatch, _convergent_db()).get("/owner-portal-convergent/SCENARIO").text
    assert "avisos observados y no necesariamente propiedades únicas" in text


def legacy_convergent_presence_marquee_renders_only_verified_channels_and_deduplicates_counter(monkeypatch):
    doc = master_doc("PRESENCE")
    doc["publicaciones"] = {
        "yapo": {"publicaciones": {"venta": {"code": "YA-1", "estado": "active"}}},
        "toctoc": {"publicaciones": {"venta": {"code": "TO-1", "estado": "draft"}}},
    }
    text = internal_client(monkeypatch, make_db(doc)).get("/owner-portal-convergent/PRESENCE").text
    assert 'aria-label="1 canales activos"' in text
    assert text.count('data-portal-id="yapo"') == 2
    assert 'data-portal-id="toctoc"' not in text
    assert "publicación verificada" in text
    assert "1 canales con presencia verificada" in text


@pytest.mark.parametrize("count", (1, 3, 6))
def legacy_convergent_presence_marquee_supports_one_three_and_six_verified_channels(monkeypatch, count):
    catalog = (
        ("portal_inmobiliario", "Portal Inmobiliario"),
        ("toctoc", "TOCTOC"),
        ("yapo", "Yapo"),
        ("chilepropiedades", "ChilePropiedades"),
        ("proppit", "Proppit"),
        ("procasa", "Procasa"),
    )
    doc = master_doc("PRESENCE-SIZES")
    doc["publicaciones"] = {
        portal_id: {"publicaciones": {"venta": {"code": f"{portal_id}-1", "estado": "active"}}}
        for portal_id, _portal_name in catalog[:count]
    }
    text = internal_client(monkeypatch, make_db(doc)).get("/owner-portal-convergent/PRESENCE-SIZES").text
    assert f'aria-label="{count} canales activos"' in text
    assert text.count("class=\"portal-marquee-sequence\"") == 2
    logo_paths = {
        "portal_inmobiliario": "/static/portal_logos/portal-inmobiliario.png",
        "toctoc": "/static/portal_logos/toctoc.png",
        "yapo": "/static/portal_logos/yapo.png",
        "chilepropiedades": "/static/portal_logos/chilepropiedades.png",
        "proppit": "/static/portal_logos/proppit.png",
        "procasa": "/static/logo.png",
    }
    for portal_id, portal_name in catalog[:count]:
        assert text.count(f'class="portal-channel" data-portal-id="{portal_id}"') == 2
        if portal_id in logo_paths:
            assert f'src="{logo_paths[portal_id]}"' in text
        else:
            assert f'<span class="portal-wordmark">{portal_name}</span>' in text


def legacy_convergent_presence_marquee_has_reduced_motion_mobile_and_no_pii_contract():
    from pathlib import Path

    text = Path("templates/owner_portal_convergent.html").read_text(encoding="utf-8")
    assert "@keyframes portal-marquee" in text
    assert "animation-play-state:paused" in text
    assert "flex:0 0 auto" in text
    assert "portal-marquee-sequence + .portal-marquee-sequence" in text
    assert "overflow-x:auto" in text
    assert "touch-action:pan-x" in text
    assert "prefers-reduced-motion:reduce" in text
    assert "/static/portal_logos/portal-inmobiliario.png" in text
    assert "/static/portal_logos/mercado-libre.png" in text
    assert "/static/portal_logos/toctoc.png" in text
    assert "/static/portal_logos/yapo.png" in text
    assert "/static/portal_logos/chilepropiedades.png" in text
    assert "/static/portal_logos/proppit.png" in text
    assert "border:0" in text
    assert "filter:grayscale(1)" in text
    assert "filter:none" in text
    assert 'id="portal-publication-links"' in text
    assert "window.open(url, '_blank'" in text
    assert "portal-channel small" not in text
    assert 'portal_logo_paths.get(publication.portal_id)' in text
    assert "'procasa': '/static/logo.png'" in text
    assert "owner_email" not in text
    assert "owner_phone" not in text
    assert "direccion_exacta" not in text


def legacy_marketplace_publication_uses_verified_mercado_libre_logo_mapping(monkeypatch):
    doc = master_doc("MERCADO-LIBRE")
    doc["publicaciones"] = {
        "portal_inmobiliario": {
            "url_mercado_libre": "https://www.mercadolibre.cl/MLC-1",
            "publicaciones": {"venta": {"code": "ML-1", "estado": "active"}},
        }
    }
    text = internal_client(monkeypatch, make_db(doc)).get("/owner-portal-convergent/MERCADO-LIBRE").text
    assert 'data-portal-id="mercadolibre"' in text
    assert 'src="/static/portal_logos/mercado-libre.png"' in text


def legacy_portal_inmobiliario_and_mercado_libre_keep_their_verified_urls_separate(monkeypatch):
    doc = master_doc("MARKETPLACE-URLS")
    doc["publicaciones"] = {
        "portal_inmobiliario": {
            "url_pi": "https://portalinmobiliario.cl/MLC-PI-1",
            "url_mercado_libre": "https://www.mercadolibre.cl/MLC-ML-1",
            "publicaciones": {"venta": {"code": "ML-1", "estado": "active", "url": "https://www.mercadolibre.cl/MLC-ML-1"}},
        }
    }
    text = internal_client(monkeypatch, make_db(doc)).get("/owner-portal-convergent/MARKETPLACE-URLS").text
    assert '"portal_id":"portal_inmobiliario","url":"https://portalinmobiliario.cl/MLC-PI-1"' in text
    assert '"portal_id":"mercadolibre","url":"https://www.mercadolibre.cl/MLC-ML-1"' in text


def legacy_convergent_template_has_mobile_and_reduced_motion_contract():
    from pathlib import Path

    text = Path("templates/owner_portal_convergent.html").read_text(encoding="utf-8")
    assert '<html lang="es">' in text
    assert 'name="viewport"' in text
    assert 'alt="Logo oficial PROCASA"' in text
    assert 'class="footer-logo"' in text
    assert "market-empty" in text
    assert "hero-photo-placeholder" in text
    assert "@media (prefers-reduced-motion:reduce)" in text
    assert "@media (max-width:620px)" in text
    assert "window.fetch" not in text


def test_convergent_report_uses_the_new_single_report_order_and_real_data(monkeypatch):
    text = internal_client(monkeypatch, _convergent_db()).get("/owner-portal-convergent/SCENARIO").text
    ordered = [
        'class="hero shell"', 'id="diagnostico"', 'id="desempeno"', 'id="mercado"',
        'id="exposicion"', 'id="gestion"', 'id="contexto"',
        'id="propuesta"', 'id="contacto"', '<footer',
    ]
    positions = [text.index(value) for value in ordered]
    assert positions == sorted(positions)
    assert 'data-report="owner-performance"' in text
    assert "3.200 UF" in text
    assert "Calle privada" not in text


def test_convergent_summary_is_bounded_and_safe_without_benchmark(monkeypatch):
    db = _convergent_db(with_lead=False)
    db["mercado_comunal"].delete_many({})
    db["propiedades_captacion"].delete_many({"listing_id": {"$regex": "^safe-cmp-"}})
    text = internal_client(monkeypatch, db).get("/owner-portal-convergent/SCENARIO").text
    assert text.count('class="signals"') == 1
    assert "Aún no contamos con una muestra suficiente para una comparación representativa." in text
    assert "no se registraron consultas vinculadas" in text.casefold()
    assert "por debajo del mercado" not in text.casefold()


def test_convergent_portals_are_compact_verified_links_with_official_assets(monkeypatch):
    db = _convergent_db()
    db["universo_cartera_prop360"].update_one(
        {"codigo": "SCENARIO"},
        {"$set": {"publicaciones": {"yapo": {"publicaciones": {"venta": {
            "code": "YA-1", "estado": "active", "url": "https://example.test/yapo"
        }}}}}},
    )
    text = internal_client(monkeypatch, db).get("/owner-portal-convergent/SCENARIO").text
    assert 'class="portal-list"' in text
    assert 'href="https://example.test/yapo"' in text
    assert "/static/portal_logos/yapo.png" in text
    assert "data-portal-marquee" not in text
    assert "publicación verificada" not in text


def test_convergent_omits_simulator_forecast_and_unsafe_causal_claims(monkeypatch):
    text = internal_client(monkeypatch, _convergent_db()).get("/owner-portal-convergent/SCENARIO").text.casefold()
    for forbidden in ("type=\"range\"", "simulador", "forecast", "tiempo estimado de venta", "podría generar más consultas", "authorization-modal", "fetch("):
        assert forbidden not in text


def test_convergent_template_is_mobile_first_and_has_no_horizontal_scroll_contract():
    from pathlib import Path

    text = Path("templates/owner_portal_convergent.html").read_text(encoding="utf-8")
    assert '<html lang="es-CL">' in text
    assert 'name="viewport"' in text
    assert "overflow-x:hidden" in text
    assert "grid-template-columns:repeat(2,1fr)" in text
    assert "min-height:54px" in text
    assert "@media(min-width:768px)" in text
    assert 'src="/static/logo.png" alt="PROCASA"' in text
    assert "window.fetch" not in text
