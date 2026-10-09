import mongomock
import pytest

from chatbot import link_extractor
from chatbot.property_lookup import PROPERTY_COLLECTION_NAME
from chatbot.utils import extraer_rut, validar_rut


# Current exact Prop360 inventory snapshot: these international identifiers
# also have a mathematically valid module-11 check digit.
INTERNATIONAL_RUT_COLLISIONS = (
    "101006506", "101006581", "101006697", "102006062", "102006100",
    "102006186", "102006232", "102006313", "102006429", "102006437",
    "102006461", "102006518", "102006623", "102006631", "102006704",
    "102006739", "102006755", "102006852", "102017005", "102017048",
    "104006507", "104006787", "104006833", "104016588", "105006810",
    "105006888", "114005347", "114005924", "114006483", "114006491",
    "114006726", "114017213", "117006751", "203006543", "203006691",
    "204006512", "217006678",
)


def test_chilean_rut_uses_standard_repeating_2_to_7_mod11_weights():
    assert validar_rut("123456785") is True
    assert validar_rut("10100676K") is True
    assert validar_rut("101006170") is False
    assert validar_rut("101006766") is False


def test_rut_extraction_requires_personal_context_and_drops_urls():
    assert extraer_rut("Mi RUT es 12.345.678-5") == "12345678-5"
    assert extraer_rut("12.345.678-5") is None
    assert extraer_rut("Código de propiedad: 10.100.650-6") is None
    assert extraer_rut("https://enlaceinmobiliario.cl/ficha/101006506") is None
    assert extraer_rut("10.100.650-6", allow_unlabelled=True) == "10100650-6"
    assert extraer_rut(
        "La propiedad 10.100.650-6", allow_unlabelled=True,
    ) is None


def test_all_current_rut_collision_codes_resolve_as_exact_properties(monkeypatch):
    assert len(INTERNATIONAL_RUT_COLLISIONS) == 37
    assert all(validar_rut(code) for code in INTERNATIONAL_RUT_COLLISIONS)

    db = mongomock.MongoClient().URLS
    db[PROPERTY_COLLECTION_NAME].insert_many([
        {"codigo": str(index + 1), "publicaciones": {"codigo_internacional": code}}
        for index, code in enumerate(INTERNATIONAL_RUT_COLLISIONS)
    ])
    monkeypatch.setattr(link_extractor, "get_db", lambda: db)

    explicit_resolved = 0
    bare_resolved = 0
    for index, code in enumerate(INTERNATIONAL_RUT_COLLISIONS, start=1):
        result = link_extractor.resolver_referencia_propiedad(
            f"Código internacional {code}", use_legacy_lookup=False,
        )
        assert result["status"] == "resolved", code
        assert result["explicit_codes"] == [code]
        assert result["property"]["codigo"] == str(index), code
        explicit_resolved += 1

        bare_result = link_extractor.resolver_referencia_propiedad(
            code, use_legacy_lookup=False,
        )
        assert bare_result["status"] == "resolved", code
        assert bare_result["explicit_codes"] == [code]
        assert bare_result["property"]["codigo"] == str(index), code
        bare_resolved += 1

    assert explicit_resolved == 37
    assert bare_resolved == 37


def test_ambiguous_collision_code_does_not_resolve_to_arbitrary_property(monkeypatch):
    db = mongomock.MongoClient().URLS
    db[PROPERTY_COLLECTION_NAME].insert_many([
        {"codigo": "6508", "publicaciones": {"codigo_internacional": "101006506"}},
        {"codigo": "6509", "publicaciones": {"codigo_internacional": "101006506"}},
    ])
    monkeypatch.setattr(link_extractor, "get_db", lambda: db)

    result = link_extractor.resolver_referencia_propiedad(
        "Código internacional 101006506", use_legacy_lookup=False,
    )
    assert result["status"] == "ambiguous"
    assert result["error_code"] == "AMBIGUOUS_PROPERTY_REFERENCE"


def test_bare_rut_collision_during_optional_rut_request_requires_clarification(monkeypatch):
    db = mongomock.MongoClient().URLS
    db[PROPERTY_COLLECTION_NAME].insert_one({
        "codigo": "6508", "publicaciones": {"codigo_internacional": "101006506"},
    })
    monkeypatch.setattr(link_extractor, "get_db", lambda: db)

    result = link_extractor.resolver_referencia_propiedad(
        "101006506", use_legacy_lookup=False,
        expected_personal_data_field="rut",
    )

    assert result["status"] == "ambiguous"
    assert result["error_code"] == "NUMERIC_PROPERTY_OR_RUT"
    assert result["property"] is None


def test_bare_phone_or_rut_without_exact_property_is_not_a_property_reference(monkeypatch):
    db = mongomock.MongoClient().URLS
    monkeypatch.setattr(link_extractor, "get_db", lambda: db)

    for value in ("912345678", "123456785"):
        result = link_extractor.resolver_referencia_propiedad(value, use_legacy_lookup=False)
        assert result["status"] == "no_reference", value
        assert result["property"] is None


def test_bare_phone_matching_a_property_requires_clarification(monkeypatch):
    db = mongomock.MongoClient().URLS
    db[PROPERTY_COLLECTION_NAME].insert_one({
        "codigo": "9001", "publicaciones": {"codigo_internacional": "912345678"},
    })
    monkeypatch.setattr(link_extractor, "get_db", lambda: db)

    result = link_extractor.resolver_referencia_propiedad("912345678", use_legacy_lookup=False)

    assert result["status"] == "ambiguous"
    assert result["error_code"] == "NUMERIC_PROPERTY_OR_PHONE"
    assert result["property"] is None
