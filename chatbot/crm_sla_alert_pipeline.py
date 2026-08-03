"""CRM SLA Alert Pipeline — evaluator → repository integration.

Reads from evaluator, applies canary filters, persists to crm_sla_alerts_v1.
Does NOT import worker, sender, NotificationService, whatsapp_client, or webhook.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

from .crm_sla_alert_evaluator import evaluate_sla_alerts
from .crm_sla_alert_repository import COLLECTION, persist_candidate
from .crm_sla_alert_settings import (
    CRM_SLA_ALERTS_ENABLED,
    CUTOVER_AT,
    MAX_PER_RECIPIENT_PER_RUN,
    MAX_PER_RUN,
    POLICY_VERSION,
    PERSIST_CONFIRMATION,
    validate_persist_config,
)
from .crm_sla_alert_templates import MESSAGE_DOMAIN
from .storage import get_async_db

import chatbot.crm_sla_alert_settings as _settings

logger = logging.getLogger(__name__)


async def run_evaluation_and_persist_once(
    db=None,
    *,
    ensure_indexes: bool = False,
    max_cycles: int = 2000,
    now=None,
) -> dict:
    """Evaluate SLA alerts and persist eligible candidates to crm_sla_alerts_v1.

    Returns a structured report with candidates, actions, and counts.
    """
    if not CRM_SLA_ALERTS_ENABLED:
        return {"status": "disabled", "candidates_evaluated": 0, "persisted": 0,
                "writes": 0, "provider_calls": 0, "claims": 0, "sends": 0}
    if db is None:
        db = get_async_db()

    # Validate config
    config = validate_persist_config()
    if not config["valid"]:
        return {"status": "config_blocked", "reason": config["reason"],
                "candidates_evaluated": 0, "persisted": 0, "writes": 0}

    # Cutover must be recent for persistence (not a historical cutover)
    cutover_safety = _settings.validate_cutover_safe_for_persistence(now=now)
    if not cutover_safety["valid"]:
        return {"status": "config_blocked", "reason": cutover_safety["reason"],
                "candidates_evaluated": 0, "persisted": 0, "writes": 0}

    # Create indexes if requested
    if ensure_indexes:
        from .crm_sla_alert_repository import ensure_crm_sla_alert_indexes
        await ensure_crm_sla_alert_indexes(db)

    # Evaluate
    eval_report = await evaluate_sla_alerts(db=db, limit_cycles=max_cycles,
                                             alert_cutover=CUTOVER_AT, now=now)
    candidates = eval_report.get("alerts", [])

    # Production mode: all eligible active agents with valid phones.
    authorized = []
    excluded_no_phone = 0
    for c in candidates:
        if not c.get("executive_phone"):
            excluded_no_phone += 1
            continue
        authorized.append(c)

    # Sort by deadline ascending
    authorized.sort(key=lambda c: c.get("deadline_dt") or datetime.max.replace(tzinfo=timezone.utc))

    # Apply limits
    max_total = MAX_PER_RUN
    max_per_recipient = MAX_PER_RECIPIENT_PER_RUN
    recipient_counts: Counter = Counter()
    to_persist = []

    for c in authorized:
        if len(to_persist) >= max_total:
            break
        uid = c.get("recipient_user_id", "")
        if recipient_counts[uid] >= max_per_recipient:
            continue
        recipient_counts[uid] += 1
        to_persist.append(c)

    excluded_by_limit = len(authorized) - len(to_persist)

    # Persist
    persisted = 0
    already_exists = 0
    now_utc = datetime.now(timezone.utc)

    for c in to_persist:
        # Enrich with pipeline metadata
        c["policy_version"] = POLICY_VERSION
        c["cutover_at"] = CUTOVER_AT.isoformat()
        c["persist_confirmation"] = PERSIST_CONFIRMATION
        c["evaluated_at"] = now_utc.isoformat()

        result = await persist_candidate(db, c)
        if result["status"] == "created":
            persisted += 1
        else:
            already_exists += 1

    return {
        "status": "completed",
        "candidates_evaluated": len(candidates),
        "authorized": len(authorized),
        "persisted": persisted,
        "already_exists": already_exists,
        "excluded_by_allowlist": 0,
        "excluded_no_phone": excluded_no_phone,
        "excluded_by_limit": excluded_by_limit,
        "writes": persisted,
        "provider_calls": 0,
        "allowlist_count": 0,
        "max_per_run": max_total,
        "max_per_recipient": max_per_recipient,
        "persist_confirmation_valid": True,
        "policy_version": POLICY_VERSION,
        "cutover_used": CUTOVER_AT.isoformat(),
    }
