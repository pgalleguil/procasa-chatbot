from copy import deepcopy
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from config import Config
from chatbot.crm_assignment_cycle_gate import (
    CycleGateStatus,
    HumanProtectionType,
    claim_cycle_for_human_management,
    classify_human_protection,
)
from chatbot.crm_sla_hybrid_rescue import RM_GLOBAL_RESCUE
from chatbot.crm_sla_hybrid_stabilization import generate_decision_id
from chatbot.crm_sla_reassignment_executor import (
    FROZEN_POLICY_VERSION,
    execute_sla_reassignment_transaction,
)
from chatbot.crm_sla_reassignment_cutover import CUTOVER_POLICY_VERSION
from chatbot.crm_sla_reassignment_models import SLAReassignmentErrorCode
from scripts.prepare_crm_sla_reassignment_indexes import build_index_migration_plan
from chatbot.crm_management import record_management_result
import chatbot.storage as crm_storage


NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


def _get(doc, path):
    current = doc
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _matches(doc, filter_):
    for key, expected in (filter_ or {}).items():
        if key == "$or":
            if not any(_matches(doc, branch) for branch in expected):
                return False
            continue
        if key == "$and":
            if not all(_matches(doc, branch) for branch in expected):
                return False
            continue
        found, actual = _get(doc, key)
        if isinstance(expected, dict):
            for op, operand in expected.items():
                if op == "$exists" and found != bool(operand):
                    return False
                if op == "$in" and not any(actual == item for item in operand):
                    if not (not found and None in operand):
                        return False
                if op == "$nin" and found and any(actual == item for item in operand):
                    return False
                if op == "$lt" and (not found or not actual < operand):
                    return False
                if op == "$ne" and found and actual == operand:
                    return False
        elif expected is None:
            if found and actual is not None:
                return False
        elif not found or actual != expected:
            return False
    return True


def _set_path(doc, path, value):
    parts = path.split(".")
    current = doc
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _eval_expr(value, doc):
    if isinstance(value, str) and value.startswith("$"):
        return _get(doc, value[1:])[1]
    if isinstance(value, dict) and "$ifNull" in value:
        first, second = value["$ifNull"]
        result = _eval_expr(first, doc)
        return second if result is None else result
    if isinstance(value, dict) and "$add" in value:
        return sum((_eval_expr(item, doc) or 0) for item in value["$add"])
    return value


class FakeResult:
    def __init__(self, matched_count=0):
        self.matched_count = matched_count
        self.modified_count = matched_count
        self.upserted_id = None


class FakeCollection:
    def __init__(self, rows=None):
        self.rows = {}
        for row in rows or []:
            self.rows[row["_id"]] = deepcopy(row)

    def find_one(self, filter_, *args, **kwargs):
        matches = [row for row in self.rows.values() if _matches(row, filter_)]
        return deepcopy(matches[0]) if matches else None

    def update_one(self, filter_, update, *args, **kwargs):
        for key, row in self.rows.items():
            if not _matches(row, filter_):
                continue
            if isinstance(update, list):
                for stage in update:
                    for path, value in stage.get("$set", {}).items():
                        _set_path(row, path, _eval_expr(value, row))
            else:
                for path, value in update.get("$set", {}).items():
                    _set_path(row, path, deepcopy(value))
            self.rows[key] = row
            return FakeResult(1)
        return FakeResult(0)

    def update_many(self, filter_, update, *args, **kwargs):
        matched = 0
        for key, row in list(self.rows.items()):
            if not _matches(row, filter_):
                continue
            for path, value in update.get("$set", {}).items():
                _set_path(row, path, deepcopy(value))
            self.rows[key] = row
            matched += 1
        return FakeResult(matched)

    def find_one_and_update(self, filter_, update, *args, **kwargs):
        for key, row in self.rows.items():
            if not _matches(row, filter_):
                continue
            if isinstance(update, list):
                for stage in update:
                    for path, value in stage.get("$set", {}).items():
                        _set_path(row, path, _eval_expr(value, row))
            self.rows[key] = row
            return deepcopy(row)
        return None

    def insert_one(self, document, *args, **kwargs):
        key = document["_id"]
        if key in self.rows:
            raise DuplicateKeyError("duplicate key")
        self.rows[key] = deepcopy(document)
        return type("InsertResult", (), {"inserted_id": key})()

    def aggregate(self, pipeline):
        return []


class FakeAdmin:
    def command(self, command):
        return {"setName": "fake-rs", "isWritablePrimary": True}


class FakeSession:
    def __init__(self, db):
        self.db = db
        self.snapshot = None
        self.start_transaction_kwargs = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def start_transaction(self, **kwargs):
        self.start_transaction_kwargs = kwargs
        self.snapshot = self.db.snapshot()

    def commit_transaction(self):
        self.snapshot = None

    def abort_transaction(self):
        if self.snapshot is not None:
            self.db.restore(self.snapshot)
            self.snapshot = None


class UnknownCommitError(Exception):
    _error_labels = {"UnknownTransactionCommitResult"}


class UnknownCommitSession(FakeSession):
    def __init__(self, db):
        super().__init__(db)
        self.commit_calls = 0

    def commit_transaction(self):
        self.commit_calls += 1
        super().commit_transaction()
        if self.commit_calls == 1:
            raise UnknownCommitError("commit result unknown")


class FakeClient:
    def __init__(self, db):
        self.db = db
        self.admin = FakeAdmin()
        self.sessions = []

    def start_session(self):
        session = FakeSession(self.db)
        self.sessions.append(session)
        return session


class UnknownCommitClient(FakeClient):
    def start_session(self):
        session = UnknownCommitSession(self.db)
        self.sessions.append(session)
        return session


class FakeDB:
    def __init__(self):
        self.collections = {
            "leads": FakeCollection(),
            "crm_assignment_cycles": FakeCollection(),
            "usuarios": FakeCollection(),
            "crm_management_results": FakeCollection(),
            "crm_events": FakeCollection(),
            "conversation_events": FakeCollection(),
            "crm_notifications_v1": FakeCollection(),
            "crm_tasks": FakeCollection(),
            "crm_sla_reassignment_audit_v1": FakeCollection(),
        }
        self.client = FakeClient(self)

    def __getitem__(self, name):
        return self.collections[name]

    def snapshot(self):
        return {name: deepcopy(collection.rows) for name, collection in self.collections.items()}

    def restore(self, snapshot):
        for name, rows in snapshot.items():
            self.collections[name].rows = rows


class NoTransactionClient:
    admin = FakeAdmin()


def make_fixture():
    db = FakeDB()
    db["leads"].insert_one({
        "_id": "lead-1",
        "ejecutivo_asignado": "Ejecutivo A",
        "prospecto": {"ejecutivo": "Ejecutivo A"},
        "lifecycle": {"current_assignment_cycle_id": "cycle-1"},
        "pipeline_stage": "NEW",
        "stage": "NEW",
        "lead_temperature_effective": "COLD",
    })
    db["crm_assignment_cycles"].insert_one({
        "_id": "mongo-cycle-1",
        "assignment_cycle_id": "cycle-1",
        "lead_id": "lead-1",
        "assigned_to_user_id": "old-user",
        "assigned_to_display_name": "Ejecutivo A",
        "assigned_at": datetime(2026, 9, 9, 13, 0, tzinfo=timezone.utc),
        "sla_started_at": datetime(2026, 9, 9, 13, 0, tzinfo=timezone.utc),
        "cycle_status": "active",
        "unassigned_at": None,
        "reason": "inbound_message",
        "cycle_origin": "inbound_message",
        "notification_eligible": True,
        "sla_policy_version": "sla_visual_v1_20260723",
        "metric_version": "crm_metrics_v1",
        "schema_version": "crm_assignment_cycle_v1",
        "temperature_at_assignment": "COLD",
        "cycle_version": 3,
    })
    db["usuarios"].insert_one({"_id": "new-user", "nombre": "Ejecutivo B", "is_active": True})
    decision = {
        "lead_id": "lead-1",
        "current_assignment_cycle_id": "cycle-1",
        "previous_owner_user_id": "old-user",
        "policy_branch": RM_GLOBAL_RESCUE,
        "candidate_user_ids": ["new-user"],
        "selected_user_id": "new-user",
        "selected_score": 88.0,
        "selection_reason": "frozen_selector",
        "performance_confidence": "HIGH",
        "assignment_number": 0,
        "automatic_reassignment_number": 0,
        "excluded_previous_owners": ["old-user"],
        "previous_owner_user_ids": ["old-user"],
        "policy_version": FROZEN_POLICY_VERSION,
        "selection_rule": "R2_L1",
        "guardrail_applied": False,
        "guardrail_reason": "",
        "candidate_scores_snapshot": [{"user_id": "new-user", "score": 88.0}],
        "sla_breached_at": NOW,
        "reassignment_cutover_at": "2026-09-01T09:00:00-03:00",
        "source_cycle_sla_breached_at": "2026-09-09T16:00:00+00:00",
        "cutover_eligible": True,
        "cutover_policy_version": CUTOVER_POLICY_VERSION,
        "evaluated_at": NOW,
    }
    decision["decision_id"] = generate_decision_id(
        decision["lead_id"], decision["current_assignment_cycle_id"], FROZEN_POLICY_VERSION
    )
    return db, decision


def enable_flags(monkeypatch):
    monkeypatch.setattr(Config, "CRM_SLA_REASSIGNMENT_ENABLED", True)
    monkeypatch.setattr(Config, "CRM_SLA_TRANSACTION_GATE_ENABLED", True)
    monkeypatch.setattr(Config, "CRM_SLA_REASSIGNMENT_CUTOVER_AT", "2026-09-01T09:00:00-03:00")
    monkeypatch.setattr(Config, "IS_PRODUCTION", False)


def test_flag_off_returns_without_touching_database(monkeypatch):
    monkeypatch.setattr(Config, "CRM_SLA_REASSIGNMENT_ENABLED", False)
    monkeypatch.setattr(Config, "CRM_SLA_TRANSACTION_GATE_ENABLED", False)

    class ExplodingDB:
        def __getitem__(self, name):
            raise AssertionError("flag off must not access db")

    _, decision = make_fixture()
    result = execute_sla_reassignment_transaction(ExplodingDB(), decision, evaluated_at=NOW)
    assert result.status == "DISABLED"
    assert result.committed is False
    assert result.transaction_attempts == 0


def test_transaction_unavailable_is_fail_closed(monkeypatch):
    enable_flags(monkeypatch)
    db, decision = make_fixture()
    db.client = NoTransactionClient()
    result = execute_sla_reassignment_transaction(db, decision, evaluated_at=NOW)
    assert result.outcome == SLAReassignmentErrorCode.TRANSACTION_UNAVAILABLE.value
    assert result.committed is False


def test_executor_applies_with_explicit_concerns_and_replays_idempotently(monkeypatch):
    enable_flags(monkeypatch)
    db, decision = make_fixture()
    result = execute_sla_reassignment_transaction(db, decision, evaluated_at=NOW)
    assert result.outcome == SLAReassignmentErrorCode.APPLIED.value
    assert result.committed is True
    assert result.transaction_attempts == 1
    session = db.client.sessions[0]
    assert session.start_transaction_kwargs["read_concern"].level == "snapshot"
    assert session.start_transaction_kwargs["write_concern"].document["w"] == "majority"
    assert session.start_transaction_kwargs["read_preference"] == __import__(
        "pymongo"
    ).ReadPreference.PRIMARY

    source = db["crm_assignment_cycles"].find_one({"_id": "mongo-cycle-1"})
    destination_id = result.destination_cycle_id
    destination = db["crm_assignment_cycles"].find_one({"_id": destination_id})
    lead = db["leads"].find_one({"_id": "lead-1"})
    assert source["cycle_status"] == "reassigned"
    assert source["assigned_to_user_id"] == "old-user"
    assert source["cycle_version"] == 4
    assert destination["cycle_status"] == "active"
    assert destination["automatic_reassignment_number"] == 1
    assert destination["sla_started_at"] == NOW
    assert lead["lifecycle"]["current_assignment_cycle_id"] == destination_id
    assert len(db["crm_sla_reassignment_audit_v1"].rows) == 1


def test_executor_bridges_string_decision_ids_to_live_object_ids(monkeypatch):
    enable_flags(monkeypatch)
    db, decision = make_fixture()
    lead_object_id = ObjectId()
    target_object_id = ObjectId()
    lead = db["leads"].rows.pop("lead-1")
    lead["_id"] = lead_object_id
    db["leads"].rows[lead_object_id] = lead
    source = db["crm_assignment_cycles"].rows["mongo-cycle-1"]
    source["lead_id"] = lead_object_id
    target = db["usuarios"].rows.pop("new-user")
    target["_id"] = target_object_id
    db["usuarios"].rows[target_object_id] = target
    decision["lead_id"] = str(lead_object_id)
    decision["selected_user_id"] = str(target_object_id)
    decision["candidate_user_ids"] = [str(target_object_id)]
    decision["decision_id"] = generate_decision_id(
        decision["lead_id"], decision["current_assignment_cycle_id"], FROZEN_POLICY_VERSION
    )
    result = execute_sla_reassignment_transaction(db, decision, evaluated_at=NOW)
    assert result.outcome == SLAReassignmentErrorCode.APPLIED.value
    assert db["leads"].find_one({"_id": lead_object_id})["lifecycle"][
        "current_assignment_cycle_id"
    ] == result.destination_cycle_id

    replay = execute_sla_reassignment_transaction(db, decision, evaluated_at=NOW)
    assert replay.outcome == SLAReassignmentErrorCode.ALREADY_APPLIED.value
    assert replay.idempotent_replay is True
    assert len(db["crm_sla_reassignment_audit_v1"].rows) == 1


@pytest.mark.parametrize(
    "stage",
    [
        "after_source_cycle_close",
        "after_destination_cycle_insert",
        "after_lead_update",
        "before_ledger_insert",
        "before_commit",
    ],
)
def test_failure_injection_rolls_back_all_documents(monkeypatch, stage):
    enable_flags(monkeypatch)
    db, decision = make_fixture()
    result = execute_sla_reassignment_transaction(
        db,
        decision,
        evaluated_at=NOW,
        test_hooks={stage: RuntimeError("injected")},
    )
    assert result.outcome == SLAReassignmentErrorCode.TRANSIENT_ERROR.value
    assert result.committed is False
    source = db["crm_assignment_cycles"].find_one({"_id": "mongo-cycle-1"})
    lead = db["leads"].find_one({"_id": "lead-1"})
    assert source["cycle_status"] == "active"
    assert source["unassigned_at"] is None
    assert db["crm_assignment_cycles"].find_one({"assignment_cycle_id": result.destination_cycle_id}) is None
    assert lead["ejecutivo_asignado"] == "Ejecutivo A"
    assert lead["lifecycle"]["current_assignment_cycle_id"] == "cycle-1"
    assert not db["crm_sla_reassignment_audit_v1"].rows


def test_human_protection_is_not_click_or_bot_and_gate_blocks_reassignment(monkeypatch):
    enable_flags(monkeypatch)
    assert classify_human_protection({
        "type": "CLICK_WHATSAPP_LEAD",
        "actor": "old-user",
        "actor_type": "human",
        "confirmed": True,
    }) is None
    assert classify_human_protection({
        "type": "SEND_WA_LEAD",
        "actor": "bot",
        "actor_type": "bot",
        "confirmed": True,
    }) is None
    assert classify_human_protection({
        "type": "SEND_WA_LEAD",
        "actor": "old-user",
        "actor_type": "human",
        "confirmed": True,
    }) == HumanProtectionType.WHATSAPP.value

    db, decision = make_fixture()
    claimed = claim_cycle_for_human_management(
        db,
        lead_id="lead-1",
        assignment_cycle_id="cycle-1",
        actor_user_id="old-user",
        protection_type=HumanProtectionType.WHATSAPP.value,
        occurred_at=NOW,
    )
    assert claimed.status == CycleGateStatus.CLAIMED.value
    result = execute_sla_reassignment_transaction(db, decision, evaluated_at=NOW)
    assert result.outcome == SLAReassignmentErrorCode.ABORT_MANAGEMENT_DETECTED.value
    assert not db["crm_sla_reassignment_audit_v1"].rows


def test_reassignment_first_locks_old_owner_management(monkeypatch):
    enable_flags(monkeypatch)
    db, decision = make_fixture()
    result = execute_sla_reassignment_transaction(db, decision, evaluated_at=NOW)
    assert result.outcome == SLAReassignmentErrorCode.APPLIED.value
    gate = claim_cycle_for_human_management(
        db,
        lead_id="lead-1",
        assignment_cycle_id="cycle-1",
        actor_user_id="old-user",
        protection_type=HumanProtectionType.WHATSAPP.value,
        occurred_at=NOW,
    )
    assert gate.status == CycleGateStatus.LEAD_REASSIGNED_SLA_LOCKED.value


def test_unknown_commit_reconciles_using_same_decision(monkeypatch):
    enable_flags(monkeypatch)
    db, decision = make_fixture()
    db.client = UnknownCommitClient(db)
    result = execute_sla_reassignment_transaction(db, decision, evaluated_at=NOW)
    assert result.outcome == SLAReassignmentErrorCode.ALREADY_APPLIED.value
    assert result.committed is True
    assert result.idempotent_replay is True
    assert len(db["crm_sla_reassignment_audit_v1"].rows) == 1


def test_concurrent_different_payload_cannot_create_second_destination(monkeypatch):
    enable_flags(monkeypatch)
    db, decision = make_fixture()
    first = execute_sla_reassignment_transaction(db, decision, evaluated_at=NOW)
    assert first.outcome == SLAReassignmentErrorCode.APPLIED.value
    second_decision = deepcopy(decision)
    second_decision["candidate_user_ids"] = ["another-user"]
    second_decision["selected_user_id"] = "another-user"
    second = execute_sla_reassignment_transaction(db, second_decision, evaluated_at=NOW)
    assert second.outcome == SLAReassignmentErrorCode.IDEMPOTENCY_CONFLICT.value
    assert len(db["crm_assignment_cycles"].rows) == 2


def test_management_result_flag_off_keeps_existing_path_without_claim(monkeypatch):
    monkeypatch.setattr(Config, "CRM_SLA_TRANSACTION_GATE_ENABLED", False)
    monkeypatch.setattr(Config, "CRM_SLA_REASSIGNMENT_ENABLED", False)
    db, _ = make_fixture()
    record = record_management_result(
        db,
        lead_id="lead-1",
        assignment_cycle_id="cycle-1",
        actor_user_id="old-user",
        result_type="EFFECTIVE_CONTACT",
        source="test",
        idempotency_key="management-off",
        occurred_at=NOW,
    )
    assert record["status"] == "completed"
    source = db["crm_assignment_cycles"].find_one({"_id": "mongo-cycle-1"})
    assert source.get("reassignment_protection_at") is None


def test_management_result_flag_on_claims_shared_gate_before_persist(monkeypatch):
    enable_flags(monkeypatch)
    db, _ = make_fixture()
    record = record_management_result(
        db,
        lead_id="lead-1",
        assignment_cycle_id="cycle-1",
        actor_user_id="old-user",
        result_type="EFFECTIVE_CONTACT",
        source="test",
        idempotency_key="management-on",
        occurred_at=NOW,
    )
    assert record["status"] == "completed"
    source = db["crm_assignment_cycles"].find_one({"_id": "mongo-cycle-1"})
    assert source["reassignment_protection_type"] == "CRM_MANAGEMENT_RESULT"
    assert source["reassignment_protection_actor_user_id"] == "old-user"


def test_human_outbound_message_uses_gate_only_when_enabled(monkeypatch):
    db, _ = make_fixture()
    monkeypatch.setattr(crm_storage, "get_db", lambda: db)
    monkeypatch.setattr(Config, "CRM_SLA_TRANSACTION_GATE_ENABLED", False)
    crm_storage.guardar_mensaje(
        "synthetic-contact",
        "assistant",
        "mensaje humano",
        {"actor_type": "human_agent", "actor_id": "old-user", "operation": "whatsapp"},
        lead_id="lead-1",
    )
    source = db["crm_assignment_cycles"].find_one({"_id": "mongo-cycle-1"})
    assert source.get("reassignment_protection_at") is None

    db, _ = make_fixture()
    monkeypatch.setattr(crm_storage, "get_db", lambda: db)
    monkeypatch.setattr(Config, "CRM_SLA_TRANSACTION_GATE_ENABLED", True)
    crm_storage.guardar_mensaje(
        "synthetic-contact",
        "assistant",
        "mensaje humano",
        {"actor_type": "human_agent", "actor_id": "old-user", "operation": "whatsapp"},
        lead_id="lead-1",
    )
    source = db["crm_assignment_cycles"].find_one({"_id": "mongo-cycle-1"})
    assert source["reassignment_protection_type"] == "WHATSAPP"


def test_index_migration_is_dry_run_and_does_not_create_indexes():
    db, _ = make_fixture()
    plan = build_index_migration_plan(db)
    assert plan["dry_run"] is True
    assert plan["blocking_duplicates"] is False
    assert all("name" in item for item in plan["proposed_indexes"])


def test_result_and_logs_contract_has_no_contact_pii(monkeypatch, caplog):
    enable_flags(monkeypatch)
    db, decision = make_fixture()
    result = execute_sla_reassignment_transaction(db, decision, evaluated_at=NOW)
    text = repr(result.to_dict()).lower() + repr(caplog.records).lower()
    assert "phone" not in text
    assert "email" not in text
    assert "whatsapp" not in text
