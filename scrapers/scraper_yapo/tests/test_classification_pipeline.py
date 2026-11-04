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
    classify_yapo_batch,
    classify_yapo_listing,
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


def test_profesional_profile_and_yapo_owner_tag_alone_remain_uncertain():
    result = classify_yapo_listing({
        **_confirmed("Alejandro Silva"),
        "seller_type": "PROFESIONAL",
        "listing_advertiser_type": "Propietario",
        "title": "Casa en venta en Maipú",
        "description": "Casa luminosa, 3 dormitorios y patio. Coordinar visita.",
    })
    assert result["state"] == "INCIERTO"
    assert "LISTING_ADVERTISER_AGENT_TYPE" not in result["classification_rule_ids"]


def test_yapo_listing_agent_type_is_probable_evidence_not_safe_confirmation():
    result = classify_yapo_listing({
        **_confirmed("Guido Parra"),
        "listing_id": "33196456",
        "seller_type": "PROFESIONAL",
        "listing_advertiser_type": "Agente",
        "listing_advertiser_type_source": "yapo_listing_attribute_tipo_anunciante",
        "description": "Comisión corredor 2% más boleta honorarios. Contacto: Guido Parra",
    })
    assert result["state"] == "CORREDOR_PROBABLE"
    assert result["source"] != "confirmed_publisher_company_and_profile"
    assert "LISTING_ADVERTISER_AGENT_TYPE" in result["classification_rule_ids"]
    assert any(item["rule_id"] == "BROKERAGE_FEE_CHARGED" for item in result["broker_evidence"])


def test_direct_owner_claim_conflicts_with_yapo_agent_tag_and_stays_uncertain():
    result = classify_yapo_listing({
        **_confirmed("Andrea Soto"),
        "listing_advertiser_type": "Agente",
        "title": "Vendo mi casa en Maipú",
        "description": "Vendo mi casa directamente. Soy su propietaria.",
    })
    assert result["state"] == "INCIERTO"
    assert any(item["family"] == "portal_listing_attribute" for item in result["broker_evidence"])
    assert any(item["family"] == "owner_first_person" for item in result["owner_evidence"])
    assert result["requires_review"] is True


def test_real_yapo_owner_signoff_with_profile_professional_is_owner_probable():
    result = classify_yapo_listing({
        **_confirmed("Alejandro"),
        "listing_id": "25939035",
        "seller_type": "PROFESIONAL",
        "listing_advertiser_type": "Propietario",
        "description": "Casa amplia, excelente estado. atte su dueño",
    })
    assert result["state"] == "DUEÑO_PROBABLE"
    assert any(item["rule_id"] == "OWNER_SIGNOFF_ATTESTS_OWNERSHIP" for item in result["owner_evidence"])


def test_real_yapo_corredor_prefix_survives_conflicting_owner_badge_as_probable():
    result = classify_yapo_listing({
        **_confirmed("Corredor - Jorge Carreño"),
        "listing_id": "25980296",
        "seller_type": "PROFESIONAL",
        "listing_advertiser_type": "Propietario",
        "description": "Agente inmobiliario vende la casa. Propiedades Carreño.",
    })
    assert result["state"] == "CORREDOR_PROBABLE"
    assert any(item["rule_id"] == "LISTING_ADVERTISER_OWNER_TYPE" for item in result["owner_evidence"])
    assert any(item["rule_id"] == "CONFLICT_OWNER_AND_BROKER_SIGNALS" for item in result["contradictions"])
    assert result["requires_review"] is True


def test_real_yapo_corredora_signature_linked_to_publisher_is_probable():
    result = classify_yapo_listing({
        **_confirmed("Maria Paz Muñoz"),
        "listing_id": "32400607",
        "description": "Mayor información contactar a corredora Maria Paz.",
    })
    assert result["state"] == "CORREDOR_PROBABLE"
    assert "BROKER_DESCRIPTION_EXPLICIT_ROLE_SIGNATURE" in result["classification_rule_ids"]


def test_real_yapo_unknown_company_and_commission_are_probable_with_owner_conflict():
    result = classify_yapo_listing({
        **_confirmed("Muriel Angela Garcia Albornoz"),
        "listing_id": "25899595",
        "seller_type": "PROFESIONAL",
        "listing_advertiser_type": "Propietario",
        "description": (
            "Más comisión del 2% de honorarios por corretaje. Te ayudamos con tu crédito. "
            "Interesados contactar: TRIUNFO PROPIEDADES"
        ),
    })
    assert result["state"] == "CORREDOR_PROBABLE"
    assert any(item["rule_id"] == "BROKER_DESCRIPTION_COMPANY_FEE_SIGNATURE" for item in result["broker_evidence"])
    assert any(item["rule_id"] == "CONFLICT_OWNER_AND_BROKER_SIGNALS" for item in result["contradictions"])
    assert result["requires_review"] is True


def test_fee_free_owner_copy_does_not_create_broker_evidence():
    result = classify_yapo_listing({
        **_confirmed("Claudio Galleguillos"),
        "seller_type": "PROFESIONAL",
        "listing_advertiser_type": "Propietario",
        "title": "Casa en Maipú sin comisión",
        "description": "Trato directo con dueños sin costos de comisión.",
    })
    assert result["state"] == "INCIERTO"
    assert not any(item["rule_id"] == "BROKERAGE_FEE_CHARGED" for item in result["broker_evidence"])


def test_real_profile_broker_evidence_correlates_only_by_exact_yapo_profile_id():
    source = {
        **_confirmed("SAULING CHIONG ABE"),
        "listing_id": "32306422",
        "seller_profile_id": "13736569",
        "listing_advertiser_type": "Agente",
        "description": "Contacto SAULING CHIONG ABE. Corredora de Propiedades.",
    }
    same_profile = {
        **_confirmed("SAULING CHIONG ABE"),
        "listing_id": "33208287",
        "seller_profile_id": "13736569",
        "description": "Casa amplia en Maipú con 4 dormitorios.",
    }
    unrelated = {
        **_confirmed("Otra persona"),
        "listing_id": "33208288",
        "seller_profile_id": "13736570",
        "description": "Casa amplia en Maipú con 4 dormitorios.",
    }
    results = classify_yapo_batch([source, same_profile, unrelated])
    assert results[0]["state"] == "CORREDOR_PROBABLE"
    assert results[1]["state"] == "CORREDOR_PROBABLE"
    assert results[1]["profile_corroboration"]["source_listing_id"] == "32306422"
    assert results[1]["requires_review"] is True
    assert results[2]["state"] == "INCIERTO"


def test_yapo_owner_listing_is_not_reclassified_from_sibling_broker_signal():
    source = {
        **_confirmed("SAULING CHIONG ABE"),
        "listing_id": "32306422",
        "seller_profile_id": "13736569",
        "listing_advertiser_type": "Agente",
        "description": "Contacto SAULING CHIONG ABE. Corredora de Propiedades.",
    }
    owner_tag_sibling = {
        **_confirmed("SAULING CHIONG ABE"),
        "listing_id": "33208287",
        "seller_profile_id": "13736569",
        "listing_advertiser_type": "Propietario",
        "description": "Casa amplia en Maipú con 4 dormitorios.",
    }
    result = classify_yapo_batch([source, owner_tag_sibling])[1]
    assert result["state"] == "INCIERTO"


def test_contact_channel_text_is_not_misread_as_a_different_named_contact():
    for description in (
        "Casa en venta. Contactar via whatsapp.",
        "Casa en venta. Contacto al fono.",
    ):
        result = classify_yapo_listing({
            **_confirmed("Yasna Valdivia"),
            "seller_type": "PROFESIONAL",
            "listing_advertiser_type": "Propietario",
            "description": description,
        })
        assert result["state"] == "INCIERTO"
        assert not any(item["rule_id"] == "OWNER_DECLARATION_CONTACT_IDENTITY_MISMATCH" for item in result["contradictions"])


def test_real_yapo_multiple_owner_claim_with_different_contacts_stays_uncertain():
    result = classify_yapo_listing({
        **_confirmed("Chester Leal Vega"),
        "listing_id": "28052500",
        "seller_type": "PROFESIONAL",
        "listing_advertiser_type": "Propietario",
        "description": "Venden sus propios dueños. Contacto Sra. Alicia Vega. Contacto Don Danilo Leal.",
    })
    assert result["state"] == "INCIERTO"
    assert any(item["rule_id"] == "OWNER_SALE_BY_OWNER" for item in result["owner_evidence"])
    assert any(item["rule_id"] == "OWNER_DECLARATION_CONTACT_IDENTITY_MISMATCH" for item in result["contradictions"])
