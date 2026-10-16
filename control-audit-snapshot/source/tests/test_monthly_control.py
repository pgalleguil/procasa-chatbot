from datetime import datetime, timezone

from analytics.monthly_control import build_month_metrics, unavailable_month_payload


def _utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _cycle(cycle_id, *, lead="lead-1", owner="Ejecutiva", assigned="2026-08-03T13:00:00Z", start=None,
           temp="NORMAL", first=None, **extra):
    row = {
        "assignment_cycle_id": cycle_id,
        "lead_id": lead,
        "assigned_to_display_name": owner,
        "assigned_at": _utc(assigned),
        "sla_started_at": _utc(start or assigned),
        "temperature_at_assignment": temp,
        "reason": "lead_created",
        "cycle_origin": "inbound_message",
    }
    if first:
        row["first_valid_management_at"] = _utc(first)
    row.update(extra)
    return row


def _build(cycles, *, events=(), results=(), audits=(), leads=(), as_of="2026-10-11T15:00:00Z", executive=None):
    return build_month_metrics(
        cycles=cycles, leads=leads, events=events, results=results,
        sla_audits=audits, executive=executive, as_of=_utc(as_of),
    )


def test_multiple_assignments_same_lead_count_episodes_and_unique_lead_once():
    # The source episode is known to be the original assignment in this case;
    # production classification requires explicit provenance rather than
    # inferring "original" from the existence of a later SLA rescue.
    source = _cycle("old", assigned="2026-08-03T13:00:00Z", assignment_number=1)
    rescue = _cycle("new", assigned="2026-08-04T13:00:00Z", owner="Receptora",
                    reassignment_reason="sla_reassignment",
                    reassignment_meta={"previous_cycle_id": "old"})
    audit = {"event_type": "SLA_REASSIGNMENT_COMMITTED", "source_cycle_id": "old",
             "lead_id": "lead-1", "reassigned_at": rescue["assigned_at"]}
    data = _build([source, rescue], audits=[audit])
    assert data["episodes_received"] == 2
    assert data["unique_leads_received"] == 1
    assert data["assignment_types"] == {
        "original": 1, "sla_rescue": 1, "other_transfer": 0, "unknown": 0,
    }


def test_exact_normal_limit_is_breached_and_supervisor_human_note_is_first_management():
    cycle = _cycle("cycle", first="2026-08-03T16:00:00Z")  # 12:00 Chile: exactly 180 business minutes.
    event = {"lead_id": "lead-1", "assignment_cycle_id": "cycle", "type": "HUMAN_NOTE", "actor": "Supervisor",
             "actor_type": "supervisor", "result": "EFFECTIVE_CONTACT", "timestamp": _utc("2026-08-03T16:00:00Z")}
    data = _build([cycle], events=[event])
    assert data["sla"]["outside"] == 1
    assert data["sla"]["within"] == 0
    assert data["records"][0]["first_management_source"] == "crm_events"


def test_exact_hot_limit_is_breached():
    cycle = _cycle("hot", temp="HOT", first="2026-08-03T14:00:00Z")
    data = _build([cycle])  # 10:00 Chile: exactly 60 business minutes from 09:00.
    assert data["sla"]["hot"] == {"within": 0, "outside": 1, "evaluable": 1}


def test_hot_segment_that_starts_after_assignment_is_not_retroactively_attributed():
    cycle = _cycle("hot-later", temp="HOT", hot_started_at=_utc("2026-08-03T14:00:00Z"),
                   first="2026-08-03T14:30:00Z")
    data = _build([cycle])
    assert data["sla"]["not_evaluable"] == 1
    assert "regla gerencial parte del episodio" in data["records"][0]["sla_not_evaluable_reason"]


def test_holiday_on_weekday_counts_as_business_time_and_out_of_hours_assignment_uses_saved_sla_start():
    # 18 Sep 2026 is a Chilean holiday, but the adopted policy counts weekdays.
    holiday = _cycle("holiday", assigned="2026-09-18T13:00:00Z", first="2026-09-18T16:00:00Z")
    after_hours = _cycle("after", assigned="2026-08-07T00:00:00Z", start="2026-08-10T13:00:00Z",
                         first="2026-08-10T16:00:00Z", lead="lead-2")
    data = _build([holiday, after_hours])
    assert data["sla"]["outside"] == 2
    assert [row["first_management_business_minutes"] for row in data["records"]] == [180.0, 180.0]


def test_normal_to_hot_episode_is_not_comparable_under_conflicting_policies():
    cycle = _cycle("escalated", hot_started_at=_utc("2026-08-03T14:00:00Z"),
                   first="2026-08-03T14:30:00Z")
    data = _build([cycle])
    assert data["sla"]["not_evaluable"] == 1
    assert data["sla"]["evaluable"] == 0
    assert "Normal→Hot" in data["records"][0]["sla_not_evaluable_reason"]


def test_whatsapp_click_is_not_first_management_or_contact_evidence():
    cycle = _cycle("cycle")
    click = {"lead_id": "lead-1", "assignment_cycle_id": "cycle", "type": "CLICK_WHATSAPP_LEAD",
             "actor": "Ejecutiva", "actor_type": "human", "timestamp": _utc("2026-08-03T13:05:00Z")}
    data = _build([cycle], events=[click])
    assert data["first_management"]["episodes_managed"] == 0
    assert data["first_management"]["p50_n"] == 0
    assert data["contact_evidence"]["effective_contact_episodes"] == 0
    assert data["sla"]["outside"] == 1


def test_management_in_following_month_remains_linked_to_assignment_month_cohort():
    cycle = _cycle("late-cycle", assigned="2026-08-31T19:00:00Z", start="2026-08-31T19:00:00Z",
                   first="2026-09-01T14:00:00Z")
    data = _build([cycle])
    assert data["episodes_received"] == 1
    assert data["first_management"]["episodes_managed"] == 1
    assert data["records"][0]["sla_state"] == "MANAGED_OUTSIDE_SLA"


def test_duplicate_results_do_not_duplicate_episode_management_median():
    cycle = _cycle("cycle", first="2026-08-03T13:30:00Z")
    result = {"assignment_cycle_id": "cycle", "status": "completed", "actor_user_id": "agent",
              "occurred_at": _utc("2026-08-03T13:30:00Z"), "result_type": "CONTACTADO"}
    data = _build([cycle], results=[result, dict(result)])
    assert data["episodes_received"] == 1
    assert data["first_management"]["episodes_managed"] == 1
    assert data["first_management"]["p50_n"] == 1
    # The dashboard suppresses an overall P50 below five observations; the
    # episode-level duration remains available and deduplicated in the ledger.
    assert data["records"][0]["first_management_business_minutes"] == 30


def test_test_cycles_are_excluded_and_owner_comes_from_historical_episode():
    test = _cycle("test", lead="test-lead", reason="smoke_test", cycle_origin="smoke_test")
    old_owner = _cycle("historical", lead="lead-1", owner="María", first="2026-08-03T13:30:00Z")
    data = _build([test, old_owner], leads=[{"_id": "test-lead", "_test_lead": True}], executive="María")
    assert data["episodes_received"] == 1
    assert data["excluded_noncommercial_cycles"] == 1
    assert data["records"][0]["executive"] == "María"


def test_missing_assignment_temperature_stays_non_evaluable():
    cycle = _cycle("unknown-temperature", temp="", first="2026-08-03T13:30:00Z")
    data = _build([cycle])
    assert data["temperature_mix"]["unknown"] == 1
    assert data["sla"]["not_evaluable"] == 1


def test_unavailable_month_end_stock_and_later_events_are_not_reported_as_zero():
    data = unavailable_month_payload("Sin historial", period_start="2026-07-01", period_end="2026-07-31")
    assert data["availability"]["status"] == "unavailable"
    assert data["indicators"]["month_end_open_portfolio"]["status"] == "unavailable"
    assert "count" not in data["indicators"]["month_end_open_portfolio"]


def test_sla_rescue_committed_in_later_month_is_an_event_there_and_rescue_episode_there():
    # August source cycle ends when the September rescue is committed. The
    # August cohort must not count a September loss; the September incoming
    # episode links back to the August source cycle via reassignment_meta.
    source = _cycle("source", assigned="2026-08-31T19:00:00Z", lead="lead-2", owner="Anterior")
    incoming = _cycle("incoming", assigned="2026-09-01T13:00:00Z", lead="lead-2", owner="Actual",
                      reassignment_reason="sla_reassignment",
                      reassignment_meta={"previous_cycle_id": "source"})
    audit = {"event_type": "SLA_REASSIGNMENT_COMMITTED", "source_cycle_id": "source",
             "lead_id": "lead-2", "reassigned_at": incoming["assigned_at"],
             "source_owner_display_name": "Anterior", "new_cycle_id": "incoming"}
    august = _build([source], audits=[])
    september = _build([incoming], audits=[audit], executive="Actual")
    source_view = _build([incoming], audits=[audit], executive="Anterior")
    assert august["sla_losses_executed"] == 0
    assert september["assignment_types"]["sla_rescue"] == 1
    assert september["sla_losses_executed"] == 0  # the loss belongs to the prior executive.
    assert source_view["sla_losses_executed"] == 1
