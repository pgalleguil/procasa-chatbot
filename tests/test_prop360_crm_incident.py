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


def test_prop360_repeat_for_same_property_reuses_whatsapp_owner_and_management():
    db = mongomock.MongoClient().incident
    lead = {"_id": "lead-wa-first", "lead_temperature_effective": "COLD"}
    db["leads"].insert_one(lead.copy())
    first = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="hernan", assigned_by="commercial_intake",
        reason="inbound_message", assigned_at=datetime(2026, 10, 19, 13, 56, tzinfo=timezone.utc),
        property_code="6732", source_system="whatsapp", source_event_id="wa-89602740",
    )
    managed_at = datetime(2026, 10, 19, 13, 59, tzinfo=timezone.utc)
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": first["assignment_cycle_id"]},
        {"$set": {"first_valid_management_at": managed_at}},
    )

    repeated = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="maria-paz", assigned_by="prop360_extractor",
        reason="lead_created", assigned_at=datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc),
        property_code="6732", force_new=True, source_system="prop360", source_event_id="12272",
    )
    stored = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": first["assignment_cycle_id"]})
    assert repeated["assignment_cycle_id"] == first["assignment_cycle_id"]
    assert repeated["assigned_to_user_id"] == "hernan"
    assert crm_metrics.coerce_utc_datetime(stored["first_valid_management_at"]) == managed_at
    assert stored["cycle_status"] == "active"
    assert db["crm_assignment_cycles"].count_documents({"lead_id": lead["_id"]}) == 1


def test_prop360_first_then_whatsapp_reuses_the_same_cycle():
    db = mongomock.MongoClient().incident
    lead = {"_id": "lead-prop-first", "lead_temperature_effective": "COLD"}
    db["leads"].insert_one(lead.copy())
    first = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="hernan", assigned_by="prop360_extractor",
        reason="lead_created", assigned_at=datetime(2026, 10, 19, 13, 56, tzinfo=timezone.utc),
        property_code="6732", source_system="prop360", source_event_id="12272",
    )
    repeated = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="maria-paz", assigned_by="commercial_intake",
        reason="inbound_message", assigned_at=datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc),
        property_code="6732", source_system="whatsapp", source_event_id="wa-89602740",
    )
    assert repeated["assignment_cycle_id"] == first["assignment_cycle_id"]
    assert repeated["assigned_to_user_id"] == "hernan"
    assert db["crm_assignment_cycles"].count_documents({"lead_id": lead["_id"]}) == 1


def test_closed_prior_cycle_stays_immutable_when_new_opportunity_starts():
    db = mongomock.MongoClient().incident
    lead = {"_id": "lead-closed-prior", "lead_temperature_effective": "COLD"}
    db["leads"].insert_one(lead.copy())
    old = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="hernan", assigned_by="commercial_intake",
        reason="inbound_message", assigned_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        property_code="OLD-PROPERTY",
    )
    managed_at = datetime(2026, 6, 2, tzinfo=timezone.utc)
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": old["assignment_cycle_id"]},
        {"$set": {"first_valid_management_at": managed_at, "cycle_status": "closed",
                  "unassigned_at": managed_at, "closed_at": managed_at}},
    )
    current = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="hernan", assigned_by="commercial_intake",
        reason="inbound_message", assigned_at=datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc),
        property_code="6732", source_system="prop360", source_event_id="12272",
    )
    prior = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": old["assignment_cycle_id"]})
    assert current["assignment_cycle_id"] != old["assignment_cycle_id"]
    assert current["property_code"] == "6732"
    assert crm_metrics.coerce_utc_datetime(prior["first_valid_management_at"]) == managed_at
    assert prior["cycle_status"] == "closed"
    assert "previous_assignment_cycle_id" not in current


def test_virna_fixture_second_property_is_held_for_review(monkeypatch):
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
    db["usuarios"].insert_one({"_id": ObjectId(), "nombre": "Rocío Aliaga", "telefono": "+56911111111"})
    old_cycle = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id=str(db["usuarios"].find_one({})["_id"]),
        assigned_by="previous_event", reason="lead_created", assigned_to_display_name="Rocío Aliaga",
        assigned_at=datetime(2026, 6, 10, 16, 23, tzinfo=timezone.utc), property_code="ANTERIOR",
        source_system="prop360", source_event_id="older-event",
    )
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": old_cycle["assignment_cycle_id"]},
        {"$set": {"first_valid_management_at": datetime(2026, 6, 10, 16, 23, tzinfo=timezone.utc)}},
    )
    db["leads"].update_one({"_id": lead_id}, {"$set": {
        "lifecycle.current_assignment_cycle_id": old_cycle["assignment_cycle_id"],
        "lifecycle.assignment_cycle_id": old_cycle["assignment_cycle_id"],
        "lifecycle.assigned_to_user_id": old_cycle["assigned_to_user_id"],
        "lifecycle.assigned_to_display_name": "Rocío Aliaga",
    }})
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
    persisted = db["leads"].find_one({"_id": lead_id})
    assert persisted["prospecto"]["codigo"] == "ANTERIOR"
    assert persisted["ejecutivo_asignado"] == "Rocío Aliaga"
    assert db["crm_assignment_cycles"].count_documents({}) == 1
    assert db["crm_assignment_cycles"].find_one({"assignment_cycle_id": old_cycle["assignment_cycle_id"]})[
        "first_valid_management_at"
    ] == datetime(2026, 6, 10, 16, 23)
    assert db["crm_commercial_property_inquiries"].find_one({
        "lead_id": lead_id, "property_code": "6636", "source_system": "prop360",
    })["status"] == "review_required"
    assert db["crm_notifications_v1"].count_documents({}) == 0


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


def _prepare_poll_cycle(monkeypatch, leads, active_codes, outcomes, last_id=9):
    db = mongomock.MongoClient().incident
    processed = []
    checkpoints = []

    class FakeExtractor:
        def __init__(self, **_kwargs):
            self.metrics = {
                "leads_created": 0, "leads_updated": 0, "events_duplicate": 0,
                "notifications_enqueued": 0, "duplicates_skipped": 0,
            }

        def login(self):
            return True

        def fetch_leads(self, **_kwargs):
            return leads

        def normalize_lead(self, raw):
            return raw

        def process_lead(self, raw, *, resume_incomplete_event=False):
            processed.append((raw["idContacto"], raw["codigo"], resume_incomplete_event))
            return outcomes[raw["idContacto"]]

    monkeypatch.setattr(extractor_prop360, "Prop360Extractor", FakeExtractor)
    monkeypatch.setattr(prop360_poll_loop, "_feature_enabled", lambda: True)
    monkeypatch.setattr(prop360_poll_loop, "_credentials", lambda: ("email", "password"))
    monkeypatch.setattr(prop360_poll_loop, "_window_hours", lambda: 48)
    monkeypatch.setattr(prop360_poll_loop, "_from_date_iso", lambda _hours: "2026-10-01")
    monkeypatch.setattr(prop360_poll_loop, "_active_cartera_codes", lambda: active_codes)
    monkeypatch.setattr(prop360_poll_loop, "_last_contacto_id", lambda _db: last_id)
    monkeypatch.setattr(prop360_poll_loop, "_save_checkpoint", lambda _e, _m, _db, **kw: checkpoints.append(kw))
    monkeypatch.setattr(prop360_poll_loop, "_persist_cycle_status", lambda *_a, **_kw: None)
    return db, processed, checkpoints


def test_poll_ingests_event_when_property_availability_flag_is_stale(monkeypatch):
    leads = [
        {"idContacto": 11, "codigo": "100", "contFono": "+56911111111"},
        {"idContacto": 10, "codigo": "16704", "contFono": "+56922222222"},
    ]
    db, processed, checkpoints = _prepare_poll_cycle(
        monkeypatch, leads, {"100"}, {10: "created", 11: "created"},
    )

    result = prop360_poll_loop.run_prop360_poll_cycle(db=db)

    assert [item[0] for item in processed] == [10, 11]
    assert result["skipped_property_inactive"] == 0
    assert result["inactive_property_events_processed"] == 1
    assert result["unprocessed_events"] == 0
    assert checkpoints[-1]["max_confirmed_id"] == 11


def test_poll_checkpoint_does_not_pass_an_unprocessed_event(monkeypatch):
    leads = [
        {"idContacto": 11, "codigo": "100", "contFono": "+56911111111"},
        {"idContacto": 10, "codigo": "100", "contFono": "+56922222222"},
    ]
    db, processed, checkpoints = _prepare_poll_cycle(
        monkeypatch, leads, {"100"}, {10: "error", 11: "created"},
    )

    result = prop360_poll_loop.run_prop360_poll_cycle(db=db)

    assert [item[0] for item in processed] == [10, 11]
    assert result["unprocessed_events"] == 1
    assert checkpoints[-1]["max_confirmed_id"] == 9


def test_extractor_resumes_incomplete_event_before_identity_dedup(monkeypatch):
    db = mongomock.MongoClient().incident
    event_id = "11555"
    lead_id = ObjectId()
    db["lead_ingest_events"].insert_one({
        "source_system": "prop360", "source_event_id": event_id,
        "status": "lead_persisted", "lead_id": lead_id,
    })
    monkeypatch.setattr(extractor_prop360, "get_db", lambda: db)
    monkeypatch.setattr(extractor_prop360, "ingest_lead_event", lambda _event: IngestResult(
        status="resume", lead_id=str(lead_id), executive="Mariela Arriagada", property_found=True,
    ))

    extractor = extractor_prop360.Prop360Extractor("", "", dry_run=False)
    extractor.normalize_lead = lambda raw: raw
    extractor._is_duplicate = lambda _raw: pytest.fail("resume must bypass identity dedup")
    enqueued = []
    extractor._enqueue_notification = lambda *args: enqueued.append(args)

    result = extractor.process_lead({
        "idContacto": event_id, "codigo": "16704", "contFono": "+56922222222",
        "contNombre": "Cliente", "contMsg": "Consulta",
    }, resume_incomplete_event=True)

    assert result == "resumed"
    assert len(enqueued) == 1


def test_prop360_poll_remains_idle_outside_business_hours(monkeypatch):
    class StopLoop(BaseException):
        pass

    monkeypatch.setattr(prop360_poll_loop, "_feature_enabled", lambda: True)
    monkeypatch.setattr(prop360_poll_loop, "_in_business_hours", lambda: False)
    monkeypatch.setattr(prop360_poll_loop, "_update_health_heartbeat", lambda **_kwargs: None)
    async def persist_off_loop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(prop360_poll_loop, "_persist_cycle_status_off_loop", persist_off_loop)
    monkeypatch.setattr(prop360_poll_loop, "run_prop360_poll_cycle", lambda: pytest.fail("poll must remain idle"))

    async def stop_at_next_slot(_interval):
        raise StopLoop()

    monkeypatch.setattr(prop360_poll_loop, "_sleep_until_next_slot", stop_at_next_slot)
    with pytest.raises(StopLoop):
        asyncio.run(prop360_poll_loop.prop360_poll_loop(3600))


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
    db["usuarios"].insert_one({"_id": ObjectId(), "nombre": "Rocío", "telefono": "+56911111111"})
    db["lead_ingest_events"].insert_one({"source_system": "prop360", "source_event_id": "11290", "status": "lead_persisted", "lead_id": lead_id, "action": "created"})
    monkeypatch.setattr(extractor_prop360, "get_db", lambda: db)

    def enqueue_digest(_db, *, lead, cycle):
        notification = {
            "assignment_cycle_ids": [cycle["assignment_cycle_id"]],
            "digest_type": "non_hot_digest_v1",
            "state": "pending",
        }
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
    db["usuarios"].insert_one({"_id": ObjectId(), "nombre": "Rocío", "telefono": "+56911111111"})
    db["lead_ingest_events"].insert_one({"source_system": "prop360", "source_event_id": "11290", "status": "lead_persisted", "lead_id": lead_id, "action": "created"})
    monkeypatch.setattr(extractor_prop360, "get_db", lambda: db)
    monkeypatch.setattr("chatbot.lead_router.get_executive_phone", lambda _name: "+56911111111")
    calls = []

    def enqueue_hot(_db, **kwargs):
        calls.append(kwargs)
        cycle = _db["crm_assignment_cycles"].find_one({"source_system": "prop360", "source_event_id": "11290"})
        notification = {
            "assignment_cycle_id": cycle["assignment_cycle_id"],
            "notification_type": "lead_assignment_hot",
            "state": "pending",
        }
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
    db["usuarios"].insert_one({"_id": ObjectId(), "nombre": "Rocío", "telefono": "+56911111111"})
    db["lead_ingest_events"].insert_one({"source_system": "prop360", "source_event_id": "11290", "status": "lead_persisted", "lead_id": lead_id, "action": "created"})
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
