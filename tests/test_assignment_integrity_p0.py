from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import UUID

import mongomock
from bson import ObjectId

from chatbot import crm_metrics
from chatbot.crm_assignment_integrity import (
    _notification_findings,
    _valid_transfer_transition,
    assignment_context,
    prepare_pending_assignment_notification,
    reconcile_assignment_integrity,
    reconcile_assignment_integrity_with_retries,
)
from chatbot.crm_service import CrmService
from chatbot.processing_service import LeadProcessingService
from chatbot.conversation_policy import (
    extract_visit_preference,
    is_explicit_commercial_reactivation,
)
from chatbot.ingest_service import LeadEvent
from scraping_convecta.extractor_prop360 import Prop360Extractor
from scripts.run_phase05_crm_reassignment_audit import current_policy_active, enrich_records


def _lead(db, lead_id="lead-p0", **extra):
    lead = {"_id": lead_id, "phone": "+56911112222", "prospecto": {"codigo": "6732"}, **extra}
    db["leads"].insert_one(lead.copy())
    return lead


def test_prop360_and_whatsapp_race_creates_only_one_active_cycle():
    db = mongomock.MongoClient().incident
    lead = _lead(db)
    crm_metrics.ensure_assignment_cycle_indexes(db)
    gate = Barrier(3)

    def assign(owner, source, event_id):
        gate.wait(timeout=5)
        return crm_metrics.create_assignment_cycle(
            db, lead=lead, assigned_to_user_id=owner, assigned_to_display_name=owner,
            assigned_by=source, reason="lead_created", property_code="6732",
            source_system=source, source_event_id=event_id,
            assigned_at=datetime(2026, 10, 19, 13, 56, tzinfo=timezone.utc),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(assign, "hernan", "whatsapp", "wa-89602740"),
            pool.submit(assign, "maria-paz", "prop360", "12272"),
        ]
        gate.wait(timeout=5)
        cycles = [future.result(timeout=10) for future in futures]

    assert cycles[0]["assignment_cycle_id"] == cycles[1]["assignment_cycle_id"]
    assert cycles[0]["assigned_to_user_id"] == cycles[1]["assigned_to_user_id"]
    assert db["crm_assignment_cycles"].count_documents({
        "lead_id": lead["_id"], "cycle_status": "active", "unassigned_at": None,
    }) == 1


def test_distinct_prop360_event_ids_are_not_identity_duplicates_or_new_cycles(monkeypatch):
    db = mongomock.MongoClient().incident
    lead = _lead(db)
    monkeypatch.setattr("scraping_convecta.extractor_prop360.get_db", lambda: db)
    extractor = Prop360Extractor("", "", dry_run=False)
    first_event = {"idContacto": "12272", "contFono": lead["phone"], "codigo": "6732"}
    next_event = {"idContacto": "12273", "contFono": lead["phone"], "codigo": "6732"}
    assert not extractor._is_duplicate(first_event)
    assert not extractor._is_duplicate(next_event)

    first = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="hernan", assigned_to_display_name="Hernán",
        assigned_by="whatsapp", reason="inbound_message", property_code="6732",
        source_system="whatsapp", source_event_id="wa-89602740",
    )
    repeated = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="maria-paz", assigned_to_display_name="María Paz",
        assigned_by="prop360_extractor", reason="lead_created", property_code="6732",
        source_system="prop360", source_event_id="12272",
    )
    assert repeated["assignment_cycle_id"] == first["assignment_cycle_id"]
    assert repeated["assigned_to_user_id"] == "hernan"
    assert db["crm_assignment_cycles"].count_documents({"lead_id": lead["_id"]}) == 1


def test_managed_cycle_is_not_reassigned_by_a_later_channel_event():
    db = mongomock.MongoClient().incident
    lead = _lead(db)
    first = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="hernan", assigned_to_display_name="Hernán Castro",
        assigned_by="commercial_intake", reason="inbound_message", property_code="6732",
        assigned_at=datetime(2026, 10, 19, 13, 56, tzinfo=timezone.utc),
    )
    managed_at = datetime(2026, 10, 19, 13, 59, tzinfo=timezone.utc)
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": first["assignment_cycle_id"]},
        {"$set": {"first_valid_management_at": managed_at}},
    )
    later = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="maria-paz", assigned_to_display_name="María Paz",
        assigned_by="prop360_extractor", reason="lead_created", property_code="6732",
        source_system="prop360", source_event_id="12272",
    )
    persisted = db["crm_assignment_cycles"].find_one({
        "assignment_cycle_id": first["assignment_cycle_id"]
    })
    assert later["assigned_to_user_id"] == "hernan"
    assert crm_metrics.coerce_utc_datetime(persisted["first_valid_management_at"]) == managed_at
    assert persisted["unassigned_at"] is None


def test_exact_giselle_6732_incident_replay_preserves_hernan_after_prop360_event(monkeypatch):
    db = mongomock.MongoClient().incident
    incident_cycle_id = "0e966571-4e5d-495f-9330-4a8aecf232c0"
    monkeypatch.setattr("chatbot.crm_metrics.uuid.uuid4", lambda: UUID(incident_cycle_id))
    lead = _lead(
        db, lead_id=ObjectId("6ad613470035d45d0f87595b"),
        ejecutivo_asignado="Hernán Castro",
        prospecto={"nombre": "Giselle", "codigo": "6732", "ejecutivo": "Hernán Castro"},
        lifecycle={},
    )
    first_assigned = datetime(2026, 10, 19, 12, 56, 2, tzinfo=timezone.utc)
    first = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="hernan-user", assigned_to_display_name="Hernán Castro",
        assigned_by="commercial_intake", reason="inbound_message", property_code="6732",
        assigned_at=first_assigned, source_system="whatsapp", source_event_id="89602740",
    )
    for suffix, occurred_at in ((1, (12, 58, 50)), (2, (12, 59, 40))):
        db["crm_management_results"].insert_one({
            "_id": f"giselle-management-{suffix}",
            "lead_id": lead["_id"], "assignment_cycle_id": first["assignment_cycle_id"],
            "result_type": "EFFECTIVE_CONTACT", "status": "completed",
            "occurred_at": datetime(2026, 10, 19, *occurred_at, tzinfo=timezone.utc),
        })

    replay = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="maria-paz-user", assigned_to_display_name="María Paz",
        assigned_by="prop360_extractor", reason="lead_created", property_code="6732",
        assigned_at=datetime(2026, 10, 19, 13, 0, 4, tzinfo=timezone.utc),
        source_system="prop360", source_event_id="12272", force_new=True,
    )

    assert first["assignment_cycle_id"] == incident_cycle_id
    assert replay["assignment_cycle_id"] == incident_cycle_id
    assert replay["assigned_to_user_id"] == "hernan-user"
    assert db["crm_assignment_cycles"].count_documents({"lead_id": lead["_id"]}) == 1
    assert db["crm_management_results"].count_documents({
        "assignment_cycle_id": first["assignment_cycle_id"],
        "result_type": "EFFECTIVE_CONTACT", "status": "completed",
    }) == 2


def test_same_phone_property_executive_managed_then_prop360_preserves_cycle_and_sla():
    db = mongomock.MongoClient().incident
    lead_id = ObjectId("6ad6066de66c461dd69d696d")
    assigned_at = datetime(2026, 10, 19, 12, 0, 43, 396000, tzinfo=timezone.utc)
    managed_at = datetime(2026, 10, 19, 12, 20, 26, 254000, tzinfo=timezone.utc)
    lead = {
        "_id": lead_id,
        "phone": "+56973707048",
        "ejecutivo_asignado": "Mariela Arriagada",
        "prospecto": {"codigo": "17252", "ejecutivo": "Mariela Arriagada", "link_detectado": True},
        "lifecycle": {},
    }
    db["leads"].insert_one(lead.copy())
    original = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="6989c6309dd2ba54e478196c",
        assigned_to_display_name="Mariela Arriagada", assigned_by="prop360_extractor",
        reason="lead_created", property_code="17252", assigned_at=assigned_at,
        source_system="prop360", source_event_id="12244",
    )
    db["crm_management_results"].insert_one({
        "_id": "crm_management:17252-valid-effective-contact",
        "lead_id": lead_id,
        "assignment_cycle_id": original["assignment_cycle_id"],
        "actor_user_id": "6989c6309dd2ba54e478196c",
        "result_type": "EFFECTIVE_CONTACT",
        "status": "completed",
        "occurred_at": managed_at,
    })
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": original["assignment_cycle_id"]},
        {"$set": {
            "first_valid_management_at": managed_at,
            "first_effective_contact_at": managed_at,
            "sla_first_management_status": "completed",
            "last_management_result": "EFFECTIVE_CONTACT",
        }},
    )
    db["leads"].update_one(
        {"_id": lead_id},
        {"$set": {
            "lifecycle.current_assignment_cycle_id": original["assignment_cycle_id"],
            "lifecycle.assignment_cycle_id": original["assignment_cycle_id"],
            "lifecycle.assigned_to_user_id": "6989c6309dd2ba54e478196c",
            "lifecycle.sla_started_at": original["sla_started_at"],
            "lifecycle.first_valid_management_at": managed_at,
            "sla_status": "fulfilled",
        }},
    )

    repeated = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="6989c6309dd2ba54e478196c",
        assigned_to_display_name="Mariela Arriagada", assigned_by="prop360_extractor",
        reason="lead_created", property_code="17252",
        assigned_at=datetime(2026, 10, 19, 16, 0, 4, 891000, tzinfo=timezone.utc),
        source_system="prop360", source_event_id="12276", force_new=True,
    )

    persisted = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": original["assignment_cycle_id"]})
    assert repeated["assignment_cycle_id"] == original["assignment_cycle_id"]
    assert db["crm_assignment_cycles"].count_documents({"lead_id": lead_id}) == 1
    assert persisted["cycle_status"] == "active" and persisted["unassigned_at"] is None
    assert persisted["assigned_to_user_id"] == "6989c6309dd2ba54e478196c"
    assert crm_metrics.coerce_utc_datetime(persisted["sla_started_at"]) == crm_metrics.coerce_utc_datetime(original["sla_started_at"])
    assert crm_metrics.coerce_utc_datetime(persisted["first_valid_management_at"]) == managed_at
    assert persisted["sla_first_management_status"] == "completed"
    assert db["crm_management_results"].count_documents({
        "assignment_cycle_id": original["assignment_cycle_id"],
        "result_type": "EFFECTIVE_CONTACT", "status": "completed",
    }) == 1


def test_assignment_integrity_reconciler_retries_transient_network_timeout(monkeypatch):
    from pymongo.errors import NetworkTimeout
    import chatbot.crm_assignment_integrity as integrity

    calls = []
    sleeps = []

    def fail_once(_db, **kwargs):
        calls.append(kwargs.get("detection_run_id"))
        if len(calls) == 1:
            raise NetworkTimeout("temporary socket timeout")
        return {"status": "completed", "findings": 0}

    monkeypatch.setattr(integrity, "reconcile_assignment_integrity", fail_once)
    report = reconcile_assignment_integrity_with_retries(
        object(), max_attempts=3, base_delay_seconds=1, max_delay_seconds=5,
        sleep=sleeps.append,
    )
    assert report["status"] == "completed"
    assert report["attempts"] == 2
    assert len(calls) == 2 and calls[0] == calls[1]
    assert sleeps == [1]


def test_new_cycle_cannot_replace_an_existing_owner_mirror_without_transfer():
    db = mongomock.MongoClient().incident
    lead = _lead(db, ejecutivo_asignado="Hernán Castro",
                 prospecto={"codigo": "6732", "ejecutivo": "Hernán Castro"},
                 messages=[{"role": "assistant", "content": "Tu ejecutiva es Hernán Castro."}])

    try:
        crm_metrics.create_assignment_cycle(
            db, lead=lead, assigned_to_user_id="u2", assigned_to_display_name="María Paz",
            assigned_by="prop360_extractor", reason="lead_created", property_code="6732",
        )
    except RuntimeError as exc:
        assert "existing owner mirrors" in str(exc)
    else:
        raise AssertionError("conflicting owner mirror was silently replaced")

    assert db["crm_assignment_cycles"].count_documents({}) == 0
    assert db["crm_assignment_integrity_alerts"].count_documents({
        "issue_code": "assignment_cycle_creation_blocked_by_existing_mirrors"
    }) == 1


def test_closed_lead_reactivation_requires_explicit_commercial_interest_and_keeps_age(monkeypatch):
    db = mongomock.MongoClient().incident
    created_at = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    lead = _lead(db, pipeline_stage="CLOSED_LOST", stage="CLOSED_LOST", created_at=created_at,
                 stage_history=[{"from": "NEW", "to": "CLOSED_LOST"}])
    prior = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="hernan", assigned_to_display_name="Hernán",
        assigned_by="commercial_intake", reason="inbound_message", property_code="6732",
    )
    monkeypatch.setattr("chatbot.crm_service.get_db", lambda: db)

    assert not is_explicit_commercial_reactivation("Hola, gracias")
    assert not CrmService.reactivate_closed_lead_if_commercial(lead["phone"], "Hola, gracias")
    assert CrmService.reactivate_closed_lead_if_commercial(
        lead["phone"], "Sí, quiero coordinar una visita a las 09:00"
    )
    reopened = db["leads"].find_one({"_id": lead["_id"]})
    assert reopened["pipeline_stage"] == "NEW"
    assert crm_metrics.coerce_utc_datetime(reopened["created_at"]) == created_at
    assert reopened["stage_history"][-1]["from"] == "CLOSED_LOST"
    assert db["crm_assignment_cycles"].count_documents({
        "assignment_cycle_id": prior["assignment_cycle_id"]
    }) == 1


def test_closed_lead_reactivation_cas_does_not_overwrite_a_newer_stage(monkeypatch):
    db = mongomock.MongoClient().incident
    lead = _lead(db, pipeline_stage="CLOSED_LOST", stage="CLOSED_LOST")

    def read_then_change(_phone):
        stale_snapshot = db["leads"].find_one({"_id": lead["_id"]})
        db["leads"].update_one({"_id": lead["_id"]}, {"$set": {"stage": "QUALIFIED"}})
        return stale_snapshot

    monkeypatch.setattr("chatbot.crm_service.get_db", lambda: db)
    monkeypatch.setattr(CrmService, "get_lead", staticmethod(read_then_change))

    assert not CrmService.reactivate_closed_lead_if_commercial(
        lead["phone"], "Sí, quiero coordinar una visita a las 09:00"
    )
    persisted = db["leads"].find_one({"_id": lead["_id"]})
    assert persisted["stage"] == "QUALIFIED"
    assert persisted["pipeline_stage"] == "CLOSED_LOST"


def test_assignment_context_blocks_inconsistent_owner_mirrors():
    db = mongomock.MongoClient().incident
    lead = _lead(db, ejecutivo_asignado="Hernán Castro",
                 lifecycle={"current_assignment_cycle_id": "cycle-1", "assigned_to_user_id": "u1"},
                 prospecto={"codigo": "6732", "ejecutivo": "Hernán Castro"})
    db["crm_assignment_cycles"].insert_one({
        "assignment_cycle_id": "cycle-1", "lead_id": lead["_id"],
        "assigned_to_user_id": "u1", "assigned_to_display_name": "Hernán Castro",
        "assigned_at": datetime(2026, 10, 19, 13, 56, tzinfo=timezone.utc),
        "property_code": "6732", "cycle_status": "active", "unassigned_at": None,
    })
    assert assignment_context(db, lead)["status"] == "consistent"
    lead["ejecutivo_asignado"] = "María Paz"
    assert assignment_context(db, lead)["status"] == "conflict"
    assert assignment_context(db, lead)["owner_name"] is None


def test_assignment_context_checks_both_lifecycle_cycle_pointers():
    db = mongomock.MongoClient().incident
    lead = _lead(db, ejecutivo_asignado="Hernán Castro",
                 lifecycle={"current_assignment_cycle_id": "cycle-1",
                            "assignment_cycle_id": "stale-cycle"},
                 prospecto={"codigo": "6732", "ejecutivo": "Hernán Castro"})
    db["crm_assignment_cycles"].insert_one({
        "assignment_cycle_id": "cycle-1", "lead_id": lead["_id"],
        "assigned_to_user_id": "u1", "assigned_to_display_name": "Hernán Castro",
        "property_code": "6732", "cycle_status": "active", "unassigned_at": None,
    })

    context = assignment_context(db, lead)

    assert context["status"] == "conflict"
    assert "cycle_pointer_mismatch" in context["issues"]


def test_background_processing_cannot_overwrite_an_active_cycle_owner(monkeypatch):
    db = mongomock.MongoClient().incident
    lead = _lead(db, ejecutivo_asignado="No Asignado",
                 lifecycle={"current_assignment_cycle_id": "cycle-1"},
                 prospecto={"codigo": "6732", "ejecutivo": "No Asignado"})
    db["crm_assignment_cycles"].insert_one({
        "assignment_cycle_id": "cycle-1", "lead_id": lead["_id"],
        "assigned_to_user_id": "u1", "assigned_to_display_name": "Hernán",
        "assigned_at": datetime(2026, 10, 19, 13, 56, tzinfo=timezone.utc),
        "property_code": "6732", "cycle_status": "active", "unassigned_at": None,
    })
    monkeypatch.setattr(LeadProcessingService, "_db", staticmethod(lambda: db))
    monkeypatch.setattr(
        "chatbot.processing_service.find_responsible_executive",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("automatic router called")),
    )

    assert LeadProcessingService.reassign_if_needed(lead) == {}
    assert db["crm_assignment_cycles"].count_documents({"cycle_status": "active"}) == 1
    assert db["leads"].find_one({"_id": lead["_id"]})["ejecutivo_asignado"] == "No Asignado"
    assert db["crm_assignment_integrity_alerts"].count_documents({
        "issue_code": "automatic_processing_assignment_blocked_by_canonical_cycle"
    }) == 1


def test_visit_context_promotes_to_hot_without_confirming_availability(monkeypatch):
    db = mongomock.MongoClient().incident
    lead = _lead(db, lead_temperature_effective="COLD", lifecycle={})
    cycle = crm_metrics.create_assignment_cycle(
        db, lead=lead, assigned_to_user_id="u1", assigned_to_display_name="Hernán",
        assigned_by="commercial_intake", reason="inbound_message", property_code="6732",
        assigned_at=datetime(2026, 10, 19, 13, 56, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("chatbot.crm_service.get_db", lambda: db)
    monkeypatch.setattr("chatbot.crm_service.log_event", lambda *args, **kwargs: None)

    preference = extract_visit_preference("Idealmente a las 09:00", visit_context=True)
    assert preference == "a las 09:00"
    assert CrmService.update_intent(lead["phone"], "ASK_VISIT", actor="bot")
    updated = db["leads"].find_one({"_id": lead["_id"]})
    hot_cycle = db["crm_assignment_cycles"].find_one({
        "assignment_cycle_id": cycle["assignment_cycle_id"]
    })
    assert updated["lead_temperature_effective"] == "HOT"
    assert updated["lifecycle"]["hot_since"]
    hot_cycle_start = crm_metrics.coerce_utc_datetime(hot_cycle["hot_started_at"])
    lead_hot_start = crm_metrics.coerce_utc_datetime(updated["lifecycle"]["hot_since"])
    assert abs((hot_cycle_start - lead_hot_start).total_seconds()) < 1
    assert "visit_confirmed_at" not in updated


def test_sla_transfer_requires_a_recorded_authorized_decision():
    before = {"assignment_cycle_id": "cycle-before", "assigned_to_user_id": "u1"}
    unauthorized = {
        "assignment_cycle_id": "cycle-after", "assigned_to_user_id": "u2",
        "reason": "inbound_message", "assigned_by": "prop360_extractor",
    }
    authorized = {
        **unauthorized, "assigned_by": "sla_reassignment",
        "reassignment_decision_id": "decision-001",
    }
    assert not _valid_transfer_transition(before, unauthorized)
    assert _valid_transfer_transition(before, authorized)


def test_integrity_audit_requires_a_matching_sla_decision_ledger_row():
    db = mongomock.MongoClient().incident
    before = {"assignment_cycle_id": "cycle-before", "assigned_to_user_id": "u1"}
    after = {
        "assignment_cycle_id": "cycle-after", "assigned_to_user_id": "u2",
        "reason": "sla_reassignment", "assigned_by": "sla_reassignment",
        "reassignment_decision_id": "decision-001",
    }

    assert not _valid_transfer_transition(before, after, db=db)
    db["crm_sla_reassignment_audit_v1"].insert_one({
        "_id": "decision-001", "decision_id": "decision-001",
        "event_type": "SLA_REASSIGNMENT_COMMITTED",
        "source_cycle_id": "cycle-before", "new_cycle_id": "cycle-after",
    })
    assert _valid_transfer_transition(before, after, db=db)



def test_multihop_sla_transfer_validates_immutable_incoming_decision_after_next_rescue():
    db = mongomock.MongoClient().incident
    before = {"assignment_cycle_id": "cycle-original", "assigned_to_user_id": "u1"}
    intermediate = {
        "assignment_cycle_id": "sla-reassignment:decision-in",
        "assigned_to_user_id": "u2",
        "assigned_by": "sla_reassignment",
        "reason": "lead_created",
        # A second rescue overwrites this field with the OUTGOING decision.
        "reassignment_decision_id": "decision-out",
    }
    db["crm_sla_reassignment_audit_v1"].insert_one({
        "_id": "decision-in", "decision_id": "decision-in",
        "event_type": "SLA_REASSIGNMENT_COMMITTED",
        "source_cycle_id": "cycle-original",
        "new_cycle_id": "sla-reassignment:decision-in",
    })
    assert _valid_transfer_transition(before, intermediate, db=db)
    # The outgoing decision is not sufficient proof for an unrelated transfer.
    assert not _valid_transfer_transition(
        {"assignment_cycle_id": "different-source", "assigned_to_user_id": "u1"},
        intermediate, db=db,
    )


def test_non_hot_digest_then_hot_escalation_is_not_a_duplicate_delivery():
    db = mongomock.MongoClient().incident
    cycle = {
        "assignment_cycle_id": "cycle-hot-escalated",
        "assigned_to_user_id": "erika",
        "property_code": "6459",
    }
    db["crm_notifications_v1"].insert_many([
        {
            "_id": "digest-1", "state": "sent",
            "assignment_cycle_ids": ["cycle-hot-escalated"],
            "digest_type": "non_hot_digest_v1",
            "recipient_user_id": "erika",
        },
        {
            "_id": "hot-1", "state": "sent",
            "assignment_cycle_id": "cycle-hot-escalated",
            "notification_type": "lead_assignment_hot",
            "recipient_user_id": "erika",
        },
    ])
    assert not any(
        finding["issue_code"] == "duplicate_delivered_notifications_for_assignment_cycle"
        for finding in _notification_findings(db, "lead-6459", [cycle])
    )

    # Two deliveries of the same HOT notification category remain an alert.
    db["crm_notifications_v1"].insert_one({
        "_id": "hot-2", "state": "sent",
        "assignment_cycle_id": "cycle-hot-escalated",
        "notification_type": "lead_assignment_hot",
        "recipient_user_id": "erika",
    })
    issues = _notification_findings(db, "lead-6459", [cycle])
    duplicates = [finding for finding in issues if
                  finding["issue_code"] == "duplicate_delivered_notifications_for_assignment_cycle"]
    assert len(duplicates) == 1
    assert duplicates[0]["notification_kind"] == "lead_assignment_hot"
    assert duplicates[0]["notification_ids"] == ["hot-1", "hot-2"]


def test_integrity_audit_flags_delivered_notification_to_wrong_owner():
    db = mongomock.MongoClient().incident
    cycle = {
        "assignment_cycle_id": "cycle-6732", "assigned_to_user_id": "hernan",
        "property_code": "6732",
    }
    db["crm_notifications_v1"].insert_one({
        "_id": "wrong-owner-notice", "assignment_cycle_id": "cycle-6732",
        "notification_type": "lead_assignment_hot", "recipient_user_id": "maria-paz",
        "state": "sent",
    })

    findings = _notification_findings(db, "lead-6732", [cycle])

    assert any(row["issue_code"] == "notification_recipient_mismatch_for_assignment_cycle"
               for row in findings)


def test_reconciliation_includes_assigned_mirrors_without_any_cycle():
    db = mongomock.MongoClient().incident
    lead = _lead(db, ejecutivo_asignado="Hernán Castro",
                 prospecto={"codigo": "6732", "ejecutivo": "Hernán Castro"},
                 messages=[{"role": "assistant", "content": "Te atenderá Hernán Castro."}])

    report = reconcile_assignment_integrity(
        db, now=datetime(2026, 10, 19, tzinfo=timezone.utc), persist_alerts=True,
    )

    assert report["leads_checked"] == 1
    assert any(row["issue_code"] == "canonical_cycle_mirror_inconsistency"
               for row in report["finding_rows"])
    assert any(row["issue_code"] == "chatbot_named_executive_during_assignment_conflict"
               for row in report["finding_rows"])
    assert db["crm_assignment_integrity_alerts"].count_documents({"lead_id": lead["_id"]}) == 2


def test_pending_notice_cannot_be_sent_to_an_owner_that_lost_the_cycle(monkeypatch):
    db = mongomock.MongoClient().incident
    lead = _lead(
        db, ejecutivo_asignado="Hernán Castro",
        prospecto={"codigo": "6732", "ejecutivo": "Hernán Castro"},
        lifecycle={"current_assignment_cycle_id": "cycle-1",
                   "assignment_cycle_id": "cycle-1", "assigned_to_user_id": "u1"},
    )
    db["crm_assignment_cycles"].insert_one({
        "assignment_cycle_id": "cycle-1", "lead_id": lead["_id"],
        "assigned_to_user_id": "u1", "assigned_to_display_name": "Hernán Castro",
        "property_code": "6732", "cycle_status": "active", "unassigned_at": None,
    })

    guarded = prepare_pending_assignment_notification(
        db, lead_phone=lead["phone"], requested_owner_name="María Paz",
    )

    assert guarded["status"] == "blocked"
    assert guarded["reason"] == "notice_target_is_not_cycle_owner"


def test_pending_notice_assigns_unowned_lead_before_it_can_be_sent(monkeypatch):
    db = mongomock.MongoClient().incident
    lead = _lead(db, ejecutivo_asignado="No Asignado",
                 prospecto={"codigo": "6732", "ejecutivo": "No Asignado"}, lifecycle={})
    db["usuarios"].insert_one({"_id": "u1", "nombre": "Hernán Castro", "telefono": "+56911111111"})
    monkeypatch.setattr("chatbot.crm_service.get_db", lambda: db)
    monkeypatch.setattr("chatbot.crm_service.log_event", lambda *args, **kwargs: None)

    guarded = prepare_pending_assignment_notification(
        db, lead_phone=lead["phone"], requested_owner_name="Hernán Castro",
    )

    assert guarded["status"] == "ready"
    assert guarded["owner_user_id"] == "u1"
    assert db["crm_assignment_cycles"].count_documents({
        "lead_id": lead["_id"], "cycle_status": "active",
    }) == 1


def test_system_misassignment_cycle_is_excluded_from_sla_performance():
    from datetime import timedelta

    now = datetime(2026, 10, 19, 14, tzinfo=timezone.utc)
    lead = {
        "_id": "lead-p0", "phone": "56911112222", "stage": "OPEN",
        "ejecutivo_asignado": "María Paz Galleguillos",
        "prospecto": {"codigo": "6732", "ejecutivo": "María Paz Galleguillos"},
        "lifecycle": {"current_assignment_cycle_id": "cycle-maria"},
    }
    cycle = {
        "lead_id": lead["_id"], "assignment_cycle_id": "cycle-maria",
        "assigned_to_user_id": "maria", "assigned_to_display_name": "María Paz Galleguillos",
        "assigned_at": now - timedelta(hours=2), "sla_started_at": now - timedelta(hours=2),
        "temperature_at_assignment": "NORMAL", "cycle_status": "active", "unassigned_at": None,
        "schema_version": "crm_assignment_cycle_v1", "reason": "lead_created",
        "cycle_origin": "inbound_message", "sla_performance_excluded": True,
    }
    data = {
        "as_of": now, "leads": [lead], "cycles": [cycle], "events": [],
        "management_results": [], "notifications": [], "properties": {},
        "users": [{"_id": "maria", "nombre": "María Paz Galleguillos", "rol": "agente", "is_active": True}],
    }

    records = enrich_records(data)

    assert records[0]["excluded_origin"] is True
    assert records[0]["is_current_policy_active"] is False
    assert current_policy_active(records) == []
