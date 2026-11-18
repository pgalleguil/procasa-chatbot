"""A republished Portal Inmobiliario MLC resolves only via verified publisher ID."""
import mongomock
import pytest

from chatbot.portal_verified_reference import (
    COLLECTION, _CACHE, _publisher_disclosed_code,
    publication_identity, persist_verified_portal_link,
)
from chatbot.property_lookup import PROPERTY_COLLECTION_NAME, lookup_property_link


URL = "https://www.portalinmobiliario.com/MLC-2406218977-departamento-en-arriendo-de-1-dorm-en-santiago-_JM"
CODE = "17081"


def _db(*, available=True, code=CODE, sale=False, rent=True):
    db = mongomock.MongoClient().URLS
    db[PROPERTY_COLLECTION_NAME].insert_one({
        "codigo": code,
        "disponible_prop360": available,
        "estado": {"oficina": "PROCASA SUCRE", "ejecutivo": "Susana Ensignia"},
        "tipo_operacion": {"tipo": "Departamento", "venta": sale, "arriendo": rent},
        "publicaciones": {"portal_inmobiliario": {
            "url_pi": "https://portalinmobiliario.cl/MLC-2268754187-departamento-en-arriendo-de-1-dorm-en-santiago-_JM",
        }},
    })
    return db


class FakeResponse:
    status_code = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}
    def __init__(self, html):
        self.html = html
    def iter_content(self, chunk_size=0):
        yield self.html.encode("utf-8")
    def close(self):
        pass


def _page(code="17081", *, seller=True, public_id="2406218977"):
    agency = "Publicado por Procasa Sucre Propiedades" if seller else "Publicado por Otra Empresa"
    return (f"<html><body>{agency}<h2>Código de la propiedad</h2>"
            f"<div>{code}</div><span>Publicación #{public_id}</span></body></html>")


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    _CACHE.clear()
    monkeypatch.setenv("PORTAL_PROPERTY_LIVE_VERIFY", "true")
    yield
    _CACHE.clear()


def test_missing_mlc_resolves_to_unique_active_procasa_code(monkeypatch):
    from chatbot import portal_verified_reference as verifier
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(_page())
    monkeypatch.setattr(verifier.requests, "get", get)
    db = _db()
    prop, meta = lookup_property_link(db, URL)
    assert prop["codigo"] == CODE
    assert meta["match_method"] == "publisher_verified_internal_code"
    assert meta["verified_external_id"] == "MLC-2406218977"
    assert meta["operation"] == "arriendo"
    assert len(calls) == 1
    assert calls[0][1]["allow_redirects"] is False
    assert persist_verified_portal_link(db, URL, CODE, "public_portal")
    assert db[COLLECTION].count_documents({}) == 1

    # Durable mapping works without any outgoing request on the next lead.
    monkeypatch.setattr(verifier.requests, "get", lambda *a, **kw: pytest.fail("should use persisted mapping"))
    prop, meta = lookup_property_link(db, URL)
    assert prop["codigo"] == CODE
    assert meta["match_method"] == "verified_portal_mapping"


@pytest.mark.parametrize("page", [
    _page(seller=False),
    _page(code="17081", public_id="9999999999"),
    "<html><body>Publicado por Procasa Sucre Propiedades Publicación #2406218977</body></html>",
    _page(code="17081") + _page(code="99999"),
])
def test_rejects_unverified_or_ambiguous_publisher_code(monkeypatch, page):
    from chatbot import portal_verified_reference as verifier
    monkeypatch.setattr(verifier.requests, "get", lambda *a, **kw: FakeResponse(page))
    result = _publisher_disclosed_code(URL)
    assert result is None


def test_inactive_or_wrong_operation_never_autoroutes(monkeypatch):
    from chatbot import portal_verified_reference as verifier
    monkeypatch.setattr(verifier.requests, "get", lambda *a, **kw: FakeResponse(_page()))
    for db in (_db(available=False), _db(sale=True, rent=False)):
        prop, _meta = lookup_property_link(db, URL)
        assert prop is None


def test_wrong_domain_and_missing_mlc_rejected_without_network(monkeypatch):
    from chatbot import portal_verified_reference as verifier
    monkeypatch.setattr(verifier.requests, "get", lambda *a, **kw: pytest.fail("untrusted URL must not be fetched"))
    for url in [
        "https://portalinmobiliario.com.evil.example/MLC-2406218977",
        "http://www.portalinmobiliario.com/MLC-2406218977-test",
        "https://www.portalinmobiliario.com.evil.example/MLC-2406218977",
        "https://www.portalinmobiliario.com/otro/MLC-2406218977",
        "https://www.portalinmobiliario.com@evil.example/MLC-2406218977",
    ]:
        assert publication_identity(url) is None


def test_verified_mapping_conflict_never_selects_other_property():
    db = _db()
    db[COLLECTION].insert_one({
        "_id": "portal_inmobiliario:MLC-2406218977",
        "portal": "portal_inmobiliario", "external_id": "MLC-2406218977",
        "property_code": "99999", "verification_source": "operator_verified_public_portal",
    })
    prop, meta = lookup_property_link(db, URL)
    assert prop is None
    assert meta["error_code"] == "VERIFIED_REFERENCE_INACTIVE_OR_CONFLICT"


def test_live_verification_can_be_disabled(monkeypatch):
    from chatbot import portal_verified_reference as verifier
    monkeypatch.setenv("PORTAL_PROPERTY_LIVE_VERIFY", "false")
    monkeypatch.setattr(verifier.requests, "get", lambda *a, **kw: pytest.fail("flag must gate remote requests"))
    prop, meta = lookup_property_link(_db(), URL)
    assert prop is None
    assert meta["resolvable"] is False


def test_original_pending_event_assigns_verified_property_to_susana_without_duplicate(monkeypatch):
    from bson import ObjectId
    from chatbot import commercial_intake
    from chatbot.inventory_reconciliation import resume_waiting_event

    db = _db()
    lead_id = ObjectId("64b000000000000000000101")
    db.leads.insert_one({
        "_id": lead_id, "phone": "+56912345678",
        "lead_temperature_effective": "COLD",
        "assignment_type": "MISSING_PROPERTY",
        "commercial_processing_state": "waiting_inventory_sync",
        "prospecto": {"link_pendiente": True, "operacion": "Arriendo"},
    })
    db.usuarios.insert_one({
        "_id": ObjectId("64b000000000000000000102"),
        "nombre": "Susana Ensignia",
        "telefono": "+56911111111",
        "is_active": True, "rol": "agente",
    })
    db.commercial_processing_events.insert_one({
        "source_inbound_provider_id": "provider-recovered-original",
        "lead_id": lead_id, "phone": "+56912345678",
        "source_property_code": URL,
        "source_external_id": "MLC-2406218977",
        "source_message": f"Hola, preguntas sobre {URL}",
        "commercial_processing_state": "waiting_inventory_sync",
    })
    assert persist_verified_portal_link(db, URL, CODE, "operator_verified_public_portal")
    monkeypatch.setattr(
        commercial_intake, "find_responsible_executive",
        lambda **kwargs: ("Susana Ensignia", "+56911111111", "PROPERTY"),
    )
    from chatbot import crm_non_hot_digest, crm_hot_delivery
    monkeypatch.setattr(crm_non_hot_digest, "accumulate_non_hot_lead", lambda *a, **kw: None)
    monkeypatch.setattr(
        crm_hot_delivery, "assign_and_enqueue_hot",
        lambda *a, **kw: pytest.fail("COLD event may not send a HOT notice"),
    )
    recovered = resume_waiting_event(db, db.commercial_processing_events.find_one({
        "source_inbound_provider_id": "provider-recovered-original",
    }))
    fresh = db.leads.find_one({"_id": lead_id})
    cycles = list(db.crm_assignment_cycles.find({"lead_id": lead_id}))
    assert recovered["status"] == "completed"
    assert fresh["prospecto"]["codigo"] == CODE
    assert fresh["ejecutivo_asignado"] == "Susana Ensignia"
    assert len(cycles) == 1
    assert cycles[0]["property_code"] == CODE
    assert cycles[0]["assigned_to_display_name"] == "Susana Ensignia"
    assert db.commercial_processing_events.count_documents({
        "source_inbound_provider_id": "provider-recovered-original",
    }) == 1
    repeated = commercial_intake.process_inbound(
        db, inbound_provider_id="provider-recovered-original",
        phone="+56912345678", text=f"Hola, preguntas sobre {URL}",
    )
    assert repeated["commercial_processing_state"] == "completed"
    assert db.crm_assignment_cycles.count_documents({"lead_id": lead_id}) == 1
