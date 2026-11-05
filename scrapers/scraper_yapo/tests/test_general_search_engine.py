from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scrapers.scraper_yapo import discovery, pilot
from scrapers.scraper_yapo.ejecutar_busqueda import main
from scrapers.scraper_yapo.search_config import SearchConfig, verify_search_page


def _search(tmp_path, *, live=True, batch="test-general", **kwargs):
    values = {
        "region": "Región Metropolitana", "communes": ("Pudahuel",),
        "operation": "venta", "property_type": "departamento",
        "price_min": 2000, "price_max": 4000, "price_currency": "UF",
        "live": live, "output_dir": str(tmp_path), "batch_id": batch,
    }
    values.update(kwargs)
    return SearchConfig(**values)


def _canonical_html(url):
    return f'<html><head><link rel="canonical" href="{url}"></head><body>resultados Yapo</body></html>'


def _config(tmp_path):
    return SimpleNamespace(
        ensure_layout=lambda: None,
        generate_batch_id=lambda: "generated-test",
        reports_dir=tmp_path,
        html_dumps_dir=tmp_path / "html",
        mongo_uri="mongodb://read-only-test-double",
        uf_valor_clp=40000,
        uf_fecha="2026-11-03",
    )


class _Proxy:
    available = True

    @classmethod
    def from_config(cls, _config):
        return cls()

    def has_proxies(self):
        return bool(type(self).available)


def _install_live_mocks(
    monkeypatch, tmp_path, *, pages_complete=True, rows=None, fail=False,
    selection_truncated=False,
):
    calls = {"discover": 0, "detail": 0}

    def discovery(urls, *, report, page_filter_validator, **_kwargs):
        calls["discover"] += 1
        if fail:
            raise RuntimeError("YAPO_PROXY_REQUEST_FAILED: ProxyError")
        first_url = urls[0]
        validation = page_filter_validator(first_url, _canonical_html(first_url))
        report.update({
            "pages": [{"url": first_url, "page_number": 1, "status": "complete",
                       "listing_count": len(rows or []), "duplicate_count": 0,
                       "filter_validation": validation}],
            "pagination_complete": pages_complete,
            "pagination_cause": "explicit_total_pages_reached" if pages_complete else "safety_max_pages_reached",
            "selection_truncated": selection_truncated,
            "selection_dropped_count": 1 if selection_truncated else 0,
            "errors": [] if pages_complete else ["pagination_incomplete"],
            "proxy_requests": 1, "request_attempts": 1,
            "successful_downloads": 1, "cache_responses": 0,
        })
        return list(rows or [])

    monkeypatch.setattr(pilot, "ProxyManager", _Proxy)
    monkeypatch.setattr(pilot, "discover_listing_urls", discovery)
    monkeypatch.setattr(pilot, "html_path_for_url", lambda *_a, **_k: SimpleNamespace(exists=lambda: False))

    def detail_download(url, *_args, **_kwargs):
        calls["detail"] += 1
        return SimpleNamespace(
            url=url, html="<!doctype html><html><body>detail</body></html>",
            status_code=200, fetch_source="requests_proxy", html_path=tmp_path / "detail.html",
            blocked=False,
        )

    monkeypatch.setattr(pilot, "download_html", detail_download)
    monkeypatch.setattr(pilot, "validate_html", lambda _html: {"status": "OK", "reason": "valid_html"})
    description = "Descripción íntegra con detalles de la propiedad. " * 30
    monkeypatch.setattr(pilot, "extract_listing_fields", lambda *_a, **_k: {
        "title": "Departamento en Pudahuel", "description": description,
        "description_source": "description", "descripcion_is_truncated": False,
        "price": "UF 3.500", "price_value": "3500", "price_currency": "UF",
        "comuna": "Pudahuel", "comuna_source": "yapo_detail_header_location",
        "comuna_evidence": {"status": "confirmed_detail"},
        "attributes": {"dormitorios": "3", "Baños": "2"},
        "publicador_visible": "Anunciante particular", "images": ["https://img.example/1.jpg"],
    })
    monkeypatch.setattr(pilot, "_enrich_property_fields", lambda fields, *_a, **_k: fields)
    monkeypatch.setattr(pilot, "classify_yapo_listing", lambda _fields: {
        "state": "INCIERTO", "evidence": ["reglas deterministas"], "source": "rules_json",
    })
    return calls


def test_react_page_two_route_is_verified_against_canonical_filters(tmp_path):
    search = _search(tmp_path, bedrooms_min=3)
    page_one = search.start_urls()[0][0]
    page_two = page_one.replace(
        "/bienes-raices-venta-de-propiedades-apartamentos/",
        "/bienes-raices-venta-de-propiedades-apartamentos.2/",
    )
    html = _canonical_html(page_one)

    verified = verify_search_page(page_two, html, search, "pudahuel")
    assert verified["verified"] is True
    assert verified["actual_filter_tokens"] == verified["expected_filter_tokens"]
    assert pilot._page_validator(search)(page_two, html)["verified"] is True

    wrong_commune = _canonical_html(page_one.replace("pudahuel", "maipu"))
    assert verify_search_page(page_two, wrong_commune, search, "pudahuel")["verified"] is False
    wrong_filter = page_two.replace("f_price.2000-4000", "f_price.1000-1500")
    assert verify_search_page(wrong_filter, html, search, "pudahuel")["verified"] is False
    extra_conflicting_filter = page_two + "&q=f_price.5000-6000"
    assert verify_search_page(extra_conflicting_filter, html, search, "pudahuel")["reason"] == "requested_query_conflicting_filters"


def test_g_detail_validation_excludes_wrong_commune_type_operation_and_price(tmp_path):
    search = _search(tmp_path)
    listing = {
        "url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-apartamentos/depto/12345678",
        "search_commune": "pudahuel", "region_slug": "region-metropolitana",
        "discovery_comuna": "Pudahuel",
    }
    base = {
        "comuna": "Pudahuel", "comuna_evidence": {"status": "confirmed_detail"},
        "price": "UF 3.500", "price_value": "3500", "price_currency": "UF",
    }
    assert pilot.validate_listing_filters(search, listing, base)["status"] == "PASA"
    assert pilot.validate_listing_filters(search, {**listing, "discovery_comuna": "Talca"}, base)["status"] == "PENDIENTE_VALIDACION"
    assert pilot.validate_listing_filters(search, {**listing, "url": listing["url"].replace("venta-de-propiedades-apartamentos", "alquiler-apartamentos")}, base)["status"] == "FUERA_DE_FILTRO"
    assert pilot.validate_listing_filters(search, listing, {**base, "comuna": "Talca"})["status"] == "FUERA_DE_FILTRO"
    assert pilot.validate_listing_filters(search, listing, {**base, "region": "Región de Los Lagos"})["status"] == "FUERA_DE_FILTRO"
    assert pilot.validate_listing_filters(search, listing, {**base, "price_value": "4500"})["status"] == "FUERA_DE_FILTRO"


def test_h_clp_listing_converts_to_uf_only_with_fresh_documented_rate(tmp_path):
    search = _search(tmp_path)
    listing = {
        "url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-apartamentos/depto/12345678",
        "search_commune": "pudahuel", "region_slug": "region-metropolitana",
        "discovery_comuna": "Pudahuel",
    }
    extracted = {
        "comuna": "Pudahuel", "comuna_evidence": {"status": "confirmed_detail"},
        "price": "$ 140.000.000 CLP", "price_value": "140000000", "price_currency": "CLP",
    }
    fresh = pilot.validate_listing_filters(
        search, listing, extracted, uf_value_clp=40000, uf_date="2026-11-03",
    )
    assert fresh["status"] == "PASA"
    assert fresh["checks"]["price"]["conversion"] == "CLP_to_UF_using_uf_40000_on_2026-11-03"
    stale = pilot.validate_listing_filters(
        search, listing, extracted, uf_value_clp=40000, uf_date="2026-07-14",
    )
    assert stale["status"] == "PENDIENTE_VALIDACION"


def test_detail_commune_provenance_survives_enrichment_that_uses_card_location(tmp_path, monkeypatch):
    search = _search(tmp_path, batch="commune-provenance")
    listing = {
        "url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-apartamentos/depto/12345678",
        "listing_id": "12345678", "discovery_comuna": "Talca",
        "source_search_url": search.start_urls()[0][0], "page_number": 1,
    }
    _install_live_mocks(monkeypatch, tmp_path, rows=[listing])

    class Store:
        def __init__(self, _config): pass
        def existing_listing_ids(self, _ids): return set()
        def existing_listing_urls(self, _urls): return set()

    monkeypatch.setattr(pilot, "MongoStore", Store)
    monkeypatch.setattr(pilot, "extract_listing_fields", lambda *_a, **_k: {
        "title": "Departamento en Pudahuel", "description": "Descripción completa",
        "price": "UF 3.500", "price_value": "3500", "price_currency": "UF",
        "comuna": "Pudahuel", "comuna_source": "structured_real_estate_address",
        "comuna_evidence": {"status": "confirmed_detail"}, "attributes": {},
    })

    def overwrite_with_card(fields, *_a, **_k):
        fields["comuna"] = fields.get("discovery_comuna", "")
        return fields

    monkeypatch.setattr(pilot, "_enrich_property_fields", overwrite_with_card)
    result = pilot.run_search(search, _config(tmp_path))
    record = json.loads(Path(result["records_json"]).read_text(encoding="utf-8"))[0]
    assert record["details"]["comuna"] == "Pudahuel"
    assert record["comuna"] == "Pudahuel"
    assert record["filter_status"] == "PENDIENTE_VALIDACION"
    assert record["filter_validation"]["checks"]["commune"]["evidence"] == "detail_and_card_commune_disagree"


def test_i_empty_results_and_proxy_errors_are_reported_without_fallback(tmp_path, monkeypatch):
    search = _search(tmp_path, batch="empty-results")
    calls = _install_live_mocks(monkeypatch, tmp_path, rows=[])
    result = pilot.run_search(search, _config(tmp_path))
    assert result["ads_discovered_unique"] == 0
    assert result["mongo_dedup_status"] == "not_required_no_ads"
    assert result["commercial_result_available"] is True
    assert calls == {"discover": 1, "detail": 0}

    _Proxy.available = False
    try:
        blocked = pilot.run_search(_search(tmp_path, batch="no-proxy"), _config(tmp_path))
    finally:
        _Proxy.available = True
    assert blocked["pagination_cause"] == "proxy_unavailable"
    assert blocked["pages_visited"] == 0
    assert calls["discover"] == 1

    calls = _install_live_mocks(monkeypatch, tmp_path, fail=True)
    failed = pilot.run_search(_search(tmp_path, batch="proxy-error"), _config(tmp_path))
    assert failed["pagination_complete"] is False
    assert failed["pagination_cause"] == "discovery_exception"
    assert failed["commercial_result_available"] is False
    assert any("ProxyError" in error for error in failed["errors_or_blocks"])


def test_i_incomplete_pagination_keeps_partial_record_out_of_commercial_result(tmp_path, monkeypatch):
    search = _search(tmp_path, batch="partial-results")
    listing = {
        "url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-apartamentos/depto/12345678",
        "listing_id": "12345678", "discovery_comuna": "Pudahuel",
        "source_search_url": search.start_urls()[0][0], "page_number": 1,
    }
    _install_live_mocks(monkeypatch, tmp_path, pages_complete=False, rows=[listing])

    class Store:
        def __init__(self, _config): pass
        def existing_listing_ids(self, _ids): return set()
        def existing_listing_urls(self, _urls): return set()

    monkeypatch.setattr(pilot, "MongoStore", Store)
    result = pilot.run_search(search, _config(tmp_path))
    assert result["ads_discovered_unique"] == 1
    assert result["pagination_complete"] is False
    assert result["commercial_result_available"] is False
    assert result["filter_matches"] == 1


def test_truncated_listing_selection_cannot_be_reported_as_complete(tmp_path, monkeypatch):
    search = _search(tmp_path, batch="truncated-selection", max_listings=1)
    listing = {
        "url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-apartamentos/depto/12345678",
        "listing_id": "12345678", "discovery_comuna": "Pudahuel",
        "source_search_url": search.start_urls()[0][0], "page_number": 1,
    }
    _install_live_mocks(
        monkeypatch, tmp_path, rows=[listing], selection_truncated=True,
    )

    class Store:
        def __init__(self, _config):
            pass

        def existing_listing_ids(self, _ids):
            return set()

        def existing_listing_urls(self, _urls):
            return set()

    monkeypatch.setattr(pilot, "MongoStore", Store)
    result = pilot.run_search(search, _config(tmp_path))

    assert result["pagination_complete"] is True
    assert result["listing_selection_complete"] is False
    assert result["selection_dropped_count"] == 1
    assert result["commercial_result_available"] is False


def test_max_listings_keeps_one_record_but_continues_through_bounded_pagination(tmp_path, monkeypatch):
    from scrapers.scraper_yapo.downloader import DownloadResult

    first = "https://www.yapo.cl/search?region=region-metropolitana-pudahuel"
    second = first + "&page=2"
    page_html = "<!doctype html><html><body>" + ("search results " * 30) + "</body></html>"
    downloads = []

    def fake_download(url, _config, _manager, *, batch_id=None):
        downloads.append(url)
        path = tmp_path / f"{len(downloads)}.html"
        return DownloadResult(url, page_html, 200, "requests_proxy", path, "OK", "valid_html")

    monkeypatch.setattr(discovery, "download_html", fake_download)
    monkeypatch.setattr(discovery, "validate_html", lambda _html: {"status": "OK", "reason": "valid_html"})
    monkeypatch.setattr(discovery, "html_path_for_url", lambda url, _config, batch_id=None: tmp_path / ("first.html" if url == first else "second.html"))
    monkeypatch.setattr(discovery, "_extract_listing_urls_detailed", lambda _html, url: (
        ([{"url": f"https://www.yapo.cl/ad/{'11111111' if url == first else '22222222'}", "listing_id": "11111111" if url == first else "22222222", "discovery_comuna": "Pudahuel"},
          {"url": f"https://www.yapo.cl/ad/{'33333333' if url == first else '44444444'}", "listing_id": "33333333" if url == first else "44444444", "discovery_comuna": "Pudahuel"}], 2, 0)
    ))
    monkeypatch.setattr(discovery, "_pagination_state", lambda _html, url: (
        (second, False, "next_page_link") if url == first else (None, True, "explicit_disabled_next_control")
    ))
    config = SimpleNamespace(
        ensure_layout=lambda: None,
        generate_batch_id=lambda: "page-cap-test",
        html_dumps_dir=tmp_path,
    )
    report = {}
    records = discovery.discover_listing_urls(
        [first], max_pages=2, max_urls=1, until_end=True, batch_id="page-cap-test",
        report=report, config=config, proxy_manager=_Proxy(),
        page_filter_validator=lambda *_a: {"verified": True, "reason": "test_canonical"},
    )
    assert len(records) == 1
    assert downloads == [first, second]
    assert report["pages"][-1]["pagination_evidence"] == "explicit_disabled_next_control"
    assert report["selection_truncated"] is True
    assert report["selection_dropped_count"] == 3
    assert report["pagination_complete"] is True
    assert report["pagination_cause"] == "explicit_disabled_next_control"


def test_j_repeat_search_uses_read_only_database_dedup_and_keeps_full_description(tmp_path, monkeypatch):
    search = _search(tmp_path, batch="idempotency")
    listing = {
        "url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-apartamentos/depto/12345678",
        "listing_id": "12345678", "discovery_comuna": "Pudahuel",
        "source_search_url": search.start_urls()[0][0], "page_number": 1,
    }
    calls = _install_live_mocks(monkeypatch, tmp_path, rows=[listing])

    class Store:
        existing = set()
        writes = []
        def __init__(self, _config): pass
        def existing_listing_ids(self, ids): return self.existing.intersection(ids)
        def existing_listing_urls(self, urls): return set()
        def __getattr__(self, name):
            if any(token in name.lower() for token in ("insert", "update", "replace", "delete", "write")):
                self.writes.append(name)
                raise AssertionError(f"Mongo write attempted: {name}")
            raise AttributeError(name)

    monkeypatch.setattr(pilot, "MongoStore", Store)
    first = pilot.run_search(search, _config(tmp_path))
    first_records = json.loads(Path(first["records_json"]).read_text(encoding="utf-8"))
    from scrapers.scraper_yapo.simulate_captacion_compatibility import _validate

    schema_validation = _validate(first_records[0])
    assert first["new_ads"] == 1
    assert len(first_records[0]["description"]) > 1000
    assert first_records[0]["description"].endswith("detalles de la propiedad. ")
    assert first_records[0]["schema_version"] == "yapo_propiedades_v1"
    assert first_records[0]["descripcion"] == first_records[0]["body_text"] == first_records[0]["description"]
    assert first_records[0]["enlaces_fotos"] == first_records[0]["images"]
    assert first_records[0]["gestion"]["ejecutivo_id"] is None
    assert "owner_probability" not in first_records[0]["classification"]
    assert schema_validation["structural_valid"] is True
    assert schema_validation["extraction_complete"] is True
    assert schema_validation["technical_valid"] is True
    assert schema_validation["persistence_ready"] is True
    assert schema_validation["assignment_ready"] is False
    Store.existing.add("12345678")  # simulate the same identity already visible to the read-only lookup
    second = pilot.run_search(search, _config(tmp_path))
    assert second["mongo_existing"] == 1
    assert second["new_ads"] == 0
    assert calls["detail"] == 1
    assert Store.writes == []
    assert first["mongo_writes"] == second["mongo_writes"] == 0
    assert first["deepseek_calls"] == second["deepseek_calls"] == 0


def test_general_cli_defaults_to_offline_simulation(tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr("scrapers.scraper_yapo.ejecutar_busqueda.run_search", lambda search, config: seen.update(search=search, config=config) or {"errors_or_blocks": []})
    code = main([
        "--region", "Región Metropolitana", "--comuna", "Pudahuel",
        "--operacion", "venta", "--tipo", "departamento",
        "--precio-min", "2000", "--precio-max", "4000", "--moneda", "UF",
        "--output-dir", str(tmp_path),
    ])
    assert code == 0
    assert seen["search"].live is False
    assert json.loads(capsys.readouterr().out)["errors_or_blocks"] == []


def test_general_cli_returns_failure_for_incomplete_live_result(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "scrapers.scraper_yapo.ejecutar_busqueda.run_search",
        lambda _search, _config: {
            "errors_or_blocks": [], "commercial_result_available": False,
        },
    )
    code = main([
        "--region", "Región Metropolitana", "--comuna", "Maipú",
        "--operacion", "venta", "--tipo", "casa",
        "--precio-min", "3000", "--precio-max", "6000", "--moneda", "UF",
        "--output-dir", str(tmp_path), "--live",
    ])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["commercial_result_available"] is False
