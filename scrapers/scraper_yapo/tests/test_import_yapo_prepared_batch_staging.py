from copy import deepcopy

from types import SimpleNamespace

from scrapers.scraper_yapo.import_yapo_prepared_batch import _mongo_import_payload, _upsert_one


def test_staging_migration_keeps_classifications_audits_and_source_provenance():
    source = {
        "source": "yapo_preimport_v3",
        "source_portal": "yapo",
        "origen": "yapo",
        "listing_id": "32200413",
        "staging_only": True,
        "crm_promotion_allowed": False,
        "distribution_blocked": True,
        "assignment_ready": False,
        "staging_batch_id": "preimport-482",
        "staging_created_at": "2026-11-03T10:00:00+00:00",
        "staging_last_batch_id": "manual-review-40",
        "staging_last_imported_at": "2026-11-04T10:00:00+00:00",
        "canonical_source_sha256": "canonical-sha256",
        "classification": {"state": "CORREDOR_SEGURO", "evidence": ["original"]},
        "classification_original": {"state": "CORREDOR_SEGURO"},
        "classification_v3": {"state": "CORREDOR_PROBABLE", "evidence": ["v3"]},
        "manual_audit_observations": [{"text": "human note"}],
        "manual_review_batches": [{"batch_id": "review-40", "comment": ""}],
        "availability_checks": [{"status": "DESACTIVADO", "source": "flashproxy"}],
        "description": "full description",
        "image_urls": ["https://yapo.cl/photo.jpg"],
    }
    original = deepcopy(source)

    payload = _mongo_import_payload(source)

    assert source == original
    assert payload["source"] == "owner_hunt"
    assert "staging_only" not in payload
    assert "crm_promotion_allowed" not in payload
    assert "assignment_ready" not in payload
    assert payload["yapo_ingestion"]["source_collection"] == "URLS.yapo_preimportacion"
    assert payload["yapo_ingestion"]["canonical_source_sha256"] == "canonical-sha256"
    assert payload["classification"] == source["classification"]
    assert payload["classification_original"] == source["classification_original"]
    assert payload["classification_v3"] == source["classification_v3"]
    assert payload["manual_audit_observations"] == source["manual_audit_observations"]
    assert payload["manual_review_batches"] == source["manual_review_batches"]
    assert payload["availability_checks"] == source["availability_checks"]
    assert payload["description"] == source["description"]
    assert payload["image_urls"] == source["image_urls"]


def test_staging_migration_uses_only_set_operators_and_creates_unassigned_row():
    writes = []

    class FakeCollection:
        def find_one(self, _query):
            return None

        def update_one(self, query, update, *, upsert=False):
            writes.append((query, update, upsert))
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id="inserted")

    class FakeStore:
        @staticmethod
        def _yapo_identity_query(record):
            return {"origen": "yapo", "listing_id": record["listing_id"]}

    record = {
        "source": "yapo_preimport_v3", "source_portal": "yapo", "origen": "yapo",
        "listing_id": "32200413", "url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/x/32200413",
        "classification": {"state": "INCIERTO", "assignment_ready": False, "owner_probability": None},
        "classification_v3": {"state": "INCIERTO"}, "staging_only": True,
        "staging_batch_id": "preimport-482", "canonical_source_sha256": "canonical-sha256",
    }

    outcome = _upsert_one(FakeCollection(), FakeStore(), record)

    assert outcome["upserted_id"] == "inserted"
    assert len(writes) == 1
    query, update, upsert = writes[0]
    assert query == {"origen": "yapo", "listing_id": "32200413"}
    assert upsert is True
    assert set(update) == {"$set", "$setOnInsert"}
    assert update["$setOnInsert"]["gestion"]["ejecutivo_id"] is None
    assert update["$setOnInsert"]["source"] == "owner_hunt"
    assert update["$set"]["yapo_ingestion"]["canonical_source_sha256"] == "canonical-sha256"
    assert "staging_only" not in update["$set"]
