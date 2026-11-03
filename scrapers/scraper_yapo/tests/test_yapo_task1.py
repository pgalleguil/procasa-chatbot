from __future__ import annotations

from pathlib import Path
import json
import sys
from types import SimpleNamespace

_SCRAPER_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

import pytest

from scrapers.scraper_yapo import config, deepseek_classifier, discovery, downloader, pilot
from scrapers.scraper_yapo.config import AppConfig
from scrapers.scraper_yapo.downloader import DownloadResult
from scrapers.scraper_yapo.proxy_manager import ProxyManager, load_proxies_from_env


def _listing(listing_id: str, *, commune: str = "Maipú", title: str = "Casa amplia") -> str:
    return f'''<div class="d3-ad-tile"><a href="/bienes-raices-venta-de-propiedades-casas/casa/{listing_id}">aviso</a>
    <div class="d3-ad-tile__location">{commune}</div><span class="d3-ad-tile__title">{title}</span>
    <div class="d3-ad-tile__seller">Particular</div></div>'''


def _html(body: str) -> str:
    return "<!doctype html><html><body>" + body + (" contenido" * 35) + "</body></html>"


def _download(url: str, html: str) -> DownloadResult:
    return DownloadResult(url, html, 200, "requests_proxy", Path("unused.html"), "OK", "valid_html")


def test_proxy_environment_variables_normalize_and_deduplicate(monkeypatch):
    monkeypatch.setenv("PROXY_URL", "proxy-a.test:8000")
    monkeypatch.setenv("PROXY_URLS", "http://proxy-b.test:8001;proxy-a.test:8000")
    monkeypatch.setenv("YAPO_PROXY_URLS", "proxy-c.test:8002")
    monkeypatch.setenv("PROXIES", "proxy-d.test:8003")
    monkeypatch.setenv("PROXY_USER", "user")
    monkeypatch.setenv("PROXY_PASS", "pass")
    proxies = load_proxies_from_env()
    assert len(proxies) == 4
    assert all(value.startswith("http://") and "@" in value for value in proxies)
    assert len(ProxyManager(proxies)._proxies) == 4


def test_config_proxy_is_used_and_no_auth_url_is_printed(monkeypatch, capsys):
    monkeypatch.setenv("PROXY_USER", "private-user")
    monkeypatch.setenv("PROXY_PASS", "private-pass")
    manager = ProxyManager.from_config(AppConfig(proxy_url="proxy.test:8123"))
    assert manager.has_proxies()
    choice = manager.next_proxy()
    assert choice and "private-user:private-pass@proxy.test:8123" in choice.url
    assert "private-pass" not in capsys.readouterr().out


def test_no_proxy_fails_before_any_direct_request(monkeypatch, tmp_path):
    called = []

    class Session:
        trust_env = True

        def get(self, *args, **kwargs):
            called.append((args, kwargs))
            raise AssertionError("network request must not run")

    monkeypatch.setattr(downloader.requests, "Session", Session)
    cfg = AppConfig(html_dumps_dir=tmp_path)
    with pytest.raises(RuntimeError, match="YAPO_PROXY_REQUIRED"):
        downloader.download_html("https://www.yapo.cl/test", cfg, None)
    assert called == []


def test_valid_proxy_is_the_only_transport(monkeypatch, tmp_path):
    captured = []
    response = SimpleNamespace(status_code=200, text=_html("contenido válido"), raise_for_status=lambda: None)

    class Session:
        trust_env = True

        def get(self, url, **kwargs):
            captured.append((url, self.trust_env, kwargs.get("proxies")))
            return response

    monkeypatch.setattr(downloader.requests, "Session", Session)
    monkeypatch.setattr(downloader.time, "sleep", lambda _: None)
    manager = ProxyManager(["proxy.test:9000"])
    result = downloader.download_html("https://www.yapo.cl/test", AppConfig(html_dumps_dir=tmp_path), manager)
    assert result.fetch_source == "requests_proxy"
    assert captured == [("https://www.yapo.cl/test", False, {"http": "http://proxy.test:9000", "https": "http://proxy.test:9000"})]


def test_proxy_transport_retries_transient_errors_without_changing_proxy(monkeypatch, tmp_path):
    captured = []
    response = SimpleNamespace(status_code=200, text=_html("contenido válido"), raise_for_status=lambda: None)

    class Session:
        trust_env = True

        def get(self, url, **kwargs):
            captured.append((url, self.trust_env, kwargs.get("proxies")))
            if len(captured) < 3:
                raise TimeoutError("transient transport failure")
            return response

    monkeypatch.setattr(downloader.requests, "Session", Session)
    monkeypatch.setattr(downloader.time, "sleep", lambda _: None)
    manager = ProxyManager(["proxy.test:9000"])
    result = downloader.download_html("https://www.yapo.cl/test", AppConfig(html_dumps_dir=tmp_path), manager)

    expected_proxy = {"http": "http://proxy.test:9000", "https": "http://proxy.test:9000"}
    assert result.validation_status == "OK"
    assert len(captured) == 3
    assert all(item == ("https://www.yapo.cl/test", False, expected_proxy) for item in captured)


def test_blocked_response_is_preserved_in_batch_sidecar(monkeypatch, tmp_path):
    blocked_html = _html("Access denied captcha challenge")
    response = SimpleNamespace(status_code=200, text=blocked_html, raise_for_status=lambda: None)

    class Session:
        trust_env = True

        def get(self, url, **kwargs):
            return response

    monkeypatch.setattr(downloader.requests, "Session", Session)
    monkeypatch.setattr(downloader.time, "sleep", lambda _: None)
    config_value = AppConfig(html_dumps_dir=tmp_path)
    result = downloader.download_html(
        "https://www.yapo.cl/searchresult/test",
        config_value,
        ProxyManager(["proxy.test:9000"]),
        batch_id="blocked-evidence",
    )
    assert result.validation_status == "BLOCKED"
    assert not result.html_path.exists()
    sidecar = result.html_path.with_suffix(".blocked.html")
    assert sidecar.exists()
    assert sidecar.read_text(encoding="utf-8") == blocked_html


def test_generic_blocked_and_forbidden_words_do_not_reject_valid_listing_html():
    html = _html(
        '<article class="d3-ad-tile"><h1>Casa amplia</h1>'
        '<p>El texto histórico menciona blocked y forbidden como palabras comunes.</p>'
        "</article>"
    )
    result = downloader.validate_html(html)
    assert result == {"status": "OK", "reason": "valid_html"}


@pytest.mark.parametrize(
    "body",
    [
        "Access denied captcha challenge",
        "Verify you are human",
        "Your request has been blocked",
    ],
)
def test_clear_challenge_messages_are_blocked(body):
    result = downloader.validate_html(_html(body))
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "strong_block_signal"


def test_http_denial_saves_private_body_without_proxy_credentials(monkeypatch, tmp_path):
    response = SimpleNamespace(
        status_code=403,
        text="Access denied by Yapo edge",
        raise_for_status=lambda: None,
    )

    class Session:
        trust_env = True

        def get(self, url, **kwargs):
            return response

    monkeypatch.setattr(downloader.requests, "Session", Session)
    monkeypatch.setattr(downloader.time, "sleep", lambda _: None)
    config_value = AppConfig(html_dumps_dir=tmp_path)
    config_value.proxy_url = "proxy.test:9000"
    monkeypatch.setenv("PROXY_USER", "private-user")
    monkeypatch.setenv("PROXY_PASS", "private-pass")

    with pytest.raises(RuntimeError, match=r"YAPO_BLOCKED: HTTP 403") as error:
        downloader.download_html(
            "https://www.yapo.cl/searchresult/test",
            config_value,
            ProxyManager.from_config(config_value),
            batch_id="http-denial",
        )

    message = str(error.value)
    assert "private-user" not in message
    assert "private-pass" not in message
    diagnostic = next((tmp_path / "http-denial").glob("*.http_403.html"))
    assert diagnostic.read_text(encoding="utf-8") == response.text


def test_http_server_error_saves_private_body(monkeypatch, tmp_path):
    response = SimpleNamespace(
        status_code=500,
        text="Yapo temporary error",
        raise_for_status=lambda: None,
    )

    class Session:
        trust_env = True

        def get(self, url, **kwargs):
            return response

    monkeypatch.setattr(downloader.requests, "Session", Session)
    monkeypatch.setattr(downloader.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match=r"YAPO_HTTP_ERROR: HTTP 500"):
        downloader.download_html(
            "https://www.yapo.cl/searchresult/test",
            AppConfig(html_dumps_dir=tmp_path),
            ProxyManager(["proxy.test:9000"]),
            batch_id="http-error",
        )
    diagnostic = next((tmp_path / "http-error").glob("*.http_500.html"))
    assert diagnostic.read_text(encoding="utf-8") == response.text


def test_real_windows_page1_html_has_ads_and_js_pagination_without_false_block():
    page_path = (
        Path(__file__).resolve().parents[1]
        / "html_dumps"
        / "owner_hunt_20261102_191911_3501"
        / "838f80574bf34b7f5178d15955c2f5cd.blocked.html"
    )
    if not page_path.is_file():
        pytest.skip("HTML real de Windows no disponible fuera de este entorno")
    page_url = "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/region-metropolitana-maipu"
    html = page_path.read_text(encoding="utf-8", errors="replace")
    assert downloader.validate_html(html) == {"status": "OK", "reason": "valid_html"}
    records, _, duplicates = discovery._extract_listing_urls_detailed(html, page_url)
    assert len(records) == 20
    assert duplicates == 0
    assert records[0]["url"].endswith("/32747511")
    soup = discovery.BeautifulSoup(html, "html.parser")
    search_next_buttons = soup.select(discovery._YAPO_NEXT_BUTTON_SELECTOR)
    assert len(search_next_buttons) == 1
    assert search_next_buttons[0].get("aria-label", "").lower().endswith("siguiente")
    assert any(button.get_text(" ", strip=True) == "Next slide" for button in soup.find_all("button"))
    assert discovery._has_client_side_pagination(html)
    assert discovery._pagination_state(html, page_url) == (
        None,
        False,
        "pagination_control_without_explicit_url",
    )


def test_gallery_next_slide_is_not_search_pagination():
    html = _html('<div class="carousel"><button aria-label="Next slide">Next slide</button></div>')
    assert not discovery._has_client_side_pagination(html)
    assert discovery._pagination_state(html, "https://www.yapo.cl/searchresult/test") == (
        None,
        False,
        "pagination_metadata_missing",
    )


def test_real_windows_page1_advances_with_observed_browser_pagination(monkeypatch, tmp_path):
    page_path = (
        Path(__file__).resolve().parents[1]
        / "html_dumps"
        / "owner_hunt_20261102_191911_3501"
        / "838f80574bf34b7f5178d15955c2f5cd.blocked.html"
    )
    if not page_path.is_file():
        pytest.skip("HTML real de Windows no disponible fuera de este entorno")
    first_url = "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/region-metropolitana-maipu"
    second_url = "https://www.yapo.cl/searchresult/test?observed=2"
    first_html = page_path.read_text(encoding="utf-8", errors="replace")
    second_html = _html(
        _listing("12345678")
        + '<nav aria-label="Paginaci\ufffdn"><button aria-current="page">2</button>'
        + '<span data-total-pages="2"></span>'
        + '<button disabled aria-disabled="true" aria-label="P\ufffdgina siguiente">Siguiente</button></nav>'
    )
    request_evidence = [{"method": "GET", "url": "https://www.yapo.cl/searchresult/test?observed=2"}]

    class FakeBrowserPagination:
        def __init__(self, page_url, page_html, config, proxy_manager):
            assert page_url == first_url
            assert page_html == first_html
            self.closed = False

        def advance(self, expected_page):
            assert expected_page == 2
            return {
                "terminal": False,
                "evidence": "client_side_button_click",
                "url": second_url,
                "html": second_html,
                "request_urls": request_evidence,
                "page_number": 2,
            }

        def close(self):
            self.closed = True

    browser_instances = []

    def make_browser(*args, **kwargs):
        browser = FakeBrowserPagination(*args, **kwargs)
        browser_instances.append(browser)
        return browser

    monkeypatch.setattr(discovery, "_YapoClientPagination", make_browser)
    monkeypatch.setattr(discovery.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    monkeypatch.setattr(discovery, "download_html", lambda url, *args, **kwargs: _download(url, first_html))
    config_value = AppConfig(html_dumps_dir=tmp_path)
    report = {}
    rows = discovery.discover_listing_urls(
        [first_url],
        report=report,
        config=config_value,
        proxy_manager=ProxyManager(["proxy.test:9000"]),
        batch_id="real-page1-browser-transition",
    )

    assert len(rows) == 21
    assert {row["page_number"] for row in rows} == {1, 2}
    assert report["pagination_complete"] is True
    assert report["pagination_cause"] == "explicit_total_pages_reached"
    assert report["pages"][0]["pagination_evidence"] == "client_side_button_click"
    assert report["pages"][0]["pagination_request_urls"] == request_evidence
    assert browser_instances[0].closed is True


def test_discovery_follows_html_pagination_until_verified_end(monkeypatch):
    pages = {
        "https://www.yapo.cl/searchresult/test": _html(_listing("12345678") + '<a rel="next" href="?o=2">Siguiente</a>'),
        "https://www.yapo.cl/searchresult/test?o=2": _html(_listing("12345679") + '<nav class="pagination"><span class="active">2</span><span data-total-pages="2"></span></nav>'),
    }
    monkeypatch.setattr(discovery.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    monkeypatch.setattr(discovery, "download_html", lambda url, *args, **kwargs: _download(url, pages[url]))
    report = {}
    rows = discovery.discover_listing_urls(["https://www.yapo.cl/searchresult/test"], report=report)
    assert [row["page_number"] for row in rows] == [1, 2]
    assert report["pagination_complete"] is True
    assert len(report["pages"]) == 2


def test_pagination_follows_explicit_data_url_control(monkeypatch):
    first = "https://www.yapo.cl/searchresult/test"
    second = "https://www.yapo.cl/searchresult/test?o=2"
    pages = {
        first: _html(_listing("12345678") + '<nav aria-label="Paginación"><button data-next-url="?o=2">Siguiente</button></nav>'),
        second: _html(_listing("12345679") + '<nav aria-label="Paginación"><span aria-current="page">2</span><span data-total-pages="2"></span></nav>'),
    }
    monkeypatch.setattr(discovery.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    monkeypatch.setattr(discovery, "download_html", lambda url, *args, **kwargs: _download(url, pages[url]))
    report = {}
    rows = discovery.discover_listing_urls([first], report=report)
    assert [row["page_number"] for row in rows] == [1, 2]
    assert report["pages"][0]["pagination_evidence"] == "next_control"
    assert report["pagination_complete"] is True


def test_pagination_follows_explicit_embedded_json_url():
    first = "https://www.yapo.cl/searchresult/test"
    html = _html(_listing("12345678") + '''<script type="application/json">
    {"pagination":{"currentPage":1,"totalPages":2,"nextUrl":"?o=2"}}
    </script>''')
    next_url, terminal, evidence = discovery._pagination_state(html, first)
    assert next_url == "https://www.yapo.cl/searchresult/test?o=2"
    assert terminal is False
    assert evidence == "embedded_next_url"


def test_numeric_pagination_without_explicit_next_url_stays_incomplete():
    html = _html(_listing("12345678") + '<nav class="pagination"><span aria-current="page">1</span><span data-total-pages="2"></span></nav>')
    next_url, terminal, evidence = discovery._pagination_state(html, "https://www.yapo.cl/searchresult/test")
    assert next_url is None
    assert terminal is False
    assert evidence == "pagination_end_not_explicitly_verified"


def test_duplicate_only_intermediate_page_does_not_stop(monkeypatch):
    first = "https://www.yapo.cl/searchresult/test"
    second = "https://www.yapo.cl/searchresult/test?o=2"
    third = "https://www.yapo.cl/searchresult/test?o=3"
    pages = {
        first: _html(_listing("12345678") + '<a rel="next" href="?o=2">Siguiente</a>'),
        second: _html(_listing("12345678") + '<a rel="next" href="?o=3">Siguiente</a>'),
        third: _html(_listing("12345679") + '<nav class="pagination"><span class="active">3</span><span data-total-pages="3"></span></nav>'),
    }
    monkeypatch.setattr(discovery.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    monkeypatch.setattr(discovery, "download_html", lambda url, *args, **kwargs: _download(url, pages[url]))
    report = {}
    rows = discovery.discover_listing_urls([first], report=report)
    assert [row["listing_id"] if "listing_id" in row else row["url"].rsplit("/", 1)[-1] for row in rows] == ["12345678", "12345679"]
    assert report["pages"][1]["duplicate_count"] == 1
    assert report["pagination_complete"] is True


def test_pagination_cycle_is_reported_incomplete(monkeypatch):
    first = "https://www.yapo.cl/searchresult/test"
    second = "https://www.yapo.cl/searchresult/test?o=2"
    pages = {
        first: _html(_listing("12345678") + '<a rel="next" href="?o=2">Siguiente</a>'),
        second: _html(_listing("12345679") + '<a rel="next" href="/searchresult/test">Siguiente</a>'),
    }
    monkeypatch.setattr(discovery.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    monkeypatch.setattr(discovery, "download_html", lambda url, *args, **kwargs: _download(url, pages[url]))
    report = {}
    discovery.discover_listing_urls([first], report=report)
    assert report["pagination_complete"] is False
    assert report["pagination_cause"] == "pagination_url_cycle"


def test_largest_visible_page_number_does_not_prove_end(monkeypatch):
    url = "https://www.yapo.cl/searchresult/test"
    html = _html(_listing("12345678") + '<nav class="pagination"><span class="active">5</span><a href="?o=4">4</a><a href="?o=8">8</a></nav>')
    monkeypatch.setattr(discovery.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    monkeypatch.setattr(discovery, "download_html", lambda u, *args, **kwargs: _download(u, html))
    report = {}
    discovery.discover_listing_urls([url], report=report)
    assert report["pagination_complete"] is False
    assert report["pagination_cause"] == "pagination_end_not_explicitly_verified"


def test_external_pagination_link_is_rejected(monkeypatch):
    url = "https://www.yapo.cl/searchresult/test"
    html = _html(_listing("12345678") + '<a rel="next" href="https://outside.example/page">Siguiente</a>')
    monkeypatch.setattr(discovery.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    monkeypatch.setattr(discovery, "download_html", lambda u, *args, **kwargs: _download(u, html))
    report = {}
    discovery.discover_listing_urls([url], report=report)
    assert report["pagination_complete"] is False
    assert report["pagination_cause"] == "unauthorized_pagination_domain"
    assert len(report["pages"]) == 1


def test_discovery_counters_separate_attempts_success_and_cache(monkeypatch, tmp_path):
    url = "https://www.yapo.cl/searchresult/test"
    monkeypatch.setattr(discovery.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    monkeypatch.setattr(discovery, "download_html", lambda u, *args, **kwargs: _download(u, _html('<nav class="pagination"><span class="active">1</span><span data-total-pages="1"></span></nav>')))
    report = {}
    discovery.discover_listing_urls([url], config=AppConfig(html_dumps_dir=tmp_path), proxy_manager=ProxyManager(["proxy.test:9000"]), report=report)
    assert report["request_attempts"] == 1
    assert report["successful_downloads"] == 1
    assert report["cache_responses"] == 0


def test_discovery_counts_cache_separately_from_network(monkeypatch, tmp_path):
    url = "https://www.yapo.cl/searchresult/test"
    cfg = AppConfig(html_dumps_dir=tmp_path)
    batch_id = "cache-test"
    from scrapers.scraper_yapo.downloader import html_path_for_url
    cache_path = html_path_for_url(url, cfg, batch_id=batch_id)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(_html('<nav class="pagination"><span class="active">1</span><span data-total-pages="1"></span></nav>'), encoding="utf-8")
    monkeypatch.setattr(discovery.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    monkeypatch.setattr(discovery, "download_html", lambda u, *args, **kwargs: DownloadResult(u, cache_path.read_text(), 200, "cache", cache_path, "OK", "valid_html"))
    report = {}
    discovery.discover_listing_urls([url], config=cfg, proxy_manager=ProxyManager(["proxy.test:9000"]), batch_id=batch_id, report=report)
    assert report["request_attempts"] == 0
    assert report["successful_downloads"] == 0
    assert report["cache_responses"] == 1


def test_block_or_captcha_stops_without_followup(monkeypatch):
    first = "https://www.yapo.cl/searchresult/test"
    blocked = _html("Access denied captcha challenge") + (" blocked" * 35)
    calls = []
    monkeypatch.setattr(discovery.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    monkeypatch.setattr(discovery, "download_html", lambda url, *args, **kwargs: calls.append(url) or _download(url, blocked))
    report = {}
    discovery.discover_listing_urls([first], report=report)
    assert len(calls) == 1
    assert report["pagination_complete"] is False
    assert report["pages"][0]["status"] == "blocked"


def test_pilot_deduplicates_by_id_and_normalized_url_before_details(monkeypatch, tmp_path):
    start_url = "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana-maipu&q=withcat.bienes-raices-venta-de-propiedades-casas%7Cf_currency.CLF%7Cf_price.3000-6000"
    rows = [
        {"url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678?utm_source=x", "listing_id": "12345678", "page_number": 1, "discovery_comuna": "Maipú"},
        {"url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/otra-casa/12345678", "listing_id": "12345678", "page_number": 2, "discovery_comuna": "Maipú"},
        {"url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678?gclid=x", "listing_id": "99999999", "page_number": 2, "discovery_comuna": "Maipú"},
    ]
    used_configs = []
    used_managers = []
    def fake_discover(*args, **kwargs):
        active_url = args[0][0]
        used_configs.append(kwargs["config"])
        used_managers.append(kwargs["proxy_manager"])
        page_html = f'<html><head><link rel="canonical" href="{active_url}"></head><body>search results</body></html>'
        filter_validation = kwargs["page_filter_validator"](active_url, page_html)
        kwargs["report"].update({"pages": [{"url": active_url, "status": "complete", "listing_count": 3, "duplicate_count": 0, "filter_validation": filter_validation}], "pagination_complete": True, "pagination_cause": "explicit_total_pages_reached", "errors": [], "proxy_requests": 1, "request_attempts": 1, "successful_downloads": 1, "cache_responses": 0})
        return [{**row, "source_search_url": active_url} for row in rows]
    monkeypatch.setattr(pilot, "discover_listing_urls", fake_discover)
    monkeypatch.setattr(pilot.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    class Store:
        writes = 0
        def __init__(self, _): pass
        def existing_listing_ids(self, ids): return set()
        def existing_listing_urls(self, urls): return set()
        def upsert_listing(self, *_): raise AssertionError("pilot must never write MongoDB")
    monkeypatch.setattr(pilot, "MongoStore", Store)
    details = []
    detail_configs = []
    def fake_download(url, cfg_arg, manager_arg, **kwargs):
        details.append(url)
        detail_configs.append((cfg_arg, manager_arg))
        return _download(url, _html('<h1>Casa completa</h1><div class="d3-property-about__text">Texto completo</div><div class="price">4.000 UF</div>'))
    monkeypatch.setattr(pilot, "download_html", fake_download)
    monkeypatch.setattr(pilot, "classify_with_rules", lambda _: {"state": "INCIERTO", "evidence": []})
    cfg = AppConfig(mongo_uri="mongodb://fake", reports_dir=tmp_path, html_dumps_dir=tmp_path / "html")
    result = pilot.run_pilot(start_url, cfg, batch_id="test")
    assert len(details) == 1
    assert result["batch_duplicates"] == 2
    assert result["mongo_writes"] == 0
    assert used_configs[0] is cfg and detail_configs[0][0] is cfg
    assert used_managers[0] is detail_configs[0][1]


def test_mongo_unavailable_is_not_treated_as_no_duplicates(monkeypatch, tmp_path):
    start_url = "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana-maipu&q=withcat.bienes-raices-venta-de-propiedades-casas%7Cf_currency.CLF%7Cf_price.3000-6000"
    def fake_discover(*args, **kwargs):
        kwargs["report"].update({"pages": [{"url": start_url, "status": "complete", "listing_count": 1, "duplicate_count": 0}], "pagination_complete": True, "pagination_cause": "explicit_total_pages_reached", "errors": [], "proxy_requests": 1, "request_attempts": 1, "successful_downloads": 1, "cache_responses": 0})
        return [{"url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678", "listing_id": "12345678", "discovery_comuna": "Maipú"}]
    monkeypatch.setattr(pilot, "discover_listing_urls", fake_discover)
    monkeypatch.setattr(pilot.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    monkeypatch.setattr(pilot, "MongoStore", lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    detail_calls = []
    monkeypatch.setattr(pilot, "download_html", lambda *args, **kwargs: detail_calls.append(args[0]))
    cfg = AppConfig(mongo_uri="mongodb://fake", reports_dir=tmp_path, html_dumps_dir=tmp_path / "html")
    result = pilot.run_pilot(start_url, cfg, batch_id="test")
    assert result["mongo_dedup_verified"] is False
    assert result["new_ads"] is None
    assert detail_calls == []


def test_pilot_skips_detail_if_search_was_interrupted(monkeypatch, tmp_path):
    start_url = "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana-maipu&q=withcat.bienes-raices-venta-de-propiedades-casas%7Cf_currency.CLF%7Cf_price.3000-6000"
    def fake_discover(*args, **kwargs):
        kwargs["report"].update({"pages": [{"listing_count": 1, "duplicate_count": 0, "status": "error"}], "pagination_complete": False, "pagination_cause": "blocked_or_download_error", "errors": ["blocked"], "proxy_requests": 0, "request_attempts": 1, "successful_downloads": 0, "cache_responses": 0})
        return [{"url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678", "listing_id": "12345678", "discovery_comuna": "Maipú"}]
    monkeypatch.setattr(pilot, "discover_listing_urls", fake_discover)
    monkeypatch.setattr(pilot.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    detail_calls = []
    monkeypatch.setattr(pilot, "download_html", lambda *a, **k: detail_calls.append(a))
    cfg = AppConfig(mongo_uri="mongodb://fake", reports_dir=tmp_path, html_dumps_dir=tmp_path / "html")
    result = pilot.run_pilot(start_url, cfg, batch_id="interrupted")
    assert result["pagination_complete"] is False
    assert detail_calls == []


@pytest.mark.parametrize("listing_html", ["invalid", "removed"])
def test_pilot_rejects_invalid_or_removed_html(monkeypatch, tmp_path, listing_html):
    start_url = "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana-maipu&q=withcat.bienes-raices-venta-de-propiedades-casas%7Cf_currency.CLF%7Cf_price.3000-6000"
    def fake_discover(*args, **kwargs):
        active_url = args[0][0]
        page_html = f'<html><head><link rel="canonical" href="{active_url}"></head><body>search results</body></html>'
        filter_validation = kwargs["page_filter_validator"](active_url, page_html)
        kwargs["report"].update({"pages": [{"url": active_url, "status": "complete", "listing_count": 1, "duplicate_count": 0, "filter_validation": filter_validation}], "pagination_complete": True, "pagination_cause": "explicit_total_pages_reached", "errors": [], "proxy_requests": 1, "request_attempts": 1, "successful_downloads": 1, "cache_responses": 0})
        return [{"url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678", "listing_id": "12345678", "discovery_comuna": "Maipú", "source_search_url": active_url}]
    class Store:
        def __init__(self, _): pass
        def existing_listing_ids(self, _): return set()
        def existing_listing_urls(self, _): return set()
    monkeypatch.setattr(pilot, "discover_listing_urls", fake_discover)
    monkeypatch.setattr(pilot.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    monkeypatch.setattr(pilot, "MongoStore", Store)
    invalid_html = "short" if listing_html == "invalid" else "<!doctype html><html><body>" + ("contenido " * 40) + "Anuncio borrado por el anunciante</body></html>"
    monkeypatch.setattr(pilot, "download_html", lambda url, *a, **k: _download(url, invalid_html))
    cfg = AppConfig(mongo_uri="mongodb://fake", reports_dir=tmp_path, html_dumps_dir=tmp_path / "html")
    result = pilot.run_pilot(start_url, cfg, batch_id="removed")
    assert result["invalid_or_removed_ads"] == 1
    assert result["filter_matches"] == 0
    records = json.loads(Path(result["records_json"]).read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0]["filter_status"] == "PENDIENTE_VALIDACION"


def test_mongo_url_dedup_is_exact_and_canonical_field_is_checked():
    class Collection:
        def __init__(self): self.query = None
        def find(self, query, projection):
            self.query = query
            return [
                {"url": "https://www.yapo.cl/casa/slug/12345678?utm_source=old", "canonical_url": ""},
                {"url": "", "canonical_url": "https://www.yapo.cl/casa/other-slug/98765432"},
            ]
    class Store(pilot.MongoStore):
        def __init__(self): self.coll = Collection()
        def collection(self): return self.coll
    store = Store()
    requested = "https://www.yapo.cl/casa/slug/12345678"
    matches = store.existing_listing_urls([requested])
    assert matches == {requested}
    assert {"url", "source_url", "canonical_url"} == {clause.keys().__iter__().__next__() for clause in store.coll.query["$or"]}


def test_deepseek_is_off_even_when_environment_enables_it(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_ENABLED", "true")
    assert AppConfig().deepseek_enabled is False
    with pytest.raises(RuntimeError, match="DeepSeek está desactivado"):
        deepseek_classifier.classify_with_deepseek({}, {}, AppConfig())


def test_yapo_package_config_does_not_shadow_project_config():
    import config as project_config
    assert config is not project_config
    assert config.AppConfig.__module__ == "scrapers.scraper_yapo.config"


def test_extractor_preserves_complete_title_and_description():
    long_description = "Descripción completa sin resumen. " * 60
    html = _html(f'<h1>Título íntegro de la propiedad</h1><div class="d3-property-about__text">{long_description}</div>')
    fields = pilot.extract_listing_fields(html, source_url="https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678")
    assert fields["title"] == "Título íntegro de la propiedad"
    assert fields["description"] == long_description.strip()
    assert fields["descripcion"] == fields["description"]


def test_meta_description_is_not_used_as_full_description():
    fields = pilot.extract_listing_fields(_html('<h1>Casa en Maipú</h1><meta name="description" content="Resumen breve">'), source_url="https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678")
    assert fields["description"] == ""
    assert fields["description_summary"] == "Resumen breve"
    assert fields["descripcion_is_truncated"] is True


def test_current_yapo_detail_template_uses_full_description_and_structured_listing_data():
    description = ("Texto completo del aviso... con sus condiciones y características. " * 14).strip()
    payload = {
        "@context": "https://schema.org", "@type": "Product", "name": "Casa en Maipú",
        "description": description + " Texto final conservado desde JSON-LD.",
        "offers": {
            "@type": "Offer", "price": "165000000", "priceCurrency": "CLP",
            "seller": {"@type": "Organization", "name": "Fuenzalida Propiedades Maipú Plaza"},
            "availableAtOrFrom": {"@type": "Place", "address": {
                "@type": "PostalAddress", "addressLocality": "Maipú",
            }},
        },
    }
    html = _html(
        '<main><h1>Casa en Maipú</h1>'
        '<section><h2>Descripción</h2><div><p>' + description + '</p></div></section>'
        '<div><h3>Publicado por</h3><div><h4>Fuenzalida Propiedades Maipú Plaza</h4>'
        '<img alt="Logo Fuenzalida Propiedades"></div></div></main>'
        '<script type="application/ld+json">' + json.dumps(payload, ensure_ascii=False) + '</script>'
        '<meta property="og:description" content="Resumen breve">'
    )
    fields = pilot.extract_listing_fields(
        html, source_url="https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678"
    )
    assert fields["description"] == description + " Texto final conservado desde JSON-LD."
    assert fields["descripcion_is_truncated"] is False
    assert fields["price"] == "165000000 CLP"
    assert fields["price_currency"] == "CLP"
    assert fields["publicador_visible"] == "Fuenzalida Propiedades Maipú Plaza"
    assert fields["comuna"] == "Maipú"
    assert fields["comuna_evidence"]["status"] == "confirmed_detail"


@pytest.mark.parametrize("raw,expected", [("4.000 UF", 4000), ("4.250 CLF", 4250), ("165000000 CLP", None)])
def test_pilot_price_parser_accepts_yapo_uf_currency_code(raw, expected):
    assert pilot._price_in_uf(raw) == expected


def test_general_pilot_accepts_unpriced_search_and_parses_any_supported_price_filter():
    base = "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/region-metropolitana-maipu"
    assert pilot._scope_url_is_valid(base + "?q=f_currency.CLF|f_price.3000-6000")[0] is True
    assert pilot._scope_url_is_valid(base)[0] is True


def test_local_classification_uses_existing_possible_owner_state_only_with_clear_direct_signal():
    from scrapers.scraper_yapo.classifier_rules import classify_yapo_listing

    possible_owner = classify_yapo_listing({"title": "Dueño vende casa", "description": "Trato directo"})
    assert possible_owner["state"] == "DUEÑO_PROBABLE"
    assert possible_owner["owner_classification_confirmed"] is False

    uncertain = classify_yapo_listing({"title": "Casa sin comisión", "description": "Se acepta corredor"})
    assert uncertain["state"] == "INCIERTO"

    company_conflict = classify_yapo_listing({
        "title": "Dueño vende", "description": "", "publicador_visible": "Casa Example Propiedades",
        "publisher_field_provenance": {"publicador_visible": {"status": "confirmed", "source": "test", "relationship": "publisher"}},
    })
    assert company_conflict["state"] == "INCIERTO"


@pytest.mark.parametrize("publisher", ["Kutt Property", "Dataprop Propiedades", "Focolare Propiedades"])
def test_pilot_observed_verified_broker_brands_are_filtered_first(publisher):
    from scrapers.scraper_yapo.classifier_rules import classify_yapo_listing

    result = classify_yapo_listing({
        "publicador_visible": publisher, "contact_name": publisher,
        "publisher_field_provenance": {
            "publicador_visible": {"status": "confirmed", "source": "test", "relationship": "publisher"},
            "contact_name": {"status": "confirmed", "source": "test", "relationship": "publisher"},
        },
    })
    assert result["state"] == "CORREDOR_SEGURO"
