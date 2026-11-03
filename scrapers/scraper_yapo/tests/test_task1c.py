from __future__ import annotations

import socket
from pathlib import Path
import sys

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from scrapers.scraper_yapo.config import AppConfig
from scrapers.scraper_yapo.diagnose_proxy import diagnose_proxy
from scrapers.scraper_yapo.discovery import _discovery_commune_evidence, _norm_commune, discover_listing_urls
from scrapers.scraper_yapo.downloader import DownloadResult
from scrapers.scraper_yapo.mongo_store import MongoStore
from scrapers.scraper_yapo.pilot import _commune_validation
from scrapers.scraper_yapo.proxy_manager import ProxyManager


class _Socket:
    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _ok_dns(host, *_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 8080))]


def _ok_tcp(*_args, **_kwargs):
    return _Socket()


@pytest.fixture
def no_proxy_env(monkeypatch):
    for key in ("PROXY_URL", "PROXY_URLS", "YAPO_PROXY_URLS", "PROXIES", "PROXY_USER", "PROXY_PASS"):
        monkeypatch.delenv(key, raising=False)


def test_general_dns_ok_and_proxy_dns_fails_is_specific(no_proxy_env, tmp_path):
    calls = []

    def dns(host, *_args, **_kwargs):
        calls.append(host)
        if host == "example.com":
            return _ok_dns(host)
        raise socket.gaierror(11001, "private hostname must not be recorded")

    tcp_calls, http_calls = [], []
    report = diagnose_proxy(
        AppConfig(proxy_url="proxy.invalid:8080", reports_dir=tmp_path), report_dir=tmp_path,
        dns_resolver=dns, tcp_connector=lambda *a, **k: tcp_calls.append(a),
        http_get=lambda *a: http_calls.append(a), source_commit="base-sha",
    )
    assert calls == ["example.com", "proxy.invalid"]
    assert report["general_dns_control"] == {"status": "resolved", "error_category": None}
    assert report["result_category"] == "PROXY_HOST_DNS_FAILURE"
    assert report["proxies"][0]["diagnostic_category"] == "PROXY_HOST_DNS_FAILURE"
    assert tcp_calls == [] and http_calls == []
    assert report["runtime"]["python_version"]
    assert report["source_commit"] == "base-sha"
    saved = Path(report["report_file"])
    assert saved.name.startswith("diagnose_proxy_")
    payload = (tmp_path / saved.name).read_text(encoding="utf-8")
    assert "proxy.invalid" not in payload and "private hostname" not in payload


def test_general_dns_failure_is_runtime_dns(no_proxy_env, tmp_path):
    def dns(host, *_args, **_kwargs):
        raise socket.gaierror(11001, "must not be stored")

    report = diagnose_proxy(
        AppConfig(proxy_url="proxy.test:8080", reports_dir=tmp_path), report_dir=tmp_path,
        dns_resolver=dns,
    )
    assert report["general_dns_control"]["status"] == "failed"
    assert report["result_category"] == "RUNTIME_DNS_FAILURE"
    assert report["proxies"][0]["diagnostic_category"] == "RUNTIME_DNS_FAILURE"


def test_proxy_dns_success_then_tcp_failure_stops_before_http(no_proxy_env, tmp_path):
    http_calls = []

    def tcp(*_args, **_kwargs):
        raise ConnectionRefusedError(10061, "not stored")

    report = diagnose_proxy(
        AppConfig(proxy_url="proxy.test:8080", reports_dir=tmp_path), report_dir=tmp_path,
        dns_resolver=_ok_dns, tcp_connector=tcp,
        http_get=lambda *a: http_calls.append(a),
    )
    assert report["general_dns_control"]["status"] == "resolved"
    assert report["proxies"][0]["dns"] == "resolved"
    assert report["proxies"][0]["tcp"] == "failed"
    assert http_calls == []


def test_authentication_rejection_is_checked_after_dns_and_tcp(monkeypatch, tmp_path, no_proxy_env):
    monkeypatch.setenv("PROXY_USER", "private-user")
    monkeypatch.setenv("PROXY_PASS", "private-pass")
    order = []

    def dns(host, *_args, **_kwargs):
        order.append("dns:" + host)
        return _ok_dns(host)

    def tcp(*_args, **_kwargs):
        order.append("tcp")
        return _Socket()

    def http(proxy, dest, _timeout):
        order.append("http")
        assert "example.com" in dest and "yapo" not in dest
        return 407

    report = diagnose_proxy(
        AppConfig(proxy_url="proxy.test:8080", reports_dir=tmp_path), report_dir=tmp_path,
        dns_resolver=dns, tcp_connector=tcp, http_get=http,
    )
    assert order == ["dns:example.com", "dns:proxy.test", "tcp", "http"]
    assert report["result_category"] == "AUTH_HTTP_407"
    assert report["proxies"][0]["authentication"] == "rejected"
    saved = (tmp_path / report["report_file"]).read_text(encoding="utf-8")
    assert "private-user" not in saved and "private-pass" not in saved


def test_first_proxy_failure_does_not_prevent_second_proxy_success(no_proxy_env, tmp_path, monkeypatch):
    monkeypatch.setenv("PROXY_USER", "proxy-user")
    monkeypatch.setenv("PROXY_PASS", "proxy-pass")
    config = AppConfig(proxy_urls=["proxy-one.test:8080", "proxy-two.test:8081"], reports_dir=tmp_path)
    tcp_calls, http_calls = [], []

    def dns(host, *_args, **_kwargs):
        if host == "proxy-one.test":
            raise socket.gaierror(11001, "not stored")
        return _ok_dns(host)

    def tcp(address, **kwargs):
        tcp_calls.append(address)
        return _Socket()

    def http(proxy, _dest, _timeout):
        http_calls.append(proxy)
        return 200

    report = diagnose_proxy(config, report_dir=tmp_path, dns_resolver=dns, tcp_connector=tcp, http_get=http)
    assert [item["dns"] for item in report["proxies"]] == ["failed", "resolved"]
    assert [item["http_category"] for item in report["proxies"]] == ["not_attempted", "OK"]
    assert report["proxies"][1]["authentication"] == "accepted"
    assert report["result_category"] == "OK"
    assert len(tcp_calls) == 1 and len(http_calls) == 1


def test_https_proxy_gets_tls_check_before_controlled_request(no_proxy_env, tmp_path, monkeypatch):
    import scrapers.scraper_yapo.diagnose_proxy as diagnostic
    tls_calls = []

    class Context:
        def wrap_socket(self, sock, server_hostname):
            tls_calls.append(server_hostname)
            return sock

    monkeypatch.setattr(diagnostic.ssl, "create_default_context", lambda: Context())
    http_calls = []
    report = diagnose_proxy(
        AppConfig(proxy_url="https://proxy.test:8443", reports_dir=tmp_path), report_dir=tmp_path,
        dns_resolver=_ok_dns, tcp_connector=_ok_tcp,
        http_get=lambda *args: http_calls.append(args) or 200,
    )
    assert report["proxies"][0]["tls_to_proxy"] == "ok"
    assert tls_calls == ["proxy.test"]
    assert len(http_calls) == 1


class _FakeCollection:
    def __init__(self, docs):
        self.docs = docs
        self.query = None
        self.projection = None

    def find(self, query, projection):
        self.query, self.projection = query, projection
        return self.docs


def test_url_dedup_normalizes_legacy_domain_slash_tracking_and_fragment():
    requested = "https://www.yapo.cl/casa/la-casa/12345678"
    stored = "http://yapo.cl/casa/la-casa/12345678/?utm_source=old&gclid=abc#contacto"

    class Store(MongoStore):
        def __init__(self):
            self.coll = _FakeCollection([{"url": stored}])

        def collection(self):
            return self.coll

    store = Store()
    assert store.normalize_listing_url(stored) == requested
    assert store.existing_listing_urls([requested]) == {requested}
    assert store.coll.query["$or"]
    assert store.coll.projection == {"url": 1, "source_url": 1, "canonical_url": 1, "_id": 0}


def test_different_yapo_ids_are_not_deduplicated_by_similar_slug():
    requested = "https://www.yapo.cl/casa/slug-compartido/12345678"
    stored = "https://www.yapo.cl/casa/slug-compartido/87654321?utm_campaign=x"

    class Store(MongoStore):
        def __init__(self):
            self.coll = _FakeCollection([{"canonical_url": stored}])

        def collection(self):
            return self.coll

    assert Store().existing_listing_urls([requested]) == set()


def test_same_yapo_id_with_historical_slug_change_is_deduplicated():
    requested = "https://www.yapo.cl/casa/new-title/12345678"
    stored = "http://yapo.cl/casa/old-title/12345678/?utm_source=legacy"

    class Store(MongoStore):
        def __init__(self):
            self.coll = _FakeCollection([{"source_url": stored}])

        def collection(self):
            return self.coll

    assert Store().existing_listing_urls([requested]) == {requested}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Maipú", "confirmed_detail"),
        ("Maipu", "confirmed_detail"),
        ("Maipú, Región Metropolitana", "confirmed_detail"),
        ("Maipú, Región Metropolitana, Pudahuel", "pending_conflicting_location_evidence"),
        ("", "pending"),
        ("Maipú / Pudahuel", "pending_conflicting_location_evidence"),
        ("Talca", "confirmed_other_commune"),
    ],
)
def test_detail_commune_validation(value, expected):
    assert _commune_validation(value, "", "maipu", "region-metropolitana")[0] == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Maipú", "possible_match"),
        ("Maipu", "possible_match"),
        ("Maipú, Región Metropolitana", "pending_compound"),
        ("", "pending_missing"),
        ("Talca", "clear_other_commune"),
    ],
)
def test_discovery_location_is_retained_as_evidence_not_used_as_exact_filter(value, expected):
    assert _discovery_commune_evidence(value, {"maipu"}) == expected


def test_commune_normalizer_handles_accents():
    assert _norm_commune("Maipú, Región Metropolitana") == "maipu-region-metropolitana"


def test_discovery_retains_compound_and_missing_card_locations(monkeypatch, tmp_path):
    locations = ["Maipú", "Maipu", "Maipú, Región Metropolitana", ""]
    tiles = "".join(
        f'<div class="d3-ad-tile"><a href="/bienes-raices-venta-de-propiedades-casas/casa/{12345678 + i}">aviso</a>'
        f'<div class="d3-ad-tile__location">{location}</div></div>'
        for i, location in enumerate(locations)
    )
    html = "<!doctype html><html><body>" + tiles + '<nav class="pagination"><span class="active">1</span><span data-total-pages="1"></span></nav>' + (" contenido" * 40) + "</body></html>"
    url = "https://www.yapo.cl/searchresult/test"
    monkeypatch.setattr(
        "scrapers.scraper_yapo.discovery.download_html",
        lambda current, *_args, **_kwargs: DownloadResult(current, html, 200, "requests_proxy", Path("unused"), "OK", "valid_html"),
    )
    rows = discover_listing_urls(
        [url], config=AppConfig(reports_dir=tmp_path), proxy_manager=ProxyManager(["proxy.test:8080"]),
        target_communes=["Maipú"], report={},
    )
    assert len(rows) == 4
    assert [row["discovery_commune_evidence"] for row in rows] == [
        "possible_match", "possible_match", "pending_compound", "pending_missing"
    ]
