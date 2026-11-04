from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scrapers.scraper_yapo import pilot, publisher_profile, run_owner_hunt
from scrapers.scraper_yapo.classifier_rules import classify_yapo_listing
from scrapers.scraper_yapo.config import AppConfig
from scrapers.scraper_yapo.downloader import assess_yapo_availability, validate_html
from scrapers.scraper_yapo.proxy_manager import ProxyManager


def _long_html(content: str) -> str:
    return f"<!doctype html><html><body>{content}{' descripción' * 40}</body></html>"


def test_live_explicit_deactivation_is_distinguished_from_valid_http_200():
    source = _long_html('<main><div role="alert">Este anuncio está desactivado</div></main>')
    result = assess_yapo_availability(
        source, fetch_source="flashproxy", validation_status="OK", status_code=200,
    )
    assert result["availability_status"] == "DESACTIVADO"
    assert result["availability_evidence"] == ["Este anuncio está desactivado"]
    assert result["availability_checked_at"]
    assert validate_html(source)["status"] == "LISTING_REMOVED"


def test_removed_message_in_related_recommendation_is_not_primary_listing_status():
    source = _long_html(
        '<main><article class="primary-listing"><h1>Casa en Maipú</h1></article>'
        '<section class="related-listings"><p>Este anuncio está desactivado</p></section></main>'
    )
    assert validate_html(source)["status"] == "OK"
    result = assess_yapo_availability(
        source, fetch_source="flashproxy", validation_status="OK", status_code=200,
    )
    assert result["availability_status"] == "NO_VERIFICADO"
    assert result["availability_evidence"] == ["no_explicit_primary_listing_status"]


def test_http_200_and_property_word_disponible_do_not_confirm_active():
    source = _long_html('<main><h1>Casa disponible</h1><p>Se vende propiedad en excelente estado.</p></main>')
    result = assess_yapo_availability(
        source, fetch_source="flashproxy", validation_status="OK", status_code=200,
    )
    assert result["availability_status"] == "NO_VERIFICADO"
    assert result["availability_evidence"] == ["no_explicit_primary_listing_status"]


def test_stale_cached_deactivation_is_historical_not_current_availability():
    source = _long_html('<main><div role="alert">Este anuncio está desactivado</div></main>')
    result = assess_yapo_availability(source, fetch_source="cache", validation_status="LISTING_REMOVED")
    assert result["availability_status"] == "NO_VERIFICADO"
    assert result["availability_checked_at"] == ""
    assert "historical_status_message:Este anuncio está desactivado" in result["availability_evidence"]


def test_only_explicit_primary_status_can_confirm_active():
    source = _long_html('<main><div class="availability-status">Este anuncio está activo</div></main>')
    result = assess_yapo_availability(
        source, fetch_source="flashproxy", validation_status="OK", status_code=200,
    )
    assert result["availability_status"] == "ACTIVO"
    assert result["availability_evidence"] == ["Este anuncio está activo"]


def test_sold_status_badge_is_a_deactivated_state():
    source = _long_html('<main><span class="listing-status">Vendido</span></main>')
    result = assess_yapo_availability(
        source, fetch_source="flashproxy", validation_status="OK", status_code=200,
    )
    assert result["availability_status"] == "DESACTIVADO"
    assert result["availability_evidence"] == ["Vendido"]


def test_profile_page_parser_deduplicates_owned_links_and_ignores_related_ads():
    html = _long_html(
        '<main><section class="published-listings">'
        '<article class="ad-card"><a href="/bienes-raices-venta-de-propiedades-casas/casa-maipu/12345678">Casa A</a>'
        '<span class="location">Maipú</span><p>Honorarios de corretaje</p></article>'
        '<article class="ad-card"><a href="/bienes-raices-venta-de-propiedades-casas/casa-maipu/12345678">Casa A duplicada</a></article>'
        '<article class="ad-card"><a href="/bienes-raices-alquiler-de-propiedades-departamentos/depto/23456789">Depto</a></article>'
        '</section><section class="related-listings"><a href="/bienes-raices-venta-de-propiedades-casas/casa/99999999">Recomendado</a></section>'
        '<nav class="pagination"><a rel="next" href="/user/profile/id/13736569?page=2">Siguiente</a></nav></main>'
    )
    result = publisher_profile.parse_publisher_profile_page(
        html,
        seller_profile_id="13736569",
        profile_url="/user/profile/id/13736569",
    )
    assert result["observed_listing_ids"] == ["12345678", "23456789"]
    assert result["observed_listing_count"] == 2
    assert result["observed_communes"] == ["Maipú"]
    assert result["observed_operations"] == ["arriendo", "venta"]
    assert result["observed_property_types"] == ["casa", "departamento"]
    assert result["profile_enumeration_complete"] is False
    assert "honorarios" in result["commercial_text_cues"]


def test_profile_reader_fails_closed_without_explicit_flashproxy(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(publisher_profile, "download_html", lambda *args, **kwargs: called.append(args))
    config = SimpleNamespace(flashproxy_proxy_url="", html_dumps_dir=tmp_path)
    result = publisher_profile.fetch_publisher_profile_history(
        "13736569", "/user/profile/id/13736569", config, batch_id="offline",
    )
    assert result["status"] == "NO_VERIFICADO"
    assert result["reason"] == "flashproxy_not_configured"
    assert result["profile_enumeration_complete"] is False
    assert called == []


def test_profile_reader_uses_only_explicit_flashproxy_endpoint(monkeypatch, tmp_path):
    captured = []
    html = _long_html(
        '<main><section class="published-listings"><article class="ad-card">'
        '<a href="/bienes-raices-venta-de-propiedades-casas/casa/12345678">Casa</a>'
        '</article><nav class="pagination"><a href="?page=1">1</a></nav></section></main>'
    )

    def fake_download(url, _config, manager, **kwargs):
        captured.append((url, list(manager._proxies), kwargs.get("batch_id")))
        return publisher_profile.DownloadResult(
            url, html, 200, "requests_proxy", tmp_path / "profile.html", "OK", "valid_html",
        )

    monkeypatch.setenv("PROXY_URL", "generic-proxy.test:8000")
    monkeypatch.setattr(publisher_profile, "download_html", fake_download)
    config = SimpleNamespace(
        flashproxy_proxy_url="flashproxy.test:9000", yapo_profile_max_pages=2,
        html_dumps_dir=tmp_path,
    )
    result = publisher_profile.fetch_publisher_profile_history(
        "13736569", "/user/profile/id/13736569", config, batch_id="offline",
    )
    assert len(captured) == 1
    assert captured[0][1] == ["http://flashproxy.test:9000"]
    assert captured[0][0] == "https://www.yapo.cl/user/profile/id/13736569"
    assert result["profile_enumeration_complete"] is True
    assert result["observed_listing_ids"] == ["12345678"]


def test_explicit_flashproxy_config_does_not_fall_back_to_generic_proxy(monkeypatch):
    monkeypatch.setenv("PROXY_URL", "generic-proxy.test:8000")
    manager = ProxyManager.from_config(AppConfig(
        proxy_url="other-proxy.test:7000",
        proxy_urls=["second-proxy.test:7001"],
        flashproxy_proxy_url="flashproxy.test:9000",
    ))
    assert manager.provider == "flashproxy"
    assert manager._proxies == ["http://flashproxy.test:9000"]


def test_generic_proxy_response_never_confirms_current_yapo_availability():
    source = _long_html('<main><div role="alert">Este anuncio está desactivado</div></main>')
    result = assess_yapo_availability(
        source, fetch_source="requests_proxy", validation_status="OK", status_code=200,
    )
    assert result["availability_status"] == "NO_VERIFICADO"
    assert result["availability_source"] == "requests_proxy"
    assert "historical_status_message:Este anuncio está desactivado" in result["availability_evidence"]


def test_profile_batch_fetch_is_cached_once_per_exact_profile_id(monkeypatch):
    calls = []

    def fake_fetch(profile_id, profile_url, _config, *, batch_id, max_pages=None):
        calls.append((profile_id, profile_url, batch_id))
        return {
            "status": "OBSERVED", "seller_profile_id": profile_id,
            "observed_listing_count": 3, "observed_listing_ids": ["1", "2", "3"],
            "profile_enumeration_complete": False,
        }

    monkeypatch.setattr(publisher_profile, "fetch_publisher_profile_history", fake_fetch)
    records = [
        {"listing_id": "11111111", "seller_profile_id": "p1", "seller_profile_url": "/user/profile/id/p1"},
        {"listing_id": "22222222", "seller_profile_id": "p1", "seller_profile_url": "/user/profile/id/p1"},
    ]
    summary = publisher_profile.enrich_batch_publisher_profiles(
        records, SimpleNamespace(yapo_profile_max_profiles=12, flashproxy_proxy_url="set"), batch_id="batch",
    )
    assert len(calls) == 1
    assert summary["profile_requests_attempted"] == 1
    assert records[0]["publisher_profile_context"]["observed_listing_count"] == 3
    assert records[1]["publisher_profile_context"]["seller_profile_id"] == "p1"


def test_batch_search_integrates_exact_profile_classifier_and_availability_hold(monkeypatch):
    calls = []

    def fake_batch(records):
        calls.append(records)
        return [
            {"state": "CORREDOR_PROBABLE", "evidence": ["same_profile"], "requires_review": True},
            {"state": "INCIERTO", "evidence": [], "requires_review": True},
        ]

    monkeypatch.setattr(pilot, "classify_yapo_batch", fake_batch)
    records = [
        {"listing_id": "11111111", "seller_profile_id": "p1", "html_validation_status": "OK",
         "availability_status": "NO_VERIFICADO", "details": {"description": "texto completo"}, "extraction_issues": []},
        {"listing_id": "22222222", "seller_profile_id": "p1", "html_validation_status": "OK",
         "availability_status": "ACTIVO", "details": {"description": "texto completo"}, "extraction_issues": []},
    ]
    pilot._reclassify_batch_by_exact_profile(records)
    assert len(calls) == 1
    assert [item["listing_id"] for item in calls[0]] == ["11111111", "22222222"]
    assert records[0]["classification"]["state"] == "CORREDOR_PROBABLE"
    assert records[0]["classification"]["manual_review_required"] is True
    assert records[0]["assignment_ready"] is False
    assert records[1]["classification"]["state"] == "INCIERTO"


def test_saved_batch_runner_reclassifies_profiles_before_persistence(monkeypatch):
    classified_batch = []
    readiness_calls = []

    class FakeProxyManager:
        @classmethod
        def from_config(cls, _config):
            return cls()

    def fake_process(record, *_args, **_kwargs):
        return {
            **record,
            "listing_id": str(record["listing_id"]),
            "seller_profile_id": "profile-1",
            "seller_profile_url": "/user/profile/id/13736569",
            "classification": {"state": "INCIERTO"},
            "availability_status": "NO_VERIFICADO",
            "scrape_stage": "processed",
        }

    def fake_classify(records):
        classified_batch.extend(records)
        return [{"state": "CORREDOR_PROBABLE", "evidence": ["same_profile"]} for _ in records]

    monkeypatch.setattr(run_owner_hunt, "ProxyManager", FakeProxyManager)
    monkeypatch.setattr(run_owner_hunt, "_process_single_record", fake_process)
    monkeypatch.setattr(run_owner_hunt, "enrich_batch_publisher_profiles", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(run_owner_hunt, "classify_yapo_batch", fake_classify)
    monkeypatch.setattr(
        "captacion_assignment_eligibility.mark_assignment_readiness",
        lambda document: readiness_calls.append(document),
    )
    result = run_owner_hunt._process_batch(
        [{"listing_id": "12345678", "url": "https://www.yapo.cl/listing/12345678"},
         {"listing_id": "12345679", "url": "https://www.yapo.cl/listing/12345679"}],
        SimpleNamespace(), no_llm=True, write_db=False,
    )
    assert len(classified_batch) == 2
    assert len(readiness_calls) == 2
    assert all(item["classification"]["state"] == "CORREDOR_PROBABLE" for item in result)
    assert all(item["classification"]["manual_review_required"] is True for item in result)


def test_real_id_28073704_unattributed_brokerage_fee_is_probable_not_safe():
    result = classify_yapo_listing({
        "listing_id": "28073704",
        "publicador_visible": "hector",
        "contact_name": "hector",
        "seller_type": "PROFESIONAL",
        "listing_advertiser_type": "Propietario",
        "title": "Extraordinaria y Hermosa casa de 3 pisos en Maipu",
        "description": "Se debe considerar el 2% de honorarios de Corretaje.",
    })
    assert result["state"] == "CORREDOR_PROBABLE"
    assert result["evidence_strength"] == "MEDIA"
    fee = next(item for item in result["broker_evidence"] if item.get("rule_id") == "BROKERAGE_FEE_CHARGED")
    assert fee["attribution"] == "unattributed_charge"
    assert fee["attribution_rule_id"] == "BROKERAGE_FEE_PUBLISHER_CHARGES_OR_UNATTRIBUTED"
    assert any(item["rule_id"] == "CONFLICT_OWNER_AND_BROKER_SIGNALS" for item in result["contradictions"])
    assert result["requires_review"] is True


def test_third_party_fee_does_not_make_the_publisher_a_broker():
    result = classify_yapo_listing({
        "listing_id": "19999999",
        "publicador_visible": "Héctor Soto",
        "listing_advertiser_type": "Propietario",
        "title": "Vendo mi casa",
        "description": "Vendo mi casa. El comprador pagará al corredor externo 2% de honorarios de corretaje.",
    })
    assert result["state"] == "INCIERTO"
    assert not result["state"].startswith("CORREDOR")
    fee = next(item for item in result["broker_evidence"] if item.get("rule_id") == "BROKERAGE_FEE_CHARGED")
    assert fee["attribution"] == "third_party_charges"
    assert fee["attribution_rule_id"] == "BROKERAGE_FEE_THIRD_PARTY_NOT_PUBLISHER"


def test_weak_owner_cues_only_raise_review_priority():
    result = classify_yapo_listing({
        "listing_id": "19999998",
        "publicador_visible": "Héctor Soto",
        "seller_profile_id": "13730000",
        "publisher_profile_context": {
            "distinct_listing_count": 1,
            "profile_enumeration_complete": False,
        },
        "title": "Casa en venta",
        "description": "Mi casa. Consulte.",
        "images": ["https://example.test/photo.jpg"],
    })
    assert result["state"] == "INCIERTO"
    signal_ids = {item["signal_id"] for item in result["owner_review_priority_signals"]}
    assert {"SHORT_DESCRIPTION", "FEW_LISTING_PHOTOS", "FIRST_PERSON_PROPERTY_REFERENCE"} <= signal_ids
    assert all(item["classification_effect"] is False for item in result["owner_review_priority_signals"])
    assert "FEW_DISTINCT_PROFILE_PROPERTIES" not in signal_ids
