"""Phase 2F.2 tests for server-side live-capacity variants."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from chatbot.crm_sla_live_capacity import (
    A1_NAME,
    A2_NAME,
    _base_pipeline,
    _capacity_from_rows,
    _event_pipeline,
)
from chatbot.crm_sla_snapshot_queries import ReadInstrumentation, aggregate_many


UTC = timezone.utc
NOW = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    async def to_list(self, length=None):
        return list(self.rows[:length] if length else self.rows)


class AggregateCollection:
    name = "crm_assignment_cycles"

    def __init__(self, rows):
        self.rows = list(rows)
        self.pipelines = []

    def aggregate(self, pipeline, **_options):
        self.pipelines.append(pipeline)
        return Cursor(self.rows)


def _cycle(*, owner="owner-a", lead_id="lead-1", cycle_id="cycle-1", temperature="NORMAL"):
    return {
        "lead_id": lead_id,
        "assignment_cycle_id": cycle_id,
        "assigned_to_user_id": owner,
        "assigned_at": NOW - timedelta(hours=4),
        "sla_started_at": NOW - timedelta(hours=4),
        "temperature_at_assignment": temperature,
        "lead_found": True,
        "lead": {"pipeline_stage": "NEW", "lead_temperature_effective": temperature, "lifecycle": {}},
        "management_results": [],
    }


def test_a1_and_a2_pipelines_have_one_cycle_aggregation_and_no_pii_projection():
    a1 = _base_pipeline(NOW - timedelta(days=1), grouped=False)
    a2 = _base_pipeline(NOW - timedelta(days=1), grouped=True)
    assert any(stage.get("$lookup", {}).get("from") == "leads" for stage in a1)
    assert any(stage.get("$lookup", {}).get("from") == "crm_management_results" for stage in a1)
    assert any(stage.get("$group", {}).get("cycles") for stage in a2)
    pipelines = a1 + a2 + _event_pipeline(["lead-1"], since=NOW - timedelta(days=1), until=NOW)
    projected_fields = []
    for stage in pipelines:
        projection = stage.get("$project") if isinstance(stage, dict) else None
        if isinstance(projection, dict):
            projected_fields.extend(str(key).lower() for key, value in projection.items() if value)
    assert not any(field.split(".")[-1] in {"phone", "email", "message", "messages", "body", "content"} for field in projected_fields)


def test_aggregate_many_is_one_logical_read_and_records_aggregate_operation():
    instrumentation = ReadInstrumentation(measure_bson_bytes=True)
    collection = AggregateCollection([{"value": 1}])
    result = asyncio.run(aggregate_many(collection, [{"$project": {"_id": 0, "value": 1}}], instrumentation=instrumentation))
    assert result == [{"value": 1}]
    summary = instrumentation.summary()
    assert summary["mongo_round_trips_total"] == 1
    assert summary["by_operation"] == {"aggregate": 1}
    assert summary["all_projections_pii_free"] is True


def test_a1_and_a2_capacity_semantics_match_for_open_unmanaged_expired_and_pressure():
    rows = [_cycle(), _cycle(owner="owner-b", lead_id="lead-2", cycle_id="cycle-2", temperature="HOT")]
    a1_capacity, a1_audit = _capacity_from_rows(rows, [], as_of=NOW, policy_since=NOW - timedelta(days=1))
    a2_capacity, a2_audit = _capacity_from_rows(rows, [], as_of=NOW, policy_since=NOW - timedelta(days=1))
    assert a1_capacity == a2_capacity
    assert a1_audit["current_policy_cycles"] == a2_audit["current_policy_cycles"] == 2
    for capacity in (a1_capacity, a2_capacity):
        for values in capacity.values():
            assert values["open"] == 1
            assert values["unmanaged"] == 1
            assert values["expired"] == 1
            assert 3 * values["expired"] + 2 * values["unmanaged"] + values["open"] == 6


def test_management_result_and_human_event_flags_are_not_mixed():
    rows = [_cycle()]
    rows[0]["management_results"] = [{"result_type": "MESSAGE_SENT_WAITING_RESPONSE"}]
    automatic_event = [{
        "lead_id": "lead-1", "type": "GESTION_LOG", "actor_human": False,
        "confirmed_effective": True, "result_effective": "CONTACTADO",
    }]
    capacity, audit = _capacity_from_rows(rows, automatic_event, as_of=NOW, policy_since=NOW - timedelta(days=1))
    assert capacity["owner-a"] == {"open": 1, "unmanaged": 1, "expired": 1}
    assert audit["management_stop_cycles"] == 0
    assert audit["human_event_stop_cycles"] == 0


def test_valid_management_result_stops_unmanaged_but_does_not_change_open():
    rows = [_cycle()]
    rows[0]["management_results"] = [{"result_type": "EFFECTIVE_CONTACT"}]
    capacity, audit = _capacity_from_rows(rows, [], as_of=NOW, policy_since=NOW - timedelta(days=1))
    assert capacity["owner-a"] == {"open": 1, "unmanaged": 0, "expired": 0}
    assert audit["management_stop_cycles"] == 1


def test_variant_names_are_the_only_two_allowed_variants():
    assert {A1_NAME, A2_NAME} == {
        "A1_AGGREGATION_PER_CYCLE_MINIMAL", "A2_AGGREGATION_GROUPED",
    }
