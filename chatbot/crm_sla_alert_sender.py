"""CRM SLA Alert Sender — injectable transport with typed contract.

No imports from NotificationService, crm_hot_delivery, webhook, whatsapp_client.

Sender result types:
- confirmed_success: provider accepted → sent
- rejected_before_acceptance: provider explicitly rejected → retryable/final
- delivery_unknown: timeout, disconnect, ambiguous → delivery_uncertain
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from .crm_sla_alert_settings import CRM_SLA_ALERTS_LIVE_SEND

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SenderResult:
    outcome: str  # "confirmed_success" | "rejected_before_acceptance" | "delivery_unknown"
    provider_message_id: str | None = None
    error: str | None = None
    http_status: int | None = None


class SlaAlertSender(Protocol):
    async def __call__(self, phone: str, message: str) -> SenderResult:
        ...


class FakeSender:
    def __init__(self, outcome: str = "confirmed_success", provider_message_id: str = "fake-msg-001"):
        self.outcome = outcome
        self.provider_message_id = provider_message_id
        self._calls: list[dict] = []

    async def __call__(self, phone: str, message: str) -> SenderResult:
        self._calls.append({"phone": phone, "message": message})
        return SenderResult(
            outcome=self.outcome,
            provider_message_id=self.provider_message_id if self.outcome == "confirmed_success" else None,
        )


class FailingSender:
    def __init__(self, exc: Exception | None = None):
        self._exc = exc or TimeoutError("provider timeout")
        self._calls: list[dict] = []

    async def __call__(self, phone: str, message: str) -> SenderResult:
        self._calls.append({"phone": phone, "message": message})
        raise self._exc


async def _null_sender(phone: str, message: str) -> SenderResult:
    return SenderResult(outcome="delivery_unknown", error="live_send_disabled")


def get_sender(sender: SlaAlertSender | None = None) -> SlaAlertSender:
    if sender is not None:
        return sender
    if not CRM_SLA_ALERTS_LIVE_SEND:
        return _null_sender
    logger.warning("[SLA_ALERT] LIVE_SEND=true but no real sender injected")
    return _null_sender
