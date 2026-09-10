from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    assert_owner_portal_payload_allowlisted,
)


def master_doc(code: str, office: str = "PROCASA SUCRE", *, active: bool = True, price: bool = True, photo_code: str | None = None, **extra):
    doc = {
        "codigo": code,
        "estado": {"oficina": office, "disponible_prop360": active},
        "tipo_operacion": {
            "tipo": "Departamento",
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


def test_no_photo_fallback(monkeypatch):
    db = make_db(master_doc("S1"))
    response = internal_client(monkeypatch, db).get("/owner-portal-preview/S1")
    assert response.status_code == 200
    assert "hero-placeholder" in response.text


def test_no_price_is_explicit(monkeypatch):
    db = make_db(master_doc("S1", price=False), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    response = internal_client(monkeypatch, db).get("/owner-portal-preview/S1")
    assert response.status_code == 200
    assert "No disponible" in response.text


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
    assert "no verifica asistencia" in text


def test_responsive_render_contract(monkeypatch):
    db = make_db(master_doc("S1"), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    text = internal_client(monkeypatch, db).get("/owner-portal-preview/S1").text
    assert 'name="viewport"' in text
    assert "@media (max-width: 700px)" in text


def test_updated_date_uses_verified_master_field(monkeypatch):
    db = make_db(master_doc("S1", estado={"oficina": "PROCASA SUCRE", "disponible_prop360": True, "ultima_actualizacion": "2026-09-10T10:00:00-03:00"}), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    text = internal_client(monkeypatch, db).get("/owner-portal-preview/S1").text
    assert "Datos actualizados al 10/09/2026" in text


def test_market_below_minimum_hides_representative_median_and_range():
    db = make_db(master_doc("S1"), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    prop = service._safe_property(db["universo_cartera_prop360"].find_one({"codigo": "S1"}), ["https://img/s.jpg"])
    market = service._market_context(db, prop, datetime.now(timezone.utc) + timedelta(days=1))
    assert market["minimum_comparables"] == 5
    assert market["comparables_count"] == 0
    assert market["median_price_uf"] is None
    assert market["range_price_uf"] == [None, None]


def test_market_at_minimum_exposes_descriptive_median():
    db = make_db(master_doc("S1"), captures=[{"listing_id": "S1", "image_urls": ["https://img/s.jpg"]}])
    for index in range(5):
        db["propiedades_captacion"].insert_one({"listing_id": f"cmp-{index}", "comuna": "Santiago", "tipo_propiedad": "Departamento", "operacion": "venta", "precio_uf": 3000 + index * 100, "superficie": 60 + index})
    prop = service._safe_property(db["universo_cartera_prop360"].find_one({"codigo": "S1"}), ["https://img/s.jpg"])
    market = service._market_context(db, prop, datetime.now(timezone.utc) + timedelta(days=1))
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
    market = service._market_context(db, prop, datetime.now(timezone.utc) + timedelta(days=1))
    assert market["comparables_count"] == 1


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
    market = service._market_context(db, prop, datetime.now(timezone.utc) + timedelta(days=1))
    assert market["comparables_count"] == 1
    assert market["median_price_uf"] is None


def test_single_as_of_is_reused_by_leads_and_market(monkeypatch):
    db = make_db(master_doc("S1"))
    as_of = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
    observed: list[datetime] = []
    monkeypatch.setattr(
        service,
        "_lead_metrics",
        lambda _db, _code, cutoff: (observed.append(cutoff) or {"property_previous_7d": 0, "property_previous_30d": 0}),
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
