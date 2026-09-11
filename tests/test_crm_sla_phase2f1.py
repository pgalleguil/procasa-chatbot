"""Phase 2F.1 read-path, bounded-query and semantic-audit tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from chatbot.crm_sla_performance_snapshot import build_live_capacity_snapshot
from chatbot.crm_sla_snapshot_queries import ReadInstrumentation, query_pattern_summary
from scripts.audit_crm_sla_read_path_phase2f1 import _compare_snapshots, _selection_signature


UTC = timezone.utc
NOW = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def limit(self, _value):
        return self

    def sort(self, _value):
        return self

    async def to_list(self, length=None):
        return list(self.rows[:length] if length else self.rows)


class Collection:
    def __init__(self, name, rows):
        self.name = name
        self.rows = list(rows)
        self.calls = []

    def find(self, query, projection=None):
        self.calls.append((dict(query), dict(projection or {})))
        return Cursor(self.rows)


class DB:
    def __init__(self, mapping):
        self.mapping = mapping

    def __getitem__(self, key):
        return self.mapping[key]


def _db():
    return DB({
        "crm_assignment_cycles": Collection("crm_assignment_cycles", [{
            "lead_id": "l1", "assignment_cycle_id": "c1", "assigned_to_user_id": "a",
            "assigned_at": NOW - timedelta(hours=4), "temperature_at_assignment": "NORMAL",
            "cycle_status": "active", "unassigned_at": None,
        }]),
        "leads": Collection("leads", [{
            "_id": "l1", "pipeline_stage": "NEW", "lead_temperature_effective": "NORMAL", "lifecycle": {},
        }]),
        "crm_management_results": Collection("crm_management_results", []),
        "crm_events": Collection("crm_events", []),
    })


def test_live_snapshot_has_fixed_set_based_query_budget_and_no_n_plus_one():
    instrumentation = ReadInstrumentation(measure_bson_bytes=True)
    result = asyncio.run(build_live_capacity_snapshot(
        _db(), as_of=NOW, policy_since=NOW - timedelta(days=1), instrumentation=instrumentation,
    ))
    summary = instrumentation.summary()
    capacity = result["capacity_metrics"]["a"]
    assert 3 * capacity["expired"] + 2 * capacity["unmanaged"] + capacity["open"] == 6
    assert summary["mongo_round_trips_total"] == 4
    assert summary["by_collection"] == {
        "crm_assignment_cycles": 1,
        "crm_events": 1,
        "crm_management_results": 1,
        "leads": 1,
    }
    assert summary["all_projections_pii_free"] is True
    assert summary["bson_bytes_estimate"] is not None
    patterns = query_pattern_summary(instrumentation)
    assert patterns
    assert all(row["n_plus_one"] == "NO" for row in patterns)


def test_instrumentation_marks_sensitive_projection_without_reading_payload():
    instrumentation = ReadInstrumentation()
    instrumentation.record_find(
        collection="leads", query_name="test", documents=1, wall_ms=1.0,
        projection={"_id": 1, "phone": 1}, cursor_batches_observed=1,
    )
    summary = instrumentation.summary()
    assert summary["all_projections_pii_free"] is False
    assert summary["calls"][0]["projection_contains_pii"] is True


def test_snapshot_comparator_proves_capacity_and_historical_float_equivalence():
    candidate = {
        "user_id": "a", "open_current_policy": 3, "unmanaged_current_policy": 2,
        "expired_current_policy": 1, "sample_size": 10, "sla_compliance_rate": 0.5,
        "attention_rate": 0.8, "p50_first_management_business_minutes": 12.0,
        "p90_first_management_business_minutes": 42.0, "sla_compliance_rate_adjusted": 0.4,
        "attention_rate_adjusted": 0.7,
    }
    old = {"candidates": [candidate], "metrics": {"a": {
        "sample_size": 10, "sla_compliance_rate": 0.5, "attention_rate": 0.8,
        "p50": 12.0, "p90": 42.0, "sla_compliance_rate_adjusted": 0.4,
        "attention_rate_adjusted": 0.7,
    }}}
    optimized = {"candidates": [dict(candidate)], "metrics": {"a": {
        "sample_size": 10, "sla_compliance_rate": 0.5000000001, "attention_rate": 0.8,
        "p50": 12.0, "p90": 42.0, "sla_compliance_rate_adjusted": 0.4,
        "attention_rate_adjusted": 0.7,
    }}}
    rows = _compare_snapshots(old, optimized)
    assert rows
    assert all(row["equivalent"] == "PASS" for row in rows)


def test_selection_signature_includes_candidate_pool_winner_second_score_and_frozen_branches():
    result = {"evaluations": [{
        "lead_id": "l1", "source_cycle_id": "c1", "branch": "RM_GLOBAL_RESCUE",
        "exclusion_reason": "", "candidate_ids": ["a", "b"], "selected_user_id": "a",
        "second_user_id": "b", "selected_score": 80.0, "guardrail": "R2_ROLLING_SHARE_LIMIT",
        "would_execute": False,
    }]}
    signature = _selection_signature(result)
    assert signature["l1|c1"] == (
        "RM_GLOBAL_RESCUE", "", ("a", "b"), "a", "b", 80.0,
        "R2_ROLLING_SHARE_LIMIT", False,
    )


def test_projection_does_not_include_pii_fields_in_live_path():
    db = _db()
    instrumentation = ReadInstrumentation()
    asyncio.run(build_live_capacity_snapshot(
        db, as_of=NOW, policy_since=NOW - timedelta(days=1), instrumentation=instrumentation,
    ))
    for call in instrumentation.summary()["calls"]:
        assert not any(field.split(".")[-1].lower() in {"phone", "telefono", "email", "message", "messages", "body", "content"} for field in call["projection_fields"])
