"""Prevent portal-specific misses from being disguised as Yapo lookups."""
import mongomock

from chatbot import link_extractor, inventory_reconciliation


URL = "https://www.portalinmobiliario.com/MLC-2406218977-departamento-en-arriendo-de-1-dorm-en-santiago-_JM"


def test_pending_inventory_reconciliation_reads_nested_mlc_publications():
    property_doc = {
        "codigo": "18001",
        "disponible_prop360": True,
        "publicaciones": {
            "portal_inmobiliario": {
                "publicaciones": {
                    "A": {
                        "code": "MLC2406218977",
                        "url": URL,
                        "external_id": ["MLC-2406218977"],
                    }
                }
            }
        },
    }
    identity = inventory_reconciliation._property_identities(property_doc)
    assert "MLC-2406218977" in identity["external_ids"]
    assert inventory_reconciliation.normalize_property_url(URL) in identity["urls"]
    event = {"source_property_code": URL, "source_external_id": "MLC-2406218977"}
    assert inventory_reconciliation._event_matches_inventory(event, identity)


def test_portal_lookup_failure_debug_does_not_claim_yapo_was_queried(monkeypatch):
    from chatbot import storage

    db = mongomock.MongoClient().URLS
    writes = []
    monkeypatch.setattr(link_extractor, "get_db", lambda: db)
    monkeypatch.setattr(storage, "actualizar_prospecto", lambda phone, data, trace_id=None: writes.append(data))
    has_link, prop, portal, external_id = link_extractor.analizar_mensaje_para_link(
        f"Hola, revisa {URL}", phone="+56946433269", trace_id="regression-test",
    )
    assert has_link is True
    assert prop is None
    assert portal == "PortalInmobiliario"
    assert external_id in {"MLC-2406218977", "2406218977"}
    assert writes
    debug = writes[-1]["debug_link"]
    assert debug["resultado"] == "NOT_FOUND"
    assert debug["ruta_prioritaria"] == "publicaciones.portal_inmobiliario"
    actual_queries = str(debug["query_usada"])
    assert "publicaciones.portal_inmobiliario.url_pi" in actual_queries
    assert "yapo.url_yapo" not in actual_queries
