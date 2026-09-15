from __future__ import annotations

from datetime import datetime, timedelta, timezone

import mongomock
import pytest

import captacion_contact_identity as identity_module
import captacion_phone_reconciliation as reconciliation_module
from captacion_contact_identity import record_automatic_broker_evidence, record_human_feedback
from captacion_phone_reconciliation import (
    EVENT_TYPE,
    JOB_COLLECTION,
    STATUS_CANCELLED_CONFLICT,
    STATUS_COMPLETED,
    ensure_phone_reconciliation_indexes,
    recover_missing_phone_reconciliation_jobs,
    run_phone_reconciliation_iteration,
)
from config import Config


PHONE = "56911112222"


@pytest.fixture
def db(monkeypatch):
    client = mongomock.MongoClient()
    database = client["URLS"]
    monkeypatch.setattr(Config, "PHONE_LEARNING_ENABLED", True)
    monkeypatch.setattr(Config, "PHONE_RECONCILIATION_ENABLED", True)
    monkeypatch.setattr(Config, "PHONE_RECONCILIATION_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(Config, "PHONE_RECONCILIATION_PAGE_SIZE", 10)
    identity_module._INDEXES_READY = False
    reconciliation_module._INDEXES_READY = False
    reconciliation_module._INDEX_SETUP_NEXT_RETRY_AT = 0.0
    reconciliation_module._INDEX_SETUP_LAST_FAILURE_AT = 0.0
    return database


def _property(db, property_id, portal, *, assigned=False, state="NUEVO"):
    gestion = {"estado": state}
    if assigned:
        gestion.update(
            {
                "ejecutivo_id": f"agent-{property_id}",
                "ejecutivo_nombre": f"Agent {property_id}",
            }
        )
    document = {
        "_id": property_id,
        "listing_id": f"listing-{property_id}",
        "source_portal": portal,
        "telefono_normalizado": PHONE,
        "phone_normalized": PHONE,
        "classification": {"state": "INCIERTO"},
        "is_test": True,
        "test_type": "phone_reconciliation_canary",
        "gestion": gestion,
        "created_at": datetime.now(timezone.utc),
    }
    db[Config.CAPTACION_COLLECTION_NAME].insert_one(document)
    return document


def _human_event(event_id="human-1", property_id="cp-1"):
    return {
        "event_id": event_id,
        "event_type": "manual_decision_confirmed",
        "property_id": property_id,
        "actor_user_id": "human-agent",
        "actor_name_snapshot": "Human Agent",
        "source_system": "test",
        "occurred_at": datetime.now(timezone.utc),
    }


def _feedback(db, event_id="human-1", property_id="cp-1"):
    doc = db[Config.CAPTACION_COLLECTION_NAME].find_one({"_id": property_id})
    return record_human_feedback(
        db,
        property_doc=doc,
        event=_human_event(event_id, property_id),
        classification="CORREDOR",
    )


def test_new_human_transition_creates_one_job_and_worker_applies_matrix(db):
    _property(db, "cp-1", "chilepropiedades")
    _property(db, "yapo-1", "yapo", assigned=True, state="En gestión")
    toctoc = _property(db, "toctoc-1", "toctoc", assigned=True, state="En gestión")
    db["captacion_management_events"].insert_one(
        {
            "event_id": "prior-human-owner",
            "event_type": "management_confirmed",
            "property_id": toctoc["_id"],
            "commercial_result": "contacted",
            "commercially_valid": True,
            "credited": True,
            "actor_user_id": "human-agent",
        }
    )

    identity = _feedback(db)
    assert identity["status"] == "CORREDOR_CONFIRMED"
    assert db[JOB_COLLECTION].count_documents({}) == 1
    assert identity["phone_reconciliation_trigger_event_id"] == "human-1"

    result = run_phone_reconciliation_iteration(db, worker_id="worker-1")
    assert result["status"] == STATUS_COMPLETED
    job = db[JOB_COLLECTION].find_one({})
    assert job["status"] == STATUS_COMPLETED
    assert job["metrics"]["unassigned_blocked"] == 1
    assert job["metrics"]["assigned_unworked_blocked"] == 1
    assert job["metrics"]["previously_managed_audited"] == 1
    assert job["metrics"]["broker_reuse_prevented_pre_assignment"] == 1
    assert job["metrics"]["broker_reuse_prevented_by_reconciliation"] == 1

    cp = db[Config.CAPTACION_COLLECTION_NAME].find_one({"_id": "cp-1"})
    yapo = db[Config.CAPTACION_COLLECTION_NAME].find_one({"_id": "yapo-1"})
    managed = db[Config.CAPTACION_COLLECTION_NAME].find_one({"_id": "toctoc-1"})
    assert cp["gestion"]["estado"] == "Corredor"
    assert yapo["gestion"]["estado"] == "Corredor"
    assert managed["gestion"]["estado"] == "En gestión"
    assert cp["classification"]["state"] == "INCIERTO"
    assert yapo["gestion"]["ejecutivo_id"] == "agent-yapo-1"

    events = list(db["captacion_management_events"].find({"event_type": EVENT_TYPE}))
    assert len(events) == 3
    assert all(event["credited"] is False for event in events)
    assert all(event["contact_attempt"] is False for event in events)
    assert all(event["contact_effective"] is False for event in events)
    assert all(event["actor_user_id"] == "SYSTEM" for event in events)


def test_second_human_feedback_same_confirmed_identity_does_not_create_job(db):
    _property(db, "cp-1", "chilepropiedades")
    _feedback(db, "human-1")
    _feedback(db, "human-2")
    assert db[JOB_COLLECTION].count_documents({}) == 1


def test_backfill_and_automatic_evidence_do_not_create_jobs(db):
    _property(db, "cp-1", "chilepropiedades")
    db["captacion_contact_identity"].insert_one(
        {
            "_id": "backfill-identity",
            "identity_key": "phone:56933334444",
            "phone_normalized": "56933334444",
            "status": "CORREDOR_CONFIRMED",
            "classification": "CORREDOR",
            "evidence": [{"source": "HUMAN_BACKFILL", "classification": "CORREDOR"}],
        }
    )
    assert recover_missing_phone_reconciliation_jobs(db) == 0
    assert db[JOB_COLLECTION].count_documents({}) == 0

    result = record_automatic_broker_evidence(
        db,
        property_doc=db[Config.CAPTACION_COLLECTION_NAME].find_one({"_id": "cp-1"}),
        evidence_type="REGEX_SIGNAL",
        classifier="test",
    )
    assert result is not None
    assert db[JOB_COLLECTION].count_documents({}) == 0


def test_recovery_reconstructs_missing_job_without_touching_backfill(db):
    _property(db, "cp-1", "chilepropiedades")
    _feedback(db)
    db[JOB_COLLECTION].delete_many({})
    db["captacion_contact_identity"].update_one(
        {"phone_normalized": PHONE},
        {"$unset": {"phone_reconciliation_trigger_event_id": "", "phone_reconciliation_triggered_at": ""}},
    )
    assert recover_missing_phone_reconciliation_jobs(db) == 1
    assert db[JOB_COLLECTION].count_documents({}) == 1
    assert db["captacion_contact_identity"].find_one({"phone_normalized": PHONE}).get("phone_reconciliation_trigger_event_id") == "human-1"


def test_en_gestion_without_human_event_is_unworked(db):
    _property(db, "cp-1", "chilepropiedades", assigned=True, state="En gestión")
    _feedback(db)
    run_phone_reconciliation_iteration(db, worker_id="worker-1")
    row = db[Config.CAPTACION_COLLECTION_NAME].find_one({"_id": "cp-1"})
    assert row["gestion"]["estado"] == "Corredor"
    assert db["captacion_management_events"].find_one({"event_type": EVENT_TYPE})["action"] == "assigned_unworked_blocked"


def test_terminal_and_conflict_are_not_overwritten(db):
    _property(db, "cp-1", "chilepropiedades", assigned=True, state="Captado")
    _feedback(db)
    run_phone_reconciliation_iteration(db, worker_id="worker-1")
    row = db[Config.CAPTACION_COLLECTION_NAME].find_one({"_id": "cp-1"})
    assert row["gestion"]["estado"] == "Captado"
    assert db["captacion_management_events"].find_one({"event_type": EVENT_TYPE})["action"] == "terminal_audit_only"

    identity = db["captacion_contact_identity"].find_one({"phone_normalized": PHONE})
    job = db[JOB_COLLECTION].find_one({})
    db["captacion_contact_identity"].update_one({"_id": identity["_id"]}, {"$set": {"status": "CONFLICT", "classification": "CONFLICT"}})
    db[JOB_COLLECTION].update_one({"_id": job["_id"]}, {"$set": {"status": "pending", "attempts": 0}})
    result = run_phone_reconciliation_iteration(db, worker_id="worker-2")
    assert result["status"] == STATUS_CANCELLED_CONFLICT
    assert db[JOB_COLLECTION].find_one({"_id": job["_id"]})["status"] == STATUS_CANCELLED_CONFLICT
    assert db[Config.CAPTACION_COLLECTION_NAME].find_one({"_id": "cp-1"})["gestion"]["estado"] == "Captado"


def test_retry_and_two_workers_are_idempotent(db):
    _property(db, "cp-1", "chilepropiedades")
    _feedback(db)
    first = run_phone_reconciliation_iteration(db, worker_id="worker-1")
    assert first["status"] == STATUS_COMPLETED
    second = run_phone_reconciliation_iteration(db, worker_id="worker-2")
    assert second["processed"] is False
    assert db["captacion_management_events"].count_documents({"event_type": EVENT_TYPE}) == 1

    job = db[JOB_COLLECTION].find_one({})
    db[JOB_COLLECTION].update_one(
        {"_id": job["_id"]},
        {
            "$set": {
                "status": "processing",
                "lease_owner": "dead-worker",
                "lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
            }
        },
    )
    recovered = run_phone_reconciliation_iteration(db, worker_id="worker-3")
    assert recovered["status"] == STATUS_COMPLETED
    assert db["captacion_management_events"].count_documents({"event_type": EVENT_TYPE}) == 1


def test_indexes_and_exact_portal_scope(db):
    assert ensure_phone_reconciliation_indexes(db) is True
    _property(db, "outside-1", "otroportal")
    _property(db, "cp-1", "chilepropiedades")
    _feedback(db)
    run_phone_reconciliation_iteration(db, worker_id="worker-1")
    assert db["captacion_management_events"].count_documents({"event_type": EVENT_TYPE}) == 1
    assert db[Config.CAPTACION_COLLECTION_NAME].find_one({"_id": "outside-1"})["gestion"]["estado"] == "NUEVO"


def test_equivalent_product_index_is_reused_by_key_and_options(db, caplog):
    properties = db[Config.CAPTACION_COLLECTION_NAME]
    properties.create_index(
        [("telefono_normalizado", 1)],
        name="idx_captacion_phone_normalized",
    )

    with caplog.at_level("INFO"):
        assert ensure_phone_reconciliation_indexes(db) is True

    indexes = {index["name"]: index for index in properties.list_indexes()}
    assert "idx_captacion_phone_normalized" in indexes
    assert "captacion_telefono_normalizado" not in indexes
    assert "index_reused existing=idx_captacion_phone_normalized" in caplog.text


def test_repeated_startup_is_idempotent_and_keeps_index_names(db):
    assert ensure_phone_reconciliation_indexes(db) is True
    first = {
        collection_name: [index["name"] for index in db[collection_name].list_indexes()]
        for collection_name in (
            JOB_COLLECTION,
            "captacion_management_events",
            Config.CAPTACION_COLLECTION_NAME,
        )
    }
    assert ensure_phone_reconciliation_indexes(db) is True
    second = {
        collection_name: [index["name"] for index in db[collection_name].list_indexes()]
        for collection_name in first
    }
    assert second == first


def test_existing_outbox_indexes_with_product_names_are_reused(db, caplog):
    jobs = db[JOB_COLLECTION]
    jobs.create_index(
        [("identity_id", 1), ("human_feedback_event_id", 1)],
        unique=True,
        name="product_identity_event",
    )
    jobs.create_index(
        [("status", 1), ("lease_expires_at", 1), ("created_at", 1)],
        name="product_claim",
    )
    jobs.create_index([("job_key", 1)], unique=True, name="product_job_key")
    events = db["captacion_management_events"]
    events.create_index(
        [("reconciliation_dedup_key", 1)],
        unique=True,
        sparse=True,
        name="product_event_dedup",
    )

    with caplog.at_level("INFO"):
        assert ensure_phone_reconciliation_indexes(db) is True

    assert {index["name"] for index in jobs.list_indexes()} == {
        "_id_",
        "product_identity_event",
        "product_claim",
        "product_job_key",
    }
    assert "product_event_dedup" in {index["name"] for index in events.list_indexes()}
    assert "index_created name=phone_reconciliation_identity_event" not in caplog.text


def test_incompatible_index_fails_without_destroying_existing_index(db):
    properties = db[Config.CAPTACION_COLLECTION_NAME]
    properties.create_index(
        [("phone_normalized", 1)],
        unique=True,
        name="captacion_phone_normalized",
    )

    assert ensure_phone_reconciliation_indexes(db) is False

    indexes = {index["name"]: index for index in properties.list_indexes()}
    assert indexes["captacion_phone_normalized"]["unique"] is True
    assert "captacion_telefono_normalizado" not in indexes
