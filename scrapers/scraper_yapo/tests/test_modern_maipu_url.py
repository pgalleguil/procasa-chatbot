"""Yapo Maipu searches must prove the requested category, commune and UF range."""
from scrapers.scraper_yapo.pilot import _scope_url_is_valid


BASE = "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/region-metropolitana-maipu"
FILTERS = "?q=f_currency.CLF%7Cf_price.3000-6000"


def test_current_maipu_sale_house_search_requires_exact_uf_filters():
    accepted, reason = _scope_url_is_valid(BASE + FILTERS)
    assert accepted is True
    assert reason == "current_yapo_sale_house_maipu_3000_6000_uf"


def test_current_maipu_search_trailing_slash_with_filters_is_accepted():
    assert _scope_url_is_valid(BASE + "/" + FILTERS)[0] is True


def test_current_maipu_search_without_price_filter_is_rejected():
    assert _scope_url_is_valid(BASE)[0] is False


def test_current_search_does_not_accept_other_commune_or_property_type():
    for url in (
        BASE.replace("maipu", "cerrillos") + FILTERS,
        "https://www.yapo.cl/bienes-raices-venta-de-propiedades-apartamentos/region-metropolitana-maipu" + FILTERS,
        "https://example.com/bienes-raices-venta-de-propiedades-casas/region-metropolitana-maipu" + FILTERS,
    ):
        assert _scope_url_is_valid(url)[0] is False
