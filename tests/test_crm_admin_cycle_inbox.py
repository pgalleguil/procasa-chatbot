"""Regression coverage for administrative cycles in the CRM inbox."""
import asyncio
from datetime import datetime, timedelta, timezone

import mongomock
from bson import ObjectId

import api_crm
from chatbot.constants import CHILE_TZ, PipelineStage
from chatbot.crm_metrics import calculate_cycle_sla
from chatbot.crm_sla_reassignment_worker import canonical_expiration_recheck
from chatbot.crm_updates import bump_crm_leads_version, get_crm_leads_version


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    async def to_list(self, length=None):
        return self.rows[:length] if length is not None else self.rows


class _Collection:
    def __init__(self, rows):
        self.rows = list(rows)
        self.last_query = None
        self.last_projection = None

    def find(self, query, projection=None):
        self.last_query = query
        self.last_projection = projection
        return _Cursor(self.rows)


class _DB:
    def __init__(self, **collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections[name]


def _admin_cycle(lead_id, *, owner_id="mariela-user", owner_name="Mariela Arriagada"):
    correction_id = f"HOLIDAY_SLA_CORRECTION_2026_10_12:{lead_id}"
    return {
        "lead_id": lead_id,
        "assignment_cycle_id": f"admin-correction:{correction_id}:v1",
        "administrative_correction_id": correction_id,
        "schema_version": "crm_assignment_cycle_v1",
        "cycle_status": "active",
        "unassigned_at": None,
        "reason": "lead_created",
        "cycle_origin": "inbound_message",
        "assigned_to_user_id": owner_id,
        "assigned_to_display_name": owner_name,
        "assigned_at": datetime(2026, 10, 13, 15, 59, tzinfo=timezone.utc),
        "notification_eligible": False,
        "first_valid_management_at": None,
        "sla_first_management_status": "pending",
    }


def _lead_for(cycle):
    return {
        "_id": cycle["lead_id"],
        "ejecutivo_asignado": cycle["assigned_to_display_name"],
        "prospecto": {"ejecutivo": cycle["assigned_to_display_name"]},
        "lifecycle": {
            "current_assignment_cycle_id": cycle["assignment_cycle_id"],
            "assigned_to_user_id": cycle["assigned_to_user_id"],
        },
        "pipeline_stage": "CONTACTED",
    }


def test_admin_cycle_is_canonical_even_when_notification_ineligible():
    lead_id = ObjectId("6acce7e71df961b65ef4cdae")
    cycle = _admin_cycle(lead_id)
    lead = _lead_for(cycle)

    assert api_crm._is_valid_administrative_cycle(
        cycle, lead, expected_owner_user_id="mariela-user"
    )
    predicate = api_crm._administrative_cycle_lookup_predicate()
    assert "notification_eligible" not in repr(predicate)
    assert "$$lead_current_cycle_id" in repr(predicate)
    assert "$$lead_owner_user_id" in repr(predicate)
    assert "$$lead_id" in repr(predicate)


def test_admin_cycle_rejects_stale_cycle_owner_or_owner_mirror():
    lead_id = ObjectId("6acce7e71df961b65ef4cdae")
    cycle = _admin_cycle(lead_id)
    lead = _lead_for(cycle)

    assert not api_crm._is_valid_administrative_cycle(
        cycle, lead, expected_owner_user_id="another-user"
    )
    stale = {**lead, "lifecycle": {**lead["lifecycle"], "current_assignment_cycle_id": "old-cycle"}}
    assert not api_crm._is_valid_administrative_cycle(cycle, stale)
    wrong_mirror = {**lead, "ejecutivo_asignado": "Otra persona", "prospecto": {"ejecutivo": "Otra persona"}}
    assert not api_crm._is_valid_administrative_cycle(cycle, wrong_mirror)


def test_pending_cycle_ignores_old_cycle_result_but_not_current_cycle_result():
    lead_id = ObjectId("6acce7e71df961b65ef4cdae")
    cycle = _admin_cycle(lead_id)
    old_result = {
        "lead_id": lead_id,
        "assignment_cycle_id": "rescued-cycle-before-correction",
        "result_type": "EFFECTIVE_CONTACT",
        "status": "completed",
        "occurred_at": cycle["assigned_at"] - timedelta(days=1),
    }
    misdated_current_result = {
        **old_result,
        "assignment_cycle_id": cycle["assignment_cycle_id"],
        "result_type": "EFFECTIVE_CONTACT",
    }
    old_event = {
        "lead_id": lead_id,
        "assignment_cycle_id": "rescued-cycle-before-correction",
        "type": "HUMAN_NOTE",
        "actor": "Mariela Arriagada",
        "actor_type": "human",
        "result": "EFFECTIVE_CONTACT",
        "timestamp": cycle["assigned_at"] - timedelta(days=1),
    }
    db = _DB(
        crm_assignment_cycles=_Collection([cycle]),
        leads=_Collection([_lead_for(cycle)]),
        crm_management_results=_Collection([old_result, misdated_current_result]),
        crm_events=_Collection([old_event]),
    )
    pending, current_ids = asyncio.run(api_crm._get_administrative_cycle_lead_ids(
        db, user_role="agente", user_name="Mariela Arriagada",
        user_id="mariela-user",
    ))
    assert pending == {lead_id}
    assert current_ids == {lead_id}
    assert "notification_eligible" not in repr(db["crm_assignment_cycles"].last_query)
    assert db["crm_assignment_cycles"].last_projection["assigned_at"] == 1

    current_event = {
        **old_event,
        "assignment_cycle_id": cycle["assignment_cycle_id"],
        "timestamp": cycle["assigned_at"] + timedelta(minutes=10),
    }
    db.collections["crm_management_results"] = _Collection([old_result, misdated_current_result])
    db.collections["crm_events"] = _Collection([old_event, current_event])
    pending_with_current_event, _ = asyncio.run(api_crm._get_administrative_cycle_lead_ids(
        db, user_role="agente", user_name="Mariela Arriagada",
        user_id="mariela-user",
    ))
    assert pending_with_current_event == set()

    current_result = {
        **old_result,
        "assignment_cycle_id": cycle["assignment_cycle_id"],
        "result_type": "NOT_INTERESTED",
        "occurred_at": cycle["assigned_at"] + timedelta(minutes=20),
    }
    db.collections["crm_management_results"] = _Collection([old_result, current_result])
    pending_with_current_result, _ = asyncio.run(api_crm._get_administrative_cycle_lead_ids(
        db, user_role="agente", user_name="Mariela Arriagada",
        user_id="mariela-user",
    ))
    assert pending_with_current_result == set()


def test_pending_admin_cycle_is_projected_into_new_without_rewriting_contacted_history():
    lead_id = ObjectId("6acce7e71df961b65ef4cdae")
    db = mongomock.MongoClient().db
    db.leads.insert_many([
        {"_id": lead_id, "pipeline_stage": "CONTACTED"},
        {"_id": ObjectId(), "pipeline_stage": "NEW"},
    ])
    new_query = api_crm._stage_query_with_pending_admin_cycles(
        api_crm._crm_stage_query([PipelineStage.NEW, "NEW"]),
        {lead_id}, include_pending=True,
    )
    managed_query = api_crm._stage_query_with_pending_admin_cycles(
        api_crm._crm_stage_query([PipelineStage.CONTACTED, "CONTACTED"]),
        {lead_id}, include_pending=False,
    )

    assert {row["_id"] for row in db.leads.find(new_query)} == {lead_id, db.leads.find_one({"pipeline_stage": "NEW"})["_id"]}
    assert list(db.leads.find(managed_query)) == []
    assert db.leads.find_one({"_id": lead_id})["pipeline_stage"] == "CONTACTED"


def test_sla_priority_uses_current_cycle_values_for_sort_order():
    rows = [
        {"name": "managed", "_has_assigned": 0, "_has_management": 0, "_overdue_minutes": 80},
        {"name": "slightly-overdue", "_has_assigned": 0, "_has_management": 1, "_overdue_minutes": 5},
        {"name": "nearly-due", "_has_assigned": 0, "_has_management": 1, "_overdue_minutes": -10},
        {"name": "unassigned", "_has_assigned": 1, "_has_management": 1, "_overdue_minutes": None},
    ]
    assert [row["name"] for row in sorted(rows, key=api_crm._crm_sla_sort_key)] == [
        "slightly-overdue", "nearly-due", "managed", "unassigned",
    ]
    assert api_crm._crm_sla_sort_key({
        "_admin_cycle_priority": 0, "_overdue_minutes": None,
    }) == (-1, 0)


def test_sla_start_wall_clock_is_rendered_in_chile_local_time():
    instant = datetime(2026, 10, 13, 15, 59, tzinfo=timezone.utc)
    local = api_crm._crm_sla_display_datetime(instant)

    assert local == CHILE_TZ.localize(datetime(2026, 10, 13, 12, 59))
    assert local.strftime("%d/%m/%Y %H:%M") == "13/10/2026 12:59"


def test_crm_active_cycle_lookup_has_separate_admin_identity_branch():
    stage = api_crm._canonical_active_cycle_lookup_stage()
    lookup = stage["$lookup"]
    match = lookup["pipeline"][0]["$match"]
    assert lookup["from"] == "crm_assignment_cycles"
    assert lookup["localField"] == "_crm_lead_join_ids"
    assert lookup["foreignField"] == "lead_id"
    assert match["$and"][1]["cycle_status"] == "active"
    assert match["$and"][1]["schema_version"] == "crm_assignment_cycle_v1"
    assert "$lead_current_cycle_id" in repr(match)
    assert "$$lead_owner_user_id" in repr(match)
    assert "$$lead_id" in repr(match)
    assert "notification_eligible" in repr(match)


def test_historical_lead_management_does_not_complete_new_cycle_sla():
    assigned = CHILE_TZ.localize(datetime(2026, 10, 13, 9, 0)).astimezone(timezone.utc)
    cycle = {
        "assignment_cycle_id": "admin-correction:HOLIDAY_SLA_CORRECTION_2026_10_12:6acce7e71df961b65ef4cdae:v1",
        "assigned_at": assigned,
        "sla_started_at": assigned,
        "sla_start_status": "started",
        "temperature_at_assignment": "NORMAL",
        "first_valid_management_at": None,
        "sla_first_management_status": "pending",
    }
    lead = {"lifecycle": {"first_valid_management_at": assigned - timedelta(days=1)}}
    result = calculate_cycle_sla(
        cycle=cycle, lead=lead,
        now=assigned + timedelta(minutes=30),
    )
    assert not result["fulfilled"]
    assert result["minutes"] == 30
    assert result["threshold_minutes"] == 180


def test_waiting_for_notification_has_no_sla_start_or_sort_priority():
    cycle = {
        "assigned_at": datetime(2026, 10, 12, 12, tzinfo=timezone.utc),
        "sla_start_status": "pending_notification",
        "temperature_at_assignment": "NORMAL",
    }
    assert api_crm._crm_sla_sort_key({
        "_has_assigned": 0, "_has_management": 1,
        "_sla_pending_notification": True, "_overdue_minutes": None,
    })[0] == 4
    expiration = canonical_expiration_recheck(
        cycle, {"lead_temperature_effective": "NORMAL"},
        now=datetime(2026, 10, 15, 12, tzinfo=timezone.utc),
    )
    assert expiration.start_source == "awaiting_owner_notification"
    assert expiration.started_at is None


def test_crm_list_version_bump_notifies_open_pollers():
    db = mongomock.MongoClient().db
    before = get_crm_leads_version(db)
    after = bump_crm_leads_version(db, reason="crm_admin_cycle_inbox_fix_deployed")
    assert after == before + 1
    assert get_crm_leads_version(db) == after
    assert db.crm_runtime_state.find_one({"_id": "crm_leads_list"})["reason"] == "crm_admin_cycle_inbox_fix_deployed"
