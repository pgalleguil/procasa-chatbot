from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

_SCRAPER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRAPER_DIR))
sys.path.insert(1, str(_SCRAPER_DIR.parent))

import pytest

import config
import deepseek_classifier
import discovery
import downloader
import pilot
from config import AppConfig
from downloader import DownloadResult
from proxy_manager import ProxyManager, load_proxies_from_env


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


def test_discovery_follows_html_pagination_until_verified_end(monkeypatch):
    pages = {
        "https://www.yapo.cl/searchresult/test": _html(_listing("12345678") + '<a rel="next" href="?o=2">Siguiente</a>'),
        "https://www.yapo.cl/searchresult/test?o=2": _html(_listing("12345679") + '<nav class="pagination"><span class="active">2</span><a href="?o=1">1</a></nav>'),
    }
    monkeypatch.setattr(discovery.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    monkeypatch.setattr(discovery, "download_html", lambda url, *args, **kwargs: _download(url, pages[url]))
    report = {}
    rows = discovery.discover_listing_urls(["https://www.yapo.cl/searchresult/test"], report=report)
    assert [row["page_number"] for row in rows] == [1, 2]
    assert report["pagination_complete"] is True
    assert len(report["pages"]) == 2


def test_duplicate_only_intermediate_page_does_not_stop(monkeypatch):
    first = "https://www.yapo.cl/searchresult/test"
    second = "https://www.yapo.cl/searchresult/test?o=2"
    third = "https://www.yapo.cl/searchresult/test?o=3"
    pages = {
        first: _html(_listing("12345678") + '<a rel="next" href="?o=2">Siguiente</a>'),
        second: _html(_listing("12345678") + '<a rel="next" href="?o=3">Siguiente</a>'),
        third: _html(_listing("12345679") + '<nav class="pagination"><span class="active">3</span><a href="?o=2">2</a></nav>'),
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
    start_url = "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana-maipu&q=withcat.bienes-raices-venta-de-propiedades-casas"
    rows = [
        {"url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678?utm_source=x", "listing_id": "12345678", "page_number": 1},
        {"url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/otra-casa/12345678", "listing_id": "12345678", "page_number": 2},
        {"url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678?gclid=x", "listing_id": "99999999", "page_number": 2},
    ]
    monkeypatch.setattr(pilot, "discover_listing_urls", lambda *args, **kwargs: rows)
    monkeypatch.setattr(pilot.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    class Store:
        writes = 0
        def __init__(self, _): pass
        def existing_listing_ids(self, ids): return set()
        def upsert_listing(self, *_): raise AssertionError("pilot must never write MongoDB")
    monkeypatch.setattr(pilot, "MongoStore", Store)
    details = []
    monkeypatch.setattr(pilot, "download_html", lambda url, *args, **kwargs: details.append(url) or _download(url, _html('<h1>Casa completa</h1><div class="d3-property-about__text">Texto completo</div><div class="price">4.000 UF</div>')))
    monkeypatch.setattr(pilot, "classify_with_rules", lambda _: {"state": "INCIERTO", "evidence": []})
    cfg = AppConfig(mongo_uri="mongodb://fake", reports_dir=tmp_path, html_dumps_dir=tmp_path / "html")
    result = pilot.run_pilot(start_url, cfg, batch_id="test")
    assert len(details) == 1
    assert result["batch_duplicates"] == 2
    assert result["mongo_writes"] == 0


def test_mongo_unavailable_is_not_treated_as_no_duplicates(monkeypatch, tmp_path):
    start_url = "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana-maipu&q=withcat.bienes-raices-venta-de-propiedades-casas"
    monkeypatch.setattr(pilot, "discover_listing_urls", lambda *args, **kwargs: [{"url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678", "listing_id": "12345678"}])
    monkeypatch.setattr(pilot.ProxyManager, "from_config", lambda _: ProxyManager(["proxy.test:9000"]))
    monkeypatch.setattr(pilot, "MongoStore", lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    detail_calls = []
    monkeypatch.setattr(pilot, "download_html", lambda *args, **kwargs: detail_calls.append(args[0]))
    cfg = AppConfig(mongo_uri="mongodb://fake", reports_dir=tmp_path, html_dumps_dir=tmp_path / "html")
    result = pilot.run_pilot(start_url, cfg, batch_id="test")
    assert result["mongo_dedup_verified"] is False
    assert result["new_ads"] == 0
    assert detail_calls == []


def test_deepseek_is_off_even_when_environment_enables_it(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_ENABLED", "true")
    assert AppConfig().deepseek_enabled is False
    with pytest.raises(RuntimeError, match="DeepSeek está desactivado"):
        deepseek_classifier.classify_with_deepseek({}, {}, AppConfig())


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
