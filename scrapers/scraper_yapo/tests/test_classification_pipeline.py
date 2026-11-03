import json
from scrapers.scraper_yapo import classification_pipeline as pipeline

from scrapers.scraper_yapo.classification_pipeline import (
    classify_with_routing,
    content_fingerprint,
    get_cached_deepseek_result,
    promote_verified_business_candidate,
    record_unknown_business_candidate,
    store_deepseek_result,
)
from scrapers.scraper_yapo.classifier_rules import (
    _phrase_matches,
    classify_obvious_broker,
    known_broker_brand_in_strong_fields,
)


def _confirmed(value, field="publicador_visible"):
    return {
        field: value,
        "publisher_field_provenance": {
            field: {"status": "confirmed", "relationship": "publisher", "source": "listing"}
        },
    }


def test_full_description_signature_excludes_visan_without_ai():
    result = classify_with_routing({
        **_confirmed("Hugo Aguila"),
        "listing_id": "32200413",
        "description": "VISAN Propiedades actúa con el mayor profesionalismo y ética. Así Ud. puede estar tranquilo.",
    })
    assert result["state"] == "CORREDOR_SEGURO"
    assert result["classification_route"] == "deterministic_only"
    assert result["deepseek_call_executed"] is False
    assert any("visan propiedades" in item.lower() for item in result["evidence"])


def test_unknown_business_stays_probable_and_is_recorded_pending_review(tmp_path):
    extracted = {
        **_confirmed("Inversiones Monte Alto Propiedades"),
        "listing_id": "12345678",
        "url": "https://yapo.cl/example/12345678",
        "description": "Ofrecemos servicios inmobiliarios y cobramos comisión por corretaje.",
    }
    result = classify_with_routing(extracted)
    assert result["state"] == "CORREDOR_PROBABLE"
    assert result["source"] == "unknown_business_context"
    registry = tmp_path / "unknown.json"
    candidate = record_unknown_business_candidate(extracted, result, path=registry)
    assert candidate["status"] == "pending_manual_verification"
    assert json.loads(registry.read_text(encoding="utf-8"))[0]["listing_id"] == "12345678"


def test_normalized_identity_not_false_matched_as_legal_suffix():
    result = classify_with_routing({
        **_confirmed("Sandra"),
        "description": "S.A. no corresponde a este aviso. Trato directo con propietario.",
    })
    assert result["source"] != "unknown_business_context"
    assert result["classification_route"] == "deepseek_candidate"


def test_ambiguous_route_is_simulation_and_cache_uses_full_description(tmp_path):
    extracted = {"listing_id": "1", "title": "Casa en venta", "description": "Trato directo."}
    first_hash = content_fingerprint(extracted)
    assert get_cached_deepseek_result(extracted, tmp_path / "cache.jsonl") is None
    store_deepseek_result(
        extracted,
        {"state": "INCIERTO", "evidence": []},
        input_tokens=20,
        output_tokens=8,
        estimated_cost_usd=0,
        path=tmp_path / "cache.jsonl",
    )
    assert get_cached_deepseek_result(extracted, tmp_path / "cache.jsonl")["result"]["state"] == "INCIERTO"
    assert content_fingerprint({**extracted, "description": "Trato directo. Comisión 2%."}) != first_hash
    result = classify_with_routing(extracted, simulate_deepseek=True, max_calls=0)
    assert result["classification_route"] == "deepseek_candidate"
    assert result["deepseek_simulation_only"] is True
    assert result["deepseek_call_executed"] is False


def test_budget_blocks_paid_route_and_cached_decision_skips_budget(tmp_path, monkeypatch):
    extracted = {"listing_id": "budget-1", "title": "Casa", "description": "Texto ambiguo."}
    monkeypatch.setattr(pipeline, "DEEPSEEK_CACHE_PATH", tmp_path / "cache.jsonl")
    blocked = classify_with_routing(extracted, max_calls=2, max_budget_usd=0, cache_path=tmp_path / "cache.jsonl")
    assert blocked["classification_route"] == "deepseek_candidate"
    assert blocked["deepseek_budget_exhausted"] is True
    assert blocked["deepseek_call_executed"] is False

    store_deepseek_result(
        extracted, {"state": "INCIERTO"}, input_tokens=3, output_tokens=3,
        estimated_cost_usd=0, path=tmp_path / "cache.jsonl",
    )
    cached = classify_with_routing(extracted, max_calls=0, max_budget_usd=0, cache_path=tmp_path / "cache.jsonl")
    assert cached["classification_route"] == "deepseek_cached"
    assert cached["deepseek_cache_hit"] is True
    assert cached["deepseek_budget_exhausted"] is False


def test_catalog_matching_tolerates_case_accents_punctuation_and_joined_words():
    assert _phrase_matches("FOCOLARE", "Focolare Propiedades") is False
    assert _phrase_matches("FocolarePropiedades", "Focolare Propiedades")
    assert _phrase_matches("RE MAX", "RE/MAX")
    assert _phrase_matches("FocusPropiedades", "Focus Propiedades")


def test_company_suffixes_do_not_match_inside_personal_names():
    for name in ("Sandra", "Sauling", "Espacios"):
        assert classify_obvious_broker(_confirmed(name)) is None
    assert known_broker_brand_in_strong_fields(_confirmed("Inversiones Nueva Identidad")) is None


def test_properties_name_without_intermediation_is_only_candidate_not_exclusion():
    result = classify_with_routing({
        **_confirmed("Propiedades Galvarino"),
        "listing_id": "98765432",
        "description": "Casa cómoda para familia, contáctanos para coordinar visita.",
    })
    assert result["state"] == "INCIERTO"
    assert result["classification_route"] == "deepseek_candidate"
    assert result["contextual_analysis"]["unknown_business_identity_evidence"]


def test_candidate_requires_explicit_verification_before_catalog_promotion(tmp_path):
    registry = tmp_path / "unknown.json"
    catalog = tmp_path / "brands.json"
    catalog.write_text(json.dumps({"brands": [], "brand_aliases": {}}), encoding="utf-8")
    extracted = {
        **_confirmed("Corredora Nueva Identidad"),
        "listing_id": "11112222",
        "description": "Promovemos servicios inmobiliarios con honorarios de corretaje.",
    }
    result = classify_with_routing(extracted)
    record_unknown_business_candidate(extracted, result, path=registry)
    try:
        promote_verified_business_candidate(
            "corredora nueva identidad", listing_id="11112222", verified=False,
            verification_source="", registry_path=registry, catalog_path=catalog,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("No debe promoverse una identidad sin verificación")
    added = promote_verified_business_candidate(
        "corredora nueva identidad", listing_id="11112222", verified=True,
        verification_source="revisión manual", registry_path=registry, catalog_path=catalog,
    )
    assert added == "added_to_catalog"
    assert "Corredora Nueva Identidad" in json.loads(catalog.read_text(encoding="utf-8"))["brands"]
    assert json.loads(registry.read_text(encoding="utf-8"))[0]["status"] == "confirmed_broker"
