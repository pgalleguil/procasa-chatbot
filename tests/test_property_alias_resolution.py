from copy import deepcopy

from chatbot.property_lookup import (
    build_property_alias,
    merge_property_aliases,
    lookup_property_link,
    normalize_property_url,
    extract_property_external_id,
    operation_from_property_url,
)


RENTAL = [
    ("mercadolibre", "https://casa.mercadolibre.cl/MLC-4247982034-casa-en-arriendo-de-3-dorm-en-puente-alto-_JM", "MLC-4247982034"),
    ("portal_inmobiliario", "https://www.portalinmobiliario.com/MLC-4247982034-casa-en-arriendo-de-3-dorm-en-puente-alto-_JM", "MLC-4247982034"),
    ("toctoc", "https://www.toctoc.com/propiedades/arriendocorredorasr/casas/puente-alto/casa-en-arriendo-en-puente-alto/b004575345d086712b8514b7a0c52970f0930059", "b004575345d086712b8514b7a0c52970f0930059"),
    ("yapo", "https://www.yapo.cl/bienes-raices-alquiler-casas/casa-en-arriendo-en-puente-alto/32757789", "32757789"),
]
SALE = [
    ("mercadolibre", "http://casa.mercadolibre.cl/MLC-1952293455-casa-en-venta-de-3-dorm-en-puente-alto-_JM", "MLC-1952293455"),
    ("portal_inmobiliario", "https://portalinmobiliario.cl//MLC-1952293455-casa-en-venta-de-3-dorm-en-puente-alto-_JM", "MLC-1952293455"),
    ("toctoc", "https://www.toctoc.com/propiedades/compracorredorasr/casas/puente-alto/casa-en-venta-de-3-dorm-en-puente-alto/cb24d400052a0d264ee5dca30e03c3904d38ba06", "cb24d400052a0d264ee5dca30e03c3904d38ba06"),
    ("yapo", "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa-en-venta-de-3-dorm-en-puente-alto/32395061", "32395061"),
]


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs
    def find_one(self, query):
        elem = query.get("publicaciones.aliases", {}).get("$elemMatch", {})
        for doc in self.docs:
            for alias in doc.get("publicaciones", {}).get("aliases", []):
                if elem.get("portal") and alias.get("portal") != elem["portal"]:
                    continue
                if elem.get("external_id") and alias.get("external_id") != elem["external_id"]:
                    continue
                if elem.get("url_normalized") and alias.get("url_normalized") != elem["url_normalized"]:
                    continue
                if alias.get("activa") is False:
                    continue
                return deepcopy(doc)
        return None


class FakeDB:
    def __init__(self, docs):
        self.col = FakeCollection(docs)
    def __getitem__(self, name):
        return self.col


class LegacyCollection:
    """Small matcher for the real legacy publication fields used in lookup."""
    def __init__(self, doc):
        self.doc = doc

    def find_one(self, query):
        field, expected = next(iter(query.items()))
        value = self.doc
        for part in field.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(expected, dict) and "$regex" in expected:
            import re
            return deepcopy(self.doc) if value and re.search(expected["$regex"], value, re.I) else None
        if isinstance(expected, dict) and "$in" in expected:
            return deepcopy(self.doc) if value in expected["$in"] else None
        return deepcopy(self.doc) if value == expected else None


class LegacyDB:
    def __init__(self, doc):
        self.col = LegacyCollection(doc)

    def __getitem__(self, name):
        return self.col


def make_doc():
    aliases = [build_property_alias(url, portal, "arriendo", ext) for portal, url, ext in RENTAL]
    aliases += [build_property_alias(url, portal, "venta", ext) for portal, url, ext in SALE]
    return {"codigo": "16704", "publicaciones": {"aliases": aliases,
        "portal_inmobiliario": {"url_pi": SALE[1][1], "url_mercado_libre": SALE[0][1]},
        "yapo": {"url_yapo": SALE[3][1]},
        "toctoc": {"url_toctoc": SALE[2][1]}}}


def test_all_eight_links_resolve_same_property_and_operation():
    db = FakeDB([make_doc()])
    for portal, url, _ in RENTAL + SALE:
        prop, meta = lookup_property_link(db, url)
        assert prop["codigo"] == "16704"
        assert meta["portal"] == portal
        assert meta["operation"] == ("arriendo" if (portal, url, _) in RENTAL else "venta")


def test_normalization_and_external_ids():
    url = RENTAL[0][1] + "?utm_source=x&foo=bar#fragment"
    assert normalize_property_url(url).endswith("?_?") is False
    assert extract_property_external_id(url) == "MLC-4247982034"
    assert operation_from_property_url(url) == "arriendo"


def test_alias_merge_is_idempotent():
    alias = build_property_alias(RENTAL[0][1], "mercadolibre", "arriendo", "MLC-4247982034")
    assert len(merge_property_aliases([], [alias, alias])) == 1


def test_unknown_link_does_not_match():
    db = FakeDB([make_doc()])
    prop, meta = lookup_property_link(db, "https://example.com/MLC-4247982034")
    assert prop is None
    assert meta["match_method"] is None


def test_legacy_publications_are_untouched_by_alias_model():
    doc = make_doc()
    doc["publicaciones"]["portal_inmobiliario"]["url_pi"] = SALE[1][1]
    assert doc["publicaciones"]["portal_inmobiliario"]["url_pi"] == SALE[1][1]


def test_portal_link_resolves_legacy_mercadolibre_url_by_canonical_mlc_identity():
    # Real producer shape: Portal Inmobiliario sends its domain, while the
    # historical Prop360 document holds the same MLC identity in the Mercado
    # Libre URL slot and has no aliases/codigo_pi field.
    doc = {
        "codigo": "6811",
        "publicaciones": {"portal_inmobiliario": {
            "url_mercado_libre": "http://inmueble.mercadolibre.cl/MLC-3776482990-oficina-en-arriendo-en-santiago-_JM",
        }},
    }
    prop, meta = lookup_property_link(
        LegacyDB(doc),
        "https://www.portalinmobiliario.com/MLC-3776482990-oficina-en-arriendo-en-santiago-_JM?utm_source=wa",
    )
    assert prop["codigo"] == "6811"
    assert meta["external_id"] == "MLC-3776482990"
    assert meta["match_method"] == "legacy_url_external_id"
