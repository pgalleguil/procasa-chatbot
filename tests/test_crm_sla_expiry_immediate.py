from datetime import datetime, timedelta, timezone

import pytest

from config import Config
from chatbot.crm_lead_access import (
    ContactVisibility,
    SLA_EXPIRED_PENDING_REASSIGNMENT,
    resolve_crm_lead_access_context,
)
from chatbot.crm_sla_cycle_links import (
    SlaCycleLinkError,
    build_sla_cycle_url,
    validate_sla_cycle_link,
)
from chatbot.crm_sla_reassignment_worker import (
    canonical_expiration_recheck,
    scan_sla_reassignment_fast_path_candidates,
)


UTC = timezone.utc


def _matches(row, query):
    for key, expected in (query or {}).items():
        if key in {"$and", "$or"}:
            children = expected
            if key == "$and" and not all(_matches(row, item) for item in children):
                return False
            if key == "$or" and not any(_matches(row, item) for item in children):
                return False
            continue
        value = row.get(key)
        if isinstance(expected, dict):
            for operator, operand in expected.items():
                if operator == "$in" and value not in operand:
                    return False
                if operator == "$lte" and (value is None or value > operand):
                    return False
                if operator == "$gte" and (value is None or value < operand):
                    return False
                if operator == "$exists" and (key in row) != bool(operand):
                    return False
                if operator == "$ne" and key in row and value == operand:
                    return False
        elif expected is None:
            if key in row and value is not None:
                return False
        elif value != expected:
            return False
    return True


class Collection:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def find_one(self, query, *args, **kwargs):
        return next((row for row in self.rows if _matches(row, query)), None)

    def find(self, query, *args, **kwargs):
        return [row for row in self.rows if _matches(row, query)]


class DB:
    def __init__(self, lead, cycle):
        self.collections = {
            "leads": Collection([lead]),
            "crm_assignment_cycles": Collection([cycle]),
        }

    def __getitem__(self, name):
        return self.collections[name]


def _fixture():
    assigned = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    lead = {
        "_id": "lead-1",
        "lifecycle": {"current_assignment_cycle_id": "cycle-1"},
        "owner_user_id": "owner-1",
        "ejecutivo_asignado": "Owner",
        "pipeline_stage": "NEW",
    }
    cycle = {
        "_id": "cycle-doc-1",
        "lead_id": "lead-1",
        "assignment_cycle_id": "cycle-1",
        "assigned_to_user_id": "owner-1",
        "assigned_to_display_name": "Owner",
        "assigned_at": assigned,
        "sla_started_at": assigned,
        "temperature_at_assignment": "NORMAL",
        "cycle_status": "active",
        "unassigned_at": None,
        "reassignment_decision_id": "previous-reassignment",
    }
    return DB(lead, cycle), lead, cycle


def test_cycle_bound_link_expires_at_canonical_deadline(monkeypatch):
    db, lead, cycle = _fixture()
    expiration = canonical_expiration_recheck(
        cycle, lead, now=datetime(2026, 9, 17, 20, 0, tzinfo=UTC)
    )
    token = build_sla_cycle_url(
        lead_id=lead["_id"], recipient_user_id="owner-1",
        assignment_cycle_id=cycle["assignment_cycle_id"], base_url="https://crm.test",
    ).rsplit("/", 1)[-1]

    monkeypatch.setattr(
        "chatbot.crm_metrics.utc_now",
        lambda: expiration.breach_at - timedelta(seconds=1),
    )
    assert validate_sla_cycle_link(
        db, token, authenticated_user_id="owner-1"
    ).cycle["assignment_cycle_id"] == "cycle-1"

    monkeypatch.setattr("chatbot.crm_metrics.utc_now", lambda: expiration.breach_at)
    with pytest.raises(SlaCycleLinkError) as exc:
        validate_sla_cycle_link(db, token, authenticated_user_id="owner-1")
    assert exc.value.code == SLA_EXPIRED_PENDING_REASSIGNMENT
    assert exc.value.status_code == 409


def test_expired_owner_access_is_redacted_and_non_operational(monkeypatch):
    db, lead, cycle = _fixture()
    expiration = canonical_expiration_recheck(
        cycle, lead, now=datetime(2026, 9, 17, 20, 0, tzinfo=UTC)
    )
    monkeypatch.setattr("chatbot.crm_metrics.utc_now", lambda: expiration.breach_at)
    context = resolve_crm_lead_access_context(
        db,
        user={"_id": "owner-1", "rol": "agente"},
        lead=lead,
        security_enabled=True,
    )
    assert context.lock_reason == SLA_EXPIRED_PENDING_REASSIGNMENT
    assert context.access_allowed is False
    assert context.contact_visibility == ContactVisibility.NONE
    assert not any(context.action_permissions.values())


@pytest.mark.asyncio
async def test_fast_path_reads_deadline_hints_before_maintenance(monkeypatch):
    observed = {}
    row = {"assignment_cycle_id": "cycle-1", "lead_id": "lead-1"}

    async def fake_find_many(collection, query, projection, **kwargs):
        observed["query"] = query
        observed["query_name"] = kwargs["query_name"]
        return [row]

    monkeypatch.setattr(
        "chatbot.crm_sla_reassignment_worker._find_many", fake_find_many
    )
    result = await scan_sla_reassignment_fast_path_candidates(
        db={"crm_assignment_cycles": object()},
        now=datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
        current_policy_since=datetime(2026, 9, 1, tzinfo=UTC),
        batch_size=25,
    )
    assert result["scan_mode"] == "fast_path"
    assert result["cycles"] == [row]
    assert observed["query_name"] == "worker.scanner.fast_path_cycles"
    assert "sla_breached_at" in repr(observed["query"])
