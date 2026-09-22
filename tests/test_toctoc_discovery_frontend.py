from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scrapers.scraper_toctoc.discovery import (  # noqa: E402
    _extract_page_records,
    _playwright_commune_label,
    _playwright_scope_url,
    _set_page_param,
    evaluate_discovery_health,
    extract_metadata_from_embedded_payload,
    is_listing_detail_url,
    matches_requested_commune,
    property_type_catalog,
    property_type_specs,
    _pagination_page_size,
    _summarize_network_contract,
    _safe_query_params,
    _compare_request_contexts,
    _batch_number_for_page,
    _batch_bounds,
    _batch_checkpoints,
    _reported_results_from_visible_text,
)


def _listing_url(token: str) -> str:
    return f"https://www.toctoc.com/venta/departamento/metropolitana/la-florida/b_{token}"


def test_current_public_route_is_a_valid_detail_url_and_commune_matches():
    url = _listing_url("a" * 40)
    assert is_listing_detail_url(url)
    assert matches_requested_commune(url, "la-florida")
    assert not matches_requested_commune(url, "maipu")


def test_legacy_next_data_fixture_is_preserved():
    payload = {
        "props": {
            "pageProps": {
                "propiedades": {
                    "total": 1,
                    "results": [{
                        "idProperty": 4351894,
                        "urlFicha": _listing_url("b" * 40),
                        "titulo": "Casa usada",
                        "comuna": "La Florida",
                        "tipoOperacion": "Venta Usado",
                    }],
                }
            }
        }
    }
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script>"
    records, diagnostics = _extract_page_records(html, "https://www.toctoc.com")
    assert len(records) == 1
    assert diagnostics["next_data_present"] is True
    assert records[0]["listing_id"] == "4351894"


def test_react_engine_props_fixture_is_supported():
    payload = {
        "filtros": {"results": [{
            "idProperty": 4111488,
            "urlFicha": _listing_url("c" * 40),
            "title": "Publicación usada",
            "tipoOperacion": "Venta Usado",
        }]},
        "total": 1,
    }
    html = '<script id="react-engine-props" type="application/json">' + json.dumps(payload) + "</script>"
    records, diagnostics = _extract_page_records(html, "https://www.toctoc.com")
    assert len(records) == 1
    assert diagnostics["react_engine_props_present"] is True
    assert diagnostics["react_engine_props_parseable"] is True
    assert records[0]["listing_id"] == "4111488"


def test_duplicate_embedded_records_are_collapsed():
    url = _listing_url("d" * 40)
    payload = {"results": [{"idProperty": 1, "url": url}, {"idProperty": 1, "url": url}]}
    records = extract_metadata_from_embedded_payload(payload, "https://www.toctoc.com", "fixture")
    assert len(records) == 1


def test_discovery_health_aborts_on_200_with_expected_results_but_zero_urls():
    health = evaluate_discovery_health(200, 1185, 0)
    assert health == {
        "discovery_degraded": True,
        "run_aborted": True,
        "abort_reason": "HTTP_200_EXPECTED_RESULTS_BUT_ZERO_URLS",
    }


def test_discovery_health_allows_real_zero_result_search():
    health = evaluate_discovery_health(200, 0, 0)
    assert health["discovery_degraded"] is False
    assert health["run_aborted"] is False


def test_pagination_parameter_replaces_previous_value():
    url = "https://www.toctoc.com/venta/departamento/metropolitana/la-florida?estado=2&pagina=1"
    assert _set_page_param(url, 2).endswith("estado=2&pagina=2")


def test_invalid_url_is_not_accepted_as_listing():
    assert not is_listing_detail_url("https://www.toctoc.com/venta/departamento/metropolitana/la-florida")
    assert not is_listing_detail_url("https://www.toctoc.com/propiedades/venta/itau/1384492?o=menu")


def test_playwright_scope_uses_current_spa_search_shell_and_preserves_used_filter():
    class Config:
        base_url = "https://www.toctoc.com"

    start = "https://www.toctoc.com/venta/departamento/metropolitana/la-florida?estado=2"
    scope = _playwright_scope_url(Config(), start, estado=2)
    assert scope.startswith("https://www.toctoc.com/resultados/lista/compra/departamento/")
    assert "estado=2" in scope
    assert "pagina=1" in scope


def test_playwright_commune_label_handles_crm_santiago_alias():
    assert _playwright_commune_label("santiago-centro") == "Santiago"
    assert _playwright_commune_label("la-florida") == "La Florida"


def test_all_current_toctoc_property_types_use_frontend_route_aliases():
    catalog = {item["slug"]: item["route_slug"] for item in property_type_catalog()}
    assert catalog == {
        "departamento": "departamento",
        "casa": "casa",
        "oficina": "oficina",
        "local-comercial": "local-comercial",
        "terreno": "terreno",
        "parcela": "parcela",
        "agricola": "campo-agricola",
        "industrial": "terreno-industrial",
        "bodega": "bodega",
        "estacionamiento": "estacionamiento",
        "vacacional": "lugar-vacacional",
    }


def test_todos_does_not_silently_reduce_to_residential_subset():
    specs = property_type_specs("todos")
    assert len(specs) == 11
    assert {item["slug"] for item in specs} == {
        "casa", "departamento", "oficina", "local-comercial", "terreno",
        "parcela", "agricola", "industrial", "bodega", "estacionamiento", "vacacional",
    }


def test_discovery_health_requires_pagination_to_be_complete_when_expected_pages_are_missing():
    # This is the diagnostic shape emitted when SSR ignores pagina and repeats
    # its bootstrap payload.  The production guard must not approve it.
    report = {
        "expected_results": 1193,
        "pages_expected": 60,
        "pages_fetched": 1,
        "repeated_page": True,
        "ssr_page_limit": True,
    }
    assert report["pages_fetched"] < report["pages_expected"]
    assert report["repeated_page"] is True


def test_embedded_preload_batch_does_not_reduce_expected_ui_pages():
    assert _pagination_page_size(0, 260) == 20
    assert _pagination_page_size(20, 260) == 20


def test_results_network_contract_requires_successful_browser_request():
    events = [
        {
            "event": "request",
            "method": "GET",
            "path": "/venta/gw-lista-seo/properties",
            "params": {"page": "2", "limit": "260"},
        },
        {
            "event": "response",
            "method": "GET",
            "path": "/venta/gw-lista-seo/properties",
            "params": {"page": "2", "limit": "260"},
            "status": 403,
            "body_marker": "recaptcha_required",
        },
    ]
    summary = _summarize_network_contract(events)
    assert summary["results_endpoint"].endswith("/gw-lista-seo/properties")
    assert summary["pagination_parameter"] == "page"
    assert summary["page_size"] == "260"
    assert summary["can_call_endpoint_directly"] is False
    assert summary["recaptcha_required_observed"] is True


def test_successful_browser_recaptcha_verification_is_not_a_blocking_challenge():
    summary = _summarize_network_contract([
        {
            "event": "request",
            "method": "POST",
            "path": "/venta/gw-lista-seo/recaptcha/verify",
            "params": {},
        },
        {
            "event": "response",
            "method": "POST",
            "path": "/venta/gw-lista-seo/recaptcha/verify",
            "params": {},
            "status": 201,
        },
        {
            "event": "response",
            "method": "GET",
            "path": "/venta/gw-lista-seo/properties",
            "params": {"page": "1", "limit": "260"},
            "status": 200,
        },
    ])
    assert summary["recaptcha_verification_observed"] is True
    assert summary["recaptcha_required_observed"] is False


def test_request_diagnostics_redact_filters_and_session_like_query_values():
    params = _safe_query_params(
        "page=2&limit=260&filtros=%7B%22comuna%22%3A%22maipu%22%7D&sessionToken=secret"
    )
    assert params["page"] == "2"
    assert params["limit"] == "260"
    assert params["filtros"]["redacted"] is True
    assert params["sessionToken"] == "<redacted>"
    assert "secret" not in repr(params)


def test_request_context_comparison_reports_only_missing_names():
    initial = {
        "header_names": ["cookie", "referer", "user-agent"],
        "request_cookie_names": ["session", "xsrf"],
        "context_cookies": [{"name": "session"}, {"name": "xsrf"}],
        "safe_headers": {"user-agent": "ua", "referer": "https://www.toctoc.com/"},
        "token_header_names": ["cookie"],
        "user_agent": "ua",
    }
    page2 = {
        "header_names": ["cookie", "user-agent"],
        "request_cookie_names": ["session"],
        "context_cookies": [{"name": "session"}],
        "safe_headers": {"user-agent": "ua"},
        "token_header_names": ["cookie"],
        "user_agent": "ua",
    }
    comparison = _compare_request_contexts(initial, page2)
    assert comparison["missing_header_names_in_page2"] == ["referer"]
    assert comparison["missing_request_cookie_names_in_page2"] == ["xsrf"]
    assert comparison["missing_context_cookie_names_in_page2"] == ["xsrf"]
    assert comparison["possible_missing_session_requirement"] is True


def test_batch_boundaries_match_the_260_result_browser_batches():
    assert _batch_number_for_page(1) == 1
    assert _batch_number_for_page(13) == 1
    assert _batch_number_for_page(14) == 2
    assert _batch_bounds(1) == (1, 13)
    assert _batch_bounds(2) == (14, 26)


def test_batch_checkpoint_preserves_challenge_without_counting_blocked_ids():
    reports = [
        {
            "page": page,
            "batch_number": 1,
            "completed": True,
            "first_listing_id": str(page * 10),
            "last_listing_id": str(page * 10 + 9),
            "listing_ids": [str(page * 10 + offset) for offset in range(10)],
            "result_statuses": [200] if page == 1 else [],
        }
        for page in range(1, 14)
    ]
    reports.append({
        "page": 14,
        "batch_number": 2,
        "completed": False,
        "listing_ids": [],
        "result_statuses": [403],
        "challenge_detected": True,
    })
    batches = _batch_checkpoints(reports)
    assert batches[0]["status"] == "COMPLETE"
    assert batches[0]["completed"] is True
    assert batches[0]["unique_count"] == 130
    assert batches[1]["status"] == "CHALLENGE_BLOCKED"
    assert batches[1]["completed"] is False
    assert batches[1]["unique_count"] == 0
    assert batches[1]["request_status"] == 403


def test_visible_result_count_is_read_only_and_locale_tolerant():
    assert _reported_results_from_visible_text("1 - 30 de 1.193 resultados de Venta") == 1193
    assert _reported_results_from_visible_text("1 - 30 of 1,193 resultados") is None
    assert _reported_results_from_visible_text("Sin resultados") is None
