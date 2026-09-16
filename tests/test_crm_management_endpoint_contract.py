"""Regression coverage for the authenticated CRM management endpoint contract."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import mongomock


class _JsonRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


def test_management_endpoint_does_not_pass_followup_token_to_canonical_writer(monkeypatch):
    """The direct CRM route must match record_management_result's explicit API."""
    import chatbot.crm_management as crm_management
    import chatbot.storage as storage

    bootstrap_db = mongomock.MongoClient()["crm_management_endpoint_contract"]
    cycle = {
        "_id": "cycle-doc",
        "assignment_cycle_id": "cycle-current",
        "lead_id": "lead-1",
        "assigned_to_user_id": "hernan-1",
        "cycle_status": "active",
        "unassigned_at": None,
    }
    captured = {}

    def canonical_writer(
        db, *, lead_id, assignment_cycle_id, actor_user_id, result_type,
        source, idempotency_key, occurred_at=None, next_follow_up_at=None,
        details_json=None, stage_override=None, legacy_stage=None,
        actor_can_manage_any_cycle=False,
    ):
        captured.update({
            "db": db,
            "lead_id": lead_id,
            "assignment_cycle_id": assignment_cycle_id,
            "actor_user_id": actor_user_id,
            "result_type": result_type,
            "source": source,
        })
        return {"result_type": result_type, "follow_up_required": False}

    monkeypatch.setattr(storage, "get_db", lambda: bootstrap_db)
    monkeypatch.setattr(crm_management, "_find_assignment_cycle", lambda *args, **kwargs: cycle)
    monkeypatch.setattr(crm_management, "record_management_result", canonical_writer)

    import webhook

    async def authorized_lead(_request, phone):
        assert phone == "56911111111"
        return (
            {"_id": "hernan-1", "rol": "agente"},
            {"_id": "lead-1", "phone": phone},
        )

    monkeypatch.setattr(webhook, "_get_authorized_crm_lead", authorized_lead)

    payload = {
        "phone": "56911111111",
        "assignment_cycle_id": "cycle-current",
        "result_type": "CALL_NO_ANSWER",
        "management_request_id": "management-contract-regression",
        "followup_token": "signed-followup-token-is-not-a-writer-argument",
        "details_json": {"notes": "contract regression"},
    }
    response = asyncio.run(webhook.api_crm_management_result(_JsonRequest(payload)))

    assert response["status"] == "ok"
    assert captured["db"] is bootstrap_db
    assert captured["lead_id"] == "lead-1"
    assert captured["assignment_cycle_id"] == "cycle-current"
    assert captured["actor_user_id"] == "hernan-1"

