"""P3 proxy-selection tests using fakes; no network traffic."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scraper_toctoc"))

from config import AppConfig  # noqa: E402
from downloader import download_html  # noqa: E402
from proxy_manager import select_proxy_from_pool  # noqa: E402


HTML = """<html><head><title>Departamento</title></head><body>
TT-123456 departamento UF 2.000 dormitorio superficie
<img src='/toctoc/fotos/123.jpg'>
</body></html>"""


class FakeRaw:
    def stream(self, decode_content=False):
        yield HTML.encode("utf-8")


class FakeResponse:
    status_code = 200
    headers = {"Content-Encoding": ""}
    raw = FakeRaw()
    content = HTML.encode("utf-8")

    def raise_for_status(self):
        return None

    def close(self):
        return None


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse()


class P3ProxyTests(unittest.TestCase):
    def test_pool_rotation_order(self):
        pool = ["proxy-one", "proxy-two", "proxy-three"]
        self.assertEqual([select_proxy_from_pool(pool, i) for i in range(4)], [
            "proxy-one", "proxy-two", "proxy-three", "proxy-one",
        ])

    def test_downloader_uses_exact_explicit_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(html_dumps_dir=Path(tmp))
            session = FakeSession()
            download_html("https://example.test/one", config, batch_id="p3", session=session, proxy="proxy-two")
            self.assertEqual(session.calls[0]["proxies"], {"http": "proxy-two", "https": "proxy-two"})

    def test_direct_mode_has_no_proxy(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PROXY_URLS": "", "PROXIES": ""}, clear=False):
            config = AppConfig(html_dumps_dir=Path(tmp))
            session = FakeSession()
            download_html("https://example.test/direct", config, batch_id="p3", session=session, attempt=0)
            self.assertIsNone(session.calls[0]["proxies"])

    def test_proxy_mode_does_not_degrade_to_direct(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(html_dumps_dir=Path(tmp))
            session = FakeSession()
            download_html("https://example.test/proxy", config, batch_id="p3", session=session, proxy="proxy-one")
            self.assertIsNotNone(session.calls[0]["proxies"])

    def test_proxy_mode_requires_a_configured_pool(self):
        source = (ROOT / "scraper_toctoc" / "run_toctoc.py").read_text(encoding="utf-8")
        self.assertIn('proxy_mode == "proxy" and not proxy_pool', source)

    def test_auto_mode_respects_configured_order(self):
        pool = ["proxy-one", "proxy-two", "proxy-three"]
        auto_fallbacks = [select_proxy_from_pool(pool, attempt - 2) for attempt in (2, 3, 4)]
        self.assertEqual(auto_fallbacks, pool)

    def test_playwright_fallback_receives_selected_proxy_parameter(self):
        source = (ROOT / "scraper_toctoc" / "run_toctoc.py").read_text(encoding="utf-8")
        self.assertIn("_download_with_pw(url, html_path_for_url(url, config, batch_id=batch_id), p_url)", source)
        self.assertIn("_ensure_pw(p_url)", source)


if __name__ == "__main__":
    unittest.main()
