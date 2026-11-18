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
