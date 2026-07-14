from discovery import _extract_listing_urls, _norm_commune
from downloader import validate_html


HTML = """<!doctype html><html><body>
<div class="d3-ad-tile"><a href="#">fav</a>
<a href="/bienes-raices-venta-de-propiedades-casas/casa-en-talca/12345678">detail</a>
<div class="d3-ad-tile__location">Talca</div><span class="d3-ad-tile__title">Casa</span>
<div class="d3-ad-tile__seller">Particular</div></div></body></html>"""


def test_tile_parser_uses_detail_link_and_location():
    rows, _ = _extract_listing_urls(HTML, "https://www.yapo.cl/searchresult/x")
    assert rows[0]["discovery_comuna"] == "Talca"
    assert rows[0]["url"].endswith("/12345678")
    assert _norm_commune("Río Claro") == "rio-claro"


def test_recaptcha_library_on_normal_page_is_not_a_block():
    html = "<!doctype html><html><body>" + ("contenido " * 30) + "getrecaptchakey recaptcha/api.js</body></html>"
    assert validate_html(html)["status"] == "OK"
