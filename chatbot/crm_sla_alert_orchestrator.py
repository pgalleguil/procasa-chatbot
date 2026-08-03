"""CRM SLA Alert Orchestrator — exclusive loop for crm_sla_alert domain.

Runs: evaluation → persistence → quarantine → worker processing.
Fully isolated from lead delivery, HOT notifications, digest, chatbot.
"""
from __future__ import annotations

import asyncio
import logging
import os
import traceback

from .crm_sla_alert_pipeline import run_evaluation_and_persist_once
from .crm_sla_alert_repository import quarantine_stale_started_deliveries
from .crm_sla_alert_worker import process_alerts_batch
from .crm_sla_alert_sender import get_sender
from .crm_sla_alert_settings import (
    CRM_SLA_ALERTS_ENABLED,
    LIVE_SEND,
    MAX_PER_RUN,
    MAX_PER_RECIPIENT_PER_RUN,
)

logger = logging.getLogger("sla_alert.orchestrator")


async def run_sla_alert_cycle(db=None):
    """One complete cycle: evaluate, persist, process. Fully self-contained."""
    if not CRM_SLA_ALERTS_ENABLED:
        return {"status": "disabled", "queries": 0, "writes": 0, "claims": 0, "sends": 0}
    if db is None:
        from .storage import get_async_db
        db = get_async_db()

    worker_id = f"sla_orch:{os.getpid()}"

    try:
        # 1. Quarantine stale deliveries (never re-send automatically)
        await quarantine_stale_started_deliveries(db, limit=20)

        # 2. Evaluate + persist (idempotent)
        persist_report = await run_evaluation_and_persist_once(db=db, max_cycles=500)
        logger.info(
            "[SLA_ALERT][MONITOR] eval: candidates=%s persisted=%s already_exists=%s",
            persist_report.get("candidates_evaluated", 0),
            persist_report.get("persisted", 0),
            persist_report.get("already_exists", 0),
        )

        # 3. Process pending alerts (worker)
        worker_report = await process_alerts_batch(
            db=db, worker_id=worker_id,
            max_total=MAX_PER_RUN,
            max_per_recipient=MAX_PER_RECIPIENT_PER_RUN,
        )
        if worker_report.get("status") not in ("send_disabled", "idle"):
            logger.info("[SLA_ALERT][WORKER] processed=%s by_status=%s",
                        worker_report.get("processed"),
                        worker_report.get("by_status"))

    except Exception:
        logger.error("[SLA_ALERT][ERROR] Cycle failed:\n%s", traceback.format_exc())


async def sla_alert_orchestrator_loop(sleep_seconds: int = 60):
    """Main loop for crm_sla_alert. Never stops other workers on exception."""
    logger.info("[SLA_ALERT] Orchestrator started. enabled=%s live_send=%s",
                CRM_SLA_ALERTS_ENABLED, LIVE_SEND)

    if not CRM_SLA_ALERTS_ENABLED:
        logger.info("[SLA_ALERT] Disabled. Exiting loop.")
        return

    while True:
        try:
            await run_sla_alert_cycle()
        except Exception:
            logger.error("[SLA_ALERT][ERROR] Orchestrator loop error:\n%s", traceback.format_exc())
        await asyncio.sleep(sleep_seconds)
