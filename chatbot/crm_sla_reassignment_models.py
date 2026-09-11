"""Structured results for the opt-in CRM SLA reassignment engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class SLAReassignmentErrorCode(str, Enum):
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    ABORT_MANAGEMENT_DETECTED = "ABORT_MANAGEMENT_DETECTED"
    ABORT_CYCLE_CHANGED = "ABORT_CYCLE_CHANGED"
    ABORT_CYCLE_CLOSED = "ABORT_CYCLE_CLOSED"
    ABORT_OWNER_CHANGED = "ABORT_OWNER_CHANGED"
    ABORT_LEAD_CLOSED = "ABORT_LEAD_CLOSED"
    ABORT_SELECTED_USER_INACTIVE = "ABORT_SELECTED_USER_INACTIVE"
    ABORT_SELECTED_USER_INELIGIBLE = "ABORT_SELECTED_USER_INELIGIBLE"
    ABORT_POLICY_VERSION_CHANGED = "ABORT_POLICY_VERSION_CHANGED"
    ABORT_ASSIGNMENT_LIMIT_REACHED = "ABORT_ASSIGNMENT_LIMIT_REACHED"
    ABORT_PREVIOUS_OWNER = "ABORT_PREVIOUS_OWNER"
    ABORT_POINTER_MISMATCH = "ABORT_POINTER_MISMATCH"
    ABORT_SLA_NOT_EXPIRED = "ABORT_SLA_NOT_EXPIRED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    TRANSACTION_UNAVAILABLE = "TRANSACTION_UNAVAILABLE"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    SUPERVISOR_REVIEW_REQUIRED = "SUPERVISOR_REVIEW_REQUIRED"
    PRE_CUTOVER_ALREADY_EXPIRED = "PRE_CUTOVER_ALREADY_EXPIRED"
    CUTOVER_NOT_CONFIGURED = "CUTOVER_NOT_CONFIGURED"
    CUTOVER_CONFIGURATION_INVALID = "CUTOVER_CONFIGURATION_INVALID"


@dataclass(frozen=True)
class SLAReassignmentResult:
    decision_id: str
    lead_id: str
    source_cycle_id: str
    destination_cycle_id: str
    previous_owner_user_id: str | None
    selected_user_id: str | None
    status: str
    outcome: str
    committed: bool
    idempotent_replay: bool
    policy_version: str
    automatic_reassignment_number: int | None
    transaction_attempts: int
    committed_at: datetime | None = None
    abort_reason: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return only the non-PII result contract."""

        return asdict(self)
