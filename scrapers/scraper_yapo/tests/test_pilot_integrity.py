import ast
import importlib
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))


def test_pilot_is_utf8_complete_python_and_exposes_the_general_engine():
    source = (ROOT / "pilot.py").read_bytes().decode("utf-8")
    tree = ast.parse(source, filename="pilot.py")
    functions = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"run_search", "run_pilot", "validate_listing_filters", "_commune_validation", "_extraction_issues"} <= functions
    compile(source, str(ROOT / "pilot.py"), "exec")


def test_pilot_imports_as_package_and_direct_script():
    module = importlib.import_module("scrapers.scraper_yapo.pilot")
    assert callable(module.run_pilot)
    direct = subprocess.run([sys.executable, str(ROOT / "pilot.py")], capture_output=True, text=True, timeout=30)
    assert direct.returncode == 0, direct.stderr


def test_pilot_source_has_no_deepseek_api_import_or_invocation():
    tree = ast.parse((ROOT / "pilot.py").read_text(encoding="utf-8"))
    deepseek_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            deepseek_calls.extend(alias.name for alias in node.names if "deepseek" in alias.name.lower())
        elif isinstance(node, ast.ImportFrom):
            if "deepseek" in str(node.module or "").lower():
                deepseek_calls.append(node.module)
        elif isinstance(node, ast.Call):
            if "deepseek" in ast.unparse(node.func).lower():
                deepseek_calls.append(ast.unparse(node.func))
    assert deepseek_calls == []


def test_pilot_cli_help_does_not_run_scrape():
    result = subprocess.run(
        [sys.executable, str(ROOT / "run_owner_hunt.py"), "pilot", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--start-url" in result.stdout


def test_pilot_uses_only_fake_read_only_mongo_and_never_calls_ai(tmp_path, monkeypatch):
    from scrapers.scraper_yapo import pilot

    writes = []

    class FakeStore:
        def __init__(self, _config):
            pass

        def existing_listing_ids(self, ids):
            return set()

        def existing_listing_urls(self, urls):
            return set()

        def __getattr__(self, name):
            if any(token in name.lower() for token in ("insert", "update", "replace", "delete", "bulk", "write")):
                writes.append(name)
                raise AssertionError(f"unexpected Mongo write method: {name}")
            raise AttributeError(name)

    class FakeProxyManager:
        @classmethod
        def from_config(cls, _config):
            return cls()

        def has_proxies(self):
            return True

    from scrapers.scraper_yapo.search_config import SearchConfig

    search = SearchConfig(
        region="Región Metropolitana", communes=("Maipú",), operation="venta",
        property_type="casa", price_min=3000, price_max=6000,
        price_currency="UF", live=True, output_dir=str(tmp_path), batch_id="offline-test",
    )
    start_url = search.start_urls()[0][0]
    search_html = f'<html><head><link rel="canonical" href="{start_url}"></head><body>search</body></html>'

    def fake_discovery(_urls, *, report, page_filter_validator, **_kwargs):
        filter_validation = page_filter_validator(start_url, search_html)
        report.update({
            "pagination_complete": True,
            "pagination_cause": "portal_end_signal",
            "pages": [{"url": start_url, "status": "complete", "listing_count": 1,
                       "duplicate_count": 0, "filter_validation": filter_validation}],
            "request_attempts": 1,
            "successful_downloads": 1,
            "cache_responses": 0,
            "proxy_requests": 1,
        })
        return [{"url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678",
                 "page_number": 1, "discovery_comuna": "Maipú", "source_search_url": start_url}]

    monkeypatch.setattr(pilot, "ProxyManager", FakeProxyManager)
    monkeypatch.setattr(pilot, "discover_listing_urls", fake_discovery)
    monkeypatch.setattr(pilot, "MongoStore", FakeStore)
    monkeypatch.setattr(pilot, "html_path_for_url", lambda *_args, **_kwargs: SimpleNamespace(exists=lambda: False))
    monkeypatch.setattr(pilot, "download_html", lambda *_args, **_kwargs: SimpleNamespace(
        fetch_source="cache", html="<html><body>ok</body></html>", blocked=False,
        html_path=tmp_path / "detail.html",
    ))
    monkeypatch.setattr(pilot, "validate_html", lambda _html: {"status": "OK", "reason": "valid_html"})
    monkeypatch.setattr(pilot, "extract_listing_fields", lambda *_args, **_kwargs: {
        "title": "Casa en venta", "description": "Descripción completa", "price": "UF 4.000",
        "price_value": "4000", "price_currency": "UF", "attributes": {},
        "comuna": "Maipú", "comuna_evidence": {"status": "confirmed_detail"},
        "seller_type": "", "contact_name": "", "contact_logo_alt": "",
    })
    monkeypatch.setattr(pilot, "_enrich_property_fields", lambda fields, *_args, **_kwargs: fields)
    monkeypatch.setattr(pilot, "classify_yapo_listing", lambda _fields: {
        "state": "INCIERTO", "evidence": [], "source": "rules_json",
    })
    monkeypatch.setattr(pilot, "_write_json", lambda *_args, **_kwargs: None)
    config = SimpleNamespace(
        ensure_layout=lambda: None,
        generate_batch_id=lambda: "offline-test",
        reports_dir=tmp_path,
        data_dir=tmp_path / "data",
        mongo_uri="mongodb://offline-mock",
        uf_valor_clp=40000,
        uf_fecha="2026-11-03",
    )
    summary = pilot.run_search(search, config=config)
    assert summary["mongo_dedup_status"] == "verified_read_only"
    assert summary["mongo_writes"] == 0
    assert summary["deepseek_calls"] == 0
    assert writes == []
    assert summary["filter_matches"] == 1
