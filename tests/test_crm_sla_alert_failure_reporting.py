"""Failure paths must not advertise successful SLA alert cycles."""
import asyncio
import inspect

import pytest

from chatbot import crm_sla_alert_orchestrator as orchestrator


@pytest.mark.asyncio
async def test_sla_alert_mongo_failure_is_reported_not_swallowed(monkeypatch):
    async def quarantine(*args, **kwargs):
        return None

    async def fail(*args, **kwargs):
        raise TimeoutError("synthetic Mongo timeout")

    async def forbidden_worker(*args, **kwargs):
        raise AssertionError("worker must not run after failed evaluation")

    monkeypatch.setattr(orchestrator, "sla_alerts_enabled", lambda: True)
    monkeypatch.setattr(orchestrator, "quarantine_stale_started_deliveries", quarantine)
    monkeypatch.setattr(orchestrator, "run_evaluation_and_persist_once", fail)
    monkeypatch.setattr(orchestrator, "process_alerts_batch", forbidden_worker)

    report = await orchestrator.run_sla_alert_cycle(db=object())
    assert report["status"] == "error"
    assert report["error_type"] == "TimeoutError"
    assert report["persist_report"] is None
    assert report["worker_report"] is None


def test_webhook_marks_reported_alert_failures_unhealthy():
    import webhook

    source = inspect.getsource(webhook)
    assert 'if report.get("status") == "completed":' in source
    assert '"status": "error:cycle_failed"' in source
    assert 'logger.error(\n                            "[SLA_ALERT] ciclo fallido' in source
    assert 'logger.info("[SLA_ALERT] ciclo ok: %s", report)' in source
