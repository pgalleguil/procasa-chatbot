from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from scrapers.scraper_yapo import classifier_rules as rules
from scrapers.scraper_yapo.extractor import extract_listing_fields


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


def test_professional_account_overrides_direct_deal_title():
    fields = extract_listing_fields(_publisher_html(
        "<span class='seller-name'>Agencia Casa</span>"
        "<span class='seller-type'>Profesional</span><span class='badge'>Cuenta profesional</span>"
    ))
    fields["title"] = "Trato directo, sin comisión"
    result = rules.classify_obvious_broker(fields)
    assert result["state"] == "CORREDOR_SEGURO"
    assert result["source"] == "structural_rules"


def test_inmobiliaria_in_description_does_not_identify_advertiser():
    fields = extract_listing_fields(_publisher_html(
        "<span class='seller-name'>María Pérez</span>",
        "<div class='description'>Se ubica frente a una inmobiliaria local.</div>",
    ))
    assert rules.classify_obvious_broker(fields) is None


def test_repeated_hard_term_does_not_become_safe_broker():
    result = rules.classify_obvious_broker(_confirmed(contact_name="Corredor de propiedades, corredor de propiedades"))
    assert result["state"] == "CORREDOR_PROBABLE"


@pytest.mark.parametrize("brand", ["RE/MAX", "RE MAX - CENTRAL", "re/max central"])
def test_verified_remax_variants_match_equivalently(brand):
    result = rules.classify_obvious_broker(_confirmed(contact_logo_alt=brand))
    assert result["state"] == "CORREDOR_SEGURO"


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


def test_identity_rules_never_upgrade_missing_evidence_to_owner():
    result = rules.classify_obvious_broker({"title": "Casa trato directo", "description": "Sin comisión"})
    assert result is None
