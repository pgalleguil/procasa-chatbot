"""Fail-closed, one-shot recovery of authenticated historical SLA delivery.

This module is deliberately an execution surface around the existing narrow
delivery-recovery primitive.  It accepts only a validated JSON payload and
never sends a provider message, creates a cycle, selects an owner, or changes
ownership.  The primitive remains the sole writer of delivery/cycle state.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from .crm_sla_reassignment_notifications import (
    HISTORICAL_DELIVERY_EVIDENCE_SOURCE,
    recover_sla_reassignment_delivery_from_evidence,
)


logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = (
    "notification_id",
    "provider_message_id",
    "first_confirmed_observed_at",
    "evidence_source",
    "evidence_reference",
)


def _non_empty_text(value: Any) -> str:
    return str(value or "").strip()


def _validate_payload(payload_json: str | None) -> tuple[list[dict[str, Any]] | None, str | None]:
    raw = _non_empty_text(payload_json)
    if not raw:
        return None, "empty_payload"
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None, "invalid_json"
    if not isinstance(payload, list) or not payload:
        return None, "payload_must_be_non_empty_array"

    records: list[dict[str, Any]] = []
    notification_ids: set[str] = set()
    provider_ids: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            return None, f"record_{index}_must_be_object"
        record = dict(item)
        missing = [field for field in _REQUIRED_FIELDS if not _non_empty_text(record.get(field))]
        if missing:
            return None, f"record_{index}_missing_required_fields"
        if _non_empty_text(record.get("evidence_source")) != HISTORICAL_DELIVERY_EVIDENCE_SOURCE:
            return None, f"record_{index}_unsupported_evidence_source"

        notification_id = _non_empty_text(record["notification_id"])
        provider_id = _non_empty_text(record["provider_message_id"])
        if notification_id in notification_ids:
            return None, f"record_{index}_duplicate_notification_id"
        if provider_id in provider_ids:
            return None, f"record_{index}_duplicate_provider_message_id"
        notification_ids.add(notification_id)
        provider_ids.add(provider_id)
        records.append(record)
    return records, None


def run_sla_historical_recovery_once(
    db: Any,
    *,
    enabled: bool,
    payload_json: str | None,
    recovery_fn: Callable[..., dict[str, Any]] = recover_sla_reassignment_delivery_from_evidence,
    now: Any = None,
) -> dict[str, Any]:
    """Run the explicitly enabled recovery payload once.

    Invalid configuration is fail-closed and does not invoke the recovery
    primitive.  Each valid record is passed only to the existing primitive;
    its durable fields and the structured startup log are the audit trail.
    """
    if not enabled:
        result = {"status": "disabled", "writes": 0, "recovery_count": 0}
        logger.info("[SLA_HISTORICAL_RECOVERY] status=disabled writes=0")
        return result

    records, validation_error = _validate_payload(payload_json)
    if validation_error:
        result = {
            "status": "blocked",
            "reason": validation_error,
            "writes": 0,
            "recovery_count": 0,
        }
        logger.error(
            "[SLA_HISTORICAL_RECOVERY] status=blocked reason=%s writes=0",
            validation_error,
        )
        return result

    outcomes: list[dict[str, Any]] = []
    for record in records or []:
        call = dict(record)
        if now is not None:
            call["now"] = now
        try:
            outcome = recovery_fn(db, **call)
        except Exception as exc:  # startup must continue; no retry/send here
            logger.exception(
                "[SLA_HISTORICAL_RECOVERY] status=error notification_id=%s error_type=%s",
                record["notification_id"],
                type(exc).__name__,
            )
            outcomes.append({
                "notification_id": record["notification_id"],
                "status": "error",
                "reason": type(exc).__name__,
            })
            continue
        outcome = dict(outcome or {})
        outcomes.append({
            "notification_id": record["notification_id"],
            "provider_message_id": record["provider_message_id"],
            "status": outcome.get("status", "unknown"),
            "reason": outcome.get("reason"),
            "activation_status": (outcome.get("activation") or {}).get("status"),
        })

    writes = sum(1 for item in outcomes if item["status"] == "recovered")
    failures = [item for item in outcomes if item["status"] in {"blocked", "error", "not_found", "race_lost"}]
    result = {
        "status": "completed" if not failures else "partial_failure",
        "writes": writes,
        "recovery_count": len(outcomes),
        "outcomes": outcomes,
    }
    logger.info(
        "[SLA_HISTORICAL_RECOVERY] status=%s records=%s writes=%s outcomes=%s",
        result["status"],
        len(outcomes),
        writes,
        outcomes,
    )
    return result
