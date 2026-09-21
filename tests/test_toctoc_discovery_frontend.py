from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scrapers.scraper_toctoc.discovery import (  # noqa: E402
    _extract_page_records,
    _set_page_param,
    evaluate_discovery_health,
    extract_metadata_from_embedded_payload,
    is_listing_detail_url,
    matches_requested_commune,
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
