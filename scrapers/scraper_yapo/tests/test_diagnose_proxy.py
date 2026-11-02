from __future__ import annotations

import json
import socket
import ssl
from pathlib import Path
import sys

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from scrapers.scraper_yapo.config import AppConfig
from scrapers.scraper_yapo.diagnose_proxy import ALLOWED_TEST_URL, _proxy_http_get, classify_error, classify_http_status, diagnose_proxy


@pytest.fixture
def clear_proxy_env(monkeypatch):
    for key in ("PROXY_URL", "PROXY_URLS", "YAPO_PROXY_URLS", "PROXIES", "PROXY_USER", "PROXY_PASS"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (socket.gaierror(-2, "getaddrinfo failed"), "DNS"),
        (ConnectionRefusedError(10061, "connection refused"), "CONNECTION_REFUSED"),
        (TimeoutError("operation timed out"), "TIMEOUT"),
        (ssl.SSLError("TLS handshake failed"), "TLS"),
        (RuntimeError("HTTP 407 proxy authentication required"), "AUTH_HTTP_407"),
        (RuntimeError("unclassified transport failure"), "OTHER"),
    ],
)
def test_error_categories_are_classified_without_echoing_error_values(error, category):
    assert classify_error(error) == category


@pytest.mark.parametrize(
    ("status", "category"),
    [(200, "OK"), (407, "AUTH_HTTP_407"), (403, "DESTINATION_BLOCKED"), (429, "DESTINATION_BLOCKED"), (500, "OTHER_HTTP_STATUS")],
)
def test_http_status_categories(status, category):
    assert classify_http_status(status) == category


def test_invalid_proxy_url_is_reported_as_format_error(clear_proxy_env, tmp_path):
    report = diagnose_proxy(AppConfig(proxy_url="http://bad host", reports_dir=tmp_path), report_dir=tmp_path)
    assert report["result_category"] == "INVALID_FORMAT"
    assert report["invalid_proxy_input_count"] == 1
    assert report["failure_origin"] == "configuration_or_normalization"


def test_dns_failure_skips_tcp_and_http(clear_proxy_env, tmp_path):
    tcp_calls, http_calls = [], []

    def dns(*_args, **_kwargs):
        raise socket.gaierror(-2, "getaddrinfo failed")

    report = diagnose_proxy(
        AppConfig(proxy_url="proxy.invalid:8080", reports_dir=tmp_path), report_dir=tmp_path,
        dns_resolver=dns, tcp_connector=lambda *a, **k: tcp_calls.append(a),
        http_get=lambda *a: http_calls.append(a),
    )
    assert report["result_category"] == "DNS"
    assert tcp_calls == [] and http_calls == []


@pytest.mark.parametrize(
    ("error", "category"),
    [(ConnectionRefusedError(10061, "refused"), "CONNECTION_REFUSED"), (TimeoutError("timed out"), "TIMEOUT")],
)
def test_tcp_connectivity_categories(clear_proxy_env, tmp_path, error, category):
    def tcp(*_args, **_kwargs):
        raise error

    report = diagnose_proxy(
        AppConfig(proxy_url="proxy.test:8080", reports_dir=tmp_path), report_dir=tmp_path,
        dns_resolver=lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8080))],
        tcp_connector=tcp,
    )
    assert report["result_category"] == category


@pytest.mark.parametrize(
    ("status_or_error", "category"),
    [(407, "AUTH_HTTP_407"), (403, "DESTINATION_BLOCKED"), (ssl.SSLError("TLS failed"), "TLS"), (RuntimeError("other failure"), "OTHER")],
)
def test_controlled_http_categories_and_sanitized_report(clear_proxy_env, tmp_path, status_or_error, category, monkeypatch):
    monkeypatch.setenv("PROXY_USER", "do-not-print-user")
    monkeypatch.setenv("PROXY_PASS", "do-not-print-password")

    def http_get(*_args):
        if isinstance(status_or_error, BaseException):
            raise status_or_error
        return status_or_error

    class Socket:
        def close(self):
            pass

    report = diagnose_proxy(
        AppConfig(proxy_url="proxy.test:8080", reports_dir=tmp_path), report_dir=tmp_path,
        dns_resolver=lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8080))],
        tcp_connector=lambda *a, **k: Socket(), http_get=http_get,
    )
    assert report["result_category"] == category
    saved = Path(report["report_path"]).read_text(encoding="utf-8")
    assert "do-not-print-user" not in saved
    assert "do-not-print-password" not in saved
    assert report["credentials_logged"] is False


def test_report_records_variable_presence_only(clear_proxy_env, tmp_path, monkeypatch):
    monkeypatch.setenv("PROXY_URL", "proxy.test:8080")
    monkeypatch.setenv("PROXY_USER", "secret-user")
    monkeypatch.setenv("PROXY_PASS", "secret-pass")
    report = diagnose_proxy(AppConfig(reports_dir=tmp_path), report_dir=tmp_path)
    saved = Path(report["report_path"]).read_text(encoding="utf-8")
    assert report["environment_variables_configured"]["PROXY_URL"] is True
    assert report["environment_variables_configured"]["PROXY_PASS"] is True
    assert "secret-user" not in saved and "secret-pass" not in saved


def test_controlled_http_uses_only_configured_proxy_and_no_environment_bypass(monkeypatch):
    import requests
    captured = []

    class Response:
        status_code = 200
        def close(self): pass

    class Session:
        trust_env = True
        def get(self, url, **kwargs):
            captured.append((url, self.trust_env, kwargs.get("proxies"), kwargs.get("allow_redirects")))
            return Response()

    monkeypatch.setattr(requests, "Session", Session)
    assert _proxy_http_get("http://user:password@proxy.test:8080", ALLOWED_TEST_URL, 1) == 200
    assert captured == [(ALLOWED_TEST_URL, False, {"http": "http://user:password@proxy.test:8080", "https": "http://user:password@proxy.test:8080"}, False)]


@pytest.mark.parametrize(
    ("exception", "category", "origin"),
    [
        (pytest.param("invalid_proxy", "INVALID_FORMAT", "code_defect_or_proxy_format", id="code-format")),
        (pytest.param("proxy_error", "PROXY_ERROR_UNCLASSIFIED", "environment_or_provider", id="proxy-environment")),
    ],
)
def test_diagnostic_separates_transport_format_from_proxy_environment(clear_proxy_env, tmp_path, exception, category, origin):
    import requests
    class Socket:
        def close(self): pass
    def http_get(*_args):
        if exception == "invalid_proxy":
            raise requests.exceptions.InvalidProxyURL("sensitive proxy detail must not be emitted")
        raise requests.exceptions.ProxyError("sensitive proxy detail must not be emitted")
    report = diagnose_proxy(
        AppConfig(proxy_url="proxy.test:8080", reports_dir=tmp_path), report_dir=tmp_path,
        dns_resolver=lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8080))],
        tcp_connector=lambda *a, **k: Socket(), http_get=http_get,
    )
    saved = Path(report["report_path"]).read_text(encoding="utf-8")
    assert report["result_category"] == category
    assert report["failure_origin"] == origin
    assert "sensitive proxy detail" not in saved
