import asyncio
from copy import deepcopy
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from chatbot.constants import CHILE_TZ
from chatbot.crm_metrics import build_weekly_crm_snapshot
from chatbot.crm_weekly_report import (
    approve_and_send, assemble_message, fallback_narrative, official_idempotency_key,
    previous_complete_week, scheduler_tick, validate_snapshot,
)


class Result:
    def __init__(self, inserted_id="id"): self.inserted_id = inserted_id


class Cursor(list):
    def sort(self, *args): return self
    def limit(self, value): return Cursor(self[:value])


class Collection:
    def __init__(self, docs=None): self.docs = list(docs or [])
    def find(self, query=None, projection=None): return Cursor(deepcopy(self.docs))
    def find_one(self, query, projection=None, sort=None):
        for doc in self.docs:
            if all((v.get("$ne") != doc.get(k) if isinstance(v, dict) and "$ne" in v else doc.get(k) == v) for k, v in query.items()):
                return deepcopy(doc)
        return None
    def insert_one(self, doc): self.docs.append(deepcopy(doc)); return Result()
    def update_one(self, query, update):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()): doc.update(deepcopy(update.get("$set", {}))); break
    def create_index(self, *args, **kwargs): return "idx"


class DB(dict):
    def __missing__(self, key): self[key] = Collection(); return self[key]


def dt(day, hour=12): return CHILE_TZ.localize(datetime(2026, 7, day, hour)).astimezone(timezone.utc)


def event(lead, timestamp, *, actor="Susana", result="CONTACTADO"):
    return {"lead_id": lead, "timestamp": timestamp, "type": "CONTACT_RESULT", "actor": actor,
            "actor_type": "human", "result": result, "confirmed": True, "identity_status": "resolved", "meta": {}}


def fixture_db():
    leads = [
        {"_id": "new-1", "created_at": dt(20), "stage": "NEW", "lead_temperature_effective": "HOT",
         "temperature_history": [{"at": dt(24, 16), "value": "HOT"}]},
        {"_id": "new-2", "created_at": dt(21), "stage": "NEW", "lead_temperature_effective": "COLD",
         "temperature_history": [{"at": dt(24, 16), "value": "COLD"}]},
        {"_id": "old-1", "created_at": dt(10), "stage": "CONTACTED", "lead_temperature_effective": "COLD"},
    ]
    events = [event("new-1", dt(22)), event("new-1", dt(23), result="NO_RESPONDIO"), event("old-1", dt(23))]
    cycles = [
        {"_id": "c1", "assignment_cycle_id": "c1", "lead_id": "new-1", "assigned_to_user_id": "Susana", "assigned_at": dt(20, 9), "unassigned_at": None},
        {"_id": "c2", "assignment_cycle_id": "c2", "lead_id": "new-2", "assigned_to_user_id": "Mariela", "assigned_at": dt(21, 9), "unassigned_at": None},
    ]
    visits = [
        {"_id": "v1", "lead_id": "old-1", "created_at": dt(23)},
        {"_id": "v2", "lead_id": "old-1", "created_at": dt(24)},
    ]
    return DB(leads=Collection(leads), crm_events=Collection(events), crm_assignment_cycles=Collection(cycles), visitas=Collection(visits))


def build_fixture():
    snapshot = build_weekly_crm_snapshot(
        fixture_db(), period_start=date(2026, 7, 20), period_end=date(2026, 7, 24),
        priority_as_of=CHILE_TZ.localize(datetime(2026, 7, 27, 8, 15)), executive_order=["Susana", "Mariela"],
    )
    snapshot["operational_focus"] = {"key": "unmanaged_cohort", "supporting_metrics": {"unmanaged_at_cutoff_unique": 1}}
    validate_snapshot(snapshot)
    return snapshot


def test_closed_monday_to_friday_cohort_and_partition():
    snapshot = build_fixture()
    assert snapshot["cohort"]["received_unique"] == 2
    assert snapshot["cohort"]["managed_unique"] == 1
    assert snapshot["cohort"]["unmanaged_at_cutoff_unique"] == 1


def test_multiple_actions_count_one_and_pipeline_is_separate_from_cohort():
    snapshot = build_fixture()
    assert snapshot["cohort"]["managed_unique"] == 1
    assert snapshot["pipeline_activity"]["leads_with_effective_contact_unique"] == 2


def test_old_lead_visit_is_pipeline_only_and_unique_differs_from_events():
    snapshot = build_fixture()
    assert snapshot["pipeline_activity"]["leads_with_visit_unique"] == 1
    assert snapshot["pipeline_activity"]["visit_events_total"] == 2
    assert snapshot["cohort"]["received_unique"] == 2


def test_priority_has_monday_as_of_and_readable_sla_age():
    snapshot = build_fixture()
    assert snapshot["monday_priorities"]["priority_as_of"] == "2026-07-27T08:15:00-04:00"
    assert "h\u00e1bil" in snapshot["monday_priorities"]["oldest_pending_display"]


def test_temperature_null_is_omitted_from_message():
    snapshot = build_fixture(); snapshot["cohort"]["hot_pending_at_cutoff_unique"] = None
    snapshot["data_quality"]["temperature_publishable"] = False
    message = assemble_message(snapshot, fallback_narrative(snapshot))
    assert "Hot pendientes" not in message


def test_sla_before_cutover_is_not_published():
    db = fixture_db(); db["crm_assignment_cycles"].docs[0]["assigned_at"] = dt(19, 9)
    snapshot = build_weekly_crm_snapshot(db, period_start=date(2026, 7, 20), period_end=date(2026, 7, 24),
                                         priority_as_of=CHILE_TZ.localize(datetime(2026, 7, 27, 8, 15)), executive_order=["Susana", "Mariela"])
    assert snapshot["monday_priorities"]["sla_overdue_publishable_unique"] == 1


def test_reassignment_dimensions_do_not_replace_management_actor():
    snapshot = build_fixture()
    susana, mariela = snapshot["executives"]
    assert susana["managed_unique"] == 2
    assert mariela["managed_unique"] == 0
    assert [row["name"] for row in snapshot["executives"]] == ["Susana", "Mariela"]


def test_deepseek_fallback_only_controls_focus_and_closing():
    snapshot = build_fixture(); narrative = fallback_narrative(snapshot)
    assert set(narrative) == {"focus", "closing"}
    message = assemble_message(snapshot, narrative)
    assert str(snapshot["cohort"]["received_unique"]) in message


def test_validation_blocks_invalid_group_and_long_message():
    snapshot = build_fixture()
    with pytest.raises(ValueError): validate_snapshot(snapshot, message="x" * 1301)
    with pytest.raises(ValueError): validate_snapshot(snapshot, message="ok", group_id="+56912345678", official=True)
    with pytest.raises(ValueError): validate_snapshot(snapshot, message="cliente@ejemplo.cl")


def test_previous_complete_week_is_20_to_24():
    start, end = previous_complete_week(CHILE_TZ.localize(datetime(2026, 7, 27, 8, 15)))
    assert (start, end) == (date(2026, 7, 20), date(2026, 7, 24))


def test_scheduler_requires_approval_and_never_sends(monkeypatch):
    db = fixture_db(); create = AsyncMock(return_value={"report_id": "r1", "status": "pending_approval"})
    with patch("chatbot.crm_weekly_report.create_preview", create):
        result = asyncio.run(scheduler_tick(CHILE_TZ.localize(datetime(2026, 7, 27, 8, 15)), db=db))
    assert result["status"] == "pending_approval"
    create.assert_awaited_once()


def test_official_idempotency_and_no_real_whatsapp(monkeypatch):
    snapshot = build_fixture(); message = assemble_message(snapshot, fallback_narrative(snapshot))
    report = {"report_id": "r1", "status": "pending_approval", "period_start": "2026-07-20", "period_end": "2026-07-24",
              "snapshot": snapshot, "generated_text": message}
    db = fixture_db(); db["crm_weekly_reports"] = Collection([report]); db["crm_weekly_deliveries"] = Collection()
    sender = AsyncMock(return_value={"success": True, "delivery_status": "delivered", "provider_message_id": "fake"})
    with patch("chatbot.crm_weekly_report.Config.CRM_WEEKLY_REPORT_GROUP_ID", "12345@g.us"):
        first = asyncio.run(approve_and_send("r1", "Admin", db=db, sender=sender))
        second = asyncio.run(approve_and_send("r1", "Admin", db=db, sender=sender))
    assert sender.await_count == 1
    assert first["idempotency_key"] == official_idempotency_key(report, "12345@g.us")
    assert second["status"] == "sent"
