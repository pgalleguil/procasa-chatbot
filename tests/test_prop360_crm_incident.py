import asyncio
from datetime import datetime, timezone

import mongomock
import pytest
from bson import ObjectId

from chatbot import crm_metrics
from chatbot import prop360_poll_loop
from chatbot import ingest_service
from chatbot.ingest_service import IngestResult, LeadEvent
from scraping_convecta import extractor_prop360


def test_new_prop360_event_creates_fresh_cycle_for_same_executive():
    db = mongomock.MongoClient().incident
    lead = {"_id": "lead-1", "lead_temperature_effective": "COLD"}
    db["leads"].insert_one(lead.copy())

    old = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="agent-1", assigned_by="test",
        reason="lead_created", assigned_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": old["assignment_cycle_id"]},
        {"$set": {"first_valid_management_at": datetime(2026, 6, 2, tzinfo=timezone.utc)}},
    )
    new = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="agent-1", assigned_by="prop360_extractor",
        reason="lead_created", assigned_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        property_code="6636", force_new=True, source_system="prop360", source_event_id="11290",
    )
    assert new["assignment_cycle_id"] != old["assignment_cycle_id"]
    assert new.get("first_valid_management_at") is None
    prior_assigned_at = db["crm_assignment_cycles"].find_one(
        {"assignment_cycle_id": old["assignment_cycle_id"]}
    )["assigned_at"]
    assert crm_metrics.coerce_utc_datetime(new["assigned_at"]) > crm_metrics.coerce_utc_datetime(prior_assigned_at)
    assert db["crm_assignment_cycles"].find_one(
        {"assignment_cycle_id": old["assignment_cycle_id"]}
    )["cycle_status"] == "closed"
    assert new["sla_started_at"] == crm_metrics.commercial_sla_start_at(new["assigned_at"])
    assert new["source_event_id"] == "11290"

    retry = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="agent-1", assigned_by="prop360_extractor",
        reason="lead_created", assigned_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        property_code="6636", force_new=True, source_system="prop360", source_event_id="11290",
    )
    assert retry["assignment_cycle_id"] == new["assignment_cycle_id"]
    assert db["crm_assignment_cycles"].count_documents({"lead_id": "lead-1"}) == 2


def test_new_prop360_event_routes_new_cycle_to_changed_executive():
    db = mongomock.MongoClient().incident
    lead = {"_id": "lead-2", "lead_temperature_effective": "COLD"}
    db["leads"].insert_one(lead.copy())
    previous = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="agent-1", assigned_by="test",
        reason="lead_created", assigned_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    current = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="agent-2", assigned_by="prop360_extractor",
        reason="lead_created", assigned_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        property_code="17258", force_new=True, source_system="prop360", source_event_id="11104",
    )
    assert current["assigned_to_user_id"] == "agent-2"
    assert current["assignment_cycle_id"] != previous["assignment_cycle_id"]
    assert db["crm_assignment_cycles"].find_one({"assignment_cycle_id": previous["assignment_cycle_id"]})["cycle_status"] == "closed"


def test_canonical_crm_recent_filter_selects_new_prop360_cycle():
    db = mongomock.MongoClient().incident
    lead = {"_id": "virna", "lead_temperature_effective": "COLD"}
    db["leads"].insert_one(lead.copy())
    prior = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="rocio", assigned_by="migration",
        reason="historical_reconciliation", assigned_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        property_code="OLD-PROPERTY",
    )
    crm_filter = {
        "lead_id": lead["_id"], "unassigned_at": None, "notification_eligible": True,
        "reason": {"$in": ["inbound_message", "lead_created", "manual_lead_created"]},
        "cycle_origin": {"$in": ["inbound_message", "manual_lead"]},
    }
    assert db["crm_assignment_cycles"].count_documents(crm_filter) == 0

    event_at = datetime(2026, 10, 2, 13, 51, tzinfo=timezone.utc)
    current = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="rocio", assigned_by="prop360_extractor",
        reason="lead_created", assigned_at=event_at, property_code="6636", force_new=True,
        source_system="prop360", source_event_id="11290",
    )
    visible = list(db["crm_assignment_cycles"].find(crm_filter).sort("assigned_at", -1))
    assert current["schema_version"] == "crm_assignment_cycle_v1"
    assert current["reason"] == "lead_created"
    assert current["cycle_origin"] == "inbound_message"
    assert current["notification_eligible"] is True
    assert current["property_code"] == "6636"
    assert current["assignment_cycle_id"] != prior["assignment_cycle_id"]
    assert len(visible) == 1 and visible[0]["assignment_cycle_id"] == current["assignment_cycle_id"]
    assert crm_metrics.coerce_utc_datetime(visible[0]["assigned_at"]) == event_at


def test_virna_fixture_new_opportunity_cycle_and_notification_are_idempotent(monkeypatch):
    db = mongomock.MongoClient().incident
    db["lead_ingest_events"].create_index([("source_system", 1), ("source_event_id", 1)], unique=True)
    lead_id = ObjectId()
    lead = {
        "_id": lead_id, "phone": "+56988061610", "ejecutivo_asignado": "Rocío Aliaga",
        "lead_temperature_effective": "COLD",
        "prospecto": {"nombre": "Virna", "codigo": "ANTERIOR", "propiedades_vistas": ["ANTERIOR"], "ejecutivo": "Rocío Aliaga"},
        "lifecycle": {"assigned_at": datetime(2026, 6, 10, 16, 23, tzinfo=timezone.utc)},
        "messages": [], "source_events": [],
    }
    db["leads"].insert_one(lead.copy())
    db["usuarios"].insert_one({"_id": ObjectId(), "nombre": "Rocío Aliaga"})
    old_cycle = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id=str(db["usuarios"].find_one({})["_id"]),
        assigned_by="previous_event", reason="lead_created",
        assigned_at=datetime(2026, 6, 10, 16, 23, tzinfo=timezone.utc), property_code="ANTERIOR",
        source_system="prop360", source_event_id="older-event",
    )
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": old_cycle["assignment_cycle_id"]},
        {"$set": {"first_valid_management_at": datetime(2026, 6, 10, 16, 23, tzinfo=timezone.utc)}},
    )
    monkeypatch.setattr(ingest_service, "get_db", lambda: db)
    monkeypatch.setattr(extractor_prop360, "get_db", lambda: db)
    monkeypatch.setattr(ingest_service, "_enrich_from_cartera", lambda _db, code: {
        "property_found": True, "comuna": "Pinto", "region": "Ñuble", "ejecutivo_ficha": "Rocío Aliaga",
    })
    monkeypatch.setattr(ingest_service, "find_responsible_executive", lambda **kwargs: ("Rocío Aliaga", "+56911111111", "test"))
    event_at = datetime(2026, 10, 2, 13, 51, tzinfo=timezone.utc)
    monkeypatch.setattr(ingest_service, "get_next_business_slot", lambda _now: event_at)
    monkeypatch.setattr(ingest_service, "log_event", lambda *args, **kwargs: None)

    event = LeadEvent("prop360", "11290", phone="+56988061610", name="Virna", message="Interesada", property_code="6636")
    result = ingest_service.ingest_lead_event(event)
    assert result.status == "updated" and result.lead_id == str(lead_id)
    assert db["leads"].count_documents({}) == 1
    persisted = db["leads"].find_one({"_id": lead_id})
    assert sum(str(item.get("source_event_id")) == "11290" for item in persisted["source_events"]) == 1

    def enqueue_digest(_db, *, lead, cycle):
        notification = {"assignment_cycle_ids": [cycle["assignment_cycle_id"]], "state": "pending"}
        _db["crm_notifications_v1"].insert_one(notification)
        return notification

    extractor = extractor_prop360.Prop360Extractor("", "", dry_run=False)
    extractor.normalize_lead = lambda raw: raw
    enqueue = extractor._enqueue_notification
    raw = {
        "idContacto": "11290", "contNombre": "Virna", "contFono": "+56988061610",
        "contMsg": "Interesada", "codigo": "6636", "medio": "Yapo",
    }

    def fail_before_cycle(*args, **kwargs):
        raise RuntimeError("simulated failure before cycle creation")

    monkeypatch.setattr(extractor, "_enqueue_notification", fail_before_cycle)
    with pytest.raises(RuntimeError, match="before cycle creation"):
        extractor.process_lead(raw)
    assert db["crm_assignment_cycles"].count_documents({"source_system": "prop360", "source_event_id": "11290"}) == 0

    monkeypatch.setattr("chatbot.crm_non_hot_digest.accumulate_non_hot_lead", enqueue_digest)
    monkeypatch.setattr(extractor, "_enqueue_notification", enqueue)
    extractor.process_lead(raw)

    cycle = db["crm_assignment_cycles"].find_one({"source_system": "prop360", "source_event_id": "11290"})
    assert db["crm_assignment_cycles"].count_documents({}) == 2
    assert db["crm_notifications_v1"].count_documents({}) == 1
    assert cycle["property_code"] == "6636"
    assert cycle.get("first_valid_management_at") is None
    assert cycle["assigned_to_user_id"] == old_cycle["assigned_to_user_id"]
    assert crm_metrics.coerce_utc_datetime(cycle["assigned_at"]) == event_at

    cycle_assigned_at = cycle["assigned_at"]
    extractor.process_lead(raw)
    assert extractor.metrics["duplicates_skipped"] == 1
    assert db["crm_assignment_cycles"].count_documents({}) == 2
    assert db["crm_notifications_v1"].count_documents({}) == 1
    after = db["crm_assignment_cycles"].find_one({"source_system": "prop360", "source_event_id": "11290"})
    assert after["assigned_at"] == cycle_assigned_at


def test_poll_loop_runs_on_consecutive_hourly_slots(monkeypatch):
    now = {"hour": 9}
    runs = []
    monkeypatch.setattr(prop360_poll_loop, "_feature_enabled", lambda: True)
    monkeypatch.setattr(prop360_poll_loop, "_in_business_hours", lambda: True)
    monkeypatch.setattr(prop360_poll_loop, "_update_health_heartbeat", lambda **kwargs: None)
    monkeypatch.setattr(prop360_poll_loop, "_persist_cycle_status_off_loop", lambda *a, **k: None)

    async def advance_slot(_interval):
        now["hour"] += 1

    async def fake_to_thread(func):
        try:
            return func()
        except StopLoop:
            raise

    class StopLoop(BaseException):
        pass

    def cycle():
        runs.append(now["hour"])
        if now["hour"] == 12:
            raise StopLoop()
        return {"status": "ok"}

    monkeypatch.setattr(prop360_poll_loop, "_sleep_until_next_slot", advance_slot)
    monkeypatch.setattr(prop360_poll_loop, "run_prop360_poll_cycle", cycle)
    monkeypatch.setattr(prop360_poll_loop.asyncio, "to_thread", fake_to_thread)

    try:
        asyncio.run(prop360_poll_loop.prop360_poll_loop(3600))
    except StopLoop:
        pass
    assert runs == [9, 10, 11, 12]


def test_updated_ingest_result_enqueues_without_nonexistent_action(monkeypatch):
    extractor = extractor_prop360.Prop360Extractor("", "", dry_run=False)
    enqueued = []
    monkeypatch.setattr(extractor, "normalize_lead", lambda raw: raw)
    monkeypatch.setattr(extractor, "_is_duplicate", lambda raw: False)
    monkeypatch.setattr(extractor, "_enqueue_notification", lambda *args: enqueued.append(args))
    monkeypatch.setattr(
        extractor_prop360, "ingest_lead_event",
        lambda _event: IngestResult(status="updated", lead_id="lead-1", executive="Rocío", assignment_changed=False),
    )

    extractor.process_lead({"idContacto": 11290, "contNombre": "Virna", "contFono": "+56912345678", "codigo": "6636"})

    assert len(enqueued) == 1
    assert extractor.metrics["leads_updated"] == 1


def test_ingest_partial_retry_resumes_and_new_property_reuses_canonical_lead(monkeypatch):
    db = mongomock.MongoClient().incident
    db["lead_ingest_events"].create_index([("source_system", 1), ("source_event_id", 1)], unique=True)
    monkeypatch.setattr(ingest_service, "get_db", lambda: db)
    monkeypatch.setattr(ingest_service, "_enrich_from_cartera", lambda _db, code: {
        "property_found": True, "comuna": "Pinto", "region": "Ñuble", "ejecutivo_ficha": "Rocío",
    })
    monkeypatch.setattr(ingest_service, "find_responsible_executive", lambda **kwargs: ("Rocío", "+56911111111", "test"))
    monkeypatch.setattr(ingest_service, "get_next_business_slot", lambda now: datetime(2026, 10, 2, 13, tzinfo=timezone.utc))
    monkeypatch.setattr(ingest_service, "log_event", lambda *args, **kwargs: None)

    first = LeadEvent("prop360", "11290", phone="+56912345678", name="Virna", message="Interesada", property_code="6636")
    created = ingest_service.ingest_lead_event(first)
    assert created.status == "created"
    lead_id = created.lead_id
    db["lead_ingest_events"].update_one(
        {"source_system": "prop360", "source_event_id": "11290"},
        {"$set": {"status": "lead_persisted"}},
    )
    before_retry = db["leads"].find_one({"_id": db["leads"].find_one({})["_id"]})
    resumed = ingest_service.ingest_lead_event(first)
    assert resumed.status == "resume"
    after_retry = db["leads"].find_one({"_id": before_retry["_id"]})
    assert len(after_retry["source_events"]) == 1
    assert len(after_retry["messages"]) == 1

    second = LeadEvent("prop360", "11291", phone="+56912345678", name="Virna", message="Otra oportunidad", property_code="6636")
    updated = ingest_service.ingest_lead_event(second)
    assert updated.status == "updated"
    assert updated.lead_id == lead_id
    assert len(db["leads"].find_one({"_id": before_retry["_id"]})["source_events"]) == 2

    db["lead_ingest_events"].update_one(
        {"source_system": "prop360", "source_event_id": "11290"},
        {"$set": {"status": "completed"}},
    )
    duplicate = ingest_service.ingest_lead_event(first)
    assert duplicate.status == "duplicate_event"


def test_notification_enqueue_retry_reuses_event_cycle_and_notification(monkeypatch):
    db = mongomock.MongoClient().incident
    lead_id = ObjectId()
    lead = {
        "_id": lead_id, "phone": "+56912345678", "prospecto": {"nombre": "Virna", "codigo": "6636"},
        "ejecutivo_asignado": "Rocío", "lead_temperature_effective": "COLD",
        "lifecycle": {"assigned_at": datetime(2026, 10, 2, 13, tzinfo=timezone.utc)},
    }
    db["leads"].insert_one(lead.copy())
    db["usuarios"].insert_one({"_id": ObjectId(), "nombre": "Rocío"})
    db["lead_ingest_events"].insert_one({"source_system": "prop360", "source_event_id": "11290", "status": "lead_persisted", "lead_id": lead_id})
    monkeypatch.setattr(extractor_prop360, "get_db", lambda: db)

    def enqueue_digest(_db, *, lead, cycle):
        notification = {"assignment_cycle_ids": [cycle["assignment_cycle_id"]], "state": "pending"}
        _db["crm_notifications_v1"].insert_one(notification)
        return notification

    monkeypatch.setattr("chatbot.crm_non_hot_digest.accumulate_non_hot_lead", enqueue_digest)
    instance = extractor_prop360.Prop360Extractor("", "", dry_run=False)
    event = LeadEvent("prop360", "11290", name="Virna", property_code="6636", message="Interesada")
    raw = {"idContacto": "11290"}
    instance._enqueue_notification(str(lead_id), "Rocío", event, raw)
    instance._enqueue_notification(str(lead_id), "Rocío", event, raw)

    assert db["crm_assignment_cycles"].count_documents({"source_system": "prop360", "source_event_id": "11290"}) == 1
    assert db["crm_notifications_v1"].count_documents({}) == 1
    assert db["lead_ingest_events"].find_one({"source_system": "prop360", "source_event_id": "11290"})["status"] == "completed"


def test_hot_notification_retry_is_cycle_idempotent(monkeypatch):
    db = mongomock.MongoClient().incident
    lead_id = ObjectId()
    lead = {
        "_id": lead_id, "phone": "+56912345678", "prospecto": {"nombre": "Virna", "codigo": "6636"},
        "ejecutivo_asignado": "Rocío", "lead_temperature_effective": "HOT",
        "lifecycle": {"assigned_at": datetime(2026, 10, 2, 13, tzinfo=timezone.utc)},
    }
    db["leads"].insert_one(lead.copy())
    db["usuarios"].insert_one({"_id": ObjectId(), "nombre": "Rocío"})
    db["lead_ingest_events"].insert_one({"source_system": "prop360", "source_event_id": "11290", "status": "lead_persisted", "lead_id": lead_id})
    monkeypatch.setattr(extractor_prop360, "get_db", lambda: db)
    monkeypatch.setattr("chatbot.lead_router.get_executive_phone", lambda _name: "+56911111111")
    calls = []

    def enqueue_hot(_db, **kwargs):
        calls.append(kwargs)
        cycle = _db["crm_assignment_cycles"].find_one({"source_system": "prop360", "source_event_id": "11290"})
        notification = {"assignment_cycle_id": cycle["assignment_cycle_id"], "state": "pending"}
        _db["crm_notifications_v1"].insert_one(notification)
        return {"notification": notification}

    monkeypatch.setattr("chatbot.crm_hot_delivery.assign_and_enqueue_hot", enqueue_hot)
    instance = extractor_prop360.Prop360Extractor("", "", dry_run=False)
    event = LeadEvent("prop360", "11290", name="Virna", property_code="6636", message="Quiero visitar")
    raw = {"idContacto": "11290"}
    instance._enqueue_notification(str(lead_id), "Rocío", event, raw)
    instance._enqueue_notification(str(lead_id), "Rocío", event, raw)

    assert len(calls) == 1
    assert db["crm_assignment_cycles"].count_documents({"source_system": "prop360", "source_event_id": "11290"}) == 1
    assert db["crm_notifications_v1"].count_documents({}) == 1


def test_notification_retry_after_cycle_persisted_completes_missing_digest(monkeypatch):
    db = mongomock.MongoClient().incident
    lead_id = ObjectId()
    lead = {
        "_id": lead_id, "phone": "+56912345678", "prospecto": {"nombre": "Virna", "codigo": "6636"},
        "ejecutivo_asignado": "Rocío", "lead_temperature_effective": "COLD",
        "lifecycle": {"assigned_at": datetime(2026, 10, 2, 13, tzinfo=timezone.utc)},
    }
    db["leads"].insert_one(lead.copy())
    db["usuarios"].insert_one({"_id": ObjectId(), "nombre": "Rocío"})
    db["lead_ingest_events"].insert_one({"source_system": "prop360", "source_event_id": "11290", "status": "lead_persisted", "lead_id": lead_id})
    monkeypatch.setattr(extractor_prop360, "get_db", lambda: db)
    instance = extractor_prop360.Prop360Extractor("", "", dry_run=False)
    event = LeadEvent("prop360", "11290", name="Virna", property_code="6636", message="Interesada")
    raw = {"idContacto": "11290"}

    def fail_digest(*args, **kwargs):
        raise RuntimeError("simulated queue failure")

    monkeypatch.setattr("chatbot.crm_non_hot_digest.accumulate_non_hot_lead", fail_digest)
    with pytest.raises(RuntimeError, match="simulated queue failure"):
        instance._enqueue_notification(str(lead_id), "Rocío", event, raw)
    assert db["crm_assignment_cycles"].count_documents({"source_system": "prop360", "source_event_id": "11290"}) == 1

    def enqueue_digest(_db, *, lead, cycle):
        notification = {"assignment_cycle_ids": [cycle["assignment_cycle_id"]], "state": "pending"}
        _db["crm_notifications_v1"].insert_one(notification)
        return notification

    monkeypatch.setattr("chatbot.crm_non_hot_digest.accumulate_non_hot_lead", enqueue_digest)
    instance._enqueue_notification(str(lead_id), "Rocío", event, raw)
    assert db["crm_assignment_cycles"].count_documents({"source_system": "prop360", "source_event_id": "11290"}) == 1
    assert db["crm_notifications_v1"].count_documents({}) == 1
    assert db["lead_ingest_events"].find_one({"source_system": "prop360", "source_event_id": "11290"})["status"] == "completed"
