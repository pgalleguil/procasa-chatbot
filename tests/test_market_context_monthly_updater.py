from __future__ import annotations

from copy import deepcopy

import mongomock

from owner_portal.market_context import (
    MARKET_CONTEXT_NATURAL_KEY,
    MARKET_CONTEXT_UNIQUE_INDEX_NAME,
    SEPTEMBER_2026_PERIOD,
    SEPTEMBER_2026_SEED,
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
