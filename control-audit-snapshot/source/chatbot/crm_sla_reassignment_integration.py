"""Async production adapter for the opt-in SLA reassignment transaction."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, MutableMapping

from .crm_sla_reassignment_executor import (
    MAX_TRANSACTION_ATTEMPTS,
    execute_sla_reassignment_transaction_async,
)
from .crm_sla_reassignment_models import SLAReassignmentResult
from .crm_sla_reassignment_notifications import enqueue_sla_reassignment_notification


@dataclass(frozen=True)
class SLAReassignmentExecution:
    result: SLAReassignmentResult
    notification: dict[str, Any] | None = None
    notification_error: str | None = None


async def execute_sla_reassignment_with_notification(
    db: Any,
    decision: Any,
    *,
    evaluated_at: datetime | None = None,
    max_attempts: int = MAX_TRANSACTION_ATTEMPTS,
    test_hooks: Any = None,
    metrics: MutableMapping[str, int] | None = None,
    metrics_hook: Callable[[Mapping[str, Any]], None] | None = None,
) -> SLAReassignmentExecution:
    """Commit the reassignment, then enqueue one notification for the new owner.

    Notification persistence is intentionally outside the Mongo transaction. A
    queue failure is surfaced in the return contract and logs, but ownership
    remains committed and is never rolled back because a provider is down.
    """
    result = await execute_sla_reassignment_transaction_async(
        db,
        decision,
        evaluated_at=evaluated_at,
        max_attempts=max_attempts,
        test_hooks=test_hooks,
        metrics=metrics,
        metrics_hook=metrics_hook,
    )
    if not result.committed or result.status not in {"APPLIED", "ALREADY_APPLIED"}:
        return SLAReassignmentExecution(result=result)

    try:
        notification = await asyncio.to_thread(
            enqueue_sla_reassignment_notification,
            db,
            decision=decision,
            result=result,
        )
        return SLAReassignmentExecution(result=result, notification=notification)
    except Exception as exc:
        # The assignment transaction has already committed.  Returning the
        # terminal result lets the caller monitor/retry notification creation
        # without attempting a destructive rollback.
        return SLAReassignmentExecution(result=result, notification_error=type(exc).__name__)
