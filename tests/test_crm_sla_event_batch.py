"""SLA evaluation must batch event reads without losing cycle-specific evidence."""
from datetime import datetime, timedelta, timezone

import pytest

from chatbot.crm_sla_alert_evaluator import _load_sla_events_batched


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return self.rows[:length]


class _Events:
    def __init__(self, events):
        self.events = events
        self.queries = []

    def find(self, query, projection=None):
        self.queries.append(query)
        rows = []
        for event in self.events:
            if "$in" in (query.get("lead_id") or {}):
                if event.get("lead_id") not in query["lead_id"]["$in"]:
                    continue
            elif event.get("lead_id") != query.get("lead_id"):
                continue
            if not isinstance(event.get("timestamp"), datetime):
                continue
            if event["timestamp"] < query["timestamp"]["$gte"]:
                continue
            if "$or" in query:
                if event.get("assignment_cycle_id") not in (
                    None, query["$or"][0]["assignment_cycle_id"],
                ):
                    continue
            rows.append(event)
        return _Cursor(rows)


def _cycle(lead, cid, at):
    return {"lead_id": lead, "assignment_cycle_id": cid, "assigned_at": at}


@pytest.mark.asyncio
async def test_one_batch_preserves_management_evidence_by_exact_cycle():
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    c1 = _cycle("a", "c1", now)
    c2 = _cycle("a", "c2", now + timedelta(minutes=15))
    c3 = _cycle("b", "c3", now)
    e1 = {"lead_id": "a", "timestamp": now + timedelta(minutes=1), "assignment_cycle_id": "c1", "type": "HUMAN_NOTE"}
    e2 = {"lead_id": "a", "timestamp": now + timedelta(minutes=16), "assignment_cycle_id": "c2", "type": "HUMAN_NOTE"}
    e3 = {"lead_id": "a", "timestamp": now + timedelta(minutes=20), "type": "GESTION_LOG"}
    e4 = {"lead_id": "b", "timestamp": now + timedelta(minutes=3), "assignment_cycle_id": "c3", "type": "CLICK_WHATSAPP_LEAD"}
    old = {"lead_id": "a", "timestamp": now - timedelta(minutes=1), "assignment_cycle_id": "c1"}
    string = {"lead_id": "a", "timestamp": "2026-10-21T12:45:00Z", "type": "HUMAN_NOTE"}
    collection = _Events([e1, e2, e3, e4, old, string])
    result = await _load_sla_events_batched({"crm_events": collection}, [c1, c2, c3], limit_events=500)
    assert result[id(c1)] == [e1, e3]
    assert result[id(c2)] == [e2, e3]
    assert result[id(c3)] == [e4]
    assert len(collection.queries) == 1
    assert set(collection.queries[0]["lead_id"]["$in"]) == {"a", "b"}


@pytest.mark.asyncio
async def test_many_cycles_use_bounded_batches_not_n_per_lead_queries():
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    cycles = [_cycle(f"lead-{i}", f"cycle-{i}", now) for i in range(130)]
    collection = _Events([])
    result = await _load_sla_events_batched({"crm_events": collection}, cycles, limit_events=500)
    assert len(result) == 130
    assert len(collection.queries) == 3
    assert max(len(q["lead_id"]["$in"]) for q in collection.queries) <= 64


@pytest.mark.asyncio
async def test_crowded_cycle_falls_back_to_original_capped_query():
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    cycle = _cycle("lead", "cycle", now)
    rows = [
        {"lead_id": "lead", "timestamp": now + timedelta(seconds=i),
         "assignment_cycle_id": "cycle", "type": "HUMAN_NOTE"}
        for i in range(4)
    ]
    collection = _Events(rows)
    result = await _load_sla_events_batched({"crm_events": collection}, [cycle], limit_events=3)
    assert result[id(cycle)] == rows[:3]
    assert len(collection.queries) == 2
    assert collection.queries[-1]["$or"][0] == {"assignment_cycle_id": "cycle"}


@pytest.mark.asyncio
async def test_query_failures_are_not_misclassified_as_missing_management():
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)

    class _FailingEvents:
        def find(self, query, projection=None):
            raise TimeoutError("MongoDB unavailable")

    with pytest.raises(TimeoutError):
        await _load_sla_events_batched(
            {"crm_events": _FailingEvents()},
            [_cycle("lead", "cycle", now)],
            limit_events=500,
        )
