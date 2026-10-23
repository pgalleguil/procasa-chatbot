"""Production contracts for CRM user-triggered refresh and five-minute SLA alert cadence.

This test module is intentionally read-only: it does not alter stored leads or
touch the real MongoDB database.
"""
from pathlib import Path

import pytest

from chatbot.crm_sla_alert_orchestrator import (
    SLA_ALERT_CADENCE_SECONDS,
    next_sla_alert_delay_seconds,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("elapsed,expected_delay", [
    (0, 300),
    (60, 240),
    (180, 120),
    (270, 30),
    (299.5, 0.5),
    (300, 60),      # overrun cooldown prevents back-to-back scans
    (480, 60),
])
def test_sla_alert_cycle_has_five_minute_start_cadence(elapsed, expected_delay):
    assert SLA_ALERT_CADENCE_SECONDS == 300
    assert next_sla_alert_delay_seconds(elapsed) == expected_delay


def test_sla_alert_backoff_still_applies_to_errors():
    assert next_sla_alert_delay_seconds(290, retry_seconds=120) == 120
    assert next_sla_alert_delay_seconds(360, retry_seconds=240) == 240


def test_webhook_uses_start_to_start_cadence_only_for_alerts():
    source = (ROOT / "webhook.py").read_text(encoding="utf-8")
    start = source.index("    async def _sla_alert_orchestrator_runner():")
    end = source.index("    sla_orch_task = None", start)
    block = source[start:end]
    assert "cycle_started_monotonic = time.monotonic()" in block
    assert "next_sla_alert_delay_seconds(" in block
    assert "time.monotonic() - cycle_started_monotonic" in block
    assert "await asyncio.sleep(delay_to_next)" in block
    assert "await asyncio.sleep(delay_seconds)" not in block
    # Do not change the reassignment worker, SLA policies or due-time calculator.
    assert "run_sla_alert_cycle()" in block


def test_crm_list_fetches_only_on_user_action_not_periodically():
    html = (ROOT / "templates/crm_leads_list.html").read_text(encoding="utf-8")
    assert "CRM_POLL_INTERVAL_MS" not in html
    assert "checkCrmUpdates(" not in html
    assert 'onclick="refreshCrmList({ force: true })"' in html
    assert "const CRM_MUTATION_ENDPOINTS =" in html
    assert "document.addEventListener('crm:mutation-complete'" in html
    assert "queueMicrotask(() => refreshCrmList({ force: true }))" in html
    assert "if (event.persisted) window.location.reload();" in html
    assert "fetch(`/crm/partial${window.location.search}`" in html


def test_evaluator_queries_recipients_once_per_cycle_without_cross_cycle_cache():
    source = (ROOT / "chatbot/crm_sla_alert_evaluator.py").read_text(encoding="utf-8")
    assert "active_agents_by_id = await _get_active_agents_batched(db, cycles)" in source
    assert "recipient_user = active_agents_by_id.get(recipient_user_id)" in source
    assert "recipient_user = await _get_assigned_agent_async(" not in source
    assert '"is_active": True, "rol": "agente"' in source
