from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import mongomock
from bson import ObjectId

from captacion_distribution import (
    MongoDistributionLock,
    assign_captacion_candidate_atomically,
    distribution_age_decision,
    distribution_age_sort_key,
    open_workloads,
    resolve_manual_batch_size,
)


def test_three_concurrent_lock_attempts_have_one_winner():
    db = mongomock.MongoClient()["test"]

    def attempt(index):
        lock = MongoDistributionLock(
            db,
            run_id=f"run-{index}",
            trigger_source="test",
        )
        return lock, lock.acquire()

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(attempt, range(3)))

    assert sum(acquired for _lock, acquired in results) == 1
    for lock, acquired in results:
        if acquired:
            lock.release()


def test_expired_lock_is_recovered_atomically():
    db = mongomock.MongoClient()["test"]
    db["captacion_distribution_locks"].insert_one({
        "_id": "global",
        "owner": "dead-process",
        "run_id": "dead-run",
        "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
    })

    lock = MongoDistributionLock(db, run_id="new-run", trigger_source="test")
    assert lock.acquire() is True
    assert db["captacion_distribution_locks"].find_one({"_id": "global"})["run_id"] == "new-run"
    lock.release()


def test_manual_batch_requires_explicit_large_opt_in():
    assert resolve_manual_batch_size(14) == 14
    try:
        resolve_manual_batch_size(15)
    except ValueError as exc:
        assert "allow-large-batch" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("large manual batch must require explicit opt-in")
    assert resolve_manual_batch_size(15, allow_large=True) == 15


def test_operational_age_policy_has_conservative_boundaries():
    now = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)

    recent = distribution_age_decision(
        {"created_at": now - timedelta(days=20)}, now=now
    )
    reserve = distribution_age_decision(
        {"created_at": now - timedelta(days=45)}, now=now
    )
    stale = distribution_age_decision(
        {"created_at": now - timedelta(days=75)}, now=now
    )
    missing = distribution_age_decision({}, now=now)

    assert recent["eligible"] is True
    assert recent["bucket"] == "0_30_days"
    assert reserve["eligible"] is True
    assert reserve["bucket"] == "31_60_days"
    assert stale["eligible"] is False
    assert stale["bucket"] == ">60_days"
    assert stale["reason"] == "STALE_FOR_AUTO_DISTRIBUTION"
    assert missing["eligible"] is False
    assert missing["reason"] == "CAPTURE_DATE_UNAVAILABLE"


def test_recent_age_bucket_precedes_reserve_for_same_classification():
    now = datetime.now(timezone.utc)
    recent = {"created_at": now - timedelta(days=10)}
    reserve = {"created_at": now - timedelta(days=45)}

    assert distribution_age_sort_key(recent) < distribution_age_sort_key(reserve)


def test_stale_new_assignment_is_blocked_without_mutating_classification():
    db = mongomock.MongoClient()["test"]
    coll = db["propiedades_captacion"]
    agent_id = str(ObjectId())
    db["usuarios"].insert_one({
        "_id": ObjectId(agent_id),
        "rol": "agente",
        "is_active": True,
    })
    now = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
    candidate = {
        "_id": ObjectId(),
        "origen": "toctoc",
        "listing_id": "toctoc-stale",
        "created_at": now - timedelta(days=75),
        "title": "Casa en venta",
        "description": "Descripción suficiente",
        "seller_name": "Particular",
        "classification": {
            "state": "INCIERTO",
            "owner_probability": 0.41,
            "source": "rules",
            "assignment_ready": True,
            "exclude_from_assignment": False,
        },
        "comuna_slug": "santiago",
        "gestion": {"estado": "NUEVO", "ejecutivo_id": None},
    }
    coll.insert_one(candidate)

    status = assign_captacion_candidate_atomically(
        db,
        coll,
        db["captacion_management_events"],
        candidate,
        {"id": agent_id, "name": "Agent", "comunas_interes_norm": ["santiago"]},
        now=now,
        max_per_agent=2,
    )

    stored = coll.find_one({"_id": candidate["_id"]})
    assert status == "stale_for_auto_distribution"
    assert stored["classification"] == candidate["classification"]
    assert stored["gestion"] == candidate["gestion"]


def test_duplicate_is_not_open_capacity():
    db = mongomock.MongoClient()["test"]
    coll = db["propiedades_captacion"]
    agent_id = str(ObjectId())
    coll.insert_many([
        {"gestion": {"ejecutivo_id": agent_id, "estado": "Duplicado"}},
        {"gestion": {"ejecutivo_id": agent_id, "estado": "NUEVO"}},
        {"gestion": {"ejecutivo_id": agent_id}},
    ])

    assert open_workloads(db, [agent_id]) == {agent_id: 2}


def test_capacity_counts_only_non_terminal_open_work():
    db = mongomock.MongoClient()["test"]
    coll = db["propiedades_captacion"]
    agent_id = str(ObjectId())
    db["usuarios"].insert_one({
        "_id": ObjectId(agent_id),
        "rol": "agente",
        "is_active": True,
    })
    coll.insert_many(
        {"gestion": {"ejecutivo_id": agent_id, "estado": "NUEVO"}}
        for _ in range(350)
    )
    candidate = {
        "_id": ObjectId(),
        "origen": "yapo",
        "classification": {"state": "INCIERTO"},
        "comuna_slug": "santiago",
        "gestion": {"estado": "NUEVO", "ejecutivo_id": None},
    }
    coll.insert_one(candidate)

    status = assign_captacion_candidate_atomically(
        db,
        coll,
        db["captacion_management_events"],
        candidate,
        {"id": agent_id, "name": "Agent", "comunas_interes_norm": ["santiago"]},
        max_per_agent=2,
    )

    assert status == "capacity"
    assert coll.find_one({"_id": candidate["_id"]})["gestion"]["ejecutivo_id"] is None


def test_phone_race_is_revalidated_before_atomic_write(monkeypatch):
    import captacion_distribution as distribution
    from config import Config

    db = mongomock.MongoClient()["test"]
    coll = db["propiedades_captacion"]
    agent_id = str(ObjectId())
    db["usuarios"].insert_one({
        "_id": ObjectId(agent_id),
        "rol": "agente",
        "is_active": True,
    })
    candidate = {
        "_id": ObjectId(),
        "origen": "chilepropiedades",
        "listing_id": "cp-phone-race",
        "created_at": datetime.now(timezone.utc) - timedelta(days=75),
        "title": "Casa en venta",
        "description": "Descripción suficiente",
        "seller_name": "Particular",
        "classification": {"state": "INCIERTO"},
        "comuna_slug": "santiago",
        "gestion": {"estado": "NUEVO", "ejecutivo_id": None},
    }
    coll.insert_one(candidate)
    monkeypatch.setattr(Config, "PHONE_LEARNING_ENABLED", True)
    monkeypatch.setattr(distribution, "phone_learning_global_lookup_enabled", lambda: True)
    monkeypatch.setattr(
        distribution,
        "get_contact_identity_evidence",
        lambda _db, _doc: {"status": "CORREDOR_CONFIRMED", "phone_normalized": "56912345678"},
    )

    status = assign_captacion_candidate_atomically(
        db,
        coll,
        db["captacion_management_events"],
        candidate,
        {"id": agent_id, "name": "Agent", "comunas_interes_norm": ["santiago"]},
        max_per_agent=2,
    )

    assert status == "phone"
    assert coll.find_one({"_id": candidate["_id"]})["gestion"]["ejecutivo_id"] is None


def test_global_distributor_is_portal_agnostic_and_bounded(monkeypatch):
    from config import Config
    import chatbot.storage as storage

    db = mongomock.MongoClient()["test"]
    monkeypatch.setattr(storage, "get_db", lambda: db)
    import api_captacion

    users = db["usuarios"]
    for index in range(10):
        users.insert_one({
            "_id": ObjectId(),
            "nombre": f"Agent {index}",
            "rol": "agente",
            "is_active": True,
            "comunas_interes_norm": ["santiago"],
        })
    coll = db[Config.CAPTACION_COLLECTION_NAME]
    for index in range(3000):
        portal = ("chilepropiedades", "yapo", "toctoc")[index % 3]
        coll.insert_one({
            "_id": ObjectId(),
            "origen": portal,
            "listing_id": f"listing-{index}",
            "title": "Casa en venta",
            "description": "Descripción suficiente",
            "comuna_slug": "santiago",
            "created_at": datetime.now(timezone.utc),
            "classification": {"state": "INCIERTO", "source": "rules"},
            "gestion": {"estado": "NUEVO", "ejecutivo_id": None},
        })

    metrics = []
    monkeypatch.setattr(api_captacion, "get_db", lambda: db)
    monkeypatch.setattr(api_captacion, "_record_distribution_metrics", lambda _db, value: metrics.append(value))
    monkeypatch.setattr(Config, "PHONE_LEARNING_ENABLED", False)

    preview = api_captacion.distribute_sourced_leads(trigger_source="test-dry-run", dry_run=True)
    assert preview["batch_selected"] == 14
    assert len(preview["selected_preview"]) == 14
    assert coll.count_documents({"gestion.ejecutivo_id": {"$ne": None}}) == 0

    assert api_captacion.distribute_sourced_leads(trigger_source="test") == 14
    assigned = list(coll.find({"gestion.ejecutivo_id": {"$ne": None}}))
    assert len(assigned) == 14
    per_agent = {}
    for row in assigned:
        agent_id = row["gestion"]["ejecutivo_id"]
        per_agent[agent_id] = per_agent.get(agent_id, 0) + 1
    assert max(per_agent.values()) <= 2
    assert {row["origen"] for row in assigned} == {"chilepropiedades", "yapo", "toctoc"}
    assert metrics[-1]["lock_acquired"] is True
    assert metrics[-1]["assigned"] == 14
