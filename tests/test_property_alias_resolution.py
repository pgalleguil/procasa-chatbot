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
        return next(iter(self.find(query)), None)

    def find(self, query):
        def get_path(obj, path):
            value = obj
            for part in path.split("."):
                if isinstance(value, list):
                    value = [item.get(part) for item in value if isinstance(item, dict)]
                else:
                    value = value.get(part) if isinstance(value, dict) else None
            return value
        def matches_value(value, expected):
            import re
            vals = value if isinstance(value, list) else [value]
            if isinstance(expected, dict) and "$elemMatch" in expected:
                return any(matches(item, expected["$elemMatch"]) for item in vals if isinstance(item, dict))
            if isinstance(expected, dict) and "$in" in expected:
                return any(v in expected["$in"] for v in vals)
            if isinstance(expected, dict) and "$ne" in expected:
                return all(v != expected["$ne"] for v in vals)
            if isinstance(expected, dict) and "$regex" in expected:
                return any(re.search(expected["$regex"], str(v or ""), re.I) for v in vals)
            return any(v == expected for v in vals)
        def matches(doc, q):
            if "$or" in q:
                return any(matches(doc, sub) for sub in q["$or"])
            return all(matches_value(get_path(doc, key), expected) for key, expected in q.items())
        return [deepcopy(doc) for doc in self.docs if matches(doc, query)]


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
    assert extract_property_external_id("https://www.portalinmobiliario.com/MLC4247982034", "portal_inmobiliario") == "MLC-4247982034"
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


def test_closed_historical_alias_remains_resolvable():
    url = "https://www.portalinmobiliario.com/MLC-3776000001-casa-en-venta"
    alias = build_property_alias(url, "portal_inmobiliario", "venta", "MLC-3776000001",
                                 active=False, publication_status="historical", resolvable=True)
    doc = {"codigo": "6473", "publicaciones": {"aliases": [alias]}}
    prop, meta = lookup_property_link(FakeDB([doc]), "MLC-3776000001")
    assert prop["codigo"] == "6473"
    assert meta["match_method"] == "historical_alias_external_id"
    assert meta["publication_status"] == "historical"
    assert alias["activa"] is False and alias["resolvable"] is True


def test_duplicate_mlc_claim_returns_ambiguous_without_guessing():
    alias = build_property_alias("https://portalinmobiliario.com/MLC-5555555555-x",
                                 "portal_inmobiliario", external_id="MLC-5555555555",
                                 active=False, publication_status="historical")
    docs = [
        {"codigo": "6473", "publicaciones": {"aliases": [alias]}},
        {"codigo": "6631", "publicaciones": {"portal_inmobiliario": {
            "url_mercado_libre": "https://casa.mercadolibre.cl/MLC-5555555555-y"}}},
    ]
    prop, meta = lookup_property_link(FakeDB(docs), "MLC-5555555555")
    assert prop is None
    assert meta["error_code"] == "AMBIGUOUS_PROPERTY_REFERENCE"
    assert set(meta["candidate_codes"]) == {"6473", "6631"}


def test_generic_identifier_lookup_does_not_fall_through_ambiguous_reference():
    docs = [
        {"codigo": "6473", "codigo_mercadolibre": "5555555555"},
        {"codigo": "6631", "codigo_mercadolibre": "5555555555"},
    ]
    from chatbot.property_lookup import find_property_by_any_identifier
    assert find_property_by_any_identifier(FakeDB(docs), "MLC-5555555555") is None


def test_duplicate_normalized_url_claim_returns_ambiguous():
    url = "https://www.portalinmobiliario.com/MLC-6666666666-casa-venta"
    alias_a = build_property_alias(url, "portal_inmobiliario", "venta", "MLC-6666666666",
                                  active=False, publication_status="historical")
    alias_b = {**alias_a, "external_id": "MLC-7777777777"}
    prop, meta = lookup_property_link(FakeDB([
        {"codigo": "6473", "publicaciones": {"aliases": [alias_a]}},
        {"codigo": "6631", "publicaciones": {"aliases": [alias_b]}},
    ]), url)
    assert prop is None
    assert meta["error_code"] == "AMBIGUOUS_PROPERTY_REFERENCE"
    assert set(meta["candidate_codes"]) == {"6473", "6631"}


def test_alias_builder_and_merge_are_idempotent_with_historical_flags():
    alias = build_property_alias("https://portalinmobiliario.com/MLC-4444444444-x",
                                 "portal_inmobiliario", external_id="MLC-4444444444",
                                 active=False, publication_status="historical", resolvable=True)
    once = merge_property_aliases([], [alias])
    twice = merge_property_aliases(once, [alias])
    assert once == twice
    assert len(twice) == 1
    assert twice[0]["publication_status"] == "historical"
    assert twice[0]["resolvable"] is True
    disabled = [{**alias, "resolvable": False}]
    assert merge_property_aliases(disabled, [alias])[0]["resolvable"] is False
