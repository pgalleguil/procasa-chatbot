from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from scrapers.scraper_yapo.classifier_rules import classify_yapo_listing
from scrapers.scraper_yapo.config import AppConfig
from scrapers.scraper_yapo.mongo_store import MongoStore


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
