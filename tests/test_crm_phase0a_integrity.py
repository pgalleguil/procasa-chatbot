"""Phase 0A invariants: authorization, canonical results, stale cycles and idempotency."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import mongomock
import pytest
from fastapi import HTTPException

from api_crm import manage_crm_notes
from chatbot.crm_management import (
    StaleAssignmentCycleError,
    record_management_result,
    record_legacy_management_result,
)
from chatbot.crm_metrics import event_evidence
from tests.test_crm_management_sla import fixture


def test_note_contains_audit_identity_and_does_not_stop_sla(monkeypatch):
    client = mongomock.MongoClient()
    db = client["phase0a"]
    lead = {
        "_id": "lead-note",
        "phone": "+56911111111",
        "lifecycle": {},
        "sticky_notes": [],
    }
    db["leads"].insert_one(lead)
    monkeypatch.setattr("api_crm.get_db", lambda: db)

    note = manage_crm_notes(
        lead["phone"], {"content": "seguimiento", "timestamp_iso": "2026-08-20T12:00:00-04:00"},
        lead_id=lead["_id"], actor_user_id="user-a", assignment_cycle_id="cycle-a",
    )

    stored = db["leads"].find_one({"_id": lead["_id"]})
    assert note["lead_id"] == "lead-note"
    assert note["actor_user_id"] == "user-a"
    assert note["assignment_cycle_id"] == "cycle-a"
    assert "first_valid_management_at" not in stored["lifecycle"]
    audit = db["crm_events"].find_one({"type": "CRM_NOTE_ADDED"})
    assert audit["lead_id"] == "lead-note"
    assert event_evidence(audit)["management"] is False


@pytest.mark.asyncio
async def test_crm_authorization_rejects_other_owner_and_unauthenticated():
    import webhook

    lead_b = {"_id": "lead-b", "ejecutivo_asignado": "Ejecutivo B", "prospecto": {}}
    agent_a = {"_id": "user-a", "rol": "agente", "nombre": "Ejecutivo A"}

    with patch.object(webhook, "get_current_user_doc", new=AsyncMock(return_value=agent_a)), \
         patch.object(webhook.CrmService, "get_lead", return_value=lead_b):
        with pytest.raises(HTTPException) as denied:
            await webhook._get_authorized_crm_lead(SimpleNamespace(), "+56911111111")
    assert denied.value.status_code == 403

    with patch.object(webhook, "get_current_user_doc", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as unauthenticated:
            await webhook._get_authorized_crm_lead(SimpleNamespace(), "+56911111111")
    assert unauthenticated.value.status_code == 401


@pytest.mark.asyncio
async def test_notes_endpoint_returns_auth_statuses():
    import webhook

    class Request:
        async def json(self):
            return {"action": "add", "phone": "+56911111111", "note": {"content": "x"}}

    agent_a = {"_id": "user-a", "rol": "agente", "nombre": "Ejecutivo A"}
    lead_b = {"_id": "lead-b", "ejecutivo_asignado": "Ejecutivo B", "prospecto": {}}
    with patch.object(webhook, "get_current_user_doc", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as unauthenticated:
            await webhook.api_crm_notes(Request())
    assert unauthenticated.value.status_code == 401

    with patch.object(webhook, "get_current_user_doc", new=AsyncMock(return_value=agent_a)), \
         patch.object(webhook.CrmService, "get_lead", return_value=lead_b):
        with pytest.raises(HTTPException) as denied:
            await webhook.api_crm_notes(Request())
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_supervisor_retains_authorized_access():
    import webhook

    supervisor = {"_id": "supervisor-1", "rol": "supervisor", "nombre": "Jefatura"}
    lead = {"_id": "lead-a", "ejecutivo_asignado": "Ejecutivo A", "prospecto": {}}
    with patch.object(webhook, "get_current_user_doc", new=AsyncMock(return_value=supervisor)), \
         patch.object(webhook.CrmService, "get_lead", return_value=lead):
        _, resolved = await webhook._get_authorized_crm_lead(SimpleNamespace(), "+56911111111")
    assert resolved["_id"] == "lead-a"


def test_detail_adapter_writes_only_canonical_result_and_is_idempotent():
    db, lead, cycle = fixture()
    payload = {
        "resultado_gestion": "requiere_seguimiento",
        "interaction_type": "hable",
        "next_action_date": "2026-08-21T10:00:00-04:00",
        "notas": "Enviar ficha",
        "management_request_id": "request-followup-1",
        "details_json": {"close_cat_radio": ""},
    }
    first = record_legacy_management_result(
        db, lead=lead, actor_user_id="user-1", actor_can_manage_any_cycle=False,
        assignment_cycle_id=cycle["assignment_cycle_id"], data=payload,
    )
    second = record_legacy_management_result(
        db, lead=lead, actor_user_id="user-1", actor_can_manage_any_cycle=False,
        assignment_cycle_id=cycle["assignment_cycle_id"], data=payload,
    )

    assert first["_id"] == second["_id"]
    assert first["result_type"] == "FOLLOW_UP_REQUESTED"
    assert len(db["crm_management_results"].docs) == 1
    assert len(db["crm_events"].docs) == 1
    assert len(db["crm_tasks"].docs) == 1
    assert db["crm_events"].docs[0]["type"] == "CONTACT_RESULT"
    assert all(event["type"] != "HUMAN_NOTE" for event in db["crm_events"].docs)


def test_stale_cycle_rejected_without_any_write():
    db, lead, cycle = fixture()
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": cycle["assignment_cycle_id"]},
        {"$set": {"cycle_status": "closed", "unassigned_at": "2026-08-20T11:00:00Z"}},
    )
    db["crm_assignment_cycles"].docs.append({
        "_id": "cycle-doc-2", "lead_id": lead["_id"], "assignment_cycle_id": "cycle-2",
        "assigned_to_user_id": "user-2", "assigned_at": cycle["assigned_at"],
        "unassigned_at": None, "cycle_status": "active", "schema_version": "crm_assignment_cycle_v1",
    })

    with pytest.raises(StaleAssignmentCycleError) as exc:
        record_legacy_management_result(
            db, lead=lead, actor_user_id="user-1", actor_can_manage_any_cycle=False,
            assignment_cycle_id="cycle-1", data={"resultado_gestion": "contactado", "management_request_id": "stale-1"},
        )
    assert str(exc.value) == "stale_assignment_cycle"
    assert db["crm_management_results"].docs == []
    assert db["crm_events"].docs == []
    assert db["leads"].find_one({"_id": lead["_id"]})["lifecycle"] == {}


def test_wrong_owner_cannot_record_result():
    db, lead, cycle = fixture()
    with pytest.raises(PermissionError):
        record_legacy_management_result(
            db, lead=lead, actor_user_id="user-2", actor_can_manage_any_cycle=False,
            assignment_cycle_id=cycle["assignment_cycle_id"], data={"resultado_gestion": "contactado", "management_request_id": "wrong-owner-1"},
        )
    assert db["crm_management_results"].docs == []


def test_detail_transports_cycle_and_stable_request_key():
    template = Path("templates/crm_lead_detail.html").read_text(encoding="utf-8")
    assert "assignment_cycle_id: \"{{ lead.assignment_cycle_id or '' }}\"" in template
    assert "management_request_id: managementRequestId" in template
    assert "managementRequestIdForCurrentIntent" in template
    assert "pendingManagementFingerprint" not in template


def _canonical_payload(result, request_id):
    return {
        "lead_id": "lead-1", "assignment_cycle_id": "cycle-1", "actor_user_id": "user-1",
        "result_type": result, "source": "test", "idempotency_key": request_id,
    }


def test_same_management_request_id_is_one_result():
    db, _, cycle = fixture()
    payload = _canonical_payload("CALL_NO_ANSWER", "request-a")
    first = record_management_result(db, **payload)
    second = record_management_result(db, **payload)
    assert first["_id"] == second["_id"]
    assert len(db["crm_management_results"].docs) == 1


def test_http_retry_is_one_result():
    db, _, cycle = fixture()
    payload = _canonical_payload("CALL_NO_ANSWER", "request-http-retry")
    record_management_result(db, **payload)
    record_management_result(db, **payload)
    assert len(db["crm_management_results"].docs) == 1


def test_two_real_call_no_answer_attempts_are_two_results():
    db, _, cycle = fixture()
    for request_id in ("request-a", "request-b"):
        record_management_result(db, **_canonical_payload("CALL_NO_ANSWER", request_id))
    assert sum(row["result_type"] == "CALL_NO_ANSWER" for row in db["crm_management_results"].docs) == 2


def test_two_real_effective_contacts_are_two_results():
    db, _, cycle = fixture()
    for request_id in ("request-a", "request-b"):
        record_management_result(db, **_canonical_payload("EFFECTIVE_CONTACT", request_id))
    assert sum(row["result_type"] == "EFFECTIVE_CONTACT" for row in db["crm_management_results"].docs) == 2


def test_missing_legacy_key_is_rejected_instead_of_hashed():
    db, lead, cycle = fixture()
    with pytest.raises(ValueError, match="management_request_id is required"):
        record_legacy_management_result(
            db, lead=lead, actor_user_id="user-1", actor_can_manage_any_cycle=False,
            assignment_cycle_id=cycle["assignment_cycle_id"],
            data={"resultado_gestion": "contactado"},
        )


def test_visit_and_closure_semantics_are_preserved():
    db, lead, cycle = fixture()
    visit = record_legacy_management_result(
        db, lead=lead, actor_user_id="user-1", actor_can_manage_any_cycle=False,
        assignment_cycle_id=cycle["assignment_cycle_id"],
        data={"resultado_gestion": "visita_agendada", "management_request_id": "visit-1"},
    )
    assert visit["result_type"] == "VISIT_SCHEDULED"
    assert visit["pipeline_stage_at_result"] == "VISIT_SCHEDULED"

    # A new cycle is used only to test a separate historical close result.
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": cycle["assignment_cycle_id"]},
        {"$set": {"cycle_status": "closed", "unassigned_at": "2026-08-20T12:00:00Z"}},
    )
    db["crm_assignment_cycles"].insert_one({
        "_id": "cycle-doc-2", "lead_id": lead["_id"], "assignment_cycle_id": "cycle-2",
        "assigned_to_user_id": "user-1", "assigned_at": cycle["assigned_at"],
        "unassigned_at": None, "cycle_status": "active", "schema_version": "crm_assignment_cycle_v1",
    })
    won = record_legacy_management_result(
        db, lead=lead, actor_user_id="user-1", actor_can_manage_any_cycle=False,
        assignment_cycle_id="cycle-2",
        data={"resultado_gestion": "lead_cerrado", "management_request_id": "won-1",
              "details_json": {"close_cat_radio": "ganado", "close_reason": "venta_concretada"}},
    )
    assert won["result_type"] == "CLOSED_WON"
    assert won["pipeline_stage_at_result"] == "CLOSED_WON"
    assert won["result_type"] != "NOT_INTERESTED"
