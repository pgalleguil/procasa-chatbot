from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import mongomock
from bson import ObjectId

from chatbot import commercial_intake
from chatbot.crm_sla_dry_run import evaluate_sla_alert_dry_run
from chatbot.processing_service import LeadProcessingService
from chatbot.property_lookup import PROPERTY_COLLECTION_NAME


def _property(*, code="6508", commune="El Bosque", property_type="Casa", sale=True, rental=False):
    return {
        "codigo": code,
        "disponible_prop360": True,
        "ubicacion": {"comuna": commune},
        "tipo_operacion": {
            "tipo": property_type,
            "venta": sale,
            "arriendo": rental,
        },
    }


def test_unknown_url_operation_cannot_override_text_resolved_property():
    prop = _property(sale=True, rental=False)

    context = commercial_intake._commercial_property_context(
        prop, {"portal": "", "operation": "arriendo"},
    )

    assert context["operacion"] == "Venta"
    assert context["operacion_fuente"] is None


def test_supported_portal_operation_is_preserved_only_when_prop360_allows_it():
    dual = _property(sale=True, rental=True)
    rental_context = commercial_intake._commercial_property_context(
        dual, {"portal": "portal_inmobiliario", "operation": "arriendo"},
    )
    conflicting_context = commercial_intake._commercial_property_context(
        _property(sale=True, rental=False),
        {"portal": "toctoc", "operation": "arriendo"},
    )

    assert rental_context["operacion"] == "arriendo"
    assert rental_context["operacion_fuente"] == "arriendo"
    assert conflicting_context["operacion"] == "Venta"
    assert conflicting_context["operacion_fuente"] is None


def test_supported_portals_keep_sale_operation_metadata():
    for portal in (
        "enlaceinmobiliario", "toctoc", "yapo", "mercadolibre",
        "portal_inmobiliario", "procasa",
    ):
        context = commercial_intake._commercial_property_context(
            _property(sale=True, rental=False),
            {"portal": portal, "operation": "venta"},
        )
        assert context["operacion"] == "venta", portal
        assert context["operacion_fuente"] == "venta", portal


def test_process_inbound_enriches_existing_assigned_lead_without_new_cycle_or_delivery():
    db = mongomock.MongoClient().URLS
    lead_id = ObjectId("6ac6f32d0035d45d0f86ce3a")
    user_id = ObjectId("64b000000000000000000001")
    cycle_id = "existing-cycle-6508"
    assigned_at = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)
    lead = {
        "_id": lead_id,
        "phone": "+56912345678",
        "lead_temperature_effective": "NORMAL",
        "ejecutivo_asignado": "Ejecutivo actual",
        "assignment_type": "MISSING_PROPERTY",
        "prospecto": {"codigo": "6508", "ejecutivo": "Ejecutivo actual"},
        "lifecycle": {
            "current_assignment_cycle_id": cycle_id,
            "assignment_cycle_id": cycle_id,
            "assigned_at": assigned_at,
            "sla_started_at": assigned_at,
        },
    }
    cycle = {
        "lead_id": lead_id,
        "assignment_cycle_id": cycle_id,
        "assigned_to_user_id": str(user_id),
        "assigned_to_display_name": "Ejecutivo actual",
        "property_code": "6508",
        "assigned_at": assigned_at,
        "sla_started_at": assigned_at,
        "cycle_status": "active",
        "unassigned_at": None,
    }
    db.leads.insert_one(deepcopy(lead))
    db.usuarios.insert_one({"_id": user_id, "nombre": "Ejecutivo actual", "telefono": "+56911111111"})
    db.crm_assignment_cycles.insert_one(deepcopy(cycle))
    db[PROPERTY_COLLECTION_NAME].insert_one(_property())
    original_notification = {
        "_id": "original-notification",
        "state": "sent",
        "lead_ids": [lead_id],
        "assignment_cycle_ids": [cycle_id],
        "sent_at": assigned_at,
    }
    db.crm_notifications_v1.insert_one(deepcopy(original_notification))
    db[commercial_intake.COLLECTION].insert_one({
        "source_inbound_provider_id": "test-recovery-context",
        "lead_id": lead_id,
        "phone": lead["phone"],
        "source_property_code": "6508",
        "commercial_processing_state": commercial_intake.WAITING_INVENTORY,
        "notification_id": original_notification["_id"],
    })
    old_sla = evaluate_sla_alert_dry_run(
        leads=[lead], cycles=[cycle], users=[{"_id": str(user_id), "telefono": "+56911111111"}],
        as_of=assigned_at + timedelta(hours=4), activation_at=assigned_at - timedelta(days=1),
    )
    assert old_sla["excluded"].get("waiting_assignment") == 1

    with patch.object(commercial_intake, "find_responsible_executive", side_effect=AssertionError("router called")), \
         patch.object(commercial_intake, "create_assignment_cycle", side_effect=AssertionError("cycle created")), \
         patch.object(commercial_intake, "find_property_by_any_identifier", wraps=commercial_intake.find_property_by_any_identifier) as lookup, \
         patch.object(LeadProcessingService, "classify", side_effect=AssertionError("classifier called")), \
         patch("chatbot.crm_non_hot_digest.accumulate_non_hot_lead", side_effect=AssertionError("notification enqueued")) as digest, \
         patch("chatbot.crm_hot_delivery.assign_and_enqueue_hot", side_effect=AssertionError("HOT notification enqueued")) as hot_delivery:
        event = commercial_intake.process_inbound(
            db,
            inbound_provider_id="test-recovery-context",
            phone=lead["phone"],
            text="La propiedad 6508",
        )

    updated = db.leads.find_one({"_id": lead_id})
    unchanged_cycle = db.crm_assignment_cycles.find_one({"assignment_cycle_id": cycle_id})
    assert updated["prospecto"]["codigo"] == "6508"
    assert updated["prospecto"]["comuna"] == "El Bosque"
    assert updated["prospecto"]["tipo"] == "Casa"
    assert updated["prospecto"]["operacion"] == "Venta"
    assert updated["comuna"] == "El Bosque"
    assert updated["comuna_norm"] == "el bosque"
    assert updated["tipo"] == "CASA"
    assert updated["operacion"] == "V"
    assert updated["cluster_id"] == "EL BOSQUE-CASA-V"
    assert updated["zone"] == "RM-SUR"
    assert updated["assignment_type"] == "PROPERTY"
    assert updated["ejecutivo_asignado"] == lead["ejecutivo_asignado"]
    assert updated["lifecycle"]["current_assignment_cycle_id"] == cycle_id
    assert unchanged_cycle["assignment_cycle_id"] == cycle_id
    assert unchanged_cycle["assigned_to_user_id"] == str(user_id)
    assert unchanged_cycle["assigned_to_display_name"] == "Ejecutivo actual"
    assert unchanged_cycle["property_code"] == "6508"
    assert unchanged_cycle["assigned_at"].replace(tzinfo=timezone.utc) == assigned_at
    assert unchanged_cycle["sla_started_at"].replace(tzinfo=timezone.utc) == assigned_at
    assert event["assignment_cycle_id"] == cycle_id
    assert event["notification_id"] == original_notification["_id"]
    unchanged_notification = db.crm_notifications_v1.find_one({"_id": original_notification["_id"]})
    assert unchanged_notification["state"] == original_notification["state"]
    assert unchanged_notification["lead_ids"] == original_notification["lead_ids"]
    assert unchanged_notification["assignment_cycle_ids"] == original_notification["assignment_cycle_ids"]
    assert unchanged_notification["sent_at"].replace(tzinfo=timezone.utc) == assigned_at
    assert digest.call_count == 0
    assert hot_delivery.call_count == 0
    assert lookup.call_count == 1
    assert db.crm_assignment_cycles.count_documents({"lead_id": lead_id}) == 1
    assert db.crm_notifications_v1.count_documents({}) == 1

    after_sla = evaluate_sla_alert_dry_run(
        leads=[updated], cycles=[cycle], users=[{"_id": str(user_id), "telefono": "+56911111111"}],
        as_of=assigned_at + timedelta(hours=4), activation_at=assigned_at - timedelta(days=1),
    )
    assert after_sla["excluded"].get("waiting_assignment", 0) == 0
    assert after_sla["writes"] == 0
    assert after_sla["provider_calls"] == 0


def test_process_lead_with_object_id_classifies_property_6508_without_assignment_or_notification():
    db = mongomock.MongoClient().URLS
    lead_id = ObjectId("6ac6f32d0035d45d0f86ce3a")
    user_id = ObjectId("64b000000000000000000001")
    cycle_id = "existing-process-cycle-6508"
    assigned_at = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)
    db.leads.insert_one({
        "_id": lead_id,
        "phone": "+56912345678",
        "stage": "NEW",
        "processing_required": True,
        "processing_event_id": "inbound-6508",
        "processing_reason": "incoming_message",
        "processing_state": "received",
        "ejecutivo_asignado": "Ejecutivo actual",
        "prospecto": {"codigo": "6508", "ejecutivo": "Ejecutivo actual"},
        "messages": [{"role": "user", "content": "La propiedad 6508"}],
        "lifecycle": {"current_assignment_cycle_id": cycle_id},
    })
    cycle = {
        "lead_id": lead_id,
        "assignment_cycle_id": cycle_id,
        "assigned_to_user_id": str(user_id),
        "assigned_to_display_name": "Ejecutivo actual",
        "property_code": "6508",
        "assigned_at": assigned_at,
        "sla_started_at": assigned_at,
        "cycle_status": "active",
        "unassigned_at": None,
    }
    db.crm_assignment_cycles.insert_one(deepcopy(cycle))
    db[PROPERTY_COLLECTION_NAME].insert_one(_property())

    with patch.object(LeadProcessingService, "_db", return_value=db), \
         patch.object(LeadProcessingService, "classify", wraps=LeadProcessingService.classify) as classify, \
         patch("chatbot.processing_service.find_responsible_executive", side_effect=AssertionError("router called")), \
         patch("chatbot.crm_metrics.create_assignment_cycle", side_effect=AssertionError("cycle created")):
        result = LeadProcessingService.process_lead(str(lead_id))

    updated = db.leads.find_one({"_id": lead_id})
    unchanged_cycle = db.crm_assignment_cycles.find_one({"assignment_cycle_id": cycle_id})
    assert result is True
    assert classify.call_count == 1
    assert updated["cluster_id"] == "EL BOSQUE-CASA-V"
    assert updated["zone"] == "RM-SUR"
    assert updated["comuna"] == "El Bosque"
    assert updated["tipo"] == "CASA"
    assert updated["operacion"] == "V"
    assert updated["ejecutivo_asignado"] == "Ejecutivo actual"
    assert updated["lifecycle"]["current_assignment_cycle_id"] == cycle_id
    assert updated["processing_state"] == "completed"
    assert unchanged_cycle["assignment_cycle_id"] == cycle_id
    assert unchanged_cycle["assigned_at"].replace(tzinfo=timezone.utc) == assigned_at
    assert unchanged_cycle["sla_started_at"].replace(tzinfo=timezone.utc) == assigned_at
    assert db.crm_assignment_cycles.count_documents({"lead_id": lead_id}) == 1
    assert db.crm_notifications_v1.count_documents({}) == 0
