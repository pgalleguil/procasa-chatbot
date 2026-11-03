from __future__ import annotations

import sys
import ast
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from scrapers.scraper_yapo.config import AppConfig
from scrapers.scraper_yapo.import_yapo_prepared_batch import (
    _build_update,
    _identity_matches,
    _safe_update_plan,
    _upsert_one,
)
from scrapers.scraper_yapo.mongo_store import MongoStore


def test_yapo_identity_audit_detects_canonical_match_and_cross_portal_conflict():
    incoming = {"listing_id": "12345678", "url": "https://www.yapo.cl/venta-casas/casa/12345678"}
    canonical = {"listing_id": "12345678", "url": "https://yapo.cl/venta-casas/casa/12345678/", "origen": "yapo"}
    conflict = {"listing_id": "12345678", "url": "https://example.com/listing/12345678", "origen": "other"}
    assert _identity_matches(incoming, canonical)["canonical_match"] is True
    assert _identity_matches(incoming, conflict)["identity_conflict"] is True


def test_update_plan_preserves_human_classification_crm_and_nonempty_values():
    store = MongoStore(AppConfig())
    existing = {
        "classification": {"state": "DUEÑO_SEGURO", "source": "crm_manual", "human_confirmed": True},
        "gestion": {"nota": "seguimiento previo"}, "assigned_to": "ejecutivo-1",
        "banos": 2, "description": "descripción más completa ya guardada",
    }
    incoming = {"classification": {"state": "INCIERTO"}, "gestion": {}, "assigned_to": "",
                "banos": None, "description": "", "title": "Casa nueva"}
    plan = _safe_update_plan(incoming, existing, store)
    update, preserved_empty = _build_update(existing, incoming, store)
    assert "classification" not in update
    assert "gestion" not in update and "assigned_to" not in update
    assert "banos" not in update and "description" not in update
    assert update == {"title": "Casa nueva"}
    assert plan["human_classification_preserved"] is True
    assert set(preserved_empty) == {"banos", "description"}


class _FakeCollection:
    def __init__(self, existing=None):
        self.doc = existing
        self.updates = []

    def find_one(self, query):
        return self.doc

    def update_one(self, query, update, upsert=False):
        assert "$unset" not in update
        self.updates.append(update)
        if self.doc is None and upsert:
            self.doc = dict(update.get("$setOnInsert", {}))
            self.doc.update(update.get("$set", {}))
            self.doc["_id"] = "inserted-id"
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id="inserted-id")
        before = dict(self.doc or {})
        (self.doc or {}).update(update.get("$set", {}))
        changed = before != (self.doc or {})
        return SimpleNamespace(matched_count=1, modified_count=int(changed), upserted_id=None)


def test_import_route_is_idempotent_and_never_writes_crm_assignment():
    store = MongoStore(AppConfig())
    collection = _FakeCollection()
    record = {
        "listing_id": "12345678", "url": "https://www.yapo.cl/venta-casas/casa/12345678",
        "canonical_url": "https://www.yapo.cl/venta-casas/casa/12345678", "origen": "yapo",
        "source_portal": "yapo", "batch_id": "preimport_test", "run_id": "preimport_test",
        "updated_at": "2026-11-03T00:00:00+00:00", "title": "Casa", "comuna": "Maipú",
        "classification": {"state": "DUEÑO_PROBABLE", "assignment_ready": False,
                            "assignment_block_reasons": ["owner_probability_missing"]},
    }
    first = _upsert_one(collection, store, record)
    snapshot = dict(collection.doc)
    second = _upsert_one(collection, store, record)
    assert first["upserted_id"] == "inserted-id"
    assert second["matched_count"] == 1
    assert second["modified_count"] == 0
    assert collection.doc == snapshot
    assert all("$unset" not in update for update in collection.updates)
    assert "assigned_to" not in collection.doc and "asignacion" not in collection.doc
    assert "owner_probability" not in collection.doc["classification"]
    assert collection.doc["classification"]["assignment_ready"] is False


def test_existing_crm_data_and_human_decision_survive_import():
    store = MongoStore(AppConfig())
    existing = {
        "_id": "stored-id", "listing_id": "12345678", "url": "https://www.yapo.cl/venta-casas/casa/12345678",
        "origen": "yapo", "gestion": {"nota": "llamar el viernes"}, "assigned_to": "asesor-7",
        "classification": {"state": "DUEÑO_SEGURO", "source": "crm_manual", "human_confirmed": True},
        "description": "texto de CRM ya revisado",
    }
    collection = _FakeCollection(existing)
    record = {
        "listing_id": "12345678", "url": existing["url"], "canonical_url": existing["url"],
        "origen": "yapo", "title": "Casa actualizada", "gestion": {}, "assigned_to": "",
        "description": "", "classification": {"state": "INCIERTO", "assignment_ready": False},
    }
    result = _upsert_one(collection, store, record)
    assert result["matched_count"] == 1
    assert collection.doc["gestion"] == {"nota": "llamar el viernes"}
    assert collection.doc["assigned_to"] == "asesor-7"
    assert collection.doc["classification"] == existing["classification"]
    assert collection.doc["description"] == "texto de CRM ya revisado"
    assert collection.doc["title"] == "Casa actualizada"


def test_import_route_source_has_no_scrape_or_post_distribution_hook():
    path = Path(__file__).resolve().parents[1] / "import_yapo_prepared_batch.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
    assert "_run_post_scrape_distribution" not in names | calls | imports
    assert "download_html" not in names | calls | imports
    assert "discover_listing_urls" not in names | calls | imports
