"""P0 SLA: the live-only optimization cannot weaken executor safeguards."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from chatbot.crm_sla_reassignment_executor import _cycle_field_protection
from chatbot.crm_sla_reassignment_worker import _legacy_lifecycle_blocked_by_executor


UTC = timezone.utc
ASSIGNED_AT = datetime(2026, 10, 13, 15, tzinfo=UTC)
BREACH_AT = ASSIGNED_AT + timedelta(hours=1)


@pytest.mark.parametrize("field", ["first_contact_attempt_at", "first_valid_management_at"])
def test_pre_assignment_marker_is_guarded_only_when_executor_would_abort(field):
    cycle = {"assigned_at": ASSIGNED_AT}
    lead = {"lifecycle": {field: ASSIGNED_AT - timedelta(days=2)}}
    assert _legacy_lifecycle_blocked_by_executor(cycle, lead, breach_at=BREACH_AT) == field
    assert _cycle_field_protection(cycle, lead, breach_at=BREACH_AT) is not None
    # A changed marker is never cached or suppressed persistently.
    assert _legacy_lifecycle_blocked_by_executor(cycle, {"lifecycle": {}},
                                                 breach_at=BREACH_AT) is None


def test_in_window_management_still_uses_original_canonical_protection():
    cycle = {"assigned_at": ASSIGNED_AT}
    lead = {"lifecycle": {"first_contact_attempt_at": ASSIGNED_AT + timedelta(minutes=10)}}
    assert _legacy_lifecycle_blocked_by_executor(cycle, lead, breach_at=BREACH_AT) is None
    assert _cycle_field_protection(cycle, lead, breach_at=BREACH_AT) is not None


def test_after_breach_management_remains_for_executor_final_recheck():
    cycle = {"assigned_at": ASSIGNED_AT}
    lead = {"lifecycle": {"first_contact_attempt_at": BREACH_AT + timedelta(minutes=2)}}
    assert _legacy_lifecycle_blocked_by_executor(cycle, lead, breach_at=BREACH_AT) is None


@pytest.mark.parametrize("cycle,lead,breach", [
    ({}, {}, BREACH_AT),
    ({"assigned_at": ASSIGNED_AT}, {}, None),
    ({"assigned_at": ASSIGNED_AT}, {"lifecycle": {"first_contact_attempt_at": "not-a-date"}}, BREACH_AT),
    ({"assigned_at": ASSIGNED_AT}, {"lifecycle": {"first_contact_attempt_at": None}}, BREACH_AT),
])
def test_uncertain_cases_fall_through_to_existing_transaction_safety(cycle, lead, breach):
    assert _legacy_lifecycle_blocked_by_executor(cycle, lead, breach_at=breach) is None


def test_only_live_worker_uses_preselection_filter_before_transacting():
    source = (Path(__file__).resolve().parents[1] /
              "chatbot/crm_sla_reassignment_worker.py").read_text(encoding="utf-8")
    idx = source.index('if execution_mode == "live":\n                    current_lead')
    assert idx > source.index('if protection.protected:')
    assert idx < source.index('assignment_number = _assignment_number(cycle)', idx)
    assert "EXECUTOR_HISTORICAL_LIFECYCLE_GUARD" in source
    executor = (Path(__file__).resolve().parents[1] /
                "chatbot/crm_sla_reassignment_executor.py").read_text(encoding="utf-8")
    assert "def _revalidate_management_before_commit(" in executor
    assert "def _cycle_field_protection(" in executor
