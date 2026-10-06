from __future__ import annotations

from copy import deepcopy

import mongomock

from owner_portal.market_context import (
    MARKET_CONTEXT_NATURAL_KEY,
    MARKET_CONTEXT_UNIQUE_INDEX_NAME,
    SEPTEMBER_2026_PERIOD,
    SEPTEMBER_2026_SEED,
    MarketContextBootstrapConflict,
    ensure_market_context_snapshots_initialized,
    inspect_unique_key_integrity,
    plan_period_update,
    validate_market_context_documents,
)
from scripts import update_market_context_snapshots as updater


def _october_document():
    return {
        "period": "2026-10",
        "indicator_kind": "UNEMPLOYMENT",
        "value": 8.4,
        "display_value": "8,4%",
        "unit": "%",
        "observation_period": "jul-sep 2026",
        "geography_level": "REGION",
        "geography_code": "RM",
        "geography_label": "Región Metropolitana",
        "source_name": "INE · Encuesta Nacional de Empleo",
        "source_reference": "https://example.test/verified-source",
        "source_published_at": "2026-10-30",
        "verified_at": "2026-10-31",
        "relevant_operations": ["VENTA", "ARRIENDO"],
        "methodology_notes": "Test unitario local; no se carga a Mongo.",
        "verified": True,
        "active": True,
    }


def test_approved_september_manifest_passes_permanent_metadata_validation():
    assert validate_market_context_documents(SEPTEMBER_2026_SEED, period=SEPTEMBER_2026_PERIOD) == []
    assert updater.validate_seed(SEPTEMBER_2026_SEED) == []


def test_period_plan_is_insert_then_unchanged_and_updates_verified_corrections():
    first = plan_period_update(
        period=SEPTEMBER_2026_PERIOD,
        incoming_documents=SEPTEMBER_2026_SEED,
        existing_period_documents=[],
    )
    assert len(first["inserts"]) == 22
    assert first["updates"] == first["unchanged"] == first["conflicts"] == []

    same = plan_period_update(
        period=SEPTEMBER_2026_PERIOD,
        incoming_documents=SEPTEMBER_2026_SEED,
        existing_period_documents=SEPTEMBER_2026_SEED,
    )
    assert len(same["unchanged"]) == 22
    assert same["inserts"] == same["updates"] == same["conflicts"] == []

    corrected = [dict(item) for item in SEPTEMBER_2026_SEED]
    corrected[0]["value"] = 4.09
    corrected[0]["display_value"] = "4,09%"
    update = plan_period_update(
        period=SEPTEMBER_2026_PERIOD,
        incoming_documents=corrected,
        existing_period_documents=SEPTEMBER_2026_SEED,
    )
    assert len(update["updates"]) == 1
    assert len(update["unchanged"]) == 21
    assert update["conflicts"] == []


def test_duplicate_or_omitted_same_period_identity_fails_closed():
    one = deepcopy(SEPTEMBER_2026_SEED[0])
    duplicate = plan_period_update(
        period=SEPTEMBER_2026_PERIOD,
        incoming_documents=SEPTEMBER_2026_SEED,
        existing_period_documents=[one, one],
    )
    assert any(item["reason"] == "duplicate natural key" for item in duplicate["conflicts"])

    omitted = plan_period_update(
        period=SEPTEMBER_2026_PERIOD,
        incoming_documents=SEPTEMBER_2026_SEED[1:],
        existing_period_documents=SEPTEMBER_2026_SEED,
    )
    assert any(item["reason"] == "existing period identity absent from manifest" for item in omitted["conflicts"])


def test_invalid_source_unverified_or_forbidden_indicator_is_rejected():
    missing_source = [dict(item) for item in SEPTEMBER_2026_SEED]
    missing_source[0]["source_reference"] = ""
    assert any("SOURCE" in item for item in validate_market_context_documents(missing_source, period=SEPTEMBER_2026_PERIOD))

    unverified = [dict(item) for item in SEPTEMBER_2026_SEED]
    unverified[0]["verified"] = False
    assert any("NOT_VERIFIED" in item for item in validate_market_context_documents(unverified, period=SEPTEMBER_2026_PERIOD))

    forbidden = [dict(item) for item in SEPTEMBER_2026_SEED]
    forbidden[0]["indicator_kind"] = "FOGAES_CONTEXT"
    assert any("FORBIDDEN_INDICATOR_KIND" in item for item in validate_market_context_documents(forbidden, period=SEPTEMBER_2026_PERIOD))


def test_period_update_does_not_touch_previous_month_and_uses_input_period_only():
    october = _october_document()
    plan = plan_period_update(
        period="2026-10",
        incoming_documents=[october],
        existing_period_documents=[*SEPTEMBER_2026_SEED],
    )
    assert len(plan["inserts"]) == 1
    assert plan["updates"] == plan["unchanged"] == plan["conflicts"] == []
    assert plan["inserts"][0]["period"] == "2026-10"


def test_unique_key_integrity_detects_missing_and_duplicate_keys():
    item = deepcopy(SEPTEMBER_2026_SEED[0])
    duplicate_issues = inspect_unique_key_integrity([item, item])
    assert any(issue["reason"] == "duplicate natural key" for issue in duplicate_issues)
    missing_issues = inspect_unique_key_integrity([{"period": "2026-09"}])
    assert any(issue["reason"] == "incomplete natural key" for issue in missing_issues)


def test_execute_creates_named_unique_natural_key_index_in_test_database():
    collection = mongomock.MongoClient().URLS.market_context_snapshots
    assert updater._ensure_unique_index(collection) is True
    spec = collection.index_information()[MARKET_CONTEXT_UNIQUE_INDEX_NAME]
    assert spec["unique"] is True
    assert list(spec["key"]) == [(field, 1) for field in MARKET_CONTEXT_NATURAL_KEY]


def test_later_period_requires_its_own_verified_manifest(monkeypatch, capsys):
    monkeypatch.setattr(updater, "_mongo_config", lambda: (None, "URLS"))
    assert updater.run_update(period="2026-10", execute=False) == 4
    assert "VERIFIED_PERIOD_MANIFEST_NOT_FOUND" in capsys.readouterr().out

    assert updater.run_update(period=SEPTEMBER_2026_PERIOD, execute=False) == 0
    output = capsys.readouterr().out
    assert "SEED_MANIFEST_TOTAL=22" in output
    assert "DATABASE_DIFF=UNAVAILABLE_NO_MONGO_URI" in output
    assert "DB_WRITES=0" in output


def test_execute_requires_mongo_uri_and_fails_before_any_database_action(monkeypatch, capsys):
    monkeypatch.setattr(updater, "_mongo_config", lambda: (None, "URLS"))
    assert updater.run_update(period=SEPTEMBER_2026_PERIOD, execute=True) == 2
    assert "MONGO_URI_NOT_CONFIGURED" in capsys.readouterr().out


class _CollectionScopedDatabase:
    """Fail tests if startup bootstrap reaches any collection but its target."""

    def __init__(self, database):
        self.database = database
        self.accessed = []

    def __getitem__(self, name):
        self.accessed.append(name)
        if name != "market_context_snapshots":
            raise AssertionError(f"bootstrap accessed unauthorized collection: {name}")
        return self.database[name]


def test_startup_bootstrap_creates_exactly_22_in_target_collection_only():
    database = mongomock.MongoClient().URLS
    database.other_collection.insert_one({"_id": "sentinel", "value": "unchanged"})
    scoped = _CollectionScopedDatabase(database)

    result = ensure_market_context_snapshots_initialized(scoped)

    assert result == {
        "status": "initialized",
        "inserts": 22,
        "updates": 0,
        "unchanged": 22,
        "conflicts": 0,
        "unique_index_ready": True,
        "collection_documents": 22,
    }
    assert scoped.accessed == ["market_context_snapshots"]
    assert database.market_context_snapshots.count_documents({}) == 22
    assert database.other_collection.find_one({"_id": "sentinel"})["value"] == "unchanged"
    assert set(database.list_collection_names()) == {"market_context_snapshots", "other_collection"}


def test_startup_bootstrap_with_complete_equal_seed_is_noop():
    collection = mongomock.MongoClient().URLS.market_context_snapshots
    collection.insert_many([deepcopy(item) for item in SEPTEMBER_2026_SEED])
    database = _CollectionScopedDatabase(collection.database)

    first = ensure_market_context_snapshots_initialized(database)
    second = ensure_market_context_snapshots_initialized(database)

    assert first["inserts"] == second["inserts"] == 0
    assert first["updates"] == second["updates"] == 0
    assert first["unchanged"] == second["unchanged"] == 22
    assert collection.count_documents({}) == 22
    assert database.accessed == ["market_context_snapshots", "market_context_snapshots"]


def test_startup_bootstrap_completes_only_missing_seed_document():
    collection = mongomock.MongoClient().URLS.market_context_snapshots
    existing = [deepcopy(item) for item in SEPTEMBER_2026_SEED if item != SEPTEMBER_2026_SEED[7]]
    collection.insert_many(existing)

    result = ensure_market_context_snapshots_initialized(_CollectionScopedDatabase(collection.database))

    assert result["inserts"] == 1
    assert result["updates"] == 0
    assert result["unchanged"] == 22
    assert result["conflicts"] == 0
    assert collection.count_documents({}) == 22
    assert collection.find_one({"period": SEPTEMBER_2026_PERIOD, "indicator_kind": SEPTEMBER_2026_SEED[7]["indicator_kind"], "geography_code": SEPTEMBER_2026_SEED[7]["geography_code"]})


def test_startup_bootstrap_conflict_stops_before_index_or_document_write():
    collection = mongomock.MongoClient().URLS.market_context_snapshots
    changed = deepcopy(SEPTEMBER_2026_SEED[0])
    changed["value"] = float(changed["value"]) + 1
    collection.insert_one(changed)
    before = list(collection.find({}))

    try:
        ensure_market_context_snapshots_initialized(_CollectionScopedDatabase(collection.database))
    except MarketContextBootstrapConflict as exc:
        assert "stored document differs" in str(exc)
    else:
        raise AssertionError("a divergent baseline document must stop the bootstrap")

    assert collection.count_documents({}) == 1
    assert list(collection.find({})) == before
    assert set(collection.index_information()) == {"_id_"}


def test_startup_bootstrap_unique_index_has_exact_natural_key():
    database = mongomock.MongoClient().URLS
    result = ensure_market_context_snapshots_initialized(_CollectionScopedDatabase(database))
    spec = database.market_context_snapshots.index_information()[MARKET_CONTEXT_UNIQUE_INDEX_NAME]

    assert result["unique_index_ready"] is True
    assert spec["unique"] is True
    assert list(spec["key"]) == [(field, 1) for field in MARKET_CONTEXT_NATURAL_KEY]
