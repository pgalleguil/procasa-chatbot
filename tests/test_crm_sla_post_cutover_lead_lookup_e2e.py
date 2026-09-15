from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import mongomock
import pytest
from bson import ObjectId

from config import Config
from chatbot.crm_metrics import create_assignment_cycle
from chatbot.crm_sla_reassignment_worker import (
    _batch_context,
    _resolve_lead_from_context,
    run_sla_reassignment_worker_iteration,
    scan_sla_reassignment_candidates,
)


UTC = timezone.utc


def _snapshot() -> dict:
    return {
        "candidates": [],
        "users_by_id": {},
        "team": {
            "sla_compliance_rate": 0.5,
            "attention_rate": 0.5,
            "team_p50_average": 30,
            "team_p90_average": 60,
        },
        "performance_snapshot_version": "post-cutover-lead-lookup-test",
        "catalog": {},
        "properties": {},
    }


def test_productive_cycle_creation_and_post_cutover_worker_lookup_are_canonical():
    db = mongomock.MongoClient().get_database("crm_sla_e2e")
    lead_id = ObjectId()
    db["leads"].insert_one({
        "_id": lead_id,
        "pipeline_stage": "NEW",
        "stage": "NEW",
        "lead_temperature_effective": "NORMAL",
        "ejecutivo_asignado": "Owner",
        "lifecycle": {},
    })
    persisted_lead = db["leads"].find_one({"_id": lead_id})

    cycle = create_assignment_cycle(
        db,
        lead=persisted_lead,
        assigned_to_user_id="owner",
        assigned_by="commercial_intake",
        reason="inbound_message",
        assigned_at=datetime(2026, 9, 10, 12, tzinfo=UTC),
        assigned_to_display_name="Owner",
    )
    db["leads"].update_one(
        {"_id": lead_id},
        {"$set": {"lifecycle.current_assignment_cycle_id": cycle["assignment_cycle_id"]}},
    )

    scanned = asyncio.run(scan_sla_reassignment_candidates(
        db,
        batch_size=10,
        current_policy_since=datetime(2026, 9, 1, tzinfo=UTC),
    ))
    assert scanned["scanned"] == 1
    assert scanned["cycles"][0]["lead_id"] == lead_id

    context = asyncio.run(_batch_context(db, scanned["cycles"]))
    resolved, lookup_status = _resolve_lead_from_context(context, lead_id)
    assert resolved is not None
    assert resolved["_id"] == lead_id
    assert lookup_status == "resolved_exact"

    previous_flags = (
        Config.CRM_SLA_REASSIGNMENT_ENABLED,
        Config.CRM_SLA_REASSIGNMENT_WORKER_ENABLED,
        Config.CRM_SLA_REASSIGNMENT_SHADOW_ENABLED,
    )
    Config.CRM_SLA_REASSIGNMENT_ENABLED = False
    Config.CRM_SLA_REASSIGNMENT_WORKER_ENABLED = True
    Config.CRM_SLA_REASSIGNMENT_SHADOW_ENABLED = True
    try:
        result = asyncio.run(run_sla_reassignment_worker_iteration(
            db=None,
            execution_mode="shadow",
            now=datetime(2026, 9, 10, 20, tzinfo=UTC),
            cutover_at=datetime(2026, 9, 1, tzinfo=UTC),
            scan_result={"cycles": scanned["cycles"], "next_page_token": None},
            context=context,
            performance_snapshot=_snapshot(),
            committed_events=[],
        ))
    finally:
        (
            Config.CRM_SLA_REASSIGNMENT_ENABLED,
            Config.CRM_SLA_REASSIGNMENT_WORKER_ENABLED,
            Config.CRM_SLA_REASSIGNMENT_SHADOW_ENABLED,
        ) = previous_flags

    assert result["iteration"]["errors"] == 0
    assert "LEAD_NOT_FOUND" not in result["iteration"]["error_codes"]


def test_cycle_creation_rejects_nonexistent_canonical_lead():
    db = mongomock.MongoClient().get_database("crm_sla_e2e_missing")
    with pytest.raises(ValueError, match="canonical lead document not found"):
        create_assignment_cycle(
            db,
            lead={"_id": ObjectId()},
            assigned_to_user_id="owner",
            assigned_by="commercial_intake",
            reason="inbound_message",
        )
