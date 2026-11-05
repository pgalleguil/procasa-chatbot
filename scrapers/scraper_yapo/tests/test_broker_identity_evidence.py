from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from scrapers.scraper_yapo import classifier_rules as rules
from scrapers.scraper_yapo.extractor import extract_listing_fields
from scrapers.scraper_yapo.reclassify_saved_pilot import _with_saved_publisher_provenance


def _confirmed(**fields):
    out = dict(fields)
    out["publisher_field_provenance"] = {
        key: {"status": "confirmed", "source": "test_publisher_block", "relationship": "publisher"}
        for key in fields
    }
    return out


def _publisher_html(inner: str, outside: str = "") -> str:
    return f"<html><body>{outside}<div class='contact_info'>{inner}</div></body></html>"


def test_remax_logo_in_publisher_block_is_safe_broker():
    fields = extract_listing_fields(_publisher_html(
        "<div class='contact_logo'><img alt='RE MAX - CENTRAL' src='/users/logo/17462647.png'></div>"
        "<span class='seller-name'>Contacto Particular</span>"
    ))
    result = rules.classify_obvious_broker(fields)
    assert fields["contact_logo_alt"] == "RE MAX - CENTRAL"
    assert fields["publisher_field_provenance"]["contact_logo_alt"]["relationship"] == "publisher"
    assert result["state"] == "CORREDOR_SEGURO"


def test_broker_brand_matching_ignores_case_accents_and_extra_letter_spacing():
    fields = _confirmed(contact_name="e N G e L  V O L K E R S")
    result = rules.classify_obvious_broker(fields)
    assert result["state"] == "CORREDOR_SEGURO"
    assert any("Engel & Völkers" in item for item in result["evidence"])


def test_new_national_catalog_matches_visan_with_mixed_case_and_inserted_spaces():
    fields = _confirmed(contact_name="v I s A n   p R o P i E d A d E s")
    result = rules.classify_obvious_broker(fields)
    assert result["state"] == "CORREDOR_SEGURO"
    assert any("VISAN Propiedades" in item for item in result["evidence"])


def test_prourbe_logo_in_publisher_block_is_safe_broker():
    fields = extract_listing_fields(_publisher_html(
        "<div class='contact_logo'><img alt='PROurbe' src='/users/logo/17462648.png'></div>"
    ))
    assert rules.classify_obvious_broker(fields)["state"] == "CORREDOR_SEGURO"


def test_remax_in_description_alone_is_not_safe_broker():
    fields = extract_listing_fields(_publisher_html(
        "<span class='seller-name'>María Pérez</span>",
        "<div class='description'>La propiedad está cerca de RE/MAX Central.</div>",
    ))
    assert rules.classify_obvious_broker(fields) is None


def test_logo_alt_on_unrelated_property_photo_is_not_seller_logo():
    fields = extract_listing_fields(_publisher_html(
        "<span class='seller-name'>María Pérez</span>",
        "<img alt='RE/MAX CENTRAL' src='/images/property-photo.jpg'>",
    ))
    assert fields["contact_logo_alt"] == ""
    assert rules.classify_obvious_broker(fields) is None


def test_jsonld_property_name_is_not_seller_identity():
    html = """<script type='application/ld+json'>
    {"@type":"RealEstateListing","name":"RE/MAX Central","description":"house"}
    </script><h1>Casa</h1>"""
    fields = extract_listing_fields(html)
    assert fields["seller_jsonld_name"] == ""
    assert rules.classify_obvious_broker(fields) is None


def test_jsonld_explicit_seller_relation_is_confirmed_identity():
    html = """<script type='application/ld+json'>
    {"@type":"RealEstateListing","name":"Casa RE/MAX","seller":{"@type":"Organization","name":"RE/MAX Central"}}
    </script>"""
    fields = extract_listing_fields(html)
    assert fields["seller_jsonld_name"] == "RE/MAX Central"
    assert fields["publisher_field_provenance"]["seller_jsonld_name"]["relationship"] == "publisher"
    assert rules.classify_obvious_broker(fields)["state"] == "CORREDOR_SEGURO"


def test_personal_name_without_commercial_signals_is_not_safe():
    assert rules.classify_obvious_broker(_confirmed(contact_name="María Pérez")) is None


def test_professional_account_and_personal_name_do_not_override_direct_deal_title():
    fields = extract_listing_fields(_publisher_html(
        "<span class='seller-name'>Agencia Casa</span>"
        "<span class='seller-type'>Profesional</span><span class='badge'>Cuenta profesional</span>"
    ))
    fields["title"] = "Trato directo, sin comisión"
    result = rules.classify_obvious_broker(fields)
    assert result is None


def test_single_professional_badge_is_not_broker_evidence():
    fields = extract_listing_fields(_publisher_html(
        "<span class='badge'>Cuenta profesional</span>"
    ))
    assert fields["seller_type"] == "PROFESIONAL"
    result = rules.classify_obvious_broker(fields)
    assert result is None


def test_single_explicit_professional_type_is_not_broker_evidence():
    fields = extract_listing_fields(_publisher_html(
        "<span class='seller-type'>Profesional</span>"
    ))
    result = rules.classify_obvious_broker(fields)
    assert result is None


def test_profile_label_and_badge_are_one_structural_signal_not_broker_proof():
    fields = extract_listing_fields(_publisher_html(
        "<span class='seller-type'>Profesional</span><span class='badge'>Cuenta profesional</span>"
    ))
    result = rules.classify_obvious_broker(fields)
    assert result is None


def test_professional_badge_outside_publisher_is_not_evidence():
    fields = extract_listing_fields(_publisher_html(
        "<span class='seller-name'>María Pérez</span>",
        "<span class='badge'>Cuenta profesional</span>",
    ))
    assert rules.classify_obvious_broker(fields) is None


def test_inmobiliaria_in_description_does_not_identify_advertiser():
    fields = extract_listing_fields(_publisher_html(
        "<span class='seller-name'>María Pérez</span>",
        "<div class='description'>Se ubica frente a una inmobiliaria local.</div>",
    ))
    assert rules.classify_obvious_broker(fields) is None


def test_repeated_hard_term_does_not_become_safe_broker():
    result = rules.classify_obvious_broker(_confirmed(contact_name="Corredor de propiedades, corredor de propiedades"))
    assert result["state"] == "CORREDOR_PROBABLE"


@pytest.mark.parametrize("brand,matched", [
    ("RE/MAX", "RE/MAX"),
    ("RE MAX", "RE/MAX"),
    ("REMAX", "REMAX"),
    ("REMAX CENTRAL", "REMAX"),
    ("ProUrbe", "ProUrbe"),
    ("PRO URBE", "PRO URBE"),
    ("Engel & Völkers", "Engel & Völkers"),
    ("Engel Volkers", "Engel Volkers"),
])
def test_verified_brand_variants_match_equivalently_and_record_alias(brand, matched):
    result = rules.classify_obvious_broker(_confirmed(contact_logo_alt=brand))
    assert result["state"] == "CORREDOR_SEGURO"
    assert any(f"matched_variant:{matched}" in item for item in result["evidence"])


@pytest.mark.parametrize("name", ["XREMAX", "Remaximo", "PROurbeX", "PROURBEX"])
def test_partial_brand_inside_different_name_does_not_match(name):
    assert rules.classify_obvious_broker(_confirmed(contact_name=name)) is None


@pytest.mark.parametrize("bad_catalog", ["{\"brands\": []}", "not-json", " "])
def test_empty_or_illegible_catalog_fails_explicitly(tmp_path, monkeypatch, bad_catalog):
    data = tmp_path / "data"
    data.mkdir()
    for filename in ("hard_broker_terms.json", "company_shape_terms.json"):
        source = ROOT / "data" / filename
        (data / filename).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (data / "known_broker_brands.json").write_text(bad_catalog, encoding="utf-8")
    monkeypatch.setattr(rules, "DATA_DIR", data)
    rules.load_rule_sets.cache_clear()
    with pytest.raises(RuntimeError, match="known_broker_brands"):
        rules.classify_obvious_broker(_confirmed(contact_name="RE/MAX"))
    rules.load_rule_sets.cache_clear()


def test_profile_volume_without_identity_does_not_prove_broker():
    fields = _confirmed(contact_name="María Pérez")
    fields["publisher_profile_context"] = {"linked_publications": 180, "confirmed_broker_count": 0}
    assert rules.classify_obvious_broker(fields) is None


def test_correlated_profile_with_confirmed_commercial_identity_stays_safe():
    fields = {"publisher_profile_context": {
        "commercial_identity_confirmed": True,
        "confirmed_broker_count": 1,
        "evidence": ["same_profile_confirmed_broker"],
    }}
    result = rules.classify_obvious_broker(fields)
    assert result["state"] == "CORREDOR_SEGURO"
    assert result["source"] == "profile_correlation"


def test_neutral_direct_deal_and_no_commission_do_not_upgrade_missing_evidence_to_owner():
    extracted = {"title": "Casa trato directo", "description": "Sin comisión"}
    assert rules.classify_obvious_broker(extracted) is None
    result = rules.classify_with_rules(extracted)
    assert result["state"] == "INCIERTO"
    assert result["requires_review"] is True
    assert result["owner_evidence"] == []
    assert result["owner_classification_confirmed"] is False


@pytest.mark.parametrize("name", [
    "Gonzalo Sánchez", "Cami San Martín Hellberg", "SANDRA ABURTO GUTIERREZ",
    "Oscar salinas", "Renato Alviz Santander", "katherinne Saavedra", "Boris Avila",
    "SAULING CHIONG ABE", "SAULING CHIONG ABE", "Patricio Javier Guiñez Sandoval",
    "fernando Salazar", "katherinne Saavedra", "sergio sanhueza",
])
def test_sa_false_positive_does_not_match_inside_personal_name(name):
    fields = _confirmed(publicador_visible=name)
    assert rules.company_shape_in_publisher_fields(fields)["company_like_evidence"] == []
    result = rules.classify_yapo_listing(fields)
    assert result["state"] == "INCIERTO"
    assert not any("s.a" in str(item).lower() for item in result.get("evidence", []))


def test_saved_pilot_values_are_not_promoted_to_confirmed_provenance():
    fields = _with_saved_publisher_provenance({
        "publicador_visible": "Focus Propiedades", "logo": "Foto de perfil de Focus Propiedades",
        "tipo_anunciante": "Empresa",
    })
    assert not any(item.get("status") == "confirmed" for item in fields["publisher_field_provenance"].values())
    assert fields["unverified_publisher_fields"]["publicador_visible"] == "Focus Propiedades"
    fields["publisher_profile_context"] = {"distinct_listing_count": 2}
    assert rules.classify_yapo_listing(fields)["state"] == "CORREDOR_PROBABLE"


@pytest.mark.parametrize("name,description", [
    ("Mery Reyes Propiedades", "Contáctenos para coordinar una visita."),
    ("Focus Propiedades", "Agenda tu visita para conocer más detalles."),
])
def test_repeated_commercial_publisher_gets_probable_without_catalog_promotion(name, description):
    fields = _with_saved_publisher_provenance({
        "publicador_visible": name, "logo": f"Foto de perfil de {name}",
        "description": description, "title": "Casa en venta en Maipú",
    })
    fields["publisher_profile_context"] = {"distinct_listing_count": 2}
    result = rules.classify_yapo_listing(fields)
    assert result["state"] == "CORREDOR_PROBABLE"
    assert "repeat_listings:2" in " ".join(result["evidence"])


def test_repeated_hyc_associados_profile_is_probable_with_publisher_logo_and_cta():
    name = "HyC Asociados"
    fields = _with_saved_publisher_provenance({
        "publicador_visible": name, "logo": f"Foto de perfil de {name}",
        "description": "Las superficies fueron proporcionadas por su dueño.",
        "title": "Casa en venta en Maipú",
    })
    fields["publisher_profile_context"] = {"distinct_listing_count": 2}
    result = rules.classify_yapo_listing(fields)
    assert result["state"] == "CORREDOR_PROBABLE"
    assert "repeated_associates" in " ".join(result["evidence"])


def test_corredor_jorge_carreno_unverified_publisher_is_probable():
    fields = _with_saved_publisher_provenance({"publicador_visible": "Corredor - Jorge Carreño"})
    result = rules.classify_obvious_broker(fields)
    assert result["state"] == "CORREDOR_PROBABLE"
    assert "unverified_saved_identity" in " ".join(result["evidence"])
