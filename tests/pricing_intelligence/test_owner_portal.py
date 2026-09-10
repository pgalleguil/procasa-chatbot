from __future__ import annotations

from datetime import datetime, timezone

import mongomock
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from owner_portal import service
from owner_portal.router import router
from owner_portal.security import require_internal_preview


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
        **extra,
    }
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
    view = service.build_owner_portal_view(db, "S1")
    assert view["lead_metrics"]["total_linked_sucre"] == 0
    assert view["lead_metrics"]["excluded_other_office_linked"] == 1


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


def test_no_ml_execution():
    db = make_db(master_doc("S1"))
    view = service.build_owner_portal_view(db, "S1")
    assert view["machine_learning_status"] == "NOT_EXECUTED"


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

    event = portal_event_contract("portal_opened", property_code="S1", metadata={"surface": "mobile"})
    assert event.event_name == "portal_opened"
    assert "email" not in event.metadata
