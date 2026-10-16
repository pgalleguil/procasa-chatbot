from datetime import datetime, timedelta, timezone

from analytics import leads_service
from analytics.monthly_control import build_month_metrics


def _period_payload(start, end, *, episodes=20, available=True):
    return {
        "period": {"start": start, "end": end},
        "availability": {"status": "available" if available else "unavailable",
                         "reason": None if available else "outside coverage"},
        "episodes_received": episodes,
        "unique_leads_received": episodes,
        "assignment_types": {"original": episodes, "sla_rescue": 0,
                              "other_transfer": 0, "unknown": 0},
        "sla": {"within": 10, "outside": 5, "evaluable": 15,
                "not_evaluable": episodes - 15,
                "hot": {"within": 0, "outside": 0, "evaluable": 0},
                "normal": {"within": 10, "outside": 5, "evaluable": 15}},
        "first_management": {"episodes_managed": 10, "denominator": episodes,
                             "traceable": episodes, "untraceable": 0},
        "sla_losses_executed": 2,
    }


def test_custom_september_auto_uses_shared_previous_window(monkeypatch):
    calls = []

    def fake_query(*, period_start, period_end, executive):
        calls.append((period_start, period_end, executive))
        return _period_payload(period_start, period_end)

    monkeypatch.setattr(leads_service, "query_monthly_leads_control", fake_query)
    result = leads_service.get_monthly_leads_control(
        period_start="2026-09-01", period_end="2026-09-30",
        executive="Executive D", compare="auto", period_preset="custom",
    )

    assert result["comparison"]["period"] == {"start": "2026-08-02", "end": "2026-08-31"}
    assert calls == [
        ("2026-09-01", "2026-09-30", "Executive D"),
        ("2026-08-02", "2026-08-31", "Executive D"),
    ]


def test_august_comparison_to_july_is_blocked(monkeypatch):
    def fake_query(*, period_start, period_end, executive):
        return _period_payload(period_start, period_end,
                               available=period_start >= "2026-08-01")

    monkeypatch.setattr(leads_service, "query_monthly_leads_control", fake_query)
    result = leads_service.get_monthly_leads_control(
        period_start="2026-08-01", period_end="2026-08-31",
        executive="Executive D", compare="auto", period_preset="month",
    )

    assert result["comparison"]["period"] == {"start": "2026-07-01", "end": "2026-07-31"}
    assert result["comparison"]["status"] == "unavailable"
    assert result["comparison"]["metrics"] is None


def test_none_and_yoy_keep_primary_values_without_unreliable_comparison(monkeypatch):
    calls = []

    def fake_query(*, period_start, period_end, executive):
        calls.append((period_start, period_end))
        return _period_payload(period_start, period_end,
                               available=period_start >= "2026-08-01")

    monkeypatch.setattr(leads_service, "query_monthly_leads_control", fake_query)
    none_result = leads_service.get_monthly_leads_control(
        period_start="2026-09-01", period_end="2026-09-30",
        executive="Executive D", compare="none", period_preset="custom",
    )
    assert none_result["episodes_received"] == 20
    assert none_result["comparison"]["status"] == "not_requested"
    assert len(calls) == 1

    yoy_result = leads_service.get_monthly_leads_control(
        period_start="2026-09-01", period_end="2026-09-30",
        executive="Executive D", compare="yoy", period_preset="custom",
    )
    assert yoy_result["episodes_received"] == 20
    assert yoy_result["comparison"]["period"] == {"start": "2025-09-01", "end": "2025-09-30"}
    assert yoy_result["comparison"]["status"] == "unavailable"


def test_assignment_categories_reconcile_unique_cycles():
    at = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    cycles = [
        {"assignment_cycle_id": "original", "lead_id": "lead-1", "assigned_at": at,
         "assigned_to_display_name": "Executive D", "assigned_by": "commercial_intake",
         "temperature_at_assignment": "NORMAL"},
        {"assignment_cycle_id": "rescue", "lead_id": "lead-2", "assigned_at": at,
         "assigned_to_display_name": "Executive D", "reassignment_reason": "sla_reassignment",
         "temperature_at_assignment": "NORMAL"},
        {"assignment_cycle_id": "transfer", "lead_id": "lead-3", "assigned_at": at,
         "assigned_to_display_name": "Executive D", "reassignment_reason": "manual_transfer",
         "temperature_at_assignment": "NORMAL"},
        {"assignment_cycle_id": "unknown", "lead_id": "lead-4", "assigned_at": at,
         "assigned_to_display_name": "Executive D", "temperature_at_assignment": "NORMAL"},
        {"assignment_cycle_id": "original", "lead_id": "lead-1", "assigned_at": at,
         "assigned_to_display_name": "Executive D", "assigned_by": "commercial_intake",
         "temperature_at_assignment": "NORMAL"},
    ]
    result = build_month_metrics(
        cycles=cycles, leads=[], events=[], results=[],
        sla_audits=[{"event_type": "SLA_REASSIGNMENT_COMMITTED",
                     "source_cycle_id": "previous", "new_cycle_id": "rescue",
                     "reassigned_at": at.isoformat()}],
        executive="Executive D", as_of=datetime(2026, 9, 4, 12, tzinfo=timezone.utc),
    )

    assert result["episodes_received"] == 4
    kinds = result["assignment_types"]
    assert kinds == {"original": 1, "sla_rescue": 1, "other_transfer": 1, "unknown": 1}
    assert sum(kinds.values()) == result["episodes_received"]
    assert result["duplicate_cycle_records_ignored"] == 1


def test_effective_contacts_use_only_canonical_completed_crm_results():
    at = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    cycles = [
        {"assignment_cycle_id": "contacted", "lead_id": "lead-1", "assigned_at": at,
         "assigned_to_display_name": "Executive D", "assigned_by": "commercial_intake",
         "temperature_at_assignment": "NORMAL"},
        {"assignment_cycle_id": "attempt", "lead_id": "lead-2", "assigned_at": at,
         "assigned_to_display_name": "Executive D", "assigned_by": "commercial_intake",
         "temperature_at_assignment": "NORMAL"},
        {"assignment_cycle_id": "event-only", "lead_id": "lead-3", "assigned_at": at,
         "assigned_to_display_name": "Executive D", "assigned_by": "commercial_intake",
         "temperature_at_assignment": "NORMAL"},
    ]
    results = [
        {"assignment_cycle_id": "contacted", "result_type": "CONTACTADO",
         "status": "completed", "actor_user_id": "agent-1", "occurred_at": at},
        {"assignment_cycle_id": "contacted", "result_type": "CONTACTADO",
         "status": "completed", "actor_user_id": "agent-1", "occurred_at": at},
        {"assignment_cycle_id": "attempt", "result_type": "NO_RESPONDIO",
         "status": "completed", "actor_user_id": "agent-1", "occurred_at": at},
    ]
    events = [{"assignment_cycle_id": "event-only", "lead_id": "lead-3",
               "type": "CONTACT_RESULT", "actor": "agent-1", "actor_type": "human",
               "confirmed": True, "result": "CONTACTADO", "timestamp": at}]

    result = build_month_metrics(
        cycles=cycles, leads=[], events=events, results=results,
        sla_audits=[], executive="Executive D", as_of=at,
    )

    assert result["contact_evidence"]["attempt_episodes"] == 3
    assert result["contact_evidence"]["effective_contact_episodes"] == 1
    assert result["contact_evidence"]["basis"] == (
        "crm_management_results completed with actor_user_id and canonical EFFECTIVE_CONTACT_RESULTS"
    )


def _control_cycle(cycle_id, assigned_at, *, sla_started_at=None, owner="Executive D",
                   assigned_by="commercial_intake", result_at=None, **extra):
    return {
        "assignment_cycle_id": cycle_id,
        "lead_id": f"lead-{cycle_id}",
        "assigned_at": assigned_at,
        "sla_started_at": sla_started_at or assigned_at,
        "assigned_to_display_name": owner,
        "assigned_to_user_id": "hernan-id",
        "assigned_by": assigned_by,
        "assignment_number": 1,
        "temperature_at_assignment": "NORMAL",
        "first_valid_management_at": result_at,
        **extra,
    }


def _control_result(cycle_id, occurred_at, result_type="CONTACTADO"):
    return {
        "assignment_cycle_id": cycle_id,
        "result_type": result_type,
        "status": "completed",
        "actor_user_id": "hernan-id",
        "occurred_at": occurred_at,
    }


def test_control_excludes_commercial_control_manager_without_removing_raw_history():
    at = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    cycles = [
        _control_cycle("manager-cycle", at, owner="Control Owner"),
        _control_cycle("broker-cycle", at, owner="Executive D"),
    ]
    audits = [
        {"event_type": "SLA_REASSIGNMENT_COMMITTED", "source_owner_display_name": "Control Owner",
         "source_cycle_id": "manager-cycle", "new_cycle_id": "target-1", "reassigned_at": at.isoformat()},
        {"event_type": "SLA_REASSIGNMENT_COMMITTED", "source_owner_display_name": "Executive E",
         "source_cycle_id": "source-2", "new_cycle_id": "target-2", "reassigned_at": at.isoformat()},
    ]

    team = build_month_metrics(
        cycles=cycles, leads=[], events=[], results=[], sla_audits=audits,
        executive=None, as_of=at,
    )
    assert team["episodes_received"] == 1
    assert team["records"][0]["executive"] == "Executive D"
    assert team["excluded_non_broker_cycles"] == 1
    # Losses are scoped to source cycles in the same received cohort; the
    # unrelated prior-period audit does not belong in this period's cards.
    assert team["sla_losses_executed"] == 0

    manager_scope = build_month_metrics(
        cycles=cycles, leads=[], events=[], results=[], sla_audits=audits,
        executive="Control Owner", as_of=at,
    )
    assert manager_scope["episodes_received"] == 0
    assert manager_scope["sla_losses_executed"] == 0
    assert manager_scope["excluded_non_broker_cycles"] == 1


def test_valid_management_between_assignment_and_sla_start_counts_with_zero_sla_minutes_and_p50():
    # Sept 8 is a Tuesday. UTC noon is 09:00 in Chile for this date.
    assignment = datetime(2026, 9, 8, 11, 30, tzinfo=timezone.utc)  # 08:30 CL
    sla_start = datetime(2026, 9, 8, 12, 30, tzinfo=timezone.utc)  # 09:30 CL
    early_management = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)  # 09:00 CL
    cycles = [_control_cycle("early", assignment, sla_started_at=sla_start,
                             result_at=early_management)]
    results = [_control_result("early", early_management)]
    expected_minutes = [0]
    for index, elapsed in enumerate((30, 60, 120, 180), start=1):
        cycle_id = f"managed-{index}"
        start = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
        first = start + timedelta(minutes=elapsed)
        cycles.append(_control_cycle(cycle_id, start, result_at=first))
        results.append(_control_result(cycle_id, first))
        expected_minutes.append(elapsed)

    result = build_month_metrics(
        cycles=cycles, leads=[], events=[], results=results, sla_audits=[],
        executive="Executive D", as_of=datetime(2026, 10, 12, tzinfo=timezone.utc),
    )

    early = next(row for row in result["records"] if row["cycle_id"] == "early")
    assert early["first_management_at"] == early_management.isoformat()
    assert early["management_before_sla_start"] is True
    assert early["first_management_business_minutes"] == 0.0
    assert early["sla_state"] == "MANAGED_WITHIN_SLA"
    assert result["first_management"]["episodes_managed"] == 5
    assert result["first_management"]["p50_n"] == 5
    assert result["first_management"]["p50_business_minutes"] == 60.0


def test_management_inside_sla_is_within_and_exact_limit_or_late_is_outside_without_transfer():
    start = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)  # 09:00 CL
    within = start + timedelta(minutes=179)
    exact = start + timedelta(minutes=180)
    late = start + timedelta(minutes=181)
    cycles = [
        _control_cycle("within", start, result_at=within),
        _control_cycle("exact", start, result_at=exact),
        _control_cycle("late", start, result_at=late),
    ]
    results = [_control_result(cycle_id, at) for cycle_id, at in (
        ("within", within), ("exact", exact), ("late", late),
    )]

    result = build_month_metrics(
        cycles=cycles, leads=[], events=[], results=results, sla_audits=[],
        executive="Executive D", as_of=datetime(2026, 10, 12, tzinfo=timezone.utc),
    )

    states = {row["cycle_id"]: row["sla_state"] for row in result["records"]}
    assert states == {
        "within": "MANAGED_WITHIN_SLA",
        "exact": "MANAGED_OUTSIDE_SLA",
        "late": "MANAGED_OUTSIDE_SLA",
    }
    assert result["sla"]["within"] == 1
    assert result["sla"]["outside"] == 2
    assert result["sla_losses_executed"] == 0


def test_policy_repair_cycles_require_evidence_of_changed_commercial_owner():
    at = datetime(2026, 9, 16, 17, 24, tzinfo=timezone.utc)
    technical = _control_cycle(
        "policy-repair-same-owner", at, assigned_by="policy_repair",
        policy_repair_id="repair-same", policy_repair_version="v1",
        policy_repair_previous_owner_user_id="hernan-id",
        policy_repair_selected_user_id="hernan-id",
        reassignment_reason="POLICY_REPAIR_TIER1",
    )
    commercial = _control_cycle(
        "policy-repair-transfer", at, assigned_by="policy_repair",
        policy_repair_id="repair-transfer", policy_repair_version="v1",
        policy_repair_previous_owner_user_id="admin-id",
        policy_repair_selected_user_id="hernan-id",
        reassignment_reason="POLICY_REPAIR_ADMIN_EXCLUDED",
    )

    result = build_month_metrics(
        cycles=[technical, commercial], leads=[], events=[], results=[], sla_audits=[],
        executive="Executive D", as_of=datetime(2026, 10, 12, tzinfo=timezone.utc),
    )

    assert result["episodes_received"] == 1
    assert result["excluded_technical_cycles"] == 1
    assert result["records"][0]["cycle_id"] == "policy-repair-transfer"
    assert result["assignment_types"]["other_transfer"] == 1


def test_four_card_reconciliation_for_episode_cohort():
    at = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    within = at + timedelta(minutes=30)
    late = at + timedelta(minutes=181)
    cycles = [
        _control_cycle("original", at, result_at=within),
        _control_cycle("rescue", at, assigned_by="sla_reassignment",
                       assignment_number=2, reassignment_reason="sla_reassignment", result_at=late),
        _control_cycle("repair-transfer", at, assigned_by="policy_repair",
                       policy_repair_id="repair", policy_repair_previous_owner_user_id="admin-id",
                       policy_repair_selected_user_id="hernan-id", reassignment_reason="POLICY_REPAIR_ADMIN_EXCLUDED",
                       result_at=within),
    ]
    results = [_control_result("original", within), _control_result("rescue", late),
               _control_result("repair-transfer", within)]
    audits = [{"event_type": "SLA_REASSIGNMENT_COMMITTED", "source_cycle_id": "source",
               "new_cycle_id": "rescue", "reassigned_at": at.isoformat(),
               "source_owner_display_name": "Otro ejecutivo"}]

    result = build_month_metrics(
        cycles=cycles, leads=[], events=[], results=results, sla_audits=audits,
        executive="Executive D", as_of=datetime(2026, 10, 12, tzinfo=timezone.utc),
    )

    assert result["episodes_received"] == 3
    assert sum(result["assignment_types"].values()) == 3
    assert result["sla"]["within"] == 2
    assert result["sla"]["outside"] == 1
    assert result["first_management"]["episodes_managed"] == 3
    assert result["sla_losses_executed"] == 0


def test_sla_losses_are_intersected_with_the_selected_received_lead_cohort():
    at = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    cycles = [
        _control_cycle("received-1", at),
        _control_cycle("received-2", at + timedelta(minutes=1)),
    ]
    audits = [
        # Two executed transitions for one of the two received leads count
        # as two movements but only one lost lead.
        {"event_type": "SLA_REASSIGNMENT_COMMITTED", "lead_id": "lead-received-1",
         "source_cycle_id": "received-1", "new_cycle_id": "successor-1",
         "source_owner_display_name": "Executive D", "reassigned_at": at.isoformat()},
        {"event_type": "SLA_REASSIGNMENT_COMMITTED", "lead_id": "lead-received-1",
         "source_cycle_id": "received-1", "new_cycle_id": "successor-2",
         "source_owner_display_name": "Executive D",
         "reassigned_at": (at + timedelta(minutes=5)).isoformat()},
        # This transfer occurred in the same date window, but its lead and
        # source cycle were not part of the selected executive's cohort.
        {"event_type": "SLA_REASSIGNMENT_COMMITTED", "lead_id": "lead-prior-period",
         "source_cycle_id": "old-cycle", "new_cycle_id": "successor-3",
         "source_owner_display_name": "Executive D",
         "reassigned_at": (at + timedelta(minutes=10)).isoformat()},
    ]

    result = build_month_metrics(
        cycles=cycles, leads=[], events=[], results=[], sla_audits=audits,
        executive="Executive D", as_of=datetime(2026, 10, 12, tzinfo=timezone.utc),
    )

    assert result["unique_leads_received"] == 2
    assert result["sla_losses_executed"] == 2
    assert result["unique_leads_lost_by_sla"] == 1


def test_management_p50_is_split_by_historical_temperature_and_keeps_late_and_pre_clock_cases():
    start = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)  # 09:00 Chile
    hot_first = start + timedelta(minutes=20)
    normal_first = start + timedelta(minutes=30)
    late_first = start + timedelta(minutes=181)
    pre_clock_assignment = start - timedelta(minutes=30)
    pre_clock_first = start
    escalated_first = start + timedelta(minutes=40)
    cycles = [
        _control_cycle("hot", start, result_at=hot_first, temperature_at_assignment="HOT",
                       hot_started_at=start),
        _control_cycle("normal", start, result_at=normal_first),
        _control_cycle("late-normal", start, result_at=late_first),
        _control_cycle("pre-clock-normal", pre_clock_assignment,
                       sla_started_at=start + timedelta(minutes=30), result_at=pre_clock_first),
        _control_cycle("escalated", start, result_at=escalated_first,
                       hot_started_at=start + timedelta(minutes=10)),
    ]
    results = [_control_result(cycle["assignment_cycle_id"], cycle["first_valid_management_at"])
               for cycle in cycles]

    result = build_month_metrics(
        cycles=cycles, leads=[], events=[], results=results, sla_audits=[],
        executive="Executive D", as_of=datetime(2026, 10, 12, tzinfo=timezone.utc),
    )

    segments = result["first_management"]["p50_by_temperature"]
    assert segments["hot"]["n"] == 2
    assert segments["hot"]["business_minutes"] == 30.0
    assert segments["normal"]["n"] == 3
    assert segments["normal"]["business_minutes"] == 30.0
    assert segments["episode_reconciliation"] == 5
    records = {row["cycle_id"]: row for row in result["records"]}
    assert records["late-normal"]["first_management_business_minutes"] == 181.0
    assert records["late-normal"]["p50_temperature"] == "NORMAL"
    assert records["pre-clock-normal"]["management_before_sla_start"] is True
    assert records["pre-clock-normal"]["first_management_business_minutes"] == 0.0
    assert records["escalated"]["p50_temperature"] == "HOT"


def test_management_p50_reports_small_samples_and_unverifiable_temperature_without_zero_fallback():
    start = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    hot_first = start + timedelta(minutes=15)
    unknown_first = start + timedelta(minutes=25)
    cycles = [
        _control_cycle("single-hot", start, result_at=hot_first,
                       temperature_at_assignment="HOT", hot_started_at=start),
        _control_cycle("unknown-temperature", start, result_at=unknown_first,
                       temperature_at_assignment=""),
    ]
    result = build_month_metrics(
        cycles=cycles, leads=[], events=[],
        results=[_control_result("single-hot", hot_first),
                 _control_result("unknown-temperature", unknown_first)],
        sla_audits=[], executive="Executive D",
        as_of=datetime(2026, 10, 12, tzinfo=timezone.utc),
    )

    segments = result["first_management"]["p50_by_temperature"]
    assert segments["hot"] == {
        "business_minutes": 15.0, "n": 1, "sla_reference_minutes": 60,
        "low_sample": True, "descriptive_only": True,
    }
    assert segments["normal"]["business_minutes"] is None
    assert segments["normal"]["n"] == 0
    assert segments["unverified_temperature_episodes"] == 1
    assert segments["episode_reconciliation"] == 2
    unknown = next(row for row in result["records"] if row["cycle_id"] == "unknown-temperature")
    assert unknown["p50_temperature"] is None
    assert "No existe temperatura histórica" in unknown["p50_exclusion_reason"]
