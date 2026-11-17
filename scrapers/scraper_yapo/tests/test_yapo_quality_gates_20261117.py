"""Regressions for false publisher attribution and owner/broker safety gates."""
from __future__ import annotations

import csv
import json

import pytest

from scrapers.scraper_yapo.classifier_rules import classify_yapo_listing
from scrapers.scraper_yapo.extractor import extract_listing_fields
from scrapers.scraper_yapo.pilot import _save_search_outputs


def _listing_html(publisher_profile: bool = True) -> str:
    own_profile = (
        '<a href="/user/profile/id/12345678">Ver perfil</a>'
        if publisher_profile else ""
    )
    return (
        '<html><body><main>'
        '<section class="related">'
        '<div class="profile"><a href="/user/profile/id/99999999">'
        '<span class="seller-name">RE/MAX Tercero</span>'
        '</a><span class="badge">Profesional</span>'
        '<img alt="Logo RE/MAX Tercero" src="/users/logo/unrelated.jpg"></div>'
        '</section>'
        '<h1>Casa en Maipú</h1>'
        '<div class="d3-property-about__text">Vendo mi propia casa.</div>'
        '<div class="publisher-card"><h3>Publicado por</h3><div>'
        '<h4>Paula Particular</h4>' + own_profile +
        '</div></div>'
        '</main></body></html>'
    )


def test_current_publisher_card_excludes_recommended_seller_profile_and_logo():
    fields = extract_listing_fields(
        _listing_html(),
        source_url="https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/87654321",
    )
    assert fields["publicador_visible"] == "Paula Particular"
    assert fields["contact_name"] == "Paula Particular"
    assert fields["seller_profile_id"] == "12345678"
    assert fields["contact_logo_alt"] == ""
    assert fields["contact_badges_text"] == ""
    assert fields["seller_type"] == ""
    assert fields["publisher_field_provenance"]["publicador_visible"]["relationship"] == "publisher"
    assert classify_yapo_listing(fields)["state"] == "DUEÑO_PROBABLE"


def test_unrelated_recommendation_profile_cannot_fill_missing_publisher_profile():
    fields = extract_listing_fields(
        _listing_html(publisher_profile=False),
        source_url="https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/87654321",
    )
    assert fields["publicador_visible"] == "Paula Particular"
    assert fields["seller_profile_id"] == ""
    assert fields["seller_profile_url"] == ""
    assert fields["seller_type"] == ""


def test_unrecognized_publisher_card_structure_does_not_borrow_recommended_identity():
    html = (
        '<html><body><main>'
        '<div class="profile"><span class="seller-name">RE/MAX Relacionado</span>'
        '<a href="/user/profile/id/99999999">Ver anunciante recomendado</a></div>'
        '<h3>Publicado por</h3>'
        '<h1>Casa en Maipú</h1>'
        '<div class="d3-property-about__text">Vendo mi propia casa.</div>'
        '</main></body></html>'
    )
    fields = extract_listing_fields(
        html,
        source_url="https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/87654321",
    )
    assert fields["publicador_visible"] == ""
    assert fields["seller_profile_id"] == ""
    assert fields["seller_type"] == ""
    assert fields["contact_logo_alt"] == ""


@pytest.mark.parametrize("description,expected_rule", [
    ("Venta de particular. No soy dueño de esta casa.", "OWNER_DECLARATION_WITH_NEGATION"),
    ("Venta de particular. Propiedad heredada de mis padres.", "OWNER_DECLARATION_WITH_REPRESENTATION"),
    ("Venta de particular. Contactar a Carlos Pérez para coordinar.", "OWNER_DECLARATION_CONTACT_IDENTITY_MISMATCH"),
])
def test_private_party_claim_with_conflicting_attribution_remains_uncertain(description, expected_rule):
    fields = {
        "description": description,
        "publicador_visible": "María Soto",
        "seller_profile_id": "12345678",
        "listing_advertiser_type": "Particular",
    }
    result = classify_yapo_listing(fields)
    assert result["state"] == "INCIERTO"
    assert result["requires_review"] is True
    assert expected_rule in {item["rule_id"] for item in result["contradictions"]}


def test_uncontradicted_private_party_claim_is_only_probable():
    result = classify_yapo_listing({"description": "Venta de particular. Documentos al día."})
    assert result["state"] == "DUEÑO_PROBABLE"
    assert result["owner_classification_confirmed"] is False
    assert result["confidence"] is None


def test_report_has_review_and_availability_columns_instead_of_opaque_only(tmp_path):
    row = {
        "listing_id": "12345678",
        "url": "https://www.yapo.cl/ad/12345678",
        "classification": {
            "state": "INCIERTO", "reason": "Revisar quién publica",
            "evidence_strength": "INSUFICIENTE", "requires_review": True,
            "contradictions": [{"rule_id": "OWNER_DECLARATION_WITH_REPRESENTATION"}],
        },
        "filter_status": "PASA",
        "availability_status": "NO_VERIFICADO",
        "assignment_ready": False,
        "exclude_from_assignment": True,
        "assignment_block_reasons": ["yapo_availability_not_confirmed"],
    }
    summary = {"batch_id": "audit-test"}
    _save_search_outputs(summary, [row], tmp_path)
    with (tmp_path / "yapo_search_audit-test_records.csv").open(encoding="utf-8-sig", newline="") as handle:
        exported = next(csv.DictReader(handle))
    assert exported["classification_state"] == "INCIERTO"
    assert exported["classification_requires_review"] == "True"
    assert exported["classification_reason"] == "Revisar quién publica"
    assert exported["availability_status"] == "NO_VERIFICADO"
    assert exported["assignment_ready"] == "False"
    assert json.loads(exported["classification_contradictions"])[0]["rule_id"] == "OWNER_DECLARATION_WITH_REPRESENTATION"
    assert json.loads(exported["assignment_block_reasons"]) == ["yapo_availability_not_confirmed"]
