from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from scrapers.scraper_yapo.search_config import (
    SearchConfig,
    SearchConfigurationError,
    inspect_search_url,
    verify_search_page,
)


def _config(**overrides):
    values = {
        "region": "Región Metropolitana",
        "communes": ("Maipú",),
        "operation": "venta",
        "property_type": "casa",
        "price_min": 3000,
        "price_max": 6000,
        "price_currency": "UF",
    }
    values.update(overrides)
    return SearchConfig(**values)


def test_a_maipu_sale_house_reproduces_pilot_route_and_uf_range():
    search = _config()
    url = search.start_urls()[0][0]
    parts = urlparse(url)
    assert parts.path == "/bienes-raices-venta-de-propiedades-casas/region-metropolitana-maipu"
    assert set(inspect_search_url(url)["query_tokens"]) == {
        "f_currency.CLF", "f_price.3000-6000",
    }
    assert verify_search_page(url, f'<link rel="canonical" href="{url}">', search, "maipu")["verified"] is True
    wrong = url.replace("maipu", "pudahuel")
    assert verify_search_page(url, f'<link rel="canonical" href="{wrong}">', search, "maipu")["verified"] is False


def test_b_pudahuel_sale_apartment_builds_distinct_configured_scope():
    search = _config(
        communes=("Pudahuel",), property_type="departamento",
        price_min=2000, price_max=4000, bedrooms_min=3,
    )
    url = search.start_urls()[0][0]
    assert urlparse(url).path == "/bienes-raices-venta-de-propiedades-apartamentos/region-metropolitana-pudahuel"
    assert "f_price.2000-4000" in inspect_search_url(url)["query_tokens"]
    assert "f_rooms.3-" in inspect_search_url(url)["query_tokens"]


def test_c_la_florida_rental_apartment_uses_clp_filter():
    search = _config(
        communes=("La Florida",), operation="arriendo", property_type="departamento",
        price_min=500000, price_max=1200000, price_currency="CLP",
    )
    url = search.start_urls()[0][0]
    assert urlparse(url).path == "/bienes-raices-alquiler-apartamentos/region-metropolitana-la-florida"
    assert set(inspect_search_url(url)["query_tokens"]) == {
        "f_currency.CLP", "f_price.500000-1200000",
    }


def test_d_multiple_communes_create_independent_exact_search_urls():
    urls = _config(communes=("Maipú", "Pudahuel")).start_urls()
    assert [commune for _, commune in urls] == ["maipu", "pudahuel"]
    assert [urlparse(url).path.rsplit("-", 1)[-1] for url, _ in urls] == ["maipu", "pudahuel"]


def test_e_no_price_filter_is_explicit_and_does_not_add_price_tokens():
    search = _config(price_min=None, price_max=None, price_currency=None)
    assert inspect_search_url(search.start_urls()[0][0])["query_tokens"] == []


def test_e_price_range_without_a_minimum_fails_closed_until_yapo_verifies_it():
    with pytest.raises(SearchConfigurationError, match="solo con máximo no está verificado"):
        _config(price_min=None, price_max=6000)


@pytest.mark.parametrize("overrides, message", [
    ({"operation": "arriendo", "property_type": "edificio"}, "no tiene una categoría"),
    ({"price_min": None, "price_max": 6000}, "solo con máximo no está verificado"),
    ({"bathrooms_min": 2}, "baños.*no.*verificada"),
    ({"surface_min_m2": 80}, "superficie.*no.*verificada"),
    ({"property_type": "bodega"}, "Tipo de propiedad no verificado"),
])
def test_f_unverified_or_incompatible_filters_fail_closed(overrides, message):
    with pytest.raises(SearchConfigurationError, match=message):
        _config(**overrides)


def test_minimum_only_price_range_uses_the_observed_open_upper_bound_syntax():
    search = _config(price_min=3000, price_max=None)
    assert "f_price.3000-" in inspect_search_url(search.start_urls()[0][0])["query_tokens"]
