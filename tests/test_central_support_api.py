"""Central commercial support: strict operator, scope and send gates."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import mongomock
import pytest
from bson import ObjectId

from chatbot import central_support_api as api
from chatbot.crm_executive_outbound import OutboundBlocked

PABLO = ObjectId("69796bc4bbebf240378eb739")
OWNER = ObjectId("6a681413140190dde11f26d1")
LEAD = ObjectId("6b05a9e1acb92f0123456789")
TASK = "active_search:manual-candidate-test"
CYCLE = "cycle-current"


@pytest.fixture
def db():
    instance = mongomock.MongoClient().central_support
    instance.usuarios.insert_one({
        "_id": PABLO, "is_active": True, "nombre": "Pablo Galleguillos",
        "rol": "agente",
    })
    instance.leads.insert_one({
        "_id": LEAD, "phone": "+56911112222",
        "ejecutivo_asignado": "Hernán Castro",
        "lifecycle": {
            "current_assignment_cycle_id": CYCLE,
            "assigned_to_user_id": str(OWNER),
        },
        "commercial_intelligence": {"active_search": {
            "status": "ACTIVE", "followup_required": True,
            "opt_out": False, "criteria": {"parking_required": True},
        }},
    })
    instance.crm_central_support_tasks.insert_one({
        "_id": TASK, "task_id": TASK, "lead_id": LEAD,
        "assignment_cycle_id": CYCLE,
        "related_executive_user_id": str(OWNER),
        "recipient_user_id": str(PABLO),
        "phone": "+56911112222", "agent": "Hernán Castro",
        "type": "SEARCH_ALTERNATIVES", "source": "verified_active_search",
        "status": "pending", "note": "Estacionamiento necesario.",
        "created_at": datetime(2026, 11, 23, 18, tzinfo=timezone.utc),
    })
    return instance


@pytest.fixture
def user():
    return {"_id": PABLO, "nombre": "Pablo Galleguillos", "rol": "agente"}


def test_executive_crm_tasks_are_never_created_for_support_requests(db):
    assert db.crm_tasks.count_documents({}) == 0
    assert db.crm_central_support_tasks.count_documents({
        "recipient_user_id": str(PABLO),
        "related_executive_user_id": str(OWNER),
    }) == 1


def test_support_view_requires_exact_verified_pablo_and_opt_in(db, user, monkeypatch):
    monkeypatch.setenv(api.VIEW_FLAG, "1")
    assert api._operator_authorized(db, user)
    assert not api._operator_authorized(db, user, send=True)
    assert not api._operator_authorized(
        db, {**user, "nombre": "Alguien distinto"}
    )
    assert not api._operator_authorized(
        db, {**user, "_id": ObjectId()}
    )
    monkeypatch.setenv(api.SEND_FLAG, "1")
    assert api._operator_authorized(db, user, send=True)
    db.usuarios.update_one({"_id": PABLO}, {"$set": {"is_active": False}})
    assert not api._operator_authorized(db, user)


def test_inbox_only_includes_open_current_commercial_searches(db):
    rows = api._open_tasks(db)
    assert len(rows) == 1
    assert rows[0]["executive"] == "Hernán Castro"
    assert "Estacionamiento" in rows[0]["note"]
    assert "messages" not in rows[0]
    db.leads.update_one(
        {"_id": LEAD},
        {"$set": {"lifecycle.current_assignment_cycle_id": "new-cycle"}},
    )
    assert api._open_tasks(db) == []


def test_explicit_operator_send_uses_current_owner_and_verifies_recording(db, user, monkeypatch):
    monkeypatch.setattr(api, "_outstanding_uncertain", lambda *_a, **_kw: False)
    calls = []

    def fake_send(db_, **kwargs):
        calls.append(kwargs)
        return {"status": "accepted", "recorded": True,
                "provider_message_id": "provider-123"}

    monkeypatch.setattr(api, "send_manual", fake_send)
    data = {"lead_id": str(LEAD), "task_id": TASK,
            "text": "Le propongo una propiedad verificada.",
            "idempotency_key": "central-test-12345",
            "confirmed": True}
    response = api._send_checked(db, user=user, data=data)
    assert response["status"] == "accepted" and response["recorded"] is True
    assert len(calls) == 1
    assert calls[0]["actor_user_id"] == str(OWNER)
    assert calls[0]["requester_user"] is user
    assert calls[0]["central_support"] is True
    assert db.crm_central_support_tasks.find_one({"_id": TASK})["last_central_send_provider_id"] == "provider-123"


@pytest.mark.parametrize("change", [
    {"commercial_intelligence.active_search.status": "COMPLETED"},
    {"commercial_intelligence.active_search.opt_out": True},
    {"phone_is_synthetic": True},
    {"contact_restrictions.marketing_opt_out": True},
    {"lifecycle.assigned_to_user_id": "different-owner"},
])
def test_no_cross_owner_no_optout_no_synthetic_send(db, user, monkeypatch, change):
    db.leads.update_one({"_id": LEAD}, {"$set": change})
    monkeypatch.setattr(api, "_outstanding_uncertain",
                        lambda *_a, **_kw: False)
    monkeypatch.setattr(
        api, "send_manual",
        lambda *_a, **_kw: pytest.fail("Provider must not be called"),
    )
    with pytest.raises(OutboundBlocked):
        api._send_checked(db, user=user, data={
            "lead_id": str(LEAD), "task_id": TASK,
            "text": "No enviar", "idempotency_key": "central-test-54321",
            "confirmed": True,
        })


def test_confirmation_is_required_and_template_has_explicit_gates(db, user):
    with pytest.raises(OutboundBlocked, match="invalid_support_request"):
        api._send_checked(db, user=user, data={
            "lead_id": str(LEAD), "task_id": TASK,
            "text": "No enviar", "idempotency_key": "central-no-confirm",
            "confirmed": False,
        })
    from jinja2 import Environment
    source = Path("templates/crm_central_support.html").read_text(encoding="utf8")
    Environment().parse(source)
    assert "id=\"confirm\"" in source
    assert "id=\"send\"" in source
    assert "X-Requested-With" in source
    assert "/api/crm/central-support/send" in source
    assert "confirm.checked" in source


def test_previous_unknown_delivery_must_be_resolved_before_any_more_outreach(db, user, monkeypatch):
    monkeypatch.setattr(api, "_outstanding_uncertain",
                        lambda *_a, **_kw: True)
    monkeypatch.setattr(
        api, "send_manual",
        lambda *_a, **_kw: pytest.fail("Should never call provider"),
    )
    with pytest.raises(OutboundBlocked, match="previous_delivery_unconfirmed"):
        api._send_checked(db, user=user, data={
            "lead_id": str(LEAD), "task_id": TASK,
            "text": "No enviar", "idempotency_key": "central-no-retry-1",
            "confirmed": True,
        })
