from datetime import datetime, timedelta, timezone
import asyncio
import threading

from chatbot.crm_sla_dry_run import evaluate_sla_alert_dry_run
from chatbot.storage import run_in_threadpool

NOW = datetime(2026, 7, 27, 16, tzinfo=timezone.utc)

def test_sla_dry_run_is_executive_only_and_idempotent():
    users = [{"_id": "exec", "active": True, "telefono": "+56911112222"}]
    leads = [
        {"_id": "hot", "lead_temperature_effective": "HOT"},
        {"_id": "normal", "lead_temperature_effective": "COLD"},
        {"_id": "waiting", "assignment_type": "WAITING_PROPERTY"},
        {"_id": "managed", "first_valid_management_at": NOW},
    ]
    cycles = [
        {"assignment_cycle_id": "c-hot", "lead_id": "hot", "assigned_to_user_id": "exec", "assigned_at": NOW-timedelta(minutes=70), "cycle_status": "active"},
        {"assignment_cycle_id": "c-normal", "lead_id": "normal", "assigned_to_user_id": "exec", "assigned_at": NOW-timedelta(minutes=200), "cycle_status": "active"},
        {"assignment_cycle_id": "c-wait", "lead_id": "waiting", "assigned_to_user_id": "exec", "assigned_at": NOW-timedelta(minutes=200), "cycle_status": "active"},
        {"assignment_cycle_id": "c-managed", "lead_id": "managed", "assigned_to_user_id": "exec", "assigned_at": NOW-timedelta(minutes=200), "cycle_status": "active"},
    ]
    first = evaluate_sla_alert_dry_run(leads=leads, cycles=cycles, users=users, as_of=NOW, activation_at=NOW-timedelta(hours=4))
    second = evaluate_sla_alert_dry_run(leads=leads, cycles=cycles, users=users, as_of=NOW, activation_at=NOW-timedelta(hours=4))
    assert first == second
    assert {row["message_type"] for row in first["alerts"]} == {"hot_breached", "normal_breached"}
    assert all(row["message_domain"] == "crm_sla_alert" and row["recipient_role"] == "executive" for row in first["alerts"])
    assert first["provider_calls"] == 0 and first["writes"] == 0
    assert first["excluded"]["waiting_assignment"] == 1
    assert first["excluded"]["human_management"] == 1

def test_threadpool_context_is_propagated_without_marking_main_thread():
    async def run():
        return await run_in_threadpool(lambda: threading.current_thread() is threading.main_thread())
    assert asyncio.run(run()) is False
