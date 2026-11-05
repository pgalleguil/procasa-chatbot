from scrapers.scraper_yapo.discovery import _same_yapo_page_url


def test_same_page_identity_accepts_equivalent_encoded_search_delimiter():
    encoded = (
        "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/"
        "region-metropolitana-maipu?q=f_currency.CLF%7Cf_price.3000-6000"
    )
    decoded = (
        "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/"
        "region-metropolitana-maipu?q=f_currency.CLF|f_price.3000-6000"
    )

    assert _same_yapo_page_url(encoded, decoded)


def test_same_page_identity_rejects_another_page_or_filter():
    first = "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/region-metropolitana-maipu?q=f_price.3000-6000"
    assert not _same_yapo_page_url(first, first + "-7000")
    assert not _same_yapo_page_url(first, first.replace("maipu", "santiago"))
