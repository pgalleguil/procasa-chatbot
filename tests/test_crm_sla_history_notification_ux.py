"""Focused contract tests for the final SLA CRM UX flow."""
from datetime import datetime, timezone

import mongomock

from chatbot.crm_assignment_history import get_historical_assignment_rows
from chatbot.crm_lead_access import LEAD_REASSIGNED_SLA_LOCKED, resolve_crm_lead_access_context
from chatbot.crm_sla_alert_templates import build_sla_message
from chatbot.crm_sla_reassignment_integration import _enqueue_post_commit_notifications
from chatbot.crm_sla_reassignment_models import SLAReassignmentResult
from chatbot.crm_sla_reassignment_notifications import (
    SLA_REASSIGNED_AWAY,
    SLA_REASSIGNED_TO,
    process_one_sla_reassignment_sync,
)


def _db():
    return mongomock.MongoClient().get_database("crm")


def test_history_is_cycle_sourced_read_only_and_excludes_contact_data():
    db = _db()
    lead_id = "lead-history-1"
    db["leads"].insert_one({
        "_id": lead_id,
        "prospecto": {
            "nombre": "Cliente Histórico",
            "codigo": "7733",
            "operacion": "Venta",
            "comuna": "Santiago",
            "email": "should-not-be-exported@example.invalid",
        },
        "phone": "+56900000000",
        "lead_temperature_effective": "HOT",
        "lifecycle": {"current_assignment_cycle_id": "cycle-current"},
    })
    db["crm_assignment_cycles"].insert_many([
        {
            "_id": "old-cycle",
            "assignment_cycle_id": "cycle-old",
            "lead_id": lead_id,
            "assigned_to_user_id": "old-owner",
            "assigned_to_display_name": "Mariela Arriagada",
            "assigned_at": datetime(2026, 9, 16, 15, tzinfo=timezone.utc),
            "sla_breached_at": datetime(2026, 9, 16, 16, tzinfo=timezone.utc),
            "reassigned_at": datetime(2026, 9, 16, 16, 1, tzinfo=timezone.utc),
            "cycle_status": "reassigned",
            "closed_reason": "sla_reassignment",
            "property_code": "7733",
            "temperature_at_assignment": "HOT",
        },
        {
            "_id": "current-cycle",
            "assignment_cycle_id": "cycle-current",
            "lead_id": lead_id,
            "assigned_to_user_id": "new-owner",
            "cycle_status": "active",
            "unassigned_at": None,
        },
    ])

    rows = get_historical_assignment_rows(db, user_id="old-owner")

    assert len(rows) == 1
    row = rows[0]
    assert row["assignment_cycle_id"] == "cycle-old"
    assert row["status_label"] == "Reasignado por SLA"
    assert row["codigo_propiedad"] == "7733"
    assert row["historical"] is True
    assert row["actions_disabled"] is True
    assert row["operational_url"] is None
    assert not {"phone", "email", "whatsapp", "notes", "recommendations"}.intersection(row)


def test_history_filter_reconciles_previous_owner_and_keeps_same_code_isolated():
    db = _db()
    from bson import ObjectId

    mariela_id = ObjectId()
    hernan_id = ObjectId()
    db["usuarios"].insert_many([
        {"_id": mariela_id, "nombre": "Mariela Arriagada", "rol": "agente"},
        {"_id": hernan_id, "nombre": "Hernán Castro", "rol": "agente"},
    ])

    lead_ids = [ObjectId() for _ in range(4)]
    codes = ["16897", "5695", "7733", "7733"]
    for index, (lead_id, code) in enumerate(zip(lead_ids, codes), start=1):
        db["leads"].insert_one({
            "_id": lead_id,
            "prospecto": {
                "nombre": f"Cliente histórico {index}",
                "codigo": code,
                "operacion": "Venta",
                "comuna": "Santiago",
            },
            "phone": f"+569000000{index:02d}",
            "email": f"private-{index}@example.invalid",
            "lead_temperature_effective": "HOT" if index == 1 else "NORMAL",
            "lifecycle": {"current_assignment_cycle_id": f"dest-{index}"},
        })
        db["crm_assignment_cycles"].insert_many([
            {
                "_id": f"source-doc-{index}",
                "assignment_cycle_id": f"source-{index}",
                "lead_id": lead_id,
                "assigned_to_user_id": mariela_id,
                "assigned_to_display_name": "Mariela Arriagada",
                "assigned_at": datetime(2026, 9, 16, 13, index, tzinfo=timezone.utc),
                "sla_breached_at": datetime(2026, 9, 16, 16, index, tzinfo=timezone.utc),
                "reassigned_at": datetime(2026, 9, 16, 16, 1, index, tzinfo=timezone.utc),
                "cycle_status": "reassigned",
                "closed_reason": "sla_reassignment",
                "property_code": code,
                "temperature_at_assignment": "HOT" if index == 1 else "NORMAL",
            },
            {
                "_id": f"destination-doc-{index}",
                "assignment_cycle_id": f"dest-{index}",
                "lead_id": lead_id,
                "assigned_to_user_id": hernan_id,
                "assigned_to_display_name": "Hernán Castro",
                "cycle_status": "active",
                "unassigned_at": None,
                "reassignment_source_cycle_id": f"source-{index}",
            },
        ])

    rows = get_historical_assignment_rows(
        db, user_name="Mariela Arriagada", limit=100
    )

    assert len(rows) == 4
    assert {(row["lead_id"], row["assignment_cycle_id"]) for row in rows} == {
        (str(lead_id), f"source-{index}")
        for index, lead_id in enumerate(lead_ids, start=1)
    }
    assert [row["codigo_propiedad"] for row in rows].count("7733") == 2
    assert all(row["status_label"] == "Reasignado por SLA" for row in rows)
    assert all(row["sla_state_label"] == "Vencido" for row in rows)
    assert all(row["response_label"] == "🔒 Reasignado" for row in rows)
    assert all(row["operational_url"] is None and row["actions_disabled"] for row in rows)
    assert not any(
        {"phone", "email", "whatsapp", "notes", "recommendations", "url"}.intersection(row)
        for row in rows
    )


def test_history_filter_contract_is_visible_and_non_operational():
    from pathlib import Path

    template = Path(__file__).parents[1].joinpath(
        "templates", "crm_leads_list.html"
    ).read_text(encoding="utf-8")

    assert 'value="SLA_REASSIGNED_HISTORY"' in template
    assert "Reasignados por SLA" in template
    assert "Reasignados SLA:" in template
    assert "history_filter_active" in template
    history_block = template.split("{% if history_filter_active %}", 1)[1].split(
        "{% endif %}", 1
    )[0]
    assert "data-lead-url" not in history_block
    assert "data-quick-management" not in history_block
    assert "data-phone" not in history_block


def test_old_owner_is_locked_while_current_owner_remains_allowed():
    db = _db()
    lead = {
        "_id": "lead-lock-1",
        "ejecutivo_asignado": "Hernán Castro",
        "prospecto": {"ejecutivo": "Hernán Castro"},
        "lifecycle": {"current_assignment_cycle_id": "cycle-current"},
        "reassigned_by_sla": True,
    }
    db["leads"].insert_one(lead)
    db["crm_assignment_cycles"].insert_one({
        "_id": "cycle-doc",
        "assignment_cycle_id": "cycle-current",
        "lead_id": lead["_id"],
        "assigned_to_user_id": "new-owner",
        "assigned_to_display_name": "Hernán Castro",
        "cycle_status": "active",
        "unassigned_at": None,
    })

    old = resolve_crm_lead_access_context(
        db, user={"_id": "old-owner", "rol": "agente"}, lead=lead, security_enabled=True
    )
    current = resolve_crm_lead_access_context(
        db, user={"_id": "new-owner", "rol": "agente"}, lead=lead, security_enabled=True
    )

    assert old.access_allowed is False
    assert old.lock_reason == LEAD_REASSIGNED_SLA_LOCKED
    assert all(value is False for value in old.action_permissions.values())
    assert current.access_allowed is True
    assert current.action_permissions["management_result"] is True


def test_breach_warning_is_race_safe_and_uses_no_technical_ids():
    message = build_sla_message(
        hot=False,
        breached=True,
        client_first_name="Ana",
        property_code="7733",
        elapsed_minutes=181,
        deadline_display="16/09/2026 13:00",
        lead_url="https://crm.example/crm/lead-id/opaque",
        outreach_state="none",
    )

    assert "⚠️ Lead con SLA vencido" in message
    assert "Aún no existe una gestión válida registrada." in message
    assert "Si el lead continúa asignado a ti" in message
    assert "Si ya fue reasignado" in message
    assert "Ciclo:" not in message
    assert "assignment_cycle" not in message


def test_post_commit_creates_independent_away_and_to_notifications_and_delivers_once():
    db = _db()
    lead_id = "lead-notification-1"
    source_cycle_id = "cycle-source"
    destination_cycle_id = "cycle-destination"
    decision_id = "decision-notification-1"
    db["leads"].insert_one({
        "_id": lead_id,
        "prospecto": {
            "nombre": "Cliente de Prueba",
            "codigo": "5695",
            "operacion": "Arriendo",
            "comuna": "Ñuñoa",
        },
        "lead_temperature_effective": "NORMAL",
        "lifecycle": {"current_assignment_cycle_id": destination_cycle_id},
        "assignment_mirror_owner_user_id": "new-owner",
    })
    db["crm_assignment_cycles"].insert_many([
        {
            "_id": "source-doc",
            "assignment_cycle_id": source_cycle_id,
            "lead_id": lead_id,
            "assigned_to_user_id": "old-owner",
            "cycle_status": "reassigned",
            "closed_reason": "sla_reassignment",
            "reassigned_at": datetime(2026, 9, 16, 17, tzinfo=timezone.utc),
            "property_code": "5695",
        },
        {
            "_id": "destination-doc",
            "assignment_cycle_id": destination_cycle_id,
            "lead_id": lead_id,
            "assigned_to_user_id": "new-owner",
            "cycle_status": "active",
            "unassigned_at": None,
            "property_code": "5695",
        },
    ])
    db["usuarios"].insert_many([
        {"_id": "old-owner", "telefono": "+56911111111"},
        {"_id": "new-owner", "telefono": "+56922222222"},
    ])
    decision = {
        "decision_id": decision_id,
        "lead_id": lead_id,
        "current_assignment_cycle_id": source_cycle_id,
        "previous_owner_user_id": "old-owner",
        "selected_user_id": "new-owner",
    }
    result = SLAReassignmentResult(
        decision_id=decision_id,
        lead_id=lead_id,
        source_cycle_id=source_cycle_id,
        destination_cycle_id=destination_cycle_id,
        previous_owner_user_id="old-owner",
        selected_user_id="new-owner",
        status="APPLIED",
        outcome="APPLIED",
        committed=True,
        idempotent_replay=False,
        policy_version="crm_sla_reassignment_v1",
        automatic_reassignment_number=1,
        transaction_attempts=1,
    )

    created = _enqueue_post_commit_notifications(db, decision=decision, result=result)
    notifications = list(db["crm_notifications_v1"].find({}))
    assert created and len(notifications) == 2
    assert {row["notification_type"] for row in notifications} == {SLA_REASSIGNED_AWAY, SLA_REASSIGNED_TO}
    messages = [row["payload"]["message"] for row in notifications]
    assert all(decision_id not in message for message in messages)
    assert all(source_cycle_id not in message and destination_cycle_id not in message for message in messages)

    calls = []

    def sender(phone, message):
        calls.append((phone, message))
        return {"success": True, "provider_message_id": f"provider-{len(calls)}", "http_status": 200}

    assert process_one_sla_reassignment_sync(db, worker_id="notifier-1", sender=sender)["status"] == "sent"
    assert process_one_sla_reassignment_sync(db, worker_id="notifier-2", sender=sender)["status"] == "sent"
    assert process_one_sla_reassignment_sync(db, worker_id="notifier-3", sender=sender)["status"] == "idle"
    assert len(calls) == 2
    stored = list(db["crm_notifications_v1"].find({}))
    assert all(row["state"] == "sent" and row["provider_message_id"] for row in stored)
    assert all(len(row["delivery_attempts"]) == 1 for row in stored)
