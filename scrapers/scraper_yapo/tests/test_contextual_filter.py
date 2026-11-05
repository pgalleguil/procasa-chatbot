from __future__ import annotations

from scrapers.scraper_yapo.classifier_rules import (
    analyze_contextual_signals,
    classify_yapo_listing,
)


def test_explicit_self_identification_is_probable_not_safe():
    result = classify_yapo_listing({"title": "Soy corredor de propiedades y ofrezco esta casa"})
    assert result["state"] == "CORREDOR_PROBABLE"
    assert result["source"] == "contextual_rules"


def test_third_party_reference_does_not_identify_publisher_as_broker():
    result = classify_yapo_listing({"title": "Se acepta corredor externo para esta venta"})
    assert result["state"] == "INCIERTO"
    assert result["contextual_analysis"]["broker_third_party_mentions"]


def test_company_signature_in_full_description_is_safe_broker_even_with_html_breaks():
    result = classify_yapo_listing({
        "publicador_visible": "Hugo Aguila",
        "description": (
            "Estamos disponibles para asesorarlo en cualquier operación inmobiliaria."
            "<br/>VISAN Propiedades actúa con el mayor profesionalismo y ética. "
            "Así Ud. puede estar tranquilo y recibir un servicio de calidad."
        ),
    })
    assert result["state"] == "CORREDOR_SEGURO"
    assert result["source"] == "description_business_signature"
    assert any("visan propiedades actua" in item for item in result["evidence"])


def test_real_estate_consultancy_offer_in_description_is_safe_broker():
    result = classify_yapo_listing({
        "description": "Consultora Inmobiliaria Ofrece para la Venta: Región Metropolitana.",
    })
    assert result["state"] == "CORREDOR_SEGURO"
    assert result["source"] == "description_business_signature"


def test_known_brand_in_description_is_case_and_spacing_insensitive():
    result = classify_yapo_listing({
        "description": "rE  mAx Chile OfReCe servicios de corretaje y asesoría inmobiliaria.",
    })
    assert result["state"] == "CORREDOR_SEGURO"
    assert any("description_known_brand_signature:RE/MAX" in item for item in result["evidence"])


def test_later_third_party_agency_mention_is_not_a_business_signature():
    result = classify_yapo_listing({
        "description": "La casa está cerca de la consultora inmobiliaria, que ofrece asesoría al barrio.",
    })
    assert result["state"] == "INCIERTO"


def test_negated_broker_reference_does_not_create_broker_classification():
    result = classify_yapo_listing({"description": "No soy corredor de propiedades. Trato directo con el dueño."})
    assert result["state"] == "INCIERTO"
    assert result["contextual_analysis"]["broker_negations"]
    assert "trato directo" in result["owner_signal_candidates"]
    assert result["contextual_analysis"]["owner_classification_confirmed"] is False


def test_explicit_possible_owner_signal_stays_probable_not_confirmed():
    result = classify_yapo_listing({"title": "Dueño vende, sin comisión"})
    assert result["state"] == "DUEÑO_PROBABLE"
    assert result["owner_signal_candidates"]
    assert result["owner_classification_confirmed"] is False


def test_direct_deal_with_commercial_contradiction_is_flagged():
    result = classify_yapo_listing({"description": "Trato directo. Se cobra comisión de corretaje."})
    assert result["state"] == "INCIERTO"
    assert result["direct_deal_with_commercial_contradictions"] is True
    assert result["contextual_analysis"]["trato_directo_with_commercial_contradictions"] is True


def test_confirmed_identity_first_filter_keeps_broker_precedence_and_flags_conflict():
    extracted = {
        "contact_logo_alt": "RE/MAX Central",
        "title": "Trato directo, sin comisión",
        "publisher_field_provenance": {
            "contact_logo_alt": {"status": "confirmed", "source": "publisher_logo", "relationship": "publisher"},
        },
    }
    result = classify_yapo_listing(extracted)
    assert result["state"] == "CORREDOR_SEGURO"
    assert result["contextual_analysis"]["identity_and_owner_signal_conflict"] is True


def test_blocked_html_is_not_given_a_commercial_state():
    assert classify_yapo_listing({"html_validation_status": "BLOCKED", "title": "dueño vende"}) is None


def test_contextual_analysis_uses_only_title_and_description():
    extracted = {"title": "Casa", "seller_text": "Soy corredor de propiedades"}
    result = classify_yapo_listing(extracted)
    assert result["state"] == "INCIERTO"
    assert analyze_contextual_signals(extracted)["broker_self_declarations"] == []


def test_pilot_and_owner_hunt_share_the_same_deterministic_function(monkeypatch):
    from scrapers.scraper_yapo import pilot, run_owner_hunt

    calls = []
    def classifier(record):
        calls.append(record)
        return {"state": "INCIERTO", "source": "test"}
    monkeypatch.setattr(pilot, "classify_yapo_listing", classifier)
    monkeypatch.setattr(run_owner_hunt, "classify_yapo_listing", classifier)
    fields = {"title": "Casa", "html_validation_status": "OK"}
    assert pilot.classify_yapo_listing(fields)["state"] == "INCIERTO"
    assert run_owner_hunt._finalize_local_classification(fields)["state"] == "INCIERTO"
    assert calls == [fields, fields]
