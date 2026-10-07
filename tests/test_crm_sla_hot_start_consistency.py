from datetime import datetime, timezone
from bson import ObjectId

import mongomock

from chatbot.constants import CHILE_TZ
from chatbot.crm_metrics import (
    calculate_cycle_sla,
    coerce_utc_datetime,
    is_first_sla_management_completed,
    resolve_cycle_hot_start,
)
from chatbot.crm_sla_alert_evaluator import (
    ALERT_LEVEL_BREACHED,
    ALERT_LEVEL_NEAR_CRITICAL,
    ALERT_LEVEL_WARNING,
    alert_level_for_sla_status,
    add_business_minutes,
)
from chatbot.crm_sla_reassignment_worker import canonical_expiration_recheck
from chatbot.crm_sla_dry_run import evaluate_sla_alert_dry_run
from chatbot.crm_sla_reassignment_cutover import canonical_sla_breached_at


UTC = timezone.utc


def local_dt(day, hour, minute, second=0, *, month=10):
    return CHILE_TZ.localize(datetime(2026, month, day, hour, minute, second)).astimezone(UTC)


def cycle(*, assigned, hot_start, temperature="HOT", management=None, **extra):
    row = {
        "lead_id": "lead-1",
        "assignment_cycle_id": "cycle-1",
        "assigned_at": assigned,
        "sla_started_at": assigned,
        "temperature_at_assignment": temperature,
        "temperature_on_assignment": "NORMAL",
        "cycle_status": "active",
        "unassigned_at": None,
    }
    if hot_start is not None:
        row["hot_started_at"] = hot_start
        row["temperature_transitioned_at"] = hot_start
    if management is not None:
        row["first_valid_management_at"] = management
    row.update(extra)
    return row


def test_normal_to_hot_uses_transition_start_for_30_45_60_minute_policy():
    assigned = local_dt(7, 10, 0)
    hot_start = local_dt(7, 12, 0)
    row = cycle(assigned=assigned, hot_start=hot_start)
    lead = {"lead_temperature_effective": "HOT"}

    at_20 = calculate_cycle_sla(cycle=row, lead=lead, now=local_dt(7, 12, 20))
    assert at_20["hot_minutes"] == 20
    expiration_20 = canonical_expiration_recheck(row, lead, now=local_dt(7, 12, 20))
    assert at_20["hot_start"] == expiration_20.hot_started_at == hot_start
    assert at_20["deadline_at"] == expiration_20.deadline_at
    assert at_20["status"] == expiration_20.sla_status == "good"
    assert alert_level_for_sla_status(at_20["status"], temperature="HOT") is None
    assert expiration_20.expired is False

    at_46 = calculate_cycle_sla(cycle=row, lead=lead, now=local_dt(7, 12, 46))
    assert at_46["hot_minutes"] == 46
    expiration_46 = canonical_expiration_recheck(row, lead, now=local_dt(7, 12, 46))
    assert at_46["deadline_at"] == expiration_46.deadline_at
    assert at_46["status"] == expiration_46.sla_status == "near_critical"
    assert alert_level_for_sla_status(at_46["status"], temperature="HOT") == ALERT_LEVEL_NEAR_CRITICAL
    assert expiration_46.expired is False

    at_61 = calculate_cycle_sla(cycle=row, lead=lead, now=local_dt(7, 13, 1))
    assert at_61["hot_minutes"] == 61
    expiration_61 = canonical_expiration_recheck(row, lead, now=local_dt(7, 13, 1))
    assert at_61["deadline_at"] == expiration_61.deadline_at
    assert at_61["status"] == expiration_61.sla_status == "critical"
    assert alert_level_for_sla_status(at_61["status"], temperature="HOT") == ALERT_LEVEL_BREACHED
    assert expiration_61.expired is True

    assert expiration_20.deadline_at == add_business_minutes(hot_start, 60)


def test_hot_at_assignment_starts_at_sla_start():
    assigned = local_dt(7, 10, 0)
    row = cycle(
        assigned=assigned, hot_start=None, temperature="HOT",
        temperature_on_assignment="HOT",
    )
    assert resolve_cycle_hot_start(cycle=row) == assigned
    result = calculate_cycle_sla(cycle=row, lead={"lead_temperature_effective": "HOT"}, now=local_dt(7, 10, 31))
    assert result["hot_minutes"] == 31
    assert result["status"] == "warning"
    assert alert_level_for_sla_status(result["status"], temperature="HOT") == ALERT_LEVEL_WARNING


def test_missing_persisted_sla_start_uses_same_commercial_fallback_everywhere():
    assigned = local_dt(10, 12, 0)  # Saturday; existing rule starts Monday 09:00.
    expected_start = local_dt(12, 9, 0)
    row = cycle(
        assigned=assigned, hot_start=None, temperature="HOT",
        temperature_on_assignment="HOT",
    )
    row.pop("sla_started_at")
    lead = {"lead_temperature_effective": "HOT"}

    result = calculate_cycle_sla(cycle=row, lead=lead, now=local_dt(12, 10, 1))
    expiration = canonical_expiration_recheck(row, lead, now=local_dt(12, 10, 1))
    cutover_deadline = canonical_sla_breached_at(row, lead=lead)

    assert result["hot_start"] == expected_start
    assert result["deadline_at"] == expiration.deadline_at == cutover_deadline
    assert result["status"] == expiration.sla_status == "critical"


def test_analy_exact_production_times_are_managed_within_hot_sla():
    assigned = local_dt(7, 10, 57, 57)
    hot_start = local_dt(7, 12, 15, 1)
    managed = local_dt(7, 12, 22, 56)
    row = cycle(
        assigned=assigned, hot_start=hot_start, management=managed,
        temperature_on_assignment="NORMAL",
        sla_first_management_status="completed",
    )
    lead = {"lead_temperature_effective": "HOT"}
    result = calculate_cycle_sla(cycle=row, lead=lead, now=local_dt(7, 13, 30))
    elapsed_to_management = result["hot_minutes"]
    assert 7 <= elapsed_to_management < 9
    assert result["canonical_state"] == "MANAGED_WITHIN_SLA"
    assert alert_level_for_sla_status(result["status"], temperature="HOT") is None
    assert canonical_expiration_recheck(row, lead, now=local_dt(7, 13, 30)).expired is False


def test_luis_normal_control_remains_managed_within_180_minutes():
    assigned = local_dt(7, 11, 0, 8)
    managed = local_dt(7, 13, 34, 45)
    row = cycle(
        assigned=assigned, hot_start=None, temperature="NORMAL",
        temperature_on_assignment="NORMAL", management=managed,
    )
    lead = {"lead_temperature_effective": "NORMAL"}
    result = calculate_cycle_sla(cycle=row, lead=lead, now=local_dt(7, 14, 0))
    assert result["canonical_state"] == "MANAGED_WITHIN_SLA"
    assert 153 <= result["minutes"] <= 155

    before_management = calculate_cycle_sla(
        cycle={**row, "first_valid_management_at": None}, lead=lead,
        now=local_dt(7, 13, 33),
    )
    assert before_management["status"] == "near_critical"
    assert alert_level_for_sla_status(before_management["status"], temperature="NORMAL") == ALERT_LEVEL_WARNING


def test_completed_cycle_marker_blocks_alert_and_reassignment_without_timestamp():
    row = cycle(
        assigned=local_dt(7, 9, 0), hot_start=None, temperature="HOT",
        sla_first_management_status="completed",
    )
    lead = {"lead_temperature_effective": "HOT"}
    assert is_first_sla_management_completed(row)
    result = calculate_cycle_sla(cycle=row, lead=lead, now=local_dt(7, 14, 0))
    assert result["fulfilled"] is True
    assert result["canonical_state"] == "MANAGED_TIME_UNKNOWN"
    assert alert_level_for_sla_status(result["status"], temperature="HOT") is None
    assert canonical_expiration_recheck(row, lead, now=local_dt(7, 14, 0)).expired is False


def test_hot_start_discrepancy_is_visible_but_cycle_value_wins():
    hot_start = local_dt(7, 12, 15, 1)
    row = cycle(assigned=local_dt(7, 10, 57, 57), hot_start=hot_start)
    lead = {"lead_temperature_effective": "HOT", "lifecycle": {"hot_since": local_dt(7, 10, 57, 57)}}
    assert resolve_cycle_hot_start(cycle=row, lead=lead) == hot_start
    deadline = canonical_expiration_recheck(row, lead, now=local_dt(7, 12, 20)).deadline_at
    assert deadline == add_business_minutes(hot_start, 60)


def test_crm_alert_and_reassignment_surfaces_publish_shared_cycle_deadline():
    from pathlib import Path

    api = Path("api_crm.py").read_text(encoding="utf-8")
    alerts = Path("chatbot/crm_sla_alert_evaluator.py").read_text(encoding="utf-8")
    reassignment = Path("chatbot/crm_sla_reassignment_worker.py").read_text(encoding="utf-8")
    assert "canonical_sla[\"deadline_at\"]" in api
    assert "canonical_sla.get(\"hot_start\")" in api
    assert "sla.get(\"deadline_at\")" in alerts
    assert "result.get(\"deadline_at\")" in reassignment


def test_read_only_dry_run_uses_same_hot_segment_and_45_minute_near_critical():
    assigned = local_dt(7, 10, 0)
    hot_start = local_dt(7, 12, 0)
    row = cycle(assigned=assigned, hot_start=hot_start)
    row["assigned_to_user_id"] = "agent-1"
    lead = {"_id": "lead-1", "lead_temperature_effective": "HOT"}
    agent = {"_id": "agent-1", "active": True, "telefono": "+56911111111"}

    before = evaluate_sla_alert_dry_run(
        leads=[lead], cycles=[row], users=[agent], as_of=local_dt(7, 12, 20),
        activation_at=assigned,
    )
    assert before["alerts"] == []

    near_critical = evaluate_sla_alert_dry_run(
        leads=[lead], cycles=[row], users=[agent], as_of=local_dt(7, 12, 46),
        activation_at=assigned,
    )
    assert near_critical["alerts"][0]["message_type"] == "hot_near_critical"
    assert near_critical["alerts"][0]["business_minutes"] == 46
    assert near_critical["alerts"][0]["threshold_business_minutes"] == 45


def test_hot_transition_persists_segment_once_and_never_moves_start_backwards():
    db = mongomock.MongoClient().sla_hot_transition
    assigned = local_dt(7, 10, 0)
    hot_start = local_dt(7, 12, 0)
    db.crm_assignment_cycles.insert_one({
        "lead_id": "lead-1", "assignment_cycle_id": "cycle-1",
        "assigned_at": assigned, "sla_started_at": assigned,
        "temperature_at_assignment": "NORMAL", "temperature_on_assignment": "NORMAL",
        "cycle_status": "active", "unassigned_at": None,
        "sla_segments": [{"policy": "NON_HOT", "segment_start": assigned, "segment_end": None}],
    })

    from chatbot.crm_metrics import sync_active_cycle_temperature

    sync_active_cycle_temperature(db, "lead-1", temperature="HOT", transition_at=hot_start)
    sync_active_cycle_temperature(db, "lead-1", temperature="HOT", transition_at=local_dt(7, 12, 5))
    persisted = db.crm_assignment_cycles.find_one({"assignment_cycle_id": "cycle-1"})
    assert coerce_utc_datetime(persisted["hot_started_at"]) == hot_start
    assert coerce_utc_datetime(persisted["temperature_transitioned_at"]) == hot_start
    assert sum(1 for segment in persisted["sla_segments"] if segment["policy"] == "HOT") == 1
    non_hot = [segment for segment in persisted["sla_segments"] if segment["policy"] == "NON_HOT"][0]
    assert coerce_utc_datetime(non_hot["segment_end"]) == hot_start


def test_metrics_persists_hot_cycle_transition_even_when_notification_is_suppressed():
    from unittest.mock import patch

    from chatbot.metrics import update_lead_metrics

    db = mongomock.MongoClient().sla_hot_transition_from_metrics
    lead_id = ObjectId()
    assigned = local_dt(7, 10, 0)
    db.leads.insert_one({
        "_id": lead_id,
        "phone": "+56912345678",
        "lead_temperature_effective": "NORMAL",
        "lead_temperature": "NORMAL",
        "last_intent": "ASK_VISIT",
        "pipeline_stage": "NEW",
        "lifecycle": {"assigned_at": assigned},
        "prospecto": {},
    })
    db.crm_assignment_cycles.insert_one({
        "lead_id": lead_id,
        "assignment_cycle_id": "cycle-transition-from-metrics",
        "assigned_at": assigned,
        "sla_started_at": assigned,
        "temperature_at_assignment": "NORMAL",
        "temperature_on_assignment": "NORMAL",
        "cycle_status": "active",
        "unassigned_at": None,
        "sla_segments": [{"policy": "NON_HOT", "segment_start": assigned, "segment_end": None}],
    })

    # Simulate the notification channel being suppressed. SLA state must still
    # persist the real transition on the active cycle.
    with patch("chatbot.metrics._enqueue_hot_lead_notification"):
        update_lead_metrics(
            db, "+56912345678", event_at=local_dt(7, 12, 0),
            event_type="STATUS_CHANGE", lead_id=lead_id,
        )

    lead = db.leads.find_one({"_id": lead_id})
    persisted = db.crm_assignment_cycles.find_one({"assignment_cycle_id": "cycle-transition-from-metrics"})
    hot_start = coerce_utc_datetime(lead["lifecycle"]["hot_since"])
    assert hot_start is not None
    assert coerce_utc_datetime(persisted["hot_started_at"]) == hot_start
    assert coerce_utc_datetime(persisted["temperature_transitioned_at"]) == hot_start
    assert persisted["temperature_on_assignment"] == "NORMAL"
    assert sum(1 for segment in persisted["sla_segments"] if segment["policy"] == "HOT") == 1


def test_hot_temperature_without_transition_time_does_not_create_retroactive_start():
    db = mongomock.MongoClient().sla_hot_missing_evidence
    assigned = local_dt(7, 10, 0)
    db.crm_assignment_cycles.insert_one({
        "lead_id": "lead-unknown-hot-start",
        "assignment_cycle_id": "cycle-unknown-hot-start",
        "assigned_at": assigned, "sla_started_at": assigned,
        "temperature_at_assignment": "NORMAL", "temperature_on_assignment": "NORMAL",
        "cycle_status": "active", "unassigned_at": None,
        "sla_segments": [{"policy": "NON_HOT", "segment_start": assigned, "segment_end": None}],
    })

    from chatbot.crm_metrics import sync_active_cycle_temperature

    sync_active_cycle_temperature(db, "lead-unknown-hot-start", temperature="HOT")
    persisted = db.crm_assignment_cycles.find_one({"assignment_cycle_id": "cycle-unknown-hot-start"})
    result = calculate_cycle_sla(
        cycle=persisted,
        lead={"lead_temperature_effective": "HOT"},
        now=local_dt(7, 14, 0),
    )

    assert "hot_started_at" not in persisted
    assert "temperature_transitioned_at" not in persisted
    assert result["canonical_state"] == "INSUFFICIENT_DATA"
    assert result["deadline_at"] is None
