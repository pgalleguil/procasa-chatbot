"""Pure contract builder for a future CRM SLA reassignment transaction.

This module deliberately has no MongoDB dependency and performs no I/O.  It
only turns already evaluated, policy-approved snapshots into a deterministic
transaction plan.  A production adapter must execute the plan with one
MongoDB session/transaction and must revalidate the snapshots immediately
before applying it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping, Sequence


ASSIGNMENT_CYCLES_COLLECTION = "crm_assignment_cycles"
LEADS_COLLECTION = "leads"
REASSIGNMENT_AUDIT_COLLECTION = "crm_sla_reassignment_audit_v1"

MAX_AUTOMATIC_REASSIGNMENTS = 2
SUPERVISOR_REVIEW_STATUSES = frozenset(
    {"PENDING", "APPROVED", "REJECTED", "NOT_REQUIRED"}
)


class TransactionContractError(ValueError):
    """Raised when a dry-run plan is not safe to persist."""


def _required(value: Any, name: str) -> Any:
    if value is None or value == "":
        raise TransactionContractError(f"missing_required:{name}")
    return value


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TransactionContractError(f"invalid_datetime:{name}")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def decision_new_cycle_id(decision_id: str) -> str:
    """Return a stable new-cycle identity for retries of one decision."""

    decision_id = str(_required(decision_id, "decision_id"))
    return f"sla-reassignment:{decision_id}"


def _previous_owner_ids(
    decision: Mapping[str, Any], source_cycle: Mapping[str, Any], current_owner: str
) -> tuple[str, ...]:
    """Resolve owner history without inventing a prior owner.

    The decision snapshot has precedence, then the source cycle's future
    contract fields, then the existing one-off metadata field.  For the first
    reassignment, the current source owner is the only known previous owner.
    """

    history = decision.get("previous_owner_user_ids")
    if history is None:
        history = source_cycle.get("previous_owner_user_ids")
    if history is None:
        metadata = source_cycle.get("reassignment_meta")
        if isinstance(metadata, Mapping):
            history = metadata.get("previous_owner_user_ids")
    if history is None:
        history = []
    if isinstance(history, (str, bytes)) or not isinstance(history, Sequence):
        raise TransactionContractError("invalid_previous_owner_user_ids")

    result: list[str] = []
    for owner_id in history:
        owner_id = str(owner_id)
        if owner_id and owner_id not in result:
            result.append(owner_id)
    if current_owner not in result:
        result.insert(0, current_owner)
    return tuple(result)


def _management_absent_filter(field: str) -> dict[str, Any]:
    return {
        "$or": [
            {field: {"$exists": False}},
            {field: None},
        ]
    }


def build_source_cycle_filter(
    decision: Mapping[str, Any],
    source_cycle: Mapping[str, Any],
    *,
    expected_sla_policy_version: str,
) -> dict[str, Any]:
    """Build the compare-and-set filter for the source active cycle."""

    lead_id = str(_required(decision.get("lead_id"), "lead_id"))
    cycle_id = str(
        _required(
            decision.get("current_assignment_cycle_id"),
            "current_assignment_cycle_id",
        )
    )
    if str(source_cycle.get("lead_id")) != lead_id:
        raise TransactionContractError("source_cycle_lead_mismatch")
    if str(source_cycle.get("assignment_cycle_id")) != cycle_id:
        raise TransactionContractError("source_cycle_id_mismatch")

    current_owner = str(_required(source_cycle.get("assigned_to_user_id"), "source_owner"))
    decision_owner = decision.get("previous_owner_user_id")
    if decision_owner not in (None, "") and str(decision_owner) != current_owner:
        raise TransactionContractError("decision_owner_mismatch")

    assigned_at = _required(source_cycle.get("assigned_at"), "source_assigned_at")
    policy_version = str(_required(expected_sla_policy_version, "sla_policy_version"))

    assignment_number = decision.get(
        "automatic_reassignment_number", decision.get("assignment_number")
    )
    if assignment_number is None:
        raise TransactionContractError("missing_required:assignment_number")
    assignment_number = int(assignment_number)
    # Fase 1G stores the number of automatic rescues already performed on the
    # source cycle: 0 means the first rescue, 1 the second, and 2 is blocked.
    if not 0 <= assignment_number < MAX_AUTOMATIC_REASSIGNMENTS:
        raise TransactionContractError("assignment_limit_reached")

    source_filter: dict[str, Any] = {
        # Decisions transport string IDs, while live cycles can use BSON
        # ObjectId.  Keep the exact identity from the source snapshot.
        "lead_id": source_cycle["lead_id"],
        "assignment_cycle_id": cycle_id,
        "assigned_at": assigned_at,
        "assigned_to_user_id": current_owner,
        "cycle_status": "active",
        "unassigned_at": None,
        "sla_policy_version": policy_version,
        "reassignment_state": {"$in": [None, "eligible"]},
        "reassignment_decision_id": {"$exists": False},
            "$and": [
            _management_absent_filter("first_valid_management_at"),
            _management_absent_filter("first_contact_attempt_at"),
            _management_absent_filter("reassignment_protection_at"),
            {
                "$or": [
                    {"automatic_reassignment_number": {"$exists": False}},
                    {
                        "automatic_reassignment_number": {
                            "$lt": MAX_AUTOMATIC_REASSIGNMENTS
                        }
                    },
                ]
            },
        ],
    }
    if source_cycle.get("_id") is not None:
        source_filter["_id"] = source_cycle["_id"]
    if source_cycle.get("cycle_version") is not None:
        source_filter["cycle_version"] = source_cycle["cycle_version"]
    return source_filter


def build_source_cycle_close_update(
    *,
    reassigned_at: datetime,
    target_user_id: str,
    decision_id: str,
    new_cycle_id: str,
    previous_owner_user_ids: Sequence[str],
    assignment_number: int,
    source_cycle_version: Any = None,
) -> dict[str, Any]:
    """Build the source-cycle close update.  It never changes source owner."""

    reassigned_at = _aware(reassigned_at, "reassigned_at")
    try:
        next_cycle_version = int(source_cycle_version) + 1
    except (TypeError, ValueError):
        # Existing cycles may not have a version.  The first transactional
        # mutation initializes it; no backfill is performed here.
        next_cycle_version = 1
    return {
        "$set": {
            "cycle_status": "reassigned",
            "unassigned_at": reassigned_at,
            "closed_at": reassigned_at,
            "closed_reason": "sla_reassignment",
            "reassigned_at": reassigned_at,
            "reassigned_to_user_id": str(_required(target_user_id, "target_user_id")),
            "reassignment_decision_id": str(_required(decision_id, "decision_id")),
            "reassignment_new_cycle_id": str(_required(new_cycle_id, "new_cycle_id")),
            "previous_owner_user_ids": list(previous_owner_user_ids),
            "automatic_reassignment_number": int(assignment_number),
            "cycle_version": next_cycle_version,
            "reassignment_state": "completed",
            "updated_at": reassigned_at,
        }
    }


def build_lead_owner_filter(
    *, lead: Mapping[str, Any], source_cycle: Mapping[str, Any]
) -> dict[str, Any]:
    """Build a fail-closed CAS filter for the lead mirror update."""

    lead_id = str(_required(lead.get("_id"), "lead_id"))
    source_cycle_id = str(
        _required(source_cycle.get("assignment_cycle_id"), "source_cycle_id")
    )
    source_display = str(
        _required(source_cycle.get("assigned_to_display_name"), "source_owner_display_name")
    )
    if str(lead.get("lifecycle", {}).get("current_assignment_cycle_id")) != source_cycle_id:
        raise TransactionContractError("lead_cycle_pointer_mismatch")
    if str(lead.get("ejecutivo_asignado")) != source_display:
        raise TransactionContractError("lead_owner_mirror_mismatch:ejecutivo_asignado")
    if str(lead.get("prospecto", {}).get("ejecutivo")) != source_display:
        raise TransactionContractError("lead_owner_mirror_mismatch:prospecto.ejecutivo")

    return {
        "_id": lead["_id"],
        "lifecycle.current_assignment_cycle_id": source_cycle_id,
        "ejecutivo_asignado": source_display,
        "prospecto.ejecutivo": source_display,
        "closed_at": {"$exists": False},
        "archived_at": {"$exists": False},
        "suppressed": {"$ne": True},
        "pipeline_stage": {"$nin": ["CLOSED", "ARCHIVED", "SUPPRESSED", "closed"]},
        "stage": {"$nin": ["CLOSED", "ARCHIVED", "SUPPRESSED", "closed"]},
    }


def build_lead_owner_mirror_update(
    *,
    target_user_id: str,
    target_display_name: str,
    new_cycle_id: str,
    reassigned_at: datetime,
) -> dict[str, Any]:
    """Build only derived lead mirrors and the current-cycle pointer."""

    return {
        "$set": {
            "ejecutivo_asignado": str(_required(target_display_name, "target_display_name")),
            "prospecto.ejecutivo": str(
                _required(target_display_name, "target_display_name")
            ),
            "lifecycle.assigned_at": _aware(reassigned_at, "reassigned_at"),
            "lifecycle.current_assignment_cycle_id": str(
                _required(new_cycle_id, "new_cycle_id")
            ),
            "last_crm_update": _aware(reassigned_at, "reassigned_at"),
            "assignment_mirror_source": "crm_assignment_cycles",
            "assignment_mirror_owner_user_id": str(
                _required(target_user_id, "target_user_id")
            ),
        }
    }


def build_new_cycle_document(
    *,
    decision: Mapping[str, Any],
    source_cycle: Mapping[str, Any],
    target_user: Mapping[str, Any],
    new_cycle_id: str,
    reassigned_at: datetime,
    effective_sla_started_at: datetime,
    expected_sla_policy_version: str,
    previous_owner_user_ids: Sequence[str],
    assignment_number: int,
    supervisor_review_status: str,
    new_temperature: str | None = None,
) -> dict[str, Any]:
    """Build a new active cycle while preserving current CRM routing shape."""

    target_id = str(_required(target_user.get("_id"), "target_user_id"))
    if target_user.get("is_active") is not True:
        raise TransactionContractError("selected_user_inactive")
    if str(decision.get("selected_user_id")) != target_id:
        raise TransactionContractError("selected_user_mismatch")
    if supervisor_review_status not in SUPERVISOR_REVIEW_STATUSES:
        raise TransactionContractError("invalid_supervisor_review_status")
    if not 1 <= int(assignment_number) <= MAX_AUTOMATIC_REASSIGNMENTS:
        raise TransactionContractError("assignment_limit_reached")

    decision_id = str(_required(decision.get("decision_id"), "decision_id"))
    lead_id = str(_required(decision.get("lead_id"), "lead_id"))
    assigned_at = _aware(reassigned_at, "reassigned_at")
    sla_started_at = _aware(effective_sla_started_at, "effective_sla_started_at")
    source_reason = _required(source_cycle.get("reason"), "source_reason")
    source_origin = _required(source_cycle.get("cycle_origin"), "source_cycle_origin")
    notification_eligible = source_cycle.get("notification_eligible")
    if notification_eligible is None:
        raise TransactionContractError("missing_required:notification_eligible")
    target_display = str(
        _first_present(target_user, "nombre", "display_name", "name")
        or _required(decision.get("selected_user_display_name"), "target_display_name")
    )

    document: dict[str, Any] = {
        "_id": new_cycle_id,
        "assignment_cycle_id": new_cycle_id,
        # Preserve the BSON identity used by the source cycle.  Decisions
        # carry transport strings, but future active-cycle queries must still
        # resolve an ObjectId lead correctly after a reassignment.
        "lead_id": source_cycle.get("lead_id", lead_id),
        "assigned_to_user_id": target_id,
        "assigned_to_display_name": target_display,
        "assigned_at": assigned_at,
        "cycle_started_at": assigned_at,
        "sla_started_at": sla_started_at,
        "temperature_at_assignment": str(
            new_temperature or source_cycle.get("temperature_at_assignment") or "COLD"
        ).upper(),
        "unassigned_at": None,
        "assigned_by": "sla_reassignment",
        # Preserve fields used by existing CRM list/detail queries.
        "reason": source_reason,
        "cycle_origin": source_origin,
        "notification_eligible": notification_eligible,
        "property_code": source_cycle.get("property_code"),
        "source_event_id": source_cycle.get("source_event_id"),
        "metric_version": source_cycle.get("metric_version", "crm_metrics_v1"),
        "schema_version": source_cycle.get(
            "schema_version", "crm_assignment_cycle_v1"
        ),
        "cycle_status": "active",
        "applied_transition_ids": [],
        "sla_policy_version": str(
            _required(expected_sla_policy_version, "sla_policy_version")
        ),
        "policy_version": decision.get("policy_version"),
        "policy_branch": decision.get("policy_branch"),
        "selected_score": decision.get("selected_score"),
        "candidate_scores_snapshot": decision.get("candidate_scores_snapshot"),
        "selection_rule": decision.get("selection_rule"),
        "guardrail_applied": decision.get("guardrail_applied"),
        "guardrail_reason": decision.get("guardrail_reason"),
        "jpc_target_share": decision.get("jpc_target_share"),
        "previous_cycle_sla_breached_at": decision.get("source_cycle_sla_breached_at") or decision.get("sla_breached_at"),
        "reassignment_cutover_at": decision.get("reassignment_cutover_at"),
        "source_cycle_sla_breached_at": decision.get("source_cycle_sla_breached_at") or decision.get("sla_breached_at"),
        "cutover_eligible": decision.get("cutover_eligible"),
        "cutover_policy_version": decision.get("cutover_policy_version"),
        "first_valid_management_at": None,
        "first_contact_attempt_at": None,
        "first_effective_contact_at": None,
        "reassignment_protection_at": None,
        "reassignment_protection_type": None,
        "reassignment_protection_actor_user_id": None,
        "sla_first_management_status": "pending",
        "reassignment_source_cycle_id": str(
            _required(source_cycle.get("assignment_cycle_id"), "source_cycle_id")
        ),
        "previous_assignment_cycle_id": str(
            _required(source_cycle.get("assignment_cycle_id"), "source_cycle_id")
        ),
        "reassignment_decision_id": decision_id,
        "reassignment_reason": "sla_reassignment",
        "previous_owner_user_ids": list(previous_owner_user_ids),
        "automatic_reassignment_number": int(assignment_number),
        "supervisor_review_status": supervisor_review_status,
        "reassignment_state": "active",
        "cycle_version": 1,
        "created_at": assigned_at,
        "updated_at": assigned_at,
    }
    # Keep the source owner identity as an ID only.  The active destination
    # display remains the existing CRM presentation field.
    document["reassigned_from_owner_user_id"] = str(
        _required(source_cycle.get("assigned_to_user_id"), "source_owner")
    )
    return document


def build_reassignment_audit_event(
    *,
    decision: Mapping[str, Any],
    source_cycle: Mapping[str, Any],
    lead: Mapping[str, Any],
    target_user: Mapping[str, Any],
    new_cycle_id: str,
    reassigned_at: datetime,
    previous_owner_user_ids: Sequence[str],
    assignment_number: int,
    supervisor_review_status: str,
) -> dict[str, Any]:
    """Build the immutable, idempotency-bearing audit event."""

    decision_id = str(_required(decision.get("decision_id"), "decision_id"))
    source_cycle_id = str(
        _required(source_cycle.get("assignment_cycle_id"), "source_cycle_id")
    )
    source_owner_id = str(
        _required(source_cycle.get("assigned_to_user_id"), "source_owner")
    )
    target_id = str(_required(target_user.get("_id"), "target_user_id"))
    timestamp = _aware(reassigned_at, "reassigned_at")

    return {
        "_id": decision_id,
        "event_type": "SLA_REASSIGNMENT_COMMITTED",
        "schema_version": "crm_sla_reassignment_audit_v1",
        "immutable": True,
        "decision_id": decision_id,
        "lead_id": str(_required(decision.get("lead_id"), "lead_id")),
        "source_cycle_id": source_cycle_id,
        "new_cycle_id": str(_required(new_cycle_id, "new_cycle_id")),
        "destination_cycle_id": str(_required(new_cycle_id, "new_cycle_id")),
        "previous_owner_user_ids": list(previous_owner_user_ids),
        "source_owner_user_id": source_owner_id,
        "previous_owner_user_id": source_owner_id,
        "target_owner_user_id": target_id,
        "selected_user_id": target_id,
        "reassigned_at": timestamp,
        "policy_version": decision.get("policy_version"),
        "policy": decision.get("policy_version"),
        "policy_branch": decision.get("policy_branch"),
        "branch": decision.get("policy_branch"),
        "sla_breached_at": decision.get("sla_breached_at"),
        "reassignment_cutover_at": decision.get("reassignment_cutover_at"),
        "source_cycle_sla_breached_at": decision.get("source_cycle_sla_breached_at") or decision.get("sla_breached_at"),
        "cutover_eligible": decision.get("cutover_eligible"),
        "cutover_policy_version": decision.get("cutover_policy_version"),
        "cutover_validation_result": decision.get("cutover_validation_result") or (
            "CUTOVER_ELIGIBLE" if decision.get("cutover_eligible") is True else "NOT_VALIDATED"
        ),
        "assignment_number": int(assignment_number),
        "automatic_reassignment_number": int(assignment_number),
        "supervisor_review_status": supervisor_review_status,
        "selection_rule": decision.get("selection_rule"),
        "selection_reason": decision.get("selection_reason"),
        "performance_confidence": decision.get("performance_confidence"),
        "candidate_user_ids": list(decision.get("candidate_user_ids") or []),
        "excluded_previous_owners": list(previous_owner_user_ids),
        "candidate_scores_snapshot": decision.get("candidate_scores_snapshot"),
        "scores": decision.get("candidate_scores_snapshot"),
        "before": {
            "lead_cycle_pointer": lead.get("lifecycle", {}).get(
                "current_assignment_cycle_id"
            ),
            "cycle_status": source_cycle.get("cycle_status"),
            "cycle_owner_user_id": source_owner_id,
            "first_valid_management_at": source_cycle.get("first_valid_management_at"),
        },
        "after": {
            "lead_cycle_pointer": str(_required(new_cycle_id, "new_cycle_id")),
            "lead_owner_user_id": target_id,
            "cycle_status": "active",
            "first_valid_management_at": None,
        },
        "actor": "sla_reassignment_service",
        "commit_time": timestamp,
        "created_at": timestamp,
    }


@dataclass(frozen=True)
class TransactionOperation:
    collection: str
    operation: str
    filter: Mapping[str, Any] | None = None
    update: Mapping[str, Any] | None = None
    document: Mapping[str, Any] | None = None
    expected: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReassignmentTransactionPlan:
    decision_id: str
    lead_id: str
    source_cycle_id: str
    new_cycle_id: str
    supervisor_review_status: str
    operations: tuple[TransactionOperation, ...]
    preconditions: tuple[Mapping[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "lead_id": self.lead_id,
            "source_cycle_id": self.source_cycle_id,
            "new_cycle_id": self.new_cycle_id,
            "supervisor_review_status": self.supervisor_review_status,
            "preconditions": list(self.preconditions),
            "operations": [operation.as_dict() for operation in self.operations],
        }


def build_transaction_plan(
    *,
    decision: Mapping[str, Any],
    source_cycle: Mapping[str, Any],
    lead: Mapping[str, Any],
    target_user: Mapping[str, Any],
    reassigned_at: datetime,
    effective_sla_started_at: datetime,
    expected_sla_policy_version: str,
    supervisor_review_status: str = "PENDING",
) -> ReassignmentTransactionPlan:
    """Build the four writes required by the future atomic operation.

    The order is intentional: source-cycle CAS, new-cycle insert, lead mirror
    CAS, immutable audit insert.  A MongoDB transaction must roll all four
    back if any operation fails.  This function itself executes none of them.
    """

    if supervisor_review_status not in SUPERVISOR_REVIEW_STATUSES:
        raise TransactionContractError("invalid_supervisor_review_status")
    if source_cycle.get("cycle_status") != "active" or source_cycle.get("unassigned_at") is not None:
        raise TransactionContractError("source_cycle_not_active")
    if source_cycle.get("first_valid_management_at") not in (None, ""):
        raise TransactionContractError("management_detected")
    if source_cycle.get("first_contact_attempt_at") not in (None, ""):
        raise TransactionContractError("management_detected")
    if source_cycle.get("reassignment_protection_at") not in (None, ""):
        raise TransactionContractError("management_detected")

    decision_id = str(_required(decision.get("decision_id"), "decision_id"))
    lead_id = str(_required(decision.get("lead_id"), "lead_id"))
    if str(lead.get("_id")) != lead_id:
        raise TransactionContractError("lead_id_mismatch")
    source_cycle_id = str(
        _required(decision.get("current_assignment_cycle_id"), "source_cycle_id")
    )
    if str(source_cycle.get("assignment_cycle_id")) != source_cycle_id:
        raise TransactionContractError("source_cycle_id_mismatch")

    target_id = str(_required(decision.get("selected_user_id"), "selected_user_id"))
    current_owner = str(_required(source_cycle.get("assigned_to_user_id"), "source_owner"))
    if target_id == current_owner:
        raise TransactionContractError("target_is_current_owner")
    if target_user.get("is_active") is not True:
        raise TransactionContractError("selected_user_inactive")

    assignment_number_value = decision.get(
        "automatic_reassignment_number", decision.get("assignment_number")
    )
    if assignment_number_value is None:
        raise TransactionContractError("missing_required:assignment_number")
    source_assignment_number = int(assignment_number_value)
    if not 0 <= source_assignment_number < MAX_AUTOMATIC_REASSIGNMENTS:
        raise TransactionContractError("assignment_limit_reached")
    destination_assignment_number = source_assignment_number + 1

    previous_owners = _previous_owner_ids(decision, source_cycle, current_owner)
    if target_id in previous_owners:
        raise TransactionContractError("target_in_previous_owner_history")
    new_cycle_id = decision_new_cycle_id(decision_id)

    source_filter = build_source_cycle_filter(
        decision,
        source_cycle,
        expected_sla_policy_version=expected_sla_policy_version,
    )
    source_close = build_source_cycle_close_update(
        reassigned_at=reassigned_at,
        target_user_id=target_id,
        decision_id=decision_id,
        new_cycle_id=new_cycle_id,
        previous_owner_user_ids=previous_owners,
        assignment_number=destination_assignment_number,
        source_cycle_version=source_cycle.get("cycle_version"),
    )
    lead_filter = build_lead_owner_filter(lead=lead, source_cycle=source_cycle)
    lead_update = build_lead_owner_mirror_update(
        target_user_id=target_id,
        target_display_name=str(
            _first_present(target_user, "nombre", "display_name", "name")
            or _required(decision.get("selected_user_display_name"), "target_display_name")
        ),
        new_cycle_id=new_cycle_id,
        reassigned_at=reassigned_at,
    )
    new_cycle = build_new_cycle_document(
        decision=decision,
        source_cycle=source_cycle,
        target_user=target_user,
        new_cycle_id=new_cycle_id,
        reassigned_at=reassigned_at,
        effective_sla_started_at=effective_sla_started_at,
        expected_sla_policy_version=expected_sla_policy_version,
        previous_owner_user_ids=previous_owners,
        assignment_number=destination_assignment_number,
        supervisor_review_status=supervisor_review_status,
        new_temperature=str(
            lead.get("lead_temperature_effective")
            or source_cycle.get("temperature_at_assignment")
            or "COLD"
        ),
    )
    audit_event = build_reassignment_audit_event(
        decision=decision,
        source_cycle=source_cycle,
        lead=lead,
        target_user=target_user,
        new_cycle_id=new_cycle_id,
        reassigned_at=reassigned_at,
        previous_owner_user_ids=previous_owners,
        assignment_number=destination_assignment_number,
        supervisor_review_status=supervisor_review_status,
    )

    return ReassignmentTransactionPlan(
        decision_id=decision_id,
        lead_id=lead_id,
        source_cycle_id=source_cycle_id,
        new_cycle_id=new_cycle_id,
        supervisor_review_status=supervisor_review_status,
        preconditions=(
            {
                "name": "future_only_cutover_at_commit",
                "reassignment_cutover_at": decision.get("reassignment_cutover_at"),
                "source_cycle_sla_breached_at": decision.get("source_cycle_sla_breached_at") or decision.get("sla_breached_at"),
                "cutover_eligible": decision.get("cutover_eligible"),
                "cutover_policy_version": decision.get("cutover_policy_version"),
                "outcome_if_false": "CUTOVER_CONFIGURATION_INVALID",
            },
            {
                "name": "selected_user_active_at_commit",
                "collection": "usuarios",
                "filter": {"_id": target_user.get("_id"), "is_active": True},
                "outcome_if_false": "ABORT_SELECTED_USER_INACTIVE",
            },
        ),
        operations=(
            TransactionOperation(
                collection=ASSIGNMENT_CYCLES_COLLECTION,
                operation="update_one",
                filter=source_filter,
                update=source_close,
                expected="matched_count == 1",
            ),
            TransactionOperation(
                collection=ASSIGNMENT_CYCLES_COLLECTION,
                operation="insert_one",
                document=new_cycle,
                expected="inserted_id == new_cycle_id",
            ),
            TransactionOperation(
                collection=LEADS_COLLECTION,
                operation="update_one",
                filter=lead_filter,
                update=lead_update,
                expected="matched_count == 1",
            ),
            TransactionOperation(
                collection=REASSIGNMENT_AUDIT_COLLECTION,
                operation="insert_one",
                document=audit_event,
                expected="inserted_id == decision_id",
            ),
        ),
    )


def failure_injection_matrix() -> tuple[dict[str, str], ...]:
    """Expected rollback contract for adapter-level failure-injection tests."""

    stages = (
        "after_source_cycle_close",
        "after_new_cycle_insert",
        "after_lead_mirror_update",
        "after_audit_insert_before_commit",
    )
    return tuple(
        {
            "inject_after": stage,
            "expected": "ROLLBACK_NO_PARTIAL_STATE",
            "requires_same_decision_retry": "true",
        }
        for stage in stages
    )


def idempotency_key_for_decision(decision_id: str) -> str:
    """Stable ledger key; useful when the adapter needs a bounded key field."""

    raw = str(_required(decision_id, "decision_id")).encode("utf-8")
    return sha256(raw).hexdigest()
