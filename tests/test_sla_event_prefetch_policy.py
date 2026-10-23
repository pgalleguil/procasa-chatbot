"""SLA event I/O optimization must preserve every alert and exclusion."""
from datetime import datetime, timedelta, timezone

import pytest

from chatbot import crm_sla_alert_evaluator as evaluator


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return self.rows[:length]


class Collection:
    def __init__(self, docs=()):
        self.docs = list(docs)
        self.queries = []

    def find(self, query, projection=None):
        self.queries.append(query)
        results = self.docs
        for key, expected in query.items():
            if key.startswith("$"):
                raise AssertionError(f"Unexpected query operator {key}")
            if isinstance(expected, dict):
                if "$in" in expected:
                    results = [d for d in results if d.get(key) in expected["$in"]]
                elif "$gte" in expected:
                    results = [d for d in results if d.get(key) is not None
                               and d[key] >= expected["$gte"]]
                elif "$exists" in expected:
                    results = [d for d in results if (key in d) == expected["$exists"]]
                else:
                    raise AssertionError(f"Unexpected condition {expected!r}")
            else:
                results = [d for d in results if d.get(key) == expected]
        return Cursor(results)


class DB(dict):
    def __missing__(self, key):
        value = Collection()
        self[key] = value
        return value


CUTOVER = datetime(2026, 10, 22, 13, tzinfo=timezone.utc)
ASSIGNED = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
NOW = ASSIGNED + timedelta(hours=5)


def cycle(name, **fields):
    return {
        "lead_id": name, "assignment_cycle_id": f"cycle-{name}",
        "assigned_to_user_id": "agent-1", "assigned_at": ASSIGNED,
        "cycle_status": "active", "unassigned_at": None,
        "reason": "lead_created", **fields,
    }


def lead(name):
    return {
        "_id": name, "lead_temperature_effective": "NORMAL",
        "pipeline_stage": "NEW", "phone": "56912345678",
        "ejecutivo_asignado": "Ejecutivo", "prospecto": {
            "nombre": name, "codigo": "P001",
        },
    }


@pytest.mark.parametrize("field,marker", [
    ("first_valid_management_at", ASSIGNED - timedelta(days=2)),
    ("sla_first_management_status", "completed"),
])
def test_verified_canonical_management_never_needs_event_reads(field, marker):
    row = cycle("already-managed", **{field: marker})
    assert not evaluator._needs_sla_event_evidence(
        row, alert_cutover=CUTOVER, mongo_cutover=CUTOVER
    )
    assert evaluator.is_first_sla_management_completed(row)


def test_outside_cutover_skips_events_without_suppressing_exclusion_reporting():
    row = cycle("old", sla_started_at=CUTOVER - timedelta(minutes=1))
    assert not evaluator._needs_sla_event_evidence(
        row, alert_cutover=CUTOVER, mongo_cutover=CUTOVER
    )


@pytest.mark.parametrize("raw", [None, "", "invalid"])
def test_unknown_date_and_unmanaged_cycle_keep_original_event_reads(raw):
    row = cycle("unmanaged", sla_started_at=raw)
    assert evaluator._needs_sla_event_evidence(
        row, alert_cutover=CUTOVER, mongo_cutover=CUTOVER
    )


@pytest.mark.asyncio
async def test_full_alert_report_same_when_event_prefetch_is_reduced(monkeypatch):
    marked = cycle("managed", first_valid_management_at=ASSIGNED + timedelta(minutes=2))
    old = cycle("old", sla_started_at=CUTOVER - timedelta(minutes=1))
    event_managed = cycle("event-managed")
    eligible = cycle("eligible")
    all_cycles = [marked, old, event_managed, eligible]
    db = DB({
        "crm_assignment_cycles": Collection(all_cycles),
        "leads": Collection([lead(row["lead_id"]) for row in all_cycles]),
        "crm_events": Collection([{
            "lead_id": "event-managed",
            "timestamp": ASSIGNED + timedelta(minutes=4),
            "assignment_cycle_id": "cycle-event-managed",
            "type": "GESTION_LOG",
            "actor": "agent-1",
            "actor_type": "human",
        }]),
        "usuarios": Collection([{
            "_id": "agent-1", "nombre": "Ejecutivo", "is_active": True,
            "rol": "agente", "telefono": "+56911111111",
        }]),
        "crm_management_results": Collection(),
        "crm_notifications_v1": Collection(),
    })
    original = evaluator._load_sla_events_batched
    observed = []

    async def full_prefetch(db_arg, selected, *, limit_events):
        observed.append(("baseline", len(selected)))
        return await original(db_arg, all_cycles, limit_events=limit_events)

    monkeypatch.setattr(evaluator, "_load_sla_events_batched", full_prefetch)
    baseline = await evaluator.evaluate_sla_alerts(
        db=db, now=NOW, alert_cutover=CUTOVER, limit_cycles=100,
    )
    baseline_queries = db["crm_events"].queries[:]
    db["crm_events"].queries.clear()

    async def reduced_prefetch(db_arg, selected, *, limit_events):
        observed.append(("optimized", len(selected)))
        return await original(db_arg, selected, limit_events=limit_events)

    monkeypatch.setattr(evaluator, "_load_sla_events_batched", reduced_prefetch)
    optimized = await evaluator.evaluate_sla_alerts(
        db=db, now=NOW, alert_cutover=CUTOVER, limit_cycles=100,
    )
    assert optimized == baseline
    assert observed == [("baseline", 2), ("optimized", 2)]
    # Baseline emulates the former complete fetch of all four cycles.
    assert len(baseline_queries[0]["lead_id"]["$in"]) == 4
    assert set(db["crm_events"].queries[0]["lead_id"]["$in"]) == {
        "event-managed", "eligible",
    }
    assert optimized["excluded_by_reason"]["before_alert_cutover"] == 1
    assert optimized["excluded_by_reason"]["has_valid_management"] == 2
    assert optimized["included"] == 1
    assert optimized["alerts"][0]["lead_id"] == "eligible"


@pytest.mark.asyncio
async def test_many_completed_cycles_reduce_bulk_queries_without_touching_fresh():
    rows = [cycle(f"managed-{i}", sla_first_management_status="completed")
            for i in range(130)]
    fresh = cycle("fresh")
    rows.append(fresh)
    selected = [c for c in rows if evaluator._needs_sla_event_evidence(
        c, alert_cutover=CUTOVER, mongo_cutover=CUTOVER,
    )]
    assert selected == [fresh]
    events = Collection()
    before = await evaluator._load_sla_events_batched(
        {"crm_events": events}, rows, limit_events=500,
    )
    assert len(before) == 131
    assert len(events.queries) == 3
    events.queries.clear()
    after = await evaluator._load_sla_events_batched(
        {"crm_events": events}, selected, limit_events=500,
    )
    assert len(after) == 1
    assert after[id(fresh)] == before[id(fresh)]
    assert len(events.queries) == 1
