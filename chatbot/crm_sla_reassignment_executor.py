"""Opt-in MongoDB executor for one already-calculated SLA reassignment.

The executor is intentionally not imported by any worker or startup path.  It
requires both reassignment and gate flags to be enabled, so the default
production behavior remains unchanged.  All critical reads/writes in the
transaction callback receive the same PyMongo session.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import logging
import time
from typing import Any, Callable, Mapping, MutableMapping

from bson import ObjectId
from pymongo import ReadPreference
from pymongo.errors import (
    ConfigurationError,
    DuplicateKeyError,
    InvalidOperation,
    OperationFailure,
    PyMongoError,
)
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from config import Config

from .crm_assignment_cycle_gate import classify_human_protection, protection_type_for_management_result
from .crm_metrics import calculate_sla, commercial_sla_start_at, coerce_utc_datetime, utc_now
from .crm_sla_hybrid_stabilization import generate_decision_id
from .crm_sla_hybrid_rescue import REGION_JPC_MARIA_HERNAN, RM_GLOBAL_RESCUE
from .crm_sla_reassignment_cutover import (
    CUTOVER_CONFIGURATION_INVALID,
    CUTOVER_NOT_CONFIGURED,
    CUTOVER_POLICY_VERSION,
    CUTOVER_ELIGIBLE,
    PRE_CUTOVER_ALREADY_EXPIRED,
    canonical_sla_breached_at,
    cutover_decision_validation,
    evaluate_cutover,
)
from .crm_sla_reassignment_models import SLAReassignmentErrorCode, SLAReassignmentResult
from .crm_sla_reassignment_transaction import (
    REASSIGNMENT_AUDIT_COLLECTION,
    TransactionContractError,
    build_transaction_plan,
    decision_new_cycle_id,
)


logger = logging.getLogger(__name__)

FROZEN_POLICY_VERSION = "crm_sla_reassignment_v1"
ALLOWED_POLICY_BRANCHES = frozenset({RM_GLOBAL_RESCUE, REGION_JPC_MARIA_HERNAN})
MAX_TRANSACTION_ATTEMPTS = 3
HUMAN_ACTOR_TYPES = frozenset({"human", "human_agent", "agent", "administrator", "supervisor"})
PROTECTED_CYCLE_FIELDS = (
    "reassignment_protection_at",
    "first_contact_attempt_at",
    "first_valid_management_at",
)


class _KnownAbort(Exception):
    def __init__(self, code: SLAReassignmentErrorCode, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


class _AlreadyApplied(Exception):
    def __init__(self, event: Mapping[str, Any]):
        super().__init__("already_applied")
        self.event = event


class _TransactionUnavailable(Exception):
    pass


def _decision_mapping(decision: Any) -> dict[str, Any]:
    if isinstance(decision, Mapping):
        return dict(decision)
    if hasattr(decision, "to_dict"):
        value = decision.to_dict()
        if isinstance(value, Mapping):
            return dict(value)
    if is_dataclass(decision):
        return dict(asdict(decision))
    raise _KnownAbort(
        SLAReassignmentErrorCode.IDEMPOTENCY_CONFLICT,
        "decision_not_structured",
    )


def _id(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None or str(value).strip() == "":
        raise _KnownAbort(SLAReassignmentErrorCode.IDEMPOTENCY_CONFLICT, f"missing_{key}")
    return str(value)


def _source_assignment_number(decision: Mapping[str, Any]) -> int:
    value = decision.get(
        "automatic_reassignment_number", decision.get("assignment_number")
    )
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _KnownAbort(
            SLAReassignmentErrorCode.ABORT_ASSIGNMENT_LIMIT_REACHED,
            "assignment_number_invalid",
        )


def _destination_cycle_id(decision: Mapping[str, Any]) -> str:
    return decision_new_cycle_id(_id(decision, "decision_id"))


def _result(
    decision: Mapping[str, Any],
    *,
    status: str,
    outcome: str,
    attempts: int,
    committed: bool,
    idempotent_replay: bool = False,
    committed_at: datetime | None = None,
    abort_reason: str | None = None,
    error_code: str | None = None,
) -> SLAReassignmentResult:
    source_count = None
    try:
        source_count = _source_assignment_number(decision)
    except _KnownAbort:
        pass
    return SLAReassignmentResult(
        decision_id=str(decision.get("decision_id") or ""),
        lead_id=str(decision.get("lead_id") or ""),
        source_cycle_id=str(decision.get("current_assignment_cycle_id") or ""),
        destination_cycle_id=_destination_cycle_id(decision) if decision.get("decision_id") else "",
        previous_owner_user_id=(
            str(decision["previous_owner_user_id"])
            if decision.get("previous_owner_user_id") is not None
            else None
        ),
        selected_user_id=(
            str(decision["selected_user_id"])
            if decision.get("selected_user_id") is not None
            else None
        ),
        status=status,
        outcome=outcome,
        committed=bool(committed),
        idempotent_replay=bool(idempotent_replay),
        policy_version=str(decision.get("policy_version") or ""),
        automatic_reassignment_number=(source_count + 1 if source_count in (0, 1) else source_count),
        transaction_attempts=int(attempts),
        committed_at=committed_at,
        abort_reason=abort_reason,
        error_code=error_code,
    )


def _log_result(
    result: SLAReassignmentResult,
    *,
    branch: str = "",
    transaction_duration_ms: float = 0.0,
) -> None:
    logger.info(
        "[CRM_SLA_REASSIGNMENT] decision_id=%s lead_id=%s source_cycle_id=%s "
        "destination_cycle_id=%s branch=%s previous_owner_user_id=%s "
        "selected_user_id=%s outcome=%s attempt=%s committed=%s "
        "transaction_duration_ms=%.1f policy_version=%s",
        result.decision_id,
        result.lead_id,
        result.source_cycle_id,
        result.destination_cycle_id,
        branch,
        result.previous_owner_user_id,
        result.selected_user_id,
        result.outcome,
        result.transaction_attempts,
        result.committed,
        float(transaction_duration_ms),
        result.policy_version,
    )


def record_reassignment_metric(
    metrics: MutableMapping[str, int] | None, result: SLAReassignmentResult
) -> None:
    """Increment an in-process hook only; no provider or collection is used."""

    if metrics is None:
        return
    if result.status != "DISABLED":
        metrics["attempted"] = int(metrics.get("attempted", 0)) + 1
    key_by_outcome = {
        SLAReassignmentErrorCode.ABORT_MANAGEMENT_DETECTED.value: "aborted_management",
        SLAReassignmentErrorCode.ABORT_CYCLE_CHANGED.value: "aborted_cycle",
        SLAReassignmentErrorCode.ABORT_CYCLE_CLOSED.value: "aborted_cycle",
        SLAReassignmentErrorCode.ABORT_OWNER_CHANGED.value: "aborted_owner",
        SLAReassignmentErrorCode.TRANSIENT_ERROR.value: "transient_error",
        SLAReassignmentErrorCode.SUPERVISOR_REVIEW_REQUIRED.value: "supervisor_review",
        SLAReassignmentErrorCode.PRE_CUTOVER_ALREADY_EXPIRED.value: "pre_cutover_expired_skipped",
        SLAReassignmentErrorCode.CUTOVER_NOT_CONFIGURED.value: "cutover_configuration_error",
        SLAReassignmentErrorCode.CUTOVER_CONFIGURATION_INVALID.value: "cutover_configuration_error",
    }
    key = key_by_outcome.get(result.outcome)
    if key is None and result.outcome == SLAReassignmentErrorCode.APPLIED.value:
        key = "applied"
    if key is None and result.outcome == SLAReassignmentErrorCode.ALREADY_APPLIED.value:
        key = "idempotent"
    if key is None:
        key = "attempted" if result.status != "DISABLED" else None
    if key:
        metrics[key] = int(metrics.get(key, 0)) + 1


def _prevalidate_decision(decision: Mapping[str, Any]) -> None:
    policy_version = str(decision.get("policy_version") or "")
    if policy_version != FROZEN_POLICY_VERSION:
        raise _KnownAbort(
            SLAReassignmentErrorCode.ABORT_POLICY_VERSION_CHANGED,
            "policy_version_mismatch",
        )
    cutover = cutover_decision_validation(
        decision,
        configured_cutover_at=getattr(Config, "CRM_SLA_REASSIGNMENT_CUTOVER_AT", None),
    )
    if not cutover.eligible:
        try:
            code = SLAReassignmentErrorCode(cutover.outcome)
        except ValueError:
            code = SLAReassignmentErrorCode.CUTOVER_CONFIGURATION_INVALID
        raise _KnownAbort(code, cutover.reason or cutover.outcome)
    lead_id = _id(decision, "lead_id")
    cycle_id = _id(decision, "current_assignment_cycle_id")
    decision_id = _id(decision, "decision_id")
    expected_id = generate_decision_id(lead_id, cycle_id, FROZEN_POLICY_VERSION)
    if decision_id != expected_id:
        raise _KnownAbort(
            SLAReassignmentErrorCode.IDEMPOTENCY_CONFLICT,
            "decision_id_not_deterministic",
        )
    branch = str(decision.get("policy_branch") or "")
    if branch not in ALLOWED_POLICY_BRANCHES:
        raise _KnownAbort(
            SLAReassignmentErrorCode.ABORT_SELECTED_USER_INELIGIBLE,
            "policy_branch_not_allowed",
        )
    selected = _id(decision, "selected_user_id")
    candidates = {str(item) for item in (decision.get("candidate_user_ids") or [])}
    if selected not in candidates:
        raise _KnownAbort(
            SLAReassignmentErrorCode.ABORT_SELECTED_USER_INELIGIBLE,
            "selected_user_not_in_decision_candidates",
        )
    count = _source_assignment_number(decision)
    if not 0 <= count < 2:
        raise _KnownAbort(
            SLAReassignmentErrorCode.ABORT_ASSIGNMENT_LIMIT_REACHED,
            "assignment_limit_reached",
        )
    if bool(decision.get("requires_supervisor_review")):
        raise _KnownAbort(
            SLAReassignmentErrorCode.SUPERVISOR_REVIEW_REQUIRED,
            str(decision.get("review_reason") or "supervisor_review_required"),
        )


def _same_ledger_payload(event: Mapping[str, Any], decision: Mapping[str, Any]) -> bool:
    return all(
        str(event.get(event_key) or "") == str(decision.get(decision_key) or "")
        for event_key, decision_key in (
            ("decision_id", "decision_id"),
            ("lead_id", "lead_id"),
            ("source_cycle_id", "current_assignment_cycle_id"),
            ("target_owner_user_id", "selected_user_id"),
            ("policy_version", "policy_version"),
        )
    )


def _existing_result(
    decision: Mapping[str, Any], event: Mapping[str, Any], *, attempts: int
) -> SLAReassignmentResult:
    result = _result(
        decision,
        status="ALREADY_APPLIED",
        outcome=SLAReassignmentErrorCode.ALREADY_APPLIED.value,
        attempts=attempts,
        committed=True,
        idempotent_replay=True,
        committed_at=coerce_utc_datetime(event.get("created_at") or event.get("reassigned_at")),
        error_code=SLAReassignmentErrorCode.ALREADY_APPLIED.value,
    )
    _log_result(result, branch=str(decision.get("policy_branch") or ""))
    return result


def _transaction_capable(db: Any) -> Any:
    client = getattr(db, "client", None)
    if client is None or not callable(getattr(client, "start_session", None)):
        raise _TransactionUnavailable("client_session_unavailable")
    admin = getattr(client, "admin", None)
    command = getattr(admin, "command", None)
    if callable(command):
        try:
            hello = command({"hello": 1}) or {}
        except Exception as exc:
            raise _TransactionUnavailable("hello_failed") from exc
        # Replica set and sharded deployments support multi-document
        # transactions.  A standalone server must fail closed.
        if not hello.get("setName") and hello.get("msg") != "isdbgrid":
            raise _TransactionUnavailable("transaction_topology_unsupported")
    return client


def _find_one(collection: Any, filter_: Mapping[str, Any], *, session: Any) -> Any:
    return collection.find_one(filter_, session=session)


def _mongo_id_variants(value: Any) -> tuple[Any, ...]:
    """Try the decision's transport ID and its BSON ObjectId form."""

    variants: list[Any] = [value]
    if isinstance(value, str) and ObjectId.is_valid(value):
        variants.append(ObjectId(value))
    return tuple(variants)


def _open_lead(lead: Mapping[str, Any]) -> bool:
    if lead.get("closed_at") is not None or lead.get("archived_at") is not None:
        return False
    if lead.get("suppressed") is True:
        return False
    closed_values = {"CLOSED", "CLOSED_WON", "CLOSED_LOST", "ARCHIVED", "SUPPRESSED"}
    return not any(
        str(lead.get(field) or "").upper() in closed_values
        for field in ("stage", "pipeline_stage", "status")
    )


def _cycle_field_protection(cycle: Mapping[str, Any], lead: Mapping[str, Any]) -> tuple[str, Any] | None:
    for field in PROTECTED_CYCLE_FIELDS:
        value = cycle.get(field)
        if value not in (None, ""):
            return field, value
    lifecycle = lead.get("lifecycle") if isinstance(lead.get("lifecycle"), Mapping) else {}
    for field in ("first_contact_attempt_at", "first_valid_management_at"):
        value = lifecycle.get(field)
        if value not in (None, ""):
            return f"lifecycle.{field}", value
    return None


def _human_evidence_in_collections(
    db: Any, *, lead_id: str, cycle_id: str, assigned_at: Any, session: Any
) -> tuple[str, Any] | None:
    management = _find_one(
        db["crm_management_results"],
        {"lead_id": lead_id, "assignment_cycle_id": cycle_id},
        session=session,
    )
    if management:
        protection = protection_type_for_management_result(
            management.get("result_type") or management.get("result")
        )
        if protection:
            return protection, management.get("occurred_at")

    events = _find_one(
        db["crm_events"],
        {
            "lead_id": lead_id,
            "assignment_cycle_id": cycle_id,
            "confirmed": True,
            "actor_type": {"$in": list(HUMAN_ACTOR_TYPES)},
        },
        session=session,
    )
    if events:
        protection = classify_human_protection(events)
        if protection:
            return protection, events.get("timestamp") or events.get("occurred_at")

    # Human WhatsApp messages are recorded in the separate append-only
    # conversation model.  They protect the cycle but do not become SLA stop
    # results here.
    conversation_filter: dict[str, Any] = {
        "lead_id": str(lead_id),
        "actor_type": "human_agent",
        "event_type": {"$in": ["human_message_sent", "human_outreach"]},
    }
    assigned = coerce_utc_datetime(assigned_at)
    if assigned:
        conversation_filter["timestamp"] = {"$gte": assigned}
    conversation_event = _find_one(
        db["conversation_events"],
        conversation_filter,
        session=session,
    )
    if conversation_event:
        return "WHATSAPP", conversation_event.get("timestamp")
    return None


def _revalidate(
    db: Any,
    decision: Mapping[str, Any],
    *,
    session: Any,
    evaluated_at: datetime,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    lead_id = _id(decision, "lead_id")
    cycle_id = _id(decision, "current_assignment_cycle_id")
    selected_id = _id(decision, "selected_user_id")
    lead = None
    for lead_key in _mongo_id_variants(lead_id):
        lead = _find_one(db["leads"], {"_id": lead_key}, session=session)
        if lead:
            break
    if not lead:
        raise _KnownAbort(SLAReassignmentErrorCode.ABORT_LEAD_CLOSED, "lead_not_found")
    source = _find_one(
        db["crm_assignment_cycles"],
        {"assignment_cycle_id": cycle_id},
        session=session,
    )
    if not source:
        raise _KnownAbort(SLAReassignmentErrorCode.ABORT_CYCLE_CHANGED, "source_cycle_not_found")
    if str(source.get("lead_id")) != lead_id:
        raise _KnownAbort(SLAReassignmentErrorCode.ABORT_CYCLE_CHANGED, "source_cycle_lead_changed")
    if source.get("cycle_status") != "active" or source.get("unassigned_at") is not None:
        raise _KnownAbort(SLAReassignmentErrorCode.ABORT_CYCLE_CLOSED, "source_cycle_not_active")
    if str(source.get("assigned_to_user_id")) != str(decision.get("previous_owner_user_id")):
        raise _KnownAbort(SLAReassignmentErrorCode.ABORT_OWNER_CHANGED, "source_owner_changed")
    if not _open_lead(lead):
        raise _KnownAbort(SLAReassignmentErrorCode.ABORT_LEAD_CLOSED, "lead_not_open")
    lifecycle = lead.get("lifecycle") if isinstance(lead.get("lifecycle"), Mapping) else {}
    if str(lifecycle.get("current_assignment_cycle_id")) != cycle_id:
        raise _KnownAbort(SLAReassignmentErrorCode.ABORT_POINTER_MISMATCH, "lead_cycle_pointer_changed")
    source_policy = str(source.get("sla_policy_version") or "")
    if source_policy != "sla_visual_v1_20260723":
        raise _KnownAbort(SLAReassignmentErrorCode.ABORT_POLICY_VERSION_CHANGED, "source_policy_changed")

    # Future-only cutover is revalidated from the canonical source-cycle
    # breach timestamp inside the transaction.  The decision timestamp must
    # match the source reconstruction; otherwise the source changed after
    # eligibility evaluation.
    canonical_breach = canonical_sla_breached_at(source, lead=lead)
    current_cutover = evaluate_cutover(
        breached_at=canonical_breach,
        cutover_at=getattr(Config, "CRM_SLA_REASSIGNMENT_CUTOVER_AT", None),
    )
    decision_cutover = cutover_decision_validation(
        decision,
        configured_cutover_at=getattr(Config, "CRM_SLA_REASSIGNMENT_CUTOVER_AT", None),
    )
    if not current_cutover.eligible:
        try:
            code = SLAReassignmentErrorCode(current_cutover.outcome)
        except ValueError:
            code = SLAReassignmentErrorCode.CUTOVER_CONFIGURATION_INVALID
        raise _KnownAbort(code, current_cutover.reason or current_cutover.outcome)
    if not decision_cutover.eligible:
        try:
            code = SLAReassignmentErrorCode(decision_cutover.outcome)
        except ValueError:
            code = SLAReassignmentErrorCode.CUTOVER_CONFIGURATION_INVALID
        raise _KnownAbort(code, decision_cutover.reason or decision_cutover.outcome)
    if current_cutover.breached_at != decision_cutover.breached_at:
        raise _KnownAbort(
            SLAReassignmentErrorCode.ABORT_CYCLE_CHANGED,
            "source_cycle_sla_breached_at_changed",
        )

    source_count = _source_assignment_number(decision)
    actual_count = source.get("automatic_reassignment_number")
    if actual_count is not None and int(actual_count) != source_count:
        raise _KnownAbort(SLAReassignmentErrorCode.ABORT_CYCLE_CHANGED, "assignment_number_changed")
    if actual_count is not None and int(actual_count) >= 2:
        raise _KnownAbort(SLAReassignmentErrorCode.ABORT_ASSIGNMENT_LIMIT_REACHED, "assignment_limit_reached")
    if source.get("reassignment_decision_id"):
        raise _KnownAbort(SLAReassignmentErrorCode.ABORT_CYCLE_CHANGED, "source_already_reassigned")

    field_protection = _cycle_field_protection(source, lead)
    if field_protection:
        raise _KnownAbort(
            SLAReassignmentErrorCode.ABORT_MANAGEMENT_DETECTED,
            f"human_protection:{field_protection[0]}",
        )
    collection_protection = _human_evidence_in_collections(
        db,
        lead_id=lead_id,
        cycle_id=cycle_id,
        assigned_at=source.get("assigned_at"),
        session=session,
    )
    if collection_protection:
        raise _KnownAbort(
            SLAReassignmentErrorCode.ABORT_MANAGEMENT_DETECTED,
            f"human_protection:{collection_protection[0]}",
        )

    temperature = str(
        lead.get("lead_temperature_effective")
        or source.get("temperature_at_assignment")
        or "COLD"
    ).upper()
    sla = calculate_sla(
        assigned_at=source.get("assigned_at"),
        first_valid_management_at=source.get("first_valid_management_at"),
        now=evaluated_at,
        temperature=temperature,
        hot_started_at=source.get("hot_started_at"),
    )
    if sla.get("status") != "critical":
        raise _KnownAbort(SLAReassignmentErrorCode.ABORT_SLA_NOT_EXPIRED, "sla_not_expired")

    target = None
    for target_key in _mongo_id_variants(selected_id):
        target = _find_one(db["usuarios"], {"_id": target_key}, session=session)
        if target:
            break
    if not target or target.get("is_active") is not True:
        raise _KnownAbort(
            SLAReassignmentErrorCode.ABORT_SELECTED_USER_INACTIVE,
            "selected_user_inactive",
        )
    if selected_id not in {str(item) for item in (decision.get("candidate_user_ids") or [])}:
        raise _KnownAbort(
            SLAReassignmentErrorCode.ABORT_SELECTED_USER_INELIGIBLE,
            "selected_user_not_allowed_by_decision",
        )
    previous = {
        str(value)
        for value in (
            decision.get("previous_owner_user_ids")
            or decision.get("excluded_previous_owners")
            or [decision.get("previous_owner_user_id")]
        )
        if value not in (None, "")
    }
    if selected_id in previous or selected_id == str(source.get("assigned_to_user_id")):
        raise _KnownAbort(
            SLAReassignmentErrorCode.ABORT_PREVIOUS_OWNER,
            "selected_user_in_previous_owner_history",
        )
    return lead, source, target


def _invoke_test_hook(test_hooks: Any, stage: str) -> None:
    if not test_hooks:
        return
    hook = test_hooks.get(stage) if isinstance(test_hooks, Mapping) else test_hooks
    if hook is None:
        return
    if isinstance(hook, BaseException):
        raise hook
    if callable(hook):
        hook(stage)


def _apply_plan(db: Any, plan: Any, *, session: Any, test_hooks: Any = None) -> None:
    for operation in plan.operations:
        collection = db[operation.collection]
        if operation.operation == "update_one":
            response = collection.update_one(
                operation.filter, operation.update, session=session
            )
            if int(getattr(response, "matched_count", 0)) != 1:
                if operation.collection == "crm_assignment_cycles":
                    raise _KnownAbort(
                        SLAReassignmentErrorCode.ABORT_CYCLE_CHANGED,
                        "source_cycle_cas_failed",
                    )
                raise _KnownAbort(
                    SLAReassignmentErrorCode.ABORT_POINTER_MISMATCH,
                    "lead_cas_failed",
                )
        elif operation.operation == "insert_one":
            collection.insert_one(operation.document, session=session)
        else:
            raise _KnownAbort(
                SLAReassignmentErrorCode.TRANSIENT_ERROR,
                "unsupported_transaction_operation",
            )
        if operation is plan.operations[0]:
            _invoke_test_hook(test_hooks, "after_source_cycle_close")
        elif operation is plan.operations[1]:
            _invoke_test_hook(test_hooks, "after_destination_cycle_insert")
        elif operation is plan.operations[2]:
            _invoke_test_hook(test_hooks, "after_lead_update")
        elif operation is plan.operations[3]:
            _invoke_test_hook(test_hooks, "before_ledger_insert")
    _invoke_test_hook(test_hooks, "before_commit")


def _abort_transaction(session: Any) -> None:
    try:
        session.abort_transaction()
    except Exception:
        logger.warning("[CRM_SLA_REASSIGNMENT] transaction abort failed", exc_info=True)


def _error_labels(exc: BaseException) -> set[str]:
    labels = getattr(exc, "_error_labels", None)
    if labels is None and hasattr(exc, "has_error_label"):
        labels = {
            label
            for label in ("TransientTransactionError", "UnknownTransactionCommitResult")
            if exc.has_error_label(label)
        }
    return set(labels or ())


def _is_transaction_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, (ConfigurationError, InvalidOperation)):
        return True
    text = str(exc).lower()
    return isinstance(exc, OperationFailure) and any(
        phrase in text
        for phrase in (
            "transaction numbers are only allowed",
            "does not support transactions",
            "transaction is not supported",
        )
    )


def _unknown_commit_resolution(
    db: Any, decision: Mapping[str, Any]
) -> SLAReassignmentResult | None:
    event = db[REASSIGNMENT_AUDIT_COLLECTION].find_one(
        {"_id": decision.get("decision_id")}
    )
    if event:
        if _same_ledger_payload(event, decision):
            return _existing_result(decision, event, attempts=1)
        return _result(
            decision,
            status="ABORTED",
            outcome=SLAReassignmentErrorCode.IDEMPOTENCY_CONFLICT.value,
            attempts=1,
            committed=False,
            abort_reason="ledger_payload_conflict_after_unknown_commit",
            error_code=SLAReassignmentErrorCode.IDEMPOTENCY_CONFLICT.value,
        )
    source = db["crm_assignment_cycles"].find_one(
        {
            "assignment_cycle_id": decision.get("current_assignment_cycle_id"),
            "reassignment_decision_id": decision.get("decision_id"),
        }
    )
    destination = db["crm_assignment_cycles"].find_one(
        {"assignment_cycle_id": _destination_cycle_id(decision)}
    )
    if source or destination:
        return _result(
            decision,
            status="ABORTED",
            outcome=SLAReassignmentErrorCode.IDEMPOTENCY_CONFLICT.value,
            attempts=1,
            committed=False,
            abort_reason="contradictory_state_after_unknown_commit",
            error_code=SLAReassignmentErrorCode.IDEMPOTENCY_CONFLICT.value,
        )
    return None


def execute_sla_reassignment_transaction(
    db: Any,
    decision: Any,
    *,
    evaluated_at: datetime | None = None,
    max_attempts: int = MAX_TRANSACTION_ATTEMPTS,
    test_hooks: Any = None,
    metrics: MutableMapping[str, int] | None = None,
    metrics_hook: Callable[[Mapping[str, Any]], None] | None = None,
) -> SLAReassignmentResult:
    """Execute one preselected A→B reassignment when both flags are ON.

    The function never chooses a candidate.  It can be called explicitly by a
    controlled internal test or future service, but no worker imports it in
    this phase.
    """

    decision_map: dict[str, Any]
    try:
        decision_map = _decision_mapping(decision)
    except _KnownAbort as exc:
        result = _result(
            {"decision_id": "", "lead_id": "", "current_assignment_cycle_id": ""},
            status="ABORTED",
            outcome=exc.code.value,
            attempts=0,
            committed=False,
            abort_reason=exc.reason,
            error_code=exc.code.value,
        )
        record_reassignment_metric(metrics, result)
        return result

    if not getattr(Config, "CRM_SLA_REASSIGNMENT_ENABLED", False) or not getattr(
        Config, "CRM_SLA_TRANSACTION_GATE_ENABLED", False
    ):
        result = _result(
            decision_map,
            status="DISABLED",
            outcome="FEATURE_DISABLED",
            attempts=0,
            committed=False,
        )
        record_reassignment_metric(metrics, result)
        return result
    if test_hooks and getattr(Config, "IS_PRODUCTION", False):
        result = _result(
            decision_map,
            status="ABORTED",
            outcome=SLAReassignmentErrorCode.TRANSIENT_ERROR.value,
            attempts=0,
            committed=False,
            abort_reason="test_hooks_forbidden_in_production",
            error_code=SLAReassignmentErrorCode.TRANSIENT_ERROR.value,
        )
        record_reassignment_metric(metrics, result)
        return result

    max_attempts = max(1, min(int(max_attempts), MAX_TRANSACTION_ATTEMPTS))
    try:
        _prevalidate_decision(decision_map)
    except _KnownAbort as exc:
        result = _result(
            decision_map,
            status="ABORTED",
            outcome=exc.code.value,
            attempts=0,
            committed=False,
            abort_reason=exc.reason,
            error_code=exc.code.value,
        )
        _log_result(result, branch=str(decision_map.get("policy_branch") or ""))
        record_reassignment_metric(metrics, result)
        return result

    if metrics is not None:
        metrics["post_cutover_expired_evaluated"] = int(
            metrics.get("post_cutover_expired_evaluated", 0)
        ) + 1

    # Idempotency precheck is only an optimization; the authoritative check is
    # repeated inside the transaction callback.
    existing = db[REASSIGNMENT_AUDIT_COLLECTION].find_one(
        {"_id": decision_map.get("decision_id")}
    )
    if existing:
        if _same_ledger_payload(existing, decision_map):
            result = _existing_result(decision_map, existing, attempts=0)
        else:
            result = _result(
                decision_map,
                status="ABORTED",
                outcome=SLAReassignmentErrorCode.IDEMPOTENCY_CONFLICT.value,
                attempts=0,
                committed=False,
                abort_reason="ledger_payload_conflict",
                error_code=SLAReassignmentErrorCode.IDEMPOTENCY_CONFLICT.value,
            )
        record_reassignment_metric(metrics, result)
        return result

    try:
        client = _transaction_capable(db)
    except _TransactionUnavailable as exc:
        result = _result(
            decision_map,
            status="ABORTED",
            outcome=SLAReassignmentErrorCode.TRANSACTION_UNAVAILABLE.value,
            attempts=0,
            committed=False,
            abort_reason=str(exc),
            error_code=SLAReassignmentErrorCode.TRANSACTION_UNAVAILABLE.value,
        )
        _log_result(result, branch=str(decision_map.get("policy_branch") or ""))
        record_reassignment_metric(metrics, result)
        return result

    started_at = evaluated_at and coerce_utc_datetime(evaluated_at) or utc_now()
    transaction_started_clock = time.perf_counter()
    final_result: SLAReassignmentResult | None = None
    try:
        with client.start_session() as session:
            for attempt in range(1, max_attempts + 1):
                try:
                    session.start_transaction(
                        read_concern=ReadConcern("snapshot"),
                        write_concern=WriteConcern("majority"),
                        read_preference=ReadPreference.PRIMARY,
                    )
                    inside_event = db[REASSIGNMENT_AUDIT_COLLECTION].find_one(
                        {"_id": decision_map.get("decision_id")}, session=session
                    )
                    if inside_event:
                        if _same_ledger_payload(inside_event, decision_map):
                            raise _AlreadyApplied(inside_event)
                        raise _KnownAbort(
                            SLAReassignmentErrorCode.IDEMPOTENCY_CONFLICT,
                            "ledger_payload_conflict",
                        )
                    lead, source, target = _revalidate(
                        db,
                        decision_map,
                        session=session,
                        evaluated_at=started_at,
                    )
                    new_sla_started_at = commercial_sla_start_at(started_at)
                    if not new_sla_started_at:
                        raise _KnownAbort(
                            SLAReassignmentErrorCode.ABORT_SLA_NOT_EXPIRED,
                            "new_sla_start_unavailable",
                        )
                    plan = build_transaction_plan(
                        decision=decision_map,
                        source_cycle=source,
                        lead=lead,
                        target_user=target,
                        reassigned_at=started_at,
                        effective_sla_started_at=new_sla_started_at,
                        expected_sla_policy_version=str(
                            source.get("sla_policy_version") or ""
                        ),
                    )
                    _apply_plan(db, plan, session=session, test_hooks=test_hooks)
                    try:
                        session.commit_transaction()
                    except Exception as exc:
                        if "UnknownTransactionCommitResult" in _error_labels(exc):
                            resolved = _unknown_commit_resolution(db, decision_map)
                            if resolved:
                                final_result = resolved
                                break
                            if attempt < max_attempts:
                                # The commit outcome is unknown, but the
                                # session may still consider the transaction
                                # active.  Close that transaction before
                                # starting the next same-payload attempt.
                                _abort_transaction(session)
                                continue
                            final_result = _result(
                                decision_map,
                                status="ERROR",
                                outcome=SLAReassignmentErrorCode.TRANSIENT_ERROR.value,
                                attempts=attempt,
                                committed=False,
                                abort_reason="unknown_commit_unconfirmed",
                                error_code=SLAReassignmentErrorCode.TRANSIENT_ERROR.value,
                            )
                            break
                        raise
                    final_result = _result(
                        decision_map,
                        status="APPLIED",
                        outcome=SLAReassignmentErrorCode.APPLIED.value,
                        attempts=attempt,
                        committed=True,
                        committed_at=started_at,
                        error_code=SLAReassignmentErrorCode.APPLIED.value,
                    )
                    break
                except _AlreadyApplied as exc:
                    _abort_transaction(session)
                    final_result = _existing_result(decision_map, exc.event, attempts=attempt)
                    break
                except _KnownAbort as exc:
                    _abort_transaction(session)
                    final_result = _result(
                        decision_map,
                        status="ABORTED",
                        outcome=exc.code.value,
                        attempts=attempt,
                        committed=False,
                        abort_reason=exc.reason,
                        error_code=exc.code.value,
                    )
                    break
                except DuplicateKeyError:
                    _abort_transaction(session)
                    replay = db[REASSIGNMENT_AUDIT_COLLECTION].find_one(
                        {"_id": decision_map.get("decision_id")}
                    )
                    if replay and _same_ledger_payload(replay, decision_map):
                        final_result = _existing_result(decision_map, replay, attempts=attempt)
                    else:
                        final_result = _result(
                            decision_map,
                            status="ABORTED",
                            outcome=SLAReassignmentErrorCode.ABORT_CYCLE_CHANGED.value,
                            attempts=attempt,
                            committed=False,
                            abort_reason="duplicate_key_race",
                            error_code=SLAReassignmentErrorCode.ABORT_CYCLE_CHANGED.value,
                        )
                    break
                except Exception as exc:
                    labels = _error_labels(exc)
                    if _is_transaction_unavailable(exc):
                        _abort_transaction(session)
                        final_result = _result(
                            decision_map,
                            status="ABORTED",
                            outcome=SLAReassignmentErrorCode.TRANSACTION_UNAVAILABLE.value,
                            attempts=attempt,
                            committed=False,
                            abort_reason="transaction_not_supported",
                            error_code=SLAReassignmentErrorCode.TRANSACTION_UNAVAILABLE.value,
                        )
                        break
                    _abort_transaction(session)
                    if "TransientTransactionError" in labels and attempt < max_attempts:
                        continue
                    final_result = _result(
                        decision_map,
                        status="ERROR",
                        outcome=SLAReassignmentErrorCode.TRANSIENT_ERROR.value,
                        attempts=attempt,
                        committed=False,
                        abort_reason=type(exc).__name__,
                        error_code=SLAReassignmentErrorCode.TRANSIENT_ERROR.value,
                    )
                    break
    except _TransactionUnavailable as exc:
        final_result = _result(
            decision_map,
            status="ABORTED",
            outcome=SLAReassignmentErrorCode.TRANSACTION_UNAVAILABLE.value,
            attempts=0,
            committed=False,
            abort_reason=str(exc),
            error_code=SLAReassignmentErrorCode.TRANSACTION_UNAVAILABLE.value,
        )

    if final_result is None:
        final_result = _result(
            decision_map,
            status="ERROR",
            outcome=SLAReassignmentErrorCode.TRANSIENT_ERROR.value,
            attempts=max_attempts,
            committed=False,
            abort_reason="executor_no_terminal_result",
            error_code=SLAReassignmentErrorCode.TRANSIENT_ERROR.value,
        )
    _log_result(
        final_result,
        branch=str(decision_map.get("policy_branch") or ""),
        transaction_duration_ms=(time.perf_counter() - transaction_started_clock) * 1000,
    )
    record_reassignment_metric(metrics, final_result)
    if metrics_hook:
        metrics_hook({
            "decision_id": final_result.decision_id,
            "lead_id": final_result.lead_id,
            "source_cycle_id": final_result.source_cycle_id,
            "destination_cycle_id": final_result.destination_cycle_id,
            "outcome": final_result.outcome,
            "attempt": final_result.transaction_attempts,
            "policy_version": final_result.policy_version,
        })
    return final_result
