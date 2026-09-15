from dataclasses import replace
from datetime import date, datetime, timezone
from uuid import uuid4

import mongomock
import pytest

from analytics.pricing_intelligence.models import snapshot_document_id
from analytics.pricing_intelligence.snapshot_builder import PropertySnapshotBuilder
from analytics.pricing_intelligence.snapshot_cli import build_parser, main
from analytics.pricing_intelligence.snapshot_repository import (
    ALREADY_COMPLETE,
    INCONSISTENT_SNAPSHOT_STATE,
    PersistenceDisabled,
    PersistenceError,
    RUN_COLLECTION,
    RUN_SCHEMA_VERSION,
    SNAPSHOT_COLLECTION,
    SnapshotPIIError,
    SnapshotRepository,
    validate_no_pii_payload,
)
from analytics.pricing_intelligence.time_utils import BUSINESS_TZ, to_utc


NOW = datetime(2026, 9, 9, 12, tzinfo=BUSINESS_TZ)


def _property(code="P1", price=5000):
    return {
        "codigo": code,
        "tipo_operacion": {
            "tipo": "Venta",
            "precio_venta": {"precio_uf": price, "precio_clp": 190000000},
        },
        "metadata": {"tipo_propiedad": "Departamento"},
        "ubicacion": {"region": "RM", "comuna": "Santiago", "sector": None},
        "caracteristicas": {"dormitorios": 2, "banos": 2, "estacionamientos": 1},
        "publicaciones": {},
        "historial_cambios": [],
    }


def _snapshot(code="P1", price=5000, as_of=NOW):
    return PropertySnapshotBuilder([], now_fn=lambda: as_of).build(
        [_property(code, price)], as_of=as_of
    ).snapshots[0]


def _repo(db=None):
    return SnapshotRepository(
        PropertySnapshotBuilder([], now_fn=lambda: NOW),
        db or mongomock.MongoClient().get_database("URLS"),
        now_fn=lambda: datetime(2026, 9, 9, 15, tzinfo=timezone.utc),
        batch_size=2,
    )


def _persist(repo, snapshots, **kwargs):
    return repo.persist(
        snapshots,
        confirm_production_write=True,
        source_counts={"master_properties": len(snapshots), "leads": 0},
        **kwargs,
    )


def _run_document(repo, snapshot, *, status="PARTIAL", expected=1, run_id=None):
    run_id = run_id or str(uuid4())
    repo.create_run(
        {
            "run_id": run_id,
            "schema_version": RUN_SCHEMA_VERSION,
            "builder_version": "test",
            "status": status,
            "started_at_utc": to_utc(snapshot.as_of_local),
            "completed_at_utc": None,
            "as_of_local": snapshot.as_of_local,
            "as_of_utc": snapshot.as_of_utc,
            "snapshot_date_local": snapshot.snapshot_date_local.isoformat(),
            "expected_properties": expected,
            "attempted_properties": 0,
            "inserted_snapshots": 0,
            "skipped_existing": 0,
            "failed_snapshots": 0,
            "source_counts": {"master_properties": expected},
            "linkage_metrics": {},
            "quality_metrics": {},
            "error_summary": [],
            "resumed_from": None,
        }
    )
    return run_id


def test_deterministic_snapshot_id_and_grain():
    assert snapshot_document_id(date(2026, 9, 9), " P1 ") == "v1:2026-09-09:P1"
    assert snapshot_document_id(date(2026, 9, 9), "P1") == snapshot_document_id(
        date(2026, 9, 9), "P1"
    )
    assert snapshot_document_id(date(2026, 9, 9), "P1") != snapshot_document_id(
        date(2026, 9, 10), "P1"
    )
    assert snapshot_document_id(date(2026, 9, 9), "P1") != snapshot_document_id(
        date(2026, 9, 9), "P2"
    )


def test_same_property_same_day_has_same_id():
    assert snapshot_document_id("2026-09-09", "P1") == "v1:2026-09-09:P1"


def test_distinct_snapshot_dates_have_distinct_ids():
    assert snapshot_document_id("2026-09-09", "P1") != snapshot_document_id("2026-09-10", "P1")


def test_distinct_property_codes_have_distinct_ids():
    assert snapshot_document_id("2026-09-09", "P1") != snapshot_document_id("2026-09-09", "P2")


def test_run_snapshots_share_one_cutoff():
    snapshots = [_snapshot("P1"), _snapshot("P2", price=5100)]
    assert len({item.as_of_local for item in snapshots}) == 1
    assert len({item.as_of_utc for item in snapshots}) == 1


def test_existing_insert_does_not_overwrite_and_complete_rerun_is_clean():
    repo = _repo()
    snapshot = _snapshot()
    first = _persist(repo, [snapshot])
    stored_before = repo.snapshot_collection.find_one({"_id": snapshot.to_dict()["_id"]})
    second = _persist(repo, [snapshot])
    stored_after = repo.snapshot_collection.find_one({"_id": snapshot.to_dict()["_id"]})
    assert first.status == "COMPLETED"
    assert second.status == ALREADY_COMPLETE
    assert stored_after == stored_before
    assert repo.snapshot_collection.count_documents({}) == 1


def test_completed_run_is_not_registered_again():
    repo = _repo()
    snapshot = _snapshot()
    first = _persist(repo, [snapshot])
    second = _persist(repo, [snapshot])
    assert first.run_id == second.run_id
    assert repo.run_collection.count_documents({}) == 1


def test_different_content_for_same_id_is_inconsistent():
    repo = _repo()
    original = _snapshot(price=5000)
    _run_document(repo, original)
    repo.snapshot_collection.insert_one(
        {**original.to_dict(), "run_id": str(uuid4())}
    )
    with pytest.raises(PersistenceError, match=INCONSISTENT_SNAPSHOT_STATE):
        _persist(repo, [_snapshot(price=5100)])


def test_partial_run_reuses_cutoff_and_fills_missing_only():
    repo = _repo()
    first = _snapshot("P1")
    second = _snapshot("P2", price=5100)
    run_id = _run_document(repo, first, expected=2)
    repo.snapshot_collection.insert_one({**first.to_dict(), "run_id": run_id})
    result = _persist(repo, [first, second])
    assert result.status == "COMPLETED"
    assert result.run_id == run_id
    assert result.inserted_snapshots == 1
    assert result.skipped_existing == 1
    assert result.as_of_local == first.as_of_local.isoformat()
    assert repo.snapshot_collection.count_documents({}) == 2


def test_snapshot_without_coherent_run_aborts():
    repo = _repo()
    snapshot = _snapshot()
    repo.snapshot_collection.insert_one(snapshot.to_dict())
    with pytest.raises(PersistenceError, match=INCONSISTENT_SNAPSHOT_STATE):
        _persist(repo, [snapshot])


def test_recursive_pii_validation_rejects_prohibited_key():
    with pytest.raises(SnapshotPIIError):
        validate_no_pii_payload({"provenance": [{"nested": {"Teléfono": "hidden"}}]})


def test_persist_requires_confirmation_without_writing():
    repo = _repo()
    with pytest.raises(PersistenceDisabled):
        repo.persist([_snapshot()])
    assert repo.db.list_collection_names() == []


def test_persist_rejects_sample_and_property_code():
    assert main(["--persist", "--confirm-production-write", "--sample", "1"]) == 2
    assert main(["--persist", "--confirm-production-write", "--property-code", "P1"]) == 2


def test_cli_modes_are_mutually_exclusive_and_dry_run_is_supported():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--dry-run", "--persist"])
    assert build_parser().parse_args(["--dry-run"]).dry_run is True


def test_first_run_has_no_baseline_and_completes_consistently():
    repo = _repo()
    result = _persist(repo, [_snapshot()])
    run = repo.run_collection.find_one({"_id": result.run_id})
    assert result.status == "COMPLETED"
    assert run["status"] == "COMPLETED"
    assert run["expected_properties"] == run["inserted_snapshots"] + run["skipped_existing"]


def test_source_count_anomaly_aborts_second_run_and_records_failed():
    repo = _repo()
    prior_date = date(2026, 9, 8)
    prior_as_of = datetime(2026, 9, 8, 12, tzinfo=BUSINESS_TZ)
    prior_snapshots = [
        replace(
            _snapshot(f"P{i}", as_of=NOW),
            snapshot_date_local=prior_date,
            as_of_local=prior_as_of,
            as_of_utc=to_utc(prior_as_of),
        )
        for i in range(10)
    ]
    prior = _persist(repo, prior_snapshots)
    assert prior.status == "COMPLETED"
    with pytest.raises(PersistenceError, match="SOURCE_COUNT_ANOMALY"):
        _persist(repo, [_snapshot("P1")])
    failed = repo.run_collection.find_one(
        {"snapshot_date_local": "2026-09-09", "status": "FAILED"}
    )
    assert failed is not None
    assert "SOURCE_COUNT_ANOMALY" in failed["error_summary"]
    assert repo.snapshot_collection.count_documents({"snapshot_date_local": "2026-09-09"}) == 0


def test_mark_partial_and_failed_are_auditable_statuses():
    repo = _repo()
    snapshot = _snapshot()
    run_id = _run_document(repo, snapshot)
    repo.mark_partial(run_id, error_summary=["test_partial"])
    assert repo.run_collection.find_one({"_id": run_id})["status"] == "PARTIAL"
    repo.mark_failed(run_id, error_summary=["test_failed"])
    failed = repo.run_collection.find_one({"_id": run_id})
    assert failed["status"] == "FAILED"
    assert failed["error_summary"] == ["test_failed"]


def test_partial_status_is_recorded_explicitly():
    repo = _repo()
    run_id = _run_document(repo, _snapshot())
    repo.mark_partial(run_id, error_summary=["partial"])
    assert repo.run_collection.find_one({"_id": run_id})["status"] == "PARTIAL"


def test_failed_status_is_recorded_explicitly():
    repo = _repo()
    run_id = _run_document(repo, _snapshot())
    repo.mark_failed(run_id, error_summary=["failed"])
    assert repo.run_collection.find_one({"_id": run_id})["status"] == "FAILED"


def test_completed_ledger_counts_are_consistent():
    repo = _repo()
    result = _persist(repo, [_snapshot()])
    run = repo.run_collection.find_one({"_id": result.run_id})
    assert run["status"] == "COMPLETED"
    assert run["expected_properties"] == run["inserted_snapshots"] + run["skipped_existing"]


def test_only_authorized_collections_are_touched():
    repo = _repo()
    _persist(repo, [_snapshot()])
    assert set(repo.db.list_collection_names()) == {SNAPSHOT_COLLECTION, RUN_COLLECTION}
