from unittest.mock import patch

import mongomock
import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import webhook as webhook_module
from chatbot import commercial_intake, ingest_service, processing_service
from chatbot import manual_entry
from chatbot.alert_service import _send_alert_once_sync
from chatbot.constants import UNASSIGNED_LABEL
from chatbot.ingest_service import LeadEvent
from chatbot.phone_utils import has_real_phone
from chatbot.property_lookup import PROPERTY_COLLECTION_NAME


VALID_PHONE = "+56912345678"
PROPERTY = {
    "codigo": "6508",
    "disponible_prop360": True,
    "ubicacion": {"comuna": "El Bosque"},
    "tipo_operacion": {"tipo": "Casa", "venta": True, "arriendo": False},
}


@pytest.mark.parametrize("value", [
    "", None, "sin teléfono", "cliente 912345678", "no-phone-test-1",
    "+56900000000", "56900000000", "0000000000", "1111111111",
    "912345678-----", "912345678)",
])
def test_phone_gate_rejects_empty_malformed_and_synthetic_values(value):
    assert has_real_phone(value) is False


@pytest.mark.parametrize("value", [
    "912345678", "+56 9 1234 5678", "+56987654321",
])
def test_phone_gate_accepts_well_formed_non_placeholder_contact_numbers(value):
    # Format and known synthetic-value check only; no phone is contacted here.
    assert has_real_phone(value) is True


@pytest.mark.parametrize("phone", ["", "texto 912345678", "+56900000000", "no-phone-test-1"])
def test_commercial_intake_does_not_route_lead_from_invalid_phone(phone):
    db = mongomock.MongoClient().URLS
    db[PROPERTY_COLLECTION_NAME].insert_one(PROPERTY)
    db.leads.insert_one({
        "phone": phone,
        "ejecutivo_asignado": UNASSIGNED_LABEL,
        "prospecto": {"codigo": "6508", "ejecutivo": UNASSIGNED_LABEL},
    })

    with patch.object(commercial_intake, "find_responsible_executive", side_effect=AssertionError("router called")), \
         patch.object(commercial_intake, "create_assignment_cycle", side_effect=AssertionError("cycle created")), \
         patch("chatbot.crm_non_hot_digest.accumulate_non_hot_lead", side_effect=AssertionError("notification queued")), \
         patch("chatbot.crm_hot_delivery.assign_and_enqueue_hot", side_effect=AssertionError("notification queued")):
        event = commercial_intake.process_inbound(
            db, inbound_provider_id=f"invalid-{phone}", phone=phone,
            text="La propiedad 6508",
        )

    assert event["commercial_processing_state"] == commercial_intake.WAITING_PROPERTY
    assert event["notification_eligible"] is False
    assert event["commercial_processing_state"] != commercial_intake.COMPLETED
    assert db.crm_assignment_cycles.count_documents({}) == 0


def test_commercial_intake_valid_phone_routes_once_and_is_idempotent():
    db = mongomock.MongoClient().URLS
    db[PROPERTY_COLLECTION_NAME].insert_one(PROPERTY)
    lead_id = ObjectId()
    db.leads.insert_one({
        "_id": lead_id,
        "phone": VALID_PHONE,
        "lead_temperature_effective": "NORMAL",
        "ejecutivo_asignado": UNASSIGNED_LABEL,
        "prospecto": {"codigo": "6508", "ejecutivo": UNASSIGNED_LABEL},
    })
    user_id = ObjectId()
    db.usuarios.insert_one({"_id": user_id, "nombre": "Ejecutivo", "telefono": "+56911111111"})

    with patch.object(commercial_intake, "find_responsible_executive", return_value=("Ejecutivo", "+56911111111", "PROPERTY")) as route, \
         patch("chatbot.crm_non_hot_digest.accumulate_non_hot_lead", return_value={"_id": "digest-1"}) as digest:
        first = commercial_intake.process_inbound(
            db, inbound_provider_id="valid-once", phone=VALID_PHONE,
            text="La propiedad 6508",
        )
        second = commercial_intake.process_inbound(
            db, inbound_provider_id="valid-once", phone=VALID_PHONE,
            text="La propiedad 6508",
        )

    assert first["commercial_processing_state"] == commercial_intake.COMPLETED
    assert second["assignment_cycle_id"] == first["assignment_cycle_id"]
    assert route.call_count == 1
    assert digest.call_count == 1
    assert db.crm_assignment_cycles.count_documents({"lead_id": lead_id}) == 1


def test_commercial_intake_recovery_uses_phone_from_already_linked_lead():
    db = mongomock.MongoClient().URLS
    db[PROPERTY_COLLECTION_NAME].insert_one(PROPERTY)
    lead_id = ObjectId()
    db.leads.insert_one({
        "_id": lead_id,
        "phone": VALID_PHONE,
        "contact_phone_normalized": VALID_PHONE,
        "lead_temperature_effective": "NORMAL",
        "ejecutivo_asignado": UNASSIGNED_LABEL,
        "prospecto": {"codigo": "6508", "ejecutivo": UNASSIGNED_LABEL},
    })
    db.usuarios.insert_one({
        "_id": ObjectId(), "nombre": "Ejecutivo", "telefono": "+56911111111",
    })
    db[commercial_intake.COLLECTION].insert_one({
        "source_inbound_provider_id": "recovery-linked-lead",
        "lead_id": lead_id,
        "source_property_code": "6508",
        "commercial_processing_state": commercial_intake.WAITING_INVENTORY,
    })

    with patch.object(commercial_intake, "find_responsible_executive", return_value=("Ejecutivo", "+56911111111", "PROPERTY")) as route, \
         patch("chatbot.crm_non_hot_digest.accumulate_non_hot_lead", return_value={"_id": "digest-recovery"}):
        event = commercial_intake.process_inbound(
            db, inbound_provider_id="recovery-linked-lead", phone="",
            text="La propiedad 6508",
        )

    assert event["commercial_processing_state"] == commercial_intake.COMPLETED
    assert route.call_args.kwargs["lead_phone"] == VALID_PHONE
    assert db.crm_assignment_cycles.count_documents({"lead_id": lead_id}) == 1


@pytest.mark.parametrize("phone", ["", "texto 912345678", "+56900000000", "no-phone-test-1"])
def test_ingest_creates_pending_lead_without_assignment_for_invalid_phone(monkeypatch, phone):
    db = mongomock.MongoClient().URLS
    monkeypatch.setattr(ingest_service, "get_db", lambda: db)
    monkeypatch.setattr(ingest_service, "_atomic_reserve_event", lambda *_args: True)
    monkeypatch.setattr(ingest_service, "_find_lead_by_id", lambda *_args: (None, None, None))
    monkeypatch.setattr(ingest_service, "_enrich_from_cartera", lambda *_args: {
        "property_found": True, "comuna": "El Bosque", "tipo": "Casa",
        "operacion": "Venta", "canonical_code": "6508",
    })
    monkeypatch.setattr(ingest_service, "log_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ingest_service, "_finalize_event", lambda *_args: None)

    with patch.object(ingest_service, "find_responsible_executive", side_effect=AssertionError("router called")):
        result = ingest_service.ingest_lead_event(LeadEvent(
            source_system="portal", source_event_id=f"invalid-{phone}",
            phone=phone, property_code="6508", message="Me interesa",
        ))

    lead = db[ingest_service.COLLECTION_CONVERSATIONS].find_one({"_id": ObjectId(result.lead_id)})
    assert result.status == "created"
    assert result.executive == UNASSIGNED_LABEL
    assert result.assignment_changed is False
    assert lead["assignment_type"] == "MISSING_PHONE"
    assert "assigned_at" not in lead["lifecycle"]
    assert lead["phone_is_synthetic"] is True


def test_processing_service_uses_stored_phone_not_technical_lead_id():
    lead_id = ObjectId()
    lead = {
        "_id": lead_id, "phone": VALID_PHONE,
        "ejecutivo_asignado": UNASSIGNED_LABEL,
        "prospecto": {"codigo": "6508", "ejecutivo": UNASSIGNED_LABEL},
    }
    db = mongomock.MongoClient().URLS

    with patch.object(processing_service.LeadProcessingService, "_db", return_value=db), \
         patch.object(processing_service, "find_property_by_any_identifier", return_value=PROPERTY), \
         patch.object(processing_service, "find_responsible_executive", return_value=("Ejecutivo", "+56911111111", "PROPERTY")) as route:
        result = processing_service.LeadProcessingService.reassign_if_needed(lead)

    assert result["ejecutivo_asignado"] == "Ejecutivo"
    assert route.call_args.kwargs["lead_phone"] == VALID_PHONE


def test_processing_service_does_not_route_unassigned_lead_without_real_phone():
    lead = {
        "_id": ObjectId(), "phone": "no-phone-portal-event",
        "ejecutivo_asignado": UNASSIGNED_LABEL,
        "prospecto": {"codigo": "6508", "ejecutivo": UNASSIGNED_LABEL},
    }
    with patch.object(processing_service.LeadProcessingService, "_db") as database, \
         patch.object(processing_service, "find_responsible_executive", side_effect=AssertionError("router called")):
        assert processing_service.LeadProcessingService.reassign_if_needed(lead) == {}
    database.assert_not_called()


def test_manual_email_only_lead_stays_pending_without_cycle_or_notification(monkeypatch):
    db = mongomock.MongoClient().URLS
    monkeypatch.setattr(manual_entry, "get_db", lambda: db)
    monkeypatch.setattr(manual_entry, "find_property_by_any_identifier", lambda *_args: PROPERTY)
    monkeypatch.setattr(manual_entry, "check_lead_duplicate", lambda *_args: ("not_found", None))
    monkeypatch.setattr(manual_entry, "log_event", lambda *_args, **_kwargs: None)

    with patch.object(manual_entry, "find_responsible_executive", side_effect=AssertionError("router called")), \
         patch("chatbot.crm_metrics.create_assignment_cycle", side_effect=AssertionError("cycle created")), \
         patch("chatbot.crm_non_hot_digest.accumulate_non_hot_lead", side_effect=AssertionError("notification queued")):
        result = manual_entry.create_manual_lead({
            "email": "client@example.test", "property_code": "6508", "nombre": "Cliente",
        })

    lead = db.leads.find_one({"_id": ObjectId(result["lead_id"])})
    assert result["status"] == "ok"
    assert result["assigned_to"] == UNASSIGNED_LABEL
    assert result["assignment_cycle_id"] is None
    assert lead["assignment_type"] == "MISSING_PHONE"
    assert "assigned_at" not in lead["lifecycle"]
    assert db.crm_assignment_cycles.count_documents({}) == 0


def test_manual_valid_phone_keeps_existing_assignment_and_cycle_path(monkeypatch):
    db = mongomock.MongoClient().URLS
    monkeypatch.setattr(manual_entry, "get_db", lambda: db)
    monkeypatch.setattr(manual_entry, "find_property_by_any_identifier", lambda *_args: PROPERTY)
    monkeypatch.setattr(manual_entry, "check_lead_duplicate", lambda *_args: ("not_found", None))
    monkeypatch.setattr(manual_entry, "log_event", lambda *_args, **_kwargs: None)
    user_id = ObjectId()
    db.usuarios.insert_one({"_id": user_id, "nombre": "Ejecutivo", "telefono": "+56911111111"})

    with patch.object(manual_entry, "find_responsible_executive", return_value=("Ejecutivo", "+56911111111", "PROPERTY")) as route, \
         patch("chatbot.crm_non_hot_digest.accumulate_non_hot_lead", return_value={"_id": "digest-valid"}) as digest:
        result = manual_entry.create_manual_lead({
            "phone": VALID_PHONE, "email": "client@example.test",
            "property_code": "6508", "nombre": "Cliente",
        })

    lead = db.leads.find_one({"_id": ObjectId(result["lead_id"])})
    assert result["status"] == "ok"
    assert result["assigned_to"] == "Ejecutivo"
    assert result["assignment_cycle_id"]
    assert lead["ejecutivo_asignado"] == "Ejecutivo"
    assert route.call_count == 1
    assert digest.call_count == 1
    assert db.crm_assignment_cycles.count_documents({"lead_id": lead["_id"]}) == 1


def test_hot_alert_does_not_assign_an_unassigned_lead_without_valid_phone():
    with patch("chatbot.alert_service.should_send_alert", return_value=True), \
         patch("chatbot.alert_service.find_responsible_executive", side_effect=AssertionError("router called")):
        result = _send_alert_once_sync(
            phone="+56900000000", lead_type="InteresVisita", lead_score=90,
            criteria={"codigo": "6508", "ejecutivo_asignado": UNASSIGNED_LABEL},
            last_response="", last_user_msg="Quiero visitar la propiedad",
            full_history=[],
        )

    assert result == {"status": "failed", "reason": "invalid_contact_phone"}


@pytest.mark.parametrize("remote_jid", [
    "", "malformed-peer", "91x2345678@s.whatsapp.net",
    "56900000000@s.whatsapp.net",
])
def test_webhook_rejects_invalid_phone_before_persisting_inbound_job(monkeypatch, remote_jid):
    monkeypatch.setattr(webhook_module.Config, "WASENDER_WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_module, "get_user_by_phone", lambda _phone: None)
    client = TestClient(webhook_module.app)
    payload = {
        "event": "messages.upsert",
        "data": {"messages": {
            "key": {"id": "provider-test-1", "fromMe": False, "remoteJid": remote_jid},
            "message": {"conversation": "Me interesa la propiedad"},
        }},
    }

    with patch("chatbot.chatbot_queue.create_inbound_job", side_effect=AssertionError("queue called")):
        response = client.post("/webhook", json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "invalid phone"


def test_webhook_accepts_formatted_phone_and_queues_canonical_number(monkeypatch):
    monkeypatch.setattr(webhook_module.Config, "WASENDER_WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_module, "get_user_by_phone", lambda _phone: None)
    db = mongomock.MongoClient().URLS
    monkeypatch.setattr("chatbot.storage.get_db", lambda: db)
    monkeypatch.setattr("chatbot.storage.log_event", lambda *_args, **_kwargs: None)
    client = TestClient(webhook_module.app)
    payload = {
        "event": "messages.upsert",
        "data": {"messages": {
            "key": {"id": "provider-test-2", "fromMe": False, "remoteJid": "56912345678@s.whatsapp.net"},
            "message": {"conversation": "Me interesa la propiedad"},
        }},
    }

    with patch("chatbot.chatbot_queue.create_inbound_job", return_value="job-test-2") as create_job:
        response = client.post("/webhook", json=payload)

    assert response.status_code == 200
    assert response.json()["job_id"] == "job-test-2"
    assert create_job.call_args.kwargs["phone"] == VALID_PHONE
