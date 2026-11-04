from __future__ import annotations

from scrapers.scraper_yapo.staging import (
    build_review_batch_entry,
    build_staging_update,
    ensure_unique_identity_index,
)
from scrapers.scraper_yapo.config import _flashproxy_endpoint_from_env
from urllib.parse import urlsplit


class FakeCollection:
    def __init__(self, docs=None, indexes=None):
        self.docs = list(docs or [])
        self.indexes = dict(indexes or {})
        self.created = []

    def find(self, query, projection=None):
        return iter(self.docs)

    def index_information(self):
        return self.indexes

    def create_index(self, keys, unique=False, name=None):
        self.created.append((keys, unique, name))
        self.indexes[name] = {"key": keys, "unique": unique}
        return name


def test_staging_update_separates_original_from_v3_and_never_promotes():
    record = {
        "listing_id": "12345678",
        "url": "https://www.yapo.cl/listing/12345678",
        "classification": {"state": "INCIERTO", "classifier_version": "yapo-deterministic-v6"},
        "gestion": {"notas": ["do not overwrite"]},
        "availability_status": "ACTIVO",
        "availability_source": "historical_cache",
        "availability_checked_at": "2026-11-03T00:00:00Z",
    }
    identity, update = build_staging_update(
        record,
        v3_classification={"state": "CORREDOR_PROBABLE", "classifier_version": "yapo-deterministic-v7"},
        batch_id="batch-1",
        canonical_sha256="a" * 64,
        imported_at="2026-11-04T00:00:00Z",
    )

    assert identity == {"source_portal": "yapo", "listing_id": "12345678"}
    inserted = update["$setOnInsert"]
    updated = update["$set"]
    assert inserted["classification"]["state"] == "INCIERTO"
    assert inserted["classification_original"]["classifier_version"] == "yapo-deterministic-v6"
    assert updated["classification_v3"]["state"] == "CORREDOR_PROBABLE"
    assert inserted["availability_status"] == "NO_VERIFICADO"
    assert updated["staging_only"] is True
    assert updated["crm_promotion_allowed"] is False
    assert updated["distribution_blocked"] is True
    assert updated["assignment_ready"] is False
    assert inserted["gestion"]["notas"] == ["do not overwrite"]


def test_flashproxy_live_status_can_be_stored_but_http_200_alone_cannot():
    base = {"listing_id": "12345678", "classification": {"state": "DUEÑO_PROBABLE"}}
    live = {
        **base,
        "availability_status": "ACTIVO",
        "availability_source": "flashproxy",
        "availability_checked_at": "2026-11-04T12:00:00Z",
        "availability_evidence": ["explicit_primary_listing_active_badge"],
    }
    _, live_update = build_staging_update(
        live, v3_classification={"state": "DUEÑO_PROBABLE"}, batch_id="live",
    )
    assert live_update["$set"]["availability_status"] == "ACTIVO"
    assert "availability_status" not in live_update["$setOnInsert"]

    unverified = {
        **base,
        "availability_status": "ACTIVO",
        "availability_source": "http_200",
        "availability_checked_at": "2026-11-04T12:00:00Z",
    }
    _, unverified_update = build_staging_update(
        unverified, v3_classification={"state": "DUEÑO_PROBABLE"}, batch_id="http-200",
    )
    assert unverified_update["$setOnInsert"]["availability_status"] == "NO_VERIFICADO"
    assert "availability_status" not in unverified_update["$set"]


def test_observations_are_additive_and_review_entry_has_empty_user_fields():
    identity, update = build_staging_update(
        {"listing_id": "12345678", "classification": {"state": "INCIERTO"}},
        v3_classification={"state": "INCIERTO"},
        batch_id="batch-2",
        manual_observations=[{"observation_id": "obs-1", "is_ground_truth": False}],
    )
    assert identity["listing_id"] == "12345678"
    assert update["$addToSet"]["manual_audit_observations"]["$each"] == [
        {"observation_id": "obs-1", "is_ground_truth": False}
    ]
    entry = build_review_batch_entry(
        listing_id="12345678", batch_id="review-40", state="INCIERTO", seed=20261104,
    )
    assert entry == {
        "batch_id": "review-40", "sampling_seed": 20261104,
        "sampled_state": "INCIERTO", "status": "PENDING",
        "listing_id": "12345678", "comment": "", "verdict": None,
    }


def test_unique_index_dry_run_does_not_write_and_apply_creates_it():
    collection = FakeCollection()
    dry = ensure_unique_identity_index(collection, apply=False)
    assert dry["ready"] is False
    assert collection.created == []
    applied = ensure_unique_identity_index(collection, apply=True)
    assert applied["ready"] is True
    assert applied["created"] is True
    assert collection.created == [([("source_portal", 1), ("listing_id", 1)], True, "source_portal_listing_id_unique")]


def test_duplicate_stage_identity_blocks_unique_index_creation():
    collection = FakeCollection(docs=[
        {"source_portal": "yapo", "listing_id": "12345678"},
        {"source_portal": "yapo", "listing_id": "12345678"},
    ])
    try:
        ensure_unique_identity_index(collection, apply=True)
    except RuntimeError as exc:
        assert "identity integrity blocks unique index creation" in str(exc)
    else:
        raise AssertionError("duplicate logical identities must block staging index creation")
    assert collection.created == []


def test_legacy_proxy_env_resolves_only_flashproxy_without_exposing_config(monkeypatch):
    monkeypatch.delenv("FLASHPROXY_PROXY_URL", raising=False)
    monkeypatch.setenv("PROXIES", "lite.flashproxy.io:6969")
    monkeypatch.setenv("PROXY_USER", "test-user")
    monkeypatch.setenv("PROXY_PASS", "test-secret")
    resolved = _flashproxy_endpoint_from_env()
    parsed = urlsplit(resolved)
    assert parsed.hostname == "lite.flashproxy.io"
    assert parsed.port == 6969
    assert parsed.username == "test-user"
    assert parsed.password == "test-secret"

    monkeypatch.setenv("PROXIES", "generic-proxy.example:8080")
    assert _flashproxy_endpoint_from_env() == ""

    monkeypatch.setenv("FLASHPROXY_PROXY_URL", "http://user:pass@not-flashproxy.example:8080")
    assert _flashproxy_endpoint_from_env() == ""
