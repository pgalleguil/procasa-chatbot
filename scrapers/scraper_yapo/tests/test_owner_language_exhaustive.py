from __future__ import annotations

import pytest

from scrapers.scraper_yapo.classifier_rules import analyze_owner_language, classify_yapo_listing
from scrapers.scraper_yapo.classification_pipeline import classify_with_routing
from scrapers.scraper_yapo.extractor import extract_listing_fields
from scrapers.scraper_yapo.reclassify_saved_pilot import _with_saved_publisher_provenance


@pytest.mark.parametrize("text", [
    "Vende su dueño", "Vende su propio dueño", "Vende su propietario", "Vende el dueño",
    "Vende directamente el dueño", "Vende directamente su propietario", "Vende su legítimo propietario",
    "Venta por dueño", "Venta por su dueño", "Venta por el dueño", "Venta por propietario",
    "Venta por su propietario", "Venta por el propietario", "Venta por sus propietarios",
    "Venta directa del propietario", "Venta directa por propietario", "Propiedad vendida por su dueño",
    "Propiedad en venta por su propietario", "Casa en venta por sus dueños", "Se vende por dueño",
    "Se vende por propietario", "Se vende por sus propietarios", "Se vende directamente por su dueño",
    "El dueño vende", "Dueño vende", "Propietario vende", "Dueños venden", "Propietarios venden",
    "Dueño directo", "Propietario directo", "Vta directa dueño", "Vta particular",
    "Venta de particular", "Venta por particular", "Vende particular", "Particular vende",
    "Vende directamente particular", "Venta particular sin intermediarios", "Soy el dueño",
    "Soy dueño de la propiedad", "Soy propietario de esta casa", "Soy el titular de la propiedad",
    "Vendo mi casa", "Vendo mi propiedad", "Vendo mi departamento", "Vendo mi vivienda",
    "Vendo mi inmueble", "Vendo mi hogar", "Vendo mi casa directamente", "Estoy vendiendo mi casa",
    "Estamos vendiendo nuestra casa", "Vendemos nuestra propiedad", "Vendemos nuestra vivienda",
    "Pongo en venta mi casa", "Pongo a la venta mi propiedad", "Ofrezco mi propia casa en venta",
    "Mi casa está en venta", "Nuestra casa está en venta", "Me encuentro vendiendo mi vivienda",
    "Soy particular", "DUEÑO VENDE SIN CORREDOR", "VENDO X SER DUEÑO", "VTA DIRECTA DUEÑO",
    "TRATO DIR. DUEÑO", "VENDO MI CASA SIN CORREDORA", "VENTA DIRECTA PROPIETARIO", "DUENO DIRECTO",
])
def test_owner_grammar_recognizes_explicit_owner_declarations(text):
    result = classify_yapo_listing({"title": text, "description": ""})
    assert result["state"] == "DUEÑO_PROBABLE", text
    assert result["owner_evidence"], text
    assert result["confidence"] is None
    assert result["classification_rule_ids"]


@pytest.mark.parametrize("text", [
    "Sin corredor", "Sin corredora", "Sin corredores", "Sin intermediarios",
    "Sin intermediación inmobiliaria", "No hay corredor", "No interviene corredora",
    "Corredores abstenerse", "Agencias abstenerse", "Solo particulares", "No acepto corredores",
    "Sin comisión", "Sin comisión para el comprador", "No se paga comisión", "Comisión cero",
    "0 por ciento de comisión", "S/comisión", "S/intermediarios", "Trato directo",
    "Trato directo con propietario", "Venta directa", "Compra directa", "Sin intermediación de terceros",
])
def test_supportive_or_neutral_phrases_alone_do_not_claim_owner(text):
    result = classify_yapo_listing({"description": text})
    assert result["state"] == "INCIERTO", text
    assert result["owner_evidence"] == [], text
    assert result["requires_review"] is True


@pytest.mark.parametrize("text", [
    "El propietario acepta ofertas.", "El dueño autoriza visitas.", "El dueño entrega la propiedad.",
    "El propietario paga comisión.", "Nuestro cliente es el propietario.",
    "Gestionamos la venta para el propietario.", "Representamos al dueño.",
    "Asesoramos al propietario.", "Contactamos al propietario.", "Trabajamos con el dueño.",
    "Propiedad captada directamente del dueño.", "Tenemos mandato del propietario.",
    "La inmobiliaria vende por cuenta del dueño.", "Agendamos visitas directamente con los dueños.",
])
def test_third_party_owner_mentions_do_not_identify_publisher_as_owner(text):
    result = classify_yapo_listing({"description": text})
    assert result["owner_evidence"] == [], text
    assert result["state"] == "INCIERTO", text


@pytest.mark.parametrize("text", [
    "No soy dueño.", "No somos propietarios.", "El propietario no vende directamente.",
    "Busco dueño que venda.", "Busco casa de propietario.", "Solo propietarios, contactar.",
])
def test_owner_negations_and_searches_do_not_create_owner_evidence(text):
    result = classify_yapo_listing({"description": text})
    assert result["owner_evidence"] == [], text
    assert result["state"] == "INCIERTO", text


def test_html_case_and_long_description_keep_exact_evidence_and_all_source_text():
    tail = "Vende su propietario, sin comisión. Consulte y agende su visita."
    description = ("Casa amplia, documentación regularizada. " * 400) + tail
    result = classify_yapo_listing({"description": description})
    assert result["state"] == "DUEÑO_PROBABLE"
    declaration = next(item for item in result["owner_evidence"] if item["rule_id"] == "OWNER_SALE_BY_OWNER")
    assert declaration["fragment"] == "Vende su propietario"
    assert description[declaration["position_start"]:declaration["position_end"]] == declaration["fragment"]
    assert "NO_COMMISSION" in result["classification_rule_ids"]
    assert len(description) > 10_000


def test_family_sale_is_a_separate_review_signal_not_owner_or_broker():
    result = classify_yapo_listing({"description": "Vendemos la casa de nuestra familia, propiedad heredada."})
    assert result["state"] == "INCIERTO"
    assert result["family_or_representative_signals"]
    assert result["owner_evidence"] == []
    assert result["broker_evidence"] == []
    assert result["requires_review"] is True


def test_profile_and_listing_advertiser_are_extracted_as_distinct_confirmed_fields():
    html = """<html><body><h1>Casa en Maipú</h1><div class="contact_info">
      <a href="/user/profile/id/12345678"><span class="seller-name">María Pérez</span></a>
      <p>PROFESIONAL</p></div><dl><dt>Tipo de anunciante</dt><dd>Propietario</dd></dl>
      <div class="description">Vende su propia casa</div></body></html>"""
    result = extract_listing_fields(html, "https://www.yapo.cl/listing/12345678")
    assert result["seller_profile_id"] == "12345678"
    assert result["seller_type"] == "PROFESIONAL"
    assert result["listing_advertiser_type"] == "Propietario"
    assert result["publisher_field_provenance"]["seller_type"]["status"] == "confirmed"
    assert result["listing_advertiser_type_source"] == "yapo_listing_attribute_tipo_anunciante"


def test_imported_export_fields_do_not_become_confirmed_publisher_identity():
    saved = _with_saved_publisher_provenance({
        "publicador_visible": "Vilches Propiedades",
        "logo": "Vilches Propiedades",
        "tipo_anunciante": "Propietario",
    })
    assert saved["publisher_field_provenance"] == {}
    assert saved["unverified_publisher_fields"] == {
        "publicador_visible": "Vilches Propiedades",
        "contact_logo_alt": "Vilches Propiedades",
    }
    assert saved["seller_type"] == ""
    assert saved["listing_advertiser_type"] == "Propietario"


def test_five_required_real_listing_regressions():
    profile_badge = {"seller_type": {"status": "confirmed", "source": "publisher_scope:publisher_profile_account_badge", "relationship": "publisher"}}
    commercial_profile = {
        "publicador_visible": {"status": "confirmed", "source": "yapo_publisher_name", "relationship": "publisher"},
        "contact_logo_alt": {"status": "confirmed", "source": "yapo_publisher_logo_alt", "relationship": "publisher"},
        "seller_type": {"status": "confirmed", "source": "publisher_scope:publisher_profile_account_badge", "relationship": "publisher"},
    }
    cases = {
        "26547167": ({"title": "Casa en Maipú", "description": "Vende su propio dueño, valor solo contado.", "publicador_visible": "Paola Andrea", "seller_profile_id": "14042195", "seller_type": "PROFESIONAL", "listing_advertiser_type": "Propietario", "publisher_field_provenance": profile_badge}, "INCIERTO", "OWNER_SALE_BY_OWNER"),
        "33190839": ({"title": "Hermosa casa individual", "description": "Propiedad con todos sus papeles al día, vende su propietario, sin comisión. Consulte y agende su visita.", "seller_profile_id": "18088099", "listing_advertiser_type": "Propietario"}, "DUEÑO_PROBABLE", "OWNER_SALE_BY_OWNER"),
        "26581974": ({"title": "Casa en Maipú", "description": "Agende su visita.", "publicador_visible": "GLORIA SILVA", "seller_profile_id": "13755528", "seller_type": "PROFESIONAL", "listing_advertiser_type": "Propietario", "publisher_field_provenance": profile_badge}, "INCIERTO", "LISTING_ADVERTISER_OWNER_TYPE"),
        "32562399": ({"title": "Excelente oportunidad habitacional", "publicador_visible": "Vilches Propiedades", "contact_logo_alt": "Vilches Propiedades", "seller_profile_id": "13735924", "seller_type": "PROFESIONAL", "listing_advertiser_type": "Agente", "publisher_field_provenance": commercial_profile}, "CORREDOR_SEGURO", "PUBLISHER_IDENTITY_COMPANY_SHAPE"),
        "32160716": ({"title": "Casa en venta", "publicador_visible": "Vivaqui Propiedades", "contact_logo_alt": "Vivaqui Propiedades", "seller_profile_id": "13723213", "seller_type": "PROFESIONAL", "publisher_field_provenance": commercial_profile}, "CORREDOR_SEGURO", "PUBLISHER_IDENTITY_COMPANY_SHAPE"),
    }
    for listing_id, (fields, expected_state, expected_rule) in cases.items():
        result = classify_yapo_listing(fields)
        assert result["state"] == expected_state, listing_id
        assert result["confidence"] is None, listing_id
        assert expected_rule in result["classification_rule_ids"], (listing_id, result["classification_rule_ids"])
        if listing_id in {"26547167", "26581974"}:
            assert result["contradictions"], listing_id
            assert result["requires_review"] is True, listing_id
        if listing_id in {"32562399", "32160716"}:
            assert result["classification_route"] if "classification_route" in result else result["state"] == "CORREDOR_SEGURO"
            assert result["broker_evidence"], listing_id


def test_routing_sends_only_unresolved_cases_to_simulation_with_zero_budget():
    clear_owner = classify_with_routing({"title": "Dueño vende casa", "description": "Vende su propiedad."})
    ambiguous = classify_with_routing({"title": "Casa", "description": "Trato directo, sin comisión."})
    known_broker = classify_with_routing({
        "publicador_visible": "Vilches Propiedades", "seller_type": "PROFESIONAL",
        "publisher_field_provenance": {
            "publicador_visible": {"status": "confirmed", "source": "yapo", "relationship": "publisher"},
            "seller_type": {"status": "confirmed", "source": "yapo", "relationship": "publisher"},
        },
    })
    assert clear_owner["classification_route"] == "deterministic_only"
    assert ambiguous["classification_route"] == "deepseek_candidate"
    assert known_broker["classification_route"] == "deterministic_only"
    for result in (clear_owner, ambiguous, known_broker):
        assert result["deepseek_call_executed"] is False
        assert result["deepseek_budget_max_calls"] == 0
        assert result["deepseek_budget_max_usd"] == 0.0
