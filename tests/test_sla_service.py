import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from chatbot.constants import CHILE_TZ
from chatbot.crm_metrics import calculate_sla


class AsyncCursor:
    def __init__(self, docs): self.docs = list(docs)
    async def to_list(self, length=None): return self.docs[:length]


class AsyncCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.inserted = []
    def find(self, query, projection=None): return AsyncCursor(self.docs)
    async def distinct(self, field, query=None): return []
    async def insert_one(self, doc): self.inserted.append(doc)


class AsyncDB(dict):
    pass


def chile_datetime(hour, minute=0):
    return CHILE_TZ.localize(datetime(2026, 7, 20, hour, minute))


def assigned_lead(assigned_at=None, first_management=None):
    lifecycle = {"assigned_at": assigned_at or chile_datetime(9)}
    if first_management:
        lifecycle["first_valid_management_at"] = first_management
    return {
        "_id": "lead-1", "phone": "+56988888888", "ejecutivo_asignado": "Ejecutiva",
        "pipeline_stage": "NEW", "lead_temperature_effective": "HOT",
        "created_at": chile_datetime(9) - timedelta(days=1),
        "lifecycle": lifecycle, "prospecto": {"nombre": "Cliente"},
    }


def test_sla_starts_at_valid_assignment_and_is_pending_without_management():
    lead = assigned_lead()
    result = calculate_sla(assigned_at=lead["lifecycle"]["assigned_at"], now=chile_datetime(11, 31))
    assert result["fulfilled"] is False
    assert result["status"] == "near_critical"


def test_first_valid_human_management_fulfils_sla_but_app_opening_does_not():
    assigned = chile_datetime(9)
    managed = assigned + timedelta(minutes=30)
    fulfilled = calculate_sla(assigned_at=assigned, first_valid_management_at=managed)
    still_pending = calculate_sla(assigned_at=assigned, first_valid_management_at=None, now=chile_datetime(12, 20))
    assert fulfilled["status"] == "fulfilled"
    assert still_pending["status"] == "critical"


def test_async_sla_monitor_uses_shared_definition_and_deduplicated_notification():
    lead = assigned_lead()
    db = AsyncDB(
        leads=AsyncCollection([lead]),
        crm_events=AsyncCollection([{
            "lead_id": lead["_id"], "phone": "56988888888", "type": "ASSIGNMENT",
            "actor": "system", "actor_type": "system", "timestamp": lead["lifecycle"]["assigned_at"],
        }]),
        crm_sla_warnings=AsyncCollection(),
    )
    send = AsyncMock(return_value=True)
    with patch("chatbot.sla_service.get_async_db", return_value=db), \
         patch("chatbot.sla_service.Config.CRM_SLA_ALERTS_ENABLED", True), \
         patch("chatbot.sla_service.should_send_now", return_value=True), \
         patch("chatbot.sla_service.get_executive_phone", return_value="+56912345678"), \
         patch("chatbot.sla_service.utc_now", return_value=chile_datetime(12, 1).astimezone(timezone.utc)), \
         patch("chatbot.sla_service.NotificationService.send_notification", send), \
         patch("chatbot.sla_service.asyncio.sleep", new=AsyncMock()):
        from chatbot.sla_service import monitor_sla_thresholds
        asyncio.run(monitor_sla_thresholds())
    send.assert_awaited_once()
    assert len(db["crm_sla_warnings"].inserted) == 1
    assert db["crm_sla_warnings"].inserted[0]["level"] == "critical"
