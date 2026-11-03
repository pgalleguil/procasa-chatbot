from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from scrapers.scraper_yapo.classifier_rules import classify_yapo_listing
from scrapers.scraper_yapo.config import AppConfig
from scrapers.scraper_yapo.extractor import extract_listing_fields
from scrapers.scraper_yapo.mongo_store import MongoStore
from scrapers.scraper_yapo.simulate_captacion_compatibility import _number, _price_fields, _validate


def _confirmed(**fields):
    return {
        **fields,
        "publisher_field_provenance": {
            key: {"status": "confirmed", "source": "test_publisher_block", "relationship": "publisher"}
            for key in fields if key in {"seller_type", "publicador_visible", "contact_name", "contact_badges_text"}
        },
    }


def test_professional_tag_alone_does_not_classify_broker_or_cancel_owner_statement():
    uncertain = classify_yapo_listing(_confirmed(
        seller_type="PROFESIONAL", publicador_visible="Sandra Muñoz",
        listing_advertiser_type="", title="Casa en venta", description="Casa en Maipú.",
    ))
    owner = classify_yapo_listing(_confirmed(
        seller_type="PROFESIONAL", publicador_visible="Jorge Muñoz",
        listing_advertiser_type="Propietario", title="Dueño vende casa", description="Vendo mi casa.",
    ))
    assert uncertain["state"] == "INCIERTO"
    assert owner["state"] == "DUEÑO_PROBABLE"
    assert not owner["contradictions"]


def test_explicit_owner_statement_with_different_named_contact_stays_review():
    result = classify_yapo_listing(_confirmed(
        seller_type="PROFESIONAL", publicador_visible="Katherine Bravo",
        title="Vende su dueña", description="Vende su dueña; contactar a: Valentina Corales",
    ))
    assert result["state"] == "INCIERTO"
    assert result["requires_review"] is True
    assert result["contradictions"][0]["rule_id"] == "OWNER_DECLARATION_CONTACT_IDENTITY_MISMATCH"


def test_compact_real_estate_brand_with_confirmed_professional_profile_is_broker():
    result = classify_yapo_listing({
        "publicador_visible": "CastroPropiedades", "contact_name": "CastroPropiedades",
        "seller_type": "PROFESIONAL", "title": "Casa en venta", "description": "Casa en Maipú",
        "publisher_field_provenance": {
            "publicador_visible": {"status": "confirmed", "relationship": "publisher"},
            "contact_name": {"status": "confirmed", "relationship": "publisher"},
            "seller_type": {"status": "confirmed", "relationship": "publisher"},
        },
    })
    assert result["state"] == "CORREDOR_SEGURO"
    assert any("propiedades" in str(item).casefold() for item in result["evidence"])


def test_reviewed_commercial_profile_is_probable_without_confirming_catalog_brand():
    base = {
        "seller_profile_id": "17057667", "publicador_visible": "Hobbins Vitacura",
        "seller_type": "PROFESIONAL",
        "publisher_field_provenance": {
            "publicador_visible": {"status": "confirmed", "relationship": "publisher"},
            "seller_type": {"status": "confirmed", "relationship": "publisher"},
        },
    }
    result = classify_yapo_listing(base)
    assert result["state"] == "CORREDOR_PROBABLE"
    assert "verified_candidate_profile_not_confirmed_brand" == result["source"]
    mismatch = dict(base, publicador_visible="Another account")
    assert classify_yapo_listing(mismatch)["state"] == "INCIERTO"


def test_current_yapo_detail_header_recovers_price_and_commune_without_carousel_values():
    html = """<html><head><title>Casa | 4 dormitorios por 4700000 en Maipú</title></head><body>
    <main><div class="space-y-4"><div class="relative pr-2">
    <h1>Casa de prueba</h1><span class="text-muted-foreground flex"><span>Región Metropolitana, Maipú</span></span>
    </div><div class="text-foreground font-bold text-2xl">UF 4.700</div></div>
    <div class="card_content"><span class="card_price">UF 9.999</span><p class="card_subtitle">Región de Valparaíso, Viña del Mar</p></div>
    </main></body></html>"""
    fields = extract_listing_fields(html, source_url="https://www.yapo.cl/venta-casa/12345678")
    assert fields["price"] == "UF 4.700"
    assert fields["price_currency"] == "UF"
    assert fields["price_value"] == "4.700"
    assert fields["comuna"] == "Maipú"
    assert fields["comuna_source"] == "yapo_detail_header_location"


def test_price_parser_ignores_yapo_discount_percentage():
    assert _number("$ 210.000.000 - 16 %") == 210000000
    assert _number("UF 3.600 - 3 %") == 3600
    assert _price_fields({"price": "UF 3.600 - 3 %"})["precio_uf"] == 3600


def test_validation_separates_structure_extraction_and_commercial_exclusion():
    doc = {
        "schema_version": "yapo_propiedades_v1", "origen": "yapo", "source_portal": "yapo",
        "source": "owner_hunt", "listing_id": "12345678", "url": "https://www.yapo.cl/a/12345678",
        "canonical_url": "https://www.yapo.cl/a/12345678", "run_id": "r", "batch_id": "b",
        "classification": {"state": "CORREDOR_SEGURO", "evidence": [], "contradictions": []},
        "html_validation_status": "OK", "html_validation_reason": "valid_html", "scrape_stage": "processed", "pipeline_state": "CLASSIFIED",
        "pipeline_complete": True, "processed_at": "now", "updated_at": "now",
        "title": "Casa", "description": "Descripción completa", "operacion": "venta",
        "descripcion": "Descripción completa", "body_text": "Descripción completa", "descripcion_source": "description",
        "tipo_propiedad": "casa", "comuna": "Maipú", "precio_raw": "UF 4000",
        "precio_moneda_original": "UF", "image_urls": [], "images": [], "attributes": {},
        "details": {}, "raw_attributes": {}, "publisher_profile_context": {}, "publisher_field_provenance": {},
        "publisher_identity_candidates": [],
        "precio_original_num": 4000,
    }
    result = _validate(doc)
    assert result["structural_valid"] is True
    assert result["extraction_complete"] is True
    assert result["commercially_excluded_broker"] is True
    assert result["commercial_candidate"] is False
    assert result["persistence_ready"] is True
    pending = dict(doc, html_validation_status="MISSING_LOCAL_HTML", html_validation_reason="missing_local_html", scrape_stage="needs_rescrape")
    pending_result = _validate(pending)
    assert pending_result["structural_valid"] is True
    assert pending_result["technical_valid"] is False
    assert pending_result["extraction_complete"] is False
    assert pending_result["persistence_ready"] is False


def test_yapo_canonical_adapter_preserves_state_and_does_not_invent_probability():
    store = MongoStore(AppConfig())
    payload = store._sanitize_payload({
        "listing_id": "25898826", "url": "https://www.yapo.cl/listing/25898826",
        "comuna": "Maipú", "description": "Dueño vende casa.",
        "classification": {"state": "DUEÑO_PROBABLE", "source": "owner_declaration_grammar",
                            "evidence_strength": "ALTA", "pipeline_complete": True,
                            "pipeline_state": "CLASSIFIED"},
    })
    classification = payload["classification"]
    assert classification["state"] == "DUEÑO_PROBABLE"
    assert classification["final"] == "OWNER_PROBABLE"
    assert classification["owner_probability"] is None
    assert not classification.get("yapo_probability_state_conflict")
    assert classification["exclude_from_assignment"] is True


class _FakeCollection:
    def __init__(self, existing):
        self.existing = existing
        self.find_query = None
        self.update_args = None

    def find_one(self, query, projection):
        self.find_query = query
        return self.existing

    def update_one(self, query, update, upsert=False):
        self.update_args = (query, update, upsert)
        return type("Result", (), {"matched_count": int(bool(self.existing)), "modified_count": 1, "upserted_id": None})()


def test_upsert_uses_portal_safe_identity_and_preserves_crm_and_human_decision(monkeypatch):
    collection = _FakeCollection({"_id": "mongo-id", "created_at": "old", "classification": {"human_confirmed": True}})
    store = MongoStore(AppConfig())
    monkeypatch.setattr(MongoStore, "collection", lambda self: collection)
    monkeypatch.setattr(MongoStore, "_sanitize_payload", lambda self, row: {
        "listing_id": row["listing_id"], "url": row["url"], "source_portal": "yapo",
        "created_at": "new", "gestion": "old management", "telefono_normalizado": "+56912345678",
        "captacion_management_date": "old date", "classification": {"state": "DUEÑO_PROBABLE"},
        "title": "updated title",
    })
    result = store.upsert_listing({"listing_id": "25898826", "url": "https://www.yapo.cl/ad/25898826"})
    query, update, upsert = collection.update_args
    assert result["matched_count"] == 1
    assert upsert is False
    assert "$and" in query and query["$and"][0] == {"listing_id": {"$in": ["25898826", 25898826]}}
    assert "gestion" not in update["$set"]
    assert "created_at" not in update["$set"]
    assert "classification" not in update["$set"]
    assert "telefono_normalizado" not in update["$set"]
    assert "captacion_management_date" not in update["$set"]
    assert update["$set"]["title"] == "updated title"


def test_yapo_identity_rejects_non_yapo_url_without_listing_id():
    with pytest.raises(ValueError):
        MongoStore._yapo_identity_query({"url": "https://other.example/ad/25898826"})
