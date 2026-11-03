from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from scrapers.scraper_yapo.extractor import extract_listing_commune, extract_listing_fields
from scrapers.scraper_yapo.pilot import (
    _commune_validation,
    _price_in_uf,
    validate_listing_filters,
)
from scrapers.scraper_yapo import pilot
from scrapers.scraper_yapo.config import AppConfig
from scrapers.scraper_yapo.proxy_manager import ProxyManager
from scrapers.scraper_yapo.search_config import SearchConfig


def _html(body: str) -> str:
    return "<!doctype html><html><body>" + body + "</body></html>"


def test_explicit_comuna_attribute_is_extracted_and_uf_4000_matches_pilot():
    fields = extract_listing_fields(_html(
        '<h1>Casa en Maipú</h1><ul><li>Comuna: Maipú</li></ul>'
        '<span class="price">UF 4.000</span>'
    ))
    assert fields["attributes"]["comuna"] == "Maipú"
    assert fields["comuna"] == "Maipú"
    assert fields["comuna_source"] == "explicit_attribute"
    assert fields["comuna_evidence"]["status"] == "confirmed_detail"
    assert _price_in_uf(fields["price"]) == 4000
    territory = _commune_validation(
        fields["comuna"], "Maipú", "maipu", "region-metropolitana",
        fields["comuna_evidence"]["status"],
    )
    assert territory[0] == "confirmed_detail"
    search = SearchConfig(
        region="Región Metropolitana", communes=("Maipú",), operation="venta",
        property_type="casa", price_min=3000, price_max=6000, price_currency="UF",
    )
    listing = {
        "url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678",
        "discovery_comuna": "Maipú", "search_commune": "maipu",
        "region_slug": "region-metropolitana",
    }
    fields.update({"price_value": "4000", "price_currency": "UF"})
    assert validate_listing_filters(search, listing, fields)["status"] == "PASA"


def test_explicit_different_commune_is_reported_as_detail_evidence():
    fields = extract_listing_fields(_html('<ul><li>Comuna: Talca</li></ul>'))
    assert fields["comuna"] == "Talca"
    assert fields["comuna_source"] == "explicit_attribute"
    assert _commune_validation(
        fields["comuna"], "Talca", "maipu", "region-metropolitana",
        fields["comuna_evidence"]["status"],
    )[0] == "confirmed_other_commune"


def test_address_locality_is_accepted_only_inside_typed_real_estate_address():
    html = _html('''<script type="application/ld+json">
    {"@context":"https://schema.org","@type":"RealEstateListing",
     "address":{"@type":"PostalAddress","addressLocality":"Maipú"}}
    </script>''')
    fields = extract_listing_fields(html)
    assert fields["comuna"] == "Maipú"
    assert fields["comuna_source"] == "structured_real_estate_address"
    assert fields["comuna_evidence"]["status"] == "confirmed_detail"


def test_unrelated_organization_address_locality_is_not_used():
    html = _html('''<script type="application/ld+json">
    {"@type":"Organization","name":"Maipú Inmobiliaria",
     "address":{"@type":"PostalAddress","addressLocality":"Maipú"}}
    </script><div class="description">Casa comercializada en Maipú</div>''')
    fields = extract_listing_fields(html)
    assert fields["comuna"] == ""
    assert fields["comuna_evidence"]["status"] == "pending"


def test_missing_commune_remains_pending_despite_marketing_text():
    html = _html('<div class="seller">Comuna Maipú Inmobiliaria</div>'
                 '<div class="description">Excelente casa ubicada en Maipú.</div>')
    fields = extract_listing_fields(html)
    assert fields["comuna"] == ""
    assert fields["comuna_evidence"]["status"] == "pending"
    assert _commune_validation(
        fields["comuna"], "Maipú", "maipu", "region-metropolitana",
        fields["comuna_evidence"]["status"],
    )[0] == "pending"


def test_conflicting_explicit_and_structured_communes_remain_ambiguous():
    html = _html('''<ul><li>Comuna: Maipú</li></ul>
    <script type="application/ld+json">
    {"@type":"RealEstateListing","address":{"@type":"PostalAddress","addressLocality":"Talca"}}
    </script>''')
    fields = extract_listing_fields(html)
    assert fields["comuna"] == "Maipú"  # explicit field retains precedence
    assert fields["comuna_evidence"]["status"] == "pending_ambiguous"
    assert _commune_validation(
        fields["comuna"], "Maipú", "maipu", "region-metropolitana",
        fields["comuna_evidence"]["status"],
    )[0] == "pending_conflicting_location_evidence"


def test_explicit_and_structured_same_commune_with_region_suffix_agree():
    html = _html('''<ul><li>Comuna: Maipú</li></ul>
    <script type="application/ld+json">
    {"@type":"RealEstateListing","address":{"@type":"PostalAddress","addressLocality":"Maipú, Región Metropolitana"}}
    </script>''')
    fields = extract_listing_fields(html)
    assert fields["comuna"] == "Maipú"
    assert fields["comuna_evidence"]["status"] == "confirmed_detail"


def test_conflict_with_secondary_discovery_location_is_ambiguous():
    assert _commune_validation("Maipú", "Talca", "maipu", "region-metropolitana", "confirmed_detail")[0] == "pending_conflicting_location_evidence"
    assert _commune_validation("Maipú", "Maipú, Región Metropolitana", "maipu", "region-metropolitana", "confirmed_detail")[0] == "confirmed_detail"


def test_microdata_address_locality_requires_real_estate_and_postal_address_types():
    html = _html('''<div itemscope itemtype="https://schema.org/House">
      <div itemprop="address" itemscope itemtype="https://schema.org/PostalAddress">
        <span itemprop="addressLocality">Maipú</span>
      </div></div>''')
    fields = extract_listing_fields(html)
    assert fields["comuna"] == "Maipú"
    assert fields["comuna_source"] == "structured_real_estate_address"


def test_locality_outside_typed_real_estate_scope_is_ignored():
    html = _html('''<div itemscope itemtype="https://schema.org/Organization">
      <div itemprop="address" itemscope itemtype="https://schema.org/PostalAddress">
        <span itemprop="addressLocality">Maipú</span>
      </div></div>''')
    fields = extract_listing_fields(html)
    assert fields["comuna"] == ""
    assert fields["comuna_evidence"]["status"] == "pending"


def test_pending_commune_listing_remains_in_audit_report(monkeypatch, tmp_path):
    search = SearchConfig(
        region="Región Metropolitana", communes=("Maipú",), operation="venta",
        property_type="casa", price_min=3000, price_max=6000,
        price_currency="UF", live=True, output_dir=str(tmp_path), batch_id="commune-pending-test",
    )
    start_url = search.start_urls()[0][0]
    search_html = f'<html><head><link rel="canonical" href="{start_url}"></head><body>search</body></html>'

    def discover(_urls, **kwargs):
        filter_validation = kwargs["page_filter_validator"](start_url, search_html)
        kwargs["report"].update({
            "pages": [{"url": start_url, "status": "complete", "listing_count": 1,
                       "duplicate_count": 0, "filter_validation": filter_validation}],
            "pagination_complete": True, "pagination_cause": "explicit_total_pages_reached",
            "errors": [], "proxy_requests": 1, "request_attempts": 1,
            "successful_downloads": 1, "cache_responses": 0,
        })
        return [{
            "url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678",
            "listing_id": "12345678", "discovery_comuna": "Maipú, Región Metropolitana",
            "source_search_url": start_url,
        }]

    class Store:
        def __init__(self, _config): pass
        def existing_listing_ids(self, _ids): return set()
        def existing_listing_urls(self, _urls): return set()

    monkeypatch.setattr(pilot, "discover_listing_urls", discover)
    monkeypatch.setattr(pilot.ProxyManager, "from_config", lambda _config: ProxyManager(["proxy.test:8080"]))
    monkeypatch.setattr(pilot, "MongoStore", Store)
    monkeypatch.setattr(pilot, "download_html", lambda *_args, **_kwargs: SimpleNamespace(
        html="<!doctype html><html><body>" + ("contenido " * 40) + "</body></html>",
        fetch_source="cache", blocked=False, html_path=tmp_path / "detail.html",
    ))
    monkeypatch.setattr(pilot, "html_path_for_url", lambda *_args, **_kwargs: tmp_path / "missing.html")
    monkeypatch.setattr(pilot, "validate_html", lambda _html: {"status": "OK", "reason": "valid_html"})
    monkeypatch.setattr(pilot, "extract_listing_fields", lambda *_args, **_kwargs: {
        "title": "Casa completa", "description": "Descripción completa",
        "price": "UF 4.000", "price_value": "4000", "price_currency": "UF",
        "comuna": "", "comuna_source": "", "attributes": {},
        "comuna_evidence": {"status": "pending"},
    })
    monkeypatch.setattr(pilot, "_enrich_property_fields", lambda fields, *_args, **_kwargs: fields)
    config = AppConfig(mongo_uri="mongodb://fake", reports_dir=tmp_path, html_dumps_dir=tmp_path / "html")
    result = pilot.run_search(search, config)
    import json
    audit = json.loads(Path(result["records_json"]).read_text(encoding="utf-8"))
    assert len(audit) == 1
    assert audit[0]["filter_validation"]["checks"]["commune"]["status"] == "pendiente"
    assert audit[0]["discovery_comuna"] == "Maipú, Región Metropolitana"
    assert result["filter_matches"] == 0
    assert result["mongo_writes"] == 0
    assert result["deepseek_calls"] == 0
