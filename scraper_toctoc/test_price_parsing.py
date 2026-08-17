from enrich import _enrich_property_fields, parse_toctoc_price


def test_price_components_clp_and_uf_are_independent():
    result = parse_toctoc_price("$ 300.000 / UF 7,34", 40842.07)
    assert result == {"precio_clp": 300000.0, "precio_uf": 7.34}
    assert result["precio_uf"] != 3000007.34


def test_price_components_common_formats():
    assert parse_toctoc_price("$ 450.000 / UF 11,01") == {"precio_clp": 450000.0, "precio_uf": 11.01}
    assert parse_toctoc_price("UF 5.200", 40842.07)["precio_uf"] == 5200.0
    assert parse_toctoc_price("UF 5.200,50", 40842.07)["precio_uf"] == 5200.5
    assert parse_toctoc_price("$ 120.000.000") == {"precio_clp": 120000000.0, "precio_uf": None}
    assert parse_toctoc_price("$300000") == {"precio_clp": 300000.0, "precio_uf": None}


def test_enrichment_uses_precio_raw_when_price_is_missing():
    doc = {"precio_raw": "$ 300.000 / UF 7,34"}
    result = _enrich_property_fields(doc, "https://www.toctoc.com/arriendo/departamento/1", 40842.07, "2026-08-17")
    assert result["precio_clp"] == 300000.0
    assert result["precio_uf"] == 7.34
