"""The live Yapo Maipu listing search uses a category/location path, not /searchresult/."""
from scrapers.scraper_yapo.pilot import _scope_url_is_valid


def test_current_maipu_sale_house_search_is_accepted():
    url = "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/region-metropolitana-maipu"
    accepted, reason = _scope_url_is_valid(url)
    assert accepted is True
    assert reason == "current_yapo_sale_house_maipu_path"


def test_current_maipu_search_trailing_slash_is_accepted():
    accepted, _ = _scope_url_is_valid(
        "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/region-metropolitana-maipu/"
    )
    assert accepted is True


def test_current_search_does_not_accept_other_commune_or_property_type():
    for url in (
        "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/region-metropolitana-cerrillos",
        "https://www.yapo.cl/bienes-raices-venta-de-propiedades-apartamentos/region-metropolitana-maipu",
        "https://example.com/bienes-raices-venta-de-propiedades-casas/region-metropolitana-maipu",
    ):
        assert _scope_url_is_valid(url)[0] is False


def test_legacy_explicit_search_url_remains_supported():
    url = (
        "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades"
        "?operation=venta&q=bienes-raices-venta-de-propiedades-casas&regionslug=maipu"
    )
    assert _scope_url_is_valid(url)[0] is True
