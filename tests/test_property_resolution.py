import re
from copy import deepcopy

from chatbot.property_lookup import (
    find_property_by_any_identifier,
    lookup_property_link,
    normalize_property_url,
)


def _get_path(document, path):
    value = document
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _matches_value(actual, expected):
    if isinstance(expected, dict):
        if "$in" in expected:
            return actual in expected["$in"]
        if "$ne" in expected:
            return actual != expected["$ne"]
        if "$regex" in expected:
            return bool(actual) and re.search(expected["$regex"], str(actual), re.I if expected.get("$options") == "i" else 0)
        return False
    return actual == expected


class Collection:
    def __init__(self, documents):
        self.documents = documents

    def find(self, query):
        if "$or" in query:
            results = {}
            for clause in query["$or"]:
                for document in self.find(clause):
                    results[str(document.get("codigo") or document.get("_id"))] = document
            return list(results.values())
        path, expected = next(iter(query.items()))
        matches = []
        for document in self.documents:
            if path == "publicaciones.aliases":
                elem = expected.get("$elemMatch", {})
                for alias in document.get("publicaciones", {}).get("aliases", []):
                    if all(_matches_value(alias.get(key), value) for key, value in elem.items()):
                        matches.append(deepcopy(document))
                        break
                continue
            actual = _get_path(document, path)
            if isinstance(actual, list):
                matched = any(_matches_value(item, expected) for item in actual)
            else:
                matched = _matches_value(actual, expected)
            if matched:
                matches.append(deepcopy(document))
        return matches

    def find_one(self, query):
        found = self.find(query)
        return found[0] if found else None


class DB:
    def __init__(self, documents):
        self.collection = Collection(documents)

    def __getitem__(self, _name):
        return self.collection


CASES = (
    ("6473", "MLC-2268754129", "http://casa.mercadolibre.cl/MLC-2268754129-casa-en-venta-en-la-serena-_JM",
     "MLC-3776482370", "https://portalinmobiliario.cl/MLC-3776482370-casa-en-venta-en-la-serena-_JM"),
    ("6631", "MLC-2268779819", "http://departamento.mercadolibre.cl/MLC-2268779819-departamento-en-venta-en-las-condes-_JM",
     "MLC-3776445222", "https://portalinmobiliario.cl/MLC-3776445222-departamento-en-venta-en-las-condes-_JM"),
    ("6852", "MLC-2268767199", "http://departamento.mercadolibre.cl/MLC-2268767199-departamento-en-venta-en-independencia-_JM",
     "MLC-3776603980", "https://portalinmobiliario.cl/MLC-3776603980-departamento-en-venta-en-independencia-_JM"),
)
CURRENT_URL = CASES[0][2]
OLD_URL = CASES[0][4]


def _doc(code="6473", current_mlc=None, current_url=None, old_mlc=None, old_url=None):
    if current_mlc is None:
        _, current_mlc, current_url, old_mlc, old_url = CASES[0]
    return {
        "codigo": code,
        "publicaciones": {
            "portal_inmobiliario": {
                "publicaciones": {"V": {"code": current_mlc, "url": current_url}},
            },
            "aliases": [{
                "portal": "portal_inmobiliario",
                "external_id": old_mlc,
                "url": old_url,
                "url_normalized": normalize_property_url(old_url),
                "activa": False,
                "publication_status": "historical",
                "resolvable": True,
            }],
        },
    }


def test_current_and_historical_mlc_and_url_resolve_to_same_property():
    for code, current_mlc, current_url, old_mlc, old_url in CASES:
        db = DB([_doc(code, current_mlc, current_url, old_mlc, old_url)])
        for reference in (current_mlc, old_mlc, current_url, old_url):
            prop, meta = lookup_property_link(db, reference)
            assert prop["codigo"] == code
            assert meta.get("error_code") is None


def test_historical_alias_resolves_even_when_publication_is_inactive():
    doc = _doc()
    prop, meta = lookup_property_link(DB([doc]), CASES[0][3])
    assert prop["codigo"] == "6473"
    assert meta["publication_status"] == "historical"


def test_general_identifier_lookup_uses_historical_mlc_alias():
    prop = find_property_by_any_identifier(DB([_doc()]), CASES[0][3])
    assert prop["codigo"] == "6473"


def test_explicitly_unresolvable_alias_is_ignored():
    doc = _doc()
    doc["publicaciones"]["aliases"][0]["resolvable"] = False
    prop, meta = lookup_property_link(DB([doc]), CASES[0][3])
    assert prop is None
    assert meta.get("error_code") is None


def test_conflicting_mlc_claims_are_ambiguous():
    first = _doc("6473")
    second = _doc("6631")
    second["publicaciones"]["portal_inmobiliario"]["publicaciones"]["V"]["code"] = CASES[0][3]
    prop, meta = lookup_property_link(DB([first, second]), CASES[0][3])
    assert prop is None
    assert meta["error_code"] == "AMBIGUOUS_PROPERTY_REFERENCE"
    assert meta["candidate_codes"] == ["6473", "6631"]
